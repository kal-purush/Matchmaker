import argparse
import json
import re
from collections import Counter
from pathlib import Path


DEFAULT_RESULTS_PATH = (
    Path(__file__).resolve().parent
    / "correctness_outputs"
    / "regex_correctness_benchmark_results.jsonl"
)


def load_rows(path):
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                rows.append({
                    "line_number": line_number,
                    "status": "malformed_json",
                    "error_type": "JSONDecodeError",
                    "error": str(exc),
                })
                continue
            row["line_number"] = line_number
            rows.append(row)
    return rows


def classify_pattern_issue(pattern):
    compact = " ".join((pattern or "").strip().split())
    if not compact:
        return "empty_pattern"
    if re.match(r"^(RedirectMatch|RewriteRule|RewriteCond|rewrite)\b", compact, re.IGNORECASE):
        return "embedded_config_snippet"
    if ".match(/" in compact or ".split(" in compact or ".join(" in compact or compact.startswith("$("):
        return "embedded_javascript_snippet"
    if re.search(r"(^|\s)\$[0-9]+\s*(?:!=|==|~|!~)", compact):
        return "embedded_shell_or_awk_snippet"
    if re.match(r"^(sed|grep|awk)\b", compact) or re.match(r"^s/.*(/[a-z]*)?;?$", compact):
        return "embedded_shell_or_awk_snippet"
    if re.search(r"\b(var|echo|delete|std::string)\b", compact) or re.search(r"\$[A-Za-z_]\w*\s*=", compact):
        return "embedded_code_snippet"
    if re.search(r"[$\\][0-9]+\b", compact) and not re.search(r"\\[0-9]", compact):
        return "replacement_or_shell_variable"
    return ""


def summarize(rows, example_limit):
    total = len(rows)
    error_rows = [row for row in rows if row.get("status") == "error" or row.get("error_type")]
    timeout_rows = [row for row in error_rows if row.get("error_type") == "TimeoutError"]
    zero_rows = [row for row in rows if row.get("generated") == 0 and not row.get("error_type")]
    invalid_rows = [row for row in rows if int(row.get("invalid") or 0) > 0]
    valid_rows = [row for row in rows if int(row.get("valid") or 0) > 0]
    total_generated = sum(int(row.get("generated") or 0) for row in rows)
    total_invalid = sum(int(row.get("invalid") or 0) for row in rows)
    invalid_patterns = {}
    for row in invalid_rows:
        invalid_patterns.setdefault(row.get("pattern", ""), row)

    zero_issue_counts = Counter()
    zero_issue_examples = {}
    zero_no_issue_examples = []
    for row in zero_rows:
        issue = classify_pattern_issue(row.get("pattern", ""))
        if issue:
            zero_issue_counts[issue] += 1
            zero_issue_examples.setdefault(issue, row)
        else:
            zero_no_issue_examples.append(row)

    invalid_with_valid = [row for row in invalid_rows if int(row.get("valid") or 0) > 0]
    invalid_without_valid = [row for row in invalid_rows if int(row.get("valid") or 0) == 0]

    print("Correctness Result Analysis")
    print("=" * 80)
    print(f"total_rows={total}")
    print(f"errors={len(error_rows)}")
    print(f"timeouts={len(timeout_rows)}")
    print(f"generated_zero={len(zero_rows)}")
    print(f"generated_zero_with_pattern_issue={sum(zero_issue_counts.values())}")
    print(f"generated_zero_without_detected_issue={len(zero_no_issue_examples)}")
    print(f"rows_with_invalid={len(invalid_rows)}")
    print(f"rows_with_invalid_and_valid={len(invalid_with_valid)}")
    print(f"rows_with_invalid_and_no_valid={len(invalid_without_valid)}")
    print(f"rows_with_valid={len(valid_rows)}")
    print(f"total_generated_inputs={total_generated}")
    print(f"total_invalid_inputs={total_invalid}")
    print(f"distinct_patterns_with_invalid={len(invalid_patterns)}")

    print("\nError Types")
    for label, count in Counter(row.get("error_type", "unknown") for row in error_rows).most_common():
        print(f"  {label}: {count}")

    print("\nGenerated=0 Pattern Issues")
    for label, count in zero_issue_counts.most_common():
        row = zero_issue_examples[label]
        print(f"  {label}: {count}")
        print(f"    example index={row.get('index')} line={row.get('line_number')} pattern={row.get('pattern')[:180]}")

    if zero_no_issue_examples:
        print("\nGenerated=0 Without Detected Pattern Issue")
        for row in zero_no_issue_examples[:example_limit]:
            print(f"  index={row.get('index')} line={row.get('line_number')} pattern={row.get('pattern')[:180]}")

    print("\nInvalid Rows With Some Valid Samples")
    for row in invalid_with_valid[:example_limit]:
        print(
            f"  index={row.get('index')} valid={row.get('valid')} invalid={row.get('invalid')} "
            f"pattern={row.get('pattern')[:160]}"
        )
        examples = row.get("invalid_examples") or []
        if examples:
            print(f"    invalid_examples={examples[:3]}")

    print("\nDistinct Patterns With Invalid Samples")
    for pattern, row in list(invalid_patterns.items())[:example_limit]:
        print(
            f"  index={row.get('index')} valid={row.get('valid')} invalid={row.get('invalid')} "
            f"pattern={pattern[:220]}"
        )

    print("\nInvalid Rows With No Valid Samples")
    for row in invalid_without_valid[:example_limit]:
        print(
            f"  index={row.get('index')} invalid={row.get('invalid')} "
            f"pattern={row.get('pattern')[:160]}"
        )
        examples = row.get("invalid_examples") or []
        if examples:
            print(f"    invalid_examples={examples[:3]}")


def main():
    parser = argparse.ArgumentParser(description="Analyze regex correctness benchmark JSONL results.")
    parser.add_argument("path", nargs="?", default=str(DEFAULT_RESULTS_PATH))
    parser.add_argument("--examples", type=int, default=10)
    args = parser.parse_args()

    summarize(load_rows(args.path), args.examples)


if __name__ == "__main__":
    main()
