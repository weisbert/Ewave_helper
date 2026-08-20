#!/bin/sh
# relocate_and_rerun.sh
#
# 根因已定：$HOME 配额爆了。
#   eresist 写 resist.rst 写不下 → 0 字节，**但它照样打印 "Execute eresist done."**，
#   写失败被吞掉；emsolver 随后读空文件 → boost::archive::archive_exception。
#   `df -h $HOME` 报的那个「可用」是误导 —— 那是文件系统剩余，不是用户配额。
#
# 本脚本：
#   0. 取证（在 $HOME 和 workarea 各写一个测试文件，直接看谁写得下）
#   1. 把整个 MVP 树搬进 workarea，$HOME 清空
#   2. 迁移后再取证一次
#   3. 串行重跑 A、B（C 已成功，其产物与路径无关，保留）
#   4. 判定
#
# 用法：sh relocate_and_rerun.sh   跑完把 .out 整份粘回来。约 12 分钟。

. ./cfg.sh
set -e

echo "########## RELOCATE + RERUN ##########"
date

WORKAREA=`dirname "$(dirname "$OFFDIR")"`
NEWMVP="$WORKAREA/ewave_mvp"
OLDMVP="$MVP"

# 直接写 64MB 测试文件 —— 比 df/quota 可靠，它们都骗过我了
space_test() {
  _d=$1; _lbl=$2
  mkdir -p "$_d" 2>/dev/null || { echo "  $_lbl : 目录都建不了"; return 1; }
  rm -f "$_d/.spacetest"
  if dd if=/dev/zero of="$_d/.spacetest" bs=1048576 count=64 >/dev/null 2>&1 \
     && [ "`wc -c < \"$_d/.spacetest\"`" = "67108864" ]; then
    echo "  $_lbl : ✅ 能写下 64MB"
    rm -f "$_d/.spacetest"; return 0
  else
    echo "  $_lbl : ❌ 写不下（配额/空间）  实际写入 `wc -c < \"$_d/.spacetest\" 2>/dev/null || echo 0` 字节"
    rm -f "$_d/.spacetest"; return 1
  fi
}

set +e
sec "0. 取证：到底哪里写得下"
echo "workarea = $WORKAREA"
echo "当前 MVP = $OLDMVP"
echo "-- du --"
du -sh "$OLDMVP" 2>&1
echo "-- 直接写测试（df 和 quota 都不可信，实写才算数）--"
space_test "$OLDMVP/spacetest" "\$HOME 下（当前 MVP）"
space_test "$WORKAREA/.spacetest_dir" "workarea 下"
rmdir "$WORKAREA/.spacetest_dir" 2>/dev/null
rmdir "$OLDMVP/spacetest" 2>/dev/null
echo "-- 参考（会骗人，只作对照）--"
df -h "$HOME" 2>&1 | tail -1
df -h "$WORKAREA" 2>&1 | tail -1
set -e

sec "1. 迁移 MVP → workarea"
case "$NEWMVP" in
  */ewave_simulation/*) echo "错误：目标落在 spine 里，拒绝" >&2; exit 2 ;;
esac
if [ "$OLDMVP" = "$NEWMVP" ]; then
  echo "已经在 workarea 下，跳过。"
else
  mkdir -p "$NEWMVP"
  echo "-- 复制（cp -a）--"
  cp -a "$OLDMVP"/. "$NEWMVP"/
  _o=`du -s "$OLDMVP" | awk '{print $1}'`
  _n=`du -s "$NEWMVP" | awk '{print $1}'`
  echo "源 $_o KB → 目标 $_n KB"
  if [ "$_o" != "$_n" ]; then
    echo "⚠️ 大小不一致，**不删源目录**，请人工确认后再删" >&2
  else
    rm -rf "$OLDMVP"
    echo "✅ 源目录已删，\$HOME 腾出 `expr $_o / 1024` MB"
  fi
fi
echo "-- \$HOME 现状 --"
ls -d "$HOME"/ewave_mvp 2>&1 || echo "  ✅ \$HOME 下已无 ewave_mvp"

echo "-- 改写 payload 里写死的旧路径 --"
for f in "$NEWMVP"/payload_*.sh; do
  [ -f "$f" ] || continue
  sed -i "s|$OLDMVP|$NEWMVP|g" "$f"
  echo "  `basename $f` : `grep -c \"$NEWMVP\" \"$f\"` 处指向新路径"
done

echo "-- 写进 site.local.sh --"
sed -i '/^MVP=/d' ./site.local.sh 2>/dev/null || true
echo "MVP=$NEWMVP" >> ./site.local.sh
cat ./site.local.sh

MVP="$NEWMVP"; W="$MVP/work"; LOGS="$MVP/logs"
RUN_A="$W/runs/A_officialports"; RUN_B="$W/runs/B_all"; RUN_C="$W/runs/C_ourgds"
mkdir -p "$LOGS"

set +e
sec "2. 迁移后再取证（必须能写下）"
space_test "$W" "新 MVP（workarea 下）"

sec "3. 串行重跑 A、B（C 已成功，保留其产物）"
for t in A B; do
  case $t in A) d="$RUN_A" ;; B) d="$RUN_B" ;; esac
  hr
  echo "=== 重跑 $t ==="
  date
  rm -rf "$d"; mkdir -p "$d"
  echo "  payload 里的路径检查："; grep -oE '\-\-workDir=[^ ]*|--gds=[^ ]*' "$MVP/payload_$t.sh"
  dsub -A "$DSUB_A" -q "$DSUB_Q" -R "$DSUB_R" -I "$MVP/payload_$t.sh" > "$LOGS/run_${t}3.log" 2>&1
  echo "  dsub 返回 $?   节点 `grep -m1 -oE 'Host : [^ ]+' \"$LOGS/run_${t}3.log\"`"
  if [ -f "$d/$TEMPDIR/resist.rst" ]; then
    echo "  resist.rst = `wc -c < \"$d/$TEMPDIR/resist.rst\"` 字节 （0 = 又没写下）"
  else
    echo "  resist.rst 缺失"
  fi
  ls -l "$d/$TEMPDIR"/*.s[0-9]*p 2>/dev/null || echo "  ❌ 没产出 .sNp"
  grep -iE 'archive_exception|exit failed|quota|no space|Calculated on|Wall Clock Time:' \
       "$LOGS/run_${t}3.log" | tail -5
  echo
done
date

sec "4. 判定"
for r in "$RUN_A" "$RUN_B" "$RUN_C"; do
  _n=`basename "$r"`
  if ls "$r/$TEMPDIR"/*.s[0-9]*p >/dev/null 2>&1; then echo "  ✅ $_n : 有 .sNp"
  else echo "  ❌ $_n : 无 .sNp"; fi
done
echo
echo "-- 全部产物 --"
ls -l "$W"/runs/*/"$TEMPDIR"/*.s[0-9]*p 2>/dev/null || echo "(无)"
echo "-- 占盘 --"
du -sh "$MVP" 2>&1

hr
echo "三个都有 .sNp 的话，接着跑： sh run.sh 4"
