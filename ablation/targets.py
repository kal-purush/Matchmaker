"""Ablation target set: functions whose input regex is defined in their own source.

The main coverage benchmark (``benchmarks/python_regex_function_coverage_benchmark``)
pairs practical, hand-written regexes with validators. Reusing those targets in the
ablation would report different coverage for the same target than the main
benchmark. These targets are disjoint from it: no module here is measured by the
main benchmark.

Each target was screened, before any ablation was run, on four criteria:

1. it is not a function measured by the main benchmark (only parse_duration shares
   a module with one: the main benchmark measures django.utils.dateparse through
   parse_datetime);
2. the generation regex is taken from the target's own source code (combined
   only with what the function itself does first, such as ``strip()``);
3. Matchmaker can generate 1000 samples from it quickly;
4. regex and function agree: at most 20% of generated positives are rejected and
   at most about 2% of generated negatives are accepted;
5. the target function itself rejects invalid input; the wrapper here only reports
   its decision. The tomllib value parsers (RE_NUMBER, RE_DATETIME, RE_LOCALTIME)
   were removed for this: they parse a valid prefix (``00:00:00`` of
   ``00:00:00:00``) and leave the rest to the TOML line parser, so negatives run the
   whole success path and only our end-of-input check rejected them.

``python -m ablation.targets`` reports the agreement numbers for this set.
"""

import json
import json.scanner
import re
from dataclasses import dataclass
from types import ModuleType
from typing import Callable

import _pydecimal
import django.utils.dateparse
import importlib.metadata
import marshmallow.validate
import packaging.specifiers
import packaging.version


@dataclass(frozen=True)
class AblationTargetSpec:
    name: str
    regex: str
    validator_fn: Callable[[str], bool]
    target_module: ModuleType
    note: str


def _accepts(function, sample):
    try:
        function(sample)
    except Exception:
        return False
    return True


# ----------------------------------------------------------------- packaging
def validate_packaging_version(sample):
    return _accepts(packaging.version.Version, sample)


def validate_packaging_specifier(sample):
    return _accepts(packaging.specifiers.Specifier, sample)


# Version and Specifier fullmatch r"\s*" + <pattern> + r"\s*" with VERBOSE | IGNORECASE.
PACKAGING_VERSION_REGEX = r"(?ix)\s*" + packaging.version.VERSION_PATTERN + r"\s*"
PACKAGING_SPECIFIER_REGEX = (
    r"(?ix)\s*" + packaging.specifiers.Specifier._specifier_regex_str + r"\s*"
)


# ---------------------------------------------------------------- marshmallow
def validate_marshmallow_email(sample):
    return _accepts(marshmallow.validate.Email(), sample)


def validate_marshmallow_url(sample):
    return _accepts(marshmallow.validate.URL(), sample)


def _without_anchors(pattern):
    return pattern.replace(r"\Z", "").replace("(^", "(").replace("|^", "|")


# Email() splits at the last "@", matches USER_REGEX on the left and DOMAIN_REGEX
# (or the whitelisted "localhost") on the right.
MARSHMALLOW_EMAIL_REGEX = (
    "(?i)(?:" + _without_anchors(marshmallow.validate.Email.USER_REGEX.pattern) + ")"
    "@(?:" + _without_anchors(marshmallow.validate.Email.DOMAIN_REGEX.pattern) + "|localhost)"
)
# URL() checks the scheme against its default list (http, https, ftp, ftps), then
# searches the regex it builds for absolute URLs with a required TLD.
MARSHMALLOW_URL_REGEX = "(?i)" + marshmallow.validate.URL._regex(
    relative=False, absolute=True, require_tld=True
).pattern.lstrip("^").replace(r"(?:[a-z0-9\.\-\+]*)://", "(?:https?|ftps?)://")


# --------------------------------------------------------------------- others
def validate_json_number(sample):
    try:
        value, end = json.scanner.py_make_scanner(json.JSONDecoder())(sample, 0)
    except Exception:
        return False
    return end == len(sample) and isinstance(value, (int, float))


def validate_entrypoint(sample):
    try:
        return importlib.metadata.EntryPoint("name", sample, "group").module is not None
    except Exception:
        return False


def validate_django_duration(sample):
    try:
        return django.utils.dateparse.parse_duration(sample) is not None
    except Exception:
        return False


# parse_duration tries standard_duration_re, iso8601_duration_re and
# postgres_interval_re in turn; their group names repeat, so they are dropped.
DJANGO_DURATION_REGEX = "(?:" + "|".join(
    re.sub(r"\(\?P<[^>]+>", "(?:", compiled.pattern)
    for compiled in (
        django.utils.dateparse.standard_duration_re,
        django.utils.dateparse.iso8601_duration_re,
        django.utils.dateparse.postgres_interval_re,
    )
) + ")"


def validate_pydecimal(sample):
    return _accepts(_pydecimal.Decimal, sample)


# Decimal(value) runs _parser on value.strip().replace("_", ""); the surrounding
# whitespace is modelled, underscores anywhere are not.
PYDECIMAL_REGEX = (
    r"(?ix)\s*(?:" + _pydecimal._parser.__self__.pattern.replace(r"\Z", "") + r")\s*"
)


def build_ablation_target_specs():
    return [
        AblationTargetSpec(
            "packaging_version", PACKAGING_VERSION_REGEX, validate_packaging_version,
            packaging.version, "packaging.version.Version; regex VERSION_PATTERN.",
        ),
        AblationTargetSpec(
            "packaging_specifier", PACKAGING_SPECIFIER_REGEX, validate_packaging_specifier,
            packaging.specifiers, "packaging.specifiers.Specifier; its specifier regex.",
        ),
        AblationTargetSpec(
            "marshmallow_email", MARSHMALLOW_EMAIL_REGEX, validate_marshmallow_email,
            marshmallow.validate, "marshmallow.validate.Email; USER_REGEX @ DOMAIN_REGEX.",
        ),
        AblationTargetSpec(
            "marshmallow_url", MARSHMALLOW_URL_REGEX, validate_marshmallow_url,
            marshmallow.validate, "marshmallow.validate.URL; its absolute-URL regex.",
        ),
        AblationTargetSpec(
            "json_number", json.scanner.NUMBER_RE.pattern, validate_json_number,
            json.scanner, "json.scanner.py_make_scanner; NUMBER_RE.",
        ),
        AblationTargetSpec(
            "importlib_entrypoint", importlib.metadata.EntryPoint.pattern.pattern,
            validate_entrypoint, importlib.metadata,
            "importlib.metadata.EntryPoint; EntryPoint.pattern.",
        ),
        AblationTargetSpec(
            "django_parse_duration", DJANGO_DURATION_REGEX, validate_django_duration,
            django.utils.dateparse,
            "django.utils.dateparse.parse_duration; its three duration regexes.",
        ),
        AblationTargetSpec(
            "pydecimal_Decimal", PYDECIMAL_REGEX, validate_pydecimal, _pydecimal,
            "_pydecimal.Decimal; its _parser regex with surrounding whitespace.",
        ),
    ]


TARGET_SETS = ("ablation", "benchmark")


def target_specs(target_set="ablation"):
    """Target specs by name for a target set."""

    if target_set == "ablation":
        specs = build_ablation_target_specs()
    elif target_set == "benchmark":
        from benchmarks.python_regex_function_coverage_benchmark import build_target_specs

        specs = build_target_specs()
    else:
        raise ValueError(f"Unknown target set: {target_set}")
    return {spec.name: spec for spec in specs}


def agreement_report(n=1000, seed=0):
    """Positives each function rejects and negatives it accepts, per target."""

    from .variants import _negative_samples, _positive_samples

    rows = []
    for spec in build_ablation_target_specs():
        positives = _positive_samples(spec.regex, n, seed, True, randomize=False)
        negatives = _negative_samples(spec.regex, n, seed, True)
        rows.append({
            "target": spec.name,
            "module": spec.target_module.__name__,
            "positives": len(positives),
            "positives_rejected": sum(not spec.validator_fn(s) for s in positives),
            "negatives": len(negatives),
            "negatives_accepted": sum(spec.validator_fn(s) for s in negatives),
        })
    return rows


def main():
    print(f"{'target':22} {'module':22} {'pos rejected':>14} {'neg accepted':>14}")
    for row in agreement_report():
        print(
            f"{row['target']:22} {row['module']:22}"
            f" {row['positives_rejected']:>6}/{row['positives']:<7}"
            f" {row['negatives_accepted']:>6}/{row['negatives']:<7}"
        )


if __name__ == "__main__":
    main()
