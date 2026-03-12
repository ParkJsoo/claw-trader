"""tests/test_generator_auto_pause.py
_set_auto_pause() idempotent 동작 검증.
- 첫 번째 호출: pause 설정 + TG 발송
- 두 번째 호출: pause 이미 설정됨 → TG 발송 안 함
"""
from unittest.mock import MagicMock, patch


def _make_generator(redis_mock):
    from ai.generator import AISignalGenerator
    gen = AISignalGenerator.__new__(AISignalGenerator)
    gen.redis = redis_mock
    return gen


def test_set_auto_pause_first_call_sends_telegram():
    """첫 번째 _set_auto_pause 호출 → TG 발송."""
    redis_mock = MagicMock()
    redis_mock.set.return_value = True  # NX 성공

    gen = _make_generator(redis_mock)

    with patch("guards.notifier.send_telegram", return_value=True) as mock_tg:
        gen._set_auto_pause("AI_CALL_CAP_EXCEEDED", "KR", "call_count=-1 cap=1000")

    mock_tg.assert_called_once()


def test_set_auto_pause_second_call_skips_telegram():
    """두 번째 _set_auto_pause 호출 → pause 이미 설정됨 → TG 발송 안 함."""
    redis_mock = MagicMock()
    redis_mock.set.return_value = None  # NX 실패 (이미 존재)

    gen = _make_generator(redis_mock)

    with patch("guards.notifier.send_telegram", return_value=True) as mock_tg:
        gen._set_auto_pause("AI_CALL_CAP_EXCEEDED", "KR", "call_count=-1 cap=1000")

    mock_tg.assert_not_called()


def test_set_auto_pause_first_call_sets_pause_meta():
    """첫 번째 호출 → Redis에 pause:reason, pause:meta 기록."""
    redis_mock = MagicMock()
    redis_mock.set.return_value = True

    gen = _make_generator(redis_mock)

    with patch("guards.notifier.send_telegram", return_value=True):
        gen._set_auto_pause("AI_CALL_CAP_EXCEEDED", "KR", "call_count=-1 cap=1000")

    # pause:reason 설정 확인
    set_calls = [str(c) for c in redis_mock.set.call_args_list]
    assert any("claw:pause:reason" in c for c in set_calls)
    redis_mock.hset.assert_called_once()


def test_set_auto_pause_second_call_skips_meta():
    """두 번째 호출 → Redis meta 기록 안 함."""
    redis_mock = MagicMock()
    redis_mock.set.return_value = None  # NX 실패

    gen = _make_generator(redis_mock)

    with patch("guards.notifier.send_telegram", return_value=True):
        gen._set_auto_pause("AI_CALL_CAP_EXCEEDED", "KR", "call_count=-1 cap=1000")

    redis_mock.hset.assert_not_called()
