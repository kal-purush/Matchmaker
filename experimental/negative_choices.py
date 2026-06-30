import string
import re


MUTATION_CHARS = tuple(
    dict.fromkeys(
        string.ascii_lowercase
        + string.ascii_uppercase
        + string.digits
        + string.punctuation
        + " \t\n"
        + "éΩ中"
    )
)
SEPARATORS = tuple(".-,_/:@")
UNICODE_CHOICES = ("é", "Ω", "Ж", "中", "あ", "ü", "ǐ")
PRINTABLE_NON_SPACE = set(string.printable) - set(string.whitespace)
ASCII_WORD = set(string.ascii_letters + string.digits + "_")
PRINTABLE = set(string.printable)
ASCII_WORD_OR_SPACE = ASCII_WORD | set(string.whitespace)


def printable_exclusions(chars):
    char_set = set(chars)
    return PRINTABLE - char_set


def looks_like_broad_printable_class(chars):
    char_set = set(chars)
    excluded = printable_exclusions(char_set)
    return len(char_set) >= 80 and 0 < len(excluded) <= 24


def is_ascii_word_class(chars):
    return set(chars) == ASCII_WORD


def is_ascii_word_or_space_class(chars):
    return set(chars) == ASCII_WORD_OR_SPACE


def is_universal_printable_class(chars):
    return set(chars) == PRINTABLE


def dedupe_preserve_order(items):
    seen = set()
    out = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


class ViolationChoiceHelper:
    def __init__(self, seed=0, flags=0):
        self.seed = seed
        self.flags = flags

    @property
    def ascii_semantics(self):
        return bool(self.flags & re.ASCII)

    def literal_choices(self, ch):
        if ch.isalpha():
            choices = []
            choices.append("b" if ch != "b" else "a")
            choices.extend(["0", "!", "é", ch.swapcase()])
            return dedupe_preserve_order(c for c in choices if c != ch)
        if ch.isdigit():
            return dedupe_preserve_order(["a", "!", "é"])
        if ch in SEPARATORS:
            return dedupe_preserve_order([sep for sep in (":", ";", "/", "_", "-", ".", ",", " ") if sep != ch])
        if ch.isspace():
            return ["A", "1", "_"]
        return dedupe_preserve_order(["a", "0", "é", "!"])

    def class_violation_choices(self, chars):
        char_set = set(chars)
        ascii_letters = set(string.ascii_letters)
        ascii_digits = set(string.digits)
        punctuation = set(string.punctuation)
        choices = []

        if is_universal_printable_class(char_set):
            choices.extend([])
        elif looks_like_broad_printable_class(char_set):
            choices.extend(sorted(printable_exclusions(char_set)))
        elif char_set == ASCII_WORD:
            choices.extend(["!", "-", ".", " ", "\t", "\n"])
            if self.ascii_semantics:
                choices.extend(["é", "Ω", "中"])
        elif char_set == ASCII_WORD_OR_SPACE:
            choices.extend(["!", "-", ".", ",", ";", ":", "/", "@"])
        elif char_set and char_set <= ascii_letters:
            if char_set <= set(string.ascii_uppercase):
                choices.append("a")
            elif char_set <= set(string.ascii_lowercase):
                choices.append("A")
            choices.extend(["0", "!", "é", "Ω", "中", " "])
        elif char_set and char_set <= ascii_digits:
            choices.extend(["a", "Z", "!", " ", "é"])
        elif char_set == PRINTABLE_NON_SPACE:
            choices.extend([" ", "\t", "\n"])
        elif char_set and char_set <= punctuation:
            choices.extend(["a", "0", "é", " "])
        elif char_set and char_set & set(string.whitespace) and not (char_set - set(string.whitespace)):
            choices.extend(["A", "1", "_", "é"])
        elif char_set <= PRINTABLE:
            choices.extend(["a", "0", "!", " "])
        else:
            choices.extend(["a", "0", "!", " ", "é", "Ω", "中"])

        return dedupe_preserve_order(choice for choice in choices if choice not in char_set)

    def group_violation_sequences(self, chars, width=3):
        char_set = set(chars)
        if is_universal_printable_class(char_set):
            return []
        if looks_like_broad_printable_class(char_set):
            excluded = sorted(printable_exclusions(char_set))
            return dedupe_preserve_order(ch * 2 for ch in excluded[:8])
        if char_set == ASCII_WORD:
            choices = ["!!", "--", "..", "  ", "\t\t"]
            if self.ascii_semantics:
                choices.extend(["éé", "ΩΩ", "中中"])
            return choices
        if char_set == ASCII_WORD_OR_SPACE:
            return ["!!", "--", "..", ",,", ";;", "::", "//"]
        if char_set and char_set <= set(string.ascii_letters):
            return ["00", "!!", "éé", "中中"]
        if char_set and char_set <= set(string.digits):
            return ["aa", "!!", "éé"]
        if char_set <= PRINTABLE:
            choices = ["aa", "00", "!!", "  ", "\t\t"]
            return dedupe_preserve_order(choice for choice in choices if any(ch not in char_set for ch in choice))
        choices = ["aa", "00", "!!", "éé", "  ", "\t\t"]
        return dedupe_preserve_order(choice for choice in choices if any(ch not in char_set for ch in choice))

    def mutation_chars(self):
        return MUTATION_CHARS
