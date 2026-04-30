r"""
Compile a reproduction list from existing HELM outputs on disk.

Given one or more roots that contain HELM outputs, discover all run directories
and emit a list of run specs you can feed into kwdagger / helm-run.

Outputs are structured so you can:
- reproduce exact run directories (by using run_entry == run directory name)
- optionally include max_eval_instances inferred from per_instance_stats.json

Ignore:

    ls /data/crfm-helm-public/thaiexam/benchmark_output/runs/v1.1.0/thai_exam:exam=tpat1,method=multiple_choice_joint,model=aisingapore_llama3-8b-cpt-sea-lionv2.1-instruct

    python -m eval_audit.cli.index_historic_helm_runs /data/crfm-helm-public --out_fpath /data/crfm-helm-audit-store/configs/run_specs.yaml --out_detail_fpath /data/crfm-helm-audit-store/configs/run_details.yaml --out_inventory_json /data/crfm-helm-audit-store/analysis/filter_inventory.json

    cat /data/crfm-helm-audit-store/configs/run_specs.yaml | grep -v together > run_specs2.yaml

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
import fnmatch
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Any

import ubelt as ub
import kwutil
import scriptconfig as scfg
from loguru import logger

from eval_audit.infra.logging import setup_cli_logging
from eval_audit.infra.api import repo_run_details_fpath, repo_run_specs_fpath
from eval_audit.helm.run_entries import (
    parse_run_entry_description,
    parse_run_name_to_kv,
    reconstruct_run_entry_from_run_spec,
)
from eval_audit.indexing.schema import (
    KNOWN_STRUCTURAL_JUNK_NAMES,
    OFFICIAL_COMPONENT_COLUMNS,
    classify_run_entry as _classify_run_entry_impl,
    component_id_for_official,
    compute_run_spec_hash as _compute_run_spec_hash_impl,
    extract_run_spec_fields,
    logical_run_key_for_official,
    normalize_for_hash as _normalize_for_hash_impl,
    now_utc_iso,
)
from eval_audit.model_registry import local_model_registry_by_name


MISSING_MODEL_METADATA_REASON = 'missing-model-metadata'
CLOSED_JUDGE_REQUIRED_REASON = 'requires-closed-judge'
GATED_DATASET_REASON = 'requires-gated-dataset'

CLOSED_JUDGE_BENCHMARKS = {
    'anthropic_red_team',
    'harm_bench',
    'omni_math',
    'simple_safety_tests',
    'wildbench',
    'xstest',
}

GATED_DATASET_BENCHMARKS = {
    'gpqa',
}


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
        str(repo_run_specs_fpath()),
        help="Where to write selected run specs. Defaults to $AUDIT_STORE_ROOT/configs/run_specs.yaml.",
    )

    out_detail_fpath = scfg.Value(
        str(repo_run_details_fpath()),
        help="Where to write detailed rows. Defaults to $AUDIT_STORE_ROOT/configs/run_details.yaml.",
    )

    out_report_dpath = scfg.Value(
        None,
        help="Deprecated. Reporting is now handled by eval_audit.cli.reports filter.",
    )

    out_inventory_json = scfg.Value(
        None,
        help="If provided, write the full filter inventory as JSON for later analysis.",
    )

    out_official_index_dpath = scfg.Value(
        None,
        help=(
            "If provided, emit the canonical official/public index as a timestamped CSV "
            "plus a .csv symlink in this directory.  This index captures ALL "
            "public HELM run entries (including structural junk) with explicit "
            "public_track and suite_version provenance. "
            "It is separate from Stage 1 selected-run artifacts."
        ),
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
            >>> from eval_audit.cli.index_historic_helm_runs import *  # NOQA
            >>> argv = False
            >>> kwargs = dict()
            >>> cls = CompileHelmReproListConfig
            >>> config = cls(**kwargs)
            >>> cls.main(argv=argv, **config)
        """
        setup_cli_logging()
        config = cls.cli(argv=argv, data=kwargs, verbose="auto")
        roots = [Path(r).expanduser() for r in config.roots]
        if not roots:
            raise SystemExit("Must provide at least one root")

        suite_pattern = config.suite_pattern
        run_pattern = config.run_pattern
        require_per_instance_stats = config.require_per_instance_stats
        include_max_eval_instances = config.include_max_eval_instances
        if config.out_report_dpath:
            raise SystemExit(
                '--out_report_dpath is no longer supported here. '
                'Use --out_inventory_json to save the Stage 1 inventory, then run '
                '`python -m eval_audit.cli.reports filter --report-dpath <reports/filtering> '
                '--inventory-json <inventory.json>`.'
            )

        runs, incomplete_rows = gather_runs(
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
        missing_model_metadata: dict[str, str] = {}
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
                missing_model_metadata[model_name] = str(ex)
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

        chosen_rows = []
        for row in rows:
            if row['model'] not in chosen_model_names:
                continue
            run_failure_reason_details = build_run_failure_reason_details(
                benchmark=describe_run_spec(row['run_spec_name'], row.get('scenario_class'))['benchmark'],
            )
            if run_failure_reason_details:
                continue
            chosen_rows.append(row)
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
                failure_reasons.append('no-local-helm-deployment')

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
                'failure_reason_details': build_failure_reason_details(
                    tags=tags,
                    is_text_like=is_text_like,
                    has_excluded_tags=has_excluded_tags,
                    size_ok=size_ok,
                    access_ok=access_ok,
                    has_local_hf_path=has_local_hf_path,
                    num_parameters=r.get('num_parameters'),
                    access=r.get('access'),
                    has_hf_client=r.get('has_hf_client', False),
                    model_name=r['name'],
                    known_hf_overrides=KNOWN_HF_OVERRIDES,
                    max_params=MAX_PARAMS,
                    exclude_tags=EXCLUDE_TAGS,
                ),
                'eligible': eligible,
                'num_parameters': r.get('num_parameters'),
                'access': r.get('access'),
                'tags': sorted(tags),
                'has_hf_client': r.get('has_hf_client', False),
                'size_threshold_params': MAX_PARAMS,
            })

        if 1:
            # Which open models are we missing due to providers
            for r in model_filter_rows:
                nonblocking_reasons =  {'no-local-helm-deployment'}
                blocking_reasons = {'too-large', 'not-open-access', 'not-text-like'}
                if len(set(r['failure_reasons']) - nonblocking_reasons) == 0:
                    print(r)
                if len(set(r['failure_reasons']) & blocking_reasons) == 0:
                    break
                    ...

        for model_name, error_text in missing_model_metadata.items():
            model_filter_rows.append({
                'model': model_name,
                'n_runs': model_histo.get(model_name, 0),
                'failure_reasons': [MISSING_MODEL_METADATA_REASON],
                'failure_reason_details': {
                    MISSING_MODEL_METADATA_REASON: (
                        'HELM could not resolve model metadata for this model name via '
                        f'model_deployment_registry: {error_text}'
                    ),
                },
                'eligible': False,
                'num_parameters': None,
                'access': None,
                'tags': [],
                'has_hf_client': False,
                'size_threshold_params': MAX_PARAMS,
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
        inventory_rows = None
        if config.out_inventory_json:
            inventory_rows = build_filter_inventory_rows(
                complete_rows=rows,
                incomplete_rows=incomplete_rows,
                model_filter_rows=model_filter_rows,
                chosen_model_names=chosen_model_names,
            )
            inventory_fpath = Path(config.out_inventory_json).expanduser().resolve()
            inventory_fpath.parent.mkdir(parents=True, exist_ok=True)
            inventory_fpath.write_text(
                json.dumps(
                    kwutil.Json.ensure_serializable(inventory_rows),
                    indent=2,
                    ensure_ascii=False,
                    default=str,
                ) + '\n'
            )
            logger.success("Wrote ⚙ {}", inventory_fpath)

        if config.out_official_index_dpath:
            official_rows = build_official_public_index_rows(
                roots=roots,
                suite_pattern=suite_pattern,
            )
            ts_fpath, latest_fpath = write_official_public_index(
                rows=official_rows,
                out_dpath=Path(config.out_official_index_dpath).expanduser().resolve(),
            )
            logger.success(
                "Wrote official public index {} ({} rows)",
                ts_fpath, len(official_rows),
            )
            logger.success("Latest alias: {}", latest_fpath)

        if config.out_detail_fpath:
            text = kwutil.Yaml.dumps(chosen_rows)
            ub.Path(config.out_detail_fpath).parent.ensuredir()
            Path(config.out_detail_fpath).write_text(text)
            logger.success("Wrote ⚙ {}", config.out_detail_fpath)

        run_spec_names = [r["run_spec_name"] for r in chosen_rows]
        text = kwutil.Yaml.dumps(run_spec_names)
        if config.out_fpath:
            Path(config.out_fpath).write_text(text)
            logger.success("Wrote ⚙ {}", config.out_fpath)
        else:
            print(text, end="")


def gather_runs(
    roots: Iterable[Path],
    suite_pattern: str = "*",
    run_pattern: str = "*:*",
    require_per_instance_stats: bool = False,
    include_max_eval_instances: bool = True,
) -> tuple[list[Any], list[dict[str, Any]]]:
    from magnet.backends.helm.helm_outputs import HelmOutputs, HelmRun
    from magnet.backends.helm.cli.materialize_helm_run import (
        discover_benchmark_output_dirs,
        is_complete_run_dir,
    )

    # Discover all benchmark_output dirs under provided roots
    logger.info('Discover benchmarks')
    bo_dirs = list(ub.ProgIter(discover_benchmark_output_dirs(roots), desc='discovering benchmarks', verbose=3, homogeneous=False))
    logger.info('Finished Discover benchmarks')
    if not bo_dirs:
        logger.warning("No benchmark_output dirs found under roots={}", roots)

    runs: list[HelmRun] = []
    incomplete_rows: list[dict[str, Any]] = []
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
                    incomplete_rows.append(build_incomplete_inventory_row(run_dir))
                    continue

                runs.append(run)

    # Stable order
    logger.info('Found {} run directories', len(runs))
    return runs, incomplete_rows


def build_run_table(
    runs: list[Any],
    *,
    include_max_eval_instances: bool = False,
) -> list[dict]:
    from magnet.backends.helm.cli.materialize_helm_run import infer_num_instances

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
            display_name = run_spec['name']
        else:
            run_spec = run.msgspec.run_spec()
            scenario_class = run_spec.scenario_spec.class_name
            model = run_spec.adapter_spec.model
            display_name = run_spec.name

        if run.path.name != display_name.replace('/', '_'):
            mismatches.append({
                'run.path.parent': run.path.parent,
                'run.path.name': run.path.name,
                'run_spec_name': display_name,
            })

        # HELM's `run_spec.json.name` is a display string and is NOT a
        # valid `helm-run --run-entries` argument across the board (mixed
        # separators, display-vs-kwarg renames, leaked metadata fields).
        # Reconstruct from the structural fields so the audit list we
        # emit round-trips through helm-run cleanly. Falls back to the
        # display name if the registry lookup or signature introspection
        # fails — the legacy "fix the model slash" hack is preserved as
        # the fallback path.
        run_spec_name, dropped_kwargs = reconstruct_run_entry_from_run_spec(run_spec)
        if dropped_kwargs:
            logger.debug(
                'Dropped non-arg kwargs while reconstructing run_entry for {}: {}',
                run.path.name, dropped_kwargs,
            )
        if run_spec_name == display_name:
            # Reconstruction declined to rewrite — apply the legacy
            # underscore-to-slash fixup so the model field is still
            # canonical when fed back into helm-run.
            normalized_model = model.replace('/', '_')
            run_spec_name = display_name.replace(normalized_model, model)

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


def format_params_human(num_params: float | int | None) -> str:
    if num_params is None:
        return 'unknown'
    value = float(num_params)
    if value >= 1e9:
        return f'{value / 1e9:.1f}B'
    if value >= 1e6:
        return f'{value / 1e6:.1f}M'
    return str(int(value))


def build_failure_reason_details(
    *,
    tags: set[str],
    is_text_like: bool,
    has_excluded_tags: bool,
    size_ok: bool,
    access_ok: bool,
    has_local_hf_path: bool,
    num_parameters: float | int | None,
    access: str | None,
    has_hf_client: bool,
    model_name: str,
    known_hf_overrides: set[str],
    max_params: float,
    exclude_tags: set[str],
) -> dict[str, str]:
    details: dict[str, str] = {}
    if not is_text_like:
        details['not-text-like'] = (
            'Model does not advertise any of the required text-compatible HELM tags.'
        )
    if has_excluded_tags:
        details['excluded-tags'] = (
            'Model carries excluded modality tags: ' + ', '.join(sorted(tags & exclude_tags))
        )
    if not size_ok:
        details['too-large'] = (
            f"Model size {format_params_human(num_parameters)} exceeds the local reproduction budget "
            f"of {format_params_human(max_params)} parameters."
        )
    if not access_ok:
        details['not-open-access'] = (
            f'Model access is {access!r}; the filter requires HELM access="open".'
        )
    if not has_local_hf_path:
        details['no-local-helm-deployment'] = (
            f'Model has_hf_client={has_hf_client} and override={model_name in known_hf_overrides}; '
            'no default local HELM deployment path is known to the Stage 1 automatic filter.'
        )
    return details


def build_run_failure_reason_details(*, benchmark: str) -> dict[str, str]:
    details: dict[str, str] = {}
    if benchmark in CLOSED_JUDGE_BENCHMARKS:
        details[CLOSED_JUDGE_REQUIRED_REASON] = (
            'Benchmark requires a proprietary / credentialed judge or annotator path; '
            'that closed-source evaluation dependency is currently out of scope for the '
            'local open-model reproduction recipe.'
        )
    if benchmark in GATED_DATASET_BENCHMARKS:
        details[GATED_DATASET_REASON] = (
            'Benchmark requires a gated dataset that is not part of the default '
            'local open-model reproduction recipe.'
        )
    return details


def short_scenario_name(scenario_class: str | None) -> str:
    if not scenario_class:
        return 'UnknownScenario'
    return scenario_class.rsplit('.', 1)[-1]


def describe_run_spec(run_spec_name: str, scenario_class: str | None = None) -> dict[str, Any]:
    benchmark = run_spec_name.split(':', 1)[0]
    kv = parse_run_name_to_kv(run_spec_name)[1]
    try:
        benchmark, parsed_kv = parse_run_entry_description(run_spec_name)
        kv = {str(k): parsed_kv[k] for k in parsed_kv}
    except Exception:
        pass

    dataset_key = None
    for key in [
        'dataset',
        'subset',
        'subject',
        'task',
        'demographic',
        'domain',
        'language_pair',
        'lang',
        'mode',
        'difficulty',
        'k',
        'level',
    ]:
        if key in kv:
            dataset_key = key
            break
    dataset = benchmark if dataset_key is None else f'{dataset_key}={kv[dataset_key]}'

    non_model_items = [
        f'{key}={value}' if value is not True else str(key)
        for key, value in kv.items()
        if key != 'model'
    ]
    setting = benchmark if not non_model_items else f'{benchmark}:' + ','.join(non_model_items)
    return {
        'benchmark': benchmark,
        'dataset': dataset,
        'dataset_key': dataset_key,
        'setting': setting,
        'scenario': short_scenario_name(scenario_class) if scenario_class else benchmark,
        'run_params': kv,
    }


def build_incomplete_inventory_row(run_dir: Path) -> dict[str, Any]:
    run_name = run_dir.name
    benchmark, kv = parse_run_name_to_kv(run_name)
    model = kv.get('model')
    if isinstance(model, str):
        model = model.replace('_', '/')
    dataset_key = None
    for key in ['dataset', 'subset', 'subject', 'task', 'demographic', 'domain', 'language_pair', 'lang', 'mode', 'difficulty', 'k', 'level']:
        if key in kv:
            dataset_key = key
            break
    dataset = benchmark if dataset_key is None else f'{dataset_key}={kv[dataset_key]}'
    return {
        'run_spec_name': run_name,
        'run_dir': str(run_dir),
        'max_eval_instances': None,
        'model': model,
        'scenario_class': None,
        'benchmark': benchmark or 'unknown',
        'dataset': dataset,
        'dataset_key': dataset_key,
        'setting': run_name,
        'scenario': benchmark or 'unknown',
        'run_params': kv,
        'selection_status': 'excluded',
        'outcome': 'excluded',
        'considered_for_selection': False,
        'eligible_candidate': False,
        'candidate_pool': 'structurally-incomplete',
        'eligible_model': False,
        'failure_reasons': ['structurally-incomplete'],
        'failure_reason_summary': 'structurally-incomplete',
        'selection_explanation': 'Excluded before candidate selection because the run directory was structurally incomplete.',
        'is_structurally_incomplete': True,
    }


def build_filter_inventory_rows(
    *,
    complete_rows: list[dict[str, Any]],
    incomplete_rows: list[dict[str, Any]],
    model_filter_rows: list[dict[str, Any]],
    chosen_model_names: set[str],
) -> list[dict[str, Any]]:
    model_info = {row['model']: row for row in model_filter_rows}
    registry = local_model_registry_by_name()
    inventory_rows: list[dict[str, Any]] = []
    for row in complete_rows:
        info = describe_run_spec(row['run_spec_name'], row.get('scenario_class'))
        model_meta = model_info.get(row['model'], {})
        model_failure_reasons = list(model_meta.get('failure_reasons', []))
        model_failure_reason_details = dict(model_meta.get('failure_reason_details', {}))
        run_failure_reason_details = build_run_failure_reason_details(benchmark=info['benchmark'])
        run_failure_reasons = list(run_failure_reason_details)
        failure_reasons = model_failure_reasons + [
            reason for reason in run_failure_reasons if reason not in model_failure_reasons
        ]
        failure_reason_details = model_failure_reason_details | run_failure_reason_details
        eligible_model = bool(model_meta.get('eligible', False))
        eligible_candidate = eligible_model and not run_failure_reasons
        candidate_pool = 'complete-run'
        if eligible_model:
            candidate_pool = 'eligible-model' if not run_failure_reasons else 'eligible-model-out-of-scope'
        selected = row['model'] in chosen_model_names and not run_failure_reasons
        reg_entry = registry.get(row['model'])
        inventory_rows.append({
            **row,
            **info,
            'selection_status': 'selected' if selected else 'excluded',
            'outcome': 'selected' if selected else 'excluded',
            'considered_for_selection': True,
            'eligible_candidate': eligible_candidate,
            'candidate_pool': candidate_pool,
            'eligible_model': eligible_model,
            'failure_reasons': failure_reasons,
            'failure_reason_details': failure_reason_details,
            'failure_reason_summary': 'selected' if selected else '|'.join(failure_reasons),
            'selection_explanation': (
                'Selected because the run was structurally complete and its model passed all eligibility filters.'
                if selected else
                'Excluded after consideration because the run failed the current reproduction filters: '
                + '; '.join(
                    failure_reason_details.get(reason, reason)
                    for reason in failure_reasons
                ) + '.'
            ),
            'model_num_parameters': model_meta.get('num_parameters'),
            'model_access': model_meta.get('access'),
            'model_tags': model_meta.get('tags', []),
            'model_has_hf_client': model_meta.get('has_hf_client'),
            'size_threshold_params': model_meta.get('size_threshold_params'),
            'is_structurally_incomplete': False,
            'expected_local_served': reg_entry.expected_local_served if reg_entry else False,
            'replaces_helm_deployment': reg_entry.replaces_helm_deployment if reg_entry else None,
            'local_registry_source': reg_entry.source if reg_entry else None,
        })
    inventory_rows.extend(incomplete_rows)
    inventory_rows.sort(key=lambda row: (row['selection_status'], str(row.get('model')), row['run_spec_name']))
    return inventory_rows


# ---------------------------------------------------------------------------
# Official/public index — version-aware canonical artifact
# ---------------------------------------------------------------------------

#: Backwards-compat alias for the canonical official-index column order.
OFFICIAL_INDEX_COLUMNS: list[str] = OFFICIAL_COMPONENT_COLUMNS


def _normalize_for_hash(obj: Any) -> Any:
    """Back-compat shim for ``eval_audit.indexing.schema.normalize_for_hash``."""
    return _normalize_for_hash_impl(obj)


def _compute_run_spec_hash(run_spec_fpath: Path) -> str | None:
    """Back-compat shim for ``eval_audit.indexing.schema.compute_run_spec_hash``."""
    return _compute_run_spec_hash_impl(run_spec_fpath)


def _classify_run_entry(entry_name: str) -> tuple[str, bool]:
    """Back-compat shim for ``eval_audit.indexing.schema.classify_run_entry``."""
    return _classify_run_entry_impl(entry_name)


def _scan_benchmark_output_dir(
    bo_dir: Path,
    public_root: str | None,
    public_track: str,
    suite_pattern: str = '*',
    index_generated_utc: str = '',
) -> list[dict[str, Any]]:
    """
    Scan a single benchmark_output directory and return official index rows.

    This is the inner loop extracted so it can be unit-tested without magnet.
    Emits component-style rows whose schema matches
    :data:`eval_audit.indexing.schema.OFFICIAL_COMPONENT_COLUMNS`.
    """
    rows: list[dict[str, Any]] = []
    runs_dir = bo_dir / 'runs'
    if not runs_dir.is_dir():
        return rows

    for suite_dir in sorted(runs_dir.iterdir()):
        if not suite_dir.is_dir():
            continue
        suite_version = suite_dir.name
        if suite_pattern != '*' and not fnmatch.fnmatch(suite_version, suite_pattern):
            continue

        for entry_dir in sorted(suite_dir.iterdir()):
            if not entry_dir.is_dir():
                continue
            run_name = entry_dir.name
            entry_kind, is_structural_junk = _classify_run_entry_impl(run_name)

            run_spec_fpath = entry_dir / 'run_spec.json'
            spec_fields = extract_run_spec_fields(run_spec_fpath)
            has_run_spec_json = spec_fields['has_run_spec_json']
            # For benchmark runs, fall back to directory-name prefix if the
            # spec didn't yield a benchmark group.
            benchmark_group = spec_fields['benchmark_group']
            if benchmark_group is None and ':' in run_name:
                benchmark_group = run_name.split(':', 1)[0]

            rows.append({
                'source_kind': 'official',
                'artifact_format': 'helm',
                'eee_artifact_path': None,
                'component_id': component_id_for_official(
                    public_track=public_track,
                    suite_version=suite_version,
                    run_name=run_name,
                ),
                'logical_run_key': logical_run_key_for_official(
                    run_spec_name=spec_fields['run_spec_name'],
                    run_name=run_name,
                ),
                'public_root': public_root,
                'public_track': public_track,
                'suite_version': suite_version,
                'public_run_dir': str(entry_dir),
                'run_path': str(entry_dir),
                'run_name': run_name,
                'entry_kind': entry_kind,
                'has_run_spec_json': has_run_spec_json,
                'run_spec_fpath': str(run_spec_fpath) if has_run_spec_json else None,
                'run_spec_name': spec_fields['run_spec_name'],
                'run_spec_hash': spec_fields['run_spec_hash'],
                'model': spec_fields['model'],
                'model_deployment': spec_fields['model_deployment'],
                'scenario_class': spec_fields['scenario_class'],
                'benchmark_group': benchmark_group,
                'max_eval_instances': None,
                'is_structural_junk': is_structural_junk,
                'index_generated_utc': index_generated_utc,
            })

    return rows


def build_official_public_index_rows(
    roots: list[Path],
    suite_pattern: str = '*',
    index_generated_utc: str | None = None,
) -> list[dict[str, Any]]:
    """
    Scan public HELM roots and build the canonical version-aware official index.

    Unlike gather_runs(), this function:
    - Does NOT filter for run completeness.
    - Records every directory entry including structural junk.
    - Preserves explicit public_track and suite_version provenance.
    - Computes a stable run_spec_hash from normalised run_spec.json content.
    """
    from magnet.backends.helm.cli.materialize_helm_run import discover_benchmark_output_dirs

    if index_generated_utc is None:
        index_generated_utc = now_utc_iso()

    bo_dirs = list(ub.ProgIter(
        discover_benchmark_output_dirs(roots),
        desc='discovering benchmark_output dirs for official index',
        verbose=3,
        homogeneous=False,
    ))

    rows: list[dict[str, Any]] = []
    for bo_dir in ub.ProgIter(bo_dirs, desc='Building official public index'):
        bo_dir = Path(bo_dir)
        public_root: str | None = None
        public_track = 'unknown'
        for root in roots:
            try:
                rel = bo_dir.parent.relative_to(root)
                public_root = str(root)
                parts = rel.parts
                public_track = '/'.join(parts) if parts else 'main'
                break
            except ValueError:
                continue

        rows.extend(_scan_benchmark_output_dir(
            bo_dir=bo_dir,
            public_root=public_root,
            public_track=public_track,
            suite_pattern=suite_pattern,
            index_generated_utc=index_generated_utc,
        ))

    rows.sort(key=lambda r: (r['public_track'], r['suite_version'], r['run_name']))
    return rows


def write_official_public_index(
    rows: list[dict[str, Any]],
    out_dpath: Path,
    timestamp: str | None = None,
) -> tuple[Path, Path]:
    """
    Write the official public index to ``official_public_index.csv``.

    The ``timestamp`` argument is preserved for backwards compatibility
    with callers but is no longer used: the post-history-retirement
    publishing model writes the canonical artifact directly to
    ``out_dpath / 'official_public_index.csv'`` and overwrites it
    atomically. Returns ``(latest_fpath, latest_fpath)``; the duplicated
    return is preserved so existing call-sites unpack two values.
    """
    import io

    import pandas as pd
    import safer

    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    del timestamp  # currently unused; preserved as an arg for callers

    out_dpath.mkdir(parents=True, exist_ok=True)
    latest_fpath = out_dpath / 'official_public_index.csv'

    df = pd.DataFrame(rows)
    for col in OFFICIAL_COMPONENT_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[OFFICIAL_COMPONENT_COLUMNS]
    # pandas .to_csv accepts a file-like; use safer.open so a crash mid-write
    # leaves the previous official_public_index.csv intact.
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    with safer.open(latest_fpath, 'w', make_parents=True) as fp:
        fp.write(buf.getvalue())
    # Returns (path, path) for backward-compat with callers that expected
    # (timestamped_fpath, latest_fpath) — both now the same canonical file.
    return latest_fpath, latest_fpath


__cli__ = CompileHelmReproListConfig

if __name__ == "__main__":
    setup_cli_logging()
    __cli__.main()
