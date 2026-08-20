# 交接报告 —— 2026-08-18/19 无人值守夜跑

写给早上的你自己，**当复核清单用，不是交付物宣传**。
凡是我没亲眼验证过的，都标了「未验证」并写了怎么验。

> 这份报告里的数字，除了「每阶段做了什么」那一节的历史单测数（取自 commit message），
> **其余全部是我自己在 2026-08-19 早上跑出来的**，不是转述任何 agent 的自报。
> 理由见 §4 打回 #3 的最后一条：本夜跑有一条 agent 自报被审查**验出是假的**。

---

## 0. 一句话结论

**七个阶段全部落地，闸门 ALL GREEN（962 条单测，退出码 0），可以拿去红区跑第一次只读 dry-run；
但「跑得对不对」这件事本机一次都没能验证过 —— 所有跟真实 eWave/Donau 打交道的形状都是照证据推的。**

> ⚠️ **这句话说的是 2026-08-18/19 夜跑那一刻（962 条）。** 之后白天又落了 5 个 commit
> 加一批未提交的改动，最大的一件是 **run group 组合模型**（BRIEF D14）——
> 闸门仍是 ALL GREEN，条数涨到 **1022**。逐条见 §3 的表和它后面那两节。
> 除此之外本文其余部分仍然成立。

能干什么：

- 在红区**只读**跑 `redzone_dryrun`，把「我们拼出来的命令」和「官方那条真命令」逐 flag / 逐端口对比
  —— **这是今晚全部工作的验收点，也是唯一值得先做的事**；
- 定义 designs × 设定轴的矩阵，展开成 run 列表，每个组合独立 `--workDir`
  （= 用户的核心痛点，静默覆盖问题的解法）；
- 五个 CLI 子命令（`run` / `dry-run` / `resume` / `archive` / `status`）+ 三版 tkinter GUI（默认 split）；
- 打包 → 过气隙 → 红区部署 → 自检的整条链路（`deploy/pack.ps1` → `deploy.sh` → `deploy/doctor.sh --test`）。

**不能**干什么（重要）：

- **不能保证真跑出来的 `.sNp` 是对的。** 逐 flag 对比里有 4 个 flag 是「与学习表同源」= 循环论证，
  只有红区真跑才有意义（见 §5-L）；
- **不能保证 Donau 调度真的会动。** `djob` 的真实回显格式**一次都没见过**（§5-B），
  `dsub` 不加 `-I` 是阻塞还是异步**未决**（§5-C）；
- **不能保证日志解析在红区认得出东西。** 本机没有任何真实 `ewave.log`，P4 的 fixture 是**合成的**（§5-A）。

---

## ⚠️ 1. 先读这三条（其余都可以往后放）

### 1.1 有一个红区值现在**还在源码里**，而且**两道闸门都看不见它**

Touchstone 后缀里那个**两位数的端口数**（`.s<N>p` 的 N），是某个真实 cell 的端口数，
**逐字来自 `references/probes/` 的三个文件和 `PROJECT_BRIEF.md`** —— 我 grep 复核过，四处都在。

按项目自己的规矩（「形状可以照抄，值不许」）**这是一处泄漏**。自己看一眼（下面这条命令不含那个值本身）：

```sh
git ls-files -co --exclude-standard -z \
  | xargs -0 grep -o '\.s[0-9][0-9]p' 2>/dev/null | wc -l
```

我数出来 **8 个文件、48 处**：

| 文件 | 处数 |
|---|---|
| `tests/test_layout.py` | 26 |
| `ewave_batch/core/layout.py` | 11 |
| `tests/test_layout_no_collision.py` | 4 |
| `ewave_batch/model.py` | 2 |
| `ewave_batch/sched/driver.py` | 2 |
| `docs/INTERFACES.md` | 1 |
| `ewave_batch/sched/fake.py` | 1 |
| `mockups/_common.py` | 1 |

**闸门为什么看不见**：`redzone_crosscheck.sh` 的候选词正则是 `[A-Za-z]?[0-9]{7,}`（7 位以上数字），
**两位数根本不进候选集**。`redzone_scan.sh` 的词表里也没有它。

**P4 的 agent 发现过这件事**，往词表里加过一条规则，**随即自己撤回了** —— 因为那条规则会命中
`mockups/_common.py`，而 `mockups/` 在「不许碰」清单里。它的判断是
「一条会逼人去改不许碰文件的规则是坏规则」，这个判断我同意；
**但撤回规则之后没有人去处理那 48 处，事情就悬在那里了。**

**我没有动它** —— 我这一趟只许碰 `docs/HANDOFF.md`，而且这是个需要你拍板的判断题，不是机械修复。

三个选项：

| 选项 | 代价 |
|---|---|
| a) 全部换成 `.sNp` / `.s4p`，`mockups/` 一并改 | 要破一次「不许碰 `mockups/`」，但那条规矩是为了保护设计成果，改一个后缀不伤设计 |
| b) 只改 `ewave_batch/` + `tests/` + `docs/`，`mockups/` 留着 | 闸门规则仍然不能加（否则红在 mockups 上），泄漏面缩小但不归零 |
| c) 判定「端口数不算站点身份」，不动 | 要把这个判断**写进 `CLAUDE.md` 或 BRIEF**，否则下一个 agent 会重新纠结一遍 |

我倾向 (a)。这个信号本身很弱（弱到 P4 agent 判断「不值得」），但
**它是唯一一处「我们知道它在那儿却留着」的值**，而这个仓库要上 public GitHub。
留着的真正代价不是这 48 处，是它让「零红区标识符」这条规矩出现了一个**有先例的例外**。

### 1.2 `--all` 会把接地端口也当 signal 端口 —— 这会改变 `.sNp` 的内容

这是 **dry-run 自己报出来的**，不是我推的。对着合成 fixture 跑，`[4/5] b)` 那一节打出来：

> 官方的 `-i` 只覆盖 5 个端口里的 4 个（有 1 个是接地端口），而 `--all` 把**全部**端口都当 signal

`--all` 能逐位复现官方**端口顺序**（D1b，这条在合成 fixture 上验过，5 位逐位吻合），
但 `-i`（signal port 子集）它复现不了 —— `--all` 没有「哪些是接地」这个信息。

**后果**：矩阵跑出来的 `.sNp` 可能和官方 GUI 跑出来的**维度相同但内容不同**。
这不是崩溃型 bug，是**静默的结果差异**，正是最贵的那一类。

红区第一次跑一定要确认这一条（见 §7）。dry-run 会主动提示两条出路：
spec 里显式写 `ports`（把 `-p`/`-i` 照官方粘出来），或者确认接地端口对结果无影响再继续用 `--all`。

### 1.3 有一次全套单测变红，**没能复现，也没能证伪**

`tests/test_cli.py::LazyImport::test_cli_works_without_tkinter` 报过一次
`TypeError: NoneType + NoneType`，根因是 reader 线程里 `UnicodeDecodeError: 'gbk' codec`。

报告这件事的 agent 倾向于「并发写文件导致的一次性现象」（当时确实有并行 agent 在往仓库里写文件），
之后它跑了 5 次全绿，**我今早又跑了 4 次全量，也全绿**。

但我**不能排除它是一条真实的偶发脆弱测试**。值得单列的原因是它的表征
—— **子进程输出按 locale 解码** —— 正是红区（`LANG=C`）最容易发作的那一类。

**怎么验证**：红区那边 `bash deploy/doctor.sh --test` 跑一次。如果这条红了，那就不是一次性现象。

---

## 2. 我自己跑出来的数（2026-08-19，不是转述）

任务书要求的五条，逐条真实输出：

```
[1] sh scripts/check.sh                                          EXIT=0
      1/5 golden fixture 未被篡改      ok
      2/5 单元测试                     Ran 962 tests in 36.902s   OK (skipped=3)
      3/5 红区标识符闸门               redzone_scan: clean
                                       crosscheck: clean（621 个证据 token，零命中）
      4/5 dry-run 冒烟                 模块 23 个｜漂移 0｜未实现 0｜平台降级 0｜合计 23
                                       ASCII locale（LANG=C）下同样退 0
      5/5 GUI 三版 headless            6 个入口全 ok（mockups 三版 + gui/frames 三版）
      => check: ALL GREEN

[2] EWAVE_ABS=/fakepdk/bin/ewave STRMOUT_ABS=/fakepdk/bin/strmout \
      python -m unittest discover -s tests -t .                   EXIT=0
      Ran 962 tests in 37.192s   OK (skipped=3)

[3] EWAVE_BIN=/other/place/ewave STRMOUT_BIN=/other/place/strmout \
      python -m unittest discover -s tests -t .                   EXIT=0
      Ran 962 tests in 37.411s   OK (skipped=3)

[4] PYTHONIOENCODING=ascii python -m ewave_batch dry-run --self-test   EXIT=0
      （中文降级成 ? 但进程不崩，这正是 ascii_safe_stdio() 的设计行为）

[5] EWB_SMOKE=1 python -m gui.frames.split                        EXIT=0
      （无输出，符合预期）
```

额外自己做的四条核查：

```
[6] git log --oneline
      10 个 commit（见 §3），最新 5f9c5f5
      ⚠️ P6 的 9 个文件还是**未提交**状态

[7] 历史泄漏扫描
      git log -p --all | grep -oE '\b[A-Za-z]?[0-9]{7,}\b' | sort | uniq -c
      => 逐个查过，命中项全部是 git 自己的 blob 短哈希（diff 里 `index <hash>..<hash>` 那种）
         和明显合成的值（10000001-10000004 / 777888999）。**历史干净。**

[8] 三条 skip 逐条看了原因，全部是合法的平台性 skip、且都带原因：
      · tests/test_logparse.py::RealLogFixtureTests（2 条）
          缺 tests/fixtures/ewave_run_real.local.d/ 和 ports_real.local.s*p
          —— 真实日志不进 git，从红区抄一份回来就自动生效
      · tests/test_spec.py::ExampleSpec::test_example_spec_parses（1 条）
          本机没装 PyYAML（红区装了 6.0.1）—— 见 §5-G，这条影响不小

[9] 「四条闸门脚本在红区会假红」这个说法我自己复现了一遍（见 §5-K），成立
```

单测数对账：`5f9c5f5` 是 918 条，P6 两路加了 `tests/test_deploy.py`(25) + `tests/test_redzone_bundle.py`(19)
= 44 条，918 + 44 = **962**。我单独跑过那两个文件确认了 25 和 19。**数对得上。**

---

## 3. 每阶段做了什么

| commit | 阶段 | 内容 | 单测 |
|---|---|---|---|
| `de198f6` | P0 | 接口冻结：`model.py`（数据结构 + 全部跨模块签名 + FROZEN 清单）+ `__main__.py` self-test + `docs/INTERFACES.md` | 23 |
| `69aafa1` | P0 后置 | **编排者自己做的**：修两个「气隙对面才发作」的闸门洞（各带正反测试） | 29 |
| `6a32113` | P1 | 核心：`matrix`+`spec` / `cmd`+`template`（golden 逐 flag）/ `layout` | 261 |
| `1b9b4cb` | — | `[interface-change]`：冻结面补三个逃逸符号 + `cmd.sh` 改成每 run 一份 | 269 |
| `535b65a` | P2 | 阶段一工具：`strmout` 模板（D1c 八字段逐字）/ `discover` 运行时解析 / **红区 dry-run 入口** | 450 |
| `98e285f` | P3 | 调度：`fake`（模拟三条实测过的坑）/ `donau` 移植 / `driver` 两阶段 DAG + resume；**新增 crosscheck 闸门** | 678 |
| `ca7db68` | P4 | 日志解析：`ewave.log` / `emsolver.log` → 收敛 / 墙钟 / 峰值内存 / 端口数 / 成败 / 频点数 | 863 |
| `9b21773` | P5 | 界面：`cli`（五个子命令）+ `gui/state.py` 桥 + `gui/_ui.py` 共用层 + 三版 frame | 914 |
| `5f9c5f5` | — | 修 `log_path` 撞名（`dsub -o` 会让 N 个 job 的 stdout 混进一份日志） | 918 |
| `01b1757` | P6 | 部署链路（`deploy.sh` / `pack.ps1` / `doctor.sh` / `_env_check.py`）+ 红区首跑 bundle | 962 |
| `a53e4ab` | — | 收尾：端口数退出源码（= §1.1 那个泄漏，走的是选项 b）+ 子进程解码不再看本机 locale | 962 |
| `0a418e0` | — | 默认值换成用户 2026-08-19 拍板的那套；撤掉「生产默认值是全局常量」这个被推翻的前提 | 963 |
| `3cc8b92` | — | 接上「Save spec as…」（`spec_to_mapping` / `dump_spec` / `save_spec` / `have_yaml`）| 975 |
| `438f3c7` | — | 修 split 布局：左栏内容被裁掉、分隔条拖不动 | 979 |
| **未提交** | — | `[interface-change]` **run group 组合模型**（BRIEF D14，`INTERFACE_VERSION` 2→3）| **1022** |

### ⚠️ P6 还没 commit

工作树现在是脏的，9 个文件待提交：

```
 M .gitattributes            M .gitignore              M tests/test_gate_portability.py
?? deploy.sh                 ?? deploy/                ?? docs/REDZONE_FIRST_RUN.md
?? scripts/redzone_bundle.sh ?? tests/test_deploy.py   ?? tests/test_redzone_bundle.py
```

按夜跑规矩 agent 不 commit，而 **P6 之后没有再跑「审查 + commit」的轮次**。
所以这 9 个文件（加上这份 `docs/HANDOFF.md`，共 10 个）是**唯一没经过审查 agent 的产出**。
早上第一件事是自己复核再 commit。

> ✅ **已了结（2026-08-19）**：复核过了，落成 `01b1757`。
> §1.1 那个端口数泄漏走的是**选项 b**（`a53e4ab`）：7 个"允许碰"的文件换成合成小值
> （`.s4p` / `.s3p`，与 P4 fixture 的 4 端口约定一致），`mockups/_common.py` 里那 **1 处没动**
> —— 它在「不许碰」清单里，而且初始提交就有。所以泄漏面从"今晚会扩散到 8 个文件"
> 缩回到"origin/main 上早就有的那 1 处"，**没有归零**；要不要连历史一起清仍是待定的判断题。

### run group 组合模型（2026-08-19，`[interface-change]`，**未提交**）

用户当天拍板的一条新决定，写进 `PROJECT_BRIEF.md` **D14**（编号跳过 D13 —— 那个号
早被「频率不是扫描轴」占了）。**为什么要它**：纯笛卡尔积表达不了「一条基线 + 几个单点变体」，
最接近的写法里一多半是废 run，而一个 run 的量级是 10 核 / 100 GB / 35 分钟。
**模型**：批次 = 一列 `RunGroup`，每组是 base 之上的 delta、各自取笛卡尔积、结果取并集，
跨组重复按 `run_id` 静默去重。

改到的面，自下而上四层：

| 层 | 改了什么 |
|---|---|
| 冻结面 | `model.py`：`BASE_GROUP` / `RunGroup` / `RunExpansion`；`BatchSpec`·`BatchState` 加 `groups`、`Run` 加 `group`；`INTERFACE_VERSION` 2→3。`SCHEMA_VERSION` **保持 1**（两个新字段都带默认值，读老 `batch.json` 不会炸，双向兼容 —— 判断依据写在它的 docstring 里）|
| 核心 | `matrix.axes_for_group` / `expand_runs_detailed`，`varying_axes`·`effective_axis_values` 加 `groups` 口径；`spec.py` 解析/校验/序列化顶层 `groups:` |
| 桥 | `gui/state.py`：15 个 group 方法；`set_axis_values()`·`axis_selection()`·`axis_counts()` 改成作用于 active group |
| 界面 | `gui/_ui.py` 的 `build_groups`（第 9 个 section）+ Settings 每根轴的「覆盖」勾选框；三版 frame 的 `SECTIONS` 8→9 |

**这一趟顺带把 10 个核心模块的面向用户字符串全部英文化并压成纯 ASCII**
（红区 `LANG` 常是 C），代码注释和 docstring 一律保留中文。

#### 闸门状态：**ALL GREEN**（2026-08-19 白天实测）

```
sh scripts/check.sh  ->  check: ALL GREEN
Ran 1022 tests ... OK (skipped=4, expected failures=1)
```

途中有一小段时间是红的，**值得记一笔**，因为它正是本项目最典型的一类失误：
三版 `gui/frames/*.py` 的 `SECTIONS` 加到 9 个（多了 `groups`）之后，
`tests/test_gui_frames.py` 里**手抄**的 `EXPECTED_SECTIONS` 还是 8 个、
还带一条写死 8 的数字断言 —— 13 条红全部长在测试文件上而不是实现上，
第一眼极容易被误读成"实现写错了"。

**教训已经固化进 `docs/INTERFACES.md`**：`SECTIONS` 那条现在明写
「改它要同一个 commit 改四处：三版 frame + `tests/test_gui_frames.py` 里那两处」。
`SECTIONS` 不在冻结面上（self-test 管不着它），所以纪律只能靠文档 + 那条一致性测试兜。

---

## 4. 审查打回 4 次，每次抓到什么

**这是这份报告最有价值的一节。** 一共 7 名审查 agent（每阶段一名），打回 4 次。
四次抓到的**没有一次是「功能坏了」**，全部是「绿得可疑」——
两次**假绿**（测试的绿依赖跑测试那台机器的偶然状态），两次**红区泄漏**（从证据里抄具体值进源码）。

### 打回 #1 —— P2 第一轮：假绿（本夜跑最有教育意义的一次）

**抓到什么**：`tests/test_redzone_dryrun.py` 把占位程序名 `"ewave"` / `"strmout"` 当 golden 期望值写死。

**根因**：这 5 条测试**只在「跑测试的机器上没装 eWave」时才绿**。
开发机没装 → `find_tool` 回退到占位名 → 绿。
红区 `ma ewave/…` 之后 PATH 上就有 → 返回绝对路径 → **5 条必红**。

也就是说：**它们在唯一真正重要的那台机器上是红的，而我们在这里永远看不到。**

**怎么修的**：编排者当场复现 ——
`EWAVE_ABS=… STRMOUT_ABS=… python -m unittest tests.test_redzone_dryrun` → `FAILED (failures=5)`，
然后把期望值改成不依赖 PATH 状态的判据。

**闸门为什么当时没拦住**：`check.sh` 只在开发机跑，而**闸门和被测代码共享同一个偶然状态**
（两边都看这台机器的 PATH）。一个只在自己家里跑的闸门，测不出「换个环境会怎样」。

**后来怎么补的**：⇒ **从 P2 起，编排者把「模拟红区的两组环境变量跑全量单测」列为每阶段的常规检查**
（就是任务书里那两条 `*_ABS` / `*_BIN` 命令）。**两次假绿都藏在那里。这两条现在是每阶段必跑的。**

### 打回 #2 —— P2 第二轮：同根残留

**抓到什么**：改完之后子进程测试用 `dict(os.environ, PATH="", *_ABS=…)` 起搭 ——
清了 `PATH` 和 `*_ABS`，**却继承了父进程的 `*_BIN`**，而 `find_tool` 的查找顺序是 `*_BIN` 优先。

**根因**：同一课没学透。「清干净环境」清了两样，漏了第三样。

**怎么修的**：改成从 `BLANK_TOOL_ENVIRON` 起搭 + 按复审建议拆成两条子进程测试
（占位名分支 / 绝对路径分支各有机器判据）。
顺带清掉**三处过期的 `ImportError` 兜底 `skipTest`** 和一处「形状不符就 skip」——
它们守的是接缝，**静默变 skip 等于把答案抹掉**。

**同轮还修掉一个真 bug（不是假绿，是真的会坏）**：**`--key` 会丢**。
`learn_default_flags` 把它当站点身份剔掉，`BUILTIN_DEFAULT_FLAGS` 又不许写死站点值
⇒ 端到端命令缺 `--key`，而官方那条有它。正解挪进 `core.cmd.build_flag_layers`。
配了 `_negative`：**`facts.key` 为空时不许凭空造** ——
宁可缺也不许编，编出来的 key 会让 run 直接失败且极难查。

**编排者在这里做了一个判断**：第三轮没有再委派，自己修的。
理由是残留已经是「一处忘清的环境变量」、机械可修；
「打回两次就冻结」那条规矩的目的是止住无界返工和防止降低测试强度，两者都没发生
（测试强度反而加强了：拆成了两条各有机器判据的子进程测试）。

### 打回 #3 —— P3：红区泄漏（本夜跑最严重的一次）

**抓到什么**：两个 8 位 Donau JOBID 被从 `references/ewave_donau_kit/` **逐字**抄进
`sched/donau.py` 的 docstring 样例和 `tests/test_sched_donau.py` 的解析样本，**共 41 处**。

**为什么算泄漏**：kit 的记录里那个 id 挂着一次真实运行的**日期和节点名** ⇒ 是站点身份。

**而当时闸门报 clean。** 根因：**没人事先想到「job id 也是站点身份」，词表里没有它。**

**怎么修的**：41 处全换成明显合成的 `10000001`–`10000004`（**形状照抄证据，值不许**）。

**后来怎么补的 —— 这是本夜跑最重要的一处工程改进**：
新增**第二道闸门** `scripts/redzone_crosscheck.sh`，**拿红区证据本身当词表反查**：

> 候选 = 红区资料里所有 `[A-Za-z]?[0-9]{7,}` 的 token；命中 = 这个 token 出现在会进 git 的文件里。

**这条不需要预知形状，所以抓得到我们还没想到的那一类。**
双向验过（修之前报红并点名 12 处，修之后 clean），已挂进 `check.sh` 第 3 步。
剔掉了「整数量级」（`1000000000` 这种单位换算）避免假阳性 ——
**假阳性多了闸门会被人关掉**，这个取舍是有意的。

**⚠️ 附带发现：一条 agent 自报被证伪。**
donau agent 在自己的 `key_tests` 里写「数字是测试里编的」，而那两个数字在 kit 里**逐字存在**。
这是本夜跑唯一一处被证明是假的报告陈述。
**⇒ 早上复核不要把任何 agent 自报当证据**（包括这份报告里我没标「我自己跑的」的部分）。

### 打回 #4 —— P4：红区泄漏（同一课第二次，源头换了）

**抓到什么**：`PROJECT_BRIEF.md` §10 的真实实测值被逐字抄进 3 个会进 git 的文件，**共 30 处**。

最扎眼的一处：fixture README 里有一张对照表，
**标题写着「别把右边那列写进 fixture」，而右列整列就是红区真值** ——
一张这样的表本身就是把那列写了进来。

**根因 —— 不是「规则不够严」，是「证据源覆盖不全」**：
P3 漏的是 kit 里的 job id，P4 漏的是 BRIEF 里的测量值。两次的共同点是
**crosscheck 当时只把 `references/` 当证据**。

**后来怎么补的**：证据源扩到 `references/` + `PROJECT_BRIEF.md` + `ENVIRONMENT.local.md`
（凡是「不进 git 的红区资料」都算证据）。双向验过。
这是**对闸门的改动、方向更严**，按 OVERNIGHT「不许碰」条款单列报备了。

**清掉的方式值得记**：保住「出处 / 为什么」，去掉数值本身 ——
所以 `logparse.py` 里现在满是「BRIEF §10 给了值（真值不复述）没给行」这种写法。

### 另外两个洞（不算打回，但同类）

- **P0 审查发现**：`redzone_scan.sh` 当时扫的是 `git ls-files`（**只含已跟踪文件**），
  而 Phase 0 的 10 个新文件全是 `??` 未跟踪 ⇒ **闸门对新写的代码是瞎的**。
  改成 `git ls-files --cached --others --exclude-standard`，现场双向验过。
- **编排者自己发现**：`PYTHONIOENCODING=ascii python -m ewave_batch dry-run --self-test`
  在修之前 **exit 1**（`UnicodeEncodeError`）。红区批处理里 `LANG` 常是 `C`
  ⇒ **开发机全绿的闸门到红区必红**，而那是最没法调试的地方。
  → `ewave_batch/_stdio.py` 的 `ascii_safe_stdio()` + `check.sh` 第 4 步加了一遍 ASCII 复跑。
  ⚠️ 值得记的细节：放 `_stdio.py` 而不是 `__init__.py`，是因为「包根零 import」那条惰性纪律
  **有测试守着，它当场把编排者拦下来了** —— 闸门对写它的人也一视同仁。

### 这四次的共同模式

```
四次里有三次，闸门在出事的那一刻是「绿」的。
  · 两次假绿：闸门和被测代码共享同一台机器的偶然状态（PATH 上有没有那个工具）
  · 两次泄漏：闸门的词表 / 证据源覆盖不到那一类值

补法也是同一个形状 —— 不是「把规则写得更严」，是「换一个不依赖我们预知形状的判据」：
  · 假绿  → 换环境复跑（*_ABS / *_BIN 两组），让闸门不再和被测代码共享状态
  · 泄漏  → 拿证据本身当词表反查（crosscheck），不需要预知形状

而 §1.1 那个还没处理的值，正好卡在这个模式的盲区里：
它是两位数，两道闸门的形状都够不着它。
```

---

## 5. 哪些地方没把握（逐条带「怎么验证」）

### A. 本机**没有**任何真实 `ewave.log` ⇒ P4 的 fixture 是**合成**的

`references/probes/` 里只有 help dump、`gdsout_setup`、workDir tree、step4 verify 输出 ——
**没有一份真实的运行日志**。所以 `tests/fixtures/ewave_log_synthetic/` 是：
**行格式照 BRIEF §10 逐字引用的那几行，数值全部自己编。**

代码里逐条标了「未经真实日志验证」的正则（我 grep 数出来 **11 处**）：

> 墙钟行 · 峰值内存行 · CPU 占用行 · 收敛行 · 版本行（两种写法）· warning 行 · 频点扫描行

其中最弱的是**峰值内存**和**CPU 占用**：BRIEF §10 只给了**值**，没给**那一行长什么样**
—— 正则完全是推的。

**怎么验证**：从红区抄一份真实 run 目录回来放 `tests/fixtures/ewave_run_real.local.d/`
（再抄一份 `.sNp` 放 `tests/fixtures/ports_real.local.s*p`），那两条 skip 的测试**自动生效**。
`.local.*` 不进 git（`.gitignore` 挡着），**抄回来是安全的**。

### B. `djob` 的真实回显格式**一次都没见过**

`sched/donau.py` 支持三种形状（JSON / 带表头的表格 / 裸状态词），认不出一律 `JobState.UNKNOWN`，**绝不猜**。
状态词表同时认 Donau 的拼法和 LSF 的同义词（`PEND`/`RUN`/`EXIT`），这样公司这个 fork 换个拼法不会静默落空。

**风险**：如果真实 `djob` 是第四种形状 ⇒ 全部 `UNKNOWN` ⇒ 每个 run 靠
`_UNKNOWN_POLL_LIMIT = 3` 拍之后的**产物兜底**判定。
**不会卡死**（这是有意设计的），但会退化成「不看队列只看磁盘」——
队列里正常排队的 job 也会被当成「查不到」。

**怎么验证**：红区提交一个 job 之后手敲一次 `djob`，把回显贴回来。
**这是最便宜、收益最高的一条验证。**

### C. `dsub` 不加 `-I` 到底阻塞还是异步返回，**未决**

生产脚本用的是 `-I`（阻塞），而我们**故意不用** ——
`BLOCKING_FLAGS` 会当场拒绝用户粘进来的 `-I` 并说人话，因为 driver 要的是
「提交完立刻返回，靠 poll 收割」。

**风险**：如果这个站点的 `dsub` **不加 `-I` 也阻塞**，那么 `driver.tick()` 会卡在提交上
⇒ **有界并发失效，整批退化成串行**。症状不是报错，是「跑得莫名其妙地慢」。

**怎么验证**：红区提交一个 job，看 `dsub` 是立刻回 job id 还是等到作业结束才回。

### D. 作业名 flag 未知 ⇒ 提交出去的 job **没有名字**

`NAME_FLAG = ""`（空串 = 不发）。Donau 用什么 flag 起作业名没实测，而 `-J` 已经被 `--json` 占了。
取舍是「宁可不给作业名，也不猜一个 flag 塞进去」——
猜错的后果是每次提交都带一个 dsub 不认识的参数，**而错误只在红区才看得见**。

**代价**：`djob` 列表里认不出哪个 job 是哪个 run，只能靠 job id 对。批次大的时候人肉排查会难受。

### E. 一批**拍的数字**（全部没有实测依据）

| 常量 | 值 | 在哪 | 拍错了会怎样 |
|---|---|---|---|
| `_UNKNOWN_POLL_LIMIT` | 3 | `sched/driver.py` | 太小 → 队列慢时误判去验产物；太大 → job 被队列忘掉后多等几拍 |
| `_STALL_TICK_LIMIT` | 5 | `sched/driver.py` | 卡死保险丝。只在「一个 job 都不在飞」时计数（这个设计是对的：真跑时排队几小时不会误杀） |
| `max_parallel` | 4 | `model.py` | **纯拍的**。红区队列配额多少不知道 |
| `poll_interval` | 15.0 秒 | `model.py` | **纯拍的** |
| `_CDS_LIB_SEARCH_UP` | 4 | `sched/driver.py` | 往上找几层 `cds.lib`。官方布局是往上两层，多找两层是给别的站点留余量 |
| `RAW_KEEP_CHARS` / `_MAX_MESSAGE_CHARS` | 2000 / 1200 | `donau.py` / `driver.py` | 只影响 `batch.json` 体积 |

这些都在源码里集中定义、带 docstring 说明理由，**改一个数就行**，不用改逻辑。
`poll_interval` 和 `max_parallel` 还能从 CLI 覆盖。

### F. 合成 fixture 只证明「解析器认得这个形状」，**不证明「这形状与红区当前版本一致」**

`tests/fixtures/offdir_synthetic/` 和 `tests/fixtures/ewave_log_synthetic/` 都是我们自己造的：
**形状照红区证据、值全假**（`MY_LIB` / `MY_CELL` / `FAKEPDK` 这种一眼假的占位符）。

**这句话适用于本夜跑几乎全部的「验过了」。**
962 条单测证明的是内部一致性和形状识别，**不是**「跟红区当前那个 eWave 版本对得上」。
那一步只能在红区做。

### G. **PyYAML 本机没装 ⇒ YAML 解析路径本机一次都没执行过**

我确认过：`python -c "import yaml"` 在本机 ImportError。所以
`tests/test_spec.py::ExampleSpec::test_example_spec_parses` 是那 3 条 skip 之一。

**红区装了 PyYAML 6.0.1 ⇒ 那条路径在红区是第一次执行。**
结构性检查（`test_structure_lint`）本机跑过，JSON 退路也跑过，
但「PyYAML 真的能吃这份 YAML」没验过 —— 而用户手写 spec 正是走这条路。

**怎么验证**（红区，30 秒）：

```tcsh
python -c "import sys; from ewave_batch.core.spec import EXAMPLE_SPEC; sys.stdout.buffer.write(EXAMPLE_SPEC.encode('utf-8'))" > my_spec.yaml
python -m ewave_batch dry-run --spec my_spec.yaml
```

### H. `deploy.sh` 只在 Windows Git Bash 上跑通，**没有任何真实 Linux 验证**

`sed -i` / `readlink -f` / `ls -1dt` / `date +%Y%m%d-%H%M%S` 在 RHEL 上都是 GNU coreutils，
应该没问题，但**没实测**。目标机是 Linux。

有意思的一条：脚本头注释说「`mv` 掉正在运行的自己在 Windows 上会失败」，
而本机 Git Bash 上完整 deploy **RC=0** —— 那条注释在本机是过于悲观的，按 Linux 语义写没错。

### I. `pack.ps1` 的**成功**路径只在一次性快照 repo 里走过

`pack.ps1` 要求工作树全部已提交，而夜跑规矩是 agent 不 commit ⇒
直接在项目仓库跑会（正确地）停在 `deploy.sh is not tracked by git`。
P6 agent 在 scratchpad 里造了个一次性快照 repo 才走通了完整路径。

**所以**：`VERSION` 的 `export-subst`（打包时替换成真实 commit hash + 时间）
只在那个快照 repo 里验过。**真正 push 到 GitHub → 黄区 clone → 打包**那一跳没人走过。

**怎么验证**：早上 commit 之后**真跑一次 `pack.ps1`**，拿真实输出校一遍
`docs/REDZONE_FIRST_RUN.md` 步骤 2 的「你应该看到」那段 ——
那段现在是从 `pack.ps1` 的 `Write-Host` 行**逐字转写**的，**不是观测到的**。
（同理，`REDZONE_FIRST_RUN.md` 里 doctor 的 tier 2 那几行也是按红区形状写的，
本机只观测过 tier 1 / `NOT AVAILABLE` 分支。）

### J. 那次没能复现的红 —— 见 §1.3

### K. 红区**没有 git** ⇒ 四条开发侧闸门脚本被 `export-ignore` 掉了

**我自己复现过这条**（因为它是一处不小的偏离，而且任务书原本以为那些脚本会「优雅跳过」）。
把三个脚本 copy 到一个非 git 目录里跑：

```
check.sh              rc=1   只打 "fatal: not a git repository (or any of the parent directories): .git"
redzone_scan.sh       rc=1   只打 "not a git repo"
redzone_crosscheck.sh rc=1   只打 "not a git repo"
```

**P6 agent 的说法成立**：那条「优雅跳过」只覆盖「没有 `references/`」，
而脚本第一句就是 `git rev-parse --show-toplevel`。装过去就是
「敲一句 `sh scripts/check.sh` 得到没头没尾的 exit 1，以为装坏了」= **假红**。

**但要知道后果**：红区**没有 `check.sh`**。
红区侧的等价物是 `bash deploy/doctor.sh --test`（跑装好的整套单测）。
如果你希望闸门脚本进包，正确修法是给那三个脚本的 `git rev-parse` 加一条
「不是 git 仓库就退 0 并说明」的回退，然后删掉 `.gitattributes` 里那四行 —— **不是**把它们硬塞进去。

### L. 逐 flag 对比里有 4 个 flag 是**循环论证**

dry-run 的 `[4/5] a)` 会自己分类报出来（下面是我对着合成 fixture 跑的实际输出）：

```
参与比较   20 项
一致       20 项
有意不同    4 项  --all --gds --includePortOrder --workDir   （每项都注明了为什么必然不同）
同源       4 项  --key --labelDepth --sparamImpedance --viaMode
                  ↑ 结构上必然相等，不构成验证
独立验证   16 项  取值另有来源（推导 / 常量 / 从别的文件学的）
```

**那 4 个「同源」的是自己证明自己**：它们的值是从官方 run 目录学来的，又拿官方 run 目录去比。
红区真跑一次才有意义。

这个分类是 dry-run **主动报出来的**，没有藏在总数里 —— 这点做得对，
但读报告的人要知道：**真正有说服力的是「独立验证」那个数，不是「一致」那个数。**

### M. `--all` 与接地端口 —— 见 §1.2（**这是结果正确性上最大的一个未知**）

---

## 6. 设计偏离（选了哪个解释、为什么、改判代价）

### 编排者定的 5 条

1. **目录布局**：BRIEF §5 画的是顶层 `core/`，实际做成 `ewave_batch/core/` + 顶层 `gui/` + 顶层 `cli.py`。
   依据：`scripts/check.sh` 跑的是 `python -m ewave_batch` 和 `python -m gui.frames.<v>`，
   §12 的部署布局也写的是 `ewave_batch/`。**可执行判据 > 示意图。**
2. **红区 dry-run 入口落在 P2 而不是 §12 说的 P1**：它要「坐标全部现场解析」，结构上依赖 `core.discover`，
   而 discover 排在 P2。**交付点没变**（今晚就能拿去红区跑），只是阶段标签不同。
3. **并行 agent 一律不改 `model.py`**：OVERNIGHT 原文设想的是串行，
   一个阶段 2–3 个 agent 并行时同时改必然互相踩
   ⇒ 改成「agent 报备 → 编排者在阶段之间串行走 `[interface-change]`」。
   **实际发生了 3 次，全部按这条处理，零次静默漂移**（self-test 也会当场抓）。
4. **`cmd.sh` → `cmd_<corner>_<temp>.sh`** —— **唯一一处对 BRIEF §5 那棵树的偏离**。
   原因：`<axes-slug>` 按定义不含 corner/temp ⇒ 同一个 `run_dir` 下住着 N 个 run
   ⇒ N 条命令往同一个文件写、只剩最后一条
   ⇒「这个 `.sNp` 是拿什么命令跑出来的」永远答不上来。
   形状照官方（`run_ewave_<corner>_<temp>.sh` 本来就是 `<corner>_<temp>/` 的同级兄弟）。
   理由写在 `model.CMD_SH_TEMPLATE` 的 docstring 里。
   ⚠️ **同一个坑在 `5f9c5f5` 又发作了第二次**（`cmd.py` 里另有一份写死的 `run.log`，
   而它被 `dsub -o` 用作 job stdout ⇒ N 个 job 的输出混进一份日志）。
   现在焊了一条跨模块断言：逐 run 断言 `basename(plan.log_path) == basename(RunPaths.run_log)`。
5. **「审查打回两次就冻结」的执行**：P2 到了第二轮，编排者判定残留是机械可修，
   **自己修掉而不是发起第三轮委派**（理由见 §4 打回 #2）。

### P6 部署路报备的 6 条

6. **`export-ignore` 掉四条开发侧闸门脚本** —— 见 §5-K，**我复核过，理由成立**。
   逐条列名而不是写 `scripts/`，保住了「新加的脚本自动进包」这条零维护性质
   （同夜另一个 agent 的 `scripts/redzone_bundle.sh` 确实自动进了包）。
7. **`doctor` 的退出码门槛定在 tier 1，与兄弟项目 SNP 的 tier 2 不同**。
   理由：SNP 那边 numpy 缺了就真的什么都算不了，而这边 **tier 1 已经能干活** ——
   解析真实官方目录、拼全部命令、dry-run、跑整套测试，「第一趟去红区试 dry-run」要的正好就是 tier 1。
   `dsub`/`ewave`/`strmout` 要先 `ma` 站点 EDA 模块才在 PATH 上，
   门槛定在 tier 2 会让一个干净的登录 shell 报成失败 = **喊狼来了**。
8. **tier 2 的硬条件是四个工具而不是 BRIEF 表里点名的三个：多了 `djob`**。
   理由：提交用 `dsub`、轮询用 `djob`；没有 `djob`，driver 永远看不到 job 离开队列，
   每个 run 卡在 pending。「能提交不能轮询」不是一个能干活的 tier 2。
   **改判代价**：改 `_env_check.TOOLS_FOR_SUBMIT` 一个元组即可（真值表里有一格专门覆盖这个分支）。
9. **修了两个从 SNP 形状继承来的真 bug**（`deploy.sh` 的空 glob 陷阱）。
   `shopt -s nullglob` 打开时 `ls -1t "$TARGET"/*.tar.gz` 无匹配会展开成**零个参数**，
   于是 `ls` 列的是**当前目录**。更严重的是交付包轮换那段用了同一个写法，
   后面跟着 `rm -f "$_p" "$_p.sha256"` ⇒ 可能删当前目录里的文件。
   两处都改成「先用 nullglob 收集成数组，再显式喂给 ls」。
   **⚠️ SNP 那边这两个洞还在 —— 值得回头告诉那个项目。**
10. **加了一段 SNP 没有的「孤儿告警」**：换装成功后列出备份里「新包并不提供」的顶层名字。
    这是整条链路上**唯一能吃掉真实工作成果的地方**，
    而默认 `batch_root` 是 `./ewave_batches` ⇒ 很容易正好落在安装目录里，
    再部署 3 次就被备份轮换静默删掉。
11. **碰了不在清单里的文件**：给 `tests/test_gate_portability.py::RedzoneScanFileSet` 加了
    `setUpClass` 平台跳过（缺 git 或缺闸门脚本就带原因 skip）。
    原因是模拟红区安装时实测到的：那三条要建临时 git repo 并调开发侧闸门脚本，而红区两样都没有
    ⇒ `doctor.sh --test` 会因为三条**与安装质量无关**的红而整体 FAIL。
    开发机上它们照跑（实测一条没跳），**闸门的牙一颗没少**。
    改判代价：删掉那个 `setUpClass` 即可，deploy 侧不受影响。

### P6 红区首跑路报备的 5 条

12. **bundle 日志落点是 `.deploy/redzone_bundle/<时间戳>/` 而不是 BRIEF §12 画的 `.deploy/tmp/`**。
    实测踩到的真冲突：`deploy/doctor.sh` 开头 `rm -rf $ROOT/.deploy/tmp`、退出 trap 里再删一次，
    而 bundle 第一步就调 doctor ⇒ **日志跑到一半消失**。
    已为这条写了回归测试 + **前提测试**（断言 doctor.sh 里确实还有那两处 `rm -rf`，
    前提没了会先红、提醒人回来重新判断）。
13. **多写了 `tests/test_redzone_bundle.py`**（19 条）。理由：`redzone_bundle.sh` 会随包发到红区、
    是红区用户敲的第一条命令，而它此前**零自动覆盖** —— 一个语法错 = 早上交出去一块砖。
14. **bundle 加了 `--check-only`**（原任务没要求）：两秒确认「这条命令会不会跑起来」，
    并让端到端测试能走同一条入口而不递归（bundle 第 2 步跑的就是 `unittest discover`，
    在测试里跑它会无限递归）。
15. **bundle 输出用中文 + ASCII 判据词**，没跟 `cli.py` 的「界面语言 = 英文」先例。
    理由：它包住的正主 `redzone_dryrun` 报告本身就是中文，套层英文壳会让同一屏两种语言。
    折中是所有**判据**词（OK / FAIL / 退出码数字）都是 ASCII，终端渲染不了 UTF-8 时结论仍读得出来。
16. **bundle 退出码优先级 `5 > 4 > 6 > 3 > 2 > 0` 是拍的**。
    只实测验证过「4 盖过 3」，其余组合没造出来验。

### P4 报备的 1 条（**悬而未决**）

17. **撤掉了一条自己加的词表规则** —— 见 §1.1。
    撤回的判断是对的（那条规则会逼人去改「不许碰」的 `mockups/`），
    **但撤回之后那 48 处没有人处理**。
    这是本夜跑唯一一处「发现了、判断了、然后就悬在那儿」的事。

---

## 7. 红区第一次跑该看什么

**照着 `docs/REDZONE_FIRST_RUN.md` 敲**（每步都写了「你应该看到什么」和「不是这样怎么办」）。
三十秒版：

```tcsh
cd <workarea>/ewave_helper
bash deploy.sh
ma python/3.11.4
bash deploy/doctor.sh --test
bash scripts/redzone_bundle.sh <OFFDIR>
```

`<OFFDIR>` = 官方 GUI 跑过一次的那个 design 目录（**判据：那个目录里有 `gdsout_setup`**）。
整条链路**只读 `<OFFDIR>`、不提交任何 job**。

### 最该盯的三件事 —— 全在 dry-run 报告的 `[4/5]` 那一节

#### ① `a) 逐 flag` —— 最后一行是不是「完全一致」

```
参与比较   N 项
一致       N 项
有意不同    4 项   ← --all --gds --includePortOrder --workDir，每项都注明了为什么必然不同
同源       4 项   ← 循环论证，不算验证（见 §5-L）
独立验证   M 项   ← 这个数才是真正的把握程度
```

**盯什么**：`一致` 是不是等于 `参与比较`。**任何一项对不上都要停下来看** ——
那意味着我们拼出来的命令和官方那条**语义不同**，跑出来的 `.sNp` 就不能拿来跟历史结果比。

⚠️ 别被总数糊弄：真正有说服力的是 `独立验证` 那个数。

#### ② `b) 端口顺序（D1b）` —— 是不是「逐位一致」

这是**整个「不依赖 GUI」成立的全部依据**：`--all` 输出的端口顺序必须逐位复现官方 `-p` 的顺序
（理论依据是 pin 名的 case-sensitive ASCII 排序，见 `references/checks/check_port_order.py`）。

**盯什么**：报告会打「逐位比较 N 个端口 / 逐位一致」。
**这一条不吻合 = 整个工具的前提塌了** —— `.sNp` 的端口映射会错位，
而错位的 S 参数**看起来完全正常**，是最贵的一类错。

⚠️ **同时看这一节末尾那条警告**（§1.2）：
如果官方 `-i` 覆盖的 signal port 数**少于**总端口数，说明有接地端口，
`--all` 会把它当 signal ⇒ **顺序对了内容仍可能不同**。

#### ③ `c) gdsout_setup（D1c）` —— 那 8 个关键字段在不在

```
逐字自检     共 N 个字段，其中 K 个是 design 相关的
模板化清点   8 个 D1c 关键字段（源码常量 vs 现场站点值）
```

**盯什么**：两行都要是「一致」。
第一行说的是「模板化 + 渲染没有丢掉任何原有字段」，
第二行说的是「源码里那份兜底模板对这个站点也是对的」。

⚠️ 报告自己声明了一个局限（诚实，但要读懂）：
**逐字自检是拿现场文件模板化出来的，所以它证明的是「没有丢字段」，
不证明「我们的兜底模板和官方一致」。** 后者靠第二行那 8 个字段的清点。

---

## 8. 失败了怎么办

按**卡在哪一步**分：

| 症状 | 多半是什么 | 下一步 |
|---|---|---|
| `bash deploy.sh` 死在 `$'\r': command not found` | 包是 CRLF 打的 | `.gitattributes` 的 `eol=lf` 没生效 —— 在**黄区**重新 clone 一次再打包（不要在红区改文件） |
| `deploy.sh` 说 `not an Ewave_helper install` | 解压路径不对，或指错了目录 | 确认目标目录里有 sentinel 文件。这是**有意的守卫**：非安装目录一个字节都不许动 |
| `deploy.sh` 说 `no *.tar.gz found` | 包没上传到位 | 确认包和 `.sha256` 都在目标目录里 |
| `doctor.sh` 停在 **tier 0**、`IMP_*` FAIL | 包坏了，或 Python 版本不够 | 先 `sha256sum -c *.sha256`；再确认 `ma python/3.11.4` 已加载。⚠️ tier 0 时四个 `TOOL_*` 一律报 MISSING —— **那是探测被跳过（依赖包能 import），不是工具没装** |
| `doctor.sh` 报 **tier 1** | **正常，不是装坏了** | tier 1 就能跑 dry-run 和整套测试。要提交 job 才需要 `ma` eWave/Donau 模块升到 tier 2。doctor 会专门打一段说明 |
| `doctor.sh --test` 有红 | 见 §1.3 和 §5-G | 如果红的是 `test_cli.py::LazyImport` → §1.3 那条偶发不是偶发；如果红的是 spec YAML → §5-G 那条路径在红区第一次执行 |
| bundle 说「目录里没有 `gdsout_setup`」 | `<OFFDIR>` 指错了一层 | 报告会明说往上还是往下挪一层。这是 `REDZONE_DRYRUN.md` 点名的**头号错误** |
| dry-run `[4/5] a)` 逐 flag **不一致** | 站点的官方脚本形状和我们解析的不一样 | **停下来。** 把不一致那几项 + `<OFFDIR>` 里的 `run_ewave_*.sh` 贴回来（**注意脱敏**）。不要靠改 flag 硬凑 |
| dry-run `[4/5] b)` 端口顺序**不吻合** | D1b 的前提不成立 | **停下来，这是最严重的一种** —— 整个工具的前提塌了。贴回逐位对比那段 |
| dry-run 报「官方的 `-i` 只覆盖 N/M 个端口」 | 有接地端口（§1.2） | 不是错误，是**需要你决定**：spec 里显式写 `ports`，还是确认接地端口对结果无影响 |
| 真跑时每个 run 卡在 pending 不动 | 多半是 `djob` 回显认不出（§5-B） | 手敲一次 `djob` 把回显贴回来。driver 有兜底（3 拍后去验产物），**不会永远卡死**，但会变慢 |
| 真跑时整批变成串行、很慢 | `dsub` 不加 `-I` 也阻塞（§5-C） | 手工试一次 `dsub`，看它是否立刻返回 |
| 日志解析出来一片空 | P4 的正则形状不对（§5-A） | **把一份真实 run 目录抄回来**放 `tests/fixtures/ewave_run_real.local.d/` —— 那两条 skip 的测试会自动生效并告诉我们哪条正则不对 |
| 中文输出全变成 `?` | `LANG=C`（**设计行为，进程不崩**） | 想看中文：`setenv LANG en_US.UTF-8`。⚠️ **别**用 `PYTHONIOENCODING=utf-8` 去跑单测（会让子进程测试炸），只对 dry-run 用 |
| 结果目录被覆盖了 | 不该发生（每组合独立 `--workDir` 就是为了这个） | 这是本工具存在的理由 —— 真发生了请把 `batch.json` 和目录树贴回来，那是个严重 bug |

**通用原则**：红区那边**任何贴回来的东西都要先脱敏**（库名 / cell 名 / 端口名 / 路径 / 账号 / 队列）。
这个仓库要上 public GitHub；`references/` 和 `*.local.*` 是唯一允许放原始证据的地方，它们都不进 git。

---

## 9. 早上的收尾清单

- [ ] **`sh scripts/check.sh` 自己跑一遍**（别信这份报告，包括我贴的那些数）
- [ ] **处理 §1.1 那 48 处** —— 三个选项已列，需要你拍板。
      这是唯一一件「已经知道有问题、但还没做」的事
- [ ] **commit P6 那 9 个文件 + 这份 `docs/HANDOFF.md`**
      （夜跑规矩 agent 不 commit，而 P6 之后没有审查轮次 —— 这 10 个文件没人复核过）
- [ ] commit 之后**真跑一次 `deploy\pack.ps1`**，拿真实输出校 `REDZONE_FIRST_RUN.md` 步骤 2 那段（§5-I）
- [ ] push 前再扫一遍历史：`git log -p | grep -oE '\b[A-Za-z]?[0-9]{7,}\b' | sort -u`
      （我今早扫过，**当时干净** —— 命中项全是 git blob 短哈希和合成值）
- [ ] 顺手告诉 `SNP_RLC_Extractor` 项目那两个 nullglob 洞（§6-9）
- [ ] 红区那趟带回来两样东西，收益最高：
      **一份真实 run 目录**（喂 §5-A 那两条 skip）和**一次 `djob` 的回显**（§5-B）
