# 2026-05-01 / 05-02 — paper-writing handoff session log (part 2)

Companion to [`2026-04-30_eee_heatmap_session_log.md`](2026-04-30_eee_heatmap_session_log.md)
and [`2026-05-01_session_log.md`](2026-05-01_session_log.md).

This log covers what came after the slim-heatmap analysis was complete
and the three case studies (entity_matching pandas-merge, SR-Natural
× Pythia broken official, WikiFact stochastic noise floor) were
characterized. The earlier logs document the *investigation*; this
one documents the *write-up* — where we decided which findings go
where, what got corrected against on-disk evidence, what got
corrected against externally-verifiable Lean theorems, and the
structural / formatting decisions that shaped the final
`technical_report.tex`.

The technical report itself is at
[`dev/paper/technical_report.tex`](technical_report.tex) (~2470
lines). The progressive distillation is `case_study_3_appendix.tex`
(~970 lines) → `case_study_3.tex` (~100 lines).

---

## Document architecture

We landed on a three-document distillation pattern that should be
reusable for future paper-side contributions:

| Document | Lines | Role |
|---|---|---|
| `technical_report.tex` | 2470 | Long-form superset; everything we found, with full evidence chains and source-line citations. Stands alone — no `\input` from the others. |
| `case_study_3_appendix.tex` | 970 | Subset for the paper appendix; reads independently, drops the §1 Executive Summary, the §3 Pipeline Architecture, and the §12 Tooling Deliverables, but keeps the case studies and methodological caveats. |
| `case_study_3.tex` | 100 | Further-distilled, paper-body section. Three paragraphs corresponding to Cases A/B/C plus a closing layer-separation paragraph. |

Distillation strategy (worked well, would do again): write the long
form first, with everything in it. Then *cut* for the appendix; then
cut further for the body. The reverse direction (writing the body
first, expanding into the appendix, expanding into the tech report)
would have meant repeatedly re-discovering the same findings in
multiple drafts. The "write long, cut twice" order was efficient.

`paper_draft/` is gitignored; `dev/paper/` is tracked. The user's
mid-session pivot was to keep working drafts in `paper_draft/` but
move the canonical released version into `dev/paper/`. The
scaffold-vs-paper distinction is important: we maintain a
self-contained `main.tex` in `dev/paper/` that compiles our section
in isolation, but we deliberately do *not* commit any content from
the broader \eee{} manuscript to `dev/paper/` — only our own
contribution. The user re-emphasised this twice; one early commit
that included a more elaborate scaffold + README was reset out of
history (`git reset HEAD~1`) and recommitted with a slimmer
preamble-only `main.tex`.

---

## Mathematical / formal verification

The user wrote
[`wikifact_consistency_claim.lean`](wikifact_consistency_claim.lean)
externally, against Mathlib, to formalize the Bernoulli-agreement
identities the WikiFact case rests on. The Lean file *compiles*
externally; the central theorem we cite is

```
expectedAgreement_eq_uniformAgreement_add_variance :
  E[agree(p_i)] = agree(avg p) + 2 * Var(p_i)
```

with corollary `uniformAgreement_le_expectedAgreement` (the
homogeneous prediction at the mean is a lower bound on the
heterogeneous expected agreement).

**The Lean caught two of my LaTeX claims being wrong.** This is the
methodologically interesting part — the formal-verification artifact
served as a fact-checker on the prose:

1. *Wrong numerical range.* I had written that
   `agree(p) = p² + (1-p)² ∈ [0.85, 0.95]` for `p ∈ [0.5, 0.9]`. The
   Lean theorems `agree_centered` and `agree_range_85_90` make the
   correct range explicit: `agree(p) ∈ [0.5, 0.82]` for that p
   interval. The Lean theorem `not_agree_92_on_85_90` further proves
   that homogeneous `p ∈ [0.85, 0.9]` *cannot* produce agreement
   `0.92`. Both fixes landed in the appendix and tech report.

2. *Loose 0.92 bound.* I had said "producing 0.92 requires either
   `p > 0.92` or `p < 0.08`". The Lean implies the precise roots:
   solving `p² + (1-p)² = 0.92` gives
   `p = (1/2)(1 ± √0.84) ≈ 0.9583` or `0.0417`. The tech-report and
   appendix both now use the precise inequality.

Citation pattern in the LaTeX: I cite Lean theorems by name in
prose (e.g., "theorem
`expectedAgreement_eq_uniformAgreement_add_variance`") plus a footnote
to the .lean file. A reviewer can audit the math claim at the
theorem level rather than re-deriving it in the manuscript.

This is a useful pattern for the rest of the paper — short Lean
files alongside specific numerical claims let the manuscript stay
informal while the underlying identities remain machine-checked.

---

## Editorial corrections, by category

### Claims I had to weaken / re-attribute against on-disk evidence

| Claim (was) | Correction | Evidence source |
|---|---|---|
| "audit against the public HELM Classic v0.3.0 corpus" | "against public HELM run artifacts from the relevant HELM suites, including HELM Classic where applicable" | `ls /data/crfm-helm-audit-store/crfm-helm-public-eee-test/` shows 13 suites: `capabilities/`, `classic/`, `ewok/`, `finance/`, `heim/`, `image2struct/`, `lite/`, `long-context/`, `medhelm/`, `mmlu/`, `mmlu-winogrande-afr/`, `safety/`, `speech/` |
| "Each [mechanism] is detected by EEE's content-derived `sample_hash`" | Only Case A (entity_matching) is detected at the hash-join layer (zero overlap → `join_failed`). Cases B and C succeed at the hash-join because prompts and references are byte-equivalent on both sides; B is detected via `evaluation.score` divergence + empty `output.raw` pattern; C is detected via run-level mean accuracy gap + cross-model uniformity diagnostic. | Per-case evidence in the appendix; the on-disk EEE samples have matching `sample_hash` for B and C cells. |
| "EEE's content-addressed `sample_hash`" | "content-derived hash" (the hash *validates* identity; it does not *retrieve* or *address* the underlying content) | Source-level read of `every_eval_ever/converters/helm/instance_level_adapter.py:231-233`. |
| "abs_tol=0" | "abs_tol=1e-9" | `cell_data.json["abs_tol"]` reads as `1e-09`. |
| Stanford HELM dashboard `/latest/` URL | The on-disk `/data/crfm-helm-public/.../stats.json` (version-stable) plus a `/v0.3.0/` dashboard URL as secondary | The `/latest/` URL would change over time; the on-disk artifact is frozen. |

### Claims I had to *strengthen* with on-disk evidence

| Claim | Evidence pulled |
|---|---|
| "We did not run the public HELM evaluations; CRFM did" | Verified by reading `source_metadata.evaluator_relationship="third_party"` and `source_metadata.source_organization_name="CRFM"` in the public-side EEE aggregate JSONs. Our local-side EEE has `source_organization_name="eval_audit_local"`. |
| Local-side experiment names + dates | Pulled from `provenance.json` sidecars: `audit-historic-grid` (Pythia/Vicuna, 2026-03-28/29 on `aiq-gpu`); `audit-falcon-7b-helm-grid` (Falcon, 2026-04-30 on `aiq-gpu`). |
| sample_hash formula | Verbatim Python excerpt from the converter source (not paraphrased); cited at the file:line level. |
| HELM v0.3.0 routes `eleutherai/*` to `TogetherClient` | Verbatim listing of `helm/proxy/clients/auto_client.py:184-200` from the v0.3.0 git tag. |
| 0% Pythia SR-Natural score on the leaderboard | Confirmed bytewise in the on-disk `stats.json` (mean=0.0 for f1/iou/exact_set_match on both `test` and `valid` splits). |

### Things I had to caveat that I'd been too confident about

- **The systematic Pythia outlier mechanism (logit margin width).** I
  hypothesised that Pythia's higher backend-substitution sensitivity
  comes from narrower per-token logit margins, mediated by lower
  training compute and absence of instruction tuning. The user
  pushed back on framing this as "the cause"; the final tech report
  now lists four independent confounds we cannot rule out without
  direct A/B testing against Together's API at known precision:
  different served checkpoint, different serving precision (fp16 vs
  bf16 vs int8 vs fp32), architecture-specific numerical sensitivity
  (Falcon's MQA vs Pythia's MHA), and generation-kernel differences.
  The conservative claim — "the divergence exists, is detected by
  EEE's `sample_hash`, is consistent with a backend-substitution
  effect, and disproportionately affects the model with the smallest
  training-compute budget and no instruction tuning" — does not
  require distinguishing among the four.

- **The "first divergent token" framing.** Initially I said
  "first-token divergence" everywhere. The user's GSM × Vicuna spot
  check disproved this for chain-of-thought benchmarks: outputs
  agree on a 50+-character prefix and diverge mid-stream. The framing
  is now "first divergent token" with the qualification that for
  multiple-choice tasks the divergence usually IS at the first token
  (since only 1-5 tokens are emitted) but for free-form CoT it can
  be 50+ tokens in.

### The "honesty caveat" on the version-pin table

The pinned SHAs in the technical report's "Versions used for the
final reproduction" table (Section 13.1, `Table tab:tr-versions`)
record the repository state *at the time of writing the report*
(2026-05-02), **not** a single static state under which every
artifact in the report was produced. The investigation went through
~60 commits between the first slim-heatmap render and the final
report:

- The audit pipeline, heatmap renderer, metric classifier, and
  reproducer scripts were all built or amended.
- Local-side HELM runs were performed at *earlier*
  `submodules/helm` pins than the table records (per per-conversion
  `provenance.json` sidecars).
- The audit-hook verdict was generated 2026-05-01 19:29 UTC and the
  slim heatmap was last re-rendered 2026-05-01 21:53 UTC, with
  intermediate pipeline-fix commits between those two times.

The table now leads with this caveat, and the table caption itself
compresses the same point. The user's read on this was important:
"we need to be honest here" — pinning a single SHA and implying
static reproducibility would over-claim.

---

## The "both-wrong masking" finding (new in this session)

This is a methodological finding that surfaced during the spot-check
of the high-but-not-perfect cells (SR × Falcon at 0.999, etc.).

**Setup**: Falcon × SR-Natural cell renders at agree_ratio = 0.999.
The cell value implies near-perfect reproducibility.

**Reality**: 10 of 1000 prompts (1.0%) have bytewise-different
`output.raw` between Together and HF. The two backends diverge at
the model-output level on 1% of prompts.

**Why the cell value hides this**: of those 10 output-divergent
prompts, only 3 produce score disagreements across the four core
metrics × 1000 prompts = 4000 score rows, because the other 7
output-divergent prompts produce outputs that both miss the gold
reference, so the per-row score is `0=0` on every metric and the row
counts as "agreement" at `abs_tol=1e-9`. The cell value `0.9992`
thus hides a ~10x larger output-divergence rate.

**Generalization**: this is a third failure mode of micro-averaged
`agree_ratio`, complementing the two we already documented:
- *Degenerate-zero agreement* (catastrophic): SR-Natural × Pythia at
  0.788 — entire side scores 0; agreement reads as
  "fraction the other side also failed".
- *Both-wrong masking* (mild): Falcon × SR at 0.999 — outputs
  differ but both miss the gold; agreement reads as 1.0.
- *Stochastic noise floor*: WikiFact at ~0.92 — recipe-level
  non-determinism by design.

The three failure modes share one mechanism (any `0=0` collision
counts as a match) at three different scales. The
"both-wrong masking" framing is now in §10 of the tech report.

**Recommendation surfaced**: honest reporting of a reproducibility
heatmap should display, alongside the cell-level `agree_ratio`,
both the run-level mean per metric on each side (to expose the
degenerate-zero case), the recipe's `temperature` setting (to
expose the stochastic-floor case), and the per-cell
output-divergence rate (to expose the both-wrong-masking case).
None of these is currently rendered inline on the heatmap PNG; a
future iteration of the renderer could surface all three.

---

## Per-cell evidence I gathered for the systematic Pythia pattern

The tech report's §9 has the per-cell breakdown for the eight
sub-0.98 Pythia cells. These are the on-disk artifacts I pulled
representative examples from, in case a future investigation wants
to extend the analysis:

| cell | recipe | n disagree (rows) | example pattern |
|---|---|---|---|
| BoolQ × Pythia (0.934) | temp=0, num_outputs=1, max_tokens=5, stops=`['\n']` | 155 / 3206 exact_match | off=`No` vs loc=`Yes` on white-house question |
| CivilComments × Pythia (0.940) | temp=0, num_outputs=1, max_tokens=5 | 194 / 3245 exact_match | off=`False` vs loc=`True` on toxicity question |
| LSAT-QA × Pythia (0.960) | temp=0, num_outputs=5, max_tokens=1 | 18 / 461 exact_match | off=`C` vs loc=`B` on locker-assignment Q |
| MMLU × Pythia (0.935) | temp=0, num_outputs=5, max_tokens=1 | 19 / 326 exact_match | off=`A` vs loc=`B` on liberal-character Q |
| NarrativeQA × Pythia (0.910) | temp=0, num_outputs=1, max_tokens=??? | 215 / ~2000 f1_score | paraphrases of Fra Luca answers |
| QuAC × Pythia (0.942) | temp=0, num_outputs=1, max_tokens=100 | 586 / 4142 f1_score | off=hallucination, loc=`CANNOTANSWER` (the gold) |
| TruthfulQA × Pythia (0.977) | temp=0, num_outputs=5, max_tokens=1 | small — single-letter flips | (similar to MMLU) |
| SR-Natural × Pythia (0.788) | temp=0, num_outputs=1, max_tokens=20 | 22% rows | empty completion (Case B; Together first-token=`\n`) |

The aggregate per-cell run-level means are close on every cell except
SR-Natural × Pythia (where the 0% vs 20% gap is the catastrophic
case).

---

## LaTeX formatting decisions

Lessons that future paper-side work could reuse:

1. **`\texorpdfstring{rich}{ASCII}` for headings with math/special
   chars.** PDF bookmarks fail on `$\times$`, `\texttt{}`,
   `$0.788$`, etc. Wrap every section title that contains math or
   `\texttt` in `\texorpdfstring{...}{ASCII fallback}`. Affected 11
   headings across the tech report and 3 in the appendix.

2. **`\path{}` from the `url` package for paths and underscore-heavy
   identifiers.** `\texttt{audit\_eee\_only\_run.py}` doesn't break
   at underscores and overflows the page; `\path{audit_eee_only_run.py}`
   breaks at `_`, `.`, `/` and renders monospace. Surgical-rule
   conversion (anything >22 chars or containing `="`): 117 sites
   converted; short identifiers like `\texttt{run\_spec}` left
   alone. Important: `\path{}` does *not* take escaped underscores —
   the input must be raw `_`.

3. **`tabularx` with a ragged `Y` column for wide tables.** The
   Pythia examples table had four columns including long prompt
   suffixes; it overflowed and produced underfull box warnings as
   `\begin{tabular}{p{2.5cm}p{4.5cm}p{3cm}p{3cm}}`. Replaced with
   `\begin{tabularx}{\linewidth}{p{1.9cm} Y p{2.2cm} p{2.2cm}}` after
   defining `\newcolumntype{Y}{>{\raggedright\arraybackslash}X}`.

4. **`[htbp]` over `[h]`** for table floats in long documents. `[h]`
   floats LaTeX often silently overrides; `[htbp]` lets the layout
   engine pick reasonable placement. Replaced 4 sites in the tech
   report and 6 in the appendix.

5. **Quiet `hyperref` colors**:

   ```tex
   \usepackage[
     colorlinks=true,
     linkcolor=black,
     citecolor=black,
     urlcolor=blue
   ]{hyperref}
   ```

   The default `colorlinks=true` produces a red TOC, which is loud.

6. **Tighter `lstlisting` with `\scriptsize`** instead of
   `\footnotesize` — listings are still readable and use less
   vertical space.

7. **Non-ASCII glyphs in `lstlisting` blocks will break compile**
   under `pdflatex`. The original token-stream listing had `Ċ` and
   `Ġ` (BPE conventions for newline and word-initial space) inline.
   Replaced with `<newline-tok>` and `<space-pfx>` placeholders, with
   the BPE-encoding explanation moved into the surrounding prose.

8. **Listing captions are fragile if long.** The token-stream caption
   was a paragraph-length explanation with multiple `\texttt{}` terms
   inside it. Moved the explanation into the surrounding prose; the
   caption is now one sentence.

9. **`\section*{}` to suppress numbering** is occasionally tempting
   but `\appendix` then `\input{...}` is cleaner for a real appendix.

10. **`bookmark` package complements `hyperref`** for clean PDF
    bookmark behaviour; load it after `hyperref`.

---

## Reusable invocation patterns

### Verifying claims against on-disk evidence (rather than session logs)

The user repeatedly emphasised: "we shouldn't rely on session logs;
verify against the actual files on disk". This worked well as a
discipline. Concrete instances:

- `cat /data/.../cell_data.json` to confirm `abs_tol=1e-9` (not the
  `abs_tol=0` I'd written from memory)
- `grep -A 6 sample_hash` in the EEE converter source to confirm the
  exact formula
- `ls /data/crfm-helm-audit-store/crfm-helm-public-eee-test/` to
  confirm the public store actually has 13 suites (not just
  `classic/`)
- `cat .../provenance.json` for local-side run timestamps
- `cat .../stats.json` to confirm the published 0% Pythia score

The mantra: every numerical claim in the technical report should be
greppable from a file on disk. If it isn't, weaken or remove the
claim. We held to this throughout the editorial pass.

### Bisecting causes via version grids (instead of speculation)

When investigating the entity_matching pandas-merge mechanism, the
breakthrough was a 5-combo `(pandas, numpy)` grid run inside `uv venv`
sandboxes (`em_pandas_mwe.sh`). The grid was constructed deliberately
to isolate one variable per row pair: rows 1-2 hold pandas constant
while sweeping numpy; rows 3-5 hold pandas constant in another bin
while sweeping numpy across the major-version boundary. The result
collapses into two distinct digest groups, with the boundary purely
on pandas. Numpy is shown to be irrelevant by direct experiment, not
by argument.

This grid pattern should be reusable for any "is X or Y the cause?"
question in the rest of the paper.

### End-to-end reproducer matrices

The `em_helm_mwe.sh` reproducer built two venvs at different
crfm-helm versions and ran the full HELM scenario chain in each, then
diffed against captured `scenario_state.json` artifacts on disk. The
4-cell verdict matrix (current vs. v0.3.0-era × LOCAL vs. OFFICIAL
captured) was an unusually convincing artifact: each diagonal cell
matched bytewise, each off-diagonal cell did not. The "exact match
on the diagonal" + "id-match-but-content-mismatch off-diagonal"
combination is sufficient to claim the mechanism without needing to
walk back to source-line argument.

This could become a paper-side methodological point: when you have a
hypothesis about a version-dependent reproducibility mechanism,
build the verdict matrix from the test environment.

---

## Open follow-ups not yet captured in the technical report

These are out-of-scope for the report itself but worth documenting
for future work:

- **Direct Together API verification of Case B.** Inferencing
  Pythia-6.9B against today's Together API at known precision and
  comparing the first-token distribution to our local HuggingFace
  serving would localize the proximate cause to one of our four
  enumerated confounds. Tech report acknowledges this as a follow-on
  in §14.
- **Bisect the exact pandas commit between 2.0.x and 2.2.x** that
  changed the `pd.merge` row ordering on this dataset. We
  established the boundary at the version level; the per-commit
  bisect would let us file a precise upstream issue or document a
  specific pandas compatibility note.
- **Cross-harness audit**: run the same three-model slim grid through
  `lm-eval-harness` or `Inspect-AI` and align per-instance scores
  against the HELM-side records via \eee{}. Surfaces a second axis
  of harness-level drift and validates EEE's cross-harness
  comparability claim from a different angle.
- **Inline output-divergence rate on the heatmap**. Per cell, render
  not just the agree_ratio but also `(off mean, loc mean,
  output-divergence rate)`, so the three failure modes of the
  micro-averaged metric (degenerate-zero, both-wrong masking,
  stochastic-floor) become visible at-a-glance without leaving the
  figure. Would require modifying the heatmap renderer in
  `eval_audit/reports/eee_only_heatmap.py`.
- **File a HELM upstream issue for the SR-Natural × Pythia
  stop_sequence trim.** The published 0% score is a real artifact of
  inference backend × text post-processing; an issue would document
  it and possibly motivate a fix on HELM's side. Independent of the
  pandas-merge issue. Tech report §14 acknowledges this as a
  follow-on.
- **Reseed the metric classifier** with the rest of the HELM scenario
  metric vocabulary. We registered the `*_set_match` family because
  it was specifically needed for `synthetic_reasoning_natural`; a
  full sweep against `submodules/helm/.../static/schema*.yaml` would
  catch any other gaps preemptively.

---

## Final commit landmarks (for reference)

The final state of `dev/paper/` lives at commit `682f2c3` (head as
of 2026-05-02). Key landmark commits during this part-2 session:

| commit | message |
|---|---|
| `2e9ea49` | dev/paper: scaffold standalone case-study build for version control (later reset out of history) |
| `9758e28` | dev/paper: case-study text, supporting Lean theorems, and investigation logs |
| `4e5b094` | dev/paper: add technical_report.tex — the long-form superset |
| `0f8bc12` | dev/paper: technical_report.tex — editorial pass on facts and framing |
| `53c69d2` | dev/paper: technical_report — narrow HELM-suite scope + correct sample_hash claim |
| `0a2aff2` | dev/paper: address all 8 format-feedback items in the technical report |
| `2774524` | dev/paper: \texttt → \path for long underscore-heavy strings; add reproduction-pin section |
| `682f2c3` | dev/paper: technical_report — honesty caveat on the version-pin table |

`paper_draft/` (gitignored) still holds working copies but is not the
canonical location.

---

## Don'ts (additions specific to this session)

- **Don't claim "content-addressed" identity for `sample_hash`.**
  It validates identity; it doesn't address content. Use
  "content-derived hash" or "hash-validating identity" depending on
  context. The hash can verify whether two records describe the same
  prompt-reference pair; it cannot retrieve the underlying content.

- **Don't claim `sample_hash` detects all three reproducibility
  mechanisms.** It's load-bearing for Case A (pandas-merge ordering)
  only. Cases B and C are detected post-join, via score divergence
  and run-level mean comparison.

- **Don't use `[h]` as a table float specifier in long documents.**
  LaTeX often overrides it; use `[htbp]` (or just omit).

- **Don't put math or `\texttt{}` in section/subsection titles
  without `\texorpdfstring`.** PDF bookmark generation will warn or
  fail.

- **Don't use `\texttt{...}` for paths or underscore-heavy
  identifiers >22 chars.** Use `\path{...}` from the `url` package.
  `\texttt` doesn't break at underscores and overflows the page.

- **Don't pin a single SHA as "the version everything was produced
  at"** when multiple commits over the investigation history were
  involved. Be explicit that the pinned SHA is "today's pin", and
  document the per-conversion provenance separately.

- **Don't write numerical bounds without checking the math.** The
  Lean caught two of mine. Symmetric bounds on `p² + (1-p)²` (the
  agreement formula) are not the same as the bound on `p` itself —
  homogeneous `p ∈ [0.85, 0.9]` gives `agree(p) ∈ [0.745, 0.82]`,
  not `[0.85, 0.95]`.

- **Don't include `/latest/` URLs in archived material.** They will
  drift. Use a versioned URL (e.g., `/v0.3.0/`) or, better, cite the
  on-disk artifact that we control.

- **Don't write "this is a reproducibility failure" for recipes that
  pin `temperature > 0`.** The variance is by design; the audit
  should report "stochastic recipe at the heterogeneity-corrected
  expected agreement", not "X% reproducibility".

- **Don't lump our own audit work with paper content from the
  broader manuscript when committing.** The user re-emphasised
  twice: "we are ONLY writing our section". `paper_draft/main.tex`
  has paper-side preamble (NeurIPS class, full author list,
  abstract, etc.); `dev/paper/main.tex` is a slim self-contained
  scaffold for our section only. Don't mix.
