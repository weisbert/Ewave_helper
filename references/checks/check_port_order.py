"""Is the GUI's -p port list just an ASCII sort of the pin names?

If yes, eWave's `--all` (which assigns P000.. "in lexicographical order") reproduces the
GUI's mapping exactly -- meaning we need neither the GUI run dir nor any GDS parsing.
"""

# Port names below are PLACEHOLDERS with the same shape as the real ones (13 upper-case
# + 4 lower-case, two families differing only in a digit): the real pin names are
# site-local and stay out of this repo.  The property under test survives the rename --
# what matters is that upper- and lower-case names are interleaved under a
# case-insensitive sort but cleanly separated under an ASCII one.
# The real 17 names were checked in place (`LC_ALL=C sort` against the GUI's -p order,
# 17/17); this script is the reproducible method, not the evidence itself.
gui_order = [
    "GND_A", "PORTN", "PORTP", "RAILN", "RAILP",
    "XN_LINE1A", "XN_LINE1B", "XN_LINE1C", "XN_LINE2A", "XN_LINE2B",
    "XP_LINE1A", "XP_LINE1B", "XP_LINE1C",
    "amn", "amp", "ubn", "ubp",
]

ascii_sorted = sorted(gui_order)                       # case-SENSITIVE, ASCII
ci_sorted = sorted(gui_order, key=str.lower)           # case-INSENSITIVE

print(f"ports: {len(gui_order)}")
print(f"GUI order == ASCII sort (case-sensitive) : {gui_order == ascii_sorted}")
print(f"GUI order == case-insensitive sort       : {gui_order == ci_sorted}")

if gui_order != ascii_sorted:
    print("\nfirst divergence vs ASCII sort:")
    for i, (a, b) in enumerate(zip(gui_order, ascii_sorted)):
        if a != b:
            print(f"  index {i}: GUI={a!r}  ascii={b!r}")
            break

print("\nside by side (P00x / GUI / ascii-sorted / ci-sorted):")
for i, name in enumerate(gui_order):
    mark = "" if name == ascii_sorted[i] else "   <-- ASCII differs"
    print(f"  P{i:03d}  {name:<10} {ascii_sorted[i]:<10} {ci_sorted[i]:<10}{mark}")
