"""红区 dry-run 入口 —— **只读、只打印、什么都不提交**。

```
ma python/3.11.4
cd <workarea>/ewave_helper
python -m ewave_batch.redzone_dryrun --offdir <官方 GUI 跑过的那个 design 目录>
```

操作手册在 `docs/REDZONE_DRYRUN.md`（一页，一条命令能拿去跑，不用先读代码）。

## 它是干什么的

`PROJECT_BRIEF.md` §12「红区验证节奏」定的那一趟：**P1 一完成就去红区试 dry-run**，
因为 `core/cmd.py` 是整个工具的地基，解析错了 P2–P6 全白做。而这一趟零风险 ——
只读、只打印、不提交。

本机的 golden 测试只能对着**抄回来的样本**验（`tests/fixtures/production_cmd.local.json`
是人从一条真实命令抽出来的），它验不了「解析一个真实目录」这件事本身：目录里有没有
`remote_run_ewave.sh`、`run_ewave_*.sh` 是不是那个形状、`ptxt` 文件名里 corner 出现几次、
`gdsout_setup` 有没有第 25 个字段 —— 这些只有在红区、对着真目录才能知道。
**这个模块就是那一步。**

## 四件事，按顺序

1. `core.discover` 解析 OFFDIR → `SiteFacts`（坐标全部现场解析，源码里零站点标识符）；
2. 阶段 1：渲染 `gdsout_setup` + 拼 `strmout` argv；
3. 阶段 2：展开矩阵 → 每个 run 的**完整 argv** + **落地目录**；
4. **自带比对**：拿 OFFDIR 里那条真实命令当基准，逐 flag diff（`core.cmd.diff_flags`）+
   端口顺序 diff（`predict_all_ports` vs 官方 `-p` 串）+ `gdsout_setup` 往返自检。

## 🚨 关于「自带比对」的诚实交代（防自证）

比对里有一部分是**结构上必然相等**的：`--labelDepth` / `--viaMode` / `--sparamImpedance`
这些「默认表」flag，我们的取值就是从**同一份**官方脚本里学来的（§11 规则 1），
拿它去和它自己比永远绿。**空过的测试比没测更坏**（BRIEF §10 的那个真 bug），
所以报告里把每一条 flag 按来源分成两堆，并且分别报数：

* 「学自本目录」—— 结构上必然相等，**不算独立验证**；
* 「源码内置 / 机制层 / 轴 / 跨文件推导」—— 这些的取值不来自被比对的那份脚本，
  对不上就是真的对不上。**这一堆的条数才是这趟验证的实际含金量。**

端口那一路完全没有这个问题：`predict_all_ports` 是纯排序（D1b 的编码器），
官方 `-p` 顺序是人给的 —— 逐位比对是货真价实的。

## 只读守卫

`--offdir` 指的是**设计师的 spine**（`<workarea>/ewave_simulation/…`），CLAUDE.md 硬约束 4：
写一个字节都是违约。所以本模块：

* 一个写文件的调用都没有（读全部走 `_read_text`，`open(..., "r")`）；
* 所有「真跑时会写这里」的落点都经过 `WriteLedger.record()`，那里有一道守卫：
  落点在 `ewave_simulation/` 里面、或者落在 OFFDIR 里面 → **当场拒绝，退 1**，
  而且是在打印任何命令**之前**拒绝的（免得有人照着那条命令手工跑一遍）。

退出码（写进文档，机器可判）：

| 码 | 含义 |
|---|---|
| 0 | 比对完成，**一致** |
| 2 | 比对完成，**有差异**（逐条列在报告末尾） |
| 3 | **没能比对**（OFFDIR 里没有官方命令行）—— argv 和落地目录照样打印了 |
| 1 | 跑不起来（目录不对 / spec 非法 / 落点选在了 spine 里） |
"""

from __future__ import annotations

import argparse
import os
import posixpath
import shlex
import sys
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from . import __version__
from ._stdio import ascii_safe_stdio
from .core import cmd, discover, layout, matrix
from .core import spec as spec_module
from .model import (
    Axis,
    BatchOptions,
    CommandPlan,
    Design,
    EwaveBatchError,
    FlagDiff,
    FlagDict,
    PlanContext,
    PortDiff,
    Run,
    RunGroup,
    RunPaths,
    SiteFacts,
)
from .tools import ewave as ewave_tool
from .tools import strmout

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

EXIT_OK = 0
"""比对完成且一致。"""

EXIT_ERROR = 1
"""跑不起来：目录不是官方 design 目录 / spec 非法 / 落点选在了 spine 里。"""

EXIT_DIFF = 2
"""比对完成但有差异。**不是崩溃** —— 报告末尾逐条列出了差异和下一步。"""

EXIT_NO_BASELINE = 3
"""没能比对：OFFDIR 里没有 `run_ewave_*.sh`（或里面没有 ewave 那一行）。"""

DEFAULT_BATCH_ROOT = "./ewave_batches"
"""落点的默认根。**只用来算路径，一个目录都不会建。**

刻意是相对路径：红区的绝对路径全是站点坐标，源码里不许有（硬约束 1b）。
用户要落在别处就 `--batch-root`。
"""

DEFAULT_BATCH_NAME = "dryrun"
"""默认批次名。真跑时由 spec 的 `batch_name:` 或时间戳决定，这里只是让路径成型。"""

GENERIC_EWAVE_PROGRAM = "ewave"
GENERIC_STRMOUT_PROGRAM = "strmout"
"""PATH 上找不到工具时 argv 里用的**通用程序名**。

这是工具语义不是站点身份（`core.discover.find_tool("ewave")` 里本来就写着同一个词）。
为什么要占位而不是报错：`SiteFacts` 的 docstring 说得很清楚 ——「本机没装是正常的，
dry-run 照跑」。本机（Windows / 无 EDA 的 VM）PATH 上永远没有 `ewave`，
而 `core.cmd.build_command_plan` 在 `facts.ewave_bin` 为空时会抛 `DiscoveryError`
⇒ 不占位的话这个入口在本机根本跑不完，也就测不了。占位之后本机与红区走**同一条路**，
只有程序名那一格不同，而报告里会明写这一格是占位的。
"""

KEY_FLAG = cmd.KEY_FLAG
"""`--key` 的 flag 名 —— **从 `core.cmd` 取，不在这儿写第二份**。

取值来自 `SiteFacts.key`，由 `core.cmd.build_flag_layers` 补进默认表层
（和「`--parallel` 从 `-R` 的 `cpu=` 推导」同一处、同一形状）。
本模块只在「哪些 flag 属于学自本目录」那一步用到这个名字。
"""

GDSOUT_SETUP_NAME = "gdsout_setup"
"""官方 design 目录里那份 setup 的文件名。往返自检要读它（只读）。"""

SPINE_DIRNAME = layout.SPINE_DIRNAME
"""设计师 spine 的目录名。从 `core.layout` 取，不在这儿写第二份（漂了就没人发现）。"""


class ReadOnlyViolation(EwaveBatchError):
    """落点选在了不许写的地方（设计师的 spine，或 OFFDIR 里面）。

    本模块自己一个字节都不写，所以这个异常永远是**在打印命令之前**抛的 ——
    它拦的不是"我们正在写"，而是"照这条命令真跑起来会写到不该写的地方"。
    """


# --------------------------------------------------------------------------
# 只读守卫
# --------------------------------------------------------------------------


def _abs(path: str) -> str:
    """归一成可比较的绝对路径（Windows 上大小写不敏感，用 normcase 抹平）。"""
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))


def _is_inside(child: str, parent: str) -> bool:
    """`child` 在 `parent` 底下（含相等）。跨盘符 / 解析不了一律当"不在里面"。"""
    if not child or not parent:
        return False
    try:
        return os.path.commonpath([_abs(child), _abs(parent)]) == _abs(parent)
    except (OSError, ValueError):  # pragma: no cover - Windows 跨盘符
        return False


def _in_spine(path: str) -> bool:
    """路径里**任何一层**叫 `ewave_simulation` 就算在 spine 里。

    与 `core.layout._assert_outside_spine` / `mvp/redzone/cfg.sh` 同一条规则：
    不管它是最后一层还是中间层。
    """
    text = str(path).replace("\\", "/")
    return any(part == SPINE_DIRNAME for part in text.split("/"))


@dataclass
class WriteLedger:
    """「真跑时会写这些」的清单。**本模块只登记，不写。**

    存在的两个理由：

    1. 把「落地目录」这件交付物变成一份可打印、可核对的清单（BRIEF §12 要求输出落地目录）；
    2. 给只读守卫一个**唯一的收口**：每一条落点都从这里过一遍闸，落进 spine 或落进
       OFFDIR 就当场拒绝。守卫写在别处会漏，写在这里漏不掉 —— 因为报告里要打印它。
    """

    offdir: str = ""
    entries: list[tuple[str, str]] = field(default_factory=list)

    def record(self, path: str, what: str) -> str:
        """登记一条落点，返回原路径（好写成 `x = ledger.record(p, "…")`）。

        空路径直接忽略（上游算不出来时留空串是合法的诚实表示，不是遗漏）。
        """
        if not path:
            return path
        if _in_spine(path):
            raise ReadOnlyViolation(
                f"落点在设计师的 spine 里：{path}\n"
                f"  （{what}）\n"
                f"  {SPINE_DIRNAME}/ 是官方 GUI 的地盘，本工具只读它（CLAUDE.md 硬约束 4）。\n"
                "  下一步：--batch-root 换到 spine 外面，例如 <workarea>/ewave_batches"
            )
        if self.offdir and _is_inside(path, self.offdir):
            raise ReadOnlyViolation(
                f"落点在 --offdir 里面：{path}\n"
                f"  （{what}）\n"
                "  OFFDIR 是官方跑过的那个 design 目录，本命令对它**只读**。\n"
                "  下一步：--batch-root 换到 OFFDIR 外面"
            )
        # 同一个落点被登记两次（比如自带比对的对照 run 和用户矩阵里的某个 run 撞在一起）
        # 只留第一条 —— 清单是给人核对"会写到哪"的，重复项只会让人以为要写两遍。
        # ⚠️ 去重放在闸门**后面**：每一条都要过闸，哪怕它不进清单。
        if any(existing == path for existing, _ in self.entries):
            return path
        self.entries.append((path, what))
        return path


def _read_text(path: str) -> str:
    """读文本。**本模块唯一碰文件系统的地方，而且只有读。**

    与 `core.discover._read_text` 同款：UTF-8 + `errors="replace"`，
    一个坏字节不该让整趟 dry-run 崩掉。
    """
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


# --------------------------------------------------------------------------
# 报告的数据结构（全部是纯数据，渲染在 format_report 里）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StageOnePlan:
    """一个 design 的阶段 1（strmout）。"""

    design_key: str
    plan: CommandPlan
    setup_path: str
    """渲染出来的 `gdsout_setup` **将来**会写到哪（本模块不写）。"""
    gds_path: str
    rendered_setup: str


@dataclass(frozen=True)
class StageTwoPlan:
    """一个 run 的阶段 2（ewave）。"""

    run: Run
    plan: CommandPlan
    paths: RunPaths


@dataclass(frozen=True)
class ComparisonReport:
    """自带比对的结果。`status` ∈ `{"clean", "diff", "unavailable"}`。"""

    status: str
    reason: str = ""
    """`unavailable` 时说明为什么比不了。"""
    baseline_file: str = ""
    baseline_command: str = ""
    reference_run_id: str = ""
    flag_diff: FlagDiff | None = None
    port_diff: PortDiff | None = None
    gdsout_diff: FlagDiff | None = None
    fallback_diff: FlagDiff | None = None
    self_proving: tuple[str, ...] = ()
    """比对里「结构上必然相等」的那些 flag（取值学自被比对的同一份脚本）。"""
    independent: tuple[str, ...] = ()
    """真正独立验证的那些 flag。**这个数字才是这趟验证的含金量。**"""
    warnings: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return self.status == "clean"


@dataclass
class DryRunReport:
    """一趟 dry-run 的全部产出。`format_report` 负责把它变成人能看懂的文本。"""

    offdir: str
    spec_path: str
    batch_dir: str
    facts: SiteFacts
    designs: list[Design]
    axes: tuple[Axis, ...]
    stage_one: list[StageOnePlan]
    stage_two: list[StageTwoPlan]
    comparison: ComparisonReport
    ledger: WriteLedger
    notes: list[str] = field(default_factory=list)
    limit: int = 0
    show_gdsout: bool = False

    @property
    def exit_code(self) -> int:
        if self.comparison.status == "clean":
            return EXIT_OK
        if self.comparison.status == "diff":
            return EXIT_DIFF
        return EXIT_NO_BASELINE


# --------------------------------------------------------------------------
# 规划：坐标 → 批次 → 每个 run 的命令
# --------------------------------------------------------------------------


def resolve_tool_names(facts: SiteFacts) -> tuple[SiteFacts, list[str]]:
    """PATH 上没有 `ewave` / `strmout` 时用通用程序名占位，并返回一条说明。

    见 `GENERIC_EWAVE_PROGRAM` 的 docstring：不占位的话本机跑不完，也就测不了。
    **不改传进来的那份 facts**（`dataclasses.replace` 复制一份）—— 解析结果是证据，
    证据不该被消费方就地改写。
    """
    notes: list[str] = []
    updates: dict[str, str] = {}
    for attr, generic in (
        ("ewave_bin", GENERIC_EWAVE_PROGRAM),
        ("strmout_bin", GENERIC_STRMOUT_PROGRAM),
    ):
        if not getattr(facts, attr):
            updates[attr] = generic
            notes.append(
                f"PATH 上没有 {generic} ⇒ argv 里的程序名用通用名 {generic!r} 占位。"
                f"红区 `ma` 出对应模块之后这里会是绝对路径（源码里永远不写死）。"
            )
    if not updates:
        return facts, notes
    return replace(facts, **updates), notes


def official_design(facts: SiteFacts) -> Design:
    """用 OFFDIR 自己的三元组造一个 `Design` —— 自带比对要**同一个 design** 才是对等比较。"""
    missing = [
        name
        for name, value in (
            ("library", facts.library),
            ("topCell", facts.top_cell),
            ("view", facts.view),
        )
        if not value
    ]
    if missing:
        raise EwaveBatchError(
            f"官方 {GDSOUT_SETUP_NAME} 里缺字段: {', '.join(missing)} —— 造不出 design 三元组。\n"
            f"  下一步：打开 {posixpath.join(facts.official_run_dir, GDSOUT_SETUP_NAME)} 看看那几行还在不在"
        )
    return Design(
        library=facts.library,
        cell=facts.top_cell,
        view=facts.view,
        official_run_dir=facts.official_run_dir,
    )


def official_axes(facts: SiteFacts) -> tuple[Axis, ...]:
    """用 OFFDIR 自己的 corner / temperature 造两根**单取值**的轴。

    单取值的轴按定义不进 `<axes-slug>`（`core.matrix.varying_axes`）⇒ 目录结构与官方同构，
    只多插了一层常量 `base`。这正是「没给 spec 时该跑什么」的最小合理答案：
    **把官方那一次跑重放一遍**，然后逐 flag 比给用户看。
    """
    catalog = matrix.builtin_axis_catalog()
    axes: list[Axis] = []
    if facts.corner:
        axes.append(matrix.axis_with_values(catalog[matrix.AXIS_CORNER], [facts.corner]))
    if facts.temperature:
        axes.append(
            matrix.axis_with_values(catalog[matrix.AXIS_TEMPERATURE], [facts.temperature])
        )
    return tuple(axes)


def plan_context(
    design: Design,
    facts: SiteFacts,
    axes: tuple[Axis, ...],
    *,
    defaults: FlagDict,
    extra_flags: FlagDict,
    options: BatchOptions,
    batch_dir: str,
) -> PlanContext:
    """一个 design 的 `PlanContext`。存在只是为了不在三处各拼一遍。"""
    return PlanContext(
        design=design,
        facts=facts,
        axes=axes,
        defaults=dict(defaults),
        extra_flags=dict(extra_flags),
        options=options,
        batch_dir=batch_dir,
    )


def build_stage_one(
    design: Design,
    ctx: PlanContext,
    paths: RunPaths,
    ledger: WriteLedger,
) -> StageOnePlan:
    """阶段 1：渲染 `gdsout_setup` + 拼 strmout argv。**不写 setup 文件**（只登记落点）。"""
    fields = strmout.gdsout_fields_for_design(design, ctx, gds_path=paths.design_gds)
    template = ctx.facts.gdsout_template or strmout.DEFAULT_GDSOUT_TEMPLATE
    rendered = strmout.render_gdsout_setup(template, fields)

    setup_path = ledger.record(paths.design_gdsout, "阶段 1：渲染出来的 gdsout_setup")
    ledger.record(paths.design_gds, "阶段 1：strmout 导出的 GDS")
    ledger.record(
        posixpath.join(strmout.cdswork_dir(ctx.batch_dir), "cds.lib"),
        "阶段 1：strmout 的 cwd 需要一份能看见目标 library 的 cds.lib（真跑时由 driver 写）",
    )
    plan = strmout.build_strmout_plan(design, ctx, setup_path=setup_path)
    return StageOnePlan(
        design_key=matrix.design_key(design),
        plan=plan,
        setup_path=setup_path,
        gds_path=paths.design_gds,
        rendered_setup=rendered,
    )


def build_stage_two(
    run: Run,
    design: Design,
    ctx: PlanContext,
    batch_dir: str,
    ledger: WriteLedger,
) -> StageTwoPlan:
    """阶段 2：一个 run 的完整 argv + 落地目录。"""
    paths = layout.compute_run_paths(batch_dir, design, run)
    run.work_dir = ledger.record(paths.run_dir, f"阶段 2：--workDir（run {run.run_id}）")
    ledger.record(paths.cmd_sh, "阶段 2：命令留档 cmd_<corner>_<temp>.sh")
    ledger.record(paths.run_log, "阶段 2：我们自己 tee 的日志")
    if paths.ewave_dir:
        ledger.record(paths.ewave_dir, "阶段 2：eWave 自己建的 <corner>_<temp>/（产物在里面）")
    plan = ewave_tool.build_ewave_plan(run, ctx)
    return StageTwoPlan(run=run, plan=plan, paths=paths)


# --------------------------------------------------------------------------
# 自带比对
# --------------------------------------------------------------------------


def flag_origins(ctx: PlanContext, run: Run) -> dict[str, str]:
    """每个 flag 最终由**哪一层**给（`内置默认 < 默认表 < Extra < 轴 < 机制`，后者赢）。

    报告靠它把「结构上必然相等」和「真独立验证」分开 —— 见模块 docstring 的那段交代。
    """
    layers = cmd.build_flag_layers(run, ctx)
    origin: dict[str, str] = {}
    for layer in type(layers).MERGE_ORDER:
        for flag in getattr(layers, layer.value):
            origin[flag] = layer.value
    return origin


def split_self_proving(
    diff: FlagDiff,
    origin: dict[str, str],
    learned_keys: set[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """把参与比较的 flag 分成「学自本目录」和「真独立验证」两堆。

    判据：**最终生效的那一层是「默认表」，而且这个 flag 确实是从本次 OFFDIR 学来的。**
    两个条件缺一不可 ——

    * 只看层不看来源会把 `--parallel` 误判成自证：它也落在默认表层，但取值是从
      `remote_run_ewave.sh` 的 `-R "cpu=…"` 推出来的（**另一个文件**），
      拿去和 `run_ewave_*.sh` 里的 `--parallel=` 比是货真价实的跨文件校验；
    * 只看来源不看层会把「被上层盖掉了的默认值」误判成自证。

    `only_expected`（官方有、我们没有）的那些 flag 我们这边根本没有取值 ⇒ 一律算独立 ——
    它们本来就是差异，不该被算进"必然相等"里去粉饰。
    """
    compared = set(diff.same) | set(diff.only_actual) | set(diff.only_expected)
    compared |= {delta.flag for delta in diff.differing}
    self_proving = sorted(
        flag for flag in compared if origin.get(flag) == "defaults" and flag in learned_keys
    )
    independent = sorted(compared - set(self_proving))
    return tuple(self_proving), tuple(independent)


def compare_gdsout(facts: SiteFacts, rendered: str) -> tuple[FlagDiff | None, FlagDiff | None]:
    """`gdsout_setup` 的两条比对，返回 `(往返自检, 兜底模板对照)`。

    1. **往返自检**：官方 setup → `templatize` → `render` → 与官方逐字段比，
       忽略那 7 个随 design 变的占位字段。它验的是「模板化 + 渲染没有动过任何别的字段」
       （D1c：`convertPin geometry` / `case preserve` / `maxVertices 200` 错一个，
       GDS 内容就变了，而且跑得出来、数字也像）。
       ⚠️ 这是**往返**，模板本来就是从这份文件来的 —— 它不能证明"我们的模板和官方一致"。
    2. **兜底模板对照**：官方 setup 的 8 个 D1c 关键字段 vs 源码常量
       `DEFAULT_GDSOUT_TEMPLATE` 的同名字段。这条**不是**往返：一边是这个站点的真文件，
       一边是仓库里的字符串常量 ⇒ 对不上是真信息（说明兜底模板对这个站点不合适）。
    """
    setup_path = os.path.join(facts.official_run_dir, GDSOUT_SETUP_NAME)
    if not os.path.isfile(setup_path):  # pragma: no cover - discover 已经验过它存在
        return None, None
    official_text = _read_text(setup_path)

    round_trip = strmout.diff_gdsout_setup(
        rendered, official_text, ignore=tuple(strmout.GDSOUT_PLACEHOLDERS)
    )
    official_fields = strmout.parse_gdsout_fields(official_text)
    builtin_fields = strmout.parse_gdsout_fields(strmout.DEFAULT_GDSOUT_TEMPLATE)
    critical = set(strmout.GDSOUT_CRITICAL_FIELDS)
    ignore = tuple(sorted((set(official_fields) | set(builtin_fields)) - critical))
    fallback = strmout.diff_gdsout_setup(
        official_text, strmout.DEFAULT_GDSOUT_TEMPLATE, ignore=ignore
    )
    return round_trip, fallback


def compare_with_official(
    facts: SiteFacts,
    *,
    batch_dir: str,
    defaults: FlagDict,
    extra_flags: FlagDict,
    options: BatchOptions,
    ledger: WriteLedger,
) -> ComparisonReport:
    """★ 本模块的核心：拿 OFFDIR 里那条真实命令当基准，比一遍我们生成的命令。

    **对照组是现造的**：用 OFFDIR 自己的 design 三元组 + 自己的 corner/temperature
    造一个 run，让两边是同一个 design、同一个工艺角、同一个温度 ——
    否则 `--corner` / `--temperature` / `--emssTechFile` 必然不同，报出来的差异没有意义。
    （用户 spec 里那些 run 照样会被完整打印，只是不拿它们当比对基准。）

    比三样：逐 flag（`cmd.diff_flags`）、端口顺序（`cmd.diff_ports`）、`gdsout_setup`。
    """
    if not facts.official_command_line:
        return ComparisonReport(
            status="unavailable",
            reason=(
                f"{facts.official_run_dir} 里没解析出官方那条 ewave 命令 —— 没有基准就没法比。\n"
                "  多半是这个 design 目录里没有 run_ewave_*.sh（官方 GUI 只做过 stream out、\n"
                "  还没提交过求解），或者脚本形态变了。上面【1/5】的警告里写了具体原因。\n"
                "  下一步：换一个**跑完过**的 design 目录再试一次。"
            ),
        )

    design = official_design(facts)
    axes = official_axes(facts)
    runs = matrix.expand_runs([design], axes)
    run = runs[0]
    ctx = plan_context(
        design,
        facts,
        axes,
        defaults=defaults,
        extra_flags=extra_flags,
        options=options,
        batch_dir=batch_dir,
    )
    paths = layout.compute_run_paths(batch_dir, design, run)
    run.work_dir = ledger.record(paths.run_dir, "自带比对：对照 run 的 --workDir")
    plan = ewave_tool.build_ewave_plan(run, ctx)

    flag_diff = cmd.diff_flags(
        plan.flags, dict(facts.official_flags), ignore=cmd.DEFAULT_DIFF_IGNORE
    )
    origin = flag_origins(ctx, run)
    learned_keys = set(discover.learn_default_flags(facts)) | {KEY_FLAG}
    self_proving, independent = split_self_proving(flag_diff, origin, learned_keys)

    warnings: list[str] = []
    official_ports = facts.official_port_spec
    port_diff: PortDiff | None = None
    if official_ports.mapping:
        predicted = cmd.predict_all_ports([pin for _, pin in official_ports.mapping])
        port_diff = cmd.diff_ports(predicted, official_ports)
        grounded = len(official_ports.mapping) - len(official_ports.signal_ports)
        if official_ports.signal_ports and grounded > 0:
            warnings.append(
                f"官方用 -i 只挑了 {len(official_ports.signal_ports)}/"
                f"{len(official_ports.mapping)} 个 signal port（其余 {grounded} 个当接地端口），"
                "而 --all 把**全部**端口都当 signal —— `--all` 表达不了接地端口（D1b 留的口子）。\n"
                "      下一步：这个 design 要么在 spec 里显式写 ports（-p/-i 照抄官方），"
                "要么确认接地端口对结果无影响再用 --all。"
            )
    else:
        warnings.append(
            "官方命令里一个 -p 都没有 —— 端口顺序这一路没比。"
            "（官方也用 --all？那就没什么可比的；否则脚本形态可能变了。）"
        )

    round_trip, fallback = compare_gdsout(
        facts,
        strmout.render_gdsout_setup(
            facts.gdsout_template or strmout.DEFAULT_GDSOUT_TEMPLATE,
            strmout.gdsout_fields_for_design(design, ctx, gds_path=paths.design_gds),
        ),
    )

    bad = (
        not flag_diff.clean
        or (port_diff is not None and not port_diff.matched)
        or (round_trip is not None and not round_trip.clean)
    )
    return ComparisonReport(
        status="diff" if bad else "clean",
        baseline_file=facts.source_files.get("official_command_line", ""),
        baseline_command=facts.official_command_line,
        reference_run_id=run.run_id,
        flag_diff=flag_diff,
        port_diff=port_diff,
        gdsout_diff=round_trip,
        fallback_diff=fallback,
        self_proving=self_proving,
        independent=independent,
        warnings=tuple(warnings),
    )


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def build_report(
    offdir: str,
    *,
    spec_path: str = "",
    batch_root: str = DEFAULT_BATCH_ROOT,
    batch_name: str = DEFAULT_BATCH_NAME,
    limit: int = 0,
    show_gdsout: bool = False,
    env: Mapping[str, str] | None = None,
) -> DryRunReport:
    """跑完整趟规划，返回一份**纯数据**的报告（渲染是 `format_report` 的事）。

    不写任何文件；所有落点经 `WriteLedger` 过闸。

    `env` 是**工具解析的注入口**，原样透传给 `core.discover.find_tool`
    （"传了 env 就只看 env"）。不给就读真实环境 —— 那是红区和用户手跑时要的行为。
    给了就完全不读本机环境：`argv[0]` 于是只由入参决定，**在任何机器上都一样**。
    这条口子是测试用的，而且是必需的：没有它，"argv[0] 等于某个期望值"这类断言的
    结果取决于跑测试的那台机器 PATH 上有没有 `ewave`（本机没有 ⇒ 绿，
    红区 `ma ewave/…` 之后有 ⇒ 红），也就是**在唯一真正重要的机器上是红的**。
    回归测试：`tests/test_redzone_dryrun.py::ToolNameInjection`。
    """
    raw_facts = discover.discover_site_facts(offdir, env=env)
    facts, notes = resolve_tool_names(raw_facts)

    ledger = WriteLedger(offdir=facts.official_run_dir)
    batch_dir = os.path.join(os.path.expanduser(batch_root), batch_name).replace("\\", "/")
    ledger.record(posixpath.join(batch_dir, "batch.json"), "批次状态（resume 只认这一个文件）")
    ledger.record(posixpath.join(batch_dir, "runs.csv"), "汇总表")

    defaults: FlagDict = dict(discover.learn_default_flags(facts))
    if facts.key:
        notes.append(
            f"{KEY_FLAG} 不在学到的默认表里（它的取值是站点身份，"
            "「学默认表」那一步把它剔掉了），"
            "由 core.cmd.build_flag_layers 从 SiteFacts.key 补进默认表层 —— "
            "取值来自这个 OFFDIR，源码里一个字都没写。"
        )

    extra_flags: FlagDict = {}
    spec_defaults: FlagDict = {}
    options = BatchOptions(dry_run=True)
    designs: list[Design] = []
    axes: tuple[Axis, ...] = ()
    groups: tuple[RunGroup, ...] = ()

    if spec_path:
        spec = spec_module.load_spec(spec_path)
        designs = list(spec.designs)
        axes = tuple(spec.axes)
        groups = tuple(spec.groups)
        extra_flags = dict(spec.extra_flags)
        spec_defaults = dict(spec.defaults)
        options = replace(spec.options, dry_run=True)
        # spec 的 defaults 是**覆盖**（§11 规则 1：留空就全从官方目录学）。
        defaults.update(spec_defaults)
        if groups:
            names = ", ".join(group.name for group in groups)
            notes.append(
                f"spec 里有 {len(groups)} 个 run group（{names}）+ base。"
                "组的取值算在「全批次在变的轴」里 ⇒ **基线自己的目录名也会跟着变**"
                "（`base/...` 变成 `eqI-on/...` 之类）。下面每条 run 的 --workDir "
                "就是真跑会落的那个，逐字比对。"
            )
    else:
        designs = [official_design(facts)]
        axes = official_axes(facts)
        notes.append(
            "没给 --spec ⇒ 用 OFFDIR 自己的 (library, topCell, view) 和 corner/temperature "
            "造了一个单点批次 = 把官方那一次跑重放一遍。要看整个矩阵就加 --spec。"
        )

    facts_cache: dict[str, SiteFacts] = {_abs(facts.official_run_dir): facts}
    """官方目录（绝对路径）→ 解析结果。同一个目录只解析一遍。"""
    context_by_key: dict[str, PlanContext] = {}
    """design_key → 这个 design 的 `PlanContext`。**阶段 1 和阶段 2 必须用同一份** ——
    两处各拼一遍就会漂（`PlanContext` 存在的理由，见 model.py）。"""
    stage_one: list[StageOnePlan] = []
    stage_two: list[StageTwoPlan] = []

    # groups 必须传进去。这趟 dry-run 是红区**唯一**能在真提交之前核对落点的手段
    # （硬约束 3：本机没有 ewave/dsub），漏掉组的后果不是"少打印几行"：
    # `<axes-slug>` 的口径是全批次的 ⇒ 组的取值会改掉**基线自己**的目录名，
    # 于是预检打印的是 `base/...`、真跑落的是 `eqI-on/...` 外加一整组从没露过面的 run。
    # 2026-08-19 复核实测到这条。
    runs = matrix.expand_runs(designs, axes, options=options, groups=groups)
    # `PlanContext.axes` 要的是**全批次并集**（`core.spec.spec_to_batch` 存进
    # `BatchState.axes` 的也是这一份）：`cmd.build_flag_layers` 拿 run 的取值去轴的
    # 取值表里查 flag，只给 base 那份的话，组独有的取值一个都查不到。
    # 展开用的是 base 轴 + groups（上一行），两者不能混。
    plan_axes = tuple(matrix._batch_axes(axes, designs, groups))
    by_key = {matrix.design_key(design): design for design in designs}

    for design in designs:
        key = matrix.design_key(design)
        design_facts = facts
        design_defaults = dict(defaults)
        if design.official_run_dir and _abs(design.official_run_dir) != _abs(
            facts.official_run_dir
        ):
            # per-design 的坐标：spec 里点名了别的官方目录就现场解析那一个。
            # 坐标是 per-design 的 —— 每个 design 有自己的端口集合、自己的 top cell、
            # 甚至可能自己的 ptxt ⇒ 默认表也要从**它自己**那份目录学。
            cached = facts_cache.get(_abs(design.official_run_dir))
            if cached is None:
                cached, extra_notes = resolve_tool_names(
                    discover.discover_site_facts(design.official_run_dir, env=env)
                )
                notes.extend(extra_notes)
                facts_cache[_abs(design.official_run_dir)] = cached
            design_facts = cached
            design_defaults = dict(discover.learn_default_flags(cached))
            design_defaults.update(spec_defaults)

        ctx = plan_context(
            design,
            design_facts,
            plan_axes,
            defaults=design_defaults,
            extra_flags=extra_flags,
            options=options,
            batch_dir=batch_dir,
        )
        context_by_key[key] = ctx
        # 阶段 1 的路径只跟 design 有关，随便拿这个 design 的一个 run 算就行；
        # 一个 run 都没有（轴塌成空）时用一个只带 design_key 的壳 Run。
        sample = next((r for r in runs if r.design_key == key), Run(run_id=key, design_key=key))
        paths = layout.compute_run_paths(batch_dir, design, sample)
        if design.gds_path:
            notes.append(f"design {key}: spec 里直接给了 gds_path ⇒ 阶段 1 不跑。")
        else:
            stage_one.append(build_stage_one(design, ctx, paths, ledger))

    for run in runs:
        design = by_key[run.design_key]
        stage_two.append(
            build_stage_two(run, design, context_by_key[run.design_key], batch_dir, ledger)
        )

    comparison = compare_with_official(
        facts,
        batch_dir=batch_dir,
        defaults=defaults,
        extra_flags=extra_flags,
        options=options,
        ledger=ledger,
    )

    return DryRunReport(
        offdir=facts.official_run_dir,
        spec_path=spec_path,
        batch_dir=batch_dir,
        facts=facts,
        designs=designs,
        axes=axes,
        stage_one=stage_one,
        stage_two=stage_two,
        comparison=comparison,
        ledger=ledger,
        notes=notes,
        limit=limit,
        show_gdsout=show_gdsout,
    )


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------


def _width(text: str) -> int:
    """显示宽度（东亚宽字符算 2）。只为了那张事实表对得齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _row(label: str, value: object, indent: str = "  ") -> str:
    text = "" if value is None else str(value)
    pad = max(0, 22 - _width(label))
    return f"{indent}{label}{' ' * pad}{text}"


def _shell(argv) -> str:
    """argv → 一条可以直接粘回终端的命令行。"""
    return " ".join(shlex.quote(str(token)) for token in argv)


def _rule(title: str) -> str:
    return f"\n{'=' * 78}\n{title}\n{'=' * 78}"


def _flag_diff_lines(diff: FlagDiff, indent: str = "      ") -> list[str]:
    lines: list[str] = []
    for flag in diff.only_actual:
        lines.append(f"{indent}多给了 {flag}（官方那条没有）")
    for flag in diff.only_expected:
        lines.append(f"{indent}少给了 {flag}（官方那条有，我们没有）")
    for delta in diff.differing:
        lines.append(f"{indent}取值不同 {delta.flag}: 我们={delta.actual!r} 官方={delta.expected!r}")
    return lines


def format_report(report: DryRunReport) -> str:
    """报告 → 文本。**纯函数**（不 print、不碰文件系统），测试直接对着它断言。"""
    facts = report.facts
    out: list[str] = []
    add = out.append

    add(_rule("ewave_batch 红区 dry-run —— 只读：不写任何文件，不提交任何 job"))
    add(_row("工具版本", __version__))
    add(_row("python", sys.version.split()[0]))
    add(_row("OFFDIR（只读）", report.offdir))
    add(_row("spec", report.spec_path or "(没给 —— 见下面的说明)"))
    add(_row("落点（不会建）", report.batch_dir))

    # ---- 1/5 坐标 ----
    add(_rule("[1/5] 站点坐标 —— 全部现场解析（core.discover），源码里一个都没有"))
    add(_row("library / topCell", f"{facts.library} / {facts.top_cell}"))
    add(_row("view", facts.view))
    add(_row("corner / temperature", f"{facts.corner or '(没解析出来)'} / {facts.temperature or '(没解析出来)'}"))
    add(_row("eWave 目录名", facts.ewave_dir_name or "(预测不出来)"))
    add(_row("layerMap", facts.layer_map or "(空)"))
    add(_row("ptxt", facts.ptxt or "(没解析出来)"))
    add(_row("ptxt 文件名模板", facts.ptxt_name_template or "(没解析出来)"))
    add(_row("dsub -A / -q / -R", f"{facts.dsub_account or '?'} / {facts.dsub_queue or '?'} / {facts.dsub_resources or '?'}"))
    add(_row("ewave / strmout", f"{facts.ewave_bin} / {facts.strmout_bin}"))
    add(_row("官方端口", f"{len(facts.official_port_spec.mapping)} 个 -p，{len(facts.official_port_spec.signal_ports)} 个 -i"))
    add(_row("官方 flag", f"{len(facts.official_flags)} 个"))
    add(_row("学到的默认表", f"{len(discover.learn_default_flags(facts))} 条"))
    if facts.source_files:
        add("  每个坐标是从哪个文件来的：")
        for name in sorted(set(facts.source_files.values())):
            add(f"    {name}")
    if facts.warnings:
        add(f"  ⚠ 解析警告 {len(facts.warnings)} 条（软失败：字段留空，不影响其余部分）：")
        for warning in facts.warnings:
            add(f"    - {warning}")
    for note in report.notes:
        add(f"  · {note}")

    # ---- 2/5 阶段 1 ----
    add(_rule("[2/5] 阶段 1：strmout（per-design，整个设定矩阵共用一份 GDS）"))
    if not report.stage_one:
        add("  (没有阶段 1 —— spec 里每个 design 都直接给了 gds_path)")
    for item in report.stage_one:
        add(f"  design {item.design_key}")
        add(_row("命令", _shell(item.plan.argv), indent="    "))
        add(_row("cwd", item.plan.cwd or "(继承调用方)", indent="    "))
        add(_row("会写 setup", item.setup_path, indent="    "))
        add(_row("产物 GDS", item.gds_path, indent="    "))
        if report.show_gdsout:
            add("    渲染出来的 gdsout_setup（本命令**不写**它，只打印）：")
            for line in item.rendered_setup.splitlines():
                add("      | " + line)

    # ---- 3/5 阶段 2 ----
    add(_rule("[3/5] 阶段 2：每个 run 的完整命令 + 落地目录"))
    total = len(report.stage_two)
    add(f"  共 {total} 个 run（{len(report.designs)} 个 design × 轴的笛卡尔积）")
    shown = report.stage_two if report.limit <= 0 else report.stage_two[: report.limit]
    for index, item in enumerate(shown, start=1):
        add(f"\n  --- run {index}/{total}: {item.run.run_id}")
        add(_row("轴取值", item.run.axis_values or "(没有轴)", indent="      "))
        add(_row("--workDir", item.paths.run_dir, indent="      "))
        add(_row("eWave 自建目录", item.paths.ewave_dir or "(预测不出来，跑完现场找)", indent="      "))
        add(_row("命令留档", item.paths.cmd_sh, indent="      "))
        add(_row("归档前缀", item.paths.sparam_prefix + ".sNp", indent="      "))
        add(f"      命令（{len(item.plan.argv)} 个 argv 项，可直接粘去手工跑）：")
        add("        " + _shell(item.plan.argv))
    if report.limit > 0 and total > report.limit:
        add(f"\n  …… 其余 {total - report.limit} 个 run 只列 id 和落点（去掉 --limit 看全部）：")
        for item in report.stage_two[report.limit :]:
            add(f"      {item.run.run_id}  ->  {item.paths.run_dir}")

    # ---- 4/5 自带比对 ----
    add(_rule("[4/5] 自带比对 —— 拿 OFFDIR 里那条真实命令当基准"))
    comparison = report.comparison
    if comparison.status == "unavailable":
        add("  ✗ 没能比对：")
        for line in comparison.reason.splitlines():
            add("    " + line)
    else:
        add(_row("基准文件", comparison.baseline_file or "(?)"))
        add(_row("对照 run", comparison.reference_run_id))
        add("  （对照 run 是用 OFFDIR 自己的 design/corner/temperature 现造的，")
        add("    这样两边是同一个格子，比出来的差异才有意义）")

        diff = comparison.flag_diff
        assert diff is not None  # status != unavailable 时必然有
        add("\n  a) 逐 flag（core.cmd.diff_flags，精确名匹配，绝不前缀匹配）")
        add(_row("参与比较", f"{diff.compared_count} 条", indent="     "))
        add(_row("一致", f"{len(diff.same)} 条", indent="     "))
        add(_row("忽略", f"{len(diff.ignored)} 条：{' '.join(diff.ignored) or '(无)'}", indent="     "))
        add("       （忽略的每一条都是**有意不同**：--workDir 是本工具存在的理由，")
        add("         --gds 走批次的 gds/，--all 取代官方逐个 -p，--includePortOrder 生产不开我们开）")
        add(_row("其中「学自本目录」", f"{len(comparison.self_proving)} 条 ⇒ 结构上必然相等，不算独立验证", indent="     "))
        if comparison.self_proving:
            add("       " + " ".join(comparison.self_proving))
        add(_row("★ 真独立验证", f"{len(comparison.independent)} 条（取值来自源码内置 / 机制层 / 轴 / 跨文件推导）", indent="     "))
        if comparison.independent:
            add("       " + " ".join(comparison.independent))
        if diff.clean:
            add("     ✓ 逐条相同")
        else:
            add("     ✗ 有差异：")
            out.extend(_flag_diff_lines(diff))

        add("\n  b) 端口顺序（D1b：--all 能不能逐位复现官方的 -p 顺序）")
        if comparison.port_diff is None:
            add("     (官方命令里没有 -p，跳过)")
        else:
            port = comparison.port_diff
            add(_row("逐位比较", f"{port.compared_count} 个端口", indent="     "))
            if port.matched:
                add("     ✓ 逐位一致 —— predict_all_ports（case-sensitive ASCII 排序）复现了官方顺序")
            else:
                add(f"     ✗ 第 {port.first_mismatch_index} 位起对不上")
                if port.only_actual:
                    add(f"       我们多了: {' '.join(port.only_actual)}")
                if port.only_expected:
                    add(f"       官方多了: {' '.join(port.only_expected)}")
                if not port.only_actual and not port.only_expected:
                    add("       两边 pin 集合相同、只是顺序不同 —— 这类错最难发现，")
                    add("       而且 .sNp 里看不出来（Touchstone 只按 P00x 排，名字被丢掉）")

        add("\n  c) gdsout_setup（D1c：那 8 个字段错一个，mesh 就变了而且跑得出来）")
        if comparison.gdsout_diff is None:
            add("     (读不到官方 gdsout_setup，跳过)")
        else:
            rt = comparison.gdsout_diff
            add(_row("往返自检", f"比了 {rt.compared_count} 个字段（忽略 7 个随 design 变的）", indent="     "))
            if rt.clean:
                add("     ✓ 模板化 + 渲染没有动过任何别的字段")
            else:
                add("     ✗ 往返之后字段变了 —— 这是 bug，不是配置问题：")
                out.extend(_flag_diff_lines(rt))
            add("       ⚠ 这是**往返**（模板就是从这份文件模板化来的），它证明不了")
            add("         「我们的模板和官方一致」，只证明「没被顺手改过」。")
        if comparison.fallback_diff is not None:
            fb = comparison.fallback_diff
            add(_row("兜底模板对照", f"{fb.compared_count} 个 D1c 关键字段（源码常量 vs 这个站点）", indent="     "))
            if fb.clean:
                add("     ✓ 一致 —— 源码里那份兜底模板对这个站点也是对的")
            else:
                add("     ⚠ 不一致（不影响本次：我们用的是从 OFFDIR 学来的那份模板）：")
                out.extend(_flag_diff_lines(fb))

        if comparison.warnings:
            add("")
            for warning in comparison.warnings:
                add(f"  ⚠ {warning}")

    # ---- 5/5 结论 ----
    add(_rule("[5/5] 结论"))
    add(f"  这趟没有写任何文件。下面 {len(report.ledger.entries)} 条是**真跑时**才会写的落点：")
    for path, what in report.ledger.entries[:12]:
        add(f"    {path}")
        add(f"        └ {what}")
    if len(report.ledger.entries) > 12:
        add(f"    …… 其余 {len(report.ledger.entries) - 12} 条同构（每个 run 各一份）")
    add("")
    out.extend(_conclusion_lines(report))
    add("")
    return "\n".join(out) + "\n"


def _conclusion_lines(report: DryRunReport) -> list[str]:
    """一眼能看懂的结论 + 明确的下一步。"""
    comparison = report.comparison
    lines: list[str] = []
    if comparison.status == "clean":
        diff = comparison.flag_diff
        assert diff is not None
        lines.append("  ✅ 一致。")
        lines.append(
            f"     我们生成的命令与官方那条在 {diff.compared_count} 个 flag 上逐条相同"
            f"（其中 {len(comparison.independent)} 条是独立验证的），"
            + (
                f"端口顺序 {comparison.port_diff.compared_count} 位逐位相同。"
                if comparison.port_diff is not None
                else "端口那一路没得比。"
            )
        )
        if comparison.warnings:
            lines.append(f"     ⚠ 但有 {len(comparison.warnings)} 条警告（在上面 [4/5]），值得看一眼。")
        lines.append("  下一步：")
        lines.append("    1) 把这个 OFFDIR 写进 spec 的 designs[].official_run_dir；")
        lines.append("       样例：python -c \"import sys; from ewave_batch.core.spec import EXAMPLE_SPEC; sys.stdout.buffer.write(EXAMPLE_SPEC.encode('utf-8'))\" > my_spec.yaml")
        lines.append("    2) 带 --spec my_spec.yaml 再跑一次，确认整个矩阵的落地目录互不覆盖；")
        lines.append("    3) 把这份输出整段贴回给开发（里面有站点坐标 ⇒ **只在公司内部流转**）。")
        lines.append("    真跑不走这个命令 —— 本命令永远不提交任何 job。")
        return lines

    if comparison.status == "unavailable":
        lines.append("  ⚠ 没能比对 —— argv 和落地目录已经打印在上面，但没有基准可对。")
        for line in comparison.reason.splitlines():
            lines.append("    " + line)
        return lines

    lines.append("  ❌ 有差异 —— 先别拿这些命令去跑，逐条看下面这几处：")
    diff = comparison.flag_diff
    if diff is not None and not diff.clean:
        for flag in diff.only_expected:
            lines.append(f"    · 少给了 {flag}：官方那条有、我们没有。")
            lines.append("      多半是「学默认表」时被剔除规则吃掉了，或者它该由某一层负责而没人给。")
        for flag in diff.only_actual:
            lines.append(f"    · 多给了 {flag}：官方那条没有。")
            lines.append("      确认它是不是我们有意加的（--includePortOrder 是有意的，已在忽略表里）。")
        for delta in diff.differing:
            lines.append(f"    · {delta.flag} 取值不同：我们={delta.actual!r}，官方={delta.expected!r}。")
            lines.append("      看这个 flag 由哪一层给：轴 → 检查 spec 的取值；默认表 → 官方目录里学错了；")
            lines.append("      内置 → core.cmd.BUILTIN_DEFAULT_FLAGS 与这个站点不符（这是真发现，值得改代码）。")
    port = comparison.port_diff
    if port is not None and not port.matched:
        lines.append(f"    · 端口顺序第 {port.first_mismatch_index} 位起对不上 —— **这条最要紧**。")
        lines.append("      D1b（--all 逐位复现官方 -p 顺序）在这个站点不成立 ⇒ 不能用 --all，")
        lines.append("      必须在 spec 里显式写 ports。否则 .sNp 的端口编号会整体错位，而且静默。")
    gdsout = comparison.gdsout_diff
    if gdsout is not None and not gdsout.clean:
        lines.append("    · gdsout_setup 往返之后字段变了 —— 这是工具自己的 bug，请把这段贴回给开发。")
    lines.append("  下一步：把 [4/5] 整段贴回给开发（含站点坐标 ⇒ 只在公司内部流转）。")
    lines.append("  这趟仍然没有写任何文件，也没有提交任何 job —— 差异不会造成任何后果。")
    return lines


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """造 `argparse` 解析器。单独一个函数是为了让测试直接拿它验参数面，不用起进程。"""
    parser = argparse.ArgumentParser(
        prog="python -m ewave_batch.redzone_dryrun",
        description=(
            "红区 dry-run：解析一个官方 GUI 跑过的 design 目录，打印我们会生成的全部命令"
            "和落地目录，并拿官方那条真实命令做逐 flag / 逐端口比对。"
            "只读 —— 不写任何文件，不提交任何 job。"
        ),
        epilog=(
            "退出码：0=一致  2=有差异  3=没能比对（目录里没有官方命令）  1=跑不起来。"
            " 详见 docs/REDZONE_DRYRUN.md"
        ),
    )
    parser.add_argument(
        "--offdir",
        required=True,
        metavar="DIR",
        help="官方 GUI 跑过的那个 design 目录（里面有 gdsout_setup）。**只读**。",
    )
    parser.add_argument(
        "--spec",
        default="",
        metavar="FILE",
        help="可选：批次 spec（YAML/JSON）。不给就用 OFFDIR 自己的 corner/temperature 造一个单点批次。",
    )
    parser.add_argument(
        "--batch-root",
        default=DEFAULT_BATCH_ROOT,
        metavar="DIR",
        help=f"落点的根，只用来算路径、**不会建目录**（默认 {DEFAULT_BATCH_ROOT}）。",
    )
    parser.add_argument(
        "--batch-name",
        default=DEFAULT_BATCH_NAME,
        metavar="NAME",
        help=f"批次名（默认 {DEFAULT_BATCH_NAME}）。",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="只详细打印前 N 个 run（其余只列 id 和落点）。0 = 全部打印（默认）。",
    )
    parser.add_argument(
        "--show-gdsout",
        action="store_true",
        help="连渲染出来的 gdsout_setup 一起打印（本命令仍然不写它）。",
    )
    return parser


def main(argv=None) -> int:
    """命令行入口。**第一件事是 `ascii_safe_stdio()`** —— 红区 LANG 常是 C，
    不做这一步一个中文 print 就让进程退 1（`scripts/check.sh` 第 4 步在查这条）。
    """
    ascii_safe_stdio()
    args = build_parser().parse_args(argv)
    try:
        report = build_report(
            args.offdir,
            spec_path=args.spec,
            batch_root=args.batch_root,
            batch_name=args.batch_name,
            limit=args.limit,
            show_gdsout=args.show_gdsout,
        )
    except EwaveBatchError as exc:
        # EwaveBatchError 的消息全是「一句话说清 + 下一步怎么办」，原样打给用户。
        # 别的异常一律让它炸出 traceback（那是我们的 bug，不该被吞成一句好话）。
        print(f"\n跑不起来：{exc}\n", file=sys.stderr)
        return EXIT_ERROR
    print(format_report(report))
    return report.exit_code


if __name__ == "__main__":  # pragma: no cover - 进程入口
    sys.exit(main())
