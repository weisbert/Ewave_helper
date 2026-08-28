"""`ewave_batch.sched.driver` —— 两阶段 DAG 的编排者。**整个工具的状态机在这。**

```
阶段 1（per-design）      strmout -templateFile <渲染出的 gdsout_setup>  →  <design>.gds
阶段 2（per-design×组合） <调度器> ewave --workDir=… --corner=… …        →  .sNp  →  验收 → 归档
```

## 形状（BRIEF §12「我拍板的实现决定」，逐条照做）

* **并发模型 = 单线程轮询**，不用线程池。对外只有一个 `tick()`：
  **CLI 用 `while` 驱动（`run_batch`）、GUI 用 `after()` 驱动同一份代码**。
  没有锁 ⇒ 状态机可推理，resume 天然（状态全在 `BatchState` 里，每拍原子落盘）。
* **阶段 1 是 per-design 的**（D1a：GDS 不随设定变，整个设定矩阵共用一份）。
  阶段 1 失败 ⇒ 该 design 整列 `skipped`，一个 job 都不提交（提交必然失败的 job 是浪费配额）。
* **阶段 2 的失败不 fail-fast**：一个组合挂了，其余照跑。
* **不自动重试。** 失败停在 `failed`，`resume` 一键补 —— 把决定权留给人。
* **有界并发**：同时在飞的 job 数 ≤ `BatchOptions.max_parallel`。
* **每推进一步就原子写 `batch.json`**（`core.layout.write_batch_state`）。resume 全靠它。

## 🚨 `done` 的判据只有一条：`core.layout.verify_run_outputs`

红区实测过三条「失败信号不可靠」（BRIEF §10，也是本阶段的验收契约）：

| 现象 | 实测 | 于是本模块**不许**这么判 |
|---|---|---|
| eWave 崩了也 `exit=0` | payload 打的是 `ewave exit=0`，进程其实 abort 了 | ❌ 拿 `Job.exit_code` 判 done |
| 崩了还留 0 字节产物、日志照样报 done | `Execute eresist done.` + 0 字节 `resist.rst` | ❌ 拿「文件存在」或日志措辞判 done |
| 写失败被吞 | 配额爆了，整条链路零错误输出 | ❌ 拿「没报错」判 done |

⇒ **`JobState.DONE` 只代表进程结束了**。run 的成败一律走
`verify_run_outputs`（存在 + 非空 + 端口数对）。本模块从头到尾没有
`{JobState.DONE: RunStatus.DONE}` 这样一张表 —— 状态映射走
`sched.donau.run_status_for_job_state`，它对全部终态返回 `None`（= 这里没有答案，去验产物）。

## 为什么阶段 1 不走调度器

`strmout` 在 MVP 里是**在提交机上直接跑通的**（BRIEF §10 step1：cd 进我们自己的
`cdswork/`，`strmout -templateFile <setup>`，exit 0）。把它 dsub 出去要求计算节点上有
Cadence license 和能看见 library 的 `cds.lib` —— 那是**没验证过**的路。
所以阶段 1 用 `RunnerProtocol` 同步执行，且**一拍最多跑一个 design**：
`tick()` 不会去等队列，但它确实会在这一拍里等一条本地命令回来。
（`StreamoutTask.job` 因此一直是 `None`；将来真要 dsub 阶段 1，改这一个方法即可。）

🚨 本文件零站点标识符：库名 / cell 名 / 路径 / 账号 / 队列全部来自
`Design` / `SiteFacts`（运行时解析），源码里一个真实取值都没有（CLAUDE.md 硬约束 1b）。
"""

from __future__ import annotations

import os
import posixpath
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone

from ..core import layout, logparse, matrix
from ..model import (
    BATCH_JSON_NAME,
    RUNS_CSV_NAME,
    TIMESTAMP_FORMAT,
    BatchState,
    CommandPlan,
    Design,
    DriverEvent,
    DriverProtocol,
    EventKind,
    EwaveBatchError,
    Job,
    JobState,
    LogFacts,
    PlanContext,
    Run,
    RunnerProtocol,
    RunPaths,
    RunResult,
    RunStatus,
    SchedulerProtocol,
    SiteFacts,
    SpecError,
    StateError,
    StreamoutTask,
    ToolMissingError,
    TickReport,
    VerifyReport,
)
from ..tools import ewave as _ewave_tool
from ..tools import strmout as _strmout

# `is_terminal` / `run_status_for_job_state` 只吃 `model.JobState`，与后端无关 ——
# 它们住在 donau.py 只是因为那个文件先写出来。**这里刻意 import 而不是再写一份**：
# 第二份 JobState→RunStatus 的映射正是 BRIEF §10 三条实测要防的东西
# （它跑起来一切正常，只是结果是假的）。见返回结果的 interface_change_requests。
from .donau import is_terminal as _job_is_terminal
from .donau import run_status_for_job_state as _run_status_for_job_state

_TERMINAL_RUN_STATUSES = frozenset({RunStatus.DONE, RunStatus.FAILED, RunStatus.SKIPPED})
"""run 的终态。`tick()` 只在这三个之外的 run 上做事。"""

_IN_FLIGHT_RUN_STATUSES = frozenset({RunStatus.PENDING, RunStatus.RUNNING})
"""已经提交出去、占着并发配额的 run。"""

_UNKNOWN_POLL_LIMIT = 3
"""连着几拍查不到 job 就启动兜底（去验产物）。

调度器短暂查不到是常事（`SchedulerProtocol.poll` 的原话），所以不能第一拍就判死。
但 LSF 系的队列会在作业结束一段时间后把它**彻底忘掉** —— 那时候 poll 永远返回
`UNKNOWN`，只等状态的 driver 会把这个 run 永远挂在"在飞"里，整个批次卡住。
⇒ 问不到就去看磁盘：产物验得过就是 done，验不过就是 failed。
"""

_STALL_TICK_LIMIT = 5
"""`run_batch` 的卡死保险丝：**一个 job 都不在飞**且连着这么多拍没有任何变化 ⇒ 认输退出。

只在"没有在飞的 job"时计数 —— 真跑的时候 job 在队列里排几个小时，每拍都是"无变化"，
拿无变化本身当卡死判据会在真实批次上误杀（而且是杀在最贵的时候）。
"""

_CDS_LIB_NAME = "cds.lib"
_CDS_LIB_SEARCH_UP = 4
"""从官方 run 目录往上找几层 `cds.lib`（BRIEF §10 step1：`CDSWORK_MODE=include`）。

官方 run 目录形如 `<workarea>/ewave_simulation/<design>/`，`cds.lib` 在 `<workarea>` ——
也就是往上两层。多找两层是给别的站点布局留余量。**路径本身不进源码，现场解析。**
"""

_SAMPLE_SUFFIX_MARK = "_sample."
"""扁平区里 `_sample` 那一份的接缝（`core.layout._flat_suffix` 产生的两种形状之一）。"""

_MESSAGE_SEP = "; "

_MAX_LOG_ERRORS_IN_MESSAGE = 3
"""失败原因里最多带几条 eWave 自己的报错。

上限存在的理由不是省地方，是**可读**：一次崩溃的日志能刷出上百条 `[error]`，
而它们几乎全是同一个根因的回声。前几条就是根因；把一百条塞进 `Run.message`
只会把"为什么失败"埋掉，而那正是这条 message 唯一的用途。
全文永远在日志文件里，界面上有 `Output log` 那扇窗（`gui._ui._RunLogWindow`）。"""

_MAX_MESSAGE_CHARS = 1200
"""`Run.message` 的上限。它每拍都被原子重写进 `batch.json`，塞一整份验收报告会让状态文件肿。"""


# --------------------------------------------------------------------------
# 小工具（全是私有的：本模块的公开面由 model.FROZEN 钉死，别长出没人盯着的 API）
# --------------------------------------------------------------------------


def _posix(path: str) -> str:
    """路径一律用 `/`。最终跑在 Linux 上，本机比对字符串也一致。"""
    return str(path).replace("\\", "/")


def _utcnow() -> str:
    """UTC、秒精度（`model.TIMESTAMP_FORMAT`）。别往 `batch.json` 里写本地时间。"""
    return datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)


def _facts_describe_design(design: Design, facts: SiteFacts) -> bool:
    """这份 `SiteFacts` 描述的**就是**这个 design 吗（library / cell / view 三段全中）。

    **私有**（`_` 开头）是有意的：它只服务本模块的 `_expected_port_count`，
    没有跨模块使用者。`docs/INTERFACES.md` 说"只加新符号也要更新 FROZEN，
    否则没人替你盯着它别漂"—— 而给一个模块内 helper 走一次 `[interface-change]`
    是把冻结面当垃圾桶用。测试直接 import 这个私有名（`gui._ui` 也是这么被测的）。

    `SiteFacts` 里的 `library` / `top_cell` / `view` 一直被解析出来却没人用 ——
    拼命令时一律走 `Design`（"facts 是官方那次跑的是谁，而我们要导的是用户点名的这个"）。
    这里第一次用到它们，而且是当**守卫**用，不是当输入用。

    为什么需要这道守卫（用户 2026-08-20 问出来的）：`_expected_port_count` 的第三级
    回退拿官方那条命令的端口表当期望值，注释写着"官方跑的就是这个 design"——
    **这个前提只在官方 run 目录属于同一个 design 时成立**。而本工具的主用例恰恰是
    "跑官方 GUI 没跑过的东西"：一个全新的 design 根本没有自己的官方目录，
    用户只能指同 PDK 里别人的那个（ptxt / key / layerMap / 队列全都对，唯独端口表不是）。
    不设防的话，第一批 run 会全判 failed，报
    `port count mismatch: output has 12 ports, expected 17`，而那个 17 跟这个设计毫无关系、
    `.sNp` 其实是好的。第二级（批次内已验过的产物）要跑完一个 run 才有，救不了第一次。

    **三段都要求非空且逐字相等**，尤其 view 不能放过：pin 长在 cellview 上，
    官方跑 `layout`、我们跑一个为 EM 派生的 cellview，端口集合可以是不一样的
    （BRIEF §5：view 不是常量）。大小写敏感 —— 端口名和库名在 Cadence 里就是敏感的，
    别在这儿养成忽略大小写的手感。

    对不上 ⇒ 调用方拿到 `None` = **没有期望值**，而不是一个错的期望值。
    那是降级成"这一项不交叉核对"，不是降级成"判人失败"。
    """
    triples = (
        (design.library, facts.library),
        (design.cell, facts.top_cell),
        (design.view, facts.view),
    )
    return all(mine.strip() and mine.strip() == theirs.strip() for mine, theirs in triples)


def _parse_stamp(text: str) -> datetime | None:
    """`TIMESTAMP_FORMAT` 的字符串 → datetime。认不出返回 None（**不拿 0 冒充**）。"""
    if not text:
        return None
    try:
        return datetime.strptime(text, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _elapsed(start: str, end: str) -> float | None:
    """两个时间戳之间的秒数。任一认不出就返回 None —— "没测到"和 0 秒是两回事。

    ⚠️ 这里**只用时间戳相减**，不读墙钟：时间戳来自调度器（真实现是 Donau 的记账，
    测试里是 `FakeScheduler` 的假时钟），于是同一份输入永远给同一个数字。
    """
    a = _parse_stamp(start)
    b = _parse_stamp(end)
    if a is None or b is None:
        return None
    delta = (b - a).total_seconds()
    return delta if delta >= 0 else None


def _log_error_detail(facts: LogFacts | None) -> str:
    """日志里的报错 → 一句能贴进 `Run.message` 的话。日志没自曝报错就返回空串。

    **不判断成败，只转述。** `LogFacts.ok` 与"这个 run 成了"之间隔着产物验收
    （`logparse` 模块 docstring 那张表），这里同理：把 eWave 说的话原样带出来，
    结论仍然由 `verify_run_outputs` 下。
    """
    if facts is None:
        return ""
    errors = [" ".join(str(line).split()) for line in facts.errors if str(line).strip()]
    if not errors:
        return ""
    shown = errors[:_MAX_LOG_ERRORS_IN_MESSAGE]
    text = "eWave's own log says: " + _MESSAGE_SEP.join(shown)
    extra = len(errors) - len(shown)
    if extra > 0:
        text += f" (+{extra} more like it in the log)"
    return text


def _clip(text: str) -> str:
    """把要写进 `batch.json` 的消息截短。"""
    body = " ".join(str(text).split())
    if len(body) <= _MAX_MESSAGE_CHARS:
        return body
    return body[: _MAX_MESSAGE_CHARS - 3] + "..."


def _find_cds_lib_root(start_dir: str, *, levels: int = _CDS_LIB_SEARCH_UP) -> str:
    """从 `start_dir` 往上找含 `cds.lib` 的目录，返回那个目录（找不到返回空串）。

    **运行时发现，不是配置项**（CLAUDE.md 硬约束 1b）：官方 run 目录在
    `<workarea>/ewave_simulation/<design>/`，`cds.lib` 就在 `<workarea>` ——
    往上走两层就撞上了，源码里因此不需要出现任何一段真实路径。
    """
    current = _posix(start_dir).rstrip("/")
    for _ in range(max(0, levels) + 1):
        if not current:
            break
        if os.path.isfile(posixpath.join(current, _CDS_LIB_NAME)):
            return current
        parent = posixpath.dirname(current)
        if parent == current:
            break
        current = parent
    return ""


def _write_text(path: str, text: str) -> None:
    """写一份文本文件，**行尾恒为 LF**（红区 bash/awk 吃不了 CRLF）。父目录自动建。"""
    target = _posix(path)
    parent = posixpath.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


class Driver:
    """两阶段 DAG 的驱动器 —— 满足 `model.DriverProtocol`。

    用法（CLI 与 GUI **共用**这一个对象，区别只在谁来敲 `tick()`）::

        driver = make_driver(state, contexts, scheduler, runner)
        while not driver.tick().finished:      # CLI：run_batch 就是这个 while
            ...                                # GUI：root.after(ms, lambda: driver.tick())

    构造参数（`make_driver` 是唯一的正门，别直接碰 `__init__`）：

    * `state` —— `core.spec.spec_to_batch` 造出来的，或 `core.layout.read_batch_state`
      读回来的。driver **就地改它**，每拍原子落盘。
    * `contexts` —— `design_key` → `PlanContext`（坐标是 per-design 解析的）。少一个 → `SpecError`。
    * `scheduler` / `runner` —— 可注入的执行面（`sched.fake` 是本机替身，`sched.donau` 是真提交）。
    * `on_event` —— 每条 `DriverEvent` 都会立刻回调一次（CLI 打印、GUI 刷表，同一份代码）。
    """

    def __init__(
        self,
        state: BatchState,
        contexts: Mapping[str, PlanContext],
        scheduler: SchedulerProtocol,
        runner: RunnerProtocol,
        *,
        on_event: Callable[[DriverEvent], None] | None = None,
    ) -> None:
        if not state.batch_dir:
            raise StateError(
                "BatchState.batch_dir is empty - no idea where the batch lands, so "
                "batch.json / runs/ / sparam/ have nowhere to go"
            )
        self._state = state
        self._contexts: dict[str, PlanContext] = dict(contexts)
        self._scheduler = scheduler
        self._runner = runner
        self._on_event = on_event
        self._cancelled = False
        self._dry_run_done = False
        self._plans: dict[str, CommandPlan] = {}
        self._paths: dict[str, RunPaths] = {}
        self._unknown_polls: dict[str, int] = {}
        self._port_counts: dict[str, int] = {}
        self._port_guard_skipped: set[str] = set()
        """官方 run 目录不属于这个 design ⇒ 端口数**这一项**没得核对的那些 design。
        每个 design 只播一次 warning（`_warn_port_guard_once`）——
        静默跳过一道安全检查比没有这道检查更糟，但每个 run 播一次就成了噪声。"""
        self._port_guard_warned: set[str] = set()

        self._designs: dict[str, Design] = {}
        for design in state.designs:
            self._designs[matrix.design_key(design)] = design

        missing = sorted(
            {
                run.design_key
                for run in state.runs
                if run.design_key and run.design_key not in self._contexts
            }
        )
        if missing:
            raise SpecError(
                "contexts is missing the PlanContext of these designs: "
                + ", ".join(missing)
                + ". Site coordinates are resolved per design (each design has its own official run "
                "dir), so without one we cannot build its command - better to fail now than halfway through"
            )

        self._ensure_streamout_tasks()
        self._seed_port_counts()

    # ---- 建账 -----------------------------------------------------------

    def _ensure_streamout_tasks(self) -> None:
        """每个 design 一条阶段 1 的账。`core.spec.spec_to_batch` 已经建过就不重复建。"""
        have = {task.design_key for task in self._state.streamout}
        for key, design in self._designs.items():
            if key in have:
                continue
            if design.gds_path:
                # spec 直接给了 GDS ⇒ 阶段 1 的产物本来就在。标 DONE 而不是 SKIPPED：
                # SKIPPED 在本模块里意味着"这一列不跑"，会把整批 solve 静默吞掉。
                self._state.streamout.append(
                    StreamoutTask(
                        design_key=key,
                        status=RunStatus.DONE,
                        gds_path=design.gds_path,
                        message="the spec gave gds_path directly, stage 1 is not needed",
                    )
                )
            else:
                self._state.streamout.append(StreamoutTask(design_key=key))

    def _seed_port_counts(self) -> None:
        """resume 时把「这个 design 的产物是几端口」从已归档的文件名里捡回来。

        端口数是本批次内**互相**校验用的（`--all` 的代价：pin 集合一变全体编号平移，
        而且静默 —— BRIEF §5）。不捡回来的话，resume 之后新跑的 run 就没有对照了。
        """
        for run in self._state.runs:
            if run.status is not RunStatus.DONE:
                continue
            for artifact in run.artifacts:
                count = layout.port_count_from_suffix(artifact)
                if count:
                    self._port_counts.setdefault(run.design_key, count)
                    break

    # ---- DriverProtocol -------------------------------------------------

    @property
    def state(self) -> BatchState:
        """当前状态。调用方**只读** —— 想改状态就走 `tick()`。"""
        return self._state

    def summary(self) -> dict[str, int]:
        """`RunStatus.value` → 条数（只数阶段 2 的 run，阶段 1 的账在 `state.streamout`）。"""
        counts = {status.value: 0 for status in RunStatus}
        for run in self._state.runs:
            counts[run.status.value] = counts.get(run.status.value, 0) + 1
        return counts

    def tick(self) -> TickReport:
        """推进一拍：收割已完成的 → 验收 + 归档 → 跑阶段 1 → 提交新的 → 落盘。

        **不阻塞在队列上**：一拍做不完的事下一拍接着做（唯一会等的是阶段 1 那条本地命令，
        见模块 docstring）。任何异常都转成事件 + `RunStatus.FAILED`，不炸穿到 GUI 的事件循环。
        """
        events: list[DriverEvent] = []
        changed = False
        try:
            changed = self._advance(events)
        except Exception as exc:  # noqa: BLE001 - 兜底：GUI 的 after() 里炸一次界面就死了
            self._note(
                events,
                EventKind.WARNING,
                f"tick failed internally (this beat made no progress, the next one retries): "
                f"{exc.__class__.__name__}: {exc}",
            )
        if changed and not self._state.options.dry_run:
            try:
                self._persist()
            except Exception as exc:  # noqa: BLE001 - 落盘失败不许把批次带走
                self._note(events, EventKind.WARNING, f"writing batch.json / runs.csv failed: {exc}")
        return TickReport(
            changed=changed,
            finished=self._finished(),
            events=tuple(events),
            counts=self.summary(),
        )

    def cancel(self) -> None:
        """取消全部在飞的 job；已完成的不动。

        ⚠️ `RunStatus` **没有** cancelled 态（恰好 6 个，BRIEF §12 用户定的）。
        被取消的 run 记成 `failed` 并在 `message` 里写明是取消 —— 这样 `resume` 能补它，
        而"取消"和"真的挂了"在事后追溯时仍然分得开。
        """
        self._cancelled = True
        changed = False
        for run in self._state.runs:
            if run.status not in _IN_FLIGHT_RUN_STATUSES or run.job is None:
                continue
            try:
                self._scheduler.cancel(run.job)
            except Exception as exc:  # noqa: BLE001 - 取消失败也要把状态记下来
                self._emit(EventKind.WARNING, f"cancelling job {run.job.job_id} failed: {exc}", run=run)
            run.status = RunStatus.FAILED
            run.ended_at = run.job.ended_at or _utcnow()
            run.message = _clip(
                f"cancelled by the user (job {run.job.job_id}). RunStatus has no cancelled state, "
                "so a cancelled run is recorded as failed - resume will pick it up again"
            )
            self._emit(EventKind.FAILED, run.message, run=run)
            changed = True
        for run in self._state.runs:
            if run.status is RunStatus.READY:
                run.status = RunStatus.SKIPPED
                run.message = _clip("the batch was cancelled, this run was never submitted")
                self._emit(EventKind.SKIPPED, run.message, run=run)
                changed = True
        for task in self._state.streamout:
            # 阶段 1 还没跑的也要收尾，否则 `_finished()` 永远是 False、批次挂着不动。
            if task.status not in _TERMINAL_RUN_STATUSES:
                task.status = RunStatus.SKIPPED
                task.message = _clip("the batch was cancelled, stage 1 never ran")
                self._emit(EventKind.SKIPPED, task.message, design_key=task.design_key)
                changed = True
        if changed and not self._state.options.dry_run:
            try:
                self._persist()
            except Exception as exc:  # noqa: BLE001
                self._emit(EventKind.WARNING, f"persisting after cancel failed: {exc}")

    # ---- 一拍的主体 -----------------------------------------------------

    def _advance(self, events: list[DriverEvent]) -> bool:
        options = self._state.options
        if options.dry_run:
            return self._plan_only(events)
        if self._cancelled:
            return False
        changed = self._poll(events)
        changed = self._streamout_step(events) or changed
        changed = self._submit_step(events) or changed
        return changed

    # ---- dry-run --------------------------------------------------------

    def _plan_only(self, events: list[DriverEvent]) -> bool:
        """D8：只拼命令、只报告，**一个文件都不写、一个 job 都不提交**。

        跑一遍就结束（`finished=True`）：dry-run 没有会自己前进的东西，
        再 tick 一百次也不会有新消息。
        """
        if self._dry_run_done:
            return False
        for design_key, design in self._designs.items():
            task = self._task_for(design_key)
            if task is None or task.status is RunStatus.DONE:
                continue
            try:
                plan, _setup_text, _paths = self._streamout_plan(design)
            except EwaveBatchError as exc:
                self._note(
                    events,
                    EventKind.WARNING,
                    f"stage 1 cannot build its command: {exc}",
                    design_key=design_key,
                )
                continue
            task.argv = tuple(plan.argv)
            self._note(
                events,
                EventKind.PLANNED,
                "stage 1: " + " ".join(plan.argv),
                design_key=design_key,
            )
        for run in self._state.runs:
            try:
                plan = self._plan_for(run)
            except EwaveBatchError as exc:
                run.status = RunStatus.FAILED
                run.message = _clip(f"building the command failed: {exc}")
                self._note(events, EventKind.FAILED, run.message, run=run)
                continue
            run.argv = tuple(plan.argv)
            self._note(events, EventKind.PLANNED, "stage 2: " + " ".join(plan.argv), run=run)
        self._dry_run_done = True
        return True

    # ---- 阶段 2：轮询 + 收割 ---------------------------------------------

    def _poll(self, events: list[DriverEvent]) -> bool:
        in_flight = [
            run
            for run in self._state.runs
            if run.status in _IN_FLIGHT_RUN_STATUSES and run.job is not None and run.job.job_id
        ]
        if not in_flight:
            return False
        try:
            updated = self._scheduler.poll([run.job for run in in_flight if run.job is not None])
        except Exception as exc:  # noqa: BLE001 - 查不到状态不等于 job 挂了
            self._note(events, EventKind.WARNING, f"polling failed (retry next beat): {exc}")
            return False

        changed = False
        for run in in_flight:
            job = updated.get(run.job.job_id) if run.job is not None else None
            state = JobState.UNKNOWN if job is None else job.state
            if job is not None:
                run.job = job
            if state is JobState.UNKNOWN:
                changed = self._handle_unknown(run, events) or changed
                continue
            self._unknown_polls.pop(run.run_id, None)
            if _job_is_terminal(state):
                # ⚠️ 终态**不等于**成功：这里唯一做的事是"去验产物"。
                self._finish_run(run, events)
                changed = True
                continue
            mapped = _run_status_for_job_state(state)
            if mapped is not None and mapped is not run.status:
                run.status = mapped
                if mapped is RunStatus.RUNNING:
                    run.started_at = (run.job.started_at if run.job else "") or _utcnow()
                    self._note(events, EventKind.STARTED, f"job {run.job.job_id} started", run=run)
                changed = True
        return changed

    def _handle_unknown(self, run: Run, events: list[DriverEvent]) -> bool:
        """调度器查不到这个 job 时怎么办。见 `_UNKNOWN_POLL_LIMIT` 的 docstring。"""
        seen = self._unknown_polls.get(run.run_id, 0) + 1
        self._unknown_polls[run.run_id] = seen
        job_id = run.job.job_id if run.job is not None else "?"
        if seen < _UNKNOWN_POLL_LIMIT:
            return False
        verdict = self._verify(run, events)
        if verdict.ok:
            self._note(
                events,
                EventKind.INFO,
                f"the scheduler could not find job {job_id} for {seen} beats in a row, "
                "but the outputs verify - calling it done",
                run=run,
            )
            self._finish_run(run, events, verdict=verdict)
            return True
        run.status = RunStatus.FAILED
        run.ended_at = run.ended_at or _utcnow()
        said = _log_error_detail(self._attach_log_facts(run))
        run.message = _clip(
            f"the scheduler could not find job {job_id} for {seen} beats in a row, "
            "and the outputs do not verify either: "
            + _MESSAGE_SEP.join(verdict.reasons)
            + ((". " + said) if said else "")
        )
        self._note(events, EventKind.FAILED, run.message, run=run)
        return True

    def _finish_run(
        self, run: Run, events: list[DriverEvent], *, verdict: VerifyReport | None = None
    ) -> None:
        """一个 run 走到终态之后：**验产物**，验过才 done，然后归档。

        🚨 这个方法里没有任何一行看 `Job.exit_code` 来判成败 —— 它只作诊断
        （BRIEF §10：eWave 崩了也 `exit=0`）。
        """
        report = verdict if verdict is not None else self._verify(run, events)
        # 读日志放在**判成败之前**：失败时要拿它当失败原因，成功时它填 runs.csv 的
        # converged / peak_memory_mb 两列。读不到就是 None，不影响下面任何一条判据。
        facts = self._attach_log_facts(run)
        job = run.job
        run.ended_at = (job.ended_at if job is not None else "") or run.ended_at or _utcnow()
        elapsed = _elapsed(run.started_at, run.ended_at)
        if elapsed is not None:
            run.wall_seconds = elapsed

        if not report.ok:
            run.status = RunStatus.FAILED
            detail = _MESSAGE_SEP.join(report.reasons) or "output verification failed (no reason given)"
            code = job.exit_code if job is not None else None
            if code == 0:
                detail += (
                    ". NOTE: this job's exit code is 0 - the exit code cannot be trusted, "
                    "the outputs are the criterion (BRIEF sec. 10, measured)"
                )
            elif code is not None:
                detail += f". job exit code = {code}"
            # ★ eWave 自己的原话排在**最后**：前面那半句说的是"我们怎么判定它失败的"，
            #   这半句说的是"它自己说它怎么了" —— 后者才是人要拿去改的东西，
            #   而 `_clip` 从尾巴上截，所以两者顺序反过来会先截掉有用的那半。
            #   （所以 `_MAX_LOG_ERRORS_IN_MESSAGE` 卡得住，1200 字符也够。）
            said = _log_error_detail(facts)
            if said:
                detail += ". " + said
            run.message = _clip(detail)
            self._note(events, EventKind.FAILED, run.message, run=run)
            return

        run.status = RunStatus.DONE
        run.message = ""
        if report.port_count:
            # ⚠️ **只从验过的 run 学端口数。** 从失败的 run 学会把整列带沟里：
            # 一个 WRONG_PORT_COUNT 的 run 产出 3 端口且自洽，学了它，
            # 后面每个正常的 4 端口 run 都会被判"端口数不符" —— 而且报的是别人的错。
            self._port_counts.setdefault(run.design_key, report.port_count)
        code = job.exit_code if job is not None else None
        if code not in (None, 0):
            # 产物验过了但退出码非 0：按 §12 以产物为准，同时**必须说出来**。
            self._note(
                events,
                EventKind.WARNING,
                f"the outputs verify, but the job exit code is {code} - per sec. 12 the outputs win; "
                "noting it for the record",
                run=run,
            )
        self._note(
            events,
            EventKind.FINISHED,
            f"verified: {len(report.sparam_files)} parameter files / {report.total_bytes} bytes"
            + (f" / {report.port_count} ports" if report.port_count else ""),
            run=run,
        )
        self._archive(run, events)

    def _archive(self, run: Run, events: list[DriverEvent]) -> None:
        """D5 归档：参数文件收进 `sparam/` 扁平区，mesh 中间件删掉，失败时留日志。

        只对**验收通过**的 run 调用 —— `archive_run` 自己也会先验一遍再删
        （"先验后删"），失败的 run 调它只会拿到一份"一个文件都不删"的报告。
        """
        options = self._state.options
        paths = self._paths_for(run)
        try:
            report = layout.archive_run(
                paths,
                run,
                keep=options.archive_keep,
                keep_logs_on_failure=options.keep_logs_on_failure,
            )
        except Exception as exc:  # noqa: BLE001 - 归档失败不许把已经验过的产物判死
            self._note(
                events,
                EventKind.WARNING,
                f"archiving failed (the outputs are still in the run dir): {exc}",
                run=run,
            )
            return
        if report.errors:
            self._note(
                events,
                EventKind.WARNING,
                "archiving had problems: " + _MESSAGE_SEP.join(report.errors),
                run=run,
            )
        run.artifacts = self._flat_artifacts(paths)
        self._note(
            events,
            EventKind.ARCHIVED,
            f"archived: kept {len(report.kept)} / removed {len(report.removed)} / "
            f"freed {report.bytes_freed} bytes / {len(run.artifacts)} in the flat area",
            run=run,
        )

    def _flat_artifacts(self, paths: RunPaths) -> tuple[str, ...]:
        """扁平汇聚区里属于这个 run 的文件（相对 `batch_dir`，`/` 分隔）。

        `ArchiveReport` 没有 `copied` 字段（P1 的交接报告里点名了这件事），
        所以直接看目录：`sparam/<design>__<slug>__<corner>_<temp>` + 后缀。

        🚨 **不是裸 `startswith`。** 词根后面只认 `core.layout._flat_suffix` 会产生的那两种形状：
        `.s4p` 和 `_sample.s4p`。裸前缀会让温度 `25.0` 的 run 把 `25.05` 的产物也认领走
        （`typical_25_0` 是 `typical_25_05` 的前缀）—— 与 MVP 那个 `--sparam` 前缀吃掉
        `--sparamImpedance` 的真 bug 是同一类，而且症状同样好看：两个 run 都"有产物"。
        """
        prefix = _posix(paths.sparam_prefix)
        directory = posixpath.dirname(prefix)
        base = posixpath.basename(prefix)
        if not directory or not base or not os.path.isdir(directory):
            return ()
        out: list[str] = []
        for name in sorted(os.listdir(directory)):
            if not name.startswith(base):
                continue
            rest = name[len(base) :]
            if not (rest.startswith(".") or rest.startswith(_SAMPLE_SUFFIX_MARK)):
                continue
            full = posixpath.join(directory, name)
            if not os.path.isfile(full):
                continue
            out.append(self._relative(full))
        return tuple(out)

    def _relative(self, path: str) -> str:
        """相对 `batch_dir` 的路径（`Run.artifacts` 的口径）。算不出就原样返回。"""
        root = _posix(self._state.batch_dir).rstrip("/") + "/"
        target = _posix(path)
        return target[len(root) :] if target.startswith(root) else target

    # ---- 阶段 1 ----------------------------------------------------------

    def _streamout_step(self, events: list[DriverEvent]) -> bool:
        """跑**一个** design 的阶段 1（一拍最多一个，理由见模块 docstring）。"""
        for design_key, design in self._designs.items():
            task = self._task_for(design_key)
            if task is None or task.status is not RunStatus.READY:
                continue
            self._run_streamout(design, task, events)
            return True
        return False

    def _streamout_plan(self, design: Design) -> tuple[CommandPlan, str, RunPaths]:
        """阶段 1 的 (命令, 渲染出来的 setup 文本, 路径)。**不写盘** —— dry-run 也走这条路。"""
        design_key = matrix.design_key(design)
        ctx = self._contexts[design_key]
        runs = [run for run in self._state.runs if run.design_key == design_key]
        if not runs:
            raise SpecError(
                f"design {design_key!r} has not a single run - nobody would use the GDS stage 1 exports"
            )
        paths = self._paths_for(runs[0])
        fields = _strmout.gdsout_fields_for_design(design, ctx, gds_path=paths.design_gds)
        template = ctx.facts.gdsout_template or _strmout.DEFAULT_GDSOUT_TEMPLATE
        rendered = _strmout.render_gdsout_setup(template, fields)
        plan = _strmout.build_strmout_plan(design, ctx, setup_path=paths.design_gdsout)
        return plan, rendered, paths

    def _run_streamout(
        self, design: Design, task: StreamoutTask, events: list[DriverEvent]
    ) -> None:
        design_key = task.design_key
        if design.gds_path:
            task.status = RunStatus.DONE
            task.gds_path = design.gds_path
            task.message = "the spec gave gds_path directly, stage 1 is not needed"
            self._note(events, EventKind.INFO, task.message, design_key=design_key)
            return
        try:
            plan, rendered, paths = self._streamout_plan(design)
        except EwaveBatchError as exc:
            self._fail_streamout(task, events, f"stage 1 cannot build its command: {exc}")
            return

        try:
            layout.ensure_run_dirs(paths)
            _write_text(paths.design_gdsout, rendered)
            self._write_cds_lib(design, plan, events)
        except (OSError, EwaveBatchError) as exc:
            self._fail_streamout(task, events, f"stage 1 failed to prepare its target dirs: {exc}")
            return

        task.gdsout_setup_path = paths.design_gdsout
        task.gds_path = paths.design_gds
        task.argv = tuple(plan.argv)
        task.log_path = plan.log_path
        task.status = RunStatus.RUNNING
        task.started_at = _utcnow()
        self._note(
            events, EventKind.STARTED, "stage 1: " + " ".join(plan.argv), design_key=design_key
        )

        try:
            result = self._runner.run(
                plan.argv,
                cwd=plan.cwd or None,
                env=plan.env or None,
                timeout=self._state.options.timeout_seconds,
            )
        except ToolMissingError as exc:
            self._fail_streamout(task, events, f"strmout cannot be started: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - runner 是注入进来的别人的代码
            self._fail_streamout(
                task, events, f"strmout cannot be started: {exc.__class__.__name__}: {exc}"
            )
            return
        task.ended_at = _utcnow()
        self._save_stage_one_log(task, result)

        gds = _posix(paths.design_gds)
        size = os.path.getsize(gds) if os.path.isfile(gds) else -1
        if size <= 0:
            # 与阶段 2 同一条判据：**产物说了算**。
            why = "the GDS was never produced" if size < 0 else "the GDS is 0 bytes"
            self._fail_streamout(
                task,
                events,
                f"stage 1 failed: {why} ({gds}), strmout exit code = {result.returncode}"
                + (", and it timed out" if result.timed_out else ""),
            )
            return
        if result.returncode != 0:
            self._note(
                events,
                EventKind.WARNING,
                f"strmout exit code is {result.returncode}, but the GDS is there ({size} bytes)"
                " - continuing on the 'outputs decide' rule; noting it for the record",
                design_key=design_key,
            )
        task.status = RunStatus.DONE
        task.message = ""
        self._note(
            events,
            EventKind.FINISHED,
            f"stage 1 done: {gds} ({size} bytes), shared by the whole settings matrix (D1a)",
            design_key=design_key,
        )

    def _save_stage_one_log(self, task: StreamoutTask, result: RunResult) -> None:
        """把 strmout 的 stdout/stderr 留一份 —— 失败现场是最贵的东西（D5 同理）。"""
        if not task.log_path:
            return
        try:
            body = result.stdout or ""
            if result.stderr:
                body += "\n--- stderr ---\n" + result.stderr
            _write_text(task.log_path, body)
        except OSError:
            # 日志写不出来不该把阶段 1 判死 —— 产物才是判据。
            task.message = _clip((task.message + " ").strip() + "(the stage 1 log was not written)")

    def _write_cds_lib(self, design: Design, plan: CommandPlan, events: list[DriverEvent]) -> None:
        """在 `cdswork/` 里放一行 `INCLUDE <找到的>/cds.lib`（BRIEF §10 step1 实测可行）。

        这样 strmout 能解析 `-library`，而 Cadence 的散落写入（`CDS.log` 之类）
        全留在我们自己的目录里 —— **不必 cd 进设计师的 workarea**（硬约束 4）。
        找不到 `cds.lib` 就不写：宁可让 strmout 用调用方自己的环境，也不编一个路径出来。
        """
        cwd = plan.cwd
        if not cwd:
            return
        os.makedirs(_posix(cwd), exist_ok=True)
        design_key = matrix.design_key(design)
        ctx = self._contexts[design_key]
        start = design.official_run_dir or ctx.facts.official_run_dir
        root = _find_cds_lib_root(start) if start else ""
        target = posixpath.join(_posix(cwd), _CDS_LIB_NAME)
        if not root:
            if not os.path.exists(target):
                self._note(
                    events,
                    EventKind.WARNING,
                    f"no {_CDS_LIB_NAME} found within {_CDS_LIB_SEARCH_UP} levels above the official "
                    f"run dir (start: {start or '<empty>'}) - {_CDS_LIB_NAME} was not written, "
                    "so strmout may not see the target library",
                    design_key=design_key,
                )
            return
        _write_text(target, f"INCLUDE {posixpath.join(root, _CDS_LIB_NAME)}\n")

    def _fail_streamout(self, task: StreamoutTask, events: list[DriverEvent], why: str) -> None:
        """阶段 1 失败 ⇒ 该 design 整列 `skipped`（§12：不去提交必然失败的 job）。"""
        task.status = RunStatus.FAILED
        task.ended_at = task.ended_at or _utcnow()
        task.message = _clip(why)
        self._note(events, EventKind.FAILED, task.message, design_key=task.design_key)
        if not self._state.options.stop_design_on_streamout_failure:
            self._note(
                events,
                EventKind.WARNING,
                "stop_design_on_streamout_failure=False - stage 2 is submitted even though stage 1 "
                "died; those jobs will most likely fail for the very same reason",
                design_key=task.design_key,
            )
            return
        skipped = 0
        for run in self._state.runs:
            if run.design_key != task.design_key or run.status in _TERMINAL_RUN_STATUSES:
                continue
            run.status = RunStatus.SKIPPED
            run.message = _clip(f"stage 1 (strmout) failed, this column is skipped: {why}")
            self._note(events, EventKind.SKIPPED, run.message, run=run)
            skipped += 1
        self._note(
            events,
            EventKind.INFO,
            f"stage 1 failed => all {skipped} combinations under design {task.design_key} are "
            "skipped, not one job is submitted",
            design_key=task.design_key,
        )

    # ---- 阶段 2：提交 ----------------------------------------------------

    def _submit_step(self, events: list[DriverEvent]) -> bool:
        options = self._state.options
        in_flight = sum(1 for run in self._state.runs if run.status in _IN_FLIGHT_RUN_STATUSES)
        budget = options.max_parallel - in_flight if options.max_parallel > 0 else len(self._state.runs)
        if budget <= 0:
            return False
        changed = False
        for run in self._state.runs:
            if budget <= 0:
                break
            if run.status is not RunStatus.READY:
                continue
            task = self._task_for(run.design_key)
            if task is None:
                run.status = RunStatus.SKIPPED
                run.message = _clip(
                    f"design {run.design_key!r} is not in the designs list - no stage 1 record, "
                    "no site coordinates either"
                )
                self._note(events, EventKind.SKIPPED, run.message, run=run)
                changed = True
                continue
            if task.status is not RunStatus.DONE:
                if (
                    task.status is RunStatus.FAILED
                    and not options.stop_design_on_streamout_failure
                ):
                    # 用户明说了"阶段 1 挂了也照跑"（`_fail_streamout` 那边发过 WARNING）。
                    # 不放行的话这些 run 会永远停在 ready、批次转不出来 ——
                    # **挂着不动比失败更糟**：没人知道它在等什么。
                    pass
                else:
                    # 阶段 1 还没成：等着（失败且开着 fail-fast 时，这一列已经被标 skipped 了）。
                    continue
            if self._submit_one(run, events):
                budget -= 1
            changed = True
        return changed

    def _submit_one(self, run: Run, events: list[DriverEvent]) -> bool:
        """提交一个 run。返回 True = 真的占用了一个并发名额。"""
        try:
            plan = self._plan_for(run)
        except Exception as exc:  # noqa: BLE001 - 拼不出命令只该让这一个 run 失败
            run.status = RunStatus.FAILED
            run.message = _clip(f"building the command failed: {exc.__class__.__name__}: {exc}")
            self._note(events, EventKind.FAILED, run.message, run=run)
            return False

        paths = self._paths_for(run)
        try:
            layout.ensure_run_dirs(paths)
            layout.write_cmd_sh(paths, plan)
        except Exception as exc:  # noqa: BLE001
            run.status = RunStatus.FAILED
            run.message = _clip(f"creating the target dirs / writing cmd.sh failed: {exc}")
            self._note(events, EventKind.FAILED, run.message, run=run)
            return False

        removed = self._clean_stale_outputs(paths)
        if removed:
            self._note(
                events,
                EventKind.INFO,
                f"cleared {len(removed)} files left over from the previous attempt before rerunning - "
                "when the port count changes the new output does not overwrite the old one "
                "(.s3p and .s4p are two different file names), and the mixture makes the verifier "
                "report 'port counts disagree' forever",
                run=run,
            )

        design = self._designs.get(run.design_key)
        ctx = self._contexts[run.design_key]
        resources = (design.resources if design is not None else "") or ctx.facts.dsub_resources
        try:
            job = self._scheduler.submit(plan, resources=resources, name=run.run_id)
        except Exception as exc:  # noqa: BLE001 - 调度器是注入进来的别人的代码
            run.status = RunStatus.FAILED
            run.message = _clip(f"submit failed: {exc}")
            self._note(events, EventKind.FAILED, run.message, run=run)
            return False

        run.job = job
        run.attempts += 1
        run.argv = tuple(plan.argv)
        run.submitted_at = job.submitted_at or _utcnow()
        run.ended_at = ""
        run.message = ""
        run.status = _run_status_for_job_state(job.state) or RunStatus.PENDING
        self._unknown_polls.pop(run.run_id, None)
        self._note(
            events,
            EventKind.SUBMITTED,
            f"job {job.job_id or '<no id>'} (submit attempt {run.attempts}; no automatic retry, "
            "a failure stops at failed and waits for a human to resume)",
            run=run,
        )
        return True

    def _clean_stale_outputs(self, paths: RunPaths) -> tuple[str, ...]:
        """重跑之前把上一次的产物清掉，**只删 `<corner>_<temp>/` 里的文件**。

        为什么必须清（这条是写测试时被真实验收器当场抓到的，不是推测）：
        端口数变了的时候新产物**不会**覆盖旧的（`.s3p` 和 `.s4p` 是两个文件名），
        两代产物混在一个目录里，`verify_run_outputs` 会（正确地）判"端口数不一致" ——
        于是这个 run 无论重跑多少次都好不了。

        代价：上一次失败的日志也一起没了。取舍依据是**用户已经按下 resume**
        ＝ 他决定重跑了；而"重跑永远好不了"是无声的坑，比丢日志贵得多。
        """
        ewave_dir = _posix(paths.ewave_dir)
        if not ewave_dir or not os.path.isdir(ewave_dir):
            return ()
        removed: list[str] = []
        for name in sorted(os.listdir(ewave_dir)):
            full = posixpath.join(ewave_dir, name)
            if not os.path.isfile(full):
                continue
            try:
                os.remove(full)
            except OSError:
                continue
            removed.append(name)
        return tuple(removed)

    # ---- 共用小件 --------------------------------------------------------

    def _task_for(self, design_key: str) -> StreamoutTask | None:
        for task in self._state.streamout:
            if task.design_key == design_key:
                return task
        return None

    def _paths_for(self, run: Run) -> RunPaths:
        """这个 run 的全套路径，顺手把 `run.work_dir` 补上（`expand_runs` 不填它）。"""
        cached = self._paths.get(run.run_id)
        if cached is not None:
            run.work_dir = run.work_dir or cached.run_dir
            return cached
        design = self._designs.get(run.design_key)
        if design is None:
            raise SpecError(f"design {run.design_key!r} of run {run.run_id!r} is not in the designs list")
        paths = layout.compute_run_paths(self._state.batch_dir, design, run)
        run.work_dir = paths.run_dir
        self._paths[run.run_id] = paths
        return paths

    def _plan_for(self, run: Run) -> CommandPlan:
        cached = self._plans.get(run.run_id)
        if cached is not None:
            return cached
        self._paths_for(run)  # 先把 work_dir 补上：build_ewave_plan 拿它当 --workDir
        ctx = self._contexts[run.design_key]
        plan = _ewave_tool.build_ewave_plan(run, ctx)
        self._plans[run.run_id] = plan
        return plan

    def _verify(self, run: Run, events: list[DriverEvent] | None = None) -> VerifyReport:
        report = layout.verify_run_outputs(
            self._paths_for(run),
            run,
            expected_port_count=self._expected_port_count(run.design_key),
        )
        if events is not None:
            self._warn_port_guard_once(run.design_key, events)
        return report

    def _warn_port_guard_once(self, design_key: str, events: list[DriverEvent]) -> None:
        """端口数这一项没核对 ⇒ 说一次。**说一次**，不是每个 run 说一次。

        为什么必须出声：`--all` 的代价就是这道防线（pin 集合一变，端口编号全平移，
        而且静默）。跳过它是对的（拿别人的端口表判人失败更糟），但用户得知道
        这个批次的第一轮少了一层保护 —— 以及怎么把它拿回来。
        """
        if design_key in self._port_guard_warned or design_key not in self._port_guard_skipped:
            return
        self._port_guard_warned.add(design_key)
        self._note(
            events,
            EventKind.WARNING,
            "port count is not cross-checked for this design: the official run dir "
            "describes a different (library, cell, view), so its port table says nothing "
            "about this one. Next: point this design at its own official run dir, or set "
            "port_spec explicitly. Once one run of this design passes, later runs in the "
            "batch are checked against it.",
            design_key=design_key,
        )

    def _expected_port_count(self, design_key: str) -> int | None:
        """这个 design 的产物**应该**是几端口。拿不到就返回 None（不编一个）。

        三个来源，从最硬到最软：

        1. `Design.port_spec` 的显式 `-p` 映射条数（D1b 留的口子，用户自己写的）；
        2. 本批次里**已经验过**的同 design 产物 —— 批次内一致性；
        3. 官方那条命令的端口表（`SiteFacts.official_port_spec`）—— 官方跑的就是这个 design，
           端口数对不上就意味着 pin 集合变了。

        这正是 `--all` 的代价要的那道防线（BRIEF §5）：设计师加/删/改名一个 pin ⇒
        所有端口编号平移 ⇒ 归档的 `.sNp` 和现成的 nport 全部静默错位。
        `BatchOptions.verify_port_count=False` 能关掉它，但别关。
        """
        if not self._state.options.verify_port_count:
            return None
        design = self._designs.get(design_key)
        if design is not None and design.port_spec is not None and design.port_spec.mapping:
            return len(design.port_spec.mapping)
        learned = self._port_counts.get(design_key)
        if learned:
            return learned
        ctx = self._contexts.get(design_key)
        if ctx is not None and ctx.facts.official_port_spec.mapping:
            # ★ 守卫：这份 facts 得**真的是这个 design**，见 `_facts_describe_design`。
            #   拿别人的端口表当期望值 = 一批本来是好的 run 被判 failed，
            #   而报错里那个数字跟这个设计毫无关系。
            if design is not None and _facts_describe_design(design, ctx.facts):
                return len(ctx.facts.official_port_spec.mapping)
            self._port_guard_skipped.add(design_key)
        return None

    def _attach_log_facts(self, run: Run) -> LogFacts | None:
        """把这个 run 的日志事实读进 `Run.log_facts` 并返回。**只读磁盘，读不到就当没有。**

        ★ 存在的理由（2026-08-28，用户在师傅的机器上实测）：在这之前一个 failed 的 run
        只说得出"产物验不过"，而**为什么**验不过（配额爆了 / mesh 崩了 / 许可证没拿到）
        逐字躺在 eWave 自己的日志里，从来没人去读它。于是界面上的失败原因永远是
        「outputs do not verify」—— 逐字正确，但对"我该改什么"零信息量。

        三条口径：

        1. **文件由 `logparse.run_log_files` 挑，不扫 run 目录**。run 目录是 corner/temp
           之间共享的（`<axes-slug>` 按定义不含它们），扫它会把邻居的日志合并进来，
           然后报出一份张冠李戴的结论。
        2. **成功的 run 也读**。`runs.csv` 的 `converged` / `peak_memory_mb` /
           `port_count` 三列本来就是从 `Run.log_facts` 来的（`layout.write_runs_csv`），
           没人填它 ⇒ 那三列一直是空的。
        3. **任何异常都吞掉**。这是诊断信息，不是判据 —— `done` 的唯一判据仍然是
           `layout.verify_run_outputs`（BRIEF §10）。让读日志把一个已经跑完的批次
           搞崩，是拿次要的东西赌主要的东西。
        """
        try:
            paths = self._paths_for(run)
        except Exception:  # noqa: BLE001 - 拼不出路径就是没日志可读，不该炸穿
            return run.log_facts
        try:
            files = logparse.run_log_files(
                ewave_dir=_posix(paths.ewave_dir),
                run_log=_posix(paths.run_log),
                run_dir=_posix(paths.run_dir),
            )
            if files:
                run.log_facts = logparse.parse_log_files(files)
        except OSError:  # pragma: no cover - 文件系统抽风
            return run.log_facts
        return run.log_facts

    def _finished(self) -> bool:
        if self._state.options.dry_run:
            return self._dry_run_done
        runs_done = all(run.status in _TERMINAL_RUN_STATUSES for run in self._state.runs)
        tasks_done = all(task.status in _TERMINAL_RUN_STATUSES for task in self._state.streamout)
        return runs_done and tasks_done

    def _persist(self) -> None:
        """原子写 `batch.json` + `runs.csv`。**每推进一步都写** —— resume 只认前者。"""
        root = _posix(self._state.batch_dir)
        layout.write_batch_state(posixpath.join(root, BATCH_JSON_NAME), self._state)
        layout.write_runs_csv(posixpath.join(root, RUNS_CSV_NAME), self._state)

    # ---- 事件 ------------------------------------------------------------

    def _note(
        self,
        events: list[DriverEvent],
        kind: EventKind,
        message: str,
        *,
        run: Run | None = None,
        design_key: str = "",
    ) -> DriverEvent:
        event = DriverEvent(
            kind=kind,
            message=message,
            run_id=run.run_id if run is not None else "",
            design_key=(run.design_key if run is not None else "") or design_key,
            at=_utcnow(),
        )
        events.append(event)
        self._fire(event)
        return event

    def _emit(
        self, kind: EventKind, message: str, *, run: Run | None = None, design_key: str = ""
    ) -> None:
        """播一条事件但不进 `TickReport`（`cancel()` 用 —— 它没有 report 可放）。"""
        self._fire(
            DriverEvent(
                kind=kind,
                message=message,
                run_id=run.run_id if run is not None else "",
                design_key=(run.design_key if run is not None else "") or design_key,
                at=_utcnow(),
            )
        )

    def _fire(self, event: DriverEvent) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception:  # noqa: BLE001 - 回调是别人的代码，炸了不许带走批次
            pass

    # ---- resume ----------------------------------------------------------

    def _reconcile(self, events: list[DriverEvent]) -> None:
        """resume 的核对：**判据来自磁盘，不是上一次的内存状态**（D7）。

        为什么不能只信 `batch.json` 里的 `status`：上一次进程被杀的时候，状态很可能停在
        `running`，而那个 job 其实已经 `exit=0` 地崩了（BRIEF §10）。所以：

        | 存的状态 | 怎么办 |
        |---|---|
        | `done` | **再验一遍产物**。验过 ⇒ 一个都不重跑（一个 run 可能 10 核 100 GB 跑 35 分钟）；验不过 ⇒ 改判 failed 并重排（`batch.json` 说 done 而磁盘上没有，那是假的 done） |
        | `pending` / `running` | 先去调度器查一遍：还活着就继续等；终态就去验产物；查不到就当它没跑成，重排 |
        | `failed` / `skipped` / `ready` | 重排（这正是 resume 要补的） |

        阶段 1 的账同理：`failed` 的重跑，`done` 的（GDS 还在且非空）不动。
        """
        self._check_spec_hash(events)
        for task in self._state.streamout:
            self._reconcile_streamout(task, events)
        for run in self._state.runs:
            self._reconcile_run(run, events)
        counts = self.summary()
        self._note(
            events,
            EventKind.INFO,
            "resume reconciliation done: "
            + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()) if value),
        )
        if not self._state.options.dry_run:
            try:
                self._persist()
            except Exception as exc:  # noqa: BLE001
                self._note(events, EventKind.WARNING, f"persisting during resume failed: {exc}")

    def _check_spec_hash(self, events: list[DriverEvent]) -> None:
        """spec 改过了就发一条 WARNING，**但照跑**（`model.resume_batch` 的原话）。"""
        prov = self._state.provenance
        if not prov.spec_path or not prov.spec_sha256:
            return
        try:
            from ..core import spec as _spec  # 惰性：spec 会去 import PyYAML

            current = _spec.spec_sha256(prov.spec_path)
        except Exception:  # noqa: BLE001 - 读不到 spec 不该挡住 resume
            return
        if current != prov.spec_sha256:
            self._note(
                events,
                EventKind.WARNING,
                f"the spec changed ({prov.spec_path}): the batch recorded sha {prov.spec_sha256[:12]}..., "
                f"now it is {current[:12]}... - resume uses the settings frozen in batch.json, "
                "not the new spec. Start a new batch to use the new settings",
            )

    def _reconcile_streamout(self, task: StreamoutTask, events: list[DriverEvent]) -> None:
        if task.status is RunStatus.DONE:
            gds = _posix(task.gds_path)
            if gds and os.path.isfile(gds) and os.path.getsize(gds) > 0:
                return
            task.status = RunStatus.READY
            self._note(
                events,
                EventKind.INFO,
                f"stage 1 was recorded as done, but the GDS is missing or 0 bytes "
                f"({gds or '<no path recorded>'}) - rerunning stage 1",
                design_key=task.design_key,
            )
            return
        if task.status in (RunStatus.FAILED, RunStatus.SKIPPED, RunStatus.PENDING, RunStatus.RUNNING):
            task.status = RunStatus.READY
            task.message = ""
            task.ended_at = ""

    def _reconcile_run(self, run: Run, events: list[DriverEvent]) -> None:
        if run.status is RunStatus.DONE:
            verdict = self._verify(run)
            if verdict.ok:
                return
            run.status = RunStatus.FAILED
            run.message = _clip(
                "batch.json says done, but the outputs on disk do not verify: "
                + _MESSAGE_SEP.join(verdict.reasons)
                + ". This resume will rerun it"
            )
            self._note(events, EventKind.WARNING, run.message, run=run)
            self._requeue(run, events, "the done recorded last time does not verify on disk")
            return

        if run.status in _IN_FLIGHT_RUN_STATUSES and run.job is not None and run.job.job_id:
            self._reconcile_in_flight(run, events)
            return

        if run.status in (RunStatus.FAILED, RunStatus.SKIPPED, RunStatus.READY):
            if run.status is not RunStatus.READY:
                self._note(
                    events,
                    EventKind.INFO,
                    f"last time it was {run.status.value}: {run.message or '(no reason recorded)'} "
                    "- requeueing",
                    run=run,
                )
            self._requeue(run, events, "")
            return

        # pending/running 但没有 job id：上一次连提交都没成 ⇒ 当没跑过。
        self._requeue(run, events, "recorded as in flight but with no job id - treating it as never submitted")

    def _reconcile_in_flight(self, run: Run, events: list[DriverEvent]) -> None:
        job = run.job
        assert job is not None  # 调用方已经查过
        try:
            updated = self._scheduler.poll([job])
        except Exception as exc:  # noqa: BLE001
            self._note(
                events, EventKind.WARNING, f"resume could not query job {job.job_id}: {exc}", run=run
            )
            return
        fresh = updated.get(job.job_id)
        state = JobState.UNKNOWN if fresh is None else fresh.state
        if fresh is not None:
            run.job = fresh
        if state in (JobState.PENDING, JobState.RUNNING):
            mapped = _run_status_for_job_state(state)
            if mapped is not None:
                run.status = mapped
            self._note(
                events,
                EventKind.INFO,
                f"job {job.job_id} is still alive ({state.value}) - keep waiting, do not resubmit",
                run=run,
            )
            return
        if _job_is_terminal(state):
            self._note(
                events,
                EventKind.INFO,
                f"job {job.job_id} is already {state.value} - verifying the outputs "
                "(a terminal state is not success)",
                run=run,
            )
            self._finish_run(run, events)
            return
        # UNKNOWN：作业可能早就从队列里老化掉了。先看磁盘。
        verdict = self._verify(run)
        if verdict.ok:
            self._note(
                events,
                EventKind.INFO,
                f"the scheduler cannot find job {job.job_id}, but the outputs verify - "
                "calling it done, no rerun",
                run=run,
            )
            self._finish_run(run, events, verdict=verdict)
            return
        self._requeue(
            run, events, f"the scheduler cannot find job {job.job_id} and the outputs do not verify either"
        )

    def _requeue(self, run: Run, events: list[DriverEvent], why: str) -> None:
        """把一个 run 放回 `ready`。**不清 `attempts`** —— 那是"人按过几次 resume"的账。"""
        run.status = RunStatus.READY
        run.job = None
        run.ended_at = ""
        run.started_at = ""
        run.wall_seconds = None
        run.message = _clip(why) if why else ""
        self._unknown_polls.pop(run.run_id, None)


# --------------------------------------------------------------------------
# 真 runner
# --------------------------------------------------------------------------

_KILL_GRACE_SECONDS = 5.0
"""`terminate()` 之后等多久再 `kill()`。"""

_WAIT_SLICE_SECONDS = 0.05
"""等子进程时每片多长。只影响"取消/超时多久才生效"，不影响结果。"""


class SubprocessRunner:
    """真的起子进程 —— 满足 `model.RunnerProtocol`。形状抄 `Auto_ext/tools/base.py`
    的 `run_subprocess`（cancel token + 独立读线程 + 逐行 flush），那份在生产里跑了很久。

    ⚠️ **本机永远测不到它跑 `ewave`**（CLAUDE.md 硬约束 3：没有 ewave / dsub / strmout）。
    单测只能拿 `sys.executable` 之类的通用命令验它的协议行为 ——
    argv 怎么拼是 `core.cmd` 的活，那边有 golden 测试对着真实生产命令验。
    """

    def __init__(self, *, wait_slice: float = _WAIT_SLICE_SECONDS) -> None:
        self.wait_slice = float(wait_slice)

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
        """执行 argv，返回 `RunResult`。签名逐字照抄 `model.RunnerProtocol.run`。

        协议约定，逐条：
        * `env` 是**增量**，这里合并到 `os.environ` 的副本上；
        * `on_line` 每读到一行调用一次（读线程里现调，不攒到最后）；
        * `cancel()` 返回 True ⇒ 尽快终止并置 `cancelled=True`；
        * **超时不抛异常**，置 `timed_out=True` 返回，已经拿到的输出照样给；
        * 找不到可执行文件 ⇒ `ToolMissingError`。
        """
        items = [str(item) for item in argv]
        if not items:
            raise ToolMissingError("argv is empty - there is no executable to start")

        merged = dict(os.environ)
        if env:
            merged.update({str(k): str(v) for k, v in env.items()})

        if cancel is not None and cancel():
            # 还没起进程就被取消了：连 fork 都不做（起了再杀会在红区留半份产物）。
            return RunResult(
                argv=tuple(items),
                returncode=-1,
                duration_seconds=0.0,
                cancelled=True,
                cwd=cwd or "",
            )

        started = time.monotonic()
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv 形式，没有 shell
                items,
                cwd=cwd or None,
                env=merged,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                universal_newlines=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
            )
        except (FileNotFoundError, NotADirectoryError, PermissionError) as exc:
            raise ToolMissingError(
                f"{items[0]}: cannot be started ({exc.__class__.__name__}: {exc}) - "
                "absolute tool paths never go into the source; this one comes from SiteFacts / PATH "
                "(hard constraint 1b)"
            ) from exc

        out_lines: list[str] = []
        err_lines: list[str] = []
        lock = threading.Lock()

        def pump(stream: object, sink: list[str], notify: bool) -> None:
            try:
                for line in stream:  # type: ignore[attr-defined]
                    text = line.rstrip("\r\n")
                    sink.append(text)
                    if notify and on_line is not None:
                        with lock:
                            try:
                                on_line(text)
                            except Exception:  # noqa: BLE001 - 回调是别人的代码
                                pass
            except (OSError, ValueError):  # pragma: no cover - 进程被杀时管道会断
                pass
            finally:
                try:
                    stream.close()  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001 # pragma: no cover
                    pass

        threads = [
            threading.Thread(target=pump, args=(proc.stdout, out_lines, True), daemon=True),
            threading.Thread(target=pump, args=(proc.stderr, err_lines, on_line is not None), daemon=True),
        ]
        for thread in threads:
            thread.start()

        cancelled = False
        timed_out = False
        while True:
            if proc.poll() is not None:
                break
            if cancel is not None and cancel():
                cancelled = True
                self._stop(proc)
                break
            if timeout is not None and (time.monotonic() - started) >= timeout:
                timed_out = True
                self._stop(proc)
                break
            time.sleep(self.wait_slice)

        returncode = proc.wait()
        for thread in threads:
            thread.join(timeout=_KILL_GRACE_SECONDS)
        return RunResult(
            argv=tuple(items),
            returncode=returncode,
            stdout="\n".join(out_lines) + ("\n" if out_lines else ""),
            stderr="\n".join(err_lines) + ("\n" if err_lines else ""),
            duration_seconds=time.monotonic() - started,
            timed_out=timed_out,
            cancelled=cancelled,
            cwd=cwd or "",
        )

    def _stop(self, proc: "subprocess.Popen[str]") -> None:
        """先礼后兵：`terminate()` 等一小会儿，还不走就 `kill()`。"""
        try:
            proc.terminate()
        except OSError:  # pragma: no cover - 进程刚好自己退了
            return
        try:
            proc.wait(timeout=_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - 赖着不走的进程
            try:
                proc.kill()
            except OSError:
                pass


# --------------------------------------------------------------------------
# 公开入口
# --------------------------------------------------------------------------


def make_driver(
    state: BatchState,
    contexts: Mapping[str, PlanContext],
    scheduler: SchedulerProtocol,
    runner: RunnerProtocol,
    *,
    on_event: Callable[[DriverEvent], None] | None = None,
) -> DriverProtocol:
    """造一个 driver。`contexts` 的键是 `design_key`（坐标是 per-design 的）。

    调用方**只通过这个工厂**拿 driver，别直接碰 `Driver.__init__` —— 那样才好换实现。
    `contexts` 少了某个 design → `SpecError`。
    """
    return Driver(state, contexts, scheduler, runner, on_event=on_event)


def run_batch(
    driver: DriverProtocol,
    *,
    poll_interval: float = 15.0,
    max_seconds: float | None = None,
) -> int:
    """CLI 的 `while` 驱动：反复 `tick()` + sleep 到全部终态，返回进程退出码
    （0 = 全成，非 0 = 有 failed）。

    GUI **不**调它 —— GUI 用 `after()` 驱动同一个 `tick()`。这是"同一份 driver 代码"的落点。
    `max_seconds` 是给测试用的保险丝。

    另外两道保险丝：
    * `poll_interval <= 0` ⇒ 一次都不 sleep（测试就是这么跑 12-run 假批次的，毫秒级）；
    * **一个 job 都不在飞**且连着 `_STALL_TICK_LIMIT` 拍没有任何变化 ⇒ 认输返回非 0。
      只在"没有在飞的 job"时计数：真跑的时候 job 在队列里排几个小时，每拍都是"无变化"。
    """
    idle = 0
    started = time.monotonic()
    while True:
        report = driver.tick()
        if report.finished:
            break
        in_flight = report.counts.get(RunStatus.PENDING.value, 0) + report.counts.get(
            RunStatus.RUNNING.value, 0
        )
        idle = 0 if (report.changed or in_flight) else idle + 1
        if idle >= _STALL_TICK_LIMIT:
            break
        if max_seconds is not None and (time.monotonic() - started) >= max_seconds:
            break
        if poll_interval > 0:
            time.sleep(poll_interval)

    counts = driver.summary()
    total = sum(counts.values())
    done = counts.get(RunStatus.DONE.value, 0)
    return 0 if done == total else 1


def resume_batch(
    batch_dir: str,
    contexts: Mapping[str, PlanContext],
    scheduler: SchedulerProtocol,
    runner: RunnerProtocol,
    *,
    on_event: Callable[[DriverEvent], None] | None = None,
) -> DriverProtocol:
    """从 `batch.json` 恢复一个批次（D7 断点续跑）。

    * `done` 的不重跑；`failed` / `ready` 的重新排；`pending` / `running` 的先去调度器查一遍
      （job 还活着就继续等，死了才重排）。
    * **不自动重试**（§12）：失败停在 `failed`，是人按了 resume 才补。
    * spec 的 sha 与 `provenance` 对不上 → 事件里发一条 `WARNING`，但照跑。

    ⚠️ 判据来自**磁盘**：`done` 的 run 会被重新验一遍产物（`verify_run_outputs`）。
    `batch.json` 说 done 而磁盘上没有产物，那是一个假的 done —— 上一次进程被杀时
    状态很可能停在半路，而 job 其实已经 `exit=0` 地崩了（BRIEF §10）。
    重跑一个真 done 的 run 是真金白银（10 核 100 GB × 35 分钟），所以**验过的一个都不重跑**。
    """
    root = _posix(batch_dir).rstrip("/")
    state = layout.read_batch_state(posixpath.join(root, BATCH_JSON_NAME))
    events: list[DriverEvent] = []
    driver = Driver(state, contexts, scheduler, runner, on_event=on_event)
    if _posix(os.path.abspath(state.batch_dir)) != _posix(os.path.abspath(root)):
        # 批次被搬过（或者当初记的是别的机器上的绝对路径）。以**现在这个目录**为准 ——
        # 否则所有落点都会指到一个不存在的地方，而症状是"产物一个都验不过"。
        driver._note(
            events,
            EventKind.INFO,
            f"batch.json records batch_dir as {state.batch_dir}, but it actually lives at {root} - "
            "the latter wins",
        )
        state.batch_dir = root
    driver._reconcile(events)
    return driver
