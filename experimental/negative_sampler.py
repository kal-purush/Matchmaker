import re
import signal
import time

from regex_positive_generator.experimental import CompactRegexGraphBuilder
from regex_positive_generator.experimental.negative_choices import (
    ViolationChoiceHelper,
    is_ascii_word_class,
    is_ascii_word_or_space_class,
    is_universal_printable_class,
    looks_like_broad_printable_class,
    printable_exclusions,
)
from regex_positive_generator.experimental.negative_context import iter_node_contexts
from regex_positive_generator.experimental.negative_families import (
    family_candidates,
    minimal_local_faults,
    optimized_family_fill_candidates,
    representative_valid,
    select_violation_families,
)
from regex_positive_generator.parser import DEFAULT_REGEX_TIMEOUT_SECONDS, REGEX_AVAILABLE

try:
    import regex
except ImportError:  # pragma: no cover - optional dependency
    regex = None


def run_with_timeout(func, timeout_seconds):
    if timeout_seconds is None or timeout_seconds <= 0:
        return func()

    def _timeout_handler(signum, frame):
        raise TimeoutError(f"Timed out after {timeout_seconds}s")

    start = time.monotonic()
    previous_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    previous_delay, previous_interval = signal.getitimer(signal.ITIMER_REAL)
    effective_timeout = min(timeout_seconds, previous_delay) if previous_delay > 0 else timeout_seconds
    signal.setitimer(signal.ITIMER_REAL, effective_timeout)
    try:
        return func()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        elapsed = time.monotonic() - start
        if previous_delay > 0:
            signal.setitimer(signal.ITIMER_REAL, max(previous_delay - elapsed, 0.0), previous_interval)
        signal.signal(signal.SIGALRM, previous_handler)


def regex_matches(pattern, sample, flags, *, use_fullmatch):
    engine = regex if REGEX_AVAILABLE and regex is not None else re
    try:
        if use_fullmatch:
            return engine.fullmatch(pattern, sample, flags) is not None
        return engine.search(pattern, sample, flags) is not None
    except Exception:
        return False


class NegativeGraphSampler:
    def __init__(self, pattern, *, flags=0, validate=True):
        self.pattern = pattern
        self.flags = flags
        self.validate = validate
        self.builder = CompactRegexGraphBuilder(pattern, flags=flags, validate=validate)
        self.representative = representative_valid(self.builder)
        self.contexts = list(iter_node_contexts(self.builder.root))
        self.has_wildcard_class = any(context.is_wildcard_class for context in self.contexts)
        self.has_broad_consuming_class = self._has_broad_consuming_class()
        self.has_universal_class = self._has_universal_class()
        self.has_dot_newline_alternative = self._has_dot_newline_alternative()
        self.has_near_universal_alternation = self._has_near_universal_alternation()
        self.raw_violation_chars = self._raw_violation_chars()

    def generate(self, n=1000, seed=0, use_fullmatch=True, timeout_seconds=None, on_sample=None):
        if n <= 0:
            return []
        if timeout_seconds is None:
            timeout_seconds = DEFAULT_REGEX_TIMEOUT_SECONDS

        def _generate():
            helper = ViolationChoiceHelper(seed=seed, flags=self.flags)
            seen = set()
            results = []

            self._add_empty_seed(results, seen, n=n, use_fullmatch=use_fullmatch, on_sample=on_sample)
            if len(results) >= n:
                return results[:n]

            families = select_violation_families(self.builder)
            family_map = family_candidates(self.builder, helper)

            for candidate in minimal_local_faults(self.builder, helper):
                self._add_candidate(
                    results,
                    seen,
                    candidate,
                    n=n,
                    use_fullmatch=use_fullmatch,
                    on_sample=on_sample,
                    family="minimal_local_fault",
                )
                if len(results) >= n:
                    return results[:n]

            selected_families = [family for family in families if family in family_map]
            cursors = {family: 0 for family in selected_families}
            active = set(selected_families)

            while active and len(results) < n:
                for family in selected_families:
                    if family not in active:
                        continue
                    candidates = family_map[family]
                    cursor = cursors[family]
                    while cursor < len(candidates):
                        candidate = candidates[cursor]
                        cursor += 1
                        if self._add_candidate(
                            results,
                            seen,
                            candidate,
                            n=n,
                            use_fullmatch=use_fullmatch,
                            on_sample=on_sample,
                            family=family,
                        ):
                            break
                    cursors[family] = cursor
                    if cursor >= len(candidates):
                        active.remove(family)
                    if len(results) >= n:
                        return results[:n]

            fill_attempts = 0
            max_raw_fill_attempts = max(n * 200, 1000)
            for candidate in optimized_family_fill_candidates(self.builder, helper):
                fill_attempts += 1
                self._add_candidate(
                    results,
                    seen,
                    candidate,
                    n=n,
                    use_fullmatch=use_fullmatch,
                    on_sample=on_sample,
                    family="optimized_family_fill",
                )
                if len(results) >= n:
                    return results[:n]
                if not self.validate and fill_attempts >= max_raw_fill_attempts:
                    break

            return results[:n]

        return run_with_timeout(_generate, timeout_seconds)

    def _add_candidate(self, results, seen, candidate, *, n, use_fullmatch, on_sample=None, family=None):
        if len(results) >= n or candidate in seen:
            return False
        if candidate == self.representative:
            return False
        if candidate == "" and regex_matches(self.pattern, candidate, self.flags, use_fullmatch=use_fullmatch):
            return False
        if not self.validate and not self._raw_candidate_allowed(candidate, family):
            return False
        if self.validate and regex_matches(self.pattern, candidate, self.flags, use_fullmatch=use_fullmatch):
            return False
        seen.add(candidate)
        results.append(candidate)
        if on_sample is not None:
            on_sample(candidate)
        return True

    def _add_empty_seed(self, results, seen, *, n, use_fullmatch, on_sample=None):
        if regex_matches(self.pattern, "", self.flags, use_fullmatch=use_fullmatch):
            return False
        return self._add_candidate(
            results,
            seen,
            "",
            n=n,
            use_fullmatch=use_fullmatch,
            on_sample=on_sample,
            family="empty_seed",
        )

    def _raw_candidate_allowed(self, candidate, family):
        if family in {"stress_boundary_violation", "bounded_fallback", "unbounded_fallback"}:
            return False
        if family == "empty_seed":
            return True
        if family in {
            "basic_boundary_violation",
            "whitespace_boundary_violation",
            "anchor_violation",
            "structure_violation",
            "zero_width_assertion_violation",
            "unicode_violation",
            "conditional_violation",
            "alternation_violation",
        }:
            return self._targeted_wildcard_dot_candidate(candidate)
        if self.has_near_universal_alternation and len(candidate) <= 1:
            return False
        if self.has_universal_class and family in {
            "minimal_local_fault",
            "character_class_violation",
            "group_violation",
            "escape_sequence_violation",
            "unicode_violation",
            "optimized_family_fill",
        }:
            return self._contains_raw_violation_char(candidate) or self._targeted_wildcard_dot_candidate(candidate)
        if family == "numeric_boundary_violation" and self.has_wildcard_class:
            return self._targeted_wildcard_dot_candidate(candidate)
        if family == "numeric_boundary_violation" and self.has_broad_consuming_class:
            return self._contains_raw_violation_char(candidate) or self._targeted_wildcard_dot_candidate(candidate)
        if family in {"quantifier_underflow_violation", "quantifier_overflow_violation"} and (
            self.has_broad_consuming_class or self.has_wildcard_class
        ):
            return self._contains_raw_violation_char(candidate) or self._targeted_wildcard_dot_candidate(candidate)
        if family == "optimized_family_fill":
            if self.has_wildcard_class:
                return self._targeted_wildcard_dot_candidate(candidate)
            if not self.raw_violation_chars and not self.has_wildcard_class:
                return True
            return self._contains_raw_violation_char(candidate) or self._targeted_wildcard_dot_candidate(candidate)
        if family in {
            "minimal_local_fault",
            "character_class_violation",
            "group_violation",
            "wrong_char_substitution",
            "escape_sequence_violation",
            "case_sensitivity_violation",
        }:
            if self.raw_violation_chars:
                return self._contains_raw_violation_char(candidate) or self._targeted_wildcard_dot_candidate(candidate)
            if self.has_wildcard_class or self.has_universal_class:
                return self._targeted_wildcard_dot_candidate(candidate)
        return family is not None

    def _targeted_wildcard_dot_candidate(self, candidate):
        if (
            not self.has_wildcard_class
            or "\n" not in candidate
            or self.flags & re.DOTALL
            or self.has_dot_newline_alternative
            or self.has_near_universal_alternation
        ):
            return False
        stripped = candidate.replace("\n", "")
        for context in self.contexts:
            if not context.is_wildcard_class:
                continue
            if context.prefix and context.prefix[-1].isspace():
                continue
            if stripped == context.prefix + context.suffix:
                return True
        return False

    def _contains_raw_violation_char(self, candidate):
        return any(ch in self.raw_violation_chars for ch in candidate)

    def _raw_violation_chars(self):
        helper = ViolationChoiceHelper(flags=self.flags)
        chars = set()
        for context in self.contexts:
            node = context.node
            if node.kind != "class" or context.is_wildcard_class:
                continue
            payload = set(node.payload)
            if is_universal_printable_class(payload):
                continue
            if looks_like_broad_printable_class(payload):
                chars.update(printable_exclusions(payload))
            elif self.has_broad_consuming_class:
                if is_ascii_word_class(payload):
                    chars.update(["!", ".", " ", "\t", "\n"])
                elif is_ascii_word_or_space_class(payload):
                    chars.update(["!", ".", ",", ";", ":", "/", "@"])
            else:
                for choice in helper.class_violation_choices(payload):
                    chars.update(choice)
                for choice in helper.group_violation_sequences(payload):
                    chars.update(choice)
        return frozenset(chars)

    def _has_broad_consuming_class(self):
        for context in self.contexts:
            node = context.node
            if node.kind != "class" or context.is_wildcard_class:
                continue
            payload = set(node.payload)
            if (
                looks_like_broad_printable_class(payload)
                or is_universal_printable_class(payload)
                or is_ascii_word_class(payload)
                or is_ascii_word_or_space_class(payload)
            ):
                return True
        return False

    def _has_universal_class(self):
        for context in self.contexts:
            node = context.node
            if node.kind == "class" and is_universal_printable_class(set(node.payload)):
                return True
        return False

    def _has_dot_newline_alternative(self):
        pattern = self.pattern
        return any(
            token in pattern
            for token in (
                r"(.|\n)",
                r"(\n|.)",
                r"(?:\s|.)",
                r"(?:.|\s)",
                r"[\d\D]",
                r"[\D\d]",
                r"[\s\S]",
                r"[\S\s]",
                r"[\w\W]",
                r"[\W\w]",
            )
        )

    def _has_near_universal_alternation(self):
        if "|" not in self.pattern:
            return False
        return self.has_universal_class or any(
            context.node.kind == "class" and looks_like_broad_printable_class(set(context.node.payload))
            for context in self.contexts
        )


def generate_negative_samples(
    pattern,
    n=1000,
    seed=0,
    flags=0,
    validate=True,
    use_fullmatch=True,
    timeout_seconds=None,
    on_sample=None,
):
    return NegativeGraphSampler(pattern, flags=flags, validate=validate).generate(
        n=n,
        seed=seed,
        use_fullmatch=use_fullmatch,
        timeout_seconds=timeout_seconds,
        on_sample=on_sample,
    )
