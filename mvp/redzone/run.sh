#!/bin/sh
# run.sh -- csh-proof 的步骤启动器。
#
# 为什么需要它：红区的登录 shell 是 csh/tcsh，那里 `2>&1` 是非法语法
# （csh 会报 "Ambiguous output redirect."），合并 stderr 要写 `|&`。
# 把重定向关进这个 sh 脚本里，你在 csh 提示符下只要打：
#
#     sh run.sh 0        # 跑 step0，输出同时上屏并存进 step0.out
#     sh run.sh 1        # …以此类推 0..4
#     sh run.sh all      # 依次跑 0 和 1（2/3/4 要人看过闸门再跑，不自动串）
#
# 直接手打也行，但记得用 csh 语法：  sh step0_probe.sh |& tee step0.out

usage() { echo "用法: sh run.sh <0|1|2|3|4|all>" >&2; exit 2; }
[ $# -eq 1 ] || usage

run_one() {
  _s=$1
  _f=`ls step${_s}_*.sh 2>/dev/null | head -1`
  if [ -z "$_f" ]; then echo "找不到 step${_s}_*.sh" >&2; return 2; fi
  echo "=========== 跑 $_f  →  step${_s}.out ==========="
  sh "$_f" 2>&1 | tee "step${_s}.out"
  echo "=========== 完成，输出存在 step${_s}.out ==========="
  echo
}

case "$1" in
  0|1|2|3|4) run_one "$1" ;;
  all)
    run_one 0 || exit $?
    run_one 1
    echo "★ step0 / step1 跑完了。先把 step0.out 的 §0.1 解析结果核一遍，"
    echo "  确认无误再 sh run.sh 2 —— step2 是闸门，过了才值得跑 step3。"
    ;;
  *) usage ;;
esac
