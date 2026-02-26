from dotenv import load_dotenv
load_dotenv()

from exchange.kis.client import KisClient

def main():
    c = KisClient()

    print("[1] ping() ...")
    ok = c.ping()
    print("ping:", ok)

    print("\n[2] get_account_snapshot() ...")
    snap = c.get_account_snapshot()
    print("currency:", snap.currency)
    print("equity:", snap.equity)
    print("cash:", snap.cash)
    print("available_cash:", snap.available_cash)

if __name__ == "__main__":
    main()
