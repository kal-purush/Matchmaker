import re
import string
import unicodedata
import unittest
import warnings

from regex_positive_generator.experimental import CompactRegexGraphBuilder
from regex_positive_generator.experimental.counting import MAX_COMPACT_COUNT
from regex_positive_generator.parser import RegexParseError


warnings.filterwarnings("ignore", category=DeprecationWarning)


class CompactRepeatTests(unittest.TestCase):
    def test_unbounded_repeat_count_uses_min_plus_extra(self):
        builder = CompactRegexGraphBuilder(r"a{10,}")

        self.assertEqual(builder.count_paths(), 4)

    def test_large_bounded_repeat_preserves_bounds(self):
        builder = CompactRegexGraphBuilder(r"a{0,255}")
        repeat_bounds = [(node.min_rep, node.max_rep) for node in builder.nodes if node.kind == "repeat"]

        self.assertIn((0, 255), repeat_bounds)
        self.assertEqual(builder.count_paths(), 256)

    def test_nested_large_bounded_repeat_counts_with_saturation(self):
        builder = CompactRegexGraphBuilder(r"([ab]{1,255}){0,255}")

        self.assertEqual(builder.count_paths(), MAX_COMPACT_COUNT)

    def test_repeat_above_compact_max_raises(self):
        with self.assertRaises(ValueError):
            CompactRegexGraphBuilder(r"a{0,257}")

    def test_long_word_repeat_generates_valid_samples(self):
        pattern = r"\w{5,255}"
        samples = CompactRegexGraphBuilder(pattern).generate_samples(n=1000, seed=15, validate=True)

        self.assertEqual(len(samples), 1000)
        self.assertTrue(all(re.fullmatch(pattern, sample) for sample in samples))

    def test_bounded_repeat_samples_representative_lengths(self):
        pattern = r"[a-z-]{2,63}"
        samples = CompactRegexGraphBuilder(pattern).generate_samples(n=60, seed=0, validate=True)
        lengths = {len(sample) for sample in samples}

        self.assertIn(2, lengths)
        self.assertIn(63, lengths)
        self.assertTrue(any(2 < length < 63 for length in lengths))
        self.assertTrue(all(re.fullmatch(pattern, sample) for sample in samples))

    def test_nested_repeat_builds_compactly(self):
        builder = CompactRegexGraphBuilder(r"((ab)+)+")

        self.assertLess(len(builder.nodes), 20)
        self.assertGreater(builder.count_paths(), 0)

    def test_large_nested_pattern_builds_compactly(self):
        pattern = r"^(((((true|false):\d+)\s{0,1})+,{0,1})+;{0,1})+\s+\+\-(.+)$"
        builder = CompactRegexGraphBuilder(pattern)

        self.assertLess(len(builder.nodes), 100)
        self.assertGreater(builder.count_paths(), 0)

    def test_generated_samples_match_simple_patterns(self):
        pattern = r"(cat|dog)\d{1,2}"
        samples = CompactRegexGraphBuilder(pattern).generate_samples(n=20, seed=2, validate=True)

        self.assertTrue(samples)
        self.assertTrue(all(re.fullmatch(pattern, sample) for sample in samples))
        self.assertTrue(any(sample.startswith("cat") for sample in samples))
        self.assertTrue(any(sample.startswith("dog") for sample in samples))

    def test_generate_samples_callback_receives_accepted_samples(self):
        emitted = []
        builder = CompactRegexGraphBuilder(r"[ab]{2}")

        samples = builder.generate_samples(n=4, seed=16, validate=True, on_sample=emitted.append)

        self.assertEqual(emitted, samples)
        self.assertEqual(len(samples), 4)
        self.assertTrue(all(re.fullmatch(r"[ab]{2}", sample) for sample in samples))

    def test_anchor_inside_repeat_only_matches_once(self):
        pattern = r"(^\d{1,9})+(,\d{1,9})*$"
        samples = CompactRegexGraphBuilder(pattern).generate_samples(n=50, seed=3, validate=True)

        self.assertTrue(samples)
        self.assertTrue(all(re.fullmatch(pattern, sample) for sample in samples))

    def test_required_start_anchor_repeat_clamps_to_one(self):
        pattern = r"(^\d{1,9})+"
        samples = CompactRegexGraphBuilder(pattern).generate_samples(n=50, seed=9, validate=True)

        self.assertTrue(samples)
        self.assertTrue(all(re.fullmatch(pattern, sample) for sample in samples))
        self.assertTrue(all(1 <= len(sample) <= 9 for sample in samples))

    def test_required_start_anchor_repeat_with_min_two_is_empty(self):
        builder = CompactRegexGraphBuilder(r"(^\d){2}")

        self.assertEqual(builder.count_paths(), 0)
        self.assertEqual(builder.generate_samples(n=10, seed=10, validate=True), [])

    def test_anchor_free_choice_route_is_not_clamped(self):
        pattern = r"(a|^)+"
        builder = CompactRegexGraphBuilder(pattern)
        repeat_bounds = [(node.min_rep, node.max_rep) for node in builder.nodes if node.kind == "repeat"]
        samples = builder.generate_samples(n=10, seed=11, validate=True)

        self.assertIn((1, 4), repeat_bounds)
        self.assertTrue(samples)
        self.assertTrue(all(re.fullmatch(pattern, sample) for sample in samples))
        self.assertTrue(any(len(sample) > 1 for sample in samples))

    def test_anchor_choice_inside_fixed_repeat_remains_generatable(self):
        pattern = r"(?:(?:^|,\s*)\d){3}"
        samples = CompactRegexGraphBuilder(pattern).generate_samples(n=20, seed=12, validate=True)

        self.assertTrue(samples)
        self.assertTrue(all(re.fullmatch(pattern, sample) for sample in samples))

    def test_multiline_line_start_repeat_is_not_clamped(self):
        pattern = r"(?m)(^\d\n)+"
        builder = CompactRegexGraphBuilder(pattern)
        repeat_bounds = [(node.min_rep, node.max_rep) for node in builder.nodes if node.kind == "repeat"]

        self.assertIn((1, 4), repeat_bounds)

    def test_multiline_absolute_start_repeat_is_clamped(self):
        pattern = r"(?m)(\A\d\n)+"
        builder = CompactRegexGraphBuilder(pattern)
        repeat_bounds = [(node.min_rep, node.max_rep) for node in builder.nodes if node.kind == "repeat"]
        samples = builder.generate_samples(n=20, seed=13, validate=True)

        self.assertIn((1, 1), repeat_bounds)
        self.assertTrue(samples)
        self.assertTrue(all(re.fullmatch(pattern, sample) for sample in samples))

    def test_backref_replays_captured_group(self):
        pattern = r"(a)\1"
        samples = CompactRegexGraphBuilder(pattern).generate_samples(n=10, seed=4, validate=True)

        self.assertEqual(set(samples), {"aa"})

    def test_conditional_respects_captured_group(self):
        pattern = r"(a)?b(?(1)c|d)"
        samples = CompactRegexGraphBuilder(pattern).generate_samples(n=50, seed=5, validate=True)

        self.assertTrue(samples)
        self.assertEqual(set(samples), {"abc", "bd"})

    def test_word_boundary_is_position_aware(self):
        pattern = r"foo\Bx"
        samples = CompactRegexGraphBuilder(pattern).generate_samples(n=10, seed=6, validate=True)

        self.assertEqual(set(samples), {"foox"})

    def test_positive_lookahead_injects_satisfying_content(self):
        pattern = r"(?=.*\d)(?=.*[a-z])(?=.*[A-Z])[0-9a-zA-Z]{8,12}"
        samples = CompactRegexGraphBuilder(pattern).generate_samples(n=50, seed=7, validate=True)

        self.assertTrue(samples)
        self.assertTrue(all(re.fullmatch(pattern, sample) for sample in samples))

    def test_positive_lookahead_does_not_consume_extra_text(self):
        pattern = r"(?=.*\d)[a-z\d]{4}"
        samples = CompactRegexGraphBuilder(pattern).generate_samples(n=50, seed=14, validate=True)

        self.assertTrue(samples)
        self.assertTrue(all(re.fullmatch(pattern, sample) for sample in samples))
        self.assertTrue(all(len(sample) == 4 for sample in samples))

    def test_recursion_only_pattern_is_depth_zero_empty(self):
        builder = CompactRegexGraphBuilder(r"(?R)")

        self.assertEqual(builder.count_paths(), 0)
        self.assertEqual(builder.generate_samples(n=10, seed=17, validate=True), [])

    def test_required_recursion_sequence_is_depth_zero_empty(self):
        builder = CompactRegexGraphBuilder(r"a(?R)b")

        self.assertEqual(builder.count_paths(), 0)
        self.assertEqual(builder.generate_samples(n=10, seed=18, validate=True), [])

    def test_recursive_alternative_uses_non_recursive_branch(self):
        pattern = r"(a|(?R))*"
        builder = CompactRegexGraphBuilder(pattern)
        samples = builder.generate_samples(n=10, seed=19, validate=False)

        self.assertTrue(samples)
        self.assertTrue(all(set(sample) <= {"a"} for sample in samples))

    def test_recursive_function_call_uses_depth_zero_language(self):
        pattern = r"\w+?\s\w+?\(([\w\s=]+,*|[\w\s=]+|(?R))*\);"
        samples = CompactRegexGraphBuilder(pattern).generate_samples(n=50, seed=20, validate=False)

        self.assertTrue(samples)
        self.assertTrue(all(sample.count("(") == 1 for sample in samples))
        self.assertTrue(all(sample.endswith(");") for sample in samples))
        self.assertTrue(all("~" not in sample for sample in samples))
    
    def test_complex_pattern(self):
        pattern = r"\w{5,255}"
        samples = CompactRegexGraphBuilder(pattern).generate_samples(n=1000, seed=15, validate=True)
        self.assertTrue(samples)
        self.assertTrue(all(re.fullmatch(pattern, sample) for sample in samples))

    def test_posix_class_matches_real_engine_semantics(self):
        try:
            import regex
        except ImportError:
            self.skipTest("regex module is not installed")

        pattern = r"[^[:space:]]+"
        samples = CompactRegexGraphBuilder(pattern).generate_samples(n=20, seed=8, validate=True)

        self.assertTrue(samples)
        self.assertTrue(all(regex.fullmatch(pattern, sample) for sample in samples))

    def test_unicode_property_inside_class_does_not_leak_syntax(self):
        pattern = r"([1-9\p{L}]{1}[0-9\p{L}]{0,3})"
        samples = CompactRegexGraphBuilder(pattern).generate_samples(n=50, seed=21, validate=False)

        self.assertTrue(samples)
        self.assertTrue(all("]" not in sample and "[" not in sample for sample in samples))
        self.assertTrue(all(1 <= len(sample) <= 4 for sample in samples))

    def test_negated_unicode_property_class_generates_single_nonexcluded_chars(self):
        pattern = r"[^\p{L} 0-9]"
        samples = CompactRegexGraphBuilder(pattern).generate_samples(n=30, seed=22, validate=False)

        self.assertTrue(samples)
        self.assertTrue(all(len(sample) == 1 for sample in samples))
        self.assertTrue(all(not sample.isalpha() for sample in samples))
        self.assertTrue(all(not sample.isdigit() for sample in samples))
        self.assertNotIn(" ", samples)

    def test_unicode_property_intersection_excludes_ascii_letters_and_syntax(self):
        pattern = r"[\p{L}&&[^a-zA-Z]]"
        samples = CompactRegexGraphBuilder(pattern).generate_samples(n=30, seed=23, validate=False)

        self.assertTrue(samples)
        self.assertTrue(all(len(sample) == 1 for sample in samples))
        self.assertTrue(all(sample not in string.ascii_letters for sample in samples))
        self.assertTrue(all(sample not in {"&", "[", "]"} for sample in samples))
        self.assertTrue(all(unicodedata.category(sample).startswith("L") for sample in samples))

    def test_short_unicode_property_and_decimal_property_inside_class(self):
        pattern = r"[\pL\p{Nd}_]"
        samples = CompactRegexGraphBuilder(pattern).generate_samples(n=30, seed=24, validate=False)

        self.assertTrue(samples)
        self.assertTrue(all(len(sample) == 1 for sample in samples))
        self.assertTrue(all("]" not in sample and "[" not in sample for sample in samples))
        self.assertTrue(
            all(
                sample == "_" or unicodedata.category(sample).startswith(("L", "Nd"))
                for sample in samples
            )
        )

    def test_large_unicode_class_without_intersection_builds_quickly(self):
        pattern = (
            r"[a-zA-Z\u0410-\u042F\u0430-\u044F\u0401\u0451"
            r"\u0101\u0100\u010c\u010d\u0112\u0113\u011E\u011F"
            r"\u012A\u012B\u0136\u0137\u013b\u013C\u0145\u0146"
            r"\u0160\u0161\u016A\u016B\u017D\u017E]$"
        )
        samples = CompactRegexGraphBuilder(pattern).generate_samples(n=20, seed=25, validate=True)

        self.assertTrue(samples)
        self.assertTrue(all(re.fullmatch(pattern, sample) for sample in samples))

    def test_unsupported_class_intersection_raises_fast(self):
        with self.assertRaises(RegexParseError):
            CompactRegexGraphBuilder(r"[a-z&&b-z]")


if __name__ == "__main__":
    unittest.main()
