"""
OpenClaw Bot - Telegram Control Plane (read-only command handler)

Supported commands:
  /claw status      - system overall status (pause, md, runners, AI summary)
  /claw ai-status   - AI eval pipeline status (detailed)
  /claw news        - news intelligence pipeline status
  /claw pnl         - realized/unrealized PnL + open positions
  /claw help        - command list

Security:
  - Only responds to TG_ALLOWED_CHAT_ID
  - Non-allowed chat_id: silent
  - Redis: status commands read-only; /claw pause on|off writes Redis (PIN protected)
  - Self-lock: ops:openclaw_bot:lock (prevents duplicate process)
  - Duplicate update_id dedup: ops:tg:seen:{update_id}
"""

from dotenv import load_dotenv
load_dotenv()

import json
import os
import signal as _signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import redis

from utils.redis_helpers import secs_until_kst_midnight as _secs_until_kst_midnight

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
    "/claw status      - system overall status\n"
    "/claw ai-status   - AI eval pipeline status\n"
    "/claw news        - news intelligence status\n"
    "/claw pnl         - PnL + open positions\n"
    "/claw report      - 오늘 KR 성과 리포트 즉시 발송\n"
    "/claw backtest    - KR 파라미터 스윕 백테스트 즉시 실행\n"
    "/claw set <param> <value> - 파라미터 변경 (stop_pct, take_pct, trail_pct, size_cash_pct, max_concurrent)\n"
    "/claw pause on    - 전역 일시정지 (자정 KST 자동 만료)\n"
    "/claw pause off   - 일시정지 해제\n"
    "/claw help        - this help"
)

_ALLOWED_PARAMS: dict[str, tuple[float, float]] = {
    "stop_pct":       (0.005, 0.05),
    "take_pct":       (0.01,  0.10),
    "trail_pct":      (0.005, 0.05),
    "size_cash_pct":  (0.05,  0.50),
    "max_concurrent": (1,     5),
}

_TG_PAUSE_PIN = os.getenv("TG_PAUSE_PIN", "")
_DUAL_CAP = int(os.getenv("DUAL_DAILY_CALL_CAP", "500"))

_BOT_LOCK_KEY = "ops:openclaw_bot:lock"
_BOT_LOCK_TTL = 60  # seconds — renewed each loop
_SEEN_UPDATE_TTL = 86400  # 1 day dedup window


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


def _safe_llen(r, key: str) -> int | None:
    try:
        return r.llen(key)
    except Exception:
        return None


def _safe_lindex(r, key: str, index: int) -> str | None:
    try:
        val = r.lindex(key, index)
        if val is None:
            return None
        return val.decode() if isinstance(val, bytes) else val
    except Exception:
        return None


def _md_age_sec(r, market: str) -> int | None:
    try:
        val = r.get(f"md:last_update:{market}")
        if val is None:
            return None
        ts_ms = int(val.decode() if isinstance(val, bytes) else val)
        return int(time.time() - ts_ms / 1000)
    except Exception:
        return None


def _seen_update(r, update_id: int) -> bool:
    """Returns True if update_id was already processed (dedup). Marks as seen."""
    key = f"ops:tg:seen:{update_id}"
    result = r.set(key, "1", nx=True, ex=_SEEN_UPDATE_TTL)
    return result is None  # None = key existed = already seen


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def handle_status(r) -> str:
    """/claw status — system overall status (compact)."""
    now_str = datetime.now(_KST).strftime("%Y-%m-%d %H:%M KST")
    today = datetime.now(_KST).strftime("%Y%m%d")
    lines = [f"Claw Status ({now_str})"]

    # Pause
    try:
        pause_val = r.get("claw:pause:global")
        pause = (pause_val.decode() if isinstance(pause_val, bytes) else pause_val) if pause_val else "false"
    except Exception:
        pause = "(error)"
    lines.append(f"pause: {'PAUSED [OK]' if pause == 'true' else 'LIVE [!]'}")

    # MD age
    kr_age = _md_age_sec(r, "KR")
    us_age = _md_age_sec(r, "US")
    kr_age_str = f"{kr_age}s {'[OK]' if kr_age is not None and kr_age < 180 else '[!]'}" if kr_age is not None else "(none)"
    us_age_str = f"{us_age}s [delayed]" if us_age is not None else "(none)"
    lines.append(f"\nData:")
    lines.append(f"  KR md_age: {kr_age_str}")
    lines.append(f"  US md_age: {us_age_str}")

    # mark_hist length
    kr_hist = _safe_llen(r, "mark_hist:KR:005930")
    us_hist = _safe_llen(r, "mark_hist:US:AAPL")
    kr_hist_str = f"{kr_hist} [OK]" if kr_hist else "(none)"
    us_hist_str = f"{us_hist} [delayed]" if us_hist else "(none)"
    lines.append(f"  hist KR:005930={kr_hist_str}")
    lines.append(f"  hist US:AAPL={us_hist_str}")

    # AI summary (dual eval consensus)
    lines.append(f"\nAI:")
    for market in ("KR", "US"):
        stats = _safe_hgetall(r, f"ai:dual_stats:consensus:{market}:{today}")
        emit = int(stats.get("emit", 0))
        hold = int(stats.get("hold", 0))
        skip = int(stats.get("skip", 0))
        prefilter = sum(
            int(stats.get(k, 0)) for k in (
                "skip_prefilter_ret1m", "skip_prefilter_ret5m",
                "skip_prefilter_range5m", "skip_call_cap",
                "skip_cold_start", "skip_feature_error",
            )
        )
        total = emit + hold + skip + prefilter
        if total == 0:
            lines.append(f"  {market}: no data yet")
        else:
            emit_rate = emit / total * 100
            emit_ind = "OK" if 5 <= emit_rate <= 40 else "!"
            lines.append(f"  {market}: emit={emit_rate:.1f}%[{emit_ind}] calls={_safe_int(r, f'ai:dual_call_count:{market}:{today}') or 0}/{_DUAL_CAP}")

    # Runner locks
    eval_ttl = _safe_ttl(r, "eval:runner:lock")
    gen_ttl = _safe_ttl(r, "gen:runner:lock")
    dual_ttl = _safe_ttl(r, "dual:runner:lock")
    news_ttl = _safe_ttl(r, "news:runner:lock")
    bot_ttl = _safe_ttl(r, _BOT_LOCK_KEY)
    lines.append(f"\nRunners:")
    lines.append(f"  gen:   {f'{gen_ttl}s[OK]' if gen_ttl else '[DOWN]'}")
    lines.append(f"  eval:  {f'{eval_ttl}s[OK]' if eval_ttl else '[DOWN]'}")
    lines.append(f"  dual:  {f'{dual_ttl}s[OK]' if dual_ttl else '[DOWN]'}")
    lines.append(f"  news:  {f'{news_ttl}s[OK]' if news_ttl else '[DOWN]'}")
    lines.append(f"  bot:   {f'{bot_ttl}s[OK]' if bot_ttl else '[DOWN]'}")

    return "\n".join(lines)


def handle_ai_status(r) -> str:
    today = datetime.now(_KST).strftime("%Y%m%d")
    now_str = datetime.now(_KST).strftime("%Y-%m-%d %H:%M KST")
    lines = [f"AI Dual Eval Status ({now_str})"]

    for market in ("KR", "US"):
        # dual eval consensus 통계
        stats = _safe_hgetall(r, f"ai:dual_stats:consensus:{market}:{today}")
        emit = int(stats.get("emit", 0))
        hold = int(stats.get("hold", 0))
        skip = int(stats.get("skip", 0))
        skip_prefilter = sum(
            int(stats.get(k, 0)) for k in (
                "skip_prefilter_ret1m", "skip_prefilter_ret5m",
                "skip_prefilter_range5m", "skip_call_cap",
                "skip_cold_start", "skip_feature_error",
            )
        )
        total = emit + hold + skip + skip_prefilter

        call_count = _safe_int(r, f"ai:dual_call_count:{market}:{today}")
        call_str = f"{call_count}/{_DUAL_CAP}" if call_count is not None else "(none)"

        if total == 0:
            lines.append(f"\n{market}: no data yet")
            continue

        emit_rate = emit / total * 100
        lines.append(f"\n{market}:")
        lines.append(f"  emit={emit} hold={hold} skip={skip} prefilter={skip_prefilter} total={total}")
        lines.append(f"  emit_rate: {emit_rate:.1f}% [{'OK' if 5 <= emit_rate <= 40 else '!'}]")
        lines.append(f"  call_count: {call_str}")

        sample_symbol = "005930" if market == "KR" else "AAPL"
        last = _safe_hgetall(r, f"ai:dual:last:consensus:{market}:{sample_symbol}")
        if last:
            consensus = last.get("consensus", "(none)")
            direction = last.get("direction", "")
            c_emit = last.get("claude_emit", "")
            q_emit = last.get("qwen_emit", "")
            lines.append(f"  last({sample_symbol}): {consensus}/{direction} c={c_emit} q={q_emit}")

    dual_ttl = _safe_ttl(r, "dual:runner:lock")
    consensus_ttl = _safe_ttl(r, "consensus:runner:lock")
    lines.append(f"\nRunners:")
    lines.append(f"  dual:runner:lock:      {f'{dual_ttl}s [OK]' if dual_ttl else '[DOWN]'}")
    lines.append(f"  consensus:runner:lock: {f'{consensus_ttl}s [OK]' if consensus_ttl else '[DOWN]'}")

    try:
        pause_val = r.get("claw:pause:global")
        pause = (pause_val.decode() if isinstance(pause_val, bytes) else pause_val) if pause_val else "false"
    except Exception:
        pause = "(error)"
    pause_label = "PAUSED [OK]" if pause == "true" else "LIVE [!]"
    lines.append(f"\npause: {pause_label}")

    return "\n".join(lines)


def handle_news(r) -> str:
    """/claw news — 뉴스 수집 현황."""
    today = datetime.now(_KST).strftime("%Y%m%d")
    now_str = datetime.now(_KST).strftime("%Y-%m-%d %H:%M KST")
    lines = [f"News Intel ({now_str})"]

    # Runner 상태
    news_ttl = _safe_ttl(r, "news:runner:lock")
    lines.append(f"runner: {f'{news_ttl}s [OK]' if news_ttl else '[DOWN]'}")

    # 시장별 통계
    for market in ("KR", "US"):
        stats = _safe_hgetall(r, f"news:stats:{market}:{today}")
        if not stats:
            lines.append(f"\n{market}: no data yet")
            continue

        total = int(stats.get("total", 0))
        high = int(stats.get("impact_high", 0))
        pos = int(stats.get("sent_positive", 0))
        neg = int(stats.get("sent_negative", 0))
        lines.append(f"\n{market}: {total}건 | high={high} pos={pos} neg={neg}")

        # 최신 high-impact 뉴스 1건 (종목별 우선)
        watchlist = ["005930", "000660"] if market == "KR" else ["AAPL", "NVDA"]
        shown = False
        for symbol in watchlist:
            raw = _safe_lindex(r, f"news:symbol:{market}:{symbol}:{today}", 0)
            if raw:
                try:
                    d = json.loads(raw)
                    if d.get("impact") == "high":
                        summary = d.get("ai_summary") or d.get("title", "")[:50]
                        sent = d.get("sentiment", "")
                        lines.append(f"  [{symbol}][{sent}] {summary[:60]}")
                        shown = True
                        break
                except Exception:
                    pass

        # high-impact 없으면 매크로 최신 1건
        if not shown:
            raw = _safe_lindex(r, f"news:macro:{market}:{today}", 0)
            if raw:
                try:
                    d = json.loads(raw)
                    summary = d.get("ai_summary") or d.get("title", "")[:50]
                    lines.append(f"  [MACRO] {summary[:60]}")
                except Exception:
                    pass

    return "\n".join(lines)


def handle_set(r, param: str, value_str: str) -> str:
    """/claw set <param> <value> — Redis config 파라미터 변경."""
    if param not in _ALLOWED_PARAMS:
        allowed_desc = ", ".join(
            f"{p} ({lo}~{hi})" for p, (lo, hi) in _ALLOWED_PARAMS.items()
        )
        return f"❌ 허용 파라미터: {allowed_desc}"

    lo, hi = _ALLOWED_PARAMS[param]
    try:
        value = float(value_str)
    except ValueError:
        return f"❌ 값이 숫자가 아닙니다: {value_str!r}"

    if not (lo <= value <= hi):
        return f"❌ {param} 범위 초과: {lo}~{hi}"

    for market in ("KR", "US"):
        r.hset(f"claw:config:{market}", param, str(value))

    return f"✅ {param} = {value} (반영까지 최대 60초 소요)"


def handle_pause_on(r, pin: str) -> str:
    """/claw pause on [PIN] — 전역 일시정지."""
    if _TG_PAUSE_PIN and pin != _TG_PAUSE_PIN:
        return "PIN 오류. 명령: /claw pause on <PIN>"
    ttl = _secs_until_kst_midnight()
    r.set("claw:pause:global", "true", ex=ttl)
    r.set("claw:pause:reason", "manual_tg", ex=ttl)
    now_str = datetime.now(_KST).strftime("%H:%M KST")
    return f"PAUSED at {now_str} (TTL={ttl//3600}h{(ttl%3600)//60}m, 자정 KST 자동 만료)"


def handle_pause_off(r, pin: str) -> str:
    """/claw pause off [PIN] — 일시정지 해제."""
    if _TG_PAUSE_PIN and pin != _TG_PAUSE_PIN:
        return "PIN 오류. 명령: /claw pause off <PIN>"
    r.delete("claw:pause:global")
    r.delete("claw:pause:reason")
    r.delete("claw:pause:meta")
    now_str = datetime.now(_KST).strftime("%H:%M KST")
    return f"RESUMED at {now_str}"


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def handle_pnl(r) -> str:
    """/claw pnl — realized/unrealized PnL + 오픈 포지션."""
    now_str = datetime.now(_KST).strftime("%Y-%m-%d %H:%M KST")
    lines = [f"PnL ({now_str})"]

    for market in ("KR", "US"):
        pnl_raw = _safe_hgetall(r, f"pnl:{market}")
        realized = _safe_float(pnl_raw.get("realized_pnl", "0"))
        unrealized = _safe_float(pnl_raw.get("unrealized_pnl", "0"))
        currency = pnl_raw.get("currency", "KRW" if market == "KR" else "USD")
        lines.append(f"\n{market} ({currency}):")
        lines.append(f"  realized:   {realized:+.2f}")
        lines.append(f"  unrealized: {unrealized:+.2f}")

        # 오픈 포지션 목록 (position_index는 SET — smembers 사용)
        try:
            raw_members = r.smembers(f"position_index:{market}")
            symbols = sorted(s.decode() if isinstance(s, bytes) else s for s in raw_members)
        except Exception:
            symbols = []

        if symbols:
            lines.append(f"  positions ({len(symbols)}):")
            for sym in symbols:
                pos = _safe_hgetall(r, f"position:{market}:{sym}")
                if not pos:
                    continue
                qty = pos.get("qty", "0")
                avg = _safe_float(pos.get("avg_price", "0"))
                upnl_raw = pos.get("unrealized_pnl", "")
                upnl_str = f" upnl={_safe_float(upnl_raw):+.2f}" if upnl_raw else ""
                lines.append(f"    {sym}: qty={qty} avg={avg:.2f}{upnl_str}")
        else:
            lines.append("  positions: (none)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Backtest handler
# ---------------------------------------------------------------------------

def handle_backtest(r, market: str = "KR") -> str:
    """/claw backtest — 파라미터 스윕 백테스트 즉시 실행."""
    from app.backtester import Backtester, ParamSet
    from utils.redis_helpers import load_watchlist
    from decimal import Decimal
    import os

    env_key = "GEN_WATCHLIST_KR" if market == "KR" else "GEN_WATCHLIST_US"
    watchlist = load_watchlist(r, market, env_key)
    if not watchlist:
        return "워치리스트 없음"

    bt = Backtester(r, market)
    _, summaries = bt.run_sweep(watchlist)
    current_params = ParamSet(
        Decimal(os.getenv("EXIT_STOP_LOSS_PCT", "0.015")),
        Decimal(os.getenv("EXIT_TAKE_PROFIT_PCT", "0.030")),
        Decimal(os.getenv("EXIT_TRAIL_STOP_PCT", "0.015")),
    )
    return bt.format_report(summaries, current_params, len(watchlist))


# ---------------------------------------------------------------------------
# Command dispatcher
# ---------------------------------------------------------------------------

def dispatch(r, chat_id: str | int, text: str) -> None:
    text = text.strip()
    if text == "/claw status":
        _send_message(chat_id, handle_status(r))
    elif text == "/claw ai-status":
        _send_message(chat_id, handle_ai_status(r))
    elif text == "/claw news":
        _send_message(chat_id, handle_news(r))
    elif text == "/claw pnl":
        _send_message(chat_id, handle_pnl(r))
    elif text == "/claw report":
        from app.performance_reporter import PerformanceReporter
        from utils.redis_helpers import today_kst
        reporter = PerformanceReporter(r)
        date_str = today_kst()
        stats = reporter.compute_and_save("KR", date_str)
        msg = reporter.format_report("KR", stats)
        _send_message(chat_id, msg)
        _send_message(chat_id, "리포트 발송 완료.")
    elif text == "/claw backtest":
        _send_message(chat_id, "백테스트 실행 중...")
        msg = handle_backtest(r, "KR")
        _send_message(chat_id, msg)
    elif text.startswith("/claw set "):
        parts = text[len("/claw set "):].strip().split()
        if len(parts) != 2:
            _send_message(chat_id, "사용법: /claw set <param> <value>")
        else:
            _send_message(chat_id, handle_set(r, parts[0], parts[1]))
    elif text.startswith("/claw pause on"):
        pin = text[len("/claw pause on"):].strip()
        _send_message(chat_id, handle_pause_on(r, pin))
    elif text.startswith("/claw pause off"):
        pin = text[len("/claw pause off"):].strip()
        _send_message(chat_id, handle_pause_off(r, pin))
    elif text in ("/claw help", "/help"):
        _send_message(chat_id, _HELP_TEXT)
    else:
        _send_message(chat_id, f"Unknown command.\n\n{_HELP_TEXT}")


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

    # Self-lock: prevent duplicate processes
    if not r.set(_BOT_LOCK_KEY, "1", nx=True, ex=_BOT_LOCK_TTL):
        print("openclaw: already running (lock exists) - exiting", flush=True)
        sys.exit(0)
    def _handle_sigterm(signum, frame):
        r.delete(_BOT_LOCK_KEY)
        print("openclaw: SIGTERM received, lock released", flush=True)
        sys.exit(0)
    _signal.signal(_signal.SIGTERM, _handle_sigterm)

    print(
        f"openclaw: started poll_sec={_POLL_INTERVAL_SEC} "
        f"allowed_chat={_ALLOWED_CHAT_ID}",
        flush=True,
    )

    offset = 0
    backoff = _POLL_INTERVAL_SEC

    try:
        while True:
            r.expire(_BOT_LOCK_KEY, _BOT_LOCK_TTL)  # renew self-lock

            try:
                updates = _get_updates(offset)
                backoff = _POLL_INTERVAL_SEC  # reset on success

                for update in updates:
                    update_id = update["update_id"]
                    offset = update_id + 1

                    if _seen_update(r, update_id):
                        print(f"openclaw: dup update_id={update_id} skipped", flush=True)
                        continue

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

                    log_text = " ".join(text.split()[:3])  # PIN 등 민감 인자 제외
                    print(f"openclaw: cmd chat={chat_id} text={log_text!r}", flush=True)
                    try:
                        dispatch(r, chat_id, text)
                    except Exception as e:
                        print(f"openclaw: dispatch_error {e}", flush=True)
                        _send_message(chat_id, "Internal error. Check logs.")

            except Exception as e:
                print(f"openclaw: poll_error {e}", flush=True)
                backoff = min(backoff * 2, _POLL_BACKOFF_MAX_SEC)

            time.sleep(backoff)

    finally:
        r.delete(_BOT_LOCK_KEY)
        print("openclaw: lock released", flush=True)


if __name__ == "__main__":
    main()
