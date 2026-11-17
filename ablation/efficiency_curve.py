"""Efficiency curves: coverage as a function of the sample budget.

At 1000 samples per side the variants of an ablation often reach the same coverage;
the curve shows how fast each one gets there. For every budget k the variant is
asked for k positives and k negatives (a fresh generation with n=k, as a user with
that budget would get, not a prefix of the 1000-sample run) and coverage is measured
in a fresh process, so run-once code is counted at every k.

    python -m ablation.efficiency_curve --variants local_and_families,families_only,local_only
    python -m ablation.efficiency_curve --variants full,no_pos,random_pos

Outputs in --out-dir: results.jsonl, curves.csv (per target and the mean over
targets), summary.json (area under each curve) and curves.png.
"""

import argparse
import csv
import json
import math
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

from .runner import run_job
from .targets import TARGET_SETS, target_specs
from .validator_regexes import REGEX_SETS
from .variants import VARIANTS

DEFAULT_BUDGETS = "1,2,3,4,5,10,15,20,25,30,40,50,75,100,150,200,300,500,1000"
METRICS = ("code_coverage", "branch_coverage")
# Categorical slots 1-5 of the dataviz reference palette, in fixed order.
COLORS = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4")
MARKERS = ("o", "s", "^", "D", "v")


def _run_budget_job(job):
    row = asdict(run_job(job))
    row["budget"] = job["budget"]
    return row


def _arguments():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--variants", default="full,no_pos,random_pos")
    parser.add_argument("--targets", default="all")
    parser.add_argument("--target-set", choices=TARGET_SETS, default="ablation")
    parser.add_argument("--regexes", choices=REGEX_SETS, default="validator")
    parser.add_argument("--budgets", default=DEFAULT_BUDGETS)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out-dir", required=True)
    return parser.parse_args()


def _mean(values):
    return sum(values) / len(values) if values else None


def _curves(rows, targets, variants, budgets):
    """{target or "mean": {variant: {metric: [value per budget]}}}, averaged over runs."""
    by_key = {}
    for row in rows:
        if row["status"] == "ok":
            key = (row["target"], row["variant"], row["budget"])
            by_key.setdefault(key, []).append(row)
    curves = {}
    for target in targets:
        curves[target] = {
            variant: {
                metric: [
                    _mean([r[metric] for r in by_key.get((target, variant, k), [])])
                    for k in budgets
                ]
                for metric in METRICS
            }
            for variant in variants
        }
    curves["mean"] = {
        variant: {
            metric: [
                _mean([
                    curves[t][variant][metric][i]
                    for t in targets
                    if curves[t][variant][metric][i] is not None
                ])
                for i in range(len(budgets))
            ]
            for metric in METRICS
        }
        for variant in variants
    }
    return curves


def _area(values, budgets):
    """Normalised area under the curve on a log2 budget axis (mean coverage, in %)."""
    xs = [math.log2(k) for k in budgets]
    points = [(x, y) for x, y in zip(xs, values) if y is not None]
    if len(points) < 2:
        return points[0][1] if points else None
    area = sum((x1 - x0) * (y0 + y1) / 2 for (x0, y0), (x1, y1) in zip(points, points[1:]))
    return area / (points[-1][0] - points[0][0])


def _plot(curves, variants, budgets, path, title):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    panels = list(curves)  # "mean" last
    panels = ["mean"] + [name for name in panels if name != "mean"]
    columns = 3
    rows = math.ceil(len(panels) / columns) * 2
    fig, axes = plt.subplots(
        rows, columns, figsize=(4.2 * columns, 2.9 * rows), squeeze=False, sharex=True
    )
    for index, panel in enumerate(panels):
        for offset, metric in enumerate(METRICS):
            ax = axes[(index // columns) * 2 + offset][index % columns]
            for slot, variant in enumerate(variants):
                ax.plot(
                    budgets,
                    curves[panel][variant][metric],
                    color=COLORS[slot % len(COLORS)],
                    marker=MARKERS[slot % len(MARKERS)],
                    markersize=4,
                    linewidth=2,
                    label=variant,
                )
            ax.set_xscale("log")
            ax.set_title(f"{panel} - {metric.split('_')[0]}", fontsize=9)
            ax.grid(True, color="#e5e5e5", linewidth=0.6)
            ax.tick_params(labelsize=8)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
    for index in range(len(panels), (rows // 2) * columns):
        for offset in range(2):
            axes[(index // columns) * 2 + offset][index % columns].axis("off")
    for ax in axes[-1]:
        ax.set_xlabel("samples per side (k)", fontsize=8)
    for row_axes in axes:
        row_axes[0].set_ylabel("coverage (%)", fontsize=8)
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.suptitle(title, y=0.995, fontsize=11)
    fig.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, 0.98),
               ncol=len(variants), frameon=False, fontsize=9)
    fig.savefig(path, dpi=130, bbox_inches="tight")


def main():
    args = _arguments()
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    unknown = sorted(set(variants) - set(VARIANTS))
    if unknown:
        raise SystemExit(f"Unknown variant(s): {', '.join(unknown)}")
    budgets = sorted({int(k) for k in args.budgets.split(",") if k.strip()})
    if not budgets or budgets[0] < 1:
        raise SystemExit("--budgets must be positive integers")
    available = set(target_specs(args.target_set))
    if args.targets.strip().lower() == "all":
        targets = sorted(available)
    else:
        targets = [t.strip() for t in args.targets.split(",") if t.strip()]
        if set(targets) - available:
            raise SystemExit(f"Unknown target(s): {', '.join(sorted(set(targets) - available))}")

    from benchmarks.python_regex_function_coverage_benchmark import SEED

    seed = SEED if args.seed is None else args.seed
    regex_set = args.regexes if args.target_set == "benchmark" else "benchmark"
    out_dir = Path(args.out_dir)
    jobs = [
        {
            "target": target,
            "variant": variant,
            "run_index": run_index,
            "seed": seed + run_index,
            "n_positive": k,
            "n_negative": k,
            "budget": k,
            "out_dir": str(out_dir),
            "mode_dir": f"k_{k:04d}",
            "regex_set": regex_set,
            "target_set": args.target_set,
        }
        for run_index in range(args.runs)
        for target in targets
        for variant in variants
        for k in budgets
    ]

    rows = []
    with ProcessPoolExecutor(max_workers=args.workers, max_tasks_per_child=1) as pool:
        futures = [pool.submit(_run_budget_job, job) for job in jobs]
        for done, future in enumerate(as_completed(futures), start=1):
            row = future.result()
            rows.append(row)
            if row["status"] != "ok":
                print(f"{row['target']}/{row['variant']} k={row['budget']}: {row['error']}")
            if done % 50 == 0 or done == len(jobs):
                print(f"[{done}/{len(jobs)}]", flush=True)
    rows.sort(key=lambda r: (r["run_index"], r["target"], r["variant"], r["budget"]))

    curves = _curves(rows, targets, variants, budgets)
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "results.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            row.pop("coverage_summary", None)
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    with (out_dir / "curves.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["target", "variant", "metric", *budgets])
        for panel, by_variant in curves.items():
            for variant, by_metric in by_variant.items():
                for metric, values in by_metric.items():
                    writer.writerow([panel, variant, metric, *(
                        "" if v is None else f"{v:.2f}" for v in values
                    )])
    areas = {
        panel: {
            variant: {m: _area(curves[panel][variant][m], budgets) for m in METRICS}
            for variant in variants
        }
        for panel in curves
    }
    config = {
        "variants": variants, "targets": targets, "budgets": budgets, "runs": args.runs,
        "seed": seed, "target_set": args.target_set, "regex_set": regex_set,
    }
    with (out_dir / "summary.json").open("w", encoding="utf-8") as handle:
        json.dump({"config": config, "curves": curves, "area": areas}, handle, indent=2)
    _plot(curves, variants, budgets, out_dir / "curves.png",
          f"Coverage vs. budget ({args.target_set} targets, {args.runs} run(s))")

    for metric in METRICS:
        print(f"\nMean {metric} over {len(targets)} targets")
        print(f"{'k':>6} " + " ".join(f"{v:>20}" for v in variants))
        for i, k in enumerate(budgets):
            print(f"{k:>6} " + " ".join(
                f"{curves['mean'][v][metric][i]:>20.2f}" for v in variants
            ))
        print(f"{'AUC':>6} " + " ".join(f"{areas['mean'][v][metric]:>20.2f}" for v in variants))
    print(f"\nWrote {out_dir}")


if __name__ == "__main__":
    main()
