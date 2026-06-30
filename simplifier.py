from .charset import representative_chars, stable_seed
from .graph_builder import RegexGraphBuilder


class SimplifiedRegexGraphBuilder(RegexGraphBuilder):
    def __init__(self, regex_pattern, flags=0, validate=True, seed=0):
        self.seed = seed
        super().__init__(regex_pattern, flags=flags, validate=validate)

    def _representatives(self, chars, salt):
        return representative_chars(chars, seed=stable_seed(self.seed, salt))

    def _get_category_chars(self, category_code):
        chars, _count, label = super()._get_category_chars(category_code)
        reps = self._representatives(chars, ("category", str(category_code)))
        return reps, len(reps), label

    def _get_any_chars(self):
        chars = super()._get_any_chars()
        return self._representatives(chars, ("any",))

    def _get_not_literal_chars(self, excluded_char):
        chars = super()._get_not_literal_chars(excluded_char)
        return self._representatives(chars, ("not_literal", excluded_char))

    def _get_in_chars(self, av):
        chars, negated = super()._get_in_chars(av)
        return self._representatives(chars, ("in", repr(av))), negated
