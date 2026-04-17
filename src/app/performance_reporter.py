"""performance_reporter — 성과 통계 계산 및 Redis 저장.

trade_index:{market}:{symbol} ZSET + trade:{market}:{trade_id} HASH를 집계하여
win rate, R:R, profit factor, max drawdown을 계산한다.

저장 키:
  perf:daily:{market}:{YYYYMMDD} — 일별 성과 HASH (TTL 90일)
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional
from zoneinfo import ZoneInfo

from redis import Redis

_KST = ZoneInfo("Asia/Seoul")
_PERF_TTL = 90 * 86400  # 90일


class PerformanceReporter:
    def __init__(self, r: Redis):
        self.r = r

    def _currency_label(self, market: str) -> str:
        return "원" if market in ("KR", "COIN") else "USD"

    def _currency_code(self, market: str) -> str:
        return "KRW" if market in ("KR", "COIN") else "USD"

    def _decode(self, v) -> str:
        if isinstance(v, bytes):
            return v.decode()
        return str(v) if v is not None else ""

    def _get_trade(self, market: str, trade_id: str) -> dict:
        raw = self.r.hgetall(f"trade:{market}:{trade_id}")
        if not raw:
            return {}
        return {self._decode(k): self._decode(v) for k, v in raw.items()}

    def _get_trade_symbols(self, market: str) -> list[str]:
        """trade_symbols SET + trade_index scan 결과를 합쳐 심볼 목록 반환."""
        symbols: set[str] = set()

        for raw_symbol in self.r.smembers(f"trade_symbols:{market}"):
            symbol = self._decode(raw_symbol)
            if symbol:
                symbols.add(symbol)

        prefix = f"trade_index:{market}:"
        for raw_key in self.r.scan_iter(match=f"{prefix}*"):
            key = self._decode(raw_key)
            if not key.startswith(prefix):
                continue
            symbol = key[len(prefix):]
            if symbol:
                symbols.add(symbol)

        return sorted(symbols)

    def _get_sell_trades_for_date(self, market: str, date_str: str) -> list[dict]:
        """해당 날짜(KST)의 SELL 체결 trade 목록 반환."""
        try:
            dt = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=_KST)
        except ValueError:
            return []

        day_start_ms = int(dt.timestamp() * 1000)
        day_end_ms = int((dt + timedelta(days=1)).timestamp() * 1000)

        symbols = self._get_trade_symbols(market)

        trades = []
        for symbol in symbols:
            key = f"trade_index:{market}:{symbol}"
            trade_ids = self.r.zrangebyscore(key, day_start_ms, day_end_ms)
            for tid_raw in trade_ids:
                tid = self._decode(tid_raw)
                t = self._get_trade(market, tid)
                if not t:
                    continue
                if t.get("side", "").upper() != "SELL":
                    continue
                try:
                    pnl = Decimal(t.get("realized_pnl") or "0")
                except InvalidOperation:
                    continue
                if pnl == 0:
                    continue
                t["_trade_id"] = tid
                t["_pnl"] = pnl
                trades.append(t)

        trades.sort(key=lambda x: int(x.get("ts") or "0"))
        return trades

    def compute_daily_stats(self, market: str, date_str: str) -> dict:
        """일별 성과 지표 계산."""
        trades = self._get_sell_trades_for_date(market, date_str)

        if not trades:
            return {
                "date": date_str,
                "market": market,
                "trade_count": 0,
                "win_count": 0,
                "loss_count": 0,
                "win_rate": 0.0,
                "gross_profit": "0",
                "gross_loss": "0",
                "net_pnl": "0",
                "profit_factor": 0.0,
                "avg_win": "0",
                "avg_loss": "0",
                "avg_rr": 0.0,
                "best_trade_pnl": "0",
                "best_trade_symbol": "",
                "worst_trade_pnl": "0",
                "worst_trade_symbol": "",
                "max_drawdown": "0",
            }

        wins = [t for t in trades if t["_pnl"] > 0]
        losses = [t for t in trades if t["_pnl"] < 0]

        gross_profit = sum((t["_pnl"] for t in wins), Decimal("0"))
        gross_loss = sum((abs(t["_pnl"]) for t in losses), Decimal("0"))
        net_pnl = gross_profit - gross_loss

        win_rate = len(wins) / len(trades) if trades else 0.0
        profit_factor = float(gross_profit / gross_loss) if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0

        avg_win = gross_profit / len(wins) if wins else Decimal("0")
        avg_loss = gross_loss / len(losses) if losses else Decimal("0")
        avg_rr = float(avg_win / avg_loss) if avg_loss > 0 else 0.0

        best = max(trades, key=lambda t: t["_pnl"])
        worst = min(trades, key=lambda t: t["_pnl"])

        # Max drawdown (누적 PnL 기준)
        cumulative = Decimal("0")
        peak = Decimal("0")
        max_dd = Decimal("0")
        for t in trades:
            cumulative += t["_pnl"]
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd

        return {
            "date": date_str,
            "market": market,
            "trade_count": len(trades),
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": round(win_rate * 100, 1),
            "gross_profit": str(gross_profit),
            "gross_loss": str(gross_loss),
            "net_pnl": str(net_pnl),
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else 999.0,
            "avg_win": str(avg_win.quantize(Decimal("1")) if market == "KR" else avg_win.quantize(Decimal("0.01"))),
            "avg_loss": str(avg_loss.quantize(Decimal("1")) if market == "KR" else avg_loss.quantize(Decimal("0.01"))),
            "avg_rr": round(avg_rr, 2),
            "best_trade_pnl": str(best["_pnl"]),
            "best_trade_symbol": best.get("symbol", ""),
            "worst_trade_pnl": str(worst["_pnl"]),
            "worst_trade_symbol": worst.get("symbol", ""),
            "max_drawdown": str(max_dd),
        }

    def save_daily_stats(self, market: str, date_str: str, stats: dict) -> None:
        key = f"perf:daily:{market}:{date_str}"
        payload = {k: str(v) for k, v in stats.items()}
        self.r.hset(key, mapping=payload)
        self.r.expire(key, _PERF_TTL)

    def get_daily_stats(self, market: str, date_str: str) -> dict:
        key = f"perf:daily:{market}:{date_str}"
        raw = self.r.hgetall(key)
        if not raw:
            return {}
        return {self._decode(k): self._decode(v) for k, v in raw.items()}

    def compute_and_save(self, market: str, date_str: str) -> dict:
        """계산 + 저장 한 번에."""
        stats = self.compute_daily_stats(market, date_str)
        self.save_daily_stats(market, date_str, stats)
        return stats

    def sync_realized_pnl(self, market: str, date_str: str) -> dict:
        """trade history 기준 일별 realized_pnl을 pnl:{market}에 재동기화."""
        stats = self.compute_and_save(market, date_str)
        key = f"pnl:{market}"
        raw = self.r.hgetall(key) or {}
        decoded = {self._decode(k): self._decode(v) for k, v in raw.items()}
        unrealized = decoded.get("unrealized_pnl", "0") or "0"
        now_ms = str(int(time.time() * 1000))
        self.r.hset(
            key,
            mapping={
                "realized_pnl": stats.get("net_pnl", "0"),
                "unrealized_pnl": unrealized,
                "currency": decoded.get("currency") or self._currency_code(market),
                "updated_ts": now_ms,
            },
        )
        return stats

    def format_report(self, market: str, stats: dict) -> str:
        """TG 발송용 리포트 문자열 생성."""
        currency = self._currency_label(market)
        date = stats.get("date", "")
        count = stats.get("trade_count", "0")
        win = stats.get("win_count", "0")
        loss = stats.get("loss_count", "0")
        win_rate = stats.get("win_rate", "0")
        net_pnl = stats.get("net_pnl", "0")
        pf = stats.get("profit_factor", "0")
        avg_rr = stats.get("avg_rr", "0")
        best_sym = stats.get("best_trade_symbol", "-")
        best_pnl = stats.get("best_trade_pnl", "0")
        worst_sym = stats.get("worst_trade_symbol", "-")
        worst_pnl = stats.get("worst_trade_pnl", "0")
        max_dd = stats.get("max_drawdown", "0")

        if str(count) == "0":
            return f"📊 [{market}] {date} — 체결 없음"

        sign = "+" if Decimal(net_pnl or "0") >= 0 else ""
        lines = [
            f"📊 [{market}] 일일 성과 리포트 {date}",
            f"━━━━━━━━━━━━━━━━━",
            f"체결: {count}건 (승 {win} / 패 {loss})",
            f"Win Rate: {win_rate}%",
            f"Net PnL: {sign}{net_pnl} {currency}",
            f"Profit Factor: {pf}",
            f"Avg R:R: {avg_rr}",
            f"Max Drawdown: {max_dd} {currency}",
            f"━━━━━━━━━━━━━━━━━",
            f"Best:  {best_sym} ({'+' if Decimal(best_pnl or '0') >= 0 else ''}{best_pnl} {currency})",
            f"Worst: {worst_sym} ({worst_pnl} {currency})",
        ]
        return "\n".join(lines)
