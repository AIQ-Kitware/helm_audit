#!/usr/bin/env bash
# One-time configuration: pin each submodule to a branch (rather than a
# specific commit), so day-to-day development inside a submodule feels
# natural — checked-out branch, no detached HEAD, ``git submodule
# update`` fast-forwards the branch instead of resetting to the
# gitlink.
#
# What this changes
# -----------------
#
# 1. ``.gitmodules`` gets a ``branch = <name>`` entry for every
#    submodule. Subsequent ``git submodule update --remote`` will fetch
#    that branch from origin and update the working tree to its tip.
#
# 2. The parent repo's ``.git/config`` gets ``submodule.<path>.update
#    = merge`` for every submodule. With this set, plain ``git
#    submodule update`` (the one git runs after ``git pull`` if you've
#    enabled ``submodule.recurse``) merges upstream into your local
#    branch instead of detaching HEAD onto the gitlink commit. Local
#    work is preserved.
#
# Branch selection rule
# ---------------------
#
# Pin to whatever branch is currently checked out in each submodule.
# That's the user's intent: "this is the line of development I'm on
# for this submodule." Skip submodules that are in detached HEAD
# (cannot infer intent) or have no upstream (can't ``--remote``-fetch
# without one). Print clear remediation hints for the skipped cases.
#
# After running this once and committing ``.gitmodules``, day-to-day
# flow becomes:
#
#   - work inside a submodule on its pinned branch (commit, push)
#   - ``make pull-submodules`` to fast-forward all submodule branches
#   - ``make push-submodules`` to publish unpushed submodule commits
#   - bump the parent gitlink with ``git add submodules/<name>`` only
#     when you want to record a specific commit in the parent (e.g.,
#     before a release tag or for reproducibility of a paper run).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

mapfile -t SUBMODULES < <(
  git config --file .gitmodules --get-regexp 'submodule\..*\.path' \
    | awk '{print $2}' \
    | sort
)

if [ ${#SUBMODULES[@]} -eq 0 ]; then
  echo "No submodules registered in .gitmodules."
  exit 0
fi

n_pinned=0
n_already=0
n_skipped=0
declare -a skipped_reasons=()

for sm in "${SUBMODULES[@]}"; do
  if [ ! -d "$sm/.git" ] && [ ! -f "$sm/.git" ]; then
    skipped_reasons+=("$sm: not initialized (run 'make pull-submodules' first)")
    ((n_skipped++)) || true
    continue
  fi

  # Skip detached HEADs — there's no branch to pin to.
  if ! branch=$(git -C "$sm" symbolic-ref --short HEAD 2>/dev/null); then
    sha=$(git -C "$sm" rev-parse --short HEAD)
    skipped_reasons+=(
      "$sm: detached HEAD at $sha — switch to a branch first, e.g. 'git -C $sm switch <branch>'"
    )
    ((n_skipped++)) || true
    continue
  fi

  # Skip branches with no upstream — ``git submodule update --remote``
  # can't fetch the branch tip without knowing where it lives.
  if ! git -C "$sm" rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
    skipped_reasons+=(
      "$sm: branch '$branch' has no upstream — set one with 'git -C $sm push -u origin $branch'"
    )
    ((n_skipped++)) || true
    continue
  fi

  # The .gitmodules key is keyed by the submodule path, not its name.
  # Both happen to match in this repo, but be explicit.
  current=$(git config --file .gitmodules --get "submodule.$sm.branch" 2>/dev/null || echo "")
  if [ "$current" = "$branch" ]; then
    echo "  unchanged: $sm  (already pinned to '$branch')"
    ((n_already++)) || true
  else
    git config --file .gitmodules "submodule.$sm.branch" "$branch"
    if [ -n "$current" ]; then
      echo "  re-pinned: $sm  '$current' -> '$branch'"
    else
      echo "  pinned:    $sm  -> '$branch'"
    fi
    ((n_pinned++)) || true
  fi

  # Tell the parent repo to use 'merge' as the update strategy so
  # ``git submodule update`` doesn't detach. This lives in
  # .git/config (per-clone), not .gitmodules — it is not shared.
  git config "submodule.$sm.update" merge
done

echo
echo "------------------------------------------------------------"
echo "Summary: $n_pinned pinned/updated, $n_already already pinned, $n_skipped skipped."
if [ ${#skipped_reasons[@]} -gt 0 ]; then
  echo
  echo "Skipped:"
  for reason in "${skipped_reasons[@]}"; do
    echo "  - $reason"
  done
fi

if [ "$n_pinned" -gt 0 ]; then
  echo
  echo "Next: review and commit the .gitmodules change:"
  echo "  git diff .gitmodules"
  echo "  git add .gitmodules"
  echo "  git commit -m 'submodules: pin each to its development branch'"
fi
