"""Naive enumeration baseline for positives: shortest strings first, and within one
length every choice in order (classes in character order, branches left to right).

For ``[A-Z]+\\d+`` this gives A0, A1, ..., A9, B0, ..., Z9, AA0, ... . It uses the full
(non-simplified) compact graph, so unbounded repeats keep Matchmaker's parse-time cap
(min + 3). Strings are checked with ``re.fullmatch`` (lookarounds, backreferences), and
duplicates are dropped.
"""

import re
from matchmaker.compact import CompactRegexGraphBuilder


def enumeration_samples(pattern, n, max_candidates=None, max_steps=None):
    if n <= 0:
        return []
    builder = CompactRegexGraphBuilder(pattern, validate=True)
    compiled = re.compile(builder.regex, builder.flags)
    max_candidates = max_candidates or max(n * 200, 20000)
    max_steps = max_steps or MAX_STEPS

    cache = {}

    def bounds(node):
        """(min, max) length of strings this node can produce; max may be None."""
        if id(node) not in cache:
            cache[id(node)] = _bounds(node)
        return cache[id(node)]

    def _bounds(node):
        kind = node.kind
        if kind == "literal":
            return len(node.payload), len(node.payload)
        if kind == "class":
            return (1, 1) if node.payload else (None, None)
        if kind == "sequence":
            low, high = 0, 0
            for child in node.children:
                child_low, child_high = bounds(child)
                if child_low is None:
                    return None, None
                low += child_low
                high = None if high is None or child_high is None else high + child_high
            return low, high
        if kind == "choice":
            ranges = [bounds(child) for child in node.children]
            ranges = [r for r in ranges if r[0] is not None]
            if not ranges:
                return None, None
            highs = [r[1] for r in ranges]
            return min(r[0] for r in ranges), None if None in highs else max(highs)
        if kind == "repeat":
            child_low, child_high = bounds(node.children[0])
            if child_low is None:
                return (0, 0) if node.min_rep == 0 else (None, None)
            high = None if child_high is None else child_high * node.max_rep
            return child_low * node.min_rep, high
        if kind in {"group", "passthrough"}:
            return bounds(node.children[0]) if node.children else (0, 0)
        if kind in {"anchor", "lookaround"}:
            return 0, 0
        if kind == "impossible":
            return None, None
        return 0, None  # backref, conditional: unknown, filtered by fullmatch

    def fits(node, length):
        low, high = bounds(node)
        return low is not None and low <= length and (high is None or length <= high)

    def of_length(node, length):
        """Strings of exactly ``length`` from ``node``, in enumeration order."""
        if not fits(node, length):
            return
        kind = node.kind
        if kind == "literal":
            yield node.payload
        elif kind == "class":
            yield from node.payload
        elif kind == "sequence":
            yield from sequence_of_length(node.children, 0, length)
        elif kind == "choice":
            for child in node.children:
                yield from of_length(child, length)
        elif kind == "repeat":
            child = node.children[0]
            for count in range(node.min_rep, node.max_rep + 1):
                yield from sequence_of_length((child,) * count, 0, length)
        elif kind in {"group", "passthrough"}:
            if node.children:
                yield from of_length(node.children[0], length)
            elif length == 0:
                yield ""
        elif kind in {"anchor", "lookaround"}:
            if length == 0:
                yield ""

    steps = [0]

    def sequence_of_length(children, index, length):
        steps[0] += 1
        if steps[0] > max_steps:
            raise _OutOfBudget
        if index == len(children):
            if length == 0:
                yield ""
            return
        child = children[index]
        low, high = bounds(child)
        rest_low = sum(bounds(c)[0] for c in children[index + 1:])
        upper = length - rest_low if high is None else min(high, length - rest_low)
        for head_length in range(low, upper + 1):
            for head in of_length(child, head_length):
                for tail in sequence_of_length(children, index + 1, length - head_length):
                    yield head + tail

    root = builder.root
    low, high = bounds(root)
    if low is None:
        return []
    # Unknown-length parts (backreferences) yield nothing here, so cap the length.
    max_length = high if high is not None else low + MAX_EXTRA_LENGTH
    results, seen, candidates = [], set(), 0
    length = low
    try:
        while len(results) < n and candidates < max_candidates and length <= max_length:
            for sample in of_length(root, length):
                candidates += 1
                if sample not in seen and compiled.fullmatch(sample):
                    seen.add(sample)
                    results.append(sample)
                    if len(results) >= n:
                        break
                if candidates >= max_candidates:
                    break
            length += 1
    except _OutOfBudget:
        pass
    return results


MAX_EXTRA_LENGTH = 64
MAX_STEPS = 2_000_000


class _OutOfBudget(Exception):
    pass
