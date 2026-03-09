"""parse_decision_response 단위 테스트."""
from __future__ import annotations

import json
import pytest

from ai.providers.base import parse_decision_response


class TestParseDecisionResponse:
    def test_valid_json(self):
        text = '{"emit": true, "direction": "LONG", "confidence": 0.8, "reason": "momentum"}'
        emit, direction, confidence, reason = parse_decision_response(text)
        assert emit is True
        assert direction == "LONG"
        assert confidence == 0.8
        assert reason == "momentum"

    def test_json_wrapped_in_markdown(self):
        text = '```json\n{"emit": false, "direction": "HOLD", "confidence": 0.3, "reason": "weak"}\n```'
        emit, direction, confidence, reason = parse_decision_response(text)
        assert emit is False
        assert direction == "HOLD"

    def test_invalid_direction_becomes_hold(self):
        text = '{"emit": true, "direction": "SHORT", "confidence": 0.9, "reason": "test"}'
        emit, direction, confidence, reason = parse_decision_response(text)
        assert direction == "HOLD"
        assert emit is False  # invalid direction → emit forced False

    def test_confidence_clamped_above_one(self):
        text = '{"emit": true, "direction": "LONG", "confidence": 1.5, "reason": "high"}'
        _, _, confidence, _ = parse_decision_response(text)
        assert confidence == 1.0

    def test_confidence_clamped_below_zero(self):
        text = '{"emit": true, "direction": "LONG", "confidence": -0.5, "reason": "neg"}'
        _, _, confidence, _ = parse_decision_response(text)
        assert confidence == 0.0

    def test_missing_fields_use_defaults(self):
        text = '{}'
        emit, direction, confidence, reason = parse_decision_response(text)
        assert emit is False
        assert direction == "HOLD"
        assert confidence == 0.0
        assert reason == ""

    def test_reason_truncated_to_100(self):
        long_reason = "x" * 200
        text = json.dumps({"emit": False, "direction": "HOLD", "confidence": 0.0, "reason": long_reason})
        _, _, _, reason = parse_decision_response(text)
        assert len(reason) == 100

    def test_invalid_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            parse_decision_response("not json at all")

    def test_exit_direction(self):
        text = '{"emit": true, "direction": "EXIT", "confidence": 0.7, "reason": "close"}'
        emit, direction, confidence, reason = parse_decision_response(text)
        assert emit is True
        assert direction == "EXIT"

    def test_json_with_leading_text(self):
        text = 'Sure, here is my answer: {"emit": false, "direction": "HOLD", "confidence": 0.1, "reason": "ok"}'
        emit, direction, confidence, reason = parse_decision_response(text)
        assert direction == "HOLD"

    def test_confidence_non_numeric_defaults_zero(self):
        text = '{"emit": true, "direction": "LONG", "confidence": "high", "reason": "ok"}'
        _, _, confidence, _ = parse_decision_response(text)
        assert confidence == 0.0
