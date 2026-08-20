"""Is the GUI's -p port list just an ASCII sort of the pin names?

If yes, eWave's `--all` (which assigns P000.. "in lexicographical order") reproduces the
GUI's mapping exactly -- meaning we need neither the GUI run dir nor any GDS parsing.
"""

# The names below are INVENTED, not the real pin names and not a rename of them:
# an earlier version kept the real suffixes and only swapped the prefixes, which leaks
# exactly the thing that must not leak (a searchable net-naming family).  Neither the
# names, their count, nor their family structure has anything to do with any real design.
#
# What the check needs from them is only this: some names are upper-case and some are
# lower-case, so that a case-INsensitive sort interleaves them while an ASCII
# (case-sensitive) sort keeps the two blocks apart.  That is the single property that
# tells the two candidate orderings apart, and it survives any renaming.
#
# The real list was checked in place, on the machine that owns it (`LC_ALL=C sort`
# against the GUI's own -p order, every position matched).  This script is the
# reproducible method, not the evidence itself -- the evidence never leaves that machine.
gui_order = [
    "ALPHA", "CHARLIE", "ECHO", "GOLF",
    "bravo", "delta", "foxtrot",
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
    print(f"  P{i:03d}  {name:<9} {ascii_sorted[i]:<9} {ci_sorted[i]:<9}{mark}")
