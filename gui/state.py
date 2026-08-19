"""`gui.state` —— GUI ↔ driver 的桥（`model.GuiBridgeProtocol` 的唯一实现）。

三版 frame（stacked / tabbed / split）**只准通过这个对象跟核心说话**：换布局不碰逻辑，
逻辑改了三版一起跟上。所以本文件里没有一行 tkinter —— 它是纯逻辑，能脱离界面单测。

## 驱动方式：GUI 用 `after()` 驱动**同一个** `driver.tick()`

```
CLI ──►  sched.driver.run_batch(driver)      while + sleep
GUI ──►  root.after(interval, gui_state.tick)  ← 同一份 driver 代码（BRIEF §12）
```

**不另起线程、不复制一份调度逻辑。** 单线程轮询是 §12 拍板的实现决定：无锁 ⇒
状态机可推理、resume 天然。GUI 这一侧只做一件事：把 `tick()` 挂到事件循环上。

## 界面上的四层参数（BRIEF §11）落在哪

| 层 | 本文件里的落点 |
|---|---|
| 锁死 | 完全不出现 —— `core.cmd` 的机制层自己算（`locked_flags()` 只是给对话框显示用） |
| 界面（轴） | `set_axis_values()` / `axes()`：GUI 勾选 → `model.Axis` |
| 默认表 | `defaults_table()` / `set_default_override()`：值**从官方 run 目录学**，不写死 |
| 逃生口 | `set_extra_flags()` + `extra_flag_conflicts()`：撞轴 → 界面标红（§11 规则 2） |

🚨 本文件零站点标识符：library / cell / view / 路径 / 账号 / 队列全部来自用户输入或
`core.discover` 的运行时解析（CLAUDE.md 硬约束 1b）。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from ewave_batch.core import cmd as cmd_module
from ewave_batch.core import discover as discover_module
from ewave_batch.core import layout as layout_module
from ewave_batch.core import matrix as matrix_module
from ewave_batch.core import spec as spec_module
from ewave_batch.model import (
    USER_FORBIDDEN_FLAGS,
    Axis,
    AxisKind,
    AxisValue,
    BatchOptions,
    BatchSpec,
    BatchState,
    CommandPlan,
    Design,
    DriverEvent,
    DriverProtocol,
    EwaveBatchError,
    FlagDict,
    PlanContext,
    Run,
    RunnerProtocol,
    RunStatus,
    SchedulerProtocol,
    SiteFacts,
    SpecError,
    TickReport,
)
from ewave_batch.tools import ewave as ewave_tool

# --------------------------------------------------------------------------
# 界面默认值 —— **全是工具语义或占位符，一个站点坐标都没有**
# --------------------------------------------------------------------------

DEFAULT_BATCH_ROOT = "./ewave_batches"
"""批次落在哪的兜底值。相对路径 ⇒ 相对当前工作目录，**绝不会指进设计师的 spine**
（硬约束 4；真落盘前还有 `core.layout` 的 `_assert_outside_spine` 兜底）。"""

CORNER_VALUES: tuple[str, ...] = ("cbest", "cworst", "rcbest", "rcworst", "typical")
"""5 个工艺角的通用名字（BRIEF §10 用户给的轴清单）。**不是站点身份** ——
它们是 PDK 通用词汇，真正的站点坐标是 ptxt 的路径，那个由 `core.discover` 现场解析。"""

SWEEP_MODES: tuple[str, ...] = ("adaptive", "linear", "logarithmic", "discrete")
"""频率扫描的四种模式。前两种走 `--multiSweep=`，后两种各走自己的 flag。"""

SWEEP_FIELDS: dict[str, tuple[str, ...]] = {
    "adaptive": ("start", "stop", "step", "points"),
    "linear": ("start", "stop", "step", "points"),
    "logarithmic": ("stop", "points"),
    "discrete": ("start",),
}
"""哪个模式下哪几个格子有意义（其余置灰）。照 `mockups/_ui.py::_sync_freq_fields`。"""

STATUS_ORDER: tuple[str, ...] = tuple(status.value for status in RunStatus)
"""6 个状态的显示顺序 = `RunStatus` 的声明顺序：ready / pending / running / done /
failed / skipped（BRIEF §12，用户 2026-08-18 定的**恰好 6 个**）。"""

DEFAULT_AXIS_SELECTION: dict[str, tuple[str, ...]] = {
    "corner": ("typical",),
    "temperature": ("-40.0", "55.0", "125.0"),
    "fullWave": ("off", "on"),
    "equalCurrent": ("on",),
    "mesh": ("0.4",),
    "relativeTolerance": ("1e-05",),
    "relativeCurrentTolerance": ("0.001",),
}
"""界面打开时的初始勾选。数值取自 BRIEF §6「已知的生产默认值」——
那是 eWave 的工具语义（和 `core.cmd.BUILTIN_DEFAULT_FLAGS` 同源），不是站点身份。"""

DEFAULT_SWEEP: dict[str, str] = {
    "mode": "adaptive",
    "start": "0",
    "stop": "40",
    "step": "0.1",
    "points": "",
}
"""生产实际在用的那条扫频（`--multiSweep=adaptive,0:0.1:40`，BRIEF §10）。"""

SWEEP_AXIS_FLAGS: tuple[str, ...] = ("--multiSweep", "--logarithmicSweep", "--discreteFreq")
"""频率扫描这根轴掌管的三个 flag —— 互斥，选中的那个给值，另两个给 `False` 抵消。
（`False` = 显式缺席，见 `docs/INTERFACES.md` 契约 1。）"""

MESH_FLAGS: tuple[str, ...] = ("-e", "-d", "--viaMergeSpace")
"""网格密度轴掌管的三个 flag（BRIEF §10：**唯一改 mesh 的轴**）。"""

MESH_SEP = "/"
"""网格取值里三个 flag 的分隔符：`0.4` = 三个都 0.4；`0.4/0.5/0.4` = 分别给。"""

_TOGGLE_AXES: frozenset[str] = frozenset({"equalCurrent", "fullWave"})
"""开关轴 —— 取值只能是内置目录里的 `on` / `off`（`matrix.axis_with_values` 对它们
**拒绝而不是猜**：猜错就是"目录名说 off、命令行说 on"）。"""


# --------------------------------------------------------------------------
# 小工具（纯函数，测试直接用）
# --------------------------------------------------------------------------


def parse_value_list(text: str) -> list[str]:
    """`"-40, 55 125"` → `["-40", "55", "125"]`。逗号和空白都当分隔符。"""
    return [token for token in str(text).replace(",", " ").split() if token]


def normalize_temperature(value: str) -> str:
    """`"-40"` → `"-40.0"`。认不出的原样返回。

    ⚠️ 这一步是**承重的**，不是美化：eWave 拿 `--temperature` 的字面量去建
    `<corner>_<temp>` 那层目录，`-40` 建出 `-40`、`-40.0` 建出 `-40_0` ——
    两个不同的目录。用户在界面上敲 `-40`、官方脚本写 `-40.0`，不归一的话
    我们预测的产物目录就找不到（`verify_run_outputs` 会判 failed，而人完全看不出为什么）。
    """
    text = str(value).strip()
    try:
        number = float(text)
    except (TypeError, ValueError):
        return text
    return f"{number:.1f}" if number == round(number, 1) else text


def sweep_axis_value(mode: str, start: str, stop: str, step: str, points: str) -> str:
    """四个格子 → 扫频轴的取值字面量（就是 flag `=` 右边那一整串）。

    照 `mockups/_common.sweep_flag` 的形状；两个数值按 BRIEF §10 的实测更正过
    （生产是 `--multiSweep=adaptive,0:0.1:40`，设计稿把 step 当成了 start）。
    """
    if mode == "logarithmic":
        return str(stop).strip()
    if mode == "discrete":
        return str(start).strip()
    kind = "adaptive" if mode == "adaptive" else "lin"
    if str(points).strip():
        return f"{kind},{str(start).strip()}-{str(points).strip()}-{str(stop).strip()}"
    return f"{kind},{str(start).strip()}:{str(step).strip()}:{str(stop).strip()}"


def sweep_flag_name(mode: str) -> str:
    """扫描模式 → 它写哪个 flag。"""
    if mode == "logarithmic":
        return "--logarithmicSweep"
    if mode == "discrete":
        return "--discreteFreq"
    return "--multiSweep"


def parse_extra_flags(text: str) -> FlagDict:
    """`"--labelDepth=0 --printDouble"` → `{"--labelDepth": "0", "--printDouble": True}`。

    只做**词法**解析，不判合法性 —— 撞轴/撞机制层的判定在 `extra_flag_conflicts()`，
    那一步要拿到当前的轴清单才做得了。
    """
    flags: FlagDict = {}
    for token in str(text).split():
        name, sep, value = token.partition("=")
        if not name.startswith("-"):
            continue
        flags[name] = value if sep else True
    return flags


def wrap_command(argv: Sequence[str]) -> str:
    """argv → 「一行一个 flag」的可读文本（`Selected run → Command` 就显示它）。

    分行规则：以 `-` 开头的 token 起新行，其余 token 接在上一行后面 ——
    于是 `-e 0.4` / `-p P000=<pin>` 这种"短 flag + 值"两个 argv 项留在同一行，
    `--corner=typical` 这种自带 `=` 的长 flag 独占一行。

    ⚠️ 这是**显示用**的启发式：短 flag 的值如果本身以 `-` 开头（比如假想中的
    `-e -0.4`）会被拆到下一行。eWave 现有的短 flag（`-e` / `-d` / `-p` / `-i` / `-m`）
    没有这种取值，而且拆错了也只是显示难看 —— **不影响真正执行的 argv**（那是
    `CommandPlan.argv`，一个 tuple，从来不经过本函数）。
    """
    lines: list[str] = []
    for index, token in enumerate(argv):
        text = str(token)
        if index == 0 or not text.startswith("-"):
            if lines:
                lines[-1] = lines[-1] + " " + text
            else:
                lines.append(text)
        else:
            lines.append(text)
    return "\n".join(lines)


def _dedup(values: Sequence[str]) -> tuple[str, ...]:
    """保序去重。笛卡尔积里出现两个一样的取值 = 两个 run 抢同一个目录。"""
    seen: list[str] = []
    for value in values:
        text = str(value)
        if text not in seen:
            seen.append(text)
    return tuple(seen)


def mesh_axis_value(edge: str, vert: str, via: str) -> str:
    """三个格子 → 一个网格取值。三个相同就压成一个数（slug 短一截，也是官方的写法）。"""
    parts = [str(edge).strip(), str(vert).strip(), str(via).strip()]
    if parts[0] and parts[0] == parts[1] == parts[2]:
        return parts[0]
    return MESH_SEP.join(parts)


def mesh_flags_for(value: str) -> FlagDict:
    """网格取值 → 它贡献的三个 flag。

    `"0.4"` → 三个都 0.4；`"0.4/0.5/0.4"` → 按 `-e` / `-d` / `--viaMergeSpace` 分给。
    段数不是 1 也不是 3 → `SpecError`（**拒绝而不是猜**：猜错就是 mesh 悄悄变了，
    而 GDS 照样导得出、eWave 照样跑得完、数字还挺像，D1c 那个坑）。
    """
    parts = [chunk.strip() for chunk in str(value).split(MESH_SEP)]
    if len(parts) == 1:
        parts = parts * 3
    if len(parts) != 3 or not all(parts):
        raise SpecError(
            f"网格取值 {value!r} 认不出：要么写一个数（三个 flag 同值），"
            f"要么写三段 `边/纵向/via` —— 分别对应 {' '.join(MESH_FLAGS)}"
        )
    return dict(zip(MESH_FLAGS, parts))


# --------------------------------------------------------------------------
# 轴的构造 —— GUI 勾选 → model.Axis
# --------------------------------------------------------------------------


def axis_from_catalog(name: str, values: Sequence[str]) -> Axis:
    """内置轴 + 用户选的取值 → 一根可用的 `Axis`。

    开关轴（`equalCurrent` / `fullWave`）走**筛选**而不是 `axis_with_values`：
    on → `True`、off → `False` 的写法不一样，`matrix.axis_with_values` 对这种轴
    明确拒绝造新取值。取值不在目录里 → `SpecError`。
    """
    catalog = matrix_module.builtin_axis_catalog()
    axis = catalog.get(name)
    if axis is None:
        raise SpecError(f"内置轴目录里没有 {name!r}")
    wanted = _dedup(values)
    known = {av.value: av for av in axis.values}
    if name in _TOGGLE_AXES or all(value in known for value in wanted):
        missing = [value for value in wanted if value not in known]
        if missing:
            raise SpecError(
                f"轴 {name!r} 不认识取值 {', '.join(missing)} —— "
                f"它只有 {', '.join(sorted(known))}（开关轴的取值不许现造，会让"
                "目录名和命令行说两套话）"
            )
        return replace(axis, values=tuple(known[value] for value in wanted))
    return matrix_module.axis_with_values(axis, wanted)


def mesh_axis(values: Sequence[str]) -> Axis:
    """网格密度轴。一个取值同时改 `-e` / `-d` / `--viaMergeSpace`（GROUP 轴）。

    不走 `builtin_axis_catalog()` 是因为界面允许三个 flag 取不同的值
    （`0.4/0.5/0.4`），而内置目录里那根只支持"三个同值"。**flag 名一字不差地照抄**，
    所以两者对轴的语义没有分歧，分歧只在取值的表达能力上。
    """
    wanted = _dedup(values)
    return Axis(
        name="mesh",
        values=tuple(AxisValue(value, flags=mesh_flags_for(value)) for value in wanted),
        kind=AxisKind.GROUP,
        flags=MESH_FLAGS,
        short="mesh",
        description="网格密度，一个取值同时改三个 flag（官方 0.4，eWave 默认 0.5）",
    )


def sweep_axis(mode: str, values: Sequence[str]) -> Axis:
    """频率扫描轴。选中的 flag 给值，另外两个给 `False` **显式抵消**。

    为什么必须抵消：默认表里学来的多半带 `--multiSweep=adaptive,0:0.1:40`，
    用户在界面上切到 discrete 之后如果只是"多加一个 `--discreteFreq`"，
    命令行里就同时有两条扫频指令 —— eWave 会用哪一条不确定，而目录名只说了一件事。
    """
    flag = sweep_flag_name(mode)
    others = [name for name in SWEEP_AXIS_FLAGS if name != flag]
    wanted = _dedup(values)
    materialized = []
    for value in wanted:
        flags: FlagDict = {flag: value}
        for other in others:
            flags[other] = False
        materialized.append(AxisValue(value, flags=flags))
    return Axis(
        name="freq",
        values=tuple(materialized),
        kind=AxisKind.VALUE,
        flags=SWEEP_AXIS_FLAGS,
        short="freq",
        description=f"频率扫描（{mode}）—— 写 {flag}，另外两个用 False 抵消",
    )


# --------------------------------------------------------------------------
# 桥
# --------------------------------------------------------------------------


class GuiState:
    """GUI ↔ driver 的桥。实现 `model.GuiBridgeProtocol`（外加界面要用的编辑面）。

    冻结面只有 10 个方法（`load_spec` / `plan` / `start` / `tick` / `cancel` /
    `runs` / `designs` / `axes` / `command_text` / `summary`），那是**最小集**，
    不是上限：界面还要能改勾选、读冲突、看默认表 —— 那些方法在下面，名字都不与
    冻结面冲突。想把它们也冻起来是接口变更，走 `[interface-change]`（见交接报告的
    `interface_change_requests`）。

    构造参数没有被冻结（`docs/INTERFACES.md`「还没冻结的东西」同款待遇）：

    * `scheduler` / `runner` 可注入 —— 本机没有 `dsub` / `ewave`（硬约束 3），
      测试注入 `sched.fake` 的那一对，红区不注入就按 `BatchOptions.scheduler` 现造。
    * `discover` 可注入 —— 本机没有官方 run 目录，测试给一份手写的 `SiteFacts`。
      **默认走 `core.discover.discover_site_facts`**，坐标现场解析（硬约束 1b）。
    """

    # ---------------------------------------------------------------- 构造
    def __init__(
        self,
        *,
        batch_root: str = "",
        batch_name: str = "",
        official_run_dir: str = "",
        scheduler: SchedulerProtocol | None = None,
        runner: RunnerProtocol | None = None,
        on_event: Callable[[DriverEvent], None] | None = None,
        env: Mapping[str, str] | None = None,
        discover: Callable[[str], SiteFacts] | None = None,
        tool_version: str = "",
    ) -> None:
        self.batch_root = batch_root or DEFAULT_BATCH_ROOT
        self.batch_name = batch_name
        self.official_run_dir = official_run_dir
        self.submit_command = ""
        """整条 dsub 命令，**原样暴露给用户改**（用户 2026-08-18 要求）。
        空 = 还没发现站点的提交前缀（`plan()` 会从 `SiteFacts` 补一条）。"""

        self._scheduler_override = scheduler
        self._runner_override = runner
        self._on_event = on_event
        self._env = dict(env) if env is not None else None
        self._discover = discover
        self._tool_version = tool_version

        self._designs: list[Design] = []
        self._selection: dict[str, tuple[str, ...]] = dict(DEFAULT_AXIS_SELECTION)
        self._sweep: dict[str, str] = dict(DEFAULT_SWEEP)
        self._extra_text = ""
        self._default_overrides: FlagDict = {}
        self._options = BatchOptions()

        self._spec: BatchSpec | None = None
        self._state: BatchState | None = None
        self._contexts: dict[str, PlanContext] = {}
        self._plans: dict[str, CommandPlan] = {}
        self._plan_errors: dict[str, str] = {}
        self._learned: FlagDict = {}
        self._facts: dict[str, SiteFacts] = {}
        self._notes: list[str] = []
        self._events: list[DriverEvent] = []
        self._driver: DriverProtocol | None = None
        self._running = False
        self._dirty = True

    # ============================================================ 冻结面
    def load_spec(self, path: str) -> None:
        """读 spec 并建 `BatchState`（还不建目录、不提交）。失败抛 `SpecError`。

        spec 里的 designs / axes / defaults / extra_flags / options 会**覆盖**界面上
        当前的勾选 —— 用户按了 Load，界面就该显示文件里的东西，而不是两者的混合。
        """
        spec = spec_module.load_spec(path)
        self._spec = spec
        self._designs = list(spec.designs)
        self._selection = self._selection_from_axes(spec.axes)
        self._sweep = self._sweep_from_axes(spec.axes)
        self._extra_text = " ".join(_render_flag_token(k, v) for k, v in spec.extra_flags.items())
        self._default_overrides = dict(spec.defaults)
        self._options = spec.options
        if spec.batch_name:
            self.batch_name = spec.batch_name
        if spec.batch_root:
            self.batch_root = spec.batch_root
        self._invalidate()

    def plan(self) -> None:
        """展开矩阵 + 拼命令，把结果放进 state。dry-run 界面靠它。

        坐标是 per-design 解析的（`PlanContext` 存在的理由）；某个 design 的官方目录
        解析不了 ⇒ 记一条 note、这个 design 的命令拼不出来，**但界面照常显示矩阵** ——
        本机（没有官方目录）也要能看见"这批会跑哪些 run"。
        """
        spec = self._spec_snapshot()
        if not spec.designs:
            # 一个 design 都没勾 —— 空矩阵是**界面的正常起始状态**，不是错误。
            # `spec_to_batch` 会在这里抛 SpecError（对 CLI 是对的：命令行给了空 spec
            # 就该当场骂人），但界面刚打开时本来就什么都没有，抛异常等于开局就弹框。
            self._state = BatchState(
                batch_name=self.batch_name,
                batch_dir=self.batch_dir(),
                options=self._options,
            )
            self._contexts = {}
            self._plans = {}
            self._plan_errors = {}
            self._dirty = False
            return
        state = spec_module.spec_to_batch(
            spec, batch_root=self.batch_root, tool_version=self._tool_version
        )
        self.batch_name = state.batch_name

        contexts: dict[str, PlanContext] = {}
        for design in state.designs:
            key = matrix_module.design_key(design)
            facts = self._facts_for(design)
            defaults = dict(discover_module.learn_default_flags(facts))
            if not self._learned:
                # 「学到的默认表」记第一份非空的那个，给 Extraction defaults 对话框显示。
                # 学不到（本机没有官方目录）时保持空 —— 空表和"学到了一张空表"在界面上
                # 是两回事，前者要显示成"还没学到，用的是内置兜底"。
                self._learned = dict(defaults)
            defaults.update(self._default_overrides)
            contexts[key] = PlanContext(
                design=design,
                facts=facts,
                axes=tuple(state.axes),
                defaults=defaults,
                extra_flags=dict(state.extra_flags),
                options=state.options,
                batch_dir=state.batch_dir,
            )

        self._state = state
        self._contexts = contexts
        self._plans = {}
        self._plan_errors = {}
        for run in state.runs:
            design = next(
                (d for d in state.designs if matrix_module.design_key(d) == run.design_key), None
            )
            if design is None:  # pragma: no cover - expand_runs 保证对得上
                continue
            paths = layout_module.compute_run_paths(state.batch_dir, design, run)
            run.work_dir = paths.run_dir
            try:
                self._plans[run.run_id] = ewave_tool.build_ewave_plan(
                    run, contexts[run.design_key]
                )
            except EwaveBatchError as exc:
                self._plan_errors[run.run_id] = f"{exc.__class__.__name__}: {exc}"
        self._dirty = False

    def start(self, *, dry_run: bool = False) -> None:
        """开跑（或 dry-run）。已经在跑时是 no-op。"""
        if self._running:
            return
        self._options.dry_run = dry_run
        if self._dirty or self._state is None:
            self.plan()
        state = self._state
        assert state is not None  # plan() 之后必然有
        state.options.dry_run = dry_run
        from ewave_batch.sched.driver import make_driver  # 惰性：CLI 不跑批次时不用加载

        self._driver = make_driver(
            state,
            self._contexts,
            self._make_scheduler(),
            self._make_runner(),
            on_event=self._record_event,
        )
        self._running = True

    def resume(self, *, dry_run: bool = False) -> None:
        """断点续跑（D7）：**从 `batch.json` 恢复**，已经 done 的一个都不重跑。

        为什么不是"再按一次 start"：`start()` 会走 `plan()` 重建一份全新的 `BatchState`，
        里面每个 run 都是 `READY` —— 于是"续跑"变成"整批重跑"。一个 run 可能
        10 核 100 GB 跑 35 分钟，而两者在界面上看起来一模一样（都是表在动）。
        判据必须来自磁盘：`sched.driver.resume_batch` 会把 `done` 的产物重新验一遍
        （`batch.json` 说 done 而磁盘上没有产物，那是个假的 done）。

        还没跑过任何东西时退回 `start()` —— 没有 `batch.json` 可读。
        """
        if self._running:
            return
        if self._driver is None:
            self.start(dry_run=dry_run)
            return
        if self._state is None or not self._contexts:  # pragma: no cover - 有 driver 必有这两样
            self.plan()
        state = self._state
        assert state is not None
        from ewave_batch.sched.driver import resume_batch

        driver = resume_batch(
            state.batch_dir,
            self._contexts,
            self._make_scheduler(),
            self._make_runner(),
            on_event=self._record_event,
        )
        self._driver = driver
        self._state = driver.state
        self._plans = {}
        self._running = True

    def tick(self) -> TickReport | None:
        """驱动一拍，没在跑时返回 None。GUI 用 `after(poll_interval_ms, ...)` 调它。

        **这里就是"CLI 和 GUI 共用同一份 driver"的落点** —— 本方法除了转发和记一下
        "跑完了"，什么都不做。任何调度逻辑写在这儿都是第二份实现。
        """
        if self._driver is None or not self._running:
            return None
        report = self._driver.tick()
        if report.finished:
            self._running = False
        return report

    def cancel(self) -> None:
        """取消全部在飞的 job（driver 会把它们记成 failed —— `RunStatus` 没有 cancelled）。"""
        if self._driver is not None:
            self._driver.cancel()
        self._running = False

    def runs(self) -> tuple[Run, ...]:
        """当前矩阵里的全部 run（**同一批对象**，driver 就地改它们的 status）。"""
        return tuple(self._state.runs) if self._state is not None else ()

    def designs(self) -> tuple[Design, ...]:
        return tuple(self._designs)

    def axes(self) -> tuple[Axis, ...]:
        """当前生效的轴（GUI 勾选 → `model.Axis`）。取值一个都没有的轴不出现。"""
        return tuple(self._build_axes())

    def command_text(self, run_id: str) -> str:
        """选中 run 的完整 argv，一行一个 flag 的可读形式。

        拼不出来时返回**那条错误本身**而不是空串：界面上"命令是空的"和"缺 ewave 路径"
        看起来一样，而后者是本机的常态、前者是 bug。
        """
        plan = self._plans.get(run_id)
        if plan is not None:
            return wrap_command(plan.argv)
        return self._plan_errors.get(run_id, "")

    def summary(self) -> dict[str, int]:
        """`RunStatus.value` → 条数。**6 个键恒在**（界面不用 `.get` 兜底）。"""
        counts = {name: 0 for name in STATUS_ORDER}
        if self._driver is not None:
            counts.update(self._driver.summary())
            return counts
        for run in self.runs():
            counts[run.status.value] = counts.get(run.status.value, 0) + 1
        return counts

    # ======================================================= 界面的编辑面
    # 冻结面之外的部分：三版 frame 用它们改勾选、读冲突、看默认表。
    # 每一个都只碰内存里的选择，**不写盘、不提交**；改完置脏，下一次 plan() 才重算。

    def set_batch_name(self, name: str) -> None:
        self.batch_name = str(name).strip()
        self._invalidate()

    def set_batch_root(self, root: str) -> None:
        self.batch_root = str(root).strip() or DEFAULT_BATCH_ROOT
        self._invalidate()

    def set_official_run_dir(self, path: str) -> None:
        """批次级的官方 run 目录（design 没写自己的就用它）。坐标从这里现场解析。

        没变就**什么都不做**：清 `_facts` 会让下一次 `plan()` 重新解析官方目录，
        而界面是每敲一个键就 `recompute()` 一次的 —— 无条件清缓存 = 每个键一次磁盘解析。
        """
        cleaned = str(path).strip()
        if cleaned == self.official_run_dir:
            return
        self.official_run_dir = cleaned
        self._facts.clear()
        self._learned = {}
        self._invalidate()

    def set_designs(self, rows: Sequence[Sequence[str]]) -> None:
        """`[(library, cell, view), …]` → `Design` 列表。空字段 → `SpecError`。"""
        designs: list[Design] = []
        for row in rows:
            library, cell, view = (str(x).strip() for x in tuple(row)[:3])
            designs.append(
                Design(
                    library=library,
                    cell=cell,
                    view=view,
                    official_run_dir=self.official_run_dir,
                )
            )
        self._designs = designs
        self._invalidate()

    def add_design(self, library: str, cell: str, view: str) -> None:
        self._designs.append(
            Design(
                library=str(library).strip(),
                cell=str(cell).strip(),
                view=str(view).strip(),
                official_run_dir=self.official_run_dir,
            )
        )
        self._invalidate()

    def remove_design(self, index: int) -> None:
        if 0 <= index < len(self._designs):
            del self._designs[index]
            self._invalidate()

    def design_rows(self) -> tuple[tuple[str, str, str], ...]:
        """界面表格要显示的三元组。"""
        return tuple((d.library, d.cell, d.view) for d in self._designs)

    def axis_selection(self) -> dict[str, tuple[str, ...]]:
        """轴名 → 当前勾选的取值（拷贝，改它不影响状态）。"""
        return dict(self._selection)

    def set_axis_values(self, name: str, values: Sequence[str]) -> None:
        """改一根轴的取值。`temperature` 会顺手归一成带小数位的写法。"""
        cleaned = [str(v).strip() for v in values if str(v).strip()]
        if name == "temperature":
            cleaned = [normalize_temperature(v) for v in cleaned]
        self._selection[name] = _dedup(cleaned)
        self._invalidate()

    def sweep(self) -> dict[str, str]:
        return dict(self._sweep)

    def set_sweep(self, **fields: str) -> None:
        """改频率扫描的某几个格子（`mode` / `start` / `stop` / `step` / `points`）。"""
        for key, value in fields.items():
            if key in self._sweep:
                self._sweep[key] = str(value)
        self._invalidate()

    def sweep_live_fields(self) -> tuple[str, ...]:
        """当前模式下**有意义**的那几个格子（其余界面上置灰）。"""
        return SWEEP_FIELDS.get(self._sweep.get("mode", ""), ())

    def extra_flags_text(self) -> str:
        return self._extra_text

    def set_extra_flags(self, text: str) -> None:
        self._extra_text = str(text)
        self._invalidate()

    def extra_flags(self) -> FlagDict:
        return parse_extra_flags(self._extra_text)

    def extra_flag_conflicts(self) -> list[str]:
        """Extra flags 里**该标红**的 flag 名（BRIEF §11 规则 2）。

        两类：①已经是某根轴在管的；②机制层锁死的（`USER_FORBIDDEN_FLAGS`）。
        返回空 list = 干净。**判定按 flag 名精确匹配，绝不前缀匹配** ——
        `--sparam` 不许吃掉 `--sparamImpedance`，那个坑 MVP 真踩过。
        """
        owned: set[str] = set(USER_FORBIDDEN_FLAGS)
        for axis in self._build_axes():
            owned.update(axis.flags)
        hits: list[str] = []
        for name in parse_extra_flags(self._extra_text):
            if name in owned and name not in hits:
                hits.append(name)
        return hits

    def conflict_message(self) -> str:
        """界面上那行红字。没有冲突 → 空串。"""
        hits = self.extra_flag_conflicts()
        if not hits:
            return ""
        return (
            f"{'  '.join(hits)} is already controlled by the tool or by an axis - "
            "writing it again in Extra flags makes the directory name disagree with "
            "the value actually used."
        )

    def set_submit_command(self, text: str) -> None:
        """整条 dsub 命令（用户可以逐字改）。`-R` 里的 `cpu=` 会同步到 `--parallel`。

        没变就什么都不做 —— 理由同 `set_official_run_dir`（每个键一次磁盘解析）。
        """
        if str(text) == self.submit_command:
            return
        self.submit_command = str(text)
        self._facts.clear()
        self._invalidate()

    def resources(self) -> str:
        """当前这条提交命令里的 `-R` 值。命令为空或没有 `-R` → 空串（**不瞎猜**）。"""
        if not self.submit_command.strip():
            return ""
        try:
            from ewave_batch.sched.donau import parse_dsub_prefix, resources_from_dsub_argv

            return resources_from_dsub_argv(parse_dsub_prefix(self.submit_command))
        except EwaveBatchError:
            return ""

    def submit_command_error(self) -> str:
        """这条 dsub 命令有什么毛病（能提交就返回空串）。界面把它显示成红字。"""
        if not self.submit_command.strip():
            return ""
        try:
            from ewave_batch.sched.donau import parse_dsub_prefix

            parse_dsub_prefix(self.submit_command)
        except EwaveBatchError as exc:
            return str(exc)
        return ""

    def parallel(self) -> int | None:
        """`--parallel` 会取几 —— 由 `-R` 的 `cpu=` × `parallel_multiplier` 推出来。

        **解析走 `core.cmd.parse_resource_string`，本文件不再实现第二份**
        （BRIEF §12：`--parallel` 与 `cpu=` 的同步只许有一份实现，两份必然漂）。
        推不出来返回 `None` —— 不许拿 1 或 0 冒充"没解析到"。
        """
        cpu = cmd_module.parse_resource_string(self.resources()).get("cpu", "")
        if not cpu.isdigit():
            return None
        return max(1, int(round(int(cpu) * self._options.parallel_multiplier)))

    def options(self) -> BatchOptions:
        """批次级开关（**同一个对象**，界面改它即时生效）。"""
        return self._options

    def defaults_table(self) -> list[tuple[str, str, str]]:
        """`Tools → Extraction defaults…` 那张表：(flag, 值, 值是哪来的)。

        §11 规则 1：**默认表的值不写死在源码** —— 所以"哪来的"这一列是有信息量的，
        它区分「从官方 run 目录学的」和「源码里的兜底常量」。
        """
        rows: list[tuple[str, str, str]] = []
        merged: FlagDict = dict(cmd_module.BUILTIN_DEFAULT_FLAGS)
        merged.update(self._learned)
        merged.update(self._default_overrides)
        for name in sorted(merged):
            value = merged[name]
            if name in self._default_overrides:
                origin = "edited here (overrides the learned value)"
            elif name in self._learned:
                origin = "learned from the official run dir"
            else:
                origin = "builtin fallback (no official run dir parsed yet)"
            rows.append((name, _flag_value_text(value), origin))
        return rows

    def set_default_override(self, flag: str, value: str) -> None:
        """在对话框里改一个默认值（对整个批次生效）。空值 = 撤销覆盖。"""
        name = str(flag).strip()
        if not name:
            return
        if str(value).strip():
            self._default_overrides[name] = str(value).strip()
        else:
            self._default_overrides.pop(name, None)
        self._invalidate()

    def reset_defaults(self) -> None:
        """恢复成从官方目录学来的值（把全部人工覆盖丢掉）。"""
        self._default_overrides = {}
        self._invalidate()

    def locked_flags(self) -> tuple[str, ...]:
        """界面上根本不出现的那一层（改了工具自身机制就失效）。只用于显示。"""
        return tuple(sorted(USER_FORBIDDEN_FLAGS))

    # -------------------------------------------------------------- 只读视图
    def run_count(self) -> int:
        """当前勾选会展开出多少个 run。**不落盘、不建目录** —— 边勾边看的那个数。"""
        try:
            return len(matrix_module.expand_runs(self._designs, self._build_axes()))
        except EwaveBatchError:
            return 0

    def formula(self) -> str:
        """`2 designs x 1 corner x 3 temp x 2 mode = 12 runs` 那一行。"""
        selection = self.axis_selection()
        parts = [f"{len(self._designs)} designs"]
        for name, label in (
            ("corner", "corner"),
            ("temperature", "temp"),
            ("fullWave", "mode"),
        ):
            parts.append(f"{len(selection.get(name, ()))} {label}")
        return " x ".join(parts) + f" = {self.run_count()} runs"

    def axis_counts(self) -> dict[str, int]:
        """轴名 → 取值个数（界面上每一行右边那个 `-> N`）。"""
        counts = {name: len(values) for name, values in self._selection.items()}
        counts["freq"] = 1
        counts["design"] = len(self._designs)
        return counts

    def batch_dir(self) -> str:
        """批次落在哪。还没 plan 过就用当前的 root + name 现拼一个预览。"""
        if self._state is not None:
            return self._state.batch_dir
        name = self.batch_name or "<batch>"
        return os.path.join(os.path.expanduser(self.batch_root), name).replace("\\", "/")

    def out_dir(self, run_id: str) -> str:
        """某个 run 的落地目录（`--workDir` 里面 eWave 自己建的那层）。"""
        plan = self._plans.get(run_id)
        run = self.run(run_id)
        if run is None:
            return ""
        base = plan.work_dir if plan is not None else run.work_dir
        return f"{base}/{run.ewave_dir}/" if base and run.ewave_dir else base

    def run(self, run_id: str) -> Run | None:
        for item in self.runs():
            if item.run_id == run_id:
                return item
        return None

    def plan_for(self, run_id: str) -> CommandPlan | None:
        return self._plans.get(run_id)

    def notes(self) -> tuple[str, ...]:
        """规划过程中的软失败（解析不了某个官方目录之类）。硬失败直接抛。"""
        return tuple(self._notes)

    def events(self) -> tuple[DriverEvent, ...]:
        """driver 播过的全部事件（状态栏显示最后一条）。"""
        return tuple(self._events)

    def is_running(self) -> bool:
        return self._running

    def has_started(self) -> bool:
        """这个批次已经交给 driver 跑过（或 dry-run 过）没有。

        ⚠️ 界面靠它决定**还准不准重新展开矩阵**：跑过之后再 `plan()` 会造一份
        全新的、每个 run 都是 `READY` 的 `BatchState` —— 表上那 9 个 done 会当场消失，
        而这看起来只是"界面刷新了一下"。跑过之后要改设定，走 `reset()`（= New batch）。
        """
        return self._driver is not None

    def reset(self) -> None:
        """New batch：把上一次的 driver / 状态 / 事件丢掉，**保留界面上的勾选**。"""
        self._driver = None
        self._state = None
        self._contexts = {}
        self._plans = {}
        self._plan_errors = {}
        self._events = []
        self._running = False
        self._dirty = True

    def is_planned(self) -> bool:
        return self._state is not None and not self._dirty

    def status_line(self) -> str:
        """状态栏那一行英文。"""
        counts = self.summary()
        total = sum(counts.values())
        if not total:
            return "New batch - nothing configured"
        if self._driver is None:
            return f"Preview up to date - {total} runs ready to submit"
        if self._running:
            return (
                f"Running - {counts['done']} done, {counts['running']} running, "
                f"{counts['pending']} pending, {counts['failed']} failed"
            )
        return f"Finished - {counts['done']} / {total} done, {counts['failed']} failed"

    # ------------------------------------------------------------ 内部
    def _invalidate(self) -> None:
        """勾选变了 ⇒ 上一次 plan 的结果作废。**正在跑的批次不动**（改设定不该动它）。"""
        if not self._running:
            self._dirty = True

    def _record_event(self, event: DriverEvent) -> None:
        self._events.append(event)
        if self._on_event is not None:
            self._on_event(event)

    def _build_axes(self) -> list[Axis]:
        """当前勾选 → 轴清单。取值为空的轴直接不出现（笛卡尔积会塌成空集）。

        顺序是固定的：corner → temperature → 频率 → mesh → fullWave → equalCurrent →
        两个 tolerance。`expand_runs` 保证"第一根轴变得最慢"，于是 runs 表的行序稳定，
        `runs.csv` 才可以逐行 diff。
        """
        axes: list[Axis] = []
        for name in ("corner", "temperature"):
            values = self._selection.get(name, ())
            if values:
                axes.append(axis_from_catalog(name, values))
        axes.append(
            sweep_axis(
                self._sweep.get("mode", "adaptive"),
                (
                    sweep_axis_value(
                        self._sweep.get("mode", "adaptive"),
                        self._sweep.get("start", ""),
                        self._sweep.get("stop", ""),
                        self._sweep.get("step", ""),
                        self._sweep.get("points", ""),
                    ),
                ),
            )
        )
        mesh_values = self._selection.get("mesh", ())
        if mesh_values:
            axes.append(mesh_axis(mesh_values))
        for name in ("fullWave", "equalCurrent", "relativeTolerance", "relativeCurrentTolerance"):
            values = self._selection.get(name, ())
            if values:
                axes.append(axis_from_catalog(name, values))
        return axes

    def _spec_snapshot(self) -> BatchSpec:
        """当前界面状态 → 一份 `BatchSpec`（`plan()` 和「Save spec」共用这一条路）。"""
        return BatchSpec(
            batch_name=self.batch_name,
            batch_root=self.batch_root,
            designs=list(self._designs),
            axes=self._build_axes(),
            defaults=dict(self._default_overrides),
            extra_flags=parse_extra_flags(self._extra_text),
            options=self._options,
            source_path=self._spec.source_path if self._spec is not None else "",
            source_sha256=self._spec.source_sha256 if self._spec is not None else "",
        )

    def spec_snapshot(self) -> BatchSpec:
        """公开版本 —— 「Save」按钮把它写出去（写盘由调用方做）。"""
        return self._spec_snapshot()

    def _facts_for(self, design: Design) -> SiteFacts:
        """这个 design 的站点坐标。解析不了 → 空 `SiteFacts` + 一条 note。

        为什么不抛：本机（和公开克隆者）根本没有官方 run 目录，界面仍然应该能展开矩阵、
        显示会跑哪些 run。真正拼命令那一步会拿空 facts 抛 `DiscoveryError`，
        那条错误显示在 `Selected run → Command` 里 —— 位置准确、看得懂。
        """
        offdir = design.official_run_dir or self.official_run_dir
        cached = self._facts.get(offdir)
        if cached is not None:
            return cached
        facts = SiteFacts(official_run_dir=offdir)
        if offdir:
            try:
                facts = (
                    self._discover(offdir)
                    if self._discover is not None
                    else discover_module.discover_site_facts(offdir, env=self._env)
                )
            except EwaveBatchError as exc:
                self._note(f"{offdir}: {exc}")
        else:
            self._note(
                "No official run dir given - site coordinates (ports, ptxt, queue) are "
                "unknown, so commands cannot be assembled yet."
            )
        resources = self.resources()
        if resources:
            # 用户改过那条 dsub 命令 ⇒ 以命令里的 `-R` 为准（`--parallel` 跟着它走）。
            facts = replace(facts, dsub_resources=resources)
        elif facts.dsub_resources and not self.submit_command.strip():
            # 第一次解析到站点的提交前缀 —— 把整条命令灌进界面让用户改。
            self.submit_command = _dsub_command_from(facts)
        self._facts[offdir] = facts
        return facts

    def _note(self, text: str) -> None:
        if text not in self._notes:
            self._notes.append(text)

    def _make_scheduler(self) -> SchedulerProtocol:
        if self._scheduler_override is not None:
            return self._scheduler_override
        if self._options.scheduler == "fake":
            from ewave_batch.sched.fake import FakeScheduler

            return FakeScheduler()
        from ewave_batch.sched.donau import DonauScheduler, parse_dsub_prefix

        facts = next(iter(self._facts.values()), SiteFacts())
        prefix: Sequence[str] = ()
        if self.submit_command.strip():
            prefix = parse_dsub_prefix(self.submit_command)
        return DonauScheduler.from_site_facts(facts, submit_prefix=prefix)

    def _make_runner(self) -> RunnerProtocol:
        if self._runner_override is not None:
            return self._runner_override
        from ewave_batch.sched.driver import SubprocessRunner

        return SubprocessRunner()

    # ---- spec → 界面勾选 -------------------------------------------------
    def _selection_from_axes(self, axes: Sequence[Axis]) -> dict[str, tuple[str, ...]]:
        """spec 里的轴 → 界面勾选。spec 没写的轴保持界面上原来的值。"""
        selection = dict(self._selection)
        for axis in axes:
            if set(axis.flags) & set(SWEEP_AXIS_FLAGS):
                # 扫频轴由 `_sweep_from_axes` 拆成那四个格子。按 **flag** 认而不是按名字认：
                # 内置目录里这根轴叫 `multiSweep` / `discreteFreq`，我们自己造的叫 `freq`，
                # 三个名字一个语义 —— 认名字就会漏掉两个，然后它们变成一份谁也不看的死数据。
                continue
            selection[axis.name] = tuple(av.value for av in axis.values)
        return selection

    def _sweep_from_axes(self, axes: Sequence[Axis]) -> dict[str, str]:
        """spec 里的扫频轴 → 界面上那几个格子。认不出就原样保留界面的值。"""
        sweep = dict(self._sweep)
        for axis in axes:
            names = set(axis.flags)
            if not names & set(SWEEP_AXIS_FLAGS):
                continue
            value = axis.values[0].value if axis.values else ""
            if "--discreteFreq" in names and len(names) == 1:
                sweep["mode"], sweep["start"] = "discrete", value
            elif "--logarithmicSweep" in names and len(names) == 1:
                sweep["mode"], sweep["stop"] = "logarithmic", value
            else:
                sweep["mode"] = "adaptive" if value.startswith("adaptive") else "linear"
                body = value.partition(",")[2]
                if ":" in body:
                    sweep["start"], sweep["step"], sweep["stop"] = (body.split(":") + ["", ""])[:3]
                    sweep["points"] = ""
        return sweep


# --------------------------------------------------------------------------
# 私有小工具
# --------------------------------------------------------------------------


def _flag_value_text(value: object) -> str:
    """`True` → `"(bare flag)"`、`False` → `"(explicitly absent)"`、其余原样。

    `False` 不是"没有"，是"**显式缺席**"（`docs/INTERFACES.md` 契约 1）——
    在界面上把它显示成空串就等于把那条契约藏起来了。
    """
    if value is True:
        return "(bare flag)"
    if value is False:
        return "(explicitly absent)"
    return str(value)


def _render_flag_token(name: str, value: object) -> str:
    """一个 flag → 能贴回 `Extra ewave flags` 那一行的写法。"""
    if value is True:
        return name
    if value is False:
        return f"{name}=false"
    return f"{name}={value}"


def _dsub_command_from(facts: SiteFacts) -> str:
    """`SiteFacts` 的 dsub 三元组 → 一行能改的命令文本。坐标全部来自运行时解析。"""
    from ewave_batch.sched.donau import DonauScheduler, format_dsub_command

    return format_dsub_command(DonauScheduler.from_site_facts(facts).dsub_prefix())
