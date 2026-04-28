# Open-Model HELM Reproduction Master Plan

Last updated: 2026-04-16 UTC

Purpose: reorient quickly, recover the real state of the project, and drive it to a publishable end state where we can say, with evidence, which open-weight HELM experiments we reproduced, on which machines, how closely they match the official benchmark outputs, and where they do not.

This is an operational planning document, not paper prose. It is intentionally concrete.

## 1. Executive Summary

The most important current fact is that we already have a credible positive result, but it is narrower than the total scope we eventually want to claim. The strongest evidence is a Vicuna 7B subset where disabling an incorrect chat-template path (`apply_chat_template: false`) makes local results line up closely with official HELM outputs on `boolq`, `mmlu:subject=us_foreign_policy`, and `narrative_qa`. That positive result appears to hold across same-machine repeats, at least one cross-machine comparison, and official-vs-local comparisons.

The second most important fact is that the broad project state is still under-analyzed. The latest checked-in aggregate inventory shows:

- 13,579 discovered public HELM runs
- 270 selected open-model runs after Stage 1 filtering
- 7 selected open-weight model families
- 487 total local portfolio rows in the refreshed aggregate summary
- 244 completed with run artifacts
- 115 analyzed
- 129 completed but not yet analyzed
- 243 failed or incomplete

That means the likely publishable story is not yet “we reproduced everything.” The publishable story is more plausibly:

1. We can reproduce a well-defined open-weight HELM subset when the deployment recipe is faithful.
2. We can explain a large fraction of the apparent failures as recipe, infrastructure, dataset, judge, or environment mismatches.
3. We can characterize the remaining genuine drift by benchmark and model family.
4. We can export or map the results into a broader evaluation ecosystem, likely including Every Eval Ever (EEE), once our internal representation is stabilized.

The best next move is to formalize the paper subset, burn down analysis debt, and only then expand to more model families.

## 2. Current Authoritative State

### 2.1 Strongest existing positive evidence

The current strongest evidence is centered on Vicuna 7B with the no-chat-template override:

- `boolq:model=lmsys/vicuna-7b-v1.3,data_augmentation=canonical`
- `mmlu:subject=us_foreign_policy,method=multiple_choice_joint,model=lmsys/vicuna-7b-v1.3,data_augmentation=canonical`
- `narrative_qa:model=lmsys/vicuna-7b-v1.3,data_augmentation=canonical`

Important files:

- `docs/helm-reproduction-status-checkpoint.md`
- `docs/helm-reproduction-agent-brief.md`
- `docs/helm-reproduction-research-journal.md`
- `configs/debug/vicuna_no_chat_template.yaml`

Important confirmed points:

- Same-machine repeats for the repaired Vicuna subset are effectively identical.
- Official-vs-local agreement is very high on the three key runs above.
- Cross-machine pair reports exist and are also very strong, though they still show mild bookkeeping / metric drift instead of strict byte-for-byte equality.

### 2.2 Current broad portfolio state

The refreshed Stage 6 aggregate summary now indicates:

- `audit-historic-grid`: 286 total, 141 completed, 85 analyzed, 56 completed-but-not-analyzed, 145 failed/incomplete
- `audit-qwen25-7b-aiq`: 137 total, 65 completed, 19 analyzed, 46 completed-but-not-analyzed, 72 failed/incomplete
- `audit-vicuna-nochat-overnight`: 3 completed, 3 analyzed
- `audit-vicuna-nochat-server`: 3 completed, 3 analyzed
- `audit-yardrat-subset`: 3 completed, 3 analyzed
- `audit-namek-subset`: 2 completed, 2 analyzed, 1 failed

Important nuance: the all-results aggregate summary is now much less stale for both the small Vicuna subset experiments and the comparable portion of the Qwen backlog, but the dominant analysis debt still sits in `audit-historic-grid` and the non-overlapping portion of `audit-qwen25-7b-aiq`.

### 2.3 Stage 1 selected model scope

The currently selected open-weight model set is:

- `eleutherai/pythia-1b-v0`
- `eleutherai/pythia-2.8b-v0`
- `eleutherai/pythia-6.9b`
- `lmsys/vicuna-7b-v1.3`
- `qwen/qwen2-72b-instruct`
- `qwen/qwen2.5-72b-instruct-turbo`
- `qwen/qwen2.5-7b-instruct-turbo`

Important implication: the actual paper claim should distinguish between “selected in principle by our open-model filter” and “executed, analyzed, and reproduced in practice.”

### 2.4 Historic-grid drift distribution

Among the 115 analyzed rows currently represented in the refreshed aggregate:

- 42 are `high_agreement_0.95+`
- 30 are `moderate_agreement_0.80+`
- 22 are `low_agreement_0.00+`
- 17 are `exact_or_near_exact`

Model-level analyzed evidence across the refreshed aggregate:

- `lmsys/vicuna-7b-v1.3`: 50 analyzed rows
  - 29 high
  - 10 moderate
  - 3 low
  - 8 exact/nearly exact
- `eleutherai/pythia-6.9b`: 41 analyzed rows
  - 16 high
  - 15 moderate
  - 7 low
  - 1 exact/nearly exact
- `lmsys/vicuna-7b-v1.3` also now has 2 additional `synthetic_reasoning_natural` reports in Stage 6 that are counted as analyzed artifacts but still lack aggregate agreement values.
- `eleutherai/pythia-6.9b` likewise now has 2 additional `synthetic_reasoning_natural` reports in Stage 6 without aggregate agreement values.
- `qwen/qwen2.5-7b-instruct-turbo`: 19 analyzed rows
  - 1 high
  - 5 moderate
  - 12 low
  - 1 exact/nearly exact
- `eleutherai/pythia-2.8b-v0`: 3 analyzed rows, all high

Lowest-agreement regions cluster around:

- `entity_matching`
- `wikifact`
- the newly analyzed Qwen overlap set, especially `narrative_qa`, `wmt_14`, and several `legalbench` subsets

These are prime candidates for the “where and why” section of the paper because they let us contrast a strong-positive Vicuna story with a materially divergent Qwen story.

### 2.5 Main current failure families

From the refreshed aggregate inventory:

- `truncated_or_incomplete_runtime`: 123 across the overall portfolio
- `missing_math_dataset`: 35
- `remote_dataset_download_failure`: 22
- `unknown_failure`: 25
- `missing_runtime_log`: 11
- `missing_openai_annotation_credentials`: 14
- `missing_dataset_or_cached_artifact`: 6
- `gated_dataset_access`: 5
- `network_or_remote_service_failure`: 2

For `audit-historic-grid` specifically:

- `truncated_or_incomplete_runtime`: 62
- `missing_math_dataset`: 28
- `unknown_failure`: 22
- `remote_dataset_download_failure`: 20
- `missing_openai_annotation_credentials`: 6
- `missing_dataset_or_cached_artifact`: 5
- `gated_dataset_access`: 2

This is a strong sign that a large portion of apparent non-reproduction is operational, not scientific.

## 3. What Is Already Established vs. What Is Not

### 3.1 Established enough to build on

- A corrected local deployment recipe matters, especially chat-template handling.
- Vicuna 7B can be reproduced on at least a small, high-value HELM subset.
- Same-machine repeat noise is not the dominant explanation for the older failures.
- Cross-machine reproduction is plausible for at least the Vicuna paper subset.
- The Stage 1 filter is already restrictive and useful; it narrows the public HELM universe to a manageable open-model target set.
- Qwen and GPT-OSS local runbooks exist, so those are not greenfield tasks.

### 3.2 Not yet established enough to claim publicly

- A final canonical “paper subset” manifest with explicit inclusion / exclusion language.
- A refreshed aggregate summary that also clears the remaining historic-grid and Qwen analysis debt.
- A second model family with evidence as strong as Vicuna no-chat.
- An end-to-end accounting of all completed-but-not-analyzed runs.
- A principled divergence taxonomy for `entity_matching`, `wikifact`, and other low-agreement pockets.
- A clean bridge from HELM audit outputs into an EEE-style export.
- A paper-ready methods section describing tolerance thresholds, repeat logic, and scope exclusions.

## 4. Recommended Publishable Claim

### Minimum publishable claim

We can reproducibly recover a meaningful subset of open-weight HELM benchmark results when the local deployment recipe is faithful to the original model and tokenizer behavior, and much of the remaining disagreement can be decomposed into configuration, environment, data-access, or benchmark-dependency issues rather than intrinsic irreproducibility.

### Target claim

We can reproduce a clearly defined subset of open-weight HELM runs across multiple machines, quantify agreement against official outputs at several tolerance levels, identify which benchmarks are robust vs. fragile, and explain the major failure families that block broader reproduction.

### Stretch claim

We can recover most of the Stage 1 selected open-model HELM surface, across multiple model families, and emit the results in a normalized format that interoperates with EEE.

Recommendation: do not make the stretch claim the planning bottleneck. The minimum and target claims are already interesting and much more achievable.

## 5. Concrete End State We Should Drive Toward

The project is “done enough to publish” when all of the following exist:

- A checked-in canonical scope document saying exactly which open-model HELM runs are in paper scope.
- A canonical paper manifest or small family of manifests for the reproduced subset.
- One refreshed aggregate summary that includes the repaired Vicuna subset, historic-grid, and at least one additional model-family expansion.
- A cross-machine reproducibility matrix for the paper subset.
- A divergence analysis for the low-agreement benchmarks.
- A failure taxonomy that cleanly separates:
  - scope exclusions
  - infrastructure failures
  - dataset access failures
  - judge / annotation dependency failures
  - recipe mismatches
  - residual output drift
- A table or export that can be mapped into Every Eval Ever.
- A paper outline, figure inventory, and appendix inventory.

## 6. Prioritization Principle

The best order is:

1. Consolidate the strongest positive claim.
2. Burn analysis debt.
3. Formalize the scope and failure taxonomy.
4. Add one more model family convincingly.
5. Only then chase breadth.

The reason is simple: a narrow, well-supported paper beats a broad but unstable one.

## 7. Workstreams

## 7A. Freeze Scope and Define the Canonical Paper Subset

Goal: stop treating “all Stage 1 selected runs” as the only success criterion.

Tasks:

- Write a formal scope note that splits runs into:
  - `paper_core`
  - `paper_secondary`
  - `excluded_for_now`
- Use the existing Vicuna no-chat subset as the initial `paper_core`.
- Decide whether Pythia belongs in `paper_core` as positive evidence, or in `paper_secondary` as contrastive / partial evidence.
- Decide whether Qwen belongs in paper scope now, or only after the current 65 completed Qwen runs are analyzed.
- Explicitly exclude closed-judge and gated-dataset benchmarks from the default open-model reproduction claim.

Deliverables:

- A checked-in manifest or manifest family for `paper_core`.
- A table mapping each candidate run entry to one of:
  - reproduced
  - analyzable but not yet analyzed
  - failed due to infrastructure
  - out of scope by policy
  - unresolved

Success condition:

- Tomorrow or next week, anyone can answer “what exactly are we claiming to have reproduced?” in one place.

## 7B. Burn Down Analysis Debt

Goal: move the project from “many runs completed” to “many runs interpreted.”

Current debt:

- 129 completed-with-run-artifacts rows are not yet analyzed in the refreshed portfolio summary.
- The most important debt buckets are:
  - `audit-historic-grid`: 56 completed-not-analyzed after repairing the `synthetic_reasoning_natural` empty-metric report path
  - `audit-qwen25-7b-aiq`: 46 completed-not-analyzed after the first targeted rebuild pass
  - `audit-gpt-oss-20b-vllm-smoke`: 2 completed, 0 analyzed
  - `audit-historic-grid-gpt-oss-20b-vllm`: 2 completed, 0 analyzed

Recommended order:

1. `audit-historic-grid` remaining completion debt
2. `audit-qwen25-7b-aiq` remaining comparable backlog
3. `audit-gpt-oss-20b-vllm-smoke`
4. `audit-historic-grid-gpt-oss-20b-vllm`
5. refresh aggregate summary

Reasoning:

- The small Vicuna subset experiments are now represented in Stage 6, so the highest-value remaining work is breadth and debt retirement.
- Historic-grid gives breadth and failure taxonomy.
- Qwen is now a stronger paper candidate because 19 overlap rows are already analyzed and many of them are genuinely low-agreement rather than merely missing.
- Historic-grid still contains a large no-candidate remainder, so “completed but not analyzed” is now partly a backlog concept and partly a comparability concept.

Success condition:

- The “completed-not-analyzed” count is close to zero for all experiments we consider in paper scope.

## 7C. Cross-Machine Reproducibility Matrix

Goal: show this is not a single-host fluke.

Current evidence:

- Strong cross-machine pair reports already exist for the repaired Vicuna subset.
- `yardrat` has all three target runs completed.
- `namek` has two completed and one incomplete.

Next tasks:

- Rebuild or formalize per-experiment analysis for:
  - `audit-yardrat-subset`
  - `audit-namek-subset`
  - `audit-vicuna-nochat-overnight`
  - `audit-vicuna-nochat-server`
- Normalize machine provenance so host labels are not ambiguous.
- Investigate why `audit-vicuna-nochat-server` appears with `aiq-gpu` host labeling in the checked-in inventory.
- For each paper-core run, preserve:
  - same-machine repeat
  - official-vs-local
  - cross-machine pair(s)

Success condition:

- Every run in `paper_core` has at least one clean cross-machine comparison in the reporting layer.

## 7D. Expand Beyond Vicuna

Goal: make the paper about open-model HELM reproduction, not only one repaired Vicuna recipe.

Recommended order:

1. Qwen 2.5 7B instruct turbo
2. Pythia 6.9B
3. Pythia 2.8B
4. Qwen 2 72B
5. Qwen 2.5 72B
6. Pythia 1B
7. GPT-OSS 20B as an optional appendix / extension

Why this order:

- Qwen 2.5 7B already has a large completed execution footprint.
- Pythia 6.9B already has mixed analyzed evidence, which makes it useful for the “where it fails” part of the paper.
- The 72B families are higher-cost and should only be prioritized once the analysis and claims around smaller models are stable.

Important policy:

- Prepared runbooks do not count as reproduced evidence.
- A family enters the paper only once we have analyzed results, not merely runnable configs.

## 7E. Divergence Analysis and Failure Taxonomy

Goal: quantify differences from official HELM and explain them.

Primary targets:

- `entity_matching`
- `wikifact`
- any benchmark with `official_instance_agree_0 < 0.80`

Questions to answer per low-agreement case:

- Is the drift output-format-related, or semantic?
- Is the tokenizer path aligned with the official model identity?
- Is prompt formatting identical?
- Is a hidden chat template or deployment wrapper changing behavior?
- Is the official run using a slightly different upstream artifact or benchmark version?
- Is there a metric sensitivity issue where small textual changes cause large measured changes?
- Is the scenario intrinsically high variance for this model family?

Deliverables:

- A per-benchmark divergence note for each low-agreement cluster.
- A compact taxonomy table: benchmark, model, failure mode, evidence, likely fixability.

Important insight already visible:

- Low-agreement rows cluster in a small number of benchmarks rather than being evenly distributed.
- That is favorable for a publishable analysis because it means we can present concrete case studies instead of diffuse hand-waving.

## 7F. Reporting and Schema Cleanup

Goal: make the reporting layer something we can trust as the paper source of truth.

Recommended cleanup items:

- Treat `run_inventory_*.csv` as the main machine-readable summary artifact.
- De-emphasize or replace older text exports whose CSV shape is difficult to parse robustly.
- Make sure smaller subset experiments show up coherently in aggregate summary views.
- Improve diagnosis language so “deployment_drift” is not the only story told for all official-vs-local deltas.
- Preserve machine provenance and alias normalization carefully.

One useful new helper already added:

- `python -m eval_audit.cli.portfolio_status`
- `python -m eval_audit.cli.analyze_many`

This command reads the checked-in aggregate run inventory and gives a concise status report even on a machine that does not currently have the `/data` run trees mounted.
The second command runs multiple experiment analyses in one Python process so the cached official-run index is reused instead of rebuilt for every experiment.

## 7G. EEE Integration

Goal: prepare a path from HELM audit outputs into the Every Eval Ever ecosystem.

Current relevant fact:

- The EEE repository already contains a HELM converter entry point under `eval_converters/helm.py`.

Practical interpretation:

- We should not invent a bespoke new results schema unless necessary.
- We should likely build a thin adapter that maps our HELM audit artifacts into the representation expected by EEE, while preserving our reproduction-specific metadata as extra fields.

Recommended approach:

1. Inspect EEE’s HELM converter assumptions carefully.
2. Decide which boundary to export from:
   - raw HELM hydrated run specs
   - our analyzed run inventory
   - our per-run core report JSON
3. Prefer exporting from our analyzed summary layer plus stable references to raw HELM artifacts.
4. Add reproduction metadata that EEE will not know by default:
   - comparison type
   - repeat agreement
   - official-vs-local agreement
   - cross-machine agreement
   - failure taxonomy
   - scope status

Recommended eventual code location:

- `eval_audit/integrations/eee_export.py`

Recommended first validation:

- Export only the repaired Vicuna paper subset into a tiny EEE-compatible sample bundle.

Success condition:

- We can hand the Every Eval Ever team a compact export without re-explaining our internal report tree.

## 7H. Paper Packaging

Goal: make the project legible as a paper, not just as a pile of reports.

Recommended paper structure:

1. Motivation
2. Scope: what counts as an open-model HELM reproduction target
3. Reproduction protocol
4. Agreement metrics and tolerance-based comparison
5. Results:
   - reproduced subset
   - cross-machine results
   - divergence / failure taxonomy
6. Case studies:
   - Vicuna no-chat repair
   - Qwen or Pythia family
   - low-agreement benchmarks
7. Export / interoperability with EEE
8. Limitations

Likely key figures:

- Stage 1 filter funnel
- end-to-end execution / analysis funnel
- model-by-benchmark agreement heatmap
- cross-machine agreement table
- failure taxonomy bar chart

Likely key tables:

- paper-core run inventory
- per-model agreement bucket counts
- low-agreement benchmark case table
- infrastructure vs. scientific failure families

## 8. Tomorrow Morning Quick Start

Read these in order:

1. `docs/open-model-helm-reproduction-master-plan.md`
2. `docs/helm-reproduction-status-checkpoint.md`
3. `docs/helm-reproduction-agent-brief.md`
4. `docs/helm-reproduction-research-journal.md`

Then run:

```bash
python -m eval_audit.cli.portfolio_status
python -m eval_audit.cli.portfolio_status --experiment-name audit-historic-grid
```

This gives the fastest current-state refresh from checked-in artifacts.

## 9. Machine-State Branch: What To Do Depending On Where You Are

## 9A. If the machine has live result roots

You should have both:

- `/data/crfm-helm-audit`
- `/data/crfm-helm-audit-store`

Sanity check:

```bash
ls -la /data/crfm-helm-audit | head
ls -la /data/crfm-helm-audit-store | head
```

If those exist, the next execution sequence should be:

```bash
export AUDIT_RESULTS_ROOT="${AUDIT_RESULTS_ROOT:-/data/crfm-helm-audit}"
export AUDIT_STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"

eval-audit-index \
  --results-root "$AUDIT_RESULTS_ROOT" \
  --report-dpath "$AUDIT_STORE_ROOT/indexes"

eval-audit-analyze-experiment \
  --experiment-name audit-vicuna-nochat-overnight \
  --index-dpath "$AUDIT_STORE_ROOT/indexes" \
  --allow-single-repeat

eval-audit-analyze-experiment \
  --experiment-name audit-vicuna-nochat-server \
  --index-dpath "$AUDIT_STORE_ROOT/indexes" \
  --allow-single-repeat

eval-audit-analyze-experiment \
  --experiment-name audit-yardrat-subset \
  --index-dpath "$AUDIT_STORE_ROOT/indexes" \
  --allow-single-repeat

eval-audit-analyze-experiment \
  --experiment-name audit-namek-subset \
  --index-dpath "$AUDIT_STORE_ROOT/indexes" \
  --allow-single-repeat

python -m eval_audit.workflows.build_reports_summary \
  --index-dpath "$AUDIT_STORE_ROOT/indexes" \
  --filter-inventory-json "$AUDIT_STORE_ROOT/analysis/filter_inventory.json"
```

Then rerun:

```bash
python -m eval_audit.cli.portfolio_status
```

At that point decide whether to proceed immediately to:

- remaining `audit-historic-grid` analysis debt
- `audit-qwen25-7b-aiq`

## 9B. If the machine only has the repo and checked-in reports

This was the earlier state of the checkout before the `/data` mounts were repaired.

Observed blocker:

- `/data/crfm-helm-audit` is effectively empty here
- `/data/crfm-helm-audit-store` is absent here

That means:

- checked-in reports can be read
- planning can proceed
- code refactors can proceed
- fresh analysis rebuilds cannot proceed

If you land on a machine in this state, do one of:

- move to the machine where the run trees live
- rsync / mount the result roots
- or explicitly restrict yourself to reporting / planning / schema work

## 10. Recommended Execution Order for the Next Few Sessions

### Session 1: Consolidate the current truth

- Run `python -m eval_audit.cli.portfolio_status`
- Verify machine state
- If live data exists, analyze the four small Vicuna subset experiments
- Rebuild aggregate summary
- Update this plan with the new counts

### Session 2: Formalize the paper subset

- Decide `paper_core` vs `paper_secondary`
- Check in a canonical manifest
- Write a scope note describing exclusions and rationale

### Session 3: Historic-grid completion and drift taxonomy

- Burn down remaining `audit-historic-grid` completed-not-analyzed rows
- Create a benchmark-level drift table
- Start case studies for `entity_matching` and `wikifact`

### Session 4: Qwen expansion

- Continue targeted analysis of the remaining comparable `audit-qwen25-7b-aiq` runs
- Triage failures into infrastructure vs genuine incompatibility
- Decide whether Qwen enters the paper or remains future work

### Session 5: EEE bridge

- Inspect the HELM converter in EEE
- Implement a minimal export adapter
- Validate it on the repaired Vicuna subset

### Session 6: Writing pass

- Produce paper figures and tables
- Draft methods
- Draft limitations and failure taxonomy

## 11. Specific Open Questions

- Should the paper scope be “all Stage 1 selected runs,” or “the reproduced and analyzable subset of Stage 1 selected runs”?
- Is Vicuna plus one additional family enough for the first paper, or do we want broader but shallower coverage?
- Do we want Pythia framed as a partial success story or primarily as a drift / fragility case study?
- Why does `audit-vicuna-nochat-server` appear with `aiq-gpu` provenance in the current checked-in inventory?
- Are the low-agreement `wikifact` rows mostly prompt-format issues, output-format issues, or genuinely unstable knowledge behavior?
- How much of Qwen’s failure mass is salvageable with better infra vs. benchmark-scope exclusions?
- What exact metadata does the EEE converter need that we do not already emit?

## 12. Refactors Worth Doing Soon

These are not mandatory for the science, but they will make the project much easier to operate.

- Add a stable scope manifest for `paper_core`.
- Add an EEE export module under `eval_audit/integrations/`.
- Normalize machine provenance and host aliases.
- Make Stage 6 aggregate summary the obvious source of truth for small follow-up experiments, not only historic-grid.
- Preserve richer diagnosis labels for official-vs-local drift.
- Keep the new `portfolio_status` helper evolving as the one-command orientation tool.

## 13. Notes From Tonight’s Pass

- The repo has enough reporting artifacts to reconstruct the strategic state.
- The repo now has the mounted `/data` artifacts needed to rebuild the analysis stack from scratch.
- The strongest current path to a paper is narrower and more defensible than “everything open-weight in HELM.”
- The strongest immediate technical priority is still not more runs; it is better synthesis of the runs already completed, especially `audit-historic-grid` and Qwen.
- Qwen should probably be the first expansion family after Vicuna because there is already significant execution volume waiting to be analyzed, and the first 19 analyzed overlap rows already show substantial drift.
- The `synthetic_reasoning_natural` family no longer crashes report generation; four previously blocked historic-grid rows now publish Stage 6 reports, although they still surface as agreement-missing in the portfolio summary.

## 14. Minimal Deliverable vs. Better Deliverable

### Minimal deliverable

- Vicuna paper subset fully analyzed
- cross-machine confirmation
- clear failure taxonomy
- paper outline

### Better deliverable

- all of the above
- one Qwen family analyzed
- historic-grid debt mostly cleared
- EEE export prototype

### Best realistic deliverable

- all of the above
- a stable, refreshable summary pipeline where tomorrow’s reports no longer feel stale

## 15. Practical Command Cheatsheet

Current repo-only orientation:

```bash
python -m eval_audit.cli.portfolio_status
python -m eval_audit.cli.portfolio_status --experiment-name audit-historic-grid
```

Failure triage for a synced result root:

```bash
python -m eval_audit.cli.summarize_experiment_failures /data/crfm-helm-audit/audit-qwen25-7b-aiq
python -m eval_audit.cli.summarize_experiment_failures /data/crfm-helm-audit/audit-historic-grid
```

Analysis-only rebuild once the result roots are present:

```bash
export AUDIT_RESULTS_ROOT=/data/crfm-helm-audit
export AUDIT_STORE_ROOT=/data/crfm-helm-audit-store

eval-audit-index \
  --results-root "$AUDIT_RESULTS_ROOT" \
  --report-dpath "$AUDIT_STORE_ROOT/indexes"

eval-audit-analyze-experiment \
  --experiment-name audit-historic-grid \
  --index-dpath "$AUDIT_STORE_ROOT/indexes" \
  --allow-single-repeat

python -m eval_audit.workflows.build_reports_summary \
  --index-dpath "$AUDIT_STORE_ROOT/indexes" \
  --filter-inventory-json "$AUDIT_STORE_ROOT/analysis/filter_inventory.json"
```

Multi-experiment refresh in one cached Python process:

```bash
python -m eval_audit.cli.analyze_many \
  --index-fpath /data/crfm-helm-audit-store/indexes/audit_results_index_20260416T184216Z.csv \
  --allow-single-repeat \
  --experiment-name audit-vicuna-nochat-overnight \
  --experiment-name audit-vicuna-nochat-server \
  --experiment-name audit-yardrat-subset \
  --experiment-name audit-namek-subset \
  --build-summary \
  --filter-inventory-json /data/crfm-helm-audit-store/analysis/filter_inventory.json
```

Targeted backlog rebuild for only the completed-but-not-yet-analyzed rows:

```bash
python -m eval_audit.cli.analyze_backlog \
  --index-fpath /data/crfm-helm-audit-store/indexes/audit_results_index_20260416T184216Z.csv \
  --run-inventory-csv /home/joncrall/code/helm_audit/reports/aggregate-summary/all-results/run_inventory.latest.csv \
  --allow-single-repeat \
  --experiment-name audit-qwen25-7b-aiq \
  --build-summary \
  --filter-inventory-json /data/crfm-helm-audit-store/analysis/filter_inventory.json
```

Observed result from the first Qwen targeted pass:

- 19 Qwen rows are now analyzed in Stage 6
- 46 Qwen rows remain completed-but-not-analyzed
- many `mmlu_clinical_afr`, `winogrande_afr`, and `bigcodebench` rows currently skip because no official historic HELM candidate is discoverable under the present matching logic
- the comparable Qwen overlap set is already scientifically interesting because most analyzed rows fall into `low_agreement_0.00+`

Observed result from the follow-up historic-grid targeted pass:

- 4 `synthetic_reasoning_natural` rows now publish Stage 6 reports
- historic-grid moved from 81 analyzed / 60 completed-not-analyzed to 85 analyzed / 56 completed-not-analyzed
- the remaining historic-grid backlog is still dominated by rows with no official historic HELM counterpart under the current matching logic
- `eval_audit/cli/analyze_backlog.py` now prints a grouped `SKIP_SUMMARY`, which should make the remaining backlog easier to classify quickly tomorrow
- `python -m eval_audit.cli.portfolio_status --experiment-name audit-historic-grid --classify-backlog` now makes that distinction explicit:
  - 4 rows have report artifacts but no scalar agreement value yet
  - 56 rows have no report and no official historic counterpart
  - 0 rows remain in the “no report but currently comparable” bucket

## 16. Final Recommendation

Treat the repaired Vicuna subset as the anchor claim, not as a side note. Tomorrow’s work should be organized around converting that anchor into a formal paper-core subset, making the reporting layer reflect it cleanly, and then deciding which second model family most efficiently upgrades the paper from a narrow positive result into a broader open-model HELM reproduction study.
