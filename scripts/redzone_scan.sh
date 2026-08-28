#!/bin/sh
# 红区标识符闸门 —— 提交/推送前扫一遍，防止站点坐标进公开仓库。
#
# 两层规则：
#   1. 通用结构规则（写在本脚本里，不含任何真实标识符，可以公开）
#   2. 站点词表（`.redzone_patterns.local`，**不进 git** —— 词表本身就是那份清单）
#
# 逐行豁免：行尾带 `redzone-allow` 注释的那一行不算命中。
#   给的是**用户明确决定要公开**的个别取值（2026-08-28：Donau 账号/队列，见
#   CLAUDE.md 硬约束 1b 的例外条）。刻意做成按行而不是按文件：整份文件豁免之后，
#   同一个文件里将来漏进去的别的坐标就再也扫不出来了。
#   每一处豁免都该在同一行里写明是谁、什么时候、为什么批的。
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
    # ⚠️ 不要写成裸 `git ls-files`：那只列**已跟踪**的文件，于是刚写出来还没 git add 的
    # 新代码一个都扫不到 —— 闸门会对着一堆未跟踪的新文件报 clean。
    # 2026-08-18 Phase 0 审查实测抓到：10 个新文件全是 `??`，check.sh 第 3 步照报 clean。
    # `--cached --others --exclude-standard` = 已跟踪 + 未跟踪但不被 .gitignore 排除的，
    # 正好是「将来会进 git 的全集」。被 ignore 的（PROJECT_BRIEF.md、*.local.* 等）仍然不扫，
    # 那是对的：它们本来就永远不会被发布出去。
    FILES=$(git ls-files --cached --others --exclude-standard)
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
    # 先扫，再把逐行豁免的那些剔掉。顺序不能反：先剔行会让"这一行本来命中什么"
    # 无从判断，而豁免的正当性恰恰是"我知道它命中了什么，并且我批准它"。
    hits=$(grep -nEi -f "$TMP" "$f" 2>/dev/null | grep -v 'redzone-allow')
    if [ -n "$hits" ]; then
        echo "--- $f"
        echo "$hits" | head -8
        HITS=1
    fi
done

if [ "$HITS" = "1" ]; then
    echo ""
    echo "redzone_scan: 命中站点标识符，拒绝提交。"
    echo "  修法：换成占位符（<account> / <queue> / MY_LIB），或改成运行时发现，"
    echo "        或把该文件加进 .gitignore。见 CLAUDE.md 硬约束 1b。"
    echo "        个别取值确实要公开时：行尾加 redzone-allow 注释并写明理由。"
    exit 1
fi
echo "redzone_scan: clean"
exit 0
