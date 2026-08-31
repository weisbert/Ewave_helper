"""`simplify2d` 轴 —— 把指定的几层金属降成 2D 换速度。

## 这根轴要守住的一句话

eWave 的 `--3d` 是**保持 3D 的白名单**：没列进去的层**静默**退成 2D
（`references/probes/ewave_probe_2025.09.sp1.txt` 的 `--3d` 条目）。
而用户想的是反过来的一句话 ——「把这几层降成 2D 省时间」。

用户 2026-08-28 手写白名单时漏了一层，那层被无声无息地降成 2D，而 run 照样跑完、
数字还挺像 —— 是他自己事后一层一层数元素数才发现的。所以这里的口径是：
**用户只说想降哪几层，白名单由工具算**（`stack - simplify`）。
于是"漏写"的后果从「那层被降级」变成「那层留在 3D」= 不改变。

## 命令行的形状（红区 2026-08-28 实跑过的那一条）

    … -e 0.5 -d 0.5 … --3d=<保持 3D 的层> --edgeDist='<层>,4 <层>,4 …' --thinMaxfactor=1
       ^^^^^ 全局（mesh 轴，短名）        ^^^^^^^^^^ 逐层（simplify2d 轴，长名）

同一个选项的两种写法同时出现是 eWave **文档化**的用法
（help 原文：`--edgeDist=2 --edgeDist=M1,0.8` = 「M1 用 0.8，其余用全局 2」）。
它成立的全部依据是「mesh 轴写短名、simplify2d 轴写长名」——
`test_mesh_axis_still_writes_the_short_name` 是这一条的锁。

## 站点标识符零出现

层名一律是 `LOW1` / `TOP2` 这种编出来的占位符。真实的金属层名是 PDK 叠层坐标
（CLAUDE.md 硬约束 1b），源码和测试里都不许有 —— 也正因如此，
`builtin_axis_catalog()` 里那根轴只有 `off` 一个取值，别的取值必须现造。
`4` / `1` / `0.5` 是 eWave 的工具语义（µm / nm），不是站点身份。
"""

from __future__ import annotations

import json
import os
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

SIMPLIFY = ("LOW2", "LOW3", "LOW4")
"""其中想降成 2D 的那几层 —— 中间三层，上下两头都留着。

上下都留是有意的：补集若只会"砍掉末尾"，`LOW1` 留不留就试不出来，
而 `LOW1` 正是用户 2026-08-28 差点漏掉的那一类（底层，元素数极少、
对速度毫无影响，但"没打算改的东西被改了"）。
"""

EXPECTED_KEEP_3D = "LOW1,TOP1,TOP2"
"""`--3d` 的期望值：叠层减去那三层，**按叠层的顺序**。"""

EXPECTED_EDGE_DIST = "LOW2,4 LOW3,4 LOW4,4"
"""`--edgeDist` 的期望值：逐层写 `层,值`，空格分隔，同样按叠层的顺序。
形状抄自 `references/probes/speed3d_run_20260828.txt` 里那条真跑过的命令。"""


def _model(**over: object) -> LayerModel:
    fields: dict = {"stack": STACK, "simplify": SIMPLIFY, "thin_max_factor": "1"}
    fields.update(over)
    return LayerModel(**fields)  # type: ignore[arg-type]


def _argv(values: list[str], *, mesh: str = "0.5", model: LayerModel | None = None) -> dict[str, list[str]]:
    """把一组取值展开成 run，返回 `{run_id: argv}`。**走的是真正的那条拼命令路径。**"""
    design = Design(library="MY_LIB", cell="CELL_A", view="layout_em")
    catalog = matrix.builtin_axis_catalog()
    axes = [
        matrix.axis_with_values(catalog["temperature"], ["25.0"]),
        matrix.axis_with_values(catalog["mesh"], [mesh]),
    ]
    if values:
        axes.append(matrix.simplify2d_axis(model if model is not None else _model(), values))
    facts = SiteFacts(ewave_bin="/fake/ewave", key="FAKEKEY", ptxt="/fake/typical.ptxt")
    ctx = PlanContext(
        design=design, axes=axes, facts=facts, options=BatchOptions(), batch_dir="/fake/batch"
    )
    out: dict[str, list[str]] = {}
    for run in matrix.expand_runs([design], axes):
        placed = replace(run, work_dir="/fake/batch/runs/" + (run.axes_slug or "base"))
        out[run.run_id] = list(cmd_module.build_command_plan(placed, ctx).argv)
    return out


def _flag(argv: list[str], name: str) -> str | None:
    """argv 里 `--name=value` 的 value。没有这个 flag → None。"""
    prefix = name + "="
    for token in argv:
        if token.startswith(prefix):
            return token[len(prefix) :]
    return None


# ==========================================================================


class WhiteListIsComputed(unittest.TestCase):
    """`--3d` 是算出来的补集，不是用户输入。"""

    def test_keep_3d_is_the_stack_minus_the_layers_the_user_named(self) -> None:
        flags = matrix.simplify2d_flags_for(_model(), "4")
        self.assertEqual(flags["--3d"], EXPECTED_KEEP_3D)
        self.assertEqual(flags["--edgeDist"], EXPECTED_EDGE_DIST)
        self.assertEqual(flags["--thinMaxfactor"], "1")

    def test_a_layer_the_user_did_not_name_stays_3d(self) -> None:
        """漏写的后果必须是"不改变"。

        这条就是用户 2026-08-28 踩的那个坑的回归：那时白名单是手写的，
        漏掉的层被静默降成 2D。现在漏掉的层落在补集里 ⇒ 留在 3D。
        """
        flags = matrix.simplify2d_flags_for(_model(simplify=("LOW3",)), "4")
        for untouched in ("LOW1", "LOW2", "LOW4", "TOP1", "TOP2"):
            self.assertIn(untouched, flags["--3d"].split(","))
        self.assertEqual(flags["--edgeDist"], "LOW3,4")

    def test_the_order_follows_the_stack_not_the_typing(self) -> None:
        """用户在框里怎么敲不影响命令行 —— 两次跑的 cmd.sh 要能逐字比。"""
        shuffled = _model(simplify=("LOW4", "LOW2", "LOW3"))
        self.assertEqual(
            matrix.simplify2d_flags_for(shuffled, "4")["--edgeDist"], EXPECTED_EDGE_DIST
        )

    def test_no_simplified_layer_leaks_into_the_keep_3d_list(self) -> None:
        keep = matrix.simplify2d_flags_for(_model(), "4")["--3d"].split(",")
        for layer in SIMPLIFY:
            self.assertNotIn(layer, keep)

    def test_an_empty_thin_max_factor_means_the_flag_is_not_given(self) -> None:
        flags = matrix.simplify2d_flags_for(_model(thin_max_factor=""), "4")
        self.assertIs(flags["--thinMaxfactor"], False)


class OffIsReallyOff(unittest.TestCase):
    """`off` 那一格的命令行必须与「根本没有这根轴」逐字相同 —— 否则基线不是基线。"""

    def test_off_adds_nothing_to_the_command(self) -> None:
        without = list(_argv([]).values())[0]
        with_off = list(_argv(["off"]).values())[0]
        self.assertEqual(with_off, without)

    def test_off_cancels_a_learned_default_instead_of_merely_omitting_it(self) -> None:
        """三个 flag 给的是 `False`（显式缺席）而不是"不写"。

        "不写"盖不掉默认表里学来的值 —— 于是基线那一格会带上一个来路不明的
        `--thinMaxfactor`，而目录名说它是基线。
        """
        flags = matrix.simplify2d_flags_for(_model(), "off")
        self.assertEqual(
            flags, {"--3d": False, "--edgeDist": False, "--thinMaxfactor": False}
        )


class GlobalAndPerLayerCoexist(unittest.TestCase):
    """mesh 的全局 `-e` 和 simplify2d 的逐层 `--edgeDist` **必须同时在命令行上**。"""

    def test_both_survive_into_the_argv(self) -> None:
        argv = _argv(["off", "4"], mesh="0.5")["MY_LIB_CELL_A_layout_em/2d-4/25_0"]
        self.assertIn("-e", argv)
        self.assertEqual(argv[argv.index("-e") + 1], "0.5")
        self.assertEqual(_flag(argv, "--edgeDist"), EXPECTED_EDGE_DIST)
        self.assertEqual(_flag(argv, "--3d"), EXPECTED_KEEP_3D)
        self.assertEqual(_flag(argv, "--thinMaxfactor"), "1")

    def test_the_per_layer_form_comes_after_the_global_one(self) -> None:
        """手册那个例子的顺序（`--edgeDist=2 --edgeDist=M1,0.8`）：先全局，后逐层。"""
        argv = _argv(["off", "4"])["MY_LIB_CELL_A_layout_em/2d-4/25_0"]
        self.assertLess(
            argv.index("-e"),
            next(i for i, t in enumerate(argv) if t.startswith("--edgeDist=")),
        )

    def test_mesh_axis_still_writes_the_short_name(self) -> None:
        """🚨 **整个"全局 + 逐层"能成立的全部依据。**

        mesh 轴写 `-e`、simplify2d 轴写 `--edgeDist` —— 两个不同的 dict 键，
        于是两条都进命令行。谁把 mesh 轴改成长名，两根轴就撞同一个键，
        后合并的那个把前一个整个吃掉：要么全局网格丢了、要么逐层覆盖丢了，
        而两种都是"目录名说一套、命令行做另一套"，还都跑得完。
        """
        mesh = matrix.builtin_axis_catalog()["mesh"]
        self.assertIn("-e", mesh.flags)
        self.assertNotIn("--edgeDist", mesh.flags)
        self.assertIn("--edgeDist", matrix.SIMPLIFY2D_FLAGS)
        self.assertNotIn("-e", matrix.SIMPLIFY2D_FLAGS)
        self.assertEqual(set(mesh.flags) & set(matrix.SIMPLIFY2D_FLAGS), set())

    def test_the_two_values_land_in_different_directories(self) -> None:
        """基线和降级版共用一个目录 = 静默覆盖，正是本工具要消灭的东西。"""
        runs = _argv(["off", "4"])
        self.assertEqual(
            sorted(runs),
            [
                "MY_LIB_CELL_A_layout_em/2d-4/25_0",
                "MY_LIB_CELL_A_layout_em/2d-off/25_0",
            ],
        )


class RefusesInsteadOfGuessing(unittest.TestCase):
    """每一条不拦的代价都是"跑得完、数字也像"。"""

    def _refuse(self, model: LayerModel, values: list[str], needle: str) -> None:
        with self.assertRaises(SpecError) as caught:
            matrix.simplify2d_axis(model, values)
        self.assertIn(needle, str(caught.exception))

    def test_an_empty_stack_is_refused(self) -> None:
        self._refuse(_model(stack=()), ["4"], "metal stack is empty")

    def test_naming_no_layer_to_simplify_is_refused(self) -> None:
        self._refuse(_model(simplify=()), ["4"], "no layer is marked for 2D")

    def test_a_layer_outside_the_stack_is_refused(self) -> None:
        """eWave 对不认识的层名**不报错**，就是不生效 —— 拼错了在命令行上看不出来。"""
        self._refuse(_model(simplify=("LOW2", "TYPO")), ["4"], "not in the metal stack")

    def test_simplifying_the_whole_stack_is_refused(self) -> None:
        self._refuse(_model(simplify=STACK), ["4"], "nothing would stay 3D")

    def test_a_wildcard_in_a_layer_name_is_refused(self) -> None:
        """手册明说不能同时给通配和具体层名，而混着给不报错、只是半生效。"""
        self._refuse(_model(simplify=("LOW2", "*")), ["4"], "no '*'")

    def test_a_layer_listed_twice_is_refused(self) -> None:
        self._refuse(_model(simplify=("LOW2", "LOW2")), ["4"], "listed twice")

    def test_a_value_that_is_not_a_number_is_refused(self) -> None:
        self._refuse(_model(), ["4x"], "neither 'off' nor a number")

    def test_a_negative_value_is_refused(self) -> None:
        self._refuse(_model(), ["-1"], "must be positive")

    def test_no_value_at_all_is_refused(self) -> None:
        self._refuse(_model(), [], "no value is selected")

    def test_an_incomplete_model_does_not_block_the_off_value(self) -> None:
        """层清单还没填时"只勾 off"必须照样能用 —— 那是界面打开时的常态。"""
        axis = matrix.simplify2d_axis(LayerModel(), ["off"])
        self.assertEqual([v.value for v in axis.values], ["off"])


class LongAndShortNamesAreOneOption(unittest.TestCase):
    """学默认表时按**选项**剔除，不是按字符串剔除。"""

    def test_a_site_writing_the_long_names_does_not_defeat_the_mesh_axis(self) -> None:
        """本站点官方脚本恰好写短名（`-e 0.4`），所以这个洞一直没发作。

        换一个写长名的站点：`--edgeDist=0.4` 学进默认表，mesh 轴写的是 `-e`，
        两个键不撞 ⇒ **两条都下发**，而 eWave 认哪一条是没定义的。
        目录名说 mesh 0.5、实际可能跑 0.4，而且跑得完。
        """
        facts = SiteFacts(
            production_flags={
                "--edgeDist": "0.4",
                "--vertDist": "0.4",
                "--viaMode": "1",
                "--sparamImpedance": "50",
            }
        )
        learned = discover_module.learn_default_flags(facts)
        self.assertNotIn("--edgeDist", learned)
        self.assertNotIn("--vertDist", learned)
        self.assertEqual(learned, {"--viaMode": "1", "--sparamImpedance": "50"})

    def test_sparam_impedance_is_still_not_eaten(self) -> None:
        """别名展开是集合运算，**不许**退化成前缀匹配（MVP 踩过的那个坑）。"""
        facts = SiteFacts(production_flags={"--sparamImpedance": "50", "--sparam": "CELL_A"})
        self.assertEqual(discover_module.learn_default_flags(facts), {"--sparamImpedance": "50"})


class SurvivesTheSpecFile(unittest.TestCase):
    """层清单存得下、读得回 —— 否则"下次启动不用再 load"这句话是假的。"""

    def _round_trip(self, spec: BatchSpec) -> BatchSpec:
        return spec_module.parse_spec_mapping(json.loads(spec_module.dump_spec(spec, as_json=True)))

    def test_the_layer_lists_come_back_unchanged(self) -> None:
        model = _model()
        spec = BatchSpec(
            designs=[Design(library="MY_LIB", cell="CELL_A", view="layout_em")],
            axes=[matrix.simplify2d_axis(model, ["off", "4"])],
            layer_model=model,
        )
        back = self._round_trip(spec)
        self.assertEqual(back.layer_model, model)
        self.assertEqual([v.value for v in back.axes[0].values], ["off", "4"])
        self.assertEqual(back.axes[0].values[1].flags["--3d"], EXPECTED_KEEP_3D)

    def test_an_old_spec_without_the_block_still_loads(self) -> None:
        """老 spec / 老 session 没有 `layer_model:` ⇒ 空的 = 功能没开。"""
        data = {"designs": [{"library": "MY_LIB", "cell": "CELL_A", "view": "layout_em"}]}
        self.assertEqual(spec_module.parse_spec_mapping(data).layer_model, LayerModel())

    def test_an_empty_block_is_not_written_out(self) -> None:
        spec = BatchSpec(designs=[Design(library="MY_LIB", cell="CELL_A", view="layout_em")])
        self.assertNotIn("layer_model", spec_module.spec_to_mapping(spec))

    def test_a_comma_separated_string_is_accepted(self) -> None:
        """手写 YAML 时写成一行是常见的，别为难人。"""
        data = {
            "designs": [{"library": "MY_LIB", "cell": "CELL_A", "view": "layout_em"}],
            "layer_model": {"stack": "LOW1, LOW2 LOW3", "simplify": "LOW2"},
        }
        parsed = spec_module.parse_spec_mapping(data).layer_model
        self.assertEqual(parsed.stack, ("LOW1", "LOW2", "LOW3"))
        self.assertEqual(parsed.simplify, ("LOW2",))

    def test_an_unknown_key_under_the_block_is_refused(self) -> None:
        data = {
            "designs": [{"library": "MY_LIB", "cell": "CELL_A", "view": "layout_em"}],
            "layer_model": {"stak": ["LOW1"]},
        }
        with self.assertRaises(SpecError):
            spec_module.parse_spec_mapping(data)


class TheBridgeExposesIt(unittest.TestCase):
    """界面那一侧（`gui.state.GuiState`，不碰 tkinter）。"""

    def _bridge(self) -> gui_state.GuiState:
        bridge = gui_state.GuiState()
        bridge.set_designs([["MY_LIB", "CELL_A", "layout_em"]])
        bridge.set_layer_model(
            stack=",".join(STACK), simplify=" ".join(SIMPLIFY), thin="1"
        )
        return bridge

    def test_the_default_selection_is_off_so_nothing_changes(self) -> None:
        bridge = gui_state.GuiState()
        self.assertEqual(bridge.axis_selection()["simplify2d"], ("off",))
        self.assertEqual(bridge.simplify2d_preview(), "")

    def test_the_text_boxes_round_trip(self) -> None:
        bridge = self._bridge()
        self.assertEqual(
            bridge.layer_model_text(), ("LOW1,LOW2,LOW3,LOW4,TOP1,TOP2", "LOW2,LOW3,LOW4", "1")
        )

    def test_the_preview_shows_what_the_tool_computed(self) -> None:
        """`--3d` 是算出来的 ⇒ 必须摆到界面上让人核对（算错了命令行上看不出来）。"""
        bridge = self._bridge()
        bridge.set_axis_values("simplify2d", ["off", "4"])
        self.assertEqual(
            bridge.simplify2d_preview(),
            f"--3d={EXPECTED_KEEP_3D} --edgeDist={EXPECTED_EDGE_DIST} --thinMaxfactor=1",
        )

    def test_the_preview_reports_the_problem_instead_of_a_command(self) -> None:
        bridge = gui_state.GuiState()
        bridge.set_designs([["MY_LIB", "CELL_A", "layout_em"]])
        bridge.set_axis_values("simplify2d", ["4"])
        self.assertIn("metal stack is empty", bridge.simplify2d_preview())

    def test_the_axis_reaches_the_matrix(self) -> None:
        bridge = self._bridge()
        bridge.set_axis_values("simplify2d", ["off", "4"])
        axis = next(a for a in bridge.axes() if a.name == "simplify2d")
        self.assertEqual([v.value for v in axis.values], ["off", "4"])
        self.assertEqual(axis.values[1].flags["--edgeDist"], EXPECTED_EDGE_DIST)

    def test_the_formula_line_stops_lying_when_the_axis_sweeps(self) -> None:
        """`1 corner x 3 temp x 2 mode = 12` 的左边连乘是 6 —— 等号两边对不上就是界面在说谎。"""
        bridge = self._bridge()
        bridge.set_axis_values("simplify2d", ["off", "4"])
        self.assertIn("2 2d", bridge.formula())
        self.assertIn("= %d runs" % bridge.run_count(), bridge.formula())

    def test_a_group_can_override_the_value_but_the_layers_stay_batch_wide(self) -> None:
        """组能自己定"降到多粗"，不能自己定"降哪几层"（层清单不进 slug）。"""
        bridge = self._bridge()
        bridge.set_axis_values("simplify2d", ["off", "4"])
        name = bridge.add_group("speed")
        bridge.set_group_override("simplify2d", ["4"], name)
        self.assertEqual(bridge.group_override("simplify2d", name), ("4",))
        self.assertEqual(bridge.layer_model().simplify, SIMPLIFY)

    def test_it_is_in_the_group_overridable_list(self) -> None:
        self.assertIn("simplify2d", gui_state.GROUP_OVERRIDABLE_AXES)


class NoSiteIdentifiersInTheSource(unittest.TestCase):
    """层名是 PDK 叠层坐标（硬约束 1b）—— 源码里一个都不许有。"""

    def test_the_catalog_axis_carries_no_layer_names(self) -> None:
        axis = matrix.builtin_axis_catalog()[matrix.AXIS_SIMPLIFY2D]
        self.assertEqual([v.value for v in axis.values], [matrix.SIMPLIFY2D_OFF])
        for value in axis.values:
            for flag_value in value.flags.values():
                self.assertIs(flag_value, False)

    def test_a_fresh_bridge_has_no_layers(self) -> None:
        model = gui_state.GuiState().layer_model()
        self.assertEqual(model.stack, ())
        self.assertEqual(model.simplify, ())

# ==========================================================================
# 层清单是**读**出来的，不是打出来的（用户 2026-08-31：
# 「你不能指望用户自己填写 --3d 这样的指令，应该是 GUI 体现出来」）
# ==========================================================================

SYNTHETIC_LOG = """\
[2026-08-31 09:43:12][info] begin to eval experssion: BURIED=count(lAt0)
[2026-08-31 09:43:12][info] the experssion belongs to via, deal it next time
[2026-08-31 09:43:12][info] begin to eval experssion: XV2=merge(lBt0,0.09um)
[2026-08-31 09:43:12][info] the experssion belongs to via, deal it next time
[2026-08-31 09:43:12][info] begin to eval experssion: XV1=merge(lCt0,0.09um)
[2026-08-31 09:43:12][info] the experssion belongs to via, deal it next time
[2026-08-31 09:43:12][info] begin to eval experssion: LOW1=<expr>
[2026-08-31 09:43:12][info] begin to eval experssion: LOW2=<expr>
[2026-08-31 09:43:12][info] begin to eval experssion: LOW3=<expr>
[2026-08-31 09:43:12][info] begin to eval experssion: LOW4=<expr>
[2026-08-31 09:43:12][info] begin to eval experssion: TOP1=<expr>
[2026-08-31 09:43:12][info] begin to eval experssion: TOP2=<expr>
[2026-08-31 09:43:12][info] begin to eval experssion: NEVER_USED=fill(lDt0,0um)
[2026-08-31 09:48:14][info] basis function are sorted from largest to smallest as follows:
[2026-08-31 09:48:14][info]   TOP1 (20%)
[2026-08-31 09:48:14][info]   TOP2 (15%)
[2026-08-31 09:48:14][info]   LOW4 (5%)
[2026-08-31 09:48:14][info]   LOW3 (5%)
[2026-08-31 09:48:14][info]   LOW2 (5%)
[2026-08-31 09:48:14][info]   LOW1 (<5%)
[2026-08-31 09:48:14][info]   XV2 (<5%)
[2026-08-31 09:48:14][info]   XV1 (<5%)
[2026-08-31 09:48:14][info]   BURIED (<5%)
[2026-08-31 09:48:14][detail]   total: 880652
[2026-08-31 09:48:14][info] build bf done: VV=1, SV=2, AV=3, PV=4, time=9.81s
"""
"""一份**合成**的 eWave 日志，形状逐字照 `references/probes/speed3d_run_20260828.txt`：
时间戳+级别前缀、层表达式段、紧跟着的 via 标记、元素占比段、`total:` 收尾。

层名是编的（真层名是 PDK 坐标，硬约束 1b）。三处是**故意**这样摆的：

* `BURIED` 是 via 但名字不以 V 开头 —— 靠名字猜 via 的实现会在这里当场红；
* `NEVER_USED` 在层表达式里有、在占比段里没有（这个 design 用不到它）——
  它不该出现在清单里，否则 `--3d` 会变得又长又假；
* 占比段的顺序（TOP1 最大）与层表达式的顺序（LOW1 在最下）**不同** ——
  钉住"输出按叠层顺序，不按占比顺序"。
"""


class TheToolReadsTheLayersSoNobodyTypesThem(unittest.TestCase):
    """`core.logparse.parse_layer_stack` —— 日志 → 层清单。"""

    def test_conductors_come_back_bottom_up(self) -> None:
        stack = logparse_module.parse_layer_stack(SYNTHETIC_LOG)
        self.assertEqual(stack.conductors, ("LOW1", "LOW2", "LOW3", "LOW4", "TOP1", "TOP2"))

    def test_vias_are_separated_by_what_the_log_says_not_by_the_name(self) -> None:
        """`BURIED` 是 via 却不以 V 开头 —— 判据只能是日志里那句话。

        猜错的方向很具体：把 via 当金属就会写进 `--3d`，而手册说那个 flag 管的是
        metal layer；把金属当 via 就会漏出 `--3d`，那层静默退 2D。
        """
        stack = logparse_module.parse_layer_stack(SYNTHETIC_LOG)
        self.assertEqual(stack.vias, ("BURIED", "XV2", "XV1"))
        self.assertNotIn("BURIED", stack.conductors)

    def test_a_layer_this_design_does_not_use_is_left_out(self) -> None:
        stack = logparse_module.parse_layer_stack(SYNTHETIC_LOG)
        self.assertNotIn("NEVER_USED", stack.conductors)
        self.assertNotIn("NEVER_USED", stack.shares)

    def test_the_element_share_comes_along(self) -> None:
        """占比不是装饰 —— 界面拿它回答"该降哪几层"（占 20% 的降了才省时间）。"""
        stack = logparse_module.parse_layer_stack(SYNTHETIC_LOG)
        self.assertEqual(stack.shares["TOP1"], "20%")
        self.assertEqual(stack.shares["LOW1"], "<5%")

    def test_a_log_without_the_element_report_gives_nothing(self) -> None:
        """**不猜**：只有层表达式就不知道这个 design 用了哪几层。"""
        head = SYNTHETIC_LOG.split("basis function")[0]
        stack = logparse_module.parse_layer_stack(head)
        self.assertTrue(stack.is_empty())
        self.assertIn("basis function", stack.note)

    def test_a_log_without_the_layer_expressions_gives_nothing(self) -> None:
        """**不猜**：只有占比段就分不出哪些是 via。"""
        tail = "basis function" + SYNTHETIC_LOG.split("basis function")[1]
        stack = logparse_module.parse_layer_stack(tail)
        self.assertTrue(stack.is_empty())
        self.assertIn("eval experssion", stack.note)

    def test_an_unrelated_log_is_not_mistaken_for_one(self) -> None:
        stack = logparse_module.parse_layer_stack("nothing to see here\nExecute emsolver done.\n")
        self.assertTrue(stack.is_empty())


class ItFindsTheLogInTheOfficialRunDir(unittest.TestCase):
    """`core.discover.find_layer_stack` —— 用户只填官方目录，层清单自己出现。"""

    def _dir(self, **files: str) -> str:
        root = tempfile.mkdtemp()
        self.addCleanup(_rmtree, root)
        for name, body in files.items():
            path = os.path.join(root, name.replace("__", os.sep))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(body)
        return root

    def test_it_reads_the_captured_stdout_log(self) -> None:
        root = self._dir(**{"run_ewave_typical_25_0.log": SYNTHETIC_LOG})
        stack = discover_module.find_layer_stack(root)
        self.assertEqual(stack.conductors, ("LOW1", "LOW2", "LOW3", "LOW4", "TOP1", "TOP2"))
        self.assertTrue(stack.source.endswith("run_ewave_typical_25_0.log"))

    def test_it_falls_back_to_the_ewave_log_one_level_down(self) -> None:
        """`<corner>_<temp>/ewave.log` —— 官方目录的实测布局（BRIEF §5）。"""
        root = self._dir(**{"typical_25_0__ewave.log": SYNTHETIC_LOG})
        stack = discover_module.find_layer_stack(root)
        self.assertEqual(stack.conductors[0], "LOW1")
        self.assertTrue(stack.source.endswith(logparse_module.EWAVE_LOG_NAME))

    def test_a_dir_with_no_log_says_so_instead_of_raising(self) -> None:
        """官方目录里没跑过是**正常状态**，不该让界面炸掉。"""
        stack = discover_module.find_layer_stack(self._dir())
        self.assertTrue(stack.is_empty())
        self.assertIn("nothing has been run there yet", stack.note)

    def test_a_missing_dir_says_so_instead_of_raising(self) -> None:
        stack = discover_module.find_layer_stack(os.path.join(tempfile.mkdtemp(), "nope"))
        self.assertTrue(stack.is_empty())
        self.assertTrue(stack.note)


class TheBridgeTurnsThatIntoCheckboxes(unittest.TestCase):
    """界面那一侧：用户勾层，不打层。"""

    def _bridge(self, **files: str) -> gui_state.GuiState:
        root = tempfile.mkdtemp()
        self.addCleanup(_rmtree, root)
        for name, body in files.items():
            with open(os.path.join(root, name), "w", encoding="utf-8", newline="\n") as handle:
                handle.write(body)
        bridge = gui_state.GuiState()
        bridge.set_designs([["MY_LIB", "CELL_A", "layout_em"]])
        bridge.set_official_run_dir(root)
        return bridge

    def test_pointing_at_the_official_dir_is_all_it_takes(self) -> None:
        bridge = self._bridge(**{"run_ewave_typical_25_0.log": SYNTHETIC_LOG})
        self.assertEqual(
            bridge.available_layers(), ("LOW1", "LOW2", "LOW3", "LOW4", "TOP1", "TOP2")
        )
        self.assertEqual(bridge.layer_shares()["TOP1"], "20%")
        self.assertIn("6 metal layers + 3 vias", bridge.layer_source_text())

    def test_ticking_layers_reproduces_the_command(self) -> None:
        """端到端：勾 4 层 → 预览就是要下发的那一串。用户一个层名都没打。"""
        bridge = self._bridge(**{"run_ewave_typical_25_0.log": SYNTHETIC_LOG})
        bridge.set_simplify_layers(["LOW2", "LOW3", "LOW4"])
        bridge.set_axis_values("simplify2d", ["off", "4"])
        self.assertEqual(
            bridge.simplify2d_preview(),
            "--3d=LOW1,TOP1,TOP2 --edgeDist=LOW2,4 LOW3,4 LOW4,4 --thinMaxfactor=1",
        )

    def test_the_click_order_does_not_reach_the_command_line(self) -> None:
        """勾选顺序归一到叠层顺序 ⇒ 两次跑的 cmd.sh 可以逐字比。"""
        bridge = self._bridge(**{"run_ewave_typical_25_0.log": SYNTHETIC_LOG})
        bridge.set_simplify_layers(["LOW4", "LOW2", "LOW3"])
        self.assertEqual(bridge.layer_model().simplify, ("LOW2", "LOW3", "LOW4"))

    def test_changing_the_official_dir_re_reads_the_layers(self) -> None:
        """层清单是 per-design 的 —— 留着上一个 design 的就可能少一层 ⇒ 静默 2D。"""
        bridge = self._bridge(**{"run_ewave_typical_25_0.log": SYNTHETIC_LOG})
        self.assertTrue(bridge.available_layers())
        empty = tempfile.mkdtemp()
        self.addCleanup(_rmtree, empty)
        bridge.set_official_run_dir(empty)
        self.assertEqual(bridge.available_layers(), ())

    def test_a_hand_added_layer_survives_next_to_the_discovered_ones(self) -> None:
        bridge = self._bridge(**{"run_ewave_typical_25_0.log": SYNTHETIC_LOG})
        bridge.set_layer_model(stack="EXTRA1", simplify="LOW2", thin="1")
        self.assertEqual(bridge.available_layers()[-1], "EXTRA1")
        self.assertEqual(bridge.extra_layers_text(), "EXTRA1")

    def test_the_extra_box_does_not_become_a_second_copy_of_the_stack(self) -> None:
        """读到的那部分不回显在「extra layers」里 —— 两个副本必然漂。"""
        bridge = self._bridge(**{"run_ewave_typical_25_0.log": SYNTHETIC_LOG})
        self.assertEqual(bridge.extra_layers_text(), "")

    def test_the_stack_is_persisted_so_a_box_without_the_log_still_works(self) -> None:
        """存的是**并集** —— 「下次启动不用再 load」这句话对这一格也要成立。"""
        bridge = self._bridge(**{"run_ewave_typical_25_0.log": SYNTHETIC_LOG})
        bridge.set_simplify_layers(["LOW2", "LOW3"])
        saved = bridge.spec_snapshot()
        self.assertEqual(saved.layer_model.stack, bridge.available_layers())
        self.assertEqual(saved.layer_model.simplify, ("LOW2", "LOW3"))

    def test_without_a_log_it_says_where_to_point_instead_of_going_blank(self) -> None:
        bridge = gui_state.GuiState()
        self.assertEqual(bridge.available_layers(), ())
        self.assertIn("official run dir", bridge.layer_source_text())


def _rmtree(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)

# ==========================================================================
# 真界面：那排勾选框是**动态建**的，所以"控件还没建好"这个瞬间是真存在的
# ==========================================================================

_SHARED_ROOT: object | None = None
"""**整个模块共用一个** Tk 根窗口 —— 一个进程里反复 `Tk()` 在某些机器上会失败，
而那是测试自己的毛病、看起来却像被测代码坏了（口径抄 `tests/test_gui_invariants.py`）。"""


def _tk_or_skip(test: unittest.TestCase) -> object:
    """本机能不能开窗口。开不了就**带原因**跳过（平台性 skip）。"""
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


class TheCheckboxesDoNotEatTheSelection(unittest.TestCase):
    """🚨 实拍过的 bug 的回归（2026-08-31 开发中）。

    那排「2D layers」勾选框的层名来自 eWave 日志，**源码里一个都没有**（硬约束 1b）⇒
    它们只能在 `_sync_layer_boxes` 里动态建。而 `recompute()` 的顺序是 **先 push、后 sync**，
    于是第一次 recompute 时 `s2d_layer_vars` 还是空的 ——
    「一个控件都还没有」和「用户把每一层都取消勾选了」在回写那一步长得一模一样，
    读回来的那份选择当场被清空，**而界面上什么都不会显示出错**。

    断言的是**结果**（选择还在、命令还对），不是"那道守卫会响"。
    """

    def _app(self, log: str = SYNTHETIC_LOG):
        root = _tk_or_skip(self)
        from gui.frames import split

        offdir = tempfile.mkdtemp()
        self.addCleanup(_rmtree, offdir)
        with open(
            os.path.join(offdir, "run_ewave_typical_25_0.log"), "w", encoding="utf-8", newline="\n"
        ) as handle:
            handle.write(log)

        bridge = gui_state.GuiState(batch_root=tempfile.mkdtemp(), batch_name="s2d")
        bridge.add_design("MY_LIB", "CELL_A", "layout_em")
        bridge.set_official_run_dir(offdir)
        bridge.set_simplify_layers(["LOW2", "LOW3", "LOW4"])
        bridge.set_axis_values("simplify2d", ["off", "4"])

        frame = split.build_frame(root, bridge)
        frame.pack()
        app = frame._ewb_app
        app.recompute()
        return bridge, app

    def test_the_first_refresh_keeps_the_layers_that_were_loaded(self) -> None:
        bridge, _app = self._app()
        self.assertEqual(bridge.layer_model().simplify, ("LOW2", "LOW3", "LOW4"))
        self.assertEqual(
            bridge.simplify2d_preview(),
            "--3d=LOW1,TOP1,TOP2 --edgeDist=LOW2,4 LOW3,4 LOW4,4 --thinMaxfactor=1",
        )

    def test_the_boxes_show_what_the_model_says(self) -> None:
        _bridge, app = self._app()
        ticked = {name for name, var in app.s2d_layer_vars.items() if var.get()}
        self.assertEqual(ticked, {"LOW2", "LOW3", "LOW4"})
        self.assertEqual(
            tuple(app.s2d_layer_vars), ("LOW1", "LOW2", "LOW3", "LOW4", "TOP1", "TOP2")
        )

    def test_unticking_one_really_unticks_it(self) -> None:
        """守卫不许把"用户真的取消勾选"也一起挡掉。"""
        bridge, app = self._app()
        app.s2d_layer_vars["LOW4"].set(False)
        app.recompute()
        self.assertEqual(bridge.layer_model().simplify, ("LOW2", "LOW3"))

    def test_no_layer_name_is_ever_typed(self) -> None:
        """界面上再没有"整份叠层"那个输入框 —— 只剩「日志没给出的额外层」那一格，
        而它正常情况下是空的。"""
        _bridge, app = self._app()
        self.assertEqual(app.s2d_extra.get(), "")
        self.assertFalse(hasattr(app, "s2d_layers"))

# ==========================================================================
# 层清单按 design 走（2026-08-31 实测出来的两个缺陷的回归）
# ==========================================================================



def _make_look_like_an_official_dir(root: str) -> None:
    """给临时目录放一份 `gdsout_setup` —— 它是"这是不是官方 design 目录"的判据。

    ⚠️ 不放的代价不是"少测一点"，是**慢 5 秒**：缺了它 `discover_site_facts` 走
    报错那条路，而那条路为了给一句"你是不是指错了目录"会去遍历父目录
    （`suggest_official_dirs`，深度 3）。临时目录的父目录是系统 temp，
    实测约 3.7 万次 lstat / 5.6 秒一次。真的官方目录里这个文件本来就在，
    所以放上它既快又更像真的。
    """
    body = '\tlibrary\t"MY_LIB"\n\ttopCell\t"CELL"\n\tview\t"layout_em"\n'
    with open(os.path.join(root, "gdsout_setup"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(body)


def _log_for(layers: tuple[str, ...], vias: tuple[str, ...] = ("XV1",)) -> str:
    """按 `SYNTHETIC_LOG` 的形状现造一份日志。层名由调用方给，好让两个 design 不一样。"""
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


class EachDesignGetsItsOwnLayers(unittest.TestCase):
    """🚨 2026-08-31 实测到的**静默 2D 复发**的回归。

    层清单当初是批次级的（取自批次那一格、或第一个 design），却给**所有** design 共用。
    而 `SiteFacts` 是 per-design 取的 —— 两边口径不一致，后果很具体：

        CELL_A 的日志: LOW1, LOW2, TOP1
        CELL_B 的日志: LOW1, LOW2, TOP1, EXTRA_TOP
        共用的 --3d  : LOW1,TOP1              <- CELL_B 的 EXTRA_TOP 不在里面
        ⇒ EXTRA_TOP 在 CELL_B 里被静默降成 2D

    也就是这个功能要消灭的那类错误，在**多 design 这一层**原样重造了一遍。
    """

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
        """两个 design，**各自**一个官方目录。走 `load_spec` 这条公开路径 ——
        per-design 的官方目录本来就只能从 spec / session 来。"""
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
        return matrix.simplify2d_flags_for(model, "4")["--3d"]

    def test_the_checkbox_list_is_the_union_of_every_design(self) -> None:
        """选择面是并集：批次里任何一个 design 有的层都该能勾。"""
        bridge = self._bridge()
        self.assertEqual(bridge.available_layers(), self.B_LAYERS)

    def test_a_layer_only_the_second_design_has_is_not_dropped(self) -> None:
        """**这条就是那个缺陷。** 修之前 CELL_B 的 --3d 是 `LOW1,TOP1`。"""
        bridge = self._bridge()
        bridge.set_simplify_layers(["LOW2"])
        self.assertEqual(self._keep3d(bridge, "CELL_A"), "LOW1,TOP1")
        self.assertEqual(self._keep3d(bridge, "CELL_B"), "LOW1,TOP1,EXTRA_TOP")

    def test_a_design_does_not_get_layers_it_does_not_have(self) -> None:
        """反方向也要对：CELL_A 的 --3d 里不许出现只有 CELL_B 才有的层。

        多一个层名最多是命令行长一点，但"eWave 会忽略不认识的层名"我们**没有证据**，
        所以能不多给就不多给。
        """
        bridge = self._bridge()
        bridge.set_simplify_layers(["LOW2"])
        self.assertNotIn("EXTRA_TOP", self._keep3d(bridge, "CELL_A"))

    def test_ticking_a_layer_this_design_lacks_is_not_an_error(self) -> None:
        """勾了一个只有 CELL_B 有的层：对 CELL_A 就是不生效，不该报错。

        多 design 批次里这很自然 —— 一个 cell 用 RDL、另一个不用。
        """
        bridge = self._bridge()
        bridge.set_simplify_layers(["LOW2", "EXTRA_TOP"])
        self.assertEqual(self._keep3d(bridge, "CELL_A"), "LOW1,TOP1")
        self.assertEqual(self._keep3d(bridge, "CELL_B"), "LOW1,TOP1")

    def test_a_design_without_a_log_falls_back_to_the_batch_union(self) -> None:
        """读不到它自己的层时退回并集 = 加这条修之前的行为。

        那时我们对它一无所知，用已知的最大集合比用空集好（空集 = 拼不出命令）。
        """
        bridge = self._bridge(b_layers=None)
        bridge.set_simplify_layers(["LOW2"])
        self.assertEqual(self._keep3d(bridge, "CELL_B"), "LOW1,TOP1")

    def test_the_per_design_axis_reaches_the_plan(self) -> None:
        """端到端：`plan()` 出来的 `PlanContext` 里那根轴就是这个 design 自己的。"""
        bridge = self._bridge()
        bridge.set_simplify_layers(["LOW2"])
        bridge.set_axis_values("simplify2d", ["4"])
        bridge.set_axis_values("temperature", ["25.0"])
        bridge.plan()
        seen = {}
        for key, ctx in bridge._contexts.items():
            axis = next(a for a in ctx.axes if a.name == "simplify2d")
            seen[key] = axis.values[0].flags["--3d"]
        self.assertEqual(seen["MY_LIB_CELL_A_layout_em"], "LOW1,TOP1")
        self.assertEqual(seen["MY_LIB_CELL_B_layout_em"], "LOW1,TOP1,EXTRA_TOP")

    def test_an_off_only_batch_is_left_alone(self) -> None:
        """整批都是 off ⇒ 这根轴对命令行没有贡献，不按 design 重算（也就不去读日志）。"""
        bridge = self._bridge()
        bridge.set_axis_values("simplify2d", ["off"])
        bridge.set_axis_values("temperature", ["25.0"])
        bridge.plan()
        for ctx in bridge._contexts.values():
            axis = next(a for a in ctx.axes if a.name == "simplify2d")
            self.assertEqual(
                axis.values[0].flags, {"--3d": False, "--edgeDist": False, "--thinMaxfactor": False}
            )

    def test_a_layer_no_design_has_shows_up_as_a_run_error_not_a_dead_preview(self) -> None:
        """造不出轴时走**按 run 显示**那条通道 —— `plan()` 的承诺是"矩阵照常画得出来"。

        在 `_build_contexts` 里抛异常会把整块预览打掉，那比看不见错误更糟。
        """
        bridge = self._bridge()
        bridge.set_layer_model(stack="GHOST", simplify="GHOST", thin="1")
        bridge.set_axis_values("simplify2d", ["4"])
        bridge.set_axis_values("temperature", ["25.0"])
        bridge.plan()
        self.assertTrue(bridge.runs(), "矩阵必须还在")
        self.assertEqual(len(bridge._plan_errors), len(bridge.runs()))
        self.assertIn("no layer is marked for 2D", list(bridge._plan_errors.values())[0])

    def test_a_historical_batch_keeps_its_frozen_axes(self) -> None:
        """打开历史批次时**不许**拿今天的日志重算 —— 那是"resume 之后 --3d 悄悄换了"。

        走的是 `_build_contexts(learn=False)`（公开路径要先把批次写到盘上，
        而这里要断言的恰恰是那一个布尔值的分岔）。
        """
        bridge = self._bridge()
        bridge.set_simplify_layers(["LOW2"])
        bridge.set_axis_values("simplify2d", ["4"])
        bridge.set_axis_values("temperature", ["25.0"])
        bridge.plan()
        state = bridge.result_state() or bridge._state
        assert state is not None
        frozen = {a.name: a for a in state.axes}["simplify2d"]
        contexts = bridge._build_contexts(state, learn=False)
        for ctx in contexts.values():
            axis = next(a for a in ctx.axes if a.name == "simplify2d")
            self.assertEqual(axis.values[0].flags, frozen.values[0].flags)


class SwitchingTheOfficialDirDropsStaleLayers(unittest.TestCase):
    """🚨 2026-08-31 实测到的第二个缺陷的回归：旧层名越堆越多。

    `available_layers()` 是"读到的 ∪ 已知的"、只增不减，而"已知的"来自上一次存盘。
    存过一次 session 之后换 design：

        A 目录:              (OLD1, OLD2, OLDTOP)
        存过盘再换到 B 目录:  (NEW1, NEW2, NEWTOP, OLD1, OLD2, OLDTOP)
                                                  ^^^^^^^^^^^^^^^^^^^ 三个是死的

    界面多三个没意义的勾选框，`--3d` 里也带着这个 design 没有的层名 ——
    而"eWave 会忽略不认识的层名"我们**没有证据**。
    """

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

    def _reopened(self, first: str):
        """在 A 目录上用一次、存盘、重开 —— 「上次那份设定」那条路。"""
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
        """新目录读不到层就留着旧的 —— 那时它是唯一的来源。

        方向与 `available_layers()` 的"只增不减"一致：宁可多，不可无。
        """
        bridge = self._reopened(self._offdir(("OLD1", "OLD2", "OLDTOP")))
        bridge.set_official_run_dir(self._offdir(None))
        self.assertEqual(bridge.available_layers(), ("OLD1", "OLD2", "OLDTOP"))

    def test_reopening_on_the_same_dir_keeps_everything(self) -> None:
        """「下次启动不用再 load」那条承诺不许被这条修弄坏。"""
        offdir = self._offdir(("OLD1", "OLD2", "OLDTOP"))
        bridge = self._reopened(offdir)
        bridge.set_official_run_dir(offdir)
        self.assertEqual(bridge.available_layers(), ("OLD1", "OLD2", "OLDTOP"))



if __name__ == "__main__":  # pragma: no cover
    unittest.main()
