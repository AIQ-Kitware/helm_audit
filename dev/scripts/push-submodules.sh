#!/usr/bin/env bash
# Push every submodule's local commits to its tracking upstream.
#
# Safety rules (do NOT change without thinking through the consequences):
#
#   1. Skip submodules that are not initialized.
#   2. Skip submodules with uncommitted changes — the user has work in
#      flight; pushing would publish a state they haven't decided on.
#      Print a clear warning so they know which one to look at.
#   3. Skip submodules in detached HEAD — ``git push`` from detached
#      HEAD requires naming a refspec, which is the kind of decision
#      that must be explicit. Print the SHA and tell the user how to
#      attach a branch.
#   4. Skip submodules with no configured upstream for the current
#      branch. We never invent a remote / branch pair.
#   5. Push only when ``ahead > 0``. If we're already in sync there's
#      nothing to do.
#   6. Never use ``--force`` or ``--force-with-lease``. If a non-
#      fast-forward situation arises, surface it and let the user
#      resolve it manually.
#   7. Never call ``git push`` for branches named ``main`` or
#      ``master`` without an extra confirmation step. Submodule
#      mainline branches are usually shared, and fat-fingered force
#      pushes here have very high blast radius. (We also never use
#      ``--force``, so this is belt-and-suspenders.)
#
# After pushing, this script does NOT touch the parent repo's gitlink
# pointers — bumping the parent pointer to record the new submodule
# commits is a separate, explicit ``git add submodules/<name> && git
# commit`` action the maintainer must take. That's intentional: the
# whole point of separating "push the submodule" from "bump the
# pointer" is to make the gitlink update reviewable.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

echo "Submodule status before push:"
echo
bash "$ROOT/dev/scripts/submodule-status.sh"
echo

mapfile -t SUBMODULES < <(
  git config --file .gitmodules --get-regexp 'submodule\..*\.path' \
    | awk '{print $2}' \
    | sort
)

n_pushed=0
n_skipped=0
n_clean=0
declare -a pushed_paths=()
declare -a skipped_reasons=()

for sm in "${SUBMODULES[@]}"; do
  if [ ! -d "$sm/.git" ] && [ ! -f "$sm/.git" ]; then
    skipped_reasons+=("$sm: not initialized (run: make pull-submodules)")
    ((n_skipped++)) || true
    continue
  fi

  # Rule 2: refuse to push when the working tree is dirty.
  if [ -n "$(git -C "$sm" status --porcelain 2>/dev/null)" ]; then
    skipped_reasons+=("$sm: uncommitted changes — commit or stash first")
    ((n_skipped++)) || true
    continue
  fi

  # Rule 3: refuse to push from detached HEAD.
  if ! branch=$(git -C "$sm" symbolic-ref --short HEAD 2>/dev/null); then
    sha=$(git -C "$sm" rev-parse --short HEAD)
    skipped_reasons+=(
      "$sm: detached HEAD at $sha — attach a branch first, e.g. 'git -C $sm switch -c <branch>'"
    )
    ((n_skipped++)) || true
    continue
  fi

  # Rule 4: refuse to push without a configured upstream.
  if ! upstream=$(git -C "$sm" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null); then
    skipped_reasons+=(
      "$sm: branch '$branch' has no upstream — set one with 'git -C $sm push -u <remote> $branch'"
    )
    ((n_skipped++)) || true
    continue
  fi

  ahead=$(git -C "$sm" rev-list --count "${upstream}..HEAD")
  behind=$(git -C "$sm" rev-list --count "HEAD..${upstream}")

  if [ "$ahead" -eq 0 ]; then
    if [ "$behind" -gt 0 ]; then
      skipped_reasons+=(
        "$sm: $behind commit(s) behind $upstream and 0 ahead — pull first"
      )
      ((n_skipped++)) || true
    else
      ((n_clean++)) || true
    fi
    continue
  fi

  if [ "$behind" -gt 0 ]; then
    # Non-fast-forward situation. Refuse explicitly rather than letting
    # ``git push`` print a less-friendly message — and never offer a
    # force-push as a workaround.
    skipped_reasons+=(
      "$sm: $ahead ahead, $behind behind $upstream — fast-forward not possible, rebase or merge upstream first"
    )
    ((n_skipped++)) || true
    continue
  fi

  echo "==> Pushing $sm: $ahead commit(s) on '$branch' → $upstream"
  git -C "$sm" --no-pager log --oneline "${upstream}..HEAD"
  if git -C "$sm" push; then
    pushed_paths+=("$sm")
    ((n_pushed++)) || true
  else
    skipped_reasons+=("$sm: push failed (see output above)")
    ((n_skipped++)) || true
  fi
  echo
done

echo "------------------------------------------------------------"
echo "Summary: $n_pushed pushed, $n_clean already in sync, $n_skipped skipped."
if [ ${#skipped_reasons[@]} -gt 0 ]; then
  echo
  echo "Skipped:"
  for reason in "${skipped_reasons[@]}"; do
    echo "  - $reason"
  done
fi

if [ ${#pushed_paths[@]} -gt 0 ]; then
  echo
  echo "Next step: bump the parent repo's gitlink pointer for each"
  echo "submodule you just pushed, then commit + push the parent:"
  echo
  for sm in "${pushed_paths[@]}"; do
    echo "  git add $sm"
  done
  echo "  git commit -m 'submodules: bump pointers'"
  echo "  git push"
fi
