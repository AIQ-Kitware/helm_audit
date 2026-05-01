from __future__ import annotations

import argparse

from loguru import logger

from eval_audit.infra.logging import rich_link, setup_cli_logging
from pathlib import Path

from eval_audit.helm.diff import HelmRunDiff
from eval_audit.infra.fs_publish import write_text_atomic
from eval_audit.normalized import SourceKind
from eval_audit.normalized.helm_compat import helm_view_from_path
from eval_audit.reports.core_packet import comparison_sample_latest_name

# Zero-overhead in normal runs; line_profiler swaps in a real profiler when
# the LINE_PROFILE env var is set.
try:
    from line_profiler import profile  # type: ignore[import-not-found]
except ImportError:
    def profile(func):  # type: ignore[no-redef]
        return func


def _infer_run_spec_name(*run_paths: str) -> str:
    names = [Path(p).name for p in run_paths if p]
    names = [n for n in names if n]
    if not names:
        return 'unknown_run_spec'
    unique = sorted(set(names))
    if len(unique) == 1:
        return unique[0]
    return unique[0]


@profile
def write_pair_samples(
    *,
    run_a: str,
    run_b: str,
    label: str,
    report_dpath: str | Path,
    top_n: int = 8,
    show_details: int = 6,
    level: int = 30,
) -> Path:
    report_dpath = Path(report_dpath).expanduser().resolve()
    report_dpath.mkdir(parents=True, exist_ok=True)

    diff = HelmRunDiff(
        helm_view_from_path(run_a, source_kind=SourceKind.OFFICIAL),
        helm_view_from_path(run_b, source_kind=SourceKind.LOCAL),
        a_name=f'{label}:A',
        b_name=f'{label}:B',
    )
    run_spec_name = _infer_run_spec_name(run_a, run_b)
    lines: list[str] = []
    lines.append('Instance Sample Inspection')
    lines.append(f'label: {label}')
    lines.append(f'run_spec_name: {run_spec_name}')
    lines.append(f'run_a: {Path(run_a).expanduser().resolve()}')
    lines.append(f'run_b: {Path(run_b).expanduser().resolve()}')
    lines.append('')
    diff.summarize_instances(
        level=level,
        top_n=top_n,
        show_details=show_details,
        writer=lines.append,
    )
    out_fpath = report_dpath / comparison_sample_latest_name(label)
    write_text_atomic(out_fpath, '\n'.join(lines) + '\n')
    return out_fpath


@profile
def main(argv: list[str] | None = None) -> None:
    setup_cli_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-a', required=True)
    parser.add_argument('--run-b', required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--report-dpath', required=True)
    parser.add_argument('--top-n', type=int, default=8)
    parser.add_argument('--show-details', type=int, default=6)
    parser.add_argument('--level', type=int, default=30)
    args = parser.parse_args(argv)

    out_fpath = write_pair_samples(
        run_a=args.run_a,
        run_b=args.run_b,
        label=args.label,
        report_dpath=args.report_dpath,
        top_n=args.top_n,
        show_details=args.show_details,
        level=args.level,
    )
    latest_fpath = Path(args.report_dpath).expanduser().resolve() / comparison_sample_latest_name(args.label)
    logger.info(f'Wrote instance sample report: {rich_link(out_fpath)}')
    logger.info(f'Updated latest link: {rich_link(latest_fpath)}')


if __name__ == '__main__':
    setup_cli_logging()
    main()
