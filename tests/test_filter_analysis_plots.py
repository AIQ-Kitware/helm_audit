from __future__ import annotations

from pathlib import Path

from helm_audit.reports.filter_analysis import (
    _make_selected_excluded_rows,
    _title_with_n,
    make_open_access_exclusion_reason_table,
    make_open_access_exclusion_reason_by_model_table,
    make_candidate_pool_table,
    make_reason_breakout_table,
    make_reason_combo_table,
)
from helm_audit.infra.fs_publish import history_publish_root
from helm_audit.utils import sankey_builder
from helm_audit.utils.sankey import emit_sankey_artifacts


def test_make_selected_excluded_rows_groups_by_facet():
    rows = _make_selected_excluded_rows(
        [
            {"model": "m1", "selection_status": "selected"},
            {"model": "m1", "selection_status": "excluded"},
            {"model": "m1", "selection_status": "excluded"},
            {"model": "m2", "selection_status": "selected"},
            {"model": "m3", "selection_status": "excluded"},
        ],
        "model",
    )

    assert rows == [
        {"model": "m1", "selection_status": "selected", "count": 1},
        {"model": "m1", "selection_status": "excluded", "count": 2},
        {"model": "m2", "selection_status": "selected", "count": 1},
        {"model": "m3", "selection_status": "excluded", "count": 1},
    ]


def test_title_helper_appends_n_suffix():
    assert _title_with_n("Example Plot", 7) == "Example Plot n=7"


def test_reason_combo_and_candidate_pool_tables_preserve_top_level_breakdowns():
    inventory_rows = [
        {
            "candidate_pool": "eligible-model",
            "selection_status": "selected",
            "failure_reasons": [],
            "run_spec_name": "a",
            "model": "m1",
            "benchmark": "b1",
        },
        {
            "candidate_pool": "eligible-model",
            "selection_status": "excluded",
            "failure_reasons": ["no-local-helm-deployment"],
            "run_spec_name": "b",
            "model": "m2",
            "benchmark": "b2",
        },
        {
            "candidate_pool": "complete-run",
            "selection_status": "excluded",
            "failure_reasons": [],
            "run_spec_name": "c",
            "model": "m3",
            "benchmark": "b3",
        },
    ]

    candidate_pool_rows = make_candidate_pool_table(inventory_rows)
    assert candidate_pool_rows == [
        {
            "candidate_pool": "eligible-model",
            "run_count": 2,
            "selected_runs": 1,
            "excluded_runs": 1,
            "fraction_of_all_runs": 2 / 3,
        },
        {
            "candidate_pool": "complete-run",
            "run_count": 1,
            "selected_runs": 0,
            "excluded_runs": 1,
            "fraction_of_all_runs": 1 / 3,
        },
    ]

    reason_combo_rows = make_reason_combo_table(inventory_rows)
    assert reason_combo_rows[0]["reason_combo"] == "no-local-helm-deployment"
    assert reason_combo_rows[0]["run_count"] == 1

    reason_by_model = make_reason_breakout_table(inventory_rows, "model")
    assert reason_by_model[0]["failure_reason"] in {"no-local-helm-deployment", "unclassified-exclusion"}


def test_open_access_exclusion_reason_table_ignores_non_open_models():
    rows = make_open_access_exclusion_reason_table(
        [
            {
                "model_access": "open",
                "selection_status": "excluded",
                "failure_reasons": ["no-local-helm-deployment", "too-large"],
            },
            {
                "model_access": "open",
                "selection_status": "excluded",
                "failure_reasons": ["requires-closed-judge"],
            },
            {
                "model_access": "open",
                "selection_status": "excluded",
                "failure_reasons": [],
            },
            {
                "model_access": "limited",
                "selection_status": "excluded",
                "failure_reasons": ["not-open-access", "no-local-helm-deployment"],
            },
        ]
    )

    assert rows == [
        {"failure_reason": "no-local-helm-deployment", "run_count": 1},
        {"failure_reason": "requires-closed-judge", "run_count": 1},
        {"failure_reason": "too-large", "run_count": 1},
        {"failure_reason": "unclassified-exclusion", "run_count": 1},
    ]


def test_open_access_exclusion_reason_by_model_table_ignores_non_open_models():
    rows = make_open_access_exclusion_reason_by_model_table(
        [
            {
                "model": "open-a",
                "model_access": "open",
                "selection_status": "excluded",
                "failure_reasons": ["no-local-helm-deployment", "too-large"],
            },
            {
                "model": "open-b",
                "model_access": "open",
                "selection_status": "excluded",
                "failure_reasons": ["too-large"],
            },
            {
                "model": "limited-c",
                "model_access": "limited",
                "selection_status": "excluded",
                "failure_reasons": ["not-open-access", "no-local-helm-deployment"],
            },
        ]
    )

    assert rows == [
        {"model": "open-a", "reason_combo": "no-local-helm-deployment|too-large", "run_count": 1},
        {"model": "open-b", "reason_combo": "too-large", "run_count": 1},
    ]


def test_open_access_exclusion_reason_by_model_table_can_filter_text_and_size_gates():
    rows = make_open_access_exclusion_reason_by_model_table(
        [
            {
                "model": "open-a",
                "model_access": "open",
                "selection_status": "excluded",
                "failure_reasons": ["not-text-like", "too-large"],
            },
            {
                "model": "open-b",
                "model_access": "open",
                "selection_status": "excluded",
                "failure_reasons": ["too-large", "no-local-helm-deployment"],
            },
            {
                "model": "open-c",
                "model_access": "open",
                "selection_status": "excluded",
                "failure_reasons": ["no-local-helm-deployment"],
            },
        ],
        excluded_reasons={"not-text-like", "excluded-tags", "too-large"},
    )

    assert rows == [
        {"model": "open-c", "reason_combo": "no-local-helm-deployment", "run_count": 1},
    ]


def test_emit_sankey_artifacts_writes_png_and_latest_alias(tmp_path: Path, monkeypatch):
    class FakeFigure:
        def write_html(self, fpath, include_plotlyjs="cdn"):
            Path(fpath).write_text("<html></html>")

        def write_image(self, fpath, scale=1.0):
            Path(fpath).write_bytes(b"PNG")

    monkeypatch.setattr(sankey_builder.SankeyDiGraph, "to_plotly", lambda self, title="Sankey": FakeFigure())
    monkeypatch.setattr("helm_audit.utils.sankey.configure_plotly_chrome", lambda: None)

    root = sankey_builder.Root(label="demo")
    root.group(by="kind", name="Kind")
    out = emit_sankey_artifacts(
        rows=[{"kind": "a"}],
        report_dpath=tmp_path / "reports",
        stamp="20260410T000000Z",
        kind="demo",
        title="Demo Sankey",
        stage_defs={"Kind": ["a"]},
        stage_order=[("kind", "Kind")],
        root=root,
        explicit_stage_names=["Kind"],
        interactive_dpath=tmp_path / "reports" / "interactive",
        static_dpath=tmp_path / "reports" / "static",
    )

    assert out["html"] is not None
    assert out["jpg"] is not None
    png_root = history_publish_root(tmp_path / "reports", tmp_path / "reports" / "static", "20260410T000000Z")
    png_fpath = png_root / "sankey_20260410T000000Z_demo.png"
    assert png_fpath.exists()
