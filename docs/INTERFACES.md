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

## 五条会咬人的契约

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
def varying_axes(axes: Sequence[Axis], *, designs: Sequence[Design] = ()) -> list[Axis]:
    # 挑出**真正在变**的轴（取值 > 1 个的）。
def compute_axes_slug(axis_values: Mapping[str, str], axes: Sequence[Axis]) -> str:
    # 算 `<axes-slug>` —— 只含 `encoded_in_ewave_dir=False` 且**在变**的轴。
def axes_for_design(design: Design, axes: Sequence[Axis]) -> list[Axis]:
    # 把 `design.axis_overrides` 套到全局轴定义上，返回这个 design 实际要扫的轴。
def builtin_axis_catalog() -> dict[str, Axis]:
    # 内置轴目录：轴名 → `Axis`（取值列表为**该轴的合法取值样例**，实际取值由 spec 给）。
def expand_runs(designs: Sequence[Design], axes: Sequence[Axis], *, options: BatchOptions | None = None) -> list[Run]:
    # 展开笛卡尔积 → `list[Run]`，每个 Run 带好 `run_id` / `axes_slug` / `ewave_dir`。
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
EXAMPLE_SPEC   # 见 model.py / 下表
```

### `ewave_batch.core.cmd` — P1

四层合并 → `CommandPlan`；冲突检测；与参考命令逐 flag 集合 diff

```python
BUILTIN_DEFAULT_FLAGS   # 见 model.py / 下表
DEFAULT_DIFF_IGNORE   # 见 model.py / 下表
def build_flag_layers(run: Run, ctx: PlanContext) -> FlagLayers:
    # 把这个 run 的五层 flag 各自装好（还没合并）。
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
    # 从 `.s17p` / `.y16p` 这种后缀里取端口数；认不出返回 None。不读文件内容。
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
def render_gdsout_setup(template_text: str, fields: GdsoutFields) -> str:
    # 把 `GDSOUT_PLACEHOLDERS` 换成 `fields` 的值，**其余逐字不动**（D1c）。
def build_strmout_plan(design: Design, ctx: PlanContext, *, setup_path: str) -> CommandPlan:
    # 拼阶段 1 的 `strmout` 命令（`-templateFile <setup_path>` 形式）。
```

### `ewave_batch.tools.ewave` — P3

阶段 2：拼 ewave argv（薄封装到 `core.cmd`）

```python
def render_ports(port_spec: PortSpec) -> list[str]:
    # 端口部分的 argv：`--all`，或者 `-p P000=<pin> -p …  -i <pin> …`（**保序**）。
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
| `core.spec.EXAMPLE_SPEC` | P1 | 一份可直接改的示例 spec 文本。占位符写 `<lib>`/`<cell>`/`<view>`，**不许真实取值** |
| `tools.strmout.DEFAULT_GDSOUT_TEMPLATE` | P2 | 兜底模板文本（形如 `mvp/redzone/gdsout_setup.tmpl`）。⚠️ **必须是源码里的字符串常量，不许放成 `.tmpl` 文件** —— `.gitignore` 里 `gdsout_setup*` 是被忽略的，放文件会静默不进包 |
| `tools.strmout.GDSOUT_PLACEHOLDERS` | P2 | 7 个占位符名（`runDir`/`library`/`topCell`/`view`/`strmFile`/`logFile`/`layerMap`）|
| `sched.fake.FakeFailureMode` | P3 | 至少要能模拟实测过的三个坑：`exit=0` 但崩了、0 字节产物报 done、写失败被吞 |
| `cli.SUBCOMMANDS` | P5 | `("run", "dry-run", "resume", "archive", "status")` |
| `gui.app.LAYOUTS` | P5 | `("stacked", "tabbed", "split")`，默认 `split` |
| `gui.frames.*.LAYOUT_NAME` | P5 | 该模块自己的布局名 |

`gui/frames/*.py` 的三条硬要求：
1. `build_frame(parent, bridge)` 返回该版布局的根 widget，**只通过 `bridge`
   （`model.GuiBridgeProtocol`）跟核心说话**，frame 里不许有业务逻辑；
2. **模块顶层不许建 `Tk()`**、不许在 import 时碰 `$DISPLAY`；
3. `EWB_SMOKE=1 python -m gui.frames.<v>` 要能 headless 建完就退（`check.sh` 第 5 步跑它）。

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

* `analyze/`（v2 接 SNP_RLC_Extractor 做批量对比）—— 本夜不做。
* `sched.fake` / `sched.donau` 各自的构造参数：只冻了它们必须满足的 Protocol，
  怎么注入假输出由 P3 自己定。
* `deploy/`（P6）：形状照抄 `SNP_RLC_Extractor/deploy/`，不走冻结面。
