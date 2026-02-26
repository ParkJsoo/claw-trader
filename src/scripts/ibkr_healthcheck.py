from dotenv import load_dotenv
load_dotenv()

from exchange.ibkr.client import IbkrClient

def main():
    c = IbkrClient()
    print("[1] ping() ...")
    print("ping:", c.ping())

    print("\n[2] get_account_snapshot() ...")
    snap = c.get_account_snapshot()
    print("currency:", snap.currency)
    print("equity:", snap.equity)
    print("cash:", snap.cash)
    print("available_cash:", snap.available_cash)

if __name__ == "__main__":
    main()
