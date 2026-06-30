MAX_REQUIRED_REPEAT_UNROLL = 256
MAX_EXTRA_UNBOUNDED_REPEAT = 3


def effective_repeat_bounds(min_rep, max_rep, maxrepeat_sentinel, *, depth=0):
    if min_rep > MAX_REQUIRED_REPEAT_UNROLL:
        raise ValueError(
            f"Minimum repeat {min_rep} is too large to unroll safely "
            f"(limit {MAX_REQUIRED_REPEAT_UNROLL})."
        )

    if max_rep == maxrepeat_sentinel:
        return min_rep, min_rep + MAX_EXTRA_UNBOUNDED_REPEAT, True

    return min_rep, max_rep, False
