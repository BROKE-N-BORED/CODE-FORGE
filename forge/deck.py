"""Phone command deck — Note 17, v2 deck (owner's "build all six").

A small local web surface the Forge serves on the owner's own network
(never a cloud relay): read the conversation, watch the machine, answer
approvals, stop a run, switch modes, and send prompts — from the couch.

v2 additions: [03] conversation view · alive indicator (elapsed timer +
last-sign-of-life, computed on the phone from server timestamps) ·
STOP (kills the turn; releases a parked approval as a denial, the same
rule as the bench's Stop) · RAM/CPU gauges · multi-line prompt box with
quick chips and a mode switcher · honest empty states and the bench's
own [NN] section styling with the B/N/B stamp.

Note 23: the [01] card carries a fire bar — a canvas strip running the
same pixel-fire sim and 8-step palette as the bench ember, in JS:
thinking = full fire, paused = smolder, a turn's end = a few seconds
of gray pixel smoke, idle = cooling embers. Pure decoration: the page
works unchanged if canvas is unsupported.

Project switching (owner's ask, 2026-11-06): a [02] PROJECT card —
a dropdown of the known projects plus a text field for a typed path
or name — and "/project <name-or-path>" typed into the prompt box.
Sections renumbered: STATE 01 · PROJECT 02 · APPROVAL 03 ·
CONVERSATION 04 · SEND 05 · RECEIPTS 06 · ACTIVITY 07. POST /project
is the one endpoint that answers with a JSON body instead of a blind
204 — a failed switch has to be able to say why on the phone.

Prompt delivery (owner's field report, 2026-11-07): POST /prompt now
answers the same way, for the same reason — it was a blind 204 and
the page cleared the prompt box without reading any answer, so the
phone could not show whether a prompt was sent at all. The endpoint
makes the /project-style synchronous round trip to the GUI thread
and answers 200 + JSON: received (the turn started), queued (a turn
is running; it starts when that turn ends), or the failure, which
the SEND card shows on its #promptmsg line. The prompt box clears
only on a delivered answer. Nothing else about the deck changed.

Disconnect honesty (audit A12): tick() used to swallow every failure,
so after a Forge restart (new token — /state answers 403) an open
phone tab showed the last synced state forever, including a "working
for …" timer that grew without bound. tick() now counts consecutive
failures (a thrown fetch OR a non-ok response); after two, the page
shows the #disc banner, clears the alive line and hides STOP, and a
successful tick hides the banner again. The POST handler also guards
its Content-Length parse (garbage earns a 400 instead of a dropped
connection) and answers 404 for unknown POST paths instead of a
blanket 204 that lied about having acted.

Note 12: a [08] PLAN card renders the plan checklist from the
snapshot's "plan" field (status + steps + SKRIBE line). Read-only —
approving from the phone is not in the note.

Plan decisions from the phone (audit C25): the [08] PLAN card was
read-only, so a drafted plan parked the turn at the owner's gate
while the phone could only watch. The card now carries Approve /
Kill buttons, shown only while the synced plan's status is "draft";
POST /plan makes the same synchronous round trip as /prompt and
answers 200 + JSON, and the window answers through the very same
_plan_gate_action the desktop buttons call. Editing the steps
stays at the PC — the phone only releases the gate or kills it.

Item #66: the PLAN card also carries a comment field under the
gate buttons (same draft-only visibility): POST /plan with
{"action": "comment", "comment": "..."} rides the same round
trip — the comment text on the holder, never the desktop's
comment box — and lands in the same _plan_gate_action("comment"),
which settles the gate as a revision (the driver rewrites the
draft against the words and the plan returns to the gate). An
empty comment is refused in the /plan handler itself, in words,
without bothering the GUI thread.

Item 10: a Council chip in the [05] SEND card, beside the mode
switcher — it shows the Reviewer Seat's state and flips it via
POST /council, following /mode's shape exactly. The desktop's
per-turn Council checkbox stays the single source of truth: the
snapshot's "council"/"council_model" fields are read from it (and
from the same reviewer setting the desktop shows), and the endpoint
only asks the window to flip that checkbox — the phone keeps no
council state of its own.

Item #71: the SEND card carries an attachment strip fed by the
snapshot's "attachments" field (name / kind / ≤96px thumbnail
data URI for images — never the full base64 original) — each item
removable via its ✕ through POST /attach-remove, a synchronous
round trip in the /plan style answering 200 + JSON. A file input
on the same card uploads through POST /attach-upload (raw bytes,
the filename on the query string, a 25 MB cap): images join the
bench's pending images, other files land in the project's uploads,
exactly as a desktop attach does. The phone can finally SEE what
is attached — the trap this item cures was an image attached on
the desktop silently riding (and poisoning) later phone turns.

Deck lockdown (S4, audit S1-06/07/15/17/26): the token is a
128-bit secret regenerated on every start() — a link from a
previous deck session dies with that session — compared with
compare_digest, and never written to the app log (the log line
shows host:port only; the full link lives on screen and in the
QR, unpersisted). Downloads answer only for paths on the synced
DOCUMENTS list, and non-upload POST bodies are capped (see
POST_BODY_MAX).

Access is a per-session token in the URL, shown only in the Forge
window. The server is OFF until the owner turns it on.

DECKAPP revamp (2026-11-09, the owner's window rule applied to the
deck itself): the one long page of numbered cards became an app —
a fixed header (wordmark + live status), one screen at a time, and
a bottom tab bar. Nothing was removed; the cards moved, whole,
into screens:
  [04] CONVERSATION + [05] SEND            -> Chat screen
  [03] APPROVAL + [08] PLAN                -> Plan screen (its tab
     carries a badge while an approval is parked or a plan draft
     is waiting)
  [01] STATE + [06] RECEIPTS + [07] ACTIVITY -> Bench screen (its
     tab carries a badge while a turn runs; RECEIPTS and ACTIVITY
     are collapsible sections there)
  [09] DOCUMENTS                           -> Docs screen
  [02] PROJECT                             -> Project screen
The wordmark/stamp and the #disc disconnect banner sit above the
screens, always visible; the SEND card's Mode switcher and Council
chip live in a collapsible MODE & COUNCIL section. Same state flow,
same endpoints, same element ids — page-side reorganization only;
the server below did not change.
"""
from __future__ import annotations
import json
import re
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PySide6.QtCore import QObject, Signal

# Item #71: the /attach-upload size cap. The app itself sets no
# upload limit (a desktop attach reads from disk), so the deck
# draws its own line: a phone photo is a few MB; 25 MB covers any
# honest attachment while keeping a chat payload a chat payload.
# Over the cap the body is never read and nothing is attached.
ATTACH_UPLOAD_MAX = 25 * 1024 * 1024

# S4 (audit S1-17): every OTHER POST is a small JSON control
# message (a prompt, an approval answer, a project name) — none
# has any business being large. A declared Content-Length over
# this cap on a non-upload POST earns a 413 before the body is
# read, so a hung or hostile client cannot make a handler thread
# buffer an unbounded body.
POST_BODY_MAX = 1_048_576

_PAGE_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CODE FORGE — phone deck</title>
<style>body{background:@{surface_base}@;color:@{text_base}@;font-family:system-ui;margin:0;padding:76px 12px 96px}
h1{font-size:16px;color:@{accent_amber}@;letter-spacing:.1em;margin:2px 0 0}
.stamp{color:@{text_muted}@;font-size:11px;letter-spacing:.06em;margin-bottom:10px}
.card{background:@{surface_compose_bottom}@;border:1px solid @{rim}@;border-radius:12px;padding:12px;margin:10px 0}
.sec{color:@{text_section}@;font-size:11px;letter-spacing:.12em;margin-bottom:8px}
button{font-size:15px;padding:11px 16px;border-radius:8px;border:1px solid @{border_button}@;background:@{surface_button_bottom}@;color:@{text_base}@}
button.ok{background:@{accent_primary_bottom}@;border-color:@{accent_amber}@;color:@{text_amber_light}@}
button.stop{background:#571d1d;border-color:#e5534b;color:@{text_white}@}
textarea,select,input{width:100%;box-sizing:border-box;font-size:16px;padding:11px;border-radius:8px;border:1px solid @{border_input}@;background:@{surface_input}@;color:@{text_base}@}
textarea{min-height:64px}
pre{white-space:pre-wrap;font-size:12px;color:@{state_idle}@;margin:4px 0}
.msg{margin:8px 0;padding:8px 10px;border-radius:8px;background:@{surface_input}@;border:1px solid @{surface_splitter}@}
.msg .who{font-size:11px;letter-spacing:.1em;color:@{accent_amber}@}
.msg.assistant .who{color:@{text_section}@}
.msg .body{white-space:pre-wrap;font-size:14px;margin-top:3px}
.bar{height:8px;background:@{surface_input}@;border-radius:4px;overflow:hidden;margin:3px 0 8px}
.bar>div{height:100%;background:@{progress_hot}@}
.bar.hot>div{background:@{progress_crit}@}
.dot{font-size:18px}.amber{color:@{accent_amber}@}.dim{color:@{text_muted}@}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
canvas#firebar{width:100%;height:26px;display:block;background:#141210;border:1px solid #26221e;border-radius:6px;margin-bottom:8px}
@keyframes flash{0%,100%{border-color:@{border_button}@}50%{border-color:@{accent_amber}@}}
.waiting{animation:flash 1.2s infinite}
#appbar{position:fixed;top:0;left:0;right:0;z-index:30;background:@{surface_base:0.94}@;-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);border-bottom:1px solid @{rim_chat}@;padding:9px 14px}
#appbar .brandrow{display:flex;align-items:baseline;flex-wrap:wrap}
#appbar .stamp{margin:0 0 0 8px}
.wordmark{font-size:15px;color:@{accent_amber}@;letter-spacing:.1em;font-weight:700}
.hstatus{margin-top:3px;font-size:13px}
.hstatus .dot{font-size:13px}
#tabbar{position:fixed;left:0;right:0;bottom:0;z-index:30;display:flex;background:@{surface_base:0.96}@;-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);border-top:1px solid @{rim_chat}@;padding:6px 4px;padding-bottom:calc(6px + env(safe-area-inset-bottom))}
#tabbar .tab{flex:1;background:none;border:none;color:@{text_muted}@;font-size:11px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;padding:6px 0 5px;min-height:48px;position:relative;border-radius:8px}
#tabbar .tab .tic{font-size:19px;line-height:1.1}
#tabbar .tab.active{color:@{accent_amber}@}
#tabbar .tab.active::after{content:'';position:absolute;top:-7px;left:24%;right:24%;height:2px;background:@{accent_amber}@;border-radius:2px}
.badge{position:absolute;top:3px;right:calc(50% - 21px);background:#e5534b;color:@{text_white}@;font-size:10px;font-weight:700;min-width:16px;height:16px;line-height:16px;border-radius:8px;text-align:center;padding:0 4px;box-sizing:border-box}
.collaphead{width:100%;display:flex;justify-content:space-between;align-items:center;background:none;border:none;padding:2px 0;color:@{text_base}@;font-size:15px;text-align:left}
.collaphead .sec{margin-bottom:0}
.collap .collapbody{margin-top:8px}
.collap.closed .collapbody{display:none}
.collap .chev{color:@{text_muted}@;font-size:13px;transition:transform .15s}
.collap:not(.closed) .chev{transform:rotate(90deg)}
.sessionbox{margin-top:12px;border-top:1px solid @{surface_splitter}@;padding-top:10px}
.sessionbox .collaphead{font-size:13px;letter-spacing:.08em;color:@{text_section}@}
.attachitem{position:relative;display:inline-block;margin:2px 10px 8px 2px;vertical-align:top}
.attachitem img{width:54px;height:54px;object-fit:cover;border-radius:8px;border:1px solid @{rim}@;display:block}
.attachitem .aname{font-size:11px;color:@{text_muted}@;max-width:76px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:3px}
.attachitem .ax{position:absolute;top:-7px;right:-7px;width:22px;height:22px;padding:0;border-radius:11px;background:#571d1d;border:1px solid #e5534b;color:@{text_white}@;font-size:13px;line-height:1}
.afile{display:inline-flex;align-items:center;background:@{surface_input}@;border:1px solid @{rim}@;border-radius:8px;padding:9px 10px;font-size:13px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
</style></head><body>
<header id="appbar">
<div class="brandrow"><span class="wordmark">CODE FORGE · PHONE DECK</span><span class="stamp">B/N/B STUDIO · EST. 2026</span></div>
<div class="hstatus"><span class="dot amber">●</span> <b id="hstate">…</b> <span class="dim" id="hmodel"></span></div>
</header>
<div class="card" id="disc" style="display:none;border-color:#e5534b;color:@{text_white}@">⚠ Deck disconnected — the Forge restarted or this link expired. Reopen the deck from the Forge to reconnect.</div>

<main>
<section class="screen" id="scr-chat">
<div class="card"><div class="sec">[ 04 ] CONVERSATION</div><div id="convo"><span class="dim">Nothing yet.</span></div></div>

<div class="card"><div class="sec">[ 05 ] SEND</div>
<div id="attachstrip" style="display:none"></div>
<textarea id="prompt" placeholder="Send a prompt to the Forge…"></textarea>
<div class="row" style="margin-top:8px">
<button class="ok" onclick="sendPrompt()">Send</button>
<button onclick="chip('/audit')">/audit</button>
<button onclick="chip('Give me a status summary: current project, what you did last, and what remains.')">Status summary</button>
</div>
<div class="row" style="margin-top:8px">
<button onclick="document.getElementById('attachfile').click()">📎 Attach a file</button>
<input type="file" id="attachfile" style="display:none" onchange="uploadAttach(this)">
</div>
<div id="attachmsg" style="margin-top:8px;font-size:13px"></div>
<div id="promptmsg" style="margin-top:8px;font-size:13px"></div>
<div class="collap closed sessionbox" id="sessionbox">
<button class="collaphead" onclick="toggleCollap('sessionbox')"><span>MODE &amp; COUNCIL</span><span class="chev">▸</span></button>
<div class="collapbody">
<div class="row" style="margin-top:8px"><span class="dim">Mode</span>
<select id="modesel" style="width:auto" onchange="setMode(this.value)">
<option>Code</option><option>Plan</option><option>Design</option><option>Architect</option><option>Review</option><option>Ask</option><option>Write</option><option>Research</option>
</select></div>
<div class="row" style="margin-top:8px"><span class="dim">Council</span>
<button id="councilbtn" onclick="toggleCouncil()">Council: …</button>
<span class="dim" id="councilmodel"></span></div>
</div>
</div>
</div>
</section>

<section class="screen" id="scr-plan" style="display:none">
<div class="card" id="pending" style="display:none">
<div class="sec">[ 03 ] APPROVAL — PAUSED, NO CLOCK RUNNING</div>
<div id="ptool" style="margin:6px 0"></div>
<div id="prisk" style="margin:6px 0"></div>
<pre id="pdesc"></pre>
<div class="row"><button class="ok" onclick="answer(true)">Approve</button>
<button onclick="answer(false)">Deny</button></div>
</div>

<div class="card"><div class="sec">[ 08 ] PLAN</div><pre id="plan">No plan.</pre>
<div class="row" id="planbtns" style="display:none">
<button class="ok" onclick="planAction('approve')">✓ Approve plan</button>
<button class="stop" onclick="planAction('kill')">✗ Kill plan</button>
</div>
<div class="row" id="plancomment" style="display:none">
<input id="plancommenttext" type="text" placeholder="Comment on the draft — the driver revises it" style="flex:1">
<button onclick="planComment()">💬 Comment</button>
</div>
<div id="planmsg" style="margin-top:8px;font-size:13px"></div>
<details id="planfish" style="display:none;margin-top:8px"><summary class="dim" style="cursor:pointer">Goldfish review — tap to read the findings</summary><pre id="planfishtext"></pre></details>
<div class="dim">Approve, comment, or kill from here — editing the steps happens at the PC.</div></div>
</section>

<section class="screen" id="scr-bench" style="display:none">
<div class="card"><div class="sec">[ 01 ] STATE</div>
<canvas id="firebar" height="26"></canvas>
<div><span class="dot amber">●</span> <b id="state">…</b> <span class="dim" id="model"></span>
<span class="dim" id="mode"></span></div>
<div id="alive" style="margin-top:6px"></div>
<div style="margin-top:8px"><span class="dim">RAM</span> <span id="ramtext"></span><div class="bar" id="rambar"><div style="width:0%"></div></div>
<span class="dim">CPU</span> <span id="cptext"></span><div class="bar" id="cpubar"><div style="width:0%"></div></div></div>
<div class="row" style="margin-top:6px"><button class="stop" id="stopbtn" onclick="stopTurn()" style="display:none">■ STOP</button></div>
</div>

<div class="card collap closed" id="receiptsbox">
<button class="collaphead" onclick="toggleCollap('receiptsbox')"><span class="sec">[ 06 ] RECEIPTS</span><span class="chev">▸</span></button>
<div class="collapbody"><pre id="receipts"></pre></div>
</div>

<div class="card collap closed" id="activitybox">
<button class="collaphead" onclick="toggleCollap('activitybox')"><span class="sec">[ 07 ] ACTIVITY</span><span class="chev">▸</span></button>
<div class="collapbody"><pre id="log"></pre></div>
</div>
<div class="card collap closed" id="monitorbox">
<button class="collaphead" onclick="toggleCollap('monitorbox')"><span class="sec">[ 10 ] WATCH — RUN ALERTS</span><span class="chev">▸</span></button>
<div class="collapbody"><pre id="monitor">No alerts — the Watch seat is quiet.</pre></div>
</div>
</section>

<section class="screen" id="scr-docs" style="display:none">
<div class="card"><div class="sec">[ 09 ] DOCUMENTS</div><div id="docs"><span class="dim">No documents yet — plans, designs and other filed documents appear here as downloads.</span></div></div>
</section>

<section class="screen" id="scr-project" style="display:none">
<div class="card"><div class="sec">[ 02 ] PROJECT</div>
<div style="margin-bottom:8px"><span class="dim">Now working in</span> <b id="projname">…</b><br>
<span class="dim" id="projpath" style="font-size:12px"></span></div>
<select id="projsel" onchange="pickProject(this.value)"></select>
<div class="row" style="margin-top:8px">
<input id="projtext" type="text" placeholder="…or type a folder path or project name" style="flex:1;width:auto">
<button onclick="typeProject()">Switch</button>
</div>
<div id="projmsg" style="margin-top:8px;font-size:13px"></div>
</div>
</section>
</main>

<nav id="tabbar">
<button class="tab active" id="tab-chat" onclick="showScreen('chat')"><span class="tic">💬</span><span>Chat</span></button>
<button class="tab" id="tab-plan" onclick="showScreen('plan')"><span class="tic">📋</span><span>Plan</span><span class="badge" id="badge-plan" style="display:none">!</span></button>
<button class="tab" id="tab-bench" onclick="showScreen('bench')"><span class="tic">🔥</span><span>Bench</span><span class="badge" id="badge-bench" style="display:none">●</span></button>
<button class="tab" id="tab-docs" onclick="showScreen('docs')"><span class="tic">⬇</span><span>Docs</span></button>
<button class="tab" id="tab-project" onclick="showScreen('project')"><span class="tic">🗂</span><span>Project</span></button>
</nav>

<script>
const K = new URLSearchParams(location.search).get('k');
let TURN = {active:false, started_at:0, last_activity:0};
let COUNCIL = false;
let FAILS = 0;
let SCREEN = 'chat';
function showScreen(name){
  SCREEN = name;
  const names = ['chat', 'plan', 'bench', 'docs', 'project'];
  for(const n of names){
    document.getElementById('scr-' + n).style.display = (n === name) ? '' : 'none';
    document.getElementById('tab-' + n).className = 'tab' + (n === name ? ' active' : '');
  }
  window.scrollTo(0, 0);
}
function toggleCollap(id){ document.getElementById(id).classList.toggle('closed'); }
function fmt(s){ s=Math.max(0,Math.floor(s)); const m=Math.floor(s/60); return (m? m+'m ':'') + (s%60) + 's'; }
function tickAlive(){
  const el = document.getElementById('alive');
  if(TURN.active){
    const now = Date.now()/1000;
    el.innerHTML = '⏱ working for <b>' + fmt(now - TURN.started_at) + '</b> · last sign of life ' + fmt(now - TURN.last_activity) + ' ago';
  } else el.textContent = '';
  document.getElementById('stopbtn').style.display = TURN.active ? '' : 'none';
}
async function tick(){
  try{
    const r = await fetch('/state?k='+K);
    if(!r.ok) throw new Error('state request failed: ' + r.status);
    const s = await r.json();
    FAILS = 0;
    document.getElementById('disc').style.display = 'none';
    TURN = s.turn || TURN;
    driveFire(s);
    document.getElementById('state').textContent = s.agent_state;
    document.getElementById('hstate').textContent = s.agent_state;
    document.getElementById('hmodel').textContent = s.model ? '· ' + s.model : '';
    document.getElementById('model').textContent = s.model ? '· '+s.model : '';
    document.getElementById('mode').textContent = s.mode ? '· mode: '+s.mode : '';
    const ms = document.getElementById('modesel');
    if(s.mode && ms.value.toLowerCase() !== s.mode) ms.value = s.mode[0].toUpperCase()+s.mode.slice(1);
    COUNCIL = !!s.council;
    const cbtn = document.getElementById('councilbtn');
    cbtn.textContent = 'Council: ' + (COUNCIL ? 'ON' : 'OFF');
    cbtn.className = COUNCIL ? 'ok' : '';
    document.getElementById('councilmodel').textContent = s.council_model ? 'reviewer · '+s.council_model : '';
    renderProjects(s);
    document.getElementById('ramtext').textContent = s.gauges.ram_text || '';
    document.getElementById('cptext').textContent = s.gauges.cpu_text || '';
    const rb = document.getElementById('rambar'); rb.firstElementChild.style.width = (s.gauges.ram_pct||0)+'%'; rb.className = 'bar' + (s.gauges.ram_pct>=80 ? ' hot' : '');
    const cb = document.getElementById('cpubar'); cb.firstElementChild.style.width = (s.gauges.cpu_pct||0)+'%'; cb.className = 'bar' + (s.gauges.cpu_pct>=85 ? ' hot' : '');
    const p = document.getElementById('pending');
    if(s.pending){ p.style.display='block'; p.classList.add('waiting');
      document.getElementById('ptool').textContent = s.pending.tool + ' → ' + s.pending.target;
      document.getElementById('prisk').textContent = s.pending.risk || '';
      document.getElementById('pdesc').textContent = s.pending.description; }
    else { p.style.display='none'; p.classList.remove('waiting'); }
    const convo = document.getElementById('convo'); convo.innerHTML='';
    if(!s.conversation.length){ convo.innerHTML = '<span class="dim">Nothing yet.</span>'; }
    for(const m of s.conversation){
      const d = document.createElement('div'); d.className = 'msg ' + m.role;
      const w = document.createElement('div'); w.className = 'who'; w.textContent = m.role === 'user' ? 'YOU' : 'FORGE';
      const b = document.createElement('div'); b.className = 'body'; b.textContent = m.text;
      d.appendChild(w); d.appendChild(b); convo.appendChild(d);
    }
    document.getElementById('receipts').textContent = s.receipts.length ? s.receipts.join('\\n') : 'No tool calls yet — receipts appear here when the agent uses a tool.';
    document.getElementById('log').textContent = s.log.join('\\n');
    renderPlan(s);
    renderDocs(s);
    renderAttach(s);
    renderMonitor(s);
    document.getElementById('badge-plan').style.display = (s.pending || (s.plan && s.plan.status === 'draft')) ? '' : 'none';
    document.getElementById('badge-bench').style.display = TURN.active ? '' : 'none';
    tickAlive();
  }catch(e){
    FAILS += 1;
    if(FAILS >= 2){
      document.getElementById('disc').style.display = 'block';
      TURN = {active:false, started_at:0, last_activity:0};
      tickAlive();
    }
  }
}
async function post(path, data){ await fetch(path+'?k='+K, {method:'POST', body: JSON.stringify(data||{})}); }
async function answer(ok){ await post('/approve', {answer: ok}); tick(); }
async function stopTurn(){ await post('/stop', {}); tick(); }
async function setMode(v){ await post('/mode', {mode: v.toLowerCase()}); tick(); }
async function toggleCouncil(){ await post('/council', {on: !COUNCIL}); tick(); }
function renderMonitor(s){
  const el = document.getElementById('monitor'); if(!el) return;
  const m = s.monitor || {alerts: [], watching: false};
  const alerts = m.alerts || [];
  if(!alerts.length){ el.textContent = m.watching ? 'Watching this run — no alerts yet.' : 'No alerts — the Watch seat is quiet.'; return; }
  el.textContent = alerts.map(a => (a.when || '') + ' · ' + a.kind + ' · ' + a.detail + (a.count > 1 ? ' (x' + a.count + ')' : '') + '\\n  ' + (a.evidence || '')).join('\\n');
}
function renderDocs(s){
  const box = document.getElementById('docs'); if(!box) return;
  const docs = s.documents || [];
  box.innerHTML = '';
  if(!docs.length){ box.innerHTML = '<span class="dim">No documents yet — plans, designs and other filed documents appear here as downloads.</span>'; return; }
  for(const d of docs){
    const row = document.createElement('div'); row.style.margin = '6px 0';
    const a = document.createElement('a');
    a.href = '/dl?k=' + K + '&path=' + encodeURIComponent(d.path);
    a.textContent = '⬇ ' + d.name;
    a.style.color = '@{accent_amber}@';
    row.appendChild(a); box.appendChild(row);
  }
}
function renderPlan(s){
  const el = document.getElementById('plan'), p = s.plan;
  const btns = document.getElementById('planbtns');
  const crow = document.getElementById('plancomment');
  const fish = document.getElementById('planfish');
  if(!p || !p.steps || !p.steps.length){ el.textContent = 'No plan.'; btns.style.display = 'none'; crow.style.display = 'none'; fish.style.display = 'none'; return; }
  const mark = {done:'✓', active:'▸', pending:'·', interrupted:'✗'};
  const lines = ['Status: ' + (p.status || '')];
  p.steps.forEach(function(st, i){ lines.push((mark[st.state] || '·') + ' ' + (i + 1) + '. ' + st.text); });
  if(p.success_criteria) lines.push('Success criteria:\\n' + p.success_criteria);
  if(p.out_of_scope) lines.push('Out of scope:\\n' + p.out_of_scope);
  if(p.skribe) lines.push('SKRIBE: ' + p.skribe);
  if(p.goldfish) lines.push(p.goldfish);
  el.textContent = lines.join('\\n');
  if(p.goldfish_text){ document.getElementById('planfishtext').textContent = p.goldfish_text; fish.style.display = ''; }
  else { fish.style.display = 'none'; }
  btns.style.display = (p.status === 'draft') ? '' : 'none';
  crow.style.display = (p.status === 'draft') ? '' : 'none';
}
async function planAction(action){
  const msg = document.getElementById('planmsg');
  try{
    const r = await fetch('/plan?k='+K, {method:'POST', body: JSON.stringify({action: action})});
    const d = await r.json();
    if(d.ok){ msg.style.color = '@{text_amber_light}@'; msg.textContent = '✓ ' + (d.message || 'Done'); }
    else { msg.style.color = '#e5534b'; msg.textContent = '✗ ' + (d.error || 'Plan action failed'); }
  }catch(e){ msg.style.color = '#e5534b'; msg.textContent = '✗ Deck request failed'; }
  tick();
}
async function planComment(){
  const msg = document.getElementById('planmsg');
  const box = document.getElementById('plancommenttext');
  const text = (box.value || '').trim();
  if(!text){ msg.style.color = '#e5534b'; msg.textContent = '✗ Type the change you want first — an empty comment cannot revise the plan.'; return; }
  try{
    const r = await fetch('/plan?k='+K, {method:'POST', body: JSON.stringify({action: 'comment', comment: text})});
    const d = await r.json();
    if(d.ok){ box.value = ''; msg.style.color = '@{text_amber_light}@'; msg.textContent = '✓ ' + (d.message || 'Done'); }
    else { msg.style.color = '#e5534b'; msg.textContent = '✗ ' + (d.error || 'Plan action failed'); }
  }catch(e){ msg.style.color = '#e5534b'; msg.textContent = '✗ Deck request failed'; }
  tick();
}
function renderAttach(s){
  const strip = document.getElementById('attachstrip');
  if(!strip) return;
  const items = s.attachments || [];
  strip.innerHTML = '';
  if(!items.length){ strip.style.display = 'none'; return; }
  strip.style.display = '';
  items.forEach(function(a, i){
    const wrap = document.createElement('span'); wrap.className = 'attachitem';
    if(a.kind === 'image' && a.thumb){
      const img = document.createElement('img'); img.src = a.thumb; img.alt = a.name || '';
      wrap.appendChild(img);
      const nm = document.createElement('div'); nm.className = 'aname'; nm.textContent = a.name || '';
      wrap.appendChild(nm);
    } else {
      const chip = document.createElement('span'); chip.className = 'afile';
      chip.textContent = (a.kind === 'image' ? '🖼 ' : '📄 ') + (a.name || '');
      wrap.appendChild(chip);
    }
    const x = document.createElement('button'); x.className = 'ax'; x.textContent = '✕';
    x.title = 'Remove ' + (a.name || 'attachment');
    x.onclick = function(){ removeAttach(i); };
    wrap.appendChild(x);
    strip.appendChild(wrap);
  });
}
async function removeAttach(i){
  const msg = document.getElementById('attachmsg');
  try{
    const r = await fetch('/attach-remove?k='+K, {method:'POST', body: JSON.stringify({index: i})});
    const d = await r.json();
    if(d.ok){ msg.style.color = '@{text_amber_light}@'; msg.textContent = '✓ ' + (d.message || 'Removed'); }
    else { msg.style.color = '#e5534b'; msg.textContent = '✗ ' + (d.error || 'Remove failed'); }
  }catch(e){ msg.style.color = '#e5534b'; msg.textContent = '✗ Deck request failed'; }
  tick();
}
async function uploadAttach(input){
  const msg = document.getElementById('attachmsg');
  const f = input.files && input.files[0];
  input.value = '';
  if(!f) return;
  msg.style.color = '@{text_amber_light}@'; msg.textContent = 'Attaching ' + f.name + '…';
  try{
    const r = await fetch('/attach-upload?k='+K+'&name='+encodeURIComponent(f.name), {method:'POST', body: f});
    const d = await r.json();
    if(d.ok){ msg.style.color = '@{text_amber_light}@'; msg.textContent = '✓ ' + (d.message || 'Attached'); }
    else { msg.style.color = '#e5534b'; msg.textContent = '✗ ' + (d.error || 'Attach failed'); }
  }catch(e){ msg.style.color = '#e5534b'; msg.textContent = '✗ Deck request failed — nothing was attached'; }
  tick();
}
function renderProjects(s){
  const proj = s.project || {}, list = s.projects || [], cur = proj.path || '';
  document.getElementById('projname').textContent = proj.name || '—';
  document.getElementById('projpath').textContent = cur;
  const sel = document.getElementById('projsel');
  const sig = list.map(p => p.path).join('|') + '::' + cur;
  if(sel.dataset.sig === sig) return;  // don't clobber a selection in progress
  sel.dataset.sig = sig; sel.innerHTML = '';
  if(!list.length){ const o = document.createElement('option'); o.value=''; o.textContent='(no projects yet — type a path below)'; sel.appendChild(o); }
  for(const p of list){
    const o = document.createElement('option'); o.value = p.path; o.textContent = p.name;
    if(p.path === cur) o.selected = true;
    sel.appendChild(o);
  }
}
async function postProject(target){
  const msg = document.getElementById('projmsg');
  try{
    const r = await fetch('/project?k='+K, {method:'POST', body: JSON.stringify({project: target})});
    const d = await r.json();
    if(d.ok){ msg.style.color = '@{text_amber_light}@'; msg.textContent = '✓ ' + (d.message || ('Project: ' + (d.project || target))); }
    else { msg.style.color = '#e5534b'; msg.textContent = '✗ ' + (d.error || 'Switch failed'); }
  }catch(e){ msg.style.color = '#e5534b'; msg.textContent = '✗ Deck request failed'; }
  tick();
}
async function pickProject(v){ if(v) await postProject(v); }
async function typeProject(){ const el = document.getElementById('projtext'); const v = el.value.trim(); if(!v) return; await postProject(v); el.value=''; }
async function postPrompt(text){
  const msg = document.getElementById('promptmsg');
  try{
    const r = await fetch('/prompt?k='+K, {method:'POST', body: JSON.stringify({text: text})});
    const d = await r.json();
    if(d.ok){ msg.style.color = '@{text_amber_light}@'; msg.textContent = '✓ ' + (d.message || 'Sent'); return true; }
    msg.style.color = '#e5534b'; msg.textContent = '✗ ' + (d.error || 'Not sent'); return false;
  }catch(e){ msg.style.color = '#e5534b'; msg.textContent = '✗ Deck request failed — the prompt was NOT sent'; return false; }
}
async function sendPrompt(){ const el = document.getElementById('prompt'); const v = el.value; if(!v.trim()) return; if(await postPrompt(v)) el.value=''; tick(); }
async function chip(t){ await postPrompt(t); tick(); }
// ---- fire bar (Note 23): the bench ember's sim + palette, in JS ----
const FIRE_PALETTE = ['#200d05','#400f03','#6e1704','#a52c06','#d95d0b','#f28c13','#ffc92e','#fff0a8'];
const FIRE_COALS = [[0.08,0.0],[0.19,1.3],[0.30,2.6],[0.41,0.7],[0.52,3.9],[0.63,5.1],[0.74,4.4],[0.85,2.0],[0.94,5.8]];
const FC = 220, FR = 8, FCELL = 1.5;  // fine grid, mirroring the bench (same bed, half-size cells)
let fireGrid = [], fireHeat = 0.10, fireMode = 'cool', firePhase = 0, smokePuffs = [], smokeUntil = 0, wasBusy = false;
for(let r = 0; r < FR; r++) fireGrid.push(new Array(FC).fill(0));
function driveFire(s){
  const st = (s.agent_state || '').toUpperCase();
  const active = (s.turn && s.turn.active) || st.indexOf('THINKING') >= 0;
  const paused = !!s.pending || st.indexOf('PAUSED') >= 0;
  if(active && !paused){ fireMode = 'full'; fireHeat = Math.min(1, fireHeat + 0.10); }
  else if(paused){ fireMode = 'paused'; fireHeat = Math.max(fireHeat, 0.85); }
  else {
    if(wasBusy && fireMode !== 'smoke'){ fireMode = 'smoke'; smokeUntil = Date.now() + 4000; }
    if(fireMode === 'smoke' && Date.now() > smokeUntil) fireMode = 'cool';
    if(fireMode !== 'smoke') fireMode = 'cool';
    fireHeat = Math.max(0.10, fireHeat * 0.94);  // settle to faint embers
  }
  wasBusy = active || paused;
}
function stepFire(){
  firePhase += 0.09;
  let intensity = fireHeat;
  if(fireMode === 'paused') intensity = Math.min(intensity, 0.55);  // smolder cap
  const old = fireGrid.map(row => row.slice());
  const bottom = fireGrid[FR - 1];
  for(let c = 0; c < FC; c++){
    const frac = c / (FC - 1);
    let best = 0, total = 0;
    for(const coal of FIRE_COALS){
      const fall = Math.max(0, 1 - Math.abs(frac - coal[0]) / 0.09);
      const pulse = 0.62 + 0.38 * Math.sin(firePhase * 1.7 + coal[1]);
      best = Math.max(best, fall * pulse); total += fall * pulse;
    }
    const source = Math.min(1, best + total * 0.35);
    let v = source * intensity * (0.88 + 0.28 * Math.random());
    if(Math.random() < 0.03) v = Math.max(v, intensity * (0.6 + 0.4 * Math.random()));
    bottom[c] = Math.max(0, Math.min(1, v));
  }
  for(let r = FR - 2; r >= 0; r--){
    const below = old[r + 1];
    for(let c = 0; c < FC; c++){
      let src = c + Math.floor(Math.random() * 3) - 1;
      src = src < 0 ? 0 : (src >= FC ? FC - 1 : src);
      fireGrid[r][c] = below[src] * (0.85 + 0.11 * Math.random());  // retuned for 8 rows: same flame height
    }
  }
  if(fireMode === 'smoke' && Math.random() < 0.55)
    smokePuffs.push({col: 15 + Math.random() * (FC - 30), yoff: 0, life: 1.0});
  smokePuffs = smokePuffs.filter(p => p.life > 0.05);
  for(const p of smokePuffs){ p.yoff -= 1.3; p.col += (Math.random() - 0.5) * 0.8; p.life -= 0.045; }
}
function drawFire(){
  const cv = document.getElementById('firebar');
  if(!cv || !cv.getContext) return;
  const ctx = cv.getContext('2d');
  if(!ctx) return;
  const W = cv.clientWidth || 320;
  if(cv.width !== W) cv.width = W;
  const H = cv.height;
  ctx.fillStyle = '#141210'; ctx.fillRect(0, 0, W, H);
  const cw = W / FC, bedY = H - FR * FCELL - 2;
  for(let r = 0; r < FR; r++) for(let c = 0; c < FC; c++){
    const v = fireGrid[r][c];
    if(v <= 0.02) continue;
    let idx = Math.floor(Math.pow(v, 0.75) * 8);
    idx = idx < 0 ? 0 : (idx > 7 ? 7 : idx);
    ctx.fillStyle = FIRE_PALETTE[idx];
    ctx.fillRect(Math.floor(c * cw), bedY + r * FCELL, Math.ceil(cw), FCELL);
  }
  for(const p of smokePuffs){
    ctx.fillStyle = 'rgba(150,155,165,' + (0.55 * Math.max(0, p.life)).toFixed(3) + ')';
    ctx.fillRect(Math.floor(p.col * cw), bedY + Math.floor(p.yoff), Math.ceil(cw), FCELL);
  }
}
setInterval(() => { if (document.hidden) return; stepFire(); drawFire(); }, 50);
showScreen('chat');
tick(); setInterval(tick, 3000); setInterval(tickAlive, 1000);
</script></body></html>"""

# U1 (UI review, 2026-11-07): the page above is a TEMPLATE. Every UI
# color in it is an @{token}@ placeholder (@{token:alpha}@ for an
# rgba() wash over a token's hex), and forge.main builds the served
# PAGE from the same TOKENS table the desktop stylesheet is generated
# from, so the two surfaces cannot drift apart again. PAGE stays the
# raw template until main binds the built page (main imports this
# module before its TOKENS table exists, so the build cannot happen
# at deck import time). The only literals left in the template are
# the disclosed exceptions pinned in tests' DeckTokenTests: the
# desktop's established error red (#e5534b — RunBlock.ERROR_RED in
# main.py, a named literal, not a TOKENS entry), the danger-button
# background (#571d1d — no TOKENS surface carries a red tint), and
# the ember simulation's painted palette (the FIRE_PALETTE ramp, the
# coal-bed tones #141210/#26221e and the smoke rgb(150,155,165) —
# mirrored literally by the desktop's own ember painter; painted
# simulation colors, never stylesheet colors on either surface).
_TOKEN_PLACEHOLDER = re.compile(r"@\{([a-z_0-9]+)(?::([0-9.]+))?\}@")


def _rgba(hex_color: str, alpha: str) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def build_page(tokens) -> str:
    """Substitute a TOKENS table into the deck page template.

    A placeholder naming a token the table does not hold raises
    KeyError here — at build time (app start / test import) — rather
    than serving a silently wrong color."""
    def _sub(match):
        value = tokens[match.group(1)]
        if match.group(2) is not None:
            return _rgba(value, match.group(2))
        return value
    return _TOKEN_PLACEHOLDER.sub(_sub, _PAGE_TEMPLATE)


PAGE = _PAGE_TEMPLATE

def resolve_project(target: str, projects: list[dict]):
    """Resolve a deck project target to a folder path.

    Returns (path, None) on success, (None, error) on failure. An exact
    project-name match (case-insensitive) against the known projects
    wins; anything else is treated as a filesystem path, which must
    exist and be a directory.
    """
    target = (target or "").strip()
    if not target:
        return None, "No project given — pick one from the list or type a name or folder path."
    lowered = target.lower()
    for entry in projects or []:
        if str(entry.get("name", "")).lower() == lowered and entry.get("path"):
            return str(entry["path"]), None
    from pathlib import Path
    candidate = Path(target).expanduser()
    if candidate.is_dir():
        return str(candidate), None
    if candidate.exists():
        return None, f"Not a folder: {target}"
    return None, f"No project named '{target}' and no such folder: {target}"


def lan_ip() -> str:
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        # Sweep R111: the failure path used to leak the socket (the
        # url property calls this per access, so a flaky network
        # leaked one fd per call). The per-access probe itself
        # stays — a cached address goes stale when the machine
        # changes networks, which is exactly when the deck is used.
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


class DeckError(Exception):
    """The deck could not start (Sweep R107) — a plain-words reason
    the bench logs instead of a raw OSError traceback escaping the
    deck toggle."""


class DeckServer(QObject):
    approval_answered = Signal(bool)
    stop_requested = Signal()
    mode_changed = Signal(str)
    council_changed = Signal(bool)
    # (target, holder) — the HTTP thread blocks on holder["event"] while
    # the GUI thread performs the switch and fills holder["result"] with
    # (ok, message). /project is the deck's one synchronous round trip.
    project_switch_requested = Signal(str, object)
    # Same shape for /prompt (2026-11-07): the GUI thread accepts or
    # queues the prompt and fills holder["result"] with (ok, message),
    # so the phone gets a real delivered / queued / failed answer.
    prompt_delivery_requested = Signal(str, object)
    # Same shape for /plan (audit C25): the GUI thread decides the
    # parked plan's approve/kill and fills holder["result"] with
    # (ok, message), so the phone shows whether its decision landed.
    plan_action_requested = Signal(str, object)
    # Same shape for /attach-remove (item #71): the GUI thread
    # removes ONE pending attachment by strip index and fills
    # holder["result"] with (ok, message).
    attachment_remove_requested = Signal(int, object)
    # Same shape for /attach-upload (item #71): (name, raw bytes,
    # holder) — the GUI thread ingests the file into the bench's
    # pending attachments and fills holder["result"].
    attachment_upload_requested = Signal(str, object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        # S4 (audit S1-06/26): 128-bit token. This initial value is
        # only a pre-start placeholder — start() regenerates it for
        # every deck session and stop() replaces it again, so no
        # token outlives the session it was shown in.
        self.token = secrets.token_urlsafe(16)
        self._lock = threading.Lock()
        self._state = {"agent_state": "IDLE", "model": "", "mode": "code",
                       "council": False, "council_model": "",
                       "pending": None, "receipts": [], "log": [],
                       "conversation": [], "documents": [],
                       "attachments": [],
                       "project": {"name": "", "path": ""}, "projects": [],
                       "turn": {"active": False, "started_at": 0, "last_activity": 0},
                       "gauges": {"ram_pct": 0, "ram_text": "", "cpu_pct": 0, "cpu_text": ""}}
        self._server: ThreadingHTTPServer | None = None
        self.port = 8765

    # ---- state pushed from the GUI thread ----
    def set_state(self, **fields):
        with self._lock:
            self._state.update(fields)

    def push_receipt(self, line: str):
        with self._lock:
            self._state["receipts"] = (self._state["receipts"] + [line])[-8:]

    def push_log(self, lines: list[str]):
        with self._lock:
            self._state["log"] = list(lines)[-12:]

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def council_on(self) -> bool:
        """The last synced council state, read under the state lock
        (Sweep R105: the bare-POST toggle in the handler used to read
        _state unlocked, racing the GUI thread's set_state)."""
        with self._lock:
            return bool(self._state.get("council", False))

    def download(self, query: str):
        """Serve one project file to the phone as a download
        (2026-11-09: the owner asked for "a copy of the document
        as a link" — the deck could show chat text but could not
        hand over a file). The path is the project-relative path
        the documents list carries; it is resolved against the
        active project and CONFINED to it — anything escaping the
        project root, missing, or not a plain file answers None
        and the handler replies 404. Returns (filename, bytes).

        S4 (audit S1-15): confinement alone let a token holder fetch
        ANY file in the project by guessing its path (source files,
        .forge/scratchpad.md — deliberately kept off the list). The
        resolved path must now also be ON the synced documents
        list: membership is tested on the RESOLVED path, after the
        confinement check, so a symlink wearing a listed name
        cannot smuggle the phone to an unlisted target."""
        from pathlib import PurePosixPath
        from urllib.parse import parse_qs
        rel = (parse_qs(query).get("path") or [""])[0]
        with self._lock:
            root = self._state.get("project", {}).get("path", "")
            listed = {str(d.get("path", ""))
                      for d in self._state.get("documents", []) or []}
        if not rel or not root:
            return None
        try:
            base = Path(root).resolve()
            target = (base / rel).resolve()
            if target != base and base not in target.parents:
                return None
            listed_posix = {PurePosixPath(p).as_posix()
                            for p in listed if p}
            if target.relative_to(base).as_posix() not in listed_posix:
                return None
            if not target.is_file():
                return None
            return target.name, target.read_bytes()
        except (OSError, ValueError):
            return None

    def _await_result(self, holder, timeout: float = 15):
        """(answered, result) for one blocking round trip. Sweep
        R101: on timeout the holder is stamped EXPIRED — Qt may
        still deliver the queued signal to the GUI thread later,
        into a bench whose state has moved on, and the GUI slots
        check the stamp first and drop a stale delivery instead of
        acting on it (a plan approved after the phone was told it
        failed; a prompt landing in a different project)."""
        if not holder["event"].wait(timeout=timeout):
            holder["expired"] = True
            return False, None
        return True, holder["result"]

    def request_project_switch(self, target: str) -> tuple[bool, str]:
        """Ask the GUI thread to switch projects and wait for its answer.

        Returns (True, message) or (False, error). The 15s ceiling only
        guards a wedged GUI loop; a healthy bench answers in milliseconds.
        """
        holder = {"event": threading.Event(), "result": None}
        self.project_switch_requested.emit(str(target), holder)
        answered, result = self._await_result(holder)
        if not answered:
            return False, "The bench didn't answer — is the Forge window responsive?"
        ok, message = result or (False, "Switch failed.")
        return bool(ok), str(message)

    def request_prompt_delivery(self, text: str) -> tuple[bool, str]:
        """Ask the GUI thread to take a phone prompt and wait for its
        answer: (True, "Received…") when the turn started, (True,
        "Queued…") when a running turn holds it for the next round,
        (False, why) when the bench refused or never answered. The
        15s ceiling only guards a wedged GUI loop, exactly like the
        project switch's."""
        holder = {"event": threading.Event(), "result": None}
        self.prompt_delivery_requested.emit(str(text), holder)
        answered, result = self._await_result(holder)
        if not answered:
            return False, "The bench didn't answer — is the Forge window responsive?"
        ok, message = result or (False, "Prompt failed.")
        return bool(ok), str(message)

    def request_plan_action(self, action: str) -> tuple[bool, str]:
        """Ask the GUI thread to approve or kill the parked plan and
        wait for its answer: (True, what happened) when the gate took
        the decision, (False, why) when no plan was waiting or the
        bench never answered. The 15s ceiling only guards a wedged
        GUI loop, exactly like the prompt delivery's."""
        holder = {"event": threading.Event(), "result": None}
        self.plan_action_requested.emit(str(action), holder)
        answered, result = self._await_result(holder)
        if not answered:
            return False, "The bench didn't answer — is the Forge window responsive?"
        ok, message = result or (False, "Plan action failed.")
        return bool(ok), str(message)

    def request_plan_comment(self, comment: str) -> tuple[bool, str]:
        """Ask the GUI thread to settle the parked plan's gate as a
        revision against the owner's comment (Item #66) and wait
        for its answer. Same signal and round trip as the plan
        action's — the comment rides the HOLDER, so the signal's
        shape (and every existing slot) is unchanged; the window's
        slot reads holder["comment"] for the "comment" action."""
        holder = {"event": threading.Event(), "result": None,
                  "comment": str(comment)}
        self.plan_action_requested.emit("comment", holder)
        answered, result = self._await_result(holder)
        if not answered:
            return False, "The bench didn't answer — is the Forge window responsive?"
        ok, message = result or (False, "Plan action failed.")
        return bool(ok), str(message)

    def request_attachment_remove(self, index) -> tuple[bool, str]:
        """Ask the GUI thread to remove one pending attachment by
        its strip index and wait for its answer: (True, what was
        removed) or (False, why). The 15s ceiling only guards a
        wedged GUI loop, exactly like the plan action's."""
        holder = {"event": threading.Event(), "result": None}
        self.attachment_remove_requested.emit(int(index), holder)
        answered, result = self._await_result(holder)
        if not answered:
            return False, "The bench didn't answer — is the Forge window responsive?"
        ok, message = result or (False, "Remove failed.")
        return bool(ok), str(message)

    def request_attachment_upload(self, name, data) -> tuple[bool, str]:
        """Ask the GUI thread to ingest one uploaded file (name +
        raw bytes) into the bench's pending attachments and wait
        for its answer: (True, where it landed) or (False, why).
        The 15s ceiling only guards a wedged GUI loop, exactly
        like the plan action's."""
        holder = {"event": threading.Event(), "result": None}
        self.attachment_upload_requested.emit(str(name), bytes(data), holder)
        answered, result = self._await_result(holder)
        if not answered:
            return False, "The bench didn't answer — is the Forge window responsive?"
        ok, message = result or (False, "Attach failed.")
        return bool(ok), str(message)

    @property
    def url(self) -> str:
        return f"http://{lan_ip()}:{self.port}/?k={self.token}"

    # ---- lifecycle ----
    def start(self):
        if self._server:
            return
        # S4 (audit S1-06): a FRESH token for every deck session,
        # minted before the server accepts a single request — a
        # bookmark, QR photo or log line from a previous session
        # carries a dead key. Callers read deck.url only after
        # start() returns, so what the panel and QR show is always
        # this session's token.
        self.token = secrets.token_urlsafe(16)
        deck = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _ok_token(self) -> bool:
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(self.path).query)
                given = (qs.get("k") or [""])[0]
                # S4 (audit S1-26): constant-time comparison, on
                # bytes so a non-ASCII guess cannot raise here.
                return secrets.compare_digest(
                    given.encode("utf-8"),
                    str(deck.token).encode("utf-8"))

            def do_GET(self):
                from urllib.parse import urlparse
                if not self._ok_token():
                    self.send_response(403); self.end_headers(); return
                path = urlparse(self.path).path
                if path == "/dl":
                    got = deck.download(urlparse(self.path).query)
                    if got is None:
                        self.send_response(404); self.end_headers(); return
                    filename, body = got
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header(
                        "Content-Disposition",
                        f'attachment; filename="{filename}"')
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path == "/state":
                    body = json.dumps(deck.snapshot()).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    # Sweep R110: no-store on the state feed and the
                    # page — a phone browser must never serve a stale
                    # cached state (or a stale page across a Forge
                    # update) as if it were live.
                    self.send_header("Cache-Control", "no-store")
                elif path == "/":
                    body = PAGE.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                else:
                    # Sweep R110: unknown GET paths used to fall
                    # through to the PAGE with a 200 — a typo'd or
                    # probed URL looked exactly like the deck. 404
                    # says nothing is here.
                    self.send_response(404); self.end_headers(); return
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                from urllib.parse import urlparse
                if not self._ok_token():
                    self.send_response(403); self.end_headers(); return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    if length < 0:
                        raise ValueError("negative Content-Length")
                except (TypeError, ValueError):
                    # A garbage length used to raise here and drop the
                    # connection with no answer at all — say 400 instead.
                    self.send_response(400); self.end_headers(); return
                path = urlparse(self.path).path
                if path != "/attach-upload" and length > POST_BODY_MAX:
                    # S4 (audit S1-17): refuse BEFORE the body is
                    # read — the JSON control endpoints never carry
                    # anything near this size. /attach-upload keeps
                    # its own 25 MB rule below, untouched.
                    self.send_response(413); self.end_headers(); return

                def _answer(ok, message):
                    # The /project family's answer shape: 200 + JSON,
                    # so the phone can show what actually happened.
                    payload = {"ok": True, "message": message} if ok \
                        else {"ok": False, "error": message}
                    body = json.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                if path == "/attach-upload":
                    # Item #71: the body is the FILE ITSELF, raw
                    # bytes — not JSON like the family above. The
                    # filename rides the query string. Over the cap
                    # the body is never read and nothing attaches.
                    if length > ATTACH_UPLOAD_MAX:
                        _answer(False,
                                "That file is over the deck's 25 MB "
                                "attachment limit — nothing was attached.")
                        return
                    from urllib.parse import parse_qs
                    qs = parse_qs(urlparse(self.path).query)
                    name = (qs.get("name") or ["attachment"])[0]
                    raw = self.rfile.read(length) if length else b""
                    ok, message = deck.request_attachment_upload(name, raw)
                    _answer(ok, message)
                    return
                try:
                    data = json.loads(self.rfile.read(length) or b"{}")
                    bad_body = not isinstance(data, dict)
                    if bad_body:
                        data = {}
                except Exception:
                    # Sweep R102: a garbled body used to be silently
                    # replaced with {} — which DENIED a pending
                    # approval on /approve, STOPPED the turn on /stop,
                    # and FLIPPED the council on a bare /council. The
                    # JSON family below refuses safely from an empty
                    # body (nothing is acted on); the blind signal
                    # paths check bad_body and answer 400, emitting
                    # nothing.
                    data = {}
                    bad_body = True
                if path == "/attach-remove":
                    # Item #71: like /plan — a synchronous round
                    # trip, answered 200 + JSON. A missing or bogus
                    # index is refused here, without bothering the
                    # GUI thread.
                    try:
                        index = int(data.get("index"))
                    except (TypeError, ValueError):
                        ok, message = False, "No attachment index given."
                    else:
                        ok, message = deck.request_attachment_remove(index)
                    _answer(ok, message)
                    return
                if path == "/project":
                    # Deliberate deviation from the blind 204s: this one
                    # answers 200 + JSON so the phone can show WHY a
                    # switch failed (bad path, mid-turn, unknown name).
                    target = str(data.get("project") or data.get("path")
                                 or data.get("target") or "")
                    ok, message = deck.request_project_switch(target)
                    payload = {"ok": True, "message": message} if ok \
                        else {"ok": False, "error": message}
                    body = json.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path == "/prompt":
                    # Like /project (and unlike the blind 204s below):
                    # a synchronous round trip, answered 200 + JSON —
                    # the phone must be able to show whether its
                    # prompt was received, queued, or refused.
                    text = str(data.get("text", "")).strip()
                    if not text:
                        ok, message = False, "Empty prompt — nothing to send."
                    else:
                        ok, message = deck.request_prompt_delivery(text)
                    payload = {"ok": True, "message": message} if ok \
                        else {"ok": False, "error": message}
                    body = json.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path == "/plan":
                    # Like /prompt (audit C25): a synchronous round
                    # trip, answered 200 + JSON — the phone must be
                    # able to show whether its plan decision actually
                    # released the gate. Unknown actions are refused
                    # here, without ever bothering the GUI thread.
                    # Item #66 adds "comment": the owner's words ride
                    # the body, and an empty comment is refused here
                    # in words, GUI thread unbothered.
                    action = str(data.get("action", "")).strip().lower()
                    if action == "comment":
                        comment = str(data.get("comment", "")).strip()
                        if not comment:
                            ok, message = False, (
                                "Type the change you want first — an "
                                "empty comment can't revise the plan.")
                        else:
                            ok, message = deck.request_plan_comment(comment)
                    elif action not in ("approve", "kill"):
                        ok, message = False, "Unknown plan action."
                    else:
                        ok, message = deck.request_plan_action(action)
                    payload = {"ok": True, "message": message} if ok \
                        else {"ok": False, "error": message}
                    body = json.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if bad_body:
                    # Sweep R102: an unparseable (or non-object) body
                    # acts on nothing — 400, no signal emitted. Only
                    # the blind-204 signal paths reach this check;
                    # the JSON family above has already answered.
                    self.send_response(400); self.end_headers(); return
                if path == "/approve":
                    # Sweep R103: only a real JSON true approves —
                    # bool("false") used to APPROVE a pending call;
                    # anything that is not exactly true denies.
                    deck.approval_answered.emit(data.get("answer") is True)
                elif path == "/stop":
                    deck.stop_requested.emit()
                elif path == "/mode":
                    mode = str(data.get("mode", "")).strip().lower()
                    if mode:
                        # Sweep R109: any string used to be forwarded
                        # as a mode and silently dropped by the
                        # window's own check, so a bad mode looked
                        # accepted. Validate here against the one
                        # MODES list — when the Write/Research modes
                        # extend that list, the deck follows.
                        from . import permissions as _perm_mod
                        if mode not in _perm_mod.MODES:
                            self.send_response(404); self.end_headers(); return
                        deck.mode_changed.emit(mode)
                elif path == "/council":
                    # {on: bool} is the chip's payload; a bare POST
                    # toggles against the last synced state.
                    if "on" in data:
                        # Sweep R103: only a real bool is a decision —
                        # a truthy string used to flip the council.
                        if not isinstance(data["on"], bool):
                            self.send_response(400); self.end_headers(); return
                        deck.council_changed.emit(data["on"])
                    else:
                        # Sweep R105: the toggle reads the synced
                        # state through the locked getter, never the
                        # raw _state dict.
                        deck.council_changed.emit(not deck.council_on())
                else:
                    # Unknown path: the old blanket 204 lied about
                    # having acted. 404 says nothing happened.
                    self.send_response(404); self.end_headers(); return
                self.send_response(204); self.end_headers()

        # Sweep R107: a bind failure (the port is already held —
        # a second Forge, a zombie deck) used to escape start() as
        # a raw OSError through the toggle. It now raises DeckError
        # with a plain-words reason and leaves _server None, so the
        # deck reads OFF everywhere it is asked.
        try:
            self._server = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        except OSError as exc:
            self._server = None
            raise DeckError(
                f"The phone deck couldn't start — port {self.port} "
                f"is already in use ({exc}). Close whatever holds "
                "it, then toggle the deck again.") from exc
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        # S4 (audit S1-06): the session's token dies with the
        # session. Replacing it (rather than leaving the last one
        # in place) closes the stop/start race where a request
        # already in flight — or a server somehow still answering —
        # would accept the old key; the next start() mints another.
        self.token = secrets.token_urlsafe(16)
