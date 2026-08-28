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
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from ewave_batch.core import cmd as cmd_module
from ewave_batch.core import discover as discover_module
from ewave_batch.core import layout as layout_module
from ewave_batch.core import logparse as logparse_module
from ewave_batch.core import matrix as matrix_module
from ewave_batch.core import spec as spec_module
from ewave_batch.model import (
    BASE_GROUP,
    BATCH_JSON_NAME,
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
    RunExpansion,
    RunGroup,
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

DEFAULT_BATCH_ROOT = layout_module.default_batch_root()
"""批次落在哪的兜底值 = `<install>/ewave_batches`。**与 cwd 无关的绝对路径。**

理由、以及这里踩过的**两个方向相反**的坑（`./ewave_batches` 会被部署吃掉、
`~/ewave_batches` 会撞红区 `$HOME` 配额且失败是静默的），逐条写在
`ewave_batch.core.layout.default_batch_root` 上 —— 那是这个值唯一的家。

界面这一侧只加一层：落点显示出来、可改，指进已知有害的地方时**标红**
（`batch_root_warning()`）。**绝不会指进设计师的 spine**（硬约束 4；真落盘前
还有 `core.layout` 的 `_assert_outside_spine` 兜底）。"""

SESSION_NAME = "session.local.json"
"""上次那份设定存在哪（装机目录下，与 `site.local.sh` 同一层）。

**`.local.` 是承重的**：里面装着 library / cell / view / 官方 run 目录 ——
全是站点标识符（CLAUDE.md 硬约束 1b）。`.gitignore` 挡着它，`deploy.sh` 的
`PRESERVE` 保着它（不保的话一次升级就把用户的设定搬进 `.deploy/backups/` 再轮转删掉，
和 2026-08-20 那次批次结果一模一样的形状）。

**固定 JSON，不跟着 PyYAML 走。** `save_spec` 会在没有 PyYAML 时把扩展名换成
`.json`，于是"存在哪"变成两个答案，而开机要去找的那个必须只有一个。
YAML 是给人写的（`Save spec as...` 那条路），这份是机器状态。
"""

CORNER_VALUES: tuple[str, ...] = ("cbest", "cworst", "rcbest", "rcworst", "typical")
"""5 个工艺角的通用名字（BRIEF §10 用户给的轴清单）。**不是站点身份** ——
它们是 PDK 通用词汇，真正的站点坐标是 ptxt 的路径，那个由 `core.discover` 现场解析。"""

MAX_PARALLEL_CAP = 64
"""界面允许的「同时在飞」上限。**不是工具的限制，是防手滑的**：一个 run 是
10 核 / 100 GB / 35 分钟（BRIEF §12），手滑多打一个 0 就是拿整个队列去换一次输入错误。
真要更高就改 spec 文件里的 `options.max_parallel` —— 那条路没有这道夹子。"""

GROUP_OVERRIDABLE_AXES: tuple[str, ...] = (
    "corner",
    "temperature",
    "mesh",
    "fullWave",
    "equalCurrent",
)
"""一个 run group **可以**自己定的轴。界面上就是 Settings 里带覆盖勾选框的那五行。

⚠️ 三根轴故意不在这里，**它们只属于 base**（口径与 `gui/_ui.py` 的 `GROUP_ROW_AXES`
逐字一致，`tests/test_gui_groups.py` 拿一条测试钉住这一点，防止两处漂）：

* `freq` —— 界面上它不是一个取值列表而是一整排格子（模式 + start/stop/step/points），
  「勾一下就覆盖」表达不了它；
* `relativeTolerance` / `relativeCurrentTolerance` —— 收敛容差是整批的性质而不是
  变体的轴。给组钉一份就会出现"用户改了 base 的容差，这个组**静默**留在老值上"。

★ 这个常量的第一个用户是 `duplicate_group`（2026-08-20 的 bug）：复制 base 时它原来
把 `_base_axes()` 的**每一根**轴都写成显式覆盖，于是复制出来的组带着一份钉死的
freq + 两个 tolerance —— 界面上看不见、点不到、撤不掉，而它们照样进笛卡尔积、
照样把 `freq` 变成"全批次在变的轴"从而改掉每一个 run 的目录名。
"""

SWEEP_MODES: tuple[str, ...] = ("adaptive", "linear", "logarithmic", "discrete")
"""频率扫描的四种模式。前两种走 `--multiSweep=`，后两种各走自己的 flag。"""

SWEEP_FIELDS: dict[str, tuple[str, ...]] = {
    "adaptive": ("start", "stop", "step", "points"),
    "linear": ("start", "stop", "step", "points"),
    "logarithmic": ("stop", "points"),
    "discrete": ("start",),
}
"""哪个模式下哪几个格子**可能**有意义。真正的可编辑集合走 `sweep_live_fields()` ——
adaptive / linear 还要再按 `spacing` 砍掉一个（见下）。"""

SWEEP_SPACINGS: tuple[str, ...] = ("step", "points")
"""adaptive / linear 下步长的两种给法，**互斥**。

用户 2026-08-20 指出的界面缺陷：`step` 和 `points` 两个格子同时可编辑、同时有值，
而 eWave 那条 flag 只有两种写法二选一 ——
`--multiSweep=adaptive,0:0.1:40`（步长）或 `--multiSweep=adaptive,0-41-40`（点数）。
两个都填的时候界面完全没说会用哪个（实际是 points 悄悄赢），
于是"界面显示的"和"真正跑的"能差一整条扫频而没人看得出来。
"""

STATUS_ORDER: tuple[str, ...] = tuple(status.value for status in RunStatus)
"""6 个状态的显示顺序 = `RunStatus` 的声明顺序：ready / pending / running / done /
failed / skipped（BRIEF §12，用户 2026-08-18 定的**恰好 6 个**）。"""

MASK_MIN_CHARS = 4
"""`redaction_map()` 收多长的串。比这短的一律不收 —— 见那个方法的第二条口径。"""

DEFAULT_AXIS_SELECTION: dict[str, tuple[str, ...]] = {
    "corner": ("typical",),
    "temperature": ("-40.0", "55.0", "125.0"),
    "fullWave": ("off", "on"),
    "equalCurrent": ("off",),
    "mesh": ("0.5",),
    "relativeTolerance": ("1e-05",),
    "relativeCurrentTolerance": ("0.001",),
}
"""界面打开时的初始勾选。数值是 eWave 的工具语义（和 `core.cmd.BUILTIN_DEFAULT_FLAGS`
同源），不是站点身份。

⚠️ `equalCurrent` 和 `mesh` 这两条是**用户 2026-08-20 当面订正的**，别照 BRIEF §6
那张「已知的生产默认值」表改回去：那张表记的是官方 run 目录里**恰好**在用的值
（`equalCurrent` 开、mesh 0.4），而这里要的是**界面打开时该给什么** ——
用户要的是 eWave 自己的默认（`equalCurrent` 关、mesh 0.5），不是某一次官方跑的取值。
两者不是一回事，混淆过一次了。"""

DEFAULT_SWEEP: dict[str, str] = {
    "mode": "adaptive",
    "spacing": "step",
    "start": "0",
    "stop": "40",
    "step": "0.1",
    "points": "",
}
"""生产实际在用的那条扫频（`--multiSweep=adaptive,0:0.1:40`，BRIEF §10）。

`spacing="step"` ⇒ `points` 那格从一开始就是空的且不可编辑，
界面上"看得见的"和命令行里"跑的"只有一条。"""

SWEEP_AXIS_FLAGS: tuple[str, ...] = ("--multiSweep", "--logarithmicSweep", "--discreteFreq")
"""频率扫描这根轴掌管的三个 flag —— 互斥，选中的那个给值，另两个给 `False` 抵消。
（`False` = 显式缺席，见 `docs/INTERFACES.md` 契约 1。）"""

MESH_FLAGS: tuple[str, ...] = ("-e", "-d", "--viaMergeSpace")
"""网格密度轴掌管的三个 flag（BRIEF §10：**唯一改 mesh 的轴**）。"""

_NL = chr(10)


def _has_batch_json(batch_dir: str) -> bool:
    """这个目录**已经是一个批次**了没有。

    判"被占了没有"只许有这一条判据，而且要和 `layout.list_batches`（历史列表）、
    `deploy.sh::looks_like_batch_data`（部署不许搬走的东西）逐字同义。
    三处不一致的后果是同一个：列表里看不见的批次会被当成空位占掉 = 覆盖。
    """
    try:
        return os.path.isfile(os.path.join(batch_dir, layout_module.BATCH_JSON_NAME))
    except (OSError, ValueError):  # pragma: no cover - 怪路径
        return False

"""换行符。`preflight()` 的多行文案用它拼 —— 那几条要在对话框里分行显示。"""

MESH_SEP = "/"
"""网格取值里三个 flag 的分隔符：`0.4` = 三个都 0.4；`0.4/0.5/0.4` = 分别给。"""

_TOGGLE_AXES: frozenset[str] = frozenset({"equalCurrent", "fullWave"})
"""开关轴 —— 取值只能是内置目录里的 `on` / `off`（`matrix.axis_with_values` 对它们
**拒绝而不是猜**：猜错就是"目录名说 off、命令行说 on"）。"""


SUBMIT_ACCOUNT_PLACEHOLDER = "ACCOUNT"
SUBMIT_QUEUE_PLACEHOLDER = "QUEUE"
"""`dsub -A` / `-q` 的占位符。**默认值不再用它们**（见 `DEFAULT_SUBMIT_COMMAND`），
但它们还活着，有两个真实用处：

1. `site.example.sh` 里仍然是占位符 —— 那份是给别的站点抄的模板；
2. 有人手工把它们打回输入框时，`submit_command_placeholders()` 照样拦得住。

⇒ 这套闸门是**在的**，只是默认路径不再触发它。别因为"默认值已经是真的了"就删掉。"""

SUBMIT_PLACEHOLDERS: tuple[str, ...] = (SUBMIT_ACCOUNT_PLACEHOLDER, SUBMIT_QUEUE_PLACEHOLDER)
"""还留在命令里就**不许真提交**，见 `GuiState.submit_command_placeholders()`。"""

DEFAULT_SUBMIT_ACCOUNT = "ug_rfic.momHClass"  # redzone-allow：用户 2026-08-28 明确批准公开
DEFAULT_SUBMIT_QUEUE = "bigmem"  # redzone-allow：用户 2026-08-28 明确批准公开
"""本站点的 Donau 账号与队列。**这两行是 CLAUDE.md 硬约束 1b 唯一的例外**，
由用户 2026-08-28 在部署实测之后明确要求：

> 「我觉得问题不大，就是服务器名字这些而已；我让你改默认值」

起因是同一天在别人机器上部署时实测的卡手：占位符默认值意味着**每台新机器都得
先配一次**才提交得了，而那一步没人猜得到该填什么。判断是用户的 —— 这是他自己
公司的调度账号，风险由他评估。

⚠️ **例外的边界就是这两行，不许外推。** library / cell / view / 端口名 / ptxt 路径 /
PDK 版本串 / workarea 路径 / 工号 / 主机名 **全部仍然禁止进源码**，理由一个字没变
（它们描述的是设计和版图，不是一台调度器的名字）。加新的站点值进来之前先问用户。

行尾那两个 `redzone-allow` 是给 `scripts/redzone_scan.sh` 看的逐行豁免 ——
少了它，这个仓库以后每一次提交都会被闸门拦下来。"""

DEFAULT_SUBMIT_COMMAND = (
    "dsub"
    f" -A {DEFAULT_SUBMIT_ACCOUNT}"
    f" -q {DEFAULT_SUBMIT_QUEUE}"
    " -R 'cpu=20;mem=100000'"
)
"""界面打开时 `Donau submit` 那一格里的东西 —— **开箱即可提交的一条真命令**。

历史（这段别删，它解释了为什么现在长这样）：

* 2026-08-20 之前：那一格**空着**。空输入框不告诉任何人它想要什么形状，
  于是「Donau 在哪里设置」这个问题在界面上无解。
* 2026-08-20 → 08-28：占位符模板 `-A ACCOUNT -q QUEUE`，换不掉就拒绝提交。
  形状对了，但**每台新机器仍然得先配一次**。
* 2026-08-28 起：账号/队列用真值（`DEFAULT_SUBMIT_ACCOUNT` / `DEFAULT_SUBMIT_QUEUE`，
  那里写着用户批准的原话）。判据从「一眼假」换成了「开箱能用」。

仍然成立的两件事：

1. **形状是真的**：flag 名与顺序跟 `sched.donau.build_dsub_argv` 同源
   （`-A` 账号 / `-q` 队列 / `-R` 资源），整条能过 `parse_dsub_prefix` ——
   它是可执行命令，不是示意图。`tests/test_gui_submit_default.py` 盯着这条同步。
2. **`-R` 那串仍然是例子，不是实测快照**：`docs/spec_example.yaml` 里那条。
   `sched.donau` 模块 docstring 第 3 条记着"kit 里那个 cpu= 核数是旧快照、
   红区实测已经变了" —— 资源串该由官方 run 目录顶掉，不该由这里定。
   ⇒ **账号/队列改成真值，不等于资源也该改成真值**，这两件事的性质不同：
   前者是"这台调度器叫什么"（不会变），后者是"这次要多少核"（每次都可能变）。

优先级没变，从弱到强：本默认值 < `site.local.sh` < 官方 run 目录解析出来的前缀
< 用户在框里手打的。账号跟这里不一样的人，仍然走 `site.local.sh` 覆盖掉它。
"""

SITE_LOCAL_NAME = "site.local.sh"
"""站点坐标的落点。**不进 git**（`.gitignore` 里有它），模板 `site.example.sh` 进。

这是 CLAUDE.md 硬约束 1b 给的两条路之一（另一条是运行时发现）：真实的 Donau 账号 /
队列是站点身份，不许写进源码 —— 但**也不该让用户每次开界面重打一遍**。
两者的交点就是这个文件：值留在跑它的那台机器上，仓库里只有形状。

沿用 `mvp/mvp_pack.sh` 已经在用的那个名字和 `KEY=value` 格式，一台机器只需要一份。
红区那边它在装机目录根下，`deploy.sh` 把它列进 `PRESERVE` ⇒ 每次升级都留着。"""

SESSION_ENV = "EWB_SESSION"
"""指定上次那份设定存哪的环境变量。见 `session_path`。"""

SITE_LOCAL_ENV = "EWB_SITE_LOCAL"
"""显式指一份 site.local.sh（给 doctor / 测试用）。给了就**只**认它，不再往下找。"""

SITE_SUBMIT_KEY = "EWB_SUBMIT_COMMAND"
"""`site.local.sh` 里那一行的键名：整条 dsub 命令，原样。

存整条命令而不是拆成 `ACCOUNT=` / `QUEUE=` 三个键 —— 界面上暴露给用户改的本来就是
整条命令（用户 2026-08-18），拆开就要在这里重新拼一遍，而拼法必须和
`sched.donau.build_dsub_argv` 逐字一致，那是第二份会漂的实现。"""


# --------------------------------------------------------------------------
# site.local.sh —— 站点坐标（纯函数，测试直接用）
# --------------------------------------------------------------------------


def shell_value(raw: str) -> str:
    """`site.local.sh` 里 `=` 右边那一坨 → 值。**不起 shell**（红区连 sh 都不该多起一个）。

    只认三种写法，够用且没有歧义：
    `'…'` / `"…"`（取引号内，后面的东西当注释丢掉）、裸值（切到第一个 ` #`）。
    引号内的 `#` 必须活下来 —— dsub 的资源串里没有 `#`，但路径里有的是。
    """
    text = str(raw).strip()
    if text[:1] in ("'", '"'):
        quote = text[0]
        end = text.find(quote, 1)
        return text[1:end] if end > 0 else text[1:]
    return text.split(" #", 1)[0].strip()


def parse_site_local(text: str) -> dict[str, str]:
    """`site.local.sh` 的文本 → `{KEY: value}`。认不出的行**跳过**，不抛。

    这个文件是用户手写的，而它的读者是一个 GUI 的构造函数 —— 里面多一个空行、
    多一句注释、多一个我们不认识的键，代价不该是「界面起不来」。
    真正会出错的是值本身（比如 dsub 命令写错），那一步有 `submit_command_error()`
    在界面上标红说清楚。
    """
    values: dict[str, str] = {}
    for line in str(text).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        key, sep, raw = stripped.partition("=")
        key = key.strip()
        if not sep or not key.isidentifier():
            continue
        values[key] = shell_value(raw)
    return values


def site_local_path(env: Mapping[str, str] | None = None) -> str:
    """这台机器上的 `site.local.sh` 在哪。没有 → 空串。

    找的顺序（先命中先用）：

    1. `$EWB_SITE_LOCAL` —— 显式指路。给了就只认它，找不到就是找不到，**不再往下猜**
       （猜下去的话，用户指错路径的症状会变成「用了另一台机器的坐标」）。
    2. **装机目录根**（本文件的上上级）—— 红区解出来的 `ewave_helper/` 就是它，
       `deploy.sh` 也把 `site.local.sh` 保在这一层。这是红区的正路。
    3. 当前工作目录 —— 开发机上从别处起 GUI 时的方便口子。
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    explicit = str(environ.get(SITE_LOCAL_ENV, "")).strip()
    if explicit:
        return explicit if os.path.isfile(explicit) else ""
    install_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for base in (install_root, os.getcwd()):
        candidate = os.path.join(base, SITE_LOCAL_NAME)
        if os.path.isfile(candidate):
            return candidate
    return ""


def session_path(env: Mapping[str, str] | None = None) -> str:
    """上次那份设定的落点。**装机目录下**，与 `site.local.sh` 同一层。

    `$EWB_SESSION` 可以指到别处（测试和"我不想让它记"都用得上；指到一个不可写的
    地方最多是存不下，见 `GuiState.save_session`）。刻意**不**跟着 cwd 走：
    从哪个目录起界面都该看到同一份设定，那正是这个功能存在的意义。
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    explicit = str(environ.get(SESSION_ENV, "")).strip()
    if explicit:
        return explicit
    return os.path.join(layout_module.install_root(), SESSION_NAME).replace(chr(92), "/")


def default_submit_command(env: Mapping[str, str] | None = None) -> str:
    """界面开局那格里该是什么：`site.local.sh` 给了就用它，否则占位符模板。

    读不出来（文件没了 / 没权限 / 编码坏了）**一律退回模板**，不抛：
    一个读不了的配置文件不该让 GUI 起不来，而退回去的那条模板本身是安全的
    （占位符没换掉就提交不了，见 `DEFAULT_SUBMIT_COMMAND` 第 3 条）。
    """
    path = site_local_path(env)
    if not path:
        return DEFAULT_SUBMIT_COMMAND
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            values = parse_site_local(handle.read())
    except OSError:
        return DEFAULT_SUBMIT_COMMAND
    return values.get(SITE_SUBMIT_KEY, "").strip() or DEFAULT_SUBMIT_COMMAND


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


def sweep_axis_value(
    mode: str, start: str, stop: str, step: str, points: str, spacing: str = ""
) -> str:
    """四个格子 → 扫频轴的取值字面量（就是 flag `=` 右边那一整串）。

    照 `mockups/_common.sweep_flag` 的形状；两个数值按 BRIEF §10 的实测更正过
    （生产是 `--multiSweep=adaptive,0:0.1:40`，设计稿把 step 当成了 start）。

    `spacing` ∈ `SWEEP_SPACINGS` 时**由它说了算**，选中的那格空着就 `SpecError`
    （拒绝而不是猜：猜出来的是 `adaptive,0--40` 这种既不报错又跑不对的东西）。
    `spacing=""` 是**老口径**（points 非空就赢），留给不认识这个参数的调用方 ——
    签名后加的可选参数，老调用点一个字都不用改。
    """
    if mode == "logarithmic":
        return str(stop).strip()
    if mode == "discrete":
        return str(start).strip()
    kind = "adaptive" if mode == "adaptive" else "lin"
    head, tail = str(start).strip(), str(stop).strip()
    want = str(spacing).strip()
    if want not in SWEEP_SPACINGS:
        want = "points" if str(points).strip() else "step"
    chosen = str(points if want == "points" else step).strip()
    if not chosen:
        raise SpecError(
            f"Frequency sweep: '{want}' is selected but empty.\n"
            f"  Next: type a value for '{want}', or switch to "
            f"'{'step' if want == 'points' else 'points'}'."
        )
    if want == "points":
        return f"{kind},{head}-{chosen}-{tail}"
    return f"{kind},{head}:{chosen}:{tail}"


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


def redact(text: str, table: Mapping[str, str]) -> str:
    """按 `GuiState.redaction_map()` 那张表把站点坐标换成占位符。

    纯字符串替换、不碰结构 —— 换完的命令仍然一眼看得出 flag 和顺序，
    只是 library / cell / ptxt / 路径变成了 `<lib1>` / `<ptxt>` / `<home>`。

    表是**长的在前**（`redaction_map()` 排好序才返回），这里逐条 `str.replace`
    就够了：先换掉长串，短串再来时那一段已经不在文本里了。

    这个函数在 `gui/state.py` 而不是 `gui/_ui.py`：它不碰 tkinter，
    所以纯 ssh 会话也能用、测试也不用起窗口（硬约束 5 的同一条理由）。
    """
    out = str(text)
    for value, placeholder in table.items():
        if value:
            out = out.replace(value, placeholder)
    return out


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
            f"Mesh value {value!r} is not understood: write a single number (all three "
            f"flags get it), or three segments edge/vertical/via separated by "
            f"{MESH_SEP!r} - they map to {' '.join(MESH_FLAGS)} in that order."
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
        raise SpecError(
            f"There is no builtin axis called {name!r}.\n"
            f"  Builtin axes: {', '.join(sorted(catalog))}"
        )
    wanted = _dedup(values)
    known = {av.value: av for av in axis.values}
    if name in _TOGGLE_AXES or all(value in known for value in wanted):
        missing = [value for value in wanted if value not in known]
        if missing:
            raise SpecError(
                f"Axis {name!r} does not know the value(s) {', '.join(missing)}.\n"
                f"  Legal values: {', '.join(sorted(known))}\n"
                "  (A toggle axis refuses to invent new values - guessing wrong makes the "
                "directory name and the command line say two different things.)"
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
        description="Mesh density - one value drives all three flags (site 0.4, eWave default 0.5)",
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
        description=(
            f"Frequency sweep ({mode}) - writes {flag}, "
            "the other two are cancelled with False"
        ),
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
        self._name_is_auto = not str(batch_name).strip()
        """当前这个批次名是**自动起的**（时间戳）还是**用户手打的**。见 `set_batch_name`。"""
        self.official_run_dir = official_run_dir
        self._default_submit_command = default_submit_command(env)
        """开局那条命令的出处：装了 `site.local.sh` 就是站点真实的那条，
        否则是 `DEFAULT_SUBMIT_COMMAND` 那条占位符模板。

        存下来是因为「用户动过没有」的判据要跟**这台机器的**默认值比，
        不是跟模板比 —— 拿模板当判据的话，装了 site.local 的机器上
        `_submit_is_template` 开局就是 False，站点前缀再也顶不进来。"""

        self.submit_command = self._default_submit_command
        """整条 dsub 命令，**原样暴露给用户改**（用户 2026-08-18 要求）。

        开局**不是空串**（用户 2026-08-20）；`plan()` 一从 `SiteFacts` 解析出站点的
        提交前缀，就把它整条顶掉 —— 官方 run 目录里那份比任何默认值都准。"""

        self._submit_is_template = True
        """`submit_command` 还是那条没人动过的默认值吗。

        这个布尔是承重的，不是记账。「用户还没给过命令」从前的判据是 `submit_command`
        为空串，而现在开局就有内容 —— 少了它会同时坏两处：
        ① `_site_facts()` 以为用户已经手写过命令，站点的提交前缀**永远灌不进来**；
        ② 模板里那条**例子**资源串会顶掉官方 run 目录里**真的**那条
        （`facts.dsub_resources`），`--parallel` 跟着一起错，而界面上两者长得一样。"""

        self._scheduler_override = scheduler
        self._runner_override = runner
        self._on_event = on_event
        self._env = dict(env) if env is not None else None
        self._discover = discover
        self._tool_version = tool_version

        self._designs: list[Design] = []
        self._selection: dict[str, tuple[str, ...]] = dict(DEFAULT_AXIS_SELECTION)
        self._groups: list[RunGroup] = []
        """base **之外**的 run group。base 的取值就是 `_selection`，存两份必然漂
        （`groups()` 现造一个空覆盖的 base 对象给界面用）。"""
        self._active_group: str = BASE_GROUP
        """界面正在编辑哪个组。`set_axis_values()` / `axis_selection()` 作用于它 ——
        active = base 时这两个方法与加组之前**逐字相同**。"""
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
        self._viewed: BatchState | None = None
        """**结果侧**：Runs 表现在显示的那一批。`None` = 显示预览（`_state`）。

        ★ 这一格就是「设定」和「结果」的分界线，2026-08-24 加的。在它之前两侧共用
        `_state` 一个对象 —— `plan()` 重建它，而 driver 就地改它 —— 于是"提交之后
        还能改设定"必然把跑着的那批状态冲掉。界面只好把设定面板整个冻住，
        而冻住又让整个工具显得是一次性的（用户原话：应该像 Cadence 的 simulation
        一样，跑一次就是一次新结果）。

        拆开之后两侧各有各的对象：
        * `_state`  = 预览。当前设定展开出来的矩阵，`plan()` 随便重建，**没人在跑它**；
        * `_viewed` = 结果。要么是 driver 手里那份（活的），要么是从磁盘 `batch.json`
          读回来的某一批（历史）。`plan()` **绝不碰它**。
        """
        self._viewed_contexts: dict[str, PlanContext] = {}
        """`_viewed` 那一批自己的 contexts。`resume()` 要用它，**不能用 `_contexts`** ——
        后者是当前设定的，而当前设定跟三个月前那一批可以毫无关系。"""
        self._result_state: BatchState | None = None
        """driver 那份结果是**对着哪一份 `BatchState`** 跑出来的。

        存它是因为 dry-run **不锁界面**（见 `has_submitted`）：跑完 dry-run 再动一个
        勾选，`recompute()` 就会重新 `plan()` 出一份全新的 `BatchState`，而 `_driver`
        还攥着上一份。少了这个身份比对，`summary()` 会拿**上一份矩阵**的计数去配
        **这一份矩阵**的表格 —— 表上 5 行、状态栏说 3 个，而两边都振振有词。
        """
        self._running = False
        self._dirty = True
        self._dry_run_only = True
        """上一次交给 driver 的是不是 dry-run。`has_submitted()` 靠它区分预览和真提交。
        没跑过的时候取 True 是有意的：那时 `has_submitted()` 必须是假，
        而 `_driver is None` 已经保证了这一点，这里只是不让它变成第二个真值来源。"""

    # ============================================================ 冻结面
    def load_spec(self, path: str) -> None:
        """读 spec 并建 `BatchState`（还不建目录、不提交）。失败抛 `SpecError`。

        spec 里的 designs / axes / defaults / extra_flags / options 会**覆盖**界面上
        当前的勾选 —— 用户按了 Load，界面就该显示文件里的东西，而不是两者的混合。
        """
        spec = spec_module.load_spec(path)
        self._apply_spec(spec, take_identity=True)

    def _apply_spec(self, spec: BatchSpec, *, take_identity: bool) -> None:
        """把一份 spec 灌进界面的勾选。`load_spec` 和 `adopt_settings_from` 共用。

        `take_identity` = 连 `batch_name` / `batch_root` 一起拿。Load 一个 spec 文件要
        （文件里写了就按文件的来）；而"照着历史里那一批再跑一次"**不要** ——
        那是新的一批，沿用旧名字就是写回旧目录。
        """
        self._spec = spec
        self._designs = list(spec.designs)
        # 组也要灌回界面，否则「Load 一个带 groups 的 spec」会静默退化成全笛卡尔积 ——
        # 那正是 run group 要消灭的东西，而界面上完全看不出少了什么。
        # 深拷一份 overrides：`BatchSpec` 里的 dict 后面还会被 `spec_to_batch` 用到。
        self._groups = [
            RunGroup(
                name=group.name,
                axis_overrides={k: tuple(v) for k, v in group.axis_overrides.items()},
                label=group.label,
            )
            for group in spec.groups
            if group.name != BASE_GROUP
        ]
        self._active_group = BASE_GROUP
        self._selection = self._selection_from_axes(spec.axes)
        # spec 里可能带一条**显式的 base 组**：顶层 `axes:` 是全批次的轴**定义**（并集），
        # base 自己扫哪几个由这条说了算。界面上"base 的勾选"就是 `_selection`，
        # 所以把它落到 `_selection` 上，而**不是**当成第 N 个组 ——
        # 当成组的话 `groups()` 会返回两个 base（它自己已经把 base 补在最前了）。
        # 这条正是 `_axes_and_groups()` 加宽轴时写出去的那一条，一来一回要闭合。
        for group in spec.groups:
            if group.name != BASE_GROUP:
                continue
            for name, values in group.axis_overrides.items():
                if name in self._selection:
                    self._selection[name] = tuple(str(v) for v in values)
        self._sweep = self._sweep_from_axes(spec.axes)
        self._extra_text = " ".join(_render_flag_token(k, v) for k, v in spec.extra_flags.items())
        self._default_overrides = dict(spec.defaults)
        self._options = spec.options
        # 批次级的官方 run 目录不在 `BatchSpec` 里（那是 per-design 字段）。
        # 顶上那一格是"没写自己那份的 design 用哪个"，读 spec 时从第一个有值的
        # design 认领回来 —— 否则 Load 完之后那格是空的，而矩阵其实是能展开的，
        # 界面看起来像"坐标丢了"。
        if not self.official_run_dir:
            self.official_run_dir = next(
                (d.official_run_dir for d in spec.designs if d.official_run_dir), ""
            )
        if take_identity:
            if spec.batch_name:
                self.batch_name = spec.batch_name
                # ★ 文件里写着的名字**是人起的**（不管是手打的还是上一次会话存下来的），
                #   所以 `_name_is_auto` 必须跟着变 False。这里从前是直接赋值、
                #   绕过 `set_batch_name`，于是那一位停在构造时的 True：
                #   * Load 一份叫 `mesh_sweep` 的 spec，下一次 Submit 会把名字丢掉、
                #     改用时间戳 —— 用户起的名字没了；
                #   * 「上次那份设定」存/读不幂等（存的时候按"自动名不存"丢掉它，
                #     再存又出现，两份文件不一样）。
                self._name_is_auto = False
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
        # 名字是 `spec_to_batch` 现起的时间戳（我们本来是空的）-> 记成"自动"。
        # `reset()` 靠这一位决定：自动的直接清空重起一个，手打的往后加序号。
        minted = not self.batch_name
        self._name_is_auto = self._name_is_auto or minted
        state = spec_module.spec_to_batch(
            spec, batch_root=self.batch_root, tool_version=self._tool_version
        )
        self.batch_name = state.batch_name
        if minted and _has_batch_json(state.batch_dir):
            # ★ **时间戳只到秒**（`model.TIMESTAMP_FORMAT`）。New batch 之后一秒内
            #   再按一次，现起的名字和上一批逐字相同 -> 又落回同一个目录，
            #   `reset()` 那一半的修复被这一下全抵消掉。而"按完 New batch 马上按
            #   Submit"恰恰是最自然的操作。
            #   所以自动名也要过一次占位检查 —— 判据是磁盘上有没有 `batch.json`，
            #   跟手打名那条路同一个判据、同一个函数。
            self.batch_name = self._next_free_batch_name()
            state = spec_module.spec_to_batch(
                replace(spec, batch_name=self.batch_name),
                batch_root=self.batch_root,
                tool_version=self._tool_version,
            )

        contexts = self._build_contexts(state, learn=True)

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

    def _build_contexts(self, state: BatchState, *, learn: bool) -> dict[str, PlanContext]:
        """`design_key` → `PlanContext`。**只从传进来的 `state` 取**，不看当前勾选。

        两个调用方，差别只在 `learn`：

        * `plan()`（`learn=True`）—— 预览。默认表现学现用（从官方目录 learn 一遍再叠
          用户的覆盖），因为设定就是"现在这一份"。顺带把第一份非空的记进 `_learned`
          给 Extraction defaults 对话框看。
        * `open_batch()`（`learn=False`）—— 打开一个**历史批次**。默认表用批次自己
          冻着的那份（`BatchState.defaults`），**不许拿今天的重新学**：那份就是为了
          "换了 PDK 版本之后 resume 老批次不能悄悄换值"才存进 batch.json 的。
          只有它是空的（老批次、或者当时就没学到）才退回现学 —— 空表拼不出命令，
          那时"用今天的"总好过"什么都没有"。
        """
        contexts: dict[str, PlanContext] = {}
        frozen = dict(state.defaults)
        for design in state.designs:
            key = matrix_module.design_key(design)
            facts = self._facts_for(design)
            if learn:
                defaults = dict(discover_module.learn_default_flags(facts))
                if not self._learned:
                    # 「学到的默认表」记第一份非空的那个，给 Extraction defaults 对话框显示。
                    # 学不到（本机没有官方目录）时保持空 —— 空表和"学到了一张空表"在界面上
                    # 是两回事，前者要显示成"还没学到，用的是内置兜底"。
                    self._learned = dict(defaults)
                defaults.update(self._default_overrides)
            else:
                defaults = dict(frozen) or dict(discover_module.learn_default_flags(facts))
            contexts[key] = PlanContext(
                design=design,
                facts=facts,
                axes=tuple(state.axes),
                defaults=defaults,
                extra_flags=dict(state.extra_flags),
                options=state.options,
                batch_dir=state.batch_dir,
            )
        return contexts

    # ==================================================== 结果侧：历史与「在看哪一批」
    # ★ 本段**只读磁盘**或只动 `_viewed`，一个设定都不碰。
    #   这条分界线是 2026-08-24「真解法」的全部内容，见 `_viewed` 上的注释。

    def batch_history(self) -> tuple[layout_module.BatchSummary, ...]:
        """落点底下有哪些批次，新的在前。**磁盘是唯一真相。**

        不缓存：批次可能是别的会话（或 CLI）跑出来的，缓存等于让界面对着一份过期的
        历史。一次 `listdir` + 每批一个小 JSON，几十批的量级不值得优化。
        """
        return layout_module.list_batches(
            os.path.abspath(os.path.expanduser(self.batch_root or DEFAULT_BATCH_ROOT))
        )

    def viewing(self) -> str:
        """Runs 表现在显示的是哪一批。空串 = 预览（当前设定会跑什么）。"""
        return self._viewed.batch_name if self._viewed is not None else ""

    def show_preview(self) -> None:
        """回到「当前设定会跑什么」。**跑着的那批照跑** —— 这只是换个看的东西。"""
        if self._running:
            raise EwaveBatchError(
                "A batch is running - the results view stays on it until it finishes." + _NL +
                "  Next: wait for it, or press Cancel."
            )
        self._viewed = None
        self._viewed_contexts = {}
        self._driver = None
        self._result_state = None
        self._invalidate()

    def open_batch(self, name: str) -> None:
        """看历史里的某一批。**只读磁盘，一个设定都不碰。**

        这是「设定」和「结果」拆开之后才成立的动作：打开三个月前那一批不该、也不会
        把界面上的勾选改成那一批的。想拿它的设定接着跑是**另一个**动作
        （`adopt_settings_from`），必须显式 —— 混成一件的话每点一次历史都会把手上
        正在编辑的设定冲掉。

        正在跑的时候不许切走：切走了 `tick()` 还在改 driver 手里那份，而表上显示的是
        别的东西 —— 两个都在动，就没人说得清哪个是真的。
        """
        if self._running:
            raise EwaveBatchError(
                "A batch is running - cannot switch the results view yet." + _NL +
                "  Next: wait for it to finish, or press Cancel."
            )
        cleaned = str(name).strip()
        if not cleaned:
            self.show_preview()
            return
        root = os.path.abspath(os.path.expanduser(self.batch_root or DEFAULT_BATCH_ROOT))
        path = os.path.join(root, cleaned, layout_module.BATCH_JSON_NAME)
        state = layout_module.read_batch_state(path)   # StateError 往上抛，界面弹框
        # 批次被搬过（或当初记的是别的机器上的绝对路径）时以**现在这个目录**为准 ——
        # 同 `sched.driver.resume_batch` 那条，理由也一样：否则产物一个都验不过。
        state.batch_dir = os.path.join(root, cleaned).replace('\\', "/")
        self._viewed = state
        self._driver = None      # 从磁盘读回来的，还没有 driver
        self._result_state = None
        self._events = []
        contexts = self._build_contexts(state, learn=False)
        self._viewed_contexts = contexts
        self._plans = {}
        self._plan_errors = {}
        for run in state.runs:
            context = contexts.get(run.design_key)
            if context is None:  # pragma: no cover - state 自洽时不会
                continue
            try:
                self._plans[run.run_id] = ewave_tool.build_ewave_plan(run, context)
            except EwaveBatchError as exc:
                self._plan_errors[run.run_id] = f"{exc.__class__.__name__}: {exc}"

    def adopt_settings_from(self, name: str) -> None:
        """把某一批的设定搬进界面（designs / 轴 / 组 / extra flags / 并行度）。

        **显式动作**，不是打开批次的副作用。搬完之后落点身份是**新的**：
        这是"照着它再跑一次"，不是"回到它"—— 沿用旧名字就是写回旧目录。
        """
        root = os.path.abspath(os.path.expanduser(self.batch_root or DEFAULT_BATCH_ROOT))
        state = layout_module.read_batch_state(
            os.path.join(root, str(name).strip(), layout_module.BATCH_JSON_NAME)
        )
        self._apply_spec(
            BatchSpec(
                designs=list(state.designs),
                axes=list(state.axes),
                groups=list(state.groups),
                defaults=dict(state.defaults),
                extra_flags=dict(state.extra_flags),
                options=replace(state.options, dry_run=False),
            ),
            take_identity=False,
        )
        self.batch_name = ""       # 新的一批 = 新身份，见 `reset()`
        self._name_is_auto = True

    def viewed_summary(self) -> layout_module.BatchSummary | None:
        """正在看的那一批的概况。看预览时 → None。"""
        if self._viewed is None:
            return None
        return layout_module.summarize_state(self._viewed)

    def start(self, *, dry_run: bool = False) -> None:
        """开跑（或 dry-run）。已经在跑时是 no-op。

        ★ **真提交一律起一个新批次**（2026-08-24 用户拍板的模型：跑一次就是一次新
        结果，旧的还在，跟 Cadence ADE 的 simulation 一样）。所以按下 Submit 的第一件
        事是换身份 —— 自动名重起一个时间戳，手打名往后找第一个没被占的 `-2`/`-3`…
        界面上那一格因此是**下一批的名字**（一个词根），不是"当前这一批"的名字。

        dry-run 不换：它不写盘、不占目录，换身份只会让那一格每按一次预览就变一次。
        """
        if self._running:
            return
        if not dry_run:
            self._mint_fresh_identity()
        self._options.dry_run = dry_run
        self._dry_run_only = dry_run
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
        self._result_state = state
        if not dry_run:
            # 提交完就看这一批 —— 它才是刚发生的那件事。
            # ★ **dry-run 不进这里**：它不写盘、磁盘上没有它，也就不是一条历史。
            #   当成"在看的一批"的话，跑完 dry-run 再改一个勾选，表会钉死在那份旧预览上
            #   （`_viewed` 不跟着 `plan()` 走，那正是它存在的意义）——
            #   而"dry-run 之后界面照样跟着勾选走"是 2026-08-20 修过的东西，不能再破一次。
            #   dry-run 跑的本来就是 `_state` 这个对象，driver 就地改它，表照样会动。
            self._viewed = state
            self._viewed_contexts = dict(self._contexts)
        self._running = True

    def _mint_fresh_identity(self) -> None:
        """换一个没被占的批次身份。`start()` 和 `reset()`（New batch）共用。

        判据是磁盘上有没有 `batch.json` —— 与 `batch_history()`、
        `deploy.sh::looks_like_batch_data` 同一条。三处不一致的话，
        列表里看不见的批次会被当成空位占掉，那就是覆盖。
        """
        self.batch_name = "" if self._name_is_auto else self._next_free_batch_name()
        self._invalidate()

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
        if self._viewed is None:
            # 没在看任何一批 = 手上只有预览，没有 `batch.json` 可读。
            # 退回 `start()`（它会起一个新批次），与本方法加进来之前逐字相同。
            self.start(dry_run=dry_run)
            return
        state = self._viewed
        from ewave_batch.sched.driver import resume_batch

        driver = resume_batch(
            state.batch_dir,
            self._viewed_contexts or self._contexts,
            self._make_scheduler(),
            self._make_runner(),
            on_event=self._record_event,
        )
        self._driver = driver
        # ★ **不碰 `_state`**：那是预览，是用户手上正在编辑的设定展开出来的东西。
        #   续跑一个历史批次不该把界面上的勾选换成它的 —— 那是 `adopt_settings_from`。
        self._viewed = driver.state
        self._result_state = driver.state
        self._dry_run_only = dry_run
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

    def result_state(self) -> BatchState | None:
        """Runs 表该显示哪一份 —— **结果侧唯一的入口**。

        看某一批时是那一批（`_viewed`，可能就是 driver 手里那份，driver 就地改它的
        status，所以表会跟着动）；看预览时是 `_state`。

        ★ 别在别处写第二个"该显示哪一份"的判断：这个分岔一旦有两份，
        表格和状态栏就会各读各的，出现"5 行的表配一句 3 runs"——
        那正是 `result_is_current()` 当年为之存在的病。
        """
        return self._viewed if self._viewed is not None else self._state

    def runs(self) -> tuple[Run, ...]:
        """正在看的那一批的全部 run（**同一批对象**，driver 就地改它们的 status）。"""
        state = self.result_state()
        return tuple(state.runs) if state is not None else ()

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
        if self._viewed is None and self.result_is_current():
            # 看预览、而预览又正好是 driver 跑的那一份（dry-run 之后没动过勾选）——
            # 那时 driver 的计数更权威。看某一批时**一律现数**：`_viewed` 里的
            # `Run.status` 就是真相（活的那份是 driver 就地改的，历史那份是磁盘读的）。
            counts.update(self._driver.summary())  # type: ignore[union-attr]
            return counts
        for run in self.runs():
            counts[run.status.value] = counts.get(run.status.value, 0) + 1
        return counts

    # ======================================================= 界面的编辑面
    # 冻结面之外的部分：三版 frame 用它们改勾选、读冲突、看默认表。
    # 每一个都只碰内存里的选择，**不写盘、不提交**；改完置脏，下一次 plan() 才重算。

    def set_batch_name(self, name: str) -> None:
        """改批次名。**顺带记住这个名字是谁起的** —— `reset()` 要靠它决定怎么换身份。

        判据是「值变了没有」，不是「有没有人调过本方法」：`plan()` 算出来的时间戳名字
        会经 `_ui.push()` 每一拍回灌一次（界面那一格是从 `batch_name` 同步的），
        每次都当成"用户手打的"，`_name_is_auto` 就永远是 False，New batch 也就永远
        换不了身份 —— 那正是这次要修的 bug 本身。
        """
        cleaned = str(name).strip()
        if cleaned != self.batch_name:
            self._name_is_auto = not cleaned
            self.batch_name = cleaned
            self._invalidate()

    def set_batch_root(self, root: str) -> None:
        self.batch_root = str(root).strip() or DEFAULT_BATCH_ROOT
        self._invalidate()

    def batch_root_warning(self) -> str:
        """落点指到了**已知会吃掉数据的地方** -> 一句红字。没问题 -> 空串。

        两条判据，是两次真实事故各留下的一条，方向**正好相反** ——
        所以这里不能只留一条，也不能把其中一条反过来写：

        1. **`$HOME` 底下**（用户 2026-08-24 报的，而且"发生很多次了"）：红区 `$HOME`
           有配额（实测约 500 MB），一个 run 的 mesh/pmsh 中间件就是几 GB。
           配额爆掉的样子是**致命的静默** —— `mvp/redzone/go_workarea.sh` 记着实测：
           eresist 写 `resist.rst` 写不下拿到 0 字节、却照样打印 "Execute eresist done."，
           emsolver 随后读空文件崩掉。`df` 和 `quota` 都骗过人，只有实写算数
           （所以还有 `preflight()` 里那个实写探针，这一条只是提前一步说话）。
        2. **`<install>/.deploy/` 底下**：那是部署自己的地盘（staging / backups /
           scratch），`deploy.sh` 会轮转删它。

        ⚠️ **安装目录本身现在是默认落点，不再报警**（2026-08-20 那版在这里报警，
        而它正是把人推去 `~/` 的那句话）。安装目录安全的理由不在界面这一层，在
        `deploy.sh`：`PRESERVE` 写死 `ewave_batches`，且任何含 `batch.json` 的顶层
        目录一律不搬（`looks_like_batch_data`），`tests/test_deploy.py` 端到端守着。

        判据一律是**解析后的绝对路径**，不是字符串前缀比较 —— `~` 和 `./` 都得先展开
        才知道到底落在哪，而这两种写法恰恰是最常见的。
        """
        env = self._env if self._env is not None else os.environ
        try:
            root = os.path.abspath(os.path.expanduser(self.batch_root or DEFAULT_BATCH_ROOT))
        except (OSError, ValueError):  # pragma: no cover - 怪路径
            return ""

        def _inside(parent: str) -> bool:
            if not parent:
                return False
            try:
                return os.path.commonpath([root, os.path.abspath(parent)]) == os.path.abspath(parent)
            except (OSError, ValueError):  # 不同盘符 / 怪路径
                return False

        install = layout_module.install_root()
        deploy_dir = os.path.join(install, ".deploy")
        if _inside(deploy_dir):
            return (
                "Batch root is inside %s." % deploy_dir + _NL +
                "  That is the deploy machinery's own scratch/backup area and it gets "
                "rotated away - results kept here disappear a few deploys later." + _NL +
                "  Next: point it somewhere else, e.g. %s" % DEFAULT_BATCH_ROOT
            )

        home = env.get("HOME") or env.get("USERPROFILE") or ""
        if home and _inside(home) and not _inside(install):
            return (
                "Batch root is under $HOME (%s)." % home + _NL +
                "  $HOME is quota'd here (~500 MB was measured) while one run's mesh "
                "intermediates run to several GB. Blowing the quota is SILENT: eWave "
                "writes a 0-byte file, still prints 'done', and the next stage crashes "
                "reading it. df and quota both under-report this - only a real write "
                "tells the truth." + _NL +
                "  Next: point it back under the workarea, e.g. %s" % DEFAULT_BATCH_ROOT
            )
        return ""

    PROBE_BYTES = 8 * 1024 * 1024
    """落点实写探针的大小。**8 MB 是刻意挑的**：小到不值一提（本地盘上几十毫秒），
    大到能撞上"配额只剩一点点"——那正是 2026-08-24 那次的形状（$HOME 配额约 500 MB，
    已经快满）。真正的中间件是几 GB，探针不可能替它证明"装得下"，它只证明
    **"现在还能不能写"**，而那一条恰恰是 df/quota 答不对的那条。"""

    def _next_free_batch_name(self) -> str:
        """`<name>` 已经被占了就找 `<name>-2` / `<name>-3`… 第一个空位。

        「被占」= 那个目录**已经是一个批次**（里面有 `batch.json`）。仅仅存在一个同名
        空目录不算 —— 那多半是人自己建来放东西的，占着它的名字没有道理。
        `_LOOP_CAP` 只是个防呆上限：真到 999 个同名批次，说明词根本身该换了。
        """
        name = self.batch_name.strip()
        if not name:
            return ""
        # 词根 = 去掉末尾那个 `-<数字>`。不去的话序号会**叠加**：
        # `mesh` -> `mesh-2` -> `mesh-2-2` -> `mesh-2-2-2`（2026-08-24 实测），
        # 连按三次 Submit 就没法看了。去掉之后是 `mesh-2` / `mesh-3` / `mesh-4`。
        # 用户自己就想叫 `mesh-2` 也没事：下面第一件事是问它被占了没有，
        # 没被占就原样返回 —— 只有真撞上了才从词根往后数。
        base = re.sub(r"-\d+$", "", name) or name
        try:
            root = os.path.abspath(os.path.expanduser(self.batch_root or DEFAULT_BATCH_ROOT))
        except (OSError, ValueError):  # pragma: no cover - 怪路径
            return base

        def taken(name: str) -> bool:
            return _has_batch_json(os.path.join(root, name))

        if not taken(name):
            return name
        for suffix in range(2, 1000):
            candidate = "%s-%d" % (base, suffix)
            if not taken(candidate):
                return candidate
        return base  # pragma: no cover - 999 个同名批次

    def next_batch_name(self) -> str:
        """**下一次 Submit** 会用哪个名字。空串 = 到时候现起一个 UTC 时间戳。

        ⚠️ 与 `batch_dir()` 是两个问题，别混：那个答的是「我正在看的那一批在哪」
        （看历史时就是那一批的目录）。混用的症状很隐蔽 —— 2026-08-24 实测：
        `preflight()` 拿 `batch_dir()` 当"下一批会落在哪"，于是提交完第一批之后
        第二次 Submit 被自己的守卫拦住（"那儿已经有一批了"——那是**正在看的**那一批），
        而按钮是亮的、什么都没发生。
        """
        return "" if self._name_is_auto else self._next_free_batch_name()

    def next_batch_dir(self) -> str:
        """下一次 Submit 会落在哪个目录。名字要到 `plan()` 才现起时 → 根目录 + `<batch>`。"""
        root = os.path.abspath(os.path.expanduser(self.batch_root or DEFAULT_BATCH_ROOT))
        return os.path.join(root, self.next_batch_name() or "<batch>").replace(chr(92), "/")

    def existing_batch_at_landing(self) -> str:
        """**下一批**的落点上已经有一个批次了 -> 它的名字；没有 -> 空串。

        判据是 `<下一批的目录>/batch.json` 在不在。给 `preflight()` 用：新提交一批
        **不许**盖在别人身上，而 `write_batch_state` 那一层是无条件原子覆盖
        （它必须是 —— 跑到一半每拍都要写），所以拦只能拦在按下去之前。

        正常路径下这条**永远不该响**：`start()` 每次先 `_mint_fresh_identity()`，
        而那个函数挑的就是第一个没被占的名字。它是**背带**，守的是 mint 挑不出空位
        （同一词根 999 批）那种边角，以及将来有人绕过 mint 直接调 `start()`。
        一条永远不响的守卫也比一条会说谎的守卫强 —— 前者是保险，后者是 bug。

        ⚠️ 只挡新提交，不挡 resume：resume 存在的全部意义就是读那个 `batch.json`。
        """
        name = self.next_batch_name()
        if not name:
            # 名字要到 `plan()` 才现起，而 `plan()` 自己会避开被占的（见那里的
            # `existing_batch_at_landing` 分支）—— 这里没有可判的东西。
            return ""
        return name if _has_batch_json(self.next_batch_dir()) else ""

    def batch_root_check(self) -> str:
        """按下 Submit 前**真往落点写一个文件**。写得下 -> 空串；写不下 -> 挡路的原因。

        为什么不能只看 `df` / `quota`：`mvp/redzone/relocate_and_rerun.sh` 记着实测 ——
        「`df -h $HOME` 报的那个『可用』是误导，那是文件系统剩余，不是用户配额」，
        两个都骗过人。而配额爆掉在 eWave 那一侧是**静默**的：写出 0 字节还照样打印
        "done"，下一级读空文件才崩 —— 也就是说不实写，第一个知道出事的会是 35 分钟后
        的 emsolver。

        判据是**回读字节数**，不是"有没有抛异常"：配额满时 `write()` 常常不报错，
        错误要到 `close()` 才出来，甚至干脆只留下一个短文件（这正是 0 字节 `resist.rst`
        的来路）。所以写完必须 flush + 关闭 + `stat` 回读。

        `batch_root_warning()` 是它的**前哨**（不落盘、每次重画都跑、只认已知有害的
        位置）；这一条贵一点、只在按下去那一刻跑一次，但它是唯一说真话的那个。
        """
        try:
            root = os.path.abspath(os.path.expanduser(self.batch_root or DEFAULT_BATCH_ROOT))
        except (OSError, ValueError):  # pragma: no cover - 怪路径
            return ""
        probe = os.path.join(root, ".ewb_write_probe")
        written = -1
        try:
            os.makedirs(root, exist_ok=True)
            # 探针文件**本工具自己命名**，只建它、只删它，绝不碰落点里别的东西。
            with open(probe, "wb") as handle:
                handle.write(bytes(self.PROBE_BYTES))
                handle.flush()
                os.fsync(handle.fileno())   # 配额错误常常要到这里才现身
            written = os.stat(probe).st_size
        except OSError as exc:
            return (
                "Cannot write to the batch root (%s): %s" % (root, exc) + _NL +
                "  Nothing has been submitted. A run writes tens of GB here, so this is "
                "checked before any job goes out." + _NL +
                "  Next: pick a Batch root you can write to, e.g. %s" % DEFAULT_BATCH_ROOT
            )
        finally:
            try:
                os.remove(probe)
            except OSError:  # pragma: no cover - 建都没建起来
                pass
        if written != self.PROBE_BYTES:
            return (
                "The batch root (%s) accepted a %d MB test write but only %d bytes "
                "landed - that is a quota or a full filesystem."
                % (root, self.PROBE_BYTES // (1024 * 1024), max(written, 0)) + _NL +
                "  This is the failure that does NOT announce itself: eWave writes a "
                "short file, still prints 'done', and the next stage crashes reading it "
                "35 minutes later." + _NL +
                "  Next: point Batch root at something with room, e.g. %s"
                % DEFAULT_BATCH_ROOT
            )
        return ""

    PROBE_BYTES = 8 * 1024 * 1024
    """落点实写探针的大小。**8 MB 是刻意挑的**：小到不值一提（本地盘上几十毫秒），
    大到能撞上"配额只剩一点点" —— 那正是 2026-08-24 那次的形状（$HOME 配额约 500 MB，
    而且已经快满）。真正的中间件是几 GB，探针不可能替它证明"装得下"，它只证明
    **"现在还能不能写"**，而那一条恰恰是 df/quota 答不对的那条。"""

    def batch_root_check(self) -> str:
        """按下 Submit 前**真往落点写一个文件**。写得下 -> 空串；写不下 -> 挡路的原因。

        为什么不能只看 `df` / `quota`：`mvp/redzone/relocate_and_rerun.sh` 记着实测 ——
        「`df -h $HOME` 报的那个『可用』是误导，那是文件系统剩余，不是用户配额」，
        两个都骗过人。而配额爆掉在 eWave 那一侧是**静默**的：写出 0 字节还照样打印
        "done"，下一级读空文件才崩 —— 不实写的话，第一个知道出事的会是 35 分钟后的
        emsolver。

        判据是**回读字节数**，不是"有没有抛异常"：配额满时 `write()` 常常不报错，
        错误要到 `close()` / `fsync()` 才出来，甚至干脆只留下一个短文件（这正是
        0 字节 `resist.rst` 的来路）。所以写完必须 flush + fsync + `stat` 回读。

        `batch_root_warning()` 是它的**前哨**（不落盘、每次重画都跑、只认已知有害的
        位置）；这一条贵一点、只在按下去那一刻跑一次，但它是唯一说真话的那个。
        """
        try:
            root = os.path.abspath(os.path.expanduser(self.batch_root or DEFAULT_BATCH_ROOT))
        except (OSError, ValueError):  # pragma: no cover - 怪路径
            return ""
        probe = os.path.join(root, ".ewb_write_probe")
        written = -1
        try:
            os.makedirs(root, exist_ok=True)
            # 探针文件**本工具自己命名**，只建它、只删它，绝不碰落点里别的东西。
            with open(probe, "wb") as handle:
                handle.write(bytes(self.PROBE_BYTES))
                handle.flush()
                os.fsync(handle.fileno())   # 配额错误常常要到这里才现身
            written = os.stat(probe).st_size
        except OSError as exc:
            return (
                "Cannot write to the batch root (%s): %s" % (root, exc) + _NL +
                "  Nothing has been submitted. A run writes tens of GB here, so this is "
                "checked before any job goes out." + _NL +
                "  Next: pick a Batch root you can write to, e.g. %s" % DEFAULT_BATCH_ROOT
            )
        finally:
            try:
                os.remove(probe)
            except OSError:  # pragma: no cover - 建都没建起来
                pass
        if written != self.PROBE_BYTES:
            return (
                "The batch root (%s) accepted a %d MB test write but only %d bytes "
                "landed - that is a quota or a full filesystem."
                % (root, self.PROBE_BYTES // (1024 * 1024), max(written, 0)) + _NL +
                "  This is the failure that does NOT announce itself: eWave writes a "
                "short file, still prints 'done', and the next stage crashes reading it "
                "35 minutes later." + _NL +
                "  Next: point Batch root at something with room, e.g. %s"
                % DEFAULT_BATCH_ROOT
            )
        return ""

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
        """**active group** 的有效取值（轴名 → 取值），拷贝，改它不影响状态。

        active = base 时就是 base 自己的勾选，与加组之前**逐字相同**。
        active 是别的组时返回**合并后**的有效值：这个组覆盖了的轴给它自己的取值，
        没覆盖的轴给 base 的 - 界面上那一排勾选框显示的就该是这个组实际会跑的东西。
        「这根轴到底是继承还是覆盖」问 `group_override()`（`None` = 继承）。
        """
        if self.active_group() == BASE_GROUP:
            return dict(self._selection)
        merged = dict(self._selection)
        merged.update(
            {name: tuple(values) for name, values in self._active().axis_overrides.items()}
        )
        return merged

    def set_axis_values(self, name: str, values: Sequence[str]) -> None:
        """改一根轴的取值 - 落在 **active group** 上。`temperature` 顺手归一。

        active = base：改 base 的勾选（与加组之前逐字相同）。
        active 是别的组：写这个组的覆盖；空取值 = 撤销覆盖（回去继承 base）——
        因为「空取值」对一个组来说不是"这根轴不扫"，而是笛卡尔积塌成 0 个 run，
        `core.matrix` 对它明确拒绝。
        """
        target = self.active_group()
        if target == BASE_GROUP:
            self._set_base_values(name, values)
        else:
            self._set_group_values(target, name, values)

    # ------------------------------------------------------------ run group
    # 非冻结面（`docs/INTERFACES.md`「还没冻结的东西」）。模型见 INTERFACES 的
    # 「run group」一节：批次 = 一列组，每组在 base 之上覆盖几根轴、各自取笛卡尔积、
    # 结果取并集。**组是 delta**，所以这里存的永远只是覆盖，不是一份完整的轴表。

    def groups(self) -> tuple[RunGroup, ...]:
        """全部组，**第一个恒为 base**（现造的空覆盖对象）。

        base 不存在 `self._groups` 里：它的轴就是界面上的勾选（`_selection`），
        存两份必然漂 - 这也是 `BatchSpec.groups` 的口径（那里存的是 base 之外的组）。
        """
        return (RunGroup(name=BASE_GROUP),) + tuple(self._groups)

    def active_group(self) -> str:
        """当前在编辑哪个组。默认 base；组被删掉之后自动退回 base。"""
        if self._active_group != BASE_GROUP and self._find_group(self._active_group) is None:
            self._active_group = BASE_GROUP
        return self._active_group

    def set_active_group(self, name: str) -> None:
        """切换正在编辑的组。名字不认识 -> `SpecError`（**不许静默退回 base**：
        那会让用户以为自己在改某个组，实际上改的是基线）。"""
        cleaned = str(name).strip() or BASE_GROUP
        if cleaned != BASE_GROUP:
            self._require_group(cleaned)
        self._active_group = cleaned

    def suggest_group_name(self, wanted: str = "") -> str:
        """**不建组**，只回答"叫这个名字的话实际会叫什么"。

        界面上的「新建 / 复制」对话框拿它当输入框的默认值 —— 用户看到的建议名
        必须就是不填时真会用的那个，否则对话框在说谎。
        """
        return self._unique_group_name(wanted or "group")

    def add_group(self, name: str = "") -> str:
        """加一个空组并切过去，返回**实际用的名字**（重名自动加后缀）。

        新组一根轴都不覆盖 => 它展开出来的 run 与 base 逐字相同 => 全被跨组去重吃掉
        => 贡献 0 个 run。这不是 bug，是"还没配"的正常中间态；`group_run_counts()`
        照样给它一行 0，界面上看得见。

        名字**显式**写成 `base` -> `SpecError`（与 `rename_group` 同一条规矩）。
        不叫"自动加后缀变成 base-2"：用户是打字打出来的，得到一个自己没打过的名字
        比被拦下来更难理解。留空则用 `group`（那不是用户的选择，加后缀理所当然）。
        """
        self._reject_reserved(name)
        actual = self._unique_group_name(name or "group")
        self._groups.append(RunGroup(name=actual))
        self._active_group = actual
        self._invalidate()
        return actual

    def duplicate_group(self, name: str, new_name: str = "") -> str:
        """复制一个组（base 也能复制）成新组并切过去，返回新名字。

        复制 base 时把当前勾选**逐轴写成显式覆盖** - 空覆盖的副本没有意义
        （它就是 base），而写成显式覆盖之后用户改一根轴就得到一个真正的变体。

        `new_name` 留空 -> `<源名>-copy`（重名自动加后缀）。给了就用给的那个 ——
        组名会出现在 Runs 表、每一条关于这个组的消息、以及**产物目录名**里，
        所以"eqcur-off"比"base-copy-2"值钱得多，用户应该能在建它的那一刻就定下来。
        """
        self._reject_reserved(new_name)
        source = str(name).strip() or BASE_GROUP
        if source == BASE_GROUP:
            overrides = self._materialised_base_overrides()
            label = ""
        else:
            group = self._require_group(source)
            overrides = {k: tuple(v) for k, v in group.axis_overrides.items()}
            label = group.label
            if not overrides:
                # 源组自己一根轴都没覆盖（`add_group` 出来的空组就是这样）⇒ 照抄等于
                # 又造一个空组：界面上**整块 Settings 是灰的**，看起来像坏了，而
                # 用户刚按的是"复制"，他要的是"一份能改的东西"。理由与复制 base
                # 那条逐字相同 —— 空覆盖的副本没有意义。
                overrides = self._materialised_base_overrides()
        actual = self._unique_group_name(str(new_name).strip() or f"{source}-copy")
        self._groups.append(RunGroup(name=actual, axis_overrides=overrides, label=label))
        self._active_group = actual
        self._invalidate()
        return actual

    def _materialised_base_overrides(self) -> dict[str, tuple[str, ...]]:
        """把 base 现在的勾选写成一份**显式覆盖** —— 只写组管得着的那几根轴。

        `GROUP_OVERRIDABLE_AXES` 那道筛子是这个方法的**全部**要点：没有它，
        复制出来的组会带着 freq 和两个 tolerance 的显式覆盖，而界面上那三样
        在非 base 组里是置灰的 —— 看不见、点不到、撤不掉，却照样进笛卡尔积。
        """
        return {
            axis.name: tuple(av.value for av in axis.values)
            for axis in self._base_axes()
            if axis.name in GROUP_OVERRIDABLE_AXES
        }

    def remove_group(self, name: str) -> None:
        """删一个组。删 base -> `SpecError`（base 就是顶层的轴，删不掉）。

        名字不存在是 **no-op**：界面上"删掉刚删过的那一行"是很自然的重复点击，
        为它弹一个框没有意义；而删 base 是界面把按钮接错了，必须响。
        """
        cleaned = str(name).strip()
        if cleaned == BASE_GROUP:
            raise SpecError(
                "The base group cannot be removed - it is the top-level axes themselves "
                "(every batch has one).\n"
                "  Next: to drop the extra runs, remove the other groups instead."
            )
        group = self._find_group(cleaned)
        if group is None:
            return
        self._groups.remove(group)
        if self._active_group == cleaned:
            self._active_group = BASE_GROUP
        self._invalidate()

    def rename_group(self, old: str, new: str) -> None:
        """改组名。base 改不了；新名字必须非空、不是 `base`、且不与别的组重名。"""
        source = str(old).strip()
        target = str(new).strip()
        if source == BASE_GROUP or target == BASE_GROUP:
            raise SpecError(
                f"{BASE_GROUP!r} is a reserved group name (it means the top-level axes), "
                "so it cannot be renamed or reused.\n"
                "  Next: pick another name, e.g. eqcur-off"
            )
        if not target:
            raise SpecError(
                "A run group needs a name - it shows up in the Runs table and in every "
                "message about that group.\n"
                "  Next: e.g. eqcur-off"
            )
        group = self._require_group(source)
        if target == source:
            return
        if self._find_group(target) is not None:
            raise SpecError(
                f"There is already a run group called {target!r} - two groups with one name "
                "would shadow each other.\n"
                "  Next: pick another name"
            )
        group.name = target
        if self._active_group == source:
            self._active_group = target
        self._invalidate()

    def group_override(self, axis: str, group: str = "") -> tuple[str, ...] | None:
        """这个组在这根轴上的覆盖。`None` = **继承 base**（界面据此画"继承"的样子）。

        `group` 省略 = active group。base 永远返回 `None`：base 没有"覆盖"这一说，
        它的取值就是 `axis_selection()`。
        """
        name = str(group).strip() or self.active_group()
        if name == BASE_GROUP:
            return None
        values = self._require_group(name).axis_overrides.get(str(axis))
        return tuple(values) if values is not None else None

    def set_group_override(self, axis: str, values: Sequence[str], group: str = "") -> None:
        """给某个组的某根轴写覆盖。`group` 省略 = active group；`group` 是 base 时
        改的就是 base 自己的勾选。空取值 = 撤销覆盖（见 `set_axis_values`）。"""
        name = str(group).strip() or self.active_group()
        if name == BASE_GROUP:
            self._set_base_values(axis, values)
            return
        self._set_group_values(name, axis, values)

    def clear_group_override(self, axis: str, group: str = "") -> None:
        """把这根轴还给 base（继承）。base 上是 no-op - base 没有可撤的覆盖。"""
        name = str(group).strip() or self.active_group()
        if name == BASE_GROUP:
            return
        if self._require_group(name).axis_overrides.pop(str(axis), None) is not None:
            self._invalidate()

    def group_run_counts(self) -> list[tuple[str, int]]:
        """`[(组名, 去重后的 run 数), ...]`，顺序 = `groups()`（base 在最前）。

        数的是**跨组去重之后**的：两个组都写了 55 度时那个 run 归先出现的组
        （base 排最前 => 基线永远归 base）。一个组的 run 全被吃掉时它就是 0，
        界面照样要给它一行 - 0 正是"这个组还没改出任何新东西"的信号。
        """
        expansion = self._expansion()
        counts = dict(expansion.per_group) if expansion is not None else {}
        return [(group.name, counts.get(group.name, 0)) for group in self.groups()]

    def merged_run_count(self) -> int:
        """跨组撞车、被静默折叠掉的 run 数（界面显示 "5 runs (1 duplicate merged)"）。

        这个数必须让人看见：只写 "5 runs" 会让用户以为自己那条组写错了、少展开了一个。
        """
        expansion = self._expansion()
        return expansion.merged if expansion is not None else 0

    def group_summary(self, name: str) -> str:
        """一行摘要。组给 delta（`"+ 55.0, eqI off"`），base 给全量。"""
        target = str(name).strip() or self.active_group()
        axes = {axis.name: axis for axis in self._base_axes()}
        if target == BASE_GROUP:
            parts = [
                _summary_fragment(axis, tuple(av.value for av in axis.values))
                # 扫频不进摘要：它的取值本身带逗号（`adaptive,0:0.1:40`），塞进这行
                # 会跟分隔符打架，而界面上它自己有一整排格子。
                for axis in self._base_axes()
                if axis.name != "freq"
            ]
            return ", ".join(parts) or "(no axes selected)"
        group = self._require_group(target)
        if not group.axis_overrides:
            return "(inherits base - nothing overridden yet)"
        parts = []
        for axis_name, values in group.axis_overrides.items():
            axis = axes.get(axis_name)
            parts.append(
                _summary_fragment(axis, tuple(values))
                if axis is not None
                else f"{axis_name} {'/'.join(str(v) for v in values)}"
            )
        return "+ " + ", ".join(parts)

    def group_of(self, run_id: str) -> str:
        """这个 run 出自哪个组。认不出的 run_id -> 空串。"""
        run = self.run(run_id)
        if run is None:
            return ""
        return run.group or BASE_GROUP

    def groups_change_warning(self) -> str:
        """改组之前该不该警告用户。没什么好警告的 -> 空串。

        为什么这句话必须存在：`<axes-slug>` 只编码「全批次在变」的轴，而组的取值也算在
        「全批次」里。于是**加一个组会改掉基线自己的目录名**（`base/...` 变成
        `eqI-on__fw-off/...`）- 这是正确且不可避免的（否则两个组的 55 度落进同一个
        目录 = 静默覆盖），但对一个已经跑过的批次来说，resume 靠 run_id 对号，
        老目录当场就认不出来了。

        ⚠️ **判据不能只看 `has_started()`**（那只是"本进程里点过 Dry-run/Submit"）。
        批次跨天是常态（一个 run 10 核 / 100 GB / 35 分钟，BRIEF §12），最常见的场景
        恰恰是"昨天跑完，今天重开 GUI 加个组" —— 那时 `self._driver is None`，
        只看 `has_started()` 的话界面上一声不吭，而磁盘上那批 `base/...` 目录已经
        对不上号了。所以磁盘上已经有这个批次的 `batch.json` 时同样要警告
        （2026-08-19 复核实测）。
        """
        started = self.has_started()
        on_disk = (not started) and self._batch_json_exists()
        if not started and not on_disk:
            return ""
        head = (
            "This batch has already been submitted once. "
            if started
            else "A batch with this name already exists on disk (%s). " % self._batch_json_path()
        )
        return (
            head + "Editing run groups changes which "
            "axes vary across the batch, and every varying axis goes into the directory name "
            "- so the run ids change, the baseline ones included (base/... becomes "
            "eqI-on__fw-off/... for example). Resume matches runs by run id, so the finished "
            "runs already on disk would no longer be recognised.\n"
            "  Next: press New batch (or pick another batch name) to start a fresh batch with "
            "these groups, or leave the groups as they are."
        )

    def _batch_json_path(self) -> str:
        """这个批次的 `batch.json` 该在哪。**不读盘**，只拼路径。"""
        return os.path.join(self.batch_dir(), BATCH_JSON_NAME).replace("\\", "/")

    def _batch_json_exists(self) -> bool:
        """磁盘上已经有这个批次了没有。

        探测失败（权限、路径怪）一律当"没有"：这条警告是提示，不该因为探测本身
        把界面搞崩。
        """
        try:
            return os.path.isfile(self._batch_json_path())
        except OSError:  # pragma: no cover - 怪到 isfile 都抛的路径
            return False

    def sweep(self) -> dict[str, str]:
        return dict(self._sweep)

    def set_sweep(self, **fields: str) -> None:
        """改频率扫描的某几个格子（`mode` / `start` / `stop` / `step` / `points`）。"""
        for key, value in fields.items():
            if key in self._sweep:
                self._sweep[key] = str(value)
        self._invalidate()

    def sweep_live_fields(self) -> tuple[str, ...]:
        """当前**真正可编辑**的那几个格子（其余界面上置灰）。

        模式先砍一刀（`SWEEP_FIELDS`），`spacing` 再砍一刀：`step` 和 `points` 互斥，
        没被选中的那个不许编辑 —— 两个都能填的时候界面没法说清会用哪一个
        （用户 2026-08-20 指出的就是这个）。
        """
        fields = SWEEP_FIELDS.get(self._sweep.get("mode", ""), ())
        spacing = self._sweep.get("spacing", "")
        if spacing not in SWEEP_SPACINGS or not set(SWEEP_SPACINGS) <= set(fields):
            # 只有**两个都在**的模式（adaptive / linear）才谈得上互斥。
            # logarithmic 的 SWEEP_FIELDS 里有 points 没有 step，硬套这条规则会把
            # 它唯一的那格也灰掉。
            return fields
        dead = "points" if spacing == "step" else "step"
        return tuple(name for name in fields if name != dead)

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
        self._submit_is_template = self.submit_command == self._default_submit_command
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

    def submit_command_placeholders(self) -> tuple[str, ...]:
        """命令里还剩哪些没换掉的占位符（`ACCOUNT` / `QUEUE`）。都换掉了 -> 空元组。

        判据是**逐 token 比对**，不是「整条命令等不等于模板」：最常见的半改状态是
        账号填了、队列忘了 —— 那条命令和模板已经不同，却照样一个 job 都提交不成。
        """
        if not self.submit_command.strip():
            return ()
        try:
            from ewave_batch.sched.donau import parse_dsub_prefix

            tokens: Sequence[str] = parse_dsub_prefix(self.submit_command)
        except EwaveBatchError:
            # 命令本身就不合法（`submit_command_error()` 会说清楚是哪种毛病）——
            # 这里退回粗分词，好让「又非法、又没换占位符」两条都报得出来。
            tokens = self.submit_command.split()
        return tuple(name for name in SUBMIT_PLACEHOLDERS if name in tokens)

    def submit_command_error(self) -> str:
        """这条 dsub 命令有什么毛病（能提交就返回空串）。界面把它显示成红字。"""
        if not self.submit_command.strip():
            return ""
        try:
            from ewave_batch.sched.donau import parse_dsub_prefix

            parse_dsub_prefix(self.submit_command)
        except EwaveBatchError as exc:
            return str(exc)
        left = self.submit_command_placeholders()
        if left:
            return (
                "placeholder(s) not replaced: %s - as it stands dsub would reject every "
                "job in this batch (no such account / queue).\n"
                "  Next: set the official run dir and the whole dsub line is read from it, "
                "or type the real Donau account / queue here."
                % " / ".join(left)
            )
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

    def set_max_parallel(self, value: int) -> int:
        """同时在飞的 job 数上限。返回**实际生效**的值（越界会被夹）。

        ★ 为什么它不走 `set_axis_values` 那条路、也不 `_invalidate()`：
        它**不是矩阵的一部分**。改一根轴会让上一次 plan 作废（run 的集合变了），
        而改这个只改"一次放几个出去"—— run 还是那些 run。所以它在批次**跑起来之后**
        照样能改，而那正是最想改它的时刻（4 个在跑、第 5 个在等，用户想让它也走）。
        driver 每一拍都重新读 `options.max_parallel`（`_submit_step`），所以下一拍就生效。
        """
        try:
            wanted = int(str(value).strip())
        except (TypeError, ValueError):
            raise SpecError(
                "Max in flight must be a whole number (how many jobs may be in the "
                "scheduler at once).\n  Next: e.g. 4"
            ) from None
        self._options.max_parallel = max(1, min(MAX_PARALLEL_CAP, wanted))
        return self._options.max_parallel

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
        """当前勾选会展开出多少个 run。**不落盘、不建目录** - 边勾边看的那个数。

        有组时这是**跨组去重之后**的数（两个组都写了 55 度只算一个），
        与 `plan()` 真正建出来的条数一致 - 两个数不一致的话，界面上那个数就是谎话。
        """
        expansion = self._expansion()
        return len(expansion.runs) if expansion is not None else 0

    def formula(self) -> str:
        """算式那一行。

        只有 base 时保持今天的写法 `2 designs x 1 corner x 3 temp x 2 mode = 12 runs`
        （最常见的场景不该因为多了一个功能而变难懂）。
        有组时改成 `2 designs x (3 + 1 + 1) = 10 runs`：括号里逐组一项，因为这时候
        整批已经不是一个笛卡尔积了，写成连乘就是假的。
        """
        expansion = self._expansion()
        total = len(expansion.runs) if expansion is not None else 0
        merged = expansion.merged if expansion is not None else 0
        tail = f" = {total} runs"
        if merged:
            word = "duplicate" if merged == 1 else "duplicates"
            tail += f" ({merged} {word} merged)"
        per_group = [count for _name, count in (expansion.per_group if expansion else ())]
        if len(per_group) <= 1:
            # 实际参与展开的只有 base（没有组，或者组都还空着）=> 用今天的连乘写法。
            # 空组写成 `1 designs x (1)` 是纯噪声：那个括号里永远只有基线自己。
            selection = self.axis_selection()
            parts = [f"{len(self._designs)} designs"]
            for name, label in (
                ("corner", "corner"),
                ("temperature", "temp"),
                ("fullWave", "mode"),
            ):
                parts.append(f"{len(selection.get(name, ()))} {label}")
            return " x ".join(parts) + tail
        designs = len(self._designs)
        # 每组的 run 数都能被 design 数整除时才把 design 提到括号外 - 提不出来
        # （某个 design 自己覆盖了轴）就老实写各组的总数，宁可难看也别写错。
        if designs and all(count % designs == 0 for count in per_group):
            inner = " + ".join(str(count // designs) for count in per_group)
            return f"{designs} designs x ({inner})" + tail
        return " + ".join(str(count) for count in per_group) + tail

    def axis_counts(self) -> dict[str, int]:
        """轴名 -> 取值个数（界面上每一行右边那个 `-> N`）。

        口径跟着 **active group** 走（用的就是 `axis_selection()`）：选中一个组的时候，
        每根轴右边那个 `-> N` 说的必须是这个组实际会扫几个值，否则界面在说谎 -
        用户看着 `temperature -> 3` 却只跑出 1 个 run。active = base 时与以前逐字相同。
        """
        counts = {name: len(values) for name, values in self.axis_selection().items()}
        counts["freq"] = 1
        counts["design"] = len(self._designs)
        return counts

    def batch_dir(self) -> str:
        """批次落在哪。还没 plan 过就用当前的 root + name 现拼一个预览。"""
        if self._viewed is not None:
            # 看某一批时，"批次目录"就是**那一批**的目录 —— 动作栏那行路径、
            # Open batch dir、resume 全指它。否则点开历史里的一批，
            # 界面显示的却是"下一批会落在哪"，而两者长得一模一样。
            return self._viewed.batch_dir
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

    def run_log_files(self, run_id: str) -> tuple[str, ...]:
        """这个 run 现在磁盘上有哪些日志可以看，按权威性排序（`ewave.log` 在最前）。

        **空元组是正常状态**，不是错误：作业还在队列里排着的时候，一个日志文件都还没有。
        界面据此显示「还没有日志」而不是报错。

        坐标的两个来源，按新鲜度：跑这一批时 `self._plans` 里有现成的 `CommandPlan`；
        从历史里点开一批看时它是空的 —— 那时靠 `Run.work_dir`（driver 填的）和
        `Job.stdout_path`（提交时记的那份 `-o` 落点）。**两条都要有**，
        否则"看历史批次的日志"这条路会静默地什么都不显示。
        """
        run = self.run(run_id)
        if run is None:
            return ()
        plan = self._plans.get(run_id)
        work_dir = (plan.work_dir if plan is not None else "") or run.work_dir
        log_path = (plan.log_path if plan is not None else "") or (
            run.job.stdout_path if run.job is not None else ""
        )
        ewave_dir = f"{work_dir}/{run.ewave_dir}" if work_dir and run.ewave_dir else ""
        try:
            return logparse_module.run_log_files(
                ewave_dir=ewave_dir, run_log=log_path, run_dir=work_dir
            )
        except OSError:  # pragma: no cover - 网络盘抽风；看日志失败不该让界面炸
            return ()

    def run_log_tail(self, path: str) -> str:
        """一份日志的末尾（`logparse.TAIL_BYTES` 字节）。读不动**不抛**，把原因当正文返回。

        这个方法会被 `Output log` 那扇窗按轮询间隔反复调用 —— 「文件还没生成」
        （作业在排队）是它最常见的返回，那是正常状态，不是异常。
        """
        if not path:
            return ""
        return logparse_module.read_log_tail(path)

    def plan_for(self, run_id: str) -> CommandPlan | None:
        return self._plans.get(run_id)

    def notes(self) -> tuple[str, ...]:
        """规划过程中的软失败（解析不了某个官方目录之类）。硬失败直接抛。"""
        return tuple(self._notes)

    def events(self) -> tuple[DriverEvent, ...]:
        """driver 播过的全部事件（状态栏显示最后一条，Log 窗口显示全部）。"""
        return tuple(self._events)

    def redaction_map(self) -> dict[str, str]:
        """站点身份 → 占位符。**只给「把日志 copy 出去」这条路用**（硬约束 1）。

        日志里那些命令逐字带着 library / cell / view / ptxt / PDK 路径 / 队列 / home
        路径 —— 全是红区标识符。而"拷出来贴给别人看"恰恰是 Log 窗口存在的理由，
        两件事只能靠一层替换调和：这张表把**已经解析出来的**坐标换成 `<lib1>` /
        `<ptxt>` / `<home>`，命令的结构、flag 名、数值一个字节都不动 ——
        贴出去的东西仍然能拿来 debug，贴出去的坐标是零。

        口径三条：

        * 只读**已经缓存**的 `SiteFacts`（`self._facts`）—— 本方法不许触发磁盘解析
          （它会被 Log 窗口在每次刷新时叫到）；
        * 只收长度 >= `MASK_MIN_CHARS` 的取值：更短的（`vdd`、`in` 之类）会误伤 flag
          名和普通英文，而误伤在这里是**静默改写命令**，比漏掉一条更难发现；
        * 宁可多换不可少换 —— 多换只是让日志难读一点，少换是把坐标发出去。

        ⚠️ **尽力而为，不是保证。** 它只认得"我们自己解析出来过"的那些串；用户手敲进
        Extra flags 的路径、第三方报错里带的路径，它不知道。要贴给外部的东西，
        贴之前自己再扫一眼。
        """
        pairs: list[tuple[str, str]] = []

        def add(value: object, placeholder: str) -> None:
            cleaned = str(value or "").strip()
            if len(cleaned) < MASK_MIN_CHARS:
                return
            pairs.append((cleaned, placeholder))
            # 同一条路径在同一份日志里会以**两种分隔符**出现：`os.path.join` 给的是
            # 本地分隔符，我们自己拼 `--workDir=` 时给的是正斜杠。只收一种 =
            # 另一种原样漏出去（2026-08-20 实测：`--gds=` 被换了，`--workDir=` 没换）。
            # Linux 上这两个变体相同，多收一条只是多一次没命中的 replace。
            for was, now in (("\\", "/"), ("/", "\\")):
                if was in cleaned:
                    pairs.append((cleaned.replace(was, now), placeholder))

        for index, design in enumerate(self._designs, start=1):
            add(design.library, f"<lib{index}>")
            add(design.cell, f"<cell{index}>")
            add(design.view, f"<view{index}>")
            add(design.official_run_dir, f"<offdir{index}>")
        add(self.official_run_dir, "<offdir>")
        add(self.batch_dir(), "<batchdir>")
        add(self.batch_root, "<batchroot>")
        for facts in self._facts.values():
            add(facts.official_run_dir, "<offdir>")
            add(facts.library, "<lib>")
            add(facts.top_cell, "<cell>")
            add(facts.view, "<view>")
            add(facts.layer_map, "<layermap>")
            add(facts.ptxt, "<ptxt>")
            add(facts.ptxt_dir, "<ptxtdir>")
            add(facts.pdk_root, "<pdkroot>")
            add(facts.key, "<key>")
            add(facts.ewave_bin, "<ewave>")
            add(facts.strmout_bin, "<strmout>")
            add(facts.dsub_account, "<account>")
            add(facts.dsub_queue, "<queue>")
            add(facts.dsub_resources, "<resources>")
            for pos, (_port_id, pin) in enumerate(facts.official_port_spec.mapping, start=1):
                add(pin, f"<pin{pos}>")
            for pos, pin in enumerate(facts.official_port_spec.signal_ports, start=1):
                add(pin, f"<signal{pos}>")
        env = self._env if self._env is not None else os.environ
        for name in ("HOME", "USER", "LOGNAME", "USERNAME"):
            add(env.get(name, ""), "<%s>" % name.lower())
        # 长的先换。`<home>/proj/<lib>` 这种嵌套下，短串先换会把长串切碎，
        # 剩下的半截照样是坐标（`/proj/x` 前面少了 home 也还是内网路径）。
        table: dict[str, str] = {}
        for value, placeholder in sorted(pairs, key=lambda item: -len(item[0])):
            table.setdefault(value, placeholder)
        return table

    def is_running(self) -> bool:
        return self._running

    def has_started(self) -> bool:
        """这个批次已经交给 driver 跑过（**dry-run 也算**）没有。

        只说明"有一份 driver 的结果摆在表上"，**不**说明"不许再动了" ——
        那件事问 `has_submitted()`。两者的分家见它的注释。
        """
        return self._driver is not None

    def has_submitted(self) -> bool:
        """这个批次**真的提交过**没有（dry-run 不算）。

        ⚠️ 这两个判据分家是 2026-08-20 用户报的那个坑修出来的：原来界面拿
        `has_started()` 当"不许再动了"的闸门，于是**一次 dry-run 就把整个界面锁死** ——
        Dry-run / Submit 变灰、`recompute()` 也不再重新展开矩阵。
        用户当时正是漏填了官方 run 目录，dry-run 全 failed，回头去填那一格
        **界面毫无反应**（矩阵冻住了），而唯一还亮着的按钮是 Resume。

        两者的成本根本不是一个量级：
        * **dry-run 不提交任何 job、不建目录**（`options.dry_run`），重跑一次的代价是零 ——
          它就是个预览，预览凭什么只能看一次；
        * **真提交**会建目录、占集群（一个 run 可能 10 核 100 GB 跑 35 分钟），
          再按一次 Submit 就是整批从头重跑。那个才值得拦。

        所以：拦 Submit / 冻矩阵一律问**本方法**；`has_started()` 只用来回答
        "表上这批数字是不是 driver 给的"。
        """
        return self._driver is not None and not self._dry_run_only

    def preflight(self, *, dry_run: bool = False) -> list[str]:
        """现在按下去能不能跑得成 —— 返回**挡路的原因**（空 list = 可以跑）。

        为什么要有这一步而不是"跑起来再说"：漏填官方 run 目录时，每一个 run 都会在
        拼命令那一步炸掉（`SiteFacts.ewave_bin is empty`），用户看到的是一表 failed
        加一句面向实现者的报错，而真正要做的只是把顶上那一格填了。
        与其让他从 6 条一模一样的失败里反推，不如在按下去的那一刻就说清楚。

        文案是**英文 + 纯 ASCII**（红区 LANG 常是 C），而且每条都带"下一步做什么"。
        """
        problems: list[str] = []
        if not self._designs:
            problems.append("No design yet. Next: add one with 'Add row' under Designs.")
        offdir = self.official_run_dir.strip() or next(
            (d.official_run_dir.strip() for d in self._designs if d.official_run_dir.strip()), ""
        )
        if not offdir:
            problems.append(
                "Official run dir is empty, so the site coordinates (ewave path, ports, "
                "ptxt, queue) are unknown and no command can be assembled." + _NL +
                "  Next: fill in 'Official run dir' at the top, or use 'Browse...' to pick "
                "the design dir the official GUI already ran once (it contains gdsout_setup)."
            )
        if not dry_run:
            # ★ 落点这两条只在**真提交**时问：dry-run 一个字节都不写，
            #   拿"这儿已经有一批了"去拦一次预览是假的，而实写探针还会**建出**
            #   batch root —— dry-run 不许有落盘副作用。
            landing = self.batch_root_check()
            if landing:
                problems.append(landing)
            occupied = self.existing_batch_at_landing()
            if occupied:
                problems.append(
                    "There is already a batch at %s." % self.next_batch_dir() + _NL +
                    "  Submitting would overwrite its batch.json and drop new results on "
                    "top of the old ones - the same silent overwrite this tool exists to "
                    "prevent, one level up." + _NL +
                    "  Next: File -> New batch (it picks a fresh name), or Batch -> "
                    "Rename... to name this one yourself. To finish the existing batch "
                    "instead, use Runs -> Re-run failed only."
                )
        landing = self.batch_root_check()
        if landing:
            problems.append(landing)
        duplicates = self._duplicate_designs()
        if duplicates:
            # ★ 这一条必须排在下面那条**前面**，而且排掉它。
            #   两行一模一样的 design 会让每个 run 撞 run_id => 整个矩阵展不开
            #   => run_count() 是 0 => 用户会读到"tick at least one value on every
            #   axis"，可每根轴都勾着，真凶在 Designs 表里。一句指错方向的
            #   "下一步"比没有"下一步"更贵。按一次 Duplicate row 就到这儿，
            #   而"复制一行再改 cell 名"正是那个按钮的预期用法。
            problems.append(
                "Two design rows are identical: %s. Every run of the second one would "
                "land in the same directory as the first, so the whole matrix is refused."
                % ", ".join(duplicates) + _NL +
                "  Next: double-click one of them to change the cell (or 'Remove row')."
            )
        elif self._designs and not self.run_count():
            # ★ 展不开的**理由**要原样端出来，不许换成一句猜的。
            #   原来这里恒定写 "tick at least one value on every axis"，而
            #   2026-08-20 写反向测试时才发现：取值为空的轴根本不进笛卡尔积
            #   （`_base_axes` 直接跳过它）=> 那句话描述的情形**一次都不会发生**，
            #   它对每一条真实的出错路径都是错的建议。真实的路径是扫频那种
            #   "选了 step 但格子是空的"，而 `_expansion()` 把那条异常吞掉了。
            reason = self.expansion_error()
            problems.append(
                "The current settings expand to 0 runs." + _NL + "  " + reason
                if reason
                else "The current settings expand to 0 runs." + _NL +
                "  Next: check the axes and the frequency sweep - something in them "
                "cannot be turned into a run."
            )
        return problems

    def expansion_error(self) -> str:
        """展不开的话，**为什么**。展得开 -> 空串。

        `_expansion()` 有意吞掉这条异常（展不开在界面上是常态，边勾边看的时候
        不该弹东西）。但按下 Dry-run / Submit 的那一刻用户要的就是这句话，
        所以这里把同一条路再走一遍、只把消息取出来。多算一次的代价只在按键那一下。
        """
        try:
            axes, groups = self._axes_and_groups()
            matrix_module.expand_runs_detailed(
                self._designs, axes, options=self._options, groups=groups
            )
        except EwaveBatchError as exc:
            return str(exc)
        return ""

    def _duplicate_designs(self) -> list[str]:
        """出现了不止一次的 design（按 `matrix.design_key` 的口径），保序去重。

        口径必须是 `design_key` 而不是 `(library, cell, view)` 的字面量比较 ——
        run_id 是拿 `design_key` 拼的，判"会不会撞"就得用同一把尺子。
        """
        seen: dict[str, int] = {}
        for design in self._designs:
            key = matrix_module.design_key(design)
            seen[key] = seen.get(key, 0) + 1
        return [key for key, count in seen.items() if count > 1]

    def reset(self) -> None:
        """New batch：丢掉上一次的 driver / 状态 / 事件，**保留勾选**，**换一个身份**。

        ★ 2026-08-24 修的 bug：原来这里不动 `batch_name`，而名字就是目录名 ⇒
        「跑完 -> New batch -> 再跑」写进的是**同一个** `batch_dir`，上一批的
        `batch.json` 被盖掉、run_id 撞上的产物直接落在旧结果上。
        连"留空 = 用时间戳现起一个"这条路也一样：名字在第一次 `plan()` 之后就被烤进
        `batch_name` 了，`reset()` 不清它就永远是第一次那个时间戳。

        这正是本工具存在的理由（`<corner>_<temp>/` 静默覆盖，见 CLAUDE.md 三行心智模型）
        在**批次这一层**原样重造了一遍 —— 而且更贵：那一层盖的是一个 run，这一层盖的是一批。

        用户要的模型是 Cadence ADE 那个：**跑一次就是一次新结果，旧的还在**
        （2026-08-24 原话）。所以：

        * 名字是自动起的 -> 清空，下一次 `plan()` 现起一个新时间戳；
        * 名字是用户手打的 -> 保住那个词根，往后找第一个没被占的 `-2` / `-3`…
          （不是拒绝：New batch 是个明确的"我要重来一批"动作，在这儿弹框问名字
          等于让人给每一批想名字。改名的路照旧在 Batch -> Rename。）

        两条都会改 `batch_name`，所以**调用方必须把界面那一格重灌一次** ——
        `_ui.push()` 每一拍把那一格写回 bridge，不灌就是把旧名字又推了回来。
        """
        self._mint_fresh_identity()
        self._viewed = None
        self._viewed_contexts = {}
        self._dry_run_only = True
        self._driver = None
        self._result_state = None
        self._state = None
        self._contexts = {}
        self._plans = {}
        self._plan_errors = {}
        self._events = []
        self._running = False
        self._dirty = True

    def is_planned(self) -> bool:
        return self._state is not None and not self._dirty

    def result_is_current(self) -> bool:
        """driver 摆在表上的那份结果，说的还是**现在这份矩阵**吗。

        dry-run 之后界面是**不锁**的（那正是 2026-08-20 修的那条：dry-run 不写盘、
        不提交，重按一次代价是零，所以不许把界面冻住）。于是"跑完 dry-run 再改一个
        勾选"是完全正常的一步，而那一步之后 `recompute()` 会 `plan()` 出一份新的
        `BatchState` —— 上一次的结论从这一刻起说的是**别的**东西。

        没有这道门的症状不是报错，是**两个都对的数字互相打架**：表格来自新矩阵
        （`runs()` 读 `_state`），计数来自旧 driver（`summary()` 读 `_driver`），
        于是 5 行的表配一句"3 runs"。真提交过的批次到不了这里 —— 那时
        `recompute()` 根本不会重新 plan（`has_submitted()` 那道闸门）。
        """
        return self._driver is not None and self._state is self._result_state

    def dry_run_result(self) -> tuple[int, int] | None:
        """上一次 dry-run 的结果 `(拼出来了几条命令, 拼不出来几条)`。不是 dry-run 的结果 → None。

        ★ 为什么单独有这个方法：dry-run **一个 run 都不会变成 done** —— 它不提交、
        不建目录，全部 run 原地留在 `ready`。于是通用的那句
        「Finished - 0 / 3 done, 0 failed」逐字都对，读起来却是"什么都没发生"，
        而实际含义是"3 条命令全拼出来了，可以提交了"（2026-08-20 用户实测反馈：
        「点击 dry run 之后，我也不知道到底可以跑了不」）。

        判据只能是 `failed` 的条数：dry-run 里唯一会改状态的就是"这条命令拼不出来"
        （`Driver._plan_only` 把它置 FAILED）。`ready` 的那些 = 命令拼出来了。
        这与 CLI 那条路的口径逐字相同（`cli.py` 的
        `f"{len(state.runs)} runs planned, {built} commands built"`）。
        """
        if self._running or not self._dry_run_only or not self.result_is_current():
            return None
        counts = self.summary()
        total = sum(counts.values())
        if not total:
            return None
        failed = counts["failed"]
        return total - failed, failed

    def status_line(self) -> str:
        """状态栏那一行英文。"""
        counts = self.summary()
        total = sum(counts.values())
        if not total:
            return "New batch - nothing configured"
        if not self.result_is_current():
            # driver 没跑过，或者它那份结果已经过期（勾选改过了）—— 两种情况下
            # 屏幕上摆的都是"还没跑的预览"，说成 Finished 就是在说谎。
            return f"Preview up to date - {total} runs ready to submit"
        if self._running:
            # 🚨 `ready` 那些**必须**出现在这一行里。它们是"还没提交"（在等一个并发
            #    名额），而不是"没有"—— 漏掉它们时这句话加起来比表里的行数少，
            #    用户看到的就是"最后一个根本就没跑"（2026-08-20 实报）。
            waiting = counts["ready"]
            line = (
                f"Running - {counts['done']} done, {counts['running']} running, "
                f"{counts['pending']} pending, {counts['failed']} failed"
            )
            if waiting:
                line += (
                    f", {waiting} waiting for a free slot "
                    f"(max {self._options.max_parallel} in flight - raise it next to the "
                    "dsub command)"
                )
            return line
        result = self.dry_run_result()
        if result is not None:
            built, failed = result
            if failed:
                return (
                    f"Dry-run finished - {built} of {total} commands built, "
                    f"{failed} could not be built. Nothing was submitted; open Log for why."
                )
            return (
                f"Dry-run OK - all {total} commands built, 0 files written, nothing "
                "submitted. Press Submit to actually run them."
            )
        return f"Finished - {counts['done']} / {total} done, {counts['failed']} failed"

    # ------------------------------------------------------------ 内部
    def _find_group(self, name: str) -> RunGroup | None:
        for group in self._groups:
            if group.name == name:
                return group
        return None

    def _require_group(self, name: str) -> RunGroup:
        """按名字取组。`base` 也进不来 - 调用方必须先自己处理 base 那条分支。"""
        group = self._find_group(str(name).strip())
        if group is None:
            raise SpecError(
                f"There is no run group called {str(name)!r} in this batch.\n"
                f"  Groups: {', '.join(item.name for item in self.groups())}\n"
                "  Next: pick one of the names above, or add the group first"
            )
        return group

    def _active(self) -> RunGroup:
        return self._require_group(self.active_group())

    def _reject_reserved(self, name: str) -> None:
        """用户**显式**要了 `base` 这个名字 -> 拦。措辞与 `rename_group` 保持一份。"""
        if str(name).strip() == BASE_GROUP:
            raise SpecError(
                f"{BASE_GROUP!r} is a reserved group name (it means the top-level axes), "
                "so it cannot be reused.\n"
                "  Next: pick another name, e.g. eqcur-off"
            )

    def _unique_group_name(self, wanted: str) -> str:
        """想要的名字 -> 没被占用的名字。`base` 是保留名，也算被占用。"""
        stem = str(wanted).strip() or "group"
        taken = {BASE_GROUP} | {group.name for group in self._groups}
        if stem not in taken:
            return stem
        index = 2
        while f"{stem}-{index}" in taken:
            index += 1
        return f"{stem}-{index}"

    def _clean_values(self, name: str, values: Sequence[str]) -> tuple[str, ...]:
        """界面给的取值 -> 干净的取值（去空、去重，温度归一）。归一见 `normalize_temperature`。"""
        cleaned = [str(value).strip() for value in values if str(value).strip()]
        if name == "temperature":
            cleaned = [normalize_temperature(value) for value in cleaned]
        return _dedup(cleaned)

    def _set_base_values(self, name: str, values: Sequence[str]) -> None:
        """改 base 自己的勾选（加组之前 `set_axis_values` 的全部行为）。"""
        self._selection[str(name)] = self._clean_values(str(name), values)
        self._invalidate()

    def _set_group_values(self, group: str, name: str, values: Sequence[str]) -> None:
        """改某个组在某根轴上的覆盖。空取值 = 撤销覆盖（回去继承 base）。"""
        target = self._require_group(group)
        axis_name = str(name)
        cleaned = self._clean_values(axis_name, values)
        if not cleaned:
            if target.axis_overrides.pop(axis_name, None) is not None:
                self._invalidate()
            return
        base_axes = {axis.name: axis for axis in self._base_axes()}
        axis = base_axes.get(axis_name)
        if axis is None:
            raise SpecError(
                f"Run group {target.name!r} cannot override axis {axis_name!r}: that axis has "
                "no values selected in the base group, so it is not part of this batch.\n"
                f"  Axes in this batch: {', '.join(sorted(base_axes)) or '(none at all)'}\n"
                "  Next: select at least one value for it in the base group first "
                "(a group only lists the axes it changes, everything else is inherited)"
            )
        # 当场校验一遍取值：留到 plan() 才炸的话，用户已经忘了自己刚点了什么，
        # 而这里能说清"这根轴的合法取值是哪几个"。
        self._axis_with_gui_values(axis_name, tuple(av.value for av in axis.values) + cleaned)
        target.axis_overrides[axis_name] = cleaned
        self._invalidate()

    def _invalidate(self) -> None:
        """勾选变了 ⇒ 上一次 plan 的结果作废。**正在跑的批次不动**（改设定不该动它）。"""
        if not self._running:
            self._dirty = True

    def _record_event(self, event: DriverEvent) -> None:
        self._events.append(event)
        if self._on_event is not None:
            self._on_event(event)

    def _build_axes(self) -> list[Axis]:
        """全批次口径的轴清单（`axes()` / `_spec_snapshot()` 用的就是它）。

        没有组时逐字等于 `_base_axes()`。有组、且某个组的取值这根轴**表达不出来**时
        才会加宽（见 `_axes_and_groups`），加宽的代价由一条显式的 base 组覆盖抵消。
        """
        return self._axes_and_groups()[0]

    def _axes_and_groups(self) -> tuple[list[Axis], list[RunGroup]]:
        """一次算出「传给 `expand_runs` 的轴」和「传给它的组」。两样必须配套算出来。

        为什么会有"加宽"这一步：`matrix.axes_for_group` 靠 `axis_with_values` 把组写的
        取值套到轴上，而它只在**能安全翻译**的时候才现造 `AxisValue`（开关轴 on/off
        写法不同 => 拒绝而不是猜）。界面自己造的 mesh / freq 轴的取值带的是**具体**
        flag（`-e 0.4/-d 0.5`、`--multiSweep=adaptive,0:0.1:40`），没有 `{value}` 占位符
        => 一个组想换一个 mesh 写法时核心翻译不出来。
        解法是把那几个取值先并进轴的取值表（用界面自己的构造器算好 flag），再用一条
        **显式的 base 组**把 base 自己那份取值锁回去 - 否则加宽会让基线也跟着多扫几个 run。
        `matrix._all_groups` 明确支持调用方自己塞一个 `base` 组（会被挪到最前）。

        ⚠️ 锁回去的**不只是 base**：加宽是"改轴的定义"，而轴的定义对**每一个**组都生效。
        只锁 base 的话，任何一个没碰这根轴的**兄弟组**都会继承加宽后的取值表，
        于是它替别的组要的那个取值也扫一遍 —— 2026-08-19 复核实测：base 3 个 run，
        `mesh-var` 组要 mesh 0.45，另一个只改 equalCurrent 的组就从 1 个 run 变成 2 个
        （多出来的那条 slug 里带着 `mesh-0_45`，而它根本没要过 0.45）。
        一个 run 是 10 核 / 100 GB / 35 分钟，多扫一条不是显示问题。
        所以下面对**所有**组补一层"这根轴按 base 来"的覆盖，只有自己写过这根轴的组豁免。
        """
        axes = self._base_axes()
        groups = self._groups_for_expand(axes)
        if not groups:
            # 一个组都没有 => 与加组之前**逐字相同**（这条是最大的回归风险，有测试守着）。
            return axes, []
        widened: list[Axis] = []
        base_overrides: dict[str, tuple[str, ...]] = {}
        for axis in axes:
            extra = self._values_axis_cannot_express(axis, groups)
            if not extra:
                widened.append(axis)
                continue
            base_values = tuple(av.value for av in axis.values)
            try:
                widened.append(self._axis_with_gui_values(axis.name, base_values + extra))
            except EwaveBatchError as exc:
                # 加宽本身失败（组里写了一个这根轴根本不认识的取值）=> 保持窄的那份，
                # 让 `expand_runs` 去报那条更具体的错。这里不许静默丢掉用户的设定。
                self._note(f"run group value not usable on axis {axis.name!r}: {exc}")
                widened.append(axis)
                continue
            base_overrides[axis.name] = base_values
        if base_overrides:
            # 兄弟组：没写过这根轴的，一律钉回 base 的取值（继承的语义就是"跟 base 一样"，
            # 而加宽之后"轴的默认取值"已经不是 base 那份了）。
            for group in groups:
                for name, base_values in base_overrides.items():
                    group.axis_overrides.setdefault(name, base_values)
            groups.insert(0, RunGroup(name=BASE_GROUP, axis_overrides=dict(base_overrides)))
        return widened, groups

    def _groups_for_expand(self, axes: Sequence[Axis]) -> list[RunGroup]:
        """界面上的组 -> 能交给核心的组（拷贝，核心改不到界面状态）。

        丢掉两种东西：**一根轴都没覆盖的组**（它展开出来就是 base，全被去重吃掉，
        而 `core.spec` 会拒绝把这种组写进 spec 文件，留着就存不下来）；
        **界面表达不出来的轴上的覆盖**（记一条 note，不静默）。
        """
        known = {axis.name for axis in axes}
        out: list[RunGroup] = []
        for group in self._groups:
            overrides = {
                name: tuple(values)
                for name, values in group.axis_overrides.items()
                if name in known
            }
            dropped = sorted(name for name in group.axis_overrides if name not in known)
            if dropped:
                self._note(
                    f"run group {group.name!r}: ignored override(s) for {', '.join(dropped)} "
                    "- that axis has no values selected in the base group"
                )
            if not overrides:
                continue
            out.append(
                RunGroup(name=group.name, axis_overrides=overrides, label=group.label)
            )
        return out

    def _values_axis_cannot_express(
        self, axis: Axis, groups: Sequence[RunGroup]
    ) -> tuple[str, ...]:
        """这些组在这根轴上写了、而 `matrix.axis_with_values` 翻译不出来的取值。"""
        extra: list[str] = []
        for group in groups:
            for value in group.axis_overrides.get(axis.name, ()):
                text = str(value)
                if text in extra:
                    continue
                try:
                    matrix_module.axis_with_values(axis, [text])
                except EwaveBatchError:
                    extra.append(text)
        return tuple(extra)

    def _axis_with_gui_values(self, name: str, values: Sequence[str]) -> Axis:
        """轴名 + 取值 -> `Axis`，走**界面自己的**构造器（flag 由它们算，只有一份实现）。"""
        if name == "mesh":
            return mesh_axis(values)
        if name == "freq":
            return sweep_axis(self._sweep.get("mode", "adaptive"), values)
        return axis_from_catalog(name, values)

    def _expansion(self) -> RunExpansion | None:
        """现在这套勾选展开出什么（run 数 / 每组几个 / 折叠了几个）。展不开 -> None。

        展不开在界面上是**常态**（一个 design 都没勾就是空矩阵），所以这里吞异常；
        真正会炸的地方是 `plan()`，那条路上错误有地方显示。

        ⚠️ `_axes_and_groups()` **必须在 try 里面**：它自己就会抛（扫频选了 `step`
        但格子是空的 → `sweep_axis_value` 抛 `SpecError`）。它原来在外面，于是
        「展不开 -> None」这句承诺在最常见的那条出错路径上是假的，异常一路穿过
        `group_run_counts()` / `merged_run_count()` / `run_count()` / `formula()`
        打到界面上 —— 2026-08-20 实测后果：Run groups 那张表在 `refresh_groups()`
        取计数那一步就抛了，**一行都没重画**，于是刚删掉的组还赖在表上（点它 →
        "There is no run group called ..."），删了也像没删。
        """
        try:
            axes, groups = self._axes_and_groups()
            return matrix_module.expand_runs_detailed(
                self._designs, axes, options=self._options, groups=groups
            )
        except EwaveBatchError:
            return None

    def _base_axes(self) -> list[Axis]:
        """base 组的轴清单。取值为空的轴直接不出现（笛卡尔积会塌成空集）。

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
                        self._sweep.get("spacing", ""),
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
        """当前界面状态 -> 一份 `BatchSpec`（`plan()` 和「Save spec」共用这一条路）。

        组也要进去，两个理由：`plan()` 靠 `spec_to_batch` 把它们传给 `expand_runs`
        （不传的话组只存不跑）；「Save spec as...」靠它把界面上配的组写回文件
        （不传的话存下来的 spec 与界面显示的不是一回事）。
        """
        axes, groups = self._axes_and_groups()
        return BatchSpec(
            batch_name=self.batch_name,
            batch_root=self.batch_root,
            designs=list(self._designs),
            axes=axes,
            groups=groups,
            defaults=dict(self._default_overrides),
            extra_flags=parse_extra_flags(self._extra_text),
            options=self._options,
            source_path=self._spec.source_path if self._spec is not None else "",
            source_sha256=self._spec.source_sha256 if self._spec is not None else "",
        )

    def spec_snapshot(self) -> BatchSpec:
        """公开版本 —— 「Save」按钮把它写出去（写盘由调用方做）。"""
        return self._spec_snapshot()

    # ==================================================== 上次那份设定（自动存 / 自动读）
    # ★ 用户 2026-08-24：「load 过一次，相关的设置就保存在本地，下次启动不用再 load」。
    #
    #   ⚠️ 存的是**设定**（designs / 轴 / 组 / 官方目录**路径** / 落点），
    #      **绝不是**从官方目录解析出来的坐标。这条界线不能含糊：
    #
    #      站点级那批（ptxt 路径、PDK 根、key、Donau 三元组、工具路径、默认 flag 表）
    #      缓存了顶多是过期；但 **per-design 的端口表缓存不得** ——
    #      端口映射不在 `.sNp` 里，在命令行里，靠 `-p` 的顺序（CLAUDE.md 三行心智模型）。
    #      设计师改一次版图加个端口，缓存里还是老表 ⇒ `-p` 错位 ⇒ **`.sNp` 每一位的
    #      含义都错了，而且跑得出来、数字也像**。那正是本工具要消灭的那类静默错误。
    #
    #      而解析一次只要约 10 ms、还按目录缓存在 `_facts` 里 —— 省下来的那点时间
    #      根本不值得拿它换。所以：**记住路径，内容每次现读。**

    def save_session(self, *, env: Mapping[str, str] | None = None) -> str:
        """把当前设定存成"上次那份"。返回写到的路径；存不下 → 空串（**不抛**）。

        存不下不是错误：装机目录只读、盘满、没权限 —— 任何一种都不该让界面弹框，
        更不该让人没法继续干活。代价只是下次开机要重新填一次，而那正是加这个功能
        之前的常态。

        批次名只在**用户手打过**的时候才存。自动名是个时间戳，存下来明天开机会变成
        `batch_20260824_032116-2` 这种词根 —— 那不是名字，是垃圾。
        """
        spec = self._spec_snapshot()
        if self._name_is_auto:
            spec = replace(spec, batch_name="")
        # 官方 run 目录：批次级那一格不在 `BatchSpec` 里（它是 per-design 字段），
        # 所以给没写自己那份的 design 补上 —— 否则读回来时顶上那格是空的，
        # 而"不用再 load"这件事恰恰全靠它。
        if self.official_run_dir:
            spec = replace(
                spec,
                designs=[
                    d if d.official_run_dir else replace(d, official_run_dir=self.official_run_dir)
                    for d in spec.designs
                ],
            )
        path = session_path(env if env is not None else self._env)
        try:
            return spec_module.save_spec(spec, path, as_json=True)
        except (OSError, EwaveBatchError):
            return ""

    def load_session(self, *, env: Mapping[str, str] | None = None) -> bool:
        """把"上次那份"读回来。读到了 → True；没有 / 读坏了 → False（**不抛**）。

        读坏了一律当没有：一份读不了的状态文件不该让 GUI 起不来，而"开局是空的"
        本来就是合法状态（同 `site.local.sh` 那条规矩）。

        ⚠️ **只在开局调**。中途调会把用户手上正在编辑的设定冲掉，
        而它看起来只是"界面刷新了一下"。
        """
        path = session_path(env if env is not None else self._env)
        if not os.path.isfile(path):
            return False
        try:
            self.load_spec(path)
        except (OSError, EwaveBatchError):
            return False
        # 这份是**状态**不是用户的工程文件：留着 `source_path` 会让
        # 「Save spec as...」把它当成"当前打开的那个 spec"，而它不是。
        if self._spec is not None:
            self._spec = replace(self._spec, source_path="", source_sha256="")
        return True

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
        # ⚠️ 模板里那条 `-R` 是**例子**，不许顶掉官方 run 目录里真的那条 ——
        #    只有用户真动过这个框，命令里的 `-R` 才算「用户说了算」。
        resources = "" if self._submit_is_template else self.resources()
        if resources:
            # 用户改过那条 dsub 命令 ⇒ 以命令里的 `-R` 为准（`--parallel` 跟着它走）。
            facts = replace(facts, dsub_resources=resources)
        elif self._submit_is_template and (
            facts.dsub_account or facts.dsub_queue or facts.dsub_resources
        ):
            # 第一次解析到站点的提交前缀 —— 它比模板准，整条顶掉，让用户接着改。
            # ⚠️ 判据是**三元组里任意一个**，不是只看 `-R`：站点那条 `remote_run_ewave.sh`
            #    只给 `-A`/`-q` 而不给 `-R` 是完全可能的（资源走队列默认），
            #    而那正是最该自动填上的两个 —— 账号和队列是用户手打最烦、也最容易打错的。
            #    只看 `-R` 的话，这种站点会一直停在占位符上，用户以为工具不认识他的集群。
            self.submit_command = _dsub_command_from(facts)
            self._submit_is_template = False
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

        # 占位符还没换掉 ⇒ **提交之前就拦住**。放过去的话 dsub 会逐个 job 拒掉
        # （账号/队列不存在），一次点击换来 N 条彼此无关、且离病根很远的失败。
        # dry-run 放行：它一个字节都不发出去，而「先看看命令长什么样」正是模板的用途。
        left = self.submit_command_placeholders()
        if left and not self._options.dry_run:
            raise EwaveBatchError(
                "the submit command still carries unreplaced placeholder(s) from the "
                "default template: %s - nothing was submitted.\n"
                "  Next: put the real Donau account / queue in the Donau submit box, or set "
                "the official run dir and let the whole dsub line be read from it."
                % " / ".join(left)
            )
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
        """spec 里的轴 → 界面勾选。**spec 没写的轴 = 空**，不是"保持界面上原来的值"。

        ★ 2026-08-24 修的一个静默翻倍。原来这里从 `dict(self._selection)` 起步，
        也就是把文件和界面**混合**起来 —— 而 `load_spec` 自己的契约写的是
        「用户按了 Load，界面就该显示文件里的东西，而不是两者的混合」。两处注释互相矛盾，
        对的是 `load_spec` 那句。

        症状：把 `fullWave` 取消勾选（于是它没有取值 ⇒ `_axes_and_groups` 不把它
        写进 spec ⇒ 文件里根本没有这根轴）⇒ Open 回来时它被填回内置默认的
        `off, on` 两个值 ⇒ **矩阵从 12 个 run 变成 24 个**，而界面上看起来只是
        "打开了刚存的那份"。一个 run 是 10 核 / 100 GB / 35 分钟，那是 12 个白跑的。

        存/读必须是**幂等**的：存一份、读回来、再存一份，两份文件要逐字相同。
        这一条在「上次那份设定」自动读进来之后更要紧 —— 它每次开机都跑一遍。

        「轴没有取值」的语义本来就是**不扫这根轴**（那个 flag 仍然取学到的默认值），
        不是"没配过、给他补一个"。补的那一下正是在替用户做他没做的决定。
        """
        selection = {name: () for name in self._selection}
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
                    sweep["spacing"] = "step"
                elif body.count("-") == 2:
                    # `adaptive,0-41-40` = 点数写法。以前这一支根本没解析，于是
                    # 「存了一份 points 写法的 spec、再 Open 回来」会静默退回上一次
                    # 界面里的 step 值 —— 文件说 41 点、界面说 0.1 步长。
                    # 频率不会是负数，所以按 `-` 拆是安全的。
                    sweep["start"], sweep["points"], sweep["stop"] = body.split("-")
                    sweep["step"] = ""
                    sweep["spacing"] = "points"
        return sweep


# --------------------------------------------------------------------------
# 私有小工具
# --------------------------------------------------------------------------


def _summary_fragment(axis: Axis, values: Sequence[str]) -> str:
    """一根轴 + 取值 -> 摘要里的一段（`"55.0"` / `"eqI off"`）。

    corner / temperature 不带前缀：它们进的是 eWave 自己那层 `<corner>_<temp>` 目录名，
    在摘要里也就是"位置就说明了它是什么"。其余轴带 `short`（`eqI` / `fw` / `mesh`），
    否则 `"off"` 一个词根本看不出说的是哪根轴。
    """
    body = "/".join(str(value) for value in values)
    if axis.encoded_in_ewave_dir:
        return body
    return f"{axis.short or axis.name} {body}"


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
