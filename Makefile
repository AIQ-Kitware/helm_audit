.PHONY: help configure-submodule-branches push-submodules pull-submodules submodule-status

help:
	@echo "helm_audit maintenance targets:"
	@echo
	@echo "  make configure-submodule-branches"
	@echo "                          One-time: pin every submodule to a branch"
	@echo "                          (rather than a commit) so day-to-day work"
	@echo "                          inside a submodule stays on a real branch"
	@echo "                          and 'pull-submodules' fast-forwards instead"
	@echo "                          of detaching HEAD. Writes 'branch=' entries"
	@echo "                          into .gitmodules; commit that change."
	@echo
	@echo "  make pull-submodules    Sync each submodule along its pinned branch"
	@echo "                          (fast-forward / merge upstream into local)."
	@echo "                          For submodules without a branch pin, falls"
	@echo "                          back to checking out the gitlink commit."
	@echo
	@echo "  make push-submodules    For each submodule that has local commits"
	@echo "                          ahead of its upstream, push them. Reports"
	@echo "                          uncommitted changes and detached-HEAD"
	@echo "                          submodules without pushing. Never uses"
	@echo "                          --force."
	@echo
	@echo "  make submodule-status   Show ahead/behind, dirty state, and HEAD"
	@echo "                          for every submodule. Read-only."
	@echo

configure-submodule-branches:
	bash dev/scripts/configure-submodule-branches.sh

pull-submodules:
	bash dev/scripts/pull-submodules.sh

push-submodules:
	bash dev/scripts/push-submodules.sh

submodule-status:
	bash dev/scripts/submodule-status.sh
