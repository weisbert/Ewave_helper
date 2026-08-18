"""`ewave_batch.sched.driver` 的测试 —— **P3 的验收判据：12-run 假批次全链路，含失败与 resume。**

这份文件要证明四件事，每一件都有计数断言：

1. **`done` 的判据是产物验过，不是退出码。** 12 个 run 里有 4 个 `exit=0` 却什么都没算出来
   （BRIEF §10 三条实测：崩了也 exit=0 / 0 字节产物照样报 done / 写失败被吞）。
   `test_exit_code_alone_would_have_misjudged_four_runs` 把那个 4 数出来。
2. **阶段 1 失败 ⇒ 该 design 整列 skipped，一个 job 都不提交**（§12）。
3. **resume 只补没成的**：已经 done 的一个都不许重新提交
   （一个 run 可能 10 核 100 GB 跑 35 分钟 —— "跑完了"是绿的，"重跑了 5 个"也是绿的，
   **只有 submit 的计数能把两者分开**）。
4. **有界并发 / 不自动重试 / dry-run 不碰磁盘**。

四条配方（`docs/OVERNIGHT.md`）在这份文件里的落点：

* **关键测试** = `EXPECTED_FINAL_STATUS` 那张 12 行的表，以及 resume 前后的 submit 计数；
* **期望值来源** = **手写字面量**（下面那两张表），不许拿被测代码算一遍。
  `MatrixAnchor` 负责把手写的 run_id 表和 `core.matrix.expand_runs` 的真实输出对上号 ——
  两张表任何一边写错都会当场红；
* **反向验证** = 每条关键测试配一条 `_negative`，**共用同一条构造路径**（`_build()`），
  只改一个入参（把一个失败模式翻成成功 / 阶段 1 改成成功 / 把 max_parallel 调大…）；
* **过滤器测试** = resume 的"该补哪些"是个过滤器，两个方向都断言：
  `test_no_done_run_was_resubmitted`（没把 done 的捞进来 = 没重跑）
  + `test_every_unfinished_run_was_resubmitted`（没漏掉 failed / 在飞的 = 没漏补）。

⏱ **全程不 sleep、不读墙钟**：时间线由 `FakeScheduler` 的"第几次 poll"推进，
时间戳来自它的假时钟（`test_timestamps_come_from_the_fake_clock` 盯着这条）。
唯一起真进程的是 `SubprocessRunnerRealProcess`，它跑的是 `sys.executable`，
**不是** eWave —— 本机永远没有那些工具（CLAUDE.md 硬约束 3）。

🚨 本文件零站点标识符：library / cell / view / 端口名 / 路径 / 账号全是显式假值。
"""

from __future__ import annotations

import csv
import os
import sys
import tempfile
import unittest
from dataclasses import dataclass, field

from ewave_batch.__main__ import check_protocol, normalize_signature
from ewave_batch.core import layout
from ewave_batch.core.matrix import expand_runs
from ewave_batch.model import (
    PLACEHOLDER_VALUE,
    Axis,
    AxisValue,
    BatchOptions,
    BatchState,
    CommandPlan,
    Design,
    DriverEvent,
    DriverProtocol,
    EventKind,
    Job,
    PlanContext,
    PortMode,
    PortSpec,
    RunnerProtocol,
    RunStatus,
    SiteFacts,
    SpecError,
    StateError,
    StreamoutTask,
    TickReport,
    ToolMissingError,
)
from ewave_batch.sched import driver as driver_mod
from ewave_batch.sched.driver import Driver, SubprocessRunner, make_driver, resume_batch, run_batch
from ewave_batch.sched.fake import FakeFailureMode, FakeRunner, FakeScheduler

# --------------------------------------------------------------------------
# 手写的假值（一个真实取值都没有）
# --------------------------------------------------------------------------

FAKE_EWAVE_BIN = "/tmp/fakebin/ewave"
FAKE_STRMOUT_BIN = "/tmp/fakebin/strmout"
FAKE_LAYER_MAP = "/tmp/fakepdk/layer.map"
FAKE_LIB = "TESTLIB"
FAKE_VIEW = "testview"
FAKE_CELL_A = "CELLA"
FAKE_CELL_B = "CELLB"
FAKE_RESOURCES = "cpu=2;mem=100"

PORT_COUNT = 4
"""假产物的端口数。**故意是个小的合成值** —— 真实 design 的端口数是站点信息（硬约束 1b）。
`WRONG_PORT_COUNT` 模式产出 `PORT_COUNT - 1` 端口的产物（少一个 pin ⇒ 全体编号平移，D1b）。"""

FAKE_PORT_NAMES = ("PIN_A", "PIN_B", "PIN_C", "PIN_D")
"""官方那条命令的端口表（`SiteFacts.official_port_spec`）。driver 拿它的**条数**当
"产物应该是几端口"的期望值 —— 这是 `--all` 的代价那道防线（BRIEF §5）。名字是编的。"""

CORNERS = ("typical", "cworst")
TEMPERATURES = ("-40.0", "25.0", "125.0")

# --------------------------------------------------------------------------
# ★ 手写的期望表（防自证配方 2：期望值不许由被测代码算出来）
# --------------------------------------------------------------------------

RUN_IDS: tuple[str, ...] = (
    # run_id = <design_key>/<axes_slug>/<ewave_dir>（model.Run.run_id 的定义）。
    # axes_slug 是 `base`：corner/temperature 都被 eWave 编进 `<corner>_<temp>/` 那层了
    # （Axis.encoded_in_ewave_dir=True ⇒ 不进 axes-slug，否则目录名里出现两遍，BRIEF §5）。
    # 温度里的小数点换下划线（model.TEMP_DECIMAL_REPLACEMENT，eWave 自己的约定）。
    "dA/base/typical_-40_0",
    "dA/base/typical_25_0",
    "dA/base/typical_125_0",
    "dA/base/cworst_-40_0",
    "dA/base/cworst_25_0",
    "dA/base/cworst_125_0",
    "dB/base/typical_-40_0",
    "dB/base/typical_25_0",
    "dB/base/typical_125_0",
    "dB/base/cworst_-40_0",
    "dB/base/cworst_25_0",
    "dB/base/cworst_125_0",
)
"""2 design × 2 corner × 3 temperature = **12 个 run**（本阶段判据里的那个 12）。

顺序照 `expand_runs` 的文档：design 在外，轴按 spec 顺序，第一根轴变得最慢。
`MatrixAnchor` 拿真实的 `expand_runs` 输出跟这张表逐条对 —— 两边任何一边错了都当场红。
"""

BATCH_MODES: dict[str, FakeFailureMode] = {
    # 本阶段任务书点名的三条实测坑，一样一个；再加"写失败被吞"凑齐 BRIEF §10 那三行。
    "dA/base/typical_25_0": FakeFailureMode.EXIT_ZERO_BUT_CRASHED,
    "dA/base/cworst_-40_0": FakeFailureMode.ZERO_BYTE_OUTPUT,
    "dB/base/typical_125_0": FakeFailureMode.WRONG_PORT_COUNT,
    "dB/base/cworst_25_0": FakeFailureMode.SWALLOWED_WRITE_FAILURE,
}
"""哪个 run 用哪个失败模式。**键是 run_id**（`FakeRunner._command_key` 的合法键：
产物目录 `<workDir>/<corner>_<temp>` 以 `/<run_id>` 结尾）。"""

EXPECTED_FINAL_STATUS: dict[str, str] = {
    # ★★ 12 行手写的终态表。出处：
    #   done   = 产物齐、非空、端口数 == 4（core.layout.verify_run_outputs 的验收契约）
    #   failed = BATCH_MODES 里点名的那四条实测坑，**每一条的退出码都是 0**
    #            （BRIEF §10：崩了也 exit=0 / 0 字节产物报 done / 写失败零错误输出 /
    #             --all 少一个 pin 全体编号平移）
    "dA/base/typical_-40_0": "done",
    "dA/base/typical_25_0": "failed",  # EXIT_ZERO_BUT_CRASHED：exit=0，零产物
    "dA/base/typical_125_0": "done",
    "dA/base/cworst_-40_0": "failed",  # ZERO_BYTE_OUTPUT：文件在、0 字节、日志报 done
    "dA/base/cworst_25_0": "done",
    "dA/base/cworst_125_0": "done",
    "dB/base/typical_-40_0": "done",
    "dB/base/typical_25_0": "done",
    "dB/base/typical_125_0": "failed",  # WRONG_PORT_COUNT：.s3p，期望 4 端口
    "dB/base/cworst_-40_0": "done",
    "dB/base/cworst_25_0": "failed",  # SWALLOWED_WRITE_FAILURE：零错误输出，零产物
    "dB/base/cworst_125_0": "done",
}

EXPECTED_DONE = 8
EXPECTED_FAILED = 4
"""★ 手写的计数：12 = 8 done + 4 failed。那个 4 就是"只看退出码会被判成功、
实际空手而归"的 run 数 —— 本工具存在的全部理由。"""


# --------------------------------------------------------------------------
# 构造（正反两向共用这一条路径）
# --------------------------------------------------------------------------


def _facts(official_run_dir: str = "") -> SiteFacts:
    """最小站点坐标。字段全是假路径（`SiteFacts` 里装的全是站点身份，硬约束 1b）。"""
    return SiteFacts(
        official_run_dir=official_run_dir,
        ewave_bin=FAKE_EWAVE_BIN,
        strmout_bin=FAKE_STRMOUT_BIN,
        layer_map=FAKE_LAYER_MAP,
        dsub_resources=FAKE_RESOURCES,
        official_port_spec=PortSpec(
            mode=PortMode.EXPLICIT,
            mapping=tuple((f"P{i:03d}", name) for i, name in enumerate(FAKE_PORT_NAMES)),
        ),
    )


def _axes(corners: tuple[str, ...] = CORNERS, temps: tuple[str, ...] = TEMPERATURES) -> list[Axis]:
    """corner × temperature。

    ⚠️ 真实的 corner 轴还要同时改 `--emssTechFile`（BRIEF §7），这里**故意省掉**：
    那个值要靠 `core.discover.ptxt_path_for_corner` 解析站点路径，而本文件测的是
    调度与状态机，不是命令拼装（那是 `test_cmd_golden.py` 的活）。
    argv 里只需要 `--corner` / `--temperature` —— eWave 那层目录名只由这两个决定。
    """
    return [
        Axis(
            name="corner",
            values=tuple(AxisValue(v, flags={"--corner": PLACEHOLDER_VALUE}) for v in corners),
            flags=("--corner",),
            short="corner",
            encoded_in_ewave_dir=True,
        ),
        Axis(
            name="temperature",
            values=tuple(AxisValue(v, flags={"--temperature": PLACEHOLDER_VALUE}) for v in temps),
            flags=("--temperature",),
            short="temp",
            encoded_in_ewave_dir=True,
        ),
    ]


@dataclass
class _Batch:
    """一次构造出来的全套东西。正反两向只改 `_build()` 的一个入参。"""

    root: str
    batch_dir: str
    state: BatchState
    contexts: dict[str, PlanContext]
    runner: FakeRunner
    scheduler: FakeScheduler
    options: BatchOptions
    events: list[DriverEvent] = field(default_factory=list)

    def driver(self) -> DriverProtocol:
        return make_driver(
            self.state, self.contexts, self.scheduler, self.runner, on_event=self.events.append
        )

    def statuses(self) -> dict[str, str]:
        return {run.run_id: run.status.value for run in self.state.runs}

    def submitted_run_ids(self) -> list[str]:
        return [plan.run_id for plan in self.scheduler.plans.values()]

    def strmout_calls(self) -> list[tuple[str, ...]]:
        return [argv for argv in self.runner.calls if "-templateFile" in argv]


def _build(
    root: str,
    *,
    name: str = "b",
    modes: dict[str, FakeFailureMode] | None = None,
    port_counts: dict[str, int] | None = None,
    corners: tuple[str, ...] = CORNERS,
    temps: tuple[str, ...] = TEMPERATURES,
    cells: tuple[tuple[str, str], ...] = (("dA", FAKE_CELL_A), ("dB", FAKE_CELL_B)),
    with_cds_lib: bool = True,
    dry_run: bool = False,
    max_parallel: int = 4,
    verify_port_count: bool = True,
    scheduler: object | None = None,
    runner: FakeRunner | None = None,
) -> _Batch:
    """走**真实的**核心链路造一个批次：`expand_runs` → `PlanContext` → driver。

    刻意不手搓 state / argv：本文件要证明的是"driver 把真实的核心件接起来之后行为对"，
    手搓就把接缝测没了 —— 而接缝正是并行开发最容易漂的地方。
    """
    batch_dir = f"{root}/{name}"
    workarea = f"{root}/wa"
    if with_cds_lib:
        os.makedirs(workarea, exist_ok=True)
        with open(f"{workarea}/cds.lib", "w", encoding="utf-8", newline="\n") as handle:
            handle.write("DEFINE FAKE ./fake\n")
    designs = [
        Design(
            library=FAKE_LIB,
            cell=cell,
            view=FAKE_VIEW,
            key=key,
            # 官方 run 目录形如 <workarea>/ewave_simulation/<design>/ —— cds.lib 在往上两层。
            # 这个目录是**只读**的（硬约束 4），我们连建都不建它。
            official_run_dir=f"{workarea}/ewave_simulation/{cell}",
        )
        for key, cell in cells
    ]
    axes = _axes(corners, temps)
    options = BatchOptions(
        dry_run=dry_run,
        max_parallel=max_parallel,
        verify_port_count=verify_port_count,
        poll_interval=0.0,
    )
    runs = expand_runs(designs, axes, options=options)
    state = BatchState(
        batch_name=name,
        batch_dir=batch_dir,
        designs=designs,
        axes=axes,
        runs=runs,
        streamout=[StreamoutTask(design_key=d.key) for d in designs],
        options=options,
    )
    contexts = {
        d.key: PlanContext(
            design=d,
            facts=_facts(d.official_run_dir),
            axes=tuple(axes),
            options=options,
            batch_dir=batch_dir,
        )
        for d in designs
    }
    fake_runner = runner if runner is not None else FakeRunner(
        modes=modes or {}, port_count=PORT_COUNT, port_counts=port_counts or {}
    )
    sched = scheduler if scheduler is not None else FakeScheduler(fake_runner)
    return _Batch(
        root=root,
        batch_dir=batch_dir,
        state=state,
        contexts=contexts,
        runner=fake_runner,
        scheduler=sched,  # type: ignore[arg-type]
        options=options,
    )


def _drive(driver: DriverProtocol, *, max_ticks: int = 60) -> list[TickReport]:
    """一直 tick 到结束。**不 sleep** —— 时间线由 poll 次数推进。"""
    reports: list[TickReport] = []
    for _ in range(max_ticks):
        report = driver.tick()
        reports.append(report)
        if report.finished:
            break
    return reports


class _TempRootTest(unittest.TestCase):
    """每个测试一个干净的临时根目录；`_root()` 再往下切互不影响的批次根。"""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.base = holder.name.replace("\\", "/")
        self._serial = 0

    def _root(self) -> str:
        """一个全新的根。同一个测试里正反两向各要一个，否则上一次的产物会串味。"""
        self._serial += 1
        root = f"{self.base}/r{self._serial}"
        os.makedirs(root, exist_ok=True)
        return root


# --------------------------------------------------------------------------
# 冻结面
# --------------------------------------------------------------------------


class FrozenContract(_TempRootTest):
    """`Driver` / `SubprocessRunner` 必须真的满足冻结的 Protocol（不只是"方法名对得上"）。"""

    def test_driver_matches_the_frozen_protocol(self) -> None:
        drifts = check_protocol("ewave_batch.sched.driver", Driver, "DriverProtocol")
        self.assertEqual(drifts, [], "Driver 与 DriverProtocol 逐方法比签名")

    def test_subprocess_runner_matches_the_frozen_protocol(self) -> None:
        drifts = check_protocol("ewave_batch.sched.driver", SubprocessRunner, "RunnerProtocol")
        self.assertEqual(drifts, [])
        self.assertEqual(
            normalize_signature(SubprocessRunner.run),
            normalize_signature(RunnerProtocol.run),
        )
        self.assertIsInstance(SubprocessRunner(), RunnerProtocol)

    def test_make_driver_returns_something_that_satisfies_the_protocol(self) -> None:
        batch = _build(self._root())
        self.assertIsInstance(batch.driver(), DriverProtocol)

    def test_make_driver_rejects_a_missing_context_negative(self) -> None:
        """`contexts` 少一个 design ⇒ `SpecError`（坐标是 per-design 的，少一个就拼不出命令）。"""
        batch = _build(self._root())
        del batch.contexts["dB"]
        with self.assertRaises(SpecError) as caught:
            make_driver(batch.state, batch.contexts, batch.scheduler, batch.runner)
        self.assertIn("dB", str(caught.exception))

    def test_empty_batch_dir_is_refused_negative(self) -> None:
        batch = _build(self._root())
        batch.state.batch_dir = ""
        with self.assertRaises(StateError):
            make_driver(batch.state, batch.contexts, batch.scheduler, batch.runner)


# --------------------------------------------------------------------------
# 手写表 vs 真实展开
# --------------------------------------------------------------------------


class MatrixAnchor(_TempRootTest):
    """把手写的 12 行表和 `core.matrix.expand_runs` 的真实输出对上号。

    这条是整份文件的锚：手写表写错了（或者矩阵展开变了）会在这里当场红，
    而不是让后面那些"终态等于期望"的断言在一张错的表上全绿。
    """

    def test_expand_runs_gives_exactly_the_hand_written_twelve(self) -> None:
        batch = _build(self._root())
        got = tuple(run.run_id for run in batch.state.runs)
        self.assertEqual(len(got), 12, "2 design × 2 corner × 3 temperature")
        self.assertEqual(got, RUN_IDS, "顺序也要一致：design 在外，第一根轴变得最慢")

    def test_the_two_hand_written_tables_agree(self) -> None:
        """失败模式表和终态表是两张独立手写的表 —— 它们必须互相说得通。"""
        self.assertEqual(set(EXPECTED_FINAL_STATUS), set(RUN_IDS))
        self.assertEqual(
            set(BATCH_MODES),
            {run_id for run_id, status in EXPECTED_FINAL_STATUS.items() if status == "failed"},
        )
        self.assertEqual(len(BATCH_MODES), EXPECTED_FAILED)
        self.assertEqual(
            sum(1 for s in EXPECTED_FINAL_STATUS.values() if s == "done"), EXPECTED_DONE
        )
        self.assertEqual(EXPECTED_DONE + EXPECTED_FAILED, 12)

    def test_the_four_named_failure_modes_are_all_distinct(self) -> None:
        """任务书点名的三条 + 写失败被吞：四条坑不许重复（重复就等于少测一种）。"""
        self.assertEqual(len(set(BATCH_MODES.values())), 4)
        self.assertIn(FakeFailureMode.EXIT_ZERO_BUT_CRASHED, BATCH_MODES.values())
        self.assertIn(FakeFailureMode.ZERO_BYTE_OUTPUT, BATCH_MODES.values())
        self.assertIn(FakeFailureMode.WRONG_PORT_COUNT, BATCH_MODES.values())


# --------------------------------------------------------------------------
# ★ 12-run 假批次全链路
# --------------------------------------------------------------------------


class TwelveRunFullChain(_TempRootTest):
    """★ 本阶段的核心验收：12 个 run 走完 阶段 1 → 提交 → 轮询 → 验收 → 归档。"""

    def setUp(self) -> None:
        super().setUp()
        self.batch = _build(self._root(), modes=dict(BATCH_MODES))
        self.driver = self.batch.driver()
        self.reports = _drive(self.driver)

    def test_the_batch_finishes(self) -> None:
        self.assertTrue(self.reports[-1].finished, "跑不完的话下面的终态断言都是空过的")
        self.assertLess(len(self.reports), 60, "60 拍还没完 = 状态机在原地打转")

    def test_final_status_table_matches_the_hand_written_expectation(self) -> None:
        """★★ 关键测试：12 个 run 的终态**逐个**等于手写的表。"""
        self.assertEqual(self.batch.statuses(), EXPECTED_FINAL_STATUS)
        counts = self.driver.summary()
        self.assertEqual(counts["done"], EXPECTED_DONE)
        self.assertEqual(counts["failed"], EXPECTED_FAILED)
        self.assertEqual(counts["skipped"], 0)
        self.assertEqual(sum(counts.values()), 12)

    def test_final_status_table_negative_one_failure_flipped_to_success(self) -> None:
        """★ 反向验证：**同一条构造路径**，只把一个失败模式翻成成功 ——
        终态表必须**正好**在那一处不同（不多不少）。

        少了这条，一个"永远判 done"的 driver 也过不了正向那条（它有 4 个 failed），
        但一个"永远判 failed"的 driver 会让正向变红而这里变绿；两条一起才把判据夹死。
        """
        flipped = dict(BATCH_MODES)
        del flipped["dA/base/typical_25_0"]  # 这一条不再崩 ⇒ 应当变成 done
        batch = _build(self._root(), modes=flipped)
        _drive(batch.driver())

        got = batch.statuses()
        differing = {
            run_id
            for run_id in RUN_IDS
            if got[run_id] != EXPECTED_FINAL_STATUS[run_id]
        }
        self.assertEqual(differing, {"dA/base/typical_25_0"})
        self.assertEqual(got["dA/base/typical_25_0"], "done")
        self.assertEqual(sum(1 for v in got.values() if v == "failed"), EXPECTED_FAILED - 1)

    def test_exit_code_alone_would_have_misjudged_four_runs(self) -> None:
        """★ 本阶段验收契约的数字化：**4 个 run 退出码是 0，产物却空手而归。**

        出处 BRIEF §10：崩了也 `exit=0` / 0 字节产物照样报 done / 写失败零错误输出 /
        `--all` 少一个 pin 全体编号平移。要是 driver 拿 `Job.exit_code` 判 done，
        这 4 个会被写成"成功"，而且 batch.json、runs.csv、扁平区全都长得一模一样。
        """
        zero_exit = [run for run in self.batch.state.runs if run.job and run.job.exit_code == 0]
        self.assertEqual(len(zero_exit), 12, "四个失败模式的退出码全是 0（对照组也退 0）")
        misjudged = [run for run in zero_exit if run.status is RunStatus.FAILED]
        self.assertEqual(len(misjudged), 4)
        self.assertEqual(
            {run.run_id for run in misjudged},
            set(BATCH_MODES),
            "被抓住的正好是那四条实测坑",
        )
        for run in misjudged:
            self.assertTrue(run.message, "失败必须留一句话，不能只是一个状态字")
            self.assertIn("退出码", run.message, "报告里要点明退出码不可信")

    def test_job_state_done_never_became_run_done_by_itself(self) -> None:
        """所有 12 个 job 都是 `JobState.DONE`（进程都退了 0），但只有 8 个 run 是 done。"""
        job_done = [run for run in self.batch.state.runs if run.job and run.job.state.value == "done"]
        self.assertEqual(len(job_done), 12)
        self.assertEqual(sum(1 for r in job_done if r.status is RunStatus.DONE), EXPECTED_DONE)

    def test_every_run_landed_in_its_own_directory(self) -> None:
        """D2：每个组合一个独立 `--workDir` ⇒ 12 个互不相同的产物目录。撞了就是静默覆盖。"""
        dirs = set()
        for run in self.batch.state.runs:
            design = next(d for d in self.batch.state.designs if d.key == run.design_key)
            paths = layout.compute_run_paths(self.batch.batch_dir, design, run)
            dirs.add(paths.ewave_dir)
            self.assertTrue(os.path.isdir(paths.ewave_dir), f"{run.run_id} 的产物目录没建出来")
        self.assertEqual(len(dirs), 12)

    def test_stage_one_ran_once_per_design(self) -> None:
        """D1a：GDS 不随设定变 ⇒ 12 个 run 只跑 **2** 次 strmout（不是 12 次）。"""
        self.assertEqual(len(self.batch.strmout_calls()), 2)
        for task in self.batch.state.streamout:
            self.assertIs(task.status, RunStatus.DONE)
            self.assertTrue(os.path.getsize(task.gds_path) > 0)

    def test_no_auto_retry(self) -> None:
        """§12：**不自动重试**。12 个 run 各提交一次，一次都不多。"""
        self.assertEqual(self.batch.scheduler.submit_calls, 12)
        for run in self.batch.state.runs:
            self.assertEqual(run.attempts, 1, f"{run.run_id} 被提交了 {run.attempts} 次")

    def test_done_runs_are_archived_into_the_flat_area(self) -> None:
        """D5：验过的 run 把参数文件收进 `sparam/` 扁平区，每个 run 两份（正式的 + `_sample`）。"""
        done = [run for run in self.batch.state.runs if run.status is RunStatus.DONE]
        self.assertEqual(len(done), EXPECTED_DONE)
        for run in done:
            self.assertEqual(len(run.artifacts), 2, f"{run.run_id}: {run.artifacts}")
            for artifact in run.artifacts:
                self.assertTrue(artifact.startswith("sparam/"), artifact)
                full = f"{self.batch.batch_dir}/{artifact}"
                self.assertTrue(os.path.getsize(full) > 0)
        flat = sorted(os.listdir(f"{self.batch.batch_dir}/sparam"))
        self.assertEqual(len(flat), EXPECTED_DONE * 2, "8 个 done × 2 份")
        for name in flat:
            self.assertTrue(name.endswith(f".s{PORT_COUNT}p"), name)

    def test_failed_runs_are_not_archived_and_keep_their_logs(self) -> None:
        """D5：失败时保留日志做诊断 —— 失败的 run 一个字节都不许进扁平区。"""
        failed = [run for run in self.batch.state.runs if run.status is RunStatus.FAILED]
        self.assertEqual(len(failed), EXPECTED_FAILED)
        for run in failed:
            self.assertEqual(run.artifacts, ())
            design = next(d for d in self.batch.state.designs if d.key == run.design_key)
            paths = layout.compute_run_paths(self.batch.batch_dir, design, run)
            self.assertTrue(
                os.path.isfile(f"{paths.ewave_dir}/ewave.log"),
                f"{run.run_id} 的现场被清掉了 —— 失败现场是最贵的东西",
            )

    def test_batch_json_on_disk_agrees_with_memory(self) -> None:
        """resume 只认 `batch.json` ⇒ 内存里是什么，盘上就得是什么。"""
        restored = layout.read_batch_state(f"{self.batch.batch_dir}/batch.json")
        self.assertEqual({r.run_id: r.status.value for r in restored.runs}, EXPECTED_FINAL_STATUS)
        self.assertEqual(len(restored.streamout), 2)
        self.assertEqual([t.status.value for t in restored.streamout], ["done", "done"])

    def test_runs_csv_has_one_row_per_run(self) -> None:
        with open(f"{self.batch.batch_dir}/runs.csv", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 12)
        self.assertEqual({row["run_id"]: row["status"] for row in rows}, EXPECTED_FINAL_STATUS)

    def test_cmd_sh_is_written_per_run(self) -> None:
        """每个 run 一份可手工重跑的命令留档（model.CMD_SH_TEMPLATE：固定名会互相覆盖）。"""
        written = set()
        for run in self.batch.state.runs:
            design = next(d for d in self.batch.state.designs if d.key == run.design_key)
            paths = layout.compute_run_paths(self.batch.batch_dir, design, run)
            self.assertTrue(os.path.isfile(paths.cmd_sh), run.run_id)
            written.add(paths.cmd_sh)
        self.assertEqual(len(written), 12, "12 个 run ⇒ 12 份互不覆盖的 cmd.sh")

    def test_timestamps_come_from_the_fake_clock_not_the_wall_clock(self) -> None:
        """确定性：run 的时间戳来自 `FakeScheduler` 的假时钟（epoch 2026-01-01）。

        它们要是来自墙钟，这条断言在任何一天都会红 —— 也就是说这条真的在测东西。
        """
        stamped = [run for run in self.batch.state.runs if run.submitted_at]
        self.assertEqual(len(stamped), 12)
        for run in stamped:
            self.assertTrue(run.submitted_at.startswith("2026-01-01T"), run.submitted_at)
            self.assertTrue(run.ended_at.startswith("2026-01-01T"), run.ended_at)
            self.assertIsNotNone(run.wall_seconds)
            self.assertGreater(run.wall_seconds or 0.0, 0.0)

    def test_events_cover_the_whole_lifecycle(self) -> None:
        """CLI 打印 / GUI 刷表都靠事件 —— 每个阶段都得播出来。"""
        kinds = {event.kind for event in self.batch.events}
        for expected in (
            EventKind.STARTED,
            EventKind.SUBMITTED,
            EventKind.FINISHED,
            EventKind.FAILED,
            EventKind.ARCHIVED,
        ):
            self.assertIn(expected, kinds)
        submitted = [e for e in self.batch.events if e.kind is EventKind.SUBMITTED]
        self.assertEqual(len(submitted), 12)
        self.assertEqual({e.run_id for e in submitted}, set(RUN_IDS))


class ArtifactsAreNotClaimedByPrefix(_TempRootTest):
    """扁平区认领产物时**不许做裸前缀匹配** —— 这是 MVP 那个真 bug 的同型回归测试。

    MVP 里排除规则写成前缀，`--sparam` 吃掉了 `--sparamImpedance`，两边同时被跳过，
    diff 空得非常好看但根本没比。这里的同型陷阱是温度：`typical_25_0` 是
    `typical_25_05` 的前缀，于是 25.0 那个 run 会把 25.05 的产物一起认领走 ——
    两个 run 都"有产物"，`runs.csv` 也很好看，只是有一列指着别人的文件。
    """

    def _run_two_prefix_temperatures(self) -> _Batch:
        batch = _build(
            self._root(),
            corners=("typical",),
            temps=("25.0", "25.05"),  # 一个是另一个的前缀
            cells=(("dA", FAKE_CELL_A),),
        )
        _drive(batch.driver())
        return batch

    def test_the_two_run_ids_really_are_prefixes_of_each_other(self) -> None:
        """先证明陷阱真的摆好了 —— 否则下面那条是空过的。"""
        batch = self._run_two_prefix_temperatures()
        ids = [run.run_id for run in batch.state.runs]
        self.assertEqual(ids, ["dA/base/typical_25_0", "dA/base/typical_25_05"])
        self.assertTrue(ids[1].startswith(ids[0]))

    def test_each_run_claims_exactly_its_own_two_files(self) -> None:
        """跑完之后，两个 run 各认领 2 份（端到端那条路）。"""
        batch = self._run_two_prefix_temperatures()
        by_id = {run.run_id: run for run in batch.state.runs}
        self.assertEqual(len(os.listdir(f"{batch.batch_dir}/sparam")), 4, "2 个 run × 2 份")
        for run_id, stem in (
            ("dA/base/typical_25_0", "dA__base__typical_25_0"),
            ("dA/base/typical_25_05", "dA__base__typical_25_05"),
        ):
            artifacts = by_id[run_id].artifacts
            self.assertEqual(
                sorted(artifacts),
                [f"sparam/{stem}.s{PORT_COUNT}p", f"sparam/{stem}_sample.s{PORT_COUNT}p"],
                f"{run_id} 认领的文件不对",
            )

    def test_the_filter_itself_rejects_the_longer_stem(self) -> None:
        """★ 过滤器本身的测试：**两份产物同时在场**时，短词根那个 run 不许多认 2 份。

        端到端那条路碰不到这个陷阱（先完成的那个 run 归档时，另一份还没落地）——
        所以这里手工把两代产物一起摆好再问过滤器。这正是 MVP 那个 bug 当年逃掉的方式：
        真实顺序恰好掩盖了它。
        """
        batch = _build(
            self._root(),
            corners=("typical",),
            temps=("25.0", "25.05"),
            cells=(("dA", FAKE_CELL_A),),
        )
        driver = Driver(batch.state, batch.contexts, batch.scheduler, batch.runner)
        design = batch.state.designs[0]
        short, long_ = batch.state.runs[0], batch.state.runs[1]
        paths_short = layout.compute_run_paths(batch.batch_dir, design, short)
        paths_long = layout.compute_run_paths(batch.batch_dir, design, long_)
        self.assertTrue(
            os.path.basename(paths_long.sparam_prefix).startswith(
                os.path.basename(paths_short.sparam_prefix)
            ),
            "陷阱没摆好：词根不再是前缀关系，这条测试就是空过的",
        )
        os.makedirs(paths_short.sparam_dir, exist_ok=True)
        for prefix in (paths_short.sparam_prefix, paths_long.sparam_prefix):
            for suffix in (f".s{PORT_COUNT}p", f"_sample.s{PORT_COUNT}p"):
                with open(prefix + suffix, "w", encoding="utf-8") as handle:
                    handle.write("! fake product\n")
        # 计数断言：4 份都在场（空目录的过滤永远好看 —— 这条专防"空得非常好看"）
        self.assertEqual(len(os.listdir(paths_short.sparam_dir)), 4)
        self.assertEqual(len(driver._flat_artifacts(paths_short)), 2, "短词根多认了别人的")
        self.assertEqual(len(driver._flat_artifacts(paths_long)), 2)
        self.assertEqual(
            set(driver._flat_artifacts(paths_short)) & set(driver._flat_artifacts(paths_long)),
            set(),
            "两个 run 认领的文件不许有交集",
        )


# --------------------------------------------------------------------------
# 阶段 1 失败 ⇒ 整列 skipped
# --------------------------------------------------------------------------


class StageOneFailureSkipsTheWholeColumn(_TempRootTest):
    """§12：阶段 1（strmout）失败 ⇒ 该 design 整列 `skipped`，**一个 job 都不提交**。"""

    def setUp(self) -> None:
        super().setUp()
        # 键是 `-templateFile` 指的那份 setup（`FakeRunner._command_key` 对 strmout 的规则）。
        self.batch = _build(
            self._root(), modes={"dB.gdsout_setup": FakeFailureMode.NONZERO_EXIT}
        )
        self.driver = self.batch.driver()
        _drive(self.driver)

    def test_that_design_is_skipped_column_wide(self) -> None:
        counts = self.driver.summary()
        self.assertEqual(counts["skipped"], 6, "dB 名下 2 corner × 3 temp = 6 个组合全跳")
        skipped = {r.run_id for r in self.batch.state.runs if r.status is RunStatus.SKIPPED}
        self.assertEqual(skipped, {rid for rid in RUN_IDS if rid.startswith("dB/")})
        for run in self.batch.state.runs:
            if run.status is RunStatus.SKIPPED:
                self.assertIn("阶段 1", run.message)

    def test_no_job_was_ever_submitted_for_that_design(self) -> None:
        """★ 计数断言：只为 dA 提交了 6 次，dB 一次都没有（提交必然失败的 job 是浪费配额）。"""
        self.assertEqual(self.batch.scheduler.submit_calls, 6)
        submitted = self.batch.submitted_run_ids()
        self.assertEqual(len(submitted), 6)
        self.assertEqual({rid.split("/")[0] for rid in submitted}, {"dA"})

    def test_the_other_design_still_finishes(self) -> None:
        """阶段 2 不 fail-fast，阶段 1 也只砍自己那一列。"""
        self.assertEqual(self.driver.summary()["done"], 6)

    def test_option_off_means_the_column_runs_anyway(self) -> None:
        """`stop_design_on_streamout_failure=False` ⇒ 阶段 1 挂了也照提交（用户明说的）。

        要紧的是**批次仍然转得出来**：停在 ready 不动比失败更糟 —— 没人知道它在等什么。
        """
        batch = _build(self._root(), modes={"dB.gdsout_setup": FakeFailureMode.NONZERO_EXIT})
        batch.state.options.stop_design_on_streamout_failure = False
        driver = batch.driver()
        reports = _drive(driver)
        self.assertTrue(reports[-1].finished, "批次必须转得出来，不许挂着")
        self.assertEqual(driver.summary()["skipped"], 0)
        self.assertEqual(batch.scheduler.submit_calls, 12, "12 个组合照样提交")
        failed_tasks = [t for t in batch.state.streamout if t.status is RunStatus.FAILED]
        self.assertEqual(len(failed_tasks), 1, "阶段 1 的账仍然如实记 failed")

    def test_negative_stage_one_success_submits_all_twelve(self) -> None:
        """★ 反向验证：**同一条构造路径**，只把阶段 1 的模式去掉 ⇒ 12 个全提交、0 个 skipped。

        没有这条，一个"永远 skip"的 driver 也能让上面三条全绿。
        """
        batch = _build(self._root())
        driver = batch.driver()
        _drive(driver)
        self.assertEqual(driver.summary()["skipped"], 0)
        self.assertEqual(batch.scheduler.submit_calls, 12)
        self.assertEqual(driver.summary()["done"], 12)


# --------------------------------------------------------------------------
# 阶段 1 的 cds.lib
# --------------------------------------------------------------------------


class CdsLibForStageOne(_TempRootTest):
    """BRIEF §10 step1 实测：在**我们自己的** `cdswork/` 里放一行 `INCLUDE <workarea>/cds.lib`，
    strmout 就能解析 `-library` —— 于是不必 cd 进设计师的 workarea（硬约束 4）。"""

    def test_cds_lib_points_at_the_discovered_workarea(self) -> None:
        root = self._root()
        batch = _build(root, with_cds_lib=True)
        _drive(batch.driver())
        written = f"{batch.batch_dir}/cdswork/cds.lib"
        self.assertTrue(os.path.isfile(written))
        with open(written, encoding="utf-8") as handle:
            body = handle.read()
        # 期望值由**测试自己**从它建的那份 fixture 拼出来（`<root>/wa/cds.lib`），
        # 不是问被测代码要的。
        self.assertEqual(body, f"INCLUDE {root}/wa/cds.lib\n")

    def test_negative_no_cds_lib_anywhere_writes_nothing_and_warns(self) -> None:
        """反向：找不到 `cds.lib` 就**不写**（宁可缺，也不编一个路径出来）+ 发 WARNING。"""
        batch = _build(self._root(), with_cds_lib=False)
        _drive(batch.driver())
        self.assertFalse(os.path.isfile(f"{batch.batch_dir}/cdswork/cds.lib"))
        warnings = [e for e in batch.events if e.kind is EventKind.WARNING]
        self.assertTrue(any("cds.lib" in e.message for e in warnings), [e.message for e in warnings])

    def test_search_walks_up_but_not_forever(self) -> None:
        """过滤器本身的测试：往上找有上限，且**只认真的有 cds.lib 的那一层**。"""
        root = self._root()
        deep = f"{root}/a/b/c/d/e/f"
        os.makedirs(deep, exist_ok=True)
        with open(f"{root}/a/cds.lib", "w", encoding="utf-8") as handle:
            handle.write("x\n")
        self.assertEqual(driver_mod._find_cds_lib_root(f"{root}/a/b/c"), f"{root}/a")
        self.assertEqual(driver_mod._find_cds_lib_root(deep), "", "超过 4 层就不找了")
        self.assertEqual(driver_mod._find_cds_lib_root(f"{root}/a"), f"{root}/a", "自己这层也算")


# --------------------------------------------------------------------------
# ★ resume
# --------------------------------------------------------------------------


class ResumeOnlyCompletesWhatIsMissing(_TempRootTest):
    """★ D7：中途"杀进程"，**从磁盘上的 batch.json 重新造一个 driver**，resume 只补没成的。

    ⚠️ 复用内存里那个 driver 等于没测 resume —— 这里每次都是
    `resume_batch(batch_dir, …)`，新的调度器、新的 runner、全新的对象图。
    """

    def setUp(self) -> None:
        super().setUp()
        self.root = self._root()
        self.first = _build(self.root, modes=dict(BATCH_MODES))
        driver = self.first.driver()
        # 跑到一半：一有 run 完成就停手（模拟进程被 kill -9）。
        for _ in range(30):
            report = driver.tick()
            if report.counts.get("done", 0) >= 3:
                break
        self.before = {run.run_id: run.status.value for run in driver.state.runs}
        self.attempts_before = {run.run_id: run.attempts for run in driver.state.runs}
        self.done_before = {rid for rid, s in self.before.items() if s == "done"}
        self.unfinished_before = {rid for rid, s in self.before.items() if s != "done"}
        del driver  # 进程没了；下面只准从磁盘读

        self.events: list[DriverEvent] = []
        self.second = _build(
            self.root, name="b", modes=dict(BATCH_MODES)
        )  # 只借它的 contexts / runner / scheduler（state 从磁盘读）
        self.resumed = resume_batch(
            self.first.batch_dir,
            self.second.contexts,
            self.second.scheduler,
            self.second.runner,
            on_event=self.events.append,
        )
        _drive(self.resumed)

    def test_the_kill_point_was_meaningful(self) -> None:
        """空过防线：杀之前既不能一个都没成，也不能全都成了 —— 否则下面全是空断言。"""
        self.assertGreater(len(self.done_before), 0)
        self.assertLess(len(self.done_before), 12)

    def test_resume_resubmitted_exactly_the_unfinished_runs(self) -> None:
        """★★ 计数断言：新调度器上的 submit 次数 == 杀之前没成的那些，一个不多一个不少。"""
        self.assertEqual(self.second.scheduler.submit_calls, len(self.unfinished_before))
        self.assertEqual(
            sorted(self.second.submitted_run_ids()), sorted(self.unfinished_before)
        )

    def test_no_done_run_was_resubmitted(self) -> None:
        """★ 过滤器方向一：**没把 done 的捞进来**（那是重跑，一个 run 值 10 核 100 GB×35 分钟）。

        "跑完了"是绿的，"重跑了 5 个"也是绿的 —— 只有这条计数能把两者分开。
        """
        resubmitted = set(self.second.submitted_run_ids())
        self.assertEqual(resubmitted & self.done_before, set())
        for run in self.resumed.state.runs:
            if run.run_id in self.done_before:
                self.assertEqual(
                    run.attempts,
                    self.attempts_before[run.run_id],
                    f"{run.run_id} 的提交次数变了 —— 它被重跑了",
                )

    def test_every_unfinished_run_was_resubmitted(self) -> None:
        """★ 过滤器方向二：**没漏掉** failed / 还在飞的（那是漏补）。"""
        resubmitted = set(self.second.submitted_run_ids())
        self.assertEqual(self.unfinished_before - resubmitted, set())
        for run_id in self.unfinished_before:
            run = next(r for r in self.resumed.state.runs if r.run_id == run_id)
            self.assertEqual(
                run.attempts,
                self.attempts_before[run_id] + 1,
                f"{run_id} 应当正好又提交了一次（不自动重试：一次 resume 只补一次）",
            )

    def test_done_count_is_the_old_ones_plus_the_new_ones(self) -> None:
        """★ 计数断言：done 总数 == 杀之前已 done + 这轮新成的。"""
        after = {run.run_id: run.status.value for run in self.resumed.state.runs}
        newly_done = {
            rid for rid in self.unfinished_before if after[rid] == "done"
        }
        self.assertEqual(
            sum(1 for s in after.values() if s == "done"),
            len(self.done_before) + len(newly_done),
        )

    def test_final_status_table_matches_the_hand_written_expectation(self) -> None:
        """★★ 12 个 run 的终态逐个等于手写表 —— resume 之后结果必须和一口气跑完一样。"""
        after = {run.run_id: run.status.value for run in self.resumed.state.runs}
        self.assertEqual(after, EXPECTED_FINAL_STATUS)

    def test_stage_one_is_not_rerun(self) -> None:
        """GDS 还在（非空）⇒ 阶段 1 一次都不重跑（D1a：整个矩阵共用那一份）。"""
        self.assertEqual(self.second.strmout_calls(), [])
        for task in self.resumed.state.streamout:
            self.assertIs(task.status, RunStatus.DONE)

    def test_disk_is_the_judge_missing_products_are_rerun(self) -> None:
        """★ 判据来自**磁盘**，不是上一次的内存状态：`batch.json` 说 done 而产物没了 ⇒ 重跑。

        （上一次进程被杀时状态可能停在 running，而那个 job 其实已经 exit=0 地崩了 ——
        所以"上次记的状态"本来就不可信，BRIEF §10。）
        """
        root = self._root()
        first = _build(root)
        _drive(first.driver())
        victim = RUN_IDS[0]
        design = next(d for d in first.state.designs if d.key == "dA")
        run = next(r for r in first.state.runs if r.run_id == victim)
        paths = layout.compute_run_paths(first.batch_dir, design, run)
        for name in os.listdir(paths.ewave_dir):
            os.remove(f"{paths.ewave_dir}/{name}")

        second = _build(root, name="b")
        resumed = resume_batch(first.batch_dir, second.contexts, second.scheduler, second.runner)
        self.assertEqual(second.scheduler.submit_calls, 0, "resume 本身不提交，tick 才提交")
        _drive(resumed)
        self.assertEqual(second.submitted_run_ids(), [victim], "只重跑产物没了的那一个")
        self.assertEqual(
            {r.run_id: r.status.value for r in resumed.state.runs},
            {rid: "done" for rid in RUN_IDS},
        )

    def test_disk_is_the_judge_negative_intact_products_are_not_rerun(self) -> None:
        """★ 反向：**同一条路径**，产物不动 ⇒ 一个都不重跑（resume 是幂等的）。"""
        root = self._root()
        first = _build(root)
        _drive(first.driver())
        second = _build(root, name="b")
        resumed = resume_batch(first.batch_dir, second.contexts, second.scheduler, second.runner)
        _drive(resumed)
        self.assertEqual(second.submitted_run_ids(), [])
        self.assertEqual(second.scheduler.submit_calls, 0)
        self.assertEqual(second.runner.calls, [])
        self.assertEqual(resumed.summary()["done"], 12)


# --------------------------------------------------------------------------
# 重跑前清产物
# --------------------------------------------------------------------------


class StaleProductsAreClearedBeforeRerun(_TempRootTest):
    """端口数变了的时候，新产物**不会**覆盖旧的（`.s3p` 和 `.s4p` 是两个文件名）。

    两代混在一个 `<corner>_<temp>/` 里，验收器会（正确地）判"端口数不一致" ——
    于是这个 run 无论重跑多少次都好不了。⇒ driver 必须先清干净再重跑。
    """

    def test_mixed_generations_would_deadlock_the_verifier(self) -> None:
        """★ 反向（不清会怎样）：手工把两代产物摆在一起，真实验收器必须判"不一致"。

        期望值不是问被测代码要的 —— 这里根本没跑 driver，只有 `verify_run_outputs`。
        """
        root = self._root()
        batch = _build(root)
        design = batch.state.designs[0]
        run = batch.state.runs[0]
        paths = layout.compute_run_paths(batch.batch_dir, design, run)
        os.makedirs(paths.ewave_dir, exist_ok=True)
        for name in (f"x_old.s{PORT_COUNT - 1}p", f"x_new.s{PORT_COUNT}p"):
            with open(f"{paths.ewave_dir}/{name}", "w", encoding="utf-8") as handle:
                handle.write("! fake\n")
        verdict = layout.verify_run_outputs(paths, run)
        self.assertFalse(verdict.ok)
        self.assertEqual(len(verdict.sparam_files), 2)
        self.assertTrue(any("端口数不一致" in reason for reason in verdict.reasons), verdict.reasons)

    def test_driver_clears_them_so_the_rerun_can_go_done(self) -> None:
        """★ 正向：第一次 3 端口（失败）→ resume 时换成 4 端口 ⇒ 清干净后判 done，
        且目录里**没有**上一代的 `.s3p`。"""
        root = self._root()
        victim = RUN_IDS[0]
        first = _build(root, modes={victim: FakeFailureMode.WRONG_PORT_COUNT})
        _drive(first.driver())
        self.assertEqual(first.statuses()[victim], "failed")

        second = _build(root, name="b")  # 这一次全成功（4 端口）
        resumed = resume_batch(first.batch_dir, second.contexts, second.scheduler, second.runner)
        _drive(resumed)

        self.assertEqual(second.submitted_run_ids(), [victim])
        self.assertEqual({r.run_id: r.status.value for r in resumed.state.runs}[victim], "done")
        design = next(d for d in first.state.designs if d.key == "dA")
        run = next(r for r in resumed.state.runs if r.run_id == victim)
        paths = layout.compute_run_paths(first.batch_dir, design, run)
        leftovers = [n for n in os.listdir(paths.ewave_dir) if n.endswith(f".s{PORT_COUNT - 1}p")]
        self.assertEqual(leftovers, [], "上一代产物没清掉 ⇒ 这个 run 永远好不了")

    def test_cleaning_only_touches_that_run_directory(self) -> None:
        """清理只发生在这个 run 自己的 `<corner>_<temp>/` 里（别的 run 一个字节都不许动）。"""
        root = self._root()
        batch = _build(root)
        design = batch.state.designs[0]
        run = batch.state.runs[0]
        paths = layout.compute_run_paths(batch.batch_dir, design, run)
        os.makedirs(paths.ewave_dir, exist_ok=True)
        os.makedirs(f"{paths.ewave_dir}/sub", exist_ok=True)
        with open(f"{paths.ewave_dir}/old.s4p", "w", encoding="utf-8") as handle:
            handle.write("x")
        with open(f"{paths.run_dir}/keepme.txt", "w", encoding="utf-8") as handle:
            handle.write("x")
        removed = Driver(
            batch.state, batch.contexts, batch.scheduler, batch.runner
        )._clean_stale_outputs(paths)
        self.assertEqual(removed, ("old.s4p",))
        self.assertTrue(os.path.isfile(f"{paths.run_dir}/keepme.txt"), "外层文件不许动")
        self.assertTrue(os.path.isdir(f"{paths.ewave_dir}/sub"), "子目录不递归删")


# --------------------------------------------------------------------------
# 有界并发
# --------------------------------------------------------------------------


class BoundedConcurrency(_TempRootTest):
    """§12：同时在飞的 job 数有上限（配额才是瓶颈，D11）。"""

    def _peak_in_flight(self, max_parallel: int) -> int:
        batch = _build(self._root(), max_parallel=max_parallel)
        driver = batch.driver()
        peak = 0
        for report in _drive(driver):
            in_flight = report.counts["pending"] + report.counts["running"]
            peak = max(peak, in_flight)
        self.assertEqual(driver.summary()["done"], 12, "跑不完的话峰值没有意义")
        return peak

    def test_never_more_than_max_parallel_in_flight(self) -> None:
        self.assertEqual(self._peak_in_flight(2), 2)

    def test_negative_a_bigger_bound_really_puts_more_in_flight(self) -> None:
        """★ 反向：把上限调大，峰值必须跟着变大 —— 否则上一条可能只是"提交得慢"而已。"""
        self.assertEqual(self._peak_in_flight(6), 6)


# --------------------------------------------------------------------------
# dry-run
# --------------------------------------------------------------------------


class DryRunTouchesNothing(_TempRootTest):
    """D8：dry-run 只打印 argv 和落地目录，**不提交、不建目录、不删文件**。"""

    def setUp(self) -> None:
        super().setUp()
        self.batch = _build(self._root(), dry_run=True)
        self.driver = self.batch.driver()
        self.reports = _drive(self.driver)

    def test_one_pass_and_it_is_finished(self) -> None:
        self.assertEqual(len(self.reports), 1)
        self.assertTrue(self.reports[0].finished)

    def test_nothing_executed_nothing_submitted(self) -> None:
        self.assertEqual(self.batch.runner.calls, [])
        self.assertEqual(self.batch.scheduler.submit_calls, 0)
        self.assertEqual(self.batch.runner.written, [])

    def test_no_file_hit_the_disk(self) -> None:
        self.assertFalse(os.path.exists(f"{self.batch.batch_dir}/batch.json"))
        self.assertFalse(os.path.exists(f"{self.batch.batch_dir}/runs"))
        self.assertFalse(os.path.exists(f"{self.batch.batch_dir}/gdsout"))

    def test_every_command_is_reported(self) -> None:
        """12 条阶段 2 的命令 + 2 条阶段 1 的命令，全都播出来给人看。"""
        planned = [e for e in self.batch.events if e.kind is EventKind.PLANNED]
        stage2 = [e for e in planned if e.run_id]
        self.assertEqual(len(stage2), 12)
        self.assertEqual({e.run_id for e in stage2}, set(RUN_IDS))
        self.assertEqual(len([e for e in planned if not e.run_id]), 2)
        for event in stage2:
            self.assertIn("--workDir=", event.message)
            self.assertIn(FAKE_EWAVE_BIN, event.message)
        for run in self.batch.state.runs:
            self.assertIs(run.status, RunStatus.READY, "dry-run 不改状态")
            self.assertTrue(run.argv)

    def test_negative_a_real_run_does_write_those_files(self) -> None:
        """★ 反向：**同一条构造路径**，只把 dry_run 关掉 —— 那些文件必须出现。

        没有这条，"batch.json 不存在"可能只是因为路径写错了。
        """
        batch = _build(self._root(), dry_run=False)
        _drive(batch.driver())
        self.assertTrue(os.path.isfile(f"{batch.batch_dir}/batch.json"))
        self.assertTrue(os.path.isdir(f"{batch.batch_dir}/runs"))
        self.assertGreater(len(batch.runner.calls), 0)
        self.assertEqual(batch.scheduler.submit_calls, 12)


# --------------------------------------------------------------------------
# 取消
# --------------------------------------------------------------------------


class CancelStopsTheBatch(_TempRootTest):
    def setUp(self) -> None:
        super().setUp()
        self.batch = _build(self._root(), max_parallel=3)
        self.driver = self.batch.driver()
        self.driver.tick()  # 阶段 1 + 提交前 3 个
        self.in_flight = [
            r.run_id
            for r in self.batch.state.runs
            if r.status in (RunStatus.PENDING, RunStatus.RUNNING)
        ]
        self.driver.cancel()

    def test_the_kill_point_was_meaningful(self) -> None:
        self.assertEqual(len(self.in_flight), 3)

    def test_in_flight_runs_are_recorded_as_cancelled(self) -> None:
        """`RunStatus` 没有 cancelled 态（恰好 6 个）⇒ 记成 failed，但 message 里说清楚。"""
        for run_id in self.in_flight:
            run = next(r for r in self.batch.state.runs if r.run_id == run_id)
            self.assertIs(run.status, RunStatus.FAILED)
            self.assertIn("取消", run.message)

    def test_the_rest_is_skipped_and_nothing_more_is_submitted(self) -> None:
        submitted_before = self.batch.scheduler.submit_calls
        report = self.driver.tick()
        self.assertTrue(report.finished)
        self.assertEqual(self.batch.scheduler.submit_calls, submitted_before)
        counts = self.driver.summary()
        self.assertEqual(counts["skipped"], 9)
        self.assertEqual(counts["failed"], 3)
        self.assertEqual(counts["ready"], 0)

    def test_negative_without_cancel_the_batch_keeps_going(self) -> None:
        """★ 反向：不按取消，同一条路径跑到底 ⇒ 12 个全 done。"""
        batch = _build(self._root(), max_parallel=3)
        driver = batch.driver()
        driver.tick()
        _drive(driver)
        self.assertEqual(driver.summary()["done"], 12)


# --------------------------------------------------------------------------
# 调度器查不到 job 时的兜底
# --------------------------------------------------------------------------


class _ForgetfulScheduler:
    """提交照走 `FakeScheduler`，但 `poll` 永远说"查不到"。

    模拟 LSF 系队列把结束的作业**彻底忘掉**那种行为（`sched/donau.py` 的交接报告里
    把它列为"没把握的地方"之一）。只等状态的 driver 会把 run 永远挂在"在飞"里。
    """

    def __init__(self, inner: FakeScheduler) -> None:
        self.inner = inner
        self.poll_calls = 0

    @property
    def submit_calls(self) -> int:
        return self.inner.submit_calls

    @property
    def plans(self) -> dict:
        return self.inner.plans

    def submit(self, plan: CommandPlan, *, resources: str = "", name: str = "") -> Job:
        return self.inner.submit(plan, resources=resources, name=name)

    def poll(self, jobs):  # type: ignore[no-untyped-def]
        self.poll_calls += 1
        return {}

    def cancel(self, job: Job) -> bool:
        return self.inner.cancel(job)


class UnknownJobFallsBackToTheProducts(_TempRootTest):
    """查不到 job 时：先忍几拍，然后**去看磁盘**（问不到 ≠ 失败，也 ≠ 成功）。"""

    def _run_until_settled(self, *, write_products: bool) -> tuple[_Batch, DriverProtocol]:
        inner_runner = FakeRunner(port_count=PORT_COUNT)
        inner = FakeScheduler(inner_runner)
        batch = _build(
            self._root(),
            corners=("typical",),
            temps=("-40.0",),
            cells=(("dA", FAKE_CELL_A),),
            runner=inner_runner,
            scheduler=_ForgetfulScheduler(inner),
        )
        driver = batch.driver()
        driver.tick()  # 阶段 1 + 提交
        run = batch.state.runs[0]
        self.assertIs(run.status, RunStatus.PENDING)
        if write_products:
            # 作业其实早就跑完了（产物在盘上），只是队列把它忘了。
            inner_runner.run(run.argv, cwd=run.work_dir)
        _drive(driver)
        return batch, driver

    def test_products_verify_so_it_is_done(self) -> None:
        batch, driver = self._run_until_settled(write_products=True)
        self.assertEqual(driver.summary()["done"], 1)
        self.assertGreaterEqual(batch.scheduler.poll_calls, driver_mod._UNKNOWN_POLL_LIMIT)
        self.assertEqual(batch.scheduler.submit_calls, 1, "兜底判 done 之后不许再提交一次")

    def test_negative_no_products_so_it_fails(self) -> None:
        """★ 反向：同一条路径，只是磁盘上什么都没有 ⇒ 判 failed（而不是永远挂着）。"""
        batch, driver = self._run_until_settled(write_products=False)
        self.assertEqual(driver.summary()["failed"], 1)
        run = batch.state.runs[0]
        self.assertIn("查不到", run.message)

    def test_it_waits_before_giving_up(self) -> None:
        """兜底不是第一拍就下手 —— 调度器短暂查不到是常事（`SchedulerProtocol.poll` 的原话）。"""
        inner_runner = FakeRunner(port_count=PORT_COUNT)
        batch = _build(
            self._root(),
            corners=("typical",),
            temps=("-40.0",),
            cells=(("dA", FAKE_CELL_A),),
            runner=inner_runner,
            scheduler=_ForgetfulScheduler(FakeScheduler(inner_runner)),
        )
        driver = batch.driver()
        driver.tick()
        for _ in range(driver_mod._UNKNOWN_POLL_LIMIT - 1):
            driver.tick()
            self.assertIs(batch.state.runs[0].status, RunStatus.PENDING, "还在忍")
        driver.tick()
        self.assertIs(batch.state.runs[0].status, RunStatus.FAILED)


# --------------------------------------------------------------------------
# run_batch
# --------------------------------------------------------------------------


class RunBatchDrivesTheSameTick(_TempRootTest):
    """CLI 的 `while` 驱动。GUI 用 `after()` 驱动**同一个 `tick()`** —— 同一份 driver 代码。"""

    def test_exit_code_zero_when_everything_is_done(self) -> None:
        batch = _build(self._root())
        self.assertEqual(run_batch(batch.driver(), poll_interval=0.0, max_seconds=120.0), 0)

    def test_exit_code_nonzero_when_something_failed(self) -> None:
        """★ 反向：**同一条构造路径**，只多了那四条失败模式 ⇒ 退出码非 0。"""
        batch = _build(self._root(), modes=dict(BATCH_MODES))
        self.assertEqual(run_batch(batch.driver(), poll_interval=0.0, max_seconds=120.0), 1)
        self.assertEqual(batch.statuses(), EXPECTED_FINAL_STATUS)

    def test_it_does_not_sleep_when_the_interval_is_zero(self) -> None:
        """`poll_interval<=0` ⇒ 一次都不 sleep（12-run 假批次要在毫秒级跑完）。"""
        calls: list[float] = []
        original = driver_mod.time.sleep
        driver_mod.time.sleep = lambda seconds: calls.append(seconds)  # type: ignore[assignment]
        try:
            batch = _build(self._root())
            self.assertEqual(run_batch(batch.driver(), poll_interval=0.0, max_seconds=120.0), 0)
        finally:
            driver_mod.time.sleep = original  # type: ignore[assignment]
        self.assertEqual(calls, [])

    def test_max_seconds_is_a_fuse_not_a_verdict(self) -> None:
        """保险丝到点就回来，状态不许被改成假的成功。"""
        batch = _build(self._root(), max_parallel=1)
        code = run_batch(batch.driver(), poll_interval=0.0, max_seconds=0.0)
        self.assertEqual(code, 1)
        self.assertLess(sum(1 for r in batch.state.runs if r.status is RunStatus.DONE), 12)


# --------------------------------------------------------------------------
# 确定性
# --------------------------------------------------------------------------


class Determinism(_TempRootTest):
    """同一份输入两次跑，结果逐字相同 —— 无人值守时"不可复现"等于"查不了"。"""

    def _one_pass(self) -> tuple[dict[str, str], list[str]]:
        batch = _build(self._root(), modes=dict(BATCH_MODES))
        _drive(batch.driver())
        return batch.statuses(), batch.submitted_run_ids()

    def test_same_input_gives_the_same_table_and_the_same_submit_order(self) -> None:
        first_table, first_order = self._one_pass()
        second_table, second_order = self._one_pass()
        self.assertEqual(first_table, second_table)
        self.assertEqual(first_order, second_order)
        self.assertEqual(len(first_order), 12)


# --------------------------------------------------------------------------
# 真 runner（本机唯一起真进程的地方 —— 跑的是 python，不是 eWave）
# --------------------------------------------------------------------------


class SubprocessRunnerRealProcess(unittest.TestCase):
    """`SubprocessRunner` 的协议行为。

    ⚠️ 本机永远没有 `ewave` / `strmout` / `dsub`（CLAUDE.md 硬约束 3），
    所以这里跑的是 `sys.executable` —— 验的是 `RunnerProtocol` 的约定，不是 argv 怎么拼
    （那是 `core.cmd` 的活，那边有 golden 测试对着真实生产命令验）。
    """

    def _python(self, code: str) -> list[str]:
        return [sys.executable, "-c", code]

    def test_captures_stdout_and_returncode(self) -> None:
        result = SubprocessRunner().run(self._python("print('alpha'); print('beta')"))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.splitlines(), ["alpha", "beta"])
        self.assertFalse(result.timed_out)
        self.assertFalse(result.cancelled)

    def test_on_line_gets_every_line(self) -> None:
        lines: list[str] = []
        SubprocessRunner().run(self._python("print('a'); print('b'); print('c')"), on_line=lines.append)
        self.assertEqual([line for line in lines if line in ("a", "b", "c")], ["a", "b", "c"])

    def test_nonzero_exit_is_reported_not_raised(self) -> None:
        result = SubprocessRunner().run(self._python("import sys; sys.exit(3)"))
        self.assertEqual(result.returncode, 3)

    def test_stderr_is_kept_separate(self) -> None:
        result = SubprocessRunner().run(
            self._python("import sys; sys.stderr.write('boom\\n'); print('ok')")
        )
        self.assertIn("ok", result.stdout)
        self.assertIn("boom", result.stderr)

    def test_env_increment_reaches_the_child(self) -> None:
        """`env` 是**增量**，实现方自己合并到 `os.environ` 的副本上（Protocol 的原话）。"""
        result = SubprocessRunner().run(
            self._python("import os; print(os.environ['EWB_TEST_MARKER'], os.environ.get('PATH') is not None)"),
            env={"EWB_TEST_MARKER": "42"},
        )
        self.assertIn("42 True", result.stdout)
        self.assertNotIn("EWB_TEST_MARKER", os.environ, "不许污染本进程的环境")

    def test_missing_tool_raises_tool_missing_error(self) -> None:
        with self.assertRaises(ToolMissingError):
            SubprocessRunner().run(["ewave_batch_no_such_tool_zzz", "--version"])

    def test_empty_argv_raises(self) -> None:
        with self.assertRaises(ToolMissingError):
            SubprocessRunner().run([])

    def test_cancel_before_start_does_not_spawn(self) -> None:
        """`cancel()` 一开始就是 True ⇒ 连 fork 都不做（起了再杀会在红区留半份产物）。"""
        marker = os.path.join(tempfile.gettempdir(), "ewb_should_never_exist.txt")
        if os.path.exists(marker):  # pragma: no cover
            os.remove(marker)
        result = SubprocessRunner().run(
            self._python(f"open({marker!r}, 'w').write('x')"), cancel=lambda: True
        )
        self.assertTrue(result.cancelled)
        self.assertFalse(os.path.exists(marker))

    def test_cancel_false_still_runs(self) -> None:
        """★ 反向：`cancel()` 返回 False ⇒ 照常跑完（证明上一条不是"永远不跑"）。"""
        result = SubprocessRunner().run(self._python("print('ran')"), cancel=lambda: False)
        self.assertFalse(result.cancelled)
        self.assertIn("ran", result.stdout)

    def test_timeout_marks_timed_out_instead_of_raising(self) -> None:
        """超时**不抛异常**，置 `timed_out=True` 返回（Protocol：已经拿到的输出要看得见）。"""
        runner = SubprocessRunner(wait_slice=0.01)
        result = runner.run(self._python("import time; time.sleep(60)"), timeout=0.3)
        self.assertTrue(result.timed_out)
        self.assertLess(result.duration_seconds, 30, "超时了就该马上回来")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
