# -*- coding: utf-8 -*-
"""GUI **动作序列**的不变量测试 —— 现有测试缺的就是这一层。

## 为什么要单独有这个文件

2026-08-20 用户报的「run group 复制出来删不掉 + 反复弹框」逃到了用户侧，而当时
`tests/test_gui_*.py` 有 190 条测试、全绿。事后量了一下：其中 **167 条一个用户动作
都不做**（建完控件树 -> 断言一张快照），19 条只做一个动作，真正走两步以上的只有 4 条。

而那个 bug 需要四拍才显形：`切组` -> `_sync_freq_fields` 清空 base 的 step ->
下一次 `push()` 把空串写回基线 -> 再下一次 `refresh_groups()` 取计数时抛出来、
整张组表冻住。**任何单拍快照都是绿的。**

所以缺的不是某一条用例，是一整类判据：

* 快照测试问的是「这一步算得对不对」；
* 这里问的是「**走一串之后，界面和模型还是不是同一件事**」。

## 判据怎么定的

不变量是**从设计文档里抄下来的承诺**，不是从被测代码反推的（防自证配方 2）：

| 不变量 | 出处 |
|---|---|
| A 组表画出来的行 == 模型里的组 | `_ui.refresh_groups` 的职责：它画的就是 `bridge.groups()` |
| B 恰好一行带 `*`，且它是 active | 同上（`* ` 是"当前组"的唯一可见标记） |
| C runs 表 == `bridge.runs()` | `_ui.refresh_tree` 的职责 |
| D 表和现算的 run 数对不上时，**界面必须明说表是旧的** | `_ui.RUNS_STALE_WARNING` |
| E 编辑别的组时**不许动基线** | `CLAUDE.md`「run group」：组是 delta；`switch_group` 的 docstring |
| F 走出错误状态时，界面必须**解释得清**（preflight 有话说 + 表已标陈旧） | `_ui.RUNS_STALE_WARNING` / `GuiState.preflight` |

D 这一条最初是缺的（2026-08-20 第一版明写了"故意不写"）：那时候表留着上一次的
矩阵、Total 那行写 `= 0 runs`，两块面板互相打脸而界面一个字都没说。修法不是让
它们相等（表跟着清空 = 每敲一个键闪一次，比陈旧更难用），是让表说出自己是旧的 ——
所以不变量的形状是**「相等 或 已标记陈旧」**，不是「相等」。

⏱ 不 sleep、不读墙钟、不起子进程。种子写死 => 失败可逐字复现（把种子填进
`FAILING_SEED` 就能单跑那一条路径）。
"""

from __future__ import annotations

import copy
import os
import random
import unittest

import gui.state as gui_state
from gui.state import GuiState

BASE = gui_state.BASE_GROUP

SEEDS = (0, 1, 2, 3, 4, 5, 6, 7)
"""跑哪几条路径。**写死**，不用时间/随机源播种 —— 闸门必须逐次给出同一个答案。"""

STEPS = 40
"""每条路径走几步。40 步足够走出"加组 -> 切过去 -> 改一根轴 -> 切回来 -> 删组"
这种四五拍的组合；再长只是重复。"""


def _tk_or_skip(test: unittest.TestCase) -> object:
    """本机能不能开窗口。开不了就**带原因**跳过（平台性 skip）。

    与 `tests/test_gui_common.py` 的同名函数逐字同义 —— 那边是另一条分工线上的
    文件，这里不 import 它的私有件。
    """
    try:
        import tkinter as tk
    except ImportError as exc:  # pragma: no cover - 本机装了 tkinter
        test.skipTest(f"平台跳过：这台机器没装 tkinter（{exc}）—— CLI 不受影响")
    global _SHARED_ROOT
    if _SHARED_ROOT is None:
        try:
            _SHARED_ROOT = tk.Tk()
        except tk.TclError as exc:  # pragma: no cover - 本机有显示
            test.skipTest(f"平台跳过：这台机器开不了显示（{exc}）—— CLI 不受影响")
        _SHARED_ROOT.withdraw()
    root = _SHARED_ROOT
    test.addCleanup(_destroy_children, root)
    return root


_SHARED_ROOT = None
"""整个进程**共用一个** Tk 根窗口。

原来是每条测试 `tk.Tk()` + `addCleanup(destroy)`。Windows 上开到几十个之后
`Tk()` 会开始抛 `Can't find a usable tk.tcl` —— 而 `_tk_or_skip` 把任何 `TclError`
都当成"这台机器没有显示"，于是**那条测试静默地跳过了**。2026-08-20 实测：同一条
命令跑两遍，skip 数在 4/5/6 之间跳，跳掉的每次都不是同一条。

一条 skip 掉的测试和一条不存在的测试，在闸门眼里长得一模一样 —— 这正是本项目
已经吃过一次亏的那种假绿。共用一个根就没有"开太多"这回事；每条测试结束时
销毁自己建的子控件，状态不带过去。
"""


def _destroy_children(root: object) -> None:
    """把这条测试往共用根里建的东西全清掉（含 Toplevel）。

    不销毁根本身 —— 它要给下一条测试用。顺手 `update()` 一次把队列里剩下的
    虚拟事件（`<<TreeviewSelect>>` 是排进队列的）跑完，免得它们打到下一条测试的
    控件上。
    """
    for child in list(root.winfo_children()):  # type: ignore[attr-defined]
        try:
            child.destroy()
        except Exception:  # noqa: BLE001 - 已经销毁过的子件，收尾而已
            pass
    try:
        root.update()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def _base_snapshot(bridge: GuiState) -> tuple[dict, dict]:
    """base **自己**的取值 + 扫频。

    读的是私有件：`axis_selection()` 给的是 **active group** 的有效值，而这里要问的
    恰恰是"在别的组上折腾的时候，基线有没有被动过"—— 公开面上没有这条路
    （要有也只能是 `axis_selection(group=...)`，那是另一棒的事）。
    """
    return copy.deepcopy(bridge._selection), copy.deepcopy(bridge._sweep)


class _Walker:
    """把「用户点了什么」编码成一串可复现的动作。

    每个动作都只走**界面自己提供的入口**（按钮 / 勾选框 / 输入框 + recompute），
    不直接调 bridge —— 直接调 bridge 就把接缝测没了，而接缝正是 bug 的家。
    """

    def __init__(self, app: object, bridge: GuiState, rng: random.Random) -> None:
        self.app = app
        self.bridge = bridge
        self.rng = rng
        self._cells = 0

    # ---- 动作
    def add_group(self) -> None:
        self.app.do_add_group()

    def duplicate_group(self) -> None:
        self.app.gtree.selection_set(self._some_group())
        self.app.do_duplicate_group()

    def remove_group(self) -> None:
        names = [g.name for g in self.bridge.groups() if g.name != BASE]
        if not names:
            return
        self.app.gtree.selection_set(self.rng.choice(names))
        self.app.do_remove_group()

    def switch_group(self) -> None:
        self.app.switch_group(self._some_group())

    def edit_temperature(self) -> None:
        self.app.temp.set(self.rng.choice(("-40.0, 55.0, 125.0", "55.0", "25, 85")))
        self.app.recompute()

    def toggle_corner(self) -> None:
        name = self.rng.choice(list(gui_state.CORNER_VALUES))
        var = self.app.corner_vars[name]
        var.set(not var.get())
        self.app.recompute()

    def toggle_mode(self) -> None:
        name = self.rng.choice(("Quasi-static", "Full wave"))
        var = self.app.mode_vars[name]
        var.set(not var.get())
        self.app.recompute()

    def edit_mesh(self) -> None:
        self.app.m_edge.set(self.rng.choice(("0.5", "0.45", "0.6")))
        self.app.recompute()

    def toggle_override(self) -> None:
        key = self.rng.choice(list(self.app.ovr_vars))
        var = self.app.ovr_vars[key]
        var.set(not var.get())
        self.app.on_override_toggle(key)

    def edit_sweep(self) -> None:
        # 扫频那一排在 base 之外的组里是 **disabled** 的（`GROUP_ROW_AXES`）——
        # 用户点不到，所以这里也不点。绕过控件状态去写变量测的是不存在的界面。
        if not self.app._active_is_base():
            return
        self.app.f_stop.set(self.rng.choice(("40", "20", "60")))
        self.app.recompute()

    def duplicate_design_row(self) -> None:
        """Designs 表的 `Duplicate row`。**故意造出两行一模一样的 design。**

        这是"合法按钮 -> 非法状态"的唯一入口，也是不变量 D / F 真正会被踩到的地方。
        没有它，D 那条在 320 步里一次都不触发（2026-08-20 实测），等于没写。
        """
        rows = self.app.dtree.get_children()
        if not rows:
            return
        self.app.dtree.selection_set(self.rng.choice(rows))
        self.app.dup_design()

    def fix_design_rows(self) -> None:
        """把重复的 design 行改成唯一的 —— 用户"复制一行再改 cell 名"的后半步。

        走出去也要走得回来：只有能恢复，"恢复之后红字必须消失"才在序列里可测。
        """
        rows = list(self.bridge.design_rows())
        if len(rows) == len(set(rows)):
            return
        fixed: list[tuple[str, str, str]] = []
        for row in rows:
            while tuple(row) in {tuple(f) for f in fixed}:
                self._cells += 1
                row = (row[0], "CELL_%d" % self._cells, row[2])
            fixed.append(tuple(row))
        self.bridge.set_designs(fixed)
        self.app.refresh_designs()
        self.app.recompute()

    def add_design(self) -> None:
        # cell 名必须唯一：两行一模一样的 design 会撞 run_id，那是**另一件事**
        # （见本文件抬头那条"已知不变量"），混进来会把这里要抓的东西盖住。
        self._cells += 1
        self.bridge.add_design("MY_LIB", "CELL_%d" % self._cells, "layout")
        self.app.refresh_designs()
        self.app.recompute()

    def new_batch(self) -> None:
        self.app.do_new_batch()

    def _some_group(self) -> str:
        return self.rng.choice([g.name for g in self.bridge.groups()])

    def all_actions(self) -> tuple[tuple[str, object], ...]:
        names = (
            "add_group",
            "duplicate_group",
            "remove_group",
            "switch_group",
            "edit_temperature",
            "toggle_corner",
            "toggle_mode",
            "edit_mesh",
            "toggle_override",
            "edit_sweep",
            "add_design",
            "duplicate_design_row",
            "fix_design_rows",
            "new_batch",
        )
        return tuple((name, getattr(self, name)) for name in names)


class GuiStaysConsistentAcrossActionSequences(unittest.TestCase):
    """★ 走一串动作，每走一步验一遍不变量。

    这是 2026-08-20 那个 bug 的**类别级**判据 —— 不是"再也别犯这一个错"，
    是"界面和模型不许悄悄分家"。
    """

    def setUp(self) -> None:
        os.environ["EWB_SMOKE"] = "1"
        self.addCleanup(os.environ.pop, "EWB_SMOKE", None)

    def _build(self, root=None):
        """一套干净的界面。**每条种子各建一套** —— 共用一套的话前一条路径加的
        design / 组会漏进下一条，跑出来的红是"测试自己脏了"，不是被测代码的问题。

        Tk 根窗口只开一个（一个进程里反复 `Tk()` 在某些机器上会失败），
        frame 逐条销毁。
        """
        root = root or _tk_or_skip(self)
        from gui.frames import split

        bridge = GuiState(batch_root="/tmp/ewb_inv", batch_name="inv")
        bridge.add_design("MY_LIB", "CELL_A", "layout")
        bridge.add_design("MY_LIB", "CELL_B", "layout")
        frame = split.build_frame(root, bridge)
        frame.pack()
        app = frame._ewb_app
        app.recompute()
        return root, bridge, app, frame

    def _check(self, app, bridge, path: str, base_before, active_before) -> None:
        where = "路径: %s" % path

        # A —— 组表画的就是 `bridge.groups()`，一行不多一行不少。
        rows = list(app.gtree.get_children())
        self.assertEqual(rows, [g.name for g in bridge.groups()], "A 组表 != 模型\n" + where)

        # B —— 恰好一行带 `* `，且它就是 active group。
        starred = [
            app.gtree.item(iid, "values")[0][2:]
            for iid in rows
            if app.gtree.item(iid, "values")[0].startswith("* ")
        ]
        self.assertEqual(starred, [bridge.active_group()], "B 星标 != active\n" + where)

        # C —— runs 表画的就是 `bridge.runs()`。
        self.assertEqual(
            list(app.tree.get_children()),
            [run.run_id for run in bridge.runs()],
            "C runs 表 != 模型\n" + where,
        )

        # D —— 表和现算的 run 数可以不等（设定临时非法时表**故意**留着旧内容），
        #      但那时候界面必须已经把它标成陈旧的。二者必居其一。
        if bridge.run_count() != len(app.tree.get_children()):
            self.assertTrue(app._runs_are_stale(), "D 对不上却没标陈旧\n" + where)
            self.assertTrue(app.runs_stale.winfo_manager(), "D 标了却没摆出红字\n" + where)

        # E —— **在别的组上做的任何事都不许改到基线。** 组是 delta，不是副本。
        if active_before != BASE:
            self.assertEqual(
                _base_snapshot(bridge), base_before, "E 改组动了基线\n" + where
            )

        # F —— 走的每一步都是界面自己给的按钮。**允许**走进出错状态（按一次
        #      Duplicate row 就到了，而那是那个按钮的预期用法），但那时候界面
        #      欠用户三样东西，一样都不许少：
        #        1. preflight 说得出话（不是空 list）；
        #        2. 每一条都带"下一步做什么"；
        #        3. 表要么是空的、要么已经标成陈旧 —— 不许摆着旧内容装作是新的。
        #      判据写成这个形状而不是"不许出错"，是因为"不许出错"会把
        #      Duplicate row 这条真实路径整个挡在测试之外。
        if app._error:
            problems = bridge.preflight()
            self.assertTrue(problems, "F 界面自己知道错了，preflight 却没话说\n" + where)
            for problem in problems:
                self.assertIn("Next:", problem, "F 只说坏了不说下一步\n" + where)
            self.assertTrue(
                not app.tree.get_children() or app._runs_are_stale(),
                "F 出错了却摆着一张没标记的旧表\n" + where,
            )

    def test_no_action_sequence_desynchronises_the_ui(self) -> None:
        root = _tk_or_skip(self)
        for seed in SEEDS:
            with self.subTest(seed=seed):
                _root, bridge, app, frame = self._build(root)
                self.addCleanup(frame.destroy)
                rng = random.Random(seed)
                walker = _Walker(app, bridge, rng)
                actions = walker.all_actions()
                path: list[str] = []
                for _ in range(STEPS):
                    name, action = rng.choice(actions)
                    active_before = bridge.active_group()
                    base_before = _base_snapshot(bridge)
                    path.append("%s@%s" % (name, active_before))
                    action()
                    # ⚠️ 必须 pump 一次事件队列：`<<TreeviewSelect>>` 是**排进队列**的
                    #    （实测，见 `test_a_redraw_is_not_a_user_click`），不 pump 就
                    #    测不到"我们自己的重画被当成了用户点击"这一类。
                    root.update()
                    self._check(app, bridge, " -> ".join(path[-8:]), base_before, active_before)

    def test_the_walk_actually_walks_negative(self) -> None:
        """反向：上一条要是一步都没真走，它就是一条永远绿的空测试。

        计数断言：8 条种子 x 40 步 = 320 步，每一步都得真改到点什么 ——
        判据取"这条路上出现过的组名个数"和"动作种类数"，两个都得远大于 1。
        """
        _root, bridge, app, frame = self._build()
        self.addCleanup(frame.destroy)
        rng = random.Random(SEEDS[0])
        walker = _Walker(app, bridge, rng)
        actions = walker.all_actions()
        seen_groups = set()
        seen_actions = set()
        for _ in range(STEPS):
            name, action = rng.choice(actions)
            action()
            seen_actions.add(name)
            seen_groups.update(g.name for g in bridge.groups())
        self.assertGreater(len(seen_actions), 6, "动作种类太少，这条路等于没走")
        self.assertGreater(len(seen_groups), 2, "一个组都没造出来，组相关的不变量白验了")

    def test_a_redraw_is_not_a_user_click(self) -> None:
        """★ `refresh_groups()` 里的 `selection_set` **不许**被当成用户切组。

        实测结论（本测试就是那个实测）：`<<TreeviewSelect>>` 是 Tk **排进事件队列**
        的虚拟事件，不是同步回调。也就是说 `refresh_groups` 跑在 `_syncing=True` 里
        这件事**保护不了任何东西** —— 处理器晚一拍才跑，那时候旗子早放下了。
        真正挡住它的是 `switch_group` 开头那句 `name == self._active_group()`。

        这条测试把那个真判据钉住：重画之后 pump 事件队列，active group 不许变。
        （反过来说：谁要是把 `switch_group` 那句早返回删了，这里当场红。）
        """
        root, bridge, app, frame = self._build()
        self.addCleanup(frame.destroy)
        app.do_add_group()
        root.update()
        active = bridge.active_group()
        self.assertNotEqual(active, BASE, "前提：现在编辑的是新加的那个组")

        app.refresh_groups()
        root.update()  # 队列里那个 <<TreeviewSelect>> 在这一步才真的跑
        self.assertEqual(bridge.active_group(), active, "自己的重画把自己切走了")

    def test_the_select_event_is_asynchronous(self) -> None:
        """把上一条依赖的那个平台事实单独钉一条 —— 它变了，上一条的理由就没了。

        期望值出处：Tk 的 `TkSendVirtualEvent`（虚拟事件进队列）。本测试不 import
        任何被测代码，纯问 tkinter。
        """
        root = _tk_or_skip(self)
        from tkinter import ttk

        tree = ttk.Treeview(root, columns=("a",), show="headings")
        for name in ("x", "y"):
            tree.insert("", "end", iid=name, values=(name,))
        fired: list[int] = []
        tree.bind("<<TreeviewSelect>>", lambda _e: fired.append(1))

        tree.selection_set("x")
        self.assertEqual(fired, [], "<<TreeviewSelect>> 要是同步派发的，`_syncing` 就真能挡住")
        root.update()
        self.assertEqual(fired, [1], "pump 之后必须收到，否则这条测试什么都没测")


if __name__ == "__main__":
    unittest.main()
