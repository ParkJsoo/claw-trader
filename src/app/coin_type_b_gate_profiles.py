"""Shared COIN Type B gate profile definitions."""
from __future__ import annotations

import os


def _to_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def current_thresholds() -> dict[str, object]:
    return {
        "min_change_rate": float(os.getenv("TYPE_B_MIN_CHANGE_RATE", "0.04")),
        "max_change_rate": float(os.getenv("TYPE_B_MAX_CHANGE_RATE", "0.12")),
        "min_near_high": float(os.getenv("TYPE_B_NEAR_HIGH_RATIO", "0.97")),
        "min_vol_24h": float(os.getenv("TYPE_B_MIN_VOL_KRW", "10000000000")),
        "min_ret_5m": float(os.getenv("TYPE_B_MIN_RET_5M", "0.005")),
        "max_ret_5m": float(os.getenv("TYPE_B_MAX_RET_5M", "0.025")),
        "require_ob_ratio": _to_bool(os.getenv("TYPE_B_REQUIRE_OB_RATIO", "true")),
        "min_ob_ratio": float(os.getenv("TYPE_B_MIN_OB_RATIO", "1.05")),
    }


def default_scenarios() -> list[dict[str, object]]:
    base = current_thresholds()

    def _scenario(name: str, **overrides: object) -> dict[str, object]:
        thresholds = dict(base)
        thresholds.update(overrides)
        return {"name": name, "thresholds": thresholds}

    return [
        _scenario("baseline_current"),
        _scenario("relax_change_rate_3pct", min_change_rate=0.03),
        _scenario("relax_near_high_0_95", min_near_high=0.95),
        _scenario("relax_volume_7b", min_vol_24h=7_000_000_000.0),
        _scenario("relax_combo_3pct_0_95_7b", min_change_rate=0.03, min_near_high=0.95, min_vol_24h=7_000_000_000.0),
        _scenario("relax_combo_2_5pct_0_95_7b", min_change_rate=0.025, min_near_high=0.95, min_vol_24h=7_000_000_000.0),
        _scenario(
            "alt_pullback_continuation",
            min_change_rate=0.02,
            min_near_high=0.93,
            min_vol_24h=5_000_000_000.0,
            min_ret_5m=0.001,
            max_ret_5m=0.02,
            min_ob_ratio=1.0,
        ),
        _scenario(
            "alt_broad_trend_positive_5m",
            min_change_rate=0.015,
            min_near_high=0.90,
            min_vol_24h=3_000_000_000.0,
            min_ret_5m=0.0,
            max_ret_5m=0.02,
            require_ob_ratio=False,
            min_ob_ratio=0.0,
        ),
        _scenario(
            "alt_pullback_setup_allow_small_dip",
            min_change_rate=0.015,
            min_near_high=0.88,
            min_vol_24h=3_000_000_000.0,
            min_ret_5m=-0.002,
            max_ret_5m=0.02,
            require_ob_ratio=False,
            min_ob_ratio=0.0,
        ),
    ]


def scenario_map() -> dict[str, dict[str, object]]:
    return {scenario["name"]: scenario["thresholds"] for scenario in default_scenarios()}


def first_gate_fail_reason(sample: dict[str, object], thresholds: dict[str, object]) -> str:
    change_rate = sample.get("change_rate")
    near_high = sample.get("near_high")
    ret_5m = sample.get("ret_5m")
    vol_24h = sample.get("vol_24h")
    ob_ratio = sample.get("ob_ratio")

    if change_rate is None:
        return "incomplete_change_rate"
    if change_rate < float(thresholds["min_change_rate"]):
        return "reject_change_rate_weak"
    if change_rate > float(thresholds["max_change_rate"]):
        return "reject_change_rate_overextended"

    if near_high is None:
        return "incomplete_near_high"
    if near_high < float(thresholds["min_near_high"]):
        return "reject_far_from_high"

    if vol_24h is None:
        return "incomplete_vol_24h"
    if vol_24h < float(thresholds["min_vol_24h"]):
        return "reject_low_vol_24h"

    if ret_5m is None:
        return "reject_no_live_price"
    if ret_5m < float(thresholds["min_ret_5m"]):
        return "reject_ret_5m_weak"
    if ret_5m > float(thresholds["max_ret_5m"]):
        return "reject_ret_5m_overextended"

    if bool(thresholds["require_ob_ratio"]) and ob_ratio is None:
        return "reject_ob_ratio_missing"
    if ob_ratio is not None and ob_ratio < float(thresholds["min_ob_ratio"]):
        return "reject_ob_ratio_weak"

    return "pass_pre_ai"


def alt_shadow_profile_names() -> list[str]:
    raw = os.getenv(
        "COIN_TYPE_B_ALT_SHADOW_PROFILES",
        "alt_broad_trend_positive_5m,alt_pullback_setup_allow_small_dip",
    )
    wanted = [token.strip() for token in raw.split(",") if token.strip()]
    known = scenario_map()
    return [name for name in wanted if name in known]
