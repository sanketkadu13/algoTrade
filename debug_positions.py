"""Run this once to see exact tradingsymbols Kite uses for your positions."""
import os
from kiteconnect import KiteConnect
from dotenv import load_dotenv

load_dotenv()

kite = KiteConnect(api_key=os.getenv("API_KEY"))
kite.set_access_token(os.getenv("ACCESS_TOKEN"))

positions = kite.positions()["net"]

if not positions:
    print("No positions found.")
else:
    print(f"{'tradingsymbol':<30} {'exchange':<8} {'qty':>6} {'pnl':>10}")
    print("-" * 60)
    for p in positions:
        print(f"{p['tradingsymbol']:<30} {p['exchange']:<8} {p['quantity']:>6} {p['pnl']:>10.2f}")
