"""`ewave_batch.core.spec` 的测试 —— 读用户手写的 spec。

**期望值全部是手写字面量**（防自证配方 2）：

* design 展开表 / run_id 表：一行一行敲出来的，推导依据写在各自的注释里
  （`PROJECT_BRIEF.md` §5「归档布局」+ 「官方流程的既有布局」，与 `tests/test_matrix.py`
  用的是同一批规则）。
* `spec_sha256` 的期望值用 **NIST 的 SHA-256 测试向量**（`sha256("abc")`）——
  它与本项目的实现完全无关，是真正的外部基准。

本机**没装 PyYAML**（红区装了 6.0.1）。所以 YAML 那条路用两种办法验：
① 往 `sys.modules` 里塞一个假的 `yaml` 模块，断言我们调的是 `safe_load` 而不是 `load`；
② 把 `sys.modules["yaml"]` 设成 None（`import yaml` 会抛 ImportError），断言 JSON 退路
   和报错文案。真·PyYAML 在场时的解析只能在红区验，`test_example_spec_parses` 会带原因 skip。

站点标识符零出现：全是 `MY_LIB` / `CELL_A` / `<lib>` 这种编出来的占位符。
"""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ewave_batch.core import matrix, spec
from ewave_batch.model import (
    AxisKind,
    BatchOptions,
    FlagConflictError,
    PortMode,
    RunStatus,
    SpecError,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PATH = ROOT / "docs" / "spec_example.yaml"


def _has_pyyaml() -> bool:
    """PyYAML 在不在。用 `find_spec` 而不是 `import` —— 别把 yaml 塞进 `sys.modules`。"""
    return importlib.util.find_spec("yaml") is not None


# ==========================================================================
# 共用输入（正反两条测试必须走同一条构造路径）
# ==========================================================================

GOLDEN_SPEC: dict = {
    "batch_name": "demo",
    "batch_root": "./batches",
    "designs": [
        {
            # library / cell / view 都可以写成列表 → 自动展开成笛卡尔积（抄 Auto_ext 的 tasks.yaml）
            "library": "MY_LIB",
            "cell": ["CELL_A", "CELL_B"],
            "view": ["layout", "layout_em"],
            "official_run_dir": "OFFDIR_A",
            "resources": "cpu=20;mem=100000",
        },
        {
            "library": "OTHER_LIB",
            "cell": "CELL_C",
            "view": "layout",
            "axes": {"temperature": ["25.0"]},  # per-design 覆盖
        },
    ],
    "axes": {"corner": ["typical"], "temperature": ["-40.0", "125.0"]},
    "options": {"max_parallel": 2, "scheduler": "fake"},
}

# 1 library × 2 cell × 2 view = 4 个 design，再加第二条 = 5 个。
# 展开顺序：library 最慢、view 最快（`itertools.product(libs, cells, views)`）。
# design id 的形状 = `<library>_<topCell>_<view>`（BRIEF §5「官方流程的既有布局」）。
GOLDEN_DESIGNS: tuple[tuple[str, str, str, str], ...] = (
    # (library, cell, view, design_key)
    ("MY_LIB", "CELL_A", "layout", "MY_LIB_CELL_A_layout"),
    ("MY_LIB", "CELL_A", "layout_em", "MY_LIB_CELL_A_layout_em"),
    ("MY_LIB", "CELL_B", "layout", "MY_LIB_CELL_B_layout"),
    ("MY_LIB", "CELL_B", "layout_em", "MY_LIB_CELL_B_layout_em"),
    ("OTHER_LIB", "CELL_C", "layout", "OTHER_LIB_CELL_C_layout"),
)

# 9 个 run：前 4 个 design 各扫 2 个温度，第 5 个被 per-design 覆盖成只扫 25.0。
# corner 只有一个取值 ⇒ 不进 slug；temperature 是 eWave 自己编进目录名的轴 ⇒ 也不进 slug
# ⇒ 全部 `base`（BRIEF §5「没有额外轴时用 base」）。
# 目录名 = `<corner>_<温度的小数点换下划线>`（§5「官方流程的既有布局」）。
GOLDEN_RUN_IDS: tuple[str, ...] = (
    "MY_LIB_CELL_A_layout/base/typical_-40_0",
    "MY_LIB_CELL_A_layout/base/typical_125_0",
    "MY_LIB_CELL_A_layout_em/base/typical_-40_0",
    "MY_LIB_CELL_A_layout_em/base/typical_125_0",
    "MY_LIB_CELL_B_layout/base/typical_-40_0",
    "MY_LIB_CELL_B_layout/base/typical_125_0",
    "MY_LIB_CELL_B_layout_em/base/typical_-40_0",
    "MY_LIB_CELL_B_layout_em/base/typical_125_0",
    "OTHER_LIB_CELL_C_layout/base/typical_25_0",
)

DESIGN_FIELDS = ("library", "cell", "view", "design_key")


def design_rows(parsed) -> list[tuple[str, str, str, str]]:
    return [(d.library, d.cell, d.view, matrix.design_key(d)) for d in parsed.designs]


def diff_rows(actual: list[tuple], expected: tuple[tuple, ...], names: tuple[str, ...]) -> tuple[list[str], int]:
    """逐行逐字段比两张表，返回 (差异描述, **实际比过的字段数**)。

    第二个返回值是防自证配方 4 要的计数断言素材 —— 空集合的 diff 永远是绿的。
    """
    problems: list[str] = []
    if len(actual) != len(expected):
        problems.append(f"行数不同: 实际 {len(actual)} != 期望 {len(expected)}")
    compared = 0
    for index, (got, want) in enumerate(zip(actual, expected)):
        for name, got_value, want_value in zip(names, got, want):
            compared += 1
            if got_value != want_value:
                problems.append(f"第 {index} 行 {name}: 实际 {got_value!r} != 期望 {want_value!r}")
    return problems, compared


class DiffHelperHasTeeth(unittest.TestCase):
    """先证明比较逻辑不是摆设。"""

    def test_diff_rows_reports_a_broken_cell(self) -> None:
        broken = [list(row) for row in GOLDEN_DESIGNS]
        broken[0][2] = "layout_em"
        problems, compared = diff_rows([tuple(r) for r in broken], GOLDEN_DESIGNS, DESIGN_FIELDS)
        self.assertEqual(compared, 20, "5 行 × 4 字段")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("view", problems[0])


# ==========================================================================
# parse_spec_mapping —— 关键测试（生成物 = 解析结果）
# ==========================================================================


class ParseSpecGolden(unittest.TestCase):
    def test_designs_expand_to_the_cartesian_product(self) -> None:
        parsed = spec.parse_spec_mapping(copy.deepcopy(GOLDEN_SPEC), source="t.yaml")
        self.assertEqual(len(parsed.designs), 1 * 2 * 2 + 1, "计数断言：library × cell × view + 第二条")
        problems, compared = diff_rows(design_rows(parsed), GOLDEN_DESIGNS, DESIGN_FIELDS)
        self.assertEqual(compared, 20, "计数断言：5 行 × 4 字段，diff 不许是空的")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_designs_expand_to_the_cartesian_product_negative(self) -> None:
        """反向：把 view 的列表砍成一个 ⇒ 展开出来的 design 少一半，比较逻辑必须报出来。"""
        broken = copy.deepcopy(GOLDEN_SPEC)
        broken["designs"][0]["view"] = ["layout"]
        parsed = spec.parse_spec_mapping(broken, source="t.yaml")
        self.assertEqual(len(parsed.designs), 3)
        problems, compared = diff_rows(design_rows(parsed), GOLDEN_DESIGNS, DESIGN_FIELDS)
        self.assertEqual(compared, 12, "只有 3 行能比 ⇒ 12 次比较（不是空过）")
        self.assertTrue(any("行数不同" in p for p in problems), "\n".join(problems))
        self.assertTrue(
            any("'layout_em'" in p for p in problems),
            "比较逻辑必须报出少掉的那个 view:\n" + "\n".join(problems),
        )

    def test_scalar_fields_and_overrides(self) -> None:
        parsed = spec.parse_spec_mapping(copy.deepcopy(GOLDEN_SPEC), source="t.yaml")
        self.assertEqual(parsed.batch_name, "demo")
        self.assertEqual(parsed.batch_root, "./batches")
        self.assertEqual(parsed.designs[0].official_run_dir, "OFFDIR_A")
        self.assertEqual(parsed.designs[0].resources, "cpu=20;mem=100000")
        self.assertEqual(parsed.designs[0].axis_overrides, {})
        self.assertEqual(parsed.designs[4].axis_overrides, {"temperature": ("25.0",)})
        self.assertEqual(parsed.options.max_parallel, 2)
        self.assertEqual(parsed.options.scheduler, "fake")
        self.assertEqual(parsed.options.poll_interval, BatchOptions().poll_interval, "没写的 option 用默认值")

    def test_axes_are_parsed_in_spec_order(self) -> None:
        parsed = spec.parse_spec_mapping(copy.deepcopy(GOLDEN_SPEC), source="t.yaml")
        self.assertEqual([a.name for a in parsed.axes], ["corner", "temperature"])
        self.assertEqual([v.value for v in parsed.axes[0].values], ["typical"])
        self.assertEqual([v.value for v in parsed.axes[1].values], ["-40.0", "125.0"])
        self.assertEqual(parsed.axes[0].flags, ("--corner", "--emssTechFile"), "corner 同时管两个 flag")
        self.assertTrue(parsed.axes[0].encoded_in_ewave_dir)


    def test_axes_are_parsed_in_spec_order_negative(self) -> None:
        """反向：spec 里两根轴换个位置 ⇒ 解析出来的顺序也换，slug 片段顺序跟着换。

        （顺序不是无所谓的：`compute_axes_slug` 按轴的顺序拼片段。）
        """
        swapped = copy.deepcopy(GOLDEN_SPEC)
        swapped["designs"] = swapped["designs"][:1]  # 第二条带 temperature 覆盖，这里不扫温度
        swapped["axes"] = {"equalCurrent": ["on", "off"], "fullWave": ["on", "off"]}
        parsed = spec.parse_spec_mapping(swapped, source="t.yaml")
        self.assertEqual([a.name for a in parsed.axes], ["equalCurrent", "fullWave"])
        first = matrix.expand_runs(parsed.designs[:1], parsed.axes)[0].axes_slug
        self.assertEqual(first, "eqI-on__fw-on")

        swapped["axes"] = {"fullWave": ["on", "off"], "equalCurrent": ["on", "off"]}
        parsed = spec.parse_spec_mapping(swapped, source="t.yaml")
        self.assertEqual([a.name for a in parsed.axes], ["fullWave", "equalCurrent"])
        first = matrix.expand_runs(parsed.designs[:1], parsed.axes)[0].axes_slug
        self.assertEqual(first, "fw-on__eqI-on", "顺序换了，slug 片段顺序也要跟着换")


class ScalarNormalisation(unittest.TestCase):
    """YAML 1.1 会把 `on`/`off` 读成 bool、把 `-40.0` 读成 float —— 都要还原成字符串取值。"""

    def test_yaml_booleans_become_on_off(self) -> None:
        data = {
            "designs": [{"library": "MY_LIB", "cell": "CELL_A", "view": "layout"}],
            # PyYAML 读 `equalCurrent: [on, off]` 得到的就是 [True, False]
            "axes": {"equalCurrent": [True, False]},
        }
        parsed = spec.parse_spec_mapping(data, source="t.yaml")
        self.assertEqual([v.value for v in parsed.axes[0].values], ["on", "off"])
        self.assertEqual(parsed.axes[0].values[0].flags, {"--equalCurrent": True})
        self.assertEqual(parsed.axes[0].values[1].flags, {"--equalCurrent": False})

    def test_yaml_booleans_become_on_off_negative(self) -> None:
        """反向：要是没还原，取值会变成 `True` / `False`，slug 就成了 `eqI-True`。"""
        data = {
            "designs": [{"library": "MY_LIB", "cell": "CELL_A", "view": "layout"}],
            "axes": {"equalCurrent": [True, False]},
        }
        parsed = spec.parse_spec_mapping(data, source="t.yaml")
        runs = matrix.expand_runs(parsed.designs, parsed.axes)
        self.assertEqual([r.axes_slug for r in runs], ["eqI-on", "eqI-off"])
        for run in runs:
            self.assertNotIn("True", run.axes_slug)

    def test_numbers_keep_their_written_form(self) -> None:
        data = {
            "designs": [{"library": "MY_LIB", "cell": "CELL_A", "view": "layout"}],
            "axes": {"temperature": [-40.0, 125.0], "relativeTolerance": [1e-05]},
        }
        parsed = spec.parse_spec_mapping(data, source="t.yaml")
        self.assertEqual([v.value for v in parsed.axes[0].values], ["-40.0", "125.0"])
        self.assertEqual([v.value for v in parsed.axes[1].values], ["1e-05"])

    def test_numbers_keep_their_written_form_negative(self) -> None:
        """反向：YAML 里写 `-40`（整数）就是 `-40`，不会被补成 `-40.0` ——
        目录名会跟着变，这个差异必须原样透出来而不是被工具"好心"修正。"""
        data = {
            "designs": [{"library": "MY_LIB", "cell": "CELL_A", "view": "layout"}],
            "axes": {"corner": ["typical"], "temperature": [-40]},
        }
        parsed = spec.parse_spec_mapping(data, source="t.yaml")
        self.assertEqual([v.value for v in parsed.axes[1].values], ["-40"])
        runs = matrix.expand_runs(parsed.designs, parsed.axes)
        self.assertEqual(runs[0].ewave_dir, "typical_-40")
        self.assertNotEqual(runs[0].ewave_dir, "typical_-40_0")


# ==========================================================================
# 自定义轴 / flag 表 / 端口
# ==========================================================================


class CustomAxes(unittest.TestCase):
    def test_custom_axis_with_one_flag(self) -> None:
        data = {
            "designs": [{"library": "MY_LIB", "cell": "CELL_A", "view": "layout"}],
            "axes": {"myKnob": {"flag": "--someFlag", "values": ["1", "2"], "short": "mk"}},
        }
        parsed = spec.parse_spec_mapping(data, source="t.yaml")
        axis = parsed.axes[0]
        self.assertEqual(axis.flags, ("--someFlag",))
        self.assertEqual([v.flags for v in axis.values], [{"--someFlag": "{value}"}] * 2)
        runs = matrix.expand_runs(parsed.designs, parsed.axes)
        self.assertEqual([r.axes_slug for r in runs], ["mk-1", "mk-2"])

    def test_custom_toggle_axis(self) -> None:
        data = {
            "designs": [{"library": "MY_LIB", "cell": "CELL_A", "view": "layout"}],
            "axes": {
                "myFlag": {"flag": "--someFlag", "kind": "toggle", "values": ["on", "off"]}
            },
        }
        parsed = spec.parse_spec_mapping(data, source="t.yaml")
        self.assertEqual(parsed.axes[0].kind, AxisKind.TOGGLE)
        self.assertEqual(
            [v.flags for v in parsed.axes[0].values],
            [{"--someFlag": True}, {"--someFlag": False}],
        )

    def test_unknown_axis_name_is_rejected_with_the_catalog(self) -> None:
        data = {
            "designs": [{"library": "MY_LIB", "cell": "CELL_A", "view": "layout"}],
            "axes": {"conrer": ["typical"]},
        }
        with self.assertRaises(SpecError) as ctx:
            spec.parse_spec_mapping(data, source="t.yaml")
        message = str(ctx.exception)
        self.assertIn("conrer", message)
        self.assertIn("corner", message, "报错要把内置轴名列出来")
        self.assertIn("flag:", message, "报错要给出「自定义轴」这条出路")

    def test_builtin_encoded_axis_cannot_be_redefined(self) -> None:
        """corner 的 flag 不许自己定义：它还要改 `--emssTechFile` 的 ptxt 文件名（BRIEF §7）。"""
        data = {
            "designs": [{"library": "MY_LIB", "cell": "CELL_A", "view": "layout"}],
            "axes": {"corner": {"flag": "--corner", "values": ["typical"]}},
        }
        with self.assertRaises(SpecError) as ctx:
            spec.parse_spec_mapping(data, source="t.yaml")
        self.assertIn("emssTechFile", str(ctx.exception))


class FlagTables(unittest.TestCase):
    def base(self, **extra) -> dict:
        data = {"designs": [{"library": "MY_LIB", "cell": "CELL_A", "view": "layout"}]}
        data.update(extra)
        return data

    def test_extra_flags_as_one_line(self) -> None:
        parsed = spec.parse_spec_mapping(
            self.base(extra_flags="--labelDepth=0 -e 0.4 --printDouble"), source="t.yaml"
        )
        self.assertEqual(
            parsed.extra_flags, {"--labelDepth": "0", "-e": "0.4", "--printDouble": True}
        )

    def test_extra_flags_as_mapping(self) -> None:
        parsed = spec.parse_spec_mapping(
            self.base(extra_flags={"--labelDepth": "0", "--equalCurrent": False}), source="t.yaml"
        )
        self.assertEqual(parsed.extra_flags, {"--labelDepth": "0", "--equalCurrent": False})
        self.assertIs(parsed.extra_flags["--equalCurrent"], False, "False = 显式缺席，不是没有")

    def test_negative_number_is_a_value_not_a_flag(self) -> None:
        """`-e 0.4` 里的值和 `-40.0` 这种负数不许被当成 flag 名。"""
        parsed = spec.parse_spec_mapping(
            self.base(extra_flags="--someFlag -40.0"), source="t.yaml"
        )
        self.assertEqual(parsed.extra_flags, {"--someFlag": True, "-40.0": True})

    def test_mechanism_flag_is_rejected(self) -> None:
        for flag in ("--workDir", "--all", "--gds", "--sparam", "-m"):
            with self.subTest(flag=flag):
                with self.assertRaises(FlagConflictError) as ctx:
                    spec.parse_spec_mapping(self.base(extra_flags={flag: "x"}), source="t.yaml")
                self.assertIn(flag, str(ctx.exception))

    def test_axis_owned_flag_is_rejected(self) -> None:
        """§11 规则 2：Extra flags 里出现已经是轴的 flag ⇒ 目录名会和实际跑的值对不上。"""
        data = self.base(axes={"temperature": ["-40.0", "125.0"]}, extra_flags="--temperature=85")
        with self.assertRaises(FlagConflictError) as ctx:
            spec.parse_spec_mapping(data, source="t.yaml")
        message = str(ctx.exception)
        self.assertIn("--temperature", message)
        self.assertIn("temperature", message)

    def test_axis_owned_flag_is_rejected_negative(self) -> None:
        """反向（过滤器不许多吃）：`--temperature` 是轴，`--temperatureSweep` 不是，必须放行。

        这条对应 MVP 那个真 bug：`--sparam` 前缀误伤 `--sparamImpedance`。
        """
        data = self.base(
            axes={"temperature": ["-40.0", "125.0"]}, extra_flags="--temperatureSweep=1"
        )
        parsed = spec.parse_spec_mapping(data, source="t.yaml")
        self.assertEqual(parsed.extra_flags, {"--temperatureSweep": "1"})

    def test_corner_axis_also_owns_emss_tech_file(self) -> None:
        data = self.base(axes={"corner": ["typical"]}, extra_flags={"--emssTechFile": "x"})
        with self.assertRaises(FlagConflictError):
            spec.parse_spec_mapping(data, source="t.yaml")

    def test_flag_without_dash_is_rejected(self) -> None:
        with self.assertRaises(SpecError) as ctx:
            spec.parse_spec_mapping(self.base(extra_flags={"labelDepth": "0"}), source="t.yaml")
        self.assertIn("dash", str(ctx.exception))


class Ports(unittest.TestCase):
    def test_explicit_ports_keep_their_order(self) -> None:
        """顺序就是映射本身（`.sNp` 里只留 P00x 编号）—— 解析绝不许排序。"""
        data = {
            "designs": [
                {
                    "library": "MY_LIB",
                    "cell": "CELL_A",
                    "view": "layout",
                    "ports": {"mapping": ["P000=PIN_B", "P001=PIN_A"], "signal": ["PIN_A"]},
                }
            ]
        }
        parsed = spec.parse_spec_mapping(data, source="t.yaml")
        port_spec = parsed.designs[0].port_spec
        self.assertIsNotNone(port_spec)
        assert port_spec is not None
        self.assertEqual(port_spec.mode, PortMode.EXPLICIT)
        self.assertEqual(port_spec.mapping, (("P000", "PIN_B"), ("P001", "PIN_A")))
        self.assertEqual(port_spec.signal_ports, ("PIN_A",))

    def test_no_ports_means_all(self) -> None:
        data = {"designs": [{"library": "MY_LIB", "cell": "CELL_A", "view": "layout"}]}
        parsed = spec.parse_spec_mapping(data, source="t.yaml")
        self.assertIsNone(parsed.designs[0].port_spec, "None = 用 --all（D1b 的默认）")

    def test_bad_mapping_shape(self) -> None:
        data = {
            "designs": [
                {
                    "library": "MY_LIB",
                    "cell": "CELL_A",
                    "view": "layout",
                    "ports": {"mapping": ["P000 PIN_B"]},
                }
            ]
        }
        with self.assertRaises(SpecError) as ctx:
            spec.parse_spec_mapping(data, source="t.yaml")
        self.assertIn("P000", str(ctx.exception))


# ==========================================================================
# 报错的质量（用户范围是"先自己用，后面给同事" ⇒ 错误必须带下一步怎么办）
# ==========================================================================


class ErrorMessages(unittest.TestCase):
    def assert_actionable(self, message: str) -> None:
        self.assertIn("Next:", message, f"报错里必须有「下一步怎么办」:\n{message}")

    def test_missing_designs(self) -> None:
        with self.assertRaises(SpecError) as ctx:
            spec.parse_spec_mapping({"axes": {"corner": ["typical"]}}, source="my_spec.yaml")
        message = str(ctx.exception)
        self.assertIn("my_spec.yaml", message, "报错要说清是哪份 spec")
        self.assertIn("designs", message)
        self.assertIn("library", message, "报错要带一段能照抄的例子")
        self.assert_actionable(message)

    def test_unknown_top_level_key(self) -> None:
        data = {
            "designs": [{"library": "MY_LIB", "cell": "CELL_A", "view": "layout"}],
            "desings": [],
        }
        with self.assertRaises(SpecError) as ctx:
            spec.parse_spec_mapping(data, source="t.yaml")
        message = str(ctx.exception)
        self.assertIn("desings", message)
        self.assertIn("designs", message, "报错要把合法的字段列出来，好让人看出是拼错了")
        self.assert_actionable(message)

    def test_unknown_design_key(self) -> None:
        data = {"designs": [{"library": "MY_LIB", "cell": "CELL_A", "view": "layout", "veiw": "x"}]}
        with self.assertRaises(SpecError) as ctx:
            spec.parse_spec_mapping(data, source="t.yaml")
        self.assertIn("designs[0]", str(ctx.exception), "报错要定位到哪一条 design")
        self.assertIn("veiw", str(ctx.exception))

    def test_missing_view(self) -> None:
        data = {"designs": [{"library": "MY_LIB", "cell": "CELL_A"}]}
        with self.assertRaises(SpecError) as ctx:
            spec.parse_spec_mapping(data, source="t.yaml")
        message = str(ctx.exception)
        self.assertIn("view", message)
        self.assertIn("designs[0]", message)
        self.assert_actionable(message)

    def test_unknown_option(self) -> None:
        data = {
            "designs": [{"library": "MY_LIB", "cell": "CELL_A", "view": "layout"}],
            "options": {"max_paralel": 4},
        }
        with self.assertRaises(SpecError) as ctx:
            spec.parse_spec_mapping(data, source="t.yaml")
        self.assertIn("max_paralel", str(ctx.exception))
        self.assertIn("max_parallel", str(ctx.exception))

    def test_per_design_override_of_undefined_axis(self) -> None:
        data = {
            "designs": [
                {
                    "library": "MY_LIB",
                    "cell": "CELL_A",
                    "view": "layout",
                    "axes": {"fullWave": ["on"]},
                }
            ],
            "axes": {"corner": ["typical"]},
        }
        with self.assertRaises(SpecError) as ctx:
            spec.parse_spec_mapping(data, source="t.yaml")
        message = str(ctx.exception)
        self.assertIn("fullWave", message)
        self.assert_actionable(message)

    def test_explicit_key_plus_expansion(self) -> None:
        data = {
            "designs": [
                {"library": "MY_LIB", "cell": ["CELL_A", "CELL_B"], "view": "layout", "key": "k"}
            ]
        }
        with self.assertRaises(SpecError) as ctx:
            spec.parse_spec_mapping(data, source="t.yaml")
        self.assertIn("key", str(ctx.exception))


# ==========================================================================
# spec_to_batch
# ==========================================================================


class SpecToBatch(unittest.TestCase):
    def parse(self, data: dict | None = None):
        return spec.parse_spec_mapping(copy.deepcopy(data or GOLDEN_SPEC), source="t.yaml")

    def test_runs_and_layout(self) -> None:
        state = spec.spec_to_batch(self.parse(), batch_root="", tool_version="9.9-test")
        self.assertEqual(len(state.runs), 4 * 2 + 1, "计数断言：4 个 design × 2 个温度 + 1 个被覆盖的")
        problems, compared = diff_rows(
            [(run.run_id,) for run in state.runs],
            tuple((run_id,) for run_id in GOLDEN_RUN_IDS),
            ("run_id",),
        )
        self.assertEqual(compared, 9, "计数断言：9 行 × 1 字段")
        self.assertEqual(problems, [], "\n".join(problems))
        self.assertEqual(len({r.run_id for r in state.runs}), 9, "run_id 撞了就是静默覆盖")

    def test_runs_and_layout_negative(self) -> None:
        """反向：温度写成 `-40`（少了小数位）⇒ eWave 建的目录名变了，比较逻辑必须报出来。"""
        broken = copy.deepcopy(GOLDEN_SPEC)
        broken["axes"]["temperature"] = ["-40", "125.0"]
        state = spec.spec_to_batch(self.parse(broken), batch_root="")
        problems, compared = diff_rows(
            [(run.run_id,) for run in state.runs],
            tuple((run_id,) for run_id in GOLDEN_RUN_IDS),
            ("run_id",),
        )
        self.assertEqual(compared, 9, "比较逻辑仍然比了 9 行（不是空过）")
        self.assertEqual(len(problems), 4, "4 个 design 各有一个 -40 的 run 变了名")
        self.assertTrue(
            any("typical_-40'" in p and "typical_-40_0'" in p for p in problems),
            "\n".join(problems),
        )

    def test_state_metadata(self) -> None:
        state = spec.spec_to_batch(self.parse(), batch_root="", tool_version="9.9-test")
        self.assertEqual(state.batch_name, "demo")
        self.assertTrue(os.path.isabs(state.batch_dir), "batch_dir 要是绝对路径")
        self.assertEqual(os.path.basename(state.batch_dir), "demo")
        self.assertEqual(os.path.basename(os.path.dirname(state.batch_dir)), "batches")
        self.assertEqual(state.provenance.tool_version, "9.9-test")
        self.assertEqual(state.provenance.official_run_dirs, ("OFFDIR_A",))
        self.assertTrue(state.provenance.created_at.endswith("Z"), "时间戳落 UTC")
        self.assertEqual(len(state.streamout), 5, "阶段 1 每个 design 一格")
        self.assertTrue(all(run.status is RunStatus.READY for run in state.runs))
        self.assertTrue(all(task.status is RunStatus.READY for task in state.streamout))

    def test_batch_root_argument_wins(self) -> None:
        state = spec.spec_to_batch(self.parse(), batch_root=os.path.join(".", "elsewhere"))
        self.assertEqual(os.path.basename(os.path.dirname(state.batch_dir)), "elsewhere")

    def test_gds_path_marks_streamout_done_not_skipped(self) -> None:
        """spec 直接给了 GDS ⇒ 阶段 1 的产物本来就在。

        标 SKIPPED 会被 driver 读成"这个 design 整列不跑"（阶段 1 失败的语义），
        把整批 solve 静默吞掉 —— 所以标 DONE。
        """
        data = copy.deepcopy(GOLDEN_SPEC)
        data["designs"] = [
            {"library": "MY_LIB", "cell": "CELL_A", "view": "layout", "gds_path": "pre.gds"}
        ]
        state = spec.spec_to_batch(self.parse(data), batch_root="")
        self.assertEqual(state.streamout[0].status, RunStatus.DONE)
        self.assertEqual(state.streamout[0].gds_path, "pre.gds")
        self.assertNotEqual(state.streamout[0].status, RunStatus.SKIPPED)

    def test_missing_batch_root(self) -> None:
        data = copy.deepcopy(GOLDEN_SPEC)
        data.pop("batch_root")
        with self.assertRaises(SpecError) as ctx:
            spec.spec_to_batch(self.parse(data), batch_root="")
        self.assertIn("batch_root", str(ctx.exception))

    def test_generated_batch_name(self) -> None:
        data = copy.deepcopy(GOLDEN_SPEC)
        data.pop("batch_name")
        state = spec.spec_to_batch(self.parse(data), batch_root="./batches")
        self.assertRegex(state.batch_name, r"^batch_\d{8}_\d{6}$")


# ==========================================================================
# spec_sha256 —— 期望值是 NIST 的 SHA-256 测试向量（外部基准，与本实现无关）
# ==========================================================================


class SpecSha256(unittest.TestCase):
    NIST_ABC = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    NIST_EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    def write(self, tmp: str, name: str, payload: bytes) -> str:
        path = os.path.join(tmp, name)
        with open(path, "wb") as handle:
            handle.write(payload)
        return path

    def test_matches_the_nist_vector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(spec.spec_sha256(self.write(tmp, "a.json", b"abc")), self.NIST_ABC)
            self.assertEqual(spec.spec_sha256(self.write(tmp, "b.json", b"")), self.NIST_EMPTY)

    def test_matches_the_nist_vector_negative(self) -> None:
        """反向：改一个字节（`abc` → `abd`）必须换一个 hash。"""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertNotEqual(spec.spec_sha256(self.write(tmp, "c.json", b"abd")), self.NIST_ABC)

    def test_lands_in_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, "s.json", json.dumps(GOLDEN_SPEC).encode("utf-8"))
            parsed = spec.load_spec(path)
            self.assertEqual(parsed.source_sha256, spec.spec_sha256(path))
            state = spec.spec_to_batch(parsed, batch_root="./batches")
            self.assertEqual(state.provenance.spec_sha256, parsed.source_sha256)
            self.assertEqual(state.provenance.spec_path, path)


# ==========================================================================
# YAML 惰性 import + JSON 退路（CLAUDE.md 硬约束 2）
# ==========================================================================


class _FakeYaml:
    """假的 `yaml` 模块 —— 本机没装 PyYAML，靠它验"我们调的是 safe_load"。"""

    def __init__(self, result: object) -> None:
        self.result = result
        self.seen: list[str] = []

    def safe_load(self, text: str) -> object:
        self.seen.append(text)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def load(self, *args: object, **kwargs: object) -> object:  # pragma: no cover - 不许被调到
        raise AssertionError("spec 是人手写的文本，只准 yaml.safe_load，不许 yaml.load")


class LazyYamlImport(unittest.TestCase):
    def write(self, tmp: str, name: str, text: str) -> str:
        path = os.path.join(tmp, name)
        with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        return path

    def test_module_does_not_import_yaml_at_top_level(self) -> None:
        with io.open(spec.__file__, encoding="utf-8") as handle:
            source = handle.read()
        offenders = [
            line
            for line in source.splitlines()
            if line.startswith("import yaml") or line.startswith("from yaml")
        ]
        self.assertEqual(offenders, [], "顶层 import yaml 会让没装 PyYAML 的机器上 CLI 直接死")
        self.assertIn("import yaml", source, "惰性 import 本身还是要在（在函数体里）")

    def test_importing_spec_does_not_pull_in_yaml(self) -> None:
        """起一个干净的解释器 import 一遍 —— 在**装了** PyYAML 的红区也必须成立。"""
        probe = (
            "import sys; import ewave_batch.core.spec; "
            "sys.exit(1 if 'yaml' in sys.modules else 0)"
        )
        proc = subprocess.run([sys.executable, "-c", probe], cwd=str(ROOT), capture_output=True)
        self.assertEqual(
            proc.returncode,
            0,
            "import ewave_batch.core.spec 时就把 yaml 拉进来了 —— 惰性 import 没做到\n"
            + proc.stderr.decode("utf-8", "replace"),
        )

    def test_yaml_path_uses_safe_load(self) -> None:
        fake = _FakeYaml({"designs": [{"library": "MY_LIB", "cell": "CELL_A", "view": "layout"}]})
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, "s.yaml", "designs: [...]\n")
            with mock.patch.dict(sys.modules, {"yaml": fake}):
                parsed = spec.load_spec(path)
        self.assertEqual(fake.seen, ["designs: [...]\n"], "整份文本原样交给 safe_load")
        self.assertEqual(parsed.designs[0].cell, "CELL_A")

    def test_yaml_syntax_error_becomes_a_speerror(self) -> None:
        fake = _FakeYaml(ValueError("mapping values are not allowed here (line 3)"))
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, "s.yaml", "designs:\n  - library: x: y\n")
            with mock.patch.dict(sys.modules, {"yaml": fake}):
                with self.assertRaises(SpecError) as ctx:
                    spec.load_spec(path)
        message = str(ctx.exception)
        self.assertIn("line 3", message, "解析器给的行号要透出来")
        self.assertIn("Next:", message)

    def test_json_spec_works_without_pyyaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, "s.json", json.dumps(GOLDEN_SPEC))
            with mock.patch.dict(sys.modules, {"yaml": None}):  # import yaml → ImportError
                parsed = spec.load_spec(path)
        self.assertEqual(len(parsed.designs), 5)
        self.assertEqual(parsed.source_path, path)

    def test_json_content_in_a_yaml_file_still_loads(self) -> None:
        """JSON 是 YAML 的子集 —— 没装 PyYAML 时这条退路让 `.yaml` 也能用。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, "s.yaml", json.dumps(GOLDEN_SPEC))
            with mock.patch.dict(sys.modules, {"yaml": None}):
                parsed = spec.load_spec(path)
        self.assertEqual(len(parsed.designs), 5)

    def test_real_yaml_without_pyyaml_says_what_to_do(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, "s.yaml", "designs:\n  - library: MY_LIB\n")
            with mock.patch.dict(sys.modules, {"yaml": None}):
                with self.assertRaises(SpecError) as ctx:
                    spec.load_spec(path)
        message = str(ctx.exception)
        self.assertIn("PyYAML", message)
        self.assertIn("JSON", message, "要告诉用户可以改用 JSON spec")
        self.assertIn("Next", message)

    def test_missing_file(self) -> None:
        with self.assertRaises(SpecError) as ctx:
            spec.load_spec(os.path.join("no", "such", "spec.yaml"))
        self.assertIn("not found", str(ctx.exception))

    def test_bad_json_reports_the_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, "s.json", '{\n  "designs": [},\n}\n')
            with self.assertRaises(SpecError) as ctx:
                spec.load_spec(path)
        self.assertIn("line", str(ctx.exception))

    def test_top_level_list_is_treated_as_designs(self) -> None:
        """抄 tasks.yaml 的手感：顶层直接写一个 design 列表也认。"""
        payload = [{"library": "MY_LIB", "cell": "CELL_A", "view": "layout"}]
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, "s.json", json.dumps(payload))
            parsed = spec.load_spec(path)
        self.assertEqual(len(parsed.designs), 1)


# ==========================================================================
# groups —— base 之上的 run group（用户 2026-08-19 拍板）
# ==========================================================================


class Groups(unittest.TestCase):
    def base(self, **extra):
        data = {
            "designs": [{"library": "MY_LIB", "cell": "CELL_A", "view": "layout"}],
            "axes": {
                "corner": ["typical"],
                "temperature": ["-40.0", "55.0", "125.0"],
                "fullWave": ["off"],
                "equalCurrent": ["on"],
            },
        }
        data.update(extra)
        return data

    PROTOTYPE = [
        {"name": "eqcur-off", "axes": {"temperature": ["55.0"], "equalCurrent": ["off"]}},
        {"name": "fullwave", "axes": {"temperature": ["55.0"], "fullWave": ["on"]}},
    ]

    def test_prototype_gives_five_runs(self) -> None:
        """契约里那个原型：3 + 1 + 1 = 5。笛卡尔积最接近的写法是 12 个，7 个是废的。"""
        parsed = spec.parse_spec_mapping(self.base(groups=self.PROTOTYPE), source="t.yaml")
        self.assertEqual([g.name for g in parsed.groups], ["eqcur-off", "fullwave"])
        runs = matrix.expand_runs(
            parsed.designs, parsed.axes, options=parsed.options, groups=parsed.groups
        )
        self.assertEqual(len(runs), 5)
        self.assertEqual(
            [r.axes_slug for r in runs],
            [
                "fw-off__eqI-on",
                "fw-off__eqI-on",
                "fw-off__eqI-on",
                "fw-off__eqI-off",
                "fw-on__eqI-on",
            ],
            "加了组之后 fullWave/equalCurrent 全批次在变 ⇒ 它们对所有 run 进 slug",
        )

    def test_no_groups_key_keeps_the_old_behaviour(self) -> None:
        """反向：不写 groups: ⇒ 只有 base ⇒ 两根单取值的轴不进 slug。"""
        parsed = spec.parse_spec_mapping(self.base(), source="t.yaml")
        self.assertEqual(parsed.groups, [])
        runs = matrix.expand_runs(parsed.designs, parsed.axes, groups=parsed.groups)
        self.assertEqual(len(runs), 3)
        self.assertEqual({r.axes_slug for r in runs}, {"base"})

    def test_spec_to_batch_carries_groups_into_the_state(self) -> None:
        parsed = spec.parse_spec_mapping(self.base(groups=self.PROTOTYPE), source="t.yaml")
        state = spec.spec_to_batch(parsed, batch_root="/tmp/ewb")
        self.assertEqual([g.name for g in state.groups], ["eqcur-off", "fullwave"])
        self.assertEqual(len(state.runs), 5)
        self.assertEqual(
            sorted({r.group for r in state.runs}), ["base", "eqcur-off", "fullwave"]
        )

    def test_group_named_base_is_merged_into_the_top_level_axes(self) -> None:
        """`name: base` 指的就是顶层 axes:，合并进去而不是新建一个组。"""
        data = self.base(groups=[{"name": "base", "axes": {"temperature": ["55.0"]}}])
        parsed = spec.parse_spec_mapping(data, source="t.yaml")
        self.assertEqual(parsed.groups, [], "base 不该变成一个独立的组")
        temperature = [a for a in parsed.axes if a.name == "temperature"][0]
        self.assertEqual([v.value for v in temperature.values], ["55.0"])

    def test_group_overriding_an_undefined_axis(self) -> None:
        data = self.base(groups=[{"name": "oops", "axes": {"conrer": ["cbest"]}}])
        with self.assertRaises(SpecError) as ctx:
            spec.parse_spec_mapping(data, source="t.yaml")
        message = str(ctx.exception)
        self.assertIn("conrer", message)
        self.assertIn("oops", message)
        self.assertIn("Next:", message)

    def test_group_without_a_name(self) -> None:
        data = self.base(groups=[{"axes": {"temperature": ["55.0"]}}])
        with self.assertRaises(SpecError):
            spec.parse_spec_mapping(data, source="t.yaml")

    def test_group_that_overrides_nothing_is_rejected(self) -> None:
        """空 delta 的组展开出来和 base 一模一样，会被去重整组吃掉 —— 看着像"没生效"。"""
        data = self.base(groups=[{"name": "empty"}])
        with self.assertRaises(SpecError) as ctx:
            spec.parse_spec_mapping(data, source="t.yaml")
        self.assertIn("Next:", str(ctx.exception))

    def test_duplicate_group_names(self) -> None:
        data = self.base(
            groups=[
                {"name": "same", "axes": {"temperature": ["55.0"]}},
                {"name": "same", "axes": {"temperature": ["125.0"]}},
            ]
        )
        with self.assertRaises(SpecError):
            spec.parse_spec_mapping(data, source="t.yaml")

    def test_groups_must_be_a_list(self) -> None:
        data = self.base(groups={"eqcur-off": {"equalCurrent": ["off"]}})
        with self.assertRaises(SpecError) as ctx:
            spec.parse_spec_mapping(data, source="t.yaml")
        self.assertIn("list", str(ctx.exception))

    def test_round_trip_through_a_real_file(self) -> None:
        """★ 手写文件 -> `load_spec` -> `save_spec` -> `load_spec`：组一个字不变。

        `tests/test_spec_dump.py` 那条往返的起点是**代码里造的** `BatchSpec`，
        走的只有「序列化 <-> 反序列化」这一段。这条的起点是**磁盘上一份人写的 spec**，
        补上的正是"用户手写的 `groups:` 段读得进来吗"那一截 —— GUI 的
        「Open spec…」+「Save spec as…」连起来就是这条路，中间掉一节就是
        "用户配了、存了、下次打开没了"，而且无声。

        文件后缀故意用 `.json`：本机没有 PyYAML，`.json` 那条路在**哪台机器上都一样**
        （`load_spec` 对 `.yaml` 的 PyYAML 缺失有专门的报错，那是另一条测试的事）。
        """
        payload = {
            "designs": [{"library": "MY_LIB", "cell": "CELL_A", "view": "layout"}],
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
                    "label": "one-off: equalCurrent off at 55C",
                },
                {"name": "fullwave", "axes": {"temperature": ["55.0"], "fullWave": ["on"]}},
            ],
        }
        expected = [
            # 手写字面量，不从 payload 现算 —— 现算的话"解析时把 axes 段丢了"照样绿。
            ("eqcur-off", "one-off: equalCurrent off at 55C",
             {"temperature": ("55.0",), "equalCurrent": ("off",)}),
            ("fullwave", "", {"temperature": ("55.0",), "fullWave": ("on",)}),
        ]

        def shape(parsed):
            return [
                (g.name, g.label, {k: tuple(v) for k, v in g.axis_overrides.items()})
                for g in parsed.groups
            ]

        with tempfile.TemporaryDirectory() as tmp:
            first_path = os.path.join(tmp, "hand_written.json")
            with io.open(first_path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, indent=2) + "\n")

            first = spec.load_spec(first_path)
            self.assertEqual(shape(first), expected, "人写的 groups: 段没被原样读进来")

            saved = spec.save_spec(first, os.path.join(tmp, "saved.json"))
            second = spec.load_spec(saved)

        self.assertEqual(shape(second), shape(first), "存一遍再读回来，组变了")
        self.assertEqual(shape(second), expected)
        # 计数断言：2 个组 x 3 个字段 = 6 个值真的被比过（空列表的 diff 永远好看）。
        self.assertEqual(sum(1 + 1 + len(o) for _n, _l, o in shape(second)), 8)
        # 组还得真的展开成 5 个 run —— 往返之后"组还在但覆盖空了"同样是坏的。
        self.assertEqual(
            len(matrix.expand_runs(second.designs, second.axes, groups=second.groups)), 5
        )

    def test_unknown_key_inside_a_group(self) -> None:
        data = self.base(groups=[{"name": "x", "axess": {"temperature": ["55.0"]}}])
        with self.assertRaises(SpecError) as ctx:
            spec.parse_spec_mapping(data, source="t.yaml")
        self.assertIn("axess", str(ctx.exception))


# ==========================================================================
# EXAMPLE_SPEC ↔ docs/spec_example.yaml
# ==========================================================================


class ExampleSpec(unittest.TestCase):
    def read_example(self) -> str:
        with io.open(EXAMPLE_PATH, encoding="utf-8", newline="") as handle:
            return handle.read()

    def test_file_and_constant_are_identical(self) -> None:
        """两份不许各改各的 —— `EXAMPLE_SPEC` 进包、`docs/spec_example.yaml` 给人看。"""
        self.assertTrue(EXAMPLE_PATH.is_file(), f"{EXAMPLE_PATH} 不见了")
        self.assertEqual(self.read_example(), spec.EXAMPLE_SPEC)

    def test_file_is_lf_only(self) -> None:
        """`.gitattributes` 把 `.sh` 钉成 LF；spec 样例要被红区的 python 读，CRLF 不该混进来。"""
        raw = EXAMPLE_PATH.read_bytes()
        self.assertNotIn(b"\r", raw, "CRLF 混进来了")
        self.assertTrue(raw.endswith(b"\n"))

    def test_only_placeholders_no_site_coordinates(self) -> None:
        text = spec.EXAMPLE_SPEC
        for placeholder in ("<lib>", "<cellA>", "<view>"):
            self.assertIn(placeholder, text)
        self.assertNotIn("/home/", text)
        self.assertNotIn("/proj/", text)
        self.assertNotIn("/data/", text)

    def test_structure_lint(self) -> None:
        """本机没装 PyYAML，解析不了 —— 至少把最容易犯的结构错误挡住（tab / 奇数缩进）。"""
        for number, line in enumerate(spec.EXAMPLE_SPEC.splitlines(), start=1):
            with self.subTest(line=number):
                self.assertNotIn("\t", line, f"第 {number} 行有 tab —— YAML 不许用 tab 缩进")
                stripped = line.lstrip(" ")
                if not stripped or stripped.startswith("#"):
                    continue
                indent = len(line) - len(stripped)
                self.assertEqual(indent % 2, 0, f"第 {number} 行缩进 {indent} 不是 2 的倍数")
                head = stripped[2:] if stripped.startswith("- ") else stripped
                self.assertRegex(
                    head,
                    r"^[A-Za-z_][A-Za-z0-9_-]*:",
                    f"第 {number} 行既不是 key: 也不是列表项",
                )

    @unittest.skipUnless(
        _has_pyyaml(),
        "本机没装 PyYAML（红区装了 6.0.1）—— 真 YAML 解析只能在红区验，"
        "结构由 test_structure_lint 兜底",
    )
    def test_example_spec_parses(self) -> None:  # pragma: no cover - 本机跳过
        parsed = spec.load_spec(str(EXAMPLE_PATH))
        self.assertEqual(len(parsed.designs), 3, "2 个 cell + 1 条 = 3 个 design")
        self.assertEqual([a.name for a in parsed.axes], ["corner", "temperature", "equalCurrent"])
        state = spec.spec_to_batch(parsed, batch_root="")
        self.assertEqual(
            len(state.runs),
            2 * (2 * 2) + 1 * (1 * 2),
            "前 2 个 design 各 2 温度 × 2 eqI，第 3 个被覆盖成 1 个温度",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
