"""
Position Engine — Fill 큐 소비, 포지션 갱신.
독립 프로세스로 실행 시 claw:fill:queue의 Fill 이벤트를 처리.
예외 시 DLQ/Retry로 fill 유실 방지.
"""
from __future__ import annotations

import os
import time
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
import redis

from guards.notifier import send_telegram
from portfolio.engine import PositionEngine
from portfolio.redis_repo import RedisPositionRepository
from utils.redis_helpers import get_config

_LUA_UPDATE_STREAK = """
local streak = tonumber(redis.call('GET', KEYS[1]) or '0')
local pnl_sign = tonumber(ARGV[1])
if pnl_sign > 0 then
    if streak > 0 then
        streak = streak + 1
    else
        streak = 1
    end
elseif pnl_sign < 0 then
    if streak < 0 then
        streak = streak - 1
    else
        streak = -1
    end
end
redis.call('SET', KEYS[1], tostring(streak))
return streak
"""

MAX_RETRY = 5
STATUS_LOG_INTERVAL_SEC = 30
FILL_QUEUE_KEY = "claw:fill:queue"
FILL_DLQ_KEY = "claw:fill:dlq"

_DEFAULT_SIZE_CASH_PCT = 0.20
_STREAK_WIN_THRESHOLD = 3
_STREAK_LOSS_THRESHOLD = -3


def _update_streak(r, market: str, pnl: Decimal) -> None:
    """SELL fill 후 연속 수익/손실 streak 추적 및 size_cash_pct 자동 조정."""
    streak_key = f"claw:streak:{market}"

    if pnl == 0:
        return  # pnl == 0: streak 변경 없음

    pnl_sign = 1 if float(pnl) > 0 else (-1 if float(pnl) < 0 else 0)
    streak = int(r.eval(_LUA_UPDATE_STREAK, 1, streak_key, pnl_sign))

    config_key = f"claw:config:{market}"
    current_pct = get_config(r, market, "size_cash_pct", _DEFAULT_SIZE_CASH_PCT)

    if streak >= _STREAK_WIN_THRESHOLD:
        new_pct = min(current_pct + 0.05, 0.50)
        r.hset(config_key, "size_cash_pct", str(new_pct))
        try:
            send_telegram(
                f"[CLAW] 📈 3연속 수익 → size_cash_pct {new_pct:.0%}로 상향"
            )
        except Exception:
            pass
        print(
            f"[position_engine] streak={streak} {market} size_cash_pct "
            f"{current_pct:.0%} → {new_pct:.0%}",
            flush=True,
        )
    elif streak <= _STREAK_LOSS_THRESHOLD:
        new_pct = max(current_pct - 0.10, 0.10)
        r.hset(config_key, "size_cash_pct", str(new_pct))
        try:
            send_telegram(
                f"[CLAW] 📉 3연속 손실 → size_cash_pct {new_pct:.0%}로 하향"
            )
        except Exception:
            pass
        print(
            f"[position_engine] streak={streak} {market} size_cash_pct "
            f"{current_pct:.0%} → {new_pct:.0%}",
            flush=True,
        )


def main():
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=True)
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    r = redis.from_url(redis_url)
    repo = RedisPositionRepository(r)
    engine = PositionEngine(repo)

    processed = 0
    requeued = 0
    dlqed = 0
    idle_count = 0
    last_status_log = 0.0

    print("[position_engine] started, consuming claw:fill:queue")
    while True:
        fill = repo.pop_fill(timeout=5)
        now = time.time()

        if fill is None:
            idle_count += 1
            if now - last_status_log >= STATUS_LOG_INTERVAL_SEC:
                queue_len = r.llen(FILL_QUEUE_KEY)
                dlq_len = r.llen(FILL_DLQ_KEY)
                print(
                    f"[position_engine] status processed={processed} "
                    f"requeued={requeued} dlqed={dlqed} idle={idle_count} "
                    f"queue={queue_len} dlq={dlq_len}"
                )
                last_status_log = now
            continue

        try:
            # SELL fill 처리 전 PnL 계산용 포지션 스냅샷
            fill_side = fill.side.value if hasattr(fill.side, "value") else str(fill.side)
            pre_pos = None
            if fill_side == "SELL":
                pre_pos = repo.get_position(fill.market, fill.symbol)

            engine.apply_fill(fill)
            processed += 1
            print(
                f"[position_engine] applied fill: {fill.symbol} "
                f"{fill.side.value} {fill.qty}@{fill.price}"
            )

            # trade_symbols SET 갱신: 거래 이력 추적 (TTL 없음, 영구)
            r.sadd(f"trade_symbols:{fill.market}", fill.symbol)

            # SELL 후 streak 업데이트
            if fill_side == "SELL" and pre_pos is not None and pre_pos.qty > 0:
                fee = getattr(fill, "fee", Decimal("0"))
                sell_qty = min(fill.qty, pre_pos.qty)
                realized_delta = (fill.price - pre_pos.avg_price) * sell_qty - fee
                _update_streak(r, fill.market, realized_delta)
        except Exception as e:
            if fill.retry >= MAX_RETRY:
                repo.push_fill_dlq(fill, reason=str(e))
                dlqed += 1
                print(
                    f"[position_engine] DLQ: {fill.symbol} "
                    f"retry={fill.retry} err={e}"
                )
            else:
                repo.requeue_fill(fill)
                requeued += 1
                print(
                    f"[position_engine] requeue: {fill.symbol} "
                    f"retry={fill.retry} err={e}"
                )

        if now - last_status_log >= STATUS_LOG_INTERVAL_SEC:
            queue_len = r.llen(FILL_QUEUE_KEY)
            dlq_len = r.llen(FILL_DLQ_KEY)
            print(
                f"[position_engine] status processed={processed} "
                f"requeued={requeued} dlqed={dlqed} idle={idle_count} "
                f"queue={queue_len} dlq={dlq_len}"
            )
            last_status_log = now


if __name__ == "__main__":
    main()
