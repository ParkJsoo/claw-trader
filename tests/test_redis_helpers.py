from __future__ import annotations

import fakeredis

from utils.redis_helpers import get_signal_family_mode, infer_signal_family


def test_infer_signal_family_prefers_explicit_value():
    assert infer_signal_family("type_b", strategy="momentum_breakout", source="consensus_signal_runner") == "type_b"


def test_infer_signal_family_from_strategy_and_source():
    assert infer_signal_family(strategy="momentum_breakout") == "type_a"
    assert infer_signal_family(source="consensus_signal_runner_type_b") == "type_b"


def test_get_signal_family_mode_uses_env_default(monkeypatch):
    r = fakeredis.FakeRedis()
    monkeypatch.setenv("COIN_TYPE_A_MODE", "shadow")

    assert get_signal_family_mode(r, "COIN", "type_a") == "shadow"


def test_get_signal_family_mode_redis_override_wins(monkeypatch):
    r = fakeredis.FakeRedis()
    monkeypatch.setenv("COIN_TYPE_A_MODE", "live")
    r.hset("claw:signal_mode:COIN", mapping={"type_a": "off"})

    assert get_signal_family_mode(r, "COIN", "type_a") == "off"


def test_alt_type_b_family_defaults_off_until_explicit_override():
    r = fakeredis.FakeRedis()

    assert get_signal_family_mode(r, "COIN", "type_b_alt_pullback") == "off"

    r.hset("claw:signal_mode:COIN", mapping={"type_b_alt_pullback": "live"})
    assert get_signal_family_mode(r, "COIN", "type_b_alt_pullback") == "live"
