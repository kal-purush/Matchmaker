from ablation.variants import VARIANTS


def test_factorial_registry_contains_all_eight_configurations():
    assert {
        name for name in VARIANTS
        if len(name) == 6 and name.startswith("p")
    } == {
        f"p{positive}l{local}m{mutation}"
        for positive in (0, 1)
        for local in (0, 1)
        for mutation in (0, 1)
    }


def test_full_matches_all_on_factorial_variant():
    pattern = r"[a-z]{2}[0-9]{2}"
    full = VARIANTS["full"][1](pattern, 30, 30, 9)
    factorial_full = VARIANTS["p1l1m1"][1](pattern, 30, 30, 9)
    assert factorial_full == full


def test_factorial_variants_return_tagged_negatives_on_request():
    _positive, negatives = VARIANTS["p0l1m0"][1](
        r"[a-z]{2}",
        10,
        20,
        4,
        include_negative_tags=True,
    )
    assert negatives
    assert {tag for _sample, tag in negatives} == {"minimal_local_fault"}


def test_factorial_gates_negative_components_without_tagging():
    pattern = r"ab[0-9]"
    _positive, no_negative_components = VARIANTS["p1l0m0"][1](
        pattern, 10, 20, 4
    )
    _positive, local_only = VARIANTS["p1l1m0"][1](pattern, 10, 20, 4)
    _positive, mutation_only = VARIANTS["p1l0m1"][1](pattern, 10, 20, 4)

    assert no_negative_components == []
    assert local_only
    assert mutation_only
