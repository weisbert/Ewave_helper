"""ewave_batch 的**接口冻结面** —— 数据结构 + 跨模块函数签名。

Phase 0 的产物。这里只有契约，没有实现：

* **数据结构**（dataclass / Enum）是真契约，字段写全，实现方直接用。
* **跨模块函数**只有 `def f(...) -> T:` + docstring + `raise NotImplementedError`。
  真身由 P1–P5 写在各自模块里，**签名必须逐字一致**（连返回注解都要抄）。
* `FROZEN` 是机器可读的清单，`python -m ewave_batch dry-run --self-test` 靠它做漂移检测。

改冻结接口的流程见 `docs/INTERFACES.md`：同一个 commit 改 model.py + INTERFACES.md +
全部调用方，message 标 `[interface-change]`。**静默漂移是禁止的，改一个错的冻结不是。**

两条写代码时必须记住的项目宪法（CLAUDE.md 硬约束）：

1. **源码里零站点标识符。** 本文件里出现的一切具体取值都是 eWave/Cadence 的**工具语义**
   （flag 名、`0.4` 这种数值、`gdsout_setup` 的非路径字段），不是站点身份。
   library / cell / view / 路径 / 队列 / 账号 一律没有默认值，运行时从官方 run 目录解析
   （见 `SiteFacts`）。
2. **只用 stdlib。** 唯一例外是读用户手写 YAML spec 时惰性 import PyYAML + JSON 退路
   （见 `ewave_batch.core.spec`）。目标解释器是红区的 **Python 3.11.4**，本机 3.10 也要能跑，
   所以不用 3.11+ 的新 API（例如 `enum.StrEnum`、`typing.Self`）。
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Protocol, runtime_checkable

# --------------------------------------------------------------------------
# 版本
# --------------------------------------------------------------------------

INTERFACE_VERSION = 2
"""冻结接口的版本。任何 `[interface-change]` commit 都要 +1。

* 1 → 2（P2 复审返工）：**只加符号 + 改注释，没动任何已有签名** ——
  `core.cmd.KEY_FLAG`、`tools.strmout` 的 7 个跨模块符号、新模块条目
  `ewave_batch.redzone_dryrun`，外加 `render_ports` 的 docstring 与实现对齐
  （`ALL` → 返回空 list，`--all` 由机制层出）。老调用方一个都不用改。
"""

SCHEMA_VERSION = 1
"""`batch.json` 的 schema 版本。读到更大的值要拒绝而不是猜。"""


# --------------------------------------------------------------------------
# 类型别名
# --------------------------------------------------------------------------

FlagValue = str | bool
"""一个 flag 的取值。

* `str`  —— 带值的 flag。长 flag 渲染成 `--x=value`，短 flag 渲染成 `-e 0.4` 两个 argv 项。
* `True` —— 裸 flag（`--nogui` / `-m` / `--all`）。
* `False` —— **显式缺席**：该 flag 必须不出现在 argv 里。
  这个取值不是摆设 —— 生产默认表里带 `--equalCurrent`，而 equalCurrent 轴的 "off" 取值
  必须能把低层的默认**抵消掉**，否则目录名说 off、命令行说 on，正是我们要消灭的那个坑。
"""

FlagDict = dict[str, FlagValue]
"""flag 名 → 取值。键是**带前导横杠的原名**（`--corner` / `-e`），不做归一。"""


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------


class EwaveBatchError(Exception):
    """本工具所有自定义异常的根。CLI 顶层只捕获它，别的异常一律让它炸出 traceback。"""


class SpecError(EwaveBatchError):
    """用户给的 spec / 数据结构自身非法（缺字段、轴取值为空、design 三元组不全）。"""


class DiscoveryError(EwaveBatchError):
    """从官方 run 目录解析坐标失败（目录不存在、没有 gdsout_setup、字段缺失）。"""


class FlagConflictError(EwaveBatchError):
    """四层合并时发现非法冲突（Extra flags 里出现了已是轴或机制的 flag）。"""


class StateError(EwaveBatchError):
    """`batch.json` 读写失败或 schema 版本不认识。"""


class SchedulerError(EwaveBatchError):
    """提交 / 轮询 / 取消失败，或调度器输出解析不了。"""


class VerificationError(EwaveBatchError):
    """产物验收失败且调用方要求硬失败时抛。driver 默认把它转成 `RunStatus.FAILED`。"""


class ToolMissingError(EwaveBatchError):
    """`ewave` / `strmout` / `dsub` 在 PATH 上找不到。dry-run 不该抛这个。"""


# --------------------------------------------------------------------------
# 枚举
# --------------------------------------------------------------------------


class RunStatus(enum.Enum):
    """一个 run 的状态。**恰好 6 个**（PROJECT_BRIEF §12，用户 2026-08-18 定）。

    `ready` 是"还没提交"，`pending` 是"已 dsub、在排队"——`pending` 用的是 Donau 自己的词，
    不要把两者合并，resume 要靠它们区分"该提交"和"该轮询"。
    """

    READY = "ready"
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class JobState(enum.Enum):
    """调度器视角的 job 状态。**不等于 `RunStatus`** —— job 说 done 只代表进程结束了，
    run 说 done 必须产物验过（BRIEF §12：eWave 崩了也 exit=0，还会留 0 字节文件报 done）。
    """

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class Stage(enum.Enum):
    """两阶段 DAG 的阶段。"""

    STREAMOUT = "streamout"
    """阶段 1：per-design，`strmout` 出 GDS。整个设定矩阵共用（D1a）。"""

    SOLVE = "solve"
    """阶段 2：per-design × per-组合，`ewave` 出 .sNp。可并行。"""


class AxisKind(enum.Enum):
    """轴的形态，只影响 slug 渲染与界面控件，不影响合并语义。"""

    VALUE = "value"
    """取值型：corner / temperature / parallel / tolerance / 频率扫描。"""

    TOGGLE = "toggle"
    """开关型：equalCurrent / fullWave。"off" 取值要用 `False` 抵消默认表。"""

    GROUP = "group"
    """一个取值同时改多个 flag：网格密度（`-e` / `-d` / `--viaMergeSpace`）。"""


class FlagLayer(enum.Enum):
    """四层参数暴露面（BRIEF §11）+ 机制层。合并顺序见 `FlagLayers.MERGE_ORDER`。"""

    BUILTIN = "builtin"
    """内置默认：源码里写死的通用值，只在没学到默认表时兜底。"""

    DEFAULTS = "defaults"
    """默认表：第一次运行时从官方 run 目录**学**来的（§11 规则 1），不写死在源码。"""

    EXTRA = "extra"
    """逃生口：用户在 Advanced 里手打的 `Extra ewave flags`。"""

    AXIS = "axis"
    """轴：这个 run 的身份，会进目录名。"""

    LOCKED = "locked"
    """机制层：`--workDir` / `--gds` / `--all` 这些工具自己算的，最后落笔，用户改不了。"""


class EventKind(enum.Enum):
    """driver 往外播的事件类型。CLI 打印它，GUI 刷新表格。"""

    PLANNED = "planned"
    SUBMITTED = "submitted"
    STARTED = "started"
    FINISHED = "finished"
    FAILED = "failed"
    SKIPPED = "skipped"
    ARCHIVED = "archived"
    INFO = "info"
    WARNING = "warning"


class PortMode(enum.Enum):
    """端口映射怎么给 eWave。"""

    ALL = "all"
    """`--all`：eWave 自己在 GDS 里找端口，按 lexicographical order 编号。
    已验证它逐位复现官方 `-p` 顺序（BRIEF §5），这是"不依赖 GUI"成立的全部依据。"""

    EXPLICIT = "explicit"
    """显式 `-p`/`-i`。`--all` 表达不了接地端口，这是 D1b 留的口子。"""


# --------------------------------------------------------------------------
# 常量（全是 eWave/Cadence 的工具语义，不是站点身份 —— 见 CLAUDE.md 硬约束 1b）
# --------------------------------------------------------------------------

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
"""所有落盘时间戳的格式：UTC、秒精度、ISO-8601。别在 `batch.json` 里写本地时间。"""

BASE_SLUG = "base"
"""没有额外轴时 `<axes-slug>` 的取值（BRIEF §5）。此时 `runs/<design>/base/<corner>_<temp>/`
的内层与官方目录同构，只多插了一层常量 —— 不重复、不跟工具对抗。"""

AXIS_SLUG_SEP = "__"
"""轴之间的分隔符。**双下划线** —— 单下划线已经被温度占用（`-40.0` → `-40_0`）。"""

TEMP_DECIMAL_REPLACEMENT = "_"
"""温度里的小数点换成什么：`-40.0` → `-40_0`。这是 eWave 自己的约定，我们只是复现。"""

MECHANISM_FLAGS: frozenset[str] = frozenset(
    {
        "--workDir",
        "--gds",
        "--top",
        "--sparam",
        "--all",
        "--includePortOrder",
        "--cadencePins",
        "--nogui",
        "-m",
    }
)
"""工具自己按 run 算出来的 flag（§11「锁死」层）。

用户层（spec 的 extra flags / GUI 的逃生口）里出现任何一个 → `FlagConflictError`。
改了它们工具自身机制就失效：`--workDir` 是我们绕开"同 corner/temp 静默覆盖"的全部手段，
`--all` 是端口映射不依赖 GUI 的全部依据。
"""

USER_FORBIDDEN_FLAGS: frozenset[str] = MECHANISM_FLAGS | frozenset({"--emssTechFile"})
"""Extra flags 里一律拒绝的 flag。

比 `MECHANISM_FLAGS` 多一个 `--emssTechFile`：它虽然"锁死"（用户不该手打），
但 **corner 轴要改它**（§7「corner 轴要同时改两处」）—— 所以它能由轴层写，不能由用户层写。
"""

LONG_FLAG_ASSIGN = "="
"""长 flag 的赋值形式：`--corner=typical`（一个 argv 项）。
短 flag 是两项：`-e`, `0.4`。渲染规则见 `ewave_batch.core.cmd.render_flags`。"""

PLACEHOLDER_VALUE = "{value}"
"""`AxisValue.flags` 里的占位符：替换成该轴取值本身。"""

PLACEHOLDER_PTXT = "{ptxt}"
"""`AxisValue.flags` 里的占位符：替换成该 corner 对应的 ptxt 绝对路径
（`ewave_batch.core.discover.ptxt_path_for_corner`）。**只有 corner 轴用得上。**
这两个是**仅有的**两个占位符 —— 想加第三个就是接口变更。"""

BATCH_JSON_NAME = "batch.json"
RUNS_CSV_NAME = "runs.csv"
GDS_DIRNAME = "gds"
GDSOUT_DIRNAME = "gdsout"
SPARAM_DIRNAME = "sparam"
RUNS_DIRNAME = "runs"
LOGS_DIRNAME = "logs"
CMD_SH_NAME = "cmd.sh"
RUN_LOG_NAME = "run.log"
"""退路名字：只在 `<corner>_<temp>` 预测不出来时用（corner/temperature 没都当轴扫）。"""

CMD_SH_TEMPLATE = "cmd_{stem}.sh"
RUN_LOG_TEMPLATE = "run_{stem}.log"
"""★ 每个 run 一份命令留档 / 一份日志，`{stem}` = `<corner>_<temp>`。

**为什么不是固定的 `cmd.sh`**（BRIEF §5 的树画的是固定名，这里是刻意偏离）：
`<axes-slug>` 按定义**不含** corner/temp，而 `<corner>_<temp>/` 是 eWave 在 `--workDir`
里面自己建的 —— 于是同一个 axes-slug 下的 N 个 corner/temp 组合共用一个 `run_dir`。
固定名 `cmd.sh` 会让这 N 个 run 的命令行**互相覆盖**，最后只剩最后一个跑的那条。
而每条命令的 `--corner` / `--temperature` / `--emssTechFile` 都不同 ⇒ 留档直接失真，
「这个结果是拿什么命令跑出来的」再也答不上来。

**静默覆盖正是本工具要消灭的东西**（BRIEF §5「原生 GUI 的真实痛点」痛点 1），
在归档层自己再造一个同样的坑说不过去。

取这个名字也不是新发明：官方 run 目录里 `run_ewave_<corner>_<temp>.sh` 和
`run_ewave_<corner>_<temp>.log` 就是 `<corner>_<temp>/` 的**同级兄弟**，形状一模一样。
"""

GDSOUT_SETUP_SUFFIX = ".gdsout_setup"
"""归档布局的固定名字（BRIEF §5「归档布局」）。别在别处重复写字符串字面量。"""

RUNS_CSV_COLUMNS: tuple[str, ...] = (
    "design",
    "run_id",
    "axes_slug",
    "ewave_dir",
    "status",
    "job_id",
    "submitted_at",
    "ended_at",
    "wall_seconds",
    "peak_memory_mb",
    "port_count",
    "converged",
    "sparam",
    "message",
)
"""`runs.csv` 的表头。**冻结** —— 下游（v2 的批量对比）按列名读，加列只许往后追加。"""


# --------------------------------------------------------------------------
# 数据结构：输入面
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PortSpec:
    """端口映射的表达。默认就是 `--all`（D1b）。

    `mapping` 的**顺序即 `-p` 的顺序**，而 Touchstone 只按 P00x 编号排列、名字被丢掉 ——
    所以顺序就是全部信息（BRIEF §5「端口映射不在 .sNp 里，在命令行里」）。
    """

    mode: PortMode = PortMode.ALL
    mapping: tuple[tuple[str, str], ...] = ()
    """`(port_id, pin_name)` 序列，例如 `(("P000", "<pin>"), ...)`。`ALL` 模式下为空。"""
    signal_ports: tuple[str, ...] = ()
    """`-i` 列出的 signal port（其余为接地端口）。`ALL` 模式下为空 = 全部是 signal。"""

    def __post_init__(self) -> None:
        if self.mode is PortMode.EXPLICIT and not self.mapping:
            raise SpecError("PortSpec(EXPLICIT) 必须给 mapping —— 否则 ewave 一个端口都不会有")


@dataclass(frozen=True)
class AxisValue:
    """轴上的一个取值。

    `flags` 是这个取值**贡献的全部 flag**，可以是多个（GROUP 轴），也可以带 `False`
    来抵消低层默认（TOGGLE 轴的 off）。值里可以出现 `PLACEHOLDER_VALUE` / `PLACEHOLDER_PTXT`，
    由 `ewave_batch.core.cmd.resolve_axis_flags` 在拿到 `SiteFacts` 之后替换。
    """

    value: str
    """用户可读的取值（`"typical"` / `"-40.0"` / `"on"`）。也是 spec 里写的那个字面量。"""
    flags: FlagDict = field(default_factory=dict)
    slug: str = ""
    """slug 片段。空 = 由 `Axis.slug_template` + `value` 现算（见 core.matrix.compute_axes_slug）。"""
    label: str = ""
    """界面上显示的名字。空 = 用 `value`。"""


@dataclass
class Axis:
    """一根设定轴 = 矩阵的一维。轴清单见 BRIEF §10（用户 2026-08-18 给出）+ §11「界面」层。

    ⚠️ **corner 轴同时改两个 flag**（`--corner=` 和 `--emssTechFile=` 的 ptxt 文件名，
    见 §7），所以 `flags` 是元组而不是单个字符串 —— 冲突检测和 Extra flags 拒绝都靠它。
    """

    name: str
    """轴名，spec 里的键（`"corner"` / `"temperature"` / `"equalCurrent"`）。"""
    values: tuple[AxisValue, ...]
    kind: AxisKind = AxisKind.VALUE
    flags: tuple[str, ...] = ()
    """这根轴掌管的全部 flag 名。用户在 Extra flags 里碰这些 → 拒绝（§11 规则 2）。"""
    short: str = ""
    """slug 里的短名（`"eqI"` / `"fw"` / `"mesh"`）。空 = 用 `name`。"""
    slug_template: str = "{short}-{slug}"
    """slug 片段模板。可用占位符：`{name}` `{short}` `{value}` `{slug}`。"""
    encoded_in_ewave_dir: bool = False
    """True = 这根轴已经被 eWave 自己编进 `<corner>_<temp>/` 那层目录名了
    （只有 corner 和 temperature 是 True）。**这种轴不进 `<axes-slug>`**，
    否则目录名里会出现两遍（BRIEF §5「归档布局」）。"""
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise SpecError("Axis.name 不能为空")
        if not self.values:
            raise SpecError(f"Axis {self.name!r} 一个取值都没有 —— 笛卡尔积会塌成空集")


@dataclass
class Design:
    """一个待提取的 design = (Library, Cell, view) + 它自己的官方 run 目录。

    每个 design 有**自己的**模板：自己的端口集合、自己的 GDS、自己的 top cell，
    所以坐标是 per-design 解析的（`official_run_dir` → `SiteFacts`），不是全局一份。
    """

    library: str
    cell: str
    view: str
    """⚠️ view 不是常量。官方样本里有 `layout` 也有为 EM 提取专门派生的 cellview，
    必须是用户输入的一部分（BRIEF §5）。"""
    official_run_dir: str = ""
    """官方 GUI 跑过的那个 design 目录（含 `gdsout_setup`）。坐标全从这里现场解析，
    不手抄（CLAUDE.md 硬约束 1b）。空 = 用批次级的那份 `SiteFacts`。"""
    key: str = ""
    """目录名用的稳定 id。空 = `ewave_batch.core.matrix.design_key()` 现算。"""
    resources: str = ""
    """per-design 的 dsub `-R` 覆盖（`"cpu=20;mem=100000"` 形状）。
    ⚠️ 必须 per-design 可覆盖：端口多的电感和端口少的走线资源需求差很远（BRIEF §5）。"""
    axis_overrides: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """轴名 → 该 design 单独用的取值列表。没写的轴用全局定义。"""
    extra_flags: FlagDict = field(default_factory=dict)
    """per-design 的逃生口。同样受 `USER_FORBIDDEN_FLAGS` 约束。"""
    port_spec: PortSpec | None = None
    """显式 `-p`/`-i` 覆盖（D1b 留的口子，`--all` 表达不了接地端口）。None = 用 `--all`。"""
    gds_path: str = ""
    """已经有 GDS 时直接给路径 → 跳过阶段 1。空 = 我们自己 `strmout`。"""
    label: str = ""

    def __post_init__(self) -> None:
        missing = [n for n, v in (("library", self.library), ("cell", self.cell), ("view", self.view)) if not v]
        if missing:
            raise SpecError(f"Design 缺字段: {', '.join(missing)}（三元组必须齐，view 尤其不能省）")


@dataclass
class BatchOptions:
    """批次级开关。**不含任何站点坐标**，可以有默认值。"""

    dry_run: bool = False
    """只打印 argv 和落地目录，不提交、不建目录、不删文件（D8）。"""
    max_parallel: int = 4
    """同时在飞的 job 数上限。license 不是瓶颈（D11），配额才是。"""
    poll_interval: float = 15.0
    """轮询间隔（秒）。CLI 用 `while` 驱动，GUI 用 `after()` 驱动同一份 driver 代码。"""
    scheduler: str = "donau"
    """`"donau"` / `"fake"`。`"fake"` 是本机跑假批次用的（P3）。"""
    native_multi_value: bool = False
    """D12：用 eWave 原生多值（`--temperature=a,b,c`）而不是展开成独立 run。
    ⚠️ 该 option 的实际行为**未验证**，第一次用之前先实测一次。默认 False = 独立 run。"""
    stop_design_on_streamout_failure: bool = True
    """阶段 1 失败 → 该 design 整列 `skipped`（fail-fast，不去提交必然失败的 job）。
    阶段 2 的失败**不** fail-fast：一个组合挂了其余照跑（§12）。"""
    keep_logs_on_failure: bool = True
    """失败时保留 `ewave.log` / `emsolver.log` 做诊断（D5）。"""
    archive_keep: tuple[str, ...] = ("*.s[0-9]*p",)
    """归档保留哪些参数文件（fnmatch 模式，相对 `<corner>_<temp>/`）。
    默认这一条同时盖住 `.sNp` 和 `_sample.sNp`。`.yNp` 暂不留 —— P4c 未定，
    用户明确只要 S 参数（D5）。要留就在 spec 里加模式，别改代码。"""
    parallel_multiplier: float = 1.0
    """`--parallel` = `cpu=` × 这个倍率。⚠️ eWave 的 `--parallel` **不**必须等于 dsub 的 `cpu=`
    （那是 ALPS 的规矩，照抄会损失一半算力）。红区当前实际是 1:1（BRIEF §6）。"""
    include_port_order: bool = True
    """开 `--includePortOrder=1`（D1d）。归档会把 .sNp 搬离原始命令行，
    而端口映射只存在于命令行 → 必须让 SNP 自描述。生产不开，我们开。"""
    verify_port_count: bool = True
    """产物验收时核对端口数。`--all` 的代价是 pin 集合一变所有编号平移，而且**静默**
    （BRIEF §5）→ 这个校验是防线，别关。"""
    timeout_seconds: float | None = None
    """单个 run 的墙钟上限，None = 不限。eWave 没有 license 排队机制，卡住只能自己管。"""


@dataclass
class Provenance:
    """这个批次是谁、在什么时候、用什么东西生成的。resume 和事后追溯都靠它。"""

    tool_version: str = ""
    interface_version: int = INTERFACE_VERSION
    schema_version: int = SCHEMA_VERSION
    python_version: str = ""
    created_at: str = ""
    updated_at: str = ""
    spec_path: str = ""
    spec_sha256: str = ""
    """来源 spec 的 hash。spec 改了而 batch.json 没重建 → resume 时要警告。"""
    official_run_dirs: tuple[str, ...] = ()
    """坐标来自哪些官方 run 目录（运行时值，只存在于红区的 batch.json 里）。"""
    notes: tuple[str, ...] = ()


@dataclass
class BatchSpec:
    """用户手写的 spec 解析之后的样子（YAML 或 JSON，见 `ewave_batch.core.spec`）。

    与 `BatchState` 的分工：spec 是**输入**（人写、可读、可带注释），
    state 是**输出**（机器写、频繁原子重写、resume 靠它）。BRIEF §5「spec 用 YAML / state 用 JSON」。
    """

    batch_name: str = ""
    batch_root: str = ""
    designs: list[Design] = field(default_factory=list)
    axes: list[Axis] = field(default_factory=list)
    defaults: FlagDict = field(default_factory=dict)
    """默认表的覆盖。留空 = 全部从官方 run 目录学（§11 规则 1）。"""
    extra_flags: FlagDict = field(default_factory=dict)
    """逃生口。含 `USER_FORBIDDEN_FLAGS` 里的键 → `FlagConflictError`。"""
    options: BatchOptions = field(default_factory=BatchOptions)
    source_path: str = ""
    source_sha256: str = ""


# --------------------------------------------------------------------------
# 数据结构：运行时解析出来的站点坐标
# --------------------------------------------------------------------------


@dataclass
class SiteFacts:
    """从**一个官方 run 目录**解析出来的全部站点坐标。

    🚨 **这个结构里装的全是站点身份 → 它只在运行时存在，所有字段的默认值必须是空。**
    源码里绝不许出现任何一个真实取值（CLAUDE.md 硬约束 1b）。样板见 `mvp/redzone/cfg.sh`：
    坐标不手抄、现场解析，既没有标识符进仓库，也没有抄错的可能。

    来源（`ewave_batch.core.discover.discover_site_facts`）：

    | 文件 | 解析出什么 |
    |---|---|
    | `gdsout_setup` | library / top_cell / view / layer_map / gdsout 模板 |
    | `run_ewave_*.sh` | ptxt / key / corner / temperature / 整串 `-p`/`-i` / 生产 flag |
    | `remote_run_ewave.sh` | dsub 的 `-A` / `-q` / `-R` |
    | `--emssTechFile` 路径倒推 | pdk_root / ptxt 目录 / corner 文件名模板 |
    | `command -v` | ewave / strmout 的实际路径（版本目录不写死） |
    """

    official_run_dir: str = ""
    library: str = ""
    top_cell: str = ""
    view: str = ""
    layer_map: str = ""
    gdsout_template: str = ""
    """把官方 `gdsout_setup` 的 7 个随 design 变的字段换成占位符之后的模板文本。
    D1c：其余字段**逐字复现** —— `convertPin "geometry"` / `case "preserve"` /
    `maxVertices 200` 任何一个错了，GDS 内容就变了，而且跑得出来、数字也像。"""
    ptxt: str = ""
    """官方那条命令里的 `--emssTechFile` 原值。"""
    ptxt_dir: str = ""
    ptxt_name_template: str = ""
    """ptxt 文件名里的 corner 换成 `{corner}` 之后的模板。corner 轴要靠它换文件名（§7）。"""
    pdk_root: str = ""
    key: str = ""
    corner: str = ""
    temperature: str = ""
    ewave_dir_name: str = ""
    """官方那次 run 的 `<corner>_<temp>` 目录名，用来校验我们的拼法一致。"""
    official_command_line: str = ""
    """`run_ewave_*.sh` 里那条完整 ewave 命令（原样）。红区 dry-run 的自带比对拿它当基准。"""
    official_flags: FlagDict = field(default_factory=dict)
    official_port_spec: PortSpec = field(default_factory=PortSpec)
    production_flags: FlagDict = field(default_factory=dict)
    """官方实际在用的、去掉站点相关项之后的 flag 集合 —— 默认表就是从这儿学的（§11 规则 1）。"""
    dsub_account: str = ""
    dsub_queue: str = ""
    dsub_resources: str = ""
    ewave_bin: str = ""
    strmout_bin: str = ""
    ewave_version: str = ""
    source_files: dict[str, str] = field(default_factory=dict)
    """字段名 → 它是从哪个文件解析出来的。出错时能一眼看出该去改哪个文件。"""
    warnings: tuple[str, ...] = ()
    """解析时的软失败（某个 flag 没找到之类）。硬失败直接抛 `DiscoveryError`。"""


@dataclass(frozen=True)
class GdsoutFields:
    """渲染 `gdsout_setup` 模板要填的 7 个字段（D1c）。

    这 7 个是**随 design 变的**；模板里其余字段一律逐字复现，一个都不许"顺手改成更合理的值"。
    """

    run_dir: str
    library: str
    top_cell: str
    view: str
    strm_file: str
    log_file: str
    layer_map: str


# --------------------------------------------------------------------------
# 数据结构：命令拼装
# --------------------------------------------------------------------------


@dataclass
class FlagLayers:
    """参数暴露面的四层 + 机制层（BRIEF §11）。

    合并顺序见 `MERGE_ORDER`：**内置默认 < 默认表 < Extra flags < 轴 < 机制**。
    轴永远赢用户层，因为轴是 run 的身份（会进目录名）；机制层最后落笔，因为改了它工具就废了。
    """

    builtin: FlagDict = field(default_factory=dict)
    defaults: FlagDict = field(default_factory=dict)
    extra: FlagDict = field(default_factory=dict)
    axis: FlagDict = field(default_factory=dict)
    locked: FlagDict = field(default_factory=dict)

    MERGE_ORDER: ClassVar[tuple[FlagLayer, ...]] = (
        FlagLayer.BUILTIN,
        FlagLayer.DEFAULTS,
        FlagLayer.EXTRA,
        FlagLayer.AXIS,
        FlagLayer.LOCKED,
    )
    """后者覆盖前者。**这个顺序是契约，不是实现细节** —— 改它等于改工具语义。"""


@dataclass(frozen=True)
class FlagConflict:
    """一条冲突。`detect_flag_conflicts` 返回它们，调用方决定是警告还是 `FlagConflictError`。"""

    flag: str
    reason: str
    """人能看懂的一句话，例如「`--temperature` 已经是轴，不许在 Extra flags 里再给一遍」。"""
    layer: FlagLayer = FlagLayer.EXTRA
    axis_name: str = ""
    value: FlagValue | None = None
    fatal: bool = True


@dataclass(frozen=True)
class FlagDelta:
    """一个 flag 在两边取值不同。"""

    flag: str
    actual: FlagValue | None
    expected: FlagValue | None


@dataclass(frozen=True)
class FlagDiff:
    """逐 flag 的集合 diff 结果。golden 测试和红区 dry-run 的自带比对**共用**这一个结构。

    ⚠️ `compared_count` 不是装饰：空集合的 diff 永远是绿的。MVP 里踩过一次 ——
    排除规则写 `--sparam` 前缀误伤了 `--sparamImpedance`，两边同时被跳过，
    diff 空得非常好看，但根本没比。**测试必须断言 `compared_count` 等于从 fixture 数出来的条数。**
    """

    only_actual: tuple[str, ...] = ()
    """我们多给的 flag。"""
    only_expected: tuple[str, ...] = ()
    """参考命令里有、我们漏掉的 flag。"""
    differing: tuple[FlagDelta, ...] = ()
    same: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()
    """被 `ignore` 排除掉的 flag 名（**精确匹配**命中的那些）。测试要断言它没多吃。"""
    compared_count: int = 0
    """真正参与比较的 flag 条数 = `len(same) + len(differing) + len(only_*)`。"""

    @property
    def clean(self) -> bool:
        """三个差异桶全空才叫一致。"""
        return not (self.only_actual or self.only_expected or self.differing)


@dataclass(frozen=True)
class PortDiff:
    """端口顺序比对。`--all` 的代价是 pin 集合一变所有编号平移，而且静默（BRIEF §5）。"""

    matched: bool = False
    first_mismatch_index: int | None = None
    only_actual: tuple[str, ...] = ()
    only_expected: tuple[str, ...] = ()
    compared_count: int = 0


@dataclass
class ParsedCommand:
    """一条现成 ewave 命令行解析出来的东西（`ewave_batch.core.template`）。

    两个用途：①「导入一条现成命令当模板」的可选入口（D3）；②golden test / 红区自带比对的基准。
    字段形状与 `tests/fixtures/production_cmd.local.json` 对齐 —— fixture 是人抽的，
    **实现方不许反过来改 fixture 去迁就代码**。
    """

    program: str = ""
    flags: FlagDict = field(default_factory=dict)
    port_spec: PortSpec = field(default_factory=PortSpec)
    positional: tuple[str, ...] = ()
    raw: str = ""


@dataclass(frozen=True)
class CommandPlan:
    """一条要执行（或只打印）的命令 —— 两个阶段共用这一个结构。"""

    argv: tuple[str, ...]
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)
    """**增量**环境变量，不是整份 environ。空 dict = 原样继承。"""
    work_dir: str = ""
    """这条命令的落地目录（阶段 2 = `--workDir` 指向的那个 `runs/<design>/<axes-slug>/`）。"""
    stage: Stage = Stage.SOLVE
    run_id: str = ""
    design_key: str = ""
    flags: FlagDict = field(default_factory=dict)
    """合并后的 flag dict。`diff_flags(plan.flags, golden)` 直接可用 —— 别让调用方从 argv 反解。"""
    port_spec: PortSpec = field(default_factory=PortSpec)
    log_path: str = ""
    """我们自己 tee 的日志。⚠️ 生产那条命令末尾恒接 `| sed -r 's/\\x1B\\[[0-9;]*m//g'` 剥 ANSI ——
    我们不靠管道，改在 `logparse.strip_ansi` 里做，argv 才干净。"""


# --------------------------------------------------------------------------
# 数据结构：落地布局
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RunPaths:
    """BRIEF §5「归档布局」那棵树上的每个位置，全在这儿，别在别处 `os.path.join` 拼第二遍。

    ```
    <batch_root>/<batch_name>/          batch_dir
      batch.json                        batch_json
      runs.csv                          runs_csv
      gds/<design>.gds                  design_gds
      gdsout/<design>.gdsout_setup      design_gdsout
      sparam/<design>__<slug>__<c>_<t>.sNp   sparam_prefix + 后缀
      runs/<design>/<axes-slug>/        run_dir   ← ★ --workDir 指到这里
        cmd_<corner>_<temp>.sh          cmd_sh    ← ★ 每个 run 一份，不是固定 cmd.sh
        run_<corner>_<temp>.log         run_log      （理由见 CMD_SH_TEMPLATE）
        <corner>_<temp>/                ewave_dir ← ★ eWave 自己建的，我们控制不了名字
    ```
    """

    batch_dir: str
    batch_json: str
    runs_csv: str
    gds_dir: str
    gdsout_dir: str
    sparam_dir: str
    logs_dir: str
    design_gds: str
    design_gdsout: str
    run_dir: str
    cmd_sh: str
    ewave_dir: str
    run_log: str
    sparam_prefix: str
    """扁平汇聚区里这个 run 的文件名前缀（不带 `.sNp` 后缀 —— 端口数要等产物出来才知道）。"""


@dataclass(frozen=True)
class ArchiveReport:
    """归档做了什么。D5：只留参数文件，mesh/中间件删掉；失败时留日志。"""

    kept: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    """按 `archive_keep` 该有却没有的 —— 这通常意味着 run 其实失败了。"""
    errors: tuple[str, ...] = ()
    bytes_freed: int = 0


@dataclass(frozen=True)
class VerifyReport:
    """产物验收。**`done` 的判据是这个，不是 job 退出码。**

    依据（BRIEF §10 实测）：eWave 崩了也 `exit=0`、还会留 0 字节文件报 "done"。
    三条失败信号都不可靠 ⇒ 验收契约 = 存在 + 非空 + 端口数对。
    """

    ok: bool = False
    reasons: tuple[str, ...] = ()
    """不 ok 时每条一句话；ok 时为空。"""
    sparam_files: tuple[str, ...] = ()
    port_count: int | None = None
    ports: tuple[str, ...] = ()
    total_bytes: int = 0


@dataclass
class LogFacts:
    """从 `ewave.log` / `emsolver.log` 抽出来的事实（P4）。

    全部 `None` = 没解析到，**不要用 0 冒充**：0 秒和"没测到"是两回事。
    """

    ok: bool | None = None
    converged: bool | None = None
    wall_seconds: float | None = None
    peak_memory_mb: float | None = None
    port_count: int | None = None
    freq_points_calculated: int | None = None
    """求解器**真算过**的频点数（官方 401 点里只真算了 19 个，其余是拟合）。"""
    freq_points_requested: int | None = None
    cpu_percent_avg: float | None = None
    ewave_version: str = ""
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    source_files: tuple[str, ...] = ()


# --------------------------------------------------------------------------
# 数据结构：执行
# --------------------------------------------------------------------------


@dataclass
class Job:
    """调度器里的一个 job。dry-run 时也会造一个（`job_id` 为空）好让状态机走完。"""

    job_id: str = ""
    scheduler: str = ""
    state: JobState = JobState.UNKNOWN
    name: str = ""
    submitted_at: str = ""
    started_at: str = ""
    ended_at: str = ""
    exit_code: int | None = None
    """⚠️ 只作诊断用。**不许拿它判 done** —— eWave 崩了也返 0。"""
    account: str = ""
    queue: str = ""
    resources: str = ""
    stdout_path: str = ""
    stderr_path: str = ""
    raw: str = ""
    """调度器原始输出片段，解析不了时留着给人看。"""


@dataclass(frozen=True)
class RunResult:
    """一次子进程执行的结果（`RunnerProtocol.run` 的返回）。"""

    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    timed_out: bool = False
    cancelled: bool = False
    cwd: str = ""


@dataclass
class Run:
    """阶段 2 的一个格子 = 一个 design × 一组轴取值 = 一次独立 ewave run = 一个独立目录（D2）。"""

    run_id: str
    """批次内唯一且稳定：`<design_key>/<axes_slug>/<ewave_dir>`。resume 靠它对上号。"""
    design_key: str
    axis_values: dict[str, str] = field(default_factory=dict)
    """轴名 → 取值（用户可读的那个字面量，不是渲染后的 flag）。"""
    axes_slug: str = BASE_SLUG
    """除 corner/temperature 之外的轴拼出来的 slug；没有额外轴时是 `base`。"""
    ewave_dir: str = ""
    """`<corner>_<temp>`。**eWave 自己建的那层**，我们只是预测它的名字好去里面找产物。"""
    work_dir: str = ""
    """`--workDir` 的值。每个组合一个独立 workDir —— 这是绕开"同 corner/temp 静默覆盖"的全部手段。"""
    status: RunStatus = RunStatus.READY
    job: Job | None = None
    attempts: int = 0
    """提交次数。**不自动重试**（§12）—— 这个数字大于 1 只可能是人按了 resume。"""
    submitted_at: str = ""
    started_at: str = ""
    ended_at: str = ""
    wall_seconds: float | None = None
    argv: tuple[str, ...] = ()
    """最终跑的那条命令，留档。与 `cmd.sh` 内容一致。"""
    artifacts: tuple[str, ...] = ()
    """归档后的参数文件路径（相对 batch_dir）。"""
    ports: tuple[str, ...] = ()
    """这个 run 实际的端口顺序。批次内互相比对，**不一致要报错而不是继续**（BRIEF §5）。"""
    log_facts: LogFacts | None = None
    message: str = ""
    """失败原因 / 跳过原因。空字符串 = 没话说。"""


@dataclass
class StreamoutTask:
    """阶段 1 的一个格子 = 一个 design 的 `strmout`。整个设定矩阵共用它的产物（D1a）。"""

    design_key: str
    status: RunStatus = RunStatus.READY
    job: Job | None = None
    gds_path: str = ""
    gdsout_setup_path: str = ""
    """渲染出来的模板，留档可追溯（D1c）。"""
    log_path: str = ""
    started_at: str = ""
    ended_at: str = ""
    argv: tuple[str, ...] = ()
    message: str = ""


@dataclass
class BatchState:
    """一个批次的全部状态。**resume 只认这一个文件**（`batch.json`，原子重写）。"""

    batch_name: str = ""
    batch_dir: str = ""
    """`<batch_root>/<batch_name>` 的绝对路径。"""
    designs: list[Design] = field(default_factory=list)
    axes: list[Axis] = field(default_factory=list)
    runs: list[Run] = field(default_factory=list)
    streamout: list[StreamoutTask] = field(default_factory=list)
    options: BatchOptions = field(default_factory=BatchOptions)
    defaults: FlagDict = field(default_factory=dict)
    """学到的默认表，冻在批次里 —— 换 PDK 版本后 resume 老批次不能悄悄换值。"""
    extra_flags: FlagDict = field(default_factory=dict)
    provenance: Provenance = field(default_factory=Provenance)
    schema_version: int = SCHEMA_VERSION


@dataclass
class PlanContext:
    """拼一条命令所需的全部上下文（除 `Run` 自己）。**per-design**，因为坐标是 per-design 解析的。

    存在的理由：没有它，`build_command_plan` / `build_ewave_plan` / `build_strmout_plan`
    要各带 6 个参数，而且三处必然漂移。
    """

    design: Design
    facts: SiteFacts
    axes: tuple[Axis, ...] = ()
    defaults: FlagDict = field(default_factory=dict)
    extra_flags: FlagDict = field(default_factory=dict)
    options: BatchOptions = field(default_factory=BatchOptions)
    batch_dir: str = ""


@dataclass(frozen=True)
class DriverEvent:
    """driver 播出来的一条事件。CLI 打印，GUI 刷表 —— **同一份代码**。"""

    kind: EventKind
    message: str = ""
    run_id: str = ""
    design_key: str = ""
    at: str = ""


@dataclass(frozen=True)
class TickReport:
    """一次 `tick()` 的结果。CLI 用 `while not report.finished` 驱动，GUI 用 `after()` 驱动。"""

    changed: bool = False
    """这一拍有没有状态变化。没变化时 GUI 不用重画。"""
    finished: bool = False
    """全部 run 都到终态（done/failed/skipped）了。"""
    events: tuple[DriverEvent, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)
    """`RunStatus.value` → 条数。键用 `.value` 而不是枚举，好直接塞进 JSON / 界面。"""


# --------------------------------------------------------------------------
# Protocol：可注入的执行面
# --------------------------------------------------------------------------


@runtime_checkable
class RunnerProtocol(Protocol):
    """可注入的子进程执行器 —— D8「全程 dry-run + 可注入 runner」的支点。

    本机没有 `ewave` / `dsub` / `strmout`，**不可能**真跑（CLAUDE.md 硬约束 3）。
    所有跑外部命令的地方都通过它，测试注入假的，红区注入真的。
    """

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        on_line: Callable[[str], None] | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> RunResult:
        """执行 argv，返回 `RunResult`。

        约定：
        * `env` 是**增量**，实现方自己合并到 `os.environ` 的副本上。
        * `on_line` 每读到一行调用一次（逐行 flush，别攒到最后）。
        * `cancel` 返回 True 时尽快终止并置 `RunResult.cancelled`。
        * 超时不抛异常，置 `timed_out=True` 返回 —— 调用方要看得见已经拿到的输出。
        * 找不到可执行文件时抛 `ToolMissingError`。
        """
        ...


@runtime_checkable
class SchedulerProtocol(Protocol):
    """提交后端。`sched.fake`（本机假批次）和 `sched.donau`（真 dsub）都实现它。"""

    def submit(self, plan: CommandPlan, *, resources: str = "", name: str = "") -> Job:
        """提交一条命令，返回带 `job_id` 的 `Job`（state 通常是 `PENDING`）。

        `resources` 是 dsub 的 `-R` 串（`"cpu=20;mem=100000"` 形状），空 = 用调度器自己的默认。
        提交失败抛 `SchedulerError`。**dry-run 不走这里** —— driver 在更上层就短路了。
        """
        ...

    def poll(self, jobs: Sequence[Job]) -> dict[str, Job]:
        """一次查一批（真实现是一次 `djob` 查全部，别一个 job 一条命令）。

        返回 `job_id` → **更新后的 Job**。查不到的 job 置 `JobState.UNKNOWN` 并保留原 Job，
        不要凭空判成 failed —— 调度器短暂查不到是常事。
        """
        ...

    def cancel(self, job: Job) -> bool:
        """取消一个 job。已经结束的返回 False 而不是抛异常。"""
        ...


@runtime_checkable
class DriverProtocol(Protocol):
    """两阶段 DAG 的驱动器。**单线程轮询**（§12 实现决定，不用线程池）。

    `tick()` 是全部对外接口：CLI 用 `while` 驱动，GUI 用 `after()` 驱动，**同一份代码**。
    """

    def tick(self) -> TickReport:
        """推进一拍：收割已完成的 → 验收 + 归档 → 提交新的（受 `max_parallel` 限）→ 落盘 state。

        **必须是非阻塞的**：不许在里面 sleep 等 job。一拍做不完的事下一拍接着做。
        任何异常都要转成 `RunStatus.FAILED` + 事件，不许炸穿到 GUI 的事件循环里。
        """
        ...

    def cancel(self) -> None:
        """取消全部在飞的 job；已完成的不动。"""
        ...

    def summary(self) -> dict[str, int]:
        """`RunStatus.value` → 条数。"""
        ...

    @property
    def state(self) -> BatchState:
        """当前状态。调用方**只读** —— 想改状态就走 `tick()`。"""
        ...


@runtime_checkable
class GuiBridgeProtocol(Protocol):
    """GUI ↔ driver 的桥。三版 frame（stacked/tabbed/split）只准通过它跟核心说话。

    这样换布局不碰逻辑，逻辑改了三版一起跟上。实现是 `gui.state.GuiState`。
    """

    def load_spec(self, path: str) -> None:
        """读 spec 并建 `BatchState`（还不建目录、不提交）。失败抛 `SpecError`。"""
        ...

    def plan(self) -> None:
        """展开矩阵 + 拼命令，把结果放进 state。dry-run 界面靠它。"""
        ...

    def start(self, *, dry_run: bool = False) -> None:
        """开跑（或 dry-run）。已经在跑时是 no-op。"""
        ...

    def tick(self) -> TickReport | None:
        """驱动一拍，没在跑时返回 None。GUI 用 `after(poll_interval_ms, ...)` 调它。"""
        ...

    def cancel(self) -> None:
        ...

    def runs(self) -> tuple[Run, ...]:
        ...

    def designs(self) -> tuple[Design, ...]:
        ...

    def axes(self) -> tuple[Axis, ...]:
        ...

    def command_text(self, run_id: str) -> str:
        """选中 run 的完整 argv，一行一个 flag 的可读形式（界面 `Selected run → Command`）。"""
        ...

    def summary(self) -> dict[str, int]:
        ...


# ==========================================================================
# 跨模块函数签名（**只有签名**。真身在各自模块里，签名必须逐字一致）
# ==========================================================================

# --------------------------------------------------------------------------
# ewave_batch.core.matrix —— 展开笛卡尔积 / slug / varying 轴
# --------------------------------------------------------------------------


def design_key(design: Design) -> str:
    """算一个 design 的稳定 id（`design.key` 非空时直接返回它）。

    默认形状沿用官方目录名约定 `<library>_<topCell>_<view>` 再过一遍 `slugify` ——
    设计师认得，而且天然唯一。不写盘。
    """
    raise NotImplementedError


def slugify(text: str) -> str:
    """把任意取值变成能当目录名的片段。

    规则：保留 `[A-Za-z0-9._-]`，小数点换成 `_`（跟 eWave 的温度约定一致），
    其余连续字符压成一个 `-`，**不改大小写**（端口名是 case-sensitive，别养成改大小写的手感）。
    空输入返回空串，由调用方决定要不要报错。
    """
    raise NotImplementedError


def ewave_dir_name(corner: str, temperature: str) -> str:
    """拼 eWave 自己会建的那层目录名：`<corner>_<temp 的小数点换下划线>`。

    ⚠️ 我们**控制不了**这个名字，只能预测它好去里面找产物。名字只由 corner+temp 决定，
    这正是"同 corner/temp 换别的 flag 会静默覆盖"的根因（BRIEF §7）。
    """
    raise NotImplementedError


def varying_axes(axes: Sequence[Axis], *, designs: Sequence[Design] = ()) -> list[Axis]:
    """挑出**真正在变**的轴（取值 > 1 个的）。

    slug 只编码变的那些：只扫 corner+temp 时目录名与官方逐字一致，设计师才认得（BRIEF §5）。
    给了 `designs` 就把 per-design 的 `axis_overrides` 也算进去（某个 design 上多出来的取值
    同样算"在变"）。
    """
    raise NotImplementedError


def compute_axes_slug(axis_values: Mapping[str, str], axes: Sequence[Axis]) -> str:
    """算 `<axes-slug>` —— 只含 `encoded_in_ewave_dir=False` 且**在变**的轴。

    形如 `eqI-on__fw-off`；一个都没有时返回 `BASE_SLUG`（`"base"`）。
    片段顺序 = `axes` 里的顺序（不是字典序），这样同一批次里 slug 稳定可读。
    不写盘。
    """
    raise NotImplementedError


def axes_for_design(design: Design, axes: Sequence[Axis]) -> list[Axis]:
    """把 `design.axis_overrides` 套到全局轴定义上，返回这个 design 实际要扫的轴。

    覆盖的是**取值列表**，不是 flag 定义 —— 轴的语义全批次一致，否则 slug 会对不上号。
    override 里出现未知轴名 → `SpecError`。
    """
    raise NotImplementedError


def builtin_axis_catalog() -> dict[str, Axis]:
    """内置轴目录：轴名 → `Axis`（取值列表为**该轴的合法取值样例**，实际取值由 spec 给）。

    覆盖 BRIEF §10 用户点名的那张清单：corner（同时改 `--corner=` **和** `--emssTechFile=`）、
    temperature、`--equalCurrent`、`--fullWave`、网格密度（`-e`/`-d`/`--viaMergeSpace`）、
    两个 tolerance、频率扫描（`--multiSweep`/`--logarithmicSweep`/`--discreteFreq`）、`--parallel`。

    ⚠️ 只许出现 flag 名和通用数值 —— **ptxt 路径靠 `PLACEHOLDER_PTXT` 占位**，
    corner 的合法取值也只是 5 个通用工艺角名字，不是站点身份。
    """
    raise NotImplementedError


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
    """
    raise NotImplementedError


# --------------------------------------------------------------------------
# ewave_batch.core.spec —— 读用户手写的 spec（唯一允许 import PyYAML 的地方）
# --------------------------------------------------------------------------


def load_spec(path: str) -> BatchSpec:
    """读 YAML（或 JSON）spec。

    **PyYAML 必须惰性 import**（CLAUDE.md 硬约束 2）：`.json` 后缀或 PyYAML 缺失时走
    `json` 退路；`.yaml/.yml` 且 PyYAML 缺失 → `SpecError` 并在消息里告诉用户可以改用 JSON。
    YAML 只准用 `yaml.safe_load`。读盘，不写盘。文件不存在 / 解析失败 → `SpecError`。
    """
    raise NotImplementedError


def parse_spec_mapping(data: Mapping[str, object], *, source: str = "") -> BatchSpec:
    """把已经解析成 dict 的 spec 变成 `BatchSpec`（YAML/JSON 共用这一条路）。

    负责：list 值自动展开成轴、未知键报错、`USER_FORBIDDEN_FLAGS` 检查。
    纯函数，不碰文件系统 —— 单测全走它。非法输入 → `SpecError` / `FlagConflictError`。
    """
    raise NotImplementedError


def spec_sha256(path: str) -> str:
    """spec 文件的 sha256（十六进制小写）。进 `Provenance.spec_sha256`，
    resume 时对不上就警告"spec 改过了"。"""
    raise NotImplementedError


def spec_to_batch(spec: BatchSpec, *, batch_root: str, tool_version: str = "") -> BatchState:
    """`BatchSpec` → 全新的 `BatchState`（run 全是 `READY`，还没建目录）。

    不写盘 —— 落盘是 `core.layout.write_batch_state` 的活。
    """
    raise NotImplementedError


# --------------------------------------------------------------------------
# ewave_batch.core.cmd —— 四层合并 → argv；冲突检测；逐 flag diff
# --------------------------------------------------------------------------


def build_flag_layers(run: Run, ctx: PlanContext) -> FlagLayers:
    """把这个 run 的五层 flag 各自装好（还没合并）。

    * `builtin` = `BUILTIN_DEFAULT_FLAGS`
    * `defaults` = `ctx.defaults`（学自官方 run 目录）
    * `extra` = `ctx.extra_flags` + `ctx.design.extra_flags`
    * `axis` = 该 run 每根轴 `resolve_axis_flags` 的结果
    * `locked` = 工具自己算的（`--workDir` / `--gds` / `--top` / `--sparam` / `--all` /
      `--includePortOrder=1` / `--cadencePins=1` / `--nogui` / `-m`）

    不写盘。
    """
    raise NotImplementedError


def resolve_axis_flags(axis: Axis, value: str, facts: SiteFacts) -> FlagDict:
    """算某根轴取某个值时贡献的 flag，并把占位符换掉。

    只认两个占位符：`PLACEHOLDER_VALUE` → 取值本身；`PLACEHOLDER_PTXT` →
    `core.discover.ptxt_path_for_corner(facts, value)`。

    ⚠️ corner 轴会同时吐 `--corner=` **和** `--emssTechFile=` 两个 flag（§7「corner 轴要同时改两处」）——
    少改一个就是"目录名说 typical、实际用了别的工艺角"，而且跑得出来、数字也像。
    `value` 不在 `axis.values` 里 → `SpecError`。
    """
    raise NotImplementedError


def merge_flag_layers(layers: FlagLayers) -> FlagDict:
    """按 `FlagLayers.MERGE_ORDER` 合并成一份 flag dict。

    * 后层覆盖前层。
    * 值为 `False` 的键表示**显式缺席**：合并后仍留在 dict 里（`False`），
      由 `render_flags` 负责不渲染 —— 这样 `diff_flags` 能看见"我们是有意不给这个 flag"。
    不写盘。
    """
    raise NotImplementedError


def detect_flag_conflicts(layers: FlagLayers, axes: Sequence[Axis]) -> list[FlagConflict]:
    """查非法冲突。返回空 list = 干净。**自己不抛异常**，抛不抛由调用方定。

    规则（BRIEF §11 规则 2）：
    1. `extra` 里出现 `USER_FORBIDDEN_FLAGS` 里的 flag → fatal。
    2. `extra` 里出现任何一根**轴掌管**的 flag（`Axis.flags`）→ fatal。
       否则目录名会和实际跑的值对不上 —— 那正是原生 GUI 覆盖坑的根因，不能自己再造一遍。
    3. `axis` 与 `locked` 撞车（除 `--emssTechFile`）→ fatal，说明轴定义写错了。
    4. `defaults` 覆盖了 `builtin` → 不是冲突，正常。
    """
    raise NotImplementedError


def render_flags(flags: FlagDict) -> list[str]:
    """flag dict → argv 片段，顺序**确定**（同样的输入永远同样的输出，cmd.sh 才可比对）。

    渲染规则：
    * `True` → `["--nogui"]` / `["-m"]`
    * `False` → 什么都不产生（显式缺席）
    * `str` 且长 flag → `["--corner=typical"]`（一项，用 `LONG_FLAG_ASSIGN`）
    * `str` 且短 flag → `["-e", "0.4"]`（两项 —— 生产就是这么写的，别自作主张合并）

    端口不在这里渲染，见 `tools.ewave.render_ports`。
    """
    raise NotImplementedError


def build_command_plan(run: Run, ctx: PlanContext) -> CommandPlan:
    """一个 run → 完整 `CommandPlan`（阶段 2）。四层合并 + 冲突检测 + argv 渲染都在这。

    冲突里有 fatal 的 → `FlagConflictError`。
    坐标缺失（没有 `facts.ewave_bin` 之类）→ `DiscoveryError`。
    **不写盘、不建目录**（dry-run 和真跑走的是同一条路，区别只在后面提不提交）。
    """
    raise NotImplementedError


def diff_flags(
    actual: FlagDict,
    expected: FlagDict,
    *,
    ignore: Sequence[str] = (),
) -> FlagDiff:
    """逐 flag 集合 diff。**golden 测试和红区 dry-run 的自带比对共用这一个函数。**

    🚨 `ignore` 按 **flag 名精确匹配**，**绝不做前缀匹配**。
    MVP 里踩过：排除规则写 `--sparam` 前缀误伤了 `--sparamImpedance`，两边同时被跳过，
    diff 空得非常好看，但根本没比。**空过的测试比没测更坏。**

    * 被 ignore 命中的进 `FlagDiff.ignored`，**不计入** `compared_count`。
    * 值为 `False` 的键视为"该 flag 不存在"，与对面真的没有这个键**算一致**。
    * 比较值时按字符串比（`"0.4"` != `"0.40"` —— 报出来让人看，别自作主张归一）。
    不写盘。
    """
    raise NotImplementedError


def diff_ports(actual: PortSpec, expected: PortSpec) -> PortDiff:
    """比端口顺序（不是集合 —— **顺序就是映射**，见 BRIEF §5）。

    `first_mismatch_index` 指出第一处错位。pin 集合一变所有编号平移，而且静默错位，
    对批量对比尤其致命 —— 所以这里报告的是位置，不只是差集。
    """
    raise NotImplementedError


def parse_resource_string(resources: str) -> dict[str, str]:
    """`"cpu=20;mem=100000"` → `{"cpu": "20", "mem": "100000"}`。空串 → 空 dict。

    给 `--parallel` 定档用。⚠️ eWave 的 `--parallel` **不**必须等于 `cpu=`
    （那是 ALPS 的规矩，照抄会损失一半算力）——倍率是 `BatchOptions.parallel_multiplier`。
    """
    raise NotImplementedError


# --------------------------------------------------------------------------
# ewave_batch.core.layout —— 目录布局 / cmd.sh / 归档 / 验收 / state 读写
# --------------------------------------------------------------------------


def compute_run_paths(batch_dir: str, design: Design, run: Run) -> RunPaths:
    """算出 BRIEF §5「归档布局」那棵树上这个 run 相关的全部路径。

    **纯函数，一个目录都不建**（`ensure_run_dirs` 才建）。路径全用 `/` 拼，
    因为最终跑在 Linux 上；Windows 上单测比对字符串也一致。
    """
    raise NotImplementedError


def ensure_run_dirs(paths: RunPaths, *, dry_run: bool = False) -> None:
    """建好 batch/gds/gdsout/sparam/runs 这些目录。`dry_run=True` 时什么都不做。

    ⚠️ **绝不建到 `<workarea>/ewave_simulation/` 里去**（CLAUDE.md 硬约束 4，那是设计师的 spine）。
    路径落在 spine 内 → `StateError`。
    """
    raise NotImplementedError


def write_cmd_sh(paths: RunPaths, plan: CommandPlan, *, dry_run: bool = False) -> str:
    """把这个 run 的完整命令写成 `cmd.sh`（可单独手工重跑），返回写到的路径。

    * 行尾必须是 **LF**（红区 bash 死在 CRLF 上，而那是最没法调试的地方）。
    * 参数逐个 `shlex.quote`，一行一个 flag 加续行 `\\`，人要能读。
    * `dry_run=True` 时只返回路径、不写文件。
    """
    raise NotImplementedError


def verify_run_outputs(
    paths: RunPaths,
    run: Run,
    *,
    expected_port_count: int | None = None,
) -> VerifyReport:
    """产物验收 —— **`done` 的判据**（不是 job 退出码，§12）。

    三条实测出来的失败信号不可靠（BRIEF §10）：`exit=0` 但崩了、0 字节文件也报 "done"、
    写失败被吞。所以验收契约是：**存在 + 非空 + 端口数对**。
    只读，不改文件。找不到产物不抛异常，返回 `ok=False` + 原因。
    """
    raise NotImplementedError


def archive_run(
    paths: RunPaths,
    run: Run,
    *,
    keep: Sequence[str] = (),
    keep_logs_on_failure: bool = True,
    dry_run: bool = False,
) -> ArchiveReport:
    """按 D5 归档：参数文件收进 `sparam/` 扁平区，mesh/pmrg/pmsh/resist 中间件删掉。

    * `keep` 是 fnmatch 模式（默认用 `BatchOptions.archive_keep`）。
    * run 失败且 `keep_logs_on_failure` → 保留 `ewave.log` / `emsolver.log` 做诊断。
    * 扁平区的文件名 = `RunPaths.sparam_prefix` + 原后缀（`.s4p` 这种，端口数来自产物本身）。
    * **删除只在 `paths.ewave_dir` 里发生**，别的地方一个文件都不许删。
    写盘（`dry_run=True` 时只报告不动手）。
    """
    raise NotImplementedError


def check_port_consistency(state: BatchState) -> list[str]:
    """批次内互相比对每个 run 的端口列表，返回问题描述（空 list = 一致）。

    ⚠️ 这不是可选的（BRIEF §5「`--all` 的代价」）：设计师加/删/改名一个 pin ⇒ 所有编号平移 ⇒
    之前建的 nport 和归档的 `.sNp` 全部错位，**而且静默**。同一个 design 的多个 run
    端口列表不一致时必须报出来，让调用方停下而不是继续。只读。
    """
    raise NotImplementedError


def port_count_from_suffix(path: str) -> int | None:
    """从 `.s4p` / `.y3p` 这种后缀里取端口数；认不出返回 None。不读文件内容。"""
    raise NotImplementedError


def state_to_dict(state: BatchState) -> dict[str, object]:
    """`BatchState` → 可 JSON 序列化的 dict（枚举落 `.value`，元组落 list）。纯函数。"""
    raise NotImplementedError


def state_from_dict(data: Mapping[str, object]) -> BatchState:
    """反过来。`schema_version` 比 `SCHEMA_VERSION` 大 → `StateError`（**拒绝而不是猜**）。"""
    raise NotImplementedError


def read_batch_state(path: str) -> BatchState:
    """读 `batch.json`。文件不存在 / 解析失败 / 版本不认识 → `StateError`。"""
    raise NotImplementedError


def write_batch_state(path: str, state: BatchState) -> None:
    """**原子**写 `batch.json`：同目录临时文件 + `os.replace`。

    每拍都要写，跑到一半断电也不能留半份 JSON —— resume 只认这一个文件。
    顺手刷新 `provenance.updated_at`。
    """
    raise NotImplementedError


def write_runs_csv(path: str, state: BatchState) -> None:
    """写汇总表。表头 = `RUNS_CSV_COLUMNS`（冻结），`newline=""` + LF，UTF-8。"""
    raise NotImplementedError


def set_run_as_current(
    paths: RunPaths,
    run: Run,
    design: Design,
    *,
    target_dir: str,
    dry_run: bool = False,
) -> list[str]:
    """把某个 run 的 `.sNp` 落到官方那个路径上，让现成的 nport 零编辑生效（BRIEF §5）。

    🚨 **这是唯一一处会写进设计师 spine 的操作**，三道约束一条都不许省：
    ① 必须显式触发（**绝不**在批量跑完时自动执行）；② 覆盖前备份原文件；③ 记进日志。
    返回做过的动作列表（给日志和界面看）。`target_dir` 不存在 → `StateError`。
    """
    raise NotImplementedError


# --------------------------------------------------------------------------
# ewave_batch.core.discover —— 从官方 run 目录解析坐标（照 mvp/redzone/cfg.sh 那条路）
# --------------------------------------------------------------------------


def discover_site_facts(official_run_dir: str, *, env: Mapping[str, str] | None = None) -> SiteFacts:
    """解析一个官方 GUI 跑过的 design 目录 → `SiteFacts`。

    这是 CLAUDE.md 硬约束 1b 的主路径：**坐标不手抄，现场解析**，既没有标识符进仓库，
    也没有抄错的可能（尤其那 34 个 `-p` flag）。解析路径与 `mvp/redzone/cfg.sh` 一致，
    改之前先读那个文件。

    目录不存在 / 没有 `gdsout_setup` → `DiscoveryError`（消息里要提示"这不像官方 design 目录"）。
    只读，一个字节都不写。
    """
    raise NotImplementedError


def parse_gdsout_setup(text: str) -> dict[str, str]:
    """`gdsout_setup` 文本 → 字段 dict。

    格式是 `key<tab>value`，value 可能带引号，也可能是裸 flag（`arrayInstToScalar` 没有值 → `""`）。
    纯字符串函数，单测全走它。
    """
    raise NotImplementedError


def templatize_gdsout_setup(text: str) -> str:
    """官方 `gdsout_setup` → 模板：只把 7 个随 design 变的字段换成 `@@…@@` 占位符
    （`GDSOUT_PLACEHOLDERS`），其余**逐字保留**。

    D1c 的全部要害在"其余逐字保留"上：`convertPin "geometry"` + `pinAttNum 1` 决定 pin 是不是
    几何图形，`case "preserve"` 决定大小写混用的端口名匹不匹配得上，`maxVertices 200`
    决定顶点怎么切 → mesh 不同 → L/Q 结果不同，而且跑得出来、数字也像。
    """
    raise NotImplementedError


def parse_dsub_options(text: str) -> dict[str, str]:
    """`remote_run_ewave.sh` 文本 → `{"account": …, "queue": …, "resources": …}`（缺的键就不给）。"""
    raise NotImplementedError


def ptxt_path_for_corner(facts: SiteFacts, corner: str) -> str:
    """算某个 corner 对应的 ptxt 绝对路径。

    ⚠️ corner 轴要同时改两处（§7）：`--corner=` 的值 **和** `--emssTechFile=` 的文件名 ——
    ptxt 是 per-corner 一个文件，文件名里带 corner。这个函数负责后者。
    `facts.ptxt_name_template` 为空（没解析出模板）→ `DiscoveryError`。不检查文件是否存在
    （那是 `check-env` 的活，dry-run 在没有 PDK 的机器上也要能跑）。
    """
    raise NotImplementedError


def learn_default_flags(facts: SiteFacts) -> FlagDict:
    """从官方实际在用的 flag 里学出「默认表」（§11 规则 1：**默认表的值不写死在源码**）。

    剔掉：站点相关（`--emssTechFile` / `--gds` / `--top` / `--workDir` / `--sparam` / `--key`）、
    机制层（`MECHANISM_FLAGS`）、以及**任何一根轴掌管的 flag**（那些由轴给）。
    剔除按 flag 名精确匹配 —— 又是 `--sparam` / `--sparamImpedance` 那个坑，别用前缀。
    """
    raise NotImplementedError


def find_tool(name: str, *, env: Mapping[str, str] | None = None) -> str | None:
    """`command -v` 的等价物（`shutil.which`），找不到返回 None。

    **工具绝对路径不许进源码**（CLAUDE.md 硬约束 1b）→ 版本目录一律靠这条路发现。
    """
    raise NotImplementedError


def suggest_official_dirs(root: str, *, max_depth: int = 3) -> list[str]:
    """在 `root` 底下找含 `gdsout_setup` 的目录，当作官方 design 目录的候选返回（已排序去重）。

    抄 `cfg.sh` 的 `suggest_offdir`：用户打错一个字符后面全废，不如让机器找。只读。
    """
    raise NotImplementedError


# --------------------------------------------------------------------------
# ewave_batch.core.logparse —— 日志 → 事实
# --------------------------------------------------------------------------


def strip_ansi(text: str) -> str:
    """剥 ANSI 颜色码。eWave 即使 `--nogui` 也输出颜色，生产脚本靠管道 sed 剥 ——
    我们不用管道（argv 要干净），改在这里剥。"""
    raise NotImplementedError


def parse_ewave_log(text: str) -> LogFacts:
    """`ewave.log` → `LogFacts`。纯字符串函数（先 `strip_ansi`），不碰文件系统。

    ⚠️ 解析不到就留 `None`，**别用 0 冒充**。`ok` 尤其要小心：日志说 "done" 不代表真成了
    （实测崩了也 exit=0 还留 0 字节文件），所以这里的 `ok` 只是日志的说法，
    最终判据是 `layout.verify_run_outputs`。
    """
    raise NotImplementedError


def parse_emsolver_log(text: str) -> LogFacts:
    """`emsolver.log` → `LogFacts`（收敛、真算过的频点数、峰值内存、CPU 占用多在这份里）。"""
    raise NotImplementedError


def merge_log_facts(*facts: LogFacts) -> LogFacts:
    """合并多份 `LogFacts`：**先到先得**（前面的非 None 值不被后面覆盖），
    `errors`/`warnings`/`source_files` 按顺序拼接去重。"""
    raise NotImplementedError


def parse_run_logs(run_dir: str) -> LogFacts:
    """读一个 run 目录里的全部日志（`ewave.log` / `emsolver.log` / `mesh.log` …）→ 合并后的事实。

    日志缺失不抛异常（失败的 run 常常什么都没留），返回全 None 的 `LogFacts` 并在
    `warnings` 里说明。只读。
    """
    raise NotImplementedError


def parse_port_order(snp_path: str) -> tuple[str, ...]:
    """从 `.sNp` 的注释头里读端口顺序（`--includePortOrder=1` 写进去的那份，D1d）。

    读不到返回空元组 —— 归档会把 `.sNp` 搬离原始命令行，而**端口映射只存在于命令行**，
    所以这份自描述是 v2 批量对比唯一能信的东西。只读。
    """
    raise NotImplementedError


# --------------------------------------------------------------------------
# ewave_batch.core.template —— 解析一条现成 ewave 命令行
# --------------------------------------------------------------------------


def split_command_line(line: str) -> list[str]:
    """把一行 shell 命令拆成 token（`shlex`，`posix=True`）。

    要能吃掉行尾续行 `\\`、单双引号、以及末尾的管道段（`| sed -r 's/…//g'` 要被丢掉）。
    拆不动 → `SpecError`。
    """
    raise NotImplementedError


def parse_command_line(line: str) -> ParsedCommand:
    """一条 ewave 命令行 → `ParsedCommand`（program + flags + 端口 + 位置参数）。

    * `--x=y` → `{"--x": "y"}`；`--x` → `{"--x": True}`；`-e 0.4` → `{"-e": "0.4"}`。
    * `-p 'P000=<pin>'` 收进 `port_spec.mapping`（**保序**），`-i <pin>` 收进 `signal_ports`，
      `--all` → `PortMode.ALL`。
    * 认不出的 token 进 `positional`，**不许静默丢弃**。

    这是 golden 测试和红区 dry-run 自带比对的入口（D3 的"可选模板入口"也是它）。纯函数。
    """
    raise NotImplementedError


def extract_command_line(text: str, *, program: str = "ewave") -> str | None:
    """从一份 shell 脚本文本里把调用 `program` 的那一行（含续行）抠出来，找不到返回 None。

    官方 `run_ewave_<corner>_<temp>.sh` 就是这么被解析的。纯函数。
    """
    raise NotImplementedError


# --------------------------------------------------------------------------
# ewave_batch.tools.strmout —— 阶段 1
# --------------------------------------------------------------------------


def render_gdsout_setup(template_text: str, fields: GdsoutFields) -> str:
    """把 `GDSOUT_PLACEHOLDERS` 换成 `fields` 的值，**其余逐字不动**（D1c）。

    🚨 绝不用 Auto_ext 那种裸 argv 调 strmout —— 它只传 5 个 flag，漏掉 8 个会改变 GDS 内容的字段。
    模板里还有没换完的占位符 → `SpecError`（宁可炸，也别默默生成一份错的 setup）。纯字符串函数。
    """
    raise NotImplementedError


def build_strmout_plan(design: Design, ctx: PlanContext, *, setup_path: str) -> CommandPlan:
    """拼阶段 1 的 `strmout` 命令（`-templateFile <setup_path>` 形式）。

    `stage=Stage.STREAMOUT`。**不**写 setup 文件（那是调用方的事，dry-run 要能只打印）。
    `facts.strmout_bin` 为空 → `ToolMissingError`。
    """
    raise NotImplementedError


# --------------------------------------------------------------------------
# ewave_batch.tools.ewave —— 阶段 2（薄封装到 core.cmd）
# --------------------------------------------------------------------------


def render_ports(port_spec: PortSpec) -> list[str]:
    """端口部分的 argv：`-p P000=<pin> -p …  -i P000 …`（**保序**）。

    顺序就是映射本身：Touchstone 只按 P00x 编号排列、名字被丢掉。排序一乱，
    整份 `.sNp` 就静默错位。

    ⚠️ `PortMode.ALL` → **返回空 list**，`--all` 由机制层的 flag dict 出
    （`core.cmd._locked_flags`）。这里再给一次的话 argv 里会出现**两个** `--all` ——
    eWave 多半照单全收，而 `.sNp` 里根本看不出端口被重复声明过。
    计数断言见 `tests/test_tools_ewave.py::test_build_ewave_plan_has_exactly_one_all_flag`。
    """
    raise NotImplementedError


def ewave_program(facts: SiteFacts) -> str:
    """要执行的 ewave 可执行文件。`facts.ewave_bin` 为空 → `ToolMissingError`。
    **绝不在源码里写死绝对路径**（CLAUDE.md 硬约束 1b）。"""
    raise NotImplementedError


def build_ewave_plan(run: Run, ctx: PlanContext) -> CommandPlan:
    """阶段 2 的 `CommandPlan` —— 薄封装：转给 `core.cmd.build_command_plan`，
    再把端口 argv 接到尾巴上。存在的意义是让 driver 只跟 `tools.*` 打交道。"""
    raise NotImplementedError


# --------------------------------------------------------------------------
# ewave_batch.sched.donau —— 真提交（从 kit 移植，runner 可注入）
# --------------------------------------------------------------------------


def build_dsub_argv(
    plan: CommandPlan,
    *,
    account: str = "",
    queue: str = "",
    resources: str = "",
    name: str = "",
    log_path: str = "",
) -> list[str]:
    """拼 `dsub … <命令>` 的 argv。

    账号 / 队列 / 资源全部**来自运行时解析**（`SiteFacts.dsub_*`）或 spec，
    **源码里不许有默认值**（CLAUDE.md 硬约束 1b）。空串 = 不加该选项。
    """
    raise NotImplementedError


def parse_dsub_submit_output(text: str) -> str:
    """从 dsub 的提交回显里抠 job id。抠不出来 → `SchedulerError`（**别返回空串继续**，
    没有 job id 的 run 后面永远轮询不到，会挂成僵尸）。"""
    raise NotImplementedError


def parse_djob_output(text: str) -> dict[str, JobState]:
    """查询回显 → `job_id` → `JobState`。认不出的状态字串映射成 `JobState.UNKNOWN`，不抛异常。"""
    raise NotImplementedError


# --------------------------------------------------------------------------
# ewave_batch.sched.driver —— 两阶段 DAG + 有界并发 + poll + verify + archive + resume
# --------------------------------------------------------------------------


def make_driver(
    state: BatchState,
    contexts: Mapping[str, PlanContext],
    scheduler: SchedulerProtocol,
    runner: RunnerProtocol,
    *,
    on_event: Callable[[DriverEvent], None] | None = None,
) -> DriverProtocol:
    """造一个 driver。`contexts` 的键是 `design_key`（坐标是 per-design 的）。

    调用方**只通过这个工厂**拿 driver，别直接碰 `Driver.__init__` —— 那样才好换实现。
    `contexts` 少了某个 design → `SpecError`。
    """
    raise NotImplementedError


def run_batch(
    driver: DriverProtocol,
    *,
    poll_interval: float = 15.0,
    max_seconds: float | None = None,
) -> int:
    """CLI 的 `while` 驱动：反复 `tick()` + sleep 到全部终态，返回进程退出码
    （0 = 全成，非 0 = 有 failed）。

    GUI **不**调它 —— GUI 用 `after()` 驱动同一个 `tick()`。这是"同一份 driver 代码"的落点。
    `max_seconds` 是给测试用的保险丝。
    """
    raise NotImplementedError


def resume_batch(
    batch_dir: str,
    contexts: Mapping[str, PlanContext],
    scheduler: SchedulerProtocol,
    runner: RunnerProtocol,
    *,
    on_event: Callable[[DriverEvent], None] | None = None,
) -> DriverProtocol:
    """从 `batch.json` 恢复一个批次（D7 断点续跑）。

    * `done` 的不重跑；`failed` / `ready` 的重新排；`pending` / `running` 的先去调度器查一遍
      （job 还活着就继续等，死了才重排）。
    * **不自动重试**（§12）：失败停在 `failed`，是人按了 resume 才补。
    * spec 的 sha 与 `provenance` 对不上 → 事件里发一条 `WARNING`，但照跑。
    """
    raise NotImplementedError


# --------------------------------------------------------------------------
# CLI / GUI 入口
# --------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口。`ewave_batch.cli.main`、顶层 `cli.main`、`gui.frames.*.main` 共用这个签名。

    子命令：`run` / `dry-run` / `resume` / `archive` / `status`（`SUBCOMMANDS`）。
    返回进程退出码，**不 `sys.exit`**（GUI 也会调它）。
    🚨 tkinter 只许在 GUI 分支里惰性 import（CLAUDE.md 硬约束 5）——
    无 `$DISPLAY` 的纯 ssh 会话里 CLI 必须可用。
    """
    raise NotImplementedError


def build_parser() -> "argparse.ArgumentParser":  # noqa: F821 - 注解是字符串，不 import argparse
    """造 `argparse` 解析器。单独一个函数是为了让测试能直接拿它验参数面，不用起进程。"""
    raise NotImplementedError


def launch(layout: str = "split") -> int:
    """起 GUI。`layout` ∈ `gui.app.LAYOUTS`（`"stacked"` / `"tabbed"` / `"split"`，默认 split）。

    tkinter 在**这个函数体内**才 import。没有 `$DISPLAY` / 没装 tkinter →
    打印一句人话 + 返回非 0，**不抛 traceback**（tier 3 缺失是降级不是失败）。
    """
    raise NotImplementedError


def build_frame(parent: object, bridge: GuiBridgeProtocol) -> object:
    """建一版布局的主 frame（`gui.frames.{stacked,tabbed,split}` 各一份）。

    * `parent` 是 tkinter 容器，返回值是这版布局的根 widget（注解写 `object` 是为了
      **让 model.py 不 import tkinter**）。
    * 只通过 `bridge` 跟核心说话，frame 里不许出现业务逻辑。
    * **模块顶层不许建 `Tk()`**，也不许在 import 时碰 `$DISPLAY` —— `EWB_SMOKE=1`
      的 headless 冒烟要能只建对象不进主循环。
    """
    raise NotImplementedError


# ==========================================================================
# 机器可读的冻结清单 —— `python -m ewave_batch dry-run --self-test` 靠它做漂移检测
# ==========================================================================

FROZEN: dict[str, tuple[str, ...]] = {
    "ewave_batch": ("__version__",),
    "ewave_batch._stdio": ("ascii_safe_stdio",),
    "ewave_batch.model": (
        # 版本 / 别名
        "INTERFACE_VERSION",
        "SCHEMA_VERSION",
        "FlagValue",
        "FlagDict",
        # 异常
        "EwaveBatchError",
        "SpecError",
        "DiscoveryError",
        "FlagConflictError",
        "StateError",
        "SchedulerError",
        "VerificationError",
        "ToolMissingError",
        # 枚举
        "RunStatus",
        "JobState",
        "Stage",
        "AxisKind",
        "FlagLayer",
        "EventKind",
        "PortMode",
        # 常量
        "TIMESTAMP_FORMAT",
        "BASE_SLUG",
        "AXIS_SLUG_SEP",
        "TEMP_DECIMAL_REPLACEMENT",
        "MECHANISM_FLAGS",
        "USER_FORBIDDEN_FLAGS",
        "LONG_FLAG_ASSIGN",
        "PLACEHOLDER_VALUE",
        "PLACEHOLDER_PTXT",
        "BATCH_JSON_NAME",
        "RUNS_CSV_NAME",
        "GDS_DIRNAME",
        "GDSOUT_DIRNAME",
        "SPARAM_DIRNAME",
        "RUNS_DIRNAME",
        "LOGS_DIRNAME",
        "CMD_SH_NAME",
        "RUN_LOG_NAME",
        "CMD_SH_TEMPLATE",
        "RUN_LOG_TEMPLATE",
        "GDSOUT_SETUP_SUFFIX",
        "RUNS_CSV_COLUMNS",
        # 数据结构
        "PortSpec",
        "AxisValue",
        "Axis",
        "Design",
        "BatchOptions",
        "Provenance",
        "BatchSpec",
        "SiteFacts",
        "GdsoutFields",
        "FlagLayers",
        "FlagConflict",
        "FlagDelta",
        "FlagDiff",
        "PortDiff",
        "ParsedCommand",
        "CommandPlan",
        "RunPaths",
        "ArchiveReport",
        "VerifyReport",
        "LogFacts",
        "Job",
        "RunResult",
        "Run",
        "StreamoutTask",
        "BatchState",
        "PlanContext",
        "DriverEvent",
        "TickReport",
        # Protocol
        "RunnerProtocol",
        "SchedulerProtocol",
        "DriverProtocol",
        "GuiBridgeProtocol",
        # 清单自身
        "FROZEN",
        "FROZEN_PHASE",
        "FROZEN_PROTOCOL_IMPLS",
        "FROZEN_SIGNATURES",
    ),
    "ewave_batch.core.matrix": (
        "design_key",
        "slugify",
        "ewave_dir_name",
        "varying_axes",
        "compute_axes_slug",
        "axes_for_design",
        "builtin_axis_catalog",
        "expand_runs",
        # P1 实做时长出来的跨模块 helper（`core.spec` 在用）。冻结面补登记，
        # 否则 self-test 不认识它们 = 不替它们盯签名漂移。
        "axis_with_values",
        "effective_axis_values",
    ),
    "ewave_batch.core.spec": (
        # 序列化那一侧（GUI 的「Save spec as…」）。spec 是本工具的**工程文件** ——
        # `batch.json` 存的是跑起来之后的状态，存不了「我打算怎么跑」。
        "spec_to_mapping",
        "dump_spec",
        "save_spec",
        "have_yaml",
        "load_spec",
        "parse_spec_mapping",
        "spec_sha256",
        "spec_to_batch",
        "EXAMPLE_SPEC",
    ),
    "ewave_batch.core.cmd": (
        "BUILTIN_DEFAULT_FLAGS",
        "DEFAULT_DIFF_IGNORE",
        # `--key` 的 flag 名。取值永远来自 `SiteFacts.key`（站点身份，源码里零写死），
        # 由 `build_flag_layers` 补进默认表层 —— 和「`--parallel` 从 `-R` 的 `cpu=` 推导」
        # 同一处、同一形状。名字在两个模块里各写一份就会漂，所以它是公开常量。
        "KEY_FLAG",
        "build_flag_layers",
        "resolve_axis_flags",
        "merge_flag_layers",
        "detect_flag_conflicts",
        "render_flags",
        "build_command_plan",
        "diff_flags",
        "diff_ports",
        "parse_resource_string",
        # D1b 的编码器：`--all` 的端口编号预测。红区那趟 dry-run 的自带比对要用它，
        # 是"不依赖 GUI"成立的全部依据 —— 这种东西必须在冻结面里盯着。
        "predict_all_ports",
    ),
    "ewave_batch.core.layout": (
        "compute_run_paths",
        "ensure_run_dirs",
        "write_cmd_sh",
        "verify_run_outputs",
        "archive_run",
        "check_port_consistency",
        "port_count_from_suffix",
        "state_to_dict",
        "state_from_dict",
        "read_batch_state",
        "write_batch_state",
        "write_runs_csv",
        "set_run_as_current",
    ),
    "ewave_batch.core.discover": (
        "discover_site_facts",
        "parse_gdsout_setup",
        "templatize_gdsout_setup",
        "parse_dsub_options",
        "ptxt_path_for_corner",
        "learn_default_flags",
        "find_tool",
        "suggest_official_dirs",
    ),
    "ewave_batch.core.logparse": (
        "strip_ansi",
        "parse_ewave_log",
        "parse_emsolver_log",
        "merge_log_facts",
        "parse_run_logs",
        "parse_port_order",
    ),
    "ewave_batch.core.template": (
        "split_command_line",
        "parse_command_line",
        "extract_command_line",
    ),
    "ewave_batch.tools.strmout": (
        "DEFAULT_GDSOUT_TEMPLATE",
        "GDSOUT_PLACEHOLDERS",
        "render_gdsout_setup",
        "build_strmout_plan",
        # P2 实做时长出来的跨模块符号 —— 补登记，否则 self-test 不认识它们
        # ＝ 不替它们盯漂移。前两个是 P3 driver 的**硬依赖**：
        "gdsout_fields_for_design",  # driver 阶段 1 靠它算 7 个字段
        "cdswork_dir",  # driver 要在这个 cwd 里写 cds.lib（BRIEF §10 step1 实测）
        "CDSWORK_DIRNAME",
        "GDSOUT_CRITICAL_FIELDS",  # D1c 的 8 个字段，redzone_dryrun 的兜底模板对照在用
        "parse_gdsout_fields",
        "diff_gdsout_setup",
        "substitute_placeholders",
    ),
    "ewave_batch.tools.ewave": (
        "render_ports",
        "ewave_program",
        "build_ewave_plan",
    ),
    "ewave_batch.sched.fake": (
        "FakeRunner",
        "FakeScheduler",
        "FakeFailureMode",
        # 模块级常量：测试和 driver 都在读，登记了 self-test 才替它们盯存在性。
        "FAKE_LOG_HEADER",
        "DEFAULT_PORT_COUNT",
        "DEFAULT_EPOCH",
    ),
    "ewave_batch.sched.donau": (
        "DonauScheduler",
        "build_dsub_argv",
        "parse_dsub_submit_output",
        "parse_djob_output",
        # ⚠️ 这两个只吃 `model.JobState`、与后端无关，却住在一个**具体后端**模块里，
        # 而 `sched.driver` 判状态的整条路径挂在它们身上（`from .donau import ...`）。
        # 这个归属是有味道的：将来加第二个后端时它们该搬到 model 或一个共享模块去。
        # 今晚不搬（搬动会牵动 driver 的全部状态判断，风险不划算），但**必须登记** ——
        # 不登记的话 self-test 不替它们盯签名，而它们一变，整个批次的状态机就跟着变。
        "is_terminal",
        "run_status_for_job_state",
    ),
    "ewave_batch.sched.driver": (
        "Driver",
        "SubprocessRunner",
        "make_driver",
        "run_batch",
        "resume_batch",
    ),
    "ewave_batch.redzone_dryrun": (
        # 红区那趟「只读 dry-run + 自带比对」的公开入口（BRIEF §12 的红区验证节奏）。
        # 它是**用户手里那条命令**，退出码写进了 docs/REDZONE_DRYRUN.md ⇒ 必须有机器判据。
        "EXIT_OK",
        "EXIT_ERROR",
        "EXIT_DIFF",
        "EXIT_NO_BASELINE",
        "ReadOnlyViolation",
        "ComparisonReport",
        "DryRunReport",
        "build_report",
        "format_report",
        "build_parser",
        "main",
    ),
    "ewave_batch.cli": (
        "SUBCOMMANDS",
        "build_parser",
        "main",
    ),
    "cli": ("main",),
    "gui.app": ("LAYOUTS", "launch"),
    "gui.state": ("GuiState",),
    "gui.frames.stacked": ("LAYOUT_NAME", "build_frame", "main"),
    "gui.frames.tabbed": ("LAYOUT_NAME", "build_frame", "main"),
    "gui.frames.split": ("LAYOUT_NAME", "build_frame", "main"),
}
"""模块名 → 它**必须导出**的符号。self-test 逐个 import + 核对存在性与签名。

三条规则：
1. 符号必须**定义在该模块里**（`__module__` 要对上）—— 从 `model` 里 re-export 一个
   `NotImplementedError` 桩子不算实现。
2. `model` 里有同名可调用对象时，签名必须一致（参数名 + 顺序 + 有无默认 + 参数种类 + 返回注解）。
3. 模块还不存在 = `pending`（不是错）；模块存在但少符号 = **漂移**（退出码 1）。
"""

FROZEN_PHASE: dict[str, str] = {
    "ewave_batch": "P0",
    "ewave_batch._stdio": "P0",
    "ewave_batch.model": "P0",
    "ewave_batch.core.matrix": "P1",
    "ewave_batch.core.spec": "P1",
    "ewave_batch.core.cmd": "P1",
    "ewave_batch.core.layout": "P1",
    "ewave_batch.core.template": "P1",
    "ewave_batch.core.discover": "P2",
    "ewave_batch.tools.strmout": "P2",
    "ewave_batch.redzone_dryrun": "P2",
    "ewave_batch.sched.fake": "P3",
    "ewave_batch.sched.donau": "P3",
    "ewave_batch.sched.driver": "P3",
    "ewave_batch.tools.ewave": "P3",
    "ewave_batch.core.logparse": "P4",
    "ewave_batch.cli": "P5",
    "cli": "P5",
    "gui.app": "P5",
    "gui.state": "P5",
    "gui.frames.stacked": "P5",
    "gui.frames.tabbed": "P5",
    "gui.frames.split": "P5",
}
"""哪个阶段负责写它。self-test 把还没写的报成 `pending: P<n>` 而不是崩。"""

FROZEN_PROTOCOL_IMPLS: dict[str, str] = {
    "ewave_batch.sched.fake:FakeRunner": "RunnerProtocol",
    "ewave_batch.sched.fake:FakeScheduler": "SchedulerProtocol",
    "ewave_batch.sched.donau:DonauScheduler": "SchedulerProtocol",
    "ewave_batch.sched.driver:SubprocessRunner": "RunnerProtocol",
    "ewave_batch.sched.driver:Driver": "DriverProtocol",
    "gui.state:GuiState": "GuiBridgeProtocol",
}
"""`模块:类名` → 它必须满足的 Protocol。self-test 逐个方法比签名 ——
`@runtime_checkable` 的 isinstance 只看方法名在不在，那挡不住参数漂移。"""

FROZEN_SIGNATURES: dict[str, str] = {}
"""`模块:符号` → 归一化签名字符串。**逃生口**：给那些在 model 里没有同名桩子的可调用对象用。

格式与 `ewave_batch.__main__.normalize_signature` 的输出一致：
`(a, b=, *args, *, kw=, **kw2) -> ret`，`=` 表示有默认值，无返回注解时省略 ` -> ret`。
现在是空的 —— 目前每个冻结的可调用对象要么有 model 桩子，要么在 `FROZEN_PROTOCOL_IMPLS` 里。
"""
