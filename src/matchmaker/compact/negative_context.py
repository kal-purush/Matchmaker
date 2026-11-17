import string
from dataclasses import dataclass, field


BROAD_WILDCARD_CHARS = frozenset(string.printable) - {"\n", "\r"}


@dataclass(frozen=True)
class RepeatInfo:
    min_rep: int
    max_rep: int

    @property
    def kind(self):
        if self.min_rep == 0 and self.max_rep == 1:
            return "optional"
        if self.min_rep == 0:
            return "star"
        if self.min_rep == 1 and self.max_rep > 1:
            return "plus"
        if self.min_rep == self.max_rep:
            return "exact"
        return "bounded"


@dataclass
class NodeContext:
    prefix: str
    node: object
    suffix: str
    memory: dict
    repeat_stack: tuple = field(default_factory=tuple)

    @property
    def repeat_kinds(self):
        return tuple(info.kind for info in self.repeat_stack)

    @property
    def is_wildcard_class(self):
        return self.node.kind == "class" and set(self.node.payload) == BROAD_WILDCARD_CHARS


def valid_char_for_class(chars):
    chars = sorted(chars)
    return chars[0] if chars else "a"


def repeat_count_for_seed(node):
    if node.max_rep <= 0:
        return 0
    return max(node.min_rep, 1)


def emit_valid(node, memory=None):
    memory = {} if memory is None else dict(memory)

    if node.kind == "sequence":
        parts = []
        local_memory = dict(memory)
        for child in node.children:
            text, local_memory = emit_valid(child, local_memory)
            if text is None:
                return None, memory
            parts.append(text)
        return "".join(parts), local_memory

    if node.kind == "choice":
        for child in node.children:
            text, next_memory = emit_valid(child, dict(memory))
            if text is not None:
                return text, next_memory
        return None, memory

    if node.kind == "conditional":
        chosen = node.children[0] if memory.get(node.payload) is not None else node.children[1]
        return emit_valid(chosen, memory)

    if node.kind == "repeat":
        pieces = []
        local_memory = dict(memory)
        for _ in range(repeat_count_for_seed(node)):
            text, local_memory = emit_valid(node.children[0], local_memory)
            if text is None:
                return None, memory
            pieces.append(text)
        return "".join(pieces), local_memory

    if node.kind == "literal":
        return node.payload, memory

    if node.kind == "class":
        return valid_char_for_class(node.payload), memory

    if node.kind == "group":
        if not node.children:
            return "", memory
        text, next_memory = emit_valid(node.children[0], dict(memory))
        if text is None:
            return None, memory
        if node.payload is not None:
            next_memory[node.payload] = text
        return text, next_memory

    if node.kind == "passthrough":
        if not node.children:
            return "", memory
        return emit_valid(node.children[0], memory)

    if node.kind == "backref":
        if node.payload not in memory:
            return None, memory
        return memory[node.payload], memory

    if node.kind in {"anchor", "lookaround"}:
        return "", memory

    if node.kind == "impossible":
        return None, memory

    return "", memory


def emit_valid_sequence(nodes, memory):
    parts = []
    local_memory = dict(memory)
    for node in nodes:
        text, local_memory = emit_valid(node, local_memory)
        if text is None:
            return None, memory
        parts.append(text)
    return "".join(parts), local_memory


def walk_nodes(node):
    yield node
    for child in node.children:
        yield from walk_nodes(child)


def iter_node_contexts(root):
    yield from _iter_contexts(root, prefix="", suffix="", memory={}, repeat_stack=())


def _iter_contexts(node, prefix, suffix, memory, repeat_stack):
    if node.kind == "sequence":
        cur_prefix = prefix
        cur_memory = dict(memory)
        for index, child in enumerate(node.children):
            valid_child, memory_after_child = emit_valid(child, cur_memory)
            tail, _tail_memory = emit_valid_sequence(node.children[index + 1 :], dict(memory_after_child))
            if valid_child is None or tail is None:
                break
            child_suffix = tail + suffix
            yield from _iter_contexts(child, cur_prefix, child_suffix, dict(cur_memory), repeat_stack)
            cur_prefix += valid_child
            cur_memory = memory_after_child
        return

    if node.kind in {"literal", "class", "backref", "anchor", "lookaround", "conditional", "choice", "repeat"}:
        yield NodeContext(prefix=prefix, node=node, suffix=suffix, memory=dict(memory), repeat_stack=repeat_stack)

    if node.kind == "choice":
        for child in node.children:
            yield from _iter_contexts(child, prefix, suffix, dict(memory), repeat_stack)
        return

    if node.kind == "conditional":
        chosen = node.children[0] if memory.get(node.payload) is not None else node.children[1]
        yield from _iter_contexts(chosen, prefix, suffix, dict(memory), repeat_stack)
        return

    if node.kind == "repeat":
        rep_count = repeat_count_for_seed(node)
        if rep_count <= 0:
            return
        child = node.children[0]
        valid_parts = []
        memories_before = []
        local_memory = dict(memory)
        for _ in range(rep_count):
            memories_before.append(dict(local_memory))
            text, local_memory = emit_valid(child, local_memory)
            if text is None:
                return
            valid_parts.append(text)
        for index in range(rep_count):
            occurrence_prefix = prefix + "".join(valid_parts[:index])
            occurrence_suffix = "".join(valid_parts[index + 1 :]) + suffix
            child_repeat_stack = repeat_stack + (RepeatInfo(node.min_rep, node.max_rep),)
            yield from _iter_contexts(child, occurrence_prefix, occurrence_suffix, memories_before[index], child_repeat_stack)
        return

    if node.kind in {"group", "passthrough"}:
        if node.children:
            yield from _iter_contexts(node.children[0], prefix, suffix, dict(memory), repeat_stack)


def literal_text_from_graph(node):
    if node.kind == "literal":
        return node.payload
    if node.kind in {"sequence", "group", "passthrough"}:
        return "".join(literal_text_from_graph(child) for child in node.children)
    if node.kind == "choice":
        return "|".join(literal_text_from_graph(child) for child in node.children)
    if node.kind == "repeat":
        return literal_text_from_graph(node.children[0]) if node.children else ""
    return ""
