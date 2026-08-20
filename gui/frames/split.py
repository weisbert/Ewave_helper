"""布局 1c — Split：左边一条配置栏，右边整高 Runs 表。**这是默认布局。**

选它的理由（`mockups/README.md` 的对照表）：三版里**唯一**能让"勾选"和"12 行 run"
同屏的。改一个勾选，右边的表和它旁边的 `-> N` 一起变 —— 而"改设定看不见后果"
正是原生 GUI 那三个痛点里最贵的一个。代价是左栏窄，标签得缩写。

本文件**只负责摆放**：控件和逻辑全在 `gui/_ui.py`，桥在 `gui/state.py`。
frame 里一行业务逻辑都没有（`docs/INTERFACES.md`「`gui/frames/*.py` 的三条硬要求」）。

自检（不弹窗、建完控件树就退）：`EWB_SMOKE=1 python -m gui.frames.split`
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 只为类型检查，运行时一行都不 import
    from collections.abc import Sequence

# 🚨 模块顶层**不许** import tkinter，也不许 import `gui._ui`（它自己 import tkinter）——
# CLAUDE.md 硬约束 5：无 $DISPLAY / 没装 python3-tk 的纯 ssh 会话里 CLI 必须照常可用，
# 而 `python -m ewave_batch dry-run --self-test` 会 import 本模块。
# 2026-08-19 复核实测：本文件当时是三版里唯一在顶层 import tkinter 的，于是在一台没装
# python3-tk 的机器上 `EWB_SMOKE=1 python -m gui.frames.split` 抛原始 ImportError 退非 0，
# 闸门第 5 步只红在**默认布局**这一版上 —— 而设计意图是「没 tkinter 属平台降级、
# 该 skip 退 0」（stacked / tabbed 就是那样的）。
SHARED_LAYER_MODULE = "gui._ui"
"""共用构件层。`build_frame` / `main` 惰性 import 它，import 失败**不**往外抛。"""

LAYOUT_NAME = "split"
"""这一版的布局名，必须是 `gui.app.LAYOUTS` 里的一个。"""

SECTIONS: tuple[str, ...] = (
    "batchbar",
    "designs",
    "groups",
    "settings",
    "resources",
    "runs",
    "detail",
    "actionbar",
    "statusbar",
)
"""三版**必须暴露同一组**顶层构件 —— 这是「三个 agent 各写各的、界面手感不一致」
唯一的机器判据（`tests/test_gui_frames.py`）。

名字对应共用层的 `build_<section>`：前八个和 `mockups/_ui.py` 的 `build_*` 一一对上，
`groups`（Run groups）是草图之后加的第九个，摆在左栏 Designs 和 Settings 之间 ——
它管的是「哪些 design 跟哪些设定相乘」，位置就该在这两者中间，而那块地方原本
是约 250px 的死白（还随窗口变大而变大）。
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

MINSIZE_SCREEN_FRACTION = 0.85
"""minsize 最多占屏幕的几成。上限存在的理由是「窗口比屏幕大就没法用」——
最小尺寸一旦超过屏幕，标题栏可能落在可视区外，窗口既拖不动也关不掉。
"""


def apply_minsize(frame: object, *, fraction: float = MINSIZE_SCREEN_FRACTION):
    """把顶层窗口的 `minsize` 设成**现算的**「这版布局真正需要多大」，返回设下去的 (w, h)。

    2026-08-19 实拍：三版一个都没有 `minsize()`。把 split 缩到 1150x720，
    `Submit` 的 `winfo_ismapped()` 就是 0 —— 点不到的按钮是功能性缺陷，不是观感问题。
    把 Tk 具名字体放大 30% 模拟红区 Linux 的字体度量，这一版要 914px 高，
    也就是说**同样的代码在红区本来就是裁着的**。

    尺寸现算、不写死：`LEFT_WIDTH` 那个照设计稿抄来的 452 已经在这件事上栽过一次
    （左栏第 5 个勾选框整个点不到），写死的像素到气隙对面就是另一个裁剪 bug。

    **调用时机也是判据的一部分：在 `recompute()` 往界面里填内容之前。**
    否则批次目录那种长路径（实测要 1336px）会被算进"最低要求"，
    最小窗口被一条路径撑得没法用。

    `frame` 不是直接建在顶层窗口里（= 被嵌进别人的容器）→ 静默跳过：
    那时候窗口不是我们的，替别人定最小尺寸是越界。

    ⚠️ 与 `gui/frames/stacked.py` / `tabbed.py` 的同名函数逐字相同 —— 三个 frame
    模块在本仓库里各自自足（`build_section` / `NullBridge` 也都是各抄一份的），
    共用层 `gui/_ui.py` 是另一条分工线上的地盘。
    """
    import tkinter as tk  # noqa: PLC0415 - 惰性：模块顶层碰不得（硬约束 5）

    try:
        top = frame.winfo_toplevel()
        if frame.nametowidget(frame.winfo_parent()) is not top:
            return None
        frame.update_idletasks()
        cap_w = int(top.winfo_screenwidth() * fraction)
        cap_h = int(top.winfo_screenheight() * fraction)
        size = (
            max(1, min(frame.winfo_reqwidth(), cap_w)),
            max(1, min(frame.winfo_reqheight(), cap_h)),
        )
        top.minsize(*size)
    except tk.TclError:  # pragma: no cover - 窗口已经被关掉的竞态
        return None
    return size


def _split_app_class(shared: object) -> type:
    """现造 `SplitApp` 类（`shared` = `gui._ui` 模块）。

    为什么是工厂而不是模块级的 `class SplitApp(_ui.BaseApp)`：基类要在**定义类的那一刻**
    就存在，于是模块级的写法必然把 `gui._ui`（连带 tkinter）拉进 import 期 ——
    正是硬约束 5 禁的那件事。stacked / tabbed 早就是这个形状，本版 2026-08-19 跟上。
    """
    import tkinter as tk  # noqa: PLC0415 - 惰性
    from tkinter import ttk  # noqa: PLC0415 - 惰性

    class SplitApp(shared.BaseApp):  # type: ignore[misc, name-defined]
        """1c：batchbar / 左配置 / 右 Runs / actionbar / statusbar。"""

        GEOMETRY = "1560x900"
        """比另外两版宽。1c 是**左右分栏**：左边那条配置栏的宽度由内容决定（见 LEFT_WIDTH），
        共用层的默认 1180 减掉它之后，右边的 Runs 表只剩四百来像素 —— 而"勾选和 run 表同屏"
        正是选这一版的**全部理由**，表被挤瘦就等于把这个理由丢了。

        2026-08-19 用户反馈「屏幕显示太窄」+ 截图确认后加的。窗口管理器会自己夹到屏幕尺寸，
        所以在小屏上不会出事；大屏上多出来的宽度按 weight 全给右边。
        """

        def layout(self) -> None:
            """摆放本版的九个 section。

            🚨 **pack 的顺序就是抢空间的顺序**：动作栏和状态栏**先**从底部拿走自己那份，
            中间那个 `expand=True` 的分栏最后 pack、只拿剩下的。stacked 版就是栽在这上面
            （整条动作栏被挤出窗口、`Submit` 的 `winfo_ismapped()` 返回 0），本版把窗口
            缩到 1150x720 是同一个症状。窗口再矮，能点的东西不许消失。
            """
            root = self.frame

            self.build_batchbar(root).pack(side=tk.TOP, fill=tk.X)
            ttk.Separator(root, orient=tk.HORIZONTAL).pack(side=tk.TOP, fill=tk.X)

            # 可拖的分隔条。用 PanedWindow 而不是 Frame + Separator：
            # 后者的分隔线是画上去的，拖不动，左栏宽度就成了写死的（见 LEFT_WIDTH 的注释）。
            # 先建、**最后 pack**（见本方法的 docstring）。
            body = ttk.PanedWindow(root, orient=tk.HORIZONTAL)

            left = ttk.Frame(body, padding=(8, 8, 6, 4))
            right = ttk.Frame(body, padding=(8, 8, 8, 4))
            # weight=0：窗口变大时多出来的空间**全给右边的 Runs 表**，左栏宽度不动。
            # 左栏装的是固定几行设定，越拉越宽没有意义；表格多一行都是赚的。
            body.add(left, weight=0)
            body.add(right, weight=1)
            self._paned = body
            self._left_pane = left
            self._right_pane = right

            # `buttons="three"` = Add / Duplicate / Remove。共用层的 `dup_design` 早就实现了，
            # 之前只有 tabbed 传了这个参数 ⇒ 这一版想复制一行 design 只能删掉重敲三个字段。
            self.build_designs(left, widths=(120, 130, 110), rows=3, buttons="three").pack(
                fill=tk.X, pady=(4, 6)
            )
            # Run groups 拿 `expand=True`：左栏 Resources 底下原本是约 250px 的死白，
            # 而且窗口越大白得越多 —— 那块地方就是分组面板的家。共用层还没有
            # `build_groups` 时整段跳过（本棒可以单独跑），死白照旧，不炸。
            if hasattr(self, "build_groups"):
                self.build_groups(left).pack(fill=tk.BOTH, expand=True, pady=(0, 6))
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

            # 底部三条先拿空间（`side=BOTTOM` 时先 pack 的更靠下 ⇒ 状态栏在最底）。
            self.build_statusbar(root).pack(side=tk.BOTTOM, fill=tk.X)
            ttk.Separator(root, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X)
            self.build_actionbar(root).pack(side=tk.BOTTOM, fill=tk.X)
            ttk.Separator(root, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X)

            body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

            self.minsize_applied = apply_minsize(root)
            """实际设下去的最小尺寸，`None` = 被嵌进别人的容器所以没设。留着给自检脚本看。"""
            root.minsize_applied = self.minsize_applied


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
                # 别把右边挤没了 —— 但「最多占一半」是个**错的**保留额，2026-08-19
                # 复验实拍：窗口正好停在 minsize 上时（1408px，= 左栏 721 + 右栏 ~680），
                # `total // 2` 只有 704，比左栏真正要的 721 还小 ⇒ sash 被摆窄 17px ⇒
                # Frequency sweep 那行末尾的 `GHz` 被切成 `G`。把窗口做窄一点就重演了
                # `LEFT_WIDTH` 当初那个裁剪 bug，只是这次裁的人是"保护右栏"的那行代码。
                # 改成拿**右栏自己的请求宽度**当保留额：minsize 本来就是
                # 左栏请求 + 右栏请求 算出来的，所以窗口不小于 minsize 时这个下限永远够，
                # 而窗口更宽时多出来的仍然全归右边（`body.add(right, weight=1)`）。
                reserve = max(1, self._right_pane.winfo_reqwidth())
                want = min(want, max(LEFT_WIDTH, total - reserve))
                self._paned.sashpos(0, want)
                self._sash_done = True
            except tk.TclError:  # pragma: no cover - 窗口已经关掉的竞态
                pass

    return SplitApp


def import_shared_layer() -> object | None:
    """惰性 import 共用构件层。没有 tkinter / 共用层不在 → None，**绝不往外抛 ImportError**。

    与 `gui/frames/stacked.py` 的同名函数逐字同义：本模块要能被 self-test import、
    要能在没装 python3-tk 的机器上退 0，这两件事都不该因为 tkinter 缺席而炸。
    """
    import importlib  # noqa: PLC0415 - 惰性

    try:
        return importlib.import_module(SHARED_LAYER_MODULE)
    except ImportError:
        return None


def build_frame(parent: object, bridge: object) -> object:
    """建这一版布局的主 frame。只通过 `bridge`（`model.GuiBridgeProtocol`）跟核心说话。"""
    shared = import_shared_layer()
    if shared is None:
        raise ImportError(
            "%s is unavailable (tkinter not installed?) - cannot build the %s layout"
            % (SHARED_LAYER_MODULE, LAYOUT_NAME)
        )
    return shared.build(_split_app_class(shared), parent, bridge)


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m gui.frames.split` —— 直接以这一版起界面。

    共用层缺席（典型情形：公开克隆者的机器上没装 python3-tk，Debian/RHEL 上它是单独的包）
    且是在跑冒烟 → 打印一句人话后**退 0**：那是平台降级，不是本布局坏了。
    闸门第 5 步不该因为「这台机器没有 GUI」而红。
    """
    from ewave_batch._stdio import ascii_safe_stdio  # noqa: PLC0415 - 惰性

    ascii_safe_stdio()
    args = list(sys.argv[1:] if argv is None else argv)
    shared = import_shared_layer()
    if shared is None or not callable(getattr(shared, "frame_main", None)):
        if os.environ.get("EWB_SMOKE") == "1" or "--smoke" in args:
            print("smoke %s: skipped, tkinter is not installed" % LAYOUT_NAME)
            return 0
        print(
            "%s is not available; cannot open a window." % SHARED_LAYER_MODULE,
            file=sys.stderr,
        )
        return 2
    return int(shared.frame_main(LAYOUT_NAME, args))


if __name__ == "__main__":  # pragma: no cover - 手工/冒烟入口
    raise SystemExit(main())
