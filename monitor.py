"""
MTM Monitor — combined P&L watchdog for two positions.

Usage:
    python monitor.py

Edit INSTRUMENT_1, INSTRUMENT_2, PROFIT_TARGET, LOSS_LIMIT in .env before running.
Instruments format in .env:  TRADINGSYMBOL:EXCHANGE  (e.g. NIFTY26MAY23600CE:NFO)
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from kiteconnect import KiteConnect
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
API_KEY       = os.getenv("API_KEY")
ACCESS_TOKEN  = os.getenv("ACCESS_TOKEN")
INSTR_1       = os.getenv("INSTRUMENT_1", "")
INSTR_2       = os.getenv("INSTRUMENT_2", "")
PROFIT_TARGET = float(os.getenv("PROFIT_TARGET", "5000"))
LOSS_LIMIT    = float(os.getenv("LOSS_LIMIT",    "3000"))
PRODUCT       = "NRML"
POLL_INTERVAL = 1   # seconds between LTP polls

# ── Validate config ───────────────────────────────────────────────────────────
for var, val in [("ACCESS_TOKEN", ACCESS_TOKEN), ("INSTRUMENT_1", INSTR_1), ("INSTRUMENT_2", INSTR_2)]:
    if not val:
        print(f"ERROR: {var} is not set in .env")
        exit(1)

def parse_instrument(raw: str) -> tuple[str, str]:
    parts = raw.strip().split(":")
    if len(parts) != 2:
        print(f"ERROR: Instrument '{raw}' must be in SYMBOL:EXCHANGE format")
        exit(1)
    return parts[0].upper(), parts[1].upper()

sym1, exch1 = parse_instrument(INSTR_1)
sym2, exch2 = parse_instrument(INSTR_2)

# ── Kite client ───────────────────────────────────────────────────────────────
kite = KiteConnect(api_key=API_KEY)
kite.set_access_token(ACCESS_TOKEN)

def load_positions() -> tuple[dict, dict]:
    """Fetch positions once at startup to get avg_price and quantity."""
    positions = kite.positions()["net"]

    pos1 = next((p for p in positions if p["tradingsymbol"] == sym1 and p["exchange"] == exch1), None)
    pos2 = next((p for p in positions if p["tradingsymbol"] == sym2 and p["exchange"] == exch2), None)

    if pos1 is None:
        print(f"ERROR: {sym1} not found in positions. Check .env INSTRUMENT_1.")
        exit(1)
    if pos2 is None:
        print(f"ERROR: {sym2} not found in positions. Check .env INSTRUMENT_2.")
        exit(1)

    return pos1, pos2

def calc_mtm(pos: dict, ltp: float) -> float:
    """MTM for a position given current LTP.
    For short (qty < 0): profit when price falls → (avg - ltp) * abs(qty)
    For long  (qty > 0): profit when price rises → (ltp - avg) * abs(qty)
    """
    avg = pos["average_price"]
    qty = pos["quantity"]
    if qty < 0:
        return (avg - ltp) * abs(qty)
    else:
        return (ltp - avg) * abs(qty)

def get_live_mtm(pos1: dict, pos2: dict) -> tuple[float, float, float]:
    """Poll LTP for both instruments and return (combined, mtm1, mtm2)."""
    key1 = f"{exch1}:{sym1}"
    key2 = f"{exch2}:{sym2}"
    quotes = kite.ltp([key1, key2])
    ltp1 = quotes[key1]["last_price"]
    ltp2 = quotes[key2]["last_price"]
    mtm1 = calc_mtm(pos1, ltp1)
    mtm2 = calc_mtm(pos2, ltp2)
    return mtm1 + mtm2, mtm1, mtm2

def exit_position(pos: dict) -> dict | None:
    """Place a market order to square off a position."""
    quantity = abs(pos["quantity"])
    if quantity == 0:
        return None

    transaction = kite.TRANSACTION_TYPE_SELL if pos["quantity"] > 0 else kite.TRANSACTION_TYPE_BUY

    order_id = kite.place_order(
        variety=kite.VARIETY_REGULAR,
        exchange=pos["exchange"],
        tradingsymbol=pos["tradingsymbol"],
        transaction_type=transaction,
        quantity=quantity,
        product=PRODUCT,
        order_type=kite.ORDER_TYPE_MARKET,
    )
    return {"order_id": order_id, "tradingsymbol": pos["tradingsymbol"], "quantity": quantity, "side": transaction}

def print_summary(trigger: str, combined_pnl: float, orders: list[dict], start_time: datetime):
    elapsed = datetime.now() - start_time
    print("\n" + "="*55)
    print("  EXECUTION SUMMARY")
    print("="*55)
    print(f"  Trigger        : {trigger}")
    print(f"  Combined MTM   : ₹{combined_pnl:,.2f}")
    print(f"  Profit target  : ₹{PROFIT_TARGET:,.2f}")
    print(f"  Loss limit     : -₹{LOSS_LIMIT:,.2f}")
    print(f"  Exit time      : {datetime.now().strftime('%H:%M:%S')}")
    print(f"  Monitor ran for: {str(elapsed).split('.')[0]}")
    print("-"*55)
    print("  Orders placed:")
    for o in orders:
        if o:
            print(f"    {o['side']:<5} {o['quantity']:>4} x {o['tradingsymbol']}  → order_id: {o['order_id']}")
    print("="*55)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\nLoading positions...")
    pos1, pos2 = load_positions()
    print(f"  {sym1}: avg={pos1['average_price']}, qty={pos1['quantity']}")
    print(f"  {sym2}: avg={pos2['average_price']}, qty={pos2['quantity']}")

    start_time = datetime.now()
    print(f"\nMonitoring started at {start_time.strftime('%H:%M:%S')}")
    print(f"  Target: +₹{PROFIT_TARGET:,.0f}  |  Stop: -₹{LOSS_LIMIT:,.0f}")
    print(f"  Polling every {POLL_INTERVAL}s\n")
    print(f"{'Time':<12}{'Combined MTM':>14}  {sym1[:20]:<22}{sym2[:20]:<22}")
    print("-" * 70)

    trigger = None
    combined_pnl = 0.0
    tick = 0

    while True:
        try:
            combined_pnl, mtm1, mtm2 = get_live_mtm(pos1, pos2)

            if tick % 5 == 0:
                now = datetime.now().strftime("%H:%M:%S")
                print(f"{now:<12}₹{combined_pnl:>12,.2f}  ₹{mtm1:>10,.2f}          ₹{mtm2:>10,.2f}")

            tick += 1

            if combined_pnl >= PROFIT_TARGET:
                trigger = f"PROFIT TARGET HIT (+₹{combined_pnl:,.2f})"
                break
            if combined_pnl <= -LOSS_LIMIT:
                trigger = f"LOSS LIMIT HIT (-₹{abs(combined_pnl):,.2f})"
                break

        except Exception as e:
            print(f"\nERROR: {e} — retrying in 5s...")
            time.sleep(5)
            continue

        time.sleep(POLL_INTERVAL)

    # ── Triggered: exit both positions ────────────────────────────────────────
    print(f"\n\n*** {trigger} — exiting positions ***\n")

    orders = []
    for pos in [pos1, pos2]:
        try:
            result = exit_position(pos)
            orders.append(result)
            if result:
                print(f"  Order placed: {result['side']} {result['quantity']} x {result['tradingsymbol']}")
        except Exception as e:
            sym = pos["tradingsymbol"]
            print(f"  ERROR placing exit order for {sym}: {e}")
            orders.append({"order_id": "FAILED", "tradingsymbol": sym, "quantity": "?", "side": "?"})

    print_summary(trigger, combined_pnl, orders, start_time)

if __name__ == "__main__":
    main()
