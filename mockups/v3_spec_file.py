# -*- coding: utf-8 -*-
"""界面草图 v3：批次写在文件里，GUI 只当运行监控台。

设定不在界面上点，而是写在一个 YAML 里（和你 Auto_ext 的 tasks.yaml 一个路子）。
GUI 负责：选文件 / 校验 / dry-run / 提交 / 看进度 / 看日志。

好处：批次本身就是可 diff、可留档、可发给同事的一个文件。
代价：每次都要编辑文本，改一个温度也要动文件。

运行：python mockups/v3_spec_file.py
"""
import os
import sys
import tkinter as tk
from tkinter import ttk, messagebox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C

SMOKE = os.environ.get("MOCKUP_SMOKE") == "1"

SAMPLE = """\
# 一个批次 = 一个文件。填几个值就跑几个 run。
batch: ind_top_a_corner_sweep
why:  "评审要求补 full wave 对比"      # provenance：半年后你还知道为什么这么跑

designs:
  - {library: MY_RF_LIB, cell: IND_TOP_A, view: layout_em_sim}
  - {library: MY_RF_LIB, cell: TRACE_B,   view: layout}

settings:
  corner:      [typical]
  temperature: [-40, 55, 125]
  mode:        [quasi-static, full-wave]     # 两个都写 = 跑两遍做对比
  frequency:   {sweep: adaptive, start: 0, stop: 40, step: 0.1}
  mesh:        {edge: 0.4, vertical: 0.4, via_merge: 0.4}
  equal_current: [on]
  tolerance:   {relative: 1e-05, relative_current: 0.001}

resources:
  submit: dsub -A <account> -q <queue> -R "cpu=20;mem=100000"
  # --parallel 自动跟随 cpu=

# 2 designs x 1 corner x 3 temp x 2 mode = 12 runs
"""

FAKE_LOG = """\
[spec    ] ok  -  12 runs, 2 designs
[strmout ] IND_TOP_A ... ok (12 s)
[strmout ] TRACE_B   ... ok (9 s)
[submit  ] 12 jobs sent
[ewave   ] IND_TOP_A typical_-40_0  Meshing ... done (31 s)
[ewave   ] IND_TOP_A typical_-40_0  Building matrix ... done (92 s)
[verify  ] IND_TOP_A typical_-40_0  non-empty, 17 ports  ->  done
"""


class App(object):
    def __init__(self, root):
        self.root = root
        root.title("eWave Batch  -  mockup v3 (spec file + monitor)")
        root.geometry("1120x780")
        self.runs = []
        self.varying = set()
        self.timer = None
        self.step = 0

        bar = ttk.Frame(root, padding=(10, 8, 10, 4))
        bar.pack(fill=tk.X)
        ttk.Label(bar, text="Spec").pack(side=tk.LEFT)
        self.path = tk.StringVar(value="batches/ind_top_a_corner_sweep.yaml")
        ttk.Entry(bar, textvariable=self.path, width=58).pack(side=tk.LEFT, padx=6)
        for t in ("Browse...", "Reload", "Open in editor"):
            ttk.Button(bar, text=t, command=lambda x=t: self.stub(x)).pack(
                side=tk.LEFT, padx=2)

        pane = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
        pane.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)

        left = ttk.LabelFrame(pane, text=" spec ", padding=6)
        self.text = tk.Text(left, wrap=tk.NONE, width=58, font=("Consolas", 9))
        self.text.pack(fill=tk.BOTH, expand=True)
        self.text.insert("1.0", SAMPLE)
        pane.add(left, weight=1)

        right = ttk.Frame(pane)
        pane.add(right, weight=1)

        rl = ttk.LabelFrame(right, text=" runs ", padding=6)
        rl.pack(fill=tk.BOTH, expand=True)
        cols = ("design", "corner", "temp", "mode", "status", "wall")
        self.tree = ttk.Treeview(rl, columns=cols, show="headings", height=12)
        for c, w in zip(cols, (120, 75, 55, 95, 85, 55)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor=tk.W)
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.tag_configure("done", foreground="#0a6")
        self.tree.tag_configure("failed", foreground="#c00")
        self.tree.tag_configure("running", foreground="#06c")
        self.tree.bind("<Button-3>", self.popup)
        self.menu = tk.Menu(root, tearoff=0)
        for lab in ("Open log", "Open output dir", "Copy ewave command"):
            self.menu.add_command(label=lab, command=lambda t=lab: self.stub(t))

        ll = ttk.LabelFrame(right, text=" log ", padding=6)
        ll.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.log = tk.Text(ll, height=10, wrap=tk.NONE, font=("Consolas", 9))
        self.log.pack(fill=tk.BOTH, expand=True)
        self.log.insert("1.0", FAKE_LOG)

        bar2 = ttk.Frame(root, padding=(10, 0, 10, 10))
        bar2.pack(fill=tk.X)
        for t, cb in (("Check spec", self.check), ("Dry-run",
                                                   lambda: self.stub("只拼命令不提交")),
                      ("Submit", self.submit), ("Cancel", self.cancel),
                      ("Resume", lambda: self.stub("只补没成的"))):
            ttk.Button(bar2, text=t, width=11, command=cb).pack(side=tk.LEFT, padx=3)
        self.foot = ttk.Label(bar2, text="", foreground="#555")
        self.foot.pack(side=tk.LEFT, padx=16)
        self.check()

    def check(self):
        self.runs = C.expand(C.DEFAULT_DESIGNS, ["typical"], ["-40", "55", "125"],
                             C.MODES, [("0.4", "0.4", "0.4")],
                             [("1e-05", "0.001")], [True])
        self.varying = C.varying_axes(["typical"], ["-40", "55", "125"], C.MODES,
                                      [1], [1], [1])
        self.refresh()
        self.foot.config(text="spec ok  -  %d runs  ->  %s/ind_top_a_corner_sweep/"
                         % (len(self.runs), C.BATCH_ROOT))

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(self.runs):
            self.tree.insert("", tk.END, iid=str(i),
                             values=(r.design[1], r.corner, r.temp, r.mode,
                                     r.status, r.wall), tags=(r.status,))

    def popup(self, ev):
        iid = self.tree.identify_row(ev.y)
        if iid:
            self.tree.selection_set(iid)
            self.menu.tk_popup(ev.x_root, ev.y_root)

    def submit(self):
        for r in self.runs:
            r.status = "pending"
            r.wall = ""
        self.step = 0
        self.tick()

    def cancel(self):
        if self.timer:
            self.root.after_cancel(self.timer)
            self.timer = None
        for r in self.runs:
            if r.status in ("pending", "running"):
                r.status = "ready"
        self.refresh()

    def tick(self):
        busy = False
        for r in self.runs:
            if r.status == "running":
                mins = int(r.wall[:-1]) + 4
                r.wall = "%dm" % mins
                if mins >= 12:
                    r.status = "failed" if self.runs.index(r) == 3 else "done"
                busy = True
        if len([r for r in self.runs if r.status == "running"]) < 3:
            for r in self.runs:
                if r.status == "pending":
                    r.status = "running"
                    r.wall = "0m"
                    self.log.insert(tk.END, "[ewave   ] %s %s  running\n"
                                    % (r.design[1], r.leaf()))
                    self.log.see(tk.END)
                    busy = True
                    break
        self.step += 1
        self.refresh()
        if busy and self.step < 300:
            self.timer = self.root.after(600, self.tick)

    def stub(self, what):
        messagebox.showinfo("mockup",
                            "这里会：%s\n\n（这只是界面草图，后端没接）" % what)


def main():
    root = tk.Tk()
    if SMOKE:
        root.withdraw()
    App(root)
    if SMOKE:
        root.after(700, root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()
