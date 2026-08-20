"""`ewave_batch.core.matrix` 的测试 —— 矩阵展开 / slug / varying 轴。

**期望值全部是手写字面量**（防自证配方 2）。本机没有 eWave，也没有一份"矩阵展开"的
golden fixture 可抄 —— 唯一的证据是设计权威 `PROJECT_BRIEF.md`，所以每张期望表上面
都写清了它是从哪一节抄来的：

* `<corner>_<temp>` 那层目录名、温度的小数点换下划线（`-40.0` → `-40_0`，`125.0` → `125_0`）
  —— §5「官方流程的既有布局（✅ 实测，2026-08-17）」，那一段是从真实红区目录抄回来的。
* `base` / `eqI-on__fw-off` / `__` 双下划线做轴分隔 —— §5「归档布局（提案）」。
* design 目录名 = `<library>_<topCell>_<view>` —— §5「官方流程的既有布局」。
* 「只有一个取值的轴不进 slug」 —— §5「归档布局」+ `docs/INTERFACES.md`。

期望值**不许**在测试里现算：下面没有一处循环去拼期望的目录名，全是一行一行敲出来的。

站点标识符零出现：库/cell 名一律是 `MY_LIB` / `CELL_A` 这种编出来的占位符；
`typical` / `cbest` / `-40.0` 是 eWave 的工具语义（BRIEF §10 明确列出的 5 个通用工艺角），
不是站点身份。
"""

from __future__ import annotations

import unittest

from ewave_batch.core import matrix
from ewave_batch.model import (
    BASE_GROUP,
    BASE_SLUG,
    Axis,
    AxisKind,
    AxisValue,
    BatchOptions,
    Design,
    RunGroup,
    SpecError,
)

# ==========================================================================
# 期望表（手写字面量）
# ==========================================================================

# 2 design × 2 corner × 2 temp = 8 个 run。
# 只扫 corner 和 temperature ⇒ 这两根轴都被 eWave 自己编进 `<corner>_<temp>` 了
# ⇒ `<axes-slug>` 里一根轴都不剩 ⇒ 全部是 `base`（BRIEF §5「没有额外轴时用 base」）。
# 目录名逐字复现官方：`typical_-40_0`（§5 归档布局第一行）。
GOLDEN_BASE: tuple[tuple[str, str, str], ...] = (
    # (run_id, axes_slug, ewave_dir)
    ("MY_LIB_CELL_A_layout/base/typical_-40_0", "base", "typical_-40_0"),
    ("MY_LIB_CELL_A_layout/base/typical_125_0", "base", "typical_125_0"),
    ("MY_LIB_CELL_A_layout/base/cbest_-40_0", "base", "cbest_-40_0"),
    ("MY_LIB_CELL_A_layout/base/cbest_125_0", "base", "cbest_125_0"),
    ("MY_LIB_CELL_B_layout/base/typical_-40_0", "base", "typical_-40_0"),
    ("MY_LIB_CELL_B_layout/base/typical_125_0", "base", "typical_125_0"),
    ("MY_LIB_CELL_B_layout/base/cbest_-40_0", "base", "cbest_-40_0"),
    ("MY_LIB_CELL_B_layout/base/cbest_125_0", "base", "cbest_125_0"),
)

# 1 design × 2 corner × 2 temp × 2 equalCurrent × 2 fullWave = 16 个 run。
# 额外的两根轴进 `<axes-slug>`，写法逐字照 BRIEF §5 的例子 `eqI-on__fw-off`：
# 轴的短名 + `-` + 取值，轴之间 `__` 双下划线（单下划线已被温度占用）。
# 片段顺序 = 轴在 spec 里的顺序（equalCurrent 在 fullWave 前面）。
GOLDEN_MULTI: tuple[tuple[str, str, str], ...] = (
    ("MY_LIB_CELL_A_layout/eqI-on__fw-on/typical_-40_0", "eqI-on__fw-on", "typical_-40_0"),
    ("MY_LIB_CELL_A_layout/eqI-on__fw-off/typical_-40_0", "eqI-on__fw-off", "typical_-40_0"),
    ("MY_LIB_CELL_A_layout/eqI-off__fw-on/typical_-40_0", "eqI-off__fw-on", "typical_-40_0"),
    ("MY_LIB_CELL_A_layout/eqI-off__fw-off/typical_-40_0", "eqI-off__fw-off", "typical_-40_0"),
    ("MY_LIB_CELL_A_layout/eqI-on__fw-on/typical_125_0", "eqI-on__fw-on", "typical_125_0"),
    ("MY_LIB_CELL_A_layout/eqI-on__fw-off/typical_125_0", "eqI-on__fw-off", "typical_125_0"),
    ("MY_LIB_CELL_A_layout/eqI-off__fw-on/typical_125_0", "eqI-off__fw-on", "typical_125_0"),
    ("MY_LIB_CELL_A_layout/eqI-off__fw-off/typical_125_0", "eqI-off__fw-off", "typical_125_0"),
    ("MY_LIB_CELL_A_layout/eqI-on__fw-on/cbest_-40_0", "eqI-on__fw-on", "cbest_-40_0"),
    ("MY_LIB_CELL_A_layout/eqI-on__fw-off/cbest_-40_0", "eqI-on__fw-off", "cbest_-40_0"),
    ("MY_LIB_CELL_A_layout/eqI-off__fw-on/cbest_-40_0", "eqI-off__fw-on", "cbest_-40_0"),
    ("MY_LIB_CELL_A_layout/eqI-off__fw-off/cbest_-40_0", "eqI-off__fw-off", "cbest_-40_0"),
    ("MY_LIB_CELL_A_layout/eqI-on__fw-on/cbest_125_0", "eqI-on__fw-on", "cbest_125_0"),
    ("MY_LIB_CELL_A_layout/eqI-on__fw-off/cbest_125_0", "eqI-on__fw-off", "cbest_125_0"),
    ("MY_LIB_CELL_A_layout/eqI-off__fw-on/cbest_125_0", "eqI-off__fw-on", "cbest_125_0"),
    ("MY_LIB_CELL_A_layout/eqI-off__fw-off/cbest_125_0", "eqI-off__fw-off", "cbest_125_0"),
)

# per-design 覆盖：全局 equalCurrent 只有一个取值，design B 自己多扫一个。
# ⇒ 这根轴在**整个批次**上是"在变"的 ⇒ 两个 design 的 slug 里都要有它
#   （否则同一批次里两个 design 的目录名规则不一样，没法比对，BRIEF §5）。
GOLDEN_OVERRIDE: tuple[tuple[str, str, str], ...] = (
    ("MY_LIB_CELL_A_layout/eqI-on/typical_-40_0", "eqI-on", "typical_-40_0"),
    ("MY_LIB_CELL_B_layout/eqI-on/typical_-40_0", "eqI-on", "typical_-40_0"),
    ("MY_LIB_CELL_B_layout/eqI-off/typical_-40_0", "eqI-off", "typical_-40_0"),
)


# ==========================================================================
# 共用的输入构造路径 + 比较逻辑（正反两条测试必须走同一条路）
# ==========================================================================


def make_designs(count: int = 2, **kwargs: object) -> list[Design]:
    """2 个 design，只有 cell 不同。**正反测试共用这一条构造路径。**"""
    cells = ("CELL_A", "CELL_B", "CELL_C")
    return [Design(library="MY_LIB", cell=cells[i], view="layout", **kwargs) for i in range(count)]


def make_axes(**values: list[str]) -> list[Axis]:
    """按内置轴目录造轴，取值由调用方给。**正反测试共用这一条构造路径。**"""
    catalog = matrix.builtin_axis_catalog()
    return [matrix.axis_with_values(catalog[name], vals) for name, vals in values.items()]


def rows_of(runs: list) -> list[tuple[str, str, str]]:
    return [(run.run_id, run.axes_slug, run.ewave_dir) for run in runs]


FIELD_NAMES = ("run_id", "axes_slug", "ewave_dir")


def diff_rows(
    actual: list[tuple[str, str, str]], expected: tuple[tuple[str, str, str], ...]
) -> tuple[list[str], int]:
    """逐行逐字段比两张表，返回 (差异描述, **实际比过的字段数**)。

    第二个返回值是防自证配方 4 要的**计数断言**素材：空集合的 diff 永远是绿的，
    所以每条正向测试都要断言"确实比了 N 个字段"，N 从期望表数出来。
    """
    problems: list[str] = []
    if len(actual) != len(expected):
        problems.append(f"行数不同: 实际 {len(actual)} != 期望 {len(expected)}")
    compared = 0
    for index, (got, want) in enumerate(zip(actual, expected)):
        for name, got_value, want_value in zip(FIELD_NAMES, got, want):
            compared += 1
            if got_value != want_value:
                problems.append(f"第 {index} 行 {name}: 实际 {got_value!r} != 期望 {want_value!r}")
    return problems, compared


class DiffHelperHasTeeth(unittest.TestCase):
    """先证明比较逻辑自己不是摆设 —— 不然下面所有"零差异"都毫无意义。"""

    def test_diff_rows_reports_a_broken_cell(self) -> None:
        broken = [list(row) for row in GOLDEN_BASE]
        broken[0][2] = "typical_-40"  # 故意弄坏一个目录名
        problems, compared = diff_rows([tuple(r) for r in broken], GOLDEN_BASE)
        self.assertEqual(compared, 24, "8 行 × 3 个字段 = 24 次比较")
        self.assertEqual(len(problems), 1, f"应该只报这一个差异，实际: {problems}")
        self.assertIn("ewave_dir", problems[0])
        self.assertIn("typical_-40", problems[0])

    def test_diff_rows_reports_missing_rows(self) -> None:
        problems, compared = diff_rows(list(GOLDEN_BASE[:4]), GOLDEN_BASE)
        self.assertEqual(compared, 12, "只有 4 行能比 ⇒ 12 次比较，其余算行数差异")
        self.assertTrue(any("行数不同" in p for p in problems), problems)


# ==========================================================================
# expand_runs —— 关键测试（生成物 = run_id / slug / 目录名）
# ==========================================================================


class ExpandRunsGolden(unittest.TestCase):
    def test_two_designs_two_corners_two_temps(self) -> None:
        """2 design × 2 corner × 2 temp：目录名与官方逐字一致，slug 全是 base。"""
        runs = matrix.expand_runs(
            make_designs(2), make_axes(corner=["typical", "cbest"], temperature=["-40.0", "125.0"])
        )
        self.assertEqual(len(runs), 2 * 2 * 2, "计数断言：design × corner × temp")
        problems, compared = diff_rows(rows_of(runs), GOLDEN_BASE)
        self.assertEqual(compared, 24, "计数断言：8 行 × 3 字段，diff 不许是空的")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_two_designs_two_corners_two_temps_negative(self) -> None:
        """反向：把 `-40.0` 写成 `-40`（少了小数位）⇒ eWave 建的目录名就变了。

        与正向测试**同一条构造路径**，只改这一个取值。
        """
        runs = matrix.expand_runs(
            make_designs(2), make_axes(corner=["typical", "cbest"], temperature=["-40", "125.0"])
        )
        self.assertEqual(len(runs), 8, "行数不变，变的只有目录名")
        problems, compared = diff_rows(rows_of(runs), GOLDEN_BASE)
        self.assertEqual(compared, 24, "比较逻辑仍然比了 24 个字段（不是空过）")
        # 4 个 run 带 -40（2 design × 2 corner），每个的 run_id 和 ewave_dir 都变了 ⇒ 8 处差异
        self.assertEqual(len(problems), 8, "\n".join(problems))
        self.assertTrue(
            all(("run_id" in p) or ("ewave_dir" in p) for p in problems), "\n".join(problems)
        )
        self.assertTrue(
            any("'typical_-40'" in p and "'typical_-40_0'" in p for p in problems),
            "比较逻辑必须**明确报告**目录名从 typical_-40_0 变成了 typical_-40:\n"
            + "\n".join(problems),
        )
        dirs = {run.ewave_dir for run in runs}
        self.assertIn("typical_-40", dirs)
        self.assertNotIn("typical_-40_0", dirs)

    def test_extra_axes_go_into_the_slug(self) -> None:
        """加两根不在 eWave 目录名里的轴 ⇒ 它们进 `<axes-slug>`，形如 `eqI-on__fw-off`。"""
        runs = matrix.expand_runs(
            make_designs(1),
            make_axes(
                corner=["typical", "cbest"],
                temperature=["-40.0", "125.0"],
                equalCurrent=["on", "off"],
                fullWave=["on", "off"],
            ),
        )
        self.assertEqual(len(runs), 2 * 2 * 2 * 2, "计数断言：corner × temp × eqI × fw")
        problems, compared = diff_rows(rows_of(runs), GOLDEN_MULTI)
        self.assertEqual(compared, 48, "计数断言：16 行 × 3 字段")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_extra_axes_go_into_the_slug_negative(self) -> None:
        """反向：equalCurrent 从 2 个取值缩成 1 个 ⇒ 它**退出** slug。

        这是"slug 只编码在变的轴"的核心行为：单轴场景下目录名要与官方逐字一致。
        """
        runs = matrix.expand_runs(
            make_designs(1),
            make_axes(
                corner=["typical", "cbest"],
                temperature=["-40.0", "125.0"],
                equalCurrent=["on"],
                fullWave=["on", "off"],
            ),
        )
        self.assertEqual(len(runs), 2 * 2 * 1 * 2, "计数断言：eqI 只剩 1 个取值")
        slugs = {run.axes_slug for run in runs}
        self.assertEqual(slugs, {"fw-on", "fw-off"}, "eqI 不在变了，必须从 slug 里消失")
        for run in runs:
            self.assertNotIn("eqI", run.axes_slug, run.axes_slug)
        problems, compared = diff_rows(rows_of(runs), GOLDEN_MULTI[:8])
        self.assertEqual(compared, 24)
        self.assertTrue(problems, "slug 变了，比较逻辑必须报出来")
        self.assertTrue(any("eqI-on__fw-on" in p for p in problems), "\n".join(problems))

    def test_all_axes_single_valued_collapse_to_base(self) -> None:
        """全部轴都只有一个取值 ⇒ slug 是 `base`，目录名与官方完全同构。"""
        runs = matrix.expand_runs(
            make_designs(1),
            make_axes(corner=["typical"], temperature=["-40.0"], equalCurrent=["on"]),
        )
        self.assertEqual(len(runs), 1)
        self.assertEqual(rows_of(runs), [("MY_LIB_CELL_A_layout/base/typical_-40_0", "base", "typical_-40_0")])
        self.assertEqual(runs[0].axes_slug, BASE_SLUG)

    def test_all_axes_single_valued_collapse_to_base_negative(self) -> None:
        """反向：任何一根非 corner/temp 的轴一旦有第二个取值，slug 就不再是 `base`。"""
        runs = matrix.expand_runs(
            make_designs(1),
            make_axes(corner=["typical"], temperature=["-40.0"], equalCurrent=["on", "off"]),
        )
        self.assertEqual(len(runs), 2)
        self.assertEqual([r.axes_slug for r in runs], ["eqI-on", "eqI-off"])
        self.assertNotIn(BASE_SLUG, {r.axes_slug for r in runs})

    def test_per_design_override_is_batch_wide_for_the_slug(self) -> None:
        """某个 design 多扫一个取值 ⇒ 这根轴在**全批次**口径下算"在变"，两个 design 的 slug 都带它。"""
        designs = make_designs(2)
        designs[1].axis_overrides = {"equalCurrent": ("on", "off")}
        runs = matrix.expand_runs(
            designs, make_axes(corner=["typical"], temperature=["-40.0"], equalCurrent=["on"])
        )
        self.assertEqual(len(runs), 1 + 2, "计数断言：design A 1 个 + design B 2 个")
        problems, compared = diff_rows(rows_of(runs), GOLDEN_OVERRIDE)
        self.assertEqual(compared, 9, "计数断言：3 行 × 3 字段")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_per_design_override_is_batch_wide_for_the_slug_negative(self) -> None:
        """反向：去掉那条 override ⇒ equalCurrent 全批次只剩一个取值 ⇒ slug 退回 `base`。"""
        designs = make_designs(2)  # 同一条构造路径，只是不加 override
        runs = matrix.expand_runs(
            designs, make_axes(corner=["typical"], temperature=["-40.0"], equalCurrent=["on"])
        )
        self.assertEqual(len(runs), 2)
        problems, compared = diff_rows(rows_of(runs), GOLDEN_OVERRIDE)
        self.assertEqual(compared, 6, "只有 2 行能比 ⇒ 6 次比较")
        self.assertTrue(any("行数不同" in p for p in problems), "\n".join(problems))
        self.assertTrue(any("'base'" in p and "'eqI-on'" in p for p in problems), "\n".join(problems))
        self.assertEqual({run.axes_slug for run in runs}, {"base"})


class RunIdUniqueness(unittest.TestCase):
    """slug/run_id 撞了 = 两个 run 落同一个目录 = 静默覆盖 = 用户的核心痛点。"""

    def test_run_ids_are_unique(self) -> None:
        runs = matrix.expand_runs(
            make_designs(2),
            make_axes(
                corner=["typical", "cbest"],
                temperature=["-40.0", "125.0"],
                equalCurrent=["on", "off"],
            ),
        )
        run_ids = [run.run_id for run in runs]
        self.assertEqual(len(run_ids), 2 * 2 * 2 * 2, "计数断言")
        self.assertEqual(len(set(run_ids)), len(run_ids), "run_id 撞了就是静默覆盖")

    def test_slug_and_dir_pair_is_unique(self) -> None:
        """(design, slug, 目录名) 三元组唯一 —— 这三样才决定产物落在哪。"""
        runs = matrix.expand_runs(
            make_designs(2),
            make_axes(
                corner=["typical", "cbest"],
                temperature=["-40.0", "125.0"],
                equalCurrent=["on", "off"],
                fullWave=["on", "off"],
            ),
        )
        keys = [(run.design_key, run.axes_slug, run.ewave_dir) for run in runs]
        self.assertEqual(len(keys), 32, "计数断言：2 design × 2 × 2 × 2 × 2")
        self.assertEqual(len(set(keys)), len(keys))

    def test_duplicate_axis_value_is_rejected(self) -> None:
        with self.assertRaises(SpecError) as ctx:
            matrix.expand_runs(
                make_designs(1), make_axes(corner=["typical", "typical"], temperature=["-40.0"])
            )
        self.assertIn("typical", str(ctx.exception))

    def test_two_designs_with_the_same_key_are_rejected(self) -> None:
        designs = [
            Design(library="MY_LIB", cell="CELL_A", view="layout", key="same"),
            Design(library="MY_LIB", cell="CELL_B", view="layout", key="same"),
        ]
        with self.assertRaises(SpecError) as ctx:
            matrix.expand_runs(designs, make_axes(corner=["typical"], temperature=["-40.0"]))
        self.assertIn("same", str(ctx.exception))


# ==========================================================================
# varying_axes —— 过滤器（防自证配方 4：断言它没把该留的也滤掉）
# ==========================================================================


class VaryingAxesFilter(unittest.TestCase):
    """`varying_axes` 是个过滤器，最容易犯的错是"顺手多滤一类"。

    最像的一次真 bug 是 MVP 里 `--sparam` 前缀误伤 `--sparamImpedance`：过滤器多吃了一个，
    diff 空得非常好看但根本没比。这里的对应物是 **corner/temperature 不许被顺手滤掉** ——
    它们 `encoded_in_ewave_dir=True`，但那只决定进不进 slug，不决定它在不在变。
    """

    def make_mixed(self) -> list[Axis]:
        return make_axes(
            corner=["typical", "cbest"],  # 在变，且 encoded_in_ewave_dir=True
            temperature=["-40.0"],  # 不变，且 encoded_in_ewave_dir=True
            equalCurrent=["on", "off"],  # 在变
            fullWave=["on"],  # 不变
        )

    def test_keeps_every_axis_that_really_varies(self) -> None:
        axes = self.make_mixed()
        varying = matrix.varying_axes(axes)
        self.assertEqual([a.name for a in varying], ["corner", "equalCurrent"])
        self.assertEqual(len(varying), 2, "计数断言：4 根轴里恰好 2 根在变")
        self.assertIs(varying[0], axes[0], "返回的应该是原对象，方便调用方对号入座")
        self.assertTrue(
            varying[0].encoded_in_ewave_dir,
            "corner 是 encoded_in_ewave_dir=True 的轴 —— 它照样在变，不许被顺手滤掉",
        )

    def test_keeps_every_axis_that_really_varies_negative(self) -> None:
        """反向：把 corner 缩成一个取值 ⇒ 它必须**退出**结果；equalCurrent 必须还在。"""
        axes = make_axes(
            corner=["typical"],
            temperature=["-40.0"],
            equalCurrent=["on", "off"],
            fullWave=["on"],
        )
        varying = matrix.varying_axes(axes)
        self.assertEqual([a.name for a in varying], ["equalCurrent"])
        self.assertEqual(len(varying), 1, "计数断言")

    def test_per_design_override_counts_as_varying(self) -> None:
        axes = make_axes(corner=["typical"], equalCurrent=["on"])
        designs = make_designs(2)
        designs[1].axis_overrides = {"equalCurrent": ("on", "off")}
        varying = matrix.varying_axes(axes, designs=designs)
        self.assertEqual([a.name for a in varying], ["equalCurrent"])

    def test_per_design_override_counts_as_varying_negative(self) -> None:
        """反向：override 给的还是那一个取值 ⇒ 它不算在变。"""
        axes = make_axes(corner=["typical"], equalCurrent=["on"])
        designs = make_designs(2)
        designs[1].axis_overrides = {"equalCurrent": ("on",)}
        self.assertEqual(matrix.varying_axes(axes, designs=designs), [])

    def test_override_that_replaces_the_only_value_everywhere_is_not_varying(self) -> None:
        """两个 design 都覆盖成同一个别的取值 ⇒ 全批次仍然只有一个取值。"""
        axes = make_axes(corner=["typical"])
        designs = make_designs(2)
        for design in designs:
            design.axis_overrides = {"corner": ("cbest",)}
        self.assertEqual(matrix.varying_axes(axes, designs=designs), [])
        self.assertEqual(matrix.effective_axis_values(axes[0], designs), ["cbest"])


# ==========================================================================
# 名字：slugify / ewave_dir_name / design_key
# ==========================================================================


class NameRules(unittest.TestCase):
    def test_ewave_dir_name(self) -> None:
        """期望值抄自 BRIEF §5：`<temp>` 是温度把小数点换成下划线。"""
        self.assertEqual(matrix.ewave_dir_name("typical", "-40.0"), "typical_-40_0")
        self.assertEqual(matrix.ewave_dir_name("typical", "125.0"), "typical_125_0")
        self.assertEqual(matrix.ewave_dir_name("rcworst", "25.0"), "rcworst_25_0")

    def test_ewave_dir_name_negative(self) -> None:
        """反向：少一位小数就是另一个目录（这正是要抓的那类静默差异）。"""
        self.assertNotEqual(matrix.ewave_dir_name("typical", "-40"), "typical_-40_0")
        self.assertEqual(matrix.ewave_dir_name("typical", "-40"), "typical_-40")

    def test_slugify(self) -> None:
        self.assertEqual(matrix.slugify("typical"), "typical")
        self.assertEqual(matrix.slugify("-40.0"), "-40_0")
        self.assertEqual(matrix.slugify("1e-05"), "1e-05")
        self.assertEqual(matrix.slugify("adaptive,0:0.1:40"), "adaptive-0-0_1-40")
        self.assertEqual(matrix.slugify("MY_LIB"), "MY_LIB", "不许改大小写")
        self.assertEqual(matrix.slugify(""), "")

    def test_slugify_negative(self) -> None:
        """反向：大小写被改了 / 小数点没换 都必须能看出来。"""
        self.assertNotEqual(matrix.slugify("MY_LIB"), "my_lib")
        self.assertNotEqual(matrix.slugify("-40.0"), "-40.0")

    def test_design_key(self) -> None:
        """期望值抄自 BRIEF §5：design 目录名 = `<library>_<topCell>_<view>`。"""
        design = Design(library="MY_LIB", cell="CELL_A", view="layout")
        self.assertEqual(matrix.design_key(design), "MY_LIB_CELL_A_layout")

    def test_design_key_negative(self) -> None:
        """反向：view 不同就是不同的 design（BRIEF §5 特别强调 view 不是常量）。"""
        a = Design(library="MY_LIB", cell="CELL_A", view="layout")
        b = Design(library="MY_LIB", cell="CELL_A", view="layout_em")
        self.assertNotEqual(matrix.design_key(a), matrix.design_key(b))

    def test_explicit_key_wins(self) -> None:
        design = Design(library="MY_LIB", cell="CELL_A", view="layout", key="my_key")
        self.assertEqual(matrix.design_key(design), "my_key")


# ==========================================================================
# 轴目录 / per-design 覆盖 / 防呆
# ==========================================================================


class BuiltinCatalog(unittest.TestCase):
    def test_covers_the_axis_list_the_user_gave(self) -> None:
        """BRIEF §10「用户 2026-08-18 给出的设定轴清单」逐条对照。"""
        catalog = matrix.builtin_axis_catalog()
        for name in (
            "corner",
            "temperature",
            "equalCurrent",
            "fullWave",
            "mesh",
            "relativeTolerance",
            "relativeCurrentTolerance",
        ):
            self.assertIn(name, catalog)

    def test_corner_axis_owns_both_flags(self) -> None:
        """corner 轴**同时改两处**：`--corner=` 和 `--emssTechFile=` 的 ptxt（BRIEF §7）。

        少改一个 = 目录名说 typical、实际用了别的工艺角，而且跑得出来、数字也像。
        """
        corner = matrix.builtin_axis_catalog()["corner"]
        self.assertEqual(sorted(corner.flags), ["--corner", "--emssTechFile"])
        for value in corner.values:
            self.assertEqual(value.flags["--corner"], "{value}")
            self.assertEqual(value.flags["--emssTechFile"], "{ptxt}", "ptxt 路径只能是占位符")

    def test_only_corner_and_temperature_are_encoded_in_the_ewave_dir(self) -> None:
        catalog = matrix.builtin_axis_catalog()
        encoded = sorted(name for name, axis in catalog.items() if axis.encoded_in_ewave_dir)
        self.assertEqual(encoded, ["corner", "temperature"])

    def test_toggle_off_uses_false_to_cancel_the_default_table(self) -> None:
        """INTERFACES 契约 1：`False` 不是"没有"，是"显式缺席"。"""
        catalog = matrix.builtin_axis_catalog()
        for name, flag in (("equalCurrent", "--equalCurrent"), ("fullWave", "--fullWave")):
            axis = catalog[name]
            self.assertEqual(axis.kind, AxisKind.TOGGLE)
            by_value = {v.value: v.flags[flag] for v in axis.values}
            self.assertEqual(by_value, {"on": True, "off": False})

    def test_mesh_axis_changes_three_flags_at_once(self) -> None:
        mesh = matrix.builtin_axis_catalog()["mesh"]
        self.assertEqual(sorted(mesh.flags), ["--viaMergeSpace", "-d", "-e"])
        self.assertEqual(mesh.kind, AxisKind.GROUP)

    def test_catalog_returns_fresh_objects(self) -> None:
        """两次调用不许共享可变对象，否则一个批次会改到另一个批次。"""
        first = matrix.builtin_axis_catalog()["corner"]
        first.values[0].flags["--corner"] = "polluted"
        second = matrix.builtin_axis_catalog()["corner"]
        self.assertEqual(second.values[0].flags["--corner"], "{value}")

    def test_new_value_is_accepted_when_the_flag_template_is_homogeneous(self) -> None:
        """取值样例只是样例：温度轴照样能接受没列出来的取值。"""
        axis = matrix.axis_with_values(matrix.builtin_axis_catalog()["temperature"], ["85.0"])
        self.assertEqual(axis.values[0].flags, {"--temperature": "{value}"})

    def test_narrowed_toggle_axis_can_be_widened_again(self) -> None:
        """回归：spec 写 `equalCurrent: [on]` 会把轴收窄，但 per-design 覆盖成 `[on, off]`
        必须仍然认得 `off` —— 翻译规则还在内置目录里。（首次实现漏了，被 override 测试抓到。）"""
        narrowed = make_axes(equalCurrent=["on"])[0]
        widened = matrix.axis_with_values(narrowed, ["on", "off"])
        self.assertEqual([v.value for v in widened.values], ["on", "off"])
        self.assertEqual(widened.values[1].flags, {"--equalCurrent": False})

    def test_narrowed_toggle_axis_can_be_widened_again_negative(self) -> None:
        """反向：同名但被改成了别的语义（flag 不一样）⇒ **不许**套用内置目录的翻译规则。"""
        impostor = Axis(
            name="equalCurrent",
            values=(AxisValue("on", flags={"--somethingElse": True}),),
            kind=AxisKind.TOGGLE,
            flags=("--somethingElse",),
        )
        with self.assertRaises(SpecError):
            matrix.axis_with_values(impostor, ["on", "off"])

    def test_unknown_toggle_value_is_rejected_instead_of_guessed(self) -> None:
        """开关轴不同取值贡献的 flag 写法不同 ⇒ 猜就会"目录名说 off、命令行说 on"。"""
        with self.assertRaises(SpecError) as ctx:
            matrix.axis_with_values(matrix.builtin_axis_catalog()["equalCurrent"], ["maybe"])
        message = str(ctx.exception)
        self.assertIn("maybe", message)
        self.assertIn("on", message)
        self.assertIn("off", message)


class AxesForDesign(unittest.TestCase):
    def test_override_replaces_values_only(self) -> None:
        axes = make_axes(corner=["typical", "cbest"], temperature=["-40.0"])
        design = Design(
            library="MY_LIB",
            cell="CELL_A",
            view="layout",
            axis_overrides={"corner": ("rcworst",)},
        )
        resolved = matrix.axes_for_design(design, axes)
        self.assertEqual([a.name for a in resolved], ["corner", "temperature"])
        self.assertEqual([v.value for v in resolved[0].values], ["rcworst"])
        self.assertEqual(resolved[0].flags, ("--corner", "--emssTechFile"), "flag 定义不许被覆盖动到")
        self.assertEqual([v.value for v in axes[0].values], ["typical", "cbest"], "不许改到原对象")

    def test_unknown_axis_name_in_override(self) -> None:
        axes = make_axes(corner=["typical"])
        design = Design(
            library="MY_LIB", cell="CELL_A", view="layout", axis_overrides={"conrer": ("typical",)}
        )
        with self.assertRaises(SpecError) as ctx:
            matrix.axes_for_design(design, axes)
        self.assertIn("conrer", str(ctx.exception))
        self.assertIn("corner", str(ctx.exception), "报错要把能用的轴名列出来")

    def test_empty_override_is_rejected(self) -> None:
        axes = make_axes(corner=["typical"])
        design = Design(
            library="MY_LIB", cell="CELL_A", view="layout", axis_overrides={"corner": ()}
        )
        with self.assertRaises(SpecError):
            matrix.axes_for_design(design, axes)


class AxesForGroup(unittest.TestCase):
    """`axes_for_group` 与 `axes_for_design` 同构 —— 但报错必须说清是**组**写错了。

    直接点名这个函数（而不是只经 `expand_runs` 顺带覆盖）：它是冻结面上的符号，
    `core.spec` 和 `gui.state` 都会单独调它。经 expand_runs 的那几条只证明"整条路能报错"，
    证明不了这个函数自己的契约。
    """

    def test_unlisted_axes_are_inherited_verbatim(self) -> None:
        """组是 **delta**：没列的轴原样继承 base，连对象都是同一个。"""
        axes = make_axes(corner=["typical", "cbest"], temperature=["-40.0", "55.0"])
        group = RunGroup(name="hot", axis_overrides={"temperature": ("55.0",)})
        resolved = matrix.axes_for_group(group, axes)
        self.assertEqual([a.name for a in resolved], ["corner", "temperature"])
        self.assertIs(resolved[0], axes[0], "没被覆盖的轴不该被复制一份")
        self.assertEqual([v.value for v in resolved[1].values], ["55.0"])
        self.assertEqual(
            resolved[1].flags, axes[1].flags, "覆盖的是取值列表，不是 flag 定义"
        )
        self.assertEqual(
            [v.value for v in axes[1].values], ["-40.0", "55.0"], "不许改到原对象"
        )

    def test_an_empty_override_map_changes_nothing_negative(self) -> None:
        """反向：一根轴都不覆盖的组 == base 自己。上面那条才不是"覆盖永远生效"的假绿。"""
        axes = make_axes(corner=["typical", "cbest"], temperature=["-40.0", "55.0"])
        resolved = matrix.axes_for_group(RunGroup(name="empty"), axes)
        self.assertEqual(
            [(a.name, [v.value for v in a.values]) for a in resolved],
            [("corner", ["typical", "cbest"]), ("temperature", ["-40.0", "55.0"])],
        )

    def test_unknown_axis_name_says_which_group(self) -> None:
        axes = make_axes(corner=["typical"], temperature=["-40.0"])
        group = RunGroup(name="eqcur-off", axis_overrides={"equalCurent": ("off",)})
        with self.assertRaises(SpecError) as ctx:
            matrix.axes_for_group(group, axes)
        message = str(ctx.exception)
        self.assertIn("equalCurent", message, "把用户写错的那个名字原样回显出来")
        self.assertIn("eqcur-off", message, "得说清是哪个组写错了，用户才知道去改 spec 的哪一段")
        self.assertIn("corner", message, "报错要把这个批次有哪些轴列出来")
        self.assertIn("temperature", message)
        self.assertIn("Next:", message, "报错必须带「下一步怎么办」")

    def test_empty_override_is_rejected(self) -> None:
        """空取值列表 = 笛卡尔积塌成 0 个 run。拒绝而不是静默产出空批次。"""
        axes = make_axes(corner=["typical"])
        with self.assertRaises(SpecError) as ctx:
            matrix.axes_for_group(RunGroup(name="oops", axis_overrides={"corner": ()}), axes)
        self.assertIn("oops", str(ctx.exception))


class GuardRails(unittest.TestCase):
    def test_no_designs(self) -> None:
        with self.assertRaises(SpecError):
            matrix.expand_runs([], make_axes(corner=["typical"]))

    def test_no_axes_gives_one_run_per_design(self) -> None:
        runs = matrix.expand_runs(make_designs(2), [])
        self.assertEqual([r.run_id for r in runs], ["MY_LIB_CELL_A_layout/base", "MY_LIB_CELL_B_layout/base"])
        self.assertEqual([r.ewave_dir for r in runs], ["", ""], "没扫 corner/temp ⇒ 目录名要等运行时才知道")

    def test_temperature_only_still_gives_unique_run_ids(self) -> None:
        """只扫温度、没扫 corner ⇒ 预测不出 eWave 那层目录名，但 run_id 仍然必须唯一。"""
        runs = matrix.expand_runs(make_designs(1), make_axes(temperature=["-40.0", "125.0"]))
        self.assertEqual(len(runs), 2)
        self.assertEqual(len({r.run_id for r in runs}), 2, "run_id 撞了就是静默覆盖")
        self.assertEqual([r.ewave_dir for r in runs], ["", ""])

    def test_duplicate_axis_name(self) -> None:
        axes = make_axes(corner=["typical"])
        with self.assertRaises(SpecError):
            matrix.expand_runs(make_designs(1), axes + axes)

    def test_custom_axis_cannot_claim_the_ewave_dir(self) -> None:
        """自定义轴标 `encoded_in_ewave_dir=True` ⇒ 它既不进 slug 又不进 eWave 目录名 ⇒ 静默覆盖。"""
        rogue = Axis(
            name="rogue",
            values=(AxisValue("a", flags={"--rogue": "a"}), AxisValue("b", flags={"--rogue": "b"})),
            flags=("--rogue",),
            encoded_in_ewave_dir=True,
        )
        with self.assertRaises(SpecError) as ctx:
            matrix.expand_runs(make_designs(1), [rogue])
        self.assertIn("rogue", str(ctx.exception))


class NativeMultiValue(unittest.TestCase):
    """D12：`--temperature=a,b,c` 交给 eWave 自己展开（默认关，未实测）。"""

    def test_temperature_collapses(self) -> None:
        runs = matrix.expand_runs(
            make_designs(1),
            make_axes(corner=["typical", "cbest"], temperature=["-40.0", "125.0"]),
            options=BatchOptions(native_multi_value=True),
        )
        self.assertEqual(len(runs), 2, "两个 corner 各一条命令，温度并进一条命令里")
        self.assertEqual([r.axis_values["temperature"] for r in runs], ["-40.0,125.0"] * 2)
        self.assertEqual([r.ewave_dir for r in runs], ["", ""], "多个温度 ⇒ eWave 会建多层目录，预测不了")
        self.assertEqual(len({r.run_id for r in runs}), 2)

    def test_corner_never_collapses(self) -> None:
        """corner 还要改 `--emssTechFile` 的 ptxt 文件名，一条命令行给不出多个 ptxt。"""
        runs = matrix.expand_runs(
            make_designs(1),
            make_axes(corner=["typical", "cbest"]),
            options=BatchOptions(native_multi_value=True),
        )
        self.assertEqual([r.axis_values["corner"] for r in runs], ["typical", "cbest"])


# ==========================================================================
# run group —— base 之上的单点变体（用户 2026-08-19 拍板的组合模型）
# ==========================================================================

# 契约里那个原型：base = typical @ 3 个温度；再加两个只覆盖一两根轴的组。
# 3 + 1 + 1 = 5 个 run。期望值一行一行敲出来，不在测试里现算。
#
# 注意 slug：加了组之后 equalCurrent 和 fullWave 在**整个批次**上都在变了
# ⇒ 它们对**所有** run（包括 base 那 3 个）进 slug。基线的目录名因此从 base/ 变成
# fw-off__eqI-on/ —— 这是正确且不可避免的，否则两组的同一个温度会落进同一个目录。
GOLDEN_GROUPS: tuple[tuple[str, str, str], ...] = (
    # (run_id, axes_slug, ewave_dir)
    ("MY_LIB_CELL_A_layout/fw-off__eqI-on/typical_-40_0", "fw-off__eqI-on", "typical_-40_0"),
    ("MY_LIB_CELL_A_layout/fw-off__eqI-on/typical_55_0", "fw-off__eqI-on", "typical_55_0"),
    ("MY_LIB_CELL_A_layout/fw-off__eqI-on/typical_125_0", "fw-off__eqI-on", "typical_125_0"),
    ("MY_LIB_CELL_A_layout/fw-off__eqI-off/typical_55_0", "fw-off__eqI-off", "typical_55_0"),
    ("MY_LIB_CELL_A_layout/fw-on__eqI-on/typical_55_0", "fw-on__eqI-on", "typical_55_0"),
)


def prototype_axes() -> list[Axis]:
    """契约里那套 base 轴。正反测试共用这一条构造路径。"""
    return make_axes(
        corner=["typical"],
        temperature=["-40.0", "55.0", "125.0"],
        fullWave=["off"],
        equalCurrent=["on"],
    )


def prototype_groups() -> list[RunGroup]:
    return [
        RunGroup(
            name="eqcur-off",
            axis_overrides={"temperature": ("55.0",), "equalCurrent": ("off",)},
        ),
        RunGroup(
            name="fullwave", axis_overrides={"temperature": ("55.0",), "fullWave": ("on",)}
        ),
    ]


class RunGroups(unittest.TestCase):
    def test_five_runs_exactly(self) -> None:
        runs = matrix.expand_runs(
            make_designs(1), prototype_axes(), groups=prototype_groups()
        )
        problems, compared = diff_rows(rows_of(runs), GOLDEN_GROUPS)
        self.assertEqual(compared, 15, "5 行 x 3 个字段 = 15 次比较（防空过）")
        self.assertEqual(problems, [], "\n".join(problems))
        # 与上面那条比对**不重复**：diff_rows 只保证"每行等于期望表那一行"，
        # 期望表自己写成两行一样的话它照样绿。而 run_id 相同 = 同一个 --workDir
        # = 静默覆盖，正是本工具存在要消灭的东西 —— 所以单独钉一条。
        self.assertEqual(len({run.run_id for run in runs}), 5)
        self.assertEqual(len(runs), 5)

    def test_group_attribution(self) -> None:
        """每个 run 记得自己出自哪个组；base 的 3 个归 base。"""
        runs = matrix.expand_runs(
            make_designs(1), prototype_axes(), groups=prototype_groups()
        )
        self.assertEqual(
            [r.group for r in runs],
            [BASE_GROUP, BASE_GROUP, BASE_GROUP, "eqcur-off", "fullwave"],
        )

    def test_per_group_counts_and_zero_merges(self) -> None:
        report = matrix.expand_runs_detailed(
            make_designs(1), prototype_axes(), groups=prototype_groups()
        )
        self.assertEqual(report.merged, 0, "这个原型里没有跨组重复")
        self.assertEqual(report.per_group, ((BASE_GROUP, 3), ("eqcur-off", 1), ("fullwave", 1)))

    def test_no_groups_is_byte_for_byte_the_old_behaviour(self) -> None:
        """`groups=()` 必须和不传这个参数逐字相同 —— 最常见的场景不许被组功能碰坏。"""
        designs = make_designs(2)
        axes = make_axes(corner=["typical", "cbest"], temperature=["-40.0", "125.0"])
        self.assertEqual(
            rows_of(matrix.expand_runs(designs, axes, groups=())),
            rows_of(matrix.expand_runs(designs, axes)),
        )
        self.assertEqual(rows_of(matrix.expand_runs(designs, axes)), list(GOLDEN_BASE))

    def test_group_values_make_an_axis_count_as_varying(self) -> None:
        """★ 本次最容易写错的一条（契约点名）。

        base 只有 `temperature: [55.0]` + `equalCurrent: [on]`，组把 equalCurrent 换成 off。
        只看 base 的话 equalCurrent 是"不变的" ⇒ 不进 <axes-slug> ⇒ 两个 run 的 run_id
        都是 `<design>/base/typical_55_0` ⇒ 同一个 --workDir ⇒ 第二个静默覆盖第一个。
        所以 `effective_axis_values` / `varying_axes` 的口径必须把组的取值并进去。
        """
        axes = make_axes(corner=["typical"], temperature=["55.0"], equalCurrent=["on"])
        groups = [
            RunGroup(name="eqcur-off", axis_overrides={"equalCurrent": ("off",)}),
        ]
        runs = matrix.expand_runs(make_designs(1), axes, groups=groups)
        self.assertEqual(len(runs), 2)
        self.assertEqual(len({r.run_id for r in runs}), 2, "run_id 撞了就是静默覆盖")
        for run in runs:
            self.assertIn("eqI", run.axes_slug, f"equalCurrent 在变却没进 slug: {run.axes_slug}")
        self.assertEqual(
            [r.axes_slug for r in runs], ["eqI-on", "eqI-off"], "基线的目录名也跟着变，这是对的"
        )
        # 同一条判据的两个下层函数也要点名，免得只有 expand_runs 侥幸对
        equal_current = axes[2]
        self.assertEqual(
            matrix.effective_axis_values(equal_current, (), groups), ["on", "off"]
        )
        self.assertEqual(
            [a.name for a in matrix.varying_axes(axes, groups=groups)], ["equalCurrent"]
        )

    def test_without_the_group_that_axis_is_not_varying_negative(self) -> None:
        """反向：去掉组之后 equalCurrent 就该退回"不变" ⇒ slug 退回 base。

        没有这条，上面那条可能只是因为 equalCurrent 永远进 slug。
        """
        axes = make_axes(corner=["typical"], temperature=["55.0"], equalCurrent=["on"])
        runs = matrix.expand_runs(make_designs(1), axes)
        self.assertEqual([r.axes_slug for r in runs], [BASE_SLUG])
        self.assertEqual(matrix.varying_axes(axes), [])

    def test_cross_group_duplicates_are_merged_silently(self) -> None:
        """两个组都写了 55 度是很自然的写法 —— 折叠掉，别逼用户手工排除。"""
        axes = make_axes(corner=["typical"], temperature=["-40.0", "55.0"])
        groups = [
            RunGroup(name="hot", axis_overrides={"temperature": ("55.0",)}),
            RunGroup(name="hot-again", axis_overrides={"temperature": ("55.0",)}),
        ]
        report = matrix.expand_runs_detailed(make_designs(1), axes, groups=groups)
        self.assertEqual(len(report.runs), 2, "两个组的 55 度都被 base 的 55 度吃掉")
        self.assertEqual(report.merged, 2)
        self.assertEqual(report.per_group, ((BASE_GROUP, 2), ("hot", 0), ("hot-again", 0)))
        self.assertEqual([r.group for r in report.runs], [BASE_GROUP, BASE_GROUP])

    def test_duplicate_inside_one_group_still_raises(self) -> None:
        """组内重复是真 bug（轴取值列表里有重复项），不是"用户两处都写了"。"""
        axes = make_axes(corner=["typical"], temperature=["-40.0"])
        groups = [RunGroup(name="dup", axis_overrides={"temperature": ("55.0", "55.0")})]
        with self.assertRaises(SpecError):
            matrix.expand_runs(make_designs(1), axes, groups=groups)

    def test_unknown_axis_in_a_group(self) -> None:
        axes = make_axes(corner=["typical"])
        groups = [RunGroup(name="oops", axis_overrides={"conrer": ("cbest",)})]
        with self.assertRaises(SpecError) as ctx:
            matrix.expand_runs(make_designs(1), axes, groups=groups)
        message = str(ctx.exception)
        self.assertIn("conrer", message)
        self.assertIn("oops", message, "报错要说清是哪个组写错了")
        self.assertIn("corner", message, "报错要把能用的轴名列出来")

    def test_empty_override_in_a_group(self) -> None:
        axes = make_axes(corner=["typical"])
        groups = [RunGroup(name="empty", axis_overrides={"corner": ()})]
        with self.assertRaises(SpecError):
            matrix.expand_runs(make_designs(1), axes, groups=groups)

    def test_duplicate_group_names(self) -> None:
        axes = make_axes(corner=["typical", "cbest"])
        groups = [
            RunGroup(name="same", axis_overrides={"corner": ("cbest",)}),
            RunGroup(name="same", axis_overrides={"corner": ("typical",)}),
        ]
        with self.assertRaises(SpecError):
            matrix.expand_runs(make_designs(1), axes, groups=groups)

    def test_design_override_wins_over_group_override(self) -> None:
        """design 是这颗 design 的特例，压在组这层粗调之上（`expand_runs` 的套用顺序）。"""
        axes = make_axes(corner=["typical"], temperature=["-40.0"])
        designs = [
            Design(library="MY_LIB", cell="CELL_A", view="layout"),
            Design(
                library="MY_LIB",
                cell="CELL_B",
                view="layout",
                axis_overrides={"temperature": ("125.0",)},
            ),
        ]
        groups = [RunGroup(name="hot", axis_overrides={"temperature": ("55.0",)})]
        runs = matrix.expand_runs(designs, axes, groups=groups)
        self.assertEqual(
            [(r.design_key, r.axis_values["temperature"], r.group) for r in runs],
            [
                ("MY_LIB_CELL_A_layout", "-40.0", BASE_GROUP),
                ("MY_LIB_CELL_B_layout", "125.0", BASE_GROUP),
                ("MY_LIB_CELL_A_layout", "55.0", "hot"),
            ],
            "CELL_B 的组 run 被它自己的覆盖压回 125.0 ⇒ 和它的 base run 重合 ⇒ 折叠掉",
        )

    def test_a_group_named_base_is_merged_not_duplicated(self) -> None:
        """调用方自己塞一个 name=base 的组时不另建组，只是排到最前（`core.spec` 走的是合并路）。"""
        axes = make_axes(corner=["typical"], temperature=["-40.0"])
        groups = [
            RunGroup(name=BASE_GROUP),
            RunGroup(name="hot", axis_overrides={"temperature": ("55.0",)}),
        ]
        report = matrix.expand_runs_detailed(make_designs(1), axes, groups=groups)
        self.assertEqual([g for g, _ in report.per_group], [BASE_GROUP, "hot"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
