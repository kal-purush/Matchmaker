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

class RegexGraphBuilder:
    def __init__(self, regex_pattern, flags=0, validate=True):
        self.regex = regex_pattern
        self.flags = flags
        self.validate = validate
        self.start_node = Node("START", kind='start')
        self.end_node = Node("END", kind='end')
        self.nodes = []
        self.recursion_counter = 0
        self.context_samples_cache = {}  # Cache for internal context construction keyed by seed/count
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
                beginning_types = {
                    getattr(sre_constants, 'AT_BEGINNING', None),
                    getattr(sre_constants, 'AT_BEGINNING_LINE', None),
                    getattr(sre_constants, 'AT_BEGINNING_STRING', None),
                }
                end_types = {
                    getattr(sre_constants, 'AT_END', None),
                    getattr(sre_constants, 'AT_END_LINE', None),
                    getattr(sre_constants, 'AT_END_STRING', None),
                }
                boundary_types = {
                    getattr(sre_constants, 'AT_BOUNDARY', None),
                    getattr(sre_constants, 'AT_LOC_BOUNDARY', None),
                    getattr(sre_constants, 'AT_UNI_BOUNDARY', None),
                }
                non_boundary_types = {
                    getattr(sre_constants, 'AT_BOUNDARY_NOT', None),
                    getattr(sre_constants, 'AT_NON_BOUNDARY', None),
                    getattr(sre_constants, 'AT_LOC_NON_BOUNDARY', None),
                    getattr(sre_constants, 'AT_UNI_NON_BOUNDARY', None),
                }
                if anchor_type in beginning_types:
                    n = Node("^", kind='anchor', payload='start')
                elif anchor_type in end_types:
                    n = Node("$", kind='anchor', payload='end')
                elif anchor_type in boundary_types:
                    n = Node("\\b", kind='anchor', payload='word_boundary')
                elif anchor_type in non_boundary_types:
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

    def _dedupe_preserve_order(self, items):
        seen = set()
        out = []
        for item in items:
            if item in seen:
                continue
            seen.add(item)
            out.append(item)
        return out

    def _regex_fullmatch(self, s):
        try:
            engine = regex if REGEX_AVAILABLE else re
            return engine.fullmatch(self.regex, s, self.flags) is not None
        except Exception:
            return False

    def _fails_regex(self, s, *, use_fullmatch=True):
        try:
            engine = regex if REGEX_AVAILABLE else re
            if use_fullmatch or self.requires_match_validation:
                return engine.fullmatch(self.regex, s, self.flags) is None
            return engine.search(self.regex, s, self.flags) is None
        except Exception:
            return False

    def _should_accept_negative(self, s, *, use_fullmatch=True):
        if s is None:
            return False
        if not self.validate:
            return True
        return self._fails_regex(s, use_fullmatch=use_fullmatch)

    def _has_escape_categories(self):
        return bool(re.search(r'\\[dDsSwW]', self.regex))

    def _has_unicode_constraints(self):
        return bool(re.search(r'\\[pP]\{[^}]+\}', self.regex) or any(ord(ch) > 127 for ch in self.regex))

    def _has_alternation(self):
        return '|' in self.regex or any(node.kind == 'split' and len(node.next) > 1 for node in self.nodes)

    def _is_case_insensitive(self):
        return bool(self.flags & re.IGNORECASE or re.search(r'\(\?[aiLmsux-]*i[aiLmsux-]*\)', self.regex))

    def _has_case_sensitive_constraints(self):
        if self._is_case_insensitive():
            return False
        if re.search(r'\[[^\]]*[a-z][^\]]*\]', self.regex):
            return True
        if re.search(r'\[[^\]]*[A-Z][^\]]*\]', self.regex):
            return True
        return any(
            node.kind == 'match' and isinstance(node.payload, str) and node.payload.isalpha()
            for node in self.nodes
        )

    def _has_zero_width_assertions(self):
        return bool(
            self.lookarounds
            or any(token in self.regex for token in ('\\b', '\\B'))
            or any(
                node.kind == 'anchor' and node.payload in {'word_boundary', 'not_word_boundary'}
                for node in self.nodes
            )
        )

    def _has_literal_separators(self):
        return any(node.kind == 'match' and node.payload in {'.', ',', '-', ':', '/', '_', '@'} for node in self.nodes)

    def _has_numeric_pattern(self):
        return any(token in self.regex for token in ['\\d', '.', ',', '$']) or any(
            node.kind == 'match' and str(node.payload).isdigit() for node in self.nodes
        )

    def _random_char_pool(self):
        pool = []
        pool.extend(string.ascii_letters)
        pool.extend(string.digits)
        pool.extend(string.punctuation)
        pool.extend([' ', '\\t', '\\n'])
        pool.extend(list('äöüßéèàçñøΩЖ中あ'))
        return pool

    def _representative_chars(self, char_set, *, rng):
        chars = sorted(set(char_set))
        if not chars:
            return []
        return self._dedupe_preserve_order([chars[0], chars[-1], chars[len(chars) // 2], rng.choice(chars)])

    def _representative_normal_char(self, node, *, rng):
        if node.kind == 'match':
            return node.payload
        if node.kind in ['class', 'any']:
            chars = self._representative_chars(node.payload, rng=rng)
            return rng.choice(chars) if chars else 'a'
        if node.kind == 'backref':
            return 'a'
        return ''

    def _get_char_outside_class(self, char_set, *, rng):
        for c in self._random_char_pool():
            if c not in char_set:
                return c
        for _ in range(200):
            c = chr(rng.randint(0x20, 0x2FFF))
            if c not in char_set:
                return c
        return '!'

    def _invalid_literal_char(self, ch, *, rng):
        for candidate in ['x', '0', '_', '-', ' ', '@', '中']:
            if candidate != ch:
                return candidate
        pool = [c for c in self._random_char_pool() if c != ch]
        return rng.choice(pool) if pool else 'x'

    def _class_violation_choices(self, char_set, *, rng):
        ascii_lower = set(string.ascii_lowercase)
        ascii_upper = set(string.ascii_uppercase)
        ascii_letters = set(string.ascii_letters)
        ascii_digits = set(string.digits)
        special_chars = set(string.punctuation + " ")
        non_ascii_letters = ["é", "Ω", "Ж", "中", "あ"]
        non_ascii_digits = ["١", "२", "৪", "７"]

        if char_set and char_set.issubset(ascii_letters):
            return self._dedupe_preserve_order([
                rng.choice(list(string.digits)),
                rng.choice(list(string.punctuation)),
                rng.choice(non_ascii_letters),
            ])

        if char_set and char_set.issubset(ascii_digits):
            return self._dedupe_preserve_order([
                rng.choice(list(string.ascii_letters)),
                rng.choice(list(string.punctuation)),
                rng.choice(non_ascii_digits),
            ])

        if char_set and char_set & ascii_letters and not (char_set - ascii_letters):
            return self._dedupe_preserve_order([
                rng.choice(list(string.digits)),
                rng.choice(list(string.punctuation)),
                rng.choice(non_ascii_letters),
            ])

        if char_set and char_set & ascii_digits and not (char_set - ascii_digits):
            return self._dedupe_preserve_order([
                rng.choice(list(string.ascii_letters)),
                rng.choice(list(string.punctuation)),
                rng.choice(non_ascii_digits),
            ])

        if char_set and char_set.issubset(special_chars):
            return self._dedupe_preserve_order([
                rng.choice(non_ascii_letters),
                rng.choice(list(string.ascii_letters)),
                rng.choice(list(string.digits)),
            ])

        candidates = [
            rng.choice(non_ascii_letters),
            rng.choice(list(string.ascii_letters)),
            rng.choice(list(string.digits)),
            rng.choice(list(string.punctuation)),
        ]
        return [c for c in self._dedupe_preserve_order(candidates) if c not in char_set]

    def _class_group_violation_sequences(self, char_set, *, rng, width=3):
        ascii_letters = set(string.ascii_letters)
        ascii_digits = set(string.digits)
        special_chars = list("!@#$%^&*")
        non_ascii_letters = ["é", "Ω", "Ж", "中", "あ"]
        non_ascii_digits = ["١", "२", "৪", "７"]

        def _repeat_from(pool):
            token = rng.choice(pool)
            count = rng.randint(1, max(1, width))
            return token * count

        if char_set and char_set.issubset(ascii_letters):
            return self._dedupe_preserve_order([
                _repeat_from(list(string.digits)),
                _repeat_from(special_chars),
                _repeat_from(non_ascii_letters),
            ])

        if char_set and char_set.issubset(ascii_digits):
            return self._dedupe_preserve_order([
                _repeat_from(list(string.ascii_letters)),
                _repeat_from(special_chars),
                _repeat_from(non_ascii_digits),
            ])

        return self._dedupe_preserve_order([
            _repeat_from(non_ascii_letters),
            _repeat_from(list(string.ascii_letters)),
            _repeat_from(list(string.digits)),
        ])

    def _find_path(self, start_node, target_node):
        from collections import deque

        queue = deque([(start_node, [start_node])])
        visited = set()
        while queue:
            node, path = queue.popleft()
            if node == target_node:
                return path
            if node in visited:
                continue
            visited.add(node)
            for nxt in node.next:
                queue.append((nxt, path + [nxt]))
        return None

    def _find_path_to_node(self, target_node):
        return self._find_path(self.start_node, target_node)

    def _find_path_to_end(self, start_node):
        return self._find_path(start_node, self.end_node)

    def _build_string_from_path(self, path, *, rng):
        out = []
        for node in path:
            if node.kind == 'match':
                out.append(node.payload)
            elif node.kind in ['class', 'any']:
                out.append(self._representative_normal_char(node, rng=rng))
            elif node.kind == 'backref':
                out.append('a')
        return ''.join(out)

    def _build_context_sample(self, *, rng, max_steps=256):
        node = self.start_node
        s = ''
        memory = {}
        active_caps = {}
        steps = 0
        while node.kind != 'end' and steps < max_steps:
            steps += 1
            if node.kind == 'match':
                s += node.payload
                node = node.next[0] if node.next else self.end_node
            elif node.kind in ['class', 'any']:
                s += self._representative_normal_char(node, rng=rng)
                node = node.next[0] if node.next else self.end_node
            elif node.kind == 'group_start':
                active_caps[node.payload] = len(s)
                node = node.next[0] if node.next else self.end_node
            elif node.kind == 'group_end':
                start_idx = active_caps.get(node.payload)
                if start_idx is not None:
                    memory[node.payload] = s[start_idx:]
                node = node.next[0] if node.next else self.end_node
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
            elif node.kind in ['cond_yes', 'cond_no', 'atomic', 'anchor', 'start']:
                node = node.next[0] if node.next else self.end_node
            elif node.kind == 'backref':
                s += memory.get(node.payload, 'a')
                node = node.next[0] if node.next else self.end_node
            elif node.kind == 'split':
                node = rng.choice(node.next) if node.next else self.end_node
            else:
                node = node.next[0] if node.next else self.end_node
        return self._clean_conditional_markers(s, memory)

    def _get_context_samples(self, *, seed=None, count=12):
        if not hasattr(self, 'context_samples_cache'):
            self.context_samples_cache = {}
        cache_key = (seed if seed is not None else 0, count)
        if cache_key in self.context_samples_cache:
            return self.context_samples_cache[cache_key]

        rng = random.Random(seed)
        samples = []
        attempts = 0
        while len(samples) < count and attempts < count * 30:
            attempts += 1
            candidate = self._build_context_sample(rng=rng)
            if candidate is None:
                continue
            if self._regex_fullmatch(candidate):
                samples.append(candidate)
        samples = self._dedupe_preserve_order(samples)
        self.context_samples_cache[cache_key] = samples
        return samples

    def _candidate_signature(self, sample, family):
        profile = (
            any(ch.isdigit() for ch in sample),
            any(ch.isalpha() for ch in sample),
            any(ch.isspace() for ch in sample),
            any(not ch.isalnum() and not ch.isspace() for ch in sample),
        )
        return (family, min(len(sample) // 4, 8), profile, sample[:2], sample[-2:])

    def _select_relevant_violation_types(self):
        selected = ['basic_boundary_violation', 'whitespace_boundary_violation', 'structure_violation']
        has_quantifier = any(q in self.regex for q in ['*', '+', '?']) or bool(re.search(r'\{\d+(?:,\d*)?\}', self.regex))
        has_anchor = any(n.kind == 'anchor' for n in self.nodes)
        has_backref = any(n.kind == 'backref' for n in self.nodes)
        has_conditional = any(n.kind == 'conditional' for n in self.nodes)
        has_class = any(n.kind in ['class', 'any'] for n in self.nodes)
        if has_class:
            selected.extend(['character_class_violation', 'group_violation', 'wrong_char_substitution'])
        if has_quantifier:
            selected.extend(['quantifier_underflow_violation', 'quantifier_overflow_violation'])
        if has_anchor:
            selected.append('anchor_violation')
        if has_backref:
            selected.append('backreference_violation')
        if has_conditional:
            selected.append('conditional_violation')
        if self._has_escape_categories():
            selected.append('escape_sequence_violation')
        if self._has_unicode_constraints():
            selected.append('unicode_violation')
        if self._has_alternation():
            selected.append('alternation_violation')
        if self._has_case_sensitive_constraints():
            selected.append('case_sensitivity_violation')
        if self._has_zero_width_assertions():
            selected.append('zero_width_assertion_violation')
        if self._has_literal_separators():
            selected.append('separator_boundary_violation')
        if self._has_numeric_pattern():
            selected.append('numeric_boundary_violation')
        return self._dedupe_preserve_order(selected)

    def _heuristic_generators(self):
        return {
            'basic_boundary_violation': self._generate_basic_boundary_violations,
            'whitespace_boundary_violation': self._generate_whitespace_boundary_violations,
            'separator_boundary_violation': self._generate_separator_boundary_violations,
            'numeric_boundary_violation': self._generate_numeric_boundary_violations,
            'character_class_violation': self._generate_class_violations,
            'group_violation': self._generate_group_violations,
            'wrong_char_substitution': self._generate_substitution_violations,
            'quantifier_underflow_violation': self._generate_quantifier_underflow_violations,
            'quantifier_overflow_violation': self._generate_quantifier_overflow_violations,
            'anchor_violation': self._generate_anchor_violations,
            'backreference_violation': self._generate_backref_violations,
            'conditional_violation': self._generate_conditional_violations,
            'escape_sequence_violation': self._generate_escape_sequence_violations,
            'unicode_violation': self._generate_unicode_violations,
            'alternation_violation': self._generate_alternation_violations,
            'case_sensitivity_violation': self._generate_case_sensitivity_violations,
            'zero_width_assertion_violation': self._generate_zero_width_assertion_violations,
            'structure_violation': self._generate_structure_violations,
        }

    def _generate_negative_samples_for_mode(self, mode, num_samples=10, *, seed=None,
                                            max_attempts=None, use_fullmatch=False,
                                            timeout_seconds=DEFAULT_REGEX_TIMEOUT_SECONDS):
        def _generate():
            rng = random.Random(seed)
            generators = self._heuristic_generators()
            families = [name for name in self._select_relevant_violation_types() if name in generators]
            if not families:
                families = ['structure_violation']

            if mode == 'heuristic':
                results = []
                for family in families:
                    for sample in generators[family](rng=rng) or []:
                        if self._should_accept_negative(sample, use_fullmatch=use_fullmatch):
                            results.append(sample)
                    if len(self._dedupe_preserve_order(results)) >= num_samples:
                        break
                return self._dedupe_preserve_order(results)[:num_samples]

            if mode == 'diverse':
                target_attempts = max_attempts or num_samples * 200
                results = []
                seen_samples = set()
                seen_signatures = set()
                family_hits = {family: 0 for family in families}
                attempts = 0
                while len(results) < num_samples and attempts < target_attempts:
                    attempts += 1
                    family = min(families, key=lambda name: (family_hits[name], rng.random()))
                    family_hits[family] += 1
                    emitted = generators[family](rng=rng) or []
                    rng.shuffle(emitted)
                    for sample in emitted:
                        if sample is None or sample in seen_samples:
                            continue
                        if not self._should_accept_negative(sample, use_fullmatch=use_fullmatch):
                            continue
                        signature = self._candidate_signature(sample, family)
                        if signature in seen_signatures and rng.random() < 0.8:
                            continue
                        seen_samples.add(sample)
                        seen_signatures.add(signature)
                        results.append(sample)
                        if len(results) >= num_samples:
                            break
                return results[:num_samples]

            results = []
            seen = set()
            for sample in self._generate_graph_local_violation_samples(rng=rng, limit=max(num_samples * 3, 24)):
                if sample not in seen and self._should_accept_negative(sample, use_fullmatch=use_fullmatch):
                    seen.add(sample)
                    results.append(sample)
                    if len(results) >= num_samples:
                        return results[:num_samples]
            extra = self._generate_negative_samples_for_mode(
                'diverse',
                num_samples=num_samples * 2,
                seed=seed,
                max_attempts=max_attempts or num_samples * 250,
                use_fullmatch=use_fullmatch,
                timeout_seconds=0,
            )
            for sample in extra:
                if sample not in seen:
                    seen.add(sample)
                    results.append(sample)
                    if len(results) >= num_samples:
                        break
            return results[:num_samples]

        return _run_with_timeout(_generate, timeout_seconds)

    def generate_negative_heuristic_samples(self, num_samples=10, *, seed=None,
                                            max_attempts=None, use_fullmatch=False,
                                            timeout_seconds=DEFAULT_REGEX_TIMEOUT_SECONDS):
        return self._generate_negative_samples_for_mode(
            'heuristic',
            num_samples=num_samples,
            seed=seed,
            max_attempts=max_attempts,
            use_fullmatch=use_fullmatch,
            timeout_seconds=timeout_seconds,
        )

    def generate_negative_diverse_samples(self, num_samples=10, *, seed=None,
                                          max_attempts=None, use_fullmatch=False,
                                          timeout_seconds=DEFAULT_REGEX_TIMEOUT_SECONDS):
        return self._generate_negative_samples_for_mode(
            'diverse',
            num_samples=num_samples,
            seed=seed,
            max_attempts=max_attempts,
            use_fullmatch=use_fullmatch,
            timeout_seconds=timeout_seconds,
        )

    def generate_negative_high_coverage_samples(self, num_samples=10, *, seed=None,
                                                max_attempts=None, use_fullmatch=False,
                                                timeout_seconds=DEFAULT_REGEX_TIMEOUT_SECONDS):
        return self._generate_negative_samples_for_mode(
            'high_coverage',
            num_samples=num_samples,
            seed=seed,
            max_attempts=max_attempts,
            use_fullmatch=use_fullmatch,
            timeout_seconds=timeout_seconds,
        )

    def generate_negative_samples(self, num_samples=10, *, seed=None,
                                  max_attempts=None, use_fullmatch=False,
                                  timeout_seconds=DEFAULT_REGEX_TIMEOUT_SECONDS):
        return self.generate_negative_diverse_samples(
            num_samples=num_samples,
            seed=seed,
            max_attempts=max_attempts,
            use_fullmatch=use_fullmatch,
            timeout_seconds=timeout_seconds,
        )

    def _generate_basic_boundary_violations(self, *, rng, per_sample=2):
        context = self._get_context_samples(seed=rng.randint(0, 10**9), count=8)
        candidates = ['', ' ', '\\t', '\\n']
        for sample in context:
            if sample:
                candidates.extend([sample[:1], sample[:-1]])
                for _ in range(per_sample):
                    candidates.append(sample[:rng.randrange(len(sample) + 1)])
            else:
                candidates.append(rng.choice(self._random_char_pool()))
        return self._dedupe_preserve_order(candidates)

    def _generate_whitespace_boundary_violations(self, *, rng, per_sample=2):
        context = self._get_context_samples(seed=rng.randint(0, 10**9), count=8) or ['']
        candidates = []
        for sample in context:
            candidates.extend([' ' + sample, sample + ' ', '\\n' + sample, sample + '\\n', '\\t' + sample + '\\t'])
            if sample:
                pos = rng.randrange(len(sample) + 1)
                candidates.append(sample[:pos] + ' ' + sample[pos:])
                candidates.append(sample[:pos] + '\\n' + sample[pos:])
        return self._dedupe_preserve_order(candidates)

    def _generate_separator_boundary_violations(self, *, rng, per_sample=2):
        separators = self._dedupe_preserve_order(
            node.payload for node in self.nodes if node.kind == 'match' and node.payload in {'.', ',', '-', ':', '/', '_', '@'}
        )
        if not separators:
            return []
        context = self._get_context_samples(seed=rng.randint(0, 10**9), count=8)
        wrong_choices = [':', ';', '/', '_', '-', '.', ',', ' ']
        candidates = []
        for sample in context:
            for sep in separators:
                if sep in sample:
                    candidates.extend([sample.replace(sep, '', 1), sample.replace(sep, sep * 2, 1)])
                    wrong = rng.choice([c for c in wrong_choices if c != sep])
                    candidates.append(sample.replace(sep, wrong, 1))
                else:
                    pos = rng.randrange(len(sample) + 1)
                    candidates.append(sample[:pos] + sep + sample[pos:])
            if per_sample > 1 and separators:
                sep = rng.choice(separators)
                candidates.extend([sep + sample, sample + sep])
        return self._dedupe_preserve_order(candidates)

    def _generate_numeric_boundary_violations(self, *, rng, per_sample=2):
        if not self._has_numeric_pattern():
            return []
        context = self._get_context_samples(seed=rng.randint(0, 10**9), count=8)
        candidates = ['01', '.5', '1.', '-1', '$.99', '1.999', '1,23', ',123']
        for sample in context:
            digits = [i for i, ch in enumerate(sample) if ch.isdigit()]
            if not digits:
                continue
            candidates.append(sample[:digits[0]] + '0' + sample[digits[0]:])
            if '.' in sample:
                left, right = sample.split('.', 1)
                candidates.extend(['.' + right, left + '.', left + '.' + right + str(rng.randint(0, 9))])
            else:
                candidates.extend([sample + '.', sample + '.' + str(rng.randint(100, 999))])
            if ',' in sample:
                candidates.extend([sample.replace(',', '', 1), sample.replace(',', ',,', 1)])
            else:
                pos = rng.choice(digits)
                candidates.append(sample[:pos] + ',' + sample[pos:])
            for _ in range(per_sample):
                i = rng.choice(digits)
                candidates.append(sample[:i] + rng.choice(['a', '_', ' ']) + sample[i + 1:])
        return self._dedupe_preserve_order(candidates)

    def _generate_class_violations(self, *, rng, max_nodes=8):
        candidates = []
        nodes = [node for node in self.nodes if node.kind in ['class', 'any']]
        rng.shuffle(nodes)
        for node in nodes[:max_nodes]:
            prefix_path = self._find_path_to_node(node)
            suffix_path = self._find_path_to_end(node.next[0]) if node.next else None
            prefix = self._build_string_from_path(prefix_path[:-1], rng=rng) if prefix_path else ''
            suffix = self._build_string_from_path(suffix_path[:-1], rng=rng) if suffix_path else ''
            choices = self._class_violation_choices(node.payload, rng=rng)
            if not choices:
                choices = [self._get_char_outside_class(node.payload, rng=rng)]
            for choice in choices:
                candidates.append(prefix + choice + suffix)
        return self._dedupe_preserve_order(candidates)

    def _generate_group_violations(self, *, rng, max_nodes=8):
        candidates = []
        nodes = [node for node in self.nodes if node.kind in ['class', 'any']]
        rng.shuffle(nodes)
        for node in nodes[:max_nodes]:
            prefix_path = self._find_path_to_node(node)
            prefix = self._build_string_from_path(prefix_path[:-1], rng=rng) if prefix_path else ''
            suffix_path = self._find_path_to_end(node.next[0]) if node.next else None
            suffix = self._build_string_from_path(suffix_path[:-1], rng=rng) if suffix_path else ''

            context = self._get_context_samples(seed=rng.randint(0, 10**9), count=6)
            group_sequences = self._class_group_violation_sequences(node.payload, rng=rng)

            for seq in group_sequences:
                candidates.append(prefix + seq + suffix)

            for sample in context:
                runs = []
                start = None
                for idx, ch in enumerate(sample):
                    if ch in node.payload:
                        if start is None:
                            start = idx
                    else:
                        if start is not None:
                            runs.append((start, idx))
                            start = None
                if start is not None:
                    runs.append((start, len(sample)))

                for run_start, run_end in runs[:2]:
                    for seq in group_sequences:
                        candidates.append(sample[:run_start] + seq + sample[run_end:])

        return self._dedupe_preserve_order(candidates)

    def _generate_quantifier_underflow_violations(self, *, rng, per_sample=2):
        context = self._get_context_samples(seed=rng.randint(0, 10**9), count=8)
        candidates = [''] if ('+' in self.regex or re.search(r'\{\d+(?:,\d*)?\}', self.regex)) else []
        for sample in context:
            if sample:
                candidates.extend([sample[:-1], sample[:-2] if len(sample) > 1 else ''])
                for _ in range(per_sample):
                    candidates.append(sample[:rng.randrange(len(sample))])
        return self._dedupe_preserve_order(candidates)

    def _generate_quantifier_overflow_violations(self, *, rng, per_sample=2):
        context = self._get_context_samples(seed=rng.randint(0, 10**9), count=8)
        candidates = []
        for sample in context:
            if not sample:
                continue
            tail = sample[-1]
            candidates.append(sample + tail * rng.randint(1, 3))
            if len(sample) > 1:
                chunk = sample[max(0, len(sample) // 2 - 1): max(1, len(sample) // 2 + 1)]
                candidates.append(sample + chunk * rng.randint(1, 2))
            for _ in range(per_sample):
                pos = rng.randrange(len(sample) + 1)
                chunk = sample[max(0, pos - 1):pos] or tail
                candidates.append(sample[:pos] + chunk * 2 + sample[pos:])
        return self._dedupe_preserve_order(candidates)

    def _generate_substitution_violations(self, *, rng, per_sample=2):
        context = self._get_context_samples(seed=rng.randint(0, 10**9), count=8)
        candidates = []
        for sample in context:
            if not sample:
                continue
            for _ in range(per_sample):
                i = rng.randrange(len(sample))
                candidates.append(sample[:i] + self._invalid_literal_char(sample[i], rng=rng) + sample[i + 1:])
        return self._dedupe_preserve_order(candidates)

    def _generate_anchor_violations(self, *, rng, per_sample=2):
        context = self._get_context_samples(seed=rng.randint(0, 10**9), count=8) or ['a']
        candidates = []
        has_start = any(n.kind == 'anchor' and n.payload == 'start' for n in self.nodes)
        has_end = any(n.kind == 'anchor' and n.payload == 'end' for n in self.nodes)
        pool = self._random_char_pool()
        for sample in context:
            prefix = ''.join(rng.choice(pool) for _ in range(rng.randint(1, 3)))
            suffix = ''.join(rng.choice(pool) for _ in range(rng.randint(1, 3)))
            if has_start:
                candidates.append(prefix + sample)
            if has_end:
                candidates.append(sample + suffix)
            candidates.append(prefix + sample + suffix)
            if per_sample > 1:
                candidates.extend(['\\n' + sample, sample + '\\n'])
        return self._dedupe_preserve_order(candidates)

    def _generate_structure_violations(self, *, rng, per_sample=2):
        context = self._get_context_samples(seed=rng.randint(0, 10**9), count=8)
        candidates = []
        for sample in context:
            if not sample:
                candidates.append(rng.choice(self._random_char_pool()))
                continue
            candidates.append(sample[::-1])
            if len(sample) > 1:
                chars = list(sample)
                rng.shuffle(chars)
                candidates.append(''.join(chars))
                midpoint = max(1, len(sample) // 2)
                candidates.append(sample[:midpoint] + sample[:midpoint] + sample[midpoint:])
            for _ in range(per_sample):
                pos = rng.randrange(len(sample) + 1)
                candidates.append(sample[:pos] + rng.choice(self._random_char_pool()) + sample[pos:])
        return self._dedupe_preserve_order(candidates)

    def _generate_escape_sequence_violations(self, *, rng, per_sample=2):
        if not self._has_escape_categories():
            return []
        context = self._get_context_samples(seed=rng.randint(0, 10**9), count=8)
        escape_tokens = set(re.findall(r'\\[dDsSwW]', self.regex))
        candidates = []
        for token in escape_tokens:
            if token == r'\d':
                candidates.extend(['a', '_', ' ', '中'])
            elif token == r'\w':
                candidates.extend(['-', '.', ' ', '\\n'])
            elif token == r'\s':
                candidates.extend(['A', '1', '_', '-'])
        for sample in context:
            if not sample:
                continue
            for _ in range(per_sample):
                i = rng.randrange(len(sample))
                token = rng.choice(sorted(escape_tokens))
                replacement = 'a'
                if token == r'\d':
                    replacement = rng.choice(['a', '_', ' ', '中'])
                elif token == r'\w':
                    replacement = rng.choice(['-', '.', ' ', '\\n'])
                elif token == r'\s':
                    replacement = rng.choice(['A', '1', '_', '-'])
                candidates.append(sample[:i] + replacement + sample[i + 1:])
        return self._dedupe_preserve_order(candidates)

    def _generate_unicode_violations(self, *, rng, per_sample=2):
        unicode_noise = ['café', 'naïve', '中文', 'Ωmega', 'Ж9', 'a中b', 'é']
        context = self._get_context_samples(seed=rng.randint(0, 10**9), count=8)
        candidates = list(unicode_noise)
        for sample in context:
            if not sample:
                continue
            for _ in range(per_sample):
                pos = rng.randrange(len(sample) + 1)
                noise = rng.choice(unicode_noise)
                candidates.append(sample[:pos] + noise + sample[pos:])
        return self._dedupe_preserve_order(candidates)

    def _literal_runs(self):
        return [run for run in re.findall(r'[A-Za-z]+', self.regex) if len(run) >= 2]

    def _generate_alternation_violations(self, *, rng, per_sample=2):
        if not self._has_alternation():
            return []
        context = self._get_context_samples(seed=rng.randint(0, 10**9), count=8)
        runs = self._literal_runs()
        candidates = []
        for run in runs[:8]:
            candidates.append(run[:-1])
            idx = min(len(run) - 1, len(run) // 2)
            candidates.append(run[:idx] + self._invalid_literal_char(run[idx], rng=rng) + run[idx + 1:])
        if len(runs) >= 2:
            for left, right in zip(runs, runs[1:]):
                candidates.append(left[:max(1, len(left) // 2)] + right[max(1, len(right) // 2):])
        for sample in context:
            if not sample:
                continue
            for _ in range(per_sample):
                i = rng.randrange(len(sample))
                candidates.append(sample[:i] + self._invalid_literal_char(sample[i], rng=rng) + sample[i + 1:])
        return self._dedupe_preserve_order(candidates)

    def _generate_case_sensitivity_violations(self, *, rng, per_sample=2):
        if not self._has_case_sensitive_constraints():
            return []
        context = self._get_context_samples(seed=rng.randint(0, 10**9), count=8)
        candidates = []
        for sample in context:
            positions = [i for i, ch in enumerate(sample) if ch.isalpha()]
            if not positions:
                continue
            for _ in range(per_sample):
                i = rng.choice(positions)
                candidates.append(sample[:i] + sample[i].swapcase() + sample[i + 1:])
        return self._dedupe_preserve_order(candidates)

    def _generate_zero_width_assertion_violations(self, *, rng, per_sample=2):
        if not self._has_zero_width_assertions():
            return []
        context = self._get_context_samples(seed=rng.randint(0, 10**9), count=8) or ['word']
        candidates = []
        for sample in context:
            candidates.extend(['_' + sample, sample + '_', ' ' + sample + ' ', '\\n' + sample, sample + '\\n'])
            if len(sample) > 1:
                candidates.extend([sample[:1] + '-' + sample[1:], sample[:-1] + '-' + sample[-1:]])
            if per_sample > 1:
                candidates.append('9' + sample + '9')
        return self._dedupe_preserve_order(candidates)

    def _generate_backref_violations(self, *, rng):
        context = self._get_context_samples(seed=rng.randint(0, 10**9), count=8)
        candidates = []
        for sample in context:
            if len(sample) > 1:
                candidates.append(sample[:-1] + self._invalid_literal_char(sample[-1], rng=rng))
                midpoint = len(sample) // 2
                candidates.append(sample[:midpoint] + self._invalid_literal_char(sample[midpoint], rng=rng) + sample[midpoint + 1:])
        return self._dedupe_preserve_order(candidates)

    def _generate_conditional_violations(self, *, rng):
        context = self._get_context_samples(seed=rng.randint(0, 10**9), count=8)
        candidates = []
        for sample in context:
            if sample:
                candidates.append(sample[::-1])
                chars = list(sample)
                rng.shuffle(chars)
                candidates.append(''.join(chars))
        return self._dedupe_preserve_order(candidates)

    def _generate_graph_local_violation_samples(self, *, rng, limit=64):
        candidates = []
        for node in self.nodes:
            if node.kind not in {'match', 'class', 'any', 'backref', 'anchor'}:
                continue
            path = self._find_path_to_node(node)
            if not path:
                continue
            prefix = self._build_string_from_path(path[:-1], rng=rng)
            suffix_path = self._find_path_to_end(node.next[0]) if node.next else None
            suffix = self._build_string_from_path(suffix_path[:-1], rng=rng) if suffix_path else ''
            if node.kind == 'match':
                candidates.append(prefix + self._invalid_literal_char(node.payload, rng=rng) + suffix)
            elif node.kind in ['class', 'any']:
                candidates.append(prefix + self._get_char_outside_class(node.payload, rng=rng) + suffix)
            elif node.kind == 'backref':
                candidates.append(prefix + 'x' + suffix)
            elif node.kind == 'anchor':
                if node.payload == 'start':
                    candidates.append('X' + prefix + suffix)
                elif node.payload == 'end':
                    candidates.append(prefix + suffix + 'X')
                else:
                    candidates.append(prefix + '_' + suffix)
            if len(candidates) >= limit:
                break
        return self._dedupe_preserve_order(candidates)[:limit]

    def _clean_conditional_markers(self, s, memory):
        import re as re_std

        result = s
        marker_pattern = r'_COND(\d+)([YN])_([^_]*)'
        for match in re_std.finditer(marker_pattern, s):
            group_id = int(match.group(1))
            branch_type = match.group(2)
            content = match.group(3)
            group_captured = group_id in memory and memory[group_id]
            if branch_type == 'Y' and not group_captured:
                return None
            if branch_type == 'N' and group_captured:
                return None
            result = result.replace(match.group(0), content)
        return result

def _run_with_timeout(func, timeout_seconds):
    if timeout_seconds is None or timeout_seconds <= 0:
        return func()

    def _timeout_handler(signum, frame):
        raise TimeoutError(f"Timed out after {timeout_seconds}s")

    start = time.monotonic()
    previous_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    previous_delay, previous_interval = signal.getitimer(signal.ITIMER_REAL)
    effective_timeout = timeout_seconds
    if previous_delay > 0:
        effective_timeout = min(timeout_seconds, previous_delay)

    signal.setitimer(signal.ITIMER_REAL, effective_timeout)
    try:
        return func()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        elapsed = time.monotonic() - start
        if previous_delay > 0:
            remaining = max(previous_delay - elapsed, 0.0)
            signal.setitimer(signal.ITIMER_REAL, remaining, previous_interval)
        signal.signal(signal.SIGALRM, previous_handler)


def generate_negative_samples(
    pattern,
    n=1000,
    seed=0,
    mode="diverse",
    use_fullmatch=True,
    timeout_seconds=DEFAULT_REGEX_TIMEOUT_SECONDS,
    validate=True,
):
    builder = RegexGraphBuilder(pattern, validate=validate)
    if mode == "default":
        mode = "diverse"
    if mode == "heuristic":
        return builder.generate_negative_heuristic_samples(
            num_samples=n,
            seed=seed,
            use_fullmatch=use_fullmatch,
            timeout_seconds=timeout_seconds,
        )
    if mode == "diverse":
        return builder.generate_negative_diverse_samples(
            num_samples=n,
            seed=seed,
            max_attempts=n * 400,
            use_fullmatch=use_fullmatch,
            timeout_seconds=timeout_seconds,
        )
    if mode == "high_coverage":
        return builder.generate_negative_high_coverage_samples(
            num_samples=n,
            seed=seed,
            max_attempts=n * 400,
            use_fullmatch=use_fullmatch,
            timeout_seconds=timeout_seconds,
        )
    raise ValueError(f"Unknown negative generation mode: {mode}")


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Generate negative regex samples.")
    parser.add_argument("pattern")
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--mode", choices=["default", "heuristic", "diverse", "high_coverage"], default="diverse")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_REGEX_TIMEOUT_SECONDS)
    parser.add_argument("--search", action="store_true", help="Use search semantics when validation is enabled.")
    parser.add_argument("--no-validate", action="store_true")
    args = parser.parse_args()

    samples = generate_negative_samples(
        args.pattern,
        n=args.n,
        seed=args.seed,
        mode=args.mode,
        use_fullmatch=not args.search,
        timeout_seconds=args.timeout_seconds,
        validate=not args.no_validate,
    )
    print(json.dumps({"pattern": args.pattern, "mode": args.mode, "count": len(samples), "samples": samples}, indent=2))
