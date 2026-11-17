"""Experimental: branch-balanced positive sampling.

``CompactGraphSampler`` draws one uniform index over every string the regex can
produce, so each alternation branch is chosen in proportion to how many strings it
holds; small branches are starved (in ``(?:[a-z]{5}|[0-9])`` a digit has a chance
of about 1 in a million). This sampler instead builds the index top-down: at each
choice it picks uniformly among branches that still have undrawn strings (a used-up
branch is skipped instead of yielding duplicates). Repeats are entered the same way:
a representative repeat count is picked uniformly and each repetition is balanced, so
choices inside ``?``, ``*`` and ``{m,n}`` are balanced too. Everything else (repeats, classes,
lookaround handling, validation, deduplication) is decoded by the original sampler.
Languages no larger than the sample budget are still enumerated completely.
"""

import random

from matchmaker.compact.lookaround_sampler import LookaroundCompactGraphSampler


class BranchBalancedSampler(LookaroundCompactGraphSampler):
    def generate_samples(self, n, seed=0, on_sample=None):
        # Same as the parent, which hard-codes its own class for the simplified graph.
        if n <= 0:
            return []
        if self.builder.simplified:
            return self._generate_direct(n, seed=seed, seen=set(), on_sample=on_sample)
        seen = set()
        simplified = BranchBalancedSampler(
            self.builder.build_simplified_graph(seed=seed), validate=self.validate
        )
        simplified._inherit_lookaround_constraints(self)
        results = simplified._generate_direct(n, seed=seed, seen=seen, on_sample=on_sample)
        if len(results) < n:
            results.extend(self._generate_direct(
                n - len(results), seed=seed + 1, seen=seen, on_sample=on_sample
            ))
        return results

    def _generate_direct(self, n, *, seed, seen, on_sample=None):
        self._rng = random.Random(seed)
        self._budget = n
        # (node, option) -> full indices already drawn through that option.
        self._drawn = {}
        return super()._generate_direct(n, seed=seed, seen=seen, on_sample=on_sample)

    def decode_index(self, index):
        root = self.builder.root
        if self._count(root) > getattr(self, "_budget", 0):
            path = []
            index = self._balanced_index(root, 1, path)
            for key in path:
                self._drawn.setdefault(key, set()).add(index)
        return super().decode_index(index)

    def _pick(self, options, path):
        """Pick uniformly among (offset, key, capacity, payload) options that still have
        undrawn strings. Capacity counts whole strings through the option (its own
        strings times the variation around it), so a one-string branch like ``Z`` in
        ``\\d\\d(?:Z|...)`` stays available while the digits still vary. If all are
        used up, fall back to every option."""
        fresh = [o for o in options if len(self._drawn.get(o[1], ())) < o[2]]
        choice = self._rng.choice(fresh or options)
        path.append(choice[1])
        return choice

    def _balanced_index(self, node, mult, path):
        # mult: number of distinct contexts (variation outside this node) it appears in.
        if node.kind == "choice":
            offset, options = 0, []
            for position, child in enumerate(node.children):
                count = self._count(child)
                if count > 0:
                    options.append((offset, (id(node), position), count * mult, child))
                offset += count
            offset, _key, _cap, child = self._pick(options, path)
            return offset + self._balanced_index(child, mult, path)
        if node.kind == "repeat" and node.children:
            # Pick one of the representative repeat counts uniformly, then balance each
            # repetition, so a choice inside ``?``, ``*`` or ``{m,n}`` is balanced too.
            child = node.children[0]
            child_count = self._count(child)
            offset, options = 0, []
            for rep, bucket in self._repeat_buckets(node):
                distinct = child_count ** rep
                if rep == 0 or distinct > 0:
                    options.append((offset, (id(node), rep), max(distinct, 1) * mult, rep))
                offset += bucket
            if not options:
                return 0
            offset, _key, _cap, rep = self._pick(options, path)
            inner = mult * child_count ** max(rep - 1, 0)
            return offset + sum(
                self._balanced_index(child, inner, path) * divisor
                for divisor in self._repeat_divisors(node, rep)
            )
        if node.kind == "sequence":
            counts = [self._count(child) for child in node.children]
            suffixes = self._sequence_suffix_counts(node)
            total = 0
            for position, (child, suffix) in enumerate(zip(node.children, suffixes)):
                others = 1
                for other_position, count in enumerate(counts):
                    if other_position != position:
                        others *= count
                total += self._balanced_index(child, mult * others, path) * suffix
            return total
        if node.kind in {"group", "passthrough"} and node.children:
            return self._balanced_index(node.children[0], mult, path)
        return self._rng.randrange(max(self._count(node), 1))
