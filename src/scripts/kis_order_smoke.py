from dotenv import load_dotenv
load_dotenv()

from decimal import Decimal
from exchange.kis.client import KisClient
from domain.models import PlaceOrderRequest, OrderSide, OrderType, TimeInForce

def main():
    c = KisClient()

    print("[0] ping:", c.ping())

    # ⚠️ 종목코드는 국내 6자리. 예: 삼성전자 005930
    symbol = "005930"

    req = PlaceOrderRequest(
        symbol=symbol,
        side=OrderSide.BUY,
        qty=Decimal("1"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("100"),  # 아주 낮게(실제 체결 목적 아님)
        tif=TimeInForce.DAY,
        client_order_id="CLAW-KR-SMOKE-001",
    )

    print("[1] place_order request:", req.model_dump())
    res = c.place_order(req)
    print("[2] place_order result:", res.model_dump())

if __name__ == "__main__":
    main()
