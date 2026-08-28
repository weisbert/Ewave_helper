# -*- coding: utf-8 -*-
"""「一个 run 失败了，人能不能看见为什么」—— 从磁盘到界面的整条路。

## 这份文件为什么存在

2026-08-28，用户在他师傅的机器上部署之后报回来一句：

> 「run 的时候 fail 了，而且不知道为什么 fail，dry run 没有问题」
> 「我根本不知道返回的错到底是什么……要实时的 log，也能返回 ewave 的报错」

事后查出来这不是一个 bug，是**三段路各断了一截**：

| 断在哪 | 断了什么 | 本文件的判据 |
|---|---|---|
| `core.logparse` | 没有"只属于这一个 run 的日志清单"，也没有"读末尾" | `LogFiles` / `LogTail` |
| `sched.driver` | 失败原因只说得出"产物验不过"，从不去读 eWave 自己的日志 | `DriverSaysWhatEwaveSaid` |
| `gui` | `Run.message` 从头到尾没有任何控件显示它 | `ReasonIsOnScreen` / `RunLogWindow` |

三截都补上了才算答完那句话，所以三层放在一个文件里 —— 它们是**同一条判据**的三段，
拆开之后没有任何一段能单独证明"人看得见了"。

## 四条配方（`docs/OVERNIGHT.md`）在这里的落点

* **期望值来源** = 手写字面量。日志正文是本文件顶上那几个 `FAKE_*` 常量，
  崩溃那三行抄自 `sched.fake._LOG_CRASH`（它自己抄自 BRIEF §10 实测）——
  **不从被测代码算一遍**；
* **反向验证** = 每条正向都配一条 `_negative`，共用同一条构造路径，只改一个入参
  （日志放在邻居目录 / 换成成功的那个 run / message 清空）；
* **计数断言** = 文件条数、尾巴行数、失败 run 数逐个等于手写值；
* ⏱ 全程不 sleep、不起 eWave：假 runner 写假日志（硬约束 3）。

🚨 本文件零站点标识符：library / cell / view / 路径全是显式假值。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from ewave_batch.core import logparse
from ewave_batch.model import RunStatus
from ewave_batch.sched.driver import make_driver, run_batch
from ewave_batch.sched.fake import FakeFailureMode

from tests.test_driver import BATCH_MODES, RUN_IDS, _build

# --------------------------------------------------------------------------
# 手写的假值
# --------------------------------------------------------------------------

FAKE_EWAVE_DIR = "typical_-40_0"
FAKE_NEIGHBOUR_DIR = "typical_125_0"
"""同一个 run_dir 底下的**另一个** corner/temp 组合。它的日志一个字都不许被借走。"""

FAKE_RUN_LOG = "run_typical_-40_0.log"
"""我们自己捕获的那份 stdout（`model.RUN_LOG_TEMPLATE`）。它在 run_dir 里，
不在 `<corner>_<temp>/` 里 —— 这正是"只扫 ewave_dir"会漏掉它的原因。"""

FAKE_CRASH_LINE = (
    "[error] eWave exit failed! Failed to execute emsolver, please contact the manufacturer."
)
"""崩溃日志里那一行 `[error]`。手抄自 `sched.fake._LOG_CRASH` 第三行
（它自己抄自 BRIEF §10 step3 的实测输出）。**这就是用户要看见的那句话。**"""

FAKE_NEIGHBOUR_LINE = "[error] this line belongs to the neighbour and must never be borrowed"
"""编的。放进邻居目录，用来证明"没串目录"。"""

EXPECTED_RUN_LOG_FILE_COUNT = 2
"""`run_log_files` 该给出几个文件：`<ewave_dir>/ewave.log` + run_dir 里那份 stdout。
手数的 —— 邻居那份**不算**。"""

EXPECTED_FALLBACK_FILE_COUNT = 3
"""同一棵树、只给 `run_dir`（退路）时会拿到几个：上面那 2 个 + 邻居的 1 个 = 3。
这个数字**就是**"不许拿 run_dir 一把梭"那条规矩的代价，反向验证盯的就是它。"""

TAIL_LINES = 200
"""造多长一份日志给 `read_log_tail` 截。"""

CRASHED_RUN_ID = "dA/base/typical_25_0"
"""`tests.test_driver.BATCH_MODES` 里用 `EXIT_ZERO_BUT_CRASHED` 的那个 run（手抄）。
它 `exit=0`、什么产物都没有、日志里躺着崩溃三行 —— BRIEF §10 的原始现场。

⚠️ 那张表**不是默认值**：`_build()` 不给 `modes` 就是整批全成功。本节每条测试都得
显式把它传进去，否则"崩掉那个 run 的原因"这句话根本没有主语（下面 `_run_batch`
的默认参数就是这个）。"""

HEALTHY_RUN_ID = "dA/base/typical_-40_0"
"""同一批里正常成功的那个（手抄自 `RUN_IDS`）。对照组：它的 Reason 必须是空的。"""


def _write(path: str, text: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


class _Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def root(self) -> str:
        return self._tmp.name.replace("\\", "/")


# ==========================================================================
# 1. logparse：这一个 run 的日志有哪些
# ==========================================================================


class LogFiles(_Tmp):
    """`logparse.run_log_files` —— **只属于这一个 run 的**日志清单。"""

    def _tree(self) -> str:
        """一棵有邻居的 run 目录树。正反两向共用它，只改传进去的参数。"""
        run_dir = f"{self.root()}/rundir"
        _write(f"{run_dir}/{FAKE_EWAVE_DIR}/ewave.log", FAKE_CRASH_LINE + "\n")
        _write(f"{run_dir}/{FAKE_NEIGHBOUR_DIR}/ewave.log", FAKE_NEIGHBOUR_LINE + "\n")
        _write(f"{run_dir}/{FAKE_RUN_LOG}", "captured stdout\n")
        return run_dir

    def test_it_takes_the_ewave_dir_plus_our_own_stdout_capture(self) -> None:
        """★ 计数断言：恰好 2 个文件，且邻居那份**不在里面**。"""
        run_dir = self._tree()
        files = logparse.run_log_files(
            ewave_dir=f"{run_dir}/{FAKE_EWAVE_DIR}",
            run_log=f"{run_dir}/{FAKE_RUN_LOG}",
            run_dir=run_dir,
        )
        self.assertEqual(len(files), EXPECTED_RUN_LOG_FILE_COUNT)
        self.assertIn(f"{run_dir}/{FAKE_EWAVE_DIR}/ewave.log", files)
        self.assertIn(f"{run_dir}/{FAKE_RUN_LOG}", files)
        self.assertNotIn(f"{run_dir}/{FAKE_NEIGHBOUR_DIR}/ewave.log", files)

    def test_it_takes_the_ewave_dir_plus_our_own_stdout_capture_negative(self) -> None:
        """反向：同一棵树，**只给 run_dir**（退路）⇒ 邻居的日志被一起捞进来。

        这条不是在测一个 bug，是把"为什么必须传 ewave_dir"钉成可执行的判据：
        少了这条，上面那条在实现改成"一律扫 run_dir"之后照样绿，
        而那时界面会**静默地**报出一份张冠李戴的收敛结论。
        """
        run_dir = self._tree()
        files = logparse.run_log_files(run_dir=run_dir)
        self.assertEqual(len(files), EXPECTED_FALLBACK_FILE_COUNT)
        self.assertIn(f"{run_dir}/{FAKE_NEIGHBOUR_DIR}/ewave.log", files)

    def test_the_authoritative_log_sorts_first(self) -> None:
        """`ewave.log` 排在我们那份 stdout 前面 —— 界面默认显示第一个。"""
        run_dir = self._tree()
        files = logparse.run_log_files(
            ewave_dir=f"{run_dir}/{FAKE_EWAVE_DIR}", run_log=f"{run_dir}/{FAKE_RUN_LOG}"
        )
        self.assertTrue(files[0].endswith("ewave.log"), files)

    def test_a_run_that_never_started_has_no_logs_and_that_is_not_an_error(self) -> None:
        """作业还在排队 ⇒ 空元组，**不抛**。界面据此显示"还没有日志"。"""
        self.assertEqual(
            logparse.run_log_files(
                ewave_dir=f"{self.root()}/nope", run_log=f"{self.root()}/nope.log"
            ),
            (),
        )


# ==========================================================================
# 2. logparse：读末尾
# ==========================================================================


class LogTail(_Tmp):
    """`logparse.read_log_tail` —— 给"实时看"用的那一段。"""

    def _long_log(self) -> str:
        path = f"{self.root()}/long.log"
        _write(path, "".join("line %d\n" % index for index in range(TAIL_LINES)))
        return path

    def test_it_keeps_the_end_and_drops_the_half_line_at_the_top(self) -> None:
        """末尾在、开头不在、而且第一行是**完整的一行**。

        第三条是重点：`seek` 落点几乎必然在某一行中间，留着那半截就是让人读半句话 ——
        而半句日志看起来像一条完整的、内容很怪的日志。
        """
        text = logparse.read_log_tail(self._long_log(), limit_bytes=200)
        self.assertIn("line %d" % (TAIL_LINES - 1), text)
        self.assertNotIn("line 0\n", text)
        body = [line for line in text.splitlines() if not line.startswith("<...")]
        self.assertTrue(body[0].startswith("line "), body[:2])

    def test_it_keeps_the_end_and_drops_the_half_line_at_the_top_negative(self) -> None:
        """反向：限额大于文件 ⇒ 一个字都不截，也没有那行 `<...>` 抬头。"""
        text = logparse.read_log_tail(self._long_log(), limit_bytes=1024 * 1024)
        self.assertIn("line 0\n", text)
        self.assertNotIn("<...", text)

    def test_it_strips_the_colour_codes(self) -> None:
        """eWave 即使 `--nogui` 也打色码，而 tkinter 的 Text 不认 escape。"""
        path = f"{self.root()}/ansi.log"
        _write(path, "\x1b[31m" + FAKE_CRASH_LINE + "\x1b[0m\n")
        text = logparse.read_log_tail(path)
        self.assertIn(FAKE_CRASH_LINE, text)
        self.assertNotIn("\x1b", text)

    def test_a_missing_file_says_so_instead_of_raising(self) -> None:
        """**不抛**：这个函数按轮询间隔被反复调用，"文件还没生成"是正常状态。"""
        text = logparse.read_log_tail(f"{self.root()}/not_there.log")
        self.assertIn("cannot read", text)


# ==========================================================================
# 3. driver：把 eWave 自己说的话带进失败原因
# ==========================================================================


class DriverSaysWhatEwaveSaid(_Tmp):
    """一个 run 失败之后，`Run.message` 里有没有 eWave 自己的原话。"""

    def _run_batch(self, **kwargs: object) -> dict[str, str]:
        """跑完一整批，返回 run_id → message。正反两向共用（只改 `modes` 一个入参）。"""
        kwargs.setdefault("modes", dict(BATCH_MODES))
        batch = _build(self.root(), **kwargs)  # type: ignore[arg-type]
        run_batch(batch.driver(), poll_interval=0.0, max_seconds=120.0)
        self._batch = batch
        return {run.run_id: run.message for run in batch.state.runs}

    def test_the_crashed_run_quotes_the_error_line_from_ewaves_own_log(self) -> None:
        """★ 关键测试：崩掉那个 run 的失败原因里**逐字**带着 `[error] eWave exit failed!`。

        在这之前它只说得出"产物验不过" —— 逐字正确，而对"我该改什么"零信息量。
        """
        messages = self._run_batch()
        message = messages[CRASHED_RUN_ID]
        self.assertIn("eWave's own log says", message)
        self.assertIn("eWave exit failed!", message)

    def test_the_crashed_run_quotes_the_error_line_from_ewaves_own_log_negative(self) -> None:
        """反向：同一条构造路径，把那个 run 改成成功 ⇒ 它 done，而且一个字的原因都没有。

        少了这条，上面那条在实现改成"给每个 run 都贴一句崩溃日志"时照样绿。
        """
        healthy = dict(BATCH_MODES)
        healthy[CRASHED_RUN_ID] = FakeFailureMode.SUCCESS
        messages = self._run_batch(modes=healthy)
        statuses = {run.run_id: run.status for run in self._batch.state.runs}
        self.assertIs(statuses[CRASHED_RUN_ID], RunStatus.DONE)
        self.assertEqual(messages[CRASHED_RUN_ID], "")

    def test_the_healthy_run_did_not_borrow_the_crashed_ones_error(self) -> None:
        """同一批里成功那个 run 的 message 必须是空的 —— 日志不许串 run。"""
        messages = self._run_batch()
        self.assertEqual(messages[HEALTHY_RUN_ID], "")

    def test_the_facts_are_written_into_the_state_so_status_can_show_them(self) -> None:
        """`Run.log_facts` 被填上了 —— `runs.csv` 的 conv / peakMB 两列靠它。

        计数断言：12 个 run 一个都不许漏（`layout.write_runs_csv` 按 run 读它）。
        """
        self._run_batch()
        filled = [run.run_id for run in self._batch.state.runs if run.log_facts is not None]
        self.assertEqual(len(filled), len(RUN_IDS))

    def test_reading_the_log_never_decides_the_verdict(self) -> None:
        """🚨 日志只作诊断：崩掉那次日志里三行 `[error]`，而它的判据仍然是产物。

        判法：把 `done` 的那些 run 的 `log_facts.ok` 拿出来看 —— 有 `ok=True` 的，
        也允许有 `None`（"日志没说"），但**不许**出现一个 `ok is False` 却 `done` 的。
        这条守的是 BRIEF §10：`done` 的唯一判据是 `verify_run_outputs`。
        """
        self._run_batch()
        for run in self._batch.state.runs:
            if run.status is RunStatus.DONE and run.log_facts is not None:
                self.assertIsNot(run.log_facts.ok, False, run.run_id)


# ==========================================================================
# 4. 界面：Reason 那一行 + Run log 那扇窗
# ==========================================================================


def _gui_modes() -> dict[str, FakeFailureMode]:
    """界面那半用的失败模式表 = `tests.test_gui_common.GUI_MODES`（键是**那边的** run_id）。

    刻意复用而不是再写一张：两张表会漂，而漂了之后本节会**静默地**退化成
    "一批全成功的批次里找失败的 run"，然后在 `_first_failed` 里 fail 出一句
    看起来像构造错误的话。
    """
    from tests.test_gui_common import GUI_MODES

    return dict(GUI_MODES)


def _tk_or_skip(case: unittest.TestCase) -> object:
    """本机能不能开窗口。**复用 `tests.test_gui_common` 那个进程级共用根。**

    🚨 别在这里自己 `tk.Tk()`：Windows 上开到几十个之后 `Tk()` 开始抛
    `Can't find a usable init.tcl`，而那被当成"这台机器没有显示"⇒ 测试**静默地跳过**
    （那边的 `_SHARED_ROOT` 注释记着 2026-08-20 的实测：skip 数在 4/5/6 之间跳）。
    本文件加了十来条建窗口的测试，正好把这个上限撞出来 —— 撞过一次了。
    """
    from tests.test_gui_common import _tk_or_skip as shared

    return shared(case)


class _GuiCase(_Tmp):
    """建控件树的测试：`EWB_SMOKE=1` 保证不弹模态框、新开的窗建了也不露脸。"""

    def setUp(self) -> None:
        super().setUp()
        os.environ["EWB_SMOKE"] = "1"
        self.addCleanup(os.environ.pop, "EWB_SMOKE", None)

    def _app(self, **kwargs: object) -> object:
        """跑完一整批 → 建 split 界面 → 返回 app。正反两向共用这一条路。

        走 `tests.test_gui_common._gui` 那条**界面**路，而不是 driver 那条：
        本节要证明的是"界面显示得出来"，用 driver 的 state 手搓一个 bridge
        就把接缝测没了。
        """
        from tests.test_gui_common import _gui
        from gui.frames import split

        root = _tk_or_skip(self)
        bridge, _runner, _sched = _gui(self.root(), **kwargs)  # type: ignore[arg-type]
        bridge.start()
        for _ in range(200):
            report = bridge.tick()
            if report is None or report.finished:
                break
        app = split.build_frame(root, bridge)._ewb_app
        return app

    def _select(self, app: object, run_id: str) -> None:
        app.tree.selection_set(run_id)  # type: ignore[attr-defined]
        app.show_detail()  # type: ignore[attr-defined]

    def _first_failed(self, app: object) -> str:
        for run in app.bridge.runs():  # type: ignore[attr-defined]
            if run.status is RunStatus.FAILED and run.message:
                return run.run_id
        self.fail("这一批里没有带失败原因的 run —— 构造路径变了")
        return ""

    def _first_done(self, app: object) -> str:
        for run in app.bridge.runs():  # type: ignore[attr-defined]
            if run.status is RunStatus.DONE:
                return run.run_id
        self.fail("这一批里一个 done 都没有 —— 构造路径变了")
        return ""


class ReasonIsOnScreen(_GuiCase):
    """`Selected run -> Reason` —— 用户那句「我根本不知道返回的错到底是什么」的正面答案。"""

    def test_a_failed_run_shows_its_reason_in_the_detail_box(self) -> None:
        """★ 关键测试：选中一个失败的 run，Reason 框里逐字是 `Run.message`。"""
        app = self._app(modes=_gui_modes())
        run_id = self._first_failed(app)
        self._select(app, run_id)
        shown = app.reason_text.get("1.0", "end-1c")  # type: ignore[attr-defined]
        expected = " ".join(app.bridge.run(run_id).message.split())  # type: ignore[attr-defined]
        self.assertEqual(shown, expected)
        self.assertNotEqual(shown.strip(), "")
        self.assertTrue(app._reason_shown)  # type: ignore[attr-defined]

    def test_a_failed_run_shows_its_reason_in_the_detail_box_negative(self) -> None:
        """反向：同一条路，选中一个 **done** 的 run ⇒ 整行收起来。

        一个常驻的空红框会让人以为"失败了但没写原因"，而真相是这条根本没失败。
        """
        app = self._app(modes=_gui_modes())
        self._select(app, self._first_done(app))
        self.assertFalse(app._reason_shown)  # type: ignore[attr-defined]

    def test_the_reason_survives_a_poll_tick(self) -> None:
        """`_refresh_selection` 每拍都跑一次 —— 它不许把已经显示出来的原因擦掉。"""
        app = self._app(modes=_gui_modes())
        self._select(app, self._first_failed(app))
        app._refresh_selection()  # type: ignore[attr-defined]
        self.assertTrue(app._reason_shown)  # type: ignore[attr-defined]
        self.assertNotEqual(app.reason_text.get("1.0", "end-1c").strip(), "")  # type: ignore[attr-defined]


class RunLogWindow(_GuiCase):
    """`Run log` 那扇窗 —— 用户那句「要实时的 log，也能返回 ewave 的报错」的正面答案。"""

    def test_it_shows_the_selected_runs_own_ewave_log(self) -> None:
        """★ 关键测试：窗里逐字有 eWave 那行 `[error]`，抬头写着这个 run 和它的原因。"""
        app = self._app(modes=_gui_modes())
        run_id = self._first_failed(app)
        self._select(app, run_id)
        window = app.show_run_log()  # type: ignore[attr-defined]
        doc = window.document()
        self.assertIn(run_id, doc)
        self.assertIn("eWave exit failed!", doc)
        self.assertIn("# reason", doc)

    def test_it_shows_the_selected_runs_own_ewave_log_negative(self) -> None:
        """反向：同一条路，选中一个 **成功** 的 run ⇒ 那行崩溃日志一个字都不许出现。

        少了这条，上面那条在实现改成"把批次里所有日志拼起来"时照样绿 ——
        而那种窗口会让人拿着邻居的报错去查自己的 run。
        """
        app = self._app(modes=_gui_modes())
        self._select(app, self._first_done(app))
        doc = app.show_run_log().document()  # type: ignore[attr-defined]
        self.assertNotIn("eWave exit failed!", doc)

    def test_it_follows_the_selection_instead_of_pinning_to_one_run(self) -> None:
        """换一行，窗跟着换 —— 钉住的话它会**静默地**显示别人的日志。"""
        app = self._app(modes=_gui_modes())
        failed, done = self._first_failed(app), self._first_done(app)
        self._select(app, failed)
        window = app.show_run_log()  # type: ignore[attr-defined]
        self.assertIn(failed, window.document())
        self._select(app, done)
        self.assertIn(done, window.document())
        self.assertNotIn(failed, window.document())

    def test_the_right_click_menu_opens_it(self) -> None:
        """★ 用户点名的入口（2026-08-28）：「右键某个仿真项的时候，有个 output log」。

        判据有两半，各自单独都能绿着骗人：

        1. `Output log` 真的在右键菜单里，且**没被置灰**（`DISABLED_MENU_ITEMS` 里那些
           是"确实接不上"的，一个能按的按钮就是一句做得到的承诺）；
        2. 走 `on_row_action` 那条**真实回调**，窗真的开了、而且装着这一条的报错 ——
           只断言菜单里有这个词，等于验一个字符串。
        """
        from gui import _ui

        self.assertIn("Output log", _ui.MENU_ITEMS)
        self.assertNotIn("Output log", _ui.DISABLED_MENU_ITEMS)

        app = self._app(modes=_gui_modes())
        run_id = self._first_failed(app)
        app.tree.selection_set(run_id)  # type: ignore[attr-defined]
        app.on_row_action("Output log")  # type: ignore[attr-defined]
        self.assertIsNotNone(app._runlog)  # type: ignore[attr-defined]
        self.assertIn("eWave exit failed!", app._runlog.document())  # type: ignore[attr-defined]

    def test_the_right_click_menu_opens_it_negative(self) -> None:
        """反向：一行都没选中就走同一条回调 ⇒ **不许**开出一扇空窗。

        一扇写着"(no run selected)"的窗比不开更坏：它看起来像"这条没有日志"。
        """
        app = self._app(modes=_gui_modes())
        app.tree.selection_remove(app.tree.selection())  # type: ignore[attr-defined]
        app.on_row_action("Output log")  # type: ignore[attr-defined]
        self.assertIsNone(app._runlog)  # type: ignore[attr-defined]

    def test_only_one_window_no_matter_how_often_you_press_it(self) -> None:
        """按第二次是"我要看日志"，不是"我要两扇窗"——两扇里只有一扇会被刷新。"""
        app = self._app(modes=_gui_modes())
        self._select(app, self._first_failed(app))
        self.assertIs(app.show_run_log(), app.show_run_log())  # type: ignore[attr-defined]

    def test_copy_for_sharing_masks_the_site_names(self) -> None:
        """🚨 硬约束 1：日志里逐字带着 library / cell / 路径，拷出去之前必须脱敏。

        判据是脱敏表非空且 `redact` 真的动了文本 —— 与 Log 窗同一条路
        （`gui.state.redact` + `GuiState.redaction_map`），这里只验它接上了。
        """
        import gui.state as gui_state

        app = self._app(modes=_gui_modes())
        self._select(app, self._first_failed(app))
        doc = app.show_run_log().document()  # type: ignore[attr-defined]
        table = app.bridge.redaction_map()  # type: ignore[attr-defined]
        self.assertTrue(table)
        masked = gui_state.redact(doc, table)
        self.assertNotEqual(masked, doc)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
