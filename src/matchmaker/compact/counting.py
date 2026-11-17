MAX_COMPACT_REPEAT_MIN = 256
MAX_COMPACT_REPEAT_MAX = 256
MAX_COMPACT_COUNT = 10**1000


def validate_compact_repeat_bounds(min_rep, max_rep):
    if min_rep > MAX_COMPACT_REPEAT_MIN:
        raise ValueError(
            f"Minimum repeat {min_rep} is too large for compact sampling "
            f"(limit {MAX_COMPACT_REPEAT_MIN})."
        )
    if max_rep > MAX_COMPACT_REPEAT_MAX:
        raise ValueError(
            f"Maximum repeat {max_rep} is too large for compact sampling "
            f"(limit {MAX_COMPACT_REPEAT_MAX})."
        )


def saturating_add(left, right):
    value = left + right
    return min(value, MAX_COMPACT_COUNT)


def saturating_mul(left, right):
    if left == 0 or right == 0:
        return 0
    if left > MAX_COMPACT_COUNT // right:
        return MAX_COMPACT_COUNT
    return left * right


def saturating_pow(base, exponent):
    if exponent == 0:
        return 1
    if base == 0:
        return 0
    result = 1
    for _ in range(exponent):
        result = saturating_mul(result, base)
        if result >= MAX_COMPACT_COUNT:
            return MAX_COMPACT_COUNT
    return result
