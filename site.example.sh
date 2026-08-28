# site.example.sh -- template for THIS machine's site coordinates.
#
# This file is a TEMPLATE: every value in it is a placeholder, so it carries no
# site identity and is safe to publish. It is tracked by git.
#
#   cp site.example.sh site.local.sh     # then edit site.local.sh
#
# site.local.sh holds the real values and is NOT tracked (see .gitignore), so a
# company account name never reaches the public repo. That is CLAUDE.md hard
# constraint 1b, and it cuts both ways: the values must stay out of the source,
# AND you must not have to retype them every time you open the GUI.
#
# WHERE IT IS LOOKED UP (first hit wins):
#   1. $EWB_SITE_LOCAL          an explicit path to the file
#   2. the install root         the directory holding deploy.sh -- the normal
#                               place on the red-zone box
#   3. the current directory
#
# ACROSS UPGRADES: deploy.sh keeps site.local.sh (it is in the preserve set next
# to .deploy/), so you write it once per box and every later deploy leaves it
# alone. It is never shipped inside a package, so it can never be overwritten
# by one either.
#
# FORMAT: KEY=value, one per line, '#' starts a comment. Quote the value if it
# contains spaces. Read by a small parser, NOT by a shell -- so no command
# substitution, no variable expansion, no line continuations. Unknown keys are
# ignored (they never break the GUI).

# The whole dsub submit line, verbatim -- what the GUI shows in the "Donau
# submit" box when it opens.
#
# SINCE 2026-08-28 YOU USUALLY DO NOT NEED THIS FILE AT ALL. The GUI now opens
# with a real, submittable account and queue built in (gui.state
# DEFAULT_SUBMIT_ACCOUNT / DEFAULT_SUBMIT_QUEUE), so a fresh box works out of
# the box. Write this file only when YOUR account or queue differs from that
# default -- which is the whole reason the override still exists.
#
# The GUI exposes this line for editing, so what you put here is a starting
# point, not a lock. Two things still override it, in this order:
#   * anything you type in the box wins immediately;
#   * setting "Official run dir" replaces the line with the triplet parsed out
#     of that run's remote_run_ewave.sh -- the script that actually ran is a
#     better source than any default, including this one.
# So if you never set this, the GUI already starts from a working default, and
# it still gets the site's own account and queue the moment you point it at an
# official run -- this file only matters when both of those are wrong for you.
#
# Shape (flags and order are what sched.donau builds):
#   dsub -A <account> -q <queue> -R "cpu=<n>;mem=<n>"
# Do NOT add -I / -Kc / -Kco here: submission is asynchronous and polled, and a
# blocking submit would hang the scheduling loop with no visible cause.
EWB_SUBMIT_COMMAND='dsub -A ACCOUNT -q QUEUE -R "cpu=20;mem=100000"'
