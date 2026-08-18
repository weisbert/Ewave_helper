# -*- coding: utf-8 -*-
"""布局 1b — Tabbed：四个 tab（Designs / Settings / Resources / Runs）。

批次栏和动作栏在 notebook 外面常驻，所以乘法公式和 Submit 永远在屏幕上。
Settings 页右边多一个 Run count 面板：逐轴 × N，底下 total。
Runs 页独占整窗，~20 行 + 选中详情。
代价：设定和它展开出来的 run 永远不同屏。

运行：python mockups\\tabbed.py
"""
import os
import sys
import tkinter as tk
from tkinter import ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ui

AXES = ("designs (Designs tab)", "corner", "temperature", "mode",
        "frequency sweep", "mesh")


class Tabbed(_ui.BaseApp):

    def layout(self):
        root = self.root
        self.build_batchbar(root, show_dir=True).pack(fill=tk.X)
        ttk.Separator(root, orient=tk.HORIZONTAL).pack(fill=tk.X)

        self.nb = ttk.Notebook(root, padding=6)
        self.nb.pack(fill=tk.BOTH, expand=True)

        t_designs = ttk.Frame(self.nb, padding=8)
        t_settings = ttk.Frame(self.nb, padding=8)
        t_res = ttk.Frame(self.nb, padding=8)
        t_runs = ttk.Frame(self.nb, padding=8)
        for f, name in ((t_designs, "Designs"), (t_settings, "Settings"),
                        (t_res, "Resources"), (t_runs, "Runs")):
            self.nb.add(f, text="  %s  " % name)

        self.build_designs(t_designs, widths=(300, 300, 260), rows=12,
                           buttons="three", titled=False).pack(fill=tk.BOTH,
                                                               expand=True)
        ttk.Label(t_designs, style="Hint.TLabel", justify=tk.LEFT,
                  text="Library / Cell / View 手工填写。这里每一行都会和 Settings "
                       "页上的每一个组合相乘。").pack(anchor=tk.W, pady=(8, 0))

        row = ttk.Frame(t_settings)
        row.pack(fill=tk.BOTH, expand=True)
        self.build_settings(row, compact=False, title=" Extraction settings ",
                            show_formula=False).pack(side=tk.LEFT, fill=tk.BOTH,
                                                     expand=True)
        self._build_count_panel(row).pack(side=tk.LEFT, fill=tk.Y, padx=(10, 0))

        self.build_resources(t_res).pack(fill=tk.X)

        self.build_runs(t_runs, rows=20, titled=False,
                        header_in_title=False).pack(fill=tk.BOTH, expand=True)
        self.build_detail(t_runs).pack(fill=tk.X, pady=(6, 0))

        ttk.Separator(root, orient=tk.HORIZONTAL).pack(fill=tk.X)
        self.build_actionbar(root, show_formula=True, show_dir=False).pack(
            fill=tk.X)
        ttk.Separator(root, orient=tk.HORIZONTAL).pack(fill=tk.X)
        self.build_statusbar(root).pack(fill=tk.X)

    def _build_count_panel(self, parent):
        box = ttk.LabelFrame(parent, text=" Run count ", padding=8, width=250)
        self.axis_lbls = {}
        for i, name in enumerate(AXES):
            ttk.Label(box, text=name).grid(row=i, column=0, sticky=tk.W, pady=1)
            v = ttk.Label(box, text="× 1", style="Count.TLabel", anchor=tk.E)
            v.grid(row=i, column=1, sticky=tk.E, padx=(20, 0))
            self.axis_lbls[name] = v
        ttk.Separator(box, orient=tk.HORIZONTAL).grid(
            row=len(AXES), column=0, columnspan=2, sticky="ew", pady=5)
        ttk.Label(box, text="total", font=_ui.UI_B).grid(
            row=len(AXES) + 1, column=0, sticky=tk.W)
        self.total_lbl = ttk.Label(box, text="0 runs", style="Count.TLabel",
                                   font=("Consolas", 11, "bold"))
        self.total_lbl.grid(row=len(AXES) + 1, column=1, sticky=tk.E)
        ttk.Label(box, style="Hint.TLabel", wraplength=200, justify=tk.LEFT,
                  text="提交前切到 Runs 页看具体每一个 run。").grid(
            row=len(AXES) + 2, column=0, columnspan=2, sticky=tk.W, pady=(8, 0))
        box.columnconfigure(1, weight=1)
        return box

    def on_counts(self, ndesign, ncorner, ntemp, nmode, total):
        for name, n in zip(AXES, (ndesign, ncorner, ntemp, nmode, 1, 1)):
            self.axis_lbls[name].config(text="× %d" % n)
        self.total_lbl.config(text="%d runs" % total)
        self.nb.tab(0, text="  Designs (%d)  " % ndesign)
        self.nb.tab(3, text="  Runs (%d)  " % total)


if __name__ == "__main__":
    _ui.run(Tabbed, "eWave Batch — 1b tabbed")
