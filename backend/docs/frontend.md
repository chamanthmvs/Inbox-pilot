# The frontend (React + Vite)

**Stage 5 of the pipeline — what you actually click.** A deliberately *thin*
single-page app that talks only to the [backend API](./api.md). It shows live
progress while the backend works, lets you approve/reject the proposals, and
confirms what was applied. Written for anyone, with the component map for
developers.

Code lives in [`../../frontend/`](../../frontend/).

---

## 1. The one-sentence version

> One screen: press **Organize**, watch it work, tick the labels you want, and
> hit **Apply** — the browser never touches Gmail directly, it only talks to our
> API.

---

## 2. What you see (the flow)

```
[Organize] ─▶ live progress bar ─▶ Review screen ─▶ [Apply] ─▶ Done + audit
  POST /runs    SSE /runs/{id}/stream   GET /proposals   POST /runs/{id}/decisions
```

1. **Connect** — a one-time "Connect Gmail" step (OAuth) before anything else.
2. **Start** — an **Organize** button with "fetch up to N" or "Everything" (for
   large inboxes), plus a stat row of current counts.
3. **Working** — a progress bar driven by **Server-Sent Events**: "Processing
   12/50 — <subject> → <label>". The "ingestion is happening" feed.
4. **Dashboard / review** (the pause) — built for **scale**:
   - **Stat tiles** (groups to review, pending, approved, needs-review).
   - **Pending by category** — single-hue magnitude bars.
   - **Grouped review**: cards by **(sender domain × label)** with counts and
     samples. Each card has **Approve all / Skip / relabel** — one click clears a
     whole sender+label group (the key to not hand-reviewing 20k emails). Expand
     to see sample subjects. Different purposes from one sender are separate
     groups. Approving remembers a learned rule.
   - **Needs review**: individual rows with a label dropdown.
   - **Apply N approved to Gmail** resumes the workflow and labels everything
     approved.
5. **Done** — applied/dry-run/error counts + a short audit.

---

## 3. How it talks to the backend

- All calls are **relative** (`/api/...`); Vite's dev server **proxies** them to
  `http://127.0.0.1:8000` (see [`vite.config.js`](../../frontend/vite.config.js)),
  so there are no CORS or hardcoded-host issues in dev.
- Progress uses the browser's built-in **`EventSource`** against
  `/api/runs/{id}/stream`. On the terminal `paused` event, the app loads
  `/api/proposals` and shows the review screen.
- Approvals: pending items become a `{id: APPROVED|REJECTED}` map; any
  NEEDS_REVIEW item you labelled is saved first via `POST /proposals/{id}/label`
  (which approves it), then `POST /runs/{id}/decisions` resumes the workflow and
  applies everything approved.

The thin client mirrors the API exactly — see [api.md](./api.md) for the routes.

---

## 4. Run it

See the repo [README](../../README.md). Short version:

```bash
cd frontend
npm install        # first time (needs Node 18+)
npm run dev        # http://localhost:5173
```

Or one-click in VSCode: **Run and Debug → “▶ Run App (backend + frontend)”**.

---

## 5. Design notes

- **Thin on purpose.** No state library, no router — one component with hooks.
  All real logic lives in the backend services; the UI just orchestrates calls.
- **Theme-aware.** CSS variables with a light/dark `prefers-color-scheme` block.
- **Fails loudly, safely.** Errors (including a failed run on the SSE stream)
  surface in a banner; nothing is applied unless you press Apply.
- **v1 scope.** Single run at a time, matching the backend. A fancier progress
  animation or multi-run history can come later — the data is already there.

---

## 6. Code map

| File | Role |
| --- | --- |
| [`frontend/index.html`](../../frontend/index.html) | Vite entry HTML. |
| [`frontend/src/main.jsx`](../../frontend/src/main.jsx) | React bootstrap. |
| [`frontend/src/App.jsx`](../../frontend/src/App.jsx) | The whole UI + run/approve flow. |
| [`frontend/src/api.js`](../../frontend/src/api.js) | Tiny fetch/SSE client for the API. |
| [`frontend/src/styles.css`](../../frontend/src/styles.css) | Theme + layout. |
| [`frontend/vite.config.js`](../../frontend/vite.config.js) | Dev server + `/api` proxy. |
