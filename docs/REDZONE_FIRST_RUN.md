# 红区首次部署 —— 照着敲，每步都写了「你应该看到什么」

写给刚睡醒、不想读代码的人。**从黄区 `git clone` 开始，到红区一条命令跑完验证结束。**

三件事先说清楚：

1. **每一步都有预期输出。** 看到的东西和这里写的对不上，就往下翻那一步的「不是这样怎么办」，
   别往下走。
2. **红区登录 shell 是 csh/tcsh。** 所有 `.sh` 一律 `bash xxx.sh` 地敲（不是 `./xxx.sh`），
   设环境变量用 `setenv FOO bar`（不是 `export FOO=bar`），看退出码用 `echo $status`（不是 `$?`）。
3. **路径全是占位符。** `<workarea>` = 你的工作区，`<OFFDIR>` = 官方 GUI 跑过一次的那个
   design 目录，`<short>` = 包名里的那串 commit 短哈希。照着替换成你自己的。

> 这份文档、本工具的源码、以及打出来的包里，**一个真实站点坐标都没有** —— 库名 / cell 名 /
> 端口名 / ptxt 路径 / 账号 / 队列全部在运行时从你给的目录里解析。这是有意的（`CLAUDE.md` 硬约束 1b），
> 也是这个仓库敢放在公网 GitHub 上当传输信道的原因。

---

## 三十秒版

已经装过一次、只想更新的话，只有下面四行：

```tcsh
# 黄区 Windows（PowerShell）
git pull
powershell -ExecutionPolicy Bypass -File deploy\pack.ps1

# —— 把 deploy\dist\ 里那两个文件上传到红区的 <workarea>/ewave_helper/ ——

# 红区（csh/tcsh）
cd <workarea>/ewave_helper
bash deploy.sh
ma python/3.11.4
bash deploy/doctor.sh --test
bash scripts/redzone_bundle.sh <OFFDIR>
```

**第一次**装的人别跳，从下面的步骤 1 开始。

---

## 链路：三跳

```
家里 Windows ──git push──▶ GitHub(public) ──git clone/pull──▶ 黄区 Windows
                                                                    │
                                                        deploy\pack.ps1
                                                                    ▼
                                                    ewave_helper_<short>.tar.gz
                                                          + .sha256
                                                                    │
                                                               上传这两个
                                                                    ▼
                                                   红区 Linux：bash deploy.sh
```

红区**无网、无 git、无 pip、无 venv**，所以第二跳只能是一个 tarball。
黄区能直接 `git clone` 公网 GitHub，所以第一跳靠 git。

---

## 步骤 1（黄区 Windows）—— 把代码拉下来

第一次：

```powershell
cd <你放代码的地方>
git clone https://github.com/weisbert/Ewave_helper.git
cd Ewave_helper
```

以后每次：

```powershell
cd <...>\Ewave_helper
git pull
```

**你应该看到**：`git pull` 报 `Fast-forward` 或 `Already up to date.`，然后

```powershell
git status
```

输出里有 `nothing to commit, working tree clean`。

**不是这样怎么办**

| 看到 | 说明 | 去做 |
|---|---|---|
| `Your branch is behind ...` 之后 `git pull` 卡住要求 merge | 黄区这份被人改过 | `git stash` 收起本地改动再 `git pull`。**黄区那份是只读中转站，不该在上面改代码** |
| `git status` 列出 `modified:` | 有未提交改动 | 下一步的打包**只打已提交的内容**，未提交的东西过不了气隙。要么提交，要么 `git checkout -- .` 丢弃 |
| `fatal: unable to access ...` | 黄区连不上 GitHub | 换个能连的机器 clone，再把整个目录拷到黄区。**别在红区解决这个问题** |

---

## 步骤 2（黄区 Windows）—— 打包

```powershell
powershell -ExecutionPolicy Bypass -File deploy\pack.ps1
```

只要 git + PowerShell，不需要 Python、不需要 tar、不下载任何东西。

**你应该看到**（末尾这几行是判据）：

```
>> packaging HEAD (a1b2c3d) -> ...\deploy\dist\ewave_helper_a1b2c3d.tar.gz

OK  package : ...\deploy\dist\ewave_helper_a1b2c3d.tar.gz  (312.4 KB)
    sha256  : 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08

commit info:
    a1b2c3d  2026-08-19T02:11:07+08:00  <这次的 commit 标题>

NEXT -- upload BOTH files into  .../ewave_helper/ :
       ewave_helper_a1b2c3d.tar.gz
       ewave_helper_a1b2c3d.tar.gz.sha256
```

判据是 **`OK  package :` 那一行**，以及 `deploy\dist\` 下确实出现了**两个**文件
（`.tar.gz` 和 `.tar.gz.sha256`）。

包名里的 `a1b2c3d` 就是 GitHub 上那个 commit 的短哈希 —— 以后「红区装的是哪一版」
一眼就能对上。

**不是这样怎么办**

| 看到 | 说明 | 去做 |
|---|---|---|
| `WARNING: Working tree has uncommitted changes; they will NOT be packaged` | 有改动没提交，包里不会有它们 | 只是警告。如果那些改动无所谓就继续；否则回步骤 1 处理干净 |
| `deploy.sh is not tracked by git - git add it first.` | 新文件还没进 git | 在**开发机**上提交并 push，黄区重新 `git pull`。黄区不提交东西 |
| `... has CRLF in the git index.` | 行尾被 Windows 污染了 | 照它说的做：`git add --renormalize <那个文件>` 然后提交。**这条不许绕过** —— CRLF 会让红区 bash 死在 `$'\r': command not found`，而那是最没法调试的地方 |
| `git archive emits CRLF for the shell scripts ...` | `.gitattributes` 修了但没提交 | 提交 `.gitattributes` 再打包。`git archive` 读的是**提交进去的**那份，不是工作树那份 |
| `git archive cannot find ... in HEAD` | 要打的那个 ref 里没有这些文件 | 确认 `git log -1` 是你以为的那个 commit |

---

## 步骤 3 —— 上传

把**两个文件**都传到红区的安装目录：

```
<workarea>/ewave_helper/ewave_helper_<short>.tar.gz
<workarea>/ewave_helper/ewave_helper_<short>.tar.gz.sha256
```

第一次装的话这个目录还不存在 —— 传到你打算安装的**父目录**里就行（下一步解压出来的
`ewave_helper/` 就是安装目录）。

**你应该看到**：红区上

```tcsh
ls -l <上传到的目录>/ewave_helper_*
```

列出两个文件，`.tar.gz` 有几百 KB，`.sha256` 只有几十字节。

**不是这样怎么办**

| 看到 | 说明 | 去做 |
|---|---|---|
| 只有 `.tar.gz`，没有 `.sha256` | 少传了一个 | 补传。没有它下一步会跳过校验并打 `WARN`，等于放弃了「传输没坏」这个保证 |
| `.tar.gz` 大小是 0 或明显偏小 | 传输被截断 | 重传。**不要**试图解压一个残包 |

---

## 步骤 4（红区）—— 校验

```tcsh
cd <上传到的目录>
sha256sum -c ewave_helper_<short>.tar.gz.sha256
```

**你应该看到**：

```
ewave_helper_<short>.tar.gz: OK
```

**不是这样怎么办**

| 看到 | 说明 | 去做 |
|---|---|---|
| `FAILED` | 传输过程中坏了 | 重传两个文件。**别继续** |
| `sha256sum: command not found` | 这台机器没有它 | 跳过这一步。步骤 5 的 `deploy.sh` 自己也会校验一次（并在缺 `sha256sum` 时打 `WARN`） |
| `No such file or directory` | 文件名对不上 | `ls` 一下确认实际文件名 —— sidecar 里记的名字必须和 `.tar.gz` 的实际名字一致 |

---

## 步骤 5（红区）—— 装上去

### 5a. 第一次（这台机器上还没有安装目录）

```tcsh
cd <workarea>
tar -xzf <上传到的目录>/ewave_helper_<short>.tar.gz
cd ewave_helper
```

**解压出来的这个 `ewave_helper/` 目录就是安装目录。** 以后所有事都在它里面做。

**你应该看到**：

```tcsh
ls
```

列出至少这些：`cli.py  deploy.sh  deploy/  docs/  ewave_batch/  gui/  scripts/  tests/  VERSION`

### 5b. 以后每次更新

把新的 `.tar.gz` + `.sha256` 传进**安装目录本身**，然后：

```tcsh
cd <workarea>/ewave_helper
bash deploy.sh
```

不用给参数 —— 它自己挑目录里最新的那个包，并告诉你挑了哪个。

**你应该看到**（末尾这几行是判据）：

```
>> found package: ewave_helper_<short>.tar.gz
>> verifying sha256...
>> extracting to staging...
>> incoming version:
     <commit hash> <日期>
>> backing up current install -> .../.deploy/backups/<时间戳>
>> installing new version...
OK  deployed.
    installed version:
     <commit hash> <日期>
    previous install backed up at: .../.deploy/backups/<时间戳>

    NEXT -- check this box can actually run it (no network / no venv needed):
       cd <workarea>/ewave_helper && bash deploy/doctor.sh --test
```

判据是 **`OK  deployed.`**，以及 `installed version` 那两行里的 hash 和你在步骤 2
看到的 `commit info` 一致。

**不是这样怎么办**

| 看到 | 说明 | 去做 |
|---|---|---|
| `bash: $'\r': command not found` | 包里的 `.sh` 是 CRLF | 步骤 2 的预检没跑或被绕过了。回黄区重新打包，**别在红区试图 `dos2unix`** —— 那只治了这一个文件 |
| `ERROR: sha256 mismatch` | 传输坏了 | 回步骤 3 重传 |
| `WARN: no .sha256 sidecar` | 少传了 sidecar | 能继续，但这次部署没有完整性保证。建议补传后重做 |
| `!! swap failed during '...' -- rolling back` 后面跟 `!! rollback complete -- install restored.` | 换装中途失败，**已经自动回滚**，原来那份还在 | 把整段输出留着。多半是磁盘满或权限问题：`df -h .` 看一眼 |
| `NOTE: these were NOT part of the new package ...` 列出了你的批次结果目录 | 你把批次结果放在安装目录里了，被挪进备份了 | 照它说的把名字写进 `.deploy/preserve.list`（一行一个），以后就不会被动 |

---

## 步骤 6（红区）—— 选 python

```tcsh
ma python/3.11.4
python -V
```

**你应该看到**：

```
Python 3.11.4
```

本工具**只用 stdlib**，没有任何要装的东西 —— 红区无装包权限，所以整个设计就是「有个
够新的 python 就能跑」。

**不是这样怎么办**

| 看到 | 说明 | 去做 |
|---|---|---|
| `Python 3.6.8` 之类 | `ma` 没生效，用的是系统自带那个 | 重新 `ma python/3.11.4`；确认在**同一个 shell** 里往下敲 |
| `ma: Command not found` | 这台机器没有那个 module 工具 | 直接找一个 3.10+ 的解释器，后面每条命令都用 `--python /path/to/python3` 指定它 |
| `python: Command not found` 但 `python3` 有 | 只提供了 `python3` | 用 `python3`。下面的命令里 `python` 一律换成 `python3` |

---

## 步骤 7（红区）—— 体检 + 跑一遍装好的单测

```tcsh
bash deploy/doctor.sh --test
```

它做两件事：① 探测这台机器上每个候选解释器能跑到哪个 tier；② 跑一遍随包发过来的
**整套单测**。在一台没网的机器上，一套全绿的测试是能拿到的最强证据 ——
它同时证明了包完整落地、解释器可用、逻辑正确。

**你应该看到**（这是本机实测的真实形状，路径换成了占位符）：

```
=== Ewave_helper -- red-zone environment doctor ===
install : <workarea>/ewave_helper
version :
     <commit hash> <日期>

>> <某个 python 的路径>  (3.11.4)
     OK  import ewave_batch.core.cmd
     OK  import ewave_batch.cli
     OK  import ewave_batch.redzone_dryrun
     OK  import gui.app (lazy, no tkinter needed)
     OK  dsub     <路径>
     OK  djob     <路径>
     OK  ewave    <路径>
     OK  strmout  <路径>
     OK  tkinter  present, but $DISPLAY is unset (headless -- no GUI)
     OK  PyYAML   6.0.1 (spec files can be YAML)
     ------------------------------------------------
     tier 1  plan / dry-run / tests           AVAILABLE
     tier 2  submit a real batch              AVAILABLE
     tier 3  GUI                              code OK, needs X11 ($DISPLAY)

RECOMMENDED: <某个 python 的路径>   (tier 2)

  cd <workarea>/ewave_helper
  interface self-test : <python> -m ewave_batch dry-run --self-test
  plan a batch        : <python> cli.py dry-run --help
  run a batch         : <python> cli.py run --help
  unit tests          : <python> -m unittest discover -s tests -t .

=== self-test with <python> ===
ewave_batch 接口自检 —— INTERFACE_VERSION=2
模块                          状态             符号
----------------------------------------------------
cli                         implemented    1/1
ewave_batch.core.cmd        implemented    13/13
...（共 23 行）...
----------------------------------------------------
模块 23 个｜漂移 0 个｜未实现 0 个｜平台降级 0 个｜合计 23
self-test: OK（无漂移）

...........................................................................
----------------------------------------------------------------------
Ran 962 tests in 28.105s

OK (skipped=2)

OK  self-test passed -- the package landed intact and this interpreter runs it.
```

**判据只有三行**：

1. `tier 1 ... AVAILABLE`（tier 2/3 缺是**降级不是失败**，见下）；
2. `self-test: OK（无漂移）`；
3. 最后那句 `OK  self-test passed`，并且 `echo $status` 是 `0`。

> **测试条数会随版本变**（`Ran 962 tests`），别拿数字当判据 —— 判据是最后那行 `OK`。
> `skipped=2` 也是正常的：少数测试需要含站点坐标的 fixture，那些 fixture **永远不进包**，
> 缺了就优雅跳过。

**关于 tier**

| tier | 能干什么 | 缺了要紧吗 |
|---|---|---|
| 1 | 解析官方 run 目录 / 拼命令 / dry-run / 跑单测 | **要紧**。只要一个 3.10+ 的 python，缺了说明包没落全 |
| 2 | 真提交跑批次 | 不要紧，但真跑之前得有。缺的是 `dsub`/`djob`/`ewave`/`strmout` 不在 PATH —— `ma` 出对应模块即可 |
| 3 | GUI | **不要紧**。纯 ssh 会话里本来就该只有 CLI |

**不是这样怎么办**

| 看到 | 说明 | 去做 |
|---|---|---|
| `VERDICT: no interpreter on this box can run Ewave_helper.` | 没有解释器能 import 这个包 | ① 先 `ma python/3.11.4` 再重跑；② 还不行：`bash deploy/doctor.sh --python /path/to/python3`；③ 如果每个候选都 import 失败，那是**包没落全** —— 回步骤 5 重做 |
| `(stops at tier 1: not on PATH: dsub, djob, ewave, strmout)` | 只到 tier 1 | **这不挡步骤 8**（dry-run 只读、不提交，本来就不需要这些工具）。等到要真跑时再 `ma` 出它们 |
| `FAIL  interface self-test failed -- the package is inconsistent.` | 冻结接口和代码对不上 | 包混装了（新旧文件掺在一起）。重跑 `bash deploy.sh` 装同一个包；仍然红就把这段贴回给开发 |
| `FAIL  self-test failed.` | 单测有红 | **别继续**，这个安装的任何结果都不可信。先重装；再红就是工具的 bug，把整段输出贴回来 |
| 满屏 `?` 或乱码 | 这台机器的 `LANG` 是 `C`，中文被降级成 `?` | 不影响判据（判据行全是 ASCII）。想看清楚就 `setenv LANG en_US.UTF-8` 再跑 |

---

## 步骤 8（红区）—— 一条命令的验证包 ★

这一步是这趟真正的目的：**验证本工具在这个站点上拼出来的命令，和官方 GUI 那条真实命令一致。**

```tcsh
bash scripts/redzone_bundle.sh <OFFDIR>
```

`<OFFDIR>` = **官方 eWave GUI 跑过一次的那个 design 目录**，判据是里面有 `gdsout_setup`，
形状是 `<workarea>/ewave_simulation/<library>_<topCell>_<view>/`。
不知道是哪个就让机器找：

```tcsh
python -c "from ewave_batch.core.discover import suggest_official_dirs as s; print(chr(10).join(s('<workarea>')))"
```

> ⚠️ 挑一个**求解真的跑完过**的目录。只做过 stream out、还没提交过求解的目录里没有
> `run_ewave_*.sh` ⇒ 没有基准可比（会退 3，argv 和落地目录照样打印）。

**它只读，可以放心跑**：不写 `<OFFDIR>`，不写 `<workarea>/ewave_simulation/`（设计师的
spine），不提交任何 job。唯一会写的地方是安装目录自己的
`.deploy/redzone_bundle/<时间戳>/`（本次的日志）。它还会在跑前跑后各拍一次
`ls -lR <OFFDIR>` 逐字节对比，把「没动过」变成一条你自己看得见的证据。

它依次做三件事，然后给一屏汇总：

```
[1/3] 环境自检     调 deploy/doctor.sh（没有就用内置精简版）
[2/3] 全部单测     python -m unittest discover -s tests -t .
[3/3] 只读 dry-run python -m ewave_batch.redzone_dryrun --offdir <OFFDIR>
```

**你应该看到**（末尾那一屏）：

```
============================================================
  汇总 —— redzone_bundle
------------------------------------------------------------
  1) 环境      OK     python 3.11.4   tier1=YES tier2=YES tier3=NO
  2) 单测      OK     Ran 962 tests in 28.105s OK (skipped=2)
  3) dry-run   OK     逐 flag / 逐端口与官方那条真实命令一致
  OFFDIR       未被改动（跑前跑后 ls -lR 逐字节相同）
------------------------------------------------------------
  结论：**全绿**。...
  下一步：...
------------------------------------------------------------
  完整输出：<workarea>/ewave_helper/.deploy/redzone_bundle/<时间戳>
    doctor.log / unittest.log / dryrun.log
    要贴回来的是 dryrun.log —— **它含站点坐标，只在公司内部流转**。
  退出码：0   （csh/tcsh 里看 echo $status）
============================================================
```

判据是最后那行 **`退出码：0`**（也可以 `echo $status` 自己确认）。

**退出码的意思**

| `$status` | 意思 | 下一步 |
|---|---|---|
| **0** | 全绿：环境够、单测全绿、命令与官方一致 | 往下走步骤 9 |
| **2** | dry-run 比对**有差异** | **先别真跑。** 输出的 `[4/5]` 和 `[5/5]` 已经逐条写了每处差异属于哪类、该改 spec 还是该改代码。把那两段贴回给开发 |
| **3** | **没能比对**：这个 OFFDIR 里没有官方命令行 | 换一个求解跑完过的 design 目录 |
| **4** | **单测有红** | 别信这次的任何结论。重跑 `bash deploy.sh`；还红就 `--python` 换解释器；再红就是工具的 bug |
| **5** | **环境不满足 tier 1** | 回步骤 6 选 python，或 `--python /path/to/python3` |
| **6** | dry-run 跑不起来 | 多半是 OFFDIR 指错一层（上一级 `ewave_simulation/`，或下一级 `<corner>_<temp>/`）。错误消息最后几行写了怎么办 |
| **1** | 命令自己用错了（没给 OFFDIR / 目录不存在） | 照提示改 |

**输出里最该亲眼看的两处**

1. **`[1/5] 站点坐标`** —— library / topCell / view / corner / ptxt / `-A -q -R` 这一张表。
   它们全是从你给的那个目录**现场解析**出来的（源码里一个都没有）。**这一段错了，后面全错。**
2. **`[3/5]` 里每个 run 的 `--workDir`** —— 不同的 run 必须落在**不同的目录**里。
   原生 GUI 的痛点就是同 corner/temp 换别的参数会**静默覆盖**；我们靠给每个组合一个独立
   `--workDir` 绕开它。几个 run 几个落点，一眼就能看出来对不对。

怎么读完整那五段输出：`docs/REDZONE_DRYRUN.md`。

**常用变体**

```tcsh
bash scripts/redzone_bundle.sh <OFFDIR> --check-only           # 两秒确认「这条命令会跑起来吗」，什么都不跑
bash scripts/redzone_bundle.sh <OFFDIR> --limit 5              # 矩阵很大时只详细打印前 5 个
bash scripts/redzone_bundle.sh <OFFDIR> --spec my_spec.yaml    # 带上自己的批次 spec
bash scripts/redzone_bundle.sh <OFFDIR> --show-gdsout          # 连渲染出来的 gdsout_setup 一起打印
bash scripts/redzone_bundle.sh <OFFDIR> --python /path/to/python3
bash scripts/redzone_bundle.sh -h                              # 全部参数 + 退出码语义
```

留一份可以贴回来的完整日志（csh 的合并重定向是 `>&`，不是 `2>&1`）：

```tcsh
bash scripts/redzone_bundle.sh <OFFDIR> >& bundle.log
echo $status
```

（其实不用手动重定向也行 —— 它自己已经把三份日志写进 `.deploy/redzone_bundle/<时间戳>/` 了。）

---

## 步骤 9（红区）—— 真跑之前

步骤 8 退 0 之后：

```tcsh
python -c "from ewave_batch.core.spec import EXAMPLE_SPEC; print(EXAMPLE_SPEC)" > my_spec.yaml
vi my_spec.yaml                                        # 按你要扫的设定改
bash scripts/redzone_bundle.sh <OFFDIR> --spec my_spec.yaml
```

**你应该看到**：`[3/5]` 里 run 的数量等于你写的笛卡尔积大小，**且每个 run 的
`--workDir` 各不相同**。确认了这一点再真跑：

```tcsh
python cli.py dry-run my_spec.yaml     # 只打印，什么都不写
python cli.py run     my_spec.yaml     # 真提交
python cli.py status  <批次目录>
```

> 真跑需要 tier 2（`dsub` / `ewave` / `strmout` 在 PATH）。步骤 7 里 tier 2 显示
> `NOT AVAILABLE` 的话，先 `ma` 出对应模块，重跑 `bash deploy/doctor.sh` 确认变成
> `AVAILABLE`，再提交。

⚠️ 批次结果如果放在安装目录**里面**，把那个顶层名字写进 `.deploy/preserve.list`
（一行一个），否则下次 `bash deploy.sh` 会把它挪进备份，再过三次部署就被轮换掉了。

---

## csh/tcsh 备忘（这份文档里用到的全部）

| 想干的事 | bash 写法（**别在红区用**） | csh/tcsh 写法 |
|---|---|---|
| 设环境变量 | `export FOO=bar` | `setenv FOO bar` |
| 看上一条命令的退出码 | `echo $?` | `echo $status` |
| stdout + stderr 一起重定向到文件 | `cmd > f 2>&1` | `cmd >& f` |
| stdout + stderr 一起进管道 | `cmd 2>&1 \| less` | `cmd \|& less` |
| 跑一个 `.sh` | `./x.sh` 或 `bash x.sh` | **一律 `bash x.sh`**（上传通道可能吃掉 exec 位） |

---

## 失败对照总表（按你踩到的顺序）

| 现象 | 在哪一步 | 一句话原因 | 去做 |
|---|---|---|---|
| `bash: $'\r': command not found` | 5 | 包里 `.sh` 是 CRLF | 回黄区重新 `pack.ps1`（它有预检，正常打不出 CRLF 包） |
| `sha256 mismatch` / `FAILED` | 4、5 | 传输坏了 | 重传两个文件 |
| `VERDICT: no interpreter ...` | 7 | 没有能 import 这个包的 python | `ma python/3.11.4`；或 `--python`；或包没落全→重装 |
| `FAIL  self-test failed` | 7 | 单测有红 | 重装；再红就是 bug，贴输出 |
| `not on PATH: dsub, djob, ewave, strmout` | 7 | 只到 tier 1 | **不挡步骤 8**。真跑前再 `ma` 出它们 |
| `里没有 gdsout_setup` | 8 | OFFDIR 指错一层 | 用 `suggest_official_dirs` 让机器列候选 |
| 退出码 3 | 8 | 那个 design 没求解过 | 换一个跑完过的 design 目录 |
| 退出码 2 | 8 | 生成的命令和官方那条不一致 | **别真跑**，把 `[4/5]`+`[5/5]` 贴回给开发 |
| 满屏 `?` | 7、8 | `LANG=C`，中文被降级 | 判据行全是 ASCII，不影响判断；想看清就 `setenv LANG en_US.UTF-8` |
| `sh scripts/check.sh` 报 `not a git repo` | 任何 | **`check.sh` 不该在红区跑** | 那是开发机的提交闸门（要 git），已经被 `export-ignore` 挡在包外。红区的等价物是 `bash deploy/doctor.sh --test` |

---

## 这一趟到底写了哪些地方

值得单独说一次，因为「敢不敢在设计师的目录旁边跑」是这个工具能不能用的前提。

**写了**（全部在安装目录里面，父目录一个字节都不碰）：

* `<workarea>/ewave_helper/` 下的代码 —— 步骤 5 的换装；
* `<workarea>/ewave_helper/.deploy/` —— 备份 / staging / scratch / 本次日志；
* 步骤 9 真跑之后：你在 spec 里指定的批次根目录。

**没写、也永远不会写**：

* `<workarea>/ewave_simulation/`（设计师的 spine）—— 只读。唯一例外是显式触发的
  「把这个 run 设为当前」，那条路会先备份再覆盖并记日志；
* `<OFFDIR>` —— 步骤 8 连它的 mtime 都没动（脚本自己跑前跑后对比给你看）；
* `/tmp`、`/opt`、`/var` —— 一个都不碰，连临时文件都在 `.deploy/` 里。

---

## 把结果贴回来的时候

`dryrun.log` 里有站点坐标（库名 / cell 名 / 端口名 / ptxt 路径 / 队列 / 账号）——
**只在公司内部流转**：不发 GitHub、不发网盘、不贴进任何第三方服务。

这也正是本工具源码里一个站点坐标都没有的原因：坐标全部运行时解析，
所以**代码可以公开传，数据不能**。
