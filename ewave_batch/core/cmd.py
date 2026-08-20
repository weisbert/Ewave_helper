"""`ewave_batch.core.cmd` —— 四层合并 → argv；冲突检测；逐 flag diff。

**这是整个工具的地基**：拼错了，P2–P6 全白做（BRIEF §12）。所以这个模块的每一条规则
都要能指回一份证据，而不是"看起来合理"。

三件事：

1. **五层合并**（`内置默认 < 默认表 < Extra flags < 轴 < 锁死`，`FlagLayers.MERGE_ORDER`）。
   轴永远赢用户层 —— 轴是 run 的身份、会进目录名，目录名和实际跑的值对不上正是原生 GUI
   覆盖坑的根因（BRIEF §11 规则 2），不能自己再造一遍。
2. **冲突检测**：用户在 Extra flags 里碰了机制 flag 或轴 flag → 拒绝并说人话。
3. **逐 flag 集合 diff**：`diff_flags` / `diff_ports` 同时服务两处 ——
   本机 golden 测试（`tests/test_cmd_golden.py`）和红区那趟"解析真实目录 + 自带比对"的
   dry-run（BRIEF §12）。**一个函数，两个用户**，所以它不许知道自己在被谁调用。

🚨 CLAUDE.md 硬约束 1b：本文件里出现的一切具体取值都是 **eWave 的工具语义**
（flag 名、`0.4` 这种数值），**不是站点身份**。library / cell / view / ptxt 路径 / key /
端口名一律没有默认值，运行时从官方 run 目录解析（`SiteFacts`）。

路径一律用 `posixpath` 拼：目标机是 Linux，开发机是 Windows —— 用 `os.path` 会让
dry-run 在两台机器上打印出不同的命令，而这个模块的产物是要拿去逐字比对的。
"""

from __future__ import annotations

import posixpath
from collections.abc import Mapping, Sequence
from types import MappingProxyType

from ..model import (
    RUN_LOG_NAME,
    RUN_LOG_TEMPLATE,
    GDS_DIRNAME,
    LONG_FLAG_ASSIGN,
    PLACEHOLDER_PTXT,
    PLACEHOLDER_VALUE,
    USER_FORBIDDEN_FLAGS,
    Axis,
    CommandPlan,
    DiscoveryError,
    FlagConflict,
    FlagConflictError,
    FlagDelta,
    FlagDict,
    FlagDiff,
    FlagLayer,
    FlagLayers,
    FlagValue,
    PlanContext,
    PortDiff,
    PortMode,
    PortSpec,
    Run,
    SiteFacts,
    SpecError,
    Stage,
)

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

BUILTIN_DEFAULT_FLAGS: Mapping[str, FlagValue] = MappingProxyType(
    {
        "--labelDepth": "0",
        # ★ 网格密度：0.5 = eWave 自己的默认值（用户 2026-08-19 定）。
        # 原来是 0.4 —— 那是**某一个 design 的某一次运行**的值，见下面「为什么改」。
        "-e": "0.5",
        "-d": "0.5",
        "--viaMergeSpace": "0.5",
        # ★ 显式 OFF（用户 2026-08-19 定）。`False` = 「显式缺席」，`render_flags` 不渲染它，
        # 而且它能把学到的默认表里那个 `--equalCurrent` **抵消掉** —— 光是"不写"做不到这件事。
        "--equalCurrent": False,
        "--viaMode": "1",
        "--multiSweep": "adaptive,0:0.1:40",
        "--sparamImpedance": "50",
        "--relativeTolerance": "1e-05",
        "--relativeCurrentTolerance": "0.001",
    }
)
"""**兜底**的内置默认。只在没学到默认表时生效。

## ⚠️ 2026-08-19 红区实测推翻了这张表原来的立论，务必读一遍

原来这张表整个抄自 `PROJECT_BRIEF.md` §6「已知的生产默认值（**来自真实生产脚本**）」——
而那串值只来自**一个 design 的一次运行**（MVP 用的那个 run 目录）。§6 的标题是单数的，
我们却把它当成了"生产默认值"这样一个全局常量。

红区第一次 dry-run（换了**另一个** view 的官方 run 目录）当场把这个假设打掉了：

| flag | MVP 那个目录 | 另一个目录 |
|---|---|---|
| `-e` / `-d` / `--viaMergeSpace` | 0.4 / 0.4 / 0.4 | **0.5 / 0.5 / 0.5**（= eWave 自己的默认） |
| `--equalCurrent` | 有 | **没有** |
| `--multiSweep` | `adaptive,0:0.1:40` | `adaptive,0:0.1:30` |

两边都是"真实生产命令"，只是**设计师给这两个 design 配的设定本来就不一样** ——
一个调过网格、一个用了 GUI 默认。所以"生产默认值"这个东西根本不存在。

⇒ 这张表的定位因此改了：**它不再声称"复现生产"，它是"用户选定的兜底默认"。**
当前取值由用户 2026-08-19 拍板：网格 0.5/0.5/0.5（eWave 默认）、`--equalCurrent` 关、
频率 0–40 GHz 步进 0.1 自适应。

**不要再拿它去对任何一个官方 run 目录求逐条相等** —— 那正是上面那个错误。
`tests/test_cmd_golden.py` 里对应的测试已经按这条重写：非轴 flag 仍逐条对生产命令，
轴掌管的 flag 只验形状、不验取值（因为它们本来就因 design 而异）。

真正想"复现某个 design 官方那次"的话，路径是 `core.discover.learn_default_flags`
从那个 run 目录学，或者在 spec/界面上把轴显式写死 —— 不是改这张表。

---

原文保留：

§11 规则 1 是"默认表的值不写死在源码，第一次运行时从官方 run 目录学"，所以这张表的地位
是**最低层**：`ctx.defaults`（学来的）一来就把它盖掉。它存在的意义只有一个 ——
红区之外（本机测试、公开克隆者）没有官方 run 目录可学时，命令仍然拼得出来。

里面**没有**这三类东西，各有理由：

* 机制 flag（`--nogui` / `-m` / `--cadencePins`）—— 在 `locked` 层，由工具自己算；
* `--key` —— 站点身份，源码里写死一个值就是把站点坐标提交上去了（硬约束 1b）。
  它的取值只能来自 `SiteFacts.key`，由 `build_flag_layers` 补进 `defaults` 层（见 `KEY_FLAG`）；
* `--parallel` —— 跟 dsub 的 `cpu=` 联动，见 `build_flag_layers`，写死一个数字会
  在换机型时静默损失算力（BRIEF §6「`--parallel` ≠ `cpu`」）。

**这张表是被 golden 测试盯着的**：`tests/test_cmd_golden.py` 拿它和真实生产命令逐条比，
凡是两边都有的键，值必须相等。改这里的任何一个数字，那条测试会当场红。

用 `MappingProxyType` 是防手滑：模块级可变 dict 被某个调用方 `.update()` 一下，
后面所有 run 的默认值就变了，而且完全静默。
"""

KEY_FLAG = "--key"
"""`--key` 的 flag 名。**取值永远来自 `SiteFacts.key`，源码里一个字都不写。**

它是 BRIEF §6「已知的生产默认值」里的一员（官方那条命令有它），但它的取值是站点身份 ⇒

* `core.discover.learn_default_flags` 把它当站点身份从「学到的默认表」里**剔掉**了；
* `BUILTIN_DEFAULT_FLAGS` 里也不许写死它（硬约束 1b）。

于是不特别处理的话它**谁都不给** —— 端到端拼出来的命令缺 `--key`，而官方那条有。
补的地方是 `build_flag_layers` 的 `defaults` 层，与「`--parallel` 从 `-R` 的 `cpu=` 推导」
同一处、同一形状：**值来自站点发现，不来自源码常量**。

🚨 `facts.key` 为空时**不许凭空造一个**（`build_flag_layers` 里那个 `if`）——
编出来的 key 会让 run 直接失败，而且失败原因极难查。宁可缺，也不许编。
"""

DEFAULT_DIFF_IGNORE: tuple[str, ...] = (
    "--workDir",
    "--gds",
    "--all",
    "--includePortOrder",
)
"""与参考命令比对时**默认**忽略的 flag。**精确名匹配，绝不前缀匹配。**

为什么恰好是这四个 —— 每一条都是"我们和官方必然不同，而且是有意不同"：

| flag | 为什么必然不同 |
|---|---|
| `--workDir` | 官方是 `.`，我们给每个组合一个独立目录 —— **这就是本工具存在的理由**（D2） |
| `--gds` | 官方用 run 目录里的相对文件名，我们用批次 `gds/` 下的路径（D1a：per-design 只 strmout 一次） |
| `--all` | 官方逐个写 `-p`/`-i`，我们用 `--all` 自动发现（D1b）。端口本身由 `diff_ports` 比，不在这里比 |
| `--includePortOrder` | 生产不开，**我们必须开**（D1d：归档会把 .sNp 搬离命令行，而端口映射只存在于命令行） |

🚨 **不在这张表里的，就是必须逐字对上的** —— 尤其 `--emssTechFile`（corner 轴要同时改
它和 `--corner`，少改一个就是"目录名说 typical、实际用了别的工艺角"，而且跑得出来、
数字也像）、`--sparam`（= cell 名，我们和官方应当一致）、`--key`（学来的，对不上说明学错了）。

⚠️ **这张表里故意没有 `--sparam`。** MVP 踩过的那个 bug 就长在这儿：排除规则写 `--sparam`
**前缀**误伤了 `--sparamImpedance`，两边同时被跳过，diff 空得非常好看但根本没比
（BRIEF §10）。`diff_flags` 的 `ignore` 因此是**精确键匹配**，加上 `FlagDiff.compared_count`
让"比了几条"变成可断言的数字。回归测试见 `tests/test_cmd_golden.py` 的
`test_ignore_is_exact_match_not_prefix`。
"""

_LOCKED_FLAG_NAMES: tuple[str, ...] = (
    "--nogui",
    "-m",
    "--workDir",
    "--gds",
    "--top",
    "--sparam",
    "--cadencePins",
    "--all",
    "--includePortOrder",
)
"""机制层里我们实际会写的 flag。`MECHANISM_FLAGS` 是"用户不许碰"的清单，这里是"我们会写
哪些"，两者应当一致 —— `tests/test_cmd_golden.py::test_locked_layer_covers_mechanism_flags`
盯着这条（少写一个 = 某个机制没生效，多写一个 = 用户被禁了一个其实没人管的 flag）。"""


# --------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------


def _is_long(flag: str) -> bool:
    """`--corner` 是长 flag（渲染成 `--corner=typical` 一项），`-e` 是短 flag（两项）。"""
    return flag.startswith("--")


def _ptxt_path_for_corner(facts: SiteFacts, corner: str) -> str:
    """算某个 corner 的 ptxt 路径。

    正主是 `ewave_batch.core.discover.ptxt_path_for_corner`（P2）—— 能 import 到就用它，
    **这里的实现只是 P2 落地之前的等价兜底**，语义照 `SiteFacts.ptxt_name_template` 的
    docstring 写：ptxt 文件名里的 corner 换成 `{corner}` 之后就是模板。

    为什么不在模块顶层 import：P1 与 P2 并行开发，`core.discover` 此刻可能还不存在；
    而且这是**运行时坐标**，`SiteFacts` 里全是站点身份，源码里一个真实取值都不许有。
    """
    try:
        from . import discover as _discover
    except ImportError:
        _discover = None  # type: ignore[assignment]
    if _discover is not None:
        return str(_discover.ptxt_path_for_corner(facts, corner))

    if facts.ptxt_dir and facts.ptxt_name_template:
        return posixpath.join(facts.ptxt_dir, facts.ptxt_name_template.replace("{corner}", corner))
    if facts.ptxt and facts.corner and facts.corner != corner:
        head, sep, name = facts.ptxt.rpartition("/")
        # 只在**文件名**里换 corner —— 目录名里也可能出现同样的字串，换错了就是
        # "指向另一个工艺角的 ptxt"，而它照样跑得出来、数字也像。
        return head + sep + name.replace(facts.corner, corner)
    return facts.ptxt


def _render_ports(port_spec: PortSpec) -> list[str]:
    """端口部分的 argv。`tools.ewave.render_ports`（P3）是它的公开门面，**转过来调这里**。

    为什么逻辑落在 `core.cmd` 而不是 `tools.ewave`：`build_command_plan` 要在 P1 就产出
    完整 argv（红区 dry-run 是 P1 的交付判据），而 `tools.ewave` 是 P3 的模块 ——
    真身放这边，P3 那个"薄封装"才真的是薄的，也不会出现两份会漂移的端口渲染。

    * `ALL` → 这里什么都不产出。`--all` 已经在 `locked` 层的 flag dict 里了
      （好让 `diff_flags` 看得见它），由 `render_flags` 渲染，这里再给一次会重复。
    * `EXPLICIT` → `-p P000=<pin> …  -i <port_id> …`，**逐字保序**（顺序就是映射，BRIEF §5）。
    * `EXPLICIT` 且 `signal_ports` 为空 → 一个 `-i` 都不给。照 eWave help 的原文
      （"`-i` 在 `-p` 集合里挑 signal port，其余接地"），不给 `-i` 就是不做这个挑选。
    """
    if port_spec.mode is PortMode.ALL:
        return []
    argv: list[str] = []
    for port_id, pin in port_spec.mapping:
        argv.extend(["-p", f"{port_id}={pin}"])
    for port_id in port_spec.signal_ports:
        argv.extend(["-i", port_id])
    return argv


def predict_all_ports(pins: Sequence[str]) -> PortSpec:
    """预测 `--all` 会给出的端口映射：**case-sensitive ASCII 排序后依次编号** P000, P001…

    这是 D1b 的编码器，也是"不依赖 GUI"成立的全部依据 —— 官方 GUI 的 `-p` 顺序被实测
    证明就是 pin 名的 case-sensitive ASCII 排序（17/17 吻合，大小写不敏感排序则不吻合，
    见 BRIEF §5 与 `references/checks/check_port_order.py`），而 `--all` 的定义正是
    "assign them in lexicographical order to P000, P001, …"。

    `sorted()` 的默认行为就是逐码位比较 = ASCII 排序，**不要给它加 `key=str.lower`**：
    那会把小写 pin 排到大写 pin 中间去，端口编号整体错位，而且**静默**错位 ——
    产物照样出得来、数字还挺像（BRIEF §5「`--all` 的代价」）。

    两个用途：

    * 本机 golden 回归（`tests/test_cmd_golden.py`，拿真实的 pin 名当基准）；
    * 红区 dry-run 的自带比对：`diff_ports(predict_all_ports(官方 -p 里的 pin 名), 官方 port_spec)`
      —— 那才是在真实数据上重跑一遍 D1b。

    重复的 pin 名会被保留（GDS 里不该有，真有就让它显形，别在这儿悄悄去重）。
    """
    ordered = sorted(pins)
    mapping = tuple((f"P{index:03d}", pin) for index, pin in enumerate(ordered))
    return PortSpec(
        mode=PortMode.EXPLICIT,
        mapping=mapping,
        signal_ports=tuple(port_id for port_id, _ in mapping),
    )


def _drop_absent(flags: Mapping[str, FlagValue]) -> dict[str, FlagValue]:
    """把"显式缺席"（值为 `False`）的键去掉 —— 比对时它与"对面根本没这个键"等价。"""
    return {flag: value for flag, value in flags.items() if value is not False}


# --------------------------------------------------------------------------
# 冻结签名的实现
# --------------------------------------------------------------------------


def build_flag_layers(run: Run, ctx: PlanContext) -> FlagLayers:
    """把这个 run 的五层 flag 各自装好（还没合并）。

    * `builtin` = `BUILTIN_DEFAULT_FLAGS`
    * `defaults` = `ctx.defaults`（学自官方 run 目录）**+ 从 `-R` 的 `cpu=` 推出来的 `--parallel`**
      **+ 来自 `SiteFacts.key` 的 `--key`**
    * `extra` = `ctx.extra_flags` + `ctx.design.extra_flags`（per-design 的后写，赢批次级）
    * `axis` = 该 run 每根轴 `resolve_axis_flags` 的结果
    * `locked` = 工具自己算的（`--workDir` / `--gds` / `--top` / `--sparam` / `--all` /
      `--includePortOrder=1` / `--cadencePins=1` / `--nogui` / `-m`）

    ⚠️ **`--parallel` 与 dsub 的 `cpu=` 联动**（BRIEF §6）：`-R "cpu=20;…"` 给了 cpu，
    就按 `BatchOptions.parallel_multiplier`（当前 1:1）算出 `--parallel` 写进 `defaults` 层。
    放 `defaults` 而不是 `locked`，是因为 §11 把 `--parallel` 划在**界面层**（可以做轴），
    放 `locked` 会让轴改不动它。放 `builtin` 又太低 —— 学来的默认表里也有 `--parallel`，
    而"用户刚把 `-R` 改成 cpu=40"必须赢过"上次从官方学到的 20"。
    kit 里 ALPS 那条 `-mt` **必须等于** `cpu` 的硬规则**不适用于 eWave**，照抄会损失算力。

    ⚠️ **`--key` 同理**（见 `KEY_FLAG`）：它是生产默认值的一员，但取值是站点身份 ——
    「学默认表」那一步把它剔掉了、`BUILTIN_DEFAULT_FLAGS` 又不许写死它 ⇒ 不在这里补
    就谁都不给，端到端拼出来的命令会缺 `--key`。`ctx.defaults` 里已经有（spec 显式给了）
    就不动它；`facts.key` 为空则**什么都不加** —— 宁可缺，也不许编一个假 key。

    不写盘。
    """
    builtin: FlagDict = dict(BUILTIN_DEFAULT_FLAGS)

    defaults: FlagDict = dict(ctx.defaults)
    resources = ctx.design.resources or ctx.facts.dsub_resources
    parallel = _parallel_from_resources(resources, ctx.options.parallel_multiplier)
    if parallel is not None:
        defaults["--parallel"] = parallel
    if ctx.facts.key and KEY_FLAG not in defaults:
        defaults[KEY_FLAG] = ctx.facts.key

    extra: FlagDict = dict(ctx.extra_flags)
    extra.update(ctx.design.extra_flags)

    axis: FlagDict = {}
    by_name = {a.name: a for a in ctx.axes}
    for name, value in run.axis_values.items():
        found = by_name.get(name)
        if found is None:
            # 轴定义没了而 run 还带着它的取值 —— 这个 run 的身份就说不清了。
            # 静默忽略等于"目录名说 fw-on、命令行里没有 --fullWave"，正是要消灭的坑。
            raise SpecError(
                f"run {run.run_id!r} carries axis {name!r}={value!r}, but PlanContext.axes has no such axis - "
                "axis definitions and the run disagree, so the directory name and the real command tell two stories"
            )
        axis.update(resolve_axis_flags(found, value, ctx.facts))

    locked: FlagDict = _locked_flags(run, ctx)
    return FlagLayers(builtin=builtin, defaults=defaults, extra=extra, axis=axis, locked=locked)


def _parallel_from_resources(resources: str, multiplier: float) -> str | None:
    """`-R` 里的 `cpu=` × 倍率 → `--parallel` 的值。给不出就返回 None（**别瞎猜**）。"""
    cpu = parse_resource_string(resources).get("cpu", "")
    if not cpu.isdigit():
        return None
    return str(max(1, int(round(int(cpu) * multiplier))))


def _locked_flags(run: Run, ctx: PlanContext) -> FlagDict:
    """机制层 —— 工具按这个 run 自己算出来的那几个（§11「锁死」层）。

    改了它们工具自身机制就失效：`--workDir` 是我们绕开"同 corner/temp 静默覆盖"的**全部**
    手段（D2），`--all` 是端口映射不依赖 GUI 的**全部**依据（D1b）。

    `--top` 和 `--sparam` 都取 `Design.cell`：`--top` 是 GDS 里的顶层 cell（阶段 1 就是按
    这个 cell 导出的），`--sparam` 是产物基名 —— 官方两处也都是 cell 名。
    """
    design = ctx.design
    flags: FlagDict = {
        "--nogui": True,
        "-m": True,
        "--workDir": run.work_dir,
        "--gds": _gds_path(run, ctx),
        "--top": design.cell,
        "--sparam": design.cell,
        "--cadencePins": "1",
    }
    port_spec = design.port_spec or PortSpec()
    if port_spec.mode is PortMode.ALL:
        flags["--all"] = True
    if ctx.options.include_port_order:
        flags["--includePortOrder"] = "1"
    return flags


def _gds_path(run: Run, ctx: PlanContext) -> str:
    """阶段 1 产物的位置。`Design.gds_path` 给了就用它（用户自带 GDS ⇒ 跳过阶段 1），
    否则按 BRIEF §5 归档布局落在 `<batch_dir>/gds/<design>.gds`（D1a：整个矩阵共用一份）。

    `core.layout.compute_run_paths` 会算同一个位置（`RunPaths.design_gds`）——
    那边是权威，driver 拿到之后应当写回 `Design.gds_path`；这里的拼法是它还没写回时的兜底。
    """
    if ctx.design.gds_path:
        return ctx.design.gds_path
    name = f"{run.design_key}.gds"
    if ctx.batch_dir:
        return posixpath.join(ctx.batch_dir, GDS_DIRNAME, name)
    return posixpath.join(GDS_DIRNAME, name)


def resolve_axis_flags(axis: Axis, value: str, facts: SiteFacts) -> FlagDict:
    """算某根轴取某个值时贡献的 flag，并把占位符换掉。

    只认两个占位符：`PLACEHOLDER_VALUE` → 取值本身；`PLACEHOLDER_PTXT` →
    `core.discover.ptxt_path_for_corner(facts, value)`。

    ⚠️ corner 轴会同时吐 `--corner=` **和** `--emssTechFile=` 两个 flag（§7「corner 轴要同时改两处」）——
    少改一个就是"目录名说 typical、实际用了别的工艺角"，而且跑得出来、数字也像。
    `value` 不在 `axis.values` 里 → `SpecError`。
    """
    for axis_value in axis.values:
        if axis_value.value == value:
            break
    else:
        legal = ", ".join(repr(v.value) for v in axis.values)
        raise SpecError(f"axis {axis.name!r} has no value {value!r} (legal values: {legal})")

    resolved: FlagDict = {}
    for flag, raw in axis_value.flags.items():
        if isinstance(raw, str):
            if PLACEHOLDER_VALUE in raw:
                raw = raw.replace(PLACEHOLDER_VALUE, value)
            if PLACEHOLDER_PTXT in raw:
                raw = raw.replace(PLACEHOLDER_PTXT, _ptxt_path_for_corner(facts, value))
        resolved[flag] = raw
    return resolved


def merge_flag_layers(layers: FlagLayers) -> FlagDict:
    """按 `FlagLayers.MERGE_ORDER` 合并成一份 flag dict。

    * 后层覆盖前层。
    * 值为 `False` 的键表示**显式缺席**：合并后仍留在 dict 里（`False`），
      由 `render_flags` 负责不渲染 —— 这样 `diff_flags` 能看见"我们是有意不给这个 flag"。
    不写盘。
    """
    merged: FlagDict = {}
    for layer in FlagLayers.MERGE_ORDER:
        # FlagLayer 的 value 就是 FlagLayers 上对应字段的名字（builtin/defaults/extra/axis/locked）。
        chunk = getattr(layers, layer.value, None)
        if chunk is None:  # pragma: no cover - 冻结面改了字段名才会到这
            raise SpecError(
                f"FlagLayers has no {layer.value!r} layer - the frozen interface and the implementation disagree"
            )
        merged.update(chunk)
    return merged


def detect_flag_conflicts(layers: FlagLayers, axes: Sequence[Axis]) -> list[FlagConflict]:
    """查非法冲突。返回空 list = 干净。**自己不抛异常**，抛不抛由调用方定。

    规则（BRIEF §11 规则 2）：
    1. `extra` 里出现 `USER_FORBIDDEN_FLAGS` 里的 flag → fatal。
    2. `extra` 里出现任何一根**轴掌管**的 flag（`Axis.flags`）→ fatal。
       否则目录名会和实际跑的值对不上 —— 那正是原生 GUI 覆盖坑的根因，不能自己再造一遍。
    3. `axis` 与 `locked` 撞车（除 `--emssTechFile`）→ fatal，说明轴定义写错了。
    4. `defaults` 覆盖了 `builtin` → 不是冲突，正常。

    额外加了一条**非 fatal** 的（不在原 4 条规则里，见交接报告的「设计偏离」）：两根轴掌管
    同一个 flag 时，后展开的那根会盖掉前一根 —— 合并结果虽然确定，但人多半没想到，
    而且两根轴都会进 slug（目录名说两件事，命令行只做一件）→ 报一条 warning。
    """
    conflicts: list[FlagConflict] = []
    owner: dict[str, str] = {}
    for axis in axes:
        for flag in axis.flags:
            if flag in owner and owner[flag] != axis.name:
                conflicts.append(
                    FlagConflict(
                        flag=flag,
                        reason=(
                            f"axis {owner[flag]!r} and axis {axis.name!r} both own {flag} - "
                            "the one expanded later overwrites the earlier one, while the dir name records both"
                        ),
                        layer=FlagLayer.AXIS,
                        axis_name=axis.name,
                        fatal=False,
                    )
                )
            owner.setdefault(flag, axis.name)

    for flag, value in layers.extra.items():
        if flag in USER_FORBIDDEN_FLAGS:
            conflicts.append(
                FlagConflict(
                    flag=flag,
                    reason=(
                        f"{flag} is computed per run by the tool itself (locked layer); "
                        "do not give it again in Extra flags - overriding it disables the tool's own mechanism"
                    ),
                    layer=FlagLayer.EXTRA,
                    value=value,
                    fatal=True,
                )
            )
            continue
        if flag in owner:
            conflicts.append(
                FlagConflict(
                    flag=flag,
                    reason=(
                        f"{flag} is already owned by axis {owner[flag]!r}; do not give it again in Extra flags - "
                        "otherwise the directory name disagrees with the value actually used "
                        "(that is the root cause of the native GUI overwrite trap)"
                    ),
                    layer=FlagLayer.EXTRA,
                    axis_name=owner[flag],
                    value=value,
                    fatal=True,
                )
            )

    for flag, value in layers.axis.items():
        if flag == "--emssTechFile":
            # corner 轴**必须**能改它（§7「corner 轴要同时改两处」），这是唯一的例外。
            continue
        if flag in layers.locked:
            conflicts.append(
                FlagConflict(
                    flag=flag,
                    reason=(
                        f"{flag} is computed by the mechanism layer, yet an axis writes it too - "
                        "the axis definition is wrong: the mechanism layer has the last word, "
                        "so the axis value is silently dropped"
                    ),
                    layer=FlagLayer.AXIS,
                    axis_name=owner.get(flag, ""),
                    value=value,
                    fatal=True,
                )
            )
    return conflicts


def render_flags(flags: FlagDict) -> list[str]:
    """flag dict → argv 片段，顺序**确定**（同样的输入永远同样的输出，cmd.sh 才可比对）。

    渲染规则：
    * `True` → `["--nogui"]` / `["-m"]`
    * `False` → 什么都不产生（显式缺席）
    * `str` 且长 flag → `["--corner=typical"]`（一项，用 `LONG_FLAG_ASSIGN`）
    * `str` 且短 flag → `["-e", "0.4"]`（两项 —— 生产就是这么写的，别自作主张合并）

    端口不在这里渲染，见 `tools.ewave.render_ports`。

    顺序 = 按 flag 名 `sorted()`。ASCII 下 `--x` 排在 `-x` 前面，于是长 flag 一堆、短 flag
    一堆，人读 `cmd.sh` 时找得到。**别改成"按插入顺序"** —— dict 顺序会随合并的层数变化，
    两个 run 的 `cmd.sh` 就没法直接 diff 了。
    """
    argv: list[str] = []
    for flag in sorted(flags):
        value = flags[flag]
        if value is False:
            continue
        if value is True:
            argv.append(flag)
            continue
        text = str(value)
        if _is_long(flag):
            argv.append(f"{flag}{LONG_FLAG_ASSIGN}{text}")
        else:
            argv.extend([flag, text])
    return argv


def _run_log_name(run: Run) -> str:
    """这个 run 的 stdout 日志文件名 —— **每个 run 一份**，不是固定的 `run.log`。

    与 `core.layout` 的 `_per_run_name` 是同一条规矩、同一个理由（见 `model.CMD_SH_TEMPLATE`）：
    `<axes-slug>` 不含 corner/temperature ⇒ 同一个 `run_dir` 下有 N 个 run。
    这里刻意**不 import `core.layout`**（那会把 cmd 层绑到布局层），但两边必须给出同一个名字，
    所以 `tests/test_cmd_log_path.py` 有一条断言把它们钉在一起 —— 各写各的才是真危险。
    """
    stem = run.ewave_dir or run.run_id.rsplit("/", 1)[-1].strip()
    if not stem:
        return RUN_LOG_NAME
    return RUN_LOG_TEMPLATE.format(stem=stem)


def build_command_plan(run: Run, ctx: PlanContext) -> CommandPlan:
    """一个 run → 完整 `CommandPlan`（阶段 2）。四层合并 + 冲突检测 + argv 渲染都在这。

    冲突里有 fatal 的 → `FlagConflictError`。
    坐标缺失（没有 `facts.ewave_bin` 之类）→ `DiscoveryError`。
    **不写盘、不建目录**（dry-run 和真跑走的是同一条路，区别只在后面提不提交）。
    """
    if not ctx.facts.ewave_bin:
        raise DiscoveryError(
            "SiteFacts.ewave_bin is empty - no idea which ewave to execute.\n"
            "  Next: run core.discover.discover_site_facts(<official run dir>), "
            "or make sure `ewave` is on PATH (hard constraint 1b: absolute tool paths never go into the source)"
        )
    if not run.work_dir:
        raise SpecError(
            f"run {run.run_id!r} has no work_dir - `--workDir` would be empty, so eWave writes its outputs "
            "into the current directory and combinations sharing one corner/temp silently overwrite each other. "
            "That is exactly the trap this tool exists to kill (BRIEF sec. 5)"
        )

    layers = build_flag_layers(run, ctx)
    conflicts = detect_flag_conflicts(layers, ctx.axes)
    fatal = [c for c in conflicts if c.fatal]
    if fatal:
        raise FlagConflictError(
            "flag conflicts, refusing to build the command:\n"
            + "\n".join(f"  {c.flag}: {c.reason}" for c in fatal)
        )

    flags = merge_flag_layers(layers)
    port_spec = ctx.design.port_spec or PortSpec()
    argv = [ctx.facts.ewave_bin, *render_flags(flags), *_render_ports(port_spec)]
    return CommandPlan(
        argv=tuple(argv),
        cwd=run.work_dir,
        work_dir=run.work_dir,
        stage=Stage.SOLVE,
        run_id=run.run_id,
        design_key=run.design_key,
        flags=flags,
        port_spec=port_spec,
        # ⚠️ **不是**固定的 `run.log`。`<axes-slug>` 按定义不含 corner/temperature，
        # 所以同一个 `work_dir` 底下住着 N 个 run；写死固定名 ⇒ N 条命令的 log_path 指向
        # 同一个文件，而 `sched.donau` 拿它当 `dsub -o` ⇒ N 个 job 的 stdout 混进一份日志。
        # 症状不是崩溃，是日志「看起来有」—— 出事时你翻开它，里面是几个 job 交织的输出。
        # 与 `model.RunPaths.run_log` / `CMD_SH_TEMPLATE` 同一条规矩（2026-08-18 已为 cmd.sh
        # 修过一次，这里是同一个坑的第二处）。词根取 `<corner>_<temp>`，预测不出来时退回
        # run_id 的最后一段（`expand_runs` 保证它在同一个 run_dir 下唯一）。
        log_path=posixpath.join(run.work_dir, _run_log_name(run)),
    )


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
    * 值为 `False` 的键视为"该 flag 不存在"，与对面真的没有这个键**算一致**
      （两边都没有 ⇒ 一条差异都不报，也不进 `compared_count`）。
    * 比较值时按字符串比（`"0.4"` != `"0.40"` —— 报出来让人看，别自作主张归一）。
    不写盘。
    """
    skip = set(ignore)
    left = _drop_absent(actual)
    right = _drop_absent(expected)

    ignored = tuple(sorted(flag for flag in set(left) | set(right) if flag in skip))
    keys = sorted((set(left) | set(right)) - skip)

    only_actual: list[str] = []
    only_expected: list[str] = []
    differing: list[FlagDelta] = []
    same: list[str] = []
    for flag in keys:
        if flag not in right:
            only_actual.append(flag)
        elif flag not in left:
            only_expected.append(flag)
        elif _same_value(left[flag], right[flag]):
            same.append(flag)
        else:
            differing.append(FlagDelta(flag=flag, actual=left[flag], expected=right[flag]))

    return FlagDiff(
        only_actual=tuple(only_actual),
        only_expected=tuple(only_expected),
        differing=tuple(differing),
        same=tuple(same),
        ignored=ignored,
        compared_count=len(keys),
    )


def _same_value(actual: FlagValue, expected: FlagValue) -> bool:
    """两个 flag 取值算不算一致。

    `True`（裸 flag）和 `"1"` 是**不同**的两件事，别归一 —— `--cadencePins=1` 与
    `--cadencePins` 在 eWave 眼里是不是一回事我们没验过，没验过就不替它决定。
    """
    if isinstance(actual, bool) or isinstance(expected, bool):
        return actual is expected
    return str(actual) == str(expected)


def diff_ports(actual: PortSpec, expected: PortSpec) -> PortDiff:
    """比端口顺序（不是集合 —— **顺序就是映射**，见 BRIEF §5）。

    `first_mismatch_index` 指出第一处错位。pin 集合一变所有编号平移，而且静默错位，
    对批量对比尤其致命 —— 所以这里报告的是位置，不只是差集。

    * 逐位比 `(port_id, pin)` 整对：只改 pin 名、只改编号、整体平移，三种都抓得到。
    * `only_actual` / `only_expected` 是 **pin 名**的差集（"多了哪个 / 少了哪个"）。
      和 `first_mismatch_index` 一起看才分得清"改了名"还是"整体平移"。
    * `compared_count` = 逐位看过的位置数 = 两边长度的**较大值**（短的那边算缺位，
      不是"没比"）。这条是防"空得非常好看"的计数锚。

    `ALL` 模式的 `PortSpec` 没有 mapping（端口要等 eWave 读了 GDS 才知道）——
    想在拼命令阶段就比，先用 `predict_all_ports(pins)` 把它变成 EXPLICIT。
    """
    left = actual.mapping
    right = expected.mapping
    compared = max(len(left), len(right))

    first_mismatch: int | None = None
    for index in range(compared):
        pair_left = left[index] if index < len(left) else None
        pair_right = right[index] if index < len(right) else None
        if pair_left != pair_right:
            first_mismatch = index
            break

    pins_left = [pin for _, pin in left]
    pins_right = [pin for _, pin in right]

    return PortDiff(
        matched=first_mismatch is None and len(left) == len(right),
        first_mismatch_index=first_mismatch,
        only_actual=tuple(sorted(set(pins_left) - set(pins_right))),
        only_expected=tuple(sorted(set(pins_right) - set(pins_left))),
        compared_count=compared,
    )


def parse_resource_string(resources: str) -> dict[str, str]:
    """`"cpu=20;mem=100000"` → `{"cpu": "20", "mem": "100000"}`。空串 → 空 dict。

    给 `--parallel` 定档用。⚠️ eWave 的 `--parallel` **不**必须等于 `cpu=`
    （那是 ALPS 的规矩，照抄会损失一半算力）——倍率是 `BatchOptions.parallel_multiplier`。

    容错：`;` 和 `,` 都当分隔符，空段跳过，两边去空白。没有 `=` 的段落记成值为空串
    （**不静默丢掉** —— 让它在 dry-run 的输出里显形，人一眼能看出 `-R` 写错了）。
    """
    parsed: dict[str, str] = {}
    for chunk in resources.replace(",", ";").split(";"):
        item = chunk.strip()
        if not item:
            continue
        key, sep, value = item.partition("=")
        parsed[key.strip()] = value.strip() if sep else ""
    return parsed
