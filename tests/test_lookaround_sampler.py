import re
import unittest

from regex_positive_generator.experimental import CompactRegexGraphBuilder
from regex_positive_generator.experimental.compact_sampler_lookaround_experimental import (
    ExperimentalLookaroundCompactGraphSampler,
)


class ExperimentalLookaroundSamplerTests(unittest.TestCase):
    def generate(self, pattern, n=100, seed=0):
        builder = CompactRegexGraphBuilder(pattern)
        return ExperimentalLookaroundCompactGraphSampler(builder, validate=False).generate_samples(n=n, seed=seed)

    def test_negative_prefix_lookahead_avoids_forbidden_prefixes(self):
        pattern = r"^(?!\d[1]{2}|[5]{3})([2-9]\d{2})([. -]*)\d{4}$"
        samples = self.generate(pattern, n=100, seed=2)
        forbidden_prefixes = {"211", "311", "411", "511", "555", "611", "711", "811", "911"}

        self.assertTrue(samples)
        self.assertTrue(all(not any(sample.startswith(prefix) for prefix in forbidden_prefixes) for sample in samples))
        self.assertTrue(all(re.fullmatch(pattern, sample) for sample in samples))

    def test_positive_contains_digit_lookahead_constructs_digit_samples(self):
        pattern = r"^(?=.*\d)\w+$"
        samples = self.generate(pattern, n=100, seed=3)

        self.assertTrue(samples)
        self.assertTrue(all(any(ch.isdigit() for ch in sample) for sample in samples))
        self.assertTrue(all(re.fullmatch(pattern, sample) for sample in samples))

    def test_multiple_positive_contains_lookaheads_construct_password_samples(self):
        pattern = r"^(?=.*[!@#$%^&*()\-_=+`~\[\]{}?|])(?=.*[a-z])(?=.*[A-Z])(?=.*[0-9]).{8,20}$"
        special = set("!@#$%^&*()-_=+`~[]{}?|")
        samples = self.generate(pattern, n=100, seed=4)

        self.assertTrue(samples)
        self.assertTrue(all(any(ch in special for ch in sample) for sample in samples))
        self.assertTrue(all(any(ch.islower() for ch in sample) for sample in samples))
        self.assertTrue(all(any(ch.isupper() for ch in sample) for sample in samples))
        self.assertTrue(all(any(ch.isdigit() for ch in sample) for sample in samples))
        self.assertTrue(all(re.fullmatch(pattern, sample) for sample in samples))

    def test_positive_fixed_lookbehind_constructs_matching_suffix(self):
        pattern = r"^[ab]+(?<=a)$"
        samples = self.generate(pattern, n=100, seed=5)

        self.assertTrue(samples)
        self.assertTrue(all(sample.endswith("a") for sample in samples))
        self.assertTrue(all(re.fullmatch(pattern, sample) for sample in samples))

    def test_negative_fixed_lookbehind_rejects_forbidden_suffix(self):
        pattern = r"^.+(?<!\.)$"
        samples = self.generate(pattern, n=100, seed=6)

        self.assertTrue(samples)
        self.assertTrue(all(not sample.endswith(".") for sample in samples))
        self.assertTrue(all(re.fullmatch(pattern, sample) for sample in samples))

    def test_leading_positive_lookbehind_needs_external_context(self):
        pattern = r"(?<=\\)[a-z]+"
        samples = self.generate(pattern, n=100, seed=7)

        self.assertEqual([], samples)

    def test_reports_unsupported_complex_lookaround(self):
        pattern = r"(?!.*([abcde]).*\1)^[abcde]{5}$"
        builder = CompactRegexGraphBuilder(pattern)
        sampler = ExperimentalLookaroundCompactGraphSampler(builder, validate=False)

        self.assertIn("negative_lookahead", sampler.lookaround_report()["unsupported_lookarounds"])


if __name__ == "__main__":
    unittest.main()
