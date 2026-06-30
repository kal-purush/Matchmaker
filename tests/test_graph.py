import unittest
import warnings

from regex_positive_generator import RegexGraphBuilder


warnings.filterwarnings("ignore", category=DeprecationWarning)


class RegexPositiveGeneratorGraphTests(unittest.TestCase):
    def assert_no_unknown_nodes(self, builder):
        unknown = [node.label for node in builder.nodes if node.kind == "unknown"]
        self.assertEqual(unknown, [])

    def test_alpha_range_builds_class_node(self):
        builder = RegexGraphBuilder(r"[A-Z]")

        self.assert_no_unknown_nodes(builder)
        classes = [node for node in builder.nodes if node.kind == "class"]
        self.assertEqual(len(classes), 1)
        self.assertEqual(classes[0].char_count, 26)

    def test_class_payload_is_ordered_tuple(self):
        builder = RegexGraphBuilder(r"[A-C]")

        classes = [node for node in builder.nodes if node.kind == "class"]
        self.assertEqual(len(classes), 1)
        self.assertEqual(classes[0].payload, ("A", "B", "C"))

    def test_digit_repeat_builds_loop_and_digit_class(self):
        builder = RegexGraphBuilder(r"\d{2}")

        self.assert_no_unknown_nodes(builder)
        self.assertTrue(any(node.kind == "split" for node in builder.nodes))
        self.assertGreaterEqual(
            len([node for node in builder.nodes if node.kind == "class" and node.char_count == 10]),
            2,
        )

    def test_alternation_builds_split(self):
        builder = RegexGraphBuilder(r"(cat|dog)")

        self.assert_no_unknown_nodes(builder)
        self.assertTrue(any(node.kind == "split" for node in builder.nodes))

    def test_anchors_build_anchor_nodes(self):
        builder = RegexGraphBuilder(r"^cat$")

        self.assert_no_unknown_nodes(builder)
        payloads = {node.payload for node in builder.nodes if node.kind == "anchor"}
        self.assertEqual(payloads, {"start", "end"})

    def test_conditional_builds_conditional_nodes(self):
        builder = RegexGraphBuilder(r"(a)?(?(1)yes|no)")

        self.assert_no_unknown_nodes(builder)
        self.assertTrue(any(node.kind == "conditional" for node in builder.nodes))
        self.assertTrue(any(node.kind == "cond_yes" for node in builder.nodes))
        self.assertTrue(any(node.kind == "cond_no" for node in builder.nodes))

    def test_lookahead_builds_lookaround_node(self):
        builder = RegexGraphBuilder(r"foo(?=bar)")

        self.assert_no_unknown_nodes(builder)
        self.assertTrue(any(node.kind == "lookaround" for node in builder.nodes))

    def test_unicode_property_is_expanded(self):
        builder = RegexGraphBuilder(r"\p{Greek}{2}")

        self.assert_no_unknown_nodes(builder)
        self.assertIn(r"\u0370", builder.expanded_regex)
        self.assertTrue(any(node.kind == "class" for node in builder.nodes))

    def test_short_unicode_property_is_expanded(self):
        builder = RegexGraphBuilder(r"((\w+):(\w+\pL.))+\s?")

        self.assert_no_unknown_nodes(builder)
        self.assertNotIn(r"\pL", builder.expanded_regex)
        self.assertTrue(any(node.kind == "class" for node in builder.nodes))

    def test_recursion_reference_normalizes_to_dead_branch(self):
        builder = RegexGraphBuilder(r"(?R)")

        self.assert_no_unknown_nodes(builder)
        self.assertEqual(builder.expanded_regex, "(?!)")
        self.assertEqual(builder.count_paths()[0], 0)


if __name__ == "__main__":
    unittest.main()
