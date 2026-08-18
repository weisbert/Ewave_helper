#!/bin/sh
# fix_ab.sh -- step3 善后：验 mesh 是否等价 + 串行重跑崩掉的 A/B
#
# 背景：step3 三个 run 并发提交，落在**同一台节点**的 A、B 双双崩溃
#       （eresist 写出 0 字节 resist.rst → emsolver 抛
#        boost::archive::archive_exception: input stream error），
#       落在另一台的 C 完好。⇒ eWave 用了主机本地的固定名临时资源，同机并发不安全。
#
# 本脚本相对上一轮**只改「并发」这一个变量**（payload 完全复用已存档的那三份），
# 所以两个都成功就能归因到并发，而不是我们的命令有问题。
#
# 用法（在解包出来的脚本目录里，和 cfg.sh / site.local.sh 同级）：
#     sh fix_ab.sh
# 跑完把 fix_ab.out 整份粘回来。约 10 分钟。

. ./cfg.sh

echo "########## FIX_AB ##########"
date

sec "1. mesh 等价性（瞬时）—— A/B 用官方 GDS，C 用我们导的 GDS"
echo "三者 mesh 若 md5 相同 ⇒ 我们的 GDS 与官方 GDS 产出逐字节相同的网格，D1c 坐实。"
for f in pmsh.gtxt.msh pmrg.gtxt.mrg pmrg.gtxt; do
  echo "-- $f --"
  md5sum "$RUN_A/$TEMPDIR/$f" "$RUN_B/$TEMPDIR/$f" "$RUN_C/$TEMPDIR/$f" 2>&1
done
echo "-- 判定 --"
for f in pmsh.gtxt.msh pmrg.gtxt.mrg pmrg.gtxt; do
  _n=`md5sum "$RUN_A/$TEMPDIR/$f" "$RUN_B/$TEMPDIR/$f" "$RUN_C/$TEMPDIR/$f" 2>/dev/null \
      | awk '{print $1}' | sort -u | wc -l`
  if [ "$_n" = "1" ]; then echo "  ✅ $f : 三者 md5 完全相同"
  else echo "  ⚠️ $f : 有 $_n 种不同的 md5"; fi
done

sec "2. 崩溃指纹确认"
for d in "$RUN_A" "$RUN_B" "$RUN_C"; do
  _r="$d/$TEMPDIR/resist.rst"
  if [ -f "$_r" ]; then echo "  `basename $d` : resist.rst = `wc -c < \"$_r\"` 字节"
  else echo "  `basename $d` : 无 resist.rst"; fi
done
echo "-- 哪些 run 落在同一台机器 --"
for t in A B C; do
  [ -f "$LOGS/run_$t.log" ] && echo "  run $t : `grep -m1 -oE 'Host : [^ ]+' \"$LOGS/run_$t.log\"`"
done

sec "3. 串行重跑 A、B（每个约 4~5 分钟）"
for t in A B; do
  case $t in
    A) d="$RUN_A" ;;
    B) d="$RUN_B" ;;
  esac
  hr
  echo "=== 重跑 $t → $d ==="
  date
  rm -rf "$d"
  mkdir -p "$d"
  dsub -A "$DSUB_A" -q "$DSUB_Q" -R "$DSUB_R" -I "$MVP/payload_$t.sh" \
       > "$LOGS/run_${t}2.log" 2>&1
  echo "dsub 返回 $?"
  echo "节点        : `grep -m1 -oE 'Host : [^ ]+' \"$LOGS/run_${t}2.log\"`"
  if [ -f "$d/$TEMPDIR/resist.rst" ]; then
    echo "resist.rst  : `wc -c < \"$d/$TEMPDIR/resist.rst\"` 字节"
  else
    echo "resist.rst  : 缺失"
  fi
  ls -l "$d/$TEMPDIR"/*.s[0-9]*p 2>/dev/null || echo "⚠️ 没产出 .sNp"
  grep -iE 'archive_exception|exit failed|Calculated on|Wall Clock Time:' "$LOGS/run_${t}2.log" | tail -5
  echo
done
date

sec "4. 三个 run 的产物汇总"
ls -l "$W"/runs/*/"$TEMPDIR"/*.s[0-9]*p 2>/dev/null || echo "(没有 .sNp)"

hr
echo "FIX_AB DONE  →  三个都有 .sNp 的话，接着跑： sh run.sh 4"
