"""Local-complement ablation: full versus negatives without local faults.

Matchmaker's negative sampler first builds one local fault per literal and character
class (the valid string with that position replaced by a designed complement
character, ``minimal_local_fault``), then fills the budget with mutation families.
``no_local`` drops the local stage; the mutation stages fill the same budget, and the
positives are the same as in ``full``. Negative-family tags are recorded, so
``summary.json`` (``negative_tag_counts``) shows how many local faults ``full`` used.

The mutation stages regenerate every local-fault string, so ``no_local`` only reorders
them. ``no_local_strings`` also blocks those exact strings in every stage, so no local
complement reaches the validator.

Default comparison (fill stage off, to isolate step 1 and step 2): ``local_and_families``
(baseline: step 1 + step 2), ``families_only`` (step 2) and ``local_only`` (step 1).
Without the fill, each variant stops when its steps run out, so negative counts differ.
"""

from .boundary_positive import main

if __name__ == "__main__":
    main(
        study="local_complement",
        default_variants="local_and_families,families_only,local_only",
        negative_tags=True,
    )
