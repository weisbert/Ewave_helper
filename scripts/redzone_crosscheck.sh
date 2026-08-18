#!/bin/sh
# 红区**交叉核对** —— 把「审查 agent 每阶段手工做的那件事」变成机器判据。
#
#   sh scripts/redzone_crosscheck.sh
#
# 与 `redzone_scan.sh` 的分工：
#   redzone_scan     照**词表和结构规则**扫（我们事先想到的形状）
#   redzone_crosscheck  照**红区证据本身**扫（我们没想到的形状）
#
# 后者存在的理由，是 2026-08-18 夜跑 P3 抓到的一次真泄漏：
# 两个 8 位 Donau JOBID 被从 `references/ewave_donau_kit/` 逐字抄进了源码和测试。
# `redzone_scan` 报 clean —— 因为没人事先想到「job id 也是站点身份」，词表里没有它。
# 而它确实是：kit 的记录里那个 id 挂着一次真实运行的日期和节点名。
#
# 教训不是「把 job id 加进词表」，是**词表永远追不上现实**。
# 能追上的只有一条：**凡是红区证据里出现过的高熵 token，都不许出现在会进 git 的文件里。**
# 这条不需要预知形状，所以它抓得到我们还没想到的那一类。
#
# 判据：
#   候选 = `references/` 里所有 `[A-Za-z]?[0-9]{7,}` 形状的 token
#          （job id、工号、长数字 id 都是这个形状；工具语义的数值都远短于 7 位）
#   命中 = 候选出现在任何会进 git 的文件里
#
# 退出码：0 = 干净或无法核对（无证据），1 = 有命中（拒绝）。
#
# `references/` 不在 git 里（红区资料）⇒ 公开克隆者跑这条会**优雅跳过**，
# 这是对的：他们手上没有证据，也就无从泄漏。

set -u
ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "not a git repo"; exit 1; }
cd "$ROOT" || exit 1

EVIDENCE=references
if [ ! -d "$EVIDENCE" ]; then
    echo "crosscheck: skip —— 本机没有 $EVIDENCE/（公开克隆者的正常情况，无证据可核对）"
    exit 0
fi

FILES=$(git ls-files --cached --others --exclude-standard)
[ -z "$FILES" ] && exit 0

TOKENS=$(mktemp) || exit 1
HITS=$(mktemp) || exit 1
trap 'rm -f "$TOKENS" "$HITS"' EXIT

# 候选 token：长数字串（可带一个字母前缀，工号就是那个形状）。
# 7 位是刻意的下限：eWave 的工具语义数值（0.4 / 200 / 50 / 1e-05 / 401）都远短于它，
# 而 job id / 工号 / 长 id 都到得了。短了会淹在噪声里，长了会漏。
grep -rhoE '[A-Za-z]?[0-9]{7,}' "$EVIDENCE" 2>/dev/null | grep -vE '^[0-9]0*$' | sort -u > "$TOKENS"
# `grep -vE '^[0-9]0*$'` 剔掉"整数量级"（1000000000 = 1e9 的单位换算、100000000 …）。
# 它们是**工具语义的数值**不是 id：`mvp/redzone/step4_verify.sh` 里就有一个 1000000000
# 用来把 Hz 换算成 GHz。留着它们只会制造假阳性，而假阳性多了闸门就会被人关掉。

if [ ! -s "$TOKENS" ]; then
    echo "crosscheck: clean（证据里没有这个形状的 token）"
    exit 0
fi

# 一遍 `grep -F -f` 扫全部文件。**不要写成 token×文件 的双重循环** ——
# 证据里有上千个 token，双重循环要跑几分钟，慢到没人愿意把它挂进闸门，
# 于是闸门就形同虚设了。固定串匹配（-F）一次喂全部 token 才是对的量级。
: > "$HITS"
# shellcheck disable=SC2086  # FILES 是换行分隔的路径列表，此处要的就是分词
grep -nF -f "$TOKENS" -- $FILES 2>/dev/null | grep -v "^scripts/redzone_crosscheck" > "$HITS" || true

if [ -s "$HITS" ]; then
    cat "$HITS"
    echo ""
    echo "crosscheck: 上面这些 token 在 references/（红区证据）里逐字存在，拒绝。"
    echo "  它们不是编出来的 —— 是从证据里抄进来的，抄进去就会被发布出去。"
    echo "  修法：换成明显假的合成值（例如 10000001），并在注释里写明它是合成的。"
    echo "  形状可以照抄证据，值不许。"
    exit 1
fi

echo "crosscheck: clean（$(wc -l < "$TOKENS") 个证据 token，零命中）"
exit 0
