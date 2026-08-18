#!/bin/sh
# relocate_and_diag.sh
#
#   第 0 节：把整个 MVP 树从 $HOME 搬到 workarea 下，$HOME 清空。
#   第 1 节：只读诊断（失败现场日志 + 环境）。
#   第 2 节：判别实验，**全部在 workarea 内**。
#
# 判别实验的变量（重新设计过，比之前更干净）：
#   之前是 "/home vs /data"，混进了文件系统这个额外变量。
#   现在三个 run 的 workDir 和 GDS 全在 workarea 同一个文件系统上，
#   唯一变的是 **GDS 所在的目录**：官方 ewave_simulation/<design>/ vs 我们自己的目录。
#
# 用法：sh relocate_and_diag.sh   跑完把 relocate_and_diag.out 整份粘回来。

. ./cfg.sh
set -e

echo "########## RELOCATE + DIAG ##########"
date

# ---------- 0. 迁移 ---------------------------------------------------------
sec "0. 把 MVP 树搬进 workarea"
WORKAREA=`dirname "$OFFDIR"`            # <workarea>/ewave_simulation
WORKAREA=`dirname "$WORKAREA"`         # <workarea>
NEWMVP="$WORKAREA/ewave_mvp"
OLDMVP="$MVP"
echo "workarea = $WORKAREA"
echo "现在的 MVP = $OLDMVP"
echo "目标 MVP   = $NEWMVP"

case "$NEWMVP" in
  */ewave_simulation/*) echo "错误：目标落在 ewave_simulation/ 里，拒绝" >&2; exit 2 ;;
esac

if [ "$OLDMVP" = "$NEWMVP" ]; then
  echo "已经在 workarea 下，跳过迁移。"
else
  echo "-- 迁移前的空间 --"
  du -sh "$OLDMVP" 2>&1
  df -h "$HOME" 2>&1 | tail -1
  df -h "$WORKAREA" 2>&1 | tail -1
  mkdir -p "$NEWMVP"
  echo "-- 复制（cp -a，先验证再删源）--"
  cp -a "$OLDMVP"/. "$NEWMVP"/
  _o=`du -s "$OLDMVP" | awk '{print $1}'`
  _n=`du -s "$NEWMVP" | awk '{print $1}'`
  echo "源 $_o KB / 目标 $_n KB"
  if [ "$_o" != "$_n" ]; then echo "⚠️ 大小不一致，保留源目录不删，请人工确认" >&2; else
    rm -rf "$OLDMVP"
    echo "✅ 源目录已删除，\$HOME 里不再有 ewave_mvp"
  fi
  ls -d "$HOME"/ewave_mvp 2>&1 || echo "  确认：\$HOME/ewave_mvp 已不存在"
fi

echo "-- 改写 payload 里写死的旧路径 --"
for f in "$NEWMVP"/payload_*.sh; do
  [ -f "$f" ] || continue
  sed -i "s|$OLDMVP|$NEWMVP|g" "$f"
  echo "  已改 `basename $f`"
done

echo "-- 把 MVP 写进 site.local.sh（以后所有 step 都用 workarea）--"
sed -i '/^MVP=/d' ./site.local.sh 2>/dev/null || true
echo "MVP=$NEWMVP" >> ./site.local.sh
cat ./site.local.sh

# 重新载入，后面全部用新路径
MVP="$NEWMVP"
W="$MVP/work"; LOGS="$MVP/logs"
RUN_A="$W/runs/A_officialports"; RUN_B="$W/runs/B_all"; RUN_C="$W/runs/C_ourgds"
OUR_GDS="$W/gds_ours/$CELL.gds"
mkdir -p "$LOGS"
echo "-- 迁移后 --"
du -sh "$MVP" 2>&1
df -h "$HOME" 2>&1 | tail -1
df -h "$WORKAREA" 2>&1 | tail -1

# ---------- 1. 只读诊断 -----------------------------------------------------
set +e
sec "1.1 失败 run 的完整 emsolver.log（很小，全文）"
for d in "$RUN_A" "$RUN_B"; do
  hr; echo "### `basename $d`/emsolver.log"; cat "$d/$TEMPDIR/emsolver.log" 2>&1
done

sec "1.2 失败 run 的 ewave.log —— eresist 前后"
for d in "$RUN_A" "$RUN_B"; do
  hr; echo "### `basename $d`"; grep -i -B3 -A12 'eresist' "$d/$TEMPDIR/ewave.log" 2>&1 | head -30
done

sec "1.3 成功 run（C）的同一段"
hr; echo "### C/emsolver.log 头 30 行"; head -30 "$RUN_C/$TEMPDIR/emsolver.log" 2>&1
hr; echo "### C/ewave.log 的 eresist 前后"; grep -i -B3 -A12 'eresist' "$RUN_C/$TEMPDIR/ewave.log" 2>&1 | head -30

sec "1.4 ewave.log 全文 diff（B 失败 vs C 成功，去掉时间戳行）"
T="$MVP/tmp"; mkdir -p "$T"
sed 's/^\[[0-9: -]*\]//' "$RUN_B/$TEMPDIR/ewave.log" > "$T/lb" 2>/dev/null
sed 's/^\[[0-9: -]*\]//' "$RUN_C/$TEMPDIR/ewave.log" > "$T/lc" 2>/dev/null
diff "$T/lb" "$T/lc" 2>&1 | head -40

sec "1.5 环境"
echo "-- eWave 自己的 work root --"
echo "NC_WORK_ROOT=${NC_WORK_ROOT:-'(未设置)'}"
[ -n "$NC_WORK_ROOT" ] && ls -ld "$NC_WORK_ROOT" 2>&1
[ -n "$NC_WORK_ROOT" ] && { [ -w "$NC_WORK_ROOT" ] && echo "  可写" || echo "  ⚠️ 不可写"; }
ls -ld "$HOME/.epcd_datdir" 2>&1
echo "-- 官方 design 目录是否可写 --"
ls -ld "$OFFDIR" 2>&1
[ -w "$OFFDIR" ] && echo "  可写" || echo "  ⚠️ 不可写 ← 若 eWave 要在 GDS 旁边落临时文件，这就是原因"
echo "-- 两份 GDS --"
ls -l "$OFF_GDS" "$OUR_GDS" 2>&1

# ---------- 2. 判别实验 -----------------------------------------------------
sec "2. 判别实验（三个 run，全部在 workarea 内，同一文件系统）"
echo "E1  GDS = 官方 ewave_simulation/<design>/     ← 复现失败（约 40 秒就崩）"
echo "E2  GDS = 官方那份**原样拷进** MVP 目录        ← 只改位置，内容一字节不变"
echo "E3  GDS = 我们 strmout 导的（也在 MVP 目录）   ← 控制组，确认搬家后仍能成功"
echo
echo "E1 崩 + E2 通  ⇒ 是 GDS 所在**目录**的问题（我们的工具天然规避）"
echo "E1 崩 + E2 崩 + E3 通 ⇒ 与目录无关，是 GDS **内容**（尽管 mesh 相同），另找"
echo "全崩（含 E3）⇒ 搬家本身引入了新问题，回头查"

COPY_DIR="$W/gds_offcopy"
rm -rf "$COPY_DIR"; mkdir -p "$COPY_DIR"
cp -p "$OFF_GDS" "$COPY_DIR/"
COPY_GDS="$COPY_DIR/`basename \"$OFF_GDS\"`"
echo "-- 拷贝前后 md5 必须相同 --"
md5sum "$OFF_GDS" "$COPY_GDS" 2>&1

run_exp() {
  _tag=$1; _gds=$2; _desc=$3
  _wd="$W/runs/$_tag"
  _pay="$MVP/payload_$_tag.sh"
  rm -rf "$_wd"; mkdir -p "$_wd"
  sed -e "s|--workDir=[^ ]*|--workDir=$_wd|" \
      -e "s|--gds=[^ ]*|--gds=$_gds|" \
      -e "s|\[B\]|[$_tag]|g" \
      "$MVP/payload_B.sh" > "$_pay"
  chmod +x "$_pay"
  hr
  echo "=== $_tag : $_desc ==="
  echo "  GDS     = $_gds"
  echo "  workDir = $_wd"
  date
  dsub -A "$DSUB_A" -q "$DSUB_Q" -R "$DSUB_R" -I "$_pay" > "$LOGS/run_$_tag.log" 2>&1
  echo "  dsub 返回 $?   节点 `grep -m1 -oE 'Host : [^ ]+' \"$LOGS/run_$_tag.log\"`"
  if [ -f "$_wd/$TEMPDIR/resist.rst" ]; then
    _n=`wc -c < "$_wd/$TEMPDIR/resist.rst"`
    echo "  resist.rst = $_n 字节"
  else
    echo "  resist.rst = 缺失"
  fi
  if ls "$_wd/$TEMPDIR"/*.s[0-9]*p >/dev/null 2>&1; then
    echo "  ✅ 产出 .sNp"; ls -l "$_wd/$TEMPDIR"/*.s[0-9]*p
  else
    echo "  ❌ 没产出 .sNp"
  fi
  grep -iE 'archive_exception|exit failed|Calculated on|Wall Clock Time:' "$LOGS/run_$_tag.log" | tail -4
  echo
}

run_exp E1 "$OFF_GDS"  "官方原位置的 GDS"
run_exp E2 "$COPY_GDS" "官方 GDS 原样拷进 MVP 目录"
run_exp E3 "$OUR_GDS"  "我们 strmout 导的 GDS（控制组）"

sec "3. 结论速览"
for e in E1 E2 E3; do
  _r="$W/runs/$e/$TEMPDIR/resist.rst"
  if [ -s "$_r" ]; then echo "  $e : ✅ 成功"; else echo "  $e : ❌ 失败"; fi
done
echo
echo "-- 全部产物 --"
ls -l "$W"/runs/*/"$TEMPDIR"/*.s[0-9]*p 2>/dev/null || echo "(无)"
echo "-- 占盘 --"
du -sh "$MVP" 2>&1
echo "-- \$HOME 确认干净 --"
ls -d "$HOME"/ewave_mvp 2>&1 || echo "  ✅ \$HOME 下已无 ewave_mvp"

hr
echo "RELOCATE+DIAG DONE"
