import re

from ablation.random_walk_sampler import random_walk_samples


def test_reproducible_for_a_seed():
    assert random_walk_samples(r"[a-z]{3}\d*", 50, seed=7) == random_walk_samples(
        r"[a-z]{3}\d*", 50, seed=7
    )


def test_returns_n_matching_samples_and_keeps_repeats():
    samples = random_walk_samples(r"(?:a|b)", 20, seed=0)
    assert len(samples) == 20
    assert set(samples) <= {"a", "b"}


def test_small_branches_are_reached():
    samples = random_walk_samples(r"(?:[a-z]{5}|[0-9])", 1000, seed=0)
    assert 400 < sum(s.isdigit() for s in samples) < 600


def test_unbounded_repeats_go_past_matchmakers_cap():
    star_lengths = {len(s) for s in random_walk_samples(r"a*", 500, seed=0)}
    assert max(star_lengths) > 3 and max(star_lengths) <= 20
    bounded_lengths = {len(s) for s in random_walk_samples(r"a{2,5}", 200, seed=0)}
    assert bounded_lengths == {2, 3, 4, 5}


def test_backrefs_and_lookarounds():
    assert set(random_walk_samples(r"(a|b)\1", 50, seed=0)) == {"aa", "bb"}
    pattern = r"(?=\d)[a-z0-9]{2}"
    samples = random_walk_samples(pattern, 50, seed=0)
    assert len(samples) == 50
    assert all(re.fullmatch(pattern, s) for s in samples)
