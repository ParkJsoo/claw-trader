"""COIN shadow evaluation.

pause 상태에서도 COIN signal snapshot을 후행 mark_hist와 결합해
가상 청산 결과를 영구 ledger로 저장한다.
"""
from __future__ import annotations

import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from zoneinfo import ZoneInfo

from redis import Redis

from app.backtester import _parse_mark_hist
from app.coin_research import (
    compute_ledger_summary,
    get_pre_consensus_signal_snapshot,
    get_signal_snapshot,
    summarize_ledger_rows,
)
from app.position_exit_runner import (
    _COIN_EARLY_EXIT_PCT,
    _COIN_EARLY_EXIT_SEC,
    _COIN_STOP_LOSS_PCT,
    _COIN_TAKE_PROFIT_PCT,
    _COIN_TIME_LIMIT_MAX_SEC,
    _COIN_TIME_LIMIT_SEC,
    _COIN_TRAIL_STOP_PCT,
    _COIN_TRAIL_STOP_TIGHT_PCT,
    _COIN_TRAIL_TIGHT_TRIGGER,
    _check_exit,
)

_KST = ZoneInfo("Asia/Seoul")
_TTL = 180 * 86400
_SIGNAL_INDEX_KEY = "research:signal_index:COIN"
_PRE_SIGNAL_INDEX_KEY = "research:pre_signal_index:COIN"
_SHADOW_KEY = "research:shadow:COIN:{signal_id}"
_SHADOW_INDEX_KEY = "research:shadow_index:COIN"
_PRE_SHADOW_KEY = "research:pre_shadow:COIN:{signal_id}"
_PRE_SHADOW_INDEX_KEY = "research:pre_shadow_index:COIN"
_SHADOW_SCAN_LIMIT = int(os.getenv("COIN_SHADOW_SCAN_LIMIT", "200"))
_SHADOW_MIN_POINTS = int(os.getenv("COIN_SHADOW_MIN_POINTS", "10"))


def _decode(v) -> str:
    if isinstance(v, bytes):
        return v.decode()
    return str(v) if v is not None else ""


def _to_decimal(raw: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _to_ts_ms(value: str) -> int:
    if not value:
        return 0
    s = str(value).strip()
    if s.isdigit():
        if len(s) >= 13:
            return int(s[:13])
        return int(s) * 1000
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def _score_bounds(date_from: Optional[str], date_to: Optional[str]) -> tuple[int, int]:
    if not date_from:
        low = 0
    else:
        low = int(datetime.strptime(date_from, "%Y%m%d").replace(tzinfo=_KST).timestamp() * 1000)

    if not date_to:
        high = 32503680000000
    else:
        end = datetime.strptime(date_to, "%Y%m%d").replace(tzinfo=_KST)
        high = int((end.timestamp() + 86400) * 1000) - 1
    return low, high


def _shadow_exists(r: Redis, signal_id: str, *, key_pattern: str) -> bool:
    return bool(r.exists(key_pattern.format(signal_id=signal_id)))


def _normalize_exit_reason(reason: str) -> str:
    return reason.split("(", 1)[0] if reason else "unknown"


def _load_price_path(r: Redis, symbol: str, signal_ts_ms: int) -> list[tuple[int, Decimal]]:
    raw = r.lrange(f"mark_hist:COIN:{symbol}", 0, -1)
    prices = _parse_mark_hist(raw)
    return [(ts_ms, price) for ts_ms, price in prices if ts_ms >= signal_ts_ms]


def evaluate_signal_snapshot(
    r: Redis,
    snapshot: dict[str, str],
) -> dict[str, str] | None:
    """단일 signal snapshot을 shadow ledger row로 평가."""
    signal_id = str(snapshot.get("signal_id") or "").strip()
    symbol = str(snapshot.get("symbol") or "").strip()
    if not signal_id or not symbol:
        return None

    signal_ts_ms = _to_ts_ms(snapshot.get("ts_ms") or snapshot.get("ts") or "")
    if signal_ts_ms <= 0:
        return None

    entry_price = _to_decimal(snapshot.get("entry_price"), "0")
    size_cash = _to_decimal(snapshot.get("size_cash"), "0")
    stop_pct = _to_decimal(snapshot.get("stop_pct"), str(_COIN_STOP_LOSS_PCT))
    take_pct = _to_decimal(snapshot.get("take_pct"), str(_COIN_TAKE_PROFIT_PCT))
    if entry_price <= 0:
        return None

    prices = _load_price_path(r, symbol, signal_ts_ms)
    if len(prices) < _SHADOW_MIN_POINTS:
        return None

    hwm_price = entry_price
    exit_reason: str | None = None
    exit_detail = ""
    exit_ts_ms = 0
    exit_price = entry_price
    observed_ticks = 0

    for observed_ticks, (ts_ms, mark_price) in enumerate(prices, start=1):
        if mark_price > hwm_price:
            hwm_price = mark_price
        detail = _check_exit(
            entry_price,
            mark_price,
            signal_ts_ms,
            hwm_price=hwm_price,
            stop_pct=stop_pct,
            take_pct=take_pct,
            trail_pct=_COIN_TRAIL_STOP_PCT,
            time_limit_sec=_COIN_TIME_LIMIT_SEC,
            time_limit_max_sec=_COIN_TIME_LIMIT_MAX_SEC,
            early_exit_sec=_COIN_EARLY_EXIT_SEC,
            early_exit_pct=_COIN_EARLY_EXIT_PCT,
            trail_tight_pct=_COIN_TRAIL_STOP_TIGHT_PCT,
            trail_tight_trigger=_COIN_TRAIL_TIGHT_TRIGGER,
            stagnant_exit=True,
            now_ts_ms=ts_ms,
        )
        if detail:
            exit_reason = _normalize_exit_reason(detail)
            exit_detail = detail
            exit_ts_ms = ts_ms
            exit_price = mark_price
            break

    last_ts_ms, last_price = prices[-1]
    if exit_reason is None:
        if last_ts_ms < signal_ts_ms + (_COIN_TIME_LIMIT_MAX_SEC * 1000):
            return None
        exit_reason = "end_of_data"
        exit_detail = exit_reason
        exit_ts_ms = last_ts_ms
        exit_price = last_price
        observed_ticks = len(prices)

    pnl_pct = (exit_price - entry_price) / entry_price if entry_price > 0 else Decimal("0")
    pnl_cash = (size_cash * pnl_pct) if size_cash > 0 else Decimal("0")
    hold_sec = max((exit_ts_ms - signal_ts_ms) // 1000, 0)
    qty = (size_cash / entry_price) if (size_cash > 0 and entry_price > 0) else Decimal("0")

    row = {
        "trade_id": f"shadow:{signal_id}",
        "signal_id": signal_id,
        "date": datetime.fromtimestamp(signal_ts_ms / 1000, _KST).strftime("%Y%m%d"),
        "ts": str(exit_ts_ms),
        "signal_ts": snapshot.get("ts") or "",
        "signal_ts_ms": str(signal_ts_ms),
        "symbol": symbol,
        "order_id": "",
        "exec_id": "",
        "qty": str(qty),
        "price": str(exit_price),
        "fee": "0",
        "realized_pnl": str(pnl_cash.quantize(Decimal("0.01"))),
        "pnl_pct": str(pnl_pct.quantize(Decimal("0.00001"))),
        "source": "shadow",
        "exit_reason": exit_reason,
        "exit_detail": exit_detail,
        "hold_sec": str(hold_sec),
        "observed_ticks": str(observed_ticks),
        "evaluation_mode": "shadow",
        "entry_strategy": snapshot.get("strategy", ""),
        "entry_source": snapshot.get("source", ""),
        "signal_family": snapshot.get("signal_family", ""),
        "entry_ts": snapshot.get("ts", ""),
        "entry_price": snapshot.get("entry_price", ""),
        "entry_size_cash": snapshot.get("size_cash", ""),
        "entry_stop_pct": snapshot.get("stop_pct", ""),
        "entry_take_pct": snapshot.get("take_pct", ""),
        "entry_ret_5m": snapshot.get("ret_5m", ""),
        "entry_range_5m": snapshot.get("range_5m", ""),
        "entry_ret_1m": snapshot.get("ret_1m", ""),
        "entry_change_rate_daily": snapshot.get("change_rate_daily", ""),
        "entry_vol_24h": snapshot.get("vol_24h", ""),
        "entry_ob_ratio": snapshot.get("ob_ratio", ""),
        "entry_claude_conf": snapshot.get("claude_conf", ""),
        "entry_news_score": snapshot.get("news_score", ""),
        "reject_reason": snapshot.get("reject_reason", ""),
        "shadow_origin": snapshot.get("shadow_origin", ""),
        "shadow_stage": snapshot.get("shadow_stage", ""),
    }
    return {k: v for k, v in row.items() if v != ""}


def save_shadow_result(
    r: Redis,
    signal_id: str,
    row: dict[str, str],
    *,
    key_pattern: str = _SHADOW_KEY,
    index_key: str = _SHADOW_INDEX_KEY,
) -> None:
    key = key_pattern.format(signal_id=signal_id)
    signal_ts_ms = int(row.get("signal_ts_ms", "0") or 0)
    r.hset(key, mapping=row)
    r.expire(key, _TTL)
    r.zadd(index_key, {signal_id: signal_ts_ms})
    r.expire(index_key, _TTL)


def _evaluate_pending_signals(
    r: Redis,
    *,
    signal_index_key: str,
    shadow_key_pattern: str,
    shadow_index_key: str,
    snapshot_loader,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict[str, int]:
    """미평가 COIN signal snapshot을 shadow ledger로 적재."""
    low, high = _score_bounds(date_from, date_to)
    scan_limit = limit or _SHADOW_SCAN_LIMIT
    signal_ids = r.zrangebyscore(signal_index_key, low, high, start=0, num=scan_limit)

    stats = {
        "scanned": 0,
        "completed": 0,
        "pending": 0,
        "skipped_existing": 0,
        "skipped_invalid": 0,
    }

    for raw_signal_id in signal_ids:
        signal_id = _decode(raw_signal_id)
        stats["scanned"] += 1

        if _shadow_exists(r, signal_id, key_pattern=shadow_key_pattern):
            stats["skipped_existing"] += 1
            continue

        snapshot = snapshot_loader(r, signal_id)
        if not snapshot or snapshot.get("market") != "COIN":
            stats["skipped_invalid"] += 1
            continue

        row = evaluate_signal_snapshot(r, snapshot)
        if row is None:
            stats["pending"] += 1
            continue

        save_shadow_result(
            r,
            signal_id,
            row,
            key_pattern=shadow_key_pattern,
            index_key=shadow_index_key,
        )
        stats["completed"] += 1

    return stats


def evaluate_pending_signals(
    r: Redis,
    *,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict[str, int]:
    return _evaluate_pending_signals(
        r,
        signal_index_key=_SIGNAL_INDEX_KEY,
        shadow_key_pattern=_SHADOW_KEY,
        shadow_index_key=_SHADOW_INDEX_KEY,
        snapshot_loader=get_signal_snapshot,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
    )


def evaluate_pending_pre_consensus_signals(
    r: Redis,
    *,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict[str, int]:
    return _evaluate_pending_signals(
        r,
        signal_index_key=_PRE_SIGNAL_INDEX_KEY,
        shadow_key_pattern=_PRE_SHADOW_KEY,
        shadow_index_key=_PRE_SHADOW_INDEX_KEY,
        snapshot_loader=get_pre_consensus_signal_snapshot,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
    )


def _load_shadow_rows(
    r: Redis,
    *,
    index_key: str,
    row_key_pattern: str,
    snapshot_loader=None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> list[dict[str, str]]:
    low, high = _score_bounds(date_from, date_to)
    row_ids = r.zrangebyscore(index_key, low, high)

    rows: list[dict[str, str]] = []
    for raw_row_id in row_ids:
        row_id = _decode(raw_row_id)
        row = {_decode(k): _decode(v) for k, v in (r.hgetall(row_key_pattern.format(signal_id=row_id)) or {}).items()}
        if row and snapshot_loader is not None:
            snapshot = snapshot_loader(r, row_id)
            for field in ("reject_reason", "shadow_origin", "shadow_stage"):
                if not row.get(field) and snapshot.get(field):
                    row[field] = snapshot[field]
        if row:
            rows.append(row)
    return rows


def compute_shadow_summary(
    r: Redis,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict[str, Any]:
    return compute_ledger_summary(
        r,
        index_key=_SHADOW_INDEX_KEY,
        row_key_pattern="research:shadow:COIN:{row_id}",
        date_from=date_from,
        date_to=date_to,
    )


def compute_pre_consensus_shadow_summary(
    r: Redis,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict[str, Any]:
    rows = _load_shadow_rows(
        r,
        index_key=_PRE_SHADOW_INDEX_KEY,
        row_key_pattern=_PRE_SHADOW_KEY,
        snapshot_loader=get_pre_consensus_signal_snapshot,
        date_from=date_from,
        date_to=date_to,
    )
    return summarize_ledger_rows(rows, date_from=date_from, date_to=date_to)


def compute_combined_shadow_summary(
    r: Redis,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict[str, Any]:
    rows = _load_shadow_rows(
        r,
        index_key=_SHADOW_INDEX_KEY,
        row_key_pattern=_SHADOW_KEY,
        date_from=date_from,
        date_to=date_to,
    )
    rows.extend(
        _load_shadow_rows(
            r,
            index_key=_PRE_SHADOW_INDEX_KEY,
            row_key_pattern=_PRE_SHADOW_KEY,
            snapshot_loader=get_pre_consensus_signal_snapshot,
            date_from=date_from,
            date_to=date_to,
        )
    )
    return summarize_ledger_rows(rows, date_from=date_from, date_to=date_to)
