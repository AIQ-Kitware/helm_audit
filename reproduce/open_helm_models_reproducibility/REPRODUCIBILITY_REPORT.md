# Open HELM Models — Reproducibility Case Study

> *How reproducible are the open-weight models in the public HELM
> corpus, given the local audit results we currently have?*

This is the expanded virtual experiment that the NeurIPS EEE paper case
study draws from. Five open-weight model families, 19 benchmarks, 121
analyzed packets totaling **431,605 instance-level comparisons**, all
joined against the published HELM reference results.

The manifest is checked in at
[`configs/virtual-experiments/open-helm-models-reproducibility.yaml`](../../configs/virtual-experiments/open-helm-models-reproducibility.yaml);
the runbook is `reproduce/open_helm_models_reproducibility/`. Many of
the structural quirks we encountered are catalogued in
[`docs/helm-gotchas.md`](../../docs/helm-gotchas.md).

## Headline numbers (recipe-aware)

> **On the 83 recipe-canonical packets — the comparisons where local
> and public HELM agree on prompt, decoding, and scenario — open-weight
> HELM runs reproduce at instance-level agreement 0.917 ± 0.097
> (mean ± stdev) at abs_tol=0 across 307,976 instances. Median 0.960,
> minimum 0.554, max 1.000.**
>
> **On the 38 recipe-drifted packets — comparisons where local and
> public HELM disagree on adapter spec (different prompts, different
> max_train_instances) — agreement drops to 0.696 ± 0.182. This drift
> is recipe-side and cannot be attributed to the deployment
> substitution.**

The "naive" aggregate of 0.848 ± 0.165 across all 121 packets mixes
these two regimes and is the wrong number to publish.

## Three-level coverage funnel

The publication-quality coverage funnel reports three nested matches:

| match                          | n_target | n_reproduced |
|--------------------------------|---------:|-------------:|
| logical (loose)                |      295 |          166 |
| recipe-canonical (publishable) |      295 |      **128** |
| recipe-identical (raw hash)    |      295 |            0 |

What each match means:

- **logical**: same scenario + model + augmentation
- **recipe-canonical** (the publishable line): + same scenario_spec,
  prompt, decoding, max_train_instances, after collapsing
  HELM-version schema drift
- **recipe-identical**: byte-for-byte `run_spec.json` match — always
  0 because HELM's run_spec schema evolved across releases (see
  `docs/helm-gotchas.md` §G1)

The 0/295 raw-hash number is *not* a reproducibility failure. It's a
HELM-version artifact: newer HELM populates `adapter_spec` fields
(`chain_of_thought_prefix`, `chain_of_thought_suffix`, `global_suffix`,
`num_trials`, `model_deployment`) that older HELM left implicit. The
canonical hash collapses those defaults and gives 128 — the real
recipe-clean count.

## Per-model

| model                            | recipe-clean / total | mean agree@0 |   min |   max | dominant diagnosis                        |
|----------------------------------|----------------------|-------------:|------:|------:|-------------------------------------------|
| `eleutherai/pythia-2.8b-v0`      | 3 / 3                |        0.993 | 0.981 | 1.000 | deployment_drift                          |
| `lmsys/vicuna-7b-v1.3`           | 39 / 39              |        0.938 | 0.554 | 1.000 | deployment_drift                          |
| `eleutherai/pythia-6.9b`         | 39 / 39              |        0.896 | 0.679 | 1.000 | deployment_drift                          |
| `qwen/qwen2.5-7b-instruct-turbo` | 0 / 38               |        0.716 | 0.283 | 1.000 | **execution_spec_drift** (recipe-drifted) |
| `openai/gpt-oss-20b`             | 0 / 2                |        0.436 | 0.434 | 0.438 | multiple_primary_reasons                  |

The Qwen number was previously attributed in this report to "different
KubeAI deployment path". That was wrong. KubeAI vs direct-vLLM are both
litellm-fronted vLLM and don't materially differ at the model-output
level. The actual cause is **recipe drift**: the public Qwen HELM runs
use prompt prefixes like `Answer with only 'generic', 'descriptive',
...\n\n` that the local audit didn't replicate. See
[G5 in `docs/helm-gotchas.md`](../../docs/helm-gotchas.md#g5-adapter_specinstructions-differs-between-helm-releases-on-identical-scenarios).
This is a real recipe difference, not a serving-stack one.

For the Qwen subset specifically: 38 packets are all recipe-drifted
(execution_spec_drift). They don't yet contribute to the recipe-clean
0.917 number; running them under the original prompt would close most
of the gap.

## Per-benchmark (recipe-clean only, sorted by mean@0 descending)

| benchmark                | clean packets | instances | mean agree@0 |   min |   max |
|--------------------------|--------------:|----------:|-------------:|------:|------:|
| `entity_data_imputation` |             4 |     3,392 |        0.998 | 0.992 | 1.000 |
| `synthetic_reasoning`    |             6 |    24,000 |        0.991 | 0.976 | 1.000 |
| `truthful_qa`            |             2 |    10,464 |        0.988 | 0.977 | 0.999 |
| `imdb`                   |             2 |     8,000 |        0.996 | 0.991 | 1.000 |
| `lsat_qa`                |             2 |     7,376 |        0.979 | 0.960 | 0.998 |
| `boolq`                  |             3 |    12,000 |        0.978 | 0.952 | 1.000 |
| `quac`                   |             2 |     6,000 |        0.963 | 0.939 | 0.987 |
| `civil_comments`         |            20 |    80,000 |        0.941 | 0.819 | 1.000 |
| `mmlu`                   |            20 |    18,144 |        0.907 | 0.649 | 1.000 |
| `gsm`                    |             4 |     4,000 |        0.904 | 0.822 | 0.990 |
| `wikifact`               |            20 |   126,832 |        0.836 | 0.554 | 0.953 |
| `entity_matching`        |             6 |    11,200 |        0.758 | 0.679 | 0.832 |
| `narrative_qa`           |             2 |     5,640 |        0.616 | 0.283 | 0.986 |

Short-output classification reproduces tightly (>0.94 mean for 9 of
the top 10). Long-form generation (narrative_qa) and free-form text
similarity (wikifact, entity_matching) sit lower. Same pattern as the
naive aggregate, but now the numbers are unmixed with adapter-drifted
cases.

## What's responsible for the remaining 8% gap on recipe-clean packets

All 81 of the recipe-clean / non-perfect packets are diagnosed as
`deployment_drift`. The local audit ran the model through vLLM (or
KubeAI-fronted-vLLM, which is the same thing); the public HELM run
went through the HuggingFace API. Both stacks see the same prompt and
the same weights, but the per-instance output flips in 3-30% of cases
depending on benchmark type:

- **3-6%** flip rate on multiple-choice scorers (mmlu, boolq, lsat_qa)
- **~15%** on classification (civil_comments, wikifact)
- **~25-40%** on long-form generation (narrative_qa, wmt_14, ifeval)

The flips are mostly *full flips* (max |Δ| = 1.0 in our quantile
breakdown) rather than gradual drift, consistent with sampling
implementation differences (transformers' `.generate()` vs vLLM's
custom kernels) rather than mathematical non-equivalence.

## Mitigation recommendations for the deployment-drift gap

The 0.917 mean is the publishable open-weight reproducibility number
*today*, with the deployment substitution baked in. To close the
remaining gap and produce a "deployment-identical" number, the
recommendations in priority order:

### 1. Register a `huggingface/<model>` deployment in HELM and rerun

This is the highest-leverage fix. HELM ships `LocalHuggingFaceClient`
support but doesn't register deployment YAMLs for every open model
that appears in the public corpus. The fix:

- Write a deployment YAML at `prod_env/model_deployments.yaml` that
  registers `huggingface/<model>` for each target model (e.g.
  `huggingface/eleutherai/pythia-6.9b`).
- Make the deployment use HELM's `LocalHuggingFaceClient` with the
  HF model id and any tokenizer overrides.
- Rerun the audit with `--model-deployment huggingface/<model>`.

Expected impact: closes the deployment-substitution gap entirely for
the registered models. The local rerun would use the same serving
code path as the original public HELM run, giving recipe-and-stack-
identical comparisons. Agreement should rise from 0.917 to ~0.99+ on
the same scope (the residual being non-determinism in HF itself
across hardware / library versions, which is small).

[See `docs/helm-gotchas.md` §G3](../../docs/helm-gotchas.md#g3-the-huggingfacemodel-deployment-alias-does-not-always-exist-in-helm)
for the underlying mechanism (why HELM doesn't ship these by default)
and §G7 for a deeper discussion of HF vs vLLM determinism.

### 2. Pin sampling parameters across both stacks

Cheap and partial. Even with HF API on the official side and vLLM on
the local side, force:

- `temperature=0` (greedy) for any scenario that doesn't explicitly
  use sampling
- `top_p=1.0`, `top_k=-1` (disable nucleus / top-k sampling)
- `repetition_penalty=1.0`
- Identical `max_tokens`
- Identical `stop` sequences

This removes the sampling-policy axis of disagreement. Doesn't close
the kernel-difference axis (HF and vLLM differ in how they implement
the next-token argmax under masking, producing slightly different
log-probs in the tail of low-probability tokens), but those flips are
rarer.

### 3. Document, don't fix

The current case study uses this approach: report the gap as
`deployment_drift` and distinguish it from genuine model-correctness
drift. This is *the publishable narrative* and is what the paper says.
A reviewer asking "why didn't you close the gap?" can be answered
with "because doing so requires registering custom HF deployments for
every model, which is itself an interesting engineering finding worth
documenting (see G3)".

### 4. Use the canonical-recipe hash to filter scope

The recipe-canonical column already gives the publishable number
without re-running anything. Restricting the case study to the 83
recipe-clean packets (instead of the full 121) gives a tighter,
defensible bound on open-weight reproducibility under deployment
substitution. The 38 recipe-drifted packets become a separate
analysis about prompt-sensitivity of HELM's published recipes —
their own useful finding but a different paper.

## Coverage funnel summary

`reports/scoped_funnel/coverage_funnel_summary.latest.txt` shows the
full three-level breakdown. Key counts:

| stage                                   |   count |
|-----------------------------------------|--------:|
| target (in-scope official rows)         |     295 |
| reproduced (logical-key match)          |     166 |
| **reproduced (recipe-canonical)**       | **128** |
| reproduced (recipe-identical, raw hash) |       0 |
| analyzed                                |     166 |

The 38-row gap between logical and recipe-canonical is exactly the
set of comparisons with adapter drift. They're the rows the paper
should *either* run with corrected prompts *or* call out as
"recipe-disagreement" rather than "model-disagreement".

## Reading order for a referee

1. `reports/aggregate-summary/.../README.latest.txt`
2. `reports/scoped_funnel/coverage_funnel_summary.latest.txt` — the
   three-level coverage funnel.
3. `reports/aggregate-summary/.../sankey_a_universe_to_scope.latest.html`
   — Stage A: how 13,579 universe rows narrow to 295 in-scope.
4. `reports/aggregate-summary/.../sankey_b_scope_to_analyzed.latest.html`
   — Stage B: in-scope → reproduced → analyzed → agreement bucket.
5. `reports/aggregate-summary/.../prioritized_examples.latest/best/`
   and `.../worst/` — exemplary high- and low-agreement packets.
6. `reports/scoped_funnel/missing_targets.latest.csv` — 129 public
   rows in scope without a local repro.
7. `docs/helm-gotchas.md` — running ledger of HELM-specific behaviors
   we hit; useful for the paper's appendix.

## Reproducing this report

```bash
./reproduce/open_helm_models_reproducibility/compose.sh
./reproduce/open_helm_models_reproducibility/build_summary.sh
```

Both are thin wrappers over `helm-audit-build-virtual-experiment` and
`helm-audit-build-summary` against the checked-in manifest. Compose
takes ~30 minutes mostly because it converts ~180 local HELM runs to
EEE format on demand; the actual analysis is fast.
