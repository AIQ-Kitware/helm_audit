#!/bin/bash
# Retry the residual TypeError rows on a machine with more RAM.
#
# As of 2026-04-25, after the EEE converter fix and the local UnknownError +
# SIGKILL_OOM retries, the only failures left in the EEE sweep DB that are
# NOT legit "missing media asset" infrastructure failures are 4 large
# natural_qa runs:
#
#   classic/v0.2.3/natural_qa:mode=openbook_longans,model=together_opt-175b
#   classic/v0.2.3/natural_qa:mode=openbook_longans,model=together_opt-66b
#   classic/v0.2.3/natural_qa:mode=openbook_longans,model=together_t5-11b,...
#   classic/v0.2.3/natural_qa:mode=openbook_longans,model=together_ul2,...
#
# Each one has a scenario_state.json of 731-984 MB and a per_instance_stats.json
# of 235-247 MB. msgspec/dacite parsing of these spikes RAM well above the
# 16 GB local budget; on a host with >=64 GB RAM they should convert fine
# under the patched HELM->EEE converter.
#
# Usage on the big machine:
#   bash retry-bigmem.sh                 # converts the 4 residual TypeErrors
#   bash retry-bigmem.sh --workers 4     # parallelize when RAM allows it
#
# The default --max-scenario-state-mb 1100 lifts the size cap above the
# largest residual run (984 MB). Bump it higher if you also want to
# convert any later, even-larger runs.
#
# To revisit the remaining FileNotFoundError rows (1120 image2struct/speech
# runs that need media assets), download the matching scenarios/ tree first,
# then run:
#   python dev/poc/eee-audit/sweep.py --retry-class FileNotFoundError --workers 4

set -euo pipefail

cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel 2>/dev/null || dirname "$(dirname "$(dirname "$0")")")"

WORKERS="${WORKERS:-2}"
TIMEOUT="${TIMEOUT:-1800}"
MAX_MB="${MAX_MB:-1100}"

# Forward any extra args so callers can override flags ad hoc.
exec python dev/poc/eee-audit/sweep.py \
    --retry-class TypeError \
    --workers "${WORKERS}" \
    --timeout "${TIMEOUT}" \
    --max-scenario-state-mb "${MAX_MB}" \
    "$@"
