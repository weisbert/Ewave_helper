#!/bin/sh
# go_workarea.sh -- 把 MVP 落点改到 workarea，然后从 step1 重跑一遍。
#
# ★ 本脚本**不删除任何东西**。$HOME 下那棵旧树原封不动，最后只把清理命令打出来，
#   删不删、什么时候删由你决定。
#
# 根因回顾：$HOME 配额爆了 → eresist 写 resist.rst 写不下得到 0 字节，
#   但它照样打印 "Execute eresist done."（写失败被吞），emsolver 随后读空文件崩掉。
#   `df -h $HOME` 报的那个「可用」是误导 —— 那是文件系统剩余，不是用户配额。
#
# 为什么重跑而不是搬：strmout 只要 2 秒，搬 335MB 没意义；重跑还能让三个 run
#   在同一个最终位置产出，结果更干净。
#
# 用法：sh go_workarea.sh   约 16 分钟（step1 约 1 分，三个 run 各约 5 分，串行）。

. ./cfg.sh
set -e

echo "########## GO_WORKAREA ##########"
date

WORKAREA=`dirname "$(dirname "$OFFDIR")"`
NEWMVP="$WORKAREA/ewave_mvp"
OLDMVP="$MVP"

sec "0. \$HOME 下现有的东西（只读清点，本脚本不会删）"
if [ -d "$HOME/ewave_mvp" ]; then
  echo "-- 总占用 --"; du -sh "$HOME/ewave_mvp" 2>&1
  echo "-- 各子树 --"; du -sh "$HOME/ewave_mvp"/* 2>&1
  echo "-- 顶两层清单 --"; find "$HOME/ewave_mvp" -maxdepth 2 2>/dev/null | sort
else
  echo "\$HOME/ewave_mvp 不存在"
fi
echo
echo "-- \$HOME 下有没有别的疑似我们建的东西（只看，不动）--"
ls -ld "$HOME/.epcd_datdir" 2>&1 || echo "  .epcd_datdir : 不存在"
ls -la "$HOME" 2>&1 | head -30

sec "1. 实写取证：哪里写得下 64MB（df 和 quota 都骗过我，实写才算数）"
space_test() {
  _d=$1; _lbl=$2
  mkdir -p "$_d" 2>/dev/null || { echo "  $_lbl : 目录建不了"; return 1; }
  # 只创建并删除**这一个**由本脚本命名的文件，不碰别的
  _f="$_d/.mvp_spacetest_delete_me"
  rm -f "$_f"
  if dd if=/dev/zero of="$_f" bs=1048576 count=64 >/dev/null 2>&1 \
     && [ "`wc -c < \"$_f\" 2>/dev/null`" = "67108864" ]; then
    echo "  $_lbl : ✅ 能写下 64MB"; rm -f "$_f"; return 0
  else
    echo "  $_lbl : ❌ 写不下（配额或空间），实际写入 `wc -c < \"$_f\" 2>/dev/null || echo 0` 字节"
    rm -f "$_f"; return 1
  fi
}
set +e
[ -d "$HOME/ewave_mvp" ] && space_test "$HOME/ewave_mvp" "\$HOME（旧落点）"
space_test "$NEWMVP" "workarea（新落点）"
_ok=$?
echo "-- 参考值（会骗人）--"
df -h "$HOME" 2>&1 | tail -1
df -h "$WORKAREA" 2>&1 | tail -1
set -e
if [ "$_ok" != "0" ]; then
  echo "❌ workarea 也写不下 64MB，先解决空间再继续" >&2
  exit 2
fi

sec "2. 把 MVP 指到 workarea"
case "$NEWMVP" in
  */ewave_simulation/*) echo "错误：目标在 spine 里，拒绝" >&2; exit 2 ;;
esac
sed -i '/^MVP=/d' ./site.local.sh 2>/dev/null || true
echo "MVP=$NEWMVP" >> ./site.local.sh
echo "site.local.sh 现在是："
cat ./site.local.sh
echo
echo "旧落点 $OLDMVP  ← 原封不动保留"
echo "新落点 $NEWMVP"

sec "3. 重跑 step1（strmout，约 2 秒出 GDS）"
sh ./step1_strmout.sh 2>&1 | tail -25

sec "4. 重跑 step3（★ 串行，A→B→C 依次）"
sh ./step3_runs.sh 2>&1 | tail -60

sec "5. 判定"
# 直接用新落点算路径，不重新 source cfg.sh —— 避免变量取到旧值这种含糊
NW="$NEWMVP/work"
for r in "$NW/runs/A_officialports" "$NW/runs/B_all" "$NW/runs/C_ourgds"; do
  _n=`basename "$r"`
  if ls "$r/$TEMPDIR"/*.s[0-9]*p >/dev/null 2>&1; then echo "  ✅ $_n : 有 .sNp"
  else
    echo "  ❌ $_n : 无 .sNp"
    [ -f "$r/$TEMPDIR/resist.rst" ] && echo "      resist.rst = `wc -c < \"$r/$TEMPDIR/resist.rst\"` 字节"
  fi
done
echo
ls -l "$NW"/runs/*/"$TEMPDIR"/*.s[0-9]*p 2>/dev/null || echo "(无产物)"
echo
echo "新落点占用："; du -sh "$NEWMVP" 2>&1

hr
echo "★ 三个都有 .sNp 的话，接着跑： sh run.sh 4"
echo
echo "★ \$HOME 下那棵旧树本脚本没动。确认新的跑通之后，**你自己**决定要不要删："
echo "      du -sh $OLDMVP          # 先看看多大"
echo "      ls -la $OLDMVP          # 先看看有什么"
echo "      rm -rf $OLDMVP          # 确认无误后再执行"
echo "GO_WORKAREA DONE"
