import random
import re

from regex_positive_generator.experimental.counting import (
    saturating_add,
    saturating_mul,
    saturating_pow,
)
from regex_positive_generator.parser import REGEX_AVAILABLE

try:
    import regex
except ImportError:  # pragma: no cover - optional dependency
    regex = None


MIN_RANDOM_ATTEMPTS = 3000
MAX_RANDOM_ATTEMPTS = 10000
MAX_RANDOM_ATTEMPTS_WITH_REQUIRED_VALIDATION = 20000


class CompactGraphSampler:
    def __init__(self, builder, *, validate=True):
        self.builder = builder
        self.validate = validate
        self.counts = {}
        self.sequence_suffix_counts = {}
        self.repeat_buckets = {}
        self.repeat_divisors = {}
        self.repeat_representatives = {}
        self.seed = 0

    def generate_samples(self, n, seed=0, on_sample=None):
        if n <= 0:
            return []

        if not self.builder.simplified:
            seen = set()
            results = []
            simplified = self.builder.build_simplified_graph(seed=seed)
            results.extend(
                CompactGraphSampler(simplified, validate=self.validate)._generate_direct(
                    n,
                    seed=seed,
                    seen=seen,
                    on_sample=on_sample,
                )
            )
            remaining = n - len(results)
            if remaining <= 0:
                return results
            results.extend(self._generate_direct(remaining, seed=seed + 1, seen=seen, on_sample=on_sample))
            return results

        return self._generate_direct(n, seed=seed, seen=set(), on_sample=on_sample)

    def _generate_direct(self, n, *, seed, seen, on_sample=None):
        if self.seed != seed:
            self.counts.clear()
            self.sequence_suffix_counts.clear()
            self.repeat_buckets.clear()
            self.repeat_divisors.clear()
            self.repeat_representatives.clear()
        self.seed = seed
        total = self._count(self.builder.root)
        if total <= 0:
            return []

        results = []

        def consider(index):
            sample = self.decode_index(index)
            if sample is None or sample in seen:
                return
            if self.validate and not self._accept(sample):
                return
            seen.add(sample)
            results.append(sample)
            if on_sample is not None:
                on_sample(sample)

        if total <= n:
            for index in range(total):
                consider(index)
                if len(results) >= n:
                    break
            return results

        rng = random.Random(seed)
        tried = set()
        retry_multiplier = 200 if self.builder.requires_match_validation else 50
        attempt_cap = (
            MAX_RANDOM_ATTEMPTS_WITH_REQUIRED_VALIDATION
            if self.builder.requires_match_validation
            else MAX_RANDOM_ATTEMPTS
        )
        max_attempts = min(max(n * retry_multiplier, MIN_RANDOM_ATTEMPTS), attempt_cap)
        attempts = 0
        while len(results) < n and attempts < max_attempts:
            attempts += 1
            index = rng.randrange(total)
            if index in tried:
                continue
            tried.add(index)
            consider(index)

        if len(results) < n:
            # Anchors/backrefs/conditionals can make large swaths of the
            # (statically over-approximate) index space unrealizable, to the
            # point random sampling alone never lands on a realizable index.
            # The lowest indices disproportionately land on minimal,
            # realizable paths (lowest repeat counts, first branch at every
            # choice), so fall back to trying those when random sampling
            # comes up empty.
            for index in range(min(total, max(n, 32))):
                if index in tried:
                    continue
                consider(index)
                if len(results) >= n:
                    break

        return results

    def decode_index(self, index):
        checkpoints = []
        text, _memory = self._decode_node(self.builder.root, index, "", {}, checkpoints)
        if text is None:
            return None
        if not self._checkpoints_satisfied(text, checkpoints):
            return None
        return text

    def _is_word_char(self, ch):
        return bool(ch) and (ch.isalnum() or ch == "_")

    def _checkpoints_satisfied(self, final_text, checkpoints):
        total_len = len(final_text)
        for checkpoint in checkpoints:
            kind = checkpoint[0]
            if kind == "end":
                _, position = checkpoint
                at_true_end = position == total_len
                at_trailing_newline = position == total_len - 1 and final_text.endswith("\n")
                if not (at_true_end or at_trailing_newline):
                    return False
            elif kind == "end_string":
                _, position = checkpoint
                if position != total_len:
                    return False
            elif kind in ("word_boundary", "not_word_boundary"):
                _, position, before_is_word = checkpoint
                next_is_word = self._is_word_char(final_text[position]) if position < total_len else False
                is_boundary = before_is_word != next_is_word
                if kind == "word_boundary" and not is_boundary:
                    return False
                if kind == "not_word_boundary" and is_boundary:
                    return False
        return True

    def _count(self, node):
        node_id = id(node)
        if node_id not in self.counts:
            if node.kind == "sequence":
                total = 1
                for child in node.children:
                    total = saturating_mul(total, self._count(child))
            elif node.kind in {"choice", "conditional"}:
                total = 0
                for child in node.children:
                    total = saturating_add(total, self._count(child))
            elif node.kind == "repeat":
                total = 0
                for _rep, bucket in self._repeat_buckets(node):
                    total = saturating_add(total, bucket)
            elif node.kind == "class":
                total = len(node.payload)
            elif node.kind == "impossible":
                total = 0
            elif node.kind in {"group", "passthrough"}:
                total = self._count(node.children[0]) if node.children else 1
            else:
                total = 1
            self.counts[node_id] = total
        return self.counts[node_id]

    def _decode_node(self, node, index, prefix, memory, checkpoints):
        if node.kind == "sequence":
            output = []
            cur_prefix = prefix
            suffix_counts = self._sequence_suffix_counts(node)
            for child, suffix_count in zip(node.children, suffix_counts):
                child_count = self._count(child)
                child_index = (index // suffix_count) % child_count if child_count else 0
                text, memory = self._decode_node(child, child_index, cur_prefix, memory, checkpoints)
                if text is None:
                    return None, memory
                output.append(text)
                cur_prefix += text
            return "".join(output), memory

        if node.kind == "choice":
            for child in node.children:
                child_count = self._count(child)
                if index < child_count:
                    return self._decode_node(child, index, prefix, memory, checkpoints)
                index -= child_count
            return None, memory

        if node.kind == "conditional":
            yes_node, no_node = node.children
            chosen = yes_node if memory.get(node.payload) is not None else no_node
            chosen_count = self._count(chosen)
            child_index = index % chosen_count if chosen_count else 0
            return self._decode_node(chosen, child_index, prefix, memory, checkpoints)

        if node.kind == "repeat":
            child = node.children[0]
            child_count = self._count(child)
            for rep, bucket in self._repeat_buckets(node):
                if index < bucket:
                    pieces = []
                    cur_prefix = prefix
                    for divisor in self._repeat_divisors(node, rep):
                        child_index = (index // divisor) % child_count if child_count else 0
                        text, memory = self._decode_node(child, child_index, cur_prefix, memory, checkpoints)
                        if text is None:
                            return None, memory
                        pieces.append(text)
                        cur_prefix += text
                    return "".join(pieces), memory
                index -= bucket
            return None, memory

        if node.kind == "literal":
            return node.payload, memory

        if node.kind == "impossible":
            return None, memory

        if node.kind == "class":
            chars = node.payload
            if not chars:
                return None, memory
            return chars[index % len(chars)], memory

        if node.kind == "group":
            if not node.children:
                return "", memory
            text, memory = self._decode_node(node.children[0], index, prefix, memory, checkpoints)
            if text is None:
                return None, memory
            memory[node.payload] = text
            return text, memory

        if node.kind == "passthrough":
            if not node.children:
                return "", memory
            return self._decode_node(node.children[0], index, prefix, memory, checkpoints)

        if node.kind == "anchor":
            return self._decode_anchor(node, prefix, checkpoints), memory

        if node.kind == "backref":
            if node.payload not in memory:
                return None, memory
            return memory[node.payload], memory

        if node.kind == "lookaround":
            return self._decode_lookaround(node, prefix, memory, checkpoints)

        return "", memory

    def _decode_lookaround(self, node, prefix, memory, checkpoints):
        return "", memory

    def _decode_anchor(self, node, prefix, checkpoints):
        kind = node.payload
        position = len(prefix)
        if kind == "start":
            return "" if position == 0 else None
        if kind == "start_line":
            if position == 0:
                return ""
            if self.builder.effective_flags & re.MULTILINE and prefix.endswith("\n"):
                return ""
            return None
        if kind == "start_string":
            return "" if position == 0 else None
        if kind == "end":
            checkpoints.append(("end", position))
            return ""
        if kind == "end_line":
            checkpoints.append(("end", position))
            return ""
        if kind == "end_string":
            checkpoints.append(("end_string", position))
            return ""
        if kind == "word_boundary":
            before_is_word = self._is_word_char(prefix[-1:])
            checkpoints.append(("word_boundary", position, before_is_word))
            return ""
        if kind == "not_word_boundary":
            before_is_word = self._is_word_char(prefix[-1:])
            checkpoints.append(("not_word_boundary", position, before_is_word))
            return ""
        return ""

    def _sequence_suffix_counts(self, node):
        node_id = id(node)
        if node_id not in self.sequence_suffix_counts:
            suffix_counts = []
            suffix = 1
            for child in reversed(node.children):
                suffix_counts.append(suffix)
                suffix = saturating_mul(suffix, self._count(child))
            suffix_counts.reverse()
            self.sequence_suffix_counts[node_id] = tuple(suffix_counts)
        return self.sequence_suffix_counts[node_id]

    def _repeat_buckets(self, node):
        node_id = id(node)
        if node_id not in self.repeat_buckets:
            child_count = self._count(node.children[0])
            reps = self._repeat_representatives(node)
            if not reps:
                self.repeat_buckets[node_id] = ()
            else:
                bucket = max(saturating_pow(child_count, rep) for rep in reps)
                self.repeat_buckets[node_id] = tuple(
                    (rep, bucket) for rep in reps
                )
        return self.repeat_buckets[node_id]

    def _repeat_representatives(self, node):
        node_id = id(node)
        if node_id not in self.repeat_representatives:
            min_rep = node.min_rep
            max_rep = node.max_rep
            if max_rep < min_rep:
                reps = []
            elif min_rep == max_rep:
                reps = [min_rep]
            elif max_rep == min_rep + 1:
                reps = [min_rep, max_rep]
            else:
                rng = random.Random(f"{self.seed}:{min_rep}:{max_rep}")
                middle_rep = rng.randint(min_rep + 1, max_rep - 1)
                reps = [min_rep, middle_rep, max_rep]
            self.repeat_representatives[node_id] = tuple(sorted(set(reps)))
        return self.repeat_representatives[node_id]

    def _repeat_divisors(self, node, rep):
        key = (id(node), rep)
        if key not in self.repeat_divisors:
            child_count = self._count(node.children[0])
            self.repeat_divisors[key] = tuple(
                saturating_pow(child_count, remaining)
                for remaining in range(rep - 1, -1, -1)
            )
        return self.repeat_divisors[key]

    def _accept(self, sample):
        if not self.validate:
            return True
        engine = regex if REGEX_AVAILABLE and regex is not None else re
        try:
            return engine.fullmatch(self.builder.regex, sample, self.builder.flags) is not None
        except Exception:
            return False
