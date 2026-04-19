from __future__ import annotations

from pathlib import Path

from helm_audit.workflows import compare_batch


def test_collect_historic_candidates_ignores_local_model_deployment(
    monkeypatch,
) -> None:
    precomputed_root = "/tmp/public-historic-root"
    run_entry = (
        "boolq:model=lmsys/vicuna-7b-v1.3,data_augmentation=canonical,"
        "model_deployment=kubeai/vicuna-7b-v1-3-no-chat-template-local"
    )
    public_candidate = {
        "run_dir": Path("/tmp/public-historic-root/suite/benchmark_output/runs/v1/boolq:model=lmsys_vicuna-7b-v1.3,data_augmentation=canonical"),
        "run_name": "boolq:model=lmsys_vicuna-7b-v1.3,data_augmentation=canonical",
        "run_name_benchmark": "boolq",
        "run_name_kv": {
            "model": "lmsys_vicuna-7b-v1.3",
            "data_augmentation": "canonical",
        },
        "source_root": Path(precomputed_root),
        "helm_version": "v1",
        "requested_max_eval_instances": 1000,
        "model_deployment": None,
        "metric_class_names": [],
    }
    wrong_benchmark = {
        **public_candidate,
        "run_name": "narrative_qa:model=lmsys_vicuna-7b-v1.3,data_augmentation=canonical",
        "run_name_benchmark": "narrative_qa",
    }

    def fake_index(_precomputed_root: str):
        return {
            "boolq": (public_candidate,),
            "narrative_qa": (wrong_benchmark,),
        }

    compare_batch._historic_candidate_benchmark_index.cache_clear()
    monkeypatch.setattr(
        compare_batch,
        "_historic_candidate_benchmark_index",
        fake_index,
    )
    candidates = compare_batch.collect_historic_candidates(precomputed_root, run_entry)
    assert [candidate["run_name"] for candidate in candidates] == [
        public_candidate["run_name"]
    ]

