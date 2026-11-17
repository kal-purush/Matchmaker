from matchmaker.compact.negative_choices import ViolationChoiceHelper
from matchmaker.compact.negative_context import iter_node_contexts
from matchmaker.compact.negative_choices import dedupe_preserve_order
from matchmaker.compact.negative_families import (
    family_candidates,
    minimal_local_faults,
    optimized_family_fill_candidates,
    select_violation_families,
)
from matchmaker.compact.negative_sampler import (
    NegativeGraphSampler,
    run_with_timeout,
)


STAGE_COMPONENT = {
    "empty_seed": "M",
    "minimal_local_fault": "L",
    "local_extended": "L",
    "family": "M",
    "optimized_family_fill": "M",
}


class StagedNegativeSampler(NegativeGraphSampler):
    def __init__(
        self,
        pattern,
        *,
        flags=0,
        validate=True,
        use_local=True,
        use_mutation=True,
        local_extended=False,
        exclude_local_strings=False,
        skip_stages=(),
    ):
        super().__init__(pattern, flags=flags, validate=validate)
        self.use_local = use_local
        self.use_mutation = use_mutation
        self.local_extended = local_extended
        # Local-complement ablation: block the exact strings the local stage would
        # produce, wherever they come from; and skip individual stages by name
        # (see STAGE_COMPONENT), e.g. "optimized_family_fill".
        self.exclude_local_strings = exclude_local_strings
        self.skip_stages = frozenset(skip_stages)

    def generate(
        self,
        n=1000,
        seed=0,
        use_fullmatch=True,
        timeout_seconds=None,
        on_sample=None,
        return_tags=False,
    ):
        if n <= 0:
            return []
        if timeout_seconds is None:
            from matchmaker.parser import DEFAULT_REGEX_TIMEOUT_SECONDS

            timeout_seconds = DEFAULT_REGEX_TIMEOUT_SECONDS

        def _generate():
            helper = ViolationChoiceHelper(seed=seed, flags=self.flags)
            seen = set()
            if self.exclude_local_strings:
                seen.update(minimal_local_faults(self.builder, helper))
            results = []
            tags = []

            def add_candidate(candidate, family):
                added = self._add_candidate(
                    results,
                    seen,
                    candidate,
                    n=n,
                    use_fullmatch=use_fullmatch,
                    on_sample=on_sample,
                    family=family,
                )
                if added:
                    tags.append(family)
                return added

            if self._stage_enabled("empty_seed"):
                before = len(results)
                self._add_empty_seed(
                    results,
                    seen,
                    n=n,
                    use_fullmatch=use_fullmatch,
                    on_sample=on_sample,
                )
                if len(results) > before:
                    tags.append("empty_seed")
                if len(results) >= n:
                    return self._tagged_result(results, tags, return_tags, n)

            if self._stage_enabled("minimal_local_fault"):
                for candidate in minimal_local_faults(self.builder, helper):
                    add_candidate(candidate, "minimal_local_fault")
                    if len(results) >= n:
                        return self._tagged_result(results, tags, return_tags, n)

                if self.local_extended:
                    for candidate in _extended_local_faults(self.builder, helper):
                        add_candidate(candidate, "local_extended")
                        if len(results) >= n:
                            return self._tagged_result(results, tags, return_tags, n)

            if self._stage_enabled("family"):
                families = select_violation_families(self.builder)
                family_map = family_candidates(self.builder, helper)
                selected_families = [family for family in families if family in family_map]
                cursors = {family: 0 for family in selected_families}
                active = set(selected_families)

                while active and len(results) < n:
                    for family in selected_families:
                        if family not in active:
                            continue
                        candidates = family_map[family]
                        cursor = cursors[family]
                        while cursor < len(candidates):
                            candidate = candidates[cursor]
                            cursor += 1
                            if add_candidate(candidate, family):
                                break
                        cursors[family] = cursor
                        if cursor >= len(candidates):
                            active.remove(family)
                        if len(results) >= n:
                            return self._tagged_result(results, tags, return_tags, n)

            if self._stage_enabled("optimized_family_fill"):
                fill_attempts = 0
                max_raw_fill_attempts = max(n * 200, 1000)
                for candidate in optimized_family_fill_candidates(self.builder, helper):
                    fill_attempts += 1
                    add_candidate(candidate, "optimized_family_fill")
                    if len(results) >= n:
                        return self._tagged_result(results, tags, return_tags, n)
                    if not self.validate and fill_attempts >= max_raw_fill_attempts:
                        break

            return self._tagged_result(results, tags, return_tags, n)

        return run_with_timeout(_generate, timeout_seconds)

    @staticmethod
    def _tagged_result(results, tags, return_tags, n):
        samples = results[:n]
        return list(zip(samples, tags[:n])) if return_tags else samples

    def _stage_enabled(self, stage):
        if stage in self.skip_stages:
            return False
        component = STAGE_COMPONENT[stage]
        return self.use_local if component == "L" else self.use_mutation


def _extended_local_faults(builder, helper):
    from matchmaker.compact.negative_families import (
        _has_nullable_wildcard_repeat,
        _local_substitution_allowed,
    )

    candidates = []
    suppress_nullable = _has_nullable_wildcard_repeat(builder)
    for context in iter_node_contexts(builder.root):
        node = context.node
        if node.kind in {"literal", "class"} and not _local_substitution_allowed(
            context,
            suppress_nullable=suppress_nullable,
        ):
            continue
        replacements = []
        if node.kind == "literal":
            replacements = helper.literal_choices(node.payload)[1:]
        elif node.kind == "class":
            replacements = helper.class_violation_choices(node.payload)[1:]
        elif node.kind == "backref":
            valid = context.memory.get(node.payload, "")
            if valid:
                replacements = [
                    valid[:-1] + alternative
                    for alternative in helper.literal_choices(valid[-1])
                ]
        candidates.extend(
            context.prefix + replacement + context.suffix
            for replacement in replacements
        )
    return dedupe_preserve_order(candidates)
