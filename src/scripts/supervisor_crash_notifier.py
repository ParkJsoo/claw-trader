"""supervisor_crash_notifier — supervisord PROCESS_STATE_FATAL 이벤트 수신 후 TG 알림.

supervisord eventlistener로 실행됨:
  [eventlistener:crash-notifier]
  command=venv/bin/python -m scripts.supervisor_crash_notifier
  events=PROCESS_STATE_FATAL
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv
load_dotenv()

from guards.notifier import send_telegram


def parse_event(header: str, body: str) -> str:
    """이벤트 헤더/바디에서 processname 추출. 없으면 'unknown' 반환."""
    for token in body.split():
        if token.startswith("processname:"):
            return token[len("processname:"):]
    return "unknown"


def _read_event() -> tuple[str, str] | None:
    """supervisord 이벤트 프로토콜에 따라 stdin에서 헤더+바디 읽기.

    반환: (header_line, body_str) 또는 None(EOF).
    """
    header_line = sys.stdin.readline()
    if not header_line:
        return None
    header_line = header_line.strip()

    # len: 파싱
    body_len = 0
    for token in header_line.split():
        if token.startswith("len:"):
            try:
                body_len = int(token[4:])
            except ValueError:
                pass

    body = sys.stdin.read(body_len) if body_len > 0 else ""
    return header_line, body


def _ack_ok() -> None:
    """supervisord에게 RESULT OK 응답."""
    sys.stdout.write("RESULT 2\nOK")
    sys.stdout.flush()


def main() -> None:
    while True:
        result = _read_event()
        if result is None:
            break
        header, body = result

        process_name = parse_event(header, body)
        try:
            send_telegram(
                f"[CLAW] ⚠️ 프로세스 크래시\n"
                f"{process_name} FATAL → supervisord 재시작 한도 초과"
            )
        except Exception as e:
            print(f"crash_notifier: send_telegram error {e}", file=sys.stderr, flush=True)

        _ack_ok()


if __name__ == "__main__":
    main()
