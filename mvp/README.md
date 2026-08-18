# mvp/ — 最小可行验证

一次性的验证脚手架，**不是**将来的产品代码。目的只有一个：在谈 GUI 交互和架构之前，
先用真机证明「不开 GUI、自己拼命令」这条路真的通。验完可以整个删掉。

实验设计、判定标准、红区侧操作步骤 → **`redzone/README.txt`**（那是唯一权威，别在这重复）。

## 这里有什么

| | |
|---|---|
| `redzone/` | **源**，9 个文件。**零红区标识符，可以公开**（CLAUDE.md 硬约束 1b） |
| `redzone/site.example.sh` | 站点坐标模板。用户 `cp` 成 `site.local.sh` 后**只填一个值** |
| `bundle.py` | 把 `redzone/` 打成下面三种形态 |
| `dist/ewave_mvp.tar.gz` + `.sha256` | **首选交付**，形状对齐 `SNP_RLC_Extractor/deploy` |
| `mvp_pack.sh` | 只能粘文本时的自解包脚本（quoted heredoc 逐字节保真） |
| `dist/ewave_mvp.tar.gz.b64` | 粘贴通道会改行尾/吃字符时的退路 |

`site.local.sh`、`mvp_pack.sh`、`dist/` 已 gitignore。

## 坐标怎么来的：现场解析，不手抄

`site.local.sh` 只给 `OFFDIR`（官方 GUI 跑过的那个 design 目录），其余全部解析：

| 来源 | → |
|---|---|
| `gdsout_setup` | library / topCell / view / layerMap |
| `run_ewave_*.sh` | ptxt / key / corner / temperature / 整串 `-p`/`-i` / 生产 flag |
| `remote_run_ewave.sh` | dsub 的 `-A` / `-q` / `-R` |
| ptxt 路径倒推 | PDK 根 / process 目录 |
| `command -v` | ewave / strmout 实际路径 |

好处有两层：标识符不进仓库；且**没有转录抄错的可能**——尤其官方那 34 个 `-p`/`-i`。

## 本机工作流

```powershell
python mvp\bundle.py          # 改了 redzone/ 下任何文件后重新打包
```

三份产物都是快照，不会自己跟着变。`tar.gz` 的 mtime 钉成 0，内容不变则 sha256 不变。

## 红区侧三条通道（内容完全相同，任选其一）

```sh
# A 有文件通道（同 Snp_analyzer 的走法）
sha256sum -c ewave_mvp.tar.gz.sha256 && tar xzf ewave_mvp.tar.gz

# B 只能粘文本
sh mvp_pack.sh

# C 粘贴会污染字符
tr -d '\r' < ewave_mvp.tar.gz.b64 | base64 -d | tar xzf -
```

## 结果回来之后

用户把 `step0..step4` 的 `.out` 粘回对话。判定在红区已由脚本做掉（`step4` 用 awk 逐 token
比 Touchstone），本机要做的是把结论写回 `PROJECT_BRIEF.md`：关掉对应的 P 项、把 D1b/D1c
从「已验证语义」升级成「已验证实现」。

## 已在本机验过的部分（不需要红区）

- **三条通道各解一遍，9/9 文件与 `redzone/` 逐字节相同**；`.sh` 的 exec 位保住、无 CRLF、
  全部 `sh -n` 通过；`sha256sum -c` 通过；b64 那条**故意灌了 CR 模拟粘贴污染，仍还原正确**
- 三个成品扫红区标识符：**0 处命中**
- **用 `references/probes/` 重建了一个假的官方 run 目录，端到端跑通 `cfg.sh` 的全部解析**：
  17/17 端口及顺序、library/cell/view/corner/temp/key/layerMap/ptxt/PDK 根、dsub 三元组，
  逐条正确；缺 `site.local.sh` 时明确报错并退 2
- **我们的默认 flag 表 vs 官方生产实际在用的：13 条逐条一致**；并加了反向验证
  （故意把 `-e 0.4` 改成 `0.5`，必须被抓到——确认这个比对不是空过的）
- `gdsout_setup.tmpl` 渲染结果与官方 `gdsout_setup` **字段名/顺序/数量完全一致**，
  值只差 `runDir`/`logFile` 两处路径
- step4 的 Touchstone 分块规则（频点块首行奇数字段、续行偶数，对任意端口数都成立）
  在合成数据上验过：**能以 ~25% 的相对差异抓到"端口顺序被置换"这个真实失效模式**
