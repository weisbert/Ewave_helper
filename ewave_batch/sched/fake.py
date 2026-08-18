"""`ewave_batch.sched.fake` —— 假 runner / 假调度器。**P3 的地基，也是本项目唯一的可测性来源。**

本机没有 `ewave` / `dsub` / `strmout`，**永远不会有**（CLAUDE.md 硬约束 3）。
于是 D8「全程 dry-run + 可注入 runner」的另一半必须由本模块顶起来：
`RunnerProtocol` / `SchedulerProtocol` 的真身在红区，替身在这里。

## 这个模块存在的**唯一**理由：模拟实测过的坑

MVP 在红区踩到三条「失败信号不可靠」（BRIEF §10「三条失败信号合起来就是调度器的验收契约」）：

| 现象 | 实测 | 对工具的要求 |
|---|---|---|
| 崩溃时退出码 | `ewave exit=0` | **不能用 exit code 判成败** |
| 写失败 | `eresist` 打印 "done"，留 0 字节文件 | **每个产物验非空，不只验存在** |
| 错误信息 | 配额爆了，但没有任何一行报错 | **"没报错" ≠ 成功** |

⇒ `done` 的判据是 `core.layout.verify_run_outputs`（**存在 + 非空 + 端口数对**），
不是退出码、不是文件存在、不是日志措辞。本模块的产物**真的写到磁盘上**，
好让那个验收器走的是它自己的真实代码路径，而不是被 mock 掉 ——
**验收逻辑必须是被真实文件验的**，否则整套测试就是自证。

## 两条硬性设计约束

1. **确定性。** 不许用真随机。要抖动就用可注入的种子（`seed`）+ `zlib.crc32`，
   或一张显式的时间线表（`pending_polls` / `running_polls`）。
   无人值守时不可复现 = 查不了。
2. **不许 sleep。** poll 的推进靠"第几次 poll"驱动，不靠墙钟；
   时间戳来自一个假时钟（`epoch` + tick × `seconds_per_poll`）。
   12-run 假批次要在毫秒级跑完，否则没人会在开发循环里跑它。

## 日志是假的，别拿它当证据

`FakeRunner` 写出来的 `ewave.log` 带一行显式的 FAKE 抬头。
`core.logparse`（P4）的期望值**必须**来自 `references/probes/` 里的真实日志 ——
拿本模块的输出当 fixture 就是"实现方自己决定期望值"，正是防自证四配方第 2 条禁止的事。
唯一从真实证据抄来的是几条**崩溃指纹**（下面每条都注了出处），
因为那几行正是 `logparse` 将来要认的东西。

🚨 本文件零站点标识符：cell 名 / 路径 / 端口名全部**从入参或 argv 来**，
默认端口数是个明显合成的小数字，不是任何真实 design 的端口数。
"""

from __future__ import annotations

import enum
import os
import posixpath
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from ..core.matrix import ewave_dir_name
from ..model import (
    TIMESTAMP_FORMAT,
    CommandPlan,
    Job,
    JobState,
    RunResult,
    SchedulerError,
    ToolMissingError,
)

# --------------------------------------------------------------------------
# 失败模式
# --------------------------------------------------------------------------


class FakeFailureMode(enum.Enum):
    """一次假执行会发生什么。**每一条都对应一件红区实测过的事**，不是假想出来的。

    出处一律是 `PROJECT_BRIEF.md` §10（MVP step3 的 A/B 崩溃 + 根因追查）。
    """

    SUCCESS = "success"
    """正常成功：产物齐、非空、端口数对，退 0。

    ⚠️ 这条是**反向验证**用的对照组，不是凑数的：少了它，
    "一律判 failed" 的验收器也能让其余五条全绿 —— 那种验收器等于没有。
    """

    EXIT_ZERO_BUT_CRASHED = "exit_zero_but_crashed"
    """**退出码 0，但根本没产物。**

    实测（§10 step3）：emsolver 反序列化空文件 → boost 抛异常 → 进程 abort，
    而 payload 打出来的仍然是 `ewave exit=0`，日志里写着 `eWave exit failed!`。
    ⇒ 退出码不可信。这条要是被判成 `done`，本工具就没有存在价值。
    """

    ZERO_BYTE_OUTPUT = "zero_byte_output"
    """**产物文件建出来了，但 0 字节**，日志措辞与成功那次**逐字相同**。

    实测（§10）：`eresist` 照常打印 "Execute eresist done."，写出的 `resist.rst` 是 0 字节。
    ⇒ "文件存在" 和 "日志说 done" 都不可信，必须验非空。
    """

    SWALLOWED_WRITE_FAILURE = "swallowed_write_failure"
    """**写失败被彻底吞掉：零错误输出、零错误日志，产物却没写出来。**

    实测（§10）：`$HOME` 配额爆了，`cp` 拿到的是 0 字节文件、md5 是空文件的 md5，
    而整条链路上没有一行错误 —— 直到下游 boost 抛出一个完全指不向根因的异常。
    与 `EXIT_ZERO_BUT_CRASHED` 的区别就是**有没有错误文本**：
    真实事故里这两条是同一条因果链的上下游，这里拆开是为了分别验证
    "退出码不可信" 和 "没报错 ≠ 成功" 两件事。
    """

    WRONG_PORT_COUNT = "wrong_port_count"
    """**产物齐、非空，但端口数不对**（该 17 端口的出了 `.s16p`）。

    对应 D1b 的真实风险（BRIEF §5「`--all` 的代价」）：设计师加/删/改名一个 pin ⇒
    所有端口编号平移 ⇒ 之前建的 nport 和归档的 `.sNp` 全部错位，**而且静默**。
    端口数是我们唯一能自动抓到这件事的把手，所以它是验收契约的第三条。
    """

    NONZERO_EXIT = "nonzero_exit"
    """**真的报错，退非 0。** 对照组：这一次错误信号是可靠的。

    存在的意义是划清边界 —— 验收器不能只会看退出码，但也不能对退出码视而不见。
    """


@dataclass(frozen=True)
class _Outcome:
    """一个失败模式在磁盘上和 stdout 上分别留下什么。

    做成一张显式的表（`_OUTCOMES`）而不是散在 if/else 里，是为了让
    "每个模式都定义过" 这件事**可以被计数断言**（见 tests 里的 meta 测试）。
    """

    products: str
    """`"full"` 写非空产物 / `"empty"` 写 0 字节产物 / `"none"` 根本不写。"""
    resist: str
    """同上，针对 `resist.rst` —— §10 点名的那个"确定指纹"。"""
    mesh: bool
    """写不写 mesh 中间件（pmrg/pmsh 那几个）。归档（D5）要删的就是它们。"""
    log: str
    """`"done"` 成功措辞 / `"crash"` 崩溃指纹 / `"error"` 普通报错。"""
    returncode: int
    port_delta: int
    """产物端口数相对 `FakeRunner.port_count` 的偏移。`-1` = 少一个 pin ⇒ 全体编号平移。"""


_OUTCOMES: dict[FakeFailureMode, _Outcome] = {
    FakeFailureMode.SUCCESS: _Outcome(
        products="full", resist="full", mesh=True, log="done", returncode=0, port_delta=0
    ),
    FakeFailureMode.EXIT_ZERO_BUT_CRASHED: _Outcome(
        # 产物零、resist 0 字节、日志里有崩溃文本 —— 而退出码**仍然是 0**。
        products="none", resist="empty", mesh=True, log="crash", returncode=0, port_delta=0
    ),
    FakeFailureMode.ZERO_BYTE_OUTPUT: _Outcome(
        # 日志用的是 "done"，与成功那次逐字相同 ⇒ 只有"非空"这条判据能抓住它。
        products="empty", resist="empty", mesh=True, log="done", returncode=0, port_delta=0
    ),
    FakeFailureMode.SWALLOWED_WRITE_FAILURE: _Outcome(
        products="none", resist="empty", mesh=True, log="done", returncode=0, port_delta=0
    ),
    FakeFailureMode.WRONG_PORT_COUNT: _Outcome(
        products="full", resist="full", mesh=True, log="done", returncode=0, port_delta=-1
    ),
    FakeFailureMode.NONZERO_EXIT: _Outcome(
        products="none", resist="none", mesh=False, log="error", returncode=3, port_delta=0
    ),
}
"""模式 → 后果。**键必须覆盖 `FakeFailureMode` 全体** —— 有 meta 测试盯着。"""


# --------------------------------------------------------------------------
# 日志文本
# --------------------------------------------------------------------------

FAKE_LOG_HEADER = (
    "# FAKE LOG - written by ewave_batch.sched.fake, NOT a real eWave log.\n"
    "# 别拿它当 core.logparse 的 fixture：那边的期望值只能来自 references/probes/ 的真日志。\n"
)
"""每份假日志的第一行。存在的理由见模块 docstring 「日志是假的」那一节。"""

_LOG_DONE = (
    "Execute emesh done.",
    "Execute eresist done.",
    "Execute emsolver done.",
)
"""成功措辞。`Execute eresist done.` 是**真实的一行**（BRIEF §10 根因链：
配额爆了写出 0 字节 `resist.rst`，它照样打印这一行）—— 也就是说这几行
在成功和失败时**一模一样**，日志措辞不可信的证据就是它。"""

_LOG_CRASH = (
    "terminate called after throwing an instance of 'boost::archive::archive_exception'",
    "  what():  input stream error",
    "[error] eWave exit failed! Failed to execute emsolver, please contact the manufacturer.",
)
"""崩溃指纹。抄自 BRIEF §10 step3 的实测输出（去掉了厂商名）。
配着 `returncode=0` 一起出现 —— 这就是"退出码不可信"的原始现场。"""

_LOG_ERROR = ("[error] fake runner: forced non-zero exit (FakeFailureMode.NONZERO_EXIT)",)
"""普通报错。这条是**我们自己编的**，不是实测文本 —— 它代表"错误信号可靠"的对照组。"""


# --------------------------------------------------------------------------
# argv 解析（runner 只拿得到 argv，拿不到 CommandPlan —— 这是 RunnerProtocol 的约定）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Parsed:
    """从 argv 里认出来的东西。字段全部来自命令行本身，没有一个是猜的。"""

    kind: str
    """`"ewave"` / `"strmout"` / `"unknown"`。"""
    program: str
    work_dir: str
    corner: str
    temperature: str
    sparam: str
    template_file: str

    @property
    def out_dir(self) -> str:
        """eWave 会把产物写进哪儿：`<workDir>/<corner>_<temp>`。

        ⚠️ corner/temp 是从 **argv** 读的，不是从 `Run` 读的 —— 这正是真实情形：
        它们可能来自默认表而不是轴，于是 `RunPaths.ewave_dir` 预测不出来、
        但目录照样会被建出来。`verify_run_outputs` 的"现场发现"分支专为这种情况存在。
        两个都认不出来时退回 `work_dir` 本身（= 我们连那层子目录都造不出来）。
        """
        stem = ewave_dir_name(self.corner, self.temperature)
        if not stem:
            return self.work_dir
        return posixpath.join(self.work_dir, stem) if self.work_dir else stem


def _flag_value(argv: Sequence[str], name: str) -> str:
    """取 `--name=value`（`render_flags` 的长 flag 形式）或 `--name value` / `-name value`。

    两种形式都认：长 flag 由 `core.cmd.render_flags` 渲染成一项（`--corner=typical`），
    短 flag 渲染成两项（`-e 0.4`、`-templateFile <file>`）。
    找不到返回空串 —— **不猜默认值**。
    """
    prefix = name + "="
    items = [str(item) for item in argv]
    for index, item in enumerate(items):
        if item.startswith(prefix):
            return item[len(prefix) :]
        if item == name and index + 1 < len(items):
            return items[index + 1]
    return ""


def _parse_argv(argv: Sequence[str]) -> _Parsed:
    """argv → `_Parsed`。**只看 argv**，不碰磁盘。

    分辨阶段靠 flag 而不是程序名：程序名是站点坐标（绝对路径），
    源码里不许写死，也不该拿它做判断（硬约束 1b）。
    `-templateFile` 只有 `tools.strmout` 会给，`--workDir` 只有阶段 2 会给。
    """
    items = [str(item) for item in argv]
    program = items[0] if items else ""
    template_file = _flag_value(items, "-templateFile")
    work_dir = _flag_value(items, "--workDir")
    if template_file:
        kind = "strmout"
    elif work_dir:
        kind = "ewave"
    else:
        kind = "unknown"
    return _Parsed(
        kind=kind,
        program=program,
        work_dir=work_dir,
        corner=_flag_value(items, "--corner"),
        temperature=_flag_value(items, "--temperature"),
        sparam=_flag_value(items, "--sparam"),
        template_file=template_file,
    )


def _command_key(argv: Sequence[str]) -> str:
    """这条命令的稳定标识 —— `FakeRunner.modes` / `port_counts` / `FakeScheduler.fail_submit`
    共用的键。

    * 阶段 2（ewave）→ 产物目录 `<workDir>/<corner>_<temp>`（= `RunPaths.ewave_dir`）；
    * 阶段 1（strmout）→ `-templateFile` 的路径；
    * 都不是 → `argv[0]`。

    为什么用产物目录而不是 `run_id`：runner 拿不到 `Run`，只拿得到 argv（`RunnerProtocol`
    的约定）。而产物目录是这条命令**在磁盘上的唯一身份**，两个不同的 run 不可能撞
    （撞了就是 D2 那个静默覆盖的坑，那种情况本来就该炸）。

    ✅ **`Run.run_id` 正好是这个键的一个合法后缀**，所以 driver / 测试可以直接拿 run_id
    当键使：`run_id` = `<design>/<axes_slug>/<ewave_dir>`，而产物目录 =
    `<batch>/runs/` + 那三段（`RunPaths.run_dir` 的拼法，见 `core.layout.compute_run_paths`）。
    ⚠️ 反过来，只写 `<ewave_dir>`（`typical_-40_0`）会同时命中所有 design 的同名组合 ——
    那有时正是想要的（"所有 design 的这个 corner 都挂"），但别当成按 run 指定。
    """
    parsed = _parse_argv(argv)
    if parsed.kind == "ewave":
        return parsed.out_dir or parsed.program
    if parsed.kind == "strmout":
        return parsed.template_file
    return parsed.program


def _lookup(key: str, table: Mapping[str, object] | None) -> object | None:
    """在 `table` 里找 `key` 对应的条目。**确定性的匹配，三步**：

    1. 精确相等；
    2. 按 `/` 分段的**后缀**（键 `"typical_-40_0"` 命中 `".../runs/dA/base/typical_-40_0"`，
       也就是"所有 design 的这个 corner/temp 组合"；键 `"dA/base/typical_-40_0"`
       —— 正好是 `Run.run_id` —— 只命中那一个 run）；
    3. 按 `/` 分段的**中段**（键 `"dA"` 命中 `".../runs/dA/base/typical_-40_0"`，
       也就是"这个 design 的所有 run"）。

    🚨 第 2、3 步都要求**整段**命中（`"/" + k` / `"/" + k + "/"`），**绝不做子串匹配**。
    这是 MVP 那个真 bug 的同型防线：排除规则写成前缀，`--sparam` 吃掉了 `--sparamImpedance`，
    两边同时被跳过，diff 空得非常好看但根本没比。这里要是退化成子串，
    `"40_0"` 就会悄悄命中 `typical_-40_0` 和 `cworst_-40_0`，而失败模式装错了是查不出来的。

    多个键都命中时取**最长的那个**（最具体），再同长就按 `sorted` 取第一个。
    ⚠️ 这两条排序规则不是装饰：没有它们，同一份输入在两次运行里可能给出不同的模式，
    而"失败无法复现"在无人值守时等于"查不了"。
    """
    if not table:
        return None
    if key in table:
        return table[key]
    hits = [k for k in table if k and (key.endswith("/" + k) or ("/" + k + "/") in key)]
    if not hits:
        return None
    hits.sort(key=lambda k: (-len(k), k))
    return table[hits[0]]


# --------------------------------------------------------------------------
# FakeRunner
# --------------------------------------------------------------------------

DEFAULT_PORT_COUNT = 4
"""假产物的默认端口数。**故意是个小的合成值** —— 真实 design 的端口数是站点信息
（硬约束 1b），测试要什么数字自己传。"""

_CANCELLED_RETURNCODE = -1
"""被取消 / 超时杀掉时的退出码。负数 = 进程不是自己退的（POSIX 下被信号杀死也是负数）。"""


class FakeRunner:
    """假的子进程执行器 —— 满足 `model.RunnerProtocol`。**产物真的写到磁盘上。**

    用法（默认全部成功）::

        runner = FakeRunner()
        result = runner.run(plan.argv, cwd=plan.cwd)

    按 run 指定失败模式::

        runner = FakeRunner(
            FakeFailureMode.SUCCESS,
            modes={"typical_-40_0": FakeFailureMode.ZERO_BYTE_OUTPUT},
        )

    键的匹配规则见 `_command_key` / `_lookup`：精确，或按 `/` 分段的后缀。

    **不 sleep**：`duration_seconds` 只是填进 `RunResult` 的一个数字，
    不会真的等 —— 12-run 假批次要在毫秒级跑完。
    """

    def __init__(
        self,
        mode: FakeFailureMode = FakeFailureMode.SUCCESS,
        *,
        modes: Mapping[str, FakeFailureMode] | None = None,
        port_count: int = DEFAULT_PORT_COUNT,
        port_counts: Mapping[str, int] | None = None,
        duration_seconds: float = 1.0,
        missing_tools: Sequence[str] = (),
    ) -> None:
        """
        * `mode` —— 默认模式，`modes` 没命中时用它。
        * `modes` —— 键 → 模式（键的匹配规则见 `_lookup`）。
        * `port_count` / `port_counts` —— 产物的端口数（`.s{n}p` 的 n）。
          `port_counts` 是 per-run 覆盖，键同上。**per-run 可变是必须的**：
          同一批次里两个 design 的端口数本来就不同（BRIEF §5：17 端口的电感 vs 16 端口的走线）。
        * `duration_seconds` —— 填进 `RunResult.duration_seconds` 的假墙钟，也是
          `timeout` 的判据（`timeout < duration_seconds` ⇒ 超时）。
        * `missing_tools` —— 这些**basename** 的程序视为"PATH 上没有"，抛 `ToolMissingError`
          （`RunnerProtocol` 约定的那条）。给 driver 测"工具缺失"用。
        """
        self.mode = mode
        self.modes: dict[str, FakeFailureMode] = dict(modes or {})
        self.port_count = int(port_count)
        self.port_counts: dict[str, int] = {k: int(v) for k, v in dict(port_counts or {}).items()}
        self.duration_seconds = float(duration_seconds)
        self.missing_tools = tuple(missing_tools)
        self.calls: list[tuple[str, ...]] = []
        """每次 `run()` 的 argv，按调用顺序。给测试做计数断言用。"""
        self.written: list[str] = []
        """本 runner 写出来的每一个文件路径，按写入顺序。"""
        self.results: list[RunResult] = []

    # ---- 查询面（测试和 driver 都要用）--------------------------------

    def command_key(self, argv: Sequence[str]) -> str:
        """这条 argv 在 `modes` / `port_counts` 里的键。见 `_command_key`。"""
        return _command_key(argv)

    def mode_for(self, argv: Sequence[str]) -> FakeFailureMode:
        """这条 argv 会走哪个模式。**纯查询，不写盘** —— 测试拿它做期望值的对照。"""
        hit = _lookup(_command_key(argv), self.modes)
        return hit if isinstance(hit, FakeFailureMode) else self.mode

    def port_count_for(self, argv: Sequence[str]) -> int:
        """这条 argv 会产出几端口的产物（已经算进 `port_delta`）。"""
        hit = _lookup(_command_key(argv), self.port_counts)
        base = int(hit) if isinstance(hit, int) else self.port_count
        delta = _OUTCOMES[self.mode_for(argv)].port_delta
        return max(1, base + delta)

    # ---- RunnerProtocol ------------------------------------------------

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        on_line: Callable[[str], None] | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> RunResult:
        """假执行一条命令。签名逐字照抄 `model.RunnerProtocol.run`（self-test 在比）。

        约定照 Protocol 走：
        * `cancel()` 返回 True ⇒ 立刻返回 `cancelled=True`，**一个文件都不写**
          （进程还没来得及干活就被杀了）；
        * `timeout` 小于 `duration_seconds` ⇒ `timed_out=True`，同样不写产物，
          但 stdout 里留着已经产生的那几行（Protocol：调用方要看得见已经拿到的输出）；
        * `argv[0]` 的 basename 在 `missing_tools` 里 ⇒ `ToolMissingError`；
        * 其余情况按 `mode_for(argv)` 落盘 + 返回。

        `env` 被记下来但不生效（这里没有真进程）。
        """
        items = tuple(str(item) for item in argv)
        self.calls.append(items)
        if not items:
            raise ToolMissingError("argv 是空的 —— 没有可执行文件可以假装执行")
        if os.path.basename(items[0]) in self.missing_tools:
            raise ToolMissingError(
                f"{items[0]}: 找不到可执行文件（FakeRunner.missing_tools 里点名了它）"
            )

        if cancel is not None and cancel():
            result = RunResult(
                argv=items,
                returncode=_CANCELLED_RETURNCODE,
                stdout="",
                stderr="",
                duration_seconds=0.0,
                cancelled=True,
                cwd=cwd or "",
            )
            self.results.append(result)
            return result

        mode = self.mode_for(items)
        outcome = _OUTCOMES[mode]
        lines = self._log_lines(outcome.log)

        if timeout is not None and timeout < self.duration_seconds:
            # 超时**不抛异常**（Protocol）：置 timed_out 返回，输出留给调用方看。
            stdout = "\n".join(lines[:1]) + "\n" if lines else ""
            self._emit(stdout, on_line)
            result = RunResult(
                argv=items,
                returncode=_CANCELLED_RETURNCODE,
                stdout=stdout,
                stderr="",
                duration_seconds=float(timeout),
                timed_out=True,
                cwd=cwd or "",
            )
            self.results.append(result)
            return result

        parsed = _parse_argv(items)
        if parsed.kind == "ewave":
            self._write_ewave_outputs(parsed, outcome, self.port_count_for(items), lines)
        elif parsed.kind == "strmout":
            self._write_strmout_outputs(parsed, outcome, lines)
        # kind == "unknown"：认不出来的命令一个文件都不写，只给退出码和输出。
        # 这不是兜底偷懒 —— driver 会拿 runner 跑别的小命令（比如 `mkdir`），
        # 那些命令的产物不该由本模块凭空发明。

        stdout = "\n".join(lines) + "\n" if lines else ""
        stderr = "\n".join(lines) + "\n" if outcome.log in ("crash", "error") else ""
        self._emit(stdout, on_line)
        result = RunResult(
            argv=items,
            returncode=outcome.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=self.duration_seconds,
            cwd=cwd or "",
        )
        self.results.append(result)
        return result

    # ---- 内部 -----------------------------------------------------------

    def _log_lines(self, kind: str) -> list[str]:
        """一份假日志的正文。

        ⚠️ `"done"` 这一支在 SUCCESS / ZERO_BYTE_OUTPUT / SWALLOWED_WRITE_FAILURE /
        WRONG_PORT_COUNT 四个模式下**逐字相同**。这不是省事，是本模块要表达的那件事：
        **日志分辨不出成败**（BRIEF §10），所以验收只能验产物。测试里有一条
        `assertEqual(成功的日志, 0 字节那次的日志)` 把这句话钉住。
        """
        if kind == "crash":
            return list(_LOG_CRASH)
        if kind == "error":
            return list(_LOG_ERROR)
        return list(_LOG_DONE)

    def _emit(self, stdout: str, on_line: Callable[[str], None] | None) -> None:
        """逐行喂给 `on_line`（Protocol：别攒到最后）。"""
        if on_line is None:
            return
        for line in stdout.splitlines():
            on_line(line)

    def _write(self, path: str, text: str = "", *, empty: bool = False) -> None:
        """写一个文件（`empty=True` ⇒ **真的 0 字节**）。父目录自动建。

        0 字节这条路径是承重的：`verify_run_outputs` 靠 `os.path.getsize(...) == 0`
        抓 §10 那个坑，用"写一个空格"糊弄过去就等于把这条验收线拆了。
        """
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            if not empty:
                handle.write(text)
        self.written.append(path)

    def _write_log(self, path: str, lines: Sequence[str]) -> None:
        self._write(path, FAKE_LOG_HEADER + "\n".join(lines) + "\n")

    def _write_ewave_outputs(
        self,
        parsed: _Parsed,
        outcome: _Outcome,
        port_count: int,
        lines: Sequence[str],
    ) -> None:
        """阶段 2 的落盘 —— 形状照 BRIEF §5「官方流程的既有布局」那棵树。

        产物基名 = `<--sparam 的值>_<corner>_<temp>`，与官方 `<Cell>_<corner>_<temp>.s17p`
        同形。`--sparam` 的取值来自 argv（`core.cmd._locked_flags` 把它设成 `Design.cell`），
        **源码里没有任何 cell 名**。
        """
        out_dir = parsed.out_dir
        if not out_dir:
            return
        os.makedirs(out_dir, exist_ok=True)
        stem = ewave_dir_name(parsed.corner, parsed.temperature)
        base = f"{parsed.sparam}_{stem}" if parsed.sparam and stem else (parsed.sparam or "output")

        if outcome.products != "none":
            empty = outcome.products == "empty"
            for name, kind in (
                (f"{base}.s{port_count}p", "S"),
                (f"{base}_sample.s{port_count}p", "S"),
                (f"{base}.y{port_count}p", "Y"),
                (f"{base}_sample.y{port_count}p", "Y"),
            ):
                self._write(
                    posixpath.join(out_dir, name),
                    _touchstone_text(port_count, kind),
                    empty=empty,
                )

        if outcome.mesh:
            for name in ("pmrg.gtxt", "pmrg.gtxt.mrg", "pmsh.gtxt", "pmsh.gtxt.msh"):
                # mesh 中间件：归档（D5）要删的就是这几个，不写的话 archive_run 无事可做。
                self._write(posixpath.join(out_dir, name), "FAKE mesh intermediate\n")

        if outcome.resist != "none":
            # `resist.rst == 0 字节` 是 §10 那次事故的**确定指纹**，logparse（P4）要认它。
            self._write(
                posixpath.join(out_dir, "resist.rst"),
                "FAKE resistance extraction result\n",
                empty=outcome.resist == "empty",
            )

        self._write_log(posixpath.join(out_dir, "ewave.log"), lines)
        self._write_log(posixpath.join(out_dir, "emsolver.log"), lines)

    def _write_strmout_outputs(
        self, parsed: _Parsed, outcome: _Outcome, lines: Sequence[str]
    ) -> None:
        """阶段 1 的落盘：GDS 落在模板里 `runDir`/`strmFile` 指的位置。

        **不在这里发明路径** —— `tools.strmout` 把它写进了 `gdsout_setup`，
        我们读那份文件（真实的 strmout 也是这么知道该写哪儿的）。
        模板读不到（dry-run 没写它）⇒ 什么都不写，只给退出码。

        ⚠️ `WRONG_PORT_COUNT` 在阶段 1 退化成成功：端口数是阶段 2 的概念，
        GDS 里没有端口数这回事。这是**有意的**，测试里有一条盯着它。
        """
        setup_path = parsed.template_file
        try:
            with open(setup_path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            return
        fields = _parse_setup_fields(text)
        run_dir = fields.get("runDir", "")
        strm_file = fields.get("strmFile", "")
        if not run_dir or not strm_file:
            return
        log_file = fields.get("logFile", "") or posixpath.join(run_dir, "gds_out.log")

        if outcome.products != "none":
            self._write(
                posixpath.join(run_dir, strm_file),
                "FAKE GDS written by ewave_batch.sched.fake\n",
                empty=outcome.products == "empty",
            )
        self._write_log(log_file, lines)


def _parse_setup_fields(text: str) -> dict[str, str]:
    """`gdsout_setup` → `{字段: 值}`，只取本模块需要的那几个。

    ⚠️ 故意**不** import `tools.strmout.parse_gdsout_fields`：那个函数对重复字段抛
    `SpecError`（D1c 的守卫，那是对的），而假 runner 的职责是"照单收下命令给的东西"，
    不是替 strmout 校验模板。假 runner 一旦开始挑剔输入，它就不再是替身，
    而是第二套业务逻辑 —— 测试就会开始测它自己。
    """
    fields: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith((";", "#")):
            continue
        parts = stripped.split(None, 1)
        key = parts[0]
        value = parts[1].strip() if len(parts) > 1 else ""
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        fields.setdefault(key, value)
    return fields


def _touchstone_text(port_count: int, kind: str) -> str:
    """一份**明显是假的** Touchstone 正文。

    `# HZ S RI R 50` 这一行的形状抄自真实产物（BRIEF §10「修掉的一个真 bug」：
    生产 `.sNp` 的 option line 是 `# HZ`，不是 GHz）—— 留着它，将来谁拿假产物
    去试 logparse 时至少单位是对的。数据行是编的，只有一行，因为
    `verify_run_outputs` 根本不读内容（它验的是存在/非空/端口数）。
    """
    return (
        "! FAKE Touchstone written by ewave_batch.sched.fake - not real data\n"
        f"! ports: {port_count}\n"
        f"# HZ {kind} RI R 50\n"
        "5.0e9 0.1 0.0\n"
    )


# --------------------------------------------------------------------------
# FakeScheduler
# --------------------------------------------------------------------------

DEFAULT_EPOCH = "2026-01-01T00:00:00Z"
"""假时钟的起点。**固定值** —— 时间戳不许来自墙钟，否则同一份输入两次跑出不同的 batch.json。"""


class FakeScheduler:
    """假的提交后端 —— 满足 `model.SchedulerProtocol`，模拟 Donau 侧的时间线。

    ```
    submit()  → job id + JobState.PENDING      （pending = 已 dsub、在排队，Donau 自己的词；
    poll() ×p → PENDING                          "还没提交" 叫 ready，那是 RunStatus 的事）
    poll() ×r → RUNNING
    poll()    → 终态：这一拍才**真的执行** FakeRunner（产物此刻才落盘）
    ```

    **推进只靠"第几次 poll"，不靠墙钟，也不 sleep。**

    ⚠️ `JobState.DONE` 只代表进程结束了（退出码 0）。**它不等于 run 成功** ——
    eWave 崩了也 `exit=0`（BRIEF §10）。判 `RunStatus.DONE` 必须走
    `core.layout.verify_run_outputs`。本类刻意让 `EXIT_ZERO_BUT_CRASHED` 给出
    `JobState.DONE`，好让"只看 job 状态"的 driver 当场露馅。
    """

    def __init__(
        self,
        runner: FakeRunner | None = None,
        *,
        pending_polls: int = 1,
        running_polls: int = 1,
        jitter_polls: int = 0,
        seed: int = 0,
        prefix: str = "fake",
        epoch: str = DEFAULT_EPOCH,
        seconds_per_poll: int = 15,
        fail_submit: Sequence[str] = (),
    ) -> None:
        """
        * `runner` —— 终态那一拍用它真的写产物。`None` ⇒ 自己造一个默认 `FakeRunner()`。
        * `pending_polls` / `running_polls` —— 有几次 poll 分别看到 `pending` / `running`。
          到终态一共需要 `pending + running + 1` 次 poll。
        * `jitter_polls` —— 每个 job 额外排队 0…jitter 次，由 `seed` + job id 的 crc32 决定。
          **可复现的抖动**：同样的 seed 给同样的时间线（模块 docstring 的约束 1）。
        * `fail_submit` —— 这些键（匹配规则同 `FakeRunner.modes`）提交时抛 `SchedulerError`。
        """
        self.runner = runner if runner is not None else FakeRunner()
        self.pending_polls = int(pending_polls)
        self.running_polls = int(running_polls)
        self.jitter_polls = int(jitter_polls)
        self.seed = int(seed)
        self.prefix = prefix
        self.epoch = epoch
        self.seconds_per_poll = int(seconds_per_poll)
        self.fail_submit = tuple(fail_submit)
        self.jobs: dict[str, Job] = {}
        self.plans: dict[str, CommandPlan] = {}
        self.results: dict[str, RunResult] = {}
        self.poll_calls = 0
        self.submit_calls = 0
        self._polls_seen: dict[str, int] = {}
        self._tick = 0

    # ---- 假时钟 ---------------------------------------------------------

    def _stamp(self) -> str:
        """当前假时刻。每次 submit / poll 走一格 —— 与墙钟无关，测试可逐字断言。"""
        base = datetime.strptime(self.epoch, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
        moment = base + timedelta(seconds=self._tick * self.seconds_per_poll)
        return moment.strftime(TIMESTAMP_FORMAT)

    def _pending_budget(self, job_id: str) -> int:
        """这个 job 要排多少次队。`jitter_polls=0` ⇒ 全批一致。

        抖动用 `zlib.crc32(seed:job_id)`，**不是 `random`**：
        同一个 (seed, job_id) 永远给同一个数，跨进程、跨平台、跨 Python 版本都一样
        （`hash()` 不行 —— 它带进程级随机盐）。
        """
        if self.jitter_polls <= 0:
            return self.pending_polls
        digest = zlib.crc32(f"{self.seed}:{job_id}".encode("utf-8"))
        return self.pending_polls + digest % (self.jitter_polls + 1)

    # ---- SchedulerProtocol ---------------------------------------------

    def submit(self, plan: CommandPlan, *, resources: str = "", name: str = "") -> Job:
        """提交一条命令，返回带 `job_id` 的 `Job`（`JobState.PENDING`）。

        **此刻什么都不执行**，产物要等 job 走到终态那一拍才落盘 —— 真实的 dsub 就是这样，
        提交完成 ≠ 算完了。job id 按提交顺序生成（`fake-0001`…），确定性。
        """
        self.submit_calls += 1
        key = _command_key(plan.argv)
        if _lookup(key, {k: True for k in self.fail_submit}) is not None:
            raise SchedulerError(f"提交失败（FakeScheduler.fail_submit 点名了 {key!r}）")

        self._tick += 1
        job_id = f"{self.prefix}-{len(self.jobs) + 1:04d}"
        job = Job(
            job_id=job_id,
            scheduler="fake",
            state=JobState.PENDING,
            name=name or plan.run_id or plan.design_key,
            submitted_at=self._stamp(),
            resources=resources,
            stdout_path=plan.log_path,
            raw=f"fake submit: {plan.argv[0] if plan.argv else '<empty argv>'} ({len(plan.argv)} argv items)",
        )
        self.jobs[job_id] = job
        self.plans[job_id] = plan
        self._polls_seen[job_id] = 0
        return replace(job)

    def poll(self, jobs: Sequence[Job]) -> dict[str, Job]:
        """一次查一批（真实现是一次 `djob` 查全部）。返回 `job_id` → 更新后的 Job。

        * 查不到的 job → `JobState.UNKNOWN` + **保留原 Job**，不凭空判 failed
          （Protocol 的原话：调度器短暂查不到是常事）。想让它继续走时间线，
          用 `adopt()` 把它重新挂上来 —— resume 场景就是这么用的。
        * 每调用一次本方法，被查的每个 job 的计数 +1；到点了才换状态。
        * 终态那一拍**同步**跑 runner（产物此刻落盘），然后按退出码给
          `DONE` / `FAILED`。⚠️ 退出码 0 ≠ 成功，见类 docstring。
        """
        self.poll_calls += 1
        self._tick += 1
        out: dict[str, Job] = {}
        for job in jobs:
            known = self.jobs.get(job.job_id)
            if known is None:
                out[job.job_id] = replace(job, state=JobState.UNKNOWN)
                continue
            out[job.job_id] = replace(self._advance(known))
        return out

    def cancel(self, job: Job) -> bool:
        """取消一个 job。已经结束的返回 False 而不是抛异常（Protocol）。"""
        known = self.jobs.get(job.job_id)
        if known is None:
            return False
        if known.state in (JobState.DONE, JobState.FAILED, JobState.CANCELLED):
            return False
        self._tick += 1
        known.state = JobState.CANCELLED
        known.ended_at = self._stamp()
        return True

    # ---- resume 用 -------------------------------------------------------

    def adopt(self, job: Job, plan: CommandPlan | None = None, *, polls_seen: int = 0) -> Job:
        """把一个**别的进程提交过的** job 重新挂到本调度器的时间线上。

        resume 的形状（D7）：上一次跑到一半进程没了，`batch.json` 里留着 `pending` /
        `running` 的 run 和它们的 `job_id`；新进程起来后 job 在队列里还活着，
        一次 `djob` 就能查到。本机没有那个队列，`adopt` 就是它的替身。

        `plan` 给了才可能在终态写产物 —— 没有 plan 的 job 走到终态只会有退出码
        没有文件，那正是"我们不知道它当初要跑什么"的诚实表示。
        """
        self.jobs[job.job_id] = job
        self._polls_seen[job.job_id] = int(polls_seen)
        if plan is not None:
            self.plans[job.job_id] = plan
        return replace(job)

    # ---- 内部 -----------------------------------------------------------

    def _advance(self, job: Job) -> Job:
        """把一个 job 往前推一拍。已经是终态的原样返回（幂等 —— runner 只跑一次）。"""
        if job.state in (JobState.DONE, JobState.FAILED, JobState.CANCELLED):
            return job
        seen = self._polls_seen.get(job.job_id, 0) + 1
        self._polls_seen[job.job_id] = seen
        pending = self._pending_budget(job.job_id)

        if seen <= pending:
            job.state = JobState.PENDING
            return job
        if seen <= pending + self.running_polls:
            if job.state is not JobState.RUNNING:
                job.started_at = self._stamp()
            job.state = JobState.RUNNING
            return job

        plan = self.plans.get(job.job_id)
        if plan is None:
            # adopt 进来但没给 plan：进程结束了，我们不知道它跑的是什么 ⇒ 不发明产物。
            job.state = JobState.DONE
            job.exit_code = 0
            job.ended_at = self._stamp()
            job.raw = "fake: 终态，但没有 plan —— 没有产物落盘（adopt 时没给 plan）"
            return job
        if not job.started_at:
            job.started_at = self._stamp()
        result = self.runner.run(plan.argv, cwd=plan.cwd or None, env=plan.env or None)
        self.results[job.job_id] = result
        job.exit_code = result.returncode
        job.state = JobState.DONE if result.returncode == 0 else JobState.FAILED
        job.ended_at = self._stamp()
        return job
