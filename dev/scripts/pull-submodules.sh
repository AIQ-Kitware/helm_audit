#!/usr/bin/env bash
# Sync every submodule along its pinned branch.
#
# This is the "after I pulled the parent repo" flow:
#   git pull --ff-only
#   make pull-submodules
#
# The script does three things in order, per submodule:
#
#   1. ``git submodule update --init`` — populate any missing working
#      trees. Idempotent. May leave HEAD detached on the gitlink
#      commit; that's normal and we fix it in step 2.
#
#   2. **Attach detached HEADs to the pinned branch.** When a clone
#      starts cold, ``git submodule update --init`` checks out the
#      gitlink SHA in detached state — even when ``branch =`` is set
#      in .gitmodules. We re-attach HEAD to that branch so day-to-day
#      development works naturally. The local branch is created from
#      ``origin/<branch>`` if it doesn't exist yet.
#
#   3. ``git submodule update --remote --merge`` — fetch the upstream
#      tip and merge it into the now-attached branch. Local commits
#      are preserved (merge-on-pull semantics). This is what makes
#      "pinned to a branch" actually mean "follows the branch tip"
#      instead of "frozen at one commit".
#
# Submodules without a ``branch =`` entry skip steps 2 and 3 — they
# stay on the gitlink commit (legacy behavior).
#
# Submodules currently checked out on a *different* branch than the
# pin are left alone with a warning. We don't force-switch — that
# could orphan work-in-progress.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# 0. Propagate any ``.gitmodules`` URL changes into both the parent
#    ``.git/config submodule.<path>.url`` and each submodule's own
#    ``remote.origin.url``. Without this, a URL change committed
#    upstream (e.g., switching ``every_eval_ever`` from ``evaleval``
#    to ``Erotemic``) reaches the file but each clone keeps fetching
#    from its old origin until ``submodule sync`` runs. The symptom
#    is fetch failures like "Unable to find current origin/<branch>
#    revision" because the branch only exists on the *new* remote.
echo "Syncing submodule URLs from .gitmodules ..."
git submodule sync --recursive

# 1. First-time / missing working-tree initialization.
echo
echo "Initializing missing submodules ..."
git submodule update --init

# 2. + 3. Per-submodule branch attach + remote merge.
echo
echo "Attaching pinned submodules to their branch and merging upstream ..."
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

    current=$(git -C "$sm" symbolic-ref --quiet --short HEAD 2>/dev/null || echo "")

    if [ -z "$current" ]; then
      # Detached HEAD. Attach to the pinned branch.
      if git -C "$sm" rev-parse --verify --quiet "$branch" >/dev/null 2>&1; then
        # Local branch exists already; just check it out.
        git -C "$sm" switch "$branch"
        echo "  $sm: attached to local '$branch'"
      else
        # Local branch doesn't exist; create it tracking origin/<branch>.
        # Fetch first so the remote-tracking ref exists.
        git -C "$sm" fetch origin "$branch" --quiet \
          || echo "  WARN: $sm: could not fetch '$branch' from origin"
        if git -C "$sm" rev-parse --verify --quiet "origin/$branch" >/dev/null 2>&1; then
          git -C "$sm" switch -c "$branch" --track "origin/$branch"
          echo "  $sm: created local '$branch' tracking 'origin/$branch'"
        else
          echo "  WARN: $sm: 'origin/$branch' does not exist on the remote;"
          echo "        leaving submodule in detached HEAD."
          continue
        fi
      fi
      current="$branch"
    elif [ "$current" != "$branch" ]; then
      echo "  $sm: on '$current', not the pinned '$branch' — leaving alone"
      continue
    fi

    # Branch is attached and matches the pin. Fetch + merge upstream tip.
    echo "  $sm: tracking '$branch' — merging upstream"
    if ! git submodule update --remote --merge "$sm"; then
      echo "  WARN: failed to update $sm (see message above);"
      echo "        usually a fetch problem (auth) or a merge conflict."
    fi
  done
fi

echo
echo "Done. Current submodule status:"
echo
bash "$ROOT/dev/scripts/submodule-status.sh"
