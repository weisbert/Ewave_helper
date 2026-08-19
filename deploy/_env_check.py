"""Probe ONE Python interpreter for Ewave_helper capability tiers.

Called by ``deploy/doctor.sh`` once per candidate interpreter; prints machine
readable ``KEY=VALUE`` lines on stdout.  Not meant to be run by hand (but
harmless if you do).

Deliberately written in the most conservative syntax available -- no f-strings,
no annotations, no dataclasses -- so that even an ancient interpreter can PARSE
it and cleanly report itself as unusable, instead of dying with a SyntaxError
that looks like a corrupt package.

Everything printed here is pure ASCII on purpose: the red zone's ``LANG`` is
often ``C``, and a non-ASCII byte in this stream would make the probe die with
UnicodeEncodeError -- i.e. a perfectly good box would look broken.

The strong check here is the real ``import`` of the shipped package, not just
"is Python new enough": importing ``ewave_batch.core.cmd`` proves the package
landed intact AND that this interpreter can compile it.

Tiers (PROJECT_BRIEF section 12; the table is repeated in deploy/README.md):

    tier 1  parse an official run dir, build commands, dry-run, run the tests
            needs: this interpreter only -- the whole core is stdlib
    tier 2  actually submit a batch to the cluster
            needs: + dsub / djob / ewave / strmout on PATH
    tier 3  GUI
            needs: + tkinter that can really open a window ($DISPLAY)

Tiers are cumulative (tier 3 implies tier 2 implies tier 1), which is what the
brief's "needs: ... plus ..." column means.  A missing tier-3 dependency is a
DEGRADE, not a failure: a plain ssh session is supposed to have no $DISPLAY.
"""

import os
import sys

# Why 3.10 and not 3.8 like the sibling project: ewave_batch/model.py has
#     FlagValue = str | bool
# at module level -- a PEP-604 union evaluated at RUNTIME (it is not an
# annotation, so ``from __future__ import annotations`` does not defer it),
# which needs 3.10.  The deployment target is 3.11.4; 3.10 is simply the oldest
# interpreter that can still import us.
MIN_PY = (3, 10)

# tier 2 = "can really submit a batch".  dsub submits and djob polls
# (ewave_batch/sched/donau.py drives both); ewave and strmout are the two
# stages.  djob is listed even though the brief's summary table names only
# three: without it sched.driver never sees a job leave the queue, so every run
# would sit in PENDING forever -- "can submit" without "can poll" is not a
# working tier 2.
TOOLS_FOR_SUBMIT = ("dsub", "djob", "ewave", "strmout")


def emit(key, value):
    sys.stdout.write("%s=%s\n" % (key, value))


def try_import(name):
    """Return (ok, detail). detail is a version string or the error text."""
    try:
        mod = __import__(name)
    except Exception:
        exc = sys.exc_info()[1]
        return False, ("%s: %s" % (exc.__class__.__name__, exc)).replace("\n", " ")
    ver = getattr(mod, "__version__", "")
    return True, ver


# ---------------------------------------------------------------------------
# Tier decision -- pure functions, so tests/test_deploy.py can drive them with a
# hand-written truth table instead of needing a real box.  Keep them free of
# I/O: the moment they probe anything themselves, the test has to fake a whole
# environment and stops being a test of the DECISION.
# ---------------------------------------------------------------------------


def missing_tools(tools_found):
    """Which of TOOLS_FOR_SUBMIT are absent, in the declared order."""
    out = []
    for name in TOOLS_FOR_SUBMIT:
        if not tools_found.get(name):
            out.append(name)
    return out


def decide_tier(py_ok, core_ok, tools_found, tk_ok, display_ok):
    """Capability facts -> tier number.

    0  unusable (interpreter too old, or the package will not import)
    1  core: parse an official run dir + plan + dry-run + unit tests
    2  + real submission
    3  + GUI

    Cumulative on purpose: a box with tkinter but no dsub is tier 1, because
    "tier 3" in the brief means "everything below it, and a GUI on top".
    """
    if not py_ok or not core_ok:
        return 0
    if missing_tools(tools_found):
        return 1
    if not (tk_ok and display_ok):
        return 2
    return 3


def tier_blocker(tier, py_ok, core_ok, tools_found, tk_ok, display_ok):
    """One line naming what keeps this interpreter out of the next tier up."""
    if tier == 0:
        if not py_ok:
            return "python is older than %d.%d" % MIN_PY
        return "the shipped package does not import -- incomplete install?"
    if tier == 1:
        return "not on PATH: " + ", ".join(missing_tools(tools_found))
    if tier == 2:
        if not tk_ok:
            return "this interpreter has no tkinter"
        return "tkinter cannot open a window (no $DISPLAY -- normal in a plain ssh session)"
    return ""


# ---------------------------------------------------------------------------


def main():
    emit("PY_EXEC", sys.executable or "?")
    emit("PY_VERSION", sys.version.split()[0])
    # The red-zone rule is "no venv" (nothing can be installed there anyway) --
    # flag one if we are somehow inside it, so doctor.sh can warn instead of
    # silently recommending a non-reproducible interpreter.
    base = getattr(sys, "base_prefix", getattr(sys, "real_prefix", sys.prefix))
    emit("PY_VENV", "YES" if base != sys.prefix else "NO")

    py_ok = sys.version_info[0] >= 3 and sys.version_info[:2] >= MIN_PY
    if not py_ok:
        emit("PY_OK", "NO")
        emit("PY_WHY", "need >= %d.%d" % MIN_PY)
        emit("TIER", "0")
        emit("TIER_WHY", tier_blocker(0, False, False, {}, False, False))
        return 1
    emit("PY_OK", "YES")

    # --- tkinter (tier 3 only; the CLI must work without it) ---------------
    tk_ok, tk_detail = try_import("tkinter")
    emit("MOD_tkinter", "OK" if tk_ok else "MISSING")
    emit("MOD_tkinter_detail", tk_detail)

    display_ok = False
    display = "SKIP"
    if tk_ok:
        display = "NO"
        try:
            import tkinter as _tk

            _root = _tk.Tk()
            _root.destroy()
            display = "YES"
            display_ok = True
        except Exception:
            display = "NO"
    emit("TK_DISPLAY", display)
    emit("ENV_DISPLAY", os.environ.get("DISPLAY", ""))

    # PyYAML is the ONE optional dependency (spec files); core.spec imports it
    # lazily and falls back to JSON.  Informational only -- never a tier gate.
    yaml_ok, yaml_detail = try_import("yaml")
    emit("MOD_yaml", "OK" if yaml_ok else "MISSING")
    emit("MOD_yaml_detail", yaml_detail)

    # --- the shipped package (the real test) -------------------------------
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    emit("INSTALL_ROOT", root)
    if root not in sys.path:
        sys.path.insert(0, root)

    cmd_ok, cmd_detail = try_import("ewave_batch.core.cmd")
    emit("IMP_core_cmd", "OK" if cmd_ok else "FAIL")
    emit("IMP_core_cmd_detail", cmd_detail)

    cli_ok, cli_detail = try_import("ewave_batch.cli")
    emit("IMP_cli", "OK" if cli_ok else "FAIL")
    emit("IMP_cli_detail", cli_detail)

    dry_ok, dry_detail = try_import("ewave_batch.redzone_dryrun")
    emit("IMP_redzone_dryrun", "OK" if dry_ok else "FAIL")
    emit("IMP_redzone_dryrun_detail", dry_detail)

    # gui.app must import even when tkinter is absent (CLAUDE.md hard rule 5:
    # the GUI's imports are lazy so a plain ssh session can still run the CLI).
    # Importing it here on a box without tkinter is a live check of exactly that.
    gui_ok, gui_detail = try_import("gui.app")
    emit("IMP_gui_app", "OK" if gui_ok else "FAIL")
    emit("IMP_gui_app_detail", gui_detail)

    core_ok = cmd_ok and cli_ok and dry_ok

    # --- external tools (tier 2) -------------------------------------------
    # Reuse the tool's OWN lookup (PATH first, then <NAME>_BIN / <NAME>_ABS)
    # instead of keeping a second copy of that rule here: doctor has to report
    # what the tool would find, not what a lookalike would.  If the package did
    # not import we are at tier 0/1 already and the answer cannot change that.
    tools_found = {}
    for name in TOOLS_FOR_SUBMIT:
        tools_found[name] = None
    if core_ok:
        try:
            from ewave_batch.core.discover import find_tool

            for name in TOOLS_FOR_SUBMIT:
                tools_found[name] = find_tool(name)
        except Exception:
            exc = sys.exc_info()[1]
            detail = ("%s: %s" % (exc.__class__.__name__, exc)).replace("\n", " ")
            emit("TOOL_LOOKUP_ERROR", detail)
    for name in TOOLS_FOR_SUBMIT:
        where = tools_found.get(name)
        emit("TOOL_" + name, "OK" if where else "MISSING")
        emit("TOOL_" + name + "_detail", where or "")

    # --- capability tiers --------------------------------------------------
    tier = decide_tier(py_ok, core_ok, tools_found, tk_ok, display_ok)
    emit("CAP_core", "YES" if tier >= 1 else "NO")
    emit("CAP_submit", "YES" if tier >= 2 else "NO")
    emit("CAP_gui", "YES" if tier >= 3 else "NO")
    # "the GUI code is fine, this box just has no X11" -- worth saying out loud
    # so a headless ssh session is not mistaken for a broken install.
    emit("GUI_CODE", "OK" if (core_ok and tk_ok) else "NO")
    emit("TIER", "%d" % tier)
    emit("TIER_WHY", tier_blocker(tier, py_ok, core_ok, tools_found, tk_ok, display_ok))
    return 0


if __name__ == "__main__":
    sys.exit(main())
