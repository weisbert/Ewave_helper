#!/usr/bin/env bash
#
# doctor.sh -- red-zone environment check for Ewave_helper.
#
# The red zone has no network: nothing can be pip-installed and no venv is
# created. So the only question that matters after a deploy is "what can THIS
# box already run?". This script answers it per interpreter, in three tiers:
#
#   tier 1  parse an official run dir, build commands, dry-run, unit tests
#                                                    needs Python only (stdlib)
#   tier 2  really submit a batch                    + dsub djob ewave strmout
#   tier 3  GUI                                      + tkinter and $DISPLAY
#
# Tiers are cumulative. A missing tier-3 dependency is a DEGRADE, not a
# failure: tiers 1-2 still run, and a plain ssh session is SUPPOSED to have no
# $DISPLAY. Tier 2 usually needs your site's EDA modules loaded first -- an
# interpreter stuck at tier 1 is normally a bare login shell, not a bad install.
#
# Usage (login shell is often tcsh -- always invoke with bash):
#   bash deploy/doctor.sh                 probe every candidate interpreter
#   bash deploy/doctor.sh --test          ... and run the shipped test suite
#   bash deploy/doctor.sh --python /path/to/python3
#   EWB_PYTHON=/path/to/python3 bash deploy/doctor.sh
#
# Exit code: 0 if at least one interpreter reaches tier 1, else 1.
# Tier 1 is the bar because tier 1 already does the work this tool exists for
# (plan a batch, print every command, run the whole test suite); the cluster
# tools come and go with a module load and must not make a good install look
# broken.
#
# Everything this script writes stays under <install>/.deploy/ -- never /tmp.
#
# English on purpose: this file runs on a box where LANG is often C.
#
set -uo pipefail

SELF="$(readlink -f "$0")"
SCRIPT_DIR="$(dirname "$SELF")"
ROOT="$(dirname "$SCRIPT_DIR")"
PROBE="$SCRIPT_DIR/_env_check.py"

RUN_TESTS=0
FORCED_PY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --test|-t)    RUN_TESTS=1; shift ;;
    --python|-p)  FORCED_PY="${2:-}"; shift 2 ;;
    -h|--help)    sed -n '2,32p' "$SELF" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)            echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done

[[ -f "$PROBE" ]] || { echo "ERROR: $PROBE missing -- incomplete install." >&2; exit 1; }

# Scratch stays inside the install dir -- never /tmp, /var or anywhere else on
# the box. Same rule the deployer follows: everything under ./.deploy/.
TMP="$ROOT/.deploy/tmp"
rm -rf "$TMP"
mkdir -p "$TMP" || { echo "ERROR: cannot create $TMP -- is $ROOT writable?" >&2; exit 1; }
trap 'rm -rf "$TMP"' EXIT

getval() { sed -n "s/^$1=//p" "$2" | head -1; }

echo "=== Ewave_helper -- red-zone environment doctor ==="
echo "install : $ROOT"
if [[ -f "$ROOT/VERSION" ]]; then
  echo "version :"
  sed -n '1,5p' "$ROOT/VERSION" | sed 's/^/     /'
fi
echo

# --- collect candidate interpreters -----------------------------------------
CANDIDATES=()
add_candidate() {
  local _c="$1" _r _e
  [[ -n "$_c" ]] || return 0
  _r="$(command -v "$_c" 2>/dev/null || true)"
  [[ -n "$_r" ]] || return 0
  _r="$(readlink -f "$_r" 2>/dev/null || echo "$_r")"
  for _e in ${CANDIDATES[@]+"${CANDIDATES[@]}"}; do
    if [[ "$_e" == "$_r" ]]; then return 0; fi
  done
  CANDIDATES+=("$_r")
}

if [[ -n "$FORCED_PY" ]]; then
  add_candidate "$FORCED_PY"
  if (( ${#CANDIDATES[@]} == 0 )); then
    echo "ERROR: --python '$FORCED_PY' is not executable." >&2; exit 1
  fi
else
  add_candidate "${EWB_PYTHON:-}"
  for c in python3 python3.13 python3.12 python3.11 python3.10 python \
           /usr/bin/python3 /usr/local/bin/python3; do
    add_candidate "$c"
  done
fi

if (( ${#CANDIDATES[@]} == 0 )); then
  echo "ERROR: no Python interpreter found on PATH." >&2
  echo "       Load your site's python module, or point at one explicitly:" >&2
  echo "       bash deploy/doctor.sh --python /path/to/python3" >&2
  exit 1
fi

# --- probe each --------------------------------------------------------------
BEST=""; BEST_TIER=-1; BEST_VENV="NO"
mark() { if [[ "$1" == "OK" || "$1" == "YES" ]]; then echo "OK "; else echo "-- "; fi; }

idx=0
for py in "${CANDIDATES[@]}"; do
  idx=$((idx + 1))
  out="$TMP/probe.$idx"
  if ! "$py" "$PROBE" > "$out" 2> "$TMP/err.$idx"; then
    if [[ "$(getval PY_OK "$out")" == "NO" ]]; then
      printf '>> %-38s %s  SKIP (%s)\n' "$py" "$(getval PY_VERSION "$out")" "$(getval PY_WHY "$out")"
    else
      printf '>> %-38s SKIP (probe failed)\n' "$py"
      sed 's/^/     /' "$TMP/err.$idx" | tail -3
    fi
    echo
    continue
  fi

  pyver="$(getval PY_VERSION "$out")"
  echo ">> $py  ($pyver)"

  printf '     %s import ewave_batch.core.cmd\n'      "$(mark "$(getval IMP_core_cmd "$out")")"
  printf '     %s import ewave_batch.cli\n'           "$(mark "$(getval IMP_cli "$out")")"
  printf '     %s import ewave_batch.redzone_dryrun\n' "$(mark "$(getval IMP_redzone_dryrun "$out")")"
  # gui.app must import with tkinter absent -- that is the lazy-import rule.
  printf '     %s import gui.app (lazy, no tkinter needed)\n' "$(mark "$(getval IMP_gui_app "$out")")"
  for k in IMP_core_cmd IMP_cli IMP_redzone_dryrun IMP_gui_app; do
    if [[ "$(getval "$k" "$out")" == "FAIL" ]]; then
      printf '        why: %s\n' "$(getval "${k}_detail" "$out")"
    fi
  done

  for t in dsub djob ewave strmout; do
    printf '     %s %-8s %s\n' "$(mark "$(getval "TOOL_$t" "$out")")" "$t" "$(getval "TOOL_${t}_detail" "$out")"
  done

  tkline="$(getval MOD_tkinter "$out")"
  disp="$(getval TK_DISPLAY "$out")"
  envdisp="$(getval ENV_DISPLAY "$out")"
  if [[ "$tkline" == "OK" && "$disp" == "NO" ]]; then
    if [[ -n "$envdisp" ]]; then
      printf '     %s tkinter  present, but cannot open a window (DISPLAY=%s)\n' "$(mark OK)" "$envdisp"
    else
      printf '     %s tkinter  present, but $DISPLAY is unset (headless -- no GUI)\n' "$(mark OK)"
    fi
  elif [[ "$disp" == "YES" ]]; then
    if [[ -n "$envdisp" ]]; then
      printf '     %s tkinter  window OK (DISPLAY=%s)\n' "$(mark OK)" "$envdisp"
    else
      printf '     %s tkinter  window OK\n' "$(mark OK)"
    fi
  else
    printf '     %s tkinter  %s\n' "$(mark "$tkline")" "$(getval MOD_tkinter_detail "$out")"
  fi

  # PyYAML is optional (spec files fall back to JSON) -- report, never gate.
  if [[ "$(getval MOD_yaml "$out")" == "OK" ]]; then
    printf '     %s PyYAML   %s (spec files can be YAML)\n' "$(mark OK)" "$(getval MOD_yaml_detail "$out")"
  else
    printf '     %s PyYAML   absent -- spec files must be JSON (supported, not a defect)\n' "$(mark MISSING)"
  fi

  if [[ "$(getval PY_VENV "$out")" == "YES" ]]; then
    printf '        NOTE: this is a virtualenv, not a system interpreter.\n'
  fi

  cc="$(getval CAP_core "$out")"; cs="$(getval CAP_submit "$out")"; cg="$(getval CAP_gui "$out")"
  guicode="$(getval GUI_CODE "$out")"
  echo "     ------------------------------------------------"
  printf '     tier 1  plan / dry-run / tests           %s\n' "$([[ "$cc" == YES ]] && echo AVAILABLE || echo "NOT AVAILABLE")"
  printf '     tier 2  submit a real batch              %s\n' "$([[ "$cs" == YES ]] && echo AVAILABLE || echo "NOT AVAILABLE")"
  if [[ "$cg" == YES ]]; then
    printf '     tier 3  GUI                              AVAILABLE\n'
  elif [[ "$guicode" == OK && "$disp" == YES ]]; then
    # tkinter + X11 are both fine; the only thing below it is missing.
    printf '     tier 3  GUI                              code + X11 OK, held back by tier 2\n'
  elif [[ "$guicode" == OK ]]; then
    printf '     tier 3  GUI                              code OK, needs X11 ($DISPLAY)\n'
  else
    printf '     tier 3  GUI                              NOT AVAILABLE\n'
  fi
  tier="$(getval TIER "$out")"
  why="$(getval TIER_WHY "$out")"
  if [[ -n "$why" ]]; then printf '     (stops at tier %s: %s)\n' "$tier" "$why"; fi
  echo

  [[ "$tier" =~ ^[0-9]+$ ]] || tier=0
  if (( tier > BEST_TIER )); then
    BEST_TIER=$tier; BEST="$py"; BEST_VENV="$(getval PY_VENV "$out")"
  fi
done

# --- verdict -----------------------------------------------------------------
if [[ -z "$BEST" || $BEST_TIER -lt 1 ]]; then
  echo "VERDICT: no interpreter on this box can run Ewave_helper."
  echo
  echo "  Tier 1 needs nothing but a Python >= 3.10 that can import the package"
  echo "  (the core is pure stdlib -- there is nothing to install). If every"
  echo "  candidate failed to import it, the package did not land intact:"
  echo "  re-deploy, then re-run this. To try a specific interpreter:"
  echo "     bash deploy/doctor.sh --python /path/to/python3"
  exit 1
fi

echo "RECOMMENDED: $BEST   (tier $BEST_TIER)"
if [[ "$BEST_VENV" == "YES" ]]; then
  echo "  (heads-up: that is a virtualenv. Fine if it works, but the red-zone"
  echo "   baseline is a system interpreter -- a venv may not exist for others.)"
fi
if (( BEST_TIER < 2 )); then
  echo
  echo "  Tier 1 only: dsub / djob / ewave / strmout are not on PATH, so this"
  echo "  session can plan and dry-run but cannot submit. Load your site's EDA"
  echo "  and cluster modules in this shell and re-run to reach tier 2."
fi
echo
echo "  cd $ROOT"
echo "  interface self-test : $BEST -m ewave_batch dry-run --self-test"
echo "  plan a batch        : $BEST cli.py dry-run --help"
if (( BEST_TIER >= 2 )); then
  echo "  run a batch         : $BEST cli.py run --help"
fi
if (( BEST_TIER >= 3 )); then
  echo "  GUI                 : $BEST cli.py --gui"
fi
echo "  unit tests          : $BEST -m unittest discover -s tests -t ."
echo

# --- optional self-test ------------------------------------------------------
if (( RUN_TESTS )); then
  echo "=== self-test with $BEST ==="
  # The tests use tempfile.mkdtemp(); TMPDIR keeps even those inside .deploy/tmp
  # so a doctor run leaves nothing at all in /tmp on the box.
  #
  # Two steps, cheapest first: the interface self-test is one second and says
  # "the frozen surface and the code still agree"; the suite is the real proof.
  if ! ( cd "$ROOT" && TMPDIR="$TMP" TEMP="$TMP" TMP="$TMP" "$BEST" -m ewave_batch dry-run --self-test ); then
    echo
    echo "FAIL  interface self-test failed -- the package is inconsistent." >&2
    exit 1
  fi
  echo
  if ( cd "$ROOT" && TMPDIR="$TMP" TEMP="$TMP" TMP="$TMP" "$BEST" -m unittest discover -s tests -t . ); then
    echo
    echo "OK  self-test passed -- the package landed intact and this interpreter runs it."
  else
    echo
    echo "FAIL  self-test failed. The package or the environment is not sound;"
    echo "      do not trust results from this install until it passes." >&2
    exit 1
  fi
fi
