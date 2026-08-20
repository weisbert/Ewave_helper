"""spec 存盘/读回的两条「无声改设定」—— 2026-08-19 三路复核抓到的。

两条都不是假想，都是当天在这棵树上实测复现过的：

1. **`spec_to_mapping` 丢掉了每个取值的 flag。** 轴在文件里通常只写成 `轴名: [取值…]`，
   读回来靠**内置目录**重新翻译。可界面自造的两根轴翻不回来 —— 三段网格
   `0.4/0.5/0.4`（`-e`/`-d`/`--viaMergeSpace` 三个值互不相同）和频率扫描
   （`--multiSweep=<一整串>` 外加两个 `False` 抵消互斥写法）。而 `axes_slug` 只由
   **取值字符串**决定 ⇒ flag 变了目录名一个字不变 ⇒ 归档里那份结果声称自己跑的是
   它根本没跑的设定。修法：`value_flags:`。
2. **`name: base` 把顶层 `axes:` 收窄了。** 顶层 `axes:` 是全批次的轴**定义**
   （「Save spec as…」写出来的就是并集）。收窄之后，只有别的组用到的那个取值就从定义里
   消失 ⇒ 读回来当场 `SpecError`，而这份文件正是本工具自己写出去的。

两条的共同点：`tests/test_spec_dump.py` 那条 mapping → spec → mapping 的**不动点**断言
一个都抓不到 —— 往返确实是不动点，变的是语义。
"""

from __future__ import annotations

import unittest

from ewave_batch import model
from ewave_batch.core import matrix
from ewave_batch.core import spec as spec_module


def _designs() -> list[dict]:
    return [{"library": "<lib>", "cell": "<cell>", "view": "<view>"}]


class ValueFlagsSurviveTheRoundTrip(unittest.TestCase):
    """存下来的 spec 读回来必须是**同一组 flag**。"""

    def _round_trip(self, axis: model.Axis) -> model.Axis:
        spec = model.BatchSpec(
            designs=[model.Design(library="<lib>", cell="<cell>", view="<view>")],
            axes=[axis],
        )
        back = spec_module.parse_spec_mapping(spec_module.spec_to_mapping(spec))
        self.assertEqual(len(back.axes), 1)
        return back.axes[0]

    @staticmethod
    def _sweep_axis() -> model.Axis:
        """界面那根 freq 轴的形状（`gui.state.sweep_axis` 造出来的那种）。"""
        return model.Axis(
            name="freq",
            values=(
                model.AxisValue(
                    "adaptive,0:0.1:40",
                    flags={
                        "--multiSweep": "adaptive,0:0.1:40",
                        "--logarithmicSweep": False,
                        "--discreteFreq": False,
                    },
                ),
            ),
            flags=("--multiSweep", "--logarithmicSweep", "--discreteFreq"),
            short="freq",
        )

    def test_sweep_axis_keeps_its_flags(self) -> None:
        got = self._round_trip(self._sweep_axis())
        self.assertEqual(
            dict(got.values[0].flags),
            {
                "--multiSweep": "adaptive,0:0.1:40",
                "--logarithmicSweep": False,
                "--discreteFreq": False,
            },
            "扫频串没了、三个互斥的 flag 还同时打开 —— sweep_axis 存在就是为了防这一件事",
        )

    def test_sweep_axis_without_value_flags_is_wrong_negative(self) -> None:
        """反向：把 `value_flags:` 从文件里删掉，读回来必须**不再**是同一组 flag。

        这条红了才说明上面那条测的是 `value_flags` 起的作用，而不是碰巧本来就对。
        """
        data = spec_module.spec_to_mapping(
            model.BatchSpec(
                designs=[model.Design(library="<lib>", cell="<cell>", view="<view>")],
                axes=[self._sweep_axis()],
            )
        )
        self.assertIn("value_flags", data["axes"]["freq"], "前提：这根轴本来就该带 value_flags")
        del data["axes"]["freq"]["value_flags"]
        back = spec_module.parse_spec_mapping(data)
        self.assertNotEqual(
            dict(back.axes[0].values[0].flags),
            dict(self._sweep_axis().values[0].flags),
            "删掉 value_flags 竟然还是同一组 flag —— 那这条序列化就没必要存在",
        )

    def test_three_segment_mesh_keeps_its_three_different_values(self) -> None:
        axis = model.Axis(
            name="mesh",
            values=(
                model.AxisValue(
                    "0.4/0.5/0.4",
                    flags={"-e": "0.4", "-d": "0.5", "--viaMergeSpace": "0.4"},
                ),
            ),
            kind=model.AxisKind.GROUP,
            flags=("-e", "-d", "--viaMergeSpace"),
            short="mesh",
        )
        got = self._round_trip(axis)
        self.assertEqual(
            dict(got.values[0].flags),
            {"-e": "0.4", "-d": "0.5", "--viaMergeSpace": "0.4"},
        )

    def test_a_translatable_axis_is_not_pinned(self) -> None:
        """★ 另一半：翻得回来的轴**不许**被写成 `value_flags:`。

        写了 `value_flags` 的轴就**不能再现造新取值**（`matrix._materialize_value` 要求整根轴
        flag 形状统一且带 `{value}` 占位符）—— 于是一个组想换个 mesh 数值就当场报错。
        所以判据必须是**语义**相等（占位符先代入再比），不是逐字相等：目录写的是模板
        `{"-e": "{value}"}`，界面写的是算好的 `{"-e": "0.4"}`，两者渲染出来一模一样。
        """
        catalog = matrix.builtin_axis_catalog()
        gui_style = model.Axis(
            name="mesh",
            values=(
                model.AxisValue(
                    "0.4", flags={"-e": "0.4", "-d": "0.4", "--viaMergeSpace": "0.4"}
                ),
            ),
            kind=model.AxisKind.GROUP,
            flags=("-e", "-d", "--viaMergeSpace"),
            short="mesh",
        )
        for axis in (matrix.axis_with_values(catalog["mesh"], ["0.4", "0.5"]), gui_style):
            data = spec_module.spec_to_mapping(
                model.BatchSpec(
                    designs=[model.Design(library="<lib>", cell="<cell>", view="<view>")],
                    axes=[axis],
                )
            )
            body = data["axes"]["mesh"]
            self.assertNotIn(
                "value_flags",
                body if isinstance(body, dict) else {},
                "翻得回来的轴不该被钉死成 value_flags —— 钉死了组就换不了取值",
            )


class ExplicitBaseGroupKeepsTheAxisDefinition(unittest.TestCase):
    """`name: base` + 别的组时，顶层 `axes:` 不许被收窄。"""

    def _mapping(self, groups: list) -> dict:
        return {
            "designs": _designs(),
            "axes": {"temperature": ["-40.0", "55.0", "125.0"]},
            "groups": groups,
        }

    def test_base_alone_still_merges_into_the_axes(self) -> None:
        """只有 base 一条时保持老行为：并进顶层 axes，`groups` 留空。"""
        spec = spec_module.parse_spec_mapping(
            self._mapping([{"name": "base", "axes": {"temperature": ["55.0"]}}])
        )
        self.assertEqual([av.value for av in spec.axes[0].values], ["55.0"])
        self.assertEqual(list(spec.groups), [])

    def test_base_with_siblings_keeps_the_definition_and_pins_them(self) -> None:
        spec = spec_module.parse_spec_mapping(
            self._mapping(
                [
                    {"name": "base", "axes": {"temperature": ["55.0"]}},
                    {"name": "cold", "axes": {"temperature": ["-40.0"]}},
                ]
            )
        )
        self.assertEqual(
            [av.value for av in spec.axes[0].values],
            ["-40.0", "55.0", "125.0"],
            "顶层 axes 是轴定义，不许被 base 收窄",
        )
        self.assertEqual([g.name for g in spec.groups], ["base", "cold"])
        state = spec_module.spec_to_batch(spec, batch_root="./x")
        self.assertEqual(
            sorted((run.group, run.axis_values["temperature"]) for run in state.runs),
            [("base", "55.0"), ("cold", "-40.0")],
            "base 只该扫 55，cold 只该扫 -40 —— 谁都不许把 125 也扫一遍",
        )

    def test_a_sibling_group_may_use_a_value_base_does_not(self) -> None:
        """收窄会让这一条直接炸掉 —— 它就是这个修法存在的理由。"""
        spec = spec_module.parse_spec_mapping(
            {
                "designs": _designs(),
                "axes": {
                    "mesh": {
                        "values": ["0.4/0.5/0.4", "0.3/0.4/0.3"],
                        "value_flags": {
                            "0.4/0.5/0.4": {"-e": "0.4", "-d": "0.5", "--viaMergeSpace": "0.4"},
                            "0.3/0.4/0.3": {"-e": "0.3", "-d": "0.4", "--viaMergeSpace": "0.3"},
                        },
                    }
                },
                "groups": [
                    {"name": "base", "axes": {"mesh": ["0.4/0.5/0.4"]}},
                    {"name": "finer", "axes": {"mesh": ["0.3/0.4/0.3"]}},
                ],
            }
        )
        state = spec_module.spec_to_batch(spec, batch_root="./x")
        self.assertEqual(
            [(run.group, run.axis_values["mesh"]) for run in state.runs],
            [("base", "0.4/0.5/0.4"), ("finer", "0.3/0.4/0.3")],
        )


class BatchStateAxesAreTheWholeBatch(unittest.TestCase):
    """★ `BatchState.axes` 存的是**全批次并集**，不是顶层 `axes:` 那份。

    `PlanContext.axes` 就是从这里来的（`cli.py` 和 `gui/state.py` 各有一处），
    而 `core.cmd.build_flag_layers` 拿 `run.axis_values[轴名]` 去**轴的取值表里查** flag。
    只存 base 那份的话，任何一个组独有的取值都查不到 ⇒
    `resolve_axis_flags` 抛 "axis 'equalCurrent' has no value 'off'"，
    CLI / GUI / 红区 dry-run 三条路一起废（2026-08-19 实测）。
    """

    def _state(self) -> model.BatchState:
        spec = spec_module.parse_spec_mapping(
            {
                "designs": _designs(),
                "axes": {"temperature": ["55.0"], "equalCurrent": ["on"]},
                "groups": [{"name": "eqcur-off", "axes": {"equalCurrent": ["off"]}}],
            }
        )
        return spec_module.spec_to_batch(spec, batch_root="./x")

    def test_group_only_value_is_in_the_stored_axis(self) -> None:
        state = self._state()
        by_name = {axis.name: axis for axis in state.axes}
        self.assertEqual(
            [av.value for av in by_name["equalCurrent"].values],
            ["on", "off"],
            "组独有的取值必须出现在批次的轴定义里",
        )

    def test_every_run_can_build_its_flags(self) -> None:
        """真正的判据：每个 run 都要能算出 flag —— 这才是那条 SpecError 的落点。"""
        from ewave_batch.core import cmd as cmd_module

        state = self._state()
        ctx = model.PlanContext(
            design=state.designs[0],
            facts=model.SiteFacts(ewave_bin="ewave"),
            axes=tuple(state.axes),
            batch_dir=state.batch_dir,
        )
        got = {}
        for run in state.runs:
            got[run.group] = cmd_module.build_flag_layers(run, ctx).axis
        self.assertEqual(got["base"]["--equalCurrent"], True)
        self.assertIs(
            got["eqcur-off"]["--equalCurrent"],
            False,
            "off 必须落 False（显式缺席），不是把 flag 丢掉 —— INTERFACES 契约 1",
        )

    def test_base_only_batch_stores_exactly_the_spec_axes_negative(self) -> None:
        """反向：没有组时，存进去的取值必须与 spec 里写的**逐字相同**（不许顺手加宽）。"""
        spec = spec_module.parse_spec_mapping(
            {"designs": _designs(), "axes": {"temperature": ["55.0"], "equalCurrent": ["on"]}}
        )
        state = spec_module.spec_to_batch(spec, batch_root="./x")
        self.assertEqual(
            {axis.name: [av.value for av in axis.values] for axis in state.axes},
            {"temperature": ["55.0"], "equalCurrent": ["on"]},
        )


if __name__ == "__main__":
    unittest.main()
