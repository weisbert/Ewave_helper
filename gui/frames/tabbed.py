# -*- coding: utf-8 -*-
"""布局 B —— Tabbed：三个 tab（Designs / Settings / Runs），草图 1b。

批次栏和动作栏在 notebook **外面**常驻，所以乘法公式和 Submit 永远在屏幕上。
Settings 页右边多一个 Run count 面板：逐轴 x N，底下 total。Runs 页独占整窗（~20 行）。
代价：设定和它展开出来的 run 永远不同屏 —— 这一版就是拿「同屏」换「表大 + 页面干净」。

⚠️ 2026-08-19：Resources **从独占的第四个 tab 挪进了 Settings 页底**。
它只有一个输入框加一行字，独占一页的后果是那一页下面空着约 700px，
而 Settings 页自己底下也空着约 450px —— 一个空页换一个空页。现在
Settings 页 = 左上设定 / 左下 Run groups / 右上 Run count / 页底 Resources，
四块把那页填满，tab 也少一个。**section 一个没少**，只是换了地方摆。

**这个模块只负责摆放。** 九个 section 的控件由 `gui/` 共用层（`gui._ui.BaseApp`）的
`build_<section>` 出，frame 里不许有业务逻辑；要跟核心说话一律走 `bridge`
（`ewave_batch.model.GuiBridgeProtocol`）。Run count 面板是本版**独有的摆放件**，
但它只是把 `bridge.axes()` / `bridge.designs()` / `bridge.runs()` 数出来显示 ——
不算逻辑，算显示（草图 1b 的 `on_counts` 就在 layout 文件里）。

🚨 模块顶层**不许 import tkinter**，也不许 import 共用层（它自己 import tkinter）——
CLAUDE.md 硬约束 5：无 ``$DISPLAY`` 的纯 ssh 会话里 CLI 必须照常可用。

headless 冒烟（`scripts/check.sh` 第 5 步）::

    EWB_SMOKE=1 python -m gui.frames.tabbed      # 必须退 0

界面上给用户看的字符串一律**英文**；代码注释仍然中文。
"""

from __future__ import annotations

import importlib
import inspect
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 只为类型检查，运行时一行都不 import
    from collections.abc import Sequence

    from ewave_batch.model import GuiBridgeProtocol, TickReport

LAYOUT_NAME = "tabbed"
"""本模块的布局名。三版互不相同，且等于模块名（`tests/test_gui_frames.py` 盯着）。"""

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
"""三版**必须暴露同一组**顶层构件 —— 这是「界面手感一致」唯一的机器判据。

⚠️ 必须与 `gui.frames.stacked.SECTIONS` / `gui.frames.split.SECTIONS` **逐字相等**。
换 tab 不该换掉一个 section：Tabbed 版只是把同样九件东西塞进三个 tab。
`groups`（Run groups）是草图之后加的第九个，摆在 Designs 和 Settings 之间。
"""

TAB_NAMES: tuple[str, ...] = ("Designs", "Settings", "Runs")
"""三个 tab 的英文标题，顺序即左到右（草图 1b 是四个，Resources 那页见模块 docstring）。"""

SECTION_TITLES: dict[str, str] = {
    "batchbar": "Batch",
    "designs": "Designs",
    "groups": "Run groups",
    "settings": "Settings",
    "resources": "Donau submit",
    "runs": "Runs",
    "detail": "Selected run",
    "actionbar": "Actions",
    "statusbar": "Status",
}
"""占位构件上写的标题（共用层缺席时才用得上）。界面字符串 = 英文。"""

BRIDGE_METHODS: tuple[str, ...] = (
    "axes",
    "cancel",
    "command_text",
    "designs",
    "load_spec",
    "plan",
    "runs",
    "start",
    "summary",
    "tick",
)
"""`model.GuiBridgeProtocol` 的全部方法。测试断言这份清单与冻结面**逐字相等**。"""

SHARED_LAYER_MODULE = "gui._ui"
"""共用构件层。`build_frame` 惰性 import 它（它自己 import tkinter，顶层碰不得）。"""

SHARED_LAYER_BRIDGE_EXTRAS: tuple[str, ...] = (
    "axis_selection",
    "batch_name",
    "extra_flags_text",
    "official_run_dir",
    "options",
    "set_max_parallel",
    "submit_command",
    "sweep",
)
"""⚠️ **已报备的接口缺口** —— 见 `gui/frames/stacked.py` 同名常量下的长注释。

一句话：`gui._ui.BaseApp` 要的比冻结的 `GuiBridgeProtocol` 多，所以这里拿「bridge
有没有这些」当判据：有 → 真界面，没有 → 占位版（布局照样建得起来）。
"""


def import_shared_layer() -> object | None:
    """惰性 import 共用构件层。没有 tkinter / 共用层还没写 → None，**绝不 ImportError**。"""
    try:
        return importlib.import_module(SHARED_LAYER_MODULE)
    except ImportError:
        return None


def shared_layer_usable(shared: object | None, bridge: object) -> bool:
    """能不能走共用层：它得有 `BaseApp` + `build`，bridge 得喂得饱它。

    两个条件都**显式检查**，不靠 try/except 兜底 —— 吞掉 AttributeError 会把共用层的
    真 bug 变成「界面少了一半但没人报错」。
    """
    if shared is None:
        return False
    if not isinstance(getattr(shared, "BaseApp", None), type):
        return False
    if not callable(getattr(shared, "build", None)):
        return False
    return all(hasattr(bridge, name) for name in SHARED_LAYER_BRIDGE_EXTRAS)


def describe_argument_problems(parent: object, bridge: object) -> list[str]:
    """检查 `build_frame` 的两个入参，返回问题描述（空 list = 干净）。

    错误必须发生在建任何控件之前 —— 半个界面比崩掉更难查。
    """
    problems: list[str] = []
    if parent is None or not hasattr(parent, "tk"):
        problems.append(
            "parent must be a tkinter container widget, got %s" % type(parent).__name__
        )
    missing = [name for name in BRIDGE_METHODS if not callable(getattr(bridge, name, None))]
    if missing:
        problems.append(
            "bridge does not satisfy GuiBridgeProtocol, missing: %s" % ", ".join(missing)
        )
    return problems


def build_section(kit: object | None, name: str, parent: object, bridge: object, **hints: object):
    """建一个 section，返回 `(widget, 被丢掉的布局提示)`。

    构件层接哪个布局提示就给哪个，不接的**报告出来**而不是悄悄吞掉。
    """
    builder = getattr(kit, "build_" + name, None) if kit is not None else None
    if not callable(builder):
        return _placeholder(parent, name), tuple(sorted(hints))
    try:
        params = inspect.signature(builder).parameters
    except (TypeError, ValueError):  # pragma: no cover - C 实现的可调用对象，签名读不到
        params = {}
    takes_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    if takes_kwargs:
        accepted = dict(hints)
        dropped: tuple[str, ...] = ()
    else:
        accepted = {k: v for k, v in hints.items() if k in params}
        dropped = tuple(sorted(k for k in hints if k not in params))
    if takes_kwargs or "bridge" in params:
        accepted["bridge"] = bridge
    return builder(parent, **accepted), dropped


def _placeholder(parent: object, name: str):
    """共用层缺席时的占位构件 —— 让布局仍然建得起来，且一眼看得出缺了什么。"""
    from tkinter import ttk

    box = ttk.LabelFrame(parent, text=" %s " % SECTION_TITLES.get(name, name), padding=6)
    ttk.Label(box, text="Shared widget layer unavailable (placeholder).").pack(anchor="w")
    return box


def count_widgets(widget: object) -> int:
    """整棵子树里有多少个 widget（含自己）。冒烟拿它当「树真的建起来了」的数字判据。"""
    return 1 + sum(count_widgets(child) for child in widget.winfo_children())


MINSIZE_SCREEN_FRACTION = 0.85
"""minsize 最多占屏幕的几成。上限存在的理由是「窗口比屏幕大就没法用」——
最小尺寸一旦超过屏幕，标题栏可能落在可视区外，窗口既拖不动也关不掉。
"""


def apply_minsize(frame: object, *, fraction: float = MINSIZE_SCREEN_FRACTION):
    """把顶层窗口的 `minsize` 设成**现算的**「这版布局真正需要多大」，返回设下去的 (w, h)。

    与 `gui/frames/stacked.py` 的同名函数逐字相同 —— 三个 frame 模块在本仓库里各自自足
    （`import_shared_layer` / `build_section` / `NullBridge` 都是这么各抄一份的），
    共用层 `gui/_ui.py` 是另一条分工线上的地盘。理由长注释见 stacked 那一份，一句话：
    **不许写死像素**（红区是 Linux，字体度量不同），而且要**在 `recompute()` 填内容之前**算
    （否则一条长路径能把最小窗口撑到一千多像素宽）。

    `frame` 不是直接建在顶层窗口里（= 被嵌进别人的容器）→ 静默跳过。
    """
    import tkinter as tk

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


def axis_counts(bridge: object) -> list[tuple[str, int]]:
    """Run count 面板要显示的每一行：`(名字, 取值个数)`，designs 排第一。

    纯读 `bridge`，不算矩阵 —— 真正的展开是 `core.matrix.expand_runs` 的事，
    这里只把它已经算好的东西数一遍给人看（草图 1b 的 Run count 面板）。
    """
    rows: list[tuple[str, int]] = [("designs", len(bridge.designs()))]
    for axis in bridge.axes():
        rows.append((axis.name, len(axis.values)))
    return rows


def place_sections(kit: object | None, root: object, bridge: object) -> dict:
    """把九个 section 摆进 `root` —— **本版布局的全部内容**，两条路共用这一份。

    摆放照 `mockups/tabbed.py`：批次栏 → notebook（三 tab）→ 动作栏 → 状态栏。

    🚨 **pack 的顺序就是抢空间的顺序**：动作栏和状态栏建在 notebook **外面**还不够，
    还得**先于** notebook 从底部 pack。stacked 版就是栽在这上面（整条动作栏被挤出窗口、
    `Submit` 的 `winfo_ismapped()` 是 0）—— 本版的 notebook 一样是 `expand=True`，
    同一个坑，同一个治法。

    返回的 dict 里除了 `built` / `dropped` / `minsize`，还带 `refresh_counts` ——
    数据变了以后重画 Run count 面板和 tab 标题上的数字。
    """
    import tkinter as tk
    from tkinter import ttk

    built: list[str] = []
    dropped: list[str] = []

    def section(name: str, where: object, **hints: object):
        widget, missed = build_section(kit, name, where, bridge, **hints)
        built.append(name)
        dropped.extend("%s.%s" % (name, key) for key in missed)
        return widget

    # 批次栏在 notebook 外面常驻 —— 换 tab 不该让批次名消失。
    section("batchbar", root, show_dir=True).pack(side=tk.TOP, fill=tk.X)
    ttk.Separator(root, orient=tk.HORIZONTAL).pack(side=tk.TOP, fill=tk.X)

    # 先建、**最后 pack**：notebook 只拿动作栏和状态栏挑剩下的空间。
    notebook = ttk.Notebook(root, padding=6)
    tabs = {}
    for title in TAB_NAMES:
        page = ttk.Frame(notebook, padding=8)
        notebook.add(page, text="  %s  " % title)
        tabs[title] = page

    # --- Designs 页：表独占整页，底下一句话说清「这里每一行都会参与相乘」
    section("designs", tabs["Designs"], widths=(300, 300, 260), rows=12,
            buttons="three", titled=False).pack(fill=tk.BOTH, expand=True)
    ttk.Label(
        tabs["Designs"],
        justify=tk.LEFT,
        text="Every row here is multiplied by every combination on the Settings tab.",
    ).pack(anchor=tk.W, pady=(8, 0))

    # --- Settings 页：左上设定 / 左下 Run groups / 右上 Run count / 页底 Resources。
    # Run groups 拿 `expand=True` 是**有意的**：这一页原来底下空着约 450px，
    # 那块空白正是分组面板的家（`resources` 只有一行，撑不起来也不该被撑高）。
    settings_row = ttk.Frame(tabs["Settings"])
    settings_row.pack(fill=tk.BOTH, expand=True)
    left_col = ttk.Frame(settings_row)
    left_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    right_col = ttk.Frame(settings_row)
    right_col.pack(side=tk.LEFT, fill=tk.Y, padx=(10, 0))

    # 先建 groups 再建 settings（`built` 的顺序必须等于 `SECTIONS`），但**先 pack
    # settings**：设定在上、分组在下。建的顺序和摆的顺序在这里故意分开。
    groups_widget = section("groups", left_col)
    section("settings", left_col, compact=False, title=" Extraction settings ",
            show_formula=False).pack(side=tk.TOP, fill=tk.X)
    groups_widget.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(8, 0))

    count_box = ttk.LabelFrame(right_col, text=" Run count ", padding=8)
    count_box.pack(side=tk.TOP, fill=tk.X)
    count_rows = ttk.Frame(count_box)
    count_rows.pack(fill=tk.X)
    count_rows.columnconfigure(1, weight=1)
    ttk.Separator(count_box, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=5)
    total_line = ttk.Frame(count_box)
    total_line.pack(fill=tk.X)
    ttk.Label(total_line, text="total").pack(side=tk.LEFT)
    total_label = ttk.Label(total_line, text="0 runs")
    total_label.pack(side=tk.RIGHT)
    ttk.Label(
        count_box,
        wraplength=200,
        justify=tk.LEFT,
        text="Switch to the Runs tab to see every run before submitting.",
    ).pack(anchor=tk.W, pady=(8, 0))

    # Resources 横贯页底：它就是一条整命令的输入框，横着摆最省地方 ——
    # 而不是像原来那样独占一个 tab、底下空七百像素。
    section("resources", tabs["Settings"]).pack(side=tk.TOP, fill=tk.X, pady=(10, 0))

    # --- Runs 页：表独占整窗 + 选中详情
    # detail 先从页底拿走自己那份，runs 最后 pack —— 同 split/stacked，理由见
    # `gui/frames/split.py` 那段长注释（2026-08-31 实测：反过来写会把 `Output log`
    # 按钮挤出窗口，而 `winfo_ismapped()` 还返回 1，所以旧判据看不出来）。
    runs = section("runs", tabs["Runs"], rows=20, titled=False, header_in_title=False)
    section("detail", tabs["Runs"]).pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))
    runs.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    # 动作栏也在 notebook 外面，而且**先于** notebook 从底部拿空间 —— Submit 永远在屏幕上。
    # `side=BOTTOM` 时先 pack 的更靠下 ⇒ 状态栏在最底，动作栏在它上面。
    actionbar = section("actionbar", root, show_formula=True, show_dir=False)
    statusbar = section("statusbar", root)
    statusbar.pack(side=tk.BOTTOM, fill=tk.X)
    ttk.Separator(root, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X)
    actionbar.pack(side=tk.BOTTOM, fill=tk.X)
    ttk.Separator(root, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X)

    notebook.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    def refresh_counts() -> tuple[tuple[str, int], ...]:
        """重画 Run count 面板 + tab 标题上的数字，返回这次画出来的行（好被测试看见）。"""
        for child in count_rows.winfo_children():
            child.destroy()
        rows = axis_counts(bridge)
        for index, (name, count) in enumerate(rows):
            ttk.Label(count_rows, text=name).grid(row=index, column=0, sticky=tk.W, pady=1)
            ttk.Label(count_rows, text="x %d" % count, anchor=tk.E).grid(
                row=index, column=1, sticky=tk.E, padx=(20, 0)
            )
        total = len(bridge.runs())
        total_label.config(text="%d runs" % total)
        notebook.tab(TAB_NAMES.index("Designs"), text="  Designs (%d)  " % len(bridge.designs()))
        notebook.tab(TAB_NAMES.index("Runs"), text="  Runs (%d)  " % total)
        return tuple(rows)

    refresh_counts()

    return {
        "built": tuple(built),
        "dropped": tuple(dropped),
        "notebook": notebook,
        "refresh_counts": refresh_counts,
        "total_label": total_label,
        "minsize": apply_minsize(root),
    }


def build_frame(parent: object, bridge: GuiBridgeProtocol) -> object:
    """建 tabbed 版的主 frame，返回它的根 widget。

    有共用层且 bridge 喂得饱它 → 真界面；否则 → 占位版（同样九个 section）。
    走了哪条路看 `frame.widget_kit_name`。返回的 widget 上挂着 `refresh_counts()`。

    入参坏了抛 `TypeError`，**在建任何控件之前**。
    """
    problems = describe_argument_problems(parent, bridge)
    if problems:
        raise TypeError("gui.frames.%s.build_frame: %s" % (LAYOUT_NAME, "; ".join(problems)))

    shared = import_shared_layer()
    placed: dict = {}
    if shared_layer_usable(shared, bridge):

        class TabbedApp(shared.BaseApp):  # type: ignore[misc, name-defined]
            """布局 B 的 app —— 只实现 `layout()`，其它全在共用层里。"""

            def layout(self) -> None:
                placed.update(place_sections(self, self.frame, self.bridge))

            def recompute(self) -> None:
                """共用层每次重算完，本版的 Run count 面板跟着刷新。

                不覆盖的话面板会停在建界面那一刻的数字 —— 而这一版的**全部理由**
                就是「设定和 run 不同屏时，至少让你看见 run 数在变」。
                """
                super().recompute()
                refresh = placed.get("refresh_counts")
                if refresh is not None:
                    refresh()

        frame = shared.build(TabbedApp, parent, bridge)
        kit_name = SHARED_LAYER_MODULE
    else:
        from tkinter import ttk

        frame = ttk.Frame(parent)
        placed = place_sections(None, frame, bridge)
        kit_name = ""

    frame.layout_name = LAYOUT_NAME
    frame.sections_built = placed.get("built", ())
    frame.dropped_hints = placed.get("dropped", ())
    frame.widget_kit_name = kit_name
    frame.minsize_applied = placed.get("minsize")
    frame.notebook = placed.get("notebook")
    frame.refresh_counts = placed.get("refresh_counts")
    frame.total_label = placed.get("total_label")
    return frame


class NullBridge:
    """一个只满足冻结面的 bridge —— 独立 headless 冒烟专用（共用层缺席时的那条路）。

    它证明的只有一件事：**控件树建得起来**。业务对不对由 `gui.state.GuiState`
    和它自己的测试负责。故意不 import model：冒烟不该依赖任何还在写的模块。
    """

    def load_spec(self, path: str) -> None:
        return None

    def plan(self) -> None:
        return None

    def start(self, *, dry_run: bool = False) -> None:
        return None

    def tick(self) -> TickReport | None:
        return None

    def cancel(self) -> None:
        return None

    def runs(self) -> tuple:
        return ()

    def designs(self) -> tuple:
        return ()

    def axes(self) -> tuple:
        return ()

    def command_text(self, run_id: str) -> str:
        return ""

    def summary(self) -> dict:
        return {}


def smoke() -> int:
    """独立 headless 自检：建完整棵控件树就销毁，**不进 mainloop**。退出码就是判据。

    走的是「只满足冻结面的 bridge」那条路 —— 共用层还没写完时也能证明本布局自己是好的。
    tkinter 没装 / 没有显示 → 打印一句人话后**退 0**（平台降级，tier 3）。
    tkinter 在、建树炸了 → 让异常原样抛出去，那才是真 bug。
    """
    try:
        import tkinter as tk
    except ImportError as exc:  # pragma: no cover - 本机装了 tkinter
        print("smoke %s: skipped, tkinter is not installed (%s)" % (LAYOUT_NAME, exc))
        return 0
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - 本机有显示
        print("smoke %s: skipped, no display (%s)" % (LAYOUT_NAME, exc))
        return 0
    try:
        root.withdraw()
        frame = build_frame(root, NullBridge())
        frame.pack(fill="both", expand=True)
        root.update_idletasks()
        print(
            "smoke %s: ok, %d/%d sections, %d tabs, %d widgets, kit=%s"
            % (
                LAYOUT_NAME,
                len(frame.sections_built),
                len(SECTIONS),
                len(frame.notebook.tabs()),
                count_widgets(frame),
                frame.widget_kit_name or "none (placeholders)",
            )
        )
        if frame.widget_kit_name and frame.dropped_hints:
            # 静默丢掉布局提示 = 三版长得一样却没人发现，所以这里必须出声。
            print(
                "smoke %s: layout hints ignored by %s: %s"
                % (LAYOUT_NAME, frame.widget_kit_name, ", ".join(frame.dropped_hints))
            )
    finally:
        root.destroy()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """入口。三版共用 `gui._ui.frame_main`（同一份代码 → 三版行为必然一致）。

    `--smoke` 是本模块自己的逃生口：不经共用层、不经 `gui.app`，只证明这一版布局
    自己建得起来。共用层缺席时 `EWB_SMOKE=1` 也退回它 —— 闸门第 5 步不该因为
    别人的模块没写完而红。
    """
    from ewave_batch._stdio import ascii_safe_stdio

    ascii_safe_stdio()
    args = list(sys.argv[1:] if argv is None else argv)
    if "--smoke" in args:
        return smoke()
    shared = import_shared_layer()
    if shared is None or not callable(getattr(shared, "frame_main", None)):
        if os.environ.get("EWB_SMOKE") == "1":
            return smoke()
        print(
            "%s is not available; cannot open a window (try --smoke)." % SHARED_LAYER_MODULE,
            file=sys.stderr,
        )
        return 2
    return int(shared.frame_main(LAYOUT_NAME, args))


if __name__ == "__main__":
    raise SystemExit(main())
