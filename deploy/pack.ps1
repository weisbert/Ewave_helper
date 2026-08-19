#requires -Version 4.0
<#
.SYNOPSIS
  Pack Ewave_helper (committed HEAD) into a git-free tarball for the red zone.

.DESCRIPTION
  Runs on the dev/yellow zone (Windows). Uses `git archive`, so the package is
  built from committed blobs in the object store, NOT from the Windows working
  tree:
    * paths are always POSIX "/", never "\"
    * text is LF (the index stores LF; an autocrlf checkout cannot corrupt the
      package -- this is what keeps deploy.sh runnable by the red zone's bash)
    * no .git/, no .gitattributes/.gitignore, no CLAUDE.md, no pack.ps1,
      no mockups/ or mvp/ (all export-ignored in .gitattributes)
    * VERSION is stamped with the real commit hash + date via export-subst

  What ships is a BLACKLIST, not a whitelist: everything in the repo crosses
  the gap unless .gitattributes export-ignores it, so new modules and new tests
  are packaged automatically and this script never needs touching.

  Emits exactly two files, under <OutDir>:
    <Name>_<shorthash>.tar.gz              the whole package (code + tests + docs)
    <Name>_<shorthash>.tar.gz.sha256
  Upload both; that is the entire delivery. The sidecar is in GNU
  `sha256sum -c` format (LF, no BOM).

  Requires only git + PowerShell (Get-FileHash). No Python, no external tar,
  no downloaded packages.

  Meant to run in the yellow zone, on a fresh `git clone`/`git pull` of the
  public repo -- so the <shorthash> in the package name is exactly the commit
  on GitHub, and "which build is installed over there" has a one-word answer.

.PARAMETER Ref
  Git ref to package (default HEAD).

.PARAMETER OutDir
  Output directory (default: <script dir>\dist).

.PARAMETER Name
  Package root directory name, i.e. what you get after `tar -xzf`, and the name
  of the red-zone install dir. Default 'ewave_helper'.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\pack.ps1
#>
param(
    [string]$Ref    = 'HEAD',
    [string]$OutDir = (Join-Path $PSScriptRoot 'dist'),
    [string]$Name   = 'ewave_helper'
)
$ErrorActionPreference = 'Stop'

# Repo root = parent of this script's dir (script lives at <repo>\deploy\).
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# Same sentinel deploy.sh checks for on the far side. Nested on purpose: a bare
# cli.py exists in half the repos on the box, ewave_batch/model.py does not.
$Sentinel = 'ewave_batch/model.py'
$Prefix   = $Name

# --- sanity ------------------------------------------------------------------
& git rev-parse --is-inside-work-tree 1>$null 2>$null
if ($LASTEXITCODE -ne 0) { throw "Not a git work tree: $RepoRoot" }
if (-not (Test-Path (Join-Path $RepoRoot $Sentinel))) {
    throw "$Sentinel not found in $RepoRoot - run this from the Ewave_helper repo."
}

$dirty = & git status --porcelain
if ($dirty) {
    Write-Warning "Working tree has uncommitted changes; they will NOT be packaged (git archive ships committed $Ref only). Commit them first."
}

$short = (& git rev-parse --short $Ref).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrEmpty($short)) { throw "cannot resolve ref: $Ref" }

# --- locate cmd.exe ----------------------------------------------------------
# The CR preflight below redirects a git stream to a file THROUGH cmd.exe,
# because PowerShell's own capture re-encodes the stream (LF -> CRLF) and would
# make the check pass on a package that is actually CRLF -- i.e. the check
# would lie in exactly the direction that bricks a deploy. Resolve cmd.exe from
# %ComSpec% / the system directory rather than the PATH: a hand-edited or
# truncated PATH that has lost C:\Windows\System32 still packs fine. Observed
# in the wild as "The term 'cmd.exe' is not recognized", which is a baffling
# thing to hit while packaging.
$ComSpec = $env:ComSpec
if ([string]::IsNullOrEmpty($ComSpec) -or -not (Test-Path -LiteralPath $ComSpec)) {
    $ComSpec = Join-Path ([Environment]::GetFolderPath('System')) 'cmd.exe'
}
if (-not (Test-Path -LiteralPath $ComSpec)) {
    throw "cmd.exe not found (ComSpec='$($env:ComSpec)'). It is required for byte-exact stream redirection - repair your PATH/environment."
}

# --- preflight: the shell scripts MUST reach the red zone as LF --------------
# This is the single failure mode that bricks a red-zone deploy (bash reports
# `$'\r': command not found`). Catch it here, at pack time, not over there.
# Two independent guards, because either one alone can be true while the package
# still comes out CRLF:
#   1. the blob in the index is LF
#   2. the `eol` attribute is lf, so `git archive` does not re-expand it to CRLF
#      via core.eol=native (the Windows default). Without an explicit eol, a
#      `text`-marked file IS re-expanded -- verified, not theoretical.
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }

$shScripts = @('deploy.sh', 'deploy/doctor.sh')
foreach ($sh in $shScripts) {
    $eolInfo = & git ls-files --eol -- $sh
    if ([string]::IsNullOrEmpty($eolInfo)) { throw "$sh is not tracked by git - `git add` it first." }
    if ($eolInfo -notmatch 'i/lf') {
        throw "$sh has CRLF in the git index. The red zone's bash cannot run it. Fix with: git add --renormalize $sh"
    }
}

# Guard 2 inspects the ACTUAL ARCHIVE OUTPUT, not the working tree. `git
# check-attr` would read the working-tree .gitattributes, but `git archive`
# reads the one committed in $Ref -- so an uncommitted .gitattributes fix looks
# green while the package still ships CRLF. Archive the scripts on their own and
# scan the raw bytes: a tar holds ASCII headers plus file content, and neither
# may contain CR here, so a single byte scan is exact.
$probeTar = Join-Path $OutDir ("_pack_probe_{0}.tar" -f $PID)   # stays in OutDir, not the system temp
& $ComSpec /c "git archive --format=tar $Ref -- $($shScripts -join ' ') > `"$probeTar`" 2>nul"
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $probeTar)) {
    if (Test-Path $probeTar) { Remove-Item -LiteralPath $probeTar -Force }
    throw "git archive cannot find $($shScripts -join ', ') in $Ref - commit them (and .gitattributes) first."
}
$probeBytes = [System.IO.File]::ReadAllBytes($probeTar)
Remove-Item -LiteralPath $probeTar -Force
if ($probeBytes -contains [byte]13) {
    throw "git archive emits CRLF for the shell scripts, so the red zone's bash would fail with `$'\r'. Ensure .gitattributes (with '* text=auto eol=lf') is COMMITTED in $Ref, not just saved."
}

# --- pack --------------------------------------------------------------------
$tarName = "${Prefix}_$short.tar.gz"
$tarPath = Join-Path $OutDir $tarName

Write-Host ">> packaging $Ref ($short) -> $tarPath"
& git archive --format=tar.gz --prefix="$Prefix/" -o $tarPath $Ref
if ($LASTEXITCODE -ne 0) { throw "git archive failed" }

# --- sha256 sidecar: "<hash>  <name>\n", LF + no BOM (GNU sha256sum -c) ------
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
function Write-Sha256Sidecar([string]$FilePath) {
    $h = (Get-FileHash -Algorithm SHA256 -LiteralPath $FilePath).Hash.ToLower()
    $n = Split-Path -Leaf $FilePath
    [System.IO.File]::WriteAllText("$FilePath.sha256", "$h  $n`n", $utf8NoBom)
    return $h
}
$hash = Write-Sha256Sidecar $tarPath

# --- report ------------------------------------------------------------------
$size = '{0:N1} KB' -f ((Get-Item $tarPath).Length / 1KB)
Write-Host ""
Write-Host "OK  package : $tarPath  ($size)"
Write-Host "    sha256  : $hash"
Write-Host ""
Write-Host "commit info:"
& git --no-pager show -s --format='    %h  %cI  %s' $Ref
Write-Host ""
Write-Host "NEXT -- upload BOTH files into  .../$Prefix/ :"
Write-Host "       $tarName"
Write-Host "       $tarName.sha256"
Write-Host "     then on the red zone (login shell is often tcsh -- use bash):"
Write-Host "       cd .../$Prefix"
Write-Host "       bash deploy.sh                    # picks up the tarball sitting here"
Write-Host "       bash deploy/doctor.sh --test      # verify the box can run it"
Write-Host ""
Write-Host "     (first time only: tar -xzf $tarName  ->  ./$Prefix/ is the install)"
