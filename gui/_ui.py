"""`gui._ui` —— 三版布局**共用**的控件与逻辑。内容一套，布局三种。

结构照 `mockups/_ui.py`（那是已定稿的界面草图）：内容做成一组可复用的 section builder，
`gui/frames/{stacked,tabbed,split}.py` 只负责"把 section 摆在哪"。方法名与草图逐个对齐，
照着草图搬布局的人不用再查一遍。

与草图的**三处刻意不同**（都是"接了后端之后草图那样做会说谎"）：

1. **不接假数据。** 全部数字来自 `gui.state.GuiState`（→ `core.matrix` / `core.cmd`）。
   草图里的 `DEMO_MIX` / `expand()` 一行都没抄过来。
2. **`Selected run → Command` 是一个多行只读 Text**，不是草图里的单行 Entry。
   冻结面 `GuiBridgeProtocol.command_text` 明说"一行一个 flag 的可读形式"，
   而一条真命令有 20 多个 flag —— 塞进单行输入框等于看不见。
3. **字体不写死 `Segoe UI` / `Consolas`**，从 Tk 的具名字体 `TkDefaultFont` /
   `TkFixedFont` 拷贝。部署目标是红区的 Linux，写死 Windows 字体名到那边会被
   静默替换成难看的默认字体。

🚨 `EWB_SMOKE=1` 时**不建对话框的 `grab_set`、不进主循环** —— headless 冒烟要能
建完整棵控件树就退（`scripts/check.sh` 第 5 步）。

🚨 本文件零站点标识符：界面上的一切具体取值要么是用户输入的，要么来自
`core.discover` 的运行时解析（CLAUDE.md 硬约束 1b）。
"""

from __future__ import annotations

import os
import shutil
import sys
import tkinter as tk
from collections.abc import Callable, Sequence
from tkinter import filedialog, font as tkfont, messagebox, ttk

from ewave_batch.core import spec as spec_module
from ewave_batch.model import BASE_GROUP, EwaveBatchError, Run

from . import state as gui_state
from .app import SMOKE_ENV, smoke_enabled
from .trace import ActionTrace, _clip as _trace_clip, wrap as _trace_wrap

SMOKE = os.environ.get(SMOKE_ENV) == "1"
"""import 时的快照。**判定一律走 `smoke_enabled()`** —— 测试要能在 import 之后打开它。"""

# 界面语言 = 英文（照 `C:\\code\\SNP_RLC_Extractor` 的先例，用户 2026-08-18 定）。
# 代码注释仍然写中文。

BLUE = "#123f7a"
GREEN = "#1a7f37"
GREY = "#7a7a7a"
HINT = "#666666"
RED = "#8d1f1f"

STATUS_STYLE: dict[str, tuple[str, str]] = {
    "ready": ("#ffffff", "#5c5c5c"),
    "pending": ("#eaeef4", "#3c4a5c"),
    "running": ("#d5e5f7", "#123f7a"),
    "done": ("#dcecdc", "#1a5c26"),
    "failed": ("#f6d8d8", "#8d1f1f"),
    "skipped": ("#ececec", "#8d8d8d"),
}
"""6 个状态的配色（bg / fg）。键**恰好**是 `model.RunStatus` 的 6 个取值 ——
`tests/test_gui_common.py` 用计数断言盯着这件事（少一个状态 = 那一行没颜色，
而"没颜色"看起来就像"ready"）。"""

RUN_COLS: tuple[tuple[str, str, int], ...] = (
    ("n", "#", 36),
    ("design", "Design", 240),
    ("group", "Group", 96),
    ("corner", "Corner", 80),
    ("temp", "Temp", 60),
    ("mode", "Mode", 120),
    ("extra", "Extra axes", 120),
    ("status", "Status", 86),
    ("wall", "Wall time", 90),
    ("job", "Job id", 100),
)
"""Runs 表的列。第三个数是**下限**，不是最终宽度。

⚠️ 2026-08-19 实拍：这里原来那一套宽度（design 150、mode 92、wall 74）比内容窄一大截 ——
两个不同的 design 都显示成同一串前缀（看不出是哪个）、表头 `Wall time` 自己被切成
`Wall tim`、九列合起来比表还宽于是 `Job id` 整列在右边界外。所以现在有两道防线：

1. 这里的数字管"再空的表也不许比表头窄"；
2. `_fit_tree_columns()` 建完/每次重画之后**按内容 + 表头现算**一次，只涨不缩。

为什么必须现算而不是把数字调大一点了事：红区是 Linux，默认字体度量与开发机不同，
同样的字符串在那边更宽 —— 任何写死的像素到气隙对面都会重新变成一个裁剪 bug
（`gui/frames/split.py` 的 `LEFT_WIDTH` 已经在这件事上栽过一次）。写死的数字只当下限，
真实宽度由 `tkfont.measure` 在**运行的那台机器上**说了算。

`Group` 那一列说的是这个 run **出自哪个组**（`Run.group`）。

它与 `Extra axes` 是两回事，两列都得在：`Extra axes`（= `axes_slug`）是「这个 run 的
落地目录里编了哪几根轴」，`Group` 是「用户在哪一行配出来的」。两个组写了同一个
温度时它们会**折叠成一个 run**（保留先出现的那个组），于是 `Group` 也是"我那条组
为什么只贡献了 0 个 run"的唯一现场解释。
"""

RUNS_VIEWPORT_WIDTH = 660
"""Runs 表**请求**多宽（不是"最多多宽"）。见 `_scrolled_tree_grid` 的 `viewport_width`。

十列的下限加起来是 1000 出头，而"下限"是逐条量出来的（design 装得下两个不同的
design key、`Wall time` 表头不被切成 `Wall tim`…）。让这个和去决定窗口的最小宽度，
等于说"这个工具在 1000px 以下没法用" —— 而它明明有横向滚动条。
660 的口径是"最少要一眼看见 # / Design / Group / Corner / Temp 这几列"。
"""

RUN_STRETCH_COLS: frozenset[str] = frozenset({"design", "extra"})
"""窗口变宽时把多出来的空间分给哪几列。

`extra` 一个人 stretch 是个坏选择：它的内容是 `axes_slug`，短的时候只要 93px，
而窗口一宽它能白占 459px —— 与此同时 `design`（内容 278px）被钉在 150px 上裁着。
"""

RUN_COL_CAP_CHARS: dict[str, int] = {"design": 46, "extra": 38}
"""这两列最多显示多少个**等宽字符**（不是多少像素）。

内容长度没有上界（design key 是三段拼的，`axes_slug` 随轴数增长），不封顶就是让表格
宽度由最长的一条数据决定 —— 见 `GROUP_SUMMARY_CAP_CHARS` 的同一条理由。

⚠️ 口径是**字符数**而不是像素数，这一条是 2026-08-19 视觉复验改的：原来写的是
`{"design": 340, "extra": 280}`，而把 Tk 具名字体放大 30%（模拟红区 Linux 的字体度量）
之后，design 那一列的内容要 349px、上限还钉在 340px ⇒ 最长的那个 design key **末尾被
静默切掉一个字符**。写死的像素到气隙对面就是另一个裁剪 bug，这正是 `LEFT_WIDTH`
栽过的那一跤 —— 上限也得跟着字走。字符数由 `_cap_px()` 在**运行的那台机器上**换算成像素。
"""

GROUP_COLS: tuple[tuple[str, str, int], ...] = (
    ("name", "Group", 108),
    ("summary", "Settings", 240),
    ("runs", "Runs", 54),
)
"""Run groups 表的三列：组名 / 这个组改了什么 / 它贡献几个 run。

第三列是**去重之后**的数（`bridge.group_run_counts()`）—— 显示展开前的数字会让
"两个组都写了 55 度"看起来像跑了两遍，而实际只跑一遍。
"""

GROUP_SUMMARY_CAP_CHARS = 43
"""组的「Settings」那一列最多显示多少个等宽字符。

它装的是 `bridge.group_summary()` 的自由文本，长度**没有上界**（base 组会把每一根
轴都写进去）。不封顶就等于让这一块面板的宽度由文字长度决定，整个左栏会被顶到窗口外。
封顶之后靠横向滚动条够得着 —— "够得着"和"全塞进来"是两回事。

口径是字符数不是像素数，理由同 `RUN_COL_CAP_CHARS`。
"""

GROUP_ROW_AXES: dict[str, tuple[str, ...]] = {
    "corner": ("corner",),
    "temperature": ("temperature",),
    "fullWave": ("fullWave",),
    "mesh": ("mesh",),
    "advanced": ("equalCurrent",),
}
"""Settings 里**一行**对应哪几根轴 —— 那个"覆盖"勾选框是按行给的，不是按轴给的。

⚠️ **三根轴故意不在这里**（= 它们只属于 base，编辑别的组时整块置灰）：

* `freq`：界面上它不是一个取值列表而是一整排格子（模式 + start/stop/step/points），
  一个组要换扫频等于换四个格子的组合，塞进"勾一下就覆盖"的模型里表达不了。
* `relativeTolerance` / `relativeCurrentTolerance`：它们与 equalCurrent 挤在同一个
  Advanced 折叠块里，一度是跟着那个勾选框一起覆盖的 —— 但那样一勾就会把**当前的**
  tolerance 值钉成这个组的显式取值，之后用户改 base 的 tolerance，这个组会**静默**
  留在老值上（用户从来没要求过覆盖它）。收敛容差本来也是整批的性质而不是变体的轴，
  所以宁可少一个能力，也不留这种"看起来跟着变、其实没跟着变"的坑。
"""

GROUP_EDIT_HINT = (
    "editing group %r  -  a greyed row is not this group's: tick the box on its left to let "
    "this group set that axis. Frequency sweep and the two tolerances are per batch "
    "(base only) - switch to base to change them."
)
"""编辑 base 之外的组时 Settings 底下那行灰字。

灰掉的控件必须说明自己为什么灰。用户 2026-08-20 报的「duplicate 出来的组有些输入框
根本填不了」里，有一半根本不是 bug 而是这条规矩（继承的行不给编辑、扫频只属于 base），
只是界面一个字都没说 —— 一个不解释自己的禁用状态，跟坏了没有区别。
"""

GROUP_NAME_HINT = (
    "The name shows up in the Runs table, in every message about this group, and in the "
    "output directory names. Leave it as suggested if you have no better one."
)
"""建组 / 复制组那个输入框底下的灰字。**目录名**那一句是重点：
组名不是个装饰，三个月后翻产物目录的人只有它可看。"""

RUNS_EMPTY_HINT = (
    "Nothing to run yet.\n"
    "Add a design and tick at least one corner and temperature -\n"
    "runs appear here as you type."
)
"""空表时表中央那句话。**只在"确实还没配"时才对**，见 `RUNS_EMPTY_BLOCKED`。"""

RUNS_EMPTY_BLOCKED = (
    "The current settings cannot be expanded.\n"
    "See the message at the bottom of the window -\n"
    "runs appear here as soon as it is fixed."
)
"""空表 + 有错时表中央那句话。

原来这里恒定是 `RUNS_EMPTY_HINT`。2026-08-20 截图实证：两行一模一样的 design
（按一次 Duplicate row 就是）⇒ 矩阵被拒 ⇒ 表空 ⇒ 中间写着"加一个 design、
勾至少一个 corner 和温度"，而 design 有两行、corner 也勾着。
这是 `preflight()` 那条错建议的**第三个家** —— 同一句误导在界面上有三处出口
（preflight / 空表提示 / 陈旧的表），一处一处堵。
"""

RUNS_STALE_WARNING = (
    "!  These rows are the LAST VALID preview, not the current settings - the settings "
    "below do not expand (see the message at the bottom of the window). Fix that and the "
    "table refreshes by itself."
)
"""Runs 表上方那条红字。

为什么必须有：设定一旦临时非法（重复的 design 行、取消掉最后一个 corner、
温度框清空重打、spacing 选了 points 还没填数），表里留着的是**上一次**的矩阵，
而 Total 那行公式写的是现算的 `= 0 runs` —— 两块面板读两个源，界面自己跟自己打架。
表不跟着清空是**有意**的（每敲一个键闪一次比陈旧更难用），所以缺的不是刷新，
是"这张表现在是旧的"这句话。
"""

MAX_PARALLEL_TIP = (
    "How many jobs may sit in the scheduler at once.\n"
    "The rest wait at 'ready' and go in as slots free up - that is why a 5th run can "
    "stay 'ready' while 4 are running.\n"
    "Changing this works while the batch is running: the next poll uses the new value."
)
"""「同时在飞」那一格的悬停提示。**第二句是全部要点** ——
`ready` 在这个工具里是"还没提交"，不是"没跑成"，而这一点在界面上没有别的地方讲。"""

OVERRIDE_TIP = (
    "%s\n"
    "  ticked  : this run group sets it itself\n"
    "  unticked: follow base (the row stays greyed)\n"
    "Editing base? then there is nothing to inherit from - the box is fixed on."
)
"""Settings 第 0 列那个勾选框的悬停提示。

那一列没有表头（加一行表头要把整张 grid 的行号全挪一遍），于是"这个小方块是干嘛的"
在界面上无解 —— 而它恰好是新建的组里**唯一**能让那些灰格子活过来的开关。
"""

LOG_GEOMETRY = "1060x620"
"""Log 窗口的起始大小。一条 ewave 命令 20 多个 flag、拼出来轻松过 300 字符 ——
窄窗口把它折成一团，"拷出去给别人看"就得先滚三屏。"""

LOG_ROWS = 24
"""Log 窗口正文留几行。"""

LOG_RULE = "-" * 78

LOG_CONT_INDENT = " " * 27
"""多行 message 的续行缩进 = `_log_line` 里定长前缀（序号+时刻+kind）的宽度。

核心那些错误信息**自带换行**（一句"是什么坏了" + 一句 `Next:` 该怎么办），
直接塞进一行会把整份日志的列对齐全带跑；而把换行吃掉又正好丢掉那句最有用的
`Next:`。所以保留换行、把续行缩进到 run_id 那一列 —— 一眼看得出它属于上一条。
"""

LOG_EMPTY = (
    "(nothing yet - press Dry-run to build every command without submitting anything, "
    "or Submit to actually run them)"
)

TRACE_HINT = (
    "Developer log - every click, every dialog, every swallowed error, plus a state line "
    "after each redraw.  Reading a state line: active=<group being edited>  "
    "sel=<row selected in the group table>  groups=[name{overridden axes}]  "
    "axes[corner=1N ...] where 1/0 = the override box and N/D = the row is editable/greyed.  "
    "1N and 0D are normal; 1D or 0N means the widgets and the model disagree - that is the bug.  "
    "Press Clear, reproduce the problem, then Copy all."
)
"""Developer log 窗口底下那行灰字。**必须解释怎么读那一行快照** ——
一行 `active=... axes[corner=1N ...]` 不自带图例的话，它对任何人（包括三个月后的
我们自己）都只是一串噪声。"""

LOG_HINT = (
    "Read-only. Select with the mouse, Ctrl-A selects all, Ctrl-C copies. "
    "'Copy for sharing' replaces site names (library / cell / ptxt / paths) with "
    "placeholders first."
)

_LOG_NAV_KEYS: frozenset[str] = frozenset(
    {
        "Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next",
        "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
    }
)
"""只读 Text 里**放行**的按键：移动光标和按住修饰键，一个都不改内容。"""

MENU_ITEMS: tuple[str, ...] = (
    "Open output dir",
    "Copy command",
    "-",
    "Re-run this one",
    "Set as current",
)
"""右键菜单。「Open log」在草图里也有，这里合进 `Open output dir`
（日志就在那个目录里，多一条只是多一次会走空的路径）。"""

NOT_IMPLEMENTED_SUFFIX = " (not implemented)"
"""接不上的菜单项后面加这个，并且**置灰**。

2026-08-19 之前它们是能点的，点下去弹一个写着「Not wired yet」的对话框。
那比没有还糟：一个能按的按钮就是一句"这件事我能做"的承诺，而它做不到 ——
用户得按一次才知道。置灰 + 写清楚，一眼就看得出来。
"""

DISABLED_MENU_ITEMS: frozenset[str] = frozenset(
    {
        # bridge 没有"只重跑某一个 run"这条路（`resume()` 的粒度是整批里没成的那些），
        # 界面自己拼一条出来就是第二份调度逻辑。
        "Re-run this one",
        # `core.layout.set_run_as_current` 是有的，但它要往设计师的 spine 里写
        # （硬约束 4：覆盖前备份 + 记日志），而 bridge 一个口子都没开出来。
        "Set as current",
        # doctor 是部署包里的一个 shell 脚本，红区才有；从界面里起一个子进程去跑它
        # 属于另一条分工线上的活。
        "Check environment (doctor)",
    }
)
"""**确实接不上**的那几项。名单在这儿是为了让"为什么灰"有个能读的答案 ——
每一条后面都写了理由，将来谁接上了就从这里删掉。"""

CMD_ROWS_MIN = 2
CMD_ROWS_MAX = 8
"""`Selected run -> Command` 那个框的行数区间。见 `show_detail`。"""

CMD_TREE_FLOOR_ROWS = 3
"""Command 框长高时，Runs 表至少得留下几行。见 `BaseApp._cmd_rows_within_budget`。

`CMD_ROWS_MAX = 8` 那句「上限 8 行是为了不把 Runs 表挤瘦」在**窗口装得下**的时候是对的，
在装不下的时候是空话：stacked 那棵树本来就比屏幕高，8 行里多出来的 6 行**全从 Runs 表扣**。
所以真正的上限不是一个常数，而是"表里还富余几行"。
"""

_NL = chr(10)
"""换行符。拼多行提示文案用它（`_preflight_blocks` 那几条要在对话框里分段）。"""

_DASH = "-"
"""空值的占位符。**用 ASCII 的连字符**，不是 em dash —— 红区 `LANG` 常是 `C`，
界面字符串走的又是 Tk 不是 stdout，一个非 ASCII 字符在那边多半渲染成方块。"""


def _label(text: str) -> str:
    """空字符串 → 占位符。"""
    return text if text else _DASH


# --------------------------------------------------------------------------
# 「不让内容被裁掉」的几个小工具。
#
# 它们都刻意**不写死像素**：部署目标是红区的 Linux，字体度量与开发机不同，同一串字
# 在那边更宽。`tkfont.measure` 在**运行的那台机器上**量，量出来多少就是多少。
# --------------------------------------------------------------------------

ELLIPSIS = "..."
"""省略号用三个 ASCII 句点，不用 U+2026 —— 红区 `LANG` 常是 `C`。"""


def _elide(text: str, font: object, budget: int, middle: bool = True) -> str:
    """把 `text` 截到 `budget` 像素以内。`middle=True` 时保头保尾（路径要的就是这个）。

    二分搜索留几个字符，不是从长到短一个个试：这个函数挂在 `<Configure>` 上，
    拖一次窗口能被调用几十次，而 `font.measure` 不是免费的。
    """
    if budget <= 0 or not text:
        return ""
    measure = font.measure  # type: ignore[attr-defined]
    if measure(text) <= budget:
        return text
    if measure(ELLIPSIS) > budget:
        return ""

    def render(keep: int) -> str:
        if not middle:
            return text[:keep] + ELLIPSIS
        head = (keep + 1) // 2
        tail = keep - head
        return text[:head] + ELLIPSIS + (text[len(text) - tail:] if tail else "")

    low, high = 0, len(text) - 1
    while low < high:
        mid = (low + high + 1) // 2
        if measure(render(mid)) <= budget:
            low = mid
        else:
            high = mid - 1
    return render(low)


class _ElideLabel(ttk.Label):
    """一条**按实际可用宽度中间省略**的标签。给长路径和长提示语用。

    为什么不是普通 `ttk.Label`：普通标签"需要多宽"由文字长度决定，于是
    ①一条批次目录路径（实测要 1336px）会把整条动作栏撑到窗口外，②窗口一窄，
    文字就从中间断掉、连头带尾一起丢。两种都发生过（2026-08-19 实拍 B1/B2）。

    做法：`width=` 给一个很小的字符数把**请求宽度**钉住（这样它撑不大窗口），
    `pack(fill=X, expand=True)` 让它拿到剩下的空间，再在 `<Configure>` 里把文字
    截到那个实际宽度。于是"请求"永远小于"给到"—— 也就永远不会被裁。
    """

    def __init__(self, master: object, *, font: object, chars: int = 10, **kwargs: object) -> None:
        super().__init__(master, width=chars, **kwargs)  # type: ignore[arg-type]
        self._full = ""
        self._font = font
        self._shown: str | None = None
        self.bind("<Configure>", self._refit, add="+")

    def set_text(self, text: str) -> None:
        self._full = text or ""
        self._refit()

    def full_text(self) -> str:
        return self._full

    def _refit(self, _event: object = None) -> None:
        available = self.winfo_width()
        # 还没映射时 `winfo_width()` 是 1 —— 那时候显示全文，反正 `width=` 已经把请求
        # 宽度钉住了，撑不大谁。第一次 <Configure> 到了自然会截。
        text = self._full if available <= 1 else _elide(self._full, self._font, available - 4)
        if text != self._shown:
            self._shown = text
            # 改文字会再触发一次 <Configure>。上面那个 `!=` 就是收敛条件，
            # 去掉它就是一个无限重排（界面表现为窗口"抖"）。
            self.configure(text=text)


class _Tooltip:
    """悬停提示。内容**惰性取**（`text` 可以是个函数，悬停时才调），路径变了不用重挂。

    自己写而不是找个库：硬约束 2，只用 stdlib。
    """

    DELAY_MS = 500

    def __init__(self, widget: object, text: object) -> None:
        self.widget = widget
        self.text = text
        self._after: str | None = None
        self._win: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")  # type: ignore[attr-defined]
        widget.bind("<Leave>", self._hide, add="+")  # type: ignore[attr-defined]
        widget.bind("<ButtonPress>", self._hide, add="+")  # type: ignore[attr-defined]

    def _schedule(self, _event: object = None) -> None:
        self._hide()
        if smoke_enabled():
            return
        try:
            self._after = self.widget.after(self.DELAY_MS, self._show)  # type: ignore[attr-defined]
        except tk.TclError:  # pragma: no cover - 控件已经没了
            self._after = None

    def _show(self) -> None:
        self._after = None
        content = self.text() if callable(self.text) else str(self.text)
        if not content or content == _DASH:
            return
        try:
            win = tk.Toplevel(self.widget)  # type: ignore[arg-type]
            win.wm_overrideredirect(True)
            x = self.widget.winfo_rootx() + 12  # type: ignore[attr-defined]
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4  # type: ignore[attr-defined]
            win.wm_geometry("+%d+%d" % (x, y))
            tk.Label(
                win,
                text=content,
                justify=tk.LEFT,
                background="#ffffe0",
                relief=tk.SOLID,
                borderwidth=1,
                wraplength=520,
            ).pack()
            self._win = win
        except tk.TclError:  # pragma: no cover
            self._win = None

    def _hide(self, _event: object = None) -> None:
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)  # type: ignore[attr-defined]
            except tk.TclError:  # pragma: no cover
                pass
            self._after = None
        if self._win is not None:
            try:
                self._win.destroy()
            except tk.TclError:  # pragma: no cover
                pass
            self._win = None


HEAD_PAD = 26
"""表头文字之外还要留多少像素（排序箭头 + 左右内边距 + 列分隔线）。"""

CELL_PAD = 16
"""单元格文字之外还要留多少像素。"""


def _fit_tree_columns(
    tree: ttk.Treeview,
    columns: Sequence[str],
    *,
    head_font: object,
    cell_font: object,
    floors: dict,
    caps: dict | None = None,
) -> None:
    """按**表头 + 当前所有行的内容**现算每一列该多宽。只涨不缩。

    三条不显然的规矩：

    1. **任何一列都不许比自己算出来的宽度窄** —— `minwidth` 给的就是 `want`，不是表头宽。
       这一条 2026-08-19 复验时改过口径，理由是踩到了：原来 `minwidth=head_width`，
       而 `stretch=True` 的列（`design` / `extra`）在**视口比列宽和窄**的时候会被 ttk
       一路压回 `minwidth` —— 实测 split 版 `design` 算出来 277px、被压成 78px，
       于是两个不同的 design key 又双双显示成同一串前缀（正是这个函数要修的那个缺陷，
       从另一扇门走回来了）。更糟的是**横向滚动条对它们不起作用**：列本身变窄了，
       不是被推到视口外，滚过去看到的还是被切的字，而 Treeview 不画省略号。
       `minwidth=want` 之后 ttk 压不动它们，横向滚动条才真的接得住 ——
       而那正是它当初被加进来的理由。
       （表头宽度仍是下限的一部分：`want` 本身就是从 `head_width` 起步的。）
    2. **不回读 `tree.column(key, "width")` 当下限。** 那个值对 `stretch=True` 的列是
       "已经被拉伸之后"的宽度；拿它当下一轮的下限，下一轮拉伸会在它上面再加一次剩余
       空间 —— 列一轮比一轮宽、永不收敛。所以自己在 `tree` 上挂一份缓存。
    3. 只涨不缩：内容短了就把列缩回去，会让表格在每次刷新时**跳一下**（run 跑完
       Wall time 从 `-` 变成 `12:34`，整张表的列就集体位移）。宁可留点空。

    `caps` 给几列一个上限。有些列的内容长度**没有上界**（组的设定摘要、`axes_slug`），
    让它们自由生长会把整块面板顶到窗口外 —— 那是把一个裁剪问题换成另一个。
    有上限的那几列靠横向滚动条够得着，而不是靠把表撑到屏幕外。
    """
    cache = getattr(tree, "_ewb_col_widths", None)
    if cache is None:
        cache = {}
        tree._ewb_col_widths = cache  # type: ignore[attr-defined]
    rows = [tree.item(iid, "values") for iid in tree.get_children("")]
    for index, key in enumerate(columns):
        head_width = head_font.measure(tree.heading(key, "text")) + HEAD_PAD  # type: ignore[attr-defined]
        want = max(head_width, int(floors.get(key, 0)), int(cache.get(key, 0)))
        for values in rows:
            if index < len(values):
                want = max(want, cell_font.measure(str(values[index])) + CELL_PAD)  # type: ignore[attr-defined]
        limit = (caps or {}).get(key)
        if limit is not None:
            want = max(head_width, min(want, int(limit)))
        cache[key] = want
        # minwidth 必须是 want 而不是 head_width，否则 stretch 列会被 ttk 压回去 ——
        # 见本函数 docstring 的规矩 1。
        tree.column(key, width=want, minwidth=want)


def _scrolled_tree_grid(wrap: object, tree: ttk.Treeview, *, viewport_width: int = 0) -> None:
    """把一个 Treeview 连同**纵横两条**滚动条摆进 `wrap`（用 grid，不是 pack）。

    横向那条是 2026-08-19 补的：九列合起来比表宽，`Job id` 整列在右边界外，
    而只有纵向滚动条时那一列**根本够不着** —— 不是难看，是拿不到 job id。

    `viewport_width` 解决横向滚动条带来的**第二个**问题：`ttk.Treeview` 的"请求宽度"
    就是各列宽度之和，于是列一加宽，`gui/frames/*.py` 里那个"按内容现算 minsize"
    的窗口下限就跟着涨（实测 Runs 表一家把 split 的最小宽度顶到 1855px，
    比 0.85 屏宽还大 -> 被夹住 -> 窗口打开就是横着滚的）。既然内容已经有滚动条够得着，
    表就不该替整个窗口定下限：把 `wrap` 的请求尺寸钉在一个够用的视口宽度上
    （`grid_propagate(False)`），表照样随窗口变大而变宽，只是不再往上顶。

    ⚠️ 这**不是** `LEFT_WIDTH` 那种"硬钉死"：钉的是"我最少要这么宽"，不是"我最多这么宽"。
    `wrap` 仍然 `fill=BOTH, expand=True`，窗口给多少就占多少；给不够的部分靠滚动条。
    """
    wrap.rowconfigure(0, weight=1)  # type: ignore[attr-defined]
    wrap.columnconfigure(0, weight=1)  # type: ignore[attr-defined]
    vertical = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=tree.yview)  # type: ignore[arg-type]
    horizontal = ttk.Scrollbar(wrap, orient=tk.HORIZONTAL, command=tree.xview)  # type: ignore[arg-type]
    tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vertical.grid(row=0, column=1, sticky="ns")
    horizontal.grid(row=1, column=0, sticky="ew")
    if viewport_width:
        wrap.update_idletasks()  # type: ignore[attr-defined]
        # 高度照抄现在算出来的（= `height=rows` 那几行 + 表头 + 横向滚动条）；
        # 只有宽度是我们替它定的。
        wrap.configure(width=viewport_width, height=wrap.winfo_reqheight())  # type: ignore[attr-defined]
        wrap.grid_propagate(False)  # type: ignore[attr-defined]


def _center_on_parent(dialog: object, parent: object) -> None:
    """把对话框摆到父窗口正中偏上。

    2026-08-19 实拍：`Extraction defaults` 直接落到屏幕**左边界外**，左半截看不见。
    Tk 不会替你居中对话框，谁建谁自己放 —— 不放就是落在 (0,0) 附近碰运气。
    算完还要夹进屏幕范围：父窗口自己贴着屏幕边缘时，居中算出来的坐标可以是负的。
    """
    try:
        dialog.update_idletasks()  # type: ignore[attr-defined]
        width = dialog.winfo_reqwidth()  # type: ignore[attr-defined]
        height = dialog.winfo_reqheight()  # type: ignore[attr-defined]
        px, py = parent.winfo_rootx(), parent.winfo_rooty()  # type: ignore[attr-defined]
        pw, ph = parent.winfo_width(), parent.winfo_height()  # type: ignore[attr-defined]
        if pw <= 1 or ph <= 1:  # 父窗口还没映射 -> 退回屏幕中心
            px, py = 0, 0
            pw = dialog.winfo_screenwidth()  # type: ignore[attr-defined]
            ph = dialog.winfo_screenheight()  # type: ignore[attr-defined]
        x = max(0, min(px + (pw - width) // 2, dialog.winfo_screenwidth() - width))  # type: ignore[attr-defined]
        y = max(0, min(py + (ph - height) // 3, dialog.winfo_screenheight() - height))  # type: ignore[attr-defined]
        dialog.wm_geometry("+%d+%d" % (x, y))  # type: ignore[attr-defined]
    except tk.TclError:  # pragma: no cover - 窗口已经关掉的竞态
        pass


def _open_in_file_manager(path: str) -> str:
    """用系统文件管理器打开一个目录。返回空串 = 成功，非空 = 失败原因（给用户看）。

    **两条路都要有**：红区是 Linux（`xdg-open`），开发机是 Windows（`os.startfile`）。
    只做 Windows 那条就等于这个按钮在唯一真正要用它的机器上是死的。
    macOS 的 `open` 顺手带上，反正只是名单里多一个名字。

    `subprocess` 惰性 import：本模块只在 GUI 分支里被 import，但一个纯 CLI 会话
    没必要为一个按钮多加载一个模块。
    """
    if not path:
        return "no path yet"
    if not os.path.isdir(path):
        return "no such directory yet (nothing has been written there)"
    if smoke_enabled():  # 冒烟时不真去起一个文件管理器
        return ""
    startfile = getattr(os, "startfile", None)
    if startfile is not None:  # Windows
        try:
            startfile(path)
            return ""
        except OSError as exc:
            return str(exc)
    import subprocess

    for name in ("xdg-open", "open"):
        opener = shutil.which(name)
        if opener is None:
            continue
        try:
            subprocess.Popen(
                [opener, path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return ""
        except OSError as exc:
            return str(exc)
    return "no xdg-open (or open) on PATH"


def _make_readonly(text: tk.Text) -> None:
    """把一个 `tk.Text` 变成**只读但可选可拷**的。

    为什么不是 `state="disabled"`：那样在一部分 Tk 版本里连鼠标选中都不给，
    而"能选中、能 Ctrl-C"正是 Log 窗口存在的**全部理由**（用户 2026-08-20：
    「我可以 copy，把结果粘贴给你 debug」）。部署目标是红区的 Linux，Tk 版本和
    开发机不同 —— 赌"这个版本的 disabled 恰好还能选"就是赌一个只在气隙对面发作的 bug。

    所以改成拦按键：会改内容的一律 `break`，导航键和 Ctrl-C / Ctrl-A 放行。

    ⚠️ Ctrl-A 必须自己接：Tk 的 Text 默认把它绑成 emacs 的"行首"，
    而这个窗口里 90% 的操作是"全选然后拷走"。
    """

    def guard(event: object) -> str | None:
        keysym = str(getattr(event, "keysym", ""))
        if keysym in _LOG_NAV_KEYS:
            return None
        if int(getattr(event, "state", 0)) & 0x4 and keysym.lower() in ("c", "a", "insert"):
            return None
        return "break"

    def select_all(_event: object = None) -> str:
        text.tag_add(tk.SEL, "1.0", "end-1c")
        text.mark_set(tk.INSERT, "1.0")
        return "break"

    text.bind("<Key>", guard)
    text.bind("<Control-a>", select_all)
    text.bind("<Control-A>", select_all)


def _log_line(index: int, event: object) -> str:
    """一条 `DriverEvent` -> 一行。

    `message` 放**最后**：run_id 和 kind 的宽度有上界，命令没有 —— 把变长的那一列
    放中间，每一行的对齐都会被最长的那条命令带跑。

    时间只留时分秒：`at` 是整串 UTC ISO，日期在同一个批次里逐行相同，白占 11 格宽度。
    """
    stamp = str(getattr(event, "at", "") or "")
    clock = stamp[11:19] if len(stamp) >= 19 else (stamp or _DASH)
    kind = str(getattr(getattr(event, "kind", None), "value", "") or "?")
    who = str(getattr(event, "run_id", "") or getattr(event, "design_key", "") or _DASH)
    message = str(getattr(event, "message", "") or "")
    lines = message.splitlines() or [""]
    out = ["%4d  %8s  %-9s  %s  %s" % (index, clock, kind, who, lines[0])]
    out.extend(LOG_CONT_INDENT + line for line in lines[1:])
    return _NL.join(out)


class _LogWindow:
    """独立的 Log 窗口：driver 播过的**全部**事件，一条一行，可选、可拷、可存盘。

    ★ 为什么值得单独一扇窗（用户 2026-08-20：「LOG 窗口页做的不太好，应该有个专门
    打印 log 窗口的页面，我可以 copy」）：在这之前"日志"只有状态栏最后那一行。
    而 **dry-run 的全部产出就是那些命令** —— 一条 300 多字符、一批十几条，
    状态栏一条都装不下，`Selected run -> Command` 一次也只看得见一个 run。
    于是"我按了 dry-run，到底能不能跑"这个问题在界面上**没有地方**能回答。

    三个按钮对应三条真实用途：

    | 按钮 | 给谁 |
    |---|---|
    | Copy all | 自己贴进工单 / 邮件 / 另一个终端 |
    | Copy for sharing | 贴到**红区外面**去（问人、贴给助手）-> 走 `state.redact()` |
    | Save as... | 存成文件，跟着批次一起归档 |

    🚨 `Copy for sharing` 不是装饰。日志里逐字带着 library / cell / ptxt / 队列 /
    home 路径，CLAUDE.md 硬约束 1 说那些一个字都不许出红区。而"拷出来问人"是个真实
    且合理的需求 —— 不给一条**合规的**路，就会有人走那条不合规的。
    脱敏表和它的口径在 `gui.state.GuiState.redaction_map`（含"尽力而为"的交代）。
    """

    def __init__(self, app: "BaseApp") -> None:
        self.app = app
        self._doc = ""
        self.top = tk.Toplevel(app.top)
        self.top.title("eWave Batch - Log")
        try:
            self.top.geometry(LOG_GEOMETRY)
        except tk.TclError:  # pragma: no cover - 嵌进别人的窗口时
            pass
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.follow = tk.BooleanVar(value=True)

        bar = ttk.Frame(self.top, padding=(8, 6))
        bar.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(bar, text="Copy all", width=10, command=lambda: self._copy(False)).pack(
            side=tk.LEFT
        )
        ttk.Button(
            bar, text="Copy for sharing", width=17, command=lambda: self._copy(True)
        ).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="Save as...", width=11, command=self._save).pack(side=tk.LEFT)
        # 从这扇窗过得去 Developer log：用户找日志时找的是"Log"，而他要的东西
        # （点了什么、报了什么）在**另一扇**窗里 —— 菜单里那一条不够，这里也给一条。
        ttk.Button(
            bar, text="Developer log", width=15, command=self.app.show_trace
        ).pack(side=tk.LEFT, padx=4)
        ttk.Checkbutton(bar, text="Follow", variable=self.follow).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Button(bar, text="Close", width=8, command=self.close).pack(side=tk.RIGHT)
        self.count_lbl = ttk.Label(bar, text="", style="Hint.TLabel")
        self.count_lbl.pack(side=tk.RIGHT, padx=8)

        # 结论那一行。事件流回答"发生了什么"，这一行回答用户真正问的那句
        # 「到底可以跑了不」—— 所以它在最上面、有颜色、是个完整的句子。
        self.verdict = tk.Label(
            self.top,
            anchor=tk.W,
            justify=tk.LEFT,
            font=app.f_ui_b,
            padx=8,
            pady=4,
            wraplength=1000,
        )
        self.verdict.pack(side=tk.TOP, fill=tk.X)

        self.hint = ttk.Label(self.top, text=LOG_HINT, style="Hint.TLabel", wraplength=1000)
        self.hint.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(2, 6))
        # `wraplength` 的单位是**像素**，写死等于假定窗口有多宽 —— 用户把窗口拖窄，
        # 那句结论就在 1000px 处才折行，也就是右半句直接看不见。跟着窗口走。
        self.top.bind("<Configure>", self._refit_wraps, add="+")

        wrap = ttk.Frame(self.top, padding=(8, 0))
        wrap.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.text = tk.Text(
            wrap,
            height=LOG_ROWS,
            wrap="none",
            font=app.f_mono,
            relief=tk.SOLID,
            bd=1,
            background="#fbfbfb",
            foreground="#222222",
        )
        yscroll = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self.text.yview)
        xscroll = ttk.Scrollbar(wrap, orient=tk.HORIZONTAL, command=self.text.xview)
        self.text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        _make_readonly(self.text)

        # headless 冒烟/测试里**建得起来但不露脸**：这扇窗是 dry-run 跑完自动弹的，
        # 而测试要能验"它确实弹了"。`smoke_enabled()` 时整个不建的话，那条断言就只能
        # 去验一个中间布尔（= 验我们自己写的 if），验不到窗口本身。
        if smoke_enabled():
            self.top.withdraw()

    def _refit_wraps(self, _event: object = None) -> None:
        """两条整句的标签跟着窗口宽度折行。"""
        try:
            width = max(200, self.top.winfo_width() - 24)
        except tk.TclError:  # pragma: no cover - 窗口已经关掉
            return
        self.verdict.config(wraplength=width)
        self.hint.config(wraplength=width)

    # ------------------------------------------------------------- 生命周期
    def alive(self) -> bool:
        """窗口还在不在（用户可能已经关掉了）。"""
        try:
            return bool(self.top.winfo_exists())
        except tk.TclError:  # pragma: no cover - 解释器收尾时
            return False

    def close(self) -> None:
        try:
            self.top.destroy()
        except tk.TclError:  # pragma: no cover
            pass

    def present(self) -> None:
        """提到最前。已经开着时按 Log 就走这条 —— 不开第二扇。"""
        if not self.alive() or smoke_enabled():
            return
        try:
            self.top.deiconify()
            self.top.lift()
        except tk.TclError:  # pragma: no cover
            pass

    # --------------------------------------------------------------- 内容
    def document(self) -> str:
        """整份日志的文本。Copy / Save 拿的就是它，屏幕上显示的也是它 —— **一份**。"""
        bridge = self.app.bridge
        head = [
            "# eWave Batch log",
            "# batch      %s" % (getattr(bridge, "batch_name", "") or _DASH),
            "# batch dir  %s" % (bridge.batch_dir() or _DASH),
            "# official   %s"
            % (getattr(bridge, "official_run_dir", "") or "<empty - no site coordinates>"),
            "# python     %s on %s" % (sys.version.split()[0], sys.platform),
            "# state      %s" % bridge.status_line(),
            LOG_RULE,
        ]
        events = bridge.events()
        body = [_log_line(index, event) for index, event in enumerate(events, start=1)]
        if not body:
            body = [LOG_EMPTY]
        return _NL.join(head + body) + _NL

    def verdict_text(self) -> tuple[str, str, str]:
        """(那句话, 前景色, 背景色)。dry-run 之外的情况照抄状态栏那一句。"""
        bridge = self.app.bridge
        result = bridge.dry_run_result()
        if result is None:
            return bridge.status_line(), "#222222", "#f0f0f0"
        built, failed = result
        total = built + failed
        if failed:
            return (
                "Dry-run: %d of %d commands could NOT be built (%d were). Nothing was "
                "submitted. The reason is on the 'failed' lines below." % (failed, total, built),
                RED,
                "#f6d8d8",
            )
        return (
            "Dry-run OK: all %d commands were built. No files written, no jobs submitted "
            "- press Submit to actually run them." % total,
            "#1a5c26",
            "#dcecdc",
        )

    def refresh(self, force: bool = False) -> None:
        """重画。内容没变就**什么都不做** —— 否则用户刚选中的那一段每一拍都被清掉。"""
        if not self.alive():
            return
        doc = self.document()
        message, foreground, background = self.verdict_text()
        self.verdict.config(text=message, fg=foreground, bg=background)
        self.count_lbl.config(text="%d events" % len(self.app.bridge.events()))
        if doc == self._doc and not force:
            return
        self._doc = doc
        first, _last = self.text.yview()
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", doc)
        self.text.yview_moveto(1.0 if self.follow.get() else first)

    # --------------------------------------------------------------- 动作
    def _copy(self, masked: bool) -> None:
        """整份日志进剪贴板。`masked` = 先过一遍脱敏表（硬约束 1）。"""
        doc = self._doc or self.document()
        note = "copied %d lines" % doc.count(_NL)
        if masked:
            table = self.app.bridge.redaction_map()
            doc = gui_state.redact(doc, table)
            note = "copied %d lines, %d site names masked" % (doc.count(_NL), len(table))
        try:
            self.top.clipboard_clear()
            self.top.clipboard_append(doc)
            # 有的窗口管理器要等一次 update 才真把剪贴板交出去 —— 少了这句，窗口一关
            # 内容就没了（X11 上剪贴板归**进程**所有，不归系统）。
            self.top.update()
        except tk.TclError as exc:  # pragma: no cover - 没有剪贴板的环境
            note = "could not copy: %s" % exc
        self.count_lbl.config(text=note)

    def _save(self) -> None:
        """存盘。存的是**原文**（没脱敏）：文件留在这台机器上，脱敏是"发出去"才要的。"""
        if smoke_enabled():
            return
        name = (getattr(self.app.bridge, "batch_name", "") or "ewave-batch").strip() or "log"
        path = filedialog.asksaveasfilename(
            parent=self.top,
            title="Save log",
            defaultextension=".log",
            initialfile="%s.log" % name,
            filetypes=[("Log file", "*.log"), ("Text file", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline=_NL) as handle:
                handle.write(self.document())
        except OSError as exc:
            _error("Cannot save the log", str(exc))
            return
        self.count_lbl.config(text="saved to %s" % os.path.basename(path))


class _TraceWindow:
    """**Developer log** —— 用户点了什么 → 界面做了什么 → 报了什么错，一条一行。

    ★ 与 `_LogWindow` 的分工（两扇窗，不是一扇窗两个 tab）：

    | | 讲什么 | 谁看 |
    |---|---|---|
    | Log | driver 事件：提交 / 完成 / 失败 / 拼出来的命令 | 用户，回答"能不能跑" |
    | Developer log | 界面事件：点击 / 弹框 / 被吞掉的异常 / 界面-模型快照 | 写代码的人，回答"刚才为什么怪" |

    分成两扇的理由是**受众不同**：Log 那扇是给"我要跑一批仿真"的人看的，混进
    `on_group_select swallowed` 只会让它更难读；而 Developer log 里一行 200 字符、
    每敲一个键就多一条，塞进 Log 会把那 6 条真正重要的事件冲走。

    用户 2026-08-20 明说这一份「不要管什么违规问题」⇒ `Copy all` 拷**原文**。
    `Copy for sharing` 照旧留着（同 `_LogWindow`），多一个按钮不增加任何摩擦。
    """

    def __init__(self, app: "BaseApp") -> None:
        self.app = app
        self._doc = ""
        self.top = tk.Toplevel(app.top)
        self.top.title("eWave Batch - Developer log")
        try:
            self.top.geometry(LOG_GEOMETRY)
        except tk.TclError:  # pragma: no cover - 嵌进别人的窗口时
            pass
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.follow = tk.BooleanVar(value=True)

        bar = ttk.Frame(self.top, padding=(8, 6))
        bar.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(bar, text="Copy all", width=10, command=lambda: self._copy(False)).pack(
            side=tk.LEFT
        )
        ttk.Button(
            bar, text="Copy for sharing", width=17, command=lambda: self._copy(True)
        ).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="Save as...", width=11, command=self._save).pack(side=tk.LEFT)
        ttk.Button(bar, text="Clear", width=8, command=self._clear).pack(side=tk.LEFT, padx=4)
        ttk.Checkbutton(bar, text="Follow", variable=self.follow).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Button(bar, text="Close", width=8, command=self.close).pack(side=tk.RIGHT)
        self.count_lbl = ttk.Label(bar, text="", style="Hint.TLabel")
        self.count_lbl.pack(side=tk.RIGHT, padx=8)

        self.hint = ttk.Label(self.top, text=TRACE_HINT, style="Hint.TLabel", wraplength=1000)
        self.hint.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(2, 6))
        self.top.bind("<Configure>", self._refit_wraps, add="+")

        wrap = ttk.Frame(self.top, padding=(8, 0))
        wrap.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.text = tk.Text(
            wrap,
            height=LOG_ROWS,
            wrap="none",
            font=app.f_mono,
            relief=tk.SOLID,
            bd=1,
            background="#fbfbfb",
            foreground="#222222",
        )
        yscroll = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self.text.yview)
        xscroll = ttk.Scrollbar(wrap, orient=tk.HORIZONTAL, command=self.text.xview)
        self.text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        _make_readonly(self.text)
        # 出事的那几行要**一眼看得见**：轨迹一屏 40 行，全黑的话 ERR / CRASH
        # 跟"一切正常"长得一模一样。
        self.text.tag_configure("err", foreground=RED)
        self.text.tag_configure("note", foreground=BLUE)
        self.text.tag_configure("state", foreground="#5a5a5a")

        # ★ 实时刷新：轨迹自己记一条就叫一声，不靠轮询。
        #   （轮询要么慢半拍，要么在没跑批次的时候根本没有拍 —— 而这些 bug
        #   恰好全发生在"还没开跑"的配置阶段。）
        self.app.trace.on_record = self._on_record
        if smoke_enabled():
            self.top.withdraw()

    # ------------------------------------------------------------- 生命周期
    def _on_record(self) -> None:
        """轨迹多了一条。窗口已经关掉时**摘掉自己**，别让回调吊着一扇死窗。"""
        if not self.alive():
            if self.app.trace.on_record is self._on_record:
                self.app.trace.on_record = None
            return
        self.refresh()

    def alive(self) -> bool:
        try:
            return bool(self.top.winfo_exists())
        except tk.TclError:  # pragma: no cover - 解释器收尾时
            return False

    def close(self) -> None:
        if self.app.trace.on_record is self._on_record:
            self.app.trace.on_record = None
        try:
            self.top.destroy()
        except tk.TclError:  # pragma: no cover
            pass

    def present(self) -> None:
        if not self.alive() or smoke_enabled():
            return
        try:
            self.top.deiconify()
            self.top.lift()
        except tk.TclError:  # pragma: no cover
            pass

    def _refit_wraps(self, _event: object = None) -> None:
        try:
            width = max(200, self.top.winfo_width() - 24)
        except tk.TclError:  # pragma: no cover - 窗口已经关掉
            return
        self.hint.config(wraplength=width)

    # --------------------------------------------------------------- 内容
    def document(self) -> str:
        """整份轨迹。抬头带环境 —— 报 bug 时"哪台机器上的哪个批次"总要问一遍。"""
        bridge = self.app.bridge
        header = [
            "# eWave Batch - developer log (what the user clicked -> what came back)",
            "# batch      %s" % (getattr(bridge, "batch_name", "") or _DASH),
            "# layout     %s" % self.app.__class__.__name__,
            "# python     %s on %s" % (sys.version.split()[0], sys.platform),
            "# tk         %s" % self._tk_version(),
            "# state      %s" % bridge.status_line(),
            LOG_RULE,
        ]
        return self.app.trace.document(header)

    def _tk_version(self) -> str:
        try:
            return str(self.top.tk.call("info", "patchlevel"))
        except tk.TclError:  # pragma: no cover
            return "?"

    def refresh(self, force: bool = False) -> None:
        if not self.alive():
            return
        doc = self.document()
        self.count_lbl.config(text="%d entries" % len(self.app.trace))
        if doc == self._doc and not force:
            return
        self._doc = doc
        first, _last = self.text.yview()
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", doc)
        self._colourise()
        self.text.yview_moveto(1.0 if self.follow.get() else first)

    def _colourise(self) -> None:
        """按 kind 那一列上色。**只碰屏幕上这一份**，不改轨迹本身。"""
        for tag, needle in (("note", "note "), ("state", "state"),
                            ("err", "ERR  "), ("err", "CRASH")):
            index = "1.0"
            while True:
                hit = self.text.search("  %s" % needle, index, tk.END, nocase=False)
                if not hit:
                    break
                line = hit.split(".")[0]
                self.text.tag_add(tag, "%s.0" % line, "%s.end" % line)
                index = "%s.end" % line

    # --------------------------------------------------------------- 动作
    def _copy(self, masked: bool) -> None:
        doc = self._doc or self.document()
        note = "copied %d lines" % doc.count(_NL)
        if masked:
            table = self.app.bridge.redaction_map()
            doc = gui_state.redact(doc, table)
            note = "copied %d lines, %d site names masked" % (doc.count(_NL), len(table))
        try:
            self.top.clipboard_clear()
            self.top.clipboard_append(doc)
            self.top.update()
        except tk.TclError as exc:  # pragma: no cover - 没有剪贴板的环境
            note = "could not copy: %s" % exc
        self.count_lbl.config(text=note)

    def _clear(self) -> None:
        """从这里重新开始记 —— 「我现在按一遍给你看」之前按它，噪声就没了。"""
        self.app.trace.clear()
        self.app.trace.note("trace cleared by the user")
        self.refresh(force=True)

    def _save(self) -> None:
        if smoke_enabled():
            return
        name = (getattr(self.app.bridge, "batch_name", "") or "ewave-batch").strip() or "trace"
        path = filedialog.asksaveasfilename(
            parent=self.top,
            title="Save developer log",
            defaultextension=".log",
            initialfile="%s-dev.log" % name,
            filetypes=[("Log file", "*.log"), ("Text file", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline=_NL) as handle:
                handle.write(self.document())
        except OSError as exc:
            _error("Cannot save the developer log", str(exc))
            return
        self.count_lbl.config(text="saved to %s" % os.path.basename(path))


class BaseApp:
    """三版布局共用的状态 + section builder。子类只实现 `layout()`。

    ⚠️ **构造时不建 `Tk()`**（`build_frame(parent, bridge)` 拿到的是别人的容器）。
    所有控件都建在 `self.frame` 里，`self.frame` 就是 `build_frame` 的返回值。
    """

    GEOMETRY = "1180x900"
    RUN_ROWS = 12

    def __init__(self, parent: object, bridge: object) -> None:
        self.parent = parent
        self.top = parent.winfo_toplevel()  # type: ignore[attr-defined]
        self.bridge = bridge
        self.frame = ttk.Frame(parent)  # type: ignore[arg-type]

        self._timer: str | None = None
        self._error = ""
        self._syncing = False
        """`recompute()` 重入保护：往 GUI 变量里写值会触发它们自己的回调。"""
        self.adv_open = False
        self.runs_titled = True
        self.runs_header: ttk.Label | None = None
        self.formula_lbl: ttk.Label | None = None
        self.bar_formula: ttk.Label | None = None
        self.batchdir_lbl: ttk.Label | None = None
        self.dir_lbl: ttk.Label | None = None
        self.right_lbl: ttk.Label | None = None
        self.detail_box: ttk.LabelFrame | None = None
        self.settings_title = " Settings "
        self.gtree: ttk.Treeview | None = None
        self.groups_box: ttk.Widget | None = None
        self.groups_hint: ttk.Label | None = None
        self.groups_warn: tk.Label | None = None
        self.runs_stale: tk.Label | None = None
        self.broot_warn: tk.Label | None = None
        self.broot_lbl: object | None = None
        self._runs_stale_anchor: object | None = None
        self.group_hint: tk.Label | None = None
        self.settings_grid: ttk.Frame | None = None
        self.sw_combo: ttk.Combobox | None = None
        self.log_btn: ttk.Button | None = None
        self._log: _LogWindow | None = None
        """Log 窗口。**最多一扇** —— 用户按第二次 Log 是"我要看日志"，
        不是"我要两份日志"，而两扇窗只有一扇会被 `_pump()` 刷新。"""
        self._trace_win: "_TraceWindow | None" = None
        """Developer log 窗口（`gui/trace.py`）。同样最多一扇。"""
        self.trace = ActionTrace()
        """动作轨迹。`_install_trace()` 会**换掉**这一个 —— 这里先摆一个空的，
        好让"控件还没建完就有人记一条"不至于 `AttributeError`。"""
        self._runs: tuple[Run, ...] = ()

        # bridge 喂不喂得饱 run group 面板 —— **当判据用，不当假设用**。
        # `gui/frames/*.py` 的 `SHARED_LAYER_BRIDGE_EXTRAS` 是那边写死的一份名单，
        # 里面没有这些方法（它不许悄悄扩张），所以一个"满足冻结面 + 那 6 样"的
        # bridge 照样可能一个组方法都没有。缺了就退成"只有 base"，界面照样建得起来。
        self.groups_ok = all(
            callable(getattr(bridge, name, None))
            for name in ("groups", "active_group", "set_active_group", "group_override")
        )

        try:
            self.top.geometry(self.GEOMETRY)  # type: ignore[attr-defined]
        except tk.TclError:  # pragma: no cover - parent 不是窗口时无所谓
            pass

        self._init_vars()
        self._init_style()
        # ★ 轨迹必须在 `build_*` **之前**装好：按钮是 `command=self.do_x` 绑的，
        #   绑的是**那一刻**的方法对象，装晚了按钮上挂的就是没被包过的原件。
        self._install_trace()
        self.build_menubar()
        self.layout()
        self.recompute()

    # --------------------------------------------------------------- 轨迹
    # 用户 2026-08-20：「做一个开发者用的 log 页面，记录用户点了什么、返回了什么报错」。
    # 模型与取舍写在 `gui/trace.py` 的模块 docstring 上，这里只做接线。

    TRACED = (
        # 组这一块是这一轮 bug 的现场，记得最细。
        "do_add_group", "do_duplicate_group", "do_remove_group",
        "on_group_rename", "on_override_toggle",
        # ⚠️ `switch_group` 也不在名单里，理由同下：它是 `on_group_select` 的下一跳，
        #    每一拍重画都会走一遍并在开头早返回。真正切了组的那一次自己记一条 `note`。
        # ⚠️ `on_group_select` **故意不在名单里**：`refresh_groups()` 每次都
        #    `selection_set(active)`，而 Tk 的 `<<TreeviewSelect>>` 是排队送达的 ⇒
        #    每一拍重画都会叫它一次。它自己那两条 `note`（被吞掉的点击 / 空选中）
        #    才是有信息量的部分，click/ok 那两条只是噪声。
        # design 表
        "add_design", "del_design", "dup_design", "on_design_edit",
        # 批次 / 运行
        "do_submit", "do_dry_run", "do_cancel", "do_resume", "do_new_batch",
        "do_rename_batch", "do_duplicate_batch", "do_open_spec", "do_save_spec",
        "do_pick_batch_root", "do_pick_offdir", "do_open_batch_dir", "do_exit",
        "on_row_action", "show_log", "show_trace", "show_defaults", "show_about",
        "on_max_parallel",
    )
    """哪些方法进轨迹。**手写一张名单**而不是 `dir()` 扫 `do_*`：

    扫出来的名单会随着新方法悄悄变大，某天有人加一个每秒跑一次的 `do_poll`，
    轨迹就废了 —— 而废掉的那一刻没有任何信号。名单在这里，加方法的人自己决定要不要记。

    `recompute` / `push` **故意不在名单里**：它们每敲一个键跑一次，包进 click/ok
    会把真正的动作淹掉。它们出事时留下的痕迹是 `_guard` 那条 `ERR` 和快照那一行。
    """

    def _install_trace(self) -> None:
        """把名单里的方法逐个换成"进去记一条、出来记一条"的包装。

        `setattr(self, ...)` 写的是**实例**字典，盖住类上的同名方法 —— 于是三版
        frame、子类覆写、`command=self.do_x` 三条路拿到的都是同一个包装。
        """
        global _DIALOG_TRACE
        self.trace = ActionTrace()
        _DIALOG_TRACE = self.trace
        self.trace.note(
            "session start",
            "python %s on %s, layout=%s" % (sys.version.split()[0], sys.platform,
                                            self.__class__.__name__),
        )
        for name in self.TRACED:
            original = getattr(self, name, None)
            if callable(original):
                setattr(self, name, _trace_wrap(self.trace, name, original))
        self._install_tk_excepthook()

    def _install_tk_excepthook(self) -> None:
        """Tk 回调里抛出来的异常 -> 轨迹。

        🚨 这是本轮"太诡异了"的**主要来源**：Tk 的默认处理是把 traceback 打到
        stderr 然后**若无其事地继续**。红区那边 GUI 是双击起来的，没有人在看 stderr ——
        于是一个真异常在用户眼里就是"点了没反应"，而下一次点击又好了（状态已经被
        改了一半）。接住它才有得查。接住之后**照旧弹默认框**，不改变行为。
        """
        top = self.top
        previous = getattr(top, "report_callback_exception", None)

        def hook(exc_type: object, exc: BaseException, tb: object) -> None:
            self.trace.error("unhandled exception in a Tk callback", exc, tb=True)
            if callable(previous):
                previous(exc_type, exc, tb)

        try:
            top.report_callback_exception = hook  # type: ignore[attr-defined]
        except (AttributeError, tk.TclError):  # pragma: no cover - 嵌进别人的窗口时
            pass

    def _trace_state(self) -> None:
        """一行快照：**界面**和**模型**在这一拍是不是同一件事。

        四样东西必须在同一行上，因为这一轮三个 bug 的判据全是"它们互相对不上"：

        | 字段 | 对不上时是什么症状 |
        |---|---|
        | `active` vs `sel` | 点了 A 组、改的却是 B 组（组表选中被我们自己的重画抢回去了） |
        | `groups` | 复制出来的组和源组的覆盖是不是真的两份 |
        | `ovr` | 五个覆盖勾选框 —— 全 0 就是"整块灰"的直接原因 |
        | `rows` | 五行控件的真实 `state`，**从控件上读**而不是从我们的意图上读 |

        读控件用 `cget("state")`：意图和事实分家的时候，只有事实有用。
        """
        trace = getattr(self, "trace", None)
        if trace is None:
            return
        try:
            groups = []
            for group in self._groups():
                overrides = getattr(group, "axis_overrides", {}) or {}
                groups.append(
                    "%s{%s}" % (group.name, ",".join(sorted(overrides)) if overrides else "-")
                )
            selection = self.gtree.selection() if self.gtree is not None else ()
            axes = []
            for key in sorted(self.ovr_vars):
                box = self.srow_boxes.get(key)
                kids = box.winfo_children() if box is not None else []
                live = str(kids[0].cget("state")) if kids else "?"
                axes.append(
                    "%s=%s%s"
                    % (
                        key,
                        "1" if self.ovr_vars[key].get() else "0",
                        "N" if live in ("normal", "readonly") else "D",
                    )
                )
            snapshot = "active=%s sel=%s groups=[%s] axes[%s]%s" % (
                self._active_group(),
                ",".join(selection) or "-",
                " ".join(groups),
                # `1N` = 勾着且能编辑（正常）；`0D` = 没勾且置灰（正常，"继承 base"）；
                # `1D` / `0N` = **界面和模型分家**，这一行就是 bug 的现场。
                " ".join(axes),
                (" err=%s" % _trace_clip(self._error, 200)) if self._error else "",
            )
        except Exception as exc:  # noqa: BLE001 - 快照失败绝不许影响界面
            trace.record("state", "(snapshot failed: %s)" % exc.__class__.__name__)
            return
        trace.state(snapshot)

    # ------------------------------------------------------------ variables
    def _init_vars(self) -> None:
        """GUI 变量的初值**从 bridge 读**，不在这里再写一份默认值（两份必然漂）。"""
        selection = self.bridge.axis_selection()
        sweep = self.bridge.sweep()
        corners = selection.get("corner", ())
        modes = selection.get("fullWave", ())
        mesh = (selection.get("mesh") or ("0.4",))[0].split(gui_state.MESH_SEP)
        if len(mesh) == 1:
            mesh = mesh * 3

        self.corner_vars = {
            name: tk.BooleanVar(value=(name in corners)) for name in gui_state.CORNER_VALUES
        }
        self.mode_vars = {
            "Quasi-static": tk.BooleanVar(value=("off" in modes)),
            "Full wave": tk.BooleanVar(value=("on" in modes)),
        }
        self.temp = tk.StringVar(value=", ".join(selection.get("temperature", ())))
        self.sw_mode = tk.StringVar(value=sweep.get("mode", "adaptive"))
        self.sw_spacing = tk.StringVar(value=sweep.get("spacing", "step"))
        """step / points 二选一（`gui_state.SWEEP_SPACINGS`）。没选中的那格置灰且清空。"""
        self.f_start = tk.StringVar(value=sweep.get("start", ""))
        self.f_stop = tk.StringVar(value=sweep.get("stop", ""))
        self.f_step = tk.StringVar(value=sweep.get("step", ""))
        self.f_pts = tk.StringVar(value=sweep.get("points", ""))
        self.m_edge = tk.StringVar(value=mesh[0])
        self.m_vert = tk.StringVar(value=mesh[1])
        self.m_via = tk.StringVar(value=mesh[2])
        self.eq_on = tk.BooleanVar(value=("on" in selection.get("equalCurrent", ())))
        self.eq_off = tk.BooleanVar(value=("off" in selection.get("equalCurrent", ())))
        self.tol_r = tk.StringVar(value=(selection.get("relativeTolerance") or ("",))[0])
        self.tol_c = tk.StringVar(value=(selection.get("relativeCurrentTolerance") or ("",))[0])
        # 每一行 Settings 的「这个组覆盖这根轴」勾选框。键 = `GROUP_ROW_AXES` 的行名。
        # 建在 `_srow` 里（那时才知道有哪几行），这里只把容器备好 —— `push()` 在
        # `build_settings` 之前就可能跑不到，但 `_push_axis` 拿不到勾选框时按"覆盖"
        # 处理，也就是加组之前的行为。
        self.ovr_vars: dict[str, tk.BooleanVar] = {}
        self.ovr_boxes: dict[str, ttk.Checkbutton] = {}
        self.srow_boxes: dict[str, ttk.Frame] = {}
        self.maxpar = tk.StringVar(value=str(self.bridge.options().max_parallel))
        """同时在飞的 job 数上限。**跑起来之后照样能改**（见 `on_max_parallel`）。"""
        self.dsub = tk.StringVar(value=self.bridge.submit_command)
        self.extra = tk.StringVar(value=self.bridge.extra_flags_text())
        self.batch = tk.StringVar(value=self.bridge.batch_name)
        self.broot = tk.StringVar(value=getattr(self.bridge, "batch_root", ""))
        self.offdir = tk.StringVar(value=self.bridge.official_run_dir)

    def _init_style(self) -> None:
        """具名字体 + ttk style。字体从 Tk 自己的 `TkDefaultFont` / `TkFixedFont` 拷贝 ——
        写死 `Segoe UI` 在红区的 Linux 上会被静默替换掉。"""
        self.f_ui = tkfont.nametofont("TkDefaultFont").copy()
        self.f_ui_b = tkfont.nametofont("TkDefaultFont").copy()
        self.f_ui_b.configure(weight="bold")
        self.f_mono = tkfont.nametofont("TkFixedFont").copy()
        self.f_mono_b = tkfont.nametofont("TkFixedFont").copy()
        self.f_mono_b.configure(weight="bold")

        # 行高按**这台机器上的字体度量**现算，写死的 21 / 20 只当下限。
        # 与列宽同一条道理（见 `_fit_tree_columns`）：红区是 Linux，同一号字的
        # linespace 比开发机大，行高一旦比字还矮，表格里的文字就被上下切掉 ——
        # 那是最难自查的一类裁剪，因为字还在、只是缺了一截。
        # 取 mono / ui 两者的较大值：单元格用 f_mono，但行里也可能落到 UI 字体上。
        line_px = max(self.f_mono.metrics("linespace"), self.f_ui.metrics("linespace"))
        self.runs_rowheight = max(21, line_px + 4)
        """Runs 表的行高（px）。`show_detail` 拿它算「表里还剩得下几行」。"""

        st = ttk.Style()
        st.configure("Runs.Treeview", font=self.f_mono, rowheight=self.runs_rowheight)
        st.configure("Runs.Treeview.Heading", font=self.f_ui_b)
        st.configure("Designs.Treeview", font=self.f_mono, rowheight=max(20, line_px + 3))
        st.configure("Designs.Treeview.Heading", font=self.f_ui_b)
        st.configure("Count.TLabel", font=self.f_mono, foreground=BLUE)
        st.configure("Off.TLabel", font=self.f_mono, foreground=GREY)
        st.configure("Hint.TLabel", font=self.f_ui, foreground=HINT)
        st.configure("Green.TLabel", font=self.f_mono, foreground=GREEN)
        st.configure("Warn.TLabel", font=self.f_ui, foreground=RED)
        st.configure("Mono.TLabel", font=self.f_mono)
        st.configure("Accent.TButton", font=self.f_ui_b)

    def _cap_px(self, chars: dict) -> dict:
        """「最多几个字符」→「最多几像素」，用**这台机器上**的等宽字体量。

        列宽上限存在的理由是"内容长度没有上界"，而不是"340 这个数字有什么道理"。
        写成像素就等于假定一个字符有多宽 —— 红区是 Linux，同一号字更宽，于是上限
        在那边偷偷变成了"少显示几个字"（实测：放大 30% 之后 design 列内容要 349px、
        上限还是 340px，最长的 design key 末尾被切掉一个字符，而 Treeview 不画省略号，
        看起来就像那个 key 本来就长这样）。换算成像素这件事只能在运行时做。
        """
        unit = max(1, self.f_mono.measure("0"))
        return {key: unit * int(count) + CELL_PAD for key, count in chars.items()}

    # -------------------------------------------------------------- menubar
    def build_menubar(self) -> None:
        """菜单挂在**顶层窗口**上。parent 不是窗口（被嵌进别的界面）时静默跳过。"""
        bar = tk.Menu(self.top)
        actions: dict[str, object] = {
            "New batch": self.do_new_batch,
            "Open spec...": self.do_open_spec,
            "Save spec as...": self.do_save_spec,
            "Exit": self.do_exit,
            "Duplicate batch...": self.do_duplicate_batch,
            "Rename...": self.do_rename_batch,
            "Open batch dir": self.do_open_batch_dir,
            "Dry-run": self.do_dry_run,
            "Submit": self.do_submit,
            "Cancel": self.do_cancel,
            "Resume": self.do_resume,
            # 「只重跑没成的」**就是** resume 的语义（D7：判据来自磁盘上的产物，
            # done 的一个都不重跑）。给它另起一条路等于第二份调度逻辑。
            "Re-run failed only": self.do_resume,
            "Extraction defaults...": self.show_defaults,
            "Show log...": self.show_log,
            "Developer log...": self.show_trace,
            "About": self.show_about,
        }
        for name, items in (
            ("File", ("New batch", "Open spec...", "Save spec as...", "-", "Exit")),
            ("Batch", ("Duplicate batch...", "Rename...", "-", "Open batch dir")),
            ("Runs", ("Dry-run", "Submit", "Cancel", "Resume", "-", "Re-run failed only")),
            (
                "Tools",
                (
                    "Show log...",
                    "Developer log...",
                    "Extraction defaults...",
                    "Check environment (doctor)",
                ),
            ),
            ("Help", ("About",)),
        ):
            menu = tk.Menu(bar, tearoff=0)
            for item in items:
                if item == "-":
                    menu.add_separator()
                    continue
                _add_menu_item(menu, item, actions.get(item))
            bar.add_cascade(label=name, menu=menu)
        try:
            self.top.config(menu=bar)  # type: ignore[attr-defined]
        except tk.TclError:  # pragma: no cover - 嵌进非窗口容器时
            pass

    # ------------------------------------------------------------ batch bar
    def build_batchbar(self, parent: object, show_dir: bool = False) -> ttk.Frame:
        """顶上那条。**两行**：第一行批次名 + 官方 run 目录，第二行落点。

        第二行是 2026-08-20 那次数据丢失之后加的。在那之前"我的结果落在哪"这个问题
        在界面上**无解**：`batch_root` 是 `./ewave_batches`（相对启动 GUI 时的 cwd），
        既不显示也不能改，唯一的线索是动作栏里那行拼好的 Batch dir。于是从安装目录
        起界面 = 结果落在安装目录里 = 下一次部署把它搬进 `.deploy/backups/` 再轮转删掉，
        而用户全程看不到任何提示。落点必须是**看得见、改得动、指错了会红**的东西。
        """
        outer = ttk.Frame(parent)  # type: ignore[arg-type]
        f = ttk.Frame(outer, padding=(8, 6, 8, 2))
        f.pack(fill=tk.X)
        ttk.Label(f, text="Batch name:").pack(side=tk.LEFT)
        entry = ttk.Entry(f, textvariable=self.batch, width=26, font=self.f_mono)
        entry.pack(side=tk.LEFT, padx=(6, 4))
        entry.bind("<KeyRelease>", lambda _e: self.recompute())
        # 「New batch」必须是个显眼的按钮，不能只藏在 File 菜单里：跑过之后界面**不再**
        # 跟着勾选重新展开矩阵（见 `recompute`），这个按钮就是"我要重来一批"的唯一出口。
        ttk.Button(f, text="New", width=6, command=self.do_new_batch).pack(side=tk.LEFT)
        ttk.Button(f, text="Open spec...", width=13, command=self.do_open_spec).pack(
            side=tk.LEFT, padx=(2, 8)
        )

        ttk.Label(f, text="Official run dir:").pack(side=tk.LEFT)
        off = ttk.Entry(f, textvariable=self.offdir, width=26, font=self.f_mono)
        off.pack(side=tk.LEFT, padx=(6, 4))
        off.bind("<KeyRelease>", lambda _e: self.recompute())
        ttk.Button(f, text="Browse...", width=9, command=self.do_pick_offdir).pack(side=tk.LEFT)
        # 这句提示和下面那条批次目录都是 `_ElideLabel`：普通标签会按全文要宽度，
        # 而这一整条是一行 pack 的 —— 实测这一行要 1516px、窗口只有 1180，于是
        # 提示语被切成 "site coordinates are parsed f"（2026-08-19 实拍 B1）。
        hint = _ElideLabel(f, font=self.f_ui, chars=12, style="Hint.TLabel")
        hint.pack(side=tk.LEFT, padx=8, fill=tk.X, expand=True)
        hint.set_text("site coordinates are parsed from it, never typed")
        _Tooltip(hint, hint.full_text)

        if show_dir:
            self.dir_lbl = _ElideLabel(f, font=self.f_mono, chars=12, style="Mono.TLabel")
            self.dir_lbl.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(8, 0))
            _Tooltip(self.dir_lbl, self.dir_lbl.full_text)

        second = ttk.Frame(outer, padding=(8, 0, 8, 4))
        second.pack(fill=tk.X)
        ttk.Label(second, text="Batch root:").pack(side=tk.LEFT)
        root_entry = ttk.Entry(second, textvariable=self.broot, width=26, font=self.f_mono)
        root_entry.pack(side=tk.LEFT, padx=(6, 4))
        root_entry.bind("<KeyRelease>", lambda _e: self.recompute())
        ttk.Button(second, text="Browse...", width=9, command=self.do_pick_batch_root).pack(
            side=tk.LEFT
        )
        # 落点算出来的绝对路径就摆在旁边：批次名 + root 拼出来的东西不该要人心算。
        self.broot_lbl = _ElideLabel(second, font=self.f_mono, chars=12, style="Mono.TLabel")
        self.broot_lbl.pack(side=tk.LEFT, padx=8, fill=tk.X, expand=True)
        _Tooltip(self.broot_lbl, self.broot_lbl.full_text)

        # 指进程序目录时的红字（`GuiState.batch_root_warning`）。用 tk.Label 不用 ttk：
        # 前景色走 style 会污染别的标签（同 `extra_warn` / `groups_warn`）。
        self.broot_warn = tk.Label(
            outer, font=self.f_ui, fg=RED, anchor=tk.W, justify=tk.LEFT, wraplength=900
        )
        return outer

    # -------------------------------------------------------------- designs
    def build_designs(
        self,
        parent: object,
        widths: tuple[int, int, int] = (230, 230, 200),
        rows: int = 3,
        buttons: str = "side",
        titled: bool = True,
    ) -> ttk.Widget:
        box: ttk.Widget
        if titled:
            box = ttk.LabelFrame(parent, text=" Designs ", padding=7)  # type: ignore[arg-type]
        else:
            box = ttk.Frame(parent, padding=2)  # type: ignore[arg-type]
        self.designs_box = box
        inner = ttk.Frame(box)
        inner.pack(fill=tk.BOTH, expand=True)

        wrap = ttk.Frame(inner)
        wrap.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.dtree = ttk.Treeview(
            wrap,
            columns=("lib", "cell", "view"),
            show="headings",
            height=rows,
            style="Designs.Treeview",
            selectmode="browse",
        )
        # `widths` 现在只是**下限**：真宽度由 `_fit_tree_columns` 按内容现算
        # （2026-08-19 实拍 B3：Library 给 120 要 138 -> 库名被切掉末尾，
        #  而同一张表里 Cell 被 stretch 撑到 392px 去装 192px 的内容）。
        self.design_floors = dict(zip(("lib", "cell", "view"), widths))
        for (key, head), width in zip(
            (("lib", "Library"), ("cell", "Cell"), ("view", "View")), widths
        ):
            self.dtree.heading(key, text=head)
            self.dtree.column(key, width=width, anchor=tk.W, stretch=(key == "cell"))
        _scrolled_tree_grid(wrap, self.dtree)
        # 双击一行 = 改这一行（A6）。在这之前只有 Add / Remove：打错一个字母就得
        # 删掉重敲三个字段。
        self.dtree.bind("<Double-1>", self.on_design_edit)

        side = ttk.Frame(inner)
        side.pack(side=tk.LEFT, padx=(6, 0), fill=tk.Y)
        ttk.Button(side, text="Add row", width=12, command=self.add_design).pack(pady=1)
        ttk.Button(side, text="Remove row", width=12, command=self.del_design).pack(pady=1)
        if buttons == "three":
            ttk.Button(side, text="Duplicate row", width=12, command=self.dup_design).pack(pady=1)

        foot = ttk.Frame(box)
        foot.pack(fill=tk.X, pady=(4, 0))
        self.design_count = ttk.Label(foot, text="-> 0", style="Count.TLabel")
        self.design_count.pack(side=tk.RIGHT)
        self.refresh_designs()
        return box

    def refresh_designs(self) -> None:
        """重画 designs 表。**一模一样的两行标红。**

        按一次 `Duplicate row` 就会出现这种行 —— 而那正是那个按钮的预期用法
        （复制一行再改 cell 名）。两行相同 ⇒ 每个 run 撞 run_id ⇒ 整个矩阵被拒 ⇒
        右边的表和 Total 那行当场自相矛盾。红色让人一眼看见"要改的是这儿"，
        `preflight()` 里那条专门的消息说清楚"改成什么"。
        """
        self.dtree.delete(*self.dtree.get_children())
        rows = list(self.bridge.design_rows())
        seen: dict[tuple[str, ...], int] = {}
        for row in rows:
            key = tuple(row)
            seen[key] = seen.get(key, 0) + 1
        for row in rows:
            duplicate = seen.get(tuple(row), 0) > 1
            self.dtree.insert("", tk.END, values=row, tags=("dupe",) if duplicate else ())
        self.dtree.tag_configure("dupe", background="#ffe6e6", foreground=RED)
        _fit_tree_columns(
            self.dtree,
            ("lib", "cell", "view"),
            head_font=self.f_ui_b,
            cell_font=self.f_mono,
            floors=getattr(self, "design_floors", {}),
        )

    DESIGN_FIELDS: tuple[str, ...] = ("Library", "Cell", "View")

    def _design_dialog(
        self, title: str, initial: Sequence[str], apply: Callable[[tuple[str, str, str]], None]
    ) -> object:
        """Add / Edit design 共用的那一个对话框。三元组必须齐 —— **view 不是常量**（BRIEF §5）。

        2026-08-19 之前这里有四个坑，全在 `ok()` 那五行里：少填一格 -> `if all(values)`
        不成立 -> 什么也没加，然后**无条件** `destroy()` -> 框关了、没有任何提示，
        用户以为加上了。另外没有 Cancel、没有 Enter/Escape、打开时焦点不在输入框。
        现在：缺哪一格就说哪一格、框不关、焦点跳到那一格。
        """
        dlg = tk.Toplevel(self.top)
        dlg.title(title)
        dlg.transient(self.top)
        dlg.columnconfigure(1, weight=1)
        variables: list[tk.StringVar] = []
        entries: list[ttk.Entry] = []
        for index, label in enumerate(self.DESIGN_FIELDS):
            ttk.Label(dlg, text=label).grid(row=index, column=0, sticky=tk.W, padx=8, pady=4)
            var = tk.StringVar(value=initial[index] if index < len(initial) else "")
            entry = ttk.Entry(dlg, textvariable=var, width=34, font=self.f_mono)
            entry.grid(row=index, column=1, sticky="ew", padx=8, pady=4)
            variables.append(var)
            entries.append(entry)
        ttk.Label(
            dlg,
            text="All three are required; the view is not a constant.",
            style="Hint.TLabel",
        ).grid(row=3, column=0, columnspan=2, sticky=tk.W, padx=8)
        # 用 tk.Label 而不是 ttk：前景色走 style 会污染别的标签（照 `extra_warn` 的做法）。
        problem = tk.Label(
            dlg, font=self.f_ui, fg=RED, anchor=tk.W, justify=tk.LEFT, wraplength=340
        )
        problem.grid(row=4, column=0, columnspan=2, sticky=tk.W, padx=8)

        def ok(_event: object = None) -> None:
            values = tuple(v.get().strip() for v in variables)
            missing = [
                name for name, value in zip(self.DESIGN_FIELDS, values) if not value
            ]
            if missing:
                problem.config(text="Fill in: %s" % ", ".join(missing))
                entries[list(values).index("")].focus_set()
                return
            try:
                apply(values)  # type: ignore[arg-type]
            except EwaveBatchError as exc:
                problem.config(text=str(exc))
                return
            dlg.destroy()
            self.refresh_designs()
            self.recompute()

        def cancel(_event: object = None) -> None:
            dlg.destroy()

        bar = ttk.Frame(dlg)
        bar.grid(row=5, column=0, columnspan=2, sticky="ew", padx=8, pady=8)
        ttk.Button(bar, text="Cancel", width=9, command=cancel).pack(side=tk.RIGHT)
        ttk.Button(bar, text="OK", width=9, command=ok).pack(side=tk.RIGHT, padx=(0, 6))
        dlg.bind("<Return>", ok)
        dlg.bind("<Escape>", cancel)
        entries[0].focus_set()
        entries[0].selection_range(0, tk.END)
        _center_on_parent(dlg, self.top)
        if not smoke_enabled():
            dlg.grab_set()
        return dlg

    def _selected_design_row(self) -> tuple[str, str, str] | None:
        """designs 表里选中的那一行的三元组；没选中返回 None。"""
        rows = self.bridge.design_rows()
        for iid in self.dtree.selection():
            index = self.dtree.index(iid)
            if 0 <= index < len(rows):
                return tuple(rows[index])  # type: ignore[return-value]
        return None

    def add_design(self) -> None:
        """加一行 design。**有选中行时预填它** —— "复制再改"于是变成一个动作。"""
        initial = self._selected_design_row() or ("", "", "")
        self._design_dialog(
            "Add design", initial, lambda values: self.bridge.add_design(*values)
        )

    def on_design_edit(self, event: object) -> None:
        """双击 designs 的一行 = 改这一行（**替换**，不是追加）。

        走 `bridge.set_designs()`：它一次收下整张表，所以"改第 2 行"就是把第 2 行
        换掉再整张交回去。remove + add 那条路会把行**挪到表尾**，而顺序在这里是有
        意义的（design 的先后决定 run 的先后）。bridge 没有 `set_designs` 时不接
        双击（老 bridge 兼容），Add/Remove 照旧能用。
        """
        setter = getattr(self.bridge, "set_designs", None)
        if not callable(setter):
            return
        iid = self.dtree.identify_row(event.y)  # type: ignore[attr-defined]
        if not iid:
            return
        rows = [list(row) for row in self.bridge.design_rows()]
        index = self.dtree.index(iid)
        if not (0 <= index < len(rows)):
            return

        def apply(values: tuple[str, str, str]) -> None:
            rows[index] = list(values)
            setter(rows)

        self._design_dialog("Edit design", tuple(rows[index]), apply)

    def del_design(self) -> None:
        for iid in self.dtree.selection():
            self.bridge.remove_design(self.dtree.index(iid))
        self.refresh_designs()
        self.recompute()

    def dup_design(self) -> None:
        rows = self.bridge.design_rows()
        for iid in self.dtree.selection():
            index = self.dtree.index(iid)
            if 0 <= index < len(rows):
                self.bridge.add_design(*rows[index])
        self.refresh_designs()
        self.recompute()

    # ---------------------------------------------------------- run groups
    # 模型见 `docs/INTERFACES.md`「run group」一节：批次 = 一列组，每组在 base 之上
    # 覆盖几根轴、各自取笛卡尔积、结果取并集。这个面板是那个模型在界面上的**全部**
    # 出口；轴的取值仍然在 Settings 里改，只是改的是"当前选中的那个组"。

    def build_groups(
        self, parent: object, compact: bool = False, rows: int = 3, titled: bool = True
    ) -> ttk.Widget:
        """第 9 个 section —— 一列 run group，选中一行就等于"接下来改的是这个组"。

        为什么必须有这块地方：笛卡尔积表达不了「一条基线 + 几个单点变体」
        （typical @ 3 个温度 + typical @ 55 关 equalCurrent + typical @ 55 全波 = 5 个
        run，写成笛卡尔积是 12 个、7 个是废的，而一个 run 可能 10 核 100GB 跑 35 分钟）。

        只有 base 一个组时**照样显示**（那是常态）：一张只有一行的表看起来像"还没配"，
        一块空白看起来像"坏了"，前者好得多。

        ⚠️ `compact` 现在**什么都不管**：它曾经把 Settings 那一列缩窄 30px，而那一列的
        内容长度没有上界，缩 30px 治不了它（真正管用的是 `GROUP_SUMMARY_CAP` + 横向
        滚动条）。参数留着的理由同 `build_resources`：布局传了共用层不认识的 hint 会被
        测试记成 `dropped_hints`。
        """
        box: ttk.Widget
        if titled:
            box = ttk.LabelFrame(parent, text=" Run groups ", padding=7)  # type: ignore[arg-type]
        else:
            box = ttk.Frame(parent, padding=2)  # type: ignore[arg-type]
        self.groups_box = box

        head = ttk.Frame(box)
        head.pack(fill=tk.X, pady=(0, 4))
        # 按钮从右往左 pack，所以这里的顺序是反的（屏幕上是 + Add / Duplicate / Remove）。
        for text, width, command in (
            ("Remove", 9, self.do_remove_group),
            ("Duplicate", 11, self.do_duplicate_group),
            ("+ Add", 8, self.do_add_group),
        ):
            ttk.Button(head, text=text, width=width, command=command).pack(side=tk.RIGHT, padx=1)
        self.groups_hint = ttk.Label(head, text="", style="Hint.TLabel")
        self.groups_hint.pack(side=tk.LEFT)

        wrap = ttk.Frame(box)
        wrap.pack(fill=tk.BOTH, expand=True)
        self.gtree = ttk.Treeview(
            wrap,
            columns=tuple(col[0] for col in GROUP_COLS),
            show="headings",
            height=rows,
            style="Designs.Treeview",
            selectmode="browse",
        )
        for key, head_text, width in GROUP_COLS:
            self.gtree.heading(key, text=head_text)
            self.gtree.column(
                key,
                width=width,
                anchor=(tk.E if key == "runs" else tk.W),
                stretch=(key == "summary"),
            )
        # `compact` 不再缩窄 summary 列：那一列的内容长度没有上界，缩 30px 治不了它，
        # 真正管用的是 `GROUP_SUMMARY_CAP` + 横向滚动条（够得着，而不是全塞进来）。
        _scrolled_tree_grid(wrap, self.gtree)
        # 选中一行 = 切到那个组（`bridge.set_active_group`），Settings 整块跟着换。
        self.gtree.bind("<<TreeviewSelect>>", lambda _e: self.on_group_select())
        self.gtree.bind("<Double-1>", self.on_group_rename)

        # 「加组会改掉基线的目录名」那句话的家。用 tk.Label 而不是 ttk：前景色走 style
        # 会污染别的标签，而这是一条一次性的告警（照 `extra_warn` 的做法）。
        self.groups_warn = tk.Label(
            box, font=self.f_ui, fg=RED, anchor=tk.W, justify=tk.LEFT, wraplength=420
        )
        self.refresh_groups()
        return box

    def refresh_groups(self) -> None:
        """重画组表。**当前组前面打一个 `*`** —— 选中高亮在表失焦时看不出来。

        🚨 **这张表画的是「有哪几个组」，不是「各有几个 run」** —— 所以算不出 run 数
        绝不许让它一行都不画。原来 `group_run_counts()` 在第一行就抛了出去，
        整个方法当场退出（`recompute()` 的 `_guard` 把异常吞成状态栏一行字），
        表就冻在上一次的内容上：刚删掉的组还在，点它 -> "There is no run group
        called ..."，于是"删不掉 + 反复弹框"。算不出来就写 `-> ?`，别写 0（0 是个
        具体的答案，而这里根本没有答案），更别不画。
        """
        if self.gtree is None:
            return
        active = self._active_group()
        try:
            counts = dict(self.bridge.group_run_counts()) if self.groups_ok else {}
            merged = self.bridge.merged_run_count() if self.groups_ok else 0
            countable = True
        except EwaveBatchError:
            counts, merged, countable = {}, 0, False
            self.trace.note("refresh_groups: run counts unavailable", "showing -> ?")
        before = self.gtree.selection()
        self.gtree.delete(*self.gtree.get_children())
        for group in self._groups():
            name = group.name
            self.gtree.insert(
                "",
                tk.END,
                iid=name,
                values=(
                    ("* " if name == active else "  ") + name,
                    self._group_summary(name),
                    ("-> %d" % counts.get(name, 0)) if countable else "-> ?",
                ),
            )
        if self.gtree.exists(active) and before and before[0] != active:
            # 选中被搬到 active 那一行。**加组 / 复制 / 删除之后这是正常的**，
            # 但它同时也是"我点了 A，它自己跳回 B"的唯一现场 —— 判据是上一条
            # `on_group_select swallowed`：那两条挨在一起时，就是一次点击被吃了。
            self.trace.note("selection moved to the active group", "%s -> %s" % (before[0], active))
        if self.gtree.exists(active):
            # ⚠️ 这一行会触发 `<<TreeviewSelect>>`，而**挡住它的不是 `_syncing`**。
            #    2026-08-20 实测（`tests/test_gui_invariants.py::
            #    test_the_select_event_is_asynchronous`）：Tk 的虚拟事件是**排进事件
            #    队列**的，处理器晚一拍才跑 —— 那时候 `recompute()` 早就把 `_syncing`
            #    放下了。这里原来写着"`_syncing` 那条重入保护就是为这行存在的"，是错的。
            #    真正管用的是 `switch_group()` 开头那句 `name == self._active_group()`：
            #    我们只会把选中放回**当前组**自己那一行，所以晚到的那个事件恒等于
            #    "切到我已经在的组" => 早返回。
            #    ⇒ 改这里的时候记住：**选中不许落在 active 之外的行上**，
            #      否则我们自己的重画会被当成用户点击，把 A 组的取值写进 B 组
            #      （正是 `switch_group` docstring 里那个最难查的场景）。
            self.gtree.selection_set(active)
        _fit_tree_columns(
            self.gtree,
            tuple(col[0] for col in GROUP_COLS),
            head_font=self.f_ui_b,
            cell_font=self.f_mono,
            floors={key: width for key, _head, width in GROUP_COLS},
            caps=self._cap_px({"summary": GROUP_SUMMARY_CAP_CHARS}),
        )
        if self.groups_hint is not None:
            if merged:
                word = "duplicate" if merged == 1 else "duplicates"
                self.groups_hint.config(text="%d %s merged across groups" % (merged, word))
            else:
                self.groups_hint.config(text="double-click a name to rename")

    def on_group_select(self) -> None:
        """用户点了组表的一行。`_syncing` 时是我们自己在重画，不当成用户操作。"""
        if self._syncing or self.gtree is None:
            # 🚨 这条早返回**吃掉一次真实点击**：`_syncing` 期间用户点的那一行，
            #    选中已经变了、active 却没跟着变，下一次 `refresh_groups()` 又把
            #    选中拽回 active —— 在用户眼里就是"点了别的组，它自己跳回来了"。
            #    记一条才看得见它到底发不发生（本轮"删不掉原来那个组"的头号嫌疑）。
            self.trace.note(
                "on_group_select swallowed",
                "syncing=%s sel=%s active=%s"
                % (
                    self._syncing,
                    ",".join(self.gtree.selection()) if self.gtree is not None else "-",
                    self._active_group(),
                ),
            )
            return
        selection = self.gtree.selection()
        if not selection:
            self.trace.note("on_group_select: empty selection")
            return
        self.switch_group(selection[0])

    def switch_group(self, name: str) -> None:
        """切到另一个组编辑。**顺序是本方法存在的全部理由。**

        必须先把界面上现在的值落到**旧组**、再切、再把界面变量重灌成新组的值，
        最后才 `recompute()`。反过来（先切再 recompute）就是把 A 组的取值当成用户
        刚配的东西写进 B 组 —— 而且看起来完全正常（B 组"继承"了一份 A 的取值），
        直到跑出一批莫名其妙的 run 才会发现。
        """
        if not self.groups_ok or name == self._active_group():
            if name != self._active_group():
                # 只有"组面板整个没接上"才值得记。`name == active` 是我们自己
                # 重画选中之后 Tk 回送的那一下，每一拍都有，记了等于没记。
                self.trace.note("switch_group blocked", "groups_ok=False, want=%s" % name)
            return
        self.trace.note("switch_group", "%s -> %s" % (self._active_group(), name))
        if name not in {group.name for group in self._groups()}:
            # 表上这一行是**旧的**（那个组已经不在了，只是上一次重画被别的错误挡住了）。
            # 这不是"用户点错了"，弹框只会让人一点一个框；重画一次让那一行消失就行。
            # `set_active_group` 那边照旧对不认识的名字抛错 —— 它面向的是写代码的人，
            # 「静默退回 base」在那一层仍然是禁止的。
            self.trace.note("switch_group: stale row", "%s is not in the model" % name)
            self.refresh_groups()
            return
        if not self.bridge.is_running() and not self.bridge.has_submitted():
            self._guard(self.push)
        try:
            self.bridge.set_active_group(name)
        except EwaveBatchError as exc:
            _error("Cannot switch run group", str(exc))
            return
        self._reload_group_vars()
        self.recompute()

    def _reload_group_vars(self) -> None:
        """把界面变量重灌成 active group 的有效取值。**必须在下一次 `push()` 之前。**"""
        self._syncing = True
        try:
            self._apply_axis_selection(self.bridge.axis_selection())
            self._sync_override_vars()
        finally:
            self._syncing = False

    def on_override_toggle(self, _key: str) -> None:
        """勾/取消一根轴的"这个组自己定" —— 值本身由 `push()` 落进 bridge。

        勾上时写进去的是**界面上此刻显示的（继承来的）取值**：从基线出发改一根，
        比先清空再从头填一遍自然得多。取消勾选时 `push()` 撤掉覆盖，
        `_sync_group_rows()` 随即把控件重新灌成 base 的值并置灰。
        """
        self.recompute()

    def do_add_group(self) -> None:
        """加一个空组并切过去 —— **先问名字**（用户 2026-08-20 要求）。

        为什么值得多一个对话框：组名不只是表里的一行字，它会进 Runs 表、进每一条
        关于这个组的消息、还会**进产物目录名**。`eqcur-off` 和 `group-2` 在三个月后
        差别巨大，而在建它的那一刻用户脑子里恰好有那个名字 —— 那是唯一不用回想的时机。
        （改名照旧：双击组名那一列。）

        新组一根轴都不覆盖 ⇒ 展开出来与 base 逐字相同 ⇒ 全被跨组去重吃掉 ⇒ 贡献
        0 个 run。表里那个 `-> 0` 不是 bug，是"还没配"—— 勾一根轴上去它就变了。
        """
        if not self.groups_ok:
            return
        name = self._ask_group_name("New run group", self._suggest_group_name())
        if name is None:  # 取消
            return
        if not self.bridge.is_running() and not self.bridge.has_submitted():
            self._guard(self.push)
        try:
            actual = self.bridge.add_group(name)
        except EwaveBatchError as exc:
            _error("Cannot add run group", str(exc))
            return
        self.trace.note("added", "%s (a new group overrides nothing => every row is grey)" % actual)
        self._warn_if_renamed(name, actual)
        self._reload_group_vars()
        self.recompute()

    def do_duplicate_group(self) -> None:
        """复制选中的组（base 也能复制 —— 那会把当前勾选写成一份显式覆盖），**先问名字**。"""
        if not self.groups_ok:
            self.trace.note("duplicate: groups_ok=False")
            return
        source = self._selected_group()
        self.trace.note(
            "duplicate source", "selected=%s active=%s treesel=%s"
            % (source, self._active_group(),
               ",".join(self.gtree.selection()) if self.gtree is not None else "-")
        )
        name = self._ask_group_name(
            "Duplicate run group", self._suggest_group_name("%s-copy" % source), source=source
        )
        if name is None:  # 取消
            self.trace.note("duplicate cancelled")
            return
        if not self.bridge.is_running() and not self.bridge.has_submitted():
            self._guard(self.push)
        try:
            actual = self.bridge.duplicate_group(source, name)
        except EwaveBatchError as exc:
            _error("Cannot duplicate run group", str(exc))
            return
        self.trace.note(
            "duplicated", "%s -> %s  copy overrides=%s  source overrides=%s"
            % (source, actual, sorted(self._overrides_of(actual)),
               sorted(self._overrides_of(source)))
        )
        self._warn_if_renamed(name, actual)
        self._reload_group_vars()
        self.recompute()

    def _suggest_group_name(self, wanted: str = "") -> str:
        """对话框里摆的那个建议名。bridge 老到没有这个方法时退回 `wanted`。"""
        suggest = getattr(self.bridge, "suggest_group_name", None)
        if callable(suggest):
            return str(suggest(wanted))
        return wanted or "group"

    def _ask_group_name(self, title: str, initial: str, source: str = "") -> str | None:
        """问一个组名。取消 -> None；留空 -> 用建议名。

        `EWB_SMOKE=1` 下直接返回建议名（`_ask_text` 的 `on_smoke`）——
        headless 里"取消"会让整条动作变成 no-op，那样 run group 这一块在
        `tests/test_gui_invariants.py` 的动作序列里就形同不存在。
        """
        hint = GROUP_NAME_HINT
        if source:
            hint = "Copy of %r. " % source + hint
        answer = self._ask_text(
            title, "Group name", initial, hint=hint, on_empty=initial, on_smoke=initial
        )
        self.trace.note(
            "%s dialog" % title, "suggested=%r answered=%r" % (initial, answer)
        )
        return answer

    def _warn_if_renamed(self, wanted: str, actual: str) -> None:
        """要的名字被占了、自动加了后缀 —— **说一声**。

        静默改名的代价：用户在 Runs 表里找 `hot`，看到的是 `hot-2`，
        而他从来没打过那个名字。
        """
        if actual and wanted and actual != wanted:
            _info(
                "Run group renamed",
                "There is already a group called %r, so this one is called %r."
                % (wanted, actual),
            )

    def do_remove_group(self) -> None:
        """删掉选中的组。删完退回 base（`remove_group` 自己会做）。"""
        if not self.groups_ok:
            return
        # 先把界面上的值落进 bridge —— 与 Add / Duplicate 同一条规矩。少了这一步，
        # "在 A 组里改了温度，然后删掉 B 组"会让那个温度被随后的 `_reload_group_vars()`
        # 用模型里的旧值盖回去（用户没撤销过任何东西，改动却没了）。
        if not self.bridge.is_running() and not self.bridge.has_submitted():
            self._guard(self.push)
        name = self._selected_group()
        self.trace.note(
            "remove target", "selected=%s active=%s treesel=%s"
            % (name, self._active_group(),
               ",".join(self.gtree.selection()) if self.gtree is not None else "-")
        )
        if name == BASE_GROUP:
            _info(
                "Cannot remove the base group",
                "The base group is the top-level settings themselves - every batch has one.\n"
                "Remove one of the other groups instead.",
            )
            return
        try:
            self.bridge.remove_group(name)
        except EwaveBatchError as exc:
            _error("Cannot remove run group", str(exc))
            return
        still = [group.name for group in self._groups()]
        self.trace.note(
            "removed" if name not in still else "REMOVE DID NOTHING",
            "%s; groups now %s" % (name, still),
        )
        self._reload_group_vars()
        self.recompute()

    def on_group_rename(self, event: object) -> None:
        """双击组名那一列 = 改名。别的列双击什么都不做（免得误触）。"""
        if not self.groups_ok or self.gtree is None:
            return
        iid = self.gtree.identify_row(event.y)  # type: ignore[attr-defined]
        column = self.gtree.identify_column(event.x)  # type: ignore[attr-defined]
        if not iid or column != "#1":
            return
        if iid == BASE_GROUP:
            _info(
                "Cannot rename the base group",
                "%r is a reserved name: it means the top-level settings." % BASE_GROUP,
            )
            return

        dlg = tk.Toplevel(self.top)
        dlg.title("Rename run group")
        dlg.transient(self.top)
        var = tk.StringVar(value=iid)
        ttk.Label(dlg, text="Group name").grid(row=0, column=0, sticky=tk.W, padx=8, pady=6)
        ttk.Entry(dlg, textvariable=var, width=26, font=self.f_mono).grid(
            row=0, column=1, padx=8, pady=6
        )
        ttk.Label(
            dlg,
            text="The name shows up in the Runs table and in every message about this group.",
            style="Hint.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky=tk.W, padx=8)

        def ok() -> None:
            try:
                self.bridge.rename_group(iid, var.get().strip())
            except EwaveBatchError as exc:
                dlg.destroy()
                _error("Cannot rename run group", str(exc))
                return
            dlg.destroy()
            self.recompute()

        ttk.Button(dlg, text="OK", command=ok).grid(row=2, column=1, sticky=tk.E, padx=8, pady=8)
        if not smoke_enabled():
            dlg.grab_set()

    # ---- run group：bridge 缺席时的兜底（见 `__init__` 里的 `groups_ok`）
    def _groups(self) -> tuple[object, ...]:
        return self.bridge.groups() if self.groups_ok else ()

    def _active_group(self) -> str:
        return self.bridge.active_group() if self.groups_ok else BASE_GROUP

    def _active_is_base(self) -> bool:
        return self._active_group() == BASE_GROUP

    def _group_summary(self, name: str) -> str:
        try:
            return self.bridge.group_summary(name)
        except (AttributeError, EwaveBatchError):
            return ""

    def _overrides_of(self, name: str) -> tuple[str, ...]:
        """某个组覆盖了哪几根轴（**只给轨迹用**）。问不出来 -> 空。

        复制出来的组和源组是不是"两份"，看的就是这个：两边各自的键集合互不影响
        才算独立（值本身相同是正常的 —— 复制嘛）。
        """
        for group in self._groups():
            if group.name == name:
                return tuple(sorted(getattr(group, "axis_overrides", {}) or {}))
        return ()

    def _selected_group(self) -> str:
        """组表里选中的那一行；没选中就用 active group（表可能没焦点）。"""
        if self.gtree is not None:
            selection = self.gtree.selection()
            if selection:
                return selection[0]
        return self._active_group()

    def _new_override_var(self, key: str) -> tk.BooleanVar:
        """给 `GROUP_ROW_AXES` 的一行造"覆盖"勾选变量（`_srow` 之外的那一行用）。"""
        var = tk.BooleanVar(value=True)
        self.ovr_vars[key] = var
        return var

    # ------------------------------------------------------------- settings
    def _srow(
        self, parent: object, row: int, label: str, width: int = 14, key: str = ""
    ) -> tuple[ttk.Frame, ttk.Label]:
        """Settings 的一行：`[覆盖勾选框] 标签 | 控件 | -> N`。

        第 0 列是**组感知**那一列：勾上 = 当前这个组自己定这根轴，不勾 = 继承 base
        （控件置灰、显示继承来的值）。编辑 base 时它恒选中且点不动 —— base 是继承链的
        源头，"base 继承谁"这个问题没有答案，所以那个勾选框不该是可点的。
        `key` 空（扫频那一行）= 这一行不参与组覆盖，第 0 列留空占位保持对齐。
        """
        if key:
            var = tk.BooleanVar(value=True)
            self.ovr_vars[key] = var
            check = ttk.Checkbutton(
                parent,  # type: ignore[arg-type]
                variable=var,
                command=lambda k=key: self.on_override_toggle(k),
            )
            check.grid(row=row, column=0, sticky=tk.W)
            self.ovr_boxes[key] = check
            _Tooltip(check, OVERRIDE_TIP % label)
        ttk.Label(parent, text=label, width=width, anchor=tk.W).grid(  # type: ignore[arg-type]
            row=row, column=1, sticky=tk.W, pady=2
        )
        box = ttk.Frame(parent)  # type: ignore[arg-type]
        box.grid(row=row, column=2, sticky=tk.W)
        if key:
            self.srow_boxes[key] = box
        count = ttk.Label(parent, text="-> 1", style="Count.TLabel", anchor=tk.E, width=6)  # type: ignore[arg-type]
        count.grid(row=row, column=3, sticky=tk.E, padx=(10, 0))
        ttk.Separator(parent, orient=tk.HORIZONTAL).grid(  # type: ignore[arg-type]
            row=row, column=0, columnspan=4, sticky="sew"
        )
        return box, count

    def build_settings(
        self,
        parent: object,
        compact: bool = False,
        title: str = " Settings ",
        show_formula: bool = True,
    ) -> ttk.LabelFrame:
        box = ttk.LabelFrame(parent, text=title, padding=7)  # type: ignore[arg-type]
        self.settings_box = box
        self.settings_title = title
        grid = ttk.Frame(box)
        grid.pack(fill=tk.X)
        self.settings_grid = grid
        # 只在编辑 base 之外的组时才 pack（见 `_sync_group_hint`）—— base 是常态，
        # 常态下多一行永远为空的灰字只是噪声。用 tk.Label 而不是 ttk：前景色走 style
        # 会污染别的标签（同 `extra_warn` / `groups_warn`）。
        self.group_hint = tk.Label(
            box, font=self.f_ui, fg=HINT, anchor=tk.W, justify=tk.LEFT, wraplength=520
        )
        # 第 0 列是"覆盖"勾选框，给它一个固定的最小宽度：没有它的那几行（扫频、
        # 以及 base 组下被藏起来的时候）会让标签整列跳一下。
        grid.columnconfigure(0, minsize=20)
        grid.columnconfigure(2, weight=1)
        # ⚠️ `compact` **不再缩写任何标签**（2026-08-19，D2）。它当初是为一个钉死在
        #    452px 的左栏做的妥协：Temperature -> Temp、Frequency sweep -> Freq sweep、
        #    vertical distance -> vert、via merge space -> via merge、
        #    "degC, comma separated" -> "degC, comma sep."。而那个左栏现在是
        #    `ttk.PanedWindow` 的一格，宽度由内容决定、还能拖 —— 宽度本来就不紧张，
        #    缩写换不到任何东西，只留下"via merge 到底是什么"这个问题。
        #    `compact` 保留下来只管两样：输入框窄一点、勾选框之间的间距小一点。
        lw = 15

        box_c, self.cnt_corner = self._srow(grid, 0, "Corner", lw, key="corner")
        for name in gui_state.CORNER_VALUES:
            ttk.Checkbutton(
                box_c, text=name, variable=self.corner_vars[name], command=self.recompute
            ).pack(side=tk.LEFT, padx=(0, 6 if compact else 11))

        box_t, self.cnt_temp = self._srow(grid, 1, "Temperature", lw, key="temperature")
        entry = ttk.Entry(box_t, textvariable=self.temp, font=self.f_mono, width=20 if compact else 32)
        entry.pack(side=tk.LEFT)
        entry.bind("<KeyRelease>", lambda _e: self.recompute())
        ttk.Label(box_t, text="degC, comma separated", style="Hint.TLabel").pack(
            side=tk.LEFT, padx=5
        )

        box_m, self.cnt_mode = self._srow(grid, 2, "Mode", lw, key="fullWave")
        for name in ("Quasi-static", "Full wave"):
            ttk.Checkbutton(
                box_m, text=name, variable=self.mode_vars[name], command=self.recompute
            ).pack(side=tk.LEFT, padx=(0, 12))

        box_f, self.cnt_freq = self._srow(grid, 3, "Frequency sweep", lw)
        combo = ttk.Combobox(
            box_f,
            textvariable=self.sw_mode,
            values=list(gui_state.SWEEP_MODES),
            width=11,
            state="readonly",
            font=self.f_mono,
        )
        combo.pack(side=tk.LEFT)
        combo.bind("<<ComboboxSelected>>", lambda _e: self.recompute())
        self.sw_combo = combo
        # `step` 和 `points` 的标签是**单选钮**而不是 Label：eWave 那条 flag 只有两种
        # 写法二选一（`adaptive,0:0.1:40` 或 `adaptive,0-41-40`），而原来两个格子同时
        # 可编辑、同时有值，界面完全没说会用哪个（实际是 points 悄悄赢）。
        # 用户 2026-08-20 指出的就是这个。单选钮把"二选一"这件事画了出来。
        self.freq_entries: dict[str, tuple[object, ttk.Entry]] = {}
        self.freq_vars: dict[str, tk.StringVar] = {}
        for key, var in (
            ("start", self.f_start),
            ("stop", self.f_stop),
            ("step", self.f_step),
            ("points", self.f_pts),
        ):
            label: object
            if key in gui_state.SWEEP_SPACINGS:
                label = ttk.Radiobutton(
                    box_f,
                    text=key,
                    value=key,
                    variable=self.sw_spacing,
                    command=self.recompute,
                )
            else:
                label = ttk.Label(box_f, text=key)
            label.pack(side=tk.LEFT, padx=(8, 3))  # type: ignore[attr-defined]
            entry = ttk.Entry(box_f, textvariable=var, width=6, font=self.f_mono)
            entry.pack(side=tk.LEFT)
            entry.bind("<KeyRelease>", lambda _e: self.recompute())
            self.freq_entries[key] = (label, entry)
            self.freq_vars[key] = var
        ttk.Label(box_f, text="GHz", style="Hint.TLabel").pack(side=tk.LEFT, padx=4)

        box_h, self.cnt_mesh = self._srow(grid, 4, "Mesh", lw, key="mesh")
        for label_text, var in (
            ("edge distance", self.m_edge),
            ("vertical distance", self.m_vert),
            ("via merge space", self.m_via),
        ):
            ttk.Label(box_h, text=label_text).pack(side=tk.LEFT, padx=(0, 3))
            entry = ttk.Entry(box_h, textvariable=var, width=5, font=self.f_mono)
            entry.pack(side=tk.LEFT, padx=(0, 10))
            entry.bind("<KeyRelease>", lambda _e: self.recompute())

        # Advanced：收起来是一行摘要，展开是真控件（第 3 层「逃生口」住在这儿）
        adv_check = ttk.Checkbutton(
            grid,
            variable=self._new_override_var("advanced"),
            command=lambda: self.on_override_toggle("advanced"),
        )
        adv_check.grid(row=5, column=0, sticky=tk.W)
        self.ovr_boxes["advanced"] = adv_check
        # ★ 这一个只管 equalCurrent（两个 tolerance 只属于 base）—— 提示语里说清楚，
        #   否则用户会以为勾上之后这个组连 tolerance 一起自己定了。
        _Tooltip(adv_check, OVERRIDE_TIP % "equalCurrent (tolerances stay on base)")
        adv_head = ttk.Frame(grid)
        adv_head.grid(row=5, column=1, columnspan=2, sticky=tk.W, pady=(2, 0))
        self.adv_btn = ttk.Button(adv_head, text="+ Advanced", width=13, command=self.toggle_adv)
        self.adv_btn.pack(side=tk.LEFT)
        self.adv_summary = ttk.Label(adv_head, text="", style="Off.TLabel")
        self.adv_summary.pack(side=tk.LEFT, padx=8)
        self.cnt_adv = ttk.Label(grid, text="-> 1", style="Off.TLabel", anchor=tk.E, width=6)
        self.cnt_adv.grid(row=5, column=3, sticky=tk.E, padx=(10, 0))

        self.adv_body = ttk.Frame(grid)
        row = ttk.Frame(self.adv_body)
        row.pack(anchor=tk.W, pady=(3, 0))
        # ★ Advanced 那一行的"覆盖"勾选框只管 **equalCurrent 这一小块**：
        #   两个 tolerance 跟扫频一样只属于 base（见 `GROUP_ROW_AXES`），
        #   Extra flags 更不是轴（整批共用），编辑某个组时照样得能改。
        eq_box = ttk.Frame(row)
        eq_box.pack(side=tk.LEFT)
        self.srow_boxes["advanced"] = eq_box
        ttk.Checkbutton(
            eq_box, text="equalCurrent on", variable=self.eq_on, command=self.recompute
        ).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Checkbutton(eq_box, text="off", variable=self.eq_off, command=self.recompute).pack(
            side=tk.LEFT, padx=(0, 18)
        )
        self.tol_box = ttk.Frame(row)
        self.tol_box.pack(side=tk.LEFT)
        for label, var in (
            ("relative tolerance", self.tol_r),
            ("relative current tolerance", self.tol_c),
        ):
            ttk.Label(self.tol_box, text=label).pack(side=tk.LEFT, padx=(0, 3))
            entry = ttk.Entry(self.tol_box, textvariable=var, width=9, font=self.f_mono)
            entry.pack(side=tk.LEFT, padx=(0, 14))
            entry.bind("<KeyRelease>", lambda _e: self.recompute())

        extra_row = ttk.Frame(self.adv_body)
        extra_row.pack(anchor=tk.W, fill=tk.X, pady=(4, 0))
        ttk.Label(extra_row, text="Extra ewave flags").pack(side=tk.LEFT, padx=(0, 4))
        extra_entry = ttk.Entry(extra_row, textvariable=self.extra, font=self.f_mono)
        extra_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        extra_entry.bind("<KeyRelease>", lambda _e: self.recompute())
        self.extra_entry = extra_entry
        ttk.Button(extra_row, text="Defaults...", width=11, command=self.show_defaults).pack(
            side=tk.LEFT
        )
        # 撞轴要标红（§11 规则 2）。用 tk.Label 而不是 ttk：ttk 的前景色要走 style，
        # 而这一条是**一次性的告警**，走 style 会污染别的标签。
        self.extra_warn = tk.Label(
            self.adv_body, font=self.f_ui, fg=RED, anchor=tk.W, justify=tk.LEFT, wraplength=520
        )

        if show_formula:
            ttk.Separator(box, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(6, 3))
            total = ttk.Frame(box)
            total.pack(fill=tk.X)
            ttk.Label(total, text="Total", font=self.f_ui_b).pack(side=tk.LEFT)
            self.formula_lbl = ttk.Label(total, text="", style="Count.TLabel", font=self.f_mono_b)
            self.formula_lbl.pack(side=tk.RIGHT)
        else:
            self.formula_lbl = None
        return box

    def toggle_adv(self) -> None:
        if self.adv_open:
            self.adv_body.grid_forget()
            self.adv_btn.config(text="+ Advanced")
            self.adv_summary.pack(side=tk.LEFT, padx=8)
        else:
            self.adv_body.grid(row=6, column=0, columnspan=4, sticky=tk.W + tk.E)
            self.adv_btn.config(text="- Advanced")
            self.adv_summary.pack_forget()
        self.adv_open = not self.adv_open

    # ------------------------------------------------------------ resources
    def build_resources(self, parent: object, compact: bool = False) -> ttk.LabelFrame:
        """**整条 dsub 命令原样暴露给用户改**（用户 2026-08-18 要求，不是只让改 `-R`）。

        改完之后 `-R` 里的 `cpu=` 仍然要能读回去同步 `--parallel` ——
        那一步走 `core.cmd.parse_resource_string`（`GuiState.parallel()`），
        本文件不再解析第二遍。

        开局那一格里是 `gui.state.DEFAULT_SUBMIT_COMMAND` 那条模板，**不是空的**
        （用户 2026-08-20：空输入框不告诉任何人它想要什么，于是「Donau 到底在哪里
        设置」这个问题在界面上无解）。模板里的账号 / 队列是占位符，没换掉时
        `dsub_warn` 那行红字一直在，真提交也会被 `GuiState._make_scheduler()` 拦下 ——
        「给个默认值」和「不许拿默认值去提交」是同一条改动的两半，别只留一半。

        ⚠️ `compact` 现在**什么都不管**（D3：它曾经把 "dsub command" 那个标签整个藏掉）。
        参数留着是因为 `gui/frames/split.py` 在传它，而"布局传了一个共用层不认识的
        hint"会被 `tests/test_gui_frames.py` 记成 `dropped_hints`（那是三版分岔的信号）。
        """
        box = ttk.LabelFrame(parent, text=" Donau submit ", padding=7)  # type: ignore[arg-type]
        top = ttk.Frame(box)
        top.pack(fill=tk.X)
        # ⚠️ `compact` 曾经把这个标签整个藏掉，于是 split 版的 Donau submit 里只剩一个
        #    **没有任何标签的空输入框**（2026-08-19 实拍 D3）—— 一个空框不告诉任何人
        #    它想要什么，省下那 100px 换不到这个代价。标签一律显示。
        ttk.Label(top, text="dsub command", width=15, anchor=tk.W).pack(side=tk.LEFT)
        entry = ttk.Entry(top, textvariable=self.dsub, font=self.f_mono)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        entry.bind("<KeyRelease>", lambda _e: self.recompute())

        bottom = ttk.Frame(box)
        bottom.pack(fill=tk.X, pady=(4, 0))
        self.par_lbl = ttk.Label(bottom, text="", style="Green.TLabel")
        self.par_lbl.pack(side=tk.LEFT)
        # ★ 「同时在飞几个」必须在界面上（用户 2026-08-20：提交 5 个、4 个 running、
        #   第 5 个停在 ready，「很奇怪」）。那个 4 是 `BatchOptions.max_parallel` 的
        #   默认值，在这之前既不显示也改不了 —— 一个看不见的上限，等于一个没有理由
        #   的行为。摆在 dsub 命令旁边：它和"提交"是同一件事的两半。
        ttk.Label(bottom, text="Max in flight", anchor=tk.E).pack(side=tk.RIGHT, padx=(0, 4))
        spin = tk.Spinbox(
            bottom,
            from_=1,
            to=gui_state.MAX_PARALLEL_CAP,
            width=4,
            textvariable=self.maxpar,
            font=self.f_mono,
            command=self.on_max_parallel,
        )
        spin.pack(side=tk.RIGHT)
        spin.bind("<KeyRelease>", lambda _e: self.on_max_parallel())
        _Tooltip(spin, MAX_PARALLEL_TIP)
        self.dsub_warn = ttk.Label(box, text="", style="Warn.TLabel", wraplength=520, justify=tk.LEFT)
        return box

    # ----------------------------------------------------------------- runs
    def build_runs(
        self,
        parent: object,
        rows: int = 12,
        titled: bool = True,
        header_in_title: bool = True,
    ) -> ttk.Widget:
        box: ttk.Widget
        if titled:
            box = ttk.LabelFrame(parent, text=" Runs ", padding=7)  # type: ignore[arg-type]
        else:
            box = ttk.Frame(parent)  # type: ignore[arg-type]
        self.runs_box = box
        self.runs_titled = titled

        # 陈旧告警。**建在表上面**（不是状态栏里）—— 它说的是"这张表"，
        # 而状态栏在窗口最底下，离表最远。tk.Label 不是 ttk：前景色走 style
        # 会污染别的标签（同 `extra_warn` / `groups_warn`）。
        self.runs_stale = tk.Label(
            box, font=self.f_ui, fg=RED, anchor=tk.W, justify=tk.LEFT, wraplength=760
        )

        if not header_in_title:
            head = ttk.Frame(box)
            head.pack(fill=tk.X, pady=(0, 4))
            ttk.Label(head, text="Runs", font=self.f_ui_b).pack(side=tk.LEFT)
            self.runs_header = ttk.Label(head, text="", style="Hint.TLabel")
            self.runs_header.pack(side=tk.LEFT, padx=8)
        else:
            self.runs_header = None

        wrap = ttk.Frame(box)
        wrap.pack(fill=tk.BOTH, expand=True)
        columns = tuple(col[0] for col in RUN_COLS)
        self.tree = ttk.Treeview(
            wrap, columns=columns, show="headings", height=rows, style="Runs.Treeview"
        )
        for key, head, width in RUN_COLS:
            self.tree.heading(key, text=head)
            # `design` 和 `extra` 一起 stretch。原来只有 `extra` 会伸 —— 于是在
            # stacked 那种宽窗口里它白占 459px（内容只要 93px），而同一张表里
            # `design` 被钉在 150px、两个不同的 design 显示成同一串前缀。
            self.tree.column(key, width=width, anchor=tk.W, stretch=(key in RUN_STRETCH_COLS))
        for key in ("n", "temp", "wall"):
            self.tree.column(key, anchor=tk.E)
        _scrolled_tree_grid(wrap, self.tree, viewport_width=RUNS_VIEWPORT_WIDTH)
        for name, (background, foreground) in STATUS_STYLE.items():
            self.tree.tag_configure(name, background=background, foreground=foreground)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.show_detail())
        self.tree.bind("<Button-3>", self.popup)

        self.empty_lbl = tk.Label(
            wrap,
            justify=tk.CENTER,
            fg="#8d8d8d",
            bg="white",
            font=self.f_ui,
            text=RUNS_EMPTY_HINT,
        )

        self.menu = tk.Menu(self.top, tearoff=0)
        for item in MENU_ITEMS:
            if item == "-":
                self.menu.add_separator()
            else:
                _add_menu_item(
                    self.menu,
                    item,
                    None if item in DISABLED_MENU_ITEMS else (lambda t=item: self.on_row_action(t)),
                )
        # `_sync_runs_stale` 要把红字插到**第一个子件之前**（= 表的上面）。
        # 建完才知道第一个是谁（`header_in_title` 决定有没有那条 head）。
        slaves = box.pack_slaves()
        self._runs_stale_anchor = slaves[0] if slaves else None
        return box

    def build_detail(self, parent: object) -> ttk.LabelFrame:
        """`Selected run` —— 落地目录 + **完整命令**（一行一个 flag）。"""
        box = ttk.LabelFrame(parent, text=" Selected run ", padding=7)  # type: ignore[arg-type]
        self.detail_box = box
        self.out_var = tk.StringVar(value=_DASH)
        ttk.Label(box, text="Out dir", width=9, anchor=tk.W).grid(row=0, column=0, sticky=tk.W)
        out = tk.Entry(
            box,
            textvariable=self.out_var,
            font=self.f_mono,
            state="readonly",
            readonlybackground="#f6f6f6",
            relief=tk.SOLID,
            bd=1,
            fg="#222222",
        )
        out.grid(row=0, column=1, sticky="ew", pady=1)
        # 单行输入框装绝对路径，窗口一窄就只看得见开头一截。只读 Entry 本来就能用
        # 方向键/拖选横向滚动，缺的只是"完整的那一份在哪" —— 悬停给出全文（B4）。
        _Tooltip(out, self.out_var.get)

        ttk.Label(box, text="Command", width=9, anchor=tk.NW).grid(row=1, column=0, sticky=tk.NW)
        holder = ttk.Frame(box)
        holder.grid(row=1, column=1, sticky="ew", pady=1)
        self.cmd_text = tk.Text(
            holder,
            height=CMD_ROWS_MIN,
            wrap="none",
            font=self.f_mono,
            relief=tk.SOLID,
            bd=1,
            background="#f6f6f6",
            foreground="#222222",
        )
        cmd_scroll = ttk.Scrollbar(holder, orient=tk.VERTICAL, command=self.cmd_text.yview)
        self.cmd_text.configure(yscrollcommand=cmd_scroll.set, state="disabled")
        self.cmd_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        cmd_scroll.pack(side=tk.LEFT, fill=tk.Y)
        box.columnconfigure(1, weight=1)
        return box

    # ----------------------------------------------------------- action bar
    def build_actionbar(
        self, parent: object, show_formula: bool = False, show_dir: bool = True
    ) -> ttk.Frame:
        f = ttk.Frame(parent, padding=(8, 5))  # type: ignore[arg-type]
        if show_formula:
            self.bar_formula = ttk.Label(f, text="", style="Count.TLabel", font=self.f_mono_b)
            self.bar_formula.pack(side=tk.LEFT)
            ttk.Separator(f, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        else:
            self.bar_formula = None

        self.btn: dict[str, ttk.Button] = {}
        for text, command, style in (
            ("Dry-run", self.do_dry_run, "TButton"),
            ("Submit", self.do_submit, "Accent.TButton"),
            ("Cancel", self.do_cancel, "TButton"),
            ("Resume", self.do_resume, "TButton"),
        ):
            button = ttk.Button(f, text=text, width=9, command=command, style=style)
            button.pack(side=tk.LEFT, padx=2)
            self.btn[text] = button
        ttk.Button(f, text="Open batch dir", width=15, command=self.do_open_batch_dir).pack(
            side=tk.LEFT, padx=(8, 2)
        )
        # C5：「N / M done」原来在这里也有一份，与状态栏右下角那份逐字相同。
        # 两份同一个数字唯一的作用是让人怀疑它们会不会不一样。留状态栏那份
        # （状态栏就是干这个的），这里空出来的宽度全给批次目录。
        self.right_lbl = None
        if show_dir:
            ttk.Label(f, text="Batch dir", style="Hint.TLabel").pack(side=tk.LEFT, padx=(10, 4))
            # B2：批次目录实测要 1336px、给到 927px，于是路径**从中间断掉**——
            # 一条只剩前半截的路径比没有更坏（看起来像一条完整的、错的路径）。
            # `_ElideLabel` 保头保尾：`/home/.../<batch_name>/`，全文在 tooltip 里。
            self.batchdir_lbl = _ElideLabel(f, font=self.f_mono, chars=16, style="Mono.TLabel")
            self.batchdir_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
            _Tooltip(self.batchdir_lbl, self.batchdir_lbl.full_text)
        else:
            self.batchdir_lbl = None
        return f

    def build_statusbar(self, parent: object) -> ttk.Frame:
        """状态栏：左边一句话，右边 `N / M done`，最右一个 **Log** 按钮。

        Log 按钮在这儿而不是只在菜单里：状态栏那一行是**摘要**，而摘要装不下
        dry-run 真正的产出（十几条命令，一条 300 多字符）。按钮就摆在摘要旁边，
        "想看细节点这里"才是一条走得通的路 —— 藏进 Tools 菜单的东西没人找得到。
        """
        f = ttk.Frame(parent, padding=(8, 2), relief=tk.SUNKEN)  # type: ignore[arg-type]
        self.status_lbl = ttk.Label(f, text="", style="Hint.TLabel")
        self.status_lbl.pack(side=tk.LEFT)
        # 先 pack 的更靠右 -> Log 在最右，`N / M done` 在它左边。
        self.log_btn = ttk.Button(f, text="Log", width=11, command=self.show_log)
        self.log_btn.pack(side=tk.RIGHT, padx=(8, 0))
        self.status_right = ttk.Label(f, text="", style="Mono.TLabel")
        self.status_right.pack(side=tk.RIGHT)
        return f

    # -------------------------------------------------------------- compute
    def push(self) -> None:
        """GUI 变量 → bridge。**这是唯一往核心写值的地方**（别在回调里各写一份）。

        轴的取值一律经 `_push_axis()`：它按当前那一行的"覆盖"勾选框决定写进 base
        还是写进 active group 的覆盖。active = base 时逐字等于加组之前。
        """
        b = self.bridge
        b.set_batch_name(self.batch.get())
        setter = getattr(b, "set_batch_root", None)
        if callable(setter) and self.broot.get().strip() != getattr(b, "batch_root", ""):
            setter(self.broot.get())
        if self.offdir.get().strip() != b.official_run_dir:
            b.set_official_run_dir(self.offdir.get())
        self._push_axis(
            "corner",
            "corner",
            [name for name in gui_state.CORNER_VALUES if self.corner_vars[name].get()],
        )
        self._push_axis(
            "temperature", "temperature", gui_state.parse_value_list(self.temp.get())
        )
        self._push_axis(
            "fullWave",
            "fullWave",
            [
                value
                for value, var in (("off", self.mode_vars["Quasi-static"]),
                                   ("on", self.mode_vars["Full wave"]))
                if var.get()
            ],
        )
        self._push_axis(
            "advanced",
            "equalCurrent",
            [value for value, var in (("on", self.eq_on), ("off", self.eq_off)) if var.get()],
        )
        self._push_axis(
            "mesh",
            "mesh",
            [gui_state.mesh_axis_value(self.m_edge.get(), self.m_vert.get(), self.m_via.get())],
        )
        self._push_base_axis("relativeTolerance", [self.tol_r.get()])
        self._push_base_axis("relativeCurrentTolerance", [self.tol_c.get()])
        # 扫频和两个 tolerance 一样只属于 base（见 `GROUP_ROW_AXES` 的注释）。
        # 编辑别的组时**一个字都不写**（同 `_push_base_axis`）：那几个格子此刻是置灰的、
        # 装的是 base 的值，回写它们最好的情况是 no-op，最坏的情况是把界面上某个被
        # 别处清空的格子当成用户的输入写进基线 —— 后者 2026-08-20 真发生过
        # （见 `_sync_freq_fields` 里那段 🚨）。让"扫频只属于 base"由结构保证，
        # 而不是由"那几个格子的值恰好没变"保证。
        if self._active_is_base():
            b.set_sweep(
                mode=self.sw_mode.get(),
                spacing=self.sw_spacing.get(),
                start=self.f_start.get(),
                stop=self.f_stop.get(),
                step=self.f_step.get(),
                points=self.f_pts.get(),
            )
        b.set_extra_flags(self.extra.get())
        b.set_submit_command(self.dsub.get())

    def _push_axis(self, key: str, axis: str, values: Sequence[str]) -> None:
        """一根轴的取值 → bridge，按那一行的"覆盖"勾选框分流。

        * active = base（或 bridge 根本没有组这一说）：`set_axis_values` —— 与加组之前
          **逐字相同**，这条路上一个新分支都没有。
        * active 是别的组、这一行勾着："这个组自己定" ⇒ 写这个组的覆盖；
        * active 是别的组、这一行没勾："继承 base" ⇒ **撤掉**覆盖。不撤的话，
          用户每敲一个键都会把继承来的值原样写成一份显式覆盖，于是"继承"这件事
          在存盘的 spec 里当场消失（组从两行变成把 base 抄了一遍）。

        非 base 那条路各自过闸：组在一根 base 没勾过的轴上写覆盖是会被核心拒绝的
        （消息很具体），而那不该把 `push()` 后面几条（扫频/Extra flags/提交命令）
        一起吞掉 —— 半推半就的 push 比一条错误消息难查得多。
        """
        if self._active_is_base():
            self.bridge.set_axis_values(axis, values)
            return
        var = self.ovr_vars.get(key)
        if var is None or var.get():
            self._guard(lambda: self.bridge.set_group_override(axis, values))
        else:
            self._guard(lambda: self.bridge.clear_group_override(axis))

    def _push_base_axis(self, axis: str, values: Sequence[str]) -> None:
        """只属于 base 的轴（两个 tolerance；扫频走 `set_sweep` 也是同一条规矩）。

        编辑别的组时**什么都不做** —— 不是写、也不是清。不写是因为界面上那几个格子
        是置灰的、里面本来就是 base 的值；不清是因为手写的 spec 文件里完全可以有一个
        组覆盖了 tolerance，界面看不见它不代表可以替用户删掉它。
        """
        if self._active_is_base():
            self.bridge.set_axis_values(axis, values)

    def _sync_override_vars(self) -> None:
        """"覆盖"勾选框 ← bridge。判据就是 `group_override()` 返不返回 None。

        base 组恒选中（它是继承链的源头，没有"继承"可言）。
        """
        if not self.ovr_vars:
            return
        base = self._active_is_base()
        for key, var in self.ovr_vars.items():
            if base:
                var.set(True)
                continue
            var.set(
                any(
                    self.bridge.group_override(axis) is not None
                    for axis in GROUP_ROW_AXES.get(key, ())
                )
            )

    def _sync_group_rows(self) -> None:
        """Settings 整块的组感知：勾选框能不能点、控件灰不灰、继承的行显示什么。

        跑在 `push()` **之后**（`recompute()` 的顺序），所以这里读到的覆盖就是刚写进去
        的那份。继承的那几行顺手用 base 的值重灌一遍 —— 它们此刻是禁用的，用户不可能
        正在里面打字，覆盖它们不会跟输入打架。
        """
        if not self.ovr_vars:
            return
        base = self._active_is_base()
        self._sync_override_vars()
        self._sync_group_hint(base)
        for key, check in self.ovr_boxes.items():
            check.state(["disabled"] if base else ["!disabled"])
        # 两个 tolerance 只属于 base（见 `GROUP_ROW_AXES`），跟扫频一样整块跟着走。
        tol_box = getattr(self, "tol_box", None)
        if tol_box is not None:
            _set_enabled(tol_box, base)
        if base:
            for container in self.srow_boxes.values():
                _set_enabled(container, True)
            return
        selection = self.bridge.axis_selection()
        inherited = [key for key, var in self.ovr_vars.items() if not var.get()]
        for key, container in self.srow_boxes.items():
            _set_enabled(container, key not in inherited)
        if inherited:
            self._syncing_vars(lambda: self._apply_axis_selection(selection, only=inherited))

    def _sync_group_hint(self, base: bool) -> None:
        """Settings 底下那行「为什么这些格子是灰的」。base 时整行收起来。

        `pack(after=...)` 要在**已经 pack 过 grid 的同一个 master 里**才认得位置，
        所以这行字建在 `box` 里、紧跟在 `settings_grid` 后面 —— 不这么写它会掉到
        Total 那条公式底下，离它解释的那堆灰格子最远。
        """
        if self.group_hint is None or self.settings_grid is None:
            return
        if base:
            if self.group_hint.winfo_manager():
                self.group_hint.pack_forget()
            return
        self.group_hint.config(text=GROUP_EDIT_HINT % self._active_group())
        if not self.group_hint.winfo_manager():
            # 每次 recompute 都 re-pack 会闪，所以只在没被摆出来的时候摆一次。
            self.group_hint.pack(fill=tk.X, after=self.settings_grid, pady=(4, 0))

    def _syncing_vars(self, step: object) -> None:
        """在"这是我们自己在写变量、不是用户操作"的旗子下跑一步。"""
        was = self._syncing
        self._syncing = True
        try:
            step()  # type: ignore[operator]
        finally:
            self._syncing = was

    def _formula_target(self) -> str:
        """那条乘法公式该显示在**哪一个**地方。

        C4：split 里它同时渲染两遍（Settings 的边框标题一份、Total 行一份，字一模一样）。
        而 stacked 恰好相反 —— 它两个都没有（settings 传 `show_formula=False`、
        actionbar 也不显示），公式在那一版**根本不出现**。两个 bug 是同一件事：
        "在哪显示"被三个布局参数各自决定了一半。

        所以改成在这里**统一挑一个**，优先级 = 离设定最近的那个：
        Total 行 > 动作栏 > 边框标题。每一版都恰好有一处，一处不多、一处不少。
        建控件的顺序不影响它 —— 本方法只在 `_sync_counts()` 里调，那时候三样都建完了。
        """
        if self.formula_lbl is not None:
            return "total"
        if self.bar_formula is not None:
            return "bar"
        return "title"

    def _settings_title(self, formula: str) -> str:
        """Settings 边框上的标题。**编辑的是哪个组必须写在这儿。**

        只有一个 base 组时不写"editing: base"：那是最常见的场景，多一句
        只会让人以为自己进了什么模式。组多于一个时它就是"我改的到底是哪一行"的答案。
        """
        title = self.settings_title.strip()
        if len(self._groups()) > 1:
            title += " - editing: %s" % self._active_group()
        if self._formula_target() == "title":
            title += "   -   %s" % formula
        return " %s " % title

    def recompute(self) -> None:
        """勾选变了 → 推给 bridge → 重算矩阵 → 刷新整屏。**重入安全。**"""
        if self._syncing:
            return
        self._syncing = True
        self._error = ""
        try:
            # ⚠️ 跑过之后**不再重新展开矩阵**。`plan()` 会造一份全新的、每个 run 都是
            # `ready` 的 state —— 表上那些 done / failed 会当场消失，而界面上看起来
            # 只是"刷新了一下"。要改设定就按 New batch（`bridge.reset()`）。
            if not self.bridge.is_running() and not self.bridge.has_submitted():
                self._guard(self.push)
                self._guard(self.bridge.plan)
            # 这四条也要各自过闸：用户把 Mesh 的某个格子清空、把温度写成一个词，
            # 核心会（正确地）拒绝，而**界面不该因此死掉**。理由写在 `_guard` 上。
            self._guard(self._sync_group_rows)
            self._guard(self._sync_freq_fields)
            self._guard(self._sync_counts)
            self._guard(self._sync_resources)
            self._guard(self._sync_extra_warning)
            self._guard(self._sync_groups_panel)
            self.refresh_tree()
            # ★ 必须在 `refresh_tree()` **之后**：判据是"表上有行、而现在的设定
            #   展不开"，前一半只有画完才知道。
            self._sync_runs_stale()
            self.update_status()
            self.sync_buttons()
        finally:
            self._syncing = False
        # 快照在 `_syncing` 放下**之后**：这一拍的界面到这里才算画完，
        # 而快照要问的正是"画完之后界面和模型是不是同一件事"。
        # 与上一拍完全相同的快照会被 `ActionTrace.state()` 折叠掉。
        self._trace_state()

    def _guard(self, step: object) -> None:
        """跑一步刷新，把 `EwaveBatchError` 转成状态栏上的一行字。

        为什么每一步各自过闸，而不是整个 `recompute()` 包一个 try：一步炸了不该把
        后面几步也吞掉 —— 那会让界面停在**半新半旧**的状态（表还是上一次的、
        计数已经变了），而这种不一致比一条错误消息难查得多。

        只吞 `EwaveBatchError`（我们自己的异常层次）。别的异常照抛 —— 那是真 bug，
        吞掉它等于把 bug 变成"界面偶尔不更新"。
        """
        try:
            step()  # type: ignore[operator]
        except EwaveBatchError as exc:
            message = f"{exc.__class__.__name__}: {exc}"
            # 🚨 进轨迹（`gui/trace.py`）：状态栏只留**最后一条**，而 `recompute()`
            #    一拍里过 8 道闸 —— 第一条错常常是真正的原因，而它在屏幕上活不过
            #    同一拍。用户报的"点了没反应"多半就是它。
            trace = getattr(self, "trace", None)
            if trace is not None:
                trace.error("_guard(%s)" % getattr(step, "__name__", step), exc)
            self._error = message if not self._error else self._error

    def _runs_are_stale(self) -> bool:
        """Runs 表现在画的是不是**上一次**的矩阵。

        两半缺一不可：这一轮 `plan()` 炸了（`_error` 非空），**并且**表上还有行
        （那些行只可能是上一次成功展开留下来的）。跑完一批之后界面本来就不再重新
        展开矩阵，但那条路上 `plan()` 根本没被调用 ⇒ `_error` 是空的 ⇒ 不会误报。
        """
        return bool(self._error) and bool(self.tree.get_children())

    def _sync_batch_root(self) -> None:
        """落点那一行：把 root 灌回输入框、显示算出来的绝对路径、指错了标红。"""
        root = getattr(self.bridge, "batch_root", "")
        if root and self.broot.get().strip() != root:
            # 与批次名同一条规矩：bridge 会把空值补成默认值，不灌回去的话输入框
            # 和真正用的落点会长期不一致，而"我的东西落在哪"正是这一行要回答的问题。
            self.broot.set(root)
        if self.broot_lbl is not None:
            self.broot_lbl.set_text("-> " + self.bridge.batch_dir())
        if self.broot_warn is None:
            return
        warn = getattr(self.bridge, "batch_root_warning", None)
        message = warn() if callable(warn) else ""
        if message:
            self.broot_warn.config(text="!  " + message)
            if not self.broot_warn.winfo_manager():
                self.broot_warn.pack(fill=tk.X, padx=8, pady=(0, 4))
        elif self.broot_warn.winfo_manager():
            self.broot_warn.pack_forget()

    def _sync_runs_stale(self) -> None:
        """Runs 表现在是不是旧的 —— 是就在表上面写明白（`RUNS_STALE_WARNING`）。

        判据两半，缺一不可：
        * `self._error` 非空 = 这一轮 `plan()` 炸了（`_guard` 把它变成了状态栏一行字）；
        * 表上还有行 = 那些行是上一次成功展开留下来的。

        跑完一批之后界面本来就**不再**重新展开矩阵（那会让 done/failed 消失），
        但那条路上 `plan()` 根本没被调用 ⇒ `_error` 是空的 ⇒ 这里不会误报。
        """
        if self.runs_stale is None:
            return
        blocked = bool(self._error)
        if self.empty_lbl is not None:
            self.empty_lbl.config(text=RUNS_EMPTY_BLOCKED if blocked else RUNS_EMPTY_HINT)
        stale = self._runs_are_stale()
        if stale:
            self.runs_stale.config(text=RUNS_STALE_WARNING)
            if not self.runs_stale.winfo_manager():
                # 每次 recompute 都 re-pack 会闪，只在没摆出来的时候摆一次。
                # `before=` 那个锚点是建界面时记下的第一个子件 —— 没有它的话
                # 重新 pack 会把这条红字放到**表的下面**，而它说的是上面那张表。
                anchor = self._runs_stale_anchor
                if anchor is not None:
                    self.runs_stale.pack(fill=tk.X, pady=(0, 4), before=anchor)
                else:  # pragma: no cover - 表还没建完
                    self.runs_stale.pack(fill=tk.X, pady=(0, 4))
        elif self.runs_stale.winfo_manager():
            self.runs_stale.pack_forget()

    def _sync_freq_fields(self) -> None:
        """扫描模式决定哪几个格子有意义 —— 其余置灰。

        编辑 base 之外的组时**整行置灰**：扫频没有按组覆盖这一说（`GROUP_ROW_AXES`），
        而这几个格子是直接写 base 的 —— 留着能编辑就等于"在 eqcur-off 组里改了扫频，
        结果基线跟着变了"，那是最难查的一类界面谎话。
        """
        live = self.bridge.sweep_live_fields()
        editable = self._active_is_base()
        for key, (label, entry) in self.freq_entries.items():
            on = editable and key in live
            # 没被选中的那一格**清空**，不只是置灰：留着一个灰掉但有值的 `points=41`
            # 会让人以为它还算数，而命令行里根本没有它。
            # 走 StringVar 而不是 `entry.delete()` —— 这一格上一轮多半已经是 disabled 的，
            # 对 disabled 的 Entry 做 delete 会被静默吃掉。
            #
            # 🚨 判据是 `key not in live`（**扫描模式**说它没用），不是 `not on`
            #    （那还含着"现在编辑的不是 base"）。用 `not on` 的后果是：切到任何一个
            #    组都会把 base 的 `step` / `points` 连同它的值一起抹掉 —— 那两个格子
            #    此刻只是置灰的 base 值，不是这个组的东西。下一次 `push()` 就把空串
            #    写回 base 的扫频，`sweep_axis_value` 当场抛 "'step' is selected but
            #    empty"，而用户什么都没改过。2026-08-20 用户报的两个 bug 都源于这里。
            if key in gui_state.SWEEP_SPACINGS and key not in live:
                self.freq_vars[key].set("")
            entry.config(state="normal" if on else "disabled")
            if isinstance(label, ttk.Radiobutton):
                # ttk.Radiobutton 没有 -foreground 选项（ttk 的前景色走 style），
                # 灰不灰只能用 state；拿 config(foreground=...) 招呼它会 TclError。
                label.state(["!disabled"] if editable else ["disabled"])
            else:
                label.config(foreground="#101010" if on else "#9c9c9c")
        if self.sw_combo is not None:
            self.sw_combo.config(state="readonly" if editable else "disabled")

    def _sync_counts(self) -> None:
        # ★ 批次名空着的时候 bridge 会现起一个 UTC 时间戳名。不灌回输入框的话，
        #   下一次 recompute（= 下一个键）又会生成一个新的 —— 批次目录每秒换一次，
        #   而界面上那行 Batch dir 就跟着跳，谁也不知道产物到底会落在哪。
        if self.bridge.batch_name and self.batch.get() != self.bridge.batch_name:
            self.batch.set(self.bridge.batch_name)
        self._sync_batch_root()
        counts = self.bridge.axis_counts()
        self.design_count.config(text="-> %d" % counts.get("design", 0))
        for label, key in (
            (self.cnt_corner, "corner"),
            (self.cnt_temp, "temperature"),
            (self.cnt_mode, "fullWave"),
            (self.cnt_freq, "freq"),
            (self.cnt_mesh, "mesh"),
        ):
            label.config(text="-> %d" % counts.get(key, 0))
        advanced = max(
            counts.get("equalCurrent", 1)
            * counts.get("relativeTolerance", 1)
            * counts.get("relativeCurrentTolerance", 1),
            1,
        )
        self.cnt_adv.config(text="-> %d" % advanced)
        eq = self.bridge.axis_selection().get("equalCurrent", ())
        summary = "equalCurrent %s - rtol %s - ictol %s" % (
            "+".join(eq) or "(none)",
            self.tol_r.get(),
            self.tol_c.get(),
        )
        extra_count = len(self.bridge.extra_flags())
        if extra_count:
            summary += "  -  +%d extra flag(s)" % extra_count
        self.adv_summary.config(text=summary)

        formula = self.bridge.formula()
        target = self._formula_target()
        if self.formula_lbl is not None:
            self.formula_lbl.config(text=formula if target == "total" else "")
        if self.bar_formula is not None:
            self.bar_formula.config(text=formula if target == "bar" else "")
        settings_box = getattr(self, "settings_box", None)
        if settings_box is not None:
            settings_box.config(text=self._settings_title(formula))
        self.on_counts(counts, self.bridge.run_count())

        batch_dir = self.bridge.batch_dir() + "/"
        for label in (self.batchdir_lbl, self.dir_lbl):
            if label is not None:
                label.set_text(batch_dir)

    def on_max_parallel(self) -> None:
        """「同时在飞」改了。**不走 `push()`** —— 那条路在批次跑起来之后是关着的。

        而这一格恰恰在跑起来之后最有用：4 个在跑、第 5 个在等名额，用户想让它也走。
        它不属于矩阵（run 的集合没变），所以不作废 plan、不动 driver、下一拍就生效。
        """
        raw = self.maxpar.get().strip()
        if not raw:
            return  # 正在删着打，别在半截上判他错
        try:
            actual = self.bridge.set_max_parallel(raw)
        except EwaveBatchError as exc:
            self.trace.error("set_max_parallel(%r)" % raw, exc)
            self._error = "%s: %s" % (exc.__class__.__name__, exc)
            self.update_status()
            return
        self.trace.note("max in flight", "%s -> %d" % (raw, actual))
        if str(actual) != raw:
            self.maxpar.set(str(actual))
        self.update_status()

    def _sync_resources(self) -> None:
        # ★ 第一次 plan 时 bridge 会从 `SiteFacts` 里学出整条 dsub 提交前缀 ——
        #   把它灌回输入框，用户才看得见"我们打算怎么提交"，也才改得动它
        #   （用户 2026-08-18 要求：整条命令原样暴露，不是只让改 `-R`）。
        if self.bridge.submit_command and self.dsub.get() != self.bridge.submit_command:
            self.dsub.set(self.bridge.submit_command)
        # 从 spec 里读进来的批次会带自己的 max_parallel —— 灌回输入框，
        # 否则界面显示 4 而真正放行的是别的数（同 `batch_root` 那条规矩）。
        current = str(self.bridge.options().max_parallel)
        if self.maxpar.get().strip() != current:
            self.maxpar.set(current)
        parallel = self.bridge.parallel()
        if parallel is None:
            self.par_lbl.config(
                text="-> ewave --parallel is not set (no cpu= in the submit command)"
            )
        else:
            self.par_lbl.config(text="-> ewave --parallel=%d  (follows cpu= 1:1)" % parallel)
        problem = self.bridge.submit_command_error()
        if problem:
            self.dsub_warn.config(text=problem)
            self.dsub_warn.pack(fill=tk.X, pady=(4, 0))
        else:
            self.dsub_warn.pack_forget()

    def _sync_extra_warning(self) -> None:
        """Extra flags 撞轴 → **标红**（§11 规则 2；这是原生 GUI 覆盖坑的根因）。"""
        message = self.bridge.conflict_message()
        if message:
            self.extra_warn.config(text="!  " + message)
            self.extra_warn.pack(anchor=tk.W, fill=tk.X, pady=(3, 0))
            self.extra_entry.config(foreground=RED)
        else:
            self.extra_warn.pack_forget()
            self.extra_entry.config(foreground="")

    def _sync_groups_panel(self) -> None:
        """重画组表 + 那条「加组会改掉基线的目录名」的红字。

        为什么这条警告必须显示出来：`<axes-slug>` 只编码「全批次在变」的轴，而组的取值
        也算在全批次里 ⇒ **加一个组会把基线自己的目录名也改掉**（`base/...` 变成
        `eqI-on__fw-off/...`）。这是正确且不可避免的（否则两个组的 55 度落进同一个目录
        = 静默覆盖 = 本工具存在的理由），但对一个已经跑过的批次来说，resume 靠 run_id
        对号，老目录当场就认不出来了。措辞由 bridge 给（`groups_change_warning()`），
        界面只负责让它看得见。
        """
        if self.gtree is None:
            return
        self.refresh_groups()
        if self.groups_warn is None:
            return
        message = self.bridge.groups_change_warning() if self.groups_ok else ""
        if message:
            self.groups_warn.config(text="!  " + message)
            self.groups_warn.pack(anchor=tk.W, fill=tk.X, pady=(4, 0))
        else:
            self.groups_warn.pack_forget()

    def refresh_tree(self) -> None:
        self._runs = self.bridge.runs()
        self.tree.delete(*self.tree.get_children())
        for index, run in enumerate(self._runs):
            self.tree.insert(
                "",
                tk.END,
                iid=run.run_id,
                values=(
                    index + 1,
                    run.design_key,
                    # `Run.group` 是出身标签，**不进 run_id**（进了就等于把跨组去重
                    # 取消掉）。老 batch.json 读回来时它可能是空的 —— 那就当 base。
                    _label(getattr(run, "group", "") or BASE_GROUP),
                    _label(run.axis_values.get("corner", "")),
                    _label(run.axis_values.get("temperature", "")),
                    _mode_text(run),
                    run.axes_slug,
                    run.status.value,
                    _wall_text(run),
                    _label(run.job.job_id if run.job is not None else ""),
                ),
                tags=(run.status.value,),
            )
        _fit_tree_columns(
            self.tree,
            tuple(col[0] for col in RUN_COLS),
            head_font=self.f_ui_b,
            cell_font=self.f_mono,
            floors={key: width for key, _head, width in RUN_COLS},
            caps=self._cap_px(RUN_COL_CAP_CHARS),
        )
        if self._runs:
            self.empty_lbl.place_forget()
        else:
            self.empty_lbl.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

    def _cmd_rows_within_budget(self, want: int) -> int:
        """把 Command 框想要的行数夹到「Runs 表还剩得下 `CMD_TREE_FLOOR_ROWS` 行」为止。

        2026-08-19 复验实拍（stacked，**默认字号**，1180x979 窗口）：点一条 run 看它的
        命令，Command 框从 2 行长到 8 行；而 stacked 的整棵树本来就比屏幕高
        （要 1107px、0.85 屏高只给得起 979px），多出来的 6 行**全部从 Runs 表身上扣** ——
        表从 87px 掉到 30px，一行 run 都看不见，连它自己的纵向滚动条都被裁掉一截。
        也就是说「点一条 run 看它的命令」这个动作会把你刚点的那张表弄没。
        split / tabbed 装得下，所以那两版一点事都没有 —— 这正是"上限写成常数"的破绽：
        同一个数字在一版是保护、在另一版是伤害。

        规矩两条：
        1. 只往**当前行数**上加表里真正富余出来的那几行，富余是负的就一行都不加；
        2. **不因为空间不够而缩**（缩由文本变短触发，那是另一回事）——
           每次选中都缩一下会让表格上下跳，理由同 `_fit_tree_columns` 的「只涨不缩」。

        表还没映射（`winfo_height() <= 1`，headless 冒烟就是这样）→ 原样返回；
        拿 1px 当"没地方"会让冒烟跑出一个和真界面不一样的形状。
        """
        tree = getattr(self, "tree", None)
        if tree is None:
            return want
        try:
            tree_px = tree.winfo_height()
            current = int(self.cmd_text.cget("height"))
        except (tk.TclError, ValueError):  # pragma: no cover - 窗口已经关掉的竞态
            return want
        if tree_px <= 1:
            return want
        line_px = max(1, self.f_mono.metrics("linespace") + 2)
        floor_px = CMD_TREE_FLOOR_ROWS * getattr(self, "runs_rowheight", 21)
        spare = (tree_px - floor_px) // line_px
        return max(CMD_ROWS_MIN, min(want, current + max(0, spare)))

    def show_detail(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        run_id = selection[0]
        self.out_var.set(_label(self.bridge.out_dir(run_id)))
        text = self.bridge.command_text(run_id) or _DASH
        self.cmd_text.config(state="normal")
        self.cmd_text.delete("1.0", tk.END)
        self.cmd_text.insert("1.0", text)
        # 随内容长高（C6）。原来恒占 4 行：还没选中 run 的时候那 4 行全是空的，
        # 而真选中了一条命令又有二十多行、4 行照样不够。上限 8 行是为了不把
        # Runs 表挤瘦 —— 再长的靠这个框自己的纵向滚动条。
        want = max(CMD_ROWS_MIN, min(CMD_ROWS_MAX, text.count("\n") + 1))
        rows = self._cmd_rows_within_budget(want)
        if int(self.cmd_text.cget("height")) != rows:
            self.cmd_text.config(height=rows)
        self.cmd_text.config(state="disabled")
        run = self.bridge.run(run_id)
        if self.detail_box is not None and run is not None:
            self.detail_box.config(text=" Selected run - %s  [%s] " % (run.run_id, run.status.value))

    def update_status(self) -> None:
        counts = self.bridge.summary()
        total = sum(counts.values())
        text = getattr(self, "_error", "") or self.bridge.status_line()
        note = self._last_event_text()
        if note and not getattr(self, "_error", ""):
            text = text + "  |  " + note
        # 结论要有**颜色**。2026-08-20 用户实测：dry-run 跑完，状态栏写的是
        # 「Finished - 0/3 done, 0 failed」，逐字都对，读起来却像"什么都没发生"——
        # 而它的真实含义是"3 条命令全拼出来了"。现在那句话本身改掉了
        # （`GuiState.status_line` 的 dry-run 分支），再给它一个绿/红，
        # 让"能不能跑"在**没读完那句话之前**就已经答完了。
        self.status_lbl.config(text=text, foreground=self._status_colour())
        right = "%d / %d done" % (counts["done"], total) if total else "0 / 0"
        self.status_right.config(text=right)
        if self.right_lbl is not None:
            self.right_lbl.config(text=right)
        header = self._runs_summary(counts, total)
        # 表是旧的时候，这行摘要里那个 run 数也是旧的 —— 它紧挨着 Total 那行现算的
        # `= 0 runs`，不标一下就是界面上最后一处自相矛盾的数字。判据与红字同源。
        if self._runs_are_stale():
            header += "  (stale)"
        if self.runs_header is not None:
            self.runs_header.config(text=header)
        elif self.runs_titled:
            self.runs_box.config(text=" Runs - %s " % header)
        if self.log_btn is not None:
            events = len(self.bridge.events())
            self.log_btn.config(text="Log (%d)" % events if events else "Log")
        self._log_refresh()

    def _status_colour(self) -> str:
        """状态栏那句话的颜色。红=有东西坏了，绿=可以提交了，灰=还没有结论。"""
        if getattr(self, "_error", ""):
            return RED
        result = self.bridge.dry_run_result()
        if result is not None:
            return RED if result[1] else GREEN
        counts = self.bridge.summary()
        if not self.bridge.is_running() and counts.get("failed"):
            return RED
        return HINT

    def _runs_summary(self, counts: dict[str, int], total: int) -> str:
        if not total:
            return "0 runs"
        # dry-run 跑完时**每个 run 都还是 `ready`**（它不提交、不建目录），于是
        # 下面那句通用的 "preview (not submitted)" 和"根本没按过 Dry-run"长得一模一样。
        # 而这两件事对用户的意义完全相反：一个是"还不知道能不能跑"，
        # 另一个是"命令已经全拼出来了，可以提交"。
        result = self.bridge.dry_run_result()
        if result is not None:
            built, failed = result
            if failed:
                return "%d runs - dry-run: %d commands built, %d failed" % (
                    total,
                    built,
                    failed,
                )
            return "%d runs - dry-run OK, commands built, nothing submitted" % total
        if not self.bridge.is_planned() or counts["ready"] == total:
            return "%d runs, preview (not submitted)" % total
        parts = [
            "%d %s" % (counts[name], name) for name in gui_state.STATUS_ORDER if counts[name]
        ]
        return "%d runs - %s" % (total, " - ".join(parts))

    def _last_event_text(self) -> str:
        """状态栏尾巴上那条「最后发生了什么」。**结果过期了就一个字都不说。**

        2026-08-20 实测：dry-run 跑完再改一个温度，状态栏成了
        「Preview up to date - 6 runs ready to submit  |  failed: ...」——
        前半句说的是新矩阵，后半句是上一份矩阵留下的尸体，而它们中间只隔一根竖线。
        `result_is_current()` 就是这两半共用的那个"说的还是同一件事吗"。
        """
        if not self.bridge.result_is_current():
            return ""
        events = self.bridge.events()
        if not events:
            return ""
        last = events[-1]
        return "%s: %s" % (last.kind.value, last.message)

    def sync_buttons(self) -> None:
        """按钮的可用性 —— 「正在跑」「真提交过」「没得跑」是**三件**事，别合成一个布尔。

        2026-08-20 用户报的坑就是把它们合成了一个：漏填官方 run 目录 -> dry-run 全
        failed -> Dry-run / Submit / Cancel 一起变灰，只剩一个 Resume 亮着，
        而 Resume 要读 dry-run 根本没写过的 batch.json。于是界面死在那儿，
        填好目录也没用（`recompute()` 那道闸门同时把矩阵冻住了）。

        现在的口径：

        | 按钮 | 什么时候能按 | 为什么 |
        |---|---|---|
        | Dry-run | 不在跑 且 有 run 且 没**真提交**过 | dry-run 过不算跑过，随便按 |
        | Submit  | 同上 | 真提交之后再按一次是整批从头重跑 |
        | Cancel  | 正在跑 | — |
        | Resume  | **真提交过** 且 不在跑 | 判据来自磁盘上的 batch.json，dry-run 没写过它 |

        「真提交过」= `bridge.has_submitted()`。它和 `has_started()` 的分家写在
        `gui.state.GuiState.has_submitted` 上。
        """
        running = self.bridge.is_running()
        submitted = self.bridge.has_submitted()
        # A7：一个 run 都没有时按下去会起一个**空批次**（建目录、写 batch.json、
        # 状态栏报 "0 runs"），而用户按它是因为以为自己配好了。没得跑就不许按。
        # `run_count()` 会因为设定不合法而抛 —— 那种时候同样不该能提交。
        empty = False
        if not running:
            # `run_count()` 每次都重算一遍笛卡尔积 —— 正在跑的时候没必要再问一次。
            # 设定不合法时 bridge 返回 0（它吞掉 `EwaveBatchError`），那种情况同样
            # 不该能提交，所以这里不需要区分"没配"和"配错了"。
            try:
                empty = self.bridge.run_count() == 0
            except EwaveBatchError:
                empty = True
        # ★ 关键的一个字：判据是 `has_submitted()` 而不是 `has_started()`。
        #   **dry-run 不算"跑过"** —— 它不提交 job、不建目录，重按一次代价是零。
        #   2026-08-20 用户报的正是这个：漏填官方 run 目录 -> dry-run 全 failed ->
        #   Dry-run 自己也变灰 -> 填好目录之后没有任何办法重新预览。
        #   反过来，**真提交之后两个都得关**：那时表上是真批次的状态，再按 Dry-run
        #   会拿同一份 state 重跑一遍预览、把真结果冲掉（补没成的走 Resume）。
        off = running or submitted or empty
        for name in ("Dry-run", "Submit"):
            self.btn[name].state(["disabled"] if off else ["!disabled"])
        self.btn["Cancel"].state(["!disabled"] if running else ["disabled"])
        # Resume 只在**真提交过**之后才有意义：它是从磁盘上的 batch.json 恢复的，
        # 而 dry-run 压根没写过那个文件（点下去只会得到一条读不到文件的报错）。
        self.btn["Resume"].state(["!disabled"] if (submitted and not running) else ["disabled"])

    def popup(self, event: object) -> None:
        iid = self.tree.identify_row(event.y)  # type: ignore[attr-defined]
        if iid:
            self.tree.selection_set(iid)
            self.menu.tk_popup(event.x_root, event.y_root)  # type: ignore[attr-defined]

    # ------------------------------------------------------ driver 的接线
    # ★ **GUI 用 `after()` 驱动同一个 `driver.tick()`**（BRIEF §12：单线程轮询）。
    #   这里没有线程、没有队列、没有第二份调度逻辑 —— 少一样都不行的那种"没有"。

    def _poll_ms(self) -> int:
        seconds = float(self.bridge.options().poll_interval)
        return max(50, int(seconds * 1000))

    def _pump(self) -> None:
        """一拍。driver 跑完 ⇒ 不再挂下一拍。"""
        self._timer = None
        report = self.bridge.tick()
        self.refresh_tree()
        self.update_status()
        self.sync_buttons()
        if report is not None and report.finished:
            self._batch_finished()
            return
        if report is not None:
            self._timer = self.frame.after(self._poll_ms(), self._pump)

    def _batch_finished(self) -> None:
        """一批跑完（或 dry-run 规划完）之后的收尾。

        ★ **dry-run 收尾时主动把 Log 窗口推到脸上。** dry-run 的全部产出就是那些
        命令，而它们一条都不在主界面上：状态栏只装得下最后一条，
        `Selected run -> Command` 一次只看得见一个 run。用户 2026-08-20 的原话是
        「点击 dry run 之后，我也不知道到底可以跑了不」—— 真正的答案
        （4 条命令都拼出来了 / 第 2 条为什么拼不出来）一直躺在事件流里，
        只是界面上没有地方显示它。

        **真提交的批次不弹**：那时表格自己就是进度，弹一扇窗只会挡着它。
        要看日志按状态栏上的 Log。
        """
        if self.bridge.dry_run_result() is None:
            self._log_refresh()
            return
        self.show_log()

    def show_log(self) -> object:
        """打开（或前置）Log 窗口。已经开着就只刷新 + 提到最前，**不开第二扇**。"""
        if self._log is None or not self._log.alive():
            self._log = _LogWindow(self)
        self._log.refresh(force=True)
        self._log.present()
        return self._log

    def _log_refresh(self) -> None:
        """Log 窗口开着就跟着刷一下；没开就什么都不做（不为了刷新去开窗）。"""
        if self._log is not None and self._log.alive():
            self._log.refresh()

    def show_trace(self) -> object:
        """打开（或前置）Developer log 窗口。同 `show_log`：**最多一扇**。"""
        if self._trace_win is None or not self._trace_win.alive():
            self._trace_win = _TraceWindow(self)
        self._trace_win.refresh(force=True)
        self._trace_win.present()
        return self._trace_win

    def _stop_timer(self) -> None:
        if self._timer is not None:
            try:
                self.frame.after_cancel(self._timer)
            except tk.TclError:  # pragma: no cover - 窗口已经没了
                pass
            self._timer = None

    def _preflight_blocks(self, what: str) -> bool:
        """按下去之前先问一句"现在跑得成吗"。挡住了就弹框说清楚并返回 True。

        为什么值得多这一步：2026-08-20 用户漏填了官方 run 目录就按 Dry-run，
        结果是**一表 failed** 加一句面向实现者的报错
        （`SiteFacts.ewave_bin is empty - no idea which ewave to execute`）——
        6 个 run 报的是同一件事，而真正要做的只是把顶上那一格填了。
        与其让他从 6 条一模一样的失败里反推，不如在按下去的那一刻就说清楚，
        而且**不起 driver** —— 不起就不会留下一批假的 failed 结果。
        """
        problems = self.bridge.preflight()
        if not problems:
            return False
        _error("Cannot %s yet" % what, (_NL + _NL).join(problems))
        return True

    def do_submit(self) -> None:
        self.recompute()
        if self._preflight_blocks("submit"):
            return
        try:
            self.bridge.start(dry_run=False)
        except EwaveBatchError as exc:
            _error("Cannot submit", str(exc))
            return
        self._stop_timer()
        self._pump()

    def do_dry_run(self) -> None:
        """只拼命令、不提交（D8）。走的是**同一个 driver**，只是 options.dry_run=True。"""
        self.recompute()
        if self._preflight_blocks("dry-run"):
            return
        try:
            self.bridge.start(dry_run=True)
        except EwaveBatchError as exc:
            _error("Cannot dry-run", str(exc))
            return
        self._stop_timer()
        self._pump()

    def do_cancel(self) -> None:
        self._stop_timer()
        self.bridge.cancel()
        self.refresh_tree()
        self.update_status()
        self.sync_buttons()

    def do_resume(self) -> None:
        """只补没成的（D7）。判据来自 `batch.json` + 磁盘上的产物，不是内存里的表。"""
        try:
            self.bridge.resume()
        except EwaveBatchError as exc:
            _error("Cannot resume", str(exc))
            return
        self._stop_timer()
        self._pump()

    def do_new_batch(self) -> None:
        """丢掉上一次的结果，回到"可以改设定、可以提交"的状态。

        单独一个动作是必须的：跑过之后界面**不再**跟着勾选重新展开矩阵
        （否则那些 done / failed 会静默消失），所以要有一个明确的"我要重来一批"。
        """
        self.bridge.reset()
        self._stop_timer()
        self.recompute()

    # ----------------------------------------------------------- 菜单动作
    def _ask_text(
        self,
        title: str,
        label: str,
        initial: str,
        hint: str = "",
        *,
        on_empty: str | None = None,
        on_smoke: str | None = None,
    ) -> str | None:
        """一个输入框的模态小对话框。取消 -> None。

        `on_empty` = 用户把输入框清空后按 OK 时返回什么。默认 `None`（= 当成取消，
        这是本方法原来的唯一行为）。给了值就是"留空 = 用这个"—— 建组那两个框要的
        就是这个手感：建议名摆在那儿，直接回车即可。

        `on_smoke` = `EWB_SMOKE=1` 时返回什么，默认 `None`。冒烟不能进
        `wait_window()`（会一直等下去），但**返回 None 等于"用户按了取消"**，
        于是整条动作在 headless 下什么都不做、也就测不到 —— 建组那两个框把它设成
        建议名，headless 下就等价于"用户接受了建议"，动作序列测试才走得下去。
        """
        if smoke_enabled():
            return on_smoke
        dlg = tk.Toplevel(self.top)
        dlg.title(title)
        dlg.transient(self.top)
        dlg.columnconfigure(1, weight=1)
        var = tk.StringVar(value=initial)
        ttk.Label(dlg, text=label).grid(row=0, column=0, sticky=tk.W, padx=8, pady=6)
        entry = ttk.Entry(dlg, textvariable=var, width=32, font=self.f_mono)
        entry.grid(row=0, column=1, sticky="ew", padx=8, pady=6)
        if hint:
            ttk.Label(dlg, text=hint, style="Hint.TLabel", wraplength=340, justify=tk.LEFT).grid(
                row=1, column=0, columnspan=2, sticky=tk.W, padx=8
            )
        answer: dict[str, str | None] = {"value": None}

        def ok(_event: object = None) -> None:
            answer["value"] = var.get().strip() or on_empty
            dlg.destroy()

        def cancel(_event: object = None) -> None:
            dlg.destroy()

        bar = ttk.Frame(dlg)
        bar.grid(row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=8)
        ttk.Button(bar, text="Cancel", width=9, command=cancel).pack(side=tk.RIGHT)
        ttk.Button(bar, text="OK", width=9, command=ok).pack(side=tk.RIGHT, padx=(0, 6))
        dlg.bind("<Return>", ok)
        dlg.bind("<Escape>", cancel)
        entry.focus_set()
        entry.selection_range(0, tk.END)
        _center_on_parent(dlg, self.top)
        dlg.grab_set()
        self.top.wait_window(dlg)  # type: ignore[attr-defined]
        return answer["value"]

    def do_rename_batch(self) -> None:
        """改批次名。**跑过之后不许改** —— 名字就是目录名。

        改一个已经落过盘的批次的名字，等于让界面指向一个空目录，而磁盘上那批产物
        还在老名字底下、resume 也跟着找不着。要另起一个名字走 Duplicate batch。

        ⚠️ 判据是 `has_submitted()` 而**不是** `has_started()`（2026-08-20 一并订正）：
        dry-run 也算 `has_started()`，于是"按一次 Dry-run 就再也改不了批次名"，
        而且拦下来时说的是「This batch has already been submitted」——
        一句**假话**（dry-run 一个字节都没写）。这和按钮那边是同一条口径，
        理由写在 `gui.state.GuiState.has_submitted` 上。
        """
        if self.bridge.has_submitted():
            _error(
                "Cannot rename this batch",
                "This batch has already been submitted, and its name is its directory "
                "name on disk.\nUse Batch -> Duplicate batch... to start a new one, or "
                "File -> New batch.",
            )
            return
        name = self._ask_text(
            "Rename batch",
            "Batch name",
            self.batch.get(),
            "This is the directory name under the batch root. Leave it empty to let the "
            "tool pick a UTC timestamp.",
        )
        if name is None:
            return
        self.batch.set(name)
        self.recompute()

    def do_duplicate_batch(self) -> None:
        """同样的设定、新的名字、新的目录，**结果不带过去**。

        与 `New batch` 的区别只有一个：那个保留名字（于是接着往同一个目录里写），
        这个换一个名字。想拿一批跑完的设定再跑一遍（换个 corner、改个 mesh）时，
        它是唯一不会覆盖上一批产物的路。
        """
        suggested = (self.batch.get().strip() or "batch") + "-copy"
        name = self._ask_text(
            "Duplicate batch",
            "New batch name",
            suggested,
            "Every setting on this window is kept; the results of the current batch are "
            "not carried over.",
        )
        if name is None:
            return
        self.bridge.reset()
        self._stop_timer()
        self.batch.set(name)
        self.recompute()

    def do_open_spec(self) -> None:
        path = filedialog.askopenfilename(
            title="Open batch spec",
            filetypes=[("Spec files", "*.yaml *.yml *.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            self.bridge.load_spec(path)
        except EwaveBatchError as exc:
            _error("Cannot read spec", str(exc))
            return
        self._init_vars_from_bridge()
        self.refresh_designs()
        self.recompute()

    def _apply_axis_selection(
        self, selection: dict, only: Sequence[str] | None = None
    ) -> None:
        """轴的取值 → 界面变量。`only` 限定只灌 `GROUP_ROW_AXES` 的哪几行。

        `only` 存在的理由是"继承"：编辑一个组时，没被覆盖的那几行要一直显示 base 的值，
        而被覆盖的那几行**不能**被重灌（用户正在里面打字）。整份重灌是 load_spec /
        切组时才做的事（那时候界面上的值本来就该整个换掉）。
        """

        def want(key: str) -> bool:
            return only is None or key in only

        if want("corner"):
            for name, var in self.corner_vars.items():
                var.set(name in selection.get("corner", ()))
        if want("temperature"):
            self.temp.set(", ".join(selection.get("temperature", ())))
        if want("fullWave"):
            modes = selection.get("fullWave", ())
            self.mode_vars["Quasi-static"].set("off" in modes)
            self.mode_vars["Full wave"].set("on" in modes)
        if want("mesh"):
            mesh = (selection.get("mesh") or ("0.4",))[0].split(gui_state.MESH_SEP)
            if len(mesh) == 1:
                mesh = mesh * 3
            self.m_edge.set(mesh[0])
            self.m_vert.set(mesh[1])
            self.m_via.set(mesh[2])
        if want("advanced"):
            eq = selection.get("equalCurrent", ())
            self.eq_on.set("on" in eq)
            self.eq_off.set("off" in eq)
            self.tol_r.set((selection.get("relativeTolerance") or ("",))[0])
            self.tol_c.set((selection.get("relativeCurrentTolerance") or ("",))[0])

    def _init_vars_from_bridge(self) -> None:
        """load_spec 之后把界面变量重新灌一遍（界面要显示文件里的东西）。"""
        sweep = self.bridge.sweep()
        self._apply_axis_selection(self.bridge.axis_selection())
        self.sw_mode.set(sweep.get("mode", "adaptive"))
        self.sw_spacing.set(sweep.get("spacing", "step"))
        self.f_start.set(sweep.get("start", ""))
        self.f_stop.set(sweep.get("stop", ""))
        self.f_step.set(sweep.get("step", ""))
        self.f_pts.set(sweep.get("points", ""))
        self.extra.set(self.bridge.extra_flags_text())
        self.batch.set(self.bridge.batch_name)
        self.offdir.set(self.bridge.official_run_dir)
        self.dsub.set(self.bridge.submit_command)
        # spec 文件里的组也要显示出来 —— 组表不刷新的话，读进来的组只在核心里存在，
        # 而界面上看起来这份 spec 一个组都没有（然后用户按 New batch 把它们丢了）。
        self._sync_override_vars()
        self.refresh_groups()

    def do_save_spec(self) -> None:
        """把界面上当前的设定写成一份 spec 文件 —— 这就是本工具的「工程文件」。

        `batch.json` 存的是**跑起来之后**的状态（resume 用），存不了「我打算怎么跑」。
        所以关掉窗口前想留住勾选，只有这一条路。
        """
        suggested = (self.bridge.batch_name or "batch") + (
            ".yaml" if spec_module.have_yaml() else ".json"
        )
        path = filedialog.asksaveasfilename(
            title="Save batch spec as",
            initialfile=suggested,
            defaultextension=".yaml" if spec_module.have_yaml() else ".json",
            filetypes=[("Spec files", "*.yaml *.yml *.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            written = spec_module.save_spec(self.bridge.spec_snapshot(), path)
        except (EwaveBatchError, OSError) as exc:
            _error("Cannot save spec", str(exc))
            return
        # 显示**真正写到的**路径：没装 PyYAML 时 save_spec 会把 .yaml 换成 .json，
        # 否则用户会拿着一个自己打不开的文件（load_spec 按扩展名选解析器）。
        self.status_lbl.config(text="Saved spec: %s" % written)

    def do_pick_batch_root(self) -> None:
        """选落点。**不建目录** —— 真正建它的是第一次 plan/提交。"""
        path = filedialog.askdirectory(title="Pick where batches should be written")
        if path:
            self.broot.set(path)
            self.recompute()

    def do_pick_offdir(self) -> None:
        path = filedialog.askdirectory(title="Pick an official run dir (contains gdsout_setup)")
        if path:
            self.offdir.set(path)
            self.recompute()

    def _reveal(self, what: str, path: str) -> str:
        """真去开一个文件管理器；开不了才退回那个只写着路径的对话框。

        原来这两个按钮**只**弹一个写着路径的 messagebox —— 那不是"打开目录"，
        那是"把路径念给你听"。红区是 Linux，所以 `xdg-open` 那条路必须有，
        只做 Windows 的 `os.startfile` 等于这个按钮在唯一要用它的机器上是死的。
        """
        problem = _open_in_file_manager(path)
        if problem:
            _info(what, "%s\n\n(could not open a file manager: %s)" % (path or _DASH, problem))
        return problem

    def do_open_batch_dir(self) -> None:
        self._reveal("Batch dir", self.bridge.batch_dir())

    def do_exit(self) -> None:
        self._stop_timer()
        try:
            self.top.destroy()  # type: ignore[attr-defined]
        except tk.TclError:  # pragma: no cover
            pass

    def on_row_action(self, action: str) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        run_id = selection[0]
        if action == "Copy command":
            text = self.bridge.command_text(run_id)
            try:
                self.top.clipboard_clear()  # type: ignore[attr-defined]
                self.top.clipboard_append(text)  # type: ignore[attr-defined]
            except tk.TclError:  # pragma: no cover
                pass
            return
        if action == "Open output dir":
            self._reveal("Output dir", self.bridge.out_dir(run_id))
            return

    DEFAULTS_COLS: tuple[tuple[str, str, int], ...] = (
        ("flag", "Flag", 200),
        ("value", "Value", 110),
        ("src", "Where the default came from", 330),
    )
    """`Extraction defaults` 那张表的三列。第三个数是下限，真宽度按内容现算。"""

    DEFAULTS_ROWS_MAX = 16
    """表最多显示几行 —— 再多靠纵向滚动条。默认表会随 PDK 长，不封顶就会长出屏幕。"""

    def show_defaults(self) -> None:
        """第 2 层：有默认值、不上主界面的 flag。一个对话框，主界面零成本（§11）。

        2026-08-19 之前这个框**自称可编辑其实只读**：正文写着 "Editing here applies
        to the whole batch"，而 `bridge.set_default_override()` 在整个 `gui/` 里一次
        都没被调用过。现在双击 Value 单元格就地改，清空 = 撤销这一条覆盖
        （bridge 那边本来就是这么定义的）。

        另外三样也在这一趟里：三列全按内容算宽（原来 `--relativeCurrentToler`、
        `(explicitly`、`builtin fallback (no official run di` 全被切掉，而且没有滚动条
        够不着）、补纵横两条滚动条、居中于父窗口（原来直接落到屏幕左边界外，B5）。
        """
        dlg = tk.Toplevel(self.top)
        dlg.title("Extraction defaults")
        dlg.transient(self.top)
        ttk.Label(
            dlg,
            padding=8,
            style="Hint.TLabel",
            justify=tk.LEFT,
            text=(
                "These flags have defaults and do not take up room on the main window.\n"
                "The values are NOT hard-coded: they are learned from the official run\n"
                "dir the first time a batch is planned, so they follow the PDK version.\n"
                "Double-click a Value to change it for the whole batch; clear it to drop\n"
                "the override and go back to the learned value. To change a single run\n"
                "use Extra ewave flags under Advanced."
            ),
        ).pack(anchor=tk.W)

        wrap = ttk.Frame(dlg)
        wrap.pack(fill=tk.BOTH, expand=True, padx=8)
        columns = tuple(col[0] for col in self.DEFAULTS_COLS)
        tree = ttk.Treeview(
            wrap,
            columns=columns,
            show="headings",
            height=min(max(len(self.bridge.defaults_table()) + 1, 6), self.DEFAULTS_ROWS_MAX),
            style="Designs.Treeview",
            selectmode="browse",
        )
        for key, head, width in self.DEFAULTS_COLS:
            tree.heading(key, text=head)
            tree.column(key, width=width, anchor=tk.W, stretch=(key == "src"))
        _scrolled_tree_grid(wrap, tree)

        def reload_rows() -> None:
            """重画整张表。改一格会同时改掉那一行的第三列（"哪来的"），所以整行重画。"""
            keep = tree.selection()
            tree.delete(*tree.get_children())
            for row in self.bridge.defaults_table():
                tree.insert("", tk.END, iid=row[0], values=row)
            for iid in keep:
                if tree.exists(iid):
                    tree.selection_set(iid)
            _fit_tree_columns(
                tree,
                columns,
                head_font=self.f_ui_b,
                cell_font=self.f_mono,
                floors={key: width for key, _head, width in self.DEFAULTS_COLS},
            )

        def start_edit(event: object) -> None:
            """双击 Value 那一列 -> 把一个 Entry 盖在那个单元格上就地编辑。

            只认 `#2`（Value）。flag 名不许改 —— 改了就不是"覆盖这个默认值"而是
            "凭空多一个 flag"，那条路是主界面上的 Extra ewave flags，有冲突检查。
            """
            iid = tree.identify_row(event.y)  # type: ignore[attr-defined]
            column = tree.identify_column(event.x)  # type: ignore[attr-defined]
            if not iid or column != "#2":
                return
            cell = tree.bbox(iid, column)
            if not cell:  # 行被滚出了可视区
                return
            x, y, width, height = cell
            var = tk.StringVar(value=tree.set(iid, "value"))
            editor = ttk.Entry(tree, textvariable=var, font=self.f_mono)
            editor.place(x=x, y=y, width=width, height=height)
            editor.focus_set()
            editor.selection_range(0, tk.END)
            done = {"yes": False}

            def finish(commit: bool) -> None:
                # <FocusOut> 和 <Return> 会一前一后都到，Escape 之后 <FocusOut> 也会到。
                # 没有这个闩就是往一个已经 destroy 掉的控件上再写一次（TclError）。
                if done["yes"]:
                    return
                done["yes"] = True
                value = var.get()
                editor.destroy()
                if not commit:
                    return
                try:
                    self.bridge.set_default_override(tree.set(iid, "flag"), value)
                except EwaveBatchError as exc:
                    _error("Cannot change this default", str(exc))
                    return
                reload_rows()
                self.recompute()

            editor.bind("<Return>", lambda _e: finish(True))
            editor.bind("<FocusOut>", lambda _e: finish(True))
            editor.bind("<Escape>", lambda _e: finish(False))

        tree.bind("<Double-1>", start_edit)
        reload_rows()

        ttk.Label(
            dlg,
            padding=8,
            style="Off.TLabel",
            justify=tk.LEFT,
            wraplength=620,
            text=(
                "locked (never shown on the main window - changing them breaks the tool's "
                "own mechanism):  " + "  ".join(self.bridge.locked_flags())
            ),
        ).pack(anchor=tk.W)

        bar = ttk.Frame(dlg, padding=8)
        bar.pack(fill=tk.X)
        ttk.Button(
            bar,
            text="Reset to learned values",
            command=lambda: (self.bridge.reset_defaults(), reload_rows(), self.recompute()),
        ).pack(side=tk.LEFT)
        ttk.Button(bar, text="Close", command=dlg.destroy).pack(side=tk.RIGHT)
        _center_on_parent(dlg, self.top)
        if not smoke_enabled():
            dlg.grab_set()

    def show_about(self) -> None:
        _info(
            "About",
            "eWave Batch - batch driver around the official eWave GUI.\n"
            "One matrix of extraction settings, one run per combination, "
            "results archived per run.",
        )

    # ------------------------------------------------------------- subclass
    def layout(self) -> None:
        """子类把 section 摆进 `self.frame`。"""
        raise NotImplementedError

    def on_counts(self, counts: dict[str, int], total: int) -> None:
        """tabbed 版用它更新那个独立的 Run count 面板。默认什么都不做。"""


# --------------------------------------------------------------------------
# 模块级小工具
# --------------------------------------------------------------------------


def _set_enabled(widget: object, on: bool) -> None:
    """把一棵子树整体置灰/放开（"这一行继承 base" 的可视化）。

    逐个 `configure(state=…)` 而不是 `state([...])`：容器（`ttk.Frame`）和分隔线根本
    没有这个选项，`TclError` 是**正常路径**而不是异常情况。组合框要单独处理 —— 给它
    `normal` 会把只读的下拉框变成可自由输入的，那是另一个 bug。
    """
    for child in widget.winfo_children():  # type: ignore[attr-defined]
        try:
            if isinstance(child, ttk.Combobox):
                child.configure(state="readonly" if on else "disabled")
            else:
                child.configure(state="normal" if on else "disabled")
        except tk.TclError:
            pass
        _set_enabled(child, on)


def _mode_text(run: Run) -> str:
    """`fullWave` 轴的取值 → 人话。轴没扫这一根时留占位符。"""
    value = run.axis_values.get("fullWave", "")
    if value == "on":
        return "full wave"
    if value == "off":
        return "quasi-static"
    return _DASH


def _wall_text(run: Run) -> str:
    if run.wall_seconds is None:
        return _DASH
    seconds = int(run.wall_seconds)
    return "%d:%02d" % (seconds // 60, seconds % 60)


_DIALOG_TRACE: ActionTrace | None = None
"""当前那扇窗的轨迹（`_error` / `_info` 是模块级函数，拿不到 `self`）。

为什么弹框必须进轨迹：用户报 bug 时说的就是弹框上那句话，而弹框是**模态**的 ——
它一关就什么都不剩。`EWB_SMOKE=1` 下弹框根本不建，那时轨迹是**唯一**的痕迹，
`tests/test_gui_trace.py` 靠它验"这一步确实拦下来了"。
"""


def _dialog(kind: str, title: str, message: str) -> None:
    """记一条弹框。窗口还没建 / 已经拆了 -> 什么都不做。"""
    if _DIALOG_TRACE is not None:
        _DIALOG_TRACE.note("dialog[%s] %s" % (kind, title), message)


def _info(title: str, message: str) -> None:
    _dialog("info", title, message)
    if smoke_enabled():
        return
    messagebox.showinfo(title, message)


def _error(title: str, message: str) -> None:
    _dialog("error", title, message)
    if smoke_enabled():
        return
    messagebox.showerror(title, message)


def _add_menu_item(menu: object, label: str, command: object) -> None:
    """加一条菜单项。**没有 handler 就置灰并写明白**（E1/E2）。

    在这之前，接不上的那六项（Duplicate batch / Rename / Re-run failed only /
    Check environment / Re-run this one / Set as current）点下去会弹一个写着
    "Not wired yet - this will: ..." 的对话框。一个能按的菜单项就是一句"这件事我
    能做"的承诺，而它做不到 —— 用户得按一次才知道，而且下次还会再按一次。
    """
    if command is None or label in DISABLED_MENU_ITEMS:
        menu.add_command(  # type: ignore[attr-defined]
            label=label + NOT_IMPLEMENTED_SUFFIX, state=tk.DISABLED
        )
        return
    menu.add_command(label=label, command=command)  # type: ignore[attr-defined]


def build(app_cls: type, parent: object, bridge: object) -> object:
    """`build_frame` 的共用实现：建 app、把它挂在 frame 上、返回 frame。

    ⚠️ `frame._ewb_app = app` **不是装饰**：app 持有全部 `tk.StringVar`，
    Python 侧一旦没人引用它，那些变量会被 GC 掉，而 Tk 侧的控件还指着它们 ——
    症状是"输入框忽然清空/不响应"，且完全没有异常。
    """
    app = app_cls(parent, bridge)
    app.frame._ewb_app = app
    return app.frame


def frame_main(layout_name: str, argv: Sequence[str] | None = None) -> int:
    """`gui.frames.*.main` 的共用实现 —— 直接把这一版交给 `gui.app.launch`。"""
    from ewave_batch._stdio import ascii_safe_stdio

    ascii_safe_stdio()
    from .app import launch

    if argv:
        from .app import build_parser

        args = build_parser().parse_args(list(argv))
        return launch(args.layout)
    return launch(layout_name)
