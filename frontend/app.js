const $ = (id) => document.getElementById(id);

const state = {
  view: "inbox",
  adminToken: localStorage.getItem("ADMIN_DASH_TOKEN") || "",
  numbers: [],
  selectedNumber: "",
  direction: "",
  limit: 100,
  offset: 0,
};

function setSubtitle(text) {
  $("subtitle").textContent = text;
}

function setConnStatus(ok) {
  $("connStatus").textContent = ok ? "Connected" : "Disconnected";
}

function apiHeaders() {
  return {
    "Content-Type": "application/json",
    "X-Admin-Token": state.adminToken,
  };
}

function fmtTs(iso) {
  try {
    const d = new Date(iso);
    return d.toLocaleString();
  } catch {
    return iso;
  }
}

function showStatus(elId, text) {
  $(elId).textContent = text || "";
}

function switchView(view) {
  state.view = view;

  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === view);
  });

  $("view-inbox").classList.toggle("hidden", view !== "inbox");
  $("view-bookings").classList.toggle("hidden", view !== "bookings");
  $("view-cache").classList.toggle("hidden", view !== "cache");

  if (view === "inbox") setSubtitle("Inbox overview");
  if (view === "bookings") setSubtitle("Booking admin");
  if (view === "cache") setSubtitle("Cache test and timings");

  if (view === "bookings") loadBookings();
}


async function apiGet(url) {
  const res = await fetch(url, {
    headers: {
      "X-Admin-Token": state.adminToken
    }
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `HTTP ${res.status}`);
  }
  return res.json();
}

async function loadBookings() {
  showStatus("bookingsStatus", "Loading bookings...");

  try {
    const limit = Number($("bookingLimit").value || 50);

    const data = await apiGet(`/api/bookings/pending?limit=${encodeURIComponent(limit)}`);
    const items = data.items || [];

    console.log("bookings sample:", items[0]);

    renderBookings(items);

    setConnStatus(true);
    showStatus("bookingsStatus", items.length === 0 ? "No pending booking requests." : "");
  } catch (e) {
    setConnStatus(false);
    showStatus("bookingsStatus", `Bookings load failed: ${e.message}`);
  }
}

function renderBookings(items) {
  const body = $("bookingsTbody");
  body.innerHTML = "";

  for (const b of items) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${b.created_ts ? fmtTs(b.created_ts) : "-"}</td>
      <td>${escapeHtml(b.customer_number ?? "")}</td>
      <td>${escapeHtml(b.service_label ?? "")}</td>
      <td>${b.start_ts && b.end_ts ? `${fmtTs(b.start_ts)} – ${fmtTs(b.end_ts)}` : escapeHtml(b.start_ts ?? "")}</td>
      <td>${escapeHtml(b.decision_status ?? "pending")}</td>
      <td>${escapeHtml(String(b.id ?? ""))}</td>
    `;
    body.appendChild(tr);
  }
}




async function loadNumbers() {
  showStatus("inboxStatus", "Loading numbers...");
  try {
    const data = await apiGet(`/api/numbers?limit=200`);
    state.numbers = data.items || [];
    setConnStatus(true);
    renderNumbers();
    renderKpis(data.totals);
    showStatus("inboxStatus", "");
  } catch (e) {
    setConnStatus(false);
    showStatus("inboxStatus", `Numbers load failed: ${e.message}`);
  }
}

function renderNumbers() {
  const q = ($("numberSearch").value || "").trim();
  const root = $("numbersList");
  root.innerHTML = "";

  const items = state.numbers.filter((x) => {
    if (!q) return true;
    return String(x.phone_number).includes(q);
  });

  if (items.length === 0) {
    root.innerHTML = `<div class="status">No numbers found.</div>`;
    return;
  }

  for (const it of items) {
    const el = document.createElement("div");
    el.className = "list-item" + (it.phone_number === state.selectedNumber ? " active" : "");
    el.innerHTML = `
      <div>
        <div class="num">${it.phone_number}</div>
        <div class="meta">Last: ${it.last_ts ? fmtTs(it.last_ts) : "-"}</div>
      </div>
      <div class="count">${it.msg_count}</div>
    `;
    el.addEventListener("click", () => {
      state.selectedNumber = it.phone_number;
      state.offset = 0;
      renderNumbers();
      loadMessages();
    });
    root.appendChild(el);
  }
}

function renderKpis(extra) {
  const unique = state.numbers.length;
  let total = 0;
  for (const it of state.numbers) total += Number(it.msg_count || 0);

  $("kpiNumbers").textContent = String(unique);
  $("kpiMsgs").textContent = String(total);

  if (extra) {
    $("kpiIn").textContent = String(extra.in_count ?? "-");
    $("kpiOut").textContent = String(extra.out_count ?? "-");
  } else {
    $("kpiIn").textContent = "-";
    $("kpiOut").textContent = "-";
  }
}

function badge(dir) {
  const cls = dir === "in" ? "badge in" : "badge out";
  const label = dir === "in" ? "IN" : "OUT";
  return `<span class="${cls}">${label}</span>`;
}

async function loadMessages() {
  if (!state.selectedNumber) {
    showStatus("inboxStatus", "Select a number on the left to view messages.");
    $("messagesTbody").innerHTML = "";
    return;
  }

  state.direction = $("directionFilter").value;
  state.limit = Number($("limitSelect").value || 100);

  showStatus("inboxStatus", "Loading messages...");
  try {
    const params = new URLSearchParams();
    params.set("phone_number", state.selectedNumber);
    if (state.direction) params.set("direction", state.direction);
    params.set("limit", String(state.limit));
    params.set("offset", String(state.offset));

    const data = await apiGet(`/api/messages?${params.toString()}`);
    const items = data.items || [];

    setConnStatus(true);
    renderMessages(items);
    $("pageLabel").textContent = `Page ${Math.floor(state.offset / state.limit) + 1}`;
    showStatus("inboxStatus", items.length === 0 ? "No messages found for this filter." : "");
  } catch (e) {
    setConnStatus(false);
    showStatus("inboxStatus", `Messages load failed: ${e.message}`);
  }
}

function renderMessages(items) {
  const body = $("messagesTbody");
  body.innerHTML = "";

  for (const m of items) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${fmtTs(m.ts)}</td>
      <td>${badge(m.direction)}</td>
      <td>${escapeHtml(m.text)}</td>
    `;
    body.appendChild(tr);
  }
}

function escapeHtml(s) {
  return String(s || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

/* Cache test (kept from your old UI) */
function appendLog(obj) {
  const line = JSON.stringify(obj);
  $("log").textContent = line + "\n" + $("log").textContent;
}

function setMetrics(r) {
  $("mCacheHit").textContent = String(r.cache_hit);
  $("mRetrieval").textContent = String(r.t_retrieval_ms);
  $("mTotal").textContent = String(r.t_total_ms);
  $("mContextLen").textContent = String(r.context_len);
}

async function callCacheOnce() {
  const payload = {
    from_number: $("fromNumber").value.trim(),
    text: $("question").value.trim(),
    disable_cache: $("disableCache").checked
  };

  const res = await fetch("/debug/cache_test", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });

  const data = await res.json().catch(() => ({}));
  appendLog({ status: res.status, ...data });
  if (data.ok) setMetrics(data);

  if (!res.ok) {
    showStatus("cacheStatus", `Cache test failed: ${data.detail || data.error || "Unknown error"}`);
  } else {
    showStatus("cacheStatus", "");
  }
}

/* Wire events */
document.querySelectorAll(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {
    switchView(btn.dataset.view);
  });
});

const loadBookingsBtn = $("loadBookingsBtn");
if (loadBookingsBtn) {
  loadBookingsBtn.addEventListener("click", async () => {
    await loadBookings();
  });
}

$("refreshBtn").addEventListener("click", async () => {
  if (state.view === "inbox") {
    await loadNumbers();
    await loadMessages();
  }
  if (state.view === "bookings") {
    await loadBookings();
  }
});


$("numberSearch").addEventListener("input", () => renderNumbers());

$("loadMsgsBtn").addEventListener("click", async () => {
  state.offset = 0;
  await loadMessages();
});

$("prevBtn").addEventListener("click", async () => {
  state.offset = Math.max(0, state.offset - state.limit);
  await loadMessages();
});

$("nextBtn").addEventListener("click", async () => {
  state.offset = state.offset + state.limit;
  await loadMessages();
});

$("sendBtn").addEventListener("click", async () => {
  $("sendBtn").disabled = true;
  try { await callCacheOnce(); }
  finally { $("sendBtn").disabled = false; }
});

$("sendTwiceBtn").addEventListener("click", async () => {
  $("sendTwiceBtn").disabled = true;
  try {
    await callCacheOnce();
    await callCacheOnce();
  } finally {
    $("sendTwiceBtn").disabled = false;
  }
});

$("clearLogBtn").addEventListener("click", () => {
  $("log").textContent = "";
  $("mCacheHit").textContent = "-";
  $("mRetrieval").textContent = "-";
  $("mTotal").textContent = "-";
  $("mContextLen").textContent = "-";
  showStatus("cacheStatus", "");
});

function openAdminModal(msg){
  const m = $("adminTokenModal");
  const e = $("adminTokenModalError");
  if (msg){
    e.textContent = msg;
    e.style.display = "block";
  } else {
    e.style.display = "none";
  }
  setTimeout(() => $("adminTokenModalInput").focus(), 50);
  m.classList.add("is-open");
}

function closeAdminModal(){
  $("adminTokenModal").classList.remove("is-open");
}

async function saveTokenFromModal(){
  const v = $("adminTokenModalInput").value.trim();
  if (!v){
    openAdminModal("Token cannot be empty");
    return;
  }

  state.adminToken = v;
  localStorage.setItem("ADMIN_DASH_TOKEN", v);
  setConnStatus(true);
  closeAdminModal();

  await loadNumbers();
  if (state.selectedNumber) await loadMessages();

}

$("adminTokenModalSaveBtn").addEventListener("click", saveTokenFromModal);
$("adminTokenModalInput").addEventListener("keydown", e => {
  if (e.key === "Enter") saveTokenFromModal();
});

/* Boot */
switchView("inbox");

if (!state.adminToken) {
  openAdminModal();
  setConnStatus(false);
} else {
  loadNumbers().then(() => loadMessages());
}
