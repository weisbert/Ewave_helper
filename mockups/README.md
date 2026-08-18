# 界面草图（mockups）

从 Claude Design 项目 `d4ad2ea7` 的三个 frame 搬成 tkinter。**不接任何后端**：
没有 ewave、没有 dsub、不写任何文件。所有数据都是编的，不含任何站点标识符。

```
python mockups\stacked.py     1a  单窗口纵向堆叠
python mockups\tabbed.py      1b  四个 tab
python mockups\split.py       1c  左配置栏 + 右整高 Runs 表
```

选中的那一版直接长成产品界面，所以现在挑剔一点是划算的。

| | 1a stacked | 1b tabbed | 1c split |
|---|---|---|---|
| 乘法公式在哪 | Settings 组标题 | 底部动作栏 + Settings 页的 Run count 面板 | 左栏 Settings 底部 Total |
| Runs 表能看几行 | ~9 | ~20（Runs 页独占） | ~25 |
| 改设定能同屏看见 run 变吗 | 能 | **不能**（要切 tab） | 能，且表大 |
| 代价 | Runs 表憋屈 | 设定和 run 永不同屏 | 左栏窄，标签得缩写 |

## 每一版都能点的东西

- **Tools → Demo state** 一键跳到设计里那四张画板：
  `01 empty` / `02 preview（12 runs）` / `03 submitted 跑到一半` / `04 右键菜单在 failed 行上`
- **Submit** 会真的假装跑：pending → running → done，第 4 个 run 故意 failed
- **右键** 任意一行：Open log / Open output dir / Copy command / Re-run this one / Set as current
- 改 Corner 勾选、Temperature 逗号列表、Mode 勾选 → 右边 `→ N` 和总数实时变
- 改 `dsub` 里的 `cpu=` → 绿字 `→ ewave --parallel=N` 跟着变
- **Tools → Extraction defaults…** 第 2 层参数（见下）

## 三层参数（回答「没暴露的参数写在哪」）

分层的依据不是「重不重要」，是 **这个参数参不参与 run 的身份**：

| 层 | 放哪 | 里面是什么 | 例子 |
|---|---|---|---|
| **锁死** | 界面上根本不出现 | 改了工具自身机制就失效的 | `--all` `--includePortOrder=1` `--workDir` `--gds` `--top` `--sparam` `--cadencePins=1` |
| **界面** | Settings 区 | 会变成目录名的 = 矩阵的轴 | corner / temp / mode / freq / mesh / equalCurrent / tolerance |
| **默认表** | `Tools → Extraction defaults…` | 影响结果但基本不动的 | `--viaMode=1` `--sparamImpedance=50` `--sweep=3` `--labelDepth=0` |
| **逃生口** | Advanced 里一行 `Extra ewave flags` | 别人临时让你加的、我们没做的 | 任意 |

两条规则：

1. 默认表里的值**不写死在源码**，是第一次运行时从官方 run 目录学来的
   （MVP 已验过 13 条逐条一致）。改 PDK 版本时自动跟上，也不会有站点坐标进仓库。
2. Extra flags 里如果写了**已经是轴**的 flag（比如 `--temperature=85`），
   界面直接标红 —— 那会让目录名和实际跑的值对不上，正是原生 GUI 那个坑的根因。

最终生效的完整命令永远在 **Selected run → Command** 那一行看得见，归档时也写进
`cmd.sh`，所以四层合并后到底跑了什么，不需要猜。

## 和设计稿的三处出入（我按事实改了，不是漏做）

1. **输出目录**。设计稿用扁平的 `run04_IND_TOP_A_typical_-40C_fullwave/`，
   但最内层 `<corner>_<temp>/` 那级是 **eWave 自己建的**、名字我们控制不了
   （BRIEF §7 P4b 实测）。所以实际是
   `<batch>/runs/<cell>/<axes-slug>/<corner>_<temp>/`。
2. **命令行**。设计稿里的 `-lib / -cell / -view / -mode / -freq / -o` 不是 eWave 真有的 flag。
   已换成生产实际在用的那一串。
3. **两个数值**。设计稿写 `start 0.1`（把 step 当成了 start）和
   `rtol 0.01 / ictol 0.05`；生产实际是 `--multiSweep=adaptive,0:0.1:40` 和
   `1e-05 / 0.001`。

## 文件

```
_common.py    假数据 + run 展开逻辑 + 三层参数表
_ui.py        三版共用的控件和逻辑（内容一套，布局三种）
stacked.py    1a
tabbed.py     1b
split.py      1c
v3_spec_file.py   另一条路：批次写 YAML、GUI 只当监控台（设计稿没画，留着备选）
```

自检（不弹窗、建完控件就退出）：`MOCKUP_SMOKE=1 python mockups\split.py`
