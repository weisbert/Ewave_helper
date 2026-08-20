# -*- coding: utf-8 -*-
"""dry-run 的**结论**、Log 窗口、以及"拷出去之前脱敏"这条路。

这份文件盯的是 2026-08-20 用户实测报回来的三件事：

1. **「点击 dry run 之后，我也不知道到底可以跑了不」** —— dry-run 一个 run 都不会
   变成 `done`（它不提交、不建目录），于是通用的那句「Finished - 0 / 3 done,
   0 failed」逐字都对、读起来却是"什么都没发生"。判据：dry-run 跑完之后状态栏那句话
   里**必须**出现"几条命令拼出来了"，且**不许**出现 `0 / N done`。
2. **「LOG 窗口页做的不太好，应该有个专门打印 log 窗口的页面，我可以 copy」** ——
   dry-run 的全部产出就是那些命令，而它们一条都不在主界面上。判据：Log 窗口的
   `document()` 里逐条都在，且 Text 不是 `state="disabled"`（那样在一部分 Tk 版本上
   连选中都做不到，而"能选中能拷"是这扇窗存在的全部理由）。
3. **拷出去的东西不许带站点坐标**（CLAUDE.md 硬约束 1）—— 用户的原话是"粘贴给你
   debug"，而日志里逐字带着 library / cell / ptxt / 路径。判据：脱敏之后那几个
   假坐标一个都搜不到，而命令的**结构**（flag 名、数值）一个都没少。

四条配方（`docs/OVERNIGHT.md`）在这里的落点：

* **期望值来源** = 手写字面量（本文件顶上那批 `FAKE_*` 和 `EXPECTED_*`），
  一个都不是从被测代码取回来的；
* **反向验证** = 每条正向都配一条 `_negative`，**共用同一条构造路径**（`_bridge()`），
  只改一个入参（有没有官方 run 目录 / 脱敏表给不给）；
* **计数断言** = 事件条数、日志行数、脱敏表条数、run 数逐个等于手写期望；
* ⏱ 全程不 sleep、不起 eWave：dry-run 本来就不执行任何东西（硬约束 3）。

🚨 本文件零站点标识符：library / cell / view / 端口名 / 路径全是显式假值。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from ewave_batch.model import DriverEvent, EventKind, PortMode, PortSpec, SiteFacts

import gui.state as gui_state
from gui.state import GuiState

# --------------------------------------------------------------------------
# 手写的假值（一个真实取值都没有）
# --------------------------------------------------------------------------

FAKE_LIB = "FAKELIB"
FAKE_CELL = "FAKECELL"
FAKE_VIEW = "fakeview"
FAKE_EWAVE_BIN = "/tmp/faketools/bin/ewave"
FAKE_STRMOUT_BIN = "/tmp/faketools/bin/strmout"
FAKE_LAYER_MAP = "/tmp/fakepdk/fake.layermap"
FAKE_PTXT_DIR = "/tmp/fakepdk/ptxt"
FAKE_PTXT_TEMPLATE = "fake_{corner}.ptxt"
FAKE_ACCOUNT = "fakeaccount"
FAKE_QUEUE = "fakequeue"
FAKE_PORT_NAMES = ("FAKEPINA", "FAKEPINB")

CORNERS: tuple[str, ...] = ("typical",)
TEMPERATURES: tuple[str, ...] = ("25.0", "55.0")

EXPECTED_RUNS = 2
"""1 design x 1 corner x 2 temperature = 2（手算）。"""

EXPECTED_EVENTS = 3
"""dry-run 播几条事件：阶段 1 一条（每个 design 一条 `planned`）+ 阶段 2 每个 run 一条。
1 + 2 = 3（手算，`sched.driver.Driver._plan_only` 的两个循环）。"""

LOG_HEADER_LINES = 7
"""`_LogWindow.document()` 的头有几行：6 行 `# …` + 1 条分隔线（手数）。"""

SITE_VALUES: tuple[str, ...] = (
    FAKE_LIB,
    FAKE_CELL,
    FAKE_VIEW,
    FAKE_EWAVE_BIN,
    FAKE_STRMOUT_BIN,
    FAKE_PTXT_DIR,
)
"""dry-run 的日志里**真的会出现**、脱敏之后一个都不许再搜得到的那些串。

阶段 1 的 `strmout` 和阶段 2 的 `ewave` 各贡献一半 —— 写进去一个日志里根本没有的值，
下面那句 `assertNotIn` 就是空过的（那份文本里本来就没有它）。"""

SITE_VALUES_IN_TABLE: tuple[str, ...] = SITE_VALUES + (FAKE_ACCOUNT, FAKE_QUEUE) + FAKE_PORT_NAMES
"""**脱敏表里**必须认得的那些。

比上面那组多出账号 / 队列 / 端口名：它们不出现在 dry-run 的 argv 里（dsub 那层是
真提交时才拼的，端口走 `--all`），但真跑起来的日志里会有 —— 表得先认得它们。"""

STRUCTURE_KEPT: tuple[str, ...] = ("--corner=typical", "--viaMode=1", "--temperature=25.0")
"""脱敏之后**必须原样还在**的那些：它们是工具语义，不是站点身份（硬约束 1b）。
少了这条断言，一个"把整份日志换成 `<redacted>`"的实现也会绿。"""


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
        ptxt_dir=FAKE_PTXT_DIR,
        ptxt_name_template=FAKE_PTXT_TEMPLATE,
        dsub_account=FAKE_ACCOUNT,
        dsub_queue=FAKE_QUEUE,
        official_port_spec=PortSpec(
            mode=PortMode.EXPLICIT,
            mapping=tuple((f"P{i:03d}", name) for i, name in enumerate(FAKE_PORT_NAMES)),
        ),
    )


def _bridge(root: str, *, with_site: bool = True) -> GuiState:
    """走界面那条路造一个批次。`with_site=False` = 没有官方 run 目录（= 本机的常态）。

    正反两向共用这一条路径，**只差这一个入参** —— 有坐标 ⇒ 命令全拼得出来，
    没坐标 ⇒ 一条都拼不出来。dry-run 的结论恰好就该在这两者之间分开。
    """
    offdir = f"{root}/wa/ewave_simulation/design" if with_site else ""
    facts = _facts(offdir)
    bridge = GuiState(
        batch_root=root,
        batch_name="log_batch",
        official_run_dir=offdir,
        discover=lambda _path: facts,
    )
    bridge.set_axis_values("corner", CORNERS)
    bridge.set_axis_values("temperature", TEMPERATURES)
    for name in ("fullWave", "equalCurrent", "relativeTolerance", "relativeCurrentTolerance"):
        bridge.set_axis_values(name, ())
    bridge.add_design(FAKE_LIB, FAKE_CELL, FAKE_VIEW)
    bridge.plan()
    return bridge


def _dry_run(bridge: GuiState) -> None:
    """跑完一整趟 dry-run。循环是保险丝不是等待 —— `_plan_only` 一拍就规划完。"""
    bridge.start(dry_run=True)
    for _ in range(4):
        report = bridge.tick()
        if report is None or report.finished:
            return
    raise AssertionError("dry-run 四拍还没结束 —— _plan_only 不该会自己前进")


def _tk_or_skip(test: unittest.TestCase) -> object:
    """本机能不能开窗口。开不了就**带原因**跳过（平台性 skip）。"""
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
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ewb_log_")
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name.replace("\\", "/")


class _SmokeTest(_TempRootTest):
    """建控件树的测试：`EWB_SMOKE=1` 保证不弹模态框、Log 窗口建了也不露脸。"""

    def setUp(self) -> None:
        super().setUp()
        os.environ["EWB_SMOKE"] = "1"
        self.addCleanup(os.environ.pop, "EWB_SMOKE", None)


# ==========================================================================
# 1. dry-run 的结论
# ==========================================================================


class DryRunVerdict(_TempRootTest):
    """★ 「到底可以跑了不」这个问题，界面上必须有一句话回答得了。"""

    def test_dry_run_reports_commands_built_not_zero_done(self) -> None:
        bridge = _bridge(self.root)
        self.assertEqual(bridge.run_count(), EXPECTED_RUNS)
        self.assertIsNone(bridge.dry_run_result(), "还没跑就不该有结论")

        _dry_run(bridge)

        # 计数断言：2 条全拼出来了、0 条失败（手写期望，不是从 summary 取回来的）。
        self.assertEqual(bridge.dry_run_result(), (EXPECTED_RUNS, 0))
        line = bridge.status_line()
        self.assertIn("Dry-run OK", line)
        self.assertIn("all %d commands built" % EXPECTED_RUNS, line)
        # ★ 这一条才是用户报的那个 bug：旧文案是「Finished - 0 / 2 done, 0 failed」。
        self.assertNotIn("done", line)
        self.assertNotIn("Finished", line)

    def test_dry_run_reports_commands_built_not_zero_done_negative(self) -> None:
        """反向：同一条构造路径去掉官方 run 目录 ⇒ 一条命令都拼不出来。

        没有这条，上面那条在"永远说 OK"的实现上也会绿 —— 而"永远说 OK"正是
        这次要修的那个毛病的镜像（永远说 0 done）。
        """
        bridge = _bridge(self.root, with_site=False)
        _dry_run(bridge)

        self.assertEqual(bridge.dry_run_result(), (0, EXPECTED_RUNS))
        line = bridge.status_line()
        self.assertIn("could not be built", line)
        self.assertNotIn("Dry-run OK", line)

    def test_dry_run_leaves_every_run_ready_and_says_so(self) -> None:
        """dry-run **不许**把 run 推进到任何终态 —— 它一个 job 都没提交。

        这条守的是"别为了让状态栏好看而去改 run 的状态"：那样 resume 会以为
        这些 run 已经交出去过了。
        """
        bridge = _bridge(self.root)
        _dry_run(bridge)
        counts = bridge.summary()
        self.assertEqual(counts["ready"], EXPECTED_RUNS)
        self.assertEqual(counts["done"], 0)
        self.assertEqual(counts["pending"], 0)
        self.assertEqual(counts["failed"], 0)

    def test_dry_run_does_not_count_as_submitted(self) -> None:
        """dry-run 不算"提交过" —— 界面不许因为它把自己锁死（2026-08-20 已修的那条）。"""
        bridge = _bridge(self.root)
        _dry_run(bridge)
        self.assertTrue(bridge.has_started())
        self.assertFalse(bridge.has_submitted())


# ==========================================================================
# 2. 结果过期
# ==========================================================================


class StaleResult(_TempRootTest):
    """dry-run 之后界面是**不锁**的 ⇒ 改一个勾选，上一次的结论就说的是别的东西了。"""

    def test_editing_after_a_dry_run_expires_the_verdict(self) -> None:
        bridge = _bridge(self.root)
        _dry_run(bridge)
        self.assertTrue(bridge.result_is_current())

        bridge.set_axis_values("temperature", TEMPERATURES + ("125.0",))
        bridge.plan()

        self.assertFalse(bridge.result_is_current())
        self.assertIsNone(bridge.dry_run_result())
        # ★ 计数必须跟着**新**矩阵走。少了这道门就是"表上 3 行、状态栏说 2 个"，
        #   而两边都振振有词。
        self.assertEqual(len(bridge.runs()), EXPECTED_RUNS + 1)
        self.assertEqual(sum(bridge.summary().values()), EXPECTED_RUNS + 1)
        self.assertIn("Preview up to date", bridge.status_line())

    def test_editing_after_a_dry_run_expires_the_verdict_negative(self) -> None:
        """反向：**不动**勾选时结论照旧有效，计数还是老那份。

        没有这条，一个"永远返回 False"的 `result_is_current()` 也会让上面那条绿。
        """
        bridge = _bridge(self.root)
        _dry_run(bridge)
        self.assertTrue(bridge.result_is_current())
        self.assertEqual(bridge.dry_run_result(), (EXPECTED_RUNS, 0))
        self.assertEqual(sum(bridge.summary().values()), EXPECTED_RUNS)


# ==========================================================================
# 3. 脱敏（硬约束 1）
# ==========================================================================


class Redaction(_TempRootTest):
    """「拷出去给别人看」这条路上，站点坐标一个都不许跟着走。"""

    def _log_like_text(self, bridge: GuiState) -> str:
        """事件流拼成一份"像日志的"文本 —— Log 窗口贴出去的正文就是这些。

        用 `events()` 而不是 `command_text()`：后者只有阶段 2 那条 ewave 命令，
        而阶段 1 的 `strmout` 路径同样是站点坐标。
        """
        return "\n".join(event.message for event in bridge.events())

    def test_every_site_value_is_masked(self) -> None:
        bridge = _bridge(self.root)
        _dry_run(bridge)
        text = self._log_like_text(bridge)
        # 先证明这份文本里**真的**有坐标 —— 否则下面那句 assertNotIn 是空过的。
        for value in SITE_VALUES:
            self.assertIn(value, text, f"构造出来的日志里没有 {value}，这条测试是空的")

        table = bridge.redaction_map()
        masked = gui_state.redact(text, table)

        for value in SITE_VALUES:
            self.assertNotIn(value, masked, f"{value} 没被换掉")
        # 结构不许跟着一起没：flag 名和数值是工具语义，不是站点身份（硬约束 1b）。
        for kept in STRUCTURE_KEPT:
            self.assertIn(kept, masked)
        # 表里还得认得那些"这趟没出现、真跑时会出现"的（账号 / 队列 / 端口名）。
        for value in SITE_VALUES_IN_TABLE:
            self.assertIn(value, table, f"脱敏表里没有 {value}")
        self.assertGreaterEqual(len(table), len(SITE_VALUES_IN_TABLE))

    def test_every_site_value_is_masked_negative(self) -> None:
        """反向：同一份文本、**空表** ⇒ 一个都没换。

        没有这条，一个"把 SITE_VALUES 硬编码删掉"的实现也会绿。
        """
        bridge = _bridge(self.root)
        _dry_run(bridge)
        text = self._log_like_text(bridge)
        untouched = gui_state.redact(text, {})
        self.assertEqual(untouched, text)
        for value in SITE_VALUES:
            self.assertIn(value, untouched)

    def test_short_values_are_not_collected(self) -> None:
        """太短的取值不进表 —— 换掉它们会静默改写 flag 名和普通英文。

        `MASK_MIN_CHARS` 的口径写在 `GuiState.redaction_map` 上。
        """
        self.assertEqual(gui_state.MASK_MIN_CHARS, 4)
        bridge = GuiState(batch_root=self.root, batch_name="short")
        bridge.add_design("ab", "cd", "ef")  # 每个都短于 MASK_MIN_CHARS
        table = bridge.redaction_map()
        for tiny in ("ab", "cd", "ef"):
            self.assertNotIn(tiny, table)

    def test_masking_covers_both_path_separators(self) -> None:
        """同一条路径的两种分隔符写法都要换掉。

        Windows 上 `os.path.join` 给反斜杠、我们自己拼 `--workDir=` 给正斜杠 ——
        只收一种等于另一种原样漏出去（2026-08-20 实测）。红区是 Linux，
        两个变体相同，这条在那边恒真、也不该因此删掉。
        """
        bridge = _bridge(self.root)
        _dry_run(bridge)
        table = bridge.redaction_map()
        batch_dir = bridge.batch_dir()
        for variant in (batch_dir.replace("\\", "/"), batch_dir.replace("/", "\\")):
            self.assertIn(variant, table, f"批次目录的 {variant!r} 写法没进脱敏表")


# ==========================================================================
# 4. Log 窗口
# ==========================================================================


class LogWindow(_SmokeTest):
    """专门那扇窗：内容全、可选可拷、dry-run 跑完自己弹出来。"""

    def _app(self, *, with_site: bool = True):
        root = _tk_or_skip(self)
        from gui.frames import split

        bridge = _bridge(self.root, with_site=with_site)
        frame = split.build_frame(root, bridge)
        return frame._ewb_app, bridge

    def test_log_document_has_every_event(self) -> None:
        app, bridge = self._app()
        app.do_dry_run()

        self.assertEqual(len(bridge.events()), EXPECTED_EVENTS)
        window = app.show_log()
        document = window.document()

        # 计数断言：头 + 每条事件至少一行。`>=` 而不是 `==` 是因为核心那些错误信息
        # 自带换行（一句"坏了什么" + 一句 `Next:`），续行也算行。
        lines = document.splitlines()
        self.assertGreaterEqual(len(lines), LOG_HEADER_LINES + EXPECTED_EVENTS)
        self.assertTrue(lines[0].startswith("# eWave Batch log"))
        for run in bridge.runs():
            self.assertIn(run.run_id, document)
        self.assertIn(FAKE_EWAVE_BIN, document, "阶段 2 的命令没进日志")
        self.assertIn(FAKE_STRMOUT_BIN, document, "阶段 1 的命令没进日志")
        # 屏幕上显示的和 Copy 拿到的是**同一份**。
        # （Tk 的 Text 恒有一个隐式收尾换行，`end-1c` 去掉的就是它 —— 我们 insert
        #  进去的那份本来就以换行结尾，所以两边逐字相等。）
        self.assertEqual(window.text.get("1.0", "end-1c"), document)

    def test_log_document_has_every_event_negative(self) -> None:
        """反向：还没跑过任何东西时，日志正文是那句"还没有"，而不是伪造的内容。"""
        app, _bridge = self._app()
        window = app.show_log()
        document = window.document()
        self.assertIn("nothing yet", document)
        self.assertNotIn(FAKE_EWAVE_BIN, document)

    def test_dry_run_opens_the_log_by_itself(self) -> None:
        """★ dry-run 跑完**自己**把 Log 推到脸上 —— 它的全部产出就在那儿。"""
        app, _bridge = self._app()
        self.assertIsNone(app._log, "还没跑就不该开窗")
        app.do_dry_run()
        self.assertIsNotNone(app._log)
        self.assertTrue(app._log.alive())
        verdict, _fg, _bg = app._log.verdict_text()
        self.assertIn("Dry-run OK", verdict)

    def test_dry_run_opens_the_log_by_itself_negative(self) -> None:
        """反向：拼不出命令时同样弹，但那句话是红的、说的是"没拼出来"。

        （没有官方 run 目录时 `do_dry_run` 会被 preflight 挡在门外 —— 那是另一条
        正确的路，所以这里直接驱动 bridge，测的是结论本身。）
        """
        app, bridge = self._app(with_site=False)
        bridge.start(dry_run=True)
        app._pump()
        self.assertIsNotNone(app._log)
        verdict, foreground, _bg = app._log.verdict_text()
        self.assertIn("could NOT be built", verdict)
        self.assertNotIn("Dry-run OK", verdict)
        from gui import _ui

        self.assertEqual(foreground, _ui.RED)

    def test_log_text_is_selectable(self) -> None:
        """★ Text **不是** `state="disabled"`。

        判据必须是机器判的：`disabled` 在一部分 Tk 版本上连鼠标选中都不给，而红区是
        Linux、Tk 版本和开发机不同 —— 那种 bug 只在气隙对面发作。所以这里验
        ①state 是 normal，②`<Key>` 有拦截绑定（= 只读靠拦按键实现），
        ③Ctrl-A 被自己接管了（Tk 默认把它绑成"行首"）。
        """
        app, _bridge = self._app()
        window = app.show_log()
        self.assertEqual(str(window.text.cget("state")), "normal")
        self.assertTrue(window.text.bind("<Key>"), "只读没有靠拦按键实现")
        self.assertTrue(window.text.bind("<Control-a>"), "Ctrl-A 没被接管")

    def test_log_button_counts_events(self) -> None:
        """状态栏上那个按钮带条数 —— 「有没有新东西可看」不该要点开才知道。"""
        app, _bridge = self._app()
        self.assertEqual(app.log_btn.cget("text"), "Log")
        app.do_dry_run()
        self.assertEqual(app.log_btn.cget("text"), "Log (%d)" % EXPECTED_EVENTS)

    def test_pressing_log_twice_reuses_the_same_window(self) -> None:
        """按第二次 Log 是"我要看日志"，不是"我要两份日志"。

        开第二扇的后果不是难看：`_pump()` 只刷新 `self._log` 那一扇，
        另一扇从此永远停在打开的那一刻，而它看起来跟活的一模一样。
        """
        app, _bridge = self._app()
        first = app.show_log()
        second = app.show_log()
        self.assertIs(first, second)

    def test_masked_copy_is_wired_to_the_same_document(self) -> None:
        """「Copy for sharing」拷的是同一份文本，只是过了一遍脱敏表。"""
        app, bridge = self._app()
        app.do_dry_run()
        window = app.show_log()
        masked = gui_state.redact(window.document(), bridge.redaction_map())
        for value in SITE_VALUES:
            self.assertNotIn(value, masked)
        for kept in STRUCTURE_KEPT:
            self.assertIn(kept, masked)


# ==========================================================================
# 5. 一行一条事件的排版
# ==========================================================================


class LogLine(unittest.TestCase):
    """`_log_line` —— 多行 message 的续行不许把列对齐带跑。"""

    def _event(self, message: str) -> DriverEvent:
        return DriverEvent(
            kind=EventKind.PLANNED,
            message=message,
            run_id="fake/run/id",
            at="2026-08-20T12:34:56Z",
        )

    def test_single_line_message_stays_on_one_line(self) -> None:
        from gui import _ui

        line = _ui._log_line(7, self._event("stage 2: fake --all"))
        self.assertEqual(len(line.splitlines()), 1)
        self.assertIn("12:34:56", line)
        self.assertIn("planned", line)
        self.assertIn("fake/run/id", line)
        self.assertTrue(line.endswith("stage 2: fake --all"))

    def test_multi_line_message_indents_its_continuation(self) -> None:
        from gui import _ui

        line = _ui._log_line(7, self._event("it broke\n  Next: do this"))
        parts = line.splitlines()
        self.assertEqual(len(parts), 2)
        self.assertTrue(parts[0].endswith("it broke"))
        self.assertTrue(
            parts[1].startswith(_ui.LOG_CONT_INDENT),
            "续行没缩进 —— 整份日志的列对齐会被它带跑",
        )
        self.assertIn("Next: do this", parts[1])

    def test_multi_line_message_indents_its_continuation_negative(self) -> None:
        """反向：单行 message **不许**被拆成两行（否则上面那条在"永远拆行"时也绿）。"""
        from gui import _ui

        line = _ui._log_line(7, self._event("one line only"))
        self.assertNotIn(_ui.LOG_CONT_INDENT, line)


if __name__ == "__main__":  # pragma: no cover - 手工入口
    unittest.main()
