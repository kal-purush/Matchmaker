"""Ablation-only regexes that follow what each validator actually accepts.

The main coverage benchmark generates samples from a practical regex for each target
(``benchmarks.python_regex_function_coverage_benchmark.build_target_specs``). Most
validators accept more than that regex: ``uuid.UUID`` ignores dashes, ``ipaddress``
accepts compressed IPv6, ``fromisoformat`` accepts any date/time separator, and so on.
Negatives generated from the benchmark regex are then often *accepted* by the
validator, so a variant without positive samples still reaches the validator's
success path, and the ablation cannot measure what positives contribute.

The regexes below are written from each validator's code so that strings outside the
regex are rejected by the validator. The main benchmark is unchanged; only the
ablation uses these. Targets missing from ``REGEXES`` already agree with their
benchmark regex (no benchmark negative is accepted) and keep it.

Some validators accept languages a regex cannot express exactly (lengths measured
after punycode encoding, WHATWG's removal of tabs anywhere in a URL, IPv4 numbers
in hosts). The regexes approximate those, so a small number of mismatches remain;
``python -m ablation.validator_regexes`` reports them.
"""

import calendar

WS = r"[ \t\n\r\f\v]"


def _alt(options):
    options = list(dict.fromkeys(options))
    return options[0] if len(options) == 1 else "(?:" + "|".join(options) + ")"


# --------------------------------------------------------------------------- UUID
# uuid.UUID(hex) (also Django UUIDField.to_python): removes every "urn:" and "uuid:",
# strips "{" and "}" from both ends, deletes every "-", then calls int(rest, 16) on
# exactly 32 characters. int() also allows surrounding whitespace and a leading "+",
# which count toward the 32.
def _uuid():
    hex_digit = "[0-9a-fA-F]-*"
    bodies = []
    for lead in range(4):
        for sign in (0, 1):
            for trail in range(4):
                used = lead + sign + trail
                if used > 3:
                    continue
                prefix = f"(?:{WS}-*){{{lead}}}" if lead else ""
                prefix += r"\+-*" if sign else ""
                suffix = f"(?:{WS}-*){{{trail}}}" if trail else ""
                bodies.append(rf"{prefix}(?:{hex_digit}){{{32 - used}}}{suffix}")
    return rf"(?:urn:|uuid:)*\{{*-*{_alt(bodies)}\}}*"


UUID = _uuid()

# ----------------------------------------------------------------------------- IP
# ipaddress.ip_address (also pydantic IPvAnyAddress): IPv4 as four decimal octets
# without leading zeros; IPv6 with optional "::" compression (standing for at least
# one group), an optional embedded IPv4 tail, and an optional "%scope".
OCTET = r"(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9][0-9]|[0-9])"
IPV4 = rf"{OCTET}(?:\.{OCTET}){{3}}"
_HEXTET = "[0-9A-Fa-f]{1,4}"


def _ipv6():
    alternatives = [rf"(?:{_HEXTET}:){{7}}{_HEXTET}", rf"(?:{_HEXTET}:){{6}}{IPV4}"]
    for ipv4_tail in (False, True):
        slots = 6 if ipv4_tail else 8
        for right in range(slots):
            max_left = slots - 1 - right
            if ipv4_tail:
                right_part = rf"(?:{_HEXTET}:){{{right}}}{IPV4}" if right else IPV4
            else:
                right_part = rf"{_HEXTET}(?::{_HEXTET}){{{right - 1}}}" if right else ""
            left_part = (
                rf"(?:{_HEXTET}(?::{_HEXTET}){{0,{max_left - 1}}})?" if max_left else ""
            )
            alternatives.append(rf"{left_part}::{right_part}")
    return _alt(alternatives)


IPV6 = _ipv6()
IP_STDLIB = rf"(?:{IPV4}|{IPV6}(?:%[^%/]+)?)"
# Django validate_ipv46_address: the same, but IPv6 strings longer than 39
# characters are rejected; scope IDs are left out to stay within that limit.
IP_DJANGO = rf"(?:{IPV4}|{IPV6})"


# ---------------------------------------------------------------------- datetimes
def _numbers(values, forms):
    """Alternation of every value written in each of the given forms."""
    return _alt(form(value) for value in values for form in forms)


def _date(year, month_forms, day_forms, sep="-", leap_year=None):
    """A calendar-valid date: day 29 of February only in leap years."""
    months_by_length = {31: [], 30: [], 28: []}
    for month in range(1, 13):
        length = 28 if month == 2 else calendar.monthrange(2001, month)[1]
        months_by_length[length].append(month)
    branches = [
        f"{year}{sep}{_numbers(months, month_forms)}{sep}"
        f"{_numbers(range(1, length + 1), day_forms)}"
        for length, months in months_by_length.items()
    ]
    if leap_year:
        branches.append(
            f"{leap_year}{sep}{_numbers([2], month_forms)}{sep}{_numbers([29], day_forms)}"
        )
    return _alt(branches)


def _two(value):
    return f"{value:02d}"


def _one(value):
    return str(value)


def _space_then_digit(value):
    return f" {value}" if value < 10 else _two(value)


def _ws_then_digit(value):
    return f"{WS}{value}" if value < 10 else _two(value)


def _digit_then_ws(value):
    return f"{value}{WS}" if value < 10 else _two(value)


YEAR = r"(?:[1-9][0-9]{3}|0[1-9][0-9]{2}|00[1-9][0-9]|000[1-9])"
LEAP_YEAR = (
    r"(?:[0-9]{2}(?:0[48]|[2468][048]|[13579][26])|(?:0[48]|[2468][048]|[13579][26])00)"
)
HOUR2 = r"(?:[01][0-9]|2[0-3])"
MINUTE2 = r"[0-5][0-9]"
FRACTION = r"[.,][0-9]+"
ANY_CHAR = r"[\s\S]"

# datetime.strptime(value, "%Y-%m-%dT%H:%M:%S"): _strptime's own field patterns
# (month and day may be one digit, day and hour may have a leading space), a
# case-insensitive "T", and a date that exists.
STRPTIME = (
    _date(YEAR, [_two, _one], [_two, _one, _space_then_digit], leap_year=LEAP_YEAR)
    + r"[Tt](?:2[0-3]|[01][0-9]|[0-9]| [0-9]):(?:[0-5][0-9]|[0-9]):(?:[0-5][0-9]|[0-9])"
)

# Django parse_datetime: datetime.fromisoformat(value), falling back to Django's
# datetime_re. fromisoformat accepts extended or basic dates and week dates, any
# single separator character, hours with optional minutes and seconds (basic or
# extended), fractions, and an optional time zone (optionally after a space).
_ISO_DATE = _alt([
    _date(YEAR, [_two], [_two], leap_year=LEAP_YEAR),
    _date(YEAR, [_two], [_two], sep="", leap_year=LEAP_YEAR),
    rf"{YEAR}-?W(?:0[1-9]|[1-4][0-9]|5[0-2])-?[1-7]",
])
_ISO_TIME = _alt([
    HOUR2,
    rf"{HOUR2}:?{MINUTE2}(?:{FRACTION})?",
    rf"{HOUR2}:{MINUTE2}:{MINUTE2}(?:{FRACTION})?",
    rf"{HOUR2}{MINUTE2}{MINUTE2}(?:{FRACTION})?",
])
_ISO_TZ = (
    rf"(?: ?(?:Z|[+-]{HOUR2}(?::?{MINUTE2}(?::?{MINUTE2}(?:\.[0-9]{{1,6}})?)?)?))?"
)
FROMISOFORMAT = rf"{_ISO_DATE}(?:{ANY_CHAR}{_ISO_TIME}{_ISO_TZ})?"
# Django's datetime_re: one- or two-digit fields, "T" or space, optional seconds and
# fraction, optional whitespace and time zone, and an optional final newline ($).
_DJANGO_HOUR = _numbers(range(24), [_two, _one])
_DJANGO_MINUTE = _numbers(range(60), [_two, _one])
DJANGO_DATETIME_RE = (
    _date(YEAR, [_two, _one], [_two, _one], leap_year=LEAP_YEAR)
    + rf"[T ]{_DJANGO_HOUR}:{_DJANGO_MINUTE}"
    + rf"(?::{_DJANGO_MINUTE}(?:[.,][0-9]{{1,12}})?)?"
    + rf"{WS}*(?:Z|[+-]{HOUR2}(?::?{MINUTE2})?)?\n?"
)
DJANGO_DATETIME = _alt([FROMISOFORMAT, DJANGO_DATETIME_RE])

# dateutil.parser.isoparse: calendar, basic, month-only, year-only, week and ordinal
# dates; any single separator character; and each fixed-width number is read with
# int(), so a whitespace character can replace a leading or trailing digit.
_DU_YEAR = _alt([YEAR, rf"{WS}[1-9][0-9]{{2}}", rf"[1-9][0-9]{{2}}{WS}"])
_DU_FIELD = [_two, _ws_then_digit, _digit_then_ws]
_DU_DATE = _alt([
    _date(_DU_YEAR, _DU_FIELD, _DU_FIELD, leap_year=LEAP_YEAR),
    _date(YEAR, [_two], [_two], sep="", leap_year=LEAP_YEAR),
    rf"{YEAR}-(?:0[1-9]|1[0-2])",
    YEAR,
    rf"{YEAR}-?W(?:0[1-9]|[1-4][0-9]|5[0-3])(?:-?[1-7])?",
    rf"{YEAR}-?(?:00[1-9]|0[1-9][0-9]|[12][0-9]{{2}}|3[0-5][0-9]|36[0-5])",
])
_DU_HOUR = _numbers(range(24), _DU_FIELD)
_DU_MINUTE = _numbers(range(60), _DU_FIELD)
_DU_TIME = _alt([
    _DU_HOUR,
    rf"{_DU_HOUR}:?{_DU_MINUTE}",
    rf"{_DU_HOUR}:{_DU_MINUTE}:{_DU_MINUTE}(?:{FRACTION})?",
    rf"{_DU_HOUR}{_DU_MINUTE}{_DU_MINUTE}(?:{FRACTION})?",
    r"24(?::?00(?::?00(?:[.,]0+)?)?)?",
])
_DU_TZ = rf"(?:[Zz]|[+-]{HOUR2}(?::?{MINUTE2})?)?"
DATEUTIL = rf"{_DU_DATE}(?:{ANY_CHAR}{_DU_TIME}{_DU_TZ})?"

# ------------------------------------------------------------- URLs and domains
PORT = (
    r"0*(?:6553[0-5]|655[0-2][0-9]|65[0-4][0-9]{2}|6[0-4][0-9]{3}|[1-5][0-9]{4}"
    r"|[0-9]{1,4})"
)

# Benchmark urllib check: urlsplit(value) with scheme http, https, ftp or ftps (any
# case), a non-empty hostname, no whitespace, and a valid port. urlsplit splits the
# userinfo at the last "@" and the port at the first ":" after it.
_URLLIB_USER = r"[^\s/?#\[\]]*@"
_URLLIB_HOST = r"[^\s/?#@:\[\]]+"
URLLIB = (
    rf"(?i:https?|ftps?)://(?:{_URLLIB_USER})?{_URLLIB_HOST}"
    rf"(?::(?:{PORT})?)?(?:[/?#][^\s]*)?"
)

# Letters from several scripts (Latin, Greek, Cyrillic, kana, CJK); IDNA maps them.
LETTERS = (
    "à-öø-ÿĀ-ɏΑ-Ωα-ω"
    "А-яぁ-ゖ一-鿿"
)

# WHATWG URL parsing (pydantic HttpUrl, scheme http or https). The parser trims
# leading and trailing C0 controls and spaces, deletes tab, LF and CR anywhere, and
# accepts any number of "/" or "\" after the scheme. Everything up to the last "@" is
# userinfo. Host labels may use any character except forbidden domain code points;
# a host whose last label is a number is parsed as an IPv4 address with 1-4 decimal,
# octal or hex parts.
_W_TRIM = r"[\x00-\x20]*"
_W_CHAR = rf"""[!"$&'()*+,0-9;=A-Z_`a-z{{}}~\t\n\r{LETTERS}-]"""
_W_NONDIGIT = rf"""[!"$&'()*+,;=A-Z_`a-z{{}}~{LETTERS}-]"""
_W_END = r"[.\t\n\r]*"
_W_NAME = rf"(?:{_W_CHAR}*\.)*{_W_CHAR}*{_W_NONDIGIT}{_W_CHAR}*{_W_END}"
_W_BYTE = (
    r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9][0-9]?|0[0-3]?[0-7]{0,2}"
    r"|0[xX][0-9a-fA-F]{0,2})"
)
_W_IPV4 = _alt([
    rf"(?:{_W_BYTE}\.){{3}}{_W_BYTE}",
    rf"(?:{_W_BYTE}\.){{2}}{_alt([_W_BYTE, r'[1-9][0-9]{0,3}', r'[1-5][0-9]{4}'])}",
    rf"{_W_BYTE}\.{_alt([_W_BYTE, r'[1-9][0-9]{0,6}'])}",
    _alt([_W_BYTE, r"[1-9][0-9]{0,8}"]),
]) + r"\.?"
_W_HOST = _alt([_W_NAME, _W_IPV4, rf"[\t\n\r]*\.{_W_END}"])
_W_USER = r"(?:[^/\\?#]*@)?"
_W_REST = rf"(?::(?:{PORT})?)?(?:[/?#][\s\S]*)?{_W_TRIM}"
_W_SLASHES = r"[/\\\t\n\r]*"
URL_PYDANTIC = rf"{_W_TRIM}(?i:https?):{_W_SLASHES}{_W_USER}{_W_HOST}{_W_REST}"
# Benchmark pydantic domain check: HttpUrl on "http://" + value.
DOMAIN_PYDANTIC = rf"{_W_SLASHES}{_W_USER}{_W_HOST}{_W_REST}"

# Benchmark IDNA check: at least two labels, no trailing dot, and labels of 1-63
# characters (any character except "."; IDNA2003 without STD3 rules) that do not
# start or end with "-".
_IDNA_LABEL = r"[^.\-](?:[^.]{0,61}[^.\-])?"
DOMAIN_IDNA = rf"{_IDNA_LABEL}(?:\.{_IDNA_LABEL})+"

# -------------------------------------------------------------------------- email
# Benchmark parseaddr check: email.utils.parseaddr(value, strict=True) must return the
# input unchanged, with one "@" and a "." in the domain. Atoms are runs of anything
# except RFC 822 specials, space, tab, CR and LF; the local part may also contain
# quoted strings, and dots may repeat anywhere.
_EMAIL_ATOM = r"""[^ \t\r\n()<>@,:;.\"\[\]]"""
_EMAIL_TOKEN = rf'(?:{_EMAIL_ATOM}+|"[^"\\\r\n]*")'
_EMAIL_LOCAL = rf"\.*{_EMAIL_TOKEN}(?:(?:\.+| +){_EMAIL_TOKEN})*\.*"
_EMAIL_DOMAIN = rf"(?:{_EMAIL_ATOM}|\.)*\.(?:{_EMAIL_ATOM}|\.)*"
PARSEADDR = rf"{_EMAIL_LOCAL}@{_EMAIL_DOMAIN}"

# email_validator.validate_email (SMTPUTF8 allowed): letters from several scripts in
# the local part and domain. Domain labels may not start or end with "-" or have
# "--" at positions 3-4, and the ASCII form of the domain must end in a letter.
# Labels are limited to 63 characters after punycode encoding, which a regex cannot
# measure, so internationalized labels are capped at 10 characters.
_EV_CHARS = f"a-z0-9{LETTERS}"


def _email_label(chars, max_length):
    char, char_or_hyphen = f"[{chars}]", f"[{chars}-]"
    return _alt([
        rf"{char}(?:{char_or_hyphen}?{char})?",
        rf"{char}{char_or_hyphen}{char_or_hyphen}{char}",
        rf"{char}{char_or_hyphen}(?:{char}{char_or_hyphen}|-{char})"
        rf"{char_or_hyphen}{{0,{max_length - 5}}}{char}",
    ])


_EV_LABEL = _alt([_email_label("a-z0-9", 63), _email_label(_EV_CHARS, 10)])
_EV_TLD = _alt([
    r"(?:[a-z0-9][a-z0-9-]{0,61})?[a-z]",
    rf"(?:[{_EV_CHARS}][{_EV_CHARS}-]{{0,4}})?[{LETTERS}]"
    rf"(?:[{_EV_CHARS}-]{{0,3}}[{_EV_CHARS}])?",
])
EMAIL_VALIDATOR = (
    rf"(?i)[{_EV_CHARS}!#$%&'*+/=?^_`{{|}}~-]+"
    rf"(?:\.[{_EV_CHARS}!#$%&'*+/=?^_`{{|}}~-]+)*@"
    rf"(?:{_EV_LABEL}\.)+{_EV_TLD}"
)

REGEXES = {
    "datetime_dateutil": DATEUTIL,
    "datetime_django_utils": DJANGO_DATETIME,
    "datetime_stdlib_strptime": STRPTIME,
    "domain_pydantic": DOMAIN_PYDANTIC,
    "domain_stdlib_idna": DOMAIN_IDNA,
    "email_stdlib_parseaddr": PARSEADDR,
    "email_validator_package": EMAIL_VALIDATOR,
    "ip_django_core": IP_DJANGO,
    "ip_pydantic": IP_STDLIB,
    "ip_stdlib_ipaddress": IP_STDLIB,
    "url_pydantic": URL_PYDANTIC,
    "url_stdlib_urllib": URLLIB,
    "uuid_django_model": UUID,
    "uuid_stdlib": UUID,
}

REGEX_SETS = ("validator", "benchmark")


def regex_for(spec, regex_set="validator"):
    """The generation regex for a benchmark target spec under a regex set."""

    if regex_set not in REGEX_SETS:
        raise ValueError(f"Unknown regex set: {regex_set}")
    if regex_set == "validator":
        return REGEXES.get(spec.name, spec.regex)
    return spec.regex


def _accepts(validator_fn, sample):
    try:
        return bool(validator_fn(sample))
    except Exception:
        return False


def mismatch_report(n=1000, seed=0):
    """For both regex sets and every target, count positives the validator rejects
    and negatives it accepts."""

    from benchmarks.python_regex_function_coverage_benchmark import build_target_specs

    from .variants import _negative_samples, _positive_samples

    rows = []
    for spec in build_target_specs():
        row = {"target": spec.name}
        for regex_set in REGEX_SETS:
            regex = regex_for(spec, regex_set)
            positives = _positive_samples(regex, n, seed, True, randomize=False)
            negatives = _negative_samples(regex, n, seed, True)
            row[regex_set] = {
                "positives": len(positives),
                "positives_rejected": sum(
                    not _accepts(spec.validator_fn, sample) for sample in positives
                ),
                "negatives": len(negatives),
                "negatives_accepted": sum(
                    _accepts(spec.validator_fn, sample) for sample in negatives
                ),
            }
        rows.append(row)
    return rows


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Report how well each regex set agrees with its validator."
    )
    parser.add_argument("--n", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    print(
        f"{'target':26} {'benchmark: pos rejected / neg accepted':>40}"
        f" {'validator: pos rejected / neg accepted':>40}"
    )
    for row in mismatch_report(args.n, args.seed):
        cells = []
        for regex_set in ("benchmark", "validator"):
            counts = row[regex_set]
            cells.append(
                f"{counts['positives_rejected']:>5}/{counts['positives']:<5}"
                f" {counts['negatives_accepted']:>5}/{counts['negatives']:<5}"
            )
        print(f"{row['target']:26} {cells[0]:>40} {cells[1]:>40}")


if __name__ == "__main__":
    main()
