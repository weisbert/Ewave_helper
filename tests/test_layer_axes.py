"""两根层轴 —— `layer2d`（哪几层用 2D 建模）和 `layermesh`（哪几层换网格）。

## 为什么是两根而不是一根

它们在 eWave 里是两个独立的 flag：

| 轴 | flag | 管什么 |
|---|---|---|
| `layer2d` | `--3d`（保持 3D 的白名单） | 用不用 3D 结构建模 |
| `layermesh` | `--edgeDist`（逐层） | 这些层的网格多大 |

2026-08-31 上午先做成了一根 `simplify2d`（取值 = µm 数，顺带把层降 2D）。
当天下午用户指出那是把两个功能区捏在了一起：「只想降 2D、网格不动」表达不出来，
也没法分别扫。于是拆开 —— `TwoIndependentThings` 那一组就是这条要求的锁。

`--thinMaxfactor` 是**第三件事**（全局、不逐层），从轴上摘掉了，走默认表层。

## `--3d` 是算出来的补集

eWave 的 `--3d` 是**保持 3D 的白名单**：没列进去的层**静默**退成 2D。
而人想的是反过来的一句话 ——「把这几层降下去」。2026-08-28 手写白名单时漏了一层，
那层被无声无息地降成 2D，run 照样跑完、数字还挺像，是事后数元素数才发现的。
所以口径是：用户只勾想降的层，白名单由工具算（`stack - simplify`）——
"漏写"的后果从「那层被降级」变成「那层留在 3D」= 不改变。

## 层名从 eWave 日志里读，不让用户打

（用户 2026-08-31：「你不能指望用户自己填写 --3d 这样的指令」）
官方 run 目录里的日志两段合起来就是权威答案，见 `TheToolReadsTheLayers`。

## 站点标识符零出现

层名一律是 `LOW1` / `TOP2` 这种编出来的占位符。真实金属层名是 PDK 叠层坐标
（CLAUDE.md 硬约束 1b），源码和测试里都不许有 —— 也正因如此
`builtin_axis_catalog()` 里那两根轴各自只有 `off` 一个取值。
`4` / `1` / `0.5` 是 eWave 的工具语义（µm / nm），不是站点身份。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from dataclasses import replace

import gui.state as gui_state
from ewave_batch.core import cmd as cmd_module
from ewave_batch.core import discover as discover_module
from ewave_batch.core import logparse as logparse_module
from ewave_batch.core import matrix
from ewave_batch.core import spec as spec_module
from ewave_batch.model import (
    BatchOptions,
    BatchSpec,
    Design,
    LayerModel,
    PlanContext,
    SiteFacts,
    SpecError,
)

# ==========================================================================
# 期望表（手写字面量）
# ==========================================================================

STACK = ("LOW1", "LOW2", "LOW3", "LOW4", "TOP1", "TOP2")
"""一个编出来的金属叠层，从下到上。"""

MIDDLE = ("LOW2", "LOW3", "LOW4")
"""中间三层 —— 上下两头都留着是有意的：补集若只会"砍掉末尾"，`LOW1` 留不留就试不出来，
而 `LOW1` 正是 2026-08-28 差点漏掉的那一类（底层，元素数极少、对速度毫无影响，
但"没打算改的东西被改了"）。"""

EXPECTED_KEEP_3D = "LOW1,TOP1,TOP2"
"""`--3d` 的期望值：叠层减去中间三层，**按叠层的顺序**。"""

EXPECTED_EDGE_DIST = "LOW2,4 LOW3,4 LOW4,4"
"""`--edgeDist` 的期望值：逐层写 `层,值`，空格分隔，同样按叠层的顺序。
形状抄自 `references/probes/speed3d_run_20260828.txt` 里那条真跑过的命令。"""


def _rmtree(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _model(**over: object) -> LayerModel:
    fields: dict = {"stack": STACK, "simplify": MIDDLE, "mesh_layers": MIDDLE}
    fields.update(over)
    return LayerModel(**fields)  # type: ignore[arg-type]


def _flag(argv: list[str], name: str) -> str | None:
    prefix = name + "="
    for token in argv:
        if token.startswith(prefix):
            return token[len(prefix) :]
    return None


def _argv(*, two_d: list[str], mesh: list[str], mesh_size: str = "0.5") -> dict[str, list[str]]:
    """把两根层轴的取值展开成 run，返回 `{run_id: argv}`。**走真正那条拼命令的路径。**"""
    design = Design(library="MY_LIB", cell="CELL_A", view="layout_em")
    catalog = matrix.builtin_axis_catalog()
    axes = [
        matrix.axis_with_values(catalog["temperature"], ["25.0"]),
        matrix.axis_with_values(catalog["mesh"], [mesh_size]),
    ]
    if two_d:
        axes.append(matrix.layer2d_axis(_model(), two_d))
    if mesh:
        axes.append(matrix.layermesh_axis(_model(), mesh))
    facts = SiteFacts(ewave_bin="/fake/ewave", key="FAKEKEY", ptxt="/fake/typical.ptxt")
    ctx = PlanContext(
        design=design, axes=axes, facts=facts, options=BatchOptions(), batch_dir="/fake/batch"
    )
    out: dict[str, list[str]] = {}
    for run in matrix.expand_runs([design], axes):
        placed = replace(run, work_dir="/fake/batch/runs/" + (run.axes_slug or "base"))
        out[run.run_id] = list(cmd_module.build_command_plan(placed, ctx).argv)
    return out


# ==========================================================================


class TwoIndependentThings(unittest.TestCase):
    """🚨 **用户 2026-08-31 的要求本身。**

    「M2-M6 定制化为 4um」和「M2-M6 设置为 2D」是两个功能区，工具不许替用户绑在一起。
    这一组把"四种组合都表达得出来"钉死。
    """

    def test_2d_only_touches_the_3d_flag(self) -> None:
        self.assertEqual(matrix.layer2d_flags_for(_model(), "on"), {"--3d": EXPECTED_KEEP_3D})

    def test_mesh_only_touches_the_edgedist_flag(self) -> None:
        self.assertEqual(
            matrix.layermesh_flags_for(_model(), "4"), {"--edgeDist": EXPECTED_EDGE_DIST}
        )

    def test_the_two_axes_own_disjoint_flags(self) -> None:
        """撞了同一个 flag 就等于没拆 —— 后展开的那根会把前一根盖掉。"""
        self.assertEqual(set(matrix.LAYER2D_FLAGS) & set(matrix.LAYERMESH_FLAGS), set())

    def test_2d_without_touching_the_mesh(self) -> None:
        """只降 2D、网格不动 —— 老的单轴设计**表达不出来**这一种。"""
        argv = _argv(two_d=["on"], mesh=[])["MY_LIB_CELL_A_layout_em/base/25_0"]
        self.assertEqual(_flag(argv, "--3d"), EXPECTED_KEEP_3D)
        self.assertIsNone(_flag(argv, "--edgeDist"))

    def test_mesh_without_making_anything_2d(self) -> None:
        """只换网格、不降 2D —— 3D 的层照样可以换网格。老设计同样表达不出来。"""
        argv = _argv(two_d=[], mesh=["4"])["MY_LIB_CELL_A_layout_em/base/25_0"]
        self.assertEqual(_flag(argv, "--edgeDist"), EXPECTED_EDGE_DIST)
        self.assertIsNone(_flag(argv, "--3d"))

    def test_both_at_once(self) -> None:
        argv = _argv(two_d=["on"], mesh=["4"])["MY_LIB_CELL_A_layout_em/base/25_0"]
        self.assertEqual(_flag(argv, "--3d"), EXPECTED_KEEP_3D)
        self.assertEqual(_flag(argv, "--edgeDist"), EXPECTED_EDGE_DIST)

    def test_they_sweep_independently(self) -> None:
        """两根轴各扫各的 ⇒ 2x2 四个组合，每个一个独立目录。

        这正是"分别扫"这件事在目录上的样子 —— 老的单轴只能给出 off / 4 两格。
        """
        runs = _argv(two_d=["off", "on"], mesh=["off", "4"])
        self.assertEqual(
            sorted(runs),
            [
                "MY_LIB_CELL_A_layout_em/2d-off__lmesh-4/25_0",
                "MY_LIB_CELL_A_layout_em/2d-off__lmesh-off/25_0",
                "MY_LIB_CELL_A_layout_em/2d-on__lmesh-4/25_0",
                "MY_LIB_CELL_A_layout_em/2d-on__lmesh-off/25_0",
            ],
        )

    def test_the_layer_lists_are_two_separate_lists(self) -> None:
        """降 2D 的层和换网格的层可以不一样 —— 那是用户的选择，不是工具的决定。"""
        model = _model(simplify=("LOW2",), mesh_layers=("TOP1",))
        self.assertEqual(matrix.layer2d_flags_for(model, "on")["--3d"], "LOW1,LOW3,LOW4,TOP1,TOP2")
        self.assertEqual(matrix.layermesh_flags_for(model, "4")["--edgeDist"], "TOP1,4")

    def test_a_mesh_size_on_the_2d_row_is_refused_with_a_pointer(self) -> None:
        """老习惯（在 2D 那行填 4）要被拦住，而且要说清楚该去哪填。"""
        with self.assertRaises(SpecError) as caught:
            matrix.layer2d_axis(_model(), ["4"])
        message = str(caught.exception)
        self.assertIn("not a legal value", message)
        self.assertIn(matrix.AXIS_LAYERMESH, message)


class TheWhiteListIsComputed(unittest.TestCase):
    """`--3d` 是算出来的补集，不是用户输入。"""

    def test_keep_3d_is_the_stack_minus_the_ticked_layers(self) -> None:
        self.assertEqual(matrix.layer2d_flags_for(_model(), "on")["--3d"], EXPECTED_KEEP_3D)

    def test_a_layer_nobody_ticked_stays_3d(self) -> None:
        """漏写的后果必须是"不改变" —— 2026-08-28 那个坑的回归。"""
        keep = matrix.layer2d_flags_for(_model(simplify=("LOW3",)), "on")["--3d"].split(",")
        for untouched in ("LOW1", "LOW2", "LOW4", "TOP1", "TOP2"):
            self.assertIn(untouched, keep)

    def test_no_ticked_layer_leaks_into_the_keep_3d_list(self) -> None:
        keep = matrix.layer2d_flags_for(_model(), "on")["--3d"].split(",")
        for layer in MIDDLE:
            self.assertNotIn(layer, keep)

    def test_the_order_follows_the_stack_not_the_clicking(self) -> None:
        """两次跑的 cmd.sh 要能逐字比 ⇒ 命令行与点击顺序无关。"""
        shuffled = _model(simplify=("LOW4", "LOW2", "LOW3"), mesh_layers=("LOW4", "LOW2", "LOW3"))
        self.assertEqual(matrix.layer2d_flags_for(shuffled, "on")["--3d"], EXPECTED_KEEP_3D)
        self.assertEqual(
            matrix.layermesh_flags_for(shuffled, "4")["--edgeDist"], EXPECTED_EDGE_DIST
        )


class OffIsReallyOff(unittest.TestCase):
    """`off` 那一格的命令行必须与「根本没有这根轴」逐字相同 —— 否则基线不是基线。"""

    def test_off_adds_nothing(self) -> None:
        without = list(_argv(two_d=[], mesh=[]).values())[0]
        with_off = list(_argv(two_d=["off"], mesh=["off"]).values())[0]
        self.assertEqual(with_off, without)

    def test_off_cancels_instead_of_merely_omitting(self) -> None:
        """给的是 `False`（显式缺席）而不是"不写" —— "不写"盖不掉学来的默认表。"""
        self.assertEqual(matrix.layer2d_flags_for(_model(), "off"), {"--3d": False})
        self.assertEqual(matrix.layermesh_flags_for(_model(), "off"), {"--edgeDist": False})


class GlobalAndPerLayerCoexist(unittest.TestCase):
    """mesh 轴的全局 `-e` 和 layermesh 的逐层 `--edgeDist` **必须同时在命令行上**。"""

    def test_both_survive_into_the_argv(self) -> None:
        argv = _argv(two_d=[], mesh=["4"], mesh_size="0.5")[
            "MY_LIB_CELL_A_layout_em/base/25_0"
        ]
        self.assertEqual(argv[argv.index("-e") + 1], "0.5")
        self.assertEqual(_flag(argv, "--edgeDist"), EXPECTED_EDGE_DIST)

    def test_the_per_layer_form_comes_after_the_global_one(self) -> None:
        """手册那个例子的顺序（`--edgeDist=2 --edgeDist=M1,0.8`）：先全局，后逐层。

        `sorted()` 默认会把 `--edgeDist` 排到 `-e` 前面，和验证过的那条反过来 ——
        `core.cmd.RENDER_LAST` 就是为这一条存在的，而本机没有 eWave 验证不了顺序。
        """
        argv = _argv(two_d=[], mesh=["4"])["MY_LIB_CELL_A_layout_em/base/25_0"]
        self.assertLess(
            argv.index("-e"),
            next(i for i, t in enumerate(argv) if t.startswith("--edgeDist=")),
        )

    def test_mesh_axis_still_writes_the_short_name(self) -> None:
        """🚨 **整个"全局 + 逐层"能成立的全部依据。**

        mesh 轴写 `-e`、layermesh 写 `--edgeDist` —— 两个不同的 dict 键，
        于是两条都进命令行。谁把 mesh 轴改成长名，两根轴就撞同一个键，
        后合并的那个把前一个整个吃掉，而两种结果都跑得完。
        """
        mesh = matrix.builtin_axis_catalog()["mesh"]
        self.assertIn("-e", mesh.flags)
        self.assertNotIn("--edgeDist", mesh.flags)
        self.assertEqual(set(mesh.flags) & set(matrix.LAYERMESH_FLAGS), set())


class ThinMaxFactorIsAThirdThing(unittest.TestCase):
    """`--thinMaxfactor` 全局、不逐层 ⇒ **不属于任何一根层轴**。"""

    def test_neither_axis_owns_it(self) -> None:
        self.assertNotIn(matrix.THIN_MAX_FACTOR_FLAG, matrix.LAYER2D_FLAGS)
        self.assertNotIn(matrix.THIN_MAX_FACTOR_FLAG, matrix.LAYERMESH_FLAGS)

    def test_it_reaches_the_defaults_layer(self) -> None:
        spec = BatchSpec(
            designs=[Design(library="MY_LIB", cell="CELL_A", view="layout_em")],
            layer_model=LayerModel(stack=STACK, thin_max_factor="1"),
        )
        state = spec_module.spec_to_batch(spec, batch_root="/fake")
        self.assertEqual(state.defaults[matrix.THIN_MAX_FACTOR_FLAG], "1")

    def test_it_does_not_need_either_axis_to_be_on(self) -> None:
        """独立就是独立：两根轴都不开，它照样下发。"""
        spec = BatchSpec(
            designs=[Design(library="MY_LIB", cell="CELL_A", view="layout_em")],
            axes=[matrix.layer2d_axis(_model(), ["off"])],
            layer_model=LayerModel(stack=STACK, thin_max_factor="1"),
        )
        state = spec_module.spec_to_batch(spec, batch_root="/fake")
        self.assertIn(matrix.THIN_MAX_FACTOR_FLAG, state.defaults)

    def test_empty_means_the_flag_is_not_given(self) -> None:
        spec = BatchSpec(
            designs=[Design(library="MY_LIB", cell="CELL_A", view="layout_em")],
            layer_model=LayerModel(stack=STACK),
        )
        state = spec_module.spec_to_batch(spec, batch_root="/fake")
        self.assertNotIn(matrix.THIN_MAX_FACTOR_FLAG, state.defaults)

    def test_an_explicit_default_wins(self) -> None:
        """用户在 `defaults:` 里手写的更下游，赢。"""
        spec = BatchSpec(
            designs=[Design(library="MY_LIB", cell="CELL_A", view="layout_em")],
            defaults={matrix.THIN_MAX_FACTOR_FLAG: "50"},
            layer_model=LayerModel(stack=STACK, thin_max_factor="1"),
        )
        state = spec_module.spec_to_batch(spec, batch_root="/fake")
        self.assertEqual(state.defaults[matrix.THIN_MAX_FACTOR_FLAG], "50")

    def test_the_gui_does_not_switch_it_on_by_itself(self) -> None:
        """初值必须是空的 —— 它是独立开关，非空初值 = 每个人的每一批都被悄悄改了网格。"""
        self.assertEqual(gui_state.DEFAULT_THIN_MAX_FACTOR, "")
        self.assertEqual(gui_state.GuiState().layer_model().thin_max_factor, "")


class RefusesInsteadOfGuessing(unittest.TestCase):
    """每一条不拦的代价都是"跑得完、数字也像"那一类。"""

    def _refuse(self, build, model: LayerModel, values: list[str], needle: str) -> None:
        with self.assertRaises(SpecError) as caught:
            build(model, values)
        self.assertIn(needle, str(caught.exception))

    def test_an_empty_stack_is_refused(self) -> None:
        self._refuse(matrix.layer2d_axis, _model(stack=()), ["on"], "metal stack is empty")

    def test_ticking_no_layer_is_refused(self) -> None:
        self._refuse(matrix.layer2d_axis, _model(simplify=()), ["on"], "no layer is selected")
        self._refuse(matrix.layermesh_axis, _model(mesh_layers=()), ["4"], "no layer is selected")

    def test_a_layer_outside_the_stack_is_refused(self) -> None:
        """eWave 对不认识的层名**不报错**，就是不生效 —— 拼错了在命令行上看不出来。"""
        self._refuse(
            matrix.layer2d_axis, _model(simplify=("LOW2", "TYPO")), ["on"], "not in the metal stack"
        )

    def test_making_the_whole_stack_2d_is_refused(self) -> None:
        self._refuse(matrix.layer2d_axis, _model(simplify=STACK), ["on"], "nothing would stay 3D")

    def test_a_wildcard_in_a_layer_name_is_refused(self) -> None:
        """手册明说不能同时给通配和具体层名，而混着给不报错、只是半生效。"""
        self._refuse(matrix.layer2d_axis, _model(simplify=("LOW2", "*")), ["on"], "no '*'")

    def test_a_layer_listed_twice_is_refused(self) -> None:
        self._refuse(matrix.layer2d_axis, _model(simplify=("LOW2", "LOW2")), ["on"], "listed twice")

    def test_a_mesh_value_that_is_not_a_number_is_refused(self) -> None:
        self._refuse(matrix.layermesh_axis, _model(), ["4x"], "neither 'off' nor a number")

    def test_a_negative_mesh_value_is_refused(self) -> None:
        self._refuse(matrix.layermesh_axis, _model(), ["-1"], "must be positive")

    def test_no_value_at_all_is_refused(self) -> None:
        self._refuse(matrix.layermesh_axis, _model(), [], "no value is selected")

    def test_an_incomplete_model_does_not_block_off(self) -> None:
        """层清单还没填时"只勾 off"必须照样能用 —— 那是界面打开时的常态。"""
        self.assertEqual(
            [v.value for v in matrix.layer2d_axis(LayerModel(), ["off"]).values], ["off"]
        )
        self.assertEqual(
            [v.value for v in matrix.layermesh_axis(LayerModel(), ["off"]).values], ["off"]
        )


class LongAndShortNamesAreOneOption(unittest.TestCase):
    """学默认表时按**选项**剔除，不是按字符串剔除。"""

    def test_a_site_writing_the_long_names_does_not_defeat_the_mesh_axis(self) -> None:
        facts = SiteFacts(
            production_flags={
                "--edgeDist": "0.4",
                "--vertDist": "0.4",
                "--viaMode": "1",
                "--sparamImpedance": "50",
            }
        )
        learned = discover_module.learn_default_flags(facts)
        self.assertEqual(learned, {"--viaMode": "1", "--sparamImpedance": "50"})

    def test_sparam_impedance_is_still_not_eaten(self) -> None:
        """别名展开是集合运算，**不许**退化成前缀匹配（MVP 踩过的那个坑）。"""
        facts = SiteFacts(production_flags={"--sparamImpedance": "50", "--sparam": "CELL_A"})
        self.assertEqual(discover_module.learn_default_flags(facts), {"--sparamImpedance": "50"})


# ==========================================================================
# 层清单是**读**出来的，不是打出来的
# ==========================================================================


def _make_look_like_an_official_dir(root: str) -> None:
    """给临时目录放一份 `gdsout_setup` —— 它是"这是不是官方 design 目录"的判据。

    ⚠️ 不放的代价不是"少测一点"，是**慢 5 秒**：缺了它 `discover_site_facts` 走报错
    那条路，而那条路为了给一句"你是不是指错了目录"会去遍历父目录
    （`suggest_official_dirs`，深度 3），实测约 3.7 万次 lstat。
    """
    body = '\tlibrary\t"MY_LIB"\n\ttopCell\t"CELL"\n\tview\t"layout_em"\n'
    with open(os.path.join(root, "gdsout_setup"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(body)


def _log_for(layers: tuple[str, ...], vias: tuple[str, ...] = ("XV1",)) -> str:
    """按真实 eWave 日志的形状现造一份（`references/probes/speed3d_run_20260828.txt`）。"""
    lines: list[str] = []
    for via in vias:
        lines.append(f"[t][info] begin to eval experssion: {via}=merge(lZt0,1um)")
        lines.append("[t][info] the experssion belongs to via, deal it next time")
    for name in layers:
        lines.append(f"[t][info] begin to eval experssion: {name}=<expr>")
    lines.append("[t][info] basis function are sorted from largest to smallest as follows:")
    for name in layers:
        lines.append(f"[t][info]   {name} (5%)")
    for via in vias:
        lines.append(f"[t][info]   {via} (<5%)")
    lines.append("[t][detail]   total: 1")
    return "\n".join(lines) + "\n"


SYNTHETIC_LOG = _log_for(("LOW1", "LOW2", "LOW3", "LOW4", "TOP1", "TOP2"), ("BURIED", "XV2", "XV1"))
"""⚠️ `BURIED` 是 via 但名字不以 V 开头 —— 靠名字猜 via 的实现会在这里当场红。"""


class TheToolReadsTheLayers(unittest.TestCase):
    """`core.logparse.parse_layer_stack` —— 日志 → 层清单。"""

    def test_conductors_come_back_bottom_up(self) -> None:
        stack = logparse_module.parse_layer_stack(SYNTHETIC_LOG)
        self.assertEqual(stack.conductors, STACK)

    def test_vias_are_separated_by_what_the_log_says_not_by_the_name(self) -> None:
        """判据只能是日志里那句话：把 via 当金属会写进 `--3d`（手册说那是 metal layer），
        把金属当 via 会漏出 `--3d`（那层静默退 2D）。"""
        stack = logparse_module.parse_layer_stack(SYNTHETIC_LOG)
        self.assertEqual(stack.vias, ("BURIED", "XV2", "XV1"))
        self.assertNotIn("BURIED", stack.conductors)

    def test_a_layer_this_design_does_not_use_is_left_out(self) -> None:
        text = SYNTHETIC_LOG.replace(
            "[t][info] begin to eval experssion: TOP2=<expr>",
            "[t][info] begin to eval experssion: TOP2=<expr>\n"
            "[t][info] begin to eval experssion: NEVER_USED=<expr>",
        )
        self.assertNotIn("NEVER_USED", logparse_module.parse_layer_stack(text).conductors)

    def test_the_element_share_comes_along(self) -> None:
        """占比不是装饰 —— 界面拿它回答"该动哪几层"。"""
        self.assertEqual(logparse_module.parse_layer_stack(SYNTHETIC_LOG).shares["TOP1"], "5%")

    def test_a_log_missing_either_half_gives_nothing(self) -> None:
        """**不猜**：只有层表达式就不知道用了哪几层，只有占比段就分不出 via。"""
        head = SYNTHETIC_LOG.split("basis function")[0]
        tail = "basis function" + SYNTHETIC_LOG.split("basis function")[1]
        self.assertTrue(logparse_module.parse_layer_stack(head).is_empty())
        self.assertTrue(logparse_module.parse_layer_stack(tail).is_empty())

    def test_an_unrelated_log_is_not_mistaken_for_one(self) -> None:
        self.assertTrue(
            logparse_module.parse_layer_stack("nothing here\nExecute emsolver done.\n").is_empty()
        )


class ItFindsTheLogInTheOfficialRunDir(unittest.TestCase):
    """`core.discover.find_layer_stack` —— 用户只填官方目录，层清单自己出现。"""

    def _dir(self, **files: str) -> str:
        root = tempfile.mkdtemp()
        self.addCleanup(_rmtree, root)
        _make_look_like_an_official_dir(root)
        for name, body in files.items():
            path = os.path.join(root, name.replace("__", os.sep))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(body)
        return root

    def test_it_reads_the_captured_stdout_log(self) -> None:
        root = self._dir(**{"run_ewave_typical_25_0.log": SYNTHETIC_LOG})
        self.assertEqual(discover_module.find_layer_stack(root).conductors, STACK)

    def test_it_falls_back_to_the_ewave_log_one_level_down(self) -> None:
        root = self._dir(**{"typical_25_0__ewave.log": SYNTHETIC_LOG})
        stack = discover_module.find_layer_stack(root)
        self.assertEqual(stack.conductors, STACK)
        self.assertTrue(stack.source.endswith(logparse_module.EWAVE_LOG_NAME))

    def test_a_dir_with_no_log_says_so_instead_of_raising(self) -> None:
        stack = discover_module.find_layer_stack(self._dir())
        self.assertTrue(stack.is_empty())
        self.assertIn("nothing has been run there yet", stack.note)

    def test_a_missing_dir_says_so_instead_of_raising(self) -> None:
        stack = discover_module.find_layer_stack(os.path.join(tempfile.mkdtemp(), "nope"))
        self.assertTrue(stack.is_empty())
        self.assertTrue(stack.note)


# ==========================================================================
# 桥
# ==========================================================================


class TheBridgeExposesBothRows(unittest.TestCase):
    def _bridge(self, layers: tuple[str, ...] = STACK) -> gui_state.GuiState:
        root = tempfile.mkdtemp()
        self.addCleanup(_rmtree, root)
        _make_look_like_an_official_dir(root)
        with open(
            os.path.join(root, "run_ewave_typical_25_0.log"), "w", encoding="utf-8", newline="\n"
        ) as handle:
            handle.write(_log_for(layers))
        bridge = gui_state.GuiState(batch_root=tempfile.mkdtemp())
        bridge.set_designs([["MY_LIB", "CELL_A", "layout_em"]])
        bridge.set_official_run_dir(root)
        return bridge

    def test_pointing_at_the_official_dir_is_all_it_takes(self) -> None:
        bridge = self._bridge()
        self.assertEqual(bridge.available_layers(), STACK)
        self.assertIn("6 metal layers + 1 vias", bridge.layer_source_text())

    def test_the_two_tick_sets_are_independent(self) -> None:
        """**这就是用户要的分开。** 勾 2D 的层不该动到勾网格的层。"""
        bridge = self._bridge()
        bridge.set_2d_layers(["LOW2", "LOW3"])
        bridge.set_mesh_layers(["TOP1"])
        self.assertEqual(bridge.layer_model().simplify, ("LOW2", "LOW3"))
        self.assertEqual(bridge.layer_model().mesh_layers, ("TOP1",))

    def test_the_click_order_does_not_reach_the_command_line(self) -> None:
        bridge = self._bridge()
        bridge.set_2d_layers(["LOW4", "LOW2", "LOW3"])
        self.assertEqual(bridge.layer_model().simplify, MIDDLE)

    def test_the_preview_shows_everything_that_will_be_sent(self) -> None:
        bridge = self._bridge()
        bridge.set_2d_layers(list(MIDDLE))
        bridge.set_mesh_layers(list(MIDDLE))
        bridge.set_layer_globals(thin="1")
        bridge.set_axis_values("layer2d", ["off", "on"])
        bridge.set_axis_values("layermesh", ["off", "4"])
        self.assertEqual(
            bridge.layer_flags_preview(),
            f"--3d={EXPECTED_KEEP_3D} --edgeDist={EXPECTED_EDGE_DIST} --thinMaxfactor=1",
        )

    def test_the_preview_shows_only_the_row_that_is_on(self) -> None:
        bridge = self._bridge()
        bridge.set_2d_layers(list(MIDDLE))
        bridge.set_mesh_layers(list(MIDDLE))
        bridge.set_axis_values("layer2d", ["on"])
        bridge.set_axis_values("layermesh", ["off"])
        self.assertEqual(bridge.layer_flags_preview(), f"--3d={EXPECTED_KEEP_3D}")

    def test_the_preview_reports_the_problem_instead_of_a_command(self) -> None:
        bridge = gui_state.GuiState()
        bridge.set_designs([["MY_LIB", "CELL_A", "layout_em"]])
        bridge.set_axis_values("layer2d", ["on"])
        self.assertIn("metal stack is empty", bridge.layer_flags_preview())

    def test_the_formula_line_stops_lying_when_a_row_sweeps(self) -> None:
        bridge = self._bridge()
        bridge.set_2d_layers(list(MIDDLE))
        bridge.set_mesh_layers(list(MIDDLE))
        bridge.set_axis_values("layer2d", ["off", "on"])
        bridge.set_axis_values("layermesh", ["off", "4"])
        formula = bridge.formula()
        self.assertIn("2 2d", formula)
        self.assertIn("2 lmesh", formula)
        self.assertIn("= %d runs" % bridge.run_count(), formula)

    def test_a_group_can_override_the_values_but_not_the_layers(self) -> None:
        bridge = self._bridge()
        bridge.set_2d_layers(list(MIDDLE))
        bridge.set_axis_values("layer2d", ["off", "on"])
        name = bridge.add_group("speed")
        bridge.set_group_override("layer2d", ["on"], name)
        self.assertEqual(bridge.group_override("layer2d", name), ("on",))
        self.assertEqual(bridge.layer_model().simplify, MIDDLE)

    def test_both_rows_are_group_overridable(self) -> None:
        self.assertIn("layer2d", gui_state.GROUP_OVERRIDABLE_AXES)
        self.assertIn("layermesh", gui_state.GROUP_OVERRIDABLE_AXES)

    def test_a_hand_added_layer_survives_next_to_the_discovered_ones(self) -> None:
        bridge = self._bridge()
        bridge.set_layer_globals(stack="EXTRA1")
        self.assertEqual(bridge.available_layers()[-1], "EXTRA1")
        self.assertEqual(bridge.extra_layers_text(), "EXTRA1")

    def test_the_extra_box_is_not_a_second_copy_of_the_stack(self) -> None:
        self.assertEqual(self._bridge().extra_layers_text(), "")

    def test_the_stack_is_persisted_for_a_box_without_the_log(self) -> None:
        bridge = self._bridge()
        bridge.set_2d_layers(["LOW2"])
        bridge.set_mesh_layers(["LOW3"])
        saved = bridge.spec_snapshot()
        self.assertEqual(saved.layer_model.stack, STACK)
        self.assertEqual(saved.layer_model.simplify, ("LOW2",))
        self.assertEqual(saved.layer_model.mesh_layers, ("LOW3",))

    def test_without_a_log_it_says_where_to_point(self) -> None:
        bridge = gui_state.GuiState()
        self.assertEqual(bridge.available_layers(), ())
        self.assertIn("official run dir", bridge.layer_source_text())


class SurvivesTheSpecFile(unittest.TestCase):
    def _round_trip(self, spec: BatchSpec) -> BatchSpec:
        return spec_module.parse_spec_mapping(json.loads(spec_module.dump_spec(spec, as_json=True)))

    def test_both_layer_lists_come_back(self) -> None:
        model = _model(simplify=("LOW2",), mesh_layers=("LOW3", "LOW4"), thin_max_factor="1")
        spec = BatchSpec(
            designs=[Design(library="MY_LIB", cell="CELL_A", view="layout_em")],
            axes=[
                matrix.layer2d_axis(model, ["off", "on"]),
                matrix.layermesh_axis(model, ["off", "4"]),
            ],
            layer_model=model,
        )
        back = self._round_trip(spec)
        self.assertEqual(back.layer_model, model)
        self.assertEqual([a.name for a in back.axes], ["layer2d", "layermesh"])

    def test_an_old_spec_without_the_block_still_loads(self) -> None:
        data = {"designs": [{"library": "MY_LIB", "cell": "CELL_A", "view": "layout_em"}]}
        self.assertEqual(spec_module.parse_spec_mapping(data).layer_model, LayerModel())

    def test_an_empty_block_is_not_written_out(self) -> None:
        spec = BatchSpec(designs=[Design(library="MY_LIB", cell="CELL_A", view="layout_em")])
        self.assertNotIn("layer_model", spec_module.spec_to_mapping(spec))

    def test_a_comma_separated_string_is_accepted(self) -> None:
        data = {
            "designs": [{"library": "MY_LIB", "cell": "CELL_A", "view": "layout_em"}],
            "layer_model": {"stack": "LOW1, LOW2 LOW3", "mesh_layers": "LOW2"},
        }
        parsed = spec_module.parse_spec_mapping(data).layer_model
        self.assertEqual(parsed.stack, ("LOW1", "LOW2", "LOW3"))
        self.assertEqual(parsed.mesh_layers, ("LOW2",))

    def test_an_unknown_key_under_the_block_is_refused(self) -> None:
        data = {
            "designs": [{"library": "MY_LIB", "cell": "CELL_A", "view": "layout_em"}],
            "layer_model": {"stak": ["LOW1"]},
        }
        with self.assertRaises(SpecError):
            spec_module.parse_spec_mapping(data)


class TheOldSingleAxisIsMigrated(unittest.TestCase):
    """2026-08-31 上午那一版存的 session 里是一根 `simplify2d` —— 不认它就静默丢设定。"""

    def test_off_and_a_number_become_two_rows(self) -> None:
        migrated = gui_state._migrate_legacy_simplify2d(
            {"layer2d": (), "layermesh": (), "simplify2d": ("off", "4")}
        )
        self.assertEqual(migrated["layer2d"], ("off", "on"))
        self.assertEqual(migrated["layermesh"], ("off", "4"))

    def test_the_migrated_command_is_the_same_as_before(self) -> None:
        """迁过来的批次跑出来的命令与老版本逐字相同，只是现在两件事可以分开调了。"""
        migrated = gui_state._migrate_legacy_simplify2d(
            {"layer2d": (), "layermesh": (), "simplify2d": ("4",)}
        )
        model = _model()
        flags = dict(matrix.layer2d_flags_for(model, migrated["layer2d"][0]))
        flags.update(matrix.layermesh_flags_for(model, migrated["layermesh"][0]))
        self.assertEqual(flags, {"--3d": EXPECTED_KEEP_3D, "--edgeDist": EXPECTED_EDGE_DIST})

    def test_a_choice_made_in_the_new_ui_is_not_overwritten(self) -> None:
        migrated = gui_state._migrate_legacy_simplify2d(
            {"layer2d": ("off",), "layermesh": (), "simplify2d": ("4",)}
        )
        self.assertEqual(migrated["layer2d"], ("off",))

    def test_the_legacy_name_does_not_linger(self) -> None:
        migrated = gui_state._migrate_legacy_simplify2d(
            {"layer2d": (), "layermesh": (), "simplify2d": ("4",)}
        )
        self.assertNotIn("simplify2d", migrated)


# ==========================================================================
# 层清单按 design 走（2026-08-31 实测出来的两个缺陷的回归）
# ==========================================================================


class EachDesignGetsItsOwnLayers(unittest.TestCase):
    """🚨 共用一份层清单会让第二个 design 独有的层被静默降 2D ——
    这个功能要消灭的那类错误，在多 design 这一层原样重造了一遍。"""

    A_LAYERS = ("LOW1", "LOW2", "TOP1")
    B_LAYERS = ("LOW1", "LOW2", "TOP1", "EXTRA_TOP")

    def _offdir(self, layers: tuple[str, ...] | None) -> str:
        root = tempfile.mkdtemp()
        self.addCleanup(_rmtree, root)
        _make_look_like_an_official_dir(root)
        if layers is not None:
            with open(
                os.path.join(root, "run_ewave_typical_25_0.log"),
                "w",
                encoding="utf-8",
                newline="\n",
            ) as handle:
                handle.write(_log_for(layers))
        return root

    def _bridge(self, a_layers=A_LAYERS, b_layers=B_LAYERS) -> gui_state.GuiState:
        spec = {
            "designs": [
                {
                    "library": "MY_LIB",
                    "cell": "CELL_A",
                    "view": "layout_em",
                    "official_run_dir": self._offdir(a_layers),
                },
                {
                    "library": "MY_LIB",
                    "cell": "CELL_B",
                    "view": "layout_em",
                    "official_run_dir": self._offdir(b_layers),
                },
            ]
        }
        root = tempfile.mkdtemp()
        self.addCleanup(_rmtree, root)
        path = os.path.join(root, "spec.json")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(spec, handle)
        bridge = gui_state.GuiState(batch_root=os.path.join(root, "batches"))
        bridge.load_spec(path)
        return bridge

    def _keep3d(self, bridge: gui_state.GuiState, cell: str) -> str:
        design = next(d for d in bridge.designs() if d.cell == cell)
        model = bridge.effective_layer_model_for(design)
        return matrix.layer2d_flags_for(model, "on")["--3d"]

    def test_the_checkbox_list_is_the_union_of_every_design(self) -> None:
        self.assertEqual(self._bridge().available_layers(), self.B_LAYERS)

    def test_a_layer_only_the_second_design_has_is_not_dropped(self) -> None:
        bridge = self._bridge()
        bridge.set_2d_layers(["LOW2"])
        self.assertEqual(self._keep3d(bridge, "CELL_A"), "LOW1,TOP1")
        self.assertEqual(self._keep3d(bridge, "CELL_B"), "LOW1,TOP1,EXTRA_TOP")

    def test_a_design_does_not_get_layers_it_does_not_have(self) -> None:
        bridge = self._bridge()
        bridge.set_2d_layers(["LOW2"])
        self.assertNotIn("EXTRA_TOP", self._keep3d(bridge, "CELL_A"))

    def test_ticking_a_layer_this_design_lacks_is_not_an_error(self) -> None:
        bridge = self._bridge()
        bridge.set_2d_layers(["LOW2", "EXTRA_TOP"])
        self.assertEqual(self._keep3d(bridge, "CELL_A"), "LOW1,TOP1")

    def test_the_mesh_row_is_narrowed_per_design_too(self) -> None:
        bridge = self._bridge()
        bridge.set_mesh_layers(["LOW2", "EXTRA_TOP"])
        design_a = next(d for d in bridge.designs() if d.cell == "CELL_A")
        design_b = next(d for d in bridge.designs() if d.cell == "CELL_B")
        self.assertEqual(
            matrix.layermesh_flags_for(bridge.effective_layer_model_for(design_a), "4"),
            {"--edgeDist": "LOW2,4"},
        )
        self.assertEqual(
            matrix.layermesh_flags_for(bridge.effective_layer_model_for(design_b), "4"),
            {"--edgeDist": "LOW2,4 EXTRA_TOP,4"},
        )

    def test_a_design_without_a_log_falls_back_to_the_batch_union(self) -> None:
        bridge = self._bridge(b_layers=None)
        bridge.set_2d_layers(["LOW2"])
        self.assertEqual(self._keep3d(bridge, "CELL_B"), "LOW1,TOP1")

    def test_the_per_design_axes_reach_the_plan(self) -> None:
        bridge = self._bridge()
        bridge.set_2d_layers(["LOW2"])
        bridge.set_axis_values("layer2d", ["on"])
        bridge.set_axis_values("temperature", ["25.0"])
        bridge.plan()
        seen = {}
        for key, ctx in bridge._contexts.items():
            axis = next(a for a in ctx.axes if a.name == "layer2d")
            seen[key] = axis.values[0].flags["--3d"]
        self.assertEqual(seen["MY_LIB_CELL_A_layout_em"], "LOW1,TOP1")
        self.assertEqual(seen["MY_LIB_CELL_B_layout_em"], "LOW1,TOP1,EXTRA_TOP")

    def test_an_off_only_batch_is_left_alone(self) -> None:
        """整批都是 off ⇒ 这根轴对命令行没有贡献，不按 design 重算（也就不去读日志）。"""
        bridge = self._bridge()
        bridge.set_axis_values("layer2d", ["off"])
        bridge.set_axis_values("temperature", ["25.0"])
        bridge.plan()
        for ctx in bridge._contexts.values():
            axis = next(a for a in ctx.axes if a.name == "layer2d")
            self.assertEqual(axis.values[0].flags, {"--3d": False})

    def test_a_layer_no_design_has_shows_up_as_a_run_error(self) -> None:
        """造不出轴时走**按 run 显示**那条通道 —— `plan()` 的承诺是"矩阵照常画得出来"。"""
        bridge = self._bridge()
        bridge.set_layer_globals(stack="GHOST")
        bridge.set_2d_layers(["GHOST"])
        bridge.set_axis_values("layer2d", ["on"])
        bridge.set_axis_values("temperature", ["25.0"])
        bridge.plan()
        self.assertTrue(bridge.runs(), "矩阵必须还在")
        self.assertEqual(len(bridge._plan_errors), len(bridge.runs()))

    def test_a_historical_batch_keeps_its_frozen_axes(self) -> None:
        """打开历史批次时**不许**拿今天的日志重算（"resume 之后 --3d 悄悄换了"）。"""
        bridge = self._bridge()
        bridge.set_2d_layers(["LOW2"])
        bridge.set_axis_values("layer2d", ["on"])
        bridge.set_axis_values("temperature", ["25.0"])
        bridge.plan()
        state = bridge._state
        assert state is not None
        frozen = {a.name: a for a in state.axes}["layer2d"]
        for ctx in bridge._build_contexts(state, learn=False).values():
            axis = next(a for a in ctx.axes if a.name == "layer2d")
            self.assertEqual(axis.values[0].flags, frozen.values[0].flags)


class SwitchingTheOfficialDirDropsStaleLayers(unittest.TestCase):
    """🚨 旧层名越堆越多的回归。存过一次 session 之后换 design，
    老层名会一直留在勾选框里、也进 `--3d`，而"eWave 会忽略不认识的层名"我们没有证据。"""

    def _offdir(self, layers: tuple[str, ...] | None) -> str:
        root = tempfile.mkdtemp()
        self.addCleanup(_rmtree, root)
        _make_look_like_an_official_dir(root)
        if layers is not None:
            with open(
                os.path.join(root, "run_ewave_typical_25_0.log"),
                "w",
                encoding="utf-8",
                newline="\n",
            ) as handle:
                handle.write(_log_for(layers))
        return root

    def _reopened(self, first: str) -> gui_state.GuiState:
        bridge = gui_state.GuiState()
        bridge.set_designs([["MY_LIB", "CELL_A", "layout_em"]])
        bridge.set_official_run_dir(first)
        saved = bridge.spec_snapshot()
        again = gui_state.GuiState()
        again.set_designs([["MY_LIB", "CELL_A", "layout_em"]])
        again._apply_spec(saved, take_identity=False)
        return again

    def test_the_previous_designs_layers_do_not_linger(self) -> None:
        bridge = self._reopened(self._offdir(("OLD1", "OLD2", "OLDTOP")))
        bridge.set_official_run_dir(self._offdir(("NEW1", "NEW2", "NEWTOP")))
        self.assertEqual(bridge.available_layers(), ("NEW1", "NEW2", "NEWTOP"))

    def test_a_dir_without_a_log_keeps_what_we_already_had(self) -> None:
        """新目录读不到层就留着旧的 —— 那时它是唯一的来源（宁可多，不可无）。"""
        bridge = self._reopened(self._offdir(("OLD1", "OLD2", "OLDTOP")))
        bridge.set_official_run_dir(self._offdir(None))
        self.assertEqual(bridge.available_layers(), ("OLD1", "OLD2", "OLDTOP"))

    def test_reopening_on_the_same_dir_keeps_everything(self) -> None:
        offdir = self._offdir(("OLD1", "OLD2", "OLDTOP"))
        bridge = self._reopened(offdir)
        bridge.set_official_run_dir(offdir)
        self.assertEqual(bridge.available_layers(), ("OLD1", "OLD2", "OLDTOP"))


class NoSiteIdentifiersInTheSource(unittest.TestCase):
    """层名是 PDK 叠层坐标（硬约束 1b）—— 源码里一个都不许有。"""

    def test_the_catalog_axes_carry_no_layer_names(self) -> None:
        catalog = matrix.builtin_axis_catalog()
        for name in (matrix.AXIS_LAYER2D, matrix.AXIS_LAYERMESH):
            axis = catalog[name]
            self.assertEqual([v.value for v in axis.values], [matrix.LAYER_AXIS_OFF])
            for value in axis.values:
                for flag_value in value.flags.values():
                    self.assertIs(flag_value, False)

    def test_a_fresh_bridge_has_no_layers(self) -> None:
        model = gui_state.GuiState().layer_model()
        self.assertEqual(model.stack, ())
        self.assertEqual(model.simplify, ())
        self.assertEqual(model.mesh_layers, ())


# ==========================================================================
# 真界面
# ==========================================================================

_SHARED_ROOT: object | None = None
"""**整个模块共用一个** Tk 根窗口 —— 一个进程里反复 `Tk()` 在某些机器上会失败。"""


def _tk_or_skip(test: unittest.TestCase) -> object:
    try:
        import tkinter as tk
    except ImportError as exc:  # pragma: no cover - 本机装了 tkinter
        test.skipTest(f"平台跳过：这台机器没装 tkinter（{exc}）—— CLI 不受影响")
    global _SHARED_ROOT
    if _SHARED_ROOT is None:
        try:
            _SHARED_ROOT = tk.Tk()
        except tk.TclError as exc:  # pragma: no cover - 本机有显示
            test.skipTest(f"平台跳过：这台机器开不了显示（{exc}）—— CLI 不受影响")
        _SHARED_ROOT.withdraw()  # type: ignore[union-attr]
    root = _SHARED_ROOT

    def _destroy_children() -> None:
        for child in list(root.winfo_children()):  # type: ignore[union-attr]
            child.destroy()

    test.addCleanup(_destroy_children)
    return root


class TheTwoRowsAreReallyTwoRows(unittest.TestCase):
    """界面上确实是两块，而且它们的勾选互不干扰。

    另外守着一个实拍过的 bug：那两排勾选框是**动态建**的（层名来自日志），
    而 `recompute()` 先 push 后 sync ⇒ 第一拍控件还是空的，
    「一个控件都没有」和「用户全取消勾选了」在回写那一步长得一模一样。
    """

    def _app(self):
        root = _tk_or_skip(self)
        from gui.frames import split

        offdir = tempfile.mkdtemp()
        self.addCleanup(_rmtree, offdir)
        _make_look_like_an_official_dir(offdir)
        with open(
            os.path.join(offdir, "run_ewave_typical_25_0.log"), "w", encoding="utf-8", newline="\n"
        ) as handle:
            handle.write(_log_for(STACK))

        bridge = gui_state.GuiState(batch_root=tempfile.mkdtemp(), batch_name="layers")
        bridge.add_design("MY_LIB", "CELL_A", "layout_em")
        bridge.set_official_run_dir(offdir)
        bridge.set_2d_layers(["LOW2", "LOW3", "LOW4"])
        bridge.set_mesh_layers(["LOW2"])
        bridge.set_axis_values("layer2d", ["off", "on"])
        bridge.set_axis_values("layermesh", ["off", "4"])

        frame = split.build_frame(root, bridge)
        frame.pack()
        app = frame._ewb_app
        app.recompute()
        return bridge, app

    def test_there_are_two_layer_boxes(self) -> None:
        _bridge, app = self._app()
        self.assertEqual(sorted(app.layer_boxes), ["layer2d", "layermesh"])
        self.assertEqual(sorted(app.layer_vars), ["layer2d", "layermesh"])

    def test_each_box_shows_its_own_ticks(self) -> None:
        _bridge, app = self._app()
        self.assertEqual(
            {n for n, v in app.layer_vars["layer2d"].items() if v.get()},
            {"LOW2", "LOW3", "LOW4"},
        )
        self.assertEqual(
            {n for n, v in app.layer_vars["layermesh"].items() if v.get()}, {"LOW2"}
        )

    def test_the_first_refresh_keeps_what_was_loaded(self) -> None:
        bridge, _app = self._app()
        self.assertEqual(bridge.layer_model().simplify, ("LOW2", "LOW3", "LOW4"))
        self.assertEqual(bridge.layer_model().mesh_layers, ("LOW2",))

    def test_unticking_one_row_leaves_the_other_alone(self) -> None:
        """**分开**这件事在界面上的样子。"""
        bridge, app = self._app()
        app.layer_vars["layer2d"]["LOW4"].set(False)
        app.recompute()
        self.assertEqual(bridge.layer_model().simplify, ("LOW2", "LOW3"))
        self.assertEqual(bridge.layer_model().mesh_layers, ("LOW2",))

    def test_no_layer_name_is_ever_typed(self) -> None:
        _bridge, app = self._app()
        self.assertEqual(app.s2d_extra.get(), "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
