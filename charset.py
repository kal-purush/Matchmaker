import random
import string
import hashlib


def stable_seed(seed, salt):
    payload = f"{seed!r}:{salt!r}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def representative_chars(chars, seed=0):
    ordered = sorted(set(chars))
    if len(ordered) <= 3:
        return set(ordered)

    rng = random.Random(seed)
    middle = rng.choice(ordered[1:-1])
    return set([ordered[0], middle, ordered[-1]])


def printable_without(chars):
    return set(string.printable) - set(chars)
