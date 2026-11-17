import string
from dataclasses import dataclass, field
import re

from regex_positive_generator.charset import representative_chars, stable_seed
from regex_positive_generator.experimental.counting import (
    saturating_add,
    saturating_mul,
    saturating_pow,
    validate_compact_repeat_bounds,
)
from regex_positive_generator.parser import MAX_RECURSION_DEPTH, RegexParser
from regex_positive_generator.repeats import effective_repeat_bounds


@dataclass
class CompactNode:
    kind: str
    payload: object = None
    children: list = field(default_factory=list)
    min_rep: int = 0
    max_rep: int = 0


class CompactRegexGraphBuilder(RegexParser):
    def __init__(
        self,
        regex_pattern,
        flags=0,
        validate=True,
        simplified=False,
        seed=0,
        max_recursion_depth=MAX_RECURSION_DEPTH,
    ):
        super().__init__(regex_pattern, flags=flags, max_recursion_depth=max_recursion_depth)
        self.validate = validate
        self.simplified = simplified
        self.seed = seed
        self.requires_match_validation = False
        tree = self.parse()
        self.root = self._build_sequence(tree, depth=0)
        self.nodes = []
        self._collect_nodes(self.root)

    def _collect_nodes(self, node):
        self.nodes.append(node)
        for child in node.children:
            self._collect_nodes(child)

    def _get_category_chars(self, category_code):
        import sre_constants

        if category_code == sre_constants.CATEGORY_DIGIT:
            return set(string.digits), "\\d"
        if category_code == sre_constants.CATEGORY_NOT_DIGIT:
            return set(string.printable) - set(string.digits), "\\D"
        if category_code == sre_constants.CATEGORY_SPACE:
            return set(string.whitespace), "\\s"
        if category_code == sre_constants.CATEGORY_NOT_SPACE:
            return set(string.printable) - set(string.whitespace), "\\S"
        if category_code == sre_constants.CATEGORY_WORD:
            return set(string.ascii_letters + string.digits + "_"), "\\w"
        if category_code == sre_constants.CATEGORY_NOT_WORD:
            return set(string.printable) - set(string.ascii_letters + string.digits + "_"), "\\W"
        return set(), f"CAT_{category_code}"

    def _get_any_chars(self):
        return set(string.printable) - {"\n", "\r"}

    def _get_not_literal_chars(self, excluded_char):
        return set(string.printable) - {excluded_char}

    def _anchor_kind_from_sre(self, anchor_type):
        import sre_constants

        if anchor_type == sre_constants.AT_BEGINNING:
            return "start_line"
        if anchor_type == getattr(sre_constants, "AT_BEGINNING_STRING", object()):
            return "start_string"
        if anchor_type == sre_constants.AT_END:
            return "end_line"
        if anchor_type == getattr(sre_constants, "AT_END_STRING", object()):
            return "end_string"
        if anchor_type in {
            sre_constants.AT_BOUNDARY,
            getattr(sre_constants, "AT_UNI_BOUNDARY", object()),
            getattr(sre_constants, "AT_LOC_BOUNDARY", object()),
        }:
            return "word_boundary"
        if anchor_type in {
            getattr(sre_constants, "AT_BOUNDARY_NOT", object()),
            getattr(sre_constants, "AT_NON_BOUNDARY", object()),
            getattr(sre_constants, "AT_UNI_NON_BOUNDARY", object()),
            getattr(sre_constants, "AT_LOC_NON_BOUNDARY", object()),
        }:
            return "not_word_boundary"
        return "unknown"

    def _tree_leading_requirements(self, items):
        import sre_constants

        sequence = items if isinstance(items, (list, tuple)) else items.data
        if not sequence:
            return [None]
        op, av = sequence[0]
        if op == sre_constants.SUBPATTERN:
            return self._tree_leading_requirements(av[3])
        if op == sre_constants.BRANCH:
            out = []
            for alt in av[1]:
                out.extend(self._tree_leading_requirements(alt))
            return out
        if op == sre_constants.AT:
            return [self._anchor_kind_from_sre(av)]
        return [None]

    def _tree_trailing_requirements(self, items):
        import sre_constants

        sequence = items if isinstance(items, (list, tuple)) else items.data
        if not sequence:
            return [None]
        op, av = sequence[-1]
        if op == sre_constants.SUBPATTERN:
            return self._tree_trailing_requirements(av[3])
        if op == sre_constants.BRANCH:
            out = []
            for alt in av[1]:
                out.extend(self._tree_trailing_requirements(alt))
            return out
        if op == sre_constants.AT:
            return [self._anchor_kind_from_sre(av)]
        return [None]

    def _anchor_requires_repeat_clamp(self, kind):
        if kind in {"start_string", "end_string"}:
            return True
        if kind in {"start_line", "end_line"}:
            return not (self.effective_flags & re.MULTILINE)
        return False

    def _repeat_child_is_anchor_limited(self, sub_tree):
        leading = self._tree_leading_requirements(sub_tree)
        if leading and all(self._anchor_requires_repeat_clamp(kind) for kind in leading):
            return True
        trailing = self._tree_trailing_requirements(sub_tree)
        if trailing and all(self._anchor_requires_repeat_clamp(kind) for kind in trailing):
            return True
        return False

    def _tree_has_start_or_end_anchor(self, items):
        import sre_constants

        sequence = items if isinstance(items, (list, tuple)) else items.data
        for op, av in sequence:
            if op == sre_constants.AT and self._anchor_kind_from_sre(av) in {
                "start_line",
                "start_string",
                "end_line",
                "end_string",
            }:
                return True
            if op == sre_constants.BRANCH:
                if any(self._tree_has_start_or_end_anchor(alt) for alt in av[1]):
                    return True
            elif op == sre_constants.SUBPATTERN:
                if self._tree_has_start_or_end_anchor(av[3]):
                    return True
            elif op in (sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT) or (
                hasattr(sre_constants, "POSSESSIVE_REPEAT") and op == sre_constants.POSSESSIVE_REPEAT
            ):
                if self._tree_has_start_or_end_anchor(av[2]):
                    return True
            elif hasattr(sre_constants, "ATOMIC_GROUP") and op == sre_constants.ATOMIC_GROUP:
                if self._tree_has_start_or_end_anchor(av):
                    return True
        return False

    def _get_in_chars(self, av):
        import sre_constants

        char_set = set()
        negated = False
        for sub_op, sub_av in av:
            if sub_op == sre_constants.LITERAL:
                char_set.add(chr(sub_av))
            elif sub_op == sre_constants.RANGE:
                start, end = sub_av
                char_set.update(chr(codepoint) for codepoint in range(start, end + 1))
            elif sub_op == sre_constants.CATEGORY:
                cat_chars, _label = self._get_category_chars(sub_av)
                char_set.update(cat_chars)
            elif sub_op == sre_constants.NEGATE:
                negated = True
        if negated:
            return set(string.printable) - char_set
        return char_set

    def _build_sequence(self, tree, *, depth):
        items = tree if isinstance(tree, (list, tuple)) else tree.data
        return CompactNode("sequence", children=[self._build_item(op, av, depth=depth) for op, av in items])

    def _char_payload(self, chars, salt):
        if self.simplified:
            chars = representative_chars(chars, seed=stable_seed(self.seed, salt))
        return tuple(sorted(chars))

    def _build_item(self, op, av, *, depth):
        import sre_constants

        if op == sre_constants.FAILURE:
            return CompactNode("impossible")
        if op == sre_constants.LITERAL:
            return CompactNode("literal", payload=chr(av))
        if op == sre_constants.ANY:
            return CompactNode("class", payload=self._char_payload(self._get_any_chars(), ("any", depth)))
        if op == sre_constants.CATEGORY:
            chars, label = self._get_category_chars(av)
            return CompactNode("class", payload=self._char_payload(chars, ("category", label, depth)))
        if op == sre_constants.IN:
            return CompactNode("class", payload=self._char_payload(self._get_in_chars(av), ("in", repr(av), depth)))
        if op == sre_constants.NOT_LITERAL:
            return CompactNode(
                "class",
                payload=self._char_payload(self._get_not_literal_chars(chr(av)), ("not_literal", av, depth)),
            )
        if op == sre_constants.BRANCH:
            return CompactNode("choice", children=[self._build_sequence(alt, depth=depth) for alt in av[1]])
        if op == sre_constants.SUBPATTERN:
            group_num = av[0]
            child = self._build_sequence(av[3], depth=depth)
            return CompactNode("group", payload=group_num, children=[child])
        if op in (sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT) or (
            hasattr(sre_constants, "POSSESSIVE_REPEAT") and op == sre_constants.POSSESSIVE_REPEAT
        ):
            if hasattr(sre_constants, "POSSESSIVE_REPEAT") and op == sre_constants.POSSESSIVE_REPEAT:
                self.requires_match_validation = True
            min_rep, max_rep, _was_unbounded = effective_repeat_bounds(
                av[0],
                av[1],
                sre_constants.MAXREPEAT,
                depth=depth,
            )
            validate_compact_repeat_bounds(min_rep, max_rep)
            if max_rep > 1 and self._repeat_child_is_anchor_limited(av[2]):
                max_rep = 1
            child = self._build_sequence(av[2], depth=depth + 1)
            return CompactNode("repeat", children=[child], min_rep=min_rep, max_rep=max_rep)
        if hasattr(sre_constants, "ATOMIC_GROUP") and op == sre_constants.ATOMIC_GROUP:
            self.requires_match_validation = True
            return CompactNode("passthrough", children=[self._build_sequence(av, depth=depth)])
        if op == sre_constants.AT:
            return CompactNode("anchor", payload=self._anchor_kind_from_sre(av))
        if op == sre_constants.GROUPREF:
            return CompactNode("backref", payload=av)
        if hasattr(sre_constants, "GROUPREF_EXISTS") and op == sre_constants.GROUPREF_EXISTS:
            group_id, yes_tree, no_tree = av
            yes_node = self._build_sequence(yes_tree, depth=depth)
            no_node = self._build_sequence(no_tree, depth=depth) if no_tree else CompactNode("sequence")
            return CompactNode("conditional", payload=group_id, children=[yes_node, no_node])
        if str(op) in {"ASSERT", "ASSERT_NOT"}:
            direction, sub_tree = av
            self.requires_match_validation = True
            payload = {
                "negative": str(op) == "ASSERT_NOT",
                "direction": direction,
                "has_anchor": self._tree_has_start_or_end_anchor(sub_tree),
            }
            child = self._build_sequence(sub_tree, depth=depth)
            return CompactNode("lookaround", payload=payload, children=[child])
        return CompactNode("passthrough")

    def count_paths(self):
        return self._count_node(self.root, {})

    def _count_node(self, node, memo):
        node_id = id(node)
        if node_id in memo:
            return memo[node_id]

        if node.kind == "sequence":
            total = 1
            for child in node.children:
                total = saturating_mul(total, self._count_node(child, memo))
        elif node.kind in {"choice", "conditional"}:
            total = 0
            for child in node.children:
                total = saturating_add(total, self._count_node(child, memo))
        elif node.kind == "repeat":
            child_count = self._count_node(node.children[0], memo)
            total = 0
            for rep in range(node.min_rep, node.max_rep + 1):
                total = saturating_add(total, saturating_pow(child_count, rep))
        elif node.kind == "literal":
            total = 1
        elif node.kind == "class":
            total = len(node.payload)
        elif node.kind in {"group", "passthrough"}:
            total = self._count_node(node.children[0], memo) if node.children else 1
        elif node.kind == "impossible":
            total = 0
        elif node.kind in {"anchor", "lookaround", "backref"}:
            total = 1
        else:
            total = 1

        memo[node_id] = total
        return total

    def generate_samples(self, n=1000, seed=0, validate=True, on_sample=None):
        from regex_positive_generator.experimental.compact_sampler import CompactGraphSampler

        return CompactGraphSampler(self, validate=validate).generate_samples(n=n, seed=seed, on_sample=on_sample)

    def build_simplified_graph(self, seed=0):
        return CompactRegexGraphBuilder(
            self.regex,
            flags=self.flags,
            validate=self.validate,
            simplified=True,
            seed=seed,
            max_recursion_depth=self.max_recursion_depth,
        )
