import re

from ablation.enumeration_sampler import enumeration_samples


def test_shortest_first_in_character_order():
    samples = enumeration_samples(r"[A-Z]+\d+", 262)
    assert samples[:3] == ["A0", "A1", "A2"]
    assert samples[9:11] == ["A9", "B0"]
    assert samples[259:262] == ["Z9", "A00", "A01"]


def test_all_samples_match_and_are_unique():
    for pattern in (r"(?:cat|dog)s?", r"(?=.*\d)[a-z0-9]{3}", r"\d{2}:[a-z]+"):
        samples = enumeration_samples(pattern, 200)
        assert samples
        assert len(set(samples)) == len(samples)
        assert all(re.fullmatch(pattern, sample) for sample in samples)


def test_small_language_is_enumerated_completely():
    assert enumeration_samples(r"(?:cat|dog)s?", 100) == ["cat", "dog", "cats", "dogs"]


def test_backreference_stops_without_samples():
    assert enumeration_samples(r"(ab|cd)\1", 10) == []
