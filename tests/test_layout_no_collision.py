"""同一个 axes-slug 下的多个 corner/temp 组合，留档文件**绝不许撞名**。

为什么单开一份测试：这是本工具存在的理由本身的回归测试。

用户的核心痛点（`PROJECT_BRIEF.md` §5「原生 GUI 的真实痛点」痛点 1）是——
官方的目录名只编码 corner 和 temperature，别的 flag 变了目录名不变，
**第二次跑会静默覆盖第一次**。我们在归档层要是自己再造一个同样的坑，那这个工具就白做了。

而它差一点就发生了：BRIEF §5 的树把命令留档画成固定名 `cmd.sh`，放在
`runs/<design>/<axes-slug>/` 这一层。但 `<axes-slug>` 按定义**不含** corner/temp
（那两个由 eWave 自己建的 `<corner>_<temp>/` 承担）⇒ 同一个 axes-slug 下的 N 个
corner/temp 组合共用同一个 `run_dir` ⇒ N 条命令行往同一个 `cmd.sh` 写。
每条的 `--corner` / `--temperature` / `--emssTechFile` 都不一样，覆盖之后
「这个 .sNp 是拿什么命令跑出来的」就永远答不上来了。

2026-08-18 夜跑 P1 的审查 agent 发现的（当时判为非阻塞、留给 P3）。
这里当场修掉并钉死，因为它属于"跑完才发现、且结果已经没法追溯"的那一类。

判据是**计数断言**：N 个 run ⇒ N 个互不相同的 cmd_sh / run_log。
只断言"某两个不相等"是不够的 —— 那种断言在轴多一根时会静默失效。
"""

from __future__ import annotations

import unittest

from ewave_batch import model
from ewave_batch.core import layout, matrix


def _batch_with(corners, temps, extra_axes=()):
    """造一个只在 corner/temperature（+ 可选额外轴）上扫的小批次。

    构造走 `matrix.expand_runs`，与产品代码同一条路径 —— 不自己拼 Run，
    否则测的就不是真实展开出来的那批 run 了。
    """
    catalog = matrix.builtin_axis_catalog()
    axes = [
        matrix.axis_with_values(catalog["corner"], list(corners)),
        matrix.axis_with_values(catalog["temperature"], list(temps)),
    ]
    for name, values in extra_axes:
        axes.append(matrix.axis_with_values(catalog[name], list(values)))
    design = model.Design(library="MY_LIB", cell="MY_CELL", view="layout")
    return design, axes, matrix.expand_runs([design], axes)


class CmdShNeverCollides(unittest.TestCase):
    def test_每个_run_一份_cmd_sh(self) -> None:
        design, _axes, runs = _batch_with(
            corners=["typical", "rcworst"], temps=["-40.0", "125.0"]
        )
        self.assertEqual(len(runs), 4, "2 corner × 2 温度应当展开成 4 个 run")

        paths = [layout.compute_run_paths("/b", design, r) for r in runs]
        cmd_shs = [p.cmd_sh for p in paths]
        run_logs = [p.run_log for p in paths]

        # ★ 计数断言：4 个 run ⇒ 4 个互不相同的留档文件。
        # 撞名的话这里是 4 != 1，报错信息直接把重复的路径印出来。
        self.assertEqual(
            len(set(cmd_shs)), len(runs),
            "cmd.sh 撞名了 —— N 个 run 的命令行会互相覆盖：\n  "
            + "\n  ".join(sorted(cmd_shs)),
        )
        self.assertEqual(
            len(set(run_logs)), len(runs),
            "run.log 撞名了：\n  " + "\n  ".join(sorted(run_logs)),
        )

        # 这 4 个 run 确实共用同一个 run_dir —— 也就是说上面那条不是碰巧成立的，
        # 而是真的在防一个会发生的碰撞。少了这条断言，测试可能因为"每个 run 本来就
        # 各有各的目录"而空过。
        self.assertEqual(
            len({p.run_dir for p in paths}), 1,
            "前提变了：这 4 个 run 不再共用 run_dir，本测试要重写",
        )

    def test_额外轴不改变结论(self) -> None:
        """多一根不进 eWave 目录名的轴（equalCurrent）时，仍然一 run 一份。

        额外轴进的是 `<axes-slug>`，corner/temp 进的是 eWave 自己建的那层。
        两边合起来才是完整身份 —— 只用其中一半命名就会撞。
        """
        design, _axes, runs = _batch_with(
            corners=["typical"],
            temps=["-40.0", "125.0"],
            extra_axes=[("equalCurrent", ["on", "off"])],
        )
        self.assertEqual(len(runs), 4)
        paths = [layout.compute_run_paths("/b", design, r) for r in runs]
        self.assertEqual(len({p.cmd_sh for p in paths}), 4)
        # 此时 run_dir 有两个（eqI-on / eqI-off），每个下面 2 个 run。
        self.assertEqual(len({p.run_dir for p in paths}), 2)

    def test_固定名会撞_negative(self) -> None:
        """反向：证明"固定名 cmd.sh"这个写法**真的**会撞，而不是我们在防一个不存在的问题。

        少了这条，上面两条无法区分「防护起作用」和「本来就不会撞」——
        而那正是 `docs/OVERNIGHT.md` 说的"空过的测试比没测更坏"。
        """
        design, _axes, runs = _batch_with(
            corners=["typical", "rcworst"], temps=["-40.0", "125.0"]
        )
        paths = [layout.compute_run_paths("/b", design, r) for r in runs]

        # 模拟旧写法：run_dir + 固定名。
        naive = {p.run_dir + "/" + model.CMD_SH_NAME for p in paths}
        self.assertEqual(
            len(naive), 1,
            "固定名竟然没撞 —— 说明前提变了，上面两条正向测试也就不再有意义",
        )
        self.assertLess(
            len(naive), len(runs),
            "固定名下 4 个 run 只剩 1 个留档文件，正是要防的静默覆盖",
        )

    def test_预测不出目录名时留档名仍然唯一(self) -> None:
        """corner/temperature 没都当轴扫 ⇒ `<corner>_<temp>/` 预测不出来 ⇒ **仍然不许撞名**。

        这是 2026-08-18 夜跑里两个并行 agent 之间的真实集成 bug，由本文件抓出来的：
        `matrix.expand_runs` 给 `Run.ewave_dir` 留空串（诚实地表示"预测不出来"），
        而 `layout.compute_run_paths` 当时**硬拒绝**空的 ewave_dir → 直接抛 StateError。

        为什么不能一拒了之：空的 ewave_dir 有两种**合法**来源 ——
        ①corner/temperature 没都当轴扫；②D12 的原生多值把温度折叠成 `--temperature=a,b,c`
        （一条命令跑出好几层目录，名字本来就不止一个）。硬拒绝等于把 D12 砍掉。

        所以放行，但留档名的词根退回 `run_id` 的最后一段 —— `expand_runs` 保证它唯一。
        撞名的后果和有目录名时一模一样，不能因为"预测不出来"就放松。
        """
        catalog = matrix.builtin_axis_catalog()
        design = model.Design(library="MY_LIB", cell="MY_CELL", view="layout")
        axes = [matrix.axis_with_values(catalog["temperature"], ["-40.0", "125.0"])]
        runs = matrix.expand_runs([design], axes)
        self.assertEqual(len(runs), 2)
        # 前提：这两个 run 的 ewave_dir 确实是空的（否则本测试测的不是这个场景）
        self.assertEqual({r.ewave_dir for r in runs}, {""})

        paths = [layout.compute_run_paths("/b", design, r) for r in runs]
        self.assertEqual(len({p.run_dir for p in paths}), 1, "前提：它们共用一个 run_dir")
        self.assertEqual(
            len({p.cmd_sh for p in paths}), 2,
            "预测不出目录名时留档名撞了：\n  " + "\n  ".join(sorted(p.cmd_sh for p in paths)),
        )
        self.assertEqual(len({p.run_log for p in paths}), 2)
        # ewave_dir 留空串（**不是** run_dir）—— 那是给 verify_run_outputs 的信号：
        # "跑完之后去 run_dir 里现场找"。填成 run_dir 会让它以为产物就在外层。
        self.assertEqual({p.ewave_dir for p in paths}, {""})

    def test_补上单值轴就能预测_negative(self) -> None:
        """反向：把 corner 补成单值轴 → 目录名立刻可预测，且单值轴不污染 slug。

        少了这条，上一条无法区分「退路机制管用」和「本来就没人能预测」。
        """
        catalog = matrix.builtin_axis_catalog()
        design = model.Design(library="MY_LIB", cell="MY_CELL", view="layout")
        axes = [
            matrix.axis_with_values(catalog["temperature"], ["-40.0", "125.0"]),
            matrix.axis_with_values(catalog["corner"], ["typical"]),
        ]
        runs = matrix.expand_runs([design], axes)
        self.assertEqual(len(runs), 2, "单值轴不该让 run 数变多")
        self.assertEqual(
            [r.ewave_dir for r in runs], ["typical_-40_0", "typical_125_0"],
            "补上单值 corner 之后目录名就该预测得出来了",
        )
        paths = [layout.compute_run_paths("/b", design, r) for r in runs]
        self.assertEqual(len({p.cmd_sh for p in paths}), 2)
        for run, path in zip(runs, paths):
            # 单值轴进 eWave 目录名（那层由 eWave 建，与"变不变"无关），
            # 但**不**进 axes-slug —— 于是单轴场景下目录名与官方逐字一致。
            self.assertNotIn("typical", run.axes_slug)
            self.assertTrue(path.cmd_sh.endswith("/cmd_" + run.ewave_dir + ".sh"))



class DiscoverEwaveDirAtVerifyTime(unittest.TestCase):
    """预测不出目录名时，验收阶段**现场发现** eWave 建的那层。

    分工是「规划要预测、验收只需要看」：跑完之后目录已经存在了，
    不确定性在那一刻自然消失 —— 不该为了规划期的方便，在验收期猜。
    """

    def _run_dir_with(self, tmp, subdirs):
        """造一个 run_dir，里面按 subdirs 建 eWave 风格的输出子目录。"""
        import os

        run_dir = os.path.join(tmp, "runs", "L_C_v", "base")
        os.makedirs(run_dir, exist_ok=True)
        for name, files in subdirs.items():
            d = os.path.join(run_dir, name)
            os.makedirs(d, exist_ok=True)
            for fname, content in files.items():
                with open(os.path.join(d, fname), "w", encoding="utf-8") as fh:
                    fh.write(content)
        return run_dir

    def test_唯一一层被找到(self):
        import tempfile

        from ewave_batch.core.layout import _discover_ewave_dirs

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run_dir_with(
                tmp, {"typical_-40_0": {"x.s4p": "!", "ewave.log": "ok"}}
            )
            found = _discover_ewave_dirs(run_dir)
            self.assertEqual(len(found), 1)
            self.assertTrue(found[0].endswith("/typical_-40_0"))

    def test_不是_ewave_产物的子目录不算_negative(self):
        """反向：光有个目录不算数，必须真有 eWave 的产物或日志在里面。

        少了这条，任何我们自己建的临时目录都会被当成"产物在这儿"，
        于是验收会对着一个空目录报"找不到 .sNp"而不是"这个 run 没跑起来" ——
        两种结论会把人引向完全不同的排查方向。
        """
        import tempfile

        from ewave_batch.core.layout import _discover_ewave_dirs

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run_dir_with(
                tmp,
                {
                    "scratch": {"notes.txt": "not an ewave output"},
                    "typical_-40_0": {"x.s4p": "!"},
                },
            )
            found = _discover_ewave_dirs(run_dir)
            self.assertEqual(len(found), 1, "只有真装着产物的那层才算：%r" % (found,))
            self.assertTrue(found[0].endswith("/typical_-40_0"))

    def test_多层时不猜(self):
        """原生多值（D12）一次跑出好几层 ⇒ 老实报出来，不猜哪层属于这个 run。

        猜错会把别的组合的产物算到这个 run 头上 —— 那种错误无声无息，
        而且会一路传到归档和下游比对里。
        """
        import tempfile

        from ewave_batch import model
        from ewave_batch.core import layout

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run_dir_with(
                tmp,
                {
                    "typical_-40_0": {"a.s4p": "!"},
                    "typical_125_0": {"b.s4p": "!"},
                },
            )
            paths = model.RunPaths(
                batch_dir=tmp, batch_json="", runs_csv="", gds_dir="", gdsout_dir="",
                sparam_dir="", logs_dir="", design_gds="", design_gdsout="",
                run_dir=run_dir, cmd_sh="", ewave_dir="", run_log="", sparam_prefix="",
            )
            report = layout.verify_run_outputs(paths, model.Run(run_id="r", design_key="d"))
            self.assertFalse(report.ok)
            joined = " ".join(report.reasons)
            self.assertIn("2", joined, "该报出发现了几层：" + joined)
            self.assertIn("Next", joined, "报错要带下一步怎么办：" + joined)

if __name__ == "__main__":
    unittest.main()
