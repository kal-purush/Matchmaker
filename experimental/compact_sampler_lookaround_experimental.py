import random

from regex_positive_generator.experimental.compact_sampler import (
    CompactGraphSampler,
    MAX_RANDOM_ATTEMPTS,
    MAX_RANDOM_ATTEMPTS_WITH_REQUIRED_VALIDATION,
    MIN_RANDOM_ATTEMPTS,
)


MAX_PREFIX_ALTERNATIVES = 128
MAX_CLASS_PREFIX_EXPANSION = 16


class ExperimentalLookaroundCompactGraphSampler(CompactGraphSampler):
    """Opt-in compact sampler with targeted constructive lookaround handling.

    This intentionally leaves the production CompactGraphSampler untouched. It
    only recognizes small, common assertion shapes:
    - positive lookahead: (?=.*CLASS)
    - negative lookahead with finite prefixes: (?!abc|\\d11)
    - positive/negative fixed-width finite lookbehind: (?<=abc), (?<!abc)
    Unsupported assertions are reported and decoded like the baseline sampler.
    """

    def __init__(self, builder, *, validate=True):
        super().__init__(builder, validate=validate)
        self.required_classes = []
        self.forbidden_prefixes = []
        self.lookbehind_constraints = {}
        self.unsupported_lookarounds = []
        self._analyze_lookarounds(self.builder.root)

    def generate_samples(self, n, seed=0, on_sample=None):
        if n <= 0:
            return []

        if not self.builder.simplified:
            seen = set()
            results = []
            simplified = self.builder.build_simplified_graph(seed=seed)
            simplified_sampler = ExperimentalLookaroundCompactGraphSampler(simplified, validate=self.validate)
            simplified_sampler._inherit_lookaround_constraints(self)
            results.extend(
                simplified_sampler._generate_direct(
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

    def _inherit_lookaround_constraints(self, other):
        self.required_classes = list(other.required_classes)
        self.forbidden_prefixes = list(other.forbidden_prefixes)
        self.unsupported_lookarounds = list(other.unsupported_lookarounds)

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
            for index in range(min(total, max(n, 32))):
                if index in tried:
                    continue
                consider(index)
                if len(results) >= n:
                    break

        return results

    def decode_index(self, index):
        sample = super().decode_index(index)
        if sample is None:
            return None
        sample = self._satisfy_required_classes(sample)
        if sample is None:
            return None
        if self._violates_forbidden_prefix(sample):
            return None
        return sample

    def lookaround_report(self):
        return {
            "positive_contains_classes": len(self.required_classes),
            "negative_prefix_lookarounds": len(self.forbidden_prefixes),
            "forbidden_prefixes": list(self.forbidden_prefixes[:20]),
            "finite_lookbehinds": len(self.lookbehind_constraints),
            "unsupported_lookarounds": list(self.unsupported_lookarounds),
        }

    def _analyze_lookarounds(self, node):
        if node.kind == "lookaround":
            payload = node.payload or {}
            if payload.get("direction", 1) < 0:
                suffixes = self._finite_lookbehind_suffixes(node.children[0] if node.children else None)
                if suffixes:
                    self.lookbehind_constraints[id(node)] = {
                        "negative": bool(payload.get("negative")),
                        "suffixes": tuple(suffixes),
                    }
                else:
                    tag = "negative_lookbehind" if payload.get("negative") else "positive_lookbehind"
                    self.unsupported_lookarounds.append(tag)
            elif payload.get("negative"):
                prefixes = self._finite_prefixes(node.children[0] if node.children else None)
                if prefixes:
                    self.forbidden_prefixes.extend(prefixes)
                else:
                    self.unsupported_lookarounds.append("negative_lookahead")
            else:
                required_class = self._positive_contains_class(node.children[0] if node.children else None)
                if required_class:
                    self.required_classes.append(tuple(required_class))
                else:
                    self.unsupported_lookarounds.append("positive_lookahead")
        for child in node.children:
            self._analyze_lookarounds(child)

    def _positive_contains_class(self, node):
        if node is None or node.kind != "sequence" or len(node.children) != 2:
            return None
        repeat, required = node.children
        if required.kind != "class":
            return None
        if repeat.kind != "repeat" or repeat.min_rep != 0:
            return None
        if not repeat.children:
            return None
        repeated = repeat.children[0]
        if repeated.kind != "sequence" or len(repeated.children) != 1:
            return None
        if repeated.children[0].kind != "class":
            return None
        return required.payload

    def _finite_prefixes(self, node):
        prefixes = self._expand_prefix_node(node)
        if not prefixes or len(prefixes) > MAX_PREFIX_ALTERNATIVES:
            return None
        return sorted(prefixes)

    def _finite_lookbehind_suffixes(self, node):
        suffixes = self._expand_prefix_node(node)
        if not suffixes or len(suffixes) > MAX_PREFIX_ALTERNATIVES:
            return None
        lengths = {len(suffix) for suffix in suffixes}
        if len(lengths) != 1:
            return None
        return sorted(suffixes)

    def _expand_prefix_node(self, node):
        if node is None:
            return {""}
        if node.kind == "literal":
            return {node.payload}
        if node.kind == "class":
            chars = tuple(node.payload)
            if len(chars) > MAX_CLASS_PREFIX_EXPANSION:
                return None
            return set(chars)
        if node.kind in {"anchor", "lookaround"}:
            return {""}
        if node.kind in {"group", "passthrough"}:
            return self._expand_prefix_node(node.children[0]) if node.children else {""}
        if node.kind == "choice":
            out = set()
            for child in node.children:
                expanded = self._expand_prefix_node(child)
                if expanded is None:
                    return None
                out.update(expanded)
                if len(out) > MAX_PREFIX_ALTERNATIVES:
                    return None
            return out
        if node.kind == "sequence":
            prefixes = {""}
            for child in node.children:
                expanded = self._expand_prefix_node(child)
                if expanded is None:
                    return None
                prefixes = {left + right for left in prefixes for right in expanded}
                if len(prefixes) > MAX_PREFIX_ALTERNATIVES:
                    return None
            return prefixes
        if node.kind == "repeat":
            if node.min_rep != node.max_rep:
                return None
            child = node.children[0] if node.children else None
            expanded = self._expand_prefix_node(child)
            if expanded is None:
                return None
            prefixes = {""}
            for _ in range(node.min_rep):
                prefixes = {left + right for left in prefixes for right in expanded}
                if len(prefixes) > MAX_PREFIX_ALTERNATIVES:
                    return None
            return prefixes
        return None

    def _satisfy_required_classes(self, sample):
        if not self.required_classes:
            return sample
        chars = list(sample)
        if not chars:
            return None

        used_positions = set()
        for class_index, required in enumerate(self.required_classes):
            existing_position = self._existing_required_position(chars, used_positions, required)
            if existing_position is not None:
                used_positions.add(existing_position)
                continue
            position = self._replacement_position(chars, used_positions, class_index)
            if position is None:
                return None
            chars[position] = self._representative_required_char(required)
            used_positions.add(position)
        return "".join(chars)

    def _existing_required_position(self, chars, used_positions, required):
        for position, ch in enumerate(chars):
            if position not in used_positions and ch in required:
                return position
        return None

    def _replacement_position(self, chars, used_positions, class_index):
        if len(used_positions) >= len(chars):
            return None
        start = class_index % len(chars)
        for offset in range(len(chars)):
            position = (start + offset) % len(chars)
            if position not in used_positions:
                return position
        return None

    def _representative_required_char(self, required):
        preferred = "0Aa!@#$%^&*()-_=+`~[]{}?|"
        for ch in preferred:
            if ch in required:
                return ch
        return tuple(required)[0]

    def _violates_forbidden_prefix(self, sample):
        return any(sample.startswith(prefix) for prefix in self.forbidden_prefixes)

    def _decode_lookaround(self, node, prefix, memory, checkpoints):
        constraint = self.lookbehind_constraints.get(id(node))
        if constraint is None:
            return super()._decode_lookaround(node, prefix, memory, checkpoints)

        matches = any(prefix.endswith(suffix) for suffix in constraint["suffixes"])
        if constraint["negative"]:
            return (None, memory) if matches else ("", memory)
        return ("", memory) if matches else (None, memory)
