"""Phase 18 자동화 기능 단위 테스트."""
import sys
import os

# ensure src is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import fakeredis
import pytest

from utils.redis_helpers import get_config


class TestGetConfig:
    def test_returns_default_when_no_redis_value(self):
        r = fakeredis.FakeRedis()
        assert get_config(r, "KR", "stop_pct", 0.015) == 0.015

    def test_returns_redis_value_when_set(self):
        r = fakeredis.FakeRedis()
        r.hset("claw:config:KR", "stop_pct", "0.012")
        assert get_config(r, "KR", "stop_pct", 0.015) == 0.012

    def test_handles_bytes_value(self):
        r = fakeredis.FakeRedis()
        r.hset("claw:config:KR", "take_pct", b"0.04")
        assert get_config(r, "KR", "take_pct", 0.03) == 0.04


# supervisor_crash_notifier 임포트 테스트
from scripts.supervisor_crash_notifier import parse_event


class TestSupervisorCrashNotifier:
    def test_parse_event_extracts_process_name(self):
        header = "ver:3.0 server:supervisor serial:1 pool:crash-notifier poolserial:1 eventname:PROCESS_STATE_FATAL len:44"
        body = "processname:runner groupname:runner from_state:BACKOFF"
        name = parse_event(header, body)
        assert name == "runner"

    def test_parse_event_missing_processname(self):
        header = "eventname:PROCESS_STATE_FATAL len:0"
        body = ""
        name = parse_event(header, body)
        assert name == "unknown"
