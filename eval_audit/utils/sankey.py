from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import kwutil
import safer

from eval_audit.infra.fs_publish import write_text_atomic
from eval_audit.infra.plotly_env import configure_plotly_chrome
from eval_audit.infra.logging import rich_link
from eval_audit.utils import sankey_builder
from loguru import logger

# Zero-overhead in normal runs; line_profiler swaps in a real profiler when
# the LINE_PROFILE env var is set. See the same shim in build_reports_summary.
try:
    from line_profiler import profile  # type: ignore[import-not-found]
except ImportError:
    def profile(func):  # type: ignore[no-redef]
        return func


@profile
def emit_sankey_artifacts(
    *,
    rows: list[dict[str, Any]],
    report_dpath: Path,
    kind: str,
    title: str,
    stage_defs: dict[str, list[str]],
    stage_order: list[tuple[str, str]],
    root: sankey_builder.Root | None = None,
    explicit_stage_names: list[str] | None = None,
    machine_dpath: Path | None = None,
    interactive_dpath: Path | None = None,
    static_dpath: Path | None = None,
    stamp: str | None = None,  # accepted for backwards-compat; unused
) -> dict[str, Any]:
    del stamp  # vestigial: filenames no longer carry a stamp infix
    report_dpath.mkdir(parents=True, exist_ok=True)
    _machine = machine_dpath if machine_dpath is not None else report_dpath
    _interactive = interactive_dpath if interactive_dpath is not None else report_dpath
    _static = static_dpath if static_dpath is not None else report_dpath
    for d in {_machine, _interactive, _static}:
        d.mkdir(parents=True, exist_ok=True)

    if root is None:
        root = sankey_builder.Root(label=f"{title} n={len(rows)}")
        node = root
        stage_names: list[str] = []
        for key, name in stage_order:
            values = {row.get(key, None) for row in rows}
            if len(values) <= 1:
                continue
            node = node.group(by=key, name=name)
            stage_names.append(name)
    else:
        stage_names = list(explicit_stage_names or [name for _, name in stage_order])

    graph = root.build_sankey(rows, label_fmt="{name}: {value}")
    graph_summary = graph.summarize(max_edges=300)
    plan_text = root.to_text()

    json_fpath = _machine / f"sankey_{kind}.json"
    txt_fpath = _static / f"sankey_{kind}.txt"
    key_fpath = _static / f"sankey_{kind}.key.txt"
    html_fpath = _interactive / f"sankey_{kind}.html"
    jpg_fpath = _static / f"sankey_{kind}.jpg"
    png_fpath = _static / f"sankey_{kind}.png"

    node_labels, source, target, value = graph._to_sankey_data()
    payload = kwutil.Json.ensure_serializable(
        {
            "kind": kind,
            "title": title,
            "n_rows": len(rows),
            "rows": rows,
            "stage_order": stage_names,
            "node_labels": node_labels,
            "source": source,
            "target": target,
            "value": value,
        }
    )
    write_text_atomic(json_fpath, json.dumps(payload, indent=2, ensure_ascii=False))
    write_text_atomic(txt_fpath, plan_text + "\n\n" + graph_summary + "\n")
    key_lines = [
        "Sankey Key",
        "----------",
        f"Graph: {title}",
        "Stage order: " + " -> ".join(stage_names),
        "",
    ]
    for stage in stage_names:
        key_lines.append(f"{stage}:")
        for item in stage_defs.get(stage, ["(no definition available)"]):
            key_lines.append(f"  {item}")
        key_lines.append("")
    write_text_atomic(key_fpath, "\n".join(key_lines).rstrip() + "\n")
    logger.debug(f'Write: {rich_link(key_fpath)}')

    html_out = None
    jpg_out = None
    png_out = None
    plotly_error = None
    if os.environ.get("HELM_AUDIT_SKIP_PLOTLY", "") in {"1", "true", "yes"}:
        plotly_error = "skipped plotly sankey rendering by configuration"
        logger.debug(plotly_error)
    else:
        try:
            configure_plotly_chrome()
            fig = graph.to_plotly(title=title)
            with safer.open(html_fpath, "w", make_parents=True, temp_file=True) as fp:
                fig.write_html(fp, include_plotlyjs="cdn")
            logger.debug(f'Write 📊: {rich_link(html_fpath)}')
            html_out = str(html_fpath)
            if os.environ.get("HELM_AUDIT_SKIP_STATIC_IMAGES", "") not in {"1", "true", "yes"}:
                try:
                    with safer.open(jpg_fpath, "wb", make_parents=True, temp_file=True) as fp:
                        fig.write_image(fp, format="jpg", scale=3.0)
                    jpg_out = str(jpg_fpath)
                    logger.debug(f'Write 🖼: {rich_link(jpg_out)}')
                    with safer.open(png_fpath, "wb", make_parents=True, temp_file=True) as fp:
                        fig.write_image(fp, format="png", scale=3.0)
                    png_out = str(png_fpath)
                    logger.debug(f'Write 🖼: {rich_link(png_out)}')
                except Exception as ex:
                    plotly_error = f"unable to write sankey JPG/PNG: {ex!r}"
                    logger.warning(plotly_error)
            else:
                logger.debug('Skip sankey static image by config')
        except Exception as ex:
            plotly_error = f"unable to write sankey HTML/images: {ex!r}"
            logger.warning(plotly_error)

    return {
        "json": str(json_fpath),
        "txt": str(txt_fpath),
        "key_txt": str(key_fpath),
        "html": html_out,
        "jpg": jpg_out,
        "png": png_out,
        "plotly_error": plotly_error,
    }
