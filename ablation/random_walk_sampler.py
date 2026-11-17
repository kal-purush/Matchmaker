"""Random-walk positive baseline for the ablation.

A plain random walk over the regex graph: at every node it picks a random branch, a
random repeat count, or a random character, and the resulting string is one sample.
There is no counting, no boundary-value logic and no deduplication (repeated
strings are kept); walks whose string does not fullmatch the regex (for example
because of a lookaround) are redrawn.

The graph is Matchmaker's ``CompactRegexGraphBuilder`` graph, except that unbounded
repeats (``*``, ``+``, ``{m,}``) may repeat up to ``min + 20`` times (exrex's default)
instead of Matchmaker's ``min + 3``.
"""

import random
import re
import sre_constants

from matchmaker.compact import CompactRegexGraphBuilder

UNBOUNDED_EXTRA = 20
_REPEAT_OPS = {sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT}
if hasattr(sre_constants, "POSSESSIVE_REPEAT"):
    _REPEAT_OPS.add(sre_constants.POSSESSIVE_REPEAT)


class _WalkGraphBuilder(CompactRegexGraphBuilder):
    def _build_item(self, op, av, *, depth):
        node = super()._build_item(op, av, depth=depth)
        if (
            op in _REPEAT_OPS
            and av[1] == sre_constants.MAXREPEAT
            and not self._repeat_child_is_anchor_limited(av[2])
        ):
            node.max_rep = node.min_rep + UNBOUNDED_EXTRA
        return node


def _walk(node, rng, memory):
    kind = node.kind
    if kind in {"sequence", "passthrough"}:
        parts = []
        for child in node.children:
            text = _walk(child, rng, memory)
            if text is None:
                return None
            parts.append(text)
        return "".join(parts)
    if kind == "choice":
        return _walk(rng.choice(node.children), rng, memory)
    if kind == "repeat":
        parts = []
        for _ in range(rng.randint(node.min_rep, node.max_rep)):
            text = _walk(node.children[0], rng, memory)
            if text is None:
                return None
            parts.append(text)
        return "".join(parts)
    if kind == "class":
        return rng.choice(node.payload) if node.payload else None
    if kind == "literal":
        return node.payload
    if kind == "group":
        text = _walk(node.children[0], rng, memory)
        if text is not None:
            memory[node.payload] = text
        return text
    if kind == "backref":
        return memory.get(node.payload)
    if kind == "conditional":
        yes, no = node.children
        return _walk(yes if node.payload in memory else no, rng, memory)
    if kind in {"anchor", "lookaround"}:
        return ""
    return None  # "impossible"


def random_walk_samples(pattern, n, seed=0):
    """Return up to ``n`` random-walk strings that fullmatch ``pattern``, repeats kept."""

    if n <= 0:
        return []
    root = _WalkGraphBuilder(pattern, validate=True).root
    compiled = re.compile(pattern)
    rng = random.Random(seed)
    samples = []
    for _ in range(max(n * 50, 5000)):
        text = _walk(root, rng, {})
        if text is not None and compiled.fullmatch(text):
            samples.append(text)
            if len(samples) == n:
                break
    return samples
