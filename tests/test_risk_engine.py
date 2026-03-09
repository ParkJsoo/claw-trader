"""RiskEngine 단위 테스트."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import fakeredis
import pytest

from domain.models import AccountSnapshot, Signal, SignalEntry, SignalStop
from executor.risk import MarketRiskConfig, RiskConfig, RiskEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signal(
    market="KR",
    symbol="005930",
    direction="LONG",
    size_cash=Decimal("100000"),
    price=Decimal("70000"),
) -> Signal:
    return Signal(
        signal_id="test-sig-001",
        ts="2026-03-09T09:00:00+09:00",
        market=market,
        symbol=symbol,
        direction=direction,
        entry=SignalEntry(price=price, size_cash=size_cash),
        stop=SignalStop(price=price * Decimal("0.98")),
    )


def _make_engine(r, available_cash=Decimal("5000000")) -> RiskEngine:
    snapshot = AccountSnapshot(
        equity=available_cash,
        cash=available_cash,
        available_cash=available_cash,
        currency="KRW",
    )
    client = MagicMock()
    client.get_account_snapshot.return_value = snapshot
    cfg = RiskConfig()
    return RiskEngine(redis=r, cfg=cfg, client=client)


# ---------------------------------------------------------------------------
# _is_truthy
# ---------------------------------------------------------------------------

class TestIsTruthy:
    def test_true_string(self):
        assert RiskEngine._is_truthy(b"true") is True

    def test_true_uppercase(self):
        assert RiskEngine._is_truthy(b"TRUE") is True

    def test_one(self):
        assert RiskEngine._is_truthy(b"1") is True

    def test_yes(self):
        assert RiskEngine._is_truthy(b"yes") is True

    def test_false_string(self):
        assert RiskEngine._is_truthy(b"false") is False

    def test_none(self):
        assert RiskEngine._is_truthy(None) is False

    def test_empty(self):
        assert RiskEngine._is_truthy(b"") is False


# ---------------------------------------------------------------------------
# Rule 0: global pause
# ---------------------------------------------------------------------------

class TestRule0GlobalPause:
    def test_paused_primary_key(self):
        r = fakeredis.FakeRedis()
        r.set("claw:pause:global", "true")
        eng = _make_engine(r)
        d = eng.check(_make_signal())
        assert d.allow is False
        assert d.reason == "PAUSED"

    def test_paused_compat_key(self):
        r = fakeredis.FakeRedis()
        r.set("trading:paused", "1")
        eng = _make_engine(r)
        d = eng.check(_make_signal())
        assert d.allow is False
        assert d.reason == "PAUSED"

    def test_not_paused(self):
        r = fakeredis.FakeRedis()
        eng = _make_engine(r)
        d = eng.check(_make_signal())
        assert d.allow is True


# ---------------------------------------------------------------------------
# Rule 1: duplicate position
# ---------------------------------------------------------------------------

class TestRule1DuplicatePosition:
    def test_no_position_allows(self):
        r = fakeredis.FakeRedis()
        eng = _make_engine(r)
        d = eng.check(_make_signal())
        assert d.allow is True

    def test_existing_position_blocks(self):
        r = fakeredis.FakeRedis()
        r.hset("position:KR:005930", "qty", "10")
        eng = _make_engine(r)
        d = eng.check(_make_signal(direction="LONG"))
        assert d.allow is False
        assert d.reason == "DUPLICATE_POSITION"

    def test_exit_direction_skips_dup_check(self):
        r = fakeredis.FakeRedis()
        r.hset("position:KR:005930", "qty", "10")
        eng = _make_engine(r)
        d = eng.check(_make_signal(direction="EXIT"))
        assert d.allow is True

    def test_corrupt_qty_blocks(self):
        r = fakeredis.FakeRedis()
        r.hset("position:KR:005930", "qty", "not_a_number")
        eng = _make_engine(r)
        d = eng.check(_make_signal())
        assert d.allow is False
        assert d.reason == "POSITION_DATA_CORRUPT"


# ---------------------------------------------------------------------------
# Rule 2: max concurrent positions
# ---------------------------------------------------------------------------

class TestRule2MaxConcurrent:
    def test_under_limit_allows(self):
        r = fakeredis.FakeRedis()
        r.sadd("position_index:KR", "005930", "000660")
        eng = _make_engine(r)
        d = eng.check(_make_signal(symbol="035420"))
        assert d.allow is True

    def test_at_limit_blocks(self):
        r = fakeredis.FakeRedis()
        for s in ["A", "B", "C", "D", "E"]:
            r.sadd("position_index:KR", s)
        eng = _make_engine(r)
        d = eng.check(_make_signal())
        assert d.allow is False
        assert d.reason == "MAX_CONCURRENT_POSITIONS"
        assert d.meta["limit"] == 5


# ---------------------------------------------------------------------------
# Rule 3: killswitch PnL
# ---------------------------------------------------------------------------

class TestRule3KillswitchPnl:
    def test_no_pnl_allows(self):
        r = fakeredis.FakeRedis()
        eng = _make_engine(r)
        d = eng.check(_make_signal())
        assert d.allow is True

    def test_above_limit_allows(self):
        r = fakeredis.FakeRedis()
        r.hset("pnl:KR", "realized_pnl", "-100000")
        eng = _make_engine(r)
        d = eng.check(_make_signal())
        assert d.allow is True

    def test_at_limit_triggers_killswitch(self):
        r = fakeredis.FakeRedis()
        r.hset("pnl:KR", "realized_pnl", "-500000")
        eng = _make_engine(r)
        d = eng.check(_make_signal())
        assert d.allow is False
        assert d.reason == "KILLSWITCH_REALIZED"
        # pause key가 설정되었는지 확인
        assert r.get("claw:pause:global") == b"true"

    def test_corrupt_pnl_blocks(self):
        r = fakeredis.FakeRedis()
        r.hset("pnl:KR", "realized_pnl", "bad_value")
        eng = _make_engine(r)
        d = eng.check(_make_signal())
        assert d.allow is False
        assert d.reason == "PNL_DATA_CORRUPT"


# ---------------------------------------------------------------------------
# Rule 4: allocation cap
# ---------------------------------------------------------------------------

class TestRule4AllocationCap:
    def test_within_cap_allows(self):
        r = fakeredis.FakeRedis()
        # available_cash=5_000_000, cap=20% → 1_000_000, size_cash=100_000
        eng = _make_engine(r, available_cash=Decimal("5000000"))
        d = eng.check(_make_signal(size_cash=Decimal("100000")))
        assert d.allow is True

    def test_exceeds_cap_blocks(self):
        r = fakeredis.FakeRedis()
        # available_cash=500_000, cap=20% → 100_000, size_cash=200_000
        eng = _make_engine(r, available_cash=Decimal("500000"))
        d = eng.check(_make_signal(size_cash=Decimal("200000")))
        assert d.allow is False
        assert d.reason == "ALLOCATION_CAP_EXCEEDED"

    def test_zero_cash_blocks(self):
        r = fakeredis.FakeRedis()
        eng = _make_engine(r, available_cash=Decimal("0"))
        d = eng.check(_make_signal())
        assert d.allow is False
        assert d.reason == "ACCOUNT_SNAPSHOT_ERROR"

    def test_client_exception_blocks(self):
        r = fakeredis.FakeRedis()
        client = MagicMock()
        client.get_account_snapshot.side_effect = RuntimeError("broker down")
        eng = RiskEngine(redis=r, cfg=RiskConfig(), client=client)
        d = eng.check(_make_signal())
        assert d.allow is False
        assert d.reason == "ACCOUNT_SNAPSHOT_ERROR"


# ---------------------------------------------------------------------------
# apply_killswitch NX 원자성
# ---------------------------------------------------------------------------

class TestApplyKillswitchNx:
    def test_nx_prevents_overwrite(self):
        r = fakeredis.FakeRedis()
        eng = _make_engine(r)
        eng.apply_killswitch("FIRST_REASON", {"market": "KR"})
        r.set("claw:pause:reason", "FIRST_REASON")  # already set
        eng.apply_killswitch("SECOND_REASON", {"market": "KR"})
        # NX → 두 번째 호출은 pause key 이미 있으므로 set_ok=False
        assert r.get("claw:pause:reason") == b"FIRST_REASON"
