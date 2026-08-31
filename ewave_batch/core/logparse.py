"""`ewave_batch.core.logparse` —— eWave 的日志 → `model.LogFacts`（P4）。

抽出来的六件事：**收敛状态 / 墙钟 / 峰值内存 / 端口数 / 成败 / 实际算了几个频点**。

## 这个模块的存在理由：`done` 不等于成功

MVP 在红区实测到三条「失败信号不可靠」（`PROJECT_BRIEF.md` §10「三条失败信号合起来
就是调度器的验收契约」）：

| 现象 | 实测 | 对本模块的要求 |
|---|---|---|
| 崩溃时退出码 | `ewave exit=0` | 别指望调用方拿退出码兜底 |
| 写失败 | `eresist` 打印 "Execute eresist done."，却留 0 字节文件 | **日志里的 "done" 不许被当成成功** |
| 错误信息 | 配额爆了，一行错都没有 | **"没报错" != 成功** |

⇒ `LogFacts.ok` 是**日志的说法**，不是判据：

* 有崩溃指纹 / `[error]` 标签 → `ok=False`（**失败是硬的**，`Execute … done.` 盖不过它）；
* 三个阶段都打了 done、且一条失败线索都没有 → `ok=True`，意思仅仅是"日志没自曝失败"；
* 其余 → `None`（"日志没说"，**不是** False）。

`ok=True` 与"这个 run 成了"之间隔着 `core.layout.verify_run_outputs`
（**存在 + 非空 + 端口数对**）。BRIEF §10 里那次事故的日志与成功那次**逐字相同** ——
只有产物验收能分开它们。本模块不假装自己能。

## 解析不到就留 `None`

`LogFacts` 的 docstring 写死了这条：**别用 0 冒充**，0 秒和"没测到"是两回事。
所有数值字段都遵守它，`ewave_version` 用空串表示"没测到"（它的类型是 `str`）。

## 哪些行格式有据、哪些是猜的

**这件事必须写在源码里**，因为本机永远拿不到真实日志（CLAUDE.md 硬约束 3），
读代码的人无从分辨。规矩：**每条正则都注明出处；没出处的一律标「未经真实日志验证」。**
`tests/fixtures/ewave_log_synthetic/README.md` 有同一张表的完整版。

有据的（出处逐条注在下面的正则旁）：`Calculated on N points.`、`Execute X done.`、
`All Ports size is N:` + `Port:` + `Ground:`、boost 崩溃三行、`expected memory: N GB`、
ANSI 色码形状、`.sNp` 的 `! Port[N] = <pin> | ref`。

猜的（正则旁标了「未经真实日志验证」）：墙钟行、峰值内存行、CPU 占用行、收敛行、
版本抬头、`[warning]` 标签、"请求了几个频点"那一行。
**它们不匹配时字段就留 `None`** —— 猜错的代价是少一个字段，不是给出错的数字。

本文件零站点标识符（硬约束 1b）：日志里的 pin 名 / 路径 / cell 名全部**只被数、不被写**；
一个真实取值都不出现在源码和 fixture 里（fixture 的数值全是编的，见那份 README）。

只读，不改任何文件。
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from ..model import LayerStack, LogFacts
from .layout import port_count_from_suffix

__all__ = [
    "EWAVE_LOG_NAME",
    "EMSOLVER_LOG_NAME",
    "LOG_FILE_NAMES",
    "RUN_LOG_PREFIX",
    "EWAVE_PHASES",
    "MAX_MESSAGES",
    "MAX_LOG_BYTES",
    "MAX_SNP_SCAN_LINES",
    "strip_ansi",
    "parse_ewave_log",
    "parse_emsolver_log",
    "parse_memory_estimate_mb",
    "merge_log_facts",
    "parse_log_files",
    "parse_run_logs",
    "run_log_files",
    "collect_log_files",
    "read_log_tail",
    "parse_port_order",
    "parse_layer_stack",
    "LAYER_BASIS_HEADER",
    "LAYER_VIA_MARKER",
    "TAIL_BYTES",
]


# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

EWAVE_LOG_NAME = "ewave.log"
"""eWave 主日志的默认名。出处：`ewave --help` 的 `--log`（"The default is saved as
ewave.log"，`references/probes/ewave_probe_*.txt`）。"""

EMSOLVER_LOG_NAME = "emsolver.log"
"""求解器日志。出处：`references/probes/workdir_tree.txt` 里 `<corner>_<temp>/` 那层的实测清单。"""

LOG_FILE_NAMES = (EWAVE_LOG_NAME, EMSOLVER_LOG_NAME, "mesh.log", "emesh_mrg.log")
"""一个成功 run 的输出目录里会有的 4 个日志。出处：BRIEF §5「官方流程的既有布局」实测清单。
**顺序就是 `parse_run_logs` 的优先级**（`merge_log_facts` 先到先得）。"""

RUN_LOG_PREFIX = "run"
"""我们自己捕获的那份 stdout（`model.RUN_LOG_TEMPLATE` = `run_{stem}.log`）。
它落在 run 目录里而不是 eWave 那层子目录里，且崩溃时往往只有它还在
（MVP 的诊断脚本正是 grep 这一份），所以也扫。"""

EWAVE_PHASES = ("emesh", "eresist", "emsolver")
"""三个阶段的名字。出处：BRIEF §10 根因链（`Execute eresist done.` 逐字引用）
+ `mvp/redzone/go_workarea.sh` 抬头。**三个都打了 done 才算"日志说成了"**。"""

MAX_MESSAGES = 50
"""`errors` / `warnings` 各自最多留几条。日志可能刷屏，而这两个字段会被写进
`batch.json`（`layout._log_facts_to_dict`）—— 不设上限就是让状态文件被一份坏日志撑爆。
截断时会追加一条说明，**不静默丢**。"""

MAX_LOG_BYTES = 16 * 1024 * 1024
"""单份日志最多读多少字节。超了只读**末尾**这么多（结论都在末尾）并留一条 warning。
实测 `ewave.log` 5.8 KB / `emsolver.log` 8.8 KB（`references/probes/mvp_step4_verify_*.txt`
里的 ls），所以这个上限正常永远碰不到；它防的是 mesh 类日志刷屏。"""

MAX_SNP_SCAN_LINES = 50000
"""`parse_port_order` 最多扫多少行。几百频点 x 十几端口的 `.sNp` 约 7000 行数值块，
留足余量；再多就是文件不对，没必要把整份读完。"""

TAIL_BYTES = 64 * 1024
"""`read_log_tail` 默认读末尾多少字节。**与 `MAX_LOG_BYTES` 不是一回事**：
那个是"解析事实时最多读多少"（16 MB，防 mesh 日志刷屏），这个是"给人看多少"。

64 KB 约 700 行，装得下一次崩溃的全部现场，又不会让界面每一拍去搬几 MB 文本 ——
它会被 `Output log` 那扇窗按轮询间隔反复调用（`gui._ui._RunLogWindow`）。"""


# --------------------------------------------------------------------------
# ANSI
# --------------------------------------------------------------------------

_ANSI = re.compile(
    "\x1b(?:"
    r"\[[0-?]*[ -/]*[@-~]"  # CSI：ESC [ 参数 中间字节 结束字节（色码是它的子集）
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC：ESC ] … BEL 或 ST（设置窗口标题那类）
    r"|[@-Z\\-_]"  # 两字符转义：ESC + 单个字节
    ")"
)
r"""ANSI 转义序列。**三个分支都以 `\x1b` 开头** —— 这是本正则唯一重要的性质：
没有 ESC 的字符一个都动不了。

出处：生产命令行末尾恒接 `| sed -r 's/\x1B[[0-9;]*m//g'`
（`references/probes/run_ewave_typical_*.sh` 逐字）。也就是说**色码是实测存在的**，
而 eWave 即使 `--nogui` 也照打。我们不靠管道剥（argv 要干净，见 `core.template`），
改在这儿剥。

比那条 sed 多认 CSI 的非 `m` 结尾、OSC 和两字符转义 —— 那是**超集**，
不会多吃普通字符。分支顺序（CSI → OSC → 两字符）不能调：
两字符那个类里含 `]`，放前面会把 OSC 的头吃掉、把标题文本留在行里。
"""


def strip_ansi(text: str) -> str:
    r"""剥 ANSI 转义序列。eWave 即使 `--nogui` 也输出颜色，生产脚本靠管道 sed 剥 ——
    我们不用管道（argv 要干净），改在这里剥。

    **只去掉转义序列，绝不动别的字符。** 这一条是承重的：日志里遍地是 `[info]`、
    `All Ports size is 4:`、`Wall Clock Time: 111 s`，要是顺手吃掉了 `[` 或数字，
    后面每一条正则都会跟着错，**而且错得很安静**（字段变 `None`，看起来像"日志里没有"）。
    所以实现上只有一条规则：**每个被删掉的片段都必须以 `\x1b` 开头**。

    没有 ESC 时原样返回（连正则都不跑）。
    """
    if "\x1b" not in text:
        return text
    return _ANSI.sub("", text)


# --------------------------------------------------------------------------
# 行级正则
# --------------------------------------------------------------------------

_RE_CALCULATED = re.compile(r"\bcalculated on\s+(\d+)\s+points?\b", re.IGNORECASE)
"""求解器**真算过**的频点数。出处：BRIEF §10 逐字引用 `Calculated on <N> points.`
（D13 那一行、P4a 关闭那一行、C 的运行数据 `Calculated on 1 points.`）。
注意 eWave 的复数是错的（"1 points"），所以写成 `points?`。"""

_RE_REQUESTED = re.compile(
    r"\b(?:sweep on|sweeping|total)\s+(\d+)\s+(?:frequency\s+)?points?\b", re.IGNORECASE
)
"""**未经真实日志验证。** "一共要扫几个点"这一行的措辞我没有证据。
知道的只有事实本身（BRIEF §10：官方那次的 401 点里只真算了一小部分）。
不匹配 → `freq_points_requested` 留 `None`，不影响别的字段。"""

_RE_PORTS_SIZE = re.compile(r"\ball ports size is\s+(\d+)", re.IGNORECASE)
"""端口数。出处：`mvp/redzone/step2_memestimate.sh` 的注释「红区 step0 实测到的格式」::

    [info] All Ports size is N:
    Port: a b c ...
    Ground:

那个脚本把它当**闸门判据**用（和官方 `-p` 列表逐位比），所以这行格式是被真实日志验过的。"""

_RE_PORT_LINE = re.compile(r"^Port:\s*(.*)$")
"""紧跟在上一行后面的端口名列表。**大小写敏感、锚在行首** —— 与那个脚本的
`grep -E '^Port:'` 同一条规则。不锚行首的话 `All Ports size is` 自己就会命中。"""

_RE_EXECUTE_DONE = re.compile(r"\bexecute\s+([A-Za-z_][A-Za-z0-9_]*)\s+done\b", re.IGNORECASE)
"""阶段完成标记。出处：BRIEF §10 根因链逐字引用 `Execute eresist done.`
（那次**写失败了照样打印**，见本模块抬头）。`sched.fake._LOG_DONE` 用的是同三行。"""

_RE_WALL_HMS = re.compile(
    r"\b(?:wall clock time|elapsed(?:\s+time)?|total time)\s*[:=]\s*(\d+):([0-5]?\d):([0-5]?\d)\b",
    re.IGNORECASE,
)
"""**未经真实日志验证。** 关键词 `Wall Clock Time:` 来自 MVP 的诊断脚本
（`mvp/redzone/diag_ab.sh` 等四处 `grep -iE '…|Wall Clock Time:'`），
但那几条 grep 是**先写的**，没有哪份粘回来的输出确认过冒号后面长什么样。
BRIEF §10 只给了数值（墙钟 （真值不复述，见 BRIEF §10）），没给行。
⇒ 两种写法都试：先 `H:MM:SS`，不中再试「数字 + 单位」。"""

_RE_WALL_UNIT = re.compile(
    r"\b(?:wall clock time|elapsed(?:\s+time)?|total time)\s*[:=]\s*"
    r"([0-9]+(?:\.[0-9]+)?)\s*"
    r"(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours)?\b",
    re.IGNORECASE,
)
"""**未经真实日志验证**（同上）。单位缺省按**秒**算（BRIEF 报的墙钟都是秒）。
必须在 `_RE_WALL_HMS` **之后**试：`00:01:51` 会被这条当成 `0`。"""

_RE_PEAK_MEM = re.compile(
    r"\b(?:peak|maximum|max)\s+memory(?:\s+usage)?\s*[:=]?\s*"
    r"([0-9]+(?:\.[0-9]+)?)\s*([KMGT])?i?B\b",
    re.IGNORECASE,
)
"""**未经真实日志验证。** BRIEF §10 给了值（峰值 （真值不复述））没给行。
**必须带 `peak|maximum|max` 前缀** —— 不然会一把吃掉 `expected memory: N GB`
（那是 `--memEstimate` 的**估算**：估算值与实际峰值，两个数不是一回事）。
这正是 MVP 那个 `--sparam` 前缀误伤 `--sparamImpedance` 的同型陷阱，测试里有回归。"""

_RE_EXPECTED_MEM = re.compile(
    r"\bexpected memory\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGT])?i?B\b", re.IGNORECASE
)
"""`--memEstimate=1` 的输出。出处：BRIEF §10「内存估算（P8a 的答案）」逐字引用
`expected memory: <估算值> GB`，`mvp/redzone/step2_memestimate.sh` 也照这个形状 grep。

它**不进** `peak_memory_mb` —— 估算不是实测。要它的人走
`parse_memory_estimate_mb()`（见那个函数的 docstring 说明为什么它没进冻结面）。"""

_RE_CPU = re.compile(
    r"\b(?:average|avg|mean)\s+cpu[^0-9%]{0,40}?([0-9]+(?:\.[0-9]+)?)\s*%", re.IGNORECASE
)
"""**未经真实日志验证。** BRIEF §10 D13 给了值（「平均 CPU 占用 <百分比>」）没给行。
百分数可以远大于 100（多核），所以不设上限。"""

_RE_VERSION_BANNER = re.compile(r"^ewave\s+v?([0-9][0-9A-Za-z._+-]*)\s*$", re.IGNORECASE)
"""**未经真实日志验证。** 只知道 `ewave --version` 这个 flag 存在
（`references/probes/ewave_probe_*.txt`），不知道日志抬头长什么样。
要求整行只有"ewave + 一个以数字开头的串"，故意保守 —— 宁可测不到，不要把
`[error] eWave exit failed!` 之类当成版本号。"""

_RE_VERSION_TAG = re.compile(r"\bversion\s*[:=]\s*v?([0-9][0-9A-Za-z._+-]*)", re.IGNORECASE)
"""**未经真实日志验证**（同上），另一种常见写法。"""

_RE_ERROR_TAG = re.compile(r"\[(?:error|fatal)\]", re.IGNORECASE)
"""**带方括号的**严重级标签。出处：BRIEF §10 逐字引用的
`[error] … eWave exit failed! Failed to execute emsolver, …`。

绝不做 `"error" in line` 这种子串判断：`mvp/redzone/step2_memestimate.sh`
的 grep 后面挂着 `grep -viE 'Invalid Via|0 error'` —— 那说明真实日志里存在
"0 error" 和 "Invalid Via" 这类**含 error 字样却无害**的行。把它们判成失败，
就会把成功的 run 报成崩溃。测试里有这条的回归。"""

_RE_WARN_TAG = re.compile(r"\[(?:warning|warn)\]", re.IGNORECASE)
"""**未经真实日志验证** —— `[error]` 的形状是实测的，`[warning]` 是照它推的。"""

_RE_WHAT = re.compile(r"^what\(\)\s*:", re.IGNORECASE)
"""C++ 异常的详情行。出处：BRIEF §10 崩溃现场第二行 `what():  input stream error`。
**只当错误消息收着，不单独判失败** —— 判失败的是它上面那行 `terminate called …`。"""

_RE_CONVERGED_NEG = re.compile(
    r"\b(?:not\s+converged|did\s+not\s+converge|fail(?:ed|s|ure)?\s+to\s+converge"
    r"|convergence\s+fail(?:ed|ure)?|no\s+convergence|diverged?)\b",
    re.IGNORECASE,
)
"""**未经真实日志验证。** BRIEF §10 只说了"iterative 35 步 82.8 s"、"33 次迭代"，
没给收敛行的措辞。**否定式先判** —— 反过来的话 `not converged` 里的 `converged`
会被判成收敛了，而那个方向的错是"把失败报成成功"。"""

_RE_CONVERGED_POS = re.compile(r"\bconverged\b", re.IGNORECASE)
"""**未经真实日志验证**（同上）。只认过去式 `converged`，不认 `converge` ——
后者在 `--relativeTolerance` 之类的参数回显里可能出现。"""

_CRASH_MARKS = (
    "boost::archive::archive_exception",
    "terminate called after throwing",
    "ewave exit failed",
    "failed to execute emsolver",
    "disk quota exceeded",
    "no space left on device",
)
"""崩溃 / 写失败指纹（在**压过空格的小写行**里做子串匹配）。出处全部是 BRIEF §10 step3：

* 前四条 = 那次 A/B 崩溃的原始输出三行（`terminate called … 'boost::archive::archive_exception'`
  / `what(): input stream error` / `[error] … eWave exit failed! Failed to execute emsolver …`）；
* 后两条 = 根因本身（`cp: failed to close …: Disk quota exceeded`）。
  配额爆了才是真凶，而它只在**别的**命令的输出里露过一次脸 ⇒ 只要日志里出现就报出来。

厂商名不进源码（`sched.fake` 也是这么处理的），所以 `ewave exit failed` 不带前缀。"""

_MEM_UNIT_MB = {
    "": 1.0 / (1024.0 * 1024.0),
    "K": 1.0 / 1024.0,
    "M": 1.0,
    "G": 1024.0,
    "T": 1024.0 * 1024.0,
}
"""内存单位 → MB 的倍率。**二进制**（1 GB = 1024 MB）—— 求解器报的是驻留内存，
按 GiB 理解比按 10^9 理解更接近实情。这个选择会影响 `peak_memory_mb` 的绝对值，
所以写在这里而不是散在正则里。"""


# --------------------------------------------------------------------------
# 内部：扫描
# --------------------------------------------------------------------------


class _Findings:
    """一份日志文本里找到的全部线索。**只装原始发现，不做判断** ——
    判断（"算不算成功"、"取哪一个值"）在 `_facts_from` 里，两件事分开才测得动。
    """

    __slots__ = (
        "version",
        "ports_size",
        "port_names",
        "calculated",
        "requested",
        "wall",
        "peak_mem_mb",
        "expected_mem_mb",
        "cpu",
        "converged",
        "phases_done",
        "errors",
        "warnings",
        "failed",
    )

    def __init__(self) -> None:
        self.version: str = ""
        self.ports_size: int | None = None
        self.port_names: tuple[str, ...] | None = None
        self.calculated: list[float] = []
        self.requested: list[float] = []
        self.wall: list[float] = []
        self.peak_mem_mb: list[float] = []
        self.expected_mem_mb: list[float] = []
        self.cpu: list[float] = []
        self.converged: bool | None = None
        self.phases_done: set[str] = set()
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.failed: bool = False


def _normalize(line: str) -> str:
    """匹配用的规范形式：小写 + 压空格 + 去首尾。

    BRIEF §10「又一个看起来绿了其实没测」那一节的结论：**每个文本判据都要先规范化**，
    否则一个多余的空格就让判据静默空过。原文（未压空格的那份）另存，进 `errors` 给人看。
    """
    return " ".join(line.lower().split())


def _to_mb(value: str, unit: str | None) -> float | None:
    """`("7.5", "G")` → 7680.0 MB。单位认不出返回 `None`（**不猜**）。"""
    factor = _MEM_UNIT_MB.get((unit or "").upper())
    if factor is None:
        return None
    try:
        return float(value) * factor
    except ValueError:  # pragma: no cover - 正则已经保证是数字
        return None


def _wall_seconds(match: re.Match[str]) -> float | None:
    """「数字 + 单位」→ 秒。单位缺省按秒（BRIEF 报的墙钟都是秒）。"""
    try:
        amount = float(match.group(1))
    except ValueError:  # pragma: no cover - 正则已经保证是数字
        return None
    unit = (match.group(2) or "s").lower()
    if unit.startswith("h"):
        return amount * 3600.0
    if unit.startswith("m"):
        return amount * 60.0
    return amount


def _append_capped(target: list[str], item: str) -> None:
    """往 `errors` / `warnings` 里追加，去重 + 封顶（见 `MAX_MESSAGES`）。**截断要留痕。**"""
    if item in target:
        return
    if len(target) < MAX_MESSAGES:
        target.append(item)
        return
    note = "(more follow; truncated at MAX_MESSAGES=%d)" % MAX_MESSAGES
    if note not in target:
        target.append(note)


def _scan(text: str) -> _Findings:
    """一份日志文本 → `_Findings`。先剥 ANSI，再逐行看。不碰文件系统。"""
    found = _Findings()
    for raw_line in strip_ansi(text).splitlines():
        raw = raw_line.strip()
        if not raw:
            continue
        low = _normalize(raw)

        # ---- 失败线索（最先看：它会盖掉后面所有 "done"）--------------------
        if _RE_ERROR_TAG.search(raw) or any(mark in low for mark in _CRASH_MARKS):
            found.failed = True
            _append_capped(found.errors, raw)
        elif _RE_WHAT.match(raw):
            # 异常详情行本身不判失败（判失败的是它上面那行），但要留给人看。
            _append_capped(found.errors, raw)
        elif _RE_WARN_TAG.search(raw):
            _append_capped(found.warnings, raw)

        # ---- 阶段完成标记 ---------------------------------------------------
        for phase in _RE_EXECUTE_DONE.findall(raw):
            found.phases_done.add(phase.lower())

        # ---- 端口 -----------------------------------------------------------
        ports_match = _RE_PORTS_SIZE.search(raw)
        if ports_match is not None:
            found.ports_size = int(ports_match.group(1))
        port_line = _RE_PORT_LINE.match(raw)
        if port_line is not None:
            found.port_names = tuple(port_line.group(1).split())

        # ---- 频点 -----------------------------------------------------------
        calculated = _RE_CALCULATED.search(raw)
        if calculated is not None:
            found.calculated.append(float(calculated.group(1)))
        requested = _RE_REQUESTED.search(raw)
        if requested is not None:
            found.requested.append(float(requested.group(1)))

        # ---- 墙钟（H:MM:SS 优先，见 _RE_WALL_UNIT 的说明）---------------------
        hms = _RE_WALL_HMS.search(raw)
        if hms is not None:
            found.wall.append(
                float(hms.group(1)) * 3600.0 + float(hms.group(2)) * 60.0 + float(hms.group(3))
            )
        else:
            unit_match = _RE_WALL_UNIT.search(raw)
            if unit_match is not None:
                seconds = _wall_seconds(unit_match)
                if seconds is not None:
                    found.wall.append(seconds)

        # ---- 内存（估算与峰值严格分开）-----------------------------------------
        peak = _RE_PEAK_MEM.search(raw)
        if peak is not None:
            megabytes = _to_mb(peak.group(1), peak.group(2))
            if megabytes is not None:
                found.peak_mem_mb.append(megabytes)
        expected = _RE_EXPECTED_MEM.search(raw)
        if expected is not None:
            megabytes = _to_mb(expected.group(1), expected.group(2))
            if megabytes is not None:
                found.expected_mem_mb.append(megabytes)

        # ---- CPU -------------------------------------------------------------
        cpu = _RE_CPU.search(raw)
        if cpu is not None:
            found.cpu.append(float(cpu.group(1)))

        # ---- 收敛（否定式先判）-------------------------------------------------
        if _RE_CONVERGED_NEG.search(raw):
            found.converged = False
        elif _RE_CONVERGED_POS.search(raw) and found.converged is not False:
            # 一旦见过"没收敛"就不再翻回 True：后面的 "converged" 可能是别的子问题。
            found.converged = True

        # ---- 版本 -------------------------------------------------------------
        if not found.version:
            banner = _RE_VERSION_BANNER.match(raw) or _RE_VERSION_TAG.search(raw)
            if banner is not None:
                found.version = banner.group(1)

    return found


def _pick_last(values: Sequence[float], label: str, warnings: list[str]) -> float | None:
    """同一份日志里同一个量出现多次时取**最后一个**（日志是流水账，最后一条是结论）。

    值不一致时留一条 warning —— **不静默**。跨文件的取舍是另一回事，那归
    `merge_log_facts` 的"先到先得"管。
    """
    if not values:
        return None
    distinct = sorted(set(values))
    if len(distinct) > 1:
        _append_capped(
            warnings,
            f"{label} has {len(distinct)} different values {distinct} in one log; taking the last one",
        )
    return values[-1]


def _facts_from(found: _Findings, *, may_claim_ok: bool) -> LogFacts:
    """`_Findings` → `LogFacts`。**全部判断集中在这里。**

    `may_claim_ok` = 这份日志有没有资格说"整个 run 成了"。只有 eWave 主日志有
    （`emsolver.log` 跑完只代表求解器那一段结束了，见 `parse_emsolver_log`）。
    """
    warnings = list(found.warnings)

    port_count = found.ports_size
    if found.port_names is not None:
        listed = len(found.port_names)
        if port_count is None:
            port_count = listed
        elif listed != port_count:
            # 端口数错位是**静默**的（BRIEF §5「--all 的代价」），所以两处对不上必须喊。
            _append_capped(
                warnings,
                f"the log contradicts itself: `All Ports size is {port_count}`, "
                f"but the `Port:` line lists {listed}",
            )

    wall = _pick_last(found.wall, "wall clock", warnings)
    peak = _pick_last(found.peak_mem_mb, "peak memory", warnings)
    cpu = _pick_last(found.cpu, "cpu usage", warnings)
    calculated = _pick_last(found.calculated, "frequency points actually solved", warnings)
    requested = _pick_last(found.requested, "frequency points requested", warnings)

    if found.failed:
        ok: bool | None = False
    elif may_claim_ok and set(EWAVE_PHASES) <= found.phases_done:
        ok = True
    else:
        ok = None

    return LogFacts(
        ok=ok,
        converged=found.converged,
        wall_seconds=wall,
        peak_memory_mb=peak,
        port_count=port_count,
        freq_points_calculated=None if calculated is None else int(calculated),
        freq_points_requested=None if requested is None else int(requested),
        cpu_percent_avg=cpu,
        ewave_version=found.version,
        errors=tuple(found.errors),
        warnings=tuple(warnings),
    )


# --------------------------------------------------------------------------
# 公开：纯字符串函数
# --------------------------------------------------------------------------


def parse_ewave_log(text: str) -> LogFacts:
    """`ewave.log` → `LogFacts`。纯字符串函数（先 `strip_ansi`），不碰文件系统。

    解析不到就留 `None`，**别用 0 冒充**。`ok` 尤其要小心：日志说 "done" 不代表真成了
    （实测崩了也 exit=0 还留 0 字节文件），所以这里的 `ok` 只是日志的说法，
    最终判据是 `layout.verify_run_outputs`。

    具体到本函数，`ok` 的三态是：

    * `False` —— 有崩溃指纹或 `[error]` / `[fatal]` 标签。**这一条压倒一切**：
      BRIEF §10 那次事故的日志里 `Execute eresist done.` 与崩溃行同时存在，
      要是让 done 赢，本模块就等于没写。
    * `True` —— `EWAVE_PHASES` 三个阶段都打了 done，且一条失败线索都没有。
      仅仅意味着"日志没自曝失败"。
    * `None` —— 日志没说（跑了一半就被杀、`--memEstimate` 这种半程 run、日志被截断…）。

    `source_files` 留空 —— 这里只有文本，没有文件名。`parse_run_logs` 会补上。
    """
    return _facts_from(_scan(text), may_claim_ok=True)


def parse_emsolver_log(text: str) -> LogFacts:
    """`emsolver.log` → `LogFacts`（收敛、真算过的频点数、峰值内存、CPU 占用多在这份里）。

    与 `parse_ewave_log` 认的是同一套行格式（同一个扫描器），**只差一条**：
    这份日志**永远不会把 `ok` 报成 `True`**。理由是语义而不是实现 ——
    emsolver 是三个阶段里的一个，它自己跑完了不等于这次 run 成了
    （BRIEF §10：`eresist` 打了 done，写出来的却是 0 字节）。
    失败方向照常：这份日志里出现崩溃指纹时 `ok=False`，因为**那次事故的现场就在这份日志里**。

    `mesh.log` / `emesh_mrg.log` 这类"部件日志"也走本函数，理由同上。
    """
    return _facts_from(_scan(text), may_claim_ok=False)


def parse_memory_estimate_mb(text: str) -> float | None:
    """`--memEstimate=1` 那次跑出来的 **估算** 内存（MB）；没有返回 `None`。

    出处：BRIEF §10「内存估算（P8a 的答案）」—— `expected memory: <估算值> GB`，
    实际峰值很接近，估得很准 ⇒ 拿它定 `-R mem=` 比人肉猜靠谱。

    **它不在冻结面里**，因为 `LogFacts` 没有"估算内存"这个字段，而把估算值塞进
    `peak_memory_mb` 是错的（那是两个不同的量）。要不要给 `LogFacts` 加一个
    `estimated_memory_mb`，走 `interface_change_requests` 由编排者决定；
    在那之前，需要这个数的调用方直接调本函数。
    """
    return _pick_last(_scan(text).expected_mem_mb, "estimated memory", [])


def merge_log_facts(*facts: LogFacts) -> LogFacts:
    """合并多份 `LogFacts`：**先到先得**（前面的非 None 值不被后面覆盖），
    `errors`/`warnings`/`source_files` 按顺序拼接去重。

    「先到先得」对 `ok` 有一个陷阱：`ok=True` 的日志排在 `ok=False` 的前面时，
    合并结果会是 `True`。本函数**照契约办事、不特事特办**；补救在
    `parse_run_logs` 里（它知道全部来源，会让失败一票否决）。
    直接调本函数的人请自己注意这一点。
    """
    merged = LogFacts()
    scalar_fields = (
        "ok",
        "converged",
        "wall_seconds",
        "peak_memory_mb",
        "port_count",
        "freq_points_calculated",
        "freq_points_requested",
        "cpu_percent_avg",
    )
    errors: list[str] = []
    warnings: list[str] = []
    sources: list[str] = []
    for item in facts:
        for name in scalar_fields:
            if getattr(merged, name) is None:
                value = getattr(item, name)
                if value is not None:
                    setattr(merged, name, value)
        if not merged.ewave_version and item.ewave_version:
            merged.ewave_version = item.ewave_version
        for message in item.errors:
            _append_capped(errors, message)
        for message in item.warnings:
            _append_capped(warnings, message)
        for source in item.source_files:
            if source not in sources:
                sources.append(source)
    merged.errors = tuple(errors)
    merged.warnings = tuple(warnings)
    merged.source_files = tuple(sources)
    return merged


# --------------------------------------------------------------------------
# 公开：读文件
# --------------------------------------------------------------------------


def _posix(path: str) -> str:
    """路径归一成正斜杠。红区是 Linux，报告里出现反斜杠只会让人对不上号。"""
    return str(path).replace("\\", "/")


def _read_log(path: str) -> tuple[str, str]:
    """读一份日志 → `(文本, 附注)`。读不动不抛异常，返回 `("", 原因)`。

    * 编码用 UTF-8 + `errors="replace"`：日志里混进别的编码是常事，
      为一个坏字节丢掉整份日志不值得。
    * 超过 `MAX_LOG_BYTES` 只读**末尾**那么多 —— 结论都在末尾。截断留附注。
    """
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return "", f"cannot read {_posix(path)}: {exc}"
    note = ""
    try:
        with open(path, "rb") as handle:
            if size > MAX_LOG_BYTES:
                handle.seek(size - MAX_LOG_BYTES)
                note = (
                    f"{_posix(path)} is {size} bytes; only the last {MAX_LOG_BYTES} bytes were read "
                    "(things near the top, such as the version banner, may therefore go undetected)"
                )
            data = handle.read()
    except OSError as exc:
        return "", f"cannot read {_posix(path)}: {exc}"
    return data.decode("utf-8", errors="replace"), note


def _is_main_log(name: str) -> bool:
    """这份日志有没有资格说"整个 run 成了"。

    `ewave.log` 是 eWave 主日志；`run*.log` 是我们自己捕获的那份 stdout
    （`model.RUN_LOG_TEMPLATE`），装的就是 eWave 的整段输出 ——
    崩溃时往往只剩它（MVP 的诊断脚本正是 grep 这一份）。
    """
    lowered = name.lower()
    if lowered == EWAVE_LOG_NAME:
        return True
    return lowered.startswith(RUN_LOG_PREFIX) and lowered.endswith(".log")


def _log_priority(name: str) -> int | None:
    """日志名 → 合并优先级（小的先，`merge_log_facts` 先到先得）。不认识返回 `None`。

    只认**名单里的**日志，不是"目录里所有 .log"：`gds_out.log`（阶段 1 的）、
    `ewaveOnVir.log`（官方 GUI 集成层的）都不该被算进某个 run 的事实。
    """
    lowered = name.lower()
    if lowered in LOG_FILE_NAMES:
        return LOG_FILE_NAMES.index(lowered)
    if lowered.startswith(RUN_LOG_PREFIX) and lowered.endswith(".log"):
        # 排在具名日志之后：ewave.log / emsolver.log 是 eWave 自己写的，更权威。
        return len(LOG_FILE_NAMES)
    return None


def _collect_log_files(root: str) -> list[str]:
    """在 `root` 和它的**直接子目录**里找日志，按 (优先级, 路径) 排序返回。

    为什么要看子目录一层：日志实际落在 `<workDir>/<corner>_<temp>/` 里（eWave 自建的那层，
    BRIEF §7 P4b 实测），而调用方手上常常只有 run 目录。两层都扫，调用方传哪个都对。
    **只下一层** —— 再往下是 mesh 中间件那些大目录，没有日志，白扫。
    """
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    entries: list[tuple[int, str]] = []
    directories: list[str] = []
    for name in names:
        full = os.path.join(root, name)
        if os.path.isdir(full):
            directories.append(full)
            continue
        priority = _log_priority(name)
        if priority is not None:
            entries.append((priority, _posix(full)))
    for directory in directories:
        try:
            inner = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in inner:
            full = os.path.join(directory, name)
            if not os.path.isfile(full):
                continue
            priority = _log_priority(name)
            if priority is not None:
                entries.append((priority, _posix(full)))
    entries.sort(key=lambda item: (item[0], item[1]))
    return [path for _, path in entries]


def parse_run_logs(run_dir: str) -> LogFacts:
    """读一个 run 目录里的全部日志（`ewave.log` / `emsolver.log` / `mesh.log` …）→ 合并后的事实。

    日志缺失不抛异常（失败的 run 常常什么都没留），返回全 None 的 `LogFacts` 并在
    `warnings` 里说明。只读。

    本函数自己只管一条规矩：**看两层** —— `run_dir` 本身 + 它的直接子目录
    （日志实际落在 `<corner>_<temp>/` 里，调用方手上常常只有 run 目录）。
    解析与合并（含「失败一票否决」）在 `parse_log_files`。

    ⚠️ 调用方如果手上有**具体某一个 run** 的坐标，该走 `run_log_files` +
    `parse_log_files` 而不是这里：run 目录是 corner/temp 之间共享的，
    对着它扫会把邻居的日志一起合并进来。
    """
    root = _posix(run_dir)
    if not root or not os.path.isdir(run_dir):
        return LogFacts(warnings=(f"log directory does not exist: {root}",))

    paths = _collect_log_files(run_dir)
    if not paths:
        expected = " / ".join(LOG_FILE_NAMES)
        return LogFacts(
            warnings=(
                f"no eWave log at all in {root} or its immediate subdirs "
                f"(looked for {expected} and {RUN_LOG_PREFIX}*.log) - this run may never have started",
            )
        )

    return parse_log_files(paths)


def parse_log_files(paths: Sequence[str]) -> LogFacts:
    """一组**指名道姓的**日志文件 → 合并后的事实。只读，缺文件不抛。

    从 `parse_run_logs` 里拆出来的，因为调用方分成了两类：

    * 「给我一个目录，你自己去找」—— `parse_run_logs`，CLI 的 `status` 走这条；
    * 「文件我已经挑好了」—— `sched.driver`，它必须**精确到这一个 run**
      （run 目录是 corner/temp 之间共享的，扫目录会把邻居的日志合并进来），
      挑法见 `run_log_files`。

    「失败一票否决」这条规矩住在这里：任何一份日志说 `ok is False`，合并结果就是
    `False`，哪怕另一份写满了 `Execute ... done.`。`merge_log_facts` 的先到先得
    管不了这件事（它不知道还有别的来源）。
    ⇒ BRIEF §10 的现场：`ewave.log` 说 done，`emsolver.log` 里躺着 boost 异常。
    """
    parsed: list[LogFacts] = []
    for path in paths:
        text, note = _read_log(path)
        name = os.path.basename(path)
        facts = parse_ewave_log(text) if _is_main_log(name) else parse_emsolver_log(text)
        facts.source_files = (_posix(path),)
        if note:
            facts.warnings = facts.warnings + (note,)
        parsed.append(facts)
    if not parsed:
        return LogFacts()
    merged = merge_log_facts(*parsed)
    if any(item.ok is False for item in parsed):
        merged.ok = False
    return merged


def collect_log_files(root: str) -> tuple[str, ...]:
    """`root` 和它**直接子目录**里的日志文件，按 (优先级, 路径) 排序。

    `_collect_log_files` 的公开面 —— 界面要的是"有哪些日志可以看"这份**清单**
    （拿去做下拉框），不是解析结果。
    """
    return tuple(_collect_log_files(root))


def _order_key(path: str) -> tuple[int, str]:
    """`run_log_files` 的排序键。认不出的日志排最后，不丢掉。"""
    priority = _log_priority(os.path.basename(path))
    return (len(LOG_FILE_NAMES) + 1 if priority is None else priority, _posix(path))


def run_log_files(
    *, ewave_dir: str = "", run_log: str = "", run_dir: str = ""
) -> tuple[str, ...]:
    """**一个 run 自己的**日志清单（去重 + 按优先级排）。三个来源，缺一不可。

    | 来源 | 是什么 | 为什么单列 |
    |---|---|---|
    | `ewave_dir` | `<corner>_<temp>/`，eWave 自建的那层 | `ewave.log` / `emsolver.log` 在这里 |
    | `run_log` | 我们自己捕获的那份 stdout（dsub `-o`） | 它在 **run_dir 里**，而且**崩得早时只有它**——eWave 还没来得及建目录 |
    | `run_dir` | 退路 | 只在 `ewave_dir` 预测不出名字（空串）时才扫 |

    🚨 **不许拿 `run_dir` 一把梭。** 同一个 run_dir 底下住着 N 个 corner/temp 组合
    （`<axes-slug>` 按定义不含它们，见 `layout.compute_run_paths`）—— 扫它就是把邻居的
    日志一起合并进来，然后报出一份张冠李戴的收敛结论。这条与 `cli._log_facts_for`
    同源，两处必须一起改。

    `run_log` 的优先级低于两份具名日志（`_log_priority` 已经这么排）：eWave 自己写的
    那两份更权威，stdout 那份是它们不在时的救命稻草。
    """
    found: list[str] = []
    if ewave_dir and os.path.isdir(ewave_dir):
        found.extend(_collect_log_files(ewave_dir))
    elif run_dir and os.path.isdir(run_dir):
        found.extend(_collect_log_files(run_dir))
    if run_log and os.path.isfile(run_log):
        found.append(_posix(run_log))
    return tuple(sorted(dict.fromkeys(found), key=_order_key))


def read_log_tail(path: str, *, limit_bytes: int = TAIL_BYTES) -> str:
    """一份日志的**末尾** `limit_bytes` 字节，给"实时看"用。读不动**不抛**，返回一句人话。

    四条口径，每条都对应一个"看起来能用其实不能"的做法：

    1. **只读末尾**：日志是追加写的，人要看的永远是最后那几十行；整份读进来只会
       让界面每一拍搬几 MB。
    2. **从中间切进来的第一行丢掉**：`seek` 落点几乎必然在某一行中间，留着它就是
       让人读半句话（而且那半句话看起来像一条完整的、内容很怪的日志）。
    3. **剥 ANSI**：eWave 即使 `--nogui` 也打色码（`strip_ansi` 的出处那条实测），
       tkinter 的 Text 不认 escape，不剥就是满屏 `[0m`。
    4. **不抛异常**：这个函数被界面按轮询间隔反复调用，而"文件还没生成"
       （作业还在排队）是**正常状态**，不是错误。读不动就把原因当正文显示出来。
    """
    target = _posix(path)
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            if size > limit_bytes:
                handle.seek(size - limit_bytes)
            data = handle.read()
    except OSError as exc:
        return f"<cannot read {target}: {exc}>"
    text = strip_ansi(data.decode("utf-8", errors="replace"))
    if size > limit_bytes:
        cut = text.find("\n")
        text = text[cut + 1 :] if cut >= 0 else text
        text = (
            f"<... showing the last {len(data)} of {size} bytes of {target} ...>\n" + text
        )
    return text


# --------------------------------------------------------------------------
# 公开：.sNp 的端口顺序
# --------------------------------------------------------------------------

_RE_SNP_PORT = re.compile(r"^!\s*Port\s*\[\s*(\d+)\s*\]\s*=\s*(.+?)\s*$", re.IGNORECASE)
"""`.sNp` 注释头里的端口行。出处：`references/probes/mvp_step4_verify_*.txt` 的实测原文，
格式 `! Port[N] = <pin名> | ref`（BRIEF §10 step4「判据②」把 P8③ 一并答了：
写的是 **pin 名**不是端口 ID）。`|` 后面是参考端，本函数只取名字。"""


# --------------------------------------------------------------------------
# 层清单 —— 「这个 design 到底有哪几层」由 eWave 自己回答
# --------------------------------------------------------------------------

LAYER_BASIS_HEADER = "basis function are sorted from largest to smallest as follows"
"""eWave 报「每层占了多少网格元素」那一段的抬头。出处：`references/probes/speed3d_run_20260828.txt`。

底下每行形如 `  <层> (20%)`，按占比从大到小。**这就是"该降哪几层"的决策依据** ——
占 20% 的那层降下去才省时间，占 <5% 的降了纯属改变了没打算改的东西。
"""

LAYER_VIA_MARKER = "the experssion belongs to via"
"""紧跟在某条 `begin to eval experssion:` 后面的话，意思是"上面那层是 via"。

**这是唯一权威的金属/via 判据。** 靠名字猜（`V*` / `RV` / `Vz`…）就是在赌命名习惯，
而 `--3d` 说的是 metal layer —— 把 via 塞进去是在拿没验证过的行为换一点方便。
"""

_LAYER_STAMP = re.compile(r"^(?:\[[^\]]*\]\s*)+")
"""行首那一串 `[2026-08-28 09:48:14][info] `。有的行只有时间戳没有级别，所以是 `+`。"""

_LAYER_EVAL = re.compile(r"begin to eval experssion:\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
"""`begin to eval experssion: <层>=<GDS 层号表达式>` → 层名。

这一段列的是**整个 PDK 的层**（含 via、含非金属导体），顺序是自下而上 ——
所以它同时给了"有哪些层"和"层的先后"。
"""

_LAYER_BASIS_ROW = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*(<?\s*\d+\s*%)\s*\)$")
"""`TOP1 (20%)` / `LOW1 (<5%)` → (层名, 占比)。"""


def _layer_strip(line: str) -> str:
    """去掉行首的时间戳/级别，再去两头空白。"""
    return _LAYER_STAMP.sub("", line).strip()


def parse_layer_stack(text: str) -> LayerStack:
    """一份 eWave 日志 → 这个 design 的导体层清单（`LayerStack`）。

    **为什么要它**：`--3d` 是保持 3D 的白名单，工具要替用户算补集就得知道有哪些层；
    而层名是 PDK 叠层坐标（CLAUDE.md 硬约束 1b）—— 源码里不许有，也**不该让用户手打**。
    官方 run 目录里本来就躺着一份 eWave 日志，两段内容合起来就是权威答案
    （硬约束 1b 那条「运行时发现优于配置项」的又一个落点）。

    两段各管一件事：

    | 段 | 给出什么 |
    |---|---|
    | `begin to eval experssion: <层>=…` | 整个 PDK 的层 + **顺序（自下而上）**；紧跟 `LAYER_VIA_MARKER` 的那些是 via |
    | `LAYER_BASIS_HEADER` 底下那几行 | 这个 design **真正meshes到**的层 + 各自占了多少元素 |

    返回的 `conductors` = 两段的交集减去 via，按第一段的顺序。
    取交集而不是直接用第一段：第一段列的是整个 PDK（几十层，多数这个 design 根本没有），
    全塞进 `--3d` 只是让命令行难读。取第二段而不减 via：`--3d` 说的是 metal layer。

    🚨 **两段缺一就返回空**（`note` 说明缺了哪一段），**不猜**：
    只有第二段就分不出 via；只有第一段就不知道这个 design 用了哪些。
    猜错的后果是 `--3d` 少一层 → 那层静默退 2D，正是这整个功能要消灭的东西。
    """
    lines = [_layer_strip(raw) for raw in strip_ansi(text).splitlines()]
    dense = [line for line in lines if line]

    order: list[str] = []
    vias: set[str] = set()
    for index, line in enumerate(dense):
        found = _LAYER_EVAL.search(line)
        if found is None:
            continue
        name = found.group(1)
        if name not in order:
            order.append(name)
        nxt = dense[index + 1] if index + 1 < len(dense) else ""
        if LAYER_VIA_MARKER in nxt.lower():
            vias.add(name)

    shares: dict[str, str] = {}
    in_basis = False
    for line in dense:
        if LAYER_BASIS_HEADER in line.lower():
            in_basis = True
            continue
        if not in_basis:
            continue
        row = _LAYER_BASIS_ROW.match(line)
        if row is None:
            # 这一段以 `total: 880652` 收尾，也可能被下一条 [info] 打断。
            break
        shares[row.group(1)] = row.group(2).replace(" ", "")

    if not shares or not order:
        missing = []
        if not shares:
            missing.append("the per-layer element report (%r)" % LAYER_BASIS_HEADER)
        if not order:
            missing.append("the layer expressions (%r)" % "begin to eval experssion:")
        return LayerStack(
            note=(
                "this log has no layer list: missing " + " and ".join(missing) + ". "
                "Both are needed - one says which layers this design actually meshes, "
                "the other says which of them are vias."
            )
        )

    used = [name for name in order if name in shares]
    return LayerStack(
        conductors=tuple(name for name in used if name not in vias),
        vias=tuple(name for name in used if name in vias),
        shares={name: shares[name] for name in used},
    )


def parse_port_order(snp_path: str) -> tuple[str, ...]:
    """从 `.sNp` 的注释头里读端口顺序（`--includePortOrder=1` 写进去的那份，D1d）。

    读不到返回空元组 —— 归档会把 `.sNp` 搬离原始命令行，而**端口映射只存在于命令行**，
    所以这份自描述是 v2 批量对比唯一能信的东西。只读。

    **宁可什么都不给，也不给一半。** 下面四种情况一律返回 `()`：

    * 一条 `! Port[...]` 都没有（没开 `--includePortOrder=1`）；
    * 序号不是 `1..N` 连续（缺号），或同一个序号出现两次；
    * 条目数与文件后缀 `.s{N}p` 数出来的端口数对不上；
    * 文件读不动。

    理由是这份数据的用途：端口顺序错一位，整条 `.sNp` 的对应关系就全错，**而且静默**
    （BRIEF §5「`--all` 的代价」）。半份端口表比没有端口表更危险 ——
    没有会让调用方去别处找，半份会让它照着错的往下算。
    """
    entries: dict[int, str] = {}
    duplicated = False
    try:
        with open(snp_path, "rb") as handle:
            for index, raw in enumerate(handle):
                if index >= MAX_SNP_SCAN_LINES:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if line.startswith("!"):
                    match = _RE_SNP_PORT.match(strip_ansi(line))
                    if match is not None:
                        number = int(match.group(1))
                        if number in entries:
                            duplicated = True
                        entries[number] = match.group(2).partition("|")[0].strip()
                    continue
                if line.startswith("#") and entries:
                    # option line（`# HZ S RI R 50`）之后是数值块。端口注释已经拿到了就停 ——
                    # 几百点 x 十几端口那种文件没必要整份读完。还没拿到就继续找。
                    break
    except OSError:
        return ()

    if not entries or duplicated:
        return ()
    if sorted(entries) != list(range(1, len(entries) + 1)):
        return ()
    expected = port_count_from_suffix(snp_path)
    if expected is not None and expected != len(entries):
        return ()
    return tuple(entries[number] for number in range(1, len(entries) + 1))
