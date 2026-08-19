"""闸门的两个「在开发机上看不见、到红区才发作」的洞 —— 各配一对正/反测试。

两条都是 2026-08-18 夜跑 Phase 0 之后实测抓到的真洞，不是假想：

1. **ASCII locale**：红区批处理上下文里 ``LANG`` 常是 ``C``，``sys.stdout`` 就是纯 ASCII。
   本工具的输出带中文 ⇒ 一个 ``print`` 就 ``UnicodeEncodeError``、进程退 1。
   于是「开发机全绿」的闸门在红区必红，而那是最没法调试的地方。
2. **redzone_scan 的文件集**：裸 ``git ls-files`` 只列已跟踪文件，刚写出来还没 ``git add``
   的新代码一个都扫不到 —— 闸门对着一堆新文件报 clean。Phase 0 的 10 个新文件就是这么空过的。

写法遵循 ``docs/OVERNIGHT.md``「防自证 → 配方 2」：每条正向测试都有一条 ``_negative``，
证明**去掉防护就会红**。只有正向测试的话，无法区分「防护起作用」和「根本没这个问题」。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# 一段必然无法编码成 ASCII 的文本。用它当探针，因为工具的真实输出就是这种。
NON_ASCII = "接口自检"


def _run(argv, *, env=None, cwd=None):
    """跑一条命令，返回 (returncode, stdout+stderr)。永不抛。"""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    proc = subprocess.run(
        argv,
        cwd=str(cwd or REPO),
        env=full_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180,
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


class AsciiLocale(unittest.TestCase):
    """自检入口必须在 ASCII-only 的 stdout 下照常退 0。"""

    ENV = {"PYTHONIOENCODING": "ascii"}

    def test_selftest_survives_ascii_stdout(self):
        rc, out = _run(
            [sys.executable, "-m", "ewave_batch", "dry-run", "--self-test"],
            env=self.ENV,
        )
        self.assertEqual(
            rc, 0,
            "ASCII locale 下 self-test 应当照常退 0（中文降级成 ? 即可）。实际输出：\n" + out,
        )
        self.assertNotIn("UnicodeEncodeError", out)

    def test_unguarded_print_dies_on_ascii_stdout_negative(self):
        """反向：**不**调 ascii_safe_stdio 时，同样的输出必须崩。

        这条红了才说明上面那条测的是真东西 —— 否则可能只是这台机器的 locale 本来就宽容。
        """
        rc, out = _run(
            [sys.executable, "-c", "print(%r)" % NON_ASCII],
            env=self.ENV,
        )
        self.assertNotEqual(
            rc, 0,
            "没有防护时 ASCII stdout 打印中文竟然成功了 —— 说明这条探针失效，"
            "上面那条正向测试也就不再有意义。输出：\n" + out,
        )
        self.assertIn("UnicodeEncodeError", out)

    def test_guard_makes_the_same_print_survive(self):
        """同一段输出，加上防护就活。正反两条共用同一个探针字符串，排除「换了个东西测」。"""
        code = (
            "from ewave_batch._stdio import ascii_safe_stdio; "
            "ascii_safe_stdio(); print(%r)" % NON_ASCII
        )
        rc, out = _run([sys.executable, "-c", code], env=self.ENV)
        self.assertEqual(rc, 0, "加了防护仍然崩：\n" + out)
        self.assertNotIn("UnicodeEncodeError", out)


class RedzoneScanFileSet(unittest.TestCase):
    """redzone_scan 必须扫到**未跟踪**文件，否则新代码带坐标也拦不住。

    在一次性的临时 git repo 里验，**不碰本仓库的工作区** —— 夜跑里有并行 agent
    在同一棵树上干活，往仓库里丢诱饵文件会让别人的闸门莫名其妙变红。
    """

    SCAN = str(REPO / "scripts" / "redzone_scan.sh")

    # 诱饵**不**用能命中通用结构规则的路径形状（`/data/…`、`/home/…`）。
    # 理由：那种字符串写进被跟踪的测试文件，闸门会当场命中它自己 —— 而"把字面量拆开
    # 绕过扫描器"是个很坏的先例，将来真泄漏时同一招就能藏住。
    # 改成给临时 repo 注入一份自己的词表，用一个明显假的 token 当诱饵：
    # 既验到了我改的东西（**扫哪些文件**，与命中哪条规则无关），又顺带验了第 2 层词表生效。
    LOCAL_PATTERN = "NOT_A_REAL_TOKEN_[0-9]+"
    BAIT = 'tag = "NOT_A_REAL_TOKEN_42"\n'

    @classmethod
    def setUpClass(cls):
        """平台性 skip（2026-08-19，P6 部署实测发现）。

        这三条验的是**开发侧的提交闸门**，需要两样东西：`git`（要建临时 repo）
        和 `scripts/redzone_scan.sh`。装到红区的那份包里两样都没有 ——
        红区本来就没装 git，而闸门脚本被 `.gitattributes` `export-ignore` 掉了
        （理由见 `deploy/README.md`「为什么 scripts/ 不进包」）。
        而 `bash deploy/doctor.sh --test` 会跑这一整套测试。

        不跳的话，红区会看到三条**与安装质量无关**的红 —— 而「一套全绿的测试」
        正是那台没网的机器上唯一能拿到的最强证据，被这三条毁掉太亏。
        开发机上 git 和脚本都在，所以这条**永远不会**在这里跳过：
        闸门本身的牙一颗没少。
        """
        if shutil.which("git") is None:
            raise unittest.SkipTest("平台性 skip：这台机器没有 git（装好的红区安装目录的正常情况）")
        if not Path(cls.SCAN).exists():
            raise unittest.SkipTest("平台性 skip：这份安装里没有 scripts/redzone_scan.sh（它只在开发机上）")

    def _tmp_repo(self, stack):
        d = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        rc, out = _run(["git", "init", "-q", "."], cwd=d)
        self.assertEqual(rc, 0, "临时 repo 建不起来：" + out)
        (d / ".redzone_patterns.local").write_text(
            "# 一次性词表，只在这个临时 repo 里存在\n" + self.LOCAL_PATTERN + "\n",
            encoding="utf-8",
        )
        return d

    def test_untracked_file_with_coordinates_is_caught(self):
        import contextlib

        with contextlib.ExitStack() as stack:
            d = self._tmp_repo(stack)
            (d / "leaky.py").write_text(self.BAIT, encoding="utf-8")
            # 关键：**不** git add。这正是 Phase 0 那 10 个文件当时的状态。
            rc, out = _run(["sh", self.SCAN], cwd=d)
            self.assertEqual(
                rc, 1,
                "未跟踪文件里的站点坐标没被拦下 —— 闸门对新写的代码是瞎的。输出：\n" + out,
            )
            self.assertIn("leaky.py", out)

    def test_clean_untracked_file_passes_negative(self):
        """反向：同样是未跟踪文件，不含坐标就必须放行。

        少了这条，上面那条可以靠「一律报红」作弊通过 —— 那种闸门等于没有。
        """
        import contextlib

        with contextlib.ExitStack() as stack:
            d = self._tmp_repo(stack)
            # 长得很像诱饵但差一位：词表要的是数字结尾。用它同时排除「规则太宽」。
            (d / "innocent.py").write_text('tag = "NOT_A_REAL_TOKEN_X"\n', encoding="utf-8")
            rc, out = _run(["sh", self.SCAN], cwd=d)
            self.assertEqual(rc, 0, "干净的未跟踪文件被误杀了：\n" + out)
            self.assertIn("redzone_scan: clean", out)
            self.assertNotIn("innocent.py", out)

    def test_scan_script_still_uses_the_wide_file_set(self):
        """守住写法本身：有人改回裸 `git ls-files` 时这条会红。

        上面两条已经验了行为，这条只是把「为什么必须带这三个 flag」钉在代码里，
        防止将来「简化」时静默退回。
        """
        src = (REPO / "scripts" / "redzone_scan.sh").read_text(encoding="utf-8")
        self.assertIn("--cached --others --exclude-standard", src)


if __name__ == "__main__":
    unittest.main()
