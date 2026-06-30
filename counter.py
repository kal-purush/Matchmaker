from dataclasses import dataclass

from .charset import representative_chars, stable_seed
from .repeats import effective_repeat_bounds


@dataclass(frozen=True)
class CountResult:
    total: int
    exact: bool = True
    has_backref: bool = False
    reason: str = ""


class RegexSpaceCounter:
    def __init__(self, builder, seed=0):
        self.builder = builder
        self.seed = seed
        self.reasons = []
        self.has_backref = False

    def count_main(self):
        self.reasons = []
        self.has_backref = False
        total = self._count_sequence(self.builder.parsed_tree, simplified=False, depth=0)
        return CountResult(
            total=total,
            exact=not self.reasons,
            has_backref=self.has_backref,
            reason="; ".join(dict.fromkeys(self.reasons)),
        )

    def count_simplified(self):
        self.reasons = []
        self.has_backref = False
        total = self._count_sequence(self.builder.parsed_tree, simplified=True, depth=0)
        return CountResult(
            total=total,
            exact=not self.reasons,
            has_backref=self.has_backref,
            reason="; ".join(dict.fromkeys(self.reasons)),
        )

    def _mark_inexact(self, reason):
        self.reasons.append(reason)

    def _count_sequence(self, items, *, simplified, depth):
        import sre_constants

        sequence = items if isinstance(items, (list, tuple)) else items.data
        total = 1
        for op, av in sequence:
            if op == sre_constants.FAILURE:
                part = 0
            elif op == sre_constants.LITERAL:
                part = 1
            elif op == sre_constants.ANY:
                part = self._char_count(self.builder._get_any_chars(), simplified, salt=("any", depth))
            elif op == sre_constants.CATEGORY:
                chars, _, _ = self.builder._get_category_chars(av)
                part = self._char_count(chars, simplified, salt=("category", str(av), depth))
            elif op == sre_constants.IN:
                chars, _negated = self.builder._get_in_chars(av)
                part = self._char_count(chars, simplified, salt=("in", repr(av), depth))
            elif op == sre_constants.NOT_LITERAL:
                chars = self.builder._get_not_literal_chars(chr(av))
                part = self._char_count(chars, simplified, salt=("not_literal", av, depth))
            elif op == sre_constants.BRANCH:
                part = sum(self._count_sequence(alt, simplified=simplified, depth=depth) for alt in av[1])
            elif op == sre_constants.SUBPATTERN:
                part = self._count_sequence(av[3], simplified=simplified, depth=depth)
            elif hasattr(sre_constants, "GROUPREF_EXISTS") and op == sre_constants.GROUPREF_EXISTS:
                _group_id, yes_tree, no_tree = av
                yes_total = self._count_sequence(yes_tree, simplified=simplified, depth=depth)
                no_total = self._count_sequence(no_tree, simplified=simplified, depth=depth) if no_tree else 1
                part = yes_total + no_total
                self._mark_inexact("conditional depends on capture state")
            elif op in (sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT):
                part = self._count_repeat(av[0], av[1], av[2], simplified=simplified, depth=depth)
            elif hasattr(sre_constants, "POSSESSIVE_REPEAT") and op == sre_constants.POSSESSIVE_REPEAT:
                part = self._count_repeat(av[0], av[1], av[2], simplified=simplified, depth=depth)
                self._mark_inexact("possessive repeat needs match validation")
            elif hasattr(sre_constants, "ATOMIC_GROUP") and op == sre_constants.ATOMIC_GROUP:
                part = self._count_sequence(av, simplified=simplified, depth=depth)
                self._mark_inexact("atomic group needs match validation")
            elif str(op) in {"ASSERT", "ASSERT_NOT"}:
                part = 1
                self._mark_inexact("lookaround is stateful")
            elif op == sre_constants.GROUPREF:
                part = 1
                self.has_backref = True
                self._mark_inexact("backreference depends on capture text")
            elif op == sre_constants.AT:
                part = 1
            else:
                part = 1
                self._mark_inexact(f"unsupported opcode {op}")

            total *= part

        return total

    def _count_repeat(self, min_rep, max_rep, sub_tree, *, simplified, depth):
        import sre_constants

        _effective_min, effective_max, was_unbounded = effective_repeat_bounds(
            min_rep,
            max_rep,
            sre_constants.MAXREPEAT,
            depth=depth,
        )
        if was_unbounded:
            self._mark_inexact("unbounded repeat counted with graph unroll cap")

        sub_count = self._count_sequence(sub_tree, simplified=simplified, depth=depth + 1)
        return sum(sub_count**count for count in range(min_rep, effective_max + 1))

    def _char_count(self, chars, simplified, salt):
        if not simplified:
            return len(set(chars))
        return len(representative_chars(chars, seed=stable_seed(self.seed, salt)))
