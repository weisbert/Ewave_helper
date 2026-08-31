# -*- coding: utf-8 -*-
"""布局 A —— Stacked：单窗口纵向堆叠（草图 1a，`mockups/stacked.py`）。

Batch -> Designs -> Run groups -> Settings -> Resources -> Runs 全部同屏，
勾一下和它的后果隔不到几厘米。代价：Runs 表只剩 ~9 行。

**这个模块只负责摆放。** 九个 section 的控件由 `gui/` 共用层（`gui._ui.BaseApp`）的
`build_<section>` 出，frame 里不许有业务逻辑；要跟核心说话一律走 `bridge`
（`ewave_batch.model.GuiBridgeProtocol`）。这正是草图的结构：`mockups/_ui.py` 提供构件，
三个 frame 只写布局。

🚨 模块顶层**不许 import tkinter**，也不许 import 共用层（它自己 import tkinter）——
CLAUDE.md 硬约束 5：无 ``$DISPLAY`` 的纯 ssh 会话里 CLI 必须照常可用。
`python -m ewave_batch dry-run --self-test` 会 import 本模块，那时也不该碰显示。

headless 冒烟（`scripts/check.sh` 第 5 步）::

    EWB_SMOKE=1 python -m gui.frames.stacked     # 必须退 0

界面上给用户看的字符串一律**英文**（照 SNP_RLC_Extractor 的先例，用户 2026-08-18 定）；
代码注释仍然中文。
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

LAYOUT_NAME = "stacked"
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

名字对应共用层的 `build_<section>`：前八个和 `mockups/_ui.py` 的 `build_*` 一一对上，
`groups`（Run groups）是草图之后加的第九个，摆在 Designs 和 Settings 之间 ——
它管的是「哪些 design 跟哪些设定相乘」，位置就该在这两者中间。
布局只决定它们摆在哪、留几行，**不决定有没有**：少一个 section 就是三版分岔的开始。

⚠️ 共用层还没有 `build_groups` 时，`build_section` 自动退回 `_placeholder` ——
本版布局照样建得起来（那条 fallback 存在的理由正是这个）。
"""

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
"""`model.GuiBridgeProtocol` 的全部方法。测试断言这份清单与冻结面**逐字相等**。

写死一份是为了让参数检查不必 import model（frame 该能独立建起来），
但「写死的这份和冻结面对不上」必须当场红 —— 不许两边慢慢漂开。
"""

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
"""⚠️ **已报备的接口缺口**：`gui._ui.BaseApp` 建界面时用到的这几样，
`model.GuiBridgeProtocol` 里**没有**（冻结面只有 10 个方法，见 `BRIDGE_METHODS`）。

于是「满足冻结面」的 bridge 不一定喂得饱共用层。这里拿它当**判据**而不是当假设：
bridge 有这些 → 走共用层，建真界面；没有（例如只实现冻结面的测试替身）→ 走占位版，
布局照样建得起来、headless 冒烟照样退 0。

正解是把这几样并进 `GuiBridgeProtocol`（或从共用层里挪走），走
`[interface-change]` 流程 —— 已写进本阶段的 `interface_change_requests`。
在那之前，这份名单是两边之间唯一的桥，**不许悄悄扩张**。

2026-08-20 加了两条，各自记在这里（"不许悄悄扩张" = 扩张要留下理由，不是不许扩张）：

* `options` —— 其实早就在用了（`BaseApp._poll_ms` 读 `options().poll_interval`），
  只是**一直没列**。这属于补账，不是新增依赖。
* `set_max_parallel` —— 「同时在飞几个」那一格（用户 2026-08-20：提交 5 个、
  4 个 running、第 5 个停在 `ready`，「很奇怪」）。它必须能在批次**跑起来之后**改，
  所以不能走 `push()` 那条路，只能是 bridge 上一个自己的方法。
"""


def import_shared_layer() -> object | None:
    """惰性 import 共用构件层。没有 tkinter / 共用层还没写 → None，**绝不 ImportError**。

    理由：`gui.frames.*` 要能被 self-test import、要能在没有显示的机器上 headless 冒烟，
    这两件事都不该因为共用层缺席而炸。
    """
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

    单独一个函数是为了让测试能不起 Tk 就验它，**也为了让错误发生在建任何控件之前**：
    参数坏了要当场喊，不许静默建出半个界面 —— 半个界面比崩掉更难查。
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

    `kit` 是构件层：`gui._ui.BaseApp` 的实例，或 None（占位版）。
    `hints` 是**布局提示**（`rows=9` / `compact=True` / …）：构件层接哪个就给哪个，
    不接的**报告出来**而不是悄悄吞掉 —— 「Runs 表能留几行」正是三版分岔的地方，
    静默丢失会让三个布局长得一模一样却没人发现。
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
"""minsize 最多占屏幕的几成。**上限存在的理由不是好看，是「窗口比屏幕大就没法用」**：
最小尺寸一旦超过屏幕，标题栏可能落在可视区外，窗口既拖不动也关不掉。
超出的那部分交给布局里自带滚动条的构件去吸收（本版是 Runs 表，见 `place_sections`）。
"""


def apply_minsize(frame: object, *, fraction: float = MINSIZE_SCREEN_FRACTION):
    """把顶层窗口的 `minsize` 设成**现算的**「这版布局真正需要多大」，返回设下去的 (w, h)。

    2026-08-19 实拍：三版一个都没有 `minsize()`，于是窗口随手一缩就把动作栏切掉、
    `Submit` 的 `winfo_ismapped()` 直接是 0 —— 点不到的按钮是功能性缺陷，不是观感问题。

    尺寸**必须现算**（`winfo_reqwidth/reqheight`），不许写死像素：红区是 Linux，
    默认字体度量和开发机不同，同样的内容更宽更高。`gui/frames/split.py` 里那个写死的
    `LEFT_WIDTH = 452` 已经在这件事上栽过一次（左栏第 5 个勾选框整个点不到），别栽第二次。

    **调用时机也是判据的一部分：在 `recompute()` 往界面里填内容之前。**
    否则批次目录那种长路径会被算进"最低要求"，最小窗口被一条路径撑到一千多像素宽。

    `frame` 不是直接建在顶层窗口里（= 被嵌进别人的容器）→ **静默跳过**：
    那时候窗口不是我们的，替别人定最小尺寸是越界。
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


def place_sections(kit: object | None, root: object, bridge: object) -> dict:
    """把九个 section 摆进 `root` —— **本版布局的全部内容**，两条路共用这一份。

    摆放照 `mockups/stacked.py`：批次栏在顶、动作栏 + 状态栏在底，中间从上到下
    Designs / Run groups / Settings / Resources / Runs / Selected run。

    🚨 **pack 的顺序就是抢空间的顺序** —— 这是 2026-08-19 那个「Submit 点不到」的
    直接病根，不是风格问题。原来 designs…actionbar 全塞在同一个 `body` 里、
    `runs` 还拿着 `expand=True`：整棵树要 973px，窗口只有 900，于是排在 runs 后面的
    detail 和**整条动作栏**被挤到窗口外，`btn["Submit"].winfo_ismapped()` 返回 0。
    现在改成：

    * 动作栏和状态栏**先**从底部拿走自己那份（`side=BOTTOM`，且在 `body` 之前 pack），
      于是它们永远在屏幕上 —— 无论窗口多矮、字体多大；
    * `body` 最后 pack，只拿剩下的；
    * `body` 内部同理，`runs` **最后** pack ⇒ 空间不够时被压缩的是那张**自带竖滚动条**
      的表（压瘦了还能滚），而不是排在它后面、一滚都不会滚的东西。

    返回 `{"built": (...), "dropped": (...), "minsize": (w, h) | None}`，好让
    `build_frame` 挂到 frame 上、让测试不起整个 app 也能验「递给构件层的布局参数对不对」。
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

    section("batchbar", root).pack(side=tk.TOP, fill=tk.X)
    ttk.Separator(root, orient=tk.HORIZONTAL).pack(side=tk.TOP, fill=tk.X)

    # 先建、**最后 pack**：body 只拿动作栏和状态栏挑剩下的空间。
    body = ttk.Frame(root, padding=(8, 6))

    # 草图 1a 的取舍：Designs 只留 2 行、Runs 只留 9 行，换「全部同屏」。
    # `buttons="three"` = Add / Duplicate / Remove —— 复制一行再改，比删掉重敲三个字段快
    # 得多；共用层的 `dup_design` 早就实现了，之前只有 tabbed 传了这个参数。
    #
    # ⚠️ **建的顺序和 pack 的顺序在这里故意不是同一个** ——
    # 建的顺序必须是 `SECTIONS` 那个规范顺序（`place_sections` 的 `built` 被
    # `tests/test_gui_frames.py` 逐字断言，三版一致靠它）；而 pack 的顺序是
    # **抢空间的顺序**，见下面那段。所以先全建出来，再按另一个顺序 pack。
    designs = section("designs", body, widths=(250, 250, 210), rows=2, buttons="three")
    groups = section("groups", body)
    settings = section("settings", body, compact=False, show_formula=False)
    resources = section("resources", body)
    runs = section("runs", body, rows=9)
    detail = section("detail", body)

    # 🚨 **detail 第一个 pack**（`side=BOTTOM`），排在那四块固定高度的 section 之前。
    # 原来它虽然也写着 `side=BOTTOM`，但排在 designs/groups/settings/resources
    # **后面** —— 而 pack 是**按调用顺序**分配空间的，那四块先把 body 吃光，
    # detail 照样被挤出去。2026-08-31 实测（stacked，1560px 宽，选中一条 failed run）：
    # `Output log` 的底边恒在 920px，窗口 900/850/800 三档全部掉在外面 ——
    # 和 split 那条是同一个病，只是阈值不同。「必须点得到的东西排最前面」
    # 这条纪律对 body 内部同样成立，不是只对 root。
    detail.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))
    designs.pack(side=tk.TOP, fill=tk.X, pady=(4, 6))
    groups.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))
    settings.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))
    resources.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))
    # runs **最后** pack ⇒ 空间不够时被压缩的是那张自带竖滚动条的表（压瘦了还能滚）。
    runs.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    # 动作栏和状态栏建在 root 上、**在 body 之前**从底部 pack（见上面那段长注释）。
    # `side=BOTTOM` 时先 pack 的更靠下 ⇒ 状态栏在最底，动作栏在它上面。
    actionbar = section("actionbar", root)
    statusbar = section("statusbar", root)
    statusbar.pack(side=tk.BOTTOM, fill=tk.X)
    ttk.Separator(root, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X)
    actionbar.pack(side=tk.BOTTOM, fill=tk.X)
    ttk.Separator(root, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X)

    body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    return {
        "built": tuple(built),
        "dropped": tuple(dropped),
        "minsize": apply_minsize(root),
    }


def build_frame(parent: object, bridge: GuiBridgeProtocol) -> object:
    """建 stacked 版的主 frame，返回它的根 widget。

    有共用层且 bridge 喂得饱它 → 真界面（`gui._ui.build` 造 app，`layout()` 里摆 section）；
    否则 → 占位版（同样九个 section，控件是占位框）。走了哪条路看 `frame.widget_kit_name`。

    入参坏了抛 `TypeError`，**在建任何控件之前**。
    """
    problems = describe_argument_problems(parent, bridge)
    if problems:
        raise TypeError("gui.frames.%s.build_frame: %s" % (LAYOUT_NAME, "; ".join(problems)))

    shared = import_shared_layer()
    placed: dict = {}
    if shared_layer_usable(shared, bridge):

        class StackedApp(shared.BaseApp):  # type: ignore[misc, name-defined]
            """布局 A 的 app —— 只实现 `layout()`，其它全在共用层里。"""

            def layout(self) -> None:
                placed.update(place_sections(self, self.frame, self.bridge))

        frame = shared.build(StackedApp, parent, bridge)
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

    走的是「只满足冻结面的 bridge」那条路 —— 于是这条冒烟不依赖 `gui.app` / `gui.state`，
    共用层还没写完时也能证明本布局自己是好的。整条产品路径由
    `EWB_SMOKE=1 python -m gui.frames.stacked` 走（那条会经过共用层）。

    tkinter 没装 / 没有显示 → 打印一句人话后**退 0**：那是平台降级（tier 3），
    和 self-test 把 import 失败报成 `blocked:` 是同一个判断。
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
            "smoke %s: ok, %d/%d sections, %d widgets, kit=%s"
            % (
                LAYOUT_NAME,
                len(frame.sections_built),
                len(SECTIONS),
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
