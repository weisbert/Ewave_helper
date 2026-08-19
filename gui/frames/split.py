"""布局 1c — Split：左边一条配置栏，右边整高 Runs 表。**这是默认布局。**

选它的理由（`mockups/README.md` 的对照表）：三版里**唯一**能让"勾选"和"12 行 run"
同屏的。改一个勾选，右边的表和它旁边的 `-> N` 一起变 —— 而"改设定看不见后果"
正是原生 GUI 那三个痛点里最贵的一个。代价是左栏窄，标签得缩写。

本文件**只负责摆放**：控件和逻辑全在 `gui/_ui.py`，桥在 `gui/state.py`。
frame 里一行业务逻辑都没有（`docs/INTERFACES.md`「`gui/frames/*.py` 的三条硬要求」）。

自检（不弹窗、建完控件树就退）：`EWB_SMOKE=1 python -m gui.frames.split`
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Sequence
from tkinter import ttk

from .. import _ui

LAYOUT_NAME = "split"
"""这一版的布局名，必须是 `gui.app.LAYOUTS` 里的一个。"""

SECTIONS: tuple[str, ...] = (
    "batchbar",
    "designs",
    "settings",
    "resources",
    "runs",
    "detail",
    "actionbar",
    "statusbar",
)
"""三版**必须暴露同一组**顶层构件 —— 这是「三个 agent 各写各的、界面手感不一致」
唯一的机器判据（`tests/test_gui_frames.py`）。

名字对应共用层的 `build_<section>`，和 `mockups/_ui.py` 里那八个 `build_*` 一一对上。
布局只决定它们摆在哪、留几行，**不决定有没有**：少一个 section 就是三版分岔的开始。

⚠️ 这个常量**不在冻结面里**（`docs/INTERFACES.md` 只冻了 `LAYOUT_NAME` /
`build_frame` / `main`）。它是并行三版之间自发长出来的约定，已写进本阶段的
`interface_change_requests` —— 该进 `FROZEN` 才有人替它盯漂移。
"""

LEFT_WIDTH = 452
"""左栏的**起始**宽度（px），照设计稿 1c。**下限不是它，内容说了算**。

⚠️ 2026-08-19 用户实测反馈 + 截图确认：原来这里是 `width=LEFT_WIDTH` 配
`pack_propagate(False)`，把左栏**硬钉死**在 452px。后果不是"有点挤"，是**内容被裁掉**：

* `Add row` / `Remove row` 两个按钮被切成 `Add ro` / `Remov`；
* Corner 那行的**第 5 个勾选框（`typical`）整个看不见** —— 最常用的工艺角**点不到**，
  这是功能性缺陷不是观感问题；
* `+ Advanced` 摘要行、Resources 的提示行右侧全被截断。

根因有两条，都得治：

1. 452 这个数字是照设计稿抄的，而设计稿是用 Windows 默认字体画的。真实内容
   （Designs 三列 120+130+110 = 360 + 侧边按钮 ~90 + padding）本来就超过它。
2. **红区是 Linux，默认字体度量与开发机不同** ⇒ 同样的内容更宽，裁得更狠。
   任何写死的像素宽度都会在气隙对面变成另一个样子。

⇒ 现在用 `ttk.PanedWindow`：起始位置取 `max(LEFT_WIDTH, 左栏内容要求的宽度)`，
**并且分隔条可以拖**。写死的像素只决定"一开始多宽"，不再决定"最多多宽"。
"""

RUN_ROWS = 25
"""右栏能留几行 run。1c 的全部优势就在这个数字上（1a 只有 ~9 行）。"""


class SplitApp(_ui.BaseApp):
    """1c：batchbar / 左配置 / 右 Runs / actionbar / statusbar。"""

    GEOMETRY = "1560x900"
    """比另外两版宽。1c 是**左右分栏**：左边那条配置栏的宽度由内容决定（见 LEFT_WIDTH），
    共用层的默认 1180 减掉它之后，右边的 Runs 表只剩四百来像素 —— 而"勾选和 run 表同屏"
    正是选这一版的**全部理由**，表被挤瘦就等于把这个理由丢了。

    2026-08-19 用户反馈「屏幕显示太窄」+ 截图确认后加的。窗口管理器会自己夹到屏幕尺寸，
    所以在小屏上不会出事；大屏上多出来的宽度按 weight 全给右边。
    """

    def layout(self) -> None:
        root = self.frame

        self.build_batchbar(root).pack(fill=tk.X)
        ttk.Separator(root, orient=tk.HORIZONTAL).pack(fill=tk.X)

        # 可拖的分隔条。用 PanedWindow 而不是 Frame + Separator：
        # 后者的分隔线是画上去的，拖不动，左栏宽度就成了写死的（见 LEFT_WIDTH 的注释）。
        body = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(body, padding=(8, 8, 6, 4))
        right = ttk.Frame(body, padding=(8, 8, 8, 4))
        # weight=0：窗口变大时多出来的空间**全给右边的 Runs 表**，左栏宽度不动。
        # 左栏装的是固定几行设定，越拉越宽没有意义；表格多一行都是赚的。
        body.add(left, weight=0)
        body.add(right, weight=1)
        self._paned = body
        self._left_pane = left

        self.build_designs(left, widths=(120, 130, 110), rows=3).pack(fill=tk.X, pady=(4, 6))
        self.build_settings(left, compact=True, show_formula=True).pack(fill=tk.X, pady=(0, 6))
        self.build_resources(left, compact=True).pack(fill=tk.X)

        self.build_runs(right, rows=RUN_ROWS, titled=False, header_in_title=False).pack(
            fill=tk.BOTH, expand=True
        )
        self.build_detail(right).pack(fill=tk.X, pady=(6, 0))

        # sash 初始位置：见 `_place_sash`。必须等窗口**真正映射**之后才算得准，
        # 所以绑在第一次 <Configure> 上，而不是 after_idle。
        self._sash_done = False
        body.bind("<Configure>", self._place_sash, add="+")

        ttk.Separator(root, orient=tk.HORIZONTAL).pack(fill=tk.X)
        self.build_actionbar(root).pack(fill=tk.X)
        ttk.Separator(root, orient=tk.HORIZONTAL).pack(fill=tk.X)
        self.build_statusbar(root).pack(fill=tk.X)


    def _place_sash(self, _event: object = None) -> None:
        """把分隔条放到「左栏内容真正需要的宽度」上。

        取 `max(LEFT_WIDTH, left.winfo_reqwidth())`：设计稿的 452 只当**下限**，
        内容更宽就听内容的 —— 红区是 Linux，字体度量和开发机不同，
        任何写死的像素数到那边都会重新变成一个裁剪 bug（这正是 2026-08-19 那次的根因）。

        绑在第一次 `<Configure>` 上而不是 `after_idle`：窗口还没映射时
        `winfo_width()` 是 1，`sashpos` 会被 Tk **夹到 0**，左栏当场整个消失
        （2026-08-19 修这个 bug 时第一版就是这么翻车的，截图为证）。
        所以要等到 paned 自己有真实宽度了再放，放完一次就不再动 —— 用户拖过之后
        不该被我们回弹。`EWB_SMOKE=1` 不进 mainloop ⇒ 不触发，也不需要。
        """
        if self._sash_done:
            return
        try:
            total = self._paned.winfo_width()
            if total <= 1:
                return  # 还没映射，Tk 会把 sashpos 夹到 0 —— 等下一次 <Configure>
            want = max(LEFT_WIDTH, self._left_pane.winfo_reqwidth())
            # 别把右边挤没了：最多占一半。
            want = min(want, max(LEFT_WIDTH, total // 2))
            self._paned.sashpos(0, want)
            self._sash_done = True
        except tk.TclError:  # pragma: no cover - 窗口已经关掉的竞态
            pass


def build_frame(parent: object, bridge: object) -> object:
    """建这一版布局的主 frame。只通过 `bridge`（`model.GuiBridgeProtocol`）跟核心说话。"""
    return _ui.build(SplitApp, parent, bridge)


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m gui.frames.split` —— 直接以这一版起界面。"""
    return _ui.frame_main(LAYOUT_NAME, argv)


if __name__ == "__main__":  # pragma: no cover - 手工/冒烟入口
    import sys

    sys.exit(main(sys.argv[1:]))
