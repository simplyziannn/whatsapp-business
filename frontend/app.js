const $ = (id) => document.getElementById(id);

const state = {
  view: "inbox",
  authToken: localStorage.getItem("DASH_AUTH_TOKEN") || "",
  authUser: JSON.parse(localStorage.getItem("DASH_AUTH_USER") || "null"),
  scopeCompanyId: Number(localStorage.getItem("DASH_SCOPE_COMPANY_ID") || "0") || null,
  theme: localStorage.getItem("DASH_THEME") || "dark",
  numbers: [],
  selectedNumber: "",
  direction: "",
  limit: 100,
  offset: 0,
  companies: [],
  kbFolders: [],
  selectedKbFolder: "",
  kbDirty: false,
};

let bookingsCalendar = null;
let bookingsCalendarInited = false;
let autoTimer = null;
let autoInFlight = false;

const AUTO_POLL_MS = 6000;

function setSubtitle(text) {
  $("subtitle").textContent = text;
}

function setConnStatus(ok) {
  $("connStatus").textContent = ok ? "Connected" : "Disconnected";
}

function applyTheme(theme) {
  const normalized = theme === "light" ? "light" : "dark";
  state.theme = normalized;
  document.body.classList.toggle("theme-light", normalized === "light");
  localStorage.setItem("DASH_THEME", normalized);
  $("themeToggle").textContent = normalized === "light" ? "Dark Mode" : "Light Mode";
}

function authHeaders(extra = {}) {
  const headers = { "Content-Type": "application/json", ...extra };
  if (state.authToken) headers.Authorization = `Bearer ${state.authToken}`;
  return headers;
}

function showStatus(elId, text) {
  const el = $(elId);
  if (el) el.textContent = text || "";
}

function setKbDirty(isDirty) {
  state.kbDirty = !!isDirty;
  const saveBtn = $("kbSaveBtn");
  if (saveBtn) {
    saveBtn.textContent = state.kbDirty ? "Save Folder to DB *" : "Save Folder to DB";
  }
}

function fmtTs(iso) {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function escapeHtml(s) {
  return String(s || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function badge(dir) {
  const cls = dir === "in" ? "badge in" : "badge out";
  const label = dir === "in" ? "IN" : "OUT";
  return `<span class="${cls}">${label}</span>`;
}

async function apiGet(url) {
  const res = await fetch(url, { headers: authHeaders() });
  if (!res.ok) {
    if (res.status === 401 || res.status === 403) {
      forceLogout("Session expired. Please login again.");
      throw new Error("Unauthorized");
    }
    throw new Error((await res.text()) || `HTTP ${res.status}`);
  }
  return res.json();
}

async function apiPost(url, bodyObj = null) {
  const res = await fetch(url, {
    method: "POST",
    headers: authHeaders(),
    body: bodyObj ? JSON.stringify(bodyObj) : null,
  });
  if (!res.ok) {
    if (res.status === 401 || res.status === 403) {
      forceLogout("Session expired. Please login again.");
      throw new Error("Unauthorized");
    }
    throw new Error((await res.text()) || `HTTP ${res.status}`);
  }
  return res.headers.get("content-type")?.includes("application/json") ? res.json() : null;
}

async function apiDelete(url) {
  const res = await fetch(url, {
    method: "DELETE",
    headers: authHeaders(),
  });
  if (!res.ok) {
    if (res.status === 401 || res.status === 403) {
      forceLogout("Session expired. Please login again.");
      throw new Error("Unauthorized");
    }
    throw new Error((await res.text()) || `HTTP ${res.status}`);
  }
  return res.headers.get("content-type")?.includes("application/json") ? res.json() : null;
}

function openLoginModal(msg = "") {
  const m = $("adminTokenModal");
  const e = $("adminTokenModalError");
  if (msg) {
    e.textContent = msg;
    e.style.display = "block";
  } else {
    e.style.display = "none";
  }
  m.classList.add("is-open");
  setTimeout(() => $("loginUserInput").focus(), 40);
}

function closeLoginModal() {
  $("adminTokenModal").classList.remove("is-open");
}

function renderAuthButton() {
  const btn = $("authBtn");
  const loggedIn = !!state.authToken;
  btn.textContent = loggedIn ? "Logout" : "Login";
  btn.classList.toggle("danger", loggedIn);
  btn.classList.toggle("primary", !loggedIn);

  const scopedCompany = state.companies.find((c) => Number(c.id) === Number(state.scopeCompanyId));
  const companyLabel = scopedCompany?.name
    || (state.authUser?.role === "platform_admin" ? "Platform Admin" : null)
    || state.authUser?.company_name
    || state.authUser?.username
    || "Dashboard";
  const scopeSuffix = state.authUser?.role === "platform_admin" && state.scopeCompanyId ? ` #${state.scopeCompanyId}` : "";
  const companyName = companyLabel + scopeSuffix;
  $("companyName").textContent = companyName;

  const isAdmin = state.authUser?.role === "platform_admin";
  $("adminNavBtn")?.classList.toggle("hidden", !isAdmin);
}

function saveSession(token, user) {
  state.authToken = token;
  state.authUser = user;
  localStorage.setItem("DASH_AUTH_TOKEN", token);
  localStorage.setItem("DASH_AUTH_USER", JSON.stringify(user));
  if (user?.role === "platform_admin") {
    setScopeCompanyId(null);
  } else {
    setScopeCompanyId(user?.company_id || null);
  }
}

function clearSession() {
  state.authToken = "";
  state.authUser = null;
  state.kbFolders = [];
  state.selectedKbFolder = "";
  state.kbDirty = false;
  localStorage.removeItem("DASH_AUTH_TOKEN");
  localStorage.removeItem("DASH_AUTH_USER");
  localStorage.removeItem("DASH_SCOPE_COMPANY_ID");
  state.scopeCompanyId = null;
}

function setScopeCompanyId(companyId) {
  state.scopeCompanyId = companyId ? Number(companyId) : null;
  if (state.scopeCompanyId) localStorage.setItem("DASH_SCOPE_COMPANY_ID", String(state.scopeCompanyId));
  else localStorage.removeItem("DASH_SCOPE_COMPANY_ID");
  renderAuthButton();
}

function appendScopeParams(params) {
  if (state.authUser?.role !== "platform_admin") return;
  if (state.scopeCompanyId) {
    params.set("company_id", String(state.scopeCompanyId));
    return;
  }
  params.set("all_companies", "1");
}

function renderCompanyCards() {
  const root = $("companyCards");
  if (!root) return;
  root.innerHTML = "";

  if (!state.companies.length) {
    root.innerHTML = `<div class="status">No businesses found yet.</div>`;
    return;
  }

  for (const company of state.companies) {
    const isActive = Number(state.scopeCompanyId) === Number(company.id);
    const isLive = !!company.whatsapp_phone_number_id;
    const card = document.createElement("div");
    card.className = "company-card" + (isActive ? " active" : "");
    card.innerHTML = `
      <div class="company-card-name">${escapeHtml(company.name)}</div>
      <div class="company-card-meta">ID #${company.id}${company.whatsapp_phone_number_id ? ` · Phone ID ${escapeHtml(company.whatsapp_phone_number_id)}` : ""}</div>
      <div class="company-card-actions">
        <button class="btn company-card-btn company-card-open" type="button">${isLive ? "Live · View logs" : "Setup pending · View logs"}</button>
        <button class="btn company-card-btn company-card-delete" type="button" ${company.is_default ? "disabled" : ""}>Delete</button>
      </div>
    `;

    card.querySelector(".company-card-open")?.addEventListener("click", async () => {
      setScopeCompanyId(company.id);
      renderCompanyCards();
      setSubtitle(`Viewing logs for ${company.name}`);
      switchView("inbox");
      await loadNumbers();
      await loadMessages();
    });

    card.querySelector(".company-card-delete")?.addEventListener("click", async () => {
      if (company.is_default) {
        showStatus("companyCardsStatus", "Default company cannot be deleted.");
        return;
      }
      const ok = confirm(`Delete company "${company.name}"?\nThis will delete its user accounts and sessions.`);
      if (!ok) return;
      showStatus("companyCardsStatus", `Deleting ${company.name}...`);
      try {
        await apiDelete(`/api/admin/companies/${company.id}`);
        if (Number(state.scopeCompanyId) === Number(company.id)) {
          setScopeCompanyId(null);
          state.selectedNumber = "";
          state.numbers = [];
          renderNumbers();
          renderMessages([]);
          renderKpis({ in_count: 0, out_count: 0 });
        }
        await loadCompanies();
        showStatus("companyCardsStatus", `Deleted ${company.name}.`);
      } catch (e) {
        showStatus("companyCardsStatus", `Delete failed: ${e.message}`);
      }
    });

    root.appendChild(card);
  }
}

async function loadCompanies() {
  if (state.authUser?.role !== "platform_admin") return;
  showStatus("companyCardsStatus", "Loading businesses...");
  try {
    const data = await apiGet("/api/admin/companies?limit=300");
    state.companies = data.items || [];
    renderCompanyCards();
    renderAuthButton();
    showStatus("companyCardsStatus", "");
  } catch (e) {
    showStatus("companyCardsStatus", `Businesses load failed: ${e.message}`);
  }
}

function renderKbFolderCards() {
  const root = $("kbFolderCards");
  if (!root) return;
  root.innerHTML = "";

  if (!state.kbFolders.length) {
    root.innerHTML = `<div class="status">No embedded folders found.</div>`;
    return;
  }

  for (const item of state.kbFolders) {
    const active = item.folder === state.selectedKbFolder;
    const sourcePreview = (item.source_files || []).slice(0, 2).join(", ") || "No source file metadata";
    const card = document.createElement("button");
    card.type = "button";
    card.className = "kb-folder-card" + (active ? " active" : "");
    card.innerHTML = `
      <div class="kb-folder-name">${escapeHtml(item.folder)}</div>
      <div class="kb-folder-meta">Collection: ${escapeHtml(item.collection)}</div>
      <div class="kb-folder-meta">Chunks: ${Number(item.chunk_count || 0)}</div>
      <div class="kb-folder-meta">Source: ${escapeHtml(sourcePreview)}</div>
    `;
    card.addEventListener("click", async () => {
      await loadKbFolderContent(item.folder);
    });
    root.appendChild(card);
  }
}

async function loadKbFolders(options = {}) {
  const { loadEditor = true } = options;
  if (!state.authToken) return;
  showStatus("kbFoldersStatus", "Loading embedded folders...");
  try {
    const data = await apiGet("/api/kb/folders");
    state.kbFolders = data.items || [];
    if (!state.selectedKbFolder && state.kbFolders.length > 0) {
      state.selectedKbFolder = state.kbFolders[0].folder;
    }
    renderKbFolderCards();
    showStatus("kbFoldersStatus", "");
    if (state.kbDirty) {
      showStatus("kbEditorStatus", "Unsaved changes detected. Editor content not auto-reloaded.");
      return;
    }
    if (loadEditor && state.selectedKbFolder) {
      await loadKbFolderContent(state.selectedKbFolder);
    }
  } catch (e) {
    showStatus("kbFoldersStatus", `Load failed: ${e.message}`);
  }
}

async function loadKbFolderContent(folder) {
  if (!folder) return;
  if (state.kbDirty && state.selectedKbFolder && folder !== state.selectedKbFolder) {
    const ok = confirm("You have unsaved KB edits. Discard them and open another folder?");
    if (!ok) return;
  }
  showStatus("kbEditorStatus", `Loading ${folder} content from pgvector...`);
  try {
    const data = await apiGet(`/api/kb/folders/${encodeURIComponent(folder)}`);
    state.selectedKbFolder = folder;
    renderKbFolderCards();
    $("kbEditorLabel").textContent = `Editing "${folder}" · ${Number(data.chunk_count || 0)} chunk(s) in ${data.collection}`;
    $("kbEditorText").value = data.content || "";
    $("kbEditorText").disabled = false;
    $("kbSaveBtn").disabled = false;
    setKbDirty(false);
    showStatus("kbEditorStatus", "");
  } catch (e) {
    $("kbEditorText").value = "";
    $("kbEditorText").disabled = true;
    $("kbSaveBtn").disabled = true;
    showStatus("kbEditorStatus", `Load failed: ${e.message}`);
  }
}

async function saveKbFolderContent() {
  const folder = state.selectedKbFolder;
  if (!folder) {
    showStatus("kbEditorStatus", "Select a folder first.");
    return;
  }
  const content = $("kbEditorText").value;
  $("kbSaveBtn").disabled = true;
  showStatus("kbEditorStatus", `Saving "${folder}" and re-embedding into pgvector...`);
  try {
    const result = await apiPost(`/api/kb/folders/${encodeURIComponent(folder)}`, { content });
    setKbDirty(false);
    showStatus(
      "kbEditorStatus",
      `Saved ${folder}. Updated ${Number(result.chunk_count || 0)} chunk(s) in ${result.collection}.`
    );
    await loadKbFolders({ loadEditor: false });
  } catch (e) {
    showStatus("kbEditorStatus", `Save failed: ${e.message}`);
  } finally {
    $("kbSaveBtn").disabled = false;
  }
}

function forceLogout(msg = "Logged out.") {
  stopAutoRefresh();
  clearSession();
  setConnStatus(false);
  renderAuthButton();
  switchView("inbox", true);
  openLoginModal(msg);
}

async function doLogin() {
  const username = $("loginUserInput").value.trim();
  const password = $("loginPassInput").value;
  if (!username || !password) {
    openLoginModal("Username and password are required.");
    return;
  }

  $("adminTokenModalSaveBtn").disabled = true;
  try {
    const data = await apiPost("/api/auth/login", { username, password });
    saveSession(data.token, data.user);
    closeLoginModal();
    setConnStatus(true);
    renderAuthButton();

    if (state.authUser?.role === "platform_admin") {
      await loadCompanies();
      switchView("admin");
      startAutoRefresh();
      return;
    }
    await loadNumbers();
    if (state.selectedNumber) await loadMessages();
    startAutoRefresh();
  } catch (err) {
    openLoginModal(`Login failed: ${err.message}`);
  } finally {
    $("adminTokenModalSaveBtn").disabled = false;
  }
}

async function logout() {
  try {
    if (state.authToken) {
      await fetch("/api/auth/logout", { method: "POST", headers: authHeaders() });
    }
  } catch {}
  forceLogout("Logged out.");
}

function switchView(view, bypassAuthGuard = false) {
  state.view = view;

  if (!bypassAuthGuard && !state.authToken) {
    view = "inbox";
    state.view = "inbox";
    openLoginModal("Login required.");
  }

  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === view);
  });

  $("view-inbox").classList.toggle("hidden", view !== "inbox");
  $("view-bookings").classList.toggle("hidden", view !== "bookings");
  $("view-kb").classList.toggle("hidden", view !== "kb");
  $("view-admin").classList.toggle("hidden", view !== "admin");

  if (view === "inbox") {
    setSubtitle("Inbox overview");
    if (state.authUser?.role === "platform_admin" && !state.scopeCompanyId) {
      state.numbers = [];
      state.selectedNumber = "";
      renderNumbers();
      renderMessages([]);
      renderKpis({ in_count: 0, out_count: 0 });
      showStatus("inboxStatus", "Select a business in Admin to view logs.");
    } else if (state.authToken) {
      loadNumbers().then(() => loadMessages());
    }
  }
  if (view === "bookings") {
    setSubtitle("Booking admin");
    initBookingsCalendarIfNeeded();
    if (state.authToken) loadBookings();
  }
  if (view === "kb") {
    setSubtitle("Knowledge base editor");
    if (state.authToken) loadKbFolders({ loadEditor: true });
  }
  if (view === "admin") {
    setSubtitle("Platform administration");
    if (state.authUser?.role !== "platform_admin") {
      showStatus("signupReqStatus", "Admin view is available to platform admins only.");
    } else {
      loadCompanies();
      loadSignupRequests();
    }
  }

  startAutoRefresh();
}

function renderKpis(extra) {
  const unique = state.numbers.length;
  let total = 0;
  for (const it of state.numbers) total += Number(it.msg_count || 0);

  $("kpiNumbers").textContent = String(unique);
  $("kpiMsgs").textContent = String(total);
  $("kpiIn").textContent = String(extra?.in_count ?? "-");
  $("kpiOut").textContent = String(extra?.out_count ?? "-");
}

function renderNumbers() {
  const q = ($("numberSearch").value || "").trim();
  const root = $("numbersList");
  root.innerHTML = "";

  const items = state.numbers.filter((x) => !q || String(x.phone_number).includes(q));
  if (items.length === 0) {
    root.innerHTML = `<div class="status">No numbers found.</div>`;
    return;
  }

  for (const it of items) {
    const el = document.createElement("div");
    el.className = "list-item" + (it.phone_number === state.selectedNumber ? " active" : "");
    el.innerHTML = `
      <div>
        <div class="num">${escapeHtml(it.phone_number)}</div>
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

async function loadNumbers() {
  if (state.authUser?.role === "platform_admin" && !state.scopeCompanyId) {
    state.numbers = [];
    state.selectedNumber = "";
    renderNumbers();
    renderMessages([]);
    renderKpis({ in_count: 0, out_count: 0 });
    showStatus("inboxStatus", "Select a business in Admin to view logs.");
    return;
  }
  showStatus("inboxStatus", "Loading numbers...");
  try {
    const params = new URLSearchParams();
    params.set("limit", "200");
    appendScopeParams(params);
    const data = await apiGet(`/api/numbers?${params.toString()}`);
    state.numbers = data.items || [];
    if (!state.selectedNumber && state.numbers.length > 0) state.selectedNumber = state.numbers[0].phone_number;
    setConnStatus(true);
    renderNumbers();
    renderKpis(data.totals);
    showStatus("inboxStatus", "");
  } catch (e) {
    setConnStatus(false);
    showStatus("inboxStatus", `Numbers load failed: ${e.message}`);
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

async function loadMessages() {
  if (state.authUser?.role === "platform_admin" && !state.scopeCompanyId) {
    $("messagesTbody").innerHTML = "";
    showStatus("inboxStatus", "Select a business in Admin to view logs.");
    return;
  }
  if (!state.selectedNumber) {
    showStatus("inboxStatus", "Select a number to view messages.");
    $("messagesTbody").innerHTML = "";
    return;
  }

  state.direction = $("directionFilter").value;
  state.limit = Number($("limitSelect").value || 100);

  try {
    const params = new URLSearchParams();
    params.set("phone_number", state.selectedNumber);
    if (state.direction) params.set("direction", state.direction);
    params.set("limit", String(state.limit));
    params.set("offset", String(state.offset));
    appendScopeParams(params);

    const data = await apiGet(`/api/messages?${params.toString()}`);
    renderMessages(data.items || []);
    $("pageLabel").textContent = `Page ${Math.floor(state.offset / state.limit) + 1}`;
    showStatus("inboxStatus", "");
  } catch (e) {
    showStatus("inboxStatus", `Messages load failed: ${e.message}`);
  }
}

function parseIsoOrNull(x) {
  if (!x) return null;
  const d = new Date(x);
  return Number.isNaN(d.getTime()) ? null : d;
}

function bookingToEvent(b) {
  const start = parseIsoOrNull(b.start_ts);
  const end = parseIsoOrNull(b.end_ts);
  if (!start) return null;
  if (["rejected", "expired", "cancelled"].includes((b.status || "").toLowerCase())) return null;

  return {
    id: String(b.public_ref ?? b.id ?? ""),
    title: `${b.service_label ?? "Booking"} (${b.status ?? "pending"})`,
    start: start.toISOString(),
    end: end ? end.toISOString() : null,
    extendedProps: { booking: b },
  };
}

function initBookingsCalendarIfNeeded() {
  if (bookingsCalendarInited) return;
  const el = $("bookingsCalendar");
  if (!el) return;

  bookingsCalendar = new FullCalendar.Calendar(el, {
    initialView: "timeGridWeek",
    height: "auto",
    nowIndicator: true,
    headerToolbar: { left: "prev,next today", center: "title", right: "dayGridMonth,timeGridWeek,timeGridDay" },
    eventClick: (info) => {
      const b = info.event.extendedProps.booking || {};
      alert([
        `Ref: ${b.public_ref ?? b.id ?? "-"}`,
        `Customer: ${b.customer_number ?? "-"}`,
        `Service: ${b.service_label ?? "-"}`,
        `Status: ${b.status ?? "-"}`,
      ].join("\n"));
    },
  });

  bookingsCalendar.render();
  bookingsCalendarInited = true;
}

function renderBookings(items) {
  const body = $("bookingsTbody");
  body.innerHTML = "";

  for (const b of items) {
    const status = (b.status || "pending").toLowerCase();
    const ref = String(b.public_ref ?? b.id ?? "");

    const actionsHtml = status === "pending"
      ? `<div class="row" style="gap:8px; padding:0;"><button class="btn primary js-booking-action" data-action="approve" data-id="${escapeHtml(ref)}">Approve</button><button class="btn js-booking-action" data-action="reject" data-id="${escapeHtml(ref)}">Reject</button></div>`
      : status === "approved"
      ? `<div class="row" style="gap:8px; padding:0;"><button class="btn js-booking-action" data-action="cancel" data-id="${escapeHtml(ref)}">Cancel</button></div>`
      : `<span style="opacity:.6;">—</span>`;

    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${b.created_ts ? fmtTs(b.created_ts) : "-"}</td>
      <td>${escapeHtml(b.customer_number ?? "")}</td>
      <td>${escapeHtml(b.service_label ?? "")}</td>
      <td>${b.start_ts && b.end_ts ? `${fmtTs(b.start_ts)} – ${fmtTs(b.end_ts)}` : "-"}</td>
      <td>${escapeHtml(status)}</td>
      <td>${escapeHtml(ref)}</td>
      <td>${escapeHtml(b.admin_note ?? "")}</td>
      <td>${actionsHtml}</td>
    `;
    body.appendChild(tr);
  }
}

async function loadBookings() {
  if (state.authUser?.role === "platform_admin" && !state.scopeCompanyId) {
    $("bookingsTbody").innerHTML = "";
    showStatus("bookingsStatus", "Select a business in Admin to view bookings.");
    return;
  }
  showStatus("bookingsStatus", "Loading bookings...");
  try {
    const status = $("bookingStatus").value || "all";
    const limit = Number($("bookingLimit").value || 50);
    const params = new URLSearchParams();
    params.set("status", status);
    params.set("limit", String(limit));
    appendScopeParams(params);
    const data = await apiGet(`/api/bookings/requests?${params.toString()}`);
    const items = data.items || [];
    renderBookings(items);

    if (bookingsCalendar) {
      bookingsCalendar.removeAllEvents();
      bookingsCalendar.addEventSource(items.map(bookingToEvent).filter(Boolean));
    }

    showStatus("bookingsStatus", items.length === 0 ? "No booking requests found." : "");
  } catch (e) {
    showStatus("bookingsStatus", `Bookings load failed: ${e.message}`);
  }
}

$("bookingsTbody")?.addEventListener("click", async (e) => {
  const btn = e.target.closest(".js-booking-action");
  if (!btn) return;
  const action = btn.dataset.action;
  const id = btn.dataset.id;
  if (!action || !id) return;

  const note = prompt(`Optional admin note for ${action.toUpperCase()} Ref #${id}:`, "") || null;
  btn.disabled = true;
  try {
    const qs = note ? `?admin_note=${encodeURIComponent(note)}` : "";
    await apiPost(`/api/bookings/${encodeURIComponent(id)}/${encodeURIComponent(action)}${qs}`);
    await loadBookings();
  } catch (err) {
    showStatus("bookingsStatus", `Action failed: ${err.message}`);
  } finally {
    btn.disabled = false;
  }
});

function renderSignupRequests(items) {
  const body = $("signupReqTbody");
  body.innerHTML = "";
  let newCount = 0;
  let pending = 0;

  for (const r of items) {
    if (r.status === "new") newCount += 1;
    if (r.status === "new" || r.status === "contacted") pending += 1;

    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${fmtTs(r.created_ts)}</td>
      <td>${escapeHtml(r.full_name)}</td>
      <td>${escapeHtml(r.company_name)}</td>
      <td>${escapeHtml(r.work_email)}</td>
      <td>${escapeHtml(r.whatsapp_number || "-")}</td>
      <td>${escapeHtml(r.automation_needs || "-")}</td>
      <td>${escapeHtml(r.status)}</td>
      <td>
        <div class="row" style="gap:6px; padding:0;">
          <button class="btn js-req-status" data-id="${r.id}" data-status="contacted">Contacted</button>
          <button class="btn primary js-req-status" data-id="${r.id}" data-status="approved">Approve</button>
        </div>
      </td>
    `;
    body.appendChild(tr);
  }

  $("kpiReqs").textContent = String(newCount);
  $("kpiPendingReqs").textContent = String(pending);
}

async function loadSignupRequests() {
  if (state.authUser?.role !== "platform_admin") return;
  showStatus("signupReqStatus", "Loading signup requests...");
  try {
    const status = $("signupStatusFilter").value || "all";
    const data = await apiGet(`/api/admin/signup_requests?status=${encodeURIComponent(status)}&limit=200`);
    const items = data.items || [];
    renderSignupRequests(items);
    showStatus("signupReqStatus", items.length === 0 ? "No requests found." : "");
  } catch (e) {
    showStatus("signupReqStatus", `Load failed: ${e.message}`);
  }
}

$("signupReqTbody")?.addEventListener("click", async (e) => {
  const btn = e.target.closest(".js-req-status");
  if (!btn) return;
  const id = Number(btn.dataset.id);
  const status = btn.dataset.status;
  const note = prompt(`Optional note for request #${id}`, "") || "";
  btn.disabled = true;
  try {
    await apiPost(`/api/admin/signup_requests/${id}/status`, { status, note });
    await loadSignupRequests();
  } catch (err) {
    showStatus("signupReqStatus", `Update failed: ${err.message}`);
  } finally {
    btn.disabled = false;
  }
});

$("createAccountBtn")?.addEventListener("click", async () => {
  const company_name = $("newCompanyName").value.trim();
  const username = $("newUsername").value.trim();
  const password = $("newPassword").value;
  const whatsapp_phone_number_id = $("newPhoneId").value.trim();

  if (!company_name || !username || !password) {
    showStatus("adminCreateStatus", "Company name, username, and password are required.");
    return;
  }

  showStatus("adminCreateStatus", "Creating account...");
  try {
    await apiPost("/api/admin/accounts", { company_name, username, password, whatsapp_phone_number_id });
    showStatus("adminCreateStatus", "Account created successfully.");
    $("newCompanyName").value = "";
    $("newUsername").value = "";
    $("newPassword").value = "";
    $("newPhoneId").value = "";
    await loadCompanies();
  } catch (err) {
    showStatus("adminCreateStatus", `Create failed: ${err.message}`);
  }
});

function stopAutoRefresh() {
  if (autoTimer) {
    clearInterval(autoTimer);
    autoTimer = null;
  }
}

async function refreshCurrentView() {
  if (state.view === "inbox") {
    await loadNumbers();
    await loadMessages();
  } else if (state.view === "bookings") {
    await loadBookings();
  } else if (state.view === "admin" && state.authUser?.role === "platform_admin") {
    await loadCompanies();
    await loadSignupRequests();
  }
}

function startAutoRefresh() {
  stopAutoRefresh();
  if (!state.authToken) return;

  autoTimer = setInterval(async () => {
    if (document.hidden || autoInFlight || !state.authToken) return;
    autoInFlight = true;
    try {
      await refreshCurrentView();
    } finally {
      autoInFlight = false;
    }
  }, AUTO_POLL_MS);
}

document.querySelectorAll(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => switchView(btn.dataset.view));
});

$("numberSearch")?.addEventListener("input", () => renderNumbers());
$("prevBtn")?.addEventListener("click", () => { state.offset = Math.max(0, state.offset - state.limit); loadMessages(); });
$("nextBtn")?.addEventListener("click", () => { state.offset += state.limit; loadMessages(); });
$("directionFilter")?.addEventListener("change", () => { state.offset = 0; loadMessages(); });
$("limitSelect")?.addEventListener("change", () => { state.offset = 0; loadMessages(); });

$("bookingStatus")?.addEventListener("change", () => loadBookings());
$("bookingLimit")?.addEventListener("change", () => loadBookings());
$("kbRefreshBtn")?.addEventListener("click", () => loadKbFolders({ loadEditor: false }));
$("kbSaveBtn")?.addEventListener("click", () => saveKbFolderContent());
$("kbEditorText")?.addEventListener("input", () => {
  if ($("kbEditorText")?.disabled) return;
  setKbDirty(true);
});
$("refreshReqBtn")?.addEventListener("click", () => loadSignupRequests());
$("signupStatusFilter")?.addEventListener("change", () => loadSignupRequests());

$("adminTokenModalSaveBtn")?.addEventListener("click", doLogin);
$("loginPassInput")?.addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });

$("authBtn")?.addEventListener("click", async () => {
  if (state.authToken) {
    if (!confirm("Logout?")) return;
    await logout();
  } else {
    openLoginModal();
  }
});

$("themeToggle")?.addEventListener("click", () => {
  applyTheme(state.theme === "dark" ? "light" : "dark");
});

// Boot
applyTheme(state.theme);
renderAuthButton();
if ($("kbSaveBtn")) $("kbSaveBtn").disabled = true;
setKbDirty(false);

if (!state.authToken) {
  switchView("inbox", true);
  openLoginModal();
  setConnStatus(false);
} else {
  setConnStatus(true);
  (async () => {
    if (state.authUser?.role === "platform_admin") {
      setScopeCompanyId(null);
      await loadCompanies();
      switchView("admin", true);
      startAutoRefresh();
      return;
    }
    switchView("inbox", true);
    await loadNumbers();
    await loadMessages();
    startAutoRefresh();
  })().catch(() => {
    forceLogout("Please login again.");
  });
}
