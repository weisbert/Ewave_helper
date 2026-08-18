Ewave_helper 最小可行验证（MVP）
================================
本目录**不含任何站点标识符**，可以公开。站点坐标全部由你在 site.local.sh 里给一个
OFFDIR，其余从官方 run 目录现场解析。


它要证明什么
------------
一句话：**不开 GUI，我们自己拼的命令能跑出与官方逐位相同的 .sNp。**

这条链路有三个还没被证过的环节，MVP 逐个把它们钉死：

  ① strmout -templateFile <我们渲染的 gdsout_setup>  ==  官方 GUI 的导出   (D1c)
  ② ewave --all                                     ==  官方那一串 -p/-i   (D1b)
  ③ eWave 在我们自己给的 --workDir 下也自建 <corner>_<temp>/ 子目录        (解覆盖问题)

实验设计：三个 run 之间**每次只动一个变量**
  A  官方 GDS  + 官方 -p/-i   ← 基线
  B  官方 GDS  + --all        ← A→B 只换端口表达 ⇒ 判 ②
  C  我们的GDS + --all        ← B→C 只换 GDS 来源 ⇒ 判 ①

三个 run 都加 --includePortOrder=1（端口映射变成可读文本，不用从数值反推），
都用 --discreteFreq 单点取代生产那个密集扫描。

为什么单频点不影响结论：quasi-static 下网格与频率无关 —— help 里
--meshFreq / --cellSize / --cpw 三条都写明 "takes effect only when the --compound
is used"，而硅工艺永不开 --compound。所以 A/B/C 的网格与生产完全相同，
只是少算了几百个频点。省下的全是解算时间，没有省掉任何被验证的东西。

run B/C 的 flag 由 cfg.sh 里的 EWAVE_COMMON **自己拼**，不抄官方脚本 —— 那正是要
证明的能力。run A 为了当基线才用解析来的原文。step0 会把两者的 flag 集合对一遍。


它**不**验证什么
----------------
矩阵展开、resume、并发调度、归档规则、GUI —— 这些是我们自己的逻辑，
在开发机上单测就能覆盖，不值得占用一次红区往返。
MVP 只验证「我们与外部工具的接口」，那才是本地测不了的部分。


怎么跑
------
0) 把这些文件放到红区任意目录，但 **⚠️ 绝对不要放进 <workarea>/ewave_simulation/**
   —— 那是官方 GUI 的地盘、设计师的 spine，本工具对它只读。
   放 $HOME 或 <workarea>/ 下自己新建的目录都行。

   若传过来报 "\r" 相关的怪错，先跑：
       sed -i 's/\r$//' *.sh *.tmpl

1) 建站点配置——**只需要填一个值**：

       cp site.example.sh site.local.sh
       # 编辑 site.local.sh，把 OFFDIR 改成官方 GUI 跑过的那个 design 目录：
       #   <workarea>/ewave_simulation/<library>_<topCell>_<view>/

   library / topCell / view / layerMap / ptxt / key / corner / temperature /
   端口表 / dsub 的 -A -q -R —— 全部从那个目录里的 gdsout_setup、run_ewave_*.sh、
   remote_run_ewave.sh 解析出来，一个都不用手抄。

2) 逐步跑，跑完把 .out 整份粘回来。**用 run.sh，它把重定向关在 sh 里**，
   所以在 csh 提示符下也不会出问题：

       sh run.sh 0     # step0 探测   秒级，只读，不占集群
       sh run.sh 1     # step1 strmout 分钟级，不占集群
       sh run.sh 2     # step2 闸门   划完网格就退出，不解算
       sh run.sh 3     # step3 三个单频点作业，并发提交
       sh run.sh 4     # step4 判定   秒级

   ⚠️ 想直接手打的话，**登录 shell 是 csh，合并 stderr 要用 `|&` 不是 `2>&1`**
   （写 2>&1 会报 "Ambiguous output redirect."）：

       sh step0_probe.sh |& tee step0.out

   ★ step0 的 §0.1 是解析结果自检 —— **逐条核对一遍**，错一个后面全废。
   ★ step2 是闸门：--all 发现的端口数必须等于官方 -p 的个数，且编号顺序与官方一致
     （step0 §0.3 已就地证明官方顺序就是 LC_ALL=C 排序）。
     不对就**停在这里**把 step2.out 粘回来 —— 往下跑是浪费。

   step0/step1 失败不阻塞后面：step0 纯探测；step1 失败只是跳过 run C，A/B 照跑（②仍能判）。

3) 登录 shell 是 csh/tcsh，但这些脚本是 POSIX sh 写的，所以**一律用 `sh xxx.sh` 启动**，
   不要 source、不要 ./xxx.sh（除非确认有 x 权限）。


判定标准（step4 会自己打，这里写明白免得误读）
----------------------------------------------
  A vs B 数值逐 token 相同         ⇒ --all 与官方 -p/-i 等价，D1b 成立，
                                     「.sNp 不依赖 GUI」这条路走通
  A vs B 的 ! Port 注释块相同      ⇒ 端口映射逐字一致（比数值更直接）
  B vs C 数值逐 token 相同         ⇒ 我们的 GDS 与官方 GDS 功能等价，D1c 成立
  三个 .sNp 都在 <workDir>/<corner>_<temp>/ 下  ⇒ 独立 workDir 方案成立

  与生产那次密集扫描在同频点的比对是**软判据** —— 生产那份是 KMOR 拟合出来的，
  我们是直接解算，有差是正常的，看的是量级对不对。

  step1 的 GDS 字节比对也是**软判据** —— GDSII 头里带时间戳，必然不同。
  真正判 GDS 等价的是 run C，不是 md5。


会往哪写
--------
只写 $MVP 这一棵树（默认 $HOME/ewave_mvp；cfg.sh 会拒绝把它设进 ewave_simulation/）。

⚠️ **容量：量一下，别猜。** 三个 run 各带一套 mesh + pmsh/pmrg 中间件。
   官方跑过的那个 run 目录就是现成的实测基数（而且它是完整密集扫描，比我们单频点更大
   —— 所以是上界）：

       du -sh <OFFDIR>/<corner>_<temp>      # 每个 run 的上界，x3 就是总量
       df -h ~ ; quota -s                   # $HOME 够不够
       df -h <workarea>                     # workarea 够不够

   $HOME 装得下就用默认值。装不下再在 site.local.sh 里把 MVP 指到
   <workarea>/ewave_mvp（与 ewave_simulation/ 平级，不是套在里面）。
   还是紧的话，把 cfg.sh 里 EWAVE_COMMON 的 `-m` 去掉——它只管输出 mesh 文件，
   而且三个 run 同时去掉，A/B/C 的互比不受影响。

只写这些：
    $MVP/gdsout_setup.mvp        渲染出来的模板
    $MVP/cdswork/cds.lib         一行 INCLUDE，给 strmout 当 cwd 用
    $MVP/payload_*.sh            交给 dsub 的三个 payload（可单独手工重跑）
    $MVP/logs/                   dsub 日志
    $MVP/work/gds_ours/          我们导的 GDS
    $MVP/work/memest/            step2 的产物
    $MVP/work/runs/{A,B,C}/      三个 run

官方 run 目录（OFFDIR）与整个 workarea 全程**只读**。跑完想清干净：rm -rf $MVP
