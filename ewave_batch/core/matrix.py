"""designs × 设定轴 → 笛卡尔积 → `list[Run]`（+ slug + varying 轴）。

三条语义是这个模块存在的全部理由（`PROJECT_BRIEF.md` §5「矩阵模型」/「归档布局」）：

1. **design 本身是矩阵的一个轴** —— `run = (design, corner, temperature, equalCurrent, …)`。
2. **slug 只编码「在变的轴」**（`varying_axes`）。全批次里某根轴只有一个取值，它就不进 slug ——
   于是只扫 corner+temp 时目录名与官方**逐字一致**，设计师才认得。
3. **`<corner>_<temp>` 那层是 eWave 自己建的，我们控制不了名字**（温度的小数点换下划线）。
   我们只控制 `--workDir`：把**额外轴**放进 workDir 的路径里，让 eWave 那层留在最内。
   这正是「同 corner/temp 换别的 flag 会静默覆盖」的解法（BRIEF §7）。

本模块**不写盘、不碰环境**，纯函数。站点坐标一个都没有：出现的取值全是 eWave 的工具语义
（工艺角名、温度、`-e 0.4` 这种数值），不是站点身份（CLAUDE.md 硬约束 1b）。
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import replace

from ..model import (
    AXIS_SLUG_SEP,
    BASE_GROUP,
    BASE_SLUG,
    PLACEHOLDER_PTXT,
    PLACEHOLDER_VALUE,
    TEMP_DECIMAL_REPLACEMENT,
    Axis,
    AxisKind,
    AxisValue,
    BatchOptions,
    Design,
    Run,
    RunExpansion,
    RunGroup,
    SpecError,
)

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

AXIS_CORNER = "corner"
"""corner 轴的规范轴名。eWave 那层目录叫 `<corner>_<temp>`，所以这两个名字是有语义的。"""

AXIS_TEMPERATURE = "temperature"
"""temperature 轴的规范轴名。"""

EWAVE_DIR_AXES: tuple[str, ...] = (AXIS_CORNER, AXIS_TEMPERATURE)
"""**只有这两根轴**允许 `encoded_in_ewave_dir=True` —— 它们是 eWave 自己写进目录名的那两个。

别的轴要是也标了 True，它既不进 `<axes-slug>`、又不进 eWave 那层目录名 ⇒ 两个 run 落同一个
目录、**静默覆盖**。那正是本工具存在的理由，所以 `expand_runs` 直接拒绝这种轴定义。
"""

_SLUG_KEEP_EXTRA = "._-"
"""`slugify` 保留的非字母数字字符（其中 `.` 随后会被换成 `_`）。"""


# --------------------------------------------------------------------------
# 基础：id 与名字
# --------------------------------------------------------------------------


def design_key(design: Design) -> str:
    """算一个 design 的稳定 id（`design.key` 非空时直接返回它）。

    默认形状沿用官方目录名约定 `<library>_<topCell>_<view>` 再过一遍 `slugify` ——
    设计师认得，而且天然唯一。不写盘。
    """
    if design.key:
        return design.key
    return slugify(f"{design.library}_{design.cell}_{design.view}")


def slugify(text: str) -> str:
    """把任意取值变成能当目录名的片段。

    规则：保留 `[A-Za-z0-9._-]`，小数点换成 `_`（跟 eWave 的温度约定一致），
    其余连续字符压成一个 `-`，**不改大小写**（端口名是 case-sensitive，别养成改大小写的手感）。
    空输入返回空串，由调用方决定要不要报错。
    """
    out: list[str] = []
    pending_sep = False
    for ch in str(text).strip():
        keep = ch.isascii() and (ch.isalnum() or ch in _SLUG_KEEP_EXTRA)
        if keep:
            if pending_sep and out:
                out.append("-")
            pending_sep = False
            out.append(TEMP_DECIMAL_REPLACEMENT if ch == "." else ch)
        elif out:
            # 前导的非法字符直接丢（不产生前导 '-'），中间的连续非法字符压成一个 '-'
            pending_sep = True
    return "".join(out)


def ewave_dir_name(corner: str, temperature: str) -> str:
    """拼 eWave 自己会建的那层目录名：`<corner>_<temp 的小数点换下划线>`。

    ⚠️ 我们**控制不了**这个名字，只能预测它好去里面找产物。名字只由 corner+temp 决定，
    这正是"同 corner/temp 换别的 flag 会静默覆盖"的根因（BRIEF §7）。

    这里**故意不做 `slugify`**：要复现的是 eWave 的行为，不是我们自己的命名品味。
    唯一的加工就是小数点换 `_`（`-40.0` → `-40_0`，`125.0` → `125_0`，BRIEF §5）。
    两个都空返回空串（= 这个批次没扫 corner/temp，目录名要等运行时才知道）。
    """
    left = str(corner).strip()
    right = str(temperature).strip().replace(".", TEMP_DECIMAL_REPLACEMENT)
    if not left and not right:
        return ""
    return f"{left}_{right}"


# --------------------------------------------------------------------------
# 轴：哪些在变 / 怎么拼 slug / per-design 覆盖
# --------------------------------------------------------------------------


def varying_axes(
    axes: Sequence[Axis],
    *,
    designs: Sequence[Design] = (),
    groups: Sequence[RunGroup] = (),
) -> list[Axis]:
    """挑出**真正在变**的轴（取值 > 1 个的）。

    slug 只编码变的那些：只扫 corner+temp 时目录名与官方逐字一致，设计师才认得（BRIEF §5）。
    给了 `designs` 就把 per-design 的 `axis_overrides` 也算进去（某个 design 上多出来的取值
    同样算"在变"）；给了 `groups` 同理 —— 见 `effective_axis_values` 里那段口径说明。

    ⚠️ 这是个**过滤器**，最容易犯的错是"顺手多滤掉一类"：
    `encoded_in_ewave_dir=True` 的 corner/temperature 在这里**必须照样返回** ——
    它们进不进 slug 是 `compute_axes_slug` 的事，不是"在不在变"的事。
    返回的是**原对象**（不是副本），调用方可以拿 `is` 比。
    """
    return [axis for axis in axes if len(effective_axis_values(axis, designs, groups)) > 1]


def effective_axis_values(
    axis: Axis,
    designs: Sequence[Design] = (),
    groups: Sequence[RunGroup] = (),
) -> list[str]:
    """这根轴在**整个批次**上实际会取到的取值（去重、保序）。

    非冻结面（P1 内部 helper，`FROZEN` 里没有它 —— 并行夜跑不许改 `model.py`）。

    没给 designs/groups 就是轴自己的取值；给了就算并集：
    * 组维度：某个组覆盖了这根轴、别的组没覆盖 ⇒ base 的取值和该组的取值**都**会出现；
    * design 维度：同理，且 design 的覆盖压在组的覆盖之上（`expand_runs` 就是这个顺序）。

    🚨 **这个函数的口径就是「在不在变」的判据，漏一层就会静默覆盖。**
    最具体的例子：base 只有 `temperature: [55.0]`、`equalCurrent: [on]`，
    加一个组 `{temperature: [55.0], equalCurrent: [off]}`。只看 base 的话 equalCurrent
    不变 ⇒ 不进 `<axes-slug>` ⇒ 两个 run 的 run_id 都是 `<design>/base/typical_55_0`
    ⇒ 同一个 `--workDir` ⇒ 第二个把第一个的结果覆盖掉，而且没有任何提示。
    把组的取值并进来之后 equalCurrent 变成"在变" ⇒ 两个 run 分别落
    `eqI-on/...` 和 `eqI-off/...`。**加组会改掉基线的目录名，这是正确且不可避免的。**
    """
    global_values = [av.value for av in axis.values]
    if not designs and not groups:
        return _dedup(global_values)
    # 先把「组维度」摊平成若干层取值。**base 那层永远在**：调用方给的 `groups` 通常是
    # 「base 之外的那些组」（`BatchSpec.groups` 就是这个口径），而 base 组是不可选的 ——
    # 漏了它，base 自己的取值就不算数，某根轴会被判成"不变"，接着就是静默覆盖。
    # 给的组里已经有一个叫 base 的（`expand_runs` 走的 `_all_groups` 就是这样）就不再补。
    group_layers: list[list[str]] = []
    if not any(group.name == BASE_GROUP for group in groups):
        group_layers.append(global_values)
    for group in groups:
        override = group.axis_overrides.get(axis.name)
        group_layers.append([str(v) for v in override] if override else global_values)
    if not group_layers:
        group_layers = [global_values]
    seen: list[str] = []
    for layer in group_layers:
        if not designs:
            seen.extend(layer)
            continue
        for design in designs:
            override = design.axis_overrides.get(axis.name)
            # design 的覆盖赢组的覆盖（`expand_runs` 里 axes_for_design 套在 axes_for_group 之后）
            seen.extend([str(v) for v in override] if override else layer)
    return _dedup(seen)


def compute_axes_slug(axis_values: Mapping[str, str], axes: Sequence[Axis]) -> str:
    """算 `<axes-slug>` —— 只含 `encoded_in_ewave_dir=False` 且**在变**的轴。

    形如 `eqI-on__fw-off`；一个都没有时返回 `BASE_SLUG`（`"base"`）。
    片段顺序 = `axes` 里的顺序（不是字典序），这样同一批次里 slug 稳定可读。
    不写盘。

    ⚠️ 传进来的 `axes` 决定了"在变"的判定。`expand_runs` 传的是**全批次口径**的轴
    （per-design override 已经并进去了），所以同一根轴在所有 design 上的 slug 写法一致。
    `axis_values` 里没有的轴名直接跳过（GUI 预览半张表时用得上）。
    """
    parts: list[str] = []
    for axis in varying_axes(axes):
        if axis.encoded_in_ewave_dir:
            continue  # 已经被 eWave 编进 <corner>_<temp> 了，再进 slug 就是写两遍
        if axis.name not in axis_values:
            continue
        parts.append(_slug_fragment(axis, str(axis_values[axis.name])))
    return AXIS_SLUG_SEP.join(parts) if parts else BASE_SLUG


def axes_for_design(design: Design, axes: Sequence[Axis]) -> list[Axis]:
    """把 `design.axis_overrides` 套到全局轴定义上，返回这个 design 实际要扫的轴。

    覆盖的是**取值列表**，不是 flag 定义 —— 轴的语义全批次一致，否则 slug 会对不上号。
    override 里出现未知轴名 → `SpecError`。
    """
    return _apply_axis_overrides(
        axes,
        design.axis_overrides,
        subject=f"design {design_key(design)!r}",
        spot="that design's own axes:",
    )


def axes_for_group(group: RunGroup, axes: Sequence[Axis]) -> list[Axis]:
    """把 `group.axis_overrides` 套到 base 轴上，返回这个组实际要扫的轴。

    与 `axes_for_design` **同构**：合并语义一模一样，只是主人不同。选这个形状正是因为
    合并这件事的代码和测试早就有了（`axes_for_design`），组因此不引入任何新语义。
    override 里出现未知轴名 → `SpecError`，消息里列出这个批次有哪些轴。
    """
    return _apply_axis_overrides(
        axes,
        group.axis_overrides,
        subject=f"run group {group.name!r}",
        spot="that group's axes:",
    )


def _apply_axis_overrides(
    axes: Sequence[Axis],
    overrides: Mapping[str, Sequence[str]],
    *,
    subject: str,
    spot: str,
) -> list[Axis]:
    """`axes_for_design` / `axes_for_group` 共用的合并实现。

    留着 `subject` / `spot` 两个参数而不把措辞写死：报错必须说清是「design 覆盖错了」
    还是「组覆盖错了」，否则用户不知道该去改 spec 的哪一段 —— 而这两处的下一步是不同的。
    """
    known = {axis.name for axis in axes}
    unknown = [name for name in overrides if name not in known]
    if unknown:
        raise SpecError(
            f"{subject} overrides an axis that does not exist: {', '.join(sorted(unknown))}\n"
            f"  axes defined in this batch: {', '.join(sorted(known)) or '(none at all)'}\n"
            f"  Next: fix the name in {spot} to one of the above, "
            "or define that axis under the top-level axes: first"
        )
    out: list[Axis] = []
    for axis in axes:
        override = overrides.get(axis.name)
        if override is None:
            out.append(axis)
            continue
        if not override:
            raise SpecError(
                f"{subject} overrides axis {axis.name!r} with an empty list - the cartesian "
                "product collapses to nothing, so it would produce zero runs.\n"
                f"  Next: give at least one value in {spot}, or drop the override "
                "(dropping it means 'inherit the base values')"
            )
        out.append(axis_with_values(axis, override))
    return out


def axis_with_values(axis: Axis, values: Sequence[str]) -> Axis:
    """把一根轴的取值换成 `values`，其余定义（flag / kind / slug 模板）原样保留。

    非冻结面（P1 内部 helper，`core.spec` 也用它把 spec 里写的取值套到内置轴上）。

    取值不在轴的取值表里时会**尽量**现造一个 `AxisValue`：只有当这根轴的每个取值贡献的
    flag 写法**完全一样**（都是 `{"--temperature": "{value}"}` 这种带占位符的模板）时才敢造。
    开关轴（on → `True` / off → `False`）不同取值写法不同 ⇒ **拒绝而不是猜**，
    猜错的后果是"目录名说 off、命令行说 on"，那正是要消灭的坑。
    """
    seen: list[str] = []
    materialized: list[AxisValue] = []
    for raw in values:
        text = str(raw)
        if text in seen:
            raise SpecError(
                f"Axis {axis.name!r} lists the value {text!r} twice - the cartesian "
                "product would expand two identical runs (same directory, the second one "
                "silently overwrites the first).\n"
                "  Next: drop the duplicate"
            )
        seen.append(text)
        materialized.append(_materialize_value(axis, text))
    return replace(axis, values=tuple(materialized))


def builtin_axis_catalog() -> dict[str, Axis]:
    """内置轴目录：轴名 → `Axis`（取值列表为**该轴的合法取值样例**，实际取值由 spec 给）。

    覆盖 BRIEF §10 用户点名的那张清单：corner（同时改 `--corner=` **和** `--emssTechFile=`）、
    temperature、`--equalCurrent`、`--fullWave`、网格密度（`-e`/`-d`/`--viaMergeSpace`）、
    两个 tolerance、频率扫描（`--multiSweep`/`--logarithmicSweep`/`--discreteFreq`）、`--parallel`。

    ⚠️ 只许出现 flag 名和通用数值 —— **ptxt 路径靠 `PLACEHOLDER_PTXT` 占位**，
    corner 的合法取值也只是 5 个通用工艺角名字，不是站点身份。

    每次调用**现造一套新对象**：`Axis` / `AxisValue.flags` 是可变的，共享一份会让某个批次
    改到别的批次。
    """
    corner_flags = {"--corner": PLACEHOLDER_VALUE, "--emssTechFile": PLACEHOLDER_PTXT}
    sweep_flag_names = ("--multiSweep", "--logarithmicSweep", "--discreteFreq")
    return {
        AXIS_CORNER: Axis(
            name=AXIS_CORNER,
            values=tuple(
                AxisValue(name, flags=dict(corner_flags))
                for name in ("cbest", "cworst", "rcbest", "rcworst", "typical")
            ),
            kind=AxisKind.VALUE,
            flags=("--corner", "--emssTechFile"),
            short="corner",
            encoded_in_ewave_dir=True,
            description=(
                "工艺角。**同时改两处**：--corner= 和 --emssTechFile 的 ptxt 文件名（BRIEF §7）"
            ),
        ),
        AXIS_TEMPERATURE: Axis(
            name=AXIS_TEMPERATURE,
            values=tuple(
                AxisValue(value, flags={"--temperature": PLACEHOLDER_VALUE})
                for value in ("-40.0", "25.0", "125.0")
            ),
            kind=AxisKind.VALUE,
            flags=("--temperature",),
            short="temp",
            encoded_in_ewave_dir=True,
            description="温度。写成 -40.0 还是 -40 会改变 eWave 建的目录名，别省小数位",
        ),
        "equalCurrent": Axis(
            name="equalCurrent",
            values=(
                AxisValue("on", flags={"--equalCurrent": True}),
                AxisValue("off", flags={"--equalCurrent": False}),
            ),
            kind=AxisKind.TOGGLE,
            flags=("--equalCurrent",),
            short="eqI",
            description="off 用 False 把默认表里的 --equalCurrent 抵消掉（INTERFACES 契约 1）",
        ),
        "fullWave": Axis(
            name="fullWave",
            values=(
                AxisValue("on", flags={"--fullWave": True}),
                AxisValue("off", flags={"--fullWave": False}),
            ),
            kind=AxisKind.TOGGLE,
            flags=("--fullWave",),
            short="fw",
            description="全波 vs 准静态",
        ),
        "mesh": Axis(
            name="mesh",
            values=tuple(
                AxisValue(
                    value,
                    flags={
                        "-e": PLACEHOLDER_VALUE,
                        "-d": PLACEHOLDER_VALUE,
                        "--viaMergeSpace": PLACEHOLDER_VALUE,
                    },
                )
                for value in ("0.4", "0.5")
            ),
            kind=AxisKind.GROUP,
            flags=("-e", "-d", "--viaMergeSpace"),
            short="mesh",
            description=(
                "网格密度，一个取值同时改三个 flag（官方 0.4，eWave 默认 0.5）。**唯一改 mesh 的轴**"
            ),
        ),
        "relativeTolerance": Axis(
            name="relativeTolerance",
            values=tuple(
                AxisValue(value, flags={"--relativeTolerance": PLACEHOLDER_VALUE})
                for value in ("1e-05", "1e-06")
            ),
            kind=AxisKind.VALUE,
            flags=("--relativeTolerance",),
            short="rtol",
            description="收敛容差（生产 1e-05）",
        ),
        "relativeCurrentTolerance": Axis(
            name="relativeCurrentTolerance",
            values=tuple(
                AxisValue(value, flags={"--relativeCurrentTolerance": PLACEHOLDER_VALUE})
                for value in ("0.001", "0.0001")
            ),
            kind=AxisKind.VALUE,
            flags=("--relativeCurrentTolerance",),
            short="rctol",
            description="电流收敛容差（生产 0.001）",
        ),
        "multiSweep": Axis(
            name="multiSweep",
            values=tuple(
                AxisValue(
                    value,
                    flags={
                        "--multiSweep": PLACEHOLDER_VALUE,
                        "--logarithmicSweep": False,
                        "--discreteFreq": False,
                    },
                )
                for value in ("adaptive,0:0.1:40", "adaptive,0:0.5:40")
            ),
            kind=AxisKind.VALUE,
            flags=sweep_flag_names,
            short="ms",
            description="频率扫描（自适应扫频）。取值就是 --multiSweep= 后面那一整串",
        ),
        "discreteFreq": Axis(
            name="discreteFreq",
            values=tuple(
                AxisValue(
                    value,
                    flags={
                        "--discreteFreq": PLACEHOLDER_VALUE,
                        "--multiSweep": False,
                        "--logarithmicSweep": False,
                    },
                )
                for value in ("5", "10")
            ),
            kind=AxisKind.VALUE,
            flags=sweep_flag_names,
            short="df",
            description=(
                "频率扫描（单/离散频点，MVP 的快捷方式）。与 multiSweep 互斥，互相用 False 抵消"
            ),
        ),
        "parallel": Axis(
            name="parallel",
            values=tuple(
                AxisValue(value, flags={"--parallel": PLACEHOLDER_VALUE}) for value in ("10", "20")
            ),
            kind=AxisKind.VALUE,
            flags=("--parallel",),
            short="par",
            description=(
                "求解器线程数。⚠️ 与 dsub -R 的 cpu= 耦合（BRIEF §6，当前倍率 1:1）——"
                "真正的同步在 core.cmd.parse_resource_string + BatchOptions.parallel_multiplier"
            ),
        ),
    }


# --------------------------------------------------------------------------
# 展开
# --------------------------------------------------------------------------


def expand_runs(
    designs: Sequence[Design],
    axes: Sequence[Axis],
    *,
    options: BatchOptions | None = None,
    groups: Sequence[RunGroup] = (),
) -> list[Run]:
    """展开笛卡尔积 → `list[Run]`，每个 Run 带好 `run_id` / `axes_slug` / `ewave_dir`。

    * design 本身是矩阵的一个轴（BRIEF §5「矩阵模型」）。
    * per-design 的 `axis_overrides` 生效（走 `axes_for_design`）。
    * `groups` 非空时批次 = base 组 + 这些组，每组各自取笛卡尔积、结果取**并集**。
      `groups=()` 与加这个参数之前**逐字相同**（`tests/test_matrix.py` 的 golden 表守着）。
    * `options.native_multi_value=True` 时**不**展开 corner/temperature（交给 eWave 原生多值，D12）。
    * `work_dir` 这里不填（要 batch_dir 才知道），由 `core.layout.compute_run_paths` 补。

    不写盘。同一个组内重复的 (design, 取值组合) → `SpecError`；**跨组**重复静默折叠。

    只要 run 数、不要「折叠了几个」的话用这个；两样都要走 `expand_runs_detailed`。
    """
    return list(expand_runs_detailed(designs, axes, options=options, groups=groups).runs)


def expand_runs_detailed(
    designs: Sequence[Design],
    axes: Sequence[Axis],
    *,
    options: BatchOptions | None = None,
    groups: Sequence[RunGroup] = (),
) -> RunExpansion:
    """`expand_runs` 的完整版：run 列表 + 跨组折叠掉几个 + 每组各贡献几个。

    展开顺序：**组在最外**，然后 design，最后轴按传进来的顺序（第一根轴变得最慢）。
    同一份输入永远同样的顺序 —— `runs.csv` 和 dry-run 的输出才可以逐行 diff。

    组为什么排在最外而不是 design 最外：跨组重复要「留第一个」，而 base 组必须是那个第一个
    （基线的 run 归 base，界面上按组分块也才连续）。design 最外的话第一个出现的就变成
    「design A 的某个组的 run」，组归属会随 design 数量抖动。
    """
    opts = options if options is not None else BatchOptions()
    axis_list = list(axes)
    design_list = list(designs)
    group_list = _all_groups(groups)
    _check_axes(axis_list)
    _check_groups(group_list)
    _check_native_multi_value(opts, group_list, axis_list)
    if not design_list:
        raise SpecError(
            "No designs at all - the matrix is empty and expands to 0 runs.\n"
            "  Next: the spec needs at least one entry under designs:, e.g.\n"
            "    designs:\n"
            "      - library: <lib>\n"
            "        cell: <cell>\n"
            "        view: <view>"
        )
    _check_design_keys(design_list)

    # slug 的口径是**全批次**的：某根轴只在 design B / 某个组上多了一个取值，它在别的 design、
    # 别的组的 slug 里也照样出现。否则同一批次里目录名规则不一致，没法比对（BRIEF §5），
    # 更糟的是漏算 ⇒ 该进 slug 的轴没进 ⇒ 两个 run 撞 run_id ⇒ 静默覆盖。
    slug_axes = _batch_axes(axis_list, design_list, group_list)

    runs: list[Run] = []
    merged = 0
    counts: dict[str, int] = {group.name: 0 for group in group_list}
    # run_id → (第一个产出它的组名, 那组算出来的 axis_values 的字面量)
    seen: dict[str, tuple[str, str]] = {}
    for group in group_list:
        group_axes = axes_for_group(group, axis_list)
        for design in design_list:
            key = design_key(design)
            # design 的覆盖压在组的覆盖之上：组是批次级的粗调，design 是这颗 design 的特例。
            design_axes = axes_for_design(design, group_axes)
            names = [axis.name for axis in design_axes]
            value_lists: list[tuple[str, ...]] = []
            collapsed: set[str] = set()
            for axis in design_axes:
                values = tuple(av.value for av in axis.values)
                if opts.native_multi_value and axis.name == AXIS_TEMPERATURE and len(values) > 1:
                    # D12：交给 eWave 原生多值（`--temperature=a,b,c`），一个 run 跑完所有温度。
                    # ⚠️ corner **不**并进来：corner 还要改 --emssTechFile 的 ptxt 文件名（BRIEF §7），
                    # 一条命令行给不出多个 ptxt。并了就会"目录名说 typical、实际用了别的工艺角"。
                    values = (",".join(values),)
                    collapsed.add(axis.name)
                value_lists.append(values)

            for combo in itertools.product(*value_lists):
                axis_values = dict(zip(names, combo))
                corner = "" if AXIS_CORNER in collapsed else axis_values.get(AXIS_CORNER, "")
                temperature = (
                    "" if AXIS_TEMPERATURE in collapsed else axis_values.get(AXIS_TEMPERATURE, "")
                )
                # corner/temperature 没都当轴扫（或被 D12 的原生多值折叠掉）时，
                # `<corner>_<temp>/` 这层的名字就**预测不出来**。留空串是诚实的表示，
                # 不是遗漏 —— eWave 跑完之后那层目录是存在的，届时由
                # `layout.verify_run_outputs` 现场发现（见那里的「预测不出来怎么办」）。
                ewave_dir = ewave_dir_name(corner, temperature) if (corner and temperature) else ""
                # run_id 的第三段：能预测 eWave 那层目录名就用它；预测不了（corner/temp 没都当轴扫）
                # 就退回用已知的那几个取值拼一个 —— run_id 必须唯一，撞了就是静默覆盖。
                tail = ewave_dir or "_".join(slugify(v) for v in (corner, temperature) if v)
                slug = compute_axes_slug(axis_values, slug_axes)
                run_id = "/".join(part for part in (key, slug, tail) if part)
                # 不变量：任何"在变"的轴要么进 axes_slug、要么进 ewave_dir/tail
                # ⇒ run_id 相同 <=> axis_values 相同。所以撞 run_id 有且只有两种解释：
                # 跨组重复（用户两个组都写了 55 度，很自然）⇒ 静默折叠；
                # 同组内重复（轴取值列表里有重复项）⇒ 真 bug，抛。
                if run_id in seen:
                    owner, previous = seen[run_id]
                    if owner == group.name:
                        # 只有真有组的时候才把组名放进消息里。用户没写过 `groups:` 却看到
                        # "in group 'base'" 的话，第一反应是去 spec 里找一个根本不存在的段。
                        where = (
                            f" in group {group.name!r}" if len(group_list) > 1 else ""
                        )
                        raise SpecError(
                            f"Two runs{where} got the same run_id {run_id!r}:\n"
                            f"  {previous}\n  {axis_values}\n"
                            "  Same run_id = same output directory = the second one silently "
                            "overwrites the first (exactly the trap this tool exists to kill).\n"
                            "  Next: look for a duplicated value in the axis value lists"
                        )
                    if previous != str(axis_values):
                        # 走到这说明「在变的轴一定被编码进 run_id」这条不变量破了。
                        # 宁可当场炸，也不能让两组不同设定落进同一个 --workDir。
                        raise SpecError(
                            f"run_id {run_id!r} collides across run groups but the axis values "
                            f"differ:\n  group {owner!r}: {previous}\n"
                            f"  group {group.name!r}: {axis_values}\n"
                            "  Same run_id = same output directory = silent overwrite.\n"
                            "  Next: this is a bug in the tool (a varying axis did not make it "
                            "into the directory name) - please report it with this spec"
                        )
                    merged += 1
                    continue
                seen[run_id] = (group.name, str(axis_values))
                counts[group.name] += 1
                runs.append(
                    Run(
                        run_id=run_id,
                        design_key=key,
                        axis_values=axis_values,
                        axes_slug=slug,
                        ewave_dir=ewave_dir,
                        group=group.name,
                    )
                )
    return RunExpansion(
        runs=tuple(runs),
        merged=merged,
        per_group=tuple((group.name, counts[group.name]) for group in group_list),
    )


def _batch_axes(
    axes: Sequence[Axis],
    designs: Sequence[Design] = (),
    groups: Sequence[RunGroup] = (),
) -> list[Axis]:
    """每根轴的取值换成**全批次并集**（base ∪ 每个组的覆盖 ∪ 每个 design 的覆盖）。

    非冻结面（P1 内部 helper）。两个完全不同的消费者，同一个口径：

    1. `expand_runs_detailed` 拿它算 slug —— "在不在变"必须按全批次判（第 6 条契约）；
    2. **`PlanContext.axes`**（`BatchState.axes` / 红区 dry-run 都从这里来）。
       第 2 条是 2026-08-19 实测出来的：`cmd.build_flag_layers` 是拿
       `run.axis_values[name]` 去轴的取值表里**查** flag 的，而 base 的轴上根本没有
       "组才用到的那个取值" => 一个 `equalCurrent: [off]` 的组会让整批 run 在
       `resolve_axis_flags` 里抛 "axis 'equalCurrent' has no value 'off'"，
       CLI / GUI / 红区 dry-run 三条路一起废。轴的定义是**批次级**的，
       所以存进 `BatchState` 的那份必须是并集，不是 base 那份。

    `axis_with_values` 翻不出某个取值时（界面自造的三段网格之类，见它的 docstring）
    保留原来那根轴 —— 这里不是报错的地方，让真正用到它的那一步去报更具体的错。
    """
    group_list = _all_groups(groups)
    out: list[Axis] = []
    for axis in axes:
        values = effective_axis_values(axis, designs, group_list)
        try:
            out.append(axis_with_values(axis, values))
        except SpecError:
            out.append(axis)
    return out


def _check_native_multi_value(
    opts: BatchOptions, group_list: Sequence[RunGroup], axes: Sequence[Axis]
) -> None:
    """`native_multi_value` 和 run group **不能同时用**。当场拒绝，别让它跑出去。

    为什么不能：原生多值（D12）把 temperature 折成一个取值 `-40.0,55.0,125.0`，
    于是这个 run 的 `ewave_dir` **预测不出来**（留空串），`run_id` 的尾巴只剩 corner，
    而 temperature 是 `encoded_in_ewave_dir=True` 的轴、永远不进 `axes_slug` ——
    也就是说"温度取了哪几个"在 `run_id` 里一个字都没有。

    只有 base 一个组时这没问题（全批次只有这一个温度列表）。一旦某个组把温度收窄：

    * 组里只剩一个温度 => 它**不**折叠，`run_id` 尾巴变成 `typical_55_0`，
      而 `axes_slug` 与 base 相同 => 两个 run 的 `--workDir` 是**同一个目录**，
      eWave 又会在里面各建一个 `typical_55_0/` => **静默覆盖**，而且两个 run_id
      不同、什么守卫都不会响（2026-08-19 复核实测）；
    * 组里剩两个温度 => 它照样折叠，尾巴同样只有 `typical` => 撞 `run_id`，
      抛的是那句"这是工具的 bug，请报告"，而用户的 spec 完全正常。

    两条都是"目录名说一套、命令行说另一套"，正是本工具存在要消灭的东西。
    真要修得让两者共存，得让折叠后的多值串进 slug —— 那会改掉**没有组**时的目录名
    （`groups=()` 必须逐字不变，golden 表守着），所以这里选择拒绝。
    """
    if not opts.native_multi_value or len(group_list) <= 1:
        return
    names = ", ".join(repr(g.name) for g in group_list[1:])
    raise SpecError(
        "options.native_multi_value cannot be combined with run groups "
        f"(groups: {names}).\n"
        "  native_multi_value collapses the whole temperature list into ONE run whose "
        "directory name cannot encode which temperatures it covers - so a group that "
        "narrows temperature lands in the SAME --workDir as the baseline run and the two "
        "silently overwrite each other.\n"
        "  Next: either drop groups: and go back to one cartesian product, or set "
        "options.native_multi_value: false and let the tool expand one run per temperature."
    )


def _all_groups(groups: Sequence[RunGroup]) -> list[RunGroup]:
    """调用方给的组 → 实际要展开的组列表（**base 永远在最前**）。

    base 组不是可选的：顶层 `axes:` 就是它，批次的基线由它产出。`groups` 为空时这里返回
    `[base]`，于是 `expand_runs` 只有一条代码路径，"没有组"和"只有 base"不会走出两种行为。

    调用方自己塞了一个叫 `base` 的组时不再另造（`core.spec` 会把 spec 里写的 `name: base`
    合并进顶层 axes，正常路径到不了这；GUI 直接构造 `BatchState` 时可能）——
    只把它挪到最前，因为顺序决定了跨组去重"留第一个"时基线归谁。
    """
    given = list(groups)
    if any(group.name == BASE_GROUP for group in given):
        return [g for g in given if g.name == BASE_GROUP] + [
            g for g in given if g.name != BASE_GROUP
        ]
    return [RunGroup(name=BASE_GROUP)] + given


def _check_groups(groups: Sequence[RunGroup]) -> None:
    """组定义本身的体检。展开之前做，错得越早越好。"""
    seen: set[str] = set()
    for group in groups:
        if not group.name.strip():
            raise SpecError(
                "A run group has an empty name.\n"
                "  The name shows up in the Runs table and in every message about that group, "
                "so it cannot be blank.\n"
                "  Next: give it a short name, e.g. name: eqcur-off"
            )
        if group.name in seen:
            raise SpecError(
                f"Run group {group.name!r} is defined twice - the second one would silently "
                "shadow the first.\n"
                "  Next: merge them into one group, or rename one of them"
            )
        seen.add(group.name)


def _check_design_keys(designs: Sequence[Design]) -> None:
    """两个 design 算出同一个 id ⇒ 产物落进同一棵目录树 ⇒ 后跑的静默覆盖先跑的。"""
    seen: dict[str, str] = {}
    for design in designs:
        key = design_key(design)
        origin = f"{design.library}/{design.cell}/{design.view}"
        if key in seen and seen[key] != origin:
            raise SpecError(
                f"Two designs produce the same id {key!r} ({seen[key]} and {origin}).\n"
                "  Their outputs would land in the same directory tree and the second run "
                "would silently overwrite the first.\n"
                "  Next: give one of them an explicit, different key:"
            )
        seen[key] = origin


# --------------------------------------------------------------------------
# 内部 helper
# --------------------------------------------------------------------------


def _dedup(values: Sequence[str]) -> list[str]:
    """去重保序。"""
    return list(dict.fromkeys(values))


def _find_value(axis: Axis, value: str) -> AxisValue | None:
    for candidate in axis.values:
        if candidate.value == value:
            return candidate
    return None


def _slug_fragment(axis: Axis, value: str) -> str:
    """一根轴在 slug 里的片段，例 `eqI-on`。"""
    known = _find_value(axis, value)
    fragment = known.slug if (known is not None and known.slug) else slugify(value)
    fields = {
        "name": axis.name,
        "short": axis.short or axis.name,
        "value": value,
        "slug": fragment,
    }
    try:
        rendered = axis.slug_template.format(**fields)
    except (KeyError, IndexError) as exc:
        raise SpecError(
            f"Axis {axis.name!r} has a slug_template {axis.slug_template!r} with an "
            f"unknown placeholder ({exc}).\n"
            "  The only ones available are: {name} {short} {value} {slug}\n"
            "  Example: slug_template: '{short}-{slug}'"
        ) from exc
    # 再过一遍 slugify：模板里可能直接插了 {value}，而取值可以是任意字符串。
    # 对默认模板 `{short}-{slug}` 是幂等的（`eqI-on` → `eqI-on`）。
    safe = slugify(rendered)
    if not safe:
        raise SpecError(
            f"Axis {axis.name!r} produced an empty slug fragment for value {value!r} - "
            "the directory name would collapse.\n"
            "  Next: give that value an explicit slug, or change slug_template"
        )
    return safe


def _catalog_twin(axis: Axis) -> Axis | None:
    """这根轴对应的内置轴（同名 + 同一套 flag + 同一种形态），没有就返回 None。

    存在的理由：spec 写 `equalCurrent: [on]` 会把轴**收窄**成只有 `on` 一个取值，
    但某个 design 仍然可以覆盖成 `[on, off]` —— `off` 那条翻译规则还在内置目录里，
    不去查就会误报"不认识的取值"。
    """
    twin = builtin_axis_catalog().get(axis.name)
    if twin is None:
        return None
    if tuple(twin.flags) != tuple(axis.flags) or twin.kind is not axis.kind:
        return None  # 同名但被用户改成了别的语义 ⇒ 不敢拿内置的翻译规则套上去
    return twin


def _materialize_value(axis: Axis, value: str) -> AxisValue:
    """取值 → `AxisValue`（轴的取值表里有就用现成的，没有就在安全的前提下现造）。"""
    known = _find_value(axis, value)
    if known is not None:
        return known
    twin = _catalog_twin(axis)
    if twin is not None:
        from_catalog = _find_value(twin, value)
        if from_catalog is not None:
            # 复制一份：`AxisValue` 是 frozen 的，但里面的 flags dict 不是，
            # 直接共享会让某个批次改到别的批次。
            return AxisValue(
                value=from_catalog.value,
                flags=dict(from_catalog.flags),
                slug=from_catalog.slug,
                label=from_catalog.label,
            )
    template = dict(axis.values[0].flags)
    homogeneous = all(dict(av.flags) == template for av in axis.values)
    has_value_placeholder = any(
        isinstance(v, str) and PLACEHOLDER_VALUE in v for v in template.values()
    )
    if homogeneous and has_value_placeholder:
        return AxisValue(value=value, flags=dict(template))
    # 报错时列**内置目录**里的全部合法取值，而不是被 spec 收窄之后剩下的那几个
    legal = " / ".join(av.value for av in (twin.values if twin is not None else axis.values))
    raise SpecError(
        f"Axis {axis.name!r} does not know the value {value!r}.\n"
        f"  Legal values: {legal}\n"
        "  (Each value of this axis contributes a different flag shape, so the tool refuses to\n"
        "   guess how a new value should translate - guessing wrong means the directory name\n"
        "   says off while the command line says on.)\n"
        "  Next: use one of the values above, or define your own axis in the spec:\n"
        "    axes:\n"
        f"      {axis.name}Custom:\n"
        "        flag: --someFlag\n"
        f"        values: [{value}]"
    )


def _check_axes(axes: Sequence[Axis]) -> None:
    """轴定义本身的体检。展开之前做，错得越早越好。"""
    seen: set[str] = set()
    for axis in axes:
        if axis.name in seen:
            raise SpecError(
                f"Axis name {axis.name!r} is defined twice - the second definition would "
                "silently shadow the first.\n"
                "  Next: merge them into one entry with all the values in a single list"
            )
        seen.add(axis.name)
        if axis.encoded_in_ewave_dir and axis.name not in EWAVE_DIR_AXES:
            raise SpecError(
                f"Axis {axis.name!r} is marked encoded_in_ewave_dir=True, but eWave only "
                f"puts {' and '.join(EWAVE_DIR_AXES)} into the directory name it builds itself.\n"
                "  Such an axis lands in neither <axes-slug> nor eWave's own directory name, so\n"
                "  two values share one directory and the second silently overwrites the first\n"
                "  (exactly the trap this tool exists to kill).\n"
                "  Next: drop encoded_in_ewave_dir (it defaults to False) so the axis goes into "
                "the slug"
            )
