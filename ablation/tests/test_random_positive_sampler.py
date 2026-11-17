import re
import string

import pytest

from matchmaker.compact import CompactRegexGraphBuilder
from matchmaker.compact.lookaround_sampler import (
    LookaroundCompactGraphSampler,
)

from ablation.random_positive_sampler import (
    RandomPositiveSampler,
)


DOMAIN = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
EMAIL = (
    r"(?i)[a-z0-9!#$%&'*+/=?^_`{|}~-]+"
    r"(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*@"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:[a-z-]{2,63}|xn--[a-z0-9]{1,59})"
)
URL = (
    r"(?i)(?:http|https|ftp|ftps)://"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:[a-z-]{2,63}|xn--[a-z0-9]{1,59})"
    r"(?::[0-9]{1,5})?"
    r"(?:/[a-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?"
)
IP = (
    r"(?:0|25[0-5]|2[0-4][0-9]|1[0-9]?[0-9]?|[1-9][0-9]?)"
    r"(?:\.(?:0|25[0-5]|2[0-4][0-9]|1[0-9]?[0-9]?|[1-9][0-9]?)){3}"
    r"|(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
)
DATETIME = (
    r"(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])T"
    r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"
)
UUID = (
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


@pytest.mark.parametrize(
    "pattern",
    [EMAIL, URL, IP, DATETIME, UUID, DOMAIN],
    ids=["email", "url", "ip", "datetime", "uuid", "domain"],
)
def test_random_positive_sampler_is_valid_unique_and_deterministic(pattern):
    builder = CompactRegexGraphBuilder(pattern, validate=True)
    sampler = RandomPositiveSampler(builder, validate=True)
    first = sampler.generate_samples(1000, seed=42)
    second = RandomPositiveSampler(
        CompactRegexGraphBuilder(pattern, validate=True), validate=True
    ).generate_samples(1000, seed=42)

    assert first == second
    assert len(set(first)) >= 990
    assert all(re.fullmatch(pattern, sample) for sample in first)


def test_random_uuid_uses_more_hex_characters_than_boundary_sampler():
    random_samples = RandomPositiveSampler(
        CompactRegexGraphBuilder(UUID, validate=True), validate=True
    ).generate_samples(1000, seed=42)
    boundary_samples = LookaroundCompactGraphSampler(
        CompactRegexGraphBuilder(UUID, validate=True), validate=True
    ).generate_samples(1000, seed=42)
    hex_chars = set(string.hexdigits)

    assert len(set("".join(random_samples)) & hex_chars) > len(
        set("".join(boundary_samples)) & hex_chars
    )


@pytest.mark.parametrize(
    "pattern",
    [
        r"([A-Z]{1,10})\d{5}\1",
        r"(?=.*\d)[a-z0-9]{6,12}",
        r"(<)?\w+(?(1)>)",
    ],
)
def test_random_positive_sampler_handles_special_graph_nodes(pattern):
    samples = RandomPositiveSampler(
        CompactRegexGraphBuilder(pattern, validate=True), validate=True
    ).generate_samples(20, seed=42)
    assert samples
    assert all(re.fullmatch(pattern, sample) for sample in samples)


def test_random_positive_sampler_rejects_simplified_builder():
    builder = CompactRegexGraphBuilder(UUID, validate=True).build_simplified_graph()
    with pytest.raises(ValueError, match="non-simplified"):
        RandomPositiveSampler(builder)
