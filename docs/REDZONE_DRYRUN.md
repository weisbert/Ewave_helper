# 红区 dry-run —— 一页操作手册

**这条命令只读。它不写任何文件，不建任何目录，不提交任何 job。**
跑它唯一的后果是屏幕上多了一大段输出。放心跑。

它干什么：解析一个**官方 GUI 跑过的 design 目录**，把我们会生成的全部命令和落地目录
打印出来，然后拿那个目录里**官方那条真实命令**当基准，逐 flag、逐端口比一遍。

为什么值得跑这一趟：`core/cmd.py` 是整个工具的地基，解析错了后面全白做。
而本机（家里的 Windows）的测试只能对着**抄回来的样本**验 —— 它验不了「解析一个真实目录」
这件事本身。**这一步只有在红区才做得了。**

---

## 1. 一条命令

```sh
ma python/3.11.4
cd <workarea>/ewave_helper
python -m ewave_batch.redzone_dryrun --offdir <官方跑过的那个 design 目录>
```

不用装任何东西（纯 stdlib）。不用先读代码。登录 shell 是 csh/tcsh 也没关系 ——
入口是 python 脚本，上面这三行没有任何 bash 专有语法。

想让输出留档（**建议**，它就是要贴回来的东西）：

```sh
python -m ewave_batch.redzone_dryrun --offdir <目录> >& dryrun.log     # csh/tcsh
```

---

## 2. `<官方跑过的那个 design 目录>` 是哪个

就是官方 eWave GUI 在 Virtuoso 里跑过一次之后留下的那个目录，形状是：

```
<workarea>/ewave_simulation/<library>_<topCell>_<view>/
    gdsout_setup                 ← 判据就是这个文件
    <topCell>.gds
    run_ewave_<corner>_<temp>.sh ← 官方那条真实命令在这里面
    remote_run_ewave.sh          ← dsub 的 -A / -q / -R 在这里面
    <corner>_<temp>/             ← eWave 自己建的那层，产物在里面
```

**判据是 `gdsout_setup`**：有这个文件的就是 design 目录。找不到就让机器找：

```sh
python -c "from ewave_batch.core.discover import suggest_official_dirs as s; print('\n'.join(s('<workarea>')))"
```

指错一层是最常见的错误（多半是指到了上一级 `ewave_simulation/`，或者指进了
`<corner>_<temp>/` 子目录）。指错时命令会告诉你往上还是往下挪一层，并把附近的候选列出来。

> ⚠️ 挑一个**求解跑完过**的 design 目录。只做过 stream out、还没提交过求解的目录里
> 没有 `run_ewave_*.sh` ⇒ 没有基准可比，命令会退 3 并说明原因（argv 和落地目录照样打印）。

---

## 3. 输出长什么样（五段）

```
[1/5] 站点坐标 —— 全部现场解析
[2/5] 阶段 1：strmout
[3/5] 阶段 2：每个 run 的完整命令 + 落地目录
[4/5] 自带比对 —— 拿 OFFDIR 里那条真实命令当基准
[5/5] 结论
```

### [1/5] 站点坐标

一张表：library / topCell / view / corner / temperature / layerMap / ptxt /
dsub 的 `-A -q -R` / `ewave` 和 `strmout` 的实际路径 / 官方端口数 / 学到的默认表条数。
下面跟一行「每个坐标是从哪个文件来的」。

**该看什么**：这些值对不对。它们全是从你给的那个目录里**现场解析**出来的 ——
源码里一个站点坐标都没有（CLAUDE.md 硬约束 1b），所以这一段错了，后面全错。

后面可能跟几条 `⚠ 解析警告`。警告是**软失败**（字段留空、其余照跑），不是错误。
最常见的两条：

* `PATH 上没有 ewave / strmout` —— 没 `ma` 出对应模块。dry-run 照跑，
  argv 里的程序名会用通用名 `ewave` 占位，并在下面明写这一格是占位的。
* `官方 run 脚本有 N 份` —— 取文件名排序的第一份（它们之间通常只差 corner/temperature）。

### [2/5] 阶段 1

`strmout -templateFile <我们渲染出来的 gdsout_setup>` 一条命令，外加 cwd 和产物位置。
加 `--show-gdsout` 可以把渲染出来的模板整份打出来看。

**该看什么**：那份模板里除了 7 个随 design 变的字段之外，其余必须和官方那份**逐字相同**
（D1c：`convertPin "geometry"` / `case "preserve"` / `maxVertices 200` 错一个，
GDS 内容就变了，而且跑得出来、数字还挺像）。这条在 [4/5] c) 里有机器判据，不用你肉眼比。

### [3/5] 每个 run

每个 run 一段：轴取值、`--workDir`、eWave 会自己建的 `<corner>_<temp>/`、
命令留档位置、归档位置，最后是**完整的一行命令**（可以直接粘去手工跑）。

矩阵很大时用 `--limit 5` 只详细打印前 5 个，其余只列 id 和落点。

**该看什么**：不同的 run 落在**不同的目录**里。原生 GUI 的痛点就是同 corner/temp 换别的
参数会静默覆盖 —— 我们靠给每个组合一个独立 `--workDir` 绕开它。四个 run 四个落点，
一眼就能看出来对不对。

### [4/5] 自带比对 ★

这一段是这趟的正主。三条：

**a) 逐 flag** —— 与官方那条真实命令逐个比。报四个数：

| 这个数 | 意思 |
|---|---|
| 参与比较 | 真的比过几条。**空集合的 diff 永远是绿的**，所以这个数必须自己看一眼 |
| 忽略 | `--workDir` `--gds` `--all` `--includePortOrder` 四条，每条都是**有意不同** |
| 其中「学自本目录」 | ⚠️ 这些 flag 的取值就是从**同一份**脚本学来的 ⇒ 结构上必然相等，**不算验证** |
| ★ 真独立验证 | 取值来自源码内置 / 机制层 / 轴 / 跨文件推导。**这个数才是含金量** |

最后那两行是刻意写出来的：如果不分开报，一句「20 条全一致」会让人高估这趟比对。

**b) 端口顺序** —— 这条完全没有上面那个问题，是货真价实的验证：
我们按 `--all` 的规则（pin 名 case-sensitive ASCII 排序）预测一遍端口编号，
和官方那串 `-p` 逐位比。**这是「不依赖 GUI」这件事成立的全部依据**（D1b）。

**c) `gdsout_setup`** —— 两条：往返自检（模板化→渲染→比，验的是"没被顺手改过"）、
兜底模板对照（仓库里那份源码常量 vs 你这个站点的真文件）。

### [5/5] 结论

先列「真跑时才会写的落点」清单（这趟一个都没写），然后一句话结论 + 下一步。

---

## 4. 退出码

| 码 | 含义 | 怎么办 |
|---|---|---|
| **0** | 比对一致 | 往下走：写 spec，加 `--spec` 再跑一次 |
| **2** | 比对完成，**有差异** | 看 [5/5]，它把每处差异和对应的下一步都列出来了 |
| **3** | 没能比对（目录里没有官方命令） | 换一个**求解跑完过**的 design 目录 |
| **1** | 跑不起来 | 消息里有「下一步」，照着做 |

csh/tcsh 里看退出码：`echo $status`（不是 `$?`）。

---

## 5. 看到差异怎么办

**先别拿那些命令去跑。** 差异分三类，[5/5] 会逐条告诉你属于哪类：

* **少给了 `--xxx`**（官方有、我们没有）
  多半是「学默认表」时被剔除规则吃掉了，或者该由某一层负责而没人给。
  → 把 [4/5] 整段贴回来。

* **`--xxx` 取值不同**
  看它由哪一层给：**轴** → 检查 spec 里写的取值；**默认表** → 从官方目录学错了；
  **源码内置** → 仓库里那张兜底表和你这个站点不符 —— 这是**真发现**，值得改代码。

* **端口顺序第 N 位起对不上** ← **这条最要紧**
  说明 D1b（`--all` 逐位复现官方 `-p` 顺序）在这个站点不成立 ⇒ **不能用 `--all`**，
  必须在 spec 里显式写 `ports`。否则 `.sNp` 的端口编号会整体错位，
  而且**静默** —— Touchstone 只按 `P00x` 排列、pin 名根本不写进文件，事后查不出来。

还有一条不算差异但值得看的**警告**：
「官方用 `-i` 只挑了 N/M 个 signal port」。`--all` 把全部端口都当 signal，
表达不了接地端口。这个 design 如果有接地端口，就要在 spec 里显式写 `ports`。

差异**不会造成任何后果** —— 这趟没写任何文件，也没提交任何 job。

---

## 6. 全部参数

```
--offdir DIR        官方跑过的那个 design 目录（必填，**只读**）
--spec FILE         可选：批次 spec（YAML/JSON）。不给就用 OFFDIR 自己的
                    corner/temperature 造一个单点批次 = 把官方那次跑重放一遍
--batch-root DIR    落点的根，只用来算路径、**不会建目录**（默认 ./ewave_batches）
--batch-name NAME   批次名（默认 dryrun）
--limit N           只详细打印前 N 个 run（0 = 全部，默认）
--show-gdsout       连渲染出来的 gdsout_setup 一起打印（仍然不写它）
```

要一份 spec 样例：

```sh
python -c "import sys; from ewave_batch.core.spec import EXAMPLE_SPEC; sys.stdout.buffer.write(EXAMPLE_SPEC.encode('utf-8'))" > my_spec.yaml
```

---

## 7. 「它真的不写东西吗」

四条，都可以自己核实：

1. 整个模块里**一个写文件的调用都没有**。读文件只有一处（`_read_text`，`open(..., "r")`）。
2. 所有「真跑时会写这里」的落点都经过一道守卫：落点在 `ewave_simulation/` 里面
   （设计师的 spine）或者落在 `--offdir` 里面 → **当场拒绝、退 1**，而且是在打印
   任何命令**之前**拒绝的。
3. 落点默认是 `./ewave_batches`，而且**连这个目录都不会建**。
4. 有一条测试专门盯这件事：把合成的官方目录放进一个真叫 `ewave_simulation` 的目录里，
   跑之前记下整棵树的文件集 + 大小 + mtime，跑之后逐个比对相同
   （`tests/test_redzone_dryrun.py::ReadOnlyGuard`）。

不放心的话，跑之前 `ls -lR <目录> > before.txt`，跑完再来一次 `diff`。

---

## 8. 把结果贴回来

**整段输出里有站点坐标**（库名 / cell 名 / 端口名 / ptxt 路径 / 队列 / 账号）——
它只在公司内部流转：不发 GitHub、不发网盘、不贴进任何第三方服务。
这也正是本工具的源码里一个站点坐标都没有的原因（坐标全部运行时解析）。
