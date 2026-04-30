#!/usr/bin/env bash
# Read-only summary of every submodule: branch / detached HEAD,
# ahead/behind upstream, dirty state. No mutations.
#
# Used by ``make submodule-status``, and as the diagnostic step at the
# top of ``push-submodules.sh`` so the user can see what they're about
# to act on.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# List submodule paths from .gitmodules so the script doesn't hard-code
# them — adding a new submodule is picked up automatically.
mapfile -t SUBMODULES < <(
  git config --file .gitmodules --get-regexp 'submodule\..*\.path' \
    | awk '{print $2}' \
    | sort
)

if [ ${#SUBMODULES[@]} -eq 0 ]; then
  echo "No submodules registered in .gitmodules."
  exit 0
fi

printf "%-30s %-40s %-12s %-5s %s\n" \
  "submodule" "branch" "ahead/behind" "dirty" "origin (resolved)"
printf "%-30s %-40s %-12s %-5s %s\n" \
  "---------" "------" "------------" "-----" "-----------------"

for sm in "${SUBMODULES[@]}"; do
  if [ ! -d "$sm/.git" ] && [ ! -f "$sm/.git" ]; then
    printf "%-30s %-40s %-12s %-5s %s\n" \
      "$sm" "(not initialized)" "-" "-" "-"
    continue
  fi

  # Branch name, or "(detached)" if HEAD isn't on a branch.
  branch=$(git -C "$sm" symbolic-ref --short HEAD 2>/dev/null \
    || echo "(detached)")

  # Ahead / behind counts vs the configured upstream, when one exists.
  if [ "$branch" != "(detached)" ] \
      && git -C "$sm" rev-parse --abbrev-ref --symbolic-full-name '@{u}' \
         >/dev/null 2>&1; then
    ahead=$(git -C "$sm" rev-list --count '@{u}..HEAD')
    behind=$(git -C "$sm" rev-list --count 'HEAD..@{u}')
    ab="$ahead/$behind"
  else
    ab="(no upstream)"
  fi

  # Dirty = uncommitted tracked changes OR untracked files.
  if [ -n "$(git -C "$sm" status --porcelain 2>/dev/null)" ]; then
    dirty="yes"
  else
    dirty="no"
  fi

  # Resolved origin URL — uses ``ls-remote --get-url`` so any
  # ``insteadOf`` / ``pushInsteadOf`` rewrites visible to this clone's
  # git are applied. That's the URL ``git fetch`` would actually
  # contact, which is the question users care about when triaging
  # auth or wrong-server issues.
  origin_url=$(git -C "$sm" ls-remote --get-url origin 2>/dev/null \
    || echo "(no origin)")

  printf "%-30s %-40s %-12s %-5s %s\n" \
    "$sm" "$branch" "$ab" "$dirty" "$origin_url"
done
