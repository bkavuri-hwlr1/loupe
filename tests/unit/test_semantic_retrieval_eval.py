"""Offline retrieval scores and corpus-specific decisions use fixed inputs only."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_script = Path(__file__).resolve().parents[2] / "scripts/semantic_retrieval_eval.py"
_spec = importlib.util.spec_from_file_location("semantic_retrieval_eval", _script)
assert _spec and _spec.loader
experiment = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(experiment)


def test_recall_counts_unique_expected_files_within_unique_rank_cutoff() -> None:
    assert experiment.recall_at_k(["a", "b"], ["a", "a", "c", "b"], 2) == 0.5
    assert experiment.recall_at_k(["a", "b"], ["a", "a", "c", "b"], 3) == 1.0
    assert experiment.recall_at_k(["a", "a"], ["b", "a"], 1) == 0.0
    assert experiment.recall_at_k(["a"], [], 10) == 0.0


@pytest.mark.parametrize("expected,k", [([], 1), (["a"], 0), (["a"], True)])
def test_recall_refuses_undefined_denominator_or_cutoff(expected, k) -> None:
    with pytest.raises(ValueError):
        experiment.recall_at_k(expected, ["a"], k)


def test_fusion_rewards_cross_arm_agreement_without_duplicate_chunk_votes() -> None:
    rankings = [["a", "a", "b"], ["b", "c"]]
    assert experiment.reciprocal_rank_fusion(rankings) == ["b", "a", "c"]
    assert experiment.reciprocal_rank_fusion(rankings) == (
        experiment.reciprocal_rank_fusion([["a", "b"], ["b", "c"]])
    )


def test_fusion_ties_are_stable_and_missing_arms_remain_usable() -> None:
    assert experiment.reciprocal_rank_fusion([["b"], ["a"]]) == ["a", "b"]
    assert experiment.reciprocal_rank_fusion([[], ["b", "a"]]) == ["b", "a"]
    assert experiment.reciprocal_rank_fusion([]) == []
    with pytest.raises(ValueError):
        experiment.reciprocal_rank_fusion([["a"]], constant=0)


def _gate():
    return {
        "minimum_queries_per_repository": 30,
        "minimum_macro_delta_at_10": 0.10,
        "minimum_repository_delta_at_10": 0.05,
        "bootstrap_seed": 20261030,
        "bootstrap_samples": 10000,
    }


def _rows(delta=0.25):
    return [
        {
            "id": f"{repository}-{number}",
            "repository": repository,
            "recall": {
                "lexical": {"5": 0.0, "10": 0.0},
                "hybrid": {"5": 0.0, "10": delta},
            },
        }
        for repository in ("first", "second")
        for number in range(30)
    ]


def test_gate_opens_only_for_clear_improvement_on_both_fixed_repositories() -> None:
    result = experiment.gate_decision(_rows(), _gate())
    assert result["decision"] == "go"
    assert result["macro_delta_at_10"] == 0.25
    assert result["paired_stratified_bootstrap_95_percent"] == [0.25, 0.25]
    assert experiment.gate_decision(_rows(delta=0.0), _gate())["decision"] == "no-go"
    assert experiment.gate_decision(_rows()[:30], _gate())["decision"] == "no-go"
    assert experiment.gate_decision(_rows()[:-1], _gate())["decision"] == "no-go"


def test_gate_refuses_a_small_cutoff_regression_despite_large_later_gain() -> None:
    rows = _rows()
    rows[0]["recall"]["lexical"]["5"] = 1.0
    assert experiment.gate_decision(rows, _gate())["decision"] == "no-go"


def test_gate_macro_weights_repositories_equally_instead_of_query_counts() -> None:
    rows = _rows(delta=0.1)
    for row in rows[:30]:
        row["recall"]["hybrid"]["10"] = 0.5
    # Extra queries change neither repository's mean nor their equal weight.
    rows.extend({**rows[-1], "id": f"extra-{i}"} for i in range(30))
    result = experiment.gate_decision(rows, _gate())
    assert result["macro_delta_at_10"] == pytest.approx(0.3)
    assert result["decision"] == "go"


def test_report_hash_describes_consumed_manifest_even_if_disk_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "queries.toml"
    initial = b"version = 1\n"
    manifest.write_bytes(initial)
    output = tmp_path / "results.json"

    def fake_evaluate(config, roots, model):
        assert config["version"] == 1
        manifest.write_text("version = 2\n")
        return {"repositories": {}, "f4_gate": {"decision": "no-go"}}

    monkeypatch.setattr(experiment, "evaluate", fake_evaluate)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "semantic_retrieval_eval",
            "--queries",
            str(manifest),
            "--model-dir",
            str(tmp_path),
            "--out",
            str(output),
        ],
    )
    experiment.main()
    assert json.loads(output.read_text())["query_file_sha256"] == (
        hashlib.sha256(initial).hexdigest()
    )


def test_another_result_created_during_evaluation_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "queries.toml"
    manifest.write_text("version = 1\n")
    output = tmp_path / "results.json"

    def fake_evaluate(config, roots, model):
        output.write_text("previous completed measurement\n")
        return {"repositories": {}, "f4_gate": {"decision": "no-go"}}

    monkeypatch.setattr(experiment, "evaluate", fake_evaluate)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "semantic_retrieval_eval",
            "--queries",
            str(manifest),
            "--model-dir",
            str(tmp_path),
            "--out",
            str(output),
        ],
    )
    with pytest.raises(FileExistsError):
        experiment.main()
    assert output.read_text() == "previous completed measurement\n"
    assert not list(tmp_path.glob("*.pending"))
