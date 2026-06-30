import re
import unittest
import warnings
from unittest.mock import patch

from regex_positive_generator import generate_negative_samples
from regex_positive_generator.benchmarks.regex_negative_correctness_benchmark import (
    benchmark_pattern,
    benchmark_pattern_worker,
    drain_worker_messages,
)
from regex_positive_generator.experimental import CompactRegexGraphBuilder
from regex_positive_generator.experimental.negative_choices import ViolationChoiceHelper
from regex_positive_generator.experimental.negative_context import iter_node_contexts
from regex_positive_generator.experimental.negative_families import (
    STRESS_LENGTH,
    case_sensitivity_violation,
    character_class_violation,
    basic_boundary_violation,
    conditional_violation,
    family_candidates,
    quantifier_violations,
    select_violation_families,
    separator_boundary_violation,
    structure_violation,
    unicode_violation,
    wildcard_dot_violation,
    wrong_char_substitution,
    zero_width_assertion_violation,
)


warnings.filterwarnings("ignore", category=DeprecationWarning)


class NegativeSamplerTests(unittest.TestCase):
    def assert_all_negative(self, pattern, samples):
        for sample in samples:
            self.assertIsNone(re.fullmatch(pattern, sample), f"sample unexpectedly matched: {sample!r}")

    def test_package_export_works(self):
        samples = generate_negative_samples(r"ab", n=2, seed=1)

        self.assertIsInstance(samples, list)

    def test_internal_timeout_with_samples_is_reported_as_partial(self):
        def timeout_after_partials(pattern, *, n, seed, validate_generator, on_sample=None):
            on_sample("b")
            on_sample("bb")
            raise TimeoutError("synthetic timeout")

        with patch(
            "regex_positive_generator.benchmarks.regex_negative_correctness_benchmark.benchmark_pattern",
            side_effect=timeout_after_partials,
        ):
            import queue

            output_queue = queue.Queue()
            benchmark_pattern_worker(output_queue, r"a+", 1000, 0, False)

        partial_samples = []
        payload = drain_worker_messages(output_queue, partial_samples)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["error_type"], "TimeoutError")
        self.assertEqual(partial_samples, ["b", "bb"])
        result = payload["result"]
        self.assertTrue(result["partial"])
        self.assertEqual(result["generated"], 2)
        self.assertEqual(result["partial_examples"], ["b", "bb"])
        self.assertEqual(result["partial_validated"], 2)

    def test_empty_string_is_first_when_invalid(self):
        samples = generate_negative_samples(r"[A-Z]{2}\d", n=5, seed=2)

        self.assertTrue(samples)
        self.assertEqual(samples[0], "")
        self.assert_all_negative(r"[A-Z]{2}\d", samples)

    def test_empty_string_is_skipped_when_valid(self):
        samples = generate_negative_samples(r"a*", n=5, seed=3)

        self.assertNotIn("", samples)
        self.assert_all_negative(r"a*", samples)

    def test_one_node_near_misses_for_simple_sequence(self):
        pattern = r"ab[0-9]"
        samples = generate_negative_samples(pattern, n=20, seed=4)
        same_length = [sample for sample in samples if len(sample) == 3]

        self.assertIn("bb0", samples)
        self.assertIn("aa0", samples)
        self.assertIn("aba", samples)
        self.assertGreaterEqual(len(same_length), 3)
        self.assertTrue(any(sample[1:] == "b0" and sample[0] != "a" for sample in same_length))
        self.assertTrue(any(sample[0] == "a" and sample[2] == "0" and sample[1] != "b" for sample in same_length))
        self.assertTrue(any(sample[:2] == "ab" and not sample[2].isdigit() for sample in same_length))
        self.assert_all_negative(pattern, samples)

    def test_repeat_occurrences_get_separate_single_faults(self):
        samples = generate_negative_samples(r"[A-Z]{2}\d", n=30, seed=7)

        self.assertIn("aA0", samples)
        self.assertIn("Aa0", samples)
        self.assertIn("AAa", samples)
        self.assert_all_negative(r"[A-Z]{2}\d", samples)

    def test_class_faults_include_multiple_violation_buckets(self):
        samples = generate_negative_samples(r"[A-Z]", n=20, seed=8)

        self.assertTrue(any(sample.isdigit() for sample in samples))
        self.assertTrue(any(sample and all(ch in string_punctuation_or_space() for ch in sample) for sample in samples))
        self.assertTrue(any(any(ord(ch) > 127 for ch in sample) for sample in samples))
        self.assert_all_negative(r"[A-Z]", samples)

    def test_digit_faults_include_letters_and_punctuation(self):
        samples = generate_negative_samples(r"\d", n=20, seed=9)

        self.assertTrue(any(sample.isalpha() and sample.isascii() for sample in samples))
        self.assertTrue(any(sample and any(not ch.isalnum() and not ch.isspace() for ch in sample) for sample in samples))
        self.assert_all_negative(r"\d", samples)

    def test_results_are_unique_and_capped(self):
        samples = generate_negative_samples(r"[ab]\d", n=7, seed=5)

        self.assertEqual(len(samples), len(set(samples)))
        self.assertLessEqual(len(samples), 7)
        self.assert_all_negative(r"[ab]\d", samples)

    def test_on_sample_receives_accepted_samples_in_order(self):
        emitted = []

        samples = generate_negative_samples(r"[ab]\d", n=12, seed=12, on_sample=emitted.append)

        self.assertEqual(emitted, samples)
        self.assertEqual(len(emitted), len(set(emitted)))
        self.assert_all_negative(r"[ab]\d", emitted)

    def test_on_sample_is_capped_at_requested_count(self):
        emitted = []

        samples = generate_negative_samples(r"[A-Z]{2}\d{3}", n=5, seed=13, on_sample=emitted.append)

        self.assertEqual(emitted, samples)
        self.assertLessEqual(len(emitted), 5)
        self.assertEqual(len(samples), 5)

    def test_same_seed_is_stable(self):
        first = generate_negative_samples(r"[A-Z]{2}\d", n=12, seed=6)
        second = generate_negative_samples(r"[A-Z]{2}\d", n=12, seed=6)

        self.assertEqual(first, second)

    def test_validation_filters_broad_quote_pattern_matches(self):
        pattern = r"^['\"]*(.*?)['\"]*$"
        samples = generate_negative_samples(pattern, n=20, seed=2, validate=True)

        self.assertNotIn("", samples)
        self.assertNotIn(" ", samples)
        self.assert_all_negative(pattern, samples)

    def test_validation_filters_case_sensitivity_matches(self):
        pattern = r"^[ABCEGHJKLMNPRSTVXYabceghjklmnprstvxy]{1}\d{1}[A-Za-z]{1}\d{1}[A-Za-z]{1}\d{1}$"
        samples = generate_negative_samples(pattern, n=20, seed=2, validate=True)

        self.assertNotIn("a0A0A0", samples)
        self.assert_all_negative(pattern, samples)

    def test_validation_filters_unicode_digits_for_python_digit_escape(self):
        pattern = r"^[ABCEGHJKLMNPRSTVXYabceghjklmnprstvxy]{1}\d{1}[A-Za-z]{1}\d{1}[A-Za-z]{1}\d{1}$"
        samples = generate_negative_samples(pattern, n=1000, seed=2, validate=True)

        self.assertNotIn("A١A0A0", samples)
        self.assertNotIn("A0A١A0", samples)
        self.assertNotIn("A0A0A١", samples)
        self.assert_all_negative(pattern, samples)

    def test_negative_benchmark_validation_filters_unicode_digits(self):
        pattern = r"^[ABCEGHJKLMNPRSTVXYabceghjklmnprstvxy]{1}\d{1}[A-Za-z]{1}\d{1}[A-Za-z]{1}\d{1}$"

        result = benchmark_pattern(pattern, n=1000, seed=2, validate_generator=True)

        self.assertEqual(result["matched"], 0)

    def test_negative_benchmark_raw_mode_uses_unicode_aware_digit_choices(self):
        pattern = r"^[ABCEGHJKLMNPRSTVXYabceghjklmnprstvxy]{1}\d{1}[A-Za-z]{1}\d{1}[A-Za-z]{1}\d{1}$"

        result = benchmark_pattern(pattern, n=1000, seed=2, validate_generator=False)

        self.assertEqual(result["matched"], 0)
        self.assertNotIn("A١A0A0", result["matched_examples"])

    def test_unbounded_fallback_fills_requested_validated_count(self):
        pattern = r"^[ABCEGHJKLMNPRSTVXYabceghjklmnprstvxy]{1}\d{1}[A-Za-z]{1}\d{1}[A-Za-z]{1}\d{1}$"

        samples = generate_negative_samples(pattern, n=1000, seed=2, validate=True)

        self.assertEqual(len(samples), 1000)
        self.assertEqual(len(samples), len(set(samples)))
        self.assert_all_negative(pattern, samples)

    def test_raw_generation_avoids_known_permissive_context_matches(self):
        quote_pattern = r"^['\"]*(.*?)['\"]*$"
        quote_samples = generate_negative_samples(quote_pattern, n=40, seed=2, validate=False)
        postal_pattern = (
            r"^[ABCEGHJKLMNPRSTVXYabceghjklmnprstvxy]{1}\d{1}"
            r"[A-Za-z]{1}\d{1}[A-Za-z]{1}\d{1}$"
        )
        postal_samples = generate_negative_samples(postal_pattern, n=40, seed=2, validate=False)

        self.assertNotIn("", quote_samples)
        self.assertNotIn('a\t"', quote_samples)
        self.assertTrue(quote_samples)
        self.assertTrue(all("\n" in sample for sample in quote_samples))
        self.assertNotIn(" ", quote_samples)
        self.assertNotIn("01", quote_samples)
        self.assertNotIn("a0A0A0", postal_samples)

    def test_raw_structured_time_regex_expands_meaningful_families(self):
        pattern = r"^(([0]?[1-9]|1[0-2])(:)([0-5][0-9]))$"

        samples = generate_negative_samples(pattern, n=300, seed=2, validate=False)

        self.assertEqual(len(samples), 300)
        self.assertEqual(len(samples), len(set(samples)))
        self.assertIn("a1:00", samples)
        self.assertIn("01;00", samples)
        self.assertIn("001:00", samples)
        self.assertIn("00:10", samples)
        self.assert_all_negative(pattern, samples)

    def test_raw_unicode_escape_semantics(self):
        digit_samples = generate_negative_samples(r"\d", n=50, seed=2, validate=False)
        nonspace_samples = generate_negative_samples(r"\S", n=50, seed=2, validate=False)
        word_samples = generate_negative_samples(r"\w", n=50, seed=2, validate=False)
        word_space_samples = generate_negative_samples(r"[\w\s]+?", n=50, seed=2, validate=False)

        self.assertNotIn("١", digit_samples)
        self.assertFalse(any(sample in {"é", "Ω", "中"} for sample in nonspace_samples))
        self.assertFalse(any(sample in {"é", "Ω", "中", "éé", "ΩΩ", "中中"} for sample in word_samples))
        self.assertFalse(any(sample in {"é", "Ω", "中", "éé", "ΩΩ", "中中"} for sample in word_space_samples))

    def test_ascii_word_choices_allow_unicode_as_invalid(self):
        helper = ViolationChoiceHelper(flags=re.ASCII)
        choices = helper.class_violation_choices(set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"))

        self.assertIn("é", choices)

    def test_raw_generation_drops_unchanged_lookaround_scaffold(self):
        samples = generate_negative_samples(r"&(?![a-z]+;|#\d+;)", n=100, seed=0, validate=False)

        self.assertNotIn("&", samples)

    def test_raw_generation_targets_broad_complement_false_positives(self):
        cases = [
            (
                r"[^: \&\.\~]*[a-z0-9]+[^:\&\.\~]+",
                {"01", "0\t\t", "\t0 "},
            ),
            (
                r"\'?\w+([-']\w+)*\'?",
                {"a0'0'", "'0a0'", "é0'0'"},
            ),
            (
                r"Group:\s+Security\s+ID:\s+.*?\\([^ ]+)",
                {"Group:\tSecurity\tID:\t\\\t", "Group:\tSecurity\tID:\t\n\\\t"},
            ),
            (
                r"[\s0-9a-zA-Z\;\&quot;\,\<\>\\?\+\=\)\(\\*\&\%\\$\#\.]*",
                {" ", "aa", "A", "01"},
            ),
            (
                r"\b([^;]+)",
                {"é", "aa", "X\t"},
            ),
        ]

        for pattern, forbidden in cases:
            with self.subTest(pattern=pattern):
                samples = generate_negative_samples(pattern, n=300, seed=2, validate=False)
                matched = [sample for sample in samples if re.fullmatch(pattern, sample)]

                self.assertTrue(samples)
                self.assertFalse(forbidden & set(samples))
                self.assertLessEqual(len(matched), max(1, len(samples) // 100))

    def test_raw_generation_keeps_concrete_violation_tokens_for_broad_classes(self):
        samples = generate_negative_samples(r"\b([^;]+)", n=30, seed=2, validate=False)

        self.assertTrue(samples)
        self.assertTrue(all(";" in sample or sample == "" for sample in samples))

    def test_raw_generation_suppresses_universal_class_false_positives(self):
        cases = [
            (r"\*[\d\D]*?\*", {"*é*", "*Ω*", "*中*"}),
            (r"<(.|\n)*?>", {"<\n>", "<\n\n>"}),
            (r"\[\[(?:\s|.)*?\]\]", {"[[\n]]", "[[\n\n]]"}),
        ]

        for pattern, forbidden in cases:
            with self.subTest(pattern=pattern):
                samples = generate_negative_samples(pattern, n=100, seed=2, validate=False)
                matched = [sample for sample in samples if re.fullmatch(pattern, sample)]

                self.assertTrue(samples)
                self.assertFalse(forbidden & set(samples))
                self.assertEqual([], matched)

    def test_raw_generation_handles_near_universal_alternation(self):
        pattern = r"^(.{4})|([^!@#]*)|[!@#]"
        samples = generate_negative_samples(pattern, n=100, seed=2, validate=False)

        self.assertTrue(samples)
        self.assertNotIn("!", samples)
        self.assertNotIn("#", samples)
        self.assertNotIn("@", samples)
        self.assertNotIn("aaaa", samples)
        self.assertEqual([], [sample for sample in samples if re.fullmatch(pattern, sample)])

    def test_family_selection_includes_relevant_feature_families(self):
        cases = [
            (r"[a-z]+", {"character_class_violation", "group_violation", "wrong_char_substitution"}),
            (r"a{2,4}", {"quantifier_underflow_violation", "quantifier_overflow_violation"}),
            (r"^test$", {"anchor_violation"}),
            (r"(ab|cd)\1", {"backreference_violation", "alternation_violation"}),
            (r"(?P<name>a)?(?(name)yes|no)", {"conditional_violation"}),
            (r"\d+", {"escape_sequence_violation", "numeric_boundary_violation"}),
            (r"\p{L}", {"unicode_violation"}),
            (r"\bword\b", {"zero_width_assertion_violation"}),
            (r"\d{3}-\d{2}", {"separator_boundary_violation", "numeric_boundary_violation"}),
        ]

        for pattern, expected in cases:
            with self.subTest(pattern=pattern):
                families = set(select_violation_families(CompactRegexGraphBuilder(pattern)))
                self.assertTrue(expected <= families)

    def test_family_generators_fill_beyond_single_fault_seed(self):
        samples = generate_negative_samples(r"[ab]\d", n=30, seed=10)

        self.assertEqual(len(samples), 30)
        self.assertEqual(len(samples), len(set(samples)))
        self.assertTrue(any(len(sample) < 2 for sample in samples))
        self.assertTrue(any(len(sample) > 2 for sample in samples))
        self.assert_all_negative(r"[ab]\d", samples)

    def test_scheduler_mixes_families_for_small_budget(self):
        pattern = r"[A-Z]{2}\d{3}"
        samples = generate_negative_samples(pattern, n=20, seed=14)

        self.assertEqual(len(samples), 20)
        self.assertEqual(len(samples), len(set(samples)))
        self.assertTrue(any(re.fullmatch(r"[^A-Z]A000", sample) for sample in samples))
        self.assertTrue(any(re.fullmatch(r"A[^A-Z]000", sample) for sample in samples))
        self.assertTrue(any(re.fullmatch(r"AA[^0-9]00", sample) for sample in samples))
        self.assertTrue(any(0 < len(sample) < 5 for sample in samples))
        self.assertTrue(any(5 < len(sample) < STRESS_LENGTH for sample in samples))
        self.assertTrue(any(sample in {"01", ".5", "1.", "-1"} for sample in samples))
        self.assert_all_negative(pattern, samples)

    def test_stress_boundary_violation_eventually_emits_large_profiles(self):
        pattern = r"[A-Z]{2}\d{3}"
        samples = generate_negative_samples(pattern, n=60, seed=15)
        stress_samples = [sample for sample in samples if len(sample) == STRESS_LENGTH]

        self.assertTrue(any(set(sample) == {"A"} for sample in stress_samples))
        self.assertTrue(any(set(sample) == {"0"} for sample in stress_samples))
        self.assertTrue(any(set(sample) == {"A", "0"} for sample in stress_samples))
        self.assertTrue(any(set(sample) == {"!"} for sample in stress_samples))
        self.assert_all_negative(pattern, stress_samples)

    def test_representative_family_patterns_return_invalid_samples(self):
        patterns = [
            r"[a-z]+",
            r"[A-Z]{2}\d{3}",
            r"^\d{3}-\d{2}$",
            r"(cat|dog)",
            r"^test$",
            r"\bword\b",
            r"(ab|cd)\1",
            r"(?P<name>a)?(?(name)yes|no)",
        ]

        for pattern in patterns:
            with self.subTest(pattern=pattern):
                samples = generate_negative_samples(pattern, n=12, seed=11)
                self.assertTrue(samples)
                self.assertEqual(len(samples), len(set(samples)))
                self.assert_all_negative(pattern, samples)


def string_punctuation_or_space():
    return set(r"""!"#$%&'()*+,-./:;<=>?@[\]^_`{|}~ """)


class NegativeModuleTests(unittest.TestCase):
    def test_choice_helper_class_buckets(self):
        helper = ViolationChoiceHelper()
        choices = helper.class_violation_choices(set("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))

        self.assertIn("0", choices)
        self.assertIn("!", choices)
        self.assertIn("é", choices)

    def test_context_walker_emits_prefix_node_suffix(self):
        builder = CompactRegexGraphBuilder(r"ab[0-9]")
        contexts = list(iter_node_contexts(builder.root))
        triples = [(context.prefix, context.node.kind, context.suffix) for context in contexts]

        self.assertIn(("", "literal", "b0"), triples)
        self.assertIn(("a", "literal", "0"), triples)
        self.assertIn(("ab", "class", ""), triples)

    def test_context_marks_repeat_ancestry_and_wildcards(self):
        cases = [
            (r"a*", "star", False),
            (r"a+", "plus", False),
            (r"a?", "optional", False),
            (r".*", "star", True),
            (r".+", "plus", True),
            (r".?", "optional", True),
            (r".{2}", "exact", True),
        ]

        for pattern, repeat_kind, wildcard in cases:
            with self.subTest(pattern=pattern):
                contexts = list(iter_node_contexts(CompactRegexGraphBuilder(pattern).root))
                inner = next(context for context in contexts if context.node.kind in {"literal", "class"})
                self.assertIn(repeat_kind, inner.repeat_kinds)
                self.assertEqual(inner.is_wildcard_class, wildcard)

    def test_family_emitters_reproduce_single_fault_outputs(self):
        builder = CompactRegexGraphBuilder(r"ab[0-9]")
        helper = ViolationChoiceHelper()
        samples = wrong_char_substitution(builder, helper) + character_class_violation(builder, helper)

        self.assertIn("bb0", samples)
        self.assertIn("aa0", samples)
        self.assertIn("aba", samples)

    def test_separator_mutations_preserve_digit_context(self):
        builder = CompactRegexGraphBuilder(r"^\d{3}-\d{2}$")
        samples = separator_boundary_violation(builder, ViolationChoiceHelper())

        self.assertIn("000:00", samples)
        self.assertTrue(any(re.fullmatch(r"000[^0-9]00", sample) for sample in samples))

    def test_quantifier_violations_use_repeat_context(self):
        builder = CompactRegexGraphBuilder(r"a{2,4}")
        underflow, overflow = quantifier_violations(builder)

        self.assertIn("a", underflow)
        self.assertTrue(any(len(sample) > 4 for sample in overflow))

    def test_quantifier_violations_handle_plus_and_optional(self):
        plus_underflow, plus_overflow = quantifier_violations(CompactRegexGraphBuilder(r"a+"))
        optional_underflow, optional_overflow = quantifier_violations(CompactRegexGraphBuilder(r"a?"))

        self.assertIn("", plus_underflow)
        self.assertEqual([], plus_overflow)
        self.assertEqual([], optional_underflow)
        self.assertIn("aa", optional_overflow)

    def test_ordinary_repeats_keep_local_invalid_substitutions(self):
        helper = ViolationChoiceHelper()

        self.assertTrue(wrong_char_substitution(CompactRegexGraphBuilder(r"a*"), helper))
        self.assertTrue(wrong_char_substitution(CompactRegexGraphBuilder(r"a+"), helper))
        self.assertTrue(wrong_char_substitution(CompactRegexGraphBuilder(r"a?"), helper))
        self.assertTrue(wrong_char_substitution(CompactRegexGraphBuilder(r"[a]*"), helper))
        self.assertTrue(wrong_char_substitution(CompactRegexGraphBuilder(r"[.]*"), helper))

    def test_wildcards_skip_arbitrary_local_substitutions_but_emit_newlines(self):
        helper = ViolationChoiceHelper()

        self.assertEqual([], wrong_char_substitution(CompactRegexGraphBuilder(r".*"), helper))
        self.assertEqual(["\n"], character_class_violation(CompactRegexGraphBuilder(r".*"), helper))
        self.assertEqual(["\n"], character_class_violation(CompactRegexGraphBuilder(r"(.)*"), helper))
        self.assertNotIn("a", character_class_violation(CompactRegexGraphBuilder(r"."), helper))

    def test_dotall_wildcards_do_not_emit_newline_violations(self):
        builder = CompactRegexGraphBuilder(r".", flags=re.DOTALL)

        self.assertEqual([], wildcard_dot_violation(builder))

    def test_bounded_dot_has_length_and_newline_violations(self):
        builder = CompactRegexGraphBuilder(r".{2,4}")
        underflow, overflow = quantifier_violations(builder)
        dot_samples = wildcard_dot_violation(builder)

        self.assertTrue(any(len(sample) == 1 for sample in underflow))
        self.assertTrue(any(len(sample) == 5 for sample in overflow))
        self.assertTrue(any("\n" in sample for sample in dot_samples))

    def test_raw_empty_seed_is_skipped_when_regex_accepts_empty(self):
        samples = generate_negative_samples(r"a*", n=12, seed=3, validate=False)

        self.assertNotIn("", samples)
        self.assertTrue(any(sample in {"b", "0", "!", "é"} for sample in samples))

    def test_case_sensitivity_uses_class_content(self):
        helper = ViolationChoiceHelper()
        del helper
        postal_pattern = (
            r"^[ABCEGHJKLMNPRSTVXYabceghjklmnprstvxy]{1}\d{1}"
            r"[A-Za-z]{1}\d{1}[A-Za-z]{1}\d{1}$"
        )

        self.assertIn("a0", case_sensitivity_violation(CompactRegexGraphBuilder(r"[A-Z]\d")))
        self.assertNotIn("a0", case_sensitivity_violation(CompactRegexGraphBuilder(r"[A-Za-z]\d")))
        self.assertNotIn("a0A0A0", case_sensitivity_violation(CompactRegexGraphBuilder(postal_pattern)))

    def test_quote_pattern_no_longer_gets_local_class_false_negative(self):
        samples = family_candidates(
            CompactRegexGraphBuilder(r"^['\"]*(.*?)['\"]*$"),
            ViolationChoiceHelper(),
        )

        self.assertNotIn('a\t"', samples["character_class_violation"])
        self.assertNotIn('a\t"', samples["escape_sequence_violation"])

    def test_whole_string_families_drop_unchanged_representative(self):
        for valid in ["&", "ab"]:
            with self.subTest(valid=valid):
                self.assertNotIn(valid, basic_boundary_violation(valid))
                self.assertNotIn(valid, structure_violation(valid))
                self.assertNotIn(valid, unicode_violation(valid))
                self.assertNotIn(valid, zero_width_assertion_violation(valid))

        self.assertNotIn("&", conditional_violation(CompactRegexGraphBuilder(r"&(?![a-z]+;|#\d+;)")))


if __name__ == "__main__":
    unittest.main()
