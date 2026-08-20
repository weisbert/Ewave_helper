# 冻结接口（Phase 0 产物）

写给 P1–P5 的每一个 agent 看。**你只能看这一页和 `ewave_batch/model.py` 来对接别人的模块** ——
并行写代码时，冻结面是唯一的共同事实。

* 权威是 `ewave_batch/model.py`（代码），本文是它的人类可读版本。两者不一致时以 model.py 为准，
  并且**说明本文该改了**。
* 机器判据：`python -m ewave_batch dry-run --self-test`（`scripts/check.sh` 第 4 步）。
  它逐个 import `model.FROZEN` 里的模块，核对符号存在性与签名。**有漂移退 1。**

---

## 目录布局

```
ewave_batch/                 ← 包根
  __init__.py  __main__.py   ← __main__ 只有 self-test，没有业务
  model.py                   ← ★ 冻结面：数据结构 + 全部跨模块签名 + FROZEN 清单
  redzone_dryrun.py          ← P2：红区那趟只读 dry-run（独立入口，不经 cli）
  cli.py                     ← P5
  core/{matrix,spec,cmd,layout,discover,logparse,template}.py
  tools/{strmout,ewave}.py
  sched/{fake,donau,driver}.py
cli.py                       ← 顶层薄壳，转发到 ewave_batch.cli
gui/                         ← 顶层
  __init__.py  app.py  state.py  frames/{__init__,stacked,tabbed,split}.py
tests/  docs/  deploy/  scripts/
```

⚠️ 全部 `__init__.py` **必须空或极薄，不许 import 子模块**。
CLAUDE.md 硬约束 5：GUI 的 import 必须惰性，无 `$DISPLAY` 的纯 ssh 会话里 CLI 要能用；
包根一旦 `from . import cli` 惰性就没了，而且这种破坏是静默的、只在红区发作。
`tests/test_interfaces.py::LazyImportDiscipline` 盯着这条。

`core/spec.py` 是本阶段**新增**的模块（BRIEF §5 的示意图里没有）：读用户手写 spec 的
YAML 惰性 import + JSON 退路 + 校验是一整块活，塞进 `matrix.py` 会让 P1 的两个 agent 打架。
**归属：P1 的 matrix agent。**

---

## 一句话职责

| 模块 | 干什么 | 阶段 |
|---|---|---|
| `core.matrix` | designs × 轴 → 笛卡尔积 → `list[Run]` + slug + varying 轴 | P1 |
| `core.spec` | 用户手写 spec（YAML/JSON）→ `BatchSpec` → `BatchState` | P1 |
| `core.cmd` | 四层合并 → `CommandPlan`；冲突检测；逐 flag diff | P1 |
| `core.layout` | 归档布局 / `cmd.sh` / 归档 / 验收 / state 原子读写 / `runs.csv` | P1 |
| `core.template` | 一条现成 ewave 命令行 → `ParsedCommand` | P1 |
| `core.discover` | 官方 run 目录 → `SiteFacts`（**站点坐标只在这条路上出现**） | P2 |
| `tools.strmout` | 渲染 `gdsout_setup` + 拼 strmout argv | P2 |
| `redzone_dryrun` | 红区只读 dry-run：解析真目录 → 打印全部命令 + 落地目录 → 自带比对 | P2 |
| `tools.ewave` | 拼 ewave argv（薄封装到 `core.cmd`） | P3 |
| `sched.fake` | 假 runner / 假调度器，模拟实测过的坑 | P3 |
| `sched.donau` | 真 `dsub`/`djob` | P3 |
| `sched.driver` | 两阶段 DAG + 有界并发 + poll + verify + archive + resume | P3 |
| `core.logparse` | 日志 → `LogFacts` | P4 |
| `cli` / `gui.*` | 界面（CLI 五个子命令 + 三版 tkinter 布局） | P5 |

---

## 数据流

```
                     spec.yaml（人写，可带注释）
                            │  core.spec.load_spec
                            ▼
        BatchSpec ──────────────────────────► core.spec.spec_to_batch ──► BatchState
            │                                                                  │
   官方 run 目录 (OFFDIR)                                                       │
            │  core.discover.discover_site_facts                                │
            ▼                                                                   │
        SiteFacts ─┐                                                            │
                   │   ┌──────────── core.matrix.expand_runs ◄──────────────────┘
   core.discover   │   │                    │
   .learn_default_flags │                   ▼
                   │   │                 list[Run]   （run_id / axes_slug / ewave_dir）
                   ▼   ▼                    │
                PlanContext ────────────────┤
                   │                        │
    阶段 1         │                        │  阶段 2
  tools.strmout    │                        │  core.cmd.build_flag_layers
  .render_gdsout_setup                      │        ↓ merge（内置<默认表<Extra<轴<机制）
  .build_strmout_plan                       │  core.cmd.build_command_plan
                   │                        │  tools.ewave.build_ewave_plan
                   ▼                        ▼
              CommandPlan               CommandPlan ──► core.layout.write_cmd_sh
                   │                        │
                   └────────► sched.driver.Driver.tick() ◄──── SchedulerProtocol
                                            │                  （fake / donau）
                                            ▼
                       core.layout.verify_run_outputs（存在+非空+端口数对）
                                            │
                                            ▼
                       core.layout.archive_run → sparam/ 扁平区
                       core.logparse.parse_run_logs → LogFacts
                       core.layout.write_batch_state（原子）+ write_runs_csv
```

**驱动方式只有一种**：`Driver.tick()` 非阻塞推进一拍。
CLI 用 `sched.driver.run_batch` 的 `while` 驱动它，GUI 用 tkinter 的 `after()` 驱动它 ——
**同一份 driver 代码**（§12 实现决定：单线程轮询，无锁，resume 天然）。

---

## 六条会咬人的契约

1. **`FlagValue` 的 `False` 不是"没有"，是"显式缺席"。**
   生产默认表里带 `--equalCurrent`，equalCurrent 轴的 `off` 取值必须用 `False`
   把它抵消掉，否则目录名说 off、命令行说 on —— 那正是我们要消灭的坑。
   `render_flags` 不渲染 `False`，`diff_flags` 把它与"对面没这个键"算一致。
2. **合并顺序 `内置默认 < 默认表 < Extra flags < 轴 < 机制`。**
   轴永远赢用户层（轴是 run 的身份、会进目录名），机制层最后落笔（改了工具就废了）。
3. **corner 轴同时改两个 flag**：`--corner=` 和 `--emssTechFile=` 的 ptxt 文件名（BRIEF §7）。
   所以 `Axis.flags` 是元组，`resolve_axis_flags` 负责把 `{ptxt}` 占位符换成
   `discover.ptxt_path_for_corner(facts, value)`。少改一个就是"目录名说 typical、
   实际用了别的工艺角"，而且跑得出来、数字也像。
4. **`diff_flags(ignore=…)` 按 flag 名精确匹配，绝不做前缀匹配。**
   MVP 踩过：`--sparam` 前缀误伤 `--sparamImpedance`，两边同时被跳过，diff 空得非常好看
   但根本没比。所以 `FlagDiff` 带 `compared_count` 和 `ignored` —— **测试必须断言
   `compared_count` 等于从 fixture 数出来的条数**（防自证配方 3）。
5. **`done` 的判据是 `verify_run_outputs`，不是 job 退出码。**
   实测：eWave 崩了也 `exit=0`、还会留 0 字节文件报 "done"、写失败被吞。
   `Job.exit_code` 只作诊断。
6. **`varying` 的口径是全批次的 —— run group 的取值必须并进去。**
   `compute_axes_slug` 只编码「在变的轴」。base 里 `equalCurrent: [on]` 不变 ⇒ 不进 slug；
   一旦有个组把它覆盖成 `[off]`，它就**对所有 run**（包括基线）进 slug。
   漏算这一层的后果是两个组的同一个 corner/temp 算出同一个 `run_id` ⇒ 同一个 `--workDir`
   ⇒ **静默覆盖**，正是本工具存在要消灭的东西。
   推论（界面上必须让人看见）：**加组会改掉基线的目录名**，所以给已经跑过的批次加组
   等于换了一批 `run_id`，resume 认不出老的 `base/...` 目录。

---

## run group：批次 = 一列组，run 取并集（用户 2026-08-19 拍板）

今天之前，界面上唯一能表达的是「我勾的所有东西的所有组合」（一个全批次笛卡尔积）。
用户真正要的是**一条基线 + 几个单点变体**：

```
typical @ {-40, 55, 125}  +  typical @ 55 且 equalCurrent off  +  typical @ 55 且 full wave  = 5 个 run
```

笛卡尔积最接近的写法是 `{typical}×{3 温度}×{eqI on/off}×{fw on/off}` = 12 个，7 个是废的。
按 BRIEF §12 的量级（一个 run 可能 10 核 100GB 跑 35 分钟）这是硬成本。
（`PROJECT_BRIEF.md` 给 `matrix.py` 的职责本来就写着「笛卡尔积/**显式组合**」—— 这半边当初
设计到了、没实现。）

**模型**：批次 = 一列 `RunGroup`，每组自己取笛卡尔积，结果取并集。
**组是 base 之上的 delta**，只列它覆盖的轴，其余继承 base：

```yaml
axes:                      # base 组。不写 groups: 时行为与以前逐字相同
  corner: [typical]
  temperature: ["-40.0", "55.0", "125.0"]
  fullWave: [off]
  equalCurrent: [on]

groups:
  - name: eqcur-off
    axes: {temperature: ["55.0"], equalCurrent: [off]}
  - name: fullwave
    axes: {temperature: ["55.0"], fullWave: [on]}
```

为什么是这个形状：合并操作**代码早就有了且有测试**（`axes_for_design`），
`axes_for_group` 是同一个函数换个主人，不引入任何新语义；而且它写得进 spec 文件、
能 diff、能带注释，base 有多少根轴一个组永远只占两行。

四条必须记住的语义：

| | |
|---|---|
| base 组永远存在 | `BatchSpec.groups` / `BatchState.groups` 存的是 **base 之外**的那些组。base 的轴就是顶层 `axes:`，重复存一份必然漂移。`matrix._all_groups` 负责把 base 补在最前 |
| slug 口径是全批次的 | 见上面第 6 条契约。`effective_axis_values` 在调用方没给 base 组时会自己补上那一层 |
| 跨组重复静默去重 | 两个组都写了 55 度是很自然的写法。保留**第一个**（base 排最前 ⇒ 基线归 base），合并数从 `RunExpansion.merged` 拿。**同一个组内部**撞 `run_id` 仍然抛 `SpecError`（那是真 bug） |
| `Run.group` 只是出身标签 | **不进 `run_id`**。进了就等于把去重取消掉：两个组算出同一组轴取值时会变成两个目录 |

`spec.py` 侧：顶层 `groups:` 是一个 **list**（组是有序的，顺序决定谁被留下），
每项 `{name, axes: {轴名: [取值…]}, label?}`。组名唯一且非空；`base` 是保留名 ——
用户写 `name: base` 就是显式指 base 组，它的覆盖**合并进顶层 axes** 而不是新建一个组。
`spec_to_mapping` / `save_spec` 会把 groups 原样写回去（GUI 的「Save spec as…」靠它）。

轴还有一个可选键 **`value_flags:`**（`{取值: {flag: 值}}`）：只在「按取值名重新翻译会得到
**不同的** flag」时才由 `spec_to_mapping` 写出来，人手写 spec 基本用不到。存在的理由是
界面自造的两根轴 —— 三段网格 `0.4/0.5/0.4`（`-e`/`-d`/`--viaMergeSpace` 三个值互不相同）
和频率扫描（`--multiSweep=<串>` 外加两个 `False` 抵消互斥写法）—— 光靠取值字符串
**翻不回来**。不写的话存盘再打开得到的是另一组 flag，而 `axes_slug` 一个字都不变
⇒ 归档里那份结果声称自己跑的是它根本没跑的设定（2026-08-19 实测）。
判据是**语义**相等（`{value}` 占位符先代入再比），不是逐字相等 —— 逐字比会让每根界面造的轴
都写 `value_flags`，而写了它的轴就不能再现造新取值了。

`core.layout.state_to_dict` / `state_from_dict` **已经**序列化 `BatchState.groups`
和 `Run.group`（2026-08-19 补上，缺口关闭）。两个字段都按**可选**读，
`SCHEMA_VERSION` **没有** +1：老的 `batch.json` 里没有这两个键，读回来落
「整批只有 base」，与加组之前逐字相同（双向兼容，判断依据写在 `model.SCHEMA_VERSION`
的 docstring 里）。`runs.csv` 也**追加**了最后一列 `group` —— 追加在末尾，
按列名读的下游一个字都不用改。

⚠️ **`BatchState.axes` 存的是「全批次并集」，不是顶层 `axes:` 那份。**
`PlanContext.axes` 就是从这里来的，而 `core.cmd.build_flag_layers` 拿
`run.axis_values[轴名]` 去**轴的取值表里查** flag。只存 base 那份的话，任何一个组独有的
取值（`equalCurrent: [off]`）都查不到 ⇒ `resolve_axis_flags` 抛
"axis 'equalCurrent' has no value 'off'"，CLI / GUI / 红区 dry-run 三条路一起废。
所以 `spec_to_batch` 存进去之前先过一遍 `matrix._batch_axes`（= 每根轴取
`effective_axis_values` 的并集）。**展开**用的仍然是 base 轴 + `groups`，两者不能混：
拿并集去展开就等于让基线也扫一遍别的组的取值。

---

## 全部冻结签名

下面是从 `model.py` 直接生成的。**实现时把签名逐字抄过去**（连返回注解一起），
self-test 比对的是参数名 + 顺序 + 参数种类 + 有没有默认值 + 返回注解字符串。
参数注解和默认值的具体对象**不比** —— 那两样假阳性太多。

数据结构（`Design` / `Axis` / `Run` / `BatchState` / `SiteFacts` / …）不在下面重复，
直接读 `ewave_batch/model.py`：那里每个字段都有注释，注释里往往写着"为什么是这个形状"。

### `ewave_batch.core.matrix` — P1

展开笛卡尔积 → `list[Run]`；算 slug；算「哪些轴在变」

```python
def design_key(design: Design) -> str:
    # 算一个 design 的稳定 id（`design.key` 非空时直接返回它）。
def slugify(text: str) -> str:
    # 把任意取值变成能当目录名的片段。
def ewave_dir_name(corner: str, temperature: str) -> str:
    # 拼 eWave 自己会建的那层目录名：`<corner>_<temp 的小数点换下划线>`。
def varying_axes(axes: Sequence[Axis], *, designs: Sequence[Design] = (), groups: Sequence[RunGroup] = ()) -> list[Axis]:
    # 挑出**真正在变**的轴（取值 > 1 个的）。口径必须是**全批次**：designs 的覆盖和
    # groups 的覆盖都要并进来，漏一层就会让某根在变的轴不进 slug ⇒ 两个 run 撞 run_id。
def compute_axes_slug(axis_values: Mapping[str, str], axes: Sequence[Axis]) -> str:
    # 算 `<axes-slug>` —— 只含 `encoded_in_ewave_dir=False` 且**在变**的轴。
def axes_for_design(design: Design, axes: Sequence[Axis]) -> list[Axis]:
    # 把 `design.axis_overrides` 套到全局轴定义上，返回这个 design 实际要扫的轴。
def axes_for_group(group: RunGroup, axes: Sequence[Axis]) -> list[Axis]:
    # 同构：把 `group.axis_overrides` 套到 base 轴上。未知轴名 → `SpecError`。
def builtin_axis_catalog() -> dict[str, Axis]:
    # 内置轴目录：轴名 → `Axis`（取值列表为**该轴的合法取值样例**，实际取值由 spec 给）。
def expand_runs(designs: Sequence[Design], axes: Sequence[Axis], *, options: BatchOptions | None = None, groups: Sequence[RunGroup] = ()) -> list[Run]:
    # 展开笛卡尔积 → `list[Run]`，每个 Run 带好 `run_id` / `axes_slug` / `ewave_dir`。
    # `groups=()` 与加这个参数之前**逐字相同**。
def expand_runs_detailed(designs: Sequence[Design], axes: Sequence[Axis], *, options: BatchOptions | None = None, groups: Sequence[RunGroup] = ()) -> RunExpansion:
    # 同上，外加「跨组折叠了几个」`merged` 和每组各贡献几个 `per_group`。
    # `expand_runs` 是它的薄封装。界面显示 "5 runs (1 duplicate merged)" 靠它。
    # 展开顺序：**组在最外**，然后 design，最后轴。组排最外是因为「跨组重复留第一个」
    # 要求 base 组必须是那个第一个 —— 基线归 base，才对得上「加组不改变基线归属」。
def axis_with_values(axis: Axis, values: Sequence[str]) -> Axis:
    # 把一根轴的取值换成 `values`，其余定义（flag / kind / slug 模板）原样保留。
    # `core.spec` 和 `axes_for_group` 都靠它把「spec/组里写的取值」套到内置轴上。
    # ⚠️ 取值不在轴的取值表里时**只有**在这根轴每个取值的 flag 写法完全一致
    # （都是 `{"--temperature": "{value}"}` 这种带占位符的模板）时才敢现造 —— 造不出来就报错。
    # 后果见「还没冻结的东西」里 `gui.state` 那条：界面自造的 mesh 轴取值带的是具体 flag、
    # 没有占位符 ⇒ 组换一个 mesh 写法翻不出来，得由调用方自己把取值表加宽。
def effective_axis_values(axis: Axis, designs: Sequence[Design] = (), groups: Sequence[RunGroup] = ()) -> list[str]:
    # 这根轴在**整个批次**上实际会取到的取值（去重、保序）—— `varying_axes` 的口径就是它。
    # designs 与 groups 的覆盖都要并进来（第 6 条契约）。
    # ⚠️ 调用方没在 `groups` 里给 base 组时，它**自己补上那一层** —— 漏了 base 的取值
    # 就会把"只有某个组覆盖过的轴"误判成不在变，于是静默覆盖。
```

### `ewave_batch.core.spec` — P1

读用户手写的 spec（**唯一允许惰性 import PyYAML 的地方**，带 JSON 退路）

```python
def load_spec(path: str) -> BatchSpec:
    # 读 YAML（或 JSON）spec。
def parse_spec_mapping(data: Mapping[str, object], *, source: str = ) -> BatchSpec:
    # 把已经解析成 dict 的 spec 变成 `BatchSpec`（YAML/JSON 共用这一条路）。
def spec_sha256(path: str) -> str:
    # spec 文件的 sha256（十六进制小写）。进 `Provenance.spec_sha256`，
def spec_to_batch(spec: BatchSpec, *, batch_root: str, tool_version: str = ) -> BatchState:
    # `BatchSpec` → 全新的 `BatchState`（run 全是 `READY`，还没建目录）。
    # groups 走两条路：传给 `expand_runs`（所以组会真的展开成 run），
    # 并原样存进 `BatchState.groups`（所以 resume / 存盘看得见批次当初是怎么组的）。
def spec_to_mapping(spec: BatchSpec) -> dict:
    # `parse_spec_mapping` 的反函数，GUI「Save spec as…」的落笔处。
    # **只写非默认字段**（spec 是给人读、给人改的）；往返必须是不动点。
    # 顶层 `groups:` 在这里原样写回去 —— 没有它，界面上配好的组一存盘就没了。
def dump_spec(spec: BatchSpec, *, as_json: bool | None = None) -> str:
    # `BatchSpec` → 写进文件的文本。有 PyYAML 出 YAML，没有出 JSON（字段名完全一样）。
def save_spec(spec: BatchSpec, path: str) -> str:
    # 原子写（同目录临时文件 + `os.replace`），**返回真正写到的路径** ——
    # `load_spec` 按扩展名挑解析器，没有 PyYAML 时内容是 JSON，扩展名必须跟着改，
    # 否则"自己写的文件自己打不开"。
def have_yaml() -> bool:
    # 这台机器能不能写/读 YAML。惰性探测，不在 import 时就依赖 PyYAML。
EXAMPLE_SPEC   # 见 model.py / 下表；与 `docs/spec_example.yaml` **逐字相同**，测试盯着这条
```

顶层 `groups:` 是一个 **list**（组有序，顺序决定跨组重复时谁被留下 —— 而 YAML 的 mapping
在旧解析器上不保证顺序，写成 mapping 的话去重结果会跟着抖）。
每项恰好三个键：`{name, axes, label}`，**多一个键就报错** ——
这条顺带把「组不许覆盖 defaults / extra_flags」钉死在解析层（理由见 BRIEF §11）。

四种情况抛 `SpecError`：

| 情况 | 为什么不能放过 |
|---|---|
| 组名为空 | 组名会出现在 Runs 表和每一条关于这个组的消息里 |
| 组名重复 | 后一个会静默盖掉前一个 |
| `axes:` 里出现未知轴名 | 消息里列出这个批次到底有哪些轴（写错轴名和"这根轴没定义"长得一样）|
| **空 delta**（什么都不覆盖的组）| 它展开出来的 run 与 base 逐个相同 ⇒ 全部被跨组去重吃掉 ⇒ 用户看到"加了个组但 run 数没变"，是最难自查的一类静默无效 |

`base` 是**保留名**但**不是错误**：写 `name: base` 就是显式指顶层 `axes:` 那一组，
它的覆盖生效在 base 身上，**不新建第二个组**（新建的话批次里会有两个 base，
展开结果一模一样、只是组名不同，跨组去重把后者整组吃掉，看着就像"这条没生效"）。
也正因如此，空 delta 那条对 `name: base` 网开一面。

落地上分两种情况（2026-08-19 实测后定的）：

| spec 里的形状 | `parse_spec_mapping` 怎么处理 |
|---|---|
| 只有 `name: base` 这一条组 | 覆盖**并进顶层 `axes:`**，`BatchSpec.groups` 保持空 |
| 还有别的组 | **顶层 `axes:` 一个字不动**（它是全批次的轴**定义**），`base` 留在 `groups` 里当第一条（`matrix._all_groups` 支持显式 base），同时把 base 的取值**下放**给每一个没自己覆盖这根轴的组 |

第二种情况为什么不能也收窄：GUI 的「Save spec as…」写出来的顶层 `axes:` 是**全批次并集**
（某个组用了一个界面自造的取值 —— 三段网格 `0.4/0.5/0.4` 之类 —— 那个取值只在这里有定义）。
收窄之后它从定义里消失，读回来就是 `SpecError: Axis 'mesh' does not know the value ...`，
而这份文件正是本工具自己写出去的。「下放给兄弟组」那一步同样不能省：不下放的话那些组会继承
**宽定义**，替别人多扫一遍它从没要过的取值（`gui.state._axes_and_groups` 里有一模一样的一段）。

### `ewave_batch.core.cmd` — P1

四层合并 → `CommandPlan`；冲突检测；与参考命令逐 flag 集合 diff

```python
BUILTIN_DEFAULT_FLAGS   # 见 model.py / 下表
DEFAULT_DIFF_IGNORE   # 见 model.py / 下表
KEY_FLAG   # 见 model.py / 下表
def build_flag_layers(run: Run, ctx: PlanContext) -> FlagLayers:
    # 把这个 run 的五层 flag 各自装好（还没合并）。
    # `defaults` 层额外补两样"值来自站点发现、不来自源码常量"的东西：
    #   `--parallel`（从 `-R` 的 `cpu=` 推）和 `--key`（从 `SiteFacts.key` 取）。
def resolve_axis_flags(axis: Axis, value: str, facts: SiteFacts) -> FlagDict:
    # 算某根轴取某个值时贡献的 flag，并把占位符换掉。
def merge_flag_layers(layers: FlagLayers) -> FlagDict:
    # 按 `FlagLayers.MERGE_ORDER` 合并成一份 flag dict。
def detect_flag_conflicts(layers: FlagLayers, axes: Sequence[Axis]) -> list[FlagConflict]:
    # 查非法冲突。返回空 list = 干净。**自己不抛异常**，抛不抛由调用方定。
def render_flags(flags: FlagDict) -> list[str]:
    # flag dict → argv 片段，顺序**确定**（同样的输入永远同样的输出，cmd.sh 才可比对）。
def build_command_plan(run: Run, ctx: PlanContext) -> CommandPlan:
    # 一个 run → 完整 `CommandPlan`（阶段 2）。四层合并 + 冲突检测 + argv 渲染都在这。
def diff_flags(actual: FlagDict, expected: FlagDict, *, ignore: Sequence[str] = ()) -> FlagDiff:
    # 逐 flag 集合 diff。**golden 测试和红区 dry-run 的自带比对共用这一个函数。**
def diff_ports(actual: PortSpec, expected: PortSpec) -> PortDiff:
    # 比端口顺序（不是集合 —— **顺序就是映射**，见 BRIEF §5）。
def parse_resource_string(resources: str) -> dict[str, str]:
    # `"cpu=20;mem=100000"` → `{"cpu": "20", "mem": "100000"}`。空串 → 空 dict。
```

### `ewave_batch.core.layout` — P1

归档布局 / `cmd.sh` / 归档规则 / 产物验收 / `batch.json` 原子读写 / `runs.csv`

```python
def compute_run_paths(batch_dir: str, design: Design, run: Run) -> RunPaths:
    # 算出 BRIEF §5「归档布局」那棵树上这个 run 相关的全部路径。
def ensure_run_dirs(paths: RunPaths, *, dry_run: bool = False) -> None:
    # 建好 batch/gds/gdsout/sparam/runs 这些目录。`dry_run=True` 时什么都不做。
def write_cmd_sh(paths: RunPaths, plan: CommandPlan, *, dry_run: bool = False) -> str:
    # 把这个 run 的完整命令写成 `cmd.sh`（可单独手工重跑），返回写到的路径。
def verify_run_outputs(paths: RunPaths, run: Run, *, expected_port_count: int | None = None) -> VerifyReport:
    # 产物验收 —— **`done` 的判据**（不是 job 退出码，§12）。
def archive_run(paths: RunPaths, run: Run, *, keep: Sequence[str] = (), keep_logs_on_failure: bool = True, dry_run: bool = False) -> ArchiveReport:
    # 按 D5 归档：参数文件收进 `sparam/` 扁平区，mesh/pmrg/pmsh/resist 中间件删掉。
def check_port_consistency(state: BatchState) -> list[str]:
    # 批次内互相比对每个 run 的端口列表，返回问题描述（空 list = 一致）。
def port_count_from_suffix(path: str) -> int | None:
    # 从 `.s4p` / `.y3p` 这种后缀里取端口数；认不出返回 None。不读文件内容。
def state_to_dict(state: BatchState) -> dict[str, object]:
    # `BatchState` → 可 JSON 序列化的 dict（枚举落 `.value`，元组落 list）。纯函数。
def state_from_dict(data: Mapping[str, object]) -> BatchState:
    # 反过来。`schema_version` 比 `SCHEMA_VERSION` 大 → `StateError`（**拒绝而不是猜**）。
def read_batch_state(path: str) -> BatchState:
    # 读 `batch.json`。文件不存在 / 解析失败 / 版本不认识 → `StateError`。
def write_batch_state(path: str, state: BatchState) -> None:
    # **原子**写 `batch.json`：同目录临时文件 + `os.replace`。
def write_runs_csv(path: str, state: BatchState) -> None:
    # 写汇总表。表头 = `RUNS_CSV_COLUMNS`（冻结），`newline=""` + LF，UTF-8。
def set_run_as_current(paths: RunPaths, run: Run, design: Design, *, target_dir: str, dry_run: bool = False) -> list[str]:
    # 把某个 run 的 `.sNp` 落到官方那个路径上，让现成的 nport 零编辑生效（BRIEF §5）。
```

### `ewave_batch.core.discover` — P2

从官方 run 目录**运行时解析**站点坐标 → `SiteFacts`

```python
def discover_site_facts(official_run_dir: str, *, env: Mapping[str, str] | None = None) -> SiteFacts:
    # 解析一个官方 GUI 跑过的 design 目录 → `SiteFacts`。
def parse_gdsout_setup(text: str) -> dict[str, str]:
    # `gdsout_setup` 文本 → 字段 dict。
def templatize_gdsout_setup(text: str) -> str:
    # 官方 `gdsout_setup` → 模板：只把 7 个随 design 变的字段换成 `@@…@@` 占位符
def parse_dsub_options(text: str) -> dict[str, str]:
    # `remote_run_ewave.sh` 文本 → `{"account": …, "queue": …, "resources": …}`（缺的键就不给）。
def ptxt_path_for_corner(facts: SiteFacts, corner: str) -> str:
    # 算某个 corner 对应的 ptxt 绝对路径。
def learn_default_flags(facts: SiteFacts) -> FlagDict:
    # 从官方实际在用的 flag 里学出「默认表」（§11 规则 1：**默认表的值不写死在源码**）。
def find_tool(name: str, *, env: Mapping[str, str] | None = None) -> str | None:
    # `command -v` 的等价物（`shutil.which`），找不到返回 None。
def suggest_official_dirs(root: str, *, max_depth: int = 3) -> list[str]:
    # 在 `root` 底下找含 `gdsout_setup` 的目录，当作官方 design 目录的候选返回（已排序去重）。
```

### `ewave_batch.core.logparse` — P4

run 目录 → `LogFacts`（收敛/墙钟/峰值内存/端口数/成败）

```python
def strip_ansi(text: str) -> str:
    # 剥 ANSI 颜色码。eWave 即使 `--nogui` 也输出颜色，生产脚本靠管道 sed 剥 ——
def parse_ewave_log(text: str) -> LogFacts:
    # `ewave.log` → `LogFacts`。纯字符串函数（先 `strip_ansi`），不碰文件系统。
def parse_emsolver_log(text: str) -> LogFacts:
    # `emsolver.log` → `LogFacts`（收敛、真算过的频点数、峰值内存、CPU 占用多在这份里）。
def merge_log_facts(*facts: LogFacts) -> LogFacts:
    # 合并多份 `LogFacts`：**先到先得**（前面的非 None 值不被后面覆盖），
def parse_run_logs(run_dir: str) -> LogFacts:
    # 读一个 run 目录里的全部日志（`ewave.log` / `emsolver.log` / `mesh.log` …）→ 合并后的事实。
def parse_port_order(snp_path: str) -> tuple[str, ...]:
    # 从 `.sNp` 的注释头里读端口顺序（`--includePortOrder=1` 写进去的那份，D1d）。
```

### `ewave_batch.core.template` — P1

解析一条现成 ewave 命令行 → `ParsedCommand`

```python
def split_command_line(line: str) -> list[str]:
    # 把一行 shell 命令拆成 token（`shlex`，`posix=True`）。
def parse_command_line(line: str) -> ParsedCommand:
    # 一条 ewave 命令行 → `ParsedCommand`（program + flags + 端口 + 位置参数）。
def extract_command_line(text: str, *, program: str = ewave) -> str | None:
    # 从一份 shell 脚本文本里把调用 `program` 的那一行（含续行）抠出来，找不到返回 None。
```

### `ewave_batch.tools.strmout` — P2

阶段 1：渲染 `gdsout_setup` 模板（D1c）+ 拼 strmout argv

```python
DEFAULT_GDSOUT_TEMPLATE   # 见 model.py / 下表
GDSOUT_PLACEHOLDERS   # 见 model.py / 下表
GDSOUT_CRITICAL_FIELDS    # D1c 点名的 8 个"会改变 GDS 内容"的字段，必须逐字复现
CDSWORK_DIRNAME           # strmout 的 cwd 目录名（批次目录下）
def render_gdsout_setup(template_text: str, fields: GdsoutFields) -> str:
    # 把 `GDSOUT_PLACEHOLDERS` 换成 `fields` 的值，**其余逐字不动**（D1c）。
def build_strmout_plan(design: Design, ctx: PlanContext, *, setup_path: str) -> CommandPlan:
    # 拼阶段 1 的 `strmout` 命令（`-templateFile <setup_path>` 形式）。
def gdsout_fields_for_design(design: Design, ctx: PlanContext, *, gds_path: str, layer_map: str = "") -> GdsoutFields:
    # 按归档布局算出这个 design 的 7 个字段。★ P3 driver 的硬依赖。
def cdswork_dir(batch_dir: str) -> str:
    # `strmout` 该在哪个目录跑。★ P3 driver 的硬依赖（要往里写 cds.lib）。
def substitute_placeholders(text: str, values: Mapping[str, str]) -> tuple[str, tuple[str, ...]]:
    # 换 `@@TOKEN@@`，返回 (换完的文本, 没被换掉的 token)。
def parse_gdsout_fields(text: str) -> dict[str, str]:
    # `gdsout_setup` 文本 → 字段 dict（值去引号）。
def diff_gdsout_setup(actual_text: str, expected_text: str, *, ignore: tuple[str, ...] = ()) -> FlagDiff:
    # 两份 `gdsout_setup` 逐字段 diff，复用 `FlagDiff`（`compared_count` 防"空得好看"）。
```

### `ewave_batch.redzone_dryrun` — P2

红区那趟**只读** dry-run：解析真实官方目录 → 打印全部 argv + 落地目录 → 自带比对
（BRIEF §12「红区验证节奏」）。独立入口，**不经 `cli`** —— P1 一完成就能拿去红区跑。
操作手册：`docs/REDZONE_DRYRUN.md`。

```python
EXIT_OK / EXIT_DIFF / EXIT_NO_BASELINE / EXIT_ERROR   # 0 / 2 / 3 / 1，写进文档、机器可判
ReadOnlyViolation   # 落点选在 spine 或 OFFDIR 里 → 打印任何命令**之前**拒绝
ComparisonReport / DryRunReport   # 纯数据；渲染是 format_report 的事
def build_report(offdir: str, *, spec_path: str = "", batch_root: str = "./ewave_batches", batch_name: str = "dryrun", limit: int = 0, show_gdsout: bool = False, env: Mapping[str, str] | None = None) -> DryRunReport:
    # 跑完整趟规划，返回纯数据报告。**不写任何文件**，所有落点经 `WriteLedger` 过闸。
    # `env` 原样透传给 `core.discover.find_tool`（"传了 env 就只看 env"）：给了就完全
    # 不读真实环境 ⇒ `argv[0]` 只由入参决定。**测试的期望值必须走这条口子** ——
    # 否则"argv[0] 等于某个值"取决于跑测试那台机器 PATH 上有没有 ewave（本机没有 ⇒ 绿，
    # 红区 `ma ewave/…` 之后有 ⇒ 红），也就是在唯一真正重要的机器上是红的。
def format_report(report: DryRunReport) -> str:
    # 报告 → 人能看懂的文本（含"哪几条是结构上必然相等"的诚实交代）。
def build_parser() -> argparse.ArgumentParser:
def main(argv: Sequence[str] | None = None) -> int:
    # 第一件事是 `ascii_safe_stdio()`（红区 LANG 常是 C）。
```

### `ewave_batch.tools.ewave` — P3

阶段 2：拼 ewave argv（薄封装到 `core.cmd`）

```python
def render_ports(port_spec: PortSpec) -> list[str]:
    # 端口部分的 argv：`-p P000=<pin> -p …  -i P000 …`（**保序**）。
    # ⚠️ `PortMode.ALL` → **返回空 list**，`--all` 由机制层的 flag dict 出（否则 argv 里两个 `--all`）。
def ewave_program(facts: SiteFacts) -> str:
    # 要执行的 ewave 可执行文件。`facts.ewave_bin` 为空 → `ToolMissingError`。
def build_ewave_plan(run: Run, ctx: PlanContext) -> CommandPlan:
    # 阶段 2 的 `CommandPlan` —— 薄封装：转给 `core.cmd.build_command_plan`，
```

### `ewave_batch.sched.fake` — P3

假调度器/假 runner —— 模拟实测过的坑，本机跑完整假批次

```python
FakeRunner   # 见 model.py / 下表
FakeScheduler   # 见 model.py / 下表
FakeFailureMode   # 见 model.py / 下表
```

`FakeRunner` 必须满足 `model.RunnerProtocol`（self-test 逐方法比签名）。

`FakeScheduler` 必须满足 `model.SchedulerProtocol`（self-test 逐方法比签名）。

### `ewave_batch.sched.donau` — P3

真提交（`dsub`/`djob`），从 kit 移植，runner 可注入

```python
DonauScheduler   # 见 model.py / 下表
def build_dsub_argv(plan: CommandPlan, *, account: str = , queue: str = , resources: str = , name: str = , log_path: str = ) -> list[str]:
    # 拼 `dsub … <命令>` 的 argv。
def parse_dsub_submit_output(text: str) -> str:
    # 从 dsub 的提交回显里抠 job id。抠不出来 → `SchedulerError`（**别返回空串继续**，
def parse_djob_output(text: str) -> dict[str, JobState]:
    # 查询回显 → `job_id` → `JobState`。认不出的状态字串映射成 `JobState.UNKNOWN`，不抛异常。
```

`DonauScheduler` 必须满足 `model.SchedulerProtocol`（self-test 逐方法比签名）。

### `ewave_batch.sched.driver` — P3

两阶段 DAG + 有界并发 + poll + verify + archive + resume

```python
Driver   # 见 model.py / 下表
SubprocessRunner   # 见 model.py / 下表
def make_driver(state: BatchState, contexts: Mapping[str, PlanContext], scheduler: SchedulerProtocol, runner: RunnerProtocol, *, on_event: Callable[[DriverEvent], None] | None = None) -> DriverProtocol:
    # 造一个 driver。`contexts` 的键是 `design_key`（坐标是 per-design 的）。
def run_batch(driver: DriverProtocol, *, poll_interval: float = 15.0, max_seconds: float | None = None) -> int:
    # CLI 的 `while` 驱动：反复 `tick()` + sleep 到全部终态，返回进程退出码
def resume_batch(batch_dir: str, contexts: Mapping[str, PlanContext], scheduler: SchedulerProtocol, runner: RunnerProtocol, *, on_event: Callable[[DriverEvent], None] | None = None) -> DriverProtocol:
    # 从 `batch.json` 恢复一个批次（D7 断点续跑）。
```

`SubprocessRunner` 必须满足 `model.RunnerProtocol`（self-test 逐方法比签名）。

`Driver` 必须满足 `model.DriverProtocol`（self-test 逐方法比签名）。

### `ewave_batch.cli` — P5

`run / dry-run / resume / archive / status`

```python
SUBCOMMANDS   # 见 model.py / 下表
def build_parser() -> "argparse.ArgumentParser":
    # 造 `argparse` 解析器。单独一个函数是为了让测试能直接拿它验参数面，不用起进程。
def main(argv: Sequence[str] | None = None) -> int:
    # 命令行入口。`ewave_batch.cli.main`、顶层 `cli.main`、`gui.frames.*.main` 共用这个签名。
```

### `cli` — P5

顶层薄壳，转发到 `ewave_batch.cli`

```python
def main(argv: Sequence[str] | None = None) -> int:
    # 命令行入口。`ewave_batch.cli.main`、顶层 `cli.main`、`gui.frames.*.main` 共用这个签名。
```

### `gui.app` — P5

起 GUI（tkinter 在函数体内才 import）

```python
LAYOUTS   # 见 model.py / 下表
def launch(layout: str = split) -> int:
    # 起 GUI。`layout` ∈ `gui.app.LAYOUTS`（`"stacked"` / `"tabbed"` / `"split"`，默认 split）。
```

### `gui.state` — P5

GUI ↔ driver 的桥，实现 `GuiBridgeProtocol`

```python
GuiState   # 见 model.py / 下表
```

`GuiState` 必须满足 `model.GuiBridgeProtocol`（self-test 逐方法比签名）。

### `gui.frames.stacked` — P5

布局 A

```python
LAYOUT_NAME   # 见 model.py / 下表
def build_frame(parent: object, bridge: GuiBridgeProtocol) -> object:
    # 建一版布局的主 frame（`gui.frames.{stacked,tabbed,split}` 各一份）。
def main(argv: Sequence[str] | None = None) -> int:
    # 命令行入口。`ewave_batch.cli.main`、顶层 `cli.main`、`gui.frames.*.main` 共用这个签名。
```

### `gui.frames.tabbed` — P5

布局 B

```python
LAYOUT_NAME   # 见 model.py / 下表
def build_frame(parent: object, bridge: GuiBridgeProtocol) -> object:
    # 建一版布局的主 frame（`gui.frames.{stacked,tabbed,split}` 各一份）。
def main(argv: Sequence[str] | None = None) -> int:
    # 命令行入口。`ewave_batch.cli.main`、顶层 `cli.main`、`gui.frames.*.main` 共用这个签名。
```

### `gui.frames.split` — P5

布局 C（默认）

```python
LAYOUT_NAME   # 见 model.py / 下表
def build_frame(parent: object, bridge: GuiBridgeProtocol) -> object:
    # 建一版布局的主 frame（`gui.frames.{stacked,tabbed,split}` 各一份）。
def main(argv: Sequence[str] | None = None) -> int:
    # 命令行入口。`ewave_batch.cli.main`、顶层 `cli.main`、`gui.frames.*.main` 共用这个签名。
```

---

## Protocol 的方法签名（实现类要逐字满足，**含返回注解**）

self-test 对 `FROZEN_PROTOCOL_IMPLS` 里的类逐方法比签名 —— `@runtime_checkable` 的 isinstance 只看方法名在不在，挡不住参数漂移。

### `model.RunnerProtocol`

实现方：`ewave_batch.sched.fake:FakeRunner`、`ewave_batch.sched.driver:SubprocessRunner`

```python
def run(self, argv: Sequence[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None, timeout: float | None = None, on_line: Callable[[str], None] | None = None, cancel: Callable[[], bool] | None = None) -> RunResult: ...
```

### `model.SchedulerProtocol`

实现方：`ewave_batch.sched.fake:FakeScheduler`、`ewave_batch.sched.donau:DonauScheduler`

```python
def cancel(self, job: Job) -> bool: ...
def poll(self, jobs: Sequence[Job]) -> dict[str, Job]: ...
def submit(self, plan: CommandPlan, *, resources: str = , name: str = ) -> Job: ...
```

### `model.DriverProtocol`

实现方：`ewave_batch.sched.driver:Driver`

```python
def cancel(self) -> None: ...
@property
def state(self) -> BatchState: ...
def summary(self) -> dict[str, int]: ...
def tick(self) -> TickReport: ...
```

### `model.GuiBridgeProtocol`

实现方：`gui.state:GuiState`

```python
def axes(self) -> tuple[Axis, ...]: ...
def cancel(self) -> None: ...
def command_text(self, run_id: str) -> str: ...
def designs(self) -> tuple[Design, ...]: ...
def load_spec(self, path: str) -> None: ...
def plan(self) -> None: ...
def runs(self) -> tuple[Run, ...]: ...
def start(self, *, dry_run: bool = False) -> None: ...
def summary(self) -> dict[str, int]: ...
def tick(self) -> TickReport | None: ...
```

---

## 常量 / 类：谁负责给出什么

| 符号 | 谁写 | 内容要求 |
|---|---|---|
| `core.cmd.BUILTIN_DEFAULT_FLAGS` | P1 | 内置兜底默认（BRIEF §6「已知的生产默认值」那一串）。**只许 flag 名和通用数值**，零站点坐标 |
| `core.cmd.DEFAULT_DIFF_IGNORE` | P1 | 与参考命令比对时默认忽略的 flag（路径/站点相关的那几个）。**精确名，不许前缀** |
| `core.cmd.KEY_FLAG` | P1 | `--key` 的 flag 名。**取值永远来自 `SiteFacts.key`，源码里零写死**（硬约束 1b）。`build_flag_layers` 把它补进默认表层；`facts.key` 为空则**什么都不加** —— 宁可缺，也不许编一个假 key |
| `core.spec.EXAMPLE_SPEC` | P1 | 一份可直接改的示例 spec 文本。占位符写 `<lib>`/`<cell>`/`<view>`，**不许真实取值** |
| `tools.strmout.DEFAULT_GDSOUT_TEMPLATE` | P2 | 兜底模板文本（形如 `mvp/redzone/gdsout_setup.tmpl`）。⚠️ **必须是源码里的字符串常量，不许放成 `.tmpl` 文件** —— `.gitignore` 里 `gdsout_setup*` 是被忽略的，放文件会静默不进包 |
| `tools.strmout.GDSOUT_PLACEHOLDERS` | P2 | 7 个占位符名（`runDir`/`library`/`topCell`/`view`/`strmFile`/`logFile`/`layerMap`）|
| `tools.strmout.GDSOUT_CRITICAL_FIELDS` | P2 | D1c 点名的 8 个字段（`maxVertices`/`case`/`convertPin`/…）。它们**不是**占位符，必须逐字复现：丢一行 GDS 照样导得出、eWave 照样跑得完、数字还挺像，只有 mesh 悄悄变了 |
| `tools.strmout.CDSWORK_DIRNAME` | P2 | `strmout` 的 cwd 目录名（批次目录下）。cwd 里要有一份能看见目标 library 的 `cds.lib` —— 放在我们自己的目录里，于是**不必 cd 进设计师的 workarea**（硬约束 4）|
| `redzone_dryrun.EXIT_*` | P2 | `0` 一致 / `2` 有差异 / `3` 没能比对 / `1` 跑不起来。写进 `docs/REDZONE_DRYRUN.md`，红区靠退出码判 |
| `sched.fake.FakeFailureMode` | P3 | 至少要能模拟实测过的三个坑：`exit=0` 但崩了、0 字节产物报 done、写失败被吞 |
| `cli.SUBCOMMANDS` | P5 | `("run", "dry-run", "resume", "archive", "status")` |
| `gui.app.LAYOUTS` | P5 | `("stacked", "tabbed", "split")`，默认 `split` |
| `gui.frames.*.LAYOUT_NAME` | P5 | 该模块自己的布局名 |

`gui/frames/*.py` 的四条硬要求：
1. `build_frame(parent, bridge)` 返回该版布局的根 widget，**只通过 `bridge`
   （`model.GuiBridgeProtocol`）跟核心说话**，frame 里不许有业务逻辑；
2. **模块顶层不许建 `Tk()`**、不许在 import 时碰 `$DISPLAY`；
3. `EWB_SMOKE=1 python -m gui.frames.<v>` 要能 headless 建完就退（`check.sh` 第 5 步跑它）。
4. 三版暴露**同一组** `SECTIONS`（顺序也一致）。这是「三个 agent 各写各的、界面手感不一致」
   唯一的机器判据（`tests/test_gui_frames.py`）。

`SECTIONS` 现在是 **9 个**（2026-08-19 从 8 个加到 9 个，D14）：

```python
("batchbar", "designs", "groups", "settings", "resources", "runs", "detail", "actionbar", "statusbar")
```

新加的是 `groups`（Run groups 面板），位置在 `designs` 和 `settings` **之间** ——
它管的正是「哪些 design 跟哪些设定相乘」，摆在这两者中间才读得通。
名字对应共用层的 `build_<section>`；前八个与 `mockups/_ui.py` 的 `build_*` 一一对上，
`groups` 是草图之后加的，`mockups/` 里没有对应物 —— 草图是当初的设计成果、**不回改**
（拿它当 fixture 的价值正是"人写的、不跟着实现动"）。

⚠️ 改 `SECTIONS` 要**同一个 commit 改四处**：三版 `gui/frames/*.py`，
加上 `tests/test_gui_frames.py` 里手抄的 `EXPECTED_SECTIONS`（旁边还有写死条数的断言）。
只改三版 frame 的话，闸门会**红在测试文件上而不是红在实现上** —— 2026-08-19 真发生过一次，
一眼看去像"实现写错了"，实际上实现是对的。`SECTIONS` 不在冻结面上、self-test 管不着它，
所以这条纪律只能靠这段文档 + 那条一致性测试兜住。

还有一处容易被顺手删掉：`test_gui_frames.py` 拿 `mockups/_ui.py` 的 `build_*`
**反查**期望值（"人写的 fixture 不跟着实现动"才有价值）。`groups` 是草图之后加的，
草图里没有它 ⇒ 那条反查里**显式豁免了它一件**。别把豁免连同断言一起删掉：
剩下八件仍然必须与草图一个不多一个不少。

---

## 改冻结接口的流程

冻结之后发现签名是错的 —— **可以改**。禁止的是静默漂移，不是改一个错的冻结。

1. **同一个 commit** 里改完三样：`ewave_batch/model.py` + `docs/INTERFACES.md` + **全部调用方**；
2. `model.INTERFACE_VERSION` +1；
3. commit message 标 `[interface-change]`，正文写清「原签名 / 新签名 / 为什么 / 影响哪些模块」；
4. 跑 `sh scripts/check.sh` 确认全绿（self-test 会把没跟上的调用方报成 DRIFT）；
5. 审查 agent 专门核对这一条。

只加**新**符号（不动已有签名）也要更新 `FROZEN`，否则 self-test 不认识它、
也就没人替你盯着它别漂。

### self-test 的判据（照着这个写就不会红）

| 情况 | 结果 |
|---|---|
| 模块文件还不存在 | `pending: P<n>` —— 不是错 |
| 模块 import 失败但缺的是**别的**依赖（比如没装 tkinter） | `blocked: <dep>` —— 平台降级，不算漂移 |
| 模块存在，符号齐、签名一致 | `implemented` |
| 少符号 / 签名对不上 / **从 `model` re-export 桩子** | `DRIFT` → 退出码 1 |

最后一条值得单独说：`from ..model import expand_runs` 这种写法能骗过"符号存在"检查，
但 self-test 会比 `__module__` —— **桩子不算实现**。

---

## 还没冻结的东西（有意留白）

* **`gui.state.GuiState` 的 run group 编辑面** —— 15 个方法，**都不在冻结面上**，
  冻的只有 `GuiBridgeProtocol` 里那几个。留白的理由：这一面是给 `gui/_ui.py`
  一个消费者用的，冻了等于把界面的形状也冻住，而界面还在改。

  | 方法 | 干什么 |
  |---|---|
  | `groups() -> tuple[RunGroup, ...]` | 全部组，**第一个恒为 base**（`name == BASE_GROUP`）|
  | `active_group() -> str` / `set_active_group(name)` | 当前在编辑哪个组，默认 base |
  | `add_group(name="") -> str` / `duplicate_group(name) -> str` | 建组 / 复制一个组（含 base）；**返回实际用的名字**（重名自动加后缀）|
  | `remove_group(name)` / `rename_group(old, new)` | base 不可删 |
  | `group_override(axis, group="") -> tuple[str, ...] \| None` | **`None` = 这根轴继承 base**；`group` 省略 = active group |
  | `set_group_override(axis, values, group="")` / `clear_group_override(axis, group="")` | 写 / 撤销这一层覆盖 |
  | `group_run_counts() -> list[tuple[str, int]]` | `[(组名, 去重后的 run 数), …]` |
  | `merged_run_count() -> int` | 跨组折叠掉几个（界面写 "5 runs (1 duplicate merged)"）|
  | `group_summary(name) -> str` | 一行摘要，例 `+ 55.0, eqI off`；base 组给全量摘要 |
  | `group_of(run_id) -> str` | 这个 run 出自哪个组（Runs 表的 Group 列）|
  | `groups_change_warning() -> str` | 批次已经跑过时给出那句「加组会改掉基线目录名、resume 认不出老目录」；**没什么好警告的返回空串** |

  三条关键约定（写错了界面就会说谎）：

  1. **`set_axis_values()` / `axis_selection()` / `axis_counts()` 作用于 active group。**
     active = base 时与加组之前**逐字相同**；active = 别的组时 `set_axis_values` 写该组的覆盖，
     而 `axis_selection` 返回**合并后的有效值**（继承的轴给 base 的值）——
     "这根轴到底是继承还是覆盖"由 `group_override()` 单独回答。
     这样切组时 `_ui.py` 的 `push()` 几乎不用改。
  2. **`run_count()` / `formula()` 数的是跨组去重之后的数**，必须与 `plan()` 真正建出来的条数一致
     —— 两个数不一致的话，界面上那个数就是谎话。`formula()` 只有 base 一个组时保持
     `2 designs x 1 corner x 3 temp x 2 mode = 12 runs` 的连乘写法（最常见的场景不该因为
     多了一个功能而变难懂），有组时改成 `2 designs x (3 + 1 + 1) = 10 runs`。
  3. ⚠️ **界面自造的轴取值（mesh / freq）带的是具体 flag、没有 `{value}` 占位符**，
     `matrix.axis_with_values` 翻不出组里写的新取值。`GuiState` 只在这种情况下把该轴的取值表
     加宽成并集，并塞一条**显式的 base 组**把基线的取值锁回去
     （`matrix._all_groups` 明确支持调用方自己给 base 组）。没有组时这条路径完全不触发。
     契约里原本没写这一层，是照代码实测补上的。
* **`gui.frames.*.SECTIONS`** —— 三版必须一致（`tests/test_gui_frames.py` 盯着），
  但**不在冻结面上**：冻的只有 `LAYOUT_NAME` / `build_frame` / `main`。
  内容和改它的纪律见上面「四条硬要求」。
* `analyze/`（v2 接 SNP_RLC_Extractor 做批量对比）—— 本夜不做。
* `sched.fake` / `sched.donau` 各自的构造参数：只冻了它们必须满足的 Protocol，
  怎么注入假输出由 P3 自己定。
* `deploy/`（P6）：形状照抄 `SNP_RLC_Extractor/deploy/`，不走冻结面。
