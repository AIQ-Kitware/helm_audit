from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import kwutil

from helm_audit.infra.fs_publish import write_latest_alias
from helm_audit.utils import sankey_builder


def emit_sankey_artifacts(
    *,
    rows: list[dict[str, Any]],
    report_dpath: Path,
    stamp: str,
    kind: str,
    title: str,
    stage_defs: dict[str, list[str]],
    stage_order: list[tuple[str, str]],
) -> dict[str, Any]:
    report_dpath.mkdir(parents=True, exist_ok=True)
    root = sankey_builder.Root(label=f"{title} n={len(rows)}")
    node = root
    stage_names: list[str] = []
    for key, name in stage_order:
        values = {row.get(key, None) for row in rows}
        if len(values) <= 1:
            continue
        node = node.group(by=key, name=name)
        stage_names.append(name)

    graph = root.build_sankey(rows, label_fmt="{name}: {value}")
    graph_summary = graph.summarize(max_edges=300)
    plan_text = root.to_text()

    stem = report_dpath / f"sankey_{stamp}_{kind}"
    json_fpath = stem.with_suffix(".json")
    txt_fpath = stem.with_suffix(".txt")
    key_fpath = stem.with_name(f"{stem.name}_key.txt")
    html_fpath = stem.with_suffix(".html")
    jpg_fpath = stem.with_suffix(".jpg")

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

    html_out = None
    jpg_out = None
    plotly_error = None
    try:
        fig = graph.to_plotly(title=title)
        fig.write_html(str(html_fpath), include_plotlyjs="cdn")
        html_out = str(html_fpath)
        try:
            fig.write_image(str(jpg_fpath), scale=2.0)
            jpg_out = str(jpg_fpath)
        except Exception as ex:
            plotly_error = f"unable to write sankey JPG: {ex!r}"
    except Exception as ex:
        plotly_error = f"unable to write sankey HTML/images: {ex!r}"

    write_latest_alias(json_fpath, report_dpath, f"sankey_{kind}.latest.json")
    write_latest_alias(txt_fpath, report_dpath, f"sankey_{kind}.latest.txt")
    write_latest_alias(key_fpath, report_dpath, f"sankey_{kind}.latest.key.txt")
    if html_out is not None:
        write_latest_alias(html_fpath, report_dpath, f"sankey_{kind}.latest.html")
    if jpg_out is not None:
        write_latest_alias(jpg_fpath, report_dpath, f"sankey_{kind}.latest.jpg")

    return {
        "json": str(json_fpath),
        "txt": str(txt_fpath),
        "key_txt": str(key_fpath),
        "html": html_out,
        "jpg": jpg_out,
        "plotly_error": plotly_error,
    }
