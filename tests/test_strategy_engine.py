"""StrategyEngine 단위 테스트."""
from __future__ import annotations

import time
from decimal import Decimal

import fakeredis
import pytest

from domain.models import Signal, SignalEntry, SignalStop
from strategy.engine import StrategyConfig, StrategyEngine, MarketStrategyConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signal(
    signal_id="sig-001",
    market="KR",
    symbol="005930",
    direction="LONG",
) -> Signal:
    return Signal(
        signal_id=signal_id,
        ts="2026-03-09T09:00:00+09:00",
        market=market,
        symbol=symbol,
        direction=direction,
        entry=SignalEntry(price=Decimal("70000"), size_cash=Decimal("100000")),
        stop=SignalStop(price=Decimal("68600")),
    )


def _make_engine(r, **cfg_overrides) -> StrategyEngine:
    cfg = StrategyConfig(
        kr=MarketStrategyConfig(**cfg_overrides),
        us=MarketStrategyConfig(**cfg_overrides),
    )
    return StrategyEngine(redis=r, cfg=cfg)


# ---------------------------------------------------------------------------
# Rule: dedupe
# ---------------------------------------------------------------------------

class TestRuleDedupe:
    def test_first_signal_passes(self):
        r = fakeredis.FakeRedis()
        eng = _make_engine(r)
        d = eng.check(_make_signal())
        assert d.allow is True

    def test_duplicate_signal_blocked(self):
        r = fakeredis.FakeRedis()
        eng = _make_engine(r)
        eng.check(_make_signal(signal_id="dup-001"))
        d = eng.check(_make_signal(signal_id="dup-001"))
        assert d.allow is False
        assert d.reason == "DUP_SIGNAL"

    def test_different_signal_ids_pass(self):
        r = fakeredis.FakeRedis()
        eng = _make_engine(r)
        d1 = eng.check(_make_signal(signal_id="sig-A", symbol="005930"))
        d2 = eng.check(_make_signal(signal_id="sig-B", symbol="000660"))  # 다른 symbol → cooldown 없음
        assert d1.allow is True
        assert d2.allow is True

    def test_different_markets_independent(self):
        r = fakeredis.FakeRedis()
        eng = _make_engine(r)
        eng.check(_make_signal(signal_id="sig-001", market="KR"))
        # 동일 signal_id지만 다른 market → 별도 dedupe 키
        d = eng.check(_make_signal(signal_id="sig-001", market="US"))
        assert d.allow is True


# ---------------------------------------------------------------------------
# Rule: cooldown
# ---------------------------------------------------------------------------

class TestRuleCooldown:
    def test_no_cooldown_passes(self):
        r = fakeredis.FakeRedis()
        eng = _make_engine(r, cooldown_sec=300)
        d = eng.check(_make_signal())
        assert d.allow is True

    def test_within_cooldown_blocked(self):
        r = fakeredis.FakeRedis()
        eng = _make_engine(r, cooldown_sec=300)
        eng.check(_make_signal(signal_id="sig-A"))
        # 다른 signal_id, 같은 symbol → cooldown 적용
        d = eng.check(_make_signal(signal_id="sig-B"))
        assert d.allow is False
        assert d.reason == "COOLDOWN"
        assert d.meta["remaining_sec"] > 0

    def test_after_cooldown_passes(self):
        r = fakeredis.FakeRedis()
        eng = _make_engine(r, cooldown_sec=1)
        eng.check(_make_signal(signal_id="sig-A"))
        time.sleep(1.1)
        d = eng.check(_make_signal(signal_id="sig-B"))
        assert d.allow is True

    def test_corrupt_cooldown_value_passes(self):
        r = fakeredis.FakeRedis()
        r.set("strategy:cooldown:KR:005930", "not_a_number")
        eng = _make_engine(r, cooldown_sec=300)
        d = eng.check(_make_signal())
        # corrupt 값 → 방어적 통과
        assert d.allow is True


# ---------------------------------------------------------------------------
# Rule: daily cap
# ---------------------------------------------------------------------------

class TestRuleDailyCap:
    def test_under_cap_passes(self):
        r = fakeredis.FakeRedis()
        symbols = ["005930", "000660", "035420", "005380", "051910"]
        eng = _make_engine(r, daily_cap=5)
        for i, sym in enumerate(symbols):
            d = eng.check(_make_signal(signal_id=f"sig-{i}", symbol=sym))
            assert d.allow is True

    def test_over_cap_blocked(self):
        r = fakeredis.FakeRedis()
        eng = _make_engine(r, daily_cap=2)
        eng.check(_make_signal(signal_id="sig-A", symbol="005930"))
        eng.check(_make_signal(signal_id="sig-B", symbol="000660"))
        d = eng.check(_make_signal(signal_id="sig-C", symbol="035420"))
        assert d.allow is False
        assert d.reason == "DAILY_CAP"
        assert d.meta["cap"] == 2

    def test_cap_counts_all_signals_including_blocked(self):
        """daily_count는 reject 포함해서 증가함"""
        r = fakeredis.FakeRedis()
        eng = _make_engine(r, daily_cap=1)
        eng.check(_make_signal(signal_id="sig-A", symbol="005930"))  # pass (count=1)
        d = eng.check(_make_signal(signal_id="sig-B", symbol="000660"))  # count=2 → blocked
        assert d.allow is False
        assert d.reason == "DAILY_CAP"


# ---------------------------------------------------------------------------
# Full flow: STRATEGY_OK
# ---------------------------------------------------------------------------

class TestStrategyOk:
    def test_fresh_signal_strategy_ok(self):
        r = fakeredis.FakeRedis()
        eng = _make_engine(r)
        d = eng.check(_make_signal())
        assert d.allow is True
        assert d.reason == "STRATEGY_OK"

    def test_cooldown_set_after_pass(self):
        r = fakeredis.FakeRedis()
        eng = _make_engine(r, cooldown_sec=300)
        eng.check(_make_signal(signal_id="sig-A"))
        # cooldown key가 설정되어 있어야 함
        key = r.get("strategy:cooldown:KR:005930")
        assert key is not None
        ts = int(key.decode())
        assert abs(ts - int(time.time() * 1000)) < 2000

    def test_pass_count_incremented(self):
        r = fakeredis.FakeRedis()
        eng = _make_engine(r)
        eng.check(_make_signal(signal_id="sig-A"))
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
        cnt = r.get(f"strategy:pass_count:KR:{today}")
        assert cnt == b"1"

    def test_reject_count_incremented(self):
        r = fakeredis.FakeRedis()
        eng = _make_engine(r)
        eng.check(_make_signal(signal_id="dup"))
        eng.check(_make_signal(signal_id="dup"))  # DUP_SIGNAL
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
        cnt = r.hget(f"strategy:reject_count:KR:{today}", "DUP_SIGNAL")
        assert cnt == b"1"
