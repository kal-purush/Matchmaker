import random

from matchmaker.compact.compact_sampler import (
    MAX_RANDOM_ATTEMPTS,
    MAX_RANDOM_ATTEMPTS_WITH_REQUIRED_VALIDATION,
    MIN_RANDOM_ATTEMPTS,
)
from matchmaker.compact.lookaround_sampler import (
    LookaroundCompactGraphSampler,
)


class RandomPositiveSampler(LookaroundCompactGraphSampler):
    """Sample uniformly at random from graph choices and repeat counts."""

    def __init__(self, builder, *, validate=True):
        if builder.simplified:
            raise ValueError("RandomPositiveSampler requires a non-simplified builder")
        super().__init__(builder, validate=validate)
        self._rng = random.Random()

    def generate_samples(self, n, seed=0, on_sample=None):
        if n <= 0:
            return []

        self._rng = random.Random(seed)
        self.counts.clear()
        self.sequence_suffix_counts.clear()
        self.repeat_buckets.clear()
        self.repeat_divisors.clear()
        self.repeat_representatives.clear()

        if self.seed != seed:
            self.seed = seed
        if self._count(self.builder.root) <= 0:
            return []

        retry_multiplier = 200 if self.builder.requires_match_validation else 50
        attempt_cap = (
            MAX_RANDOM_ATTEMPTS_WITH_REQUIRED_VALIDATION
            if self.builder.requires_match_validation
            else MAX_RANDOM_ATTEMPTS
        )
        max_attempts = min(max(n * retry_multiplier, MIN_RANDOM_ATTEMPTS), attempt_cap)
        results = []
        seen = set()

        for _ in range(max_attempts):
            if len(results) >= n:
                break
            sample = self.decode_index(0)
            if sample is None or sample in seen:
                continue
            if self.validate and not self._accept(sample):
                continue
            seen.add(sample)
            results.append(sample)
            if on_sample is not None:
                on_sample(sample)
        return results

    def _decode_node(self, node, index, prefix, memory, checkpoints):
        if node.kind == "sequence":
            output = []
            current_prefix = prefix
            for child in node.children:
                text, memory = self._decode_node(
                    child, 0, current_prefix, memory, checkpoints
                )
                if text is None:
                    return None, memory
                output.append(text)
                current_prefix += text
            return "".join(output), memory

        if node.kind == "choice":
            eligible = [child for child in node.children if self._count(child) > 0]
            if not eligible:
                return None, memory
            child = self._rng.choice(eligible)
            return self._decode_node(child, 0, prefix, memory, checkpoints)

        if node.kind == "repeat":
            child = node.children[0]
            if self._count(child) <= 0:
                return (("", memory) if node.min_rep == 0 else (None, memory))
            repetition_count = self._rng.randint(node.min_rep, node.max_rep)
            output = []
            current_prefix = prefix
            for _ in range(repetition_count):
                text, memory = self._decode_node(
                    child, 0, current_prefix, memory, checkpoints
                )
                if text is None:
                    return None, memory
                output.append(text)
                current_prefix += text
            return "".join(output), memory

        if node.kind == "class":
            if not node.payload:
                return None, memory
            return self._rng.choice(node.payload), memory

        return super()._decode_node(node, index, prefix, memory, checkpoints)
