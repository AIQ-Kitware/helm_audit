from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Iterator


def parse_run_entry_description(desc: str) -> tuple[str, dict[str, object]]:
    if ":" not in desc:
        raise ValueError(
            "Run entry description must contain ':' separating benchmark and parameters"
        )
    from helm.common.object_spec import parse_object_spec

    spec = parse_object_spec(desc)
    return spec.class_name, spec.args


def parse_run_name_to_kv(run_name: str) -> tuple[str, dict[str, object]]:
    if ":" not in run_name:
        return "", {}
    bench, rest = run_name.split(":", 1)
    bench = bench.strip()
    kv: dict[str, object] = {}
    rest = rest.strip()
    if rest:
        for part in rest.split(","):
            part = part.strip()
            if not part:
                continue
            if "=" in part:
                k, v = part.split("=", 1)
                kv[k.strip()] = v.strip()
            else:
                kv[part] = True
    return bench, kv


def format_run_name_from_kv(bench: str, kv: dict[str, object]) -> str:
    parts: list[str] = []
    for key, value in kv.items():
        if value is True:
            parts.append(str(key))
        else:
            parts.append(f"{key}={value}")
    if not bench:
        return ",".join(parts)
    if not parts:
        return bench
    return f"{bench}:{','.join(parts)}"


def normalize_run_entry_for_historic_lookup(run_entry: str) -> str:
    """
    Normalize local bookkeeping-only fields away when matching against public
    historic HELM runs.

    In particular, local runs may include a machine/deployment-specific
    ``model_deployment=...`` suffix that has no public historic counterpart.
    """
    bench, kv = parse_run_name_to_kv(run_entry)
    if not bench:
        return run_entry
    if "model_deployment" not in kv:
        return run_entry
    kv = dict(kv)
    kv.pop("model_deployment", None)
    return format_run_name_from_kv(bench, kv)


# HELM display-name kwarg aliases keyed by benchmark family. Some HELM
# run-spec functions accept one kwarg name but write a *different* token
# into the run_spec.name display string — e.g. ``mmlu_pro`` accepts
# ``subject`` but emits ``subset=...``. Mirrors the table in
# ``aiq-magnet/.../materialize_helm_run.py``; keep them in sync.
_BENCHMARK_KWARG_ALIASES: dict[str, dict[str, str]] = {
    "mmlu_pro": {"subject": "subset"},
}


def canonicalize_kv(kv: dict[str, object], benchmark: str | None = None) -> dict[str, object]:
    kv = dict(kv)
    # HELM run dirs replace ``/`` with ``_`` in model and model_deployment
    # tokens. Mirror that here so requested-vs-candidate matches survive
    # the round-trip.
    for key in ("model", "model_deployment"):
        value = kv.get(key, None)
        if isinstance(value, str):
            kv[key] = value.replace("/", "_")
    aliases = _BENCHMARK_KWARG_ALIASES.get(benchmark or "", {})
    for src, dst in aliases.items():
        if src in kv and dst not in kv:
            kv[dst] = kv.pop(src)
    return kv


def run_dir_matches_requested(run_dir_name: str, requested_desc: str) -> bool:
    req_bench, req_kv = parse_run_name_to_kv(requested_desc)
    cand_bench, cand_kv = parse_run_name_to_kv(run_dir_name)
    if req_bench != cand_bench:
        return False

    req_kv = canonicalize_kv(req_kv, benchmark=req_bench)
    cand_kv = canonicalize_kv(cand_kv, benchmark=cand_bench)
    for k, v in req_kv.items():
        if k not in cand_kv:
            return False
        if cand_kv[k] != v:
            return False
    return True


@lru_cache(maxsize=1)
def _registered_run_spec_function_names() -> tuple[str, ...]:
    """Return all run-spec-function names registered with HELM (cached)."""
    from helm.benchmark.run_spec import (
        _REGISTERED_RUN_SPEC_FUNCTIONS,
        discover_run_spec_functions,
    )
    discover_run_spec_functions()
    return tuple(_REGISTERED_RUN_SPEC_FUNCTIONS.keys())


@lru_cache(maxsize=1)
def _registered_run_expander_names() -> frozenset[str]:
    """Return all HELM RunExpander ``name`` tags (cached).

    RunExpanders are meta-kwargs the run-entry parser intercepts *before*
    calling the run-spec-function (e.g. ``data_augmentation``, ``eval_split``,
    ``temperature``). They are valid run-entry kwargs even though they don't
    appear in any run-spec-function signature — preserve them during
    reconstruction.
    """
    from helm.benchmark.run_expander import RUN_EXPANDERS
    return frozenset(RUN_EXPANDERS.keys())


# kwargs HELM appends to a run_spec.name as pure display decoration *after*
# the run_spec is constructed. They are never valid as run-entry kwargs;
# always drop them silently when reconstructing.
_DISPLAY_ONLY_DECORATIONS: frozenset[str] = frozenset({"groups"})


def _resolve_registry_name_from_display(display_name: str) -> str | None:
    """Find the run-spec-function registry name that prefixes a display name.

    HELM uses ``:``, ``,``, and ``_`` inconsistently as the separator between
    the scenario name and its first kwarg in the *display* string (the
    ``run_spec.json`` ``name`` field). Returns the longest registered name
    ``N`` such that ``display_name == N`` or
    ``display_name[len(N)] in {':', ',', '_'}``. ``None`` if no registered
    name matches.
    """
    if not display_name:
        return None
    candidates = [
        n for n in _registered_run_spec_function_names()
        if display_name == n
        or (
            display_name.startswith(n)
            and len(display_name) > len(n)
            and display_name[len(n)] in (':', ',', '_')
        )
    ]
    return max(candidates, key=len) if candidates else None


def _extract_display_kwargs(display_name: str, registry_name: str) -> dict[str, str]:
    """Best-effort parse of ``key=value`` tokens from the display-name suffix.

    Splits on ``,`` and ``:`` only — never on ``_`` (HELM display names
    contain underscores inside legitimate kwarg names like
    ``use_chain_of_thought``). Leading separator characters on each token
    are stripped so the ``dyck_language_np=3`` shape ("kwarg glued to the
    scenario name with ``_``") still parses cleanly.
    """
    rest = display_name[len(registry_name):]
    kv: dict[str, str] = {}
    for tok in re.split(r'[,:]', rest):
        tok = re.sub(r'^[_,:]+', '', tok).strip()
        if '=' in tok:
            k, _, v = tok.partition('=')
            kv[k.strip()] = v.strip()
    return kv


def reconstruct_run_entry_from_run_spec(run_spec: dict[str, Any]) -> tuple[str, list[str]]:
    """Build a valid ``helm-run --run-entries`` argument from a ``run_spec.json`` dict.

    HELM's ``run_spec['name']`` is a *display* string used as a directory
    and log identifier. It does not round-trip through ``helm-run``'s
    parser in several known cases:

    - mixed separators (``dyck_language_np=3``, ``legal_support,method=...``)
    - display-vs-kwarg renames (``mmlu_pro: subset`` vs kwarg ``subject``)
    - non-constructor metadata leaked into the name
      (``...,eval_split=test,groups=mmlu_<subject>`` for ``mmlu``)

    Reconstruct from the structural fields HELM stores explicitly:

    1. registry name — longest run-spec-function name that prefixes the
       display name with one of ``{':', ',', '_'}`` after it.
    2. scenario kwargs — :attr:`scenario_spec.args` is authoritative and
       wins over anything parsed from the display name.
    3. ``method`` — pulled from :attr:`adapter_spec.method` when the
       run-spec-function accepts it.
    4. additional kwargs — display-name tokens whose names appear in the
       function signature and aren't already provided by (2) or (3).
    5. ``model`` — always taken from :attr:`adapter_spec.model` (HELM's
       run-entry convention).

    Kwargs that don't match the run-spec-function signature are dropped
    and reported in the second return value, so the caller can log a
    warning. If the registry lookup fails or the function can't be
    introspected, returns ``(display_name, ['unresolved_registry_name'])``
    so the caller can fall back.
    """
    import inspect

    display_name = (run_spec.get('name') or '').strip()
    scenario_spec = run_spec.get('scenario_spec') or {}
    scenario_args = dict(scenario_spec.get('args') or {})
    adapter_spec = run_spec.get('adapter_spec') or {}
    model = adapter_spec.get('model')
    method = adapter_spec.get('method')

    registry_name = _resolve_registry_name_from_display(display_name)
    if registry_name is None:
        return display_name, ['unresolved_registry_name']

    try:
        from helm.benchmark.run_spec import get_run_spec_function
        fn = get_run_spec_function(registry_name)
        sig_params = set(inspect.signature(fn).parameters.keys())
    except Exception:
        return display_name, [f'introspection_failed:{registry_name}']

    kwargs: dict[str, str] = {}
    dropped: list[str] = []

    for k, v in scenario_args.items():
        if k in sig_params:
            kwargs[k] = str(v)
        else:
            dropped.append(f'scenario_args.{k}')

    if 'method' in sig_params and method:
        kwargs.setdefault('method', str(method))

    expander_names = _registered_run_expander_names()
    for k, v in _extract_display_kwargs(display_name, registry_name).items():
        if k == 'model':
            continue  # canonical model comes from adapter_spec.model
        if k in _DISPLAY_ONLY_DECORATIONS:
            continue  # never valid as a run-entry kwarg
        if k in sig_params or k in expander_names:
            kwargs.setdefault(k, str(v))
        else:
            dropped.append(f'display.{k}')

    if model:
        kwargs['model'] = str(model)

    parts = [f'{k}={v}' for k, v in kwargs.items()]
    run_entry = f'{registry_name}:{",".join(parts)}' if parts else registry_name
    return run_entry, dropped


def discover_benchmark_output_dirs(
    roots: Iterable[os.PathLike[str] | str],
) -> Iterator[Path]:
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        if root.name == "benchmark_output" and root.is_dir():
            yield root
            continue

        for dirpath, dirnames, _filenames in os.walk(
            root, topdown=True, followlinks=False
        ):
            prunable = {".git", "__pycache__", ".venv", "venv", "node_modules"}
            dirnames[:] = [d for d in dirnames if d not in prunable]
            if "benchmark_output" in dirnames:
                bo = Path(dirpath) / "benchmark_output"
                if bo.is_dir():
                    yield bo
                dirnames[:] = [d for d in dirnames if d != "benchmark_output"]
