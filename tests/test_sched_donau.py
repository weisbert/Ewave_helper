"""`sched.donau` 的测试 —— 提交 / 轮询 / 取消，全部靠注入的假 runner。

本机没有 `dsub`（CLAUDE.md 硬约束 3），所以这里测的**不是**"能不能提交"，而是三件
只要写错就会在红区静默出事的事：

1. **拼出去的 argv 逐字对不对**（`-R` 的引号、flag 重复、站点值有没有被编出来）；
2. **回显解析对不对**，尤其"认不出的时候会不会编一个答案"——
   编一个 job id 出来的后果是整批次追着一个不存在的作业轮询到超时；
   把认不出的状态归成 `running` 会让批次永远卡着，归成 `done` 会让人拿到没跑完的结果；
3. **job 的终态不许直接变成 run 的 done**（BRIEF §10 实测：eWave 崩了也 `exit=0`、
   还会留 0 字节产物、日志照样报 "done" —— 三条失败信号全不可信）。

四条防自证配方（`docs/OVERNIGHT.md`）在这份文件里的落点：

1. **关键测试** = `test_build_dsub_argv_golden`（整条 argv 等于期望值）、
   `test_submit_argv_with_user_prefix_golden`、各条 `test_parse_*`（解析结果等于期望值）；
2. **期望值来源**：全部是**手写字面量**。站点那三个值（account/queue/resources）
   照 `tests/fixtures/offdir_synthetic/remote_run_ewave.sh` 里的**假值**手抄，
   并且有一条测试（`test_fixture_still_says_what_we_hardcoded`）用一段**测试自己写的**
   朴素正则把 fixture 里的值抠出来跟手写字面量比 —— 这样 fixture 一改，
   期望值就当场红，而不是两边一起悄悄漂。
   形状（`dsub -A … -q … -R … <命令>` 的顺序）来自 `references/ewave_donau_kit/ewave/
   run_examples/remote_run_ewave.sh`（红区证据，只取形状不取值）；
   job id / 状态词的样本形状来自 kit 的 `parse_job_id` / `map_state` 所依据的格式
   （`ALPS_DONAU_NOTES` §9 的流式回显、`TOOL_FACTS` 的 `dsub --json` 信封）。
   **绝不**拿被测函数自己的输出当期望值；
3. **反向验证**：每条关键测试配一条 `_negative`，与正向共用同一个输入构造路径
   （`_plan()`），只改坏一个值（换队列 / 删 `-R` / 把 id 从回显里拿掉）；
4. **计数断言**：`-A`/`-q`/`-R`/`-EP`/`-o`/`-J` 各恰好一次（重复传会让 dsub 行为不确定）、
   `djob` 一次查全部时 runner 只被调一次、取消一个已结束的 job 时 runner 一次都不调、
   表格解析出来的条数 == 表里数出来的行数（空集合的 diff 永远好看）。

🚨 本文件零站点标识符：account / queue / 资源串 / 路径 / cell 名全是显式假值
（`fake_account` / `/tmp/...` / `TESTCELL`），一个真实取值都没有。
"""

from __future__ import annotations

import inspect
import re
import unittest
from pathlib import Path

from ewave_batch.model import (
    CommandPlan,
    Job,
    JobState,
    RunResult,
    RunStatus,
    SchedulerError,
    SchedulerProtocol,
    SiteFacts,
    Stage,
)
from ewave_batch.sched import donau

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "offdir_synthetic" / "remote_run_ewave.sh"

# --------------------------------------------------------------------------
# 手写的假值 —— 站点三元组照 offdir_synthetic/remote_run_ewave.sh 里的**假值**抄
# --------------------------------------------------------------------------

FAKE_ACCOUNT = "fake_account"
FAKE_QUEUE = "fake_queue"
FAKE_RESOURCES = "cpu=20;mem=100000"
"""合成 fixture 里的 dsub 三元组。它们是**假值**（见 `offdir_synthetic/README.md`）。

真实的账号/队列是站点身份，源码和测试里都不许出现（硬约束 1b）。这里要验的只是
"喂进去什么就该原样出现在 argv 里"，用什么假值都行 —— 但必须和 fixture 一致，
`test_fixture_still_says_what_we_hardcoded` 盯着这件事。
"""

FAKE_EWAVE_BIN = "/tmp/fakebin/ewave"
FAKE_RUN_DIR = "/tmp/ewb/runs/TESTCELL/base"
FAKE_LOG = "/tmp/ewb/runs/TESTCELL/base/run_typical_-40_0.log"

PLAN_ARGV = (
    FAKE_EWAVE_BIN,
    "--nogui",
    "--all",
    "--corner=typical",
    "--temperature=-40.0",
    f"--workDir={FAKE_RUN_DIR}",
)
"""被提交的那条命令。**这里不调 `build_ewave_plan`** —— 那是另一个模块的事，
把它拉进来就等于"用一个实现去验另一个实现"，而且它的 argv 一变本文件就会跟着变，
于是这里的 golden 就不再是人写的期望值了。"""

GOLDEN_DSUB_ARGV = [
    "dsub",
    "-A",
    FAKE_ACCOUNT,
    "-q",
    FAKE_QUEUE,
    "-R",
    FAKE_RESOURCES,
    "-EP",
    FAKE_RUN_DIR,
    "-o",
    FAKE_LOG,
    "-J",
    *PLAN_ARGV,
]
"""**手写**的期望 argv。

* `dsub -A … -q … -R … <命令>` 的形状和顺序：官方 `remote_run_ewave.sh`
  （`references/ewave_donau_kit/ewave/run_examples/`，红区证据，只取形状）；
* 三个站点值：合成 fixture 的假值（上面）；
* `-EP` / `-o` / `-J` 是我们加的机制层，理由逐条写在 `donau.build_dsub_argv` 的 docstring 里
  （节点工作目录 / 作业 stdout 落文件 / JSON 回显好解析 job id）；
* 官方那条命令里的 `-I`（阻塞）**故意没有**：driver 是单线程轮询，阻塞提交会让
  `tick()` 再也回不来。这条差异是有意的，不是抄漏了。
"""


def _plan(**overrides: object) -> CommandPlan:
    """正反两条测试共用的输入构造路径（配方 3：反向验证只许改坏一个值）。"""
    fields: dict[str, object] = {
        "argv": PLAN_ARGV,
        "cwd": FAKE_RUN_DIR,
        "log_path": FAKE_LOG,
        "stage": Stage.SOLVE,
        "run_id": "TESTCELL/base/typical_-40_0",
        "design_key": "TESTCELL",
    }
    fields.update(overrides)
    return CommandPlan(**fields)  # type: ignore[arg-type]


class ScriptedRunner:
    """满足 `model.RunnerProtocol` 的假 runner：按程序名回放预先写好的输出。

    `script` 是 `程序名 → 一条或多条回应`；多条时按调用顺序消费，用完之后重复最后一条
    （轮询会被调很多次）。每次调用都记进 `self.calls`，测试拿它做**计数断言**。
    """

    def __init__(self, script: dict[str, object] | None = None, *, explode: bool = False) -> None:
        self.script: dict[str, list[dict[str, object]]] = {}
        for program, responses in (script or {}).items():
            items = responses if isinstance(responses, list) else [responses]
            self.script[program] = [dict(item) for item in items]  # type: ignore[arg-type]
        # `{"respond": fn}` = 每次调用现算一条回应（12 个 run 要 12 个不同的 job id）
        self.calls: list[list[str]] = []
        self.explode = explode

    def run(
        self,
        argv,
        *,
        cwd=None,
        env=None,
        timeout=None,
        on_line=None,
        cancel=None,
    ) -> RunResult:
        self.calls.append(list(argv))
        if self.explode:
            raise OSError("假 runner 故意炸了")
        queue = self.script.get(argv[0], [])
        if not queue:
            spec: dict[str, object] = {"returncode": 0, "stdout": ""}
        elif len(queue) == 1:
            spec = queue[0]
        else:
            spec = queue.pop(0)
        if callable(spec.get("respond")):
            spec = dict(spec["respond"](list(argv)))  # type: ignore[operator,index]
        return RunResult(
            argv=tuple(argv),
            returncode=int(spec.get("returncode", 0)),  # type: ignore[arg-type]
            stdout=str(spec.get("stdout", "")),
            stderr=str(spec.get("stderr", "")),
            timed_out=bool(spec.get("timed_out", False)),
            cwd=cwd or "",
        )

    def programs(self) -> list[str]:
        """每次调用的程序名，按顺序 —— 计数断言用。"""
        return [call[0] for call in self.calls]


FIXED_CLOCK = "2026-08-18T00:00:00Z"


def _scheduler(runner: ScriptedRunner, **kwargs: object) -> donau.DonauScheduler:
    """站点三元组全给齐的调度器（正反两条测试共用）。"""
    params: dict[str, object] = {
        "account": FAKE_ACCOUNT,
        "queue": FAKE_QUEUE,
        "resources": FAKE_RESOURCES,
        "clock": lambda: FIXED_CLOCK,
    }
    params.update(kwargs)
    return donau.DonauScheduler(runner, **params)  # type: ignore[arg-type]


# ==========================================================================
# 1. 拼 argv
# ==========================================================================


class DsubArgvGolden(unittest.TestCase):
    """`build_dsub_argv` 的 golden：期望值手写，形状照官方 remote 提交脚本。"""

    def test_fixture_still_says_what_we_hardcoded(self) -> None:
        """手写的三个假值必须仍然等于 fixture 里的值 —— 否则期望值就漂了。

        这里**故意用一段测试自己写的朴素正则**去抠，而不是调
        `core.discover.parse_dsub_options`：拿被测系统的解析器来产出期望值，
        就是"实现方决定期望值"的那种自证。
        """
        self.assertTrue(
            FIXTURE.exists(),
            f"{FIXTURE} 不见了 —— 它是进 git 的合成 fixture，不该缺（不是平台性缺失）",
        )
        text = FIXTURE.read_text(encoding="utf-8")
        line = next(ln for ln in text.splitlines() if ln.strip().startswith("dsub"))
        self.assertEqual(re.search(r"-A\s+(\S+)", line).group(1), FAKE_ACCOUNT)
        self.assertEqual(re.search(r"-q\s+(\S+)", line).group(1), FAKE_QUEUE)
        self.assertEqual(re.search(r'-R\s+"([^"]+)"', line).group(1), FAKE_RESOURCES)
        # 计数：fixture 那一行里三个 flag 各恰好一次（形状本身也要是"每个一次"）
        self.assertEqual(len(re.findall(r"(?<!\S)-A(?!\S)", line)), 1)
        self.assertEqual(len(re.findall(r"(?<!\S)-q(?!\S)", line)), 1)
        self.assertEqual(len(re.findall(r"(?<!\S)-R(?!\S)", line)), 1)

    def test_build_dsub_argv_golden(self) -> None:
        """整条 argv 逐字等于手写的期望值。"""
        argv = donau.build_dsub_argv(
            _plan(),
            account=FAKE_ACCOUNT,
            queue=FAKE_QUEUE,
            resources=FAKE_RESOURCES,
            log_path=FAKE_LOG,
        )
        self.assertEqual(argv, GOLDEN_DSUB_ARGV)

    def test_build_dsub_argv_golden_negative(self) -> None:
        """同一条构造路径，只把队列改坏一个字 —— 必须被看见，且只差那一处。"""
        argv = donau.build_dsub_argv(
            _plan(),
            account=FAKE_ACCOUNT,
            queue="other_queue",
            resources=FAKE_RESOURCES,
            log_path=FAKE_LOG,
        )
        self.assertNotEqual(argv, GOLDEN_DSUB_ARGV, "换了队列却和 golden 一样 —— 比对根本没生效")
        differing = [i for i, (a, b) in enumerate(zip(argv, GOLDEN_DSUB_ARGV)) if a != b]
        self.assertEqual(differing, [GOLDEN_DSUB_ARGV.index("-q") + 1])
        self.assertEqual(len(argv), len(GOLDEN_DSUB_ARGV))

    def test_build_dsub_argv_missing_resources_negative(self) -> None:
        """删掉 `-R` 也必须被看见（少一个 flag 是"空得好看"的另一种形态）。"""
        argv = donau.build_dsub_argv(
            _plan(),
            account=FAKE_ACCOUNT,
            queue=FAKE_QUEUE,
            resources="",
            log_path=FAKE_LOG,
        )
        self.assertNotEqual(argv, GOLDEN_DSUB_ARGV)
        self.assertNotIn("-R", argv)
        self.assertEqual(len(argv), len(GOLDEN_DSUB_ARGV) - 2)

    def test_each_option_appears_exactly_once(self) -> None:
        """计数断言：重复传同一个 flag 会让 dsub 的行为不确定。"""
        argv = donau.build_dsub_argv(
            _plan(),
            account=FAKE_ACCOUNT,
            queue=FAKE_QUEUE,
            resources=FAKE_RESOURCES,
            log_path=FAKE_LOG,
        )
        for flag in ("-A", "-q", "-R", "-EP", "-o", "-J"):
            self.assertEqual(argv.count(flag), 1, f"{flag} 出现了 {argv.count(flag)} 次")

    def test_resource_value_carries_no_shell_quotes(self) -> None:
        """`-R` 的值在 shell 脚本里带引号，在 argv 里**不带** —— 我们直接 exec，没有 shell。"""
        argv = donau.build_dsub_argv(_plan(), resources=FAKE_RESOURCES)
        value = argv[argv.index("-R") + 1]
        self.assertEqual(value, FAKE_RESOURCES)
        self.assertNotIn('"', value)
        self.assertNotIn("'", value)

    def test_no_site_defaults_in_source(self) -> None:
        """一个参数都不给 → argv 里**没有** `-A`/`-q`/`-R`（硬约束 1b：源码零默认值）。"""
        argv = donau.build_dsub_argv(_plan())
        for flag in ("-A", "-q", "-R"):
            self.assertNotIn(flag, argv, f"{flag} 凭空出现了 —— 源码里有站点默认值？")
        self.assertEqual(argv[0], "dsub")
        self.assertEqual(argv[-len(PLAN_ARGV) :], list(PLAN_ARGV))

    def test_name_does_not_inject_an_unverified_flag(self) -> None:
        """Donau 起作业名用哪个 flag 还没实测（`-J` 已经是 `--json`）⇒ **不猜**。"""
        argv = donau.build_dsub_argv(_plan(), name="run-0001")
        self.assertNotIn("run-0001", argv)
        self.assertEqual(donau.NAME_FLAG, "")

    def test_empty_plan_argv_is_refused(self) -> None:
        """没有要跑的命令还提交，等于往队列里塞一个空作业。"""
        with self.assertRaises(SchedulerError):
            donau.build_dsub_argv(_plan(argv=()))

    def test_djob_and_dkill_argv(self) -> None:
        self.assertEqual(donau.build_djob_argv(["101", "102"]), ["djob", "101", "102"])
        self.assertEqual(donau.build_dkill_argv("101"), ["dkill", "101"])
        self.assertEqual(donau.build_dpeek_argv("101"), ["dpeek", "101"])
        with self.assertRaises(SchedulerError):
            donau.build_djob_argv([])
        with self.assertRaises(SchedulerError):
            donau.build_dkill_argv("")


# ==========================================================================
# 2. 用户改整条 dsub 命令
# ==========================================================================


class UserEditablePrefix(unittest.TestCase):
    """用户 2026-08-18 要求：整条 dsub 命令原样暴露给人改；`cpu=` 要能读回去同步 `--parallel`。"""

    EDITED = 'dsub -A other_acct -q other_q -R "cpu=8;mem=64000"'

    def test_parse_dsub_prefix(self) -> None:
        self.assertEqual(
            donau.parse_dsub_prefix(self.EDITED),
            ["dsub", "-A", "other_acct", "-q", "other_q", "-R", "cpu=8;mem=64000"],
        )

    def test_parse_dsub_prefix_rejects_blocking_flag(self) -> None:
        """生产脚本正是 `-I`，粘过来最自然 —— 但它会让轮询循环再也回不来。"""
        with self.assertRaises(SchedulerError) as caught:
            donau.parse_dsub_prefix(self.EDITED + " -I")
        self.assertIn("-I", str(caught.exception))

    def test_parse_dsub_prefix_rejects_shell_metachars(self) -> None:
        with self.assertRaises(SchedulerError):
            donau.parse_dsub_prefix(self.EDITED + " ./x.sh 2>&1 |tee x.log")

    def test_metachar_filter_does_not_eat_the_resource_semicolon(self) -> None:
        """★ 过滤器测试：`;` 是 shell 的分隔符，**也是 `-R` 的合法内容**。

        把它一起拉黑就会误伤每一条正常命令 —— 和 MVP 那次 `--sparam` 前缀误伤
        `--sparamImpedance` 同一类错（过滤器多吃一口，症状是"看起来很干净"）。
        这条测试就是那个 bug 的回归测试；它在写这份文件时**真的红过一次**。
        """
        tokens = donau.parse_dsub_prefix('dsub -R "cpu=8;mem=64000"')
        self.assertEqual(tokens[-1], "cpu=8;mem=64000")
        self.assertNotIn(";", donau.SHELL_METACHARS)
        # 不带引号的写法也要能过（我们直接 exec，不经过 shell）
        self.assertEqual(donau.parse_dsub_prefix("dsub -R cpu=8;mem=64000")[-1], "cpu=8;mem=64000")

    def test_parse_dsub_prefix_rejects_non_dsub(self) -> None:
        with self.assertRaises(SchedulerError):
            donau.parse_dsub_prefix("sh ./remote_submit.sh")

    def test_submit_argv_with_user_prefix_golden(self) -> None:
        """用户改过的前缀**逐字**用，机制层三个补在后面 —— 期望值手写。"""
        prefix = donau.parse_dsub_prefix(self.EDITED)
        sched = _scheduler(ScriptedRunner(), submit_prefix=prefix)
        argv = sched.submit_argv(_plan())
        self.assertEqual(
            argv,
            [
                "dsub",
                "-A",
                "other_acct",
                "-q",
                "other_q",
                "-R",
                "cpu=8;mem=64000",
                "-EP",
                FAKE_RUN_DIR,
                "-o",
                FAKE_LOG,
                "-J",
                *PLAN_ARGV,
            ],
        )
        for flag in ("-A", "-q", "-R", "-EP", "-o", "-J"):
            self.assertEqual(argv.count(flag), 1)

    def test_submit_argv_with_user_prefix_negative(self) -> None:
        """把用户前缀里的队列改坏 → 提交出去的 argv 必须跟着变（前缀不是被忽略的）。"""
        prefix = donau.parse_dsub_prefix(self.EDITED.replace("other_q", "wrong_q"))
        sched = _scheduler(ScriptedRunner(), submit_prefix=prefix)
        argv = sched.submit_argv(_plan())
        self.assertIn("wrong_q", argv)
        self.assertNotIn("other_q", argv)
        self.assertNotIn(FAKE_QUEUE, argv, "构造参数里的队列不该盖过用户改的那条命令")

    def test_user_prefix_mechanism_flags_not_duplicated(self) -> None:
        """用户自己写了 `-o` → 我们不再补一个（同名 flag 传两次行为不确定）。"""
        prefix = donau.parse_dsub_prefix(self.EDITED + " -o /tmp/mine.log")
        sched = _scheduler(ScriptedRunner(), submit_prefix=prefix)
        argv = sched.submit_argv(_plan())
        self.assertEqual(argv.count("-o"), 1)
        self.assertEqual(argv[argv.index("-o") + 1], "/tmp/mine.log")

    def test_cpu_syncs_from_edited_command(self) -> None:
        """`--parallel` 的档要跟着用户改过的 `cpu=` 走（换算委托给 core.cmd）。"""
        prefix = donau.parse_dsub_prefix(self.EDITED)
        sched = _scheduler(ScriptedRunner(), submit_prefix=prefix)
        self.assertEqual(sched.effective_resources(), "cpu=8;mem=64000")
        self.assertEqual(donau.cpu_from_resources(sched.effective_resources()), 8)

    def test_cpu_syncs_from_edited_command_negative(self) -> None:
        """反向：不读用户改过的那条命令，就会拿到构造参数里的 20 —— 那正是要防的。"""
        prefix = donau.parse_dsub_prefix(self.EDITED)
        sched = _scheduler(ScriptedRunner(), submit_prefix=prefix)
        self.assertNotEqual(sched.effective_resources(), FAKE_RESOURCES)
        self.assertNotEqual(donau.cpu_from_resources(sched.effective_resources()), 20)

    def test_cpu_from_resources_unparsable_is_none(self) -> None:
        """认不出返回 None，**不许拿 1 或 0 冒充** —— 那会让"没解析到"和"真的要 1 核"一样。"""
        self.assertIsNone(donau.cpu_from_resources(""))
        self.assertIsNone(donau.cpu_from_resources("mem=100"))
        self.assertIsNone(donau.cpu_from_resources("cpu=many"))

    def test_resources_from_dsub_argv_forms(self) -> None:
        self.assertEqual(donau.resources_from_dsub_argv(["dsub", "-R", "cpu=4"]), "cpu=4")
        self.assertEqual(donau.resources_from_dsub_argv(["dsub", "--resource=cpu=4"]), "cpu=4")
        self.assertEqual(donau.resources_from_dsub_argv(["dsub", "-q", "x"]), "")

    def test_prefix_without_resources_does_not_fall_back(self) -> None:
        """用户把 `-R` 删了 → 就是"这次没指定"，**不许**回退到构造时那份。

        回退看着周到，实际是撒谎：dsub 会用它自己的默认资源，而我们会把一个没送出去的
        资源串记进 `Job.resources`，还拿它的 `cpu=` 去定 `--parallel` 的档。
        """
        prefix = donau.parse_dsub_prefix("dsub -A other_acct -q other_q")
        sched = _scheduler(ScriptedRunner(), submit_prefix=prefix)
        self.assertEqual(sched.effective_resources(), "")
        self.assertIsNone(donau.cpu_from_resources(sched.effective_resources()))

    def test_job_records_what_was_actually_sent(self) -> None:
        """`batch.json` 是事后追溯的唯一依据 —— 记的必须是真送出去的那三个值。"""
        prefix = donau.parse_dsub_prefix(self.EDITED)
        runner = ScriptedRunner({"dsub": {"stdout": JSON_OK}})
        job = _scheduler(runner, submit_prefix=prefix).submit(_plan())
        self.assertEqual(job.account, "other_acct")
        self.assertEqual(job.queue, "other_q")
        self.assertEqual(job.resources, "cpu=8;mem=64000")
        self.assertNotEqual(job.account, FAKE_ACCOUNT, "构造参数不该盖过用户改的那条命令")

    def test_format_dsub_command_quotes_the_resource_string(self) -> None:
        """`;` 在 shell 里是命令分隔符 —— 展示给人贴回去的那一行必须带引号。"""
        text = donau.format_dsub_command(["dsub", "-R", FAKE_RESOURCES])
        self.assertIn(FAKE_RESOURCES, text)
        self.assertNotEqual(text, "dsub -R " + FAKE_RESOURCES)


# ==========================================================================
# 3. 解析提交回显
# ==========================================================================


class SubmitOutputParsing(unittest.TestCase):
    """`parse_dsub_submit_output` —— 四种形状 + 认不出必须报错。

    样本形状来自 kit：`TOOL_FACTS`（`dsub --json` 回 `{"data":{"jobId":…}}`，
    "numeric-only; ignore requestId"）和 `ALPS_DONAU_NOTES` §9 的流式回显
    （`JOBID <数字>` / `Submit job successfully`）。**数字是这里编的**。
    """

    def test_json_envelope(self) -> None:
        text = '{"code":"success","requestId":"777888999","data":{"jobId":"10000002"}}'
        self.assertEqual(donau.parse_dsub_submit_output(text), "10000002")

    def test_json_envelope_ignores_request_id_negative(self) -> None:
        """`requestId` 不是 job id。拿它去轮询 = 追一个不存在的作业到超时。"""
        text = '{"code":"success","requestId":"777888999","data":{"jobId":"10000002"}}'
        self.assertNotEqual(donau.parse_dsub_submit_output(text), "777888999")

    def test_json_bare_id(self) -> None:
        self.assertEqual(donau.parse_dsub_submit_output('{"id": 10000001}'), "10000001")

    def test_streamed_banner(self) -> None:
        text = "Submit job successfully\nJOBID 10000001\nPENDING\n"
        self.assertEqual(donau.parse_dsub_submit_output(text), "10000001")

    def test_table_with_header(self) -> None:
        text = "JOBID      STATE      QUEUE\n10000001   PENDING    some_queue\n"
        self.assertEqual(donau.parse_dsub_submit_output(text), "10000001")

    def test_lowercase_key_form(self) -> None:
        self.assertEqual(donau.parse_dsub_submit_output("jobId: 372"), "372")

    def test_lsf_angle_bracket_form(self) -> None:
        """`Job <10000001> is submitted to queue <…>.` —— bsub 的经典句式。

        Donau 是 bsub 味儿的公司 fork（kit §2），而这句里**没有 "id" 三个字母** ——
        只按 `jobid` 找的解析器会整条漏掉，然后 run 挂成僵尸。
        """
        text = "Job <10000001> is submitted to queue <some_queue>.\n"
        self.assertEqual(donau.parse_dsub_submit_output(text), "10000001")

    def test_empty_output_raises(self) -> None:
        """空输出 + `exit=0` 是这个项目最熟悉的骗局 —— 必须当场失败。"""
        with self.assertRaises(SchedulerError):
            donau.parse_dsub_submit_output("")
        with self.assertRaises(SchedulerError):
            donau.parse_dsub_submit_output("   \n  \n")

    def test_unrecognised_output_raises_negative(self) -> None:
        """★ 反向验证：喂一段**不含** job id 的回显，必须报错而不是返回一个假 id。

        返回假 id 的后果：整批次追着一个不存在的作业轮询到超时，
        而每一步看起来都很正常（提交"成功"了、状态"查不到"、最后"超时"）。
        """
        text = "Error: invalid option or param. Unexpected argument.\n"
        with self.assertRaises(SchedulerError) as caught:
            donau.parse_dsub_submit_output(text)
        message = str(caught.exception)
        self.assertIn("Error: invalid option", message, "报错里要带上原文，人才知道 dsub 说了啥")

    def test_no_digits_anywhere_raises(self) -> None:
        with self.assertRaises(SchedulerError):
            donau.parse_dsub_submit_output("submitted.")

    def test_digits_without_job_id_marker_raises(self) -> None:
        """光有数字不算 —— 没有 `jobid` 标记就不许把随便一个数字当 id。"""
        with self.assertRaises(SchedulerError):
            donau.parse_dsub_submit_output("Queue depth: 10000001 tasks waiting\n")


# ==========================================================================
# 4. 解析查询回显 + 状态映射
# ==========================================================================


class DjobOutputParsing(unittest.TestCase):
    """`parse_djob_output` —— 表格 / 详情块 / JSON / 空输出 / 认不出的状态。"""

    TABLE = (
        "JOBID      USER       STAT   QUEUE\n"
        "10000001   someuser   RUN    some_queue\n"
        "10000003   someuser   PEND   some_queue\n"
        "10000004   someuser   DONE   some_queue\n"
    )

    def test_table_with_header(self) -> None:
        states = donau.parse_djob_output(self.TABLE)
        self.assertEqual(
            states,
            {
                "10000001": JobState.RUNNING,
                "10000003": JobState.PENDING,
                "10000004": JobState.DONE,
            },
        )

    def test_table_row_count(self) -> None:
        """计数断言：解析出来的条数 == 表里数出来的数据行数（空集合的 diff 永远好看）。"""
        data_rows = [
            line for line in self.TABLE.splitlines()[1:] if line.strip()
        ]  # 表头之后的非空行
        self.assertEqual(len(data_rows), 3)
        self.assertEqual(len(donau.parse_djob_output(self.TABLE)), len(data_rows))

    def test_table_negative(self) -> None:
        """把一行的状态改坏（`RUN` → 一个没人认识的词）→ 必须变成 UNKNOWN，不许还报 running。"""
        broken = self.TABLE.replace("RUN    ", "FROBNI ")
        states = donau.parse_djob_output(broken)
        self.assertEqual(states["10000001"], JobState.UNKNOWN)
        self.assertNotEqual(states["10000001"], JobState.RUNNING)
        # 其余两行不受影响 —— 坏一格不该把整张表吃掉
        self.assertEqual(states["10000003"], JobState.PENDING)
        self.assertEqual(len(states), 3)

    def test_single_job_detail_block(self) -> None:
        text = "Job Id: 10000001\nUser: someuser\nState: RUNNING\nQueue: some_queue\n"
        self.assertEqual(donau.parse_djob_output(text), {"10000001": JobState.RUNNING})

    def test_json_form(self) -> None:
        text = '{"code":"success","data":{"jobs":[{"jobId":"101","state":"RUNNING"},'
        text += '{"jobId":"102","status":"PENDING"}]}}'
        self.assertEqual(
            donau.parse_djob_output(text),
            {"101": JobState.RUNNING, "102": JobState.PENDING},
        )

    def test_empty_output_is_empty_dict_not_error(self) -> None:
        """调度器短暂没回话是常事 —— 空 dict，不抛异常，也不凭空判失败。"""
        self.assertEqual(donau.parse_djob_output(""), {})
        self.assertEqual(donau.parse_djob_output("\n  \n"), {})

    def test_unrecognised_format_yields_nothing_not_a_guess(self) -> None:
        self.assertEqual(donau.parse_djob_output("djob: command not found\n"), {})

    def test_loose_line_form(self) -> None:
        text = "10000001 RUNNING on node-a\n10000003 PENDING\n"
        self.assertEqual(
            donau.parse_djob_output(text),
            {"10000001": JobState.RUNNING, "10000003": JobState.PENDING},
        )

    def test_stats_line_is_not_mistaken_for_a_job(self) -> None:
        """`2 jobs found` 也是"行首数字"—— 不许被记成一个 id 为 2 的作业。"""
        self.assertEqual(donau.parse_djob_output("2 jobs found\n"), {})


class StateMapping(unittest.TestCase):
    """`map_job_state` —— 过滤器测试：认不出的**必须**是显式 UNKNOWN。"""

    def test_known_tokens(self) -> None:
        cases = {
            "PENDING": JobState.PENDING,
            "PEND": JobState.PENDING,
            "RUNNING": JobState.RUNNING,
            "RUN": JobState.RUNNING,
            "DONE": JobState.DONE,
            "FAILED": JobState.FAILED,
            "KILLED": JobState.CANCELLED,
        }
        for token, expected in cases.items():
            with self.subTest(token=token):
                self.assertEqual(donau.map_job_state(token), expected)
        self.assertEqual(len(cases), 7)

    def test_unknown_token_is_unknown_negative(self) -> None:
        """★ 不许静默归成某个已知态。

        静默归成 `running` → 批次永远卡着；静默归成 `done` → 人拿到没跑完的结果。
        """
        for token in ("FROBNICATED", "ZZZ", "state: mysterious", "?"):
            with self.subTest(token=token):
                state = donau.map_job_state(token)
                self.assertEqual(state, JobState.UNKNOWN)
                self.assertNotEqual(state, JobState.RUNNING)
                self.assertNotEqual(state, JobState.DONE)
                self.assertNotEqual(state, JobState.FAILED)

    def test_empty_is_unknown(self) -> None:
        self.assertEqual(donau.map_job_state(""), JobState.UNKNOWN)

    def test_exit_code_zero_is_done_not_failed(self) -> None:
        """kit 已经修过的坑：`Exit: 0` 是干净结束，不是 LSF 的状态词 `EXIT`。

        读反了就会把一次成功的 run 报成失败，而人只会看到"明明跑完了工具却说失败"。
        """
        for text in ("Exit: 0", "exited 0", "exit code 0"):
            with self.subTest(text=text):
                self.assertEqual(donau.map_job_state(text), JobState.DONE)

    def test_exit_code_nonzero_is_failed(self) -> None:
        self.assertEqual(donau.map_job_state("Exit: 137"), JobState.FAILED)

    def test_bare_exit_word_is_failed(self) -> None:
        """裸的 LSF 状态词 `EXIT`（没有码）仍然是失败 —— 上一条不许把它一起吃掉。"""
        self.assertEqual(donau.map_job_state("EXIT"), JobState.FAILED)

    def test_state_field_wins_over_stray_words(self) -> None:
        text = "Job Id: 1\nState: PENDING\nExit Code: -\nComment: previous run failed\n"
        self.assertEqual(donau.map_job_state(text), JobState.PENDING)

    def test_is_terminal(self) -> None:
        self.assertTrue(donau.is_terminal(JobState.DONE))
        self.assertTrue(donau.is_terminal(JobState.FAILED))
        self.assertTrue(donau.is_terminal(JobState.CANCELLED))
        self.assertFalse(donau.is_terminal(JobState.PENDING))
        self.assertFalse(donau.is_terminal(JobState.RUNNING))
        self.assertFalse(donau.is_terminal(JobState.UNKNOWN))


class JobStateToRunStatus(unittest.TestCase):
    """★ 验收契约：job 说 done ≠ run 说 done。"""

    def test_queued_and_running_map(self) -> None:
        self.assertEqual(donau.run_status_for_job_state(JobState.PENDING), RunStatus.PENDING)
        self.assertEqual(donau.run_status_for_job_state(JobState.RUNNING), RunStatus.RUNNING)

    def test_job_done_does_not_become_run_done_negative(self) -> None:
        """实测三条（BRIEF §10）：eWave 崩了也 `exit=0`、崩了还留 0 字节产物、日志照样报 "done"。

        ⇒ 终态**不许**由 job 状态直接决定 run 状态，必须过
        `core.layout.verify_run_outputs`。这条测试就是那个契约的守卫。
        """
        self.assertIsNone(donau.run_status_for_job_state(JobState.DONE))
        self.assertNotEqual(donau.run_status_for_job_state(JobState.DONE), RunStatus.DONE)

    def test_failed_and_cancelled_also_need_a_decision(self) -> None:
        self.assertIsNone(donau.run_status_for_job_state(JobState.FAILED))
        self.assertIsNone(donau.run_status_for_job_state(JobState.CANCELLED))

    def test_unknown_holds(self) -> None:
        """查不到 ≠ 失败。凭空判失败会让人白跑一遍。"""
        self.assertIsNone(donau.run_status_for_job_state(JobState.UNKNOWN))


# ==========================================================================
# 5. 调度器：提交 / 轮询 / 取消
# ==========================================================================

JSON_OK = '{"code":"success","data":{"jobId":"10000002"}}'


class SchedulerSubmit(unittest.TestCase):
    def test_submit_happy_path(self) -> None:
        runner = ScriptedRunner({"dsub": {"stdout": JSON_OK}})
        job = _scheduler(runner).submit(_plan(), name="run-0001")
        self.assertEqual(job.job_id, "10000002")
        self.assertEqual(job.state, JobState.PENDING)
        self.assertEqual(job.scheduler, "donau")
        self.assertEqual(job.name, "run-0001")
        self.assertEqual(job.submitted_at, FIXED_CLOCK)
        self.assertEqual(job.account, FAKE_ACCOUNT)
        self.assertEqual(job.queue, FAKE_QUEUE)
        self.assertEqual(job.resources, FAKE_RESOURCES)
        self.assertEqual(job.stdout_path, FAKE_LOG)

    def test_submit_executes_the_golden_argv(self) -> None:
        """提交出去的那条命令就是 golden —— 拼命令和提交之间不许再有一层加工。"""
        runner = ScriptedRunner({"dsub": {"stdout": JSON_OK}})
        _scheduler(runner).submit(_plan())
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0], GOLDEN_DSUB_ARGV)

    def test_submit_executes_the_golden_argv_negative(self) -> None:
        """反向：换一个队列构造，提交出去的 argv 就不该再等于 golden。"""
        runner = ScriptedRunner({"dsub": {"stdout": JSON_OK}})
        _scheduler(runner, queue="other_queue").submit(_plan())
        self.assertNotEqual(runner.calls[0], GOLDEN_DSUB_ARGV)

    def test_submit_nonzero_returncode_raises(self) -> None:
        runner = ScriptedRunner({"dsub": {"returncode": 1, "stderr": "Error: bad queue"}})
        with self.assertRaises(SchedulerError) as caught:
            _scheduler(runner).submit(_plan())
        self.assertIn("Error: bad queue", str(caught.exception))

    def test_submit_exit_zero_without_job_id_raises_negative(self) -> None:
        """★ `exit=0` 但抠不出 job id → 照样失败。

        这个项目已经证明过 `exit=0` 不可信（BRIEF §10：eWave 崩了也返 0）。
        没有 job id 的 run 会永远轮询不到、挂成僵尸 —— 宁可当场红。
        """
        runner = ScriptedRunner({"dsub": {"returncode": 0, "stdout": "Submitted.\n"}})
        with self.assertRaises(SchedulerError):
            _scheduler(runner).submit(_plan())

    def test_submit_reads_job_id_from_stderr_too(self) -> None:
        runner = ScriptedRunner({"dsub": {"stdout": "", "stderr": "JOBID 10000001\n"}})
        self.assertEqual(_scheduler(runner).submit(_plan()).job_id, "10000001")

    def test_submit_timeout_raises(self) -> None:
        runner = ScriptedRunner({"dsub": {"timed_out": True}})
        with self.assertRaises(SchedulerError) as caught:
            _scheduler(runner).submit(_plan())
        self.assertIn("djob", str(caught.exception), "超时要提醒人先查一眼再 resume，别重复提交")

    def test_submit_without_any_site_facts_refuses(self) -> None:
        """账号/队列/资源全空 = 坐标没解析到。宁可不提交，也不提交一条没有账号的作业。"""
        runner = ScriptedRunner({"dsub": {"stdout": JSON_OK}})
        sched = donau.DonauScheduler(runner)
        with self.assertRaises(SchedulerError):
            sched.submit(_plan())
        self.assertEqual(runner.calls, [], "拒绝之前不许真的跑出去一条命令")

    def test_from_site_facts_takes_the_triple(self) -> None:
        """站点三元组只能从运行时解析来（硬约束 1b）。"""
        facts = SiteFacts(
            dsub_account=FAKE_ACCOUNT, dsub_queue=FAKE_QUEUE, dsub_resources=FAKE_RESOURCES
        )
        sched = donau.DonauScheduler.from_site_facts(facts, ScriptedRunner())
        self.assertEqual(sched.dsub_prefix(), ["dsub", "-A", FAKE_ACCOUNT, "-q", FAKE_QUEUE, "-R", FAKE_RESOURCES])

    def test_command_line_is_pasteable(self) -> None:
        sched = _scheduler(ScriptedRunner())
        text = sched.command_line(_plan())
        self.assertTrue(text.startswith("dsub "))
        self.assertIn(FAKE_ACCOUNT, text)


class SchedulerPoll(unittest.TestCase):
    TABLE = "JOBID     STAT\n101       RUN\n102       PEND\n"

    def _jobs(self) -> list[Job]:
        return [
            Job(job_id="101", scheduler="donau", state=JobState.PENDING, submitted_at="t0"),
            Job(job_id="102", scheduler="donau", state=JobState.PENDING, submitted_at="t0"),
            Job(job_id="103", scheduler="donau", state=JobState.RUNNING, submitted_at="t0"),
        ]

    def test_poll_queries_all_jobs_in_one_command(self) -> None:
        """协议要求：一次 `djob` 查全部，别一个 job 一条命令。"""
        runner = ScriptedRunner({"djob": {"stdout": self.TABLE}})
        _scheduler(runner).poll(self._jobs())
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0], ["djob", "101", "102", "103"])

    def test_poll_maps_states(self) -> None:
        runner = ScriptedRunner({"djob": {"stdout": self.TABLE}})
        updated = _scheduler(runner).poll(self._jobs())
        self.assertEqual(updated["101"].state, JobState.RUNNING)
        self.assertEqual(updated["102"].state, JobState.PENDING)
        self.assertEqual(len(updated), 3)

    def test_poll_missing_job_is_unknown_not_failed_negative(self) -> None:
        """★ 查不到的 job 不许被判成失败 —— 调度器短暂查不到是常事。"""
        runner = ScriptedRunner({"djob": {"stdout": self.TABLE}})
        updated = _scheduler(runner).poll(self._jobs())
        self.assertEqual(updated["103"].state, JobState.UNKNOWN)
        self.assertNotEqual(updated["103"].state, JobState.FAILED)
        self.assertEqual(updated["103"].submitted_at, "t0", "其余字段要原样保住")

    def test_poll_does_not_mutate_input_jobs(self) -> None:
        runner = ScriptedRunner({"djob": {"stdout": self.TABLE}})
        jobs = self._jobs()
        _scheduler(runner).poll(jobs)
        self.assertEqual(jobs[0].state, JobState.PENDING, "poll 不该就地改调用方的 Job")

    def test_poll_sets_timestamps_once(self) -> None:
        runner = ScriptedRunner({"djob": {"stdout": "JOBID  STAT\n101  RUN\n"}})
        sched = _scheduler(runner)
        first = sched.poll([Job(job_id="101", state=JobState.PENDING)])["101"]
        self.assertEqual(first.started_at, FIXED_CLOCK)
        again = sched.poll([dataclasses_replace(first, started_at="earlier")])["101"]
        self.assertEqual(again.started_at, "earlier", "已经有开始时间就不该被覆盖")

    def test_poll_falls_back_to_one_query_per_job(self) -> None:
        """`djob` 支不支持多 id 没实测到 —— 失败一次就退回逐个查（卡死比多起进程贵）。"""
        runner = ScriptedRunner(
            {
                "djob": [
                    {"returncode": 1, "stderr": "djob: too many arguments"},
                    {"stdout": "101 RUN\n"},
                    {"stdout": "102 PEND\n"},
                    {"stdout": "103 DONE\n"},
                ]
            }
        )
        sched = _scheduler(runner)
        updated = sched.poll(self._jobs())
        self.assertEqual(len(runner.calls), 4, "1 次多 id + 3 次逐个")
        self.assertEqual(updated["101"].state, JobState.RUNNING)
        self.assertEqual(updated["103"].state, JobState.DONE)

    def test_poll_remembers_the_fallback(self) -> None:
        """退回过一次就别再试多 id —— 否则每一拍都白花一次进程。"""
        runner = ScriptedRunner(
            {"djob": [{"returncode": 1, "stderr": "nope"}, {"stdout": "101 RUN\n"}]}
        )
        sched = _scheduler(runner)
        sched.poll(self._jobs())
        before = len(runner.calls)
        sched.poll(self._jobs())
        self.assertEqual(len(runner.calls) - before, 3, "第二拍只该有 3 次逐个查询")

    def test_poll_ignores_jobs_without_id(self) -> None:
        """没有 job_id 的 job 没法查，也没法当键 —— 不出现在返回里，也不白花一次进程。"""
        runner = ScriptedRunner({"djob": {"stdout": self.TABLE}})
        updated = _scheduler(runner).poll([Job(job_id="", state=JobState.UNKNOWN)])
        self.assertEqual(updated, {})
        self.assertEqual(runner.calls, [])


class SchedulerCancel(unittest.TestCase):
    def test_cancel_running_job(self) -> None:
        runner = ScriptedRunner({"dkill": {"returncode": 0, "stdout": "ok"}})
        job = Job(job_id="101", state=JobState.RUNNING)
        self.assertTrue(_scheduler(runner).cancel(job))
        self.assertEqual(runner.calls, [["dkill", "101"]])
        self.assertEqual(job.state, JobState.CANCELLED)
        self.assertEqual(job.ended_at, FIXED_CLOCK)

    def test_cancel_terminal_job_returns_false_without_running_anything(self) -> None:
        """计数断言：已经结束的 job 不该再花一次进程去杀。"""
        runner = ScriptedRunner({"dkill": {"returncode": 0}})
        job = Job(job_id="101", state=JobState.DONE)
        self.assertFalse(_scheduler(runner).cancel(job))
        self.assertEqual(runner.calls, [])

    def test_cancel_failure_returns_false_and_leaves_a_trace(self) -> None:
        """签名只能返回 bool —— 失败原因写进 `job.raw`，别让"没取消掉"毫无痕迹。"""
        runner = ScriptedRunner({"dkill": {"returncode": 1, "stderr": "no such job"}})
        job = Job(job_id="101", state=JobState.RUNNING)
        self.assertFalse(_scheduler(runner).cancel(job))
        self.assertIn("no such job", job.raw)
        self.assertEqual(job.state, JobState.RUNNING, "杀失败了就不该改状态")

    def test_cancel_job_without_id(self) -> None:
        runner = ScriptedRunner()
        self.assertFalse(_scheduler(runner).cancel(Job(job_id="")))
        self.assertEqual(runner.calls, [])


class SchedulerPeek(unittest.TestCase):
    def test_peek_returns_the_tail(self) -> None:
        runner = ScriptedRunner({"dpeek": {"stdout": "a\nb\nc\nd\n"}})
        self.assertEqual(_scheduler(runner).peek(Job(job_id="101"), lines=2), "c\nd")

    def test_peek_never_raises(self) -> None:
        """诊断用的东西不许把主流程带崩 —— driver 是在"已经失败了"的路径上调它的。"""
        self.assertEqual(_scheduler(ScriptedRunner(explode=True)).peek(Job(job_id="101")), "")

    def test_peek_without_job_id(self) -> None:
        runner = ScriptedRunner()
        self.assertEqual(_scheduler(runner).peek(Job(job_id="")), "")
        self.assertEqual(runner.calls, [])


class TwelveRunBatchThroughTheScheduler(unittest.TestCase):
    """12 个 run 走一遍提交 → 轮询 → 终态，全在调度器这一层（driver 的假批次在别处）。

    这里要盯的是"批量"才会暴露的三件事：
    * 12 次提交拿到 12 个**不同**的 job id（拿错一个就会有两个 run 追同一个作业）；
    * 12 个 job 只花**一次** `djob`（协议要求；一个 job 一条命令在 12 个格子上就是 12 倍开销）；
    * 终态出来之后 `run_status_for_job_state` **一个都不给 `RunStatus.DONE`** ——
      产物验收才是 done 的判据（BRIEF §10）。
    """

    COUNT = 12

    def _plans(self) -> list[CommandPlan]:
        plans = []
        for index in range(self.COUNT):
            run_dir = f"/tmp/ewb/runs/TESTCELL/axis{index:02d}"
            plans.append(
                _plan(
                    argv=(FAKE_EWAVE_BIN, "--nogui", "--all", f"--workDir={run_dir}"),
                    cwd=run_dir,
                    log_path=f"{run_dir}/run.log",
                    run_id=f"TESTCELL/axis{index:02d}/typical_-40_0",
                )
            )
        return plans

    def test_submit_poll_finish(self) -> None:
        counter = {"n": 1000}

        def dsub_response(_argv: list[str]) -> dict[str, object]:
            counter["n"] += 1
            return {"stdout": '{"code":"success","data":{"jobId":"%d"}}' % counter["n"]}

        runner = ScriptedRunner({"dsub": {"respond": dsub_response}})
        sched = _scheduler(runner)
        plans = self._plans()
        jobs = [sched.submit(plan, name=plan.run_id) for plan in plans]

        self.assertEqual(len(jobs), self.COUNT)
        self.assertEqual(len({job.job_id for job in jobs}), self.COUNT, "12 个 run 必须拿到 12 个不同的 id")
        self.assertEqual(len(runner.calls), self.COUNT)
        self.assertEqual(runner.programs(), ["dsub"] * self.COUNT)
        # 每条提交都带着自己那个 run 的 workDir / -EP / -o —— 不许串台
        for plan, call in zip(plans, runner.calls):
            self.assertEqual(call[call.index("-EP") + 1], plan.cwd)
            self.assertEqual(call[call.index("-o") + 1], plan.log_path)
            self.assertIn(f"--workDir={plan.cwd}", call)

        # 一拍：前 6 个跑起来了，后 6 个还在排队
        table = ["JOBID    STAT"]
        for index, job in enumerate(jobs):
            table.append(f"{job.job_id}   {'RUN' if index < 6 else 'PEND'}")
        runner.script["djob"] = [{"stdout": "\n".join(table) + "\n"}]
        before = len(runner.calls)
        polled = sched.poll(jobs)
        self.assertEqual(len(runner.calls) - before, 1, "12 个 job 只该花一次 djob")
        states = [polled[job.job_id].state for job in jobs]
        self.assertEqual(states.count(JobState.RUNNING), 6)
        self.assertEqual(states.count(JobState.PENDING), 6)

        # 再一拍：11 个结束、1 个失败、外加一个查不到的
        table = ["JOBID    STAT"]
        for index, job in enumerate(jobs[:-1]):
            table.append(f"{job.job_id}   {'EXIT' if index == 0 else 'DONE'}")
        runner.script["djob"] = [{"stdout": "\n".join(table) + "\n"}]
        final = sched.poll([polled[job.job_id] for job in jobs])
        outcome = [final[job.job_id].state for job in jobs]
        self.assertEqual(outcome.count(JobState.DONE), 10)
        self.assertEqual(outcome.count(JobState.FAILED), 1)
        self.assertEqual(outcome.count(JobState.UNKNOWN), 1, "查不到的那个要显形，不许被判成失败")
        self.assertEqual(len(outcome), self.COUNT)

        # ★ 终态一个都不许直接变成 run 的 done
        for job in final.values():
            self.assertIsNone(donau.run_status_for_job_state(job.state))

    def test_submit_stops_at_the_first_bad_reply_negative(self) -> None:
        """批量提交时，第 3 条回显没有 job id → 当场炸，而不是给这个 run 一个假 id。"""
        replies = [
            {"stdout": '{"data":{"jobId":"1001"}}'},
            {"stdout": '{"data":{"jobId":"1002"}}'},
            {"stdout": "Submitted.\n"},
        ]
        runner = ScriptedRunner({"dsub": replies})
        sched = _scheduler(runner)
        plans = self._plans()
        self.assertEqual(sched.submit(plans[0]).job_id, "1001")
        self.assertEqual(sched.submit(plans[1]).job_id, "1002")
        with self.assertRaises(SchedulerError):
            sched.submit(plans[2])


def _param_shape(func: object) -> list[tuple[str, str, bool]]:
    """(参数名, 种类, 有没有默认值) —— 只比这三样，不比注解。

    和 `ewave_batch.__main__.normalize_signature` 同一个判据：注解写法不同
    （`Sequence[str]` vs `list[str]`）不改调用方式，比了只会有假阳性。
    """
    return [
        (p.name, str(p.kind), p.default is not inspect.Parameter.empty)
        for p in inspect.signature(func).parameters.values()  # type: ignore[arg-type]
        if p.name != "self"
    ]


class ProtocolConformance(unittest.TestCase):
    """`DonauScheduler` 必须满足 `model.SchedulerProtocol`（`check.sh` 第 4 步也在查）。"""

    def test_isinstance(self) -> None:
        self.assertIsInstance(_scheduler(ScriptedRunner()), SchedulerProtocol)

    def test_method_signatures_match(self) -> None:
        """`@runtime_checkable` 的 isinstance 只看方法名在不在，挡不住参数漂移。"""
        for name in ("submit", "poll", "cancel"):
            with self.subTest(method=name):
                self.assertEqual(
                    _param_shape(getattr(donau.DonauScheduler, name)),
                    _param_shape(getattr(SchedulerProtocol, name)),
                )

    def test_scripted_runner_matches_runner_protocol(self) -> None:
        """假 runner 自己也要照冻结签名写 —— 否则这份测试是在验一个不存在的接口。"""
        from ewave_batch.model import RunnerProtocol

        self.assertEqual(_param_shape(ScriptedRunner.run), _param_shape(RunnerProtocol.run))


def dataclasses_replace(job: Job, **changes: object) -> Job:
    """`dataclasses.replace` 的小门面（测试里只用这一处，省一个 import 名字冲突）。"""
    import dataclasses

    return dataclasses.replace(job, **changes)  # type: ignore[arg-type]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
