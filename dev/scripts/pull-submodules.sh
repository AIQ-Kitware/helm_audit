#!/usr/bin/env bash
# Sync every submodule along its pinned branch.
#
# This is the "after I pulled the parent repo" flow:
#   git pull --ff-only
#   make pull-submodules
#
# Behavior depends on whether the submodule has a ``branch =`` entry
# in .gitmodules (set up via ``make configure-submodule-branches``):
#
#   - branch-pinned submodule: ``git submodule update --init --remote
#     --merge`` is used. This fetches the configured upstream branch
#     and merges its tip into the submodule's currently-checked-out
#     branch. Local commits are preserved (merge-on-pull semantics).
#     If the local branch matches the configured branch, this is a
#     fast-forward in the common case.
#
#   - submodule with no branch entry: ``git submodule update --init``
#     is used (legacy behavior). Will check out the parent's gitlink
#     commit, possibly leaving a detached HEAD.
#
# Why two behaviors? The ``branch`` entry is the user's signal that
# they want to develop in the submodule on a real branch and have
# pulls fast-forward, not snap to a frozen commit. We honor that
# signal where present and stay out of the way otherwise.
#
# After running, the submodule status table is printed so any merge
# conflicts, detached HEADs, or no-upstream branches are easy to spot.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# First-time clones need the working trees populated before --remote
# can do anything useful. ``--init`` is idempotent.
echo "Initializing missing submodules ..."
git submodule update --init

# Pin-aware update: --remote follows the configured branch; --merge
# preserves local commits in the submodule's checked-out branch.
# Submodules without a ``branch =`` entry are silently no-ops for
# --remote (git logs a warning that we suppress to keep output clean).
echo
echo "Updating branch-pinned submodules to upstream tips ..."
mapfile -t PINNED < <(
  git config --file .gitmodules --get-regexp 'submodule\..*\.branch' \
    | awk '{print $1}' \
    | sed -E 's@^submodule\.(.*)\.branch$@\1@' \
    | sort
)

if [ ${#PINNED[@]} -eq 0 ]; then
  echo "  (no submodules have a 'branch =' entry; run"
  echo "   'make configure-submodule-branches' to set them up)"
else
  for sm in "${PINNED[@]}"; do
    branch=$(git config --file .gitmodules --get "submodule.$sm.branch")
    echo "  $sm: tracking '$branch'"
    # Fetch + merge for this submodule. We go submodule-by-submodule
    # rather than ``git submodule update --remote --merge`` in bulk
    # so an error in one doesn't abort the rest, and so the per-
    # submodule output is attributable.
    if ! git submodule update --remote --merge "$sm"; then
      echo "  WARN: failed to update $sm (see message above);"
      echo "        usually a merge conflict in the submodule —"
      echo "        resolve inside that working tree and re-run."
    fi
  done
fi

echo
echo "Done. Current submodule status:"
echo
bash "$ROOT/dev/scripts/submodule-status.sh"
