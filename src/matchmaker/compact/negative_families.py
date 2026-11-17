import re

from regex_positive_generator.experimental.negative_choices import (
    SEPARATORS,
    UNICODE_CHOICES,
    ViolationChoiceHelper,
    dedupe_preserve_order,
)
from regex_positive_generator.experimental.negative_context import (
    emit_valid,
    iter_node_contexts,
    literal_text_from_graph,
    repeat_count_for_seed,
    valid_char_for_class,
    walk_nodes,
)


STRESS_LENGTH = 100_000


def _drop_unchanged(candidates, valid):
    return dedupe_preserve_order(candidate for candidate in candidates if candidate != valid)


def has_case_sensitive_constraints(pattern, flags, nodes):
    if flags & re.IGNORECASE or re.search(r"\(\?[aiLmsux-]*i[aiLmsux-]*\)", pattern):
        return False
    if re.search(r"\[[^\]]*[a-z][^\]]*\]", pattern):
        return True
    if re.search(r"\[[^\]]*[A-Z][^\]]*\]", pattern):
        return True
    return any(node.kind == "literal" and isinstance(node.payload, str) and node.payload.isalpha() for node in nodes)


def select_violation_families(builder):
    nodes = list(walk_nodes(builder.root))
    pattern = builder.regex
    selected = [
        "basic_boundary_violation",
        "whitespace_boundary_violation",
        "structure_violation",
        "stress_boundary_violation",
    ]
    has_class = any(node.kind == "class" for node in nodes)
    has_repeat = any(node.kind == "repeat" for node in nodes) or bool(re.search(r"[*+?]|\{\d+(?:,\d*)?\}", pattern))
    has_anchor = any(node.kind == "anchor" for node in nodes)
    has_backref = any(node.kind == "backref" for node in nodes)
    has_conditional = any(node.kind == "conditional" for node in nodes)
    has_choice = any(node.kind == "choice" for node in nodes) or "|" in pattern
    has_zero_width = any(node.kind in {"anchor", "lookaround"} for node in nodes) or any(
        token in pattern for token in (r"\b", r"\B", "(?=", "(?!", "(?<=", "(?<!")
    )

    if has_class:
        selected.extend(["character_class_violation", "group_violation", "wrong_char_substitution"])
    if has_repeat:
        selected.extend(["quantifier_underflow_violation", "quantifier_overflow_violation"])
    if has_anchor:
        selected.append("anchor_violation")
    if has_backref:
        selected.append("backreference_violation")
    if has_conditional:
        selected.append("conditional_violation")
    if re.search(r"\\[dDsSwW]", pattern):
        selected.append("escape_sequence_violation")
    if re.search(r"\\[pP]\{[^}]+\}|\\[pP][A-Za-z]", pattern) or any(ord(ch) > 127 for ch in pattern):
        selected.append("unicode_violation")
    if has_choice:
        selected.append("alternation_violation")
    if has_case_sensitive_constraints(pattern, builder.flags, nodes):
        selected.append("case_sensitivity_violation")
    if has_zero_width:
        selected.append("zero_width_assertion_violation")
    if any(node.kind == "literal" and node.payload in SEPARATORS for node in nodes):
        selected.append("separator_boundary_violation")
    if re.search(r"\\d|[0-9$.,]", pattern) or any(node.kind == "literal" and str(node.payload).isdigit() for node in nodes):
        selected.append("numeric_boundary_violation")
    return dedupe_preserve_order(selected)


def representative_valid(builder):
    text, _memory = emit_valid(builder.root, {})
    return text or ""


def _context_candidates(builder, predicate, replacement_fn):
    candidates = []
    for context in iter_node_contexts(builder.root):
        if not predicate(context.node):
            continue
        for replacement in replacement_fn(context):
            candidates.append(context.prefix + replacement + context.suffix)
    return dedupe_preserve_order(candidates)


def _node_has_wildcard_class(node):
    for context in iter_node_contexts(node):
        if context.is_wildcard_class:
            return True
    return False


def _has_nullable_wildcard_repeat(builder):
    for context in iter_node_contexts(builder.root):
        node = context.node
        if node.kind == "repeat" and node.min_rep == 0 and node.children and _node_has_wildcard_class(node.children[0]):
            return True
    return False


def _dot_matches_newline(builder):
    return bool(getattr(builder, "effective_flags", builder.flags) & re.DOTALL)


def _local_substitution_allowed(context, *, suppress_nullable=False):
    if context.is_wildcard_class:
        return False
    if suppress_nullable and any(kind in {"star", "optional"} for kind in context.repeat_kinds):
        return False
    return True


def _local_context_candidates(builder, predicate, replacement_fn):
    candidates = []
    suppress_nullable = _has_nullable_wildcard_repeat(builder)
    for context in iter_node_contexts(builder.root):
        if not _local_substitution_allowed(context, suppress_nullable=suppress_nullable):
            continue
        if not predicate(context):
            continue
        for replacement in replacement_fn(context):
            candidates.append(context.prefix + replacement + context.suffix)
    return dedupe_preserve_order(candidates)


def minimal_local_faults(builder, helper):
    candidates = []
    suppress_nullable = _has_nullable_wildcard_repeat(builder)
    for context in iter_node_contexts(builder.root):
        node = context.node
        if node.kind in {"literal", "class"} and not _local_substitution_allowed(
            context,
            suppress_nullable=suppress_nullable,
        ):
            continue
        replacements = []
        if node.kind == "literal":
            replacements = helper.literal_choices(node.payload)
        elif node.kind == "class":
            replacements = helper.class_violation_choices(node.payload)
        elif node.kind == "backref":
            valid = context.memory.get(node.payload, "")
            if valid:
                replacements = [valid[:-1] + helper.literal_choices(valid[-1])[0]]
            else:
                replacements = ["x"]
        if replacements:
            candidates.append(context.prefix + replacements[0] + context.suffix)
    return dedupe_preserve_order(candidates)


def character_class_violation(builder, helper):
    candidates = _local_context_candidates(
        builder,
        lambda context: context.node.kind == "class",
        lambda context: helper.class_violation_choices(context.node.payload),
    )
    candidates.extend(wildcard_dot_violation(builder))
    return dedupe_preserve_order(candidates)


def wildcard_dot_violation(builder):
    if _dot_matches_newline(builder):
        return []
    candidates = []
    for context in iter_node_contexts(builder.root):
        if context.is_wildcard_class:
            candidates.append(context.prefix + "\n" + context.suffix)
    return dedupe_preserve_order(candidates)


def group_violation(builder, helper):
    return _local_context_candidates(
        builder,
        lambda context: context.node.kind == "class",
        lambda context: helper.group_violation_sequences(context.node.payload),
    )


def wrong_char_substitution(builder, helper):
    return _local_context_candidates(
        builder,
        lambda context: context.node.kind == "literal",
        lambda context: helper.literal_choices(context.node.payload),
    )


def separator_boundary_violation(builder, helper):
    choices = [":", ";", "/", "_", "-", ".", ",", " "]
    return _local_context_candidates(
        builder,
        lambda context: context.node.kind == "literal" and context.node.payload in SEPARATORS,
        lambda context: [choice for choice in choices if choice != context.node.payload] + ["", context.node.payload * 2],
    )


def numeric_boundary_violation(builder, helper):
    candidates = ["01", ".5", "1.", "-1", "$.99", "1.999", "1,23", ",123", "00:10"]
    candidates.extend(
        _local_context_candidates(
            builder,
            lambda context: context.node.kind == "class" and set(context.node.payload) <= set("0123456789"),
            lambda context: ["a", "!", "é", ",0", "00"],
        )
    )
    return dedupe_preserve_order(candidates)


def escape_sequence_violation(builder, helper):
    candidates = []
    tokens = set(re.findall(r"\\[dDsSwW]", builder.regex))
    for token in tokens:
        if token == r"\d":
            candidates.extend(["a", "!", " ", "中"])
        elif token == r"\D":
            candidates.extend(["0", "5"])
        elif token == r"\w":
            candidates.extend(["-", ".", " ", "\n"])
        elif token == r"\W":
            candidates.extend(["a", "Z", "0", "_"])
        elif token == r"\s":
            candidates.extend(["A", "1", "_", "-"])
        elif token == r"\S":
            candidates.extend([" ", "\t", "\n"])
    candidates.extend(character_class_violation(builder, helper))
    return dedupe_preserve_order(candidates)


def backreference_violation(builder, helper):
    def replacements(context):
        valid = context.memory.get(context.node.payload, "")
        if not valid:
            return ["x"]
        return [valid[:-1] + helper.literal_choices(valid[-1])[0]]

    return _context_candidates(builder, lambda node: node.kind == "backref", replacements)


def quantifier_violations(builder):
    underflow = []
    overflow = []
    for context in iter_node_contexts(builder.root):
        node = context.node
        if node.kind != "repeat" or not node.children:
            continue
        body, _memory = emit_valid(node.children[0], dict(context.memory))
        if body is None:
            continue
        if node.min_rep > 0:
            underflow.append(context.prefix + body * max(0, node.min_rep - 1) + context.suffix)
        if node.max_rep == 1:
            overflow.append(context.prefix + body * 2 + context.suffix)
        elif node.min_rep == node.max_rep or node.min_rep > 1:
            overflow_count = max(node.max_rep + 1, repeat_count_for_seed(node) + 1)
            overflow.append(context.prefix + body * overflow_count + context.suffix)
    return dedupe_preserve_order(underflow), dedupe_preserve_order(overflow)


def alternation_violation(builder, helper):
    literal_text = literal_text_from_graph(builder.root)
    runs = [run for run in re.findall(r"[A-Za-z]+", literal_text) if len(run) >= 2]
    candidates = []
    for run in runs:
        candidates.append(run[:-1])
        mid = min(len(run) - 1, len(run) // 2)
        replacement = helper.literal_choices(run[mid])[0]
        candidates.append(run[:mid] + replacement + run[mid + 1 :])
    return dedupe_preserve_order(candidates)


def conditional_violation(builder):
    valid = representative_valid(builder)
    candidates = []
    if valid:
        candidates.append(valid[::-1])
        if len(valid) > 1:
            candidates.append(valid[1:] + valid[:1])
    return _drop_unchanged(candidates, valid)


def basic_boundary_violation(valid):
    candidates = ["", " ", "\t", "\n"]
    if valid:
        candidates.extend([valid[:1], valid[:-1], valid + "x", "x" + valid, valid + "!"])
    return _drop_unchanged(candidates, valid)


def whitespace_boundary_violation(valid):
    candidates = []
    for sample in [valid] if valid else [""]:
        candidates.extend([" " + sample, sample + " ", "\t" + sample, sample + "\t", "\n" + sample, sample + "\n"])
        if sample:
            midpoint = len(sample) // 2
            candidates.extend([sample[:midpoint] + " " + sample[midpoint:], sample[:midpoint] + "\n" + sample[midpoint:]])
    return _drop_unchanged(candidates, valid)


def structure_violation(valid):
    if not valid:
        return []
    candidates = [valid[::-1]]
    if len(valid) > 1:
        midpoint = max(1, len(valid) // 2)
        candidates.extend([
            valid[1:] + valid[:1],
            valid[:midpoint] + valid[:midpoint] + valid[midpoint:],
            valid[:midpoint],
            valid[: midpoint - 1] + valid[midpoint:],
        ])
    return _drop_unchanged(candidates, valid)


def anchor_violation(valid):
    return _drop_unchanged(["X" + valid, valid + "X", "\n" + valid, valid + "\n"], valid)


def unicode_violation(valid):
    noise = ["café", "naïve", "中文", "Ωmega", "Ж9", "a中b", "é", "üǐ"]
    candidates = list(noise)
    if valid:
        for item in noise[:4]:
            candidates.append(item + valid)
            candidates.append(valid + item)
    return _drop_unchanged(candidates, valid)


def stress_boundary_violation():
    return [
        "A" * STRESS_LENGTH,
        "0" * STRESS_LENGTH,
        "A0" * (STRESS_LENGTH // 2),
        "!" * STRESS_LENGTH,
    ]


def case_sensitivity_violation(builder):
    candidates = []
    suppress_nullable = _has_nullable_wildcard_repeat(builder)
    for context in iter_node_contexts(builder.root):
        if not _local_substitution_allowed(context, suppress_nullable=suppress_nullable):
            continue
        node = context.node
        if node.kind == "literal" and isinstance(node.payload, str) and node.payload.isalpha():
            candidates.append(context.prefix + node.payload.swapcase() + context.suffix)
        elif node.kind == "class":
            chars = set(node.payload)
            ch = valid_char_for_class(chars)
            if ch.isalpha():
                replacement = ch.swapcase()
                if replacement not in chars:
                    candidates.append(context.prefix + replacement + context.suffix)
    return dedupe_preserve_order(candidates)


def zero_width_assertion_violation(valid):
    sample = valid or "word"
    candidates = ["_" + sample, sample + "_", " " + sample + " ", "\n" + sample, sample + "\n"]
    if len(sample) > 1:
        candidates.extend([sample[:1] + "-" + sample[1:], sample[:-1] + "-" + sample[-1:]])
    return _drop_unchanged(candidates, valid)


def family_candidates(builder, helper=None):
    helper = helper or ViolationChoiceHelper(flags=builder.flags)
    valid = representative_valid(builder)
    underflow, overflow = quantifier_violations(builder)
    return {
        "basic_boundary_violation": basic_boundary_violation(valid),
        "whitespace_boundary_violation": whitespace_boundary_violation(valid),
        "separator_boundary_violation": separator_boundary_violation(builder, helper),
        "numeric_boundary_violation": numeric_boundary_violation(builder, helper),
        "character_class_violation": character_class_violation(builder, helper),
        "group_violation": group_violation(builder, helper),
        "wrong_char_substitution": wrong_char_substitution(builder, helper),
        "quantifier_underflow_violation": underflow,
        "quantifier_overflow_violation": overflow,
        "anchor_violation": anchor_violation(valid),
        "backreference_violation": backreference_violation(builder, helper),
        "conditional_violation": conditional_violation(builder),
        "escape_sequence_violation": escape_sequence_violation(builder, helper),
        "unicode_violation": unicode_violation(valid),
        "stress_boundary_violation": stress_boundary_violation(),
        "alternation_violation": alternation_violation(builder, helper),
        "case_sensitivity_violation": case_sensitivity_violation(builder),
        "zero_width_assertion_violation": zero_width_assertion_violation(valid),
        "structure_violation": structure_violation(valid),
    }


def bounded_exhaustive_candidates(pattern, valid):
    alphabet = dedupe_preserve_order(list("aA0! _-.,") + list(valid) + list(pattern[:32]) + list(UNICODE_CHOICES[:3]))
    for ch in alphabet:
        yield ch
    for left in alphabet[:16]:
        for right in alphabet[:16]:
            yield left + right
    if valid:
        for ch in alphabet[:12]:
            yield ch + valid
            yield valid + ch


def unbounded_exhaustive_candidates(pattern, valid):
    alphabet = dedupe_preserve_order(
        list("\naA0! _-.,;:/@#&")
        + list(valid)
        + list(pattern[:64])
        + list(UNICODE_CHOICES)
    )
    if not alphabet:
        alphabet = ["x"]
    round_index = 1
    while True:
        if valid:
            yield "\n" * round_index + valid
            yield valid + "\n" * round_index
            for index in range(len(valid) + 1):
                yield valid[:index] + "\n" * round_index + valid[index:]
        yield "\n" * round_index
        yield ("!\n") * round_index
        yield ("A\n0") * round_index
        for ch in alphabet:
            yield ch * round_index
            if valid:
                yield ch * round_index + valid
                yield valid + ch * round_index
                yield f"{valid}!{round_index}"
                yield f"{round_index}!{valid}"
        for index, left in enumerate(alphabet):
            right = alphabet[(index + round_index) % len(alphabet)]
            yield (left + right) * round_index
            yield left * round_index + right * round_index
        round_index += 1


def optimized_family_fill_candidates(builder, helper=None):
    helper = helper or ViolationChoiceHelper(flags=builder.flags)
    suppress_nullable = _has_nullable_wildcard_repeat(builder)
    round_index = 1
    while True:
        if not _dot_matches_newline(builder):
            for context in iter_node_contexts(builder.root):
                if context.is_wildcard_class:
                    noise = "\n" * round_index
                    yield context.prefix + noise + context.suffix
                    yield noise + context.prefix + context.suffix
                    yield context.prefix + context.suffix + noise

        for context in iter_node_contexts(builder.root):
            node = context.node
            if node.kind in {"literal", "class"} and not _local_substitution_allowed(
                context,
                suppress_nullable=suppress_nullable,
            ):
                continue
            if node.kind == "class" and not context.is_wildcard_class:
                replacements = helper.class_violation_choices(node.payload) + helper.group_violation_sequences(node.payload)
                for replacement in replacements:
                    yield context.prefix + (replacement * round_index) + context.suffix
            elif node.kind == "literal":
                for replacement in helper.literal_choices(node.payload):
                    yield context.prefix + (replacement * round_index) + context.suffix
            elif node.kind == "backref":
                replay = context.memory.get(node.payload, "")
                if replay:
                    for replacement in helper.literal_choices(replay[-1]):
                        yield context.prefix + replay[:-1] + (replacement * round_index) + context.suffix

        underflow, overflow = quantifier_violations(builder)
        for candidate in underflow + overflow:
            yield candidate

        round_index += 1
