"""读用户手写的 spec（YAML 或 JSON）→ `BatchSpec` → `BatchState`。

形态抄 `Auto_ext/config/tasks.yaml`（同一个用户的既有习惯）：**一个 designs 列表，
每项是 (library, cell, view…)，list 值自动展开成笛卡尔积**。

三条硬规矩：

1. **PyYAML 惰性 import**（CLAUDE.md 硬约束 2）。红区装了 6.0.1，但 CLI 不该因为它缺失就死 ——
   模块顶层**没有** `import yaml`，只有 `load_spec` 里那一处 `try: import yaml`，
   失败就走 JSON 退路，并在报错里告诉用户下一步怎么办。只准 `yaml.safe_load`。
2. **报错要人能照着改**：哪个位置、缺什么、例子长什么样、下一步做什么。
   用户范围是"先自己用，后面给同事"，一句 `KeyError: 'cell'` 是不合格的。
3. **零站点标识符**：本文件里的示例全是 `<lib>` / `<cell>` / `<view>` 这种占位符
   （CLAUDE.md 硬约束 1b）。

`EXAMPLE_SPEC` 与 `docs/spec_example.yaml` **逐字相同**（`tests/test_spec.py` 盯着这条），
改一个必须改另一个。
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import platform
import shlex
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import fields as dataclass_fields
from dataclasses import replace

from .. import __version__ as _TOOL_VERSION
from ..model import (
    BASE_GROUP,
    MECHANISM_FLAGS,
    PLACEHOLDER_VALUE,
    TIMESTAMP_FORMAT,
    USER_FORBIDDEN_FLAGS,
    Axis,
    AxisKind,
    AxisValue,
    BatchOptions,
    BatchSpec,
    BatchState,
    Design,
    FlagConflictError,
    FlagDict,
    FlagValue,
    PortMode,
    PortSpec,
    Provenance,
    RunGroup,
    RunStatus,
    SpecError,
    StreamoutTask,
)
from . import matrix

# --------------------------------------------------------------------------
# 合法键（拼错一个键就该当场报错，而不是被静默忽略）
# --------------------------------------------------------------------------

_TOP_KEYS: tuple[str, ...] = (
    "batch_name",
    "batch_root",
    "designs",
    "axes",
    "groups",
    "defaults",
    "extra_flags",
    "options",
)

_GROUP_KEYS: tuple[str, ...] = (
    "name",
    "axes",
    "label",
)

_DESIGN_KEYS: tuple[str, ...] = (
    "library",
    "cell",
    "view",
    "official_run_dir",
    "key",
    "label",
    "resources",
    "axes",
    "extra_flags",
    "ports",
    "gds_path",
)

_AXIS_KEYS: tuple[str, ...] = (
    "values",
    "flag",
    "flags",
    "value_flags",
    "short",
    "kind",
    "slug_template",
    "description",
)

_TRUE_WORDS = ("on", "true", "yes", "1")
_FALSE_WORDS = ("off", "false", "no", "0")


EXAMPLE_SPEC = '''\
# ============================================================================
# ewave_batch 批次 spec —— 可直接复制改的样例
#
# 跑法：  python -m ewave_batch dry-run <这个文件>
# 换成 JSON 也行（PyYAML 缺失时的退路）：把注释删掉、写成 .json 即可，字段完全一样。
#
# 心智模型：
#     run = (design, corner, temperature, equalCurrent, fullWave, 网格密度, …)
#   design 本身就是矩阵的一根轴；每根轴的取值列表**自动展开成笛卡尔积**。
#
# 目录长这样（<axes-slug> 只编码「在变的轴」）：
#     <batch_root>/<batch_name>/runs/<design>/<axes-slug>/<corner>_<temp>/
#   最里面那层 <corner>_<temp> 是 eWave 自己建的，我们控制不了名字。
# ============================================================================

batch_name: demo_batch          # 留空 = 用 UTC 时间戳现起一个
batch_root: ./batches           # 批次落在哪。**绝不要指到设计师的 workarea 里**

# ---------------------------------------------------------------------------
# designs —— 要提取的 (Library, Cell, view)
#
#   * library / cell / view 三个字段**都可以写成列表**，会自动展开成笛卡尔积。
#     下面这一项就是 1 个 library × 2 个 cell × 1 个 view = 2 个 design。
#   * view 不是常量：为 EM 提取派生的 cellview 和普通 layout 都可能，必须自己写清楚。
#   * official_run_dir = 官方 GUI 跑过一次的那个 design 目录。库名/端口/ptxt/队列
#     全部从那里**现场解析**，本文件里一个站点坐标都不用写。
# ---------------------------------------------------------------------------
designs:
  - library: <lib>
    cell: [<cellA>, <cellB>]
    view: <view>
    official_run_dir: <官方 GUI 跑过的那个 design 目录>
    resources: "cpu=20;mem=100000"   # per-design 的 dsub -R。端口多的电感要的比走线多

  - library: <lib>
    cell: <cellC>
    view: <view>
    official_run_dir: <另一个官方 run 目录>
    # 这个 design 单独少扫一点（没写的轴用全局取值）
    axes:
      temperature: ["25.0"]

# ---------------------------------------------------------------------------
# axes —— 设定轴。**取值列表 = 笛卡尔积的一维**
#
#   内置轴：corner / temperature / equalCurrent / fullWave / mesh /
#           relativeTolerance / relativeCurrentTolerance /
#           multiSweep / discreteFreq / parallel
#
#   ⚠️ 只有一个取值的轴**不进目录名** —— 这样只扫 corner+temp 时目录名与官方逐字一致。
#   ⚠️ 温度务必带引号且带小数位："-40.0" 和 "-40" 会让 eWave 建出**不同的**目录
#      （-40_0 vs -40）。YAML 里不加引号的 -40.0 会被读成数字，仍然是 -40.0，但
#      写 -40 就真的变成了 -40。
#   ⚠️ corner 轴会**同时**改 --corner= 和 --emssTechFile 的 ptxt 文件名，两处一起动。
# ---------------------------------------------------------------------------
axes:
  corner: [typical]                 # 只有一个取值 → 不进 slug，但照样进 eWave 的目录名
  temperature: ["-40.0", "125.0"]
  equalCurrent: [on, off]           # 开关轴：off 会把默认表里的 --equalCurrent 抵消掉

  # 自定义轴：内置目录里没有的 flag 这样加（工具不会替你猜取值该怎么翻译）
  # myKnob:
  #   flag: --someFlag              # 每个取值渲染成 --someFlag=<取值>
  #   values: ["1", "2"]
  #   short: mk                     # slug 片段写成 mk-1 / mk-2

# ---------------------------------------------------------------------------
# groups —— run group：base 之上的「单点变体」。可选，不写就只有 base 一组，
#           行为与没有这个字段时**逐字相同**。
#
#   批次 = 一列 run group，**每组各自取笛卡尔积，结果取并集**。
#   组是 base 之上的 delta：只列它覆盖的轴，没列的轴继承上面的 axes:。
#
#   为什么要它：想跑「基线 3 个温度 + 55 度那点单独关 equalCurrent + 55 度那点单独开
#   fullWave」一共 5 个 run。用笛卡尔积最接近的写法是 3 温度 × 2 eqI × 2 fw = 12 个，
#   其中 7 个是废的 —— 而一个 run 的量级是 10 核 / 100GB / 35 分钟，凑不起。
#
#   ⚠️ 加组会改掉**基线**的目录名，这是正确且不可避免的：
#      equalCurrent 本来全批次只有 on ⇒ 不进 <axes-slug> ⇒ 基线落 .../base/typical_55_0；
#      加了下面这个组之后它在变了 ⇒ 对**所有** run 进 slug ⇒ 基线变成
#      .../eqI-on/typical_55_0。不这样的话两组的 55 度会落进同一个目录、静默覆盖。
#      ⇒ 给**已经跑过**的批次加组 = 换了一批 run_id，resume 认不出老目录。
#   ⚠️ 跨组重复（两个组都写了同一组取值）会**静默去重**，只留先出现的那个。
#   ⚠️ base 是保留名：写 name: base 就是指上面那个 axes:，它的覆盖会并进 base 而不是新建组。
# ---------------------------------------------------------------------------
# groups:
#   - name: eqcur-off               # 组名，批次内唯一且非空
#     axes: {temperature: ["25.0"], equalCurrent: [off]}
#   - name: fullwave
#     axes: {temperature: ["25.0"], fullWave: [on]}

# ---------------------------------------------------------------------------
# defaults —— 默认表的**覆盖**。留空 = 全部从 official_run_dir 里的真实命令学
#             （§11 规则 1：默认表的值不写死在源码，换 PDK 自动跟上）
# ---------------------------------------------------------------------------
defaults: {}

# ---------------------------------------------------------------------------
# extra_flags —— 逃生口：别人临时要求加的、工具没做成轴的 flag
#
#   两种写法都行：
#     extra_flags: "--labelDepth=0 --printDouble"
#     extra_flags: {--labelDepth: "0", --printDouble: true}
#   写 false 表示「显式缺席」：把低层默认表里的那个 flag 抵消掉。
#
#   ⚠️ 已经是轴的 flag（如 --temperature）和工具自己管的 flag（--workDir / --gds /
#      --all / --sparam / --top / -m / --nogui / --cadencePins / --includePortOrder /
#      --emssTechFile）在这里会被**拒绝** —— 否则目录名会和实际跑的值对不上。
# ---------------------------------------------------------------------------
extra_flags: {}

# ---------------------------------------------------------------------------
# options —— 批次级开关（全部可省，下面写的就是默认值）
# ---------------------------------------------------------------------------
options:
  dry_run: false
  max_parallel: 4                 # 同时在飞的 job 数上限
  poll_interval: 15.0
  scheduler: donau                # donau | fake（fake = 本机跑假批次，不提交任何东西）
  keep_logs_on_failure: true
  include_port_order: true        # 让 .sNp 自描述端口顺序（归档后命令行就找不到了）
  verify_port_count: true         # 端口数不对就算失败。--all 的编号平移是静默的，别关
  parallel_multiplier: 1.0        # --parallel = dsub 的 cpu= × 这个倍率
'''
"""一份可直接改的示例 spec。与 `docs/spec_example.yaml` 逐字相同。"""


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def load_spec(path: str) -> BatchSpec:
    """读 YAML（或 JSON）spec。

    **PyYAML 必须惰性 import**（CLAUDE.md 硬约束 2）：`.json` 后缀或 PyYAML 缺失时走
    `json` 退路；`.yaml/.yml` 且 PyYAML 缺失 → `SpecError` 并在消息里告诉用户可以改用 JSON。
    YAML 只准用 `yaml.safe_load`。读盘，不写盘。文件不存在 / 解析失败 → `SpecError`。
    """
    text_path = os.fspath(path)
    if not os.path.isfile(text_path):
        raise SpecError(
            f"spec file not found: {text_path}\n"
            "  Next: check the path; to get a template you can edit, run\n"
            "    python -c \"import sys; from ewave_batch.core.spec import EXAMPLE_SPEC; sys.stdout.buffer.write(EXAMPLE_SPEC.encode('utf-8'))\" > my_spec.yaml"
        )
    try:
        with open(text_path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise SpecError(f"spec file could not be read: {text_path}\n  {exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SpecError(
            f"spec file is not UTF-8: {text_path}\n  {exc}\n  Next: re-save it as UTF-8"
        ) from exc

    suffix = os.path.splitext(text_path)[1].lower()
    if suffix == ".json":
        data = _load_json_text(text, text_path)
    else:
        data = _load_yaml_text(text, text_path)
    spec = parse_spec_mapping(_as_top_mapping(data, text_path), source=text_path)
    spec.source_path = text_path
    spec.source_sha256 = spec_sha256(text_path)
    return spec


def parse_spec_mapping(data: Mapping[str, object], *, source: str = "") -> BatchSpec:
    """把已经解析成 dict 的 spec 变成 `BatchSpec`（YAML/JSON 共用这一条路）。

    负责：list 值自动展开成轴、未知键报错、`USER_FORBIDDEN_FLAGS` 检查。
    纯函数，不碰文件系统 —— 单测全走它。非法输入 → `SpecError` / `FlagConflictError`。
    """
    where = source or "spec"
    if not isinstance(data, Mapping):
        raise SpecError(
            f"{where}: the top level must be a mapping (key: value), got {_typename(data)}.\n"
            "  Next: follow the shape of EXAMPLE_SPEC; at the very least it needs designs:"
        )
    _reject_unknown_keys(data, _TOP_KEYS, where=where, what="top-level field")

    raw_designs = data.get("designs")
    if raw_designs is None:
        raise SpecError(
            f"{where}: no designs: - the tool does not know which (Library, Cell, view) to extract.\n"
            "  Next: add it, e.g.\n"
            "    designs:\n"
            "      - library: <lib>\n"
            "        cell: <cell>\n"
            "        view: <view>\n"
            "        official_run_dir: <the design directory the official GUI already ran in>"
        )
    if not isinstance(raw_designs, Sequence) or isinstance(raw_designs, (str, bytes)):
        raise SpecError(
            f"{where}: designs: must be a list (one design per item), got {_typename(raw_designs)}.\n"
            "  Next: prefix each item with '- ', e.g.\n"
            "    designs:\n"
            "      - library: <lib>\n"
            "        cell: <cell>\n"
            "        view: <view>"
        )
    designs: list[Design] = []
    for index, entry in enumerate(raw_designs):
        designs.extend(_parse_design(entry, index, where))
    if not designs:
        raise SpecError(
            f"{where}: designs: is empty - that expands to 0 runs.\n  Next: write at least one design"
        )

    axes = _parse_axes(data.get("axes"), where)
    groups = _parse_groups(data.get("groups"), where)
    # 用户显式写 `name: base` 指的就是 base 组本身 —— 把它的覆盖合并进顶层 axes，
    # **不**另建一个组。否则批次里会有两个 base：一个是顶层 axes、一个是这条，
    # 展开出来的 run 一模一样、只是组名不同，跨组去重会把后者整组吃掉，看着像"这条没生效"。
    base_overrides = [g for g in groups if g.name == BASE_GROUP]
    if base_overrides:
        base_group = base_overrides[0]
        others = [g for g in groups if g.name != BASE_GROUP]
        if not others:
            # 只有 base 一条 => 直接并进顶层 axes，`groups` 保持空（文件最简单，
            # 而且 `BatchSpec.groups` 里不出现 base，与契约一致）。
            axes = matrix.axes_for_group(base_group, axes)
            groups = []
        else:
            # 还有别的组 => **不能**把 axes 收窄。顶层 `axes:` 是这个批次的**轴定义**
            # （GUI 的「Save spec as...」写出来的就是全批次并集），收窄之后别的组要用的
            # 取值就从定义里消失了 —— 一个组想换个 mesh 写法，读回来当场 SpecError，
            # 而这份文件正是本工具自己写出来的（2026-08-19 实测）。
            # 于是 base 留在组里（`matrix._all_groups` 明确支持显式 base，会把它挪到最前），
            # 同时把 base 的取值**下放**给每一个没自己覆盖这根轴的组 —— 不下放的话，
            # 那些组会继承"宽定义"，替别人多扫一遍它从没要过的取值（同一个坑，
            # `gui.state._axes_and_groups` 里有一模一样的一段）。
            for name, values in base_group.axis_overrides.items():
                for other in others:
                    other.axis_overrides.setdefault(name, tuple(values))
            groups = [base_group] + others
    _check_design_overrides(designs, axes, where)
    _check_group_overrides(groups, axes, where)

    defaults = _parse_flags(data.get("defaults"), where=f"{where}: defaults")
    _reject_flags(
        defaults, MECHANISM_FLAGS, where=f"{where}: defaults", why="computed by the tool per run"
    )

    extra_flags = _parse_flags(data.get("extra_flags"), where=f"{where}: extra_flags")
    _check_user_flags(extra_flags, axes, where=f"{where}: extra_flags")
    for design in designs:
        _check_user_flags(
            design.extra_flags, axes, where=f"{where}: design {matrix.design_key(design)} extra_flags"
        )

    options = _parse_options(data.get("options"), where)

    return BatchSpec(
        batch_name=_opt_str(data.get("batch_name"), "batch_name", where),
        batch_root=_opt_str(data.get("batch_root"), "batch_root", where),
        designs=designs,
        axes=axes,
        groups=groups,
        defaults=defaults,
        extra_flags=extra_flags,
        options=options,
        source_path=source,
    )


def spec_sha256(path: str) -> str:
    """spec 文件的 sha256（十六进制小写）。进 `Provenance.spec_sha256`，
    resume 时对不上就警告"spec 改过了"。"""
    digest = hashlib.sha256()
    try:
        with open(os.fspath(path), "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError as exc:
        raise SpecError(f"cannot compute the sha256 of the spec: {path}\n  {exc}") from exc
    return digest.hexdigest()


def spec_to_batch(spec: BatchSpec, *, batch_root: str, tool_version: str = "") -> BatchState:
    """`BatchSpec` → 全新的 `BatchState`（run 全是 `READY`，还没建目录）。

    不写盘 —— 落盘是 `core.layout.write_batch_state` 的活。
    """
    root = batch_root or spec.batch_root
    if not root:
        raise SpecError(
            "Do not know where the batch should land: the spec has no batch_root: and the "
            "command line did not give one.\n"
            "  Next: add a line batch_root: ./batches to the spec, or pass --batch-root"
        )
    name = spec.batch_name or time.strftime("batch_%Y%m%d_%H%M%S", time.gmtime())
    batch_dir = os.path.abspath(os.path.join(os.path.expanduser(root), name))
    now = time.strftime(TIMESTAMP_FORMAT, time.gmtime())

    runs = matrix.expand_runs(
        spec.designs, spec.axes, options=spec.options, groups=spec.groups
    )

    streamout: list[StreamoutTask] = []
    for design in spec.designs:
        if design.gds_path:
            # spec 直接给了 GDS ⇒ 阶段 1 的产物本来就在，标 DONE 而不是 SKIPPED：
            # SKIPPED 在 driver 那边意味着"这个 design 整列不跑"（阶段 1 失败的语义），
            # 会把整批 solve 静默吞掉。
            streamout.append(
                StreamoutTask(
                    design_key=matrix.design_key(design),
                    status=RunStatus.DONE,
                    gds_path=design.gds_path,
                    message="spec 里直接给了 gds_path，阶段 1 不用跑",
                )
            )
        else:
            streamout.append(StreamoutTask(design_key=matrix.design_key(design)))

    official_dirs = _dedup(
        [design.official_run_dir for design in spec.designs if design.official_run_dir]
    )
    provenance = Provenance(
        tool_version=tool_version or _TOOL_VERSION,
        python_version=platform.python_version(),
        created_at=now,
        updated_at=now,
        spec_path=spec.source_path,
        spec_sha256=spec.source_sha256,
        official_run_dirs=tuple(official_dirs),
    )
    return BatchState(
        batch_name=name,
        batch_dir=batch_dir,
        designs=list(spec.designs),
        # ⚠️ 存进来的轴是**全批次并集**，不是顶层 `axes:` 那份。
        # `PlanContext.axes` 就是从这里来的，而 `cmd.build_flag_layers` 拿
        # `run.axis_values[轴名]` 去轴的取值表里**查** flag —— 只存 base 那份的话，
        # 任何一个组独有的取值（`equalCurrent: [off]`）都查不到，整批 run 一起炸。
        # 展开用的仍然是 base 轴 + groups（上面那次 `expand_runs`），两者不能混。
        axes=matrix._batch_axes(spec.axes, spec.designs, spec.groups),
        groups=list(spec.groups),
        runs=runs,
        streamout=streamout,
        options=spec.options,
        defaults=dict(spec.defaults),
        extra_flags=dict(spec.extra_flags),
        provenance=provenance,
    )


# --------------------------------------------------------------------------
# 读文件：YAML 惰性 import + JSON 退路
# --------------------------------------------------------------------------


def _load_yaml_text(text: str, path: str) -> object:
    """YAML 文本 → 数据。**PyYAML 只在这里 import，而且包在 try 里。**"""
    try:
        import yaml  # noqa: PLC0415 - 惰性：CLAUDE.md 硬约束 2，CLI 不许因为缺 PyYAML 就死
    except ImportError:
        yaml = None
    if yaml is None:
        # JSON 是 YAML 的子集：有人把 JSON 存成 .yaml 也照样读得了。
        try:
            return json.loads(text)
        except ValueError:
            pass
        raise SpecError(
            f"cannot read the YAML spec: {path}\n"
            "  PyYAML is not available on this machine (the red zone has it; not being able "
            "to pip install is fine).\n"
            "  Next, pick one:\n"
            "    1) with PyYAML installed, YAML just works;\n"
            "    2) for now use a JSON spec - the field names are identical, drop the\n"
            "       comments and save as .json, e.g.\n"
            "       {\"designs\": [{\"library\": \"<lib>\", \"cell\": \"<cell>\"}]}"
        )
    try:
        return yaml.safe_load(text)  # 只准 safe_load：spec 是人手写的文本，不是可信代码
    except Exception as exc:  # yaml.YAMLError，但不 import 具体类型以免耦合
        raise SpecError(
            f"YAML syntax error: {path}\n  {exc}\n"
            "  Next: usually indentation, or a missing quote. A value that starts with a colon "
            "or a minus sign needs quotes, e.g. temperature: [\"-40.0\"]"
        ) from exc


def _load_json_text(text: str, path: str) -> object:
    try:
        return json.loads(text)
    except ValueError as exc:
        line = getattr(exc, "lineno", None)
        col = getattr(exc, "colno", None)
        spot = f"line {line} column {col}" if line else "position unknown"
        raise SpecError(
            f"JSON syntax error: {path} ({spot})\n  {exc}\n"
            "  Next: JSON allows no comments and no trailing commas, and every string is "
            "double-quoted"
        ) from exc


def _as_top_mapping(data: object, path: str) -> Mapping[str, object]:
    """顶层允许两种形状：完整 mapping，或者**光一个 designs 列表**（抄 tasks.yaml 的手感）。"""
    if isinstance(data, Mapping):
        return data
    if isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
        return {"designs": list(data)}
    raise SpecError(
        f"{path}: the spec parsed to {_typename(data)}, which is neither a mapping nor a list.\n"
        "  Next: is the file empty? Follow the shape of EXAMPLE_SPEC"
    )


# --------------------------------------------------------------------------
# designs
# --------------------------------------------------------------------------


def _parse_design(entry: object, index: int, source: str) -> list[Design]:
    """一条 design 条目 → 一个或多个 `Design`（list 值自动展开成笛卡尔积）。"""
    where = f"{source}: designs[{index}]"
    if not isinstance(entry, Mapping):
        raise SpecError(
            f"{where}: every design entry must be a mapping, got {_typename(entry)}.\n"
            "  Next: e.g.\n"
            "    designs:\n"
            "      - library: <lib>\n"
            "        cell: <cell>\n"
            "        view: <view>"
        )
    _reject_unknown_keys(entry, _DESIGN_KEYS, where=where, what="design field")

    triples: list[list[str]] = []
    for field_name in ("library", "cell", "view"):
        raw = entry.get(field_name)
        if raw is None or raw == []:
            raise SpecError(
                f"{where}: no {field_name}: - the (library, cell, view) triple must be complete "
                "(view especially cannot be omitted: a cellview derived for EM extraction is "
                "not the same thing as layout).\n"
                f"  Next: add it, e.g. {field_name}: <{field_name}>"
            )
        values = [_scalar(item, f"{where}.{field_name}") for item in _as_list(raw)]
        if any(not v for v in values):
            raise SpecError(f"{where}: {field_name}: has an empty value.\n  Next: drop it")
        triples.append(values)

    explicit_key = _opt_str(entry.get("key"), "key", where)
    combos = list(itertools.product(*triples))
    if explicit_key and len(combos) > 1:
        raise SpecError(
            f"{where}: key: is set while library/cell/view are lists (they expand to "
            f"{len(combos)} designs),\n"
            "  so all of them would share one id => they land in the same directory tree and "
            "silently overwrite each other.\n"
            "  Next: either drop key: (the tool derives <library>_<cell>_<view> automatically), "
            "or split this entry into several"
        )

    overrides = _parse_design_axes(entry.get("axes"), where)
    extra_flags = _parse_flags(entry.get("extra_flags"), where=f"{where}: extra_flags")
    ports = _parse_ports(entry.get("ports"), where)

    designs: list[Design] = []
    for library, cell, view in combos:
        designs.append(
            Design(
                library=library,
                cell=cell,
                view=view,
                official_run_dir=_opt_str(entry.get("official_run_dir"), "official_run_dir", where),
                key=explicit_key,
                resources=_opt_str(entry.get("resources"), "resources", where),
                axis_overrides=dict(overrides),
                extra_flags=dict(extra_flags),
                port_spec=ports,
                gds_path=_opt_str(entry.get("gds_path"), "gds_path", where),
                label=_opt_str(entry.get("label"), "label", where),
            )
        )
    return designs


def _parse_design_axes(raw: object, where: str) -> dict[str, tuple[str, ...]]:
    """一段 `axes:` 覆盖 → `{轴名: (取值…)}`。**design 和 run group 共用这一条路**。

    覆盖的只是取值列表，改不了 flag 定义 —— 轴的语义全批次一致，否则 slug 会对不上号。
    措辞刻意不提 "design"：`where` 已经说清是 `designs[0]` 还是 `groups[1]` 了。
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise SpecError(
            f"{where}: this axes: block must be a mapping (axis name: [values...]), "
            f"got {_typename(raw)}.\n"
            "  Next: e.g.\n    axes:\n      temperature: [\"25.0\"]"
        )
    out: dict[str, tuple[str, ...]] = {}
    for name, values in raw.items():
        items = [_scalar(v, f"{where}.axes.{name}") for v in _as_list(values)]
        if not items:
            raise SpecError(
                f"{where}: axis {name!r} is overridden with an empty list - the cartesian "
                "product collapses to nothing, so it would produce zero runs.\n"
                "  Next: give at least one value, or delete the line (deleting = inherit)"
            )
        out[str(name)] = tuple(items)
    return out


def _parse_groups(raw: object, where: str) -> list[RunGroup]:
    """顶层 `groups:` → `list[RunGroup]`（顺序 = spec 里写的顺序）。

    形状是一个 list 而不是 mapping：组是**有序**的（跨组重复留第一个），而 YAML 的 mapping
    在旧解析器上不保证顺序；顺序一变，哪个组"留下"就变了，去重结果跟着抖。
    """
    if raw is None:
        return []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise SpecError(
            f"{where}: groups: must be a list (one run group per item), got {_typename(raw)}.\n"
            "  Next: e.g.\n"
            "    groups:\n"
            "      - name: eqcur-off\n"
            '        axes: {temperature: ["55.0"], equalCurrent: [off]}'
        )
    out: list[RunGroup] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        spot = f"{where}: groups[{index}]"
        if not isinstance(entry, Mapping):
            raise SpecError(
                f"{spot}: every run group must be a mapping, got {_typename(entry)}.\n"
                "  Next: e.g.\n"
                "    groups:\n"
                "      - name: eqcur-off\n"
                '        axes: {temperature: ["55.0"], equalCurrent: [off]}'
            )
        _reject_unknown_keys(entry, _GROUP_KEYS, where=spot, what="run group field")
        name = _opt_str(entry.get("name"), "name", spot)
        if not name:
            raise SpecError(
                f"{spot}: a run group needs a name:.\n"
                "  The name shows up in the Runs table and in every message about that group.\n"
                "  Next: e.g. name: eqcur-off"
            )
        if name in seen:
            raise SpecError(
                f"{spot}: run group {name!r} is defined twice - the second one would silently "
                "shadow the first.\n"
                "  Next: merge them into one group, or rename one of them"
            )
        seen.add(name)
        overrides = _parse_design_axes(entry.get("axes"), spot)
        if not overrides and name != BASE_GROUP:
            raise SpecError(
                f"{spot}: run group {name!r} overrides nothing, so it expands to exactly the "
                "same runs as base and every one of them gets merged away.\n"
                "  A group is a delta on top of base: list only the axes it changes.\n"
                '  Next: e.g. axes: {temperature: ["55.0"], equalCurrent: [off]}'
            )
        out.append(
            RunGroup(
                name=name,
                axis_overrides=dict(overrides),
                label=_opt_str(entry.get("label"), "label", spot),
            )
        )
    return out


def spec_to_mapping(spec: BatchSpec) -> dict:
    """`BatchSpec` → 一个能被 `parse_spec_mapping` **原样读回来**的 mapping。

    这是 `parse_spec_mapping` 的反函数，也是 GUI「Save spec as…」的落笔处。

    只写**非默认**的字段：spec 是给人读、给人改的，写一堆 `resources: ""` 只会淹没重点。
    往返（dump → load → dump）必须是不动点，`tests/test_spec_dump.py` 有断言盯着 ——
    少序列化一个字段的后果是「用户在界面上设了、保存了、下次打开没了」，而且**无声**。
    """
    out: dict = {}
    if spec.batch_name:
        out["batch_name"] = spec.batch_name
    if spec.batch_root:
        out["batch_root"] = spec.batch_root

    designs: list[dict] = []
    for design in spec.designs:
        entry: dict = {"library": design.library, "cell": design.cell, "view": design.view}
        for name, value in (
            ("official_run_dir", design.official_run_dir),
            ("key", design.key),
            ("label", design.label),
            ("resources", design.resources),
            ("gds_path", design.gds_path),
        ):
            if value:
                entry[name] = value
        if design.axis_overrides:
            entry["axes"] = {k: list(v) for k, v in design.axis_overrides.items()}
        if design.extra_flags:
            entry["extra_flags"] = _flags_to_mapping(design.extra_flags)
        ports = _ports_to_spec_value(design.port_spec)
        if ports is not None:
            entry["ports"] = ports
        designs.append(entry)
    out["designs"] = designs

    if spec.axes:
        # 轴写成 `轴名: [取值…]` —— 内置轴只需要取值，flag/kind 那些由目录给。
        # 自定义轴（spec 里带 flag/flags 的）要把定义一起写回去，否则读回来就不认识它了。
        axes: dict = {}
        catalog = matrix.builtin_axis_catalog()
        for axis in spec.axes:
            values = [av.value for av in axis.values]
            builtin = catalog.get(axis.name)
            if builtin is not None and tuple(builtin.flags) == tuple(axis.flags):
                entry: object = values
            else:
                body: dict = {"values": values}
                if len(axis.flags) == 1:
                    body["flag"] = axis.flags[0]
                elif axis.flags:
                    body["flags"] = list(axis.flags)
                if axis.short:
                    body["short"] = axis.short
                if axis.description:
                    body["description"] = axis.description
                entry = body
            axes[axis.name] = _with_value_flags_if_needed(axis, entry)
        out["axes"] = axes

    if spec.groups:
        # 组只写它自己覆盖的轴（这就是「delta 不是重写」在文件里的样子）。
        # 空 overrides 的组不会出现在这里：`_parse_groups` 已经拒了它。
        groups: list[dict] = []
        for group in spec.groups:
            entry = {"name": group.name}
            if group.label:
                entry["label"] = group.label
            entry["axes"] = {k: list(v) for k, v in group.axis_overrides.items()}
            groups.append(entry)
        out["groups"] = groups

    if spec.defaults:
        out["defaults"] = _flags_to_mapping(spec.defaults)
    if spec.extra_flags:
        out["extra_flags"] = _flags_to_mapping(spec.extra_flags)

    options = _options_to_mapping(spec.options)
    if options:
        out["options"] = options
    return out


def _flag_signature(value: str, flags: FlagDict) -> dict:
    """一个取值的 flag **渲染之后**长什么样。比 flag dict 本身更接近"真正下发的命令行"。

    为什么不能直接比 dict：同一件事有两种**等价**写法 ——
    内置目录写的是模板 `{"-e": "{value}"}`（`cmd.resolve_axis_flags` 运行时把
    `{value}` 换成取值），界面自己的构造器写的是算好的 `{"-e": "0.4"}`。
    逐字比的话每一根界面造出来的轴都会被判成"读不回来"，于是 `value_flags` 逢轴必写，
    而写了 `value_flags` 的轴**就不能再现造新取值**了（`matrix._materialize_value` 要求
    整根轴的 flag 形状统一且带占位符）—— 一个组想换一个 mesh 数值就当场报错。
    2026-08-19 实测过这条连锁反应。
    """
    return {
        name: (
            raw.replace(PLACEHOLDER_VALUE, value)
            if isinstance(raw, str) and PLACEHOLDER_VALUE in raw
            else raw
        )
        for name, raw in flags.items()
    }


def _axis_survives_round_trip(axis: Axis, entry: object) -> bool:
    """把 `entry` 交给**真正的解析路径**读一遍，看每个取值渲染出来的 flag 还是不是原来那份。

    不是"照理应该一样"，是当场解析一次再逐个比 —— 这里出错的后果是无声的：
    目录名（`axes_slug`）只由取值字符串决定，flag 变了它一个字都不变，
    于是归档里那份结果声称自己跑的是它根本没跑的设定。
    """
    try:
        rebuilt = _parse_axes({axis.name: entry}, "<round-trip>")
    except SpecError:
        return False
    if len(rebuilt) != 1:  # pragma: no cover - _parse_axes 一个键只出一根轴
        return False
    got = rebuilt[0]
    if len(got.values) != len(axis.values):  # pragma: no cover - 取值是逐个抄过去的
        return False
    return all(
        a.value == b.value
        and _flag_signature(a.value, a.flags) == _flag_signature(b.value, b.flags)
        for a, b in zip(got.values, axis.values)
    )


def _with_value_flags_if_needed(axis: Axis, entry: object) -> object:
    """轴写回文件的那一项：翻译得回来就照原样，翻译不回来就补 `value_flags:`。

    翻译不回来的两类都来自界面自造的轴（`docs/INTERFACES.md`「还没冻结的东西」那条）：
    三段网格 `0.4/0.5/0.4`（`-e`/`-d`/`--viaMergeSpace` 三个值互不相同）和
    频率扫描（`--multiSweep=<串>` 外加两个 `False` 把互斥的另两种写法抵消掉）。
    不补的话读回来会变成"三个互斥的扫频 flag 同时打开"，而 `sweep_axis` 的存在
    正是为了防这一件事。
    """
    if _axis_survives_round_trip(axis, entry):
        return entry
    body = dict(entry) if isinstance(entry, Mapping) else {"values": list(entry)}  # type: ignore[arg-type]
    body["value_flags"] = {av.value: dict(av.flags) for av in axis.values}
    return body


def _flags_to_mapping(flags: FlagDict) -> dict:
    """flag dict → spec 里的写法。`True`/`False` 原样保留（`False` = 显式关掉，有语义）。"""
    return {name: value for name, value in flags.items()}


def _ports_to_spec_value(port_spec: PortSpec | None) -> object:
    """`PortSpec` → `ports:` 的值。默认（`--all`）返回 None = 不写这一项。"""
    if port_spec is None:
        return None
    if port_spec.mode is PortMode.ALL and not port_spec.mapping:
        return None
    body: dict = {"mapping": [f"{pid}={pin}" for pid, pin in port_spec.mapping]}
    if port_spec.signal_ports:
        body["signal"] = list(port_spec.signal_ports)
    return body


def _options_to_mapping(options: BatchOptions) -> dict:
    """只写和默认值**不同**的 option。"""
    default = BatchOptions()
    out: dict = {}
    for field_info in dataclass_fields(BatchOptions):
        value = getattr(options, field_info.name)
        if value != getattr(default, field_info.name):
            out[field_info.name] = list(value) if isinstance(value, tuple) else value
    return out


def have_yaml() -> bool:
    """这台机器能不能写/读 YAML。惰性探测，不在 import 时就依赖 PyYAML。

    红区装了 6.0.1；开发机和公开克隆者可能没有。`save_spec` 用它决定落哪种格式，
    **并据此决定文件扩展名** —— 见那里的说明。
    """
    try:
        import yaml  # noqa: F401, PLC0415 - 只探测存在性
    except ImportError:
        return False
    return True


def dump_spec(spec: BatchSpec, *, as_json: bool | None = None) -> str:
    """`BatchSpec` → 写进文件的文本。

    默认有 PyYAML（红区装了 6.0.1）就出 YAML —— 人能读、能改、能加注释；
    没有就出 **JSON**（字段名完全一样）。`as_json=True/False` 可以强制。

    ⚠️ **JSON 分支不加 `#` 注释头** —— JSON 没有注释语法，加了就不是合法 JSON，
    而 `load_spec` 在没有 PyYAML 的机器上正是靠 `json.loads` 读它
    ⇒ 会出现"自己写的文件自己读不回来"。YAML 分支才加（YAML 认 `#`）。
    """
    data = spec_to_mapping(spec)
    if as_json is None:
        as_json = not have_yaml()
    if as_json:
        return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    import yaml  # noqa: PLC0415 - 惰性：上面已确认可用

    # 抬头是**产物内容**（用户会去 cat / vi / grep 它），按铁律 5 走英文纯 ASCII：
    # 红区 LANG 常是 C，中文抬头在那边就是三行乱码，grep 这个文件时也匹配不上。
    # 代码注释仍然中文 —— 那是给读代码的人看的，不进产物。
    header = (
        "# ewave_batch batch spec\n"
        '# Written by "Save spec as..." in the GUI. Edit by hand, then Open it again.\n'
        "# Field reference: docs/spec_example.yaml\n"
    )
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return header + body


def save_spec(spec: BatchSpec, path: str) -> str:
    """把 spec 写到 `path`（**原子写**：同目录临时文件 + `os.replace`）。**返回真正写到的路径。**

    ## 为什么返回路径而不是文本

    `load_spec` 是**按扩展名**决定用 YAML 还是 JSON 解析的。所以在一台没有 PyYAML 的机器上
    存成 `xxx.yaml`，内容会是 JSON、而读的时候按 YAML 走 ⇒ **自己写的文件自己打不开**。
    与其让用户撞上这个，不如落盘时就把扩展名换成 `.json`，并把真实路径交回去
    （GUI 在状态栏显示它）。两种格式字段名完全一样，换机器（红区有 PyYAML）照样读得回来。

    调用方要文本的话自己 `dump_spec`。

    原子写的理由和 `batch.json` 一样：写到一半断电或磁盘满，不能留半份 spec ——
    半份 YAML 读回来要么报错、要么**语义不同**，后者更坏。
    """
    target = str(path)
    as_json = not have_yaml()
    if as_json and os.path.splitext(target)[1].lower() in (".yaml", ".yml"):
        target = os.path.splitext(target)[0] + ".json"
    text = dump_spec(spec, as_json=as_json)
    directory = os.path.dirname(os.path.abspath(target)) or "."
    os.makedirs(directory, exist_ok=True)
    handle, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(target)}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def _parse_ports(raw: object, where: str) -> PortSpec | None:
    """`ports:` → `PortSpec`。不写 = `None` = 用 `--all`（D1b 的默认）。"""
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip().lower() == "all":
        return PortSpec(mode=PortMode.ALL)
    if not isinstance(raw, Mapping):
        raise SpecError(
            f"{where}: ports: must be either all or a mapping, got {_typename(raw)}.\n"
            "  Next: e.g.\n"
            "    ports:\n"
            "      mapping: [\"P000=<pin>\", \"P001=<pin>\"]\n"
            "      signal: [<pin>]"
        )
    _reject_unknown_keys(raw, ("mode", "mapping", "signal"), where=f"{where}: ports", what="ports field")
    pairs: list[tuple[str, str]] = []
    for item in _as_list(raw.get("mapping") or []):
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) == 2:
            pairs.append((_scalar(item[0], where), _scalar(item[1], where)))
            continue
        text = _scalar(item, where)
        port, sep, pin = text.partition("=")
        if not sep or not port or not pin:
            raise SpecError(
                f"{where}: {text!r} in ports.mapping is not of the form '<port id>=<pin name>'.\n"
                "  Next: e.g. [\"P000=<pin>\", \"P001=<pin>\"].\n"
                "  WARNING: the order IS the mapping (.sNp keeps only the P00x numbers and "
                "throws the pin names away) - never sort it"
            )
        pairs.append((port, pin))
    signal = tuple(_scalar(v, where) for v in _as_list(raw.get("signal") or []))
    mode_text = _opt_str(raw.get("mode"), "mode", where) or ("explicit" if pairs else "all")
    try:
        mode = PortMode(mode_text)
    except ValueError:
        raise SpecError(
            f"{where}: ports.mode must be all or explicit, got {mode_text!r}"
        ) from None
    if mode is PortMode.ALL:
        return PortSpec(mode=PortMode.ALL)
    if not pairs:
        raise SpecError(
            f"{where}: ports.mode is explicit but no mapping was given - ewave would get no "
            "ports at all.\n"
            "  Next: give mapping: [\"P000=<pin>\", ...], or delete the whole ports: block "
            "(the default is --all)"
        )
    return PortSpec(mode=PortMode.EXPLICIT, mapping=tuple(pairs), signal_ports=signal)


# --------------------------------------------------------------------------
# axes
# --------------------------------------------------------------------------


def _parse_axes(raw: object, where: str) -> list[Axis]:
    """`axes:` → `list[Axis]`（顺序 = spec 里写的顺序，slug 片段就按这个顺序拼）。"""
    if raw is None:
        return []
    if not isinstance(raw, Mapping):
        raise SpecError(
            f"{where}: axes: must be a mapping (axis name: [values...]), got {_typename(raw)}.\n"
            "  Next: e.g.\n"
            "    axes:\n"
            "      corner: [typical]\n"
            '      temperature: ["-40.0", "125.0"]'
        )
    catalog = matrix.builtin_axis_catalog()
    out: list[Axis] = []
    for name, body in raw.items():
        axis_name = str(name)
        spot = f"{where}: axes.{axis_name}"
        values, extras = _split_axis_body(body, spot)
        if not values:
            raise SpecError(
                f"{spot}: no values at all - the cartesian product collapses to nothing.\n"
                f"  Next: give at least one value, e.g. {axis_name}: [<value>]"
            )
        value_flags = _parse_value_flags(extras.get("value_flags"), values, spot)
        custom = "flag" in extras or "flags" in extras
        if custom and axis_name in matrix.EWAVE_DIR_AXES:
            raise SpecError(
                f"{spot}: {axis_name} is a built-in axis; its flag cannot be redefined.\n"
                "  (corner has to change both --corner= and the ptxt file name inside "
                "--emssTechFile, and temperature's values are used to predict the directory "
                "eWave builds - redefining either makes the directory name say one thing while "
                "the command line says another.)\n"
                f"  Next: give values only, e.g. {axis_name}: [<value>]"
            )
        if custom:
            axis = _build_custom_axis(axis_name, values, extras, spot)
        elif axis_name in catalog:
            if value_flags is not None and all(v in value_flags for v in values):
                # value_flags 把每个取值的 flag 都写全了 => 不必再走 `axis_with_values`
                # 现造。**必须绕过它**：GUI 那种取值（`0.4/0.5/0.4` 三段网格）本来就是
                # 目录里翻不出来的，正是它们才需要 value_flags。
                _reject_duplicate_values(axis_name, values, spot)
                axis = replace(
                    catalog[axis_name],
                    values=tuple(AxisValue(v, flags=dict(value_flags[v])) for v in values),
                )
            else:
                axis = matrix.axis_with_values(catalog[axis_name], values)
            axis = _apply_axis_extras(axis, extras, spot)
        else:
            raise SpecError(
                f"{spot}: unknown axis name {axis_name!r}.\n"
                f"  Built-in axes: {', '.join(sorted(catalog))}\n"
                "  Next: use one of the above, or define it as a custom axis:\n"
                f"    {axis_name}:\n"
                "      flag: --someFlag\n"
                f"      values: {list(values)}"
            )
        if value_flags:
            axis = _with_value_flags(axis, value_flags)
        out.append(axis)
    return out


def _reject_duplicate_values(axis_name: str, values: Sequence[str], spot: str) -> None:
    """取值列表里有重复 -> 报错。绕过 `axis_with_values` 那条路时得自己做这一检查。"""
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise SpecError(
                f"{spot}: the value {value!r} is listed twice - the cartesian product would "
                "expand two identical runs (same directory, the second silently overwrites "
                "the first).\n  Next: drop the duplicate"
            )
        seen.add(value)


def _parse_value_flags(
    raw: object, values: Sequence[str], spot: str
) -> dict[str, FlagDict] | None:
    """`value_flags:` -> `{取值: flag dict}`。没写这一项返回 None。

    为什么需要这一项：轴写成 `名字: [取值…]` 时，读回来是靠**取值目录**重新翻译的，
    而目录只认得它自己列的那几个取值 —— 界面自造的 `0.4/0.5/0.4`（三段网格，
    `-e`/`-d`/`--viaMergeSpace` 各不相同）和 `--multiSweep=<一整串>` 都翻不出来，
    于是"存下来再打开"得到的是**另一组 flag**，而目录名（`axes_slug`）一模一样
    —— 归档里那份结果会声称自己跑的是它根本没跑的设定。2026-08-19 复核实测到这条。
    所以 `spec_to_mapping` 在"翻译不回来"时把每个取值的 flag 原样写出来。

    人手写 spec 时**不必**写它：形状能被目录翻出来的轴根本不会出现这一项。
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise SpecError(
            f"{spot}: value_flags: must be a mapping (value: {{flag: value}}), "
            f"got {_typename(raw)}"
        )
    known = {str(v) for v in values}
    out: dict[str, FlagDict] = {}
    for key, body in raw.items():
        value = str(key)
        if value not in known:
            raise SpecError(
                f"{spot}: value_flags mentions {value!r}, which is not in values:.\n"
                f"  values: {sorted(known)}\n"
                "  Next: drop it, or add it to values:"
            )
        out[value] = _parse_flags(body, where=f"{spot}.value_flags.{value}")
    return out


def _with_value_flags(axis: Axis, value_flags: Mapping[str, FlagDict]) -> Axis:
    """把 `value_flags` 里写的那几个取值的 flag 换掉，其余取值原样保留。"""
    return replace(
        axis,
        values=tuple(
            AxisValue(av.value, flags=dict(value_flags[av.value]))
            if av.value in value_flags
            else av
            for av in axis.values
        ),
    )


def _split_axis_body(body: object, spot: str) -> tuple[list[str], dict[str, object]]:
    """轴的两种写法：`名字: [取值…]` 或 `名字: {values: […], flag: …}`。"""
    if isinstance(body, Mapping):
        _reject_unknown_keys(body, _AXIS_KEYS, where=spot, what="axis field")
        if "values" not in body:
            raise SpecError(
                f"{spot}: an axis written as a mapping must have values:.\n"
                "  Next: e.g.\n"
                f"    {spot.rsplit('.', 1)[-1]}:\n"
                "      flag: --someFlag\n"
                "      values: [\"1\", \"2\"]"
            )
        values = [_scalar(v, spot) for v in _as_list(body["values"])]
        extras = {k: v for k, v in body.items() if k != "values"}
        return values, extras
    return [_scalar(v, spot) for v in _as_list(body)], {}


def _apply_axis_extras(axis: Axis, extras: Mapping[str, object], spot: str) -> Axis:
    """内置轴上允许微调的那几个字段（短名 / slug 模板 / 说明）。"""
    changes: dict[str, object] = {}
    if "short" in extras:
        changes["short"] = _scalar(extras["short"], spot)
    if "slug_template" in extras:
        changes["slug_template"] = _scalar(extras["slug_template"], spot)
    if "description" in extras:
        changes["description"] = _scalar(extras["description"], spot)
    if "kind" in extras:
        changes["kind"] = _parse_axis_kind(extras["kind"], spot)
    return replace(axis, **changes) if changes else axis


def _parse_axis_kind(raw: object, spot: str) -> AxisKind:
    text = _scalar(raw, spot).lower()
    try:
        return AxisKind(text)
    except ValueError:
        legal = ", ".join(kind.value for kind in AxisKind)
        raise SpecError(f"{spot}: kind must be one of {legal}, got {text!r}") from None


def _build_custom_axis(
    name: str, values: Sequence[str], extras: Mapping[str, object], spot: str
) -> Axis:
    """自定义轴：用户自己给 flag 名，工具负责把取值翻译成 flag。"""
    kind = _parse_axis_kind(extras["kind"], spot) if "kind" in extras else AxisKind.VALUE
    single_flag = _opt_str(extras.get("flag"), "flag", spot)
    flag_map: FlagDict = {}
    if "flags" in extras:
        flag_map = _parse_flags(extras["flags"], where=f"{spot}.flags")
    if single_flag:
        if not single_flag.startswith("-"):
            raise SpecError(
                f"{spot}: a flag name needs its leading dash, got {single_flag!r}.\n"
                "  Next: e.g. flag: --someFlag"
            )
        flag_map = dict(flag_map)
        flag_map.setdefault(single_flag, PLACEHOLDER_VALUE)
    if not flag_map:
        raise SpecError(f"{spot}: a custom axis needs flag: or flags:, or it changes nothing")
    _reject_flags(flag_map, USER_FORBIDDEN_FLAGS, where=spot, why="computed by the tool per run")

    axis_values: list[AxisValue] = []
    for value in values:
        if kind is AxisKind.TOGGLE:
            if not single_flag or len(flag_map) != 1:
                raise SpecError(
                    f"{spot}: a kind: toggle axis may own exactly ONE flag (adding it or not "
                    "adding it is the whole semantics).\n"
                    "  Next: write flag: --someFlag plus values: [on, off]"
                )
            axis_values.append(AxisValue(value, flags={single_flag: _toggle(value, spot)}))
        else:
            axis_values.append(AxisValue(value, flags=dict(flag_map)))
    axis = Axis(
        name=name,
        values=tuple(axis_values),
        kind=kind,
        flags=tuple(flag_map),
        short=_opt_str(extras.get("short"), "short", spot),
        description=_opt_str(extras.get("description"), "description", spot),
    )
    if "slug_template" in extras:
        axis = replace(axis, slug_template=_scalar(extras["slug_template"], spot))
    return axis


def _toggle(value: str, spot: str) -> bool:
    text = value.strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    raise SpecError(
        f"{spot}: a toggle axis only accepts {' / '.join(_TRUE_WORDS)} or "
        f"{' / '.join(_FALSE_WORDS)}, got {value!r}"
    )


# --------------------------------------------------------------------------
# flags / options
# --------------------------------------------------------------------------


def _parse_flags(raw: object, *, where: str) -> FlagDict:
    """flag 表。三种写法：mapping、列表、一整行字符串（GUI 的 Extra flags 就是一行）。"""
    if raw is None or raw == "" or raw == {} or raw == []:
        return {}
    if isinstance(raw, Mapping):
        out: FlagDict = {}
        for key, value in raw.items():
            name = str(key).strip()
            if not name.startswith("-"):
                raise SpecError(
                    f"{where}: a flag name needs its leading dash, got {name!r}.\n"
                    "  Next: e.g. {\"--labelDepth\": \"0\"}"
                )
            out[name] = _flag_value(value, where)
        return out
    if isinstance(raw, str):
        return _flag_tokens(shlex.split(raw), where)
    if isinstance(raw, Sequence):
        tokens: list[str] = []
        for item in raw:
            tokens.extend(shlex.split(_scalar(item, where)))
        return _flag_tokens(tokens, where)
    raise SpecError(
        f"{where}: unrecognised shape ({_typename(raw)}).\n"
        "  Next: write a mapping ({\"--labelDepth\": \"0\"}) or a single string line "
        "(\"--labelDepth=0\")"
    )


def _flag_value(value: object, where: str) -> FlagValue:
    """flag 的取值：`True` = 裸 flag，`False` = **显式缺席**（把低层默认抵消掉）。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return True
    return _scalar(value, where)


def _flag_tokens(tokens: Sequence[str], where: str) -> FlagDict:
    """`--a=1 --b -e 0.4` → `{"--a": "1", "--b": True, "-e": "0.4"}`。"""
    out: FlagDict = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-"):
            raise SpecError(
                f"{where}: {token!r} looks neither like a flag (no leading dash) nor like the "
                "value of the flag before it.\n"
                "  Next: e.g. \"--labelDepth=0 -e 0.4 --printDouble\""
            )
        if "=" in token:
            name, _, value = token.partition("=")
            out[name] = value
            index += 1
        elif token.startswith("--"):
            out[token] = True
            index += 1
        elif index + 1 < len(tokens) and not _looks_like_flag(tokens[index + 1]):
            out[token] = tokens[index + 1]
            index += 2
        else:
            out[token] = True
            index += 1
    return out


def _looks_like_flag(token: str) -> bool:
    """`-e 0.4` 里的 `0.4` 不是 flag，`-40.0` 也不是 —— 负数长得像短 flag。"""
    if not token.startswith("-"):
        return False
    body = token[1:]
    try:
        float(body if not body.startswith("-") else body[1:])
    except ValueError:
        return True
    return False


def _parse_options(raw: object, where: str) -> BatchOptions:
    if raw is None:
        return BatchOptions()
    if not isinstance(raw, Mapping):
        raise SpecError(
            f"{where}: options: must be a mapping, got {_typename(raw)}.\n"
            "  Next: e.g.\n    options:\n      max_parallel: 4"
        )
    legal = {field.name: field for field in dataclass_fields(BatchOptions)}
    _reject_unknown_keys(raw, tuple(sorted(legal)), where=f"{where}: options", what="option")
    kwargs: dict[str, object] = {}
    for key, value in raw.items():
        name = str(key)
        default = legal[name].default
        spot = f"{where}: options.{name}"
        if name == "timeout_seconds":
            kwargs[name] = None if value is None else _number(value, spot, float)
        elif isinstance(default, bool):
            kwargs[name] = _bool(value, spot)
        elif isinstance(default, int):
            kwargs[name] = _number(value, spot, int)
        elif isinstance(default, float):
            kwargs[name] = _number(value, spot, float)
        elif isinstance(default, str):
            kwargs[name] = _scalar(value, spot)
        else:  # archive_keep 之类的 tuple
            kwargs[name] = tuple(_scalar(v, spot) for v in _as_list(value))
    return BatchOptions(**kwargs)  # type: ignore[arg-type]


def _bool(value: object, spot: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    raise SpecError(f"{spot}: expected true/false, got {value!r}")


def _number(value: object, spot: str, cast: type) -> object:
    if isinstance(value, bool):
        raise SpecError(f"{spot}: expected a number, got {value!r}")
    try:
        return cast(value)
    except (TypeError, ValueError):
        raise SpecError(f"{spot}: expected a number, got {value!r}") from None


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------


def _check_user_flags(flags: Mapping[str, FlagValue], axes: Sequence[Axis], *, where: str) -> None:
    """用户层的 flag 体检：机制层 flag 一律拒绝；已经是轴的 flag 也拒绝（§11 规则 2）。"""
    _reject_flags(flags, USER_FORBIDDEN_FLAGS, where=where, why="computed by the tool per run")
    owned: dict[str, str] = {}
    for axis in axes:
        for flag in axis.flags:
            owned.setdefault(flag, axis.name)
    for name in flags:
        if name in owned:
            raise FlagConflictError(
                f"{where}: {name} is already owned by axis {owned[name]!r}, it cannot be "
                "given here a second time.\n"
                "  Otherwise the directory name and the value actually used drift apart - that "
                "is the root cause of the native GUI's overwrite trap, and rebuilding it here "
                "is not an option (BRIEF 11, rule 2).\n"
                f"  Next: express it as an axis, e.g.\n    axes:\n      {owned[name]}: [<value>]"
            )


def _reject_flags(
    flags: Mapping[str, FlagValue], forbidden: frozenset[str], *, where: str, why: str
) -> None:
    hits = [name for name in flags if name in forbidden]
    if hits:
        raise FlagConflictError(
            f"{where}: these flags are {why}, the user layer may not set them: "
            f"{', '.join(sorted(hits))}\n"
            "  Changing them breaks the tool's own mechanism (--workDir is the entire means of "
            "avoiding silent overwrites, --all is the entire basis for port mapping without "
            "the GUI).\n"
            "  Next: remove them"
        )


def _check_design_overrides(designs: Sequence[Design], axes: Sequence[Axis], where: str) -> None:
    """per-design 的 `axes:` 覆盖了不存在的轴 → 当场报，别等到展开的时候。"""
    known = {axis.name for axis in axes}
    for design in designs:
        unknown = sorted(name for name in design.axis_overrides if name not in known)
        if unknown:
            raise SpecError(
                f"{where}: the axes: under design {matrix.design_key(design)!r} overrides axes "
                f"the top level never defined: {', '.join(unknown)}\n"
                f"  axes defined at the top level: {', '.join(sorted(known)) or '(none at all)'}\n"
                "  Next: define it under the top-level axes: first (a single value is enough), "
                "then override it in the design"
            )


def _check_group_overrides(groups: Sequence[RunGroup], axes: Sequence[Axis], where: str) -> None:
    """组的 `axes:` 覆盖了不存在的轴 → 当场报，别等到展开的时候。

    与 `_check_design_overrides` 同形。分开写是因为「下一步怎么办」不同：
    design 那边是"先在顶层定义"，组这边还要提醒它是 delta（没列的轴自动继承 base）。
    """
    known = {axis.name for axis in axes}
    for group in groups:
        unknown = sorted(name for name in group.axis_overrides if name not in known)
        if unknown:
            raise SpecError(
                f"{where}: run group {group.name!r} overrides axes the top level never "
                f"defined: {', '.join(unknown)}\n"
                f"  axes defined at the top level: {', '.join(sorted(known)) or '(none at all)'}\n"
                "  Next: define it under the top-level axes: first (a single value is enough); "
                "a group only lists the axes it changes, everything else is inherited"
            )


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _reject_unknown_keys(
    data: Mapping[str, object], legal: Sequence[str], *, where: str, what: str
) -> None:
    unknown = sorted(str(key) for key in data if str(key) not in legal)
    if unknown:
        raise SpecError(
            f"{where}: unknown {what}: {', '.join(unknown)}\n"
            f"  {what}s that exist: {', '.join(sorted(legal))}\n"
            "  Next: most likely a typo (the tool deliberately refuses to ignore unknown keys - "
            "a misspelled key means that line silently did nothing)"
        )


def _as_list(value: object) -> list[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return [value]


def _scalar(value: object, where: str) -> str:
    """标量 → 字符串。

    ⚠️ YAML 1.1 把 `on` / `off` / `yes` / `no` 读成 bool —— 开关轴写 `[on, off]` 时
    到这里已经是 `[True, False]` 了，必须还原成 `on` / `off`，否则轴取值会变成 `True`。
    """
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    raise SpecError(
        f"{where}: unrecognised value {value!r} ({_typename(value)}).\n"
        "  Next: write a string or a number; do not nest a list where a list is already expected"
    )


def _opt_str(value: object, field_name: str, where: str) -> str:
    if value is None:
        return ""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        raise SpecError(f"{where}: {field_name} takes exactly one value, got a list {value!r}")
    return _scalar(value, f"{where}.{field_name}")


def _typename(value: object) -> str:
    return type(value).__name__


def _dedup(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))
