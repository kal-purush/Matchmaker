import argparse
import json
import os
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from email_validator_tool_coverage import SEED, require
from regex_positive_generator.benchmarks.python_regex_function_coverage_benchmark import (
    SampleCase,
    TargetSpec,
    DEFAULT_EXTERNAL_TOOL_TIMEOUT_SECONDS,
    build_target_specs,
    build_tool_error_message,
    compute_branch_coverage,
    get_tool_failure_details,
    get_tool_samples,
    run_validator_with_coverage,
    safe_name,
    write_jsonl_row,
)


BASELINE_TOOL_NAME = "RegexInstantiator"
COMBINED_TOOL_NAME = "CombinedOtherTools"
COMBINED_TOOL_KEYS = ("exrex", "xeger", "egret", "generex", "rgxgen", "randexp", "mutrex")

DEFAULT_RUNS = 10
DEFAULT_N_POSITIVE = 1000
DEFAULT_N_NEGATIVE = 1000
DEFAULT_WORKERS = 1
DEFAULT_BASELINE_BUDGET_MODE = "per-tool"
DEFAULT_COVERAGE_ROOT = "python_regex_function_combined_tools_coverage_reports"
DEFAULT_RESULTS_JSONL = "regex_positive_generator/benchmarks/coverage_outputs/python_regex_function_combined_tools_coverage_results.jsonl"
DEFAULT_SUMMARY_JSON = "regex_positive_generator/benchmarks/coverage_outputs/python_regex_function_combined_tools_coverage_summary.json"
DEFAULT_RESULTS_JSON = "python_regex_function_combined_tools_coverage_results.json"

TOOL_LABELS = {
    "ours": BASELINE_TOOL_NAME,
    "exrex": "exrex",
    "xeger": "Xeger",
    "egret": "EGRET",
    "generex": "Generex",
    "rgxgen": "RgxGen",
    "randexp": "RandExp.js",
    "mutrex": "MutRex",
}


@dataclass
class CombinedCoverageRun:
    target: str
    tool: str
    regex: str
    positives_requested: int
    negatives_requested: int
    positives: int
    negatives: int
    total_samples: int
    unique_samples: int
    accepted: int
    rejected: int
    code_coverage: float
    branch_coverage: float
    coverage_summary: Dict[str, object]
    run_index: int
    seed: int
    component_tools: List[str]
    status: str = "ok"
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    component_failures: Optional[List[Dict[str, object]]] = None
    java_regex_valid: Optional[bool] = None
    java_regex_error: Optional[str] = None
    java_regex_error_index: Optional[int] = None
    java_regex_error_pattern: Optional[str] = None


def dedupe_cases(cases: List[SampleCase]) -> List[SampleCase]:
    seen = set()
    unique: List[SampleCase] = []
    for case in cases:
        key = case.sample
        if key in seen:
            continue
        seen.add(key)
        unique.append(case)
    return unique


def generate_baseline_cases(
    pattern: str,
    n_positive: int,
    n_negative: int,
    seed: int,
    timeout_seconds: float,
) -> List[SampleCase]:
    return get_tool_samples(
        pattern,
        "ours",
        n_pos=n_positive,
        n_neg=n_negative,
        seed=seed,
        timeout_seconds=timeout_seconds,
    )


def build_component_failure(
    pattern: str,
    tool_key: str,
    exc: Exception,
    timeout_seconds: float,
) -> Dict[str, object]:
    failure_details = get_tool_failure_details(
        pattern,
        tool_key,
        exc,
        timeout_seconds=timeout_seconds,
    )
    return {
        "tool_key": tool_key,
        "tool": TOOL_LABELS[tool_key],
        "error_type": type(exc).__name__,
        "error_message": build_tool_error_message(TOOL_LABELS[tool_key], exc, failure_details)[:1000],
        "java_regex_valid": failure_details.get("java_regex_valid"),
        "java_regex_error": failure_details.get("java_regex_error"),
        "java_regex_error_index": failure_details.get("java_regex_error_index"),
        "java_regex_error_pattern": failure_details.get("java_regex_error_pattern"),
        "failure_details": failure_details,
    }


def generate_combined_other_tool_cases(
    pattern: str,
    n_positive: int,
    n_negative: int,
    seed: int,
    timeout_seconds: float,
) -> tuple[List[SampleCase], List[Dict[str, object]]]:
    cases: List[SampleCase] = []
    failures: List[Dict[str, object]] = []
    for offset, tool_key in enumerate(COMBINED_TOOL_KEYS):
        tool_seed = seed + offset
        try:
            cases.extend(get_tool_samples(
                pattern,
                tool_key,
                n_pos=n_positive,
                n_neg=n_negative,
                seed=tool_seed,
                timeout_seconds=timeout_seconds,
            ))
        except Exception as exc:
            failures.append(build_component_failure(pattern, tool_key, exc, timeout_seconds))
    return dedupe_cases(cases), failures


def build_error_run(
    *,
    spec: TargetSpec,
    tool: str,
    positives_requested: int,
    negatives_requested: int,
    run_index: int,
    seed: int,
    component_tools: List[str],
    exc: Exception,
    component_failures: Optional[List[Dict[str, object]]] = None,
) -> CombinedCoverageRun:
    return CombinedCoverageRun(
        target=spec.name,
        tool=tool,
        regex=spec.regex,
        positives_requested=positives_requested,
        negatives_requested=negatives_requested,
        positives=0,
        negatives=0,
        total_samples=0,
        unique_samples=0,
        accepted=0,
        rejected=0,
        code_coverage=0.0,
        branch_coverage=0.0,
        coverage_summary={},
        run_index=run_index,
        seed=seed,
        component_tools=component_tools,
        status="error",
        error_type=type(exc).__name__,
        error_message=str(exc)[:1000],
        component_failures=component_failures or [],
    )


def run_coverage_case(
    spec: TargetSpec,
    *,
    tool: str,
    cases: List[SampleCase],
    positives_requested: int,
    negatives_requested: int,
    run_index: int,
    seed: int,
    component_tools: List[str],
    coverage_root: str,
    component_failures: Optional[List[Dict[str, object]]] = None,
) -> CombinedCoverageRun:
    samples = [case.sample for case in cases]
    positives = sum(1 for case in cases if case.expected_match)
    negatives = len(cases) - positives
    safe_tool = safe_name(tool)
    html_dir = os.path.join(coverage_root, f"run_{run_index:03d}", spec.name, safe_tool, "html")
    json_path = os.path.join(coverage_root, f"run_{run_index:03d}", spec.name, safe_tool, "coverage.json")
    accepted, rejected, coverage_summary = run_validator_with_coverage(
        samples=samples,
        validator_fn=spec.validator_fn,
        target_module=spec.target_module,
        html_dir=html_dir,
        json_path=json_path,
    )
    return CombinedCoverageRun(
        target=spec.name,
        tool=tool,
        regex=spec.regex,
        positives_requested=positives_requested,
        negatives_requested=negatives_requested,
        positives=positives,
        negatives=negatives,
        total_samples=len(samples),
        unique_samples=len(set(samples)),
        accepted=accepted,
        rejected=rejected,
        code_coverage=(coverage_summary.get("percent_covered", 0.0) or 0.0) / 100.0,
        branch_coverage=compute_branch_coverage(coverage_summary),
        coverage_summary=coverage_summary,
        run_index=run_index,
        seed=seed,
        component_tools=component_tools,
        component_failures=component_failures or [],
    )


def run_job(job) -> CombinedCoverageRun:
    (
        run_index,
        seed,
        target_name,
        suite_key,
        n_positive,
        n_negative,
        coverage_root,
        external_tool_timeout_seconds,
    ) = job
    specs_by_name = {spec.name: spec for spec in build_target_specs()}
    spec = specs_by_name[target_name]

    try:
        component_failures: List[Dict[str, object]] = []
        if suite_key == "baseline":
            tool = BASELINE_TOOL_NAME
            cases = generate_baseline_cases(
                spec.regex,
                n_positive,
                n_negative,
                seed,
                external_tool_timeout_seconds,
            )
            positives_requested = n_positive
            negatives_requested = n_negative
            component_tools = [BASELINE_TOOL_NAME]
        elif suite_key == "combined_other_tools":
            tool = COMBINED_TOOL_NAME
            cases, component_failures = generate_combined_other_tool_cases(
                spec.regex,
                n_positive,
                n_negative,
                seed,
                external_tool_timeout_seconds,
            )
            positives_requested = n_positive * len(COMBINED_TOOL_KEYS)
            negatives_requested = n_negative * len(COMBINED_TOOL_KEYS)
            component_tools = [TOOL_LABELS[key] for key in COMBINED_TOOL_KEYS]
            if not cases:
                raise RuntimeError("All combined component tools failed or generated no samples.")
        else:
            raise ValueError(f"Unknown suite key: {suite_key}")

        run = run_coverage_case(
            spec,
            tool=tool,
            cases=cases,
            positives_requested=positives_requested,
            negatives_requested=negatives_requested,
            run_index=run_index,
            seed=seed,
            component_tools=component_tools,
            coverage_root=coverage_root,
            component_failures=component_failures,
        )
        if component_failures:
            run.status = "partial"
            run.error_type = "ComponentToolFailure"
            run.error_message = "; ".join(
                f"{failure['tool']}: {failure['error_message']}" for failure in component_failures
            )[:1000]
        return run
    except Exception as exc:
        if suite_key == "baseline":
            return build_error_run(
                spec=spec,
                tool=BASELINE_TOOL_NAME,
                positives_requested=n_positive,
                negatives_requested=n_negative,
                run_index=run_index,
                seed=seed,
                component_tools=[BASELINE_TOOL_NAME],
                exc=exc,
            )
        component_failures = locals().get("component_failures", [])
        return build_error_run(
            spec=spec,
            tool=COMBINED_TOOL_NAME,
            positives_requested=n_positive * len(COMBINED_TOOL_KEYS),
            negatives_requested=n_negative * len(COMBINED_TOOL_KEYS),
            run_index=run_index,
            seed=seed,
            component_tools=[TOOL_LABELS[key] for key in COMBINED_TOOL_KEYS],
            exc=exc,
            component_failures=component_failures,
        )


def build_jobs(
    target_specs: List[TargetSpec],
    *,
    runs: int,
    seed: int,
    n_positive: int,
    n_negative: int,
    baseline_positive: int,
    baseline_negative: int,
    coverage_root: str,
    external_tool_timeout_seconds: float,
) -> List[tuple]:
    jobs = []
    for run_index in range(runs):
        run_seed = seed + run_index
        for spec in target_specs:
            jobs.append((
                run_index,
                run_seed,
                spec.name,
                "baseline",
                baseline_positive,
                baseline_negative,
                coverage_root,
                external_tool_timeout_seconds,
            ))
            jobs.append((
                run_index,
                run_seed,
                spec.name,
                "combined_other_tools",
                n_positive,
                n_negative,
                coverage_root,
                external_tool_timeout_seconds,
            ))
    return jobs


def run_jobs_streaming(
    jobs: List[tuple],
    *,
    workers: int,
    results_jsonl: str,
    progress_every: int,
) -> List[CombinedCoverageRun]:
    results_dir = os.path.dirname(results_jsonl)
    if results_dir:
        os.makedirs(results_dir, exist_ok=True)

    runs: List[CombinedCoverageRun] = []
    total_jobs = len(jobs)
    completed = 0

    with open(results_jsonl, "w", encoding="utf-8") as jsonl_handle:
        def record_run(run: CombinedCoverageRun) -> None:
            nonlocal completed
            runs.append(run)
            completed += 1
            write_jsonl_row(jsonl_handle, asdict(run))
            if progress_every > 0 and (completed == 1 or completed % progress_every == 0 or completed == total_jobs):
                print(
                    f"[{completed}/{total_jobs}] run={run.run_index} target={run.target} tool={run.tool} "
                    f"status={run.status} code={run.code_coverage * 100:.2f}% "
                    f"branch={run.branch_coverage * 100:.2f}% "
                    f"component_failures={len(run.component_failures or [])}",
                    flush=True,
                )

        if workers <= 1:
            for job in jobs:
                record_run(run_job(job))
        else:
            try:
                with ProcessPoolExecutor(max_workers=workers) as executor:
                    futures = [executor.submit(run_job, job) for job in jobs]
                    for future in as_completed(futures):
                        record_run(future.result())
            except PermissionError as exc:
                print(f"Process pool unavailable ({exc}); falling back to sequential execution.", flush=True)
                for job in jobs:
                    record_run(run_job(job))

    runs.sort(key=lambda row: (row.run_index, row.target, row.tool))
    return runs


def aggregate_runs(runs: List[CombinedCoverageRun], target_specs: List[TargetSpec]) -> List[CombinedCoverageRun]:
    grouped: Dict[tuple, List[CombinedCoverageRun]] = {}
    for run in runs:
        if run.status in {"ok", "partial"}:
            grouped.setdefault((run.target, run.tool), []).append(run)

    specs_by_name = {spec.name: spec for spec in target_specs}
    aggregate: List[CombinedCoverageRun] = []
    for spec in target_specs:
        for tool in (BASELINE_TOOL_NAME, COMBINED_TOOL_NAME):
            group = grouped.get((spec.name, tool), [])
            if not group:
                continue
            count = len(group)
            aggregate.append(
                CombinedCoverageRun(
                    target=spec.name,
                    tool=tool,
                    regex=specs_by_name[spec.name].regex,
                    positives_requested=round(sum(run.positives_requested for run in group) / count),
                    negatives_requested=round(sum(run.negatives_requested for run in group) / count),
                    positives=round(sum(run.positives for run in group) / count),
                    negatives=round(sum(run.negatives for run in group) / count),
                    total_samples=round(sum(run.total_samples for run in group) / count),
                    unique_samples=round(sum(run.unique_samples for run in group) / count),
                    accepted=round(sum(run.accepted for run in group) / count),
                    rejected=round(sum(run.rejected for run in group) / count),
                    code_coverage=sum(run.code_coverage for run in group) / count,
                    branch_coverage=sum(run.branch_coverage for run in group) / count,
                    coverage_summary={
                        "runs": count,
                        "mean_percent_covered": sum(run.code_coverage for run in group) * 100.0 / count,
                        "mean_branch_coverage": sum(run.branch_coverage for run in group) / count,
                    },
                    run_index=-1,
                    seed=0,
                    component_tools=group[0].component_tools,
                    component_failures=[
                        failure
                        for run in group
                        for failure in (run.component_failures or [])
                    ],
                    status="aggregate",
                )
            )
    aggregate.sort(key=lambda row: (row.target, row.tool))
    return aggregate


def print_summary(runs: List[CombinedCoverageRun]) -> None:
    print("\nPython Regex Function Coverage: RegexInstantiator vs Combined Other Tools")
    print("=" * 150)
    print(
        f"{'Target':<30} {'Tool':<22} {'Req +':>7} {'Req -':>7} {'Gen +':>7} {'Gen -':>7} "
        f"{'Total':>8} {'Accepted':>9} {'Rejected':>9} {'Code':>8} {'Branch':>8}"
    )
    print("-" * 150)
    for run in runs:
        print(
            f"{run.target:<30} {run.tool:<22} {run.positives_requested:>7} {run.negatives_requested:>7} "
            f"{run.positives:>7} {run.negatives:>7} {run.total_samples:>8} "
            f"{run.accepted:>9} {run.rejected:>9} "
            f"{run.code_coverage * 100:>7.2f}% {run.branch_coverage * 100:>7.2f}%"
        )


def print_comparison(aggregate: List[CombinedCoverageRun], target_specs: List[TargetSpec]) -> None:
    lookup = {(run.target, run.tool): run for run in aggregate}
    print("\nCoverage Delta")
    print("=" * 92)
    print(f"{'Target':<30} {'Code Delta':>14} {'Branch Delta':>14} {'Winner':>22}")
    print("-" * 92)
    for spec in target_specs:
        baseline = lookup.get((spec.name, BASELINE_TOOL_NAME))
        combined = lookup.get((spec.name, COMBINED_TOOL_NAME))
        if baseline is None or combined is None:
            continue
        code_delta = (baseline.code_coverage - combined.code_coverage) * 100.0
        branch_delta = (baseline.branch_coverage - combined.branch_coverage) * 100.0
        if code_delta > 0:
            winner = BASELINE_TOOL_NAME
        elif code_delta < 0:
            winner = COMBINED_TOOL_NAME
        else:
            winner = "tie"
        print(f"{spec.name:<30} {code_delta:>+13.2f} {branch_delta:>+13.2f} {winner:>22}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare RegexInstantiator coverage against all other regex generators combined."
    )
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--n-positive", type=int, default=DEFAULT_N_POSITIVE)
    parser.add_argument("--n-negative", type=int, default=DEFAULT_N_NEGATIVE)
    parser.add_argument(
        "--baseline-budget-mode",
        choices=("per-tool", "combined"),
        default=DEFAULT_BASELINE_BUDGET_MODE,
        help=(
            "per-tool: RegexInstantiator gets --n-positive/--n-negative. "
            "combined: RegexInstantiator gets --n-positive/--n-negative multiplied by the number of combined tools."
        ),
    )
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--coverage-root", default=DEFAULT_COVERAGE_ROOT)
    parser.add_argument("--results-jsonl", default=DEFAULT_RESULTS_JSONL)
    parser.add_argument("--summary-json", default=DEFAULT_SUMMARY_JSON)
    parser.add_argument("--results-json", default=DEFAULT_RESULTS_JSON)
    parser.add_argument("--progress-every", type=int, default=1)
    parser.add_argument(
        "--external-tool-timeout-seconds",
        type=float,
        default=DEFAULT_EXTERNAL_TOOL_TIMEOUT_SECONDS,
        help="Timeout for each subprocess-backed generator invocation.",
    )
    args = parser.parse_args()

    require(args.runs > 0, "--runs must be positive")
    require(args.n_positive >= 0, "--n-positive must be non-negative")
    require(args.n_negative >= 0, "--n-negative must be non-negative")
    require(args.external_tool_timeout_seconds > 0, "--external-tool-timeout-seconds must be positive")

    if args.baseline_budget_mode == "combined":
        baseline_positive = args.n_positive * len(COMBINED_TOOL_KEYS)
        baseline_negative = args.n_negative * len(COMBINED_TOOL_KEYS)
    else:
        baseline_positive = args.n_positive
        baseline_negative = args.n_negative

    target_specs = build_target_specs()
    os.makedirs(args.coverage_root, exist_ok=True)
    for output_path in (args.results_jsonl, args.summary_json, args.results_json):
        output_dir = os.path.dirname(output_path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

    jobs = build_jobs(
        target_specs,
        runs=args.runs,
        seed=args.seed,
        n_positive=args.n_positive,
        n_negative=args.n_negative,
        baseline_positive=baseline_positive,
        baseline_negative=baseline_negative,
        coverage_root=args.coverage_root,
        external_tool_timeout_seconds=args.external_tool_timeout_seconds,
    )
    raw_runs = run_jobs_streaming(
        jobs,
        workers=max(1, args.workers),
        results_jsonl=args.results_jsonl,
        progress_every=args.progress_every,
    )
    aggregate = aggregate_runs(raw_runs, target_specs)

    output = {
        "runs": args.runs,
        "seed": args.seed,
        "external_tool_timeout_seconds": args.external_tool_timeout_seconds,
        "baseline": {
            "tool": BASELINE_TOOL_NAME,
            "budget_mode": args.baseline_budget_mode,
            "positives_requested_per_run": baseline_positive,
            "negatives_requested_per_run": baseline_negative,
        },
        "combined_other_tools": {
            "tool": COMBINED_TOOL_NAME,
            "component_tools": [TOOL_LABELS[key] for key in COMBINED_TOOL_KEYS],
            "positives_requested_per_run": args.n_positive * len(COMBINED_TOOL_KEYS),
            "negatives_requested_per_run": args.n_negative * len(COMBINED_TOOL_KEYS),
            "per_component_positives_requested": args.n_positive,
            "per_component_negatives_requested": args.n_negative,
        },
        "targets": [
            {
                "name": spec.name,
                "regex": spec.regex,
                "note": spec.note,
            }
            for spec in target_specs
        ],
        "results": [asdict(run) for run in aggregate],
        "raw_results_jsonl": os.path.abspath(args.results_jsonl),
        "notes": {
            "comparison": "Baseline is RegexInstantiator with n_positive+n_negative samples. CombinedOtherTools requests that same budget from each other tool, then deduplicates all returned samples before coverage measurement.",
            "combined_components": "CombinedOtherTools includes exrex, Xeger, EGRET, Generex, RgxGen, RandExp.js, and MutRex.",
            "partial_status": "A combined run with one or more failed component tools is marked partial; successful component samples are still measured and component_failures records why each failed component failed.",
            "tool_limitations": "exrex, Xeger, Generex, and RandExp.js generate positive samples only. MutRex emits a finite suite, so it may return fewer samples than requested.",
            "mutrex_semantics": "MutRex CONF/REJECT labels are checked with Python re.fullmatch; dialect disagreements are recorded as component failures.",
            "code_coverage": "coverage.py total percent_covered for the validator package/module.",
            "branch_coverage": "coverage.py covered_branches / num_branches for the validator package/module.",
        },
    }
    summary = {
        **output,
        "raw_status_counts": dict(Counter(run.status for run in raw_runs)),
        "raw_results": [asdict(run) for run in raw_runs],
    }

    with open(args.results_json, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2)
        fh.write("\n")
    with open(args.summary_json, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
        fh.write("\n")

    print_summary(aggregate)
    print_comparison(aggregate, target_specs)
    print(f"\nRaw JSONL saved to: {os.path.abspath(args.results_jsonl)}")
    print(f"Saved aggregate results to: {os.path.abspath(args.results_json)}")
    print(f"Summary saved to: {os.path.abspath(args.summary_json)}")
    print(f"Coverage reports saved under: {os.path.abspath(args.coverage_root)}")


if __name__ == "__main__":
    main()
