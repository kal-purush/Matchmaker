import argparse
import json
import multiprocessing
import os
import queue
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MPL_CONFIG_DIR = os.path.abspath(".matplotlib-cache")
os.makedirs(MPL_CONFIG_DIR, exist_ok=True)
os.environ.setdefault("MPLCONFIGDIR", MPL_CONFIG_DIR)

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from email_validator_tool_coverage import SEED, require
from regex_positive_generator.benchmarks.python_regex_function_coverage_benchmark import (
    TOOL_SPECS,
    build_tool_error_message,
    build_target_specs as build_all_target_specs,
    compute_branch_coverage,
    get_include_patterns,
    get_tool_failure_details,
    get_tool_samples,
    parse_tools,
    run_validator_plain,
)

try:
    import coverage
except ImportError:  # pragma: no cover - dependency check for runtime
    coverage = None

try:
    from email_validator import validate_email as package_validate_email
    import email_validator as email_validator_module
except ImportError:  # pragma: no cover - optional benchmark target
    package_validate_email = None
    email_validator_module = None

try:
    import dateutil.parser as dateutil_parser_module
except ImportError:  # pragma: no cover - optional benchmark target
    dateutil_parser_module = None


DEFAULT_SAMPLE_SIZES = "10,50,100,500,1000,10000,1000000"
DEFAULT_TARGET = "email_validator_package"
DEFAULT_RESULTS_JSONL = "regex_positive_generator/benchmarks/coverage_outputs/regex_generator_diversity_coverage_results.jsonl"
DEFAULT_SUMMARY_JSON = "regex_positive_generator/benchmarks/coverage_outputs/regex_generator_diversity_coverage_summary.json"
DEFAULT_RESULTS_JSON = "regex_generator_diversity_coverage_results.json"
DEFAULT_COVERAGE_ROOT = "regex_generator_diversity_coverage_reports"
DEFAULT_CHART_PATH = "regex_generator_diversity_coverage_curve.png"
DEFAULT_CHART_DIR = "regex_generator_diversity_coverage_figures"
DEFAULT_TIMEOUT_SECONDS = 60.0


@dataclass
class TargetSpec:
    name: str
    regex: str
    validator_fn: Callable[[str], bool]
    target_module: ModuleType
    note: str


@dataclass
class DiversityCoverageRun:
    target: str
    tool: str
    samples_per_class: int
    positives_requested: int
    negatives_requested: int
    positives_generated: int
    negatives_generated: int
    total_samples: int
    unique_samples: int
    accepted: int
    rejected: int
    code_coverage: float
    branch_coverage: float
    coverage_summary: Dict[str, object]
    seed: int
    timeout_seconds: float
    status: str = "ok"
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    failure_details: Optional[Dict[str, object]] = None


def validate_email_validator_package(sample: str) -> bool:
    require(package_validate_email is not None, "email-validator is not installed. Run: pip install email-validator")
    try:
        package_validate_email(sample, check_deliverability=False)
        return True
    except Exception:
        return False


def validate_datetime_dateutil(sample: str) -> bool:
    require(dateutil_parser_module is not None, "python-dateutil is not installed. Run: pip install python-dateutil")
    try:
        dateutil_parser_module.isoparse(sample)
        return True
    except Exception:
        return False


def build_lightweight_target_specs() -> Dict[str, TargetSpec]:
    email_regex = (
        r"(?i)"
        r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+"
        r"(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
        r"@"
        r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
        r"(?:[a-z-]{2,63}|xn--[a-z0-9]{1,59})"
    )
    datetime_regex = (
        r"(?:19|20)\d{2}-"
        r"(?:0[1-9]|1[0-2])-"
        r"(?:0[1-9]|[12]\d|3[01])"
        r"T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"
    )

    specs: Dict[str, TargetSpec] = {}
    if email_validator_module is not None:
        specs["email_validator_package"] = TargetSpec(
            name="email_validator_package",
            regex=email_regex,
            validator_fn=validate_email_validator_package,
            target_module=email_validator_module,
            note="Practical email regex for generation; validated by email_validator.validate_email(check_deliverability=False).",
        )
    if dateutil_parser_module is not None:
        specs["datetime_dateutil"] = TargetSpec(
            name="datetime_dateutil",
            regex=datetime_regex,
            validator_fn=validate_datetime_dateutil,
            target_module=dateutil_parser_module,
            note="ISO datetime regex for generation; validated by dateutil.parser.isoparse.",
        )
    return specs


def parse_sample_sizes(raw_value: str) -> List[int]:
    sizes = []
    for item in raw_value.split(","):
        item = item.strip().replace("_", "")
        if not item:
            continue
        value = int(item)
        require(value > 0, "sample sizes must be positive")
        sizes.append(value)
    require(sizes, "--sample-sizes must include at least one positive integer")
    return sorted(dict.fromkeys(sizes))


def safe_name(value: str) -> str:
    return value.lower().replace("/", "_").replace(" ", "_")


def write_jsonl_row(handle, row: Dict[str, object]) -> None:
    handle.write(json.dumps(row, ensure_ascii=True, sort_keys=True))
    handle.write("\n")
    handle.flush()


def run_validator_with_coverage(
    samples: List[str],
    validator_fn: Callable[[str], bool],
    target_module: ModuleType,
    html_dir: str,
    json_path: str,
) -> Tuple[int, int, Dict[str, object]]:
    require(coverage is not None, "coverage is not installed. Run: pip install coverage")
    include_patterns = get_include_patterns(target_module)
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    cov = coverage.Coverage(
        branch=True,
        include=include_patterns,
        data_file=os.path.join(os.path.dirname(json_path), ".coverage"),
    )
    cov.start()
    try:
        accepted, rejected = run_validator_plain(samples, validator_fn)
    finally:
        cov.stop()
        cov.save()

    os.makedirs(html_dir, exist_ok=True)
    cov.html_report(directory=html_dir)
    cov.json_report(outfile=json_path)
    with open(json_path, "r", encoding="utf-8") as fh:
        coverage_summary = json.load(fh).get("totals", {})
    return accepted, rejected, coverage_summary


def run_diversity_coverage_case(
    spec: TargetSpec,
    *,
    tool_name: str,
    tool_key: str,
    samples_per_class: int,
    seed: int,
    coverage_root: str,
    timeout_seconds: float,
) -> DiversityCoverageRun:
    positives_requested = samples_per_class
    negatives_requested = samples_per_class
    try:
        cases = get_tool_samples(
            spec.regex,
            tool_key,
            n_pos=positives_requested,
            n_neg=negatives_requested,
            seed=seed,
            timeout_seconds=timeout_seconds,
        )
        samples = [case.sample for case in cases]
        positives_generated = sum(1 for case in cases if case.expected_match)
        negatives_generated = len(cases) - positives_generated
        safe_target = safe_name(spec.name)
        safe_tool = safe_name(tool_name)
        html_dir = os.path.join(coverage_root, safe_target, safe_tool, f"n_{samples_per_class}", "html")
        json_path = os.path.join(coverage_root, safe_target, safe_tool, f"n_{samples_per_class}", "coverage.json")
        accepted, rejected, coverage_summary = run_validator_with_coverage(
            samples=samples,
            validator_fn=spec.validator_fn,
            target_module=spec.target_module,
            html_dir=html_dir,
            json_path=json_path,
        )
        return DiversityCoverageRun(
            target=spec.name,
            tool=tool_name,
            samples_per_class=samples_per_class,
            positives_requested=positives_requested,
            negatives_requested=negatives_requested,
            positives_generated=positives_generated,
            negatives_generated=negatives_generated,
            total_samples=len(samples),
            unique_samples=len(set(samples)),
            accepted=accepted,
            rejected=rejected,
            code_coverage=(coverage_summary.get("percent_covered", 0.0) or 0.0) / 100.0,
            branch_coverage=compute_branch_coverage(coverage_summary),
            coverage_summary=coverage_summary,
            seed=seed,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        failure_details = get_tool_failure_details(
            spec.regex,
            tool_key,
            exc,
            timeout_seconds=timeout_seconds,
        )
        return build_failed_run(
            target=spec.name,
            tool=tool_name,
            samples_per_class=samples_per_class,
            seed=seed,
            timeout_seconds=timeout_seconds,
            status="error",
            error_type=type(exc).__name__,
            error_message=build_tool_error_message(tool_name, exc, failure_details)[:1000],
            failure_details=failure_details,
        )


def build_failed_run(
    *,
    target: str,
    tool: str,
    samples_per_class: int,
    seed: int,
    timeout_seconds: float,
    status: str,
    error_type: str,
    error_message: str,
    failure_details: Optional[Dict[str, object]] = None,
) -> DiversityCoverageRun:
    return DiversityCoverageRun(
        target=target,
        tool=tool,
        samples_per_class=samples_per_class,
        positives_requested=samples_per_class,
        negatives_requested=samples_per_class,
        positives_generated=0,
        negatives_generated=0,
        total_samples=0,
        unique_samples=0,
        accepted=0,
        rejected=0,
        code_coverage=0.0,
        branch_coverage=0.0,
        coverage_summary={},
        seed=seed,
        timeout_seconds=timeout_seconds,
        status=status,
        error_type=error_type,
        error_message=error_message,
        failure_details=failure_details,
    )


def run_case_worker(
    result_queue,
    target_name: str,
    tool_name: str,
    tool_key: str,
    samples_per_class: int,
    seed: int,
    coverage_root: str,
    timeout_seconds: float,
) -> None:
    try:
        target_specs = {spec.name: spec for spec in build_all_target_specs()}
        require(target_name in target_specs, f"Target is unavailable in worker: {target_name}")
        run = run_diversity_coverage_case(
            target_specs[target_name],
            tool_name=tool_name,
            tool_key=tool_key,
            samples_per_class=samples_per_class,
            seed=seed,
            coverage_root=coverage_root,
            timeout_seconds=timeout_seconds,
        )
        result_queue.put(asdict(run))
    except Exception as exc:
        result_queue.put(
            asdict(
                build_failed_run(
                    target=target_name,
                    tool=tool_name,
                    samples_per_class=samples_per_class,
                    seed=seed,
                    timeout_seconds=timeout_seconds,
                    status="error",
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:1000],
                )
            )
        )


def run_case_with_timeout(
    ctx,
    *,
    target_name: str,
    tool_name: str,
    tool_key: str,
    samples_per_class: int,
    seed: int,
    coverage_root: str,
    timeout_seconds: float,
) -> DiversityCoverageRun:
    result_queue = ctx.Queue(maxsize=1)
    process = ctx.Process(
        target=run_case_worker,
        args=(
            result_queue,
            target_name,
            tool_name,
            tool_key,
            samples_per_class,
            seed,
            coverage_root,
            timeout_seconds,
        ),
    )
    process.start()
    process.join(timeout_seconds)
    if process.is_alive():
        process.terminate()
        process.join()
        return build_failed_run(
            target=target_name,
            tool=tool_name,
            samples_per_class=samples_per_class,
            seed=seed,
            timeout_seconds=timeout_seconds,
            status="timeout",
            error_type="TimeoutError",
            error_message=f"Timed out after {timeout_seconds:.1f} seconds.",
            failure_details={
                "tool": tool_name,
                "regex_preserved": True,
                "timeout_seconds": timeout_seconds,
                "timeout_scope": "generation_and_coverage",
            },
        )

    try:
        row = result_queue.get_nowait()
    except queue.Empty:
        return build_failed_run(
            target=target_name,
            tool=tool_name,
            samples_per_class=samples_per_class,
            seed=seed,
            timeout_seconds=timeout_seconds,
            status="error",
            error_type="WorkerError",
            error_message=f"Worker exited with code {process.exitcode} without returning a result.",
            failure_details={
                "tool": tool_name,
                "regex_preserved": True,
                "worker_exit_code": process.exitcode,
            },
        )
    return DiversityCoverageRun(**row)


def render_coverage_curve(runs: List[DiversityCoverageRun], output_path: str) -> None:
    ok_runs = sorted((run for run in runs if run.status == "ok"), key=lambda row: (row.tool, row.samples_per_class))
    if not ok_runs:
        return

    tools = sorted({run.tool for run in ok_runs})
    fig, axes = plt.subplots(2, 1, figsize=(11, 9), sharex=True)
    for tool in tools:
        tool_runs = [run for run in ok_runs if run.tool == tool]
        x_values = [run.samples_per_class for run in tool_runs]
        code_values = [run.code_coverage * 100.0 for run in tool_runs]
        branch_values = [run.branch_coverage * 100.0 for run in tool_runs]
        axes[0].plot(x_values, code_values, marker="o", linewidth=2.0, label=tool)
        axes[1].plot(x_values, branch_values, marker="s", linewidth=2.0, label=tool)

    axes[0].set_title(f"Code Coverage vs Sample Count ({ok_runs[0].target})")
    axes[0].set_ylabel("Coverage %")
    axes[0].set_ylim(0, 100)
    axes[0].grid(True, which="both", alpha=0.25)

    axes[1].set_title(f"Branch Coverage vs Sample Count ({ok_runs[0].target})")
    axes[1].set_xscale("log")
    axes[1].set_xlabel("Samples per class")
    axes[1].set_ylabel("Coverage %")
    axes[1].set_ylim(0, 100)
    axes[1].grid(True, which="both", alpha=0.25)

    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=max(1, len(tools)))
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(output_path, dpi=220)
    plt.close(fig)


def render_individual_coverage_curves(runs: List[DiversityCoverageRun], output_dir: str) -> List[str]:
    os.makedirs(output_dir, exist_ok=True)
    output_paths = []
    for target in sorted({run.target for run in runs}):
        target_runs = [run for run in runs if run.target == target]
        if not any(run.status == "ok" for run in target_runs):
            continue
        output_path = os.path.join(output_dir, f"{safe_name(target)}_coverage_curve.png")
        render_coverage_curve(target_runs, output_path)
        output_paths.append(os.path.abspath(output_path))
    return output_paths


def print_summary(runs: List[DiversityCoverageRun]) -> None:
    print("\nGenerator Diversity Coverage Sweep")
    print("=" * 136)
    print(
        f"{'Target':<30} {'Tool':<18} {'N/Class':>10} {'Generated':>10} {'Unique':>10} {'+':>8} {'-':>8} "
        f"{'Accepted':>9} {'Rejected':>9} {'Code':>8} {'Branch':>8} {'Status':>8}"
    )
    print("-" * 136)
    for run in sorted(runs, key=lambda row: (row.target, row.tool, row.samples_per_class)):
        print(
            f"{run.target:<30} {run.tool:<18} {run.samples_per_class:>10} {run.total_samples:>10} {run.unique_samples:>10} "
            f"{run.positives_generated:>8} {run.negatives_generated:>8} "
            f"{run.accepted:>9} {run.rejected:>9} "
            f"{run.code_coverage * 100:>7.2f}% {run.branch_coverage * 100:>7.2f}% "
            f"{run.status:>8}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sweep generator sample budgets and plot validator coverage vs requested samples."
    )
    parser.add_argument("--target", default=DEFAULT_TARGET, help="Target name, or 'all' for every target in python_regex_function_coverage_benchmark.py.")
    parser.add_argument("--tools", default=",".join(tool_key for _tool_name, tool_key in TOOL_SPECS))
    parser.add_argument("--sample-sizes", default=DEFAULT_SAMPLE_SIZES)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--coverage-root", default=DEFAULT_COVERAGE_ROOT)
    parser.add_argument("--results-jsonl", default=DEFAULT_RESULTS_JSONL)
    parser.add_argument("--summary-json", default=DEFAULT_SUMMARY_JSON)
    parser.add_argument("--results-json", default=DEFAULT_RESULTS_JSON)
    parser.add_argument("--chart-path", default=DEFAULT_CHART_PATH)
    parser.add_argument("--chart-dir", default=DEFAULT_CHART_DIR)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()

    require(coverage is not None, "coverage is not installed. Run: pip install coverage")
    require(args.timeout_seconds > 0.0, "--timeout-seconds must be positive")
    sample_sizes = parse_sample_sizes(args.sample_sizes)
    tool_specs = parse_tools(args.tools)
    if args.target == "all":
        selected_target_specs = build_all_target_specs()
    else:
        all_target_specs = {spec.name: spec for spec in build_all_target_specs()}
        if args.target not in all_target_specs:
            lightweight_specs = build_lightweight_target_specs()
            require(args.target in lightweight_specs, f"Unknown target: {args.target}")
            selected_target_specs = [lightweight_specs[args.target]]
        else:
            selected_target_specs = [all_target_specs[args.target]]

    for output_path in (args.results_jsonl, args.summary_json, args.results_json, args.chart_path):
        output_dir = os.path.dirname(output_path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
    os.makedirs(args.chart_dir, exist_ok=True)
    os.makedirs(args.coverage_root, exist_ok=True)

    runs: List[DiversityCoverageRun] = []
    ctx = multiprocessing.get_context("spawn")
    with open(args.results_jsonl, "w", encoding="utf-8") as jsonl_handle:
        total_jobs = len(selected_target_specs) * len(tool_specs) * len(sample_sizes)
        completed = 0
        for spec in selected_target_specs:
            for tool_name, tool_key in tool_specs:
                for samples_per_class in sample_sizes:
                    completed += 1
                    run = run_case_with_timeout(
                        ctx,
                        target_name=spec.name,
                        tool_name=tool_name,
                        tool_key=tool_key,
                        samples_per_class=samples_per_class,
                        seed=args.seed,
                        coverage_root=args.coverage_root,
                        timeout_seconds=args.timeout_seconds,
                    )
                    runs.append(run)
                    write_jsonl_row(jsonl_handle, asdict(run))
                    print(
                        f"[{completed}/{total_jobs}] target={spec.name} tool={tool_name} "
                        f"n_per_class={samples_per_class} status={run.status} "
                        f"code={run.code_coverage * 100:.2f}% branch={run.branch_coverage * 100:.2f}%",
                        flush=True,
                    )

    chart_paths = render_individual_coverage_curves(runs, args.chart_dir)
    if len(selected_target_specs) == 1:
        render_coverage_curve(runs, args.chart_path)
        chart_paths.append(os.path.abspath(args.chart_path))

    output = {
        "target": args.target,
        "targets": [
            {
                "name": spec.name,
                "regex": spec.regex,
                "note": spec.note,
            }
            for spec in selected_target_specs
        ],
        "tools": tool_specs,
        "sample_sizes_per_class": sample_sizes,
        "seed": args.seed,
        "timeout_seconds": args.timeout_seconds,
        "results": [asdict(run) for run in runs],
        "raw_results_jsonl": os.path.abspath(args.results_jsonl),
        "chart_path": os.path.abspath(args.chart_path),
        "chart_dir": os.path.abspath(args.chart_dir),
        "chart_paths": chart_paths,
        "coverage_root": os.path.abspath(args.coverage_root),
        "notes": {
            "code_coverage": "coverage.py total percent_covered for the selected validator target.",
            "branch_coverage": "coverage.py covered_branches / num_branches for the selected validator target.",
            "sample_sizes_per_class": "Each n requests n positive samples and n negative samples. Tools without negative generation may return only positive samples.",
            "timeout_seconds": "Each (tool, n) job has this wall-clock timeout for generation plus coverage measurement.",
            "external_tools": "The tool registry includes RandExp.js and MutRex. RandExp.js is positive-only; MutRex may return fewer samples because its mutation suite is finite.",
            "mutrex_semantics": "MutRex CONF/REJECT labels are checked with Python re.fullmatch; dialect disagreements are recorded in failure_details.",
        },
    }
    summary = {
        **output,
        "status_counts": dict(Counter(run.status for run in runs)),
    }

    with open(args.results_json, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2)
        fh.write("\n")
    with open(args.summary_json, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
        fh.write("\n")

    print_summary(runs)
    print(f"\nRaw JSONL saved to: {os.path.abspath(args.results_jsonl)}")
    print(f"Saved results to: {os.path.abspath(args.results_json)}")
    print(f"Summary saved to: {os.path.abspath(args.summary_json)}")
    if len(selected_target_specs) == 1:
        print(f"Coverage curve saved to: {os.path.abspath(args.chart_path)}")
    print(f"Individual target figures saved under: {os.path.abspath(args.chart_dir)}")
    print(f"Coverage reports saved under: {os.path.abspath(args.coverage_root)}")


if __name__ == "__main__":
    main()
