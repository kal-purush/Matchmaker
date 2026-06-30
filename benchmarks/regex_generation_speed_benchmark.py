import argparse
import contextlib
import io
import json
import multiprocessing
import os
import queue
import random
import subprocess
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from regex_positive_generator.experimental import CompactRegexGraphBuilder
from regex_positive_generator.experimental.compact_sampler_lookaround_experimental import (
    ExperimentalLookaroundCompactGraphSampler,
)

try:
    import exrex
except ImportError:  # pragma: no cover - optional benchmark dependency
    exrex = None

try:
    from xeger import Xeger
except ImportError:  # pragma: no cover - optional benchmark dependency
    Xeger = None


DEFAULT_PATTERNS_PATHS = ["exponential-regexes.json", "polynomial-regexes.json"]
DEFAULT_RESULTS_JSONL = "regex_generation_speed_benchmark_results.jsonl"
DEFAULT_SUMMARY_JSON = "regex_generation_speed_benchmark_summary.json"
DEFAULT_NUM_PATTERNS = 5000
DEFAULT_SAMPLES_PER_PATTERN = 1000
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_SEED = 0
DEFAULT_MODE = "diverse"
DEFAULT_GENERATOR_VALIDATE = False
DEFAULT_WORKERS = 1
PARTIAL_EXAMPLE_LIMIT = 5
PARTIAL_PROGRESS_EVERY = 100

TOOL_SPECS = [
    ("RegexInstantiator", "ours"),
    ("exrex", "exrex"),
    ("Xeger", "xeger"),
    ("EGRET", "egret"),
]


@dataclass
class PatternRecord:
    pattern_id: int
    pattern: str
    selection_index: int
    source_file: str
    source_type: str
    source_uri: str
    source_line: int
    feature_tags: List[str]
    complexity: str


@dataclass
class SpeedRun:
    pattern_id: int
    selection_index: int
    pattern: str
    source_file: str
    source_line: int
    source_type: str
    source_uri: str
    feature_tags: List[str]
    complexity: str
    tool: str
    tool_key: str
    status: str
    requested_samples: int
    generated_samples: int
    unique_samples: int
    possible_samples: Optional[int]
    exhausted_effective_space: bool
    status_reason: Optional[str]
    elapsed_seconds: float
    timeout_seconds: float
    error_type: Optional[str]
    error_message: Optional[str]
    nodes: Optional[int] = None
    build_seconds: Optional[float] = None
    count_seconds: Optional[float] = None
    generation_seconds: Optional[float] = None
    partial_examples: Optional[List[str]] = None


@dataclass
class GenerationResult:
    samples: List[str]
    possible_samples: Optional[int] = None
    exhausted_effective_space: bool = False
    nodes: Optional[int] = None
    build_seconds: Optional[float] = None
    count_seconds: Optional[float] = None
    generation_seconds: Optional[float] = None


def detect_feature_tags(pattern: str) -> List[str]:
    checks = {
        "lookahead": ("(?=", "(?!"),
        "lookbehind": ("(?<=", "(?<!"),
        "named_group": ("(?P<", "(?<"),
        "conditional": ("(?(",),
        "inline_flags": ("(?i", "(?m", "(?s", "(?x", "(?a", "(?u", "(?L"),
        "lazy_quantifier": ("*?", "+?", "??", "}?",),
        "unicode_property": (r"\p{", r"\P{"),
        "recursion": ("(?R)", "(?0)", "(?&"),
        "atomic_or_possessive": ("(?>", "*+", "++", "?+", "}+"),
    }
    tags = [tag for tag, needles in checks.items() if any(needle in pattern for needle in needles)]
    if any(f"\\{digit}" in pattern for digit in "123456789"):
        tags.append("backreference")
    if "^" in pattern or "$" in pattern or r"\A" in pattern or r"\Z" in pattern:
        tags.append("anchor")
    if r"\b" in pattern or r"\B" in pattern:
        tags.append("word_boundary")
    if any(token in pattern for token in ("*", "+", "{")):
        tags.append("repeat")
    return sorted(set(tags))


def load_jsonl_patterns(path: str, limit: int) -> List[PatternRecord]:
    records: List[PatternRecord] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            if limit and len(records) >= limit:
                break
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            pattern = row.get("pattern")
            if not isinstance(pattern, str) or not pattern:
                continue
            records.append(
                PatternRecord(
                    pattern_id=int(row.get("pattern_id", len(records))),
                    pattern=pattern,
                    selection_index=int(row.get("selection_index", line_number - 1)),
                    source_file=str(row.get("source_file") or os.path.basename(path)),
                    source_type=str(row.get("source_type") or ""),
                    source_uri=str(row.get("source_uri") or ""),
                    source_line=int(row.get("source_line", line_number)),
                    feature_tags=list(row.get("feature_tags") or []),
                    complexity=str(row.get("complexity") or ""),
                )
            )
    return records


def infer_complexity_from_path(path: str) -> str:
    filename = os.path.basename(path).lower()
    if "exponential" in filename:
        return "exponential"
    if "polynomial" in filename:
        return "polynomial"
    return ""


def load_sampled_complexity_patterns(path: str, limit: int, id_offset: int = 0) -> List[PatternRecord]:
    records: List[PatternRecord] = []
    with open(path, "r", encoding="utf-8") as fh:
        rows = json.load(fh)
    if not isinstance(rows, list):
        raise ValueError(f"Expected {path} to contain a JSON list")

    for index, row in enumerate(rows):
        if limit and len(records) >= limit:
            break
        if not isinstance(row, dict):
            continue
        pattern = row.get("regex")
        if not isinstance(pattern, str) or not pattern:
            continue
        complexity = str(row.get("complexity") or infer_complexity_from_path(path))
        feature_tags = detect_feature_tags(pattern)
        if complexity:
            feature_tags = sorted(set(feature_tags + [f"complexity:{complexity}"]))
        records.append(
            PatternRecord(
                pattern_id=id_offset + len(records),
                pattern=pattern,
                selection_index=id_offset + len(records),
                source_file=os.path.basename(path),
                source_type="SampledComplexityRegexSource",
                source_uri="",
                source_line=index + 1,
                feature_tags=feature_tags,
                complexity=complexity,
            )
        )
    return records


def load_patterns(paths: List[str], limit: int) -> List[PatternRecord]:
    records: List[PatternRecord] = []
    seen_patterns = set()
    for path in paths:
        remaining = max(limit - len(records), 0) if limit else 0
        if limit and remaining <= 0:
            break
        if path.endswith(".jsonl"):
            loaded = load_jsonl_patterns(path, remaining)
        else:
            loaded = load_sampled_complexity_patterns(path, remaining, id_offset=len(records))
        for record in loaded:
            if record.pattern in seen_patterns:
                continue
            seen_patterns.add(record.pattern)
            records.append(
                PatternRecord(
                    pattern_id=len(records),
                    pattern=record.pattern,
                    selection_index=len(records),
                    source_file=record.source_file,
                    source_type=record.source_type,
                    source_uri=record.source_uri,
                    source_line=record.source_line,
                    feature_tags=record.feature_tags,
                    complexity=record.complexity,
                )
            )
            if limit and len(records) >= limit:
                break
    return records


def dedupe_count(samples: List[str]) -> int:
    return len(set(samples))


def locate_egret_runner() -> str:
    candidates = [
        os.path.abspath("egret/egret.py"),
        os.path.abspath("egret.worktrees/copilot-worktree-2026-05-13T10-31-47/egret.py"),
    ]
    for candidate in candidates:
        candidate_dir = os.path.dirname(candidate)
        has_extension = (
            any(
                name.startswith("egret_ext") and (name.endswith(".so") or name.endswith(".pyd"))
                for name in os.listdir(candidate_dir)
            )
            if os.path.isdir(candidate_dir)
            else False
        )
        if os.path.exists(candidate) and has_extension:
            return candidate
    raise RuntimeError("EGRET runner with compiled egret_ext module not found in workspace.")


def parse_egret_matches(stdout: str) -> List[str]:
    samples: List[str] = []
    section: Optional[str] = None
    for raw_line in stdout.splitlines():
        stripped = raw_line.strip()
        if stripped == "Matches:":
            section = "matches"
            continue
        if stripped == "Non-matches:":
            section = "non_matches"
            continue
        if not stripped or stripped.startswith("Regex:") or stripped.startswith("Description:"):
            continue
        if section == "matches":
            samples.append("" if stripped == "<empty>" else stripped)
    return samples


def generate_with_tool(
    pattern: str,
    tool_key: str,
    samples_per_pattern: int,
    seed: int,
    mode: str,
    validate: bool,
    timeout_seconds: float,
    on_sample=None,
    on_started=None,
) -> GenerationResult:
    if tool_key == "ours":
        build_started = time.perf_counter()
        builder = CompactRegexGraphBuilder(pattern, validate=validate)
        build_seconds = time.perf_counter() - build_started
        nodes = len(builder.nodes)

        count_started = time.perf_counter()
        possible_samples = builder.count_paths()
        count_seconds = time.perf_counter() - count_started

        if on_started is not None:
            on_started(
                {
                    "nodes": nodes,
                    "possible_samples": possible_samples,
                    "build_seconds": build_seconds,
                    "count_seconds": count_seconds,
                }
            )

        generation_started = time.perf_counter()
        samples = ExperimentalLookaroundCompactGraphSampler(builder, validate=validate).generate_samples(
            n=samples_per_pattern,
            seed=seed,
            on_sample=on_sample,
        )
        generation_seconds = time.perf_counter() - generation_started
        unique_samples = len(set(samples))
        exhausted_effective_space = possible_samples <= samples_per_pattern and unique_samples >= possible_samples
        return GenerationResult(
            samples=samples,
            possible_samples=possible_samples,
            exhausted_effective_space=exhausted_effective_space,
            nodes=nodes,
            build_seconds=build_seconds,
            count_seconds=count_seconds,
            generation_seconds=generation_seconds,
        )

    if tool_key == "exrex":
        if exrex is None:
            raise RuntimeError("exrex is not installed. Run: pip install exrex")
        samples: List[str] = []
        seen = set()
        exhausted_effective_space = True
        for sample in exrex.generate(pattern):
            if sample in seen:
                continue
            seen.add(sample)
            samples.append(sample)
            if len(samples) >= samples_per_pattern:
                exhausted_effective_space = False
                break
        return GenerationResult(
            samples=samples,
            possible_samples=len(seen) if exhausted_effective_space else None,
            exhausted_effective_space=exhausted_effective_space,
        )

    if tool_key == "xeger":
        if Xeger is None:
            raise RuntimeError("xeger is not installed. Run: pip install xeger")
        rng_state = random.getstate()
        random.seed(seed)
        generator = Xeger(limit=64)
        samples = []
        seen = set()
        attempts = 0
        max_attempts = max(samples_per_pattern * 50, 5000)
        try:
            while len(samples) < samples_per_pattern and attempts < max_attempts:
                attempts += 1
                sample = generator.xeger(pattern)
                if sample in seen:
                    continue
                seen.add(sample)
                samples.append(sample)
        finally:
            random.setstate(rng_state)
        return GenerationResult(samples=samples)

    if tool_key == "egret":
        runner = locate_egret_runner()
        runner_dir = os.path.dirname(runner)
        result = subprocess.run(
            [sys.executable, runner, "-r", pattern],
            cwd=runner_dir,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "EGRET failed")
        return GenerationResult(samples=parse_egret_matches(result.stdout)[:samples_per_pattern])

    raise ValueError(f"Unknown tool key: {tool_key}")


def tool_worker(
    pattern: str,
    tool_key: str,
    samples_per_pattern: int,
    seed: int,
    mode: str,
    validate: bool,
    timeout_seconds: float,
    output_queue,
) -> None:
    started = time.perf_counter()
    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()
    try:
        partial_state = {"generated": 0}

        def send_partial(sample):
            partial_state["generated"] += 1
            generated = partial_state["generated"]
            if generated <= PARTIAL_EXAMPLE_LIMIT:
                output_queue.put({"status": "partial_sample", "sample": sample, "generated": generated})
            elif generated % PARTIAL_PROGRESS_EVERY == 0:
                output_queue.put({"status": "partial_progress", "generated": generated})

        def send_started(metadata):
            payload = {"status": "started"}
            payload.update(metadata)
            output_queue.put(payload)

        with contextlib.redirect_stdout(captured_stdout), contextlib.redirect_stderr(captured_stderr):
            generation = generate_with_tool(
                pattern=pattern,
                tool_key=tool_key,
                samples_per_pattern=samples_per_pattern,
                seed=seed,
                mode=mode,
                validate=validate,
                timeout_seconds=timeout_seconds,
                on_sample=send_partial if tool_key == "ours" else None,
                on_started=send_started if tool_key == "ours" else None,
            )
    except Exception as exc:
        tool_output = (captured_stdout.getvalue() + captured_stderr.getvalue()).strip()
        message = str(exc)
        if tool_output:
            message = f"{message}\n\nTool output:\n{tool_output}"
        output_queue.put(
            {
                "status": "failed",
                "generated_samples": 0,
                "unique_samples": 0,
                "possible_samples": None,
                "exhausted_effective_space": False,
                "status_reason": "error",
                "elapsed_seconds": time.perf_counter() - started,
                "error_type": type(exc).__name__,
                "error_message": message,
                "nodes": None,
                "build_seconds": None,
                "count_seconds": None,
                "generation_seconds": None,
            }
        )
        return

    samples = generation.samples
    generated = len(samples)
    unique_samples = dedupe_count(samples)
    if generated >= samples_per_pattern:
        status = "ok"
        status_reason = "requested_samples_generated"
    elif generation.exhausted_effective_space:
        status = "ok"
        status_reason = "effective_sample_space_exhausted"
    else:
        status = "partial"
        status_reason = "requested_samples_not_reached"
    output_queue.put(
        {
            "status": status,
            "generated_samples": generated,
            "unique_samples": unique_samples,
            "possible_samples": generation.possible_samples,
            "exhausted_effective_space": generation.exhausted_effective_space,
            "status_reason": status_reason,
            "elapsed_seconds": time.perf_counter() - started,
            "error_type": None,
            "error_message": None,
            "nodes": generation.nodes,
            "build_seconds": generation.build_seconds,
            "count_seconds": generation.count_seconds,
            "generation_seconds": generation.generation_seconds,
        }
    )


def drain_worker_messages(output_queue, partial_samples: List[str], state: Dict[str, object]):
    final_payload = None
    while True:
        try:
            payload = output_queue.get_nowait()
        except queue.Empty:
            break

        status = payload.get("status")
        if status == "partial_sample":
            partial_samples.append(payload["sample"])
            state["partial_generated_count"] = payload.get("generated", len(partial_samples))
        elif status == "partial_progress":
            state["partial_generated_count"] = payload.get("generated", state.get("partial_generated_count", 0))
        elif status == "started":
            state.update(
                {
                    "nodes": payload.get("nodes"),
                    "possible_samples": payload.get("possible_samples"),
                    "build_seconds": payload.get("build_seconds"),
                    "count_seconds": payload.get("count_seconds"),
                }
            )
        elif status in {"ok", "partial", "failed"}:
            final_payload = payload
        else:
            final_payload = {
                "status": "failed",
                "generated_samples": 0,
                "unique_samples": 0,
                "possible_samples": state.get("possible_samples"),
                "exhausted_effective_space": False,
                "status_reason": "unknown_worker_message",
                "elapsed_seconds": 0.0,
                "error_type": "RuntimeError",
                "error_message": f"Unknown worker message: {status}",
                "nodes": state.get("nodes"),
                "build_seconds": state.get("build_seconds"),
                "count_seconds": state.get("count_seconds"),
                "generation_seconds": None,
            }
    return final_payload


def run_tool_with_timeout(
    record: PatternRecord,
    tool_name: str,
    tool_key: str,
    samples_per_pattern: int,
    seed: int,
    mode: str,
    validate: bool,
    timeout_seconds: float,
) -> SpeedRun:
    context = multiprocessing.get_context()

    output_queue = context.Queue()
    partial_samples: List[str] = []
    state: Dict[str, object] = {}
    final_payload = None
    started = time.perf_counter()
    process = context.Process(
        target=tool_worker,
        args=(record.pattern, tool_key, samples_per_pattern, seed, mode, validate, timeout_seconds, output_queue),
    )
    process.start()
    deadline = time.perf_counter() + timeout_seconds
    while process.is_alive():
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            break
        process.join(min(0.05, remaining))
        payload = drain_worker_messages(output_queue, partial_samples, state)
        if payload is not None:
            final_payload = payload

    payload = drain_worker_messages(output_queue, partial_samples, state)
    if payload is not None:
        final_payload = payload
    elapsed = time.perf_counter() - started

    if process.is_alive():
        process.terminate()
        process.join(1.0)
        if process.is_alive():
            process.kill()
            process.join()
        drain_worker_messages(output_queue, partial_samples, state)
        payload = {
            "status": "timeout",
            "generated_samples": int(state.get("partial_generated_count", len(partial_samples))),
            "unique_samples": len(set(partial_samples)),
            "possible_samples": state.get("possible_samples"),
            "exhausted_effective_space": False,
            "status_reason": "timeout",
            "elapsed_seconds": elapsed,
            "error_type": "TimeoutError",
            "error_message": f"Timed out after {timeout_seconds}s",
            "nodes": state.get("nodes"),
            "build_seconds": state.get("build_seconds"),
            "count_seconds": state.get("count_seconds"),
            "generation_seconds": None,
            "partial_examples": partial_samples[:5],
        }
    else:
        if final_payload is None:
            payload = {
                "status": "failed",
                "generated_samples": 0,
                "unique_samples": 0,
                "possible_samples": state.get("possible_samples"),
                "exhausted_effective_space": False,
                "status_reason": "missing_worker_result",
                "elapsed_seconds": elapsed,
                "error_type": "RuntimeError",
                "error_message": "Worker exited without returning a result",
                "nodes": state.get("nodes"),
                "build_seconds": state.get("build_seconds"),
                "count_seconds": state.get("count_seconds"),
                "generation_seconds": None,
            }
        else:
            payload = final_payload
            payload.setdefault("nodes", state.get("nodes"))
            payload.setdefault("build_seconds", state.get("build_seconds"))
            payload.setdefault("count_seconds", state.get("count_seconds"))
            payload.setdefault("partial_examples", partial_samples[:5] if payload["status"] == "partial" else None)

    return SpeedRun(
        pattern_id=record.pattern_id,
        selection_index=record.selection_index,
        pattern=record.pattern,
        source_file=record.source_file,
        source_line=record.source_line,
        source_type=record.source_type,
        source_uri=record.source_uri,
        feature_tags=record.feature_tags,
        complexity=record.complexity,
        tool=tool_name,
        tool_key=tool_key,
        status=str(payload["status"]),
        requested_samples=samples_per_pattern,
        generated_samples=int(payload["generated_samples"]),
        unique_samples=int(payload["unique_samples"]),
        possible_samples=payload.get("possible_samples"),
        exhausted_effective_space=bool(payload.get("exhausted_effective_space")),
        status_reason=payload.get("status_reason"),
        elapsed_seconds=float(payload["elapsed_seconds"]),
        timeout_seconds=timeout_seconds,
        error_type=payload.get("error_type"),
        error_message=payload.get("error_message"),
        nodes=payload.get("nodes"),
        build_seconds=payload.get("build_seconds"),
        count_seconds=payload.get("count_seconds"),
        generation_seconds=payload.get("generation_seconds"),
        partial_examples=payload.get("partial_examples"),
    )


def parse_tools(raw_tools: str) -> List[tuple]:
    requested = [tool.strip().lower() for tool in raw_tools.split(",") if tool.strip()]
    if not requested:
        raise ValueError("--tools must include at least one tool key")
    specs_by_key = {tool_key: (tool_name, tool_key) for tool_name, tool_key in TOOL_SPECS}
    unknown = [tool for tool in requested if tool not in specs_by_key]
    if unknown:
        raise ValueError(f"Unknown tool key(s): {', '.join(unknown)}")
    return [specs_by_key[tool] for tool in requested]


def run_job(job):
    (
        job_index,
        tool_order,
        record,
        tool_name,
        tool_key,
        samples_per_pattern,
        seed,
        mode,
        validate,
        timeout_seconds,
    ) = job
    run = run_tool_with_timeout(
        record=record,
        tool_name=tool_name,
        tool_key=tool_key,
        samples_per_pattern=samples_per_pattern,
        seed=seed,
        mode=mode,
        validate=validate,
        timeout_seconds=timeout_seconds,
    )
    return job_index, tool_order, run


def build_jobs(
    records: List[PatternRecord],
    tool_specs: List[tuple],
    *,
    samples_per_pattern: int,
    seed: int,
    mode: str,
    validate: bool,
    timeout_seconds: float,
) -> List[tuple]:
    jobs = []
    for job_index, record in enumerate(records):
        for tool_order, (tool_name, tool_key) in enumerate(tool_specs):
            jobs.append(
                (
                    job_index,
                    tool_order,
                    record,
                    tool_name,
                    tool_key,
                    samples_per_pattern,
                    seed + record.pattern_id,
                    mode,
                    validate,
                    timeout_seconds,
                )
            )
    return jobs


def run_jobs(jobs: List[tuple], workers: int, progress_every: int, total_patterns: int) -> List[SpeedRun]:
    completed: List[tuple] = []
    total_jobs = len(jobs)
    if workers <= 1:
        for job in jobs:
            result = run_job(job)
            completed.append(result)
            _print_progress(result[2], len(completed), total_jobs, progress_every, total_patterns)
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(run_job, job) for job in jobs]
            for future in as_completed(futures):
                result = future.result()
                completed.append(result)
                _print_progress(result[2], len(completed), total_jobs, progress_every, total_patterns)

    completed.sort(key=lambda item: (item[0], item[1]))
    return [run for _job_index, _tool_order, run in completed]


def _print_progress(run: SpeedRun, completed_jobs: int, total_jobs: int, progress_every: int, total_patterns: int) -> None:
    if progress_every <= 0:
        return
    if completed_jobs == 1 or completed_jobs % progress_every == 0 or completed_jobs == total_jobs:
        pattern_index = min(total_patterns, run.selection_index + 1)
        print(
            f"[{completed_jobs}/{total_jobs} jobs; pattern~{pattern_index}/{total_patterns}] "
            f"tool={run.tool} status={run.status} generated={run.generated_samples} "
            f"elapsed={run.elapsed_seconds:.3f}s",
            flush=True,
        )


def write_jsonl_row(fh, row: Dict[str, object]) -> None:
    fh.write(json.dumps(row, ensure_ascii=True, sort_keys=True))
    fh.write("\n")
    fh.flush()


def build_summary(
    runs: List[SpeedRun],
    patterns_paths: List[str],
    num_patterns: int,
    samples_per_pattern: int,
    timeout_seconds: float,
    seed: int,
    mode: str,
    validate: bool,
    tool_specs: List[tuple],
    elapsed_seconds: float,
) -> Dict[str, object]:
    by_tool: Dict[str, List[SpeedRun]] = defaultdict(list)
    for run in runs:
        by_tool[run.tool].append(run)

    primary_tool = tool_specs[0][0] if tool_specs else ""
    complexity_counts = Counter(run.complexity or "unknown" for run in runs if run.tool == primary_tool)
    tool_summaries = []
    for tool_name, tool_runs in sorted(by_tool.items()):
        status_counts = Counter(run.status for run in tool_runs)
        completed = [run for run in tool_runs if run.status in {"ok", "partial"}]
        ok_runs = [run for run in tool_runs if run.status == "ok"]
        total_generated = sum(run.generated_samples for run in tool_runs)
        total_unique = sum(run.unique_samples for run in tool_runs)
        total_time = sum(run.elapsed_seconds for run in tool_runs)
        avg_time_all = total_time / len(tool_runs) if tool_runs else 0.0
        avg_time_ok = sum(run.elapsed_seconds for run in ok_runs) / len(ok_runs) if ok_runs else 0.0
        samples_per_second = total_generated / total_time if total_time > 0 else 0.0
        complexity_groups: Dict[str, List[SpeedRun]] = defaultdict(list)
        for run in tool_runs:
            complexity_groups[run.complexity or "unknown"].append(run)
        by_complexity = {}
        for complexity, complexity_runs in sorted(complexity_groups.items()):
            complexity_status_counts = Counter(run.status for run in complexity_runs)
            complexity_total_time = sum(run.elapsed_seconds for run in complexity_runs)
            complexity_total_generated = sum(run.generated_samples for run in complexity_runs)
            by_complexity[complexity] = {
                "runs": len(complexity_runs),
                "status_counts": dict(complexity_status_counts),
                "ok": complexity_status_counts.get("ok", 0),
                "partial": complexity_status_counts.get("partial", 0),
                "failed": complexity_status_counts.get("failed", 0),
                "timeout": complexity_status_counts.get("timeout", 0),
                "total_generated_samples": complexity_total_generated,
                "total_elapsed_seconds": complexity_total_time,
                "samples_per_second": (
                    complexity_total_generated / complexity_total_time if complexity_total_time > 0 else 0.0
                ),
            }
        tool_summaries.append(
            {
                "tool": tool_name,
                "runs": len(tool_runs),
                "status_counts": dict(status_counts),
                "ok": status_counts.get("ok", 0),
                "partial": status_counts.get("partial", 0),
                "failed": status_counts.get("failed", 0),
                "timeout": status_counts.get("timeout", 0),
                "completed_without_failure": len(completed),
                "total_generated_samples": total_generated,
                "total_unique_samples": total_unique,
                "total_elapsed_seconds": total_time,
                "avg_elapsed_seconds_all": avg_time_all,
                "avg_elapsed_seconds_ok": avg_time_ok,
                "samples_per_second": samples_per_second,
                "by_complexity": by_complexity,
            }
        )

    return {
        "benchmark": "regex_generation_speed",
        "patterns_paths": [os.path.abspath(path) for path in patterns_paths],
        "corpus": "exponential-and-polynomial-regexes",
        "requested_patterns": num_patterns,
        "selected_patterns": len({run.pattern_id for run in runs}),
        "complexity_counts": dict(complexity_counts),
        "samples_per_pattern": samples_per_pattern,
        "timeout_seconds_per_tool_pattern": timeout_seconds,
        "seed": seed,
        "mode": mode,
        "validate": validate,
        "tools": [tool_name for tool_name, _tool_key in tool_specs],
        "tool_keys": [tool_key for _tool_name, tool_key in tool_specs],
        "total_runs": len(runs),
        "elapsed_seconds": elapsed_seconds,
        "tool_summaries": tool_summaries,
    }


def print_summary(summary: Dict[str, object]) -> None:
    print("\nRegex Generation Speed Benchmark")
    print("=" * 112)
    print(
        f"{'Tool':<20} {'Runs':>6} {'OK':>6} {'Partial':>8} {'Failed':>8} {'Timeout':>8} "
        f"{'Generated':>12} {'Avg s':>10} {'Samples/s':>12}"
    )
    print("-" * 112)
    for row in summary["tool_summaries"]:
        print(
            f"{row['tool']:<20} {row['runs']:>6} {row['ok']:>6} {row['partial']:>8} "
            f"{row['failed']:>8} {row['timeout']:>8} {row['total_generated_samples']:>12} "
            f"{row['avg_elapsed_seconds_all']:>10.4f} {row['samples_per_second']:>12.2f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark regex sample generation speed across tools.")
    parser.add_argument(
        "--patterns-path",
        nargs="+",
        default=DEFAULT_PATTERNS_PATHS,
        help="One or more JSON/JSONL corpus files. Defaults to exponential-regexes.json and polynomial-regexes.json.",
    )
    parser.add_argument(
        "--selected-patterns-jsonl",
        default=None,
        help="Backward-compatible alias for older JSONL corpora. Overrides --patterns-path when set.",
    )
    parser.add_argument("--num-patterns", type=int, default=DEFAULT_NUM_PATTERNS)
    parser.add_argument("--samples-per-pattern", type=int, default=DEFAULT_SAMPLES_PER_PATTERN)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--mode", default=DEFAULT_MODE, choices=["sample", "heuristic", "diverse", "high_coverage"])
    parser.add_argument("--validate", action="store_true", default=DEFAULT_GENERATOR_VALIDATE)
    parser.add_argument("--tools", default=",".join(tool_key for _tool_name, tool_key in TOOL_SPECS))
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--results-jsonl", default=DEFAULT_RESULTS_JSONL)
    parser.add_argument("--summary-json", default=DEFAULT_SUMMARY_JSON)
    parser.add_argument("--progress-every", type=int, default=50)
    args = parser.parse_args()

    tool_specs = parse_tools(args.tools)
    patterns_paths = [args.selected_patterns_jsonl] if args.selected_patterns_jsonl else args.patterns_path
    records = load_patterns(patterns_paths, args.num_patterns)
    started = time.perf_counter()
    jobs = build_jobs(
        records,
        tool_specs,
        samples_per_pattern=args.samples_per_pattern,
        seed=args.seed,
        mode=args.mode,
        validate=args.validate,
        timeout_seconds=args.timeout_seconds,
    )
    runs = run_jobs(jobs, max(1, args.workers), args.progress_every, len(records))

    results_dir = os.path.dirname(args.results_jsonl)
    if results_dir:
        os.makedirs(results_dir, exist_ok=True)
    summary_dir = os.path.dirname(args.summary_json)
    if summary_dir:
        os.makedirs(summary_dir, exist_ok=True)

    with open(args.results_jsonl, "w", encoding="utf-8") as results_fh:
        for run in runs:
            write_jsonl_row(results_fh, asdict(run))

    elapsed = time.perf_counter() - started
    summary = build_summary(
        runs=runs,
        patterns_paths=patterns_paths,
        num_patterns=args.num_patterns,
        samples_per_pattern=args.samples_per_pattern,
        timeout_seconds=args.timeout_seconds,
        seed=args.seed,
        mode=args.mode,
        validate=args.validate,
        tool_specs=tool_specs,
        elapsed_seconds=elapsed,
    )
    with open(args.summary_json, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=True, sort_keys=True)
        fh.write("\n")

    print_summary(summary)
    print(f"\nResults saved to: {os.path.abspath(args.results_jsonl)}")
    print(f"Summary saved to: {os.path.abspath(args.summary_json)}")


if __name__ == "__main__":
    main()
