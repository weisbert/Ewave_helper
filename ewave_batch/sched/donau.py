"""`ewave_batch.sched.donau` —— 真提交后端（`dsub` / `djob` / `dkill` / `dpeek`）。

**这是移植，不是重写。** 源在 `references/ewave_donau_kit/donau_alps/code/donau.py`
（红区资料，不进 git），那份已经在真集群上跑通过一次完整自驱（提交 → 排队 → 运行 →
产物与官方逐字节一致）。它已经踩对的部分**原样保住**：

| kit 里的东西 | 这里的落点 | 为什么保住它 |
|---|---|---|
| `SubprocessRunner` 可注入 | 构造参数 `runner`（`model.RunnerProtocol`） | 本机没有 `dsub`，测试注入假的（硬约束 3） |
| `build_dsub_cmd` | `build_dsub_argv` | `-A`/`-q`/`-R` 三元组的形状与顺序 |
| `parse_job_id` 认三种格式 | `parse_dsub_submit_output` | 只认一种格式是解析器最常见的死法 |
| `map_state` 的**退出码优先**规则 | `map_job_state` 第 2 步 | `Exit: 0` 是"干净结束"，不是 LSF 的状态词 `EXIT`；反过来读会把成功报成失败 |
| `poll_once` 非阻塞 | `DonauScheduler.poll` | driver 是单线程轮询，`tick()` 不许阻塞（BRIEF §12） |

接到我们冻结面上时**改掉**的地方（每条都有理由，别改回去）：

1. **状态映射改成 `model.JobState`**（多了 `CANCELLED` / `UNKNOWN` 两个态）。
   kit 只有 4 个态、把"认不出"返回 `None`；我们要一个**显式的 UNKNOWN**，
   因为静默归成 `running` 会让批次永远卡着，静默归成 `done` 会让人拿到没跑完的结果。
2. **`JobState.DONE` 不许直接变成 `RunStatus.DONE`**（`run_status_for_job_state` 对终态
   返回 `None`）。实测：eWave 崩了也 `exit=0`、还会留 0 字节产物、日志照样报 "done"
   （BRIEF §10）⇒ `done` 的唯一判据是 `core.layout.verify_run_outputs`。
3. **`-A` / `-q` / `-R` 源码里零默认值。** kit 的 `DonauCfg` 把站点三元组写成了字段默认值，
   照抄就是把站点身份钉进公开仓库（CLAUDE.md 硬约束 1b）。这里全部来自
   `SiteFacts`（`core.discover.parse_dsub_options` 从官方 `remote_run_ewave.sh` 现场解析）
   或用户在界面里改的那条命令。**空串 = 不加该选项**，宁可缺也不编。
   （顺带：kit 记的 `cpu=` 核数是旧快照，红区实测已经变了 —— 又一条"别照抄快照当默认值"。）
4. **不提交阻塞式作业。** 生产那条 `remote_run_ewave.sh` 用 `-I`（前台阻塞 + tee），
   那是人手工跑一个 run 的形态；我们是有界并发轮询，`-I` 会让 `tick()` 卡死在一次提交上。
   ⇒ 异步提交 + `-J`（JSON 回显）+ `djob` 轮询，并且 `parse_dsub_prefix` **拒绝**
   用户把 `-I`/`-Kc`/`-Kco` 粘进来（那是最容易犯、也最难看出原因的错）。
5. **`--parallel` 不在这里算。** `-R` 的 `cpu=` → `--parallel` 的同步已经在
   `core.cmd`（`parse_resource_string` + `build_flag_layers`）。这里只提供
   `resources_from_dsub_argv` / `cpu_from_resources` 两个取数口子，**委托**给它，
   不再实现第二份会漂移的解析。

🚨 **本文件零站点标识符**：账号 / 队列 / 资源串 / 主机名一个都没有默认值，
全部从入参来。想知道站点值长什么样，去看运行时的 `SiteFacts`，不要写进这里。

⚠️ 还没实测确认的两件事（写在这里免得后人以为它们是定论）：

* **Donau 的"作业名" flag 未知。** LSF 是 `-J <name>`，但 Donau 的 `-J` 是 `--json`
  —— 照抄 LSF 会把作业名当 JSON 开关传进去。⇒ `NAME_FLAG` 默认是空串（不发任何东西），
  `name` 只用于 `Job.name` 这层自家记账。等红区确认了真 flag，改这一个常量即可。
* **`djob` 能不能一次查多个 id 未知。** 默认一次查全部（协议要求"别一个 job 一条命令"），
  失败一次就永久退回逐个查询（`DonauScheduler._multi_id_ok`）—— 查不到状态的后果是
  批次永远卡着，比多起几个进程贵得多。
"""

from __future__ import annotations

import dataclasses
import json as _json
import re
import shlex
import time
from collections.abc import Callable, Mapping, Sequence

from ..model import (
    TIMESTAMP_FORMAT,
    CommandPlan,
    Job,
    JobState,
    RunnerProtocol,
    RunResult,
    RunStatus,
    SchedulerError,
    SiteFacts,
)

# --------------------------------------------------------------------------
# 常量 —— 全是 Donau 的**工具语义**（命令名与 flag 名），不是站点身份
# --------------------------------------------------------------------------

SCHEDULER_NAME = "donau"
"""落进 `Job.scheduler`。`BatchOptions.scheduler` 用的也是这个字面量。"""

DSUB = "dsub"
DJOB = "djob"
DKILL = "dkill"
DPEEK = "dpeek"
"""Donau 命令家族（kit `ALPS_DONAU_NOTES` §2a′，`ls` 过 `dsub` 的 bin 目录确认）：
提交 / 查询 / 取消 / tail。**不是** LSF 的 `bsub`/`bjobs`/`bkill`/`bpeek`。"""

ACCOUNT_FLAG = "-A"
QUEUE_FLAG = "-q"
RESOURCE_FLAG = "-R"
"""站点三元组的 flag 名（形状来自官方 `remote_run_ewave.sh`）。**只有 flag 名在源码里，
取值永远从外面来。**"""

EXEC_PATH_FLAG = "-EP"
"""节点上的工作目录（kit §2a″，`dsub --help` 确认）。用 `CommandPlan.cwd` 而不是
`work_dir`：cwd 是"在哪跑"，`--workDir` 是"往哪写"，阶段 1 的 strmout 两者本来就不同。"""

STDLOG_FLAG = "-o"
"""作业 stdout 落文件（追加）。**不用 `-oo`**（覆盖）—— 失败现场是最贵的东西，
resume 时把上一次的日志覆盖掉，人就没得看了（D5「失败时保留日志」同一条理由）。"""

JSON_FLAG = "-J"
"""`-J,--json`：让提交回显是 JSON，job id 可确定地解析出来（kit `TOOL_FACTS`：
`dsub --json` 回 `{"data":{"jobId":"…"}}`）。⚠️ **Donau 的 `-J` 不是 LSF 的作业名**。
某个站点的 dsub 不认它 → 把这个常量改成空串，解析器仍能认流式回显。"""

NAME_FLAG = ""
"""作业名的 flag —— **空串 = 不发**。见模块 docstring：Donau 用什么 flag 起作业名
还没实测，而 `-J` 已经被 `--json` 占了。宁可不给作业名，也不猜一个 flag 塞进去：
猜错的后果是每次提交都带一个 dsub 不认识的参数，而错误只在红区才看得见。"""

BLOCKING_FLAGS = (
    "-I",
    "--interactive",
    "-Kc",
    "--blockcontinue",
    "-Kco",
    "--blockcontinue_output",
)
"""阻塞式提交的 flag（kit §2a″）。生产脚本用 `-I`，用户很可能整行粘过来 ——
`parse_dsub_prefix` 会当场拒绝并说人话。理由见模块 docstring 第 4 条。"""

SHELL_METACHARS = ("|", "&", ">", "<", "$(", "`")
"""用户粘进来的那条命令里不该出现的东西。我们是 argv 直接 exec，没有 shell ——
`2>&1 |tee x.log &` 会被原样当成 dsub 的参数传下去，然后作业以一种非常难懂的方式失败。

⚠️ **`;` 故意不在这张表里。** 它确实是 shell 的命令分隔符，但它同时是 `-R` 资源串的
**合法内容**（`cpu=…;mem=…`）。把它拉黑就会误伤每一条正常的提交命令 ——
和 MVP 那次 `--sparam` 前缀误伤 `--sparamImpedance` 是同一类错：
过滤器多吃了一口，而症状是"看起来很干净"。`tests/test_sched_donau.py` 有一条
测试专门盯着"带 `;` 的 `-R` 不许被拒"。"""

RAW_KEEP_CHARS = 2000
"""`Job.raw` 最多留多少字符。它会被写进 `batch.json`（每 tick 原子重写一次），
把整份 djob 回显塞进去会让状态文件肿起来 —— 只在**解析不了**的时候留，且截断。"""

# --------------------------------------------------------------------------
# 状态映射 —— kit 的 `map_state`，接到 `model.JobState`
# --------------------------------------------------------------------------

_STATE_TOKENS: dict[str, JobState] = {
    # 排队
    "pending": JobState.PENDING,
    "pend": JobState.PENDING,
    "queued": JobState.PENDING,
    "waiting": JobState.PENDING,
    "wait": JobState.PENDING,
    "configuring": JobState.PENDING,
    "submitted": JobState.PENDING,
    "suspended": JobState.PENDING,
    "psusp": JobState.PENDING,
    "ssusp": JobState.PENDING,
    # 运行
    "running": JobState.RUNNING,
    "run": JobState.RUNNING,
    "started": JobState.RUNNING,
    "active": JobState.RUNNING,
    # 结束（进程角度的"结束"，**不等于** run 的 done）
    "done": JobState.DONE,
    "succeeded": JobState.DONE,
    "success": JobState.DONE,
    "completed": JobState.DONE,
    "complete": JobState.DONE,
    "finished": JobState.DONE,
    # 失败
    "failed": JobState.FAILED,
    "fail": JobState.FAILED,
    "exit": JobState.FAILED,
    "exited": JobState.FAILED,
    "error": JobState.FAILED,
    "timeout": JobState.FAILED,
    "aborted": JobState.FAILED,
    # 取消（kit 把这几个也归 failed —— 我们有独立的态，分开报更诚实）
    "killed": JobState.CANCELLED,
    "cancelled": JobState.CANCELLED,
    "canceled": JobState.CANCELLED,
}
"""Donau/LSF 的状态词 → `JobState`。**大小写不敏感**（下面统一 lower）。

同时认 Donau 的词（`PENDING`/`RUNNING`/`DONE`/`FAILED`，kit §9 实测流式回显里见过）
和 LSF 的同义词（`PEND`/`RUN`/`EXIT`），这样公司这个 fork 换个拼法不会静默落空。
**认不出的一律 `JobState.UNKNOWN`，绝不猜。**"""

_STATE_FIELD_RE = re.compile(r"\b(?:state|status|stat)\s*[:=]?\s*([a-z_]+)", re.IGNORECASE)
_EXIT_CODE_RE = re.compile(r"\bexit(?:ed)?\b\s*(?:code|status|:|=)?\s*(\d+)\b", re.IGNORECASE)

_TERMINAL_STATES = frozenset({JobState.DONE, JobState.FAILED, JobState.CANCELLED})


def map_job_state(raw: str) -> JobState:
    """一段 `djob` 回显（或单个状态词）→ `JobState`。认不出 → `JobState.UNKNOWN`。

    三步，**顺序有讲究**（kit 已经踩过的坑，别调换）：

    1. 显式的 `State: <word>` / `status=<word>` 字段优先；
    2. **退出码字段先于裸词扫描**：`Exit: 0` / `exited 0` / `exit code 0` 是"干净结束"，
       而 LSF 的裸状态词 `EXIT` 是失败。反过来读会把一次成功的 run 报成失败，
       而人看日志只会看到"任务明明跑完了工具却说失败"；
    3. 最后才扫裸词，取**文本里出现得最早**的那个（kit 是按 dict 顺序取第一个命中的词，
       多任务文本里那等于随机挑一个 —— 这里改成按位置，行为可预测）。

    **认不出不许猜。** 静默归成 `running` → 批次永远卡着；静默归成 `done` → 人拿到
    没跑完的结果还以为跑完了。返回 `UNKNOWN` 让上层看见（`run_status_for_job_state`
    对 `UNKNOWN` 返回 `None` = 保持原状，driver 下一拍再问一次）。
    """
    text = (raw or "").strip()
    if not text:
        return JobState.UNKNOWN

    field = _STATE_FIELD_RE.search(text)
    if field is not None:
        state = _STATE_TOKENS.get(field.group(1).lower())
        if state is not None:
            return state

    exited = _EXIT_CODE_RE.search(text)
    if exited is not None:
        return JobState.DONE if int(exited.group(1)) == 0 else JobState.FAILED

    best_pos: int | None = None
    best_state = JobState.UNKNOWN
    lowered = text.lower()
    for token, state in _STATE_TOKENS.items():
        found = re.search(rf"\b{re.escape(token)}\b", lowered)
        if found is not None and (best_pos is None or found.start() < best_pos):
            best_pos = found.start()
            best_state = state
    return best_state


def is_terminal(state: JobState) -> bool:
    """这个 job 状态是不是终态（调度器不会再改它了）。

    ⚠️ 终态 **≠ run 成功**。`DONE` 只代表进程结束了；run 的成败要看
    `core.layout.verify_run_outputs`（存在 + 非空 + 端口数对）。
    """
    return state in _TERMINAL_STATES


def run_status_for_job_state(state: JobState) -> RunStatus | None:
    """`JobState` → `RunStatus`，**只映射能确定的那两个**；其余返回 `None`。

    | JobState | 返回 | 为什么 |
    |---|---|---|
    | `PENDING` | `RunStatus.PENDING` | 已 dsub、在排队 —— Donau 自己的词 |
    | `RUNNING` | `RunStatus.RUNNING` | |
    | `DONE` / `FAILED` / `CANCELLED` | `None` | **终态不许由 job 状态决定 run 状态** |
    | `UNKNOWN` | `None` | 保持原状，下一拍再问 |

    `None` 的意思是"**这里没有答案，调用方自己定**"：

    * 终态 → 去跑 `core.layout.verify_run_outputs`，验过了才写 `RunStatus.DONE`。
      实测三条（BRIEF §10）：eWave 崩了也 `exit=0`、崩了还留 0 字节产物、日志照样报 "done"
      ⇒ 退出码 / 文件存在 / 日志措辞**三个都不可信**；
    * `UNKNOWN` → 什么都别改。调度器短暂查不到是常事，凭空判失败会让人白跑一遍。

    做成"返回 None"而不是"返回一个看起来对的态"，是为了让**误用变成显式的空值**，
    而不是一个悄悄写进 `batch.json` 的假 `done`。
    """
    if state is JobState.PENDING:
        return RunStatus.PENDING
    if state is JobState.RUNNING:
        return RunStatus.RUNNING
    return None


# --------------------------------------------------------------------------
# 拼命令
# --------------------------------------------------------------------------


def build_dsub_argv(
    plan: CommandPlan,
    *,
    account: str = "",
    queue: str = "",
    resources: str = "",
    name: str = "",
    log_path: str = "",
) -> list[str]:
    """拼 `dsub … <命令>` 的 argv。

    账号 / 队列 / 资源全部**来自运行时解析**（`SiteFacts.dsub_*`）或 spec，
    **源码里不许有默认值**（CLAUDE.md 硬约束 1b）。空串 = 不加该选项。

    形状（官方 `remote_run_ewave.sh`）：

    ```
    dsub -A <account> -q <queue> -R "<resources>" … <要跑的命令>
    ```

    ⚠️ `-R` 的值在 shell 脚本里带引号，在 **argv 里不带** —— 我们是直接 exec，
    没有 shell 来脱引号。把 `"cpu=…"` 连引号一起传下去，Donau 会拿到一个多两个字符的
    资源串（要么报错，要么更糟：静默按默认值给资源）。

    机制层的三个（用户改不了，每个 run 不同）：

    * `-EP <plan.cwd>` —— 节点上的工作目录；
    * `-o <log_path>` —— 作业 stdout 落文件（追加，不覆盖）；
    * `-J` —— JSON 回显，好确定地解析 job id。

    `name` **默认不进 argv**：Donau 起作业名用哪个 flag 还没实测，而 `-J` 在这里是
    `--json` 不是 LSF 的作业名（见模块 docstring）。等确认了改 `NAME_FLAG` 一个常量。
    """
    if not plan.argv:
        raise SchedulerError(
            "CommandPlan.argv is empty - there is no command to submit.\n"
            "  Next: build the plan with tools.ewave.build_ewave_plan / tools.strmout.build_strmout_plan"
        )
    argv: list[str] = [DSUB]
    if account:
        argv += [ACCOUNT_FLAG, account]
    if queue:
        argv += [QUEUE_FLAG, queue]
    if resources:
        argv += [RESOURCE_FLAG, resources]
    if plan.cwd:
        argv += [EXEC_PATH_FLAG, plan.cwd]
    if log_path:
        argv += [STDLOG_FLAG, log_path]
    if name and NAME_FLAG:
        argv += [NAME_FLAG, name]
    if JSON_FLAG:
        argv.append(JSON_FLAG)
    argv.extend(plan.argv)
    return argv


def build_djob_argv(job_ids: Sequence[str]) -> list[str]:
    """`djob <id> [<id> …]` —— 一次查一批（协议要求：别一个 job 一条命令）。"""
    ids = [str(j) for j in job_ids if str(j).strip()]
    if not ids:
        raise SchedulerError("build_djob_argv: not a single job id - query what, exactly?")
    return [DJOB, *ids]


def build_dkill_argv(job_id: str) -> list[str]:
    """`dkill <id>` —— 取消一个 job。"""
    if not str(job_id).strip():
        raise SchedulerError("build_dkill_argv: job id is empty - refusing to send a cancel with no target")
    return [DKILL, str(job_id)]


def build_dpeek_argv(job_id: str) -> list[str]:
    """`dpeek <id>` —— tail 一个 job 的 stdout（失败时捞现场用）。"""
    if not str(job_id).strip():
        raise SchedulerError("build_dpeek_argv: job id is empty")
    return [DPEEK, str(job_id)]


def format_dsub_command(argv: Sequence[str]) -> str:
    """argv → 一行能贴回 shell 的命令文本（界面上给用户看/改的就是这一行）。

    用 `shlex.join`：`-R cpu=…;mem=…` 里的 `;` 在 shell 里是命令分隔符，
    不加引号贴回去会把后半截当成另一条命令。
    """
    return shlex.join(str(token) for token in argv)


def parse_dsub_prefix(text: str) -> list[str]:
    """用户手改过的那条 dsub 命令（文本）→ argv 前缀。

    用户 2026-08-18 要求：**整条 dsub 命令原样暴露出来给人改**（不是只让改 `-R`）。
    改完之后 `-R` 里的 `cpu=` 仍然要能被读回去同步 `--parallel` ——
    那一步走 `resources_from_dsub_argv` + `cpu_from_resources`。

    这里拒绝三类输入，**每一类都是我们知道会发生、且失败现场很难懂的**：

    1. 不是 `dsub` 开头 —— 多半是把整个 remote 提交脚本粘进来了；
    2. 带 shell 元字符（`|tee …`、`2>&1`、`&`）—— 我们直接 exec，没有 shell 来解释它们，
       它们会原样变成 dsub 的参数；
    3. 带阻塞 flag（`-I` / `-Kc` / `-Kco`）—— 生产脚本正是 `-I`，粘过来最自然。
       但 driver 是单线程轮询，一个阻塞的提交会让 `tick()` 再也回不来，
       界面从此不动，而"卡住"这个现象**看不出是这里造成的**。
    """
    tokens = shlex.split(text or "")
    if not tokens:
        raise SchedulerError("the dsub command is empty")
    head = tokens[0]
    if head != DSUB and not head.endswith("/" + DSUB):
        raise SchedulerError(
            f"this command does not start with {DSUB} (the first word is {head!r}). "
            f"What is wanted here is the submit prefix itself, shaped like `{DSUB} {ACCOUNT_FLAG} <account> "
            f"{QUEUE_FLAG} <queue> {RESOURCE_FLAG} cpu=...;mem=...`, "
            "not the whole remote submit script"
        )
    for token in tokens:
        for meta in SHELL_METACHARS:
            if meta in token:
                raise SchedulerError(
                    f"the command carries a shell metacharacter {meta!r} (inside {token!r}). "
                    "This tool execs argv directly, without a shell - pipes / redirections / background "
                    "symbols would be passed to dsub verbatim as arguments.\n"
                    "  Next: drop them; logging and backgrounding are handled by the tool itself"
                )
        if token in BLOCKING_FLAGS:
            raise SchedulerError(
                f"the command carries a blocking submit flag {token!r}. The official remote script is a "
                "human running one run by hand, where blocking on the output makes sense; this tool is "
                "bounded concurrency plus polling, and a blocking submit makes the scheduling loop hang "
                "on this one submit forever (the UI freezes, with no visible cause).\n"
                f"  Next: drop it - submission is asynchronous and the state comes from {DJOB} polling"
            )
    return tokens


def option_value_from_dsub_argv(argv: Sequence[str], *names: str) -> str:
    """从一条 dsub argv 里取回某个选项的值（没有 → 空串）。

    认 `-X val`、`--long val`、`--long=val` 三种写法。用户把整条命令改了之后，
    "这次到底用的哪个账号/队列/资源"只有这条命令知道 —— 记进 `Job` 的必须是它，
    不是构造调度器时传进来的那份（那份可能已经被人改掉了）。
    """
    tokens = [str(t) for t in argv]
    for index, token in enumerate(tokens):
        if token in names:
            return tokens[index + 1] if index + 1 < len(tokens) else ""
        for name in names:
            if token.startswith(name + "="):
                return token[len(name) + 1 :]
    return ""


def resources_from_dsub_argv(argv: Sequence[str]) -> str:
    """从一条 dsub argv 里取回 `-R` 的值（没有 → 空串）。

    用户把整条命令改了之后，`--parallel` 要跟着 `cpu=` 走 —— 取数的口子就是这里。
    """
    return option_value_from_dsub_argv(argv, RESOURCE_FLAG, "--resource", "--resources")


def cpu_from_resources(resources: str) -> int | None:
    """`-R` 的值 → `cpu=` 的核数（认不出返回 None）。

    **委托给 `core.cmd.parse_resource_string`，这里不再解析一遍**（BRIEF §12：
    `--parallel` 与 `cpu=` 的同步只许有一份实现，两份必然漂）。本函数只多做一件事：
    把字符串变成 int，认不出就 None —— **不许拿 1 或 0 冒充**，那会让
    "没解析到" 和 "真的只要一个核" 看起来一样。
    """
    from ..core.cmd import parse_resource_string  # 惰性：sched 与 core 双向可见，import 时不结环

    value = parse_resource_string(resources or "").get("cpu", "")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# 解析回显
# --------------------------------------------------------------------------

_JOBID_RE = re.compile(r"job[\s_\-]*id[\"'\s]*[:=]?\s*[\"']?(\d+)", re.IGNORECASE)
"""`JOBID 10000001`（流式回显）/ `"jobId":"10000002"`（Donau JSON）/ `jobId: 372`（无引号）。
分隔符字符类要同时吃掉右引号、冒号或等号、左引号，三种写法才能一条正则通吃。"""

_JOB_ANGLE_RE = re.compile(r"\bjob\s*<(\d+)>", re.IGNORECASE)
"""`Job <10000001> is submitted to queue <…>.` —— LSF `bsub` 的经典回显。
Donau 是 bsub 味儿的公司 fork（kit §2），它的 dsub 很可能保留这个句式，
而这句里**没有 "id" 这三个字母** ⇒ 不单独认它，`_JOBID_RE` 会漏掉整条回显。"""

_BARE_ID_RE = re.compile(r"[\"']id[\"']\s*[:=]\s*[\"']?(\d+)", re.IGNORECASE)
"""`-J` 的 JSON 里那种 `"id": 10000001`。**单独一条、且排在 `_JOBID_RE` 之后**：
`"requestId"` 这类键不该被当成 job id（kit `TOOL_FACTS` 点名过 "ignore requestId"），
而正则的最左匹配优先会让一条合并的大正则先咬到文本里靠前的那个 id。"""

_JOB_ID_PATTERNS = (_JOBID_RE, _JOB_ANGLE_RE, _BARE_ID_RE)
"""认 job id 的全部句式，**按可靠性排序**（前面的先试）。"""

_ID_HEADERS = ("JOBID", "JOB_ID", "JOB-ID", "ID", "JOB")
_STATE_HEADERS = ("STAT", "STATE", "STATUS")


def parse_dsub_submit_output(text: str) -> str:
    """从 dsub 的提交回显里抠 job id。抠不出来 → `SchedulerError`（**别返回空串继续**，
    没有 job id 的 run 后面永远轮询不到，会挂成僵尸）。

    认四种形状（解析器最常见的死法是"只认到自己造的那一种"）：

    1. Donau 的 JSON 信封 `{"code":"success","data":{"jobId":"10000002"}}`
       （kit `TOOL_FACTS` 实测；**`requestId` 不是 job id**，别拿它去轮询）；
    2. `-J` 的 JSON `{"id": 10000001}`；
    3. 带表头的多行 `JOBID  STATE` / `10000001  PENDING`；
    4. 流式/嘈杂回显 `Submit job successfully` + `... JOBID 10000001 ...`（kit §9 实测）。

    空输出、认不出的格式 → 抛 `SchedulerError`，错误信息里带上原文（截断），
    好让人一眼看出 dsub 到底说了什么。
    """
    raw = text or ""
    if not raw.strip():
        raise SchedulerError(
            "dsub printed nothing, so no job id can be extracted. "
            "The exit code may well be 0 (looking like a successful submit), but without an id there is "
            "nothing to poll and this run would hang as a zombie, so we refuse to continue.\n"
            f"  Next: make sure the submit command carries {JSON_FLAG} (JSON echo), "
            "or check whether something swallowed the dsub output"
        )

    from_json = _job_id_from_json(raw)
    if from_json:
        return from_json

    for job_id, _state in _parse_table(raw):
        return job_id

    for pattern in _JOB_ID_PATTERNS:
        match = pattern.search(raw)
        if match is not None:
            return match.group(1)

    raise SchedulerError(
        "cannot recognize the dsub submit echo, so no job id can be extracted (and no fake id is "
        "returned - polling a fake id would chase a job that does not exist until timeout and waste "
        "the whole batch).\n"
        f"  Raw output: {_trim(raw)}"
    )


def parse_djob_output(text: str) -> dict[str, JobState]:
    """查询回显 → `job_id` → `JobState`。认不出的状态字串映射成 `JobState.UNKNOWN`，不抛异常。

    `djob` 的确切输出格式没实测到（kit 只对单个 job 整段扫），所以这里**并联四种读法**，
    按可靠性排序合并（先到的赢，但 `UNKNOWN` 会被后面认出来的真状态顶掉）：

    1. JSON（整段可解析成 JSON 时）；
    2. 带表头的表格（`JOBID … STAT …`）—— 按列取，列位置由表头决定，不猜；
    3. 宽松逐行：行首是数字 + 行内有已知状态词；
    4. `key: value` 详情块 —— **只在块里只有一个 job id 时才认**，
       否则一段多任务的文本会被整体当成一个 job 的详情，把第一个 id 配上别人的状态。

    空输出 → 空 dict（**不是错**：调度器短暂没回话是常事，上层保持原状即可）。
    """
    raw = text or ""
    if not raw.strip():
        return {}
    merged: dict[str, JobState] = {}
    for pairs in (
        _parse_json_states(raw),
        _parse_table(raw),
        _parse_loose(raw),
        _parse_blocks(raw),
    ):
        for job_id, state in pairs:
            current = merged.get(job_id)
            if current is None or (current is JobState.UNKNOWN and state is not JobState.UNKNOWN):
                merged[job_id] = state
    return merged


def _job_id_from_json(text: str) -> str:
    """JSON 信封 → job id（认不出返回空串）。键名精确匹配，`requestId` 不在列。"""
    try:
        obj = _json.loads(text)
    except (ValueError, TypeError):
        return ""
    if not isinstance(obj, dict):
        return ""
    data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
    for source in (data, obj):
        for key in ("jobId", "jobid", "JOBID", "job_id", "id"):
            value = source.get(key)
            if value is not None and re.fullmatch(r"\d+", str(value).strip()):
                return str(value).strip()
    return ""


def _parse_json_states(text: str) -> list[tuple[str, JobState]]:
    """JSON 形态的 djob 回显 → [(job_id, state)]。认不出返回空 list。"""
    try:
        obj = _json.loads(text)
    except (ValueError, TypeError):
        return []
    found: list[tuple[str, JobState]] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            job_id = ""
            for key in ("jobId", "jobid", "JOBID", "job_id", "id"):
                value = node.get(key)
                if value is not None and re.fullmatch(r"\d+", str(value).strip()):
                    job_id = str(value).strip()
                    break
            if job_id:
                state = JobState.UNKNOWN
                for key in ("state", "status", "stat", "jobState", "jobStatus"):
                    value = node.get(key)
                    if value is not None:
                        state = map_job_state(str(value))
                        break
                found.append((job_id, state))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(obj)
    return found


def _parse_table(text: str) -> list[tuple[str, JobState]]:
    """带表头的表格 → [(job_id, state)]。没有表头就返回空 list（**不猜列**）。

    表头的判据故意收紧：整行没有一个纯数字的词。`JOBID 10000001` 这种"看起来像表头
    其实是数据"的一行因此不会被当成表头 —— 它归下面的宽松逐行去认。
    """
    lines = [line for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        tokens = line.split()
        if len(tokens) < 2 or any(token.isdigit() for token in tokens):
            continue
        upper = [token.upper().strip(":") for token in tokens]
        id_col = next((i for i, token in enumerate(upper) if token in _ID_HEADERS), None)
        state_col = next((i for i, token in enumerate(upper) if token in _STATE_HEADERS), None)
        if id_col is None or state_col is None:
            continue
        rows: list[tuple[str, JobState]] = []
        for data_line in lines[index + 1 :]:
            cells = data_line.split()
            if len(cells) <= max(id_col, state_col):
                continue
            if not cells[id_col].isdigit():
                continue
            rows.append((cells[id_col], map_job_state(cells[state_col])))
        if rows:
            return rows
    return []


def _parse_loose(text: str) -> list[tuple[str, JobState]]:
    """宽松逐行：行首是数字、且这一行后面有已知状态词 → 认。

    没有状态词的行**整行丢掉**（而不是记成 UNKNOWN）：`2 jobs found` 这种统计行
    也是"行首数字"，记下来只会往结果里塞垃圾 id。真正查不到的 job 由
    `DonauScheduler.poll` 统一置 UNKNOWN，效果一样而且只有一处负责。
    """
    rows: list[tuple[str, JobState]] = []
    for line in text.splitlines():
        tokens = line.split()
        if len(tokens) < 2 or not tokens[0].isdigit():
            continue
        for token in tokens[1:]:
            state = map_job_state(token.strip(":,"))
            if state is not JobState.UNKNOWN:
                rows.append((tokens[0], state))
                break
    return rows


def _parse_blocks(text: str) -> list[tuple[str, JobState]]:
    """`key: value` 详情块 → [(job_id, state)]。**块里有多个不同 job id 就跳过。**

    跳过的理由：一段没有空行的多任务输出会被当成一个块，于是第一个 id 配上
    文本里最早出现的状态词 —— 那个状态很可能是别人的。宁可这条读法什么都不给
    （前面三条已经覆盖了行式输出），也不给一个看起来对的错答案。
    """
    rows: list[tuple[str, JobState]] = []
    for block in re.split(r"\n\s*\n", text):
        if not block.strip():
            continue
        ids: set[str] = set()
        for pattern in _JOB_ID_PATTERNS:
            ids.update(m.group(1) for m in pattern.finditer(block))
        if len(ids) != 1:
            continue
        rows.append((ids.pop(), map_job_state(block)))
    return rows


def _trim(text: str, limit: int = RAW_KEEP_CHARS) -> str:
    """截断原始回显（错误信息和 `Job.raw` 都用它）。"""
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    return body[:limit] + f"... (truncated, {len(body)} chars total)"


def _utc_now() -> str:
    """落盘时间戳：UTC、秒精度、ISO-8601（`model.TIMESTAMP_FORMAT`）。"""
    return time.strftime(TIMESTAMP_FORMAT, time.gmtime())


# --------------------------------------------------------------------------
# 调度器
# --------------------------------------------------------------------------


class DonauScheduler:
    """`model.SchedulerProtocol` 的真实现：提交走 `dsub`，轮询走 `djob`，取消走 `dkill`。

    构造参数**没有被冻结**（`docs/INTERFACES.md`「还没冻结的东西」），但下面这几条是
    有意为之的：

    * `runner` 可注入（`model.RunnerProtocol`）—— 本机没有 `dsub`，全部测试注入假的。
      不给就在**第一次真要执行**的时候惰性拿 `sched.driver.SubprocessRunner`
      （惰性是为了让本模块不依赖同阶段并行写的 `driver.py`，import 得动）；
    * `account` / `queue` / `resources` **没有默认值**，从 `SiteFacts` 来
      （`from_site_facts`）或从用户改过的那条命令来（`submit_prefix`）；
    * `submit_prefix` 是"整条 dsub 命令原样暴露给用户改"的落点。给了它就**逐字**用，
      我们只补机制层那三个（`-EP` / `-o` / `-J`）**且只在用户没写过它们的时候补** ——
      同一个 flag 传两次会让 dsub 的行为不确定，那正是计数断言要防的事。
    """

    def __init__(
        self,
        runner: RunnerProtocol | None = None,
        *,
        account: str = "",
        queue: str = "",
        resources: str = "",
        submit_prefix: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.runner = runner
        self.account = account
        self.queue = queue
        self.resources = resources
        self.submit_prefix: tuple[str, ...] = tuple(str(t) for t in submit_prefix)
        self.env = dict(env) if env else {}
        self.timeout = timeout
        self._clock = clock or _utc_now
        self._multi_id_ok = True
        """`djob` 一次查多个 id 行不行。失败一次就永久退回逐个查 —— 见模块 docstring。"""

    # ---------------------------------------------------------------- 构造
    @classmethod
    def from_site_facts(
        cls,
        facts: SiteFacts,
        runner: RunnerProtocol | None = None,
        **kwargs: object,
    ) -> DonauScheduler:
        """从**运行时解析出来的**站点坐标造一个调度器（硬约束 1b 的正路）。

        `SiteFacts.dsub_*` 由 `core.discover.parse_dsub_options` 从官方
        remote 提交脚本里解析 —— 坐标不手抄、现场解析，既没有标识符进仓库，
        也没有手抄错的可能。
        """
        return cls(
            runner,
            account=facts.dsub_account,
            queue=facts.dsub_queue,
            resources=facts.dsub_resources,
            **kwargs,  # type: ignore[arg-type]
        )

    # ---------------------------------------------------------------- 展示 / 取数
    def dsub_prefix(self) -> list[str]:
        """当前的 dsub 提交前缀（站点那一段）。界面把它渲染成一行给用户改。

        不含机制层的 `-EP` / `-o`：那两个每个 run 都不同，是工具算的（四层里的"机制层"），
        用户改不了也不该改。
        """
        if self.submit_prefix:
            return list(self.submit_prefix)
        argv: list[str] = [DSUB]
        if self.account:
            argv += [ACCOUNT_FLAG, self.account]
        if self.queue:
            argv += [QUEUE_FLAG, self.queue]
        if self.resources:
            argv += [RESOURCE_FLAG, self.resources]
        return argv

    def effective_account(self) -> str:
        """这次真正会用的账号（用户改过命令就以命令里的为准）。"""
        if self.submit_prefix:
            return option_value_from_dsub_argv(self.submit_prefix, ACCOUNT_FLAG, "--account")
        return self.account

    def effective_queue(self) -> str:
        """这次真正会用的队列。"""
        if self.submit_prefix:
            return option_value_from_dsub_argv(self.submit_prefix, QUEUE_FLAG, "--queue")
        return self.queue

    def effective_resources(self) -> str:
        """真正会用的 `-R` 值 —— 用户改过命令就以命令里的为准。

        `--parallel` 要跟着它的 `cpu=` 走（默认 1:1，`BatchOptions.parallel_multiplier`），
        换算在 `core.cmd`，这里只负责"以谁为准"。

        ⚠️ 用户的命令里**没有** `-R` 时返回空串，**不回退**到构造时那份。
        回退看着更"周到"，实际是在撒谎：dsub 会用它自己的默认资源，而我们会把一个
        没送出去的资源串记进 `Job.resources`、还拿它的 `cpu=` 去定 `--parallel` 的档。
        空串是诚实的答案 —— 上层看见空串就知道"这次没指定"。
        """
        if self.submit_prefix:
            return resources_from_dsub_argv(self.submit_prefix)
        return self.resources

    def command_line(self, plan: CommandPlan, *, name: str = "") -> str:
        """这个 plan 会被提交成什么样（一行文本，界面展示 / 写进 `cmd.sh` 的注释）。"""
        return format_dsub_command(self.submit_argv(plan, name=name))

    def submit_argv(self, plan: CommandPlan, *, resources: str = "", name: str = "") -> list[str]:
        """算出这次提交的完整 argv。`submit` 用它，dry-run 也用它（只打印不执行）。"""
        effective = resources or self.effective_resources()
        if self.submit_prefix:
            argv = list(self.submit_prefix)
            _append_missing_mechanism(argv, plan)
            argv.extend(plan.argv)
            return argv
        if not (self.account or self.queue or effective):
            raise SchedulerError(
                "dsub has none of -A / -q / -R - the site coordinates were never resolved.\n"
                "  This tool stores no account or queue in its source (hard constraint 1b); they can only\n"
                "  come from one of two places:\n"
                "    1) the SiteFacts parsed by core.discover.discover_site_facts(<official run dir>);\n"
                "    2) the dsub command the user edited in the UI (submit_prefix).\n"
                "  With both empty we do not submit - a job without an account is either rejected or\n"
                "  lands in some default queue nobody knows about."
            )
        return build_dsub_argv(
            plan,
            account=self.account,
            queue=self.queue,
            resources=effective,
            name=name,
            log_path=plan.log_path,
        )

    # ---------------------------------------------------------------- Protocol
    def submit(self, plan: CommandPlan, *, resources: str = "", name: str = "") -> Job:
        """提交一条命令，返回带 `job_id` 的 `Job`（state 是 `PENDING` = 已排队）。

        三道闸，**每一道都对应一种"看起来成功了其实没有"**：

        1. 超时 → `SchedulerError`（不是"提交成功但很慢"）；
        2. 退出码非 0 → `SchedulerError`，把 stderr 原样带出来；
        3. **退出码 0 但抠不出 job id → 照样 `SchedulerError`**。这条最重要：
           `exit=0` 在这个项目里已经被证明不可信（BRIEF §10 实测 eWave 崩了也返 0），
           而一个没有 job id 的 run 会永远轮询不到、挂成僵尸。宁可当场失败。
        """
        argv = self.submit_argv(plan, resources=resources, name=name)
        result = self._execute(argv, cwd=plan.cwd or None)
        if result.timed_out:
            raise SchedulerError(
                f"dsub submit timed out ({self.timeout} s). The job MAY already have been submitted.\n"
                f"  Next: check with `{DJOB}` before resuming, do not submit twice.\n"
                f"  Command: {format_dsub_command(argv)}"
            )
        if result.returncode != 0:
            raise SchedulerError(
                f"dsub submit failed (rc={result.returncode}).\n"
                f"  Command: {format_dsub_command(argv)}\n"
                f"  Output: {_trim(result.stderr or result.stdout)}"
            )
        job_id = parse_dsub_submit_output(_combined(result))
        return Job(
            job_id=job_id,
            scheduler=SCHEDULER_NAME,
            state=JobState.PENDING,
            name=name,
            submitted_at=self._clock(),
            # ★ 记的是**这一次真的送出去的**三元组，不是构造时那份 ——
            # 用户改过命令之后两者会不一样，而 batch.json 是事后追溯的唯一依据。
            account=self.effective_account(),
            queue=self.effective_queue(),
            resources=resources or self.effective_resources(),
            stdout_path=plan.log_path,
        )

    def poll(self, jobs: Sequence[Job]) -> dict[str, Job]:
        """一次 `djob` 查一批，返回 `job_id` → **更新后的 Job**（不改传进来的对象）。

        * 查不到的 job → `JobState.UNKNOWN` + 保留原 Job 的全部字段，**不判失败**。
          调度器短暂查不到是常事；而且 LSF 系的查询在作业结束一段时间后本来就查不到了 ——
          那时候真正的判据是产物（`core.layout.verify_run_outputs`），不是这里。
          ⇒ 上层看到 `UNKNOWN` 的正确反应是"保持原状 + 该验产物就去验"，不是"标失败"。
        * 没有 `job_id` 的 job 不会出现在返回里（没法查，也没法当键）。
        """
        wanted = [job for job in jobs if job.job_id]
        if not wanted:
            return {}
        states, raw = self._query([job.job_id for job in wanted])
        updated: dict[str, Job] = {}
        for job in wanted:
            state = states.get(job.job_id, JobState.UNKNOWN)
            if state is JobState.UNKNOWN:
                updated[job.job_id] = dataclasses.replace(
                    job, state=JobState.UNKNOWN, raw=_trim(raw)
                )
            else:
                updated[job.job_id] = self._advance(job, state)
        return updated

    def cancel(self, job: Job) -> bool:
        """取消一个 job。已经结束的返回 False 而不是抛异常。

        失败也返回 False（签名只能返回 bool）—— 但原因会写进 `job.raw`，
        免得"取消没生效"变成一个完全没有痕迹的事件。
        """
        if not job.job_id or is_terminal(job.state):
            return False
        result = self._execute(build_dkill_argv(job.job_id), cwd=None)
        if result.returncode != 0 or result.timed_out:
            job.raw = _trim(
                f"{DKILL} {job.job_id} failed (rc={result.returncode}"
                f"{', timed out' if result.timed_out else ''}): {result.stderr or result.stdout}"
            )
            return False
        job.state = JobState.CANCELLED
        if not job.ended_at:
            job.ended_at = self._clock()
        return True

    # ---------------------------------------------------------------- 诊断
    def peek(self, job: Job, *, lines: int = 40) -> str:
        """`dpeek <id>` 的尾巴 —— 失败时给人看现场。**永不抛异常**，拿不到就返回空串。

        诊断用的东西不许把主流程带崩：driver 是在"某个 run 已经失败了"的路径上调它的。
        """
        if not job.job_id:
            return ""
        try:
            result = self._execute(build_dpeek_argv(job.job_id), cwd=None)
        except Exception:  # noqa: BLE001 - 纯诊断，任何失败都只是"没捞到"
            return ""
        text = result.stdout or result.stderr or ""
        return "\n".join(text.splitlines()[-lines:])

    # ---------------------------------------------------------------- 内部
    def _advance(self, job: Job, state: JobState) -> Job:
        """状态推进：只改该改的字段，时间戳只写一次（第一次进入该状态时）。"""
        started = job.started_at
        ended = job.ended_at
        if state is JobState.RUNNING and not started:
            started = self._clock()
        if is_terminal(state) and not ended:
            ended = self._clock()
        return dataclasses.replace(job, state=state, started_at=started, ended_at=ended, raw="")

    def _query(self, job_ids: Sequence[str]) -> tuple[dict[str, JobState], str]:
        """查一批 job 的状态，返回 (状态表, 原始回显)。

        先试一次 `djob <id> <id> …`；这条路失败过一次就永久退回逐个查询
        （`djob` 支不支持多 id 没实测到；查不到状态的后果是批次卡死，比多起几个进程贵）。
        """
        ids = list(job_ids)
        if self._multi_id_ok and len(ids) > 1:
            result = self._execute(build_djob_argv(ids), cwd=None)
            text = _combined(result)
            states = parse_djob_output(text)
            if states or result.returncode == 0:
                return states, text
            self._multi_id_ok = False
        merged: dict[str, JobState] = {}
        chunks: list[str] = []
        for job_id in ids:
            result = self._execute(build_djob_argv([job_id]), cwd=None)
            text = _combined(result)
            chunks.append(text)
            merged.update(parse_djob_output(text))
        return merged, "\n".join(chunk for chunk in chunks if chunk.strip())

    def _execute(self, argv: Sequence[str], *, cwd: str | None) -> RunResult:
        """跑一条命令。`runner` 没给就惰性拿默认的（本机测试永远注入假的）。"""
        runner = self.runner
        if runner is None:
            runner = self.runner = _default_runner()
        return runner.run(
            list(argv),
            cwd=cwd,
            env=self.env or None,
            timeout=self.timeout,
        )


def _append_missing_mechanism(argv: list[str], plan: CommandPlan) -> None:
    """往用户改过的前缀上补机制层的三个 flag —— **只补用户没写过的**。

    同一个 flag 传两次，dsub 认哪个是没定义的（也可能直接报错）。所以这里逐个查在不在，
    在就不补。`tests/test_sched_donau.py` 有计数断言盯着"每个 flag 恰好一次"。
    """
    if plan.cwd and not _has_flag(argv, EXEC_PATH_FLAG, "--execPath"):
        argv += [EXEC_PATH_FLAG, plan.cwd]
    if plan.log_path and not _has_flag(argv, STDLOG_FLAG, "-oo", "--stdlog", "--stdlog_override"):
        argv += [STDLOG_FLAG, plan.log_path]
    if JSON_FLAG and not _has_flag(argv, JSON_FLAG, "--json"):
        argv.append(JSON_FLAG)


def _has_flag(argv: Sequence[str], *names: str) -> bool:
    """argv 里有没有这几个 flag 中的任意一个（认 `--x=y` 的等号写法）。"""
    for token in argv:
        for name in names:
            if token == name or (name.startswith("--") and token.startswith(name + "=")):
                return True
    return False


def _combined(result: RunResult) -> str:
    """stdout + stderr —— 有些工具把 id / 状态打到 stderr 上。

    合并是安全的：两个解析器都要求出现明确的 `jobid` / 状态标记才认，
    不会把 stderr 里随便一个数字当成 job id。
    """
    out = result.stdout or ""
    err = result.stderr or ""
    if err.strip():
        return out + ("\n" if out and not out.endswith("\n") else "") + err
    return out


def _default_runner() -> RunnerProtocol:
    """默认 runner = `sched.driver.SubprocessRunner`，**惰性 import**。

    惰性的理由是同阶段并行：`driver.py` 和本文件是同时写出来的，import 时就绑死会让
    两边任何一边没写完都 import 不动（`__init__.py` 零 import 的同一条纪律）。
    """
    try:
        from .driver import SubprocessRunner
    except ImportError as exc:  # pragma: no cover - driver 缺失只可能发生在半成品树上
        raise SchedulerError(
            "no usable runner: none was injected into DonauScheduler and "
            f"ewave_batch.sched.driver.SubprocessRunner cannot be imported either ({exc})"
        ) from exc
    return SubprocessRunner()
