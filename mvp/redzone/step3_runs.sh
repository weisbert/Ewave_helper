#!/bin/sh
# step3 -- 三个单频点真跑。这是 MVP 的主体实验。
#
# 三个 run 之间**每次只动一个变量**，所以结论不会含糊：
#
#   A  官方 GDS  +  官方那一串 -p/-i          ← 基线（除频点外与生产逐字一致）
#   B  官方 GDS  +  --all                     ← A→B 只换了端口表达方式 ⇒ 判定 D1b
#   C  我们的GDS +  --all                     ← B→C 只换了 GDS 来源     ⇒ 判定 D1c
#
# 三个都加 --includePortOrder=1，于是端口映射是**可读文本**，不用从数值反推。
# 三个都用 --discreteFreq=5（单点 5 GHz）取代 --multiSweep=adaptive,0:0.1:40。
#   为什么这不影响结论：quasi-static 下网格与频率无关（--meshFreq/--cellSize/--cpw
#   在 help 里都写明 "takes effect only when --compound is used"，而硅工艺永不开
#   compound）⇒ 换频点不动网格，A/B/C 三者的网格完全相同。
#
# 用法：sh step3_runs.sh 2>&1 | tee step3.out

. ./cfg.sh
set -e

echo "########## STEP3 A/B/C 单频点真跑 ##########"
date
mkdir -p "$RUN_A" "$RUN_B" "$RUN_C" "$LOGS"

if [ ! -s "$OUR_GDS" ]; then
  echo "⚠️ 我们的 GDS 不存在（step1 没成功？）—— 只跑 A 和 B，跳过 C。"
  SKIP_C=1
fi

EW=`ewave_bin`

# run A 的端口 flag：**运行时从官方 run_ewave_*.sh 原样解析**（cfg.sh 里的 PORTS_OFFICIAL）。
# 不抄进源码有两个好处：端口名不进我们的仓库；也没有转录抄错的可能。
if [ -z "$PORTS_OFFICIAL" ]; then
  echo "错误：没能从 $OFF_RUNSH 解析出 -p/-i —— 先看 step0 §0.1 的解析结果" >&2
  exit 2
fi
echo "run A 用的端口 flag（解析自官方脚本，`echo \"$PORTS_OFFICIAL\" | tr ' ' '\n' | grep -c '^-p$'` 个 -p）"

# make_payload <tag> <workdir> <gds> <portflags>
# run B/C 的 flag 由 $EWAVE_COMMON **自己拼**（不抄官方脚本）—— 那正是 MVP 要证明的能力。
# run A 为了当基线才用解析来的原文。step0 §0.2 已经把两者的 flag 集合对过一遍。
make_payload() {
  _tag=$1; _wd=$2; _gds=$3; _ports=$4
  _pay="$MVP/payload_$_tag.sh"
  cat > "$_pay" <<EOF
#!/bin/sh
echo "[$_tag] host=\`hostname\`  pwd=\`pwd\`  start=\`date\`"
# ⚠️ 2026-08-18 实测：同一台计算节点上并发跑两个 eWave 会**静默互毁** ——
#    eresist 写出 0 字节的 resist.rst，emsolver 随即抛
#    boost::archive::archive_exception: input stream error。
#    冲突资源是**主机本地**的（异机的第三个 run 完全没事）。
#    下面两行是候选缓解（把临时文件和 cwd 都隔离到本 run 目录），**尚未验证充分性**。
#    真正的保险是 STEP3_PARALLEL 默认关闭（串行提交）。
mkdir -p $_wd/tmp
TMPDIR=$_wd/tmp; TMP=\$TMPDIR; TEMP=\$TMPDIR; export TMPDIR TMP TEMP
cd $_wd || exit 1
$EW $EWAVE_COMMON --workDir=$_wd \\
  --emssTechFile='$PTXT' \\
  --gds=$_gds \\
  --top=$CELL \\
  $_ports \\
  --includePortOrder=1 \\
  --discreteFreq=$FREQ \\
  --sparam=$CELL \\
  --corner=$CORNER --temperature=$TEMP --key=$KEY |sed -r 's/\x1B[[0-9;]*m//g'
echo "[$_tag] ewave exit=\$?  end=\`date\`"
EOF
  chmod +x "$_pay"
  echo "$_pay"
}

PAY_A=`make_payload A "$RUN_A" "$OFF_GDS" "$PORTS_OFFICIAL"`
PAY_B=`make_payload B "$RUN_B" "$OFF_GDS" "--all"`
PAY_C=""
if [ -z "$SKIP_C" ]; then
  PAY_C=`make_payload C "$RUN_C" "$OUR_GDS" "--all"`
fi

sec "3.1 三条 payload（存档在 $MVP，失败可单独手工重跑）"
for p in "$PAY_A" "$PAY_B" $PAY_C; do hr; echo "### $p"; cat "$p"; done

sec "3.2 提交（默认**串行** —— 见下）"
echo "⚠️ 2026-08-18 实测：三个 run 并发提交时，落在同一台节点的两个双双崩溃"
echo "   （resist.rst 写出 0 字节 → emsolver 抛 boost archive input stream error），"
echo "   落在另一台的那个完好。⇒ eWave 用了主机本地的固定名临时资源，同机并发不安全。"
echo "   所以这里**默认串行**。要试并发：STEP3_PARALLEL=1 sh step3_runs.sh"
echo "   （payload 里已加 per-run TMPDIR + cd 作为候选缓解，但充分性未验证）"
submit() {
  _t=$1; _p=$2
  echo "+ [$_t] dsub -A $DSUB_A -q $DSUB_Q -R \"$DSUB_R\" -I $_p"
  dsub -A "$DSUB_A" -q "$DSUB_Q" -R "$DSUB_R" -I "$_p" > "$LOGS/run_$_t.log" 2>&1
  echo "[$_t] dsub 返回 $? ，日志 $LOGS/run_$_t.log"
}
set +e
if [ "${STEP3_PARALLEL:-0}" = "1" ]; then
  echo "*** 并发模式（你显式要求的）***"
  PC=""
  submit A "$PAY_A" &
  PA=$!
  submit B "$PAY_B" &
  PB=$!
  if [ -n "$PAY_C" ]; then submit C "$PAY_C" & PC=$!; fi
  wait $PA; wait $PB
  if [ -n "$PC" ]; then wait $PC; fi
else
  echo "*** 串行模式（默认）*** 三个 run 依次跑，单个约 4~5 分钟"
  submit A "$PAY_A"
  submit B "$PAY_B"
  if [ -n "$PAY_C" ]; then submit C "$PAY_C"; fi
fi
set -e
date

sec "3.3 ★ 撞车指纹自检：resist.rst 是不是 0 字节"
for d in "$RUN_A" "$RUN_B" "$RUN_C"; do
  _r="$d/$TEMPDIR/resist.rst"
  if [ -f "$_r" ]; then
    _n=`wc -c < "$_r"`
    if [ "$_n" -eq 0 ]; then echo "  ❌ `basename $d`: resist.rst = 0 字节 ← 同机并发撞车的指纹"
    else echo "  ✅ `basename $d`: resist.rst = $_n 字节"; fi
  else
    echo "  ⚠️ `basename $d`: 没有 resist.rst"
  fi
done
grep -l 'archive_exception' "$LOGS"/run_*.log 2>/dev/null   && echo "  ↑ 上面这些日志里有 boost archive 异常" || true

sec "3.4 每个 run 的日志尾巴"
for t in A B C; do
  [ -f "$LOGS/run_$t.log" ] || continue
  hr; echo "### run $t 尾 25 行"; tail -25 "$LOGS/run_$t.log"
done

sec "3.5 产物落位（★ 同时在验：eWave 在我们给的 --workDir 下自己建了 $TEMPDIR/ 吗）"
for d in "$RUN_A" "$RUN_B" "$RUN_C"; do
  hr; echo "### $d"; ls -la "$d" 2>&1 | head -10
  ls -la "$d/$TEMPDIR" 2>&1 | head -25
done

hr
echo "STEP3 DONE  →  接着跑 step4_verify.sh"
