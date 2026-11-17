import json
import os
from dataclasses import asdict, dataclass
from typing import Callable, Dict, List

MPL_CONFIG_DIR = os.path.abspath(".matplotlib-cache")
os.makedirs(MPL_CONFIG_DIR, exist_ok=True)
os.environ.setdefault("MPLCONFIGDIR", MPL_CONFIG_DIR)

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

from email_validator_tool_coverage import (
    EMAIL_REGEX,
    SEED,
    SampleCase,
    configure_django_for_standalone_use,
    require,
)
from regex_positive_generator import generate_negative_samples
from regex_positive_generator.experimental import CompactRegexGraphBuilder
from regex_positive_generator.experimental.compact_sampler_lookaround_experimental import (
    ExperimentalLookaroundCompactGraphSampler,
)

try:
    from django.core.exceptions import ValidationError as DjangoValidationError
    from django.core.validators import EmailValidator as DjangoEmailValidator
except ImportError:  # pragma: no cover - dependency check for runtime
    DjangoValidationError = Exception
    DjangoEmailValidator = None

try:
    from email_validator import EmailNotValidError, validate_email
except ImportError:  # pragma: no cover - dependency check for runtime
    EmailNotValidError = Exception
    validate_email = None

try:
    import validators
except ImportError:  # pragma: no cover - dependency check for runtime
    validators = None

try:
    from marshmallow import ValidationError as MarshmallowValidationError
    from marshmallow.validate import Email as MarshmallowEmailValidator
except ImportError:  # pragma: no cover - dependency check for runtime
    MarshmallowValidationError = Exception
    MarshmallowEmailValidator = None


N_POSITIVE = int(os.environ.get("HEATMAP_N_POSITIVE", "1000"))
N_NEGATIVE = int(os.environ.get("HEATMAP_N_NEGATIVE", "1000"))
OUTPUT_DIR = os.path.abspath("validator_disagreement_outputs")
GENERATOR_TIMEOUT_SECONDS = float(os.environ.get("HEATMAP_GENERATOR_TIMEOUT_SECONDS", "120"))


@dataclass
class ValidatorSummary:
    validator: str
    accepted: int
    rejected: int
    acceptance_rate: float


@dataclass
class SampleExample:
    sample: str
    source: str
    expected_match: bool
    decisions: Dict[str, bool]


def generate_ours_samples(pattern: str, n_pos: int, n_neg: int, seed: int) -> List[SampleCase]:
    builder = CompactRegexGraphBuilder(pattern, validate=True)
    positives = ExperimentalLookaroundCompactGraphSampler(builder, validate=True).generate_samples(
        n=n_pos,
        seed=seed,
    )
    negatives = generate_negative_samples(
        pattern,
        n=n_neg,
        seed=seed,
        use_fullmatch=True,
        validate=True,
        timeout_seconds=GENERATOR_TIMEOUT_SECONDS,
    )
    return [
        *(SampleCase(sample=sample, expected_match=True, source="generated_positive") for sample in positives),
        *(SampleCase(sample=sample, expected_match=False, source="generated_negative") for sample in negatives),
    ]


def validate_with_django_default(sample: str) -> bool:
    require(DjangoEmailValidator is not None, "django is not installed. Run: pip install django")
    configure_django_for_standalone_use()
    validator = DjangoEmailValidator()
    try:
        validator(sample)
        return True
    except DjangoValidationError:
        return False
    except Exception:
        return False


def validate_with_email_validator(sample: str, **kwargs) -> bool:
    require(validate_email is not None, "email-validator is not installed. Run: pip install email-validator")
    try:
        validate_email(sample, **kwargs)
        return True
    except EmailNotValidError:
        return False
    except Exception:
        return False


def validate_with_validators(sample: str, **kwargs) -> bool:
    require(validators is not None, "validators is not installed. Run: pip install validators")
    try:
        return validators.email(sample, **kwargs) is True
    except Exception:
        return False


def validate_with_marshmallow(sample: str) -> bool:
    require(MarshmallowEmailValidator is not None, "marshmallow is not installed. Run: pip install marshmallow")
    validator = MarshmallowEmailValidator()
    try:
        validator(sample)
        return True
    except MarshmallowValidationError:
        return False
    except Exception:
        return False


def get_validator_specs() -> List[tuple[str, Callable[[str], bool]]]:
    require(DjangoEmailValidator is not None, "django is not installed. Run: pip install django")
    require(validate_email is not None, "email-validator is not installed. Run: pip install email-validator")
    require(validators is not None, "validators is not installed. Run: pip install validators")
    require(MarshmallowEmailValidator is not None, "marshmallow is not installed. Run: pip install marshmallow")
    return [
        ("django.default", validate_with_django_default),
        (
            "email_validator.default",
            lambda s: validate_with_email_validator(s, check_deliverability=False),
        ),
        ("validators.default", lambda s: validate_with_validators(s)),
        ("marshmallow.validate.Email", validate_with_marshmallow),
    ]


def build_decision_frame(samples, validator_specs) -> pd.DataFrame:
    rows: List[Dict[str, object]] = []
    for idx, case in enumerate(samples):
        row: Dict[str, object] = {
            "sample_id": idx,
            "sample": case.sample,
            "expected_match": case.expected_match,
            "source": case.source,
        }
        for validator_name, validator_fn in validator_specs:
            row[validator_name] = validator_fn(case.sample)
        rows.append(row)
    return pd.DataFrame(rows)


def summarize_validators(frame: pd.DataFrame, validator_names: List[str]) -> List[ValidatorSummary]:
    summaries: List[ValidatorSummary] = []
    total = len(frame)
    for name in validator_names:
        accepted = int(frame[name].sum())
        rejected = total - accepted
        summaries.append(
            ValidatorSummary(
                validator=name,
                accepted=accepted,
                rejected=rejected,
                acceptance_rate=(accepted / total) if total else 0.0,
            )
        )
    return summaries


def compute_disagreement_matrices(
    frame: pd.DataFrame,
    validator_names: List[str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    count_matrix = pd.DataFrame(0, index=validator_names, columns=validator_names, dtype=int)
    rate_matrix = pd.DataFrame(0.0, index=validator_names, columns=validator_names, dtype=float)
    total = len(frame)

    for left in validator_names:
        for right in validator_names:
            disagreements = int((frame[left] != frame[right]).sum())
            count_matrix.loc[left, right] = disagreements
            rate_matrix.loc[left, right] = (disagreements / total) if total else 0.0

    return count_matrix, rate_matrix


def render_heatmap(rate_matrix: pd.DataFrame, count_matrix: pd.DataFrame, output_path: str) -> None:
    annotations = rate_matrix.copy().astype(str)
    for row_name in rate_matrix.index:
        for col_name in rate_matrix.columns:
            count = int(count_matrix.loc[row_name, col_name])
            rate = float(rate_matrix.loc[row_name, col_name]) * 100.0
            annotations.loc[row_name, col_name] = f"{count}\n{rate:.1f}%"

    plt.figure(figsize=(14, 11))
    sns.heatmap(
        rate_matrix,
        annot=annotations,
        fmt="",
        cmap="magma",
        vmin=0.0,
        vmax=1.0,
        linewidths=0.5,
        cbar_kws={"label": "Disagreement rate"},
    )
    plt.title("Email Validator Disagreement Heatmap")
    plt.xlabel("Validator")
    plt.ylabel("Validator")
    plt.tight_layout()
    plt.savefig(output_path, dpi=220)
    plt.close()


def print_summary(summaries: List[ValidatorSummary]) -> None:
    print("\nValidator Acceptance Summary")
    print("=" * 82)
    print(f"{'Validator':<40} {'Accepted':>10} {'Rejected':>10} {'Accept %':>10}")
    print("-" * 82)
    for summary in summaries:
        print(
            f"{summary.validator:<40} {summary.accepted:>10} {summary.rejected:>10} "
            f"{summary.acceptance_rate * 100:>9.2f}%"
        )


def collect_examples(
    frame: pd.DataFrame,
    validator_names: List[str],
    limit: int = 10,
) -> Dict[str, List[SampleExample]]:
    example_buckets: Dict[str, List[SampleExample]] = {
        "django_accepts_but_should_not": [],
        "django_rejects_but_should_accept": [],
        "django_accepts_but_others_reject": [],
        "django_rejects_but_others_accept": [],
    }

    other_validators = [name for name in validator_names if name != "django.default"]

    for _, row in frame.iterrows():
        decisions = {name: bool(row[name]) for name in validator_names}
        example = SampleExample(
            sample=row["sample"],
            source=row["source"],
            expected_match=bool(row["expected_match"]),
            decisions=decisions,
        )

        if decisions["django.default"] and not bool(row["expected_match"]):
            if len(example_buckets["django_accepts_but_should_not"]) < limit:
                example_buckets["django_accepts_but_should_not"].append(example)

        if (not decisions["django.default"]) and bool(row["expected_match"]):
            if len(example_buckets["django_rejects_but_should_accept"]) < limit:
                example_buckets["django_rejects_but_should_accept"].append(example)

        if decisions["django.default"] and any(not decisions[name] for name in other_validators):
            if len(example_buckets["django_accepts_but_others_reject"]) < limit:
                example_buckets["django_accepts_but_others_reject"].append(example)

        if (not decisions["django.default"]) and any(decisions[name] for name in other_validators):
            if len(example_buckets["django_rejects_but_others_accept"]) < limit:
                example_buckets["django_rejects_but_others_accept"].append(example)

    return example_buckets


def print_examples(title: str, examples: List[SampleExample]) -> None:
    print(f"\n{title}")
    print("-" * len(title))
    if not examples:
        print("None")
        return
    for idx, example in enumerate(examples, start=1):
        decisions_str = ", ".join(
            f"{name}={'T' if accepted else 'F'}"
            for name, accepted in example.decisions.items()
        )
        print(
            f"{idx}. sample={example.sample!r} | expected_match={example.expected_match} "
            f"| source={example.source} | {decisions_str}"
        )


def main() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    validator_specs = get_validator_specs()

    samples = generate_ours_samples(
        EMAIL_REGEX,
        n_pos=N_POSITIVE,
        n_neg=N_NEGATIVE,
        seed=SEED,
    )
    validator_names = [name for name, _ in validator_specs]
    frame = build_decision_frame(samples, validator_specs)
    count_matrix, rate_matrix = compute_disagreement_matrices(frame, validator_names)
    summaries = summarize_validators(frame, validator_names)
    example_buckets = collect_examples(frame, validator_names)

    sample_output_path = os.path.join(OUTPUT_DIR, "validator_decisions.jsonl")
    csv_output_path = os.path.join(OUTPUT_DIR, "validator_decisions.csv")
    summary_output_path = os.path.join(OUTPUT_DIR, "validator_summary.json")
    count_matrix_path = os.path.join(OUTPUT_DIR, "validator_disagreement_counts.csv")
    rate_matrix_path = os.path.join(OUTPUT_DIR, "validator_disagreement_rates.csv")
    heatmap_path = os.path.join(OUTPUT_DIR, "validator_disagreement_heatmap.png")

    with open(sample_output_path, "w", encoding="utf-8") as fh:
        for row in frame.to_dict(orient="records"):
            fh.write(json.dumps(row, ensure_ascii=True) + "\n")
    frame.to_csv(csv_output_path, index=False)
    count_matrix.to_csv(count_matrix_path)
    rate_matrix.to_csv(rate_matrix_path)

    with open(summary_output_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "regex": EMAIL_REGEX,
                "seed": SEED,
                "n_positive": N_POSITIVE,
                "n_negative": N_NEGATIVE,
                "positive_sampler": "ExperimentalLookaroundCompactGraphSampler",
                "generator_timeout_seconds": GENERATOR_TIMEOUT_SECONDS,
                "total_samples": len(frame),
                "validators": validator_names,
                "summaries": [asdict(summary) for summary in summaries],
            },
            fh,
            indent=2,
        )

    render_heatmap(rate_matrix, count_matrix, heatmap_path)

    print(f"\nGenerated {len(frame)} samples using our tool only.")
    print(f"Positive samples: {int(frame['expected_match'].sum())}")
    print(f"Negative samples: {len(frame) - int(frame['expected_match'].sum())}")
    print_summary(summaries)
    print_examples(
        "Samples Django Accepts But Should Not Accept",
        example_buckets["django_accepts_but_should_not"],
    )
    print_examples(
        "Samples Django Rejects But Should Accept",
        example_buckets["django_rejects_but_should_accept"],
    )
    print_examples(
        "Samples Django Accepts But Others Reject",
        example_buckets["django_accepts_but_others_reject"],
    )
    print_examples(
        "Samples Django Rejects But Others Accept",
        example_buckets["django_rejects_but_others_accept"],
    )
    print(f"\nPer-sample decisions: {sample_output_path}")
    print(f"Decision table CSV: {csv_output_path}")
    print(f"Summary JSON: {summary_output_path}")
    print(f"Disagreement counts CSV: {count_matrix_path}")
    print(f"Disagreement rates CSV: {rate_matrix_path}")
    print(f"Heatmap: {heatmap_path}")


if __name__ == "__main__":
    main()
