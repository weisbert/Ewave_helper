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
    "defaults",
    "extra_flags",
    "options",
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
            f"spec 文件不存在: {text_path}\n"
            "  下一步：确认路径拼对了；要一份可以照着改的样例就跑\n"
            "    python -c \"from ewave_batch.core.spec import EXAMPLE_SPEC; print(EXAMPLE_SPEC)\" > my_spec.yaml"
        )
    try:
        with open(text_path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise SpecError(f"spec 文件读不了: {text_path}\n  {exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SpecError(
            f"spec 文件不是 UTF-8: {text_path}\n  {exc}\n  下一步：用 UTF-8 重存一次"
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
            f"{where}: 顶层要是一个 mapping（key: value），实际是 {_typename(data)}。\n"
            "  下一步：照 EXAMPLE_SPEC 的形状写，至少要有 designs:"
        )
    _reject_unknown_keys(data, _TOP_KEYS, where=where, what="顶层字段")

    raw_designs = data.get("designs")
    if raw_designs is None:
        raise SpecError(
            f"{where}: 缺 designs: —— 不知道要提取哪个 (Library, Cell, view)。\n"
            "  下一步：加上，例：\n"
            "    designs:\n"
            "      - library: <lib>\n"
            "        cell: <cell>\n"
            "        view: <view>\n"
            "        official_run_dir: <官方 GUI 跑过的那个 design 目录>"
        )
    if not isinstance(raw_designs, Sequence) or isinstance(raw_designs, (str, bytes)):
        raise SpecError(
            f"{where}: designs: 要是一个列表（每项一个 design），实际是 {_typename(raw_designs)}。\n"
            "  下一步：每项前面加 '- '，例：\n"
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
            f"{where}: designs: 是空的 —— 展开出来 0 个 run。\n  下一步：至少写一个 design"
        )

    axes = _parse_axes(data.get("axes"), where)
    _check_design_overrides(designs, axes, where)

    defaults = _parse_flags(data.get("defaults"), where=f"{where}: defaults")
    _reject_flags(defaults, MECHANISM_FLAGS, where=f"{where}: defaults", why="工具自己按 run 算的")

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
        raise SpecError(f"算不了 spec 的 sha256: {path}\n  {exc}") from exc
    return digest.hexdigest()


def spec_to_batch(spec: BatchSpec, *, batch_root: str, tool_version: str = "") -> BatchState:
    """`BatchSpec` → 全新的 `BatchState`（run 全是 `READY`，还没建目录）。

    不写盘 —— 落盘是 `core.layout.write_batch_state` 的活。
    """
    root = batch_root or spec.batch_root
    if not root:
        raise SpecError(
            "不知道批次要落在哪：spec 里没写 batch_root:，命令行也没给。\n"
            "  下一步：spec 里加一行 batch_root: ./batches，或者用 --batch-root 指定"
        )
    name = spec.batch_name or time.strftime("batch_%Y%m%d_%H%M%S", time.gmtime())
    batch_dir = os.path.abspath(os.path.join(os.path.expanduser(root), name))
    now = time.strftime(TIMESTAMP_FORMAT, time.gmtime())

    runs = matrix.expand_runs(spec.designs, spec.axes, options=spec.options)

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
        axes=list(spec.axes),
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
            f"读不了 YAML spec: {path}\n"
            "  这台机器上没有 PyYAML（红区是装了的，pip 装不了也没关系）。\n"
            "  下一步二选一：\n"
            "    1) 装了 PyYAML 就能用 YAML；\n"
            "    2) 现在请用 JSON spec —— 字段名完全一样，把注释删掉写成 .json 即可，\n"
            "       例：{\"designs\": [{\"library\": \"<lib>\", \"cell\": \"<cell>\", \"view\": \"<view>\"}]}"
        )
    try:
        return yaml.safe_load(text)  # 只准 safe_load：spec 是人手写的文本，不是可信代码
    except Exception as exc:  # yaml.YAMLError，但不 import 具体类型以免耦合
        raise SpecError(
            f"YAML 语法错误: {path}\n  {exc}\n"
            "  下一步：多半是缩进或者少了引号。带冒号/减号开头的取值要加引号，"
            '例：temperature: ["-40.0"]'
        ) from exc


def _load_json_text(text: str, path: str) -> object:
    try:
        return json.loads(text)
    except ValueError as exc:
        line = getattr(exc, "lineno", None)
        col = getattr(exc, "colno", None)
        spot = f"第 {line} 行第 {col} 列" if line else "位置未知"
        raise SpecError(
            f"JSON 语法错误: {path}（{spot}）\n  {exc}\n"
            "  下一步：JSON 不许有注释、不许有多余的逗号，字符串一律双引号"
        ) from exc


def _as_top_mapping(data: object, path: str) -> Mapping[str, object]:
    """顶层允许两种形状：完整 mapping，或者**光一个 designs 列表**（抄 tasks.yaml 的手感）。"""
    if isinstance(data, Mapping):
        return data
    if isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
        return {"designs": list(data)}
    raise SpecError(
        f"{path}: spec 解析出来是 {_typename(data)}，既不是 mapping 也不是列表。\n"
        "  下一步：文件是不是空的？照 EXAMPLE_SPEC 的形状写"
    )


# --------------------------------------------------------------------------
# designs
# --------------------------------------------------------------------------


def _parse_design(entry: object, index: int, source: str) -> list[Design]:
    """一条 design 条目 → 一个或多个 `Design`（list 值自动展开成笛卡尔积）。"""
    where = f"{source}: designs[{index}]"
    if not isinstance(entry, Mapping):
        raise SpecError(
            f"{where}: 每一项 design 要是一个 mapping，实际是 {_typename(entry)}。\n"
            "  下一步：例\n"
            "    designs:\n"
            "      - library: <lib>\n"
            "        cell: <cell>\n"
            "        view: <view>"
        )
    _reject_unknown_keys(entry, _DESIGN_KEYS, where=where, what="design 字段")

    triples: list[list[str]] = []
    for field_name in ("library", "cell", "view"):
        raw = entry.get(field_name)
        if raw is None or raw == []:
            raise SpecError(
                f"{where}: 缺 {field_name}: —— (library, cell, view) 三元组必须齐"
                "（view 尤其不能省，为 EM 提取派生的 cellview 和 layout 不是一回事）。\n"
                f"  下一步：补上，例 {field_name}: <{field_name}>"
            )
        values = [_scalar(item, f"{where}.{field_name}") for item in _as_list(raw)]
        if any(not v for v in values):
            raise SpecError(f"{where}: {field_name}: 里有空取值。\n  下一步：删掉空的那一项")
        triples.append(values)

    explicit_key = _opt_str(entry.get("key"), "key", where)
    combos = list(itertools.product(*triples))
    if explicit_key and len(combos) > 1:
        raise SpecError(
            f"{where}: 写了 key: 又把 library/cell/view 写成了列表（会展开成 {len(combos)} 个 design），\n"
            "  它们会共用同一个 id ⇒ 落进同一棵目录树、互相静默覆盖。\n"
            "  下一步：要么去掉 key:（工具会按 <library>_<cell>_<view> 自动起），"
            "要么把这一项拆成多条"
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
    """design 底下的 `axes:` = per-design 的取值覆盖（不能改 flag 定义）。"""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise SpecError(
            f"{where}: design 底下的 axes: 要是 mapping（轴名: [取值…]），"
            f"实际是 {_typename(raw)}。\n"
            "  下一步：例\n    axes:\n      temperature: [\"25.0\"]"
        )
    out: dict[str, tuple[str, ...]] = {}
    for name, values in raw.items():
        items = [_scalar(v, f"{where}.axes.{name}") for v in _as_list(values)]
        if not items:
            raise SpecError(
                f"{where}: 轴 {name!r} 被覆盖成了空列表 —— 这个 design 一个 run 都不会有。\n"
                "  下一步：给至少一个取值，或者删掉这一行（删掉 = 用全局取值）"
            )
        out[str(name)] = tuple(items)
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
                axes[axis.name] = values
                continue
            body: dict = {"values": values}
            if len(axis.flags) == 1:
                body["flag"] = axis.flags[0]
            elif axis.flags:
                body["flags"] = list(axis.flags)
            if axis.short:
                body["short"] = axis.short
            if axis.description:
                body["description"] = axis.description
            axes[axis.name] = body
        out["axes"] = axes

    if spec.defaults:
        out["defaults"] = _flags_to_mapping(spec.defaults)
    if spec.extra_flags:
        out["extra_flags"] = _flags_to_mapping(spec.extra_flags)

    options = _options_to_mapping(spec.options)
    if options:
        out["options"] = options
    return out


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

    header = (
        "# ewave_batch batch spec\n"
        "# 由 GUI 的「Save spec as…」写出。可以直接手改，也可以再 Open 回去。\n"
        "# 字段含义见 docs/spec_example.yaml。\n"
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
            f"{where}: ports: 要么写 all，要么是一个 mapping，实际是 {_typename(raw)}。\n"
            "  下一步：例\n"
            "    ports:\n"
            "      mapping: [\"P000=<pin>\", \"P001=<pin>\"]\n"
            "      signal: [<pin>]"
        )
    _reject_unknown_keys(raw, ("mode", "mapping", "signal"), where=f"{where}: ports", what="ports 字段")
    pairs: list[tuple[str, str]] = []
    for item in _as_list(raw.get("mapping") or []):
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) == 2:
            pairs.append((_scalar(item[0], where), _scalar(item[1], where)))
            continue
        text = _scalar(item, where)
        port, sep, pin = text.partition("=")
        if not sep or not port or not pin:
            raise SpecError(
                f"{where}: ports.mapping 里的 {text!r} 不是 '<端口号>=<pin 名>' 的形状。\n"
                "  下一步：例 [\"P000=<pin>\", \"P001=<pin>\"]。\n"
                "  ⚠️ 顺序就是映射本身（.sNp 里只留 P00x 编号，pin 名会被丢掉），别随手排序"
            )
        pairs.append((port, pin))
    signal = tuple(_scalar(v, where) for v in _as_list(raw.get("signal") or []))
    mode_text = _opt_str(raw.get("mode"), "mode", where) or ("explicit" if pairs else "all")
    try:
        mode = PortMode(mode_text)
    except ValueError:
        raise SpecError(
            f"{where}: ports.mode 只能是 all 或 explicit，实际是 {mode_text!r}"
        ) from None
    if mode is PortMode.ALL:
        return PortSpec(mode=PortMode.ALL)
    if not pairs:
        raise SpecError(
            f"{where}: ports.mode 是 explicit 但没给 mapping —— ewave 一个端口都不会有。\n"
            "  下一步：给 mapping: [\"P000=<pin>\", …]，或者把 ports: 整段删掉（默认 --all）"
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
            f"{where}: axes: 要是 mapping（轴名: [取值…]），实际是 {_typename(raw)}。\n"
            "  下一步：例\n"
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
                f"{spot}: 一个取值都没有 —— 笛卡尔积会塌成空集。\n"
                f"  下一步：给至少一个取值，例 {axis_name}: [<取值>]"
            )
        custom = "flag" in extras or "flags" in extras
        if custom and axis_name in matrix.EWAVE_DIR_AXES:
            raise SpecError(
                f"{spot}: {axis_name} 是内置轴，它的 flag 不许自己定义。\n"
                "  （corner 要同时改 --corner= 和 --emssTechFile 的 ptxt 文件名，"
                "temperature 的取值还要用来预测 eWave 建的目录名 —— 改了就会"
                "「目录名说一套、命令行说另一套」）\n"
                f"  下一步：只给取值，例 {axis_name}: [<取值>]"
            )
        if custom:
            axis = _build_custom_axis(axis_name, values, extras, spot)
        elif axis_name in catalog:
            axis = matrix.axis_with_values(catalog[axis_name], values)
            axis = _apply_axis_extras(axis, extras, spot)
        else:
            raise SpecError(
                f"{spot}: 不认识的轴名 {axis_name!r}。\n"
                f"  内置轴：{', '.join(sorted(catalog))}\n"
                "  下一步：改成上面之一，或者把它定义成自定义轴：\n"
                f"    {axis_name}:\n"
                "      flag: --someFlag\n"
                f"      values: {list(values)}"
            )
        out.append(axis)
    return out


def _split_axis_body(body: object, spot: str) -> tuple[list[str], dict[str, object]]:
    """轴的两种写法：`名字: [取值…]` 或 `名字: {values: […], flag: …}`。"""
    if isinstance(body, Mapping):
        _reject_unknown_keys(body, _AXIS_KEYS, where=spot, what="轴字段")
        if "values" not in body:
            raise SpecError(
                f"{spot}: 写成 mapping 的轴必须有 values:。\n"
                "  下一步：例\n"
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
        raise SpecError(f"{spot}: kind 只能是 {legal}，实际是 {text!r}") from None


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
                f"{spot}: flag 名要带前导横杠，实际是 {single_flag!r}。\n"
                "  下一步：例 flag: --someFlag"
            )
        flag_map = dict(flag_map)
        flag_map.setdefault(single_flag, PLACEHOLDER_VALUE)
    if not flag_map:
        raise SpecError(f"{spot}: 自定义轴要给 flag: 或 flags:，否则这根轴什么都不改")
    _reject_flags(flag_map, USER_FORBIDDEN_FLAGS, where=spot, why="工具自己按 run 算的")

    axis_values: list[AxisValue] = []
    for value in values:
        if kind is AxisKind.TOGGLE:
            if not single_flag or len(flag_map) != 1:
                raise SpecError(
                    f"{spot}: kind: toggle 的轴只能有**一个** flag（加/不加它就是全部语义）。\n"
                    "  下一步：写成 flag: --someFlag + values: [on, off]"
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
        f"{spot}: 开关轴的取值只能是 {' / '.join(_TRUE_WORDS)} 或 {' / '.join(_FALSE_WORDS)}，"
        f"实际是 {value!r}"
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
                    f"{where}: flag 名要带前导横杠，实际是 {name!r}。\n"
                    "  下一步：例 {\"--labelDepth\": \"0\"}"
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
        f"{where}: 认不出的写法（{_typename(raw)}）。\n"
        "  下一步：写成 mapping（{\"--labelDepth\": \"0\"}）或者一行字符串（\"--labelDepth=0\"）"
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
                f"{where}: {token!r} 不像 flag（没有前导横杠），也不像上一个 flag 的取值。\n"
                "  下一步：例 \"--labelDepth=0 -e 0.4 --printDouble\""
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
            f"{where}: options: 要是 mapping，实际是 {_typename(raw)}。\n"
            "  下一步：例\n    options:\n      max_parallel: 4"
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
    raise SpecError(f"{spot}: 要 true/false，实际是 {value!r}")


def _number(value: object, spot: str, cast: type) -> object:
    if isinstance(value, bool):
        raise SpecError(f"{spot}: 要数字，实际是 {value!r}")
    try:
        return cast(value)
    except (TypeError, ValueError):
        raise SpecError(f"{spot}: 要数字，实际是 {value!r}") from None


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------


def _check_user_flags(flags: Mapping[str, FlagValue], axes: Sequence[Axis], *, where: str) -> None:
    """用户层的 flag 体检：机制层 flag 一律拒绝；已经是轴的 flag 也拒绝（§11 规则 2）。"""
    _reject_flags(flags, USER_FORBIDDEN_FLAGS, where=where, why="工具自己按 run 算的")
    owned: dict[str, str] = {}
    for axis in axes:
        for flag in axis.flags:
            owned.setdefault(flag, axis.name)
    for name in flags:
        if name in owned:
            raise FlagConflictError(
                f"{where}: {name} 已经是轴 {owned[name]!r} 管的 flag，不能再在这里写一遍。\n"
                "  否则目录名会和实际跑的值对不上 —— 那正是原生 GUI 覆盖坑的根因，"
                "不能自己再造一遍（§11 规则 2）。\n"
                f"  下一步：把它当轴写，例\n    axes:\n      {owned[name]}: [<取值>]"
            )


def _reject_flags(
    flags: Mapping[str, FlagValue], forbidden: frozenset[str], *, where: str, why: str
) -> None:
    hits = [name for name in flags if name in forbidden]
    if hits:
        raise FlagConflictError(
            f"{where}: 这些 flag 是{why}，用户层不许写：{', '.join(sorted(hits))}\n"
            "  改了它们工具自身机制就失效（--workDir 是绕开静默覆盖的全部手段，"
            "--all 是端口映射不依赖 GUI 的全部依据）。\n"
            "  下一步：删掉它们"
        )


def _check_design_overrides(designs: Sequence[Design], axes: Sequence[Axis], where: str) -> None:
    """per-design 的 `axes:` 覆盖了不存在的轴 → 当场报，别等到展开的时候。"""
    known = {axis.name for axis in axes}
    for design in designs:
        unknown = sorted(name for name in design.axis_overrides if name not in known)
        if unknown:
            raise SpecError(
                f"{where}: design {matrix.design_key(design)!r} 底下的 axes: "
                f"覆盖了顶层没定义的轴: {', '.join(unknown)}\n"
                f"  顶层定义了的轴: {', '.join(sorted(known)) or '（一个都没有）'}\n"
                "  下一步：先在顶层 axes: 里定义它（哪怕只有一个取值），再在 design 里覆盖"
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
            f"{where}: 不认识的{what}: {', '.join(unknown)}\n"
            f"  能写的{what}: {', '.join(sorted(legal))}\n"
            "  下一步：多半是拼错了（工具故意不静默忽略未知字段 —— 拼错一个键就等于那行没生效）"
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
        f"{where}: 认不出的取值 {value!r}（{_typename(value)}）。\n"
        "  下一步：写成字符串/数字；一整个列表的地方别嵌套列表"
    )


def _opt_str(value: object, field_name: str, where: str) -> str:
    if value is None:
        return ""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        raise SpecError(f"{where}: {field_name} 只能有一个取值，实际给了一个列表 {value!r}")
    return _scalar(value, f"{where}.{field_name}")


def _typename(value: object) -> str:
    return type(value).__name__


def _dedup(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))
