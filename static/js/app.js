/* Yeske Trade — application logic (vanilla JS, no build step) */

  // ── Daily token refresh (modal) ──────────────────────────────────────────────
  async function openAuthModal() {
    const m = document.getElementById('auth-modal');
    const link = document.getElementById('auth-login-link');
    const status = document.getElementById('auth-status');
    document.getElementById('auth-redirect-url').value = '';
    status.textContent = ''; status.style.color = 'var(--dim)';
    link.href = '#'; link.textContent = '↗ Loading…';
    m.classList.add('show');
    try {
      const r = await fetch('/auth/login-url');
      const d = await r.json();
      if (d.ok) {
        link.href = d.login_url; link.textContent = '↗ Open Kite Login';
        if (d.last_refresh) status.textContent = `Last refreshed: ${d.last_refresh}`;
      } else {
        status.textContent = 'Error: ' + (d.error || 'unknown'); status.style.color = 'var(--red)';
      }
    } catch (e) {
      status.textContent = 'Network error: ' + e.message; status.style.color = 'var(--red)';
    }
  }
  function closeAuthModal() {
    document.getElementById('auth-modal').classList.remove('show');
  }
  async function submitToken() {
    const url = document.getElementById('auth-redirect-url').value.trim();
    const status = document.getElementById('auth-status');
    const btn = document.getElementById('btn-auth-submit');
    if (!url) {
      status.textContent = 'Paste the redirect URL first.'; status.style.color = 'var(--orange)'; return;
    }
    btn.textContent = 'Submitting…'; btn.disabled = true;
    status.textContent = 'Exchanging request_token with Kite…'; status.style.color = 'var(--dim)';
    try {
      const r = await fetch('/auth/submit-token', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({redirect_url: url}),
      });
      const d = await r.json();
      if (d.ok) {
        status.innerHTML = `<span style="color:var(--green-hi)">✓ Token saved.</span> User: <span style="color:var(--text)">${d.user}</span> · Token: <span style="color:var(--text)">${d.token_preview}</span>`;
        btn.textContent = 'Done ✓'; btn.style.borderColor = 'var(--green)';
        setTimeout(() => { closeAuthModal(); btn.textContent = 'Submit & Save'; btn.disabled = false; }, 1500);
      } else {
        status.textContent = '✗ ' + (d.error || 'Failed'); status.style.color = 'var(--red)';
        btn.textContent = 'Submit & Save'; btn.disabled = false;
      }
    } catch (e) {
      status.textContent = 'Network error: ' + e.message; status.style.color = 'var(--red)';
      btn.textContent = 'Submit & Save'; btn.disabled = false;
    }
  }
  // ESC closes any open modal
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { closeAuthModal(); closeExportModal(); }
  });

  // ── Theme toggle ─────────────────────────────────────────────────────────────
  function updateThemeUi(theme) {
    const icon  = document.getElementById('theme-icon');
    const label = document.getElementById('theme-label');
    if (!icon || !label) return;
    if (theme === 'light') { icon.textContent = '☾'; label.textContent = 'Dark mode'; }
    else                   { icon.textContent = '☀'; label.textContent = 'Light mode'; }
  }
  function signOut() {
    const msg = "Sign out and clear basic-auth credentials?\n\n" +
                "Important: after the signed-out page loads, you must " +
                "CLOSE ALL TABS of this site (or use a fresh incognito window) " +
                "to fully clear the cached credentials. HTTP Basic Auth doesn't " +
                "have a programmatic logout — this is a browser limitation.";
    if (!confirm(msg)) return;
    // Navigate to /logout which returns 401 with a new realm so the browser
    // stops auto-resending the cached creds for the original realm.
    window.location.href = '/logout';
  }
  function toggleTheme() {
    const cur = document.documentElement.getAttribute('data-theme') || 'dark';
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('theme', next); } catch(e) {}
    updateThemeUi(next);
    // Rebuild chart so its axis/grid colors pick up new theme
    if (typeof chart !== 'undefined' && chart && typeof buildChart === 'function') {
      buildChart(chartSyms || []);
    }
  }
  document.addEventListener('DOMContentLoaded', () => {
    updateThemeUi(document.documentElement.getAttribute('data-theme') || 'dark');
  });

  // ── Constants ────────────────────────────────────────────────────────────────
  const POS_COLORS = ['#58a6ff','#bc8cff','#f78166','#3fb950','#e3b341','#79c0ff'];

  // ── State ────────────────────────────────────────────────────────────────────
  let currentTab        = null;
  let lastData          = null;
  let running           = false;
  let monitorStartTime  = null;
  let alertPlayed       = false;
  let availablePositions = [];           // from /kite-positions
  const selectedSets    = {};            // {sid: Set<"SYM:EXCH">}
  let selectedInitialized = false;

  // ── Chart state ───────────────────────────────────────────────────────────
  const chartDataMap = {};               // {sid: {labels,combined,pos,syms,loaded}}
  let chart     = null;
  let chartSyms = [];
  let prevPosCount = -1;

  function getChartData(sid) {
    if (!chartDataMap[sid])
      chartDataMap[sid] = {labels:[], combined:[], pos:[], syms:[], loaded:false};
    return chartDataMap[sid];
  }

  // ── Sound ────────────────────────────────────────────────────────────────
  function playAlert() {
    try {
      const ctx = new (window.AudioContext||window.webkitAudioContext)();
      [0,.25,.5].forEach(d => {
        const o=ctx.createOscillator(), g=ctx.createGain();
        o.connect(g); g.connect(ctx.destination);
        o.frequency.value=880; o.type='sine';
        g.gain.setValueAtTime(0,ctx.currentTime+d);
        g.gain.linearRampToValueAtTime(.45,ctx.currentTime+d+.05);
        g.gain.exponentialRampToValueAtTime(.001,ctx.currentTime+d+.3);
        o.start(ctx.currentTime+d); o.stop(ctx.currentTime+d+.35);
      });
    } catch(_) {}
  }
  function playWarning() {
    try {
      const ctx=new (window.AudioContext||window.webkitAudioContext)();
      const o=ctx.createOscillator(), g=ctx.createGain();
      o.connect(g); g.connect(ctx.destination);
      o.frequency.value=520; o.type='sine';
      g.gain.setValueAtTime(.3,ctx.currentTime);
      g.gain.exponentialRampToValueAtTime(.001,ctx.currentTime+.6);
      o.start(); o.stop(ctx.currentTime+.6);
    } catch(_) {}
  }

  // ── SSE ──────────────────────────────────────────────────────────────────
  function setConn(ok) {
    const dot=document.getElementById('conn-dot'), txt=document.getElementById('conn-text');
    dot.className='conn-dot '+(ok?'ok':'bad');
    txt.textContent=ok?'LIVE':'RECONNECTING';
    document.getElementById('conn-indicator').title=ok?'Stream connected':'Reconnecting...';
  }
  function connectSSE() {
    setConn(false);
    const es=new EventSource('/stream');
    es.onopen=()=>setConn(true);
    es.onmessage=e=>{
      setConn(true);
      const d=JSON.parse(e.data);
      if (d.heartbeat) return;
      render(d);
      _updateTabTitle(d);
    };
    es.onerror=()=>{ setConn(false); es.close(); setTimeout(connectSSE,3000); };
  }

  // ── Browser tab title: live MTM, context-matched to what's on screen ─────
  // When the user is looking at a specific strategy (Custom/Scheduled tab
  // with a strategy in focus), the title shows THAT strategy's MTM so the
  // tab number matches the hero card. For other views (Active/Owl/Straddle/
  // etc.), the title falls back to the sum across all running strategies.
  // Nothing running → title resets to the brand.
  const _BASE_TITLE = 'Yeske Trade';
  function _updateTabTitle(d) {
    const strats = (d && d.strategies) || {};
    let val = null;     // the number we'll show in the title
    let suffix = '';    // optional context tag like ' · all'

    // Helper: find the running scheduled strategy, if any.
    const findRunningScheduled = () => {
      for (const sid in strats) {
        if (strats[sid] && strats[sid].running && strats[sid].type === 'scheduled') return sid;
      }
      return null;
    };

    if (currentView === 'strategies' && currentTab && strats[currentTab] && strats[currentTab].running) {
      // User is focused on a specific Custom-tab strategy.
      val = Number(strats[currentTab].mtm) || 0;
    } else if (currentView === 'scheduled') {
      const sid = findRunningScheduled();
      if (sid) val = Number(strats[sid].mtm) || 0;
    } else {
      // Aggregate view: sum across all running. Tag as ' · all' when more than
      // one is running so the user knows it's a combined number.
      let total = 0, count = 0;
      for (const sid in strats) {
        const s = strats[sid];
        if (s && s.running) { count++; total += Number(s.mtm) || 0; }
      }
      if (count > 0) {
        val = total;
        if (count > 1) suffix = ` · ${count} strats`;
      }
    }

    if (val === null) {
      document.title = _BASE_TITLE;
      return;
    }
    // Whole-rupee rounding — sub-rupee decimals flicker distractingly in tabs.
    const rounded = Math.round(val);
    const sign  = rounded > 0 ? '+' : rounded < 0 ? '-' : '';
    const dot   = rounded > 0 ? '🟢' : rounded < 0 ? '🔴' : '⚪';
    const abs   = Math.abs(rounded).toLocaleString('en-IN');
    document.title = `${dot} ${sign}₹${abs}${suffix} · ${_BASE_TITLE}`;
  }

  // ── Duration timer ────────────────────────────────────────────────────────
  function fmtDur(ms) {
    const s=Math.floor(ms/1000),h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=s%60;
    if (h>0) return `${h}h ${m}m`;
    if (m>0) return `${m}m ${String(sec).padStart(2,'0')}s`;
    return `${sec}s`;
  }
  setInterval(()=>{
    const el=document.getElementById('duration');
    el.textContent=(running&&monitorStartTime)?' · '+fmtDur(Date.now()-monitorStartTime):'';
  },1000);

  // ── Market warning ────────────────────────────────────────────────────────
  let mktWarnPlayed=false;
  function checkMarket() {
    const ist=new Date(new Date().toLocaleString('en-US',{timeZone:'Asia/Kolkata'}));
    const mins=ist.getHours()*60+ist.getMinutes();
    const w=document.getElementById('market-banner');
    if (mins>=900&&mins<930) {
      w.textContent=`⏰ Market closes in ${930-mins} min — broker will auto square-off at 15:20`;
      w.classList.add('show');
      if (!mktWarnPlayed){playWarning();mktWarnPlayed=true;}
    } else {
      w.classList.remove('show');
      if (mins>=930) mktWarnPlayed=false;
    }
  }
  setInterval(checkMarket,30000); checkMarket();

  // ── Formatting ────────────────────────────────────────────────────────────
  const inr  = n => n==null?'—':(n>=0?'+':'-')+'₹'+Math.abs(n).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});
  const plain= n => n==null?'—':n.toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});
  const cls  = n => n==null?'':(n>0?'pos':n<0?'neg':'');

  function pct(ltp,avg) {
    if (!avg||!ltp) return {text:'',cls:''};
    const p=((ltp-avg)/avg*100).toFixed(1);
    return {text:(p>=0?'+':'')+p+'%',cls:p>=0?'pos':'neg'};
  }
  function setNum(id,val,signed) {
    const el=document.getElementById(id); if(!el) return;
    const txt=signed?inr(val):plain(val);
    if (el.textContent===txt) return;
    el.textContent=txt;
    el.className=el.className.replace(/\bpos\b|\bneg\b/g,'').trim();
    if (cls(val)) el.classList.add(cls(val));
    el.classList.add('flash'); setTimeout(()=>el.classList.remove('flash'),260);
  }
  function escHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

  // ── Tab management ────────────────────────────────────────────────────────
  function renderTabs(strategies, order) {
    const strip=document.getElementById('tab-strip');
    // Filter by current view (Custom → type=custom, Scheduled → type=scheduled, Arb → hide all)
    if (currentView === 'arb') { strip.innerHTML=''; return; }
    const wantType = (currentView === 'scheduled') ? 'scheduled' : 'custom';
    const visible  = order.filter(sid => (strategies[sid]?.type || 'custom') === wantType);
    // If current tab is hidden by filter, auto-switch to first visible
    if (visible.length && !visible.includes(currentTab)) currentTab = visible[0];
    const tabs=visible.map(sid=>{
      const s=strategies[sid];
      const dc=s.status==='monitoring'?'green':s.status==='triggered'?'orange':s.status==='error'?'red':'';
      return `<div class="tab${sid===currentTab?' active':''}"
                   onclick="switchTab('${sid}')"
                   ondblclick="promptRename('${sid}')">
        <span class="tab-name">${escHtml(s.name)}</span>
        <span class="tab-dot${dc?' '+dc:''}"></span>
        <span class="tab-x" onclick="event.stopPropagation();closeTab('${sid}')" title="Remove">×</span>
      </div>`;
    }).join('');
    // "+ New" button is type-aware. For scheduled (singleton), hide if one already exists.
    const hasScheduled = Object.values(strategies).some(s => (s.type || 'custom') === 'scheduled');
    const showNew = !(currentView === 'scheduled' && hasScheduled);
    const newBtn = showNew
      ? `<div class="tab-sep"></div><button class="btn-new-tab" onclick="addTab()">+ New ${currentView==='scheduled'?'Scheduled':''}</button>`
      : '';
    strip.innerHTML = tabs + newBtn;
  }

  function _clearInputTouched() {
    ['inp-profit','inp-loss','inp-activate','inp-trail-by',
     'ae-chk','ae-time','ae-qty','ae-product'].forEach(id=>{
      const el=document.getElementById(id); if(el) el._touched=false;
    });
    const chk=document.getElementById('trail-chk'); if(chk) chk._touched=false;
  }

  async function switchTab(sid) {
    if (sid===currentTab) return;
    currentTab=sid;
    prevPosCount=-1;
    _clearInputTouched();
    if (availablePositions.length) renderInstrumentList(sid);
    await loadStrategyHistory(sid);
    if (lastData?.strategies?.[sid]) renderStrategy(lastData.strategies[sid], sid);
  }

  async function addTab() {
    const n=Object.keys(lastData?.strategies||{}).length+1;
    // Offer to use a saved template
    let tplName = null;
    try {
      const tj = await (await fetch('/templates')).json();
      const tpls = tj.templates || [];
      if (tpls.length) {
        const options = tpls.map((t,i)=>`${i+1}. ${t.name}`).join('\n');
        const choice = prompt(`New strategy — pick a template (or blank for empty):\n\n${options}\n\nEnter number or leave blank:`);
        if (choice && /^\d+$/.test(choice.trim())) {
          const idx = parseInt(choice.trim()) - 1;
          if (idx >= 0 && idx < tpls.length) tplName = tpls[idx].name;
        }
      }
    } catch(e) {}
    let resp;
    const newType = (currentView === 'scheduled') ? 'scheduled' : 'custom';
    const defaultName = (newType === 'scheduled') ? 'Scheduled' : `Strategy ${n}`;
    if (tplName) {
      resp = await fetch('/strategies/from-template',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({template:tplName,name:`${tplName} ${n}`,type:newType})});
    } else {
      resp = await fetch('/strategies',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:defaultName,type:newType})});
    }
    const data=await resp.json();
    if (data.ok) { currentTab=data.id; }
    else if (data.msg) { alert(data.msg); }
  }

  async function saveAsTemplate() {
    const name = prompt('Template name:');
    if (!name) return;
    const r = await fetch('/templates',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sid:currentTab,name})});
    const d = await r.json();
    alert(d.ok ? `Saved template "${name}"` : `Failed: ${d.msg||'unknown'}`);
  }

  async function autoRefreshToken() {
    const btn = document.getElementById('btn-auto-refresh');
    const status = document.getElementById('auth-status');
    btn.disabled = true; btn.textContent = 'Trying…';
    status.textContent = 'Logging in with stored credentials + TOTP…'; status.style.color = 'var(--dim)';
    try {
      const r = await fetch('/auth/auto-refresh',{method:'POST'});
      const d = await r.json();
      if (d.ok) {
        status.textContent = '✓ ' + d.msg; status.style.color = 'var(--green-hi)';
        btn.textContent = 'Done ✓'; setTimeout(()=>{closeAuthModal();btn.disabled=false;btn.textContent='Try Auto-Refresh';},1500);
      } else {
        status.textContent = '✗ ' + d.msg; status.style.color = 'var(--orange)';
        btn.disabled = false; btn.textContent = 'Try Auto-Refresh';
      }
    } catch (e) {
      status.textContent = 'Network error: ' + e.message; status.style.color = 'var(--red)';
      btn.disabled = false; btn.textContent = 'Try Auto-Refresh';
    }
  }

  // ── View toggle (Custom / Scheduled / Arb / Owl / Settings) ──────────
  let currentView = 'strategies';
  const _VIEWS = ['strategies','scheduled','arb','owl','settings'];
  function switchView(v) {
    currentView = v;
    _VIEWS.forEach(function (name) {
      var el  = document.getElementById('view-' + name);
      if (el)  el.style.display = (name === v) ? '' : 'none';
      var btn = document.getElementById('vt-' + name);
      if (btn) btn.classList.toggle('vt-active', name === v);
    });
    if (lastData && lastData.strategies) renderTabs(lastData.strategies, lastData.strategy_order || []);
    if (v === 'arb') { loadArbConfig(); loadArbSnapshot(); loadArbPositions(); }
    if (v === 'scheduled') {
      _schClearTouched();
      var pv = document.getElementById('sch-preview');
      if (pv) { pv.style.display = 'none'; pv.innerHTML = ''; }
      renderScheduledView();
    }
    if (v === 'settings') loadSettings();
    if (v === 'owl') loadOwl();
    if (typeof lastData !== 'undefined' && lastData) _updateTabTitle(lastData);
    if (typeof closeMobileNav === 'function') closeMobileNav();
  }

  // ── Owl Method ─────────────────────────────────────────────────────────────
  let _owlPollTimer = null;

  async function loadOwl() {
    try {
      const r = await fetch('/owl');
      const d = await r.json();
      if (!d.ok) return;
      renderOwl(d);
      const h = await (await fetch('/owl/history?n=20')).json();
      renderOwlHistory(h.history || []);
    } catch (e) {
      console.error('loadOwl', e);
    }
  }

  function _owlMtmColor(v) {
    if (v === null || v === undefined || isNaN(v) || v === 0) return 'var(--text)';
    return v > 0 ? 'var(--green-hi)' : 'var(--red-hi)';
  }

  function _owlLegRow(label, leg, lastLtp) {
    if (!leg) {
      return `<div style="padding:8px 0;color:var(--dim)">${label}: <span style="color:var(--dim)">not entered yet</span></div>`;
    }
    const mtm = leg.mtm ?? 0;
    const ltp = (lastLtp !== null && lastLtp !== undefined) ? `₹${Number(lastLtp).toFixed(2)}` : '—';
    let statusBadge;
    if (leg.status === 'open')        statusBadge = '<span style="color:var(--green-hi)">● open</span>';
    else if (leg.status === 'closed') statusBadge = `<span style="color:var(--dim)">● closed (${leg.exit_reason || '?'})</span>`;
    else if (leg.status === 'failed') statusBadge = '<span style="color:var(--red-hi)">● failed</span>';
    else                              statusBadge = `<span style="color:var(--muted)">● ${leg.status}</span>`;
    const exitLine = (leg.exit_price !== null && leg.exit_price !== undefined)
      ? `<div style="font-size:10px;color:var(--dim);margin-top:2px">Exit: ₹${Number(leg.exit_price).toFixed(2)} (${leg.exit_reason})</div>`
      : '';
    return `
      <div style="padding:10px 0;border-bottom:1px solid var(--border-dim)">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:10px">
          <div>
            <div><strong>${label}</strong> ${leg.symbol || ''} ${statusBadge} ${leg.paper ? '<span style="color:var(--orange);font-size:10px;margin-left:4px">PAPER</span>' : ''}</div>
            <div style="color:var(--muted);font-size:11px;margin-top:2px">
              Strike ${leg.strike} · Qty ${leg.qty} · Entry ₹${(leg.entry_price ?? 0).toFixed(2)} · LTP ${ltp}
            </div>
            ${exitLine}
          </div>
          <div style="text-align:right;font-size:18px;font-weight:600;color:${_owlMtmColor(mtm)}">
            ₹${Number(mtm || 0).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}
          </div>
        </div>
      </div>`;
  }

  function renderOwl(d) {
    const cfg = d.config || {};
    const s   = d.state  || {};
    // Hydrate form (only once, to avoid stomping user edits mid-typing)
    if (!document.getElementById('owl-entry-time').dataset.hydrated) {
      document.getElementById('owl-entry-time').value  = cfg.entry_time   || '09:20';
      document.getElementById('owl-exit-time').value   = cfg.exit_time    || '15:00';
      document.getElementById('owl-per-leg-sl').value  = cfg.per_leg_sl   ?? 2000;
      document.getElementById('owl-lots').value        = cfg.lots         ?? 1;
      document.getElementById('owl-otm-pct').value     = cfg.otm_pct      ?? 1.5;
      document.getElementById('owl-active').checked    = !!cfg.active;
      document.getElementById('owl-paper-mode').checked = !!cfg.paper_mode;
      document.getElementById('owl-entry-time').dataset.hydrated = '1';
    }

    // Status badge
    const badge = document.getElementById('owl-status-badge');
    const life  = s.lifecycle || 'idle';
    const colorByLife = {
      idle:         ['var(--dim)',     '○ idle'],
      armed:        ['var(--blue)',    '◐ armed'],
      in_position:  ['var(--green-hi)', '● in position'],
      squared_off:  ['var(--muted)',   '◯ squared off'],
      skipped:      ['var(--orange)',  '⊘ skipped'],
    };
    const [col, lbl] = colorByLife[life] || ['var(--muted)', life];
    const modeChip = `<span style="color:var(--orange);font-size:10px;margin-left:6px">${cfg.paper_mode ? 'PAPER' : 'LIVE'}</span>`;
    const activeChip = cfg.active
      ? '<span style="color:var(--green-hi);font-size:10px;margin-left:6px">AUTO</span>'
      : '<span style="color:var(--dim);font-size:10px;margin-left:6px">manual</span>';
    badge.innerHTML = `<span style="color:${col}">${lbl}</span> ${activeChip} ${modeChip}`;

    // Today body
    document.getElementById('owl-today-date').textContent = s.session_date || d.today || '';
    const body = document.getElementById('owl-today-body');
    if (d.is_expiry_day) {
      body.innerHTML = '<div style="color:var(--orange)">⊘ Monthly expiry day — Owl rule says skip. No entry today.</div>';
    } else if (s.skip_today) {
      body.innerHTML = `<div style="color:var(--orange)">⊘ Skipped: ${s.skip_reason || 'manual'}</div>`;
    } else if (!s.anchor_price) {
      body.innerHTML = '<div style="color:var(--dim)">Awaiting setup. Will arm at <strong>' + (cfg.entry_time || '09:20') + '</strong>, or hit "Enter Now" to trigger manually.</div>';
    } else {
      const ce = s.ce_leg, pe = s.pe_leg;
      const netMtm = (ce?.mtm || 0) + (pe?.mtm || 0);
      body.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;padding-bottom:10px;border-bottom:1px solid var(--border-dim)">
          <div>
            <div style="color:var(--muted);font-size:11px">NIFTY @ entry</div>
            <div style="font-size:18px">₹${Number(s.anchor_price).toFixed(2)}</div>
            <div style="color:var(--dim);font-size:10px;margin-top:2px">Expiry ${s.expiry || '—'}</div>
          </div>
          <div style="text-align:right">
            <div style="color:var(--muted);font-size:11px">Net day MTM</div>
            <div style="font-size:22px;font-weight:600;color:${_owlMtmColor(netMtm)}">
              ₹${Number(netMtm).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}
            </div>
          </div>
        </div>
        ${_owlLegRow('CE', ce, s.last_ce_ltp)}
        ${_owlLegRow('PE', pe, s.last_pe_ltp)}
      `;
    }

    // Logs
    const logsEl = document.getElementById('owl-logs');
    if (!s.logs || s.logs.length === 0) {
      logsEl.innerHTML = '<span style="color:var(--dim)">No activity yet.</span>';
    } else {
      logsEl.innerHTML = s.logs.slice(0, 30).map(l => {
        const extras = Object.entries(l).filter(([k]) => !['ts','event'].includes(k))
          .map(([k,v]) => `${k}=${typeof v === 'number' ? v : JSON.stringify(v)}`).join(' ');
        return `<div><span style="color:var(--muted)">${l.ts}</span> <strong>${l.event}</strong> <span style="color:var(--dim)">${extras}</span></div>`;
      }).join('');
    }
  }

  function renderOwlHistory(rows) {
    const body = document.getElementById('owl-history-body');
    if (!rows || rows.length === 0) {
      body.innerHTML = '<tr><td colspan="8" style="padding:10px;color:var(--dim);text-align:center">No history yet.</td></tr>';
      return;
    }
    body.innerHTML = rows.slice().reverse().map(r => {
      const net = r.net_mtm ?? 0;
      const cePnl = r.ce_mtm ?? 0;
      const pePnl = r.pe_mtm ?? 0;
      if (r.skip) {
        return `<tr style="border-bottom:1px solid var(--border-dim)">
          <td style="padding:6px 8px">${r.date}</td>
          <td colspan="6" style="padding:6px 8px;color:var(--orange)">⊘ skipped — ${r.skip_reason || ''}</td>
          <td style="padding:6px 8px;color:var(--dim)">—</td>
        </tr>`;
      }
      return `<tr style="border-bottom:1px solid var(--border-dim)">
        <td style="padding:6px 8px">${r.date}</td>
        <td style="padding:6px 8px;text-align:right">${r.anchor_price?.toFixed?.(2) ?? '—'}</td>
        <td style="padding:6px 8px;text-align:right">${r.ce_strike ?? '—'}</td>
        <td style="padding:6px 8px;text-align:right">${r.pe_strike ?? '—'}</td>
        <td style="padding:6px 8px;text-align:right;color:${_owlMtmColor(cePnl)}">${Number(cePnl).toLocaleString('en-IN',{maximumFractionDigits:0})}</td>
        <td style="padding:6px 8px;text-align:right;color:${_owlMtmColor(pePnl)}">${Number(pePnl).toLocaleString('en-IN',{maximumFractionDigits:0})}</td>
        <td style="padding:6px 8px;text-align:right;font-weight:600;color:${_owlMtmColor(net)}">${Number(net).toLocaleString('en-IN',{maximumFractionDigits:0})}</td>
        <td style="padding:6px 8px;color:var(--dim)">${r.paper ? 'paper' : 'live'}</td>
      </tr>`;
    }).join('');
  }

  function _owlSetResult(elId, ok, msg) {
    const el = document.getElementById(elId);
    el.innerHTML = `<span style="color:${ok ? 'var(--green-hi)' : 'var(--red-hi)'}">${ok ? '✓' : '⚠'} ${msg}</span>`;
    setTimeout(() => { if (el.textContent.includes(msg)) el.innerHTML = ''; }, 4000);
  }

  async function saveOwlConfig() {
    const payload = {
      entry_time: document.getElementById('owl-entry-time').value,
      exit_time:  document.getElementById('owl-exit-time').value,
      per_leg_sl: parseFloat(document.getElementById('owl-per-leg-sl').value),
      lots:       parseInt(document.getElementById('owl-lots').value, 10),
      otm_pct:    parseFloat(document.getElementById('owl-otm-pct').value),
      active:     document.getElementById('owl-active').checked,
      paper_mode: document.getElementById('owl-paper-mode').checked,
    };
    const r = await fetch('/owl/config', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body:   JSON.stringify(payload),
    });
    const d = await r.json();
    if (!d.ok) { _owlSetResult('owl-config-result', false, d.error || 'Save failed.'); return; }
    _owlSetResult('owl-config-result', true, 'Saved.');
  }

  async function owlEnterNow() {
    if (!confirm('Enter Owl strangle now?\n\nThis will fetch NIFTY spot, compute today\'s CE/PE strikes (±1.5% from spot), and place SELL orders (or paper-record them if Paper mode is on).')) return;
    const r = await fetch('/owl/enter', { method: 'POST' });
    const d = await r.json();
    _owlSetResult('owl-action-result', !!d.ok, d.ok ? 'Entering…' : (d.error || 'Failed.'));
    setTimeout(loadOwl, 1500);
  }

  async function owlExit(leg) {
    const labels = { ce: 'CE leg', pe: 'PE leg', both: 'BOTH legs' };
    if (!confirm(`Exit ${labels[leg]}? This buys back the short option(s) at market.`)) return;
    const r = await fetch(`/owl/exit/${leg}`, { method: 'POST' });
    const d = await r.json();
    _owlSetResult('owl-action-result', !!d.ok, d.ok ? `Exiting ${labels[leg]}…` : (d.error || 'Failed.'));
    setTimeout(loadOwl, 1500);
  }

  // Auto-refresh while view is open
  setInterval(() => {
    if (currentView === 'owl') loadOwl();
  }, 3000);

  // ── Settings: unified loader ──────────────────────────────────────────────
  async function loadSettings() {
    // Fan out — four independent endpoints.
    loadMISConfig();
    loadAuthStatus();
    loadKiteCreds();
    loadTelegramConfig();
  }

  // ── Settings: Kite token auto-refresh status ─────────────────────────────
  async function loadAuthStatus() {
    const badge = document.getElementById('auth-status-badge');
    try {
      const r = await fetch('/auth/status');
      const d = await r.json();
      if (!d.ok) { badge.innerHTML = '<span style="color:var(--red-hi)">● load failed</span>'; return; }
      const enabled = !!d.enabled;
      document.getElementById('auth-status-state').textContent  = enabled ? 'enabled' : 'disabled (creds missing)';
      document.getElementById('auth-status-state').style.color  = enabled ? 'var(--green-hi)' : 'var(--red-hi)';
      document.getElementById('auth-status-next').textContent   = d.next_scheduled || '—';
      document.getElementById('auth-status-last').textContent   = d.last_refresh   || 'never';
      const missing = (d.missing || []);
      document.getElementById('auth-status-missing').textContent = missing.length ? missing.join(', ') : 'none — all set';
      document.getElementById('auth-status-missing').style.color = missing.length ? 'var(--orange)' : 'var(--green-hi)';
      badge.innerHTML = enabled
        ? '<span style="color:var(--green-hi)">● armed</span>'
        : '<span style="color:var(--red-hi)">● needs creds</span>';
    } catch (e) {
      badge.innerHTML = '<span style="color:var(--red-hi)">● load failed</span>';
    }
  }

  async function testTokenRefresh() {
    const out = document.getElementById('auth-test-result');
    out.innerHTML = '<span style="color:var(--dim)">Running headless TOTP login…</span>';
    try {
      const r = await fetch('/auth/auto-refresh', {method: 'POST'});
      const d = await r.json();
      if (d.ok) {
        out.innerHTML = `<span style="color:var(--green-hi)">✓ ${d.msg}</span>`;
      } else {
        out.innerHTML = `<span style="color:var(--red-hi)">⚠ ${d.msg}</span>`;
      }
      loadAuthStatus();
    } catch (e) {
      out.innerHTML = `<span style="color:var(--red-hi)">⚠ ${e.message}</span>`;
    }
  }

  // ── Settings: MIS pre-square ──────────────────────────────────────────────
  async function loadMISConfig() {
    const badge = document.getElementById('mis-status-badge');
    try {
      const r = await fetch('/config/mis-presquare');
      const d = await r.json();
      if (!d.ok) { badge.innerHTML = '<span style="color:var(--red-hi)">● load failed</span>'; return; }
      const cfg = d.config || {};
      const enabledEl = document.getElementById('mis-enabled');
      const timeEl    = document.getElementById('mis-time');
      if (enabledEl) enabledEl.checked = cfg.enabled !== false;
      if (timeEl)    timeEl.value      = cfg.time   || '15:15';
      badge.innerHTML = cfg.enabled !== false
        ? `<span style="color:var(--green-hi)">● armed</span> <span style="color:var(--dim)">fires ${cfg.time || '15:15'} IST</span>`
        : '<span style="color:var(--dim)">○ disabled</span>';
    } catch (e) {
      badge.innerHTML = '<span style="color:var(--red-hi)">● load failed</span>';
    }
  }

  async function saveMISConfig() {
    const out = document.getElementById('mis-result');
    const enabled = document.getElementById('mis-enabled').checked;
    const time    = document.getElementById('mis-time').value || '15:15';
    out.innerHTML = '<span style="color:var(--dim)">Saving…</span>';
    const r = await fetch('/config/mis-presquare', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ enabled, time }),
    });
    const d = await r.json();
    if (!d.ok) { out.innerHTML = `<span style="color:var(--red-hi)">⚠ ${d.error || 'save failed'}</span>`; return; }
    out.innerHTML = '<span style="color:var(--green-hi)">✓ Saved</span>';
    setTimeout(() => { out.textContent = ''; }, 1500);
    loadMISConfig();
  }

  // ── Settings: Kite credentials ────────────────────────────────────────────
  async function loadKiteCreds() {
    try {
      const r = await fetch('/config/kite');
      const d = await r.json();
      if (!d.ok) return;
      const creds = d.creds || {};
      for (const k of Object.keys(creds)) {
        const inp    = document.getElementById('kc-' + k);
        const status = document.getElementById('kc-' + k + '-status');
        if (inp) {
          inp.value = '';   // always blank — user must re-paste to update
          if (creds[k].set) {
            if (k === 'KITE_USER_ID') {
              inp.placeholder = 'Saved: ' + (creds[k].value || '');
            } else {
              inp.placeholder = 'Saved: ' + (creds[k].masked || '••••') + '  (leave blank to keep)';
            }
          } else {
            inp.placeholder = 'Not set';
          }
        }
        if (status) {
          status.textContent = creds[k].set ? '✓ saved' : '○ not set';
          status.style.color = creds[k].set ? 'var(--green-hi)' : 'var(--dim)';
        }
      }
    } catch (e) {
      console.error('loadKiteCreds', e);
    }
  }

  async function saveKiteCreds() {
    const out = document.getElementById('kc-result');
    const fields = ['API_KEY','API_SECRET','ACCESS_TOKEN','KITE_USER_ID','KITE_PASSWORD','KITE_TOTP_SECRET'];
    // Only include fields the user actually typed into. Empty input = "keep current".
    const payload = {};
    for (const k of fields) {
      const v = document.getElementById('kc-' + k).value.trim();
      if (v) payload[k] = v;
    }
    if (Object.keys(payload).length === 0) {
      out.innerHTML = '<span style="color:var(--orange)">Nothing to save — paste a value to update.</span>';
      return;
    }
    out.innerHTML = '<span style="color:var(--dim)">Saving…</span>';
    const r = await fetch('/config/kite', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(payload),
    });
    const d = await r.json();
    if (!d.ok) { out.innerHTML = `<span style="color:var(--red-hi)">⚠ ${d.error || 'save failed'}</span>`; return; }
    out.innerHTML = `<span style="color:var(--green-hi)">✓ Updated ${(d.changed || []).length} field(s)</span>`;
    setTimeout(() => { out.textContent = ''; }, 2000);
    loadKiteCreds();
  }

  // ── Telegram alerts config ────────────────────────────────────────────────
  async function loadTelegramConfig() {
    const badge   = document.getElementById('tg-status-badge');
    const tokenEl = document.getElementById('tg-token');
    const chatEl  = document.getElementById('tg-chat-id');
    const result  = document.getElementById('tg-result');
    if (result) result.textContent = '';
    try {
      const r = await fetch('/config/telegram');
      const d = await r.json();
      if (!d.ok) {
        badge.innerHTML  = '<span style="color:var(--red-hi)">● load failed</span>';
        return;
      }
      // Show the saved chat_id (it's not a secret — the bot token is).
      chatEl.value = d.chat_id || '';
      // Use masked token as placeholder; leave the input blank so submitting
      // an empty token doesn't accidentally overwrite the saved one. Users
      // re-enter the full token only when changing it.
      tokenEl.value = '';
      tokenEl.placeholder = d.configured
        ? `Saved: ${d.token_masked}  (leave blank to keep)`
        : 'e.g. 1234567890:AAH•••••••••••••';
      if (d.configured) {
        badge.innerHTML = '<span style="color:var(--green-hi)">● configured</span>';
      } else {
        badge.innerHTML = '<span style="color:var(--dim)">● not configured</span>';
      }
    } catch (e) {
      badge.innerHTML = '<span style="color:var(--red-hi)">● load failed</span>';
    }
  }

  function _tgSetResult(ok, msg) {
    const el = document.getElementById('tg-result');
    const color = ok ? 'var(--green-hi)' : 'var(--red-hi)';
    const icon  = ok ? '✓' : '⚠';
    el.innerHTML = `<span style="color:${color}">${icon} ${msg}</span>`;
  }

  async function saveTelegramConfig() {
    const tokenInput = document.getElementById('tg-token').value.trim();
    const chatId     = document.getElementById('tg-chat-id').value.trim();
    if (!chatId) { _tgSetResult(false, 'Chat ID is required.'); return; }

    // If the token field is blank AND we already have one saved, this is a
    // "keep token, only update chat_id" case — fetch current state to get
    // a real (masked) marker so we can preserve the existing token.
    let token = tokenInput;
    if (!token) {
      const cur = await (await fetch('/config/telegram')).json();
      if (!cur.configured) {
        _tgSetResult(false, 'Bot token is required (no token currently saved).');
        return;
      }
      // The backend doesn't expose the real token, so we can't "preserve" it
      // by re-sending it. We send a sentinel and have the backend treat
      // empty-token-with-existing-chat_id as a no-op for token... but to
      // keep the backend simple, just tell the user to re-paste.
      _tgSetResult(false, 'Token field is blank — paste the full token to save, or hit "Disable alerts" to clear.');
      return;
    }

    const r = await fetch('/config/telegram', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ token, chat_id: chatId }),
    });
    const d = await r.json();
    if (!d.ok) { _tgSetResult(false, d.error || 'Save failed.'); return; }
    _tgSetResult(true, 'Saved. Click "Send test message" to verify.');
    loadTelegramConfig();
  }

  async function testTelegramConfig() {
    _tgSetResult(true, 'Sending…');
    const r = await fetch('/config/telegram/test', { method: 'POST' });
    const d = await r.json();
    if (!d.ok) { _tgSetResult(false, d.error || 'Test failed.'); return; }
    _tgSetResult(true, d.message || 'Test message sent. Check Telegram.');
  }

  async function clearTelegramConfig() {
    if (!confirm('Disable Telegram alerts?\n\nAll outgoing alerts stop and the bot stops listening for commands. Your token + chat_id are removed from this instance\'s .env. You can re-enable any time by saving them again.')) return;
    const r = await fetch('/config/telegram', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ token: '', chat_id: '' }),
    });
    const d = await r.json();
    if (!d.ok) { _tgSetResult(false, d.error || 'Clear failed.'); return; }
    _tgSetResult(true, 'Alerts disabled.');
    document.getElementById('tg-token').value   = '';
    document.getElementById('tg-chat-id').value = '';
    loadTelegramConfig();
  }



  // ── Arb Monitor ──────────────────────────────────────────────────────────
  async function loadArbConfig() {
    try {
      const d = await (await fetch('/arb/config')).json();
      if (!d.ok) return;
      const c = d.config;
      const el = id => document.getElementById(id);

      // Populate underlying dropdown from server (so new entries auto-appear)
      const sel = el('arb-underlying');
      if (!sel._touched && d.underlyings) {
        const cur = sel.value || c.underlying || 'NIFTY';
        sel.innerHTML = d.underlyings.map(u =>
          `<option value="${u.key}">${u.label} (lot ${u.lot_size}, step ${u.strike_step}, ${u.exchange})</option>`
        ).join('');
        sel.value = cur;
      }

      if (!sel._touched)                  sel.value                  = c.underlying || 'NIFTY';
      if (!el('arb-strikes')._touched)    el('arb-strikes').value    = c.strikes_around_atm || 3;
      if (!el('arb-alert')._touched)      el('arb-alert').value      = c.alert_pts || 3;
      setArbMonitorButton(c.monitor_active);
    } catch (e) {}
  }

  function setArbMonitorButton(active) {
    const btn = document.getElementById('arb-start-btn');
    const dot = document.getElementById('arb-mon-dot');
    const txt = document.getElementById('arb-mon-text');
    if (active) {
      btn.textContent = '⏹ Stop Monitor';
      btn.style.borderColor = 'var(--red)';
      btn.style.color = 'var(--red-hi)';
      dot.className = 'conn-dot ok';
      txt.textContent = 'MONITORING';
    } else {
      btn.textContent = '▶ Start Monitor';
      btn.style.borderColor = 'var(--green)';
      btn.style.color = 'var(--green-hi)';
      dot.className = 'conn-dot';
      txt.textContent = 'IDLE';
    }
  }

  async function saveArbConfig() {
    const payload = {
      underlying:         document.getElementById('arb-underlying').value,
      strikes_around_atm: parseInt(document.getElementById('arb-strikes').value) || 3,
      alert_pts:          parseFloat(document.getElementById('arb-alert').value) || 3,
    };
    await fetch('/arb/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    ['arb-underlying','arb-strikes','arb-alert'].forEach(id => { const e=document.getElementById(id); if(e) e._touched=false; });
    // Reset chart strike memory so it re-picks ATM for the new underlying
    arbChartStrike = null;
    if (arbChart) { arbChart.destroy(); arbChart = null; }
    document.getElementById('arb-chart-panel').style.display = 'none';
  }

  async function toggleArbMonitor() {
    const cur = (document.getElementById('arb-mon-text').textContent || '').toUpperCase();
    const wantActive = cur !== 'MONITORING';
    await fetch('/arb/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({monitor_active:wantActive})});
    setArbMonitorButton(wantActive);
    if (wantActive) setTimeout(loadArbSnapshot, 3500);
  }

  async function loadArbSnapshot() {
    if (currentView !== 'arb') return;
    try {
      const d = await (await fetch('/arb/snapshot')).json();
      if (!d.ok || !d.snapshot || !d.snapshot.ts) {
        document.getElementById('arb-snap-panel').style.display = 'none';
        document.getElementById('arb-table-panel').style.display = 'none';
        return;
      }
      renderArbSnapshot(d.snapshot);
    } catch (e) {}
  }

  function renderArbSnapshot(s) {
    document.getElementById('arb-snap-panel').style.display = '';
    document.getElementById('arb-table-panel').style.display = '';
    const threshold = (s.config && typeof s.config.alert_pts === 'number') ? s.config.alert_pts : 3;
    document.getElementById('arb-summary').innerHTML =
      `<span style="color:var(--text)">${s.underlying}</span>` +
      `  ·  Spot <span style="color:var(--text)">₹${s.spot.toLocaleString('en-IN')}</span>` +
      `  ·  ATM <span style="color:var(--text)">${s.atm}</span>` +
      `  ·  Expiry <span style="color:var(--text)">${s.expiry}</span>` +
      `  ·  ${s.fut_sym} bid <span style="color:var(--text)">₹${s.fut_bid}</span>` +
      ` / ask <span style="color:var(--text)">₹${s.fut_ask}</span>` +
      `  ·  Lot ${s.lot_size}` +
      `  ·  Round-trip cost <span style="color:var(--orange)">~${(s.cost_pts ?? 0).toFixed(2)} pts</span>` +
      `  ·  Execute threshold <span style="color:var(--green-hi)">+${threshold.toFixed(2)} pts</span>` +
      `  ·  <span style="color:var(--dim)">@ ${s.ts.slice(11,19)}</span>`;

    // Keep chart-strike dropdown in sync with current snapshot's strikes
    _arbChartUpdateStrikeOptions(s);

    const lots = parseInt(document.getElementById('arb-lots').value) || 1;
    const tbody = document.getElementById('arb-tbody');
    const cls  = v => v > 0 ? 'pos' : v < 0 ? 'neg' : '';
    const fmt  = v => (v >= 0 ? '+' : '') + v.toFixed(2);

    tbody.innerHTML = s.rows.map(r => {
      const sellOk = r.net_sell_pts > threshold;
      const buyOk  = r.net_buy_pts  > threshold;
      const sellCls = sellOk ? 'profitable' : cls(r.net_sell_pts);
      const buyCls  = buyOk  ? 'profitable' : cls(r.net_buy_pts);
      return `<tr class="${r.is_atm?'atm-row':''}">
        <td>${r.strike}${r.is_atm?' ★':''}</td>
        <td>${r.ce_bid}</td><td>${r.ce_ask}</td>
        <td>${r.pe_bid}</td><td>${r.pe_ask}</td>
        <td class="fut-cell">${s.fut_bid}</td><td class="fut-cell">${s.fut_ask}</td>
        <td>${r.synth_buy_at}</td><td>${r.synth_sell_at}</td>
        <td class="${sellCls}">${fmt(r.net_sell_pts)}</td>
        <td class="${buyCls}">${fmt(r.net_buy_pts)}</td>
        <td style="white-space:nowrap">
          <button class="btn-arb-exec ${sellOk?'go':''}" ${sellOk?'':'disabled'}
                  onclick="${sellOk?`executeArb('${s.underlying}',${r.strike},'SELL_F_BUY_SYNTH',${lots})`:''}">Sell F</button>
          <button class="btn-arb-exec ${buyOk?'go':''}" ${buyOk?'':'disabled'}
                  onclick="${buyOk?`executeArb('${s.underlying}',${r.strike},'BUY_F_SELL_SYNTH',${lots})`:''}"
                  style="margin-left:4px">Buy F</button>
        </td>
      </tr>`;
    }).join('');
  }

  async function executeArb(underlying, strike, direction, lots) {
    if (!confirm(`Execute arb? ${direction} on ${underlying} K=${strike}  ·  ${lots} lot(s)\n\nThis will fire 3 LIVE orders.`)) return;
    const r = await fetch('/arb/execute', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({underlying, strike, direction, lots}),
    });
    const d = await r.json();
    alert(d.ok ? 'Execution started — watch Telegram for result.' : ('Failed: ' + (d.msg || '?')));
    setTimeout(loadArbPositions, 5000);
  }

  async function loadArbPositions() {
    if (currentView !== 'arb') return;
    try {
      const d = await (await fetch('/arb/positions')).json();
      if (!d.ok) return;
      renderArbPositions(d.positions || []);
    } catch (e) {}
  }

  function renderArbPositions(positions) {
    const el = document.getElementById('arb-positions');
    const open = positions.filter(p => p.status === 'open');
    const closed = positions.filter(p => p.status !== 'open').slice(-5).reverse();
    if (!open.length && !closed.length) {
      el.innerHTML = '<div style="font-family:var(--mono);font-size:12px;color:var(--dim)">No positions yet.</div>';
      return;
    }
    const fmt = v => v == null ? '—' : (v > 0 ? '+' : '') + v.toFixed(2);
    const renderOne = p => {
      const cls = v => v == null ? '' : (v > 0 ? 'pos' : v < 0 ? 'neg' : '');
      if (p.status === 'open') {
        return `<div class="arb-pos-row">
          <div><div class="label">${p.id} · ${p.underlying} K=${p.strike}</div><div class="value">${p.direction}</div></div>
          <div><div class="label">Entry basis</div><div class="value">${fmt(p.entry_basis)} pts</div></div>
          <div><div class="label">Current</div><div class="value ${cls(p.current_basis)}">${fmt(p.current_basis)} pts</div></div>
          <div><div class="label">Unrealized</div><div class="value ${cls(p.unrealized_pts)}">${fmt(p.unrealized_pts)} pts · ₹${(p.unrealized_inr||0).toLocaleString('en-IN')}</div></div>
          <div><button class="btn-arb-exec" style="border-color:var(--red);color:var(--red-hi)" onclick="unwindArb('${p.id}')">Unwind</button></div>
        </div>`;
      }
      return `<div class="arb-pos-row" style="opacity:.6">
        <div><div class="label">${p.id} · CLOSED</div><div class="value">${p.underlying} K=${p.strike}</div></div>
        <div><div class="label">Entry</div><div class="value">${fmt(p.entry_basis)}</div></div>
        <div><div class="label">Exit</div><div class="value">${fmt(p.exit_basis)}</div></div>
        <div><div class="label">Realized</div><div class="value ${cls(p.realized_pts)}">${fmt(p.realized_pts)} pts · ₹${(p.realized_inr||0).toLocaleString('en-IN')}</div></div>
        <div></div>
      </div>`;
    };
    el.innerHTML = [...open.map(renderOne), ...closed.map(renderOne)].join('');
  }

  async function unwindArb(aid) {
    if (!confirm(`Unwind arb ${aid}? Squares off all 3 legs.`)) return;
    const r = await fetch('/arb/unwind/' + aid, {method: 'POST'});
    const d = await r.json();
    alert(d.ok ? 'Unwind started — watch Telegram.' : ('Failed: ' + (d.msg || '?')));
    setTimeout(loadArbPositions, 5000);
  }

  // ── Synthetic future chart ────────────────────────────────────────────────
  let arbChart = null;
  let arbChartStrike = null;

  function _arbChartUpdateStrikeOptions(snap) {
    const sel = document.getElementById('arb-chart-strike');
    if (!sel || !snap || !snap.rows) return;
    const cur = sel.value || (snap.atm + '');
    const opts = snap.rows.map(r => `<option value="${r.strike}">${r.strike}${r.is_atm?' (ATM)':''}</option>`).join('');
    if (sel.innerHTML !== opts) sel.innerHTML = opts;
    // Default to ATM on first render
    if (!sel.value || !snap.rows.find(r => r.strike == sel.value)) {
      sel.value = snap.atm;
    } else {
      sel.value = cur;
    }
    if (!arbChartStrike) arbChartStrike = sel.value;
  }

  function onArbChartStrikeChange() {
    arbChartStrike = document.getElementById('arb-chart-strike').value;
    refreshArbChart();
  }

  async function refreshArbChart() {
    if (currentView !== 'arb' || !arbChartStrike) return;
    const u = document.getElementById('arb-underlying').value;
    try {
      const r = await fetch(`/arb/history?underlying=${u}&strike=${arbChartStrike}`);
      const d = await r.json();
      if (!d.ok) return;
      renderArbChart(d.history || []);
    } catch (e) {}
  }

  function renderArbChart(history) {
    document.getElementById('arb-chart-panel').style.display = '';
    const ctx = document.getElementById('arb-chart-canvas').getContext('2d');
    if (arbChart) arbChart.destroy();

    const labels = history.map(h => h.ts);
    const spotData  = history.map(h => h.spot);
    const synthData = history.map(h => h.synth);
    const futData   = history.map(h => h.fut);

    const latest = history[history.length - 1];
    const statsEl = document.getElementById('arb-chart-stats');
    if (latest) {
      const basis = (latest.fut - latest.synth).toFixed(2);
      const bClr  = basis > 0.5 ? 'var(--green-hi)' : basis < -0.5 ? 'var(--red-hi)' : 'var(--muted)';
      statsEl.innerHTML =
        `Spot <span style="color:var(--text)">₹${latest.spot}</span>  ·  ` +
        `Synth <span style="color:var(--blue)">₹${latest.synth}</span>  ·  ` +
        `Future <span style="color:var(--orange)">₹${latest.fut}</span>  ·  ` +
        `Basis (F−Synth): <span style="color:${bClr};font-weight:700">${basis >= 0 ? '+' : ''}${basis}</span>`;
    } else {
      statsEl.textContent = 'Collecting data… start monitor and wait a few ticks';
    }
    if (!history.length) {
      if (arbChart) arbChart.destroy();
      return;
    }

    arbChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [
          {label:'Spot',      data: spotData,  borderColor: _cssVar('--green-hi'), borderWidth: 2,   pointRadius: 0, tension: 0.15, fill: false},
          {label:'Synthetic', data: synthData, borderColor: _cssVar('--blue'),     borderWidth: 1.5, borderDash:[5,3], pointRadius: 0, tension: 0.15, fill: false},
          {label:'Future',    data: futData,   borderColor: _cssVar('--orange'),   borderWidth: 1.5, borderDash:[3,3], pointRadius: 0, tension: 0.15, fill: false},
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: {mode:'index', intersect:false},
        plugins: {
          legend: {
            display: true, align: 'end', position: 'top',
            labels: {color: _cssVar('--muted'), font:{family:"'Comic Sans MS','Comic Neue',cursive", size:11}, boxWidth: 18, boxHeight: 2, padding: 12}
          },
          tooltip: {
            backgroundColor: _cssVar('--surface-2'),
            borderColor: _cssVar('--border'), borderWidth: 1,
            titleColor: _cssVar('--muted'), bodyColor: _cssVar('--text'),
            titleFont:{family:"'Comic Sans MS','Comic Neue',cursive", size:11},
            bodyFont:{family:"'Comic Sans MS','Comic Neue',cursive", size:12},
            callbacks: {label: c => `  ${c.dataset.label}: ₹${c.raw}`}
          }
        },
        scales: {
          x: {ticks:{color: _cssVar('--dim'), font:{family:"'Comic Sans MS','Comic Neue',cursive", size:10}, maxTicksLimit:8, maxRotation:0}, grid:{color: _cssVar('--border-dim')}},
          y: {ticks:{color: _cssVar('--dim'), font:{family:"'Comic Sans MS','Comic Neue',cursive", size:10}, callback: v=>'₹'+v.toLocaleString('en-IN')}, grid:{color: _cssVar('--border-dim')}}
        }
      }
    });
  }

  // Arb polling while in arb view (snapshot every 3s, chart every 3s, positions every 5s)
  setInterval(() => { if (currentView === 'arb') loadArbSnapshot(); }, 3000);
  setInterval(() => { if (currentView === 'arb') refreshArbChart(); }, 3000);
  setInterval(() => { if (currentView === 'arb') loadArbPositions(); }, 5000);

  // ── Export modal ─────────────────────────────────────────────────────────
  async function openExportModal() {
    document.getElementById('export-modal').classList.add('show');
    const listEl = document.getElementById('export-files');
    listEl.innerHTML = '<div style="font-family:var(--mono);font-size:11px;color:var(--dim);padding:6px">Loading…</div>';
    try {
      const r = await fetch('/export/csv-files');
      const d = await r.json();
      if (!d.ok || !d.files || !d.files.length) {
        listEl.innerHTML = '<div style="font-family:var(--mono);font-size:11px;color:var(--dim);padding:6px">No CSV files yet.</div>';
        return;
      }
      const fmtSize = b => b < 1024 ? `${b} B` : b < 1024*1024 ? `${(b/1024).toFixed(1)} KB` : `${(b/(1024*1024)).toFixed(2)} MB`;
      listEl.innerHTML = d.files.map(f => `
        <a class="export-file-row" href="/export/csv/${encodeURIComponent(f.name)}" target="_blank">
          <span class="ef-name">${escHtml(f.name)}</span>
          <span class="ef-size">${fmtSize(f.size)}</span>
          <span class="ef-time">${f.mtime.replace('T',' ').slice(0,19)}</span>
          <span class="ef-arrow">↓</span>
        </a>
      `).join('');
    } catch (e) {
      listEl.innerHTML = `<div style="font-family:var(--mono);font-size:11px;color:var(--red);padding:6px">${e.message}</div>`;
    }
  }
  function closeExportModal() {
    document.getElementById('export-modal').classList.remove('show');
  }

  // ── Scheduled view ────────────────────────────────────────────────────────
  let _schSkipToday = null;   // YYYY-MM-DD when user clicked Skip Today

  function _findScheduledStrategy() {
    if (!lastData?.strategies) return null;
    for (const sid of (lastData.strategy_order || Object.keys(lastData.strategies))) {
      const s = lastData.strategies[sid];
      if (s && (s.type || 'custom') === 'scheduled') return {sid, s};
    }
    return null;
  }

  function _todayIso() {
    const d = new Date();
    return d.toISOString().slice(0, 10);
  }
  function _hhmmNow() {
    const d = new Date();
    return d.toTimeString().slice(0, 5);
  }
  function _timeUntil(hhmm) {
    if (!hhmm) return null;
    const [h, m] = hhmm.split(':').map(Number);
    const now = new Date();
    const target = new Date(now); target.setHours(h, m, 0, 0);
    if (target < now) return null;
    const diffMin = Math.round((target - now) / 60000);
    if (diffMin < 60) return `${diffMin} min`;
    return `${Math.floor(diffMin/60)}h ${diffMin%60}m`;
  }

  function _schEnsureSettingsRendered() {
    const el = document.getElementById('sch-settings');
    if (!el || el.dataset.rendered === '1') return;
    el.innerHTML = `
      <div class="field"><label>Auto-entry</label>
        <label class="sw"><input type="checkbox" id="sch-ae-chk" oninput="this._touched=true"><div class="sw-track"></div></label>
      </div>
      <div class="field"><label>Time (IST)</label>
        <input type="time" id="sch-time" oninput="this._touched=true">
      </div>
      <div class="field"><label>Qty per leg</label>
        <input type="number" id="sch-qty" min="1" oninput="this._touched=true">
      </div>
      <div class="field"><label>Product</label>
        <select id="sch-product" oninput="this._touched=true"
                style="font-family:var(--mono);font-size:15px;background:var(--bg);border:1px solid var(--border-dim);border-radius:4px;padding:8px 12px;color:var(--text);width:100%;outline:none">
          <option value="MIS">MIS</option>
          <option value="NRML">NRML</option>
        </select>
      </div>
      <div class="field"><label>Expiry</label>
        <select id="sch-expiry" oninput="this._touched=true"
                style="font-family:var(--mono);font-size:15px;background:var(--bg);border:1px solid var(--border-dim);border-radius:4px;padding:8px 12px;color:var(--text);width:100%;outline:none">
          <option value="next_weekly">Next weekly (default)</option>
          <option value="current_weekly">Current weekly</option>
          <option value="current_monthly">Current monthly</option>
          <option value="next_monthly">Next monthly</option>
        </select>
        <span id="sch-expiry-resolved" style="color:var(--dim);font-size:10px;margin-top:4px;display:block">— loading actual date —</span>
      </div>
    `;
    el.dataset.rendered = '1';
    // Populate the resolved-date labels from /expiry-options so user sees
    // the actual date their policy resolves to (e.g. 'Tue 17 Jun 2026').
    fetch('/expiry-options').then(r => r.json()).then(d => {
      if (!d.ok) return;
      const sel = document.getElementById('sch-expiry');
      const dateMap = {};
      (d.options || []).forEach(o => {
        dateMap[o.value] = o.label;
        const opt = sel.querySelector(`option[value="${o.value}"]`);
        if (opt) opt.textContent = `${opt.textContent.split(' — ')[0]} — ${o.label}`;
      });
      window._schExpiryDateMap = dateMap;
      _schUpdateResolvedHint();
    }).catch(()=>{});
    const expSel = document.getElementById('sch-expiry');
    if (expSel) expSel.addEventListener('change', _schUpdateResolvedHint);
  }

  function _schUpdateResolvedHint() {
    const sel  = document.getElementById('sch-expiry');
    const hint = document.getElementById('sch-expiry-resolved');
    if (!sel || !hint) return;
    const m = window._schExpiryDateMap || {};
    hint.textContent = m[sel.value] ? `Resolves to: ${m[sel.value]}` : '';
  }

  function _schClearTouched() {
    ['sch-ae-chk','sch-time','sch-qty','sch-product','sch-expiry',
     'sch-target','sch-sl','sch-trail-chk','sch-trail-activate','sch-trail-by']
      .forEach(id => { const e=document.getElementById(id); if(e) e._touched=false; });
  }

  function renderScheduledView() {
    const found = _findScheduledStrategy();
    document.getElementById('sch-empty').style.display   = found ? 'none' : '';
    document.getElementById('sch-content').style.display = found ? '' : 'none';
    if (!found) return;
    const {sid, s} = found;

    // ── Status banner ────
    const dot = document.getElementById('sch-mon-dot');
    const txt = document.getElementById('sch-mon-text');
    let statusHtml = '';
    if (s.running) {
      dot.className = 'conn-dot ok'; txt.textContent = 'LIVE';
      statusHtml = `<span style="color:var(--green-hi);font-weight:700">🟢 LIVE</span>  ·  Started ${s.monitoring_start_ts ? s.monitoring_start_ts.slice(11,19) : '?'}  ·  Current MTM: <span style="color:${s.mtm>=0?'var(--green-hi)':'var(--red-hi)'}">₹${(s.mtm||0).toLocaleString('en-IN',{minimumFractionDigits:2})}</span>`;
    } else if (s.status === 'triggered' || s.trigger) {
      dot.className = 'conn-dot bad'; txt.textContent = 'DONE';
      statusHtml = `<span style="color:var(--orange);font-weight:700">⚡ ${escHtml(s.trigger || 'CLOSED')}</span>  ·  Last MTM: ₹${(s.mtm||0).toLocaleString('en-IN',{minimumFractionDigits:2})}`;
    } else if (_schSkipToday === _todayIso()) {
      dot.className = 'conn-dot'; txt.textContent = 'SKIPPED';
      statusHtml = `<span style="color:var(--orange)">⏭ Skipped for today</span>`;
    } else if (s.auto_entry_last_fired === _todayIso()) {
      dot.className = 'conn-dot'; txt.textContent = 'FIRED';
      statusHtml = `<span style="color:var(--muted)">Fired earlier today @ ${s.auto_entry_time}</span>  ·  Status: ${s.auto_entry_status||'?'}`;
    } else {
      const eta = _timeUntil(s.auto_entry_time);
      dot.className = 'conn-dot'; txt.textContent = 'WAITING';
      statusHtml = s.auto_entry_enabled
        ? `<span style="color:var(--muted)">⏳ Waiting for ${s.auto_entry_time} IST</span>` + (eta ? `  ·  T-<span style="color:var(--text)">${eta}</span>` : `  ·  Window closed for today`)
        : `<span style="color:var(--dim)">Auto-entry DISABLED</span> — enable in Settings or use Trigger Now`;
    }
    document.getElementById('sch-status-body').innerHTML = statusHtml;

    // ── Plan card ────
    document.getElementById('sch-plan-body').innerHTML =
      `Underlying: <span style="color:var(--text)">NIFTY</span>  ·  ` +
      `Time: <span style="color:var(--text)">${s.auto_entry_time || '10:00'}</span> IST  ·  ` +
      `Qty/leg: <span style="color:var(--text)">${s.auto_entry_qty || 65}</span>  ·  ` +
      `Product: <span style="color:var(--text)">${s.auto_entry_product || 'MIS'}</span>  ·  ` +
      `Target: <span style="color:var(--green-hi)">₹${(s.profit_target||0).toLocaleString('en-IN')}</span>  ·  ` +
      `SL: <span style="color:var(--red-hi)">₹${(s.loss_limit||0).toLocaleString('en-IN')}</span>` +
      (s.selected?.length ? `<br><span style="color:var(--dim)">Last selected:</span> ${s.selected.map(i=>`<code style="color:var(--text)">${i.tradingsymbol}</code>`).join(' · ')}` : '');

    // Skip button label reflects current state
    const skipBtn = document.getElementById('sch-skip-btn');
    if (_schSkipToday === _todayIso()) {
      skipBtn.textContent = 'Cancel Skip';
      skipBtn.style.borderColor = 'var(--green)'; skipBtn.style.color = 'var(--green-hi)';
    } else {
      skipBtn.textContent = 'Skip Today';
      skipBtn.style.borderColor = ''; skipBtn.style.color = '';
    }

    // ── Settings: render structure once; update values only if user not editing ────
    _schEnsureSettingsRendered();
    const setVal = (id, v) => {
      const el = document.getElementById(id);
      if (el && !el._touched) el.value = v;
    };
    const aeChk = document.getElementById('sch-ae-chk');
    if (aeChk && !aeChk._touched) aeChk.checked = !!s.auto_entry_enabled;
    setVal('sch-time',    s.auto_entry_time    || '10:00');
    setVal('sch-qty',     s.auto_entry_qty     || 65);
    setVal('sch-product', s.auto_entry_product || 'MIS');
    setVal('sch-expiry',  s.auto_entry_expiry  || 'next_weekly');
    _schUpdateResolvedHint();

    // ── P&L Targets (prominent) ────
    const tgt = document.getElementById('sch-target');
    const lim = document.getElementById('sch-sl');
    if (tgt && !tgt._touched) tgt.value = s.profit_target || 2500;
    if (lim && !lim._touched) lim.value = s.loss_limit    || 2000;
    const tgtChk = document.getElementById('sch-tgt-chk');
    const slChk  = document.getElementById('sch-sl-chk');
    // Default: both ON (backwards compatible with older saved state without these fields).
    if (tgtChk) tgtChk.checked = (s.profit_target_enabled !== false);
    if (slChk)  slChk.checked  = (s.loss_limit_enabled    !== false);
    const tgtBasisSch = document.getElementById('sch-tgt-basis');
    const slBasisSch  = document.getElementById('sch-sl-basis');
    if (tgtBasisSch) tgtBasisSch.value = (s.profit_target_basis === 'exit') ? 'exit' : 'ltp';
    if (slBasisSch)  slBasisSch.value  = (s.loss_limit_basis    === 'exit') ? 'exit' : 'ltp';

    // ── Trailing SL (prominent) ────
    const trChk = document.getElementById('sch-trail-chk');
    const trAt  = document.getElementById('sch-trail-activate');
    const trBy  = document.getElementById('sch-trail-by');
    const trVal = document.getElementById('sch-trail-sl-val');
    const trBody= document.getElementById('sch-trail-body');
    if (trChk && !trChk._touched) {
      trChk.checked = !!s.trail_enabled;
      if (trBody) trBody.style.display = trChk.checked ? '' : 'none';
    }
    if (trAt && !trAt._touched) trAt.value = s.trail_activate_at || 500;
    if (trBy && !trBy._touched) trBy.value = s.trail_by          || 300;
    if (trVal) {
      if (s.trail_sl !== null && s.trail_sl !== undefined) {
        trVal.textContent = `₹${Number(s.trail_sl).toLocaleString('en-IN',{maximumFractionDigits:0})}`;
        trVal.style.color = 'var(--orange)';
      } else if (s.trail_enabled) {
        trVal.textContent = '— inactive';
        trVal.style.color = 'var(--dim)';
      } else {
        trVal.textContent = '— inactive';
        trVal.style.color = 'var(--dim)';
      }
    }

    // ── MTM Trend chart (shown when running) ────
    const chartCard = document.getElementById('sch-chart-card');
    if (s.running) {
      chartCard.style.display = '';
      _schEnsureChart(sid, s);
    } else {
      chartCard.style.display = 'none';
      if (_schChart) { try { _schChart.destroy(); } catch(e){} _schChart = null; _schChartSid = null; }
    }

    // ── Live MTM (when running) ────
    const live = document.getElementById('sch-live');
    if (s.running) {
      live.style.display = '';
      live.innerHTML = _schLiveMtmHtml(s);
    } else {
      live.style.display = 'none';
      live.innerHTML = '';
    }

    // ── Recent history ────
    document.getElementById('sch-history').innerHTML = _schHistoryHtml(s);
  }

  // ── MTM trend chart for Scheduled view (separate Chart.js instance from Custom) ──
  let _schChart    = null;
  let _schChartSid = null;
  async function _schEnsureChart(sid, s) {
    if (_schChartSid !== sid) {
      if (_schChart) { try { _schChart.destroy(); } catch(e){} _schChart = null; }
      _schChartSid = sid;
    }
    const canvas = document.getElementById('sch-chart');
    if (!canvas) return;
    if (_schChart) {
      // Just refresh target/stop lines + the latest point if it advanced
      _schUpdateChart(s);
      return;
    }
    // Build from /history/<sid>
    let history = [];
    try {
      const r = await fetch('/history/' + sid);
      const d = await r.json();
      history = d.history || [];
    } catch (e) {}
    const labels = history.map(p => (p.ts || '').slice(11, 19));
    const combined = history.map(p => p.combined);
    const target = s.profit_target || 0;
    const stop   = -(s.loss_limit || 0);
    const ctx = canvas.getContext('2d');
    _schChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [
          { label:'Combined', data:combined, borderColor:'#e6edf3', borderWidth:2, pointRadius:0, tension:0.3 },
          { label:'Target',   data:labels.map(()=>target), borderColor:'#2ea043', borderWidth:1, borderDash:[6,4], pointRadius:0 },
          { label:'Stop',     data:labels.map(()=>stop),   borderColor:'#f85149', borderWidth:1, borderDash:[6,4], pointRadius:0 },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        animation: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: '#6e7681', maxTicksLimit: 8 }, grid: { color: 'rgba(110,118,129,0.08)' } },
          y: { ticks: { color: '#6e7681', callback:v=>'₹'+v.toLocaleString('en-IN') }, grid: { color: 'rgba(110,118,129,0.08)' } },
        },
      },
    });
  }
  function _schUpdateChart(s) {
    if (!_schChart) return;
    const target = s.profit_target || 0;
    const stop   = -(s.loss_limit || 0);
    const labels = _schChart.data.labels;
    _schChart.data.datasets[1].data = labels.map(()=>target);
    _schChart.data.datasets[2].data = labels.map(()=>stop);
    _schChart.update('none');
  }
  // Append the SSE-pushed new point if it belongs to the scheduled strategy
  function _schMaybeAppendPoint(sid, pt) {
    if (!_schChart || _schChartSid !== sid) return;
    const label = pt.t || ((pt.ts || '').slice(11, 19));
    _schChart.data.labels.push(label);
    _schChart.data.datasets[0].data.push(pt.combined);
    // extend the reference lines for the new x length
    const ds = _schChart.data.datasets;
    ds[1].data.push(ds[1].data[ds[1].data.length-1] ?? 0);
    ds[2].data.push(ds[2].data[ds[2].data.length-1] ?? 0);
    _schChart.update('none');
  }

  function _schLiveMtmHtml(s) {
    const cls = s.mtm > 0 ? 'pos' : s.mtm < 0 ? 'neg' : '';
    const ecls = (s.exit_mtm ?? 0) > 0 ? 'pos' : (s.exit_mtm ?? 0) < 0 ? 'neg' : '';
    const slip = s.slippage ?? ((s.mtm ?? 0) - (s.exit_mtm ?? 0));
    const slipCls = Math.abs(slip) > 0.5 ? 'orange' : '';
    const tDim = (s.profit_target_enabled === false) ? 'opacity:.4;text-decoration:line-through' : '';
    const sDim = (s.loss_limit_enabled    === false) ? 'opacity:.4;text-decoration:line-through' : '';
    const positions = (s.positions || []).map(p => {
      const pcls = p.mtm > 0 ? 'pos' : p.mtm < 0 ? 'neg' : '';
      // Per-position bid/ask + exit MTM row (only if depth is available).
      const bidAsk = (p.bid || p.ask)
        ? `<div class="pos-row">
            <div class="stat"><div class="stat-label">Bid</div><div class="stat-val">${(p.bid||0).toFixed(2)}</div></div>
            <div class="stat"><div class="stat-label">Ask</div><div class="stat-val">${(p.ask||0).toFixed(2)}</div></div>
          </div>
          <div class="pos-row">
            <div class="stat"><div class="stat-label">Exit</div><div class="stat-val">${(p.exit_price||0).toFixed(2)}</div></div>
            <div class="stat"><div class="stat-label">Exit MTM</div><div class="stat-val ${(p.exit_mtm||0) > 0 ? 'pos' : (p.exit_mtm||0) < 0 ? 'neg' : ''}">₹${(p.exit_mtm||0).toLocaleString('en-IN',{minimumFractionDigits:2})}</div></div>
          </div>` : '';
      return `<div class="pos-card" style="margin-bottom:8px">
        <div class="pos-header">${escHtml(p.sym)} · ${p.exch}</div>
        <div class="pos-body">
          <div class="pos-row">
            <div class="stat"><div class="stat-label">Avg</div><div class="stat-val">${p.avg}</div></div>
            <div class="stat"><div class="stat-label">LTP</div><div class="stat-val">${p.ltp}</div></div>
          </div>
          <div class="pos-row">
            <div class="stat"><div class="stat-label">Qty</div><div class="stat-val">${p.qty}</div></div>
            <div class="stat"><div class="stat-label">MTM</div><div class="stat-val ${pcls}">₹${(p.mtm||0).toLocaleString('en-IN',{minimumFractionDigits:2})}</div></div>
          </div>
          ${bidAsk}
        </div>
      </div>`;
    }).join('');
    return `
      <div class="hero" style="margin-bottom:12px">
        <div class="hero-label">Combined MTM</div>
        <div class="hero-num ${cls}">₹ ${(s.mtm||0).toLocaleString('en-IN',{minimumFractionDigits:2})}</div>
        <div class="hero-meta">
          <div class="hero-stat"><div class="hero-stat-label">Exit MTM</div><div class="hero-stat-val ${ecls}">${s.exit_mtm!=null?'₹'+s.exit_mtm.toLocaleString('en-IN',{minimumFractionDigits:2}):'—'}</div></div>
          <div class="hero-divider"></div>
          <div class="hero-stat"><div class="hero-stat-label">Slippage</div><div class="hero-stat-val ${slipCls}">${Math.abs(slip).toLocaleString('en-IN',{minimumFractionDigits:2})}</div></div>
          <div class="hero-divider"></div>
          <div class="hero-stat" style="${tDim}"><div class="hero-stat-label">Target</div><div class="hero-stat-val">+₹${(s.profit_target||0).toLocaleString('en-IN')}</div></div>
          <div class="hero-divider"></div>
          <div class="hero-stat" style="${sDim}"><div class="hero-stat-label">Stop</div><div class="hero-stat-val">-₹${(s.loss_limit||0).toLocaleString('en-IN')}</div></div>
          <div class="hero-divider"></div>
          <div class="hero-stat"><div class="hero-stat-label">Peak Today</div><div class="hero-stat-val pos">${s.peak_mtm_day!=null?'₹'+s.peak_mtm_day.toLocaleString('en-IN',{minimumFractionDigits:2}):'—'}</div></div>
        </div>
      </div>
      <div class="positions">${positions}</div>
      <div class="exit-bar show" style="margin-top:8px"><button class="btn-exit" onclick="schManualExit()">⚠ EXIT NOW</button></div>
    `;
  }

  function _schHistoryHtml(s) {
    const sessions = (s.sessions || []).slice(0, 30);
    if (!sessions.length) return '<div style="color:var(--dim);padding:8px 0">No sessions yet.</div>';
    let wins = 0, total = 0;
    const rows = sessions.map(sess => {
      const start = sess.start ? new Date(sess.start) : null;
      const end   = sess.end   ? new Date(sess.end)   : null;
      const dateStr = start ? start.toLocaleDateString('en-IN',{day:'2-digit',month:'short'}) : '—';
      const range = (start && end) ? `${start.toTimeString().slice(0,5)}–${end.toTimeString().slice(0,5)}` : '—';
      const pnl = sess.final_mtm || 0;
      total += 1; if (pnl > 0) wins += 1;
      const pcls = pnl > 0 ? 'var(--green-hi)' : pnl < 0 ? 'var(--red-hi)' : 'var(--muted)';
      return `<div style="display:grid;grid-template-columns:60px 100px 1fr 100px;gap:10px;padding:6px 0;border-bottom:1px solid var(--border-dim)">
        <span style="color:var(--text)">${dateStr}</span>
        <span style="color:var(--dim)">${range}</span>
        <span style="color:var(--muted)">${escHtml(sess.trigger||'—')}</span>
        <span style="color:${pcls};text-align:right;font-weight:700">₹${pnl.toLocaleString('en-IN',{minimumFractionDigits:2,signDisplay:'always'})}</span>
      </div>`;
    }).join('');
    const totalPnl = sessions.reduce((a,b)=>a+(b.final_mtm||0),0);
    const wr = total ? (wins/total*100).toFixed(0) : 0;
    return rows + `<div style="padding-top:10px;margin-top:6px;border-top:1px solid var(--border);font-weight:700;color:var(--text)">
      Last ${total} trades  ·  Win rate <span style="color:var(--green-hi)">${wr}%</span> (${wins}/${total})  ·  Net <span style="color:${totalPnl>=0?'var(--green-hi)':'var(--red-hi)'}">₹${totalPnl.toLocaleString('en-IN',{signDisplay:'always'})}</span>
    </div>`;
  }

  async function schPreview() {
    const found = _findScheduledStrategy(); if (!found) return;
    const box = document.getElementById('sch-preview');
    box.style.display = 'block';
    box.style.borderColor = 'var(--border-dim)';
    box.innerHTML = '<span style="color:var(--dim)">Computing…</span>';
    try {
      const r = await fetch('/preview-entry/' + found.sid);
      const d = await r.json();
      if (!d.ok) {
        box.style.borderColor = 'var(--red)';
        box.innerHTML = `<span style="color:var(--red)">⚠ ${escHtml(d.error || d.msg || 'Preview failed')}</span>`;
        return;
      }
      if (d.expiry_day) {
        box.style.borderColor = 'var(--orange)';
        box.innerHTML = `<span style="color:var(--orange)">⏭ Today is monthly expiry (${d.expiry}) — auto-entry will SKIP.</span>`;
        return;
      }
      box.innerHTML = `
        <div style="color:var(--dim);font-size:10px;letter-spacing:.18em;text-transform:uppercase;margin-bottom:8px">Preview — no orders placed</div>
        <div><span style="color:var(--dim)">Prev day H/L:</span>  <span style="color:var(--text)">${d.prev_high.toLocaleString('en-IN')}</span> / <span style="color:var(--text)">${d.prev_low.toLocaleString('en-IN')}</span></div>
        <div><span style="color:var(--dim)">Expiry:</span>         <span style="color:var(--text)">${d.expiry} (${d.expiry_short})</span></div>
        <div><span style="color:var(--dim)">CE strike:</span>      <span style="color:var(--green-hi)">${d.ce_strike}</span>  →  <span style="color:var(--text)">${d.ce_symbol}</span>  <span style="color:var(--dim)">SELL ${d.qty}</span></div>
        <div><span style="color:var(--dim)">PE strike:</span>      <span style="color:var(--red-hi)">${d.pe_strike}</span>  →  <span style="color:var(--text)">${d.pe_symbol}</span>  <span style="color:var(--dim)">SELL ${d.qty}</span></div>
        <div><span style="color:var(--dim)">Product:</span>        <span style="color:var(--text)">${d.product}</span></div>
        <div style="margin-top:6px;color:var(--dim);font-size:10px">Refreshed @ ${new Date().toTimeString().slice(0,8)}</div>
      `;
    } catch (e) {
      box.style.borderColor = 'var(--red)';
      box.innerHTML = `<span style="color:var(--red)">⚠ ${e.message}</span>`;
    }
  }

  async function schTriggerNow() {
    const found = _findScheduledStrategy(); if (!found) return;
    if (!confirm(`Trigger ${found.s.name} now? Real orders will fire.`)) return;
    await fetch('/trigger-entry/' + found.sid, {method:'POST'});
    alert('Triggered — watch Telegram for fill confirmation.');
  }

  async function schManualExit() {
    const found = _findScheduledStrategy(); if (!found) return;
    if (!confirm('Exit all positions for this scheduled strategy now?')) return;
    await fetch('/exit/' + found.sid, {method:'POST'});
  }

  function schToggleSkipToday() {
    const t = _todayIso();
    _schSkipToday = (_schSkipToday === t) ? null : t;
    renderScheduledView();
    // Note: this is a UI-only "skip" hint. To enforce, we'd need a backend pause flag.
    // The Telegram /pause command provides the actual server-side skip.
    alert(_schSkipToday ? 'Skip today — UI only. For server-side skip use Telegram /pause.' : 'Skip cleared.');
  }

  async function schSaveSettings() {
    const found = _findScheduledStrategy(); if (!found) return;
    const sid = found.sid;
    // Auto-entry config (target / SL / trailing live in their own panels now)
    await fetch('/auto-entry/' + sid, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        auto_entry_enabled: document.getElementById('sch-ae-chk').checked,
        auto_entry_time:    document.getElementById('sch-time').value || '10:00',
        auto_entry_qty:     parseInt(document.getElementById('sch-qty').value) || 65,
        auto_entry_product: document.getElementById('sch-product').value || 'MIS',
        auto_entry_expiry:  document.getElementById('sch-expiry').value || 'next_weekly',
      })
    });
    ['sch-ae-chk','sch-time','sch-qty','sch-product','sch-expiry'].forEach(id => {
      const e = document.getElementById(id); if (e) e._touched = false;
    });
    const btn = document.querySelector('#view-scheduled button[onclick="schSaveSettings()"]');
    if (btn) {
      const orig = btn.textContent;
      btn.textContent = 'Saved ✓'; btn.classList.add('ok');
      setTimeout(()=>{ btn.textContent = orig; btn.classList.remove('ok'); }, 1500);
    }
  }

  async function schSaveTargets() {
    const found = _findScheduledStrategy(); if (!found) return;
    const sid = found.sid;
    const pt = parseFloat(document.getElementById('sch-target').value) || 2500;
    const ll = parseFloat(document.getElementById('sch-sl').value)     || 2000;
    const tEn = document.getElementById('sch-tgt-chk').checked;
    const sEn = document.getElementById('sch-sl-chk').checked;
    const tBasis = (document.getElementById('sch-tgt-basis')||{}).value || 'ltp';
    const sBasis = (document.getElementById('sch-sl-basis') ||{}).value || 'ltp';
    await fetch('/config/' + sid, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        profit_target: pt, loss_limit: ll,
        profit_target_enabled: tEn, loss_limit_enabled: sEn,
        profit_target_basis: tBasis, loss_limit_basis: sBasis,
      })
    });
    ['sch-target','sch-sl'].forEach(id => {
      const e = document.getElementById(id); if (e) e._touched = false;
    });
    const btn = document.getElementById('sch-btn-targets');
    if (btn) {
      const orig = btn.textContent;
      btn.textContent = 'Saved ✓'; btn.classList.add('ok');
      setTimeout(()=>{ btn.textContent = orig; btn.classList.remove('ok'); }, 1500);
    }
  }

  async function schSaveTrail() {
    const found = _findScheduledStrategy(); if (!found) return;
    const sid = found.sid;
    const enabled = document.getElementById('sch-trail-chk').checked;
    const at = parseFloat(document.getElementById('sch-trail-activate').value) || 0;
    const by = parseFloat(document.getElementById('sch-trail-by').value)       || 0;
    await fetch('/config/' + sid, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({trail_enabled: enabled, trail_activate_at: at, trail_by: by})
    });
    ['sch-trail-chk','sch-trail-activate','sch-trail-by'].forEach(id => {
      const e = document.getElementById(id); if (e) e._touched = false;
    });
    const btn = document.getElementById('sch-btn-trail');
    if (btn) {
      const orig = btn.textContent;
      btn.textContent = 'Saved ✓'; btn.classList.add('ok');
      setTimeout(()=>{ btn.textContent = orig; btn.classList.remove('ok'); }, 1500);
    }
  }

  function schOnTrailToggle() {
    const found = _findScheduledStrategy(); if (!found) return;
    const sid = found.sid;
    const chk = document.getElementById('sch-trail-chk');
    chk._touched = true;
    document.getElementById('sch-trail-body').style.display = chk.checked ? '' : 'none';
    // persist the toggle immediately so the backend respects it on the next monitor tick
    fetch('/config/' + sid, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({trail_enabled: chk.checked})
    });
  }
  function schOnTargetToggle() {
    const found = _findScheduledStrategy(); if (!found) return;
    const chk = document.getElementById('sch-tgt-chk');
    fetch('/config/' + found.sid, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({profit_target_enabled: chk.checked})
    });
  }
  function schOnSlToggle() {
    const found = _findScheduledStrategy(); if (!found) return;
    const chk = document.getElementById('sch-sl-chk');
    fetch('/config/' + found.sid, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({loss_limit_enabled: chk.checked})
    });
  }
  function schOnTargetBasisChange() {
    const found = _findScheduledStrategy(); if (!found) return;
    const sel = document.getElementById('sch-tgt-basis');
    fetch('/config/' + found.sid, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({profit_target_basis: sel.value})
    });
  }
  function schOnSlBasisChange() {
    const found = _findScheduledStrategy(); if (!found) return;
    const sel = document.getElementById('sch-sl-basis');
    fetch('/config/' + found.sid, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({loss_limit_basis: sel.value})
    });
  }

  async function schDemoteToCustom() {
    const found = _findScheduledStrategy(); if (!found) return;
    if (!confirm(`Move "${found.s.name}" back to Custom?`)) return;
    const r = await fetch('/strategies/' + found.sid + '/set-type', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({type: 'custom'}),
    });
    const d = await r.json();
    if (!d.ok) alert('Failed: ' + (d.msg || '?'));
    else { switchView('strategies'); }
  }

  async function promoteCurrentToScheduled() {
    if (!currentTab) return;
    const s = lastData?.strategies?.[currentTab]; if (!s) return;
    if (!confirm(`Promote "${s.name}" to the singleton Scheduled strategy?\n\nThis will:\n• Move it out of Custom view into Scheduled view\n• Replace any existing Scheduled strategy (if any)`)) return;
    const r = await fetch('/strategies/' + currentTab + '/set-type', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({type: 'scheduled'}),
    });
    const d = await r.json();
    if (!d.ok) { alert('Failed: ' + (d.msg || '?')); return; }
    switchView('scheduled');
  }

  async function promptConvertToScheduled() {
    // Build list of existing custom strategies
    const custom = Object.entries(lastData?.strategies || {})
      .filter(([sid, s]) => (s.type || 'custom') === 'custom');
    if (!custom.length) { alert('No Custom strategies to convert. Create one first or use "Create new".'); return; }
    const lines = custom.map(([sid, s], i) => `${i+1}. ${s.name} (${sid})`).join('\n');
    const choice = prompt(`Convert which Custom strategy to Scheduled?\n\n${lines}\n\nEnter number:`);
    if (!choice || !/^\d+$/.test(choice.trim())) return;
    const idx = parseInt(choice.trim()) - 1;
    if (idx < 0 || idx >= custom.length) { alert('Invalid choice.'); return; }
    const [sid] = custom[idx];
    const r = await fetch('/strategies/' + sid + '/set-type', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({type: 'scheduled'}),
    });
    const d = await r.json();
    if (!d.ok) alert('Failed: ' + (d.msg || '?'));
    else { currentTab = sid; }
  }

  async function loadHealthCheck() {
    try {
      const r = await fetch('/health-check');
      const d = await r.json();
      if (d.ok && d.result && d.result.timestamp) renderHealth(d.result);
    } catch (e) {}
  }
  async function runHealthCheck() {
    const btn = document.getElementById('btn-health-run');
    btn.disabled = true; btn.textContent = 'Running…';
    try {
      const r = await fetch('/health-check', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
      const d = await r.json();
      if (d.ok) renderHealth(d.result);
    } catch (e) {}
    btn.disabled = false; btn.textContent = '↻ Run Now';
  }
  function renderHealth(r) {
    const card = document.getElementById('health-card');
    card.style.display = 'flex';
    const ts = (r.timestamp || '').replace('T', ' ').slice(0, 19);
    document.getElementById('health-time').textContent = ts ? `Last: ${ts}` : '—';

    const items = [];
    items.push(r.token_ok
      ? `<span class="health-item ok">✓ Token (${r.user_name || 'OK'})</span>`
      : `<span class="health-item err">✗ Token broken</span>`);
    if (r.cash_available != null) {
      const low = r.cash_available < 30000;
      items.push(`<span class="health-item ${low?'warn':'ok'}">${low?'⚠':'✓'} Cash ₹${r.cash_available.toLocaleString('en-IN',{maximumFractionDigits:0})}</span>`);
    }
    if (r.public_ip) {
      items.push(`<span class="health-item info">⌖ IP ${r.public_ip}</span>`);
    }
    items.push(r.trading_day
      ? `<span class="health-item ok">✓ Trading day</span>`
      : `<span class="health-item warn">⏭ Holiday / weekend</span>`);
    if (r.expiry_day) {
      items.push(`<span class="health-item warn">⚠ Monthly expiry — auto-entry will SKIP</span>`);
    }
    document.getElementById('health-status').innerHTML = items.join('');

    const issuesEl = document.getElementById('health-issues');
    if (r.issues && r.issues.length) {
      issuesEl.style.display = 'block';
      issuesEl.innerHTML = '⚠ ' + r.issues.map(escHtml).join('  ·  ');
    } else {
      issuesEl.style.display = 'none';
    }

    const dueEl = document.getElementById('health-due');
    if (r.due_strategies && r.due_strategies.length) {
      dueEl.style.display = 'block';
      dueEl.innerHTML = `<span style="color:var(--dim);font-size:10px;letter-spacing:.14em;text-transform:uppercase">Auto-entry scheduled:</span> `
        + r.due_strategies.map(s => `<span class="due-strat">${escHtml(s.name)} @ ${s.time} · ${s.qty} · ${s.product}</span>`).join('');
    } else {
      dueEl.style.display = 'none';
    }
  }

  async function loadStats() {
    try {
      const d = await (await fetch('/stats')).json();
      if (!d.ok) return;
      const fmt = v => (v>=0?'+':'-')+'₹'+Math.abs(v).toLocaleString('en-IN',{maximumFractionDigits:0});
      const setEl = (id, val, cls) => {
        const el = document.getElementById(id); if (!el) return;
        el.textContent = val;
        el.className = cls || '';
      };
      setEl('stats-today',   d.today_trades ? fmt(d.today_pnl) : '—',  d.today_pnl>0?'pos':d.today_pnl<0?'neg':'');
      setEl('stats-total',   d.total_trades ? fmt(d.total_pnl) : '—',  d.total_pnl>0?'pos':d.total_pnl<0?'neg':'');
      setEl('stats-winrate', d.total_trades ? `${d.win_rate.toFixed(0)}% (${d.wins}/${d.total_trades})` : '—', '');
      setEl('stats-dd',      d.max_drawdown>0 ? '-₹'+Math.round(d.max_drawdown).toLocaleString('en-IN') : '—', d.max_drawdown>0?'neg':'');
    } catch (e) {}
  }
  // Refresh stats every 30s, health check on load
  setInterval(loadStats, 30000);
  document.addEventListener('DOMContentLoaded', () => {
    setTimeout(loadStats, 1000);
    setTimeout(loadHealthCheck, 1200);
  });

  async function closeTab(sid) {
    if (lastData?.strategies?.[sid]?.running){alert('Stop monitoring before removing this strategy.');return;}
    if (!confirm(`Remove "${lastData?.strategies?.[sid]?.name||'this strategy'}"?`)) return;
    const resp=await fetch(`/strategies/${sid}`,{method:'DELETE'});
    const data=await resp.json();
    if (!data.ok){alert(data.msg);return;}
    if (currentTab===sid) {
      const rem=(lastData?.strategy_order||[]).filter(s=>s!==sid);
      if (rem.length) switchTab(rem[0]);
    }
  }

  async function promptRename(sid) {
    const s=lastData?.strategies?.[sid]; if(!s) return;
    const name=prompt(`Rename "${s.name}":`,s.name);
    if (!name||!name.trim()) return;
    await fetch(`/strategies/${sid}/rename`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name.trim()})});
  }

  // ── Instrument selector ───────────────────────────────────────────────────
  function getSelectedSet(sid) {
    if (!selectedSets[sid]) {
      selectedSets[sid]=new Set();
      const strat=lastData?.strategies?.[sid];
      if (strat?.selected) strat.selected.forEach(s=>selectedSets[sid].add(`${s.tradingsymbol}:${s.exchange}`));
    }
    return selectedSets[sid];
  }

  async function loadPositions() {
    const btn=document.getElementById('btn-load');
    btn.textContent='↻ Loading...'; btn.disabled=true;
    try {
      const data=await (await fetch('/kite-positions')).json();
      if (!data.ok){alert('Error: '+data.error);return;}
      availablePositions=data.positions;
      renderInstrumentList(currentTab);
    } catch(e){alert('Failed: '+e);}
    finally{btn.textContent='↻ Load from Kite';btn.disabled=false;}
  }

  function renderInstrumentList(sid) {
    const body=document.getElementById('instr-body'); if(!body) return;
    const sel=getSelectedSet(sid);

    // Build set of currently-held position keys for fast lookup
    const heldKeys = new Set(availablePositions.map(p => `${p.tradingsymbol}:${p.exchange}`));
    // Stale = selected but not in current positions (squared-off, expired, etc.)
    const staleKeys = Array.from(sel).filter(k => !heldKeys.has(k));

    if (!availablePositions.length && !staleKeys.length){
      body.innerHTML='<span class="instr-hint">No open positions found in Kite</span>';
      return;
    }

    const header=`<div class="instr-header">
        <span></span>
        <span class="h-sym">Symbol</span>
        <span>Exch</span>
        <span>Qty</span>
        <span>Avg</span>
        <span>LTP</span>
        <span>P&amp;L</span>
      </div>`;

    const liveRows = availablePositions.map(p=>{
      const key=`${p.tradingsymbol}:${p.exchange}`;
      const checked=sel.has(key);
      const qc=p.quantity<0?'neg':'pos';
      const pnl = p.pnl || 0;
      const pnlC = pnl>0?'pos':pnl<0?'neg':'';
      const pnlStr = (pnl>=0?'+':'-')+'₹'+Math.abs(pnl).toLocaleString('en-IN',{minimumFractionDigits:2});
      const ltpStr = p.last_price?('₹'+p.last_price.toLocaleString('en-IN',{minimumFractionDigits:2})):'—';
      return `<label class="instr-row${checked?' checked':''}" data-sym="${p.tradingsymbol}" data-exch="${p.exchange}">
        <input type="checkbox"${checked?' checked':''}>
        <span class="instr-sym">${p.tradingsymbol}</span>
        <span class="instr-exch">${p.exchange}</span>
        <span class="instr-qty ${qc}">${p.quantity>0?'+':''}${p.quantity}</span>
        <span class="instr-avg">₹${p.average_price.toLocaleString('en-IN',{minimumFractionDigits:2})}</span>
        <span class="instr-ltp">${ltpStr}</span>
        <span class="instr-pnl ${pnlC}">${pnlStr}</span>
      </label>`;
    }).join('');

    // Ghost rows for stale selections (not held anymore — selectable to remove)
    const staleRows = staleKeys.map(key=>{
      const [sym, exch] = key.split(':');
      return `<label class="instr-row stale checked" data-sym="${sym}" data-exch="${exch}" title="No longer in your Kite positions — uncheck to remove">
        <input type="checkbox" checked>
        <span class="instr-sym" style="text-decoration:line-through;opacity:.7">${sym}</span>
        <span class="instr-exch">${exch}</span>
        <span class="instr-qty" style="color:var(--orange)">stale</span>
        <span class="instr-avg">—</span>
        <span class="instr-ltp">—</span>
        <span class="instr-pnl" style="color:var(--orange);font-size:10px">not held</span>
      </label>`;
    }).join('');

    body.innerHTML = header + liveRows + staleRows;
    body.querySelectorAll('.instr-row').forEach(row=>{
      const chk=row.querySelector('input[type="checkbox"]');
      chk.addEventListener('change',()=>{
        const key=`${row.dataset.sym}:${row.dataset.exch}`, s=getSelectedSet(currentTab);
        if (chk.checked){s.add(key);row.classList.add('checked');}
        else{s.delete(key);row.classList.remove('checked');}
        saveInstrumentSelection(currentTab);
        updateSelBadge(currentTab);
      });
    });
    updateSelBadge(sid);
  }

  async function saveInstrumentSelection(sid) {
    const instruments=Array.from(getSelectedSet(sid)).map(k=>{const[t,e]=k.split(':');return{tradingsymbol:t,exchange:e};});
    await fetch('/instruments/'+sid,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({instruments})});
  }

  function updateSelBadge(sid) {
    const badge=document.getElementById('instr-badge'); if(!badge) return;
    const n=getSelectedSet(sid).size;
    badge.textContent=n===0?'none selected':`${n} selected`;
    badge.className='instr-badge'+(n>0?' active':'');
  }

  // ── Positions rendering ───────────────────────────────────────────────────
  function renderPositions(positions) {
    const grid=document.getElementById('positions-grid'); if(!grid) return;
    if (!positions||!positions.length) {
      if (prevPosCount!==0){grid.innerHTML='<div class="pos-placeholder">Select instruments and start monitoring</div>';grid.style.gridTemplateColumns='1fr';prevPosCount=0;}
      return;
    }
    if (positions.length!==prevPosCount) {
      prevPosCount=positions.length;
      grid.style.gridTemplateColumns=positions.length===1?'1fr':positions.length<=4?'1fr 1fr':'repeat(auto-fill,minmax(280px,1fr))';
      grid.innerHTML=positions.map((p,i)=>posCardHTML(p,i)).join('');
    } else {
      positions.forEach((p,i)=>updatePosCard(p,i));
    }
  }

  function posCardHTML(p,i) {
    const pc=pct(p.ltp,p.avg);
    // Bid/ask + exit MTM row only rendered when backend has depth data.
    const bidAskRow = (p.bid || p.ask) ? `
        <div class="pos-row">
          <div class="stat"><span class="stat-label">Bid</span><span class="stat-val" id="pbid${i}">${plain(p.bid||0)}</span></div>
          <div class="stat"><span class="stat-label">Ask</span><span class="stat-val" id="pask${i}">${plain(p.ask||0)}</span></div>
        </div>
        <div class="pos-mtm-row stat"><span class="stat-label">Exit MTM</span><span class="stat-val ${cls(p.exit_mtm)}" id="pemtm${i}">${inr(p.exit_mtm||0)}</span></div>` : '';
    return `<div class="pos-card">
      <div class="pos-header">${p.sym}</div>
      <div class="pos-body">
        <div class="pos-row">
          <div class="stat"><span class="stat-label">Avg Price</span><span class="stat-val" id="pa${i}">${plain(p.avg)}</span></div>
          <div class="stat"><span class="stat-label">LTP</span><span class="stat-val" id="pl${i}">${plain(p.ltp)}</span><span class="stat-pct ${pc.cls}" id="pp${i}">${pc.text}</span></div>
        </div>
        <div class="pos-row">
          <div class="stat"><span class="stat-label">Qty</span><span class="stat-val">${p.qty}</span></div>
          <div class="stat"><span class="stat-label">Exchange</span><span class="stat-val" style="font-size:12px;color:var(--dim)">${p.exch}</span></div>
        </div>
        <div class="pos-mtm-row stat"><span class="stat-label">MTM</span><span class="stat-val ${cls(p.mtm)}" id="pm${i}">${inr(p.mtm)}</span></div>
        ${bidAskRow}
      </div>
    </div>`;
  }

  function updatePosCard(p,i) {
    const ltpEl=document.getElementById('pl'+i), mtmEl=document.getElementById('pm'+i), pctEl=document.getElementById('pp'+i);
    if (!ltpEl) return;
    const lt=plain(p.ltp);
    if (ltpEl.textContent!==lt){ltpEl.textContent=lt;ltpEl.classList.add('flash');setTimeout(()=>ltpEl.classList.remove('flash'),260);}
    const pc=pct(p.ltp,p.avg); pctEl.textContent=pc.text; pctEl.className='stat-pct '+pc.cls;
    const mt=inr(p.mtm);
    if (mtmEl.textContent!==mt){mtmEl.textContent=mt;mtmEl.className='stat-val '+cls(p.mtm);mtmEl.classList.add('flash');setTimeout(()=>mtmEl.classList.remove('flash'),260);}
    // Bid/ask/exit MTM updates — only when those rows were rendered (cards from
    // before depth-data feed will just lack the elements, which is fine).
    const bidEl =document.getElementById('pbid'+i);
    const askEl =document.getElementById('pask'+i);
    const emtmEl=document.getElementById('pemtm'+i);
    if (bidEl)  bidEl.textContent  = plain(p.bid||0);
    if (askEl)  askEl.textContent  = plain(p.ask||0);
    if (emtmEl){
      const ev = inr(p.exit_mtm||0);
      if (emtmEl.textContent!==ev){emtmEl.textContent=ev; emtmEl.className='stat-val '+cls(p.exit_mtm);}
    }
  }

  // ── Chart ────────────────────────────────────────────────────────────────
  function _refLineValues(sid) {
    const s = lastData?.strategies?.[sid||currentTab] || {};
    return {
      pt:  s.profit_target || 0,
      ll:  s.loss_limit    || 0,
      tsl: (s.trail_enabled && s.trail_sl != null) ? s.trail_sl : null,
    };
  }

  function _cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  function buildChart(syms) {
    const ctx=document.getElementById('mtm-chart').getContext('2d');
    if (chart) chart.destroy();
    const cd=getChartData(currentTab);
    const {pt, ll, tsl}=_refLineValues();
    const blank=()=>cd.labels.map(()=>null);
    const flat=v=>cd.labels.map(()=>v);
    // Theme-aware colors
    const cText = _cssVar('--text'),  cMuted = _cssVar('--muted'),  cDim = _cssVar('--dim'),
          cBdrD = _cssVar('--border-dim'), cBdr = _cssVar('--border'), cSurf2 = _cssVar('--surface-2'),
          cGr   = _cssVar('--green'), cRed  = _cssVar('--red'), cOr  = _cssVar('--orange');
    const datasets=[
      {label:'Combined',data:cd.combined,borderColor:cText,borderWidth:2,pointRadius:0,tension:.2,fill:false},
      ...syms.map((sym,i)=>({label:sym,data:cd.pos[i]||[],borderColor:POS_COLORS[i%POS_COLORS.length],borderWidth:1.5,pointRadius:0,tension:.2,fill:false})),
      {label:'',  isRefLine:true,data:flat(0),  borderColor:cBdr,borderWidth:1,  borderDash:[4,4],pointRadius:0,tension:0,fill:false},
      {label:'Target', isRefLine:true,data:flat(pt), borderColor:cGr, borderWidth:1.5,borderDash:[6,3],pointRadius:0,tension:0,fill:false},
      {label:'Stop',   isRefLine:true,data:flat(-ll),borderColor:cRed,borderWidth:1.5,borderDash:[6,3],pointRadius:0,tension:0,fill:false},
      {label:'Trail',  isRefLine:true,data:tsl!=null?flat(tsl):blank(),borderColor:cOr,borderWidth:1.5,borderDash:[3,3],pointRadius:0,tension:0,fill:false},
    ];
    updateChartLegend(syms, tsl);
    chart=new Chart(ctx,{
      type:'line',
      data:{labels:cd.labels,datasets},
      options:{
        responsive:true,maintainAspectRatio:false,animation:false,
        interaction:{mode:'index',intersect:false},
        plugins:{
          legend:{display:false},
          tooltip:{
            filter:item=>!item.dataset.isRefLine,
            backgroundColor:cSurf2,borderColor:cBdr,borderWidth:1,titleColor:cMuted,bodyColor:cText,
            titleFont:{family:"'Comic Sans MS','Comic Neue',cursive",size:11},bodyFont:{family:"'Comic Sans MS','Comic Neue',cursive",size:12},
            callbacks:{label:c=>c.dataset.label?`  ${c.dataset.label}: ${c.raw>=0?'+':''}₹${c.raw.toLocaleString('en-IN',{minimumFractionDigits:2})}`:null}
          }
        },
        scales:{
          x:{ticks:{color:cDim,font:{family:"'Comic Sans MS','Comic Neue',cursive",size:11},maxTicksLimit:8,maxRotation:0},grid:{color:cBdrD}},
          y:{ticks:{color:cDim,font:{family:"'Comic Sans MS','Comic Neue',cursive",size:11},callback:v=>(v>=0?'+':'')+'₹'+v.toLocaleString('en-IN')},grid:{color:cBdrD}}
        }
      }
    });
  }

  function updateChartLegend(syms, tsl) {
    const hasTrail = tsl != null;
    document.getElementById('chart-legend').innerHTML=
      `<div class="legend-item"><div class="legend-dot" style="background:#e6edf3"></div>Combined</div>`+
      syms.map((sym,i)=>`<div class="legend-item"><div class="legend-dot" style="background:${POS_COLORS[i%POS_COLORS.length]}"></div>${sym}</div>`).join('')+
      `<div class="legend-item"><div class="legend-dot" style="background:#2ea043;border-radius:1px;height:2px;width:14px;margin-top:1px"></div>Target</div>`+
      `<div class="legend-item"><div class="legend-dot" style="background:#f85149;border-radius:1px;height:2px;width:14px;margin-top:1px"></div>Stop</div>`+
      (hasTrail?`<div class="legend-item"><div class="legend-dot" style="background:#e3b341;border-radius:1px;height:2px;width:14px;margin-top:1px"></div>Trail SL</div>`:'');
  }

  function updateChartRefLines() {
    if (!chart || currentTab === null) return;
    const cd = getChartData(currentTab);
    const {pt, ll, tsl} = _refLineValues();
    const n = chart.data.datasets.length;
    if (n < 4) return;
    const flat = v => cd.labels.map(()=>v);
    const blank = () => cd.labels.map(()=>null);
    chart.data.datasets[n-4].data = flat(0);
    chart.data.datasets[n-3].data = flat(pt);
    chart.data.datasets[n-2].data = flat(-ll);
    chart.data.datasets[n-1].data = tsl != null ? flat(tsl) : blank();
    updateChartLegend(chartSyms, tsl);
    chart.update('none');
  }

  function maybeRebuildChart(positions) {
    const syms=(positions||[]).map(p=>p.sym);
    if (JSON.stringify(syms)===JSON.stringify(chartSyms)) return;
    chartSyms=syms;
    const cd=getChartData(currentTab);
    cd.syms=syms;
    if (cd.pos.length!==syms.length) cd.pos=syms.map(()=>[]);
    buildChart(syms);
  }

  function appendChartPoint(sid, pt) {
    const cd=getChartData(sid);
    cd.labels.push(pt.t); cd.combined.push(pt.combined);
    (pt.pos||[]).forEach((pp,i)=>{if(!cd.pos[i])cd.pos[i]=[];cd.pos[i].push(pp.mtm);});
    if (sid!==currentTab||!chart) return;
    chart.data.datasets[0].data=cd.combined;
    (pt.pos||[]).forEach((_,i)=>{if(chart.data.datasets[i+1])chart.data.datasets[i+1].data=cd.pos[i]||[];});
    // refresh reference lines (last 4 datasets: zero, target, stop, trail)
    const n=chart.data.datasets.length;
    const {pt:target, ll, tsl}=_refLineValues(sid);
    chart.data.datasets[n-4].data=cd.labels.map(()=>0);
    chart.data.datasets[n-3].data=cd.labels.map(()=>target);
    chart.data.datasets[n-2].data=cd.labels.map(()=>-ll);
    chart.data.datasets[n-1].data=tsl!=null?cd.labels.map(()=>tsl):cd.labels.map(()=>null);
    chart.update('none');
  }

  async function loadStrategyHistory(sid) {
    const cd=getChartData(sid);
    if (cd.loaded){chartSyms=cd.syms;buildChart(cd.syms);return;}
    try {
      const data=await(await fetch('/history/'+sid)).json();
      cd.loaded=true;
      if (data.points.length) {
        cd.syms=(data.points[0].pos||[]).map(p=>p.sym);
        cd.pos=cd.syms.map(()=>[]);
        data.points.forEach(pt=>{
          cd.labels.push(pt.t); cd.combined.push(pt.combined);
          (pt.pos||[]).forEach((pp,i)=>{if(cd.pos[i])cd.pos[i].push(pp.mtm);});
        });
      }
      chartSyms=cd.syms; buildChart(cd.syms);
    } catch(_){buildChart([]);}
  }

  // ── Controls ──────────────────────────────────────────────────────────────
  async function toggleMonitor() {
    if (!currentTab) return;
    const isRunning=lastData?.strategies?.[currentTab]?.running;
    await fetch((isRunning?'/stop/':'/start/')+currentTab,{method:'POST'});
  }

  async function manualExit() {
    if (!confirm('Exit ALL positions for this strategy at market price now?')) return;
    await fetch('/exit/'+currentTab,{method:'POST'});
  }

  async function saveTargets() {
    const btn=document.getElementById('btn-tgt');
    const profit=parseFloat(document.getElementById('inp-profit').value);
    const loss=parseFloat(document.getElementById('inp-loss').value);
    if (isNaN(profit)||isNaN(loss)) return;
    const tEn=document.getElementById('tgt-chk').checked;
    const sEn=document.getElementById('sl-chk').checked;
    const tBasis=(document.getElementById('inp-tgt-basis')||{}).value || 'ltp';
    const sBasis=(document.getElementById('inp-sl-basis') ||{}).value || 'ltp';
    await fetch('/config/'+currentTab,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
      profit_target:profit, loss_limit:loss,
      profit_target_enabled:tEn, loss_limit_enabled:sEn,
      profit_target_basis:tBasis, loss_limit_basis:sBasis,
    })});
    document.getElementById('inp-profit')._touched=false;
    document.getElementById('inp-loss')._touched=false;
    btn.textContent='Saved ✓';btn.classList.add('ok');setTimeout(()=>{btn.textContent='Save Targets';btn.classList.remove('ok');},1500);
  }

  // Flip the enable flag immediately on toggle change (matches the trail-toggle
  // pattern: a checkbox change should take effect without a Save click).
  function onTargetToggle() {
    const chk=document.getElementById('tgt-chk');
    if (!currentTab) return;
    fetch('/config/'+currentTab,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({profit_target_enabled:chk.checked})});
  }
  function onSlToggle() {
    const chk=document.getElementById('sl-chk');
    if (!currentTab) return;
    fetch('/config/'+currentTab,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({loss_limit_enabled:chk.checked})});
  }
  function onTargetBasisChange() {
    const sel=document.getElementById('inp-tgt-basis');
    if (!currentTab) return;
    fetch('/config/'+currentTab,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({profit_target_basis:sel.value})});
  }
  function onSlBasisChange() {
    const sel=document.getElementById('inp-sl-basis');
    if (!currentTab) return;
    fetch('/config/'+currentTab,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({loss_limit_basis:sel.value})});
  }

  async function saveTrail() {
    const btn=document.getElementById('btn-trail');
    const enabled=document.getElementById('trail-chk').checked;
    const at=parseFloat(document.getElementById('inp-activate').value);
    const by=parseFloat(document.getElementById('inp-trail-by').value);
    if (isNaN(at)||isNaN(by)) return;
    await fetch('/config/'+currentTab,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({trail_enabled:enabled,trail_activate_at:at,trail_by:by})});
    document.getElementById('inp-activate')._touched=false;
    document.getElementById('inp-trail-by')._touched=false;
    btn.textContent='Saved ✓';btn.classList.add('ok');setTimeout(()=>{btn.textContent='Save Trail';btn.classList.remove('ok');},1500);
  }

  function onTrailToggle() {
    const chk=document.getElementById('trail-chk');
    chk._touched=true;
    document.getElementById('trail-body').className='trail-body'+(chk.checked?' on':'');
    if (currentTab) fetch('/config/'+currentTab,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({trail_enabled:chk.checked})});
  }

  async function saveAutoEntry() {
    const btn=document.getElementById('btn-ae-save');
    const payload={
      auto_entry_enabled: document.getElementById('ae-chk').checked,
      auto_entry_time:    document.getElementById('ae-time').value || '10:00',
      auto_entry_qty:     parseInt(document.getElementById('ae-qty').value) || 65,
      auto_entry_product: document.getElementById('ae-product').value || 'MIS',
    };
    await fetch('/auto-entry/'+currentTab,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    ['ae-chk','ae-time','ae-qty','ae-product'].forEach(id=>{const el=document.getElementById(id); if(el) el._touched=false;});
    btn.textContent='Saved ✓';btn.classList.add('ok');setTimeout(()=>{btn.textContent='Save';btn.classList.remove('ok');},1500);
  }

  async function triggerEntryNow() {
    if (!confirm('Place SHORT STRANGLE entry now? This will fire real orders.')) return;
    const btn=document.getElementById('btn-ae-trigger');
    btn.textContent='Triggering…'; btn.disabled=true;
    const r=await fetch('/trigger-entry/'+currentTab,{method:'POST'});
    const j=await r.json();
    btn.textContent=j.ok?'Triggered ✓':'Failed'; setTimeout(()=>{btn.textContent='Trigger Now';btn.disabled=false;},2500);
  }

  async function previewEntry() {
    const btn=document.getElementById('btn-ae-preview');
    const box=document.getElementById('ae-preview');
    btn.textContent='Loading…'; btn.disabled=true;
    try {
      const r = await fetch('/preview-entry/'+currentTab);
      const d = await r.json();
      if (!d.ok) {
        box.style.display='block';
        box.style.borderColor='var(--red)';
        box.innerHTML=`<span style="color:var(--red)">⚠ ${d.error||d.msg||'Preview failed'}</span>`;
      } else if (d.expiry_day) {
        box.style.display='block';
        box.style.borderColor='var(--orange)';
        box.innerHTML=`<span style="color:var(--orange)">⏭ Today is monthly expiry (${d.expiry}) — auto-entry will SKIP.</span>`;
      } else {
        box.style.display='block';
        box.style.borderColor='var(--border-dim)';
        box.innerHTML = `
          <div style="color:var(--dim);font-size:10px;letter-spacing:.18em;text-transform:uppercase;margin-bottom:8px">Preview — no orders placed</div>
          <div><span style="color:var(--dim)">Prev day H/L:</span>  <span style="color:var(--text)">${d.prev_high.toLocaleString('en-IN')}</span> / <span style="color:var(--text)">${d.prev_low.toLocaleString('en-IN')}</span></div>
          <div><span style="color:var(--dim)">Expiry:</span>         <span style="color:var(--text)">${d.expiry} (${d.expiry_short})</span></div>
          <div><span style="color:var(--dim)">CE strike:</span>      <span style="color:var(--green-hi)">${d.ce_strike}</span>  →  <span style="color:var(--text)">${d.ce_symbol}</span>  <span style="color:var(--dim)">SELL ${d.qty}</span></div>
          <div><span style="color:var(--dim)">PE strike:</span>      <span style="color:var(--red-hi)">${d.pe_strike}</span>  →  <span style="color:var(--text)">${d.pe_symbol}</span>  <span style="color:var(--dim)">SELL ${d.qty}</span></div>
          <div><span style="color:var(--dim)">Product:</span>        <span style="color:var(--text)">${d.product}</span></div>
        `;
      }
    } catch (e) {
      box.style.display='block';
      box.style.borderColor='var(--red)';
      box.innerHTML=`<span style="color:var(--red)">⚠ ${e.message}</span>`;
    }
    btn.textContent='Preview Strikes'; btn.disabled=false;
  }

  // ── Per-strategy render ───────────────────────────────────────────────────
  function renderStrategy(s, sid) {
    running=s.running;

    // Status
    const dot=document.getElementById('status-dot'), stxt=document.getElementById('status-text'), btn=document.getElementById('main-btn');
    dot.className='dot';
    stxt.textContent=(s.status||'idle').toUpperCase();
    if (s.status==='monitoring'){
      dot.classList.add('green');
      btn.textContent='STOP';btn.className='btn btn-halt';
      if (s.monitoring_start_ts&&!monitorStartTime) monitorStartTime=new Date(s.monitoring_start_ts).getTime();
      document.getElementById('instr-card').classList.add('locked');
    } else {
      if (s.status==='triggered') dot.classList.add('orange');
      else if (s.status==='error') dot.classList.add('red');
      btn.textContent='START';btn.className='btn btn-go';
      if (!running) monitorStartTime=null;
      document.getElementById('instr-card').classList.remove('locked');
    }

    // Exit bar
    document.getElementById('exit-bar').className='exit-bar'+(s.running?' show':'');

    // Trigger banner + sound
    const banner=document.getElementById('trigger-banner');
    if (s.trigger){banner.textContent='⚡ '+s.trigger;banner.classList.add('show');if(!alertPlayed){playAlert();alertPlayed=true;}}
    else{banner.classList.remove('show');alertPlayed=false;}

    // Hero MTM
    const heroNum=document.getElementById('hero-num'),newTxt=inr(s.mtm);
    if (heroNum.textContent!==newTxt){
      heroNum.textContent=newTxt;heroNum.className='hero-num '+cls(s.mtm);
      heroNum.classList.add('flash');setTimeout(()=>heroNum.classList.remove('flash'),260);
      document.getElementById('hero').style.setProperty('--hero-color',
        s.mtm>0?'rgba(46,160,67,.09)':s.mtm<0?'rgba(248,81,73,.09)':'transparent');
    }

    // Hero meta
    // Target/SL: dimmed when their enable toggle is off so user can see the
    // value but knows it won't fire. Strikethrough conveys "ignored".
    const tEnVisible = (s.profit_target_enabled !== false);
    const sEnVisible = (s.loss_limit_enabled    !== false);
    const tgtProfitEl=document.getElementById('tgt-profit');
    const tgtLossEl  =document.getElementById('tgt-loss');
    tgtProfitEl.textContent=s.profit_target!=null?'+₹'+s.profit_target.toLocaleString('en-IN'):'—';
    tgtLossEl.textContent  =s.loss_limit!=null?'-₹'+s.loss_limit.toLocaleString('en-IN'):'—';
    tgtProfitEl.style.textDecoration = tEnVisible ? 'none' : 'line-through';
    tgtProfitEl.style.opacity        = tEnVisible ? '1'    : '0.4';
    tgtLossEl.style.textDecoration   = sEnVisible ? 'none' : 'line-through';
    tgtLossEl.style.opacity          = sEnVisible ? '1'    : '0.4';
    // Exit MTM + slippage (only meaningful while running with positions)
    const exitEl = document.getElementById('hero-exit-mtm');
    const slipEl = document.getElementById('hero-slippage');
    if (s.running && s.exit_mtm != null) {
      exitEl.textContent = inr(s.exit_mtm);
      exitEl.className   = 'hero-stat-val ' + cls(s.exit_mtm);
      const slip = s.slippage ?? (s.mtm - s.exit_mtm);
      slipEl.textContent = '₹' + Math.abs(slip).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2});
      // Positive slippage = MTM is BETTER than exit MTM (exit costs money)
      slipEl.className   = 'hero-stat-val ' + (slip > 0.5 ? 'orange' : 'pos');
    } else {
      exitEl.textContent = '—'; exitEl.className = 'hero-stat-val';
      slipEl.textContent = '—'; slipEl.className = 'hero-stat-val';
    }
    const peakEl=document.getElementById('peak-mtm-day');
    peakEl.textContent=s.peak_mtm_day!=null?inr(s.peak_mtm_day):'—';
    const trailStat=document.getElementById('trail-stat'),trailDiv=document.getElementById('trail-divider');
    if (s.trail_enabled&&s.trail_sl!=null){trailStat.style.display=trailDiv.style.display='';document.getElementById('tgt-trail').textContent=inr(s.trail_sl);}
    else{trailStat.style.display=trailDiv.style.display='none';}

    // Positions + chart ref lines
    maybeRebuildChart(s.positions);
    updateChartRefLines();
    renderPositions(s.positions);

    // Instrument selector badge
    if (!selectedSets[sid]) getSelectedSet(sid);
    updateSelBadge(sid);
    if (availablePositions.length) renderInstrumentList(sid);

    // Config inputs — sync when not running AND user hasn't started editing (_touched)
    const elP=document.getElementById('inp-profit'),elL=document.getElementById('inp-loss');
    const elA=document.getElementById('inp-activate'),elB=document.getElementById('inp-trail-by');
    if (!s.running) {
      if (!elP._touched&&s.profit_target!=null)    elP.value=s.profit_target;
      if (!elL._touched&&s.loss_limit!=null)        elL.value=s.loss_limit;
      if (!elA._touched&&s.trail_activate_at!=null) elA.value=s.trail_activate_at;
      if (!elB._touched&&s.trail_by!=null)          elB.value=s.trail_by;
    }
    // Toggles sync regardless of running state — the user should see the
    // current backend state. Default ON when fields are missing (backwards compat).
    const tgtChk2=document.getElementById('tgt-chk'), slChk2=document.getElementById('sl-chk');
    if (tgtChk2) tgtChk2.checked = (s.profit_target_enabled !== false);
    if (slChk2)  slChk2.checked  = (s.loss_limit_enabled    !== false);
    const tgtBasisEl=document.getElementById('inp-tgt-basis');
    const slBasisEl =document.getElementById('inp-sl-basis');
    if (tgtBasisEl) tgtBasisEl.value = (s.profit_target_basis === 'exit') ? 'exit' : 'ltp';
    if (slBasisEl)  slBasisEl.value  = (s.loss_limit_basis    === 'exit') ? 'exit' : 'ltp';
    const chk=document.getElementById('trail-chk');
    if (!s.running&&!chk._touched) chk.checked=!!s.trail_enabled;
    document.getElementById('trail-body').className='trail-body'+(s.trail_enabled?' on':'');
    const slVal=document.getElementById('trail-sl-val'),slBox=document.getElementById('trail-sl-box');
    if (s.trail_sl!=null){slVal.textContent=inr(s.trail_sl);slVal.className='trail-sl-val active';slBox.className='trail-sl-box active';}
    else{slVal.textContent='— inactive';slVal.className='trail-sl-val';slBox.className='trail-sl-box';}

    // Auto-entry fields
    const aeChk=document.getElementById('ae-chk'),aeT=document.getElementById('ae-time'),
          aeQ=document.getElementById('ae-qty'),aeP=document.getElementById('ae-product');
    if (!aeChk._touched) aeChk.checked=!!s.auto_entry_enabled;
    if (!aeT._touched && s.auto_entry_time)    aeT.value   = s.auto_entry_time;
    if (!aeQ._touched && s.auto_entry_qty!=null) aeQ.value = s.auto_entry_qty;
    if (!aeP._touched && s.auto_entry_product) aeP.value   = s.auto_entry_product;
    const aeStatus=document.getElementById('ae-status');
    const stat=s.auto_entry_status||'idle', last=s.auto_entry_last_fired||'never';
    aeStatus.textContent=`Status: ${stat}  ·  Last fired: ${last}`;
    aeStatus.style.color = stat.startsWith('failed') ? 'var(--red)' :
                           stat==='done' ? 'var(--green-hi)' :
                           stat==='firing' ? 'var(--orange)' : 'var(--dim)';

    // Logs
    if (s.logs?.length) document.getElementById('log-lines').innerHTML=s.logs.slice(0,10).map(l=>{
      const c=l.includes('ERROR')?'err':l.includes('***')?'trig':'';
      return `<div class="log-line ${c}">${l}</div>`;
    }).join('');

    // Sessions
    renderSessions(s.sessions||[]);
  }

  // ── Main render ───────────────────────────────────────────────────────────
  function render(d) {
    lastData=d;

    // Auto-pick first tab
    if (!currentTab||!d.strategies[currentTab])
      currentTab=d.strategy_order[0];

    // Init selected sets from server state (once)
    if (!selectedInitialized) {
      selectedInitialized=true;
      for (const [sid,strat] of Object.entries(d.strategies)) {
        if (!selectedSets[sid]){
          selectedSets[sid]=new Set((strat.selected||[]).map(s=>`${s.tradingsymbol}:${s.exchange}`));
        }
      }
    }
    // Also init any new strategies added after first render
    for (const [sid,strat] of Object.entries(d.strategies)) {
      if (!selectedSets[sid])
        selectedSets[sid]=new Set((strat.selected||[]).map(s=>`${s.tradingsymbol}:${s.exchange}`));
    }

    renderTabs(d.strategies, d.strategy_order);

    const strat=d.strategies[currentTab];
    if (strat) renderStrategy(strat, currentTab);

    // Keep the Scheduled view in sync (live MTM, status, history)
    if (currentView === 'scheduled') renderScheduledView();

    // Chart point for any strategy — fans out to Custom and Scheduled charts
    if (d.new_point&&d.new_point_sid) {
      appendChartPoint(d.new_point_sid, d.new_point);
      _schMaybeAppendPoint(d.new_point_sid, d.new_point);
    }
  }

  // ── Session history ───────────────────────────────────────────────────────
  function _postExitBlock(s) {
    const pe = s.post_exit;
    if (!pe || (pe.current==null && pe.peak==null && pe.eod==null)) return '';
    const exit_mtm = s.final_mtm || 0;
    const now      = pe.current;
    const peak     = pe.peak;
    const trough   = pe.trough;
    const eod      = pe.eod;        // only populated after 15:30 finalize
    const ref      = (eod != null ? eod : now);    // primary reference
    const diff     = (ref != null) ? ref - exit_mtm : null;
    const live     = pe.active ? '<span class="pe-live">● LIVE</span>' : '<span class="pe-done">FINAL</span>';
    const verdict  = diff == null ? '' :
                     Math.abs(diff) < 50      ? '<span class="pe-verdict">≈ right call</span>' :
                     diff < 0                 ? '<span class="pe-verdict pe-saved">✓ exit saved you ₹' + Math.abs(Math.round(diff)).toLocaleString('en-IN') + '</span>' :
                                                '<span class="pe-verdict pe-missed">⤴ left ₹' + Math.round(diff).toLocaleString('en-IN') + ' on the table</span>';
    const cls = v => v == null ? '' : (v > 0 ? 'pos' : v < 0 ? 'neg' : '');
    const fmtV = v => v == null ? '—' : inr(v);
    return `<div class="post-exit-block">
      <div class="pe-header">
        <span class="pe-label">Post-exit ${live}</span>
        ${pe.last_update ? `<span class="pe-time">@ ${pe.last_update}</span>` : ''}
      </div>
      <div class="pe-grid">
        <div class="pe-cell"><span>Now</span><span class="${cls(now)}">${fmtV(now)}</span></div>
        <div class="pe-cell"><span>Peak${pe.peak_at?` (${pe.peak_at})`:''}</span><span class="${cls(peak)}">${fmtV(peak)}</span></div>
        <div class="pe-cell"><span>Trough</span><span class="${cls(trough)}">${fmtV(trough)}</span></div>
        ${eod != null ? `<div class="pe-cell"><span>EOD</span><span class="${cls(eod)}">${fmtV(eod)}</span></div>` : ''}
      </div>
      ${verdict ? `<div class="pe-verdict-row">${verdict}</div>` : ''}
    </div>`;
  }

  function renderSessions(sessions) {
    const el=document.getElementById('session-list');
    if (!sessions.length){el.innerHTML='<div class="session-empty">No sessions recorded yet.</div>';return;}
    el.innerHTML=sessions.slice(0,15).map(s=>{
      const st=new Date(s.start),en=new Date(s.end);
      const dur=Math.round((en-st)/60000);
      const fmt=d=>d.getHours().toString().padStart(2,'0')+':'+d.getMinutes().toString().padStart(2,'0');
      const fc=s.final_mtm>=0?'pos':'neg';
      return `<div class="session-row-wrap">
        <div class="session-row">
          <div class="session-time">${fmt(st)}–${fmt(en)} (${dur}m)</div>
          <div class="session-trigger">${s.trigger||'—'}</div>
          <div class="session-stats">
            <div class="session-kv"><span class="session-kv-label">Final</span><span class="session-kv-val ${fc}">${inr(s.final_mtm)}</span></div>
            <div class="session-kv"><span class="session-kv-label">Peak</span><span class="session-kv-val pos">${inr(s.peak_mtm)}</span></div>
          </div>
        </div>
        ${_postExitBlock(s)}
      </div>`;
    }).join('');
  }

  // ── Init ──────────────────────────────────────────────────────────────────
  currentTab = 's1';
  loadStrategyHistory('s1').then(()=>connectSSE());
