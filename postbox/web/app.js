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
let YOU = localStorage.getItem("postbox.you") || null;   // your (human) identity address
let viewer = null;                                 // identity we're viewing/sending as (Stage 4: impersonation)
let THREADS = [];
let openId = null;
let panel = "chat";                                // chat | fleet (Stage 5)
let _lastMsgId = null, _openOther = null;

const agentByAddr = a => AGENTS.find(x=>x.address===a) || {address:a, name:a, status:"offline", profile:null};
const nameOf   = a => agentByAddr(a).name;
const isHuman  = a => !!agentByAddr(a).profile?.human;
const isOnline = a => !isHuman(a) && agentByAddr(a).status === "online";
const impersonating = () => viewer !== YOU;
const otherOf = t => (t.members||[]).find(m=>m!==viewer) || (t.members||[])[0] || "";
const unreadFor = t => (t.unread && t.unread[viewer]) || 0;

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
  const list = $("dmList"); list.innerHTML = "";
  THREADS.forEach(t=>{
    const o = otherOf(t), un = unreadFor(t);
    const row = document.createElement("div");
    row.className = "dm" + (panel==="chat" && openId===t.thread_id ? " active" : "") + (un?" unread":"");
    row.dataset.act = "open"; row.dataset.val = t.thread_id;
    row.innerHTML = `<span class="dav" style="background:${colorFor(o)}">${initials(nameOf(o))}${isHuman(o)?"":`<span class="pres ${isOnline(o)?'':'off'}"></span>`}</span>`
      + `<span class="nm">${esc(nameOf(o))}</span>` + (un?`<span class="b">${un}</span>`:"");
    list.appendChild(row);
  });
  if(!THREADS.length) list.innerHTML = `<div style="color:var(--side-ink-dim);font-size:13px;padding:4px 14px">No conversations yet — search above.</div>`;
}

function emptyMain(){
  $("mTitle").textContent = "Messages"; $("mSub").textContent = "";
  $("msgs").innerHTML = `<div class="empty"><div class="emptybox"><div class="et">No conversation open</div><div class="eh">Search for someone above to start a direct message${impersonating()?` as ${esc(nameOf(viewer))}`:''}.</div></div></div>`;
  showComposer(false);
}

async function showThread(id){
  openId = id; panel = "chat";
  const d = await j("/observer/threads/"+encodeURIComponent(id));
  const o = (d.members||[]).find(m=>m!==viewer) || (d.members||[])[0] || "";
  _openOther = o;
  $("mTitle").textContent = nameOf(o);
  $("mSub").textContent = isHuman(o) ? "person" : (isOnline(o) ? "online" : "offline");
  const msgs = $("msgs"); msgs.innerHTML = "";
  (d.messages||[]).forEach(m=>{
    const row = document.createElement("div"); row.className = "msg";
    row.innerHTML = `<div class="av" style="background:${colorFor(m.from)}">${initials(nameOf(m.from))}</div>`
      + `<div><div class="l1"><span class="who">${esc(nameOf(m.from))}</span><span class="t">${(m.created_at||'').slice(11,16)}</span></div>`
      + `<div class="txt">${esc(m.body)}</div>${receiptHtml(m)}</div>`;
    msgs.appendChild(row);
  });
  msgs.scrollTop = msgs.scrollHeight;
  _lastMsgId = (d.messages && d.messages.length) ? d.messages[d.messages.length-1].id : null;
  showComposer(true);
  $("cinput").placeholder = `Message ${nameOf(o)}${impersonating()?` as ${nameOf(viewer)}`:''}…`;
  // email-style auto-read — ONLY as a human reading their own inbox; never when impersonating an agent.
  if(!impersonating() && isHuman(viewer) && (d.members||[]).includes(viewer)){
    try{
      const res = await j("/observer/read", {method:"POST", headers:{'content-type':'application/json'},
        body: JSON.stringify({as: viewer, thread_id: id})});
      if(res && res.marked){ await loadThreads(); }
    }catch(e){ /* non-fatal */ }
  }
  renderSidebar();
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

function connectLive(){
  const url = "/observer/events" + (OBS_TOKEN ? "?token="+encodeURIComponent(OBS_TOKEN) : "");
  const es = new EventSource(url);
  const refresh = async ()=>{
    if(panel==="fleet") return;                 // Stage 5 refreshes the fleet separately
    await loadThreads(); renderSidebar();
    if(openId) await showThread(openId);
  };
  es.addEventListener("message.received", refresh);
  es.addEventListener("message.read", refresh);   // flips ✓ Delivered → ✓✓ Read live
  es.onerror = ()=>{};                            // browser auto-reconnects
}

$("cinput").addEventListener("input", e=> $("send").classList.toggle("on", !!e.target.value.trim()));
$("cinput").addEventListener("keydown", e=>{ if(e.key==="Enter"){ e.preventDefault(); doSend(); }});
document.addEventListener("click", e=>{
  const t = e.target.closest("[data-act]"); if(!t) return;
  e.preventDefault();
  const a = t.dataset.act;
  if(a==="open") return showThread(t.dataset.val);
  if(a==="send") return doSend();
  // search (Stage 3), vas/asme/setviewer (Stage 4), showfleet/fleet (Stage 5) wired in later stages
});

(async function boot(){
  await loadAgents();
  await ensureYou();
  viewer = YOU;
  await loadThreads();
  renderSidebar();
  openId = THREADS.length ? THREADS[0].thread_id : null;
  if(openId) await showThread(openId); else emptyMain();
  connectLive();
})();
