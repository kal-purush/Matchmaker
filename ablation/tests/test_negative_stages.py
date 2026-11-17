import pytest

from matchmaker.compact.negative_sampler import (
    generate_negative_samples,
)

from ablation.negative_stages import StagedNegativeSampler
from ablation.tests.test_random_positive_sampler import (
    DATETIME,
    DOMAIN,
    EMAIL,
    IP,
    URL,
    UUID,
)


@pytest.mark.parametrize(
    "pattern",
    [EMAIL, URL, IP, DATETIME, UUID, DOMAIN],
    ids=["email", "url", "ip", "datetime", "uuid", "domain"],
)
@pytest.mark.parametrize("seed", range(5))
def test_staged_sampler_matches_baseline_when_all_stages_enabled(pattern, seed):
    baseline = generate_negative_samples(
        pattern, n=1000, seed=seed, use_fullmatch=True, validate=True
    )
    staged = StagedNegativeSampler(
        pattern, validate=True, use_local=True, use_mutation=True
    ).generate(n=1000, seed=seed, use_fullmatch=True)
    assert staged == baseline


def test_mutation_disabled_emits_only_local_fault_tags():
    tagged = StagedNegativeSampler(
        r"ab[0-9]", use_local=True, use_mutation=False
    ).generate(n=100, seed=0, return_tags=True)

    assert tagged
    assert {tag for _sample, tag in tagged} == {"minimal_local_fault"}


def test_extended_local_tags_only_local_faults_when_mutation_disabled():
    tagged = StagedNegativeSampler(
        r"ab[0-9]",
        use_local=True,
        use_mutation=False,
        local_extended=True,
    ).generate(n=100, seed=0, return_tags=True)

    assert tagged
    assert {tag for _sample, tag in tagged} <= {
        "minimal_local_fault",
        "local_extended",
    }
    assert "local_extended" in {tag for _sample, tag in tagged}


def test_local_disabled_never_emits_minimal_local_faults():
    tagged = StagedNegativeSampler(
        r"ab[0-9]", use_local=False, use_mutation=True
    ).generate(n=100, seed=0, return_tags=True)

    assert tagged
    assert "minimal_local_fault" not in {tag for _sample, tag in tagged}
    assert "local_extended" not in {tag for _sample, tag in tagged}


def test_local_only_can_fill_from_extended_replacements():
    tagged = StagedNegativeSampler(
        r"ab[0-9]",
        use_local=True,
        use_mutation=False,
        local_extended=True,
    ).generate(n=100, seed=0, return_tags=True)
    assert len(tagged) > len(
        StagedNegativeSampler(
            r"ab[0-9]", use_local=True, use_mutation=False
        ).generate(n=100, seed=0)
    )
