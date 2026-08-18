#!/bin/sh
# step4 -- 判定。秒级，本地前台，不占集群。
#
# 判据（按重要性）：
#   ① A 与 B 的数值块逐 token 相同   ⇒ `--all` 与官方那一串 -p/-i **等价**  ⇒ D1b 成立
#   ② A 与 B 的 `! Port` 注释块相同  ⇒ 端口映射逐字一致（比数值更直接的证据）
#   ③ B 与 C 的数值块逐 token 相同   ⇒ 我们 strmout 出的 GDS 与官方 GDS **功能等价** ⇒ D1c 成立
#   ④ 三个 .sNp 都落在 <workDir>/<corner>_<temp>/ 下 ⇒ 独立 workDir 方案成立（解覆盖问题）
#   ⑤ 与生产那次密集扫描在同频点的值对得上（软判据，允许 KMOR 拟合误差）
#
# 用法：sh step4_verify.sh 2>&1 | tee step4.out       整份粘回来

. ./cfg.sh

T="$MVP/tmp"; mkdir -p "$T"

# 用 glob 定位 S 参数文件（不写死端口数 —— 万一 --all 找到的端口数与官方不同，
# 后缀就会跟着变，那本身就是最重要的发现）
find_snp() {
  find "$1" -name "*.s[0-9]*p" ! -name "*_sample*" 2>/dev/null | head -1
}
A=`find_snp "$RUN_A"`
B=`find_snp "$RUN_B"`
C=`find_snp "$RUN_C"`

echo "########## STEP4 VERIFY ##########"
date

sec "4.0 产物存在性 + 落位（判据 ④）"
echo "A = ${A:-<未找到>}"
echo "B = ${B:-<未找到>}"
echo "C = ${C:-<未找到>}"
echo
echo "★ 观察点：路径里有没有多出一层 '$TEMPDIR/'？"
echo "  有 → eWave 会在 --workDir 下自建 <corner>_<temp>/（BRIEF §7 P4b 的原结论）"
echo "  没有 → 那层是官方 GUI 自己算出来传进去的，eWave 无此行为"
echo "         （ewaveOnVir.log 的证据倾向这一边 —— 果真如此则输出路径完全由我们控制，更好）"
echo "  两种结果都不影响 ①②③ 的判定，只影响归档布局怎么设计。"
echo
echo "-- 各 run 目录下的全部参数文件 --"
find "$W/runs" -name '*.s[0-9]*p' -o -name '*.y[0-9]*p' 2>/dev/null | sort

# ---- 工具函数 --------------------------------------------------------------
# 把 Touchstone 数值块拍平成一列 token（跳过每个频点块首行的频率值）
# Touchstone 规则：频点块首行字段数为奇数（freq + 2k），续行为偶数
flatten() {
  awk '!/^!/ && !/^#/ && NF>0 { s=1; if (NF%2==1) s=2; for(i=s;i<=NF;i++) print $i }' "$1"
}
cmpnum() {
  _l=$1; _x=$2; _y=$3
  if [ ! -f "$_x" ] || [ ! -f "$_y" ]; then echo "$_l: 跳过（文件缺失）"; return; fi
  flatten "$_x" > "$T/x"; flatten "$_y" > "$T/y"
  nx=`wc -l < "$T/x"`; ny=`wc -l < "$T/y"`
  if [ "$nx" != "$ny" ]; then
    echo "$_l: ❌ token 数不同  $nx vs $ny  （端口数或频点数不一致，先别看数值）"
    return
  fi
  paste "$T/x" "$T/y" | awk -v L="$_l" '
    { d=$1-$2; if(d<0)d=-d
      a=$1; if(a<0)a=-a
      r=(a>1e-12)? d/a : d
      if(d>maxd){maxd=d; mi=NR}
      if(r>maxr)maxr=r
      n++ }
    END{ if(maxd==0) printf "%s: ✅ 逐 token 完全相同  (n=%d)\n", L, n
         else printf "%s: ⚠️ 有差异  n=%d  max|Δ|=%.6g  max相对Δ=%.6g  (首个最大差在第 %d 个 token)\n", L, n, maxd, maxr, mi }'
}

sec "4.1 判据 ② -- 端口映射注释块（★ 这是 D1b 最直接的证据，也回答 BRIEF P8③）"
echo "-- run A（官方 -p/-i）的 Port 注释 --"
grep -i '^!.*[Pp]ort' "$A" 2>/dev/null | head -25 || echo "(无)"
echo
echo "-- run B（--all）的 Port 注释 --"
grep -i '^!.*[Pp]ort' "$B" 2>/dev/null | head -25 || echo "(无)"
echo
echo "-- 两者 diff（空 = 端口映射逐字一致）--"
if [ -f "$A" ] && [ -f "$B" ]; then
  grep -i '^!.*[Pp]ort' "$A" > "$T/pa"; grep -i '^!.*[Pp]ort' "$B" > "$T/pb"
  if diff "$T/pa" "$T/pb" > "$T/pd" 2>&1; then echo "✅ 端口映射完全相同"; else echo "⚠️ 不同："; cat "$T/pd"; fi
else
  echo "(文件缺失，跳过)"
fi

sec "4.2 判据 ① / ③ -- 数值等价"
cmpnum "A vs B  (--all 是否 == 官方 -p/-i)" "$A" "$B"
cmpnum "B vs C  (我们的 GDS 是否 == 官方 GDS)" "$B" "$C"
cmpnum "A vs C  (端到端：我们全套 vs 官方全套)" "$A" "$C"

sec "4.3 全文 diff（若上面报 ✅，这里应当只剩注释/时间戳行）"
if [ -f "$A" ] && [ -f "$B" ]; then diff "$A" "$B" | head -30 || true; fi

sec "4.4 判据 ⑤ -- 与生产那次密集扫描在同频点的值对比（软判据）"
echo "生产那份是密集自适应扫描 + KMOR 拟合出来的，"
echo "我们是 --discreteFreq=5 直接解算 ⇒ 允许存在拟合误差，不是硬判据。"
# ⚠️ Touchstone 的频率单位写在 option line 里。红区实测生产文件是 "# HZ ..." ——
# 而 --discreteFreq 的单位是 GHz。不换算的话这里会静默抽出 0 行、判据⑤ 空过。
freq_in_file_units() {
  _u=`grep -m1 '^#' "$1" 2>/dev/null | awk '{print toupper($2)}'`
  case "$_u" in
    HZ)  _m=1000000000 ;;
    KHZ) _m=1000000 ;;
    MHZ) _m=1000 ;;
    *)   _m=1 ;;                 # GHZ 或读不出来时按 GHz
  esac
  awk -v f="$FREQ" -v m="$_m" 'BEGIN{printf "%.10g", f*m}'
}
FTARGET=`freq_in_file_units "$OFF_SNP"`
echo "生产文件的频率单位 = `grep -m1 '^#' "$OFF_SNP" 2>/dev/null | awk '{print $2}'`"
echo "  ⇒ ${FREQ} GHz 在该文件里应写作 $FTARGET"
# 用相对容差匹配，别用浮点相等
awk -v F="$FTARGET" '!/^!/ && !/^#/ && NF>0 {
   if (NF%2==1) { d=$1-F; if(d<0)d=-d; blk = (F!=0 && d/F<1e-6) || (F==0 && d==0) }
   if (blk) print }' "$OFF_SNP" > "$T/prod5" 2>/dev/null
echo "从生产 .sNp 抽出的同频点块：`wc -l < $T/prod5` 行"
if [ ! -s "$T/prod5" ]; then
  echo "  ⚠️ 抽不到 —— 生产那次的扫描点里没有恰好 $FTARGET 的频点。"
  echo "     生产实际算过的频点（_sample）："
  awk '!/^!/ && !/^#/ && NF>0 && NF%2==1 {print $1}' "$OFF_SAMPLE_SNP" 2>/dev/null | head -25
  echo "     判据⑤ 跳过（它本来就是软判据，不影响 ①②③④）"
fi
echo "-- option line 对比（单位/格式必须一致，否则数值没法比）--"
echo "  生产: `grep '^#' $OFF_SNP 2>/dev/null | head -1`"
echo "  我们: `grep '^#' $B 2>/dev/null | head -1`"
cmpnum "生产@同频点 vs run B" "$T/prod5" "$B"
echo "-- 两边的 S11（首行前 3 对）--"
echo "  生产: `head -1 $T/prod5`"
echo "  我们: `grep -v '^!' $B 2>/dev/null | grep -v '^#' | head -1`"

sec "4.5 我们这次 run 的 _sample 文件（验 BRIEF P4a 的推断）"
BSAMPLE=`find "$RUN_B" -name "*_sample*.s[0-9]*p" 2>/dev/null | head -1`
ls -la "$RUN_B"/*/ 2>&1 | head -20
echo "单频点直接解算时，.sNp 与 _sample.sNp 应当都只有 1 个频点："
for f in "$B" "$BSAMPLE"; do
  if [ -n "$f" ] && [ -f "$f" ]; then
    printf '%s  频点数=' "`basename \"$f\"`"
    awk '!/^!/ && !/^#/ && NF>0 && NF%2==1 {c++} END{print c+0}' "$f"
  fi
done

sec "4.6 收尾：占了多少盘"
du -sh "$MVP" 2>&1

hr
echo "STEP4 DONE"
