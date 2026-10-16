// Tiny API client. All calls are relative ("/api/..."); Vite proxies them to
// the FastAPI backend in dev (see vite.config.js).

const BASE = "/api";

async function json(res) {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

export const api = {
  status: () => fetch(`${BASE}/status`).then(json),
  taxonomy: () => fetch(`${BASE}/taxonomy`).then(json),
  connect: () => fetch(`${BASE}/connect`, { method: "POST" }).then(json),
  setDryRun: (enabled) =>
    fetch(`${BASE}/settings/dry_run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    }).then(json),

  startRun: (maxMessages) =>
    fetch(`${BASE}/runs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ max_messages: maxMessages }),
    }).then(json),

  startFull: () => fetch(`${BASE}/runs/full`, { method: "POST" }).then(json),

  classify: (reclassify = false) =>
    fetch(`${BASE}/classify`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reclassify }),
    }).then(json),

  proposals: (status) =>
    fetch(`${BASE}/proposals${status ? `?status=${encodeURIComponent(status)}` : ""}`)
      .then(json)
      .then((d) => d.proposals),

  counts: () => fetch(`${BASE}/proposals/counts`).then(json),

  groups: (status = "PENDING") =>
    fetch(`${BASE}/groups?status=${encodeURIComponent(status)}`).then(json).then((d) => d.groups),

  decideGroup: (domain, label, verdict, newLabel, action) =>
    fetch(`${BASE}/groups/decide`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ domain, label, verdict, new_label: newLabel || null, action: action || "label" }),
    }).then(json),

  setLabel: (messageId, label) =>
    fetch(`${BASE}/proposals/${encodeURIComponent(messageId)}/label`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label }),
    }).then(json),

  submitDecisions: (runId, decisions) =>
    fetch(`${BASE}/runs/${encodeURIComponent(runId)}/decisions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decisions }),
    }).then(json),

  apply: () => fetch(`${BASE}/apply`, { method: "POST" }).then(json),

  autopilot: (level = "balanced", threshold = 0.85) =>
    fetch(`${BASE}/autopilot`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ level, threshold }),
    }).then(json),

  undo: () => fetch(`${BASE}/undo`, { method: "POST" }).then(json),

  setVerify: (enabled) =>
    fetch(`${BASE}/settings/verify_rules`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    }).then(json),

  drill: (label) => fetch(`${BASE}/drill?label=${encodeURIComponent(label)}`).then(json),

  decideSlice: (payload) =>
    fetch(`${BASE}/slices/decide`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(json),

  segregate: (label) =>
    fetch(`${BASE}/segregate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label }),
    }).then(json),

  audit: (limit = 50) => fetch(`${BASE}/audit?limit=${limit}`).then(json).then((d) => d.audit),

  streamUrl: (runId) => `${BASE}/runs/${encodeURIComponent(runId)}/stream`,
};
