import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import datetime
import email.utils
import encodings.idna
import itertools
import json
import os
import random
import re
import shutil
import subprocess
import sys
import urllib.parse
import uuid
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
import _strptime
import coverage
import ipaddress
import numpy as np

from email_validator_tool_coverage import (
    SEED,
    configure_django_for_standalone_use,
    require,
)
from benchmark_generators.java_regex_generators import generate_generex, generate_rgxgen, validate_java_regex
from benchmark_generators.external_regex_generators import (
    ExternalGeneratorError,
    generate_mutrex,
    generate_randexp,
    validate_javascript_regex,
)
from regex_positive_generator import generate_negative_samples
from regex_positive_generator.experimental import CompactRegexGraphBuilder
from regex_positive_generator.experimental.compact_sampler_lookaround_experimental import (
    ExperimentalLookaroundCompactGraphSampler,
)

try:
    import exrex
except ImportError:  # pragma: no cover - optional generator
    exrex = None

try:
    from xeger import Xeger
except ImportError:  # pragma: no cover - optional generator
    Xeger = None

try:
    from django.core.exceptions import ValidationError as DjangoValidationError
    from django.core.validators import (
        DomainNameValidator,
        URLValidator,
        validate_domain_name as django_validate_domain_name,
        validate_email as django_validate_email,
        validate_ipv46_address as django_validate_ipv46_address,
    )
    from django.db.models import UUIDField as DjangoUUIDField
    import django.db.models.fields as django_model_fields_module
    import django.utils.dateparse as django_dateparse_module
    import django.core.validators as django_validators_module
except ImportError:  # pragma: no cover - dependency check for runtime
    DjangoValidationError = Exception
    DomainNameValidator = None
    URLValidator = None
    django_validate_domain_name = None
    django_validate_email = None
    django_validate_ipv46_address = None
    DjangoUUIDField = None
    django_model_fields_module = None
    django_dateparse_module = None
    django_validators_module = None

try:
    from email_validator import validate_email as package_validate_email
    import email_validator as email_validator_module
except ImportError:  # pragma: no cover - dependency check for runtime
    package_validate_email = None
    email_validator_module = None

try:
    from pydantic import HttpUrl, IPvAnyAddress, TypeAdapter
    import pydantic as pydantic_module
except ImportError:  # pragma: no cover - dependency check for runtime
    HttpUrl = None
    IPvAnyAddress = None
    TypeAdapter = None
    pydantic_module = None

try:
    import dateutil.parser as dateutil_parser_module
except ImportError:  # pragma: no cover - dependency check for runtime
    dateutil_parser_module = None


N_POSITIVE = int(os.environ.get("PY_REGEX_COVERAGE_N_POSITIVE", "1000"))
N_NEGATIVE = int(os.environ.get("PY_REGEX_COVERAGE_N_NEGATIVE", "1000"))
GENERATOR_VALIDATE = os.environ.get("PY_REGEX_GENERATOR_VALIDATE", "1").strip().lower() not in {"0", "false", "no"}
POSITIVE_MODE = os.environ.get("PY_REGEX_POSITIVE_MODE", "diverse")
COVERAGE_ROOT = os.path.abspath("python_regex_function_coverage_benchmark_reports")
CHART_PATH = os.path.abspath("python_regex_function_coverage_benchmark_comparison.png")
LATEX_TABLE_PATH = os.path.abspath("python_regex_function_coverage_benchmark_table.tex")
DEFAULT_RESULTS_JSON = os.path.abspath("python_regex_function_coverage_benchmark_results.json")
DEFAULT_RESULTS_JSONL = os.path.abspath("regex_positive_generator/benchmarks/coverage_outputs/python_regex_function_coverage_benchmark_results.jsonl")
DEFAULT_SUMMARY_JSON = os.path.abspath("regex_positive_generator/benchmarks/coverage_outputs/python_regex_function_coverage_benchmark_summary.json")
DEFAULT_WORKERS = 1
DEFAULT_RUNS = 1
DEFAULT_STATS_ITERATIONS = 10000
DEFAULT_EXTERNAL_TOOL_TIMEOUT_SECONDS = 120.0

TOOL_SPECS = [
    ("RegexInstantiator", "ours"),
    ("exrex", "exrex"),
    ("Xeger", "xeger"),
    ("EGRET", "egret"),
    ("Generex", "generex"),
    ("RgxGen", "rgxgen"),
    ("RandExp.js", "randexp"),
    ("MutRex", "mutrex"),
]


@dataclass
class SampleCase:
    sample: str
    expected_match: bool
    source: str


@dataclass
class CoverageRun:
    target: str
    tool: str
    regex: str
    positives: int
    negatives: int
    total_samples: int
    accepted: int
    rejected: int
    code_coverage: float
    branch_coverage: float
    coverage_summary: Dict[str, object]
    run_index: int = 0
    seed: int = SEED
    status: str = "ok"
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    java_regex_valid: Optional[bool] = None
    java_regex_error: Optional[str] = None
    java_regex_error_index: Optional[int] = None
    java_regex_error_pattern: Optional[str] = None
    failure_details: Optional[Dict[str, object]] = None


@dataclass
class TargetSpec:
    name: str
    regex: str
    validator_fn: Callable[[str], bool]
    target_module: ModuleType
    note: str


def get_include_patterns(module: ModuleType) -> List[str]:
    module_file = getattr(module, "__file__", None)
    require(module_file is not None, f"Could not determine coverage target for module: {module!r}")
    module_file = os.path.abspath(module_file)
    package_dir = os.path.dirname(module_file)
    init_name = os.path.basename(module_file)
    if init_name == "__init__.py":
        return [os.path.join(package_dir, "*.py")]
    return [module_file]


def run_validator_plain(samples: List[str], validator_fn: Callable[[str], bool]) -> Tuple[int, int]:
    accepted = 0
    rejected = 0
    for sample in samples:
        if validator_fn(sample):
            accepted += 1
        else:
            rejected += 1
    return accepted, rejected


def run_validator_with_coverage(
    samples: List[str],
    validator_fn: Callable[[str], bool],
    target_module: ModuleType,
    html_dir: str,
    json_path: str,
) -> Tuple[int, int, Dict[str, object]]:
    include_patterns = get_include_patterns(target_module)
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    data_file = os.path.join(os.path.dirname(json_path), ".coverage")
    if os.path.exists(data_file):
        os.remove(data_file)
    if os.path.exists(json_path):
        os.remove(json_path)
    if os.path.isdir(html_dir):
        shutil.rmtree(html_dir)
    cov = coverage.Coverage(
        branch=True,
        include=include_patterns,
        data_file=data_file,
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


def compute_branch_coverage(coverage_summary: Dict[str, object]) -> float:
    branch_total = coverage_summary.get("num_branches", 0) or 0
    covered_branches = coverage_summary.get("covered_branches", 0) or 0
    return (covered_branches / branch_total) if branch_total else 0.0


def _normalize_samples(samples: List[str], *, expected_match: bool, source: str) -> List[SampleCase]:
    seen = set()
    normalized: List[SampleCase] = []
    for sample in samples:
        if sample in seen:
            continue
        seen.add(sample)
        normalized.append(SampleCase(sample=sample, expected_match=expected_match, source=source))
    return normalized


def generate_ours_samples(pattern: str, n_pos: int, n_neg: int, seed: int) -> List[SampleCase]:
    builder = CompactRegexGraphBuilder(pattern, validate=GENERATOR_VALIDATE)
    positives = ExperimentalLookaroundCompactGraphSampler(builder, validate=GENERATOR_VALIDATE).generate_samples(
        n=n_pos,
        seed=seed,
    )
    negatives = generate_negative_samples(
        pattern,
        n=n_neg,
        seed=seed,
        use_fullmatch=True,
        validate=GENERATOR_VALIDATE,
    )
    return (
        _normalize_samples(positives, expected_match=True, source="generated_positive")
        + _normalize_samples(negatives, expected_match=False, source="generated_negative")
    )


def generate_exrex_samples(pattern: str, n_pos: int) -> List[SampleCase]:
    require(exrex is not None, "exrex is not installed. Run: pip install exrex")
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
            seen.add(sample)
            positives.append(sample)
    finally:
        random.setstate(rng_state)
    require(len(positives) >= n_pos, f"Xeger could only generate {len(positives)} / {n_pos} positive samples.")
    return _normalize_samples(positives, expected_match=True, source="generated_positive")


def locate_egret_runner() -> str:
    candidates = [
        os.path.abspath("egret/egret.py"),
        os.path.abspath("egret.worktrees/copilot-worktree-2026-05-13T10-31-47/egret.py"),
    ]
    for candidate in candidates:
        candidate_dir = os.path.dirname(candidate)
        has_extension = (
            any(name.startswith("egret_ext") and (name.endswith(".so") or name.endswith(".pyd")) for name in os.listdir(candidate_dir))
            if os.path.isdir(candidate_dir)
            else False
        )
        if os.path.exists(candidate) and has_extension:
            return candidate
    raise RuntimeError("EGRET runner with compiled egret_ext module not found in workspace.")


def parse_egret_output(stdout: str) -> Tuple[List[str], List[str]]:
    positives: List[str] = []
    negatives: List[str] = []
    section = None
    for raw_line in stdout.splitlines():
        stripped = raw_line.rstrip("\n").strip()
        if stripped == "Matches:":
            section = "matches"
            continue
        if stripped == "Non-matches:":
            section = "non_matches"
            continue
        if not stripped or stripped.startswith("Regex:") or stripped.startswith("Description:"):
            continue
        sample = "" if stripped == "<empty>" else stripped
        if section == "matches":
            positives.append(sample)
        elif section == "non_matches":
            negatives.append(sample)
    return positives, negatives


def generate_egret_samples(
    pattern: str,
    n_pos: int,
    n_neg: int,
    timeout_seconds: float = DEFAULT_EXTERNAL_TOOL_TIMEOUT_SECONDS,
) -> List[SampleCase]:
    runner = locate_egret_runner()
    runner_dir = os.path.dirname(runner)
    result = subprocess.run(
        [sys.executable, runner, "-r", pattern],
        cwd=runner_dir,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    require(result.returncode == 0, f"EGRET failed: {result.stderr.strip() or result.stdout.strip()}")
    positives, negatives = parse_egret_output(result.stdout)
    return (
        _normalize_samples(positives, expected_match=True, source="generated_positive")[:n_pos]
        + _normalize_samples(negatives, expected_match=False, source="generated_negative")[:n_neg]
    )


def generate_generex_samples(
    pattern: str,
    n_pos: int,
    timeout_seconds: float = DEFAULT_EXTERNAL_TOOL_TIMEOUT_SECONDS,
) -> List[SampleCase]:
    positives = generate_generex(pattern, n_pos, unique=True, timeout_seconds=timeout_seconds)
    require(len(positives) >= n_pos, f"Generex could only generate {len(positives)} / {n_pos} positive samples.")
    return _normalize_samples(positives, expected_match=True, source="generated_positive")


def generate_rgxgen_samples(
    pattern: str,
    n_pos: int,
    n_neg: int,
    seed: int,
    timeout_seconds: float = DEFAULT_EXTERNAL_TOOL_TIMEOUT_SECONDS,
) -> List[SampleCase]:
    positives = generate_rgxgen(
        pattern,
        n_pos,
        seed=seed,
        unique=True,
        timeout_seconds=timeout_seconds,
    )
    negatives = generate_rgxgen(
        pattern,
        n_neg,
        seed=seed + 1,
        unique=True,
        negative=True,
        timeout_seconds=timeout_seconds,
    ) if n_neg else []
    require(len(positives) >= n_pos, f"RgxGen could only generate {len(positives)} / {n_pos} positive samples.")
    require(len(negatives) >= n_neg, f"RgxGen could only generate {len(negatives)} / {n_neg} negative samples.")
    return (
        _normalize_samples(positives, expected_match=True, source="generated_positive")
        + _normalize_samples(negatives, expected_match=False, source="generated_negative")
    )


def generate_randexp_samples(
    pattern: str,
    n_pos: int,
    seed: int,
    timeout_seconds: float = DEFAULT_EXTERNAL_TOOL_TIMEOUT_SECONDS,
) -> List[SampleCase]:
    positives = generate_randexp(pattern, n_pos, seed=seed, timeout_seconds=timeout_seconds)
    return _normalize_samples(positives, expected_match=True, source="generated_positive")


def generate_mutrex_samples(
    pattern: str,
    n_pos: int,
    n_neg: int,
    timeout_seconds: float = DEFAULT_EXTERNAL_TOOL_TIMEOUT_SECONDS,
) -> List[SampleCase]:
    generated = generate_mutrex(
        pattern,
        n_positive=n_pos,
        n_negative=n_neg,
        timeout_seconds=timeout_seconds,
    )
    compiled = re.compile(pattern)
    mismatches = []
    for sample in generated:
        actual_match = compiled.fullmatch(sample.value) is not None
        if actual_match != sample.expected_match:
            mismatches.append({
                "sample": sample.value,
                "mutrex_label": "CONF" if sample.expected_match else "REJECT",
                "python_fullmatch": actual_match,
            })
    if mismatches:
        raise ExternalGeneratorError(
            f"MutRex changed regex semantics; {len(mismatches)} generated label(s) disagree with Python re.fullmatch",
            failure_details={
                "tool": "MutRex",
                "regex_engine": "dk.brics.automaton",
                "regex_preserved": True,
                "semantic_validation": "Python re.fullmatch",
                "semantic_mismatch_count": len(mismatches),
                "semantic_mismatches": mismatches[:10],
            },
        )
    return [
        SampleCase(
            sample=sample.value,
            expected_match=sample.expected_match,
            source="generated_positive" if sample.expected_match else "generated_negative",
        )
        for sample in generated
    ]


def get_tool_samples(
    pattern: str,
    tool_key: str,
    n_pos: int,
    n_neg: int,
    seed: int,
    timeout_seconds: float = DEFAULT_EXTERNAL_TOOL_TIMEOUT_SECONDS,
) -> List[SampleCase]:
    if tool_key == "ours":
        return generate_ours_samples(pattern, n_pos=n_pos, n_neg=n_neg, seed=seed)
    if tool_key == "exrex":
        return generate_exrex_samples(pattern, n_pos=n_pos)
    if tool_key == "xeger":
        return generate_xeger_samples(pattern, n_pos=n_pos, seed=seed)
    if tool_key == "egret":
        return generate_egret_samples(pattern, n_pos=n_pos, n_neg=n_neg, timeout_seconds=timeout_seconds)
    if tool_key == "generex":
        return generate_generex_samples(pattern, n_pos=n_pos, timeout_seconds=timeout_seconds)
    if tool_key == "rgxgen":
        return generate_rgxgen_samples(
            pattern,
            n_pos=n_pos,
            n_neg=n_neg,
            seed=seed,
            timeout_seconds=timeout_seconds,
        )
    if tool_key == "randexp":
        return generate_randexp_samples(pattern, n_pos=n_pos, seed=seed, timeout_seconds=timeout_seconds)
    if tool_key == "mutrex":
        return generate_mutrex_samples(pattern, n_pos=n_pos, n_neg=n_neg, timeout_seconds=timeout_seconds)
    raise ValueError(f"Unknown tool key: {tool_key}")


def is_java_tool(tool_key: str) -> bool:
    return tool_key in {"generex", "rgxgen"}


def get_java_regex_details(
    pattern: str,
    tool_key: str,
    timeout_seconds: float = DEFAULT_EXTERNAL_TOOL_TIMEOUT_SECONDS,
) -> Dict[str, object]:
    if not is_java_tool(tool_key):
        return {
            "java_regex_valid": None,
            "java_regex_error": None,
            "java_regex_error_index": None,
            "java_regex_error_pattern": None,
        }
    try:
        return validate_java_regex(pattern, tool=tool_key, timeout_seconds=timeout_seconds)
    except Exception as exc:
        return {
            "java_regex_valid": None,
            "java_regex_error": f"{type(exc).__name__}: {exc}",
            "java_regex_error_index": None,
            "java_regex_error_pattern": pattern,
        }


def get_tool_failure_details(
    pattern: str,
    tool_key: str,
    exc: Exception,
    timeout_seconds: float = DEFAULT_EXTERNAL_TOOL_TIMEOUT_SECONDS,
) -> Dict[str, object]:
    details = dict(getattr(exc, "failure_details", {}) or {})
    details.setdefault("regex", pattern)
    details.setdefault("regex_preserved", True)
    details.setdefault("error_type", type(exc).__name__)
    details.setdefault("error_message", str(exc)[:2000])
    if is_java_tool(tool_key):
        details.update(get_java_regex_details(pattern, tool_key, timeout_seconds=timeout_seconds))
    elif tool_key == "randexp" and "javascript_regex_valid" not in details:
        details.update(validate_javascript_regex(pattern, timeout_seconds=timeout_seconds))
    return details


def build_tool_error_message(tool_name: str, exc: Exception, failure_details: Dict[str, object]) -> str:
    base_error = str(exc)[:1000]
    if isinstance(exc, ExternalGeneratorError):
        return base_error
    java_valid = failure_details.get("java_regex_valid")
    if java_valid is True:
        return f"Java regex valid, {tool_name} failed: {base_error}"
    if java_valid is False:
        return f"Java regex invalid: {failure_details.get('java_regex_error')}; {tool_name} failed: {base_error}"
    java_error = failure_details.get("java_regex_error")
    if java_error:
        return f"Java regex validity unknown ({java_error}); {tool_name} failed: {base_error}"
    javascript_valid = failure_details.get("javascript_regex_valid")
    if javascript_valid is True:
        return f"JavaScript regex valid, {tool_name} failed: {base_error}"
    if javascript_valid is False:
        return f"JavaScript regex invalid: {failure_details.get('javascript_regex_error')}; {tool_name} failed: {base_error}"
    return base_error


def require_module(module: ModuleType | None, install_hint: str) -> ModuleType:
    require(module is not None, install_hint)
    return module


def validate_email_stdlib_parseaddr(sample: str) -> bool:
    try:
        parsed_name, parsed_email = email.utils.parseaddr(sample, strict=True)
    except TypeError:
        parsed_name, parsed_email = email.utils.parseaddr(sample)
    except Exception:
        return False

    if parsed_name:
        return False
    if parsed_email != sample:
        return False
    if parsed_email.count("@") != 1:
        return False
    local_part, domain_part = parsed_email.rsplit("@", 1)
    return bool(local_part and domain_part and "." in domain_part)


def validate_email_django_core(sample: str) -> bool:
    require(django_validate_email is not None, "django is not installed. Run: pip install django")
    configure_django_for_standalone_use()
    try:
        django_validate_email(sample)
        return True
    except DjangoValidationError:
        return False
    except Exception:
        return False


def validate_email_validator_package(sample: str) -> bool:
    require(package_validate_email is not None, "email-validator is not installed. Run: pip install email-validator")
    try:
        package_validate_email(sample, check_deliverability=False)
        return True
    except Exception:
        return False


def validate_ip_stdlib_ipaddress(sample: str) -> bool:
    try:
        ipaddress.ip_address(sample)
        return True
    except ValueError:
        return False
    except Exception:
        return False


def validate_ip_django_core(sample: str) -> bool:
    require(django_validate_ipv46_address is not None, "django is not installed. Run: pip install django")
    configure_django_for_standalone_use()
    try:
        django_validate_ipv46_address(sample)
        return True
    except DjangoValidationError:
        return False
    except Exception:
        return False


def validate_ip_pydantic(sample: str) -> bool:
    require(TypeAdapter is not None and IPvAnyAddress is not None, "pydantic is not installed. Run: pip install pydantic")
    try:
        TypeAdapter(IPvAnyAddress).validate_python(sample)
        return True
    except Exception:
        return False


def validate_url_stdlib_urllib(sample: str) -> bool:
    try:
        split = urllib.parse.urlsplit(sample)
        if split.scheme not in {"http", "https", "ftp", "ftps"}:
            return False
        if not split.netloc or not split.hostname:
            return False
        if any(ch.isspace() for ch in sample):
            return False
        split.port
        return True
    except Exception:
        return False


def validate_url_django_core(sample: str) -> bool:
    require(URLValidator is not None, "django is not installed. Run: pip install django")
    configure_django_for_standalone_use()
    validator = URLValidator()
    try:
        validator(sample)
        return True
    except DjangoValidationError:
        return False
    except Exception:
        return False


def validate_url_pydantic(sample: str) -> bool:
    require(TypeAdapter is not None and HttpUrl is not None, "pydantic is not installed. Run: pip install pydantic")
    try:
        TypeAdapter(HttpUrl).validate_python(sample)
        return True
    except Exception:
        return False


def validate_datetime_stdlib_strptime(sample: str) -> bool:
    try:
        datetime.datetime.strptime(sample, "%Y-%m-%dT%H:%M:%S")
        return True
    except ValueError:
        return False
    except Exception:
        return False


def validate_datetime_django_utils(sample: str) -> bool:
    require(django_dateparse_module is not None, "django is not installed. Run: pip install django")
    try:
        return django_dateparse_module.parse_datetime(sample) is not None
    except Exception:
        return False


def validate_datetime_dateutil(sample: str) -> bool:
    require(dateutil_parser_module is not None, "python-dateutil is not installed. Run: pip install python-dateutil")
    try:
        dateutil_parser_module.isoparse(sample)
        return True
    except Exception:
        return False


def validate_uuid_stdlib(sample: str) -> bool:
    try:
        uuid.UUID(sample)
        return True
    except ValueError:
        return False
    except Exception:
        return False


def validate_uuid_django_model(sample: str) -> bool:
    require(DjangoUUIDField is not None, "django is not installed. Run: pip install django")
    configure_django_for_standalone_use()
    field = DjangoUUIDField()
    try:
        field.to_python(sample)
        return True
    except DjangoValidationError:
        return False
    except Exception:
        return False


def validate_uuid_pydantic(sample: str) -> bool:
    require(TypeAdapter is not None, "pydantic is not installed. Run: pip install pydantic")
    try:
        TypeAdapter(uuid.UUID).validate_python(sample)
        return True
    except Exception:
        return False


def validate_domain_stdlib_idna(sample: str) -> bool:
    try:
        if not sample or len(sample) > 255 or sample.endswith("."):
            return False
        labels = sample.split(".")
        if len(labels) < 2:
            return False
        for label in labels:
            encoded = label.encode("idna")
            decoded = encoded.decode("idna")
            if not encoded or len(encoded) > 63:
                return False
            if encoded.startswith(b"-") or encoded.endswith(b"-"):
                return False
            if not decoded:
                return False
        sample.encode("idna").decode("idna")
        return True
    except Exception:
        return False


def validate_domain_django_core(sample: str) -> bool:
    require(django_validate_domain_name is not None, "django is not installed. Run: pip install django")
    configure_django_for_standalone_use()
    try:
        django_validate_domain_name(sample)
        return True
    except DjangoValidationError:
        return False
    except Exception:
        return False


def validate_domain_pydantic(sample: str) -> bool:
    require(TypeAdapter is not None and HttpUrl is not None, "pydantic is not installed. Run: pip install pydantic")
    try:
        TypeAdapter(HttpUrl).validate_python(f"http://{sample}")
        return True
    except Exception:
        return False


def build_target_specs() -> List[TargetSpec]:
    require(django_validators_module is not None, "django is not installed. Run: pip install django")
    require(django_dateparse_module is not None, "django is not installed. Run: pip install django")
    require(django_model_fields_module is not None, "django is not installed. Run: pip install django")
    require(email_validator_module is not None, "email-validator is not installed. Run: pip install email-validator")
    require(pydantic_module is not None, "pydantic is not installed. Run: pip install pydantic")
    require(dateutil_parser_module is not None, "python-dateutil is not installed. Run: pip install python-dateutil")

    domain_regex = (
        r"(?i)"
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
    )
    url_regex = (
        r"(?i)"
        r"(?:http|https|ftp|ftps)://"
        r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
        r"(?:[a-z-]{2,63}|xn--[a-z0-9]{1,59})"
        r"(?::[0-9]{1,5})?"
        r"(?:/[a-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?"
    )
    ipv4_regex = r"(?:0|25[0-5]|2[0-4][0-9]|1[0-9]?[0-9]?|[1-9][0-9]?)(?:\.(?:0|25[0-5]|2[0-4][0-9]|1[0-9]?[0-9]?|[1-9][0-9]?)){3}"
    ipv6_regex = r"(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
    ip_regex = rf"(?:{ipv4_regex}|{ipv6_regex})"
    datetime_regex = r"(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"
    email_regex = (
        r"(?i)"
        r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+"
        r"(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
        r"@"
        r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
        r"(?:[a-z-]{2,63}|xn--[a-z0-9]{1,59})"
    )
    uuid_regex = (
        r"[0-9a-fA-F]{8}-"
        r"[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{12}"
    )

    email_utils_module = require_module(email.utils, "email.utils could not be imported from the Python standard library")
    urllib_parse_module = require_module(urllib.parse, "urllib.parse could not be imported from the Python standard library")
    uuid_module = require_module(uuid, "uuid could not be imported from the Python standard library")

    return [
        TargetSpec(
            name="email_stdlib_parseaddr",
            regex=email_regex,
            validator_fn=validate_email_stdlib_parseaddr,
            target_module=email_utils_module,
            note="Practical email regex for generation; validated by stdlib email.utils.parseaddr(strict=True).",
        ),
        TargetSpec(
            name="email_django_core",
            regex=email_regex,
            validator_fn=validate_email_django_core,
            target_module=django_validators_module,
            note="Practical email regex for generation; validated by Django validate_email.",
        ),
        TargetSpec(
            name="email_validator_package",
            regex=email_regex,
            validator_fn=validate_email_validator_package,
            target_module=email_validator_module,
            note="Practical email regex for generation; validated by email_validator.validate_email(check_deliverability=False).",
        ),
        TargetSpec(
            name="ip_stdlib_ipaddress",
            regex=ip_regex,
            validator_fn=validate_ip_stdlib_ipaddress,
            target_module=ipaddress,
            note="Practical IPv4-or-IPv6 regex for generation; validated by stdlib ipaddress.ip_address.",
        ),
        TargetSpec(
            name="ip_django_core",
            regex=ip_regex,
            validator_fn=validate_ip_django_core,
            target_module=django_validators_module,
            note="Practical IPv4-or-IPv6 regex for generation; validated by Django validate_ipv46_address.",
        ),
        TargetSpec(
            name="ip_pydantic",
            regex=ip_regex,
            validator_fn=validate_ip_pydantic,
            target_module=pydantic_module,
            note="Practical IPv4-or-IPv6 regex for generation; validated by pydantic IPvAnyAddress.",
        ),
        TargetSpec(
            name="url_stdlib_urllib",
            regex=url_regex,
            validator_fn=validate_url_stdlib_urllib,
            target_module=urllib_parse_module,
            note="Simplified URL regex for generation; validated by urllib.parse.urlsplit with scheme/netloc checks.",
        ),
        TargetSpec(
            name="url_django_core",
            regex=url_regex,
            validator_fn=validate_url_django_core,
            target_module=django_validators_module,
            note="Simplified URL regex for generation; validated by Django URLValidator.",
        ),
        TargetSpec(
            name="url_pydantic",
            regex=url_regex,
            validator_fn=validate_url_pydantic,
            target_module=pydantic_module,
            note="Simplified URL regex for generation; validated by pydantic HttpUrl.",
        ),
        TargetSpec(
            name="datetime_stdlib_strptime",
            regex=datetime_regex,
            validator_fn=validate_datetime_stdlib_strptime,
            target_module=_strptime,
            note="ISO datetime regex for generation; validated by datetime.datetime.strptime('%Y-%m-%dT%H:%M:%S').",
        ),
        TargetSpec(
            name="datetime_django_utils",
            regex=datetime_regex,
            validator_fn=validate_datetime_django_utils,
            target_module=django_dateparse_module,
            note="ISO datetime regex for generation; validated by Django django.utils.dateparse.parse_datetime.",
        ),
        TargetSpec(
            name="datetime_dateutil",
            regex=datetime_regex,
            validator_fn=validate_datetime_dateutil,
            target_module=dateutil_parser_module,
            note="ISO datetime regex for generation; validated by dateutil.parser.isoparse.",
        ),
        TargetSpec(
            name="uuid_stdlib",
            regex=uuid_regex,
            validator_fn=validate_uuid_stdlib,
            target_module=uuid_module,
            note="Canonical UUID regex for generation; validated by stdlib uuid.UUID.",
        ),
        TargetSpec(
            name="uuid_django_model",
            regex=uuid_regex,
            validator_fn=validate_uuid_django_model,
            target_module=django_model_fields_module,
            note="Canonical UUID regex for generation; validated by Django UUIDField.to_python.",
        ),
        TargetSpec(
            name="uuid_pydantic",
            regex=uuid_regex,
            validator_fn=validate_uuid_pydantic,
            target_module=pydantic_module,
            note="Canonical UUID regex for generation; validated by pydantic TypeAdapter(uuid.UUID).",
        ),
        TargetSpec(
            name="domain_stdlib_idna",
            regex=domain_regex,
            validator_fn=validate_domain_stdlib_idna,
            target_module=encodings.idna,
            note="Simplified domain regex for generation; validated by stdlib IDNA encode/decode checks.",
        ),
        TargetSpec(
            name="domain_django_core",
            regex=domain_regex,
            validator_fn=validate_domain_django_core,
            target_module=django_validators_module,
            note="Simplified domain regex for generation; validated by Django validate_domain_name.",
        ),
        TargetSpec(
            name="domain_pydantic",
            regex=domain_regex,
            validator_fn=validate_domain_pydantic,
            target_module=pydantic_module,
            note="Simplified domain regex for generation; validated by pydantic HttpUrl using http://{sample}.",
        ),
    ]


def write_jsonl_row(handle, row: Dict[str, object]) -> None:
    handle.write(json.dumps(row, ensure_ascii=True, sort_keys=True))
    handle.write("\n")
    handle.flush()


def safe_name(value: str) -> str:
    return value.lower().replace("/", "_").replace(" ", "_")


def parse_tools(raw_tools: str) -> List[tuple]:
    requested = [tool.strip().lower() for tool in raw_tools.split(",") if tool.strip()]
    if not requested:
        raise ValueError("--tools must include at least one tool key")
    specs_by_key = {tool_key: (tool_name, tool_key) for tool_name, tool_key in TOOL_SPECS}
    unknown = [tool for tool in requested if tool not in specs_by_key]
    if unknown:
        raise ValueError(f"Unknown tool key(s): {', '.join(unknown)}")
    return [specs_by_key[tool] for tool in requested]


def run_coverage_job(job) -> CoverageRun:
    (
        run_index,
        seed,
        target_name,
        tool_name,
        tool_key,
        n_positive,
        n_negative,
        coverage_root,
        debug_samples,
        external_tool_timeout_seconds,
    ) = job
    specs_by_name = {spec.name: spec for spec in build_target_specs()}
    spec = specs_by_name[target_name]
    try:
        cases = get_tool_samples(
            spec.regex,
            tool_key,
            n_pos=n_positive,
            n_neg=n_negative,
            seed=seed,
            timeout_seconds=external_tool_timeout_seconds,
        )
        print_sample_preview(cases, run_index=run_index, target=spec.name, tool=tool_name, limit=debug_samples)
        samples = [case.sample for case in cases]
        positives = sum(1 for case in cases if case.expected_match)
        negatives = len(cases) - positives
        safe_tool = safe_name(tool_name)
        html_dir = os.path.join(coverage_root, f"run_{run_index:03d}", spec.name, safe_tool, "html")
        json_path = os.path.join(coverage_root, f"run_{run_index:03d}", spec.name, safe_tool, "coverage.json")
        accepted, rejected, coverage_summary = run_validator_with_coverage(
            samples=samples,
            validator_fn=spec.validator_fn,
            target_module=spec.target_module,
            html_dir=html_dir,
            json_path=json_path,
        )
        return CoverageRun(
            target=spec.name,
            tool=tool_name,
            regex=spec.regex,
            positives=positives,
            negatives=negatives,
            total_samples=len(cases),
            accepted=accepted,
            rejected=rejected,
            code_coverage=(coverage_summary.get("percent_covered", 0.0) or 0.0) / 100.0,
            branch_coverage=compute_branch_coverage(coverage_summary),
            coverage_summary=coverage_summary,
            run_index=run_index,
            seed=seed,
        )
    except Exception as exc:
        spec = specs_by_name.get(target_name)
        failure_details = get_tool_failure_details(
            spec.regex if spec is not None else "",
            tool_key,
            exc,
            timeout_seconds=external_tool_timeout_seconds,
        )
        error_message = build_tool_error_message(tool_name, exc, failure_details)
        print_failure_preview(
            run_index=run_index,
            target=target_name,
            tool=tool_name,
            error_type=type(exc).__name__,
            error_message=error_message,
            failure_details=failure_details,
            limit=debug_samples,
        )
        return CoverageRun(
            target=target_name,
            tool=tool_name,
            regex=spec.regex if spec is not None else "",
            positives=0,
            negatives=0,
            total_samples=0,
            accepted=0,
            rejected=0,
            code_coverage=0.0,
            branch_coverage=0.0,
            coverage_summary={},
            run_index=run_index,
            seed=seed,
            status="error",
            error_type=type(exc).__name__,
            error_message=error_message[:1000],
            java_regex_valid=failure_details.get("java_regex_valid"),
            java_regex_error=failure_details.get("java_regex_error"),
            java_regex_error_index=failure_details.get("java_regex_error_index"),
            java_regex_error_pattern=failure_details.get("java_regex_error_pattern"),
            failure_details=failure_details,
        )


def build_jobs(
    target_specs: List[TargetSpec],
    tool_specs: List[tuple],
    *,
    runs: int,
    seed: int,
    n_positive: int,
    n_negative: int,
    coverage_root: str,
    debug_samples: int,
    external_tool_timeout_seconds: float,
) -> List[tuple]:
    jobs = []
    for run_index in range(runs):
        run_seed = seed + run_index
        for spec in target_specs:
            for tool_name, tool_key in tool_specs:
                jobs.append(
                    (
                        run_index,
                        run_seed,
                        spec.name,
                        tool_name,
                        tool_key,
                        n_positive,
                        n_negative,
                        coverage_root,
                        debug_samples,
                        external_tool_timeout_seconds,
                    )
                )
    return jobs


def print_sample_preview(cases: List[SampleCase], *, run_index: int, target: str, tool: str, limit: int) -> None:
    if limit <= 0:
        return
    preview = []
    for case in cases[:limit]:
        marker = "+" if case.expected_match else "-"
        preview.append(f"{marker}:{case.sample!r}")
    print(
        f"[samples] run={run_index} target={target} tool={tool} "
        f"count={len(cases)} preview={preview}",
        flush=True,
    )


def print_failure_preview(
    *,
    run_index: int,
    target: str,
    tool: str,
    error_type: str,
    error_message: str,
    failure_details: Dict[str, object],
    limit: int,
) -> None:
    if limit <= 0:
        return
    print(
        f"[failure] run={run_index} target={target} tool={tool} "
        f"error_type={error_type} regex_engine={failure_details.get('regex_engine')!r} "
        f"java_regex_valid={failure_details.get('java_regex_valid')} "
        f"javascript_regex_valid={failure_details.get('javascript_regex_valid')} "
        f"message={error_message[:500]!r}",
        flush=True,
    )


def run_jobs_streaming(
    jobs: List[tuple],
    *,
    workers: int,
    results_jsonl: str,
    progress_every: int,
) -> List[CoverageRun]:
    results_dir = os.path.dirname(results_jsonl)
    if results_dir:
        os.makedirs(results_dir, exist_ok=True)
    runs: List[CoverageRun] = []
    total_jobs = len(jobs)
    completed = 0
    with open(results_jsonl, "w", encoding="utf-8") as jsonl_handle:
        def run_sequential():
            nonlocal completed
            for job in jobs:
                run = run_coverage_job(job)
                runs.append(run)
                completed += 1
                write_jsonl_row(jsonl_handle, asdict(run))
                print_job_progress(run, completed, total_jobs, progress_every)

        if workers <= 1:
            run_sequential()
        else:
            try:
                with ProcessPoolExecutor(max_workers=workers) as executor:
                    futures = [executor.submit(run_coverage_job, job) for job in jobs]
                    for future in as_completed(futures):
                        run = future.result()
                        runs.append(run)
                        completed += 1
                        write_jsonl_row(jsonl_handle, asdict(run))
                        print_job_progress(run, completed, total_jobs, progress_every)
            except PermissionError as exc:
                print(f"Process pool unavailable ({exc}); falling back to sequential execution.", flush=True)
                run_sequential()
    runs.sort(key=lambda row: (row.run_index, row.target, row.tool))
    return runs


def print_job_progress(run: CoverageRun, completed: int, total: int, progress_every: int) -> None:
    if progress_every <= 0:
        return
    if completed == 1 or completed % progress_every == 0 or completed == total:
        print(
            f"[{completed}/{total}] run={run.run_index} target={run.target} tool={run.tool} "
            f"status={run.status} code={run.code_coverage * 100:.2f}% "
            f"branch={run.branch_coverage * 100:.2f}%",
            flush=True,
        )


def aggregate_runs(runs: List[CoverageRun], target_specs: List[TargetSpec], tool_specs: List[tuple]) -> List[CoverageRun]:
    ok_runs = [run for run in runs if run.status == "ok"]
    grouped: Dict[tuple, List[CoverageRun]] = {}
    for run in ok_runs:
        grouped.setdefault((run.target, run.tool), []).append(run)
    specs_by_name = {spec.name: spec for spec in target_specs}
    aggregated: List[CoverageRun] = []
    for spec in target_specs:
        for tool_name, _tool_key in tool_specs:
            group = grouped.get((spec.name, tool_name), [])
            if not group:
                continue
            count = len(group)
            coverage_summary = {
                "runs": count,
                "mean_percent_covered": sum(run.code_coverage for run in group) * 100.0 / count,
                "mean_branch_coverage": sum(run.branch_coverage for run in group) / count,
            }
            aggregated.append(
                CoverageRun(
                    target=spec.name,
                    tool=tool_name,
                    regex=specs_by_name[spec.name].regex,
                    positives=round(sum(run.positives for run in group) / count),
                    negatives=round(sum(run.negatives for run in group) / count),
                    total_samples=round(sum(run.total_samples for run in group) / count),
                    accepted=round(sum(run.accepted for run in group) / count),
                    rejected=round(sum(run.rejected for run in group) / count),
                    code_coverage=sum(run.code_coverage for run in group) / count,
                    branch_coverage=sum(run.branch_coverage for run in group) / count,
                    coverage_summary=coverage_summary,
                    run_index=-1,
                    seed=0,
                    status="aggregate",
                )
            )
    aggregated.sort(key=lambda row: (row.target, -row.code_coverage, -row.branch_coverage, row.tool))
    return aggregated


def sign_flip_permutation_pvalue(deltas: np.ndarray, rng: np.random.Generator, iterations: int) -> float:
    if deltas.size == 0:
        return 1.0
    observed = abs(float(np.mean(deltas)))
    if observed == 0.0:
        return 1.0
    signs = rng.choice(np.array([-1.0, 1.0]), size=(iterations, deltas.size))
    simulated = np.abs(np.mean(signs * deltas, axis=1))
    return float((np.count_nonzero(simulated >= observed) + 1) / (iterations + 1))


def bootstrap_mean_ci(deltas: np.ndarray, rng: np.random.Generator, iterations: int) -> Tuple[float, float]:
    if deltas.size == 0:
        return 0.0, 0.0
    samples = rng.choice(deltas, size=(iterations, deltas.size), replace=True)
    means = np.mean(samples, axis=1)
    low, high = np.percentile(means, [2.5, 97.5])
    return float(low), float(high)


def holm_bonferroni_adjust(results: List[Dict[str, object]]) -> None:
    indexed = sorted(enumerate(results), key=lambda item: float(item[1]["p_value"]))
    m = len(indexed)
    running_max = 0.0
    for rank, (original_index, result) in enumerate(indexed):
        adjusted = min(1.0, float(result["p_value"]) * (m - rank))
        running_max = max(running_max, adjusted)
        results[original_index]["p_value_holm"] = running_max


def compute_statistical_tests(
    runs: List[CoverageRun],
    tool_specs: List[tuple],
    *,
    baseline_tool: str = "RegexInstantiator",
    iterations: int = DEFAULT_STATS_ITERATIONS,
    seed: int = SEED,
) -> List[Dict[str, object]]:
    ok_runs = [run for run in runs if run.status == "ok"]
    lookup = {(run.run_index, run.target, run.tool): run for run in ok_runs}
    pairing_keys = sorted({(run.run_index, run.target) for run in ok_runs})
    comparison_tools = [tool_name for tool_name, _tool_key in tool_specs if tool_name != baseline_tool]
    rng = np.random.default_rng(seed)
    results: List[Dict[str, object]] = []
    for tool in comparison_tools:
        for metric in ("code_coverage", "branch_coverage"):
            deltas = []
            for run_index, target in pairing_keys:
                baseline_run = lookup.get((run_index, target, baseline_tool))
                other_run = lookup.get((run_index, target, tool))
                if baseline_run is None or other_run is None:
                    continue
                deltas.append((getattr(baseline_run, metric) - getattr(other_run, metric)) * 100.0)
            delta_array = np.asarray(deltas, dtype=float)
            wins = int(np.count_nonzero(delta_array > 0.0))
            ties = int(np.count_nonzero(delta_array == 0.0))
            losses = int(np.count_nonzero(delta_array < 0.0))
            ci_low, ci_high = bootstrap_mean_ci(delta_array, rng, iterations)
            results.append(
                {
                    "baseline": baseline_tool,
                    "comparison": tool,
                    "metric": metric,
                    "paired_observations": int(delta_array.size),
                    "mean_delta_points": float(np.mean(delta_array)) if delta_array.size else 0.0,
                    "median_delta_points": float(np.median(delta_array)) if delta_array.size else 0.0,
                    "ci95_mean_delta_points": [ci_low, ci_high],
                    "wins": wins,
                    "ties": ties,
                    "losses": losses,
                    "p_value": sign_flip_permutation_pvalue(delta_array, rng, iterations),
                    "p_value_holm": None,
                    "test": "two_sided_sign_flip_permutation",
                }
            )
    holm_bonferroni_adjust(results)
    return results


def print_statistical_tests(statistical_tests: List[Dict[str, object]]) -> None:
    if not statistical_tests:
        return
    print("\nStatistical Tests")
    print("=" * 132)
    print(
        f"{'Comparison':<22} {'Metric':<16} {'N':>5} {'Mean Δ':>10} {'Median Δ':>10} "
        f"{'95% CI':>24} {'W/T/L':>12} {'p':>10} {'p Holm':>10}"
    )
    print("-" * 132)
    for row in statistical_tests:
        low, high = row["ci95_mean_delta_points"]
        print(
            f"{row['baseline']} - {row['comparison']:<4} "
            f"{row['metric']:<16} {row['paired_observations']:>5} "
            f"{row['mean_delta_points']:>+10.3f} {row['median_delta_points']:>+10.3f} "
            f"[{low:+.3f}, {high:+.3f}]".rjust(24)
            + f" {row['wins']}/{row['ties']}/{row['losses']:>4} "
            f"{row['p_value']:>10.4f} {row['p_value_holm']:>10.4f}"
        )


def print_summary(runs: List[CoverageRun]) -> None:
    print("\nPython Regex Function Coverage Summary")
    print("=" * 132)
    print(
        f"{'Target':<30} {'Tool':<18} {'+':>6} {'-':>6} {'Accepted':>9} "
        f"{'Rejected':>9} {'Code':>8} {'Branch':>8}"
    )
    print("-" * 132)
    for run in runs:
        print(
            f"{run.target:<30} {run.tool:<18} {run.positives:>6} {run.negatives:>6} "
            f"{run.accepted:>9} {run.rejected:>9} {run.code_coverage * 100:>7.2f}% "
            f"{run.branch_coverage * 100:>7.2f}%"
        )


def print_comparison_summary(runs: List[CoverageRun], target_specs: List[TargetSpec]) -> None:
    print("\nComparison View")
    print("=" * 132)
    grouped: Dict[str, List[CoverageRun]] = {}
    for run in runs:
        grouped.setdefault(run.target, []).append(run)

    for spec in target_specs:
        target_runs = sorted(grouped.get(spec.name, []), key=lambda row: (-row.code_coverage, -row.branch_coverage, row.tool))
        if not target_runs:
            continue
        best = target_runs[0]
        worst = target_runs[-1]
        code_gap = (best.code_coverage - worst.code_coverage) * 100.0
        branch_gap = (best.branch_coverage - worst.branch_coverage) * 100.0
        print(f"{spec.name}:")
        print(
            f"  best={best.tool} code={best.code_coverage*100:.2f}% branch={best.branch_coverage*100:.2f}% | "
            f"worst={worst.tool} code={worst.code_coverage*100:.2f}% branch={worst.branch_coverage*100:.2f}%"
        )
        print(f"  gap: code={code_gap:.2f} pts, branch={branch_gap:.2f} pts")

    tool_totals: Dict[str, Dict[str, float]] = {}
    for run in runs:
        stats = tool_totals.setdefault(run.tool, {"code": 0.0, "branch": 0.0, "count": 0.0})
        stats["code"] += run.code_coverage
        stats["branch"] += run.branch_coverage
        stats["count"] += 1.0

    print("\nAverage Across Targets:")
    for tool, stats in sorted(
        tool_totals.items(),
        key=lambda item: (-(item[1]["code"] / item[1]["count"]), -(item[1]["branch"] / item[1]["count"]), item[0]),
    ):
        avg_code = (stats["code"] / stats["count"]) * 100.0
        avg_branch = (stats["branch"] / stats["count"]) * 100.0
        print(f"  {tool:<18} code={avg_code:.2f}% branch={avg_branch:.2f}%")


def print_coverage_matrix(runs: List[CoverageRun], target_specs: List[TargetSpec], tool_specs: List[tuple]) -> None:
    tools = [tool_name for tool_name, _ in tool_specs]
    target_width = max([len("Target")] + [len(spec.name) for spec in target_specs])
    cell_width = max(18, max(len(tool) for tool in tools), len("code / branch"))
    lookup = {(run.target, run.tool): run for run in runs}

    print("\nCoverage Matrix")
    print("=" * (target_width + 3 + (cell_width + 3) * len(tools)))
    print("Each cell is code% / branch%.")
    print(f"{'Target':<{target_width}} • " + " | ".join(f"{tool:>{cell_width}}" for tool in tools))
    print("-" * (target_width + 3 + (cell_width + 3) * len(tools)))

    for spec in target_specs:
        cells = []
        for tool in tools:
            run = lookup.get((spec.name, tool))
            if run is None:
                cells.append(f"{'n/a':>{cell_width}}")
                continue
            cell = f"{run.code_coverage * 100:.2f}% / {run.branch_coverage * 100:.2f}%"
            cells.append(f"{cell:>{cell_width}}")
        print(f"{spec.name:<{target_width}} • " + " | ".join(cells))


def print_regexinstantiator_advantage(runs: List[CoverageRun], target_specs: List[TargetSpec], tool_specs: List[tuple]) -> None:
    baseline = "RegexInstantiator"
    comparison_tools = [tool_name for tool_name, _ in tool_specs if tool_name != baseline]
    if not comparison_tools:
        return
    target_width = max([len("Target")] + [len(spec.name) for spec in target_specs])
    cell_width = max(20, len("code pts / branch pts"))
    lookup = {(run.target, run.tool): run for run in runs}
    totals = {tool: {"code": 0.0, "branch": 0.0, "wins": 0.0, "count": 0.0} for tool in comparison_tools}

    print("\nRegexInstantiator Advantage")
    print("=" * (target_width + 3 + (cell_width + 3) * len(comparison_tools)))
    print("Each cell is RegexInstantiator minus the other tool: code points / branch points.")
    print(f"{'Target':<{target_width}} • " + " | ".join(f"vs {tool:>{cell_width - 3}}" for tool in comparison_tools))
    print("-" * (target_width + 3 + (cell_width + 3) * len(comparison_tools)))

    for spec in target_specs:
        baseline_run = lookup.get((spec.name, baseline))
        cells = []
        for tool in comparison_tools:
            other_run = lookup.get((spec.name, tool))
            if baseline_run is None or other_run is None:
                cells.append(f"{'n/a':>{cell_width}}")
                continue

            code_delta = (baseline_run.code_coverage - other_run.code_coverage) * 100.0
            branch_delta = (baseline_run.branch_coverage - other_run.branch_coverage) * 100.0
            totals[tool]["code"] += code_delta
            totals[tool]["branch"] += branch_delta
            totals[tool]["wins"] += float(code_delta > 0.0 and branch_delta > 0.0)
            totals[tool]["count"] += 1.0
            cell = f"{code_delta:+.2f} / {branch_delta:+.2f}"
            cells.append(f"{cell:>{cell_width}}")
        print(f"{spec.name:<{target_width}} • " + " | ".join(cells))

    print("\nAverage Advantage:")
    for tool in comparison_tools:
        count = totals[tool]["count"] or 1.0
        avg_code = totals[tool]["code"] / count
        avg_branch = totals[tool]["branch"] / count
        wins = int(totals[tool]["wins"])
        compared = int(totals[tool]["count"])
        print(
            f"  vs {tool:<18} code={avg_code:+.2f} pts "
            f"branch={avg_branch:+.2f} pts wins={wins}/{compared}"
        )


def latex_escape(text: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in text)


def format_latex_percent(value: float, best_value: float) -> str:
    formatted = f"{value * 100.0:.2f}"
    if abs(value - best_value) <= 1e-12:
        return rf"\textbf{{{formatted}}}"
    return formatted


def _latex_tool_label(tool_name: str) -> str:
    if tool_name == "RegexInstantiator":
        return "RegexInst."
    return tool_name


def build_latex_coverage_table(runs: List[CoverageRun], target_specs: List[TargetSpec], tool_specs: List[tuple]) -> str:
    tools = [tool_name for tool_name, _ in tool_specs]
    lookup = {(run.target, run.tool): run for run in runs}
    header_tools = " & ".join(rf"\textbf{{{latex_escape(_latex_tool_label(tool))}}}" for tool in tools)
    alignment = "l" + ("r" * (len(tools) * 2))
    last_code_col = 1 + len(tools)
    first_branch_col = last_code_col + 1
    last_branch_col = len(tools) * 2 + 1
    lines = [
        r"% Requires \usepackage{booktabs}. If the table is too wide, wrap it with adjustbox.",
        r"% Optional: \usepackage{adjustbox}",
        r"\begin{table*}[t]",
        r"\centering",
        r"\caption{Code and branch coverage achieved by each regex sample generator. The best result per target and metric is bolded.}",
        r"\label{tab:regex-function-coverage}",
        r"\small",
        rf"\begin{{tabular}}{{{alignment}}}",
        r"\toprule",
        r"\textbf{Target} &",
        rf"\multicolumn{{{len(tools)}}}{{c}}{{\textbf{{Code Coverage}}}} &",
        rf"\multicolumn{{{len(tools)}}}{{c}}{{\textbf{{Branch Coverage}}}} \\",
        rf"\cmidrule(lr){{2-{last_code_col}}}\cmidrule(lr){{{first_branch_col}-{last_branch_col}}}",
        rf"& {header_tools} & {header_tools} \\",
        r"\midrule",
    ]

    for spec in target_specs:
        target_runs = [lookup.get((spec.name, tool)) for tool in tools]
        present_runs = [run for run in target_runs if run is not None]
        best_code = max((run.code_coverage for run in present_runs), default=0.0)
        best_branch = max((run.branch_coverage for run in present_runs), default=0.0)
        code_cells = [
            format_latex_percent(run.code_coverage, best_code) if run is not None else r"\textemdash{}"
            for run in target_runs
        ]
        branch_cells = [
            format_latex_percent(run.branch_coverage, best_branch) if run is not None else r"\textemdash{}"
            for run in target_runs
        ]
        row = " & ".join([latex_escape(spec.name)] + code_cells + branch_cells) + r" \\"
        lines.append(row)

    lines.extend(
        [
            r"\bottomrule",
            r"\end{tabular}",
            r"\end{table*}",
            "",
        ]
    )
    return "\n".join(lines)


def write_latex_coverage_table(
    runs: List[CoverageRun],
    target_specs: List[TargetSpec],
    tool_specs: List[tuple],
    output_path: str,
) -> None:
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(build_latex_coverage_table(runs, target_specs, tool_specs))


def render_comparison_chart(runs: List[CoverageRun], tool_specs: List[tuple], output_path: str) -> None:
    targets = sorted({run.target for run in runs})
    tools = [tool_name for tool_name, _ in tool_specs]
    code_lookup = {(run.target, run.tool): run.code_coverage * 100.0 for run in runs}
    branch_lookup = {(run.target, run.tool): run.branch_coverage * 100.0 for run in runs}

    fig, axes = plt.subplots(2, 1, figsize=(16, 12), sharex=True)
    width = min(0.8 / max(len(tools), 1), 0.18)
    x_positions = list(range(len(targets)))

    for idx, tool in enumerate(tools):
        offsets = [x + (idx - (len(tools) - 1) / 2.0) * width for x in x_positions]
        code_values = [code_lookup.get((target, tool), 0.0) for target in targets]
        branch_values = [branch_lookup.get((target, tool), 0.0) for target in targets]
        axes[0].bar(offsets, code_values, width=width, label=tool)
        axes[1].bar(offsets, branch_values, width=width, label=tool)

    axes[0].set_title("Code Coverage by Target and Generator")
    axes[0].set_ylabel("Coverage %")
    axes[0].set_ylim(0, 100)
    axes[0].grid(axis="y", alpha=0.25)

    axes[1].set_title("Branch Coverage by Target and Generator")
    axes[1].set_ylabel("Coverage %")
    axes[1].set_ylim(0, 100)
    axes[1].grid(axis="y", alpha=0.25)
    axes[1].set_xticks(x_positions)
    axes[1].set_xticklabels(targets, rotation=20, ha="right")

    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=len(tools))
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(output_path, dpi=220)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure validator coverage from regex-generated samples.")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--tools", default=",".join(tool_key for _tool_name, tool_key in TOOL_SPECS))
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--n-positive", type=int, default=N_POSITIVE)
    parser.add_argument("--n-negative", type=int, default=N_NEGATIVE)
    parser.add_argument("--results-jsonl", default=DEFAULT_RESULTS_JSONL)
    parser.add_argument("--summary-json", default=DEFAULT_SUMMARY_JSON)
    parser.add_argument("--results-json", default=DEFAULT_RESULTS_JSON)
    parser.add_argument("--coverage-root", default=COVERAGE_ROOT)
    parser.add_argument("--chart-path", default=CHART_PATH)
    parser.add_argument("--latex-table-path", default=LATEX_TABLE_PATH)
    parser.add_argument("--progress-every", type=int, default=1)
    parser.add_argument("--stats-iterations", type=int, default=DEFAULT_STATS_ITERATIONS)
    parser.add_argument(
        "--external-tool-timeout-seconds",
        type=float,
        default=DEFAULT_EXTERNAL_TOOL_TIMEOUT_SECONDS,
        help="Timeout for each subprocess-backed generator invocation.",
    )
    parser.add_argument(
        "--debug-samples",
        type=int,
        default=0,
        help="Print this many generated samples per run/target/tool before coverage measurement.",
    )
    parser.add_argument(
        "--targets",
        default="all",
        help="Comma-separated target names to run, or 'all'.",
    )
    args = parser.parse_args()

    require(coverage is not None, "coverage is not installed. Run: pip install coverage")
    require(args.runs > 0, "--runs must be positive")
    require(args.n_positive >= 0, "--n-positive must be non-negative")
    require(args.n_negative >= 0, "--n-negative must be non-negative")
    require(args.debug_samples >= 0, "--debug-samples must be non-negative")
    require(args.external_tool_timeout_seconds > 0, "--external-tool-timeout-seconds must be positive")

    tool_specs = parse_tools(args.tools)
    target_specs = build_target_specs()
    if args.targets.strip().lower() != "all":
        requested_targets = {target.strip() for target in args.targets.split(",") if target.strip()}
        known_targets = {spec.name for spec in target_specs}
        unknown_targets = sorted(requested_targets - known_targets)
        require(not unknown_targets, f"Unknown target(s): {', '.join(unknown_targets)}")
        target_specs = [spec for spec in target_specs if spec.name in requested_targets]
        require(target_specs, "--targets did not select any targets")
    os.makedirs(args.coverage_root, exist_ok=True)
    for output_path in (args.summary_json, args.results_json, args.chart_path, args.latex_table_path):
        output_dir = os.path.dirname(output_path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

    jobs = build_jobs(
        target_specs,
        tool_specs,
        runs=args.runs,
        seed=args.seed,
        n_positive=args.n_positive,
        n_negative=args.n_negative,
        coverage_root=args.coverage_root,
        debug_samples=args.debug_samples,
        external_tool_timeout_seconds=args.external_tool_timeout_seconds,
    )
    raw_runs = run_jobs_streaming(
        jobs,
        workers=max(1, args.workers),
        results_jsonl=args.results_jsonl,
        progress_every=args.progress_every,
    )
    aggregate = aggregate_runs(raw_runs, target_specs, tool_specs)
    statistical_tests = compute_statistical_tests(
        raw_runs,
        tool_specs,
        iterations=args.stats_iterations,
        seed=args.seed,
    )

    output = {
        "n_positive": args.n_positive,
        "n_negative": args.n_negative,
        "runs": args.runs,
        "seed": args.seed,
        "debug_samples": args.debug_samples,
        "external_tool_timeout_seconds": args.external_tool_timeout_seconds,
        "generator_validate": GENERATOR_VALIDATE,
        "positive_mode": POSITIVE_MODE,
        "negative_mode": "compact_graph",
        "tools": tool_specs,
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
        "statistical_tests": statistical_tests,
        "notes": {
            "comparison_chart": "Two-panel grouped bar chart: top for code coverage, bottom for branch coverage.",
            "generator_regexes": "Some Django targets use simplified practical regexes for generation because the full validator regex is too complex for the graph generator.",
            "statistics": "Paired two-sided sign-flip permutation tests compare RegexInstantiator against each other tool by run_index and target.",
            "debug_samples": "When --debug-samples is positive, the benchmark prints generated sample previews to stdout.",
            "external_tool_timeout": "Subprocess-backed generators are stopped after --external-tool-timeout-seconds.",
            "mutrex_semantics": "MutRex CONF/REJECT labels are checked with Python re.fullmatch; dialect disagreements are recorded as tool failures.",
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

    write_latex_coverage_table(aggregate, target_specs, tool_specs, args.latex_table_path)
    render_comparison_chart(aggregate, tool_specs, args.chart_path)
    print_summary(aggregate)
    print_comparison_summary(aggregate, target_specs)
    print_statistical_tests(statistical_tests)
    print(f"\nRaw JSONL saved to: {os.path.abspath(args.results_jsonl)}")
    print(f"Saved aggregate results to: {os.path.abspath(args.results_json)}")
    print(f"Summary saved to: {os.path.abspath(args.summary_json)}")
    print(f"LaTeX table saved to: {os.path.abspath(args.latex_table_path)}")
    print(f"Comparison chart saved to: {os.path.abspath(args.chart_path)}")
    print(f"Coverage reports saved under: {os.path.abspath(args.coverage_root)}")
    print_coverage_matrix(aggregate, target_specs, tool_specs)
    print_regexinstantiator_advantage(aggregate, target_specs, tool_specs)


if __name__ == "__main__":
    main()
