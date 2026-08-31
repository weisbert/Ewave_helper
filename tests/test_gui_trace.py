# -*- coding: utf-8 -*-
"""Developer log（`gui/trace.py`）+ 它当场抓出来的三条修正。

## 这份文件为什么存在

2026-08-20 用户连报三条，全部长在界面这一层，而且全部**逃过了**当时 1158 条全绿的测试：

1. 「复制到第三个组的时候，输入框全灰」
2. 「复制出来的组好像和上一个组有联系」
3. 「提交 5 个，4 个 running，最后一个是 ready 而不是 pending，根本没跑」

前两条的共同原因是 `duplicate_group` 造出来的覆盖集合不对，第三条根本不是 bug 而是
**一个看不见的上限**（`max_parallel=4`）加**一句加不起来的计数**。三条都不是"某个函数
算错了"，而是"界面和模型说的不是一件事" —— 而当时没有任何一条测试问这个问题。

⇒ 这里的判据分两层：

| 层 | 问什么 | 落点 |
|---|---|---|
| 轨迹本身 | 点一下**留不留得下证据** | `TraceRecordsWhatTheUserDid` |
| 那三条修正 | 修完之后**模型是什么形状** | 后面三个 class |

## 期望值哪来的（防自证配方 2：不从被测代码反推）

* `GROUP_OVERRIDABLE_AXES` 那五根轴 —— 抄 `gui/_ui.py` 的 `GROUP_ROW_AXES`，
  而那份名单的出处是它自己 docstring 里那段「三根轴故意不在这里」的理由；
* 「复制出来的组不许带 freq / tolerance」—— 出处是同一段理由（组覆盖不了它们，
  于是界面上看不见、点不到、撤不掉）；
* 「5 个 run、上限 4 ⇒ 状态栏必须提到那 1 个」—— 出处是 `BatchOptions.max_parallel`
  的语义 + 用户实报的那句"最后一个根本就没跑"。

⏱ 不 sleep、不读墙钟、不起子进程、不碰真工具。
🚨 零站点标识符：library / cell / view 全是显式假值。
"""

from __future__ import annotations

import os
import unittest

import gui.state as gui_state
from ewave_batch.model import EwaveBatchError, RunStatus
from gui.state import GuiState
from gui.trace import ActionTrace

BASE = gui_state.BASE_GROUP

GROUP_OVERRIDABLE_EXPECTED = (
    "corner",
    "temperature",
    "mesh",
    "layer2d",
    "layermesh",
    "fullWave",
    "equalCurrent",
)
"""**手写**的期望值，出处见模块 docstring。改这一行之前先改 `GROUP_ROW_AXES` 的理由。"""


_ROOT: object | None = None
"""**整个模块共用一个** Tk 根窗口。

🚨 不是省事，是必须：一个进程里反复 `Tk()` 会在某些机器上失败
（实测本机第 8 次 `Tk()` 抛 "tk wasn't installed properly"），而那是**测试自己**
的毛病，看起来却像被测代码坏了。frame 逐条销毁（`_app()` 的 `addCleanup`），
根窗口留着 —— 与 `tests/test_gui_invariants.py` 同一条规矩。
"""


def _tk_or_skip(test: unittest.TestCase) -> object:
    """本机能不能开窗口。开不了就**带原因**跳过（平台性 skip，同 `test_gui_invariants`）。"""
    global _ROOT
    try:
        import tkinter as tk
    except ImportError as exc:  # pragma: no cover - 本机装了 tkinter
        test.skipTest(f"平台跳过：这台机器没装 tkinter（{exc}）—— CLI 不受影响")
    if _ROOT is not None:
        return _ROOT
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - 本机有显示
        test.skipTest(f"平台跳过：这台机器开不了显示（{exc}）—— CLI 不受影响")
    root.withdraw()
    _ROOT = root
    return root


def _bridge() -> GuiState:
    state = GuiState(batch_root="/tmp/ewb_trace", batch_name="trace")
    state.add_design("MY_LIB", "CELL_A", "layout")
    return state


class TraceIsPlainStdlibAndBounded(unittest.TestCase):
    """轨迹这个数据结构自己的性质。**不需要显示** —— 它不许 import tkinter。"""

    def test_it_records_in_order_and_keeps_the_kind(self) -> None:
        trace = ActionTrace()
        trace.record("click", "do_thing")
        trace.note("branch taken", "because x")
        rows = trace.lines()
        self.assertEqual(len(rows), 2)
        self.assertIn("click", rows[0])
        self.assertIn("do_thing", rows[0])
        self.assertIn("note", rows[1])
        self.assertIn("because x", rows[1])

    def test_the_ring_buffer_has_a_floor_and_says_what_it_dropped(self) -> None:
        """无上界的 list 会把内存吃光；有上界就必须**说**自己丢了东西。

        丢了不说的症状是"最早那几次点击不见了"，而那看起来跟 bug 一模一样。
        """
        trace = ActionTrace(capacity=1)  # 会被抬到下限 50
        for index in range(60):
            trace.record("click", "step-%d" % index)
        rows = trace.lines()
        self.assertEqual(len(rows), 51, "50 条内容 + 1 条「丢了多少」的交代")
        self.assertIn("挤掉", rows[0])
        self.assertIn("step-59", rows[-1])
        self.assertNotIn("step-0 ", "".join(rows))

    def test_consecutive_identical_snapshots_collapse(self) -> None:
        """`recompute()` 每敲一个键跑一次 —— 不折叠的话真正的动作会被淹掉。"""
        trace = ActionTrace()
        trace.state("active=base sel=base")
        trace.state("active=base sel=base")
        trace.state("active=g1 sel=g1")
        trace.state("active=base sel=base")
        self.assertEqual(len(trace.lines()), 3, "只有变化的那几拍进轨迹")

    def test_recording_never_raises(self) -> None:
        """记日志的东西把被记的东西搞崩，是最差的一种 bug。"""

        class Boom:
            def __str__(self) -> str:
                raise RuntimeError("nope")

        trace = ActionTrace()
        trace.record("click", Boom())  # 不许抛
        trace.on_record = lambda: 1 / 0  # 回调炸了也不许抛
        trace.record("click", "fine")
        self.assertGreaterEqual(len(trace.lines()), 1)

    def test_errors_carry_the_exception_type_and_a_traceback_on_demand(self) -> None:
        trace = ActionTrace()
        try:
            raise ValueError("boom")
        except ValueError as exc:
            trace.error("do_thing", exc)
            trace.error("do_thing", exc, tb=True)
        doc = trace.document()
        self.assertIn("ValueError: boom", doc)
        self.assertIn("CRASH", doc)
        self.assertIn("test_gui_trace.py", doc, "traceback 里要看得见是哪一行")

    def test_it_does_not_import_tkinter(self) -> None:
        """CLAUDE.md 硬约束 5 的推论：这个模块在无 `$DISPLAY` 的 ssh 会话里也要能 import。

        判据是**真的有 import 语句**（走 `ast`），不是"文本里出现过这几个字" ——
        后者会被自己的注释咬到，而一条被注释咬到的测试下一个人只会把它删掉。
        """
        import ast

        import gui.trace as module

        tree = ast.parse(open(module.__file__, encoding="utf-8").read())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("tkinter", imported)
        self.assertLessEqual(
            imported,
            {"__future__", "os", "time", "traceback", "collections"},
            "纯 stdlib（硬约束 2），而且只用这几样",
        )


class TraceRecordsWhatTheUserDid(unittest.TestCase):
    """★ 点一下，留不留得下证据。判据是**用户报 bug 时会说的那些词**。"""

    def setUp(self) -> None:
        os.environ["EWB_SMOKE"] = "1"
        self.addCleanup(os.environ.pop, "EWB_SMOKE", None)

    def _app(self):
        root = _tk_or_skip(self)
        from gui.frames import split

        bridge = _bridge()
        frame = split.build_frame(root, bridge)
        frame.pack()
        self.addCleanup(frame.destroy)
        app = frame._ewb_app
        app.recompute()
        return root, bridge, app

    def test_a_click_leaves_a_click_and_an_ok(self) -> None:
        _root, _bridge_, app = self._app()
        app.trace.clear()
        app.do_add_group()
        doc = app.trace.document()
        self.assertIn("click", doc)
        self.assertIn("do_add_group", doc)
        self.assertIn("ok", doc)

    def test_a_blocked_action_leaves_the_dialog_text(self) -> None:
        """`EWB_SMOKE=1` 下弹框根本不建 ⇒ 轨迹是**唯一**的痕迹。

        用户报 bug 时说的就是弹框上那句话，所以那句话必须留得下来。
        """
        _root, _bridge_, app = self._app()
        app.trace.clear()
        app.gtree.selection_set(BASE)
        app.do_remove_group()  # 删 base -> 被拦
        doc = app.trace.document()
        self.assertIn("dialog[info] Cannot remove the base group", doc)

    def test_the_state_line_carries_the_four_things_that_can_disagree(self) -> None:
        """快照那一行是三条 bug 的公共判据，四个字段一个都不能少。"""
        _root, _bridge_, app = self._app()
        app.trace.clear()
        app.recompute()
        app._trace_state()
        line = [row for row in app.trace.lines() if " state " in row]
        self.assertTrue(line, "recompute 之后必须留下一行快照")
        for field in ("active=", "sel=", "groups=[", "axes["):
            self.assertIn(field, line[-1])

    def test_a_swallowed_guard_error_is_recorded(self) -> None:
        """`_guard` 把错误吞成状态栏一行字，而状态栏只留**最后一条**。

        一拍里过 8 道闸，第一条错常常是真正的原因 —— 它在屏幕上活不过同一拍。
        """
        _root, _bridge_, app = self._app()
        app.trace.clear()

        def boom() -> None:
            raise EwaveBatchError("something specific went wrong")

        app._guard(boom)
        doc = app.trace.document()
        self.assertIn("ERR", doc)
        self.assertIn("something specific went wrong", doc)

    def test_an_unhandled_tk_callback_exception_is_recorded(self) -> None:
        """Tk 的默认处理是打到 stderr 然后若无其事地继续 —— 红区没人在看 stderr。

        接住它才有得查（这正是"点了没反应"的家）。
        """
        _root, _bridge_, app = self._app()
        app.trace.clear()
        try:
            raise RuntimeError("callback blew up")
        except RuntimeError as exc:
            app.top.report_callback_exception(RuntimeError, exc, exc.__traceback__)
        doc = app.trace.document()
        self.assertIn("unhandled exception in a Tk callback", doc)
        self.assertIn("RuntimeError: callback blew up", doc)

    def test_the_developer_log_window_shows_the_trace(self) -> None:
        _root, _bridge_, app = self._app()
        app.do_add_group()
        window = app.show_trace()
        self.addCleanup(window.close)
        self.assertIn("do_add_group", window.document())
        self.assertIn("developer log", window.document().splitlines()[0].lower())
        # 第二次按 Developer log 是"我要看日志"，不是"我要两份日志"。
        self.assertIs(app.show_trace(), window)

    def test_clear_starts_a_fresh_trace(self) -> None:
        """「我现在按一遍给你看」之前按 Clear，噪声就没了 —— 那是这个按钮的全部用途。"""
        _root, _bridge_, app = self._app()
        app.do_add_group()
        window = app.show_trace()
        self.addCleanup(window.close)
        window._clear()
        self.assertNotIn("do_add_group", window.document())


class DuplicateGroupProducesAnIndependentEditableGroup(unittest.TestCase):
    """★ 用户 2026-08-20 的第 1 + 第 2 条。**纯模型层**，不需要显示。"""

    def test_the_overridable_axes_list_matches_the_settings_rows(self) -> None:
        """两处名单不许漂：`gui.state` 决定复制出什么，`gui._ui` 决定界面画什么。

        漂了的症状是"这个组覆盖了一根轴，而界面上没有任何地方显示它" ——
        看不见、点不到、撤不掉，却照样进笛卡尔积。
        """
        self.assertEqual(gui_state.GROUP_OVERRIDABLE_AXES, GROUP_OVERRIDABLE_EXPECTED)
        try:
            import gui._ui as ui
        except ImportError as exc:  # pragma: no cover - 本机装了 tkinter
            self.skipTest(f"平台跳过：{exc}")
        rows = tuple(sorted({axis for axes in ui.GROUP_ROW_AXES.values() for axis in axes}))
        self.assertEqual(rows, tuple(sorted(gui_state.GROUP_OVERRIDABLE_AXES)))

    def test_copying_base_does_not_pin_the_sweep_or_the_tolerances(self) -> None:
        """🚨 这是 bug 本身。

        原来复制 base 会把 `_base_axes()` 的**每一根**轴写成显式覆盖，于是副本带着
        一份钉死的 freq + 两个 tolerance：界面上那三样在非 base 组里是置灰的
        （`GROUP_ROW_AXES` 明说它们只属于 base），所以用户看不见、点不到、撤不掉 ——
        而它们照样进笛卡尔积，照样让 `freq` 变成"全批次在变的轴"从而改掉**每一个**
        run 的目录名。
        """
        bridge = _bridge()
        name = bridge.duplicate_group(BASE, "copy")
        overrides = set(bridge._require_group(name).axis_overrides)
        self.assertEqual(overrides, set(GROUP_OVERRIDABLE_EXPECTED))
        for forbidden in ("freq", "relativeTolerance", "relativeCurrentTolerance"):
            self.assertNotIn(forbidden, overrides)

    def test_copying_an_empty_group_gives_something_editable(self) -> None:
        """`+ Add` 出来的组一根轴都不覆盖 ⇒ 整块 Settings 是灰的（那是**对的**）。

        但复制它的时候用户要的是"一份能改的东西"，照抄一个空集合等于又造一个空组，
        界面上看起来就是"输入全灰" —— 用户 2026-08-20 报的第 1 条。
        理由与复制 base 那条逐字相同：空覆盖的副本没有意义。
        """
        bridge = _bridge()
        empty = bridge.add_group("empty")
        self.assertEqual(bridge._require_group(empty).axis_overrides, {})
        copy = bridge.duplicate_group(empty, "copy-of-empty")
        self.assertEqual(
            set(bridge._require_group(copy).axis_overrides), set(GROUP_OVERRIDABLE_EXPECTED)
        )

    def test_the_copy_and_the_source_are_two_separate_dicts(self) -> None:
        """「复制出来的组好像和上一个组有联系」—— 改一个不许动另一个。"""
        bridge = _bridge()
        first = bridge.duplicate_group(BASE, "first")
        second = bridge.duplicate_group(first, "second")
        base_temps = tuple(bridge._selection["temperature"])
        bridge.set_group_override("temperature", ["25"], group=second)
        self.assertEqual(bridge.group_override("temperature", group=second), ("25.0",))
        self.assertEqual(
            bridge.group_override("temperature", group=first),
            base_temps,
            "改副本不许碰到源组",
        )
        bridge.remove_group(first)
        self.assertEqual([g.name for g in bridge.groups()], [BASE, "second"])
        self.assertEqual(
            set(bridge._require_group("second").axis_overrides), set(GROUP_OVERRIDABLE_EXPECTED)
        )

    def test_negative_a_group_that_really_overrides_one_axis_is_copied_verbatim(self) -> None:
        """反向验证：**非空**的源组照抄，不许被"补全"成五根轴。

        共用同一条构造路径，只改一个入参（源组空 vs 非空）。
        """
        bridge = _bridge()
        group = bridge.add_group("eqcur-off")
        bridge.set_group_override("equalCurrent", ["off"], group=group)
        copy = bridge.duplicate_group(group, "copy")
        self.assertEqual(set(bridge._require_group(copy).axis_overrides), {"equalCurrent"})


class _DriverStub:
    """只回答 `summary()` 的假 driver。

    在场的理由：`status_line()` 走 `result_is_current()`，那条要求 driver 在场 ——
    没有它整句话会退回 "Preview up to date"，测的就不是要测的那条路了。
    数数的口径与 `GuiState.summary()` 的兜底分支逐字相同（`RunStatus.value` -> 条数），
    所以这个 double 不引入任何新语义：它替的是"driver 在跑"这个事实，不是算法。
    """

    def __init__(self, state: object) -> None:
        self._state = state

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for run in self._state.runs:  # type: ignore[attr-defined]
            counts[run.status.value] = counts.get(run.status.value, 0) + 1
        return counts


class WaitingRunsAreVisible(unittest.TestCase):
    """★ 用户 2026-08-20 的第 3 条：4 个 running、第 5 个停在 `ready`。

    那是**按设计工作**（`max_parallel` 默认 4），但界面上那个 4 从来没显示过，
    而状态栏那句话把第 5 个整个漏掉了 —— 加起来比表里的行数少。
    """

    def test_max_parallel_is_settable_and_clamped(self) -> None:
        bridge = _bridge()
        self.assertEqual(bridge.options().max_parallel, 4, "默认值出处：BatchOptions")
        self.assertEqual(bridge.set_max_parallel(8), 8)
        self.assertEqual(bridge.set_max_parallel(0), 1, "0 个在飞 = 永远不动，夹到 1")
        self.assertEqual(
            bridge.set_max_parallel(10_000),
            gui_state.MAX_PARALLEL_CAP,
            "手滑多打一个 0 不该拿整个队列去换",
        )
        with self.assertRaises(EwaveBatchError):
            bridge.set_max_parallel("four")

    def _five_runs(self) -> GuiState:
        """5 个 run 的批次，摆成"批次正在跑"的样子。

        ⚠️ 必须把 `fullWave` 掐成一个取值：默认是 on + off 两个（`gui.state` 的默认表），
        5 个温度会展成 **10** 个 run。这里要的是"5 个 run、上限 4"这个最小场景。
        ⚠️ `status_line()` 走 `result_is_current()`，那条要求 driver 在场 ——
        没有它整句话会退回 "Preview up to date"，测的就不是要测的那条路了。
        """
        bridge = _bridge()
        bridge.set_axis_values("temperature", ["-40", "25", "55", "85", "125"])
        bridge.set_axis_values("fullWave", ["off"])
        bridge.plan()
        bridge._running = True
        bridge._driver = _DriverStub(bridge._state)
        bridge._result_state = bridge._state
        return bridge

    def test_the_status_line_counts_the_runs_that_are_still_waiting(self) -> None:
        """🚨 判据是**加得起来**：状态栏那句话提到的条数必须等于表里的行数。

        漏掉 `ready` 的时候用户读到的是"最后一个根本就没跑"（实报原话）。
        """
        bridge = self._five_runs()
        runs = bridge.runs()
        self.assertEqual(len(runs), 5, "5 个温度 × 1 corner × 1 mode = 5 个 run")
        for run in runs[:4]:
            run.status = RunStatus.RUNNING
        line = bridge.status_line()
        self.assertIn("4 running", line)
        self.assertIn("1 waiting for a free slot", line)
        self.assertIn("max 4 in flight", line)

    def test_negative_nothing_waiting_means_no_extra_sentence(self) -> None:
        """反向验证：同一条构造路径，只把「还在等的那一个」也置成 running。"""
        bridge = self._five_runs()
        for run in bridge.runs():
            run.status = RunStatus.RUNNING
        line = bridge.status_line()
        self.assertIn("5 running", line)
        self.assertNotIn("waiting for a free slot", line)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
