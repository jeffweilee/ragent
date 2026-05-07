---
name: onboard-business-metric
description: Add a new Prometheus business metric to ragent. Use when the user asks to track, count, time, or expose a new business signal — e.g. "add a metric for X", "track Y per tenant", "expose Z to Grafana", "p95 of W". Codifies the cardinality, label-bounding, TDD, and emission-site discipline already wired into bootstrap/metrics.py.
---

# Onboarding a New Business Metric

This skill adapts the patterns already in `src/ragent/bootstrap/metrics.py` to any new metric. Read that file before adding code — every helper described here has a real example there.

---

## Step 1 — Decide the metric type, NOT the dashboard query

Pick the metric type that fits the **source signal**, not the panel you imagine.

| Source signal | Metric type | Example in repo |
|---|---|---|
| Monotonic event count (failures, requests, retries) | **Counter** | `ragent_pipeline_runs_total` |
| Wall-clock or size sample (ask for p50/p95/p99 later) | **Histogram** | `worker_pipeline_duration_seconds` |
| Last-known instant value (queue depth, "is X up") | **Gauge** | `ragent_readyz_probe_status` |
| Snapshot count from an external source of truth (DB, file) | **Custom Collector** | `DocumentStatsCollector` |

Anti-patterns — never do these:
- Don't pre-compute rates, ratios, or percentiles. Prometheus does that at query time. Emit the raw counter / histogram and write the PromQL.
- Don't bake a time window into the metric name (`_per_5m`, `_24h`). Windows belong in `rate(...[5m])`.
- Don't add a separate metric per status / tenant / etc. — use **labels**.

---

## Step 2 — Audit cardinality before writing code

For every label, answer: *what's the upper bound on the value set?*

| Label source | Bound | Action |
|---|---|---|
| Closed enum in code (`status`, `outcome`) | Compile-time | OK to use as label |
| FastAPI route template | Bounded by codebase | OK (instrumentator already does this) |
| Tenant-supplied string (`source_app`, `tenant_id`, `user_id`) | **Unbounded** | Run through `normalize_source_app` or build a similar allow-list |
| Free-form (`error_message`, `path`, `query`) | Unbounded | **Never** as a label — drop or hash to a fixed bucket |
| Persisted column on a table (`mime_type`) | Schema-bounded | OK if column has a real CHECK / ENUM, else bound at emission |

Cross-product cardinality must stay under ~200 series per metric for a single ragent process. If your three labels are 5 × 10 × 4 = 200, that's the ceiling.

If a value set isn't bounded by the codebase, add an env-driven allow-list **before** the metric ships:

```python
# Allow-list lives in .env + spec §4.6.8. See normalize_source_app().
RAGENT_METRICS_<LABEL>_ALLOWLIST=value1,value2,value3
RAGENT_METRICS_<LABEL>_FALLBACK=other
```

The drift test (`tests/unit/test_env_example_drift.py`) will fail if you add the env var to `.env.example` without also updating `docs/00_spec.md` §4.6.8 — keep them symmetric.

---

## Step 3 — Place the metric in `bootstrap/metrics.py`

Definitions live in **one** module so duplicate-registration on the global `prometheus_client.REGISTRY` is impossible. Two-line rule:

1. Define the metric at module scope.
2. Wrap every emission site in a helper function (`record_X`, `observe_X`) that does the cardinality normalization. Never let a caller pass a raw label value through.

```python
_my_metric = Counter(
    "ragent_<noun>_<unit>_total",
    "<one-sentence purpose>. Drives <which dashboard panel>.",
    labelnames=("source_app", "outcome"),
)

def record_my_metric(*, source_app: str | None, outcome: str) -> None:
    _my_metric.labels(
        source_app=normalize_source_app(source_app),
        outcome=outcome,
    ).inc()
```

**Naming**: `ragent_<noun>_<unit>` for ours, `<noun>_<unit>` for prometheus-fastapi-instrumentator's HTTP set. Suffix conventions: `_total` (counter), `_seconds` / `_bytes` (histogram), no suffix (gauge).

---

## Step 4 — Pick the emission site (and only that site)

| Metric kind | Where it fires |
|---|---|
| Pipeline outcome counter | At the **terminal status transition** (READY/FAILED), in worker AND reconciler. Both write to `documents.status`. |
| Pipeline duration histogram | Same place as the outcome counter, observed once with `time.monotonic() - started`. |
| Probe metrics | Inside the wrapper (`run_probe`), not in each probe. Single instrumentation point. |
| HTTP metrics | Already handled by `prometheus-fastapi-instrumentator` in `setup_metrics(app)`. Don't add per-route counters. |
| State gauges (DB-derived) | Custom `Collector.collect()` runs the GROUP BY at scrape time — no background timer, no separate process. |

Rules:
- **One** emission site per outcome. If a status transition happens in two places (worker + reconciler), call the helper from both — don't duplicate the metric increment logic.
- Never emit from inside hot loops over chunks/embeddings — emit once at the boundary.
- For `Collector` subclasses: `collect()` is **synchronous** and runs on the event loop. Async DB clients (`aiomysql`) deadlock there. Use a sync engine (see `make_document_stats_fetcher`).

---

## Step 5 — Mandatory TDD sequence

Per `CLAUDE.md`, every metric ships as Red → Green → Refactor with structural / behavioral commits split.

1. **Red** — write a unit test that asserts the metric appears in the registry with the expected labels and value. Use `prometheus_client.REGISTRY.get_sample_value(name, labels)`. Reset the allow-list cache in an `autouse` fixture so test order doesn't matter:
   ```python
   @pytest.fixture(autouse=True)
   def _allowlist(monkeypatch):
       monkeypatch.setenv("RAGENT_METRICS_SOURCE_APP_ALLOWLIST", "slack")
       from ragent.bootstrap.metrics import _source_app_allowlist, _source_app_fallback
       _source_app_allowlist.cache_clear()
       _source_app_fallback.cache_clear()
   ```
2. **Green** — define the metric + helper. Run the new test. Run `tests/unit/test_health_endpoints.py` to confirm `/metrics` still responds.
3. **Refactor** — extract repeated label-building. Run lint + format.
4. **Wire** — call the helper from the emission site(s). Add a separate test that exercises the call site (or rely on the existing worker / reconciler tests if they cover it).
5. **Verify** — `uv run pytest tests/unit -q`. If touching probes, also `uv run pytest tests/integration -m docker` (start docker daemon first; see `docs/00_rule.md`).

Commit discipline (CLAUDE.md "Tidy First"):
- `[STRUCTURAL]` — moving metric defs, renaming, adding kwargs with defaults that don't change behavior.
- `[BEHAVIORAL]` — anything that changes what's emitted or written.
- **Never mix** in the same commit. Schema migration + writing the new column is two commits.

---

## Step 6 — Wire the auth bypass and exclusion list

Anything served at `/metrics`, `/livez`, `/readyz`, `/startupz` must:
1. Be in `_NO_USER_ID_PATHS` in `src/ragent/bootstrap/app.py` (auth middleware bypass).
2. Be in `_SKIP_PATHS` in `src/ragent/middleware/logging.py` (request log skip).
3. Be in `_EXCLUDED_HANDLERS` in `src/ragent/bootstrap/metrics.py` (instrumentator doesn't track probe traffic as RPS).

If you're adding a new probe-style endpoint, update all three. Forgetting one is a silent dashboard distortion, not a test failure.

---

## Step 7 — Document the dashboard query alongside the metric

In the docstring or a comment next to the definition, write the PromQL the metric is *for*. Future readers should never have to derive it. Two examples already in the codebase:

```python
# Drives the dashboard fail-rate panel:
#   sum by (source_app) (rate(ragent_pipeline_runs_total{outcome="failed"}[5m]))
#   / sum by (source_app) (rate(ragent_pipeline_runs_total[5m]))
```

```python
# Drives the dashboard p95 panel:
#   histogram_quantile(0.95,
#     sum by (le, source_app)
#       (rate(worker_pipeline_duration_seconds_bucket[5m])))
```

---

## Quick checklist (paste into the PR description)

- [ ] Metric type matches the source signal, not the panel (counter / histogram / gauge / collector)
- [ ] Every label has a bounded value set; unbounded ones are normalized
- [ ] Definition lives in `bootstrap/metrics.py`; emission goes through a helper
- [ ] One emission site per outcome (worker + reconciler both call the same helper, not duplicate logic)
- [ ] Unit test asserts `REGISTRY.get_sample_value(...)` with labels
- [ ] If a new env var was added: `.env.example` + `docs/00_spec.md` §4.6.8 both updated (drift test gates this)
- [ ] PromQL the metric drives is documented next to the definition
- [ ] `[STRUCTURAL]` and `[BEHAVIORAL]` commits split
- [ ] `uv run pytest tests/unit -q` green; integration docker tests run if probes / DB shape touched
