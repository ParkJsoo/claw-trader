from dotenv import load_dotenv
load_dotenv()

import json
import uuid
from datetime import datetime, timezone
import os
import redis

def main():
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        raise RuntimeError("REDIS_URL is not set")
    r = redis.from_url(redis_url)

    payload = {
        "signal_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "market": "KR",
        "symbol": "005930",
        "direction": "LONG",
        "entry": {"price": "100", "size_cash": "1000"},
        "stop": {"price": "90"},
    }

    r.lpush("claw:signal:queue", json.dumps(payload))
    print("pushed:", payload)

if __name__ == "__main__":
    main()
