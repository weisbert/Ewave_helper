# Ewave_helper

在公司 **eWave 官方 GUI 外面**加一层批量驱动：用户定义若干提取设定的组合，一次性批量跑，
结果自动归档，后续可批量对比不同设定下同一参数的差异。

eWave 是公司的商用 3D EM 场求解器（抽电感/走线用）。官方 GUI 一次只能配一个设定、
输出落默认目录、要的文件得手动 copy —— 这三件事由本工具接掉。

**状态（2026-08-18）：设计已谈完，进入实现。**
参数暴露面见 `PROJECT_BRIEF.md` §11，实现计划与部署方案见 §12。
§9a 那句「四块模块先别动」是 08-17 的状态，**已被 §12 取代**。

---

## 权威文件在哪（别在别处重复记）

| 文件 | 内容 | 进 git？ |
|---|---|---|
| **`PROJECT_BRIEF.md`** | **唯一的设计权威。** 需求、已拍板的决定 D1–D14、架构、复用清单、未决项 P2–P9 | ❌ 里面有真实端口名 / ptxt PDK 串 / 红区实测值；`.gitignore` 第 4 行排除它，`redzone_crosscheck.sh` 拿它当证据源 |
| `ENVIRONMENT.local.md` | 三套环境对照、红区坐标、Donau 硬规则、可用库清单 | ❌ 含工号/主机名/内网路径 |
| `references/probes/` | **红区取回的原始证据**（help dump、gdsout_setup、workDir tree、真实运行脚本） | ❌ 红区 |
| `references/checks/` | 支撑设计决定的可复现验证脚本（纯 stdlib，Windows 直接跑） | ✅ |
| `references/ewave_donau_kit/` | 从 VM 搬来的先期资料包（eWave 知识 + Donau 调度实现） | ❌ 红区 |

**开工前先读 `PROJECT_BRIEF.md` §4 的决定表**，那里每条都带理由，理由往往比结论重要。

---

## 不可违反的硬约束

1. **`references/probes/`、`references/ewave_donau_kit/`、`ENVIRONMENT.local.md` 是公司红区资料。**
   只在用户自己的机器之间流转。不推 GitHub/网盘/IM，不发布成 Artifact，不把里面的
   工号/主机名/项目代号/cell 名/端口名贴进网络搜索或第三方服务。

1b. **本项目自己的代码将来要上 public GitHub（用户 2026-08-17 决定）→ 源码里零红区标识符。**
   这条是 #1 的推论，但方向相反、要主动做：**不是"别把红区文件提交上去"，而是"写代码时
   就不许出现站点坐标"**。具体：
   - 工号/主机名/项目代号/workarea 路径/library/cell/view/端口名/PDK 版本串/ptxt 路径/
     Donau 账号与队列/工具绝对路径 —— **一律不进源码**。
   - 走两条路取代：**site-local 配置**（`site.example.sh` 进 git，`site.local.sh` 不进）
     或 **运行时发现**（从官方 run 目录解析、`command -v`、环境变量）。
   - 运行时发现优于配置项：官方 run 目录里本来就有全部坐标（`gdsout_setup` →
     library/topCell/view/layerMap；`run_ewave_*.sh` → ptxt/key/corner/temp/端口表；
     `remote_run_ewave.sh` → dsub 三元组），既没有标识符进仓库，也没有手抄错的可能。
     `mvp/redzone/cfg.sh` 是这条路的样板。
   - **通用的东西可以进源码**：eWave 的 flag 名和数值（`-e 0.4`、`--viaMode=1`…）、
     gdsout_setup 的非路径字段（`maxVertices 200`、`convertPin geometry`…）——
     那些是工具语义，不是站点身份。
   - 红区资料本身（`references/probes/` 等）仍然**永不进 git**，公开与否都一样。
2. **依赖只用 stdlib。** 红区无装包权限（视为只读）。核心（矩阵/命令/归档/日志解析/调度）
   本来就不需要 numpy。GUI 用 stdlib 的 **tkinter** —— 红区虽然装了 PyQt5，但
   ①Virtuoso 会往 `LD_LIBRARY_PATH` 塞冲突的 `libQt5Core.so.5`，②要对接的
   `C:\code\SNP_RLC_Extractor` 是 tkinter。**不要换 GUI 框架。**
   唯一例外：读用户手写的 YAML spec 时用 PyYAML（红区已装 6.0.1），惰性 import + JSON 退路。
3. **没有任何真实本地调试的可能。** Windows 本机和本地 Linux VM 都没有 `ewave`、没有 `dsub`、
   没有 `strmout`。所以：纯逻辑核心 + 可注入 runner + 全程 `--dry-run` + golden 命令测试。
   真实验证只能在红区，由用户执行。**不要写"跑一下看看"这种依赖真实工具的验证步骤。**
4. **绝不写进设计师的 spine。** `<workarea>/ewave_simulation/` 只读（那是官方 GUI 的地盘）。
   唯一例外是显式触发的「把这个 run 设为当前」（覆盖前备份 + 记日志），见 BRIEF §5。
5. **GUI 的 import 必须惰性**、只在 GUI 分支执行，保证无 `$DISPLAY` 的纯 ssh 会话里 CLI 可用。

---

## 三行心智模型

```
用户输入：designs = [(Library, Cell, view), …]
          +  设定轴 {corner, temperature, equalCurrent, fullWave, …}
          +  run groups = [base, 变体1, 变体2, …]      每组只写它覆盖的轴，其余继承 base

组合 = 每个 group 各自取笛卡尔积，结果取并集（跨组重复按 run_id 静默去重）

阶段 1（per-design）      strmout -templateFile <渲染出的 gdsout_setup>   →  <Cell>.gds
阶段 2（per-design×组合） dsub … ewave --all … --corner=… --temperature=…  →  .sNp  →  归档
```

**「组合」不等于「全批次笛卡尔积」**（用户 2026-08-19 拍板，BRIEF D14）。
用户真正要的是「一条基线 + 几个单点变体」：`typical @ {-40, 55, 125}` 再加两个只在 55 度上
各改一根轴的变体 = **5 个 run**；纯笛卡尔积最接近的写法是 12 个，7 个是废的，
而一个 run 的量级是 10 核 / 100 GB / 35 分钟。不写 `groups:` 时行为与从前逐字相同。

三个最容易搞错的点：

- **端口映射不在 `.sNp` 里，在命令行里**（靠 `-p` 的顺序）。而官方那个顺序就是
  pin 名的 case-sensitive ASCII 排序 —— 所以 `--all` 能逐位复现它。这是"不依赖 GUI"
  成立的全部依据，证据在 `references/checks/check_port_order.py`。
- **`<corner>_<temp>/` 那层子目录是 eWave 自己建的**，名字只由 corner+temp 决定 →
  同 corner/temp 下换别的 flag 会**静默覆盖**。这正是用户的核心痛点，解法是给每个组合
  独立的 `--workDir`。
- **`<axes-slug>` 只编码「在变的轴」，而「在变」的口径是全批次的** ——
  全局轴 ∪ per-design 覆盖 ∪ **run group 覆盖**，三个来源缺一不可。
  推论很反直觉但必须接受：**加一个组会改掉基线的目录名**（某根轴从"不变"变成"在变"，
  它就对所有 run 进 slug）。这是对的 —— 不改的话两个组的同一个温度会落进同一个
  `--workDir`，就是把上一条那个静默覆盖原样重造一遍。
  代价是给**已经跑过**的批次加组 = 换了一批 `run_id`，resume 认不出老的 `base/...` 目录。

---

## 环境速查

| | 红区（部署目标） | 本地 VM | 本机 Windows |
|---|---|---|---|
| 用途 | 唯一能真跑的地方 | 只有 Cadence IC618/Spectre，无 eWave/Donau | 全部开发在这里 |
| shell | **csh/tcsh**（`\|&` 不是 `2>&1`，`$status` 不是 `$?`） | bash | PowerShell 5.1 |
| Python | 3.11.4（`ma python/3.11.4`） | 3.11.13 | 3.10.11 / 3.13 |
| 访问 | 用户手动执行，粘贴回来 | `ssh ewave-vm`（免密） | — |

**版本方向性**：本地 Python 比红区新 → 风险是"本地用了红区没有的新 API 而本地测不出来"。
以红区 3.11.4 为准。

---

## 兄弟项目（都在 `C:\code\`，抄它们的经验）

| 项目 | 抄什么 |
|---|---|
| `SNP_RLC_Extractor` | 部署骨架（`deploy/pack.ps1`+`doctor.sh`+`_env_check.py`）、红区环境权威快照、**v2 的对接对象**（= 用户说的 snp_analyze） |
| `Auto_ext` | `tools/base.py` 的 `run_subprocess`（cancel token + 独立读线程 + 逐行 flush）**直接可抄**；`Tool` ABC 插件结构；`config/tasks.yaml` 的笛卡尔积 spec 格式。⚠️ 但它的 `strmout.py` 裸 argv 形式**不能照抄**，见 BRIEF D1c |
| `LDO_modeling` | Donau 集群先例；skillbridge 踩坑记录 |
