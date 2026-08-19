"""仓库根的薄壳 —— 只做一件事：把命令行转给 `ewave_batch.cli`。

```
python cli.py dry-run my_spec.yaml       # 和 `python -m ewave_batch dry-run …` 完全等价
python cli.py --gui                      # 开界面
```

## 为什么要这一层

部署到红区的是**一棵目录树**（`deploy/` 打包，没有 `pip install`），用户 `cd` 进去之后
最顺手的就是 `python cli.py …`。而 `python -m ewave_batch …` 要求当前目录正好是包的父目录 ——
两条路都要能走，且**行为必须一模一样**，否则"我照文档敲的怎么不一样"会变成常见提问。

所以这里**没有第二份参数解析**：`build_parser` 只有一个，在 `ewave_batch.cli` 里。
本文件把 `argv` 原样传过去，退出码原样传回来。

## 三条纪律

1. **零业务逻辑。** 这里多写一行判断，两条入口就开始分叉。
2. **模块顶层不许 import `tkinter` 或 `gui.*`**（CLAUDE.md 硬约束 5）——
   无 `$DISPLAY` 的纯 ssh 会话里这条命令必须照常可用。GUI 由 `ewave_batch.cli.main`
   在 `--gui` 分支里就地 import。
3. **签名冻结**：`main(argv=None) -> int`，与 `ewave_batch.cli.main`、`gui.frames.*.main`
   共用（`docs/INTERFACES.md`）。返回退出码，**不 `sys.exit`**。

⚠️ 本文件叫 `cli.py`，和 `ewave_batch/cli.py` **同名不同模块**（`cli` vs `ewave_batch.cli`）。
`ewave_batch` 内部一律用相对 import，所以两者不会互相遮蔽。
"""

from __future__ import annotations

from collections.abc import Sequence

from ewave_batch.cli import main as _main


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口。`ewave_batch.cli.main`、顶层 `cli.main`、`gui.frames.*.main` 共用这个签名。

    转发，不加工。`ascii_safe_stdio()` 由 `ewave_batch.cli.main` 在自己的第一行调
    —— 那是**入口点唯一的一处**，在这里再调一遍只会多一份将来会漂的代码。
    """
    return _main(argv)


if __name__ == "__main__":  # pragma: no cover - 进程入口
    import sys

    sys.exit(main())
