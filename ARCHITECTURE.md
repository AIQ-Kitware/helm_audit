# eval_audit Architecture Design

## Table of contents

* [Purpose](#purpose)
* [Glossary](#glossary)
* [Core architecture](#core-architecture)
* [Main data areas](#main-data-areas)
* [Standard workflows](#standard-workflows)
* [What the system does](#what-the-system-does)
* [Comparability](#comparability)
* [What the reports should communicate](#what-the-reports-should-communicate)
* [Appendix: Architecture decision records](#appendix-architecture-decision-records)

  * [ADR 1 — Separate public and local indexes](#adr-1--separate-public-and-local-indexes)
  * [ADR 2 — Keep raw results separate from derived analysis](#adr-2--keep-raw-results-separate-from-derived-analysis)
  * [ADR 3 — `reports/` is the publication surface](#adr-3--reports-is-the-publication-surface)
  * [ADR 4 — The filesystem is part of the interface](#adr-4--the-filesystem-is-part-of-the-interface)
  * [ADR 5 — Every meaningful generated output gets a reproduce script](#adr-5--every-meaningful-generated-output-gets-a-reproduce-script)
  * [ADR 6 — Filtering is remembered and reportable](#adr-6--filtering-is-remembered-and-reportable)
  * [ADR 7 — Paths surfaced to humans should support browsing](#adr-7--paths-surfaced-to-humans-should-support-browsing)
  * [ADR 8 — Log every meaningful write to disk](#adr-8--log-every-meaningful-write-to-disk)
  * [ADR 9 — Plot inputs must never be silently truncated](#adr-9--plot-inputs-must-never-be-silently-truncated)
  * [ADR 10 — Plot labels and titles must explain what is being counted](#adr-10--plot-labels-and-titles-must-explain-what-is-being-counted)
* [Ideal state](#ideal-state)

## Purpose

`eval_audit` exists to answer one question:

**When we try to reproduce public HELM benchmark results locally, what matches, what does not match, and why?**

The repository is the system around the benchmark results. It is responsible for:

* indexing public HELM results,
* indexing local reproduction attempts,
* running local reproductions when needed,
* analyzing the results,
* and producing reports that are understandable, auditable, and easy to regenerate.

The ideal system lets a person move from:

1. the full public HELM universe,
2. to the subset we chose to try,
3. to the runs we attempted,
4. to the runs that completed,
5. to the runs we analyzed,
6. to the reasons they matched or differed,
7. and finally to the exact evidence for one case.

---

## Glossary

* **Public HELM results**: The benchmark outputs published by HELM. These are the reference results.
* **Local reproduction results**: The benchmark outputs produced by our own execution pipeline when we try to reproduce selected public HELM runs.
* **Index**: A machine-readable inventory over a result corpus. An index answers what exists and what metadata is attached to it.
* **Filtering**: The process of deciding which public HELM runs are in scope for local reproduction.
* **Experiment**: A coordinated batch of local reproduction attempts that belong together operationally.
* **Comparison**: A concrete analysis of one case, usually comparing a local result to a public HELM reference result.
* **Report surface**: The human-facing browsing and publication layer.
* **Latest alias**: A stable filename `<artifact>.latest.<ext>` that holds the newest version of a generated artifact. Since the history retirement on 2026-04-28 this is the actual file (re-runs overwrite it in place), not a symlink into `.history/...`.
* **Reproduce script**: A generated script placed near an output that can rebuild that output from saved inputs and metadata.
* **Packet**: The planner’s canonical comparison unit: a named bundle of normalized run components, the specific comparisons to render between them, and the comparability metadata that explains how trustworthy or caveated those comparisons are. In practice, one packet is the declarative input to one core report

---

## Core architecture

### Separate raw results from derived analysis

Keep three things separate:

1. public HELM results,
2. local reproduction results,
3. derived analysis and reports.

Raw results are evidence and should stay stable. Analysis and reports should be cheap to rebuild.

### Keep public and local indexes separate

Maintain:

* one canonical index for public HELM results,
* one canonical index for local reproduction results.

They should be similar enough in shape that later analysis can use them together, but they should remain separate sources of truth.

### Make reports tell a story

The reporting surface should explain the process in a stable order:

1. full public HELM universe,
2. locally eligible subset,
3. attempted subset,
4. completed and analyzed subset,
5. agreement and reproducibility,
6. machine or hardware split.

### Make the filesystem part of the interface and the data model

The filesystem should serve two roles at once:

1. **human guidance** for browsing and understanding the system,
2. and a **durable organizational structure** for raw results and derived outputs.

For human guidance, it should support:

* stable directory families,
* symlink-based navigation (between trees, e.g.
  `summary_root/level_001.latest -> summary_root/level_001/`),
* latest aliases (the actual file at `<name>.latest.<ext>`; see note below),
* clickable path rendering,
* and reproduce scripts.

(Timestamped history under `.history/` was retired on 2026-04-28: each
re-run overwrites the visible `*.latest.*` artifact in place. Old
`.history/` trees still on disk are orphaned and will eventually be
cleaned up out of band. The runtime no longer creates them.)

For data organization, it should provide a predictable place for:

* raw public results,
* raw local results,
* derived experiment analysis,
* and publication/report surfaces.

In particular, the symlinked browsing surfaces should help a human move through the evidence, while the underlying directory structure should act as a practical database for organizing and locating raw and derived results.

### Keep derived analysis cheap to rebuild

Raw benchmark jobs can be expensive. Derived reports should not be.

---

## Main data areas

### 1. Public HELM results

This is the reference corpus. It tells us what was published and defines the denominator.

The system should be able to build and analyze an index over this path independently.

### 2. Local reproduction results

This is the audit corpus. It contains the runs we executed ourselves.

It differs from public HELM because it can carry extra provenance such as:

* process or UUID-like identity,
* machine host,
* machine specs,
* deployment configuration,
* and local execution metadata.

These raw local results should be treated as read-only once execution finishes.

They should also be easy to combine from multiple machines into one analysis environment.

### 3. Derived analysis

This is where experiment-level analysis bundles live. It is separate from raw results.

### 4. Publication surface

This is the browsing and presentation layer under `reports/`.

It should contain:

* stable latest aliases,
* ordered story artifacts,
* drill-down surfaces,
* prioritized examples,
* and clickable paths into the underlying evidence.

---

## Standard workflows

### 1. Build and inspect the public HELM index

A user should be able to build the canonical public index and inspect the reference universe on its own.

### 2. Build and inspect the local reproduction index

A user should be able to build the canonical local index and inspect attempted and completed local runs on their own.

### 3. Rebuild the filtering view

A user should be able to regenerate the filtering surface from saved inventory data and explain what was included or excluded.

### 4. Rebuild experiment-level analysis

A user should be able to regenerate one experiment’s analysis bundle without rerunning raw benchmark jobs.

### 5. Inspect one concrete comparison case

A user should be able to start from one report directory and understand:

* what was compared,
* where those runs live,
* what evidence was generated,
* and how to regenerate the derived outputs.

### 6. Rebuild the aggregate story surface

A user should be able to regenerate the aggregate report bundle from the indexes and experiment analysis roots.

### 7. Drill down from summary to evidence

A user should be able to move from:

1. high-level summary,
2. to breakdown view,
3. to prioritized example,
4. to experiment-level report,
5. to concrete comparison evidence,
6. to the underlying raw run directories.

### 8. Merge raw results from multiple machines

A user should be able to collect raw audit results from multiple execution machines into one analysis environment, then rebuild indexes and reports.

### 9. Regenerate a single output after changing report logic

A user should be able to tweak reporting logic and rebuild one output or one bundle without rerunning raw benchmark execution.

### 10. Build slide-ready story artifacts

A user should be able to regenerate the ordered story surface used for communication and slides.

---

## What the system does

### 1. Index public HELM results

The system builds an index over public HELM results so it can answer:

* what public runs exist,
* what tracks and versions they belong to,
* what benchmark families they cover,
* and what run specification defines them.

### 2. Filter the public universe

The system decides which public runs are in scope for local reproduction and preserves those decisions.

This stage should remember:

* all discovered runs,
* which were eligible,
* which were selected,
* and why others were excluded.

### 3. Run local reproductions

The system translates selected runs into execution manifests and local jobs.

This includes handling models that are not available by default in HELM, including local-serving paths such as vLLM-backed execution.

### 4. Index local reproduction results

The system builds an index over local results so it can answer:

* what local runs were attempted,
* which completed,
* what machine they ran on,
* and what execution provenance was recorded.

### 5. Analyze each index independently

The public index and local index should each be analyzable on their own.

### 6. Build comparison evidence

The smallest important analysis unit is a concrete comparison case.

By default this is pairwise:

* a local result,
* compared against a public HELM reference result,
* and sometimes also against another local result.

The point of this layer is to make it easy to inspect one case and understand:

* what was compared,
* why it was compared,
* and what happened.

### 7. Build experiment-level analysis

The system groups comparison evidence into experiment-level bundles that explain:

* what an experiment attempted,
* what completed,
* what was analyzed,
* and where the evidence lives.

### 8. Build aggregate reports

The system produces higher-level reports for:

* coverage,
* filtering,
* failure reasons,
* reproducibility,
* prioritized examples,
* and slide-ready story surfaces.

---

## Comparability

The system should distinguish between:

* same benchmark family,
* same scenario implementation,
* same base model,
* same deployment,
* same adapter instructions,
* same requested evaluation size,
* and strict versus caveated comparability.

Not every useful comparison is perfectly identical.

Some are strict reproducibility checks. Others are intended reproductions with known drift, such as deployment substitution or instruction mismatch.

The architecture should support both:

* grouping runs into the same comparison family,
* and clearly recording when the comparison is not strictly apples-to-apples.

---

## What the reports should communicate

Reports should help answer four kinds of questions.

### Coverage

* What exists in public HELM?
* What did we consider?
* What did we attempt?
* What completed?
* What was analyzed?

### Reproducibility

* Which local results agree with public HELM?
* Which do not?
* How sensitive is the answer to tolerance?
* Which metrics drift the most?

### Failure

* Why did attempted runs fail or remain incomplete?

### Evidence

* For one concrete example, what exact runs were compared?
* Where do those runs live?
* What sample mismatches explain the disagreement?

---

## Story and presentation requirements

The architecture should make it easy to build a stable, slide-ready story from the reporting surface.

That story should move through this order:

1. the full public HELM universe,
2. the locally eligible subset,
3. the attempted subset,
4. the completed and analyzed subset,
5. agreement and reproducibility,
6. machine or hardware split,
7. and the output structure that makes the work auditable.

The last item matters because the output structure itself helps explain how the system supports exploratory work without losing trust.

---

## Appendix: Architecture decision records

This appendix keeps the main document readable while still recording the important architectural decisions and policies that shape the repository.

## ADR 1 — Separate public and local indexes

### Context

Public HELM results and local reproduction results come from different sources and carry different metadata.

### Decision

Maintain:

* one canonical index for public HELM results,
* one canonical index for local reproduction results.

They should be similar enough in shape that later analysis can use them together, but they remain separate sources of truth.

### Consequences

* discovery logic can remain different for public and local results,
* provenance stays cleaner,
* and later grouping can still consume both indexes consistently.

## ADR 2 — Keep raw results separate from derived analysis

### Context

Raw benchmark outputs are evidence. Reports and summaries are interpretations built from that evidence.

### Decision

Keep raw results separate from derived analysis and treat raw results as read-only outside execution.

### Consequences

* analysis can be rebuilt without mutating evidence,
* raw results can be merged across machines,
* and report logic can evolve safely.

## ADR 3 — `reports/` is the publication surface

### Context

The system needs both a canonical analysis area and a presentation/browsing area.

### Decision

Treat experiment analysis roots as the canonical derived analysis area, and treat a folder named `reports/` as the publication and browsing surface.

The folder is *named* `reports/` consistently, but its **location is parameterized** via `eval_audit.infra.paths.publication_root()`. The default points at `<audit_store>/reports/` so derived outputs do not pollute the checked-in repository tree. The `HELM_AUDIT_PUBLICATION_ROOT` environment variable, and per-CLI flags such as `analyze_experiment --publication-root` or `build_reports_summary --summary-root`, override the default. Code must obtain the publication root via the helper, not by hard-coding `<repo>/reports/`.

### Consequences

* `reports/` can optimize for readability and navigation,
* the analysis roots can optimize for correctness and rebuildability,
* the two roles stay conceptually separate,
* and the publication surface can be relocated (e.g. to a dedicated artifact store, a per-virtual-experiment output root, or back into the repo for the legacy layout) without code changes — only configuration.

## ADR 4 — The filesystem is part of the interface

### Context

Users browse this system through paths, symlinks, report trees, and logs, not only through code.

### Decision

Use the filesystem both as:

* a source of human guidance for browsing and understanding,
* and a durable organizational structure for raw and derived results.

### Consequences

The system should intentionally support:

* symlink-based navigation between trees,
* latest aliases (the canonical name `<artifact>.latest.<ext>` is the
  actual file; some same-tree shortcuts like
  `reproduce.sh -> reproduce.latest.sh` remain symlinks),
* clickable path rendering,
* and stable directory responsibilities.

History note: between roughly 2024 and 2026-04-28 every artifact had a
sibling stamped copy under `.history/<YYYYMMDD>/<full-stamp>/...`. That
layer was retired because re-runs accumulated faster than they were
useful and the directory growth outweighed the rare benefit of digging
out a previous version. Re-runs now overwrite the `*.latest.*` artifact
in place. The git history of derived artifacts is no longer captured by
the filesystem — only by the source code and the seed data the report
was built from. Old `.history/` trees on disk are orphans; new code
neither writes nor reads them.

## ADR 5 — Every meaningful generated output gets a reproduce script

### Context

Raw benchmark execution is expensive, but derived analysis should be cheap to rebuild.

### Decision

Place reproduce scripts near meaningful generated outputs so they can be rebuilt from saved inputs and metadata.

### Consequences

* report logic can evolve without rerunning raw jobs,
* outputs become more auditable,
* and iteration becomes cheaper.

## ADR 6 — Filtering is remembered and reportable

### Context

The reproducibility story depends on the denominator, not just on the analyzed subset.

### Decision

Treat filtering as a first-class, remembered part of the system. Preserve what was discovered, selected, attempted, completed, and analyzed, along with exclusion reasons when possible.

### Consequences

* the funnel story can be reconstructed later,
* the denominator remains explainable,
* and filtering stops being a hidden setup step.

## ADR 7 — Paths surfaced to humans should support browsing

### Context

Many logs and reports tell a human where to go next in the filesystem.

### Decision

Whenever logs or report text intentionally surface a path for a human to inspect, path rendering should support browsing rather than just dumping raw strings.

### Consequences

* paths become easier to follow,
* logs become more actionable,
* and the filesystem works better as a user interface.

## ADR 8 — Log every meaningful write to disk

### Context

Generated outputs are only useful if people can tell what was written and where.

### Decision

Treat it as a policy to log every meaningful write to disk.

### Consequences

A person reading the logs should be able to tell:

* what was written,
* where it was written,
* and what they should inspect next.

## ADR 9 — Plot inputs must never be silently truncated

### Context

Plots are often used for decision-making and presentation, so hidden truncation damages trust.

### Decision

Never silently truncate the data that goes into a plot. If a plot shows a subset, that subset must come from an explicit selection rule.

### Consequences

* plots remain interpretable,
* “clean” visuals do not hide evidence,
* and important plots should have an inspectable source table when needed.

## ADR 10 — Plot labels and titles must explain what is being counted

### Context

Ambiguous plot labels create avoidable confusion.

### Decision

Titles and labels should make clear what population a plot summarizes and what each count refers to.

### Consequences

* readers do not have to guess what `n` means,
* plots become more slide-ready without losing trust,
* and the reporting surface becomes easier to interpret.

---

## Ideal state

In the ideal state:

* official results and reproduced results are clearly separated,
* both are indexed and analyzable on their own,
* filtering decisions are remembered,
* local execution metadata is preserved,
* the report surface tells the reproducibility story in a stable order,
* the filesystem is intentionally browsable,
* every important output can be regenerated,
* and a human can move from high-level slides to low-level evidence without relying on memory.


# AMENDMENTS

### Amendment 1 — Keep plot data complete
Do not silently truncate plotted data.  
Long static labels may be abbreviated if the mapping is deterministic and emitted nearby.

### Amendment 2 — Default heavy pairwise artifacts to scripts
Do not auto-render every pairwise interactive artifact.  
Write a nearby script to generate richer HTML/Plotly outputs on demand.
