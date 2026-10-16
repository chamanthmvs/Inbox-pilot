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
  const [runSummary, setRunSummary] = useState(null);
  const [audit, setAudit] = useState([]);
  const [error, setError] = useState(null);
  const [connecting, setConnecting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [showHelp, setShowHelp] = useState(false);
  const [drill, setDrill] = useState(null);       // label being drilled, or null
  const [drillData, setDrillData] = useState(null);
  const esRef = useRef(null);

  const connected = !!(status && status.authenticated);
  const dryRun = !!(status && status.dry_run);
  const llmEnabled = !!(status && status.llm_enabled);
  const verifyRules = !!(status && status.verify_rules);
  const approvedCount = counts.APPROVED || 0;
  const hasWork = groups.length > 0 || needsReview.length > 0 || approvedCount > 0;
  const idle = phase === "idle";
  const showDashboard = idle && hasWork && !drill;

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
        if (data.summary) setRunSummary(data.summary);
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

  async function toggleVerify() {
    if (!status) return;
    try {
      await api.setVerify(!status.verify_rules);
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

  async function decideGroup(group, verdict, newLabel, action) {
    setBusy(true);
    setError(null);
    try {
      await api.decideGroup(group.domain, group.label, verdict, newLabel, action);
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
    const breakdown = approvedCount;
    const ok = window.confirm(
      dryRun
        ? `Preview (DRY-RUN): simulate ${fmt(breakdown)} approved change(s)? Nothing in Gmail will change.`
        : `Apply ${fmt(breakdown)} approved change(s) to Gmail now?\n\n• Labels file emails into folders\n• Archive removes them from the Inbox (still searchable)\n• Trash moves them to Gmail's bin (recoverable ~30 days)\n\nEverything is reversible and logged.`
    );
    if (!ok) return;
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

  async function openDrill(label) {
    setDrill(label);
    setDrillData(null);
    try {
      setDrillData(await api.drill(label));
    } catch (e) {
      setError(e.message);
    }
  }

  function closeDrill() {
    setDrill(null);
    setDrillData(null);
  }

  async function sliceAct(kind, value, action) {
    if (action === "trash" && !window.confirm(`Trash the "${value}" slice? Recoverable in Gmail ~30 days.`)) return;
    setBusy(true);
    setError(null);
    try {
      await api.decideSlice({ label: drill, [kind]: value, verdict: "APPROVED", action });
      setDrillData(await api.drill(drill));
      await refreshStatus();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  function splitWithAI() {
    const label = drill;
    closeDrill();
    setRunSummary(null);
    launch(api.segregate(label));
  }

  function autoOrganize() {
    setRunSummary(null);
    setApplyResult(null);
    launch(api.autopilot("balanced", 0.85));
  }

  async function undoLast() {
    if (!window.confirm(
      "Undo the last run?\n\nThis removes the labels just applied, puts archived emails back in your Inbox, and untrashes anything trashed. Those emails return to your review list."
    )) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.undo();
      setRunSummary(null);
      setApplyResult(null);
      await loadDashboard();
      window.alert(`Undone — reversed ${fmt(r.undone || 0)} change(s).`);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
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
              title={dryRun ? "Preview mode — Apply only simulates. Click to go LIVE." : "Live mode — Apply makes real changes. Click to preview safely."}
            >
              {dryRun ? "👀 Preview mode" : "🟢 Live mode"}
            </button>
          )}
          {status && (
            <button
              className={`mode ${verifyRules ? "live" : "dry"}`}
              onClick={toggleVerify}
              title={verifyRules
                ? "AI-vet is ON — the AI confirms or corrects every rule; nothing is labeled on a rule alone. Click to turn off (faster, rules-only)."
                : "AI-vet is OFF — fast rules-first. Click to have the AI check every rule (more accurate)."}
            >
              {verifyRules ? "🧠 AI-vet: on" : "⚡ AI-vet: off"}
            </button>
          )}
        </div>
      </header>

      <div className="safety-strip">
        🔒 Reads only metadata — never your email content · nothing is ever deleted · every change is reversible &amp; logged
        <button className="linkbtn" onClick={() => setShowHelp((v) => !v)}>
          {showHelp ? "Hide how it works" : "How it works"}
        </button>
      </div>

      {showHelp && (
        <div className="help card">
          <ol className="help-steps">
            <li><b>Connect</b> your Gmail — a one-time Google sign-in. The app can read &amp; label, but <b>cannot delete</b>.</li>
            <li><b>Organize</b> — it reads just the envelope (sender, subject, date) of your emails and suggests a tidy category for each.</li>
            <li><b>Review by sender</b> — emails are grouped (e.g. “1,842 from naukri.com → Job/Applications”). You <b>Approve</b>, <b>Archive</b>, or <b>Trash</b> a whole group in one click.</li>
            <li><b>Apply</b> — only what you approved is changed in Gmail, and it’s all written to a log you can undo.</li>
          </ol>
          <p className="muted small">Actions: <b>Label</b> = file into a folder · <b>Archive</b> = remove from Inbox (kept &amp; searchable) · <b>Trash</b> = Gmail bin (recoverable ~30 days).</p>
          <p className="muted small">🧠 <b>AI-vet</b> (top-right): when on, the AI confirms or corrects every rule and only labels what it can justify — no rule decides on its own. Turn off for a faster rules-only pass.</p>
        </div>
      )}

      {error && <div className="banner error">⚠ {error}</div>}

      {dryRun && (
        <div className="banner info">
          👀 <b>Preview mode (DRY-RUN):</b> Apply will only simulate — nothing in Gmail changes.
          Flip the top-right switch to <b>LIVE</b> when you’re ready to make real changes.
        </div>
      )}

      {applyResult && (
        <div className={`banner ${applyResult.applied > 0 ? "ok" : "info"}`}>
          {dryRun
            ? `👀 Preview complete: ${fmt(applyResult.dry_run)} change(s) would be made. Nothing in Gmail changed — switch to Live mode to apply for real.`
            : `✅ Done! Applied ${fmt(applyResult.applied)} change(s) to Gmail${applyResult.errors ? ` · ${fmt(applyResult.errors)} couldn’t be applied` : ""}.`}
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

      {/* Progress — friendly, stage-aware */}
      {(phase === "running" || phase === "applying") && (
        <section className="card">
          <div className="working">
            <span className="spinner" aria-hidden="true" />
            <h2 style={{ margin: 0 }}>{stageTitle(phase, progress)}</h2>
          </div>
          <div className="bar"><div className="fill" style={{ width: `${pct}%` }} /></div>
          <p className="progress-text">{stageText(phase, progress)}</p>
          {phase === "running" && progress && progress.mode === "full" && (
            <p className="muted small">
              🟢 This runs in the background and rides out Gmail’s rate limits — you can leave
              this page and come back anytime; it keeps going and resumes where it left off.
            </p>
          )}
          {phase === "applying" && (
            <p className="muted small">Talking to Gmail in bulk — this is quick even for thousands of emails.</p>
          )}
        </section>
      )}

      {runSummary && idle && runSummary.sublabels !== undefined && (
        <div className={`banner ${runSummary.error ? "info" : "ok"}`}>
          {runSummary.error
            ? `ℹ️ ${runSummary.error}`
            : <>✂️ Split <b>{runSummary.label}</b> into <b>{runSummary.sublabels.length}</b> sub-label(s)
                — moved <b>{fmt(runSummary.moved)}</b>, <b>{fmt(runSummary.unchanged)}</b> unchanged.
                The finer groups are below for review.</>}
        </div>
      )}

      {runSummary && idle && runSummary.auto_label !== undefined && (
        <div className="banner ok">
          ✅ Auto-organized: labeled <b>{fmt(runSummary.auto_label)}</b>, archived <b>{fmt(runSummary.auto_archive)}</b>
          {runSummary.apply ? <> · applied <b>{fmt(runSummary.apply.applied)}</b> to Gmail</> : null}
          {" "}· <b>{fmt(runSummary.left_for_review)}</b> left for you to review.
          {status && status.audit_count > 0 && (
            <button className="linkbtn" style={{ marginLeft: "10px" }} onClick={undoLast}>↩ Undo this run</button>
          )}
        </div>
      )}

      {/* Drill-down: deep review of one label */}
      {idle && drill && (
        <section className="card">
          <div className="row between">
            <h2>🔍 {drill}</h2>
            <button className="btn ghost sm" onClick={closeDrill}>← Back</button>
          </div>
          {!drillData ? (
            <p className="muted">Analyzing…</p>
          ) : (
            <>
              <p className="muted small">
                {fmt(drillData.total)} emails in this label. Let the AI split it into finer
                sub-categories, or act on a specific slice below.
              </p>
              <button className="btn primary" disabled={busy || !llmEnabled}
                onClick={splitWithAI}
                title={llmEnabled ? "" : "Turn on AI (add ANTHROPIC_API_KEY) to use this"}>
                ✨ Split with AI into sub-categories
              </button>
              {!llmEnabled && <span className="muted small" style={{ marginLeft: 10 }}>AI is off</span>}

              <h3>By sender</h3>
              {drillData.senders.map((s) => (
                <SliceRow key={"d" + s.domain} name={s.domain || "(no domain)"} count={s.count}
                  busy={busy} onAct={(a) => sliceAct("domain", s.domain, a)} />
              ))}
              <h3>By subject keyword</h3>
              {drillData.keywords.map((k) => (
                <SliceRow key={"k" + k.keyword} name={`“${k.keyword}”`} count={k.count}
                  busy={busy} onAct={(a) => sliceAct("keyword", k.keyword, a)} />
              ))}
            </>
          )}
        </section>
      )}

      {/* Dashboard / review — shown whenever there is work, no live run needed */}
      {showDashboard && (
        <>
          <div className="autopilot-bar">
            <div>
              <div className="ap-title">🚀 Auto-Organize</div>
              <div className="muted small">
                Apply everything I’m confident about (label + archive the noise) and only show you the
                unsure ones. <b>Balanced · high confidence · never auto-trashes · reversible.</b>
              </div>
            </div>
            <div className="ap-actions">
              <button className="btn primary" disabled={busy || phase !== "idle" || (counts.PENDING || 0) === 0}
                onClick={autoOrganize}>
                🚀 Auto-Organize {fmt(counts.PENDING || 0)}
              </button>
              {status && status.audit_count > 0 && (
                <button className="btn ghost" disabled={busy} onClick={undoLast}>↩ Undo last run</button>
              )}
            </div>
          </div>

          {(counts.NEEDS_REVIEW || 0) > 0 && (
            <div className="banner info" style={{display:"flex",alignItems:"center",justifyContent:"space-between",gap:"14px",flexWrap:"wrap"}}>
              <span>
                <b>{fmt(counts.NEEDS_REVIEW)}</b> emails the sorter wasn’t sure about.
                {llmEnabled
                  ? " Let the AI label them for you — it won’t re-download anything from Gmail."
                  : " The AI helper is off — add your Anthropic key to backend/.env and restart to clear most of these automatically."}
              </span>
              {llmEnabled && (
                <button className="btn primary" disabled={busy || phase !== "idle"}
                  onClick={() => launch(api.classify(false))}>
                  ✨ Auto-classify with AI
                </button>
              )}
            </div>
          )}

          <section className="tiles">
            <Tile label="Sender groups to review" value={fmt(groups.length)} accent />
            <Tile label="Emails waiting" value={fmt(counts.PENDING || 0)} />
            <Tile label="Approved — ready to apply" value={fmt(approvedCount)} good />
            <Tile label="Need your input" value={fmt(counts.NEEDS_REVIEW || 0)} warn />
          </section>

          <CategoryBars groups={groups} />

          <section className="card">
            <div className="row between">
              <h2>Review, grouped by sender</h2>
              <button
                className="btn primary"
                onClick={applyApproved}
                disabled={approvedCount === 0 || busy}
                title={approvedCount === 0 ? "Approve a group or two first" : ""}
              >
                {dryRun ? "👀 Preview" : "✓ Apply"} {fmt(approvedCount)} approved
              </button>
            </div>
            <p className="muted small">
              Each card is a batch of emails from one sender. Pick what to do, then approve the
              whole batch at once — <b>Label</b> (file it), <b>Archive</b> (out of Inbox), or
              <b> Trash</b> (recoverable). Approving teaches the app so that sender is automatic next time.
            </p>
            {groups.length === 0 && (
              <div className="empty">
                {approvedCount > 0 ? (
                  <>
                    <p>🎉 <b>All groups reviewed.</b> You have <b>{fmt(approvedCount)}</b> email(s) approved and ready.</p>
                    <p className="muted small">
                      Hit the <b>{dryRun ? "👀 Preview" : "✓ Apply"} {fmt(approvedCount)} approved</b> button above
                      {dryRun ? " — you’re in Preview mode, so switch to Live mode first to make it real." : " and they’ll be organized in Gmail."}
                    </p>
                  </>
                ) : (counts.NEEDS_REVIEW || 0) > 0 ? (
                  <p className="muted">Just the “need your input” emails left — handle them in the section below.</p>
                ) : (
                  <p className="muted">🎉 All caught up — your inbox is organized!</p>
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
                onDrill={openDrill}
              />
            ))}
          </section>

          {needsReview.length > 0 && (
            <section className="card">
              <h2>Need your input ({fmt(needsReview.length)})</h2>
              <p className="muted small">The sorter wasn’t confident about these. Pick a category for any you care about — the rest can wait.</p>
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

function SliceRow({ name, count, busy, onAct }) {
  return (
    <div className="review-row">
      <div className="grow"><b>{name}</b> <span className="muted small">· {fmt(count)}</span></div>
      <button className="btn good sm" disabled={busy} onClick={() => onAct("label")}>✓ Keep</button>
      <button className="btn sm" disabled={busy} onClick={() => onAct("archive")}>📥 Archive</button>
      <button className="btn ghost sm" disabled={busy} onClick={() => onAct("trash")}>🗑 Trash</button>
    </div>
  );
}

function GroupCard({ group, taxonomy, busy, onDecide, onDrill }) {
  const [open, setOpen] = useState(false);
  const [relabel, setRelabel] = useState("");
  const [action, setAction] = useState("label");
  const actionBtn =
    action === "trash" ? "🗑 Trash all" : action === "archive" ? "📥 Archive all" : "✓ Approve all";
  const target = relabel || group.label;

  function approve() {
    if (action === "trash") {
      if (!window.confirm(
        `Move ${fmt(group.count)} email(s) from ${group.domain || "this sender"} to Gmail Trash?\n\nThey're recoverable in Gmail for ~30 days — nothing is permanently deleted.`
      )) return;
    } else if (action === "archive") {
      if (!window.confirm(
        `Archive ${fmt(group.count)} email(s) from ${group.domain || "this sender"}?\n\nThey leave your Inbox (labeled "${target}") but stay in Gmail and are searchable.`
      )) return;
    }
    onDecide(group, "APPROVED", relabel || undefined, action);
  }

  return (
    <div className="group">
      <div className="group-head">
        <button className="expand" onClick={() => setOpen(!open)} aria-label="expand">
          {open ? "▾" : "▸"}
        </button>
        {onDrill && group.label && (
          <button className="btn ghost sm" disabled={busy} title="Deep review / split this label"
            onClick={() => onDrill(group.label)}>🔍</button>
        )}
        <div className="grow">
          <div className="group-title">
            <span className="count">{fmt(group.count)}</span>
            <span className="muted">emails from</span>
            <span className="domain">{group.domain || "(no sender domain)"}</span>
            <span className="arrow">→</span>
            <span className="pill">{relabel || group.label}</span>
          </div>
          <div className="muted small">
            {confidenceLabel(group)} · suggested by {methodLabel(group.methods)}
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
          <select
            className="select sm"
            value={action}
            disabled={busy}
            onChange={(e) => setAction(e.target.value)}
            title="What Apply will do for this group"
          >
            <option value="label">Label only</option>
            <option value="archive">Label + Archive</option>
            <option value="trash">Trash (recoverable)</option>
          </select>
          <button className="btn good sm" disabled={busy} onClick={approve}>
            {actionBtn}
          </button>
          <button className="btn ghost sm" disabled={busy}
            onClick={() => onDecide(group, "REJECTED")} title="Leave these as-is for now">
            ✕ Skip
          </button>
        </div>
      </div>
      {open && (
        <div className="samples-wrap">
          <div className="muted small" style={{ marginBottom: "4px" }}>A few subjects from this batch:</div>
          <ul className="samples">
            {(group.samples || []).map((s, i) => (
              <li key={i} className="ellipsis">{s.subject || "(no subject)"}</li>
            ))}
            {group.count > (group.samples || []).length && (
              <li className="muted small">…and {fmt(group.count - group.samples.length)} more</li>
            )}
          </ul>
        </div>
      )}
    </div>
  );
}

// --- friendly helpers --------------------------------------------------------

function fmt(n) {
  return (n ?? 0).toLocaleString();
}

function stageTitle(phase, progress) {
  if (phase === "applying") return "Organizing your Gmail…";
  if (progress && (progress.action || progress.stage)) return "🚀 Auto-organizing…";
  if (progress && progress.mode === "full") return "Reading & sorting your inbox…";
  return "Sorting your emails…";
}

function stageText(phase, progress) {
  if (phase === "applying") return "Applying your approved changes in Gmail.";
  if (!progress) return "Getting started…";
  if (progress.stage === "deciding") return "Deciding what’s safe to auto-apply…";
  if (progress.action) {
    const d = fmt(progress.done || 0);
    const t = progress.total ? ` of ${fmt(progress.total)}` : "";
    return `Applying confident changes… ${d}${t}`;
  }
  const done = fmt(progress.done || 0);
  const total = progress.total ? ` of ${fmt(progress.total)}` : "";
  if (progress.mode === "full") {
    let t = `Sorted ${done}${total} emails`;
    if (progress.ingested != null) t += ` · fetched ${fmt(progress.ingested)}`;
    if (progress.errors) t += ` · ${fmt(progress.errors)} skipped`;
    return t + " — you can keep working, this runs on its own.";
  }
  return `Sorted ${done}${total} emails…`;
}

function confidenceLabel(group) {
  const c = group.confidence_max || 0;
  if ((group.methods || []).includes("rules") || (group.methods || []).includes("learned") || c >= 0.85) return "High confidence";
  if (c >= 0.6) return "Medium confidence";
  return "Lower confidence — worth a peek";
}

function methodLabel(methods) {
  const m = methods || [];
  if (m.includes("learned")) return "a rule you taught it";
  if (m.includes("rules")) return "a built-in rule";
  if (m.includes("llm")) return "AI";
  if (m.includes("human")) return "you";
  return "the sorter";
}
