import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .validator_regexes import regex_for
from .variants import VARIANTS


@dataclass
class AblationRow:
    target: str
    variant: str
    run_index: int
    seed: int
    positives: int = 0
    negatives: int = 0
    accepted: int = 0
    rejected: int = 0
    code_coverage: float = 0.0
    branch_coverage: float = 0.0
    covered_lines: int = 0
    covered_branches: int = 0
    status: str = "ok"
    error: str = ""
    coverage_summary: dict = field(default_factory=dict)
    negative_tags: dict = field(default_factory=dict)
    local_extended: bool = False
    regex_set: str = "benchmark"
    target_set: str = "benchmark"


def _target_specs(target_set="benchmark"):
    from .targets import target_specs

    return target_specs(target_set)


def run_job(job):
    target_name = job["target"]
    variant_name = job["variant"]
    row = AblationRow(
        target=target_name,
        variant=variant_name,
        run_index=job["run_index"],
        seed=job["seed"],
        local_extended=job.get("local_extended", False),
        regex_set=job.get("regex_set", "benchmark"),
        target_set=job.get("target_set", "benchmark"),
    )
    try:
        benchmark = __import__(
            "benchmarks.python_regex_function_coverage_benchmark",
            fromlist=["run_validator_with_coverage", "compute_branch_coverage"],
        )
        spec = _target_specs(row.target_set)[target_name]
        _label, variant = VARIANTS[variant_name]
        positives, negatives = variant(
            regex_for(spec, row.regex_set),
            job["n_positive"],
            job["n_negative"],
            job["seed"],
            validate=True,
            local_extended=job.get("local_extended", False),
            include_negative_tags=job.get("include_negative_tags", False),
        )
        row.positives = len(positives)
        if job.get("include_negative_tags", False):
            negative_pairs = negatives
            row.negative_tags = {}
            for _sample, tag in negative_pairs:
                row.negative_tags[tag] = row.negative_tags.get(tag, 0) + 1
            negative_samples = [sample for sample, _tag in negative_pairs]
        else:
            negative_samples = negatives
        row.negatives = len(negative_samples)
        samples = list(dict.fromkeys(positives + negative_samples))

        report_dir = (
            Path(job["out_dir"])
            / "coverage_reports"
            / job.get("mode_dir", "")
            / f"run_{job['run_index']:03d}"
            / target_name
            / variant_name
        )
        html_dir = report_dir / "html"
        json_path = report_dir / "coverage.json"
        accepted, rejected, totals = benchmark.run_validator_with_coverage(
            samples,
            spec.validator_fn,
            spec.target_module,
            str(html_dir),
            str(json_path),
        )
        statements = int(totals.get("num_statements", 0) or 0)
        branches = int(totals.get("num_branches", 0) or 0)
        covered_lines = int(totals.get("covered_lines", 0) or 0)
        covered_branches = int(totals.get("covered_branches", 0) or 0)
        row.accepted = accepted
        row.rejected = rejected
        row.code_coverage = 100.0 * covered_lines / statements if statements else 0.0
        row.branch_coverage = 100.0 * benchmark.compute_branch_coverage(totals)
        row.covered_lines = covered_lines
        row.covered_branches = covered_branches
        row.coverage_summary = totals
    except Exception as exc:
        row.status = "error"
        row.error = f"{type(exc).__name__}: {exc}"
        row.coverage_summary = {}
    return row


def run_all(jobs, workers):
    rows = []
    with ProcessPoolExecutor(
        max_workers=workers,
        max_tasks_per_child=1,
    ) as executor:
        futures = {executor.submit(run_job, job): job for job in jobs}
        for completed, future in enumerate(as_completed(futures), start=1):
            row = future.result()
            rows.append(row)
            print(
                f"[{completed}/{len(futures)}] {row.target}/{row.variant} "
                f"run={row.run_index} status={row.status}",
                flush=True,
            )
    return sorted(rows, key=lambda row: (row.run_index, row.target, row.variant))


def _paired_deltas(rows, baseline, comparison, metric):
    lookup = {
        (row.run_index, row.target, row.variant): getattr(row, metric)
        for row in rows
        if row.status == "ok"
    }
    return [
        lookup[(run_index, target, baseline)]
        - lookup[(run_index, target, comparison)]
        for run_index, target, _variant in lookup
        if (run_index, target, baseline) in lookup
        and (run_index, target, comparison) in lookup
    ]


def summarize(
    rows,
    targets,
    variants,
    *,
    stats_iterations=10000,
    seed=0,
    baseline="full",
):
    import numpy as np

    from benchmarks.python_regex_function_coverage_benchmark import (
        bootstrap_mean_ci,
        holm_bonferroni_adjust,
        sign_flip_permutation_pvalue,
    )

    ok_rows = [row for row in rows if row.status == "ok"]
    per_target = {}
    for target in targets:
        per_target[target] = {}
        for variant in variants:
            selected = [
                row for row in ok_rows
                if row.target == target and row.variant == variant
            ]
            per_target[target][variant] = {
                metric: (
                    float(np.mean([getattr(row, metric) for row in selected]))
                    if selected else None
                )
                for metric in ("code_coverage", "branch_coverage")
            }

    overall = {}
    for variant in variants:
        overall[variant] = {
            metric: (
                float(np.mean([
                    per_target[target][variant][metric]
                    for target in targets
                    if per_target[target][variant][metric] is not None
                ]))
                if any(
                    per_target[target][variant][metric] is not None
                    for target in targets
                ) else None
            )
            for metric in ("code_coverage", "branch_coverage")
        }

    tests = []
    comparisons = (
        [variant for variant in variants if variant != baseline]
        if baseline in variants else []
    )
    for variant in comparisons:
        for metric in ("code_coverage", "branch_coverage"):
            deltas = np.asarray(_paired_deltas(rows, baseline, variant, metric), dtype=float)
            rng = np.random.default_rng(seed + len(tests))
            low, high = bootstrap_mean_ci(deltas, rng, stats_iterations)
            p_value = sign_flip_permutation_pvalue(deltas, rng, stats_iterations)
            target_counts = {"better": 0, "tied": 0, "worse": 0}
            target_deltas = []
            for target in targets:
                baseline_value = per_target[target][baseline][metric]
                other_value = per_target[target][variant][metric]
                if baseline_value is None or other_value is None:
                    continue
                delta = baseline_value - other_value
                target_deltas.append(delta)
                if abs(delta) <= 1e-12:
                    target_counts["tied"] += 1
                elif delta > 0:
                    target_counts["better"] += 1
                else:
                    target_counts["worse"] += 1
            tests.append({
                "variant": variant,
                "metric": metric,
                "mean_delta_points": float(np.mean(deltas)) if deltas.size else 0.0,
                "bootstrap_95_ci": [low, high],
                "p_value": p_value,
                "target_counts": target_counts,
            })
    holm_bonferroni_adjust(tests)
    violations = []
    if baseline in variants and "no_pos" in variants:
        for run_index in sorted({row.run_index for row in ok_rows}):
            for target in targets:
                full = next((row for row in ok_rows if row.run_index == run_index and row.target == target and row.variant == baseline), None)
                no_pos = next((row for row in ok_rows if row.run_index == run_index and row.target == target and row.variant == "no_pos"), None)
                if full and no_pos and (
                    full.code_coverage + 1e-12 < no_pos.code_coverage
                    or full.branch_coverage + 1e-12 < no_pos.branch_coverage
                ):
                    violations.append({
                        "run_index": run_index,
                        "target": target,
                        baseline: {
                            "code_coverage": full.code_coverage,
                            "branch_coverage": full.branch_coverage,
                        },
                        "no_pos": {
                            "code_coverage": no_pos.code_coverage,
                            "branch_coverage": no_pos.branch_coverage,
                        },
                    })
    if violations:
        print(f"Coverage superset invariant violations: {json.dumps(violations)}")
    negative_tag_counts = {}
    for variant in variants:
        counts = {}
        for row in ok_rows:
            if row.variant != variant:
                continue
            for tag, count in row.negative_tags.items():
                counts[tag] = counts.get(tag, 0) + count
        negative_tag_counts[variant] = counts
    return {
        "per_target": per_target,
        "overall_mean_over_targets": overall,
        "paired_tests": tests,
        "baseline": baseline,
        "negative_tag_counts": negative_tag_counts,
        "superset_invariant_violations": violations,
        "error_rows": [asdict(row) for row in rows if row.status != "ok"],
    }


def write_outputs(rows, summary, config, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "results.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(asdict(row), sort_keys=True) + "\n")
    with (out_dir / "summary.json").open("w", encoding="utf-8") as handle:
        json.dump({"config": config, **summary}, handle, indent=2, sort_keys=True)

    variants = config["variants"]
    targets = config["targets"]
    with (out_dir / "table.tex").open("w", encoding="utf-8") as handle:
        handle.write("\\begin{tabular}{l" + "rr" * len(variants) + "}\n")
        handle.write("\\toprule\nTarget & " + " & ".join(
            f"\\multicolumn{{2}}{{c}}{{{_latex_escape(name)}}}"
            for name in variants
        ) + " \\\\\n")
        handle.write(" & " + " & ".join(["Code & Branch"] * len(variants)) + " \\\\\n")
        handle.write("\\midrule\n")
        for target in targets:
            values = []
            for variant in variants:
                metrics = summary["per_target"][target][variant]
                values.extend(
                    "--" if metrics[metric] is None else f"{metrics[metric]:.2f}"
                    for metric in ("code_coverage", "branch_coverage")
                )
            handle.write(_latex_escape(target) + " & " + " & ".join(values) + " \\\\\n")
        handle.write("\\bottomrule\n\\end{tabular}\n")

    _write_delta_table(summary, variants, targets, out_dir / "delta_table.tex")


_METRICS = ("code_coverage", "branch_coverage")


def _comparison_variants(summary, variants):
    baseline = summary.get("baseline", "full")
    if baseline not in variants:
        return baseline, []
    return baseline, [variant for variant in variants if variant != baseline]


def _format_delta(value):
    return "--" if value is None else f"{value:+.2f}"


def _write_delta_table(summary, variants, targets, path):
    """Write per-target coverage deltas (baseline minus variant), in points.

    A positive delta means the baseline (the full system) covers more than
    the ablated variant.
    """

    baseline, comparisons = _comparison_variants(summary, variants)
    if not comparisons:
        return
    per_target = summary["per_target"]
    tests = {
        (test["variant"], test["metric"]): test
        for test in summary.get("paired_tests", [])
    }

    def delta(target, variant, metric):
        base = per_target[target][baseline][metric]
        other = per_target[target][variant][metric]
        return None if base is None or other is None else base - other

    with path.open("w", encoding="utf-8") as handle:
        handle.write("\\begin{tabular}{l" + "rr" * len(comparisons) + "}\n")
        handle.write("\\toprule\nTarget & " + " & ".join(
            f"\\multicolumn{{2}}{{c}}{{$\\Delta$ "
            f"{_latex_escape(baseline)} $-$ {_latex_escape(variant)}}}"
            for variant in comparisons
        ) + " \\\\\n")
        handle.write(" & " + " & ".join(["Code & Branch"] * len(comparisons)) + " \\\\\n")
        handle.write("\\midrule\n")
        for target in targets:
            values = [
                _format_delta(delta(target, variant, metric))
                for variant in comparisons
                for metric in _METRICS
            ]
            handle.write(_latex_escape(target) + " & " + " & ".join(values) + " \\\\\n")
        if tests:
            handle.write("\\midrule\n")
            rows = {"Mean": [], "95\\% CI": [], "$p$ (Holm)": []}
            for variant in comparisons:
                for metric in _METRICS:
                    test = tests.get((variant, metric))
                    if test is None:
                        for cells in rows.values():
                            cells.append("--")
                        continue
                    low, high = test["bootstrap_95_ci"]
                    rows["Mean"].append(_format_delta(test["mean_delta_points"]))
                    rows["95\\% CI"].append(f"[{low:+.2f}, {high:+.2f}]")
                    rows["$p$ (Holm)"].append(
                        f"{test.get('p_value_holm', test['p_value']):.4f}"
                    )
            for label, cells in rows.items():
                handle.write(label + " & " + " & ".join(cells) + " \\\\\n")
        handle.write("\\bottomrule\n\\end{tabular}\n")


def print_delta_report(summary):
    """Print the paired coverage deltas (baseline minus each variant)."""

    tests = summary.get("paired_tests", [])
    if not tests:
        return
    baseline = summary.get("baseline", "full")
    print(f"\nCoverage delta: {baseline} minus variant, in percentage points "
          "(positive = the full system covers more)")
    header = (
        f"{'variant':<12} {'metric':<7} {'mean delta':>10}  "
        f"{'95% CI':<18} {'p (Holm)':>8}  better/tied/worse"
    )
    print(header)
    print("-" * len(header))
    for test in tests:
        low, high = test["bootstrap_95_ci"]
        counts = test["target_counts"]
        print(
            f"{test['variant']:<12} {test['metric'].split('_')[0]:<7} "
            f"{test['mean_delta_points']:>+10.2f}  "
            f"{f'[{low:+.2f}, {high:+.2f}]':<18} "
            f"{test.get('p_value_holm', test['p_value']):>8.4f}  "
            f"{counts['better']}/{counts['tied']}/{counts['worse']}"
        )


def _latex_escape(text):
    return str(text).replace("_", "\\_")
