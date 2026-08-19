"""部署链路（P6）里**本机能机器判定**的那一部分。

本机没有 Linux、没有红区、跑不了 `deploy.sh`。所以这份测试**不假装**测部署，
它只测三样能在 Windows 上判死的东西 —— 而这三样恰好是 SNP 那边真正踩出血的三样：

1. **行尾**：`git archive` 出来的 `.sh` 逐字节零 `\r`。
   CRLF 会让红区 bash 死在 `bash: $'\r': command not found`，
   而红区是最没法调试的地方（PROJECT_BRIEF §12 硬规矩 2）。
2. **包的内容边界**：该进的进了、不该进的一个都没进（黑名单是否真的生效）。
3. **tier 判定**：`deploy/_env_check.py` 的 `decide_tier` 是纯函数，
   拿手写的真值表逐格对；配 `_negative` 证明**拿掉一个依赖会掉档**，
   而不是照报满分（`docs/OVERNIGHT.md` 防自证配方 3）。

外加 `bash -n` 的语法自检 —— 一个语法错的 `deploy.sh` 传过去就是砖。

## 为什么档案取自「工作树 tree」而不是 `HEAD`

`git archive HEAD` 只看得见**已提交**的 blob。但本项目的规矩是
「agent 不 commit，审查放行后统一 commit」⇒ 引入一个 CRLF 文件的那一次跑，
恰恰是 `HEAD` 里还没有它的那一次 —— 闸门会在**唯一重要的时刻**优雅跳过。
所以这里用临时 index（`GIT_INDEX_FILE` + `git add -A` + `git write-tree`）
造一棵包含工作树的 tree 再 archive：判据完全相同（checkin 转换 + `.gitattributes`
的 `export-ignore` 都照常生效），但它在**文件刚写出来**的那一刻就有牙。
临时 index 不碰仓库的真 index，也不产生 commit。
"""

from __future__ import annotations

import ast
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENV_CHECK_PATH = REPO / "deploy" / "_env_check.py"
DEPLOY_SH = REPO / "deploy.sh"
DOCTOR_SH = REPO / "deploy" / "doctor.sh"
PACK_PS1 = REPO / "deploy" / "pack.ps1"

# 红区 bash 真正会执行的两条。它们是「必须是 LF」的硬对象，其余 `.sh` 顺带一起验。
CRITICAL_SHELL_SCRIPTS = ("deploy.sh", "deploy/doctor.sh")


# --------------------------------------------------------------------------
# 档案（跑一次，全文件共用）
# --------------------------------------------------------------------------


_ARCHIVE_CACHE: dict[str, bytes] | None = None
_ARCHIVE_ERROR: str = ""


def _git(args: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    full = dict(os.environ)
    if env:
        full.update(env)
    return subprocess.run(
        ["git"] + args,
        cwd=str(REPO),
        env=full,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
    )


def _build_archive() -> dict[str, bytes]:
    """工作树 → `git archive` 的成员表 {包内路径: 原始字节}。

    走临时 index，所以**没提交的文件也算数**（见模块 docstring）。
    """
    global _ARCHIVE_CACHE, _ARCHIVE_ERROR
    if _ARCHIVE_CACHE is not None or _ARCHIVE_ERROR:
        return _ARCHIVE_CACHE or {}

    tmpdir = tempfile.mkdtemp(prefix="ewb_pack_")
    try:
        index = os.path.join(tmpdir, "index")
        env = {"GIT_INDEX_FILE": index}
        added = _git(["add", "-A", "--", "."], env=env)
        if added.returncode != 0:
            _ARCHIVE_ERROR = "git add 失败: " + added.stderr.decode("utf-8", "replace")[-400:]
            return {}
        written = _git(["write-tree"], env=env)
        if written.returncode != 0:
            _ARCHIVE_ERROR = "git write-tree 失败: " + written.stderr.decode("utf-8", "replace")[-400:]
            return {}
        tree = written.stdout.decode("ascii").strip()
        packed = _git(["archive", "--format=tar", tree])
        if packed.returncode != 0:
            _ARCHIVE_ERROR = "git archive 失败: " + packed.stderr.decode("utf-8", "replace")[-400:]
            return {}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    members: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(packed.stdout), mode="r:") as tar:
        for info in tar.getmembers():
            if not info.isfile():
                continue
            handle = tar.extractfile(info)
            members[info.name] = handle.read() if handle else b""
    _ARCHIVE_CACHE = members
    return members


def archive_or_skip(case: unittest.TestCase) -> dict[str, bytes]:
    if shutil.which("git") is None:
        case.skipTest("平台性 skip：本机没有 git，无法构造 git archive 的判据")
    # ★ 这份测试会随包被装到红区（`doctor.sh --test` 会跑它），而**安装目录不是
    #   git 仓库**。那边没有源可以打包，也就没有「包对不对」这个问题 —— 跳过，
    #   别把一次正常的红区自检染红。开发机上这条永远不会跳。
    if _git(["rev-parse", "--is-inside-work-tree"]).returncode != 0:
        case.skipTest("平台性 skip：这里不是 git 工作树（装好的红区安装目录的正常情况）")
    members = _build_archive()
    if _ARCHIVE_ERROR:
        case.fail("无法构造档案（这本身就是打包会失败的信号）：" + _ARCHIVE_ERROR)
    return members


def count_cr(data: bytes) -> int:
    """字节里 `\\r` 出现的次数。判据就是它 —— 不是「看起来像 LF」。"""
    return data.count(b"\r")


# --------------------------------------------------------------------------
# 1. 行尾
# --------------------------------------------------------------------------


class ShellScriptsAreLf(unittest.TestCase):
    """PROJECT_BRIEF §12 硬规矩 2：包里的 `.sh` 必须逐字节零 CR。"""

    def test_shipped_shell_scripts_have_zero_cr(self):
        members = archive_or_skip(self)
        scripts = {name: blob for name, blob in members.items() if name.endswith(".sh")}

        # ★ 防「空得非常好看」：零个 `.sh` 的 CR 总数也是 0。先证集合非空，
        #   而且红区 bash 真正会执行的那两条确实在里面。
        for name in CRITICAL_SHELL_SCRIPTS:
            self.assertIn(name, scripts, f"{name} 不在包里 —— 红区将无从更新/自检")
        self.assertGreaterEqual(len(scripts), len(CRITICAL_SHELL_SCRIPTS))
        self.assertGreater(sum(len(b) for b in scripts.values()), 0, "扫过的字节数为 0")

        for name, blob in sorted(scripts.items()):
            self.assertEqual(
                count_cr(blob),
                0,
                f"{name} 在包里带 CR ⇒ 红区 bash 会死在 $'\\r'。"
                f"修法：确认 .gitattributes 的 `*.sh text eol=lf` 已提交，"
                f"再 `git add --renormalize {name}`",
            )

    def test_shipped_shell_scripts_have_zero_cr_negative(self):
        """同一条构造路径，只往取回来的字节里塞一个 CR ⇒ 判据必须报出来。

        没有这一条就分不清「行尾防护起作用」和「压根没扫到文件」。
        """
        members = archive_or_skip(self)
        blob = members["deploy.sh"]
        self.assertEqual(count_cr(blob), 0)

        poisoned = blob.replace(b"\n", b"\r\n", 1)  # 只毒化第一行
        self.assertEqual(count_cr(poisoned), 1, "判据没认出被塞进去的那个 CR")
        self.assertNotEqual(count_cr(poisoned), count_cr(blob))


# --------------------------------------------------------------------------
# 2. 包的内容边界
# --------------------------------------------------------------------------


# 红区必须拿到的东西（PROJECT_BRIEF §12「红区布局」逐条抄下来的）
MUST_SHIP = (
    "deploy.sh",
    "deploy/doctor.sh",
    "deploy/_env_check.py",
    "deploy/README.md",
    "cli.py",
    "VERSION",
    "ewave_batch/model.py",
    "ewave_batch/cli.py",
    "ewave_batch/redzone_dryrun.py",
    "gui/app.py",
    "docs/INTERFACES.md",
    "docs/REDZONE_DRYRUN.md",
    "tests/test_deploy.py",
)

# 过了气隙没意义、或者根本不许出气隙的东西。
# 前四类是开发侧；`PROJECT_BRIEF.md` / `ENVIRONMENT.local.md` / `references/probes/`
# 是红区资料（在 .gitignore 里 ⇒ 本来就进不了 git，这里再拦一道）。
MUST_NOT_SHIP_PREFIXES = (
    ".gitattributes",
    ".gitignore",
    "CLAUDE.md",
    "deploy/pack.ps1",
    "docs/OVERNIGHT.md",
    "mockups/",
    "mvp/",
    "scripts/check.sh",
    "scripts/redzone_scan.sh",
    "scripts/redzone_crosscheck.sh",
    "scripts/install_hooks.sh",
    "PROJECT_BRIEF.md",
    "ENVIRONMENT.local.md",
    ".redzone_patterns.local",
    "references/probes/",
    "references/ewave_donau_kit/",
)


class ArchiveContents(unittest.TestCase):
    """黑名单是否真的生效 —— 两个方向都验。"""

    def test_package_ships_what_the_red_zone_needs(self):
        members = archive_or_skip(self)
        missing = [p for p in MUST_SHIP if p not in members]
        self.assertEqual(missing, [], f"这些东西没进包，红区会缺件：{missing}")
        # 计数断言：清单本身不许被悄悄改短
        self.assertEqual(len(MUST_SHIP), 13)

    def test_package_omits_dev_only_and_red_zone_material(self):
        members = archive_or_skip(self)
        leaked = [
            name
            for name in members
            for prefix in MUST_NOT_SHIP_PREFIXES
            if name == prefix or name.startswith(prefix)
        ]
        self.assertEqual(leaked, [], f"这些东西不该进包：{leaked}")

        # ★ 防「空得非常好看」：上面那条对一个空仓库也是绿的。
        #   所以再断言「本机确实存在、且确实被排除掉了」的条目数 > 0 ——
        #   排除规则要么真的在干活，要么这条会红。
        really_excluded = [
            prefix
            for prefix in MUST_NOT_SHIP_PREFIXES
            if (REPO / prefix.rstrip("/")).exists()
        ]
        self.assertGreater(
            len(really_excluded),
            5,
            "本机上几乎没有一个黑名单条目真实存在 ⇒ 这条测试没有证明力",
        )
        for prefix in really_excluded:
            self.assertNotIn(prefix.rstrip("/"), members)

    def test_no_local_fixture_crosses_the_gap(self):
        """`tests/fixtures/*.local.*` 是从真实生产命令抽出来的 golden，含站点坐标。

        它在 `.gitignore` 里 ⇒ 本来就进不了 git，这里当第二道闸门。
        （本机确实有那份文件时这条才有证明力，所以顺带断言它存在。）
        """
        members = archive_or_skip(self)
        offenders = [n for n in members if ".local." in Path(n).name]
        self.assertEqual(offenders, [], f"红区 fixture 进包了：{offenders}")

        local_golden = REPO / "tests" / "fixtures" / "production_cmd.local.json"
        if not local_golden.exists():
            self.skipTest("平台性 skip：本机没有 golden fixture（公开克隆者的正常情况）")
        self.assertNotIn("tests/fixtures/production_cmd.local.json", members)


class SentinelAgreement(unittest.TestCase):
    """`deploy.sh` 和 `pack.ps1` 的 sentinel 必须是同一个、且真的在包里。

    打错一个字符的后果：每一次 `bash deploy.sh` 都报「这不是一个安装目录」，
    或者更糟 —— 换装完的完整性检查失败、触发回滚，而回滚是那条最不该被触发的路径。
    """

    @staticmethod
    def _sentinel_from(path: Path, marker: str) -> str:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(marker):
                return stripped.split("=", 1)[1].strip().strip("'\"")
        raise AssertionError(f"{path.name} 里找不到 {marker}")

    def setUp(self):
        # pack.ps1 是开发侧文件（export-ignore），装到红区的那份包里没有它。
        if not PACK_PS1.exists():
            self.skipTest("平台性 skip：这份安装里没有 deploy/pack.ps1（它只在开发机上）")

    def test_both_sides_agree_and_the_file_ships(self):
        from_sh = self._sentinel_from(DEPLOY_SH, "SENTINEL=")
        from_ps = self._sentinel_from(PACK_PS1, "$Sentinel")
        self.assertEqual(from_sh, from_ps, "两边的 sentinel 不一致 ⇒ 打得出包却装不上")

        members = archive_or_skip(self)
        self.assertIn(from_sh, members, f"sentinel {from_sh} 不在包里 ⇒ 每次部署都会被判成坏包")

    def test_both_sides_agree_negative(self):
        """同一条读取路径，把其中一边换成打错字的版本 ⇒ 比较逻辑必须报不一致。"""
        from_sh = self._sentinel_from(DEPLOY_SH, "SENTINEL=")
        typo = from_sh.replace("model", "modle")
        self.assertNotEqual(typo, from_sh)
        members = archive_or_skip(self)
        self.assertNotIn(typo, members, "打错字的 sentinel 居然存在，这条测试选错了探针")


class DeployScriptHardRules(unittest.TestCase):
    """§12 硬规矩 4/6 的回归守卫 —— 这两条被简化过的话，代价是整个安装。"""

    @staticmethod
    def _code_lines(path: Path) -> list[str]:
        """去掉整行注释后的正文（注释里本来就写着 "no /tmp"，不能拿来当命中）。"""
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            out.append(line)
        return out

    def test_nothing_is_written_outside_the_install_dir(self):
        for path in (DEPLOY_SH, DOCTOR_SH):
            body = "\n".join(self._code_lines(path))
            for forbidden in ("mktemp", "/tmp/", "/var/", "/opt/"):
                self.assertNotIn(
                    forbidden,
                    body,
                    f"{path.name} 往安装目录外面写（{forbidden}）—— §12 硬规矩 4",
                )
            self.assertIn(".deploy", body, f"{path.name} 没有把暂存放进 .deploy/")

    def test_rollback_distinguishes_backup_phase_from_install_phase(self):
        body = DEPLOY_SH.read_text(encoding="utf-8")
        self.assertIn('PHASE="backup"', body)
        self.assertIn('PHASE="install"', body)
        # 回滚里必须有「只有 install 阶段才敢清 TARGET」那个判断
        self.assertIn('if [[ "$PHASE" == "install" ]]', body)
        self.assertIn("KEEP_BACKUPS=3", body)


# --------------------------------------------------------------------------
# 3. tier 判定
# --------------------------------------------------------------------------


def _load_env_check():
    spec = importlib.util.spec_from_file_location("_ewb_env_check_probe", ENV_CHECK_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ENV_CHECK = _load_env_check()

# 四个工具都在（值随便给，`decide_tier` 只看真假）
ALL_TOOLS = {"dsub": "/x/dsub", "djob": "/x/djob", "ewave": "/x/ewave", "strmout": "/x/strmout"}
NO_TOOLS: dict[str, str] = {}

# ★ 手写的真值表。期望值不是算出来的，是从 PROJECT_BRIEF §12「doctor 的三个 tier」
#   那张表 + 「tier 3 缺失是降级不是失败」那句话逐格读出来的。
#   列：(py_ok, core_ok, tools, tk_ok, display_ok) -> tier
TIER_TABLE = (
    # 解释器太老 ⇒ 什么都别谈
    ((False, True, ALL_TOOLS, True, True), 0, "python 太老"),
    # 包 import 不起来 ⇒ 包没完整落地
    ((True, False, ALL_TOOLS, True, True), 0, "包 import 失败"),
    # 纯净登录 shell：只有 python
    ((True, True, NO_TOOLS, False, False), 1, "只有 python"),
    # 有 GUI 但没加载集群模块 ⇒ 仍然只有 tier 1（三档是累加的）
    ((True, True, NO_TOOLS, True, True), 1, "有 tkinter 但没工具"),
    # 少一个 djob 也是 tier 1：能提交不能轮询，run 会永远卡在 pending
    ((True, True, {"dsub": "/x", "ewave": "/x", "strmout": "/x"}, True, True), 1, "缺 djob"),
    # 工具齐了，没有 tkinter
    ((True, True, ALL_TOOLS, False, False), 2, "无 tkinter"),
    # 有 tkinter 但开不了窗（纯 ssh 会话的常态）⇒ 降级不是失败
    ((True, True, ALL_TOOLS, True, False), 2, "无 $DISPLAY"),
    # 全齐
    ((True, True, ALL_TOOLS, True, True), 3, "全齐"),
)


class TierDecision(unittest.TestCase):
    def test_tier_table(self):
        seen = []
        for facts, expected, why in TIER_TABLE:
            got = ENV_CHECK.decide_tier(*facts)
            seen.append(got)
            self.assertEqual(got, expected, f"{why}：期望 tier {expected}，实得 {got}")

        # 计数断言：三个 tier 外加「不可用」都被真正走到过。
        # 少了这一条，一张只覆盖 tier 1 的表也会全绿。
        self.assertEqual(sorted(set(seen)), [0, 1, 2, 3], "真值表没有覆盖到全部四种结论")
        self.assertEqual(len(TIER_TABLE), 8)

    def test_tier_table_negative_dropping_one_dependency_drops_the_tier(self):
        """同一条构造路径（从 tier 3 那格出发），每次只拿掉一样 ⇒ 必须掉档。

        反过来说：如果 `decide_tier` 写成 `return 3`，上面的正向表会有 7 格红，
        但真正危险的是写成「只要 py_ok 就报满分」那种 —— 这条专抓它。
        """
        base_facts, base_tier, _ = TIER_TABLE[-1]
        self.assertEqual(ENV_CHECK.decide_tier(*base_facts), 3)

        py_ok, core_ok, tools, tk_ok, display_ok = base_facts

        # 拿掉 dsub
        without_dsub = {k: v for k, v in tools.items() if k != "dsub"}
        self.assertEqual(
            ENV_CHECK.decide_tier(py_ok, core_ok, without_dsub, tk_ok, display_ok),
            1,
            "没有 dsub 还报 tier 2/3",
        )
        # 拿掉 djob（brief 的表里没点名它，但没有它 driver 永远收不到终态）
        without_djob = {k: v for k, v in tools.items() if k != "djob"}
        self.assertEqual(
            ENV_CHECK.decide_tier(py_ok, core_ok, without_djob, tk_ok, display_ok), 1
        )
        # 拿掉 tkinter
        self.assertEqual(
            ENV_CHECK.decide_tier(py_ok, core_ok, tools, False, False), 2, "没有 tkinter 还报 tier 3"
        )
        # 有 tkinter 但开不了窗
        self.assertEqual(ENV_CHECK.decide_tier(py_ok, core_ok, tools, True, False), 2)
        # 包 import 不起来
        self.assertEqual(ENV_CHECK.decide_tier(py_ok, False, tools, tk_ok, display_ok), 0)

        self.assertEqual(base_tier, 3)

    def test_missing_tools_reports_exactly_the_absent_ones(self):
        self.assertEqual(ENV_CHECK.missing_tools(ALL_TOOLS), [])
        self.assertEqual(
            ENV_CHECK.missing_tools({"dsub": "/x", "strmout": "/x"}), ["djob", "ewave"]
        )
        # 空串 / None 都算「没有」，别把 which 返回的空串当命中
        self.assertEqual(
            ENV_CHECK.missing_tools({"dsub": "", "djob": None, "ewave": "/x", "strmout": "/x"}),
            ["dsub", "djob"],
        )
        # 计数断言：四个工具一个不少地被检查过
        self.assertEqual(len(ENV_CHECK.missing_tools({})), 4)
        self.assertEqual(len(ENV_CHECK.TOOLS_FOR_SUBMIT), 4)

    def test_tier_blocker_names_what_is_missing(self):
        facts = (True, True, {"dsub": "/x", "djob": "/x", "ewave": "/x"}, True, True)
        tier = ENV_CHECK.decide_tier(*facts)
        self.assertEqual(tier, 1)
        why = ENV_CHECK.tier_blocker(tier, *facts)
        self.assertIn("strmout", why, "停在 tier 1 却没说是谁缺席")
        self.assertNotIn("dsub", why, "把在场的工具也报成缺席")

        # tier 2 卡在 X11 上时要说人话，别让干净的 ssh 会话看着像坏了
        facts2 = (True, True, ALL_TOOLS, True, False)
        self.assertEqual(ENV_CHECK.decide_tier(*facts2), 2)
        self.assertIn("DISPLAY", ENV_CHECK.tier_blocker(2, *facts2))

        # 顶档没有 blocker
        self.assertEqual(ENV_CHECK.tier_blocker(3, True, True, ALL_TOOLS, True, True), "")


class EnvCheckSyntaxDiscipline(unittest.TestCase):
    """`_env_check.py` 是**探针**：它必须能被一台太老的解释器 parse 出来。

    否则「这台机器的 python 太旧」会表现成一个 SyntaxError —— 看上去像包坏了，
    而那正是它本来要替你排除的可能性。
    """

    def test_no_fstrings_and_no_annotations(self):
        tree = ast.parse(ENV_CHECK_PATH.read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                offenders.append(("f-string", node.lineno))
            elif isinstance(node, ast.AnnAssign):
                offenders.append(("变量注解", node.lineno))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.returns is not None:
                    offenders.append(("返回注解", node.lineno))
                for arg in list(node.args.args) + list(node.args.kwonlyargs):
                    if arg.annotation is not None:
                        offenders.append(("参数注解", node.lineno))
        self.assertEqual(offenders, [], f"探针用了新语法：{offenders}")

    def test_min_py_matches_what_the_package_actually_needs(self):
        """`MIN_PY` 不许比真实下限松。

        证据：`ewave_batch/model.py` 里 `FlagValue = str | bool` 是**运行时**求值的
        PEP-604 union（不是注解，`from __future__ import annotations` 管不着它）
        ⇒ 3.10 起才 import 得动。部署目标是 3.11.4。
        """
        self.assertGreaterEqual(ENV_CHECK.MIN_PY, (3, 10))
        model_src = (REPO / "ewave_batch" / "model.py").read_text(encoding="utf-8")
        self.assertIn(
            "FlagValue = str | bool",
            model_src,
            "证据消失了：model.py 不再有那个运行时 union ⇒ 重新核对 MIN_PY 的下限",
        )

    def test_probe_output_is_pure_ascii(self):
        """红区 `LANG` 常是 C —— 探针的输出里出现一个非 ASCII 字节就会退 1。"""
        raw = ENV_CHECK_PATH.read_bytes()
        try:
            raw.decode("ascii")
        except UnicodeDecodeError as exc:  # pragma: no cover - 只在写错时走到
            self.fail(f"_env_check.py 里有非 ASCII 字节：{exc}")


class EnvCheckEndToEnd(unittest.TestCase):
    """把探针当子进程真跑一遍 —— 证明它在这台机器上确实能产出可解析的结论。"""

    # `core.discover.find_tool` 的兜底口子。跑测试的 shell 里可能本来就设了它们
    # （check.sh 的第 2/3 条命令就故意设 `EWAVE_ABS` / `STRMOUT_BIN`）⇒ 不先擦掉的话
    # 下面那对正/反测试的结论会取决于**谁在跑它**，而不是取决于被测逻辑。
    TOOL_ENV_KEYS = tuple(
        f"{tool.upper()}{suffix}"
        for tool in ("dsub", "djob", "ewave", "strmout")
        for suffix in ("_BIN", "_ABS")
    )

    @classmethod
    def _run(cls, extra_env: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        for key in cls.TOOL_ENV_KEYS:
            env.pop(key, None)
        env["PYTHONIOENCODING"] = "ascii"  # 模拟红区 LANG=C
        if extra_env:
            env.update(extra_env)
        proc = subprocess.run(
            [sys.executable, str(ENV_CHECK_PATH)],
            cwd=str(REPO),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
        )
        text = proc.stdout.decode("ascii")
        facts = {}
        for line in text.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                facts.setdefault(key, value)
        facts["_rc"] = str(proc.returncode)
        facts["_stderr"] = proc.stderr.decode("utf-8", "replace")[-400:]
        return facts

    def test_probe_runs_under_ascii_locale_and_imports_the_package(self):
        facts = self._run()
        self.assertEqual(facts["_rc"], "0", facts.get("_stderr", ""))
        self.assertEqual(facts["PY_OK"], "YES")
        self.assertEqual(facts["IMP_core_cmd"], "OK", facts.get("IMP_core_cmd_detail"))
        self.assertEqual(facts["IMP_cli"], "OK", facts.get("IMP_cli_detail"))
        self.assertEqual(facts["IMP_redzone_dryrun"], "OK", facts.get("IMP_redzone_dryrun_detail"))
        # gui.app 必须在**不碰 tkinter** 的前提下 import 得动（CLAUDE.md 硬约束 5）
        self.assertEqual(facts["IMP_gui_app"], "OK", facts.get("IMP_gui_app_detail"))
        self.assertIn(facts["TIER"], {"1", "2", "3"})

    def test_tools_come_from_the_tools_own_lookup(self):
        """注入 `<NAME>_BIN` ⇒ 四个工具都该被认出来，tier 升到 ≥ 2。

        走的是 `core.discover.find_tool` 自己的兜底口子（PATH → `_BIN`/`_ABS`），
        所以这条同时证明了 doctor 报的是「工具自己会找到的东西」。
        """
        facts = self._run(
            {
                "DSUB_BIN": "/fake/dsub",
                "DJOB_BIN": "/fake/djob",
                "EWAVE_BIN": "/fake/ewave",
                "STRMOUT_BIN": "/fake/strmout",
            }
        )
        self.assertEqual(facts["_rc"], "0", facts.get("_stderr", ""))
        for tool in ("dsub", "djob", "ewave", "strmout"):
            self.assertEqual(facts["TOOL_" + tool], "OK", f"{tool} 没被环境变量兜底认出来")
        self.assertGreaterEqual(int(facts["TIER"]), 2)

    def test_tools_come_from_the_tools_own_lookup_negative(self):
        """同一条构造路径，不注入 ⇒ 必须报缺席、且 tier 掉回 1。

        本机没有 dsub/ewave/strmout（CLAUDE.md 硬约束 3），所以这条在本机成立；
        万一哪台机器真有，就跳过并说明原因 —— 那时它证明不了任何东西。
        """
        on_path = [t for t in ("dsub", "djob", "ewave", "strmout") if shutil.which(t)]
        if on_path:
            self.skipTest(f"平台性 skip：这台机器 PATH 上真有 {on_path}，反向判据不成立")
        facts = self._run()
        for tool in ("dsub", "djob", "ewave", "strmout"):
            self.assertEqual(facts["TOOL_" + tool], "MISSING")
        self.assertEqual(facts["TIER"], "1", "工具全缺却没掉回 tier 1")
        self.assertIn("dsub", facts["TIER_WHY"])


# --------------------------------------------------------------------------
# 4. shell 语法自检
# --------------------------------------------------------------------------


class ShellSyntax(unittest.TestCase):
    """一个语法错的 `deploy.sh` 传过去就是一块砖 —— 本机有 Git Bash，先 parse 一遍。"""

    def _bash(self, *args: str) -> subprocess.CompletedProcess:
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("平台性 skip：本机没有 bash（Git Bash 未安装）")
        return subprocess.run(
            [bash] + list(args),
            cwd=str(REPO),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
        )

    def test_deploy_sh_parses(self):
        proc = self._bash("-n", "deploy.sh")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))

    def test_doctor_sh_parses(self):
        proc = self._bash("-n", "deploy/doctor.sh")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))

    def test_both_declare_bash_not_sh(self):
        """两条都用了 bash-ism（数组、`[[ ]]`、`shopt`）⇒ shebang 必须是 bash。

        红区登录 shell 是 tcsh/csh，脚本靠 `bash deploy.sh` 起；shebang 写错时
        `./deploy.sh` 会被 dash 之类接走，报一堆无从查起的语法错。
        """
        for path in (DEPLOY_SH, DOCTOR_SH):
            first = path.read_text(encoding="utf-8").splitlines()[0]
            self.assertIn("bash", first, f"{path.name} 的 shebang 不是 bash：{first}")

    def test_deploy_refuses_a_directory_that_is_not_an_install(self):
        """sentinel 不在 ⇒ 一个字节都不许动，先报错退出。

        这是 SNP 那套里最便宜也最值钱的一条护栏：把 `deploy.sh` copy 到别处随手一跑，
        它绝不能开始「备份 + 换装」那段非原子的窗口。
        """
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("平台性 skip：本机没有 bash（Git Bash 未安装）")
        with tempfile.TemporaryDirectory(prefix="ewb_deploy_") as tmp:
            box = Path(tmp)
            shutil.copy2(DEPLOY_SH, box / "deploy.sh")
            proc = subprocess.run(
                [bash, "deploy.sh"],
                cwd=str(box),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=180,
            )
            out = proc.stdout.decode("utf-8", "replace")
            self.assertNotEqual(proc.returncode, 0, "不是安装目录却照跑：\n" + out)
            self.assertIn("not an Ewave_helper install", out)
            self.assertFalse((box / ".deploy").exists(), "拒绝之前就已经在动目录了")

    def test_deploy_refuses_when_no_package_is_present_negative(self):
        """反向：同一条构造路径，把 sentinel 补上 ⇒ 换一个理由拒绝（没有包），

        而**不是**开始换装。正反两条一起证明「拒绝」是分情况的、不是一律报错。
        """
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("平台性 skip：本机没有 bash（Git Bash 未安装）")
        sentinel = SentinelAgreement._sentinel_from(DEPLOY_SH, "SENTINEL=")
        with tempfile.TemporaryDirectory(prefix="ewb_deploy_") as tmp:
            box = Path(tmp)
            shutil.copy2(DEPLOY_SH, box / "deploy.sh")
            (box / sentinel).parent.mkdir(parents=True, exist_ok=True)
            (box / sentinel).write_text("# stand-in\n", encoding="utf-8")
            proc = subprocess.run(
                [bash, "deploy.sh"],
                cwd=str(box),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=180,
            )
            out = proc.stdout.decode("utf-8", "replace")
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("no *.tar.gz found", out)
            self.assertNotIn("not an Ewave_helper install", out, "sentinel 补上了却还在报同一个错")
            self.assertFalse((box / ".deploy").exists(), "拒绝之前就已经在动目录了")

    def test_doctor_help_runs_without_touching_the_install(self):
        """`--help` 要在建 scratch 目录之前就退出（它是最常被随手敲的一条）。

        只盯 `.deploy/tmp/` 这一个点：`.deploy/` 本身可能被同一台机器上别的东西
        （例如 `scripts/redzone_bundle.sh` 的日志目录）先建出来，拿它当判据会误报。
        """
        scratch = REPO / ".deploy" / "tmp"
        before = scratch.exists()
        proc = self._bash("deploy/doctor.sh", "--help")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        out = proc.stdout.decode("utf-8", "replace")
        self.assertIn("tier 1", out)
        self.assertIn("tier 3", out)
        self.assertEqual(
            scratch.exists(),
            before,
            "--help 就已经建出了 .deploy/tmp/ —— 它该在那之前退出",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
