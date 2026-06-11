from __future__ import annotations

import csv
import json
import math
import os
import queue
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime, timedelta, timezone

from dotenv import load_dotenv, set_key
import io
import zipfile

from flask import Flask, Response, jsonify, redirect, render_template, request, send_file, url_for
from kiteconnect import KiteConnect

# Load .env from DATA_DIR if set (so each user instance gets its own creds),
# else fall back to a .env next to app.py (legacy single-user layout).
_data_dir_envfile = os.path.join(os.environ.get("DATA_DIR", os.path.dirname(__file__)), ".env")
load_dotenv(_data_dir_envfile)

app = Flask(__name__)

# ── Kite ──────────────────────────────────────────────────────────────────────
API_KEY      = os.getenv("API_KEY")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
kite = KiteConnect(api_key=API_KEY)
kite.set_access_token(ACCESS_TOKEN)

# ── Per-strategy state ────────────────────────────────────────────────────────
_lock         = threading.RLock()
strategies:   dict[str, dict]            = {}
strategy_order: list[str]               = []
_strategy_ctr = 0

_stop_events: dict[str, threading.Event]        = {}
_threads:     dict[str, threading.Thread | None] = {}
_histories:   dict[str, list]                   = {}
_history_lock = threading.Lock()
_csv_paths:   dict[str, str]                    = {}

# ── Path layout (single-user-per-process / process-isolation model) ──────────
# CODE_DIR is the directory of this file — shared across all user instances
# (one git checkout, many running processes). It contains read-only resources
# like the NSE holiday calendar, templates, and static assets.
# DATA_DIR is where the running instance writes its per-user state: .env,
# strategies.json, CSVs, runtime JSON snapshots. Default = CODE_DIR for the
# legacy single-user layout; override via the DATA_DIR env var to point at a
# user-specific dir like /opt/kite-data-omkar/.
CODE_DIR = os.path.dirname(__file__)
DATA_DIR = os.environ.get("DATA_DIR", CODE_DIR)

# All per-strategy MTM CSVs live under <DATA_DIR>/data/csv/
_CSV_DIR = os.path.join(DATA_DIR, "data", "csv")
os.makedirs(_CSV_DIR, exist_ok=True)

def _make_strategy(name: str, sid: str, type_: str = "custom") -> dict:
    return {
        "id": sid, "name": name,
        "type": type_,                    # "custom" | "scheduled" (singleton)
        "running": False, "status": "idle",
        "mtm": 0.0, "positions": [], "selected": [],
        "profit_target": 2500.0, "loss_limit": 2000.0,
        # Independent toggles for each exit trigger. Default both ON to
        # preserve the original behavior (target + SL always-on).
        "profit_target_enabled": True,
        "loss_limit_enabled":    True,
        # Which MTM basis each trigger compares against — independent for
        # target vs SL so the user can mix: e.g. SL on exit-MTM (fires earlier
        # in illiquid books = safer) + target on LTP (easier to hit when bid/ask
        # is wide). Trail SL inherits loss_limit_basis (same downside-protection
        # category, no point splitting them).
        #   "ltp"  → combined MTM from LTP
        #   "exit" → combined MTM if we exited NOW (bid for longs, ask for shorts)
        "profit_target_basis": "ltp",
        "loss_limit_basis":    "ltp",
        "trail_enabled": False, "trail_activate_at": 500.0, "trail_by": 300.0,
        "peak_mtm": None, "trail_sl": None,
        "peak_mtm_day": None, "monitoring_start_ts": None,
        "trigger": None, "logs": [], "sessions": [],
        # Auto-entry config
        "auto_entry_enabled":     False,
        "auto_entry_time":        "10:00",
        "auto_entry_qty":         65,
        "auto_entry_product":     "MIS",
        "auto_entry_last_fired":  None,   # runtime — YYYY-MM-DD (IST)
        "auto_entry_status":      "idle", # runtime — idle | firing | done | failed:<msg>
        # Expiry policy resolved at fire time. Default to next-week so the
        # scheduled flow uses weeklies — users can change to current_weekly,
        # current_monthly, or next_monthly per strategy.
        "auto_entry_expiry":      "next_weekly",
    }

_CONFIG_FILE = os.path.join(DATA_DIR, "strategies.json")


def _atomic_json_dump(path: str, obj) -> None:
    """Write JSON to `path` atomically: write to a tmp file, fsync, then rename.
    Prevents partial / truncated files from being readable on restart, which
    was the root cause of strategies vanishing after Flask restarts."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _load_module_config_into(name: str, target: dict) -> None:
    """Overlay disk-persisted module config (DATA_DIR/data/<name>_config.json)
    onto `target` in place. Unknown keys on disk are ignored so removing a
    field from code doesn't blow up on restart. Missing file = use defaults."""
    path = os.path.join(DATA_DIR, "data", f"{name}_config.json")
    try:
        if not os.path.exists(path):
            return
        with open(path) as f:
            disk = json.load(f)
        for k, v in disk.items():
            if k in target:
                target[k] = v
    except Exception as e:
        print(f"[{name}] config load failed: {e}")


def _save_module_config(name: str, cfg: dict) -> None:
    """Atomic write of a module config dict to DATA_DIR/data/<name>_config.json."""
    path = os.path.join(DATA_DIR, "data", f"{name}_config.json")
    try:
        _atomic_json_dump(path, cfg)
    except Exception as e:
        print(f"[{name}] config save failed: {e}")


def _save_config():
    """Persist all strategy configs to strategies.json. Atomic write."""
    data = {}
    with _lock:
        for i, sid in enumerate(strategy_order):
            s = strategies.get(sid)
            if not s:
                continue
            data[sid] = {
                "order":               i,
                "name":                s["name"],
                "type":                s.get("type", "custom"),
                "selected":            s["selected"],
                "profit_target":         s["profit_target"],
                "loss_limit":            s["loss_limit"],
                "profit_target_enabled": s.get("profit_target_enabled", True),
                "loss_limit_enabled":    s.get("loss_limit_enabled",    True),
                "profit_target_basis":   s.get("profit_target_basis",   "ltp"),
                "loss_limit_basis":      s.get("loss_limit_basis",      "ltp"),
                "trail_enabled":         s["trail_enabled"],
                "trail_activate_at":   s["trail_activate_at"],
                "trail_by":            s["trail_by"],
                "auto_entry_enabled":  s.get("auto_entry_enabled", False),
                "auto_entry_time":     s.get("auto_entry_time", "10:00"),
                "auto_entry_qty":      s.get("auto_entry_qty", 65),
                "auto_entry_product":  s.get("auto_entry_product", "MIS"),
                "auto_entry_expiry":   s.get("auto_entry_expiry", "next_weekly"),
                # Persisted runtime flags — fix the restart-loses-state bug.
                # `running` lets the startup code resume monitor threads.
                # `auto_entry_last_fired` keeps the scheduler from firing
                # twice on the same day across restarts.
                "running":               bool(s.get("running")),
                "auto_entry_last_fired": s.get("auto_entry_last_fired"),
            }
    try:
        _atomic_json_dump(_CONFIG_FILE, data)
    except Exception as e:
        print(f"[config] Save failed: {e}")


def _load_config() -> dict:
    """Load strategy configs from strategies.json. Returns {} if not found
    or unreadable (we never want a corrupt file to prevent Flask from
    booting — losing config is recoverable, losing the process isn't)."""
    try:
        with open(_CONFIG_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _new_strategy(name: str = "") -> str:
    global _strategy_ctr
    _strategy_ctr += 1
    sid = f"s{_strategy_ctr}"
    if not name:
        name = f"Strategy {_strategy_ctr}"
    strategies[sid] = _make_strategy(name, sid)
    strategy_order.append(sid)
    _stop_events[sid] = threading.Event()
    _threads[sid]     = None
    _histories[sid]   = []
    _csv_paths[sid]   = os.path.join(
        _CSV_DIR,
        f"mtm_{sid}_{datetime.now().strftime('%Y%m%d')}.csv"
    )
    return sid

def _init_strategy_slot(sid: str, name: str):
    """Wire up runtime state for a strategy being restored (no counter bump)."""
    strategies[sid] = _make_strategy(name, sid)
    strategy_order.append(sid)
    _stop_events[sid] = threading.Event()
    _threads[sid]     = None
    _histories[sid]   = []
    _csv_paths[sid]   = os.path.join(
        _CSV_DIR,
        f"mtm_{sid}_{datetime.now().strftime('%Y%m%d')}.csv"
    )

_CONFIG_FIELDS = ("name", "type", "selected", "profit_target", "loss_limit",
                  "profit_target_enabled", "loss_limit_enabled",
                  "profit_target_basis",   "loss_limit_basis",
                  "trail_enabled", "trail_activate_at", "trail_by",
                  "auto_entry_enabled", "auto_entry_time", "auto_entry_qty",
                  "auto_entry_product",   "auto_entry_expiry",
                  # Persisted runtime flags (see _save_config notes)
                  "auto_entry_last_fired")

def _apply_saved(sid: str, sc: dict):
    s = strategies[sid]
    for f in _CONFIG_FIELDS:
        if f in sc:
            s[f] = sc[f]

def _default_selected() -> list[dict]:
    sel = []
    for key in ["INSTRUMENT_1", "INSTRUMENT_2"]:
        raw = os.getenv(key, "")
        if raw and ":" in raw:
            parts = raw.strip().split(":")
            sel.append({"tradingsymbol": parts[0].upper(), "exchange": parts[1].upper()})
    return sel

# Initialize default strategy from .env (may be overridden by saved config below)
_sid1 = _new_strategy("Strategy 1")
strategies[_sid1]["selected"]      = _default_selected()
strategies[_sid1]["profit_target"] = float(os.getenv("PROFIT_TARGET", "2500"))
strategies[_sid1]["loss_limit"]    = float(os.getenv("LOSS_LIMIT",    "2000"))

# Restore all strategies from strategies.json (takes precedence over .env for s1 config)
_saved_cfg = _load_config()
if _saved_cfg:
    if _sid1 in _saved_cfg:
        sc = _saved_cfg[_sid1]
        _apply_saved(_sid1, sc)
        if sc.get("selected"):                          # keep .env instruments as fallback
            strategies[_sid1]["selected"] = sc["selected"]
    extras = sorted(
        [(sid, sc) for sid, sc in _saved_cfg.items() if sid != _sid1],
        key=lambda x: x[1].get("order", 999)
    )
    for sid, sc in extras:
        try:
            _strategy_ctr = max(_strategy_ctr, int(sid[1:]))
        except (ValueError, IndexError):
            pass
        _init_strategy_slot(sid, sc.get("name", sid))
        _apply_saved(sid, sc)

# Strategies that were running when the previous process exited need their
# monitor threads restarted. We can't call _start_monitor here because it
# isn't defined yet — collect the SIDs and resume them from a deferred
# thread spawned at the end of this module.
_pending_resume_sids: list[str] = []
if _saved_cfg:
    for _sid, _sc in _saved_cfg.items():
        if _sc.get("running"):
            _pending_resume_sids.append(_sid)


# ── History ───────────────────────────────────────────────────────────────────
def _record_point(sid: str, combined: float, positions: list[dict]) -> dict:
    point = {
        "t":        datetime.now().strftime("%H:%M:%S"),
        "combined": round(combined, 2),
        "pos":      [{"sym": p["sym"], "mtm": round(p["mtm"], 2)} for p in positions],
    }
    with _history_lock:
        _histories[sid].append(point)
    csv_path = _csv_paths.get(sid, "")
    if csv_path:
        write_header = not os.path.exists(csv_path)
        with open(csv_path, "a", newline="") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(["time", "combined_mtm"] + [p["sym"] for p in positions])
            w.writerow([point["t"], point["combined"]] + [p["mtm"] for p in positions])
    return point

# ── SSE broadcast ─────────────────────────────────────────────────────────────
_clients_lock = threading.Lock()
_clients: list[queue.Queue] = []

def broadcast(event_type: str = "update", new_point: dict | None = None, sid: str | None = None):
    with _lock:
        payload = {
            "event":          event_type,
            "strategies":     {k: dict(v) for k, v in strategies.items()},
            "strategy_order": list(strategy_order),
        }
    if new_point and sid:
        payload["new_point"]     = new_point
        payload["new_point_sid"] = sid
    with _clients_lock:
        for q in list(_clients):
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass

# ── Helpers ───────────────────────────────────────────────────────────────────
def _log(sid: str, msg: str):
    ts    = datetime.now().strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    with _lock:
        if sid in strategies:
            strategies[sid]["logs"].insert(0, entry)
            strategies[sid]["logs"] = strategies[sid]["logs"][:50]
    print(f"[{sid}] {entry}")

def _telegram(msg: str):
    token, chat_id = os.getenv("TELEGRAM_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        payload = urllib.parse.urlencode({"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=payload, timeout=5)
    except Exception as e:
        print(f"Telegram error: {e}")

def _get_public_ip() -> str:
    for url in ["https://ifconfig.me", "https://api.ipify.org"]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/7.0"})
            return urllib.request.urlopen(req, timeout=5).read().decode().strip()
        except Exception:
            continue
    return "unknown"

_last_known_ip = ""

def _ip_monitor():
    global _last_known_ip
    while True:
        time.sleep(1800)
        ip = _get_public_ip()
        if ip != _last_known_ip and _last_known_ip:
            _telegram(f"⚠️ *IP Address Changed*\nNew IP: `{ip}`\nUpdate on: developers.kite.trade\nOrders will fail until whitelisted.")
        _last_known_ip = ip

threading.Thread(target=_ip_monitor, daemon=True).start()

def _calc_mtm(avg: float, ltp: float, qty: int, multiplier: float = 1.0) -> float:
    effective = abs(qty) * multiplier
    return (avg - ltp) * effective if qty < 0 else (ltp - avg) * effective

def _buffer_price(ltp: float, side: str) -> float:
    raw = ltp * 1.03 if side == "BUY" else ltp * 0.97
    return round(round(raw / 0.05) * 0.05, 2)

def _exit_limit_price(sym: str, exch: str, side: str, fallback_ltp: float = 0) -> float:
    """Tight LIMIT from live order book: BUY at best_ask × 1.005, SELL at best_bid × 0.995.
    Falls back to LTP × 1.03 / 0.97 if quote/depth unavailable."""
    try:
        key = f"{exch}:{sym}"
        q   = kite.quote([key])[key]
        depth = q.get("depth", {}) or {}
        if side == "BUY":
            asks = depth.get("sell") or []
            anchor = asks[0]["price"] if asks else q.get("last_price")
            raw = anchor * 1.005 if anchor else fallback_ltp * 1.03
        else:
            bids = depth.get("buy") or []
            anchor = bids[0]["price"] if bids else q.get("last_price")
            raw = anchor * 0.995 if anchor else fallback_ltp * 0.97
        return round(round(raw / 0.05) * 0.05, 2)
    except Exception as e:
        print(f"[exit] quote failed for {sym}: {e}; falling back to LTP buffer")
        return _buffer_price(fallback_ltp, side)

def _is_ip_error(e: Exception) -> bool:
    return "not allowed" in str(e).lower() or "ip" in str(e).lower()

def _place_with_retry(pos: dict, side: str, qty: int, price: float,
                      product: str = "NRML",
                      max_retries: int = 5, retry_delay: int = 30) -> str:
    sym = pos["tradingsymbol"]
    for attempt in range(1, max_retries + 1):
        try:
            return kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=pos["exchange"],
                tradingsymbol=sym,
                transaction_type=side,
                quantity=qty,
                product=product,
                order_type=kite.ORDER_TYPE_LIMIT,
                price=price,
            )
        except Exception as e:
            if _is_ip_error(e):
                ip = _get_public_ip()
                _telegram(
                    f"🚨 *IP BLOCKED — Orders Failing!*\n"
                    f"Attempt {attempt}/{max_retries} for `{sym}`\n"
                    f"Your IP: `{ip}`\n"
                    f"➡ Go to developers.kite.trade → add this IP\n"
                    f"Retrying in {retry_delay}s..."
                )
                if attempt < max_retries:
                    time.sleep(retry_delay)
            else:
                raise
    raise RuntimeError(f"Order failed after {max_retries} attempts (IP not whitelisted)")

def _chase_unfilled(results: list[dict], max_attempts: int = 3):
    """If any exit order is still OPEN after 3s, modify it with progressively wider LIMIT.
    Buffer ladder: 1% → 2% → 3% above best ask (BUY) / below best bid (SELL)."""
    pcts_buy  = [1.010, 1.020, 1.030]
    pcts_sell = [0.990, 0.980, 0.970]
    for attempt in range(max_attempts):
        time.sleep(3)
        try:
            orders = {str(o["order_id"]): o for o in kite.orders()}
        except Exception as e:
            print(f"[chase] orders() failed: {e}")
            return
        # Early exit: if all orders are terminal, no need to keep chasing
        all_done = all(
            orders.get(str(r.get("oid")), {}).get("status") in ("COMPLETE", "REJECTED", "CANCELLED")
            for r in results if r.get("oid")
        )
        if all_done:
            return
        for r in results:
            oid = r.get("oid")
            if not oid:
                continue
            o = orders.get(str(oid))
            if not o or o["status"] in ("COMPLETE", "REJECTED", "CANCELLED"):
                continue
            try:
                key = f"{o['exchange']}:{o['tradingsymbol']}"
                q   = kite.quote([key])[key]
                d   = q.get("depth", {}) or {}
                if o["transaction_type"] == "BUY":
                    asks = d.get("sell") or []
                    anchor = asks[0]["price"] if asks else q.get("last_price")
                    new_price = round(round(anchor * pcts_buy[attempt] / 0.05) * 0.05, 2)
                else:
                    bids = d.get("buy") or []
                    anchor = bids[0]["price"] if bids else q.get("last_price")
                    new_price = round(round(anchor * pcts_sell[attempt] / 0.05) * 0.05, 2)
                kite.modify_order(
                    variety=kite.VARIETY_REGULAR,
                    order_id=oid,
                    price=new_price,
                    order_type=kite.ORDER_TYPE_LIMIT,
                )
                print(f"[chase] {o['tradingsymbol']} → ₹{new_price} (attempt {attempt+1})")
                r["price"] = new_price
            except Exception as e:
                print(f"[chase] modify failed for {oid}: {e}")

def _exit_all(positions: list[dict]):
    """Fire all exit orders in parallel using tight book-based LIMITs, then collect fills."""
    def _exit_one(pos: dict) -> dict:
        qty = abs(pos["qty"])
        if qty == 0:
            return {"sym": pos["sym"], "skip": True}
        side    = kite.TRANSACTION_TYPE_SELL if pos["qty"] > 0 else kite.TRANSACTION_TYPE_BUY
        price   = _exit_limit_price(pos["sym"], pos["exch"], side, pos.get("ltp", 0))
        product = pos.get("product", "NRML")
        kite_pos = {"tradingsymbol": pos["sym"], "exchange": pos["exch"], "quantity": pos["qty"]}
        try:
            oid = _place_with_retry(kite_pos, side, qty, price, product=product)
            return {"sym": pos["sym"], "side": side, "qty": qty, "price": price, "oid": oid}
        except Exception as e:
            return {"sym": pos["sym"], "error": str(e)}

    n = max(1, len(positions))
    with ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(_exit_one, positions))

    # Chase unfilled orders with wider LIMITs (3 attempts, 3s apart)
    _chase_unfilled([r for r in results if r.get("oid")])

    # Final status fetch
    try:
        orders = kite.orders()
    except Exception:
        orders = []

    lines = []
    for r in results:
        if r.get("skip"):
            continue
        if r.get("error"):
            lines.append(f"❌ `{r['sym']}` FAILED: {r['error']}")
            continue
        lines.append(f"✅ `{r['sym']}` {r['side']} {r['qty']} @ ₹{r['price']} → `{r['oid']}`")
        matched = next((o for o in orders if str(o["order_id"]) == str(r["oid"])), None)
        if matched:
            status = matched["status"]
            if status == "COMPLETE":
                lines.append(f"   ↳ Filled @ ₹{matched.get('average_price', r['price'])}")
            elif status in ("REJECTED", "CANCELLED"):
                lines.append(f"   ↳ ⚠️ {status}: {matched.get('status_message','')}")
            else:
                lines.append(f"   ↳ Status: {status}")
    _telegram("*Exit Summary*\n" + "\n".join(lines))

# ── Monitor thread ────────────────────────────────────────────────────────────
def monitor_loop(sid: str):
    with _lock:
        selected = list(strategies[sid]["selected"])

    if not selected:
        _log(sid, "ERROR: No instruments selected")
        with _lock:
            strategies[sid].update({"status": "error", "running": False})
        broadcast()
        return

    try:
        net = kite.positions()["net"]
    except Exception as e:
        _log(sid, f"ERROR loading positions: {e}")
        with _lock:
            strategies[sid].update({"status": "error", "running": False})
        broadcast()
        return

    tracked = []
    for instr in selected:
        sym  = instr["tradingsymbol"]
        exch = instr["exchange"]
        pos  = next((p for p in net if p["tradingsymbol"] == sym and p["exchange"] == exch), None)
        if not pos:
            _log(sid, f"ERROR: {sym} not found in positions")
            with _lock:
                strategies[sid].update({"status": "error", "running": False})
            broadcast()
            return
        tracked.append({"sym": sym, "exch": exch,
                        "avg": pos["average_price"], "qty": pos["quantity"],
                        "mult": float(pos.get("multiplier", 1) or 1),
                        "product": pos.get("product", "NRML"),
                        "ltp": 0.0, "mtm": 0.0})

    with _lock:
        strategies[sid].update({
            "positions":           [{**t} for t in tracked],
            "peak_mtm":            None,
            "trail_sl":            None,
            "monitoring_start_ts": datetime.now().isoformat(),
        })

    global _last_known_ip
    _last_known_ip = _get_public_ip()
    strat_name = strategies[sid]["name"]
    sym_lines  = "\n".join(f"`{t['sym']}` avg ₹{t['avg']} × {t['qty']}" for t in tracked)
    for t in tracked:
        _log(sid, f"Loaded {t['sym']}: avg={t['avg']}, qty={t['qty']}")
    _log(sid, f"IP: {_last_known_ip}")
    _telegram(
        f"🟢 *{strat_name} Started ({len(tracked)} positions)*\n{sym_lines}\n"
        f"Target: +₹{strategies[sid]['profit_target']:,.0f}  |  Stop: -₹{strategies[sid]['loss_limit']:,.0f}\n"
        f"IP: `{_last_known_ip}`"
    )
    broadcast()

    stop_ev   = _stop_events[sid]
    hist_tick = 0

    while not stop_ev.is_set():
        try:
            keys   = [f"{t['exch']}:{t['sym']}" for t in tracked]
            # quote() returns LTP + depth in one call. Same rate-limit cost as
            # ltp() for our small instrument count; gives us bid/ask for the
            # exit-aware MTM display alongside the current LTP-based MTM.
            quotes = kite.quote(keys)

            combined         = 0.0
            combined_exit    = 0.0
            for t in tracked:
                q = quotes.get(f"{t['exch']}:{t['sym']}") or {}
                t["ltp"] = float(q.get("last_price") or 0)
                t["mtm"] = _calc_mtm(t["avg"], t["ltp"], t["qty"], t.get("mult", 1))
                combined += t["mtm"]

                # Bid/ask from top of book for exit-aware MTM.
                depth = q.get("depth") or {}
                bids  = depth.get("buy")  or []
                asks  = depth.get("sell") or []
                bid = float(bids[0]["price"]) if bids and bids[0].get("price") else 0.0
                ask = float(asks[0]["price"]) if asks and asks[0].get("price") else 0.0
                # Exit price assumption: a long position exits by SELLING at the
                # best bid; a short exits by BUYING at the best ask. Fall back
                # to LTP if depth missing so exit_mtm degrades gracefully.
                if t["qty"] >= 0:
                    exit_price = bid if bid > 0 else t["ltp"]
                else:
                    exit_price = ask if ask > 0 else t["ltp"]
                t["bid"] = bid
                t["ask"] = ask
                t["exit_price"] = exit_price
                t["exit_mtm"]   = _calc_mtm(t["avg"], exit_price, t["qty"], t.get("mult", 1))
                combined_exit  += t["exit_mtm"]

            slippage = combined - combined_exit

            with _lock:
                s             = strategies[sid]
                profit_target = s["profit_target"]
                loss_limit    = s["loss_limit"]
                target_on     = bool(s.get("profit_target_enabled", True))
                sl_on         = bool(s.get("loss_limit_enabled",    True))
                target_basis  = (s.get("profit_target_basis") or "ltp").lower()
                sl_basis      = (s.get("loss_limit_basis")    or "ltp").lower()
                trail_enabled = s["trail_enabled"]
                trail_by      = s["trail_by"]
                activate_at   = s["trail_activate_at"]
                peak_mtm      = s["peak_mtm"]
            # Per-trigger basis: each check compares against either LTP MTM
            # (combined) or exit-price MTM (combined_exit). Trail SL inherits
            # the loss_limit basis since they're both downside protection.
            target_val = combined_exit if target_basis == "exit" else combined
            sl_val     = combined_exit if sl_basis     == "exit" else combined

            trail_sl = None
            if trail_enabled:
                if peak_mtm is None and sl_val >= activate_at:
                    peak_mtm = sl_val
                    _log(sid, f"Trail SL activated — peak ₹{sl_val:,.2f}")
                    _telegram(
                        f"📈 *Trail SL ACTIVATED* [{strat_name}]\n"
                        f"Activated at MTM ₹{sl_val:+,.2f}\n"
                        f"Trail SL: ₹{sl_val - trail_by:+,.2f}  (peak − ₹{trail_by:,.0f})\n"
                        f"Downside now capped."
                    )
                if peak_mtm is not None:
                    peak_mtm = max(peak_mtm, sl_val)
                    trail_sl = peak_mtm - trail_by

            with _lock:
                s            = strategies[sid]
                new_peak_day = max(s["peak_mtm_day"] or combined, combined)
                s.update({
                    "mtm":          combined,
                    "exit_mtm":     combined_exit,
                    "slippage":     slippage,
                    "positions":    [{**t} for t in tracked],
                    "peak_mtm":     peak_mtm,
                    "trail_sl":     trail_sl,
                    "peak_mtm_day": new_peak_day,
                })

            new_point = None
            # Detect externally squared-off positions (e.g., user closed in Kite app)
            if hist_tick % 60 == 0 and hist_tick > 0:
                try:
                    live = kite.positions()["net"]
                    for t in list(tracked):
                        match = next((p for p in live
                                      if p["tradingsymbol"] == t["sym"]
                                      and p["exchange"] == t["exch"]), None)
                        if not match or match["quantity"] == 0:
                            _log(sid, f"⚠ {t['sym']} no longer in positions — removing from tracking")
                            _telegram(
                                f"⚠️ *Position Disappeared* [{strat_name}]\n"
                                f"`{t['sym']}` no longer in Kite (squared off externally?)\n"
                                f"Removed from monitor. Verify on Kite."
                            )
                            tracked.remove(t)
                except Exception as e:
                    print(f"[position-check] {e}")

            if hist_tick % 5 == 0:
                new_point = _record_point(sid, combined, tracked)
            hist_tick += 1

            broadcast(new_point=new_point, sid=sid)

            # Target / SL checks honor their independent enable flags AND
            # their independent basis (LTP vs Exit-price MTM).
            trigger = None
            if target_on and target_val >= profit_target:
                trigger = f"PROFIT TARGET HIT (+₹{target_val:,.2f}, basis={target_basis})"
            elif sl_on and sl_val <= -loss_limit:
                trigger = f"LOSS LIMIT HIT (-₹{abs(sl_val):,.2f}, basis={sl_basis})"
            elif trail_sl is not None and sl_val <= trail_sl:
                trigger = f"TRAILING SL HIT at ₹{trail_sl:,.2f} (basis={sl_basis})"

            if trigger:
                _log(sid, f"*** {trigger} — placing exit orders ***")
                pos_lines = "\n".join(f"`{t['sym']}`: ₹{t['mtm']:,.2f}" for t in tracked)
                with _lock:
                    s = strategies[sid]
                    s.update({"status": "triggered", "trigger": trigger, "running": False})
                    s["sessions"].insert(0, {
                        "start":     s["monitoring_start_ts"],
                        "end":       datetime.now().isoformat(),
                        "trigger":   trigger,
                        "final_mtm": round(combined, 2),
                        "peak_mtm":  s["peak_mtm_day"],
                        "exit_positions": [{
                            "sym":  t["sym"], "exch": t["exch"],
                            "avg":  t["avg"], "qty": t["qty"],
                            "mult": t.get("mult", 1),
                        } for t in tracked],
                        "post_exit": {"active": True, "current": None,
                                      "peak": None, "trough": None,
                                      "peak_at": None, "last_update": None},
                    })
                _telegram(
                    f"⚡ *{trigger}* [{strat_name}]\n\n{pos_lines}\n"
                    f"*Combined: ₹{combined:,.2f}*\n\n🔄 Placing exit orders..."
                )
                broadcast("triggered", sid=sid)
                _exit_all([{**t} for t in tracked])
                break

        except Exception as e:
            _log(sid, f"Poll error: {e}")
            broadcast()
            if stop_ev.wait(5):
                break
            continue

        if stop_ev.wait(1):
            break

    with _lock:
        s = strategies[sid]
        if s["status"] == "monitoring":
            s.update({"status": "stopped", "running": False})
            s["sessions"].insert(0, {
                "start":     s["monitoring_start_ts"],
                "end":       datetime.now().isoformat(),
                "trigger":   "Stopped by user",
                "final_mtm": s["mtm"],
                "peak_mtm":  s["peak_mtm_day"],
            })
    broadcast()


# ── Auto-entry ────────────────────────────────────────────────────────────────
IST                = timezone(timedelta(hours=5, minutes=30))
NIFTY_INDEX_TOKEN  = 256265
_MONTH_ABBR        = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]

def _now_ist() -> datetime:
    return datetime.now(IST)

def _load_nse_holidays() -> set:
    """Load NSE holiday dates from data/nse_holidays_YYYY.json."""
    holidays = set()
    year = _now_ist().year
    # Holiday calendar is reference data — lives with the code, not per-user.
    path = os.path.join(CODE_DIR, "data", f"nse_holidays_{year}.json")
    try:
        with open(path) as f:
            data = json.load(f)
        for h in data.get("holidays", []):
            try:
                holidays.add(date.fromisoformat(h["date"]))
            except (ValueError, KeyError):
                continue
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[holidays] could not load {path}: {e}")
    return holidays

_NSE_HOLIDAYS = _load_nse_holidays()

def _is_trading_day(d: date) -> bool:
    """True if NSE was/is open on date d (not weekend, not in holiday list)."""
    return d.weekday() < 5 and d not in _NSE_HOLIDAYS

def _last_tuesday_of_month(year: int, month: int) -> date:
    """Last Tuesday of given month — NIFTY monthly expiry."""
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    last_day = nxt - timedelta(days=1)
    days_back = (last_day.weekday() - 1) % 7   # weekday(): Mon=0, Tue=1
    return last_day - timedelta(days=days_back)

def _shift_to_trading_day(d: date) -> date:
    """If d is a holiday, shift back to the most recent trading day."""
    while not _is_trading_day(d):
        d = d - timedelta(days=1)
    return d

def _current_monthly_expiry(today: date | None = None) -> date:
    """The active monthly expiry, shifted back if it lands on a holiday."""
    today = today or _now_ist().date()
    this_month_exp = _shift_to_trading_day(_last_tuesday_of_month(today.year, today.month))
    if today > this_month_exp:
        if today.month == 12:
            return _shift_to_trading_day(_last_tuesday_of_month(today.year + 1, 1))
        return _shift_to_trading_day(_last_tuesday_of_month(today.year, today.month + 1))
    return this_month_exp

def _is_monthly_expiry_day(today: date | None = None) -> bool:
    today = today or _now_ist().date()
    return today == _current_monthly_expiry(today)

def _format_kite_expiry(d: date) -> str:
    """e.g. date(2026,5,26) → '26MAY'"""
    return f"{d.year % 100:02d}{_MONTH_ABBR[d.month - 1]}"

def _is_monthly_expiry_of_its_month(d: date) -> bool:
    """True if `d` is the NIFTY monthly expiry of its calendar month
    (i.e. the last Tuesday of that month, shifted back for holidays).
    Used to pick monthly vs weekly Kite symbol format."""
    return d == _shift_to_trading_day(_last_tuesday_of_month(d.year, d.month))

# NSE/Kite weekly option symbol: month encoded as single char.
# Jan-Sep: "1"-"9"; Oct: "O"; Nov: "N"; Dec: "D".
_WEEKLY_MONTH_CHAR = {1:"1",2:"2",3:"3",4:"4",5:"5",6:"6",7:"7",8:"8",9:"9",
                      10:"O",11:"N",12:"D"}

def _nifty_option_symbol(strike: int, opt_type: str, expiry: date) -> str:
    """Build the Kite tradingsymbol for a NIFTY option. Picks the right
    Kite format based on whether `expiry` is the monthly expiry of its
    month (e.g. NIFTY26JUN23500CE) or a non-monthly weekly Tuesday
    (e.g. NIFTY2661723500CE for 17 Jun 2026)."""
    if _is_monthly_expiry_of_its_month(expiry):
        return f"NIFTY{_format_kite_expiry(expiry)}{strike}{opt_type}"
    mc = _WEEKLY_MONTH_CHAR[expiry.month]
    return f"NIFTY{expiry.year % 100:02d}{mc}{expiry.day:02d}{strike}{opt_type}"

def _current_weekly_expiry(today: date | None = None) -> date:
    """Nearest weekly NIFTY expiry (Tuesday). If today IS a Tuesday and
    trading is open, returns today; otherwise the upcoming Tuesday.
    Holidays shift the date back to the most recent trading day."""
    today = today or _now_ist().date()
    days_to_tue = (1 - today.weekday()) % 7   # Mon=0, Tue=1
    exp = today + timedelta(days=days_to_tue)
    return _shift_to_trading_day(exp)

def _next_weekly_expiry(today: date | None = None) -> date:
    """The weekly expiry AFTER the current one — i.e. next Tuesday."""
    today = today or _now_ist().date()
    cur = _current_weekly_expiry(today)
    return _shift_to_trading_day(cur + timedelta(days=7))

def _next_monthly_expiry(today: date | None = None) -> date:
    """The monthly expiry AFTER the current one."""
    today = today or _now_ist().date()
    cur = _current_monthly_expiry(today)
    nxt = cur + timedelta(days=1)
    if nxt.month == 12 + 1:
        nxt = nxt.replace(year=nxt.year + 1, month=1)
    return _shift_to_trading_day(_last_tuesday_of_month(nxt.year, nxt.month))

# Stored values for the per-strategy expiry choice. The auto-entry path
# resolves the policy at fire time so saved strategies stay correct across
# weeks (i.e. "next_weekly" always means "next week from today", not a
# frozen date from when the user saved the config).
_EXPIRY_CHOICES = ("current_weekly", "next_weekly", "current_monthly", "next_monthly")

def _resolve_expiry_choice(choice: str, today: date | None = None) -> date:
    """Resolve an expiry-policy string to an actual date.
    Falls back to next_weekly on unknown / missing values."""
    today = today or _now_ist().date()
    if choice == "current_weekly":  return _current_weekly_expiry(today)
    if choice == "current_monthly": return _current_monthly_expiry(today)
    if choice == "next_monthly":    return _next_monthly_expiry(today)
    return _next_weekly_expiry(today)   # default

def _get_prev_day_nifty_range() -> tuple[float, float]:
    """Previous trading day's high and low for NIFTY 50 spot via historical_data."""
    now = _now_ist()
    from_dt = now - timedelta(days=10)            # buffer for weekends/holidays
    to_dt   = now - timedelta(seconds=1)
    candles = kite.historical_data(NIFTY_INDEX_TOKEN, from_dt, to_dt, interval="day")
    if not candles:
        raise RuntimeError("No NIFTY historical data returned")
    today = now.date()
    past = [c for c in candles if c["date"].date() < today]
    if not past:
        raise RuntimeError("No previous trading day found")
    prev = past[-1]
    return float(prev["high"]), float(prev["low"])

def _compute_strangle_strikes(high: float, low: float) -> tuple[int, int]:
    """CE = ceil(high/50)*50, PE = floor(low/50)*50."""
    import math
    return int(math.ceil(high / 50) * 50), int(math.floor(low / 50) * 50)

def _place_entry_with_retry(sym: str, exch: str, qty: int, product: str) -> tuple[str, float]:
    """Short-sell with progressively wider LIMITs. Returns (order_id, price_used)."""
    pcts = [0.995, 0.99, 0.98]   # 0.5% → 1% → 2% below best bid
    last_err = None
    for i, pct in enumerate(pcts):
        try:
            key = f"{exch}:{sym}"
            q   = kite.quote([key])[key]
            depth = q.get("depth", {}) or {}
            bids  = depth.get("buy") or []
            anchor = bids[0]["price"] if bids else q.get("last_price", 0)
            if not anchor:
                raise RuntimeError(f"No price data for {sym}")
            price = round(round(anchor * pct / 0.05) * 0.05, 2)
            oid = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=exch,
                tradingsymbol=sym,
                transaction_type=kite.TRANSACTION_TYPE_SELL,
                quantity=qty,
                product=product,
                order_type=kite.ORDER_TYPE_LIMIT,
                price=price,
            )
            return oid, price
        except Exception as e:
            last_err = e
            if _is_ip_error(e):
                ip = _get_public_ip()
                _telegram(
                    f"🚨 *IP BLOCKED on entry*\nFor `{sym}`. IP: `{ip}`\n"
                    f"Whitelist at developers.kite.trade"
                )
                break
            print(f"[entry] {sym} attempt {i+1} @ {pct*100:.1f}% failed: {e}")
            time.sleep(0.5)
    raise RuntimeError(f"Entry failed for {sym}: {last_err}")

def _start_monitor(sid: str) -> bool:
    """Start monitor_loop for a strategy. Caller logs context-appropriate message."""
    with _lock:
        s = strategies.get(sid)
        if not s or s["running"] or not s["selected"]:
            return False
        s.update({"running": True, "status": "monitoring", "trigger": None})
    _stop_events[sid].clear()
    t = threading.Thread(target=monitor_loop, args=(sid,), daemon=True)
    _threads[sid] = t
    t.start()
    return True


def _resume_persisted_monitors():
    """Re-launch monitor threads for strategies that were `running=true` when
    the previous process exited. Sleeps briefly to let Kite + WebSocket settle.

    Run in a daemon thread spawned at module-bottom (after every function
    referenced here is defined). This fixes 'open positions go unwatched
    after Flask restart' — the bug that bit us when systemd restarted
    kite-monitor mid-day."""
    time.sleep(3)
    for sid in list(_pending_resume_sids):
        try:
            if _start_monitor(sid):
                _log(sid, "Monitor resumed after restart (persisted state)")
                print(f"[startup] Resumed monitor for {sid}")
            else:
                print(f"[startup] Could not resume {sid} (no instruments? already running?)")
        except Exception as e:
            print(f"[startup] Resume failed for {sid}: {e}")
    _pending_resume_sids.clear()

def _do_auto_entry(sid: str) -> dict:
    """Fire the strangle entry, then start monitoring."""
    with _lock:
        s = strategies.get(sid)
        if not s:
            return {"ok": False, "msg": "Strategy not found"}
        if s["running"]:
            return {"ok": False, "msg": "Already monitoring; stop first"}
        qty     = int(s.get("auto_entry_qty", 65))
        product = s.get("auto_entry_product", "MIS")
        choice  = s.get("auto_entry_expiry", "next_weekly")
        name    = s["name"]
        s["auto_entry_status"] = "firing"
    broadcast()

    today_ist = _now_ist().date()

    # Pre-entry: token sanity check
    try:
        kite.profile()
    except Exception as e:
        with _lock:
            strategies[sid]["auto_entry_status"] = f"failed:token"
        _log(sid, f"Auto-entry ABORTED — token check failed: {e}")
        _telegram(f"❌ *Auto-entry ABORTED* [{name}]\nReason: Kite token broken — {e}\nUse 🔑 button to refresh.")
        broadcast()
        return {"ok": False, "msg": f"token check failed: {e}"}

    # Pre-entry: margin warning
    try:
        m = kite.margins()
        cash = (m.get("equity", {}) or {}).get("available", {}).get("cash", 0)
        # Rough margin estimate: ~₹30k per short option leg in MIS, ~₹1.5L in NRML
        est_per_leg = 30000 if product == "MIS" else 150000
        est_total   = est_per_leg * 2  # strangle = 2 legs
        if cash < est_total * 1.2:     # need 20% headroom
            _log(sid, f"⚠ Low cash: ₹{cash:,.0f} vs ~₹{est_total:,.0f} needed for {product} strangle")
            _telegram(
                f"⚠️ *Low Margin Warning* [{name}]\n"
                f"Cash available: ₹{cash:,.0f}\n"
                f"Estimated need (~{product} strangle): ₹{est_total:,.0f}\n"
                f"Proceeding anyway — orders may fail."
            )
    except Exception as e:
        print(f"[pre-entry] margin check failed: {e}")

    if _is_monthly_expiry_day(today_ist):
        with _lock:
            strategies[sid]["auto_entry_status"] = "failed:expiry day"
        _log(sid, "Auto-entry skipped — monthly expiry day")
        _telegram(f"⏭️ *Auto-entry SKIPPED* [{name}]\nReason: monthly expiry day")
        broadcast()
        return {"ok": False, "msg": "Monthly expiry day"}

    try:
        high, low = _get_prev_day_nifty_range()
    except Exception as e:
        with _lock:
            strategies[sid]["auto_entry_status"] = f"failed:OHLC"
        _log(sid, f"Auto-entry FAILED — OHLC fetch error: {e}")
        _telegram(f"❌ *Auto-entry FAILED* [{name}]\nOHLC error: {e}")
        broadcast()
        return {"ok": False, "msg": str(e)}

    ce_strike, pe_strike = _compute_strangle_strikes(high, low)
    # Expiry honors the per-strategy choice. The policy is resolved at fire
    # time so 'next_weekly' always means 'next week from today', not a frozen
    # date saved weeks ago.
    expiry = _resolve_expiry_choice(choice, today_ist)
    ce_sym = _nifty_option_symbol(ce_strike, "CE", expiry)
    pe_sym = _nifty_option_symbol(pe_strike, "PE", expiry)

    _log(sid, f"Prev day H={high:.2f} L={low:.2f} → CE {ce_strike} | PE {pe_strike}")
    _log(sid, f"Expiry {expiry.isoformat()} ({choice}) | qty={qty} | product={product}")
    _log(sid, f"Symbols: {ce_sym}, {pe_sym}")

    legs = [(ce_sym, "NFO"), (pe_sym, "NFO")]
    def _one(leg):
        s_, e_ = leg
        try:
            oid, price = _place_entry_with_retry(s_, e_, qty, product)
            _telegram(f"✓ Leg filled: `{s_}` SELL {qty} @ ₹{price}  (order `{oid}`)")
            return s_, {"ok": True, "oid": oid, "price": price}
        except Exception as ex:
            _telegram(f"✗ Leg FAILED: `{s_}` — {ex}")
            return s_, {"ok": False, "error": str(ex)}
    results = {}
    with ThreadPoolExecutor(max_workers=2) as ex:
        for sym_, r in ex.map(_one, legs):
            results[sym_] = r

    any_ok  = any(r.get("ok") for r in results.values())
    all_ok  = all(r.get("ok") for r in results.values())

    if not any_ok:
        with _lock:
            strategies[sid]["auto_entry_status"] = "failed:both legs"
        msg = " | ".join(f"{s_}: {r.get('error','')}" for s_, r in results.items())
        _log(sid, f"Auto-entry FAILED — {msg}")
        _telegram(f"❌ *Auto-entry FAILED* [{name}]\n{msg}")
        broadcast()
        return {"ok": False, "msg": msg}

    if not all_ok:
        failed = [s_ for s_, r in results.items() if not r.get("ok")]
        _log(sid, f"Auto-entry PARTIAL — failed: {', '.join(failed)}")
        _telegram(
            f"⚠️ *Auto-entry PARTIAL* [{name}]\n"
            f"Failed leg: {', '.join(failed)}\n"
            f"Filled legs will be monitored. Handle unfilled leg manually."
        )

    selected_now = [
        {"tradingsymbol": s_, "exchange": "NFO"}
        for s_, r in results.items() if r.get("ok")
    ]
    with _lock:
        strategies[sid]["selected"]              = selected_now
        strategies[sid]["auto_entry_last_fired"] = today_ist.isoformat()
        strategies[sid]["auto_entry_status"]     = "done"
    _save_config()

    filled_str = " + ".join(f"`{s_}`@₹{r['price']}" for s_, r in results.items() if r.get("ok"))
    _telegram(
        f"✅ *Auto-entry FILLED* [{name}]\n"
        f"Prev day H={high:.2f} L={low:.2f}\n"
        f"Sold {qty}× {filled_str}\n"
        f"Monitor starting (target ₹{strategies[sid]['profit_target']:,.0f} / SL ₹{strategies[sid]['loss_limit']:,.0f})…"
    )

    time.sleep(4)               # let Kite reflect positions
    if _start_monitor(sid):
        _log(sid, "Monitoring started (post auto-entry)")
    broadcast()
    return {"ok": True, "msg": "Entry placed; monitoring started"}

_auto_entry_paused_for: str | None = None   # YYYY-MM-DD on which auto-entry is paused
                                            # Declared here (not later) so the scheduler
                                            # thread can read it on its first tick.

def _auto_entry_scheduler():
    """Check every 20s if any strategy is due for auto-entry."""
    while True:
        try:
            now       = _now_ist()
            now_hm    = now.strftime("%H:%M")
            today_iso = now.date().isoformat()
            if (9 <= now.hour <= 15
                and _is_trading_day(now.date())
                and _auto_entry_paused_for != today_iso):
                with _lock:
                    due = [
                        sid for sid, s in strategies.items()
                        if s.get("auto_entry_enabled")
                        and s.get("auto_entry_time", "10:00") <= now_hm
                        and s.get("auto_entry_last_fired") != today_iso
                        and not s.get("running")
                    ]
                for sid in due:
                    print(f"[scheduler] Firing {sid} at {now_hm} IST")
                    with _lock:                      # claim immediately to prevent double-fire
                        strategies[sid]["auto_entry_last_fired"] = today_iso
                    threading.Thread(target=_do_auto_entry, args=(sid,), daemon=True).start()
        except Exception as e:
            print(f"[scheduler] error: {e}")
        time.sleep(20)

threading.Thread(target=_auto_entry_scheduler, daemon=True).start()
threading.Thread(target=_resume_persisted_monitors, daemon=True).start()


# ── Pre-trade health check (9:00 AM IST + on-demand) ──────────────────────────
_health_check_result: dict = {"timestamp": None}   # last run snapshot

def _run_health_check(send_telegram: bool = True) -> dict:
    """Collect structured health data. Stores in module-global + optionally Telegrams it."""
    global _health_check_result
    today = _now_ist().date()
    result = {
        "timestamp":      _now_ist().isoformat(timespec="seconds"),
        "trading_day":    _is_trading_day(today),
        "expiry_day":     _is_monthly_expiry_day(today),
        "token_ok":       False,
        "user_name":      None,
        "cash_available": None,
        "public_ip":      None,
        "due_strategies": [],
        "issues":         [],
    }

    try:
        profile = kite.profile()
        result["token_ok"]  = True
        result["user_name"] = profile.get("user_name")
    except Exception as e:
        result["issues"].append(f"Kite token: {e}")

    try:
        m = kite.margins()
        cash = (m.get("equity", {}) or {}).get("available", {}).get("cash", 0)
        result["cash_available"] = round(cash, 2)
        if cash < 30000:
            result["issues"].append(f"Low cash: ₹{cash:,.0f}")
    except Exception as e:
        result["issues"].append(f"Margin fetch: {e}")

    result["public_ip"] = _get_public_ip()

    with _lock:
        for sid, s in strategies.items():
            if s.get("auto_entry_enabled"):
                result["due_strategies"].append({
                    "sid":     sid,
                    "name":    s["name"],
                    "time":    s.get("auto_entry_time", "10:00"),
                    "qty":     s.get("auto_entry_qty", 65),
                    "product": s.get("auto_entry_product", "MIS"),
                })

    _health_check_result = result

    if send_telegram:
        lines = [f"🌅 *Pre-market check* — {_now_ist().strftime('%a %d %b %Y')}"]
        lines.append(f"{'✓' if result['token_ok'] else '✗'} Kite token (user: {result['user_name'] or '—'})")
        if result["cash_available"] is not None:
            lines.append(f"Cash available: ₹{result['cash_available']:,.0f}")
        lines.append(f"Public IP: `{result['public_ip']}`")
        lines.append(f"{'✓ Trading day' if result['trading_day'] else '⏭ Holiday/weekend'}")
        if result["expiry_day"]:
            lines.append("⚠️ Monthly expiry today — auto-entry will SKIP")
        if result["due_strategies"]:
            lines.append(f"\nAuto-entry today: {len(result['due_strategies'])} strategies")
            for s in result["due_strategies"]:
                lines.append(f"  • {s['name']} @ {s['time']} ({s['qty']} qty, {s['product']})")
        if result["issues"]:
            lines.append("\n*Issues:*")
            for i in result["issues"]:
                lines.append(f"  ⚠ {i}")
        else:
            lines.append("\n✅ All checks passed")
        _telegram("\n".join(lines))

    return result

# Legacy entry-point name kept for the scheduler
def _pre_market_health_check():
    today = _now_ist().date()
    if not _is_trading_day(today):
        return
    _run_health_check(send_telegram=True)

@app.route("/health-check", methods=["GET"])
def health_check_get():
    return jsonify({"ok": True, "result": _health_check_result})

@app.route("/health-check", methods=["POST"])
def health_check_run():
    """User clicked 'Run Now'. Don't spam Telegram on manual runs."""
    send_tg = bool((request.json or {}).get("send_telegram", False))
    result  = _run_health_check(send_telegram=send_tg)
    return jsonify({"ok": True, "result": result})

def _health_check_scheduler():
    last_run = None
    while True:
        try:
            now = _now_ist()
            iso = now.date().isoformat()
            if now.hour == 9 and now.minute < 5 and last_run != iso:
                _pre_market_health_check()
                last_run = iso
        except Exception as e:
            print(f"[health-check] {e}")
        time.sleep(60)

threading.Thread(target=_health_check_scheduler, daemon=True).start()


# ── EOD summary (15:30 IST) ───────────────────────────────────────────────────
def _eod_summary():
    today = _now_ist().date()
    if not _is_trading_day(today):
        return
    iso = today.isoformat()
    today_sess = []
    with _lock:
        for sid, s in strategies.items():
            for sess in s.get("sessions", []):
                if (sess.get("start", "") or "").startswith(iso):
                    today_sess.append({"strategy": s["name"], **sess})

    lines = [f"🌇 *EOD Summary* — {_now_ist().strftime('%a %d %b %Y')}"]
    if not today_sess:
        lines.append("No trades today.")
        _telegram("\n".join(lines))
        return

    total = sum(s.get("final_mtm", 0) for s in today_sess)
    wins  = [s for s in today_sess if s.get("final_mtm", 0) > 0]
    loss  = [s for s in today_sess if s.get("final_mtm", 0) <= 0]
    lines.append(f"Trades: *{len(today_sess)}* ({len(wins)}W / {len(loss)}L)")
    lines.append(f"Net P&L: *₹{total:+,.2f}*")
    if wins: lines.append(f"Avg win:  ₹{sum(s['final_mtm'] for s in wins)/len(wins):+,.2f}")
    if loss: lines.append(f"Avg loss: ₹{sum(s['final_mtm'] for s in loss)/len(loss):+,.2f}")
    lines.append("")
    for s in today_sess:
        emoji = "🟢" if s.get("final_mtm", 0) > 0 else "🔴"
        lines.append(f"{emoji} [{s['strategy']}] {s.get('trigger','?')}: ₹{s.get('final_mtm',0):+,.2f}")
    _telegram("\n".join(lines))

def _eod_scheduler():
    last_run = None
    while True:
        try:
            now = _now_ist()
            iso = now.date().isoformat()
            if now.hour == 15 and now.minute >= 35 and last_run != iso:
                _eod_summary()
                last_run = iso
        except Exception as e:
            print(f"[eod] {e}")
        time.sleep(60)

threading.Thread(target=_eod_scheduler, daemon=True).start()


# ── NIFTY velocity alerts (sharp 5-min moves) ─────────────────────────────────
_nifty_history: list = []        # list of (datetime_ist, price)
_velocity_cooldown_until: datetime | None = None

# Pre-open call auction runs 09:00–09:15; the 09:15 open can jump 1%+ off
# the prior close as auction matches print, which is mechanical, not gamma
# risk. Skip both the pre-open window and a one-minute settle after open.
_VELOCITY_WINDOW_OPEN  = dtime(9, 16)
_VELOCITY_WINDOW_CLOSE = dtime(15, 30)


def _nifty_velocity_monitor():
    global _velocity_cooldown_until
    while True:
        try:
            now = _now_ist()
            in_window = (_VELOCITY_WINDOW_OPEN <= now.time() <= _VELOCITY_WINDOW_CLOSE)
            if in_window and _is_trading_day(now.date()):
                try:
                    q = kite.quote(["NSE:NIFTY 50"])["NSE:NIFTY 50"]
                    price = q["last_price"]
                    _nifty_history.append((now, price))
                    # Trim to last 10 min
                    cutoff = now - timedelta(minutes=10)
                    while _nifty_history and _nifty_history[0][0] < cutoff:
                        _nifty_history.pop(0)
                    # Compare to ~5 min ago
                    target_t = now - timedelta(minutes=5)
                    past = [(t, p) for t, p in _nifty_history if t <= target_t]
                    if past:
                        _, old_price = past[-1]
                        pct = (price - old_price) / old_price * 100
                        if abs(pct) >= 0.5:
                            if not _velocity_cooldown_until or now > _velocity_cooldown_until:
                                _telegram(
                                    f"⚡ *NIFTY {pct:+.2f}% in 5 min*\n"
                                    f"₹{old_price:.2f} → ₹{price:.2f}\n"
                                    f"Check positions — gamma risk elevated."
                                )
                                _velocity_cooldown_until = now + timedelta(minutes=10)
                except Exception:
                    pass
        except Exception as e:
            print(f"[velocity] {e}")
        time.sleep(60)

threading.Thread(target=_nifty_velocity_monitor, daemon=True).start()


# ── Post-exit "what-if" tracker ───────────────────────────────────────────────
def _hypothetical_mtm(exit_positions: list, ltps: dict) -> float | None:
    """Compute MTM as if positions were still held. Returns None if any LTP missing."""
    total = 0.0
    for p in exit_positions:
        key = f"{p['exch']}:{p['sym']}"
        ltp = ltps.get(key)
        if ltp is None:
            return None
        qty   = p["qty"]
        avg   = p["avg"]
        mult  = p.get("mult", 1) or 1
        eff   = abs(qty) * mult
        total += (avg - ltp) * eff if qty < 0 else (ltp - avg) * eff
    return total

def _post_exit_finalize_and_summarize():
    """At 15:30, mark all today's trackers inactive and Telegram a 'what if' summary."""
    today_iso = _now_ist().date().isoformat()
    finalized = []
    with _lock:
        for sid, s in strategies.items():
            for sess in s.get("sessions", []):
                pe = sess.get("post_exit")
                if not pe or not pe.get("active"):
                    continue
                if not (sess.get("start") or "").startswith(today_iso):
                    continue
                pe["active"] = False
                pe["eod"]    = pe.get("current")
                pe["finalized_at"] = _now_ist().isoformat(timespec="seconds")
                finalized.append({
                    "strategy": s["name"],
                    "trigger":  sess.get("trigger"),
                    "exit_mtm": sess.get("final_mtm"),
                    "peak":     pe.get("peak"),
                    "trough":   pe.get("trough"),
                    "eod":      pe.get("eod"),
                })
    if not finalized:
        return
    lines = [f"🪞 *What-if Summary* — {_now_ist().strftime('%a %d %b')}"]
    for f in finalized:
        diff = (f["eod"] or 0) - (f["exit_mtm"] or 0)
        verdict = ("✅ exit saved" if diff < 0 else "💸 left on table") if abs(diff) > 50 else "≈ right call"
        lines.append(
            f"\n*{f['strategy']}* — {f['trigger']}"
            f"\nExit: ₹{(f['exit_mtm'] or 0):+,.0f}  |  EOD: ₹{(f['eod'] or 0):+,.0f}"
            f"\nPeak ₹{(f['peak'] or 0):+,.0f}  ·  Trough ₹{(f['trough'] or 0):+,.0f}"
            f"\nDiff: ₹{diff:+,.0f}  ({verdict})"
        )
    _telegram("\n".join(lines))

def _post_exit_tracker():
    """Every 30s during market hours, refresh hypothetical MTM for all today's tracked sessions."""
    finalized_for_date: str | None = None
    while True:
        try:
            now       = _now_ist()
            today_iso = now.date().isoformat()
            in_window = (9 <= now.hour <= 15) and _is_trading_day(now.date())

            if in_window:
                tracked: list = []
                keys: set = set()
                with _lock:
                    for sid, s in strategies.items():
                        for idx, sess in enumerate(s.get("sessions", [])):
                            pe = sess.get("post_exit") or {}
                            if not pe.get("active"):
                                continue
                            if not (sess.get("start") or "").startswith(today_iso):
                                continue
                            ep = sess.get("exit_positions") or []
                            if not ep:
                                continue
                            tracked.append((sid, idx, ep))
                            for p in ep:
                                keys.add(f"{p['exch']}:{p['sym']}")

                if keys:
                    try:
                        q    = kite.ltp(list(keys))
                        ltps = {k: v.get("last_price") for k, v in q.items()}
                    except Exception as e:
                        print(f"[post-exit] LTP fetch failed: {e}")
                        ltps = {}
                    if ltps:
                        with _lock:
                            for sid, idx, ep in tracked:
                                mtm = _hypothetical_mtm(ep, ltps)
                                if mtm is None:
                                    continue
                                pe = strategies[sid]["sessions"][idx].setdefault("post_exit", {})
                                pe["current"] = round(mtm, 2)
                                if pe.get("peak") is None or mtm > pe["peak"]:
                                    pe["peak"]    = round(mtm, 2)
                                    pe["peak_at"] = now.strftime("%H:%M:%S")
                                if pe.get("trough") is None or mtm < pe["trough"]:
                                    pe["trough"] = round(mtm, 2)
                                pe["last_update"] = now.strftime("%H:%M:%S")
                        broadcast("post_exit_update")

            # Finalize once at 15:30
            if now.hour == 15 and now.minute >= 30 and finalized_for_date != today_iso:
                _post_exit_finalize_and_summarize()
                finalized_for_date = today_iso

        except Exception as e:
            print(f"[post-exit] {e}")
        time.sleep(30)

threading.Thread(target=_post_exit_tracker, daemon=True).start()


# ── EOD open-position warning (15:15 IST) ─────────────────────────────────────
def _eod_open_position_warning():
    today = _now_ist().date()
    if not _is_trading_day(today):
        return
    with _lock:
        still_running = [(sid, s) for sid, s in strategies.items() if s.get("running")]
    if not still_running:
        return
    lines = [f"🕒 *15:15 IST — Open Position Warning*"]
    for sid, s in still_running:
        lines.append(f"`{sid}` {s['name']} · MTM ₹{s.get('mtm',0):+,.2f} · product {s.get('auto_entry_product','?')}")
    lines.append("\nDecide: hold / EXIT / let MIS auto-square at 3:20")
    _telegram("\n".join(lines))

def _eod_warning_scheduler():
    last_run = None
    while True:
        try:
            now = _now_ist()
            iso = now.date().isoformat()
            if now.hour == 15 and now.minute >= 15 and now.minute < 20 and last_run != iso:
                _eod_open_position_warning()
                last_run = iso
        except Exception as e:
            print(f"[eod-warn] {e}")
        time.sleep(60)

threading.Thread(target=_eod_warning_scheduler, daemon=True).start()


# ── MIS pre-square scheduler ──────────────────────────────────────────────────
# Kite RMS auto-squares MIS positions at 15:20 IST. If our SL/target hasn't
# fired by then, RMS exits at whatever the market gives — we lose price control
# and the dashboard's recorded P&L diverges from the actual fill.
#
# This scheduler forces an exit at the configured time (default 15:15 IST,
# 5 min before RMS) for any running strategy holding MIS positions. Uses the
# existing _manual_exit_internal helper so the exit path, Telegram alert, and
# session record match a regular UI-driven exit. Only fires for strategies
# that BOTH have running=True AND have at least one MIS position currently tracked.
_mis_presquare_config = {
    "enabled": True,
    "time":    "15:15",   # HH:MM (IST). Must be strictly before 15:20.
}
_load_module_config_into("mis_presquare", _mis_presquare_config)


def _mis_presquare_dtime() -> dtime:
    """Parse the configured time string into a dtime, falling back to 15:15
    on any parse error so the safety net survives bad config."""
    s = str(_mis_presquare_config.get("time") or "15:15").strip()
    try:
        h, m = (int(x) for x in s.split(":"))
        if 0 <= h < 24 and 0 <= m < 60:
            return dtime(h, m)
    except Exception:
        pass
    return dtime(15, 15)


def _mis_presquare_scheduler():
    last_run = None
    while True:
        try:
            now   = _now_ist()
            today = now.date()
            iso   = today.isoformat()
            fire_at = _mis_presquare_dtime()
            if (_mis_presquare_config.get("enabled", True)
                and last_run != iso
                and _is_trading_day(today)
                and now.time() >= fire_at
                and now.time() < dtime(15, 30)):     # narrow window — only this slot fires
                with _lock:
                    candidates = []
                    for sid, s in strategies.items():
                        if not s.get("running"):
                            continue
                        positions = s.get("positions") or []
                        if any((p.get("product") or "").upper() == "MIS" for p in positions):
                            candidates.append((sid, s.get("name", sid)))
                for sid, name in candidates:
                    print(f"[mis-presquare] Force-exit {sid} ({name}) at {now.strftime('%H:%M:%S')}")
                    _telegram(
                        f"⏰ *MIS pre-square* [{name}]\n"
                        f"Forcing exit at {fire_at.strftime('%H:%M')} IST "
                        f"(Kite RMS at 15:20). You control the price, not RMS."
                    )
                    try:
                        _manual_exit_internal(sid, source="mis_presquare")
                    except Exception as e:
                        print(f"[mis-presquare] exit failed for {sid}: {e}")
                        _telegram(f"⚠️ MIS pre-square exit FAILED [{name}]: {e}")
                last_run = iso
        except Exception as e:
            print(f"[mis-presquare] {e}")
        time.sleep(30)


threading.Thread(target=_mis_presquare_scheduler, daemon=True).start()


@app.route("/config/mis-presquare", methods=["GET"])
def mis_presquare_get():
    """Current MIS pre-square config (enabled + HH:MM)."""
    return jsonify({"ok": True, "config": dict(_mis_presquare_config)})


@app.route("/config/mis-presquare", methods=["POST"])
def mis_presquare_set():
    """Update MIS pre-square config. Body: {enabled?: bool, time?: 'HH:MM'}.
    Time must be strictly before 15:20 IST — Kite RMS waits for no one."""
    d = request.json or {}
    if "enabled" in d:
        _mis_presquare_config["enabled"] = bool(d["enabled"])
    if "time" in d:
        v = str(d["time"]).strip()[:5]
        try:
            h, m = (int(x) for x in v.split(":"))
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError("out of range")
        except Exception:
            return jsonify({"ok": False, "error": "time must be HH:MM"}), 400
        # Hard guardrail: refuse anything >= 15:20 IST.
        if (h, m) >= (15, 20):
            return jsonify({"ok": False,
                            "error": "Time must be strictly before 15:20 IST (Kite RMS deadline)."}), 400
        _mis_presquare_config["time"] = f"{h:02d}:{m:02d}"
    _save_module_config("mis_presquare", _mis_presquare_config)
    return jsonify({"ok": True, "config": dict(_mis_presquare_config)})


# ── Telegram bot (long-polling, bidirectional) ────────────────────────────────
# Token/chat_id read from os.environ on every call so the UI's
# /config/telegram POST can hot-update them without a process restart.
def _tg_token() -> str:
    return os.getenv("TELEGRAM_TOKEN", "")

def _tg_chat_id() -> str:
    return str(os.getenv("TELEGRAM_CHAT_ID", ""))

def _tg_api() -> str:
    t = _tg_token()
    return f"https://api.telegram.org/bot{t}" if t else ""

_tg_last_update_id = 0
_tg_pending: dict = {}        # chat_id -> {action, args, expires_at}

def _tg_send_reply(text: str):
    """Send a Telegram message (Markdown). Returns bool."""
    api, chat_id = _tg_api(), _tg_chat_id()
    if not api or not chat_id:
        return False
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat_id, "text": text, "parse_mode": "Markdown"
        }).encode()
        urllib.request.urlopen(f"{api}/sendMessage", data=data, timeout=10)
        return True
    except Exception as e:
        print(f"[tg-send] {e}")
        return False

# ── Bot command handlers ──────────────────────────────────────────────────────
def _cmd_help(args, msg):
    _tg_send_reply(
        "*Commands*\n"
        "`/status` — running strategies + today's P&L\n"
        "`/strategies` — list all with status\n"
        "`/positions` — live Kite positions\n"
        "`/health` — run pre-market health check\n"
        "`/stats` — cumulative win rate + DD\n"
        "`/pnl` — today's P&L by strategy\n"
        "`/preview <sid>` — auto-entry strike preview\n"
        "`/start <sid>` — start monitor\n"
        "`/stop <sid>` — stop monitor (no exit)\n"
        "`/exit <sid|all>` — square off (confirms)\n"
        "`/pause` — disable auto-entry for today\n"
        "`/resume` — re-enable auto-entry\n"
        "`/refresh` — TOTP token refresh"
    )

def _cmd_status(args, msg):
    today_iso = _now_ist().date().isoformat()
    today_pnl = 0.0
    running   = 0
    lines = ["📊 *Status*"]
    with _lock:
        for sid, s in strategies.items():
            if s.get("running"):
                running += 1
                lines.append(
                    f"🟢 `{sid}` {s['name']} · ₹{s.get('mtm',0):+,.2f} "
                    f"(target ₹{s['profit_target']:,.0f} / SL ₹{s['loss_limit']:,.0f})"
                )
            for sess in s.get("sessions", []):
                if (sess.get("start") or "").startswith(today_iso):
                    today_pnl += sess.get("final_mtm", 0) or 0
    lines.append(f"\nToday's *closed* P&L: ₹{today_pnl:+,.2f}")
    lines.append(f"Running monitors: {running}")
    if _auto_entry_paused_for == today_iso:
        lines.append("⏸ Auto-entry PAUSED for today")
    _tg_send_reply("\n".join(lines))

def _cmd_strategies(args, msg):
    lines = ["📋 *Strategies*"]
    with _lock:
        for sid, s in strategies.items():
            st = s.get("status", "idle")
            dot = "🟢" if st == "monitoring" else "🟠" if st == "triggered" else "⚪"
            lines.append(f"{dot} `{sid}` {s['name']} · {st}")
    _tg_send_reply("\n".join(lines))

def _cmd_positions(args, msg):
    try:
        net = [p for p in kite.positions()["net"] if p.get("quantity")]
    except Exception as e:
        _tg_send_reply(f"❌ {e}")
        return
    if not net:
        _tg_send_reply("No open positions.")
        return
    lines = ["💼 *Open Positions*"]
    tot = 0.0
    for p in net:
        pnl = p.get("pnl", 0) or 0
        tot += pnl
        lines.append(f"`{p['tradingsymbol']}` {p['quantity']:+d} · ₹{pnl:+,.2f}")
    lines.append(f"\n*Total P&L: ₹{tot:+,.2f}*")
    _tg_send_reply("\n".join(lines))

def _cmd_health(args, msg):
    r = _run_health_check(send_telegram=False)
    lines = [f"🏥 *Health Check* — {r.get('timestamp','')}"]
    lines.append(f"{'✓' if r['token_ok'] else '✗'} Token (user: {r.get('user_name') or '-'})")
    if r.get("cash_available") is not None:
        lines.append(f"Cash: ₹{r['cash_available']:,.0f}")
    lines.append(f"IP: `{r.get('public_ip','?')}`")
    lines.append(f"{'✓ Trading day' if r.get('trading_day') else '⏭ Holiday/weekend'}")
    if r.get("expiry_day"):
        lines.append("⚠️ Monthly expiry")
    if r.get("issues"):
        lines.append("\n*Issues:*\n" + "\n".join(f"⚠ {i}" for i in r["issues"]))
    _tg_send_reply("\n".join(lines))

def _cmd_stats(args, msg):
    with app.test_request_context():
        rsp = get_stats().get_json()
    if not rsp.get("ok"):
        _tg_send_reply("❌ stats fetch failed"); return
    d = rsp
    _tg_send_reply(
        f"📈 *Cumulative Stats*\n"
        f"Trades: {d['total_trades']}  ({d['wins']}W / {d['losses']}L)\n"
        f"Win rate: {d['win_rate']:.0f}%\n"
        f"All-time P&L: ₹{d['total_pnl']:+,.0f}\n"
        f"Today P&L: ₹{d['today_pnl']:+,.0f} ({d['today_trades']} trades)\n"
        f"Max drawdown: ₹{d['max_drawdown']:,.0f}\n"
        f"Avg win: ₹{d['avg_win']:+,.0f} · Avg loss: ₹{d['avg_loss']:+,.0f}"
    )

def _cmd_pnl(args, msg):
    today_iso = _now_ist().date().isoformat()
    lines = ["💰 *Today's P&L*"]
    total = 0.0
    with _lock:
        for sid, s in strategies.items():
            today_sess = [x for x in s.get("sessions", []) if (x.get("start") or "").startswith(today_iso)]
            strat_pnl = sum(x.get("final_mtm", 0) or 0 for x in today_sess)
            if today_sess:
                total += strat_pnl
                lines.append(f"`{sid}` {s['name']}: ₹{strat_pnl:+,.2f} ({len(today_sess)} trades)")
            elif s.get("running"):
                live = s.get("mtm", 0)
                total += live
                lines.append(f"`{sid}` {s['name']}: ₹{live:+,.2f} (LIVE)")
    if total == 0 and len(lines) == 1:
        lines.append("No trades or live positions today.")
    else:
        lines.append(f"\n*Total: ₹{total:+,.2f}*")
    _tg_send_reply("\n".join(lines))

def _cmd_preview(args, msg):
    if not args:
        _tg_send_reply("Usage: `/preview <sid>`"); return
    sid = args[0]
    if sid not in strategies:
        _tg_send_reply(f"Unknown strategy: `{sid}`"); return
    try:
        today  = _now_ist().date()
        expiry = _current_monthly_expiry(today)
        h, l   = _get_prev_day_nifty_range()
        ce, pe = _compute_strangle_strikes(h, l)
        s = strategies[sid]
        _tg_send_reply(
            f"🔍 *Preview* `{sid}` ({s['name']})\n"
            f"Prev H/L: {h:.2f} / {l:.2f}\n"
            f"Expiry: {expiry.isoformat()}\n"
            f"CE: `{_nifty_option_symbol(ce, 'CE', expiry)}`\n"
            f"PE: `{_nifty_option_symbol(pe, 'PE', expiry)}`\n"
            f"Qty: {s.get('auto_entry_qty',65)} · Product: {s.get('auto_entry_product','MIS')}"
        )
    except Exception as e:
        _tg_send_reply(f"❌ {e}")

def _cmd_start(args, msg):
    if not args:
        _tg_send_reply("Usage: `/start <sid>`"); return
    sid = args[0]
    if sid not in strategies:
        _tg_send_reply(f"Unknown strategy: `{sid}`"); return
    if strategies[sid].get("running"):
        _tg_send_reply(f"`{sid}` already running."); return
    if not strategies[sid].get("selected"):
        _tg_send_reply(f"`{sid}` has no instruments selected."); return
    if _start_monitor(sid):
        _log(sid, "Monitoring started (via Telegram)")
        _tg_send_reply(f"▶ `{sid}` started.")
    else:
        _tg_send_reply(f"Failed to start `{sid}`.")

def _cmd_stop(args, msg):
    if not args:
        _tg_send_reply("Usage: `/stop <sid>`"); return
    sid = args[0]
    ok, m = _stop_monitor_internal(sid, source="Telegram")
    _tg_send_reply(f"{'⏹' if ok else '✗'} {m}")

def _cmd_exit(args, msg):
    if not args:
        _tg_send_reply("Usage: `/exit <sid|all>`"); return
    chat_id = str(msg.get("chat", {}).get("id"))
    tgt = args[0].lower()
    if tgt == "all":
        running = [sid for sid, s in strategies.items() if s.get("running")]
        if not running:
            _tg_send_reply("No strategies running."); return
        _tg_pending[chat_id] = {"action": "exit_all", "args": running, "expires_at": time.time() + 30}
        _tg_send_reply(f"⚠️ Exit *ALL* running strategies ({len(running)})?\nReply *yes* within 30s.")
    elif tgt in strategies:
        if not strategies[tgt].get("running"):
            _tg_send_reply(f"`{tgt}` not running."); return
        _tg_pending[chat_id] = {"action": "exit_one", "args": tgt, "expires_at": time.time() + 30}
        _tg_send_reply(f"⚠️ Exit `{tgt}` ({strategies[tgt]['name']})?\nReply *yes* within 30s.")
    else:
        _tg_send_reply(f"Unknown strategy: `{tgt}`")

def _cmd_pause(args, msg):
    global _auto_entry_paused_for
    _auto_entry_paused_for = _now_ist().date().isoformat()
    _tg_send_reply(f"⏸ Auto-entry PAUSED for today ({_auto_entry_paused_for}).")

def _cmd_resume(args, msg):
    global _auto_entry_paused_for
    _auto_entry_paused_for = None
    _tg_send_reply("▶ Auto-entry RESUMED.")

def _cmd_refresh(args, msg):
    ok, m = _auto_refresh_token_via_totp()
    _tg_send_reply(f"{'🔑' if ok else '⚠️'} {m}")

_BOT_HANDLERS = {
    "/help":       _cmd_help,
    "/status":     _cmd_status,
    "/strategies": _cmd_strategies,
    "/positions":  _cmd_positions,
    "/health":     _cmd_health,
    "/stats":      _cmd_stats,
    "/pnl":        _cmd_pnl,
    "/preview":    _cmd_preview,
    "/start":      _cmd_start,
    "/stop":       _cmd_stop,
    "/exit":       _cmd_exit,
    "/pause":      _cmd_pause,
    "/resume":     _cmd_resume,
    "/refresh":    _cmd_refresh,
}

def _handle_pending(chat_id: str, text: str) -> bool:
    """Returns True if this text was consumed by a pending confirmation."""
    p = _tg_pending.get(chat_id)
    if not p:
        return False
    if time.time() > p["expires_at"]:
        _tg_pending.pop(chat_id, None)
        return False
    low = text.lower().strip()
    if low in ("yes", "y", "confirm"):
        _tg_pending.pop(chat_id, None)
        if p["action"] == "exit_all":
            for sid in p["args"]:
                threading.Thread(target=_manual_exit_internal, args=(sid, "Telegram"), daemon=True).start()
            _tg_send_reply(f"🔴 Exit fired on {len(p['args'])} strategies.")
        elif p["action"] == "exit_one":
            threading.Thread(target=_manual_exit_internal, args=(p["args"], "Telegram"), daemon=True).start()
            _tg_send_reply(f"🔴 Exit fired on `{p['args']}`.")
        return True
    if low in ("no", "n", "cancel"):
        _tg_pending.pop(chat_id, None)
        _tg_send_reply("Cancelled.")
        return True
    return False

def _telegram_bot_loop():
    """Long-poll Telegram getUpdates. Only chat_id == TELEGRAM_CHAT_ID is allowed.

    Re-reads token/chat_id every iteration so a UI save via /config/telegram
    starts (or stops) polling without needing a process restart.
    """
    global _tg_last_update_id
    last_state = None     # "active" | "waiting"
    while True:
        api, chat_id = _tg_api(), _tg_chat_id()
        if not api or not chat_id:
            if last_state != "waiting":
                print("[tg-bot] idle — TELEGRAM_TOKEN/CHAT_ID not configured; will re-check periodically")
                last_state = "waiting"
            time.sleep(30)
            continue
        if last_state != "active":
            print(f"[tg-bot] polling started for chat_id={chat_id}")
            last_state = "active"
        try:
            params = urllib.parse.urlencode({"offset": _tg_last_update_id + 1, "timeout": 30})
            req = urllib.request.Request(f"{api}/getUpdates?{params}")
            with urllib.request.urlopen(req, timeout=40) as resp:
                payload = json.loads(resp.read().decode())
            for upd in payload.get("result", []):
                _tg_last_update_id = max(_tg_last_update_id, upd.get("update_id", 0))
                m = upd.get("message") or upd.get("edited_message") or {}
                msg_chat_id = str(m.get("chat", {}).get("id") or "")
                text = (m.get("text") or "").strip()
                if not msg_chat_id or not text:
                    continue
                if msg_chat_id != chat_id:
                    print(f"[tg-bot] unauthorized chat_id {msg_chat_id}")
                    continue
                # Pending confirmation first
                if _handle_pending(chat_id, text):
                    continue
                if not text.startswith("/"):
                    continue
                parts = text.split()
                cmd   = parts[0].split("@")[0].lower()   # strip @botname in groups
                args  = parts[1:]
                handler = _BOT_HANDLERS.get(cmd)
                if handler:
                    try:
                        handler(args, m)
                    except Exception as e:
                        _tg_send_reply(f"❌ Command error: {e}")
                else:
                    _tg_send_reply(f"Unknown command `{cmd}`. /help for list.")
        except Exception as e:
            print(f"[tg-bot] poll error: {e}")
            time.sleep(5)

threading.Thread(target=_telegram_bot_loop, daemon=True).start()


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/favicon.ico")
def favicon_legacy():
    """Browsers request /favicon.ico before parsing the HTML <link>. Redirect
    to the PNG so we don't 404 (clean log noise + faster tab icon)."""
    return redirect(url_for("static", filename="favicon.png"), code=301)


@app.route("/logout")
def logout_route():
    """Sign out for HTTP Basic Auth — best-effort.

    Previously returned 401 with a rotated realm, which caused the browser
    to pop its native auth dialog *over* this page — users saw a credentials
    prompt and assumed sign-out was broken. Now returns 200 with a clean
    page plus Clear-Site-Data so modern browsers actually clear their cache.

    Basic Auth has no programmatic logout — fully clearing the credential
    cache still requires closing all tabs. The page makes that explicit and
    offers a button that closes the tab via window.close() where allowed.
    """
    body = """<!doctype html>
<html><head><meta charset="utf-8"><title>Signed out — YASHAM</title>
<style>
  body { background:#0b0e14;color:#e8edf5;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif;margin:0;padding:60px 20px;text-align:center; }
  .card { max-width:520px;margin:0 auto;background:#161b22;border:1px solid #30363d;border-radius:10px;padding:30px; }
  h1 { color:#3fb950;font-size:22px;margin:0 0 10px 0; }
  p { color:#8b949e;font-size:13px;line-height:1.6; }
  .hint { background:rgba(227,179,65,.08);border:1px solid #e3b341;border-radius:6px;padding:12px;color:#e3b341;font-size:12px;margin-top:18px;text-align:left; }
  .btn { display:inline-block;margin:8px 4px 0;padding:10px 22px;background:transparent;border:1px solid #58a6ff;color:#58a6ff;border-radius:4px;text-decoration:none;font-size:13px;cursor:pointer;font-family:inherit; }
  .btn.red { border-color:#f85149;color:#f85149; }
  ol { color:#8b949e;font-size:12px;line-height:1.7;padding-left:20px;text-align:left; }
</style></head><body>
  <div class="card">
    <h1>✓ Signed out</h1>
    <p>Server-side session cleared. To fully sign out of this device:</p>
    <div class="hint">
      <strong>HTTP Basic Auth limitation:</strong> browsers cache credentials per-site until every tab of this site is closed. There is no programmatic logout for Basic Auth.
      <ol>
        <li>Close this tab AND every other tab pointing at this site.</li>
        <li>Or use a private/incognito window — credentials don't persist there.</li>
        <li>The next time you visit, your browser will ask for credentials again.</li>
      </ol>
    </div>
    <button class="btn red" onclick="window.close();">Close this tab</button>
    <a href="/" class="btn">Sign in again</a>
  </div>
</body></html>"""
    resp = Response(body, status=200, mimetype="text/html")
    # Clear-Site-Data is supported by Chromium-based browsers (Chrome, Edge,
    # Brave) — it instructs them to clear cache + storage. Firefox ignores it.
    # The 'executionContexts' value forces a clean reload context.
    resp.headers["Clear-Site-Data"] = '"cache", "cookies", "storage", "executionContexts"'
    resp.headers["Cache-Control"]   = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/active", methods=["GET"])
def active_route():
    """Unified view of everything that's armed / running / scanning across all
    modules. Single source of truth for the dashboard's top-of-page banner and
    the 'Active' tab. Designed to eliminate the s5/s6 surprise: every code
    path that can place orders MUST show up here.

    Response shape:
      {ok, summary: {armed, running, scanners, has_position, real_money},
       items: [ ... ]}

    Where `real_money` is the count of items that could place a real (non-paper)
    order without further user action — the headline number the banner uses.
    """
    items: list = []
    armed = running = scanners = has_position = real_money = 0

    # ─ Strategies (custom + scheduled) ─
    with _lock:
        for sid, s in strategies.items():
            sym_list = [i.get("tradingsymbol") for i in s.get("selected", [])]
            r       = bool(s.get("running"))
            ae      = bool(s.get("auto_entry_enabled"))
            ae_time = s.get("auto_entry_time", "")
            ae_last = s.get("auto_entry_last_fired")
            mtm     = s.get("mtm")
            # Concerning if it's actually doing something OR primed to do
            # something. Strategies always place REAL orders (no paper switch).
            concerning = r or ae
            if concerning:
                real_money += 1
            if ae: armed += 1
            if r:  running += 1
            items.append({
                "kind":              "strategy",
                "id":                sid,
                "name":              s.get("name", ""),
                "type":              s.get("type", "custom"),
                "running":           r,
                "status":            s.get("status", "idle"),
                "auto_entry":        ae,
                "auto_entry_time":   ae_time,
                "auto_entry_last_fired": ae_last,
                "symbols":           sym_list,
                "mtm":               mtm,
                "profit_target":     s.get("profit_target"),
                "loss_limit":        s.get("loss_limit"),
                "paper_mode":        False,   # strategies always real
                "concerning":        concerning,
            })

    # ─ Module helper: pulls (active, paper, lifecycle, has_position) safely ─
    def _mod(id_: str, name: str, cfg: dict, state: dict | None = None,
             has_pos_keys: tuple = ("position",), extra: dict | None = None):
        nonlocal armed, scanners, has_position, real_money
        active = bool(cfg.get("active"))
        paper  = bool(cfg.get("paper_mode", True))
        life   = (state or {}).get("lifecycle") if state else None
        has_pos = False
        if state:
            for k in has_pos_keys:
                v = state.get(k)
                if v: has_pos = True; break
        concerning = active or has_pos
        if active and not paper: real_money += 1
        if active and id_ == "owl":                    armed += 1
        if has_pos: has_position += 1
        item = {
            "kind":       "module",
            "id":         id_,
            "name":       name,
            "active":     active,
            "paper_mode": paper,
            "lifecycle":  life,
            "has_position": has_pos,
            "concerning": concerning,
        }
        if extra: item.update(extra)
        items.append(item)

    _mod("owl",      "Owl Method",
         _owl_config, _owl_state)
    # Calspread has per-underlying active flags — flatten
    cs_cfg_active = _calspread_config.get("active", {}) or {}
    cs_cfg_paper  = _calspread_config.get("paper_mode", {}) or {}
    for u in ("NIFTY", "BANKNIFTY"):
        u_active = bool(cs_cfg_active.get(u))
        u_paper  = bool(cs_cfg_paper.get(u, True))
        u_state  = (_calspread_state.get(u) or {}) if isinstance(_calspread_state, dict) else {}
        u_has_pos = bool(u_state.get("position"))
        if u_active and u_has_pos: has_position += 1
        if u_active and not u_paper: real_money += 1
        if u_active: scanners += 1
        items.append({
            "kind":         "module",
            "id":           f"calspread:{u}",
            "name":         f"Calendar Spread ({u})",
            "active":       u_active,
            "paper_mode":   u_paper,
            "lifecycle":    u_state.get("lifecycle"),
            "has_position": u_has_pos,
            "concerning":   u_active or u_has_pos,
        })
    return jsonify({
        "ok":      True,
        "summary": {
            "armed":         armed,           # strategies with auto_entry_enabled OR module armed
            "running":       running,         # strategies currently monitoring
            "scanners":      scanners,        # calspread active
            "has_position":  has_position,    # modules holding open positions
            "real_money":    real_money,      # would place REAL orders if triggered now
            "total_items":   len(items),
        },
        "items":   items,
    })


@app.route("/stream")
def stream():
    q: queue.Queue = queue.Queue(maxsize=30)
    with _clients_lock:
        _clients.append(q)
    broadcast()

    def generate():
        try:
            while True:
                try:
                    data = q.get(timeout=30)
                    yield f"data: {json.dumps(data)}\n\n"
                except queue.Empty:
                    yield f"data: {json.dumps({'heartbeat': True})}\n\n"
        finally:
            with _clients_lock:
                if q in _clients:
                    _clients.remove(q)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/history/<sid>")
def history(sid: str):
    with _history_lock:
        return jsonify({"points": list(_histories.get(sid, []))})

@app.route("/kite-positions")
def kite_positions():
    try:
        net    = kite.positions()["net"]
        result = [
            {
                "tradingsymbol": p["tradingsymbol"],
                "exchange":      p["exchange"],
                "quantity":      p["quantity"],
                "average_price": round(p["average_price"], 2),
                "last_price":    round(p.get("last_price", 0) or 0, 2),
                "product":       p["product"],
                "pnl":           round(p.get("pnl", 0) or 0, 2),
                "multiplier":    float(p.get("multiplier", 1) or 1),
            }
            for p in net if p["quantity"] != 0
        ]
        return jsonify({"ok": True, "positions": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ── Strategy CRUD ─────────────────────────────────────────────────────────────
def _has_scheduled_strategy() -> str | None:
    """Return the sid of the existing scheduled strategy, or None."""
    with _lock:
        for sid, s in strategies.items():
            if s.get("type") == "scheduled":
                return sid
    return None


def _symbol_key(instruments) -> frozenset:
    """Normalize a list of instrument dicts to a frozenset for set-equality
    comparison (order-independent). Bad/empty entries are dropped."""
    out = set()
    for i in (instruments or []):
        sym  = (i.get("tradingsymbol") or "").strip().upper()
        exch = (i.get("exchange")      or "").strip().upper()
        if sym and exch:
            out.add((sym, exch))
    return frozenset(out)


def _find_armed_duplicate(skip_sid, symbols, time_str: str):
    """Return sid of an existing armed strategy whose (symbols, auto_entry_time)
    match the proposed values, or None.

    Used to hard-block the 'two identical scheduled strangles fire at the same
    time' bug we hit on 2026-06-02. Pass skip_sid=None when checking a
    not-yet-created strategy (template apply, create_strategy)."""
    if not time_str:
        return None
    key = _symbol_key(symbols)
    if not key:
        return None
    norm_time = str(time_str).strip()[:5]
    with _lock:
        for sid, s in strategies.items():
            if sid == skip_sid:
                continue
            if not s.get("auto_entry_enabled"):
                continue
            if (str(s.get("auto_entry_time") or "").strip()[:5]) != norm_time:
                continue
            if _symbol_key(s.get("selected") or []) == key:
                return sid
    return None

@app.route("/strategies", methods=["POST"])
def create_strategy():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    t    = (data.get("type") or "custom").strip()
    if t not in ("custom", "scheduled"):
        return jsonify({"ok": False, "msg": f"Unknown type: {t}"})
    if t == "scheduled" and _has_scheduled_strategy():
        return jsonify({"ok": False, "msg": "A scheduled strategy already exists. Convert or delete it first."})
    sid = _new_strategy(name)
    with _lock:
        strategies[sid]["type"] = t
    _save_config()
    broadcast()
    return jsonify({"ok": True, "id": sid})

@app.route("/strategies/<sid>/set-type", methods=["POST"])
def set_strategy_type(sid: str):
    """Convert a strategy between 'custom' and 'scheduled'. Enforces singleton on scheduled."""
    if sid not in strategies:
        return jsonify({"ok": False, "msg": "Strategy not found"})
    t = ((request.json or {}).get("type") or "").strip()
    if t not in ("custom", "scheduled"):
        return jsonify({"ok": False, "msg": f"Unknown type: {t}"})
    with _lock:
        cur_type = strategies[sid].get("type", "custom")
        if cur_type == t:
            return jsonify({"ok": True, "msg": "Already that type"})
        if t == "scheduled":
            # Singleton check (excluding self)
            other = next((s_id for s_id, s in strategies.items()
                          if s.get("type") == "scheduled" and s_id != sid), None)
            if other:
                return jsonify({"ok": False, "msg": f"Another scheduled strategy exists: {strategies[other]['name']}"})
        strategies[sid]["type"] = t
    _log(sid, f"Type changed: {cur_type} → {t}")
    _save_config()
    broadcast()
    return jsonify({"ok": True, "type": t})

@app.route("/strategies/<sid>", methods=["DELETE"])
def delete_strategy(sid: str):
    if sid not in strategies:
        return jsonify({"ok": False, "msg": "Strategy not found"})
    if strategies[sid]["running"]:
        return jsonify({"ok": False, "msg": "Stop monitoring before deleting"})
    with _lock:
        del strategies[sid]
        strategy_order.remove(sid)
        # If this was the last one, auto-create a fresh default so the UI is
        # never left with zero tabs. The user can rename / re-bind it later.
        # (Previously the route hard-blocked this case with 'Cannot delete the
        # last strategy', which broke the scheduled-only deletion flow.)
        if not strategies:
            new_sid = _new_strategy("Strategy 1")
            strategies[new_sid]["selected"]      = _default_selected()
            strategies[new_sid]["profit_target"] = float(os.getenv("PROFIT_TARGET", "2500"))
            strategies[new_sid]["loss_limit"]    = float(os.getenv("LOSS_LIMIT",    "2000"))
    _stop_events.pop(sid, None)
    _threads.pop(sid, None)
    _histories.pop(sid, None)
    _csv_paths.pop(sid, None)
    _save_config()
    broadcast()
    return jsonify({"ok": True})

@app.route("/strategies/<sid>/rename", methods=["POST"])
def rename_strategy(sid: str):
    if sid not in strategies:
        return jsonify({"ok": False, "msg": "Strategy not found"})
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "msg": "Name required"})
    with _lock:
        strategies[sid]["name"] = name
    _save_config()
    broadcast()
    return jsonify({"ok": True})

# ── Instrument selection ──────────────────────────────────────────────────────
@app.route("/instruments/<sid>", methods=["POST"])
def set_instruments(sid: str):
    if sid not in strategies:
        return jsonify({"ok": False, "msg": "Strategy not found"})
    instrs = (request.json or {}).get("instruments", [])
    normalized = [
        {"tradingsymbol": i["tradingsymbol"].upper(), "exchange": i["exchange"].upper()}
        for i in instrs
    ]
    # Duplicate hard-block: only bites when this strategy is auto-armed. The
    # idle case is fine — strategies without auto_entry can share symbols
    # freely (e.g. manual entry on the same strangle).
    with _lock:
        is_armed = bool(strategies[sid].get("auto_entry_enabled"))
        ae_time  = strategies[sid].get("auto_entry_time", "10:00")
    if is_armed:
        dup = _find_armed_duplicate(sid, normalized, ae_time)
        if dup:
            return jsonify({"ok": False,
                            "msg": f"Symbols + auto-entry time match armed strategy "
                                   f"'{strategies[dup].get('name', dup)}' ({dup}). Disable that one or change the time."}), 400
    with _lock:
        strategies[sid]["selected"] = normalized
    _log(sid, f"Instruments: {', '.join(i['tradingsymbol'] for i in instrs) or 'none'}")
    _save_config()
    broadcast()
    return jsonify({"ok": True, "count": len(instrs)})

# ── Monitor control ───────────────────────────────────────────────────────────
@app.route("/start/<sid>", methods=["POST"])
def start(sid: str):
    if sid not in strategies:
        return jsonify({"ok": False, "msg": "Strategy not found"})
    s = strategies[sid]
    if s["running"]:
        return jsonify({"ok": False, "msg": "Already running"})
    if not s["selected"]:
        return jsonify({"ok": False, "msg": "No instruments selected"})
    if not _start_monitor(sid):
        return jsonify({"ok": False, "msg": "Failed to start monitor"})
    _log(sid, "Monitoring started")
    broadcast()
    return jsonify({"ok": True})

# ── Auto-entry config & manual trigger ─────────────────────────────────────────
@app.route("/auto-entry/<sid>", methods=["POST"])
def update_auto_entry(sid: str):
    if sid not in strategies:
        return jsonify({"ok": False, "msg": "Strategy not found"})
    data = request.json or {}
    # Compute the proposed end state to evaluate the duplicate hard-block.
    with _lock:
        cur = strategies[sid]
        proposed_enabled = bool(data.get("auto_entry_enabled", cur.get("auto_entry_enabled", False)))
        proposed_time    = str(data.get("auto_entry_time",    cur.get("auto_entry_time", "10:00")))[:5]
        symbols          = cur.get("selected") or []
    # Only check when the result would be 'armed'. Disabling never collides.
    if proposed_enabled:
        dup = _find_armed_duplicate(sid, symbols, proposed_time)
        if dup:
            return jsonify({"ok": False,
                            "msg": f"Another armed strategy ('{strategies[dup].get('name', dup)}' / {dup}) "
                                   f"already has the same symbols + auto-entry time. "
                                   f"Disable that one or change this time."}), 400
    with _lock:
        s = strategies[sid]
        if "auto_entry_enabled" in data:
            s["auto_entry_enabled"] = bool(data["auto_entry_enabled"])
            _log(sid, f"Auto-entry {'enabled' if s['auto_entry_enabled'] else 'disabled'}")
        if "auto_entry_time" in data:
            s["auto_entry_time"] = str(data["auto_entry_time"])[:5]   # HH:MM
        if "auto_entry_qty" in data:
            s["auto_entry_qty"] = int(data["auto_entry_qty"])
        if "auto_entry_product" in data:
            s["auto_entry_product"] = str(data["auto_entry_product"]).upper()
        if "auto_entry_expiry" in data:
            v = str(data["auto_entry_expiry"]).strip()
            if v in _EXPIRY_CHOICES:
                s["auto_entry_expiry"] = v
            else:
                return jsonify({"ok": False,
                                "msg": f"auto_entry_expiry must be one of {list(_EXPIRY_CHOICES)}"}), 400
    _save_config()
    broadcast()
    return jsonify({"ok": True})

# Resolved expiry preview — used by the Scheduled tab to show the user the
# actual date their selected policy will fire against, recomputed in IST so
# it doesn't drift across days.
@app.route("/expiry-options", methods=["GET"])
def expiry_options_route():
    today = _now_ist().date()
    out = []
    for c in _EXPIRY_CHOICES:
        d = _resolve_expiry_choice(c, today)
        out.append({
            "value":    c,
            "date":     d.isoformat(),
            "label":    d.strftime("%a %d %b %Y"),
            "is_today": d == today,
        })
    return jsonify({"ok": True, "today": today.isoformat(), "options": out})

@app.route("/trigger-entry/<sid>", methods=["POST"])
def trigger_entry(sid: str):
    if sid not in strategies:
        return jsonify({"ok": False, "msg": "Strategy not found"})
    # Run in background thread so HTTP returns quickly
    threading.Thread(target=_do_auto_entry, args=(sid,), daemon=True).start()
    return jsonify({"ok": True, "msg": "Triggered — watch logs / Telegram"})

# ── Daily token refresh from UI ───────────────────────────────────────────────
_last_token_refresh: str | None = None   # ISO timestamp of last successful refresh

def _auto_refresh_token_via_totp() -> tuple[bool, str]:
    """Headless token refresh using stored credentials + TOTP secret.
    Returns (success, message). Requires KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET in .env."""
    global _last_token_refresh
    import re as _re
    user_id     = os.getenv("KITE_USER_ID")
    password    = os.getenv("KITE_PASSWORD")
    totp_secret = os.getenv("KITE_TOTP_SECRET")
    api_secret  = os.getenv("API_SECRET")
    if not all([user_id, password, totp_secret, api_secret]):
        return False, "Missing KITE_USER_ID/PASSWORD/TOTP_SECRET in .env (auto-refresh disabled)"
    try:
        import pyotp
        import requests
    except ImportError as e:
        return False, f"pip install pyotp requests  ({e})"

    sess = requests.Session()
    try:
        # Step 1: login
        r1 = sess.post("https://kite.zerodha.com/api/login",
                       data={"user_id": user_id, "password": password}, timeout=15)
        j1 = r1.json()
        if j1.get("status") != "success":
            return False, f"login failed: {j1.get('message', j1)}"
        request_id = j1["data"]["request_id"]

        # Step 2: TOTP
        code = pyotp.TOTP(totp_secret).now()
        r2 = sess.post("https://kite.zerodha.com/api/twofa",
                       data={"user_id": user_id, "request_id": request_id,
                             "twofa_value": code, "twofa_type": "totp"}, timeout=15)
        j2 = r2.json()
        if j2.get("status") != "success":
            return False, f"TOTP failed: {j2.get('message', j2)}"

        # Step 3: follow redirects from kite.login_url() to grab request_token
        url = kite.login_url()
        while True:
            r = sess.get(url, allow_redirects=False, timeout=15)
            loc = r.headers.get("Location", "")
            if "request_token=" in loc:
                url = loc; break
            if r.status_code in (301, 302) and loc:
                url = loc; continue
            return False, "no request_token in redirect chain"
        m = _re.search(r"request_token=([^&]+)", url)
        if not m:
            return False, "request_token regex failed"
        rt = m.group(1)

        # Step 4: exchange for access_token
        sd = kite.generate_session(rt, api_secret=api_secret)
        new_token = sd["access_token"]
        env_path = os.path.join(DATA_DIR, ".env")
        set_key(env_path, "ACCESS_TOKEN", new_token)
        kite.set_access_token(new_token)
        _last_token_refresh = _now_ist().isoformat(timespec="seconds")
        return True, f"refreshed: {new_token[:8]}…{new_token[-4:]}"
    except Exception as e:
        return False, f"exception: {e}"

def _token_refresh_scheduler():
    """Try TOTP auto-refresh at 8:30 AM IST on trading days."""
    last_run = None
    while True:
        try:
            now = _now_ist()
            iso = now.date().isoformat()
            if now.hour == 8 and now.minute >= 30 and last_run != iso:
                if _is_trading_day(now.date()):
                    ok, msg = _auto_refresh_token_via_totp()
                    if ok:
                        _telegram(f"🔑 *Token auto-refreshed*\n{msg}")
                    else:
                        _telegram(f"⚠️ *Token auto-refresh skipped/failed*\n{msg}\n\nUse 🔑 button on dashboard.")
                last_run = iso
        except Exception as e:
            print(f"[token-refresh] {e}")
        time.sleep(60)

threading.Thread(target=_token_refresh_scheduler, daemon=True).start()

@app.route("/auth/login-url", methods=["GET"])
def auth_login_url():
    """Return the Kite login URL the user should open in their browser."""
    try:
        return jsonify({"ok": True, "login_url": kite.login_url(), "last_refresh": _last_token_refresh})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/auth/auto-refresh", methods=["POST"])
def auth_auto_refresh():
    """One-click headless refresh using TOTP secret in .env."""
    ok, msg = _auto_refresh_token_via_totp()
    return jsonify({"ok": ok, "msg": msg, "last_refresh": _last_token_refresh})

@app.route("/auth/status", methods=["GET"])
def auth_status_route():
    """Auto-refresh visibility: are creds set? when did it last fire?
    when will it fire next? Used by the Settings tab to show users why
    auto-refresh is or isn't working without them needing to read journals."""
    required = ("KITE_USER_ID", "KITE_PASSWORD", "KITE_TOTP_SECRET", "API_SECRET")
    missing  = [k for k in required if not os.getenv(k)]
    # Compute next scheduled fire: today 08:30 IST if in the future, else
    # the next trading day's 08:30. We don't actually re-schedule here, just
    # report what the existing _token_refresh_scheduler will do.
    now    = _now_ist()
    today  = now.date()
    target = now.replace(hour=8, minute=30, second=0, microsecond=0)
    if now >= target or not _is_trading_day(today):
        cand = today + timedelta(days=1)
        while not _is_trading_day(cand):
            cand = cand + timedelta(days=1)
        next_run = datetime.combine(cand, dtime(8, 30))
    else:
        next_run = target
    return jsonify({
        "ok":               True,
        "enabled":          not missing,           # auto-refresh runs iff all creds present
        "missing":          missing,               # human-readable list of unset env vars
        "last_refresh":     _last_token_refresh,
        "next_scheduled":   next_run.isoformat(timespec="minutes") + " IST",
        "scheduler_window": "08:30 IST on every trading day",
    })

@app.route("/auth/submit-token", methods=["POST"])
def auth_submit_token():
    """User pastes the redirect URL (containing request_token). We exchange + save it."""
    global _last_token_refresh
    import re
    data = request.json or {}
    redirect_url = (data.get("redirect_url") or "").strip()
    if not redirect_url:
        return jsonify({"ok": False, "error": "Empty URL"})
    m = re.search(r"request_token=([^&]+)", redirect_url)
    if not m:
        return jsonify({"ok": False, "error": "No request_token in URL — make sure you copied the full redirect URL"})
    request_token = m.group(1)
    try:
        api_secret = os.getenv("API_SECRET")
        if not api_secret:
            return jsonify({"ok": False, "error": "API_SECRET missing from .env"})
        session_data = kite.generate_session(request_token, api_secret=api_secret)
        new_token = session_data["access_token"]
        env_path = os.path.join(DATA_DIR, ".env")
        set_key(env_path, "ACCESS_TOKEN", new_token)
        kite.set_access_token(new_token)                 # update in-memory client — no restart needed
        _last_token_refresh = _now_ist().isoformat(timespec="seconds")
        # Sanity check: fetch profile to confirm token works
        profile = kite.profile()
        return jsonify({
            "ok": True,
            "user": profile.get("user_name", ""),
            "token_preview": f"{new_token[:8]}…{new_token[-4:]}",
            "last_refresh": _last_token_refresh,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ── Strategy templates ────────────────────────────────────────────────────────
_TEMPLATES_FILE = os.path.join(DATA_DIR, "data", "templates.json")

def _load_templates() -> list:
    try:
        with open(_TEMPLATES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return [{
            "name": "10 AM Strangle (MIS)",
            "profit_target": 2500, "loss_limit": 2000,
            "trail_enabled": False, "trail_activate_at": 500, "trail_by": 300,
            "auto_entry_enabled": True, "auto_entry_time": "10:00",
            "auto_entry_qty": 65, "auto_entry_product": "MIS",
        }]

def _save_templates(t: list):
    try:
        with open(_TEMPLATES_FILE, "w") as f:
            json.dump(t, f, indent=2)
    except Exception as e:
        print(f"[templates] save failed: {e}")

@app.route("/templates", methods=["GET"])
def list_templates():
    return jsonify({"ok": True, "templates": _load_templates()})

@app.route("/templates", methods=["POST"])
def save_template():
    """Save a strategy's current config as a named template."""
    data = request.json or {}
    sid  = data.get("sid")
    name = (data.get("name") or "").strip()
    if not name or not sid or sid not in strategies:
        return jsonify({"ok": False, "msg": "Need sid + name"})
    s = strategies[sid]
    tpl = {
        "name": name,
        "profit_target":     s["profit_target"],
        "loss_limit":        s["loss_limit"],
        "trail_enabled":     s["trail_enabled"],
        "trail_activate_at": s["trail_activate_at"],
        "trail_by":          s["trail_by"],
        "auto_entry_enabled":   s["auto_entry_enabled"],
        "auto_entry_time":      s["auto_entry_time"],
        "auto_entry_qty":       s["auto_entry_qty"],
        "auto_entry_product":   s["auto_entry_product"],
    }
    tpls = [t for t in _load_templates() if t["name"] != name]
    tpls.append(tpl)
    _save_templates(tpls)
    return jsonify({"ok": True})

@app.route("/templates/<name>", methods=["DELETE"])
def delete_template(name: str):
    tpls = [t for t in _load_templates() if t["name"] != name]
    _save_templates(tpls)
    return jsonify({"ok": True})

@app.route("/strategies/from-template", methods=["POST"])
def create_from_template():
    data = request.json or {}
    tpl_name = (data.get("template") or "").strip()
    new_name = (data.get("name") or "").strip()
    new_type = (data.get("type") or "custom").strip()
    if new_type not in ("custom", "scheduled"):
        return jsonify({"ok": False, "msg": f"Unknown type: {new_type}"})
    if new_type == "scheduled" and _has_scheduled_strategy():
        return jsonify({"ok": False, "msg": "A scheduled strategy already exists."})
    tpl = next((t for t in _load_templates() if t["name"] == tpl_name), None)
    if not tpl:
        return jsonify({"ok": False, "msg": "Template not found"})
    # Duplicate hard-block: if the template would create an armed strategy
    # whose symbols + auto_entry_time match an existing armed one, refuse.
    if tpl.get("auto_entry_enabled"):
        dup = _find_armed_duplicate(None, tpl.get("selected") or [],
                                    tpl.get("auto_entry_time") or "10:00")
        if dup:
            return jsonify({"ok": False,
                            "msg": f"This template's symbols + auto-entry time match armed "
                                   f"'{strategies[dup].get('name', dup)}' ({dup}). Disable that one first."}), 400
    sid = _new_strategy(new_name or tpl_name)
    with _lock:
        strategies[sid]["type"] = new_type
        for k, v in tpl.items():
            if k != "name" and k in strategies[sid]:
                strategies[sid][k] = v
    _save_config()
    broadcast()
    return jsonify({"ok": True, "id": sid})


# ── Cumulative stats ──────────────────────────────────────────────────────────
@app.route("/stats", methods=["GET"])
def get_stats():
    """Aggregate session stats across all strategies."""
    all_sess = []
    with _lock:
        for sid, s in strategies.items():
            for sess in s.get("sessions", []):
                all_sess.append({"sid": sid, "strategy": s["name"], **sess})

    iso_today = _now_ist().date().isoformat()
    today  = [s for s in all_sess if (s.get("start", "") or "").startswith(iso_today)]
    wins   = [s for s in all_sess if s.get("final_mtm", 0) > 0]
    losses = [s for s in all_sess if s.get("final_mtm", 0) <= 0]

    # Drawdown from chronological cumulative P&L
    sorted_sess = sorted(all_sess, key=lambda x: x.get("start") or "")
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for s in sorted_sess:
        cum += s.get("final_mtm", 0) or 0
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    n = len(all_sess)
    return jsonify({
        "ok": True,
        "total_trades":  n,
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      (len(wins) / n * 100) if n else 0,
        "total_pnl":     sum(s.get("final_mtm", 0) or 0 for s in all_sess),
        "today_pnl":     sum(s.get("final_mtm", 0) or 0 for s in today),
        "today_trades":  len(today),
        "max_drawdown":  max_dd,
        "avg_win":       (sum(s["final_mtm"] for s in wins)   / len(wins))   if wins   else 0,
        "avg_loss":      (sum(s["final_mtm"] for s in losses) / len(losses)) if losses else 0,
    })

@app.route("/preview-entry/<sid>", methods=["GET"])
def preview_entry(sid: str):
    """Dry-run: compute strikes/symbols without placing any orders.
    Honors the strategy's configured auto_entry_expiry choice."""
    if sid not in strategies:
        return jsonify({"ok": False, "msg": "Strategy not found"})
    try:
        today  = _now_ist().date()
        s = strategies[sid]
        choice = s.get("auto_entry_expiry", "next_weekly")
        expiry = _resolve_expiry_choice(choice, today)
        is_exp = _is_monthly_expiry_day(today)
        high, low = _get_prev_day_nifty_range()
        ce_strike, pe_strike = _compute_strangle_strikes(high, low)
        return jsonify({
            "ok":         True,
            "expiry_day": is_exp,
            "prev_high":  round(high, 2),
            "prev_low":   round(low, 2),
            "ce_strike":  ce_strike,
            "pe_strike":  pe_strike,
            "ce_symbol":  _nifty_option_symbol(ce_strike, "CE", expiry),
            "pe_symbol":  _nifty_option_symbol(pe_strike, "PE", expiry),
            "expiry":     expiry.isoformat(),
            "expiry_short": _format_kite_expiry(expiry),
            "qty":        s.get("auto_entry_qty", 65),
            "product":    s.get("auto_entry_product", "MIS"),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

def _stop_monitor_internal(sid: str, source: str = "user") -> tuple[bool, str]:
    """Stop monitoring without placing orders. Returns (ok, msg)."""
    if sid not in strategies:
        return False, "Strategy not found"
    with _lock:
        s = strategies[sid]
        if not s.get("running"):
            return False, "Not running"
        was_name = s["name"]
        was_mtm  = s.get("mtm", 0)
        _stop_events[sid].set()
        s.update({"running": False, "status": "stopped"})
    _log(sid, f"Stopped ({source})")
    _telegram(f"⏹ *Strategy STOPPED* [{was_name}]\nMTM at stop: ₹{was_mtm:+,.2f}\nSource: {source} · No exit orders placed.")
    broadcast()
    return True, "Stopped"

@app.route("/stop/<sid>", methods=["POST"])
def stop(sid: str):
    ok, msg = _stop_monitor_internal(sid, source="UI")
    return jsonify({"ok": ok, "msg": msg})

def _manual_exit_internal(sid: str, source: str = "UI") -> tuple[bool, str]:
    """Square off all tracked positions in a strategy. Returns (ok, msg)."""
    if sid not in strategies:
        return False, "Strategy not found"
    with _lock:
        s         = strategies[sid]
        positions = [dict(p) for p in s["positions"]]
        mtm       = s["mtm"]
        if not positions:
            return False, "No positions loaded — start monitoring first"
        s.update({"trigger": "MANUAL EXIT", "status": "triggered", "running": False})
        s["sessions"].insert(0, {
            "start":     s["monitoring_start_ts"],
            "end":       datetime.now().isoformat(),
            "trigger":   "Manual exit",
            "final_mtm": mtm,
            "peak_mtm":  s["peak_mtm_day"],
            "exit_positions": [{
                "sym":  p["sym"], "exch": p["exch"],
                "avg":  p["avg"], "qty": p["qty"],
                "mult": p.get("mult", 1),
            } for p in positions],
            "post_exit": {"active": True, "current": None,
                          "peak": None, "trough": None,
                          "peak_at": None, "last_update": None},
        })
    _stop_events[sid].set()
    _log(sid, f"Manual exit ({source}) — MTM ₹{mtm:,.2f}")
    _telegram(f"🔴 *Manual Exit* [{strategies[sid]['name']}]\nMTM: ₹{mtm:+,.2f}\nSource: {source}\n🔄 Placing orders...")
    broadcast("triggered", sid=sid)
    threading.Thread(target=_exit_all, args=(positions,), daemon=True).start()
    return True, "Exit triggered"

@app.route("/exit/<sid>", methods=["POST"])
def manual_exit(sid: str):
    ok, msg = _manual_exit_internal(sid, source="UI")
    return jsonify({"ok": ok, "msg": msg})

@app.route("/config/<sid>", methods=["POST"])
def update_config(sid: str):
    if sid not in strategies:
        return jsonify({"ok": False, "msg": "Strategy not found"})
    data     = request.json or {}
    env_path = os.path.join(DATA_DIR, ".env")
    # Snapshot before-change values for audit alert if running
    was_running = strategies[sid].get("running", False)
    before = {k: strategies[sid].get(k) for k in
              ("profit_target","loss_limit","profit_target_enabled","loss_limit_enabled",
               "trail_enabled","trail_activate_at","trail_by")}
    with _lock:
        s = strategies[sid]
        if "profit_target" in data:
            v = float(data["profit_target"])
            s["profit_target"] = v
            if sid == _sid1:
                set_key(env_path, "PROFIT_TARGET", str(v))
            _log(sid, f"Profit target → ₹{v:,.0f}")
        if "loss_limit" in data:
            v = float(data["loss_limit"])
            s["loss_limit"] = v
            if sid == _sid1:
                set_key(env_path, "LOSS_LIMIT", str(v))
            _log(sid, f"Loss limit → ₹{v:,.0f}")
        if "profit_target_enabled" in data:
            s["profit_target_enabled"] = bool(data["profit_target_enabled"])
            _log(sid, f"Profit target {'enabled' if s['profit_target_enabled'] else 'disabled'}")
        if "loss_limit_enabled" in data:
            s["loss_limit_enabled"] = bool(data["loss_limit_enabled"])
            _log(sid, f"Loss limit {'enabled' if s['loss_limit_enabled'] else 'disabled'}")
        if "profit_target_basis" in data:
            v = str(data["profit_target_basis"]).lower().strip()
            if v not in ("ltp", "exit"):
                return jsonify({"ok": False, "msg": "profit_target_basis must be 'ltp' or 'exit'"}), 400
            s["profit_target_basis"] = v
            _log(sid, f"Profit target basis → {v.upper()} MTM")
        if "loss_limit_basis" in data:
            v = str(data["loss_limit_basis"]).lower().strip()
            if v not in ("ltp", "exit"):
                return jsonify({"ok": False, "msg": "loss_limit_basis must be 'ltp' or 'exit'"}), 400
            s["loss_limit_basis"] = v
            _log(sid, f"Loss limit basis → {v.upper()} MTM (trail SL inherits)")
        if "trail_enabled" in data:
            s["trail_enabled"] = bool(data["trail_enabled"])
            if not s["trail_enabled"]:
                s["peak_mtm"] = None
                s["trail_sl"] = None
            _log(sid, f"Trailing SL {'enabled' if s['trail_enabled'] else 'disabled'}")
        if "trail_activate_at" in data:
            s["trail_activate_at"] = float(data["trail_activate_at"])
        if "trail_by" in data:
            s["trail_by"] = float(data["trail_by"])
    _save_config()
    # Mid-session change alert
    if was_running:
        s_now = strategies[sid]
        diffs = []
        for k, old in before.items():
            new = s_now.get(k)
            if new != old:
                diffs.append(f"{k}: {old} → {new}")
        if diffs:
            _telegram(f"⚙️ *Config changed mid-session* [{s_now['name']}]\n" + "\n".join(diffs))
    broadcast()
    return jsonify({"ok": True})

# ── Telegram alert configuration ──────────────────────────────────────────────
def _mask_tg_token(t: str) -> str:
    """Show only the last 4 chars so the UI can confirm 'something is set'
    without leaking the full bot token."""
    if not t:
        return ""
    if len(t) <= 8:
        return "•" * len(t)
    return ("•" * (len(t) - 4)) + t[-4:]


@app.route("/config/telegram", methods=["GET"])
def telegram_config_get():
    """Return current Telegram config (token is masked)."""
    token   = _tg_token()
    chat_id = _tg_chat_id()
    return jsonify({
        "ok":           True,
        "configured":   bool(token and chat_id),
        "token_masked": _mask_tg_token(token),
        "chat_id":      chat_id,
    })


@app.route("/config/telegram", methods=["POST"])
def telegram_config_set():
    """Save Telegram config to this instance's .env and hot-update os.environ.

    Body:
      {token: str, chat_id: str}        # set both
      {token: "", chat_id: ""}          # clear (disables alerts + bot)

    The send helper + bot loop both read os.environ on every call/iteration
    so the change takes effect immediately — no process restart needed.
    """
    d = request.json or {}
    token   = (d.get("token") or "").strip()
    chat_id = (d.get("chat_id") or "").strip()

    # Shape checks. Empty-both is a valid "clear" request, so only validate
    # when at least one field is non-empty.
    if token or chat_id:
        if not token or not chat_id:
            return jsonify({"ok": False, "error": "Both token and chat_id are required (or both empty to clear)"}), 400
        # Bot tokens look like "<digits>:<base64-ish>" — a loose check, not a strict regex
        if ":" not in token or len(token) < 20:
            return jsonify({"ok": False, "error": "Token doesn't look like a Telegram bot token (expected '<digits>:<long string>')"}), 400
        if not chat_id.lstrip("-").isdigit():
            return jsonify({"ok": False, "error": "chat_id must be a number (positive for users, negative for groups)"}), 400

    env_path = os.path.join(DATA_DIR, ".env")
    try:
        set_key(env_path, "TELEGRAM_TOKEN",   token)
        set_key(env_path, "TELEGRAM_CHAT_ID", chat_id)
    except Exception as e:
        return jsonify({"ok": False, "error": f"failed to write .env: {e}"}), 500
    # Update process env so live readers (_telegram, _tg_send_reply, bot loop)
    # see the new values without restart.
    os.environ["TELEGRAM_TOKEN"]   = token
    os.environ["TELEGRAM_CHAT_ID"] = chat_id

    return jsonify({"ok": True, "configured": bool(token and chat_id)})


@app.route("/config/telegram/test", methods=["POST"])
def telegram_config_test():
    """Send a test message using *currently configured* creds. Returns the
    actual Telegram API response so the user sees the real error (bad token,
    bad chat_id, bot not started, etc.) instead of a generic 'failed'.
    """
    token   = _tg_token()
    chat_id = _tg_chat_id()
    if not token or not chat_id:
        return jsonify({"ok": False, "error": "Telegram not configured. Save token + chat_id first."}), 400
    try:
        data = urllib.parse.urlencode({
            "chat_id":    chat_id,
            "text":       "✓ Kite dashboard test message — Telegram alerts are working.",
            "parse_mode": "Markdown",
        }).encode()
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=10) as resp:
            body = json.loads(resp.read().decode())
        if body.get("ok"):
            return jsonify({"ok": True, "message": "Test message sent. Check Telegram."})
        return jsonify({"ok": False, "error": body.get("description") or "Telegram API returned ok=false"}), 400
    except urllib.error.HTTPError as e:
        # Surface the real reason (404 = bad token, 400 = bad chat_id, etc.)
        try:
            body = json.loads(e.read().decode())
            err  = body.get("description") or str(e)
        except Exception:
            err  = f"{e.code} {e.reason}"
        return jsonify({"ok": False, "error": f"Telegram API: {err}"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"network error: {e}"}), 500


# ── Kite credentials config ───────────────────────────────────────────────────
# Read/write Kite API + auto-refresh creds from this instance's .env. Same
# masking pattern as Telegram: only the last 4 chars are surfaced to the UI
# so the field can show "saved/not saved" without leaking the secret.

_KITE_CRED_FIELDS = (
    "API_KEY", "API_SECRET", "ACCESS_TOKEN",
    "KITE_USER_ID",      # for headless auto-refresh
    "KITE_PASSWORD",
    "KITE_TOTP_SECRET",
)


def _mask_secret(v: str, show_last: int = 4) -> str:
    if not v:
        return ""
    if len(v) <= show_last + 2:
        return "•" * len(v)
    return ("•" * (len(v) - show_last)) + v[-show_last:]


@app.route("/config/kite", methods=["GET"])
def kite_creds_get():
    """Current Kite credentials, every secret masked. User_id stays plain
    (it's a public-ish broker username, not a secret on its own)."""
    out = {}
    for k in _KITE_CRED_FIELDS:
        v = os.getenv(k, "") or ""
        if k == "KITE_USER_ID":
            out[k] = {"set": bool(v), "value": v}
        else:
            out[k] = {"set": bool(v), "masked": _mask_secret(v)}
    return jsonify({"ok": True, "creds": out})


@app.route("/config/kite", methods=["POST"])
def kite_creds_set():
    """Update Kite credentials in this instance's .env. Only fields PRESENT
    in the body are touched — omitted fields are preserved untouched. To
    clear a field explicitly, send it as an empty string.

    After write we also refresh the live `kite` object's api_key/access_token
    so order placement uses the new values without a process restart."""
    d = request.json or {}
    env_path = os.path.join(DATA_DIR, ".env")
    changed = []
    try:
        for k in _KITE_CRED_FIELDS:
            if k not in d:
                continue
            v = (d.get(k) or "").strip()
            set_key(env_path, k, v)
            os.environ[k] = v
            changed.append(k)
    except Exception as e:
        return jsonify({"ok": False, "error": f"failed to write .env: {e}"}), 500

    # Hot-update the live KiteConnect client when API_KEY / ACCESS_TOKEN move,
    # so order routes use the new creds without a restart.
    try:
        if "API_KEY" in changed:
            kite.api_key = os.getenv("API_KEY", "")
        if "ACCESS_TOKEN" in changed:
            kite.set_access_token(os.getenv("ACCESS_TOKEN", ""))
    except Exception as e:
        # Non-fatal: the .env is saved; next process restart will pick up cleanly.
        print(f"[kite-creds] live update skipped: {e}")

    return jsonify({"ok": True, "changed": changed})


# ── Arbitrage (Future ↔ Synthetic Future) ─────────────────────────────────────
_ARB_UNDERLYINGS = {
    # NSE indexes — F&O on NFO segment
    "NIFTY": {
        "label":       "NIFTY 50",
        "spot_key":    "NSE:NIFTY 50",
        "spot_token":  256265,
        "fut_prefix":  "NIFTY",
        "opt_prefix":  "NIFTY",
        "exchange":    "NFO",
        "strike_step": 50,
        "lot_size":    65,
    },
    "BANKNIFTY": {
        "label":       "BANK NIFTY",
        "spot_key":    "NSE:NIFTY BANK",
        "spot_token":  260105,
        "fut_prefix":  "BANKNIFTY",
        "opt_prefix":  "BANKNIFTY",
        "exchange":    "NFO",
        "strike_step": 100,
        "lot_size":    30,
    },
    "FINNIFTY": {
        "label":       "FIN NIFTY",
        "spot_key":    "NSE:NIFTY FIN SERVICE",
        "spot_token":  257801,
        "fut_prefix":  "FINNIFTY",
        "opt_prefix":  "FINNIFTY",
        "exchange":    "NFO",
        "strike_step": 50,
        "lot_size":    60,
    },
    "MIDCPNIFTY": {
        "label":       "MIDCAP NIFTY",
        "spot_key":    "NSE:NIFTY MID SELECT",
        "spot_token":  288009,
        "fut_prefix":  "MIDCPNIFTY",
        "opt_prefix":  "MIDCPNIFTY",
        "exchange":    "NFO",
        "strike_step": 25,
        "lot_size":    120,
    },
    "NIFTYNXT50": {
        "label":       "NIFTY NEXT 50",
        "spot_key":    "NSE:NIFTY NEXT 50",
        "spot_token":  270857,
        "fut_prefix":  "NIFTYNXT50",
        "opt_prefix":  "NIFTYNXT50",
        "exchange":    "NFO",
        "strike_step": 100,
        "lot_size":    25,
    },
    # BSE indexes — F&O on BFO segment
    "SENSEX": {
        "label":       "SENSEX",
        "spot_key":    "BSE:SENSEX",
        "spot_token":  265,
        "fut_prefix":  "SENSEX",
        "opt_prefix":  "SENSEX",
        "exchange":    "BFO",
        "strike_step": 100,
        "lot_size":    20,
    },
    "BANKEX": {
        "label":       "BANKEX",
        "spot_key":    "BSE:BANKEX",
        "spot_token":  274441,
        "fut_prefix":  "BANKEX",
        "opt_prefix":  "BANKEX",
        "exchange":    "BFO",
        "strike_step": 100,
        "lot_size":    30,
    },
}

_arb_config = {
    "underlying":         "NIFTY",
    "strikes_around_atm": 3,
    "monitor_active":     False,
    "alert_pts":          3.0,            # Telegram if net edge > this
    "last_alerted_key":   None,           # to suppress spam
}
_arb_snapshot: dict = {"ts": None, "rows": [], "config": dict(_arb_config)}
_arb_positions: list = []
_arb_lock = threading.Lock()
_arb_id_ctr = 0
_arb_history: dict = {}   # key "UNDERLYING:STRIKE" -> list of {ts,spot,synth,fut}
_ARB_HISTORY_MAX = 1000   # ~50 min at 3s polling

# CSV write thinning: skip ticks where nothing meaningful changed.
_ARB_LOG_THIN_EDGE      = 0.5   # log if net_edge moved at least this much (pts)
_ARB_LOG_THIN_INTERVAL  = 30    # ...or if this many seconds passed since last log
_arb_last_logged: dict  = {}    # key "UNDERLYING:STRIKE" -> {"ts": datetime, "ne": float}

def _generic_option_symbol(prefix: str, strike: int, opt: str, expiry: date) -> str:
    return f"{prefix}{_format_kite_expiry(expiry)}{strike}{opt}"

def _future_symbol(prefix: str, expiry: date) -> str:
    return f"{prefix}{_format_kite_expiry(expiry)}FUT"

def _estimate_arb_costs(underlying: str, avg_premium: float) -> dict:
    """Round-trip cost estimate per lot, in INR and NIFTY-points equivalent."""
    u = _ARB_UNDERLYINGS[underlying]
    lot   = u["lot_size"]
    # Use spot-ish notional for future STT estimate
    fut_notional   = lot * max(1, avg_premium * 100)   # heuristic; user updates if needed
    fut_stt_sell   = fut_notional * 0.0002              # 0.02% sell side futures
    opt_stt_sell   = lot * avg_premium * 0.001          # 0.1% on premium, sell side options
    brokerage      = 6 * 20                              # 6 legs round trip, Zerodha flat
    misc           = 120                                 # GST + SEBI + exchange
    total_inr      = round(fut_stt_sell + opt_stt_sell + brokerage + misc, 2)
    return {
        "total_inr": total_inr,
        "total_pts": round(total_inr / lot, 2),
        "lot_size":  lot,
    }

def _build_basis_snapshot(underlying: str, strikes_around: int) -> dict:
    u = _ARB_UNDERLYINGS[underlying]
    today  = _now_ist().date()
    expiry = _current_monthly_expiry(today)

    # 1) Get spot to determine ATM
    spot_key = u["spot_key"]
    spot_q   = kite.ltp([spot_key])
    spot     = spot_q[spot_key]["last_price"]
    step     = u["strike_step"]
    atm      = int(round(spot / step) * step)

    strikes = [atm + i * step for i in range(-strikes_around, strikes_around + 1)]

    # 2) Build all symbols to query in ONE batch
    fut_sym = _future_symbol(u["fut_prefix"], expiry)
    fut_key = f"{u['exchange']}:{fut_sym}"
    keys = [fut_key]
    sym_map = {}
    for k in strikes:
        ce = _generic_option_symbol(u["opt_prefix"], k, "CE", expiry)
        pe = _generic_option_symbol(u["opt_prefix"], k, "PE", expiry)
        keys.append(f"{u['exchange']}:{ce}")
        keys.append(f"{u['exchange']}:{pe}")
        sym_map[k] = (ce, pe)

    quotes = kite.quote(keys)

    def best(q, side):
        d = (q.get("depth") or {}).get(side) or []
        price = d[0]["price"] if d else 0
        # If best bid/ask is 0 (e.g. market closed, empty book), fall back to LTP
        return price if price else q.get("last_price", 0)

    fut_q   = quotes.get(fut_key, {}) or {}
    fut_bid = best(fut_q, "buy")
    fut_ask = best(fut_q, "sell")
    fut_ltp = fut_q.get("last_price", 0)

    # 3) Per-strike basis + net edge
    rows = []
    total_premium_sum = 0
    for k in strikes:
        ce_sym, pe_sym = sym_map[k]
        ce_q = quotes.get(f"{u['exchange']}:{ce_sym}", {}) or {}
        pe_q = quotes.get(f"{u['exchange']}:{pe_sym}", {}) or {}
        ce_bid, ce_ask = best(ce_q, "buy"), best(ce_q, "sell")
        pe_bid, pe_ask = best(pe_q, "buy"), best(pe_q, "sell")

        # Synthetic-buy = build LONG synth = BUY CE @ ask, SELL PE @ bid → effective F = K + CE_ask - PE_bid
        synth_buy_at  = k + ce_ask - pe_bid
        # Synthetic-sell = build SHORT synth = SELL CE @ bid, BUY PE @ ask → effective F = K + CE_bid - PE_ask
        synth_sell_at = k + ce_bid - pe_ask

        # ARB-A: F is rich → SELL F at bid, BUY synth at synth_buy_at → edge = F_bid - synth_buy_at
        edge_a = fut_bid - synth_buy_at
        # ARB-B: F is cheap → BUY F at ask, SELL synth at synth_sell_at → edge = synth_sell_at - F_ask
        edge_b = synth_sell_at - fut_ask

        best_edge = max(edge_a, edge_b)
        direction = "SELL_F_BUY_SYNTH" if edge_a >= edge_b else "BUY_F_SELL_SYNTH"

        # Approx avg premium across CE+PE at this strike — used for cost estimate
        avg_prem = (ce_bid + ce_ask + pe_bid + pe_ask) / 4.0 if (ce_bid + pe_bid) else 0
        cost = _estimate_arb_costs(underlying, avg_prem)
        net_edge_pts = round(best_edge - cost["total_pts"], 2)

        rows.append({
            "strike":         k,
            "is_atm":         k == atm,
            "ce_sym":         ce_sym,
            "pe_sym":         pe_sym,
            "ce_bid":         round(ce_bid, 2),
            "ce_ask":         round(ce_ask, 2),
            "pe_bid":         round(pe_bid, 2),
            "pe_ask":         round(pe_ask, 2),
            "synth_buy_at":   round(synth_buy_at, 2),
            "synth_sell_at":  round(synth_sell_at, 2),
            "edge_sell_fut":  round(edge_a, 2),
            "edge_buy_fut":   round(edge_b, 2),
            "best_edge":      round(best_edge, 2),
            "best_direction": direction,
            "cost_pts":       cost["total_pts"],
            "cost_inr":       cost["total_inr"],
            "net_sell_pts":   round(edge_a - cost["total_pts"], 2),
            "net_buy_pts":    round(edge_b - cost["total_pts"], 2),
            "net_edge_pts":   net_edge_pts,
        })
        total_premium_sum += avg_prem

    avg_cost_pts = round(sum(r["cost_pts"] for r in rows) / max(1, len(rows)), 2)
    return {
        "ts":         _now_ist().isoformat(timespec="seconds"),
        "underlying": underlying,
        "spot":       round(spot, 2),
        "atm":        atm,
        "expiry":     expiry.isoformat(),
        "fut_sym":    fut_sym,
        "fut_bid":    round(fut_bid, 2),
        "fut_ask":    round(fut_ask, 2),
        "fut_ltp":    round(fut_ltp, 2),
        "lot_size":   u["lot_size"],
        "strike_step": u["strike_step"],
        "exchange":   u["exchange"],
        "cost_pts":   avg_cost_pts,
        "rows":       rows,
    }

def _arb_csv_log(snap: dict):
    """Append snapshot to data/csv/arb_<underlying>_<YYYYMMDD>.csv.

    Thinned: a strike's row is written only if its net_edge moved at least
    _ARB_LOG_THIN_EDGE pts since last write OR _ARB_LOG_THIN_INTERVAL seconds
    passed. Cuts a ~6MB/day file down to a few hundred KB while preserving
    every meaningful move."""
    try:
        u    = snap["underlying"]
        d    = _now_ist().strftime("%Y%m%d")
        path = os.path.join(_CSV_DIR, f"arb_{u}_{d}.csv")
        write_header = not os.path.exists(path)
        now = _now_ist()
        rows_to_write = []
        for r in snap["rows"]:
            key  = f"{u}:{r['strike']}"
            last = _arb_last_logged.get(key)
            ne   = float(r["net_edge_pts"])
            if (last is None
                or (now - last["ts"]).total_seconds() >= _ARB_LOG_THIN_INTERVAL
                or abs(ne - last["ne"]) >= _ARB_LOG_THIN_EDGE):
                rows_to_write.append(r)
                _arb_last_logged[key] = {"ts": now, "ne": ne}
        if not rows_to_write:
            return
        with open(path, "a", newline="") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(["ts","underlying","spot","atm","fut_bid","fut_ask",
                            "strike","ce_bid","ce_ask","pe_bid","pe_ask",
                            "synth_buy_at","synth_sell_at","edge_sell_fut",
                            "edge_buy_fut","best_edge","cost_pts","net_edge_pts"])
            for r in rows_to_write:
                w.writerow([snap["ts"], snap["underlying"], snap["spot"], snap["atm"],
                            snap["fut_bid"], snap["fut_ask"],
                            r["strike"], r["ce_bid"], r["ce_ask"], r["pe_bid"], r["pe_ask"],
                            r["synth_buy_at"], r["synth_sell_at"], r["edge_sell_fut"],
                            r["edge_buy_fut"], r["best_edge"], r["cost_pts"], r["net_edge_pts"]])
    except Exception as e:
        print(f"[arb-csv] {e}")

_ARB_ALERT_MIN_TICKS = 3   # require this many consecutive ticks above threshold
_arb_alert_streak: dict = {}   # key "U|K|dir" -> consecutive count

def _arb_check_alert(snap: dict):
    """Telegram if a strike's net edge holds above the threshold for
    _ARB_ALERT_MIN_TICKS consecutive ticks (cooldown by key).

    Single-tick spikes are filtered out — those tend to be stale-quote
    artifacts, not executable opportunities."""
    threshold = _arb_config.get("alert_pts", 3.0)
    best = max(snap["rows"], key=lambda r: r["net_edge_pts"], default=None)
    if not best or best["net_edge_pts"] < threshold:
        # nothing above threshold this tick — break every active streak
        _arb_alert_streak.clear()
        return
    key = f"{snap['underlying']}|{best['strike']}|{best['best_direction']}"
    # increment this key's streak, drop any others (best moved or disappeared)
    new_count = _arb_alert_streak.get(key, 0) + 1
    _arb_alert_streak.clear()
    _arb_alert_streak[key] = new_count
    if new_count < _ARB_ALERT_MIN_TICKS:
        return
    if _arb_config.get("last_alerted_key") == key:
        return
    _arb_config["last_alerted_key"] = key
    _telegram(
        f"🎯 *Arb opportunity* — {snap['underlying']} K={best['strike']}\n"
        f"Direction: {best['best_direction']}\n"
        f"Gross edge: {best['best_edge']:+.2f} pts\n"
        f"Cost: {best['cost_pts']:.2f} pts\n"
        f"Net edge: *{best['net_edge_pts']:+.2f} pts*  (~₹{best['net_edge_pts']*snap['lot_size']:+,.0f} per lot)\n"
        f"Held for {new_count} consecutive ticks"
    )

def _record_arb_history(snap: dict):
    """Append a tick to per-strike time series; trim to ring-buffer size."""
    ts = (snap.get("ts") or "")[11:19]   # HH:MM:SS
    if not ts:
        return
    fut_mid = (snap["fut_bid"] + snap["fut_ask"]) / 2.0
    with _arb_lock:
        for r in snap["rows"]:
            key  = f"{snap['underlying']}:{r['strike']}"
            mid  = round((r["synth_buy_at"] + r["synth_sell_at"]) / 2.0, 2)
            arr  = _arb_history.setdefault(key, [])
            arr.append({"ts": ts, "spot": snap["spot"], "synth": mid, "fut": round(fut_mid, 2)})
            if len(arr) > _ARB_HISTORY_MAX:
                del arr[: len(arr) - _ARB_HISTORY_MAX]

_ARB_MARKET_OPEN  = dtime(9, 15)
_ARB_MARKET_CLOSE = dtime(15, 30)

_ARB_AUCTION_PAUSE_FROM = dtime(14, 59)   # pause arb polls during last minute
_ARB_AUCTION_PAUSE_TO   = dtime(15, 0)    #   of the auction window (1s polling there)

def _arb_monitor_loop():
    """Polls at wall-clock seconds 0, 3, 6, 9, ... so we don't collide every
    tick with the auctions book poll (which uses seconds 1, 6, 11, ...).

    Also pauses entirely in the final minute of the auction window when the
    auctions loop ramps to 1-second polling — keeps us safely under Kite's
    1 quote-req/sec per-app limit."""
    while True:
        try:
            now = _now_ist()
            now_t = now.time()
            in_auction_burst = (_ARB_AUCTION_PAUSE_FROM <= now_t < _ARB_AUCTION_PAUSE_TO)
            should_poll = (
                _arb_config.get("monitor_active")
                and _is_trading_day(now.date())
                and _ARB_MARKET_OPEN <= now_t < _ARB_MARKET_CLOSE
                and not in_auction_burst
                and now.second % 3 == 0       # aligned slot: 0,3,6,9,...
            )
            if should_poll:
                u = _arb_config["underlying"]
                sw = int(_arb_config["strikes_around_atm"])
                snap = _build_basis_snapshot(u, sw)
                with _arb_lock:
                    _arb_snapshot.clear()
                    _arb_snapshot.update(snap)
                    _arb_snapshot["config"] = dict(_arb_config)
                _arb_csv_log(snap)
                _record_arb_history(snap)
                _arb_check_alert(snap)
                broadcast("arb_update")
        except Exception as e:
            print(f"[arb-monitor] {e}")
        time.sleep(1)

threading.Thread(target=_arb_monitor_loop, daemon=True).start()

# ── Arb execute + unwind (Phase 3-4) ──────────────────────────────────────────
def _execute_arb_internal(underlying: str, strike: int, direction: str, lots: int = 1) -> dict:
    """Fire 3-leg order (future + CE + PE). Rolls back filled legs if any leg fails.
    direction: 'SELL_F_BUY_SYNTH' or 'BUY_F_SELL_SYNTH'."""
    global _arb_id_ctr
    u = _ARB_UNDERLYINGS.get(underlying)
    if not u:
        return {"ok": False, "msg": "Unknown underlying"}
    expiry = _current_monthly_expiry(_now_ist().date())
    fut_sym = _future_symbol(u["fut_prefix"], expiry)
    ce_sym  = _generic_option_symbol(u["opt_prefix"], strike, "CE", expiry)
    pe_sym  = _generic_option_symbol(u["opt_prefix"], strike, "PE", expiry)
    qty     = u["lot_size"] * lots

    # Decide per-leg side
    if direction == "SELL_F_BUY_SYNTH":
        legs = [
            ("fut", fut_sym, "SELL"),
            ("ce",  ce_sym,  "BUY"),
            ("pe",  pe_sym,  "SELL"),
        ]
    elif direction == "BUY_F_SELL_SYNTH":
        legs = [
            ("fut", fut_sym, "BUY"),
            ("ce",  ce_sym,  "SELL"),
            ("pe",  pe_sym,  "BUY"),
        ]
    else:
        return {"ok": False, "msg": f"Bad direction {direction}"}

    exch = u["exchange"]

    def _place_one(leg):
        tag, sym, side = leg
        try:
            price = _exit_limit_price(sym, exch, side, 0)
            oid = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=exch,
                tradingsymbol=sym,
                transaction_type=side,
                quantity=qty,
                product="NRML",                      # carry to expiry
                order_type=kite.ORDER_TYPE_LIMIT,
                price=price,
            )
            return tag, {"ok": True, "sym": sym, "side": side, "price": price, "oid": oid}
        except Exception as e:
            return tag, {"ok": False, "sym": sym, "side": side, "error": str(e)}

    with ThreadPoolExecutor(max_workers=3) as ex:
        results = dict(ex.map(_place_one, legs))

    failed = [tag for tag, r in results.items() if not r.get("ok")]
    if failed:
        # Rollback: cancel any pending + reverse any filled
        time.sleep(2)
        try:
            orders = {str(o["order_id"]): o for o in kite.orders()}
        except Exception:
            orders = {}
        rollback_msgs = []
        for tag, r in results.items():
            if not r.get("ok"):
                continue
            oid = r["oid"]
            o = orders.get(str(oid), {})
            if o.get("status") == "COMPLETE":
                # reverse this leg
                rev_side = "BUY" if r["side"] == "SELL" else "SELL"
                try:
                    kite.place_order(
                        variety=kite.VARIETY_REGULAR,
                        exchange=exch, tradingsymbol=r["sym"],
                        transaction_type=rev_side, quantity=qty, product="NRML",
                        order_type=kite.ORDER_TYPE_MARKET, price=0,
                    )
                    rollback_msgs.append(f"reversed {tag}")
                except Exception as e:
                    rollback_msgs.append(f"REVERSAL FAILED {tag}: {e}")
            elif o.get("status") in ("OPEN", "TRIGGER PENDING"):
                try:
                    kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=oid)
                    rollback_msgs.append(f"cancelled {tag}")
                except Exception as e:
                    rollback_msgs.append(f"CANCEL FAILED {tag}: {e}")
        msg = f"Arb FAILED — failed legs: {failed}. Rollback: {rollback_msgs or 'nothing to rollback'}"
        _telegram(f"❌ *Arb FAILED* {underlying} K={strike}\n{msg}")
        return {"ok": False, "msg": msg}

    # All filled — record position
    _arb_id_ctr += 1
    aid = f"a{_arb_id_ctr}"
    pos = {
        "id":         aid,
        "underlying": underlying,
        "exchange":   exch,
        "strike":     strike,
        "direction":  direction,
        "qty":        qty,
        "lots":       lots,
        "entry_ts":   _now_ist().isoformat(timespec="seconds"),
        "expiry":     expiry.isoformat(),
        "legs":       results,
        "status":     "open",
        "entry_basis": None,
        "current_basis": None,
    }
    # Compute entry basis from leg prices
    fut_p = results["fut"]["price"]
    ce_p  = results["ce"]["price"]
    pe_p  = results["pe"]["price"]
    if direction == "SELL_F_BUY_SYNTH":
        # We sold F at fut_p, bought synth at K + CE_ask - PE_bid (≈ K + ce_p - pe_p)
        entry_basis = fut_p - (strike + ce_p - pe_p)
    else:
        # We bought F at fut_p, sold synth at K + CE_bid - PE_ask
        entry_basis = (strike + ce_p - pe_p) - fut_p
    pos["entry_basis"] = round(entry_basis, 2)

    with _arb_lock:
        _arb_positions.append(pos)

    _telegram(
        f"✅ *Arb OPENED* `{aid}` {underlying} K={strike}\n"
        f"Direction: {direction}\n"
        f"F @ ₹{fut_p}  ·  CE @ ₹{ce_p}  ·  PE @ ₹{pe_p}\n"
        f"Entry basis: {entry_basis:+.2f} pts  (~₹{entry_basis*qty:+,.0f})"
    )
    broadcast("arb_position_opened")
    return {"ok": True, "id": aid, "entry_basis": entry_basis}

def _unwind_arb_internal(aid: str) -> dict:
    """Square off all 3 legs of an open arb position."""
    with _arb_lock:
        pos = next((p for p in _arb_positions if p["id"] == aid), None)
    if not pos:
        return {"ok": False, "msg": "Arb position not found"}
    if pos["status"] != "open":
        return {"ok": False, "msg": "Already closed"}

    legs = pos["legs"]
    qty  = pos["qty"]
    exch = pos["exchange"]

    def _close_one(tag):
        r = legs[tag]
        sym = r["sym"]
        # Reverse the entry side
        rev_side = "BUY" if r["side"] == "SELL" else "SELL"
        try:
            price = _exit_limit_price(sym, exch, rev_side, 0)
            oid = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=exch, tradingsymbol=sym,
                transaction_type=rev_side, quantity=qty, product="NRML",
                order_type=kite.ORDER_TYPE_LIMIT, price=price,
            )
            return tag, {"ok": True, "side": rev_side, "price": price, "oid": oid}
        except Exception as e:
            return tag, {"ok": False, "error": str(e)}

    with ThreadPoolExecutor(max_workers=3) as ex:
        exit_results = dict(ex.map(_close_one, ["fut", "ce", "pe"]))

    with _arb_lock:
        pos["status"]       = "closed"
        pos["exit_ts"]      = _now_ist().isoformat(timespec="seconds")
        pos["exit_legs"]    = exit_results
        # Compute realized basis
        ep = exit_results
        if ep["fut"].get("ok") and ep["ce"].get("ok") and ep["pe"].get("ok"):
            fut_p = ep["fut"]["price"]; ce_p = ep["ce"]["price"]; pe_p = ep["pe"]["price"]
            if pos["direction"] == "SELL_F_BUY_SYNTH":
                exit_basis = fut_p - (pos["strike"] + ce_p - pe_p)
            else:
                exit_basis = (pos["strike"] + ce_p - pe_p) - fut_p
            pos["exit_basis"]     = round(exit_basis, 2)
            pos["realized_pts"]   = round(pos["entry_basis"] - exit_basis, 2)
            pos["realized_inr"]   = round(pos["realized_pts"] * qty, 2)

    _telegram(
        f"🔻 *Arb UNWOUND* `{aid}`\n"
        f"Entry basis: {pos.get('entry_basis')} pts\n"
        f"Exit basis:  {pos.get('exit_basis')} pts\n"
        f"Realized: {pos.get('realized_pts')} pts  (₹{pos.get('realized_inr')})"
    )
    broadcast("arb_position_closed")
    return {"ok": True, "result": pos}

def _update_arb_position_basis():
    """Background refresh: for each open arb position, compute current basis."""
    with _arb_lock:
        open_pos = [p for p in _arb_positions if p["status"] == "open"]
    if not open_pos:
        return
    # Build a single batch quote
    keys = set()
    for p in open_pos:
        for tag in ("fut", "ce", "pe"):
            keys.add(f"{p['exchange']}:{p['legs'][tag]['sym']}")
    try:
        q = kite.quote(list(keys))
    except Exception:
        return
    def ltp(p, tag):
        return q.get(f"{p['exchange']}:{p['legs'][tag]['sym']}", {}).get("last_price", 0)
    with _arb_lock:
        for p in open_pos:
            fut_p = ltp(p, "fut"); ce_p = ltp(p, "ce"); pe_p = ltp(p, "pe")
            if p["direction"] == "SELL_F_BUY_SYNTH":
                cur = fut_p - (p["strike"] + ce_p - pe_p)
            else:
                cur = (p["strike"] + ce_p - pe_p) - fut_p
            p["current_basis"] = round(cur, 2)
            p["unrealized_pts"] = round(p["entry_basis"] - cur, 2)
            p["unrealized_inr"] = round(p["unrealized_pts"] * p["qty"], 2)

def _arb_position_tracker():
    while True:
        try:
            now = _now_ist()
            if _is_trading_day(now.date()) and 9 <= now.hour <= 15:
                _update_arb_position_basis()
                broadcast("arb_positions_update")
        except Exception as e:
            print(f"[arb-track] {e}")
        time.sleep(5)

threading.Thread(target=_arb_position_tracker, daemon=True).start()

# ── Arb routes ────────────────────────────────────────────────────────────────
@app.route("/arb/config", methods=["GET", "POST"])
def arb_config_route():
    if request.method == "POST":
        data = request.json or {}
        with _arb_lock:
            if "underlying" in data and data["underlying"] in _ARB_UNDERLYINGS:
                _arb_config["underlying"] = data["underlying"]
            if "strikes_around_atm" in data:
                _arb_config["strikes_around_atm"] = max(1, min(10, int(data["strikes_around_atm"])))
            if "alert_pts" in data:
                _arb_config["alert_pts"] = float(data["alert_pts"])
            if "monitor_active" in data:
                _arb_config["monitor_active"] = bool(data["monitor_active"])
            _arb_config["last_alerted_key"] = None
        return jsonify({"ok": True, "config": dict(_arb_config)})
    return jsonify({
        "ok": True,
        "config": dict(_arb_config),
        "underlyings": [{"key": k, **v} for k, v in _ARB_UNDERLYINGS.items()],
    })

@app.route("/arb/snapshot", methods=["GET"])
def arb_snapshot_route():
    with _arb_lock:
        return jsonify({"ok": True, "snapshot": dict(_arb_snapshot)})

@app.route("/arb/positions", methods=["GET"])
def arb_positions_route():
    with _arb_lock:
        return jsonify({"ok": True, "positions": list(_arb_positions)})

@app.route("/arb/history", methods=["GET"])
def arb_history_route():
    u = request.args.get("underlying", _arb_config["underlying"])
    k = request.args.get("strike", "")
    with _arb_lock:
        hist = list(_arb_history.get(f"{u}:{k}", []))
    return jsonify({"ok": True, "history": hist})

@app.route("/arb/analyze", methods=["GET"])
def arb_analyze_route():
    """Compact JSON summary of a day's arb CSV.

    Query params:
      underlying  default = current monitor underlying
      date        YYYYMMDD, default = today (IST)
      threshold   net-edge pts to count an opportunity, default = alert_pts

    Returns ~10KB even if the CSV is 6MB. Designed so the dashboard (or you)
    can ingest the summary without loading the raw CSV.
    """
    u    = request.args.get("underlying", _arb_config["underlying"])
    date = request.args.get("date", _now_ist().strftime("%Y%m%d"))
    try:
        threshold = float(request.args.get("threshold",
                                           _arb_config.get("alert_pts", 3.0)))
    except (TypeError, ValueError):
        threshold = float(_arb_config.get("alert_pts", 3.0))
    path = os.path.join(_CSV_DIR, f"arb_{u}_{date}.csv")
    if not os.path.exists(path):
        return jsonify({"ok": False,
                        "error": f"file not found: arb_{u}_{date}.csv"}), 404

    per_strike: dict = {}
    spot_vals: list  = []
    fut_basis_vals: list = []
    opps_sell: list  = []
    opps_buy:  list  = []
    first_ts = last_ts = None
    total    = 0

    with open(path, "r", newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            total += 1
            ts = row.get("ts", "")
            if first_ts is None:
                first_ts = ts
            last_ts = ts
            try:
                spot      = float(row["spot"])
                strike    = int(row["strike"])
                fut_bid   = float(row["fut_bid"])
                fut_ask   = float(row["fut_ask"])
                edge_sell = float(row["edge_sell_fut"])
                edge_buy  = float(row["edge_buy_fut"])
                cost      = float(row["cost_pts"])
            except (KeyError, ValueError, TypeError):
                continue
            spot_vals.append(spot)
            fut_mid = (fut_bid + fut_ask) / 2.0
            fut_basis_vals.append(fut_mid - spot)
            ne_sell = edge_sell - cost
            ne_buy  = edge_buy  - cost
            ps = per_strike.setdefault(strike, {
                "ticks": 0,
                "ne_sell_max":  -1e9, "ne_buy_max":  -1e9,
                "ne_sell_sum":   0.0, "ne_buy_sum":   0.0,
                "above_sell":      0, "above_buy":      0,
                "best_sell": None, "best_buy": None,
            })
            ps["ticks"]        += 1
            ps["ne_sell_sum"]  += ne_sell
            ps["ne_buy_sum"]   += ne_buy
            if ne_sell > ps["ne_sell_max"]:
                ps["ne_sell_max"] = ne_sell
                ps["best_sell"]   = {"ts": ts, "ne": round(ne_sell, 2),
                                     "spot": spot}
            if ne_buy > ps["ne_buy_max"]:
                ps["ne_buy_max"]  = ne_buy
                ps["best_buy"]    = {"ts": ts, "ne": round(ne_buy, 2),
                                     "spot": spot}
            if ne_sell >= threshold:
                ps["above_sell"] += 1
                opps_sell.append({"ts": ts, "strike": strike,
                                  "ne": round(ne_sell, 2)})
            if ne_buy >= threshold:
                ps["above_buy"]  += 1
                opps_buy.append({"ts": ts, "strike": strike,
                                 "ne": round(ne_buy, 2)})

    out_strikes = []
    for k in sorted(per_strike):
        ps = per_strike[k]
        out_strikes.append({
            "strike":          k,
            "ticks":           ps["ticks"],
            "ne_sell_max":     round(ps["ne_sell_max"], 2),
            "ne_buy_max":      round(ps["ne_buy_max"],  2),
            "ne_sell_mean":    round(ps["ne_sell_sum"] / ps["ticks"], 2),
            "ne_buy_mean":     round(ps["ne_buy_sum"]  / ps["ticks"], 2),
            "above_sell":      ps["above_sell"],
            "above_buy":       ps["above_buy"],
            "best_sell":       ps["best_sell"],
            "best_buy":        ps["best_buy"],
        })

    def _stats(arr):
        if not arr:
            return {"min": 0, "max": 0, "open": 0, "close": 0, "mean": 0}
        return {"min":   round(min(arr), 2),  "max":   round(max(arr), 2),
                "open":  round(arr[0],   2),  "close": round(arr[-1],  2),
                "mean":  round(sum(arr) / len(arr), 2)}

    try:
        size_kb = round(os.path.getsize(path) / 1024.0, 1)
    except OSError:
        size_kb = None

    return jsonify({
        "ok":            True,
        "file":          os.path.basename(path),
        "file_size_kb":  size_kb,
        "underlying":    u,
        "date":          date,
        "threshold":     threshold,
        "total_rows":    total,
        "first_ts":      first_ts,
        "last_ts":       last_ts,
        "spot":          _stats(spot_vals),
        "fut_basis":     _stats(fut_basis_vals),
        "per_strike":    out_strikes,
        "opps_sell_count": len(opps_sell),
        "opps_buy_count":  len(opps_buy),
        "top_sell_opps":  sorted(opps_sell, key=lambda x: -x["ne"])[:20],
        "top_buy_opps":   sorted(opps_buy,  key=lambda x: -x["ne"])[:20],
    })

@app.route("/arb/execute", methods=["POST"])
def arb_execute_route():
    d = request.json or {}
    underlying = d.get("underlying", _arb_config["underlying"])
    strike     = int(d.get("strike"))
    direction  = d.get("direction")
    lots       = int(d.get("lots", 1))
    threading.Thread(target=_execute_arb_internal,
                     args=(underlying, strike, direction, lots),
                     daemon=True).start()
    return jsonify({"ok": True, "msg": "Execution started — watch Telegram"})

@app.route("/arb/unwind/<aid>", methods=["POST"])
def arb_unwind_route(aid: str):
    threading.Thread(target=_unwind_arb_internal, args=(aid,), daemon=True).start()
    return jsonify({"ok": True, "msg": "Unwind started — watch Telegram"})


# ── Owl Method (1.5%-OTM monthly strangle, intraday) ──────────────────────────
#
# Strategy: at configured entry time (default 09:20 IST), short:
#   CE strike = ceil ( anchor * 1.015 / 50 ) * 50    # round UP   (more OTM)
#   PE strike = floor( anchor * 0.985 / 50 ) * 50    # round DOWN (more OTM)
# where `anchor` = NIFTY spot LTP at entry trigger (NOT the official open).
# On NIFTY monthly expiry. Per-leg SL ₹2,000 (independent). Other leg keeps
# running until exit_time (default 15:00 IST). Skip monthly expiry day.
# Paper-trade mode supported (default ON until user explicitly switches).
#
# Rationale (from Mahesh Chandra Kaushik's "Ullu Vidhi"): the residual move
# after gap-open + first-hour churn rarely exceeds ±1.5%. The transcript uses
# open price because his backtests need a fixed historical reference; for live
# execution, CMP at the moment the trader actually enters is the right anchor.

_NIFTY_LOT       = 65                  # current lot size as of 2026
_OWL_STRIKE_STEP = 50                  # NIFTY strike interval

_OWL_CONFIG_PATH = os.path.join(DATA_DIR, "data", "owl_config.json")
_OWL_STATE_PATH  = os.path.join(DATA_DIR, "data", "owl_state.json")

_owl_config = {
    "active":       False,             # master kill switch (scheduler obeys)
    "paper_mode":   True,               # default paper until user opts into live
    "entry_time":   "09:20",
    "exit_time":    "15:00",
    "per_leg_sl":   2000.0,             # ₹ per leg before forced exit
    "lots":         1,                  # number of NIFTY lots per leg
    "otm_pct":      1.5,                # ±1.5% from open price
}
_load_module_config_into("owl", _owl_config)

_owl_state: dict = {
    "session_date":  None,              # YYYY-MM-DD of today's setup (if any)
    "anchor_price":  None,              # NIFTY spot at the moment of entry trigger
    "expiry":        None,              # ISO date string of monthly expiry used
    "ce_strike":     None,
    "pe_strike":     None,
    "ce_symbol":     None,
    "pe_symbol":     None,
    "ce_leg":        None,              # see _owl_leg_shape() below
    "pe_leg":        None,
    "skip_today":    False,
    "skip_reason":   None,
    "last_ce_ltp":   None,
    "last_pe_ltp":   None,
    "logs":          [],                # newest first
    "history":       [],                # last ~60 trading days
    "last_error":    None,
    "lifecycle":     "idle",            # idle / armed / in_position / squared_off / skipped
}
_owl_lock = threading.Lock()


def _owl_leg_shape() -> dict:
    """Template for a leg dict."""
    return {
        "side":         "SELL",
        "strike":       None,
        "symbol":       None,
        "qty":          0,
        "entry_price":  None,
        "entry_at":     None,
        "entry_oid":    None,            # PAPER-CE/PE or kite order_id
        "exit_price":   None,
        "exit_at":      None,
        "exit_oid":     None,
        "exit_reason":  None,            # "SL" / "EOD" / "manual" / "entry_failed"
        "mtm":          0.0,
        "status":       "pending",       # pending / open / closed / failed
        "paper":        True,
    }


def _owl_load() -> None:
    """Load persisted config + history (best-effort)."""
    try:
        if os.path.exists(_OWL_CONFIG_PATH):
            with open(_OWL_CONFIG_PATH) as f:
                disk = json.load(f)
            for k in _owl_config:
                if k in disk:
                    _owl_config[k] = disk[k]
    except Exception as e:
        print(f"[owl] config load: {e}")
    try:
        if os.path.exists(_OWL_STATE_PATH):
            with open(_OWL_STATE_PATH) as f:
                disk = json.load(f)
            _owl_state["history"] = (disk.get("history") or [])[-60:]
    except Exception as e:
        print(f"[owl] state load: {e}")


def _owl_save_config() -> None:
    try:
        with open(_OWL_CONFIG_PATH, "w") as f:
            json.dump(_owl_config, f, indent=2)
    except Exception as e:
        print(f"[owl] config save: {e}")


def _owl_save_state() -> None:
    """Only persists `history` (the in-memory state is rebuilt each session)."""
    try:
        with open(_OWL_STATE_PATH, "w") as f:
            json.dump({"history": _owl_state.get("history") or []}, f, indent=2)
    except Exception as e:
        print(f"[owl] state save: {e}")


_owl_load()


def _owl_log(event: str, **extra) -> None:
    ts = _now_ist().strftime("%H:%M:%S")
    entry = {"ts": ts, "event": event, **extra}
    with _owl_lock:
        _owl_state["logs"].insert(0, entry)
        _owl_state["logs"] = _owl_state["logs"][:120]
    print(f"[owl] {ts} {event} {extra}")


def _owl_ce_strike(target: float) -> int:
    """CE round UP: pick the strike at or above the 1.5% target so the call
    is at least as OTM as intended (never closer to spot than the rule says)."""
    return int(math.ceil(target / _OWL_STRIKE_STEP) * _OWL_STRIKE_STEP)


def _owl_pe_strike(target: float) -> int:
    """PE round DOWN: pick the strike at or below the 1.5% target so the put
    is at least as OTM as intended."""
    return int(math.floor(target / _OWL_STRIKE_STEP) * _OWL_STRIKE_STEP)


def _owl_fetch_spot_price() -> float | None:
    """NIFTY 50 spot at *this moment* — used as the 1.5%-OTM anchor.

    We deliberately use live LTP (not the day's official OHLC.open) because the
    entry time is user-configurable: if the user sets entry to 09:45 and NIFTY
    has moved 0.7% from open by then, the 1.5% buffer should be measured from
    where price IS at trigger, not where it opened. The transcript uses open
    price as a backtest convenience (need a fixed reference for historical
    rows) — in live execution, CMP at entry is the right anchor.
    """
    try:
        q = kite.quote(["NSE:NIFTY 50"])["NSE:NIFTY 50"]
        spot = float(q.get("last_price") or 0)
        return spot if spot > 0 else None
    except Exception as e:
        print(f"[owl] spot fetch: {e}")
        return None


def _owl_resolve_token(tradingsymbol: str) -> int | None:
    try:
        for inst in kite.instruments("NFO"):
            if inst.get("tradingsymbol") == tradingsymbol:
                return inst.get("instrument_token")
    except Exception as e:
        print(f"[owl] token lookup: {e}")
    return None


def _owl_setup_today() -> bool:
    """Compute today's strikes from NIFTY open. Sets state, doesn't place orders."""
    today = _now_ist().date()
    if _is_monthly_expiry_day(today):
        with _owl_lock:
            _owl_state.update({
                "session_date": today.isoformat(),
                "skip_today":   True,
                "skip_reason":  "monthly expiry day — Owl rule says skip",
                "lifecycle":    "skipped",
            })
        _owl_log("skip", reason="expiry_day")
        broadcast("owl_update")
        return False

    spot = _owl_fetch_spot_price()
    if not spot:
        with _owl_lock:
            _owl_state["last_error"] = "could not fetch NIFTY spot"
        _owl_log("setup_failed", reason="no_spot_price")
        return False

    pct    = float(_owl_config.get("otm_pct") or 1.5) / 100.0
    # CE rounds UP, PE rounds DOWN — always land at or beyond the 1.5%
    # buffer (never closer to spot), so the realized OTM ≥ configured OTM.
    ce_str = _owl_ce_strike(spot * (1 + pct))
    pe_str = _owl_pe_strike(spot * (1 - pct))
    expiry = _current_monthly_expiry(today)
    ce_sym = _nifty_option_symbol(ce_str, "CE", expiry)
    pe_sym = _nifty_option_symbol(pe_str, "PE", expiry)

    with _owl_lock:
        _owl_state.update({
            "session_date":  today.isoformat(),
            "anchor_price":  round(spot, 2),
            "expiry":        expiry.isoformat(),
            "ce_strike":     ce_str, "pe_strike": pe_str,
            "ce_symbol":     ce_sym, "pe_symbol": pe_sym,
            "ce_leg":        None,   "pe_leg":    None,
            "skip_today":    False,  "skip_reason": None,
            "last_ce_ltp":   None,   "last_pe_ltp": None,
            "last_error":    None,
            "lifecycle":     "armed",
        })
    _owl_log("setup",
             anchor_price=round(spot, 2), expiry=expiry.isoformat(),
             ce_strike=ce_str, pe_strike=pe_str,
             ce_symbol=ce_sym, pe_symbol=pe_sym)
    _telegram(
        f"🦉 *Owl armed* — NIFTY @ entry ₹{spot:.2f} ({expiry.strftime('%d-%b')})\n"
        f"CE: `{ce_sym}`  · PE: `{pe_sym}`\n"
        f"Mode: *{'PAPER' if _owl_config['paper_mode'] else 'LIVE'}*"
    )
    broadcast("owl_update")
    return True


def _owl_ltp(symbol: str) -> float | None:
    """Best-effort LTP for an NFO option symbol."""
    try:
        key = f"NFO:{symbol}"
        q = kite.quote([key]).get(key) or {}
        ltp = float(q.get("last_price") or 0)
        return ltp if ltp > 0 else None
    except Exception as e:
        print(f"[owl] ltp {symbol}: {e}")
        return None


def _owl_place_sell(symbol: str, qty: int, paper: bool) -> tuple[str | None, float | None, str | None]:
    """Returns (order_id, fill_price, error). For paper mode fill = LTP."""
    ltp = _owl_ltp(symbol)
    if paper:
        return "PAPER", ltp, None
    if not ltp:
        return None, None, "no LTP for fill estimate"
    try:
        oid = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange="NFO", tradingsymbol=symbol,
            transaction_type=kite.TRANSACTION_TYPE_SELL,
            quantity=qty, product=kite.PRODUCT_NRML,
            order_type=kite.ORDER_TYPE_MARKET,
            tag="owl_entry",
        )
        return oid, ltp, None
    except Exception as e:
        return None, None, str(e)


def _owl_place_buy(symbol: str, qty: int, paper: bool) -> tuple[str | None, float | None, str | None]:
    ltp = _owl_ltp(symbol)
    if paper:
        return "PAPER", ltp, None
    if not ltp:
        return None, None, "no LTP for fill estimate"
    try:
        oid = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange="NFO", tradingsymbol=symbol,
            transaction_type=kite.TRANSACTION_TYPE_BUY,
            quantity=qty, product=kite.PRODUCT_NRML,
            order_type=kite.ORDER_TYPE_MARKET,
            tag="owl_exit",
        )
        return oid, ltp, None
    except Exception as e:
        return None, None, str(e)


def _owl_enter() -> bool:
    """Sell CE + PE for today. Assumes _owl_setup_today() has run."""
    with _owl_lock:
        cfg = dict(_owl_config)
        s   = dict(_owl_state)
    if s.get("skip_today"):
        return False
    if not (s.get("ce_symbol") and s.get("pe_symbol")):
        _owl_log("enter_failed", reason="no_symbols_setup_first")
        return False
    if s.get("ce_leg") or s.get("pe_leg"):
        _owl_log("enter_skipped", reason="already_entered_today")
        return False

    paper = bool(cfg.get("paper_mode"))
    lots  = max(1, int(cfg.get("lots") or 1))
    qty   = lots * _NIFTY_LOT
    ce_sym, pe_sym = s["ce_symbol"], s["pe_symbol"]

    # Place both in parallel
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_ce = ex.submit(_owl_place_sell, ce_sym, qty, paper)
        f_pe = ex.submit(_owl_place_sell, pe_sym, qty, paper)
        ce_oid, ce_fill, ce_err = f_ce.result()
        pe_oid, pe_fill, pe_err = f_pe.result()

    now_iso = _now_ist().isoformat()
    ce_leg = _owl_leg_shape() | {
        "side": "SELL", "strike": s["ce_strike"], "symbol": ce_sym,
        "qty": qty, "paper": paper,
    }
    pe_leg = _owl_leg_shape() | {
        "side": "SELL", "strike": s["pe_strike"], "symbol": pe_sym,
        "qty": qty, "paper": paper,
    }

    if ce_err or ce_oid is None:
        ce_leg.update(status="failed", exit_reason="entry_failed",
                      entry_at=now_iso)
        _owl_log("ce_entry_failed", error=ce_err)
    else:
        ce_leg.update(status="open", entry_price=ce_fill,
                      entry_at=now_iso, entry_oid=str(ce_oid))

    if pe_err or pe_oid is None:
        pe_leg.update(status="failed", exit_reason="entry_failed",
                      entry_at=now_iso)
        _owl_log("pe_entry_failed", error=pe_err)
    else:
        pe_leg.update(status="open", entry_price=pe_fill,
                      entry_at=now_iso, entry_oid=str(pe_oid))

    with _owl_lock:
        _owl_state["ce_leg"]    = ce_leg
        _owl_state["pe_leg"]    = pe_leg
        _owl_state["lifecycle"] = "in_position" if (
            ce_leg["status"] == "open" or pe_leg["status"] == "open"
        ) else "idle"
    _owl_log("entry",
             ce_fill=ce_fill, pe_fill=pe_fill, qty=qty,
             paper=paper)
    _telegram(
        f"📍 *Owl entered* {'(PAPER)' if paper else ''}\n"
        f"SELL CE `{ce_sym}` @ ₹{ce_fill or 0:.2f}  ({ce_leg['status']})\n"
        f"SELL PE `{pe_sym}` @ ₹{pe_fill or 0:.2f}  ({pe_leg['status']})\n"
        f"Qty/leg: {qty}  ·  Per-leg SL: ₹{cfg['per_leg_sl']:,.0f}"
    )
    broadcast("owl_update")
    return True


def _owl_close_leg(which: str, reason: str) -> bool:
    """which ∈ {'ce','pe'}. Reason ∈ {'SL','EOD','manual'}.
    Buys back the leg (or paper-records) and marks it closed."""
    if which not in ("ce", "pe"):
        return False
    leg_key = f"{which}_leg"
    with _owl_lock:
        cfg = dict(_owl_config)
        leg = _owl_state.get(leg_key)
    if not leg or leg.get("status") != "open":
        return False

    paper = bool(cfg.get("paper_mode"))
    oid, fill, err = _owl_place_buy(leg["symbol"], leg["qty"], paper)
    now_iso = _now_ist().isoformat()
    if err or oid is None:
        # Mark the attempt but keep status open so a retry can happen later
        _owl_log(f"{which}_exit_failed", reason=err)
        _telegram(f"⚠️ Owl {which.upper()} exit failed: {err}")
        return False

    entry = float(leg["entry_price"] or 0)
    exit_ = float(fill or 0)
    # Short option P&L = (entry - exit) × qty
    mtm  = (entry - exit_) * int(leg["qty"])
    with _owl_lock:
        _owl_state[leg_key].update({
            "exit_price":  exit_,
            "exit_at":     now_iso,
            "exit_oid":    str(oid),
            "exit_reason": reason,
            "mtm":         round(mtm, 2),
            "status":      "closed",
        })
        any_open = any(
            (_owl_state.get(k) or {}).get("status") == "open"
            for k in ("ce_leg", "pe_leg")
        )
        if not any_open:
            _owl_state["lifecycle"] = "squared_off"
    _owl_log(f"{which}_exit", reason=reason,
             entry=entry, exit=exit_, mtm=round(mtm, 2))
    _telegram(
        f"🦉 *Owl {which.upper()} closed* ({reason}) {'(PAPER)' if paper else ''}\n"
        f"`{leg['symbol']}`  entry ₹{entry:.2f} → exit ₹{exit_:.2f}\n"
        f"Leg P&L: ₹{mtm:+,.2f}"
    )
    broadcast("owl_update")
    return True


def _owl_archive_today() -> None:
    """After both legs closed (or expiry/skip), append today's snapshot to history."""
    with _owl_lock:
        s = dict(_owl_state)
    if not s.get("session_date"):
        return
    # Avoid duplicates
    hist = _owl_state.get("history") or []
    if hist and hist[-1].get("date") == s["session_date"]:
        return
    ce = s.get("ce_leg") or {}
    pe = s.get("pe_leg") or {}
    record = {
        "date":         s["session_date"],
        "anchor_price": s.get("anchor_price"),
        "expiry":       s.get("expiry"),
        "ce_strike":    s.get("ce_strike"),
        "pe_strike":    s.get("pe_strike"),
        "ce_entry":     ce.get("entry_price"),
        "ce_exit":      ce.get("exit_price"),
        "ce_reason":    ce.get("exit_reason"),
        "ce_mtm":       ce.get("mtm"),
        "pe_entry":     pe.get("entry_price"),
        "pe_exit":      pe.get("exit_price"),
        "pe_reason":    pe.get("exit_reason"),
        "pe_mtm":       pe.get("mtm"),
        "net_mtm":      round(float(ce.get("mtm") or 0) + float(pe.get("mtm") or 0), 2),
        "skip":         bool(s.get("skip_today")),
        "skip_reason":  s.get("skip_reason"),
        "paper":        (ce.get("paper") if ce else pe.get("paper") if pe else True),
    }
    with _owl_lock:
        _owl_state["history"].append(record)
        _owl_state["history"] = _owl_state["history"][-60:]
    _owl_save_state()


def _owl_tick_loop():
    """Polls CE/PE LTPs every 2s, updates leg MTM, fires per-leg SL."""
    while True:
        try:
            with _owl_lock:
                cfg = dict(_owl_config)
                s   = dict(_owl_state)
            ce_leg, pe_leg = s.get("ce_leg"), s.get("pe_leg")
            need_ce = ce_leg and ce_leg.get("status") == "open"
            need_pe = pe_leg and pe_leg.get("status") == "open"
            if not (need_ce or need_pe):
                time.sleep(2)
                continue

            keys = []
            if need_ce: keys.append(f"NFO:{ce_leg['symbol']}")
            if need_pe: keys.append(f"NFO:{pe_leg['symbol']}")
            try:
                quotes = kite.quote(keys)
            except Exception as e:
                print(f"[owl-tick] quote: {e}")
                time.sleep(3); continue

            sl = float(cfg.get("per_leg_sl") or 2000.0)
            if need_ce:
                ce_ltp = float((quotes.get(f"NFO:{ce_leg['symbol']}") or {}).get("last_price") or 0)
                if ce_ltp > 0:
                    mtm = (ce_leg["entry_price"] - ce_ltp) * int(ce_leg["qty"])
                    with _owl_lock:
                        _owl_state["last_ce_ltp"]   = ce_ltp
                        _owl_state["ce_leg"]["mtm"] = round(mtm, 2)
                    if mtm <= -sl:
                        _owl_close_leg("ce", "SL")
            if need_pe:
                pe_ltp = float((quotes.get(f"NFO:{pe_leg['symbol']}") or {}).get("last_price") or 0)
                if pe_ltp > 0:
                    mtm = (pe_leg["entry_price"] - pe_ltp) * int(pe_leg["qty"])
                    with _owl_lock:
                        _owl_state["last_pe_ltp"]   = pe_ltp
                        _owl_state["pe_leg"]["mtm"] = round(mtm, 2)
                    if mtm <= -sl:
                        _owl_close_leg("pe", "SL")
            broadcast("owl_update")
        except Exception as e:
            print(f"[owl-tick] {e}")
        time.sleep(2)


def _owl_morning_loop():
    """Once per day: setup at entry_time-1min, then enter at entry_time.
    Honors `active=False` (kill switch) and skips weekends/holidays/expiry day."""
    last_setup_for = None
    last_entry_for = None
    while True:
        try:
            now = _now_ist()
            today = now.date()
            if not _owl_config.get("active"):
                time.sleep(15); continue
            if not _is_trading_day(today):
                time.sleep(60); continue
            if _is_monthly_expiry_day(today):
                # Mark skipped so UI reflects it
                if _owl_state.get("session_date") != today.isoformat():
                    _owl_setup_today()        # this branch sets skip_today=True
                time.sleep(60); continue

            entry_h, entry_m = [int(x) for x in (_owl_config.get("entry_time") or "09:20").split(":")]
            entry_dt = now.replace(hour=entry_h, minute=entry_m, second=0, microsecond=0)
            setup_dt = entry_dt - timedelta(minutes=1)

            # Setup runs once per trading day, at entry_time - 1min
            if last_setup_for != today and now >= setup_dt and now < entry_dt + timedelta(minutes=30):
                _owl_setup_today()
                last_setup_for = today

            # Enter runs once per trading day, at entry_time
            if last_entry_for != today and now >= entry_dt and now < entry_dt + timedelta(minutes=30):
                # Make sure setup has run (e.g. if scheduler started mid-window)
                if _owl_state.get("session_date") != today.isoformat():
                    _owl_setup_today()
                # Only enter if not skipped + symbols ready + no legs yet
                with _owl_lock:
                    s = dict(_owl_state)
                if (not s.get("skip_today")) and s.get("ce_symbol") and s.get("pe_symbol") \
                   and not s.get("ce_leg") and not s.get("pe_leg"):
                    _owl_enter()
                last_entry_for = today
        except Exception as e:
            print(f"[owl-morning] {e}")
        time.sleep(15)


def _owl_squareoff_loop():
    """At exit_time, force-close any leg still open."""
    last_run_for = None
    while True:
        try:
            now = _now_ist()
            today = now.date()
            exit_h, exit_m = [int(x) for x in (_owl_config.get("exit_time") or "15:00").split(":")]
            exit_dt = now.replace(hour=exit_h, minute=exit_m, second=0, microsecond=0)
            if last_run_for != today and now >= exit_dt and now < exit_dt + timedelta(minutes=30):
                with _owl_lock:
                    ce = (_owl_state.get("ce_leg") or {})
                    pe = (_owl_state.get("pe_leg") or {})
                if ce.get("status") == "open":
                    _owl_close_leg("ce", "EOD")
                if pe.get("status") == "open":
                    _owl_close_leg("pe", "EOD")
                _owl_archive_today()
                last_run_for = today
        except Exception as e:
            print(f"[owl-squareoff] {e}")
        time.sleep(20)


# Boot threads
threading.Thread(target=_owl_morning_loop,   daemon=True).start()
threading.Thread(target=_owl_tick_loop,      daemon=True).start()
threading.Thread(target=_owl_squareoff_loop, daemon=True).start()


# ── Owl routes ────────────────────────────────────────────────────────────────
@app.route("/owl", methods=["GET"])
def owl_route():
    with _owl_lock:
        s = dict(_owl_state)
        cfg = dict(_owl_config)
    return jsonify({
        "ok":     True,
        "config": cfg,
        "state":  s,
        "today":  _now_ist().date().isoformat(),
        "is_expiry_day": _is_monthly_expiry_day(_now_ist().date()),
    })


@app.route("/owl/config", methods=["POST"])
def owl_config_route():
    d = request.json or {}
    for k in ("entry_time", "exit_time"):
        if k in d:
            v = str(d[k]).strip()
            # Tiny shape check: HH:MM
            try:
                h, m = [int(x) for x in v.split(":")]
                if not (0 <= h < 24 and 0 <= m < 60):
                    raise ValueError("out of range")
                _owl_config[k] = f"{h:02d}:{m:02d}"
            except Exception:
                return jsonify({"ok": False, "error": f"{k} must be HH:MM"}), 400
    for k in ("per_leg_sl", "otm_pct"):
        if k in d:
            try: _owl_config[k] = float(d[k])
            except Exception:
                return jsonify({"ok": False, "error": f"{k} must be a number"}), 400
    if "lots" in d:
        try:
            v = int(d["lots"])
            if v < 1:
                return jsonify({"ok": False, "error": "lots must be ≥ 1"}), 400
            _owl_config["lots"] = v
        except Exception:
            return jsonify({"ok": False, "error": "lots must be an integer"}), 400
    if "paper_mode" in d:
        _owl_config["paper_mode"] = bool(d["paper_mode"])
    if "active" in d:
        _owl_config["active"] = bool(d["active"])
    _save_module_config("owl", _owl_config)
    broadcast("owl_update")
    return jsonify({"ok": True, "config": dict(_owl_config)})


@app.route("/owl/enter", methods=["POST"])
def owl_enter_route():
    """Manual same-day entry — runs setup if needed, then enters.
    Skips silently if today is expiry day or legs already exist."""
    threading.Thread(target=_owl_manual_enter, daemon=True).start()
    return jsonify({"ok": True})


def _owl_manual_enter():
    today = _now_ist().date()
    if _owl_state.get("session_date") != today.isoformat():
        if not _owl_setup_today():
            return
    _owl_enter()


@app.route("/owl/exit/<leg>", methods=["POST"])
def owl_exit_route(leg: str):
    leg = (leg or "").lower()
    if leg not in ("ce", "pe", "both"):
        return jsonify({"ok": False, "error": "leg must be ce/pe/both"}), 400
    threading.Thread(target=_owl_manual_exit, args=(leg,), daemon=True).start()
    return jsonify({"ok": True})


def _owl_manual_exit(leg: str):
    if leg in ("ce", "both"): _owl_close_leg("ce", "manual")
    if leg in ("pe", "both"): _owl_close_leg("pe", "manual")
    # If both are now closed, archive
    with _owl_lock:
        ce = (_owl_state.get("ce_leg") or {})
        pe = (_owl_state.get("pe_leg") or {})
    if (ce.get("status") in (None, "closed", "failed")) and (pe.get("status") in (None, "closed", "failed")):
        _owl_archive_today()


@app.route("/owl/history", methods=["GET"])
def owl_history_route():
    """Last N trading-day records. Read-only."""
    n = int(request.args.get("n") or 30)
    with _owl_lock:
        hist = list(_owl_state.get("history") or [])
    return jsonify({"ok": True, "history": hist[-n:]})


# ── Calendar Spread (Phase A: monitor + log only, no orders) ──────────────────
#
# Tracks (near-month future, far-month future) on selected underlyings. For
# each tick:
#   spread        = F_far - F_near
#   fair_linear   = F_near * carry_rate * days_between_expiries / 365
#   fair_empirical= rolling 5-trading-day mean of daily-close spread
#   fair_combined = max(fair_linear, fair_empirical)
#   deviation     = spread - fair_combined
#   z_score       = deviation / rolling_5d_std
#   would_fire_*  = combined trigger (|deviation|>entry_pts AND |z|>entry_z)
#
# Logs every tick to data/csv/calspread_<underlying>_YYYYMMDD.csv so we can
# review "what would've fired" before turning on Phase B (orders).

_CALSPREAD_SETUP_AT       = dtime(9, 16)
_CALSPREAD_MARKET_OPEN    = dtime(9, 15)
_CALSPREAD_MARKET_CLOSE   = dtime(15, 30)
_CALSPREAD_TICK_SLOT_MOD  = 10                   # poll every 10s
_CALSPREAD_TICK_SLOT_REM  = 5                    # at second 5,15,25,35,45,55 — collides with nothing
_CALSPREAD_ROLL_WINDOW    = 5                    # trading-day rolling window for empirical fair value

# Underlyings supported in Phase A. Easy to extend later by adding rows here.
_CALSPREAD_UNDERLYINGS = {
    "NIFTY": {
        "label":       "NIFTY",
        "fut_prefix":  "NIFTY",
        "lot_size":    65,
        "exchange":    "NFO",
    },
    "BANKNIFTY": {
        "label":       "BANKNIFTY",
        "fut_prefix":  "BANKNIFTY",
        "lot_size":    30,
        "exchange":    "NFO",
    },
}

# Shared config (applies to all underlyings unless per-underlying override).
_calspread_config = {
    "carry_rate":              0.065,    # 6.5% repo (configurable from UI later)
    "entry_pts":               8.0,
    "entry_z":                 2.5,
    "exit_band_pts":           2.0,      # exit when |deviation| <= this
    "sl_z":                    1.0,      # SL when |z| moves by this much AGAINST entry direction
    "roll_lockout_days":       5,
    "min_far_book_pts":        1.5,
    "max_trades_per_day":      1,
    "max_notional_per_under":  300000,
    "lots":                    1,
    "active":                  {u: False for u in _CALSPREAD_UNDERLYINGS},   # per-underlying master switch
    "paper_mode":              {u: False for u in _CALSPREAD_UNDERLYINGS},   # Phase A doesn't place orders anyway
}
_load_module_config_into("calspread", _calspread_config)

# Per-underlying live state (built on each trading-day setup).
_calspread_state: dict = {u: {
    "session_date":     None,
    "near_expiry":      None,
    "far_expiry":       None,
    "near_symbol":      None, "near_token": None,
    "far_symbol":       None, "far_token":  None,
    "days_between":     None,
    "last_near_ltp":    None,
    "last_far_ltp":     None,
    "last_spread":      None,
    "fair_linear":      None,
    "fair_empirical":   None,
    "fair_combined":    None,
    "deviation":        None,
    "z_score":          None,
    "rolling_mean":     None,
    "rolling_std":      None,
    "rolling_history":  [],          # last N days' (date, daily_close_spread) for the empirical model
    "would_fire_long":  False,
    "would_fire_short": False,
    "would_fire_count": 0,            # cumulative ticks that fired today (for visibility)
    "last_error":       None,
    "lifecycle":        "idle",       # idle | armed | (Phase B will add more)
} for u in _CALSPREAD_UNDERLYINGS}
_calspread_lock = threading.Lock()


def _calspread_resolve_expiries(underlying: str, today: date | None = None):
    """Return (near_expiry, far_expiry) — both adjusted for holidays.

    NIFTY/BANKNIFTY monthlies expire on the last Tuesday of each month (per
    existing _last_tuesday_of_month helper). Near = current month if today is
    on or before its expiry, else next month. Far = the month after near.
    """
    today = today or _now_ist().date()
    this_exp = _shift_to_trading_day(_last_tuesday_of_month(today.year, today.month))
    if today <= this_exp:
        near = this_exp
        nm_year, nm_month = (today.year, today.month + 1) if today.month < 12 else (today.year + 1, 1)
        far = _shift_to_trading_day(_last_tuesday_of_month(nm_year, nm_month))
    else:
        # Past this month's expiry — roll forward.
        nm_year, nm_month = (today.year, today.month + 1) if today.month < 12 else (today.year + 1, 1)
        near = _shift_to_trading_day(_last_tuesday_of_month(nm_year, nm_month))
        fm_year, fm_month = (nm_year, nm_month + 1) if nm_month < 12 else (nm_year + 1, 1)
        far  = _shift_to_trading_day(_last_tuesday_of_month(fm_year, fm_month))
    return near, far


def _calspread_instrument_token(symbol: str, exchange: str = "NFO") -> int | None:
    """Look up a tradingsymbol's instrument_token via kite.instruments()."""
    try:
        instruments = kite.instruments(exchange)
    except Exception as e:
        print(f"[calspread-token] kite.instruments error: {e}")
        return None
    for inst in instruments:
        if inst.get("tradingsymbol") == symbol:
            return inst.get("instrument_token")
    return None


def _calspread_fetch_history(near_token: int, far_token: int, days: int) -> list[dict]:
    """Fetch past `days` of daily-close data for both legs; pair them by date
    and return [{date, spread}]. Used to seed the empirical fair value."""
    now = _now_ist()
    # 2x days of calendar lookback to survive weekends + holidays
    from_dt = now - timedelta(days=days * 2 + 10)
    to_dt   = now - timedelta(seconds=1)
    try:
        near_bars = kite.historical_data(near_token, from_dt, to_dt, interval="day") or []
        far_bars  = kite.historical_data(far_token,  from_dt, to_dt, interval="day") or []
    except Exception as e:
        print(f"[calspread-history] {e}")
        return []
    near_map = {b["date"].strftime("%Y-%m-%d"): b for b in near_bars}
    far_map  = {b["date"].strftime("%Y-%m-%d"): b for b in far_bars}
    common = sorted(set(near_map) & set(far_map))
    out = []
    for d in common[-days:]:
        out.append({
            "date":         d,
            "near_close":   float(near_map[d]["close"]),
            "far_close":    float(far_map[d]["close"]),
            "spread":       float(far_map[d]["close"]) - float(near_map[d]["close"]),
        })
    return out


def _calspread_setup_underlying(underlying: str) -> bool:
    """At 09:16 each trading day: resolve near/far expiries, look up tokens,
    fetch historical for empirical fair value. Idempotent — safe to call
    multiple times in a day (re-resolves)."""
    cfg = _CALSPREAD_UNDERLYINGS.get(underlying)
    if not cfg:
        return False
    today = _now_ist().date()
    near_exp, far_exp = _calspread_resolve_expiries(underlying, today)
    near_sym = _future_symbol(cfg["fut_prefix"], near_exp)
    far_sym  = _future_symbol(cfg["fut_prefix"], far_exp)
    near_tok = _calspread_instrument_token(near_sym, cfg["exchange"])
    far_tok  = _calspread_instrument_token(far_sym, cfg["exchange"])
    if not near_tok or not far_tok:
        with _calspread_lock:
            _calspread_state[underlying]["last_error"] = f"token lookup failed: {near_sym}/{far_sym}"
        return False

    days_between = (far_exp - near_exp).days

    # Historical for empirical mean + std
    hist = _calspread_fetch_history(near_tok, far_tok, _CALSPREAD_ROLL_WINDOW)
    if hist:
        spreads = [h["spread"] for h in hist]
        mean    = sum(spreads) / len(spreads)
        if len(spreads) >= 2:
            var = sum((s - mean) ** 2 for s in spreads) / (len(spreads) - 1)
            std = var ** 0.5
        else:
            std = 0.0
    else:
        mean = None
        std  = None

    with _calspread_lock:
        s = _calspread_state[underlying]
        s.update({
            "session_date":     today.isoformat(),
            "near_expiry":      near_exp.isoformat(),
            "far_expiry":       far_exp.isoformat(),
            "near_symbol":      near_sym, "near_token": near_tok,
            "far_symbol":       far_sym,  "far_token":  far_tok,
            "days_between":     days_between,
            "rolling_history":  hist,
            "rolling_mean":     mean,
            "rolling_std":      std,
            "last_error":       None,
            "lifecycle":        "armed",
        })
    print(f"[calspread] {underlying} setup: near={near_sym} ({near_exp}) "
          f"far={far_sym} ({far_exp}) days_between={days_between} "
          f"hist_n={len(hist)} mean={mean and round(mean, 2)} std={std and round(std, 2)}")
    return True


def _calspread_compute_metrics(underlying: str, near_ltp: float, far_ltp: float):
    """Update spread / fair / deviation / z / would-fire flags for one tick."""
    cfg_global = _calspread_config
    with _calspread_lock:
        s = _calspread_state[underlying]
        rolling_mean = s.get("rolling_mean")
        rolling_std  = s.get("rolling_std")
        days_between = s.get("days_between") or 30

    spread = round(far_ltp - near_ltp, 2)

    # Linear fair value (cost-of-carry, simple)
    carry_rate = float(cfg_global.get("carry_rate") or 0.065)
    fair_linear = round(near_ltp * carry_rate * days_between / 365.0, 2)

    # Empirical fair value: rolling mean (or fall back to linear if no history)
    fair_empirical = rolling_mean if rolling_mean is not None else fair_linear

    # Combined: max of the two — most conservative reference point
    fair_combined = max(fair_linear, fair_empirical)
    deviation = round(spread - fair_combined, 2)

    # z-score from empirical history
    if rolling_std and rolling_std > 0:
        z = round((spread - (rolling_mean or 0)) / rolling_std, 2)
    else:
        z = None

    # Would-fire (dual trigger): |deviation| > entry_pts AND |z| > entry_z
    entry_pts = float(cfg_global.get("entry_pts") or 8.0)
    entry_z   = float(cfg_global.get("entry_z")   or 2.5)
    abs_dev = abs(deviation)
    abs_z   = abs(z) if z is not None else 0.0
    long_side  = deviation < 0 and abs_dev > entry_pts and abs_z > entry_z   # buy spread (sell near, buy far)
    short_side = deviation > 0 and abs_dev > entry_pts and abs_z > entry_z   # sell spread (buy near, sell far)

    with _calspread_lock:
        s = _calspread_state[underlying]
        s.update({
            "last_near_ltp":    near_ltp,
            "last_far_ltp":     far_ltp,
            "last_spread":      spread,
            "fair_linear":      fair_linear,
            "fair_empirical":   round(fair_empirical, 2) if fair_empirical is not None else None,
            "fair_combined":    round(fair_combined, 2),
            "deviation":        deviation,
            "z_score":          z,
            "would_fire_long":  long_side,
            "would_fire_short": short_side,
        })
        if long_side or short_side:
            s["would_fire_count"] = (s.get("would_fire_count") or 0) + 1


def _calspread_csv_log(underlying: str):
    """Append a row to data/csv/calspread_<underlying>_YYYYMMDD.csv."""
    try:
        d = _now_ist().strftime("%Y%m%d")
        path = os.path.join(_CSV_DIR, f"calspread_{underlying}_{d}.csv")
        write_header = not os.path.exists(path)
        with _calspread_lock:
            s = dict(_calspread_state[underlying])
        with open(path, "a", newline="") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow([
                    "ts", "underlying", "near_sym", "far_sym",
                    "near_ltp", "far_ltp", "spread",
                    "fair_linear", "fair_empirical", "fair_combined",
                    "deviation", "z_score",
                    "would_fire_long", "would_fire_short",
                ])
            w.writerow([
                _now_ist().isoformat(),
                underlying,
                s.get("near_symbol"), s.get("far_symbol"),
                s.get("last_near_ltp"), s.get("last_far_ltp"), s.get("last_spread"),
                s.get("fair_linear"), s.get("fair_empirical"), s.get("fair_combined"),
                s.get("deviation"), s.get("z_score"),
                s.get("would_fire_long"), s.get("would_fire_short"),
            ])
    except Exception as e:
        print(f"[calspread-csv] {e}")


def _calspread_morning_setup_loop():
    """Once each trading day at >= 09:16 IST, run setup for every underlying
    that doesn't already have today's session_date. Idempotent."""
    while True:
        try:
            now = _now_ist()
            today = now.date().isoformat()
            if _is_trading_day(now.date()) and now.time() >= _CALSPREAD_SETUP_AT and now.time() < _CALSPREAD_MARKET_CLOSE:
                for underlying in _CALSPREAD_UNDERLYINGS:
                    with _calspread_lock:
                        done = _calspread_state[underlying].get("session_date") == today
                    if not done:
                        _calspread_setup_underlying(underlying)
        except Exception as e:
            print(f"[calspread-morning] {e}")
        time.sleep(30)


def _calspread_tick_loop():
    """Polls near + far futures every 10s during market hours. Computes
    metrics, logs to CSV. Phase A: no orders, no telegram alerts."""
    while True:
        try:
            now = _now_ist()
            if (_is_trading_day(now.date())
                and _CALSPREAD_MARKET_OPEN <= now.time() < _CALSPREAD_MARKET_CLOSE
                and now.second % _CALSPREAD_TICK_SLOT_MOD == _CALSPREAD_TICK_SLOT_REM):
                _calspread_poll_all_underlyings()
        except Exception as e:
            print(f"[calspread-tick] {e}")
        time.sleep(1)


def _calspread_poll_all_underlyings():
    """One pass: batch kite.quote() for every underlying that's set up today."""
    today_iso = _now_ist().date().isoformat()
    with _calspread_lock:
        active_states = {
            u: _calspread_state[u]
            for u in _CALSPREAD_UNDERLYINGS
            if _calspread_state[u].get("session_date") == today_iso
            and _calspread_state[u].get("near_token")
            and _calspread_state[u].get("far_token")
        }
    if not active_states:
        return
    tokens = []
    for u, s in active_states.items():
        tokens.append(s["near_token"])
        tokens.append(s["far_token"])
    try:
        q = kite.quote(tokens)
    except Exception as e:
        with _calspread_lock:
            for u in active_states:
                _calspread_state[u]["last_error"] = f"quote error: {e}"
        return
    for u, s in active_states.items():
        near_q = q.get(str(s["near_token"])) or {}
        far_q  = q.get(str(s["far_token"]))  or {}
        near_ltp = float(near_q.get("last_price") or 0)
        far_ltp  = float(far_q.get("last_price")  or 0)
        if near_ltp <= 0 or far_ltp <= 0:
            continue
        _calspread_compute_metrics(u, near_ltp, far_ltp)
        _calspread_csv_log(u)
    broadcast("calspread_update")


# Boot threads
threading.Thread(target=_calspread_morning_setup_loop, daemon=True).start()
threading.Thread(target=_calspread_tick_loop,          daemon=True).start()


# ── Calendar spread routes (Phase A: read-only) ──────────────────────────────
@app.route("/calspread", methods=["GET"])
def calspread_state_route():
    """Returns config + per-underlying state. Frontend polls this."""
    with _calspread_lock:
        states = {u: dict(_calspread_state[u]) for u in _CALSPREAD_UNDERLYINGS}
    return jsonify({
        "ok":            True,
        "config":        dict(_calspread_config),
        "underlyings":   {u: dict(meta) for u, meta in _CALSPREAD_UNDERLYINGS.items()},
        "states":        states,
        "roll_window":   _CALSPREAD_ROLL_WINDOW,
    })


@app.route("/calspread/config", methods=["POST"])
def calspread_config_route():
    """Update shared config. Phase A only tunes thresholds; entry/exit
    decisions are not acted on until Phase B."""
    d = request.json or {}
    for k in ("carry_rate", "entry_pts", "entry_z", "exit_band_pts",
              "sl_z", "min_far_book_pts", "max_notional_per_under"):
        if k in d:
            try: _calspread_config[k] = float(d[k])
            except Exception: pass
    for k in ("roll_lockout_days", "max_trades_per_day", "lots"):
        if k in d:
            try: _calspread_config[k] = int(d[k])
            except Exception: pass
    if "active" in d and isinstance(d["active"], dict):
        for u, v in d["active"].items():
            if u in _CALSPREAD_UNDERLYINGS:
                _calspread_config["active"][u] = bool(v)
    _save_module_config("calspread", _calspread_config)
    return jsonify({"ok": True, "config": dict(_calspread_config)})


@app.route("/calspread/setup/<underlying>", methods=["POST"])
def calspread_force_setup_route(underlying: str):
    """Force-run setup for one underlying (useful for testing without waiting
    for the 09:16 morning loop)."""
    if underlying not in _CALSPREAD_UNDERLYINGS:
        return jsonify({"ok": False, "error": "unknown underlying"}), 404
    ok = _calspread_setup_underlying(underlying)
    if not ok:
        with _calspread_lock:
            err = _calspread_state[underlying].get("last_error")
        return jsonify({"ok": False, "error": err or "setup failed"}), 500
    return jsonify({"ok": True})


# ── Data export ───────────────────────────────────────────────────────────────
_SESSIONS_CSV_HEADER = [
    "strategy_id", "strategy_name", "strategy_type",
    "start", "end", "duration_min",
    "trigger", "final_mtm", "peak_mtm",
    "positions_count", "positions_summary",
    "post_exit_peak", "post_exit_trough", "post_exit_eod", "post_exit_diff_inr",
]

def _flatten_sessions_to_csv(buf):
    w = csv.writer(buf)
    w.writerow(_SESSIONS_CSV_HEADER)
    with _lock:
        for sid, s in strategies.items():
            for sess in s.get("sessions", []):
                start = sess.get("start") or ""
                end   = sess.get("end")   or ""
                duration = ""
                try:
                    if start and end:
                        sdt = datetime.fromisoformat(start)
                        edt = datetime.fromisoformat(end)
                        duration = round((edt - sdt).total_seconds() / 60, 1)
                except Exception:
                    pass
                ep = sess.get("exit_positions") or []
                ep_sum = "; ".join(f"{p.get('sym','?')}({p.get('qty', 0):+})" for p in ep)
                pe = sess.get("post_exit") or {}
                fm = sess.get("final_mtm")
                eod = pe.get("eod")
                diff = (eod - fm) if (eod is not None and fm is not None) else ""
                w.writerow([
                    sid, s.get("name", ""), s.get("type", "custom"),
                    start, end, duration,
                    sess.get("trigger", "") or "",
                    fm if fm is not None else "",
                    sess.get("peak_mtm") if sess.get("peak_mtm") is not None else "",
                    len(ep), ep_sum,
                    pe.get("peak")   if pe.get("peak")   is not None else "",
                    pe.get("trough") if pe.get("trough") is not None else "",
                    eod              if eod              is not None else "",
                    diff,
                ])

@app.route("/export/sessions.csv", methods=["GET"])
def export_sessions_csv():
    buf = io.StringIO()
    _flatten_sessions_to_csv(buf)
    out = io.BytesIO(buf.getvalue().encode("utf-8"))
    out.seek(0)
    name = f"sessions_{_now_ist().strftime('%Y%m%d_%H%M%S')}.csv"
    return send_file(out, mimetype="text/csv", as_attachment=True, download_name=name)

@app.route("/export/strategies.json", methods=["GET"])
def export_strategies_json():
    if not os.path.exists(_CONFIG_FILE):
        return jsonify({"ok": False, "msg": "strategies.json not found"}), 404
    return send_file(_CONFIG_FILE, mimetype="application/json",
                     as_attachment=True, download_name="strategies.json")

@app.route("/export/csv-files", methods=["GET"])
def export_csv_files_list():
    if not os.path.isdir(_CSV_DIR):
        return jsonify({"ok": True, "files": []})
    files = []
    for f in sorted(os.listdir(_CSV_DIR)):
        if not f.endswith(".csv"):
            continue
        p = os.path.join(_CSV_DIR, f)
        if not os.path.isfile(p):
            continue
        st = os.stat(p)
        files.append({
            "name":  f,
            "size":  st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        })
    return jsonify({"ok": True, "files": files})

@app.route("/export/csv/<path:fname>", methods=["GET"])
def export_csv_download(fname: str):
    # Strict allowlist: only basename, must end .csv, must exist under _CSV_DIR
    safe = os.path.basename(fname)
    if safe != fname or not safe.endswith(".csv"):
        return jsonify({"ok": False, "msg": "Invalid filename"}), 400
    p = os.path.join(_CSV_DIR, safe)
    if not os.path.isfile(p):
        return jsonify({"ok": False, "msg": "Not found"}), 404
    return send_file(p, mimetype="text/csv", as_attachment=True, download_name=safe)

@app.route("/export/bundle.zip", methods=["GET"])
def export_bundle_zip():
    """One-click full export: strategies.json + sessions.csv + every CSV in data/csv/."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        if os.path.exists(_CONFIG_FILE):
            zf.write(_CONFIG_FILE, arcname="strategies.json")
        # sessions.csv inline
        sbuf = io.StringIO()
        _flatten_sessions_to_csv(sbuf)
        zf.writestr("sessions.csv", sbuf.getvalue())
        # All per-day CSVs (MTM + arb)
        if os.path.isdir(_CSV_DIR):
            for f in sorted(os.listdir(_CSV_DIR)):
                if not f.endswith(".csv"):
                    continue
                p = os.path.join(_CSV_DIR, f)
                if os.path.isfile(p):
                    zf.write(p, arcname=f"csv/{f}")
    out.seek(0)
    name = f"kite_export_{_now_ist().strftime('%Y%m%d_%H%M%S')}.zip"
    return send_file(out, mimetype="application/zip", as_attachment=True, download_name=name)


if __name__ == "__main__":
    # Each user instance picks its own port via the PORT env var (default 5001
    # for the primary/legacy instance).
    _port = int(os.environ.get("PORT", "5001"))
    app.run(debug=False, host="127.0.0.1", port=_port, threaded=True)
