# HELM Gotchas — undocumented or hard-to-see behaviors

A running ledger of HELM-specific behaviors we've hit while building the
audit pipeline. Each entry is short, names the symptom, the underlying
mechanism, and where we worked around it. Worth reading before the
NeurIPS EEE paper appendix is finalized; many of these are exactly the
kind of things a reviewer will ask about.

Append to this file as new gotchas surface; do not delete entries
(they're the institutional memory).

---

## G1. `run_spec.json` schema evolves silently between HELM releases

**Symptom.** Byte-for-byte hashing of `run_spec.json` produces 0/N
matches between local audit runs and public HELM rows that describe the
*same* recipe.

**Mechanism.** Newer HELM populates `adapter_spec` fields that older
HELM didn't write at all:

| field | new-HELM value | old-HELM behavior |
|---|---|---|
| `adapter_spec.chain_of_thought_prefix` | explicit `""` | absent (default `""`) |
| `adapter_spec.chain_of_thought_suffix` | explicit `"\n"` | absent (default `"\n"`) |
| `adapter_spec.global_suffix` | explicit `""` | absent (default `""`) |
| `adapter_spec.num_trials` | explicit `1` | absent (default `1`) |
| `adapter_spec.model_deployment` | explicit (e.g. `huggingface/eleutherai/pythia-6.9b`) | absent (defaulted to HF API) |
| top-level `metric_specs` | populated | absent or different shape |
| top-level `groups`, `annotators` | populated | absent |

A canonical-recipe hash that strips/defaults these fields produces
realistic match rates across releases.

**Workaround.** `helm_audit.virtual.coverage._canonical_recipe_hash`
computes a schema-collapsed hash; the coverage funnel reports both the
raw-hash match (= byte-for-byte identical) and the canonical-hash match
(= schema-collapsed). The gap between them is HELM-version churn; the
gap between canonical-hash and logical-key matches is real recipe drift.

---

## G2. The `suite` field on local audit runs is the experiment name, not a public-track version

**Symptom.** Versioned join (`logical_run_key + suite_version`) between
local and official rows produces 0 matches even when both refer to the
same logical run-spec.

**Mechanism.** HELM's `--suite` flag is whatever the operator passed at
run time. For public-track runs it's `v0.2.4` etc.; for local audits we
typically pass an experiment name like `audit-historic-grid`.

**Workaround.** The coverage funnel detects this (regex
`^v\d+\.\d+(\.\d+)?$` against the local-side `suite` field) and reports
`versioned_join_meaningful: False` so the summary shows `N/A` rather than
a misleading 0. Long-term fix: add a `target_suite_version` field to the
local audit index recording which public version the local run intended
to reproduce.

---

## G3. The `huggingface/<model>` deployment alias does not always exist in HELM

**Symptom.** Trying to rerun an open-weight HELM benchmark via
HuggingFace locally fails because HELM has no registered deployment
for the model.

**Mechanism.** Many public HELM rows were originally executed via the
HuggingFace API as `huggingface/<org>/<model>`. HELM's deployment
registry only ships YAMLs for a subset of these aliases. When the alias
is missing, attempting to run via that deployment errors out, and the
operator falls back to a different stack (vLLM, KubeAI, etc.) — which
introduces deployment-substitution drift.

**Workaround.** Register a custom deployment YAML pointing at a local
`LocalHuggingFaceClient` for the missing models (see G7 for the setup).
This lets the local rerun match the original deployment exactly.
Recommended for paper-quality reproducibility comparisons.

---

## G4. `adapter_spec.model_deployment` semantics differ across HELM versions

**Symptom.** Comparing `adapter_spec.model_deployment` between local
and official run_specs shows either `<MISSING> vs huggingface/<model>`
or `huggingface/<model> vs <MISSING>` for nearly every packet.

**Mechanism.** Older HELM didn't record the field (defaulted to "the HF
API"). Newer HELM does. So a local run with `huggingface/<model>`
written and an official run with the field absent are both "the HF
API" and semantically identical — but they hash differently.

**Workaround.** Treat as schema-evolution: drop the field from the
canonical recipe hash. Real serving-stack drift (e.g. `litellm/X` vs
`together/Y`) shows up in `adapter_spec.model_deployment` only when
both rows carry the field with different non-trivial values, which is
rare in our audit.

---

## G5. `adapter_spec.instructions` differs between HELM releases on identical scenarios

**Symptom.** Same scenario + same model + same data_augmentation, but
the prompt text differs.

**Mechanism.** Examples we've observed:

- MMLU: official prepends `Answer with only a single letter.\n\n` to
  the instructions; local doesn't.
- LegalBench: official prepends `Answer with only 'generic',
  'descriptive', 'suggestive', 'arbitrary' or 'fanciful'.\n\n`; local
  doesn't.
- Several legalbench scenarios show the same shape — official has a
  list-the-allowed-labels preamble, local doesn't.

This is a *real* recipe drift, not schema evolution. The model sees a
different prompt and produces measurably different output.

**Workaround.** None — this is genuine recipe disagreement. Surface it
in the per-packet `core_metric_management_summary.latest.txt` (already
done via the `same_instructions` comparability fact); enumerate
affected scenarios in the report.

---

## G6. The `classic` HELM corpus moved bucket prefixes

**Symptom.** `gs://crfm-helm-public/benchmark_output/runs/v0.2.4/...`
returns no objects; `--list-versions classic` produces an empty list.

**Mechanism.** HELM reorganized the public bucket: `classic` runs that
used to live at the bucket root under `benchmark_output/runs/<ver>/...`
now live at `gs://crfm-helm-public/classic/benchmark_output/runs/<ver>/...`,
mirroring every other benchmark suite.

**Workaround.** The `_runs_root('classic')` quirk in
`submodules/aiq-magnet/.../download_helm_results.py` was removed; classic
now resolves like every other benchmark.

---

## G7. HuggingFace API determinism: same weights ≠ same output

**Symptom.** Replaying the same scenario+model+adapter via the original
HF API and via local vLLM produces different outputs on a small
fraction of instances.

**Mechanism.** Even with greedy decoding, the two stacks differ in:

- sampling implementation (HF transformers' `.generate()` vs vLLM's
  custom kernels)
- tokenizer batching/chunking
- stop-sequence handling
- defaults for sampling parameters that aren't explicitly set in the
  scenario adapter (e.g. `repetition_penalty`)
- handling of EOS/padding tokens

Net effect: 3-6% per-instance flip rate on multiple-choice scorers,
much higher on long-form generation (~30-40%).

**Mitigation paths.**

1. **Best**: register a `huggingface/<model>` deployment in HELM that
   uses `LocalHuggingFaceClient` for the model. This replicates the
   original serving stack on local hardware.
2. **Cheap**: pin `temperature=0`, `top_p=1.0`, identical
   `max_tokens`, identical `stop` sequences in both stacks. Doesn't
   close the gap entirely (kernel differences still flip a few
   instances) but removes sampling-policy drift.
3. **Document, don't fix**: report the gap as "deployment_drift" and
   distinguish it from genuine model-correctness drift. This is what
   the case study currently does; it's the publishable narrative.

---

## G8. `metric_specs` schema evolution

**Symptom.** Top-level `metric_specs` differs between local and
official run_spec.json on 85/125 packets even when no other recipe
change is intended.

**Mechanism.** Newer HELM may rename or restructure `metric_specs`
entries (e.g. add scoring sub-metrics for legalbench). The field is
schema-evolving and not necessarily a recipe difference.

**Workaround.** Excluded from the canonical-recipe hash. If you want
to actually compare metrics, use the run-level metric output, not
the run_spec's `metric_specs` declaration.

---

## G9. Local index `model_deployment` doubles up the model name

**Symptom.** Some local audit rows have `model_deployment` like
`kubeai/vicuna-7b-v1-3-no-chat-template-local` — a deployment that
doesn't appear in the public HELM run_spec at all.

**Mechanism.** Local audits use custom deployments registered in the
audit's `model_deployments.yaml`. These names are stable but
audit-specific; the public HELM corpus uses different deployment names.

**Workaround.** None — this is by design. The `logical_run_key` join
collapses across deployments (matches on
`benchmark + model + augmentation + method`); the comparison's
`comparability_facts.same_deployment` correctly reports `no` when the
deployments differ.

---

## G10. Public-track `suite_version` is *not* the same as HELM release version

**Symptom.** A public run from `gs://crfm-helm-public/classic/.../v0.2.4/`
and another from `.../v0.3.0/` may have *identical* run_spec.json. The
`v0.2.4`/`v0.3.0` is the suite-tracking version, not the HELM release
version.

**Mechanism.** Public HELM publishes versioned snapshots of the
benchmark corpus. A given run_spec might appear in v0.2.4 and v0.3.0
unchanged because the suite tracks new model evaluations, not
necessarily new recipes for old models.

**Workaround.** Use `run_spec_hash` to identify recipe-identical
public-track versions of the same logical run.

---

## G11. `per_instance_stats.json` corruption on giant runs

**Symptom.** `every_eval_ever convert helm` fails with
`json.decoder.JSONDecodeError: Unterminated string starting at: line
57094485 column 23 (char 3644456680)` on certain msmarco runs.

**Mechanism.** Public HELM's `cohere_small-20220720` msmarco runs were
originally ~3.5 GB on disk and got truncated mid-write during the
upload to GCS (apparent based on consistent-byte-offset failures
across the v0.2.2/v0.2.3/v0.2.4 mirrors). Recently re-uploaded
versions are ~44 MB and parse cleanly.

**Workaround.** `dev/poc/eee-audit/sweep.py --show-failure-paths
JSONDecodeError` lists the affected paths; redownload via
`download_helm_results.py` (size mismatch triggers fresh fetch).

---

## G12. `run_spec_hash` is computed from canonicalized JSON, but canonicalization differs by release

**Symptom.** Two run_spec.json files with semantically identical
content can have different `run_spec_hash` values across HELM releases.

**Mechanism.** HELM canonicalizes the run_spec (key sort, drop
implementation-specific fields like absolute paths) before hashing,
but the canonicalization rules are HELM-version-dependent. New fields
added in newer releases mean the canonicalized form differs.

**Workaround.** Use the *coverage*-side canonical hash
(`_canonical_recipe_hash` in `helm_audit/virtual/coverage.py`), which
applies our own normalization on top of HELM's. The output is stable
across HELM releases for the same recipe.
