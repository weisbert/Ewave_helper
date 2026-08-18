#!/bin/sh
# step1 -- 用「渲染出的 gdsout_setup + strmout -templateFile」自己导一份 GDS，
#          再与官方 GUI 导出的那份做字节层诊断。分钟级，不占集群。
#
# 验的是 D1c：模板保真那 8 个会改变 GDS 内容的字段（convertPin/pinAttNum/case/
# maxVertices/hierDepth/strmVersion/convertDot/arrayInstToScalar）。
#
# ⚠️ 字节不同 **不等于** 失败 —— GDSII 头里带时间戳，必然不同。
#    真正的判据是 step3 的 run C（我们的 GDS）与 run B（官方 GDS）解算结果是否一致。
#    这一步只提供诊断线索。
#
# 用法：sh step1_strmout.sh 2>&1 | tee step1.out

. ./cfg.sh
set -e

echo "########## STEP1 STRMOUT ##########"
date
mkdir -p "$OURGDS_DIR" "$LOGS"

sec "1.1 渲染 gdsout_setup（只替换随 design 变的 6 个字段，其余逐字复现官方）"
SETUP="$MVP/gdsout_setup.mvp"
sed -e "s|@@RUNDIR@@|$OURGDS_DIR|" \
    -e "s|@@LIBRARY@@|$LIBRARY|" \
    -e "s|@@TOPCELL@@|$CELL|" \
    -e "s|@@VIEW@@|$VIEW|" \
    -e "s|@@STRMFILE@@|$CELL.gds|" \
    -e "s|@@LOGFILE@@|$OURGDS_DIR/gds_out.log|" \
    -e "s|@@LAYERMAP@@|$LAYERMAP|" \
    gdsout_setup.tmpl > "$SETUP"
cat "$SETUP"
echo "-- 与官方 gdsout_setup 的差异（应当只有 runDir / logFile 两处路径）--"
diff "$OFF_SETUP" "$SETUP" || true

sec "1.2 决定 strmout 的 cwd（CDSWORK_MODE=$CDSWORK_MODE）"
if [ "$CDSWORK_MODE" = "include" ]; then
  mkdir -p "$MVP/cdswork"
  echo "INCLUDE $CDSROOT/cds.lib" > "$MVP/cdswork/cds.lib"
  CWD="$MVP/cdswork"
  echo "cwd = $CWD  （所有 Cadence 的散落写入都留在 MVP 内，不碰 spine）"
  echo "cds.lib 内容: `cat $MVP/cdswork/cds.lib`"
else
  CWD="$CDSROOT"
  echo "cwd = $CWD  （⚠️ fallback 模式：Cadence 可能往那里写 CDS.log 之类）"
fi

sec "1.3 跑 strmout"
STRMOUT=`strmout_bin`
echo "+ cd $CWD"
echo "+ $STRMOUT -templateFile $SETUP"
set +e
cd "$CWD" && "$STRMOUT" -templateFile "$SETUP" > "$LOGS/strmout.stdout" 2>&1
RC=$?
set -e
echo "exit status = $RC"
echo "-- strmout stdout 尾 30 行 --"
tail -30 "$LOGS/strmout.stdout"
echo "-- gds_out.log 尾 30 行 --"
tail -30 "$OURGDS_DIR/gds_out.log" 2>&1

if [ ! -s "$OUR_GDS" ]; then
  hr
  echo "STEP1 FAILED: 没产出 $OUR_GDS"
  echo "→ 若报的是 library 找不到 / cds.lib 相关，把 cfg.sh 里 CDSWORK_MODE 改成 workarea 重跑。"
  echo "  （这一条正是 PROJECT_BRIEF §7 P7a-1 要验的东西，失败本身也是有效信息）"
  exit 1
fi

sec "1.4 与官方 GDS 的字节诊断"
ls -l "$OFF_GDS" "$OUR_GDS"
md5sum "$OFF_GDS" "$OUR_GDS"
OS=`wc -c < "$OFF_GDS"`; NS=`wc -c < "$OUR_GDS"`
if [ "$OS" = "$NS" ]; then
  echo "SIZE: 完全相同 ($OS 字节) —— 强信号：内容大概率只差时间戳"
else
  echo "SIZE: 不同  官方=$OS  我们=$NS  差=`expr $NS - $OS`  ⚠️ 内容有实质差异"
fi
echo "-- 逐字节差异统计 --"
NDIFF=`cmp -l "$OFF_GDS" "$OUR_GDS" 2>/dev/null | wc -l`
echo "差异字节数 = $NDIFF"
echo "-- ★ 差异分类：时间戳 vs 字符串 --"
echo "   GDSII 时间戳是 12 个 int16，值都 <256 ⇒ 只有低字节变 ⇒ 差异【每隔一字节】出现。"
echo "   【连续】的差异字节 = 某个字符串字段变了（cell 名之类），那不是时间戳，要单独看。"
cmp -l "$OFF_GDS" "$OUR_GDS" 2>/dev/null > "$MVP/gdsdiff.txt"
awk '{ o=$1+0
       if (o == prev+1) run++
       else { if (run >= 3) consec += run; else alt += run; run = 1 }
       prev = o }
     END { if (run >= 3) consec += run; else alt += run
           print "   每隔一字节型（疑似时间戳）: " alt+0 " 字节"
           print "   连续型（疑似字符串）      : " consec+0 " 字节" }' "$MVP/gdsdiff.txt"

echo "-- 前 40 处差异（偏移 十进制；两个字节值 八进制）--"
head -40 "$MVP/gdsdiff.txt"

echo "-- 连续差异段的上下文（若上面【连续型】> 0，这些就是变了的字符串）--"
awk '{ o=$1+0
       if (o != prev+1) { if (run >= 3) print start; start = o; run = 1 } else run++
       prev = o }
     END { if (run >= 3) print start }' "$MVP/gdsdiff.txt" | head -5 > "$MVP/gdsruns.txt"
while read -r _o; do
  _s=`expr "$_o" - 24`
  if [ "$_s" -lt 1 ]; then _s=1; fi
  echo "   --- 偏移 $_o 附近（前后各若干字节，不可打印字符显示为 .）---"
  printf '   官方: '; tail -c +$_s "$OFF_GDS" | head -c 56 | tr -c '[:print:]' '.'; echo
  printf '   我们: '; tail -c +$_s "$OUR_GDS" | head -c 56 | tr -c '[:print:]' '.'; echo
done < "$MVP/gdsruns.txt"
rm -f "$MVP/gdsruns.txt"


echo "-- 差异偏移的分布（前 20 个差异簇的起始位置）--"
cmp -l "$OFF_GDS" "$OUR_GDS" 2>/dev/null | awk '{o=$1+0; if (o > last+8) {print o; n++} last=o} n>=20{exit}'

hr
echo "STEP1 DONE"
