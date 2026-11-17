import json
import os
import random
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]

from matchmaker import generate_negative_samples
from matchmaker.compact import CompactGraphSampler, CompactRegexGraphBuilder

try:
    from django.conf import settings as django_settings
    from django.core.exceptions import ValidationError as DjangoValidationError
    from django.core.validators import EmailValidator as DjangoEmailValidator
except ImportError:  # pragma: no cover - dependency check for runtime
    django_settings = None
    DjangoValidationError = Exception
    DjangoEmailValidator = None

try:
    from email_validator import EmailNotValidError, validate_email
except ImportError:  # pragma: no cover - dependency check for runtime
    EmailNotValidError = Exception
    validate_email = None

try:
    import exrex
except ImportError:  # pragma: no cover - optional generator
    exrex = None

try:
    from xeger import Xeger
except ImportError:  # pragma: no cover - optional generator
    Xeger = None


EMAIL_REGEX = (
    r"(?i)"
    r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+"
    r"(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:[a-z-]{2,63}|xn--[a-z0-9]{1,59})"
)

# EMAIL_REGEX = (
#     r"^[A-Za-z0-9](([_\.\-]?[A-Za-z0-9]+)*)@([A-Za-z0-9]+)"
#     r"(([\.\-]?[A-Za-z0-9]+)*)\.([A-Za-z]{2,})$"
# )

N_POSITIVE = 1000
N_NEGATIVE = 1000
SEED = 0
POSITIVE_TIMEOUT_SECONDS = 120
NEGATIVE_TIMEOUT_SECONDS = 120

TOOL_SPECS = [
    ("RegexInstantiator", "ours"),
    ("exrex", "exrex"),
    ("Xeger", "xeger"),
    ("EGRET", "egret"),
]


@dataclass
class SampleCase:
    sample: str
    expected_match: bool
    source: str


@dataclass
class ValidationResult:
    accepted: bool
    error_type: Optional[str]
    error_message: Optional[str]


@dataclass
class DifferentialFinding:
    tool: str
    source: str
    sample: str
    expected_match: bool
    django_accepted: bool
    email_validator_accepted: bool
    django_error_type: Optional[str]
    django_error_message: Optional[str]
    email_validator_error_type: Optional[str]
    email_validator_error_message: Optional[str]


@dataclass
class ToolSummary:
    tool: str
    positives: int
    negatives: int
    total_samples: int
    django_accepted: int
    django_rejected: int
    email_validator_accepted: int
    email_validator_rejected: int
    mismatches: int
    regex_positive_disagreements: int
    regex_negative_disagreements: int


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def configure_django_for_standalone_use() -> None:
    if django_settings is None or django_settings.configured:
        return
    django_settings.configure(
        USE_I18N=False,
        USE_L10N=False,
        USE_TZ=False,
        SECRET_KEY="email-validator-differential-testing",
    )


def safe_exception_message(exc: Exception) -> str:
    try:
        return str(exc)
    except Exception:
        pass

    for attr in ("messages", "message", "args"):
        try:
            value = getattr(exc, attr)
        except Exception:
            continue
        try:
            return repr(value)
        except Exception:
            continue

    return f"<unprintable {type(exc).__name__}>"


def _normalize_samples(samples: List[str], *, expected_match: bool, source: str) -> List[SampleCase]:
    seen = set()
    normalized: List[SampleCase] = []
    for sample in samples:
        if sample in seen:
            continue
        seen.add(sample)
        normalized.append(
            SampleCase(
                sample=sample,
                expected_match=expected_match,
                source=source,
            )
        )
    return normalized


def matches_full(pattern: str, sample: str) -> bool:
    try:
        return re.fullmatch(pattern, sample) is not None
    except re.error:
        return False


def generate_ours_samples(pattern: str, n_pos: int, n_neg: int, seed: int) -> List[SampleCase]:
    builder = CompactRegexGraphBuilder(pattern, validate=True)
    positives = CompactGraphSampler(builder, validate=True).generate_samples(
        n=n_pos,
        seed=seed,
    )

    negatives = generate_negative_samples(
        pattern,
        n=n_neg,
        seed=seed,
        use_fullmatch=True,
        validate=True,
        timeout_seconds=NEGATIVE_TIMEOUT_SECONDS,
    )

    return (
        _normalize_samples(positives, expected_match=True, source="generated_positive")
        + _normalize_samples(negatives, expected_match=False, source="generated_negative")
    )


def generate_exrex_samples(pattern: str, n_pos: int) -> List[SampleCase]:
    require(exrex is not None, "exrex is not installed. Run: pip install exrex")
    import itertools

    samples = list(itertools.islice(exrex.generate(pattern), n_pos * 3))
    positives: List[str] = []
    seen = set()
    for sample in samples:
        if sample in seen:
            continue
        seen.add(sample)
        positives.append(sample)
        if len(positives) >= n_pos:
            break

    require(len(positives) >= n_pos, f"exrex could only generate {len(positives)} / {n_pos} positive samples.")
    return _normalize_samples(positives, expected_match=True, source="generated_positive")


def generate_xeger_samples(pattern: str, n_pos: int, seed: int) -> List[SampleCase]:
    require(Xeger is not None, "xeger is not installed. Run: pip install xeger")
    rng_state = random.getstate()
    random.seed(seed)
    xeger = Xeger(limit=64)
    positives: List[str] = []
    seen = set()
    attempts = 0
    max_attempts = max(n_pos * 50, 5000)
    try:
        while len(positives) < n_pos and attempts < max_attempts:
            attempts += 1
            sample = xeger.xeger(pattern)
            if sample in seen:
                continue
            if not matches_full(pattern, sample):
                continue
            seen.add(sample)
            positives.append(sample)
    finally:
        random.setstate(rng_state)

    require(len(positives) >= n_pos, f"Xeger could only generate {len(positives)} / {n_pos} positive samples.")
    return _normalize_samples(positives, expected_match=True, source="generated_positive")


def locate_egret_runner() -> str:
    candidates = [
        str(REPO_ROOT / "egret" / "egret.py"),
    ]
    for candidate in candidates:
        candidate_dir = os.path.dirname(candidate)
        has_extension = any(
            name.startswith("egret_ext") and (name.endswith(".so") or name.endswith(".pyd"))
            for name in os.listdir(candidate_dir)
        ) if os.path.isdir(candidate_dir) else False
        if os.path.exists(candidate) and has_extension:
            return candidate
    raise RuntimeError(
        "EGRET runner with compiled egret_ext module not found in egret/. "
        "Build it with ./setup_coverage_benchmarks.sh."
    )


def parse_egret_output(stdout: str, pattern: str) -> Tuple[List[str], List[str]]:
    positives: List[str] = []
    negatives: List[str] = []
    section: Optional[str] = None

    for raw_line in stdout.splitlines():
        line = raw_line.rstrip("\n")
        stripped = line.strip()
        if stripped == "Matches:":
            section = "matches"
            continue
        if stripped == "Non-matches:":
            section = "non_matches"
            continue
        if (
            not stripped
            or stripped.startswith("Regex:")
            or stripped.startswith("Description:")
        ):
            continue
        if section == "matches":
            sample = "" if stripped == "<empty>" else stripped
            if matches_full(pattern, sample):
                positives.append(sample)
        elif section == "non_matches":
            sample = "" if stripped == "<empty>" else stripped
            if not matches_full(pattern, sample):
                negatives.append(sample)

    return positives, negatives


def generate_egret_samples(pattern: str, n_pos: int, n_neg: int) -> List[SampleCase]:
    runner = locate_egret_runner()
    runner_dir = os.path.dirname(runner)
    result = subprocess.run(
        [sys.executable, runner, "-r", pattern],
        cwd=runner_dir,
        capture_output=True,
        text=True,
        timeout=120,
    )
    require(result.returncode == 0, f"EGRET failed: {result.stderr.strip() or result.stdout.strip()}")
    positives, negatives = parse_egret_output(result.stdout, pattern)
    normalized_positives = _normalize_samples(positives, expected_match=True, source="generated_positive")
    normalized_negatives = _normalize_samples(negatives, expected_match=False, source="generated_negative")
    # require(len(normalized_positives) >= n_pos, f"EGRET produced {len(normalized_positives)} / {n_pos} unique positive samples.")
    # require(len(normalized_negatives) >= n_neg, f"EGRET produced {len(normalized_negatives)} / {n_neg} unique negative samples.")
    return normalized_positives[:n_pos] + normalized_negatives[:n_neg]


def get_tool_samples(pattern: str, tool_key: str, n_pos: int, n_neg: int, seed: int) -> List[SampleCase]:
    if tool_key == "ours":
        return generate_ours_samples(pattern, n_pos=n_pos, n_neg=n_neg, seed=seed)
    if tool_key == "exrex":
        return generate_exrex_samples(pattern, n_pos=n_pos)
    if tool_key == "xeger":
        return generate_xeger_samples(pattern, n_pos=n_pos, seed=seed)
    if tool_key == "egret":
        return generate_egret_samples(pattern, n_pos=n_pos, n_neg=n_neg)
    raise ValueError(f"Unknown tool key: {tool_key}")


def validate_with_django(sample: str) -> ValidationResult:
    require(DjangoEmailValidator is not None, "django is not installed. Run: pip install django")
    configure_django_for_standalone_use()
    validator = DjangoEmailValidator()
    try:
        validator(sample)
        return ValidationResult(True, None, None)
    except DjangoValidationError as exc:
        return ValidationResult(False, type(exc).__name__, safe_exception_message(exc))
    except Exception as exc:  # pragma: no cover - defensive
        return ValidationResult(False, type(exc).__name__, safe_exception_message(exc))


def validate_with_email_validator_pkg(sample: str) -> ValidationResult:
    require(validate_email is not None, "email-validator is not installed. Run: pip install email-validator")
    try:
        validate_email(sample, check_deliverability=False)
        return ValidationResult(True, None, None)
    except EmailNotValidError as exc:
        return ValidationResult(False, type(exc).__name__, safe_exception_message(exc))
    except Exception as exc:  # pragma: no cover - defensive
        return ValidationResult(False, type(exc).__name__, safe_exception_message(exc))


def analyze_tool(tool_name: str, cases: List[SampleCase]) -> Tuple[ToolSummary, List[DifferentialFinding]]:
    django_accepted = 0
    django_rejected = 0
    email_validator_accepted = 0
    email_validator_rejected = 0
    regex_positive_disagreements = 0
    regex_negative_disagreements = 0
    findings: List[DifferentialFinding] = []

    for case in cases:
        django_result = validate_with_django(case.sample)
        email_result = validate_with_email_validator_pkg(case.sample)

        django_accepted += int(django_result.accepted)
        django_rejected += int(not django_result.accepted)
        email_validator_accepted += int(email_result.accepted)
        email_validator_rejected += int(not email_result.accepted)

        if case.expected_match and (not django_result.accepted or not email_result.accepted):
            regex_positive_disagreements += 1
        if (not case.expected_match) and (django_result.accepted or email_result.accepted):
            regex_negative_disagreements += 1

        if django_result.accepted != email_result.accepted:
            findings.append(
                DifferentialFinding(
                    tool=tool_name,
                    source=case.source,
                    sample=case.sample,
                    expected_match=case.expected_match,
                    django_accepted=django_result.accepted,
                    email_validator_accepted=email_result.accepted,
                    django_error_type=django_result.error_type,
                    django_error_message=django_result.error_message,
                    email_validator_error_type=email_result.error_type,
                    email_validator_error_message=email_result.error_message,
                )
            )

    positives = sum(1 for case in cases if case.expected_match)
    negatives = sum(1 for case in cases if not case.expected_match)
    summary = ToolSummary(
        tool=tool_name,
        positives=positives,
        negatives=negatives,
        total_samples=len(cases),
        django_accepted=django_accepted,
        django_rejected=django_rejected,
        email_validator_accepted=email_validator_accepted,
        email_validator_rejected=email_validator_rejected,
        mismatches=len(findings),
        regex_positive_disagreements=regex_positive_disagreements,
        regex_negative_disagreements=regex_negative_disagreements,
    )
    return summary, findings


def print_summary(summaries: List[ToolSummary]) -> None:
    print("\nEmail Differential Testing Summary")
    print("=" * 120)
    print(
        f"{'Tool':<20} {'+':>6} {'-':>6} {'Django OK':>10} {'Django Bad':>11} "
        f"{'Pkg OK':>8} {'Pkg Bad':>9} {'Mismatch':>10} {'Regex +Diff':>12} {'Regex -Diff':>12}"
    )
    print("-" * 120)
    for summary in summaries:
        print(
            f"{summary.tool:<20} {summary.positives:>6} {summary.negatives:>6} "
            f"{summary.django_accepted:>10} {summary.django_rejected:>11} "
            f"{summary.email_validator_accepted:>8} {summary.email_validator_rejected:>9} "
            f"{summary.mismatches:>10} {summary.regex_positive_disagreements:>12} "
            f"{summary.regex_negative_disagreements:>12}"
        )


def write_findings_jsonl(path: str, findings: List[DifferentialFinding]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for finding in findings:
            fh.write(json.dumps(asdict(finding), ensure_ascii=True) + "\n")


def main() -> None:
    require(DjangoEmailValidator is not None, "django is not installed. Run: pip install django")
    require(validate_email is not None, "email-validator is not installed. Run: pip install email-validator")

    all_summaries: List[ToolSummary] = []
    all_findings: List[DifferentialFinding] = []

    for tool_name, tool_key in TOOL_SPECS:
        cases = get_tool_samples(
            EMAIL_REGEX,
            tool_key,
            n_pos=N_POSITIVE,
            n_neg=N_NEGATIVE,
            seed=SEED,
        )
        summary, findings = analyze_tool(tool_name, cases)
        all_summaries.append(summary)
        all_findings.extend(findings)
        print(
            f"\nGenerated samples for {tool_name}: "
            f"{summary.positives} positives, {summary.negatives} negatives, "
            f"{summary.mismatches} validator mismatches"
        )

    results = {
        "regex": EMAIL_REGEX,
        "n_positive": N_POSITIVE,
        "n_negative": N_NEGATIVE,
        "seed": SEED,
        "tools": [asdict(summary) for summary in all_summaries],
        "total_mismatches": len(all_findings),
        "notes": {
            "mismatch_definition": "A bug candidate is any sample where Django EmailValidator and email_validator disagree.",
            "regex_positive_disagreements": "Regex-generated positive samples that at least one validator rejected.",
            "regex_negative_disagreements": "Regex-generated negative samples that at least one validator accepted.",
        },
    }

    summary_path = os.path.abspath("email_validator_differential_results.json")
    findings_path = os.path.abspath("email_validator_differential_findings.jsonl")

    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    write_findings_jsonl(findings_path, all_findings)

    print_summary(all_summaries)
    print(f"\nTotal validator mismatches: {len(all_findings)}")
    print(f"Summary saved to: {summary_path}")
    print(f"Findings saved to: {findings_path}")


if __name__ == "__main__":
    main()
