# site.example.sh -- 站点坐标模板。**这份是示例，值全是占位符，可以公开。**
#
#   cp site.example.sh site.local.sh    然后只改 OFFDIR 一行
#
# site.local.sh 含真实内网路径 → 已 gitignore，只留在红区那台机器上。

# ---- 唯一必填 --------------------------------------------------------------
# 官方 eWave GUI 跑过的那个 design 目录，即
#   <workarea>/ewave_simulation/<library>_<topCell>_<view>/
# 里面应当有 gdsout_setup / run_ewave_*.sh / remote_run_ewave.sh / <topCell>.gds
# 和一个 <corner>_<temp>/ 子目录。
#
# 其余坐标全部从这个目录里的文件解析出来 —— library / topCell / view / layerMap /
# ptxt / key / corner / temperature / 端口表 / dsub 的 -A -q -R，一个都不用手抄。
OFFDIR=/path/to/workarea/ewave_simulation/MyLib_MyCell_layout

# ---- 以下都可不填，填了就覆盖默认 ------------------------------------------

# MVP 的落地根目录。**推荐放 <workarea> 下自己的目录**：
#   - $HOME 在共享 EDA 机上通常有配额，三个 run 的 mesh 中间件轻松几百 MB
#   - workarea 是项目空间，容量够
# 唯一禁区是 <workarea>/ewave_simulation/ —— 那是设计师的 spine，本工具只读它，
# cfg.sh 会拒绝把 MVP 设进去。
MVP=/path/to/workarea/ewave_mvp

# 单频点，GHz。取一个生产扫描范围内的值即可
#FREQ=5

# 工具不在 PATH 上时给绝对路径
#EWAVE_ABS=/path/to/ewave
#STRMOUT_ABS=/path/to/cadence/tools/dfII/bin/strmout

# strmout 的 cwd 策略：include（默认，在 MVP 内建一个 INCLUDE 的 cds.lib）
# 或 workarea（直接 cd 过去跑）。include 失败时才改
#CDSWORK_MODE=include
# cds.lib 所在的根。默认取 OFFDIR 的上两级（= workarea），不对时在这指定
#CDSROOT=/path/to/workarea
