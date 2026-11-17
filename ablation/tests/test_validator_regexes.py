import re
from types import SimpleNamespace

import pytest

from ablation import validator_regexes
from ablation.validator_regexes import REGEXES, regex_for


@pytest.mark.parametrize("target", sorted(REGEXES))
def test_every_validator_regex_compiles(target):
    re.compile(REGEXES[target])


# Strings each validator accepts although the main benchmark regex rejects them
# (found among the benchmark's own negative samples). The validator regex must
# accept them, so they are no longer generated as negatives.
VALIDATOR_ACCEPTS = {
    "uuid_stdlib": [
        "000000000000-0000-0000-0000-00000000",
        " 0000000-0000-0000-0000-000000000000",
        "00000000--0000-0000-0000-000000000000",
        "00000000-0000-0000-0000-00000000000 ",
        "urn:uuid:{00000000-0000-0000-0000-000000000000}",
    ],
    "ip_stdlib_ipaddress": ["0::0:0:0:0:0:0", "0:0:0:0:0:0::0", "::1%eth0", "::ffff:1.2.3.4"],
    "ip_django_core": ["0::0:0:0:0:0:0", "::"],
    "datetime_stdlib_strptime": [
        "1900-01-01t00:00:00",
        "1900-01-01T00:00:0",
        "1900-01-1T00:00:00",
        "1900-01-01T 0:00:00",
    ],
    "datetime_django_utils": [
        "1900-01-01b00:00:00",
        "1900-01-01T00:00:00 ",
        "1900-01-01T00:00:00\n",
        "1900-01-01T00-00:00",
        "1900-01-01T00:00.00",
    ],
    "datetime_dateutil": ["1900-01-01t00:00:00", "190 -01-01T00:00:00", "1900-01-01T0 :00:00"],
    "domain_stdlib_idna": ["!-0.0-0", " 0-0.0-0", "0- .0-0"],
    "domain_pydantic": ["!-0.0-0", "0-0", "0-0/0-0", "01"],
    "url_pydantic": ["http://!-0.--:0/!", " http://0-0.--:0/!"],
    "url_stdlib_urllib": ["http://!-0.--:0/!", "http://0-0.--;0/!", "https://!@!@!:065530#!"],
    "email_stdlib_parseaddr": ["!.!@!-0.--", ".!@0-0.--", "! !@0-0.--"],
    "email_validator_package": ["!.!@0-0.één--0", "a@b.中", "Ω@b.com"],
}


@pytest.mark.parametrize(
    ("target", "sample"),
    [(target, sample) for target, samples in VALIDATOR_ACCEPTS.items() for sample in samples],
)
def test_validator_regex_accepts_inputs_the_validator_accepts(target, sample):
    assert re.fullmatch(REGEXES[target], sample)


# Canonical inputs that the benchmark regex already generates stay positives.
CANONICAL = {
    "uuid_stdlib": "123e4567-e89b-12d3-a456-426614174000",
    "ip_stdlib_ipaddress": "192.168.0.1",
    "ip_django_core": "2001:db8:0:0:0:0:0:1",
    "datetime_stdlib_strptime": "2024-02-29T23:59:59",
    "datetime_django_utils": "2024-02-29T23:59:59",
    "datetime_dateutil": "2024-02-29T23:59:59",
    "domain_stdlib_idna": "example.com",
    "domain_pydantic": "example.com",
    "url_pydantic": "https://example.com:8080/path",
    "url_stdlib_urllib": "ftp://example.com/file",
    "email_stdlib_parseaddr": "user.name@example.com",
    "email_validator_package": "user.name@example.com",
}


@pytest.mark.parametrize(("target", "sample"), sorted(CANONICAL.items()))
def test_validator_regex_keeps_canonical_inputs(target, sample):
    assert re.fullmatch(REGEXES[target], sample)


@pytest.mark.parametrize(
    ("target", "sample"),
    [
        ("datetime_stdlib_strptime", "2023-02-29T00:00:00"),
        ("ip_stdlib_ipaddress", "01.2.3.4"),
        ("uuid_stdlib", "0000000-0000-0000-0000-000000000000"),
        ("url_stdlib_urllib", "http://host:65536/"),
    ],
)
def test_validator_regex_rejects_inputs_the_validator_rejects(target, sample):
    assert not re.fullmatch(REGEXES[target], sample)


def test_regex_for_falls_back_to_the_benchmark_regex():
    listed = SimpleNamespace(name="uuid_stdlib", regex="benchmark-uuid")
    unlisted = SimpleNamespace(name="uuid_pydantic", regex="benchmark-pydantic")
    assert regex_for(listed, "validator") == validator_regexes.UUID
    assert regex_for(listed, "benchmark") == "benchmark-uuid"
    assert regex_for(unlisted, "validator") == "benchmark-pydantic"
    with pytest.raises(ValueError):
        regex_for(listed, "other")
