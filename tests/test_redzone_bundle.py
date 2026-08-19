"""`scripts/redzone_bundle.sh` —— 红区验证包的契约测试。

这个脚本是**红区用户敲的第一条命令**（`docs/REDZONE_FIRST_RUN.md` 步骤 8），而且它会
随包发过去（`.gitattributes` 只 export-ignore 了四条开发侧脚本，它不在其中）。
所以它坏掉的后果和别的脚本不一样：早上收到的是一块砖，而气隙对面没人能调试。

本文件盯四件事：

1. **语法**（`sh -n` / `bash -n`）—— 一个语法错就是一块砖，而它在本机永远不会被
   「顺手跑一下」发现，因为完整跑一次要一分钟；
2. **退出码契约** —— 脚本抬头声明的那张表，必须和 `docs/REDZONE_FIRST_RUN.md`
   里发布给用户的那张表**逐个相同**。用户按 `echo $status` 决定下一步，两张表分叉
   = 用户按错误的说明书行动；
3. **参数校验的真实行为** —— 没给 OFFDIR / 目录不存在 / 目录里没有 `gdsout_setup`
   这三条，是最可能被踩到的（指错一层是 `docs/REDZONE_DRYRUN.md` 点名的头号错误）；
4. **落点纪律** —— 日志不许住在 `.deploy/tmp/` 下面。这条是 2026-08-19 实测踩出来的
   真 bug 的回归测试：`deploy/doctor.sh` 开头 `rm -rf <install>/.deploy/tmp`、退出时
   trap 里再删一次，而本脚本第 1 步就调 doctor.sh ⇒ 日志会被当场删掉，
   然后 `cat` 报 `No such file or directory`。

**为什么不做端到端**：脚本第 2 步跑的就是 `unittest discover` —— 在测试里跑它
会递归（测试 → 脚本 → 整套测试 → 脚本 …）。所以脚本提供了 `--check-only`：
把「能不能起跑」全部验完就退，不跑单测、不跑 dry-run、不调 doctor.sh。
正反两条关键测试都走这条口子，用的是**同一个输入构造路径**（`_run`）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE = os.path.join(ROOT, "scripts", "redzone_bundle.sh")
DOC = os.path.join(ROOT, "docs", "REDZONE_FIRST_RUN.md")
SYNTHETIC_OFFDIR = os.path.join(ROOT, "tests", "fixtures", "offdir_synthetic")

# 退出码契约。**手写的字面量**，不是从被测脚本里读出来的 —— 这一串就是这个工具
# 对红区用户的承诺，它的出处是 `scripts/redzone_bundle.sh` 抬头「退出码」那一节
# 与 `docs/REDZONE_FIRST_RUN.md` 步骤 8 的表，两份文档由本文件负责钉在一起。
EXPECTED_EXIT_CODES = {
    0: "全绿",
    1: "bundle 自己跑不起来 / 用法错",
    2: "dry-run 比对有差异",
    3: "dry-run 没能比对（没有基准）",
    4: "单测有红",
    5: "环境不满足 tier 1",
    6: "dry-run 跑不起来",
}


def _bash() -> str:
    found = shutil.which("bash")
    if found is None:  # pragma: no cover - 取决于跑测试的机器
        raise unittest.SkipTest("平台性 skip：本机没有 bash（Git Bash 未安装），无法执行 .sh")
    return found


def _run(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    """跑 `bash scripts/redzone_bundle.sh <args>`。**正反两条测试共用这一条路。**

    ⚠️ `encoding="utf-8"` 是承重的，不能写成光秃秃的 `text=True`：那样 subprocess 会用
    **本机 locale** 解码，而脚本里的 `echo` 吐的是原样 UTF-8 字节（shell 不转码）。
    于是在 GBK 的 Windows 上、以及在 `LANG=C` 的红区上，中文断言都会莫名其妙地红 ——
    2026-08-19 本机实测过一次，两条关键测试双双假红。
    """
    return subprocess.run(
        [_bash(), BUNDLE, *args],
        cwd=ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


class Exists(unittest.TestCase):
    def test_bundle_script_ships(self) -> None:
        """脚本本身在，而且**没有被 export-ignore 掉**（红区拿不到它这一步就没了）。"""
        self.assertTrue(os.path.isfile(BUNDLE), f"{BUNDLE} 不存在")
        attrs = os.path.join(ROOT, ".gitattributes")
        if not os.path.isfile(attrs):
            self.skipTest("平台性 skip：这份安装里没有 .gitattributes（红区安装目录的正常情况）")
        with open(attrs, encoding="utf-8") as fh:
            text = fh.read()
        for line in text.splitlines():
            if "export-ignore" in line and line.split()[0] == "scripts/redzone_bundle.sh":
                self.fail(
                    "scripts/redzone_bundle.sh 被 export-ignore 了 ⇒ 它不会进包，"
                    "而 docs/REDZONE_FIRST_RUN.md 步骤 8 让用户敲的就是它。"
                )

    def test_doc_ships(self) -> None:
        self.assertTrue(os.path.isfile(DOC), f"{DOC} 不存在")


class Syntax(unittest.TestCase):
    """一个语法错 = 红区收到一块砖。这两条一秒钟，值得每次都跑。"""

    def test_posix_sh_syntax(self) -> None:
        sh = shutil.which("sh") or shutil.which("dash")
        if sh is None:  # pragma: no cover
            self.skipTest("平台性 skip：本机没有 sh/dash")
        proc = subprocess.run([sh, "-n", BUNDLE], capture_output=True, text=True, errors="replace")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_bash_syntax(self) -> None:
        proc = subprocess.run([_bash(), "-n", BUNDLE], capture_output=True, text=True, errors="replace")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_shebang_is_plain_sh(self) -> None:
        """shebang 是 `#!/bin/sh`：抬头承诺「纯 POSIX sh」，红区不保证有别的。

        （用户仍然按 `bash xxx.sh` 敲 —— 上传通道可能吃掉 exec 位，见 BRIEF §12 规矩 5。
        shebang 写成 sh 是为了让「它到底依赖什么」这个问题有一个诚实的答案。）
        """
        with open(BUNDLE, encoding="utf-8") as fh:
            first = fh.readline().strip()
        self.assertEqual(first, "#!/bin/sh", f"shebang 变了：{first}")


class ExitCodeContract(unittest.TestCase):
    """抬头那张表 == 文档那张表 == 上面手写的那份期望。三方一致才算数。"""

    @staticmethod
    def _codes_in_script() -> set[int]:
        with open(BUNDLE, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        # 抬头里「退出码」那一节：形如 `#   0  全绿 —— …`
        codes: set[int] = set()
        inside = False
        for line in lines:
            if not line.startswith("#"):
                break
            if "退出码（机器可判" in line:
                inside = True
                continue
            if inside and "全部参数" in line:
                break
            if inside:
                m = re.match(r"^#\s{2,}\*{0,2}(\d)\*{0,2}\s{2,}", line)
                if m:
                    codes.add(int(m.group(1)))
        return codes

    @staticmethod
    def _codes_in_doc() -> set[int]:
        with open(DOC, encoding="utf-8") as fh:
            text = fh.read()
        # 步骤 8 的表：形如 `| **0** | 全绿… | … |`
        codes: set[int] = set()
        for m in re.finditer(r"^\|\s*\*{0,2}(\d)\*{0,2}\s*\|", text, re.MULTILINE):
            codes.add(int(m.group(1)))
        return codes

    def test_script_header_lists_every_documented_code(self) -> None:
        found = self._codes_in_script()
        # ★ 计数断言：空集合的比较永远是绿的（防自证配方 4）。
        self.assertEqual(
            len(found),
            len(EXPECTED_EXIT_CODES),
            f"从脚本抬头里只解析出 {len(found)} 个退出码（{sorted(found)}），"
            f"期望 {len(EXPECTED_EXIT_CODES)} 个 —— 要么抬头改了，要么这个解析器该修了。",
        )
        self.assertEqual(found, set(EXPECTED_EXIT_CODES))

    def test_doc_table_lists_every_documented_code(self) -> None:
        found = self._codes_in_doc()
        self.assertEqual(
            len(found),
            len(EXPECTED_EXIT_CODES),
            f"从 docs/REDZONE_FIRST_RUN.md 只解析出 {len(found)} 个退出码（{sorted(found)}），"
            f"期望 {len(EXPECTED_EXIT_CODES)} 个。",
        )
        self.assertEqual(found, set(EXPECTED_EXIT_CODES))

    def test_script_and_doc_agree(self) -> None:
        """用户按文档那张表决定下一步；脚本按抬头那张表退。分叉 = 按错的说明书行动。"""
        self.assertEqual(self._codes_in_script(), self._codes_in_doc())


class ArgumentValidation(unittest.TestCase):
    """最可能被踩到的三条。全部在跑任何重活**之前**返回 ⇒ 每条不到一秒。"""

    def test_no_offdir_exits_1_and_says_how_to_find_it(self) -> None:
        proc = _run()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        out = proc.stdout + proc.stderr
        self.assertIn("gdsout_setup", out, "没告诉用户判据是什么")
        self.assertIn("suggest_official_dirs", out, "没给「让机器找」那条命令")

    def test_missing_dir_exits_1(self) -> None:
        proc = _run(os.path.join(ROOT, "no", "such", "dir"))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)

    def test_unknown_option_exits_1(self) -> None:
        proc = _run(SYNTHETIC_OFFDIR, "--not-a-real-flag")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)


class CheckOnlyGate(unittest.TestCase):
    """★ 关键测试对：同一条入口（`_run` + `--check-only`），只差 OFFDIR 里有没有
    `gdsout_setup` —— 正向必须放行，反向必须拦下并说清往哪挪一层。
    """

    def test_valid_offdir_passes_the_gate(self) -> None:
        """正向：合成 fixture 是一个像样的官方 design 目录 ⇒ 起跑条件满足，退 0。"""
        if not os.path.isfile(os.path.join(SYNTHETIC_OFFDIR, "gdsout_setup")):
            self.skipTest("平台性 skip：本机没有 tests/fixtures/offdir_synthetic/gdsout_setup")
        proc = _run(SYNTHETIC_OFFDIR, "--check-only")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("--check-only", proc.stdout)
        # 三步都必须只是「会跑」，不是「跑了」。
        self.assertIn("这三步都没跑", proc.stdout)

    def test_valid_offdir_passes_the_gate_negative(self) -> None:
        """反向：**同一个目录，只删掉 `gdsout_setup`** ⇒ 必须退 1，
        并且明说是往上挪一层还是往下挪一层（指错一层是头号错误）。

        故意改坏的就是那一个字段 —— 其余文件原样 copy 过来，排除「换了个东西测」。
        """
        if not os.path.isdir(SYNTHETIC_OFFDIR):
            self.skipTest("平台性 skip：本机没有 tests/fixtures/offdir_synthetic")
        with tempfile.TemporaryDirectory() as tmp:
            broken = os.path.join(tmp, "offdir")
            shutil.copytree(SYNTHETIC_OFFDIR, broken)
            os.remove(os.path.join(broken, "gdsout_setup"))
            proc = _run(broken, "--check-only")
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, out)
        self.assertIn("gdsout_setup", out)
        self.assertIn("往下挪一层", out)
        self.assertIn("往上挪一层", out)

    def test_check_only_writes_nothing(self) -> None:
        """`--check-only` 连日志目录都不该建 —— 「它只写这一个地方」这句话越干净越好。"""
        if not os.path.isfile(os.path.join(SYNTHETIC_OFFDIR, "gdsout_setup")):
            self.skipTest("平台性 skip：本机没有 tests/fixtures/offdir_synthetic/gdsout_setup")
        work = os.path.join(ROOT, ".deploy", "redzone_bundle")
        before = sorted(os.listdir(work)) if os.path.isdir(work) else None
        proc = _run(SYNTHETIC_OFFDIR, "--check-only")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        after = sorted(os.listdir(work)) if os.path.isdir(work) else None
        self.assertEqual(before, after, "--check-only 在 .deploy/redzone_bundle/ 下留了东西")


class ReadOnlyDiscipline(unittest.TestCase):
    """脚本对用户的承诺：不写 OFFDIR、不写 spine、不提交任何 job。"""

    @staticmethod
    def _body() -> str:
        """去掉抬头注释之后的脚本正文 —— 承诺写在注释里，检查要看代码。"""
        with open(BUNDLE, encoding="utf-8") as fh:
            return fh.read()

    def test_never_submits_a_job(self) -> None:
        """正文里不许出现执行 dsub/djob/ewave/strmout 的行。

        它们只应该作为**字符串**出现（探针里 `find_tool("ewave")`、提示语里的模块名）。
        判据：任何一行，去掉引号内的内容之后，还剩下这四个词之一 ⇒ 那就是在执行它。
        """
        offenders = []
        for lineno, line in enumerate(self._body().splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # 抹掉所有引号里的东西（含中文提示语和 python 探针里的字面量）
            bare = re.sub(r"\"[^\"]*\"", "", stripped)
            bare = re.sub(r"'[^']*'", "", bare)
            for tool in ("dsub", "djob", "ewave", "strmout"):
                if re.search(r"(^|[;&|(\s])" + tool + r"($|\s)", bare):
                    offenders.append(f"{lineno}: {stripped}")
        self.assertEqual(offenders, [], "这几行看起来在执行提交类工具：\n" + "\n".join(offenders))

    def test_log_dir_is_not_under_deploy_tmp(self) -> None:
        """★ 回归测试（2026-08-19 实测踩过）：日志不许住在 `.deploy/tmp/` 下面。

        `deploy/doctor.sh` 开头 `rm -rf <install>/.deploy/tmp`，退出时 trap 里再删一次。
        本脚本第 1 步就调 doctor.sh ⇒ 日志放那儿会在跑到一半时消失，
        表现是 `cat: .../doctor.log: No such file or directory` 外加见证文件不翼而飞。
        """
        body = self._body()
        # 用 assertTrue 而不是 assertIn：assertIn 失败时会把整个脚本（近 3 万字节）
        # 打进报告，真正的一句话结论就被埋了。
        self.assertTrue(
            ".deploy/redzone_bundle/$STAMP" in body,
            "没找到日志目录的定义（.deploy/redzone_bundle/$STAMP）—— 这条测试的解析假设该更新了",
        )
        self.assertFalse(
            ".deploy/tmp/redzone_bundle" in body,
            "日志目录又回到 .deploy/tmp/ 下面了 —— deploy/doctor.sh 会在第 1 步把它删掉，"
            "表现是跑到一半 cat 报 No such file。",
        )

    def test_doctor_really_deletes_deploy_tmp(self) -> None:
        """上一条的**前提**也机器化：doctor.sh 确实会 `rm -rf .deploy/tmp`。

        前提没了（doctor 改成不删了）不代表上一条该放松 —— 那时这条会先红，
        提醒人回来重新判断，而不是让规则悄悄失去理由。
        """
        doctor = os.path.join(ROOT, "deploy", "doctor.sh")
        if not os.path.isfile(doctor):
            self.skipTest("平台性 skip：这份安装里没有 deploy/doctor.sh")
        with open(doctor, encoding="utf-8") as fh:
            text = fh.read()
        self.assertRegex(
            text,
            r'rm -rf "\$TMP"',
            "doctor.sh 不再删 .deploy/tmp 了 —— 回去重新判断 redzone_bundle 的日志该放哪。",
        )
        self.assertRegex(text, r'TMP="\$ROOT/\.deploy/tmp"', "doctor.sh 的 scratch 路径变了")


class DocumentedCommandsExist(unittest.TestCase):
    """文档里让用户敲的东西必须真的存在 —— 「照着敲」的前提是每条都敲得通。"""

    def test_doc_references_only_shipping_paths(self) -> None:
        with open(DOC, encoding="utf-8") as fh:
            text = fh.read()
        for path in ("scripts/redzone_bundle.sh", "deploy/doctor.sh", "deploy.sh", "cli.py"):
            self.assertIn(path, text, f"文档里没提到 {path}")
            self.assertTrue(
                os.path.exists(os.path.join(ROOT, path)),
                f"文档让用户敲 {path}，但仓库里没有它",
            )

    def test_doc_uses_csh_syntax_not_bash(self) -> None:
        """红区登录 shell 是 csh/tcsh。文档里出现 `export FOO=` 就是让用户敲一条会报错的命令。

        允许出现在「csh 备忘」那张对照表里（那里是**故意**并排展示两种写法）。
        """
        with open(DOC, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        offenders = [
            f"{i}: {line}"
            for i, line in enumerate(lines, start=1)
            if re.search(r"(^|\s)export\s+[A-Z_]+=", line) and not line.lstrip().startswith("|")
        ]
        self.assertEqual(offenders, [], "文档里有 bash 写法的 export（csh 要 setenv）：\n" + "\n".join(offenders))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
