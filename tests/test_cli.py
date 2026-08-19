"""`ewave_batch.cli` + 顶层 `cli` 的测试 —— **P5 的 CLI 判据。**

这份文件要证明五件事，每一件都有计数断言：

1. **五个子命令都在，且都有测试。** `SubcommandCoverage` 拿 `cli.SUBCOMMANDS` 当清单遍历：
   少写一个子命令、写了却没配 golden 测试、写了却没配 `_negative` —— 三种情况都当场红。
2. **每个子命令的关键输出是对的。** 期望值是**手写的字面量**（下面那几张表），
   出处逐条注在旁边（`model.Run.run_id` 的定义 / `model.RunPaths` 的归档布局树 /
   `model.CMD_SH_TEMPLATE`）。**不许拿被测代码算一遍当期望值。**
3. **`dry-run` 全程零写入。** 跑前跑后把整棵目录树快照下来逐条比对；
   `_negative` 用同一套快照去看 `run`，断言它**确实**看得见写入 ——
   空的 diff 永远是绿的，这条专防"比对逻辑其实什么都没比"。
4. **过滤器两个方向都对。** `resume` 只补没成的（没漏补 / 没重跑），
   `archive` 只归档 done 的（没跳过该归的 / 没归不该归的），
   `_summary_line` 只列非零的桶（没多列 / 没吃掉真的有的）。
5. **惰性 import（CLAUDE.md 硬约束 5）。** 在**子进程**里让 `tkinter` 变成不可 import，
   照样跑 `dry-run` / `status` 并退 0；再断言 `import ewave_batch.cli` 之后
   `sys.modules` 里没有 `tkinter`、也没有 `gui.*`。
   这条检测器自己也配了 `_negative`：**故意 import 一下 `gui.app`，断言检测器抓得到** ——
   否则"没检测到泄漏"和"检测器根本不工作"看起来一模一样。

⏱ **全程不 sleep、不起真进程跑 EDA 工具**（本机没有它们，CLAUDE.md 硬约束 3）：
时间线由 `sched.fake.FakeScheduler` 的"第几次 poll"推进，`--poll-interval 0` 让
`run_batch` 一次都不 sleep。唯一起子进程的是 `LazyImport`，它跑的是 `sys.executable`。

🔌 **站点坐标由 `cli.discover_facts` 这个口子注入**（那个函数存在的全部理由）：
本机没有官方 run 目录，而"CLI 把核心件接起来之后行为对不对"与"解析真目录对不对"
是两件事，后者是 `tests/test_discover.py` 的活。测试注入一份**全手写**的 `SiteFacts`，
于是 argv 与端口数在任何机器上都一样 —— 特别地，**跑测试那台机器 PATH 上有没有
`ewave`、有没有设 `EWAVE_BIN` 都不影响结果**（否则本机绿、红区红，而红区才是唯一要紧的地方）。

🚨 本文件零站点标识符：library / cell / view / 端口名 / 路径 / 工具全是显式假值。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from unittest import mock

import cli as root_cli
from ewave_batch import cli
from ewave_batch.core import layout as layout_module
from ewave_batch.core import spec as spec_module
from ewave_batch.model import (
    BATCH_JSON_NAME,
    BatchState,
    PortMode,
    PortSpec,
    Run,
    RunStatus,
    SiteFacts,
)
from ewave_batch.sched.fake import FakeFailureMode, FakeRunner, FakeScheduler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --------------------------------------------------------------------------
# 手写的假值（一个真实取值都没有）
# --------------------------------------------------------------------------

FAKE_LIB = "TESTLIB"
FAKE_CELL = "CELLA"
FAKE_VIEW = "testview"
DESIGN_KEY = "dA"
"""spec 里显式给的 `key:` —— 免得目录名变成 `TESTLIB_CELLA_testview` 那么长，
`run_id` 的每一段都要在下面手写出来。"""

FAKE_EWAVE_BIN = "/tmp/fakebin/ewave"
FAKE_STRMOUT_BIN = "/tmp/fakebin/strmout"
FAKE_LAYER_MAP = "/tmp/fakepdk/layer.map"
FAKE_PTXT_DIR = "/tmp/fakepdk/ptxt"
FAKE_PTXT_TEMPLATE = "tech_{corner}.ptxt"
FAKE_KEY = "FAKEKEY"
FAKE_RESOURCES = "cpu=2;mem=100"

FAKE_PORT_NAMES = ("PIN_A", "PIN_B", "PIN_C", "PIN_D")
"""官方那条命令的端口表（`SiteFacts.official_port_spec`）。driver 拿它的**条数**
当"产物应该是几端口"的期望值。

⚠️ 这个 4 必须等于 `sched.fake.DEFAULT_PORT_COUNT`（`FakeRunner` 默认产 `.s4p`），
否则每个 run 都会因为"端口数不对"失败 —— 那是 `--all` 的代价那道防线在正常工作，
但不是本文件要测的东西。"""

BATCH_NAME = "b1"
CORNERS = ("typical",)
TEMPERATURES = ("-40.0", "125.0")

# --------------------------------------------------------------------------
# ★ 手写的期望表（防自证配方 2：期望值不许由被测代码算出来）
# --------------------------------------------------------------------------

EXPECTED_RUN_IDS: tuple[str, ...] = (
    # run_id = `<design_key>/<axes_slug>/<ewave_dir>`（model.Run.run_id 的定义）。
    #   design_key = spec 里给的 key:            → dA
    #   axes_slug  = base：corner/temperature 两根轴都是 encoded_in_ewave_dir=True，
    #                已经被 eWave 编进 `<corner>_<temp>/` 那层了，不进 slug
    #                （进了目录名里就出现两遍，BRIEF §5）→ model.BASE_SLUG
    #   ewave_dir  = `<corner>_<温度的小数点换下划线>`（model.TEMP_DECIMAL_REPLACEMENT）
    # 顺序：design 在外，轴按 spec 里写的顺序，第一根轴变得最慢（core.matrix.expand_runs）。
    # corner 只有一个取值 ⇒ 只有 temperature 在变。
    "dA/base/typical_-40_0",
    "dA/base/typical_125_0",
)

EXPECTED_TREE: dict[str, str] = {
    # ★ 归档布局，逐条抄自 `model.RunPaths` 的 docstring（= BRIEF §5「归档布局」那棵树）。
    # 键是这份测试自己起的名字，值是**相对 batch_dir** 的路径（`self.rel()` 拼绝对路径）。
    "gds": "gds/dA.gds",
    "gdsout": "gdsout/dA.gdsout_setup",
    "run_dir": "runs/dA/base",
    # cmd.sh 每个 run 一份（model.CMD_SH_TEMPLATE：同一个 run_dir 底下住着 N 个
    # corner/temp 组合，固定名 `cmd.sh` 会让它们互相覆盖）。
    "cmd_sh_0": "runs/dA/base/cmd_typical_-40_0.sh",
    "cmd_sh_1": "runs/dA/base/cmd_typical_125_0.sh",
    "ewave_dir_0": "runs/dA/base/typical_-40_0",
    "ewave_dir_1": "runs/dA/base/typical_125_0",
    # 扁平区：`<design>__<axes-slug>__<corner>_<temp>`，分隔符是 model.AXIS_SLUG_SEP
    # （双下划线 —— 单下划线已经被温度占用）。
    "sparam_0": "sparam/dA__base__typical_-40_0",
    "sparam_1": "sparam/dA__base__typical_125_0",
}

EXPECTED_ARTIFACTS: tuple[str, ...] = (
    # 归档之后 `Run.artifacts` 里那两份（相对 batch_dir）：主参数文件 + eWave 顺带产的
    # `_sample`（求解器真算过的那些频点）。后缀 `.s4p` 的 4 = len(FAKE_PORT_NAMES)。
    "sparam/dA__base__typical_-40_0.s4p;sparam/dA__base__typical_-40_0_sample.s4p",
    "sparam/dA__base__typical_125_0.s4p;sparam/dA__base__typical_125_0_sample.s4p",
)

EXPECTED_JOB_IDS: tuple[str, ...] = ("fake-0001", "fake-0002")
"""`FakeScheduler` 按提交顺序发号（`fake-0001`…，确定性，见它的 docstring）。"""

EXPECTED_WALL_SECONDS = "15.0"
"""`FakeScheduler` 的假时钟每次 submit / poll 走 `seconds_per_poll=15` 秒一格
（默认 `pending_polls=1`、`running_polls=1`）。**与墙钟无关** ——
`test_timestamps_come_from_the_fake_clock` 在 test_driver.py 里盯着这条性质。"""

# 手写的日志内容（塞进第一个 run 的 `<corner>_<temp>/` 里，第二个 run 什么都不放）。
FAKE_EMSOLVER_LOG = "Solution converged after 7 iterations.\npeak memory: 512 MB\n"
EXPECTED_CONVERGED = "yes"
EXPECTED_PEAK_MB = "512.0"

JUNK_FILES: tuple[str, ...] = ("mesh.dat", "resist.rst")
"""归档该删掉的中间件（D5）。名字照 BRIEF §10 实测清单里的形状。"""
JUNK_BYTES = b"1234567"
EXPECTED_FREED_BYTES = len(JUNK_BYTES) * len(JUNK_FILES)  # 7 * 2 = 14


def _p(path: str) -> str:
    """路径归一成 `/`。**本文件自己实现一份**，不借 `cli._posix` ——
    期望值不该由被测模块的函数算出来（哪怕只是个分隔符替换）。"""
    text = str(path).replace("\\", "/")
    while len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    return text


# --------------------------------------------------------------------------
# 构造（正反两向共用这一条路径）
# --------------------------------------------------------------------------


def _facts(official_run_dir: str = "") -> SiteFacts:
    """一份全手写的站点坐标。`SiteFacts` 里装的全是站点身份 ⇒ 这里每个字段都是假值。"""
    return SiteFacts(
        official_run_dir=official_run_dir or "/fake/offdir",
        ewave_bin=FAKE_EWAVE_BIN,
        strmout_bin=FAKE_STRMOUT_BIN,
        layer_map=FAKE_LAYER_MAP,
        dsub_resources=FAKE_RESOURCES,
        ptxt=f"{FAKE_PTXT_DIR}/tech_typical.ptxt",
        ptxt_dir=FAKE_PTXT_DIR,
        ptxt_name_template=FAKE_PTXT_TEMPLATE,
        key=FAKE_KEY,
        official_port_spec=PortSpec(
            mode=PortMode.EXPLICIT,
            mapping=tuple((f"P{i:03d}", name) for i, name in enumerate(FAKE_PORT_NAMES)),
        ),
    )


def _fake_discover(official_run_dir: str, *, env=None) -> SiteFacts:
    """`cli.discover_facts` 的替身。本机没有官方 run 目录（硬约束 3）。"""
    return _facts(official_run_dir)


def _backend_factory(modes, port_counts):
    """`cli._make_backends` 的替身：注入失败模式。scheduler 和 runner **共用同一个**
    `FakeRunner` —— 阶段 1（strmout）走 runner，阶段 2 的产物由 scheduler 在终态那一拍
    让同一个 runner 写出来，两边必须是同一个对象。"""

    def factory(options, contexts):
        runner = FakeRunner(modes=dict(modes or {}), port_counts=dict(port_counts or {}))
        return FakeScheduler(runner), runner

    return factory


@dataclass
class _Result:
    """一次 `cli.main()` 的结果。"""

    code: int
    out: str
    err: str

    @property
    def lines(self) -> list[str]:
        return self.out.splitlines()

    def count(self, pattern: str) -> int:
        """输出里匹配某个正则的行数。**计数断言用的就是它。**"""
        rx = re.compile(pattern)
        return sum(1 for line in self.lines if rx.search(line))


def _invoke(argv, *, modes=None, port_counts=None, entry=cli.main) -> _Result:
    """跑一次 CLI，把 stdout / stderr 都收下来。

    `modes` / `port_counts` 非 None 时才替换 `cli._make_backends` —— 正向 golden 走
    **真实的**后端构造，反向注入失败模式时才替换，两者的其余入参完全相同。
    `test_injected_backends_match_the_real_ones` 断言这两条路给出同样的结果，
    于是正反两条确实是在比同一件事（防自证配方 3：不许"换了个东西测"）。
    """
    out, err = io.StringIO(), io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(cli, "discover_facts", _fake_discover))
        if modes is not None or port_counts is not None:
            stack.enter_context(
                mock.patch.object(cli, "_make_backends", _backend_factory(modes, port_counts))
            )
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(contextlib.redirect_stderr(err))
        code = entry(list(argv))
    return _Result(code=int(code), out=out.getvalue(), err=err.getvalue())


def _spec_dict(
    batch_root: str,
    *,
    temperatures: tuple[str, ...] = TEMPERATURES,
    corners: tuple[str, ...] = CORNERS,
    scheduler: str = "fake",
) -> dict:
    """一份最小 spec。**写成 JSON** —— PyYAML 是惰性依赖，本机不一定装
    （`core.spec.load_spec` 对 `.json` 走 `json` 退路，那正是它存在的理由）。"""
    return {
        "batch_name": BATCH_NAME,
        "batch_root": batch_root,
        "designs": [
            {
                "library": FAKE_LIB,
                "cell": FAKE_CELL,
                "view": FAKE_VIEW,
                "key": DESIGN_KEY,
                "official_run_dir": "/fake/offdir",
            }
        ],
        "axes": {"corner": list(corners), "temperature": list(temperatures)},
        "options": {"scheduler": scheduler, "poll_interval": 0.0},
    }


def _snapshot(root: str) -> dict[str, int]:
    """整棵目录树 → `相对路径 → 大小`。目录记成 `-1`。

    `dry-run` 的"零写入"判据就是它：跑前跑后必须**逐条相等**。
    """
    out: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            full = os.path.join(dirpath, name)
            out[_p(os.path.relpath(full, root))] = -1
        for name in filenames:
            full = os.path.join(dirpath, name)
            out[_p(os.path.relpath(full, root))] = os.path.getsize(full)
    return out


class _CliTest(unittest.TestCase):
    """每个测试一个干净的临时根目录 + 一份写好的 spec。"""

    maxDiff = None

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = _p(self._tmp.name)
        self.batch_root = f"{self.root}/batches"
        self.batch_dir = f"{self.batch_root}/{BATCH_NAME}"

    def write_spec(self, data: dict | None = None, *, name: str = "spec.json") -> str:
        path = os.path.join(self._tmp.name, name)
        payload = _spec_dict(self.batch_root) if data is None else data
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=2))
        return _p(path)

    def rel(self, key: str) -> str:
        """`EXPECTED_TREE` 里的一条 → 绝对路径。"""
        return f"{self.batch_dir}/{EXPECTED_TREE[key]}"

    def assert_lines_present(self, result: _Result, expected: list[str]) -> None:
        """逐条断言这些**手写的行**都出现在输出里，缺哪条就报哪条。"""
        missing = [line for line in expected if line not in result.lines]
        self.assertEqual(missing, [], f"这些手写的关键行没出现：\n{result.out}")


# ==========================================================================
# 1. meta：五个子命令都在，都有测试
# ==========================================================================

SUBCOMMAND_TESTS: dict[str, str] = {
    # 子命令 → 负责它的 golden 测试类。**这张表和 `cli.SUBCOMMANDS` 必须一一对应。**
    "run": "RunGolden",
    "dry-run": "DryRunGolden",
    "resume": "ResumeGolden",
    "archive": "ArchiveGolden",
    "status": "StatusGolden",
}


class SubcommandCoverage(unittest.TestCase):
    """★ 计数断言：`SUBCOMMANDS` 有几个，就必须有几个实现、几条 golden、几条 `_negative`。

    为什么值得单独一条：漏写一个子命令是**静默**的 —— 其余测试照样全绿，
    因为它们根本不知道少了谁。这条把"清单"和"实现/测试"绑在一起，漏一个当场红。
    """

    def test_subcommands_is_exactly_five(self) -> None:
        # 出处：docs/INTERFACES.md「常量：谁负责给出什么」——
        # cli.SUBCOMMANDS = ("run", "dry-run", "resume", "archive", "status")
        self.assertEqual(
            cli.SUBCOMMANDS, ("run", "dry-run", "resume", "archive", "status")
        )
        self.assertEqual(len(cli.SUBCOMMANDS), 5)

    def test_every_subcommand_has_a_handler(self) -> None:
        self.assertEqual(sorted(cli._HANDLERS), sorted(cli.SUBCOMMANDS))
        self.assertEqual(len(cli._HANDLERS), len(cli.SUBCOMMANDS))

    def test_every_subcommand_has_a_parser(self) -> None:
        import argparse

        # `_SubParsersAction` 是 argparse 的私有名字，但它是**唯一**能问出
        # "这个 parser 认哪些子命令"的口子（公开面上没有）。3.8–3.13 一直是这个名字。
        subparsers = [
            action
            for action in cli.build_parser()._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        self.assertEqual(len(subparsers), 1)
        names = sorted(subparsers[0].choices)
        self.assertEqual(names, sorted(cli.SUBCOMMANDS))
        self.assertEqual(len(names), len(cli.SUBCOMMANDS))

    def test_every_subcommand_has_a_golden_and_a_negative_test(self) -> None:
        self.assertEqual(sorted(SUBCOMMAND_TESTS), sorted(cli.SUBCOMMANDS))
        module = sys.modules[__name__]
        for name, class_name in sorted(SUBCOMMAND_TESTS.items()):
            klass = getattr(module, class_name, None)
            self.assertIsNotNone(klass, f"子命令 {name!r} 的测试类 {class_name} 不存在")
            methods = [m for m in dir(klass) if m.startswith("test_")]
            self.assertTrue(methods, f"{class_name} 一条测试都没有（子命令 {name!r}）")
            negatives = [m for m in methods if m.endswith("_negative")]
            self.assertTrue(
                negatives,
                f"{class_name} 没有 _negative 测试（子命令 {name!r}）—— "
                "每条关键测试都要配一条反向验证，否则一条空过的 golden 也是绿的",
            )

    def test_help_documents_every_exit_code(self) -> None:
        # 「退出码语义写进 --help」的机器判据：四个码全都出现在 epilog 的 exit codes 段里。
        block = cli._EPILOG
        for code in (cli.EXIT_OK, cli.EXIT_RUN_FAILED, cli.EXIT_USAGE, cli.EXIT_INTERRUPTED):
            self.assertRegex(block, rf"(?m)^  {code}\b")
        documented = re.findall(r"(?m)^  (\d+)\b", block)
        self.assertEqual(sorted(int(c) for c in documented), [0, 1, 2, 130])
        self.assertEqual(len(documented), 4)

    def test_help_documents_every_exit_code_negative(self) -> None:
        # 反向：把 epilog 里的一个码抠掉，同一段检查必须报出来（否则它什么都没在查）。
        damaged = re.sub(r"(?m)^  130\b.*\n", "", cli._EPILOG)
        documented = re.findall(r"(?m)^  (\d+)\b", damaged)
        self.assertNotEqual(sorted(int(c) for c in documented), [0, 1, 2, 130])
        self.assertEqual(len(documented), 3)


# ==========================================================================
# 2. dry-run
# ==========================================================================


class DryRunGolden(_CliTest):
    """`dry-run`：打印每个 run 的 argv 和落地目录，**一个文件都不写**。"""

    def _dry_run(self, spec_path: str, *extra: str) -> _Result:
        return _invoke(["dry-run", spec_path, *extra])

    def test_dry_run_golden_lines(self) -> None:
        result = self._dry_run(self.write_spec())
        self.assertEqual(result.code, cli.EXIT_OK, result.err)

        # ★ 手写的关键行。抬头那几行的数字来自 spec：1 个 design × 1 corner × 2 温度。
        self.assert_lines_present(
            result,
            [
                "dry run - nothing is written, nothing is submitted",
                f"  batch dir   {self.batch_dir}",
                "  designs     1",
                "  runs        2",
                "stage 1  streamout (one per design, shared by the whole matrix)",
                f"  [1/1] {DESIGN_KEY}",
                "stage 2  solve (one per design x axis combination)",
                f"  [1/2] {EXPECTED_RUN_IDS[0]}",
                f"  [2/2] {EXPECTED_RUN_IDS[1]}",
                "dry-run: 2 runs planned, 2 commands built, 0 files written",
            ],
        )

    def test_dry_run_prints_the_landing_tree(self) -> None:
        result = self._dry_run(self.write_spec())
        # ★ 落地目录：逐条抄自 model.RunPaths 的 docstring（BRIEF §5 归档布局）。
        self.assert_lines_present(
            result,
            [
                f"    work dir  {self.rel('run_dir')}",
                f"    ewave dir {self.rel('ewave_dir_0')}",
                f"    cmd.sh    {self.rel('cmd_sh_0')}",
                f"    sparam    {self.rel('sparam_0')}.sNp",
                f"    ewave dir {self.rel('ewave_dir_1')}",
                f"    cmd.sh    {self.rel('cmd_sh_1')}",
                f"    sparam    {self.rel('sparam_1')}.sNp",
            ],
        )

    def test_dry_run_argv_carries_the_axis_values(self) -> None:
        result = self._dry_run(self.write_spec())
        argv_lines = [ln for ln in result.lines if ln.strip().startswith("argv ")]
        # ★ 计数断言：1 条阶段 1 + 2 条阶段 2 = 3 条命令，一条不多一条不少。
        # 空集合的断言永远是绿的，这条专防"其实一条 argv 都没打出来"。
        self.assertEqual(len(argv_lines), 1 + len(EXPECTED_RUN_IDS))
        self.assertIn(FAKE_STRMOUT_BIN, argv_lines[0])
        self.assertIn("-templateFile", argv_lines[0])
        self.assertIn(self.rel("gdsout"), argv_lines[0])
        for index, temperature in enumerate(TEMPERATURES):
            line = argv_lines[index + 1]
            self.assertIn(FAKE_EWAVE_BIN, line)
            # 轴的两个 flag（`--corner` 同时改 `--emssTechFile` 的 ptxt 文件名，BRIEF §7）
            self.assertIn(f"--corner={CORNERS[0]}", line)
            self.assertIn(f"--emssTechFile={FAKE_PTXT_DIR}/tech_{CORNERS[0]}.ptxt", line)
            self.assertIn(f"--temperature={temperature}", line)
            # 机制层：每个组合一个独立 workDir —— 绕开"同 corner/temp 静默覆盖"的全部手段
            self.assertIn(f"--workDir={self.rel('run_dir')}", line)
            self.assertIn(f"--gds={self.rel('gds')}", line)

    def test_dry_run_limit_shortens_the_printout_not_the_conclusion(self) -> None:
        """`--limit` 只影响打印的详细程度，**不影响结论**。

        跟着"打印了几条"数会让 `--limit 1` 报出 "1 commands built" —— 而那正是用户
        拿来判断"这份 spec 在这台机器上能不能真跑"的数字，少数一条就是假绿。
        """
        result = self._dry_run(self.write_spec(), "--limit", "1")
        self.assertEqual(result.code, cli.EXIT_OK, result.err)
        # 结论不变：两个 run 都规划了，两条命令都拼出来了。
        self.assertIn("dry-run: 2 runs planned, 2 commands built, 0 files written", result.out)
        # ★ 计数断言：两个 run 的抬头都还在，但只有 1 条阶段 2 的 argv 被详细打印
        #   （加上阶段 1 那条 = 2 条 argv 行）。
        self.assertEqual(result.count(r"^  \[\d+/2\] "), len(EXPECTED_RUN_IDS))
        self.assertEqual(len([ln for ln in result.lines if ln.strip().startswith("argv ")]), 2)
        self.assertNotIn(EXPECTED_TREE["cmd_sh_1"], result.out)

    def test_dry_run_reports_a_changed_temperature_negative(self) -> None:
        """反向：**同一条构造路径**，只把第二个温度从 125.0 改成 25.0。

        断言输出确实跟着变了 —— 否则上面那条 golden 可能是在对着一份写死的模板打勾。
        """
        changed = "25.0"
        spec = self.write_spec(
            _spec_dict(self.batch_root, temperatures=(TEMPERATURES[0], changed))
        )
        result = self._dry_run(spec)
        self.assertEqual(result.code, cli.EXIT_OK, result.err)
        self.assertIn(f"  [2/2] dA/base/typical_25_0", result.out)
        self.assertIn(f"--temperature={changed}", result.out)
        # 原来那条必须消失（run_id、目录名、flag 三处一起变，少变一处就是"目录名说一套、
        # 命令行说另一套" —— 本工具存在的理由）。
        self.assertNotIn(EXPECTED_RUN_IDS[1], result.out)
        self.assertNotIn(f"--temperature={TEMPERATURES[1]}", result.out)
        self.assertNotIn(EXPECTED_TREE["ewave_dir_1"], result.out)

    def test_dry_run_without_site_coordinates_still_prints_the_matrix(self) -> None:
        """本机（没有官方 run 目录）也要能看见"这批会跑哪些 run" —— 那是 dry-run 最常
        被用来回答的问题，和坐标无关。命令拼不出来就逐条说明，**退出码仍是 0**。"""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["dry-run", self.write_spec()])  # 不注入坐标
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("dry-run: 2 runs planned, 0 commands built, 0 files written", out.getvalue())
        self.assertIn("<unavailable>", out.getvalue())

    def test_dry_run_strict_fails_without_site_coordinates(self) -> None:
        """`--strict` 是给红区的机器判据："这份 spec 在这台机器上已经可以真跑了"。"""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["dry-run", self.write_spec(), "--strict"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("next:", err.getvalue())

    def test_dry_run_self_test_still_exits_zero(self) -> None:
        """`scripts/check.sh` 第 4 步的那条路。闸门跑的是 `ewave_batch/__main__.py` 的短路，
        本模块认同一个 flag 是为了让 `python cli.py dry-run --self-test` 给出同样的答案 ——
        闸门的判据不该取决于用户用了哪个入口。"""
        for entry in (cli.main, root_cli.main):
            result = _invoke(["dry-run", "--self-test"], entry=entry)
            self.assertEqual(result.code, 0, result.out + result.err)
            self.assertIn("self-test", result.out)


class DryRunWritesNothing(_CliTest):
    """★ `dry-run` 全程零写入 —— 跑前跑后整棵目录树逐条比对。"""

    def test_dry_run_writes_nothing(self) -> None:
        spec = self.write_spec()
        before = _snapshot(self.root)
        result = _invoke(["dry-run", spec])
        after = _snapshot(self.root)
        self.assertEqual(result.code, cli.EXIT_OK, result.err)
        self.assertEqual(after, before, "dry-run 动了磁盘")
        # ★ 计数断言：比对里确实有东西（至少 spec 那一份）。空快照之间的 diff 永远是绿的。
        self.assertGreaterEqual(len(before), 1)
        self.assertFalse(os.path.exists(self.batch_dir))

    def test_dry_run_writes_nothing_negative(self) -> None:
        """反向：同一套快照去看 `run` —— 它**必须**被看见写了东西。

        没有这一条的话，"跑前跑后一样"既可能是 dry-run 干净，也可能是 `_snapshot`
        根本没在看那个目录（比如路径写错了）。两种情况都是绿的。
        """
        spec = self.write_spec()
        before = _snapshot(self.root)
        result = _invoke(["run", spec, "--poll-interval", "0"])
        after = _snapshot(self.root)
        self.assertEqual(result.code, cli.EXIT_OK, result.err)
        self.assertNotEqual(after, before)
        new_paths = sorted(set(after) - set(before))
        self.assertGreater(len(new_paths), 0)
        self.assertIn(f"batches/{BATCH_NAME}/{BATCH_JSON_NAME}", new_paths)


# ==========================================================================
# 3. run
# ==========================================================================


class RunGolden(_CliTest):
    """`run`：展开矩阵 → 建 driver → 驱动到全部终态。"""

    def test_run_golden_lines(self) -> None:
        result = _invoke(["run", self.write_spec(), "--poll-interval", "0"])
        self.assertEqual(result.code, cli.EXIT_OK, result.out + result.err)
        # ★ 手写的关键行。事件标签来自 model.EventKind 的取值。
        self.assert_lines_present(
            result,
            [
                f"batch {BATCH_NAME}: 2 runs -> {self.batch_dir}",
                "  scheduler   fake, max 4 in flight",
                f"[submitted] {EXPECTED_RUN_IDS[0]}",
                f"[submitted] {EXPECTED_RUN_IDS[1]}",
                f"[finished] {EXPECTED_RUN_IDS[0]}",
                f"[archived] {EXPECTED_RUN_IDS[0]}",
                f"batch {BATCH_NAME}: 2 runs: 2 done",
                f"  state       {self.batch_dir}/{BATCH_JSON_NAME}",
                f"  summary     {self.batch_dir}/runs.csv",
            ],
        )
        # ★ 计数断言：提交了恰好 2 次（每个 run 一次，**不自动重试**，§12）。
        self.assertEqual(result.count(r"^\[submitted\] "), len(EXPECTED_RUN_IDS))
        self.assertEqual(result.count(r"^\[archived\] "), len(EXPECTED_RUN_IDS))
        self.assertTrue(os.path.isfile(f"{self.batch_dir}/{BATCH_JSON_NAME}"))
        self.assertTrue(os.path.isfile(f"{self.batch_dir}/runs.csv"))

    def test_run_reports_a_failed_run_negative(self) -> None:
        """反向：**同一条构造路径**，只给第二个 run 注入一条实测过的坑
        （0 字节产物 + 日志报 done，BRIEF §10）。

        断言 CLI 把它报成 failed 且退 1 —— 否则上面那条 golden 只证明了
        "全成的时候打印得好看"，证明不了它**看得见失败**。而 `exit=0 却空手而归`
        正是本工具存在的全部理由。
        """
        result = _invoke(
            ["run", self.write_spec(), "--poll-interval", "0"],
            modes={EXPECTED_RUN_IDS[1]: FakeFailureMode.ZERO_BYTE_OUTPUT},
        )
        self.assertEqual(result.code, cli.EXIT_RUN_FAILED, result.out)
        self.assertIn(f"batch {BATCH_NAME}: 2 runs: 1 done, 1 failed", result.out)
        self.assertEqual(result.count(rf"^\[failed\] {re.escape(EXPECTED_RUN_IDS[1])}"), 1)
        self.assertEqual(result.count(rf"^\[archived\] {re.escape(EXPECTED_RUN_IDS[0])}"), 1)
        # 失败的那个一个 artifact 都没归档（先验后删）。
        self.assertEqual(result.count(rf"^\[archived\] {re.escape(EXPECTED_RUN_IDS[1])}"), 0)
        self.assertIn("next", result.out)

    def test_injected_backends_match_the_real_ones(self) -> None:
        """正反两条确实在比同一件事：注入空 `modes` 的那条路和不注入的那条路，
        终态**逐条相同**。没有这一条，`_negative` 里的差异可能来自"换了个后端"
        而不是"注入了失败模式"。"""
        plain = _invoke(["run", self.write_spec(), "--poll-interval", "0"])
        injected = _invoke(["run", self.write_spec(name="s2.json"), "--poll-interval", "0"], modes={})
        self.assertEqual(plain.code, injected.code)
        self.assertIn(f"batch {BATCH_NAME}: 2 runs: 2 done", plain.out)
        self.assertIn(f"batch {BATCH_NAME}: 2 runs: 2 done", injected.out)

    def test_run_submits_nothing_when_no_command_can_be_built(self) -> None:
        """坐标缺一样，**每一个** run 都会挂在同一个原因上 ⇒ 一个 job 都不提交，
        错误只说一遍，且带下一步（§12 fail-fast 的精神在阶段 2 之前也成立）。"""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["run", self.write_spec(), "--poll-interval", "0"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("2 of 2 commands could not be built", err.getvalue())
        self.assertIn("next:", err.getvalue())
        self.assertFalse(os.path.exists(f"{self.batch_dir}/{BATCH_JSON_NAME}"))

    def test_ctrl_c_cancels_and_exits_130(self) -> None:
        """Ctrl-C 的退出码写在 `--help` 里（130 = 128 + SIGINT）⇒ 必须有机器判据。

        在 `submit` 上抛 `KeyboardInterrupt`：`Driver.tick` 只兜 `Exception`，
        `KeyboardInterrupt` 是 `BaseException` ⇒ 它会一路穿到 `_drive` 的那道
        `except KeyboardInterrupt` —— 那正是"先取消在飞的 job 再退"的落点。
        """

        class _Interrupting(FakeScheduler):
            def submit(self, plan, *, resources="", name=""):
                raise KeyboardInterrupt

        def factory(options, contexts):
            runner = FakeRunner()
            return _Interrupting(runner), runner

        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli, "discover_facts", _fake_discover))
            stack.enter_context(mock.patch.object(cli, "_make_backends", factory))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            code = cli.main(["run", self.write_spec(), "--poll-interval", "0"])
        self.assertEqual(code, cli.EXIT_INTERRUPTED, out.getvalue())
        self.assertIn("interrupted - cancelling in-flight jobs", out.getvalue())
        # 取消之后没提交过的 run 记成 skipped（RunStatus 没有 cancelled 态）。
        self.assertIn(f"batch {BATCH_NAME}: 2 runs: 2 skipped", out.getvalue())

    def test_make_backends_fake_shares_one_runner(self) -> None:
        """`fake` 那一路 scheduler 和 runner 必须是**同一个** `FakeRunner`：
        阶段 1（strmout）走 runner，阶段 2 的产物由 scheduler 让 runner 写 ——
        两个对象的话本机假批次的阶段 1 会去找一个根本不存在的 `strmout`，整批 skipped。"""
        from ewave_batch.model import BatchOptions

        scheduler, runner = cli._make_backends(BatchOptions(scheduler="fake"), {})
        self.assertIsInstance(scheduler, FakeScheduler)
        self.assertIsInstance(runner, FakeRunner)
        self.assertIs(scheduler.runner, runner)


# ==========================================================================
# 4. resume
# ==========================================================================


class ResumeGolden(_CliTest):
    """`resume`：**只补没成的**。这是个过滤器 ⇒ 两个方向都要断言。"""

    def _run_then_resume(self, modes) -> tuple[_Result, _Result]:
        spec = self.write_spec()
        first = _invoke(["run", spec, "--poll-interval", "0"], modes=modes)
        second = _invoke(["resume", self.batch_dir, "--poll-interval", "0"])
        return first, second

    def test_resume_golden_lines_and_only_retries_the_failed_run(self) -> None:
        first, second = self._run_then_resume(
            {EXPECTED_RUN_IDS[1]: FakeFailureMode.ZERO_BYTE_OUTPUT}
        )
        self.assertEqual(first.code, cli.EXIT_RUN_FAILED, first.out)
        self.assertEqual(second.code, cli.EXIT_OK, second.out + second.err)
        self.assert_lines_present(
            second,
            [
                f"resuming {self.batch_dir}: 2 runs: 1 done, 1 failed before this resume",
                f"[submitted] {EXPECTED_RUN_IDS[1]}",
                f"batch {BATCH_NAME}: 2 runs: 2 done",
            ],
        )
        # ★ 计数断言（方向一：没重跑已经成的）。一个 run 可能 10 核 100 GB 跑 35 分钟 ——
        # "跑完了"是绿的，"重跑了一个"也是绿的，**只有提交次数能把两者分开**。
        self.assertEqual(second.count(r"^\[submitted\] "), 1)
        self.assertEqual(second.count(rf"^\[submitted\] {re.escape(EXPECTED_RUN_IDS[0])}"), 0)
        # 方向二：没漏补没成的。
        self.assertEqual(second.count(rf"^\[submitted\] {re.escape(EXPECTED_RUN_IDS[1])}"), 1)

    def test_resume_resubmits_nothing_when_all_runs_are_done_negative(self) -> None:
        """反向：**同一条构造路径**，只把注入的失败模式去掉（全成）。

        断言 resume 一个 job 都不提交 —— 否则上面那条"只提交了 1 次"可能只是
        "反正每次都提交 1 次"，而不是"过滤器挑对了人"。
        """
        first, second = self._run_then_resume({})
        self.assertEqual(first.code, cli.EXIT_OK, first.out)
        self.assertEqual(second.code, cli.EXIT_OK, second.out + second.err)
        self.assertIn(
            f"resuming {self.batch_dir}: 2 runs: 2 done before this resume", second.out
        )
        self.assertEqual(second.count(r"^\[submitted\] "), 0)

    def test_resume_without_a_batch_json_negative(self) -> None:
        result = _invoke(["resume", f"{self.root}/nope", "--poll-interval", "0"])
        self.assertEqual(result.code, cli.EXIT_USAGE)
        self.assertIn("next:", result.err)


# ==========================================================================
# 5. archive
# ==========================================================================


class ArchiveGolden(_CliTest):
    """`archive`：D5 —— 参数文件收进 `sparam/` 扁平区，mesh/中间件删掉。

    也是个过滤器（只归档 done 的）⇒ 两个方向都要断言。
    """

    def _run_then_litter(self, modes) -> _Result:
        """先跑一遍假批次，再往每个 run 的输出目录里丢两份"中间件"。"""
        result = _invoke(["run", self.write_spec(), "--poll-interval", "0"], modes=modes)
        for key in ("ewave_dir_0", "ewave_dir_1"):
            directory = self.rel(key)
            if not os.path.isdir(directory):
                continue
            for name in JUNK_FILES:
                with open(os.path.join(directory, name), "wb") as handle:
                    handle.write(JUNK_BYTES)
        return result

    def test_archive_golden_lines(self) -> None:
        self._run_then_litter({})
        result = _invoke(["archive", self.batch_dir])
        self.assertEqual(result.code, cli.EXIT_OK, result.out + result.err)
        # ★ 手写：keep 模式是 BatchOptions.archive_keep 的默认值（同时盖住 .sNp 和 _sample.sNp）。
        # 留 2（主参数文件 + _sample）、删 2（丢进去的两份中间件）、释放 2 x 7 字节。
        self.assert_lines_present(
            result,
            [
                f"archive {self.batch_dir}  (keep: *.s[0-9]*p)",
                f"  [1/2] {EXPECTED_RUN_IDS[0]}  kept 2, removed 2, "
                f"freed {EXPECTED_FREED_BYTES} bytes",
                f"  [2/2] {EXPECTED_RUN_IDS[1]}  kept 2, removed 2, "
                f"freed {EXPECTED_FREED_BYTES} bytes",
                "archive: 2 archived, 0 skipped, 0 problems",
            ],
        )
        # ★ 计数断言：删完之后目录里恰好剩 2 个文件，一个不多一个不少。
        for key in ("ewave_dir_0", "ewave_dir_1"):
            left = sorted(os.listdir(self.rel(key)))
            self.assertEqual(len(left), 2, f"{key} 剩下的是 {left}")
            for name in JUNK_FILES:
                self.assertNotIn(name, left)

    def test_archive_skips_runs_that_are_not_done_negative(self) -> None:
        """反向：**同一条构造路径**，只给第二个 run 注入失败。

        断言它被跳过、且它目录里的中间件**一个都没删** —— 失败的 run 的 mesh 和日志
        正是诊断材料（先验后删，D5）。没有这一条，上面那条"删了 2 个"可能只是
        "见文件就删"，那会把诊断材料一起删光，而且不可逆。
        """
        self._run_then_litter({EXPECTED_RUN_IDS[1]: FakeFailureMode.ZERO_BYTE_OUTPUT})
        result = _invoke(["archive", self.batch_dir])
        self.assertEqual(result.code, cli.EXIT_OK, result.out + result.err)
        self.assert_lines_present(
            result,
            [
                f"  [2/2] {EXPECTED_RUN_IDS[1]}  skipped (failed)",
                "archive: 1 archived, 1 skipped, 0 problems",
            ],
        )
        survivors = sorted(os.listdir(self.rel("ewave_dir_1")))
        for name in JUNK_FILES:
            self.assertIn(name, survivors)

    def test_archive_dry_run_touches_nothing(self) -> None:
        self._run_then_litter({})
        before = _snapshot(self.batch_dir)
        result = _invoke(["archive", self.batch_dir, "--dry-run"])
        after = _snapshot(self.batch_dir)
        self.assertEqual(result.code, cli.EXIT_OK, result.err)
        self.assertIn("dry run - nothing is copied, nothing is deleted", result.out)
        self.assertEqual(after, before)
        # ★ 计数断言：报告里说会删 —— 但磁盘上一个都没少。
        self.assertIn(f"removed 2, freed {EXPECTED_FREED_BYTES} bytes", result.out)


# ==========================================================================
# 6. status
# ==========================================================================


class StatusGolden(_CliTest):
    """`status`：读 `batch.json` 打印状态 / 墙钟 / jobid / 产物（+ 日志里的收敛与峰值内存）。"""

    def _row(self, result: _Result, run_id: str) -> list[str]:
        """表里那一行 → token 列表。列宽是按数据算的，逐字比整行会把"对齐"也变成契约。"""
        for line in result.lines:
            if line.strip().startswith(run_id + " ") or line.strip() == run_id:
                return line.split()
        self.fail(f"表里没有 {run_id} 这一行：\n{result.out}")
        return []

    def _write_log(self, key: str, text: str) -> None:
        with open(os.path.join(self.rel(key), "emsolver.log"), "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_status_golden_row(self) -> None:
        _invoke(["run", self.write_spec(), "--poll-interval", "0"])
        # 只给**第一个** run 放一份日志 —— 第二个必须仍然是"没测到"。
        self._write_log("ewave_dir_0", FAKE_EMSOLVER_LOG)
        result = _invoke(["status", self.batch_dir])
        self.assertEqual(result.code, cli.EXIT_OK, result.out + result.err)

        self.assert_lines_present(
            result,
            [
                f"batch       {BATCH_NAME}",
                f"  directory   {self.batch_dir}",
                "  scheduler   fake",
                "  streamout   1/1 designs done",
                f"status: 2 runs: 2 done",
            ],
        )
        self.assertEqual(
            [c for c in result.lines if c.strip().startswith("run ")][0].split(),
            ["run", "status", "wall(s)", "job", "conv", "peakMB", "sparam"],
        )
        # ★ 手写的整行（token 化）。每一格的出处：
        #   status  = core.layout.verify_run_outputs 验过 ⇒ done
        #   wall(s) = FakeScheduler 的假时钟，每格 15 秒
        #   job     = FakeScheduler 按提交顺序发的号
        #   conv / peakMB = 我们刚写进去的那份 emsolver.log
        #   sparam  = 归档后的扁平区文件（相对 batch_dir）
        self.assertEqual(
            self._row(result, EXPECTED_RUN_IDS[0]),
            [
                EXPECTED_RUN_IDS[0],
                "done",
                EXPECTED_WALL_SECONDS,
                EXPECTED_JOB_IDS[0],
                EXPECTED_CONVERGED,
                EXPECTED_PEAK_MB,
                EXPECTED_ARTIFACTS[0],
            ],
        )

    def test_status_does_not_borrow_the_neighbour_logs(self) -> None:
        """★ 第二个 run 没有日志 ⇒ 它的 conv / peakMB 必须是 `-`。

        为什么值得一条独立的测试：`<axes-slug>` 按定义**不含** corner/temperature，
        于是同一个 `run_dir` 底下住着 N 个 run，而 `core.logparse.parse_run_logs`
        会连**直接子目录**一起读 —— 对着 `run_dir` 读就是把邻居的日志合并进来，
        然后报出一份张冠李戴的收敛结论，而且看起来完全正常（每个 run 都"有数据"）。
        """
        _invoke(["run", self.write_spec(), "--poll-interval", "0"])
        self._write_log("ewave_dir_0", FAKE_EMSOLVER_LOG)
        result = _invoke(["status", self.batch_dir])
        row = self._row(result, EXPECTED_RUN_IDS[1])
        self.assertEqual(row[4], "-", "第二个 run 借用了第一个 run 的日志")
        self.assertEqual(row[5], "-", "第二个 run 借用了第一个 run 的日志")

    def test_status_reports_a_failed_run_negative(self) -> None:
        """反向：**同一条构造路径**，只给第二个 run 注入失败。

        断言它那一行确实变了、汇总跟着变、退出码变成 1 —— 否则上面那条 golden
        证明不了 status 真的在读每个 run 的状态（打印一列常量 `done` 也是绿的）。
        """
        _invoke(
            ["run", self.write_spec(), "--poll-interval", "0"],
            modes={EXPECTED_RUN_IDS[1]: FakeFailureMode.ZERO_BYTE_OUTPUT},
        )
        result = _invoke(["status", self.batch_dir])
        self.assertEqual(result.code, cli.EXIT_RUN_FAILED, result.out)
        self.assertIn("status: 2 runs: 1 done, 1 failed", result.out)
        self.assertEqual(self._row(result, EXPECTED_RUN_IDS[1])[1], "failed")
        # 成的那个一个字都没变（失败没有污染它）。
        good = self._row(result, EXPECTED_RUN_IDS[0])
        self.assertEqual(good[1], "done")
        self.assertEqual(good[3], EXPECTED_JOB_IDS[0])
        self.assertIn("next", result.err)

    def test_status_no_logs_skips_the_log_columns(self) -> None:
        _invoke(["run", self.write_spec(), "--poll-interval", "0"])
        self._write_log("ewave_dir_0", FAKE_EMSOLVER_LOG)
        result = _invoke(["status", self.batch_dir, "--no-logs"])
        row = self._row(result, EXPECTED_RUN_IDS[0])
        self.assertEqual(row[4], "-")
        self.assertEqual(row[5], "-")

    def test_status_writes_nothing(self) -> None:
        _invoke(["run", self.write_spec(), "--poll-interval", "0"])
        before = _snapshot(self.batch_dir)
        _invoke(["status", self.batch_dir])
        self.assertEqual(_snapshot(self.batch_dir), before)


# ==========================================================================
# 7. 汇总行的过滤器（"只列非零的桶"）
# ==========================================================================


def _state_with(*statuses: RunStatus) -> BatchState:
    return BatchState(
        batch_name=BATCH_NAME,
        batch_dir="/tmp/fake/batch",
        runs=[
            Run(run_id=f"r{i}", design_key=DESIGN_KEY, status=status)
            for i, status in enumerate(statuses)
        ],
    )


class SummaryFilter(unittest.TestCase):
    """`_summary_line` 只列非零的桶 —— 那是个过滤器，两个方向都要断言。"""

    def test_only_non_empty_buckets_are_listed(self) -> None:
        line = cli._summary_line(
            _state_with(RunStatus.DONE, RunStatus.DONE, RunStatus.FAILED)
        )
        self.assertEqual(line, "3 runs: 2 done, 1 failed")
        # ★ 计数断言：列出来的桶数 == 实际出现过的状态数（不是"恰好看起来对"）。
        self.assertEqual(len(line.split(":")[1].split(",")), 2)

    def test_a_status_that_is_present_is_never_dropped_negative(self) -> None:
        """反向：加一个 `skipped` 进去，它必须出现。

        没有这一条，"只列非零"可能是"只列我恰好写进代码的那两个"，
        而 skipped（阶段 1 失败 ⇒ 整列不跑）恰恰是最需要被看见的那个状态。
        """
        line = cli._summary_line(
            _state_with(RunStatus.DONE, RunStatus.DONE, RunStatus.FAILED, RunStatus.SKIPPED)
        )
        self.assertEqual(line, "4 runs: 2 done, 1 failed, 1 skipped")
        self.assertEqual(len(line.split(":")[1].split(",")), 3)

    def test_empty_batch(self) -> None:
        self.assertEqual(cli._summary_line(_state_with()), "0 runs: none")


# ==========================================================================
# 8. 输入不对的时候：带「下一步」的错误 + 非 0 退出码（不是崩栈）
# ==========================================================================


class InputErrors(_CliTest):
    """三条点名的负向：spec 不存在 / `batch.json` 损坏 / 落点在 spine 里。"""

    def _assert_usage_error(self, result: _Result, needle: str) -> None:
        self.assertEqual(result.code, cli.EXIT_USAGE, result.out + result.err)
        self.assertTrue(result.err.startswith("error: "), result.err)
        self.assertIn("next:", result.err)
        self.assertIn(needle, result.err)

    def test_missing_spec_negative(self) -> None:
        missing = f"{self.root}/nope/missing.json"
        for argv in (["dry-run", missing], ["run", missing]):
            self._assert_usage_error(_invoke(argv), "missing.json")

    def test_corrupt_batch_json_negative(self) -> None:
        os.makedirs(self.batch_dir, exist_ok=True)
        with open(f"{self.batch_dir}/{BATCH_JSON_NAME}", "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        for argv in (
            ["status", self.batch_dir],
            ["archive", self.batch_dir],
            ["resume", self.batch_dir, "--poll-interval", "0"],
        ):
            self._assert_usage_error(_invoke(argv), BATCH_JSON_NAME)

    def test_missing_batch_json_negative(self) -> None:
        os.makedirs(self.batch_dir, exist_ok=True)
        self._assert_usage_error(_invoke(["status", self.batch_dir]), "not a batch")

    def test_batch_root_inside_the_spine_negative(self) -> None:
        """CLAUDE.md 硬约束 4：`<workarea>/ewave_simulation/` 是官方 GUI 的地盘，只读。

        ⚠️ 这条**必须在 dry-run 上也成立**：dry-run 一个字节都不写，于是它永远撞不到
        `core.layout` 那道写时守卫 —— 而"落点选错了"恰恰是最该在 dry-run 阶段说清楚的事。
        """
        spine_root = f"{self.root}/wa/{layout_module.SPINE_DIRNAME}/batches"
        spec = self.write_spec(_spec_dict(spine_root))
        for argv in (["dry-run", spec], ["run", spec, "--poll-interval", "0"]):
            result = _invoke(argv)
            self._assert_usage_error(result, layout_module.SPINE_DIRNAME)
        self.assertFalse(os.path.exists(spine_root), "落点在 spine 里，一个目录都不许建")

    def test_batch_root_outside_the_spine_is_accepted_negative(self) -> None:
        """反向：把 `ewave_simulation` 从路径里拿掉，同一条命令必须放行。

        没有这一条，上面那条可能是在拒绝**所有**落点（比如一个总是抛异常的守卫），
        而那同样会让测试全绿。
        """
        ok_root = f"{self.root}/wa/ewave_batches/batches"
        result = _invoke(["dry-run", self.write_spec(_spec_dict(ok_root))])
        self.assertEqual(result.code, cli.EXIT_OK, result.err)

    def test_unknown_subcommand_exits_two(self) -> None:
        result = _invoke(["nosuch"])
        self.assertEqual(result.code, cli.EXIT_USAGE)

    def test_no_subcommand_prints_help(self) -> None:
        result = _invoke([])
        self.assertEqual(result.code, cli.EXIT_USAGE)
        self.assertIn("exit codes:", result.out)

    def test_help_and_version_return_instead_of_exiting(self) -> None:
        """冻结面写着"返回进程退出码，**不 `sys.exit`**（GUI 也会调它）"——
        argparse 在 `--help` / 用法错上都调 `sys.exit`，`main` 必须把它接回成返回值，
        否则 GUI 里一个参数笔误就把整个界面进程带走。"""
        for argv, expected in ((["--help"], 0), (["--version"], 0), (["--nosuch"], 2)):
            result = _invoke(argv)
            self.assertEqual(result.code, expected, argv)


# ==========================================================================
# 9. ★ 惰性 import（CLAUDE.md 硬约束 5）—— 机器判据在**子进程**里
# ==========================================================================

_BLOCK_TKINTER = """\
import sys
# `sys.modules[name] = None` 让 `import name` 当场抛 ImportError —— 等价于"这台机器没装"。
sys.modules["tkinter"] = None
sys.modules["_tkinter"] = None
sys.path.insert(0, {root!r})
"""

_DETECT = """\
import sys
sys.path.insert(0, {root!r})
{extra}
import ewave_batch.cli
import cli
leaked = sorted(
    name for name in sys.modules
    if name == "tkinter" or name == "gui" or name.startswith(("tkinter.", "gui."))
)
if leaked:
    sys.stderr.write("leaked: " + repr(leaked))
    raise SystemExit(1)
raise SystemExit(0)
"""


def _python(code: str) -> subprocess.CompletedProcess:
    """在一个**干净的子进程**里跑一段代码。惰性 import 只有隔离的进程测得准 ——
    本进程里 `gui` 早被别的测试 import 进 `sys.modules` 了。"""
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        # ⚠️ **必须显式给 encoding/errors，不能靠 `text=True` 的默认值。**
        # 默认是「用这台机器的 locale 编码解码子进程输出」——本机 locale 是 GBK，
        # 而子进程在 PYTHONIOENCODING=utf-8 下吐 UTF-8 ⇒ 读取线程里抛 UnicodeDecodeError，
        # `proc.stdout`/`stderr` 双双变成 None，随后 `proc.stdout + proc.stderr` 抛 TypeError，
        # **把真正的失败原因盖掉**。2026-08-19 实测：
        #   python -m unittest tests.test_cli.LazyImport            -> OK
        #   PYTHONIOENCODING=utf-8 python -m unittest ...           -> ERROR（就是这个）
        # 又是一条「绿得取决于跑测试那台机器的偶然状态」——本项目今晚栽过的同一类。
        # 红区 locale 与本机不同，那边会稳定发作。
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )


class LazyImport(_CliTest):
    """无 `$DISPLAY`、甚至没装 tkinter 的纯 ssh 会话里，CLI 必须照常可用。"""

    def _prepare_batch(self) -> str:
        """造一个 `batch.json`（run 全是 READY）——  `status` 要有东西可读。

        用的是 `core.spec` 的正路，不是手搓 JSON：手搓的那份一旦和 schema 漂了，
        这条测试会因为**别的原因**红，然后没人再信它。
        """
        spec_path = self.write_spec()
        spec = spec_module.load_spec(spec_path)
        state = spec_module.spec_to_batch(spec, batch_root="", tool_version="test")
        state.batch_dir = self.batch_dir
        os.makedirs(self.batch_dir, exist_ok=True)
        layout_module.write_batch_state(f"{self.batch_dir}/{BATCH_JSON_NAME}", state)
        return spec_path

    def test_cli_works_without_tkinter(self) -> None:
        spec_path = self._prepare_batch()
        code = _BLOCK_TKINTER.format(root=ROOT) + (
            "from ewave_batch import cli\n"
            "import cli as root_cli\n"
            f"rc = cli.main(['dry-run', {spec_path!r}])\n"
            "if rc != 0: raise SystemExit('dry-run exit ' + str(rc))\n"
            f"rc = root_cli.main(['status', {self.batch_dir!r}])\n"
            "if rc != 0: raise SystemExit('status exit ' + str(rc))\n"
            "raise SystemExit(0)\n"
        )
        proc = _python(code)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_cli_works_without_tkinter_negative(self) -> None:
        """反向：同一段代码，只多一行 `import tkinter`。

        断言子进程**确实炸了** —— 否则"没装 tkinter 也退 0"既可能是惰性 import 做对了，
        也可能是 `sys.modules[...] = None` 那一手根本没生效（tkinter 照常可 import），
        两种情况都是绿的。
        """
        code = _BLOCK_TKINTER.format(root=ROOT) + "import tkinter\n"
        proc = _python(code)
        self.assertNotEqual(proc.returncode, 0)
        # `sys.modules[name] = None` 之下 Python 抛的是 ModuleNotFoundError
        # （ImportError 的子类），消息逐字是 "import of tkinter halted; None in sys.modules"。
        self.assertIn("import of tkinter halted", proc.stderr)

    def test_importing_cli_does_not_import_tkinter_or_gui(self) -> None:
        proc = _python(_DETECT.format(root=ROOT, extra=""))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_importing_cli_does_not_import_tkinter_or_gui_negative(self) -> None:
        """反向：同一个检测器，只多一行 `import gui.app`。

        断言它**抓得到** —— 否则"没检测到泄漏"和"检测器根本没在看"看起来一模一样。
        """
        proc = _python(_DETECT.format(root=ROOT, extra="import gui.app"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("leaked", proc.stderr)

    def test_cli_module_source_has_no_top_level_gui_import(self) -> None:
        """源码层面的第二道：`gui` / `tkinter` 只许出现在缩进过的行里（= 函数体内）。"""
        for rel in ("ewave_batch/cli.py", "cli.py"):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as handle:
                text = handle.read()
            offenders = [
                line
                for line in text.splitlines()
                if re.match(r"^(import|from)\s+(tkinter|gui)\b", line)
            ]
            self.assertEqual(offenders, [], f"{rel} 在模块顶层 import 了 GUI：{offenders}")


# ==========================================================================
# 10. 顶层薄壳
# ==========================================================================


class RootShell(_CliTest):
    """仓库根的 `cli.py`：转发，不加工。"""

    def test_root_cli_forwards_to_the_package_cli(self) -> None:
        spec = self.write_spec()
        through_root = _invoke(["dry-run", spec], entry=root_cli.main)
        through_package = _invoke(["dry-run", spec], entry=cli.main)
        self.assertEqual(through_root.code, cli.EXIT_OK, through_root.err)
        self.assertEqual(through_root.out, through_package.out)

    def test_root_cli_main_is_defined_here_not_reexported(self) -> None:
        """冻结清单要求符号**定义在该模块里**（`model.FROZEN` 的规则 1：
        从别处 re-export 一个桩子不算实现）。self-test 也在查这条，这里再钉一遍。"""
        self.assertEqual(root_cli.main.__module__, "cli")


if __name__ == "__main__":
    unittest.main()
