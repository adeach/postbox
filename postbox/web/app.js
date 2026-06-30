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
const j = async (u, o) => { const r = await fetch(API+u, o); if(!r.ok) throw new Error(await r.text()); return r.status===204?null:r.json(); };

async function loadAgents(){ AGENTS = await j("/observer/agents"); }
function agentByAddr(a){ return AGENTS.find(x=>x.address===a) || {address:a,name:a,status:"offline"}; }
function isOnline(a){ return agentByAddr(a).status !== "offline"; }

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
  const opt = (addr,name,globe,online,you,unread)=>{
    const o = document.createElement("div"); o.className="opt";
    const col = globe ? "#5a2b5c" : colorFor(addr);
    o.innerHTML = `<span class="av ${globe?'globe':''}" style="background:${col}">${globe?'🌐':initials(name)}${globe?'':`<span class="pres ${online?'':'off'}"></span>`}</span>
      <span class="nm">${esc(name)}</span>${you?'<span class="you">you</span>':''}
      ${unread?`<span class="badge">${unread}</span>`:''}${(globe?'all':addr)===current?'<span class="ck">✓</span>':''}`;
    o.onclick = e=>{ e.stopPropagation(); setIdentity(globe?'all':addr); closeMenu(); };
    m.appendChild(o);
  };
  opt(null,"All activity",true,true,false,0);
  const sep = document.createElement("div"); sep.className="sepm"; m.appendChild(sep);
  AGENTS.forEach(a=> opt(a.address, a.name, false, a.status!=="offline", !!a.profile?.human, 0));
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

// delivery/read receipt for a message, from its recipients' read state
function receiptHtml(m){
  if(!m.to || !m.to.length) return "";
  const unread = m.to.filter(r=> !(m.read_by||[]).includes(r));
  if(unread.length === 0) return `<span class="rcpt read" title="Read by recipient">✓✓ Read</span>`;
  const offline = unread.filter(r=> !isOnline(r));
  const note = offline.length ? ` · ${esc(offline.join(", "))} offline` : "";
  return `<span class="rcpt" title="Delivered to inbox; not read yet">✓ Delivered${note}</span>`;
}

async function selectThread(tid){
  composeMode = false;
  openThread = tid;
  const d = await j("/observer/threads/"+tid);
  $("mTitle").textContent = "# "+(d.subject||"(no subject)");
  $("mSub").textContent = d.members.join(" ↔ ");
  const obs = current==="all" || !d.members.includes(current);
  const ot = $("obsTag"); ot.style.display = obs?"inline-block":"none"; ot.textContent = current==="all"?"all activity":"observing";
  const avs = $("mAvs"); avs.innerHTML = "";
  d.members.forEach(mm=>{
    const x=document.createElement("div"); x.className="a"; x.style.background=colorFor(mm); x.title=mm+(isOnline(mm)?" (online)":" (offline)");
    x.innerHTML = initials(mm)+`<span class="pres ${isOnline(mm)?'':'off'}"></span>`;
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
}

function openCompose(){
  if(current === "all"){ setStatus("Open as an identity first (top-left), then compose.", "err"); return; }
  composeMode = true; openThread = null;
  $("mTitle").textContent = "✎ New message";
  $("mSub").textContent = "from "+current;
  $("obsTag").style.display = "none"; $("mAvs").innerHTML = "";
  const others = AGENTS.filter(a=> a.address !== current);
  const opts = others.map(a=>`<option value="${a.address}">${esc(a.name)} ${a.status==="offline"?"— offline":"— online"}</option>`).join("");
  $("msgs").innerHTML = `<div class="composeform">
    <label>To</label>
    <select id="cTo">${opts || '<option value="">(no other agents yet)</option>'}</select>
    <label>Subject <span class="opt">(optional)</span></label>
    <input id="cSubj" placeholder="e.g. quick question" autocomplete="off">
    <div class="hint">Type your message in the box below and press <b>Enter</b> to send.
    The recipient is poked in real time; you'll see <b>✓ Delivered</b> the instant it's in their inbox, then <b>✓✓ Read</b> once they open it.</div>
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
    setStatus(`✓ Delivered to ${to}${isOnline(to)?"" : " (offline — will see when online)"}`, "ok");
  }catch(e){
    setStatus("⚠ Failed to send — "+String(e.message||e).slice(0,80), "err");
    input.value = txt;  // restore so it isn't lost
  }
}
sendBtn.onclick = doSend;
input.addEventListener("keydown", e=>{ if(e.key==="Enter"){ e.preventDefault(); doSend(); }});

function connectLive(){
  const es = new EventSource("/observer/events");
  const refresh = async ()=>{ await loadThreads(); renderSide(); if(openThread && !composeMode) await selectThread(openThread); };
  es.addEventListener("message.received", refresh);
  es.addEventListener("message.read", refresh);   // flips ✓ Delivered → ✓✓ Read live
  es.onerror = ()=>{};  // browser auto-reconnects
}

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
  current = idn; localStorage.setItem("postbox.identity", idn);
  composeMode = false;
  $("sideName").textContent = idn==="all" ? "All activity" : agentByAddr(idn).name;
  await loadThreads();
  openThread = THREADS.length ? THREADS[0].thread_id : null;
  renderSide();
  if(openThread) await selectThread(openThread);
  else { $("msgs").innerHTML = '<div class="empty">No threads yet — hit “✎ New message” to start one.</div>'; $("mTitle").textContent="#"; $("mSub").textContent=""; $("mAvs").innerHTML=""; }
}
