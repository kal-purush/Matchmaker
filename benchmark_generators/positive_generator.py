import os
import random
import re
import signal
import string
import time
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

try:
    import regex
    REGEX_AVAILABLE = True
except ImportError:
    REGEX_AVAILABLE = False

# --- Configuration ---
MAX_LOOP_UNROLL = 3  # How many times to unroll *, + (e.g., 0, 1, 2, 3)
MAX_RECURSION_DEPTH = 1  # Maximum depth for recursive patterns (?R)
DEFAULT_REGEX_TIMEOUT_SECONDS = float(os.environ.get("REGEX_GRAPH_TIMEOUT_SECONDS", "10"))
AFTER_END_ANCHOR_KEY = "__after_end_anchor__"
NEXT_WORD_EXPECTATION_KEY = "__next_word_expectation__"
class Node:
    def __init__(self, label, kind='simple', payload=None):
        self.id = None  # Assigned later
        self.label = label
        self.kind = kind
        self.payload = payload
        self.next = []
        self.char_count = 1

    def connect(self, node):
        self.next.append(node)
        return node

    def __repr__(self):
        return f"[{self.label}]"

class _BaseRegexGraphBuilder:
    def __init__(self, regex_pattern, flags=0, validate=True):
        self.regex = regex_pattern
        self.flags = flags
        self.validate = validate
        self.start_node = Node("START", kind='start')
        self.end_node = Node("END", kind='end')
        self.nodes = []
        self.recursion_counter = 0
        self.valid_samples_cache = None  # Cache for mutation-based negatives
        self.conditionals = {}  # Store conditional info: {marker: (group_id, yes, no)}
        self.lookarounds = []  # Store lookaround assertions
        self.requires_match_validation = False
        
        # Try to use regex module first for advanced features
        if REGEX_AVAILABLE:
            self.using_regex_module = True
            self._build_with_regex_module()
        else:
            self.using_regex_module = False
            self._build_with_re_module()
        
        self._assign_ids()

        # Early-exit guard AFTER _assign_ids, which is what actually populates
        # self.nodes. Threshold calibrated by benchmark on this hardware:
        #
        #   143 nodes  →   5ms  ✓
        #   353 nodes  →   9ms  ✓
        #   621 nodes  →   6ms  ✓  (real-world address regex)
        #   703 nodes  →  19ms  ✓  (near limit)
        #  1050 nodes  →  sre_parse recursion crash
        #
        # 750 gives comfortable headroom under 10s for any real pattern.
        # The earlier limit of 500 was wrong — it was derived from synthetic
        # repeat-heavy patterns where node count correlated with nesting depth.
        # Real-world patterns with many nodes but shallow nesting (like the
        # address regex) are much faster per node.
        MAX_SAFE_NODES = 1000
        if len(self.nodes) > MAX_SAFE_NODES:
            raise ValueError(
                f"Pattern too complex: graph has {len(self.nodes)} nodes "
                f"(limit {MAX_SAFE_NODES}). Simplify the pattern or reduce "
                "MAX_LOOP_UNROLL."
            )

    def _build_with_regex_module(self):
        """Build using the advanced regex module"""
        try:
            import regex as re_module
            # Parse the pattern
            parsed = re_module.compile(self.regex, self.flags)
            # Get the parsed structure
            pattern_obj = parsed.pattern
            
            # First expand Unicode properties
            expanded = self._expand_unicode_properties(self.regex)
            
            # Detect lookarounds (store for negative generation, but don't expand)
            expanded, has_lookarounds = self._detect_and_expand_lookarounds(expanded)
            
            # Detect and handle conditionals - store them for later processing
            expanded, has_conditionals = self._detect_and_expand_conditionals(expanded)
            
            # Then expand recursion
            expanded = self._expand_recursion(expanded, MAX_RECURSION_DEPTH)
            
            # Now parse with standard library since recursion is expanded
            import sre_parse
            parsed = sre_parse.parse(expanded, self.flags)
            data = parsed.data if hasattr(parsed, 'data') else parsed
            self.expanded_regex = expanded
            self.parsed_tree = data
            
            last = self._build(data, self.start_node)
            for l in last:
                l.connect(self.end_node)
                
        except Exception as e:
            print(f"Parse Error with regex module: {e}")
            print("Falling back to standard re module...")
            self.using_regex_module = False
            self._build_with_re_module()

    def _expand_recursion(self, pattern, max_depth):
        """
        Manually expand (?R) recursion to a fixed depth.
        This is a simplified expansion - proper implementation would need full parser.
        """
        if '(?R)' not in pattern and '(?0)' not in pattern:
            return pattern
        
        # For the simple case of r'\((?:[^()]|(?R))*\)'
        # We'll expand it iteratively
        
        # Base case (depth 0): match empty or non-parens
        base = r'\([^()]*\)'
        
        # Build depth by depth
        current = base
        for depth in range(1, max_depth + 1):
            # Replace (?R) with the previous level pattern
            # For \((?:[^()]|(?R))*\), we replace (?R) with previous expansion
            next_pattern = pattern.replace('(?R)', current).replace('(?0)', current)
            current = next_pattern
        
        return current

    def _expand_unicode_properties(self, pattern):
        """
        Convert Unicode property escapes like \\p{L} to character classes.
        The regex module supports these, but sre_parse doesn't.
        This expansion stays intentionally approximate, but supports more
        property spellings so the parser can handle a wider set of patterns.
        """
        import re as re_std

        category_bodies = {
            'l': r'a-zA-Z\u00C0-\u024F\u1E00-\u1EFF',
            'll': r'a-z\u00E0-\u00FF',
            'lu': r'A-Z\u00C0-\u00DE',
            'n': r'0-9',
            'nd': r'0-9',
            'p': r'!-/:-@\[-`{-~',
            's': r'$+<->\^`|~',
            'z': r' \t\n\r',
            'zs': r' ',
        }
        script_bodies = {
            'greek': r'\u0370-\u03FF\u1F00-\u1FFF',
            'cyrillic': r'\u0400-\u052F',
            'latin': r'A-Za-z\u00C0-\u024F\u1E00-\u1EFF',
            'hiragana': r'\u3040-\u309F',
            'katakana': r'\u30A0-\u30FF',
            'han': r'\u3400-\u4DBF\u4E00-\u9FFF',
        }

        def _normalize_property_name(name):
            return ''.join(ch for ch in name.lower() if ch not in ' _-\t\r\n')

        def _resolve_property_body(spec):
            normalized = _normalize_property_name(spec)

            if normalized.startswith('script='):
                normalized = normalized.split('=', 1)[1]
            elif normalized.startswith('sc='):
                normalized = normalized.split('=', 1)[1]
            elif normalized.startswith('block='):
                normalized = normalized.split('=', 1)[1]
            elif normalized.startswith('blk='):
                normalized = normalized.split('=', 1)[1]
            elif normalized.startswith('is'):
                normalized = normalized[2:]
            elif normalized.startswith('in'):
                normalized = normalized[2:]

            if normalized in category_bodies:
                return category_bodies[normalized]
            if normalized in script_bodies:
                return script_bodies[normalized]
            return None

        def _replace_property(match):
            prop_type = match.group(1)
            body = _resolve_property_body(match.group(2))
            if body is None:
                return match.group(0)
            if prop_type == 'P':
                return f'[^{body}]'
            return f'[{body}]'

        return re_std.sub(r'\\([pP])\{([^}]+)\}', _replace_property, pattern)
    
    def _detect_and_expand_conditionals(self, pattern):
        """
        Leave conditionals intact.
        Python's parser already emits GROUPREF_EXISTS for both numeric and
        named conditionals; the graph builder handles that op directly.
        """
        return pattern, ('(?(' in pattern)
    
    def _detect_and_expand_lookarounds(self, pattern):
        """
        Detect lookahead/lookbehind assertions and mark them for special handling.
        Lookarounds are zero-width assertions that don't consume characters.
        
        Supported:
        (?=...)  - Positive lookahead
        (?!...)  - Negative lookahead  
        (?<=...) - Positive lookbehind
        (?<!...) - Negative lookbehind
        """
        import re as re_std
        
        # Detect all lookaround types
        lookahead_pos = r'\(\?=([^)]+)\)'      # (?=...)
        lookahead_neg = r'\(\?!([^)]+)\)'      # (?!...)
        lookbehind_pos = r'\(\?<=([^)]+)\)'    # (?<=...)
        lookbehind_neg = r'\(\?<!([^)]+)\)'    # (?<!...)
        
        has_lookarounds = False
        
        # Check for each type and store info
        for pattern_type, regex_pattern, assertion_type in [
            ('positive_lookahead', lookahead_pos, 'must_follow'),
            ('negative_lookahead', lookahead_neg, 'must_not_follow'),
            ('positive_lookbehind', lookbehind_pos, 'must_precede'),
            ('negative_lookbehind', lookbehind_neg, 'must_not_precede'),
        ]:
            matches = list(re_std.finditer(regex_pattern, pattern))
            if matches:
                has_lookarounds = True
                for match in matches:
                    content = match.group(1)
                    marker = f'__{pattern_type.upper()}_{content}__'
                    if not hasattr(self, 'lookarounds'):
                        self.lookarounds = []
                    self.lookarounds.append({
                        'type': pattern_type,
                        'assertion': assertion_type,
                        'content': content,
                        'marker': marker
                    })
        
        # For now, we'll mark lookarounds but keep them in pattern
        # The regex module will handle them, we'll use them for negative generation
        return pattern, has_lookarounds

    def _build_with_re_module(self):
        """Build using standard re module (no recursion support)"""
        import sre_parse, sys
        # sre_parse uses recursion proportional to pattern nesting depth.
        # Raise the limit temporarily so deeply nested patterns don't crash
        # before _build even runs. We restore it immediately after parsing.
        _old_limit = sys.getrecursionlimit()
        sys.setrecursionlimit(max(_old_limit, 10_000))
        try:
            # First expand Unicode properties (standard re doesn't support them either)
            expanded = self._expand_unicode_properties(self.regex)
            
            # Detect lookarounds
            expanded, has_lookarounds = self._detect_and_expand_lookarounds(expanded)
            
            # Detect and handle conditionals
            expanded, has_conditionals = self._detect_and_expand_conditionals(expanded)
            
            parsed = sre_parse.parse(expanded, self.flags)
            data = parsed.data if hasattr(parsed, 'data') else parsed
            self.expanded_regex = expanded
            self.parsed_tree = data

            last = self._build(data, self.start_node)
            for l in last:
                l.connect(self.end_node)
        except Exception as e:
            print(f"Parse Error: {e} {self.regex}")
        finally:
            sys.setrecursionlimit(_old_limit)

    def _assign_ids(self):
        """Assign unique IDs for visualization"""
        visited = set()
        stack = [self.start_node]
        counter = 0
        while stack:
            node = stack.pop()
            if node in visited: continue
            visited.add(node)
            node.id = str(counter)
            counter += 1
            stack.extend(node.next)
            self.nodes.append(node)

    def _get_category_chars(self, category_code):
        """Convert category codes to character sets"""
        import sre_constants
        if category_code == sre_constants.CATEGORY_DIGIT:
            return set(string.digits), 10, "\\d"
        elif category_code == sre_constants.CATEGORY_NOT_DIGIT:
            all_printable = set(string.printable)
            return all_printable - set(string.digits), 90, "\\D"
        elif category_code == sre_constants.CATEGORY_SPACE:
            return set(string.whitespace), 6, "\\s"
        elif category_code == sre_constants.CATEGORY_NOT_SPACE:
            all_printable = set(string.printable)
            return all_printable - set(string.whitespace), 94, "\\S"
        elif category_code == sre_constants.CATEGORY_WORD:
            word_chars = set(string.ascii_letters + string.digits + '_')
            return word_chars, 63, "\\w"
        elif category_code == sre_constants.CATEGORY_NOT_WORD:
            all_printable = set(string.printable)
            word_chars = set(string.ascii_letters + string.digits + '_')
            return all_printable - word_chars, 37, "\\W"
        else:
            return set(), 0, f"CAT_{category_code}"

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

    def _get_backref_fragment(self, memory, group_id):
        if group_id not in memory:
            return False, ""
        return True, memory[group_id]

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
            if op == sre_constants.SUBPATTERN:
                if self._tree_sequence_can_consume(av[3]):
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

    def _heuristic_anchor_allows(self, anchor_node, current_string, next_can_consume):
        payload = anchor_node.payload
        if payload in {"start", "beginning", "beginning_string"}:
            return len(current_string) == 0
        if payload in {"end", "end_string"}:
            return not next_can_consume
        if payload == "word_boundary":
            previous_is_word = self._is_word_char(current_string[-1:]) if current_string else False
            return previous_is_word != next_can_consume
        if payload == "not_word_boundary":
            previous_is_word = self._is_word_char(current_string[-1:]) if current_string else False
            return previous_is_word == next_can_consume
        return True

    def _active_after_end_anchor(self, active_caps):
        return bool(active_caps.get(AFTER_END_ANCHOR_KEY))

    def _mark_after_end_anchor(self, active_caps):
        next_active = active_caps.copy()
        next_active[AFTER_END_ANCHOR_KEY] = True
        return next_active

    def _mark_next_word_expectation(self, active_caps, expected_is_word):
        next_active = active_caps.copy()
        next_active[NEXT_WORD_EXPECTATION_KEY] = expected_is_word
        return next_active

    def _next_word_expectation(self, active_caps):
        return active_caps.get(NEXT_WORD_EXPECTATION_KEY)

    def _clear_next_word_expectation(self, active_caps):
        if NEXT_WORD_EXPECTATION_KEY not in active_caps:
            return active_caps.copy()
        next_active = active_caps.copy()
        next_active.pop(NEXT_WORD_EXPECTATION_KEY, None)
        return next_active

    def _char_satisfies_next_word_expectation(self, ch, active_caps):
        expected = self._next_word_expectation(active_caps)
        return expected is None or self._is_word_char(ch) == expected

    def _chars_allowed_by_next_word_expectation(self, chars, active_caps):
        expected = self._next_word_expectation(active_caps)
        if expected is None:
            return list(chars)
        return [ch for ch in chars if self._is_word_char(ch) == expected]

    def _boundary_expected_wordness(self, anchor_node, current_string):
        previous_is_word = self._is_word_char(current_string[-1:]) if current_string else False
        if anchor_node.payload == "word_boundary":
            return not previous_is_word
        if anchor_node.payload == "not_word_boundary":
            return previous_is_word
        return None

    def _allowed_pass_through_next_nodes(self, node, active_caps):
        if not self._active_after_end_anchor(active_caps):
            return list(node.next)
        return [nxt for nxt in node.next if not self._next_can_consume(nxt)]

    def _tree_has_end_anchor(self, items):
        import sre_constants

        sequence = items if isinstance(items, (list, tuple)) else items.data
        for op, av in sequence:
            if op == sre_constants.AT and av in {
                sre_constants.AT_END,
                getattr(sre_constants, "AT_END_STRING", object()),
            }:
                return True
            if op == sre_constants.BRANCH:
                if any(self._tree_has_end_anchor(alt) for alt in av[1]):
                    return True
            elif op == sre_constants.SUBPATTERN:
                if self._tree_has_end_anchor(av[3]):
                    return True
            elif op in (sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT) or (
                hasattr(sre_constants, "POSSESSIVE_REPEAT") and op == sre_constants.POSSESSIVE_REPEAT
            ):
                if self._tree_has_end_anchor(av[2]):
                    return True
            elif hasattr(sre_constants, "ATOMIC_GROUP") and op == sre_constants.ATOMIC_GROUP:
                if self._tree_has_end_anchor(av):
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

    def _build_repeat(self, current_leads, min_rep, max_rep, sub_tree, label_prefix, _depth):
        import sre_constants

        effective_unroll = max(1, MAX_LOOP_UNROLL - _depth)
        if max_rep == sre_constants.MAXREPEAT:
            max_rep = effective_unroll

        split = Node(f"{label_prefix} {min_rep}-{max_rep}", kind='split')
        for lead in current_leads:
            lead.connect(split)

        exit_leads = []
        chain = [split]
        if min_rep == 0:
            exit_leads.append(split)

        for i in range(1, max_rep + 1):
            chain = self._build(sub_tree, chain, _depth + 1)
            if i >= min_rep:
                exit_leads.extend(chain)

        return exit_leads

    def _parse_loop_bounds(self, label):
        import re as re_std

        match = re_std.search(r'(\d+)-(\d+)$', str(label))
        if not match:
            return None, None
        return int(match.group(1)), int(match.group(2))

    def _build(self, tree, start_node, _depth=0):
        """
        Build graph from parse tree.

        ROOT CAUSE FIX: the old code looped over current_leads and called
        _build(sub_tree, single_lead) once per lead, creating a *separate copy*
        of every sub-pattern for every lead.  With nested repeats this is
        O(branches ^ depth) — exponential.

        The fix: pass ALL current leads into the sub-pattern at once so that ONE
        shared set of nodes is built regardless of how many leads feed into it.
        The signature keeps start_node for backwards compatibility with all call
        sites; internally we always work with a list.

        _depth tracks repeat nesting level so deeply-nested repeats get a lower
        unroll cap, preventing lead explosion in patterns like (\\w+\\s?)+ where
        each level multiplies leads by (63^unroll).
        """
        import sre_constants

        # Reduce unroll depth for each level of repeat nesting.
        # depth=0 → MAX_LOOP_UNROLL, depth=1 → max(1, unroll-1), etc.
        # This keeps the graph tractable for patterns with nested unbounded repeats.
        effective_unroll = max(1, MAX_LOOP_UNROLL - _depth)

        current_leads = [start_node] if not isinstance(start_node, list) else start_node
        items = tree if isinstance(tree, (list, tuple)) else tree.data

        for op, av in items:
            new_leads = []

            # --- LITERAL (Char) ---
            if op == sre_constants.LITERAL:
                n = Node(f"'{chr(av)}'", kind='match', payload=chr(av))
                n.char_count = 1
                for lead in current_leads:
                    lead.connect(n)
                new_leads = [n]

            # --- ANY (.) ---
            elif op == sre_constants.ANY:
                char_set = set(string.printable) - {'\n', '\r'}
                n = Node(".", kind='class', payload=char_set)
                n.char_count = len(char_set)
                for lead in current_leads:
                    lead.connect(n)
                new_leads = [n]

            # --- CATEGORY (\d, \w, \s, etc) ---
            elif op == sre_constants.CATEGORY:
                char_set, count, label = self._get_category_chars(av)
                n = Node(label, kind='class', payload=char_set)
                n.char_count = count
                for lead in current_leads:
                    lead.connect(n)
                new_leads = [n]

            # --- OR (Branch) ---
            elif op == sre_constants.BRANCH:
                split = Node("Split", kind='split')
                for lead in current_leads:
                    lead.connect(split)
                for alt_path in av[1]:
                    ends = self._build(alt_path, [split], _depth)
                    new_leads.extend(ends)

            # --- CAPTURE / NON-CAPTURE GROUP ---
            elif op == sre_constants.SUBPATTERN:
                group_num = av[0]
                sub_tree = av[3]

                if group_num is None:
                    # Non-capturing group (?:...)
                    new_leads = self._build(sub_tree, current_leads, _depth)
                else:
                    # Capturing group: one shared cap_start node for all leads
                    cap_start = Node(f"Start G{group_num}", kind='group_start', payload=group_num)
                    for lead in current_leads:
                        lead.connect(cap_start)
                    inner_ends = self._build(sub_tree, [cap_start], _depth)
                    cap_end = Node(f"End G{group_num}", kind='group_end', payload=group_num)
                    for end in inner_ends:
                        end.connect(cap_end)
                    new_leads = [cap_end]

            # --- CONDITIONAL (?(id/name)yes|no) ---
            elif hasattr(sre_constants, 'GROUPREF_EXISTS') and op == sre_constants.GROUPREF_EXISTS:
                group_id, yes_tree, no_tree = av
                cond = Node(f"Cond G{group_id}", kind='conditional', payload=group_id)
                for lead in current_leads:
                    lead.connect(cond)

                yes_gate = Node("YES", kind='cond_yes', payload=group_id)
                no_gate = Node("NO", kind='cond_no', payload=group_id)
                cond.connect(yes_gate)
                cond.connect(no_gate)

                new_leads.extend(self._build(yes_tree, [yes_gate], _depth))
                if no_tree:
                    new_leads.extend(self._build(no_tree, [no_gate], _depth))
                else:
                    new_leads.append(no_gate)

            # --- MAX_REPEAT (*, +, {n,m}) - GREEDY ---
            elif op == sre_constants.MAX_REPEAT:
                min_rep, max_rep = av[0], av[1]
                sub_tree = av[2]
                new_leads = self._build_repeat(current_leads, min_rep, max_rep, sub_tree, "Loop", _depth)

            # --- MIN_REPEAT (non-greedy *?, +?, {n,m}?) ---
            elif op == sre_constants.MIN_REPEAT:
                min_rep, max_rep = av[0], av[1]
                sub_tree = av[2]
                new_leads = self._build_repeat(current_leads, min_rep, max_rep, sub_tree, "Loop?", _depth)

            # --- POSSESSIVE_REPEAT (*+, ++, {m,n}+) ---
            elif hasattr(sre_constants, 'POSSESSIVE_REPEAT') and op == sre_constants.POSSESSIVE_REPEAT:
                min_rep, max_rep = av[0], av[1]
                sub_tree = av[2]
                self.requires_match_validation = True
                new_leads = self._build_repeat(current_leads, min_rep, max_rep, sub_tree, "Loop+", _depth)

            # --- ATOMIC_GROUP (?>...) ---
            elif hasattr(sre_constants, 'ATOMIC_GROUP') and op == sre_constants.ATOMIC_GROUP:
                self.requires_match_validation = True
                atomic_start = Node("Atomic", kind='atomic')
                for lead in current_leads:
                    lead.connect(atomic_start)
                new_leads = self._build(av, [atomic_start], _depth)

            # --- LOOKAROUND ASSERTIONS (?=...), (?!...), (?<=...), (?<!...) ---
            elif str(op) in {"ASSERT", "ASSERT_NOT"}:
                payload = self._lookaround_payload_from_sre(op, av)
                direction_label = "ahead" if payload["direction"] >= 0 else "behind"
                sign_label = "not " if payload["negative"] else ""
                n = Node(f"Assert {sign_label}{direction_label}", kind='lookaround', payload=payload)
                n.char_count = 0
                for lead in current_leads:
                    lead.connect(n)
                new_leads = [n]

            # --- BACKREFERENCE ---
            elif op == sre_constants.GROUPREF:
                ref_id = av
                n = Node(f"\\{ref_id}", kind='backref', payload=ref_id)
                for lead in current_leads:
                    lead.connect(n)
                new_leads = [n]

            # --- CHAR CLASS [abc] or [a-z] ---
            elif op == sre_constants.IN:
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
                    all_chars = set(string.printable)
                    char_set = all_chars - char_set
                    label = f"[^...] ({len(char_set)})"
                else:
                    label = f"[...] ({len(char_set)})"
                n = Node(label, kind='class', payload=char_set)
                n.char_count = len(char_set)
                for lead in current_leads:
                    lead.connect(n)
                new_leads = [n]

            # --- ANCHORS ---
            elif op == sre_constants.AT:
                anchor_type = av
                payload = self._anchor_payload_from_sre(anchor_type)
                if payload == "start":
                    n = Node("^", kind='anchor', payload='start')
                elif payload == "end":
                    n = Node("$", kind='anchor', payload='end')
                elif payload == "word_boundary":
                    n = Node("\\b", kind='anchor', payload='word_boundary')
                elif payload == "not_word_boundary":
                    n = Node("\\B", kind='anchor', payload='not_word_boundary')
                else:
                    n = Node(f"Anchor({anchor_type})", kind='anchor', payload=anchor_type)
                n.char_count = 0
                for lead in current_leads:
                    lead.connect(n)
                new_leads = [n]

            # --- NOT_LITERAL ---
            elif op == sre_constants.NOT_LITERAL:
                excluded_char = chr(av)
                all_chars = set(string.printable)
                char_set = all_chars - {excluded_char}
                n = Node(f"[^{excluded_char}]", kind='class', payload=char_set)
                n.char_count = len(char_set)
                for lead in current_leads:
                    lead.connect(n)
                new_leads = [n]

            # --- Unknown ---
            else:
                n = Node(f"?({op})", kind='unknown', payload=av)
                for lead in current_leads:
                    lead.connect(n)
                new_leads = [n]

            current_leads = new_leads

        return current_leads

    def count_paths(self):
        """Count total number of valid strings"""
        memo = {}
        
        def count_from_node(node, has_backref=False):
            if node.kind == 'end':
                return 1
            
            if not has_backref and node in memo:
                return memo[node]
            
            total = 0
            
            if node.kind == 'backref':
                has_backref = True
                for next_node in node.next:
                    total += count_from_node(next_node, has_backref)
            
            elif node.kind in ['match', 'class', 'any']:
                for next_node in node.next:
                    total += node.char_count * count_from_node(next_node, has_backref)
            
            elif node.kind in ['split', 'group_start', 'group_end', 'start', 'anchor', 'conditional', 'cond_yes', 'cond_no', 'atomic']:
                for next_node in node.next:
                    total += count_from_node(next_node, has_backref)
            
            else:
                for next_node in node.next:
                    total += count_from_node(next_node, has_backref)
            
            if not has_backref:
                memo[node] = total
            
            return total
        
        total_count = count_from_node(self.start_node)
        has_backref = any(n.kind == 'backref' for n in self.nodes)
        
        return total_count, has_backref

    def visualize(self, filename='regex_graph', view_on_desktop=False):
        if not GRAPHVIZ_AVAILABLE:
            raise RuntimeError("Visualization requires the 'graphviz' package. Install with: pip install graphviz")
        dot = graphviz.Digraph(format='png')
        dot.attr(rankdir='LR')
        
        for n in self.nodes:
            color = 'black'
            shape = 'box'
            style = ''
            fillcolor = 'white'
            label = str(n.label)
            
            if n.kind in ['class', 'any'] and n.char_count > 1:
                label += f"\n({n.char_count} chars)"
            
            if n.kind == 'match': 
                color = 'green'
            elif n.kind == 'class' or n.kind == 'any':
                color = 'purple'
                style = 'filled'
                fillcolor = '#f3e5f5'
            elif n.kind == 'group_start': 
                shape, color = 'component', 'blue'
                style = 'filled'
                fillcolor = '#e3f2fd'
            elif n.kind == 'group_end': 
                shape, color = 'component', 'blue'
                style = 'filled'
                fillcolor = '#e3f2fd'
            elif n.kind == 'backref': 
                shape, style, color = 'note', 'filled', 'orange'
                fillcolor = '#fff3e0'
            elif n.kind == 'conditional':
                shape = 'diamond'
                style = 'filled'
                fillcolor = '#fff8e1'
            elif n.kind in ['cond_yes', 'cond_no']:
                shape = 'oval'
                style = 'filled'
                fillcolor = '#fff8e1'
            elif n.kind == 'atomic':
                shape = 'box3d'
                style = 'filled'
                fillcolor = '#fce4ec'
            elif n.kind == 'split': 
                shape = 'diamond'
            elif n.kind == 'anchor':
                shape = 'hexagon'
                style = 'filled'
                fillcolor = '#e8f5e9'
            elif n.kind == 'start': 
                shape = 'doublecircle'
                style = 'filled'
                fillcolor = '#eeeeee'
            elif n.kind == 'end': 
                shape = 'doublecircle'
                style = 'filled'
                fillcolor = '#eeeeee'

            dot.node(n.id, label, shape=shape, color=color, style=style, fillcolor=fillcolor)

            for nxt in n.next:
                dot.edge(n.id, nxt.id)
        
        if view_on_desktop:
            dot.render(filename, view=True)
            
        return dot

    def generate_sample(self, max_samples=None, force_complete=False,
                        timeout_seconds=DEFAULT_REGEX_TIMEOUT_SECONDS, seed=None):
        """Backward-compatible wrapper to the diversity-first sampler."""
        return self.generate_diverse_samples(
            n=1_000 if max_samples is None else max_samples,
            seed=seed,
            timeout_seconds=timeout_seconds,
        )

    def generate_heuristic_samples(self, seed=None,
                                   timeout_seconds=DEFAULT_REGEX_TIMEOUT_SECONDS,
                                   max_samples=None):
        """Emit a stable checklist of representative inputs."""
        return _run_with_timeout(
            lambda: self._collect_heuristic_samples(seed=seed, max_samples=max_samples),
            timeout_seconds,
        )

    def generate_diverse_samples(self, n=1000, seed=None,
                                 timeout_seconds=DEFAULT_REGEX_TIMEOUT_SECONDS):
        """Generate valid inputs with branch and charset diversity as the priority."""
        def _generate():
            total_count, has_backref = self.count_paths()

            if total_count <= n and not has_backref:
                # print(f"Generating all {total_count} strings exhaustively...")
                return sorted(self._generate_exhaustive(n))

            # print(f"Total possible: {total_count}, sampling {n} diverse strings...")
            return self._generate_diverse_sample(n, seed=seed)

        return _run_with_timeout(_generate, timeout_seconds)

    def generate_high_coverage_samples(self, n=None, seed=None,
                                       timeout_seconds=DEFAULT_REGEX_TIMEOUT_SECONDS):
        """Generate many valid inputs quickly while still steering toward coverage."""
        def _generate():
            target = 5_000 if n is None else n
            total_count, has_backref = self.count_paths()

            if total_count <= target and not has_backref:
                # print(f"Generating all {total_count} strings exhaustively...")
                return sorted(self._generate_exhaustive(target))

            # print(f"Total possible: {total_count}, sampling up to {target} high-coverage strings...")
            results = []
            seen = set()

            heuristic_seed = 0 if seed is None else seed
            for s in self._collect_heuristic_samples(seed=heuristic_seed, max_samples=min(64, target)):
                if s not in seen:
                    seen.add(s)
                    results.append(s)

            remaining = max(0, target - len(results))
            if remaining:
                stratified = self._generate_stratified_sample(max(remaining // 2, min(remaining, 256)), seed=seed)
                for s in stratified:
                    if s is not None and s not in seen:
                        seen.add(s)
                        results.append(s)
                        if len(results) >= target:
                            return results

            remaining = max(0, target - len(results))
            if remaining:
                for s in self._generate_high_coverage_walk(remaining, seed=seed):
                    if s not in seen:
                        seen.add(s)
                        results.append(s)
                        if len(results) >= target:
                            break

            return results[:target]

        return _run_with_timeout(_generate, timeout_seconds)

    def _dedupe_preserve_order(self, items):
        seen = set()
        out = []
        for item in items:
            if item in seen:
                continue
            seen.add(item)
            out.append(item)
        return out

    def _collect_heuristic_samples(self, seed=None, max_samples=None):
        import random as _random

        rng = _random.Random(0 if seed is None else seed)
        raw = self._heuristic_expand_sequence(self.parsed_tree, rng, depth=0, memory={})
        candidates = self._dedupe_preserve_order(s for s, _ in raw)
        valid = []
        for s in candidates:
            if self.validate:
                accepted = self._regex_fullmatch(s)
            else:
                accepted = s is not None
            if accepted:
                valid.append(s)
        if max_samples is not None:
            return valid[:max_samples]
        return valid

    def _cap_heuristic_results(self, states, limit=256):
        if len(states) <= limit:
            return states
        return states[:limit]

    def _heuristic_repeat_counts(self, min_rep, max_rep, depth, rng):
        import sre_constants

        representable_max = max_rep
        if max_rep == sre_constants.MAXREPEAT:
            representable_max = max(1, MAX_LOOP_UNROLL - depth)

        if min_rep == max_rep and max_rep != sre_constants.MAXREPEAT:
            return [min_rep]
        if min_rep == 0 and max_rep == 1:
            return [0, 1]
        if max_rep == sre_constants.MAXREPEAT and min_rep == 0:
            large = min(representable_max, rng.randint(min_rep + 1, min_rep + 1000))
            return self._dedupe_preserve_order([0, 1, 2, large])
        if max_rep == sre_constants.MAXREPEAT and min_rep == 1:
            large = min(representable_max, rng.randint(min_rep + 1, min_rep + 1000))
            return self._dedupe_preserve_order([1, 2, large])
        if max_rep == sre_constants.MAXREPEAT:
            large = min(representable_max, rng.randint(min_rep + 1, min_rep + 1000))
            return self._dedupe_preserve_order([min_rep, min_rep + 1, large])
        if max_rep - min_rep > 1:
            middle = rng.randint(min_rep + 1, max_rep - 1)
        else:
            middle = rng.randint(min_rep, max_rep)
        return self._dedupe_preserve_order([min_rep, middle, max_rep])

    def _repeat_sequence(self, tree, count, rng, depth, memory):
        if count > 1 and self._tree_has_start_or_end_anchor(tree):
            return []
        states = [("", dict(memory))]
        for _ in range(count):
            next_states = []
            for prefix, mem in states:
                for frag, next_mem in self._heuristic_expand_sequence(tree, rng, depth + 1, dict(mem)):
                    next_states.append((prefix + frag, next_mem))
            states = self._cap_heuristic_results(next_states)
        return states

    def _lookaround_fragments(self, sub_tree, rng, depth, memory):
        fragments = []
        try:
            for frag, _next_mem in self._heuristic_expand_sequence(sub_tree, rng, depth + 1, dict(memory)):
                if frag is not None:
                    fragments.append(frag)
        except Exception:
            return []
        return self._dedupe_preserve_order(fragments)[:8]

    def _lookaround_payload_from_sre(self, op, av):
        direction, sub_tree = av
        return {
            "negative": str(op) == "ASSERT_NOT",
            "direction": direction,
            "tree": sub_tree,
        }

    def _apply_lookaround_fragments(self, prefix, payload, rng, depth, memory):
        if payload["direction"] >= 0 and self._tree_has_start_or_end_anchor(payload["tree"]):
            return [("", dict(memory))]
        fragments = self._lookaround_fragments(payload["tree"], rng, depth, memory)
        if payload["negative"]:
            return [("", dict(memory))]
        if not fragments:
            return []
        return [(fragment, dict(memory)) for fragment in fragments]

    def _representative_chars(self, char_set, rng, include_digit_word=False):
        chars = sorted(set(char_set))
        if not chars:
            return []
        reps = [chars[0], chars[-1]]
        if include_digit_word and "_" in chars:
            reps.append("_")
        if include_digit_word:
            digit = next((c for c in chars if c.isdigit()), None)
            alpha = next((c for c in chars if c.isalpha()), None)
            if alpha is not None:
                reps.append(alpha)
            if digit is not None:
                reps.append(digit)
        middle = chars[len(chars) // 2]
        reps.append(middle)
        reps.append(rng.choice(chars))
        return self._dedupe_preserve_order(reps)

    def _chars_for_category(self, category_code, rng):
        import sre_constants

        if category_code == sre_constants.CATEGORY_DIGIT:
            return self._dedupe_preserve_order(["0", "9", rng.choice(list(string.digits))])
        if category_code == sre_constants.CATEGORY_WORD:
            return self._dedupe_preserve_order(["a", "0", "_", rng.choice(list(string.ascii_letters + string.digits + "_"))])
        if category_code == sre_constants.CATEGORY_SPACE:
            return self._dedupe_preserve_order([" ", "\t", rng.choice(list(string.whitespace))])
        char_set, _, _ = self._get_category_chars(category_code)
        return self._representative_chars(char_set, rng)

    def _chars_for_in(self, av, rng):
        import sre_constants

        char_set = set()
        negated = False
        has_word_category = False
        for sub_op, sub_av in av:
            if sub_op == sre_constants.LITERAL:
                char_set.add(chr(sub_av))
            elif sub_op == sre_constants.RANGE:
                start, end = sub_av
                char_set.update(chr(c) for c in range(start, end + 1))
            elif sub_op == sre_constants.CATEGORY:
                if sub_av == sre_constants.CATEGORY_WORD:
                    has_word_category = True
                cat_chars, _, _ = self._get_category_chars(sub_av)
                char_set.update(cat_chars)
            elif sub_op == sre_constants.NEGATE:
                negated = True
        if negated:
            char_set = set(string.printable) - char_set
            reps = self._representative_chars(char_set, rng)
            outside = self._get_char_outside_class(char_set, rng=rng)
            return self._dedupe_preserve_order(reps[:2] + [outside, rng.choice(reps)])
        return self._representative_chars(char_set, rng, include_digit_word=has_word_category)

    def _chars_for_any(self, rng):
        chars = sorted(set(string.printable) - {"\n", "\r"})
        return self._dedupe_preserve_order([chars[0], chars[-1], chars[len(chars) // 2], rng.choice(chars)])

    def _heuristic_expand_sequence(self, items, rng, depth, memory):
        import sre_constants

        states = [("", dict(memory))]
        sequence = items if isinstance(items, (list, tuple)) else items.data

        for index, (op, av) in enumerate(sequence):
            next_states = []
            for prefix, mem in states:
                expansions = []

                if op == sre_constants.LITERAL:
                    expansions = [(chr(av), dict(mem))]
                elif op == sre_constants.ANY:
                    expansions = [(ch, dict(mem)) for ch in self._chars_for_any(rng)]
                elif op == sre_constants.CATEGORY:
                    expansions = [(ch, dict(mem)) for ch in self._chars_for_category(av, rng)]
                elif op == sre_constants.BRANCH:
                    for alt in av[1]:
                        expansions.extend(self._heuristic_expand_sequence(alt, rng, depth + 1, dict(mem)))
                elif op == sre_constants.SUBPATTERN:
                    group_num = av[0]
                    sub_tree = av[3]
                    for frag, next_mem in self._heuristic_expand_sequence(sub_tree, rng, depth + 1, dict(mem)):
                        updated = dict(next_mem)
                        if group_num is not None:
                            updated[group_num] = frag
                        expansions.append((frag, updated))
                elif op in (sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT) or (
                    hasattr(sre_constants, "POSSESSIVE_REPEAT") and op == sre_constants.POSSESSIVE_REPEAT
                ):
                    min_rep, max_rep, sub_tree = av[0], av[1], av[2]
                    for count in self._heuristic_repeat_counts(min_rep, max_rep, depth, rng):
                        expansions.extend(self._repeat_sequence(sub_tree, count, rng, depth + 1, dict(mem)))
                elif hasattr(sre_constants, 'GROUPREF_EXISTS') and op == sre_constants.GROUPREF_EXISTS:
                    group_id, yes_tree, no_tree = av
                    branch_tree = yes_tree if mem.get(group_id) else no_tree
                    if branch_tree:
                        expansions.extend(self._heuristic_expand_sequence(branch_tree, rng, depth + 1, dict(mem)))
                    else:
                        expansions.append(("", dict(mem)))
                elif op == sre_constants.GROUPREF:
                    found, frag = self._get_backref_fragment(mem, av)
                    expansions = [(frag, dict(mem))] if found else []
                elif op == sre_constants.IN:
                    expansions = [(ch, dict(mem)) for ch in self._chars_for_in(av, rng)]
                elif op == sre_constants.AT:
                    anchor = Node("anchor", kind="anchor", payload=self._anchor_payload_from_sre(av))
                    remaining = sequence[index + 1 :]
                    next_can_consume = self._tree_sequence_can_consume(remaining)
                    if self._heuristic_anchor_allows(anchor, prefix, next_can_consume):
                        updated = dict(mem)
                        expected_wordness = self._boundary_expected_wordness(anchor, prefix)
                        if expected_wordness is not None:
                            updated[NEXT_WORD_EXPECTATION_KEY] = expected_wordness
                        expansions = [("", updated)]
                    else:
                        expansions = []
                elif op == sre_constants.NOT_LITERAL:
                    excluded = chr(av)
                    char_set = set(string.printable) - {excluded}
                    expansions = [(ch, dict(mem)) for ch in self._representative_chars(char_set, rng)]
                elif hasattr(sre_constants, "ATOMIC_GROUP") and op == sre_constants.ATOMIC_GROUP:
                    expansions.extend(self._heuristic_expand_sequence(av, rng, depth + 1, dict(mem)))
                elif str(op) in {"ASSERT", "ASSERT_NOT"}:
                    payload = self._lookaround_payload_from_sre(op, av)
                    expansions = self._apply_lookaround_fragments(prefix, payload, rng, depth, mem)
                else:
                    expansions = [("", dict(mem))]

                for frag, next_mem in expansions:
                    expected_wordness = mem.get(NEXT_WORD_EXPECTATION_KEY)
                    if expected_wordness is not None and frag:
                        if self._is_word_char(frag[:1]) != expected_wordness:
                            continue
                        next_mem = dict(next_mem)
                        next_mem.pop(NEXT_WORD_EXPECTATION_KEY, None)
                    next_states.append((prefix + frag, next_mem))
            states = self._cap_heuristic_results(next_states)
        return states

    def _valid_next_strings(self, node, s, memory, active_caps, *, rng, class_hits, class_cursors, per_class):
        next_states = []
        if node.kind in ['class', 'any']:
            choices = self._representative_chars(node.payload, rng) if node.kind == 'class' else self._chars_for_any(rng)
            usage = class_hits.setdefault(node.id, {})
            ordered = sorted(choices, key=lambda ch: (usage.get(ch, 0), ch))
            if ordered:
                cursor = class_cursors.get(node.id, 0) % len(ordered)
                ordered = ordered[cursor:] + ordered[:cursor]
                class_cursors[node.id] = class_cursors.get(node.id, 0) + per_class
            for char in ordered[:per_class]:
                usage[char] = usage.get(char, 0) + 1
                next_s = s + char
                for nxt in node.next:
                    next_states.append((nxt, next_s, memory.copy(), active_caps.copy()))
            return next_states
        return self._get_next_states(node, s, memory, active_caps)

    def _state_novelty(self, node, next_node, next_s, edge_hits, seen_lengths):
        edge_key = (getattr(node, "id", id(node)), getattr(next_node, "id", id(next_node)))
        novelty = 100 - edge_hits.get(edge_key, 0) * 10
        novelty -= seen_lengths.get(len(next_s), 0)
        return novelty

    def _generate_diverse_sample(self, target_count, seed=None):
        import random as _random

        results = []
        seen = set()
        rng = _random.Random(seed)

        heuristic_budget = min(max(8, target_count // 4), target_count, 16)
        for s in self._collect_heuristic_samples(seed=seed, max_samples=heuristic_budget):
            if s not in seen:
                seen.add(s)
                results.append(s)
                if len(results) >= target_count:
                    return results[:target_count]

        stratified_target = max(0, target_count - len(results))
        if stratified_target:
            for s in self._generate_stratified_sample(stratified_target, seed=seed):
                if s is not None and s not in seen:
                    seen.add(s)
                    results.append(s)
                    if len(results) >= target_count:
                        return results[:target_count]

        if len(results) < target_count:
            path_counts = self._compute_path_counts_from_nodes()
            rounds_without_progress = 0
            refill_round = 0

            while len(results) < target_count and rounds_without_progress < 8:
                before = len(results)

                remaining = target_count - len(results)
                refill_seed = rng.randint(0, 10**9) + refill_round
                batch_target = min(max(remaining * 2, 64), max(remaining * 4, 256))

                for s in self._generate_stratified_sample(batch_target, seed=refill_seed):
                    if s is not None and s not in seen:
                        seen.add(s)
                        results.append(s)
                        if len(results) >= target_count:
                            return results[:target_count]

                if len(results) < target_count:
                    bfs_batch = self._generate_bfs_sample(min(max(remaining * 2, 64), 5_000))
                    for s in bfs_batch:
                        if s not in seen:
                            seen.add(s)
                            results.append(s)
                            if len(results) >= target_count:
                                return results[:target_count]

                if len(results) < target_count:
                    random_attempts = min(max(remaining * 20, 500), 50_000)
                    for _ in range(random_attempts):
                        s = self._generate_random_path(path_counts, rng)
                        if s is None or s in seen:
                            continue
                        seen.add(s)
                        results.append(s)
                        if len(results) >= target_count:
                            return results[:target_count]

                if len(results) == before:
                    rounds_without_progress += 1
                else:
                    rounds_without_progress = 0
                refill_round += 1

        return results[:target_count]

    def _generate_high_coverage_walk(self, target_count, seed=None):
        import random as _random

        rng = _random.Random(seed)
        path_counts = self._compute_path_counts_from_nodes()
        results = []
        seen = set()
        branch_hits = {}
        max_attempts = max(target_count * 20, 500)

        for _ in range(max_attempts):
            s = self._generate_random_path(path_counts, rng)
            if s is None or s in seen:
                continue
            seen.add(s)
            results.append(s)
            if len(results) >= target_count:
                break

        if len(results) < target_count:
            extra = self._generate_diverse_sample(target_count - len(results), seed=seed)
            for s in extra:
                if s not in seen:
                    seen.add(s)
                    results.append(s)
                    if len(results) >= target_count:
                        break

        return results[:target_count]
    
    def _generate_exhaustive(self, max_samples):
        """
        Generate ALL possible strings by exhaustive graph traversal.
        Stops once max_samples results are collected or the stack is exhausted.
        """
        results = set()
        MAX_STACK = max_samples * 1000  # generous headroom before truncating

        stack = [(self.start_node, "", {}, {})]

        while stack:
            if len(stack) > MAX_STACK:
                print(f"[exhaustive] stack exceeded {MAX_STACK}, truncating.")
                break
            if len(results) >= max_samples:
                break

            node, s, memory, active_caps = stack.pop()

            if node.kind == 'end':
                clean_s = self._clean_conditional_markers(s, memory)
                if self._should_accept_positive(clean_s):
                    results.add(clean_s)
                continue

            next_states = self._get_next_states(node, s, memory, active_caps)
            stack.extend(next_states)

        return list(results)
    
    def _generate_stratified_sample(self, target_count, seed=None):
        """
        Rank-based uniform sampling over the graph's string space.

        Every possible string the graph accepts is implicitly numbered 0 ..
        total_paths-1. We pick target_count indices uniformly at random across
        that range and decode each index into a concrete string by walking the
        graph. This replaces the old random-walk approach which:
          - could loop indefinitely hunting for unique strings
          - sampled heavily from the same structural branches (low diversity)
          - lost precision for path counts > 2^53 (float weights)

        Works correctly for total_paths up to arbitrarily large integers
        because Python ints are unbounded.
        """
        import random as _random
        rng = _random.Random(seed)

        path_counts = self._compute_path_counts_from_nodes()
        total = path_counts.get(self.start_node, 0)

        if total == 0:
            return []

        # Sample target_count unique indices from [0, total-1].
        # For astronomically large totals, collision probability is negligible
        # (birthday bound: ~k^2 / 2*total which is ~0 for total > 10^10).
        k = min(target_count, int(total))
        seen = set()
        indices = []
        max_attempts = k * 10
        attempts = 0
        while len(indices) < k and attempts < max_attempts:
            attempts += 1
            idx = rng.randint(0, int(total) - 1)
            if idx not in seen:
                seen.add(idx)
                indices.append(idx)

        results = []
        for idx in sorted(indices):
            s = self._decode_path_index(idx, path_counts)
            if s is not None:
                results.append(s)

        return results

    def _decode_path_index(self, index, counts):
        """
        Decode an integer path index into a concrete string.

        At each node we use integer division/modulo to split the index into
        "which branch / which character" and "position within that branch",
        so every index maps to exactly one string with no randomness.
        """
        node = self.start_node
        s = ""
        memory = {}
        active_caps = {}
        MAX_STEPS = 500
        steps = 0

        while node is not self.end_node and steps < MAX_STEPS:
            steps += 1

            if node.kind == 'match':
                if not self._char_satisfies_next_word_expectation(node.payload[:1], active_caps):
                    return None
                s += node.payload
                active_caps = self._clear_next_word_expectation(active_caps)
                node = node.next[0] if node.next else self.end_node

            elif node.kind in ('class', 'any'):
                chars = sorted(self._chars_allowed_by_next_word_expectation(node.payload, active_caps))
                if not chars:
                    return None
                downstream = sum(counts.get(nxt, 1) for nxt in node.next)
                if downstream == 0:
                    downstream = 1
                char_idx = int(index) // downstream
                index    = int(index)  % downstream
                char_idx = min(char_idx, len(chars) - 1)
                s += chars[char_idx]
                active_caps = self._clear_next_word_expectation(active_caps)
                node = node.next[0] if node.next else self.end_node

            elif node.kind == 'split':
                # Walk branches, subtracting counts until we find the right one
                next_nodes = self._allowed_pass_through_next_nodes(node, active_caps)
                for nxt in next_nodes:
                    c = counts.get(nxt, 1)
                    if index < c:
                        node = nxt
                        break
                    index -= c
                else:
                    node = next_nodes[-1] if next_nodes else self.end_node

            elif node.kind == 'group_start':
                active_caps[node.payload] = len(s)
                node = node.next[0] if node.next else self.end_node

            elif node.kind == 'group_end':
                start_idx = active_caps.get(node.payload)
                if start_idx is not None:
                    memory[node.payload] = s[start_idx:]
                next_nodes = self._allowed_pass_through_next_nodes(node, active_caps)
                node = next_nodes[0] if next_nodes else self.end_node

            elif node.kind == 'conditional':
                group_captured = bool(memory.get(node.payload))
                chosen = None
                for nxt in node.next:
                    if group_captured and nxt.kind == 'cond_yes':
                        chosen = nxt
                        break
                    if not group_captured and nxt.kind == 'cond_no':
                        chosen = nxt
                node = chosen if chosen is not None else (node.next[0] if node.next else self.end_node)

            elif node.kind in ('cond_yes', 'cond_no', 'atomic'):
                next_nodes = self._allowed_pass_through_next_nodes(node, active_caps)
                node = next_nodes[0] if next_nodes else self.end_node

            elif node.kind == 'backref':
                group_id = node.payload
                found, frag = self._get_backref_fragment(memory, group_id)
                if not found:
                    return None
                if frag and not self._char_satisfies_next_word_expectation(frag[:1], active_caps):
                    return None
                s += frag
                active_caps = self._clear_next_word_expectation(active_caps)
                node = node.next[0] if node.next else self.end_node

            elif node.kind == 'anchor':
                next_nodes = []
                for nxt in node.next:
                    if self._anchor_allows(node, s, nxt):
                        next_nodes.append(nxt)
                if not next_nodes:
                    return None
                if node.payload in {"end", "end_string"}:
                    active_caps = self._mark_after_end_anchor(active_caps)
                expected_wordness = self._boundary_expected_wordness(node, s)
                if expected_wordness is not None:
                    active_caps = self._mark_next_word_expectation(active_caps, expected_wordness)
                node = next_nodes[0]

            elif node.kind == 'lookaround':
                rng = random.Random(len(s) + int(index))
                fragments = self._apply_lookaround_fragments(s, node.payload, rng, 0, memory)
                if not fragments:
                    return None
                frag_index = min(int(index), len(fragments) - 1)
                frag, next_mem = fragments[frag_index]
                s += frag
                memory = next_mem
                node = node.next[0] if node.next else self.end_node

            else:  # anchor, start, unknown — pass through
                next_nodes = self._allowed_pass_through_next_nodes(node, active_caps)
                node = next_nodes[0] if next_nodes else self.end_node

        clean_s = self._clean_conditional_markers(s, memory)
        if clean_s is None:
            return None
        if not self._should_accept_positive(clean_s):
            return None
        return clean_s
    
    def _get_next_states(self, node, s, memory, active_caps):
        """
        Get all possible next states from current node.
        Returns list of (next_node, next_string, next_memory, next_active_caps)
        """
        next_states = []
        
        if node.kind == 'match':
            if not self._char_satisfies_next_word_expectation(node.payload[:1], active_caps):
                return next_states
            next_s = s + node.payload
            next_active = self._clear_next_word_expectation(active_caps)
            for nxt in node.next:
                next_states.append((nxt, next_s, memory.copy(), next_active.copy()))
        
        elif node.kind in ['class', 'any']:
            # For exhaustive generation, explore ALL characters in the set
            char_set = self._chars_allowed_by_next_word_expectation(node.payload, active_caps)
            next_active = self._clear_next_word_expectation(active_caps)
            for char in char_set:
                next_s = s + char
                for nxt in node.next:
                    next_states.append((nxt, next_s, memory.copy(), next_active.copy()))
        
        elif node.kind == 'group_start':
            next_active = active_caps.copy()
            next_active[node.payload] = len(s)
            for nxt in node.next:
                next_states.append((nxt, s, memory.copy(), next_active))
        
        elif node.kind == 'group_end':
            next_mem = memory.copy()
            start_idx = active_caps.get(node.payload)
            if start_idx is not None:
                captured = s[start_idx:]
                next_mem[node.payload] = captured
            for nxt in self._allowed_pass_through_next_nodes(node, active_caps):
                next_states.append((nxt, s, next_mem, active_caps.copy()))

        elif node.kind == 'conditional':
            group_captured = bool(memory.get(node.payload))
            for nxt in node.next:
                if group_captured and nxt.kind == 'cond_yes':
                    next_states.append((nxt, s, memory.copy(), active_caps.copy()))
                elif not group_captured and nxt.kind == 'cond_no':
                    next_states.append((nxt, s, memory.copy(), active_caps.copy()))

        elif node.kind == 'backref':
            group_id = node.payload
            found, frag = self._get_backref_fragment(memory, group_id)
            if not found:
                return next_states
            if frag and not self._char_satisfies_next_word_expectation(frag[:1], active_caps):
                return next_states
            next_s = s + frag
            next_active = self._clear_next_word_expectation(active_caps)
            for nxt in node.next:
                next_states.append((nxt, next_s, memory.copy(), next_active.copy()))

        elif node.kind == 'anchor':
            for nxt in node.next:
                if not self._anchor_allows(node, s, nxt):
                    continue
                next_active = self._mark_after_end_anchor(active_caps) if node.payload in {"end", "end_string"} else active_caps.copy()
                expected_wordness = self._boundary_expected_wordness(node, s)
                if expected_wordness is not None:
                    next_active = self._mark_next_word_expectation(next_active, expected_wordness)
                next_states.append((nxt, s, memory.copy(), next_active))

        elif node.kind == 'lookaround':
            rng = random.Random(len(s) + len(next_states))
            for frag, next_mem in self._apply_lookaround_fragments(s, node.payload, rng, 0, memory):
                next_s = s + frag
                for nxt in node.next:
                    next_states.append((nxt, next_s, next_mem, active_caps.copy()))
        
        elif node.kind in ['split', 'start', 'group_start', 'group_end', 'cond_yes', 'cond_no', 'atomic']:
            # Pass through nodes - just continue
            for nxt in self._allowed_pass_through_next_nodes(node, active_caps):
                next_states.append((nxt, s, memory.copy(), active_caps.copy()))
        
        else:
            # Unknown node type - pass through
            for nxt in node.next:
                next_states.append((nxt, s, memory.copy(), active_caps.copy()))
        
        return next_states
    
    def _compute_path_counts_from_nodes(self):
        """
        Compute the number of paths from each node to END.
        Used for weighted random sampling to ensure diversity.

        FIX: was recursive DFS with no depth limit — deep graphs hit Python's
        recursion limit, and a missed cycle would recurse forever.
        Replaced with iterative post-order traversal (topological sort order).
        """
        from collections import deque as _deque

        memo = {}

        # Iterative post-order DFS using explicit stack
        # Stack entries: (node, visited_set, phase)
        # phase 0 = first visit (push children), phase 1 = compute (children done)
        stack = [(self.start_node, frozenset(), 0)]
        while stack:
            node, visited, phase = stack.pop()

            if node in memo:
                continue

            if node.kind == 'end':
                memo[node] = 1
                continue

            if node in visited:      # cycle — treat as 1 path
                memo[node] = 1
                continue

            if phase == 0:
                # Re-push self in phase 1, then push all children in phase 0
                stack.append((node, visited, 1))
                new_visited = visited | {node}
                for nxt in node.next:
                    if nxt not in memo:
                        stack.append((nxt, new_visited, 0))
            else:
                # All reachable children should be in memo now
                total = 0
                char_count = node.char_count if hasattr(node, 'char_count') else 1
                for nxt in node.next:
                    child_val = memo.get(nxt, 1)
                    if node.kind in ['match', 'class', 'any']:
                        total += char_count * child_val
                    else:
                        total += child_val
                memo[node] = max(total, 1)

        return memo
    
    def _generate_random_path(self, path_counts, random):
        """
        Generate one random path through the graph.
        Uses weighted selection at choice points based on downstream path counts.
        """
        node = self.start_node
        s = ""
        memory = {}
        active_caps = {}
        
        max_steps = 1000  # Prevent infinite loops
        steps = 0
        
        while node.kind != 'end' and steps < max_steps:
            steps += 1
            
            if node.kind == 'match':
                if not self._char_satisfies_next_word_expectation(node.payload[:1], active_caps):
                    return None
                s += node.payload
                active_caps = self._clear_next_word_expectation(active_caps)
                if node.next:
                    node = node.next[0]
                else:
                    break
            
            elif node.kind in ['class', 'any']:
                # Randomly select a character from the set
                char_set = self._chars_allowed_by_next_word_expectation(list(node.payload), active_caps)
                if char_set:
                    s += random.choice(char_set)
                    active_caps = self._clear_next_word_expectation(active_caps)
                else:
                    return None
                if node.next:
                    node = node.next[0]
                else:
                    break
            
            elif node.kind == 'group_start':
                active_caps[node.payload] = len(s)
                if node.next:
                    node = node.next[0]
                else:
                    break
            
            elif node.kind == 'group_end':
                start_idx = active_caps.get(node.payload)
                if start_idx is not None:
                    memory[node.payload] = s[start_idx:]
                next_nodes = self._allowed_pass_through_next_nodes(node, active_caps)
                if next_nodes:
                    node = next_nodes[0]
                else:
                    break

            elif node.kind == 'conditional':
                group_captured = bool(memory.get(node.payload))
                chosen = None
                for nxt in node.next:
                    if group_captured and nxt.kind == 'cond_yes':
                        chosen = nxt
                        break
                    if not group_captured and nxt.kind == 'cond_no':
                        chosen = nxt
                if chosen is not None:
                    node = chosen
                elif node.next:
                    node = node.next[0]
                else:
                    break

            elif node.kind in ['cond_yes', 'cond_no', 'atomic']:
                next_nodes = self._allowed_pass_through_next_nodes(node, active_caps)
                if next_nodes:
                    node = next_nodes[0]
                else:
                    break
            
            elif node.kind == 'backref':
                group_id = node.payload
                found, frag = self._get_backref_fragment(memory, group_id)
                if not found:
                    return None
                if frag and not self._char_satisfies_next_word_expectation(frag[:1], active_caps):
                    return None
                s += frag
                active_caps = self._clear_next_word_expectation(active_caps)
                if node.next:
                    node = node.next[0]
                else:
                    break
            
            elif node.kind == 'split':
                next_nodes = self._allowed_pass_through_next_nodes(node, active_caps)
                if not next_nodes:
                    break
                # Check if this is a loop node (quantifier)
                if 'Loop' in node.label:
                    # Extract min and max from label like "Loop 1-3"
                    try:
                        min_rep, max_rep = self._parse_loop_bounds(node.label)
                        if min_rep is not None and max_rep is not None:
                            
                            # Use smart quantifier sampling
                            chosen_count = self._choose_repeat_count(min_rep, max_rep, random)
                            
                            # Find the path that represents the chosen count
                            # In our graph, we have branches for 0, 1, 2, ... max_rep iterations
                            # We need to pick the branch that matches chosen_count
                            
                            # Since we unrolled loops, each branch represents a specific count
                            # Branch 0: skip (0 iterations)
                            # Branch 1: 1 iteration
                            # Branch 2: 2 iterations, etc.
                            
                            if chosen_count < len(next_nodes):
                                node = next_nodes[chosen_count]
                            elif next_nodes:
                                # If chosen_count exceeds branches, take the last one
                                node = next_nodes[-1]
                            else:
                                break
                            continue
                    except:
                        pass
                
                # Regular split (not a loop) - weighted random choice
                if next_nodes:
                    # Weight by downstream path counts
                    weights = [path_counts.get(n, 1) for n in next_nodes]
                    total_weight = sum(weights)
                    if total_weight > 0:
                        # Normalize and choose
                        probs = [w / total_weight for w in weights]
                        node = random.choices(next_nodes, weights=probs)[0]
                    else:
                        node = random.choice(next_nodes)
                else:
                    break

            elif node.kind == 'anchor':
                next_nodes = [nxt for nxt in node.next if self._anchor_allows(node, s, nxt)]
                if not next_nodes:
                    return None
                if node.payload in {"end", "end_string"}:
                    active_caps = self._mark_after_end_anchor(active_caps)
                expected_wordness = self._boundary_expected_wordness(node, s)
                if expected_wordness is not None:
                    active_caps = self._mark_next_word_expectation(active_caps, expected_wordness)
                node = next_nodes[0]

            elif node.kind == 'lookaround':
                fragments = self._apply_lookaround_fragments(s, node.payload, random, 0, memory)
                if not fragments:
                    return None
                frag, memory = random.choice(fragments)
                s += frag
                if node.next:
                    node = node.next[0]
                else:
                    break
            
            else:
                # Pass-through nodes
                next_nodes = self._allowed_pass_through_next_nodes(node, active_caps)
                if next_nodes:
                    node = next_nodes[0]
                else:
                    break
        
        clean_s = self._clean_conditional_markers(s, memory)
        if clean_s is None:
            return None
        if not self._should_accept_positive(clean_s):
            return None
        return clean_s
    
    def _choose_repeat_count(self, min_rep, max_rep, rng):
        """
        Smart quantifier sampling rules for diverse test case generation.
        
        Patterns:
        * or {0,}        -> 0, 1, 2              (common cases)
        + or {1,}        -> 1, 2, 3              (common cases)
        ? or {0,1}       -> 0, 1, 2              (2 is overflow for negative tests)
        {0,n} or {,n}    -> 0, 1, n, n+1         (min, typical, max, overflow)
        {m,n}            -> m, random(m,n), n    (min, middle, max)
        {m} (m>0)        -> 1, random(1,m), m    (underflow, middle, exact)
        {m,} (m>1)       -> m, m+random(0..3)    (min, slightly more)
        
        Args:
            min_rep: Minimum repetitions
            max_rep: Maximum repetitions (could be MAX_LOOP_UNROLL for unbounded)
            rng: Random number generator
            
        Returns:
            Chosen repetition count
        """
        # Detect pattern type based on min/max
        
        # Pattern: * or {0,}
        if min_rep == 0 and max_rep >= MAX_LOOP_UNROLL:
            return rng.choice([0, 1, 2])
        
        # Pattern: + or {1,}
        elif min_rep == 1 and max_rep >= MAX_LOOP_UNROLL:
            return rng.choice([1, 2, 3])
        
        # Pattern: ? or {0,1}
        elif min_rep == 0 and max_rep == 1:
            return rng.choice([0, 1, 2])  # 2 is overflow for testing
        
        # Pattern: {0,n} where n is specific
        elif min_rep == 0 and max_rep < MAX_LOOP_UNROLL:
            choices = [0, 1, max_rep]
            if max_rep > 1:
                choices.append(max_rep + 1)  # overflow case
            return rng.choice(choices)
        
        # Pattern: {m,n} with both bounds
        elif min_rep > 0 and max_rep < MAX_LOOP_UNROLL and max_rep > min_rep:
            # Choose from: min, random middle, max
            if max_rep - min_rep > 2:
                mid = rng.randint(min_rep + 1, max_rep - 1)
                return rng.choice([min_rep, mid, max_rep])
            else:
                return rng.choice([min_rep, max_rep])
        
        # Pattern: {m} (exact count)
        elif min_rep == max_rep and min_rep > 0:
            if min_rep == 1:
                return 1
            elif min_rep == 2:
                return rng.choice([1, 2])  # underflow, exact
            else:
                mid = rng.randint(1, min_rep - 1)
                return rng.choice([1, mid, min_rep])  # underflow, middle, exact
        
        # Pattern: {m,} where m > 1 (unbounded from m)
        elif min_rep > 1 and max_rep >= MAX_LOOP_UNROLL:
            offset = rng.randint(0, 3)
            return rng.choice([min_rep, min_rep + offset])
        
        # Default: random within range
        else:
            if max_rep > min_rep:
                return rng.randint(min_rep, max_rep)
            else:
                return min_rep
    
    def _generate_bfs_sample(self, count):
        """
        Fallback: BFS-based sampling with random selection at each level.
        Used when stratified sampling doesn't generate enough samples.
        """
        import random
        from collections import deque
        
        results = set()
        queue = deque([(self.start_node, "", {}, {})])
        
        iterations = 0
        max_iterations = count * 100
        # FIX: iterations counted dequeues but queue grew unboundedly inside the
        # limit because each dequeue could push many states. Cap queue size too.
        MAX_QUEUE = 50_000

        while queue and len(results) < count and iterations < max_iterations:
            iterations += 1

            # FIX: trim queue if it balloons
            if len(queue) > MAX_QUEUE:
                # Keep a random subset to maintain diversity
                items = list(queue)
                random.shuffle(items)
                queue = deque(items[:MAX_QUEUE // 2])

            node, s, memory, active_caps = queue.popleft()
            next_states = self._get_next_states(node, s, memory, active_caps)

            # For class nodes, sample randomly instead of all
            if node.kind in ['class', 'any'] and len(next_states) > 5:
                next_states = random.sample(next_states, min(5, len(next_states)))

            for state in next_states:
                next_node, next_s, next_mem, next_active = state
                if next_node.kind == 'end':
                    clean_s = self._clean_conditional_markers(next_s, next_mem)
                    if self._should_accept_positive(clean_s):
                        results.add(clean_s)
                else:
                    queue.append(state)
        
        return results
    
    def _clean_conditional_markers(self, s, memory):
        """
        Remove conditional markers from the string and validate conditions.
        Returns None if the conditional is invalid for the given memory state.
        """
        import re as re_std
        
        # Find all conditional markers in the string
        # Pattern: _COND{group_id}Y_{content} or _COND{group_id}N_{content}
        marker_pattern = r'_COND(\d+)([YN])_([^_]*)'
        
        result = s
        for match in re_std.finditer(marker_pattern, s):
            group_id = int(match.group(1))
            branch_type = match.group(2)  # 'Y' for yes branch, 'N' for no branch
            content = match.group(3)
            
            # Check if the group was captured
            group_captured = group_id in memory and memory[group_id]
            
            # Validate the conditional
            if branch_type == 'Y' and not group_captured:
                # Yes branch taken but group didn't match - invalid
                return None
            elif branch_type == 'N' and group_captured:
                # No branch taken but group did match - invalid
                return None
            
            # Replace the marker with just the content
            result = result.replace(match.group(0), content)
        
        return result

    def _regex_fullmatch(self, s):
        try:
            engine = regex if REGEX_AVAILABLE else re
            return engine.fullmatch(self.regex, s, self.flags) is not None
        except Exception:
            return False

    def _should_accept_positive(self, s):
        if s is None:
            return False
        if not self.validate:
            return True
        if self.requires_match_validation:
            return self._regex_fullmatch(s)
        return True

    def _select_relevant_violation_types(self):
        """
        Decide which violation types are interesting for the given regex.
        """
        has_unbounded = any(q in self.regex for q in ['*', '+'])
        # Matches {m,n} or {m}
        has_bounded = bool(re.search(r'\{\d+,?\d*\}', self.regex))
        
        has_anchors = any(n.kind == 'anchor' for n in self.nodes)
        has_backref = any(n.kind == 'backref' for n in self.nodes)
        has_class = any(n.kind in ['class', 'any'] for n in self.nodes)
        
        selected = []

        # 1. Add Lookaround/Conditional first if they exist
        if self.lookarounds:
            if any('lookahead' in l['type'] for l in self.lookarounds):
                selected.append('lookahead_violation')
            if any('lookbehind' in l['type'] for l in self.lookarounds):
                selected.append('lookbehind_violation')
        
        if any(n.kind == 'conditional' for n in self.nodes):
            selected.append('conditional_violation')

        # 2. Class-related
        if has_class:
            selected.extend(['character_class_violation', 'wrong_char_substitution'])

        # 3. Quantifiers: {m,n} gets above_max, others get below_min
        if has_unbounded or has_bounded:
            selected.append('quantifier_below_min')
        if has_bounded:
            selected.append('quantifier_above_max')

        # 4. Feature specific
        if has_anchors: selected.append('anchor_violation')
        if has_backref: selected.append('backreference_violation')

        # 5. Always useful structural mutations
        selected.extend(['missing_required_char', 'structure_violation'])

        return selected


    def generate_negative_samples(self, num_samples=10, *, seed=None,
                                  max_attempts=None, use_fullmatch=False,
                                  timeout_seconds=DEFAULT_REGEX_TIMEOUT_SECONDS):
        """
        Randomized negative sample generation.
        - Diverse outputs via randomized choices inside each violation generator
        - Validated to actually FAIL the regex
        - Optional seed for reproducibility
        """
        def _generate():
            rng = random.Random(seed)

            negative_tests = set()

            violation_types = self._select_relevant_violation_types()
            # print(f"Selected violation types for negative samples: {violation_types}")
            if not violation_types:
                violation_types = ['structure_violation', 'insert_invalid_char']

            rng.shuffle(violation_types)

            mapping = {
                'character_class_violation': lambda: self._generate_class_violations(rng=rng),
                'quantifier_below_min':      lambda: self._generate_quantifier_below_violations(rng=rng),
                'quantifier_above_max':      lambda: self._generate_quantifier_above_violations(rng=rng),
                'missing_required_char':     lambda: self._generate_missing_char_violations(rng=rng),
                'wrong_char_substitution':   lambda: self._generate_substitution_violations(rng=rng),
                'insert_invalid_char':       lambda: self._generate_insertion_violations(rng=rng),
                'delete_required_char':      lambda: self._generate_deletion_violations(rng=rng),
                'anchor_violation':          lambda: self._generate_anchor_violations(rng=rng),
                'structure_violation':       lambda: self._generate_structure_violations(rng=rng),
                'backreference_violation':   lambda: self._generate_backref_violations(rng=rng),
                'lookahead_violation':       lambda: self._generate_lookahead_violations(rng=rng),
                'lookbehind_violation':      lambda: self._generate_lookbehind_violations(rng=rng),
                'conditional_violation':     lambda: self._generate_conditional_violations(rng=rng),
            }

            local_max_attempts = max_attempts
            if local_max_attempts is None:
                local_max_attempts = num_samples * 200

            def fails_regex(s: str) -> bool:
                try:
                    import re as _re_std
                    _engine = regex if REGEX_AVAILABLE else _re_std
                    if use_fullmatch:
                        return _engine.fullmatch(self.regex, s) is None
                    return _engine.search(self.regex, s) is None
                except Exception:
                    return False

            attempts = 0
            while len(negative_tests) < num_samples and attempts < local_max_attempts:
                attempts += 1
                v_type = rng.choice(violation_types)
                gen = mapping.get(v_type)
                if not gen:
                    continue

                candidates = gen() or []
                # print(f"Attempt {attempts}: Generating violation of type '{v_type}', got {len(candidates)} candidates")
                if not candidates:
                    continue

                rng.shuffle(candidates)
                for s in candidates[: rng.randint(3, 12)]:
                    if len(negative_tests) >= num_samples:
                        break
                    if s is None:
                        continue
                    if fails_regex(s):
                        negative_tests.add(s)
                        break

            return rng.sample(sorted(negative_tests), k=min(num_samples, len(negative_tests)))

        return _run_with_timeout(_generate, timeout_seconds)


    def _get_valid_samples_cache(self, *, rng=None):
        if self.valid_samples_cache is None:
            # if your generate_sample supports randomness/seed, pass it through
            self.valid_samples_cache = self.generate_sample(50)
        return self.valid_samples_cache


    def _random_char_pool(self):
        # bigger pool than string.printable, but still “safe-ish”
        pool = []
        pool.extend(string.ascii_letters)
        pool.extend(string.digits)
        pool.extend(string.punctuation)
        pool.extend([" ", "\t", "\n"])
        # a few non-ascii for diversity (helps break ASCII-only classes)
        pool.extend(list("äöüßéèàçñøΩЖ中あ"))
        return pool


    def _get_char_outside_class(self, char_set, *, rng):
        # char_set might be set/list/str; normalize
        if char_set is None:
            char_set = set()
        if isinstance(char_set, str):
            char_set = set(char_set)
        else:
            char_set = set(char_set)

        pool = self._random_char_pool()
        outside = [c for c in pool if c not in char_set]
        if outside:
            return rng.choice(outside)

        # fallback: try random unicode range
        for _ in range(50):
            c = chr(rng.randint(0x20, 0x2FFF))
            if c not in char_set:
                return c
        return "!"


    def _random_outside_ascii_word(self, *, rng):
        # good for breaking \w / [A-Za-z0-9_]
        return rng.choice(["-", ".", " ", "\n", "中", "Ж", "Ω", "ä", "!", "@"])


    def _generate_class_violations(self, *, rng, max_nodes=1, per_node=1):
        violations = []

        class_nodes = [n for n in self.nodes if n.kind in ['class', 'any']]
        if not class_nodes:
            return violations

        rng.shuffle(class_nodes)
        for node in class_nodes[:max_nodes]:
            path = self._find_path_to_node(node)
            if not path:
                continue

            prefix = self._build_string_from_path(path[:-1], rng=rng)

            for _ in range(per_node):
                invalid_char = self._get_char_outside_class(node.payload, rng=rng)
                # also try adding random suffix noise
                suffix = ""
                if rng.random() < 0.6:
                    suffix = "".join(rng.choice(self._random_char_pool()) for _ in range(rng.randint(0, 4)))
                violations.append(prefix + invalid_char + suffix)

        return violations


    def _generate_quantifier_below_violations(self, *, rng, per_loop=1):
        violations = []
        loops = [n for n in self.nodes if n.kind == 'split' and 'Loop' in str(n.label)]
        rng.shuffle(loops)

        for node in loops:
            try:
                min_val, _ = self._parse_loop_bounds(node.label)
                if min_val is None:
                    continue
                if min_val <= 0:
                    continue

                for _ in range(per_loop):
                    reps = rng.randint(0, max(0, min_val - 1))
                    s = self._build_string_with_repetitions(node, reps, rng=rng)
                    if s:
                        # sometimes truncate or add junk to break more
                        if rng.random() < 0.4 and len(s) > 0:
                            s = s[:-rng.randint(1, min(3, len(s)))]
                        if rng.random() < 0.3:
                            s += rng.choice(self._random_char_pool())
                        violations.append(s)
            except Exception:
                continue

        return violations


    def _generate_quantifier_above_violations(self, *, rng, per_loop=1):
        violations = []
        loops = [n for n in self.nodes if n.kind == 'split' and 'Loop' in str(n.label)]
        rng.shuffle(loops)

        for node in loops:
            try:
                _, max_val = self._parse_loop_bounds(node.label)
                if max_val is None:
                    continue

                # don’t explode
                cap = min(25, max_val + 10)
                for _ in range(per_loop):
                    reps = rng.randint(max_val + 1, cap) if max_val >= 0 else rng.randint(1, 10)
                    s = self._build_string_with_repetitions(node, reps, rng=rng)
                    if s:
                        # sometimes wrap with extra noise
                        if rng.random() < 0.5:
                            s = rng.choice(["X", "__", " "]) + s
                        if rng.random() < 0.5:
                            s = s + rng.choice(["Y", "__", "\n"])
                        violations.append(s)
            except Exception:
                continue

        return violations


    def _generate_missing_char_violations(self, *, rng, per_sample=1):
        violations = []
        valids = self._get_valid_samples_cache(rng=rng)
        if not valids:
            return violations

        samples = rng.sample(valids, k=min(10, len(valids)))
        for s in samples:
            if len(s) == 0:
                continue
            for _ in range(per_sample):
                if len(s) == 1:
                    violations.append("")
                    continue
                i = rng.randrange(len(s))
                violations.append(s[:i] + s[i+1:])
                # sometimes delete a slice
                if len(s) > 3 and rng.random() < 0.4:
                    a = rng.randrange(len(s))
                    b = rng.randrange(a, min(len(s), a + rng.randint(1, 3)))
                    violations.append(s[:a] + s[b:])

        return violations


    def _generate_substitution_violations(self, *, rng, per_sample=1):
        violations = []
        valids = self._get_valid_samples_cache(rng=rng)
        if not valids:
            return violations

        samples = rng.sample(valids, k=min(10, len(valids)))
        pool = self._random_char_pool()

        for s in samples:
            if not s:
                continue
            for _ in range(per_sample):
                i = rng.randrange(len(s))
                ch = s[i]

                # pick a “different type” char often
                if ch.isdigit():
                    new = rng.choice(list(string.ascii_letters) + ["_", "-", "中"])
                elif ch.isalpha():
                    new = rng.choice(list(string.digits) + ["@", "!", " "])
                elif ch == "_":
                    new = rng.choice(["-", ".", " ", "9"])
                else:
                    new = rng.choice(list(string.ascii_letters) + list(string.digits))

                if new == ch:
                    new = rng.choice(pool)

                t = s[:i] + new + s[i+1:]

                # sometimes change multiple positions
                if len(s) > 3 and rng.random() < 0.5:
                    j = rng.randrange(len(s))
                    if j != i:
                        t = t[:j] + rng.choice(pool) + t[j+1:]

                violations.append(t)

        return violations


    def _generate_insertion_violations(self, *, rng, per_sample=1):
        violations = []
        valids = self._get_valid_samples_cache(rng=rng)
        if not valids:
            return violations

        samples = rng.sample(valids, k=min(10, len(valids)))
        pool = self._random_char_pool()

        for s in samples:
            for _ in range(per_sample):
                pos = rng.randrange(len(s) + 1)
                ins_len = 1 if rng.random() < 0.7 else rng.randint(2, 5)
                blob = "".join(rng.choice(pool) for _ in range(ins_len))
                violations.append(s[:pos] + blob + s[pos:])

        return violations


    def _generate_deletion_violations(self, *, rng, per_sample=1):
        violations = []
        valids = self._get_valid_samples_cache(rng=rng)
        if not valids:
            return violations

        samples = rng.sample(valids, k=min(10, len(valids)))
        for s in samples:
            if len(s) <= 1:
                continue

            for _ in range(per_sample):
                a = rng.randrange(len(s))
                b = rng.randrange(a + 1, min(len(s) + 1, a + rng.randint(2, 6)))
                violations.append(s[:a] + s[b:])

            # also “thin out” with random mask
            if len(s) > 4:
                mask = [c for c in s if rng.random() < 0.5]
                violations.append("".join(mask))

        return violations


    def _generate_anchor_violations(self, *, rng, per_sample=1):
        violations = []
        has_start = any(n.kind == 'anchor' and n.payload == 'start' for n in self.nodes)
        has_end = any(n.kind == 'anchor' and n.payload == 'end' for n in self.nodes)

        valids = self._get_valid_samples_cache(rng=rng)
        if not valids:
            valids = ["a", "1", "test"]

        samples = rng.sample(valids, k=min(10, len(valids)))
        pool = self._random_char_pool()

        for s in samples:
            for _ in range(per_sample):
                pref = "".join(rng.choice(pool) for _ in range(rng.randint(1, 4)))
                suf = "".join(rng.choice(pool) for _ in range(rng.randint(1, 4)))

                if has_start and rng.random() < 0.7:
                    violations.append(pref + s)
                if has_end and rng.random() < 0.7:
                    violations.append(s + suf)

                # even without anchors, adding noise often breaks structure-based patterns
                if rng.random() < 0.3:
                    violations.append(pref + s + suf)

        return violations

    def _generate_structure_violations(self, *, rng, per_sample=1):
        violations = []
        valids = self._get_valid_samples_cache(rng=rng)
        if not valids:
            return violations

        samples = rng.sample(valids, k=min(10, len(valids)))
        pool = self._random_char_pool()

        for s in samples:
            if not s:
                violations.append(rng.choice(pool)) # Add noise to empty
                continue

            for _ in range(per_sample):
                mode = rng.choice(["reverse", "shuffle", "rotate", "dup_chunk", "drop_chunk", "noise"])
                
                if mode == "reverse":
                    violations.append(s[::-1])
                elif mode == "shuffle" and len(s) > 1:
                    chars = list(s)
                    rng.shuffle(chars)
                    violations.append("".join(chars))
                elif mode == "rotate" and len(s) > 1:
                    k = rng.randint(1, len(s))
                    violations.append(s[k:] + s[:k])
                elif mode == "dup_chunk" and len(s) >= 1:
                    a = rng.randrange(len(s))
                    # FIX: Ensure b is at least a + 1
                    b = rng.randint(a + 1, len(s)) 
                    chunk = s[a:b]
                    pos = rng.randrange(len(s) + 1)
                    violations.append(s[:pos] + chunk + s[pos:])
                elif mode == "drop_chunk" and len(s) > 1:
                    a = rng.randrange(len(s))
                    # FIX: Ensure b is at least a + 1
                    b = rng.randint(a + 1, len(s))
                    violations.append(s[:a] + s[b:])
                else:
                    # noise
                    t = list(s)
                    for _ in range(rng.randint(1, 3)):
                        i = rng.randint(0, len(t))
                        t.insert(i, rng.choice(pool))
                    violations.append("".join(t))

        return violations

    
    def _build_string_from_path(self, path, *, rng):
        out = []
        for node in path:
            if node.kind == 'match':
                out.append(node.payload)
            elif node.kind in ['class', 'any']:
                payload = node.payload
                if payload:
                    if isinstance(payload, str):
                        payload = list(payload)
                    else:
                        payload = list(payload)
                    out.append(rng.choice(payload))
                else:
                    out.append(rng.choice(self._random_char_pool()))
        return "".join(out)


    def _build_string_with_repetitions(self, loop_node, num_reps, *, rng):
        # still heuristic, but randomized so outputs diversify
        if not loop_node.next:
            return None

        # try to pick a representative token from loop body
        candidates = []
        for nxt in loop_node.next:
            if nxt.kind == 'match' and nxt.payload:
                candidates.append(nxt.payload)
            elif nxt.kind in ['class', 'any'] and nxt.payload:
                payload = list(nxt.payload) if isinstance(nxt.payload, str) else list(nxt.payload)
                if payload:
                    candidates.append(rng.choice(payload))

        if not candidates:
            candidates = [rng.choice(self._random_char_pool())]

        base = rng.choice(candidates)

        # sometimes vary per repetition instead of repeating same char
        if rng.random() < 0.5 and len(base) == 1:
            pool = candidates if len(candidates) > 1 else self._random_char_pool()
            return "".join(rng.choice(pool) for _ in range(num_reps))

        return base * num_reps

    def _generate_backref_violations(self, *, rng):
        violations = []
        valids = self._get_valid_samples_cache(rng=rng)
        samples = rng.sample(valids, k=min(5, len(valids)))
        for s in samples:
            if len(s) > 2:
                # Use rng instead of random.choice
                violations.append(s[:-1] + (rng.choice(string.ascii_letters)))
        return violations

    def _generate_lookahead_violations(self, *, rng):
        violations = []
        valids = self._get_valid_samples_cache(rng=rng)
        for la in self.lookarounds:
            if 'lookahead' in la['type']:
                content = la['content']
                for s in rng.sample(valids, k=min(3, len(valids))):
                    violations.append(s + "WRONG" + content)
        return violations

    def _generate_lookbehind_violations(self, *, rng):
        violations = []
        valids = self._get_valid_samples_cache(rng=rng)
        for lb in self.lookarounds:
            if 'lookbehind' in lb['type']:
                for s in rng.sample(valids, k=min(3, len(valids))):
                    content_len = len(lb['content'])
                    violations.append(s[content_len:] if len(s) > content_len else rng.choice(["X", "!", "9"]))
        return violations

    def _generate_conditional_violations(self, *, rng):
        valids = self._get_valid_samples_cache(rng=rng)
        samples = rng.sample(valids, k=min(5, len(valids)))
        # Diversify by reversing or shuffling
        return [s[::-1] if rng.random() > 0.5 else "".join(rng.sample(list(s), len(s))) for s in samples]

    def _find_path_to_node(self, target_node):
        """BFS to find a path from start to target node"""
        from collections import deque
        queue = deque([(self.start_node, [self.start_node])])
        visited = set()
        
        while queue:
            node, path = queue.popleft()
            if node == target_node:
                return path
            
            if node in visited:
                continue
            visited.add(node)
            
            for next_node in node.next:
                queue.append((next_node, path + [next_node]))
        
        return None



def _run_with_timeout(func, timeout_seconds):
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

class RegexGraphBuilder(_BaseRegexGraphBuilder):
    """
    Two-phase sampler:
    1. Force local heuristic obligations (branches, class representatives, repeats).
    2. Fill the remaining budget with rank-bucket stratified sampling.

    This keeps heuristic coverage systematic without the combinatorial expansion
    behavior of `_heuristic_expand_sequence`.
    """

    HEURISTIC_CAP = 100
    HEURISTIC_RETRY_LIMIT = 8
    DIVERSE_SEED_CAP = 256

    def __init__(self, regex_pattern, flags=0, validate=True):
        super().__init__(regex_pattern, flags=flags, validate=validate)
        self._heuristic_specs = {}
        self._heuristic_order = []
        self._collect_heuristic_specs(self.parsed_tree)

    def generate_sample(self, max_samples=None, force_complete=False,
                        timeout_seconds=DEFAULT_REGEX_TIMEOUT_SECONDS, seed=None):
        return self.generate_diverse_samples(
            n=1_000 if max_samples is None else max_samples,
            seed=seed,
            timeout_seconds=timeout_seconds,
        )

    def generate_heuristic_samples(self, seed=None,
                                   timeout_seconds=DEFAULT_REGEX_TIMEOUT_SECONDS,
                                   max_samples=None):
        target = self.HEURISTIC_CAP if max_samples is None else min(max_samples, self.HEURISTIC_CAP)
        return _run_with_timeout(
            lambda: self._generate_heuristic_obligation_samples(target_count=target, seed=seed),
            timeout_seconds,
        )

    def generate_diverse_samples(self, n=1000, seed=None,
                                 timeout_seconds=DEFAULT_REGEX_TIMEOUT_SECONDS):
        def _generate():
            total_count, has_backref = self.count_paths()
            if total_count <= n and not has_backref:
                return sorted(self._generate_exhaustive(n))

            rng = random.Random(seed)
            seen = set()
            results = []

            seed_budget = min(n, self.DIVERSE_SEED_CAP)
            for s in self._generate_heuristic_obligation_samples(seed_budget, seed=seed):
                if s not in seen:
                    seen.add(s)
                    results.append(s)
                    if len(results) >= n:
                        return results[:n]

            remaining = n - len(results)
            if remaining > 0:
                for s in self._generate_rank_bucket_samples(
                    remaining,
                    seed=rng.randint(0, 10**9),
                    existing=seen,
                ):
                    if s not in seen:
                        seen.add(s)
                        results.append(s)
                        if len(results) >= n:
                            return results[:n]

            if len(results) < n:
                path_counts = self._compute_path_counts_from_nodes()
                for _ in range(min(max((n - len(results)) * 8, 256), 50_000)):
                    s = self._generate_random_path(path_counts, rng)
                    if s is None or s in seen:
                        continue
                    seen.add(s)
                    results.append(s)
                    if len(results) >= n:
                        break

            return results[:n]

        return _run_with_timeout(_generate, timeout_seconds)

    def _collect_heuristic_specs(self, items, path=()):
        import sre_constants

        sequence = items if isinstance(items, (list, tuple)) else items.data
        for index, (op, av) in enumerate(sequence):
            node_path = path + (index,)

            if op == sre_constants.BRANCH:
                choices = list(range(len(av[1])))
                self._register_heuristic_spec(node_path, "branch", choices)
                for alt_index, alt in enumerate(av[1]):
                    self._collect_heuristic_specs(alt, node_path + ("branch", alt_index))

            elif op == sre_constants.SUBPATTERN:
                group_num = av[0]
                self._collect_heuristic_specs(av[3], node_path + ("group", group_num))

            elif op in (sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT) or (
                hasattr(sre_constants, "POSSESSIVE_REPEAT") and op == sre_constants.POSSESSIVE_REPEAT
            ):
                min_rep, max_rep = av[0], av[1]
                reps = self._repeat_representatives(min_rep, max_rep)
                if self._tree_has_start_or_end_anchor(av[2]):
                    reps = [rep for rep in reps if rep <= 1]
                self._register_heuristic_spec(node_path, "repeat", reps)
                self._collect_heuristic_specs(av[2], node_path + ("repeat_body",))

            elif op == sre_constants.CATEGORY:
                char_set, _, _ = self._get_category_chars(av)
                reps = self._category_representatives(char_set)
                self._register_heuristic_spec(node_path, "class", reps)

            elif op == sre_constants.IN:
                reps = self._class_representatives_from_in(av)
                self._register_heuristic_spec(node_path, "class", reps)

            elif op == sre_constants.ANY:
                chars = sorted(set(string.printable) - {"\n", "\r"})
                reps = self._dedupe_preserve_order([chars[0], chars[len(chars) // 2], chars[-1]])
                self._register_heuristic_spec(node_path, "class", reps)

            elif op == sre_constants.NOT_LITERAL:
                excluded = chr(av)
                chars = [ch for ch in string.printable if ch != excluded]
                reps = self._dedupe_preserve_order([chars[0], chars[len(chars) // 2], chars[-1]])
                self._register_heuristic_spec(node_path, "class", reps)

            elif hasattr(sre_constants, "ATOMIC_GROUP") and op == sre_constants.ATOMIC_GROUP:
                self._collect_heuristic_specs(av, node_path + ("atomic",))

    def _register_heuristic_spec(self, node_path, kind, choices):
        choices = self._dedupe_preserve_order(choices)
        if not choices:
            return
        self._heuristic_specs[node_path] = {
            "kind": kind,
            "choices": choices,
        }
        self._heuristic_order.append(node_path)

    def _repeat_representatives(self, min_rep, max_rep):
        import sre_constants

        if max_rep == sre_constants.MAXREPEAT:
            capped_max = max(min_rep + 2, min_rep + MAX_LOOP_UNROLL - 1)
        else:
            capped_max = max_rep

        if capped_max <= min_rep:
            return [min_rep]

        middle = min_rep + ((capped_max - min_rep) // 2)
        return self._dedupe_preserve_order([min_rep, middle, capped_max])

    def _class_representatives_from_in(self, av):
        import sre_constants

        literals = []
        digits = []
        lowers = []
        uppers = []
        others = []
        negated = False
        char_set = set()

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
            char_set = set(string.printable) - char_set

        for ch in sorted(char_set):
            if ch.isdigit():
                digits.append(ch)
            elif "A" <= ch <= "Z":
                uppers.append(ch)
            elif "a" <= ch <= "z":
                lowers.append(ch)
            elif ch in string.punctuation:
                literals.append(ch)
            else:
                others.append(ch)

        reps = []
        reps.extend(self._bucket_representatives(literals))
        reps.extend(self._bucket_representatives(digits))
        reps.extend(self._bucket_representatives(lowers))
        reps.extend(self._bucket_representatives(uppers))
        reps.extend(self._bucket_representatives(others))

        if not reps and char_set:
            chars = sorted(char_set)
            reps = [chars[0], chars[len(chars) // 2], chars[-1]]

        return self._dedupe_preserve_order(reps)

    def _category_representatives(self, char_set):
        chars = sorted(char_set)
        if not chars:
            return []

        digits = [ch for ch in chars if ch.isdigit()]
        lowers = [ch for ch in chars if "a" <= ch <= "z"]
        uppers = [ch for ch in chars if "A" <= ch <= "Z"]
        punct = [ch for ch in chars if ch in string.punctuation]
        spaces = [ch for ch in chars if ch in string.whitespace]
        others = [ch for ch in chars if ch not in set(digits + lowers + uppers + punct + spaces)]

        reps = []
        reps.extend(self._bucket_representatives(punct))
        reps.extend(self._bucket_representatives(digits))
        reps.extend(self._bucket_representatives(lowers))
        reps.extend(self._bucket_representatives(uppers))
        reps.extend(self._bucket_representatives(spaces))
        reps.extend(self._bucket_representatives(others))
        return self._dedupe_preserve_order(reps)

    def _bucket_representatives(self, bucket):
        if not bucket:
            return []
        if len(bucket) == 1:
            return [bucket[0]]
        if len(bucket) == 2:
            return [bucket[0], bucket[1]]
        return [bucket[0], bucket[-1], bucket[len(bucket) // 2]]

    def _heuristic_tasks(self):
        tasks = []
        kind_order = {"branch": 0, "class": 1, "repeat": 2}
        for node_path in sorted(
            self._heuristic_order,
            key=lambda item: (kind_order.get(self._heuristic_specs[item]["kind"], 99), item),
        ):
            spec = self._heuristic_specs[node_path]
            for choice in spec["choices"]:
                tasks.append((node_path, choice))
        return tasks

    def _generate_heuristic_obligation_samples(self, target_count, seed=None):
        if target_count <= 0:
            return []

        rng = random.Random(seed)
        tasks = self._heuristic_tasks()
        cursors = {node_path: 0 for node_path in self._heuristic_order}
        seen = set()
        results = []

        for node_path, choice in tasks:
            if len(results) >= target_count:
                return results[:target_count]
            candidate = self._generate_candidate_with_constraints(
                constraints={node_path: choice},
                cursors=cursors,
                rng=rng,
                existing=seen,
            )
            if candidate is not None:
                seen.add(candidate)
                results.append(candidate)

        while len(results) < target_count:
            candidate = self._generate_candidate_with_constraints(
                constraints={},
                cursors=cursors,
                rng=rng,
                existing=seen,
            )
            if candidate is None:
                break
            seen.add(candidate)
            results.append(candidate)

        return results[:target_count]

    def _generate_candidate_with_constraints(self, constraints, cursors, rng, existing):
        for retry in range(self.HEURISTIC_RETRY_LIMIT):
            local_cursors = dict(cursors)
            try:
                sample = self._emit_sequence(
                    self.parsed_tree,
                    path=(),
                    constraints=constraints,
                    cursors=local_cursors,
                    rng=rng,
                    memory={},
                    offset=retry,
                )
            except Exception:
                continue

            if sample is None:
                continue
            sample = self._clean_conditional_markers(sample, {})
            if sample is None or sample in existing:
                continue
            if self.validate and not self._regex_fullmatch(sample):
                continue

            cursors.clear()
            cursors.update(local_cursors)
            return sample

        return None

    def _emit_sequence(self, items, path, constraints, cursors, rng, memory, offset):
        import sre_constants

        parts = []
        sequence = items if isinstance(items, (list, tuple)) else items.data

        for index, (op, av) in enumerate(sequence):
            node_path = path + (index,)

            if op == sre_constants.LITERAL:
                ch = chr(av)
                expected = memory.get(NEXT_WORD_EXPECTATION_KEY)
                if expected is not None and self._is_word_char(ch) != expected:
                    return None
                memory.pop(NEXT_WORD_EXPECTATION_KEY, None)
                parts.append(ch)

            elif op == sre_constants.ANY:
                ch = self._choose_choice(node_path, constraints, cursors, offset)
                expected = memory.get(NEXT_WORD_EXPECTATION_KEY)
                if expected is not None and self._is_word_char(ch) != expected:
                    return None
                memory.pop(NEXT_WORD_EXPECTATION_KEY, None)
                parts.append(ch)

            elif op == sre_constants.CATEGORY:
                ch = self._choose_choice(node_path, constraints, cursors, offset)
                expected = memory.get(NEXT_WORD_EXPECTATION_KEY)
                if expected is not None and self._is_word_char(ch) != expected:
                    return None
                memory.pop(NEXT_WORD_EXPECTATION_KEY, None)
                parts.append(ch)

            elif op == sre_constants.IN:
                ch = self._choose_choice(node_path, constraints, cursors, offset)
                expected = memory.get(NEXT_WORD_EXPECTATION_KEY)
                if expected is not None and self._is_word_char(ch) != expected:
                    return None
                memory.pop(NEXT_WORD_EXPECTATION_KEY, None)
                parts.append(ch)

            elif op == sre_constants.NOT_LITERAL:
                ch = self._choose_choice(node_path, constraints, cursors, offset)
                expected = memory.get(NEXT_WORD_EXPECTATION_KEY)
                if expected is not None and self._is_word_char(ch) != expected:
                    return None
                memory.pop(NEXT_WORD_EXPECTATION_KEY, None)
                parts.append(ch)

            elif op == sre_constants.BRANCH:
                branch_index = self._choose_choice(node_path, constraints, cursors, offset)
                alt = av[1][branch_index]
                frag = self._emit_sequence(
                    alt,
                    path=node_path + ("branch", branch_index),
                    constraints=constraints,
                    cursors=cursors,
                    rng=rng,
                    memory=memory,
                    offset=offset,
                )
                if frag is None:
                    return None
                parts.append(frag)

            elif op == sre_constants.SUBPATTERN:
                group_num = av[0]
                frag = self._emit_sequence(
                    av[3],
                    path=node_path + ("group", group_num),
                    constraints=constraints,
                    cursors=cursors,
                    rng=rng,
                    memory=memory,
                    offset=offset,
                )
                if frag is None:
                    return None
                parts.append(frag)
                if group_num is not None:
                    memory[group_num] = frag

            elif op in (sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT) or (
                hasattr(sre_constants, "POSSESSIVE_REPEAT") and op == sre_constants.POSSESSIVE_REPEAT
            ):
                reps = self._choose_choice(node_path, constraints, cursors, offset)
                if reps > 1 and self._tree_has_start_or_end_anchor(av[2]):
                    return None
                for _ in range(reps):
                    frag = self._emit_sequence(
                        av[2],
                        path=node_path + ("repeat_body",),
                        constraints=constraints,
                        cursors=cursors,
                        rng=rng,
                        memory=memory,
                        offset=offset,
                    )
                    if frag is None:
                        return None
                    parts.append(frag)

            elif hasattr(sre_constants, "GROUPREF_EXISTS") and op == sre_constants.GROUPREF_EXISTS:
                group_id, yes_tree, no_tree = av
                branch = yes_tree if memory.get(group_id) else no_tree
                if branch:
                    frag = self._emit_sequence(
                        branch,
                        path=node_path + ("conditional", group_id),
                        constraints=constraints,
                        cursors=cursors,
                        rng=rng,
                        memory=memory,
                        offset=offset,
                    )
                    if frag is None:
                        return None
                    parts.append(frag)

            elif op == sre_constants.GROUPREF:
                found, frag = self._get_backref_fragment(memory, av)
                if not found:
                    return None
                expected = memory.get(NEXT_WORD_EXPECTATION_KEY)
                if expected is not None and frag and self._is_word_char(frag[:1]) != expected:
                    return None
                memory.pop(NEXT_WORD_EXPECTATION_KEY, None)
                parts.append(frag)

            elif op == sre_constants.AT:
                payload = self._anchor_payload_from_sre(av)
                current = "".join(parts)
                remaining_can_consume = self._tree_sequence_can_consume(sequence[index + 1 :])
                anchor = Node("anchor", kind="anchor", payload=payload)
                if not self._heuristic_anchor_allows(anchor, current, remaining_can_consume):
                    return None
                expected_wordness = self._boundary_expected_wordness(anchor, current)
                if expected_wordness is not None:
                    memory[NEXT_WORD_EXPECTATION_KEY] = expected_wordness

            elif hasattr(sre_constants, "ATOMIC_GROUP") and op == sre_constants.ATOMIC_GROUP:
                frag = self._emit_sequence(
                    av,
                    path=node_path + ("atomic",),
                    constraints=constraints,
                    cursors=cursors,
                    rng=rng,
                    memory=memory,
                    offset=offset,
                )
                if frag is None:
                    return None
                parts.append(frag)

            elif str(op) in {"ASSERT", "ASSERT_NOT"}:
                payload = self._lookaround_payload_from_sre(op, av)
                current = "".join(parts)
                fragments = self._apply_lookaround_fragments(current, payload, rng, len(path), memory)
                if not fragments:
                    return None
                frag, next_memory = fragments[0]
                parts.append(frag)
                memory.update(next_memory)

        return "".join(parts)

    def _choose_choice(self, node_path, constraints, cursors, offset):
        spec = self._heuristic_specs.get(node_path)
        if not spec:
            raise KeyError(f"No heuristic spec registered for {node_path}")

        choices = spec["choices"]
        if node_path in constraints:
            choice = constraints[node_path]
            if choice in choices:
                cursors[node_path] = choices.index(choice) + 1
                return choice
            return choices[0]

        cursor = cursors.get(node_path, 0)
        index = (cursor + offset) % len(choices)
        cursors[node_path] = cursor + 1
        return choices[index]

    def _generate_rank_bucket_samples(self, target_count, seed=None, existing=None):
        existing = set() if existing is None else set(existing)
        rng = random.Random(seed)
        path_counts = self._compute_path_counts_from_nodes()
        total = path_counts.get(self.start_node, 0)
        if total <= 0 or target_count <= 0:
            return []

        results = []
        seen = set(existing)
        total_int = int(total)

        for attempt in range(3):
            bucket_count = max(target_count, 1)
            for bucket_index in range(bucket_count):
                start = (bucket_index * total_int) // bucket_count
                end = (((bucket_index + 1) * total_int) // bucket_count) - 1
                if end < start:
                    continue

                if attempt == 0:
                    idx = start if start == end else rng.randint(start, end)
                elif attempt == 1:
                    idx = (start + end) // 2
                else:
                    idx = end

                sample = self._decode_path_index(idx, path_counts)
                if sample is None or sample in seen:
                    continue
                seen.add(sample)
                results.append(sample)
                if len(results) >= target_count:
                    return results

        return results[:target_count]



def generate_positive_samples(pattern, n=1000, seed=0, mode="diverse", timeout_seconds=DEFAULT_REGEX_TIMEOUT_SECONDS, validate=True):
    builder = RegexGraphBuilder(pattern, validate=validate)
    if mode == "sample":
        return builder.generate_sample(max_samples=n, seed=seed, timeout_seconds=timeout_seconds)
    if mode == "heuristic":
        return builder.generate_heuristic_samples(seed=seed, max_samples=n, timeout_seconds=timeout_seconds)
    if mode == "diverse":
        return builder.generate_diverse_samples(n=n, seed=seed, timeout_seconds=timeout_seconds)
    if mode == "high_coverage":
        return builder.generate_high_coverage_samples(n=n, seed=seed, timeout_seconds=timeout_seconds)
    raise ValueError(f"Unknown positive generation mode: {mode}")


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Generate positive regex samples.")
    parser.add_argument("pattern")
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--mode", choices=["sample", "heuristic", "diverse", "high_coverage"], default="diverse")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_REGEX_TIMEOUT_SECONDS)
    parser.add_argument("--no-validate", action="store_true")
    args = parser.parse_args()

    samples = generate_positive_samples(
        args.pattern,
        n=args.n,
        seed=args.seed,
        mode=args.mode,
        timeout_seconds=args.timeout_seconds,
        validate=not args.no_validate,
    )
    print(json.dumps({"pattern": args.pattern, "mode": args.mode, "count": len(samples), "samples": samples}, indent=2))
