import React, { useEffect, useRef, useState } from "react";
import { api } from "./api.js";

export default function App() {
  const [status, setStatus] = useState(null);
  const [taxonomy, setTaxonomy] = useState([]);
  const [phase, setPhase] = useState("idle"); // idle | running | applying
  const [runId, setRunId] = useState(null);
  const [maxMessages, setMaxMessages] = useState(200);
  const [fetchAll, setFetchAll] = useState(false);
  const [progress, setProgress] = useState(null);
  const [groups, setGroups] = useState([]);
  const [needsReview, setNeedsReview] = useState([]);
  const [counts, setCounts] = useState({});
  const [applyResult, setApplyResult] = useState(null);
  const [audit, setAudit] = useState([]);
  const [error, setError] = useState(null);
  const [connecting, setConnecting] = useState(false);
  const [busy, setBusy] = useState(false);
  const esRef = useRef(null);

  const connected = !!(status && status.authenticated);
  const dryRun = !!(status && status.dry_run);
  const approvedCount = counts.APPROVED || 0;
  const hasWork = groups.length > 0 || needsReview.length > 0 || approvedCount > 0;
  const idle = phase === "idle";
  const showDashboard = idle && hasWork;

  async function refreshStatus() {
    try {
      const s = await api.status();
      setStatus(s);
      setCounts(s.proposal_counts || {});
      return s;
    } catch (e) {
      setError(e.message);
    }
  }

  async function loadDashboard() {
    try {
      const [g, n] = await Promise.all([api.groups("PENDING"), api.proposals("NEEDS_REVIEW")]);
      setGroups(g);
      setNeedsReview(n);
      await refreshStatus();
    } catch (e) {
      setError(e.message);
    }
  }

  useEffect(() => {
    api.taxonomy().then((t) => setTaxonomy(t.labels)).catch((e) => setError(e.message));
    loadDashboard();
    // Reconnect to an in-flight run after a browser refresh.
    refreshStatus().then((s) => {
      if (s && s.run && s.run.status === "running") {
        setRunId(s.run.run_id);
        setProgress(s.run.progress || null);
        setPhase("running");
        openStream(s.run.run_id);
      }
    });
    return () => esRef.current && esRef.current.close();
  }, []);

  function openStream(id) {
    const es = new EventSource(api.streamUrl(id));
    esRef.current = es;
    es.onmessage = (ev) => {
      let data;
      try {
        data = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (data.type === "progress") setProgress(data);
      else if (data.type === "paused" || data.type === "done") {
        setPhase("idle");
        loadDashboard();
        es.close();
      } else if (data.type === "error") {
        setPhase("idle");
        setError((data.message || "run failed") + " — progress so far is saved; re-run to resume.");
        loadDashboard(); // show the real, saved state (not a stale snapshot)
        es.close();
      }
    };
    es.onerror = () => es.close();
  }

  function launch(startPromise) {
    setError(null);
    setApplyResult(null);
    setProgress(null);
    setPhase("running");
    startPromise
      .then((run) => {
        setRunId(run.run_id);
        openStream(run.run_id);
      })
      .catch((e) => {
        setPhase("idle");
        setError(e.message);
      });
  }

  async function toggleDryRun() {
    if (!status) return;
    try {
      await api.setDryRun(!status.dry_run);
      await refreshStatus();
    } catch (e) {
      setError(e.message);
    }
  }

  async function connect() {
    setConnecting(true);
    setError(null);
    try {
      await api.connect();
      await refreshStatus();
    } catch (e) {
      setError(e.message);
    } finally {
      setConnecting(false);
    }
  }

  function organize() {
    launch(fetchAll ? api.startFull() : api.startRun(maxMessages));
  }

  async function decideGroup(group, verdict, newLabel) {
    setBusy(true);
    setError(null);
    try {
      await api.decideGroup(group.domain, group.label, verdict, newLabel);
      await loadDashboard();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function labelReview(messageId, label) {
    if (!label) return;
    setBusy(true);
    setError(null);
    try {
      await api.setLabel(messageId, label);
      await loadDashboard();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function applyApproved() {
    setPhase("applying");
    setError(null);
    try {
      const res = await api.apply(); // direct — no dependency on a live run
      setApplyResult(res);
      setAudit(await api.audit());
    } catch (e) {
      setError(e.message);
    } finally {
      setPhase("idle");
      await loadDashboard();
    }
  }

  const pct = progress && progress.total ? Math.round((progress.done / progress.total) * 100) : 0;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo">📨</span>
          <div>
            <h1>Gmail Organizer</h1>
            <p className="tag">Propose · you approve by the group · it applies · never deletes</p>
          </div>
        </div>
        <div className="conn">
          <span className={connected ? "dot ok" : "dot"} />
          {connected ? "Connected" : "Not connected"}
          {status && (
            <button
              className={`mode ${dryRun ? "dry" : "live"}`}
              onClick={toggleDryRun}
              title="Toggle whether Apply changes Gmail for real"
            >
              {dryRun ? "DRY-RUN (safe)" : "LIVE"}
            </button>
          )}
        </div>
      </header>

      {error && <div className="banner error">⚠ {error}</div>}

      {applyResult && (
        <div className={`banner ${applyResult.applied > 0 ? "ok" : "info"}`}>
          {dryRun
            ? `DRY-RUN: ${applyResult.dry_run} would be applied (nothing changed). Switch to LIVE to apply for real.`
            : `Applied ${applyResult.applied} label(s) to Gmail · ${applyResult.errors} error(s).`}
        </div>
      )}

      {/* Connect gate */}
      {idle && !connected && (
        <section className="card center">
          <h2>Connect your Gmail</h2>
          <p className="muted">
            Authorize once. The app can <b>read and label</b> your mail — it{" "}
            <b>cannot delete</b> anything.
          </p>
          <button className="btn primary" onClick={connect} disabled={connecting}>
            {connecting ? "Opening Google…" : "🔑 Connect Gmail"}
          </button>
        </section>
      )}

      {/* Start */}
      {idle && connected && (
        <section className="card">
          <div className="row between">
            <h2>Organize my inbox</h2>
            <StatRow counts={counts} audit={status ? status.audit_count : 0} />
          </div>
          <div className="controls">
            <label className={`seg ${!fetchAll ? "on" : ""}`}>
              <input type="radio" checked={!fetchAll} onChange={() => setFetchAll(false)} />
              Fetch up to
              <input
                className="num"
                type="number"
                min="1"
                value={maxMessages}
                disabled={fetchAll}
                onChange={(e) => setMaxMessages(Number(e.target.value))}
              />
            </label>
            <label className={`seg ${fetchAll ? "on" : ""}`}>
              <input type="radio" checked={fetchAll} onChange={() => setFetchAll(true)} />
              Everything (large inboxes)
            </label>
            <button className="btn primary" onClick={organize}>▶ Organize</button>
          </div>
          {!hasWork && (
            <p className="muted small">
              No proposals yet — click Organize to fetch and classify. Use “Everything” to pull
              a large backlog in one pass.
            </p>
          )}
        </section>
      )}

      {/* Progress */}
      {(phase === "running" || phase === "applying") && (
        <section className="card">
          <h2>{phase === "running" ? "Working…" : "Applying to Gmail…"}</h2>
          <div className="bar"><div className="fill" style={{ width: `${pct}%` }} /></div>
          <p className="progress-text">
            {phase === "applying"
              ? "Creating labels and applying approved emails…"
              : !progress
              ? "Starting…"
              : progress.mode === "full"
              ? `Classified ${progress.done}${progress.total ? ` / ${progress.total}` : ""}` +
                (progress.ingested != null ? ` · fetched ${progress.ingested}` : "") +
                (progress.errors ? ` · ${progress.errors} skipped` : "")
              : `${progress.phase === "item" ? "Processing" : progress.phase} ${progress.done}/${progress.total}` +
                (progress.subject ? ` — ${progress.subject}` : "") +
                (progress.label ? ` → ${progress.label}` : "")}
          </p>
          {progress && progress.mode === "full" && (
            <p className="muted small">
              This runs in the background and is resilient to Gmail rate limits — you can leave
              this page and come back; it keeps going. Groups will be ready when it finishes.
            </p>
          )}
        </section>
      )}

      {/* Dashboard / review — shown whenever there is work, no live run needed */}
      {showDashboard && (
        <>
          <section className="tiles">
            <Tile label="Groups to review" value={groups.length} accent />
            <Tile label="Pending emails" value={counts.PENDING || 0} />
            <Tile label="Approved (ready)" value={approvedCount} good />
            <Tile label="Needs review" value={counts.NEEDS_REVIEW || 0} warn />
          </section>

          <CategoryBars groups={groups} />

          <section className="card">
            <div className="row between">
              <h2>Review by sender · label</h2>
              <button
                className="btn primary"
                onClick={applyApproved}
                disabled={approvedCount === 0 || busy}
                title={approvedCount === 0 ? "Approve some groups first" : ""}
              >
                ✓ Apply {approvedCount} approved{dryRun ? " (DRY-RUN)" : ""}
              </button>
            </div>
            <p className="muted small">
              One click handles a whole sender+label group. Expand to see samples, or relabel a
              group before approving. Approving teaches a rule so that sender is automatic next time.
            </p>
            {groups.length === 0 && (
              <div className="empty">
                {approvedCount > 0 ? (
                  <>
                    <p><b>Nothing left to review.</b> You have <b>{approvedCount}</b> approved email(s) ready.</p>
                    <p className="muted small">
                      Click <b>“Apply {approvedCount} approved{dryRun ? " (DRY-RUN)" : ""}”</b> above
                      {dryRun ? " — but DRY-RUN is on, so switch the top-right toggle to LIVE first." : " to label them in Gmail."}
                    </p>
                  </>
                ) : (counts.NEEDS_REVIEW || 0) > 0 ? (
                  <p className="muted">Only “needs review” items remain — label them below.</p>
                ) : (
                  <p className="muted">✓ All caught up.</p>
                )}
              </div>
            )}
            {groups.map((g) => (
              <GroupCard
                key={`${g.domain}|${g.label}`}
                group={g}
                taxonomy={taxonomy}
                busy={busy}
                onDecide={decideGroup}
              />
            ))}
          </section>

          {needsReview.length > 0 && (
            <section className="card">
              <h2>Needs review ({needsReview.length})</h2>
              <p className="muted small">The classifier wasn't sure. Pick a label to include each.</p>
              {needsReview.map((r) => (
                <div className="review-row" key={r.message_id}>
                  <div className="grow">
                    <div className="subject">{r.subject || "(no subject)"}</div>
                    <div className="muted small">{r.from_addr}</div>
                  </div>
                  <select
                    className="select"
                    defaultValue=""
                    disabled={busy}
                    onChange={(e) => labelReview(r.message_id, e.target.value)}
                  >
                    <option value="">— pick a label —</option>
                    {taxonomy.map((l) => (
                      <option key={l.name} value={l.name}>{l.name}</option>
                    ))}
                  </select>
                </div>
              ))}
            </section>
          )}

          {audit.length > 0 && (
            <section className="card">
              <h3>Recent changes</h3>
              <ul className="audit">
                {audit.slice(0, 12).map((a) => (
                  <li key={a.id}>
                    <span className={`tag ${a.outcome}`}>{a.outcome}</span>
                    <span className="pill">{a.label}</span>
                    <span className="muted small ellipsis">{a.message_id}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
    </div>
  );
}

function Tile({ label, value, accent, good, warn }) {
  const cls = accent ? "accent" : good ? "good" : warn ? "warn" : "";
  return (
    <div className={`tile ${cls}`}>
      <div className="tile-value">{value}</div>
      <div className="tile-label">{label}</div>
    </div>
  );
}

function StatRow({ counts, audit }) {
  const entries = Object.entries(counts || {});
  if (entries.length === 0) return <span className="muted small">no proposals yet</span>;
  return (
    <span className="statrow">
      {entries.map(([k, v]) => (
        <span className="chip soft" key={k}>{k.toLowerCase()}: {v}</span>
      ))}
      <span className="chip soft">audit: {audit}</span>
    </span>
  );
}

function CategoryBars({ groups }) {
  const totals = {};
  groups.forEach((g) => {
    totals[g.label] = (totals[g.label] || 0) + g.count;
  });
  const rows = Object.entries(totals).sort((a, b) => b[1] - a[1]).slice(0, 8);
  if (rows.length === 0) return null;
  const max = Math.max(...rows.map((r) => r[1]));
  return (
    <section className="card">
      <h2>Pending by category</h2>
      <div className="bars">
        {rows.map(([label, n]) => (
          <div className="barrow" key={label}>
            <div className="barlabel ellipsis" title={label}>{label}</div>
            <div className="bartrack">
              <div className="barfill" style={{ width: `${Math.max(4, (n / max) * 100)}%` }} />
            </div>
            <div className="barval">{n}</div>
          </div>
        ))}
      </div>
    </section>
  );
}

function GroupCard({ group, taxonomy, busy, onDecide }) {
  const [open, setOpen] = useState(false);
  const [relabel, setRelabel] = useState("");
  return (
    <div className="group">
      <div className="group-head">
        <button className="expand" onClick={() => setOpen(!open)} aria-label="expand">
          {open ? "▾" : "▸"}
        </button>
        <div className="grow">
          <div className="group-title">
            <span className="domain">{group.domain || "(no domain)"}</span>
            <span className="arrow">→</span>
            <span className="pill">{group.label}</span>
            <span className="count">{group.count}</span>
          </div>
          <div className="muted small">
            {(group.methods || []).join(", ")}
            {group.confidence_max
              ? ` · confidence ${group.confidence_min.toFixed(2)}–${group.confidence_max.toFixed(2)}`
              : ""}
          </div>
        </div>
        <div className="group-actions">
          <select
            className="select sm"
            value={relabel}
            disabled={busy}
            onChange={(e) => setRelabel(e.target.value)}
            title="Relabel this group before approving"
          >
            <option value="">keep label</option>
            {taxonomy.map((l) => (
              <option key={l.name} value={l.name}>{l.name}</option>
            ))}
          </select>
          <button className="btn good sm" disabled={busy}
            onClick={() => onDecide(group, "APPROVED", relabel || undefined)}>
            ✓ Approve all
          </button>
          <button className="btn ghost sm" disabled={busy}
            onClick={() => onDecide(group, "REJECTED")}>
            ✕ Skip
          </button>
        </div>
      </div>
      {open && (
        <ul className="samples">
          {(group.samples || []).map((s, i) => (
            <li key={i} className="ellipsis">{s.subject}</li>
          ))}
          {group.count > (group.samples || []).length && (
            <li className="muted small">…and {group.count - group.samples.length} more</li>
          )}
        </ul>
      )}
    </div>
  );
}
