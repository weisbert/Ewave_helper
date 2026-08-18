# -*- coding: utf-8 -*-
"""三版布局共用的控件与逻辑。

来自 Claude Design 项目 d4ad2ea7 的三个 frame：Stacked / Tabbed / Split。
设计说得很清楚——三版内容完全相同，只在「run 数显示在哪」和「Runs 表能留多少行」
上分岔。所以这里把内容做成一套可复用的 section builder，三个布局文件只负责摆放。

不接后端：不调 ewave / dsub，不写任何文件。
"""
import os
import sys
import tkinter as tk
from tkinter import ttk, messagebox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C

SMOKE = os.environ.get("MOCKUP_SMOKE") == "1"

UI = ("Segoe UI", 9)
UI_B = ("Segoe UI", 9, "bold")
MONO = ("Consolas", 9)
BLUE = "#123f7a"
GREEN = "#1a7f37"
GREY = "#7a7a7a"
HINT = "#666666"

# 设计里定死的状态配色（bg / fg）
STATUS_STYLE = {
    "ready":   ("#ffffff", "#5c5c5c"),
    "pending": ("#eaeef4", "#3c4a5c"),
    "running": ("#d5e5f7", "#123f7a"),
    "done":    ("#dcecdc", "#1a5c26"),
    "failed":  ("#f6d8d8", "#8d1f1f"),
    "skipped": ("#ececec", "#8d8d8d"),
}

# 设计 artboard 03/04 的那一屏：3 done / 1 failed / 2 running / 5 pending / 1 skipped
DEMO_MIX = [
    ("done", "0:41", "482113"), ("done", "0:38", "482114"), ("done", "1:02", "482115"),
    ("failed", "0:12", "482116"), ("running", "0:26", "482117"),
    ("running", "0:21", "482118"), ("pending", "—", "482119"),
    ("pending", "—", "482120"), ("skipped", "—", "—"),
    ("pending", "—", "482122"), ("pending", "—", "482123"),
    ("pending", "—", "482124"),
]

RUN_COLS = (("n", "#", 40), ("design", "Design", 150), ("corner", "Corner", 78),
            ("temp", "Temp", 56), ("mode", "Mode", 92), ("extra", "Extra axes", 150),
            ("status", "Status", 86), ("wall", "Wall time", 74), ("job", "Job id", 74))

MENU_ITEMS = ("Open log", "Open output dir", "Copy command", "-",
              "Re-run this one", "Set as current")


def stub(what):
    messagebox.showinfo("mockup", "这里会：%s\n\n（界面草图，后端没接）" % what)


class BaseApp(object):
    """三个布局共用的状态 + section builder。子类只实现 layout()。"""

    TITLE = "eWave Batch"
    GEOMETRY = "1180x900"
    RUN_ROWS = 12

    def __init__(self, root):
        self.root = root
        root.geometry(self.GEOMETRY)

        self.designs = list(C.DESIGN_ROWS)
        self.runs = []
        self.varying = set()
        self.parallel = 20
        self.submitted = False
        self.timer = None
        self.step = 0
        self.adv_open = False
        self.batch = tk.StringVar(value="ind_top_re_freq_2026_0818")

        self._init_vars()
        self._init_style()
        self.build_menubar()
        self.layout()
        self.recompute()

    # ------------------------------------------------------------ variables
    def _init_vars(self):
        self.corner_vars = {c: tk.BooleanVar(value=(c == "typical"))
                            for c in C.CORNERS}
        self.mode_vars = {m: tk.BooleanVar(value=True) for m in C.MODES}
        self.temp = tk.StringVar(value="-40, 55, 125")
        self.sw_mode = tk.StringVar(value="adaptive")
        self.f_start = tk.StringVar(value="0")
        self.f_stop = tk.StringVar(value="40")
        self.f_step = tk.StringVar(value="0.1")
        self.f_pts = tk.StringVar(value="")
        self.m_edge = tk.StringVar(value="0.4")
        self.m_vert = tk.StringVar(value="0.4")
        self.m_via = tk.StringVar(value="0.4")
        self.eq_on = tk.BooleanVar(value=True)
        self.eq_off = tk.BooleanVar(value=False)
        self.tol_r = tk.StringVar(value="1e-05")
        self.tol_c = tk.StringVar(value="0.001")
        self.dsub = tk.StringVar(value=C.DEFAULT_DSUB)
        self.par_follow = tk.BooleanVar(value=True)
        self.extra = tk.StringVar(value="")

    def _init_style(self):
        st = ttk.Style()
        try:
            st.theme_use("vista")
        except tk.TclError:
            pass
        st.configure("Runs.Treeview", font=MONO, rowheight=21)
        st.configure("Runs.Treeview.Heading", font=UI_B)
        st.configure("Designs.Treeview", font=MONO, rowheight=20)
        st.configure("Designs.Treeview.Heading", font=UI_B)
        st.configure("TLabel", font=UI)
        st.configure("TButton", font=UI)
        st.configure("TCheckbutton", font=UI)
        st.configure("TLabelframe.Label", font=UI_B)
        st.configure("Count.TLabel", font=MONO, foreground=BLUE)
        st.configure("Off.TLabel", font=MONO, foreground=GREY)
        st.configure("Hint.TLabel", font=UI, foreground=HINT)
        st.configure("Green.TLabel", font=MONO, foreground=GREEN)
        st.configure("Mono.TLabel", font=MONO)
        st.configure("Accent.TButton", font=UI_B)

    # -------------------------------------------------------------- menubar
    def build_menubar(self):
        bar = tk.Menu(self.root)
        for name, items in (
                ("File", ("New batch", "Open batch…", "Save", "Save as…", "-", "Exit")),
                ("Batch", ("Duplicate batch…", "Rename…", "-", "Open batch dir")),
                ("Runs", ("Dry-run", "Submit", "Cancel", "Resume", "-",
                          "Re-run failed only")),
                ("Tools", ("Check environment (doctor)",)),
                ("Help", ("About",))):
            m = tk.Menu(bar, tearoff=0)
            for it in items:
                if it == "-":
                    m.add_separator()
                else:
                    m.add_command(label=it, command=lambda t=it: stub(t))
            if name == "Tools":
                m.add_command(label="Extraction defaults…",
                              command=self.show_defaults)
                m.add_separator()
                demo = tk.Menu(m, tearoff=0)
                for lab, fn in (("01 empty batch", self.demo_empty),
                                ("02 preview (12 runs)", self.demo_preview),
                                ("03 submitted, half way", self.demo_running),
                                ("04 right-click on failed", self.demo_context)):
                    demo.add_command(label=lab, command=fn)
                m.add_cascade(label="Demo state", menu=demo)
            bar.add_cascade(label=name, menu=m)
        self.root.config(menu=bar)

    def show_defaults(self):
        """第 2 层：有默认值、不上主界面的 flag。一个对话框，主界面零成本。"""
        dlg = tk.Toplevel(self.root)
        dlg.title("Extraction defaults")
        dlg.transient(self.root)
        ttk.Label(dlg, padding=8, style="Hint.TLabel", justify=tk.LEFT,
                  text="这些 flag 有默认值、不占主界面。默认值不是写死在源码里的，\n"
                       "是第一次运行时从官方 run 目录学来的（改 PDK 版本时自动跟上）。\n"
                       "在这里改 = 对整个批次生效；只改一个 run 用 Advanced 里的 "
                       "Extra flags。").pack(anchor=tk.W)
        tv = ttk.Treeview(dlg, columns=("flag", "value", "src"), show="headings",
                          height=len(C.SITE_DEFAULTS) + 1, style="Designs.Treeview")
        for c, h, w in (("flag", "Flag", 190), ("value", "Value", 90),
                        ("src", "Where the default came from", 250)):
            tv.heading(c, text=h)
            tv.column(c, width=w, anchor=tk.W)
        for row in C.SITE_DEFAULTS:
            tv.insert("", tk.END, values=row)
        tv.pack(fill=tk.BOTH, expand=True, padx=8)
        lock = ttk.Label(dlg, padding=8, style="Off.TLabel", justify=tk.LEFT,
                         text="locked (界面上不出现，改了工具机制就失效):  "
                              + "  ".join(C.LOCKED_FLAGS))
        lock.pack(anchor=tk.W)
        bar = ttk.Frame(dlg, padding=8)
        bar.pack(fill=tk.X)
        ttk.Button(bar, text="Reset to learned values",
                   command=lambda: stub("恢复成从官方目录学来的值")).pack(side=tk.LEFT)
        ttk.Button(bar, text="Close", command=dlg.destroy).pack(side=tk.RIGHT)
        if not SMOKE:
            dlg.grab_set()

    # ------------------------------------------------------------ batch bar
    def build_batchbar(self, parent, show_dir=False):
        f = ttk.Frame(parent, padding=(8, 6))
        ttk.Label(f, text="Batch name:").pack(side=tk.LEFT)
        ttk.Entry(f, textvariable=self.batch, width=34, font=MONO).pack(
            side=tk.LEFT, padx=(6, 8))
        for t in ("New", "Load…", "Save"):
            ttk.Button(f, text=t, width=7,
                       command=lambda x=t: stub(x)).pack(side=tk.LEFT, padx=1)
        ttk.Separator(f, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        ttk.Button(f, text="Duplicate batch…", style="Accent.TButton",
                   command=lambda: stub("照上一批再跑一遍、只改一个参数")
                   ).pack(side=tk.LEFT)
        if show_dir:
            self.dir_lbl = ttk.Label(f, text="", style="Mono.TLabel")
            self.dir_lbl.pack(side=tk.RIGHT)
        else:
            ttk.Label(f, text="copy last batch, change one parameter",
                      style="Hint.TLabel").pack(side=tk.LEFT, padx=8)
        return f

    # -------------------------------------------------------------- designs
    def build_designs(self, parent, widths=(230, 230, 200), rows=3,
                      buttons="side", titled=True):
        if titled:
            box = ttk.LabelFrame(parent, text=" Designs ", padding=7)
        else:
            box = ttk.Frame(parent, padding=2)
        self.designs_box = box
        inner = ttk.Frame(box)
        inner.pack(fill=tk.BOTH, expand=True)

        self.dtree = ttk.Treeview(inner, columns=("lib", "cell", "view"),
                                  show="headings", height=rows,
                                  style="Designs.Treeview", selectmode="browse")
        for (c, h), w in zip((("lib", "Library"), ("cell", "Cell"),
                              ("view", "View")), widths):
            self.dtree.heading(c, text=h)
            self.dtree.column(c, width=w, anchor=tk.W, stretch=(c == "cell"))
        self.dtree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        side = ttk.Frame(inner)
        side.pack(side=tk.LEFT, padx=(6, 0), fill=tk.Y)
        ttk.Button(side, text="Add row", width=12,
                   command=self.add_design).pack(pady=1)
        ttk.Button(side, text="Remove row", width=12,
                   command=self.del_design).pack(pady=1)
        if buttons == "three":
            ttk.Button(side, text="Duplicate row", width=12,
                       command=self.dup_design).pack(pady=1)

        foot = ttk.Frame(box)
        foot.pack(fill=tk.X, pady=(4, 0))
        self.design_count = ttk.Label(foot, text="→ 2", style="Count.TLabel")
        self.design_count.pack(side=tk.RIGHT)
        self.refresh_designs()
        return box

    def refresh_designs(self):
        self.dtree.delete(*self.dtree.get_children())
        for d in self.designs:
            self.dtree.insert("", tk.END, values=d)

    def add_design(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("Add design")
        dlg.transient(self.root)
        vs = []
        for i, lab in enumerate(("Library", "Cell", "View")):
            ttk.Label(dlg, text=lab).grid(row=i, column=0, sticky=tk.W,
                                          padx=8, pady=4)
            v = tk.StringVar()
            ttk.Entry(dlg, textvariable=v, width=30, font=MONO).grid(
                row=i, column=1, padx=8, pady=4)
            vs.append(v)

        def ok():
            vals = tuple(v.get().strip() for v in vs)
            if all(vals):
                self.designs.append(vals)
                self.refresh_designs()
                self.recompute()
            dlg.destroy()

        ttk.Button(dlg, text="OK", command=ok).grid(row=3, column=1,
                                                    sticky=tk.E, padx=8, pady=8)
        if not SMOKE:
            dlg.grab_set()

    def del_design(self):
        for iid in self.dtree.selection():
            del self.designs[self.dtree.index(iid)]
        self.refresh_designs()
        self.recompute()

    def dup_design(self):
        for iid in self.dtree.selection():
            self.designs.append(self.designs[self.dtree.index(iid)])
        self.refresh_designs()
        self.recompute()

    # ------------------------------------------------------------- settings
    def _srow(self, parent, r, label, lw=14):
        ttk.Label(parent, text=label, width=lw, anchor=tk.W).grid(
            row=r, column=0, sticky=tk.W, pady=2)
        box = ttk.Frame(parent)
        box.grid(row=r, column=1, sticky=tk.W)
        cnt = ttk.Label(parent, text="→ 1", style="Count.TLabel", anchor=tk.E,
                        width=5)
        cnt.grid(row=r, column=2, sticky=tk.E, padx=(10, 0))
        ttk.Separator(parent, orient=tk.HORIZONTAL).grid(
            row=r, column=0, columnspan=3, sticky="sew")
        return box, cnt

    def build_settings(self, parent, compact=False, title=" Settings ",
                       show_formula=True):
        box = ttk.LabelFrame(parent, text=title, padding=7)
        self.settings_box = box
        self.show_formula_in_title = show_formula
        g = ttk.Frame(box)
        g.pack(fill=tk.X)
        g.columnconfigure(1, weight=1)
        lw = 11 if compact else 15

        box_c, self.cnt_corner = self._srow(g, 0, "Corner", lw)
        for c in C.CORNERS:
            ttk.Checkbutton(box_c, text=c, variable=self.corner_vars[c],
                            command=self.recompute).pack(side=tk.LEFT,
                                                         padx=(0, 8 if compact else 11))

        box_t, self.cnt_temp = self._srow(g, 1, "Temp" if compact else "Temperature", lw)
        e = ttk.Entry(box_t, textvariable=self.temp, font=MONO,
                      width=20 if compact else 32)
        e.pack(side=tk.LEFT)
        e.bind("<KeyRelease>", lambda _e: self.recompute())
        ttk.Label(box_t, text="°C, comma sep." if compact else "°C, comma separated",
                  style="Hint.TLabel").pack(side=tk.LEFT, padx=5)

        box_m, self.cnt_mode = self._srow(g, 2, "Mode", lw)
        for m in C.MODES:
            ttk.Checkbutton(box_m, text=m, variable=self.mode_vars[m],
                            command=self.recompute).pack(side=tk.LEFT, padx=(0, 12))

        box_f, self.cnt_freq = self._srow(
            g, 3, "Freq sweep" if compact else "Frequency sweep", lw)
        cb = ttk.Combobox(box_f, textvariable=self.sw_mode, values=C.SWEEP_MODES,
                          width=10, state="readonly", font=MONO)
        cb.pack(side=tk.LEFT)
        cb.bind("<<ComboboxSelected>>", lambda _e: self.recompute())
        self.freq_entries = {}
        for key, lab, var in (("start", "start", self.f_start),
                              ("stop", "stop", self.f_stop),
                              ("step", "step", self.f_step),
                              ("points", "points", self.f_pts)):
            l = ttk.Label(box_f, text=lab)
            l.pack(side=tk.LEFT, padx=(8, 3))
            en = ttk.Entry(box_f, textvariable=var, width=6, font=MONO)
            en.pack(side=tk.LEFT)
            en.bind("<KeyRelease>", lambda _e: self.recompute())
            self.freq_entries[key] = (l, en)
        ttk.Label(box_f, text="GHz", style="Hint.TLabel").pack(side=tk.LEFT, padx=4)

        box_h, self.cnt_mesh = self._srow(g, 4, "Mesh", lw)
        for lab, short, var in (("edge distance", "edge", self.m_edge),
                                ("vertical distance", "vert", self.m_vert),
                                ("via merge space", "via merge", self.m_via)):
            ttk.Label(box_h, text=short if compact else lab).pack(side=tk.LEFT,
                                                                  padx=(0, 3))
            en = ttk.Entry(box_h, textvariable=var, width=5, font=MONO)
            en.pack(side=tk.LEFT, padx=(0, 10))
            en.bind("<KeyRelease>", lambda _e: self.recompute())

        # Advanced：收起来时是一行摘要，展开是真控件
        adv_head = ttk.Frame(g)
        adv_head.grid(row=5, column=0, columnspan=2, sticky=tk.W, pady=(2, 0))
        self.adv_btn = ttk.Button(adv_head, text="▸ Advanced", width=12,
                                  command=self.toggle_adv)
        self.adv_btn.pack(side=tk.LEFT)
        self.adv_summary = ttk.Label(adv_head, text="", style="Off.TLabel")
        self.adv_summary.pack(side=tk.LEFT, padx=8)
        self.cnt_adv = ttk.Label(g, text="→ 1", style="Off.TLabel", anchor=tk.E,
                                 width=5)
        self.cnt_adv.grid(row=5, column=2, sticky=tk.E, padx=(10, 0))

        self.adv_body = ttk.Frame(g)
        b = ttk.Frame(self.adv_body)
        b.pack(anchor=tk.W, pady=(3, 0))
        ttk.Checkbutton(b, text="equalCurrent on", variable=self.eq_on,
                        command=self.recompute).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Checkbutton(b, text="off", variable=self.eq_off,
                        command=self.recompute).pack(side=tk.LEFT, padx=(0, 18))
        for lab, var in (("relative tolerance", self.tol_r),
                         ("relative current tolerance", self.tol_c)):
            ttk.Label(b, text=lab).pack(side=tk.LEFT, padx=(0, 3))
            ttk.Entry(b, textvariable=var, width=8, font=MONO).pack(
                side=tk.LEFT, padx=(0, 14))

        # 第 3 层：逃生口。别人让你加个我们没做的 flag 时不用等改代码。
        x = ttk.Frame(self.adv_body)
        x.pack(anchor=tk.W, fill=tk.X, pady=(4, 0))
        ttk.Label(x, text="Extra ewave flags").pack(side=tk.LEFT, padx=(0, 4))
        en = ttk.Entry(x, textvariable=self.extra, font=MONO)
        en.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        en.bind("<KeyRelease>", lambda _e: self.recompute())
        ttk.Button(x, text="Defaults…", width=10,
                   command=self.show_defaults).pack(side=tk.LEFT)
        self.extra_warn = tk.Label(self.adv_body, font=UI, fg="#8d1f1f",
                                   anchor=tk.W, justify=tk.LEFT)

        if show_formula:
            sep = ttk.Separator(box, orient=tk.HORIZONTAL)
            sep.pack(fill=tk.X, pady=(6, 3))
            tot = ttk.Frame(box)
            tot.pack(fill=tk.X)
            ttk.Label(tot, text="Total", font=UI_B).pack(side=tk.LEFT)
            self.formula_lbl = ttk.Label(tot, text="", style="Count.TLabel",
                                         font=("Consolas", 9, "bold"))
            self.formula_lbl.pack(side=tk.RIGHT)
        else:
            self.formula_lbl = None
        return box

    def toggle_adv(self):
        if self.adv_open:
            self.adv_body.grid_forget()
            self.adv_btn.config(text="▸ Advanced")
            self.adv_summary.pack(side=tk.LEFT, padx=8)
        else:
            self.adv_body.grid(row=6, column=0, columnspan=3, sticky=tk.W)
            self.adv_btn.config(text="▾ Advanced")
            self.adv_summary.pack_forget()
        self.adv_open = not self.adv_open

    # ------------------------------------------------------------ resources
    def build_resources(self, parent, compact=False):
        box = ttk.LabelFrame(parent, text=" Resources ", padding=7)
        top = ttk.Frame(box)
        top.pack(fill=tk.X)
        if not compact:
            ttk.Label(top, text="Submit command", width=15,
                      anchor=tk.W).pack(side=tk.LEFT)
        e = ttk.Entry(top, textvariable=self.dsub, font=MONO)
        e.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        e.bind("<KeyRelease>", lambda _e: self.recompute())
        if not compact:
            ttk.Button(top, text="Per-design override…",
                       command=lambda: stub("给某个 design 单独设资源")).pack(side=tk.LEFT)
        bot = ttk.Frame(box)
        bot.pack(fill=tk.X, pady=(4, 0))
        ttk.Checkbutton(bot, text="follow cpu count from command",
                        variable=self.par_follow,
                        command=self.recompute).pack(side=tk.LEFT)
        self.par_lbl = ttk.Label(bot, text="", style="Green.TLabel")
        self.par_lbl.pack(side=tk.LEFT, padx=8)
        if compact:
            ttk.Button(bot, text="Per-design override…",
                       command=lambda: stub("给某个 design 单独设资源")
                       ).pack(side=tk.RIGHT)
        return box

    # ----------------------------------------------------------------- runs
    def build_runs(self, parent, rows=12, titled=True, header_in_title=True):
        if titled:
            box = ttk.LabelFrame(parent, text=" Runs ", padding=7)
        else:
            box = ttk.Frame(parent)
        self.runs_box = box
        self.runs_titled = titled

        if not header_in_title:
            head = ttk.Frame(box)
            head.pack(fill=tk.X, pady=(0, 4))
            ttk.Label(head, text="Runs", font=UI_B).pack(side=tk.LEFT)
            self.runs_header = ttk.Label(head, text="", style="Hint.TLabel")
            self.runs_header.pack(side=tk.LEFT, padx=8)
        else:
            self.runs_header = None

        wrap = ttk.Frame(box)
        wrap.pack(fill=tk.BOTH, expand=True)
        cols = tuple(c[0] for c in RUN_COLS)
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", height=rows,
                                 style="Runs.Treeview")
        for key, head, w in RUN_COLS:
            self.tree.heading(key, text=head)
            self.tree.column(key, width=w, anchor=tk.W,
                             stretch=(key == "extra"))
        for key in ("n", "temp", "wall"):
            self.tree.column(key, anchor=tk.E)
        sb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.LEFT, fill=tk.Y)
        for name, (bg, fg) in STATUS_STYLE.items():
            self.tree.tag_configure(name, background=bg, foreground=fg)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.show_detail())
        self.tree.bind("<Button-3>", self.popup)

        self.empty_lbl = tk.Label(
            wrap, justify=tk.CENTER, fg="#8d8d8d", bg="white", font=UI,
            text="Nothing to run yet.\nAdd a design and tick at least one corner,"
                 " temperature and mode —\nruns appear here as you type.")

        self.menu = tk.Menu(self.root, tearoff=0)
        for it in MENU_ITEMS:
            if it == "-":
                self.menu.add_separator()
            else:
                self.menu.add_command(label=it, command=lambda t=it: stub(t))
        return box

    def build_detail(self, parent):
        box = ttk.LabelFrame(parent, text=" Selected run ", padding=7)
        self.detail_box = box
        self.out_var = tk.StringVar(value="—")
        self.cmd_var = tk.StringVar(value="—")
        for r, (lab, var) in enumerate((("Out dir", self.out_var),
                                        ("Command", self.cmd_var))):
            ttk.Label(box, text=lab, width=9, anchor=tk.W).grid(
                row=r, column=0, sticky=tk.W, pady=1)
            en = tk.Entry(box, textvariable=var, font=MONO, state="readonly",
                          readonlybackground="#f6f6f6", relief=tk.SOLID, bd=1,
                          fg="#222222")
            en.grid(row=r, column=1, sticky="ew", pady=1)
        box.columnconfigure(1, weight=1)
        return box

    # ----------------------------------------------------------- action bar
    def build_actionbar(self, parent, show_formula=False, show_dir=True):
        f = ttk.Frame(parent, padding=(8, 5))
        if show_formula:
            self.bar_formula = ttk.Label(f, text="", style="Count.TLabel",
                                         font=("Consolas", 9, "bold"))
            self.bar_formula.pack(side=tk.LEFT)
            ttk.Separator(f, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y,
                                                      padx=8)
        else:
            self.bar_formula = None
        self.btn = {}
        for t, cb, style in (("Dry-run", lambda: stub("只拼命令、不提交"), "TButton"),
                             ("Submit", self.submit, "Accent.TButton"),
                             ("Cancel", self.cancel, "TButton"),
                             ("Resume", lambda: stub("只补没成的"), "TButton")):
            b = ttk.Button(f, text=t, width=9, command=cb, style=style)
            b.pack(side=tk.LEFT, padx=2)
            self.btn[t] = b
        ttk.Button(f, text="Open batch dir", width=14,
                   command=lambda: stub("打开批次目录")).pack(side=tk.LEFT, padx=(8, 2))
        if show_dir:
            ttk.Label(f, text="Batch dir", style="Hint.TLabel").pack(side=tk.LEFT,
                                                                     padx=(10, 4))
            self.batchdir_lbl = ttk.Label(f, text="", style="Mono.TLabel")
            self.batchdir_lbl.pack(side=tk.LEFT)
        else:
            self.batchdir_lbl = None
        self.right_lbl = ttk.Label(f, text="", style="Mono.TLabel")
        self.right_lbl.pack(side=tk.RIGHT)
        return f

    def build_statusbar(self, parent):
        f = ttk.Frame(parent, padding=(8, 2), relief=tk.SUNKEN)
        self.status_lbl = ttk.Label(f, text="", style="Hint.TLabel")
        self.status_lbl.pack(side=tk.LEFT)
        self.status_right = ttk.Label(f, text="", style="Mono.TLabel")
        self.status_right.pack(side=tk.RIGHT)
        return f

    # -------------------------------------------------------------- compute
    def axis_values(self):
        corners = [c for c in C.CORNERS if self.corner_vars[c].get()]
        temps = C.parse_list(self.temp.get())
        modes = [m for m in C.MODES if self.mode_vars[m].get()]
        meshes = [(self.m_edge.get(), self.m_vert.get(), self.m_via.get())]
        tols = [(self.tol_r.get(), self.tol_c.get())]
        eqs = [b for b, v in ((True, self.eq_on), (False, self.eq_off)) if v.get()]
        return corners, temps, modes, meshes, tols, eqs

    def sweep_flag(self):
        return C.sweep_flag(self.sw_mode.get(), self.f_start.get(),
                            self.f_stop.get(), self.f_step.get(), self.f_pts.get())

    def _sync_freq_fields(self):
        """扫描模式决定哪几个格子有意义 —— 其余置灰（设计里那个 n/a）。"""
        mode = self.sw_mode.get()
        if mode in ("adaptive", "linear"):
            live = ("start", "stop", "step", "points")
        elif mode == "logarithmic":
            live = ("stop", "points")
        else:                                   # discrete
            live = ("start",)
        for key, (lab, en) in self.freq_entries.items():
            on = key in live
            en.config(state="normal" if on else "disabled")
            lab.config(foreground="#101010" if on else "#9c9c9c")

    def recompute(self):
        corners, temps, modes, meshes, tols, eqs = self.axis_values()
        self._sync_freq_fields()

        cpu = C.parse_cpu(self.dsub.get())
        self.parallel = cpu if (cpu and self.par_follow.get()) else 20
        self.par_lbl.config(text="→ ewave --parallel=%s" % self.parallel)

        self.runs = C.expand(self.designs, corners, temps, modes, meshes, tols, eqs)
        self.varying = C.varying_axes(corners, temps, modes, meshes, tols, eqs)
        if self.submitted:
            for i, r in enumerate(self.runs):
                if i < len(self.demo_status):
                    r.status, r.wall, r.jobid = self.demo_status[i]

        n = len(self.runs)
        self.design_count.config(text="→ %d" % len(self.designs))
        for lbl, cnt in ((self.cnt_corner, len(corners)), (self.cnt_temp, len(temps)),
                         (self.cnt_mode, len(modes)), (self.cnt_mesh, len(meshes))):
            lbl.config(text="→ %d" % cnt)
        self.cnt_freq.config(text="→ 1")
        self.cnt_adv.config(text="→ %d" % max(len(eqs) * len(tols), 1))
        self.adv_summary.config(
            text="equalCurrent %s · rtol %s · ictol %s"
                 % ("on" if self.eq_on.get() and not self.eq_off.get()
                    else ("on+off" if self.eq_on.get() else "off"),
                    self.tol_r.get(), self.tol_c.get())
                 + ("  ·  +%d extra flag(s)" % len(self.extra.get().split())
                    if self.extra.get().strip() else ""))

        hits = C.conflicting_flags(self.extra.get())
        if hits:
            self.extra_warn.config(
                text="⚠  %s 已经是界面上的轴 —— 在 Extra flags 里再写一遍，"
                     "目录名就会和实际跑的值对不上" % "  ".join(hits))
            self.extra_warn.pack(anchor=tk.W, fill=tk.X, pady=(3, 0))
        else:
            self.extra_warn.pack_forget()

        formula = "%d designs × %d corner × %d temp × %d mode = %d runs" % (
            len(self.designs), len(corners), len(temps), len(modes), n)
        if self.formula_lbl is not None:
            self.formula_lbl.config(text=formula)
        if getattr(self, "bar_formula", None) is not None:
            self.bar_formula.config(text=formula)
        if self.show_formula_in_title:
            self.settings_box.config(text=" Settings   —   %s " % formula)
        self.on_counts(len(self.designs), len(corners), len(temps), len(modes), n)

        header = self.runs_summary(n)
        if self.runs_header is not None:
            self.runs_header.config(text=header)
        elif self.runs_titled:
            self.runs_box.config(text=" Runs — %s " % header)

        batchdir = "%s/%s/" % (C.BATCH_ROOT, self.batch.get())
        for lbl in (getattr(self, "batchdir_lbl", None), getattr(self, "dir_lbl", None)):
            if lbl is not None:
                lbl.config(text=batchdir)
        self.refresh_tree()
        self.update_status()
        self.sync_buttons()

    def runs_summary(self, n):
        if not n:
            return "0 runs"
        if not self.submitted:
            return "%d runs, preview (not submitted)" % n
        by = {}
        for r in self.runs:
            by[r.status] = by.get(r.status, 0) + 1
        parts = ["%d %s" % (by[k], k) for k in
                 ("done", "running", "failed", "pending", "skipped") if k in by]
        return "%d runs · %s" % (n, " · ".join(parts))

    def update_status(self):
        n = len(self.runs)
        if not n:
            self.status_lbl.config(text="New batch — nothing configured")
            self.status_right.config(text="0 / 0")
            if self.right_lbl is not None:
                self.right_lbl.config(text="0 / 0")
            return
        if self.submitted:
            done = len([r for r in self.runs if r.status == "done"])
            failed = [r for r in self.runs if r.status == "failed"]
            txt = "Submitted 14:02"
            if failed:
                txt += " · run %d failed: verify found no S-parameter output" \
                       " (right-click → Open log)" % (self.runs.index(failed[0]) + 1)
            right = "%d / %d done" % (done, n)
        else:
            txt = "Preview up to date — %d runs ready to submit" % n
            right = "%d ready" % n
        self.status_lbl.config(text=txt)
        self.status_right.config(text=right)
        if self.right_lbl is not None:
            self.right_lbl.config(text=right)

    def sync_buttons(self):
        for t in ("Dry-run", "Submit"):
            self.btn[t].state(["disabled"] if self.submitted else ["!disabled"])
        for t in ("Cancel", "Resume"):
            self.btn[t].state(["!disabled"] if self.submitted else ["disabled"])

    def refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(self.runs):
            self.tree.insert(
                "", tk.END, iid=str(i),
                values=(i + 1, r.design[1], r.corner, "%s°C" % r.temp,
                        r.mode_short(), r.axes_slug(self.varying),
                        "■ " + r.status, r.wall or "—", r.jobid or "—"),
                tags=(r.status,))
        if self.runs:
            self.empty_lbl.place_forget()
        else:
            self.empty_lbl.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

    def show_detail(self):
        sel = self.tree.selection()
        if not sel:
            return
        r = self.runs[int(sel[0])]
        self.out_var.set(r.outdir(self.batch.get(), self.varying))
        self.cmd_var.set(r.command(self.sweep_flag(), self.parallel,
                                   self.extra.get()))
        if self.detail_box is not None:
            self.detail_box.config(
                text=" Selected run — #%s  %s · %s · %s°C · %s · %s "
                     % (int(sel[0]) + 1, r.design[1], r.corner, r.temp,
                        r.mode_short(), r.status))

    def popup(self, ev):
        iid = self.tree.identify_row(ev.y)
        if iid:
            self.tree.selection_set(iid)
            self.menu.tk_popup(ev.x_root, ev.y_root)

    # ------------------------------------------------------ fake execution
    demo_status = []

    def submit(self):
        self.submitted = True
        self.demo_status = []
        for r in self.runs:
            r.status, r.wall, r.jobid = "pending", "", ""
        self.step = 0
        self.refresh_tree()
        self.sync_buttons()
        self.tick()

    def cancel(self):
        if self.timer:
            self.root.after_cancel(self.timer)
            self.timer = None
        self.submitted = False
        self.demo_status = []
        for r in self.runs:
            r.status, r.wall, r.jobid = "ready", "", ""
        self.recompute()

    def tick(self):
        busy = False
        for i, r in enumerate(self.runs):
            if r.status == "running":
                secs = C.wall_to_secs(r.wall) + 13
                r.wall = C.secs_to_wall(secs)
                if secs >= 60:
                    r.status = "failed" if i == 3 else "done"
                busy = True
        if len([r for r in self.runs if r.status == "running"]) < 2:
            for i, r in enumerate(self.runs):
                if r.status == "pending":
                    r.status = "running"
                    r.wall = "0:00"
                    r.jobid = str(482113 + i)
                    busy = True
                    break
        self.step += 1
        self.refresh_tree()
        self.update_status()
        if busy and self.step < 400:
            self.timer = self.root.after(600, self.tick)

    # ---------------------------------------------------------- demo states
    def _stop(self):
        if self.timer:
            self.root.after_cancel(self.timer)
            self.timer = None

    def demo_empty(self):
        self._stop()
        self.submitted = False
        self.demo_status = []
        self.designs = []
        self.temp.set("")
        for v in self.corner_vars.values():
            v.set(False)
        for v in self.mode_vars.values():
            v.set(False)
        self.refresh_designs()
        self.recompute()

    def demo_preview(self):
        self._stop()
        self.submitted = False
        self.demo_status = []
        self.designs = list(C.DESIGN_ROWS)
        self.temp.set("-40, 55, 125")
        for c, v in self.corner_vars.items():
            v.set(c == "typical")
        for v in self.mode_vars.values():
            v.set(True)
        self.refresh_designs()
        self.recompute()

    def demo_running(self):
        self.demo_preview()
        self.submitted = True
        self.demo_status = list(DEMO_MIX)
        self.recompute()
        self.tree.selection_set("3")

    def demo_context(self):
        self.demo_running()
        self.tree.update_idletasks()
        bbox = self.tree.bbox("3")
        if bbox:
            x = self.tree.winfo_rootx() + 120
            y = self.tree.winfo_rooty() + bbox[1] + bbox[3]
            self.menu.tk_popup(x, y)

    # ------------------------------------------------------------- subclass
    def layout(self):
        raise NotImplementedError

    def on_counts(self, ndesign, ncorner, ntemp, nmode, total):
        """Tabbed 版用它更新右边那个 Run count 面板。"""
        pass


def run(app_cls, title):
    root = tk.Tk()
    root.title(title)
    if SMOKE:
        root.withdraw()
    app_cls(root)
    if SMOKE:
        root.after(700, root.destroy)
    root.mainloop()
