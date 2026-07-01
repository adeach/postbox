const API = "";  // same origin
const palette = ["#2e8bba","#d9633b","#6b5bd2","#8a4fc4","#2f8f63","#b5495b","#3d7a8c","#9a6a2f"];
const colorFor = a => palette[[...a].reduce((h,c)=>h+c.charCodeAt(0),0) % palette.length];
const initials = n => n.replace(/[^a-z0-9]/gi,'').slice(0,2).toUpperCase();
// escapes for HTML *text* content only — do NOT use the result inside an HTML attribute (no quote escaping)
const esc = s => (s||"").replace(/[&<>]/g, m=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[m]));

let AGENTS = [];                    // [{address,name,status,...}]
let THREADS = [];                   // summaries for the current view
let current = localStorage.getItem("postbox.identity") || "all";
let openThread = null;
let composeMode = false;
let _lastIds = {};

const $ = id => document.getElementById(id);
// Observer token (only needed when POSTBOX_OBSERVER_TOKEN is set on the server, e.g. the VM).
// EventSource can't set headers, so it goes on the query string; everything else uses a header.
const OBS_TOKEN = new URLSearchParams(location.search).get("token")
  || localStorage.getItem("postbox.observer_token") || "";
const j = async (u, o={}) => {
  const headers = {...(o.headers||{})};
  if(OBS_TOKEN) headers["X-Observer-Token"] = OBS_TOKEN;
  const r = await fetch(API+u, {...o, headers});
  if(!r.ok) throw new Error(await r.text());
  return r.status===204?null:r.json();
};

async function loadAgents(){ AGENTS = await j("/observer/agents"); }
function agentByAddr(a){ return AGENTS.find(x=>x.address===a) || {address:a,name:a,status:"offline"}; }
function isHuman(a){ return !!agentByAddr(a).profile?.human; }
function isOnline(a){ return !isHuman(a) && agentByAddr(a).status === "online"; }

function totalUnread(t){ return Object.values(t.unread||{}).reduce((a,b)=>a+b,0); }
function unreadForView(t){ return current==="all" ? totalUnread(t) : (t.unread?.[current]||0); }

async function loadThreads(){
  const q = current==="all" ? "" : "?address="+encodeURIComponent(current);
  THREADS = await j("/observer/threads"+q);
}

function setStatus(msg, kind){
  const s = $("sendStatus"); s.className = "sendstatus" + (kind?(" "+kind):"");
  s.textContent = msg || "";
  if(kind==="ok" || kind==="err") setTimeout(()=>{ if(s.textContent===msg) s.textContent=""; }, 3500);
}

function renderMenu(){
  const m = $("idMenu"); m.innerHTML = '<div class="mh">Open as</div>';
  // human = render a "person" chip (no presence dot); agent = avatar with live dot
  const opt = (addr,name,globe,online,human,unread)=>{
    const o = document.createElement("div"); o.className="opt";
    const col = globe ? "#5a2b5c" : colorFor(addr);
    const av = globe ? `<span class="av globe" style="background:${col}">🌐</span>`
      : human ? `<span class="av person" style="background:${col}">${initials(name)}</span>`
      : `<span class="av" style="background:${col}">${initials(name)}<span class="pres ${online?'':'off'}"></span></span>`;
    o.innerHTML = `${av}<span class="nm">${esc(name)}</span>${human?'<span class="you">you</span>':''}
      ${unread?`<span class="badge">${unread}</span>`:''}${(globe?'all':addr)===current?'<span class="ck">✓</span>':''}`;
    o.onclick = e=>{ e.stopPropagation(); setIdentity(globe?'all':addr); closeMenu(); };
    m.appendChild(o);
  };
  opt(null,"All activity",true,true,false,0);
  const sep = document.createElement("div"); sep.className="sepm"; m.appendChild(sep);
  AGENTS.forEach(a=> opt(a.address, a.name, false, a.status==="online", !!a.profile?.human, 0));
  const add = document.createElement("div"); add.className="opt"; add.style.color="#1264a3";
  add.innerHTML = '<span class="av" style="background:#e8eef7;color:#1264a3">＋</span><span class="nm">New identity…</span>';
  add.onclick = async e=>{ e.stopPropagation(); const name = prompt("New identity name (e.g. your name):"); if(name){ const r = await j("/observer/identity",{method:"POST",headers:{'content-type':'application/json'},body:JSON.stringify({name})}); await loadAgents(); setIdentity(r.address); } closeMenu(); };
  m.appendChild(add);
}
function openMenu(){ renderMenu(); $("idMenu").classList.add("show"); $("idSwitch").classList.add("open"); }
function closeMenu(){ $("idMenu").classList.remove("show"); $("idSwitch").classList.remove("open"); }
$("idSwitch").onclick = e=>{ e.stopPropagation(); $("idMenu").classList.contains("show")?closeMenu():openMenu(); };
document.addEventListener("click", closeMenu);

function renderSide(){
  const isAll = current==="all";
  const name = isAll ? "All activity" : (agentByAddr(current).name);
  $("sideName").textContent = name;
  const you = !isAll && !!agentByAddr(current).profile?.human;
  $("youTag").style.display = you ? "inline-block" : "none";
  $("newMsg").disabled = isAll;
  $("newMsg").title = isAll ? "Open as an identity first" : `Send a new message as ${name}`;
  $("cinput").placeholder = isAll ? "Pick an identity (top-left) to send…"
    : (composeMode ? "Write your message — Enter to send…" : `Reply as ${name}…`);
  $("cinput").disabled = isAll;
  const list = $("chList"); list.innerHTML = "";
  THREADS.forEach(t=>{
    const un = unreadForView(t);
    const others = isAll ? t.members.join(" ↔ ") : t.members.filter(m=>m!==current).join(", ");
    const el = document.createElement("div");
    el.className = "ch"+(openThread===t.thread_id?" active":"")+(un?" unread":"");
    el.innerHTML = `<span class="hash">#</span><span class="name">${esc(t.subject||others||"(no subject)")}</span>`+(un?`<span class="badge">${un}</span>`:"");
    el.title = others;
    el.onclick = ()=> selectThread(t.thread_id);
    list.appendChild(el);
  });
}

// Honest delivery/read receipt. Read = recipient opened it. Otherwise per recipient:
// online agent -> Delivered (a live session has it); offline agent -> Queued (delivers
// when they connect); human -> Sent (no agent session; waits until they open Postbox).
function receiptHtml(m){
  if(!m.to || !m.to.length) return "";
  const unread = m.to.filter(r=> !(m.read_by||[]).includes(r));
  if(unread.length === 0) return `<span class="rcpt read" title="Opened by the recipient">✓✓ Read</span>`;
  let anyPending = false;
  const parts = unread.map(r=>{
    if(isHuman(r)){ anyPending = true; return `<span class="dot off"></span>◷ Sent · waiting for ${esc(r)} to open`; }
    if(isOnline(r)) return `<span class="dot on"></span>✓ Delivered`;
    anyPending = true; return `<span class="dot off"></span>◷ Queued · delivers when ${esc(r)} connects`;
  });
  return `<span class="rcpt ${anyPending?'queued':''}" title="Live delivery status">${parts.join(" · ")}</span>`;
}

async function selectThread(tid){
  leaveFleet();
  composeMode = false;
  openThread = tid;
  const d = await j("/observer/threads/"+tid);
  $("mTitle").textContent = "# "+(d.subject||"(no subject)");
  $("mSub").textContent = d.members.join(" ↔ ");
  const obs = current==="all" || !d.members.includes(current);
  const ot = $("obsTag"); ot.style.display = obs?"inline-block":"none"; ot.textContent = current==="all"?"all activity":"observing";
  const avs = $("mAvs"); avs.innerHTML = "";
  d.members.forEach(mm=>{
    const human = isHuman(mm);
    const x=document.createElement("div"); x.className="a"+(human?" person":""); x.style.background=colorFor(mm);
    x.title = mm + (human ? " (person)" : (isOnline(mm)?" (online)":" (offline)"));
    x.innerHTML = initials(mm) + (human ? "" : `<span class="pres ${isOnline(mm)?'':'off'}"></span>`);
    avs.appendChild(x);
  });
  const msgs = $("msgs"); msgs.innerHTML = '<div class="daydiv"><span>Conversation</span></div>';
  d.messages.forEach(m=>{
    const to = m.to[0] || "";
    const isSelf = m.from===current;
    const el = document.createElement("div"); el.className="msg";
    el.innerHTML = `<div class="av" style="background:${colorFor(m.from)}">${initials(m.from)}</div>
      <div><div class="l1"><span class="who">${esc(m.from)}</span>${isSelf?'<span class="self">this identity</span>':''}
      <span class="arrow">→ ${esc(to)}</span><span class="t">${(m.created_at||'').slice(11,16)}</span></div>
      <div class="txt">${esc(m.body)}</div>${receiptHtml(m)}</div>`;
    msgs.appendChild(el);
  });
  msgs.scrollTop = msgs.scrollHeight;
  _lastIds[tid] = d.messages.length ? d.messages[d.messages.length - 1].id : null;  // in_reply_to → reply stays in this thread
  renderSide();
  if(current!=="all") $("cinput").focus();
  // auto-mark-read like email — ONLY when opening as yourself (a human participant);
  // observing as an agent or in "all activity" must never touch read state.
  if(current!=="all" && isHuman(current) && d.members.includes(current)){
    try{
      const res = await j("/observer/read", {method:"POST", headers:{'content-type':'application/json'},
        body: JSON.stringify({as: current, thread_id: tid})});
      if(res && res.marked){ await loadThreads(); renderSide(); }
    }catch(e){ /* non-fatal */ }
  }
}

function openCompose(){
  leaveFleet();
  if(current === "all"){ setStatus("Open as an identity first (top-left), then compose.", "err"); return; }
  composeMode = true; openThread = null;
  $("mTitle").textContent = "✎ New message";
  $("mSub").textContent = "from "+current;
  $("obsTag").style.display = "none"; $("mAvs").innerHTML = "";
  const others = AGENTS.filter(a=> a.address !== current);
  const label = a => a.profile?.human ? "— person" : (a.status==="online" ? "— online" : "— offline");
  const opts = others.map(a=>`<option value="${a.address}">${esc(a.name)} ${label(a)}</option>`).join("");
  $("msgs").innerHTML = `<div class="composeform">
    <label>To</label>
    <select id="cTo">${opts || '<option value="">(no other agents yet)</option>'}</select>
    <label>Subject <span class="opt">(optional)</span></label>
    <input id="cSubj" placeholder="e.g. quick question" autocomplete="off">
    <div class="hint">Type your message below and press <b>Enter</b> to send.
    An <b>online agent</b> is woken in real time (<b>✓ Delivered</b>); an <b>offline agent</b> is <b>◷ Queued</b> until it connects; a <b>person</b> sees it (<b>◷ Sent</b>) when they open Postbox. It turns <b>✓✓ Read</b> once opened.</div>
  </div>`;
  $("cinput").value = ""; $("cinput").disabled = false;
  $("cinput").placeholder = "Write your message — Enter to send…";
  renderSide();
  ($("cTo")||{}).onchange = null;
  $("cinput").focus();
}
$("newMsg").onclick = openCompose;

const input = $("cinput"), sendBtn = $("send");
input.addEventListener("input", ()=> sendBtn.classList.toggle("on", !!input.value.trim() && current!=="all"));

async function doSend(){
  const txt = input.value.trim();
  if(!txt || current === "all") return;
  let to, subject = null, in_reply_to = null;
  if(composeMode){
    const sel = $("cTo"); to = sel ? sel.value : "";
    subject = ($("cSubj")?.value || "").trim() || null;
    if(!to){ setStatus("Pick a recipient.", "err"); return; }
  } else {
    if(!openThread) return;
    const t = THREADS.find(x=>x.thread_id===openThread); if(!t) return;
    to = t.members.find(m=>m!==current) || t.members[0];
    in_reply_to = _lastIds[openThread] || null;
  }
  input.value = ""; sendBtn.classList.remove("on");
  setStatus("Sending…");
  try{
    const res = await j("/observer/send", {method:"POST", headers:{'content-type':'application/json'},
      body: JSON.stringify({from: current, to, body: txt, subject, in_reply_to})});
    composeMode = false;
    await loadThreads();
    await selectThread(res.thread_id);
    const note = isHuman(to) ? `◷ Sent to ${to} — they'll see it when they open Postbox`
      : isOnline(to) ? `✓ Delivered to ${to}`
      : `◷ Queued for ${to} — delivers when they connect`;
    setStatus(note, "ok");
  }catch(e){
    setStatus("⚠ Failed to send — "+String(e.message||e).slice(0,80), "err");
    input.value = txt;  // restore so it isn't lost
  }
}
sendBtn.onclick = doSend;
input.addEventListener("keydown", e=>{ if(e.key==="Enter"){ e.preventDefault(); doSend(); }});

function connectLive(){
  const url = "/observer/events" + (OBS_TOKEN ? "?token="+encodeURIComponent(OBS_TOKEN) : "");
  const es = new EventSource(url);
  const refresh = async ()=>{
    if(fleetView){ refreshFleetList(); return; }
    await loadThreads(); renderSide(); if(openThread && !composeMode) await selectThread(openThread);
  };
  es.addEventListener("message.received", refresh);
  es.addEventListener("message.read", refresh);   // flips ✓ Delivered → ✓✓ Read live
  es.onerror = ()=>{};  // browser auto-reconnects
}

// ---- Fleet control panel ----
let fleetView = false, fleetTimer = null, fleetSig = "", fleetPausedUntil = 0;
// idle must look clearly ON (blue), distinct from disabled (grey + dimmed row)
const FLEET_STATE = { running:["#17a673","running"], queued:["#e8912d","queued"],
  idle:["#3d9be0","ready"], backoff:["#e01e5a","backoff"], disabled:["#b8bcc4","disabled"] };

function leaveFleet(){ fleetView=false; if(fleetTimer){ clearInterval(fleetTimer); fleetTimer=null; } }

function openFleet(){
  fleetView = true; composeMode = false; openThread = null; fleetSig = "";
  $("obsTag").style.display="none"; $("mAvs").innerHTML="";
  $("mTitle").textContent = "🤖 Fleet";
  $("mSub").textContent = "headless agents — a turn is spawned when they get mail";
  $("cinput").disabled = true; $("cinput").placeholder = "Fleet control panel";
  // Render the shell (add form + list container) ONCE. The refresh only rewrites
  // #fleetList, so it never wipes what you're typing or eats a button click.
  $("msgs").innerHTML = `<div class="fleet">
    <div class="faddbar">
      <input id="fAddr" placeholder="agent name (e.g. reviewer)" autocomplete="off">
      <input id="fCmd" placeholder="command — default: copilot -p {prompt}" autocomplete="off">
      <input id="fCwd" placeholder="cwd (optional)" autocomplete="off">
      <button id="fAdd">Add agent</button>
    </div>
    <div id="fleetList"></div>
  </div>`;
  $("fAdd").onclick = addFleetAgent;
  refreshFleetList(true);
  if(fleetTimer) clearInterval(fleetTimer);
  fleetTimer = setInterval(()=>{ if(fleetView) refreshFleetList(); }, 2000);
}

async function refreshFleetList(force){
  const host = $("fleetList"); if(!host) return;
  if(!force && Date.now() < fleetPausedUntil) return;   // just interacted — don't clobber
  let list;
  try{ list = await j("/fleet"); }
  catch(e){ host.innerHTML = `<div class="empty">Fleet API error: ${esc(String(e.message||e))}</div>`; return; }
  const sig = JSON.stringify(list);
  if(!force && sig === fleetSig) return;                 // unchanged → don't re-render (keeps buttons live)
  fleetSig = sig;
  host.innerHTML = list.map(a=>{
    const [col,lbl] = FLEET_STATE[a.state] || ["#888", a.state];
    const cmd = esc((a.command||[]).join(" "));
    const meta = [a.last_exit!=null?`exit ${a.last_exit}`:"", a.fail_count?`fails ${a.fail_count}`:"",
                  a.last_run?("ran "+a.last_run.slice(11,19)):""].filter(Boolean).join(" · ");
    const running = a.state==="running";
    return `<div class="frow${a.enabled?"":" off"}" data-a="${esc(a.address)}">
      <span class="fdot" style="background:${col}" title="${lbl}"></span>
      <div class="fmain"><div class="fname">${esc(a.address)}<span class="fstate">${a.enabled?lbl:"disabled"}</span></div>
        <div class="fcmd">${cmd}${a.cwd?` <span class="fcwd">@ ${esc(a.cwd)}</span>`:""}</div>
        ${meta?`<div class="fmeta">${esc(meta)}</div>`:""}
        ${a.tail?`<pre class="ftail">${esc(a.tail)}</pre>`:""}</div>
      <div class="fbtns">
        <button data-act="run" ${running?"disabled":""}>Run</button>
        <button data-act="kill" ${running?"":"disabled"}>Kill</button>
        <button data-act="${a.enabled?"disable":"enable"}">${a.enabled?"Disable":"Enable"}</button>
        <button data-act="remove" class="danger" title="Remove from fleet">✕</button>
      </div></div>`;
  }).join("") || '<div class="empty">No fleet agents yet — add one above.</div>';
  host.querySelectorAll(".frow .fbtns button").forEach(b=>{
    b.onclick = ()=> fleetAction(b.closest(".frow").dataset.a, b.dataset.act);
  });
}

async function addFleetAgent(){
  const address = $("fAddr").value.trim();
  if(!address){ setStatus("Enter an agent name.","err"); return; }
  const cmdRaw = $("fCmd").value.trim();
  const command = cmdRaw ? cmdRaw.split(/\s+/) : null;   // arg-list; server never shells out
  const cwd = $("fCwd").value.trim() || null;
  fleetPausedUntil = Date.now() + 1500;
  try{
    await j("/fleet",{method:"POST",headers:{'content-type':'application/json'},
      body:JSON.stringify({address, command, cwd})});
    setStatus(`Added ${address}`,"ok");
    $("fAddr").value=""; $("fCmd").value=""; $("fCwd").value="";
    await loadAgents(); await refreshFleetList(true);
  }catch(e){ setStatus("⚠ "+String(e.message||e).slice(0,120),"err"); }
}

async function fleetAction(addr, act){
  fleetPausedUntil = Date.now() + 1500;                  // don't let the 2s refresh race this click
  try{
    if(act==="remove"){
      if(!confirm(`Remove ${addr} from the fleet? (its inbox/identity stays)`)) return;
      await j("/fleet/"+encodeURIComponent(addr),{method:"DELETE"});
    } else {
      await j(`/fleet/${encodeURIComponent(addr)}/${act}`,{method:"POST"});
    }
    setStatus(`${addr}: ${act}`,"ok"); await loadAgents(); await refreshFleetList(true);
  }catch(e){ setStatus("⚠ "+String(e.message||e).slice(0,120),"err"); }
}
$("fleetBtn").onclick = openFleet;

(async function boot(){
  await loadAgents();
  const asParam = new URLSearchParams(location.search).get("as");   // ?as=<address> deep-links to an identity
  if(asParam && AGENTS.some(a=>a.address===asParam)) current = asParam;
  if(current!=="all" && !AGENTS.some(a=>a.address===current)) current = "all";
  await setIdentity(current);
  if(location.search.includes("compose")) openCompose();           // ?compose opens the new-message form
  if(!location.search.includes("nolive")) connectLive();           // ?nolive skips SSE (used for screenshots/tests)
})();

async function setIdentity(idn){
  leaveFleet();
  current = idn; localStorage.setItem("postbox.identity", idn);
  composeMode = false;
  $("sideName").textContent = idn==="all" ? "All activity" : agentByAddr(idn).name;
  await loadThreads();
  openThread = THREADS.length ? THREADS[0].thread_id : null;
  renderSide();
  if(openThread) await selectThread(openThread);
  else { $("msgs").innerHTML = '<div class="empty">No threads yet — hit “✎ New message” to start one.</div>'; $("mTitle").textContent="#"; $("mSub").textContent=""; $("mAvs").innerHTML=""; }
}
