from matchmaker.compact import CompactRegexGraphBuilder
from matchmaker.compact.balanced_sampler import BranchBalancedSampler
from matchmaker.compact.lookaround_sampler import (
    LookaroundCompactGraphSampler,
)
from matchmaker.compact.negative_sampler import (
    generate_negative_samples,
)

from .negative_stages import StagedNegativeSampler
from .random_positive_sampler import RandomPositiveSampler


def _positive_samples(pattern, n_pos, seed, validate, *, randomize, balanced=False):
    builder = CompactRegexGraphBuilder(pattern, validate=validate)
    if randomize:
        sampler_type = RandomPositiveSampler
    elif balanced:
        sampler_type = BranchBalancedSampler
    else:
        sampler_type = LookaroundCompactGraphSampler
    return sampler_type(builder, validate=validate).generate_samples(n_pos, seed=seed)


def _negative_samples(
    pattern,
    n_neg,
    seed,
    validate,
    *,
    use_local=True,
    use_mutation=True,
    local_extended=False,
    include_negative_tags=False,
):
    if (
        include_negative_tags
        or not use_local
        or not use_mutation
        or local_extended
    ):
        sampler = StagedNegativeSampler(
            pattern,
            validate=validate,
            use_local=use_local,
            use_mutation=use_mutation,
            local_extended=local_extended,
        )
        return sampler.generate(
            n=n_neg,
            seed=seed,
            use_fullmatch=True,
            return_tags=include_negative_tags,
        )
    return generate_negative_samples(
        pattern,
        n=n_neg,
        seed=seed,
        use_fullmatch=True,
        validate=validate,
    )


def _full(
    pattern,
    n_pos,
    n_neg,
    seed,
    validate=True,
    *,
    local_extended=False,
    include_negative_tags=False,
):
    return (
        _positive_samples(pattern, n_pos, seed, validate, randomize=False),
        _negative_samples(
            pattern,
            n_neg,
            seed,
            validate,
            local_extended=local_extended,
            include_negative_tags=include_negative_tags,
        ),
    )


def _no_pos(
    pattern,
    n_pos,
    n_neg,
    seed,
    validate=True,
    *,
    local_extended=False,
    include_negative_tags=False,
):
    return [], _negative_samples(
        pattern,
        n_neg,
        seed,
        validate,
        local_extended=local_extended,
        include_negative_tags=include_negative_tags,
    )


def _random_pos(
    pattern,
    n_pos,
    n_neg,
    seed,
    validate=True,
    *,
    local_extended=False,
    include_negative_tags=False,
):
    return (
        _positive_samples(pattern, n_pos, seed, validate, randomize=True),
        _negative_samples(
            pattern,
            n_neg,
            seed,
            validate,
            local_extended=local_extended,
            include_negative_tags=include_negative_tags,
        ),
    )


def _balanced_pos(pattern, n_pos, n_neg, seed, validate=True, **negative_options):
    """Experimental: branch-balanced positives, same negatives as full."""
    return (
        _positive_samples(pattern, n_pos, seed, validate, randomize=False, balanced=True),
        _negative_samples(pattern, n_neg, seed, validate, **negative_options),
    )


def _walk_pos(pattern, n_pos, n_neg, seed, validate=True, **negative_options):
    """Random-walk positives (ablation/random_walk_sampler.py), same negatives as full."""
    from .random_walk_sampler import random_walk_samples

    return (
        random_walk_samples(pattern, n_pos, seed),
        _negative_samples(pattern, n_neg, seed, validate, **negative_options),
    )


def _enum_pos(pattern, n_pos, n_neg, seed, validate=True, **negative_options):
    """Naive enumeration positives (ablation/enumeration_sampler.py): shortest first,
    every choice in order; same negatives as full."""
    from .enumeration_sampler import enumeration_samples

    return (
        enumeration_samples(pattern, n_pos),
        _negative_samples(pattern, n_neg, seed, validate, **negative_options),
    )


def _factorial_variant(positive_enabled, local_enabled, mutation_enabled):
    def generate(
        pattern,
        n_pos,
        n_neg,
        seed,
        validate=True,
        *,
        local_extended=False,
        include_negative_tags=False,
    ):
        positives = (
            _positive_samples(
                pattern,
                n_pos,
                seed,
                validate,
                randomize=not positive_enabled,
            )
            if n_pos > 0 else []
        )
        negatives = _negative_samples(
            pattern,
            n_neg,
            seed,
            validate,
            use_local=local_enabled,
            use_mutation=mutation_enabled,
            local_extended=local_extended,
            include_negative_tags=include_negative_tags,
        )
        return positives, negatives

    return generate


VARIANTS = {
    "full": ("Full", _full),
    "no_pos": ("No positive samples", _no_pos),
    "random_pos": ("Random positive samples", _random_pos),
    "balanced_pos": ("Branch-balanced positive samples (experimental)", _balanced_pos),
    "walk_pos": ("Random-walk positive samples", _walk_pos),
    "enum_pos": ("Naive enumeration positive samples", _enum_pos),
}
for _positive in (0, 1):
    for _local in (0, 1):
        for _mutation in (0, 1):
            _name = f"p{_positive}l{_local}m{_mutation}"
            VARIANTS[_name] = (
                _name,
                _factorial_variant(bool(_positive), bool(_local), bool(_mutation)),
            )
# Local-complement ablation: boundary positives, negatives without the local stage.
VARIANTS["no_local"] = ("No local complements", VARIANTS["p1l0m1"][1])


def _no_local_strings(pattern, n_pos, n_neg, seed, validate=True, *, include_negative_tags=False, **_):
    """No local-complement strings at all: the local stage is off and its exact strings
    are blocked in every mutation stage (which would otherwise regenerate all of them:
    six families, or optimized_family_fill if those families are removed)."""
    negatives = StagedNegativeSampler(
        pattern, validate=validate, use_local=False, exclude_local_strings=True
    ).generate(n=n_neg, seed=seed, use_fullmatch=True, return_tags=include_negative_tags)
    return _positive_samples(pattern, n_pos, seed, validate, randomize=False), negatives


VARIANTS["no_local_strings"] = ("No local-complement strings from any stage", _no_local_strings)


# Step-1-versus-step-2 study (no fill stage): step 1 = minimal_local_fault, step 2 =
# the violation families, step 3 = optimized_family_fill (always skipped here).
def _stage_variant(*skip_stages):
    def generate(pattern, n_pos, n_neg, seed, validate=True, *, include_negative_tags=False, **_):
        negatives = StagedNegativeSampler(
            pattern, validate=validate, skip_stages=("optimized_family_fill",) + skip_stages
        ).generate(n=n_neg, seed=seed, use_fullmatch=True, return_tags=include_negative_tags)
        return _positive_samples(pattern, n_pos, seed, validate, randomize=False), negatives

    return generate


VARIANTS["local_and_families"] = ("Steps 1 and 2, no fill", _stage_variant())
VARIANTS["families_only"] = ("Step 2 only, no fill", _stage_variant("minimal_local_fault"))
VARIANTS["local_only"] = ("Step 1 only, no fill", _stage_variant("family"))


def _local_fill(pattern, n_pos, n_neg, seed, validate=True, *, include_negative_tags=False, **_):
    """Budget-matched step 1: local faults, then the fallback fill (step 3), no
    mutation families. Compared with full (steps 1+2+3), both reach n_neg."""
    negatives = StagedNegativeSampler(pattern, validate=validate, skip_stages=("family",)).generate(
        n=n_neg, seed=seed, use_fullmatch=True, return_tags=include_negative_tags
    )
    return _positive_samples(pattern, n_pos, seed, validate, randomize=False), negatives


VARIANTS["local_fill"] = ("Step 1 + fill, no mutation families", _local_fill)


def _families_no_local(pattern, n_pos, n_neg, seed, validate=True, *, include_negative_tags=False, **_):
    """Step 2 only, minus step 1's strings: no local stage, no fill, and every string
    local_only would produce (minimal_local_faults, which does not depend on the
    seed) is blocked, so the mutation families contribute only their own strings."""
    negatives = StagedNegativeSampler(
        pattern,
        validate=validate,
        exclude_local_strings=True,
        skip_stages=("minimal_local_fault", "optimized_family_fill"),
    ).generate(n=n_neg, seed=seed, use_fullmatch=True, return_tags=include_negative_tags)
    return _positive_samples(pattern, n_pos, seed, validate, randomize=False), negatives


VARIANTS["families_no_local"] = ("Step 2 only, without step 1's strings", _families_no_local)
