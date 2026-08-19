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
import tkinter as tk
from collections.abc import Sequence
from tkinter import filedialog, font as tkfont, messagebox, ttk

from ewave_batch.model import EwaveBatchError, Run

from . import state as gui_state
from .app import SMOKE_ENV, smoke_enabled

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
    ("n", "#", 40),
    ("design", "Design", 150),
    ("corner", "Corner", 78),
    ("temp", "Temp", 60),
    ("mode", "Mode", 92),
    ("extra", "Extra axes", 150),
    ("status", "Status", 86),
    ("wall", "Wall time", 74),
    ("job", "Job id", 84),
)

MENU_ITEMS: tuple[str, ...] = (
    "Open output dir",
    "Copy command",
    "-",
    "Re-run this one",
    "Set as current",
)
"""右键菜单。「Open log」在草图里也有，这里合进 `Open output dir`
（日志就在那个目录里，多一条只是多一次会走空的路径）。"""

_DASH = "-"
"""空值的占位符。**用 ASCII 的连字符**，不是 em dash —— 红区 `LANG` 常是 `C`，
界面字符串走的又是 Tk 不是 stdout，一个非 ASCII 字符在那边多半渲染成方块。"""


def _label(text: str) -> str:
    """空字符串 → 占位符。"""
    return text if text else _DASH


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
        self.show_formula_in_title = False
        self._runs: tuple[Run, ...] = ()

        try:
            self.top.geometry(self.GEOMETRY)  # type: ignore[attr-defined]
        except tk.TclError:  # pragma: no cover - parent 不是窗口时无所谓
            pass

        self._init_vars()
        self._init_style()
        self.build_menubar()
        self.layout()
        self.recompute()

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
        self.dsub = tk.StringVar(value=self.bridge.submit_command)
        self.extra = tk.StringVar(value=self.bridge.extra_flags_text())
        self.batch = tk.StringVar(value=self.bridge.batch_name)
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

        st = ttk.Style()
        st.configure("Runs.Treeview", font=self.f_mono, rowheight=21)
        st.configure("Runs.Treeview.Heading", font=self.f_ui_b)
        st.configure("Designs.Treeview", font=self.f_mono, rowheight=20)
        st.configure("Designs.Treeview.Heading", font=self.f_ui_b)
        st.configure("Count.TLabel", font=self.f_mono, foreground=BLUE)
        st.configure("Off.TLabel", font=self.f_mono, foreground=GREY)
        st.configure("Hint.TLabel", font=self.f_ui, foreground=HINT)
        st.configure("Green.TLabel", font=self.f_mono, foreground=GREEN)
        st.configure("Warn.TLabel", font=self.f_ui, foreground=RED)
        st.configure("Mono.TLabel", font=self.f_mono)
        st.configure("Accent.TButton", font=self.f_ui_b)

    # -------------------------------------------------------------- menubar
    def build_menubar(self) -> None:
        """菜单挂在**顶层窗口**上。parent 不是窗口（被嵌进别的界面）时静默跳过。"""
        bar = tk.Menu(self.top)
        actions: dict[str, object] = {
            "New batch": self.do_new_batch,
            "Open spec...": self.do_open_spec,
            "Save spec as...": self.do_save_spec,
            "Exit": self.do_exit,
            "Open batch dir": self.do_open_batch_dir,
            "Dry-run": self.do_dry_run,
            "Submit": self.do_submit,
            "Cancel": self.do_cancel,
            "Resume": self.do_resume,
            "Extraction defaults...": self.show_defaults,
            "About": self.show_about,
        }
        for name, items in (
            ("File", ("New batch", "Open spec...", "Save spec as...", "-", "Exit")),
            ("Batch", ("Duplicate batch...", "Rename...", "-", "Open batch dir")),
            ("Runs", ("Dry-run", "Submit", "Cancel", "Resume", "-", "Re-run failed only")),
            ("Tools", ("Extraction defaults...", "Check environment (doctor)")),
            ("Help", ("About",)),
        ):
            menu = tk.Menu(bar, tearoff=0)
            for item in items:
                if item == "-":
                    menu.add_separator()
                    continue
                handler = actions.get(item)
                if handler is None:
                    menu.add_command(label=item, command=lambda t=item: _todo(t))
                else:
                    menu.add_command(label=item, command=handler)  # type: ignore[arg-type]
            bar.add_cascade(label=name, menu=menu)
        try:
            self.top.config(menu=bar)  # type: ignore[attr-defined]
        except tk.TclError:  # pragma: no cover - 嵌进非窗口容器时
            pass

    # ------------------------------------------------------------ batch bar
    def build_batchbar(self, parent: object, show_dir: bool = False) -> ttk.Frame:
        f = ttk.Frame(parent, padding=(8, 6))  # type: ignore[arg-type]
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
        ttk.Label(
            f,
            text="site coordinates are parsed from it, never typed",
            style="Hint.TLabel",
        ).pack(side=tk.LEFT, padx=8)

        if show_dir:
            self.dir_lbl = ttk.Label(f, text="", style="Mono.TLabel")
            self.dir_lbl.pack(side=tk.RIGHT)
        return f

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

        self.dtree = ttk.Treeview(
            inner,
            columns=("lib", "cell", "view"),
            show="headings",
            height=rows,
            style="Designs.Treeview",
            selectmode="browse",
        )
        for (key, head), width in zip(
            (("lib", "Library"), ("cell", "Cell"), ("view", "View")), widths
        ):
            self.dtree.heading(key, text=head)
            self.dtree.column(key, width=width, anchor=tk.W, stretch=(key == "cell"))
        self.dtree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

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
        self.dtree.delete(*self.dtree.get_children())
        for row in self.bridge.design_rows():
            self.dtree.insert("", tk.END, values=row)

    def add_design(self) -> None:
        """三个输入框的小对话框。三元组必须齐 —— **view 不是常量**（BRIEF §5）。"""
        dlg = tk.Toplevel(self.top)
        dlg.title("Add design")
        dlg.transient(self.top)
        variables: list[tk.StringVar] = []
        for index, label in enumerate(("Library", "Cell", "View")):
            ttk.Label(dlg, text=label).grid(row=index, column=0, sticky=tk.W, padx=8, pady=4)
            var = tk.StringVar()
            ttk.Entry(dlg, textvariable=var, width=30, font=self.f_mono).grid(
                row=index, column=1, padx=8, pady=4
            )
            variables.append(var)
        ttk.Label(
            dlg,
            text="All three are required; the view is not a constant.",
            style="Hint.TLabel",
        ).grid(row=3, column=0, columnspan=2, sticky=tk.W, padx=8)

        def ok() -> None:
            values = tuple(v.get().strip() for v in variables)
            if all(values):
                self.bridge.add_design(*values)
                self.refresh_designs()
                self.recompute()
            dlg.destroy()

        ttk.Button(dlg, text="OK", command=ok).grid(row=4, column=1, sticky=tk.E, padx=8, pady=8)
        if not smoke_enabled():
            dlg.grab_set()

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

    # ------------------------------------------------------------- settings
    def _srow(self, parent: object, row: int, label: str, width: int = 14) -> tuple[ttk.Frame, ttk.Label]:
        ttk.Label(parent, text=label, width=width, anchor=tk.W).grid(  # type: ignore[arg-type]
            row=row, column=0, sticky=tk.W, pady=2
        )
        box = ttk.Frame(parent)  # type: ignore[arg-type]
        box.grid(row=row, column=1, sticky=tk.W)
        count = ttk.Label(parent, text="-> 1", style="Count.TLabel", anchor=tk.E, width=6)  # type: ignore[arg-type]
        count.grid(row=row, column=2, sticky=tk.E, padx=(10, 0))
        ttk.Separator(parent, orient=tk.HORIZONTAL).grid(  # type: ignore[arg-type]
            row=row, column=0, columnspan=3, sticky="sew"
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
        self.show_formula_in_title = show_formula
        grid = ttk.Frame(box)
        grid.pack(fill=tk.X)
        grid.columnconfigure(1, weight=1)
        lw = 11 if compact else 15

        box_c, self.cnt_corner = self._srow(grid, 0, "Corner", lw)
        for name in gui_state.CORNER_VALUES:
            ttk.Checkbutton(
                box_c, text=name, variable=self.corner_vars[name], command=self.recompute
            ).pack(side=tk.LEFT, padx=(0, 6 if compact else 11))

        box_t, self.cnt_temp = self._srow(grid, 1, "Temp" if compact else "Temperature", lw)
        entry = ttk.Entry(box_t, textvariable=self.temp, font=self.f_mono, width=20 if compact else 32)
        entry.pack(side=tk.LEFT)
        entry.bind("<KeyRelease>", lambda _e: self.recompute())
        ttk.Label(
            box_t,
            text="degC, comma sep." if compact else "degC, comma separated",
            style="Hint.TLabel",
        ).pack(side=tk.LEFT, padx=5)

        box_m, self.cnt_mode = self._srow(grid, 2, "Mode", lw)
        for name in ("Quasi-static", "Full wave"):
            ttk.Checkbutton(
                box_m, text=name, variable=self.mode_vars[name], command=self.recompute
            ).pack(side=tk.LEFT, padx=(0, 12))

        box_f, self.cnt_freq = self._srow(
            grid, 3, "Freq sweep" if compact else "Frequency sweep", lw
        )
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
        self.freq_entries: dict[str, tuple[ttk.Label, ttk.Entry]] = {}
        for key, var in (
            ("start", self.f_start),
            ("stop", self.f_stop),
            ("step", self.f_step),
            ("points", self.f_pts),
        ):
            label = ttk.Label(box_f, text=key)
            label.pack(side=tk.LEFT, padx=(8, 3))
            entry = ttk.Entry(box_f, textvariable=var, width=6, font=self.f_mono)
            entry.pack(side=tk.LEFT)
            entry.bind("<KeyRelease>", lambda _e: self.recompute())
            self.freq_entries[key] = (label, entry)
        ttk.Label(box_f, text="GHz", style="Hint.TLabel").pack(side=tk.LEFT, padx=4)

        box_h, self.cnt_mesh = self._srow(grid, 4, "Mesh", lw)
        for long_label, short_label, var in (
            ("edge distance", "edge", self.m_edge),
            ("vertical distance", "vert", self.m_vert),
            ("via merge space", "via merge", self.m_via),
        ):
            ttk.Label(box_h, text=short_label if compact else long_label).pack(
                side=tk.LEFT, padx=(0, 3)
            )
            entry = ttk.Entry(box_h, textvariable=var, width=5, font=self.f_mono)
            entry.pack(side=tk.LEFT, padx=(0, 10))
            entry.bind("<KeyRelease>", lambda _e: self.recompute())

        # Advanced：收起来是一行摘要，展开是真控件（第 3 层「逃生口」住在这儿）
        adv_head = ttk.Frame(grid)
        adv_head.grid(row=5, column=0, columnspan=2, sticky=tk.W, pady=(2, 0))
        self.adv_btn = ttk.Button(adv_head, text="+ Advanced", width=13, command=self.toggle_adv)
        self.adv_btn.pack(side=tk.LEFT)
        self.adv_summary = ttk.Label(adv_head, text="", style="Off.TLabel")
        self.adv_summary.pack(side=tk.LEFT, padx=8)
        self.cnt_adv = ttk.Label(grid, text="-> 1", style="Off.TLabel", anchor=tk.E, width=6)
        self.cnt_adv.grid(row=5, column=2, sticky=tk.E, padx=(10, 0))

        self.adv_body = ttk.Frame(grid)
        row = ttk.Frame(self.adv_body)
        row.pack(anchor=tk.W, pady=(3, 0))
        ttk.Checkbutton(
            row, text="equalCurrent on", variable=self.eq_on, command=self.recompute
        ).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Checkbutton(row, text="off", variable=self.eq_off, command=self.recompute).pack(
            side=tk.LEFT, padx=(0, 18)
        )
        for label, var in (
            ("relative tolerance", self.tol_r),
            ("relative current tolerance", self.tol_c),
        ):
            ttk.Label(row, text=label).pack(side=tk.LEFT, padx=(0, 3))
            entry = ttk.Entry(row, textvariable=var, width=9, font=self.f_mono)
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
            self.adv_body.grid(row=6, column=0, columnspan=3, sticky=tk.W + tk.E)
            self.adv_btn.config(text="- Advanced")
            self.adv_summary.pack_forget()
        self.adv_open = not self.adv_open

    # ------------------------------------------------------------ resources
    def build_resources(self, parent: object, compact: bool = False) -> ttk.LabelFrame:
        """**整条 dsub 命令原样暴露给用户改**（用户 2026-08-18 要求，不是只让改 `-R`）。

        改完之后 `-R` 里的 `cpu=` 仍然要能读回去同步 `--parallel` ——
        那一步走 `core.cmd.parse_resource_string`（`GuiState.parallel()`），
        本文件不再解析第二遍。
        """
        box = ttk.LabelFrame(parent, text=" Resources ", padding=7)  # type: ignore[arg-type]
        top = ttk.Frame(box)
        top.pack(fill=tk.X)
        if not compact:
            ttk.Label(top, text="Submit command", width=15, anchor=tk.W).pack(side=tk.LEFT)
        entry = ttk.Entry(top, textvariable=self.dsub, font=self.f_mono)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        entry.bind("<KeyRelease>", lambda _e: self.recompute())

        bottom = ttk.Frame(box)
        bottom.pack(fill=tk.X, pady=(4, 0))
        self.par_lbl = ttk.Label(bottom, text="", style="Green.TLabel")
        self.par_lbl.pack(side=tk.LEFT)
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
            self.tree.column(key, width=width, anchor=tk.W, stretch=(key == "extra"))
        for key in ("n", "temp", "wall"):
            self.tree.column(key, anchor=tk.E)
        scroll = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.LEFT, fill=tk.Y)
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
            text=(
                "Nothing to run yet.\n"
                "Add a design and tick at least one corner and temperature -\n"
                "runs appear here as you type."
            ),
        )

        self.menu = tk.Menu(self.top, tearoff=0)
        for item in MENU_ITEMS:
            if item == "-":
                self.menu.add_separator()
            else:
                self.menu.add_command(label=item, command=lambda t=item: self.on_row_action(t))
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

        ttk.Label(box, text="Command", width=9, anchor=tk.NW).grid(row=1, column=0, sticky=tk.NW)
        holder = ttk.Frame(box)
        holder.grid(row=1, column=1, sticky="ew", pady=1)
        self.cmd_text = tk.Text(
            holder,
            height=4,
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
        if show_dir:
            ttk.Label(f, text="Batch dir", style="Hint.TLabel").pack(side=tk.LEFT, padx=(10, 4))
            self.batchdir_lbl = ttk.Label(f, text="", style="Mono.TLabel")
            self.batchdir_lbl.pack(side=tk.LEFT)
        else:
            self.batchdir_lbl = None
        self.right_lbl = ttk.Label(f, text="", style="Mono.TLabel")
        self.right_lbl.pack(side=tk.RIGHT)
        return f

    def build_statusbar(self, parent: object) -> ttk.Frame:
        f = ttk.Frame(parent, padding=(8, 2), relief=tk.SUNKEN)  # type: ignore[arg-type]
        self.status_lbl = ttk.Label(f, text="", style="Hint.TLabel")
        self.status_lbl.pack(side=tk.LEFT)
        self.status_right = ttk.Label(f, text="", style="Mono.TLabel")
        self.status_right.pack(side=tk.RIGHT)
        return f

    # -------------------------------------------------------------- compute
    def push(self) -> None:
        """GUI 变量 → bridge。**这是唯一往核心写值的地方**（别在回调里各写一份）。"""
        b = self.bridge
        b.set_batch_name(self.batch.get())
        if self.offdir.get().strip() != b.official_run_dir:
            b.set_official_run_dir(self.offdir.get())
        b.set_axis_values(
            "corner", [name for name in gui_state.CORNER_VALUES if self.corner_vars[name].get()]
        )
        b.set_axis_values("temperature", gui_state.parse_value_list(self.temp.get()))
        b.set_axis_values(
            "fullWave",
            [
                value
                for value, var in (("off", self.mode_vars["Quasi-static"]),
                                   ("on", self.mode_vars["Full wave"]))
                if var.get()
            ],
        )
        b.set_axis_values(
            "equalCurrent",
            [value for value, var in (("on", self.eq_on), ("off", self.eq_off)) if var.get()],
        )
        b.set_axis_values(
            "mesh",
            [gui_state.mesh_axis_value(self.m_edge.get(), self.m_vert.get(), self.m_via.get())],
        )
        b.set_axis_values("relativeTolerance", [self.tol_r.get()])
        b.set_axis_values("relativeCurrentTolerance", [self.tol_c.get()])
        b.set_sweep(
            mode=self.sw_mode.get(),
            start=self.f_start.get(),
            stop=self.f_stop.get(),
            step=self.f_step.get(),
            points=self.f_pts.get(),
        )
        b.set_extra_flags(self.extra.get())
        b.set_submit_command(self.dsub.get())

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
            if not self.bridge.is_running() and not self.bridge.has_started():
                self._guard(self.push)
                self._guard(self.bridge.plan)
            # 这四条也要各自过闸：用户把 Mesh 的某个格子清空、把温度写成一个词，
            # 核心会（正确地）拒绝，而**界面不该因此死掉**。理由写在 `_guard` 上。
            self._guard(self._sync_freq_fields)
            self._guard(self._sync_counts)
            self._guard(self._sync_resources)
            self._guard(self._sync_extra_warning)
            self.refresh_tree()
            self.update_status()
            self.sync_buttons()
        finally:
            self._syncing = False

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
            self._error = message if not self._error else self._error

    def _sync_freq_fields(self) -> None:
        """扫描模式决定哪几个格子有意义 —— 其余置灰。"""
        live = self.bridge.sweep_live_fields()
        for key, (label, entry) in self.freq_entries.items():
            on = key in live
            entry.config(state="normal" if on else "disabled")
            label.config(foreground="#101010" if on else "#9c9c9c")

    def _sync_counts(self) -> None:
        # ★ 批次名空着的时候 bridge 会现起一个 UTC 时间戳名。不灌回输入框的话，
        #   下一次 recompute（= 下一个键）又会生成一个新的 —— 批次目录每秒换一次，
        #   而界面上那行 Batch dir 就跟着跳，谁也不知道产物到底会落在哪。
        if self.bridge.batch_name and self.batch.get() != self.bridge.batch_name:
            self.batch.set(self.bridge.batch_name)
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
        if self.formula_lbl is not None:
            self.formula_lbl.config(text=formula)
        if self.bar_formula is not None:
            self.bar_formula.config(text=formula)
        if self.show_formula_in_title:
            self.settings_box.config(text=" Settings   -   %s " % formula)
        self.on_counts(counts, self.bridge.run_count())

        batch_dir = self.bridge.batch_dir() + "/"
        for label in (self.batchdir_lbl, self.dir_lbl):
            if label is not None:
                label.config(text=batch_dir)

    def _sync_resources(self) -> None:
        # ★ 第一次 plan 时 bridge 会从 `SiteFacts` 里学出整条 dsub 提交前缀 ——
        #   把它灌回输入框，用户才看得见"我们打算怎么提交"，也才改得动它
        #   （用户 2026-08-18 要求：整条命令原样暴露，不是只让改 `-R`）。
        if self.bridge.submit_command and self.dsub.get() != self.bridge.submit_command:
            self.dsub.set(self.bridge.submit_command)
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
        if self._runs:
            self.empty_lbl.place_forget()
        else:
            self.empty_lbl.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

    def show_detail(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        run_id = selection[0]
        self.out_var.set(_label(self.bridge.out_dir(run_id)))
        self.cmd_text.config(state="normal")
        self.cmd_text.delete("1.0", tk.END)
        self.cmd_text.insert("1.0", self.bridge.command_text(run_id) or _DASH)
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
        self.status_lbl.config(text=text)
        right = "%d / %d done" % (counts["done"], total) if total else "0 / 0"
        self.status_right.config(text=right)
        if self.right_lbl is not None:
            self.right_lbl.config(text=right)
        header = self._runs_summary(counts, total)
        if self.runs_header is not None:
            self.runs_header.config(text=header)
        elif self.runs_titled:
            self.runs_box.config(text=" Runs - %s " % header)

    def _runs_summary(self, counts: dict[str, int], total: int) -> str:
        if not total:
            return "0 runs"
        if not self.bridge.is_planned() or counts["ready"] == total:
            return "%d runs, preview (not submitted)" % total
        parts = [
            "%d %s" % (counts[name], name) for name in gui_state.STATUS_ORDER if counts[name]
        ]
        return "%d runs - %s" % (total, " - ".join(parts))

    def _last_event_text(self) -> str:
        events = self.bridge.events()
        if not events:
            return ""
        last = events[-1]
        return "%s: %s" % (last.kind.value, last.message)

    def sync_buttons(self) -> None:
        """按钮的可用性 —— 「跑过了」和「正在跑」是两回事，别合成一个布尔。

        跑过之后 Submit **保持禁用**：再按一次会把整批从头重跑（一个 run 可能
        10 核 100 GB 跑 35 分钟）。补没成的那些走 Resume，重来一批走 New batch。
        """
        running = self.bridge.is_running()
        started = self.bridge.has_started()
        for name in ("Dry-run", "Submit"):
            self.btn[name].state(["disabled"] if (running or started) else ["!disabled"])
        self.btn["Cancel"].state(["!disabled"] if running else ["disabled"])
        self.btn["Resume"].state(["!disabled"] if (started and not running) else ["disabled"])

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
        if report is not None and not report.finished:
            self._timer = self.frame.after(self._poll_ms(), self._pump)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            try:
                self.frame.after_cancel(self._timer)
            except tk.TclError:  # pragma: no cover - 窗口已经没了
                pass
            self._timer = None

    def do_submit(self) -> None:
        self.recompute()
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

    def _init_vars_from_bridge(self) -> None:
        """load_spec 之后把界面变量重新灌一遍（界面要显示文件里的东西）。"""
        selection = self.bridge.axis_selection()
        sweep = self.bridge.sweep()
        for name, var in self.corner_vars.items():
            var.set(name in selection.get("corner", ()))
        modes = selection.get("fullWave", ())
        self.mode_vars["Quasi-static"].set("off" in modes)
        self.mode_vars["Full wave"].set("on" in modes)
        self.temp.set(", ".join(selection.get("temperature", ())))
        self.sw_mode.set(sweep.get("mode", "adaptive"))
        self.f_start.set(sweep.get("start", ""))
        self.f_stop.set(sweep.get("stop", ""))
        self.f_step.set(sweep.get("step", ""))
        self.f_pts.set(sweep.get("points", ""))
        mesh = (selection.get("mesh") or ("0.4",))[0].split(gui_state.MESH_SEP)
        if len(mesh) == 1:
            mesh = mesh * 3
        self.m_edge.set(mesh[0])
        self.m_vert.set(mesh[1])
        self.m_via.set(mesh[2])
        eq = selection.get("equalCurrent", ())
        self.eq_on.set("on" in eq)
        self.eq_off.set("off" in eq)
        self.tol_r.set((selection.get("relativeTolerance") or ("",))[0])
        self.tol_c.set((selection.get("relativeCurrentTolerance") or ("",))[0])
        self.extra.set(self.bridge.extra_flags_text())
        self.batch.set(self.bridge.batch_name)
        self.offdir.set(self.bridge.official_run_dir)
        self.dsub.set(self.bridge.submit_command)

    def do_save_spec(self) -> None:
        _todo("write the current settings back out as a spec file")

    def do_pick_offdir(self) -> None:
        path = filedialog.askdirectory(title="Pick an official run dir (contains gdsout_setup)")
        if path:
            self.offdir.set(path)
            self.recompute()

    def do_open_batch_dir(self) -> None:
        _info("Batch dir", self.bridge.batch_dir())

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
            _info("Output dir", self.bridge.out_dir(run_id) or _DASH)
            return
        _todo("%s (%s)" % (action, run_id))

    def show_defaults(self) -> None:
        """第 2 层：有默认值、不上主界面的 flag。一个对话框，主界面零成本（§11）。"""
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
                "Editing here applies to the whole batch; to change a single run use\n"
                "Extra ewave flags under Advanced."
            ),
        ).pack(anchor=tk.W)

        rows = self.bridge.defaults_table()
        tree = ttk.Treeview(
            dlg,
            columns=("flag", "value", "src"),
            show="headings",
            height=max(len(rows) + 1, 6),
            style="Designs.Treeview",
        )
        for key, head, width in (
            ("flag", "Flag", 200),
            ("value", "Value", 110),
            ("src", "Where the default came from", 330),
        ):
            tree.heading(key, text=head)
            tree.column(key, width=width, anchor=tk.W)
        for row in rows:
            tree.insert("", tk.END, values=row)
        tree.pack(fill=tk.BOTH, expand=True, padx=8)

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
            command=lambda: (self.bridge.reset_defaults(), dlg.destroy(), self.recompute()),
        ).pack(side=tk.LEFT)
        ttk.Button(bar, text="Close", command=dlg.destroy).pack(side=tk.RIGHT)
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


def _info(title: str, message: str) -> None:
    if smoke_enabled():
        return
    messagebox.showinfo(title, message)


def _error(title: str, message: str) -> None:
    if smoke_enabled():
        return
    messagebox.showerror(title, message)


def _todo(what: str) -> None:
    _info("Not wired yet", "This will: %s" % what)


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
