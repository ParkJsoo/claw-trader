from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from app import market_data_runner


def test_remaining_sleep_sec_returns_full_budget_when_fast(monkeypatch):
    monkeypatch.setattr(market_data_runner, "POLL_INTERVAL", 3)

    assert market_data_runner._remaining_sleep_sec(1.25) == 1.75


def test_remaining_sleep_sec_clamps_to_zero_when_overrun(monkeypatch):
    monkeypatch.setattr(market_data_runner, "POLL_INTERVAL", 3)

    assert market_data_runner._remaining_sleep_sec(4.5) == 0.0
