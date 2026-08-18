"""`ewave_batch.tools.ewave` —— 阶段 2：拼 `ewave` argv。**薄封装到 `core.cmd`。**

薄是这个模块的设计目标，不是偷懒：真正的四层合并（内置默认 < 默认表 < Extra < 轴 < 机制）
在 `core.cmd`，那里有 golden 测试对着**真实生产命令**逐 flag 验。这里只做三件小事 ——
端口渲染、程序名取用、把两者接起来 —— 这样 driver（P3）只跟 `tools.*` 打交道，
而端口渲染和 flag 渲染**不会有第二份会漂移的实现**。

三个函数各自的"为什么"：

* `render_ports` —— 顺序就是映射本身。Touchstone 只按 `P00x` 编号排列、**名字被丢掉**
  （BRIEF §5「端口映射不在 .sNp 里，在命令行里」）。排序一乱，整份 `.sNp` 静默错位。
* `ewave_program` —— 工具绝对路径**不写进源码**（CLAUDE.md 硬约束 1b）。
  `command -v` 那一步在 `core.discover.find_tool`，结果落在 `SiteFacts.ewave_bin`。
* `build_ewave_plan` —— 转给 `core.cmd.build_command_plan`。

🚨 本文件里零站点标识符：端口名、cell 名、可执行文件路径全部**从入参来**，
一个默认值都没有。
"""

from __future__ import annotations

from ..model import (
    CommandPlan,
    PlanContext,
    PortSpec,
    Run,
    SiteFacts,
    ToolMissingError,
)


def render_ports(port_spec: PortSpec) -> list[str]:
    """端口部分的 argv：`--all`，或者 `-p P000=<pin> -p …  -i <pin> …`（**保序**）。

    顺序就是映射本身：Touchstone 只按 P00x 编号排列、名字被丢掉。排序一乱，
    整份 `.sNp` 就静默错位。

    ⚠️ **`PortMode.ALL` 在这里返回空 list，不返回 `["--all"]`** —— 与 model.py 里那句
    "`--all`，或者 `-p`…" 的字面读法不同，理由是它已经由 `core.cmd._locked_flags` 放进
    机制层的 flag dict 了（好让 `diff_flags` 能看见它、能被计数），再由 `render_flags`
    渲染出来。这里再给一次，argv 里就会出现**两个** `--all`。
    `tests/test_tools_ewave.py` 有一条计数断言盯着"整条命令里 `--all` 恰好出现一次"。

    真身在 `core.cmd._render_ports`，本函数是它的公开门面（`core.cmd` 的 docstring
    里写明了这个分工）：`build_command_plan` 要在 P1 就产出完整 argv，而本模块是 P3 的 ——
    真身放那边，这边才真的是薄的，也不会有两份会漂移的端口渲染。
    """
    from ..core import cmd as _cmd  # 惰性：tools 与 core 双向可见，import 时不结环

    return _cmd._render_ports(port_spec)


def ewave_program(facts: SiteFacts) -> str:
    """要执行的 ewave 可执行文件。`facts.ewave_bin` 为空 → `ToolMissingError`。
    **绝不在源码里写死绝对路径**（CLAUDE.md 硬约束 1b）。

    ⚠️ **这里不做 PATH 回退**（`command -v` / `shutil.which`），尽管"运行时发现"是本项目
    的既定路线。两条理由：

    1. 发现那一步有自己的家：`core.discover.find_tool`（冻结面里就是 `command -v` 的等价物），
       它的结果落进 `SiteFacts.ewave_bin`。在这里再来一遍就是第二份会漂移的发现逻辑。
    2. **本机与红区行为必须一致**。本机 PATH 上永远没有 `ewave`，红区上永远有 ——
       在这里回退，"facts 是空的会怎样"这条测试就会在两台机器上给出不同答案，
       而 `scripts/check.sh` 是要在红区也跑的。测不准的守卫等于没有守卫。
    """
    if not facts.ewave_bin:
        raise ToolMissingError(
            "SiteFacts.ewave_bin 是空的 —— 不知道该执行哪个 ewave。"
            "先跑 core.discover.discover_site_facts(<官方 run 目录>)，"
            "或确认 `ewave` 在 PATH 上（硬约束 1b：工具绝对路径不写进源码）"
        )
    return facts.ewave_bin


def build_ewave_plan(run: Run, ctx: PlanContext) -> CommandPlan:
    """阶段 2 的 `CommandPlan` —— 薄封装：转给 `core.cmd.build_command_plan`，
    再把端口 argv 接到尾巴上。存在的意义是让 driver 只跟 `tools.*` 打交道。

    ⚠️ 端口 argv **不在这里再接一次**：`build_command_plan` 内部已经调
    `core.cmd._render_ports` 接过了（P1 的实现选择 —— 红区 dry-run 是 P1 的交付判据，
    那时候本模块还不存在，所以完整 argv 必须在 `core.cmd` 里就成型）。
    在这里再接一遍会让每个 `-p` 出现两次，而 eWave 多半会很高兴地照单全收 ——
    `tests/test_tools_ewave.py` 用计数断言盯着这件事。

    先调一次 `ewave_program` 当守卫：坐标缺失时给 `ToolMissingError`（比
    `build_command_plan` 的 `DiscoveryError` 更贴切，也让 `ewave_program` 真的有人用，
    而不是一个没人调的门面）。
    """
    from ..core import cmd as _cmd  # 惰性：同上

    program = ewave_program(ctx.facts)
    plan = _cmd.build_command_plan(run, ctx)
    if plan.argv[:1] != (program,):  # pragma: no cover - 两边都读 facts.ewave_bin，对不上说明有人改了其中一边
        raise ToolMissingError(
            f"程序名对不上：ewave_program 给的是 {program!r}，"
            f"core.cmd 拼出来的是 {plan.argv[:1]!r} —— 两处必须同源（SiteFacts.ewave_bin）"
        )
    return plan
