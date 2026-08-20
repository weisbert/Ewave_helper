# -*- coding: utf-8 -*-
"""`gui.trace` —— 开发者用的**动作轨迹**：用户点了什么 → 界面做了什么 → 报了什么错。

## 为什么要有这个文件

用户 2026-08-20：「问题太多；太诡异了；你最好做一个开发者用的 log 页面，里面记录
用户点了什么，然后返回了什么报错」。

在这之前，界面上能看到的"日志"只有 **driver 事件**（`_LogWindow`）—— 那一份讲的是
*批次*发生了什么（提交 / 完成 / 失败），一个字都不讲*界面*发生了什么。而这一轮报上来的
三个 bug（复制到第三个组时输入框全灰、复制出来的组和原组"有联系"、原组删不掉）
全部长在界面这一层：

* 现象是**间歇的** —— 同一串点击有时对有时错 ⇒ 事后重放需要知道**那一次**点了什么；
* 出错的那一步**不弹框** —— `_guard()` 把 `EwaveBatchError` 吞成状态栏一行字，
  而状态栏只留最后一条，上一条当场没了；
* Tk 回调里抛出去的异常连状态栏都不进 —— 它落在 stderr 上，而红区那边 GUI 是
  双击起来的，没有人在看 stderr。

⇒ 缺的不是"再加一条日志"，是**一条从点击到结果的完整链路**，而且要能整段拷走。

## 记什么（三类，缺一不可）

| 类 | 例子 | 回答的问题 |
|---|---|---|
| `click` / `ok` / `ERR` / `CRASH` | `do_duplicate_group` → `SpecError: ...` | 我点了什么，它成了没有 |
| `note` | `dialog name='base-copy-2'`、`dialog[error] Cannot remove...` | 中间那些**没有留痕**的分支走了哪条 |
| `state` | `active=... sel=... groups=[...] ovr={...} rows={...}` | 点完之后界面和模型**是不是同一件事** |

`state` 那一行是三个 bug 的公共判据：「组表选中的是哪一行」「模型认为 active 是谁」
「五个覆盖勾选框是什么」「五行控件灰不灰」四件事只要有一件对不上，bug 就在那一拍。
**连续两次一模一样的 `state` 会被折叠**（`recompute()` 每敲一个键跑一次，不折叠的话
真正的动作会被淹掉）。

## 脱敏

用户明说这一份「不要管什么违规问题」（2026-08-20），所以 `Copy all` 拷的是**原文**。
`Copy for sharing` 仍然留着（走 `gui.state.redact`）—— 多一个按钮不增加任何摩擦，
而少一条合规的路会让人走那条不合规的。轨迹本身也只记界面层的东西：组名、轴的取值、
控件状态、异常类型 —— design 的 library/cell 只在用户自己敲进去的时候才会出现。

## 约束

* 纯 stdlib（CLAUDE.md 硬约束 2），不 import tkinter —— 这个文件必须在没有显示的
  ssh 会话里也能 import（`gui/app.py` 的 CLI 分支会碰到它）。
* 环形缓冲，**有上界**：GUI 一开就是一整天，无上界的 list 会把内存吃光。
* 记录本身**绝不抛异常**：一个记日志的东西把被记的东西搞崩，是最差的一种 bug。
"""

from __future__ import annotations

import os
import time
import traceback
from collections import deque
from collections.abc import Callable

DEFAULT_CAPACITY = 4000
"""留最近几条。一条 ~120 字符 ⇒ 满载约 0.5 MB，一整天的点击装得下。"""

TRACE_FILE_ENV = "EWB_TRACE_FILE"
"""设了它就**同时**往这个文件追加一份。

在场的理由：界面被 Tk 卡死 / 进程被杀的时候，内存里那份跟着一起没了，而那恰好是
最想看的一次。设了环境变量就有一份落在盘上，代价是每条一次 open/append。
"""

KIND_WIDTH = 5
"""`kind` 那一列的宽度。四个类别（click/ok/note/state）加上 ERR / CRASH 都塞得下。"""


def _clock() -> str:
    """`hh:mm:ss.mmm` 本地时间。

    只留时分秒毫秒：日期在同一次会话里逐行相同，白占 11 格宽度（同 `_log_line`）。
    毫秒**必须留** —— 「点击和它的结果之间隔了多久」是判断"卡住了还是没跑"的唯一线索。
    """
    now = time.time()
    return time.strftime("%H:%M:%S", time.localtime(now)) + ".%03d" % int((now % 1) * 1000)


def _clip(text: object, cap: int = 400) -> str:
    """一条记录里的自由文本。换行压成 `|`，超长截断并**说明截断了多少**。

    压换行的理由：轨迹是一行一条，多行消息（`SpecError` 全带 `  Next:` 那一行）
    会把"一行 = 一拍"这个唯一的结构毁掉。
    """
    flat = " | ".join(str(text).splitlines())
    if len(flat) <= cap:
        return flat
    return flat[:cap] + " …(+%d chars)" % (len(flat) - cap)


class ActionTrace:
    """一条会话的动作轨迹。**只增不改**，除非用户按 Clear。

    线程安全性：不做锁。GUI 是单线程的（BRIEF §12：`after()` 驱动同一个 `tick()`），
    加锁只会制造"这里到底是不是多线程"的疑问。
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._rows: deque[str] = deque(maxlen=max(50, int(capacity)))
        self._seq = 0
        self._depth = 0
        self._last_state = ""
        self._dropped = 0
        """被环形缓冲挤掉了几条。不说的话"最早的点击不见了"看起来像 bug。"""
        self.on_record: Callable[[], None] | None = None
        """记了一条之后叫一声（Dev log 窗口拿它做实时刷新）。异常一律吞掉。"""
        self._path = os.environ.get(TRACE_FILE_ENV, "").strip()

    # ------------------------------------------------------------------ 写
    def record(self, kind: str, what: str, detail: str = "") -> None:
        """记一条。**绝不抛** —— 记日志的东西不许把被记的东西搞崩。"""
        try:
            self._record(kind, what, detail)
        except Exception:  # noqa: BLE001 - 见 docstring
            pass

    def _record(self, kind: str, what: str, detail: str) -> None:
        if len(self._rows) == self._rows.maxlen:
            self._dropped += 1
        self._seq += 1
        row = "%5d  %s  %-*s %s%s" % (
            self._seq,
            _clock(),
            KIND_WIDTH,
            str(kind)[:KIND_WIDTH],
            "  " * self._depth,
            str(what),
        )
        if detail:
            row += "  " + _clip(detail)
        self._rows.append(row)
        if self._path:
            self._append_to_file(row)
        if self.on_record is not None:
            try:
                self.on_record()
            except Exception:  # noqa: BLE001 - 窗口可能已经关掉了
                pass

    def _append_to_file(self, row: str) -> None:
        try:
            with open(self._path, "a", encoding="utf-8", errors="replace") as handle:
                handle.write(row + "\n")
        except OSError:
            # 写不进去就**别再试** —— 每条都失败一次会把界面拖死，而这只是个副本。
            self._path = ""

    def note(self, what: str, detail: str = "") -> None:
        """一条"这里走了哪个分支"。弹框、对话框的返回值、被吞掉的早返回都走它。"""
        self.record("note", what, detail)

    def state(self, snapshot: str) -> None:
        """界面 + 模型的快照。**与上一条一模一样时不记**（见模块 docstring）。"""
        if snapshot == self._last_state:
            return
        self._last_state = snapshot
        self.record("state", snapshot)

    def error(self, what: str, exc: BaseException, tb: bool = False) -> None:
        """一条异常。`tb=True` 时把 traceback 一起记进去（`CRASH` 用）。"""
        self.record(
            "CRASH" if tb else "ERR",
            what,
            "%s: %s" % (exc.__class__.__name__, exc),
        )
        if not tb:
            return
        for line in traceback.format_exception(type(exc), exc, exc.__traceback__):
            for part in line.rstrip().splitlines():
                self.record("CRASH", "  " + part.rstrip())

    def call(self, what: str) -> "_Call":
        """`with trace.call("do_x"):` —— 进去记 `click`，出来记 `ok` / `ERR` / `CRASH`。"""
        return _Call(self, what)

    def clear(self) -> None:
        self._rows.clear()
        self._seq = 0
        self._depth = 0
        self._last_state = ""
        self._dropped = 0

    # ------------------------------------------------------------------ 读
    def lines(self) -> list[str]:
        rows = list(self._rows)
        if self._dropped:
            rows.insert(0, "…… 前 %d 条已被环形缓冲挤掉（只留最近 %d 条）" % (self._dropped, self._rows.maxlen))
        return rows

    def document(self, header: "list[str] | tuple[str, ...]" = ()) -> str:
        rows = self.lines() or ["(nothing recorded yet - click something)"]
        return "\n".join(list(header) + rows) + "\n"

    def __len__(self) -> int:
        return len(self._rows)


class _Call:
    """一次被跟踪的调用。嵌套时靠 `_depth` 缩进（`on_group_select` → `switch_group`）。"""

    def __init__(self, trace: ActionTrace, what: str) -> None:
        self._trace = trace
        self._what = what
        self._t0 = 0.0

    def __enter__(self) -> "_Call":
        self._trace.record("click", self._what)
        self._trace._depth += 1
        self._t0 = time.time()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001 - 上下文管理器协议
        self._trace._depth = max(0, self._trace._depth - 1)
        ms = int((time.time() - self._t0) * 1000)
        if exc is None:
            self._trace.record("ok", self._what, "%d ms" % ms)
            return False
        self._trace.error("%s (%d ms)" % (self._what, ms), exc, tb=True)
        return False  # 照抛：吞异常正是这个文件要治的病，不是它要犯的


def wrap(trace: ActionTrace, name: str, func: Callable) -> Callable:
    """把一个方法包成"进去记一条、出来记一条"。

    保留 `__name__` / `__doc__`：`functools.wraps` 在绑定方法上一样能用，而界面别处
    有按 `__doc__` 显示说明的地方（`_add_menu_item` 的置灰判据读的是 handler 本身）。
    """

    def traced(*args: object, **kwargs: object) -> object:
        with trace.call(name):
            return func(*args, **kwargs)

    try:
        traced.__name__ = getattr(func, "__name__", name)
        traced.__doc__ = getattr(func, "__doc__", None)
    except (AttributeError, TypeError):  # pragma: no cover - 内建/槽方法
        pass
    return traced
