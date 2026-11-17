import string

from .model import Node
from .parser import MAX_RECURSION_DEPTH, RegexParser
from .repeats import effective_repeat_bounds


MAX_LOOP_UNROLL = 3
AFTER_END_ANCHOR_KEY = "__after_end_anchor__"
NEXT_WORD_EXPECTATION_KEY = "__next_word_expectation__"


class RegexGraphBuilder(RegexParser):
    MAX_SAFE_NODES = 1000

    def __init__(self, regex_pattern, flags=0, validate=True, max_recursion_depth=MAX_RECURSION_DEPTH):
        super().__init__(regex_pattern, flags=flags, max_recursion_depth=max_recursion_depth)
        self.validate = validate
        self.start_node = Node("START", kind="start")
        self.end_node = Node("END", kind="end")
        self.nodes = []
        self.requires_match_validation = False

        tree = self.parse()
        last = self._build(tree, self.start_node)
        for lead in last:
            lead.connect(self.end_node)

        self._assign_ids()
        if len(self.nodes) > self.MAX_SAFE_NODES:
            raise ValueError(
                f"Pattern too complex: graph has {len(self.nodes)} nodes "
                f"(limit {self.MAX_SAFE_NODES}). Simplify the pattern or reduce MAX_LOOP_UNROLL."
            )

    def _assign_ids(self):
        visited = set()
        stack = [self.start_node]
        counter = 0
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            node.id = str(counter)
            counter += 1
            stack.extend(node.next)
            self.nodes.append(node)

    def _get_category_chars(self, category_code):
        import sre_constants

        if category_code == sre_constants.CATEGORY_DIGIT:
            return set(string.digits), 10, "\\d"
        if category_code == sre_constants.CATEGORY_NOT_DIGIT:
            all_printable = set(string.printable)
            return all_printable - set(string.digits), 90, "\\D"
        if category_code == sre_constants.CATEGORY_SPACE:
            return set(string.whitespace), 6, "\\s"
        if category_code == sre_constants.CATEGORY_NOT_SPACE:
            all_printable = set(string.printable)
            return all_printable - set(string.whitespace), 94, "\\S"
        if category_code == sre_constants.CATEGORY_WORD:
            word_chars = set(string.ascii_letters + string.digits + "_")
            return word_chars, 63, "\\w"
        if category_code == sre_constants.CATEGORY_NOT_WORD:
            all_printable = set(string.printable)
            word_chars = set(string.ascii_letters + string.digits + "_")
            return all_printable - word_chars, 37, "\\W"
        return set(), 0, f"CAT_{category_code}"

    def _get_any_chars(self):
        return set(string.printable) - {"\n", "\r"}

    def _get_not_literal_chars(self, excluded_char):
        return set(string.printable) - {excluded_char}

    def _get_in_chars(self, av):
        import sre_constants

        char_set = set()
        negated = False
        for sub_op, sub_av in av:
            if sub_op == sre_constants.LITERAL:
                char_set.add(chr(sub_av))
            elif sub_op == sre_constants.RANGE:
                start, end = sub_av
                char_set.update(chr(c) for c in range(start, end + 1))
            elif sub_op == sre_constants.CATEGORY:
                cat_chars, _, _ = self._get_category_chars(sub_av)
                char_set.update(cat_chars)
            elif sub_op == sre_constants.NEGATE:
                negated = True
        if negated:
            return set(string.printable) - char_set, True
        return char_set, False

    def _ordered_chars(self, char_set):
        return tuple(sorted(char_set))

    def _is_word_char(self, ch):
        return bool(ch) and (ch.isalnum() or ch == "_")

    def _node_can_consume(self, node):
        return node.kind in {"match", "class", "any", "backref"}

    def _next_can_consume(self, node, seen=None):
        if node is None:
            return False
        if seen is None:
            seen = set()
        if node in seen:
            return False
        seen.add(node)
        if node.kind == "end":
            return False
        if self._node_can_consume(node):
            return True
        return any(self._next_can_consume(nxt, seen) for nxt in node.next)

    def _next_can_finish_without_consuming(self, node, seen=None):
        if node is None:
            return False
        if seen is None:
            seen = set()
        if node in seen:
            return False
        seen.add(node)
        if node.kind == "end":
            return True
        if self._node_can_consume(node):
            return False
        return any(self._next_can_finish_without_consuming(nxt, seen) for nxt in node.next)

    def _first_wordness_options(self, node, seen=None):
        if node is None:
            return {False}
        if seen is None:
            seen = set()
        if node in seen:
            return set()
        seen.add(node)
        if node.kind == "end":
            return {False}
        if node.kind == "match":
            return {self._is_word_char(node.payload[:1])}
        if node.kind in {"class", "any"}:
            chars = node.payload or set()
            options = set()
            if any(self._is_word_char(ch) for ch in chars):
                options.add(True)
            if any(not self._is_word_char(ch) for ch in chars):
                options.add(False)
            return options or {False}
        if node.kind == "backref":
            return {True, False}

        options = set()
        for nxt in node.next:
            options.update(self._first_wordness_options(nxt, seen))
        return options or {False}

    def _anchor_allows(self, anchor_node, current_string, next_node=None):
        payload = anchor_node.payload
        if payload in {"start", "beginning", "beginning_string"}:
            return len(current_string) == 0
        if payload in {"end", "end_string"}:
            return self._next_can_finish_without_consuming(next_node)
        if payload == "word_boundary":
            previous_is_word = self._is_word_char(current_string[-1:]) if current_string else False
            return any(previous_is_word != next_is_word for next_is_word in self._first_wordness_options(next_node))
        if payload == "not_word_boundary":
            previous_is_word = self._is_word_char(current_string[-1:]) if current_string else False
            return any(previous_is_word == next_is_word for next_is_word in self._first_wordness_options(next_node))
        return True

    def _anchor_payload_from_sre(self, anchor_type):
        import sre_constants

        if anchor_type in {
            sre_constants.AT_BEGINNING,
            getattr(sre_constants, "AT_BEGINNING_STRING", object()),
        }:
            return "start"
        if anchor_type in {
            sre_constants.AT_END,
            getattr(sre_constants, "AT_END_STRING", object()),
        }:
            return "end"
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
        return anchor_type

    def _tree_sequence_can_consume(self, items):
        import sre_constants

        sequence = items if isinstance(items, (list, tuple)) else items.data
        for op, av in sequence:
            if op in {
                sre_constants.LITERAL,
                sre_constants.ANY,
                sre_constants.CATEGORY,
                sre_constants.IN,
                sre_constants.NOT_LITERAL,
                sre_constants.GROUPREF,
            }:
                return True
            if op == sre_constants.BRANCH:
                return any(self._tree_sequence_can_consume(alt) for alt in av[1])
            if op == sre_constants.SUBPATTERN and self._tree_sequence_can_consume(av[3]):
                return True
            if op in (sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT) or (
                hasattr(sre_constants, "POSSESSIVE_REPEAT") and op == sre_constants.POSSESSIVE_REPEAT
            ):
                min_rep, sub_tree = av[0], av[2]
                if min_rep > 0 and self._tree_sequence_can_consume(sub_tree):
                    return True
            if hasattr(sre_constants, "ATOMIC_GROUP") and op == sre_constants.ATOMIC_GROUP:
                if self._tree_sequence_can_consume(av):
                    return True
        return False

    def _tree_has_start_or_end_anchor(self, items):
        import sre_constants

        sequence = items if isinstance(items, (list, tuple)) else items.data
        for op, av in sequence:
            if op == sre_constants.AT and av in {
                sre_constants.AT_BEGINNING,
                sre_constants.AT_END,
                getattr(sre_constants, "AT_BEGINNING_STRING", object()),
                getattr(sre_constants, "AT_END_STRING", object()),
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

    def _lookaround_payload_from_sre(self, op, av):
        direction, sub_tree = av
        return {
            "negative": str(op) == "ASSERT_NOT",
            "direction": direction,
            "tree": sub_tree,
        }

    def _build_repeat(self, current_leads, min_rep, max_rep, sub_tree, label_prefix, depth):
        import sre_constants

        min_rep, max_rep, _was_unbounded = effective_repeat_bounds(
            min_rep,
            max_rep,
            sre_constants.MAXREPEAT,
            depth=depth,
        )

        split = Node(f"{label_prefix} {min_rep}-{max_rep}", kind="split")
        for lead in current_leads:
            lead.connect(split)

        exit_leads = []
        chain = [split]
        if min_rep == 0:
            exit_leads.append(split)

        for i in range(1, max_rep + 1):
            chain = self._build(sub_tree, chain, depth + 1)
            if i >= min_rep:
                exit_leads.extend(chain)

        return exit_leads

    def _build(self, tree, start_node, depth=0):
        import sre_constants

        current_leads = [start_node] if not isinstance(start_node, list) else start_node
        items = tree if isinstance(tree, (list, tuple)) else tree.data

        for op, av in items:
            new_leads = []

            if op == sre_constants.FAILURE:
                new_leads = []

            elif op == sre_constants.LITERAL:
                node = Node(f"'{chr(av)}'", kind="match", payload=chr(av))
                node.char_count = 1
                for lead in current_leads:
                    lead.connect(node)
                new_leads = [node]

            elif op == sre_constants.ANY:
                chars = self._ordered_chars(self._get_any_chars())
                node = Node(".", kind="class", payload=chars)
                node.char_count = len(chars)
                for lead in current_leads:
                    lead.connect(node)
                new_leads = [node]

            elif op == sre_constants.CATEGORY:
                char_set, _count, label = self._get_category_chars(av)
                chars = self._ordered_chars(char_set)
                node = Node(label, kind="class", payload=chars)
                node.char_count = len(chars)
                for lead in current_leads:
                    lead.connect(node)
                new_leads = [node]

            elif op == sre_constants.BRANCH:
                split = Node("Split", kind="split")
                for lead in current_leads:
                    lead.connect(split)
                for alt_path in av[1]:
                    ends = self._build(alt_path, [split], depth)
                    new_leads.extend(ends)

            elif op == sre_constants.SUBPATTERN:
                group_num = av[0]
                sub_tree = av[3]
                if group_num is None:
                    new_leads = self._build(sub_tree, current_leads, depth)
                else:
                    cap_start = Node(f"Start G{group_num}", kind="group_start", payload=group_num)
                    for lead in current_leads:
                        lead.connect(cap_start)
                    inner_ends = self._build(sub_tree, [cap_start], depth)
                    cap_end = Node(f"End G{group_num}", kind="group_end", payload=group_num)
                    for end in inner_ends:
                        end.connect(cap_end)
                    new_leads = [cap_end]

            elif hasattr(sre_constants, "GROUPREF_EXISTS") and op == sre_constants.GROUPREF_EXISTS:
                group_id, yes_tree, no_tree = av
                cond = Node(f"Cond G{group_id}", kind="conditional", payload=group_id)
                for lead in current_leads:
                    lead.connect(cond)

                yes_gate = Node("YES", kind="cond_yes", payload=group_id)
                no_gate = Node("NO", kind="cond_no", payload=group_id)
                cond.connect(yes_gate)
                cond.connect(no_gate)

                new_leads.extend(self._build(yes_tree, [yes_gate], depth))
                if no_tree:
                    new_leads.extend(self._build(no_tree, [no_gate], depth))
                else:
                    new_leads.append(no_gate)

            elif op == sre_constants.MAX_REPEAT:
                new_leads = self._build_repeat(current_leads, av[0], av[1], av[2], "Loop", depth)

            elif op == sre_constants.MIN_REPEAT:
                new_leads = self._build_repeat(current_leads, av[0], av[1], av[2], "Loop?", depth)

            elif hasattr(sre_constants, "POSSESSIVE_REPEAT") and op == sre_constants.POSSESSIVE_REPEAT:
                self.requires_match_validation = True
                new_leads = self._build_repeat(current_leads, av[0], av[1], av[2], "Loop+", depth)

            elif hasattr(sre_constants, "ATOMIC_GROUP") and op == sre_constants.ATOMIC_GROUP:
                self.requires_match_validation = True
                atomic_start = Node("Atomic", kind="atomic")
                for lead in current_leads:
                    lead.connect(atomic_start)
                new_leads = self._build(av, [atomic_start], depth)

            elif str(op) in {"ASSERT", "ASSERT_NOT"}:
                payload = self._lookaround_payload_from_sre(op, av)
                direction_label = "ahead" if payload["direction"] >= 0 else "behind"
                sign_label = "not " if payload["negative"] else ""
                node = Node(f"Assert {sign_label}{direction_label}", kind="lookaround", payload=payload)
                node.char_count = 0
                for lead in current_leads:
                    lead.connect(node)
                new_leads = [node]

            elif op == sre_constants.GROUPREF:
                node = Node(f"\\{av}", kind="backref", payload=av)
                for lead in current_leads:
                    lead.connect(node)
                new_leads = [node]

            elif op == sre_constants.IN:
                char_set, negated = self._get_in_chars(av)
                if negated:
                    label = f"[^...] ({len(char_set)})"
                else:
                    label = f"[...] ({len(char_set)})"
                chars = self._ordered_chars(char_set)
                node = Node(label, kind="class", payload=chars)
                node.char_count = len(chars)
                for lead in current_leads:
                    lead.connect(node)
                new_leads = [node]

            elif op == sre_constants.AT:
                payload = self._anchor_payload_from_sre(av)
                if payload == "start":
                    node = Node("^", kind="anchor", payload="start")
                elif payload == "end":
                    node = Node("$", kind="anchor", payload="end")
                elif payload == "word_boundary":
                    node = Node("\\b", kind="anchor", payload="word_boundary")
                elif payload == "not_word_boundary":
                    node = Node("\\B", kind="anchor", payload="not_word_boundary")
                else:
                    node = Node(f"Anchor({av})", kind="anchor", payload=av)
                node.char_count = 0
                for lead in current_leads:
                    lead.connect(node)
                new_leads = [node]

            elif op == sre_constants.NOT_LITERAL:
                excluded_char = chr(av)
                chars = self._ordered_chars(self._get_not_literal_chars(excluded_char))
                node = Node(f"[^{excluded_char}]", kind="class", payload=chars)
                node.char_count = len(chars)
                for lead in current_leads:
                    lead.connect(node)
                new_leads = [node]

            else:
                node = Node(f"?({op})", kind="unknown", payload=av)
                for lead in current_leads:
                    lead.connect(node)
                new_leads = [node]

            current_leads = new_leads

        return current_leads

    def count_paths(self):
        memo = {}

        def count_from_node(node, has_backref=False):
            if node.kind == "end":
                return 1
            if not has_backref and node in memo:
                return memo[node]

            total = 0
            next_has_backref = has_backref or node.kind == "backref"
            for next_node in node.next:
                if node.kind in {"match", "class", "any"}:
                    total += node.char_count * count_from_node(next_node, next_has_backref)
                else:
                    total += count_from_node(next_node, next_has_backref)

            if not next_has_backref:
                memo[node] = total
            return total

        return count_from_node(self.start_node), any(node.kind == "backref" for node in self.nodes)

    def count_main_space(self):
        from .counter import RegexSpaceCounter

        return RegexSpaceCounter(self).count_main()

    def count_simplified_space(self, seed=0):
        from .counter import RegexSpaceCounter

        return RegexSpaceCounter(self, seed=seed).count_simplified()

    def build_simplified_graph(self, seed=0):
        from .simplifier import SimplifiedRegexGraphBuilder

        return SimplifiedRegexGraphBuilder(self.regex, flags=self.flags, validate=self.validate, seed=seed)

    def generate_samples(self, n=1000, seed=0, validate=True):
        from .sampler import GraphSampler

        return GraphSampler(self, validate=validate).generate_samples(n=n, seed=seed)
