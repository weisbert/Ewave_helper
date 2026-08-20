"""★ 红区只读 dry-run 必须把 `spec.groups` 一起展开 —— 2026-08-19 复核实测。

硬约束 3：本机没有 ewave / dsub，红区那趟 dry-run 是**唯一**能在真提交之前核对落点的手段，
`docs/REDZONE_DRYRUN.md` §4 明写「退 0 就往下走」。

漏掉组的后果不是"少打印几行"：`<axes-slug>` 的口径是**全批次**的，组的取值会改掉
**基线自己**的目录名（`base/...` 变成 `eqI-on/...`）。于是用户在预检里看到一份
`base/...` 的清单、退 0、放心提交，真跑落的却是 `eqI-on/...` 外加一整组他从没在预检里
见过的 `eqI-off/...`。

判据用的是「同一份 spec 交给两条路，落点必须逐字相同」：
`core.spec.spec_to_batch`（CLI / GUI 走的那条）与 `redzone_dryrun.build_report`。
只断言"dry-run 里有 4 条"是不够的 —— 那不能证明它和真跑对得上。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from ewave_batch import redzone_dryrun as rz
from ewave_batch.core import layout as layout_module
from ewave_batch.core import spec as spec_module

ROOT = Path(__file__).resolve().parent.parent
OFFDIR = ROOT / "tests" / "fixtures" / "offdir_synthetic"

# 工具解析不读真实环境（"传了 env 就只看 env"）——否则 argv 取决于跑测试那台机器的 PATH。
NO_TOOLS_ENV: dict = {}

SPEC = {
    "designs": [{"library": "MY_LIB", "cell": "MY_CELL", "view": "layout_em"}],
    "axes": {
        "corner": ["typical"],
        "temperature": ["-40.0", "55.0", "125.0"],
        "fullWave": ["off"],
        "equalCurrent": ["on"],
    },
    "groups": [
        {
            "name": "eqcur-off",
            "axes": {"temperature": ["55.0"], "equalCurrent": ["off"]},
        }
    ],
}

# 手写期望表。来源：契约第 6 条（组的取值算进"全批次在变的轴" ⇒ equalCurrent 进 slug，
# 基线跟着从 base/... 变成 eqI-on/...）。**不是从实现里抄回来的。**
EXPECTED = (
    ("MY_LIB_MY_CELL_layout_em/eqI-on/typical_-40_0", "base"),
    ("MY_LIB_MY_CELL_layout_em/eqI-on/typical_55_0", "base"),
    ("MY_LIB_MY_CELL_layout_em/eqI-on/typical_125_0", "base"),
    ("MY_LIB_MY_CELL_layout_em/eqI-off/typical_55_0", "eqcur-off"),
)


class GroupsReachTheDryRun(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.batch_root = os.path.join(self.tmp.name, "batches")
        self.spec_path = os.path.join(self.tmp.name, "spec.json")
        with open(self.spec_path, "w", encoding="utf-8") as handle:
            json.dump(SPEC, handle)

    def _report(self, spec_path: str):
        return rz.build_report(
            str(OFFDIR),
            spec_path=spec_path,
            batch_root=self.batch_root,
            batch_name="dryrun",
            env=NO_TOOLS_ENV,
        )

    def _for_real(self, spec_path: str):
        spec = spec_module.load_spec(spec_path)
        return spec_module.spec_to_batch(spec, batch_root=self.batch_root)

    def test_dry_run_lists_exactly_the_runs_the_real_run_would_create(self) -> None:
        report = self._report(self.spec_path)
        got = tuple((plan.run.run_id, plan.run.group) for plan in report.stage_two)
        self.assertEqual(got, EXPECTED)
        real = tuple((run.run_id, run.group) for run in self._for_real(self.spec_path).runs)
        self.assertEqual(got, real, "预检和真跑必须逐字相同，否则预检退 0 毫无意义")

    def test_work_dirs_match_the_real_run(self) -> None:
        """真正要核对的是 `--workDir` 落在哪 —— 目录名对不上就是白检一场。"""
        report = self._report(self.spec_path)
        state = self._for_real(self.spec_path)
        design = state.designs[0]
        real_dirs = [
            layout_module.compute_run_paths(state.batch_dir, design, run).run_dir
            for run in state.runs
        ]
        dry_dirs = [
            layout_module.compute_run_paths(state.batch_dir, design, plan.run).run_dir
            for plan in report.stage_two
        ]
        self.assertEqual(dry_dirs, real_dirs)
        self.assertEqual(len(set(dry_dirs)), 2, "前提：两个 slug ⇒ 两个 run_dir")

    def test_the_group_is_mentioned_in_the_notes(self) -> None:
        """加组会改掉基线的目录名 —— 这件反直觉的事必须在报告里说出来，不能让人自己发现。"""
        report = self._report(self.spec_path)
        joined = "\n".join(report.notes)
        self.assertIn("eqcur-off", joined)
        self.assertIn("run group", joined)

    def test_a_spec_without_groups_is_unchanged_negative(self) -> None:
        """★ 回归闸门：不写 `groups:` 时这条路必须与加这个参数之前**逐字相同**。

        同时它也是上一条的对照组：没有组 ⇒ equalCurrent 不在变 ⇒ 不进 slug
        ⇒ 目录名退回 `base/...`。两份输出不同才说明上面测的是真东西。
        """
        plain = dict(SPEC)
        plain.pop("groups")
        path = os.path.join(self.tmp.name, "plain.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(plain, handle)
        report = self._report(path)
        got = [plan.run.run_id for plan in report.stage_two]
        self.assertEqual(
            got,
            [
                "MY_LIB_MY_CELL_layout_em/base/typical_-40_0",
                "MY_LIB_MY_CELL_layout_em/base/typical_55_0",
                "MY_LIB_MY_CELL_layout_em/base/typical_125_0",
            ],
        )
        self.assertEqual(
            got, [run.run_id for run in self._for_real(path).runs]
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
