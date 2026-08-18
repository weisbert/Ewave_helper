# cfg.sh -- 通用配置。**本文件不含任何站点标识符**（无工号/主机名/项目代号/cell 名/端口名），
#           可以公开。全部站点坐标要么来自 site.local.sh，要么在运行时从官方 run 目录解析出来。
#
# 设计取向：坐标不手抄，现场解析。官方 GUI 的 run 目录里本来就躺着全部坐标 ——
#   gdsout_setup        → library / topCell / view / layerMap
#   run_ewave_*.sh      → ptxt / key / corner / temperature / 端口表 / 生产 flag
#   remote_run_ewave.sh → dsub 的 -A / -q / -R
# 所以 site.local.sh 只需要给一个值：OFFDIR。抄错的机会都没有。

# ---- 站点配置（唯一需要用户提供的东西）------------------------------------
if [ -f ./site.local.sh ]; then
  . ./site.local.sh
fi
# OFFDIR 没填或填错时，就近把候选找出来（含 gdsout_setup 的目录就是 design 目录），
# 省得手打长路径 —— 打错一个字符后面全废，不如让机器找。
suggest_offdir() {
  for _r in "$PWD/.." "$PWD" "$PWD/../.." "$HOME"; do
    [ -d "$_r" ] && find "$_r" -maxdepth 3 -name gdsout_setup -type f 2>/dev/null
  done | sed 's|/gdsout_setup$||' \
  | while read -r _d; do (cd "$_d" 2>/dev/null && pwd); done \
  | sort -u
}
offdir_help() {
  echo "  改 site.local.sh 里的 OFFDIR，指向官方 GUI 跑过的那个 design 目录" >&2
  echo "  （即 <workarea>/ewave_simulation/<library>_<topCell>_<view>/，里面有 gdsout_setup）" >&2
  echo >&2
  echo "  -- 在附近找到的候选（直接抄一行）--" >&2
  _c=`suggest_offdir`
  if [ -n "$_c" ]; then echo "$_c" | sed 's/^/  OFFDIR=/' >&2
  else echo "  (没找到；请手动指定)" >&2; fi
}
if [ -z "$OFFDIR" ]; then
  echo "错误：没设 OFFDIR。先 cp site.example.sh site.local.sh" >&2
  offdir_help
  exit 2
fi
case "$OFFDIR" in
  /path/to/*)
    echo "错误：OFFDIR 还是 site.example.sh 里的占位符，忘了改。" >&2
    offdir_help
    exit 2 ;;
esac
if [ ! -d "$OFFDIR" ]; then
  echo "错误：OFFDIR 不是目录: $OFFDIR" >&2
  offdir_help
  exit 2
fi
if [ ! -f "$OFFDIR/gdsout_setup" ]; then
  echo "错误：$OFFDIR 里没有 gdsout_setup —— 这不像是官方 GUI 的 design 目录" >&2
  offdir_help
  exit 2
fi

# MVP 落地根。**必须在 <workarea>/ewave_simulation/ 外面**（那是设计师的 spine，只读）。
# 默认放 $HOME；但 mesh/pmsh 中间件可能有几 GB，$HOME 常有配额 →
# 容量吃紧就在 site.local.sh 里把 MVP 指到 <workarea>/ewave_mvp。
MVP="${MVP:-$HOME/ewave_mvp}"
case "$MVP" in
  */ewave_simulation|*/ewave_simulation/*)
    echo "错误：MVP 落在 ewave_simulation/ 里面了 —— 那是设计师的 spine，本工具只读它。" >&2
    echo "  在 site.local.sh 里把 MVP 改到别处，例如 <workarea>/ewave_mvp" >&2
    exit 2 ;;
esac
# workarea = OFFDIR 往上两级。MVP 不在它下面就明确警告 ——
# 2026-08-18 踩过：用户要求全部落 workarea，MVP 却默默用了 $HOME 默认值，跑了四步才发现。
_WA=`dirname "$(dirname "$OFFDIR")"`
case "$MVP" in
  "$_WA"/*) : ;;
  *) echo "⚠️ 注意：MVP=$MVP 不在 workarea（$_WA）下面。" >&2
     echo "   \$HOME 通常有配额，三个 run 的 mesh 中间件轻松几百 MB。" >&2
     echo "   要改就在 site.local.sh 里设 MVP=$_WA/ewave_mvp" >&2 ;;
esac

# ---- 从官方 run 目录解析坐标 ----------------------------------------------
OFF_SETUP="$OFFDIR/gdsout_setup"
OFF_RUNSH=`ls "$OFFDIR"/run_ewave_*.sh 2>/dev/null | head -1`
OFF_REMOTE="$OFFDIR/remote_run_ewave.sh"

# gdsout_setup 是 "key<tab>value" 格式，value 可能带引号
gs_field() {
  awk -v k="$1" '$1==k { s=$0
      sub(/^[ \t]*[A-Za-z]+[ \t]*/,"",s); sub(/[ \t]+$/,"",s)
      gsub(/^"|"$/,"",s); print s; exit }' "$OFF_SETUP" 2>/dev/null
}
LIBRARY=`gs_field library`
CELL=`gs_field topCell`
VIEW=`gs_field view`
LAYERMAP=`gs_field layerMap`

# run_ewave_*.sh 里的单值 flag
rs_flag() { grep -o -E -- "$1=('[^']*'|\"[^\"]*\"|[^ ]+)" "$OFF_RUNSH" 2>/dev/null \
            | head -1 | sed "s|^$1=||; s|^['\"]||; s|['\"]$||"; }
PTXT=`rs_flag --emssTechFile`
KEY=`rs_flag --key`
CORNER=`rs_flag --corner`
TEMP=`rs_flag --temperature`

# ★ 官方那一串 -p/-i，原样取回 —— 端口名不进我们的源码，只在红区现场存在
PORTS_OFFICIAL=`grep -o -E -- "-p +'[^']*'|-p +[^ ]+|-i +[A-Za-z0-9_]+" "$OFF_RUNSH" 2>/dev/null | tr '\n' ' '`
# 只要 pin 名（按官方 -p 出现的顺序），给 step2 做排序核对
PINS_OFFICIAL=`grep -o -E -- "-p +'[^']*'" "$OFF_RUNSH" 2>/dev/null | sed "s/.*=//; s/'$//"`

# eWave 自己建的那层子目录：<corner>_<temp 的小数点换下划线>
TEMPDIR="${CORNER}_$(echo "$TEMP" | tr '.' '_')"

# remote_run_ewave.sh 里的 dsub 三元组
DSUB_A=`grep -o -E -- '-A +[^ ]+' "$OFF_REMOTE" 2>/dev/null | head -1 | sed 's/-A *//'`
DSUB_Q=`grep -o -E -- '-q +[^ ]+' "$OFF_REMOTE" 2>/dev/null | head -1 | sed 's/-q *//'`
DSUB_R=`grep -o -E -- '-R +"[^"]*"' "$OFF_REMOTE" 2>/dev/null | head -1 | sed 's/-R *"//; s/"$//'`
: "${DSUB_R:=cpu=10;mem=100000}"

# PDK 根 / process 目录，从 ptxt 路径倒推（不写死）
PDKROOT=`echo "$PTXT" | sed 's|/apps/ewave/.*||'`
PTXT_DIR=`dirname "$PTXT" 2>/dev/null`
PTXT_VERDIR=`dirname "$PTXT_DIR" 2>/dev/null`
PTXT_PROCDIR=`dirname "$PTXT_VERDIR" 2>/dev/null`

OFF_GDS="$OFFDIR/$CELL.gds"
OFF_SNP=`ls "$OFFDIR/$TEMPDIR"/*.s[0-9]*p 2>/dev/null | grep -v _sample | head -1`
OFF_SAMPLE_SNP=`ls "$OFFDIR/$TEMPDIR"/*_sample*.s[0-9]*p 2>/dev/null | head -1`

# ---- 我们自己的生产默认 flag（★ 通用值，不含站点信息，可以公开）-------------
# 来源：PROJECT_BRIEF §6「已知的生产默认值」。run B/C 用这一串**自己拼**命令，
# 而不是抄官方脚本 —— 那正是 MVP 要证明的能力。step0 会把它与官方实际用的对一遍。
EWAVE_COMMON="--nogui -m --cadencePins=1 --labelDepth=0 --viaMergeSpace=0.4 -e 0.4 -d 0.4 --equalCurrent --viaMode=1 --relativeTolerance=1e-05 --relativeCurrentTolerance=0.001 --sparamImpedance=50 --parallel=20"

# 把一条命令行归一成"每行一个 flag、已排序"，剔掉站点相关和端口相关的，好做集合比对
norm_flags() {
  sed -e 's/|.*//' \
      -e 's/ -e /  -e/g' -e 's/ -d /  -d/g' \
  | tr ' ' '\n' | grep -E '^-' \
  | grep -vE '^(--emssTechFile=|--gds=|--top=|--workDir=|--sparam=|--corner=|--temperature=|--key=|--multiSweep=|--discreteFreq=|--includePortOrder=|--memEstimate=|--all$|-p$|-i$)' \
  | sort -u
}

# ---- 本次验证的设定 --------------------------------------------------------
FREQ="${FREQ:-5}"            # 单频点，GHz。见 README「为什么单频点是干净的」

# ---- strmout 的 cwd 策略（BRIEF P7a-1 就是要验这个）------------------------
#   include   = 在 $MVP/cdswork 下建一个 `INCLUDE <workarea>/cds.lib` 的 cds.lib，
#               cd 到那里跑 —— 所有写入都留在 MVP 内，不碰 spine（默认，推荐）
#   workarea  = 直接 cd 到 $CDSROOT 跑。仅当 include 失败时改
CDSWORK_MODE="${CDSWORK_MODE:-include}"
# cds.lib 所在的根（默认从 OFFDIR 往上两级：<workarea>/ewave_simulation/<design>/）
CDSROOT="${CDSROOT:-$(dirname "$(dirname "$OFFDIR")")}"

# ---- 工具路径（PATH 上有就用 PATH 上的，可用 site.local.sh 覆盖）-----------
ewave_bin()   { if command -v ewave   >/dev/null 2>&1; then echo ewave;   else echo "${EWAVE_ABS:-ewave}";     fi; }
strmout_bin() { if command -v strmout >/dev/null 2>&1; then echo strmout; else echo "${STRMOUT_ABS:-strmout}"; fi; }

# ---- 派生路径（不用改）----------------------------------------------------
W="$MVP/work"
OURGDS_DIR="$W/gds_ours"
OUR_GDS="$OURGDS_DIR/$CELL.gds"
RUN_A="$W/runs/A_officialports"
RUN_B="$W/runs/B_all"
RUN_C="$W/runs/C_ourgds"
LOGS="$MVP/logs"

hr() { echo "------------------------------------------------------------"; }
sec() { echo; echo "=== $* ==="; }
