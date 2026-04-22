"""Shared COIN Type B reject insight helpers."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _to_float(raw: object) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class _ThresholdSpec:
    sample_field: str
    summary_field: str
    comparison: str
    threshold: float
    near_margin: float
    very_near_margin: float
    gap_unit: str


def _env_float(name: str, default: str) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


def _threshold_spec(reason: str) -> _ThresholdSpec | None:
    specs = {
        "reject_change_rate_weak": _ThresholdSpec(
            sample_field="change_rate",
            summary_field="change_rate_pct",
            comparison="min",
            threshold=_env_float("TYPE_B_MIN_CHANGE_RATE", "0.04"),
            near_margin=0.01,
            very_near_margin=0.005,
            gap_unit="pct_point",
        ),
        "reject_change_rate_overextended": _ThresholdSpec(
            sample_field="change_rate",
            summary_field="change_rate_pct",
            comparison="max",
            threshold=_env_float("TYPE_B_MAX_CHANGE_RATE", "0.12"),
            near_margin=0.02,
            very_near_margin=0.01,
            gap_unit="pct_point",
        ),
        "reject_far_from_high": _ThresholdSpec(
            sample_field="near_high",
            summary_field="near_high_ratio",
            comparison="min",
            threshold=_env_float("TYPE_B_NEAR_HIGH_RATIO", "0.97"),
            near_margin=0.02,
            very_near_margin=0.01,
            gap_unit="ratio",
        ),
        "reject_low_vol_24h": _ThresholdSpec(
            sample_field="vol_24h",
            summary_field="vol_24h_krw",
            comparison="min",
            threshold=_env_float("TYPE_B_MIN_VOL_KRW", "10000000000"),
            near_margin=2_000_000_000.0,
            very_near_margin=1_000_000_000.0,
            gap_unit="krw",
        ),
        "reject_ret_5m_weak": _ThresholdSpec(
            sample_field="ret_5m",
            summary_field="ret_5m_pct",
            comparison="min",
            threshold=_env_float("TYPE_B_MIN_RET_5M", "0.005"),
            near_margin=0.0025,
            very_near_margin=0.001,
            gap_unit="pct_point",
        ),
        "reject_ret_5m_overextended": _ThresholdSpec(
            sample_field="ret_5m",
            summary_field="ret_5m_pct",
            comparison="max",
            threshold=_env_float("TYPE_B_MAX_RET_5M", "0.025"),
            near_margin=0.005,
            very_near_margin=0.0025,
            gap_unit="pct_point",
        ),
        "reject_ob_ratio_weak": _ThresholdSpec(
            sample_field="ob_ratio",
            summary_field="ob_ratio",
            comparison="min",
            threshold=_env_float("TYPE_B_MIN_OB_RATIO", "1.05"),
            near_margin=0.03,
            very_near_margin=0.01,
            gap_unit="ratio",
        ),
    }
    return specs.get(reason)


def _scaled_value(value: float, *, summary_field: str) -> float:
    if summary_field.endswith("_pct"):
        return value * 100.0
    return value


def _metric_summary(samples: list[dict[str, object]], field: str, *, pct: bool = False) -> dict[str, float] | None:
    values = [_to_float(sample.get(field)) for sample in samples]
    nums = [v for v in values if v is not None]
    if not nums:
        return None
    scale = 100.0 if pct else 1.0
    return {
        "avg": round(sum(nums) / len(nums) * scale, 3),
        "min": round(min(nums) * scale, 3),
        "max": round(max(nums) * scale, 3),
    }


def _threshold_context(reason: str, samples: list[dict[str, object]]) -> dict[str, object] | None:
    spec = _threshold_spec(reason)
    if spec is None:
        return None

    values = [_to_float(sample.get(spec.sample_field)) for sample in samples]
    nums = [v for v in values if v is not None]
    if not nums:
        return None

    eps = 1e-12

    if spec.comparison == "min":
        gaps = [spec.threshold - value for value in nums if value < spec.threshold]
        near_count = sum(1 for gap in gaps if gap <= spec.near_margin + eps)
        very_near_count = sum(1 for gap in gaps if gap <= spec.very_near_margin + eps)
    else:
        gaps = [value - spec.threshold for value in nums if value > spec.threshold]
        near_count = sum(1 for gap in gaps if gap <= spec.near_margin + eps)
        very_near_count = sum(1 for gap in gaps if gap <= spec.very_near_margin + eps)

    if not gaps:
        return None

    threshold_value = _scaled_value(spec.threshold, summary_field=spec.summary_field)
    near_margin = _scaled_value(spec.near_margin, summary_field=spec.summary_field)
    very_near_margin = _scaled_value(spec.very_near_margin, summary_field=spec.summary_field)
    scaled_gaps = [_scaled_value(gap, summary_field=spec.summary_field) for gap in gaps]

    return {
        "metric": spec.summary_field,
        "comparison": spec.comparison,
        "threshold": round(threshold_value, 3),
        "gap_unit": spec.gap_unit,
        "avg_gap": round(sum(scaled_gaps) / len(scaled_gaps), 3),
        "min_gap": round(min(scaled_gaps), 3),
        "max_gap": round(max(scaled_gaps), 3),
        "near_threshold": {
            "margin": round(near_margin, 3),
            "count": near_count,
            "share_pct": round((near_count / len(gaps)) * 100, 2) if gaps else 0.0,
        },
        "very_near_threshold": {
            "margin": round(very_near_margin, 3),
            "count": very_near_count,
            "share_pct": round((very_near_count / len(gaps)) * 100, 2) if gaps else 0.0,
        },
    }


def summarize_reject_samples(reason: str, samples: list[dict[str, object]]) -> dict[str, object]:
    if not samples:
        return {}

    unique_symbols: list[str] = []
    seen: set[str] = set()
    for sample in samples:
        symbol = sample.get("symbol")
        if symbol is None:
            continue
        symbol_text = str(symbol)
        if symbol_text and symbol_text not in seen:
            unique_symbols.append(symbol_text)
            seen.add(symbol_text)
        if len(unique_symbols) >= 5:
            break

    out: dict[str, object] = {
        "sample_count": len(samples),
        "sample_symbols": unique_symbols,
    }
    field_map = {
        "change_rate": ("change_rate_pct", True),
        "near_high": ("near_high_ratio", False),
        "ret_5m": ("ret_5m_pct", True),
        "vol_24h": ("vol_24h_krw", False),
        "ob_ratio": ("ob_ratio", False),
    }
    for field, (label, pct) in field_map.items():
        summary = _metric_summary(samples, field, pct=pct)
        if summary:
            out[label] = summary

    threshold = _threshold_context(reason, samples)
    if threshold:
        out["threshold_context"] = threshold
    return out


def format_reject_insight_short(reason: str, insight: dict[str, object]) -> str:
    metric_aliases = {
        "change_rate_pct": "avg_change",
        "near_high_ratio": "avg_near_high",
        "ret_5m_pct": "avg_ret_5m",
        "vol_24h_krw": "avg_vol",
        "ob_ratio": "avg_ob_ratio",
    }
    metrics: list[str] = []
    for field, label in metric_aliases.items():
        metric = insight.get(field)
        if not isinstance(metric, dict) or "avg" not in metric:
            continue
        avg = float(metric["avg"])
        if field.endswith("_pct"):
            metrics.append(f"{label}={avg:.2f}%")
        elif field == "vol_24h_krw":
            metrics.append(f"{label}={avg / 1_000_000_000:.2f}B")
        else:
            metrics.append(f"{label}={avg:.3f}")

    threshold = insight.get("threshold_context")
    if isinstance(threshold, dict):
        avg_gap = threshold.get("avg_gap")
        near = threshold.get("near_threshold")
        gap_unit = str(threshold.get("gap_unit") or "")
        if isinstance(avg_gap, (float, int)):
            if gap_unit == "pct_point":
                metrics.append(f"avg_gap={float(avg_gap):.2f}pp")
            elif gap_unit == "krw":
                metrics.append(f"avg_gap={float(avg_gap) / 1_000_000_000:.2f}B")
            else:
                metrics.append(f"avg_gap={float(avg_gap):.3f}")
        if isinstance(near, dict):
            margin = near.get("margin")
            count = int(near.get("count", 0) or 0)
            total = int(insight.get("sample_count", 0) or 0)
            if isinstance(margin, (float, int)) and total > 0:
                if gap_unit == "pct_point":
                    margin_text = f"{float(margin):.2f}pp"
                elif gap_unit == "krw":
                    margin_text = f"{float(margin) / 1_000_000_000:.2f}B"
                else:
                    margin_text = f"{float(margin):.3f}"
                metrics.append(f"near_cutoff<={margin_text}:{count}/{total}")

    if not metrics:
        metrics.append(f"samples={int(insight.get('sample_count', 0) or 0)}")
    return f"{reason}({'; '.join(metrics)})"
