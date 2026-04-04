r"""
Compile a reproduction list from existing HELM outputs on disk.

Given one or more roots that contain HELM outputs, discover all run directories
and emit a list of run specs you can feed into kwdagger / helm-run.

Outputs are structured so you can:
- reproduce exact run directories (by using run_entry == run directory name)
- optionally include max_eval_instances inferred from per_instance_stats.json

Ignore:

    ls /data/crfm-helm-public/thaiexam/benchmark_output/runs/v1.1.0/thai_exam:exam=tpat1,method=multiple_choice_joint,model=aisingapore_llama3-8b-cpt-sea-lionv2.1-instruct

    python -m helm_audit.cli.index_historic_helm_runs /data/crfm-helm-public --out_fpath run_specs.yaml --out_detail_fpath run_details.yaml

    cat run_specs.yaml | grep -v together > run_specs2.yaml

    python ~/code/aiq-magnet/dev/poc/inspect_historic_helm_runs.py /data/Public/AIQ/crfm-helm-public/

    # we need fully featured helm installed
    uv pip install crfm-helm[all] -U

    # Need to login to huggingface can pass token via --token
    hf auth login

    # Need TogetherAPI credentials

    kwdagger schedule \
      --params="
        pipeline: 'magnet.backends.helm.pipeline.helm_single_run_pipeline()'
        matrix:
          helm.run_entry:
            - __include__: run_specs2.yaml
          helm.max_eval_instances:
            - 1000
          helm.precomputed_root: null
      " \
      --devices="0,1,2,3" \
      --tmux_workers=4 \
      --root_dpath=$PWD/results \
      --backend=tmux \
      --skip_existing=1 \
      --run=1
"""

from __future__ import annotations
from pathlib import Path
from typing import Iterable, Any

import ubelt as ub
import kwutil
import scriptconfig as scfg
from loguru import logger

from magnet.backends.helm.helm_outputs import HelmOutputs, HelmRun

# Reuse your existing discovery + inference logic
from magnet.backends.helm.cli.materialize_helm_run import (
    discover_benchmark_output_dirs,
    infer_num_instances,
    is_complete_run_dir,
)


class CompileHelmReproListConfig(scfg.DataConfig):
    roots = scfg.Value(
        ['/data/crfm-helm-public'],
        nargs="+",
        help=(
            "One or more roots that either ARE a benchmark_output dir, contain "
            "benchmark_output dirs, or contain suite/benchmark_output dirs."
        ),
        position=1,
    )

    suite_pattern = scfg.Value(
        "*",
        help="Glob applied to benchmark_output/runs/<suite> directories.",
    )

    run_pattern = scfg.Value(
        "*:*",
        help="Glob applied within each suite to select runs (default selects HELM run dirs).",
    )

    require_per_instance_stats = scfg.Value(
        False,
        help="If True, only include runs that have per_instance_stats.json.",
    )

    include_max_eval_instances = scfg.Value(
        False,
        help="If True, infer max_eval_instances from per_instance_stats.json when possible. CAN BE VERY SLOW",
    )

    out_fpath = scfg.Value(
        None,
        help="Where to write output. If omitted, prints to stdout.",
    )

    out_detail_fpath = scfg.Value(
        None,
        help="Where to write detailed output.",
    )

    out_report_dpath = scfg.Value(
        None,
        help="If provided, write filter-step analysis (Sankey + text report) to this directory.",
    )

    dedupe = scfg.Value(
        True,
        help="If True, dedupe identical (suite, run_entry, max_eval_instances) rows.",
    )

    @classmethod
    def main(cls, argv=None, **kwargs):
        """
        Example:
            >>> # It's a good idea to setup a doctest.
            >>> import sys, ubelt
            >>> sys.path.append(ubelt.expandpath('~/code/aiq-magnet/dev/poc'))
            >>> from inspect_historic_helm_runs import *  # NOQA
            >>> argv = False
            >>> kwargs = dict()
            >>> cls = CompileHelmReproListConfig
            >>> config = cls(**kwargs)
            >>> cls.main(argv=argv, **config)
        """
        config = cls.cli(argv=argv, data=kwargs, verbose="auto")
        roots = [Path(r).expanduser() for r in config.roots]
        if not roots:
            raise SystemExit("Must provide at least one root")

        suite_pattern = config.suite_pattern
        run_pattern = config.run_pattern
        require_per_instance_stats = config.require_per_instance_stats
        include_max_eval_instances = config.include_max_eval_instances

        runs, n_structurally_incomplete = gather_runs(
            roots=roots,
            suite_pattern=suite_pattern,
            run_pattern=run_pattern,
            require_per_instance_stats=require_per_instance_stats,
            include_max_eval_instances=include_max_eval_instances,
        )
        rows = build_run_table(
            runs,
            include_max_eval_instances=include_max_eval_instances,
        )
        if config.dedupe:
            rows = dedupe_rows(rows)

        scenario_histo = ub.dict_hist([r['scenario_class'] for r in rows])
        model_histo = ub.dict_hist([r['model'] for r in rows])
        scenario_histo = ub.udict.sorted_values(scenario_histo)
        model_histo = ub.udict.sorted_values(model_histo)
        print(f'scenario_histo = {ub.urepr(scenario_histo, nl=1)}')
        print(f'model_histo = {ub.urepr(model_histo, nl=1)}')

        from helm.benchmark import config_registry
        from helm.benchmark import  model_deployment_registry
        config_registry.register_builtin_configs_from_helm_package()
        model_rows = []
        for model_name, count in model_histo.items():
            HF_CLIENT = 'helm.clients.huggingface_client.HuggingFaceClient'
            try:
                model_meta = model_deployment_registry.get_model_metadata(model_name)
                model_row = model_meta.__dict__ | {'count': count}

                clients = {}
                if model_meta.deployment_names:
                    for deploy_name in model_meta.deployment_names:
                        deploy_info = model_deployment_registry.get_model_deployment(deploy_name)
                        clients[deploy_name] = deploy_info.client_spec.class_name

                model_row['clients'] = clients
                model_row['has_hf_client'] = HF_CLIENT in clients.values()
                model_rows.append(model_row)
            except (TypeError, ValueError) as ex:
                logger.warning(f'missing: model_name = {ub.urepr(model_name, nl=1)} {ex}')
        if 0:
            ub.dict_hist([r.get('client') for r in model_rows])

        # Filter to text models that will fit in memory
        HF_CLIENT = 'helm.clients.huggingface_client.HuggingFaceClient'

        SOFT_TEXT_TAGS = {
            'TEXT_MODEL_TAG',
            'FULL_FUNCTIONALITY_TEXT_MODEL_TAG',
            'INSTRUCTION_FOLLOWING_MODEL_TAG',
        }

        EXCLUDE_TAGS = {
            'VISION_LANGUAGE_MODEL_TAG',
            'AUDIO_LANGUAGE_MODEL_TAG',
            'IMAGE_MODEL_TAG',
            'TEXT_TO_IMAGE_MODEL_TAG',
            'CODE_MODEL_TAG',
        }

        # Keep this conservative if you want, but allow unknown sizes through.
        MAX_PARAMS = 10e9
        # MAX_PARAMS = 200e9

        # Optional manual escape hatch for models that are probably HF-runnable
        # even if HELM currently resolves them to a non-HF deployment.
        KNOWN_HF_OVERRIDES = {
            'qwen/qwen2.5-7b-instruct-turbo',
            'qwen/qwen2-72b-instruct',
            'qwen/qwen2.5-72b-instruct-turbo',
        }

        if 1:
            # check for dropped reasons
            for r in model_rows:
                if 'qwen' in r['name'].lower():
                    tags = set(r.get('tags', []))
                    reasons = []

                    is_text_like = bool(tags & SOFT_TEXT_TAGS)
                    has_excluded_tags = bool(tags & EXCLUDE_TAGS)
                    size_ok = (r.get('num_parameters') is None or r['num_parameters'] <= MAX_PARAMS)
                    access_ok = (r.get('access') == 'open')
                    has_local_hf_path = (
                        r.get('has_hf_client', False) or
                        r['name'] in KNOWN_HF_OVERRIDES
                    )

                    if not is_text_like:
                        reasons.append(f"missing_soft_text_tags={SOFT_TEXT_TAGS - tags}")
                    if has_excluded_tags:
                        reasons.append(f"excluded_tags={tags & EXCLUDE_TAGS}")
                    if not size_ok:
                        reasons.append(f"num_parameters={r.get('num_parameters')}")
                    if not access_ok:
                        reasons.append(f"access={r.get('access')}")
                    if not has_local_hf_path:
                        reasons.append(
                            f"no_local_hf_path has_hf_client={r.get('has_hf_client', False)} "
                            f"override={r['name'] in KNOWN_HF_OVERRIDES}"
                        )

                    print(r['name'])
                    print('  deployment_names =', r.get('deployment_names'))
                    print('  clients =', r.get('clients'))
                    print('  reasons =', reasons)

        chosen_model_rows = []
        for r in model_rows:
            tags = set(r.get('tags', []))

            is_text_like = bool(tags & SOFT_TEXT_TAGS)
            has_excluded_tags = bool(tags & EXCLUDE_TAGS)
            size_ok = (r.get('num_parameters') is None or r['num_parameters'] <= MAX_PARAMS)
            access_ok = (r.get('access') == 'open')
            has_local_hf_path = (
                r.get('has_hf_client', False) or
                r['name'] in KNOWN_HF_OVERRIDES
            )

            if (
                is_text_like and
                not has_excluded_tags and
                size_ok and
                access_ok and
                has_local_hf_path
            ):
                chosen_model_rows.append(r)

        chosen_model_names = {r['name'] for r in chosen_model_rows}
        logger.info('Filter to {} / {} models', len(chosen_model_rows), len(model_rows))

        chosen_rows = [r for r in rows if r['model'] in chosen_model_names]
        logger.info('Filter to {} / {} runs', len(chosen_rows), len(rows))

        # Prepare filter-step analysis data (for report generation)
        model_filter_rows = []  # one dict per model with all failure reasons
        for r in model_rows:
            tags = set(r.get('tags', []))
            is_text_like = bool(tags & SOFT_TEXT_TAGS)
            has_excluded_tags = bool(tags & EXCLUDE_TAGS)
            size_ok = (r.get('num_parameters') is None or r['num_parameters'] <= MAX_PARAMS)
            access_ok = (r.get('access') == 'open')
            has_local_hf_path = (
                r.get('has_hf_client', False) or
                r['name'] in KNOWN_HF_OVERRIDES
            )

            # Collect ALL failing reasons (not just the first)
            failure_reasons = []
            if not is_text_like:
                failure_reasons.append('not-text-like')
            if has_excluded_tags:
                failure_reasons.append('excluded-tags')
            if not size_ok:
                failure_reasons.append('too-large')
            if not access_ok:
                failure_reasons.append('not-open-access')
            if not has_local_hf_path:
                failure_reasons.append('no-hf-deployment')

            eligible = (
                is_text_like and
                not has_excluded_tags and
                size_ok and
                access_ok and
                has_local_hf_path
            )

            model_filter_rows.append({
                'model': r['name'],
                'n_runs': model_histo.get(r['name'], 0),
                'failure_reasons': failure_reasons,
                'eligible': eligible,
            })
        # logger.info(f'chosen_rows = {ub.urepr(chosen_rows, nl=1)}')

        if 1:
            # Show filtered histograms
            scenario_histo = ub.dict_hist([r['scenario_class'] for r in chosen_rows])
            model_histo = ub.dict_hist([r['model'] for r in chosen_rows])
            scenario_histo = ub.udict.sorted_values(scenario_histo)
            model_histo = ub.udict.sorted_values(model_histo)
            logger.info(f'scenario_histo = {ub.urepr(scenario_histo, nl=1)}')
            logger.info(f'model_histo = {ub.urepr(model_histo, nl=1)}')

        # Generate filter-step report if requested
        if config.out_report_dpath:
            from pathlib import Path as PathlibPath
            from helm_audit.utils.sankey import emit_sankey_artifacts

            report_dpath = PathlibPath(config.out_report_dpath).expanduser().resolve()
            report_dpath.mkdir(parents=True, exist_ok=True)

            # Build sankey rows: one row per run per filter failure (if failed), or one row per selected run
            sankey_rows = []
            for _ in range(n_structurally_incomplete):
                sankey_rows.append({'filter_reason': 'structurally-incomplete', 'outcome': 'excluded'})

            for mrow in model_filter_rows:
                n_runs = mrow['n_runs']
                if mrow['eligible']:
                    for _ in range(n_runs):
                        sankey_rows.append({'filter_reason': 'selected', 'outcome': 'selected'})
                else:
                    for reason in mrow['failure_reasons']:
                        for _ in range(n_runs):
                            sankey_rows.append({'filter_reason': reason, 'outcome': 'excluded'})

            # Emit sankey artifacts
            emit_sankey_artifacts(
                rows=sankey_rows,
                report_dpath=report_dpath,
                stamp=__import__('datetime').datetime.now(__import__('datetime').UTC).strftime('%Y%m%dT%H%M%SZ'),
                kind='model_filter',
                title='Run Selection Filter: Which HELM Runs Were Included',
                stage_defs={
                    'filter_reason': [
                        'selected: model passed all 5 eligibility criteria and had complete run data',
                        'structurally-incomplete: run directory missing required files (run_spec.json, stats.json, etc)',
                        'not-text-like: model has no text-compatible tags (vision, audio, or image model)',
                        'excluded-tags: model tagged as vision/audio/image/code which we exclude',
                        'too-large: model size exceeds 10 billion parameters (conservative for local execution)',
                        'not-open-access: model access is not "open" in HELM registry',
                        'no-hf-deployment: model has no HuggingFace deployment and not in known overrides',
                    ],
                    'outcome': [
                        'selected: run was included in reproduction list',
                        'excluded: run was excluded from reproduction list',
                    ],
                },
                stage_order=[('filter_reason', 'Exclusion Criterion'), ('outcome', 'Outcome')],
            )

            # Write text report
            text_lines = ['Model Selection Filter Report', '']
            text_lines.append(f'Total discovered runs: {n_structurally_incomplete + len(rows)}')
            text_lines.append(f'Structurally complete runs: {len(rows)}')
            text_lines.append(f'Structurally incomplete runs: {n_structurally_incomplete}')
            text_lines.append('')
            text_lines.append(f'Total models in complete runs: {len(model_rows)}')
            text_lines.append(f'Selected models (passed all filters): {len(chosen_model_rows)}')
            text_lines.append(f'Selected runs: {len(chosen_rows)}')
            text_lines.append('')
            text_lines.append('Filter Criteria Statistics:')

            # Count runs affected by each filter
            filter_counts = {}
            for mrow in model_filter_rows:
                for reason in mrow['failure_reasons']:
                    filter_counts[reason] = filter_counts.get(reason, 0) + mrow['n_runs']

            for reason in sorted(filter_counts.keys()):
                text_lines.append(f'  {reason}: {filter_counts[reason]} runs')

            report_txt_fpath = report_dpath / 'model_filter_report.txt'
            report_txt_fpath.write_text('\n'.join(text_lines) + '\n')
            logger.success('Wrote filter report: {}', report_dpath)

        if config.out_detail_fpath:
            text = kwutil.Yaml.dumps(chosen_rows)
            Path(config.out_detail_fpath).write_text(text)
            logger.success("Wrote {}", config.out_detail_fpath)

        run_spec_names = [r["run_spec_name"] for r in chosen_rows]
        text = kwutil.Yaml.dumps(run_spec_names)
        if config.out_fpath:
            Path(config.out_fpath).write_text(text)
            logger.success("Wrote {}", config.out_fpath)
        else:
            print(text, end="")


def gather_runs(
    roots: Iterable[Path],
    suite_pattern: str = "*",
    run_pattern: str = "*:*",
    require_per_instance_stats: bool = False,
    include_max_eval_instances: bool = True,
) -> tuple[list[HelmRun], int]:

    # Discover all benchmark_output dirs under provided roots
    logger.info('Discover benchmarks')
    bo_dirs = list(ub.ProgIter(discover_benchmark_output_dirs(roots), desc='discovering benchmarks', verbose=3, homogeneous=False))
    logger.info('Finished Discover benchmarks')
    if not bo_dirs:
        logger.warning("No benchmark_output dirs found under roots={}", roots)

    runs: list[HelmRun] = []
    n_structurally_incomplete = 0
    for bo in ub.ProgIter(bo_dirs, desc='Check dirs'):
        try:
            outputs = HelmOutputs.coerce(bo)
        except Exception:
            continue

        for suite in outputs.suites(pattern=suite_pattern):
            for run in suite.runs(pattern=run_pattern):
                run_dir = Path(run.path)

                run = HelmRun(run_dir)

                # TODO: if not run.exists():
                #     ...
                # Only include if it looks "complete enough"
                if not is_complete_run_dir(run_dir, require_per_instance_stats=require_per_instance_stats):
                    n_structurally_incomplete += 1
                    continue

                runs.append(run)

    # Stable order
    logger.info('Found {} run directories', len(runs))
    return runs, n_structurally_incomplete


def build_run_table(
    runs: list[HelmRun],
    *,
    include_max_eval_instances: bool = False,
) -> list[dict]:
    rows = []
    mismatches = []
    for run in ub.ProgIter(runs, desc='Extract run spec info'):
        max_eval_instances = None
        if include_max_eval_instances:
            max_eval_instances = infer_num_instances(run.path)

        # Not sure if there is an advantage to msgspec or json here
        # ZFS is likely messing up my timings.
        if 1:
            run_spec = run.json.run_spec()
            scenario_class = run_spec['scenario_spec']['class_name']
            model = run_spec['adapter_spec']['model']
            run_spec_name = run_spec['name']
        else:
            run_spec = run.msgspec.run_spec()
            scenario_class = run_spec.scenario_spec.class_name
            model = run_spec.adapter_spec.model
            run_spec_name = run_spec.name

        if run.path.name != run_spec_name.replace('/', '_'):
            mismatches.append({
                'run.path.parent': run.path.parent,
                'run.path.name': run.path.name,
                'run_spec_name': run_spec_name,
            })

        # Hack: run spec names sometimes don't correctly encode the model
        FIX_RUN_SPEC_NAME = True
        if FIX_RUN_SPEC_NAME:
            normalized_model = model.replace('/', '_')
            run_spec_name = run_spec_name.replace(normalized_model, model)

        rows.append({
            # "benchmark_output_dir": str(Path(outputs.root_dir)),
            # "suite": suite.name,
            # # Use run directory name as the canonical "run_entry" to reproduce.
            # # This is faithful even if HELM normalized defaults into the name.

            # Use run directory name as the canonical "run_entry" to reproduce.
            # This is faithful even if HELM normalized defaults into the name.
            "run_spec_name": run_spec_name,
            "run_dir": str(run.path),
            "max_eval_instances": max_eval_instances,
            'model': model,
            'scenario_class': scenario_class,
        })
    logger.warning(f'mismatches = {ub.urepr(mismatches, nl=2, align=":")}')
    rows.sort(key=lambda r: (r["run_dir"]))
    return rows


def dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    out = []
    for r in rows:
        key = (r["run_spec_name"], r.get("max_eval_instances", None))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


__cli__ = CompileHelmReproListConfig

if __name__ == "__main__":
    __cli__.main()
