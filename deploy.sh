#!/usr/bin/env bash
#
# deploy.sh -- red-zone (Linux) deployer for Ewave_helper.
#
# Verifies a packed tarball (from deploy/pack.ps1), backs up the current
# install, and swaps in the new one *in place*. Everything stays under the
# install dir -- the PARENT directory is NEVER touched. No git, no network, no
# pip, no venv, no Python required: only bash + tar + sha256sum (base RHEL).
#
# Managed layout (all inside the install dir, e.g. .../ewave_helper/):
#   <install>/deploy.sh                     this script -- the update entry point
#   <install>/.deploy/incoming/             uploaded tarball + .sha256
#   <install>/.deploy/staging/              full extract happens here first
#   <install>/.deploy/backups/<timestamp>/  previous install (keeps last N)
#   <install>/.deploy/tmp/                  scratch (doctor.sh)
#   <install>/.deploy/preserve.list         optional: top-level names to KEEP
#                                           across deploys (your batch results)
#
# Usage -- upload the tarball INTO the install dir, then just run this:
#   cd .../ewave_helper
#   bash deploy.sh                      # auto-detects the .tar.gz sitting here
#   bash deploy/doctor.sh --test        # then check the box can actually run it
#
# With no argument it picks up the newest *.tar.gz in the install dir (and says
# which one, plus which it ignored). Pass a path explicitly to override.
#
# Uploaded *.tar.gz / *.sha256 at the top level are treated as DELIVERIES, not as
# install content: they are never backed up, so backups stay the size of the
# install and the file you uploaded survives the swap.
#
# They ARE rotated, though -- see KEEP_PACKAGES. Rotation happens only AFTER a
# successful swap, so a failed or aborted deploy never destroys a delivery you
# may still need.
#
# !! BATCH RESULTS: if you run batches with their root inside this directory,
# put that top-level name in .deploy/preserve.list -- otherwise the next deploy
# moves it into .deploy/backups/<ts>/ and the third deploy after that rotates it
# away. The swap warns about anything it moved that the package does not ship.
#
# Invoke via `bash` (not ./deploy.sh): the red zone's login shell is often
# tcsh/csh, and an upload channel may drop the exec bit. `bash` needs neither.
# Run it as a script -- never `source` it (it is bash, and it exits on success).
#
# Nothing is ever written outside the install dir: no /tmp, no /opt, no
# /var. Every scratch, staging and backup path lives under ./.deploy/.
#
# FIRST-TIME BOOTSTRAP (no deploy.sh on the box yet):
#   tar -xzf ewave_helper_<hash>.tar.gz   # yields ./ewave_helper/
#   # that directory IS the install; thereafter deploy.sh handles every update.
#
# ROLLBACK: each run backs up the previous install to .deploy/backups/<ts>/.
# To revert: delete the new contents (everything but .deploy) and
#   mv .deploy/backups/<ts>/* .  (or just re-deploy an older tarball).
#
# English on purpose: this file runs on a box where LANG is often C.
#
set -euo pipefail

KEEP_BACKUPS=3

# How many uploaded *.tar.gz deliveries to keep at the top level, newest first,
# counting the one just deployed. 2 = "what is installed, plus one step back".
#
# Not 3, to match KEEP_BACKUPS: these are the SECONDARY rollback route. The
# primary one is .deploy/backups/<ts>/, which holds three whole installs and
# needs no untarring. A delivery is only worth keeping so you can re-deploy an
# older build without another transfer across the air gap, and one step back
# covers that. Set to 0 to keep every delivery forever.
KEEP_PACKAGES=2

# Must exist at the install root -- guards against running in the wrong dir or
# extracting a bad archive. A nested path on purpose: a bare `cli.py` exists in
# half the repos on the box, `ewave_batch/model.py` does not.
SENTINEL="ewave_batch/model.py"

# --- locate ourselves: deploy.sh sits AT the install root -------------------
# i.e. .../ewave_helper/deploy.sh, so the install dir is simply our own dir.
# The install dir may be named anything; nothing here depends on its name.
SELF="$(readlink -f "$0")"
TARGET="$(dirname "$SELF")"
DEPLOY="$TARGET/.deploy"
INCOMING="$DEPLOY/incoming"
STAGING="$DEPLOY/staging"
BACKUPS="$DEPLOY/backups"

die() { echo "ERROR: $*" >&2; exit 1; }
print_version() { while IFS= read -r _l; do echo "     $_l"; done < "$1"; }

[[ -f "$TARGET/$SENTINEL" ]] || die \
  "$TARGET/$SENTINEL missing -- $TARGET is not an Ewave_helper install. Do the one-time bootstrap first (see header)."

# --- pick the tarball --------------------------------------------------------
# No argument: take the newest *.tar.gz sitting in the install dir. That is the
# normal path -- you upload the package here and run `bash deploy.sh`, nothing
# else. Only the top level is scanned, so copies under .deploy/ are ignored.
if [[ $# -ge 1 ]]; then
  TARBALL_SRC="$(readlink -f "$1")"
  [[ -f "$TARBALL_SRC" ]] || die "tarball not found: $1"
else
  # !! Do NOT write this as `ls -1t "$TARGET"/*.tar.gz`. With nullglob on, a glob
  # that matches nothing expands to ZERO arguments, so `ls -1t` lists the CURRENT
  # DIRECTORY instead -- and the first entry it prints (a source directory, say)
  # gets treated as "the newest package". The error you then get is about `cp`
  # refusing a directory, which tells you nothing about what actually went wrong.
  # Collect with nullglob first, then hand ls the paths explicitly, so ls is
  # never called with an empty operand list.
  # (Caught 2026-08-19 by tests/test_deploy.py; the sibling project still has it.)
  _found=()
  shopt -s nullglob
  _cands=()
  for _c in "$TARGET"/*.tar.gz; do _cands+=("$_c"); done
  shopt -u nullglob
  if (( ${#_cands[@]} > 0 )); then
    while IFS= read -r _c; do _found+=("$_c"); done < <(ls -1t "${_cands[@]}")
  fi
  if (( ${#_found[@]} == 0 )); then
    die "no *.tar.gz found in $TARGET -- upload the package here first (with its .sha256), then re-run. Or pass a path explicitly."
  fi
  TARBALL_SRC="$(readlink -f "${_found[0]}")"
  if (( ${#_found[@]} > 1 )); then
    echo ">> found ${#_found[@]} archives here; using the newest:"
    echo "     $(basename "$TARBALL_SRC")"
    for _o in "${_found[@]:1}"; do echo "     (ignoring $(basename "$_o"))"; done
  else
    echo ">> found package: $(basename "$TARBALL_SRC")"
  fi
fi

mkdir -p "$INCOMING" "$STAGING" "$BACKUPS"

# --- preserve list: top-level names that survive the swap --------------------
# Always .deploy; plus anything the operator listed (batch result roots, their
# own scratch dirs, ...). One name per line, '#' starts a comment.
PRESERVE=(".deploy")
if [[ -f "$DEPLOY/preserve.list" ]]; then
  while IFS= read -r _line || [[ -n "$_line" ]]; do
    _line="${_line%%#*}"
    _line="${_line#"${_line%%[![:space:]]*}"}"   # ltrim
    _line="${_line%"${_line##*[![:space:]]}"}"   # rtrim
    _line="${_line%/}"                            # tolerate a trailing slash
    if [[ -n "$_line" ]]; then PRESERVE+=("$_line"); fi
  done < "$DEPLOY/preserve.list"
fi
is_preserved() {
  local _n="$1" _p
  for _p in "${PRESERVE[@]}"; do
    if [[ "$_n" == "$_p" ]]; then return 0; fi
  done
  return 1
}

# Uploaded packages are DELIVERIES, not install content. They live at the top
# level because that is where you drop them, but they must never be backed up
# (which would silently eat the file you just uploaded and bloat every backup by
# a full package) nor deleted by a rollback.
is_payload() {
  case "$1" in
    *.tar.gz|*.tar.gz.sha256|*.tgz|*.tgz.sha256) return 0 ;;
    *) return 1 ;;
  esac
}

# Anything the swap must leave exactly where it is.
is_untouchable() {
  if is_preserved "$1"; then return 0; fi
  if is_payload   "$1"; then return 0; fi
  return 1
}

# --- stage the tarball + sidecar into .deploy/incoming -----------------------
TAR_NAME="$(basename "$TARBALL_SRC")"
rm -rf "${INCOMING:?}/"* 2>/dev/null || true
cp -f "$TARBALL_SRC" "$INCOMING/$TAR_NAME"
if [[ -f "$TARBALL_SRC.sha256" ]]; then cp -f "$TARBALL_SRC.sha256" "$INCOMING/$TAR_NAME.sha256"; fi

# --- verify checksum (abort before touching the install) ---------------------
# Compare HASH VALUES, not `sha256sum -c`. The sidecar records the name the file
# had when it was packed, and -c looks that name up on disk -- so renaming the
# tarball (or letting a transfer tool rename it) would fail verification even
# though the bytes are perfect. Hash-only comparison makes the filename
# irrelevant, which is the whole point: name the package whatever you like.
if [[ -f "$INCOMING/$TAR_NAME.sha256" ]]; then
  sed -i 's/\r$//' "$INCOMING/$TAR_NAME.sha256"   # tolerate a CRLF sidecar (Windows-edited)
  if command -v sha256sum >/dev/null 2>&1; then
    echo ">> verifying sha256..."
    # GNU "<hash>  <name>" and BSD "SHA256 (<name>) = <hash>" both supported.
    _first="$(awk 'NF{print $1; exit}' "$INCOMING/$TAR_NAME.sha256")"
    case "$_first" in
      SHA256|sha256) _expect="$(awk 'NF{print $NF; exit}' "$INCOMING/$TAR_NAME.sha256")" ;;
      *)             _expect="$_first" ;;
    esac
    _recorded="$(awk 'NF{print $2; exit}' "$INCOMING/$TAR_NAME.sha256")"
    [[ -n "$_expect" ]] || die "sidecar $TAR_NAME.sha256 is empty or unparseable."
    _actual="$(sha256sum "$INCOMING/$TAR_NAME" | awk '{print $1}')"
    _expect="$(printf '%s' "$_expect" | tr 'A-F' 'a-f')"
    _actual="$(printf '%s' "$_actual" | tr 'A-F' 'a-f')"
    if [[ "$_expect" != "$_actual" ]]; then
      echo "   expected $_expect" >&2
      echo "   actual   $_actual" >&2
      die "checksum FAILED -- the package is corrupt or truncated. Aborting, install untouched."
    fi
    if [[ -n "$_recorded" && "$_recorded" != "$TAR_NAME" ]]; then
      echo "   (contents verified; sidecar was written for '$_recorded', file is now '$TAR_NAME')"
    fi
  else
    echo "WARN: sha256sum not found; skipping checksum verification" >&2
  fi
else
  echo "WARN: no .sha256 sidecar next to the tarball; skipping checksum verification" >&2
fi

# --- extract to staging (full extract BEFORE touching the install) -----------
echo ">> extracting to staging..."
rm -rf "${STAGING:?}/"*
tar -xzf "$INCOMING/$TAR_NAME" -C "$STAGING"
# The package root is whatever single directory the archive contains -- do not
# hardcode it, so renaming the package (or the install dir) never breaks a deploy.
NEW=""
shopt -s nullglob
for _d in "$STAGING"/*; do
  if [[ -d "$_d" ]]; then
    if [[ -n "$NEW" ]]; then shopt -u nullglob; die "archive has more than one top-level directory -- bad package."; fi
    NEW="$_d"
  fi
done
shopt -u nullglob
[[ -n "$NEW" ]]           || die "archive has no top-level directory -- bad package."
[[ -f "$NEW/$SENTINEL" ]] || die "staged package missing $SENTINEL -- bad archive."

if [[ -f "$NEW/VERSION" ]]; then echo ">> incoming version:"; print_version "$NEW/VERSION"; fi

# --- guard: a preserved name must not also be shipped by the package ---------
# Otherwise the swap's `mv` would nest the new copy INSIDE the kept directory.
shopt -s dotglob nullglob
for _it in "$NEW"/*; do
  _n="$(basename "$_it")"
  if is_preserved "$_n"; then
    shopt -u dotglob nullglob
    die "'$_n' is in .deploy/preserve.list but the package also ships it. Remove it from preserve.list, or rename your local copy."
  fi
done
shopt -u dotglob nullglob

# --- backup + swap (the only non-atomic window; rollback on any failure) -----
TS="$(date +%Y%m%d-%H%M%S)"
BK="$BACKUPS/$TS"
mkdir -p "$BK"

shopt -s dotglob nullglob

# PHASE tells rollback() how much of the install is old vs new, which decides
# what it is allowed to delete. Getting this wrong destroys the install: if the
# BACKUP loop dies partway, the backup is INCOMPLETE and the files still sitting
# in TARGET are the only copy of the originals -- they must not be touched.
PHASE="backup"

rollback() {
  trap - ERR
  echo "!! swap failed during '$PHASE' -- rolling back from $BK" >&2

  # Only in the install phase is TARGET's content known to come from the new
  # package (the backup is complete by then), so only then may we clear it.
  if [[ "$PHASE" == "install" ]]; then
    for _it in "$TARGET"/*; do
      if is_untouchable "$(basename "$_it")"; then continue; fi
      rm -rf "$_it"
    done
  fi

  # Restore whatever actually made it into the backup, overwriting same-name
  # collisions. During a backup-phase failure the names in BK and TARGET are
  # disjoint (mv moves), so nothing in TARGET is lost.
  for _it in "$BK"/*; do
    _n="$(basename "$_it")"
    rm -rf "${TARGET:?}/$_n"
    mv "$_it" "$TARGET"/
  done
  echo "!! rollback complete -- install restored." >&2
  exit 1
}
trap rollback ERR

# NOTE: this moves deploy.sh -- this very script. That is safe on Linux: mv
# within one filesystem is a rename, the inode is untouched, and bash keeps
# reading the script through its open fd. (It does fail on Windows, which locks
# the running script -- but the red zone is Linux.) Do not "fix" this by copying
# the script elsewhere first.
echo ">> backing up current install -> $BK"
for _it in "$TARGET"/*; do
  if is_untouchable "$(basename "$_it")"; then continue; fi
  mv "$_it" "$BK"/
done

PHASE="install"
# Record what the package ships BEFORE the loop below empties $NEW -- the
# orphan report further down needs it, and after the mv there is nothing left
# in $NEW to ask. (Asking afterwards reports every backed-up name as an orphan,
# which is worse than not reporting at all: the one real warning drowns.)
_shipped=()
for _it in "$NEW"/*; do _shipped+=("$(basename "$_it")"); done

echo ">> installing new version..."
for _it in "$NEW"/*; do
  mv "$_it" "$TARGET"/
done

# post-swap integrity check -- a bare failing test trips the ERR trap -> rollback
if [[ ! -f "$TARGET/$SENTINEL" ]]; then echo "post-swap sentinel missing" >&2; false; fi

trap - ERR

# --- warn about content that only exists in the backup now -------------------
# Anything we moved out that the new package does NOT ship is the operator's own
# data (batch results, notes). It is safe in $BK today, but backup rotation
# deletes it after KEEP_BACKUPS more deploys -- silently. Say so once, loudly,
# and name the fix. This is the one place where the swap can eat real work.
_orphans=()
for _it in "$BK"/*; do
  _n="$(basename "$_it")"
  _hit=0
  for _s in ${_shipped[@]+"${_shipped[@]}"}; do
    if [[ "$_n" == "$_s" ]]; then _hit=1; break; fi
  done
  if (( ! _hit )); then _orphans+=("$_n"); fi
done
shopt -u dotglob nullglob

# --- rotate backups ----------------------------------------------------------
echo ">> rotating backups (keeping newest $KEEP_BACKUPS)..."
_i=0
while IFS= read -r _d; do
  _i=$((_i + 1))
  if (( _i > KEEP_BACKUPS )); then rm -rf "$_d"; fi
done < <(ls -1dt "$BACKUPS"/*/ 2>/dev/null || true)

# --- rotate uploaded deliveries ----------------------------------------------
# Only reached after the swap succeeded and the sentinel check passed, so a
# failed deploy leaves every delivery exactly where it was. Ordered the same way
# the picker orders them (ls -t, newest mtime first), so "kept" here means the
# same thing as "would be chosen" up there. The tarball just deployed is newest
# by that ordering and is therefore always kept; it is also compared by path as
# a belt-and-braces guard, in case a transfer tool preserved an odd mtime.
if (( KEEP_PACKAGES > 0 )); then
  # Same empty-glob trap as the picker above, and here it would `rm` -- collect
  # explicitly so ls can never fall back to listing the current directory.
  _doomed=(); _seen=0
  shopt -s nullglob
  _all=()
  for _p in "$TARGET"/*.tar.gz; do _all+=("$_p"); done
  shopt -u nullglob
  if (( ${#_all[@]} > 0 )); then
    while IFS= read -r _p; do
      [[ -n "$_p" ]] || continue
      _seen=$((_seen + 1))
      (( _seen <= KEEP_PACKAGES )) && continue
      [[ "$(readlink -f "$_p")" == "$TARBALL_SRC" ]] && continue
      _doomed+=("$_p")
    done < <(ls -1t "${_all[@]}")
  fi

  if (( ${#_doomed[@]} > 0 )); then
    echo ">> rotating deliveries (keeping newest $KEEP_PACKAGES)..."
    for _p in "${_doomed[@]}"; do
      rm -f "$_p" "$_p.sha256"
      echo "     removed $(basename "$_p")"
    done
  fi
fi

rm -rf "${STAGING:?}/"*

# --- done --------------------------------------------------------------------
echo
echo "OK  deployed."
if [[ -f "$TARGET/VERSION" ]]; then echo "    installed version:"; print_version "$TARGET/VERSION"; fi
echo "    previous install backed up at: $BK"
if (( ${#PRESERVE[@]} > 1 )); then
  echo "    preserved across swap: ${PRESERVE[*]}"
fi
if (( ${#_orphans[@]} > 0 )); then
  echo
  echo "    NOTE: these were NOT part of the new package, so they now live only"
  echo "          in the backup above: ${_orphans[*]}"
  echo "          If that is your own data (batch results, notes), move it back"
  echo "          and add the name to .deploy/preserve.list so future deploys"
  echo "          leave it alone -- otherwise backup rotation deletes it after"
  echo "          $KEEP_BACKUPS more deploys."
fi
if (( KEEP_PACKAGES > 0 )); then
  echo "    $(basename "$TARBALL_SRC") left in place; the newest $KEEP_PACKAGES deliveries are"
  echo "      kept here and older ones are removed on the next successful deploy"
  echo "      (a copy of this one is also in .deploy/incoming/)"
else
  echo "    $(basename "$TARBALL_SRC") left in place; delete it whenever you like"
  echo "      (a copy is kept in .deploy/incoming/)"
fi
echo
echo "    NEXT -- check this box can actually run it (no network / no venv needed):"
echo "       cd $TARGET && bash deploy/doctor.sh --test"
