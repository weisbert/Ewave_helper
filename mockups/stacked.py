# -*- coding: utf-8 -*-
"""布局 1a — Stacked：单窗口纵向堆叠。

Batch → Designs → Settings → Resources → Runs 全部同屏。
乘法公式挂在 Settings 组的标题上，勾一下和它的后果隔不到几厘米。
代价：Runs 表只剩 ~9 行。

运行：python mockups\\stacked.py
"""
import os
import sys
import tkinter as tk
from tkinter import ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ui


class Stacked(_ui.BaseApp):

    def layout(self):
        root = self.root
        self.build_batchbar(root).pack(fill=tk.X)
        ttk.Separator(root, orient=tk.HORIZONTAL).pack(fill=tk.X)

        body = ttk.Frame(root, padding=(8, 6))
        body.pack(fill=tk.BOTH, expand=True)

        self.build_designs(body, widths=(250, 250, 210), rows=2).pack(
            fill=tk.X, pady=(4, 6))
        self.build_settings(body, compact=False, show_formula=False).pack(
            fill=tk.X, pady=(0, 6))
        self.build_resources(body).pack(fill=tk.X, pady=(0, 6))
        self.build_runs(body, rows=9).pack(fill=tk.BOTH, expand=True)
        self.build_detail(body).pack(fill=tk.X, pady=(6, 0))
        self.build_actionbar(body).pack(fill=tk.X, pady=(4, 0))

        ttk.Separator(root, orient=tk.HORIZONTAL).pack(fill=tk.X)
        self.build_statusbar(root).pack(fill=tk.X)


if __name__ == "__main__":
    _ui.run(Stacked, "eWave Batch — 1a stacked")
