"""`ewave_batch.sched.fake` 的测试 —— **本阶段的验收契约就写在这份文件里。**

BRIEF §10 实测出来三条「失败信号不可靠」：`exit=0` 但崩了、0 字节产物照样报 "done"、
写失败被吞。于是 `done` 的判据是 `core.layout.verify_run_outputs`（存在 + 非空 + 端口数对）。
本文件要证明的就是这一句：**每个失败模式，拿真实的验收器去验真实的磁盘产物，判决必须对。**

⚠️ 一条自我约束：这里**从不 mock `verify_run_outputs`**，也不给它喂假的 `VerifyReport`。
FakeRunner 把文件真的写到 `tmp` 里，验收器走的是它自己那条 `os.path.getsize` 的代码路径。
不这么做的话，"验收器是对的" 就成了实现方自己说的 —— 那正是防自证要防的东西。

四条配方（`docs/OVERNIGHT.md`）在这份文件里的落点：

1. **关键测试** = `FailureModeVerdicts` 里那六条 + `TwelveRunFakeBatch`：
   断言「真实验收器对真实产物的判决」等于期望值；
2. **期望值来源**：全部**手写字面量**，逐条注明出处
   （BRIEF §10 的实测表 / §5 的官方布局 / `core.layout.verify_run_outputs` 的验收契约）。
   没有一处拿被测代码自己的输出当期望值；
3. **反向验证**：每条关键测试配一条 `_negative`，**共用同一条输入构造路径**（`_act()`），
   只改 `mode` 一个入参。最要紧的一对是
   `test_success_verifies_done` ←→ `test_success_verifies_done_negative_zero_byte`：
   少了正向那条，"一律判 failed" 的验收器也能让其余五条全绿 —— 那种验收器等于没有；
4. **计数断言**：写出来的文件条数、`sparam_files` 条数、`reasons` 条数、
   12-run 批次里 job 状态与验收判决的**差值**（★ 那个 4 就是"只看退出码会误判几个"），
   以及 `ModeCoverageMeta` 里"每个枚举成员都有测试覆盖"的遍历断言。

🚨 本文件零站点标识符：library / cell / view / 端口名 / 路径全是显式假值，
端口数用一个明显合成的小数字（真实 design 的端口数是站点信息）。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from dataclasses import dataclass

from ewave_batch.__main__ import normalize_signature
from ewave_batch.core import layout
from ewave_batch.core.matrix import expand_runs
from ewave_batch.model import (
    Axis,
    AxisValue,
    BatchOptions,
    CommandPlan,
    Design,
    Job,
    JobState,
    PLACEHOLDER_VALUE,
    PlanContext,
    Run,
    RunPaths,
    RunnerProtocol,
    RunResult,
    SchedulerError,
    SchedulerProtocol,
    SiteFacts,
    ToolMissingError,
)
from ewave_batch.sched import fake
from ewave_batch.sched.fake import FakeFailureMode, FakeRunner, FakeScheduler
from ewave_batch.tools import strmout
from ewave_batch.tools.ewave import build_ewave_plan

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

PORT_COUNT = 4
"""假产物的端口数。**故意是个小的合成值** —— 真实 design 的端口数是站点信息（硬约束 1b）。
`WRONG_PORT_COUNT` 模式会产出 `PORT_COUNT - 1` 端口的产物（少一个 pin ⇒ 全体编号平移，D1b）。"""

EXPECTED_FILES_PER_SUCCESSFUL_RUN = 11
"""一次成功的假 run 在 `<corner>_<temp>/` 里留下几个文件。

**手写字面量**，出处是 BRIEF §5「官方流程的既有布局」那棵树的子集：
4 个产物（`.sNp` / `_sample.sNp` / `.yNp` / `_sample.yNp`）
+ 4 个 mesh 中间件（pmrg 两件 + pmsh 两件）+ `resist.rst` + 2 份日志。
官方那层还有 `mesh.log` / `emesh_mrg.log` / `pmrg.gtxt_bak.mrg`，假 runner 不写 ——
它们对验收和归档都没有影响，写出来只会让这个数字显得更权威而已。
"""


def _facts() -> SiteFacts:
    """最小站点坐标。字段全是假路径 —— `SiteFacts` 里装的全是站点身份，
    源码和测试里都只许出现显式假值（硬约束 1b）。"""
    return SiteFacts(
        ewave_bin=FAKE_EWAVE_BIN,
        strmout_bin=FAKE_STRMOUT_BIN,
        layer_map=FAKE_LAYER_MAP,
    )


def _designs() -> list[Design]:
    return [
        Design(library=FAKE_LIB, cell=FAKE_CELL_A, view=FAKE_VIEW, key="dA"),
        Design(library=FAKE_LIB, cell=FAKE_CELL_B, view=FAKE_VIEW, key="dB"),
    ]


def _axes() -> list[Axis]:
    """corner × temperature = 2 × 3。

    ⚠️ 真实的 corner 轴还要同时改 `--emssTechFile`（BRIEF §7「corner 轴要同时改两处」），
    这里**故意省掉**：那个 flag 的值要靠 `core.discover.ptxt_path_for_corner` 解析站点路径，
    而本文件测的是调度与验收，不是命令拼装（那是 `test_cmd_golden.py` 的活）。
    省掉它不影响本文件的任何判据 —— 我们只需要 argv 里有 `--corner` / `--temperature`，
    因为 eWave 那层目录名只由这两个决定。
    """
    return [
        Axis(
            name="corner",
            values=(
                AxisValue("typical", flags={"--corner": PLACEHOLDER_VALUE}),
                AxisValue("cworst", flags={"--corner": PLACEHOLDER_VALUE}),
            ),
            flags=("--corner",),
            short="corner",
            encoded_in_ewave_dir=True,
        ),
        Axis(
            name="temperature",
            values=tuple(
                AxisValue(value, flags={"--temperature": PLACEHOLDER_VALUE})
                for value in ("-40.0", "25.0", "125.0")
            ),
            flags=("--temperature",),
            short="temp",
            encoded_in_ewave_dir=True,
        ),
    ]


@dataclass(frozen=True)
class _Case:
    """一个 run 的全套东西。**正反两向共用这一条构造路径。**"""

    design: Design
    run: Run
    paths: RunPaths
    plan: CommandPlan


def _cases(batch_dir: str) -> list[_Case]:
    """走**真实的**核心链路造出 12 个 run：`expand_runs` → `compute_run_paths` →
    `tools.ewave.build_ewave_plan`。

    刻意不手搓 argv：本文件要证明的是"假 runner 认得出真命令行、验收器验得了真产物"，
    手搓 argv 就会把这条接缝测没了 —— 而接缝正是并行开发最容易漂的地方。
    """
    designs = _designs()
    axes = _axes()
    runs = expand_runs(designs, axes)
    by_key = {design.key: design for design in designs}
    out: list[_Case] = []
    for run in runs:
        design = by_key[run.design_key]
        paths = layout.compute_run_paths(batch_dir, design, run)
        run.work_dir = paths.run_dir
        ctx = PlanContext(
            design=design,
            facts=_facts(),
            axes=tuple(axes),
            options=BatchOptions(),
            batch_dir=batch_dir,
        )
        out.append(_Case(design=design, run=run, paths=paths, plan=build_ewave_plan(run, ctx)))
    return out


class _TempBatchTest(unittest.TestCase):
    """每个测试一个干净的临时根目录，`_batch()` 再往下切互不影响的批次目录。"""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = holder.name.replace("\\", "/")
        self._serial = 0

    def _batch(self) -> str:
        """一个全新的批次目录。同一个测试里正反两向各要一个，否则上一次的产物会串味。"""
        self._serial += 1
        return f"{self.root}/b{self._serial}"

    def _read(self, path: str) -> str:
        with open(path, encoding="utf-8") as handle:
            return handle.read()


# --------------------------------------------------------------------------
# ★ 关键测试：六个失败模式，各自的验收判决
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Act:
    case: _Case
    runner: FakeRunner
    result: RunResult
    verdict: layout.VerifyReport


class FailureModeVerdicts(_TempBatchTest):
    """一个模式一条测试：**真实的** `verify_run_outputs` 对**真实落盘的**产物给出什么判决。

    出处：BRIEF §10「三条失败信号合起来就是调度器的验收契约」那张表。
    """

    def _act(
        self,
        mode: FakeFailureMode,
        *,
        expected_port_count: int | None = None,
        ports: tuple[str, ...] = (),
    ) -> _Act:
        """**唯一的输入构造路径。** 正反两向都走它，只改 `mode`（或期望端口数）一个入参 ——
        排除"换了个东西测"。"""
        case = _cases(self._batch())[0]
        if ports:
            case.run.ports = ports
        runner = FakeRunner(mode, port_count=PORT_COUNT)
        result = runner.run(case.plan.argv, cwd=case.plan.cwd)
        verdict = layout.verify_run_outputs(
            case.paths, case.run, expected_port_count=expected_port_count
        )
        return _Act(case=case, runner=runner, result=result, verdict=verdict)

    # ---- SUCCESS：对照组，没有它上面全绿也说明不了任何事 ------------------

    def test_success_verifies_done(self) -> None:
        act = self._act(FakeFailureMode.SUCCESS)
        self.assertEqual(act.result.returncode, 0)
        self.assertTrue(act.verdict.ok, act.verdict.reasons)
        self.assertEqual(act.verdict.reasons, ())
        # 端口数来自产物后缀 `.s4p` —— 期望值就是我们喂进去的 PORT_COUNT。
        self.assertEqual(act.verdict.port_count, PORT_COUNT)
        # 计数断言：`.sNp` 恰好两份（正式的 + `_sample` 的，BRIEF §5 官方布局）。
        # `.yNp` 不算 —— `_is_sparam_name` 只认 S 参数（D5：用户只要 S 参数）。
        self.assertEqual(len(act.verdict.sparam_files), 2)
        self.assertGreater(act.verdict.total_bytes, 0)
        # 计数断言：落盘文件条数 == 手写的 EXPECTED_FILES_PER_SUCCESSFUL_RUN。
        self.assertEqual(len(act.runner.written), EXPECTED_FILES_PER_SUCCESSFUL_RUN)

    def test_success_verifies_done_negative_zero_byte(self) -> None:
        """同一条构造路径，只把 mode 改坏 ⇒ 判决必须翻过来。

        **这条是整份文件里最重要的一条**：没有它，一个"永远返回 ok=True"的验收器
        也能让 `test_success_verifies_done` 绿；没有 `test_success_verifies_done`，
        一个"永远返回 ok=False"的验收器又能让其余五条全绿。两条一起才是判据。
        """
        act = self._act(FakeFailureMode.ZERO_BYTE_OUTPUT)
        self.assertFalse(act.verdict.ok)
        self.assertNotEqual(act.verdict.reasons, ())

    # ---- 坑 1：exit=0 但崩了 ---------------------------------------------

    def test_exit_zero_but_crashed_is_failed(self) -> None:
        """实测（§10 step3）：emsolver abort 了，payload 打的仍是 `ewave exit=0`，零产物。

        ⇒ 退出码说成功、验收必须说失败。**这条要是绿不了，本工具就没有存在价值。**
        """
        act = self._act(FakeFailureMode.EXIT_ZERO_BUT_CRASHED)
        self.assertEqual(act.result.returncode, 0)  # ★ 退出码说"成功"
        self.assertFalse(act.verdict.ok)  # ★ 验收说"失败"
        self.assertEqual(act.verdict.sparam_files, ())
        self.assertEqual(act.verdict.total_bytes, 0)
        self.assertEqual(len(act.verdict.reasons), 1)
        # 目录建起来了、日志也在 —— "文件存在"这条信号同样不可信。
        self.assertTrue(os.path.isdir(act.case.paths.ewave_dir))
        self.assertTrue(os.path.isfile(f"{act.case.paths.ewave_dir}/ewave.log"))
        # `resist.rst == 0 字节` 是 §10 那次事故的确定指纹（logparse P4 要认它）。
        self.assertEqual(os.path.getsize(f"{act.case.paths.ewave_dir}/resist.rst"), 0)

    def test_exit_zero_but_crashed_negative_success(self) -> None:
        """同一条路径换成 SUCCESS ⇒ 必须判 done。证明上一条不是"一律 failed"。"""
        act = self._act(FakeFailureMode.SUCCESS)
        self.assertEqual(act.result.returncode, 0)
        self.assertTrue(act.verdict.ok, act.verdict.reasons)

    # ---- 坑 2：0 字节产物 + 日志报 done -----------------------------------

    def test_zero_byte_output_is_failed(self) -> None:
        """实测（§10）：`eresist` 打印 "Execute eresist done."，写出来的是 0 字节文件。

        ⇒ 文件**在**、日志**说 done**、退出码**是 0**，三条信号全说成功，而产物是空的。
        只有"非空"这条判据能抓住它。
        """
        act = self._act(FakeFailureMode.ZERO_BYTE_OUTPUT)
        self.assertEqual(act.result.returncode, 0)
        self.assertFalse(act.verdict.ok)
        # 文件确实在（两份 .sNp），但一个字节都没有。
        self.assertEqual(len(act.verdict.sparam_files), 2)
        self.assertEqual(act.verdict.total_bytes, 0)
        # 计数断言：两份空文件 ⇒ 恰好两条原因，一份一条。
        self.assertEqual(len(act.verdict.reasons), 2)
        for path in act.verdict.sparam_files:
            self.assertEqual(os.path.getsize(path), 0)

    def test_zero_byte_output_negative_non_empty(self) -> None:
        """同一条路径、同样两份 `.sNp`，只是这次非空 ⇒ 判 done。

        正反两条只差"文件里有没有字节"，所以绿/红的差异只能来自"非空"这条判据本身。
        """
        act = self._act(FakeFailureMode.SUCCESS)
        self.assertEqual(len(act.verdict.sparam_files), 2)
        self.assertGreater(act.verdict.total_bytes, 0)
        self.assertTrue(act.verdict.ok, act.verdict.reasons)

    # ---- 坑 3：写失败被吞（零错误输出）------------------------------------

    def test_swallowed_write_failure_is_failed(self) -> None:
        """实测（§10 根因）：`$HOME` 配额爆了，写出来的是空文件，
        而整条链路上**没有一行错误** —— stdout / stderr / 日志全是 "done"。

        ⇒ "没报错" ≠ 成功。这条测试的重点是那三个 `assertNotIn`：
        任何"日志里没有 error 就算过"的判据，在这里都会放行一个空手而归的 run。
        """
        act = self._act(FakeFailureMode.SWALLOWED_WRITE_FAILURE)
        self.assertEqual(act.result.returncode, 0)
        self.assertEqual(act.result.stderr, "")
        self.assertNotIn("error", act.result.stdout.lower())
        self.assertNotIn("fail", act.result.stdout.lower())
        log_text = self._read(f"{act.case.paths.ewave_dir}/ewave.log").lower()
        self.assertNotIn("error", log_text)
        self.assertNotIn("fail", log_text)
        # 而产物根本没写出来。
        self.assertFalse(act.verdict.ok)
        self.assertEqual(act.verdict.sparam_files, ())
        self.assertEqual(os.path.getsize(f"{act.case.paths.ewave_dir}/resist.rst"), 0)

    def test_swallowed_write_failure_negative_loud_error(self) -> None:
        """同一条路径换成"真的报错"⇒ 错误文本必须出现在 stderr 里。

        没有这条，上面那三个 `assertNotIn` 可能只是因为 FakeRunner 压根不写 stderr ——
        那样它们就是空过的断言（"空得非常好看"）。
        """
        act = self._act(FakeFailureMode.NONZERO_EXIT)
        self.assertNotEqual(act.result.stderr, "")
        self.assertIn("error", act.result.stderr.lower())

    # ---- 坑 4：端口数不对（D1b 的静默平移）--------------------------------

    def test_wrong_port_count_is_failed(self) -> None:
        """产物齐、非空、退出码 0 —— 只有端口数少了一个。

        对应 BRIEF §5「`--all` 的代价」：设计师删掉一个 pin ⇒ 所有端口编号平移 ⇒
        归档的 `.sNp` 和现成的 nport 全部错位，**而且静默**。
        """
        act = self._act(FakeFailureMode.WRONG_PORT_COUNT, expected_port_count=PORT_COUNT)
        self.assertEqual(act.result.returncode, 0)
        self.assertEqual(len(act.verdict.sparam_files), 2)
        self.assertGreater(act.verdict.total_bytes, 0)  # 非空，"验非空"这条抓不到它
        self.assertEqual(act.verdict.port_count, PORT_COUNT - 1)  # 手写：少一个 pin
        self.assertFalse(act.verdict.ok)
        self.assertEqual(len(act.verdict.reasons), 1)

    def test_wrong_port_count_negative_matching_count(self) -> None:
        """同一条路径、同一个 `expected_port_count`，只把 mode 换成 SUCCESS ⇒ 判 done。

        证明端口数校验不是"给了期望值就一律报错"。
        """
        act = self._act(FakeFailureMode.SUCCESS, expected_port_count=PORT_COUNT)
        self.assertEqual(act.verdict.port_count, PORT_COUNT)
        self.assertTrue(act.verdict.ok, act.verdict.reasons)

    def test_wrong_port_count_caught_via_run_ports(self) -> None:
        """不给 `expected_port_count`，改从 `Run.ports` 推 —— 另一条分支，同样要抓到。

        driver 手上的期望值有两个来源：`BatchOptions.verify_port_count` 走显式参数，
        批次内互相比对走 `Run.ports`。两条都要能拦。
        """
        ports = tuple(f"pin{i}" for i in range(PORT_COUNT))
        act = self._act(FakeFailureMode.WRONG_PORT_COUNT, ports=ports)
        self.assertEqual(len(act.verdict.ports), PORT_COUNT)
        self.assertEqual(act.verdict.port_count, PORT_COUNT - 1)
        self.assertFalse(act.verdict.ok)

    def test_wrong_port_count_slips_through_without_expectation(self) -> None:
        """⚠️ **诚实的负面结论**：既不给 `expected_port_count`、`Run.ports` 又是空的时候，
        端口数不对**验不出来**（产物存在且非空，验收器无从知道该有几个端口）。

        写下来是因为它是一条真实的使用约束：`BatchOptions.verify_port_count` 关掉，
        或者 driver 忘了把期望值传下来，D1b 那个静默平移就会一路通过。
        把它测成"应该抓到"是自欺欺人；把它测出来，将来谁改了行为都会看到这条红。
        """
        act = self._act(FakeFailureMode.WRONG_PORT_COUNT)
        self.assertEqual(act.verdict.port_count, PORT_COUNT - 1)
        self.assertTrue(act.verdict.ok)

    # ---- 对照组：真的报错，退非 0 ----------------------------------------

    def test_nonzero_exit_is_failed(self) -> None:
        """错误信号可靠的那一次：退 3、stderr 有文本、产物没有。

        存在的意义是划边界 —— 验收器不能只看退出码，但也不能对退出码视而不见。
        """
        act = self._act(FakeFailureMode.NONZERO_EXIT)
        self.assertEqual(act.result.returncode, 3)  # 手写：_OUTCOMES 里给 NONZERO_EXIT 定的
        self.assertNotEqual(act.result.stderr, "")
        self.assertFalse(act.verdict.ok)
        self.assertEqual(act.verdict.sparam_files, ())

    def test_nonzero_exit_negative_zero_exit(self) -> None:
        """同一条路径换成 SUCCESS ⇒ 退 0 且判 done。"""
        act = self._act(FakeFailureMode.SUCCESS)
        self.assertEqual(act.result.returncode, 0)
        self.assertEqual(act.result.stderr, "")
        self.assertTrue(act.verdict.ok, act.verdict.reasons)


# --------------------------------------------------------------------------
# 日志措辞不可信 —— 用"逐字相同"把这句话钉住
# --------------------------------------------------------------------------


class LogTextCannotTellSuccessFromFailure(_TempBatchTest):
    """成功那次和三种失败那次写出来的 `ewave.log` **逐字相同**。

    这不是实现偷懒，是 BRIEF §10 的实测事实：`eresist` 写出 0 字节文件之后，
    照样打印 "Execute eresist done."。日志分辨不出成败 ⇒ 验收只能验产物。
    """

    def _log_of(self, mode: FakeFailureMode) -> str:
        case = _cases(self._batch())[0]
        FakeRunner(mode, port_count=PORT_COUNT).run(case.plan.argv, cwd=case.plan.cwd)
        return self._read(f"{case.paths.ewave_dir}/ewave.log")

    def test_done_wording_is_identical_across_modes(self) -> None:
        success = self._log_of(FakeFailureMode.SUCCESS)
        for mode in (
            FakeFailureMode.ZERO_BYTE_OUTPUT,
            FakeFailureMode.SWALLOWED_WRITE_FAILURE,
            FakeFailureMode.WRONG_PORT_COUNT,
        ):
            with self.subTest(mode=mode):
                self.assertEqual(self._log_of(mode), success)
        self.assertIn("Execute eresist done.", success)

    def test_done_wording_is_identical_across_modes_negative_crash_differs(self) -> None:
        """反向：崩溃那次的日志必须**不同**，且带得上 §10 抄回来的那条指纹。

        没有这条，上面那个 `assertEqual` 可能只是因为 FakeRunner 根本不写日志内容。
        """
        success = self._log_of(FakeFailureMode.SUCCESS)
        crashed = self._log_of(FakeFailureMode.EXIT_ZERO_BUT_CRASHED)
        self.assertNotEqual(crashed, success)
        self.assertIn("boost::archive::archive_exception", crashed)


# --------------------------------------------------------------------------
# RunnerProtocol 的约定
# --------------------------------------------------------------------------


class RunnerProtocolContract(_TempBatchTest):
    """`FakeRunner` 必须真的满足 `model.RunnerProtocol`（不只是"方法名对得上"）。"""

    def test_signature_matches_frozen_protocol(self) -> None:
        """逐字比签名。`@runtime_checkable` 的 isinstance 只看方法名在不在，挡不住参数漂移 ——
        `scripts/check.sh` 第 4 步的 self-test 也在比同一件事，这里再钉一遍是因为
        单测跑得比闸门勤。"""
        self.assertIsInstance(FakeRunner(), RunnerProtocol)
        self.assertEqual(
            normalize_signature(FakeRunner.run),
            normalize_signature(RunnerProtocol.run),
        )

    def test_cancel_writes_nothing(self) -> None:
        """`cancel()` 返回 True ⇒ 立刻收工，**一个文件都不写**。"""
        case = _cases(self._batch())[0]
        runner = FakeRunner(port_count=PORT_COUNT)
        result = runner.run(case.plan.argv, cwd=case.plan.cwd, cancel=lambda: True)
        self.assertTrue(result.cancelled)
        self.assertEqual(runner.written, [])
        self.assertFalse(os.path.isdir(case.paths.ewave_dir))

    def test_cancel_false_still_writes(self) -> None:
        """反向：`cancel()` 返回 False ⇒ 照常落盘。

        少了它，"取消时不写文件"可能只是因为 FakeRunner 在任何情况下都不写。
        """
        case = _cases(self._batch())[0]
        runner = FakeRunner(port_count=PORT_COUNT)
        result = runner.run(case.plan.argv, cwd=case.plan.cwd, cancel=lambda: False)
        self.assertFalse(result.cancelled)
        self.assertEqual(len(runner.written), EXPECTED_FILES_PER_SUCCESSFUL_RUN)

    def test_timeout_marks_timed_out_and_writes_nothing(self) -> None:
        """超时**不抛异常**（Protocol），置 `timed_out=True` 返回，产物没写出来。"""
        case = _cases(self._batch())[0]
        runner = FakeRunner(port_count=PORT_COUNT, duration_seconds=10.0)
        result = runner.run(case.plan.argv, cwd=case.plan.cwd, timeout=1.0)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.duration_seconds, 1.0)
        self.assertEqual(runner.written, [])

    def test_timeout_generous_enough_still_writes(self) -> None:
        """反向：给足时间就照常跑完（证明上一条不是"给了 timeout 就永远超时"）。"""
        case = _cases(self._batch())[0]
        runner = FakeRunner(port_count=PORT_COUNT, duration_seconds=10.0)
        result = runner.run(case.plan.argv, cwd=case.plan.cwd, timeout=60.0)
        self.assertFalse(result.timed_out)
        self.assertEqual(len(runner.written), EXPECTED_FILES_PER_SUCCESSFUL_RUN)

    def test_on_line_gets_every_stdout_line(self) -> None:
        """逐行喂 `on_line`（Protocol：别攒到最后）。计数断言：条数 == stdout 的行数。"""
        case = _cases(self._batch())[0]
        seen: list[str] = []
        runner = FakeRunner(port_count=PORT_COUNT)
        result = runner.run(case.plan.argv, cwd=case.plan.cwd, on_line=seen.append)
        self.assertEqual(seen, result.stdout.splitlines())
        self.assertEqual(len(seen), 3)  # 手写：_LOG_DONE 是三行

    def test_missing_tool_raises(self) -> None:
        """`missing_tools` 点名的程序 ⇒ `ToolMissingError`（Protocol 约定的那条）。"""
        case = _cases(self._batch())[0]
        runner = FakeRunner(missing_tools=("ewave",))
        with self.assertRaises(ToolMissingError):
            runner.run(case.plan.argv, cwd=case.plan.cwd)
        self.assertEqual(runner.written, [])

    def test_empty_argv_raises(self) -> None:
        with self.assertRaises(ToolMissingError):
            FakeRunner().run([])

    def test_unknown_command_writes_nothing(self) -> None:
        """认不出来的命令（既没有 `--workDir` 也没有 `-templateFile`）⇒ 不发明任何产物。"""
        runner = FakeRunner()
        result = runner.run(["/tmp/fakebin/whoami"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(runner.written, [])


# --------------------------------------------------------------------------
# 模式选择：键怎么匹配
# --------------------------------------------------------------------------


class ModeSelection(_TempBatchTest):
    """`modes` / `port_counts` 的键匹配规则：精确，或按 `/` 分段的后缀，最长者胜。

    规则本身要有测试（配方 4：凡是有"排除/忽略/前缀过滤"的地方）——
    尤其要断言它**没把不该命中的一起命中**。
    """

    def test_default_mode_when_nothing_matches(self) -> None:
        case = _cases(self._batch())[0]
        runner = FakeRunner(FakeFailureMode.SUCCESS, modes={"no/such/run": FakeFailureMode.NONZERO_EXIT})
        self.assertIs(runner.mode_for(case.plan.argv), FakeFailureMode.SUCCESS)

    def test_run_id_is_a_valid_key(self) -> None:
        """`Run.run_id` 正好是命令键的后缀 ⇒ driver 可以直接拿 run_id 当键。"""
        case = _cases(self._batch())[0]
        runner = FakeRunner(modes={case.run.run_id: FakeFailureMode.ZERO_BYTE_OUTPUT})
        self.assertIs(runner.mode_for(case.plan.argv), FakeFailureMode.ZERO_BYTE_OUTPUT)

    def test_exact_key_is_a_valid_key(self) -> None:
        case = _cases(self._batch())[0]
        key = runner_key = FakeRunner().command_key(case.plan.argv)
        self.assertEqual(key, case.paths.ewave_dir)  # 键就是产物目录
        runner = FakeRunner(modes={runner_key: FakeFailureMode.NONZERO_EXIT})
        self.assertIs(runner.mode_for(case.plan.argv), FakeFailureMode.NONZERO_EXIT)

    def test_short_key_hits_every_design_with_that_combination(self) -> None:
        """只写 `<corner>_<temp>` 会命中**所有 design** 的同名组合 —— 这是规则的直接后果，
        测出来免得有人以为它能指定单个 run。"""
        cases = _cases(self._batch())
        runner = FakeRunner(modes={"typical_-40_0": FakeFailureMode.NONZERO_EXIT})
        hit = [c for c in cases if runner.mode_for(c.plan.argv) is FakeFailureMode.NONZERO_EXIT]
        self.assertEqual(len(hit), 2)  # 手写：两个 design × 这一个组合
        self.assertEqual(sorted(c.run.design_key for c in hit), ["dA", "dB"])

    def test_longest_key_wins(self) -> None:
        """短键和长键同时命中时取更具体的那个 —— 确定性要求（同样输入永远同样输出）。"""
        cases = _cases(self._batch())
        target = cases[0]
        runner = FakeRunner(
            FakeFailureMode.SUCCESS,
            modes={
                "typical_-40_0": FakeFailureMode.NONZERO_EXIT,
                target.run.run_id: FakeFailureMode.ZERO_BYTE_OUTPUT,
            },
        )
        self.assertIs(runner.mode_for(target.plan.argv), FakeFailureMode.ZERO_BYTE_OUTPUT)
        # 另一个 design 的同名组合仍然走短键。
        other = next(c for c in cases if c.run.design_key == "dB" and c.run.ewave_dir == "typical_-40_0")
        self.assertIs(runner.mode_for(other.plan.argv), FakeFailureMode.NONZERO_EXIT)

    def test_key_does_not_match_a_partial_path_segment(self) -> None:
        """**不许前缀/子串误伤**：`_40_0` 不是 `typical_-40_0` 的合法后缀键。

        这条是 MVP 那个真 bug 的同型回归（`--sparam` 前缀吃掉了 `--sparamImpedance`）：
        匹配一旦从"按段"退化成"按字符"，一个键就会悄悄命中一堆 run，
        而失败模式装错了是查不出来的。
        """
        case = _cases(self._batch())[0]
        runner = FakeRunner(FakeFailureMode.SUCCESS, modes={"40_0": FakeFailureMode.NONZERO_EXIT})
        self.assertIs(runner.mode_for(case.plan.argv), FakeFailureMode.SUCCESS)

    def test_port_counts_are_per_run(self) -> None:
        """同一批次里两个 design 的端口数本来就不同（BRIEF §5：17 端口电感 vs 16 端口走线）。

        键 `"dB"` 走的是**中段**匹配（design 那一段），于是一条键盖住该 design 的全部 run。
        """
        cases = _cases(self._batch())
        a = next(c for c in cases if c.run.design_key == "dA")
        b = next(c for c in cases if c.run.design_key == "dB")
        runner = FakeRunner(port_count=PORT_COUNT, port_counts={"dB": 7})
        self.assertEqual(runner.port_count_for(a.plan.argv), PORT_COUNT)
        self.assertEqual(runner.port_count_for(b.plan.argv), 7)


# --------------------------------------------------------------------------
# FakeScheduler：Donau 侧的时间线
# --------------------------------------------------------------------------


class SchedulerTimeline(_TempBatchTest):
    """submit → pending ×N → running ×M → 终态。**靠"第几次 poll"推进，不靠墙钟，不 sleep。**

    `pending` 是 Donau 自己的词（已 dsub、在排队），"还没提交"叫 `ready` ——
    两者不许合并，resume 要靠它们区分"该提交"和"该轮询"（用户 2026-08-18 定）。
    """

    def _submit_one(self, **kwargs: object) -> tuple[_Case, FakeRunner, FakeScheduler, Job]:
        case = _cases(self._batch())[0]
        runner = FakeRunner(kwargs.pop("mode", FakeFailureMode.SUCCESS), port_count=PORT_COUNT)  # type: ignore[arg-type]
        scheduler = FakeScheduler(runner, **kwargs)  # type: ignore[arg-type]
        job = scheduler.submit(case.plan, resources="cpu=20;mem=100000", name=case.run.run_id)
        return case, runner, scheduler, job

    def test_signature_matches_frozen_protocol(self) -> None:
        scheduler = FakeScheduler()
        self.assertIsInstance(scheduler, SchedulerProtocol)
        for attr in ("submit", "poll", "cancel"):
            with self.subTest(attr=attr):
                self.assertEqual(
                    normalize_signature(getattr(FakeScheduler, attr)),
                    normalize_signature(getattr(SchedulerProtocol, attr)),
                )

    def test_timeline_pending_running_done(self) -> None:
        """状态序列和时间戳都是**手写字面量**：假时钟 = epoch + tick × 15s，
        submit 走一格、每次 poll 走一格（模块 docstring 的"不许 sleep"）。"""
        case, runner, scheduler, job = self._submit_one()
        self.assertEqual(job.job_id, "fake-0001")
        self.assertIs(job.state, JobState.PENDING)
        self.assertEqual(job.submitted_at, "2026-01-01T00:00:15Z")

        states = []
        for _ in range(4):
            states.append(scheduler.poll([job])[job.job_id].state)
        self.assertEqual(
            states,
            [JobState.PENDING, JobState.RUNNING, JobState.DONE, JobState.DONE],
        )
        final = scheduler.jobs[job.job_id]
        self.assertEqual(final.started_at, "2026-01-01T00:00:45Z")
        self.assertEqual(final.ended_at, "2026-01-01T00:01:00Z")
        self.assertEqual(final.exit_code, 0)

    def test_artifacts_appear_only_at_the_terminal_poll(self) -> None:
        """提交完成 ≠ 算完了。产物在走到终态那一拍才落盘 —— 这正是真实 dsub 的形状。"""
        case, runner, scheduler, job = self._submit_one()
        self.assertFalse(os.path.isdir(case.paths.ewave_dir))
        scheduler.poll([job])
        scheduler.poll([job])
        self.assertEqual(runner.calls, [])  # 还在排队/运行，一次都没执行
        self.assertFalse(os.path.isdir(case.paths.ewave_dir))
        scheduler.poll([job])
        self.assertEqual(len(runner.calls), 1)
        self.assertTrue(os.path.isdir(case.paths.ewave_dir))
        # 再 poll 也不许重跑（幂等）——重跑会把产物写第二遍，还会多烧一次配额。
        scheduler.poll([job])
        scheduler.poll([job])
        self.assertEqual(len(runner.calls), 1)

    def test_job_state_done_does_not_mean_run_done(self) -> None:
        """★ `JobState.DONE` 只代表进程结束了。eWave 崩了也 `exit=0` ⇒ job 说 done、
        验收说 failed。**这条是 `JobState` 与 `RunStatus` 必须分开的全部理由。**"""
        case, runner, scheduler, job = self._submit_one(mode=FakeFailureMode.EXIT_ZERO_BUT_CRASHED)
        for _ in range(3):
            latest = scheduler.poll([job])[job.job_id]
        self.assertIs(latest.state, JobState.DONE)
        self.assertEqual(latest.exit_code, 0)
        verdict = layout.verify_run_outputs(case.paths, case.run)
        self.assertFalse(verdict.ok)

    def test_job_state_failed_when_process_really_fails(self) -> None:
        """反向：真的退非 0 时 job 就该是 FAILED（证明上一条不是"永远 DONE"）。"""
        case, runner, scheduler, job = self._submit_one(mode=FakeFailureMode.NONZERO_EXIT)
        for _ in range(3):
            latest = scheduler.poll([job])[job.job_id]
        self.assertIs(latest.state, JobState.FAILED)
        self.assertEqual(latest.exit_code, 3)

    def test_unknown_job_polls_as_unknown(self) -> None:
        """查不到的 job → `UNKNOWN` + 保留原 Job，**不凭空判 failed**（Protocol 原话）。"""
        scheduler = FakeScheduler()
        stray = Job(job_id="fake-9999", name="stray", submitted_at="2026-01-01T00:00:00Z")
        got = scheduler.poll([stray])["fake-9999"]
        self.assertIs(got.state, JobState.UNKNOWN)
        self.assertEqual(got.name, "stray")
        self.assertEqual(got.submitted_at, "2026-01-01T00:00:00Z")

    def test_submit_returns_a_copy(self) -> None:
        """返回的是快照，不是活对象 —— 调用方不 poll 就不该看见状态变化（真调度器就是这样）。"""
        case, runner, scheduler, job = self._submit_one()
        scheduler.poll([job])
        scheduler.poll([job])
        scheduler.poll([job])
        self.assertIs(job.state, JobState.PENDING)  # 手上的快照没变
        self.assertIs(scheduler.jobs[job.job_id].state, JobState.DONE)

    def test_cancel_in_flight_job(self) -> None:
        case, runner, scheduler, job = self._submit_one()
        self.assertTrue(scheduler.cancel(job))
        self.assertIs(scheduler.jobs[job.job_id].state, JobState.CANCELLED)
        # 取消掉的 job 再怎么 poll 也不会执行 —— 不许在取消之后还烧配额。
        for _ in range(5):
            scheduler.poll([job])
        self.assertEqual(runner.calls, [])

    def test_cancel_finished_job_returns_false(self) -> None:
        """已经结束的返回 False 而不是抛异常（Protocol）。"""
        case, runner, scheduler, job = self._submit_one()
        for _ in range(3):
            scheduler.poll([job])
        self.assertFalse(scheduler.cancel(job))

    def test_cancel_unknown_job_returns_false(self) -> None:
        self.assertFalse(FakeScheduler().cancel(Job(job_id="fake-9999")))

    def test_fail_submit_raises_scheduler_error(self) -> None:
        case = _cases(self._batch())[0]
        scheduler = FakeScheduler(fail_submit=(case.run.run_id,))
        with self.assertRaises(SchedulerError):
            scheduler.submit(case.plan)

    def test_fail_submit_negative_other_run_goes_through(self) -> None:
        """反向：没被点名的 run 照常提交（证明上一条不是"提交永远失败"）。"""
        cases = _cases(self._batch())
        scheduler = FakeScheduler(fail_submit=(cases[0].run.run_id,))
        job = scheduler.submit(cases[1].plan)
        self.assertEqual(job.job_id, "fake-0001")
        self.assertIs(job.state, JobState.PENDING)


class DeterministicTimeline(_TempBatchTest):
    """同样的输入必须给同样的输出 —— 不许真随机。

    无人值守时不可复现 = 查不了：一个只在某次跑里翻红的失败，第二天早上没人能重放它。
    """

    def _terminal_polls(self, seed: int) -> list[int]:
        """每个 job 在第几次 poll 走到终态。只用 job 状态判断，不看内部计数。"""
        cases = _cases(self._batch())
        scheduler = FakeScheduler(
            FakeRunner(port_count=PORT_COUNT),
            pending_polls=0,
            running_polls=0,
            jitter_polls=3,
            seed=seed,
        )
        jobs = [scheduler.submit(case.plan, name=case.run.run_id) for case in cases]
        when: dict[str, int] = {}
        for index in range(1, 9):
            for job_id, job in scheduler.poll(jobs).items():
                if job_id not in when and job.state in (JobState.DONE, JobState.FAILED):
                    when[job_id] = index
        return [when[job.job_id] for job in jobs]

    def test_same_seed_gives_the_same_timeline(self) -> None:
        first = self._terminal_polls(seed=0)
        second = self._terminal_polls(seed=0)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 12)

    def test_jitter_actually_spreads_the_batch(self) -> None:
        """计数断言：抖动**真的**产生了不同的完成时刻。

        没有这条，一个"jitter 参数被忽略了"的实现照样能让上一条绿 ——
        两次全都不抖，当然完全一致。空过的测试比没测更坏。
        """
        timeline = self._terminal_polls(seed=0)
        self.assertGreater(len(set(timeline)), 1)

    def test_timestamps_do_not_come_from_the_wall_clock(self) -> None:
        """时间戳是假时钟算出来的，两次跑逐字相同。真墙钟会让 `batch.json` 每次都不一样。"""
        case = _cases(self._batch())[0]
        stamps = []
        for _ in range(2):
            scheduler = FakeScheduler(FakeRunner(port_count=PORT_COUNT))
            job = scheduler.submit(case.plan)
            for _ in range(3):
                scheduler.poll([job])
            final = scheduler.jobs[job.job_id]
            stamps.append((final.submitted_at, final.started_at, final.ended_at))
        self.assertEqual(stamps[0], stamps[1])
        self.assertEqual(
            stamps[0],
            ("2026-01-01T00:00:15Z", "2026-01-01T00:00:45Z", "2026-01-01T00:01:00Z"),
        )


# --------------------------------------------------------------------------
# ★ 12-run 假批次全链路
# --------------------------------------------------------------------------

BATCH_MODES: dict[str, FakeFailureMode] = {
    "dA/base/typical_25_0": FakeFailureMode.EXIT_ZERO_BUT_CRASHED,
    "dA/base/cworst_-40_0": FakeFailureMode.ZERO_BYTE_OUTPUT,
    "dA/base/cworst_125_0": FakeFailureMode.SWALLOWED_WRITE_FAILURE,
    "dB/base/typical_-40_0": FakeFailureMode.WRONG_PORT_COUNT,
    "dB/base/cworst_25_0": FakeFailureMode.NONZERO_EXIT,
}
"""12 个 run 里挑 5 个让它们各挂一种法子，其余 7 个正常成功。**键是 run_id**。

数字都是手写的：12 = 2 design × 2 corner × 3 temperature；5 种失败一样一个；
7 = 12 − 5。下面每一条计数断言都对得上这三个数。
"""


class TwelveRunFakeBatch(_TempBatchTest):
    """判据里那句"12-run 假批次全链路"。

    ★ 本类里最重要的一条是 `test_exit_code_alone_would_misjudge_four_runs`：
    它把"为什么不能用退出码判成败"变成了一个**数字**。
    """

    def _drive(self) -> tuple[list[_Case], FakeRunner, FakeScheduler, dict[str, Job]]:
        cases = _cases(self._batch())
        runner = FakeRunner(FakeFailureMode.SUCCESS, modes=BATCH_MODES, port_count=PORT_COUNT)
        scheduler = FakeScheduler(runner)
        jobs = [scheduler.submit(case.plan, name=case.run.run_id) for case in cases]
        latest: dict[str, Job] = {job.job_id: job for job in jobs}
        for _ in range(10):
            latest = scheduler.poll(list(latest.values()))
            if all(
                job.state in (JobState.DONE, JobState.FAILED, JobState.CANCELLED)
                for job in latest.values()
            ):
                break
        return cases, runner, scheduler, latest

    def test_matrix_expands_to_twelve_runs(self) -> None:
        cases = _cases(self._batch())
        self.assertEqual(len(cases), 12)  # 2 design × 2 corner × 3 temperature
        self.assertEqual(len(BATCH_MODES), 5)
        # 手写的模式表必须对得上真实展开出来的 run_id —— 打错一个字，整张表就静默失效。
        run_ids = {case.run.run_id for case in cases}
        self.assertTrue(set(BATCH_MODES) <= run_ids, sorted(set(BATCH_MODES) - run_ids))

    def test_every_run_executes_exactly_once_into_its_own_directory(self) -> None:
        """每个 run 跑一次，且落在**互不相同**的目录里。

        后半句是 D2 的机器判据：`<corner>_<temp>/` 那层是 eWave 建的、名字只由 corner+temp 决定，
        两个 run 撞进同一个 `--workDir` 就会静默覆盖 —— 那正是本工具要消灭的坑（痛点 1）。
        """
        cases, runner, scheduler, _ = self._drive()
        self.assertEqual(scheduler.submit_calls, 12)
        self.assertEqual(len(runner.calls), 12)
        keys = {runner.command_key(argv) for argv in runner.calls}
        self.assertEqual(len(keys), 12)
        self.assertEqual(keys, {case.paths.ewave_dir for case in cases})

    def test_verdicts_match_the_hand_written_expectation(self) -> None:
        """逐个 run 拿真实验收器判一遍，失败的那 5 个必须**正好**是手写表里的那 5 个。"""
        cases, runner, scheduler, _ = self._drive()
        failed = sorted(
            case.run.run_id
            for case in cases
            if not layout.verify_run_outputs(
                case.paths, case.run, expected_port_count=PORT_COUNT
            ).ok
        )
        self.assertEqual(failed, sorted(BATCH_MODES))
        self.assertEqual(len(failed), 5)
        self.assertEqual(12 - len(failed), 7)

    def test_exit_code_alone_would_misjudge_four_runs(self) -> None:
        """★ **本阶段的验收契约，写成一个数字。**

        12 个 run 里有 11 个进程退 0（`JobState.DONE`），而真正合格的只有 7 个。
        差值 4 = 只看退出码就会被判成功、实际空手而归的 run
        （exit=0 但崩了 / 0 字节产物 / 写失败被吞 / 端口数不对）。

        这 4 个正是 BRIEF §10 那张表的全部内容，也是本工具存在的理由。
        """
        cases, runner, scheduler, latest = self._drive()
        job_done = [job for job in latest.values() if job.state is JobState.DONE]
        job_failed = [job for job in latest.values() if job.state is JobState.FAILED]
        self.assertEqual(len(job_done), 11)  # 手写：只有 NONZERO_EXIT 那个真的退非 0
        self.assertEqual(len(job_failed), 1)

        verified_ok = [
            case
            for case in cases
            if layout.verify_run_outputs(
                case.paths, case.run, expected_port_count=PORT_COUNT
            ).ok
        ]
        self.assertEqual(len(verified_ok), 7)  # 手写：12 − 5 种失败
        self.assertEqual(len(job_done) - len(verified_ok), 4)  # ★ 会被误判的条数

    def test_all_twelve_output_directories_exist(self) -> None:
        """连失败的 run 都留下了目录和日志 —— "目录在/日志在" 一样不能当成功的判据。"""
        cases, runner, scheduler, _ = self._drive()
        existing = [case for case in cases if os.path.isdir(case.paths.ewave_dir)]
        self.assertEqual(len(existing), 12)
        logs = [case for case in cases if os.path.isfile(f"{case.paths.ewave_dir}/ewave.log")]
        self.assertEqual(len(logs), 12)


# --------------------------------------------------------------------------
# resume：只补没成的
# --------------------------------------------------------------------------


class ResumeOnlyRedoesWhatIsMissing(_TempBatchTest):
    """D7 断点续跑：跑到一半进程没了，新进程起来只补没成的那些。

    driver（`sched/driver.py`）还没写，所以这里手工走一遍它将来要走的那条路：
    ① 提交 12 个；② poll 两次就"进程被杀"；③ 新起一个 scheduler + 新的 runner；
    ④ **判据来自磁盘**（哪些 run 的产物验不过），不是来自上一次的内存状态；
    ⑤ 只对这些 run 重来一次。

    ⚠️ 第 ④ 步是关键：resume 的判据必须是产物验收，不能是"上次内存里记的状态"——
    上次的状态可能停在 `running`，而那个 job 其实早就崩了（还是 exit=0 那种崩法）。
    """

    def test_resume_reruns_exactly_the_unfinished_runs(self) -> None:
        cases = _cases(self._batch())
        runner1 = FakeRunner(FakeFailureMode.SUCCESS, modes=BATCH_MODES, port_count=PORT_COUNT)
        scheduler1 = FakeScheduler(
            runner1, pending_polls=0, running_polls=0, jitter_polls=3, seed=0
        )
        jobs = {
            case.run.run_id: scheduler1.submit(case.plan, name=case.run.run_id) for case in cases
        }

        # ---- 跑两拍就"断电" ------------------------------------------------
        latest = dict(jobs)
        for _ in range(2):
            polled = scheduler1.poll(list(latest.values()))
            latest = {
                run_id: polled[job.job_id] for run_id, job in latest.items() if job.job_id in polled
            }

        by_id = {job.job_id: run_id for run_id, job in jobs.items()}
        finished_first = {
            by_id[job.job_id]
            for job in latest.values()
            if job.state in (JobState.DONE, JobState.FAILED)
        }
        # 这个测试必须落在"有的完了、有的没完"这个区间里，否则它什么都没测。
        self.assertGreater(len(finished_first), 0)
        self.assertLess(len(finished_first), 12)

        # ---- 从磁盘判断谁还欠着（不看上一次的内存状态）----------------------
        by_run_id = {case.run.run_id: case for case in cases}
        unfinished = sorted(
            run_id
            for run_id, case in by_run_id.items()
            if not layout.verify_run_outputs(
                case.paths, case.run, expected_port_count=PORT_COUNT
            ).ok
        )
        self.assertIn("dA/base/typical_25_0", unfinished)  # 崩了那个当然还欠着

        # ---- 新进程：只补没成的 --------------------------------------------
        # ⚠️ 重跑之前必须先清掉那个 run 的旧产物。理由见
        # `test_resume_without_cleaning_mixes_old_and_new_products`：
        # 上一次留下的 `.s3p` 不会被这一次的 `.s4p` 覆盖（文件名不同），
        # 两代产物混在一个目录里，验收器会（正确地）判"端口数不一致"。
        for run_id in unfinished:
            shutil.rmtree(by_run_id[run_id].paths.ewave_dir, ignore_errors=True)

        runner2 = FakeRunner(FakeFailureMode.SUCCESS, port_count=PORT_COUNT)
        scheduler2 = FakeScheduler(runner2)
        resumed = [
            scheduler2.submit(by_run_id[run_id].plan, name=run_id) for run_id in unfinished
        ]
        for _ in range(4):
            resumed = list(scheduler2.poll(resumed).values())

        # 计数断言：第二次只跑了"欠着的"那些，一个多余的都没有。
        self.assertEqual(scheduler2.submit_calls, len(unfinished))
        self.assertEqual(len(runner2.calls), len(unfinished))
        rerun_keys = {runner2.command_key(argv) for argv in runner2.calls}
        self.assertEqual(rerun_keys, {by_run_id[run_id].paths.ewave_dir for run_id in unfinished})

        # 第一次就验过的那些**一个都没被重跑**（重跑会白烧配额，还会覆盖已归档的产物）。
        already_ok = {
            case.paths.ewave_dir
            for run_id, case in by_run_id.items()
            if run_id not in unfinished
        }
        self.assertEqual(rerun_keys & already_ok, set())

        # ---- 补完之后：12 个全绿 --------------------------------------------
        ok_now = [
            case
            for case in cases
            if layout.verify_run_outputs(
                case.paths, case.run, expected_port_count=PORT_COUNT
            ).ok
        ]
        self.assertEqual(len(ok_now), 12)

    def test_resume_without_cleaning_mixes_old_and_new_products(self) -> None:
        """⚠️ **一条实测出来的 resume 约束**（写这份测试时被验收器当场抓到的）：

        端口数变了的时候，重跑**不会**覆盖上一次的产物 —— `.s3p` 和 `.s4p` 是两个文件名。
        于是同一个 `<corner>_<temp>/` 里躺着两代产物，而 `.sNp` 里没有任何东西能说明
        哪一份是新的（端口映射只存在于命令行，BRIEF §5）。

        验收器的判决是"同一个 run 里的 .sNp 端口数不一致"—— 对的判决。
        ⇒ **driver 的 resume 必须先清掉旧产物再重跑**，不能只是"再提交一次"。
        """
        case = _cases(self._batch())[0]
        # 第一次：少一个 pin（D1b 的静默平移）⇒ 出 .s3p
        FakeRunner(FakeFailureMode.WRONG_PORT_COUNT, port_count=PORT_COUNT).run(
            case.plan.argv, cwd=case.plan.cwd
        )
        # 第二次：pin 补回来了 ⇒ 出 .s4p，但 .s3p 还在原地
        FakeRunner(FakeFailureMode.SUCCESS, port_count=PORT_COUNT).run(
            case.plan.argv, cwd=case.plan.cwd
        )
        verdict = layout.verify_run_outputs(case.paths, case.run)
        self.assertEqual(len(verdict.sparam_files), 4)  # 两代各两份
        self.assertIsNone(verdict.port_count)  # 混着，说不出是几端口
        self.assertFalse(verdict.ok)

        # 反向：先清干净再重跑 ⇒ 判 done。证明上面那条红不是"重跑一律失败"。
        shutil.rmtree(case.paths.ewave_dir)
        FakeRunner(FakeFailureMode.SUCCESS, port_count=PORT_COUNT).run(
            case.plan.argv, cwd=case.plan.cwd
        )
        clean = layout.verify_run_outputs(case.paths, case.run)
        self.assertEqual(len(clean.sparam_files), 2)
        self.assertEqual(clean.port_count, PORT_COUNT)
        self.assertTrue(clean.ok, clean.reasons)

    def test_adopt_puts_a_restored_job_back_on_the_timeline(self) -> None:
        """resume 的另一半：job 还在队列里活着，新进程 `adopt()` 之后接着 poll。

        （真实现里这一步是 `djob` 查得到它；本机没有队列，`adopt` 就是它的替身。）
        """
        case = _cases(self._batch())[0]
        runner1 = FakeRunner(port_count=PORT_COUNT)
        scheduler1 = FakeScheduler(runner1)
        job = scheduler1.submit(case.plan, name=case.run.run_id)
        scheduler1.poll([job])  # 排队中，进程就没了

        runner2 = FakeRunner(port_count=PORT_COUNT)
        scheduler2 = FakeScheduler(runner2)
        restored = scheduler1.jobs[job.job_id]
        scheduler2.adopt(restored, case.plan)
        for _ in range(3):
            latest = scheduler2.poll([restored])[job.job_id]
        self.assertIs(latest.state, JobState.DONE)
        self.assertEqual(len(runner2.calls), 1)
        self.assertTrue(layout.verify_run_outputs(case.paths, case.run).ok)

    def test_adopt_without_plan_produces_no_artifacts(self) -> None:
        """没给 plan 就 adopt ⇒ 走到终态也没有产物。

        这是**诚实的表示**："我们不知道它当初要跑什么"，于是不发明产物。
        验收随后会判它 failed —— 正确的结局，因为磁盘上确实什么都没有。
        """
        case = _cases(self._batch())[0]
        scheduler = FakeScheduler(FakeRunner(port_count=PORT_COUNT))
        stray = Job(job_id="fake-0001", state=JobState.RUNNING, name=case.run.run_id)
        scheduler.adopt(stray)
        for _ in range(3):
            latest = scheduler.poll([stray])["fake-0001"]
        self.assertIs(latest.state, JobState.DONE)
        self.assertFalse(layout.verify_run_outputs(case.paths, case.run).ok)


# --------------------------------------------------------------------------
# 归档看到的是真文件
# --------------------------------------------------------------------------


class ArchiveSeesRealFiles(_TempBatchTest):
    """归档（D5）跑在假 runner 写出来的真文件上 —— 又一处"验收逻辑必须被真实文件验"。"""

    def _run_and_archive(self, mode: FakeFailureMode) -> tuple[_Case, layout.ArchiveReport]:
        case = _cases(self._batch())[0]
        FakeRunner(mode, port_count=PORT_COUNT).run(case.plan.argv, cwd=case.plan.cwd)
        return case, layout.archive_run(case.paths, case.run)

    def test_success_archives_sparam_and_removes_intermediates(self) -> None:
        case, report = self._run_and_archive(FakeFailureMode.SUCCESS)
        self.assertEqual(report.errors, ())
        # 留下的正好是两份 .sNp（`archive_keep` 默认 `*.s[0-9]*p`）。
        self.assertEqual(sorted(report.kept), sorted(n for n in report.kept if ".s4p" in n))
        self.assertEqual(len(report.kept), 2)
        # 其余 9 个（4 mesh + resist + 2 log + 2 份 .yNp）被删掉。
        self.assertEqual(len(report.removed), EXPECTED_FILES_PER_SUCCESSFUL_RUN - 2)
        self.assertTrue(os.path.isfile(f"{case.paths.sparam_prefix}.s{PORT_COUNT}p"))

    def test_zero_byte_output_archive_deletes_nothing(self) -> None:
        """反向：验收没过 ⇒ **一个文件都不删**（先验后删）。

        mesh 和日志正是诊断材料，删完了这个 run 就没法查了。
        """
        case, report = self._run_and_archive(FakeFailureMode.ZERO_BYTE_OUTPUT)
        self.assertEqual(report.removed, ())
        self.assertEqual(report.bytes_freed, 0)
        self.assertNotEqual(report.errors, ())
        self.assertEqual(len(report.kept), EXPECTED_FILES_PER_SUCCESSFUL_RUN)


# --------------------------------------------------------------------------
# 阶段 1（strmout）
# --------------------------------------------------------------------------


class StreamoutStage(_TempBatchTest):
    """假 runner 也要认得阶段 1：GDS 落在**模板里写的**位置，不是我们现编的位置。"""

    def _prepare(self) -> tuple[_Case, CommandPlan]:
        case = _cases(self._batch())[0]
        ctx = PlanContext(
            design=case.design,
            facts=_facts(),
            options=BatchOptions(),
            batch_dir=case.paths.batch_dir,
        )
        fields = strmout.gdsout_fields_for_design(
            case.design, ctx, gds_path=case.paths.design_gds
        )
        text = strmout.render_gdsout_setup(strmout.DEFAULT_GDSOUT_TEMPLATE, fields)
        os.makedirs(os.path.dirname(case.paths.design_gdsout), exist_ok=True)
        with open(case.paths.design_gdsout, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        plan = strmout.build_strmout_plan(case.design, ctx, setup_path=case.paths.design_gdsout)
        return case, plan

    def test_streamout_writes_the_gds_where_the_template_says(self) -> None:
        case, plan = self._prepare()
        runner = FakeRunner()
        result = runner.run(plan.argv, cwd=plan.cwd)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(os.path.isfile(case.paths.design_gds))
        self.assertGreater(os.path.getsize(case.paths.design_gds), 0)
        self.assertEqual(len(runner.written), 2)  # GDS + gds_out.log

    def test_streamout_zero_byte_output_is_empty_gds(self) -> None:
        """反向：0 字节模式下 GDS 建出来了但是空的 —— 阶段 1 也会踩同一个坑。"""
        case, plan = self._prepare()
        FakeRunner(FakeFailureMode.ZERO_BYTE_OUTPUT).run(plan.argv, cwd=plan.cwd)
        self.assertTrue(os.path.isfile(case.paths.design_gds))
        self.assertEqual(os.path.getsize(case.paths.design_gds), 0)

    def test_streamout_wrong_port_count_degrades_to_success(self) -> None:
        """`WRONG_PORT_COUNT` 在阶段 1 退化成成功 —— GDS 里没有"端口数"这回事。

        写成测试是因为这是**有意的**退化：不写下来，下一个人会以为是漏了。
        """
        case, plan = self._prepare()
        FakeRunner(FakeFailureMode.WRONG_PORT_COUNT).run(plan.argv, cwd=plan.cwd)
        self.assertGreater(os.path.getsize(case.paths.design_gds), 0)

    def test_streamout_without_template_file_writes_nothing(self) -> None:
        """模板还没写出来（dry-run）⇒ 什么都不写，也不炸。"""
        case, plan = self._prepare()
        os.remove(case.paths.design_gdsout)
        runner = FakeRunner()
        result = runner.run(plan.argv, cwd=plan.cwd)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(runner.written, [])


# --------------------------------------------------------------------------
# meta：模式一个都不许漏测
# --------------------------------------------------------------------------


def _all_test_method_names() -> set[str]:
    """本模块里全部测试方法名。"""
    names: set[str] = set()
    for obj in list(globals().values()):
        if isinstance(obj, type) and issubclass(obj, unittest.TestCase):
            names |= {name for name in dir(obj) if name.startswith("test")}
    return names


class ModeCoverageMeta(unittest.TestCase):
    """遍历枚举，断言每个成员都被覆盖到 —— **漏一个模式要当场红，而不是无声少测一种坑**。"""

    def test_mode_count(self) -> None:
        """计数断言：模式恰好 6 个（4 种"信号不可靠" + 成功 + 真报错两条对照）。

        加了新模式而忘了给它写测试 ⇒ 这条先红。
        """
        self.assertEqual(len(FakeFailureMode), 6)

    def test_every_mode_has_an_outcome(self) -> None:
        """每个模式都要在 `_OUTCOMES` 表里有定义，否则 `run()` 会在半路 KeyError。"""
        self.assertEqual(set(fake._OUTCOMES), set(FakeFailureMode))

    def test_every_mode_is_covered_by_a_test(self) -> None:
        """每个模式的 `.value` 都得出现在某个测试方法名里。"""
        names = _all_test_method_names()
        for mode in FakeFailureMode:
            with self.subTest(mode=mode):
                hits = [name for name in names if mode.value in name]
                self.assertTrue(hits, f"没有任何测试覆盖 {mode.name}（方法名里要带 {mode.value!r}）")

    def test_modes_do_not_all_behave_the_same(self) -> None:
        """反向：六条后果不许全都一样。

        全一样的话，上面那三条 meta 断言可以全绿，而这个模块其实只模拟了一种情况。
        """
        self.assertEqual(len(set(fake._OUTCOMES.values())), 6)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
