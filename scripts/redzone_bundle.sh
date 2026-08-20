#!/bin/sh
# redzone_bundle.sh —— 红区验证包。**一条命令**，跑完就知道这台机器能不能真跑。
#
#   bash scripts/redzone_bundle.sh <OFFDIR>
#
# `<OFFDIR>` = 官方 eWave GUI 跑过一次、里面有 `gdsout_setup` 的那个 design 目录。
# 不用先读代码，不用装任何东西（纯 POSIX sh + 已经装好的 python，只用 stdlib）。
#
# ---------------------------------------------------------------------------
# 它只读。放心跑。
# ---------------------------------------------------------------------------
# * **绝不写 `<workarea>/ewave_simulation/`**（那是设计师的 spine，CLAUDE.md 硬约束 4）；
# * **绝不写 `<OFFDIR>`** —— 连它的父目录都不碰。默认还会在跑前跑后各拍一次
#   `ls -lR <OFFDIR>` 逐字节对比，把「没动过」变成一条你自己能看见的证据
#   （`--no-witness` 关掉）；
# * **绝不提交任何 job** —— 不调 dsub，不调 ewave，不调 strmout；
# * 唯一会写的地方是**这个安装目录自己的** `.deploy/redzone_bundle/<时间戳>/`，
#   里面只有本次运行的日志（就是你要贴回来的那份）。`--no-log` 连这个都不写。
#
# ---------------------------------------------------------------------------
# 三步（顺序固定：前一步塌了，后一步的结论就不可信）
# ---------------------------------------------------------------------------
#   [1/3] 环境自检   这台机器的 python 能跑到哪个 tier。装了 deploy/doctor.sh
#                    就直接调它（tier 判定以它为准）；没有就用 bundle 内置的精简版。
#   [2/3] 全部单测   `python -m unittest discover -s tests -t .`
#                    没网的机器上，**一套全绿的测试是能拿到的最强证据** ——
#                    它同时证明了包完整落地、解释器可用、逻辑正确。
#   [3/3] 只读 dry-run
#                    `python -m ewave_batch.redzone_dryrun --offdir <OFFDIR>`
#                    解析真实 run 目录 -> 拼命令 -> 逐 flag 对着官方那条真实命令 diff
#                    -> 验端口顺序（D1b）-> 验 gdsout 模板（D1c）。
#
# ---------------------------------------------------------------------------
# 退出码（机器可判。csh/tcsh 里看 `echo $status`，不是 `$?`）
# ---------------------------------------------------------------------------
#   0  全绿 —— 环境够、单测全绿、生成的命令与官方那条一致。**可以往真跑走。**
#   1  bundle 自己跑不起来：没给 OFFDIR / 目录不存在 / 装的东西不全 / 参数写错。
#   2  dry-run 比对**有差异**（环境和单测是绿的）。先别真跑，看报告的 [4/5] [5/5]。
#   3  dry-run **没能比对** —— OFFDIR 里没有官方命令行（那个 design 只 stream 过、
#      没求解过）。argv 和落地目录照样打印了，只是没有基准可对。换一个求解跑完过的目录。
#   4  **单测有红** —— 包没落全，或者这个解释器不对。别信本次的任何结论。
#   5  **环境不满足 tier 1** —— 这台机器上没有能跑本工具的 python。
#   6  dry-run **跑不起来**（目录指错一层 / spec 非法 / 落点选在了 spine 里）。
#
#   同时红了几处时报**最靠前**的那一个（5 > 4 > 6 > 3 > 2 > 0）：先修地基，再看上层。
#
# ---------------------------------------------------------------------------
# 全部参数
# ---------------------------------------------------------------------------
#   <OFFDIR> | --offdir DIR   官方跑过的那个 design 目录（必填，**只读**）
#   --spec FILE               可选：批次 spec（YAML/JSON），透传给 dry-run
#   --limit N                 只详细打印前 N 个 run（透传给 dry-run）
#   --show-gdsout             连渲染出来的 gdsout_setup 一起打印（透传）
#   --check-only              只验「能不能起跑」（参数 / OFFDIR 形状 / 解释器 / 包完整性）
#                             就退，**不跑单测、不跑 dry-run、不调 doctor.sh**。两秒出结果
#   --python PATH             指定解释器（也可以 `setenv EWB_PYTHON /path/to/python3`）
#   --no-witness              跳过 OFFDIR 的跑前跑后对比
#   --no-log                  一个字节都不写，全部输出只走屏幕
#   -h | --help               打印这段抬头
#
# 手册：docs/REDZONE_FIRST_RUN.md（首次部署，从黄区 git pull 一路到这条命令）
#       docs/REDZONE_DRYRUN.md（第 3 步的输出怎么读、差异怎么办）

set -u

# ---------------------------------------------------------------------------
# 定位安装目录。**不用 git** —— 红区没有 git。
# ---------------------------------------------------------------------------
SELF=$0
SELF_DIR=$(dirname "$SELF")
ROOT=$(cd "$SELF_DIR/.." 2>/dev/null && pwd) || ROOT=""
if [ -z "$ROOT" ] || [ ! -d "$ROOT/ewave_batch" ]; then
    echo "redzone_bundle: 找不到安装目录（从 $SELF 往上没看到 ewave_batch/）。" >&2
    echo "  下一步：cd 进解压出来的那个目录，再敲" >&2
    echo "          bash scripts/redzone_bundle.sh <OFFDIR>" >&2
    exit 1
fi
cd "$ROOT" || exit 1

# 抬头就是 --help 的正文。用「从第 2 行读到第一条非注释行为止」而不是写死行号 ——
# 写死行号的版本会在抬头长了一行之后**静默**少打半页，而那种坏法没人会发现。
usage() {
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$SELF"
}

# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
OFFDIR=""
SPEC=""
LIMIT=""
SHOW_GDSOUT=0
FORCED_PY=${EWB_PYTHON:-}
WITNESS=1
DO_LOG=1
CHECK_ONLY=0

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            usage; exit 0 ;;
        --offdir)
            [ $# -ge 2 ] || { echo "--offdir 后面要跟目录" >&2; exit 1; }
            OFFDIR=$2; shift 2 ;;
        --spec)
            [ $# -ge 2 ] || { echo "--spec 后面要跟文件" >&2; exit 1; }
            SPEC=$2; shift 2 ;;
        --limit)
            [ $# -ge 2 ] || { echo "--limit 后面要跟数字" >&2; exit 1; }
            LIMIT=$2; shift 2 ;;
        --python)
            [ $# -ge 2 ] || { echo "--python 后面要跟解释器" >&2; exit 1; }
            FORCED_PY=$2; shift 2 ;;
        --show-gdsout)
            SHOW_GDSOUT=1; shift ;;
        --check-only)
            CHECK_ONLY=1; shift ;;
        --no-witness)
            WITNESS=0; shift ;;
        --no-log)
            DO_LOG=0; shift ;;
        --)
            shift ;;
        -*)
            echo "不认识的参数：$1（试试 -h）" >&2; exit 1 ;;
        *)
            if [ -z "$OFFDIR" ]; then
                OFFDIR=$1; shift
            else
                echo "多余的参数：$1（试试 -h）" >&2; exit 1
            fi ;;
    esac
done

SUGGEST_SNIPPET="from ewave_batch.core.discover import suggest_official_dirs as s"

if [ -z "$OFFDIR" ]; then
    echo ""
    echo "用法：bash scripts/redzone_bundle.sh <OFFDIR>"
    echo ""
    echo "  <OFFDIR> = 官方 eWave GUI 跑过一次的那个 design 目录。"
    echo "  判据是**里面有 gdsout_setup**。形状："
    echo "      <workarea>/ewave_simulation/<library>_<topCell>_<view>/"
    echo ""
    echo "  不知道是哪个就让机器找（把 <workarea> 换成你的工作区）："
    echo "      python -c \"$SUGGEST_SNIPPET; print(chr(10).join(s('<workarea>')))\""
    echo ""
    echo "  这条命令只读：不写 OFFDIR、不写 ewave_simulation/、不提交任何 job。"
    echo "  详见 docs/REDZONE_FIRST_RUN.md，或 bash scripts/redzone_bundle.sh -h"
    echo ""
    exit 1
fi

if [ ! -d "$OFFDIR" ]; then
    echo "redzone_bundle: OFFDIR 不是一个目录：$OFFDIR" >&2
    echo "  下一步：确认路径拼写。csh 里 ~ 在引号内不展开，写绝对路径最稳。" >&2
    exit 1
fi
OFFDIR_ABS=$(cd "$OFFDIR" 2>/dev/null && pwd) || OFFDIR_ABS=$OFFDIR
if [ ! -f "$OFFDIR_ABS/gdsout_setup" ]; then
    echo "redzone_bundle: $OFFDIR_ABS 里没有 gdsout_setup" >&2
    echo "  => 这不是一个官方 design 目录。最常见的两种指错：" >&2
    echo "     · 指到了上一级（ewave_simulation/）—— 往下挪一层，进 <library>_<topCell>_<view>/" >&2
    echo "     · 指进了 <corner>_<temp>/ 子目录 —— 往上挪一层" >&2
    echo "  让机器列候选：" >&2
    echo "     python -c \"$SUGGEST_SNIPPET; print(chr(10).join(s('$OFFDIR_ABS/..')))\"" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# 日志目录。**只在安装目录自己的 .deploy/ 下** —— 和 deploy.sh 同一条规矩：
# 不写 /tmp、不写 /opt、不写 /var、不 mktemp，父目录永不修改。
#
# ⚠ 故意**不放在 `.deploy/tmp/` 里面**：`deploy/doctor.sh` 开头就 `rm -rf <install>/.deploy/tmp`，
#   退出时 trap 里再 `rm -rf` 一次。本脚本第 1 步会调 doctor.sh ⇒ 日志放那儿会被它当场删掉
#   （2026-08-19 实测过：doctor.log 和 offdir.before 一起没了，然后 `cat` 报 No such file）。
#   所以我们住在 `.deploy/redzone_bundle/`，是 `tmp/` 的**兄弟**不是它的孩子。
# ---------------------------------------------------------------------------
STAMP=$(date +%Y%m%d-%H%M%S 2>/dev/null) || STAMP=run
[ -n "$STAMP" ] || STAMP=run
WORK=""
# --check-only 什么都不跑 ⇒ 也不该留下一个空的日志目录（「它只写这一个地方」这句话
# 越干净越好：一个空目录也是一次写）。
if [ "$DO_LOG" = "1" ] && [ "$CHECK_ONLY" != "1" ]; then
    WORK="$ROOT/.deploy/redzone_bundle/$STAMP"
    if ! mkdir -p "$WORK" 2>/dev/null; then
        echo "⚠ 建不了日志目录 $WORK（安装目录不可写？）—— 当成 --no-log 继续，结论照样有效。"
        WORK=""
    fi
fi

LOG_TESTS=""
LOG_DRY=""
LOG_DOCTOR=""
WITNESS_BEFORE=""
WITNESS_AFTER=""
if [ -n "$WORK" ]; then
    LOG_TESTS="$WORK/unittest.log"
    LOG_DRY="$WORK/dryrun.log"
    LOG_DOCTOR="$WORK/doctor.log"
    WITNESS_BEFORE="$WORK/offdir.before"
    WITNESS_AFTER="$WORK/offdir.after"
else
    WITNESS=0
fi

rule() { echo "============================================================"; }
thin() { echo "------------------------------------------------------------"; }
step() { echo ""; rule; echo "  $1"; rule; }
yesno() { if [ "$1" = "YES" ]; then echo "可用  "; else echo "不可用"; fi; }

# ---------------------------------------------------------------------------
# 抬头 —— 先把「我要干什么、我不会干什么」说清楚，再动手。
# ---------------------------------------------------------------------------
rule
echo "  ewave_batch 红区验证包 —— 环境 + 全部单测 + 只读 dry-run"
rule
echo "  安装目录 : $ROOT"
echo "  OFFDIR   : $OFFDIR_ABS   (只读)"
if [ -n "$WORK" ]; then
    echo "  日志     : $WORK"
elif [ "$CHECK_ONLY" = "1" ]; then
    echo "  日志     : (--check-only: 不写任何文件)"
else
    echo "  日志     : (--no-log: 不写任何文件)"
fi
echo ""
echo "  这条命令**不写 OFFDIR、不写 ewave_simulation/、不提交任何 job**。"
echo "  唯一会写的地方是上面那个日志目录（在这个安装目录里面）。"
if [ -f "$ROOT/VERSION" ]; then
    echo ""
    echo "  VERSION（打包时戳上的 commit）："
    sed -n '1,3p' "$ROOT/VERSION" 2>/dev/null | sed 's/^/      /'
fi

# 跑之前给 OFFDIR 拍一张
if [ "$WITNESS" = "1" ]; then
    LC_ALL=C ls -lR -- "$OFFDIR_ABS" > "$WITNESS_BEFORE" 2>/dev/null || WITNESS=0
fi

# ---------------------------------------------------------------------------
# [1/3] 环境自检
# ---------------------------------------------------------------------------
step "[1/3] 环境自检 —— 这台机器能跑到哪个 tier"

# 内置探针。输出全是 KEY=VALUE，纯 ASCII，机器可判。
probe() {
    "$1" - <<'PYEOF' 2>/dev/null
import importlib
import os
import sys

sys.path.insert(0, os.getcwd())


def emit(key, value):
    print("%s=%s" % (key, value))


emit("PY_VERSION", "%d.%d.%d" % sys.version_info[:3])
emit("PY_EXE", sys.executable or "?")
emit("PY_OK", "YES" if sys.version_info[:2] >= (3, 10) else "NO")
emit("PY_PREFERRED", "YES" if sys.version_info[:2] == (3, 11) else "NO")
emit("PY_VENV", "YES" if sys.prefix != getattr(sys, "base_prefix", sys.prefix) else "NO")

missing = [p for p in ("ewave_batch", "tests", "docs", "cli.py") if not os.path.exists(p)]
emit("TREE_MISSING", ",".join(missing))

bad = []
for name in (
    "ewave_batch",
    "ewave_batch.cli",
    "ewave_batch.redzone_dryrun",
    "ewave_batch.core.cmd",
    "ewave_batch.core.discover",
    "ewave_batch.core.layout",
    "ewave_batch.core.matrix",
    "ewave_batch.core.spec",
    "ewave_batch.tools.strmout",
    "ewave_batch.sched.driver",
):
    try:
        importlib.import_module(name)
    except Exception as exc:  # 探针要把任何失败原样报出来
        bad.append("%s (%s: %s)" % (name, exc.__class__.__name__, exc))
emit("IMPORT_BAD", " | ".join(bad))

try:
    pkg = importlib.import_module("ewave_batch")
    emit("TOOL_VERSION", getattr(pkg, "__version__", "?"))
    emit("PKG_FILE", getattr(pkg, "__file__", "?"))
except Exception:
    emit("TOOL_VERSION", "?")
    emit("PKG_FILE", "?")

# tier 2 用的三个工具：find_tool 就是 shutil.which，这里走同一条路。
try:
    find_tool = importlib.import_module("ewave_batch.core.discover").find_tool
except Exception:
    import shutil

    def find_tool(name):
        return shutil.which(name)

for tool in ("ewave", "strmout", "dsub", "djob"):
    emit("BIN_" + tool, find_tool(tool) or "")

try:
    importlib.import_module("tkinter")
    has_tk = True
except Exception:
    has_tk = False
emit("MOD_tkinter", "YES" if has_tk else "NO")
emit("ENV_DISPLAY", os.environ.get("DISPLAY", ""))

tier1 = not missing and not bad and sys.version_info[:2] >= (3, 10)
tier2 = tier1 and all(find_tool(t) for t in ("ewave", "strmout", "dsub"))
tier3 = tier1 and has_tk and bool(os.environ.get("DISPLAY"))
emit("TIER1", "YES" if tier1 else "NO")
emit("TIER2", "YES" if tier2 else "NO")
emit("TIER3", "YES" if tier3 else "NO")
PYEOF
}

getval() { printf '%s\n' "$PROBE_OUT" | sed -n "s/^$1=//p" | head -1; }

if [ -n "$FORCED_PY" ]; then
    CANDIDATES=$FORCED_PY
else
    CANDIDATES="python3.11 python3 python python3.13 python3.12 python3.10"
fi

PY=""
PROBE_OUT=""
BEST_OUT=""
BEST_PY=""
TRIED=""
for c in $CANDIDATES; do
    command -v "$c" >/dev/null 2>&1 || continue
    PROBE_OUT=$(probe "$c")
    if [ -z "$PROBE_OUT" ]; then
        TRIED="$TRIED
    $c  -> 探针跑不起来（不是可用的 python？）"
        continue
    fi
    TRIED="$TRIED
    $c  -> python $(getval PY_VERSION), tier1=$(getval TIER1)"
    if [ -z "$BEST_PY" ]; then BEST_PY=$c; BEST_OUT=$PROBE_OUT; fi
    if [ "$(getval TIER1)" = "YES" ]; then PY=$c; break; fi
done

if [ -z "$PY" ]; then
    PROBE_OUT=$BEST_OUT
    echo "FAIL  这台机器上没有能跑本工具的 python（连 tier 1 都到不了）。"
    echo ""
    echo "  试过的解释器：$TRIED"
    if [ -n "$BEST_OUT" ]; then
        echo ""
        echo "  最接近的那个（$BEST_PY）卡在："
        [ "$(getval PY_OK)" = "NO" ] && \
            echo "    · python 版本是 $(getval PY_VERSION)，本工具要 3.10+（目标机基线是 3.11.4）"
        [ -n "$(getval TREE_MISSING)" ] && \
            echo "    · 安装目录缺东西：$(getval TREE_MISSING)  —— 包没落全"
        [ -n "$(getval IMPORT_BAD)" ] && \
            echo "    · import 失败：$(getval IMPORT_BAD)"
    fi
    echo ""
    echo "  下一步："
    echo "    1) 先 ma python/3.11.4 —— 红区的 python 是 module 加载出来的，登录默认那个常常太老；"
    echo "    2) 还不行就明确指一个："
    echo "         bash scripts/redzone_bundle.sh <OFFDIR> --python /path/to/python3"
    echo "       （csh 里也可以先 setenv EWB_PYTHON /path/to/python3）；"
    echo "    3) 如果是「安装目录缺东西」，那是包没落全 —— 回到"
    echo "       docs/REDZONE_FIRST_RUN.md 的步骤 5，重做一次解压/部署。"
    echo ""
    rule
    echo "  结论：环境不满足 tier 1，后面两步没跑。退出码 5。"
    rule
    exit 5
fi

PY_VERSION=$(getval PY_VERSION)
TIER1=$(getval TIER1)
TIER2=$(getval TIER2)
TIER3=$(getval TIER3)

# --check-only：到这里为止「能不能起跑」已经全部有答案了（参数、OFFDIR 形状、解释器、
# 包完整性），而后面两步要花一分钟。所以给一条早退的路：先花两秒确认这条命令会跑起来，
# 再决定要不要等那一分钟。**它不跑单测、不跑 dry-run、不调 doctor.sh。**
if [ "$CHECK_ONLY" = "1" ]; then
    echo "  python      : $PY  ($PY_VERSION)  ->  $(getval PY_EXE)"
    echo "  ewave_batch : $(getval TOOL_VERSION)   <- $(getval PKG_FILE)"
    echo "  tier1=$TIER1 tier2=$TIER2 tier3=$TIER3"
    echo ""
    echo "OK  --check-only：起跑条件都满足。去掉 --check-only 就会依次跑："
    echo "      [1/3] bash deploy/doctor.sh --python $PY"
    echo "      [2/3] $PY -m unittest discover -s tests -t ."
    echo "      [3/3] $PY -m ewave_batch.redzone_dryrun --offdir $OFFDIR_ABS"
    echo "    这三步都没跑。退出码 0。"
    exit 0
fi

# 有 deploy/doctor.sh 就调它 —— tier 判定的权威是它，本脚本的探针只是它的精简版。
DOCTOR_RC="-"
if [ -f "$ROOT/deploy/doctor.sh" ]; then
    echo "（下面这段由 deploy/doctor.sh 输出 —— tier 判定以它为准）"
    thin
    # 明确把**我们选中的那个解释器**交给它，两步才是同一个故事
    # （doctor 自己的候选顺序是 python3 优先，可能和这里选的不是同一个）。
    if [ -n "$LOG_DOCTOR" ]; then
        bash "$ROOT/deploy/doctor.sh" --python "$PY" > "$LOG_DOCTOR" 2>&1
        DOCTOR_RC=$?
        cat "$LOG_DOCTOR" 2>/dev/null
    else
        bash "$ROOT/deploy/doctor.sh" --python "$PY" 2>&1
        DOCTOR_RC=$?
    fi
    thin
    if [ "$DOCTOR_RC" != "0" ]; then
        echo "⚠ doctor.sh 退了 $DOCTOR_RC（不是 0），而 bundle 自己的探针说 tier1=$TIER1。"
        echo "  两者不一致时**以 doctor.sh 为准**：先按它的提示修，再回来跑这条命令。"
    fi
else
    echo "（这个安装里没有 deploy/doctor.sh —— 用 bundle 内置的精简版）"
fi

echo ""
echo "  python      : $PY  ($PY_VERSION)  ->  $(getval PY_EXE)"
[ "$(getval PY_PREFERRED)" = "YES" ] || \
    echo "                ⚠ 不是 3.11.x。目标机基线是 3.11.4；别的版本能跑，但没在那上面验过。"
[ "$(getval PY_VENV)" = "YES" ] && \
    echo "                ⚠ 这是个 virtualenv，不是系统解释器。"
echo "  ewave_batch : $(getval TOOL_VERSION)   <- $(getval PKG_FILE)"
# doctor.sh 已经打过一张 tier 表了，别在同一屏上再放第二张 —— 两张表说同一件事，
# 只会让人怀疑「哪张对」。没有 doctor.sh 的旧包里才由 bundle 自己补这张。
if [ "$DOCTOR_RC" = "-" ]; then
    echo ""
    echo "  tier 1  解析 run 目录 / 拼命令 / dry-run / 单测   $(yesno "$TIER1")  （只要 python，纯 stdlib）"
    echo "  tier 2  真提交跑批次                             $(yesno "$TIER2")  （还要 ewave/strmout/dsub 在 PATH）"
    echo "  tier 3  GUI                                      $(yesno "$TIER3")  （还要 tkinter + \$DISPLAY）"
    printf '            %-8s %s\n' "ewave"   "$(getval BIN_ewave)"
    printf '            %-8s %s\n' "strmout" "$(getval BIN_strmout)"
    printf '            %-8s %s\n' "dsub"    "$(getval BIN_dsub)"
    printf '            %-8s %s\n' "tkinter" "$(getval MOD_tkinter)   DISPLAY=$(getval ENV_DISPLAY)"
fi
if [ "$TIER2" != "YES" ]; then
    echo ""
    echo "  ⓘ tier 2 不可用**不影响这次验证** —— dry-run 只读、不提交，本来就不需要这三个工具。"
    echo "    argv 里的程序名会用通用名占位（dry-run 的 [1/5] 会明写这一格是占位的）。"
    echo "    想让它们出现：先 ma 出 eWave / Donau 对应的模块，再跑一次。"
fi
if [ "$TIER3" != "YES" ]; then
    echo "  ⓘ tier 3 不可用是**降级不是失败** —— 纯 ssh 会话里本来就只有 CLI。"
fi
echo ""
echo "OK  [1/3] 环境够用（tier 1 = 本次验证需要的全部）"

# ---------------------------------------------------------------------------
# [2/3] 全部单测
# ---------------------------------------------------------------------------
step "[2/3] 全部单测 —— 没网的机器上，一套全绿的测试是最强的证据"
echo "  $PY -m unittest discover -s tests -t ."
echo "  （它一次证明三件事：包完整落地、这个解释器跑得动、逻辑还是对的）"
echo ""

TESTS_RAN=""
TESTS_VERDICT=""
if [ -n "$LOG_TESTS" ]; then
    # 测试自己会 mkdtemp。把 TMPDIR 也钉进安装目录，跑完这台机器的 /tmp 一个字节都不多。
    mkdir -p "$WORK/tmp" 2>/dev/null
    # ⚠ 不要在这里加 `PYTHONIOENCODING=utf-8`。2026-08-19 试过，当场红一条：
    #   tests/test_cli.py::LazyImport 起子进程跑 CLI，子进程继承这个变量后按 UTF-8 输出，
    #   而父进程的 subprocess(text=True) 仍按 **locale** 解码 ⇒ UnicodeDecodeError。
    #   在 LANG=C 的红区同样会炸（那边 locale 是 ASCII）。
    # 这套东西对付 locale 的正解是 `ascii_safe_stdio()`（errors="replace"，宁可字糊不让进程死），
    # 验证包该跑的就是**这条标准路径**，不该跑一个只有它自己才用的特殊配置。
    ( TMPDIR="$WORK/tmp" TEMP="$WORK/tmp" TMP="$WORK/tmp" \
      "$PY" -m unittest discover -s tests -t . ) > "$LOG_TESTS" 2>&1
    TESTS_RC=$?
    tail -20 "$LOG_TESTS"
    TESTS_RAN=$(sed -n 's/^\(Ran [0-9][0-9]* test.*\)$/\1/p' "$LOG_TESTS" | tail -1)
    TESTS_VERDICT=$(sed -n 's/^\(OK.*\)$/\1/p;s/^\(FAILED.*\)$/\1/p' "$LOG_TESTS" | tail -1)
else
    "$PY" -m unittest discover -s tests -t . 2>&1
    TESTS_RC=$?
fi
echo ""
if [ "$TESTS_RC" = "0" ]; then
    echo "OK  [2/3] 单测全绿   ${TESTS_RAN:-} ${TESTS_VERDICT:-}"
    echo "    （skip 是允许的：少数 fixture 含站点坐标 ⇒ 永不进包，缺了就优雅跳过）"
else
    echo "FAIL  [2/3] 单测有红（退出码 $TESTS_RC）   ${TESTS_RAN:-} ${TESTS_VERDICT:-}"
    echo ""
    echo "    这一步红了，**第 3 步的结论一律不可信** —— 但第 3 步照样是只读的，"
    echo "    所以下面仍然会跑一遍，好让你一次拿全信息。"
    echo ""
    echo "    下一步（按可能性排序）："
    echo "      1) 包没落全 / 落了一半：重新 bash deploy.sh，再跑一次这条命令；"
    echo "      2) 解释器不对：bash scripts/redzone_bundle.sh <OFFDIR> --python /path/to/python3；"
    echo "      3) 都不是 ⇒ 这是工具自己的 bug。把 unittest 的输出整段贴回给开发"
    echo "         （单测输出里没有站点坐标，但仍按公司内部资料对待）。"
fi

# ---------------------------------------------------------------------------
# [3/3] 只读 dry-run
# ---------------------------------------------------------------------------
step "[3/3] 只读 dry-run —— 逐 flag / 逐端口对着官方那条真实命令 diff"
set -- --offdir "$OFFDIR_ABS"
[ -n "$SPEC" ]  && set -- "$@" --spec "$SPEC"
[ -n "$LIMIT" ] && set -- "$@" --limit "$LIMIT"
[ "$SHOW_GDSOUT" = "1" ] && set -- "$@" --show-gdsout
echo "  $PY -m ewave_batch.redzone_dryrun $*"
echo "  （怎么读这段输出：docs/REDZONE_DRYRUN.md 第 3 节）"
echo ""

if [ -n "$LOG_DRY" ]; then
    # 这里**故意不设 PYTHONIOENCODING** —— 跑的就是用户手敲那条命令的标准路径。
    # 万一日志回来是一片 `?`（LANG=C 下 `ascii_safe_stdio()` 的降级），
    # 单独重跑一次拿可读版本：setenv PYTHONIOENCODING utf-8 再敲同一条 dry-run。
    # （只对 dry-run 有效。**别用它跑单测**，理由见上面 [2/3] 那段注释。）
    "$PY" -m ewave_batch.redzone_dryrun "$@" > "$LOG_DRY" 2>&1
    DRY_RC=$?
    cat "$LOG_DRY"
else
    "$PY" -m ewave_batch.redzone_dryrun "$@" 2>&1
    DRY_RC=$?
fi

# 跑完再给 OFFDIR 拍一张
WITNESS_LINE="(--no-witness / --no-log：跳过)"
if [ "$WITNESS" = "1" ]; then
    LC_ALL=C ls -lR -- "$OFFDIR_ABS" > "$WITNESS_AFTER" 2>/dev/null
    if cmp -s "$WITNESS_BEFORE" "$WITNESS_AFTER"; then
        WITNESS_LINE="未被改动（跑前跑后 ls -lR 逐字节相同）"
    else
        WITNESS_LINE="⚠ 有变化 —— 见上面的 diff"
        echo ""
        echo "⚠ OFFDIR 在这段时间里变了。本命令一个写调用都没有，所以最可能是别的进程"
        echo "  （官方 GUI / 别人的 job）正在往里写。差异（最多 20 行）："
        diff "$WITNESS_BEFORE" "$WITNESS_AFTER" 2>/dev/null | head -20
    fi
fi

# ---------------------------------------------------------------------------
# 汇总 —— 一屏
# ---------------------------------------------------------------------------
case "$DRY_RC" in
    0) DRY_TXT="OK     逐 flag / 逐端口与官方那条真实命令一致" ;;
    2) DRY_TXT="DIFF   比对完成，**有差异**" ;;
    3) DRY_TXT="NOBASE 没能比对：OFFDIR 里没有官方命令行" ;;
    *) DRY_TXT="ERROR  跑不起来（退出码 $DRY_RC）" ;;
esac
if [ "$TESTS_RC" = "0" ]; then
    TESTS_TXT="OK     ${TESTS_RAN:-全绿} ${TESTS_VERDICT:-}"
else
    TESTS_TXT="FAIL   单测有红（退出码 $TESTS_RC）"
fi

echo ""
rule
echo "  汇总 —— redzone_bundle"
thin
printf '  1) 环境      %s\n' "OK     python $PY_VERSION   tier1=$TIER1 tier2=$TIER2 tier3=$TIER3"
printf '  2) 单测      %s\n' "$TESTS_TXT"
printf '  3) dry-run   %s\n' "$DRY_TXT"
printf '  OFFDIR       %s\n' "$WITNESS_LINE"
thin

# 退出码：先修地基，再看上层。5 > 4 > 6 > 3 > 2 > 0
if [ "$TESTS_RC" != "0" ]; then
    RC=4
    echo "  结论：**单测有红** ⇒ 这次 dry-run 的结论不可信（代码本身就没落对）。"
    echo "  下一步：重新 bash deploy.sh；还红就换解释器（--python）；再红就是工具的 bug，"
    echo "          把 unittest 输出贴回来。"
elif [ "$DRY_RC" = "0" ]; then
    RC=0
    echo "  结论：**全绿**。在这台机器上，本工具生成的命令与官方 GUI 那条逐 flag、逐端口一致。"
    echo ""
    echo "  绿了说明什么（三条，都是这一趟才验得了的）："
    echo "    · 站点坐标是**现场解析**对了的 —— 源码里一个都没有，全从你给的那个目录读出来；"
    echo "    · --all 能逐位复现官方 -p 的端口顺序（D1b 在这个站点成立）"
    echo "      ⇒ 「不依赖 GUI」这件事在这里是成立的；"
    echo "    · gdsout 模板里那 8 个会改变 GDS 内容的字段没被动过（D1c）。"
    echo ""
    echo "  下一步："
    echo "    1) 生成一份 spec 样例，按你要扫的设定改："
    echo "         $PY -c \"import sys; from ewave_batch.core.spec import EXAMPLE_SPEC; sys.stdout.buffer.write(EXAMPLE_SPEC.encode('utf-8'))\" > my_spec.yaml"
    echo "    2) 带上它再跑一次这条命令（仍然只读）："
    echo "         bash scripts/redzone_bundle.sh <OFFDIR> --spec my_spec.yaml"
    echo "       ——**重点看每个 run 的落地目录互不相同**，那正是这个工具存在的理由；"
    echo "    3) 都确认了才真跑：  $PY cli.py run my_spec.yaml"
    echo "       （真跑不走这条命令 —— redzone_bundle 永远不提交任何 job）"
elif [ "$DRY_RC" = "2" ]; then
    RC=2
    echo "  结论：**有差异**。注意环境和单测都是绿的 ⇒ 差异来自「这个站点和我们以为的不一样」，"
    echo "        不是「装坏了」。这正是这一趟要找的东西 —— 本机的测试盖不到它。"
    echo ""
    echo "  下一步：**先别真跑。** 上面 [4/5] 和 [5/5] 已经逐条写了每处差异属于哪一类、"
    echo "          该改 spec 还是该改代码。把那两段整段贴回给开发。"
    echo "          （里面有站点坐标 ⇒ 只在公司内部流转，别发第三方服务）"
    echo "          差异本身没有任何后果：这一趟没写任何文件，也没提交任何 job。"
elif [ "$DRY_RC" = "3" ]; then
    RC=3
    echo "  结论：**没能比对**。命令和落地目录都打印了，但这个 OFFDIR 里没有官方命令行，"
    echo "        所以「我们拼的和官方一样吗」这个问题这次没回答。"
    echo ""
    echo "  下一步：换一个**求解真的跑完过**的 design 目录（只 stream out 过的目录里"
    echo "          没有 run_ewave_*.sh）。让机器列候选："
    echo "            $PY -c \"$SUGGEST_SNIPPET; print(chr(10).join(s('<workarea>')))\""
else
    RC=6
    echo "  结论：**dry-run 跑不起来**（退出码 $DRY_RC）。"
    echo ""
    echo "  下一步：它自己的错误消息带「下一步怎么办」，就在上面输出的最后几行。"
    echo "          最常见的是 OFFDIR 指错一层（上一级 ewave_simulation/，"
    echo "          或下一级 <corner>_<temp>/）。"
fi

thin
if [ -n "$WORK" ]; then
    echo "  完整输出：$WORK"
    if [ -f "$LOG_DOCTOR" ]; then
        echo "    doctor.log / unittest.log / dryrun.log"
    else
        echo "    unittest.log / dryrun.log"
    fi
    echo "    要贴回来的是 dryrun.log —— **它含站点坐标，只在公司内部流转**。"
else
    echo "  （--no-log：什么都没写。要留档就在 csh 里加 >& bundle.log）"
fi
echo "  退出码：$RC   （csh/tcsh 里看 echo \$status）"
rule
exit $RC
