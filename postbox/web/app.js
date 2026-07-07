const API = "";  // same origin
const palette = ["#2e8bba","#d9633b","#6b5bd2","#8a4fc4","#2f8f63","#b5495b","#3d7a8c","#9a6a2f"];
const colorFor = a => palette[[...(a||"")].reduce((h,c)=>h+c.charCodeAt(0),0) % palette.length];
const initials = n => (n||"").replace(/[^a-z0-9]/gi,'').slice(0,2).toUpperCase();
const esc = s => (s||"").replace(/[&<>]/g, m=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[m]));
const $ = id => document.getElementById(id);

// Observer token — only needed when POSTBOX_OBSERVER_TOKEN is set on the server (e.g. a VM).
// EventSource can't set headers, so it rides the query string; everything else uses a header.
const OBS_TOKEN = new URLSearchParams(location.search).get("token")
  || localStorage.getItem("postbox.observer_token") || "";
const j = async (u, o={}) => {
  const headers = {...(o.headers||{})};
  if(OBS_TOKEN) headers["X-Observer-Token"] = OBS_TOKEN;
  const r = await fetch(API+u, {...o, headers});
  if(!r.ok) throw new Error(await r.text());
  return r.status===204 ? null : r.json();
};

let AGENTS = [];                                   // [{id,name,address,profile,status}]
let PEERS = [];
let YOU = localStorage.getItem("postbox.you") || null;   // your (human) identity address
let viewer = null;                                 // identity we're viewing/sending as (Stage 4: impersonation)
let THREADS = [];
let openId = null;
let panel = "chat";                                // chat | fleet (Stage 5)
let _lastMsgId = null, _openOther = null, draftTo = null;

const agentByAddr = a => AGENTS.find(x=>x.address===a) || {address:a, name:a, status:"offline", profile:null};
const nameOf   = a => agentByAddr(a).name;
const isHuman  = a => !!agentByAddr(a).profile?.human;
const isRemote = a => !!agentByAddr(a).profile?.remote;
const isOnline = a => !isHuman(a) && agentByAddr(a).status === "online";
const peerNames = () => new Set(PEERS.map(p=>p.name));
const impersonating = () => viewer !== YOU;
const otherOf = t => (t.members||[]).find(m=>m!==viewer) || (t.members||[])[0] || "";
const unreadFor = t => (t.unread && t.unread[viewer]) || 0;
const remoteTag = a => isRemote(a) ? `<span class="rtag">${esc(agentByAddr(a).profile?.peer || "remote")}</span>` : "";

async function loadAgents(){ AGENTS = await j("/observer/agents"); }
async function loadThreads(){ THREADS = await j("/observer/threads?address="+encodeURIComponent(viewer)); }

function setStatus(msg, kind){
  const s = $("sendStatus"); s.className = "sendstatus" + (kind?(" "+kind):"");
  s.textContent = msg || "";
  if(kind==="ok" || kind==="err") setTimeout(()=>{ if(s.textContent===msg) s.textContent=""; }, 3500);
}
function showComposer(on){
  $("composer").style.display = on ? "" : "none";
  if(!on){ $("cinput").value = ""; $("send").classList.remove("on"); }
}

// First run: establish WHO YOU ARE (a human identity). Reused across reloads via localStorage.
async function ensureYou(){
  if(YOU && AGENTS.some(a=>a.address===YOU)) return;
  let name = (prompt("Welcome to Postbox — what's your name?", "") || "").trim();
  if(!name) name = "me";
  const existing = AGENTS.find(a=> a.name.toLowerCase()===name.toLowerCase()
                                || a.address.toLowerCase()===name.toLowerCase());
  if(existing){ YOU = existing.address; }
  else {
    const c = await j("/observer/identity", {method:"POST", headers:{'content-type':'application/json'},
      body: JSON.stringify({name})});
    YOU = c.address; await loadAgents();
  }
  localStorage.setItem("postbox.you", YOU);
}

// Honest receipt for the viewer's own outgoing messages.
function receiptHtml(m){
  if(m.from !== viewer) return "";
  const to = m.to || [];
  if(!to.length) return "";
  const unread = to.filter(r=> !(m.read_by||[]).includes(r));
  if(unread.length === 0) return `<span class="rcpt read" title="Opened by the recipient">✓✓ Read</span>`;
  const parts = unread.map(r=>{
    if(isHuman(r)) return {k:"sent", h:`✉ Sent · waiting for ${esc(nameOf(r))} to open`};
    if(isOnline(r)) return {k:"delivered", h:`<span class="dot on"></span>✓ Delivered`};
    return {k:"queued", h:`<span class="dot off"></span>◷ Queued · delivers when ${esc(nameOf(r))} connects`};
  });
  const cls = parts.some(p=>p.k==="queued") ? "queued" : parts.some(p=>p.k==="sent") ? "sent" : "";
  return `<span class="rcpt ${cls}" title="Live delivery status">${parts.map(p=>p.h).join(" · ")}</span>`;
}

function renderSidebar(){
  $("vasName").textContent = impersonating() ? nameOf(viewer) : "You";
  $("vasBtn").classList.toggle("impersonating", impersonating());
  $("foot").innerHTML = impersonating()
    ? `Impersonating <b>${esc(nameOf(viewer))}</b> · <a href="#" data-act="asme">back to you</a>`
    : `You’re signed in as ${esc(nameOf(YOU))}`;
  $("navFleet").classList.toggle("active", panel==="fleet");
  $("navAll").classList.toggle("active", panel==="allconv");
  const list = $("dmList"); list.innerHTML = "";
  THREADS.forEach(t=>{
    const o = otherOf(t), un = unreadFor(t);
    const row = document.createElement("div");
    row.className = "dm" + (panel==="chat" && openId===t.thread_id ? " active" : "") + (un?" unread":"");
    row.dataset.act = "open"; row.dataset.val = t.thread_id;
    row.innerHTML = `<span class="dav" style="background:${colorFor(o)}">${initials(nameOf(o))}${isHuman(o)?"":`<span class="pres ${isOnline(o)?'':'off'}"></span>`}</span>`
      + `<span class="nm">${esc(nameOf(o))}</span>${remoteTag(o)}` + (un?`<span class="b">${un}</span>`:"");
    list.appendChild(row);
  });
  if(!THREADS.length) list.innerHTML = `<div style="color:var(--side-ink-dim);font-size:13px;padding:4px 14px">No conversations yet — search above.</div>`;
}

function emptyMain(){
  $("mTitle").textContent = "Messages"; $("mSub").textContent = "";
  $("msgs").innerHTML = `<div class="empty"><div class="emptybox"><div class="et">No conversation open</div><div class="eh">Search for someone above to start a direct message${impersonating()?` as ${esc(nameOf(viewer))}`:''}.</div></div></div>`;
  showComposer(false);
}

async function showThread(id, readOnly=false){
  openId = id; panel = readOnly ? "allconv" : "chat"; draftTo = null;
  const d = await j("/observer/threads/"+encodeURIComponent(id));
  const o = (d.members||[]).find(m=>m!==viewer) || (d.members||[])[0] || "";
  _openOther = o;
  if(readOnly){
    $("mTitle").innerHTML = (d.members||[]).map(m=>`${esc(nameOf(m))}${remoteTag(m)}`).join(' <span class="convarrow">↔</span> ') || "conversation";
    $("mSub").textContent = "read-only · watching";
  } else {
    $("mTitle").innerHTML = `${esc(nameOf(o))}${remoteTag(o)}`;
    $("mSub").textContent = isHuman(o) ? "person" : (isOnline(o) ? "online" : "offline");
  }
  const msgs = $("msgs"); msgs.innerHTML = "";
  (d.messages||[]).forEach(m=>{
    const row = document.createElement("div"); row.className = "msg";
    row.innerHTML = `<div class="av" style="background:${colorFor(m.from)}">${initials(nameOf(m.from))}</div>`
      + `<div><div class="l1"><span class="who">${esc(nameOf(m.from))}</span><span class="t">${(m.created_at||'').slice(11,16)}</span></div>`
      + `<div class="txt">${esc(m.body)}</div>${readOnly?"":receiptHtml(m)}</div>`;
    msgs.appendChild(row);
  });
  msgs.scrollTop = msgs.scrollHeight;
  _lastMsgId = (d.messages && d.messages.length) ? d.messages[d.messages.length-1].id : null;
  showComposer(!readOnly);
  if(!readOnly) $("cinput").placeholder = `Message ${nameOf(o)}${impersonating()?` as ${nameOf(viewer)}`:''}…`;
  // email-style auto-read — ONLY as a human reading their own inbox; never when impersonating or watching.
  if(!readOnly && !impersonating() && isHuman(viewer) && (d.members||[]).includes(viewer)){
    try{
      const res = await j("/observer/read", {method:"POST", headers:{'content-type':'application/json'},
        body: JSON.stringify({as: viewer, thread_id: id})});
      if(res && res.marked){ await loadThreads(); }
    }catch(e){ /* non-fatal */ }
  }
  renderSidebar();
}

// Read-only "watch everything" view — every thread, incl. agent↔agent + remote,
// without impersonating. Backed by the identity-agnostic /observer/threads.
async function openAllConv(){
  panel = "allconv"; openId = null; draftTo = null; closeVas(); closeSearch();
  $("mTitle").textContent = "All conversations";
  $("mSub").textContent = "every thread — read-only";
  showComposer(false);
  renderSidebar();
  let all = [];
  try{ all = await j("/observer/threads"); }catch(e){ all = []; }
  const rows = all.map(t=>{
    const members = (t.members||[]).map(m=>nameOf(m)).join(", ") || "(no members)";
    const last = t.last||{}; const when = (last.at||"").slice(11,16);
    const subj = t.subject ? `<span class="convsubj">${esc(t.subject)}</span>` : "";
    return `<div class="convrow" data-act="openall" data-val="${esc(t.thread_id)}">`
      + `<div class="convtop"><span class="convmem">${esc(members)}</span>${subj}<span class="convwhen">${esc(when)}</span></div>`
      + `<div class="convlast">${last.from?`<b>${esc(nameOf(last.from))}:</b> `:""}${esc((last.text||"").slice(0,140))}</div></div>`;
  }).join("") || `<div class="fnote">No conversations yet.</div>`;
  $("msgs").innerHTML = `<div class="conv">${rows}</div>`;
}

async function doSend(){
  const inp = $("cinput"), body = inp.value.trim();
  if(!body || !_openOther) return;
  inp.value = ""; $("send").classList.remove("on");
  setStatus("Sending…");
  try{
    const res = await j("/observer/send", {method:"POST", headers:{'content-type':'application/json'},
      body: JSON.stringify({from: viewer, to: _openOther, body, in_reply_to: _lastMsgId})});
    await loadThreads(); await showThread(res.thread_id);
    const to = _openOther;
    setStatus(isHuman(to) ? `✉ Sent to ${nameOf(to)}` : isOnline(to) ? `✓ Delivered to ${nameOf(to)}` : `◷ Queued for ${nameOf(to)}`, "ok");
  }catch(e){
    setStatus("⚠ Failed to send — "+String(e.message||e).slice(0,80), "err");
    inp.value = body;
  }
}

// Slack-style quick switcher: find anyone and DM them (opens the existing thread, or a draft).
function renderSearch(){
  const res = $("searchRes"), raw = ($("search").value||"").trim(), q = raw.toLowerCase();
  const hits = AGENTS.filter(a=> a.address!==viewer
    && (!q || a.name.toLowerCase().includes(q) || a.address.toLowerCase().includes(q)));
  const m = q.match(/^([a-z0-9._-]+)@([a-z0-9._-]+)$/);
  const canFirstContact = m && peerNames().has(m[2]) && !AGENTS.some(a=>a.address.toLowerCase()===q);
  const firstContact = canFirstContact
    ? [`<div class="sres" data-act="dm" data-val="${esc(raw)}"><span class="dav" style="background:${colorFor(raw)}">${initials(raw)}</span>Message ${esc(raw)}<span class="smeta">via ${esc(m[2])}</span></div>`]
    : [];
  res.style.display = "block";
  res.innerHTML = (firstContact.length || hits.length)
    ? firstContact.concat(hits.map(a=>`<div class="sres" data-act="dm" data-val="${esc(a.address)}"><span class="dav" style="background:${colorFor(a.address)}">${initials(a.name)}</span>${esc(a.name)}${remoteTag(a.address)}<span class="smeta">${isHuman(a.address)?'person':(isOnline(a.address)?'online':'offline')}</span></div>`)).join("")
    : `<div class="sempty">${q?`No one matches “${esc(q)}”`:"No one to message yet"}</div>`;
}
function closeSearch(){ $("search").value=""; $("searchRes").style.display="none"; $("searchRes").innerHTML=""; }

async function openDm(addr){
  closeSearch();
  const t = THREADS.find(x=> (x.members||[]).length===2 && x.members.includes(viewer) && x.members.includes(addr));
  if(t) return showThread(t.thread_id);
  // no thread yet — show a draft; the first send creates the real thread
  panel="chat"; openId=null; draftTo=addr; _openOther=addr; _lastMsgId=null;
  $("mTitle").innerHTML = `${esc(nameOf(addr))}${remoteTag(addr)}`;
  $("mSub").textContent = isHuman(addr) ? "person" : (isOnline(addr) ? "online" : "offline");
  $("msgs").innerHTML = `<div class="empty"><div class="emptybox"><div class="et">Message ${esc(nameOf(addr))}</div><div class="eh">This is the start of your direct message${impersonating()?` as ${esc(nameOf(viewer))}`:''}. Say hi 👋</div></div></div>`;
  showComposer(true);
  $("cinput").value = "";
  $("cinput").placeholder = `Message ${nameOf(addr)}${impersonating()?` as ${nameOf(viewer)}`:''}…`;
  $("cinput").focus();
  renderSidebar();
}

// Impersonation: deliberately act as another agent. Purely a client concept — the
// observer API can read/send as any identity. Mark-read stays human-only (see showThread),
// so viewing an agent's inbox never silently marks its mail read.
function renderVas(){
  const m = $("vasMenu");
  let h = `<div class="mh">Viewing as</div>`
    + `<div class="opt" data-act="asme"><span class="av person" style="background:${colorFor(YOU)}">${initials(nameOf(YOU))}</span><span class="nm">${esc(nameOf(YOU))} (you)</span>${!impersonating()?'<span class="ck">✓</span>':''}</div>`
    + `<div class="sepm"></div><div class="mcap">Impersonate an agent</div>`;
  AGENTS.filter(a=> a.address!==YOU && !a.profile?.human).forEach(a=>{
    h += `<div class="opt" data-act="setviewer" data-val="${esc(a.address)}"><span class="av" style="background:${colorFor(a.address)}">${initials(a.name)}<span class="pres ${isOnline(a.address)?'':'off'}"></span></span><span class="nm">${esc(a.name)}</span>${viewer===a.address?'<span class="ck">✓</span>':''}</div>`;
  });
  m.innerHTML = h;
}
function toggleVas(){ renderVas(); $("vasMenu").classList.toggle("show"); }
function closeVas(){ $("vasMenu").classList.remove("show"); }
async function setViewer(addr){
  closeVas(); closeSearch();
  viewer = addr; openId = null; draftTo = null; panel = "chat";
  await loadThreads();
  renderSidebar();
  openId = THREADS.length ? THREADS[0].thread_id : null;
  if(openId) await showThread(openId); else emptyMain();
}

// ---- Fleet control panel (headless agents) ----
let fleetSig = "", fleetPausedUntil = 0;
const FLEET_STATE = { running:["#17a673","running"], queued:["#e8912d","queued"],
  idle:["#3d9be0","ready"], backoff:["#e01e5a","backoff"], disabled:["#b8bcc4","disabled"] };

function openFleet(){
  panel = "fleet"; openId = null; draftTo = null; closeVas(); closeSearch();
  $("mTitle").textContent = "🤖 Fleet";
  $("mSub").textContent = "headless workers (spawned on mail) + interactive terminal agents (you attach)";
  showComposer(false);
  renderSidebar();
  // static shell; refresh is MANUAL (the ↻ button) so the list never flickers under you
  $("msgs").innerHTML = `<div class="fleet">
    <div class="faddbar">
      <input id="fAddr" placeholder="agent name (e.g. reviewer)" autocomplete="off">
      <input id="fCmd" placeholder="command — default: copilot -p {prompt}" autocomplete="off">
      <input id="fCwd" placeholder="cwd (optional)" autocomplete="off">
      <button data-act="fleetadd" class="fbtn primary">Add agent</button>
      <button data-act="fleetrefresh" class="fbtn" title="Refresh fleet status + presence">↻ Refresh</button>
    </div>
    <div class="faddbar term">
      <input id="tName" placeholder="terminal agent name (e.g. alice)" autocomplete="off">
      <input id="tCwd" placeholder="cwd (optional)" autocomplete="off">
      <button data-act="termspawn" class="fbtn primary">Spin up terminal</button>
      <span class="fhint">interactive copilot in tmux — attach from your terminal</span>
    </div>
    <div id="fleetList"></div></div>`;
  refreshFleet(true);
}

async function refreshFleet(force){
  const host = $("fleetList"); if(!host || panel!=="fleet") return;
  if(!force && Date.now() < fleetPausedUntil) return;     // just interacted — don't clobber
  let list;
  try{ list = await j("/fleet"); }
  catch(e){ host.innerHTML = `<div class="empty"><div class="emptybox"><div class="eh">Fleet API error: ${esc(String(e.message||e))}</div></div></div>`; return; }
  try{ await loadAgents(); }catch(e){}                    // refresh presence for the directory below
  let terms = [];
  try{ terms = await j("/terminals"); }catch(e){ terms = []; }
  const sig = JSON.stringify([list, terms, AGENTS.map(a=>[a.address, a.status])]);
  if(!force && sig === fleetSig) return;                  // unchanged → keep buttons live
  fleetSig = sig;
  const inFleet = new Set(list.map(a=>a.address));
  const rows = list.map(a=>{
    const [col,lbl] = FLEET_STATE[a.state] || ["#888", a.state];
    const running = a.state==="running";
    const meta = [a.last_exit!=null?`exit ${a.last_exit}`:"", a.fail_count?`fails ${a.fail_count}`:"",
                  a.last_run?("ran "+a.last_run.slice(11,19)):""].filter(Boolean).join(" · ");
    return `<div class="frow${a.enabled?"":" off"}">
      <div class="frtop">
        <span class="fdot" style="background:${col}"></span>
        <span class="fname">${esc(a.address)}</span>
        <span class="fpill ${esc(a.state)}">${esc(lbl)}</span>
        <div class="fbtns">
          <button data-act="fleet" data-op="run" data-val="${esc(a.address)}" ${running?"disabled":""} title="Force a turn now">Run now</button>
          <button data-act="fleet" data-op="kill" data-val="${esc(a.address)}" ${running?"":"disabled"} title="Stop the current turn">Kill</button>
          <button data-act="fleet" data-op="${a.enabled?"disable":"enable"}" data-val="${esc(a.address)}" title="${a.enabled?'Stop auto-running on new mail':'Auto-run when mail arrives'}">${a.enabled?"Disable":"Enable"}</button>
          <button data-act="fleet" data-op="remove" data-val="${esc(a.address)}" class="danger" title="Remove from fleet">✕</button>
        </div>
      </div>
      <div class="fcmd">${esc((a.command||[]).join(" "))}${a.cwd?` <span class="fcwd">@ ${esc(a.cwd)}</span>`:""}</div>
      ${meta?`<div class="fmeta">${esc(meta)}</div>`:""}
      ${a.tail?`<details class="ftaild"${running?" open":""}><summary>Latest output</summary><pre class="ftail">${esc(a.tail)}</pre></details>`:""}
    </div>`;
  }).join("") || `<div class="fnote">No fleet agents yet — add one above and Postbox will spawn a headless turn when it gets mail.</div>`;
  // directory: EVERY registered identity + live presence (which are online/running, who's in the fleet)
  // sorted by name so rows stay put — presence changes update the label in place, never reorder.
  const dir = [...AGENTS].sort((a,b)=>a.name.localeCompare(b.name)).map(a=>{
    const human = isHuman(a.address), online = isOnline(a.address);
    const dot = human ? "#8a4fc4" : (online ? "#17a673" : "#b8bcc4");
    const meta = [human ? "person" : (online ? "online" : "offline"), inFleet.has(a.address) ? "in fleet" : ""].filter(Boolean).join(" · ");
    const sid = a.session_key
      ? `<button class="sidchip" data-act="copysid" data-val="${esc(a.session_key)}" title="Copy session id — resume this agent with:  copilot --resume ${esc(a.session_key)}">↻ ${esc(a.session_key.slice(0,8))}</button>`
      : "";
    const dm = a.address !== viewer;                 // can't DM the identity you're viewing as
    const chat = dm ? `<button class="fchat" data-act="dm" data-val="${esc(a.address)}" title="Message ${esc(a.name)}">💬 Chat</button>` : "";
    return `<div class="fdirrow"><span class="fdot" style="background:${dot}"></span><span class="nm">${esc(a.name)}</span>${sid}<span class="meta">${esc(meta)}</span>${chat}<button class="fx" data-act="forget" data-val="${esc(a.id)}" title="Forget — hide from this list (keeps its messages + session)">✕</button></div>`;
  }).join("") || `<div class="fnote">No agents registered yet.</div>`;
  // interactive terminal agents (tmux sessions you attach to)
  const termRows = terms.map(t=>
    `<div class="fdirrow"><span class="fdot" style="background:#3d9be0"></span>`
    + `<span class="nm">${esc(t.name)}</span>`
    + `<button class="sidchip" data-act="copytext" data-val="${esc(t.attach)}" title="Copy — run this in a terminal to attach">${esc(t.attach)}</button>`
    + `<span class="meta">tmux</span>`
    + `<button class="fchat" data-act="dm" data-val="${esc(t.name)}" title="Message ${esc(t.name)}">💬 Chat</button>`
    + `<button class="fx" data-act="termkill" data-val="${esc(t.name)}" title="Kill this terminal session (its identity/messages stay)">✕</button></div>`
  ).join("") || `<div class="fnote">No terminal agents running — spin one up above, then attach with the tmux command it gives you.</div>`;
  host.innerHTML = `<div class="fgrp">Fleet agents <span class="fgc">${list.length} spawned on new mail</span></div>${rows}`
    + `<div class="fgrp">Terminal agents <span class="fgc">${terms.length} interactive</span></div><div class="fdir">${termRows}</div>`
    + `<div class="fgrp">All registered agents <span class="fgc">${AGENTS.length} with an inbox</span></div><div class="fdir">${dir}</div>`;
}

async function addFleetAgent(){
  const address = $("fAddr").value.trim();
  if(!address){ alert("Enter an agent name."); return; }
  const cmdRaw = $("fCmd").value.trim();
  const command = cmdRaw ? cmdRaw.split(/\s+/) : null;    // arg-list; the server never shells out
  const cwd = $("fCwd").value.trim() || null;
  fleetPausedUntil = Date.now() + 1500;
  try{
    await j("/fleet", {method:"POST", headers:{'content-type':'application/json'},
      body: JSON.stringify({address, command, cwd})});
    $("fAddr").value=""; $("fCmd").value=""; $("fCwd").value="";
    await loadAgents(); await refreshFleet(true);
  }catch(e){ alert("Could not add agent — "+String(e.message||e).slice(0,160)); }
}

async function fleetAction(op, addr){
  fleetPausedUntil = Date.now() + 1500;
  try{
    if(op==="remove"){
      if(!confirm(`Remove ${addr} from the fleet? (its inbox/identity stays)`)) return;
      await j("/fleet/"+encodeURIComponent(addr), {method:"DELETE"});
    } else {
      await j(`/fleet/${encodeURIComponent(addr)}/${op}`, {method:"POST"});
    }
    await loadAgents(); await refreshFleet(true);
  }catch(e){ alert(`Fleet ${op} failed — `+String(e.message||e).slice(0,160)); }
}

async function forgetAgent(id){
  const a = AGENTS.find(x=>x.id===id);
  const who = a ? a.name : id;
  if(!confirm(`Forget ${who}?\n\nIt disappears from this list, but its messages and session are kept — resume its session to bring it back.`)) return;
  fleetPausedUntil = Date.now() + 1500;
  try{
    await j("/observer/agents/"+encodeURIComponent(id), {method:"DELETE"});
    await loadAgents(); await refreshFleet(true);
  }catch(e){ alert("Could not forget — "+String(e.message||e).slice(0,160)); }
}

async function copyText(text, btn){
  try{ await navigator.clipboard.writeText(text); }
  catch(e){ prompt("Copy this:", text); return; }
  if(btn){ const o = btn.textContent; btn.textContent = "✓ copied"; setTimeout(()=>{ btn.textContent = o; }, 1200); }
}

async function spawnTerminal(){
  const name = $("tName").value.trim();
  if(!name){ alert("Enter a name for the terminal agent."); return; }
  const cwd = $("tCwd").value.trim() || null;
  fleetPausedUntil = Date.now() + 1500;
  try{
    const res = await j("/terminals", {method:"POST", headers:{'content-type':'application/json'},
      body: JSON.stringify({name, cwd})});
    $("tName").value=""; $("tCwd").value="";
    await refreshFleet(true);
    try{ await navigator.clipboard.writeText(res.attach); }catch(e){}
    alert(`Spawned terminal agent “${res.name}”.\n\nAttach from a terminal with:\n\n    ${res.attach}\n\n(copied to your clipboard)`);
  }catch(e){ alert("Could not spin up terminal — "+String(e.message||e).slice(0,200)); }
}

async function killTerminal(name){
  if(!confirm(`Kill terminal session for “${name}”?\n\nThis stops the tmux session. The agent's identity and messages are kept.`)) return;
  fleetPausedUntil = Date.now() + 1500;
  try{
    await j("/terminals/"+encodeURIComponent(name), {method:"DELETE"});
    await refreshFleet(true);
  }catch(e){ alert("Could not kill terminal — "+String(e.message||e).slice(0,160)); }
}

function connectLive(){
  const url = "/observer/events" + (OBS_TOKEN ? "?token="+encodeURIComponent(OBS_TOKEN) : "");
  const es = new EventSource(url);
  const refresh = async ()=>{
    if(panel==="fleet") return;                 // Fleet is manual-refresh (the ↻ button)
    if(panel==="allconv"){ if(openId){ await showThread(openId, true); } else { await openAllConv(); } return; }
    await loadThreads(); renderSidebar();
    if(openId) await showThread(openId);
  };
  es.addEventListener("message.received", refresh);
  es.addEventListener("message.read", refresh);   // flips ✓ Delivered → ✓✓ Read live
  es.onerror = ()=>{};                            // browser auto-reconnects
}

$("cinput").addEventListener("input", e=> $("send").classList.toggle("on", !!e.target.value.trim()));
$("cinput").addEventListener("keydown", e=>{ if(e.key==="Enter"){ e.preventDefault(); doSend(); }});
$("search").addEventListener("input", renderSearch);
$("search").addEventListener("focus", renderSearch);   // show everyone the moment you click in
document.addEventListener("click", e=>{
  const t = e.target.closest("[data-act]");
  if(!t){
    if(!e.target.closest(".searchwrap")) closeSearch();
    if(!e.target.closest(".vas")) closeVas();
    return;
  }
  e.preventDefault();
  const a = t.dataset.act;
  if(a==="open") return showThread(t.dataset.val);
  if(a==="dm") return openDm(t.dataset.val);
  if(a==="send") return doSend();
  if(a==="vas") return toggleVas();
  if(a==="asme") return setViewer(YOU);
  if(a==="setviewer") return setViewer(t.dataset.val);
  if(a==="showfleet") return openFleet();
  if(a==="fleetrefresh") return refreshFleet(true);
  if(a==="fleet") return fleetAction(t.dataset.op, t.dataset.val);
  if(a==="fleetadd") return addFleetAgent();
  if(a==="forget") return forgetAgent(t.dataset.val);
  if(a==="copysid") return copyText(t.dataset.val, t);
  if(a==="copytext") return copyText(t.dataset.val, t);
  if(a==="termspawn") return spawnTerminal();
  if(a==="termkill") return killTerminal(t.dataset.val);
  if(a==="showall") return openAllConv();
  if(a==="openall") return showThread(t.dataset.val, true);
});

(async function boot(){
  await loadAgents();
  try{ PEERS = await j("/peers"); }catch(e){ PEERS = []; }
  await ensureYou();
  viewer = YOU;
  await loadThreads();
  renderSidebar();
  openId = THREADS.length ? THREADS[0].thread_id : null;
  if(openId) await showThread(openId); else emptyMain();
  connectLive();
})();
