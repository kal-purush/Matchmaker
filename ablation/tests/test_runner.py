import pytest

pytest.importorskip("numpy", reason="ablation studies need the [benchmarks] extra")

import sys
import types
from concurrent.futures import Future
from types import SimpleNamespace

import numpy as np

from ablation import runner
from ablation.runner import AblationRow


MODULE_NAME = (
    "benchmarks."
    "python_regex_function_coverage_benchmark"
)


def _fake_benchmark():
    module = types.ModuleType(MODULE_NAME)
    module.build_target_specs = lambda: [
        SimpleNamespace(
            name="target",
            regex="x",
            validator_fn=lambda sample: sample == "yes",
            target_module=object(),
        )
    ]
    module.compute_branch_coverage = lambda totals: (
        totals["covered_branches"] / totals["num_branches"]
    )
    module.run_validator_with_coverage = (
        lambda samples, validator, target, html, json_path: (
            len(samples),
            0,
            {
                "num_statements": 10,
                "covered_lines": 8,
                "num_branches": 5,
                "covered_branches": 3,
            },
        )
    )
    module.bootstrap_mean_ci = lambda deltas, rng, iterations: (
        float(np.mean(deltas)),
        float(np.mean(deltas)),
    )
    module.sign_flip_permutation_pvalue = lambda deltas, rng, iterations: 0.5
    module.holm_bonferroni_adjust = lambda results: [
        result.update(p_value_holm=result["p_value"]) for result in results
    ]
    return module


def test_run_job_records_coverage_and_dedupes_inputs(monkeypatch, tmp_path):
    module = _fake_benchmark()
    monkeypatch.setitem(sys.modules, MODULE_NAME, module)
    observed = {}

    def variant(pattern, n_pos, n_neg, seed, validate=True, **kwargs):
        observed["validate"] = validate
        return ["a", "same"], ["same", "b"]

    monkeypatch.setitem(runner.VARIANTS, "test", ("Test", variant))
    row = runner.run_job({
        "target": "target",
        "variant": "test",
        "run_index": 0,
        "seed": 3,
        "n_positive": 2,
        "n_negative": 2,
        "out_dir": str(tmp_path),
    })

    assert row.status == "ok"
    assert observed["validate"] is True
    assert (row.positives, row.negatives) == (2, 2)
    assert (row.accepted, row.rejected) == (3, 0)
    assert row.code_coverage == 80.0
    assert row.branch_coverage == 60.0


def test_run_job_generates_from_the_requested_regex_set(monkeypatch):
    monkeypatch.setitem(sys.modules, MODULE_NAME, _fake_benchmark())
    seen = []

    def variant(pattern, n_pos, n_neg, seed, **kwargs):
        seen.append(pattern)
        return ["yes"], ["no"]

    monkeypatch.setitem(runner.VARIANTS, "record", ("record", variant))
    monkeypatch.setattr(runner, "regex_for", lambda spec, regex_set: f"{spec.regex}:{regex_set}")
    job = {
        "target": "target",
        "variant": "record",
        "run_index": 0,
        "seed": 1,
        "n_positive": 1,
        "n_negative": 1,
        "out_dir": ".",
    }
    default_row = runner.run_job(job)
    validator_row = runner.run_job({**job, "regex_set": "validator"})
    assert seen == ["x:benchmark", "x:validator"]
    assert default_row.regex_set == "benchmark"
    assert validator_row.regex_set == "validator"


def test_run_job_converts_variant_exceptions_to_error_rows(monkeypatch):
    monkeypatch.setitem(sys.modules, MODULE_NAME, _fake_benchmark())

    def broken(*args, **kwargs):
        raise RuntimeError("generator failed")

    monkeypatch.setitem(runner.VARIANTS, "broken", ("Broken", broken))
    row = runner.run_job({
        "target": "target",
        "variant": "broken",
        "run_index": 2,
        "seed": 5,
        "n_positive": 1,
        "n_negative": 1,
        "out_dir": ".",
    })
    assert row.status == "error"
    assert "generator failed" in row.error


def test_summarize_computes_paired_deltas_and_superset_check(monkeypatch):
    monkeypatch.setitem(sys.modules, MODULE_NAME, _fake_benchmark())
    rows = [
        AblationRow(
            target="target",
            variant=variant,
            run_index=0,
            seed=1,
            code_coverage=code,
            branch_coverage=branch,
        )
        for variant, code, branch in (
            ("full", 80.0, 60.0),
            ("no_pos", 70.0, 50.0),
        )
    ]
    summary = runner.summarize(
        rows,
        ["target"],
        ["full", "no_pos"],
        stats_iterations=10,
    )
    assert summary["paired_tests"][0]["mean_delta_points"] == 10.0
    assert summary["superset_invariant_violations"] == []


def test_run_all_uses_a_fresh_worker_for_each_job(monkeypatch):
    class ImmediateExecutor:
        def __init__(self, *, max_workers, max_tasks_per_child):
            assert max_workers == 1
            assert max_tasks_per_child == 1

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def submit(self, function, job):
            future = Future()
            future.set_result(AblationRow(
                target=job["target"],
                variant=job["variant"],
                run_index=job["run_index"],
                seed=job["seed"],
            ))
            return future

    monkeypatch.setattr(runner, "ProcessPoolExecutor", ImmediateExecutor)
    rows = runner.run_all(
        [
            {"target": "one", "variant": "full", "run_index": 0, "seed": 1},
            {"target": "two", "variant": "full", "run_index": 0, "seed": 1},
        ],
        workers=1,
    )
    assert [row.target for row in rows] == ["one", "two"]


def test_write_outputs_writes_json_and_escaped_latex(tmp_path):
    rows = [AblationRow(
        target="target_name",
        variant="full",
        run_index=0,
        seed=1,
        code_coverage=80.0,
        branch_coverage=60.0,
    )]
    summary = {
        "per_target": {
            "target_name": {
                "full": {"code_coverage": 80.0, "branch_coverage": 60.0},
            },
        },
    }
    config = {"variants": ["full"], "targets": ["target_name"]}
    runner.write_outputs(rows, summary, config, tmp_path)
    assert (tmp_path / "results.jsonl").is_file()
    assert (tmp_path / "summary.json").is_file()
    table = (tmp_path / "table.tex").read_text()
    assert "target\\_name" in table
    assert not (tmp_path / "delta_table.tex").exists()


def test_write_outputs_writes_delta_table_and_report(tmp_path, capsys):
    rows = []
    summary = {
        "baseline": "full",
        "per_target": {
            "target_name": {
                "full": {"code_coverage": 80.0, "branch_coverage": 60.0},
                "no_pos": {"code_coverage": 70.0, "branch_coverage": 50.0},
                "random_pos": {"code_coverage": 82.0, "branch_coverage": None},
            },
        },
        "paired_tests": [
            {
                "variant": "no_pos",
                "metric": "code_coverage",
                "mean_delta_points": 10.0,
                "bootstrap_95_ci": [9.0, 11.0],
                "p_value": 0.01,
                "p_value_holm": 0.02,
                "target_counts": {"better": 1, "tied": 0, "worse": 0},
            },
        ],
    }
    config = {
        "variants": ["full", "no_pos", "random_pos"],
        "targets": ["target_name"],
    }
    runner.write_outputs(rows, summary, config, tmp_path)
    table = (tmp_path / "delta_table.tex").read_text()
    assert "full $-$ no\\_pos" in table
    assert "full $-$ random\\_pos" in table
    assert "target\\_name & +10.00 & +10.00 & -2.00 & -- \\\\" in table
    assert "Mean & +10.00 & -- & -- & --" in table
    assert "0.0200" in table

    runner.print_delta_report(summary)
    output = capsys.readouterr().out
    assert "no_pos" in output and "+10.00" in output and "1/0/0" in output
