import unittest
import warnings

from regex_positive_generator import RegexGraphBuilder


warnings.filterwarnings("ignore", category=DeprecationWarning)


class RegexSpaceCounterTests(unittest.TestCase):
    def test_alpha_range_counts(self):
        builder = RegexGraphBuilder(r"[A-Z]")

        self.assertEqual(builder.count_main_space().total, 26)
        self.assertEqual(builder.count_simplified_space(seed=0).total, 3)

    def test_alpha_range_repeat_counts(self):
        builder = RegexGraphBuilder(r"[A-Z]{2}")

        self.assertEqual(builder.count_main_space().total, 676)
        self.assertEqual(builder.count_simplified_space(seed=0).total, 9)

    def test_alternation_digit_repeat_count(self):
        builder = RegexGraphBuilder(r"(cat|dog)\d{1,2}")

        self.assertEqual(builder.count_main_space().total, 220)

    def test_tiny_class_keeps_full_count(self):
        builder = RegexGraphBuilder(r"[ab]")

        self.assertEqual(builder.count_main_space().total, 2)
        self.assertEqual(builder.count_simplified_space(seed=0).total, 2)

    def test_backreference_count_is_inexact(self):
        builder = RegexGraphBuilder(r"(a|b)\1")
        result = builder.count_main_space()

        self.assertFalse(result.exact)
        self.assertTrue(result.has_backref)

    def test_unbounded_repeat_count_uses_min_plus_extra(self):
        builder = RegexGraphBuilder(r"a{10,}")
        result = builder.count_main_space()

        self.assertEqual(result.total, 4)
        self.assertFalse(result.exact)

    def test_large_minimum_repeat_raises(self):
        with self.assertRaises(ValueError):
            RegexGraphBuilder(r"a{257,}")

    def test_backref_minimum_repeat_builds(self):
        builder = RegexGraphBuilder(r"^(.)\1{10,}$")

        self.assertTrue(builder.nodes)
        self.assertGreater(builder.count_paths()[0], 0)
        self.assertTrue(any(node.kind == "backref" for node in builder.nodes))

    def test_dead_recursion_branch_counts_as_zero(self):
        builder = RegexGraphBuilder(r"(?R)")

        self.assertEqual(builder.count_main_space().total, 0)
        self.assertEqual(builder.count_simplified_space(seed=0).total, 0)

    def test_recursive_alternative_counts_non_recursive_route(self):
        builder = RegexGraphBuilder(r"(a|(?R))*")

        self.assertEqual(builder.count_main_space().total, 4)


class SimplifiedGraphTests(unittest.TestCase):
    def class_payload(self, builder):
        classes = [node.payload for node in builder.nodes if node.kind == "class"]
        self.assertEqual(len(classes), 1)
        return set(classes[0])

    def test_alpha_range_simplified_graph_has_three_chars(self):
        builder = RegexGraphBuilder(r"[A-Z]")
        simplified = builder.build_simplified_graph(seed=0)
        chars = self.class_payload(simplified)

        self.assertEqual(len(chars), 3)
        self.assertIn("A", chars)
        self.assertIn("Z", chars)

    def test_same_seed_is_stable(self):
        first = RegexGraphBuilder(r"[A-Z]").build_simplified_graph(seed=123)
        second = RegexGraphBuilder(r"[A-Z]").build_simplified_graph(seed=123)

        self.assertEqual(self.class_payload(first), self.class_payload(second))

    def test_different_seed_preserves_endpoints(self):
        first = RegexGraphBuilder(r"[A-Z]").build_simplified_graph(seed=1)
        second = RegexGraphBuilder(r"[A-Z]").build_simplified_graph(seed=2)

        for chars in (self.class_payload(first), self.class_payload(second)):
            self.assertEqual(len(chars), 3)
            self.assertIn("A", chars)
            self.assertIn("Z", chars)


if __name__ == "__main__":
    unittest.main()
