"""`plan.log_path` 与 `RunPaths.run_log` 必须给出**同一个**文件名。

## 这条测试在防什么

`sched.donau` 把 `plan.log_path` 当 `dsub -o` 用（作业 stdout 落到那个文件）。
而 `<axes-slug>` 按定义**不含** corner/temperature ⇒ 同一个 `work_dir` 底下住着 N 个 run。
所以 `log_path` 一旦是固定名，N 个 job 的 stdout 会全部写进同一份文件。

**症状不是崩溃，是日志「看起来有」** —— 出事那天你翻开它，里面是几个 job 交织的输出，
而且没有任何东西提示你它被混过。这类"安静的错"正是本项目最防的一种。

## 为什么必须由测试来钉，而不是让两边共用一个函数

`core.cmd` 刻意**不** import `core.layout`（那会把"拼命令"绑到"算目录布局"上，
而 `cmd` 要在红区 dry-run 里独立可用）。两边因此各有一份取名逻辑 ——
各写各的才是真危险，所以用一条跨模块断言把它们焊在一起。

## 出处

2026-08-18 已经为 `cmd.sh` 修过一次同样的坑（见 `model.CMD_SH_TEMPLATE` 的 docstring
与 `tests/test_layout_no_collision.py`）。当时改的是 `RunPaths`，
但 `core.cmd.build_command_plan` 里另有一份写死的 `run.log` —— **同一个坑的第二处**，
2026-08-19 夜跑 P5 审查转达实现方的报备时才发现。
"""

from __future__ import annotations

import posixpath
import unittest

from ewave_batch import model
from ewave_batch.core import cmd, layout, matrix


def _batch(corners, temps):
    """用真实的 `expand_runs` 展开，不自己拼 Run —— 否则测的就不是真实产物。"""
    catalog = matrix.builtin_axis_catalog()
    axes = [
        matrix.axis_with_values(catalog["corner"], list(corners)),
        matrix.axis_with_values(catalog["temperature"], list(temps)),
    ]
    design = model.Design(library="MY_LIB", cell="MY_CELL", view="layout")
    return design, matrix.expand_runs([design], axes)


class LogPathNeverCollides(unittest.TestCase):
    def test_每个_run_一份日志(self) -> None:
        design, runs = _batch(["typical", "rcworst"], ["-40.0", "125.0"])
        self.assertEqual(len(runs), 4)

        names = [cmd._run_log_name(r) for r in runs]
        # ★ 计数断言：4 个 run ⇒ 4 个互不相同的日志名。
        self.assertEqual(
            len(set(names)), len(runs),
            "log_path 撞名了 —— N 个 job 的 stdout 会混进同一份文件：\n  "
            + "\n  ".join(sorted(names)),
        )
        # 前提：它们确实共用同一个 work_dir。少了这条，上面那条可能因为
        # "每个 run 本来就各有各的目录"而空过。
        paths = [layout.compute_run_paths("/b", design, r) for r in runs]
        self.assertEqual(len({p.run_dir for p in paths}), 1)

    def test_与_layout_给出的名字逐字相同(self) -> None:
        """跨模块契约：两边各算各的，结果必须一个字节都不差。

        这是本文件存在的**主要理由** —— `core.cmd` 不 import `core.layout`，
        两份取名逻辑只能靠这条断言绑在一起。
        """
        design, runs = _batch(["typical"], ["-40.0", "125.0"])
        for run in runs:
            paths = layout.compute_run_paths("/b", design, run)
            from_cmd = posixpath.join(run.work_dir or paths.run_dir, cmd._run_log_name(run))
            self.assertEqual(
                posixpath.basename(from_cmd),
                posixpath.basename(paths.run_log),
                f"run {run.run_id}: cmd 与 layout 对日志文件名的说法不一致 —— "
                "两边各写各的就是这么漂移的",
            )

    def test_固定名会撞_negative(self) -> None:
        """反向：证明"固定 `run.log`"这个写法**真的**会撞。

        少了这条，上面两条无法区分「防护起作用」和「本来就不会撞」。
        """
        design, runs = _batch(["typical", "rcworst"], ["-40.0", "125.0"])
        paths = [layout.compute_run_paths("/b", design, r) for r in runs]
        naive = {p.run_dir + "/" + model.RUN_LOG_NAME for p in paths}
        self.assertEqual(len(naive), 1, "固定名竟然没撞 —— 前提变了，上面两条也就没意义了")
        self.assertLess(len(naive), len(runs))

    def test_plan_的_log_path_落在_work_dir_下(self) -> None:
        """顺带钉死它是**绝对**落在 run 的 work_dir 里，而不是某个共享目录。"""
        catalog = matrix.builtin_axis_catalog()
        axes = [
            matrix.axis_with_values(catalog["corner"], ["typical"]),
            matrix.axis_with_values(catalog["temperature"], ["-40.0"]),
        ]
        design = model.Design(library="MY_LIB", cell="MY_CELL", view="layout")
        runs = matrix.expand_runs([design], axes)
        run = runs[0]
        paths = layout.compute_run_paths("/b", design, run)
        run = model.Run(
            run_id=run.run_id, design_key=run.design_key, axis_values=run.axis_values,
            axes_slug=run.axes_slug, ewave_dir=run.ewave_dir, work_dir=paths.run_dir,
        )
        ctx = model.PlanContext(
            design=design,
            # 全合成的假坐标：corner 轴要靠 ptxt 模板推 --emssTechFile，
            # 少了它 build_flag_layers 会（正确地）拒绝 —— 那条守卫防的是
            # 「目录名说一个工艺角、实际算的是另一个」。
            facts=model.SiteFacts(
                ewave_bin="ewave",
                ptxt_dir="/fake/pdk/ptxt_enc",
                ptxt_name_template="FAKEPDK_{corner}_encrypted.ptxt",
                corner="typical",
            ),
            axes=tuple(axes),
        )
        plan = cmd.build_command_plan(run, ctx)
        self.assertTrue(
            plan.log_path.startswith(paths.run_dir + "/"),
            f"log_path 跑到 work_dir 外面去了：{plan.log_path}",
        )
        self.assertEqual(posixpath.basename(plan.log_path), posixpath.basename(paths.run_log))


if __name__ == "__main__":
    unittest.main()
