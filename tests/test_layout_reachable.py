# -*- coding: utf-8 -*-
"""「界面上那几个按钮，人真的点得到吗」—— 按**窗口相对坐标**量。

## 这份文件为什么存在

2026-08-31，用户在红区部署完新版之后报回来：

> 「当我选中目标仿真后，下面的 Output log 被挤到看不见的地方了」

`ca12c09` 那一整套失败诊断（`Reason` 那一行 + `Output log` 那扇窗）在代码里全都在、
控件也全都建出来了，但在 **split 布局里它们落在窗口外面** ——
于是"这条 run 为什么失败"这个问题在界面上依旧无解，等于没交付。

病根是 pack 顺序：**pack 按调用顺序分配空间**，`runs` 带着 `rows=25` 的请求高度先 pack
且 `expand=True`，`detail` 排在它后面只能捡剩下的，而剩下的常常是负数。
`stacked.py` 2026-08-19 已经在同一个坑里栽过一次（那次掉出去的是整条动作栏，
`Submit` 点不到），可它自己也只修了一半：`detail` 写着 `side=BOTTOM`，却排在
designs/groups/settings/resources **之后**，那四块先把 body 吃光 ——
2026-08-31 实测 stacked 的 `Output log` 底边恒在 920px，窗口 900/850/800 三档全部掉在外面。

三版现在统一成一条纪律：**必须点得到的东西先 pack，能滚的表最后 pack。**

## 🚨 判据：`mapped == 1` **且** 底边在窗口里，两条缺一不可

这是本轮最值得记下来的一条。两个单独用都会放过真 bug：

* **只看 `winfo_ismapped()`** —— 实测（split，1560px 宽，选中一条 failed run，修复前）：

  | 窗口高 | `Output log` 底边 | `ismapped()` | 人点得到吗 |
  |---|---|---|---|
  | 900 | 893 | 1 | 能 |
  | 880 | 893 | **1** | **不能** —— 掉在窗口外 13px |
  | 800 | 893 | **1** | **不能** —— 掉在窗口外 93px |
  | 768 | 893 | 0 | 不能 |

  800–880 这一整段 `ismapped` 返回 1 而人点不到。只断言它的测试在这段是绿的。

* **只看底边** —— 一个**根本没分配到位置**的控件，`winfo_rooty()` 返回的是父窗口原点
  附近的退化值（实测 stacked @ 720px 时是 69），拿它跟窗口高比会得出"没超出"。
  那是假绿里最难看的一种：控件压根不在画面上，判据却说它在。

所以两条一起断言。

## 为什么必须**映射**窗口

试过 withdrawn 的 Toplevel（不弹窗、不闪屏），**不行**：withdrawn 的窗口根本不理会
`geometry()`，三版各档全停在自己 `GEOMETRY` 声明的那个高度上；套一层
`pack_propagate(False)` 的定长容器则整个拿不到几何（高度是 1）。
只有真映射出来的窗口尺寸才听测试的。代价是跑这几条时屏幕上会闪几下窗口。

## 反向验证（`docs/OVERNIGHT.md` 四条配方之一）

`ClippedByTheOldPackOrder` 走**同一条构造路径**，只把右栏按修复前的顺序重排一次，
断言按钮确实掉出窗口。它红，才证明上面那些绿的不是"空得非常好看"。

🚨 本文件零站点标识符（CLAUDE.md 硬约束 1b）：design / cell / 失败原因全是编的。
"""

from __future__ import annotations

import unittest
from importlib import import_module

from ewave_batch import model
from tests.test_gui_common import _tk_or_skip

# --------------------------------------------------------------------------
# 手写的假值
# --------------------------------------------------------------------------

WIDTH = 1560
"""窗口宽度。split 的 `GEOMETRY` 是 1560x900 —— 宽度不是这个 bug 的变量，钉住它。"""

HEIGHTS = (1000, 900, 850, 800, 720)
"""要验的窗口高度。

900 是 split 自己的 `GEOMETRY`；720 是笔记本上很常见的可用高度；
850/800 那两档是修复前**实测掉出去**的那一段（见上面的表）。
5 档不是随手取的，少了哪一档都会让"修好了"这个结论变窄。
"""

LONG_REASON = (
    "eWave exited 1: mesh generation failed. "
    "Next: check the layer map, then re-run this one with a coarser mesh. "
) * 4
"""长到把 `Reason` 撑满 `REASON_ROWS_MAX` 的失败原因 —— **最坏情况**。

`_set_reason` 按 `len(text) // 88 + 1` 算行数、5 行封顶，而 `Reason` 一涨，
排在它后面的 `Output log` 按钮就被往下推。用户报的正是"选中之后"才没的，
所以这里必须用撑满的那一版，不能用一行的短消息。
"""

RUN_COUNT = 8
FAILED_RUN_ID = "r00"


def _make_runs():
    """一批 run，第 0 条是 failed（带长 message）—— 用户复现路径上的那一条。"""
    runs = []
    for index in range(RUN_COUNT):
        failed = index == 0
        runs.append(
            model.Run(
                run_id="r%02d" % index,
                design_key="d",
                axis_values={"corner": "typical", "temperature": "-40.0"},
                status=model.RunStatus.FAILED if failed else model.RunStatus.DONE,
                message=LONG_REASON if failed else "",
            )
        )
    return tuple(runs)


def _bridge():
    """真桥（`gui.state.GuiState`）**只换掉 `runs()`**。

    不用测试替身：这个 bug 是布局的，而布局吃的是真 bridge 灌出来的控件高度。
    换成 stub 就有"stub 少喂了几个控件所以不挤"这条静默的退路。
    不起 driver、不碰 eWave（CLAUDE.md 硬约束 3）。
    """
    from gui.state import GuiState

    class Bridge(GuiState):
        def runs(self):
            return _make_runs()

    return Bridge()


# --------------------------------------------------------------------------
# 量尺
# --------------------------------------------------------------------------


def _find_by_text(widget, text: str):
    """按显示文本找控件。找不到返回 None。"""
    import tkinter as tk

    for child in widget.winfo_children():
        try:
            if str(child.cget("text")) == text:
                return child
        except tk.TclError:  # 容器没有 -text 这个选项，这是正常路径
            pass
        found = _find_by_text(child, text)
        if found is not None:
            return found
    return None


def _bottom(widget, top) -> int:
    """控件底边相对 `top` 原点的位置。判据的左边那一半。"""
    return widget.winfo_rooty() - top.winfo_rooty() + widget.winfo_height()


class _Bench:
    """建一版布局、选中那条 failed run、把窗口调到指定高度，然后随便量。"""

    def __init__(self, test: unittest.TestCase, layout: str):
        """**一版只建一次**，各档高度靠 `resize()` 走。

        真映射一个窗口 + 建一整棵控件树大约 2 秒；每档各建一次就是 30 次 ≈ 1 分钟，
        而 `deploy/doctor.sh --test` 在红区（很可能还隔着 X forwarding）也要跑这一遍。
        建一次、resize 五次量五次 —— 量到的东西一个不少。
        """
        import tkinter as tk

        root = _tk_or_skip(test)
        self.test = test
        self.layout = layout
        self.top = tk.Toplevel(root)
        # 摆到左上角：这几条测试期间窗口是真映射出来的（见本文件 docstring），
        # 钉住位置至少让它别在屏幕中间乱跳。
        self.top.geometry("%dx%d+0+0" % (WIDTH, HEIGHTS[0]))
        test.addCleanup(self._teardown)

        module = import_module("gui.frames.%s" % layout)
        self.frame = module.build_frame(self.top, _bridge())
        self.frame.pack(fill=tk.BOTH, expand=True)
        self.app = self.frame._ewb_app

        # tabbed 版的 detail 在 Runs 那一页里 —— 没选中那一页就没有几何可言。
        self._select_runs_tab()

        # `apply_minsize` 的下限（0.85×屏幕）会盖掉 geometry，而本测试要的正是
        # "窗口比内容矮"这件事。撤掉下限之后 geometry 才说了算。
        self.top.minsize(1, 1)
        self.app.tree.selection_set(FAILED_RUN_ID)

    def resize(self, height: int) -> None:
        """把窗口调到这个高度，并把选中详情重画一遍。"""
        self.top.geometry("%dx%d+0+0" % (WIDTH, height))
        self.app.show_detail()
        self.top.update_idletasks()
        self.top.update()
        got = self.top.winfo_height()
        if got != height:
            # 窗口管理器把尺寸夹了（屏幕装不下之类）。**跳过、并说清楚原因** ——
            # 在一个尺寸不是测试说了算的窗口上量出来的数没有意义。
            self.test.skipTest(
                "平台跳过：要 %dx%d 的窗口，窗口管理器给的是 %dx%d"
                % (WIDTH, height, self.top.winfo_width(), got)
            )

    def _select_runs_tab(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        def walk(widget):
            for child in widget.winfo_children():
                if isinstance(child, ttk.Notebook):
                    return child
                found = walk(child)
                if found is not None:
                    return found
            return None

        book = walk(self.top)
        if book is None:
            return
        for index in range(len(book.tabs())):
            # 标题带计数，形如 `  Runs (8)  ` —— 前缀匹配，别写死那个数字。
            if book.tab(index, "text").strip().startswith("Runs"):
                book.select(index)
                try:
                    self.top.update_idletasks()
                except tk.TclError:  # pragma: no cover
                    pass
                return

    def _teardown(self) -> None:
        import tkinter as tk

        # 自动存盘那个 after 定时器不取消的话，窗口销毁之后 Tk 会往 stderr 吐
        # `invalid command name "..._save_session_now"`。取消它，**不调 close()**
        # —— close() 会真的往盘上写一份 session。
        timer = getattr(self.app, "_session_timer", None)
        if timer is not None:
            try:
                self.top.after_cancel(timer)
            except tk.TclError:  # pragma: no cover
                pass
            self.app._session_timer = None
        try:
            self.top.destroy()
        except tk.TclError:  # pragma: no cover
            pass

    def height(self) -> int:
        return self.top.winfo_height()

    def anchor(self, text: str):
        return _find_by_text(self.frame, text)

    def assert_reachable(self, widget, what: str) -> None:
        """**判据本体**：映射了，而且底边在窗口里。两条缺一不可（见 docstring）。"""
        window = self.height()
        bottom = _bottom(widget, self.top)
        self.test.assertEqual(
            widget.winfo_ismapped(),
            1,
            "%s @ %dpx：%s 根本没被映射出来（底边报的 %d 是退化值，不能信）"
            % (self.layout, window, what, bottom),
        )
        self.test.assertLessEqual(
            bottom,
            window,
            "%s @ %dpx：%s 的底边在 %d，窗口只有 %d —— 它掉出窗口 %dpx，人点不到。"
            % (self.layout, window, what, bottom, window, bottom - window),
        )


# --------------------------------------------------------------------------
# 1. 正向：三版、五档高度，该点得到的都点得到
# --------------------------------------------------------------------------

ANCHORS = ("Output log", "Submit")
"""必须永远在窗口里的两个按钮。

`Output log` 是 2026-08-31 这条 bug 的主角；`Submit` 是 2026-08-19 那条的主角
（`stacked.py` 那段注释）。两条一起钉，因为它们是同一个病的两次发作。
"""

LAYOUTS_UNDER_TEST = ("split", "tabbed", "stacked")


class EveryAnchorStaysInsideTheWindow(unittest.TestCase):
    def test_三版五档全都点得到(self) -> None:
        checked = 0
        for layout in LAYOUTS_UNDER_TEST:
            bench = _Bench(self, layout)
            for height in HEIGHTS:
                bench.resize(height)
                for text in ANCHORS:
                    widget = bench.anchor(text)
                    self.assertIsNotNone(
                        widget, "%s 布局里根本没有 %r 这个按钮" % (layout, text)
                    )
                    bench.assert_reachable(widget, repr(text))
                    checked += 1
        # 计数断言：真的量了 3 版 × 5 档 × 2 个按钮，不是空循环绿得好看。
        self.assertEqual(checked, len(LAYOUTS_UNDER_TEST) * len(HEIGHTS) * len(ANCHORS))

    def test_失败原因那一行也在窗口里(self) -> None:
        """`Reason` 和 `Output log` 是同一个答案的两半，一半掉出去就等于没答。"""
        shown = 0
        for layout in LAYOUTS_UNDER_TEST:
            bench = _Bench(self, layout)
            for height in HEIGHTS:
                bench.resize(height)
                holder = getattr(bench.app, "reason_holder", None)
                if holder is None or not bench.app._reason_shown:
                    continue  # 这一版没建 detail 框 —— 由上面那条测试管
                bench.assert_reachable(holder, "Reason 那一行")
                shown += 1
        self.assertGreater(shown, 0, "一版都没显示 Reason —— 本测试什么都没验到")


# --------------------------------------------------------------------------
# 2. 反向：把修复前的 pack 顺序装回去，断言它确实掉出窗口
# --------------------------------------------------------------------------


class ClippedByTheOldPackOrder(unittest.TestCase):
    """同一条构造路径，只把右栏重排回「runs 先 pack + expand」。

    它**必须**报出"掉出窗口"，否则上面那些绿的说明不了任何事情
    —— 那才是本项目吃过亏的"假绿"。
    """

    def test_旧顺序会把_Output_log_挤出窗口(self) -> None:
        import tkinter as tk

        bench = _Bench(self, "split")
        bench.resize(800)
        right = bench.app._right_pane
        detail = bench.app.detail_box
        kids = [k for k in right.winfo_children() if k is not detail]
        self.assertEqual(len(kids), 1, "右栏的结构变了，本反向测试要重写：%r" % (kids,))
        runs = kids[0]

        for widget in (runs, detail):
            widget.pack_forget()
        runs.pack(fill=tk.BOTH, expand=True)   # 修复前：表先 pack，还 expand
        detail.pack(fill=tk.X, pady=(6, 0))    # 修复前：detail 排它后面
        bench.app.show_detail()
        bench.top.update_idletasks()
        bench.top.update()

        widget = bench.anchor("Output log")
        self.assertIsNotNone(widget)
        bottom = _bottom(widget, bench.top)
        self.assertGreater(
            bottom,
            bench.height(),
            "旧的 pack 顺序竟然没把按钮挤出去（底边 %d，窗口 %d）—— "
            "那说明正向那几条测的不是这个 bug，判据得重写"
            % (bottom, bench.height()),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
