from dotenv import load_dotenv
load_dotenv()

import json
import uuid
from datetime import datetime, timezone
import redis
import os

def main():
    r = redis.from_url(os.getenv("REDIS_URL"))

    payload = {
        "signal_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "market": "US",          # "KR"로 바꾸면 KIS executor로 감
        "symbol": "AAPL",        # KR이면 "005930"
        "direction": "LONG",     # "EXIT"
        "entry": {"price": "10", "size_cash": "5"},  # Decimal 문자열 권장
        "stop": {"price": "9"},
    }

    r.lpush("claw:signal:queue", json.dumps(payload))
    print("pushed:", payload)

if __name__ == "__main__":
    main()
