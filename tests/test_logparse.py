"""`core.logparse`：eWave 日志 → `model.LogFacts`。

## 期望值从哪来（防自证第 2 条）

本机没有真实的 `ewave.log` / `emsolver.log`（`references/probes/` 里只有 help dump、
`gdsout_setup`、workdir tree、step4 verify 输出 —— **没有任何一份完整日志**）。
所以 fixture 是**手写的合成日志**：

* **行格式**照抄 `PROJECT_BRIEF.md` §10 / `mvp/redzone/*.sh` 里逐字引用过的真实行；
* **每一个数值都是编的**，且刻意与红区真实值不同（端口 4 而不是 17、墙钟 111 s 而不是
  峰值用编的而不是真值…）。从证据里抄值正是 P3 被打回的那件事。

⇒ 下面每条断言的期望值都是**手写字面量**，注释写明「格式出处 = BRIEF 的哪一句」。
完整的出处表在 `tests/fixtures/ewave_log_synthetic/README.md`。

## 计数断言（防自证第 4 条）

`_populated()` 数出「解析器给出了值的字段」，每份 fixture 都断言它**逐字段等于**
一份手写的集合。空集合的 diff 永远是绿的 —— 少认一个字段、或者整个解析器返回
`LogFacts()` 空壳，都会当场红。

## 反向验证（防自证第 3 条）

每条关键断言配一条 `_negative`：拿**同一份 fixture 文本**改坏一个值，
断言解析结果跟着变。证明解析器真在解析，不是返回常量。

## 过滤器测试（防自证第 4 条）

`logparse` 里有四处「排除 / 前缀区分」，每处都有回归测试：
`strip_ansi` 只吃转义、`[error]` 不吃 "0 error"、`peak memory` 不吃
`expected memory`、`not converged` 不被当成收敛。
前两条不是假想的：`mvp/redzone/step2_memestimate.sh` 的 grep 后面挂着
`grep -viE 'Invalid Via|0 error'`，那是真实日志里存在这类行的直接证据。

本文件零站点标识符（硬约束 1b）：pin 名 / 路径 / cell 名全部是合成的。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from ewave_batch.core import logparse
from ewave_batch.model import LogFacts

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "ewave_log_synthetic"

SUCCESS_EWAVE = FIXTURES / "success" / "ewave.log"
SUCCESS_EMSOLVER = FIXTURES / "success" / "emsolver.log"
CRASH_EWAVE = FIXTURES / "crash" / "ewave.log"
CRASH_EMSOLVER = FIXTURES / "crash" / "emsolver.log"
CRASH_ALL_DONE = FIXTURES / "crash" / "ewave_says_all_done.log"
ANSI_EWAVE = FIXTURES / "ansi" / "ewave.log"
MEMEST_EWAVE = FIXTURES / "memestimate" / "ewave.log"
SNP_DIR = FIXTURES / "snp"

# 用户以后从红区抄回真实日志时往这儿放。
# ⚠️ 目录名末尾的 `.d` 不是装饰：`.gitignore` 第 7 行是 `tests/fixtures/*.local.*`，
#    要有 `local` 后面那个点才命中。写成 `…_real.local/` 的话，目录本身**不被排除**，
#    里面的 `emsolver.log` 就会被 git 收进去 —— 那是一份真实红区日志。
#    （`ewave.log` 恰好被另一条通用规则挡住，所以这个洞会假装不存在。）
REAL_RUN_DIR = ROOT / "tests" / "fixtures" / "ewave_run_real.local.d"
REAL_SNP_GLOB = "ports_real.local.s*p"

REAL_SKIP = (
    "本机没有 tests/fixtures/ewave_run_real.local.d/ —— 那是从红区抄回来的真实 run 日志，"
    "含站点坐标所以不进 git。没有它时这条只能跳过（公开克隆者与夜跑机器上看到这条 skip 是正常的）。"
    "验法：把一个真实 run 目录整个拷成那个名字，重跑本文件即可。"
)

# 本文件真正依赖的 fixture。**必须逐个进 git** —— 见 FixtureAreTrackableTests。
FIXTURE_FILES = (
    SUCCESS_EWAVE,
    SUCCESS_EMSOLVER,
    CRASH_EWAVE,
    CRASH_EMSOLVER,
    CRASH_ALL_DONE,
    ANSI_EWAVE,
    MEMEST_EWAVE,
    SNP_DIR / "ports.s4p",
    SNP_DIR / "ports_shuffled.s4p",
    SNP_DIR / "ports_missing.s4p",
    SNP_DIR / "no_ports.s4p",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# `LogFacts` 的全部字段 —— 手写，不用 dataclasses.fields() 现算。
# 现算的话，冻结面加了字段本测试会**静默**跟着变，而"跟着变"正是漂移。
ALL_FIELDS = (
    "ok",
    "converged",
    "wall_seconds",
    "peak_memory_mb",
    "port_count",
    "freq_points_calculated",
    "freq_points_requested",
    "cpu_percent_avg",
    "ewave_version",
    "errors",
    "warnings",
    "source_files",
)


def _populated(facts: LogFacts) -> set[str]:
    """解析器**给出了值**的字段名集合。

    `None` = 没测到（`LogFacts` 的 docstring：别用 0 冒充），空串 / 空元组同理。
    ⚠️ 用 `is not None` 而不是真值判断 —— `ok=False` 和 `wall_seconds=0.0` 都是"测到了"。
    """
    out: set[str] = set()
    for name in ALL_FIELDS:
        value = getattr(facts, name)
        if value is None:
            continue
        if isinstance(value, (str, tuple)) and len(value) == 0:
            continue
        out.add(name)
    return out


class FixtureAreTrackableTests(unittest.TestCase):
    """本文件依赖的 fixture **必须能进 git**。

    这条不是形式主义：`.gitignore` 的「运行产物」段里有 `ewave.log` 和 `*.s[0-9]p`，
    它们会把这批 fixture 一个不剩地挡在 git 外面。开发机上文件在磁盘上、测试全绿；
    别人克隆下来少 11 个文件，测试当场红 —— **而且没人会想到去查 .gitignore**。
    （2026-08-19 实测踩到过，修法是给这个目录加一条 `!` 放行。）
    """

    def test_every_fixture_exists_on_disk(self) -> None:
        for path in FIXTURE_FILES:
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file(), f"fixture 不见了: {path}")
        self.assertEqual(len(FIXTURE_FILES), 11)  # 计数断言：清单别悄悄缩水

    def test_no_fixture_is_gitignored(self) -> None:
        import shutil
        import subprocess

        if shutil.which("git") is None or not (ROOT / ".git").exists():
            self.skipTest("本机没有 git 或这不是 git 仓库 —— 无从查 .gitignore（部署包里正常）")
        ignored: list[str] = []
        for path in FIXTURE_FILES + (FIXTURES / "README.md",):
            relative = path.relative_to(ROOT).as_posix()
            done = subprocess.run(
                ["git", "check-ignore", "-q", "--", relative],
                cwd=str(ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if done.returncode == 0:  # 0 = 被忽略
                ignored.append(relative)
        self.assertEqual(
            ignored,
            [],
            "这些 fixture 被 .gitignore 挡住了，提交后别人克隆下来就少文件："
            + "、".join(ignored)
            + "。修法：在 .gitignore 末尾给 tests/fixtures/ewave_log_synthetic/ 加 `!` 放行，"
            "**不要**把它们改名绕开 —— parse_run_logs 认的就是 ewave.log 这个名字。",
        )

    def test_the_real_log_home_is_still_gitignored_negative(self) -> None:
        """反向：真实日志的落点必须**仍然**被挡住，否则上一条的放行开得太大了。"""
        import shutil
        import subprocess

        if shutil.which("git") is None or not (ROOT / ".git").exists():
            self.skipTest("本机没有 git 或这不是 git 仓库 —— 无从查 .gitignore（部署包里正常）")
        for relative in (
            # 真实 run 日志的落点（目录名末尾的 .d 是承重的，见 REAL_RUN_DIR 的注释）
            "tests/fixtures/ewave_run_real.local.d/emsolver.log",
            "tests/fixtures/ewave_run_real.local.d/ewave.log",
            "tests/fixtures/ports_real.local.sNp",
            # 有人图省事把红区日志塞进已放行的合成目录
            "tests/fixtures/ewave_log_synthetic/leaked.local.log",
            "tests/fixtures/ewave_log_synthetic/real.local.d/emsolver.log",
        ):
            with self.subTest(path=relative):
                done = subprocess.run(
                    ["git", "check-ignore", "-q", "--", relative],
                    cwd=str(ROOT),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                self.assertEqual(
                    done.returncode,
                    0,
                    f"{relative} **没有**被 .gitignore 挡住 —— 那是红区日志的落点，"
                    "放进去就会被提交出去（CLAUDE.md 硬约束 1）。",
                )


class LogFactsFieldSetTests(unittest.TestCase):
    """`ALL_FIELDS` 必须与冻结面一致 —— 否则上面的计数断言会漏字段而没人发现。"""

    def test_all_fields_matches_frozen_dataclass(self) -> None:
        import dataclasses

        actual = tuple(f.name for f in dataclasses.fields(LogFacts))
        self.assertEqual(
            actual,
            ALL_FIELDS,
            "model.LogFacts 的字段变了。本文件的计数断言是手写的，"
            "**故意**不跟着自动变 —— 请人工核对新字段该不该被解析，再更新 ALL_FIELDS。",
        )


# --------------------------------------------------------------------------
# strip_ansi —— 过滤器测试（防自证第 4 条）
# --------------------------------------------------------------------------


class StripAnsiTests(unittest.TestCase):
    """`strip_ansi` **只**去掉 ANSI 转义。

    这是承重的一条：日志里遍地 `[info]` / `All Ports size is 4:` / `111 s`，
    要是顺手吃掉 `[` 或数字，后面每条正则都跟着错，**而且错得很安静**
    （字段变 None，看起来像"日志里没有这行"）。
    """

    def test_removes_color_codes(self) -> None:
        # 形状出处：生产命令行末尾恒接 `| sed -r 's/\x1B[[0-9;]*m//g'`
        # （references/probes/run_ewave_typical_*.sh 逐字）。
        self.assertEqual(logparse.strip_ansi("\x1b[31mred\x1b[0m"), "red")
        self.assertEqual(logparse.strip_ansi("a\x1b[1;33mb\x1b[0mc"), "abc")

    def test_keeps_brackets_and_digits(self) -> None:
        """**没有 ESC 的行必须逐字节原样返回。**"""
        for line in (
            "[info] All Ports size is 4:",  # 方括号 + 数字 + 冒号
            "Port: PIN_A PIN_B PIN_C PIN_D",
            "Wall Clock Time: 111 s",
            "! Port[1] = PIN_A | ref",  # .sNp 注释头，方括号在中间
            "[error] eWave exit failed!",
            "m[0;31m",  # 长得像色码但**没有 ESC** —— 一个字符都不许动
            "]0;title",
            "\\[not ansi]",
        ):
            with self.subTest(line=line):
                self.assertEqual(logparse.strip_ansi(line), line)

    def test_no_escape_returns_identical_object(self) -> None:
        """没有 ESC 时连正则都不跑（快路径）。同时也是"绝不动别的字符"的最强形式。"""
        text = "[info] 0 error, 0 warning\nCalculated on 3 points.\n"
        self.assertIs(logparse.strip_ansi(text), text)

    def test_removes_osc_and_two_char_escapes(self) -> None:
        # OSC（设置标题）后面紧跟一个字面量 `[info]` —— 终止符认错就会把它一起吃掉。
        self.assertEqual(logparse.strip_ansi("\x1b]0;title\x07[info] x"), "[info] x")
        # ESC + 单字节（两字符转义）。
        self.assertEqual(logparse.strip_ansi("done.\x1bM"), "done.")

    def test_ansi_fixture_strips_to_the_success_fixture_byte_for_byte(self) -> None:
        """两份 fixture **各自手写**，所以"剥完相等"不是自证。

        `ansi/ewave.log` 里的转义码是手工插在这些位置的：字中间（`Execute em<ESC>[0mesh`）、
        数字中间（`Wall Clock Time: 1<ESC>[0m11 s`）、`[info]` 紧邻处、OSC、两字符转义。
        任何一处多吃一个字符，本条就红。
        """
        self.assertEqual(logparse.strip_ansi(_read(ANSI_EWAVE)), _read(SUCCESS_EWAVE))

    def test_ansi_fixture_actually_contains_escapes_negative(self) -> None:
        """反向：先证明 fixture 里**真有** ESC，否则上一条就是拿两份相同文本互比。"""
        raw = _read(ANSI_EWAVE)
        self.assertIn("\x1b", raw)
        self.assertGreaterEqual(raw.count("\x1b"), 10)
        self.assertNotEqual(raw, _read(SUCCESS_EWAVE))

    def test_parse_is_immune_to_color_codes(self) -> None:
        """走完整条解析链再比一次 —— 剥色码这件事对上层是透明的。"""
        self.assertEqual(
            logparse.parse_ewave_log(_read(ANSI_EWAVE)),
            logparse.parse_ewave_log(_read(SUCCESS_EWAVE)),
        )


# --------------------------------------------------------------------------
# parse_ewave_log —— 关键测试 + 计数断言
# --------------------------------------------------------------------------


class ParseEwaveLogTests(unittest.TestCase):
    """成功那份日志。**期望值全是手写字面量**，逐条注明格式出处。"""

    def setUp(self) -> None:
        self.text = _read(SUCCESS_EWAVE)
        self.facts = logparse.parse_ewave_log(self.text)

    def test_field_count_matches_what_the_fixture_carries(self) -> None:
        """计数断言：解析出来的字段集合 == 我写进 fixture 的那 6 个。

        少认一个（正则写错）→ 红；多认一个（拿别的行硬凑）→ 也红。
        专防"空得非常好看"：解析器整个返回 `LogFacts()` 时这条第一个炸。
        """
        expected = {
            "ok",  # Execute {emesh,eresist,emsolver} done. 三行齐 + 零失败线索
            "port_count",  # [info] All Ports size is 4:
            "freq_points_calculated",  # Calculated on 3 points.
            "freq_points_requested",  # Sweep on 21 points.（格式是猜的，见 README）
            "wall_seconds",  # Wall Clock Time: 111 s（格式是猜的）
            "ewave_version",  # eWave 9999.99.sp9（格式是猜的）
        }
        self.assertEqual(_populated(self.facts), expected)
        self.assertEqual(len(_populated(self.facts)), 6)

    def test_freq_points_calculated(self) -> None:
        # 期望值 3 = 我写进 fixture 的数（真实值是 19，故意不用）。
        # 格式出处 = BRIEF §10 D13 那一行逐字引用的 `Calculated on <N> points.`
        #            与 C 的运行数据 `Calculated on 1 points.`。
        self.assertEqual(self.facts.freq_points_calculated, 3)
        self.assertIsInstance(self.facts.freq_points_calculated, int)

    def test_freq_points_calculated_negative(self) -> None:
        """同一份输入，只把 fixture 里那个 3 改成 8 → 解析结果必须跟着变成 8。

        证明它真在解析这一行，而不是返回一个常量（或返回 fixture 名对应的硬编码）。
        """
        mutated = self.text.replace("Calculated on 3 points.", "Calculated on 8 points.")
        self.assertNotEqual(mutated, self.text)  # 先证明真的改动了
        self.assertEqual(logparse.parse_ewave_log(mutated).freq_points_calculated, 8)

    def test_port_count(self) -> None:
        # 期望值 4 = fixture 里 `All Ports size is 4:` 和 `Port:` 那行的 4 个名字。
        # 格式出处 = mvp/redzone/step2_memestimate.sh 的注释「红区 step0 实测到的格式」。
        self.assertEqual(self.facts.port_count, 4)

    def test_port_count_negative(self) -> None:
        """把两处 4 都改成 6 → 端口数跟着变，且不该有"自相矛盾"的 warning。"""
        mutated = self.text.replace(
            "All Ports size is 4:", "All Ports size is 6:"
        ).replace("Port: PIN_A PIN_B PIN_C PIN_D", "Port: PIN_A PIN_B PIN_C PIN_D PIN_E PIN_F")
        facts = logparse.parse_ewave_log(mutated)
        self.assertEqual(facts.port_count, 6)
        self.assertEqual(facts.warnings, ())

    def test_port_count_mismatch_is_reported_negative(self) -> None:
        """只改一处 → 两处对不上，必须留 warning（端口数错位是静默的，见 BRIEF §5）。"""
        mutated = self.text.replace("All Ports size is 4:", "All Ports size is 6:")
        facts = logparse.parse_ewave_log(mutated)
        self.assertEqual(facts.port_count, 6)  # 显式那个数说了算
        self.assertEqual(len(facts.warnings), 1)
        self.assertIn("自相矛盾", facts.warnings[0])

    def test_wall_seconds(self) -> None:
        # 期望值 111.0 = fixture 里 `Wall Clock Time: 111 s`（真实值另有其数，故意不用）。
        # ⚠️ 格式**未经真实日志验证**：关键词来自 mvp/redzone/diag_ab.sh 的 grep，
        #    但没有任何粘回来的输出确认过冒号后面长什么样。
        self.assertEqual(self.facts.wall_seconds, 111.0)

    def test_wall_seconds_negative(self) -> None:
        mutated = self.text.replace("Wall Clock Time: 111 s", "Wall Clock Time: 222 s")
        self.assertEqual(logparse.parse_ewave_log(mutated).wall_seconds, 222.0)

    def test_wall_seconds_units(self) -> None:
        """单位换算与 `H:MM:SS` 写法。两种都是猜的形状，所以两种都试。"""
        base = "Wall Clock Time: 111 s"
        for line, want in (
            ("Wall Clock Time: 2 min", 120.0),
            ("Wall Clock Time: 0.5 h", 1800.0),
            ("Wall Clock Time: 00:01:51", 111.0),
            ("Wall Clock Time: 111", 111.0),  # 无单位按秒
        ):
            with self.subTest(line=line):
                mutated = self.text.replace(base, line)
                self.assertEqual(logparse.parse_ewave_log(mutated).wall_seconds, want)

    def test_ewave_version(self) -> None:
        # 期望值是**合成的**版本串（真实版本串不进 git，硬约束 1b）。
        self.assertEqual(self.facts.ewave_version, "9999.99.sp9")

    def test_ok_is_true_but_only_means_the_log_did_not_confess(self) -> None:
        """`ok=True` 只表示"日志没自曝失败"。判 run 成败的是 `layout.verify_run_outputs`。"""
        self.assertIs(self.facts.ok, True)

    def test_unparsed_fields_stay_none_not_zero(self) -> None:
        """没测到就留 `None`（`LogFacts` docstring：0 秒和"没测到"是两回事）。"""
        self.assertIsNone(self.facts.converged)
        self.assertIsNone(self.facts.peak_memory_mb)
        self.assertIsNone(self.facts.cpu_percent_avg)

    def test_unknown_lines_are_ignored(self) -> None:
        """fixture 里故意混了两行无关内容，解析结果不受影响。"""
        self.assertIn("[info] reading layout from synthetic.gds", self.text)
        stripped = "\n".join(
            line
            for line in self.text.splitlines()
            if "reading layout" not in line and "Invalid Via" not in line
        )
        self.assertEqual(logparse.parse_ewave_log(stripped + "\n"), self.facts)

    def test_empty_text_gives_all_none(self) -> None:
        self.assertEqual(_populated(logparse.parse_ewave_log("")), set())


class DoneIsNotSuccessTests(unittest.TestCase):
    """**本模块存在意义的判据**：日志说 done 不等于成功（BRIEF §10 实测：崩了也打 done）。

    与 `core.layout.verify_run_outputs` 的契约一致 —— 那边判 `done` 靠
    「存在 + 非空 + 端口数对」，不靠日志措辞、不靠退出码。这边只负责一件事：
    **日志里有失败线索时，绝不把它报成成功。**
    """

    def test_crash_log_is_not_ok(self) -> None:
        facts = logparse.parse_ewave_log(_read(CRASH_EWAVE))
        self.assertIs(facts.ok, False)

    def test_crash_log_still_contains_a_done_line(self) -> None:
        """先证明 fixture 里**确实**写着 done —— 否则上一条测的就不是这件事。"""
        # 逐字出处 = BRIEF §10 根因链：「但它照样打印 "Execute eresist done."（写失败被吞掉）」
        self.assertIn("Execute eresist done.", _read(CRASH_EWAVE))

    def test_all_three_done_markers_still_lose_to_a_crash_fingerprint(self) -> None:
        """最狠的一份：三个 `Execute … done.` 全打了，同时有 boost 崩溃指纹。

        `ok` 必须是 `False`。这条要是绿不了，整个模块没有存在价值。
        """
        text = _read(CRASH_ALL_DONE)
        for phase in logparse.EWAVE_PHASES:
            self.assertIn(f"Execute {phase} done.", text)
        self.assertIs(logparse.parse_ewave_log(text).ok, False)

    def test_removing_the_crash_lines_flips_it_back_negative(self) -> None:
        """反向：把崩溃三行删掉（其余一字不改）→ 同一份输入变成 `ok=True`。

        证明 `False` 是那三行**导致**的，不是这份 fixture 被硬编码成失败。
        """
        text = _read(CRASH_ALL_DONE)
        clean = "\n".join(
            line
            for line in text.splitlines()
            if "terminate called" not in line
            and "what():" not in line
            and "[error]" not in line
        )
        self.assertIs(logparse.parse_ewave_log(clean + "\n").ok, True)
        self.assertIs(logparse.parse_ewave_log(text).ok, False)

    def test_crash_errors_are_captured_verbatim(self) -> None:
        """三行崩溃现场都要留给人看。出处 = BRIEF §10 step3 那个代码块的三行。"""
        facts = logparse.parse_ewave_log(_read(CRASH_EWAVE))
        self.assertEqual(len(facts.errors), 3)  # 计数断言：fixture 里就是 3 行
        joined = "\n".join(facts.errors)
        self.assertIn("boost::archive::archive_exception", joined)
        self.assertIn("input stream error", joined)
        self.assertIn("eWave exit failed", joined)

    def test_crash_field_count(self) -> None:
        """计数断言：崩溃那份能抽出来的就这 4 项（跑到一半就断了，没有墙钟/频点）。"""
        facts = logparse.parse_ewave_log(_read(CRASH_EWAVE))
        self.assertEqual(_populated(facts), {"ok", "port_count", "ewave_version", "errors"})

    def test_quota_fingerprint_is_a_failure(self) -> None:
        """配额爆了才是 §10 那次事故的真凶，而它当时一行错都没报。

        直接证据是 `cp: failed to close …: Disk quota exceeded` ——
        只要这句出现在日志里就判失败。
        """
        text = "Execute emesh done.\nExecute eresist done.\nExecute emsolver done.\n"
        self.assertIs(logparse.parse_ewave_log(text).ok, True)
        quota = text + "cp: failed to close 'x.gds': Disk quota exceeded\n"
        self.assertIs(logparse.parse_ewave_log(quota).ok, False)

    def test_partial_done_is_none_not_false(self) -> None:
        """只打了一部分 done、又没有失败线索 → `None`（"日志没说"），**不是** False。"""
        text = "Execute emesh done.\nExecute eresist done.\n"
        self.assertIsNone(logparse.parse_ewave_log(text).ok)


# --------------------------------------------------------------------------
# parse_emsolver_log
# --------------------------------------------------------------------------


class ParseEmsolverLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _read(SUCCESS_EMSOLVER)
        self.facts = logparse.parse_emsolver_log(self.text)

    def test_field_count_matches_what_the_fixture_carries(self) -> None:
        expected = {
            "converged",  # [info] iterative solver converged in 7 steps（格式是猜的）
            "peak_memory_mb",  # peak memory: 7.5 GB（格式是猜的）
            "cpu_percent_avg",  # average cpu usage: 250%（格式是猜的）
            "port_count",  # [info] All Ports size is 4:
            "freq_points_calculated",  # Calculated on 3 points.
            "wall_seconds",  # Wall Clock Time: 96 s（格式是猜的）
        }
        self.assertEqual(_populated(self.facts), expected)
        self.assertEqual(len(_populated(self.facts)), 6)

    def test_peak_memory_mb(self) -> None:
        # 期望值 7680.0 MB = fixture 里的 7.5 GB x 1024（二进制换算，见 _MEM_UNIT_MB）。
        # 7.5 是编的（真实峰值 （真值不复述），故意不用）。
        self.assertEqual(self.facts.peak_memory_mb, 7680.0)

    def test_peak_memory_negative(self) -> None:
        mutated = self.text.replace("peak memory: 7.5 GB", "peak memory: 2.5 GB")
        self.assertEqual(logparse.parse_emsolver_log(mutated).peak_memory_mb, 2560.0)

    def test_peak_memory_units(self) -> None:
        base = "peak memory: 7.5 GB"
        for line, want in (
            ("peak memory: 512 MB", 512.0),
            ("peak memory usage: 1 TB", 1024.0 * 1024.0),
            ("Maximum Memory = 2048 MiB", 2048.0),
        ):
            with self.subTest(line=line):
                mutated = self.text.replace(base, line)
                self.assertEqual(logparse.parse_emsolver_log(mutated).peak_memory_mb, want)

    def test_cpu_percent_can_exceed_100(self) -> None:
        """多核时 CPU 占用远大于 100%（BRIEF §10 D13 实测 远大于 100%（多核））。"""
        self.assertEqual(self.facts.cpu_percent_avg, 250.0)
        mutated = self.text.replace("average cpu usage: 250%", "average cpu usage: 1900%")
        self.assertEqual(logparse.parse_emsolver_log(mutated).cpu_percent_avg, 1900.0)

    def test_converged(self) -> None:
        self.assertIs(self.facts.converged, True)

    def test_emsolver_never_claims_the_whole_run_succeeded(self) -> None:
        """即使这份日志里写着 `Execute emsolver done.`，`ok` 也必须是 `None`。

        理由是语义：emsolver 是三个阶段之一，它跑完不等于 run 成了
        （BRIEF §10：`eresist` 打了 done，写出来的却是 0 字节）。
        """
        self.assertIn("Execute emsolver done.", self.text)
        self.assertIsNone(self.facts.ok)

    def test_emsolver_still_reports_failure(self) -> None:
        """失败方向照常 —— 那次事故的现场就在这份日志里。"""
        facts = logparse.parse_emsolver_log(_read(CRASH_EMSOLVER))
        self.assertIs(facts.ok, False)
        self.assertEqual(len(facts.errors), 2)  # 计数断言：fixture 里就是 2 行

    def test_same_scanner_as_ewave_log(self) -> None:
        """两个函数认的是同一套行格式，**只差 `ok` 那一条**：除 `ok` 外逐字段相等。"""
        as_main = logparse.parse_ewave_log(self.text)
        for name in ALL_FIELDS:
            if name == "ok":
                continue
            with self.subTest(field=name):
                self.assertEqual(getattr(self.facts, name), getattr(as_main, name))

    def test_the_only_difference_is_ok_negative(self) -> None:
        """反向：喂一份三个阶段都打了 done 的文本，两个函数的 `ok` 必须分道扬镳。

        用 `success/ewave.log` 而不是 emsolver 那份 —— 后者只打了一个阶段的 done，
        两个函数都会给 `None`，那样就什么都没测到。
        """
        text = _read(SUCCESS_EWAVE)
        self.assertIs(logparse.parse_ewave_log(text).ok, True)
        self.assertIsNone(logparse.parse_emsolver_log(text).ok)


# --------------------------------------------------------------------------
# 过滤器回归（防自证第 4 条）—— 每条都对应一个真实存在的坑
# --------------------------------------------------------------------------


class FilterRegressionTests(unittest.TestCase):
    """「排除 / 前缀区分」的每一处都要证明它**没把不该忽略的一起忽略掉**。

    同型的真 bug 在本项目出过两次（`--sparam` 前缀误伤 `--sparamImpedance`；
    `# HZ` vs GHz 单位）。规律：每加一个自动判据，必须配一条"故意造错必须被抓到"。
    """

    def test_zero_error_line_is_not_an_error(self) -> None:
        """`0 error` 含 error 字样但无害。

        证据：`mvp/redzone/step2_memestimate.sh` 的 grep 后面挂着
        `grep -viE 'Invalid Via|0 error'` —— 那说明真实日志里确实有这类行。
        把它判成失败，成功的 run 会被报成崩溃。
        """
        text = "Execute emesh done.\nExecute eresist done.\nExecute emsolver done.\n[info] 0 error, 0 warning\n"
        facts = logparse.parse_ewave_log(text)
        self.assertIs(facts.ok, True)
        self.assertEqual(facts.errors, ())

    def test_invalid_via_line_is_not_an_error(self) -> None:
        text = "[info] Invalid Via count: 0\nExecute emesh done.\n"
        self.assertEqual(logparse.parse_ewave_log(text).errors, ())

    def test_bracketed_error_tag_is_an_error_negative(self) -> None:
        """反向：**带方括号**的那个必须被抓到，否则上面两条就是"什么都不抓"。"""
        text = "[error] something went wrong\n"
        facts = logparse.parse_ewave_log(text)
        self.assertIs(facts.ok, False)
        self.assertEqual(facts.errors, ("[error] something went wrong",))

    def test_expected_memory_is_not_peak_memory(self) -> None:
        """`expected memory`（估算）绝不许被当成 `peak memory`（实测）。

        BRIEF §10：`--memEstimate` 的估算值与实际峰值是**两个不同的量**（真值不复述）。
        这正是 `--sparam` 吃掉 `--sparamImpedance` 的同型陷阱。
        """
        text = _read(MEMEST_EWAVE)
        self.assertIn("expected memory: 9.25 GB", text)
        self.assertIsNone(logparse.parse_ewave_log(text).peak_memory_mb)

    def test_expected_memory_is_still_reachable_negative(self) -> None:
        """反向：那个值没被丢掉，只是走另一个出口。"""
        # 期望值 9472.0 MB = fixture 里的 9.25 GB x 1024（9.25 是编的，真实值另有其数）。
        self.assertEqual(logparse.parse_memory_estimate_mb(_read(MEMEST_EWAVE)), 9472.0)

    def test_peak_memory_line_is_still_matched_negative(self) -> None:
        """反向：同一份日志里换成 peak 措辞就必须认得出来。"""
        text = _read(MEMEST_EWAVE).replace("expected memory: 9.25 GB", "peak memory: 9.25 GB")
        self.assertEqual(logparse.parse_ewave_log(text).peak_memory_mb, 9472.0)
        self.assertIsNone(logparse.parse_memory_estimate_mb(text))

    def test_memestimate_run_is_not_reported_as_ok(self) -> None:
        """`--memEstimate` 是半程 run（只有 emesh）—— `ok` 必须是 `None`，不是 True 也不是 False。"""
        facts = logparse.parse_ewave_log(_read(MEMEST_EWAVE))
        self.assertIsNone(facts.ok)
        self.assertEqual(
            _populated(facts), {"port_count", "ewave_version", "wall_seconds"}
        )

    def test_not_converged_is_not_converged(self) -> None:
        """否定式必须先判 —— 反过来的话 `not converged` 里的 `converged` 会赢，
        而那个方向的错是"把失败报成成功"。"""
        for line in (
            "solver not converged after 200 steps",
            "iterative solver did not converge",
            "failed to converge",
            "convergence failed",
            "solution diverged",
        ):
            with self.subTest(line=line):
                self.assertIs(logparse.parse_emsolver_log(line + "\n").converged, False)

    def test_converged_is_converged_negative(self) -> None:
        """反向：肯定式还认得出来，否则上一条就是"一律判没收敛"。"""
        self.assertIs(
            logparse.parse_emsolver_log("iterative solver converged in 7 steps\n").converged, True
        )

    def test_a_later_converged_does_not_undo_an_earlier_failure(self) -> None:
        text = "outer loop not converged\ninner loop converged\n"
        self.assertIs(logparse.parse_emsolver_log(text).converged, False)

    def test_port_line_must_be_anchored(self) -> None:
        """`Port:` 锚在行首 —— 不锚的话 `All Ports size is` 自己就会命中它。"""
        text = "[info] All Ports size is 4:\nPort: PIN_A PIN_B PIN_C PIN_D\nGround:\n"
        self.assertEqual(logparse.parse_ewave_log(text).port_count, 4)
        self.assertEqual(logparse.parse_ewave_log(text).warnings, ())

    def test_port_count_from_port_line_alone(self) -> None:
        """只有 `Port:` 那行时也能数出来（`All Ports size is` 被截断的情形）。"""
        self.assertEqual(logparse.parse_ewave_log("Port: A B C\n").port_count, 3)

    def test_ground_line_is_not_counted_as_ports(self) -> None:
        text = "Port: PIN_A PIN_B\nGround: PIN_G PIN_H PIN_I\n"
        self.assertEqual(logparse.parse_ewave_log(text).port_count, 2)

    def test_conflicting_values_in_one_file_are_reported(self) -> None:
        """同一份日志里同一个量出现两个不同的值 → 取最后一个 **并留 warning**（不静默）。"""
        text = "Calculated on 3 points.\nCalculated on 9 points.\n"
        facts = logparse.parse_ewave_log(text)
        self.assertEqual(facts.freq_points_calculated, 9)
        self.assertEqual(len(facts.warnings), 1)
        self.assertIn("真算过的频点数", facts.warnings[0])

    def test_repeated_identical_values_do_not_warn_negative(self) -> None:
        """反向：值相同就不该报 —— 否则上一条只是"重复即告警"，没检查值。"""
        text = "Calculated on 3 points.\nCalculated on 3 points.\n"
        facts = logparse.parse_ewave_log(text)
        self.assertEqual(facts.freq_points_calculated, 3)
        self.assertEqual(facts.warnings, ())


# --------------------------------------------------------------------------
# merge_log_facts
# --------------------------------------------------------------------------


class MergeLogFactsTests(unittest.TestCase):
    def test_first_wins(self) -> None:
        first = LogFacts(wall_seconds=111.0, port_count=4)
        second = LogFacts(wall_seconds=222.0, port_count=6, peak_memory_mb=7680.0)
        merged = logparse.merge_log_facts(first, second)
        self.assertEqual(merged.wall_seconds, 111.0)  # 前面的不被覆盖
        self.assertEqual(merged.port_count, 4)
        self.assertEqual(merged.peak_memory_mb, 7680.0)  # 前面没有的才补

    def test_first_wins_negative(self) -> None:
        """反向：交换顺序 → 结果跟着换。证明"先到先得"真的按顺序，不是恰好取了某一份。"""
        first = LogFacts(wall_seconds=111.0, port_count=4)
        second = LogFacts(wall_seconds=222.0, port_count=6, peak_memory_mb=7680.0)
        merged = logparse.merge_log_facts(second, first)
        self.assertEqual(merged.wall_seconds, 222.0)
        self.assertEqual(merged.port_count, 6)

    def test_false_is_a_value_not_a_hole(self) -> None:
        """`ok=False` / `converged=False` 是"测到了"，后面的 `True` 不许覆盖它。"""
        merged = logparse.merge_log_facts(
            LogFacts(ok=False, converged=False), LogFacts(ok=True, converged=True)
        )
        self.assertIs(merged.ok, False)
        self.assertIs(merged.converged, False)

    def test_zero_is_a_value_not_a_hole(self) -> None:
        merged = logparse.merge_log_facts(LogFacts(wall_seconds=0.0), LogFacts(wall_seconds=9.0))
        self.assertEqual(merged.wall_seconds, 0.0)

    def test_lists_concatenate_in_order_and_dedup(self) -> None:
        merged = logparse.merge_log_facts(
            LogFacts(errors=("a", "b"), source_files=("x",)),
            LogFacts(errors=("b", "c"), source_files=("x", "y")),
        )
        self.assertEqual(merged.errors, ("a", "b", "c"))
        self.assertEqual(merged.source_files, ("x", "y"))

    def test_version_first_non_empty_wins(self) -> None:
        merged = logparse.merge_log_facts(LogFacts(), LogFacts(ewave_version="9999.99.sp9"))
        self.assertEqual(merged.ewave_version, "9999.99.sp9")

    def test_no_arguments_gives_an_empty_shell(self) -> None:
        self.assertEqual(_populated(logparse.merge_log_facts()), set())

    def test_merge_does_not_mutate_its_inputs(self) -> None:
        first = LogFacts(wall_seconds=111.0)
        second = LogFacts(port_count=4)
        logparse.merge_log_facts(first, second)
        self.assertIsNone(first.port_count)
        self.assertIsNone(second.wall_seconds)


# --------------------------------------------------------------------------
# parse_run_logs
# --------------------------------------------------------------------------


class ParseRunLogsTests(unittest.TestCase):
    """读一个 run 目录。**产物真的落在磁盘上**（跟 `sched.fake` 一个道理：
    验收逻辑必须被真实文件验，否则整套测试就是自证）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def _make_run(self, ewave: Path | None, emsolver: Path | None, *, nested: bool = True) -> str:
        """造一个 run 目录。`nested=True` 时日志落在 `<corner>_<temp>/` 那层
        （eWave 自建的那层，BRIEF §7 P4b 实测的真实布局）。"""
        run_dir = os.path.join(self.root, "runA")
        target = os.path.join(run_dir, "typical_-40_0") if nested else run_dir
        os.makedirs(target, exist_ok=True)
        if ewave is not None:
            with open(os.path.join(target, "ewave.log"), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(_read(ewave))
        if emsolver is not None:
            path = os.path.join(target, "emsolver.log")
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(_read(emsolver))
        return run_dir

    def test_merges_both_logs(self) -> None:
        run_dir = self._make_run(SUCCESS_EWAVE, SUCCESS_EMSOLVER)
        facts = logparse.parse_run_logs(run_dir)
        # 计数断言：两份 fixture 合起来能给的字段，一个不多一个不少。
        self.assertEqual(
            _populated(facts),
            {
                "ok",
                "converged",
                "wall_seconds",
                "peak_memory_mb",
                "port_count",
                "freq_points_calculated",
                "freq_points_requested",
                "cpu_percent_avg",
                "ewave_version",
                "source_files",
            },
        )
        self.assertIs(facts.ok, True)
        self.assertEqual(facts.wall_seconds, 111.0)  # ewave.log 先到（96 是 emsolver 的）
        self.assertEqual(facts.peak_memory_mb, 7680.0)  # 只有 emsolver.log 有
        self.assertIs(facts.converged, True)
        self.assertEqual(len(facts.source_files), 2)

    def test_finds_logs_in_the_run_dir_itself_too(self) -> None:
        """调用方传 run 目录还是传 eWave 那层子目录都得认。"""
        run_dir = self._make_run(SUCCESS_EWAVE, SUCCESS_EMSOLVER, nested=False)
        self.assertIs(logparse.parse_run_logs(run_dir).ok, True)

    def test_a_crashed_component_log_vetoes_a_clean_main_log(self) -> None:
        """★ BRIEF §10 的现场：`ewave.log` 写满 done，`emsolver.log` 里躺着 boost 异常。

        「先到先得」会让 `ewave.log` 的 `ok=True` 赢 —— 所以 `parse_run_logs`
        额外做了失败一票否决。这条盯的就是那个否决。
        """
        run_dir = self._make_run(CRASH_ALL_DONE, CRASH_EMSOLVER)
        facts = logparse.parse_run_logs(run_dir)
        self.assertIs(facts.ok, False)

    def test_a_clean_pair_is_still_ok_negative(self) -> None:
        """反向：两份都干净时不许被误否决，否则上一条只是"一律判失败"。"""
        run_dir = self._make_run(SUCCESS_EWAVE, SUCCESS_EMSOLVER)
        self.assertIs(logparse.parse_run_logs(run_dir).ok, True)

    def test_main_log_alone_would_have_said_ok(self) -> None:
        """把 `emsolver.log` 拿掉，同一份 `ewave.log` 就变回 `ok=True` ——
        证明上上条的 `False` 确实来自那份崩溃日志。"""
        run_dir = self._make_run(CRASH_ALL_DONE, None)
        # 这份 fixture 自己带崩溃指纹，所以仍然是 False；换成干净的主日志才会翻绿。
        self.assertIs(logparse.parse_run_logs(run_dir).ok, False)
        run_dir2 = self._make_run(SUCCESS_EWAVE, CRASH_EMSOLVER)
        self.assertIs(logparse.parse_run_logs(run_dir2).ok, False)

    def test_missing_directory_warns_instead_of_raising(self) -> None:
        facts = logparse.parse_run_logs(os.path.join(self.root, "nope"))
        self.assertEqual(_populated(facts), {"warnings"})
        self.assertEqual(len(facts.warnings), 1)
        self.assertIn("不存在", facts.warnings[0])

    def test_empty_directory_warns_instead_of_raising(self) -> None:
        empty = os.path.join(self.root, "empty")
        os.makedirs(empty)
        facts = logparse.parse_run_logs(empty)
        self.assertEqual(_populated(facts), {"warnings"})
        self.assertIn("没有任何 eWave 日志", facts.warnings[0])

    def test_unrelated_logs_are_not_picked_up(self) -> None:
        """`gds_out.log`（阶段 1 的）/ `ewaveOnVir.log`（官方 GUI 的）不该被算进这个 run。"""
        run_dir = os.path.join(self.root, "runB")
        os.makedirs(run_dir)
        for name in ("gds_out.log", "ewaveOnVir.log"):
            with open(os.path.join(run_dir, name), "w", encoding="utf-8", newline="\n") as fh:
                fh.write("Execute emesh done.\nExecute eresist done.\nExecute emsolver done.\n")
        facts = logparse.parse_run_logs(run_dir)
        self.assertEqual(_populated(facts), {"warnings"})

    def test_our_own_run_log_is_picked_up_negative(self) -> None:
        """反向：`run_<stem>.log`（`model.RUN_LOG_TEMPLATE`）必须被认 ——
        崩溃时往往只剩它。"""
        run_dir = os.path.join(self.root, "runC")
        os.makedirs(run_dir)
        with open(
            os.path.join(run_dir, "run_typical_-40_0.log"), "w", encoding="utf-8", newline="\n"
        ) as fh:
            fh.write(_read(CRASH_EWAVE))
        facts = logparse.parse_run_logs(run_dir)
        self.assertIs(facts.ok, False)
        self.assertEqual(len(facts.source_files), 1)

    def test_source_files_are_posix_and_deterministic(self) -> None:
        run_dir = self._make_run(SUCCESS_EWAVE, SUCCESS_EMSOLVER)
        first = logparse.parse_run_logs(run_dir).source_files
        second = logparse.parse_run_logs(run_dir).source_files
        self.assertEqual(first, second)  # 两次跑结果一致（无人值守时"不可复现"= 查不了）
        for path in first:
            self.assertNotIn("\\", path)
        self.assertTrue(first[0].endswith("ewave.log"))  # 优先级：ewave.log 排第一


# --------------------------------------------------------------------------
# parse_port_order
# --------------------------------------------------------------------------


class ParsePortOrderTests(unittest.TestCase):
    """`.sNp` 注释头 → 端口顺序。格式出处 = `references/probes/mvp_step4_verify_*.txt`
    的实测原文 `! Port[N] = <pin名> | ref`（BRIEF §10 step4 判据②，同时答了 P8③）。"""

    def test_reads_the_port_names_in_order(self) -> None:
        # 期望值是**合成的** pin 名（真实 pin 名不进 git，硬约束 1b）。
        self.assertEqual(
            logparse.parse_port_order(str(SNP_DIR / "ports.s4p")),
            ("PIN_A", "PIN_B", "PIN_C", "PIN_D"),
        )

    def test_count_matches_the_fixture(self) -> None:
        """计数断言：解析出的端口数 == fixture 里写的个数。

        右边那个数**独立于被测函数**数出来（直接数文件里的 `! Port[` 行），
        所以它不是"拿解析器的输出当期望值"。
        """
        raw = (SNP_DIR / "ports.s4p").read_text(encoding="utf-8")
        written = raw.count("! Port[")
        self.assertEqual(written, 4)  # 手写字面量再钉一次
        self.assertEqual(len(logparse.parse_port_order(str(SNP_DIR / "ports.s4p"))), written)

    def test_sorts_by_index_not_by_file_order(self) -> None:
        """注释块乱序时按序号排 —— 端口顺序错一位整条 `.sNp` 就全错（BRIEF §5）。"""
        self.assertEqual(
            logparse.parse_port_order(str(SNP_DIR / "ports_shuffled.s4p")),
            ("PIN_A", "PIN_B", "PIN_C", "PIN_D"),
        )

    def test_shuffled_fixture_really_is_shuffled_negative(self) -> None:
        """先证明那份 fixture **真的**是乱序的，否则上一条什么都没测。"""
        lines = [
            line
            for line in (SNP_DIR / "ports_shuffled.s4p").read_text(encoding="utf-8").splitlines()
            if line.startswith("! Port[")
        ]
        self.assertEqual(len(lines), 4)
        self.assertNotEqual(lines, sorted(lines))

    def test_incomplete_header_gives_nothing_negative(self) -> None:
        """4 端口的文件只列了 3 个 → 返回 `()`。**半份端口表比没有更危险。**"""
        path = SNP_DIR / "ports_missing.s4p"
        self.assertEqual(path.read_text(encoding="utf-8").count("! Port["), 3)
        self.assertEqual(logparse.parse_port_order(str(path)), ())

    def test_no_header_gives_nothing(self) -> None:
        """没开 `--includePortOrder=1` 的文件（生产就不开，D1d 说我们要开）。"""
        self.assertEqual(logparse.parse_port_order(str(SNP_DIR / "no_ports.s4p")), ())

    def test_missing_file_gives_nothing(self) -> None:
        self.assertEqual(logparse.parse_port_order(str(SNP_DIR / "nope.s4p")), ())

    def test_duplicate_index_gives_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dup.s2p")
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("! Port[1] = PIN_A | ref\n! Port[1] = PIN_B | ref\n# HZ S RI R 50\n")
            self.assertEqual(logparse.parse_port_order(path), ())

    def test_duplicate_index_that_survives_the_count_checks_gives_nothing(self) -> None:
        """重号 + 条目数和后缀**都对得上**的情形 —— 这才是真正危险的那个。

        `Port[1]` 出现两次、`Port[2]` 一次 ⇒ 去重后 2 个、`.s2p` 也是 2 个、序号也连续，
        条数检查和后缀检查**双双放行**。只有重号这条守卫拦得住它，
        而拦不住的后果是"端口表看起来齐整、其实丢了一个名字"，静默错位。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dup2.s2p")
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(
                    "! Port[1] = PIN_A | ref\n"
                    "! Port[1] = PIN_B | ref\n"
                    "! Port[2] = PIN_C | ref\n"
                    "# HZ S RI R 50\n"
                )
            self.assertEqual(logparse.parse_port_order(path), ())

    def test_clean_two_port_header_still_works_negative(self) -> None:
        """反向：把上面那份的重号去掉 → 必须正常返回，否则上一条只是"一律返回空"。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "clean.s2p")
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("! Port[1] = PIN_B | ref\n! Port[2] = PIN_C | ref\n# HZ S RI R 50\n")
            self.assertEqual(logparse.parse_port_order(path), ("PIN_B", "PIN_C"))

    def test_suffix_mismatch_gives_nothing(self) -> None:
        """后缀说 4 端口、注释头只有 2 个 → 不猜。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mismatch.s4p")
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("! Port[1] = PIN_A | ref\n! Port[2] = PIN_B | ref\n# HZ S RI R 50\n")
            self.assertEqual(logparse.parse_port_order(path), ())

    def test_unknown_suffix_still_works_negative(self) -> None:
        """反向：后缀数不出来时（`.txt`）不做那条交叉检查，照样返回端口表 ——
        证明上一条是"数不上"红的，不是"一律返回空"。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ports.txt")
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("! Port[1] = PIN_A | ref\n! Port[2] = PIN_B | ref\n# HZ S RI R 50\n")
            self.assertEqual(logparse.parse_port_order(path), ("PIN_A", "PIN_B"))

    def test_reference_suffix_is_dropped(self) -> None:
        """`| ref` 是参考端，不是名字的一部分。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "one.s1p")
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("! Port[1] = PIN_A | ref\n")
            self.assertEqual(logparse.parse_port_order(path), ("PIN_A",))


# --------------------------------------------------------------------------
# 真实日志（红区抄回来才有）—— 平台性 skip，带原因
# --------------------------------------------------------------------------


class RealLogFixtureTests(unittest.TestCase):
    """用户从红区抄回真实日志后，把它放进 `tests/fixtures/ewave_log_real.local/` 就能验。

    **这组不断言具体数值** —— 数值是站点信息，写进测试就等于写进 git（硬约束 1b）。
    它验的是「解析器在真实日志上不崩、且**没有空过**」，也就是**验格式，不验值**。
    真实值对不对由跑测试的人看打印出来的摘要。
    """

    def test_parse_run_logs_on_a_real_run_directory(self) -> None:
        if not REAL_RUN_DIR.is_dir():
            self.skipTest(REAL_SKIP)
        facts = logparse.parse_run_logs(str(REAL_RUN_DIR))
        got = _populated(facts)
        print(f"\n[real fixture] {REAL_RUN_DIR} -> 解析到 {sorted(got)}")
        print(f"[real fixture] {facts}")
        self.assertTrue(facts.source_files, "一份日志都没读到 —— 目录里是不是没有 ewave.log？")
        # 空过防线：真日志至少要能给出**两个**事实，否则说明正则集体没对上格式。
        self.assertGreaterEqual(
            len(got - {"source_files", "warnings", "errors"}),
            2,
            "真实日志里一个事实都没抽出来 —— 正则与真实格式对不上，"
            "把这份日志的关键行贴回来，按它改 core/logparse.py 的正则（并更新 fixture README）。",
        )

    def test_parse_port_order_on_a_real_snp(self) -> None:
        candidates = sorted((ROOT / "tests" / "fixtures").glob(REAL_SNP_GLOB))
        if not candidates:
            self.skipTest(
                "本机没有 tests/fixtures/" + REAL_SNP_GLOB + " —— 那是从红区抄回来的真实 .sNp"
                "（只需要注释头），含真实 pin 名所以不进 git。放一份进去即可验证。"
            )
        path = candidates[0]
        order = logparse.parse_port_order(str(path))
        print(f"\n[real fixture] {path.name} -> {len(order)} 个端口")
        expected = logparse.port_count_from_suffix(str(path))
        self.assertTrue(
            order,
            f"{path.name} 的注释头没解析出端口 —— 要么它没开 --includePortOrder=1，"
            "要么 `! Port[N] = <pin> | ref` 的格式变了。",
        )
        self.assertEqual(
            len(order),
            expected,
            f"端口数与后缀 .s{expected}p 对不上 —— 这正是 parse_port_order 会返回 () 的情形，"
            "但它这次返回了非空，说明交叉检查失效了。",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
