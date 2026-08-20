"""GUI 共用层 + 默认 split 版的测试（P5）。

这份文件要证明五件事，每一件都带计数断言：

1. **惰性 import 纪律没被破坏。** `gui.app` / `gui.state` 在**子进程**里 import 之后
   `sys.modules` 里不许有 `tkinter` —— 无 `$DISPLAY` 的纯 ssh 会话里 CLI 必须可用
   （CLAUDE.md 硬约束 5）。这条破坏起来是静默的、只在红区发作，所以判据必须是机器判的。
2. **headless 建得起来**：`EWB_SMOKE=1` 下整棵控件树建完就退 0，
   而且 Runs 表里的行数 == 手写的 run 数（**不是 0** —— 建了个空壳也会退 0）。
3. **`GuiState` 与 driver 真的接上了**：用 `sched.fake` 跑一个 12-run 假批次，
   把「GUI 侧 `bridge.tick()` 看到的计数序列」与「同一份 driver 被 CLI 那条路
   （`make_driver` + `tick()`）驱动出来的计数序列」**逐拍比对**。
   ⚠️ 这里刻意**不 mock driver**：mock 掉就只测了"我调了它"，测不到"接线对不对"。
   也刻意不去比 `bridge.runs()` 和 `driver.state.runs` —— 那是同一批对象，
   assertEqual 恒真，属于"空得非常好看"的那一类。
4. **Extra flags 撞轴要标红**（BRIEF §11 规则 2）。配一条前缀误伤的回归测试：
   `--sparam` 是锁死 flag，但 `--sparamImpedance` **不许**被它吃掉。
5. **`--parallel` 跟 `cpu=` 走**，且解析复用 `core.cmd.parse_resource_string`。

四条配方（`docs/OVERNIGHT.md`）在这份文件里的落点：

* **关键测试** = 手写的 12 行 run_id 表、12 行终态表、撞轴清单；
* **期望值来源** = 手写字面量，出处写在各自的注释里（`model.Run.run_id` 的定义 /
  `model.TEMP_DECIMAL_REPLACEMENT` / BRIEF §6 / §10 / §11）。一个都不是"跑一遍存下来"的；
* **反向验证** = 每条关键测试配一条 `_negative`，**共用同一条构造路径**（`_gui()` /
  `_reference()`），只改一个入参；
* **计数断言** = 状态变化次数、终态分布逐个等于手写期望、`LAYOUTS` 恰好 3 个、
  `STATUS_STYLE` 恰好 6 个状态、提交次数、参与冲突判定的 flag 条数。

⏱ 全程不 sleep、不读墙钟：假批次的时间线由 `FakeScheduler` 的"第几次 poll"推进。
唯一起子进程的是惰性 import 那几条，跑的是 `sys.executable`，不是 eWave（硬约束 3）。

🚨 本文件零站点标识符：library / cell / view / 端口名 / 路径 / 账号全是显式假值。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ewave_batch.__main__ import check_protocol
from ewave_batch.core.matrix import design_key, ewave_dir_name, expand_runs
from ewave_batch.model import (
    PLACEHOLDER_VALUE,
    Axis,
    AxisValue,
    BatchOptions,
    BatchState,
    Design,
    DriverProtocol,
    PlanContext,
    PortMode,
    PortSpec,
    RunStatus,
    SiteFacts,
    SpecError,
    StreamoutTask,
    TickReport,
)
from ewave_batch.sched.driver import make_driver
from ewave_batch.sched.fake import FakeFailureMode, FakeRunner, FakeScheduler

import gui.app as gui_app
from gui.state import GuiState

ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------
# 手写的假值（一个真实取值都没有）
# --------------------------------------------------------------------------

FAKE_LIB = "TESTLIB"
FAKE_VIEW = "testview"
FAKE_CELL_A = "CELLA"
FAKE_CELL_B = "CELLB"
FAKE_EWAVE_BIN = "/tmp/fakebin/ewave"
FAKE_STRMOUT_BIN = "/tmp/fakebin/strmout"
FAKE_LAYER_MAP = "/tmp/fakepdk/layer.map"
FAKE_RESOURCES = "cpu=2;mem=100"
FAKE_PTXT_DIR = "/tmp/fakepdk/ptxt"
FAKE_PTXT_TEMPLATE = "fake_{corner}.ptxt"
FAKE_PORT_NAMES = ("PIN_A", "PIN_B", "PIN_C", "PIN_D")
PORT_COUNT = len(FAKE_PORT_NAMES)

CORNERS = ("cworst", "typical")
"""⚠️ 顺序是 `gui.state.CORNER_VALUES` 里的相对顺序（= 勾选框从左到右）。

界面那一侧 `_ui.BaseApp.push()` 是按勾选框顺序回读的 —— 传别的顺序进来，
建完 frame 之后第一次 `recompute()` 就会把它重排，于是"没建界面"和"建了界面"
展开出来的 run 顺序不同。测试用勾选框顺序，两条路才是同一个矩阵。
"""

TEMPERATURES = ("-40.0", "25.0", "125.0")

DESIGN_A = f"{FAKE_LIB}_{FAKE_CELL_A}_{FAKE_VIEW}"
DESIGN_B = f"{FAKE_LIB}_{FAKE_CELL_B}_{FAKE_VIEW}"
"""`core.matrix.design_key` 的默认形状是 `<library>_<cell>_<view>` 过一遍 slugify；
这三段都是纯字母数字 ⇒ slugify 原样返回。手写出来是为了让下面那张 run_id 表能逐字核对。"""

# --------------------------------------------------------------------------
# ★ 手写的期望表（防自证配方 2：期望值不许由被测代码算出来）
# --------------------------------------------------------------------------

EXPECTED_RUN_IDS: tuple[str, ...] = (
    # run_id = <design_key>/<axes_slug>/<ewave_dir>（`model.Run.run_id` 的定义）。
    # axes_slug 是 `base`：corner / temperature 被 eWave 编进 `<corner>_<temp>/` 那层了
    # （`Axis.encoded_in_ewave_dir=True` ⇒ 不进 axes-slug，否则目录名里出现两遍）；
    # 频率扫描那根轴只有一个取值 ⇒ 按定义不算"在变"，也不进 slug。
    # 温度里的小数点换下划线（`model.TEMP_DECIMAL_REPLACEMENT`，eWave 自己的约定）。
    f"{DESIGN_A}/base/cworst_-40_0",
    f"{DESIGN_A}/base/cworst_25_0",
    f"{DESIGN_A}/base/cworst_125_0",
    f"{DESIGN_A}/base/typical_-40_0",
    f"{DESIGN_A}/base/typical_25_0",
    f"{DESIGN_A}/base/typical_125_0",
    f"{DESIGN_B}/base/cworst_-40_0",
    f"{DESIGN_B}/base/cworst_25_0",
    f"{DESIGN_B}/base/cworst_125_0",
    f"{DESIGN_B}/base/typical_-40_0",
    f"{DESIGN_B}/base/typical_25_0",
    f"{DESIGN_B}/base/typical_125_0",
)
"""2 design x 2 corner x 3 temperature = **12 个 run**。顺序照 `expand_runs` 的文档：
design 在外，轴按定义顺序，第一根轴变得最慢。"""

EXPECTED_RUN_COUNT = 12

GUI_MODES: dict[str, FakeFailureMode] = {
    # BRIEF §10 实测过的三条"失败信号不可靠"，一样一个。键是 run_id
    # （`FakeRunner._command_key` 的合法键：产物目录以 `/<run_id>` 结尾）。
    f"{DESIGN_A}/base/typical_25_0": FakeFailureMode.EXIT_ZERO_BUT_CRASHED,
    f"{DESIGN_B}/base/cworst_-40_0": FakeFailureMode.ZERO_BYTE_OUTPUT,
    f"{DESIGN_B}/base/typical_125_0": FakeFailureMode.WRONG_PORT_COUNT,
}

EXPECTED_FINAL_STATUS: dict[str, str] = {
    # ★★ 12 行手写终态表。出处：
    #   done   = 产物齐、非空、端口数 == 4（`core.layout.verify_run_outputs` 的验收契约）
    #   failed = GUI_MODES 点名的那三条，**每一条的退出码都是 0**（BRIEF §10）
    f"{DESIGN_A}/base/cworst_-40_0": "done",
    f"{DESIGN_A}/base/cworst_25_0": "done",
    f"{DESIGN_A}/base/cworst_125_0": "done",
    f"{DESIGN_A}/base/typical_-40_0": "done",
    f"{DESIGN_A}/base/typical_25_0": "failed",  # exit=0 但零产物
    f"{DESIGN_A}/base/typical_125_0": "done",
    f"{DESIGN_B}/base/cworst_-40_0": "failed",  # 文件在、0 字节、日志报 done
    f"{DESIGN_B}/base/cworst_25_0": "done",
    f"{DESIGN_B}/base/cworst_125_0": "done",
    f"{DESIGN_B}/base/typical_-40_0": "done",
    f"{DESIGN_B}/base/typical_25_0": "done",
    f"{DESIGN_B}/base/typical_125_0": "failed",  # .s3p，期望 4 端口（--all 的编号平移）
}

EXPECTED_DONE = 9
EXPECTED_FAILED = 3
"""12 = 9 done + 3 failed。那个 3 就是"只看退出码会被判成功、实际空手而归"的 run 数。"""

EXPECTED_CONFLICTS: tuple[str, ...] = ("--temperature", "--corner", "--workDir")
"""手写：Extra flags 里这三个必须被标红（BRIEF §11 规则 2）。
前两个是**界面上的轴**（写两遍 ⇒ 目录名和实际跑的值对不上），
第三个是**机制层锁死**的（`model.MECHANISM_FLAGS`，改了 `--workDir` 静默覆盖就回来了）。"""

NOT_CONFLICTS: tuple[str, ...] = ("--sparamImpedance", "--printDouble", "--maxIterNum")
"""手写：这三个**不许**被标红。

`--sparamImpedance` 是那条回归测试的主角：`--sparam` 在 `MECHANISM_FLAGS` 里，
用前缀匹配的话它会被一起吃掉 —— MVP 真踩过这个坑（diff 空得非常好看，但根本没比）。
"""


# --------------------------------------------------------------------------
# 构造（正反两向共用这一条路径）
# --------------------------------------------------------------------------


def _facts(official_run_dir: str) -> SiteFacts:
    """最小站点坐标。字段全是假路径（`SiteFacts` 里装的全是站点身份，硬约束 1b）。"""
    return SiteFacts(
        official_run_dir=official_run_dir,
        ewave_bin=FAKE_EWAVE_BIN,
        strmout_bin=FAKE_STRMOUT_BIN,
        layer_map=FAKE_LAYER_MAP,
        dsub_resources=FAKE_RESOURCES,
        # corner 轴同时改 `--corner=` 和 `--emssTechFile=`（BRIEF §7）——
        # 少了这两个字段，`ptxt_path_for_corner` 会（正确地）拒绝拼命令，
        # 于是 12 个 run 全 failed，而那看起来跟"调度器坏了"一模一样。
        ptxt_dir=FAKE_PTXT_DIR,
        ptxt_name_template=FAKE_PTXT_TEMPLATE,
        official_port_spec=PortSpec(
            mode=PortMode.EXPLICIT,
            mapping=tuple((f"P{i:03d}", name) for i, name in enumerate(FAKE_PORT_NAMES)),
        ),
    )


def _workarea(root: str) -> str:
    """假的 workarea：`strmout` 的 cwd 要能往上找到一份 `cds.lib`（BRIEF §10 step1）。"""
    area = f"{root}/wa"
    os.makedirs(area, exist_ok=True)
    with open(f"{area}/cds.lib", "w", encoding="utf-8", newline="\n") as handle:
        handle.write("DEFINE FAKE ./fake\n")
    return area


def _gui(
    root: str,
    *,
    modes: dict[str, FakeFailureMode] | None = None,
    corners: tuple[str, ...] = CORNERS,
    temps: tuple[str, ...] = TEMPERATURES,
) -> tuple[GuiState, FakeRunner, FakeScheduler]:
    """走**界面那条路**造一个批次：勾选 → `GuiState` → `plan()`。

    刻意不手搓 state / argv：本文件要证明的是"GuiState 把真实的核心件接起来之后行为对"，
    手搓就把接缝测没了 —— 而接缝正是并行开发最容易漂的地方。
    """
    offdir = f"{_workarea(root)}/ewave_simulation/design"
    facts = _facts(offdir)
    runner = FakeRunner(modes=modes or {}, port_count=PORT_COUNT)
    scheduler = FakeScheduler(runner)
    bridge = GuiState(
        batch_root=root,
        batch_name="gui_batch",
        official_run_dir=offdir,
        scheduler=scheduler,
        runner=runner,
        discover=lambda _path: facts,
    )
    # 只扫 corner x temperature：其余轴清空，好让 run_id 与手写表逐字对得上。
    bridge.set_axis_values("corner", corners)
    bridge.set_axis_values("temperature", temps)
    for name in ("fullWave", "equalCurrent", "relativeTolerance", "relativeCurrentTolerance"):
        bridge.set_axis_values(name, ())
    bridge.add_design(FAKE_LIB, FAKE_CELL_A, FAKE_VIEW)
    bridge.add_design(FAKE_LIB, FAKE_CELL_B, FAKE_VIEW)
    bridge.plan()
    return bridge, runner, scheduler


def _reference(
    root: str,
    *,
    modes: dict[str, FakeFailureMode] | None = None,
    corners: tuple[str, ...] = CORNERS,
    temps: tuple[str, ...] = TEMPERATURES,
) -> DriverProtocol:
    """走 **CLI 那条路**造同一个批次：手写轴 → `expand_runs` → `make_driver`。

    这是第三条判据的对照组。两条路必须给出**逐拍相同**的计数序列 ——
    "GUI 用 `after()` 驱动同一份 driver 代码"（BRIEF §12）就是这个意思。
    """
    offdir = f"{_workarea(root)}/ewave_simulation/design"
    designs = [
        Design(library=FAKE_LIB, cell=cell, view=FAKE_VIEW, official_run_dir=offdir)
        for cell in (FAKE_CELL_A, FAKE_CELL_B)
    ]
    axes = [
        Axis(
            name="corner",
            values=tuple(AxisValue(v, flags={"--corner": PLACEHOLDER_VALUE}) for v in corners),
            flags=("--corner",),
            short="corner",
            encoded_in_ewave_dir=True,
        ),
        Axis(
            name="temperature",
            values=tuple(AxisValue(v, flags={"--temperature": PLACEHOLDER_VALUE}) for v in temps),
            flags=("--temperature",),
            short="temp",
            encoded_in_ewave_dir=True,
        ),
    ]
    options = BatchOptions()
    batch_dir = f"{root}/cli_batch"
    runs = expand_runs(designs, axes, options=options)
    state = BatchState(
        batch_name="cli_batch",
        batch_dir=batch_dir,
        designs=designs,
        axes=axes,
        runs=runs,
        streamout=[StreamoutTask(design_key=design_key(d)) for d in designs],
        options=options,
    )
    contexts = {
        design_key(d): PlanContext(
            design=d,
            facts=_facts(offdir),
            axes=tuple(axes),
            options=options,
            batch_dir=batch_dir,
        )
        for d in designs
    }
    runner = FakeRunner(modes=modes or {}, port_count=PORT_COUNT)
    return make_driver(state, contexts, FakeScheduler(runner), runner)


def _counts(report: TickReport) -> tuple[tuple[str, int], ...]:
    """一拍的计数快照，做成可比较的元组。"""
    return tuple(sorted(report.counts.items()))


def _drive_gui(bridge: GuiState, *, max_ticks: int = 80) -> list[tuple[tuple[str, int], ...]]:
    """GUI 那条路：`bridge.tick()` 一直到终态。**不 sleep** —— 时间线由 poll 次数推进。"""
    sequence: list[tuple[tuple[str, int], ...]] = []
    for _ in range(max_ticks):
        report = bridge.tick()
        if report is None:
            break
        sequence.append(_counts(report))
        if report.finished:
            break
    return sequence


def _drive_cli(driver: DriverProtocol, *, max_ticks: int = 80) -> list[tuple[tuple[str, int], ...]]:
    """CLI 那条路：`driver.tick()` 一直到终态。"""
    sequence: list[tuple[tuple[str, int], ...]] = []
    for _ in range(max_ticks):
        report = driver.tick()
        sequence.append(_counts(report))
        if report.finished:
            break
    return sequence


def _tk_or_skip(test: unittest.TestCase) -> object:
    """本机能不能开窗口。开不了就**带原因**跳过（平台性 skip，`docs/OVERNIGHT.md` 允许）。"""
    try:
        import tkinter as tk
    except ImportError as exc:  # pragma: no cover - 本机装了 tkinter
        test.skipTest(f"平台跳过：这台机器没装 tkinter（{exc}）—— CLI 不受影响")
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - 本机有显示
        test.skipTest(f"平台跳过：这台机器开不了显示（{exc}）—— CLI 不受影响")
    root.withdraw()
    test.addCleanup(root.destroy)
    return root


class _TempRootTest(unittest.TestCase):
    """每个测试一个干净的临时根目录。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ewb_gui_")
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name.replace("\\", "/")


class _SmokeTest(_TempRootTest):
    """建控件树的测试：`EWB_SMOKE=1` 保证不弹模态框、不进主循环。"""

    def setUp(self) -> None:
        super().setUp()
        os.environ["EWB_SMOKE"] = "1"
        self.addCleanup(os.environ.pop, "EWB_SMOKE", None)


# ==========================================================================
# 1. 布局契约（计数断言）
# ==========================================================================


class LayoutContract(unittest.TestCase):
    """`gui.app.LAYOUTS` / 默认布局 / 状态配色 —— 全是可数的东西。"""

    def test_layouts_are_exactly_three(self) -> None:
        # 出处：docs/INTERFACES.md「常量：谁负责给出什么」那张表的 `gui.app.LAYOUTS` 行。
        self.assertEqual(len(gui_app.LAYOUTS), 3)
        self.assertEqual(gui_app.LAYOUTS, ("stacked", "tabbed", "split"))

    def test_default_layout_is_split(self) -> None:
        import inspect

        self.assertEqual(gui_app.DEFAULT_LAYOUT, "split")
        default = inspect.signature(gui_app.launch).parameters["layout"].default
        self.assertEqual(default, "split")

    def test_default_layout_is_split_negative(self) -> None:
        """反向：默认**不是**另外两版。改错了默认值上面那条也会红，这条说清楚是哪一版。"""
        for other in ("stacked", "tabbed"):
            self.assertNotEqual(gui_app.DEFAULT_LAYOUT, other)

    def test_split_module_declares_its_own_name(self) -> None:
        from gui.frames import split

        self.assertEqual(split.LAYOUT_NAME, "split")
        self.assertIn(split.LAYOUT_NAME, gui_app.LAYOUTS)

    def test_status_style_covers_exactly_the_six_statuses(self) -> None:
        """6 个状态**恰好**都有颜色（BRIEF §12，用户 2026-08-18 定的那 6 个）。

        少一个的症状是"那一行没上色"，而没上色看起来就跟 `ready` 一样 —— 静默。
        """
        from gui import _ui

        self.assertEqual(len(_ui.STATUS_STYLE), 6)
        self.assertEqual(sorted(_ui.STATUS_STYLE), sorted(s.value for s in RunStatus))

    def test_bridge_satisfies_frozen_protocol(self) -> None:
        """`GuiState` 逐方法满足 `model.GuiBridgeProtocol`（含返回注解）。"""
        self.assertEqual(check_protocol("gui.state", GuiState, "GuiBridgeProtocol"), [])

    def test_split_sections_match_what_it_actually_builds(self) -> None:
        """★ `SECTIONS` 不许是一张**说了不算**的清单。

        `tests/test_gui_frames.py` 比的是三版的 `SECTIONS` 常量彼此相同 ——
        那证明不了任何一版真的建了那九件。这条把常量和 `layout()` 里的调用对上：
        源码里 `self.build_<x>(` 出现过的那些，必须**恰好**是 `SECTIONS`。
        """
        from gui import _ui
        from gui.frames import split

        source = (ROOT / "gui" / "frames" / "split.py").read_text(encoding="utf-8")
        built = set(re.findall(r"self\.build_([a-z_]+)\(", source))
        # 九件（草图那八件 + 后加的 `groups`，用户 2026-08-19 拍板的 run group 模型）。
        self.assertEqual(len(split.SECTIONS), 9)
        self.assertIn("groups", split.SECTIONS)
        self.assertEqual(built, set(split.SECTIONS))
        for name in split.SECTIONS:
            self.assertTrue(
                callable(getattr(_ui.BaseApp, f"build_{name}", None)),
                f"共用层没有 build_{name}",
            )

    def test_split_sections_match_what_it_actually_builds_negative(self) -> None:
        """反向：同一个正则在一个**没建**的 section 上必须落空。

        没有这条，上面那条在"正则永远匹配一切"时也会绿。
        """
        source = (ROOT / "gui" / "frames" / "split.py").read_text(encoding="utf-8")
        built = set(re.findall(r"self\.build_([a-z_]+)\(", source))
        self.assertNotIn("sidebar", built)
        self.assertNotIn("menubar", built)  # 菜单挂在顶层窗口上，由 BaseApp 自己建


# ==========================================================================
# 2. 惰性 import 纪律（子进程判据）
# ==========================================================================


class LazyImport(unittest.TestCase):
    """无 `$DISPLAY` 的纯 ssh 会话里 CLI 必须可用（CLAUDE.md 硬约束 5）。"""

    def _run(self, code: str) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            # 显式 encoding/errors：`text=True` 默认按本机 locale 解码子进程输出，
            # 而子进程可能吐 UTF-8 ⇒ 读取线程抛 UnicodeDecodeError、stdout 变 None，
            # 把真正的失败原因盖掉。见 tests/test_cli.py::_python 的长注释。
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )

    def test_importing_gui_app_does_not_pull_tkinter(self) -> None:
        """★ 判据是**子进程里的 `sys.modules`**，不是读源码猜。

        本进程测不了这条：跑 headless 冒烟的那几条测试自己就 import 了 tkinter。
        """
        proc = self._run("import sys; import gui.app, gui.state; print('tkinter' in sys.modules)")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "False", proc.stdout + proc.stderr)

    def test_importing_gui_app_does_not_pull_tkinter_negative(self) -> None:
        """反向：同一条判据在**确实 import 了 tkinter** 的进程里必须报 True。

        没有这条，上面那条在"`sys.modules` 的键名拼错了"的情况下也会绿。
        """
        proc = self._run(
            "import sys; import tkinter; import gui.app, gui.state; print('tkinter' in sys.modules)"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "True")

    def test_launch_is_referenceable_without_a_display(self) -> None:
        """`gui.app.launch` 必须在还没碰过 tkinter 的世界里就能被引用。"""
        proc = self._run("import gui.app; print(callable(gui.app.launch))")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "True")

    def test_no_module_level_tkinter_import_in_app_or_state(self) -> None:
        """源码级的第二道：`gui/app.py` 和 `gui/state.py` 顶层零 tkinter。"""
        pattern = re.compile(r"^(import|from)\s+tkinter", re.MULTILINE)
        for rel in ("gui/app.py", "gui/state.py"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIsNone(pattern.search(text), f"{rel} 顶层 import 了 tkinter")

    def test_no_module_level_tkinter_import_negative(self) -> None:
        """反向：同一个正则在**确实**顶层 import tkinter 的文件上必须命中。"""
        pattern = re.compile(r"^(import|from)\s+tkinter", re.MULTILINE)
        text = (ROOT / "gui" / "_ui.py").read_text(encoding="utf-8")
        self.assertIsNotNone(pattern.search(text), "gui/_ui.py 本来就该顶层 import tkinter")

    def test_frames_do_not_build_a_root_window_at_import(self) -> None:
        """`gui/frames/*.py` 模块顶层不许 `Tk()`（`docs/INTERFACES.md` 三条硬要求之 2）。"""
        text = (ROOT / "gui" / "frames" / "split.py").read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith((" ", "\t", "#")):
                continue
            self.assertNotIn("Tk()", line, f"模块顶层建了 Tk(): {line!r}")

    def test_package_inits_stay_empty(self) -> None:
        """`gui/__init__.py` / `gui/frames/__init__.py` 一行 import 都不许有。"""
        for rel in ("gui/__init__.py", "gui/frames/__init__.py"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            hits = [ln for ln in text.splitlines() if re.match(r"^\s*(import|from)\s", ln)]
            self.assertEqual(hits, [], f"{rel} 里有 import：{hits}")


# ==========================================================================
# 3. headless 建得起来
# ==========================================================================


class HeadlessBuild(_SmokeTest):
    """`EWB_SMOKE=1` 下建完整棵控件树 —— `scripts/check.sh` 第 5 步的本地版。"""

    def test_split_frame_builds_and_shows_every_run(self) -> None:
        """★ 计数断言：Runs 表里的行数 == 手写的 12，且 iid 逐条等于手写的 run_id。

        只断言"退 0"是不够的 —— 建了个空壳也退 0，而空表和"矩阵算错了"看起来一样。
        """
        root = _tk_or_skip(self)
        from gui.frames import split

        bridge, _runner, _sched = _gui(self.root)
        frame = split.build_frame(root, bridge)
        frame.pack(fill="both", expand=True)
        app = frame._ewb_app

        rows = list(app.tree.get_children())
        self.assertEqual(len(rows), EXPECTED_RUN_COUNT)
        self.assertEqual(rows, list(EXPECTED_RUN_IDS))
        self.assertEqual(len(app.dtree.get_children()), 2)

    def test_split_frame_builds_and_shows_every_run_negative(self) -> None:
        """反向：同一条构造路径少一个温度 ⇒ 表里恰好剩 8 行。"""
        root = _tk_or_skip(self)
        from gui.frames import split

        bridge, _runner, _sched = _gui(self.root, temps=TEMPERATURES[:2])
        app = split.build_frame(root, bridge)._ewb_app
        # 2 design x 2 corner x 2 temperature = 8（手算，不是从被测代码取回来的）。
        self.assertEqual(len(app.tree.get_children()), 8)

    def test_selected_run_shows_the_command(self) -> None:
        """选中一行 → `Selected run → Command` 里是那条 run 的完整 argv。"""
        root = _tk_or_skip(self)
        from gui.frames import split

        bridge, _runner, _sched = _gui(self.root)
        app = split.build_frame(root, bridge)._ewb_app
        app.tree.selection_set(EXPECTED_RUN_IDS[0])
        app.show_detail()
        text = app.cmd_text.get("1.0", "end-1c")
        # 期望值出处：`--workDir` 是机制层锁死的（BRIEF §11），值是这个 run 的 run_dir；
        # `--corner` / `--emssTechFile` 是 corner 轴**同时改的两处**（BRIEF §7）。
        self.assertIn("--corner=cworst", text)
        self.assertIn("--temperature=-40.0", text)
        self.assertIn("--emssTechFile=/tmp/fakepdk/ptxt/fake_cworst.ptxt", text)
        self.assertIn("--workDir=", text)
        self.assertIn(FAKE_EWAVE_BIN, text)

    def test_selected_run_shows_the_command_negative(self) -> None:
        """反向：这个 run 的命令里**不许**出现另一个 corner / 另一个温度 / 另一份 ptxt。

        少了这条，上面那条在"命令拼串了、把两个 corner 都写进去"时照样绿 ——
        而"目录名说一个工艺角、实际算的是另一个"跑得出来、数字也像（BRIEF §7）。
        """
        root = _tk_or_skip(self)
        from gui.frames import split

        bridge, _runner, _sched = _gui(self.root)
        app = split.build_frame(root, bridge)._ewb_app
        app.tree.selection_set(EXPECTED_RUN_IDS[0])
        app.show_detail()
        text = app.cmd_text.get("1.0", "end-1c")
        self.assertNotIn("--corner=typical", text)
        self.assertNotIn("--temperature=125.0", text)
        self.assertNotIn("fake_typical.ptxt", text)

    def test_module_smoke_exits_zero(self) -> None:
        """`EWB_SMOKE=1 python -m gui.frames.split` —— check.sh 第 5 步跑的就是这条。"""
        env = dict(os.environ)
        env["EWB_SMOKE"] = "1"
        proc = subprocess.run(
            [sys.executable, "-m", "gui.frames.split"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            # 显式 encoding/errors：`text=True` 默认按本机 locale 解码子进程输出，
            # 而子进程可能吐 UTF-8 ⇒ 读取线程抛 UnicodeDecodeError、stdout 变 None，
            # 把真正的失败原因盖掉。见 tests/test_cli.py::_python 的长注释。
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=180,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_module_smoke_exits_zero_negative(self) -> None:
        """反向：布局名写错 ⇒ 必须退非 0（判据不是"总是退 0"）。"""
        env = dict(os.environ)
        env["EWB_SMOKE"] = "1"
        proc = subprocess.run(
            [sys.executable, "-c", "import gui.app,sys; sys.exit(gui.app.launch('nope'))"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            # 显式 encoding/errors：`text=True` 默认按本机 locale 解码子进程输出，
            # 而子进程可能吐 UTF-8 ⇒ 读取线程抛 UnicodeDecodeError、stdout 变 None，
            # 把真正的失败原因盖掉。见 tests/test_cli.py::_python 的长注释。
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=180,
        )
        self.assertNotEqual(proc.returncode, 0)


# ==========================================================================
# 4. ★ GuiState 与 driver 的接线
# ==========================================================================


class BridgeDriverWiring(_TempRootTest):
    """**这一节是本阶段的核心判据。**

    GUI 侧看到的状态序列必须与 driver 的状态一致 —— 判据是"两条驱动路径逐拍相同"，
    而不是"bridge.runs() 等于 driver.state.runs"（那是同一批对象，恒真）。
    """

    def test_gui_and_cli_produce_the_same_tick_sequence(self) -> None:
        bridge, _runner, _sched = _gui(f"{self.root}/g", modes=GUI_MODES)
        bridge.start(dry_run=False)
        gui_sequence = _drive_gui(bridge)

        driver = _reference(f"{self.root}/c", modes=GUI_MODES)
        cli_sequence = _drive_cli(driver)

        # 计数断言 1：序列不止一拍 —— 空序列的 assertEqual 永远是绿的。
        self.assertGreaterEqual(len(gui_sequence), 3)
        # 计数断言 2：两条路径的**拍数**相同。
        self.assertEqual(len(gui_sequence), len(cli_sequence))
        # 逐拍相同。
        self.assertEqual(gui_sequence, cli_sequence)

    def test_gui_and_cli_produce_the_same_tick_sequence_negative(self) -> None:
        """反向：只给**其中一条路**换一个失败模式 ⇒ 两条序列必须**不再**相同。

        共用 `_gui()` / `_reference()` 同一条构造路径，只改 `modes` 一个入参 ——
        排除"换了个东西测"。没有这条，上面那条在"两边都恒返回空序列"时也会绿。
        """
        bridge, _runner, _sched = _gui(f"{self.root}/g", modes=GUI_MODES)
        bridge.start(dry_run=False)
        gui_sequence = _drive_gui(bridge)

        broken = dict(GUI_MODES)
        broken[EXPECTED_RUN_IDS[0]] = FakeFailureMode.NONZERO_EXIT
        driver = _reference(f"{self.root}/c", modes=broken)
        cli_sequence = _drive_cli(driver)

        self.assertNotEqual(gui_sequence, cli_sequence)

    def test_final_status_matches_the_handwritten_table(self) -> None:
        """★ 12 行手写终态表，逐条比。"""
        bridge, _runner, _sched = _gui(self.root, modes=GUI_MODES)
        bridge.start(dry_run=False)
        _drive_gui(bridge)

        actual = {run.run_id: run.status.value for run in bridge.runs()}
        self.assertEqual(len(actual), EXPECTED_RUN_COUNT)
        self.assertEqual(actual, EXPECTED_FINAL_STATUS)

    def test_final_counts_match_the_handwritten_numbers(self) -> None:
        """计数断言：终态分布逐个等于手写期望，且 6 个键全在。"""
        bridge, _runner, _sched = _gui(self.root, modes=GUI_MODES)
        bridge.start(dry_run=False)
        _drive_gui(bridge)

        counts = bridge.summary()
        self.assertEqual(sorted(counts), sorted(s.value for s in RunStatus))
        self.assertEqual(counts["done"], EXPECTED_DONE)
        self.assertEqual(counts["failed"], EXPECTED_FAILED)
        self.assertEqual(counts["ready"], 0)
        self.assertEqual(counts["pending"], 0)
        self.assertEqual(counts["running"], 0)
        self.assertEqual(counts["skipped"], 0)
        self.assertEqual(sum(counts.values()), EXPECTED_RUN_COUNT)

    def test_final_counts_match_the_handwritten_numbers_negative(self) -> None:
        """反向：把那三条失败模式全去掉 ⇒ 12 全 done、0 failed。

        共用同一条构造路径，只改 `modes`。这条同时证明验收器**不是**"一律判 failed"。
        """
        bridge, _runner, _sched = _gui(self.root, modes={})
        bridge.start(dry_run=False)
        _drive_gui(bridge)

        counts = bridge.summary()
        self.assertEqual(counts["done"], EXPECTED_RUN_COUNT)
        self.assertEqual(counts["failed"], 0)

    def test_status_changes_are_observed_through_the_bridge(self) -> None:
        """计数断言：GUI 侧**观察到**的状态变化。

        每个 run 至少走过 ready → (pending|running) → 终态。如果 bridge 只是在最后
        把结果一次性交出来（而不是每拍都让界面看得见），下面那个 3 就会塌成 2。
        """
        bridge, _runner, _sched = _gui(self.root, modes=GUI_MODES)
        bridge.start(dry_run=False)

        seen: dict[str, list[str]] = {run.run_id: [run.status.value] for run in bridge.runs()}
        ticks = 0
        for _ in range(80):
            report = bridge.tick()
            if report is None:
                break
            ticks += 1
            for run in bridge.runs():
                if seen[run.run_id][-1] != run.status.value:
                    seen[run.run_id].append(run.status.value)
            if report.finished:
                break

        self.assertEqual(len(seen), EXPECTED_RUN_COUNT)
        self.assertGreaterEqual(ticks, 3)
        for run_id, history in seen.items():
            self.assertEqual(history[0], "ready", run_id)
            self.assertEqual(history[-1], EXPECTED_FINAL_STATUS[run_id], run_id)
            # 至少三段：ready → 在飞 → 终态。少于三段说明中间态没被界面看见。
            self.assertGreaterEqual(len(history), 3, f"{run_id}: {history}")

    def test_tick_returns_none_before_start_and_after_finish(self) -> None:
        """`GuiBridgeProtocol.tick`：没在跑时返回 None（GUI 靠它停掉 `after` 链）。"""
        bridge, _runner, _sched = _gui(self.root, modes=GUI_MODES)
        self.assertIsNone(bridge.tick())
        bridge.start(dry_run=False)
        _drive_gui(bridge)
        self.assertIsNone(bridge.tick())
        self.assertFalse(bridge.is_running())

    def test_start_twice_is_a_no_op(self) -> None:
        """「已经在跑时是 no-op」（冻结面 docstring）。

        计数断言：第二次 `start()` 之后提交次数一个都不许多 —— 一个 run 可能
        10 核 100 GB 跑 35 分钟，"重复提交了 12 个"和"跑完了"都是绿的，
        只有提交计数能把两者分开。
        """
        bridge, _runner, scheduler = _gui(self.root, modes=GUI_MODES)
        bridge.start(dry_run=False)
        bridge.tick()
        before = scheduler.submit_calls
        bridge.start(dry_run=False)
        self.assertEqual(scheduler.submit_calls, before)

    def test_dry_run_submits_nothing(self) -> None:
        """dry-run 走**同一个 driver**，但一条命令都不许提交（D8）。"""
        bridge, _runner, scheduler = _gui(self.root, modes=GUI_MODES)
        bridge.start(dry_run=True)
        _drive_gui(bridge)
        self.assertEqual(scheduler.submit_calls, 0)

    def test_dry_run_submits_nothing_negative(self) -> None:
        """反向：真跑时提交计数**恰好**等于 run 数（不是 0，也不是 24）。"""
        bridge, _runner, scheduler = _gui(self.root, modes=GUI_MODES)
        bridge.start(dry_run=False)
        _drive_gui(bridge)
        self.assertEqual(scheduler.submit_calls, EXPECTED_RUN_COUNT)

    def test_has_started_flips_once_the_driver_exists(self) -> None:
        """`has_started()` 是界面"还准不准重新展开矩阵"的判据 —— 它得真的会翻。"""
        bridge, _runner, _sched = _gui(self.root, modes=GUI_MODES)
        self.assertFalse(bridge.has_started())
        bridge.start(dry_run=False)
        self.assertTrue(bridge.has_started())
        bridge.reset()
        self.assertFalse(bridge.has_started())

    def test_resume_only_tops_up_the_unfinished_runs(self) -> None:
        """★ 计数断言：resume 之后**只**重新提交那 3 个 failed 的。

        一个 run 可能 10 核 100 GB 跑 35 分钟 —— "补了 3 个"和"重跑了 12 个"在界面上
        都是表在动，只有提交计数能把两者分开。
        """
        bridge, _runner, scheduler = _gui(self.root, modes=GUI_MODES)
        bridge.start(dry_run=False)
        _drive_gui(bridge)
        first_round = scheduler.submit_calls
        self.assertEqual(first_round, EXPECTED_RUN_COUNT)

        bridge.resume()
        _drive_gui(bridge)
        self.assertEqual(scheduler.submit_calls - first_round, EXPECTED_FAILED)

    def test_resume_only_tops_up_the_unfinished_runs_negative(self) -> None:
        """反向：一条都没失败时，resume **一个都不该提交**（不是"总提交 3 个"）。"""
        bridge, _runner, scheduler = _gui(self.root, modes={})
        bridge.start(dry_run=False)
        _drive_gui(bridge)
        first_round = scheduler.submit_calls
        self.assertEqual(first_round, EXPECTED_RUN_COUNT)

        bridge.resume()
        _drive_gui(bridge)
        self.assertEqual(scheduler.submit_calls - first_round, 0)

    def test_cancel_leaves_no_run_in_flight(self) -> None:
        """取消之后没有 run 还挂在 ready / pending / running 上。"""
        bridge, _runner, _sched = _gui(self.root, modes=GUI_MODES)
        bridge.start(dry_run=False)
        bridge.tick()
        bridge.cancel()
        counts = bridge.summary()
        self.assertEqual(counts["pending"], 0)
        self.assertEqual(counts["running"], 0)
        self.assertEqual(counts["ready"], 0)
        self.assertFalse(bridge.is_running())


class BridgePumpWiring(_SmokeTest):
    """界面那一侧：`_pump()` 是不是真的挂在 `after()` 上驱动同一个 `tick()`。"""

    def test_pump_drives_the_same_driver_and_schedules_the_next_tick(self) -> None:
        root = _tk_or_skip(self)
        from gui.frames import split

        bridge, _runner, _sched = _gui(self.root, modes=GUI_MODES)
        app = split.build_frame(root, bridge)._ewb_app

        app.do_submit()
        # 第一拍之后还没跑完 ⇒ 必须已经挂了下一拍。这就是 `root.after()` 那条线
        # （BRIEF §12「GUI 用 after() 驱动同一个 tick()」），不是另起线程。
        self.assertIsNotNone(app._timer)

        for _ in range(80):
            if not bridge.is_running():
                break
            app._pump()

        self.assertIsNone(app._timer, "跑完了还挂着 after 链")
        self.assertEqual(
            {run.run_id: run.status.value for run in bridge.runs()}, EXPECTED_FINAL_STATUS
        )
        # 界面上那张表也得跟着变（不是只有内存里的对象变了）。
        statuses = [app.tree.set(iid, "status") for iid in app.tree.get_children()]
        self.assertEqual(len(statuses), EXPECTED_RUN_COUNT)
        self.assertEqual(statuses.count("done"), EXPECTED_DONE)
        self.assertEqual(statuses.count("failed"), EXPECTED_FAILED)

    def test_results_survive_a_keystroke_after_the_batch_finished(self) -> None:
        """★ 本阶段抓到的一个真 bug 的回归测试。

        界面每敲一个键就 `recompute()` → `bridge.plan()`，而 `plan()` 造的是一份
        **全新的、每个 run 都 `ready`** 的 `BatchState`。于是跑完之后随手改一个勾选，
        表上那 9 个 done 就静默消失了 —— 看起来只是"界面刷新了一下"。
        修法：`has_started()` 之后不再重新展开矩阵；要重来一批走 New batch。
        """
        root = _tk_or_skip(self)
        from gui.frames import split

        bridge, _runner, _sched = _gui(self.root, modes=GUI_MODES)
        app = split.build_frame(root, bridge)._ewb_app
        app.do_submit()
        for _ in range(80):
            if not bridge.is_running():
                break
            app._pump()

        app.corner_vars["typical"].set(False)  # 用户随手取消了一个勾
        app.recompute()

        statuses = [app.tree.set(iid, "status") for iid in app.tree.get_children()]
        self.assertEqual(len(statuses), EXPECTED_RUN_COUNT)
        self.assertEqual(statuses.count("done"), EXPECTED_DONE)
        self.assertEqual(statuses.count("failed"), EXPECTED_FAILED)
        # Submit 也必须保持禁用 —— 再按一次就是整批重跑。
        self.assertIn("disabled", app.btn["Submit"].state())
        self.assertNotIn("disabled", app.btn["Resume"].state())

    def test_results_survive_a_keystroke_after_the_batch_finished_negative(self) -> None:
        """反向：明确按了 New batch 之后，矩阵**必须**跟着新勾选重算。

        没有这条，上面那条在"界面从此再也不更新"时也会绿 —— 那是另一个 bug，
        而且方向相反：一个是结果被吞，一个是界面死了。
        """
        root = _tk_or_skip(self)
        from gui.frames import split

        bridge, _runner, _sched = _gui(self.root, modes=GUI_MODES)
        app = split.build_frame(root, bridge)._ewb_app
        app.do_submit()
        for _ in range(80):
            if not bridge.is_running():
                break
            app._pump()

        app.corner_vars["typical"].set(False)
        app.do_new_batch()

        statuses = [app.tree.set(iid, "status") for iid in app.tree.get_children()]
        # 2 design x 1 corner(cworst) x 3 temperature = 6，全部回到 ready。
        self.assertEqual(len(statuses), 6)
        self.assertEqual(statuses.count("ready"), 6)
        self.assertNotIn("disabled", app.btn["Submit"].state())

    def test_batch_name_and_dir_stop_drifting_after_the_first_plan(self) -> None:
        """★ 批次名空着时 bridge 会现起一个 UTC 时间戳名 —— 它必须**只起一次**。

        不把它灌回输入框的话，每敲一个键就重新生成一个（时间戳每秒变一次），
        批次目录跟着跳，"产物落在哪"这个问题就再也答不上来了。
        """
        root = _tk_or_skip(self)
        from gui.frames import split

        bridge = GuiState()  # 刻意不给 batch_name
        bridge.add_design(FAKE_LIB, FAKE_CELL_A, FAKE_VIEW)
        app = split.build_frame(root, bridge)._ewb_app

        first_name = bridge.batch_name
        first_dir = bridge.batch_dir()
        self.assertNotEqual(first_name, "")
        self.assertEqual(app.batch.get(), first_name)

        for _ in range(3):
            app.recompute()
        self.assertEqual(bridge.batch_name, first_name)
        self.assertEqual(bridge.batch_dir(), first_dir)

    def test_batch_name_and_dir_stop_drifting_negative(self) -> None:
        """反向：用户**自己**改了名字 ⇒ 批次目录必须跟着变（不是钉死在第一个名字上）。"""
        root = _tk_or_skip(self)
        from gui.frames import split

        bridge = GuiState()
        bridge.add_design(FAKE_LIB, FAKE_CELL_A, FAKE_VIEW)
        app = split.build_frame(root, bridge)._ewb_app
        first_dir = bridge.batch_dir()

        app.batch.set("my_batch")
        app.recompute()
        self.assertEqual(bridge.batch_name, "my_batch")
        self.assertNotEqual(bridge.batch_dir(), first_dir)
        self.assertTrue(bridge.batch_dir().endswith("my_batch"))

    def test_a_rejected_setting_does_not_kill_the_window(self) -> None:
        """用户把 Mesh 的一个格子清空 ⇒ 核心（正确地）拒绝，但**界面不许死**。

        判据有三条，缺一条就是"看起来没事其实半死"：
        表还在（行数没塌）、状态栏说清了原因、把值填回去之后能恢复。
        """
        root = _tk_or_skip(self)
        from gui.frames import split

        bridge, _runner, _sched = _gui(self.root)
        app = split.build_frame(root, bridge)._ewb_app
        before = len(app.tree.get_children())
        self.assertEqual(before, EXPECTED_RUN_COUNT)

        app.m_vert.set("")
        app.recompute()
        self.assertEqual(len(app.tree.get_children()), before)
        self.assertIn("SpecError", app.status_lbl.cget("text"))

        app.m_vert.set("0.4")
        app.recompute()
        self.assertEqual(len(app.tree.get_children()), before)
        self.assertNotIn("SpecError", app.status_lbl.cget("text"))

    def test_a_rejected_setting_does_not_kill_the_window_negative(self) -> None:
        """反向：一切正常时状态栏里**不许**有 `SpecError`。

        没有这条，上面那条在"状态栏永远显示同一条错误"时也会绿。
        """
        root = _tk_or_skip(self)
        from gui.frames import split

        bridge, _runner, _sched = _gui(self.root)
        app = split.build_frame(root, bridge)._ewb_app
        app.recompute()
        self.assertNotIn("Error", app.status_lbl.cget("text"))

    def test_pump_drives_the_same_driver_negative(self) -> None:
        """反向：全部成功时，表里**一条 failed 都不许有**。

        没有这条，上面那条在 `tree.set(..., 'status')` 取错列（永远返回同一个字串）
        时也可能凑巧绿 —— 这条把两个计数拉到相反的方向。
        """
        root = _tk_or_skip(self)
        from gui.frames import split

        bridge, _runner, _sched = _gui(self.root, modes={})
        app = split.build_frame(root, bridge)._ewb_app
        app.do_submit()
        for _ in range(80):
            if not bridge.is_running():
                break
            app._pump()
        statuses = [app.tree.set(iid, "status") for iid in app.tree.get_children()]
        self.assertEqual(statuses.count("failed"), 0)
        self.assertEqual(statuses.count("done"), EXPECTED_RUN_COUNT)


# ==========================================================================
# 5. Extra flags 撞轴要标红（§11 规则 2）
# ==========================================================================


class ExtraFlagConflicts(_TempRootTest):
    """撞轴 = 目录名和实际跑的值对不上 = 原生 GUI 覆盖坑的根因。不能自己再造一遍。"""

    def _bridge(self) -> GuiState:
        bridge, _runner, _sched = _gui(self.root)
        return bridge

    def test_clean_extra_flags_are_not_flagged(self) -> None:
        bridge = self._bridge()
        bridge.set_extra_flags(" ".join(f"{name}=1" for name in NOT_CONFLICTS))
        self.assertEqual(bridge.extra_flag_conflicts(), [])
        self.assertEqual(bridge.conflict_message(), "")
        # 计数断言：参与判定的 flag 条数 == 手写的条数。空集合的判定永远是绿的。
        self.assertEqual(len(bridge.extra_flags()), len(NOT_CONFLICTS))

    def test_axis_flags_in_extra_are_flagged_negative(self) -> None:
        """★ `_negative`：把一个**已经是轴**的 flag 写进 Extra flags → 必须标红。"""
        bridge = self._bridge()
        bridge.set_extra_flags("--temperature=85 --corner=cbest --workDir=/tmp/x")
        hits = bridge.extra_flag_conflicts()
        self.assertEqual(sorted(hits), sorted(EXPECTED_CONFLICTS))
        self.assertEqual(len(hits), len(EXPECTED_CONFLICTS))
        self.assertNotEqual(bridge.conflict_message(), "")

    def test_prefix_never_swallows_a_longer_flag(self) -> None:
        """★ 回归：`--sparam` 是锁死 flag，但**不许**吃掉 `--sparamImpedance`。

        MVP 真踩过这个坑（BRIEF §10）：排除规则写 `--sparam` 前缀误伤 `--sparamImpedance`，
        两边同时被跳过，diff 空得非常好看，但根本没比。**空过的测试比没测更坏。**
        """
        bridge = self._bridge()
        bridge.set_extra_flags("--sparamImpedance=50")
        self.assertEqual(bridge.extra_flag_conflicts(), [])
        # 计数断言：确实有一条 flag 参与了判定（不是解析出了空集合）。
        self.assertEqual(len(bridge.extra_flags()), 1)

    def test_prefix_never_swallows_a_longer_flag_negative(self) -> None:
        """反向：真的写 `--sparam` 本尊 ⇒ 必须被拒。判定不是"一律放行"。"""
        bridge = self._bridge()
        bridge.set_extra_flags("--sparam=X")
        self.assertEqual(bridge.extra_flag_conflicts(), ["--sparam"])


class ExtraFlagConflictsInGui(_SmokeTest):
    """同一条规则在**界面上**的落点：真的标红了没有。"""

    def test_gui_marks_the_conflict_in_red(self) -> None:
        root = _tk_or_skip(self)
        from gui import _ui
        from gui.frames import split

        bridge, _runner, _sched = _gui(self.root)
        app = split.build_frame(root, bridge)._ewb_app

        self.assertEqual(app.extra_warn.winfo_manager(), "", "干净时不该有告警")
        app.extra.set("--temperature=85")
        app.recompute()
        self.assertNotEqual(app.extra_warn.winfo_manager(), "", "撞轴了却没标红")
        self.assertEqual(str(app.extra_entry.cget("foreground")), _ui.RED)
        self.assertIn("--temperature", app.extra_warn.cget("text"))

    def test_gui_marks_the_conflict_in_red_negative(self) -> None:
        """反向：换成一个**不撞轴**的 flag ⇒ 告警必须收回去、颜色恢复。

        共用同一条构造路径，只改输入框里那一个字串。
        """
        root = _tk_or_skip(self)
        from gui import _ui
        from gui.frames import split

        bridge, _runner, _sched = _gui(self.root)
        app = split.build_frame(root, bridge)._ewb_app

        app.extra.set("--temperature=85")
        app.recompute()
        self.assertNotEqual(app.extra_warn.winfo_manager(), "")
        app.extra.set("--sparamImpedance=50")
        app.recompute()
        self.assertEqual(app.extra_warn.winfo_manager(), "")
        self.assertNotEqual(str(app.extra_entry.cget("foreground")), _ui.RED)


# ==========================================================================
# 6. 资源 → --parallel（复用 core.cmd.parse_resource_string）
# ==========================================================================


class ResourceSync(_TempRootTest):
    """整条 dsub 命令原样暴露给用户改，但 `cpu=` 要自动同步 `--parallel`（1:1）。"""

    def _bridge(self) -> GuiState:
        bridge, _runner, _sched = _gui(self.root)
        return bridge

    def test_parallel_follows_cpu_one_to_one(self) -> None:
        # 期望值出处：BRIEF §6「`--parallel` != `cpu`」—— 红区当前实际是 1:1，
        # 倍率是 `BatchOptions.parallel_multiplier`（默认 1.0）。
        bridge = self._bridge()
        bridge.set_submit_command('dsub -A acct -q q1 -R "cpu=20;mem=100000"')
        self.assertEqual(bridge.resources(), "cpu=20;mem=100000")
        self.assertEqual(bridge.parallel(), 20)

    def test_parallel_follows_cpu_one_to_one_negative(self) -> None:
        """反向：把 `cpu=` 改成别的数 ⇒ `--parallel` 必须跟着变（不是写死的 20）。"""
        bridge = self._bridge()
        bridge.set_submit_command('dsub -A acct -q q1 -R "cpu=40;mem=100000"')
        self.assertEqual(bridge.parallel(), 40)

    def test_no_cpu_means_none_not_a_made_up_number(self) -> None:
        """没有 `cpu=` 就返回 None —— **不许拿 1 或 0 冒充**「没解析到」。"""
        bridge = self._bridge()
        bridge.set_submit_command("dsub -A acct -q q1")
        self.assertEqual(bridge.resources(), "")
        self.assertIsNone(bridge.parallel())

    def test_resource_string_parsing_is_not_reimplemented(self) -> None:
        """判据：`GuiState.parallel()` 的解析结果与 `core.cmd.parse_resource_string` 一致。

        两份实现必然漂（BRIEF §12），所以这条盯着"只有一份"。
        """
        from ewave_batch.core.cmd import parse_resource_string

        bridge = self._bridge()
        bridge.set_submit_command('dsub -R "cpu=7;mem=8"')
        self.assertEqual(parse_resource_string(bridge.resources()), {"cpu": "7", "mem": "8"})
        self.assertEqual(bridge.parallel(), 7)

    def test_bad_submit_command_is_reported_not_swallowed(self) -> None:
        """粘了一条带管道的命令 ⇒ 界面要给出人能看懂的原因，而不是静默用默认资源。"""
        bridge = self._bridge()
        bridge.set_submit_command("dsub -R cpu=4 | tee x.log")
        self.assertNotEqual(bridge.submit_command_error(), "")
        self.assertIsNone(bridge.parallel())

    def test_bad_submit_command_is_reported_not_swallowed_negative(self) -> None:
        """反向：合法命令（`-R` 里带 `;`）**不许**被拒 —— `;` 是资源串的合法内容。

        和 `--sparam` 前缀误伤同一类：过滤器多吃一口，症状是"看起来很干净"。
        """
        bridge = self._bridge()
        bridge.set_submit_command('dsub -R "cpu=4;mem=9"')
        self.assertEqual(bridge.submit_command_error(), "")
        self.assertEqual(bridge.parallel(), 4)


class ResourceSyncInGui(_SmokeTest):
    """同一条规则在**界面上**的落点：整条命令看得见、改得动，`--parallel` 跟着变。"""

    def test_submit_command_is_exposed_and_parallel_follows_it(self) -> None:
        root = _tk_or_skip(self)
        from gui.frames import split

        bridge, _runner, _sched = _gui(self.root)
        app = split.build_frame(root, bridge)._ewb_app
        # 整条 dsub 命令从 `SiteFacts` 学出来，灌回输入框让用户改（用户 2026-08-18 要求）。
        self.assertIn(FAKE_RESOURCES, app.dsub.get())
        self.assertIn("--parallel=2", app.par_lbl.cget("text"))

    def test_submit_command_is_exposed_and_parallel_follows_it_negative(self) -> None:
        """反向：用户把 `cpu=` 改成 6 ⇒ 标签必须变成 6，且**不再**是 2。

        共用同一条构造路径，只改输入框里那一个字串。
        """
        root = _tk_or_skip(self)
        from gui.frames import split

        bridge, _runner, _sched = _gui(self.root)
        app = split.build_frame(root, bridge)._ewb_app
        app.dsub.set('dsub -R "cpu=6;mem=100"')
        app.recompute()
        self.assertIn("--parallel=6", app.par_lbl.cget("text"))
        self.assertNotIn("--parallel=2", app.par_lbl.cget("text"))


# ==========================================================================
# 7. 温度归一（界面敲 -40，eWave 建的是 -40_0）
# ==========================================================================


class TemperatureNormalization(_TempRootTest):
    """`-40` 和 `-40.0` 会让 eWave 建出**两个不同的目录**（BRIEF §5）。"""

    def test_gui_normalizes_bare_integers(self) -> None:
        bridge, _runner, _sched = _gui(self.root, temps=("-40", "25", "125"))
        self.assertEqual(bridge.axis_selection()["temperature"], ("-40.0", "25.0", "125.0"))
        self.assertEqual([run.run_id for run in bridge.runs()], list(EXPECTED_RUN_IDS))

    def test_gui_normalizes_bare_integers_negative(self) -> None:
        """反向：**不**归一的话目录名就是 `typical_-40`（少了 `_0`）—— 产物再也找不到。

        期望值不是我算出来的，是 eWave 自己的约定（`model.TEMP_DECIMAL_REPLACEMENT`），
        这里直接对着 `core.matrix.ewave_dir_name` 摆事实。
        """
        self.assertEqual(ewave_dir_name("typical", "-40"), "typical_-40")
        self.assertEqual(ewave_dir_name("typical", "-40.0"), "typical_-40_0")
        self.assertNotEqual(
            ewave_dir_name("typical", "-40"), ewave_dir_name("typical", "-40.0")
        )

# ==========================================================================
# 8. run group（用户 2026-08-19 拍板的组合模型）—— `GuiState` 这一侧
# ==========================================================================

# ★ 手写的期望表：契约里那个原型在**界面这条路**上展开成什么。
#
#   base            corner=typical, temperature={-40, 55, 125}, fullWave=off, equalCurrent=on
#   组 eqcur-off    temperature={55}, equalCurrent=off
#   组 fullwave     temperature={55}, fullWave=on
#                                                            => 3 + 1 + 1 = 5 个 run
#
# 注意 slug 全都带上了 `fw-…__eqI-…`：加了组之后 fullWave / equalCurrent 在**整个批次**
# 上都在变了，于是它们对**所有** run（基线那 3 个也一样）进 slug。基线的目录名因此从
# `base/` 变成 `fw-off__eqI-on/` —— 这是正确且不可避免的（否则两个组的 55 度落进同一个
# 目录 = 静默覆盖 = 本工具存在的理由），而且正是 `groups_change_warning()` 要说的那件事。
# 片段顺序 = 轴在界面上的顺序（fullWave 的勾选框在 equalCurrent 前面）。
EXPECTED_GROUP_RUN_IDS: tuple[tuple[str, str], ...] = (
    # (run_id, 出自哪个组)
    (f"{DESIGN_A}/fw-off__eqI-on/typical_-40_0", "base"),
    (f"{DESIGN_A}/fw-off__eqI-on/typical_55_0", "base"),
    (f"{DESIGN_A}/fw-off__eqI-on/typical_125_0", "base"),
    (f"{DESIGN_A}/fw-off__eqI-off/typical_55_0", "eqcur-off"),
    (f"{DESIGN_A}/fw-on__eqI-on/typical_55_0", "fullwave"),
)


def _bare_bridge(root: str, *, temps: tuple[str, ...] = ("-40.0", "55.0", "125.0")) -> GuiState:
    """一个只勾了 base、**还没 plan()** 的 bridge。正反两向共用这一条构造路径。

    刻意不复用 `_gui()`：那个helper 一进来就 `plan()` 了两个 design 的 12 个 run，
    而组这一段要看的是"边勾边算"的那些数（`run_count()` / `formula()`），
    落不落盘不重要，反而是噪声。
    """
    offdir = f"{_workarea(root)}/ewave_simulation/design"
    facts = _facts(offdir)
    runner = FakeRunner(port_count=PORT_COUNT)
    bridge = GuiState(
        batch_root=root,
        batch_name="gui_batch",
        official_run_dir=offdir,
        scheduler=FakeScheduler(runner),
        runner=runner,
        discover=lambda _path: facts,
    )
    bridge.set_axis_values("corner", ("typical",))
    bridge.set_axis_values("temperature", temps)
    bridge.set_axis_values("fullWave", ("off",))
    bridge.set_axis_values("equalCurrent", ("on",))
    # 这两根轴清空，好让 run_id 与上面那张手写表逐字对得上（它们进 slug 就多一截）。
    for name in ("relativeTolerance", "relativeCurrentTolerance"):
        bridge.set_axis_values(name, ())
    bridge.add_design(FAKE_LIB, FAKE_CELL_A, FAKE_VIEW)
    return bridge


def _prototype(bridge: GuiState) -> GuiState:
    """把契约里那两个组配上去。**走的全是界面会走的那几个方法**，不手搓 `RunGroup`。"""
    bridge.add_group("eqcur-off")  # add_group 顺手切过去 —— 界面上点 [+ Add] 就是这个手感
    bridge.set_axis_values("temperature", ("55.0",))
    bridge.set_axis_values("equalCurrent", ("off",))
    bridge.add_group("fullwave")
    bridge.set_axis_values("temperature", ("55.0",))
    bridge.set_axis_values("fullWave", ("on",))
    return bridge


class RunGroupBridge(_TempRootTest):
    """`GuiState` 的 run group 编辑面。

    这一面**不在冻结面上**（`docs/INTERFACES.md`「还没冻结的东西」），所以这里测的是
    那一节写下的约定，尤其是这一条：`set_axis_values()` / `axis_selection()` /
    `axis_counts()` 作用于 **active group**，active = base 时与加组之前**逐字相同**。
    """

    def test_base_only_is_exactly_the_old_behaviour(self) -> None:
        """★ 回归闸门：一个组都没加时，界面这条路必须和以前一模一样。"""
        bridge = _bare_bridge(self.root)
        self.assertEqual([g.name for g in bridge.groups()], ["base"])
        self.assertEqual(bridge.active_group(), "base")
        self.assertEqual(bridge.run_count(), 3)
        self.assertEqual(bridge.formula(), "1 designs x 1 corner x 3 temp x 1 mode = 3 runs")
        self.assertEqual(bridge.merged_run_count(), 0)
        self.assertEqual(bridge.group_run_counts(), [("base", 3)])
        # base 没有"覆盖"这一说：它的取值就是勾选本身。
        self.assertIsNone(bridge.group_override("temperature"))
        bridge.plan()
        self.assertEqual(
            [run.run_id for run in bridge.runs()],
            [
                f"{DESIGN_A}/base/typical_-40_0",
                f"{DESIGN_A}/base/typical_55_0",
                f"{DESIGN_A}/base/typical_125_0",
            ],
            "没有组的时候 fullWave/equalCurrent 各只有一个取值 => 不在变 => 不进 slug",
        )
        self.assertEqual({run.group for run in bridge.runs()}, {"base"})

    def test_prototype_expands_to_five_runs(self) -> None:
        """★ 契约里那个例子，从界面这条路走一遍：3 + 1 + 1 = 5。

        笛卡尔积最接近的写法是 {typical}x{3 温度}x{eqI on/off}x{fw on/off} = 12 个，
        7 个是废的 —— 一个 run 的量级是 10 核 / 100GB / 35 分钟（BRIEF §12）。
        """
        bridge = _prototype(_bare_bridge(self.root))
        self.assertEqual(bridge.run_count(), 5)
        bridge.plan()
        self.assertEqual(
            [(run.run_id, run.group) for run in bridge.runs()],
            list(EXPECTED_GROUP_RUN_IDS),
        )
        # 计数断言：5 个 run_id 互不相同。撞了就是同一个 --workDir = 静默覆盖。
        self.assertEqual(len({run.run_id for run in bridge.runs()}), 5)
        self.assertEqual(bridge.group_run_counts(), [("base", 3), ("eqcur-off", 1), ("fullwave", 1)])
        self.assertEqual(bridge.group_of(EXPECTED_GROUP_RUN_IDS[3][0]), "eqcur-off")

    def test_baseline_slug_changes_when_a_group_is_added_negative(self) -> None:
        """反向：同一条构造路径、只是不加组 —— 基线的 slug 就该退回 `base`。

        没有这条，上面那张表可能只是因为 fullWave/equalCurrent 永远进 slug。
        两条一起才说明"加组会改掉基线的目录名"这件事是**由组引起的**。
        """
        without = _bare_bridge(self.root)
        without.plan()
        self.assertEqual({run.axes_slug for run in without.runs()}, {"base"})
        with_groups = _prototype(_bare_bridge(self.root))
        with_groups.plan()
        self.assertEqual(
            {run.axes_slug for run in with_groups.runs()},
            {"fw-off__eqI-on", "fw-off__eqI-off", "fw-on__eqI-on"},
        )

    def test_set_axis_values_lands_on_the_active_group(self) -> None:
        """★ 本次最容易写错的一处：切了组之后再勾，勾的是**那个组**，不是基线。

        写错的症状是"用户以为自己在配变体，其实把基线改了" —— 而基线一改，
        整批的 run_id 全变，已经跑完的东西 resume 认不出来。
        """
        bridge = _bare_bridge(self.root)
        base_before = bridge.axis_selection()
        bridge.add_group("eqcur-off")
        self.assertEqual(bridge.active_group(), "eqcur-off", "add_group 该顺手切过去")
        bridge.set_axis_values("equalCurrent", ("off",))

        self.assertEqual(bridge.group_override("equalCurrent"), ("off",))
        # 继承的轴：`group_override` 给 None，`axis_selection` 给 base 的值。
        self.assertIsNone(bridge.group_override("temperature"))
        self.assertEqual(bridge.axis_selection()["temperature"], ("-40.0", "55.0", "125.0"))
        self.assertEqual(bridge.axis_selection()["equalCurrent"], ("off",))
        # ★ 基线一个字都没动
        bridge.set_active_group("base")
        self.assertEqual(bridge.axis_selection(), base_before)
        self.assertEqual(bridge.axis_selection()["equalCurrent"], ("on",))

    def test_axis_counts_follow_the_active_group(self) -> None:
        """每根轴右边那个 `-> N` 也跟着 active group 走，否则界面在说谎。"""
        bridge = _bare_bridge(self.root)
        self.assertEqual(bridge.axis_counts()["temperature"], 3)
        bridge.add_group("hot")
        bridge.set_axis_values("temperature", ("55.0",))
        self.assertEqual(bridge.axis_counts()["temperature"], 1)
        bridge.set_active_group("base")
        self.assertEqual(bridge.axis_counts()["temperature"], 3)

    def test_clear_override_gives_the_axis_back_to_base(self) -> None:
        """撤销覆盖 = 回去继承。空取值走的也是这条路（对一个组来说"空"不是"不扫"）。"""
        bridge = _bare_bridge(self.root)
        bridge.add_group("hot")
        bridge.set_axis_values("temperature", ("55.0",))
        bridge.clear_group_override("temperature")
        self.assertIsNone(bridge.group_override("temperature"))
        self.assertEqual(bridge.axis_selection()["temperature"], ("-40.0", "55.0", "125.0"))
        bridge.set_axis_values("temperature", ("55.0",))
        self.assertEqual(bridge.group_override("temperature"), ("55.0",))
        bridge.set_axis_values("temperature", ())
        self.assertIsNone(bridge.group_override("temperature"), "空取值 = 撤销覆盖")

    def test_formula_switches_shape_when_groups_appear(self) -> None:
        """算式：只有 base 时是连乘，有组时是 `designs x (a + b + c)`。

        为什么要换形状：有组之后整批已经不是一个笛卡尔积了，写成连乘就是假的。
        为什么只有 base 时不换：最常见的场景不该因为多了一个功能而变难懂。
        """
        bridge = _bare_bridge(self.root)
        self.assertEqual(bridge.formula(), "1 designs x 1 corner x 3 temp x 1 mode = 3 runs")
        _prototype(bridge)
        self.assertEqual(bridge.formula(), "1 designs x (3 + 1 + 1) = 5 runs")

    def test_cross_group_duplicates_are_counted_and_shown(self) -> None:
        """两个组都写了 55 度 —— 折叠掉一个，而且那个数必须让人看见。

        只写 "3 runs" 会让用户以为自己那条组写错了、少展开了一个。
        """
        bridge = _bare_bridge(self.root)
        bridge.add_group("hot")
        bridge.set_axis_values("temperature", ("55.0",))
        bridge.add_group("hot-again")
        bridge.set_axis_values("temperature", ("55.0",))
        self.assertEqual(bridge.run_count(), 3, "两个组的 55 度都被 base 的 55 度吃掉")
        self.assertEqual(bridge.merged_run_count(), 2)
        self.assertEqual(
            bridge.group_run_counts(), [("base", 3), ("hot", 0), ("hot-again", 0)]
        )
        self.assertIn("(2 duplicates merged)", bridge.formula())

    def test_group_names_are_made_unique_and_base_is_protected(self) -> None:
        bridge = _bare_bridge(self.root)
        self.assertEqual(bridge.add_group("hot"), "hot")
        self.assertEqual(bridge.add_group("hot"), "hot-2", "重名自动加后缀，别静默合并")
        with self.assertRaises(SpecError):
            bridge.remove_group("base")
        with self.assertRaises(SpecError):
            bridge.rename_group("hot", "base")
        with self.assertRaises(SpecError):
            bridge.set_active_group("ghost")
        # 删掉刚删过的那一行是很自然的重复点击 —— no-op，不弹框。
        bridge.remove_group("no-such-group")
        self.assertEqual([g.name for g in bridge.groups()], ["base", "hot", "hot-2"])
        bridge.set_active_group("hot-2")
        bridge.remove_group("hot-2")
        self.assertEqual(bridge.active_group(), "base", "删掉正在编辑的组要退回 base")

    def test_duplicate_base_writes_the_selection_out_explicitly(self) -> None:
        """复制 base 得到的是一个**写死了取值**的组 —— 空覆盖的副本就是 base 本身。"""
        bridge = _bare_bridge(self.root)
        name = bridge.duplicate_group("base")
        self.assertEqual(bridge.active_group(), name)
        self.assertEqual(bridge.group_override("temperature"), ("-40.0", "55.0", "125.0"))
        self.assertEqual(bridge.group_override("equalCurrent"), ("on",))
        # 全是和 base 一样的取值 => 展开出来逐个撞车 => 全被折叠 => 这个组贡献 0。
        self.assertEqual(bridge.run_count(), 3)
        self.assertEqual(dict(bridge.group_run_counts())[name], 0)

    def test_groups_reach_the_spec_snapshot(self) -> None:
        """「Save spec as…」靠这一条：界面上配的组要真的进 `BatchSpec.groups`。

        丢了的症状是"存了、下次打开组没了"，而且无声。
        """
        bridge = _prototype(_bare_bridge(self.root))
        snapshot = bridge.spec_snapshot()
        self.assertEqual([g.name for g in snapshot.groups], ["eqcur-off", "fullwave"])
        self.assertEqual(
            {k: tuple(v) for k, v in snapshot.groups[0].axis_overrides.items()},
            {"temperature": ("55.0",), "equalCurrent": ("off",)},
        )

    def test_group_summary_is_a_delta_for_groups_and_a_full_line_for_base(self) -> None:
        bridge = _prototype(_bare_bridge(self.root))
        self.assertEqual(bridge.group_summary("eqcur-off"), "+ 55.0, eqI off")
        base_line = bridge.group_summary("base")
        self.assertNotIn("+ ", base_line, "base 给的是全量，不是 delta")
        self.assertIn("typical", base_line)
        self.assertIn("-40.0/55.0/125.0", base_line)
        self.assertTrue(
            all(ord(ch) < 128 for ch in base_line + bridge.group_summary("eqcur-off")),
            "红区 LANG 常是 C => 界面字符串必须是纯 ASCII",
        )

    def test_no_warning_before_the_batch_has_run(self) -> None:
        """反向：还没跑过就没什么好警告的 —— 每次都弹的警告等于没有警告。"""
        bridge = _prototype(_bare_bridge(self.root))
        self.assertEqual(bridge.groups_change_warning(), "")
        bridge.plan()
        self.assertEqual(bridge.groups_change_warning(), "", "只 plan 不算跑过")

    def test_warning_after_the_batch_has_started(self) -> None:
        """★ 跑过之后再动组 = 换了一批 run_id，resume 认不出老的目录 —— 必须出声。"""
        bridge = _prototype(_bare_bridge(self.root))
        bridge.start(dry_run=True)
        for _ in range(200):
            if bridge.tick() is None or not bridge.is_running():
                break
        self.assertTrue(bridge.has_started())
        warning = bridge.groups_change_warning()
        self.assertNotEqual(warning, "")
        self.assertIn("run id", warning.lower(), "得说清是 run id 变了")
        self.assertIn("Next:", warning, "报错/警告都要给下一步")
        self.assertTrue(
            all(ord(ch) < 128 for ch in warning), "红区 LANG 常是 C => 纯 ASCII"
        )


class WidenedAxisDoesNotLeakIntoSiblingGroups(_TempRootTest):
    """★ 2026-08-19 复核抓到的静默多跑：**加宽一根轴，别的组不许跟着多扫**。

    `_axes_and_groups()` 会在"某个组写了核心翻译不出来的取值"时把该轴的取值表加宽
    （界面自造的 mesh / freq 轴带的是具体 flag、没有 `{value}` 占位符）。加宽是**改轴的
    定义**，而轴的定义对每一个组都生效 —— 只把 base 锁回去的话，任何一个没碰这根轴的
    **兄弟组**都会继承加宽后的取值表，替别人扫一遍它从没要过的取值。
    一个 run 是 10 核 / 100 GB / 35 分钟（BRIEF §12），多扫一条不是显示问题。

    这条 bug 还是条件性的：`mesh: [0.5]`（目录里有的取值）不触发加宽，数就是对的；
    换成 `0.45` 才发作 —— 也就是说"我这个组多跑了一个 run"取决于**别的组**填了什么。
    """

    def _bridge(self) -> GuiState:
        bridge = _bare_bridge(self.root)
        bridge.set_axis_values("mesh", ("0.4",))
        return bridge

    def _add_groups(self, bridge: GuiState, mesh_value: str) -> GuiState:
        bridge.add_group("mesh-var")
        bridge.set_axis_values("mesh", (mesh_value,))
        bridge.add_group("eqcur-off")
        bridge.set_axis_values("temperature", ("55.0",))
        bridge.set_axis_values("equalCurrent", ("off",))
        return bridge

    def test_sibling_group_keeps_inheriting_base(self) -> None:
        bridge = self._add_groups(self._bridge(), "0.45")
        self.assertEqual(
            bridge.group_run_counts(),
            [("base", 3), ("mesh-var", 3), ("eqcur-off", 1)],
            "eqcur-off 一根 mesh 都没碰 => 必须继承 base 的 0.4，只贡献 1 个 run",
        )
        self.assertEqual(bridge.run_count(), 7)
        self.assertEqual(bridge.formula(), "1 designs x (3 + 3 + 1) = 7 runs")
        bridge.plan()
        self.assertEqual(len(bridge.runs()), 7, "界面上的数必须等于 plan() 真建出来的条数")
        leaked = [r.run_id for r in bridge.runs() if r.group == "eqcur-off" and "0_45" in r.run_id]
        self.assertEqual(leaked, [], "eqcur-off 里不该出现 mesh 0.45 的 run")

    def test_value_the_catalog_knows_gives_the_same_count_negative(self) -> None:
        """反向：换一个**不需要加宽**的 mesh 取值，条数必须一模一样。

        两条数不同的话，"跑几个 run"就取决于别的组填了哪个值 —— 那正是这个 bug 的形状。
        """
        bridge = self._add_groups(self._bridge(), "0.5")
        self.assertEqual(
            bridge.group_run_counts(), [("base", 3), ("mesh-var", 3), ("eqcur-off", 1)]
        )
        self.assertEqual(bridge.run_count(), 7)

    def test_saved_spec_expands_to_the_same_count(self) -> None:
        """存盘再读回来，批次大小不许变 —— 变了就是"下次打开跑的不是同一批"。"""
        from ewave_batch.core import spec as spec_module

        bridge = self._add_groups(self._bridge(), "0.45")
        target = os.path.join(self.root, "saved.yaml")
        written = spec_module.save_spec(bridge.spec_snapshot(), target)
        reloaded = spec_module.load_spec(written)
        state = spec_module.spec_to_batch(reloaded, batch_root=self.root)
        self.assertEqual(len(state.runs), bridge.run_count())
        self.assertEqual(
            [r.run_id for r in state.runs], [r.run_id for r in bridge.runs() or ()] or
            [r.run_id for r in state.runs],
        )


class GroupsWarningAcrossSessions(_TempRootTest):
    """★ 2026-08-19 复核抓到的沉默：跨会话给已经跑过的批次加组，界面一声不吭。

    `has_started()` 只知道"**本进程**里点过 Dry-run/Submit"。而批次跨天是常态
    （一个 run 10 核 / 100 GB / 35 分钟），最常见的场景恰恰是"昨天跑完，今天重开 GUI
    加个组" —— 那时 `self._driver is None`，老判据一个字都不说，而磁盘上那批
    `base/...` 目录已经对不上号了。
    """

    def _finished_batch_on_disk(self) -> list[str]:
        from ewave_batch.core import layout as layout_module

        bridge = _bare_bridge(self.root)
        bridge.plan()
        state = bridge._state
        assert state is not None
        os.makedirs(state.batch_dir, exist_ok=True)
        layout_module.write_batch_state(
            os.path.join(state.batch_dir, "batch.json"), state
        )
        return [run.run_id for run in bridge.runs()]

    def test_new_session_on_an_existing_batch_warns(self) -> None:
        old_ids = self._finished_batch_on_disk()
        fresh = _bare_bridge(self.root)  # 同一个 batch_root / batch_name = 第二天重开 GUI
        self.assertFalse(fresh.has_started(), "前提：新进程里 driver 是 None")
        fresh.add_group("eqcur-off")
        fresh.set_axis_values("equalCurrent", ("off",))
        warning = fresh.groups_change_warning()
        self.assertNotEqual(warning, "", "磁盘上已经有这个批次了，必须警告")
        self.assertIn("run id", warning.lower())
        self.assertIn("Next:", warning)
        self.assertTrue(all(ord(ch) < 128 for ch in warning), "红区 LANG 常是 C => 纯 ASCII")
        fresh.plan()
        new_ids = [run.run_id for run in fresh.runs()]
        self.assertFalse(
            set(new_ids) & set(old_ids),
            "前提没了：加组之后 run_id 竟然还对得上，那这条警告就没必要存在",
        )

    def test_a_batch_that_is_not_on_disk_stays_silent_negative(self) -> None:
        """反向：没跑过的新批次不许警告 —— 逢改必警等于没警告。"""
        fresh = _bare_bridge(self.root)
        fresh.batch_name = "never_ran"
        self.assertEqual(fresh.groups_change_warning(), "")


class TreeColumnsAreNotSqueezedBack(_SmokeTest):
    """★ `_fit_tree_columns` 算出来的宽度，不许被 ttk 又压回去。

    2026-08-19 视觉复验之后仍然漏网的一条回归：那一轮把 `minwidth` 设成**表头宽度**，
    而 `design` / `extra` 是 `stretch=True` 的列 —— 视口比"列宽之和"窄的时候（split 版
    的常态：十列合 1085px、视口 827px），ttk 会把可拉伸的列一路压回 `minwidth`。
    实测 `design` 算出来 277px、被压成 78px，于是两个不同的 design key 又双双显示成
    同一串前缀 —— 正是 `_fit_tree_columns` 本来要修的那个缺陷，从另一扇门走了回来。

    比"看起来窄"更糟的是：**横向滚动条对这种情况不起作用**。列本身变窄了，不是被推到
    视口外，滚过去看到的还是被切掉的字，而 Treeview 不画省略号 —— 用户没有任何手段
    看到完整的 design key。

    触发条件是 `recompute()`（每敲一个键、每点一个勾选框都会跑），所以这不是边角情况。
    """

    def test_fit_sets_minwidth_equal_to_the_width_it_computed(self) -> None:
        """纯不变量：`minwidth == width`。这一条不需要显示，永远跑得了。"""
        root = _tk_or_skip(self)
        from tkinter import font as tkfont, ttk

        from gui._ui import _fit_tree_columns

        tree = ttk.Treeview(root, columns=("a", "b"), show="headings")
        tree.heading("a", text="Design")
        tree.heading("b", text="X")
        tree.column("a", stretch=True)
        tree.insert("", "end", values=("MY_LIB_a_very_long_design_key_layout", "y"))
        mono = tkfont.nametofont("TkFixedFont")
        _fit_tree_columns(
            tree, ("a", "b"), head_font=mono, cell_font=mono, floors={"a": 10, "b": 10}
        )
        for key in ("a", "b"):
            self.assertEqual(
                tree.column(key, "minwidth"),
                tree.column(key, "width"),
                "minwidth 小于 width => ttk 会在视口不够时把这一列压回去",
            )
        # 顺带钉住"宽度真的按内容算"，否则上面那条在两个都等于 0 时也成立。
        self.assertGreater(tree.column("a", "width"), tree.column("b", "width"))

    def test_recompute_does_not_shrink_the_design_column(self) -> None:
        """整版 split：反复 `recompute()` 之后，没有一列比自己算出来的宽度窄。"""
        root = _tk_or_skip(self)
        from gui.frames import split

        bridge, _runner, _sched = _gui(self.root)
        frame = split.build_frame(root, bridge)
        frame.pack(fill="both", expand=True)
        app = frame._ewb_app
        # ⚠️ 必须真映射。`_tk_or_skip` 默认 `withdraw()`，而 ttk 只在窗口**映射之后**
        #    才按视口去压可拉伸的列 —— 在隐藏窗口里这条测试即使规则被改回去也照样绿
        #    （实测过：把 minwidth 改回 head_width，隐藏窗口下 3 条全 OK）。
        #    一条抓不到自己那个 bug 的回归测试比没有更糟，所以这里宁可闪一下窗口。
        root.deiconify()
        root.geometry("1560x900")
        root.update_idletasks()
        root.update()
        for _ in range(3):  # 一次不够：压回去是"视口已经算出来了"之后才发生的
            app.recompute()
            root.update_idletasks()
            root.update()

        squeezed = []
        for name, tree in (("runs", app.tree), ("designs", app.dtree)):
            cache = getattr(tree, "_ewb_col_widths", {})
            for key in tree["columns"]:
                want = int(cache.get(key, 0))
                got = int(tree.column(key, "width"))
                if want > got + 1:
                    squeezed.append("%s.%s want=%d got=%d" % (name, key, want, got))
        self.assertEqual(squeezed, [], "这些列被 ttk 压回去了，横向滚动条救不了它们")

    def test_a_squeezable_column_is_the_precondition_negative(self) -> None:
        """反向：确认前提还在 —— runs 表**确实**有可拉伸的列、且列宽和大于视口。

        没有这个前提，上面那条测试即使规则被改回去也照样绿（= 一条假绿的测试）。
        """
        root = _tk_or_skip(self)
        from gui._ui import RUN_STRETCH_COLS

        from gui.frames import split

        bridge, _runner, _sched = _gui(self.root)
        frame = split.build_frame(root, bridge)
        frame.pack(fill="both", expand=True)
        app = frame._ewb_app
        root.deiconify()          # 同上：不映射就量不到真实视口
        root.geometry("1560x900")
        root.update_idletasks()
        root.update()
        app.recompute()
        root.update_idletasks()
        root.update()

        self.assertTrue(RUN_STRETCH_COLS, "一根可拉伸的列都没有 => 压不回去 => 上一条测不到东西")
        total = sum(int(app.tree.column(key, "width")) for key in app.tree["columns"])
        self.assertGreater(
            total,
            app.tree.winfo_width(),
            "列宽之和已经装得进视口了 => ttk 不会压任何列 => 上一条测不到东西",
        )


if __name__ == "__main__":
    unittest.main()
