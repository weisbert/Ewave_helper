"""`ewave_batch.cli` —— 五个子命令：`run` / `dry-run` / `resume` / `archive` / `status`。

```
python -m ewave_batch dry-run  my_spec.yaml      # 只打印，什么都不写
python -m ewave_batch run      my_spec.yaml      # 真跑（提交 + 轮询 + 验收 + 归档）
python -m ewave_batch resume   ./batches/b1      # 只补没成的
python -m ewave_batch archive  ./batches/b1      # 补做 D5 归档
python -m ewave_batch status   ./batches/b1      # 状态 / 墙钟 / jobid / 产物 / 收敛
```

## 三条本文件必须守住的纪律

1. **惰性 import（CLAUDE.md 硬约束 5）。** 本模块**任何位置**都不在模块顶层 import
   `tkinter` 或 `gui.*` —— 无 `$DISPLAY`、甚至没装 tkinter 的纯 ssh 会话里 CLI 必须照常可用。
   GUI 只在 `--gui` 这一个分支里就地 import（那是"用户明确要 GUI"的唯一入口）。
   机器判据在 `tests/test_cli.py::LazyImport`：在子进程里让 `tkinter` 变成不可 import，
   照样跑 `dry-run` / `status` 并断言退 0；再断言 `import ewave_batch.cli` 之后
   `sys.modules` 里没有 `tkinter`、也没有 `gui`。

2. **`dry-run --self-test` 是闸门的第 4 步，一个字都不许改。**
   `scripts/check.sh` 跑 `python -m ewave_batch dry-run --self-test`，而 `ewave_batch/__main__.py`
   在把参数转给本模块**之前**就把 `--self-test` 短路掉了 —— 那条路和本模块无关。
   本模块仍然认这个 flag（转给同一个 `selftest()`），好让
   `python cli.py dry-run --self-test` 这条路也给出同样的答案：闸门的判据不该取决于
   用户用了哪个入口。

3. **入口第一件事 `ascii_safe_stdio()`。** 红区登录 shell 是 csh/tcsh，`LANG` 常是 `C`，
   而 driver / core 的诊断消息带中文 —— 不做这一步一个 `print` 就让进程退 1。

## 错误都带「下一步怎么办」

用户范围是"先自己用，后面给同事"，所以每一条错误都是
`error: <发生了什么>` + 一行或多行 `next: <该做什么>`（`_error` 是唯一出口）。
`core.*` 抛出来的 `EwaveBatchError` 消息本身已经带中文的"下一步"，本模块**再补一条英文的**
（按异常类型分派，见 `_NEXT_STEPS`）—— 两者不重复：前者说"这份输入哪里不对"，
后者说"在命令行上该敲什么"。

## 界面语言 = 英文

照 `SNP_RLC_Extractor` 与 `gui/` 的先例：**用户可见的输出全英文**，代码注释仍写中文。
顺带一个好处：英文输出在 `LANG=C` 下逐字节安全，不依赖第 3 条的兜底。
（`core.*` / `sched.*` 抛出来的消息是中文的，原样透传 —— 那是诊断信息，
翻译它等于在两个地方各维护一份说法。）

🚨 本文件零站点标识符：库名 / cell 名 / 路径 / 账号 / 队列全部来自 spec、
`batch.json` 或运行时解析（`core.discover`），源码里一个真实取值都没有（硬约束 1b）。
"""

from __future__ import annotations

import argparse
import os
import posixpath
import shlex
import sys
from collections.abc import Mapping, Sequence

from . import __version__
from ._stdio import ascii_safe_stdio
from .core import discover as discover_module
from .core import layout as layout_module
from .core import logparse as logparse_module
from .core import matrix as matrix_module
from .core import spec as spec_module
from .model import (
    BATCH_JSON_NAME,
    RUNS_CSV_NAME,
    BatchOptions,
    BatchState,
    Design,
    DiscoveryError,
    DriverEvent,
    EventKind,
    EwaveBatchError,
    FlagConflictError,
    LogFacts,
    PlanContext,
    Run,
    RunStatus,
    SchedulerError,
    SiteFacts,
    SpecError,
    StateError,
    ToolMissingError,
)

# --------------------------------------------------------------------------
# 冻结面上的常量
# --------------------------------------------------------------------------

SUBCOMMANDS: tuple[str, ...] = ("run", "dry-run", "resume", "archive", "status")
"""五个子命令。**恰好 5 个**（`docs/INTERFACES.md`「常量：谁负责给出什么」）。

顺序 = 用户实际会用到的顺序：先 `dry-run` 看，再 `run` 跑，挂了 `resume` 补，
`archive` 收尾，`status` 事后查。`tests/test_cli.py::SubcommandCoverage` 拿它做计数断言 ——
少写一个子命令、或者写了却没测，都当场红。
"""

# --------------------------------------------------------------------------
# 退出码（写进 `--help`，机器可判）
# --------------------------------------------------------------------------

EXIT_OK = 0
"""干完了：批次全成 / 报告打出来了 / dry-run 规划完整。"""

EXIT_RUN_FAILED = 1
"""跑完了但**有 run 没成**（`status` 看到 failed 也是这个码）。不是崩溃 —— 去看 `status`。"""

EXIT_USAGE = 2
"""用法 / 输入错：spec 不存在或非法、`batch.json` 缺失或损坏、落点选在了设计师的 spine 里、
坐标缺得连一条命令都拼不出来。**一个 job 都没提交。**（argparse 自己的用法错也是 2。）"""

EXIT_INTERRUPTED = 130
"""Ctrl-C。在飞的 job 会先被取消（`128 + SIGINT`，shell 的老规矩）。"""

_EPILOG = """\
exit codes:
  0    finished, everything succeeded (or the report was printed)
  1    the batch finished but at least one run failed - see `status`
  2    usage or input error: bad spec, missing/corrupt batch.json, landing spot
       inside the designer spine, or site coordinates too incomplete to build a
       single command. Nothing was submitted.
  130  interrupted with Ctrl-C (in-flight jobs are cancelled first)

Read `docs/INTERFACES.md` for the module map and `PROJECT_BRIEF.md` for the design.
"""

_SCHEDULERS: tuple[str, ...] = ("donau", "fake")
"""`BatchOptions.scheduler` 的合法取值。`fake` = 本机跑一遍假批次，什么都不提交。"""

_MESSAGE_KINDS: frozenset[EventKind] = frozenset(
    {EventKind.FAILED, EventKind.SKIPPED, EventKind.WARNING, EventKind.INFO}
)
"""这些事件的 `message` 一定要打出来 —— 它们是"为什么"，短标签替代不了。
其余事件（submitted / started / finished / archived）只打一行标签，
免得一个 12-run 批次刷出几百行看不完的字。"""

_NEXT_STEPS: tuple[tuple[type, tuple[str, ...]], ...] = (
    # 顺序有意义：从具体到笼统，第一个 isinstance 命中的赢。
    (
        ToolMissingError,
        (
            "point --official-run-dir at a design directory the official GUI has run "
            "(the one that contains gdsout_setup); every site coordinate is read from there",
            "or make the tools visible: `command -v ewave strmout`, "
            "or set EWAVE_BIN / STRMOUT_BIN",
        ),
    ),
    (
        DiscoveryError,
        (
            "check that --official-run-dir points at a *design* directory "
            "(it must contain gdsout_setup), not at the workarea above it",
            "`python -m ewave_batch.redzone_dryrun --offdir <dir>` prints what it could parse",
        ),
    ),
    (
        FlagConflictError,
        (
            "drop that flag from extra_flags - an axis or the tool itself already owns it",
            "axes win over user flags on purpose: the directory name must not disagree "
            "with the command line",
        ),
    ),
    (
        SchedulerError,
        (
            "check the queue and account: they are read from the official remote submit "
            "script under --official-run-dir",
            "`--scheduler fake` runs the whole batch locally without submitting anything",
        ),
    ),
    (
        StateError,
        (
            f"pass the directory that holds {BATCH_JSON_NAME} "
            "(the 'batch dir' line printed by `run`)",
            "`run <spec>` creates it; nothing else does",
        ),
    ),
    (
        SpecError,
        (
            "compare the spec with the built-in example: "
            "`python -c \"import sys; from ewave_batch.core.spec import EXAMPLE_SPEC; sys.stdout.buffer.write(EXAMPLE_SPEC.encode('utf-8'))\"`",
            "`dry-run <spec>` prints the whole matrix without writing anything",
        ),
    ),
)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _posix(path: str) -> str:
    """路径归一成 `/` 分隔、无尾斜杠。

    Windows 上 `os.path.abspath` 给的是反斜杠，而 `core.layout` 全程用 `/` ——
    不在入口归一，同一个批次目录会以两种写法出现在输出里（`--gds=C:\\a\\b/gds/x.gds`
    那种混合形状）。本工具的最终运行环境是 Linux，`/` 是唯一正确的那个。
    """
    text = str(path).replace("\\", "/")
    while len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    return text


def _shell(argv: Sequence[str]) -> str:
    """argv → 一条可以直接粘回终端的命令行。"""
    return " ".join(shlex.quote(str(token)) for token in argv)


def _error(what: str, *next_steps: str) -> int:
    """打印一条带「下一步」的错误，返回 `EXIT_USAGE`。**本模块报错的唯一出口。**

    形状固定成 `error:` + 若干 `next:` 是为了让人和机器都好认：
    用户一眼看到该敲什么，测试用 `assertIn("next:", ...)` 就能断言"这条错误没把人晾着"。
    """
    print(f"error: {what}", file=sys.stderr)
    for step in next_steps:
        if step:
            print(f"  next: {step}", file=sys.stderr)
    return EXIT_USAGE


def _next_steps_for(exc: BaseException) -> tuple[str, ...]:
    """按异常类型给「下一步」。认不出来就给一条通用的。"""
    for kind, steps in _NEXT_STEPS:
        if isinstance(exc, kind):
            return steps
    return (
        "`python -m ewave_batch <subcommand> --help` lists the options",
        "`dry-run <spec>` reproduces the planning step without writing anything",
    )


def _note(text: str) -> None:
    """一条不致命的提醒（缺坐标之类）。走 stderr —— stdout 是报告，要能被管道接走。"""
    print(f"note: {text}", file=sys.stderr)


def _fmt_float(value: float | None, digits: int = 1) -> str:
    """`None` → `-`（"没测到"不是 0，`LogFacts` 的规矩）。"""
    return "-" if value is None else f"{value:.{digits}f}"


def _fmt_bool(value: bool | None) -> str:
    return "-" if value is None else ("yes" if value else "no")


def _design_of(state: BatchState, run: Run) -> Design:
    """这个 run 属于哪个 design。对不上号 → `SpecError`（`batch.json` 自相矛盾）。"""
    for design in state.designs:
        if matrix_module.design_key(design) == run.design_key:
            return design
    raise SpecError(
        f"run {run.run_id!r} 指向的 design {run.design_key!r} 不在 designs 列表里 —— "
        f"{BATCH_JSON_NAME} 自相矛盾（是手工改过吗？）"
    )


def _guard_spine(batch_dir: str) -> None:
    """落点不许在设计师的 spine 里（CLAUDE.md 硬约束 4）。

    `core.layout` 在**写**的时候也有同一道守卫，但那是最后一道：`dry-run` 一个字节都不写，
    于是它永远撞不到那道守卫，而"落点选错了"恰恰是最该在 dry-run 阶段就说清楚的事。
    所以这里在**规划一开始**就查一遍 —— 判据（路径里任何一层叫
    `core.layout.SPINE_DIRNAME`）与那道守卫同源，不另立一份。
    """
    spine = layout_module.SPINE_DIRNAME
    if spine in _posix(batch_dir).split("/"):
        raise StateError(
            f"the landing spot is inside {spine}/: {batch_dir}\n"
            f"  that directory belongs to the official GUI (the designer's spine); "
            f"this tool only ever reads it"
        )


# --------------------------------------------------------------------------
# 站点坐标：单独一个函数，因为它是**测试唯一需要替换的东西**
# --------------------------------------------------------------------------


def discover_facts(
    official_run_dir: str, *, env: Mapping[str, str] | None = None
) -> SiteFacts:
    """解析一个官方 run 目录 → `SiteFacts`。薄封装到 `core.discover`。

    单独一个模块级函数（而不是内联那一行）的理由：**本机没有官方 run 目录，也没有
    `ewave` / `strmout`**（CLAUDE.md 硬约束 3），而 CLI 的测试要验的是"子命令把核心件
    接起来之后行为对"，不是"解析真目录对不对"（那是 `tests/test_discover.py` 的活）。
    测试把这个名字换掉就能注入一份手写的坐标，`main()` 的签名一个字不用改
    （它是冻结的）—— 与 `gui.state.GuiState(discover=…)` 是同一条口子，形状不同而已。
    """
    return discover_module.discover_site_facts(official_run_dir, env=env)


def _facts_for(official_run_dir: str, seen: dict[str, SiteFacts]) -> SiteFacts:
    """带缓存地解析坐标。解析不了**不抛** —— 记一条 note，交给后面的 preflight 决定生死。

    为什么不当场抛：`dry-run` 在本机（没有官方目录）也必须能把矩阵打出来 ——
    "这批会跑哪些 run、落在哪" 与站点坐标无关，而那正是 dry-run 最常被用来回答的问题。
    真正拼不出命令的地方会给出精确的 `ToolMissingError`，位置比这里准。
    """
    key = _posix(official_run_dir)
    cached = seen.get(key)
    if cached is not None:
        return cached
    if not key:
        facts = SiteFacts()
        _note(
            "no official run dir given - ports, ptxt, queue and the tool paths are unknown, "
            "so commands cannot be built (pass --official-run-dir, or put "
            "official_run_dir: into each design in the spec)"
        )
    else:
        try:
            facts = discover_facts(key)
        except EwaveBatchError as exc:
            facts = SiteFacts(official_run_dir=key)
            _note(f"{key}: {exc}")
    seen[key] = facts
    return facts


def _contexts_for(state: BatchState, *, official_run_dir: str = "") -> dict[str, PlanContext]:
    """每个 design 一份 `PlanContext`（坐标是 **per-design** 解析的）。

    默认表按 §11 规则 1 **学自官方 run 目录**，再让 spec 里的 `defaults:` 覆盖 ——
    源码里一个默认值都不写死，换 PDK 自动跟上。
    """
    contexts: dict[str, PlanContext] = {}
    seen: dict[str, SiteFacts] = {}
    for design in state.designs:
        key = matrix_module.design_key(design)
        facts = _facts_for(design.official_run_dir or official_run_dir, seen)
        defaults = dict(discover_module.learn_default_flags(facts))
        defaults.update(state.defaults)
        contexts[key] = PlanContext(
            design=design,
            facts=facts,
            axes=tuple(state.axes),
            defaults=defaults,
            extra_flags=dict(state.extra_flags),
            options=state.options,
            batch_dir=state.batch_dir,
        )
    return contexts


# --------------------------------------------------------------------------
# spec → BatchState（run / dry-run 共用）
# --------------------------------------------------------------------------


def _apply_option_overrides(options: BatchOptions, args: argparse.Namespace) -> BatchOptions:
    """命令行上给了的选项覆盖 spec 里的（就地改，返回同一个对象）。

    `None` = 命令行没给 ⇒ 保留 spec 的值。**别用 `or`**：`--max-parallel 0`
    和"没给"是两回事，前者是用户明确说的话。
    """
    if getattr(args, "scheduler", None) is not None:
        options.scheduler = args.scheduler
    if getattr(args, "max_parallel", None) is not None:
        options.max_parallel = args.max_parallel
    if getattr(args, "poll_interval", None) is not None:
        options.poll_interval = args.poll_interval
    if options.scheduler not in _SCHEDULERS:
        raise SpecError(
            f"unknown scheduler {options.scheduler!r}; expected one of: "
            f"{', '.join(_SCHEDULERS)}"
        )
    return options


def _plan_from_spec(args: argparse.Namespace) -> tuple[BatchState, dict[str, PlanContext]]:
    """spec → `BatchState` + 每个 design 的 `PlanContext`。**一个目录都不建。**"""
    spec = spec_module.load_spec(args.spec)
    if args.batch_name:
        spec.batch_name = args.batch_name
    _apply_option_overrides(spec.options, args)
    spec.options.dry_run = bool(getattr(args, "plan_only", False))
    state = spec_module.spec_to_batch(
        spec, batch_root=args.batch_root, tool_version=__version__
    )
    # 归一成 `/`：`spec_to_batch` 用 `os.path.abspath`，Windows 上给反斜杠，
    # 而 `core.layout` 全程 `/` —— 不在这儿统一，输出里会出现两种写法的同一个目录。
    state.batch_dir = _posix(state.batch_dir)
    _guard_spine(state.batch_dir)
    contexts = _contexts_for(state, official_run_dir=args.official_run_dir)
    return state, contexts


def _fill_work_dirs(state: BatchState) -> None:
    """给每个 run 补上 `work_dir`（`expand_runs` 不填它 —— 它不知道 batch_dir）。

    唯一的副作用是"给 `Run.work_dir` 赋值"：`compute_run_paths` 自己一个目录都不建。
    driver 后面会再做一遍同样的事（幂等），这里先做是因为 preflight 和 dry-run 都要
    在 driver 之前拼命令，而 `--workDir` 正是从 `run.work_dir` 来的 —— 不先填，
    dry-run 打出来的命令里 `--workDir=` 会是空的，那条命令粘回终端会把产物落进当前目录。
    """
    for run in state.runs:
        design = _design_of(state, run)
        run.work_dir = run.work_dir or layout_module.compute_run_paths(
            state.batch_dir, design, run
        ).run_dir


def _preflight(
    state: BatchState, contexts: Mapping[str, PlanContext]
) -> list[tuple[str, EwaveBatchError]]:
    """真提交之前先把每条命令都拼一遍，返回 `(run_id, 异常)` 列表（空 = 全都拼得出来）。

    为什么值得多花这一遍：坐标缺一样（没有 `ewave_bin`、没有 ptxt…），**每一个** run 都会
    在同一个原因上失败。没有 preflight 时用户看到的是"12 个 job 全 failed"，
    而真正的原因埋在 12 条一模一样的消息里；有了它，一个 job 都不提交，
    错误只说一遍，且带着"下一步"。§12 的 fail-fast 精神在阶段 2 之前也成立。
    """
    from .tools import ewave as ewave_tool  # 惰性：status / archive 用不着拼命令

    problems: list[tuple[str, EwaveBatchError]] = []
    for run in state.runs:
        try:
            ewave_tool.build_ewave_plan(run, contexts[run.design_key])
        except EwaveBatchError as exc:
            problems.append((run.run_id, exc))
    return problems


# --------------------------------------------------------------------------
# batch.json → BatchState（resume / archive / status 共用）
# --------------------------------------------------------------------------


def _resolve_batch(path: str) -> tuple[str, str]:
    """`(batch_dir, batch_json)`。给目录、给 `batch.json` 本身，两种都认。"""
    target = _posix(os.path.abspath(os.path.expanduser(str(path))))
    if target.endswith("/" + BATCH_JSON_NAME) or (
        os.path.isfile(target) and os.path.basename(target) == BATCH_JSON_NAME
    ):
        return posixpath.dirname(target), target
    return target, posixpath.join(target, BATCH_JSON_NAME)


def _read_batch(path: str) -> tuple[str, BatchState]:
    """读一个已有批次。目录不对 / JSON 坏 / schema 太新 → `StateError`。

    ⚠️ **以现在这个目录为准**：`batch.json` 里记的 `batch_dir` 是当初那台机器上的绝对路径，
    批次被搬过（或者当初就记的是别人的路径）时照着它走会让所有落点指到不存在的地方，
    而症状是"产物一个都验不过" —— 极难查。`sched.driver.resume_batch` 用的是同一条规矩。
    """
    batch_dir, batch_json = _resolve_batch(path)
    if not os.path.isfile(batch_json):
        raise StateError(
            f"no {BATCH_JSON_NAME} under {batch_dir} - that directory is not a batch"
        )
    state = layout_module.read_batch_state(batch_json)
    state.batch_dir = batch_dir
    return batch_dir, state


# --------------------------------------------------------------------------
# 执行面：调度器 / runner
# --------------------------------------------------------------------------


def _make_backends(
    options: BatchOptions, contexts: Mapping[str, PlanContext]
) -> tuple[object, object]:
    """`(scheduler, runner)`。**两者都惰性 import** —— `status` / `archive` 用不着它们。

    `fake` 那一路让 scheduler 和 runner **共用同一个 `FakeRunner`**：阶段 1（strmout）
    走 runner、阶段 2 的产物由 scheduler 在终态那一拍让 runner 写出来，
    两边必须是同一个对象，否则本机跑出来的假批次里阶段 1 用真 `SubprocessRunner`
    去找一个本机根本没有的 `strmout`，整批直接 skipped —— 假批次也就失去了意义。
    """
    if options.scheduler == "fake":
        from .sched.fake import FakeRunner, FakeScheduler

        runner = FakeRunner()
        return FakeScheduler(runner), runner

    from .sched.donau import DonauScheduler
    from .sched.driver import SubprocessRunner

    runner = SubprocessRunner()
    facts = next(
        (ctx.facts for ctx in contexts.values() if ctx.facts.dsub_account or ctx.facts.dsub_queue),
        next(iter(contexts.values())).facts if contexts else SiteFacts(),
    )
    return DonauScheduler.from_site_facts(facts, runner), runner


class _EventPrinter:
    """driver 的事件 → 一行输出。CLI 和 GUI 用的是同一份 driver，区别只在这个回调。"""

    def __init__(self, *, verbose: bool = False, quiet: bool = False) -> None:
        self.verbose = verbose
        self.quiet = quiet
        self.lines: list[str] = []

    def __call__(self, event: DriverEvent) -> None:
        loud = event.kind in (EventKind.FAILED, EventKind.WARNING)
        if self.quiet and not loud:
            return
        who = event.run_id or event.design_key or "-"
        line = f"[{event.kind.value}] {who}"
        if event.message and (self.verbose or event.kind in _MESSAGE_KINDS):
            line = f"{line}  {event.message}"
        self.lines.append(line)
        print(line)


def _summary_line(state: BatchState) -> str:
    """`12 runs: 8 done, 4 failed` —— 只列非零的桶，零的桶是噪音。"""
    counts: dict[str, int] = {}
    for run in state.runs:
        counts[run.status.value] = counts.get(run.status.value, 0) + 1
    body = ", ".join(
        f"{counts[status.value]} {status.value}"
        for status in RunStatus
        if counts.get(status.value)
    )
    return f"{len(state.runs)} runs: {body or 'none'}"


def _print_port_warnings(state: BatchState) -> None:
    """批次内端口一致性（`--all` 的代价，BRIEF §5）。有问题就说 —— 别让人继续比数字。"""
    for problem in layout_module.check_port_consistency(state):
        _note(problem)


# --------------------------------------------------------------------------
# 子命令：dry-run
# --------------------------------------------------------------------------


def _handle_dry_run(args: argparse.Namespace) -> int:
    """规划一遍并打印，**一个文件都不写、一个 job 都不提交**（D8）。"""
    if args.self_test:
        # 闸门第 4 步走的是 `ewave_batch/__main__.py` 的短路，根本到不了这里。
        # 这里认同一个 flag 是为了让 `python cli.py dry-run --self-test` 给出同样的答案。
        from .__main__ import selftest

        return selftest()
    if not args.spec:
        return _error(
            "dry-run needs a spec file",
            "`dry-run <spec.yaml>` prints every command and landing directory",
            "`dry-run --self-test` instead checks the frozen interface for drift",
        )

    args.plan_only = True
    state, contexts = _plan_from_spec(args)
    _fill_work_dirs(state)

    from .sched.driver import make_driver  # 惰性：同一份 driver，dry-run 只是它的一个分支

    scheduler, runner = _make_backends(state.options, contexts)
    driver = make_driver(state, contexts, scheduler, runner)
    # `Driver._plan_only` 一拍就把整批规划完（dry-run 没有会自己前进的东西）。
    # 循环只是保险丝，不是等待。
    for _ in range(4):
        if driver.tick().finished:
            break

    print("dry run - nothing is written, nothing is submitted")
    print(f"  spec        {state.provenance.spec_path or args.spec}")
    print(f"  batch dir   {state.batch_dir}")
    print(f"  designs     {len(state.designs)}")
    print(f"  runs        {len(state.runs)}")
    print(f"  scheduler   {state.options.scheduler} (not contacted)")

    print("")
    print("stage 1  streamout (one per design, shared by the whole matrix)")
    for index, task in enumerate(state.streamout, start=1):
        print(f"  [{index}/{len(state.streamout)}] {task.design_key}")
        if task.argv:
            print(f"    argv      {_shell(task.argv)}")
        elif task.gds_path:
            print(f"    skipped   gds_path given in the spec: {task.gds_path}")
        else:
            print(f"    argv      <unavailable> {task.message or 'command could not be built'}")

    print("")
    print("stage 2  solve (one per design x axis combination)")
    limit = args.limit if args.limit > 0 else len(state.runs)
    # ★ 从**全部** run 数，不是从打印出来的那些数：`--limit` 只影响打印的详细程度，
    # 不影响结论。跟着循环数会让 `--limit 1` 报出 "1 commands built"，
    # 而那正是用户拿来判断"这份 spec 在这台机器上能不能真跑"的那个数字。
    unbuildable = sum(1 for run in state.runs if not run.argv)
    for index, run in enumerate(state.runs, start=1):
        design = _design_of(state, run)
        paths = layout_module.compute_run_paths(state.batch_dir, design, run)
        print(f"  [{index}/{len(state.runs)}] {run.run_id}")
        if index > limit:
            print(f"    work dir  {paths.run_dir}")
            continue
        print(f"    work dir  {paths.run_dir}")
        print(f"    ewave dir {paths.ewave_dir or '<decided by eWave at run time>'}")
        print(f"    cmd.sh    {paths.cmd_sh}")
        print(f"    sparam    {paths.sparam_prefix}.sNp")
        if run.argv:
            print(f"    argv      {_shell(run.argv)}")
        else:
            print(f"    argv      <unavailable> {run.message or 'command could not be built'}")

    print("")
    built = len(state.runs) - unbuildable
    print(f"dry-run: {len(state.runs)} runs planned, {built} commands built, 0 files written")
    if unbuildable:
        _note(
            f"{unbuildable} of {len(state.runs)} commands could not be built - "
            "site coordinates are missing (this is normal off-site; "
            "pass --official-run-dir where the tools live)"
        )
        if args.strict:
            return _error(
                f"--strict: {unbuildable} of {len(state.runs)} commands could not be built",
                "run this on the machine that has the official run directory and the tools",
                *_NEXT_STEPS[0][1],
            )
    return EXIT_OK


# --------------------------------------------------------------------------
# 子命令：run
# --------------------------------------------------------------------------


def _drive(driver: object, args: argparse.Namespace) -> int:
    """`while driver.tick()` 到全部终态。Ctrl-C 时先取消在飞的 job 再退。

    `run` 和 `resume` 共用这一段 —— 两者的区别只在"driver 是怎么造出来的"。
    """
    from .sched.driver import run_batch

    state: BatchState = driver.state  # type: ignore[attr-defined]
    poll = state.options.poll_interval
    try:
        rc = run_batch(driver, poll_interval=poll, max_seconds=args.max_seconds)
    except KeyboardInterrupt:
        print("")
        print("interrupted - cancelling in-flight jobs")
        driver.cancel()  # type: ignore[attr-defined]
        print(f"batch {state.batch_name}: {_summary_line(state)}")
        return EXIT_INTERRUPTED
    _print_port_warnings(state)
    print("")
    print(f"batch {state.batch_name}: {_summary_line(state)}")
    print(f"  batch dir   {state.batch_dir}")
    print(f"  state       {posixpath.join(state.batch_dir, BATCH_JSON_NAME)}")
    print(f"  summary     {posixpath.join(state.batch_dir, RUNS_CSV_NAME)}")
    if rc != 0:
        print(f"  next        `python -m ewave_batch status {state.batch_dir}` for the details")
        print(f"  next        `python -m ewave_batch resume {state.batch_dir}` retries only the "
              "runs that did not finish")
        return EXIT_RUN_FAILED
    return EXIT_OK


def _handle_run(args: argparse.Namespace) -> int:
    """读 spec → 展开矩阵 → 建 driver → 驱动到全部终态。"""
    args.plan_only = False
    state, contexts = _plan_from_spec(args)
    _fill_work_dirs(state)

    problems = _preflight(state, contexts)
    if problems:
        run_id, exc = problems[0]
        return _error(
            f"{len(problems)} of {len(state.runs)} commands could not be built, "
            f"so nothing was submitted. First one - {run_id}: "
            f"{exc.__class__.__name__}: {exc}",
            *_next_steps_for(exc),
        )

    from .sched.driver import make_driver

    scheduler, runner = _make_backends(state.options, contexts)
    printer = _EventPrinter(verbose=args.verbose, quiet=args.quiet)
    driver = make_driver(state, contexts, scheduler, runner, on_event=printer)
    print(f"batch {state.batch_name}: {len(state.runs)} runs -> {state.batch_dir}")
    print(f"  scheduler   {state.options.scheduler}, max {state.options.max_parallel} in flight")
    return _drive(driver, args)


# --------------------------------------------------------------------------
# 子命令：resume
# --------------------------------------------------------------------------


def _handle_resume(args: argparse.Namespace) -> int:
    """从 `batch.json` 续跑（D7）：**已经 done 的一个都不重跑**。"""
    batch_dir, state = _read_batch(args.batch_dir)
    _apply_option_overrides(state.options, args)
    contexts = _contexts_for(state, official_run_dir=args.official_run_dir)

    from .sched.driver import resume_batch

    scheduler, runner = _make_backends(state.options, contexts)
    printer = _EventPrinter(verbose=args.verbose, quiet=args.quiet)
    # 抬头先打：`resume_batch` 在**构造过程中**就会播事件（"job 还活着"/"这个 done 是假的"），
    # 抬头晚一步就会出现在那些事件底下，读起来像是它们属于上一条命令。
    print(f"resuming {batch_dir}: {_summary_line(state)} before this resume")
    driver = resume_batch(batch_dir, contexts, scheduler, runner, on_event=printer)
    # `resume_batch` 自己从磁盘重读了一份 state ⇒ 命令行给的覆盖要往**它那一份**上再套一次，
    # 否则 `--max-parallel` / `--poll-interval` 在 resume 上静默失效。
    _apply_option_overrides(driver.state.options, args)
    return _drive(driver, args)


# --------------------------------------------------------------------------
# 子命令：archive
# --------------------------------------------------------------------------


def _handle_archive(args: argparse.Namespace) -> int:
    """对已经跑完的批次补做 D5 归档：参数文件收进 `sparam/`，mesh 中间件删掉。

    ⚠️ **不改 `batch.json`。** 归档只动 run 目录里的文件；`Run.artifacts` 由 driver
    在跑的时候记（它有一份"扁平区里哪些文件属于这个 run"的精确判据，
    裸前缀会让 `typical_25_0` 认领走 `typical_25_05` 的产物）。在这里再写一份
    就是第二份会漂移的实现 —— 宁可这一列留空，也不要一列**看起来对**的错数据。
    """
    _batch_dir, state = _read_batch(args.batch_dir)
    print(f"archive {state.batch_dir}  (keep: {', '.join(state.options.archive_keep)})")
    if args.dry_run:
        print("  dry run - nothing is copied, nothing is deleted")

    archived = 0
    skipped = 0
    failures = 0
    for index, run in enumerate(state.runs, start=1):
        head = f"  [{index}/{len(state.runs)}] {run.run_id}"
        if run.status is not RunStatus.DONE:
            skipped += 1
            print(f"{head}  skipped ({run.status.value})")
            continue
        design = _design_of(state, run)
        paths = layout_module.compute_run_paths(state.batch_dir, design, run)
        report = layout_module.archive_run(
            paths,
            run,
            keep=state.options.archive_keep,
            keep_logs_on_failure=state.options.keep_logs_on_failure,
            dry_run=args.dry_run,
        )
        archived += 1
        print(
            f"{head}  kept {len(report.kept)}, removed {len(report.removed)}, "
            f"freed {report.bytes_freed} bytes"
        )
        for missing in report.missing:
            _note(f"{run.run_id}: keep pattern {missing!r} matched nothing")
        for problem in report.errors:
            failures += 1
            _note(f"{run.run_id}: {problem}")

    print("")
    print(f"archive: {archived} archived, {skipped} skipped, {failures} problems")
    print(f"  flat copies land in {posixpath.join(state.batch_dir, 'sparam')}/")
    if failures:
        print(
            f"  next        `python -m ewave_batch status {state.batch_dir}` shows which runs "
            "have no artifacts",
            file=sys.stderr,
        )
        return EXIT_RUN_FAILED
    return EXIT_OK


# --------------------------------------------------------------------------
# 子命令：status
# --------------------------------------------------------------------------

_STATUS_COLUMNS: tuple[str, ...] = ("run", "status", "wall(s)", "job", "conv", "peakMB", "sparam")
"""`status` 那张表的表头。**不是 `RUNS_CSV_COLUMNS`** —— 那份是给下游程序读的（冻结、
14 列、加列只许往后追加），这份是给人在终端里看的。混用会让"终端好看"变成改冻结面的理由。"""


def _log_facts_for(run: Run, run_dir: str, ewave_dir: str, *, read_logs: bool) -> LogFacts | None:
    """这个 run 的日志事实。**磁盘优先，`batch.json` 里那份兜底。**（只读，不写任何文件。）

    两个来源都是真的，只是新鲜度和存活期不同：

    | 来源 | 新鲜度 | 什么时候它是唯一的一份 |
    |---|---|---|
    | 磁盘 | 现在这一刻 | 作业还在写；或者跑完之后有人手工补跑过什么 |
    | `Run.log_facts` | 这个 run 走到终态那一刻（`sched.driver._attach_log_facts` 存的） | run 目录已经被 `archive --clean` 清掉了 |

    ⇒ 谁也不能单独用。磁盘读得出来就以磁盘为准，读不出的字段由 state 补上
    （`merge_log_facts` 先到先得）。`--no-logs` 只关掉磁盘那一半 —— 它省的是 IO，
    不是"假装我们什么都不知道"。

    读的是 `<corner>_<temp>/` 那一层而不是 `run_dir`：同一个 `run_dir` 底下住着 N 个
    corner/temp 组合（`<axes-slug>` 按定义不含它们），而 `parse_run_logs` 会连**直接子目录**
    一起读 —— 对着 `run_dir` 读就是把邻居的日志也合并进来，然后报出一份张冠李戴的收敛结论。
    这条与 `core.logparse.run_log_files` 同源，两处必须一起改。
    """
    disk: LogFacts | None = None
    if read_logs:
        target = ewave_dir or run_dir
        if target and os.path.isdir(target):
            try:
                disk = logparse_module.parse_run_logs(target)
            except OSError:  # pragma: no cover - 读日志失败不该让 status 崩
                disk = None
    if disk is None:
        return run.log_facts
    if run.log_facts is None:
        return disk
    # 磁盘在前 = 磁盘优先（`merge_log_facts` 先到先得），state 只补磁盘没说的字段。
    return logparse_module.merge_log_facts(disk, run.log_facts)


def _handle_status(args: argparse.Namespace) -> int:
    """读 `batch.json` 打印每个 run 的状态 / 墙钟 / jobid / 产物（+ 日志里的收敛与峰值内存）。

    **只读**：一个文件都不写。退出码 1 = 有 run failed（好让脚本 `if ! status; then …`）。
    """
    _batch_dir, state = _read_batch(args.batch_dir)
    prov = state.provenance
    runs_csv = posixpath.join(state.batch_dir, RUNS_CSV_NAME)

    print(f"batch       {state.batch_name}")
    print(f"  directory   {state.batch_dir}")
    print(f"  spec        {prov.spec_path or '-'}")
    print(f"  tool        {prov.tool_version or '-'}  "
          f"interface {prov.interface_version}  schema {state.schema_version}")
    print(f"  created     {prov.created_at or '-'}")
    print(f"  updated     {prov.updated_at or '-'}")
    print(f"  summary     {runs_csv}{'' if os.path.isfile(runs_csv) else '  (not written yet)'}")
    print(f"  scheduler   {state.options.scheduler}")

    if state.streamout:
        done = sum(1 for task in state.streamout if task.status is RunStatus.DONE)
        print(f"  streamout   {done}/{len(state.streamout)} designs done")
        for task in state.streamout:
            if task.status is not RunStatus.DONE and task.message:
                _note(f"streamout {task.design_key}: {task.message}")

    rows: list[tuple[str, ...]] = []
    for run in state.runs:
        design = _design_of(state, run)
        paths = layout_module.compute_run_paths(state.batch_dir, design, run)
        facts = _log_facts_for(run, paths.run_dir, paths.ewave_dir, read_logs=not args.no_logs)
        rows.append(
            (
                run.run_id,
                run.status.value,
                _fmt_float(run.wall_seconds),
                (run.job.job_id if run.job is not None else "") or "-",
                _fmt_bool(None if facts is None else facts.converged),
                _fmt_float(None if facts is None else facts.peak_memory_mb),
                ";".join(run.artifacts) or "-",
            )
        )

    widths = [
        max(len(_STATUS_COLUMNS[i]), max((len(row[i]) for row in rows), default=0))
        for i in range(len(_STATUS_COLUMNS))
    ]
    print("")
    print("  " + "  ".join(name.ljust(widths[i]) for i, name in enumerate(_STATUS_COLUMNS)))
    print("  " + "  ".join("-" * widths[i] for i in range(len(_STATUS_COLUMNS))))
    for row in rows:
        print("  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))

    print("")
    print(f"status: {_summary_line(state)}")
    for run in state.runs:
        if run.status is RunStatus.FAILED and run.message:
            _note(f"{run.run_id}: {run.message}")
    _print_port_warnings(state)

    if any(run.status is RunStatus.FAILED for run in state.runs):
        print(
            f"  next        `python -m ewave_batch resume {state.batch_dir}` retries only the "
            "runs that did not finish",
            file=sys.stderr,
        )
        return EXIT_RUN_FAILED
    return EXIT_OK


# --------------------------------------------------------------------------
# 参数面
# --------------------------------------------------------------------------

_HANDLERS = {
    "run": _handle_run,
    "dry-run": _handle_dry_run,
    "resume": _handle_resume,
    "archive": _handle_archive,
    "status": _handle_status,
}
"""子命令 → 处理函数。**键必须与 `SUBCOMMANDS` 一字不差**
（`tests/test_cli.py::SubcommandCoverage` 有计数断言）。"""


def _add_planning_options(parser: argparse.ArgumentParser) -> None:
    """`run` / `dry-run` 共用的那几个（都是"批次落在哪、坐标从哪来"）。"""
    parser.add_argument(
        "--batch-root",
        default="",
        metavar="DIR",
        help="where batches land (default: batch_root: from the spec, "
        "else <install>/ewave_batches). Never point this inside the designer "
        "spine, and avoid $HOME - it is quota'd here and overrunning it is silent.",
    )
    parser.add_argument(
        "--batch-name",
        default="",
        metavar="NAME",
        help="batch directory name (default: batch_name: from the spec, else a UTC timestamp)",
    )
    parser.add_argument(
        "--official-run-dir",
        default="",
        metavar="DIR",
        help="a design directory the official GUI has already run (it contains gdsout_setup). "
        "Every site coordinate - library, ports, ptxt, queue, tool paths - is parsed from "
        "there at run time. Used for designs whose spec entry has no official_run_dir:.",
    )


def _add_execution_options(parser: argparse.ArgumentParser) -> None:
    """`run` / `resume` 共用的那几个（都是"怎么跑"）。"""
    parser.add_argument(
        "--scheduler",
        choices=_SCHEDULERS,
        default=None,
        help="submit backend (default: scheduler: from the spec, else donau). "
        "'fake' runs the whole batch locally and submits nothing.",
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=None,
        metavar="N",
        help="how many jobs may be in flight at once (default: from the spec)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=None,
        metavar="SEC",
        help="seconds between polls (default: from the spec). 0 = never sleep.",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        metavar="SEC",
        help="give up after this much wall clock (a fuse for CI; default: no limit)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="print every event message")
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="only print failures and warnings"
    )


def build_parser() -> argparse.ArgumentParser:
    """造 `argparse` 解析器。单独一个函数是为了让测试能直接拿它验参数面，不用起进程。"""
    parser = argparse.ArgumentParser(
        prog="python -m ewave_batch",
        description=(
            "Batch driver around the official eWave GUI: define a matrix of extraction "
            "settings once, run it in one go, keep every result in its own directory."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"ewave_batch {__version__}")
    parser.add_argument(
        "--gui",
        nargs="?",
        const="",
        default=None,
        metavar="LAYOUT",
        help="open the tkinter GUI instead of a subcommand; LAYOUT is optional "
        "(`python -m gui.app --help` lists them). tkinter is imported only here, so a "
        "plain ssh session without $DISPLAY keeps working.",
    )
    sub = parser.add_subparsers(dest="command", metavar="{" + ",".join(SUBCOMMANDS) + "}")

    p_run = sub.add_parser(
        "run",
        help="expand the matrix and run it (exit 1 if any run failed)",
        description="Read a spec, expand the matrix, submit and drive it to completion.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_run.add_argument("spec", metavar="SPEC", help="batch spec (YAML, or JSON as a fallback)")
    _add_planning_options(p_run)
    _add_execution_options(p_run)

    p_dry = sub.add_parser(
        "dry-run",
        help="print every command and landing directory; write nothing, submit nothing",
        description=(
            "Plan the whole batch and print it. Nothing is written, nothing is submitted - "
            "this is the command to run before the real one."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_dry.add_argument(
        "spec",
        metavar="SPEC",
        nargs="?",
        default="",
        help="batch spec (YAML, or JSON as a fallback)",
    )
    _add_planning_options(p_dry)
    p_dry.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="print full detail only for the first N runs (0 = all, the default)",
    )
    p_dry.add_argument(
        "--strict",
        action="store_true",
        help="exit 2 if any command could not be built (a machine criterion for "
        "'this spec is ready to run here')",
    )
    p_dry.add_argument(
        "--self-test",
        action="store_true",
        help="check the frozen interface for drift instead of planning a batch "
        "(this is what scripts/check.sh runs)",
    )

    p_resume = sub.add_parser(
        "resume",
        help="retry only the runs that did not finish",
        description=(
            "Resume a batch from its batch.json. Runs that are done stay done - their "
            "artifacts are re-verified on disk, and a verified run is never resubmitted."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_resume.add_argument("batch_dir", metavar="BATCH_DIR", help=f"directory holding {BATCH_JSON_NAME}")
    p_resume.add_argument(
        "--official-run-dir",
        default="",
        metavar="DIR",
        help="official design directory to parse site coordinates from (see `run --help`)",
    )
    _add_execution_options(p_resume)

    p_archive = sub.add_parser(
        "archive",
        help="apply the archiving rules to a finished batch",
        description=(
            "Collect the touchstone files of every finished run into the flat sparam/ area "
            "and delete the mesh intermediates. Verified first: a run whose artifacts do not "
            "pass verification loses nothing."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_archive.add_argument(
        "batch_dir", metavar="BATCH_DIR", help=f"directory holding {BATCH_JSON_NAME}"
    )
    p_archive.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be kept and removed without touching anything",
    )

    p_status = sub.add_parser(
        "status",
        help="print the state of every run (exit 1 if any run failed)",
        description=(
            "Read batch.json and print one line per run: status, wall clock, job id, "
            "artifacts, plus convergence and peak memory parsed from the logs on disk."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_status.add_argument(
        "batch_dir", metavar="BATCH_DIR", help=f"directory holding {BATCH_JSON_NAME}"
    )
    p_status.add_argument(
        "--no-logs",
        action="store_true",
        help="do not read the run logs (skips convergence and peak memory)",
    )
    return parser


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口。`ewave_batch.cli.main`、顶层 `cli.main`、`gui.frames.*.main` 共用这个签名。

    子命令：`run` / `dry-run` / `resume` / `archive` / `status`（`SUBCOMMANDS`）。
    返回进程退出码，**不 `sys.exit`**（GUI 也会调它）。
    🚨 tkinter 只许在 GUI 分支里惰性 import（CLAUDE.md 硬约束 5）——
    无 `$DISPLAY` 的纯 ssh 会话里 CLI 必须可用。
    """
    ascii_safe_stdio()
    try:
        args = build_parser().parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        # argparse 在用法错和 `--help` / `--version` 上都调 `sys.exit`。
        # 冻结面写着"返回进程退出码，不 sys.exit（GUI 也会调它）"—— 一个 GUI 里的
        # 参数笔误不该把整个界面进程带走，所以在这儿把它接回成返回值。
        return int(exc.code or 0)

    if args.gui is not None:
        # ★ 唯一允许碰 GUI 的分支：用户明确要界面。tkinter 和 gui.* 都在这几行里才 import。
        from gui.app import DEFAULT_LAYOUT, launch

        return int(launch(args.gui or DEFAULT_LAYOUT))

    if not args.command:
        build_parser().print_help()
        return EXIT_USAGE

    handler = _HANDLERS[args.command]
    try:
        return int(handler(args))
    except KeyboardInterrupt:  # pragma: no cover - 交互式打断
        print("")
        print("interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except EwaveBatchError as exc:
        # 本工具自己的异常 = 「这份输入 / 这个环境有问题」，一句话说清 + 下一步。
        # 别的异常一律让它炸出 traceback —— 那是我们的 bug，不该被吞成一句好话。
        return _error(str(exc), *_next_steps_for(exc))


if __name__ == "__main__":  # pragma: no cover - 进程入口
    sys.exit(main())
