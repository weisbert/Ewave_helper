#!/bin/sh
# 红区标识符闸门 —— 提交/推送前扫一遍，防止站点坐标进公开仓库。
#
# 两层规则：
#   1. 通用结构规则（写在本脚本里，不含任何真实标识符，可以公开）
#   2. 站点词表（`.redzone_patterns.local`，**不进 git** —— 词表本身就是那份清单）
#
# 用法：
#   sh scripts/redzone_scan.sh              扫全部会被 git 跟踪的文件
#   sh scripts/redzone_scan.sh --staged     只扫已 stage 的（pre-commit hook 用这个）
#
# 退出码：0 = 干净，1 = 有命中（拒绝提交）。

set -u
ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "not a git repo"; exit 1; }
cd "$ROOT" || exit 1

if [ "${1:-}" = "--staged" ]; then
    FILES=$(git diff --cached --name-only --diff-filter=ACM)
else
    FILES=$(git ls-files)
fi
[ -z "$FILES" ] && exit 0

TMP=$(mktemp) || exit 1
trap 'rm -f "$TMP"' EXIT

# ---- 层 1：通用结构规则（不含真实标识符）---------------------------------
cat > "$TMP" <<'GENERIC'
/data/[A-Za-z0-9_]+/
/proj/[A-Za-z0-9_]+/[A-Za-z0-9_]
/software/[A-Za-z0-9_]+/
/home/[a-z][a-z0-9_]{2,}
-A[ =][A-Za-z][A-Za-z0-9_]*\.[A-Za-z]
GENERIC

# ---- 层 2：站点词表（本地文件，不进 git）----------------------------------
LOCAL="$ROOT/.redzone_patterns.local"
if [ -f "$LOCAL" ]; then
    grep -v '^[[:space:]]*#' "$LOCAL" | grep -v '^[[:space:]]*$' >> "$TMP"
else
    echo "redzone_scan: 没找到 .redzone_patterns.local —— 只跑了通用规则。"
    echo "              （公开克隆者看到这条是正常的；本机开发请把词表补上）"
fi

HITS=0
for f in $FILES; do
    [ -f "$f" ] || continue
    case "$f" in
        scripts/redzone_scan.sh) continue ;;   # 本脚本自带规则，会自己命中自己
    esac
    if grep -nEi -f "$TMP" "$f" > /dev/null 2>&1; then
        echo "--- $f"
        grep -nEi -f "$TMP" "$f" | head -8
        HITS=1
    fi
done

if [ "$HITS" = "1" ]; then
    echo ""
    echo "redzone_scan: 命中站点标识符，拒绝提交。"
    echo "  修法：换成占位符（<account> / <queue> / MY_LIB），或改成运行时发现，"
    echo "        或把该文件加进 .gitignore。见 CLAUDE.md 硬约束 1b。"
    exit 1
fi
echo "redzone_scan: clean"
exit 0
