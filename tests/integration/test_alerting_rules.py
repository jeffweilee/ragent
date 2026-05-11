"""T7.1a — Prometheus alert: reconciler_tick_total flat for >10 min fires alert (R8, S30)."""

from __future__ import annotations

from pathlib import Path

import yaml

_ALERTS_PATH = Path(__file__).resolve().parents[2] / "deploy" / "prometheus" / "alerts.yaml"


def _load_rules() -> dict:
    with _ALERTS_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_alerts_file_exists() -> None:
    assert _ALERTS_PATH.is_file(), f"missing alert rules file at {_ALERTS_PATH}"


def test_reconciler_tick_stalled_rule_present() -> None:
    rules = _load_rules()
    groups = rules["groups"]
    rec_group = next(g for g in groups if g["name"] == "ragent.reconciler")
    rule_names = [r["alert"] for r in rec_group["rules"]]
    assert "ReconcilerTickStalled" in rule_names


def test_alert_uses_reconciler_tick_total_metric() -> None:
    rules = _load_rules()
    rule = _find_alert(rules, "ReconcilerTickStalled")
    assert "reconciler_tick_total" in rule["expr"]


def test_alert_window_is_at_least_10_minutes() -> None:
    """expr must look back ≥ 10m so a single missed tick (5m cron) still appears stale."""
    rules = _load_rules()
    rule = _find_alert(rules, "ReconcilerTickStalled")
    expr = rule["expr"]
    # accepts either [10m] or larger windows
    assert "[10m]" in expr or "[15m]" in expr or "[20m]" in expr


def test_alert_fires_when_increase_is_zero() -> None:
    """A flat counter (no ticks) → increase()==0 → expr is true."""
    rules = _load_rules()
    rule = _find_alert(rules, "ReconcilerTickStalled")
    assert "== 0" in rule["expr"].replace(" ", "") or "==0" in rule["expr"].replace(" ", "")


def test_alert_severity_critical() -> None:
    rules = _load_rules()
    rule = _find_alert(rules, "ReconcilerTickStalled")
    assert rule["labels"]["severity"] == "critical"


def _find_alert(rules: dict, name: str) -> dict:
    for group in rules["groups"]:
        for rule in group["rules"]:
            if rule.get("alert") == name:
                return rule
    raise AssertionError(f"alert {name!r} not found")
