#!/bin/sh
# step2 -- fail-fast 闸门。用官方那份 GDS + `--all` + `--memEstimate=1` 短路运行：
#          划完网格就报内存估算然后退出，不解算。
#
# 这一步单独存在的理由：它在不花任何解算成本的前提下回答 D1b 的生死问题 ——
#   ① `--all` 能不能和 `--cadencePins=1` 共用
#   ② `--all` 发现了几个端口、按什么顺序编号
#   ③ 顺带拿到内存估算值（→ BRIEF P8a 的预检阶段有没有价值）
#   ④ 顺带看 eWave 在我们给的 --workDir（不是官方的 `.`）下会不会自己建 <corner>_<temp>/
# 这四条里任何一条崩了，都不该往下跑 step3。
#
# 用法：sh step2_memestimate.sh 2>&1 | tee step2.out

. ./cfg.sh
set -e

echo "########## STEP2 --all + --memEstimate=1 ##########"
date
mkdir -p "$W/memest" "$LOGS"

EW=`ewave_bin`
PAY="$MVP/payload_memest.sh"

cat > "$PAY" <<EOF
#!/bin/sh
echo "[payload] host=\`hostname\`  pwd=\`pwd\`"
echo "[payload] （pwd 这行是在验：没有 -EP 时 dsub 把我们扔在哪 —— BRIEF 里悬着的一条）"
$EW $EWAVE_COMMON --workDir=$W/memest \\
  --emssTechFile='$PTXT' \\
  --gds=$OFF_GDS \\
  --top=$CELL \\
  --all --includePortOrder=1 \\
  --discreteFreq=$FREQ \\
  --sparam=$CELL \\
  --corner=$CORNER --temperature=$TEMP --key=$KEY \\
  --memEstimate=1 |sed -r 's/\x1B[[0-9;]*m//g'
echo "[payload] ewave exit=\$?"
EOF
chmod +x "$PAY"

sec "2.1 要提交的 payload"
cat "$PAY"

sec "2.2 提交（与官方 remote_run_ewave.sh 同形：-I 阻塞，无 -x all、无 -EP）"
echo "+ dsub -A $DSUB_A -q $DSUB_Q -R \"$DSUB_R\" -I $PAY"
set +e
# ⚠️ 不要 tee 上屏 —— eWave 会吐几千行 mesh 日志，淹掉判据。只落文件，末尾打 tail。
dsub -A "$DSUB_A" -q "$DSUB_Q" -R "$DSUB_R" -I "$PAY" > "$LOGS/memest.log" 2>&1
echo "dsub 返回 $?，完整日志在 $LOGS/memest.log（`wc -l < "$LOGS/memest.log"` 行）"
echo "-- 尾 25 行 --"
tail -25 "$LOGS/memest.log"
set -e

sec "2.3 判读"
echo "-- ★ 内存估算（定 -R mem= 的依据）--"
grep -oE 'expected memory: [0-9.]+ *GB' "$LOGS/memest.log" | tail -1 || echo "(没抓到 expected memory)"
echo "-- 相关上下文 --"
grep -iE 'memory|estimat' "$LOGS/memest.log" | head -20 || echo "(没抓到，看下面全文)"

echo
echo "-- ★ 端口发现结果（D1b 的核心证据）--"
grep -iE 'port' "$LOGS/memest.log" | head -60 || echo "(日志里没有 port 字样)"

echo
echo "-- eWave 在我们给的 workDir 下建了什么 --"
find "$W/memest" -maxdepth 2 2>/dev/null | head -40

echo
echo "-- ewave.log 里的端口段 --"
grep -iE 'port|P0[0-9][0-9]' "$W/memest/$TEMPDIR/ewave.log" 2>/dev/null | head -60 \
  || grep -iE 'port|P0[0-9][0-9]' "$W/memest/ewave.log" 2>/dev/null | head -60 \
  || echo "(找不到 ewave.log)"

echo
echo "-- 有没有报错 --"
grep -iE 'error|fatal|cannot|failed|unknown option|not found' "$LOGS/memest.log"   | grep -viE 'Invalid Via|0 error' | head -30 || echo "(无 error 字样)"

sec "2.4 ★ 闸门判据 —— --all 发现的端口 vs 官方 -p 列表（机械比对）"
# eWave 自己会打印（红区 step0 实测到的格式）：
#     [info] All Ports size is N:
#     Port: a b c ...
#     Ground:
# 所以这一步是文本比对，不用人眼数。
CAND="$LOGS/memest.log $W/memest/ewave.log $W/memest/$TEMPDIR/ewave.log"
PLOG=""
for _f in $CAND; do
  if [ -f "$_f" ] && grep -q 'All Ports size is' "$_f" 2>/dev/null; then PLOG="$_f"; break; fi
done
NEXPECT=`echo "$PINS_OFFICIAL" | grep -c .`
EXPECT=`echo "$PINS_OFFICIAL" | tr '\n' ' ' | sed 's/  */ /g; s/^ //; s/ $//'`
echo "官方 -p 给出：$NEXPECT 个"
echo "  $EXPECT"
echo
if [ -z "$PLOG" ]; then
  echo "⚠️ 没在任何日志里找到 'All Ports size is' —— 把下面的原始日志粘回来我看"
  tail -60 "$LOGS/memest.log" 2>/dev/null
else
  echo "端口信息来自：$PLOG"
  grep -E 'All Ports size is|^Port:|^Ground:' "$PLOG" | head -6
  echo
  NFOUND=`grep -oE 'All Ports size is [0-9]+' "$PLOG" | tr -d '' | head -1 | grep -oE '[0-9]+$'`
  GOT=`grep -E '^Port:' "$PLOG" | head -1 | sed 's/^Port:[ \t]*//; s/  */ /g; s/ *$//'`
  GND=`grep -E '^Ground:' "$PLOG" | head -1 | sed 's/^Ground:[ \t]*//; s/ *$//'`
  echo "-- 判据 --"
  if [ "$NFOUND" = "$NEXPECT" ]; then echo "  ✅ 端口数 $NFOUND == 官方 $NEXPECT"
  else echo "  ❌ 端口数 $NFOUND != 官方 $NEXPECT   ← 停，别跑 step3"; fi
  if [ "$GOT" = "$EXPECT" ]; then echo "  ✅ 端口顺序与官方 -p 逐位相同  ⇒ D1b 的实现层假设成立"
  else
    echo "  ❌ 端口顺序不同   ← 停，别跑 step3"
    echo "     官方 : $EXPECT"
    echo "     --all: $GOT"
  fi
  if [ -z "$GND" ]; then echo "  ✅ Ground 为空 —— 与官方全 signal port 一致"
  else echo "  ⚠️ Ground 非空: $GND   ← --all 把某些端口当接地了，与官方不同"; fi
fi

hr
echo "STEP2 DONE"
echo "★ §2.4 三条全 ✅ 才往下跑 step3。有 ❌ 就停在这里把 step2.out 粘回来。"
