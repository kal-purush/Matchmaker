import functools
import os
import re
import string
import sys
import unicodedata
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

try:
    import regex

    REGEX_AVAILABLE = True
except ImportError:
    regex = None
    REGEX_AVAILABLE = False


MAX_RECURSION_DEPTH = 1
DEFAULT_REGEX_TIMEOUT_SECONDS = float(os.environ.get("REGEX_GRAPH_TIMEOUT_SECONDS", "10"))

POSIX_CLASS_BODIES = {
    "alpha": "a-zA-Z",
    "digit": "0-9",
    "alnum": "a-zA-Z0-9",
    "upper": "A-Z",
    "lower": "a-z",
    "space": "".join(re.escape(ch) for ch in string.whitespace),
    "punct": "".join(re.escape(ch) for ch in string.punctuation),
    "cntrl": r"\x00-\x1f\x7f",
    "print": r"\x20-\x7e",
    "graph": r"\x21-\x7e",
    "blank": r" \t",
    "xdigit": "0-9A-Fa-f",
}

UNICODE_CATEGORY_SCAN_LIMIT = 0xFFFF  # Basic Multilingual Plane, matches prior approximation's rough scope

UNICODE_PROPERTY_CATEGORY_SPECS = {
    "l": "L",
    "ll": "Ll",
    "lu": "Lu",
    "n": "N",
    "nd": "Nd",
}


@functools.lru_cache(maxsize=None)
def _unicode_category_chars(category_spec):
    exact = len(category_spec) == 2
    codepoints = [
        cp
        for cp in range(UNICODE_CATEGORY_SCAN_LIMIT + 1)
        if (unicodedata.category(chr(cp)) == category_spec if exact else unicodedata.category(chr(cp)).startswith(category_spec))
    ]

    parts = []
    start = prev = None
    for cp in codepoints:
        if start is None:
            start = prev = cp
        elif cp == prev + 1:
            prev = cp
        else:
            parts.append(_format_codepoint_range(start, prev))
            start = prev = cp
    if start is not None:
        parts.append(_format_codepoint_range(start, prev))
    return "".join(parts)


def _format_codepoint_range(start, end):
    if start == end:
        return f"\\u{start:04x}"
    if end == start + 1:
        return f"\\u{start:04x}\\u{end:04x}"
    return f"\\u{start:04x}-\\u{end:04x}"


@functools.lru_cache(maxsize=None)
def _unicode_category_char_set(category_spec):
    exact = len(category_spec) == 2
    return frozenset(
        chr(cp)
        for cp in range(UNICODE_CATEGORY_SCAN_LIMIT + 1)
        if (unicodedata.category(chr(cp)) == category_spec if exact else unicodedata.category(chr(cp)).startswith(category_spec))
    )


def _escape_class_codepoint(codepoint):
    if codepoint <= 0xFFFF:
        return f"\\u{codepoint:04x}"
    return f"\\U{codepoint:08x}"


def _chars_to_class_body(chars):
    codepoints = sorted(ord(ch) for ch in set(chars))
    parts = []
    start = prev = None
    for cp in codepoints:
        if start is None:
            start = prev = cp
        elif cp == prev + 1:
            prev = cp
        else:
            if start == prev:
                parts.append(_escape_class_codepoint(start))
            else:
                parts.append(f"{_escape_class_codepoint(start)}-{_escape_class_codepoint(prev)}")
            start = prev = cp
    if start is not None:
        if start == prev:
            parts.append(_escape_class_codepoint(start))
        else:
            parts.append(f"{_escape_class_codepoint(start)}-{_escape_class_codepoint(prev)}")
    return "".join(parts)


class RegexParseError(ValueError):
    pass


class RegexParser:
    def __init__(self, regex_pattern, flags=0, max_recursion_depth=MAX_RECURSION_DEPTH):
        self.regex = regex_pattern
        self.flags = flags
        self.max_recursion_depth = max_recursion_depth
        self.lookarounds = []
        self.using_regex_module = False
        self.expanded_regex = regex_pattern
        self.parsed_tree = None
        self.effective_flags = flags

    def parse(self):
        if REGEX_AVAILABLE:
            try:
                return self._parse_with_regex_module()
            except Exception:
                self.using_regex_module = False

        return self._parse_with_re_module()

    def _parse_with_regex_module(self):
        import sre_parse

        regex.compile(self.regex, self.flags)
        self.using_regex_module = True
        expanded = self._normalize_for_sre_parse(self.regex)
        parsed = sre_parse.parse(expanded, self.flags)
        self.expanded_regex = expanded
        self.effective_flags = getattr(getattr(parsed, "state", None), "flags", self.flags)
        self.parsed_tree = parsed.data if hasattr(parsed, "data") else parsed
        return self.parsed_tree

    def _parse_with_re_module(self):
        import sre_parse

        old_limit = sys.getrecursionlimit()
        sys.setrecursionlimit(max(old_limit, 10_000))
        try:
            expanded = self._normalize_for_sre_parse(self.regex)
            parsed = sre_parse.parse(expanded, self.flags)
            self.expanded_regex = expanded
            self.effective_flags = getattr(getattr(parsed, "state", None), "flags", self.flags)
            self.parsed_tree = parsed.data if hasattr(parsed, "data") else parsed
            return self.parsed_tree
        except Exception as exc:
            raise RegexParseError(f"Parse Error: {exc} {self.regex}") from exc
        finally:
            sys.setrecursionlimit(old_limit)

    def _normalize_for_sre_parse(self, pattern):
        expanded = self._expand_posix_classes(pattern)
        expanded = self._expand_unicode_properties(expanded)
        expanded, _ = self._detect_and_expand_lookarounds(expanded)
        expanded, _ = self._detect_and_expand_conditionals(expanded)
        return self._expand_recursion(expanded, self.max_recursion_depth)

    def _expand_posix_classes(self, pattern):
        if "[:" not in pattern:
            return pattern

        result = []
        i = 0
        n = len(pattern)
        in_class = False
        while i < n:
            ch = pattern[i]

            if ch == "\\" and i + 1 < n:
                result.append(pattern[i:i + 2])
                i += 2
                continue

            if not in_class:
                if ch == "[":
                    in_class = True
                result.append(ch)
                i += 1
                continue

            if ch == "[" and pattern[i + 1:i + 2] == ":":
                end = pattern.find(":]", i + 2)
                if end != -1:
                    name = pattern[i + 2:end]
                    body = POSIX_CLASS_BODIES.get(name)
                    if body is not None:
                        result.append(body)
                        i = end + 2
                        continue

            if ch == "]":
                in_class = False
            result.append(ch)
            i += 1

        return "".join(result)

    def _expand_recursion(self, pattern, max_depth):
        if "(?R)" not in pattern and "(?0)" not in pattern:
            return pattern

        return pattern.replace("(?R)", "(?!)").replace("(?0)", "(?!)")

    def _expand_unicode_properties(self, pattern):
        category_bodies = {
            "n": r"0-9",
            "nd": r"0-9",
            "xdigit": r"0-9A-Fa-f",
            "p": r"!-/:-@\[-`{-~",
            "s": r"$+<->\^`|~",
            "z": r" \t\n\r",
            "zs": r" ",
        }
        script_bodies = {
            "greek": r"\u0370-\u03FF\u1F00-\u1FFF",
            "cyrillic": r"\u0400-\u052F",
            "latin": r"A-Za-z\u00C0-\u024F\u1E00-\u1EFF",
            "hiragana": r"\u3040-\u309F",
            "katakana": r"\u30A0-\u30FF",
            "han": r"\u3400-\u4DBF\u4E00-\u9FFF",
        }

        def normalize_property_name(name):
            return "".join(ch for ch in name.lower() if ch not in " _-\t\r\n")

        def resolve_property_body(spec):
            normalized = normalize_property_name(spec)
            if normalized.startswith("script="):
                normalized = normalized.split("=", 1)[1]
            elif normalized.startswith("sc="):
                normalized = normalized.split("=", 1)[1]
            elif normalized.startswith("block="):
                normalized = normalized.split("=", 1)[1]
            elif normalized.startswith("blk="):
                normalized = normalized.split("=", 1)[1]
            elif normalized.startswith("is"):
                normalized = normalized[2:]
            elif normalized.startswith("in"):
                normalized = normalized[2:]

            if normalized in UNICODE_PROPERTY_CATEGORY_SPECS:
                return _unicode_category_chars(UNICODE_PROPERTY_CATEGORY_SPECS[normalized])
            if normalized in category_bodies:
                return category_bodies[normalized]
            if normalized in script_bodies:
                return script_bodies[normalized]
            return None

        bmp_universe = {chr(cp) for cp in range(UNICODE_CATEGORY_SCAN_LIMIT + 1)}

        def resolve_property_chars(spec):
            normalized = normalize_property_name(spec)
            if normalized.startswith("script="):
                normalized = normalized.split("=", 1)[1]
            elif normalized.startswith("sc="):
                normalized = normalized.split("=", 1)[1]
            elif normalized.startswith("block="):
                normalized = normalized.split("=", 1)[1]
            elif normalized.startswith("blk="):
                normalized = normalized.split("=", 1)[1]
            elif normalized.startswith("is"):
                normalized = normalized[2:]
            elif normalized.startswith("in"):
                normalized = normalized[2:]

            if normalized in UNICODE_PROPERTY_CATEGORY_SPECS:
                return _unicode_category_char_set(UNICODE_PROPERTY_CATEGORY_SPECS[normalized])
            body = resolve_property_body(spec)
            if body is None:
                return None
            return parse_class_body_chars(body)

        def parse_property_escape(text, index):
            if index + 2 > len(text) or text[index] != "\\" or text[index + 1:index + 2] not in {"p", "P"}:
                return None
            prop_type = text[index + 1]
            if index + 2 < len(text) and text[index + 2] == "{":
                end = text.find("}", index + 3)
                if end == -1:
                    return None
                spec = text[index + 3:end]
                return prop_type, spec, end + 1

            cursor = index + 2
            if cursor >= len(text) or not text[cursor].isalpha():
                return None
            cursor += 1
            while cursor < len(text) and text[cursor].isalnum():
                cursor += 1
            return prop_type, text[index + 2:cursor], cursor

        def parse_class_atom(text, index):
            prop = parse_property_escape(text, index)
            if prop is not None:
                prop_type, spec, end = prop
                chars = resolve_property_chars(spec)
                if chars is None:
                    raise RegexParseError(f"Unsupported Unicode property \\{prop_type}{spec}")
                if prop_type == "P":
                    chars = bmp_universe - chars
                return chars, end

            if text[index:index + 2] == r"\d":
                return set(string.digits), index + 2
            if text[index:index + 2] == r"\w":
                return set(string.ascii_letters + string.digits + "_"), index + 2
            if text[index:index + 2] == r"\s":
                return set(string.whitespace), index + 2
            if text[index] == "\\" and index + 1 < len(text):
                return {text[index + 1]}, index + 2
            return {text[index]}, index + 1

        def parse_class_body_chars(body):
            chars = set()
            index = 0
            while index < len(body):
                atom_chars, next_index = parse_class_atom(body, index)
                if (
                    len(atom_chars) == 1
                    and next_index < len(body) - 1
                    and body[next_index] == "-"
                ):
                    right_chars, after_right = parse_class_atom(body, next_index + 1)
                    if len(right_chars) == 1:
                        start = ord(next(iter(atom_chars)))
                        end = ord(next(iter(right_chars)))
                        if start <= end:
                            chars.update(chr(cp) for cp in range(start, end + 1))
                            index = after_right
                            continue
                chars.update(atom_chars)
                index = next_index
            return chars

        def find_class_end(text, start):
            escaped = False
            index = start + 1
            while index < len(text):
                char = text[index]
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif text[index:index + 2] == "[^" and "&&" in text[start:index]:
                    nested_end = find_class_end(text, index)
                    if nested_end == -1:
                        return -1
                    index = nested_end
                elif char == "]":
                    return index
                index += 1
            return -1

        def split_supported_intersection_body(body):
            escaped = False
            index = 0
            while index < len(body):
                char = body[index]
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif body[index:index + 4] == "&&[^" and body.endswith("]"):
                    return body[:index], body[index + 4:-1]
                index += 1
            return None

        def normalize_class_intersections(text):
            if "&&" not in text:
                return text

            result = []
            index = 0
            while index < len(text):
                char = text[index]
                if char == "\\" and index + 1 < len(text):
                    result.append(text[index:index + 2])
                    index += 2
                    continue
                if char != "[":
                    result.append(char)
                    index += 1
                    continue

                end = find_class_end(text, index)
                if end == -1:
                    result.append(char)
                    index += 1
                    continue

                body = text[index + 1:end]
                if "&&" not in body:
                    result.append(text[index:end + 1])
                    index = end + 1
                    continue

                parts = split_supported_intersection_body(body)
                if parts is None:
                    raise RegexParseError(f"Unsupported character-class set operation in {self.regex}")

                left_body, right_excluded_body = parts
                left = parse_class_body_chars(left_body)
                right_excluded = parse_class_body_chars(right_excluded_body)
                chars = left & (bmp_universe - right_excluded)
                result.append(f"[{_chars_to_class_body(chars)}]")
                index = end + 1

            return "".join(result)

        def replace_property_at(text, index, *, in_class):
            prop = parse_property_escape(text, index)
            if prop is None:
                return None
            prop_type, spec, end = prop
            body = resolve_property_body(spec)
            chars = resolve_property_chars(spec)
            if body is None or chars is None:
                return None
            if prop_type == "P":
                if in_class:
                    return _chars_to_class_body(bmp_universe - chars), end
                return f"[^{body}]", end
            if in_class:
                return body, end
            return f"[{body}]", end

        if "&&" in pattern:
            pattern = normalize_class_intersections(pattern)

        result = []
        index = 0
        in_class = False
        while index < len(pattern):
            replacement = replace_property_at(pattern, index, in_class=in_class)
            if replacement is not None:
                text, index = replacement
                result.append(text)
                continue

            char = pattern[index]
            if char == "\\" and index + 1 < len(pattern):
                result.append(pattern[index:index + 2])
                index += 2
                continue
            if char == "[" and not in_class:
                in_class = True
            elif char == "]" and in_class:
                in_class = False
            result.append(char)
            index += 1

        expanded = "".join(result)

        if self._has_unsupported_class_set_operator(expanded):
            raise RegexParseError(f"Unsupported character-class set operation in {self.regex}")

        return expanded

    def _has_unsupported_class_set_operator(self, pattern):
        escaped = False
        in_class = False
        index = 0
        while index < len(pattern):
            char = pattern[index]
            if escaped:
                escaped = False
                index += 1
                continue
            if char == "\\":
                escaped = True
                index += 1
                continue
            if char == "[":
                in_class = True
            elif char == "]" and in_class:
                in_class = False
            elif in_class and pattern[index:index + 2] == "&&":
                return True
            index += 1
        return False

    def _detect_and_expand_conditionals(self, pattern):
        return pattern, ("(?(" in pattern)

    def _detect_and_expand_lookarounds(self, pattern):
        lookahead_pos = r"\(\?=([^)]+)\)"
        lookahead_neg = r"\(\?!([^)]+)\)"
        lookbehind_pos = r"\(\?<=([^)]+)\)"
        lookbehind_neg = r"\(\?<!([^)]+)\)"

        has_lookarounds = False
        for pattern_type, regex_pattern, assertion_type in [
            ("positive_lookahead", lookahead_pos, "must_follow"),
            ("negative_lookahead", lookahead_neg, "must_not_follow"),
            ("positive_lookbehind", lookbehind_pos, "must_precede"),
            ("negative_lookbehind", lookbehind_neg, "must_not_precede"),
        ]:
            matches = list(re.finditer(regex_pattern, pattern))
            if not matches:
                continue
            has_lookarounds = True
            for match in matches:
                content = match.group(1)
                self.lookarounds.append(
                    {
                        "type": pattern_type,
                        "assertion": assertion_type,
                        "content": content,
                        "marker": f"__{pattern_type.upper()}_{content}__",
                    }
                )

        return pattern, has_lookarounds
