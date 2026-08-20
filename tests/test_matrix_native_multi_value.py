"""★ `options.native_multi_value` 和 run group **不能同时用** —— 2026-08-19 复核实测。

原生多值（D12）把整条温度列表折成一个取值 `-40.0,55.0,125.0`，于是这个 run 的
`ewave_dir` **预测不出来**（留空串），`run_id` 的尾巴只剩 corner；而 temperature 是
`encoded_in_ewave_dir=True` 的轴、永远不进 `axes_slug` —— 也就是说"这个 run 跑了哪几个温度"
在 `run_id` 里一个字都没有。

只有 base 一个组时这没问题。一旦某个组把温度收窄，两种坏法各一条，下面各配一条测试：

* 组里只剩**一个**温度 ⇒ 它不折叠，尾巴变成 `typical_55_0`，而 `axes_slug` 与 base 相同
  ⇒ 两个 run 的 `--workDir` 是**同一个目录**，eWave 又会在里面各建一个 `typical_55_0/`
  ⇒ **静默覆盖**，而且两个 `run_id` 不同、任何守卫都不会响；
* 组里剩**两个**温度 ⇒ 它照样折叠，尾巴同样只有 `typical` ⇒ 撞 `run_id`，抛的是那句
  "这是工具的 bug，请报告"，而用户的 spec 完全正常。

`options.native_multi_value` 是用户可达的（`core.spec._parse_options` 收下 `BatchOptions`
的每一个字段），所以这不是"内部才碰得到的组合"。
"""

from __future__ import annotations

import unittest

from ewave_batch.core import layout, matrix
from ewave_batch.model import BatchOptions, Design, RunGroup, SpecError


def _designs() -> list[Design]:
    return [Design(library="MY_LIB", cell="CELL_A", view="layout")]


def _axes(temps: list[str]) -> list:
    catalog = matrix.builtin_axis_catalog()
    return [
        matrix.axis_with_values(catalog["corner"], ["typical"]),
        matrix.axis_with_values(catalog["temperature"], temps),
        matrix.axis_with_values(catalog["equalCurrent"], ["on"]),
    ]


TEMPS = ["-40.0", "55.0", "125.0"]


class NativeMultiValueRejectsGroups(unittest.TestCase):
    def test_a_group_that_narrows_temperature_is_rejected(self) -> None:
        with self.assertRaises(SpecError) as caught:
            matrix.expand_runs(
                _designs(),
                _axes(TEMPS),
                options=BatchOptions(native_multi_value=True),
                groups=[RunGroup(name="t55", axis_overrides={"temperature": ("55.0",)})],
            )
        message = str(caught.exception)
        self.assertIn("native_multi_value", message)
        self.assertIn("Next:", message, "报错要给下一步")
        self.assertTrue(all(ord(ch) < 128 for ch in message), "红区 LANG 常是 C => 纯 ASCII")

    def test_a_group_that_keeps_several_temperatures_is_rejected_too(self) -> None:
        """另一种坏法（撞 run_id 报"工具 bug"）也必须走同一条拒绝路径。"""
        with self.assertRaises(SpecError) as caught:
            matrix.expand_runs(
                _designs(),
                _axes(TEMPS),
                options=BatchOptions(native_multi_value=True),
                groups=[
                    RunGroup(name="t2", axis_overrides={"temperature": ("-40.0", "55.0")})
                ],
            )
        self.assertIn("native_multi_value", str(caught.exception))
        self.assertNotIn(
            "please report",
            str(caught.exception),
            "普通 spec 不该被告知'这是工具的 bug'",
        )

    def test_native_multi_value_without_groups_still_works_negative(self) -> None:
        """反向：没有组时 D12 照常工作 —— 拒绝的是**组合**，不是这个 option 本身。"""
        runs = matrix.expand_runs(
            _designs(), _axes(TEMPS), options=BatchOptions(native_multi_value=True)
        )
        self.assertEqual(len(runs), 1, "三个温度折成一个 run 正是 D12 的全部意义")
        self.assertEqual(runs[0].axis_values["temperature"], "-40.0,55.0,125.0")

    def test_groups_without_native_multi_value_still_work_negative(self) -> None:
        """反向的另一半：组自己也照常工作。"""
        runs = matrix.expand_runs(
            _designs(),
            _axes(TEMPS),
            groups=[RunGroup(name="t55", axis_overrides={"temperature": ("55.0",)})],
        )
        self.assertEqual(len(runs), 3, "t55 的 55 度与 base 的 55 度跨组去重 => 还是 3 个")
        self.assertEqual({run.group for run in runs}, {"base"})

    def test_the_collision_this_guard_prevents_is_real(self) -> None:
        """证明这条守卫拦的是真东西：手工绕过它，两个 run 确实落进同一个目录。

        没有这一条的话，上面那些 `assertRaises` 只证明"我们会报错"，
        证明不了"不报错就会出事" —— 防自证配方 2。
        """
        design = _designs()[0]
        base = matrix.expand_runs(
            [design], _axes(TEMPS), options=BatchOptions(native_multi_value=True)
        )
        narrowed = matrix.expand_runs(
            [design], _axes(["55.0"]), options=BatchOptions(native_multi_value=True)
        )
        self.assertEqual(len(base), 1)
        self.assertEqual(len(narrowed), 1)
        self.assertNotEqual(
            base[0].run_id, narrowed[0].run_id, "前提：run_id 不同 => 撞不出 SpecError"
        )
        base_dir = layout.compute_run_paths("/batch", design, base[0]).run_dir
        other_dir = layout.compute_run_paths("/batch", design, narrowed[0]).run_dir
        self.assertEqual(
            base_dir,
            other_dir,
            "两个设定不同的 run 落进同一个 --workDir —— 这就是那条守卫拦住的东西",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
