# deploy — 过气隙的部署链路

红区是一台隔离的 Linux：**无网、无 git、无 pip、无 venv**。所以它 `git pull` 不了，
也装不了任何东西。这条链路把**已提交的代码**打成一个普通 tarball 送过去，
到了那边再告诉你「这台机器到底能跑到哪一档」。

形状**照抄 `C:\code\SNP_RLC_Extractor/deploy/`**（用户 2026-08-18 指定）——
两个工具的部署手感不该分叉，肌肉记忆是一套。

```
家里 Windows ──git push──▶ GitHub(public) ──clone/pull──▶ 黄区 Windows
                                                              │  deploy\pack.ps1
                                                              ▼
                                              ewave_helper_<short>.tar.gz + .sha256
                                                              │  上传两个文件
                                                              ▼
                                              红区 Linux：bash deploy.sh
                                                          bash deploy/doctor.sh --test
```

`pack.ps1` **在黄区跑**（只要 git + PowerShell）。家里这台只负责 push ——
这样包名里的 `<short>` 就是 GitHub 上那个 commit 的短哈希，
「红区装的是哪一版」有一个词的答案。

包由 `git archive` 生成，所以它 **100% 无 git**（没有 `.git/`、没有
`.gitattributes`/`.gitignore`、没有 `CLAUDE.md`、没有 `pack.ps1`、没有 `mockups/` `mvp/`
—— 全在 `.gitattributes` 里 `export-ignore` 掉了），并且免疫经典的 Windows→Linux 陷阱：
路径永远是 `/`，文本是 LF（读的是**已提交的 blob**，不是 Windows 工作树），exec 位保留。

**进包的规则是黑名单，不是白名单。** 仓库里的东西默认全部过气隙，除非被
`export-ignore` —— 所以你以后新加的模块和测试**自动进包**，这条链路零维护。

**永远不往安装目录外面写。** 没有 `/tmp`、没有 `/opt`、没有 `/var`、不 `mktemp`。
全部 staging / backup / scratch 都在 `<install>/.deploy/` 下面。**父目录永不修改。**

## 红区布局

```
<workarea>/ewave_helper/              ← 安装目录；叫什么名字都行
├── deploy.sh                         ← 更新入口（故意放最外层）
├── ewave_helper_<short>.tar.gz       ← 你把包传到这里
├── ewave_helper_<short>.tar.gz.sha256
├── cli.py  VERSION  README.md
├── ewave_batch/  gui/  tests/  docs/  references/checks/
├── deploy/{doctor.sh, _env_check.py, README.md}
└── .deploy/                          ← 全部运行时状态，永不离开这台机器
    ├── incoming/                     # 上传来的 tarball + .sha256
    ├── staging/                      # 换装前的完整解压
    ├── backups/<timestamp>/          # 上一版安装（留最近 3 份）
    ├── tmp/                          # scratch（doctor.sh）
    └── preserve.list                 # 可选，见下面「保住你自己的数据」
```

安装目录**不必**叫 `ewave_helper` —— `deploy.sh` 把「自己所在的目录」当安装目录，
包根名从压缩包里自动认。两边随便改名。

## 1. 黄区 Windows —— 打包

`git pull` 到最新之后：

```powershell
powershell -ExecutionPolicy Bypass -File deploy\pack.ps1
```

在 `deploy\dist\` 下产出**恰好两个**文件：

| 文件 | 用途 |
|---|---|
| `ewave_helper_<short>.tar.gz` | 整个安装（代码 + 测试 + 文档） |
| `ewave_helper_<short>.tar.gz.sha256` | 到了那边验完整性 |

两个都传过去。整个交付就这些 —— 故意没有别的东西要 copy、也没得挑。

`-Name <dir>` 可以改包根目录名（默认 `ewave_helper`，也就是 `tar -xzf` 出来的那个目录）。

只需要 **git + PowerShell**（不需要 Python，不需要外部 tar）。它打的是**已提交的 `HEAD`**
—— 没提交的改动**不会**进包（会给你一条 warning）。

它还会**预检 shell 脚本的行尾**：把 `deploy.sh` 和 `deploy/doctor.sh` 单独 archive 出来，
扫原始字节找 CR。只要包会以 CRLF 落地就**当场中止打包**。
理由：那一颗雷会让红区的 bash 死在 `bash: $'\r': command not found`，
而红区正是你最没法调试的地方 —— 所以在这边抓，不在那边抓。

## 2. 红区 —— 部署

把 tarball **和它的 `.sha256`** 一起传进安装目录，然后：

```tcsh
cd <workarea>/ewave_helper
bash deploy.sh
```

不用带参数 —— 它自己挑安装目录里**最新**的那个 `*.tar.gz`，并打印挑了哪个
（以及忽略了哪些，如果躺着好几个）。想指定就把路径当参数传。

> 用 **`bash`** 起，不要 `./deploy.sh`：红区登录 shell 常是 **tcsh/csh**，
> 上传通道还可能吃掉 exec 位 —— `bash` 两样都不需要。
> 当**脚本**跑，别 `source`。

它会验 sha256 → 解压到 staging → 把当前安装**备份**到 `.deploy/backups/<timestamp>/`
（留最近 3 份）→ 原地换装。**只碰安装目录，父目录永不修改。**
换装中途任何一步失败都会自动回滚。

回滚**区分两个阶段**：「备份阶段失败」（备份不完整 ⇒ 还在原地的文件是原件的唯一副本，
不许删）和「安装阶段失败」（备份完整 ⇒ 新文件可以清）。
**这个区分搞错会毁掉整个安装 —— 别简化它。**

## 3. 红区 —— doctor（这一步才是重点）

因为那台机器什么都装不了，部署完真正的问题是「**它现在能跑什么**」。
`doctor.sh` 挨个探测候选解释器，报三档：

| tier | 能干什么 | 需要 |
|---|---|---|
| 1 | 解析官方 run 目录 + 拼命令 + `dry-run` + 跑全部单测 | 只要 Python ≥ 3.10（核心是纯 stdlib）|
| 2 | 真提交跑批次 | 外加 `dsub` / `djob` / `ewave` / `strmout` 在 PATH |
| 3 | GUI | 外加 tkinter + 真能开窗（`$DISPLAY`）|

**三档是累加的**：tier 3 含 tier 2 含 tier 1。

```bash
bash deploy/doctor.sh --test
```

`--test` 额外跑两样：接口自检（`python -m ewave_batch dry-run --self-test`）和
**装好的整套单测**。
在一台没网的机器上，**一套全绿的测试是能拿到的最强证据** ——
它同时证明了包完整落地、解释器可用、逻辑正确。

几条判读规则，别误判：

- **tier 3 缺失是降级不是失败。** 纯 ssh 会话本来就没有 `$DISPLAY`，
  tier 1–2 照跑。doctor 会分开说「GUI 代码没问题，缺的是 X11」。
- **tier 2 缺失通常也不是装坏了。** `dsub` / `ewave` / `strmout` 要先加载站点的
  EDA / 集群模块才在 PATH 上；一个停在 tier 1 的解释器多半只是个干净的登录 shell。
  在**同一个 shell** 里加载完模块再跑一遍 doctor 就上去了。
- **`djob` 也算 tier 2 的硬条件**（brief 的表里只点了三个）。理由：
  `ewave_batch/sched/donau.py` 提交用 `dsub`、轮询用 `djob`；没有 `djob`，
  driver 永远看不到 job 离开队列，每个 run 会一直卡在 `pending`。
  「能提交但不能轮询」不是一个能干活的 tier 2。
- **PyYAML 缺席不算问题。** spec 文件退回 JSON（`core.spec` 是惰性 import + JSON 退路）。

**退出码：只要有一个解释器到得了 tier 1 就退 0**，否则退 1。
（兄弟项目 SNP 的门槛是 tier 2，本项目**故意不同**：那边 numpy 缺了就真的什么都算不了，
而这边 tier 1 已经能干活了 —— 解析真实官方目录、拼出全部命令、dry-run、跑完整套测试，
「P1 完成就去红区试 dry-run」那一趟要的正好就是 tier 1。
把门槛定在 tier 2 会让「模块还没加载」的干净登录 shell 报成失败，是在喊狼来了。）

指定解释器：

```bash
bash deploy/doctor.sh --python /path/to/python3
# 或： export EWB_PYTHON=/path/to/python3
```

## 首次 bootstrap（机器上还没有 `deploy.sh`）

`deploy.sh` **在包里面**，所以以后每次更新它自我刷新 —— 但第一次没有东西能跑它。
一次性：

```tcsh
cd <workarea>                          # 安装目录的父目录
tar -xzf ewave_helper_<short>.tar.gz   # 解出 ./ewave_helper/
cd ewave_helper
bash deploy/doctor.sh --test
```

解出来的那个目录**就是**安装目录。从此每次更新只剩两步：
「把包传进去」+「`bash deploy.sh`」。

## 保住你自己的数据

一次部署会替换掉除 `.deploy/` 之外的每一个顶层条目。
**如果你把批次结果落在安装目录里面**（例如默认的 `ewave_batches/`），
把那个顶层名字写进 `.deploy/preserve.list`（一行一个，`#` 是注释）：

```
ewave_batches
```

写了就不会被换装碰。包自己也带的名字（例如 `docs`）会被明确报错拒绝，
而不是被静默地套一层。

没写会怎样：那个目录**不会被删**，它会被 `mv` 进 `.deploy/backups/<ts>/`
—— 换装完 `deploy.sh` 会专门列出「这些东西现在只存在于备份里」并提醒你补
`preserve.list`。但再部署 3 次之后备份轮换就会把它删掉，**而且是静默的**。
所以看到那条提醒就动手。

上传的 `*.tar.gz` 留在原地（`.deploy/incoming/` 里另有一份）。
默认保留**最新 2 个**交付包，更老的在下一次**成功**部署后删掉 ——
失败的部署一个都不删。

## 回滚

每次部署都会把上一版备份到 `.deploy/backups/<timestamp>/`。手工回退：

```bash
cd <workarea>/ewave_helper
# 删掉当前内容（除了 .deploy），然后：
mv .deploy/backups/<timestamp>/* .
```

或者直接部署一个旧包：`bash deploy.sh <older>.tar.gz`。

部署跑到一半失败会自己回滚。

## 为什么 `scripts/` 里那四条不进包

`scripts/{check,redzone_scan,redzone_crosscheck,install_hooks}.sh` 是**开发侧的提交闸门**
（拦红区坐标进 public GitHub），每一条都以 `git rev-parse --show-toplevel` 开头。
红区没有 git ⇒ 它们必然 `exit 1`，其中两条只打一句 `not a git repo`
（2026-08-19 实测：把它们 copy 到一个非 git 目录里跑，三条全部 exit 1）。
装过去只会制造「敲一句 `sh scripts/check.sh` 得到没头没尾的 exit 1，以为装坏了」。
而它们的职责本来就是「拦住红区坐标别进 public GitHub」，在红区内部没有意义。

它们在 `.gitattributes` 里被**逐条**列名 `export-ignore` —— **不是**写 `scripts/`。
这样 `scripts/` 里那些**给红区用**的脚本（例如 `scripts/redzone_bundle.sh`）
照常自动进包，「新加的脚本自动进包」这条零维护性质不受影响。

红区侧的等价物是 `bash deploy/doctor.sh --test`。

顺带：`tests/test_gate_portability.py::RedzoneScanFileSet` 那三条测的正是上面那几个
闸门脚本，需要 git + `redzone_scan.sh`。它们在红区会**优雅跳过并说明原因**
（2026-08-19 补的 `setUpClass` 平台判断）—— 否则 `doctor.sh --test` 会因为三条
与安装质量无关的红而失去证明力。开发机上两样都在，那三条照跑。
