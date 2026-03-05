"""
OpenClaw Bot - Telegram Control Plane (read-only command handler)

Supported commands:
  /claw ai-status   - AI eval pipeline status
  /claw help        - command list

Security:
  - Only responds to TG_ALLOWED_CHAT_ID
  - Non-allowed chat_id: silent
  - Redis read-only - no system state changes
"""

from dotenv import load_dotenv
load_dotenv()

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import redis

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_KST = ZoneInfo("Asia/Seoul")
_POLL_INTERVAL_SEC = float(os.getenv("OPENCLAW_POLL_SEC", "3"))
_POLL_BACKOFF_MAX_SEC = 60.0
_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
_ALLOWED_CHAT_ID = os.getenv("TG_ALLOWED_CHAT_ID", "")

_HELP_TEXT = (
    "OpenClaw supported commands:\n"
    "/claw ai-status - AI eval pipeline status\n"
    "/claw help      - this help"
)


# ---------------------------------------------------------------------------
# Telegram API utils
# ---------------------------------------------------------------------------

def _tg_request(method: str, payload: dict, timeout: int = 10) -> dict | None:
    url = f"https://api.telegram.org/bot{_BOT_TOKEN}/{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"openclaw: tg_http_error {method} status={e.code}", flush=True)
        return None
    except Exception as e:
        print(f"openclaw: tg_error {method} {e}", flush=True)
        return None


def _send_message(chat_id: str | int, text: str) -> None:
    _tg_request("sendMessage", {"chat_id": chat_id, "text": text})


def _get_updates(offset: int) -> list[dict]:
    result = _tg_request(
        "getUpdates",
        {"offset": offset, "timeout": 25, "limit": 10},
        timeout=30,
    )
    if result and result.get("ok"):
        return result.get("result", [])
    return []


# ---------------------------------------------------------------------------
# Redis helpers (None-safe)
# ---------------------------------------------------------------------------

def _safe_int(r, key: str) -> int | None:
    val = r.get(key)
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _safe_hgetall(r, key: str) -> dict:
    try:
        raw = r.hgetall(key) or {}
        return {
            (k.decode() if isinstance(k, bytes) else k): (
                v.decode() if isinstance(v, bytes) else v
            )
            for k, v in raw.items()
        }
    except Exception:
        return {}


def _safe_ttl(r, key: str) -> int | None:
    try:
        ttl = r.ttl(key)
        return ttl if ttl > 0 else None
    except Exception:
        return None


def _safe_hget(r, key: str, field: str) -> str | None:
    try:
        val = r.hget(key, field)
        if val is None:
            return None
        return val.decode() if isinstance(val, bytes) else val
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def handle_ai_status(r) -> str:
    today = datetime.now(_KST).strftime("%Y%m%d")
    now_str = datetime.now(_KST).strftime("%Y-%m-%d %H:%M KST")
    lines = [f"AI Eval Status ({now_str})"]

    for market in ("KR", "US"):
        stats = _safe_hgetall(r, f"ai:eval_stats:{market}:{today}")
        emit = int(stats.get("emit", 0))
        no_emit = int(stats.get("no_emit", 0))
        errors = sum(
            int(v) for k, v in stats.items() if k.startswith("error_")
        )
        skip = sum(
            int(v) for k, v in stats.items() if k.startswith("skip_")
        )
        total = emit + no_emit + errors + skip

        call_count = _safe_int(r, f"ai:eval_call_count:{market}:{today}")
        call_str = f"{call_count}/2000" if call_count is not None else "(none)"

        if total == 0:
            lines.append(f"\n{market}: no data yet")
            continue

        emit_rate = emit / total * 100
        err_rate = errors / total * 100
        emit_ind = "OK" if 10.0 <= emit_rate <= 30.0 else "!"
        err_ind = "OK" if err_rate < 5.0 else "ERR"

        lines.append(f"\n{market}:")
        lines.append(f"  emit_rate: {emit_rate:.1f}% ({emit}/{total}) [{emit_ind}]")
        lines.append(f"  error_rate: {err_rate:.1f}% [{err_ind}]")
        lines.append(f"  call_count: {call_str}")

        sample_symbol = "005930" if market == "KR" else "AAPL"
        last_key = f"ai:eval:last:{market}:{sample_symbol}"
        direction = _safe_hget(r, last_key, "direction") or "(none)"
        emit_val = _safe_hget(r, last_key, "emit")
        if emit_val == "1":
            emit_label = "emit=True"
        elif emit_val == "0":
            emit_label = "emit=False"
        else:
            emit_label = "(none)"
        lines.append(f"  last({sample_symbol}): {direction} {emit_label}")

    eval_ttl = _safe_ttl(r, "eval:runner:lock")
    gen_ttl = _safe_ttl(r, "gen:runner:lock")
    eval_str = f"{eval_ttl}s [OK]" if eval_ttl else "[DOWN]"
    gen_str = f"{gen_ttl}s [OK]" if gen_ttl else "[DOWN]"

    lines.append(f"\nRunners:")
    lines.append(f"  eval:runner:lock: {eval_str}")
    lines.append(f"  gen:runner:lock:  {gen_str}")

    try:
        pause_val = r.get("claw:pause:global")
        pause = (pause_val.decode() if isinstance(pause_val, bytes) else pause_val) if pause_val else "false"
    except Exception:
        pause = "(error)"
    pause_label = "PAUSED [OK]" if pause == "true" else "LIVE [!]"
    lines.append(f"\npause: {pause_label}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command dispatcher
# ---------------------------------------------------------------------------

def dispatch(r, chat_id: str | int, text: str) -> None:
    text = text.strip()
    if text == "/claw ai-status":
        msg = handle_ai_status(r)
        _send_message(chat_id, msg)
    elif text in ("/claw help", "/help"):
        _send_message(chat_id, _HELP_TEXT)
    else:
        _send_message(chat_id, f"Unknown command: {text}\n\n{_HELP_TEXT}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    if not _BOT_TOKEN:
        print("openclaw: TG_BOT_TOKEN not set - exiting", flush=True)
        sys.exit(1)
    if not _ALLOWED_CHAT_ID:
        print("openclaw: TG_ALLOWED_CHAT_ID not set - exiting", flush=True)
        sys.exit(1)

    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        print("openclaw: REDIS_URL not set - exiting", flush=True)
        sys.exit(1)

    r = redis.from_url(redis_url)
    print(
        f"openclaw: started poll_sec={_POLL_INTERVAL_SEC} "
        f"allowed_chat={_ALLOWED_CHAT_ID}",
        flush=True,
    )

    offset = 0
    backoff = _POLL_INTERVAL_SEC

    while True:
        try:
            updates = _get_updates(offset)
            backoff = _POLL_INTERVAL_SEC  # reset on success

            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message") or update.get("edited_message")
                if not msg:
                    continue
                chat_id = str(msg["chat"]["id"])
                text = msg.get("text", "").strip()
                if not text:
                    continue

                if chat_id != str(_ALLOWED_CHAT_ID):
                    print(f"openclaw: ignored unauthorized chat_id={chat_id}", flush=True)
                    continue

                print(f"openclaw: cmd chat={chat_id} text={text!r}", flush=True)
                try:
                    dispatch(r, chat_id, text)
                except Exception as e:
                    print(f"openclaw: dispatch_error {e}", flush=True)
                    _send_message(chat_id, f"Error: {e}")

        except Exception as e:
            print(f"openclaw: poll_error {e}", flush=True)
            backoff = min(backoff * 2, _POLL_BACKOFF_MAX_SEC)

        time.sleep(backoff)


if __name__ == "__main__":
    main()
