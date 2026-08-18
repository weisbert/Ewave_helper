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


def varying_axes(axes: Sequence[Axis], *, designs: Sequence[Design] = ()) -> list[Axis]:
    """挑出**真正在变**的轴（取值 > 1 个的）。

    slug 只编码变的那些：只扫 corner+temp 时目录名与官方逐字一致，设计师才认得（BRIEF §5）。
    给了 `designs` 就把 per-design 的 `axis_overrides` 也算进去（某个 design 上多出来的取值
    同样算"在变"）。

    ⚠️ 这是个**过滤器**，最容易犯的错是"顺手多滤掉一类"：
    `encoded_in_ewave_dir=True` 的 corner/temperature 在这里**必须照样返回** ——
    它们进不进 slug 是 `compute_axes_slug` 的事，不是"在不在变"的事。
    返回的是**原对象**（不是副本），调用方可以拿 `is` 比。
    """
    return [axis for axis in axes if len(effective_axis_values(axis, designs)) > 1]


def effective_axis_values(axis: Axis, designs: Sequence[Design] = ()) -> list[str]:
    """这根轴在**整个批次**上实际会取到的取值（去重、保序）。

    非冻结面（P1 内部 helper，`FROZEN` 里没有它 —— 并行夜跑不许改 `model.py`）。

    没给 designs 就是轴自己的取值；给了就按每个 design 的 `axis_overrides` 算并集：
    某个 design 覆盖了它、别的 design 没覆盖 ⇒ 全局取值和覆盖取值都会出现在这个批次里。
    """
    global_values = [av.value for av in axis.values]
    if not designs:
        return _dedup(global_values)
    seen: list[str] = []
    for design in designs:
        override = design.axis_overrides.get(axis.name)
        seen.extend([str(v) for v in override] if override else global_values)
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
    known = {axis.name for axis in axes}
    unknown = [name for name in design.axis_overrides if name not in known]
    if unknown:
        raise SpecError(
            f"design {design_key(design)!r} 覆盖了不存在的轴: {', '.join(sorted(unknown))}\n"
            f"  这个批次定义了的轴: {', '.join(sorted(known)) or '（一个都没有）'}\n"
            "  下一步：把 design 下面的 axes: 里那个名字改成上面之一，"
            "或者先在顶层 axes: 里定义它"
        )
    out: list[Axis] = []
    for axis in axes:
        override = design.axis_overrides.get(axis.name)
        if override is None:
            out.append(axis)
            continue
        if not override:
            raise SpecError(
                f"design {design_key(design)!r} 把轴 {axis.name!r} 覆盖成了空列表 —— "
                "笛卡尔积会塌成空集，这个 design 一个 run 都不会有。\n"
                "  下一步：给至少一个取值，或者直接删掉这条覆盖（删掉 = 用全局取值）"
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
                f"轴 {axis.name!r} 的取值列表里 {text!r} 出现了两次 —— "
                "笛卡尔积会展开出两个一模一样的 run（同一个目录，第二个静默覆盖第一个）。\n"
                "  下一步：去掉重复的那个"
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
) -> list[Run]:
    """展开笛卡尔积 → `list[Run]`，每个 Run 带好 `run_id` / `axes_slug` / `ewave_dir`。

    * design 本身是矩阵的一个轴（BRIEF §5「矩阵模型」）。
    * per-design 的 `axis_overrides` 生效（走 `axes_for_design`）。
    * `options.native_multi_value=True` 时**不**展开 corner/temperature（交给 eWave 原生多值，D12）。
    * `work_dir` 这里不填（要 batch_dir 才知道），由 `core.layout.compute_run_paths` 补。

    不写盘。重复的 (design, 取值组合) → `SpecError`。

    展开顺序：design 在外，轴按传进来的顺序（第一根轴变得最慢）。同一份输入永远同样的顺序 ——
    `runs.csv` 和 dry-run 的输出才可以逐行 diff。
    """
    opts = options if options is not None else BatchOptions()
    axis_list = list(axes)
    design_list = list(designs)
    _check_axes(axis_list)
    if not design_list:
        raise SpecError(
            "一个 design 都没有 —— 矩阵是空的，展开出来 0 个 run。\n"
            "  下一步：spec 的 designs: 至少要有一项，例：\n"
            "    designs:\n"
            "      - library: <lib>\n"
            "        cell: <cell>\n"
            "        view: <view>"
        )

    # slug 的口径是**全批次**的：某根轴只在 design B 上多了一个取值，它在 design A 的 slug 里
    # 也照样出现，否则同一批次里两个 design 的目录名规则不一样，没法比对（BRIEF §5）。
    slug_axes = [
        axis_with_values(axis, effective_axis_values(axis, design_list)) for axis in axis_list
    ]

    runs: list[Run] = []
    seen: dict[str, str] = {}
    seen_design_keys: dict[str, str] = {}
    for design in design_list:
        key = design_key(design)
        origin = f"{design.library}/{design.cell}/{design.view}"
        if key in seen_design_keys and seen_design_keys[key] != origin:
            raise SpecError(
                f"两个 design 算出同一个 id {key!r}（{seen_design_keys[key]} 和 {origin}）——\n"
                "  它们的产物会落进同一棵目录树，后跑的静默覆盖先跑的。\n"
                "  下一步：给其中一个 design 显式写一个不同的 key:"
            )
        seen_design_keys[key] = origin

        design_axes = axes_for_design(design, axis_list)
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
            if run_id in seen:
                raise SpecError(
                    f"两个 run 算出同一个 run_id {run_id!r}：\n"
                    f"  {seen[run_id]}\n  {axis_values}\n"
                    "  同一个 run_id = 同一个落地目录 = 第二个静默覆盖第一个"
                    "（正是本工具要消灭的坑）。\n"
                    "  下一步：检查轴的取值列表里有没有重复项"
                )
            seen[run_id] = str(axis_values)
            runs.append(
                Run(
                    run_id=run_id,
                    design_key=key,
                    axis_values=axis_values,
                    axes_slug=slug,
                    ewave_dir=ewave_dir,
                )
            )
    return runs


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
            f"轴 {axis.name!r} 的 slug_template {axis.slug_template!r} 用了认不出的占位符 ({exc})。\n"
            "  能用的只有: {name} {short} {value} {slug}\n"
            "  例：slug_template: '{short}-{slug}'"
        ) from exc
    # 再过一遍 slugify：模板里可能直接插了 {value}，而取值可以是任意字符串。
    # 对默认模板 `{short}-{slug}` 是幂等的（`eqI-on` → `eqI-on`）。
    safe = slugify(rendered)
    if not safe:
        raise SpecError(
            f"轴 {axis.name!r} 在取值 {value!r} 上拼出了空的 slug 片段 —— 目录名会塌掉。\n"
            "  下一步：给这个取值一个显式 slug，或换个 slug_template"
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
        f"轴 {axis.name!r} 不认识取值 {value!r}。\n"
        f"  合法取值：{legal}\n"
        "  （这根轴的每个取值贡献的 flag 写法不一样，工具不敢替你猜新取值该翻译成什么 ——\n"
        "    猜错的后果是「目录名说 off、命令行说 on」）\n"
        "  下一步：改成上面的取值之一，或者在 spec 里自定义一根轴：\n"
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
                f"轴名 {axis.name!r} 定义了两次 —— 后一份会悄悄盖掉前一份。\n"
                "  下一步：合并成一条，把取值写在同一个列表里"
            )
        seen.add(axis.name)
        if axis.encoded_in_ewave_dir and axis.name not in EWAVE_DIR_AXES:
            raise SpecError(
                f"轴 {axis.name!r} 标了 encoded_in_ewave_dir=True，但 eWave 只把 "
                f"{' 和 '.join(EWAVE_DIR_AXES)} 写进它自己那层目录名。\n"
                "  这样这根轴既不进 <axes-slug>、又不进 eWave 的目录名 ⇒ 两个取值落同一个目录、\n"
                "  第二个静默覆盖第一个（正是本工具要消灭的坑）。\n"
                "  下一步：把 encoded_in_ewave_dir 去掉（默认 False），让它正常进 slug"
            )
