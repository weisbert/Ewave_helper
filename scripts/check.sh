#!/bin/sh
# 一票否决的验收命令。退出码就是判据 —— 过不了不许进下一阶段。
#
#   sh scripts/check.sh
#
# 无人值守跑的时候，这条命令是唯一的裁判。任何一项红就是红，不许"大体上通过"。
set -u
ROOT=$(git rev-parse --show-toplevel) || exit 1
cd "$ROOT" || exit 1
FAIL=0
step() { printf '\n=== %s ===\n' "$1"; }

step "1/5  golden fixture 未被篡改"
if [ -f tests/fixtures/production_cmd.local.json ]; then
    HAVE=$(python -c "import hashlib;print(hashlib.sha256(open('tests/fixtures/production_cmd.local.json','rb').read()).hexdigest())")
    WANT=$(cat tests/fixtures/production_cmd.sha256 2>/dev/null | tr -d ' \r\n')
    if [ "$HAVE" = "$WANT" ]; then
        echo "ok  期望值仍是人从真实生产命令抽出来的那份"
    else
        echo "FAIL  golden fixture 被改过了。"
        echo "      期望值只能由人从真实命令抽取。实现方改期望值 = 测试自己证明自己。"
        FAIL=1
    fi
else
    echo "skip  本机没有 fixture（公开克隆者正常）"
fi

step "2/5  单元测试"
if [ -d tests ] && ls tests/test_*.py >/dev/null 2>&1; then
    python -m unittest discover -s tests -t . -v 2>&1 | tail -25 || FAIL=1
else
    echo "FAIL  一个测试都没有"; FAIL=1
fi

step "3/5  红区标识符闸门"
sh scripts/redzone_scan.sh || FAIL=1

step "4/5  dry-run 冒烟"
if [ -f cli.py ] || [ -d ewave_batch ]; then
    python -m ewave_batch dry-run --self-test || FAIL=1
else
    echo "skip  CLI 还没有（P1 之前正常）"
fi

step "5/5  GUI 三版建得起来（headless）"
for f in stacked tabbed split; do
    if [ -f "mockups/$f.py" ]; then
        MOCKUP_SMOKE=1 python "mockups/$f.py" && echo "ok  mockups/$f" || { echo "FAIL mockups/$f"; FAIL=1; }
    fi
    if [ -f "gui/frames/$f.py" ]; then
        EWB_SMOKE=1 python -m gui.frames.$f && echo "ok  gui/frames/$f" || { echo "FAIL gui/frames/$f"; FAIL=1; }
    fi
done

printf '\n'
if [ "$FAIL" = "0" ]; then echo "check: ALL GREEN"; else echo "check: RED —— 不许进下一阶段"; fi
exit $FAIL
