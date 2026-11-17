import argparse
import itertools
import json
from pathlib import Path

import numpy as np

from .runner import run_all, summarize
from .variants import VARIANTS


FACTORIAL_VARIANTS = [
    f"p{positive}l{local}m{mutation}"
    for positive in (0, 1)
    for local in (0, 1)
    for mutation in (0, 1)
]
DEFAULT_VARIANTS = FACTORIAL_VARIANTS + ["no_pos"]
COMPONENTS = ("P", "L", "M")
METRICS = ("code_coverage", "branch_coverage")


def _arguments():
    parser = argparse.ArgumentParser(description="Run the P x L x M factorial ablation.")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--variants", default=",".join(DEFAULT_VARIANTS))
    parser.add_argument("--targets", default="all")
    parser.add_argument("--n-positive", type=int, default=1000)
    parser.add_argument("--n-negative", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--stats-iterations", type=int, default=10000)
    parser.add_argument(
        "--out-dir",
        default=str(Path(__file__).resolve().parent / "outputs" / "factorial"),
    )
    return parser.parse_args()


def factorial_effects(summary, targets, variants=FACTORIAL_VARIANTS):
    per_target = {}
    for target in targets:
        target_values = {
            metric: {
                name: summary["per_target"][target][name][metric]
                for name in variants
            }
            for metric in METRICS
        }
        if any(
            value is None
            for metric_values in target_values.values()
            for value in metric_values.values()
        ):
            per_target[target] = {"status": "incomplete"}
            continue
        per_target[target] = {}
        for metric in METRICS:
            values = target_values[metric]
            main_effects = {}
            for component, position in zip(COMPONENTS, range(3)):
                high = [value for name, value in values.items() if int(name[position * 2 + 1]) == 1]
                low = [value for name, value in values.items() if int(name[position * 2 + 1]) == 0]
                main_effects[component] = float(np.mean(high) - np.mean(low))

            interactions = {}
            for first, second in itertools.combinations(range(3), 2):
                sign_sum = sum(
                    ((1 if int(name[first * 2 + 1]) else -1)
                     * (1 if int(name[second * 2 + 1]) else -1))
                    * value
                    for name, value in values.items()
                )
                interactions[f"{COMPONENTS[first]}x{COMPONENTS[second]}"] = float(
                    sign_sum / 4.0
                )
            per_target[target][metric] = {
                "main_effects_points": main_effects,
                "two_way_interactions_points": interactions,
            }

    overall = {}
    for metric in METRICS:
        complete = [
            target for target in targets
            if per_target[target].get("status") != "incomplete"
        ]
        overall[metric] = {
            key: {
                component: (
                    float(np.mean([
                        per_target[target][metric][key][component]
                        for target in complete
                    ]))
                    if complete else None
                )
                for component in (
                    COMPONENTS if key == "main_effects_points"
                    else ("PxL", "PxM", "LxM")
                )
            }
            for key in ("main_effects_points", "two_way_interactions_points")
        }
    complete_targets = [
        target for target in targets
        if per_target[target].get("status") != "incomplete"
    ]
    return {
        "per_target": per_target,
        "complete_targets": complete_targets,
        "overall_mean_over_targets": overall,
    }


def shapley_contributions(summary, targets, variants=FACTORIAL_VARIANTS):
    per_target = {}
    for target in targets:
        target_values = {
            metric: {
                name: summary["per_target"][target][name][metric]
                for name in variants
            }
            for metric in METRICS
        }
        if any(
            value is None
            for metric_values in target_values.values()
            for value in metric_values.values()
        ):
            per_target[target] = {"status": "incomplete"}
            continue
        per_target[target] = {}
        for metric in METRICS:
            values = target_values[metric]
            contribution = {component: 0.0 for component in COMPONENTS}
            orders = list(itertools.permutations(COMPONENTS))
            for order in orders:
                active = {"P": 0, "L": 0, "M": 0}
                previous = values["p0l0m0"]
                for component in order:
                    active[component] = 1
                    name = f"p{active['P']}l{active['L']}m{active['M']}"
                    current = values[name]
                    contribution[component] += (current - previous) / len(orders)
                    previous = current
            total_gain = values["p1l1m1"] - values["p0l0m0"]
            if abs(sum(contribution.values()) - total_gain) > 1e-9:
                raise AssertionError(
                    f"Shapley contributions do not sum to total gain for {target}/{metric}"
                )
            shares = {
                component: (
                    contribution[component] / total_gain
                    if abs(total_gain) > 1e-15 else None
                )
                for component in COMPONENTS
            }
            per_target[target][metric] = {
                "contribution_points": contribution,
                "share_of_total_gain": shares,
                "total_gain_points": total_gain,
            }

    overall = {}
    complete_targets = [
        target for target in targets
        if per_target[target].get("status") != "incomplete"
    ]
    for metric in METRICS:
        contribution = {
            component: float(np.mean([
                per_target[target][metric]["contribution_points"][component]
                for target in complete_targets
            ])) if complete_targets else 0.0
            for component in COMPONENTS
        }
        baseline = float(np.mean([
            summary["per_target"][target]["p0l0m0"][metric]
            for target in complete_targets
        ])) if complete_targets else 0.0
        full = float(np.mean([
            summary["per_target"][target]["p1l1m1"][metric]
            for target in complete_targets
        ])) if complete_targets else 0.0
        total_gain = full - baseline
        if abs(sum(contribution.values()) - total_gain) > 1e-9:
            raise AssertionError(f"Overall Shapley contributions do not sum for {metric}")
        overall[metric] = {
            "contribution_points": contribution,
            "share_of_total_gain": {
                component: (
                    contribution[component] / total_gain
                    if abs(total_gain) > 1e-15 else None
                )
                for component in COMPONENTS
            },
            "total_gain_points": total_gain,
        }
    return {
        "per_target": per_target,
        "complete_targets": complete_targets,
        "overall_mean_over_targets": overall,
    }


def attributable_coverage(rows, out_dir, variants=FACTORIAL_VARIANTS):
    coverage_by_mode = {}
    missing_reports = []
    for row in rows:
        if row.status != "ok" or row.variant not in variants:
            continue
        mode = "extended_local" if row.local_extended else "natural"
        report_path = (
            Path(out_dir)
            / "coverage_reports"
            / mode
            / f"run_{row.run_index:03d}"
            / row.target
            / row.variant
            / "coverage.json"
        )
        if not report_path.is_file():
            missing_reports.append({
                "mode": mode,
                "run_index": row.run_index,
                "target": row.target,
                "variant": row.variant,
                "path": str(report_path),
            })
            continue
        with report_path.open(encoding="utf-8") as handle:
            report = json.load(handle)
        config_sets = coverage_by_mode.setdefault(mode, {}).setdefault(
            row.variant, {"lines": set(), "branches": set()}
        )
        for filename, details in report.get("files", {}).items():
            config_sets["lines"].update(
                (filename, int(line))
                for line in details.get("executed_lines", [])
            )
            config_sets["branches"].update(
                (filename, int(arc[0]), int(arc[1]))
                for arc in details.get("executed_branches", [])
                if isinstance(arc, (list, tuple)) and len(arc) == 2
            )

    output = {"missing_reports": missing_reports}
    for mode, config_sets in coverage_by_mode.items():
        component_results = {}
        config_unions = {
            name: {
                "covered_line_count": len(sets["lines"]),
                "covered_branch_count": len(sets["branches"]),
            }
            for name, sets in config_sets.items()
        }
        for component, bit_index in zip(COMPONENTS, range(3)):
            with_component = set()
            without_component = set()
            with_lines = set()
            without_lines = set()
            for name in variants:
                if name not in config_sets:
                    continue
                on = int(name[bit_index * 2 + 1]) == 1
                branch_set = config_sets[name]["branches"]
                line_set = config_sets[name]["lines"]
                (with_component if on else without_component).update(branch_set)
                (with_lines if on else without_lines).update(line_set)
            attributable_branches = with_component - without_component
            attributable_lines = with_lines - without_lines
            component_results[component] = {
                "branches_covered_with_component": len(with_component),
                "branches_covered_without_component": len(without_component),
                "attributable_branch_count": len(attributable_branches),
                "attributable_branches": [
                    list(item) for item in sorted(attributable_branches)
                ],
                "attributable_line_count": len(attributable_lines),
            }
        output[mode] = {
            "configuration_coverage_union": config_unions,
            "components": component_results,
        }
    return output


def _write_results(rows, out_dir):
    with (Path(out_dir) / "results.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            from dataclasses import asdict

            handle.write(json.dumps(asdict(row), sort_keys=True) + "\n")


def _write_factorial_table(mode_summary, variants, out_dir, filename):
    summary = mode_summary["coverage"]
    shapley = mode_summary["shapley"]["overall_mean_over_targets"]
    lines = [
        r"\begin{tabular}{lccc rrrr}",
        r"\toprule",
        r"Configuration & P & L & M & Code & Branch & \#pos & \#neg \\",
        r"\midrule",
    ]
    for variant in variants:
        if variant == "no_pos":
            bits = (0, 1, 1)
        else:
            bits = tuple(int(variant[position]) for position in (1, 3, 5))
        metrics = summary["overall_mean_over_targets"].get(variant, {})
        selected = [
            row for row in mode_summary["rows"] if row.variant == variant
        ]
        pos = float(np.mean([row.positives for row in selected])) if selected else 0.0
        neg = float(np.mean([row.negatives for row in selected])) if selected else 0.0
        checks = ["\\checkmark" if bit else "" for bit in bits]
        code = metrics.get("code_coverage")
        branch = metrics.get("branch_coverage")
        cells = [
            variant,
            *checks,
            "--" if code is None else f"{code:.2f}",
            "--" if branch is None else f"{branch:.2f}",
            f"{pos:.1f}",
            f"{neg:.1f}",
        ]
        lines.append(" & ".join(cells) + r" \\")
    share_cells = []
    for metric in METRICS:
        shares = shapley[metric]["share_of_total_gain"]
        share_cells.append(
            ", ".join(
                f"{component} {shares[component]:.1%}"
                if shares[component] is not None else f"{component} --"
                for component in COMPONENTS
            )
        )
    lines.extend([
        r"\midrule",
        "Shapley shares & & & & "
        + share_cells[0]
        + " & "
        + share_cells[1]
        + " & & \\\\",
        r"\bottomrule",
        r"\end{tabular}",
    ])
    (Path(out_dir) / filename).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    args = _arguments()
    variants = [value.strip() for value in args.variants.split(",") if value.strip()]
    invalid = sorted(set(variants) - set(DEFAULT_VARIANTS))
    if invalid:
        raise SystemExit(f"Unknown factorial variant(s): {', '.join(invalid)}")
    if not set(FACTORIAL_VARIANTS).issubset(variants):
        raise SystemExit("Factorial studies require all eight p0/1l0/1m0/1 configurations")
    if args.runs < 1 or args.workers < 1 or args.stats_iterations < 1:
        raise SystemExit("--runs, --workers, and --stats-iterations must be at least 1")
    if args.n_positive < 0 or args.n_negative < 0:
        raise SystemExit("--n-positive and --n-negative must be non-negative")

    from benchmarks.python_regex_function_coverage_benchmark import (
        SEED,
        build_target_specs,
    )
    target_names = {spec.name for spec in build_target_specs()}
    if args.targets.strip().lower() == "all":
        targets = sorted(target_names)
    else:
        targets = [value.strip() for value in args.targets.split(",") if value.strip()]
        unknown = sorted(set(targets) - target_names)
        if unknown:
            raise SystemExit(f"Unknown target(s): {', '.join(unknown)}")
    if not targets:
        raise SystemExit("At least one target is required")

    seed = SEED if args.seed is None else args.seed
    out_dir = Path(args.out_dir)
    jobs = []
    for local_extended, mode_dir in ((False, "natural"), (True, "extended_local")):
        jobs.extend(
            {
                "target": target,
                "variant": variant,
                "run_index": run_index,
                "seed": seed + run_index,
                "n_positive": args.n_positive,
                "n_negative": args.n_negative,
                "out_dir": str(out_dir),
                "mode_dir": mode_dir,
                "local_extended": local_extended,
                "include_negative_tags": True,
            }
            for run_index in range(args.runs)
            for target in targets
            for variant in variants
        )

    rows_by_mode = {}
    for local_extended, mode_dir in ((False, "natural"), (True, "extended_local")):
        mode_jobs = [job for job in jobs if job["mode_dir"] == mode_dir]
        rows_by_mode[mode_dir] = run_all(mode_jobs, args.workers)
    all_rows = rows_by_mode["natural"] + rows_by_mode["extended_local"]

    mode_summaries = {}
    for mode, mode_rows in rows_by_mode.items():
        coverage = summarize(
            mode_rows,
            targets,
            variants,
            stats_iterations=args.stats_iterations,
            seed=seed,
            baseline="p1l1m1",
        )
        mode_summary = {
            "coverage": coverage,
            "effects": factorial_effects(coverage, targets),
            "shapley": shapley_contributions(coverage, targets),
            "rows": mode_rows,
        }
        mode_summaries[mode] = mode_summary

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_results(all_rows, out_dir)
    summary_json = {
        "config": {
            "study": "factorial",
            "runs": args.runs,
            "seed": seed,
            "variants": variants,
            "targets": targets,
            "n_positive": args.n_positive,
            "n_negative": args.n_negative,
            "workers": args.workers,
            "stats_iterations": args.stats_iterations,
            "negative_modes": ["natural", "extended_local"],
        },
        "modes": {
            mode: {
                "coverage": data["coverage"],
                "effects": data["effects"],
                "shapley": data["shapley"],
            }
            for mode, data in mode_summaries.items()
        },
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary_json, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    attributable = attributable_coverage(all_rows, out_dir)
    (out_dir / "attributable.json").write_text(
        json.dumps(attributable, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_factorial_table(
        mode_summaries["natural"], variants, out_dir, "factorial_table.tex"
    )
    _write_factorial_table(
        mode_summaries["extended_local"],
        variants,
        out_dir,
        "factorial_table_extended.tex",
    )


if __name__ == "__main__":
    main()
