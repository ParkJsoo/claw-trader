from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

from app.order_watcher import OrderWatcher, WatcherConfig


def main():
    # 프로젝트 루트의 .env 로드
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")

    cfg = WatcherConfig(
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        poll_interval_sec=float(os.getenv("WATCHER_POLL_SEC", "1.0")),
        ttl_cancel_sec=int(os.getenv("WATCHER_TTL_CANCEL_SEC", "15")),
    )

    w = OrderWatcher(cfg)
    w.run_forever()


if __name__ == "__main__":
    main()
