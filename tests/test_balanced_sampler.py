import re

from matchmaker.compact import CompactRegexGraphBuilder
from matchmaker.compact.balanced_sampler import BranchBalancedSampler
from matchmaker.compact.lookaround_sampler import LookaroundCompactGraphSampler


def _sample(sampler_type, pattern, n, seed=0):
    builder = CompactRegexGraphBuilder(pattern, validate=True)
    return sampler_type(builder, validate=True).generate_samples(n, seed=seed)


def test_small_branches_are_not_starved():
    pattern = r"(?:[a-z]{5}|[0-9])"
    original = _sample(LookaroundCompactGraphSampler, pattern, 40)
    balanced = _sample(BranchBalancedSampler, pattern, 40)
    assert sum(s.isdigit() for s in original) == 0
    # The simplified graph keeps 3 representative digits; all of them appear.
    assert sum(s.isdigit() for s in balanced) == 3
    assert all(re.fullmatch(pattern, s) for s in balanced)
    assert len(set(balanced)) == len(balanced) == 40


def test_deterministic_and_enumerates_small_languages():
    pattern = r"(?:ab|c[0-2])x?"
    assert _sample(BranchBalancedSampler, pattern, 3, seed=5) == _sample(
        BranchBalancedSampler, pattern, 3, seed=5
    )
    assert sorted(_sample(BranchBalancedSampler, pattern, 50)) == sorted(
        _sample(LookaroundCompactGraphSampler, pattern, 50)
    )
