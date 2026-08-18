#!/bin/sh
# step0 -- 零成本探测。不动任何东西，只读。秒级。
# 目的：① 确认从官方 run 目录解析出来的坐标全对（往下每一步都建立在这上面）
#       ② 一次关掉 PROJECT_BRIEF §7 的 P2/P3/P4a/P5/P9/P7a-1
#       ③ 把我们源码里的生产默认 flag 与官方实际用的对一遍（免费的额外测试）
# 用法：sh step0_probe.sh 2>&1 | tee step0.out      然后把 step0.out 整份粘回来

. ./cfg.sh
T="$MVP/tmp"; mkdir -p "$T"

echo "########## STEP0 PROBE ##########"
date
echo "host=`hostname`  shell=$SHELL"

sec "0.1 从官方 run 目录解析出来的坐标（★ 逐条核对，错一个后面全废）"
echo "OFFDIR      = $OFFDIR"
echo "library     = $LIBRARY"
echo "topCell     = $CELL"
echo "view        = $VIEW"
echo "layerMap    = $LAYERMAP"
echo "ptxt        = $PTXT"
echo "key         = $KEY"
echo "corner      = $CORNER"
echo "temperature = $TEMP        → eWave 子目录 $TEMPDIR"
echo "dsub        = -A $DSUB_A -q $DSUB_Q -R \"$DSUB_R\""
echo "官方 GDS    = $OFF_GDS"
echo "官方 .sNp   = $OFF_SNP"
echo "端口数（从 -p 数） = `echo \"$PINS_OFFICIAL\" | grep -c .`"
echo "MVP 落地根   = $MVP   （★ 确认这在 workarea 下、且不在 ewave_simulation/ 里面）"
echo
echo "-- 存在性 --"
for p in "$OFFDIR" "$OFF_SETUP" "$OFF_RUNSH" "$OFF_REMOTE" "$OFF_GDS" "$OFF_SNP" "$PTXT" "$LAYERMAP"; do
  if [ -n "$p" ] && [ -e "$p" ]; then echo "OK      $p"; else echo "MISSING $p"; fi
done

sec "0.2 ★ 我们的生产默认 flag vs 官方实际在用的（免费的额外测试）"
echo "我们源码里写的（cfg.sh 的 EWAVE_COMMON，来自 PROJECT_BRIEF §6）："
echo "  $EWAVE_COMMON"
echo
echo "-- 集合比对（剔掉站点相关和端口相关的 flag）--"
echo "$EWAVE_COMMON" | norm_flags > "$T/ours.flags"
norm_flags < "$OFF_RUNSH" > "$T/off.flags"
if diff "$T/ours.flags" "$T/off.flags" > "$T/flags.diff" 2>&1; then
  echo "✅ 完全一致 —— 我们的默认 flag 表就是生产在用的那套"
else
  echo "⚠️ 有差异（< 是我们的，> 是官方的）："
  cat "$T/flags.diff"
fi

sec "0.3 ★ 端口顺序：官方 -p 的顺序 是不是 case-sensitive ASCII 排序"
echo "（这就是 references/checks/check_port_order.py 在红区的一行复现；"
echo "  LC_ALL=C sort 正是 case-sensitive ASCII 排序）"
echo "$PINS_OFFICIAL" > "$T/pins"
LC_ALL=C sort "$T/pins" > "$T/pins.sorted"
echo "-- 官方顺序（前 20）--"; head -20 "$T/pins"
if diff "$T/pins" "$T/pins.sorted" > /dev/null 2>&1; then
  echo "✅ 官方 -p 顺序 == LC_ALL=C 排序  ⇒ --all 的 lexicographical 若是 C collation 就逐位吻合"
else
  echo "⚠️ 不一致，首处差异："; diff "$T/pins" "$T/pins.sorted" | head -10
fi
echo "-- 对照：大小写不敏感排序（应当**不**一致，否则上面的吻合是巧合）--"
sort -f "$T/pins" > "$T/pins.ci" 2>/dev/null
if diff "$T/pins" "$T/pins.ci" > /dev/null 2>&1; then
  echo "⚠️ 也一致 —— 两种排序在这组 pin 上无法区分，collation 未被唯一确定"
else
  echo "✅ 不一致 —— collation 被唯一确定为 case-sensitive"
fi

sec "0.4 P3 -- ewave 是 wrapper 还是裸二进制"
EWB=`command -v ewave 2>/dev/null`
echo "which ewave: ${EWB:-'(not on PATH)'}"
if [ -n "$EWB" ]; then
  file "$EWB" 2>&1
  echo "-- 安装根目录（可执行文件往上三级）--"
  ls -la "`dirname \"$EWB\"`/../.." 2>&1 | head -20
  echo "-- ldd 缺失的 so --"
  ldd "$EWB" 2>&1 | grep -i 'not found' || echo "(无缺失 so)"
fi

sec "0.5 P7a-1 -- strmout 与 cds.lib"
echo "which strmout: `command -v strmout 2>/dev/null || echo '(not on PATH)'`"
echo "CDSROOT（推出来的 cds.lib 所在根）= $CDSROOT"
ls -la "$CDSROOT"/cds.lib "$CDSROOT"/lib.defs "$CDSROOT"/.cdsinit 2>&1
echo "-- cds.lib 里能不能找到这个 library（只看命中数）--"
grep -c "$LIBRARY" "$CDSROOT"/cds.lib 2>/dev/null || echo "(读不到或无命中)"

sec "0.6 P9 -- ptxt / layermap 路径的环境变量反查"
echo "PDK_LAYER_MAP_FILE = ${PDK_LAYER_MAP_FILE:-'(未设置)'}"
if [ -n "$PDK_LAYER_MAP_FILE" ]; then
  if [ "$PDK_LAYER_MAP_FILE" = "$LAYERMAP" ]; then echo "  == gdsout_setup 里的 layerMap  ✅ 可以用环境变量"
  else echo "  != gdsout_setup 里的 layerMap  ⚠️"; fi
fi
echo "-- 反查：哪些变量指向 PDK 根 --"
env | grep -F "$PDKROOT" 2>/dev/null | sort | head -30 || echo "(无)"
echo "-- 反查：哪些变量含 ewaveinterface --"
env | grep -F 'ewaveinterface' | sort || echo "(无)"
echo "-- 正查 --"
env | grep -iE 'pdk|ptxt|ewave|layer|tech|process|rfic|cds|cadence|unicad' | sort | head -40

sec "0.7 P5 -- ptxt 目录布局 / 各 corner 的文件名"
ls -d "$PTXT_PROCDIR"/*/ 2>&1
echo "-- 当前版本目录下的 ptxt 清单（corner 轴要靠这个命名规律）--"
ls -la "$PTXT_DIR"/ 2>&1 | head -30

sec "0.8 P4a -- .sNp 与 _sample.sNp 的频点数"
for f in "$OFF_SNP" "$OFF_SAMPLE_SNP"; do
  [ -n "$f" ] && [ -f "$f" ] || continue
  printf '%-34s 真频点数 = ' "`basename \"$f\"`"
  awk '!/^!/ && !/^#/ && NF>0 && NF%2==1 {c++} END{print c+0}' "$f"
done
echo "-- 生产 .sNp 的 option line + 前 2 行 --"
grep -v '^!' "$OFF_SNP" 2>/dev/null | head -3
echo "-- _sample 实际算过的频点里 3~8 GHz 那几个 --"
awk '!/^!/ && !/^#/ && NF>0 && NF%2==1 {print $1}' "$OFF_SAMPLE_SNP" 2>/dev/null | awk '$1>3 && $1<8'

sec "0.9 P2 -- 队列与账号"
dqueue 2>&1 | head -20
echo "-- 官方 GUI 实际用的提交行（我们照抄，不试探）--"
cat "$OFF_REMOTE" 2>&1

sec "0.10 官方两阶段流程的自述日志（验证我们的两阶段模型没漏步骤）"
echo "-- ewaveOnVir.log 头 40 行 --"; head -40 "$OFFDIR"/ewaveOnVir.log 2>&1
echo "-- ewaveOnVir.log 尾 20 行 --"; tail -20 "$OFFDIR"/ewaveOnVir.log 2>&1
echo "-- 成功那次的 ewave.log 尾 40 行（写 logparse 要用）--"
tail -40 "$OFFDIR/$TEMPDIR"/ewave.log 2>&1
echo "-- gds_out.log 尾 20 行（strmout 成功长什么样）--"
tail -20 "$OFFDIR"/gds_out.log 2>&1

hr
echo "STEP0 DONE"
