"""Diff the 2025.09.sp1 `ewave --help` dump against the 2026-05-07 capture in the kit.

Both sides are normalised by stripping trailing whitespace (the new dump has trailing
spaces on most lines, which would otherwise make every line look changed).
"""
import difflib
import pathlib
import re

# Paths are resolved relative to this file so the check keeps working if the repo moves.
_REFS = pathlib.Path(__file__).resolve().parents[1]          # .../references
OLD = _REFS / "ewave_donau_kit" / "ewave" / "ewave_cli_reference.md"
NEW = _REFS / "probes" / "ewave_probe_2025.09.sp1.txt"


def norm(lines):
    return [ln.rstrip() for ln in lines]


def old_help():
    """The verbatim block: between the ``` fences, dropping the leading `ewave --help` echo."""
    text = OLD.read_text(encoding="utf-8", errors="replace").splitlines()
    fences = [i for i, ln in enumerate(text) if ln.strip() == "```"]
    body = text[fences[0] + 1:fences[1]]
    if body and body[0].strip() == "ewave --help":
        body = body[1:]
    return norm(body)


def new_help():
    """From `Usage: ewave [options]` up to the trailing shell prompt."""
    text = NEW.read_text(encoding="utf-8", errors="replace").splitlines()
    start = next(i for i, ln in enumerate(text) if ln.startswith("Usage: ewave"))
    end = len(text)
    for i in range(start, len(text)):
        # trailing shell prompt, e.g. "<host>:<cwd>> " -- host name is site-local
        if re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*:.*>", text[i]):
            end = i
            break
    return norm(text[start:end])


a, b = old_help(), new_help()
print(f"old (2026-05-07 capture): {len(a)} lines")
print(f"new (2025.09.sp1)       : {len(b)} lines")

# --- flag-level comparison: the thing that actually matters -----------------
FLAG = re.compile(r"^(--[A-Za-z0-9][A-Za-z0-9]*)")


def flags(lines):
    out = {}
    for ln in lines:
        if ln.startswith("-") and not ln.startswith(" "):
            # a flag decl line, e.g. "--edgeDist=value, -e value" or "-p value"
            name = ln.split(",")[0].split("=")[0].strip()
            out[name] = ln
    return out


fa, fb = flags(a), flags(b)
only_old = [k for k in fa if k not in fb]
only_new = [k for k in fb if k not in fa]
changed = [k for k in fa if k in fb and fa[k] != fb[k]]

print(f"\n=== FLAGS: old {len(fa)} / new {len(fb)} ===")
print(f"removed in 2025.09.sp1 ({len(only_old)}): {only_old or '(none)'}")
print(f"added   in 2025.09.sp1 ({len(only_new)}): {only_new or '(none)'}")
print(f"decl-line changed ({len(changed)}):")
for k in changed:
    print(f"  {k}\n    old: {fa[k]}\n    new: {fb[k]}")

# --- full unified diff ------------------------------------------------------
d = list(difflib.unified_diff(a, b, "old_2026-05-07", "new_2025.09.sp1", lineterm="", n=2))
print(f"\n=== UNIFIED DIFF: {len(d)} lines ===")
if len(d) <= 400:
    print("\n".join(d))
else:
    out = pathlib.Path(NEW).with_name("help_diff.txt")
    out.write_text("\n".join(d), encoding="utf-8")
    print(f"(too long, written to {out}; first 200 lines below)")
    print("\n".join(d[:200]))
