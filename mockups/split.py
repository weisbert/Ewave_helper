# -*- coding: utf-8 -*-
"""布局 1c — Split：左边 452px 配置栏，右边整高 Runs 表。

改一个勾选，右边的表和它旁边的 → N 同屏一起变。Runs 能留 ~25 行。
代价：左栏窄，标签得缩写（Temp / Freq sweep / edge · vert · via merge）。

运行：python mockups\\split.py
"""
import os
import sys
import tkinter as tk
from tkinter import ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ui

LEFT_W = 452


class Split(_ui.BaseApp):

    def layout(self):
        root = self.root
        self.build_batchbar(root).pack(fill=tk.X)
        ttk.Separator(root, orient=tk.HORIZONTAL).pack(fill=tk.X)

        body = ttk.Frame(root)
        body.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(body, width=LEFT_W, padding=(8, 8, 6, 4))
        left.pack(side=tk.LEFT, fill=tk.Y)
        left.pack_propagate(False)
        ttk.Separator(body, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y)

        right = ttk.Frame(body, padding=(8, 8, 8, 4))
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.build_designs(left, widths=(120, 130, 110), rows=3).pack(
            fill=tk.X, pady=(4, 6))
        self.build_settings(left, compact=True, show_formula=True).pack(
            fill=tk.X, pady=(0, 6))
        self.build_resources(left, compact=True).pack(fill=tk.X)

        self.build_runs(right, rows=25, titled=False,
                        header_in_title=False).pack(fill=tk.BOTH, expand=True)
        self.build_detail(right).pack(fill=tk.X, pady=(6, 0))

        ttk.Separator(root, orient=tk.HORIZONTAL).pack(fill=tk.X)
        self.build_actionbar(root).pack(fill=tk.X)
        ttk.Separator(root, orient=tk.HORIZONTAL).pack(fill=tk.X)
        self.build_statusbar(root).pack(fill=tk.X)


if __name__ == "__main__":
    _ui.run(Split, "eWave Batch — 1c split")
