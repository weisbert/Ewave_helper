# `offdir_synthetic/` —— 合成的「官方 run 目录」

`ewave_batch.core.discover` 的解析对象。**形状照真实文件，值全是明显的假占位符。**

## 为什么要它

`discover.py` 的全部意义是 CLAUDE.md 硬约束 1b：**坐标不手抄，现场解析**。
于是它的测试有一个先天矛盾 —— 要验解析对不对就得有输入，而真实输入是红区资料
（`references/probes/`、`references/ewave_donau_kit/`，永不进 git）。

解法：**形状进 git，值不进 git**。

| 这里的文件 | 形状抄自（红区，不进 git） |
|---|---|
| `gdsout_setup` | `references/probes/gdsout_setup_<lib>_<cell>_<view>.txt` |
| `run_ewave_typical_-40_0.sh` | `references/probes/run_ewave_typical_<temp>.sh` |
| `remote_run_ewave.sh` | `references/ewave_donau_kit/ewave/run_examples/remote_run_ewave.sh` |

**一个真实值都没有抄过来。** 抄的只有格式：tab 分隔、value 带引号、裸 flag
（`arrayInstToScalar`）、`--x=y` 与 `-e 0.4` 混用、末尾那段 `|sed -r 's/\x1B…//g'`
剥色管道、`dsub -A … -q … -R "…"` 三元组。

## 假值一览（测试里的期望值就是这些字面量）

| 坐标 | 这里写什么 |
|---|---|
| library / topCell / view | `MY_LIB` / `MY_CELL` / `layout_em` |
| pin 名 | `MY_GND` / `MY_INN` / `MY_INP` / `my_bias` / `my_tune`（**大小写混用是故意的**，见下） |
| PDK / workarea 路径 | `/fake/pdk/…` / `/fake/wa/…` |
| dsub 三元组 | `fake_account` / `fake_queue` / `cpu=20;mem=100000` |
| `--key` | `000000` |

## ⚠️ ptxt 路径是一个**陷阱**，不是随手编的

```
/fake/pdk/apps/ewave/ewaveinterface/process/typical/typical_v2/ptxt_enc/FAKEPDK_atypical_typical_V1.0_encrypted_package.ptxt
                                            ~~~~~~~ ~~~~~~~~~~            ~~~~~~~~ ~~~~~~~
                                            目录段   目录段(子串)          子串     ★ 只有这一处该换
```

corner 轴要**同时改两处**（BRIEF §7）：`--corner=` 的值，和 `--emssTechFile=` 的
**文件名**里那段 corner。少改一个 = 「目录名说 typical、实际用了别的工艺角」，
而且跑得出来、数字也像 —— 属于最难发现的一类错。

所以这条路径故意让 `typical` 出现四次：两次在目录段（其中一次是 `typical_v2` 这种
子串形态）、一次作为 `atypical` 的尾巴、一次是真正该换的那处。
`tests/test_discover.py::PtxtCornerFilter` 断言换 corner 之后**只有第四处变了**。
拿 `path.replace("typical", …)` 这种朴素写法会把四处一起换掉，那条测试会当场红。

## 规矩

* 改这里的任何值 = 改 `tests/test_discover.py` 里的期望值字面量，两边要一起改。
  （期望值是**手写字面量**，不许拿被测函数自己解析一遍 —— 那是自证。）
* 字段数是被断言的：`gdsout_setup` 24 个字段、`-p` 5 个、`-i` 4 个、
  ewave flag 22 个。加字段就要改计数断言，这是有意的摩擦。
* **绝不许**把红区真实值写进来。这个目录进 git，会公开。
