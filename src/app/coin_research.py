"""COIN 연구용 신호/체결 레저.

COIN 전략 분석을 위해:
  - 진입 시점 feature snapshot 저장
  - 청산 시점 realized PnL과 feature를 결합한 trade ledger 저장
  - 전략/exit reason/feature bucket별 요약 제공
"""
from __future__ import annotations

import json
import time
import os
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from zoneinfo import ZoneInfo

from redis import Redis

from domain.models import FillEvent, OrderSide

_KST = ZoneInfo("Asia/Seoul")

_TTL = 180 * 86400
_SIGNAL_KEY = "research:signal:COIN:{signal_id}"
_SIGNAL_INDEX_KEY = "research:signal_index:COIN"
_TRADE_KEY = "research:trade:COIN:{trade_id}"
_TRADE_INDEX_KEY = "research:trade_index:COIN"

_OVERALL_MIN_TRADES = int(os.getenv("COIN_RESUME_MIN_TRADES", "30"))
_OVERALL_MIN_PF = float(os.getenv("COIN_RESUME_MIN_PF", "1.10"))
_OVERALL_MIN_NET_PNL = float(os.getenv("COIN_RESUME_MIN_NET_PNL", "0"))
_OVERALL_MIN_AVG_PNL = float(os.getenv("COIN_RESUME_MIN_AVG_PNL", "0"))

_TYPE_B_MIN_TRADES = int(os.getenv("COIN_TYPE_B_MIN_TRADES", "20"))
_TYPE_B_MIN_PF = float(os.getenv("COIN_TYPE_B_MIN_PF", "1.15"))
_TYPE_B_MIN_NET_PNL = float(os.getenv("COIN_TYPE_B_MIN_NET_PNL", "0"))
_TYPE_B_MIN_AVG_PNL = float(os.getenv("COIN_TYPE_B_MIN_AVG_PNL", "0"))
_TYPE_B_MIN_WIN_RATE = float(os.getenv("COIN_TYPE_B_MIN_WIN_RATE", "25"))

_TYPE_A_MIN_TRADES = int(os.getenv("COIN_TYPE_A_MIN_TRADES", "20"))
_TYPE_A_MIN_PF = float(os.getenv("COIN_TYPE_A_MIN_PF", "1.10"))
_TYPE_A_MIN_NET_PNL = float(os.getenv("COIN_TYPE_A_MIN_NET_PNL", "0"))
_TYPE_A_MIN_AVG_PNL = float(os.getenv("COIN_TYPE_A_MIN_AVG_PNL", "0"))
_TYPE_A_MIN_WIN_RATE = float(os.getenv("COIN_TYPE_A_MIN_WIN_RATE", "25"))


def _decode(v) -> str:
    if isinstance(v, bytes):
        return v.decode()
    return str(v) if v is not None else ""


def _hgetall_str(r: Redis, key: str) -> dict[str, str]:
    raw = r.hgetall(key)
    if not raw:
        return {}
    return {_decode(k): _decode(v) for k, v in raw.items()}


def _to_ts_ms(value: str) -> int:
    if not value:
        return int(time.time() * 1000)
    s = str(value).strip()
    if s.isdigit():
        if len(s) >= 13:
            return int(s[:13])
        return int(s) * 1000
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return int(time.time() * 1000)


def _normalize_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float, Decimal)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _signal_family_from_payload(payload: dict[str, Any]) -> str:
    source = str(payload.get("source") or "")
    strategy = str(payload.get("strategy") or "")
    if "type_b" in source or strategy == "trend_riding":
        return "type_b"
    if strategy == "momentum_breakout":
        return "type_a"
    return "unknown"


def save_signal_snapshot(r: Redis, payload: dict[str, Any]) -> None:
    """COIN 진입 신호 feature snapshot 저장."""
    if payload.get("market") != "COIN":
        return

    signal_id = str(payload.get("signal_id") or "").strip()
    if not signal_id:
        return

    entry = payload.get("entry") or {}
    stop = payload.get("stop") or {}
    ts = str(payload.get("ts") or "")
    ts_ms = _to_ts_ms(ts)

    mapping = {
        "signal_id": signal_id,
        "market": "COIN",
        "symbol": _normalize_scalar(payload.get("symbol")),
        "ts": ts,
        "ts_ms": str(ts_ms),
        "date": datetime.fromtimestamp(ts_ms / 1000, _KST).strftime("%Y%m%d"),
        "direction": _normalize_scalar(payload.get("direction")),
        "source": _normalize_scalar(payload.get("source")),
        "strategy": _normalize_scalar(payload.get("strategy")),
        "signal_family": _signal_family_from_payload(payload),
        "status": _normalize_scalar(payload.get("status")),
        "entry_price": _normalize_scalar(entry.get("price")),
        "size_cash": _normalize_scalar(entry.get("size_cash")),
        "stop_price": _normalize_scalar(stop.get("price")),
        "stop_pct": _normalize_scalar(payload.get("stop_pct")),
        "take_pct": _normalize_scalar(payload.get("take_pct")),
        "claude_conf": _normalize_scalar(payload.get("claude_conf")),
        "ret_5m": _normalize_scalar(payload.get("ret_5m")),
        "range_5m": _normalize_scalar(payload.get("range_5m")),
        "ret_1m": _normalize_scalar(payload.get("ret_1m")),
        "change_rate_daily": _normalize_scalar(payload.get("change_rate_daily")),
        "vol_24h": _normalize_scalar(payload.get("vol_24h")),
        "ob_ratio": _normalize_scalar(payload.get("ob_ratio")),
        "news_score": _normalize_scalar(payload.get("news_score")),
    }

    key = _SIGNAL_KEY.format(signal_id=signal_id)
    filtered = {k: v for k, v in mapping.items() if v != ""}
    r.hset(key, mapping=filtered)
    r.expire(key, _TTL)
    r.zadd(_SIGNAL_INDEX_KEY, {signal_id: ts_ms})
    r.expire(_SIGNAL_INDEX_KEY, _TTL)


def get_signal_snapshot(r: Redis, signal_id: str) -> dict[str, str]:
    """research snapshot 조회. 없으면 기존 consensus audit를 fallback 사용."""
    if not signal_id:
        return {}

    key = _SIGNAL_KEY.format(signal_id=signal_id)
    data = _hgetall_str(r, key)
    if data:
        return data

    raw = r.get(f"consensus:audit:COIN:{signal_id}")
    if not raw:
        return {}
    try:
        parsed = json.loads(_decode(raw))
        return {k: _normalize_scalar(v) for k, v in parsed.items() if v is not None}
    except Exception:
        return {}


def bind_position_signal_context(r: Redis, symbol: str, signal_id: str) -> None:
    """COIN 포지션 해시에 진입 신호 context를 보존."""
    if not symbol or not signal_id:
        return

    snapshot = get_signal_snapshot(r, signal_id)
    key = f"position:COIN:{symbol}"
    if not r.exists(key):
        return

    mapping = {"signal_id": signal_id}
    field_map = {
        "strategy": "entry_strategy",
        "source": "entry_source",
        "signal_family": "entry_signal_family",
        "ts": "entry_ts",
        "entry_price": "entry_price",
        "size_cash": "entry_size_cash",
        "stop_pct": "entry_stop_pct",
        "take_pct": "entry_take_pct",
        "ret_5m": "entry_ret_5m",
        "range_5m": "entry_range_5m",
        "ret_1m": "entry_ret_1m",
        "change_rate_daily": "entry_change_rate_daily",
        "vol_24h": "entry_vol_24h",
        "ob_ratio": "entry_ob_ratio",
        "claude_conf": "entry_claude_conf",
        "news_score": "entry_news_score",
    }
    for src, dst in field_map.items():
        val = snapshot.get(src, "")
        if val:
            mapping[dst] = val

    r.hset(key, mapping=mapping)
    r.expire(key, 7 * 86400)


def hydrate_fill_signal_id(
    r: Redis,
    fill: FillEvent,
    position_ctx: Optional[dict[str, str]] = None,
) -> str:
    """SELL fill에 signal_id가 빠져 있으면 포지션/주문 메타에서 보완."""
    if fill.market != "COIN":
        return fill.signal_id or ""
    if fill.signal_id:
        return fill.signal_id

    ctx = position_ctx or _hgetall_str(r, f"position:COIN:{fill.symbol}")
    signal_id = ctx.get("signal_id", "")

    if not signal_id and fill.order_id:
        signal_id = _hgetall_str(r, f"claw:order_meta:COIN:{fill.order_id}").get("signal_id", "")

    if signal_id:
        fill.signal_id = signal_id
    return signal_id


def record_closed_trade(
    r: Redis,
    fill: FillEvent,
    realized_pnl: Decimal,
    position_ctx: Optional[dict[str, str]] = None,
) -> None:
    """COIN SELL fill을 연구용 ledger로 저장."""
    if fill.market != "COIN" or fill.side != OrderSide.SELL:
        return

    ctx = position_ctx or _hgetall_str(r, f"position:COIN:{fill.symbol}")
    signal_id = fill.signal_id or ctx.get("signal_id", "")
    if not signal_id:
        return

    snapshot = get_signal_snapshot(r, signal_id)
    trade_id = fill.exec_id or fill.trade_id()
    key = _TRADE_KEY.format(trade_id=trade_id)
    ts_ms = _to_ts_ms(fill.ts)
    order_meta = _hgetall_str(r, f"claw:order_meta:COIN:{fill.order_id}") if fill.order_id else {}

    hold_sec = ""
    opened_ts = ctx.get("opened_ts", "")
    if opened_ts:
        try:
            opened_ms = _to_ts_ms(opened_ts)
            hold_sec = str(max((ts_ms - opened_ms) // 1000, 0))
        except Exception:
            hold_sec = ""

    mapping = {
        "trade_id": trade_id,
        "signal_id": signal_id,
        "date": datetime.fromtimestamp(ts_ms / 1000, _KST).strftime("%Y%m%d"),
        "ts": str(ts_ms),
        "symbol": fill.symbol,
        "order_id": fill.order_id or "",
        "exec_id": fill.exec_id or "",
        "qty": str(fill.qty),
        "price": str(fill.price),
        "fee": str(fill.fee),
        "realized_pnl": str(realized_pnl),
        "source": fill.source or "",
        "exit_reason": order_meta.get("exit_reason", ""),
        "hold_sec": hold_sec,
        "entry_strategy": snapshot.get("strategy") or ctx.get("entry_strategy", ""),
        "entry_source": snapshot.get("source") or ctx.get("entry_source", ""),
        "signal_family": snapshot.get("signal_family") or ctx.get("entry_signal_family", ""),
        "entry_ts": snapshot.get("ts") or ctx.get("entry_ts", ""),
        "entry_price": snapshot.get("entry_price") or ctx.get("entry_price", "") or ctx.get("avg_price", ""),
        "entry_size_cash": snapshot.get("size_cash") or ctx.get("entry_size_cash", ""),
        "entry_stop_pct": snapshot.get("stop_pct") or ctx.get("entry_stop_pct", ""),
        "entry_take_pct": snapshot.get("take_pct") or ctx.get("entry_take_pct", ""),
        "entry_ret_5m": snapshot.get("ret_5m") or ctx.get("entry_ret_5m", ""),
        "entry_range_5m": snapshot.get("range_5m") or ctx.get("entry_range_5m", ""),
        "entry_ret_1m": snapshot.get("ret_1m") or ctx.get("entry_ret_1m", ""),
        "entry_change_rate_daily": snapshot.get("change_rate_daily") or ctx.get("entry_change_rate_daily", ""),
        "entry_vol_24h": snapshot.get("vol_24h") or ctx.get("entry_vol_24h", ""),
        "entry_ob_ratio": snapshot.get("ob_ratio") or ctx.get("entry_ob_ratio", ""),
        "entry_claude_conf": snapshot.get("claude_conf") or ctx.get("entry_claude_conf", ""),
        "entry_news_score": snapshot.get("news_score") or ctx.get("entry_news_score", ""),
    }

    filtered = {k: v for k, v in mapping.items() if v != ""}
    r.hset(key, mapping=filtered)
    r.expire(key, _TTL)
    r.zadd(_TRADE_INDEX_KEY, {trade_id: ts_ms})
    r.expire(_TRADE_INDEX_KEY, _TTL)


def _score_bounds(date_from: Optional[str], date_to: Optional[str]) -> tuple[int, int]:
    if not date_from:
        low = 0
    else:
        low = int(datetime.strptime(date_from, "%Y%m%d").replace(tzinfo=_KST).timestamp() * 1000)

    if not date_to:
        high = int(time.time() * 1000)
    else:
        end = datetime.strptime(date_to, "%Y%m%d").replace(tzinfo=_KST) + timedelta(days=1)
        high = int(end.timestamp() * 1000) - 1
    return low, high


def compute_ledger_summary(
    r: Redis,
    *,
    index_key: str,
    row_key_pattern: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict[str, Any]:
    """공통 research ledger 요약."""
    low, high = _score_bounds(date_from, date_to)
    row_ids = r.zrangebyscore(index_key, low, high)

    rows: list[dict[str, str]] = []
    for raw_row_id in row_ids:
        row_id = _decode(raw_row_id)
        row = _hgetall_str(r, row_key_pattern.format(row_id=row_id))
        if row:
            rows.append(row)

    by_signal_family: dict[str, list[dict[str, str]]] = defaultdict(list)
    by_strategy: dict[str, list[dict[str, str]]] = defaultdict(list)
    by_exit_reason: dict[str, list[dict[str, str]]] = defaultdict(list)
    by_change_rate_bucket: dict[str, list[dict[str, str]]] = defaultdict(list)
    by_ret_5m_bucket: dict[str, list[dict[str, str]]] = defaultdict(list)

    for row in rows:
        by_signal_family[row.get("signal_family", "") or "unknown"].append(row)
        by_strategy[row.get("entry_strategy", "") or "unknown"].append(row)
        by_exit_reason[row.get("exit_reason", "") or "unknown"].append(row)
        by_change_rate_bucket[_bucket_change_rate(row.get("entry_change_rate_daily", ""))].append(row)
        by_ret_5m_bucket[_bucket_ret_5m(row.get("entry_ret_5m", ""))].append(row)

    return {
        "date_from": date_from or "",
        "date_to": date_to or "",
        "overall": _summarize_rows(rows),
        "by_signal_family": {k: _summarize_rows(v) for k, v in sorted(by_signal_family.items())},
        "by_strategy": {k: _summarize_rows(v) for k, v in sorted(by_strategy.items())},
        "by_exit_reason": {k: _summarize_rows(v) for k, v in sorted(by_exit_reason.items())},
        "by_change_rate_bucket": {k: _summarize_rows(v) for k, v in sorted(by_change_rate_bucket.items())},
        "by_ret_5m_bucket": {k: _summarize_rows(v) for k, v in sorted(by_ret_5m_bucket.items())},
    }


def _bucket_change_rate(raw: str) -> str:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return "unknown"
    if val < 0.07:
        return "<7%"
    if val < 0.10:
        return "7-10%"
    if val < 0.15:
        return "10-15%"
    return ">=15%"


def _bucket_ret_5m(raw: str) -> str:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return "unknown"
    if val < 0.01:
        return "<1%"
    if val < 0.02:
        return "1-2%"
    return ">=2%"


def _summarize_rows(rows: list[dict[str, str]]) -> dict[str, Any]:
    total = len(rows)
    if total == 0:
        return {
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
            "win_rate": 0.0,
            "net_pnl": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "profit_factor": 0.0,
            "avg_pnl": 0.0,
            "avg_hold_sec": 0.0,
            "exit_reasons": {},
        }

    pnls: list[Decimal] = []
    hold_secs: list[int] = []
    exit_reasons: dict[str, int] = {}
    for row in rows:
        try:
            pnl = Decimal(row.get("realized_pnl", "0"))
        except InvalidOperation:
            pnl = Decimal("0")
        pnls.append(pnl)
        try:
            hold_secs.append(int(row.get("hold_sec", "0") or 0))
        except ValueError:
            pass
        exit_reason = row.get("exit_reason", "") or "unknown"
        exit_reasons[exit_reason] = exit_reasons.get(exit_reason, 0) + 1

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins, Decimal("0"))
    gross_loss = sum((-p for p in losses), Decimal("0"))
    net = sum(pnls, Decimal("0"))
    avg_pnl = net / Decimal(total)
    profit_factor = float(gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)

    return {
        "trade_count": total,
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": round(len(wins) / total * 100, 1),
        "net_pnl": round(float(net), 2),
        "gross_profit": round(float(gross_profit), 2),
        "gross_loss": round(float(gross_loss), 2),
        "profit_factor": round(profit_factor, 2),
        "avg_pnl": round(float(avg_pnl), 2),
        "avg_hold_sec": round(sum(hold_secs) / len(hold_secs), 1) if hold_secs else 0.0,
        "exit_reasons": dict(sorted(exit_reasons.items())),
    }


def compute_trade_summary(
    r: Redis,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict[str, Any]:
    """COIN research ledger 요약."""
    return compute_ledger_summary(
        r,
        index_key=_TRADE_INDEX_KEY,
        row_key_pattern="research:trade:COIN:{row_id}",
        date_from=date_from,
        date_to=date_to,
    )


def choose_resume_summary(
    trade_summary: dict[str, Any],
    shadow_summary: dict[str, Any],
    ledger: str = "auto",
) -> dict[str, Any]:
    """resume 판단에 사용할 evidence 선택."""
    trade_count = int((trade_summary.get("overall", {}) or {}).get("trade_count", 0) or 0)
    shadow_count = int((shadow_summary.get("overall", {}) or {}).get("trade_count", 0) or 0)

    if ledger == "trade":
        selected = "trade"
    elif ledger == "shadow":
        selected = "shadow"
    else:
        selected = "trade" if trade_count > 0 else "shadow"

    return {
        "selected_ledger": selected,
        "selected_trade_count": trade_count if selected == "trade" else shadow_count,
        "trade_count": trade_count,
        "shadow_count": shadow_count,
        "summary": trade_summary if selected == "trade" else shadow_summary,
    }


def _evaluate_bucket(
    name: str,
    stats: dict[str, Any],
    *,
    min_trades: int,
    min_profit_factor: float,
    min_net_pnl: float,
    min_avg_pnl: float,
    min_win_rate: float,
) -> dict[str, Any]:
    checks = {
        "min_trades": {
            "actual": int(stats.get("trade_count", 0) or 0),
            "required": min_trades,
        },
        "profit_factor": {
            "actual": float(stats.get("profit_factor", 0.0) or 0.0),
            "required": min_profit_factor,
        },
        "net_pnl": {
            "actual": float(stats.get("net_pnl", 0.0) or 0.0),
            "required": min_net_pnl,
        },
        "avg_pnl": {
            "actual": float(stats.get("avg_pnl", 0.0) or 0.0),
            "required": min_avg_pnl,
        },
        "win_rate": {
            "actual": float(stats.get("win_rate", 0.0) or 0.0),
            "required": min_win_rate,
        },
    }
    checks["min_trades"]["pass"] = checks["min_trades"]["actual"] >= checks["min_trades"]["required"]
    checks["profit_factor"]["pass"] = checks["profit_factor"]["actual"] >= checks["profit_factor"]["required"]
    checks["net_pnl"]["pass"] = checks["net_pnl"]["actual"] > checks["net_pnl"]["required"]
    checks["avg_pnl"]["pass"] = checks["avg_pnl"]["actual"] > checks["avg_pnl"]["required"]
    checks["win_rate"]["pass"] = checks["win_rate"]["actual"] >= checks["win_rate"]["required"]

    blockers = [key for key, meta in checks.items() if not meta["pass"]]
    return {
        "name": name,
        "ready": not blockers,
        "blockers": blockers,
        "checks": checks,
        "stats": stats,
    }


def evaluate_resume_readiness(summary: dict[str, Any]) -> dict[str, Any]:
    """COIN 재개 가능성 평가.

    기본 정책:
      - COIN은 기본적으로 keep_paused
      - overall + type_b가 기준 통과 시에만 type_b_only 재개 후보
      - type_a는 별도 증명 전까지 canary/shadow 유지
    """
    overall = summary.get("overall", {}) or {}
    by_signal_family = summary.get("by_signal_family", {}) or {}

    overall_eval = _evaluate_bucket(
        "overall",
        overall,
        min_trades=_OVERALL_MIN_TRADES,
        min_profit_factor=_OVERALL_MIN_PF,
        min_net_pnl=_OVERALL_MIN_NET_PNL,
        min_avg_pnl=_OVERALL_MIN_AVG_PNL,
        min_win_rate=0.0,
    )
    type_b_eval = _evaluate_bucket(
        "type_b",
        by_signal_family.get("type_b", {}) or {},
        min_trades=_TYPE_B_MIN_TRADES,
        min_profit_factor=_TYPE_B_MIN_PF,
        min_net_pnl=_TYPE_B_MIN_NET_PNL,
        min_avg_pnl=_TYPE_B_MIN_AVG_PNL,
        min_win_rate=_TYPE_B_MIN_WIN_RATE,
    )
    type_a_eval = _evaluate_bucket(
        "type_a",
        by_signal_family.get("type_a", {}) or {},
        min_trades=_TYPE_A_MIN_TRADES,
        min_profit_factor=_TYPE_A_MIN_PF,
        min_net_pnl=_TYPE_A_MIN_NET_PNL,
        min_avg_pnl=_TYPE_A_MIN_AVG_PNL,
        min_win_rate=_TYPE_A_MIN_WIN_RATE,
    )

    recommendation = "keep_paused"
    rationale: list[str] = []
    if not overall_eval["ready"]:
        rationale.append("overall sample/edge is not proven yet")
    if not type_b_eval["ready"]:
        rationale.append("type_b edge is not proven yet")

    if overall_eval["ready"] and type_b_eval["ready"]:
        if type_a_eval["ready"]:
            recommendation = "resume_candidate_type_b_only"
            rationale.append("type_b is proven; type_a is also positive but should remain canary first")
        else:
            recommendation = "resume_candidate_type_b_only"
            rationale.append("type_b is proven; type_a remains unproven and should stay paused/shadow")
    else:
        recommendation = "keep_paused"

    return {
        "recommendation": recommendation,
        "ready_to_resume": recommendation != "keep_paused",
        "policy": {
            "default_mode": "keep_paused",
            "resume_mode": "type_b_only",
            "type_a_mode": "shadow_until_proven",
        },
        "rationale": rationale,
        "evaluations": {
            "overall": overall_eval,
            "type_b": type_b_eval,
            "type_a": type_a_eval,
        },
    }
