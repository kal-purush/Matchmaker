import re
import unittest
import warnings

from regex_positive_generator import RegexGraphBuilder, generate_positive_samples


warnings.filterwarnings("ignore", category=DeprecationWarning)


class GraphSamplerTests(unittest.TestCase):
    def test_alpha_range_under_simplified_count(self):
        samples = RegexGraphBuilder(r"[A-Z]").generate_samples(n=2, seed=0)

        self.assertEqual(len(samples), 2)
        self.assertTrue(all(re.fullmatch(r"[A-Z]", sample) for sample in samples))

    def test_alpha_range_falls_back_to_main_graph(self):
        samples = RegexGraphBuilder(r"[A-Z]").generate_samples(n=5, seed=0)

        self.assertEqual(len(samples), 5)
        self.assertEqual(len(set(samples)), 5)
        self.assertTrue(all(re.fullmatch(r"[A-Z]", sample) for sample in samples))

    def test_alpha_range_repeat_satisfied_by_simplified_graph(self):
        samples = RegexGraphBuilder(r"[A-Z]{2}").generate_samples(n=9, seed=1)

        self.assertEqual(len(samples), 9)
        self.assertEqual(len(set(samples)), 9)
        self.assertTrue(all(re.fullmatch(r"[A-Z]{2}", sample) for sample in samples))

    def test_alternation_digit_repeat_valid_and_covers_branches(self):
        samples = RegexGraphBuilder(r"(cat|dog)\d{1,2}").generate_samples(n=30, seed=2)

        self.assertTrue(samples)
        self.assertTrue(all(re.fullmatch(r"(cat|dog)\d{1,2}", sample) for sample in samples))
        self.assertTrue(any(sample.startswith("cat") for sample in samples))
        self.assertTrue(any(sample.startswith("dog") for sample in samples))

    def test_tiny_space_returns_available_unique_samples(self):
        samples = RegexGraphBuilder(r"(a|b)").generate_samples(n=10, seed=3)

        self.assertEqual(set(samples), {"a", "b"})

    def test_same_seed_is_stable(self):
        first = RegexGraphBuilder(r"[A-Z]{2}\d").generate_samples(n=20, seed=4)
        second = RegexGraphBuilder(r"[A-Z]{2}\d").generate_samples(n=20, seed=4)

        self.assertEqual(first, second)

    def test_package_function(self):
        samples = generate_positive_samples(r"[ab]\d", n=4, seed=5)

        self.assertEqual(len(samples), 4)
        self.assertTrue(all(re.fullmatch(r"[ab]\d", sample) for sample in samples))


if __name__ == "__main__":
    unittest.main()
