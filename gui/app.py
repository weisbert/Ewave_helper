"""`gui.app` —— 起 GUI。**本文件在 import 时不许碰 tkinter。**

无 `$DISPLAY` 的纯 ssh 会话里 CLI 必须可用（CLAUDE.md 硬约束 5），而 `cli.py` 要能
`from gui.app import launch` 之后才决定起不起界面 —— 所以 tkinter 和三版 frame 都在
`launch()` 的**函数体内**才 import。这条纪律的破坏是静默的、只在红区发作，
`tests/test_gui_common.py::LazyImport` 盯着它。

```
gui/
  app.py        ← LAYOUTS / launch()（本文件，零 tkinter import）
  state.py      ← GuiState：GUI ↔ driver 的桥（零 tkinter import）
  _ui.py        ← 三版共用的控件层（import tkinter，只被 frame 和 launch 用）
  frames/
    stacked.py tabbed.py split.py   ← 各自只负责"把 section 摆在哪"
```
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Sequence

LAYOUTS: tuple[str, ...] = ("stacked", "tabbed", "split")
"""三版布局。**恰好 3 个**，顺序 = 设计稿里 1a / 1b / 1c 的顺序。"""

DEFAULT_LAYOUT = "split"
"""默认 1c split（左配置右大表）。`mockups/README.md` 推荐它的理由是唯一能让
「勾选」和「12 行 run」同屏 —— 改设定时看得见后果，那正是本工具要解决的事。"""

SMOKE_ENV = "EWB_SMOKE"
"""`EWB_SMOKE=1` = headless 冒烟：建完整棵控件树就退，不进主循环。
`scripts/check.sh` 第 5 步跑的就是它（`EWB_SMOKE=1 python -m gui.frames.split`）。"""

WINDOW_TITLE = "eWave Batch"

_EXIT_OK = 0
_EXIT_USAGE = 2
_EXIT_NO_GUI = 3
"""tkinter 缺失 / 没有显示 —— **降级不是失败**（doctor 的 tier 3），所以单独一个码。"""


def smoke_enabled() -> bool:
    """现在是不是 headless 冒烟。每次读环境变量 —— 测试要能在 import 之后再打开它。"""
    return os.environ.get(SMOKE_ENV) == "1"


def launch(layout: str = "split") -> int:
    """起 GUI。`layout` ∈ `gui.app.LAYOUTS`（`"stacked"` / `"tabbed"` / `"split"`，默认 split）。

    tkinter 在**这个函数体内**才 import。没有 `$DISPLAY` / 没装 tkinter →
    打印一句人话 + 返回非 0，**不抛 traceback**（tier 3 缺失是降级不是失败）。

    `EWB_SMOKE=1` 时的两处不同：
    * 窗口 `withdraw()`（不闪一下），建完就 `destroy()`，`mainloop` 立刻返回；
    * **tkinter 缺失 / 起不了窗口时返回 0 并打印一行 `skip` + 原因** ——
      冒烟测的是"控件树建得起来"，而"这台机器有没有显示"是平台能力，不是本项目的红。
      注意范围：只有 `Tk()` **本身**建不起来才算平台原因；控件树建到一半炸了照样退非 0
      （那才是我们要抓的东西）。
    """
    from ewave_batch._stdio import ascii_safe_stdio

    ascii_safe_stdio()

    if layout not in LAYOUTS:
        print(f"unknown layout {layout!r}; expected one of: {', '.join(LAYOUTS)}")
        return _EXIT_USAGE

    smoke = smoke_enabled()
    try:
        import tkinter as tk
    except ImportError as exc:  # tier 3：红区装了 tkinter，公开克隆者不一定
        return _no_gui(f"tkinter is not available ({exc}); the CLI works without it", smoke)

    try:
        root = tk.Tk()
    except tk.TclError as exc:  # 无 $DISPLAY
        return _no_gui(f"cannot open a display ({exc}); use the CLI over plain ssh", smoke)

    try:
        root.title(f"{WINDOW_TITLE} - {layout}")
        frame_module = importlib.import_module(f"{__package__}.frames.{layout}")
        from .state import GuiState

        bridge = GuiState()
        # ★ 上次那份设定（用户 2026-08-24：「load 过一次，下次启动不用再 load」）。
        #   **在 build_frame 之前**：界面变量是建控件时从 bridge 灌的，
        #   读晚一步就得再走一遍整份重灌，而那条路（`_init_vars_from_bridge`）
        #   是给 Open spec 用的、有它自己的时机。
        #   读不到 / 读坏了一律当没有，界面照常起来 —— 一份坏掉的状态文件
        #   不该让人没法干活（同 `site.local.sh` 那条规矩）。
        #   ⚠️ 存的只有**设定**，官方目录里的坐标（尤其端口表）每次现读，
        #      理由写在 `GuiState.save_session` 上。
        if not smoke:
            # 冒烟不读：那会让自检的结果取决于这台机器上恰好留着什么设定，
            # 而自检要证明的是"控件树建得起来"。
            bridge.load_session()
        frame = frame_module.build_frame(root, bridge)
        frame.pack(fill="both", expand=True)
        if smoke:
            root.withdraw()
            root.after(0, root.destroy)
        root.mainloop()
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001 - 已经销毁过了，收尾而已
            pass
    return _EXIT_OK


def _no_gui(reason: str, smoke: bool) -> int:
    """tkinter / 显示缺失时的统一出口。冒烟下是 skip（退 0），平时是降级（退非 0）。"""
    if smoke:
        print(f"skip  GUI smoke: {reason}")
        return _EXIT_OK
    print(f"GUI unavailable: {reason}")
    return _EXIT_NO_GUI


def build_parser() -> "object":  # noqa: F821 - 注解是字符串，不在模块顶层 import argparse
    """`--layout` 的解析器。单独一个函数是为了让测试直接验参数面，不用起进程。"""
    import argparse

    parser = argparse.ArgumentParser(
        prog="ewave-batch-gui",
        description="eWave batch driver - tkinter front end",
    )
    parser.add_argument(
        "--layout",
        choices=LAYOUTS,
        default=DEFAULT_LAYOUT,
        help=f"which layout to open (default: {DEFAULT_LAYOUT})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m gui.app [--layout split]`。返回进程退出码，**不 `sys.exit`**。"""
    from ewave_batch._stdio import ascii_safe_stdio

    ascii_safe_stdio()
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    return launch(args.layout)


if __name__ == "__main__":  # pragma: no cover - 手工入口
    import sys

    sys.exit(main())
