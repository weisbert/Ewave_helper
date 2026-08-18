"""`ewave_batch.core.layout` 的测试 —— 目录布局 / 归档 / 验收 / state 读写。

**防自证纪律**（`docs/OVERNIGHT.md`「四条配方」）在本文件里的落法：

1. 期望路径、期望 `cmd.sh` 文本、期望扁平区文件名，**全是手写字面量**，
   每处注释写明它抄自 `PROJECT_BRIEF.md` §5「归档布局」那棵树的哪一行。
   一个字节都不许用被测函数自己拼一遍当期望值。
2. 每条关键测试配一条 `_negative`：**同一个构造路径**，故意改坏一个值，
   断言比较逻辑报告了这个差异。
3. 凡是有「排除 / 过滤」的地方（`keep` 的 fnmatch、spine 守卫的目录名匹配、
   Touchstone 后缀识别）都额外测「没把不该忽略的一起忽略掉」。
4. 计数断言：归档后**留下 4 个 / 删掉 9 个**、`len(kept)+len(removed)` 等于
   fixture 里数出来的 13 个。空集合的 diff 永远是绿的，这条专防"空得非常好看"。

fixture 里的 library / cell / view / pin 名**全是编出来的占位符**（`demo_lib` /
`demo_cell` / `pin00`），红区坐标一个都不进这个文件（CLAUDE.md 硬约束 1b）。
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import unittest
from unittest import mock

from ewave_batch import model
from ewave_batch.core import layout
from ewave_batch.model import (
    Axis,
    AxisKind,
    AxisValue,
    BatchOptions,
    BatchState,
    CommandPlan,
    Design,
    Job,
    JobState,
    LogFacts,
    PortMode,
    PortSpec,
    Provenance,
    Run,
    RunPaths,
    RunStatus,
    Stage,
    StateError,
    StreamoutTask,
)

# --------------------------------------------------------------------------
# fixture —— 全是编的占位符，零红区坐标
# --------------------------------------------------------------------------

DESIGN = Design(library="demo_lib", cell="demo_cell", view="layout")
DESIGN_DIR = "demo_lib_demo_cell_layout"
"""BRIEF §5「官方流程的既有布局」：**design 目录名 = `<library>_<topCell>_<view>`**。"""

EWAVE_DIR = "typical_-40_0"
"""BRIEF §5：`<temp>` 是温度把小数点换成下划线（`-40.0` → `-40_0`），
这层目录名是 **eWave 自己建的**，我们只是预测它。"""

CELL_STEM = "demo_cell_typical_-40_0"
"""官方产物的文件名形状：`<Cell>_<corner>_<temp>`（BRIEF §5 那棵树）。"""

PORTS_17 = tuple(f"pin{index:02d}" for index in range(17))

PARAM_FILES = (
    f"{CELL_STEM}.s17p",
    f"{CELL_STEM}.y17p",
    f"{CELL_STEM}_sample.s17p",
    f"{CELL_STEM}_sample.y17p",
)
"""BRIEF §5 官方布局里的 **4 个参数文件**（S/Y × 全量/_sample）。"""

INTERMEDIATES = (
    "ewave.log",
    "emsolver.log",
    "mesh.log",
    "emesh_mrg.log",
    "pmrg.gtxt",
    "pmrg.gtxt.mrg",
    "pmrg.gtxt_bak.mrg",
    "pmsh.gtxt.msh",
    "resist.rst",
)
"""BRIEF §5 官方布局里的 **9 个中间件**（4 log + pmrg 三件套 + pmsh + resist）。
D5 说这些要删掉 —— 用户"手动一个一个 copy"的来源就是它们和 `.sNp` 混在一起。"""


def make_run(
    *,
    axes_slug: str = "base",
    ports: tuple[str, ...] = PORTS_17,
    status: RunStatus = RunStatus.DONE,
) -> Run:
    """本文件里所有测试共用的 run 构造路径（正反两条测试必须走同一条，见配方 3）。"""
    return Run(
        run_id=f"{DESIGN_DIR}/{axes_slug}/{EWAVE_DIR}",
        design_key=DESIGN_DIR,
        axis_values={"corner": "typical", "temperature": "-40.0"},
        axes_slug=axes_slug,
        ewave_dir=EWAVE_DIR,
        status=status,
        ports=ports,
    )


def write_file(path: str, text: str) -> int:
    """写一个真文件（`tempfile` 里），返回字节数。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return len(text.encode("utf-8"))


def build_run_dir(batch_dir: str, *, sparam_text: str = "! touchstone\n", sparam_name: str | None = None):
    """造一个"跑完了"的 run 目录。正向和 `_negative` 测试**共用这一个构造路径**。"""
    run = make_run()
    paths = layout.compute_run_paths(batch_dir, DESIGN, run)
    layout.ensure_run_dirs(paths)
    os.makedirs(paths.ewave_dir, exist_ok=True)
    name = sparam_name or PARAM_FILES[0]
    write_file(f"{paths.ewave_dir}/{name}", sparam_text)
    return paths, run


# --------------------------------------------------------------------------
# 比较用的小工具 —— 测试自己的，跟被测代码无关
# --------------------------------------------------------------------------


def line_diff(actual: str, expected: str) -> tuple[list[str], int]:
    """逐行比两段文本，返回 (差异描述, 比过的行数)。

    返回行数是给**计数断言**用的：空文本的 diff 永远是绿的。
    """
    actual_lines = actual.splitlines()
    expected_lines = expected.splitlines()
    problems: list[str] = []
    total = max(len(actual_lines), len(expected_lines))
    for index in range(total):
        got = actual_lines[index] if index < len(actual_lines) else "<缺这一行>"
        want = expected_lines[index] if index < len(expected_lines) else "<多出这一行>"
        if got != want:
            problems.append(f"第 {index + 1} 行: 实际 {got!r} / 期望 {want!r}")
    return problems, total


def leaf_diff(actual: object, expected: object, path: str = "") -> tuple[list[str], list[str]]:
    """递归比两棵 `dataclasses.asdict` 出来的树，返回 (差异, 比过的叶子路径)。

    **故意不用 `layout.state_to_dict`** —— 用被测模块自己的序列化去验它自己的往返，
    那就是"实现方决定期望值"。这里走 stdlib 的 `dataclasses.asdict`，是独立的一条路。
    """
    problems: list[str] = []
    leaves: list[str] = []
    if isinstance(actual, dict) and isinstance(expected, dict):
        for key in sorted(set(actual) | set(expected)):
            child = f"{path}.{key}" if path else str(key)
            if key not in actual:
                problems.append(f"{child}: 实际这边没有这个键")
                continue
            if key not in expected:
                problems.append(f"{child}: 实际这边多了一个键")
                continue
            sub_problems, sub_leaves = leaf_diff(actual[key], expected[key], child)
            problems.extend(sub_problems)
            leaves.extend(sub_leaves)
        return problems, leaves
    if isinstance(actual, (list, tuple)) and isinstance(expected, (list, tuple)):
        if len(actual) != len(expected):
            problems.append(f"{path}: 长度 {len(actual)} != {len(expected)}")
        for index in range(min(len(actual), len(expected))):
            sub_problems, sub_leaves = leaf_diff(actual[index], expected[index], f"{path}[{index}]")
            problems.extend(sub_problems)
            leaves.extend(sub_leaves)
        return problems, leaves
    leaves.append(path)
    if actual != expected:
        problems.append(f"{path}: 实际 {actual!r} / 期望 {expected!r}")
    return problems, leaves


# --------------------------------------------------------------------------
# compute_run_paths —— 期望值全是手写字面量
# --------------------------------------------------------------------------


class ComputeRunPaths(unittest.TestCase):
    """期望路径逐条抄自 PROJECT_BRIEF.md §5「归档布局」那棵树。"""

    def test_paths_match_the_brief_tree(self) -> None:
        paths = layout.compute_run_paths("/batches/demo", DESIGN, make_run())

        # ↓ 全部手写。来源：BRIEF §5「归档布局」代码块的这几行 ——
        #   `<batch_root>/<batch_name>/`
        #   `  batch.json` / `  runs.csv`
        #   `  gds/<design>.gds`
        #   `  gdsout/<design>.gdsout_setup`
        #   `  sparam/<design>__<axes-slug>__<corner>_<temp>.s17p`
        #   `  runs/<design>/<axes-slug>/`  ← ★ --workDir 指到这里
        #   `    cmd.sh`
        #   `    <corner>_<temp>/`          ← ★ eWave 自己建的那层
        self.assertEqual(paths.batch_dir, "/batches/demo")
        self.assertEqual(paths.batch_json, "/batches/demo/batch.json")
        self.assertEqual(paths.runs_csv, "/batches/demo/runs.csv")
        self.assertEqual(paths.gds_dir, "/batches/demo/gds")
        self.assertEqual(paths.design_gds, "/batches/demo/gds/demo_lib_demo_cell_layout.gds")
        self.assertEqual(paths.gdsout_dir, "/batches/demo/gdsout")
        self.assertEqual(
            paths.design_gdsout,
            "/batches/demo/gdsout/demo_lib_demo_cell_layout.gdsout_setup",
        )
        self.assertEqual(paths.sparam_dir, "/batches/demo/sparam")
        self.assertEqual(
            paths.sparam_prefix,
            "/batches/demo/sparam/demo_lib_demo_cell_layout__base__typical_-40_0",
        )
        self.assertEqual(paths.run_dir, "/batches/demo/runs/demo_lib_demo_cell_layout/base")
        # ★ 每个 run 一份，名字带 <corner>_<temp> —— **不是**固定的 `cmd.sh`。
        # BRIEF §5 的树画的是固定名，这里是刻意偏离：`<axes-slug>` 不含 corner/temp，
        # 所以同一个 axes-slug 下的 N 个 corner/temp 组合共用一个 run_dir，固定名会让
        # N 条命令行互相覆盖 —— 而静默覆盖正是本工具要消灭的东西。
        # 形状照官方：`run_ewave_<corner>_<temp>.sh` 就是 `<corner>_<temp>/` 的同级兄弟。
        self.assertEqual(
            paths.cmd_sh,
            "/batches/demo/runs/demo_lib_demo_cell_layout/base/cmd_typical_-40_0.sh",
        )
        self.assertEqual(
            paths.ewave_dir,
            "/batches/demo/runs/demo_lib_demo_cell_layout/base/typical_-40_0",
        )
        # ↓ 这两个 BRIEF 的树里没画，是本模块补的（见交接报告「设计偏离」）
        self.assertEqual(paths.logs_dir, "/batches/demo/logs")
        self.assertEqual(
            paths.run_log,
            "/batches/demo/runs/demo_lib_demo_cell_layout/base/run_typical_-40_0.log",
        )

    def test_paths_match_the_brief_tree_negative(self) -> None:
        """同一个构造路径，只把 axes-slug 从 `base` 换成多轴那个 → 路径必须跟着变。

        BRIEF §5：`<axes-slug>` = 除 corner/temp 之外的所有轴，例 `eqI-on__fw-off`。
        它不变就意味着"同 corner/temp 换别的 flag 会静默覆盖" —— 那正是用户的核心痛点。
        """
        base = layout.compute_run_paths("/batches/demo", DESIGN, make_run())
        other = layout.compute_run_paths(
            "/batches/demo", DESIGN, make_run(axes_slug="eqI-on__fw-off")
        )

        self.assertNotEqual(base.run_dir, other.run_dir)
        self.assertNotEqual(base.ewave_dir, other.ewave_dir)
        self.assertNotEqual(base.sparam_prefix, other.sparam_prefix)
        # 手写字面量（同一棵树，只换 <axes-slug>）
        self.assertEqual(
            other.run_dir, "/batches/demo/runs/demo_lib_demo_cell_layout/eqI-on__fw-off"
        )
        self.assertEqual(
            other.sparam_prefix,
            "/batches/demo/sparam/demo_lib_demo_cell_layout__eqI-on__fw-off__typical_-40_0",
        )
        # 而 design 级的东西（GDS / gdsout 模板）整个矩阵共用（D1a），**不该**跟着轴变
        self.assertEqual(base.design_gds, other.design_gds)
        self.assertEqual(base.design_gdsout, other.design_gdsout)

    def test_design_dir_falls_back_to_library_cell_view(self) -> None:
        """`run.design_key` 和 `design.key` 都空时，按 BRIEF §5 拼 `<library>_<topCell>_<view>`。"""
        run = Run(run_id="x", design_key="", axes_slug="base", ewave_dir=EWAVE_DIR)
        paths = layout.compute_run_paths("/batches/demo", DESIGN, run)
        self.assertEqual(paths.run_dir, "/batches/demo/runs/demo_lib_demo_cell_layout/base")

    def test_windows_backslashes_are_normalised(self) -> None:
        """路径一律 `/` —— 最终跑在 Linux 上，Windows 上比字符串也要一致。"""
        paths = layout.compute_run_paths("C:\\batches\\demo", DESIGN, make_run())
        self.assertEqual(paths.batch_json, "C:/batches/demo/batch.json")

    def test_traversal_in_a_slug_is_rejected(self) -> None:
        run = make_run(axes_slug="../../etc")
        with self.assertRaises(StateError):
            layout.compute_run_paths("/batches/demo", DESIGN, run)


class SpineGuard(unittest.TestCase):
    """CLAUDE.md 硬约束 4：`<workarea>/ewave_simulation/` 只读。

    守卫抄的是 `mvp/redzone/cfg.sh`：
    `case "$MVP" in */ewave_simulation|*/ewave_simulation/*) ... exit 2`。
    """

    def test_batch_root_inside_the_spine_is_rejected(self) -> None:
        for bad in (
            "/work/ewave_simulation",
            "/work/ewave_simulation/batches",
            "/work/ewave_simulation/demo_lib_demo_cell_layout/x",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(StateError) as ctx:
                    layout.compute_run_paths(bad, DESIGN, make_run())
                self.assertIn("ewave_simulation", str(ctx.exception))

    def test_batch_root_inside_the_spine_is_rejected_negative(self) -> None:
        """反向：名字**只是像**的目录不许被误伤。

        （和 `--sparam` 前缀误伤 `--sparamImpedance` 是同一类错误 —— 守卫按**整层目录名**
        精确匹配，不做前缀/子串匹配。）
        """
        for ok in (
            "/work/ewave_simulations/demo",
            "/work/ewave_simulation_backup/demo",
            "/work/my_ewave_simulation/demo",
            "/work/ewave_batches/demo",
        ):
            with self.subTest(ok=ok):
                paths = layout.compute_run_paths(ok, DESIGN, make_run())
                self.assertTrue(paths.batch_json.endswith("/batch.json"))

    def test_ensure_run_dirs_refuses_the_spine(self) -> None:
        """`RunPaths` 也可能是别人手工拼的 —— 守卫不能只在 compute_run_paths 里。"""
        with tempfile.TemporaryDirectory() as tmp:
            spine = f"{tmp}/ewave_simulation/demo".replace("\\", "/")
            paths = layout.compute_run_paths(f"{tmp}/ok".replace("\\", "/"), DESIGN, make_run())
            bad = dataclasses.replace(paths, batch_dir=spine, run_dir=f"{spine}/runs/x/base")
            with self.assertRaises(StateError):
                layout.ensure_run_dirs(bad)
            self.assertFalse(os.path.exists(f"{tmp}/ewave_simulation"), "一个目录都不许建出来")

    def test_write_batch_state_refuses_the_spine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = f"{tmp}/ewave_simulation/demo/batch.json".replace("\\", "/")
            with self.assertRaises(StateError):
                layout.write_batch_state(target, BatchState(batch_name="demo"))
            self.assertFalse(os.path.exists(f"{tmp}/ewave_simulation"))


class EnsureRunDirs(unittest.TestCase):
    def test_creates_the_tree_but_not_the_ewave_layer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = layout.compute_run_paths(f"{tmp}/demo".replace("\\", "/"), DESIGN, make_run())
            layout.ensure_run_dirs(paths)
            for directory in (paths.gds_dir, paths.gdsout_dir, paths.sparam_dir, paths.run_dir):
                self.assertTrue(os.path.isdir(directory), directory)
            # `<corner>_<temp>` 那层是 **eWave 自己建的**（BRIEF §5）。
            # 我们提前建出来，只会掩盖"eWave 根本没跑起来"。
            self.assertFalse(os.path.isdir(paths.ewave_dir))

    def test_dry_run_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = f"{tmp}/demo".replace("\\", "/")
            paths = layout.compute_run_paths(root, DESIGN, make_run())
            layout.ensure_run_dirs(paths, dry_run=True)
            self.assertFalse(os.path.exists(root))


# --------------------------------------------------------------------------
# cmd.sh
# --------------------------------------------------------------------------

# 手写期望值。来源：`ewave_batch/model.py` 里 `write_cmd_sh` 的 docstring ——
# 「行尾必须是 LF」「参数逐个 shlex.quote，一行一个 flag 加续行 `\`，人要能读」。
EXPECTED_CMD_SH = """#!/bin/sh
# 由 ewave_batch 生成：这个 run 的完整命令，可单独手工重跑。
# 改这个文件不会改批次状态（batch.json 才是权威）。
# run_id : demo_lib_demo_cell_layout/base/typical_-40_0
# design : demo_lib_demo_cell_layout
# stage  : solve
# workDir: /batches/demo/runs/demo_lib_demo_cell_layout/base
set -e
ewave \\
    --nogui \\
    -m \\
    --corner=typical \\
    -e \\
    0.4
"""

EXPECTED_CMD_SH_LINES = 14
"""上面那段手数出来的行数。计数断言：空文本的 diff 永远是绿的。"""


def make_plan(*, corner: str = "typical") -> CommandPlan:
    """cmd.sh 正反两条测试共用的构造路径。"""
    return CommandPlan(
        argv=("ewave", "--nogui", "-m", f"--corner={corner}", "-e", "0.4"),
        work_dir="/batches/demo/runs/demo_lib_demo_cell_layout/base",
        stage=Stage.SOLVE,
        run_id="demo_lib_demo_cell_layout/base/typical_-40_0",
        design_key="demo_lib_demo_cell_layout",
    )


class WriteCmdSh(unittest.TestCase):
    def test_cmd_sh_matches_the_hand_written_golden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = layout.compute_run_paths(f"{tmp}/demo".replace("\\", "/"), DESIGN, make_run())
            layout.ensure_run_dirs(paths)
            written = layout.write_cmd_sh(paths, make_plan())
            self.assertEqual(written, paths.cmd_sh)
            with open(written, "rb") as handle:
                raw = handle.read()

        self.assertNotIn(b"\r\n", raw, "cmd.sh 里有 CRLF —— 红区 bash 会死在这上面")
        text = raw.decode("utf-8")
        problems, compared = line_diff(text, EXPECTED_CMD_SH)
        self.assertEqual(problems, [], "生成的 cmd.sh 和手写基准对不上")
        self.assertEqual(compared, EXPECTED_CMD_SH_LINES, "比过的行数和手数的对不上")

    def test_cmd_sh_matches_the_hand_written_golden_negative(self) -> None:
        """同一个构造路径，只把 `--corner=typical` 改成别的 → 比较逻辑必须报出来。

        改坏的正是 corner —— 目录名说 typical、命令行说别的工艺角，跑得出来、数字也像，
        这是 BRIEF §7「corner 轴要同时改两处」警告的那类静默错误。
        """
        with tempfile.TemporaryDirectory() as tmp:
            paths = layout.compute_run_paths(f"{tmp}/demo".replace("\\", "/"), DESIGN, make_run())
            layout.ensure_run_dirs(paths)
            written = layout.write_cmd_sh(paths, make_plan(corner="fast"))
            with open(written, encoding="utf-8") as handle:
                text = handle.read()

        problems, compared = line_diff(text, EXPECTED_CMD_SH)
        self.assertEqual(compared, EXPECTED_CMD_SH_LINES)
        self.assertEqual(len(problems), 1, f"应当只有 corner 那一行不同，实际: {problems}")
        self.assertIn("--corner=fast", problems[0])
        self.assertIn("--corner=typical", problems[0])

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = layout.compute_run_paths(f"{tmp}/demo".replace("\\", "/"), DESIGN, make_run())
            layout.ensure_run_dirs(paths)
            written = layout.write_cmd_sh(paths, make_plan(), dry_run=True)
            self.assertEqual(written, paths.cmd_sh)
            self.assertFalse(os.path.exists(written))

    def test_arguments_with_spaces_are_quoted(self) -> None:
        plan = CommandPlan(argv=("ewave", "--gds=/a b/c.gds"), run_id="r", stage=Stage.SOLVE)
        with tempfile.TemporaryDirectory() as tmp:
            paths = layout.compute_run_paths(f"{tmp}/demo".replace("\\", "/"), DESIGN, make_run())
            layout.ensure_run_dirs(paths)
            with open(layout.write_cmd_sh(paths, plan), encoding="utf-8") as handle:
                text = handle.read()
        self.assertIn("'--gds=/a b/c.gds'", text)


# --------------------------------------------------------------------------
# port_count_from_suffix —— 过滤器本身要有测试
# --------------------------------------------------------------------------


class PortCountFromSuffix(unittest.TestCase):
    def test_reads_the_port_count(self) -> None:
        # 期望值手写。来源：BRIEF §5 官方布局里的真实文件名形状
        # （`<Cell>_<corner>_<temp>.s17p` / `.y17p`），以及 model 里 `port_count_from_suffix`
        # 的 docstring（"从 .s17p / .y16p 这种后缀里取端口数"）。
        cases = {
            "demo_cell_typical_-40_0.s17p": 17,
            "demo_cell_typical_-40_0.y17p": 17,
            "demo_cell_typical_-40_0_sample.s17p": 17,
            "/batches/demo/sparam/x__base__typical_-40_0.s16p": 16,
            "a.s2p": 2,
            "A.S17P": 17,
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(layout.port_count_from_suffix(path), expected)
        self.assertEqual(len(cases), 6, "计数断言：上面手写了 6 条，别悄悄少比几条")

    def test_reads_the_port_count_negative(self) -> None:
        """反向：不该认出来的一个都不许认出来（"没把不该忽略的一起忽略掉"的镜像）。"""
        not_touchstone = (
            "pmsh.gtxt.msh",
            "pmrg.gtxt_bak.mrg",
            "resist.rst",
            "ewave.log",
            "demo_cell.gds",
            "demo_cell_typical_-40_0.s17p.bak",  # 备份不是产物
            "demo_cell.snp",  # N 不是数字
            "demo_cell.s0p",  # 0 端口 = 认不出，不是 0
        )
        for path in not_touchstone:
            with self.subTest(path=path):
                self.assertIsNone(layout.port_count_from_suffix(path))
        self.assertEqual(len(not_touchstone), 8)


# --------------------------------------------------------------------------
# verify_run_outputs —— done 的判据
# --------------------------------------------------------------------------


class VerifyRunOutputs(unittest.TestCase):
    """三条独立测试，每条配一个 `_negative`。全部用 `tempfile` 造真文件。"""

    # ---- ① 正常产物 → done ------------------------------------------------

    def test_good_output_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, run = build_run_dir(f"{tmp}/demo".replace("\\", "/"))
            report = layout.verify_run_outputs(paths, run)
        self.assertTrue(report.ok, report.reasons)
        self.assertEqual(report.reasons, ())
        self.assertEqual(report.port_count, 17)
        self.assertEqual(len(report.sparam_files), 1)
        self.assertGreater(report.total_bytes, 0)

    def test_good_output_is_accepted_negative(self) -> None:
        """同一个构造路径，只是产物根本没落地 → 必须判失败。"""
        with tempfile.TemporaryDirectory() as tmp:
            paths, run = build_run_dir(f"{tmp}/demo".replace("\\", "/"))
            os.remove(f"{paths.ewave_dir}/{PARAM_FILES[0]}")
            report = layout.verify_run_outputs(paths, run)
        self.assertFalse(report.ok)
        self.assertTrue(any(".sNp" in reason for reason in report.reasons), report.reasons)

    # ---- ② 0 字节 → 必须判失败（实测过的真坑，不是假想）-------------------

    def test_zero_byte_sparam_is_rejected(self) -> None:
        """BRIEF §10 实测：eWave 崩了也 `exit=0`、还会留 **0 字节文件**报 "done"。

        所以退出码不算数，"存在"也不算数 —— 必须**非空**。
        """
        with tempfile.TemporaryDirectory() as tmp:
            paths, run = build_run_dir(f"{tmp}/demo".replace("\\", "/"), sparam_text="")
            report = layout.verify_run_outputs(paths, run)
        self.assertFalse(report.ok, "0 字节的 .sNp 被当成成功了 —— 这正是 MVP 踩过的坑")
        self.assertTrue(any("0 字节" in reason for reason in report.reasons), report.reasons)
        self.assertEqual(report.total_bytes, 0)

    def test_zero_byte_sparam_is_rejected_negative(self) -> None:
        """同一个构造路径，产物里有 1 个字节 → 就不该因为"空"而失败。"""
        with tempfile.TemporaryDirectory() as tmp:
            paths, run = build_run_dir(f"{tmp}/demo".replace("\\", "/"), sparam_text="!")
            report = layout.verify_run_outputs(paths, run)
        self.assertTrue(report.ok, report.reasons)
        self.assertEqual(report.total_bytes, 1)

    # ---- ③ 端口数不符 → 必须判失败 ---------------------------------------

    def test_port_count_mismatch_is_rejected(self) -> None:
        """`--all` 的代价：pin 集合一变所有端口编号平移，**而且静默**（BRIEF §5）。"""
        with tempfile.TemporaryDirectory() as tmp:
            paths, run = build_run_dir(
                f"{tmp}/demo".replace("\\", "/"), sparam_name=f"{CELL_STEM}.s16p"
            )
            self.assertEqual(len(run.ports), 17, "前提：这个 run 有 17 个端口")
            report = layout.verify_run_outputs(paths, run)
        self.assertFalse(report.ok, ".s16p 配 17 端口的 run 被放行了 —— 静默错位就是这么来的")
        self.assertTrue(any("端口数不符" in reason for reason in report.reasons), report.reasons)
        self.assertEqual(report.port_count, 16)

    def test_port_count_mismatch_is_rejected_negative(self) -> None:
        """同一个构造路径，只把 `.s16p` 换回 `.s17p` → 必须通过。"""
        with tempfile.TemporaryDirectory() as tmp:
            paths, run = build_run_dir(
                f"{tmp}/demo".replace("\\", "/"), sparam_name=f"{CELL_STEM}.s17p"
            )
            report = layout.verify_run_outputs(paths, run)
        self.assertTrue(report.ok, report.reasons)
        self.assertEqual(report.port_count, 17)

    # ---- 其余边角 ---------------------------------------------------------

    def test_explicit_expected_port_count_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, run = build_run_dir(f"{tmp}/demo".replace("\\", "/"))
            report = layout.verify_run_outputs(paths, run, expected_port_count=16)
        self.assertFalse(report.ok)
        self.assertTrue(any("端口数不符" in reason for reason in report.reasons))

    def test_missing_ewave_dir_is_not_an_exception(self) -> None:
        """找不到产物**不抛异常**，返回 ok=False + 原因（driver 要把它转成 failed）。"""
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run()
            paths = layout.compute_run_paths(f"{tmp}/demo".replace("\\", "/"), DESIGN, run)
            report = layout.verify_run_outputs(paths, run)
        self.assertFalse(report.ok)
        self.assertEqual(len(report.reasons), 1)

    def test_mixed_port_counts_in_one_run_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, run = build_run_dir(f"{tmp}/demo".replace("\\", "/"))
            write_file(f"{paths.ewave_dir}/{CELL_STEM}_sample.s16p", "! touchstone\n")
            report = layout.verify_run_outputs(paths, run)
        self.assertFalse(report.ok)
        self.assertTrue(any("不一致" in reason for reason in report.reasons), report.reasons)


# --------------------------------------------------------------------------
# archive_run —— 计数断言
# --------------------------------------------------------------------------

ALL_KEEP = ("*.s[0-9]*p", "*.y[0-9]*p")
"""这条测试用的 keep：把 4 个参数文件全留下（P4c 建议至少留 .sNp + _sample.sNp）。
默认表只留 S 参数（`BatchOptions.archive_keep`），另有一条测试盯着它。"""

# 手写期望值。来源：BRIEF §5「归档布局」的
#   `sparam/<design>__<axes-slug>__<corner>_<temp>.s17p`
EXPECTED_FLAT_NAMES = (
    "demo_lib_demo_cell_layout__base__typical_-40_0.s17p",
    "demo_lib_demo_cell_layout__base__typical_-40_0.y17p",
    "demo_lib_demo_cell_layout__base__typical_-40_0_sample.s17p",
    "demo_lib_demo_cell_layout__base__typical_-40_0_sample.y17p",
)


def build_finished_run(batch_dir: str, *, broken: bool = False, status: RunStatus = RunStatus.DONE):
    """造一个官方形状的 run 目录：4 个参数文件 + 9 个中间件。

    正向和 `_negative` **共用这一个构造路径**，`broken=True` 只把主 `.s17p` 弄成 0 字节。
    """
    run = make_run(status=status)
    paths = layout.compute_run_paths(batch_dir, DESIGN, run)
    layout.ensure_run_dirs(paths)
    os.makedirs(paths.ewave_dir, exist_ok=True)
    sizes: dict[str, int] = {}
    for index, name in enumerate(PARAM_FILES):
        text = "" if (broken and index == 0) else "! touchstone\n" * (index + 1)
        sizes[name] = write_file(f"{paths.ewave_dir}/{name}", text)
    for index, name in enumerate(INTERMEDIATES):
        sizes[name] = write_file(f"{paths.ewave_dir}/{name}", "x" * (index + 1))
    return paths, run, sizes


class ArchiveRun(unittest.TestCase):
    def test_counts_kept_and_removed(self) -> None:
        """计数断言：留下 4、删掉 9、两者之和等于 fixture 里数出来的 13。"""
        self.assertEqual(len(PARAM_FILES), 4, "fixture 里手数的参数文件数")
        self.assertEqual(len(INTERMEDIATES), 9, "fixture 里手数的中间件数")

        with tempfile.TemporaryDirectory() as tmp:
            paths, run, sizes = build_finished_run(f"{tmp}/demo".replace("\\", "/"))
            report = layout.archive_run(paths, run, keep=ALL_KEEP)

            self.assertEqual(report.errors, (), report.errors)
            self.assertEqual(len(report.kept), 4, f"该留 4 个，实际 {report.kept}")
            self.assertEqual(len(report.removed), 9, f"该删 9 个，实际 {report.removed}")
            self.assertEqual(
                len(report.kept) + len(report.removed),
                13,
                "参与归档的条目数 != fixture 里的 13 个文件 —— 有文件被静默跳过了",
            )
            self.assertEqual(sorted(report.kept), sorted(PARAM_FILES))
            self.assertEqual(sorted(report.removed), sorted(INTERMEDIATES))
            self.assertEqual(report.missing, ())

            # 逐个断言：该留的还在
            for name in PARAM_FILES:
                self.assertTrue(os.path.isfile(f"{paths.ewave_dir}/{name}"), f"{name} 被误删了")
            # 逐个断言：该删的没了
            for name in INTERMEDIATES:
                self.assertFalse(os.path.exists(f"{paths.ewave_dir}/{name}"), f"{name} 没删掉")
            self.assertEqual(sorted(os.listdir(paths.ewave_dir)), sorted(PARAM_FILES))

            self.assertEqual(
                report.bytes_freed,
                sum(sizes[name] for name in INTERMEDIATES),
                "bytes_freed 和实际删掉的字节数对不上",
            )

            # 扁平区：文件名逐条对手写字面量
            self.assertEqual(sorted(os.listdir(paths.sparam_dir)), sorted(EXPECTED_FLAT_NAMES))
            for name in EXPECTED_FLAT_NAMES:
                self.assertGreater(os.path.getsize(f"{paths.sparam_dir}/{name}"), 0)

    def test_counts_kept_and_removed_negative(self) -> None:
        """同一个构造路径，只把主 `.s17p` 弄成 0 字节 → **先验后删**：一个都不许删。

        0 字节产物是实测过的真坑（BRIEF §10）。此时 mesh 和日志正是诊断材料。
        """
        with tempfile.TemporaryDirectory() as tmp:
            paths, run, _ = build_finished_run(f"{tmp}/demo".replace("\\", "/"), broken=True)
            report = layout.archive_run(paths, run, keep=ALL_KEEP)

            self.assertEqual(report.removed, (), "验收没过还敢删 —— 诊断材料就没了")
            self.assertEqual(report.bytes_freed, 0)
            self.assertEqual(len(report.kept), 13, "验收没过时 13 个文件全都该留着")
            self.assertTrue(report.errors)
            self.assertTrue(any("先验后删" in err for err in report.errors), report.errors)
            self.assertEqual(
                sorted(os.listdir(paths.ewave_dir)),
                sorted(PARAM_FILES + INTERMEDIATES),
                "一个文件都不该动",
            )
            self.assertFalse(os.listdir(paths.sparam_dir), "验收没过还往扁平区收")

    def test_default_keep_only_holds_s_parameters(self) -> None:
        """`BatchOptions.archive_keep` 默认只留 S 参数（D5：用户明确只要 S 参数）。

        顺带是过滤器的过/欠匹配测试：`*.s[0-9]*p` 必须
        **同时**盖住 `.s17p` 和 `_sample.s17p`、**且不许**吃掉 `.y17p` 或 `pmsh.gtxt.msh`。
        """
        with tempfile.TemporaryDirectory() as tmp:
            paths, run, _ = build_finished_run(f"{tmp}/demo".replace("\\", "/"))
            report = layout.archive_run(paths, run)  # keep=() → 用 BatchOptions 的默认

            self.assertEqual(
                sorted(report.kept),
                sorted([f"{CELL_STEM}.s17p", f"{CELL_STEM}_sample.s17p"]),
            )
            self.assertEqual(len(report.removed), 11)
            self.assertEqual(len(report.kept) + len(report.removed), 13)
            self.assertIn(f"{CELL_STEM}.y17p", report.removed, "Y 参数默认不留（D5）")
            self.assertIn("pmsh.gtxt.msh", report.removed, "mesh 中间件不许被 keep 模式误吃")

    def test_keep_pattern_that_matches_nothing_deletes_nothing(self) -> None:
        """keep 打错字 → 一个都不删。照删会把这个 run 的产物全删光，那是不可逆的。"""
        with tempfile.TemporaryDirectory() as tmp:
            paths, run, _ = build_finished_run(f"{tmp}/demo".replace("\\", "/"))
            report = layout.archive_run(paths, run, keep=("*.nosuch",))
            self.assertEqual(report.removed, ())
            self.assertEqual(report.missing, ("*.nosuch",))
            self.assertEqual(len(os.listdir(paths.ewave_dir)), 13)

    def test_failed_run_keeps_its_logs(self) -> None:
        """D5：失败时保留 `ewave.log` / `emsolver.log` 做诊断。

        这里产物是好的（存在 + 非空 + 端口数对），只是 run 被标成 failed ——
        于是中间件照删，日志留下。
        """
        with tempfile.TemporaryDirectory() as tmp:
            paths, run, _ = build_finished_run(
                f"{tmp}/demo".replace("\\", "/"), status=RunStatus.FAILED
            )
            report = layout.archive_run(paths, run, keep=ALL_KEEP)
            remaining = sorted(os.listdir(paths.ewave_dir))
        self.assertIn("ewave.log", remaining)
        self.assertIn("emsolver.log", remaining)
        self.assertNotIn("pmsh.gtxt.msh", remaining, "日志留下不等于中间件也留下")
        self.assertEqual(len(report.kept), 8, "4 个参数文件 + 4 份日志")
        self.assertEqual(len(report.removed), 5)

    def test_failed_run_keeps_its_logs_negative(self) -> None:
        """同一个构造路径，只把 `keep_logs_on_failure` 关掉 → 日志就该跟着走。"""
        with tempfile.TemporaryDirectory() as tmp:
            paths, run, _ = build_finished_run(
                f"{tmp}/demo".replace("\\", "/"), status=RunStatus.FAILED
            )
            report = layout.archive_run(paths, run, keep=ALL_KEEP, keep_logs_on_failure=False)
            remaining = sorted(os.listdir(paths.ewave_dir))
        self.assertNotIn("ewave.log", remaining)
        self.assertEqual(len(report.kept), 4)
        self.assertEqual(len(report.removed), 9)

    def test_dry_run_touches_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, run, _ = build_finished_run(f"{tmp}/demo".replace("\\", "/"))
            report = layout.archive_run(paths, run, keep=ALL_KEEP, dry_run=True)
            self.assertEqual(len(report.removed), 9, "dry-run 也要**报告**会删哪些")
            self.assertEqual(len(os.listdir(paths.ewave_dir)), 13, "dry-run 不许真删")
            self.assertFalse(os.listdir(paths.sparam_dir), "dry-run 不许真拷")

    def test_deletion_is_confined_to_the_run_dir(self) -> None:
        """删除只在 `paths.ewave_dir` 里发生 —— 别的地方一个文件都不许删。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = f"{tmp}/demo".replace("\\", "/")
            paths, run, _ = build_finished_run(root)
            outsider = f"{tmp}/outside.txt".replace("\\", "/")
            write_file(outsider, "别动我")
            sibling = f"{paths.run_dir}/cmd.sh"
            write_file(sibling, "#!/bin/sh\n")
            layout.archive_run(paths, run, keep=ALL_KEEP)
            self.assertTrue(os.path.isfile(outsider))
            self.assertTrue(os.path.isfile(sibling), "run_dir 里的 cmd.sh 不在归档范围内")


# --------------------------------------------------------------------------
# check_port_consistency
# --------------------------------------------------------------------------


class PortConsistency(unittest.TestCase):
    def _state(self, runs: list[Run]) -> BatchState:
        return BatchState(batch_name="demo", designs=[DESIGN], runs=runs)

    def test_consistent_ports_report_nothing(self) -> None:
        runs = [make_run(axes_slug=slug) for slug in ("base", "eqI-on", "fw-on")]
        self.assertEqual(layout.check_port_consistency(self._state(runs)), [])

    def test_consistent_ports_report_nothing_negative(self) -> None:
        """同一个构造路径，只把一个 run 的一个 pin 改名 → 必须报出来。

        BRIEF §5：设计师改一个 pin 名 ⇒ 所有端口编号平移 ⇒ 归档的 .sNp 和 nport
        **静默**错位，两份数字还挺像。
        """
        drifted = list(PORTS_17)
        drifted[3] = "pin03_renamed"
        runs = [
            make_run(axes_slug="base"),
            make_run(axes_slug="eqI-on", ports=tuple(drifted)),
        ]
        problems = layout.check_port_consistency(self._state(runs))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("eqI-on", problems[0])
        self.assertIn("pin03_renamed", problems[0])

    def test_different_designs_may_differ(self) -> None:
        """不同 design 端口数本来就不同（官方样本一个 17 端口一个 16 端口）→ 不许误报。"""
        other = Run(
            run_id="other_design/base/typical_-40_0",
            design_key="other_design",
            axes_slug="base",
            ewave_dir=EWAVE_DIR,
            ports=tuple(f"pin{i:02d}" for i in range(16)),
        )
        self.assertEqual(layout.check_port_consistency(self._state([make_run(), other])), [])

    def test_runs_without_ports_are_skipped(self) -> None:
        """还没跑的 run 没有端口列表 —— 那不是"不一致"。"""
        pending = make_run(axes_slug="eqI-on", ports=(), status=RunStatus.READY)
        self.assertEqual(layout.check_port_consistency(self._state([make_run(), pending])), [])


# --------------------------------------------------------------------------
# batch.json 往返 + 原子写
# --------------------------------------------------------------------------


def rich_state(batch_dir: str) -> BatchState:
    """一份把冻结面上每种字段形状都碰一遍的 state（枚举 / 元组 / 嵌套 dataclass / None / bool flag）。"""
    return BatchState(
        batch_name="demo",
        batch_dir=batch_dir,
        designs=[
            Design(
                library="demo_lib",
                cell="demo_cell",
                view="layout",
                key=DESIGN_DIR,
                resources="cpu=20;mem=100000",
                axis_overrides={"temperature": ("-40.0", "125.0")},
                extra_flags={"--tolerance": "0.01"},
                port_spec=PortSpec(
                    mode=PortMode.EXPLICIT,
                    mapping=(("P000", "pin00"), ("P001", "pin01")),
                    signal_ports=("pin00",),
                ),
            ),
            Design(library="demo_lib2", cell="demo_cell2", view="layout", key="demo2"),
        ],
        axes=[
            Axis(
                name="corner",
                values=(AxisValue(value="typical", flags={"--corner": "typical"}),),
                kind=AxisKind.VALUE,
                flags=("--corner", "--emssTechFile"),
                encoded_in_ewave_dir=True,
            ),
            Axis(
                name="equalCurrent",
                # ★ False = 「显式缺席」，不是「没有」（model.FlagValue 的契约 1）。
                #   往返之后它还得是 False，不能变成 "False" 字符串或者干脆丢了。
                values=(
                    AxisValue(value="on", flags={"--equalCurrent": True}, slug="on"),
                    AxisValue(value="off", flags={"--equalCurrent": False}, slug="off"),
                ),
                kind=AxisKind.TOGGLE,
                flags=("--equalCurrent",),
                short="eqI",
            ),
        ],
        runs=[
            make_run(axes_slug="base"),
            Run(
                run_id=f"{DESIGN_DIR}/eqI-off/{EWAVE_DIR}",
                design_key=DESIGN_DIR,
                axis_values={"corner": "typical", "equalCurrent": "off"},
                axes_slug="eqI-off",
                ewave_dir=EWAVE_DIR,
                work_dir=f"{batch_dir}/runs/{DESIGN_DIR}/eqI-off",
                status=RunStatus.FAILED,
                job=Job(job_id="1234", scheduler="fake", state=JobState.FAILED, exit_code=0),
                attempts=1,
                wall_seconds=12.5,
                argv=("ewave", "--nogui"),
                artifacts=("sparam/demo.s17p",),
                ports=PORTS_17,
                log_facts=LogFacts(ok=False, converged=None, peak_memory_mb=1024.5),
                message="崩了但 exit=0",
            ),
        ],
        streamout=[StreamoutTask(design_key=DESIGN_DIR, status=RunStatus.DONE, gds_path="gds/x.gds")],
        options=BatchOptions(max_parallel=6, timeout_seconds=None, archive_keep=("*.s[0-9]*p",)),
        defaults={"-e": "0.4", "--viaMode": "1"},
        extra_flags={"--tolerance": "0.005"},
        provenance=Provenance(tool_version="0.1.0.dev0", notes=("测试用",)),
    )


class StateRoundTrip(unittest.TestCase):
    def test_round_trip_is_field_by_field_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = f"{tmp}/demo".replace("\\", "/")
            path = f"{root}/batch.json"
            state = rich_state(root)
            layout.write_batch_state(path, state)
            # write 会顺手刷 provenance.updated_at（就地改），所以快照要在写之后取。
            before = dataclasses.asdict(state)
            after = dataclasses.asdict(layout.read_batch_state(path))

        problems, leaves = leaf_diff(after, before)
        self.assertEqual(problems, [], "往返之后有字段对不上")
        # 计数断言：空树的 diff 永远是绿的。
        self.assertGreater(len(leaves), 80, f"只比了 {len(leaves)} 个叶子，太少了")
        must_compare = {
            "batch_name",
            "schema_version",
            "runs[1].status",
            "runs[1].job.state",
            "runs[1].job.exit_code",
            "runs[1].log_facts.converged",
            "runs[1].wall_seconds",
            "designs[0].port_spec.mode",
            "designs[0].port_spec.mapping[0][1]",
            "designs[0].axis_overrides.temperature[1]",
            "axes[1].values[1].flags.--equalCurrent",
            "options.archive_keep[0]",
            "options.timeout_seconds",
            "provenance.updated_at",
            "streamout[0].status",
        }
        missing = sorted(must_compare - set(leaves))
        self.assertEqual(missing, [], f"这些字段根本没参与比较: {missing}")

    def test_round_trip_is_field_by_field_identical_negative(self) -> None:
        """同一个构造路径，读回来之后**故意改坏一个字段** → 比较逻辑必须指名报出来。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = f"{tmp}/demo".replace("\\", "/")
            path = f"{root}/batch.json"
            state = rich_state(root)
            layout.write_batch_state(path, state)
            before = dataclasses.asdict(state)
            reloaded = layout.read_batch_state(path)
            reloaded.runs[1].status = RunStatus.DONE  # 本来是 FAILED
            after = dataclasses.asdict(reloaded)

        problems, _ = leaf_diff(after, before)
        self.assertEqual(len(problems), 1, f"应当只报 runs[1].status 一条，实际: {problems}")
        self.assertIn("runs[1].status", problems[0])

    def test_false_flag_survives_the_round_trip(self) -> None:
        """`False` 是「显式缺席」不是「没有」—— 往返丢了它，目录名说 off、命令行说 on。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = f"{tmp}/demo".replace("\\", "/")
            path = f"{root}/batch.json"
            layout.write_batch_state(path, rich_state(root))
            reloaded = layout.read_batch_state(path)
        off_flags = reloaded.axes[1].values[1].flags
        self.assertIn("--equalCurrent", off_flags)
        self.assertIs(off_flags["--equalCurrent"], False)

    def test_future_schema_version_is_rejected(self) -> None:
        with self.assertRaises(StateError):
            layout.state_from_dict({"schema_version": model.SCHEMA_VERSION + 1})

    def test_current_schema_version_is_accepted_negative(self) -> None:
        """反向：当前版本不许被误拒（守卫是 `>`，不是 `>=`）。"""
        state = layout.state_from_dict({"schema_version": model.SCHEMA_VERSION, "batch_name": "x"})
        self.assertEqual(state.batch_name, "x")

    def test_missing_file_raises_state_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(StateError):
                layout.read_batch_state(f"{tmp}/nope/batch.json".replace("\\", "/"))

    def test_truncated_json_raises_state_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/batch.json".replace("\\", "/")
            write_file(path, '{"batch_name": "demo"')  # 写到一半断电
            with self.assertRaises(StateError):
                layout.read_batch_state(path)

    def test_unknown_enum_value_raises_state_error(self) -> None:
        with self.assertRaises(StateError):
            layout.state_from_dict(
                {"runs": [{"run_id": "r", "design_key": "d", "status": "half-done"}]}
            )


class AtomicWrite(unittest.TestCase):
    def test_no_temp_file_debris(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = f"{tmp}/demo".replace("\\", "/")
            path = f"{root}/batch.json"
            layout.write_batch_state(path, rich_state(root))
            layout.write_batch_state(path, rich_state(root))  # 再写一遍，覆盖路径也要干净
            self.assertEqual(
                sorted(os.listdir(root)),
                ["batch.json"],
                "目标目录里留下了临时文件残骸 —— 下一个 resume 的人会以为批次坏了",
            )

    def test_failed_replace_leaves_no_debris_and_keeps_the_old_file(self) -> None:
        """`os.replace` 挂了（跨卷 / 权限）也不许留半份 JSON，更不许把原文件弄坏。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = f"{tmp}/demo".replace("\\", "/")
            path = f"{root}/batch.json"
            layout.write_batch_state(path, rich_state(root))
            with open(path, encoding="utf-8") as handle:
                original = handle.read()

            broken = rich_state(root)
            broken.batch_name = "should-not-land"
            with mock.patch(
                "ewave_batch.core.layout.os.replace", side_effect=OSError("跨卷了")
            ):
                with self.assertRaises(StateError):
                    layout.write_batch_state(path, broken)

            self.assertEqual(sorted(os.listdir(root)), ["batch.json"], "留下了 .tmp 残骸")
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), original, "原来那份 batch.json 被弄坏了")

    def test_json_is_utf8_and_lf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = f"{tmp}/demo".replace("\\", "/")
            path = f"{root}/batch.json"
            layout.write_batch_state(path, rich_state(root))
            with open(path, "rb") as handle:
                raw = handle.read()
        self.assertNotIn(b"\r\n", raw, "batch.json 里有 CRLF")
        self.assertEqual(json.loads(raw.decode("utf-8"))["batch_name"], "demo")


# --------------------------------------------------------------------------
# runs.csv
# --------------------------------------------------------------------------


class RunsCsv(unittest.TestCase):
    def _write(self, tmp: str, state: BatchState) -> list[str]:
        path = f"{tmp}/runs.csv".replace("\\", "/")
        layout.write_runs_csv(path, state)
        with open(path, "rb") as handle:
            raw = handle.read()
        self.assertNotIn(b"\r\n", raw, "runs.csv 里有 CRLF —— csv 模块默认吐 \\r\\n，得关掉")
        return raw.decode("utf-8").split("\n")

    def test_header_and_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = f"{tmp}/demo".replace("\\", "/")
            lines = self._write(tmp, rich_state(root))

        # 表头是冻结的（model.RUNS_CSV_COLUMNS）—— 下游按列名读
        self.assertEqual(lines[0], ",".join(model.RUNS_CSV_COLUMNS))
        # 手写期望行。列序来自 RUNS_CSV_COLUMNS，取值来自上面 rich_state 里那个 failed run。
        self.assertEqual(
            lines[2],
            "demo_lib_demo_cell_layout,demo_lib_demo_cell_layout/eqI-off/typical_-40_0,"
            "eqI-off,typical_-40_0,failed,1234,,,12.5,1024.5,17,,sparam/demo.s17p,崩了但 exit=0",
        )
        self.assertEqual(lines[-1], "", "最后一行该是 LF 收尾")

    def test_header_and_row_negative(self) -> None:
        """同一个构造路径，只把 status 改掉 → 那一格必须跟着变。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = f"{tmp}/demo".replace("\\", "/")
            state = rich_state(root)
            state.runs[1].status = RunStatus.DONE
            lines = self._write(tmp, state)
        cells = lines[2].split(",")
        self.assertEqual(cells[model.RUNS_CSV_COLUMNS.index("status")], "done")
        self.assertNotIn(",failed,", lines[2])

    def test_none_is_empty_not_zero(self) -> None:
        """`None` 是「没测到」，0 秒是「真的 0 秒」—— csv 里不许把前者写成后者。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = f"{tmp}/demo".replace("\\", "/")
            lines = self._write(tmp, rich_state(root))
        cells = lines[1].split(",")
        self.assertEqual(cells[model.RUNS_CSV_COLUMNS.index("wall_seconds")], "")
        self.assertEqual(cells[model.RUNS_CSV_COLUMNS.index("peak_memory_mb")], "")


# --------------------------------------------------------------------------
# set_run_as_current —— 唯一一处写进 spine 的操作
# --------------------------------------------------------------------------


class SetRunAsCurrent(unittest.TestCase):
    def _official(self, tmp: str) -> str:
        """造一个官方 design 目录（BRIEF §5「官方流程的既有布局」的形状）。"""
        official = f"{tmp}/spine/{DESIGN_DIR}".replace("\\", "/")
        write_file(f"{official}/{EWAVE_DIR}/{CELL_STEM}.s17p", "OLD DATA\n")
        return official

    def test_copies_with_backup_and_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            official = self._official(tmp)
            paths, run = build_run_dir(f"{tmp}/demo".replace("\\", "/"), sparam_text="NEW DATA\n")

            actions = layout.set_run_as_current(paths, run, DESIGN, target_dir=official)

            dest = f"{official}/{EWAVE_DIR}/{CELL_STEM}.s17p"
            with open(dest, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "NEW DATA\n", "没把新数据落到官方路径上")
            backups = [n for n in os.listdir(f"{official}/{EWAVE_DIR}") if ".bak." in n]
            self.assertEqual(len(backups), 1, "覆盖前必须备份（BRIEF §5 三道约束之二）")
            with open(f"{official}/{EWAVE_DIR}/{backups[0]}", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "OLD DATA\n")

            log_path = f"{paths.logs_dir}/{layout.SET_CURRENT_LOG_NAME}"
            self.assertTrue(os.path.isfile(log_path), "必须记进日志（三道约束之三）")
            with open(log_path, encoding="utf-8") as handle:
                self.assertIn(run.run_id, handle.read())
            self.assertTrue(any("备份" in line for line in actions), actions)

    def test_copies_with_backup_and_log_negative(self) -> None:
        """同一个构造路径，只把 `dry_run` 打开 → spine 里一个字节都不许变。"""
        with tempfile.TemporaryDirectory() as tmp:
            official = self._official(tmp)
            paths, run = build_run_dir(f"{tmp}/demo".replace("\\", "/"), sparam_text="NEW DATA\n")

            actions = layout.set_run_as_current(paths, run, DESIGN, target_dir=official, dry_run=True)

            dest = f"{official}/{EWAVE_DIR}/{CELL_STEM}.s17p"
            with open(dest, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "OLD DATA\n", "dry-run 改了设计师的 spine")
            self.assertEqual(os.listdir(f"{official}/{EWAVE_DIR}"), [f"{CELL_STEM}.s17p"])
            self.assertFalse(os.path.exists(f"{paths.logs_dir}/{layout.SET_CURRENT_LOG_NAME}"))
            self.assertTrue(all(line.startswith("[dry-run]") for line in actions), actions)

    def test_missing_target_dir_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, run = build_run_dir(f"{tmp}/demo".replace("\\", "/"))
            with self.assertRaises(StateError):
                layout.set_run_as_current(
                    paths, run, DESIGN, target_dir=f"{tmp}/nope".replace("\\", "/")
                )

    def test_zero_byte_sparam_is_not_pushed_to_the_spine(self) -> None:
        """0 字节产物绝不许盖到设计师的 spine 上（BRIEF §10 实测过的坑）。"""
        with tempfile.TemporaryDirectory() as tmp:
            official = self._official(tmp)
            paths, run = build_run_dir(f"{tmp}/demo".replace("\\", "/"), sparam_text="")
            with self.assertRaises(StateError):
                layout.set_run_as_current(paths, run, DESIGN, target_dir=official)
            with open(f"{official}/{EWAVE_DIR}/{CELL_STEM}.s17p", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "OLD DATA\n")

    def test_target_may_be_the_ewave_dir_itself(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            official = self._official(tmp)
            paths, run = build_run_dir(f"{tmp}/demo".replace("\\", "/"), sparam_text="NEW\n")
            layout.set_run_as_current(
                paths, run, DESIGN, target_dir=f"{official}/{EWAVE_DIR}"
            )
            self.assertFalse(
                os.path.isdir(f"{official}/{EWAVE_DIR}/{EWAVE_DIR}"),
                "把 <corner>_<temp> 又套了一层",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
