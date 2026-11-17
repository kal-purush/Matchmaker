import pytest

pytest.importorskip("numpy", reason="ablation studies need the [benchmarks] extra")

import json

from ablation.factorial import (
    FACTORIAL_VARIANTS,
    attributable_coverage,
    factorial_effects,
    shapley_contributions,
)
from ablation.runner import AblationRow


def _coverage_summary():
    per_target = {"target": {}}
    for variant in FACTORIAL_VARIANTS:
        p, local, mutation = (
            int(variant[1]),
            int(variant[3]),
            int(variant[5]),
        )
        value = (
            40 + p * 5 + local * 3 + mutation * 2
            + p * local * 4 + p * mutation * 6 + local * mutation * 8
        )
        per_target["target"][variant] = {
            "code_coverage": float(value),
            "branch_coverage": float(value * 2),
        }
    return {"per_target": per_target}


def test_factorial_effects_calculate_main_and_pairwise_contrasts():
    effects = factorial_effects(_coverage_summary(), ["target"])
    code = effects["per_target"]["target"]["code_coverage"]
    assert code["main_effects_points"] == {"P": 10.0, "L": 9.0, "M": 9.0}
    assert code["two_way_interactions_points"] == {
        "PxL": 2.0,
        "PxM": 3.0,
        "LxM": 4.0,
    }


def test_shapley_contributions_sum_to_full_minus_empty():
    contributions = shapley_contributions(_coverage_summary(), ["target"])
    code = contributions["per_target"]["target"]["code_coverage"]
    assert abs(sum(code["contribution_points"].values()) - 28.0) < 1e-9
    assert abs(sum(code["share_of_total_gain"].values()) - 1.0) < 1e-12


def test_attributable_coverage_unions_runs_and_compares_component_states(tmp_path):
    p1_variant = "p1l0m0"
    p0_variant = "p0l0m0"
    for variant, arcs in ((p1_variant, [[10, 11], [10, 12]]), (p0_variant, [[10, 12]])):
        report_dir = (
            tmp_path / "coverage_reports" / "natural" / "run_000"
            / "target" / variant
        )
        report_dir.mkdir(parents=True)
        (report_dir / "coverage.json").write_text(json.dumps({
            "files": {
                "validator.py": {
                    "executed_lines": [10, 11],
                    "executed_branches": arcs,
                }
            }
        }))

    rows = [
        AblationRow(target="target", variant=p1_variant, run_index=0, seed=1),
        AblationRow(target="target", variant=p0_variant, run_index=0, seed=1),
    ]
    result = attributable_coverage(rows, tmp_path)["natural"]["components"]["P"]
    assert result["attributable_branch_count"] == 1
    assert result["attributable_branches"] == [["validator.py", 10, 11]]
