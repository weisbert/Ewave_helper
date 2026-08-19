"""布局 1c — Split：左边一条配置栏，右边整高 Runs 表。**这是默认布局。**

选它的理由（`mockups/README.md` 的对照表）：三版里**唯一**能让"勾选"和"12 行 run"
同屏的。改一个勾选，右边的表和它旁边的 `-> N` 一起变 —— 而"改设定看不见后果"
正是原生 GUI 那三个痛点里最贵的一个。代价是左栏窄，标签得缩写。

本文件**只负责摆放**：控件和逻辑全在 `gui/_ui.py`，桥在 `gui/state.py`。
frame 里一行业务逻辑都没有（`docs/INTERFACES.md`「`gui/frames/*.py` 的三条硬要求」）。

自检（不弹窗、建完控件树就退）：`EWB_SMOKE=1 python -m gui.frames.split`
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Sequence
from tkinter import ttk

from .. import _ui

LAYOUT_NAME = "split"
"""这一版的布局名，必须是 `gui.app.LAYOUTS` 里的一个。"""

SECTIONS: tuple[str, ...] = (
    "batchbar",
    "designs",
    "settings",
    "resources",
    "runs",
    "detail",
    "actionbar",
    "statusbar",
)
"""三版**必须暴露同一组**顶层构件 —— 这是「三个 agent 各写各的、界面手感不一致」
唯一的机器判据（`tests/test_gui_frames.py`）。

名字对应共用层的 `build_<section>`，和 `mockups/_ui.py` 里那八个 `build_*` 一一对上。
布局只决定它们摆在哪、留几行，**不决定有没有**：少一个 section 就是三版分岔的开始。

⚠️ 这个常量**不在冻结面里**（`docs/INTERFACES.md` 只冻了 `LAYOUT_NAME` /
`build_frame` / `main`）。它是并行三版之间自发长出来的约定，已写进本阶段的
`interface_change_requests` —— 该进 `FROZEN` 才有人替它盯漂移。
"""

LEFT_WIDTH = 452
"""左栏宽度（px）。照设计稿 1c；`pack_propagate(False)` 把它钉住，
否则里面的 Settings 一撑，右边的 Runs 表就没地方了。"""

RUN_ROWS = 25
"""右栏能留几行 run。1c 的全部优势就在这个数字上（1a 只有 ~9 行）。"""


class SplitApp(_ui.BaseApp):
    """1c：batchbar / 左配置 / 右 Runs / actionbar / statusbar。"""

    def layout(self) -> None:
        root = self.frame

        self.build_batchbar(root).pack(fill=tk.X)
        ttk.Separator(root, orient=tk.HORIZONTAL).pack(fill=tk.X)

        body = ttk.Frame(root)
        body.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(body, width=LEFT_WIDTH, padding=(8, 8, 6, 4))
        left.pack(side=tk.LEFT, fill=tk.Y)
        left.pack_propagate(False)
        ttk.Separator(body, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y)

        right = ttk.Frame(body, padding=(8, 8, 8, 4))
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.build_designs(left, widths=(120, 130, 110), rows=3).pack(fill=tk.X, pady=(4, 6))
        self.build_settings(left, compact=True, show_formula=True).pack(fill=tk.X, pady=(0, 6))
        self.build_resources(left, compact=True).pack(fill=tk.X)

        self.build_runs(right, rows=RUN_ROWS, titled=False, header_in_title=False).pack(
            fill=tk.BOTH, expand=True
        )
        self.build_detail(right).pack(fill=tk.X, pady=(6, 0))

        ttk.Separator(root, orient=tk.HORIZONTAL).pack(fill=tk.X)
        self.build_actionbar(root).pack(fill=tk.X)
        ttk.Separator(root, orient=tk.HORIZONTAL).pack(fill=tk.X)
        self.build_statusbar(root).pack(fill=tk.X)


def build_frame(parent: object, bridge: object) -> object:
    """建这一版布局的主 frame。只通过 `bridge`（`model.GuiBridgeProtocol`）跟核心说话。"""
    return _ui.build(SplitApp, parent, bridge)


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m gui.frames.split` —— 直接以这一版起界面。"""
    return _ui.frame_main(LAYOUT_NAME, argv)


if __name__ == "__main__":  # pragma: no cover - 手工/冒烟入口
    import sys

    sys.exit(main(sys.argv[1:]))
