"""让本工具的输出在 ASCII-only 的 locale 下**不崩** —— 唯一的职责，一个函数。

为什么单开一个模块而不是塞进 `ewave_batch/__init__.py`：包根必须保持**零 import**
（CLAUDE.md 硬约束 5 的惰性 import 纪律，`tests/test_interfaces.py` 有测试守着）。
连一行 `import sys` 都不该有 —— 那条纪律的价值就在于它没有例外。
"""

from __future__ import annotations

import sys


def ascii_safe_stdio() -> None:
    """把 stdout/stderr 改成编码失败时降级而不是抛异常。**每个入口点第一件事就调它。**

    为什么需要：本工具的输出带中文，而红区的登录 shell 是 csh/tcsh、批处理与 ssh
    上下文里 ``LANG`` 常常是 ``C`` 或干脆没设 —— 此时 ``sys.stdout.encoding`` 是
    ``ansi_x3.4-1968``（纯 ASCII），一个中文字就让 ``print`` 抛 ``UnicodeEncodeError``、
    进程退 1。

    2026-08-18 在本机复现过，判据是机器可判的、不是推测::

        PYTHONIOENCODING=ascii python -m ewave_batch dry-run --self-test   # 修之前 → exit 1

    也就是说：开发机上全绿的闸门，到红区会因为**一个换不掉的 locale** 变红，
    而那正是最没法调试的地方 —— 和 CRLF 那颗雷同一类的「气隙对面才发作」的坑。

    做法：把两个流改成 ``errors="replace"``。终端认 UTF-8 时输出一字不变；只认 ASCII
    时中文降级成 ``?``，信息有损但**进程永不中断**。宁可字糊，不可进程死 ——
    因为这些输出全是诊断信息，糊掉的诊断仍然有用，崩掉的进程什么都不剩。

    幂等，重复调用无副作用。流不支持 ``reconfigure``（被重定向成 StringIO 等）时静默跳过。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            # 流已关闭或被换成不支持重配的对象 —— 不是我们该在这儿处理的问题。
            pass
