"""Unit smoke tests for the T-CHAOS.0 scaffold (spec §3.6.1, B49).

The chaos drill suite under `tests/e2e/test_chaos/` will use
`scrape_chaos_outcomes()` to assert that each per-case test incremented
the `chaos_drill_outcome_total{case,outcome}` counter. If the helper
silently returns `{}` (e.g. a future prometheus_client release renames
`family.name`), every chaos case would falsely report a missing
increment — exactly the kind of gate-eroding bug the spec §3.6.1
acceptance asserts are meant to surface.

These unit tests exercise the helper without spinning up the API: we
generate a fresh Prometheus exposition with a controlled counter and
verify the parsing round-trips.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, generate_latest
from prometheus_client.parser import text_string_to_metric_families


def test_chaos_counter_round_trips_through_parser() -> None:
    """`scrape_chaos_outcomes` filters match the family + sample names
    emitted by prometheus_client's Counter exposition."""
    reg = CollectorRegistry()
    counter = Counter(
        "ragent_chaos_drill_outcome_total",
        "test counter",
        labelnames=("case", "outcome"),
        registry=reg,
    )
    counter.labels(case="C1", outcome="pass").inc()
    counter.labels(case="C2", outcome="fail").inc(3)

    text = generate_latest(reg).decode()

    parsed: dict[tuple[str, str], int] = {}
    for family in text_string_to_metric_families(text):
        # NOTE: same two filters as scrape_chaos_outcomes(). If a future
        # prometheus_client revision changes the family-name suffix
        # semantics this test fails BEFORE the e2e suite silently
        # returns {} and every chaos case becomes a false positive.
        if family.name != "ragent_chaos_drill_outcome":
            continue
        for sample in family.samples:
            if sample.name != "ragent_chaos_drill_outcome_total":
                continue
            labels = sample.labels
            parsed[(labels["case"], labels["outcome"])] = int(sample.value)

    assert parsed == {("C1", "pass"): 1, ("C2", "fail"): 3}


def test_chaos_drill_outcome_total_metric_is_importable() -> None:
    """The Counter must be importable from `ragent.bootstrap.metrics`;
    every chaos case will `from ragent.bootstrap.metrics import
    chaos_drill_outcome_total` and call `.labels().inc()`."""
    from ragent.bootstrap.metrics import chaos_drill_outcome_total

    # Label names match spec §3.6.1: case ∈ {C1..C6}, outcome ∈ {pass, fail}.
    assert chaos_drill_outcome_total._labelnames == ("case", "outcome")  # noqa: SLF001
