from __future__ import annotations

import csv
import json
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

from flask import Flask, Response, jsonify, render_template, request, send_file
from kiteconnect import KiteConnect

# Load .env from DATA_DIR if set (so each user instance gets its own creds),
# else fall back to a .env next to app.py (legacy single-user layout).
_data_dir_envfile = os.path.join(os.environ.get("DATA_DIR", os.path.dirname(__file__)), ".env")
load_dotenv(_data_dir_envfile)

app = Flask(__name__)

# ── Admin / onboarding (only enabled when ADMIN_ENABLED=true in this instance's .env) ──
# When ON, this instance can invite new users + provision additional Flask
# processes via the bin/add_user_ui.sh script (run via sudo). Only one
# instance (typically the primary / "omkar") should have this enabled.
ADMIN_ENABLED = os.getenv("ADMIN_ENABLED", "").lower() in ("1", "true", "yes", "on")
ADMIN_MAX_USERS = int(os.getenv("ADMIN_MAX_USERS", "10"))
import secrets as _secrets
import subprocess as _subprocess

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
    }

_CONFIG_FILE = os.path.join(DATA_DIR, "strategies.json")

def _save_config():
    """Persist all strategy configs to strategies.json."""
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
                "profit_target":       s["profit_target"],
                "loss_limit":          s["loss_limit"],
                "trail_enabled":       s["trail_enabled"],
                "trail_activate_at":   s["trail_activate_at"],
                "trail_by":            s["trail_by"],
                "auto_entry_enabled":  s.get("auto_entry_enabled", False),
                "auto_entry_time":     s.get("auto_entry_time", "10:00"),
                "auto_entry_qty":      s.get("auto_entry_qty", 65),
                "auto_entry_product":  s.get("auto_entry_product", "MIS"),
            }
    try:
        with open(_CONFIG_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[config] Save failed: {e}")

def _load_config() -> dict:
    """Load strategy configs from strategies.json. Returns {} if not found."""
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
                  "trail_enabled", "trail_activate_at", "trail_by",
                  "auto_entry_enabled", "auto_entry_time", "auto_entry_qty",
                  "auto_entry_product")

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
            quotes = kite.ltp(keys)

            combined = 0.0
            for t in tracked:
                t["ltp"] = quotes[f"{t['exch']}:{t['sym']}"]["last_price"]
                t["mtm"] = _calc_mtm(t["avg"], t["ltp"], t["qty"], t.get("mult", 1))
                combined += t["mtm"]

            with _lock:
                s             = strategies[sid]
                profit_target = s["profit_target"]
                loss_limit    = s["loss_limit"]
                trail_enabled = s["trail_enabled"]
                trail_by      = s["trail_by"]
                activate_at   = s["trail_activate_at"]
                peak_mtm      = s["peak_mtm"]

            trail_sl = None
            if trail_enabled:
                if peak_mtm is None and combined >= activate_at:
                    peak_mtm = combined
                    _log(sid, f"Trail SL activated — peak ₹{combined:,.2f}")
                    _telegram(
                        f"📈 *Trail SL ACTIVATED* [{strat_name}]\n"
                        f"Activated at MTM ₹{combined:+,.2f}\n"
                        f"Trail SL: ₹{combined - trail_by:+,.2f}  (peak − ₹{trail_by:,.0f})\n"
                        f"Downside now capped."
                    )
                if peak_mtm is not None:
                    peak_mtm = max(peak_mtm, combined)
                    trail_sl = peak_mtm - trail_by

            with _lock:
                s            = strategies[sid]
                new_peak_day = max(s["peak_mtm_day"] or combined, combined)
                s.update({
                    "mtm":          combined,
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

            trigger = None
            if combined >= profit_target:
                trigger = f"PROFIT TARGET HIT (+₹{combined:,.2f})"
            elif combined <= -loss_limit:
                trigger = f"LOSS LIMIT HIT (-₹{abs(combined):,.2f})"
            elif trail_sl is not None and combined <= trail_sl:
                trigger = f"TRAILING SL HIT at ₹{trail_sl:,.2f}"

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

def _nifty_option_symbol(strike: int, opt_type: str, expiry: date) -> str:
    """NIFTY26MAY23500CE"""
    return f"NIFTY{_format_kite_expiry(expiry)}{strike}{opt_type}"

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
    expiry = _current_monthly_expiry(today_ist)
    ce_sym = _nifty_option_symbol(ce_strike, "CE", expiry)
    pe_sym = _nifty_option_symbol(pe_strike, "PE", expiry)

    _log(sid, f"Prev day H={high:.2f} L={low:.2f} → CE {ce_strike} | PE {pe_strike}")
    _log(sid, f"Expiry {expiry.isoformat()} | qty={qty} | product={product}")
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

def _nifty_velocity_monitor():
    global _velocity_cooldown_until
    while True:
        try:
            now = _now_ist()
            if 9 <= now.hour <= 15 and _is_trading_day(now.date()):
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


# ── Telegram bot (long-polling, bidirectional) ────────────────────────────────
_TG_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
_TG_CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID", ""))
_TG_API     = f"https://api.telegram.org/bot{_TG_TOKEN}" if _TG_TOKEN else None
_tg_last_update_id = 0
_tg_pending: dict = {}        # chat_id -> {action, args, expires_at}

def _tg_send_reply(text: str):
    """Send a Telegram message (Markdown). Returns bool."""
    if not _TG_API or not _TG_CHAT_ID:
        return False
    try:
        data = urllib.parse.urlencode({
            "chat_id": _TG_CHAT_ID, "text": text, "parse_mode": "Markdown"
        }).encode()
        urllib.request.urlopen(f"{_TG_API}/sendMessage", data=data, timeout=10)
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
    """Long-poll Telegram getUpdates. Only chat_id == TELEGRAM_CHAT_ID is allowed."""
    global _tg_last_update_id
    if not _TG_API or not _TG_CHAT_ID:
        print("[tg-bot] disabled — TELEGRAM_TOKEN/CHAT_ID missing")
        return
    print(f"[tg-bot] polling started for chat_id={_TG_CHAT_ID}")
    while True:
        try:
            params = urllib.parse.urlencode({"offset": _tg_last_update_id + 1, "timeout": 30})
            req = urllib.request.Request(f"{_TG_API}/getUpdates?{params}")
            with urllib.request.urlopen(req, timeout=40) as resp:
                payload = json.loads(resp.read().decode())
            for upd in payload.get("result", []):
                _tg_last_update_id = max(_tg_last_update_id, upd.get("update_id", 0))
                m = upd.get("message") or upd.get("edited_message") or {}
                chat_id = str(m.get("chat", {}).get("id") or "")
                text = (m.get("text") or "").strip()
                if not chat_id or not text:
                    continue
                if chat_id != _TG_CHAT_ID:
                    print(f"[tg-bot] unauthorized chat_id {chat_id}")
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
    if len(strategies) <= 1:
        return jsonify({"ok": False, "msg": "Cannot delete the last strategy"})
    if strategies[sid]["running"]:
        return jsonify({"ok": False, "msg": "Stop monitoring before deleting"})
    with _lock:
        del strategies[sid]
        strategy_order.remove(sid)
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
    with _lock:
        strategies[sid]["selected"] = [
            {"tradingsymbol": i["tradingsymbol"].upper(), "exchange": i["exchange"].upper()}
            for i in instrs
        ]
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
    _save_config()
    broadcast()
    return jsonify({"ok": True})

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
    """Dry-run: compute strikes/symbols without placing any orders."""
    if sid not in strategies:
        return jsonify({"ok": False, "msg": "Strategy not found"})
    try:
        today  = _now_ist().date()
        expiry = _current_monthly_expiry(today)
        is_exp = _is_monthly_expiry_day(today)
        high, low = _get_prev_day_nifty_range()
        ce_strike, pe_strike = _compute_strangle_strikes(high, low)
        s = strategies[sid]
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
              ("profit_target","loss_limit","trail_enabled","trail_activate_at","trail_by")}
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


# ── Auctions (sell-into-auction + auto-buyback) ───────────────────────────────
#
# Flow: morning loop fetches kite.get_auction_instruments() — the intersection
# of "in today's auction session" and "in user's demat". User Schedules a snipe
# per scrip from the dashboard (no order placed yet, just an intent + qty).
# At T-`snipe_lead_seconds` before window close (default 14:59:55) the snipe
# loop reads the live best bid and PLACES a fresh sell order at that price.
# We do not place a resting order earlier; NSE disallows auction order
# modifications during the session, so a fresh place at snipe time is the
# only reliable path. Window 14:30–15:00 IST. Buyback fires 1 min after close.

# NSE equity auction session per Zerodha support: "after 2:30 PM, ~30 minutes".
# Window 14:30–15:00 IST. Buyback fires 1 min after close.
_AUCTIONS_WINDOW_OPEN  = dtime(14, 30)
_AUCTIONS_WINDOW_CLOSE = dtime(15, 0)
_AUCTIONS_BUYBACK_AT   = dtime(15, 1)
# Fetch start time is overridable from the UI via _auctions_config["fetch_from"]

_auctions_config = {
    "assumed_premium_pct":      3.0,    # for pre-session profit estimate
    "max_notional_per_day":     200000, # ₹2L hard cap across all auction sells
    "max_notional_per_scrip":   50000,  # ₹50K per scrip
    "snipe_lead_seconds":       5,      # snipe-modify at T-N seconds
    "auto_buyback":             False,  # off by default; user must confirm Phase D wiring
    "fetch_from":               "13:30",# IST; morning loop waits until this to start polling
}

_auctions_state: dict = {
    "session_date":   None,      # YYYY-MM-DD when list was last fetched
    "fetched_at":     None,      # ISO ts
    "list":           [],        # enriched eligible auction rows
    "book":           {},        # auction_number -> {"best_bid","best_bid_qty","best_ask","best_ask_qty","ltp","ts","depth"}
    "scheduled":      {},        # auction_number -> {"qty","scheduled_at"} — intent, no order placed yet
    "snipes":         {},        # auction_number -> {"order_id","target_price","placed_at","attempted","error"}
    "buybacks":       {},        # auction_number -> {"order_id","qty","price","placed_at"}
    "fills":          [],        # finalized round-trip records for today
    "today_notional": 0.0,       # ₹ reserved by scheduled snipes today
    "last_error":     None,
}
_auctions_lock = threading.Lock()

def _equity_roundtrip_cost(notional: float, exchange: str = "NSE") -> dict:
    """Zerodha CNC delivery roundtrip cost on equity.

    Sources: zerodha.com/charges (delivery row). Brokerage is ₹0 for delivery.
    Returns each component plus total ₹ and as a % of notional.
    """
    if notional <= 0:
        return {"total": 0.0, "pct_of_notional": 0.0,
                "stt": 0.0, "exch": 0.0, "sebi": 0.0,
                "stamp": 0.0, "ipft": 0.0, "gst": 0.0, "brokerage": 0.0}
    sell_value = float(notional)
    buy_value  = float(notional)
    stt        = (sell_value + buy_value) * 0.001            # 0.1% both legs
    exch_rate  = 0.0000297 if exchange == "NSE" else 0.0000375
    exch       = (sell_value + buy_value) * exch_rate
    sebi       = (sell_value + buy_value) * 0.000001         # ₹10/cr
    stamp      = buy_value * 0.00015                          # 0.015% buy-side only
    ipft       = (sell_value + buy_value) * 0.000001 if exchange == "NSE" else 0.0
    brokerage  = 0.0
    gst        = (exch + sebi + ipft + brokerage) * 0.18
    total      = stt + exch + sebi + stamp + ipft + gst + brokerage
    return {
        "stt":       round(stt, 2),
        "exch":      round(exch, 2),
        "sebi":      round(sebi, 2),
        "stamp":     round(stamp, 2),
        "ipft":      round(ipft, 2),
        "gst":       round(gst, 2),
        "brokerage": brokerage,
        "total":     round(total, 2),
        "pct_of_notional": round(total / notional * 100, 3),
    }

def _auctions_state_path() -> str:
    d = _now_ist().strftime("%Y%m%d")
    return os.path.join(_CSV_DIR, f"auctions_state_{d}.json")

def _auctions_save():
    """Persist today's in-memory state to JSON so we survive restarts."""
    try:
        with _auctions_lock:
            data = {k: v for k, v in _auctions_state.items()}
        with open(_auctions_state_path(), "w") as f:
            json.dump(data, f, default=str, indent=2)
    except Exception as e:
        print(f"[auctions-save] {e}")

def _auctions_load():
    """Restore state if today's JSON exists (e.g. mid-day restart)."""
    try:
        path = _auctions_state_path()
        if not os.path.exists(path):
            return
        with open(path, "r") as f:
            data = json.load(f)
        with _auctions_lock:
            for k, v in data.items():
                _auctions_state[k] = v
        print(f"[auctions] restored state from {path}")
    except Exception as e:
        print(f"[auctions-load] {e}")

def _fetch_auctions() -> list:
    """Call kite.get_auction_instruments() and enrich with holdings + regular LTP."""
    rows = []
    try:
        rows = kite.get_auction_instruments() or []
    except Exception as e:
        with _auctions_lock:
            _auctions_state["last_error"] = f"fetch: {e}"
        print(f"[auctions-fetch] {e}")
        return []

    # Build a map of holdings by tradingsymbol+exchange for cross-reference
    hold_map = {}
    try:
        for h in (kite.holdings() or []):
            key = f"{h.get('exchange','')}:{h.get('tradingsymbol','')}"
            hold_map[key] = h
    except Exception as e:
        print(f"[auctions-holdings] {e}")

    # Regular-market LTPs for buyback estimate
    keys = [f"{r.get('exchange','NSE')}:{r.get('tradingsymbol','')}"
            for r in rows if r.get("tradingsymbol")]
    ltp_map = {}
    try:
        if keys:
            ltp_map = kite.ltp(keys) or {}
    except Exception as e:
        print(f"[auctions-ltp] {e}")

    enriched = []
    for r in rows:
        sym  = r.get("tradingsymbol", "")
        exch = r.get("exchange", "NSE")
        key  = f"{exch}:{sym}"
        hold = hold_map.get(key, {})
        ltp  = (ltp_map.get(key) or {}).get("last_price")
        close      = float(r.get("close_price")   or r.get("last_price") or 0)
        avg_cost   = float(hold.get("average_price") or 0)
        eligible_q = int(r.get("quantity") or hold.get("quantity") or 0)
        band_high  = round(close * 1.20, 2) if close else 0
        band_low   = round(close * 0.80, 2) if close else 0
        enriched.append({
            "auction_number":   r.get("auction_number"),
            "tradingsymbol":    sym,
            "exchange":         exch,
            "instrument_token": r.get("instrument_token"),
            "close_price":      close,
            "last_price":       float(r.get("last_price") or 0),
            "regular_ltp":      float(ltp) if ltp is not None else None,
            "quantity":         eligible_q,
            "holding_qty":      int(hold.get("quantity") or 0),
            "avg_cost":         avg_cost,
            "band_high":        band_high,
            "band_low":         band_low,
        })
    return enriched

def _estimate_pre_session(row: dict, premium_pct: float) -> dict:
    close = row.get("close_price") or 0
    qty   = row.get("quantity") or 0
    if close <= 0 or qty <= 0:
        return {"net_pnl": 0, "net_pct": 0, "costs": 0, "break_even_pct": 0}
    sell_est = close * (1 + premium_pct / 100.0)
    buy_est  = close  # buy back near close
    gross    = (sell_est - buy_est) * qty
    notional = sell_est * qty
    cost     = _equity_roundtrip_cost(notional, row.get("exchange", "NSE"))["total"]
    net      = gross - cost
    # Break-even: what premium % just covers costs?
    be_pct   = (cost / (close * qty)) * 100
    return {
        "sell_est":       round(sell_est, 2),
        "buy_est":        round(buy_est, 2),
        "gross_pnl":      round(gross, 2),
        "costs":          round(cost, 2),
        "net_pnl":        round(net, 2),
        "net_pct":        round(net / notional * 100, 3),
        "break_even_pct": round(be_pct, 3),
    }

def _estimate_live(row: dict, book: dict) -> dict:
    qty = row.get("quantity") or 0
    if qty <= 0 or not book:
        return {"net_pnl": 0, "net_pct": 0, "fill_prob": "no_data"}
    best_bid = book.get("best_bid") or 0
    reg_ltp  = row.get("regular_ltp") or row.get("close_price") or 0
    if best_bid <= 0 or reg_ltp <= 0:
        return {"net_pnl": 0, "net_pct": 0, "fill_prob": "no_book"}
    gross    = (best_bid - reg_ltp) * qty
    notional = best_bid * qty
    cost     = _equity_roundtrip_cost(notional, row.get("exchange", "NSE"))["total"]
    net      = gross - cost
    # Fill prob from depth: total buy qty at or above best_bid
    total_above = 0
    for lvl in (book.get("depth") or {}).get("buy", []):
        if (lvl.get("price") or 0) >= best_bid:
            total_above += int(lvl.get("quantity") or 0)
    if total_above >= qty:                fp = "full"
    elif total_above >= int(qty * 0.30):  fp = "partial"
    else:                                  fp = "thin"
    return {
        "sell_live":      round(best_bid, 2),
        "buy_live":       round(reg_ltp, 2),
        "gross_pnl":      round(gross, 2),
        "costs":          round(cost, 2),
        "net_pnl":        round(net, 2),
        "net_pct":        round(net / notional * 100, 3),
        "fill_prob":      fp,
        "buy_qty_above":  total_above,
    }

def _poll_auction_book():
    """Refresh _auctions_state['book'] from kite.quote() for each eligible row."""
    with _auctions_lock:
        rows = list(_auctions_state.get("list") or [])
    if not rows:
        return
    tokens = [r["instrument_token"] for r in rows if r.get("instrument_token")]
    if not tokens:
        return
    try:
        q = kite.quote(tokens)
    except Exception as e:
        with _auctions_lock:
            _auctions_state["last_error"] = f"book-poll: {e}"
        return
    now_iso = _now_ist().isoformat()
    new_book = {}
    for r in rows:
        an  = r.get("auction_number")
        tok = r.get("instrument_token")
        if not an or not tok:
            continue
        # kite.quote returns keys as str(token)
        qd = q.get(str(tok)) or q.get(tok) or {}
        depth = qd.get("depth") or {"buy": [], "sell": []}
        buy   = depth.get("buy")  or []
        sell  = depth.get("sell") or []
        new_book[str(an)] = {
            "best_bid":     (buy[0].get("price")     if buy  else 0),
            "best_bid_qty": (buy[0].get("quantity")  if buy  else 0),
            "best_ask":     (sell[0].get("price")    if sell else 0),
            "best_ask_qty": (sell[0].get("quantity") if sell else 0),
            "ltp":          qd.get("last_price") or 0,
            "ts":           now_iso,
            "depth":        depth,
        }
    with _auctions_lock:
        _auctions_state["book"] = new_book

_AUCTIONS_LAST_CHANCE_AT = dtime(14, 25)   # 5 min before window opens — final notification deadline

def _auctions_morning_fetch_loop():
    """Between fetch_from (default 13:30 IST) and the auction window open,
    keep refreshing the eligible list. Sends exactly one Telegram per day:

      • As soon as we first see a non-empty list  → the scrip listing
      • At 14:25 IST if still empty               → "no eligible auctions today"
    """
    notified_today = None
    while True:
        try:
            now = _now_ist()
            today = now.date().isoformat()
            try:
                fetch_from = datetime.strptime(
                    _auctions_config.get("fetch_from", "13:30"), "%H:%M"
                ).time()
            except ValueError:
                fetch_from = dtime(13, 30)
            with _auctions_lock:
                session_date = _auctions_state.get("session_date")
                current_list = list(_auctions_state.get("list") or [])
            have_data = (session_date == today and len(current_list) > 0)
            in_wait_window = (
                _is_trading_day(now.date())
                and fetch_from <= now.time() < _AUCTIONS_WINDOW_OPEN
            )
            if in_wait_window and not have_data:
                rows = _fetch_auctions()
                with _auctions_lock:
                    _auctions_state["list"]         = rows
                    _auctions_state["session_date"] = today
                    _auctions_state["fetched_at"]   = now.isoformat()
                _auctions_save()
                broadcast("auctions_update")
                have_data = bool(rows)
                if rows and notified_today != today:
                    notified_today = today
                    lines = "\n".join(
                        f"• `{r['tradingsymbol']}`  qty {r['quantity']}  · close ₹{r['close_price']:.2f}  · LTP ₹{(r['regular_ltp'] or r['last_price'] or 0):.2f}  · auction #{r['auction_number']}"
                        for r in rows[:20]
                    )
                    extra = f"\n…and {len(rows)-20} more" if len(rows) > 20 else ""
                    _telegram(
                        f"🔨 *Eligible auctions today* — {len(rows)} scrip(s)\n"
                        f"Window: 14:30–15:00 IST\n\n{lines}{extra}"
                    )
                else:
                    print(f"[auctions] poll: {len(rows)} rows")
            # Empty-day fallback: by 14:25 IST, if we still haven't notified,
            # ping a "nothing today" message so the user knows the system ran.
            if (_is_trading_day(now.date())
                and now.time() >= _AUCTIONS_LAST_CHANCE_AT
                and now.time() < _AUCTIONS_WINDOW_OPEN
                and notified_today != today
                and not have_data):
                notified_today = today
                _telegram(
                    "🔨 *No eligible auctions today*\n"
                    "Nothing in your holdings matches today's NSE auction session. "
                    "Auctions window 14:30–15:00 IST."
                )
        except Exception as e:
            print(f"[auctions-morning] {e}")
        # 60s cadence — small enough to fire the 14:25 message on time even if
        # we'd otherwise have landed between two 5-min slots. Cheap on quota.
        time.sleep(60)

def _auctions_book_loop():
    """During the auction window, poll the auction order book.

    Outside the final minute we fire on wall-clock seconds 1, 6, 11, ... —
    offset by 1 from the arb monitor's 0, 3, 6, ... slots so the two never
    collide on the same second except every 15 s (one collision per 15 calls
    is well inside Kite's burst tolerance).

    In the last 60 s we ramp to every second; the arb monitor steps aside
    during that minute via _ARB_AUCTION_PAUSE_FROM."""
    while True:
        try:
            now = _now_ist()
            t = now.time()
            if (_is_trading_day(now.date())
                and _AUCTIONS_WINDOW_OPEN <= t < _AUCTIONS_WINDOW_CLOSE):
                last_minute = (t >= _ARB_AUCTION_PAUSE_FROM)
                if last_minute or (now.second % 5 == 1):
                    _poll_auction_book()
                    broadcast("auctions_update")
            time.sleep(1)
        except Exception as e:
            print(f"[auctions-book] {e}")
            time.sleep(5)

def _auctions_snipe_loop():
    """Fires the snipe at T-`snipe_lead_seconds` before window close.
    Each scheduled scrip gets a fresh place_order at the live best bid
    (step-down to L2/L3 when our qty exceeds best-bid qty). No modification
    or cancellation is used — NSE disallows modifies during the auction
    session, and cancellation of partially filled orders is restricted."""
    fired_for_date = None
    while True:
        try:
            now = _now_ist()
            today = now.date().isoformat()
            lead = int(_auctions_config.get("snipe_lead_seconds", 5))
            close_dt = now.replace(hour=_AUCTIONS_WINDOW_CLOSE.hour,
                                   minute=_AUCTIONS_WINDOW_CLOSE.minute,
                                   second=0, microsecond=0)
            fire_dt  = close_dt - timedelta(seconds=lead)
            if (_is_trading_day(now.date())
                and fired_for_date != today
                and now >= fire_dt
                and now < close_dt):
                _execute_snipe()
                fired_for_date = today
            time.sleep(0.5)
        except Exception as e:
            print(f"[auctions-snipe] {e}")
            time.sleep(2)

def _execute_snipe():
    """For each scrip we scheduled, place a fresh sell order at the live best
    bid (or deeper if our qty exceeds that level)."""
    with _auctions_lock:
        rows      = list(_auctions_state.get("list") or [])
        scheduled = dict(_auctions_state.get("scheduled") or {})
        book      = dict(_auctions_state.get("book") or {})
    for r in rows:
        an = str(r.get("auction_number") or "")
        if an not in scheduled:
            continue
        rec = scheduled[an]
        our_qty = int(rec.get("qty") or r.get("quantity") or 0)
        if our_qty <= 0:
            continue
        b = book.get(an) or {}
        levels = (b.get("depth") or {}).get("buy") or []
        if not levels:
            err = "no live book — cannot determine snipe price"
            print(f"[auctions-snipe] {r['tradingsymbol']}: {err}")
            with _auctions_lock:
                _auctions_state["snipes"][an] = {
                    "target_price": None,
                    "placed_at":    _now_ist().isoformat(),
                    "attempted":    True,
                    "error":        err,
                }
            _telegram(f"⚠️ Snipe {r['tradingsymbol']} aborted: {err}")
            continue
        # Walk buy levels best→worst; find price at which cumulative qty covers our_qty.
        cum = 0
        target = float(levels[0].get("price") or 0)
        for lvl in levels:
            cum   += int(lvl.get("quantity") or 0)
            target = float(lvl.get("price") or target)
            if cum >= our_qty:
                break
        if target <= 0:
            continue
        try:
            oid = kite.place_order(
                variety=kite.VARIETY_AUCTION,
                exchange=r["exchange"],
                tradingsymbol=r["tradingsymbol"],
                transaction_type=kite.TRANSACTION_TYPE_SELL,
                quantity=our_qty,
                product=kite.PRODUCT_CNC,
                order_type=kite.ORDER_TYPE_LIMIT,
                price=target,
                auction_number=r["auction_number"],
                tag="auction_snipe",
            )
            with _auctions_lock:
                _auctions_state["snipes"][an] = {
                    "order_id":     oid,
                    "target_price": target,
                    "qty":          our_qty,
                    "placed_at":    _now_ist().isoformat(),
                    "attempted":    True,
                }
            _telegram(
                f"🎯 *Snipe placed* {r['tradingsymbol']}\n"
                f"Sell {our_qty} @ ₹{target:.2f}  (cumulative buy qty above this price ≥ {our_qty})\n"
                f"Order #{oid}"
            )
        except Exception as e:
            print(f"[auctions-snipe-place] {r['tradingsymbol']}: {e}")
            with _auctions_lock:
                _auctions_state["snipes"][an] = {
                    "target_price": target,
                    "placed_at":    _now_ist().isoformat(),
                    "attempted":    True,
                    "error":        str(e),
                }
            _telegram(f"⚠️ Snipe failed for {r['tradingsymbol']}: {e}")
    _auctions_save()
    broadcast("auctions_update")

def _auctions_buyback_loop():
    """At 14:46 IST, for each scrip with a non-zero auction fill, place a CNC
    buy-back order at the current ask (or +tick buffer)."""
    fired_for_date = None
    while True:
        try:
            now = _now_ist()
            today = now.date().isoformat()
            if (_is_trading_day(now.date())
                and _auctions_config.get("auto_buyback")
                and fired_for_date != today
                and now.time() >= _AUCTIONS_BUYBACK_AT
                and now.time() < dtime(15, 25)):
                _execute_buybacks()
                fired_for_date = today
            time.sleep(2)
        except Exception as e:
            print(f"[auctions-buyback] {e}")
            time.sleep(5)

def _execute_buybacks():
    """For each auction order with a fill, place a regular CNC buy for that qty."""
    try:
        live_orders = {str(o["order_id"]): o for o in (kite.orders() or [])}
    except Exception as e:
        print(f"[auctions-buyback-orders] {e}")
        return
    with _auctions_lock:
        rows   = list(_auctions_state.get("list") or [])
        snipes = dict(_auctions_state.get("snipes") or {})
    for r in rows:
        an = str(r.get("auction_number") or "")
        rec = snipes.get(an) or {}
        oid = rec.get("order_id")
        if not oid:
            continue
        o = live_orders.get(str(oid))
        if not o:
            continue
        filled_qty = int(o.get("filled_quantity") or 0)
        if filled_qty <= 0:
            continue
        # Get current ask in regular market
        key = f"{r.get('exchange','NSE')}:{r.get('tradingsymbol','')}"
        try:
            q = kite.quote([key])[key]
            ask = (q.get("depth", {}).get("sell") or [{}])[0].get("price") or q.get("last_price") or 0
        except Exception as e:
            print(f"[auctions-buyback-quote] {r['tradingsymbol']}: {e}")
            continue
        buy_price = round(ask * 1.0005, 2)  # cross by 5bps for fill confidence
        try:
            bid = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=r["exchange"],
                tradingsymbol=r["tradingsymbol"],
                transaction_type=kite.TRANSACTION_TYPE_BUY,
                quantity=filled_qty,
                product=kite.PRODUCT_CNC,
                order_type=kite.ORDER_TYPE_LIMIT,
                price=buy_price,
                tag="auction_buyback",
            )
            with _auctions_lock:
                _auctions_state["buybacks"][an] = {
                    "order_id":   bid,
                    "qty":        filled_qty,
                    "price":      buy_price,
                    "placed_at":  _now_ist().isoformat(),
                }
            avg_fill = float(o.get("average_price") or 0)
            gross = (avg_fill - buy_price) * filled_qty
            cost  = _equity_roundtrip_cost(avg_fill * filled_qty, r.get("exchange","NSE"))["total"]
            net   = gross - cost
            _telegram(
                f"🔁 Buyback {r['tradingsymbol']} qty={filled_qty} @ ₹{buy_price:.2f}\n"
                f"Auction avg: ₹{avg_fill:.2f}  →  Round-trip est P&L: ₹{net:+.0f}"
            )
            _auctions_log_csv(r, o, buy_price, filled_qty, gross, cost, net)
        except Exception as e:
            print(f"[auctions-buyback-place] {r['tradingsymbol']}: {e}")
            _telegram(f"⚠️ Buyback failed for {r['tradingsymbol']}: {e}\nDecide manually.")
    _auctions_save()
    broadcast("auctions_update")

def _auctions_log_csv(row: dict, auction_order: dict, buy_price: float,
                      qty: int, gross: float, cost: float, net: float):
    try:
        d = _now_ist().strftime("%Y%m%d")
        path = os.path.join(_CSV_DIR, f"auctions_{d}.csv")
        write_header = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(["date","symbol","exchange","qty",
                            "auction_avg_price","buyback_price",
                            "gross_pnl","costs","net_pnl",
                            "auction_order_id","close_price","holding_avg"])
            w.writerow([
                _now_ist().isoformat(),
                row.get("tradingsymbol"), row.get("exchange"), qty,
                auction_order.get("average_price"), buy_price,
                round(gross,2), round(cost,2), round(net,2),
                auction_order.get("order_id"),
                row.get("close_price"), row.get("avg_cost"),
            ])
    except Exception as e:
        print(f"[auctions-csv] {e}")

# Boot
_auctions_load()
threading.Thread(target=_auctions_morning_fetch_loop, daemon=True).start()
threading.Thread(target=_auctions_book_loop,          daemon=True).start()
threading.Thread(target=_auctions_snipe_loop,         daemon=True).start()
threading.Thread(target=_auctions_buyback_loop,       daemon=True).start()


@app.route("/auctions", methods=["GET"])
def auctions_route():
    """Returns eligible list + book + estimates + config. Frontend polls this."""
    with _auctions_lock:
        rows      = list(_auctions_state.get("list") or [])
        book      = dict(_auctions_state.get("book") or {})
        scheduled = dict(_auctions_state.get("scheduled") or {})
        snipes    = dict(_auctions_state.get("snipes") or {})
        buys      = dict(_auctions_state.get("buybacks") or {})
        fetched_at   = _auctions_state.get("fetched_at")
        session_date = _auctions_state.get("session_date")
        today_notional = _auctions_state.get("today_notional") or 0.0
        err = _auctions_state.get("last_error")
    premium = float(_auctions_config.get("assumed_premium_pct") or 3.0)
    enriched = []
    for r in rows:
        an = str(r.get("auction_number") or "")
        b  = book.get(an) or {}
        pre  = _estimate_pre_session(r, premium)
        live = _estimate_live(r, b) if b else None
        enriched.append({
            **r,
            "book":          b,
            "pre_estimate":  pre,
            "live_estimate": live,
            "scheduled":     scheduled.get(an),
            "snipe":         snipes.get(an),
            "buyback":       buys.get(an),
        })
    return jsonify({
        "ok":             True,
        "session_date":   session_date,
        "fetched_at":     fetched_at,
        "rows":           enriched,
        "config":         dict(_auctions_config),
        "today_notional": today_notional,
        "last_error":     err,
        "window": {
            "open":  _AUCTIONS_WINDOW_OPEN.strftime("%H:%M"),
            "close": _AUCTIONS_WINDOW_CLOSE.strftime("%H:%M"),
        },
    })

@app.route("/auctions/refresh", methods=["POST"])
def auctions_refresh_route():
    rows = _fetch_auctions()
    today = _now_ist().date().isoformat()
    with _auctions_lock:
        _auctions_state["list"]         = rows
        _auctions_state["session_date"] = today
        _auctions_state["fetched_at"]   = _now_ist().isoformat()
    _auctions_save()
    broadcast("auctions_update")
    return jsonify({"ok": True, "count": len(rows)})

@app.route("/auctions/config", methods=["POST"])
def auctions_config_route():
    d = request.json or {}
    for k in ("assumed_premium_pct", "max_notional_per_day",
              "max_notional_per_scrip", "snipe_lead_seconds"):
        if k in d:
            _auctions_config[k] = float(d[k]) if k != "snipe_lead_seconds" else max(2, int(d[k]))
    if "auto_buyback" in d:
        _auctions_config["auto_buyback"] = bool(d["auto_buyback"])
    if "fetch_from" in d:
        # validate HH:MM
        try:
            datetime.strptime(str(d["fetch_from"]), "%H:%M")
            _auctions_config["fetch_from"] = str(d["fetch_from"])
        except (ValueError, TypeError):
            pass
    return jsonify({"ok": True, "config": dict(_auctions_config)})

@app.route("/auctions/schedule/<an>", methods=["POST"])
def auctions_schedule_route(an: str):
    """Schedule a snipe for this scrip. No order is placed now — the snipe
    loop will place a fresh sell at T-`snipe_lead_seconds` before window close,
    targeting the live best bid.

    Body: {qty: int} (defaults to row['quantity'])."""
    d = request.json or {}
    with _auctions_lock:
        row = next((r for r in (_auctions_state.get("list") or [])
                    if str(r.get("auction_number")) == str(an)), None)
        today_notional = _auctions_state.get("today_notional") or 0.0
        already        = (_auctions_state.get("scheduled") or {}).get(str(an))
    if not row:
        return jsonify({"ok": False, "error": "auction not found"}), 404
    if already:
        return jsonify({"ok": False, "error": "already scheduled"}), 400
    qty = int(d.get("qty") or row.get("quantity") or 0)
    if qty <= 0 or qty > int(row.get("quantity") or 0):
        return jsonify({"ok": False, "error": "invalid qty"}), 400
    # Cap checks: use band_high as the worst-case notional reservation.
    notional_est = float(row.get("band_high") or row.get("close_price") or 0) * qty
    cap_day      = float(_auctions_config.get("max_notional_per_day") or 0)
    cap_scrip    = float(_auctions_config.get("max_notional_per_scrip") or 0)
    if cap_scrip and notional_est > cap_scrip:
        return jsonify({"ok": False,
                        "error": f"per-scrip cap exceeded: ₹{notional_est:,.0f} > ₹{cap_scrip:,.0f}"}), 400
    if cap_day and (today_notional + notional_est) > cap_day:
        return jsonify({"ok": False,
                        "error": f"day cap exceeded: ₹{today_notional+notional_est:,.0f} > ₹{cap_day:,.0f}"}), 400
    with _auctions_lock:
        _auctions_state["scheduled"][str(an)] = {
            "qty":          qty,
            "scheduled_at": _now_ist().isoformat(),
        }
        _auctions_state["today_notional"] = today_notional + notional_est
    _auctions_save()
    lead = int(_auctions_config.get("snipe_lead_seconds", 5))
    fire_t = (datetime.combine(_now_ist().date(), _AUCTIONS_WINDOW_CLOSE)
              - timedelta(seconds=lead)).strftime("%H:%M:%S")
    _telegram(
        f"📌 Snipe scheduled — {row['tradingsymbol']} qty={qty}\n"
        f"Will place fresh order at {fire_t} IST targeting live best bid"
    )
    broadcast("auctions_update")
    return jsonify({"ok": True, "qty": qty, "fire_at": fire_t})

@app.route("/auctions/unschedule/<an>", methods=["POST"])
def auctions_unschedule_route(an: str):
    """Remove the scheduled snipe. If the snipe has already fired (order
    placed), the user must cancel it from Kite directly — we do not call
    cancel here because NSE restricts cancellation of pending/partial auction
    orders."""
    with _auctions_lock:
        rec = (_auctions_state.get("scheduled") or {}).get(str(an))
        snipe_done = (_auctions_state.get("snipes") or {}).get(str(an), {}).get("order_id")
    if not rec:
        return jsonify({"ok": False, "error": "not scheduled"}), 404
    if snipe_done:
        return jsonify({"ok": False,
                        "error": "snipe already placed (order live in Kite — cancel from Kite if still open)"}), 400
    # Refund the reserved notional from today's bucket
    with _auctions_lock:
        row = next((r for r in (_auctions_state.get("list") or [])
                    if str(r.get("auction_number")) == str(an)), None)
        refund = float((row or {}).get("band_high") or (row or {}).get("close_price") or 0) * int(rec.get("qty") or 0)
        _auctions_state["scheduled"].pop(str(an), None)
        _auctions_state["today_notional"] = max(0.0,
            (_auctions_state.get("today_notional") or 0.0) - refund)
    _auctions_save()
    broadcast("auctions_update")
    return jsonify({"ok": True})

@app.route("/auctions/buyback/<an>", methods=["POST"])
def auctions_buyback_route(an: str):
    """Manual single-scrip buyback trigger (used when auto_buyback is off)."""
    threading.Thread(target=_execute_buybacks, daemon=True).start()
    return jsonify({"ok": True, "msg": "Buyback started for any filled auctions"})


# ── Short Straddle (combined-premium + VWAP + prior-day levels) ───────────────
#
# Strategy: short ATM Nifty weekly straddle when combined premium (CE+PE) drops
# below both prior-day's close+low AND below today's session VWAP. Exit on VWAP
# cross-back-above (5-min bar close). Hard SL on combined premium (tick-level).
# Up to 3 re-entries/day. Force square-off at 15:15 IST.

_STRADDLE_ATM_LOCK_AT   = dtime(9, 16)
_STRADDLE_BAR_INTERVAL  = 5 * 60        # seconds per bar
_STRADDLE_TICK_HZ       = 1             # poll cadence inside the tick loop

_straddle_config = {
    "underlying":         "NIFTY",
    "lots":               1,
    "sl_pts":             25,
    "max_reentries":      3,
    "skip_filter_pct":    40.0,         # skip if first bar combined <= prior_low × (1 - pct/100)
    "paper_mode":         True,
    "active":             False,        # master kill switch
    "entry_from":         "09:20",
    "entry_to":           "15:00",
    "squareoff_at":       "15:15",
}

_straddle_state: dict = {
    "session_date":     None,
    "atm_strike":       None,
    "expiry":           None,           # ISO date string
    "ce_symbol":        None, "ce_token": None,
    "pe_symbol":        None, "pe_token": None,
    "prior_close":      None,           # combined yesterday's last 5-min close
    "prior_low":        None,           # combined yesterday's min
    "today_first_combined": None,
    "skip_today":       False,
    "skip_reason":      None,
    "bars":             [],             # closed 5-min bars: {ts_start, combined_o,h,l,c, vol_total}
    "current_bar":      None,           # in-progress bar
    "vwap":             None,
    "_vwap_num":        0.0,
    "_vwap_den":        0.0,
    "last_combined":    None,
    "last_ce":          None,
    "last_pe":          None,
    "position":         None,           # see _straddle_enter for shape
    "reentries_used":   0,
    "trades":           [],             # finalized round-trips today
    "signals":          [],             # event log
    "last_error":       None,
    "lifecycle":        "idle",         # idle / armed / in_position / squared_off / skipped
}
_straddle_lock = threading.Lock()


def _current_weekly_expiry(today: date | None = None) -> date:
    """Nearest weekly Nifty expiry (Tuesday), shifted back if it's a holiday.
    If today IS the expiry day and market is open, returns today."""
    today = today or _now_ist().date()
    days_to_tue = (1 - today.weekday()) % 7   # Mon=0, Tue=1
    exp = today + timedelta(days=days_to_tue)
    return _shift_to_trading_day(exp)

def _straddle_log(event: str, **extra):
    """Append to signals + persist to today's CSV."""
    ts = _now_ist().isoformat()
    entry = {"ts": ts, "event": event, **extra}
    with _straddle_lock:
        _straddle_state["signals"].insert(0, entry)
        _straddle_state["signals"] = _straddle_state["signals"][:200]
    try:
        d = _now_ist().strftime("%Y%m%d")
        path = os.path.join(_CSV_DIR, f"straddle_{d}.csv")
        write_header = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(["ts","event","atm","expiry","prior_close","prior_low",
                            "combined","vwap","ce","pe","reason","pnl_pts","mode","extra"])
            with _straddle_lock:
                s = _straddle_state
                w.writerow([
                    ts, event, s.get("atm_strike"), s.get("expiry"),
                    s.get("prior_close"), s.get("prior_low"),
                    s.get("last_combined"), s.get("vwap"),
                    s.get("last_ce"), s.get("last_pe"),
                    extra.get("reason",""), extra.get("pnl_pts",""),
                    "paper" if _straddle_config.get("paper_mode") else "live",
                    json.dumps({k:v for k,v in extra.items() if k not in ("reason","pnl_pts")}, default=str),
                ])
    except Exception as e:
        print(f"[straddle-csv] {e}")


def _straddle_pick_atm() -> int | None:
    """Round Nifty spot to nearest 50."""
    try:
        q = kite.quote(["NSE:NIFTY 50"])["NSE:NIFTY 50"]
        spot = float(q.get("last_price") or 0)
        if spot <= 0:
            return None
        return int(round(spot / 50.0) * 50)
    except Exception as e:
        print(f"[straddle-atm] {e}")
        return None


def _straddle_instrument_token(tradingsymbol: str, exchange: str = "NFO") -> int | None:
    """Look up instrument_token via dump. Falls back to None on miss."""
    try:
        instruments = kite.instruments(exchange)
    except Exception as e:
        print(f"[straddle-tokens] {e}")
        return None
    for inst in instruments:
        if inst.get("tradingsymbol") == tradingsymbol:
            return inst.get("instrument_token")
    return None


def _straddle_setup_today():
    """At 09:16 IST: lock ATM, compute prior-day reference from historical bars."""
    today = _now_ist().date()
    atm = _straddle_pick_atm()
    if not atm:
        _straddle_state["last_error"] = "could not fetch spot for ATM"
        return False
    expiry = _current_weekly_expiry(today)
    ce_sym = _nifty_option_symbol(atm, "CE", expiry)
    pe_sym = _nifty_option_symbol(atm, "PE", expiry)
    ce_tok = _straddle_instrument_token(ce_sym)
    pe_tok = _straddle_instrument_token(pe_sym)
    if not ce_tok or not pe_tok:
        _straddle_state["last_error"] = f"token lookup failed: {ce_sym}/{pe_sym}"
        return False

    # Prior-day combined series from kite.historical_data
    yday = _shift_to_trading_day(today - timedelta(days=1))
    from_dt = datetime.combine(yday, dtime(9, 15))
    to_dt   = datetime.combine(yday, dtime(15, 30))
    try:
        ce_bars = kite.historical_data(ce_tok, from_dt, to_dt, interval="5minute") or []
        pe_bars = kite.historical_data(pe_tok, from_dt, to_dt, interval="5minute") or []
    except Exception as e:
        _straddle_state["last_error"] = f"historical fetch: {e}"
        return False

    # Align bars by timestamp; combine
    ce_map = {b["date"].strftime("%H:%M"): b for b in ce_bars}
    pe_map = {b["date"].strftime("%H:%M"): b for b in pe_bars}
    common = sorted(set(ce_map) & set(pe_map))
    if not common:
        _straddle_state["last_error"] = "no overlapping prior-day bars"
        return False
    combined_series = [(t, ce_map[t]["close"] + pe_map[t]["close"]) for t in common]
    combined_lows   = [ce_map[t]["low"] + pe_map[t]["low"] for t in common]
    prior_close     = combined_series[-1][1]
    prior_low       = min(combined_lows)

    with _straddle_lock:
        _straddle_state.update({
            "session_date":     today.isoformat(),
            "atm_strike":       atm,
            "expiry":           expiry.isoformat(),
            "ce_symbol":        ce_sym, "ce_token": ce_tok,
            "pe_symbol":        pe_sym, "pe_token": pe_tok,
            "prior_close":      round(prior_close, 2),
            "prior_low":        round(prior_low, 2),
            "today_first_combined": None,
            "skip_today":       False,
            "skip_reason":      None,
            "bars":             [],
            "current_bar":      None,
            "vwap":             None,
            "_vwap_num":        0.0,
            "_vwap_den":        0.0,
            "last_combined":    None,
            "last_ce":          None,
            "last_pe":          None,
            "position":         None,
            "reentries_used":   0,
            "trades":           [],
            "signals":          [],
            "last_error":       None,
            "lifecycle":        "armed",
        })
    _straddle_log("setup", atm=atm, expiry=expiry.isoformat(),
                  prior_close=prior_close, prior_low=prior_low,
                  ce_sym=ce_sym, pe_sym=pe_sym)
    _telegram(
        f"🎯 *Straddle armed* — NIFTY {atm} ({expiry.strftime('%d-%b')})\n"
        f"Prior close: ₹{prior_close:.2f}  ·  Prior low: ₹{prior_low:.2f}\n"
        f"Mode: *{'PAPER' if _straddle_config['paper_mode'] else 'LIVE'}*"
    )
    broadcast("straddle_update")
    return True


def _straddle_apply_skip_filter(first_combined: float):
    """If today's first bar combined is well below prior_low, skip the day."""
    with _straddle_lock:
        pl = _straddle_state.get("prior_low")
        pct = float(_straddle_config.get("skip_filter_pct") or 40.0)
    if not pl or pl <= 0:
        return
    threshold = pl * (1 - pct / 100.0)
    if first_combined <= threshold:
        with _straddle_lock:
            _straddle_state["skip_today"]  = True
            _straddle_state["skip_reason"] = (
                f"first bar {first_combined:.2f} ≤ prior_low×{(1-pct/100):.2f} = {threshold:.2f}"
            )
            _straddle_state["lifecycle"]   = "skipped"
        _straddle_log("skip_filter_hit",
                      first_combined=first_combined, threshold=threshold)
        _telegram(f"⏭ Straddle skip — first bar combined {first_combined:.2f} ≤ {threshold:.2f}")


def _straddle_finalize_bar(bar: dict):
    """Bar just closed. Update VWAP and append. Then evaluate signals."""
    typical = (bar["combined_h"] + bar["combined_l"] + bar["combined_c"]) / 3.0
    weight  = max(int(bar.get("vol_total") or 0), 1)
    with _straddle_lock:
        _straddle_state["_vwap_num"] += typical * weight
        _straddle_state["_vwap_den"] += weight
        vwap = _straddle_state["_vwap_num"] / _straddle_state["_vwap_den"]
        _straddle_state["vwap"] = round(vwap, 2)
        _straddle_state["bars"].append(bar)
        first_bar = (_straddle_state.get("today_first_combined") is None)
        if first_bar:
            _straddle_state["today_first_combined"] = bar["combined_c"]
    if first_bar:
        _straddle_apply_skip_filter(bar["combined_c"])
    _straddle_log("bar_close",
                  bar_ts=bar["ts_start"],
                  combined_o=bar["combined_o"], combined_h=bar["combined_h"],
                  combined_l=bar["combined_l"], combined_c=bar["combined_c"],
                  vwap=round(vwap, 2))
    _straddle_evaluate_signals(bar)


def _straddle_in_entry_window() -> bool:
    cfg = _straddle_config
    now_t = _now_ist().time()
    f = datetime.strptime(cfg["entry_from"], "%H:%M").time()
    t = datetime.strptime(cfg["entry_to"],   "%H:%M").time()
    return f <= now_t < t


def _straddle_evaluate_signals(closed_bar: dict):
    """Apply entry/exit logic at 5-min bar close."""
    with _straddle_lock:
        if (not _straddle_config.get("active")
            or _straddle_state.get("skip_today")
            or _straddle_state.get("lifecycle") in ("squared_off", "skipped")):
            return
        pos      = _straddle_state.get("position")
        pc       = _straddle_state.get("prior_close")
        pl       = _straddle_state.get("prior_low")
        vwap     = _straddle_state.get("vwap")
        reused   = _straddle_state.get("reentries_used") or 0
        max_re   = int(_straddle_config.get("max_reentries") or 3)
    combined = closed_bar["combined_c"]

    if pos is None:
        # ENTRY?
        if not _straddle_in_entry_window():
            return
        if reused >= max_re + 1:           # +1 to allow initial + N reentries? we use "fresh entries" semantics
            return
        if pc is None or pl is None or vwap is None:
            return
        if combined < pl and combined < pc and combined < vwap:
            _straddle_enter_position(combined)
    else:
        # EXIT on VWAP cross-up at bar close?
        if vwap is not None and combined > vwap:
            _straddle_exit_position("vwap_cross_up", combined)


def _straddle_enter_position(combined: float):
    """Place CE + PE sells in parallel (or paper-record)."""
    with _straddle_lock:
        cfg  = dict(_straddle_config)
        s    = dict(_straddle_state)
    lots    = int(cfg.get("lots") or 1)
    qty_leg = lots * 65            # NIFTY lot
    paper   = bool(cfg.get("paper_mode"))
    ce_sym, pe_sym = s["ce_symbol"], s["pe_symbol"]
    ce_ltp = s.get("last_ce") or 0
    pe_ltp = s.get("last_pe") or 0

    if paper:
        ce_oid, pe_oid = "PAPER-CE", "PAPER-PE"
        ce_fill, pe_fill = ce_ltp, pe_ltp
    else:
        try:
            with ThreadPoolExecutor(max_workers=2) as ex:
                f_ce = ex.submit(kite.place_order,
                                 variety=kite.VARIETY_REGULAR,
                                 exchange="NFO", tradingsymbol=ce_sym,
                                 transaction_type=kite.TRANSACTION_TYPE_SELL,
                                 quantity=qty_leg, product=kite.PRODUCT_NRML,
                                 order_type=kite.ORDER_TYPE_MARKET,
                                 tag="straddle_entry")
                f_pe = ex.submit(kite.place_order,
                                 variety=kite.VARIETY_REGULAR,
                                 exchange="NFO", tradingsymbol=pe_sym,
                                 transaction_type=kite.TRANSACTION_TYPE_SELL,
                                 quantity=qty_leg, product=kite.PRODUCT_NRML,
                                 order_type=kite.ORDER_TYPE_MARKET,
                                 tag="straddle_entry")
                ce_oid = f_ce.result(timeout=10)
                pe_oid = f_pe.result(timeout=10)
            # Best-effort fill price from quote (market orders fill ~immediately)
            ce_fill, pe_fill = ce_ltp, pe_ltp
        except Exception as e:
            _straddle_log("entry_error", reason=str(e))
            _telegram(f"⚠️ Straddle entry failed: {e}")
            return

    entry_combined = ce_fill + pe_fill
    sl_pts = float(cfg.get("sl_pts") or 25)
    pos = {
        "entered_at":     _now_ist().isoformat(),
        "ce_sell_price":  ce_fill,
        "pe_sell_price":  pe_fill,
        "entry_combined": entry_combined,
        "qty_per_leg":    qty_leg,
        "ce_order_id":    ce_oid,
        "pe_order_id":    pe_oid,
        "sl_combined":    entry_combined + sl_pts,
        "sl_pts":         sl_pts,
    }
    with _straddle_lock:
        _straddle_state["position"]  = pos
        _straddle_state["lifecycle"] = "in_position"
    _straddle_log("entry",
                  ce=ce_fill, pe=pe_fill, combined=entry_combined,
                  sl=pos["sl_combined"], qty_per_leg=qty_leg,
                  reentry_num=(_straddle_state.get("reentries_used") or 0))
    _telegram(
        f"📍 *Straddle entered* {'(PAPER)' if paper else ''}\n"
        f"CE sell ₹{ce_fill:.2f} · PE sell ₹{pe_fill:.2f}\n"
        f"Combined: ₹{entry_combined:.2f}  ·  SL: ₹{pos['sl_combined']:.2f} ({sl_pts:+.0f} pts)\n"
        f"Qty/leg: {qty_leg}"
    )
    broadcast("straddle_update")


def _straddle_exit_position(reason: str, combined_at_exit: float):
    """Buy back CE + PE (or paper-record). Bumps reentries_used."""
    with _straddle_lock:
        cfg  = dict(_straddle_config)
        s    = dict(_straddle_state)
        pos  = s.get("position")
    if not pos:
        return
    paper   = bool(cfg.get("paper_mode"))
    qty_leg = int(pos["qty_per_leg"])
    ce_sym, pe_sym = s["ce_symbol"], s["pe_symbol"]
    ce_ltp = s.get("last_ce") or 0
    pe_ltp = s.get("last_pe") or 0

    if paper:
        ce_buy, pe_buy = ce_ltp, pe_ltp
    else:
        try:
            with ThreadPoolExecutor(max_workers=2) as ex:
                f_ce = ex.submit(kite.place_order,
                                 variety=kite.VARIETY_REGULAR,
                                 exchange="NFO", tradingsymbol=ce_sym,
                                 transaction_type=kite.TRANSACTION_TYPE_BUY,
                                 quantity=qty_leg, product=kite.PRODUCT_NRML,
                                 order_type=kite.ORDER_TYPE_MARKET,
                                 tag="straddle_exit")
                f_pe = ex.submit(kite.place_order,
                                 variety=kite.VARIETY_REGULAR,
                                 exchange="NFO", tradingsymbol=pe_sym,
                                 transaction_type=kite.TRANSACTION_TYPE_BUY,
                                 quantity=qty_leg, product=kite.PRODUCT_NRML,
                                 order_type=kite.ORDER_TYPE_MARKET,
                                 tag="straddle_exit")
                f_ce.result(timeout=10); f_pe.result(timeout=10)
            ce_buy, pe_buy = ce_ltp, pe_ltp
        except Exception as e:
            _straddle_log("exit_error", reason=str(e))
            _telegram(f"⚠️ Straddle exit failed: {e}")
            return

    exit_combined = ce_buy + pe_buy
    pnl_pts = pos["entry_combined"] - exit_combined          # short straddle: lower combined = profit
    pnl_inr = pnl_pts * qty_leg                              # one leg's qty (since pts × qty applies to combined)
    trade = {
        "entered_at":     pos["entered_at"],
        "exited_at":      _now_ist().isoformat(),
        "entry_combined": pos["entry_combined"],
        "exit_combined":  round(exit_combined, 2),
        "ce_sell":        pos["ce_sell_price"],
        "pe_sell":        pos["pe_sell_price"],
        "ce_buy":         round(ce_buy, 2),
        "pe_buy":         round(pe_buy, 2),
        "reason":         reason,
        "pnl_pts":        round(pnl_pts, 2),
        "pnl_inr":        round(pnl_inr, 2),
        "mode":           "paper" if paper else "live",
    }
    with _straddle_lock:
        _straddle_state["trades"].append(trade)
        _straddle_state["position"] = None
        _straddle_state["reentries_used"] = (_straddle_state.get("reentries_used") or 0) + 1
        # lifecycle reset so we can re-enter (unless EOD or max reentries reached)
        if _straddle_state["reentries_used"] >= int(cfg.get("max_reentries", 3)) + 1:
            _straddle_state["lifecycle"] = "squared_off"
        else:
            _straddle_state["lifecycle"] = "armed"
    _straddle_log("exit", reason=reason,
                  exit_combined=exit_combined, pnl_pts=pnl_pts, pnl_inr=pnl_inr)
    _telegram(
        f"📤 *Straddle exit* — {reason}\n"
        f"Combined: ₹{pos['entry_combined']:.2f} → ₹{exit_combined:.2f}\n"
        f"P&L: ₹{pnl_inr:+,.0f} ({pnl_pts:+.2f} pts)"
    )
    broadcast("straddle_update")


def _straddle_check_sl(latest_combined: float):
    """Tick-level SL: combined breached upward by sl_pts from entry."""
    with _straddle_lock:
        pos = _straddle_state.get("position")
    if not pos:
        return
    if latest_combined >= pos["sl_combined"]:
        _straddle_exit_position("stop_loss", latest_combined)


def _straddle_tick_loop():
    """1 Hz tick poller: builds bars + tick-level SL check."""
    while True:
        try:
            now = _now_ist()
            t   = now.time()
            with _straddle_lock:
                active   = bool(_straddle_config.get("active"))
                session  = _straddle_state.get("session_date")
                ce_tok   = _straddle_state.get("ce_token")
                pe_tok   = _straddle_state.get("pe_token")
                skipped  = _straddle_state.get("skip_today")
            today_iso = now.date().isoformat()
            in_window = (_is_trading_day(now.date())
                         and dtime(9, 15) <= t < dtime(15, 25)
                         and active
                         and session == today_iso
                         and ce_tok and pe_tok
                         and not skipped)
            if in_window:
                # poll on second%2 == 0 to avoid colliding with auctions(%5==1) and arb(%3==0)
                if now.second % 2 == 0:
                    _straddle_poll_once(now)
        except Exception as e:
            print(f"[straddle-tick] {e}")
            with _straddle_lock:
                _straddle_state["last_error"] = str(e)
        time.sleep(1)


def _straddle_poll_once(now: datetime):
    """One tick: fetch CE+PE quote, update last_combined, accrete bar, check SL."""
    with _straddle_lock:
        ce_tok = _straddle_state.get("ce_token")
        pe_tok = _straddle_state.get("pe_token")
    if not ce_tok or not pe_tok:
        return
    try:
        q = kite.quote([ce_tok, pe_tok])
    except Exception as e:
        print(f"[straddle-quote] {e}")
        return
    ce_q = q.get(str(ce_tok)) or {}
    pe_q = q.get(str(pe_tok)) or {}
    ce_ltp = float(ce_q.get("last_price") or 0)
    pe_ltp = float(pe_q.get("last_price") or 0)
    ce_vol = int(ce_q.get("volume") or 0)
    pe_vol = int(pe_q.get("volume") or 0)
    if ce_ltp <= 0 or pe_ltp <= 0:
        return
    combined = round(ce_ltp + pe_ltp, 2)
    with _straddle_lock:
        _straddle_state["last_ce"]       = ce_ltp
        _straddle_state["last_pe"]       = pe_ltp
        _straddle_state["last_combined"] = combined
        bar = _straddle_state.get("current_bar")
        bar_start = _straddle_bar_start(now)
        if bar is None or bar["ts_start"] != bar_start:
            # close prior bar if exists
            if bar is not None:
                _straddle_state["current_bar"] = None
                bar_to_finalize = bar
            else:
                bar_to_finalize = None
            # open new bar
            _straddle_state["current_bar"] = {
                "ts_start":   bar_start,
                "combined_o": combined,
                "combined_h": combined,
                "combined_l": combined,
                "combined_c": combined,
                "vol_total":  ce_vol + pe_vol,
                "_vol_at_open": ce_vol + pe_vol,
            }
        else:
            bar["combined_h"] = max(bar["combined_h"], combined)
            bar["combined_l"] = min(bar["combined_l"], combined)
            bar["combined_c"] = combined
            # bar volume = volume since bar open (incremental)
            bar["vol_total"]  = max(0, (ce_vol + pe_vol) - bar.get("_vol_at_open", 0))
            bar_to_finalize   = None
    if bar_to_finalize is not None:
        _straddle_finalize_bar(bar_to_finalize)
    _straddle_check_sl(combined)
    broadcast("straddle_update")


def _straddle_bar_start(now: datetime) -> str:
    """5-min bar start ISO string anchored to wall clock."""
    floored = now.replace(second=0, microsecond=0)
    floored = floored.replace(minute=(floored.minute // 5) * 5)
    return floored.isoformat()


def _straddle_morning_loop():
    """Once a trading day at >= 09:16 IST, run _straddle_setup_today() if not yet done."""
    while True:
        try:
            now = _now_ist()
            today = now.date().isoformat()
            with _straddle_lock:
                done = (_straddle_state.get("session_date") == today)
                active = bool(_straddle_config.get("active"))
            if (_is_trading_day(now.date())
                and active
                and not done
                and now.time() >= _STRADDLE_ATM_LOCK_AT
                and now.time() < dtime(15, 0)):
                _straddle_setup_today()
        except Exception as e:
            print(f"[straddle-morning] {e}")
        time.sleep(20)


def _straddle_squareoff_loop():
    """Hard square-off at squareoff_at (15:15 default)."""
    fired_for_date = None
    while True:
        try:
            now = _now_ist()
            today = now.date().isoformat()
            cfg_t = datetime.strptime(_straddle_config["squareoff_at"], "%H:%M").time()
            if (_is_trading_day(now.date())
                and fired_for_date != today
                and now.time() >= cfg_t):
                with _straddle_lock:
                    pos_exists = _straddle_state.get("position") is not None
                if pos_exists:
                    with _straddle_lock:
                        combined = _straddle_state.get("last_combined") or 0
                    _straddle_exit_position("squareoff_eod", combined)
                with _straddle_lock:
                    _straddle_state["lifecycle"] = "squared_off"
                fired_for_date = today
                broadcast("straddle_update")
        except Exception as e:
            print(f"[straddle-squareoff] {e}")
        time.sleep(15)


# Boot threads
threading.Thread(target=_straddle_morning_loop,   daemon=True).start()
threading.Thread(target=_straddle_tick_loop,      daemon=True).start()
threading.Thread(target=_straddle_squareoff_loop, daemon=True).start()


# ── Straddle routes ───────────────────────────────────────────────────────────
@app.route("/straddle", methods=["GET"])
def straddle_route():
    with _straddle_lock:
        s = dict(_straddle_state)
        # don't leak internal accumulators
        s.pop("_vwap_num", None); s.pop("_vwap_den", None)
    return jsonify({
        "ok":     True,
        "config": dict(_straddle_config),
        "state":  s,
    })

@app.route("/straddle/config", methods=["POST"])
def straddle_config_route():
    d = request.json or {}
    for k in ("lots", "max_reentries"):
        if k in d:
            try: _straddle_config[k] = int(d[k])
            except: pass
    for k in ("sl_pts", "skip_filter_pct"):
        if k in d:
            try: _straddle_config[k] = float(d[k])
            except: pass
    for k in ("entry_from", "entry_to", "squareoff_at", "underlying"):
        if k in d: _straddle_config[k] = str(d[k])
    if "paper_mode" in d:
        _straddle_config["paper_mode"] = bool(d["paper_mode"])
    if "active" in d:
        _straddle_config["active"] = bool(d["active"])
    broadcast("straddle_update")
    return jsonify({"ok": True, "config": dict(_straddle_config)})

@app.route("/straddle/start", methods=["POST"])
def straddle_start_route():
    _straddle_config["active"] = True
    threading.Thread(target=_straddle_setup_today, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/straddle/stop", methods=["POST"])
def straddle_stop_route():
    _straddle_config["active"] = False
    return jsonify({"ok": True})

@app.route("/straddle/exit-now", methods=["POST"])
def straddle_exit_now_route():
    with _straddle_lock:
        combined = _straddle_state.get("last_combined") or 0
        has_pos  = _straddle_state.get("position") is not None
    if not has_pos:
        return jsonify({"ok": False, "error": "no open position"}), 400
    threading.Thread(target=_straddle_exit_position,
                     args=("manual_exit", combined), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/straddle/skip-today", methods=["POST"])
def straddle_skip_today_route():
    with _straddle_lock:
        _straddle_state["skip_today"]  = True
        _straddle_state["skip_reason"] = "manual"
        _straddle_state["lifecycle"]   = "skipped"
    _straddle_log("manual_skip")
    return jsonify({"ok": True})


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


# ── Inter-Exchange Arbitrage (NSE vs BSE same-share, intraday) — Phase A ─────
#
# Reads top-N stocks from data/interarb_universe.json. At boot, resolves each
# tradingsymbol to its NSE + BSE instrument_token. Subscribes to ALL of them
# via KiteTicker (WebSocket, mode=full → 5-level depth pushed in real-time).
#
# Every second, walks the universe and computes, for each symbol:
#   sell_nse_buy_bse_edge = nse_best_bid - bse_best_ask  (sell on NSE, buy on BSE)
#   sell_bse_buy_nse_edge = bse_best_bid - nse_best_ask  (sell on BSE, buy on NSE)
#   fill_qty              = min(qty on both legs)        (the guaranteed top-of-book qty)
#   gross_pnl, costs, net_pnl, net_pct                   (Zerodha intraday MIS round-trip)
#
# Phase A is READ-ONLY: opportunities are surfaced in the UI; no orders are
# placed. The paper/live toggle ships disabled by default and is wired to the
# UI but does nothing until Phase B.

_INTERARB_UNIVERSE_PATH = os.path.join(CODE_DIR, "data", "interarb_universe.json")

_interarb_config = {
    "min_net_pct":     0.05,    # filter the UI table to rows where net_pct >= this
    "max_qty_per_row": 5000,    # safety cap on guaranteed-fill qty per opportunity
    "paper_mode":      True,    # Phase A: no effect (no orders). Phase B will respect this.
    "active":          False,   # master switch; Phase B execution will require true.
}

# Live top-of-book depth populated by the KiteTicker thread. Keyed by
# instrument_token (int). Each entry: bid, bid_qty, ask, ask_qty, ltp, ts.
_interarb_depth: dict = {}
_interarb_depth_lock = threading.Lock()

# Symbol → {nse_token, bse_token, lot/lot_size=1 for equity}. Populated once.
_interarb_symbols: dict = {}     # symbol → {nse_token, bse_token}
_interarb_tok2sym: dict = {}     # token → (symbol, exchange) — fast reverse lookup

# Live top-N opportunities snapshot (recomputed every second)
_interarb_opps: list = []
_interarb_last_compute_at: str = ""
_interarb_status: dict = {
    "ws_state":     "init",     # init / connecting / connected / disconnected / failed
    "ws_last_tick": None,
    "subscribed":   0,
    "resolved":     0,
    "skipped":      0,
    "boot_error":   None,
}


def _interarb_load_universe() -> list[str]:
    try:
        with open(_INTERARB_UNIVERSE_PATH, "r") as f:
            data = json.load(f)
        return list(data.get("symbols") or [])
    except Exception as e:
        print(f"[interarb-universe] {e}")
        return []


def _interarb_resolve_tokens(symbols: list[str]) -> None:
    """For each symbol, find its instrument_token on NSE and BSE. Skips
    symbols not listed on both. Called once at boot."""
    global _interarb_symbols, _interarb_tok2sym
    try:
        nse_inst = kite.instruments("NSE")
        bse_inst = kite.instruments("BSE")
    except Exception as e:
        _interarb_status["boot_error"] = f"instruments() failed: {e}"
        print(f"[interarb-resolve] {e}")
        return
    # Build lookup: tradingsymbol → token (equity only, segment 'EQ' on NSE/BSE)
    nse_lookup = {}
    for i in nse_inst:
        if i.get("instrument_type") == "EQ" and i.get("segment") in ("NSE",):
            nse_lookup[i["tradingsymbol"]] = i["instrument_token"]
    bse_lookup = {}
    for i in bse_inst:
        if i.get("instrument_type") == "EQ" and i.get("segment") in ("BSE",):
            bse_lookup[i["tradingsymbol"]] = i["instrument_token"]
    resolved = {}
    skipped  = []
    for sym in symbols:
        n = nse_lookup.get(sym)
        b = bse_lookup.get(sym)
        if n and b:
            resolved[sym] = {"nse_token": n, "bse_token": b}
        else:
            skipped.append((sym, bool(n), bool(b)))
    _interarb_symbols = resolved
    _interarb_tok2sym = {}
    for sym, t in resolved.items():
        _interarb_tok2sym[t["nse_token"]] = (sym, "NSE")
        _interarb_tok2sym[t["bse_token"]] = (sym, "BSE")
    _interarb_status["resolved"] = len(resolved)
    _interarb_status["skipped"]  = len(skipped)
    print(f"[interarb-resolve] resolved={len(resolved)} skipped={len(skipped)}")
    if skipped:
        # Show first few; helpful for debugging the universe file
        sample = ", ".join(s for s, _, _ in skipped[:5])
        print(f"[interarb-resolve] sample skipped: {sample}")


# ── Zerodha intraday MIS round-trip cost (₹) ─────────────────────────────────
def _interarb_intraday_cost(buy_value: float, sell_value: float, qty: int) -> dict:
    """Per Zerodha equity intraday MIS charges. Both legs are intraday so
    NO delivery STT (which is 0.1%). MIS STT is 0.025% on sell side only."""
    if buy_value <= 0 or sell_value <= 0 or qty <= 0:
        return {"total": 0.0, "stt": 0, "exch": 0, "sebi": 0, "stamp": 0,
                "ipft": 0, "gst": 0, "brokerage": 0}
    # STT 0.025% on sell-side only (intraday)
    stt = sell_value * 0.00025
    # Exchange transaction charges (NSE ~0.00297%, BSE ~0.00375%) per leg.
    # We pay roughly the average across both legs since one leg is on each.
    exch = (buy_value * 0.0000375) + (sell_value * 0.0000297)
    # SEBI fee: ₹10 per crore both sides
    sebi = (buy_value + sell_value) * 0.000001
    # Stamp duty: 0.003% on buy side, intraday equity
    stamp = buy_value * 0.00003
    # IPFT: NSE 0.0001%, BSE 0.0001% (charged on both sides on respective exchange)
    ipft  = (buy_value + sell_value) * 0.000001
    # Zerodha brokerage: ₹20/leg or 0.03% (lower) for intraday — 2 legs
    brokerage = min(20.0, buy_value  * 0.0003) + min(20.0, sell_value * 0.0003)
    # GST: 18% on (brokerage + exch + sebi + ipft)
    gst = (brokerage + exch + sebi + ipft) * 0.18
    total = stt + exch + sebi + stamp + ipft + gst + brokerage
    return {
        "stt":       round(stt, 2),
        "exch":      round(exch, 2),
        "sebi":      round(sebi, 2),
        "stamp":     round(stamp, 2),
        "ipft":      round(ipft, 2),
        "gst":       round(gst, 2),
        "brokerage": round(brokerage, 2),
        "total":     round(total, 2),
    }


# ── KiteTicker (WebSocket) — pushed depth ────────────────────────────────────
_interarb_ticker = None
_interarb_ws_thread = None

def _interarb_on_ticks(ws, ticks):
    """Each tick has keys: instrument_token, last_price, depth (in 'full' mode)."""
    now_iso = _now_ist().isoformat()
    with _interarb_depth_lock:
        for t in ticks:
            tok = t.get("instrument_token")
            if not tok:
                continue
            depth = t.get("depth") or {}
            buy_levels  = depth.get("buy")  or []
            sell_levels = depth.get("sell") or []
            best_bid     = float(buy_levels[0]["price"])    if buy_levels  else 0.0
            best_bid_qty = int(buy_levels[0]["quantity"])   if buy_levels  else 0
            best_ask     = float(sell_levels[0]["price"])   if sell_levels else 0.0
            best_ask_qty = int(sell_levels[0]["quantity"])  if sell_levels else 0
            _interarb_depth[tok] = {
                "bid":     best_bid,
                "bid_qty": best_bid_qty,
                "ask":     best_ask,
                "ask_qty": best_ask_qty,
                "ltp":     float(t.get("last_price") or 0),
                "ts":      now_iso,
            }
    _interarb_status["ws_last_tick"] = now_iso


def _interarb_on_connect(ws, response):
    """When the WebSocket connects, subscribe + set mode for all resolved tokens."""
    _interarb_status["ws_state"] = "connected"
    tokens = []
    for sym, t in _interarb_symbols.items():
        tokens.append(t["nse_token"])
        tokens.append(t["bse_token"])
    # Subscribe in chunks of 1000 (Kite's per-subscribe message cap)
    chunk = 800
    for i in range(0, len(tokens), chunk):
        batch = tokens[i:i + chunk]
        ws.subscribe(batch)
        ws.set_mode(ws.MODE_FULL, batch)
    _interarb_status["subscribed"] = len(tokens)
    print(f"[interarb-ws] connected, subscribed {len(tokens)} tokens in FULL mode")


def _interarb_on_close(ws, code, reason):
    _interarb_status["ws_state"] = "disconnected"
    print(f"[interarb-ws] closed code={code} reason={reason}")


def _interarb_on_error(ws, code, reason):
    _interarb_status["ws_state"] = "error"
    print(f"[interarb-ws] error code={code} reason={reason}")


def _interarb_on_reconnect(ws, attempts):
    _interarb_status["ws_state"] = "reconnecting"
    print(f"[interarb-ws] reconnecting, attempt {attempts}")


def _interarb_start_ws():
    """Build + start a KiteTicker. The kiteconnect library handles its own
    auto-reconnect (built-in exponential backoff) so we just spin it up."""
    global _interarb_ticker
    try:
        from kiteconnect import KiteTicker
    except Exception as e:
        _interarb_status["ws_state"] = "failed"
        _interarb_status["boot_error"] = f"KiteTicker import: {e}"
        return
    if not API_KEY or not ACCESS_TOKEN:
        _interarb_status["ws_state"] = "failed"
        _interarb_status["boot_error"] = "missing API_KEY / ACCESS_TOKEN"
        return
    if not _interarb_symbols:
        _interarb_status["ws_state"] = "failed"
        _interarb_status["boot_error"] = "no resolved symbols"
        return
    _interarb_status["ws_state"] = "connecting"
    kt = KiteTicker(API_KEY, ACCESS_TOKEN)
    kt.on_ticks     = _interarb_on_ticks
    kt.on_connect   = _interarb_on_connect
    kt.on_close     = _interarb_on_close
    kt.on_error     = _interarb_on_error
    kt.on_reconnect = _interarb_on_reconnect
    # Auto-reconnect: kiteconnect KiteTicker uses these attributes (not a
    # method) — set on the instance before connect().
    kt.reconnect             = True
    kt.reconnect_max_tries   = 300
    kt.reconnect_max_delay   = 60
    _interarb_ticker = kt
    kt.connect(threaded=True)


# ── Opportunity scanner: recompute every second ──────────────────────────────
def _interarb_compute_opps():
    """For every resolved symbol, compute both directions of arb. Return the
    top opportunities sorted by net_pct descending."""
    cfg = _interarb_config
    cap_qty = int(cfg.get("max_qty_per_row") or 5000)
    min_net_pct = float(cfg.get("min_net_pct") or 0.0)
    rows = []
    with _interarb_depth_lock:
        depth_snapshot = dict(_interarb_depth)
    for sym, toks in _interarb_symbols.items():
        nse = depth_snapshot.get(toks["nse_token"]) or {}
        bse = depth_snapshot.get(toks["bse_token"]) or {}
        nse_bid     = nse.get("bid")     or 0
        nse_bid_qty = nse.get("bid_qty") or 0
        nse_ask     = nse.get("ask")     or 0
        nse_ask_qty = nse.get("ask_qty") or 0
        bse_bid     = bse.get("bid")     or 0
        bse_bid_qty = bse.get("bid_qty") or 0
        bse_ask     = bse.get("ask")     or 0
        bse_ask_qty = bse.get("ask_qty") or 0
        nse_ltp     = nse.get("ltp")     or 0
        bse_ltp     = bse.get("ltp")     or 0

        # Direction 1: sell NSE, buy BSE → profit if nse_bid > bse_ask
        for direction, sell_px, sell_qty, buy_px, buy_qty, sell_exch, buy_exch in (
            ("sell_nse_buy_bse", nse_bid, nse_bid_qty, bse_ask, bse_ask_qty, "NSE", "BSE"),
            ("sell_bse_buy_nse", bse_bid, bse_bid_qty, nse_ask, nse_ask_qty, "BSE", "NSE"),
        ):
            if sell_px <= 0 or buy_px <= 0 or sell_qty <= 0 or buy_qty <= 0:
                continue
            edge = sell_px - buy_px
            if edge <= 0:
                continue
            fill_qty = min(sell_qty, buy_qty, cap_qty)
            if fill_qty <= 0:
                continue
            buy_value  = buy_px  * fill_qty
            sell_value = sell_px * fill_qty
            cost = _interarb_intraday_cost(buy_value, sell_value, fill_qty)
            gross = edge * fill_qty
            net   = gross - cost["total"]
            notional = (buy_value + sell_value) / 2.0
            net_pct  = (net / notional * 100) if notional > 0 else 0.0
            if net_pct < min_net_pct:
                continue
            rows.append({
                "symbol":       sym,
                "direction":    direction,
                "sell_exch":    sell_exch,
                "buy_exch":     buy_exch,
                "sell_price":   round(sell_px, 2),
                "buy_price":    round(buy_px, 2),
                "sell_qty_top": sell_qty,
                "buy_qty_top":  buy_qty,
                "fill_qty":     fill_qty,
                "edge_pts":     round(edge, 2),
                "gross_pnl":    round(gross, 2),
                "cost_total":   cost["total"],
                "net_pnl":      round(net, 2),
                "net_pct":      round(net_pct, 4),
                "nse_bid":      nse_bid,   "nse_ask": nse_ask,
                "nse_bid_qty":  nse_bid_qty, "nse_ask_qty": nse_ask_qty,
                "bse_bid":      bse_bid,   "bse_ask": bse_ask,
                "bse_bid_qty":  bse_bid_qty, "bse_ask_qty": bse_ask_qty,
                "nse_ltp":      nse_ltp,   "bse_ltp": bse_ltp,
            })
    rows.sort(key=lambda r: -r["net_pct"])
    return rows


def _interarb_compute_loop():
    """Recompute opportunities every second."""
    global _interarb_opps, _interarb_last_compute_at
    while True:
        try:
            opps = _interarb_compute_opps()
            _interarb_opps = opps
            _interarb_last_compute_at = _now_ist().isoformat()
        except Exception as e:
            print(f"[interarb-compute] {e}")
        time.sleep(1)


# ── Boot: resolve tokens + start WS + start compute loop ─────────────────────
def _interarb_boot():
    syms = _interarb_load_universe()
    if not syms:
        _interarb_status["boot_error"] = "universe file missing or empty"
        return
    _interarb_resolve_tokens(syms)
    if not _interarb_symbols:
        _interarb_status["boot_error"] = "no symbols resolved (token lookup failed?)"
        return
    _interarb_start_ws()
    threading.Thread(target=_interarb_compute_loop, daemon=True).start()

# Run boot in a thread so it doesn't block startup if instruments() is slow
threading.Thread(target=_interarb_boot, daemon=True).start()


# ── Routes ──────────────────────────────────────────────────────────────────
@app.route("/interarb", methods=["GET"])
def interarb_route():
    """Returns current top opportunities + config + ws status."""
    return jsonify({
        "ok":             True,
        "config":         dict(_interarb_config),
        "status":         dict(_interarb_status),
        "opportunities":  list(_interarb_opps),
        "last_compute_at": _interarb_last_compute_at,
        "universe_size":  len(_interarb_symbols),
    })


@app.route("/interarb/config", methods=["POST"])
def interarb_config_route():
    d = request.json or {}
    for k in ("min_net_pct", "max_qty_per_row"):
        if k in d:
            try: _interarb_config[k] = float(d[k]) if k == "min_net_pct" else int(d[k])
            except Exception: pass
    if "paper_mode" in d: _interarb_config["paper_mode"] = bool(d["paper_mode"])
    if "active" in d:     _interarb_config["active"]     = bool(d["active"])
    return jsonify({"ok": True, "config": dict(_interarb_config)})


# ── Admin / onboarding ────────────────────────────────────────────────────────
# Lets the primary user (ADMIN_ENABLED=true in their .env) generate invite
# links, list active users, and remove users. Onboarding is handled by a
# public /onboard?token=X page that takes the invitee's Kite creds + chosen
# password, validates them, then runs sudo /opt/kite/bin/add_user_ui.sh to
# provision a fresh isolated instance.
#
# Invite tokens are stored in DATA_DIR/data/invites.json — small JSON file.

_INVITES_PATH = os.path.join(DATA_DIR, "data", "invites.json")
_invites_lock = threading.Lock()

def _load_invites() -> dict:
    if not os.path.exists(_INVITES_PATH):
        return {}
    try:
        with open(_INVITES_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_invites(invites: dict):
    os.makedirs(os.path.dirname(_INVITES_PATH), exist_ok=True)
    tmp = _INVITES_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(invites, f, indent=2, default=str)
    os.replace(tmp, _INVITES_PATH)

def _scan_provisioned_users() -> list[dict]:
    """Scan /etc/systemd/system/kite-monitor-*.service to find provisioned
    user instances. Each returned row has slug + flask_port + public_port +
    is_running."""
    out = []
    sysd_dir = "/etc/systemd/system"
    if not os.path.isdir(sysd_dir):
        return out
    for fname in sorted(os.listdir(sysd_dir)):
        m = None
        if fname.startswith("kite-monitor-") and fname.endswith(".service"):
            slug = fname[len("kite-monitor-"):-len(".service")]
        else:
            continue
        try:
            with open(os.path.join(sysd_dir, fname), "r") as f:
                content = f.read()
        except OSError:
            continue
        # Parse PORT and DATA_DIR from the unit
        flask_port = None
        data_dir   = None
        for line in content.splitlines():
            line = line.strip()
            if line.startswith('Environment="PORT='):
                try:
                    flask_port = int(line.split("PORT=")[1].rstrip('"'))
                except (ValueError, IndexError):
                    pass
            elif line.startswith('Environment="DATA_DIR='):
                data_dir = line.split("DATA_DIR=")[1].rstrip('"')
        # Look up public port from the matching nginx file
        public_port = None
        nginx_file = f"/etc/nginx/sites-enabled/kite-{slug}"
        if os.path.exists(nginx_file):
            try:
                with open(nginx_file, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("listen "):
                            try:
                                public_port = int(line.split()[1].rstrip(";"))
                                break
                            except (ValueError, IndexError):
                                pass
            except OSError:
                pass
        # Service status
        try:
            r = _subprocess.run(
                ["systemctl", "is-active", f"kite-monitor-{slug}"],
                capture_output=True, text=True, timeout=2,
            )
            is_running = (r.stdout.strip() == "active")
        except Exception:
            is_running = False
        out.append({
            "slug":         slug,
            "flask_port":   flask_port,
            "public_port":  public_port,
            "data_dir":     data_dir,
            "is_running":   is_running,
        })
    return out


@app.route("/admin/state", methods=["GET"])
def admin_state_route():
    """Returns admin-panel state. 403 if this instance isn't an admin instance."""
    if not ADMIN_ENABLED:
        return jsonify({"ok": False, "error": "admin not enabled on this instance"}), 403
    with _invites_lock:
        invites_raw = _load_invites()
    # Filter expired
    now = _now_ist()
    invites = []
    for tok, rec in invites_raw.items():
        try:
            expires = datetime.fromisoformat(rec["expires_at"])
        except Exception:
            continue
        if expires < now:
            continue
        if rec.get("used"):
            continue
        invites.append({
            "token":      tok,
            "slug_hint":  rec.get("slug_hint", ""),
            "created_at": rec.get("created_at"),
            "expires_at": rec.get("expires_at"),
        })
    return jsonify({
        "ok": True,
        "admin_enabled": True,
        "max_users":     ADMIN_MAX_USERS,
        "users":         _scan_provisioned_users(),
        "invites":       invites,
    })


@app.route("/admin/invite", methods=["POST"])
def admin_invite_route():
    """Create a one-time invite. Body: {slug_hint?: str, ttl_hours?: int}."""
    if not ADMIN_ENABLED:
        return jsonify({"ok": False, "error": "admin not enabled"}), 403
    # Cap total users
    if len(_scan_provisioned_users()) >= ADMIN_MAX_USERS:
        return jsonify({"ok": False, "error": f"max users ({ADMIN_MAX_USERS}) reached"}), 400
    d = request.json or {}
    slug_hint = (d.get("slug_hint") or "").strip().lower()
    ttl_hours = int(d.get("ttl_hours") or 24)
    if slug_hint and not slug_hint.replace("-", "").isalnum():
        return jsonify({"ok": False, "error": "slug_hint must be lowercase a-z, 0-9, dash"}), 400
    token = _secrets.token_urlsafe(24)
    now = _now_ist()
    rec = {
        "slug_hint":  slug_hint,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=ttl_hours)).isoformat(),
        "used":       False,
    }
    with _invites_lock:
        invites = _load_invites()
        invites[token] = rec
        _save_invites(invites)
    # Build the invite URL using the request host
    base = request.host_url.rstrip("/")
    url = f"{base}/onboard?token={token}"
    return jsonify({"ok": True, "token": token, "url": url, "expires_at": rec["expires_at"]})


@app.route("/admin/invite/<token>", methods=["DELETE"])
def admin_invite_revoke_route(token: str):
    """Revoke an outstanding invite."""
    if not ADMIN_ENABLED:
        return jsonify({"ok": False, "error": "admin not enabled"}), 403
    with _invites_lock:
        invites = _load_invites()
        if token in invites:
            invites.pop(token, None)
            _save_invites(invites)
    return jsonify({"ok": True})


@app.route("/admin/users/<slug>/remove", methods=["POST"])
def admin_remove_user_route(slug: str):
    """Remove a provisioned user. Calls sudo bin/remove_user.sh; archives data."""
    if not ADMIN_ENABLED:
        return jsonify({"ok": False, "error": "admin not enabled"}), 403
    if not slug.replace("-", "").isalnum():
        return jsonify({"ok": False, "error": "bad slug"}), 400
    # remove_user.sh has an interactive confirm. We feed the slug to stdin.
    try:
        r = _subprocess.run(
            ["sudo", "/opt/kite/bin/remove_user.sh", slug],
            input=f"{slug}\n", capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        return jsonify({"ok": False, "error": f"subprocess failed: {e}"}), 500
    if r.returncode != 0:
        return jsonify({"ok": False, "error": r.stderr or r.stdout}), 500
    return jsonify({"ok": True, "output": r.stdout})


# ── Public onboarding (invite-token gated) ─────────────────────────────────
@app.route("/onboard", methods=["GET"])
def onboard_page():
    """Render the public onboarding form. Validates the token before showing
    the form so visitors with a bad/expired/used token see a clear error."""
    token = (request.args.get("token") or "").strip()
    if not token:
        return render_template("onboard.html", error="No invite token in URL.", token="")
    with _invites_lock:
        invites = _load_invites()
        rec = invites.get(token)
    err = None
    if not rec:
        err = "Invite link is invalid."
    elif rec.get("used"):
        err = "This invite has already been used."
    else:
        try:
            expires = datetime.fromisoformat(rec["expires_at"])
            if expires < _now_ist():
                err = "This invite has expired."
        except Exception:
            err = "Invite is malformed."
    return render_template(
        "onboard.html",
        error=err,
        token=token,
        slug_hint=(rec or {}).get("slug_hint", "") if not err else "",
    )


@app.route("/onboard/submit", methods=["POST"])
def onboard_submit_route():
    """Provision a new user from form values + invite token."""
    d = request.json or request.form.to_dict() or {}
    token = (d.get("token") or "").strip()
    if not token:
        return jsonify({"ok": False, "error": "missing token"}), 400

    # ── Validate token under lock so we can't double-consume
    with _invites_lock:
        invites = _load_invites()
        rec = invites.get(token)
        if not rec:
            return jsonify({"ok": False, "error": "invalid token"}), 400
        if rec.get("used"):
            return jsonify({"ok": False, "error": "token already used"}), 400
        try:
            if datetime.fromisoformat(rec["expires_at"]) < _now_ist():
                return jsonify({"ok": False, "error": "token expired"}), 400
        except Exception:
            return jsonify({"ok": False, "error": "malformed token"}), 400

    slug          = (d.get("slug") or "").strip().lower()
    dash_password = d.get("dash_password") or ""
    api_key       = (d.get("api_key") or "").strip()
    api_secret    = (d.get("api_secret") or "").strip()
    access_token  = (d.get("access_token") or "").strip()
    kite_user_id  = (d.get("kite_user_id") or "").strip()
    kite_password = d.get("kite_password") or ""
    totp_secret   = (d.get("totp_secret") or "").strip()
    tg_token      = (d.get("telegram_token") or "").strip()
    tg_chat       = (d.get("telegram_chat_id") or "").strip()

    # ── Basic shape checks
    if not slug or not slug.replace("-", "").replace("_", "").isalnum():
        return jsonify({"ok": False, "error": "slug: lowercase letters/digits/dash only"}), 400
    if slug in ("omkar", "admin", "root"):
        return jsonify({"ok": False, "error": f"slug '{slug}' is reserved"}), 400
    if len(dash_password) < 6:
        return jsonify({"ok": False, "error": "dashboard password must be at least 6 chars"}), 400
    if not (api_key and api_secret and access_token):
        return jsonify({"ok": False, "error": "Kite API key, secret, and access token are required"}), 400

    # ── Validate creds against Kite before provisioning
    try:
        k = KiteConnect(api_key=api_key)
        k.set_access_token(access_token)
        profile = k.profile()
    except Exception as e:
        return jsonify({"ok": False, "error": f"Kite credential check failed: {e}"}), 400

    # ── Provision via sudo script
    env = {
        "PATH":             "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "SLUG":             slug,
        "DASH_PASSWORD":    dash_password,
        "API_KEY":          api_key,
        "API_SECRET":       api_secret,
        "ACCESS_TOKEN":     access_token,
        "KITE_USER_ID":     kite_user_id or profile.get("user_id") or "",
        "KITE_PASSWORD":    kite_password or "",
        "KITE_TOTP_SECRET": totp_secret or "",
        "TELEGRAM_TOKEN":   tg_token or "",
        "TELEGRAM_CHAT_ID": tg_chat or "",
    }
    try:
        r = _subprocess.run(
            ["sudo", "-E", "/opt/kite/bin/add_user_ui.sh"],
            env=env, capture_output=True, text=True, timeout=60,
        )
    except Exception as e:
        return jsonify({"ok": False, "error": f"subprocess failed: {e}"}), 500

    # add_user_ui.sh prints structured JSON either way
    try:
        result = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        result = {"ok": False, "error": (r.stderr or r.stdout or "no output").strip()}

    if not result.get("ok"):
        return jsonify(result), 500

    # ── Mark token used
    with _invites_lock:
        invites = _load_invites()
        if token in invites:
            invites[token]["used"]      = True
            invites[token]["used_at"]   = _now_ist().isoformat()
            invites[token]["used_slug"] = slug
            _save_invites(invites)

    # ── Build the user's URL from the request host but swap the port
    public_port = result["public_port"]
    host = request.host.split(":")[0]
    scheme = "https" if request.headers.get("X-Forwarded-Proto") == "https" else "http"
    dashboard_url = f"{scheme}://{host}:{public_port}/"

    return jsonify({
        "ok":            True,
        "slug":          slug,
        "public_port":   public_port,
        "dashboard_url": dashboard_url,
        "login_user":    slug,
        "kite_user_id":  profile.get("user_id"),
    })


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
