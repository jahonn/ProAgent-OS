const $ = (id) => document.getElementById(id);

function getKey() {
  return localStorage.getItem("proagent_api_key") || "";
}
function saveKey() {
  localStorage.setItem("proagent_api_key", $("apiKey").value.trim());
  alert("API key saved locally.");
}
const TASK_STATUSES = ["awaiting_human", "running", "completed", "cancelled", "failed"];
const RECEIPT_STATUSES = ["started", "succeeded", "failed"];
const TRACE_HIDDEN = new Set([
  "arguments",
  "args",
  "args_summary",
  "raw_arguments",
  "pending_action",
  "gated_arguments",
]);

window.addEventListener("DOMContentLoaded", () => {
  $("apiKey").value = getKey();
  $("hitlList").addEventListener("submit", onHitlSubmit);
  if (getKey()) loadHitlTasks();
});

async function api(path, opts = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  const key = getKey();
  if (key) headers["Authorization"] = `Bearer ${key}`;
  const res = await fetch(`/api/v1${path}`, { ...opts, headers });
  const text = await res.text();
  let data;
  try { data = JSON.parse(text); } catch { data = { raw: text }; }
  if (!res.ok) throw new Error(data.detail || data.raw || res.statusText);
  return data;
}

function showTab(name) {
  document.querySelectorAll("[id^=tab-]").forEach((el) => el.classList.add("hidden"));
  $(`tab-${name}`).classList.remove("hidden");
  document.querySelectorAll("nav.sidebar button").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === name);
  });
}

function appendMsg(role, content) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.textContent = content;
  $("chatLog").appendChild(div);
  $("chatLog").scrollTop = $("chatLog").scrollHeight;
}

function statusClass(status, allowed) {
  return allowed.indexOf(status) >= 0 ? status : "unknown";
}

function setTaskStatus(status, detail) {
  const el = $("taskStatus");
  el.replaceChildren();
  if (status) {
    const badge = document.createElement("span");
    badge.className = `status ${statusClass(status, TASK_STATUSES)}`;
    badge.textContent = status;
    el.appendChild(badge);
    el.appendChild(document.createTextNode(" "));
  }
  if (detail) el.appendChild(document.createTextNode(detail));
}

function redactToolArgs(value) {
  if (Array.isArray(value)) return value.map(redactToolArgs);
  if (value && typeof value === "object") {
    const out = {};
    for (const [key, item] of Object.entries(value)) {
      if (TRACE_HIDDEN.has(key)) continue;
      out[key] = redactToolArgs(item);
    }
    return out;
  }
  return value;
}

async function runTask() {
  const input = $("taskInput").value.trim();
  if (!input) return;
  setTaskStatus("", "Running…");
  appendMsg("user", input);
  try {
    const data = await api("/tasks", { method: "POST", body: JSON.stringify({ input }) });
    const status = data.status || "completed";
    if (status === "awaiting_human") {
      appendMsg("assistant", data.pause_reason || "Waiting for a person.");
    } else {
      appendMsg("assistant", data.result || "(empty)");
    }
    $("taskTrace").textContent = JSON.stringify(
      redactToolArgs({ task_id: data.task_id, status, plan: data.plan, trace: data.trace }),
      null,
      2
    );
    setTaskStatus(status, data.task_id || "");
    loadHitlTasks();
  } catch (e) {
    setTaskStatus("", e.message);
  }
}

async function runHitlDemo() {
  $("taskInput").value = "Pause this run for human approval before continuing. proagent-hitl-demo";
  await runTask();
}

async function ingestUrl() {
  try {
    const data = await api("/kb/ingest/url", { method: "POST", body: JSON.stringify({ url: $("kbUrl").value }) });
    $("kbOut").textContent = JSON.stringify(data, null, 2);
  } catch (e) { $("kbOut").textContent = e.message; }
}

async function ingestFile() {
  const f = $("kbFile").files[0];
  if (!f) return alert("Choose a file");
  const fd = new FormData();
  fd.append("file", f);
  try {
    const res = await fetch("/api/v1/kb/ingest/file", {
      method: "POST",
      headers: { Authorization: `Bearer ${getKey()}` },
      body: fd,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "upload failed");
    $("kbOut").textContent = JSON.stringify(data, null, 2);
  } catch (e) { $("kbOut").textContent = e.message; }
}

async function searchKb() {
  try {
    const data = await api("/kb/search", { method: "POST", body: JSON.stringify({ query: $("kbQuery").value, top_k: 5 }) });
    $("kbOut").textContent = JSON.stringify(data, null, 2);
  } catch (e) { $("kbOut").textContent = e.message; }
}

async function listDocs() {
  try {
    const data = await api("/kb/documents");
    $("kbOut").textContent = JSON.stringify(data, null, 2);
  } catch (e) { $("kbOut").textContent = e.message; }
}

async function loadRecs() {
  try {
    const data = await api("/recommendations");
    const root = $("recList");
    root.innerHTML = "";
    if (!data.length) { root.innerHTML = '<div class="muted">No recommendations yet.</div>'; return; }
    data.forEach((r) => {
      const el = document.createElement("div");
      el.className = "card";
      const actions = [];
      if (r.status === "pending") {
        actions.push(`<button class="btn ok" onclick="decideRec('${r.id}','approved')">Approve</button>`);
        actions.push(`<button class="btn danger" onclick="decideRec('${r.id}','rejected')">Reject</button>`);
      } else if (r.status === "approved") {
        actions.push(`<button class="btn secondary" onclick="decideRec('${r.id}','executed')">Mark executed</button>`);
      }
      const actionRow = actions.length ? `<div class="row">${actions.join("")}</div>` : "";
      const hint = r.status === "approved"
        ? `<div class="muted">Approved. Execution has not completed.</div>`
        : "";
      el.innerHTML = `<div class="row" style="justify-content:space-between">
        <strong>${escapeHtml(r.title)}</strong>
        <span class="status ${r.status}">${r.status}</span>
      </div>
      <div class="muted" style="margin:.4rem 0">${escapeHtml(r.rationale || "")}</div>
      ${hint}
      ${actionRow}`;
      root.appendChild(el);
    });
  } catch (e) { $("recList").textContent = e.message; }
}

async function decideRec(id, status) {
  try {
    const data = await api(`/recommendations/${id}/decide`, { method: "POST", body: JSON.stringify({ status }) });
    if (data.execution && data.execution.ok === false) {
      alert(data.execution.error || "Execution failed");
    }
    loadRecs();
  } catch (e) { alert(e.message); }
}

async function loadSkills() {
  try {
    const data = await api("/skills");
    $("skillList").innerHTML = data.map((s) =>
      `<div class="card"><strong>${escapeHtml(s.name)}</strong> <span class="muted">(${escapeHtml(s.id)})</span>
       <div class="muted">${escapeHtml(s.description)}</div>
       <div class="muted">runner: ${s.has_runner}</div></div>`
    ).join("") || '<div class="muted">No skills loaded</div>';
  } catch (e) { $("skillList").textContent = e.message; }
}

async function createMonitor() {
  try {
    const config = JSON.parse($("monConfig").value || "{}");
    const data = await api("/monitors", {
      method: "POST",
      body: JSON.stringify({ name: $("monName").value, kind: $("monKind").value, config, interval_seconds: 60 }),
    });
    await api(`/monitors/${data.id}/run`, { method: "POST" });
    loadMonitors();
    alert("Monitor created and run once.");
  } catch (e) { alert(e.message); }
}

async function loadMonitors() {
  try {
    const data = await api("/monitors");
    $("monList").innerHTML = data.map((m) =>
      `<div class="card"><strong>${escapeHtml(m.name)}</strong> · ${escapeHtml(m.kind)}
       <div class="muted">id=${m.id} enabled=${m.enabled} last=${m.last_run_at || "never"}</div>
       <button class="btn secondary" onclick="runMon('${m.id}')">Run now</button></div>`
    ).join("") || '<div class="muted">No monitors</div>';
  } catch (e) { $("monList").textContent = e.message; }
}

async function runMon(id) {
  try { await api(`/monitors/${id}/run`, { method: "POST" }); loadMonitors(); loadRecs(); }
  catch (e) { alert(e.message); }
}

async function loadDash() {
  try {
    const data = await api("/dashboard");
    $("dashOut").textContent = JSON.stringify(data, null, 2);
  } catch (e) { $("dashOut").textContent = e.message; }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;" }[c]));
}

function safeTaskId(id) {
  return /^[A-Za-z0-9_-]+$/.test(id) ? id : "";
}

let hitlCanDecide = false;

async function loadHitlTasks() {
  const note = $("hitlRoleNote");
  const root = $("hitlList");
  if (!getKey()) {
    hitlCanDecide = false;
    note.textContent = "Save a tenant API key to load tasks. Resume and Reject need a tenant operator key.";
    root.innerHTML = "";
    return;
  }
  try {
    const me = await api("/tenants/me");
    const role = me.role || "";
    hitlCanDecide = role === "tenant_operator";
    if (hitlCanDecide) {
      note.textContent = "This key can Resume or Reject a task in awaiting_human.";
    } else if (role === "tenant_readonly") {
      note.textContent = "This key is tenant_readonly. You can see awaiting_human and receipts. Resume and Reject stay hidden.";
    } else if (me.is_admin || role === "admin") {
      note.textContent = "The admin key can read tasks. Resume and Reject need a tenant operator key.";
    } else {
      note.textContent = "Resume and Reject are hidden for this key.";
    }
  } catch (e) {
    hitlCanDecide = false;
    note.textContent = e.message;
    root.textContent = "";
    return;
  }
  try {
    const data = await api("/tasks?limit=30");
    const tasks = Array.isArray(data) ? data.slice() : [];
    tasks.sort((a, b) => (a.status === "awaiting_human" ? 0 : 1) - (b.status === "awaiting_human" ? 0 : 1));
    root.innerHTML = "";
    if (!tasks.length) {
      root.innerHTML = '<div class="muted">No tasks yet. Run a task, or Run mock approval, to see status here.</div>';
      return;
    }
    tasks.forEach((task) => root.appendChild(renderHitlCard(task)));
  } catch (e) {
    root.textContent = e.message;
  }
}

function renderHitlCard(task) {
  const el = document.createElement("div");
  el.className = "card";
  const status = String(task.status || "");
  const id = safeTaskId(String(task.id || ""));
  const receipts = Array.isArray(task.hitl_execute_receipts) ? task.hitl_execute_receipts : [];
  const receiptHtml = receipts.length
    ? receipts.map((receipt) => {
        const tool = escapeHtml(receipt.tool_name || "tool");
        const rawStatus = String(receipt.status || "");
        const shown = escapeHtml(rawStatus || "unknown");
        return `<div class="hitl-receipt"><span class="mono">${tool}</span> <span class="status ${statusClass(rawStatus, RECEIPT_STATUSES)}">${shown}</span></div>`;
      }).join("")
    : '<div class="muted">No execute receipts yet.</div>';
  const waiting = status === "awaiting_human";
  const started = receipts.some((receipt) => receipt.status === "started");
  let form = "";
  if (waiting && hitlCanDecide && id) {
    const reexecute = started
      ? `<label class="muted"><input type="checkbox" name="reexecute" /> Re-run in-flight tool</label>`
      : "";
    form = `<form data-task-id="${escapeHtml(id)}">
      <label>Decision note</label>
      <input name="note" placeholder="optional" />
      ${reexecute}
      <div class="row">
        <button class="btn ok" type="submit" value="resume">Resume</button>
        <button class="btn danger" type="submit" value="reject">Reject</button>
      </div>
    </form>`;
  } else if (waiting && !hitlCanDecide) {
    form = '<div class="muted">Resume and Reject need a tenant operator key.</div>';
  }
  const reason = task.pause_reason
    ? `<div class="muted" style="margin:.35rem 0">${escapeHtml(task.pause_reason)}</div>`
    : "";
  const decision = task.hitl_decision
    ? `<div class="muted">Decision: ${escapeHtml(task.hitl_decision)}</div>`
    : "";
  const preview = escapeHtml(String(task.input || "").slice(0, 160));
  el.innerHTML = `<div class="row" style="justify-content:space-between">
      <strong class="mono">${escapeHtml(id || task.id || "")}</strong>
      <span class="status ${statusClass(status, TASK_STATUSES)}">${escapeHtml(status || "unknown")}</span>
    </div>
    <div style="margin:.35rem 0">${preview}</div>
    ${reason}
    ${decision}
    <div class="muted">hitl_execute_receipts</div>
    ${receiptHtml}
    ${form}`;
  return el;
}

async function onHitlSubmit(ev) {
  ev.preventDefault();
  const form = ev.target;
  if (!form || !form.getAttribute) return;
  const id = safeTaskId(form.getAttribute("data-task-id") || "");
  const action = ev.submitter && ev.submitter.value;
  if (!id || (action !== "resume" && action !== "reject")) return;
  const noteInput = form.elements.note;
  const note = noteInput ? String(noteInput.value || "").trim() : "";
  const body = { note };
  if (action === "resume" && form.elements.reexecute && form.elements.reexecute.checked) {
    body.reexecute = true;
  }
  $("hitlActionStatus").textContent = action === "resume" ? "Resuming…" : "Rejecting…";
  try {
    await api(`/tasks/${id}/${action}`, { method: "POST", body: JSON.stringify(body) });
    $("hitlActionStatus").textContent = "";
    await loadHitlTasks();
  } catch (e) {
    $("hitlActionStatus").textContent = e.message;
  }
}
