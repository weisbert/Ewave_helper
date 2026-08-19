# `ewave_log_synthetic/` —— 合成日志 fixture（`core.logparse` 的测试素材）

## 为什么是合成的

`docs/OVERNIGHT.md` P4 那一行写的是「用 MVP 取回的真实日志当 fixture（`*.local.*`）」。
**本机没有那份真实日志。** `references/probes/` 里只有 help dump、`gdsout_setup`、
workdir tree、step4 verify 的输出 —— 没有一份完整的 `ewave.log` / `emsolver.log`
（`mvp_step4_verify_*.txt` 里只有 `ls -l` 列出的文件名和字节数）。

所以这批 fixture 是**手写的合成日志**，规矩两条：

1. **形状照抄证据，数值一律自己编。**
   每一行的格式都能追到下面那张表里的一条出处；每一个数字都是我编的。
   从证据里抄值正是 2026-08-18 夜跑 P3 被打回的那件事，`scripts/redzone_crosscheck.sh`
   就是为它加的。
2. **猜的格式要标出来。** 下表「出处」一栏写「**猜的**」的行，是没有任何证据支撑的措辞。
   `core/logparse.py` 里对应的正则旁边也标了同一句话。
   它们不匹配时字段留 `None`，不会给出错的数字。

## 这里的数值全是编的

| 量 | 本 fixture 用的 |
|---|---|
| 端口数 | **4** |
| 真算过的频点 | **3** |
| 墙钟 | **111 s** / 96 s / 12 s |
| 峰值内存 | **7.5 GB** |
| 估算内存 | **9.25 GB** |
| CPU 占用 | **250%** |
| 版本串 | **9999.99.sp9** |
| pin 名 | **PIN_A…PIN_D** |

**格式照抄红区证据，数值一律自己编。**

⚠️ 这里原本有一张「合成值 vs 红区真实值」的对照表，2026-08-19 夜跑 P4 审查把它打回了 ——
那张表的右列整列是红区真实测量（真实 cell 在真实站点上的规模），
而它就写在一个**会进 git** 的文件里。一张标题写着「别把右边那列写进 fixture」的表，
本身就是把那列写了进来。
要对照真值请直接看 `PROJECT_BRIEF.md` §10（那份不进 git）。

## 行格式的出处

| fixture 里的行 | 出处 | 有据？ |
|---|---|---|
| `Calculated on 3 points.` | BRIEF §10 逐字引用 `Calculated on <N> points.`（D13 那一行 / P4a 关闭那一行 / C 的运行数据 `Calculated on 1 points.`） | ✅ |
| `Execute emesh done.` / `Execute eresist done.` / `Execute emsolver done.` | BRIEF §10 根因链逐字引用 `Execute eresist done.`；`mvp/redzone/go_workarea.sh` 抬头 | ✅ |
| `[info] All Ports size is 4:` + `Port: …` + `Ground:` | `mvp/redzone/step2_memestimate.sh` 的注释「红区 step0 实测到的格式」，且该脚本拿它当闸门判据 | ✅ |
| `terminate called after throwing an instance of 'boost::archive::archive_exception'` | BRIEF §10 step3 崩溃现场第 1 行 | ✅ |
| `what():  input stream error` | 同上，第 2 行 | ✅ |
| `[error] eWave exit failed! Failed to execute emsolver, please contact the manufacturer.` | 同上，第 3 行（**去掉了厂商名** —— 与 `sched.fake._LOG_CRASH` 同一处理） | ✅ |
| `expected memory: 9.25 GB` | BRIEF §10「内存估算（P8a 的答案）」逐字引用 `expected memory: <估算值> GB` | ✅ |
| ANSI 色码（`ansi/ewave.log`） | 生产命令行末尾恒接 `\| sed -r 's/\x1B[[0-9;]*m//g'`（`references/probes/run_ewave_typical_*.sh`） | ✅ |
| `! Port[1] = PIN_A \| ref` | `references/probes/mvp_step4_verify_*.txt` 的实测原文；BRIEF §10 step4 判据② | ✅ |
| `# HZ   S   RI   R    50` | 同上（option line 对比那一节）；BRIEF §10「修掉的一个真 bug」 | ✅ |
| `[info] 0 error, 0 warning` | `mvp/redzone/step2_memestimate.sh` 的 `grep -viE 'Invalid Via\|0 error'` —— 反证真实日志里存在这类**含 error 字样却无害**的行 | ✅ |
| `[info] Invalid Via count: 0` | 同上 | ✅（形状是推的，"Invalid Via" 这个词是实测的） |
| `Wall Clock Time: 111 s` | **猜的。** 关键词来自 `mvp/redzone/diag_ab.sh` 等四处 `grep -iE '…\|Wall Clock Time:'`，但那几条 grep 是先写的，没有粘回来的输出确认过冒号后面长什么样 | ⚠️ |
| `peak memory: 7.5 GB` | **猜的。** BRIEF §10 只给了值（（真值不复述）），没给行 | ⚠️ |
| `average cpu usage: 250%` | **猜的。** BRIEF §10 D13 只给了 「平均 CPU 占用 <百分比>」 | ⚠️ |
| `[info] iterative solver converged in 7 steps` | **猜的。** BRIEF §10 只说了 "iterative 35 步 82.8 s" / "33 次迭代" | ⚠️ |
| `eWave 9999.99.sp9`（抬头） | **猜的。** 只知道有 `--version` 这个 flag | ⚠️ |
| `Sweep on 21 points.` | **猜的。** "一共要扫几个点"这一行的措辞完全没有证据 | ⚠️ |
| `[info] emsolver start, 4 threads` / `[info] reading layout from synthetic.gds` | **纯凑数的无关行**，故意放进来，测「认不出的行必须被忽略」 | — |

## 文件清单

| 路径 | 用途 |
|---|---|
| `success/ewave.log` | 干净成功。`ok=True`（= 日志没自曝失败，**不是**「run 成功」） |
| `success/emsolver.log` | 收敛 / 峰值内存 / CPU 占用都在这份里；`ok` 恒为 `None` |
| `crash/ewave.log` | BRIEF §10 那次事故的忠实复刻：`Execute eresist done.` 与 boost 崩溃同时在 |
| `crash/emsolver.log` | 事故现场（崩溃两行） |
| `crash/ewave_says_all_done.log` | **合成的极端例**：三个 `Execute … done.` 全打了，同时有崩溃指纹。`ok` 必须是 `False`。这是本模块存在意义的判据 |
| `ansi/ewave.log` | 与 `success/ewave.log` **剥完 ANSI 后逐字节相同**。两份文件各自手写，所以相等这件事不是自证 |
| `memestimate/ewave.log` | `--memEstimate` 的半程 run：有 `expected memory`，**没有** peak；`ok` 必须是 `None` |
| `snp/ports.s4p` | 正常端口注释块 |
| `snp/ports_shuffled.s4p` | 序号乱序 → 必须按序号排好再返回 |
| `snp/ports_missing.s4p` | 4 端口的文件只列了 3 个 → 必须返回 `()`（宁可不给，也不给一半） |
| `snp/no_ports.s4p` | 没开 `--includePortOrder=1` → 返回 `()` |

## 真实日志将来放哪

`tests/test_logparse.py` 里有一组测试指向 `*.local.*` 路径（`.gitignore` 已排除），
**本机没有那些文件时优雅 skip 并打印原因**。用户从红区抄回真实日志后放进去就能验：

```
tests/fixtures/ewave_run_real.local.d/        <- 一个真实 run 目录的拷贝
    ewave.log
    emsolver.log
tests/fixtures/ports_real.local.sNp          <- 一份真实 .sNp（只需要注释头）
```

> ⚠️ 目录名末尾那个 `.d` 是**承重的**，不是装饰。
> `.gitignore` 的规则是 `tests/fixtures/*.local.*` —— 要有 `local` 后面那个点才命中。
> 写成 `ewave_log_real.local/` 的话目录本身不被排除，里面的 `emsolver.log` 会被 git
> 收进去，而那是一份真实红区日志。（`ewave.log` 恰好被另一条通用规则挡住，
> 所以这个洞会假装不存在 —— 2026-08-19 实测踩到过。）
> `tests/test_logparse.py::FixtureAreTrackableTests` 两个方向都盯着这件事。

那组测试**不断言具体数值**（数值是站点信息，不能进 git），只断言
「解析器在真实日志上没崩、且抽出来的字段不全是 None」—— 也就是**验格式，不验值**。
真实值对不对由跑测试的人肉眼看打印出来的那份摘要。

## 这个目录为什么在 `.gitignore` 里有一条 `!` 放行

「运行产物」段里的 `ewave.log` 和 `*.s[0-9]p` 会把这 11 份 fixture 一个不剩地挡在
git 外面。不放行的话：开发机上文件在磁盘上、测试全绿；别人克隆下来少 11 个文件、
测试当场红，**而且没人会想到去查 `.gitignore`**。
放行只覆盖这一个目录，且 `*.local` / `*.local.*` 被重新排除回去 ——
红区日志不许借这条路进 git。
