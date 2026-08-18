#!/bin/sh
# diag_ab.sh -- 查清 A/B 为什么崩，C 为什么活。
#
# 已排除：并发竞态（串行独占节点重跑仍崩，确定性复现）。
# 已确认：三份 mesh 逐字节相同 ⇒ 不是几何问题。
# 剩下的唯一判别变量：--gds 的**路径**（A/B 在 /data/…/ewave_simulation/，C 在 /home/…）。
#
# 本脚本：第 1~5 节零成本（只读日志和环境），第 6 节做判别实验（约 5 分钟）。
# 用法：sh diag_ab.sh   跑完把 diag_ab.out 整份粘回来。

. ./cfg.sh
T="$MVP/tmp"; mkdir -p "$T"

echo "########## DIAG_AB ##########"
date

sec "1. 失败 run 的完整 emsolver.log（很小，全文）"
for d in "$RUN_A" "$RUN_B"; do
  hr; echo "### `basename $d`/emsolver.log"
  cat "$d/$TEMPDIR/emsolver.log" 2>&1
done

sec "2. 失败 run 的 ewave.log —— eresist 前后各 15 行"
for d in "$RUN_A" "$RUN_B"; do
  hr; echo "### `basename $d`/ewave.log"
  grep -n -i -B3 -A15 'eresist' "$d/$TEMPDIR/ewave.log" 2>&1 | head -40
done

sec "3. 成功 run（C）的同一段 —— 并排看差别"
hr; echo "### C_ourgds/emsolver.log 头 40 行"
head -40 "$RUN_C/$TEMPDIR/emsolver.log" 2>&1
hr; echo "### C_ourgds/ewave.log 的 eresist 前后"
grep -n -i -B3 -A15 'eresist' "$RUN_C/$TEMPDIR/ewave.log" 2>&1 | head -40

sec "4. ewave.log 全文 diff（B 失败 vs C 成功）—— 只差 GDS 路径，差异应当极少"
grep -v '^\[20' "$RUN_B/$TEMPDIR/ewave.log" > "$T/lb" 2>/dev/null
grep -v '^\[20' "$RUN_C/$TEMPDIR/ewave.log" > "$T/lc" 2>/dev/null
diff "$T/lb" "$T/lc" 2>&1 | head -40
echo "-- 带时间戳的版本，行数对比 --"
wc -l "$RUN_B/$TEMPDIR/ewave.log" "$RUN_C/$TEMPDIR/ewave.log" 2>&1

sec "5. 环境：盘、配额、eWave 的几个 work 路径"
echo "-- /home 剩余与配额 --"
df -h "$HOME" 2>&1
quota -s 2>&1 | head -10
echo "-- MVP 占用 --"
du -sh "$MVP" 2>&1
echo "-- NC_WORK_ROOT（eWave 自己的 work root）--"
echo "NC_WORK_ROOT=${NC_WORK_ROOT:-'(未设置)'}"
if [ -n "$NC_WORK_ROOT" ]; then
  ls -ld "$NC_WORK_ROOT" 2>&1
  if [ -w "$NC_WORK_ROOT" ]; then echo "  可写"; else echo "  ⚠️ 不可写"; fi
fi
echo "-- .epcd_datdir --"
ls -ld "$HOME/.epcd_datdir" 2>&1
echo "-- 两份 GDS 的权限/属主 --"
ls -l "$OFF_GDS" "$OUR_GDS" 2>&1
echo "-- 官方 GDS 所在目录是否可写（eWave 若要在旁边落临时文件就需要）--"
ls -ld "$OFFDIR" 2>&1
if [ -w "$OFFDIR" ]; then echo "  可写"; else echo "  ⚠️ 不可写"; fi

sec "6. ★ 判别实验：把官方 GDS **原样拷到 /home** 再跑一次 --all"
echo "只改 GDS 的**位置**，内容逐字节不变（下面 md5 会证明）。"
echo "  成功 ⇒ 问题出在路径/位置，与 GDS 内容无关"
echo "  失败 ⇒ 问题出在 GDS 内容（尽管网格相同），需要另找"
D_DIR="$W/runs/D_offgds_copied"
D_GDS="$W/gds_offcopy/`basename \"$OFF_GDS\"`"
rm -rf "$D_DIR" "`dirname \"$D_GDS\"`"
mkdir -p "$D_DIR" "`dirname \"$D_GDS\"`"
cp -p "$OFF_GDS" "$D_GDS"
echo "-- 拷贝前后 md5 必须相同 --"
md5sum "$OFF_GDS" "$D_GDS" 2>&1

PAY_D="$MVP/payload_D.sh"
sed -e "s|--workDir=$RUN_B|--workDir=$D_DIR|" \
    -e "s|--gds=$OFF_GDS|--gds=$D_GDS|" \
    -e "s|^\[B\]|[D]|" -e "s|\"\[B\]|\"[D]|" \
    "$MVP/payload_B.sh" > "$PAY_D"
chmod +x "$PAY_D"
echo "-- payload_D（应当只有 workDir 和 gds 两处与 payload_B 不同）--"
diff "$MVP/payload_B.sh" "$PAY_D" 2>&1

echo
echo "-- 提交 --"
date
dsub -A "$DSUB_A" -q "$DSUB_Q" -R "$DSUB_R" -I "$PAY_D" > "$LOGS/run_D.log" 2>&1
echo "dsub 返回 $?"
date
echo "节点        : `grep -m1 -oE 'Host : [^ ]+' \"$LOGS/run_D.log\"`"
if [ -f "$D_DIR/$TEMPDIR/resist.rst" ]; then
  echo "resist.rst  : `wc -c < \"$D_DIR/$TEMPDIR/resist.rst\"` 字节"
else
  echo "resist.rst  : 缺失"
fi
ls -l "$D_DIR/$TEMPDIR"/*.s[0-9]*p 2>/dev/null || echo "⚠️ 没产出 .sNp"
grep -iE 'archive_exception|exit failed|Calculated on|Wall Clock Time:' "$LOGS/run_D.log" | tail -5

hr
echo "-- 结论速览 --"
if [ -s "$D_DIR/$TEMPDIR/resist.rst" ]; then
  echo "  ✅ 官方 GDS 拷到 /home 后跑通 ⇒ **是路径/位置问题，不是 GDS 内容**"
else
  echo "  ❌ 官方 GDS 拷到 /home 仍崩 ⇒ **与位置无关，是 GDS 内容或别的因素**"
fi
echo "DIAG_AB DONE"
