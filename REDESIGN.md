# Yeske Trade — Redesign & Cleanup

> **Yeske Trade** · _Algo Trading Platform_ — a clean, fast, mobile-first
> trading dashboard for Zerodha Kite. Rebranded and stripped down from the
> previous "Sniper Eagle" build.

This document is the engineering record for the refactor: what was removed,
the new architecture, and how to ship it safely.

---

## 1. What changed at a glance

| Area | Before | After |
|---|---|---|
| Brand | "Sniper Eagle", Comic Sans, neon | **Yeske Trade**, system/Inter stack, Kite/Linear palette |
| Backend | `app.py` 7,051 lines | `app.py` **4,969 lines** (−2,082) |
| Frontend | one 5,309-line `index.html` (HTML+CSS+JS inline) | `index.html` 897 · `static/css/app.css` 961 · `static/js/app.js` 2,515 |
| Modules | 10 strategy/scanner modules | **5 kept**, 5 removed |
| Navigation | 10-item vertical list, desktop-only | mobile-first: left rail (desktop) → bottom tab-bar + top app-bar (mobile) |
| Theme | dark + light (Comic Sans) | dark + light, refined tokens, no flash, `prefers-reduced-motion` aware |

### Kept (core trading)
- **Custom Strategies** — manual MTM monitor: instrument selection, P&L targets, trailing stop, auto-entry, start/stop/exit, live chart, logs, session history.
- **Scheduled** — daily auto-entry strangle.
- **Arb** — future ↔ synthetic-future basis monitor + execution.
- **Owl** — OTM monthly strangle (intraday).
- **Calendar Spread** — monitor (read-only).
- Plus: Auth (Kite OAuth + TOTP refresh), Telegram bot, Stats, Export, Settings.

### Removed completely
Auctions · Short Straddle · NSE↔BSE Inter-Exchange Arb (interarb) · Commodity Lot Arb (commarb) · the **Active** dashboard tab & its top banner.

---

## 2. New folder structure

```
algotrading/
├── app.py                  # Flask backend (routes, monitors, schedulers)
├── auth.py                 # one-shot Kite login helper (CLI)
├── monitor.py              # standalone two-leg P&L watchdog (CLI, optional)
├── debug_positions.py      # dev helper: print Kite tradingsymbols
├── requirements.txt        # kiteconnect, python-dotenv, flask, pyotp, requests
├── strategies.json         # persisted strategy slots (s1, s2, …)
├── data/
│   └── nse_holidays_2026.json   # trading-day calendar (kept — used by expiry logic)
├── static/
│   ├── favicon.png         # ⚠ still the old logo — replace with Yeske Trade mark
│   ├── css/
│   │   └── app.css         # design system: Part 1 components · Part 2 redesign override
│   └── js/
│       └── app.js          # all client logic (vanilla, no build step)
├── templates/
│   └── index.html          # lean shell: head + nav + view sections + <script>
└── deploy/                 # systemd unit, nginx conf, install.sh, env template
```

**Removed data files:** `data/interarb_universe.json`, `data/commarb_pairs.json`.

---

## 3. Component / frontend architecture

No framework, no bundler — intentionally. The app loads instantly and has zero
build step, which matches the "fast loading / execution speed" goal.

### Layers
1. **`templates/index.html`** — the shell. Contains:
   - `<head>`: rebranded metadata, synchronous theme-init (prevents flash), Chart.js CDN, one `<link>` to `app.css`.
   - `.appbar` — mobile top app-bar (brand + quick actions). Hidden ≥960px.
   - `.sidebar` — desktop left rail: brand, primary nav (`.view-toggle`), strategy-slot tabs (`#tab-strip`), stats, action buttons.
   - `.main` — the view container. Each view is a `<div id="view-*">` toggled by `switchView()`.
   - Modals: auth (token refresh) and export.

2. **`static/css/app.css`** — single stylesheet, two parts:
   - **Part 1 — components:** the proven component rules (hero card, panels, tables, switches, modals, session list…).
   - **Part 2 — redesign override:** design tokens (colour + typography), modern surface treatment, and the **responsive navigation system**. Because every component reads `var(--token)`, redefining tokens reskins the whole app.

3. **`static/js/app.js`** — classic script sharing one global scope (so inline
   `onclick=` handlers resolve). Organised by section banners:
   `Theme · SSE stream · Tab title · Tab strip · View toggle · Owl · Settings ·
   Telegram · Arb · Scheduled · Instruments · Positions · Render · Sessions`.

### Navigation system (the redesign's core)
- **Desktop (≥960px):** fixed left rail; 5 vertical nav items with active accent bar.
- **Mobile/tablet (<960px):** `.sidebar` becomes `display:contents` so its
  children reflow individually:
  - primary nav → **fixed bottom tab-bar** (thumb-friendly, 5 columns, icon over label, `safe-area-inset` aware);
  - strategy-slot tabs → **sticky sub-bar** under the app-bar;
  - brand + actions → the **top app-bar**;
  - stats hidden (still on desktop).

### Design tokens (single source of truth)
`--bg --surface --surface-2 --border --text --muted --dim` · semantic
`--green/--red/--orange/--blue/--accent` (+ `-hi` / `-glow`) · typography
`--sans` (system/Inter) / `--mono` (numbers & tickers only) · scale
`--radius* --shadow* --rail-w --appbar-h --bottomnav-h --ease`. Light theme
overrides the same names under `[data-theme="light"]`.

---

## 4. Backend cleanup — what was done

`app.py` deletions (surgical, verified `py_compile` + Flask test-client 200):

| Removed module | Routes | Functions/threads | Lines |
|---|---|---|---|
| Auctions | `/auctions*` (6) | morning-fetch / book / snipe / buyback loops | ~720 |
| Short Straddle | `/straddle*` (6) | tick / morning / squareoff loops, entry/exit | ~640 |
| Inter-Exchange (NSE↔BSE) | `/interarb*` (2) | KiteTicker WS, opportunity scanner | ~360 |
| Commodity Lot Arb | `/commarb*` (2) | pair resolver, scanner | ~330 |
| Active aggregator refs | — | trimmed `/active` `_mod()` calls | ~30 |

- The `/active` route is **kept but slimmed** (now only Strategies + Owl +
  Calspread) — it still backs nothing UI-facing after the banner removal, but is
  harmless and cheap. It can be deleted later if desired.
- `KiteTicker` was only imported inside the interarb block → gone with it.
- Dependencies unchanged: `pyotp` + `requests` are still used by the TOTP token
  auto-refresh path, so `requirements.txt` stays as-is.

---

## 5. Data / "database" cleanup

This app has **no SQL database** — state is JSON + per-day CSVs on disk. The
"DB cleanup" is therefore a data-file cleanup:

**Deleted from the repo:** `data/interarb_universe.json`, `data/commarb_pairs.json`.

**Safe to delete from each running instance's `DATA_DIR/data/` (runtime artefacts):**
```
auctions_state_*.json      straddle_*.csv
auctions_*.csv             interarb_config.json
straddle config in module-config store   commarb_config.json
```
These are regenerated-or-orphaned; removing them frees space and removes stale
state. **Do not delete:** `strategies.json`, `nse_holidays_*.json`,
`telegram_config.json`, `templates.json`, per-strategy `*_*.csv`, and the kept
modules' state (`owl_*.json`, `calspread_config.json`, `arb_*.csv`).

---

## 6. Responsive design

| Breakpoint | Layout |
|---|---|
| ≥1440px | wider rail, extra side padding, centred content (max 1080px) |
| 960–1439px | left rail + fluid main |
| 421–959px | top app-bar + **bottom tab-bar**, full-width content |
| ≤420px | tighter padding, smaller hero number, compact nav labels |

Verified targets: mobile, tablet, laptop, desktop. Uses `100dvh`,
`env(safe-area-inset-*)` for notched phones, and `prefers-reduced-motion`.

---

## 7. Migration checklist

- [x] Backend: remove Auctions / Straddle / InterArb / ComArb routes, loops, threads, state.
- [x] Backend: clean `/active` aggregator references to removed modules.
- [x] Backend: delete unused data files; confirm deps still used.
- [x] Backend: rebrand strings (signed-out page title + font).
- [x] Backend: `py_compile` clean; Flask test-client `GET /` → 200.
- [x] Frontend: extract CSS/JS to `static/`; rebuild `index.html` shell.
- [x] Frontend: new design system + mobile-first responsive nav; dark/light.
- [x] Frontend: remove deleted views, nav items, JS functions, Active banner.
- [x] Frontend: rebrand to **Yeske Trade** / _Algo Trading Platform_.
- [x] Frontend: `node --check` clean; static assets serve 200.
- [ ] **Replace `static/favicon.png`** with a Yeske Trade logo (wordmark is done; image asset is still the old mark).
- [ ] Hard-refresh / bust cache after deploy — asset URLs are versioned (`?v=`), bump on each change.
- [ ] Smoke test with a **real Kite token**: login → load positions → set targets → start/stop a custom strategy → check live MTM + chart + SSE.
- [ ] Verify Owl, Arb, Scheduled, Calendar Spread views render and their endpoints respond.
- [ ] On a phone: confirm bottom nav, sticky strategy tabs, and app-bar actions.
- [ ] (Optional) delete stale runtime data files listed in §5 on each instance.
- [ ] (Optional) remove the now-unused `/active` route if you don't want it.

---

## 8. Rollback

The pre-refactor code is the `First Commit` in git. To roll back:
`git checkout <first-commit> -- app.py templates/index.html` (and restore the
two deleted `data/*.json` files). The new `static/` tree can be removed.
