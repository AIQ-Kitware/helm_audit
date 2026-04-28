from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import kwutil

from eval_audit.infra.fs_publish import history_publish_root, write_latest_alias
from eval_audit.infra.plotly_env import configure_plotly_chrome
from eval_audit.infra.logging import rich_link
from eval_audit.utils import sankey_builder
from loguru import logger


def emit_sankey_artifacts(
    *,
    rows: list[dict[str, Any]],
    report_dpath: Path,
    stamp: str,
    kind: str,
    title: str,
    stage_defs: dict[str, list[str]],
    stage_order: list[tuple[str, str]],
    root: sankey_builder.Root | None = None,
    explicit_stage_names: list[str] | None = None,
    machine_dpath: Path | None = None,
    interactive_dpath: Path | None = None,
    static_dpath: Path | None = None,
) -> dict[str, Any]:
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

    machine_history = history_publish_root(report_dpath, _machine, stamp)
    static_history = history_publish_root(report_dpath, _static, stamp)
    interactive_history = history_publish_root(report_dpath, _interactive, stamp)
    base_name = f"sankey_{stamp}_{kind}"
    json_fpath = (machine_history / base_name).with_suffix(".json")
    txt_fpath = (static_history / base_name).with_suffix(".txt")
    key_fpath = static_history / f"{base_name}_key.txt"
    html_fpath = (interactive_history / base_name).with_suffix(".html")
    jpg_fpath = (static_history / base_name).with_suffix(".jpg")
    png_fpath = (static_history / base_name).with_suffix(".png")

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
    json_fpath.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    txt_fpath.write_text(plan_text + "\n\n" + graph_summary + "\n")
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
    key_fpath.write_text("\n".join(key_lines).rstrip() + "\n")
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
            fig.write_html(str(html_fpath), include_plotlyjs="cdn")
            logger.debug(f'Write 📊: {rich_link(html_fpath)}')
            html_out = str(html_fpath)
            if os.environ.get("HELM_AUDIT_SKIP_STATIC_IMAGES", "") not in {"1", "true", "yes"}:
                try:
                    fig.write_image(str(jpg_fpath), scale=3.0)
                    jpg_out = str(jpg_fpath)
                    logger.debug(f'Write 🖼: {rich_link(jpg_out)}')
                    fig.write_image(str(png_fpath), scale=3.0)
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

    # write_latest_alias renames the stamped intermediates onto the visible
    # *.latest.* paths (history layer retired 2026-04-28). Capture the
    # post-rename paths so the returned dict points at the actual files.
    json_latest = write_latest_alias(json_fpath, _machine, f"sankey_{kind}.latest.json")
    txt_latest = write_latest_alias(txt_fpath, _static, f"sankey_{kind}.latest.txt")
    key_latest = write_latest_alias(key_fpath, _static, f"sankey_{kind}.latest.key.txt")
    html_latest = None
    jpg_latest = None
    png_latest = None
    if html_out is not None:
        html_latest = write_latest_alias(html_fpath, _interactive, f"sankey_{kind}.latest.html")
    if jpg_out is not None:
        jpg_latest = write_latest_alias(jpg_fpath, _static, f"sankey_{kind}.latest.jpg")
    if png_out is not None:
        png_latest = write_latest_alias(png_fpath, _static, f"sankey_{kind}.latest.png")

    return {
        "json": str(json_latest),
        "txt": str(txt_latest),
        "key_txt": str(key_latest),
        "html": str(html_latest) if html_latest is not None else None,
        "jpg": str(jpg_latest) if jpg_latest is not None else None,
        "png": str(png_latest) if png_latest is not None else None,
        "plotly_error": plotly_error,
    }
