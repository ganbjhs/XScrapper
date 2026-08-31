# Manage Projects — the UI (rename / archive / delete with a preview)

*The React side of the feature as shipped in the Collector (commit `066fe95`),
for reuse in a tool whose project switcher already looks the same. Nothing
here is about streams or the scraper; §5 only says what JSON the modal
expects back so the other tool's backend can return that shape.*

## 1. What the user sees

The project switcher's dropdown gets one more entry under **+ New project**:
**Manage projects…**. It opens a modal listing every project, archived ones
included, one row each with the name, a small line of counts, and three
buttons:

- **Rename** — turns the name into an inline input. Enter saves, Escape
  cancels, blur saves. Nothing is sent if the name did not change.
- **Archive / Unarchive** — toggles the soft hide.
- **Delete…** — disabled (with a tooltip) when only one project exists.
  Otherwise it swaps the modal for a second one:
  a "Will be deleted" list and a "Kept — shared with another project" list
  (both filled from a dry-run call to the server), then a text field
  "Type the project name to confirm". The red button stays disabled until
  the typed text equals the name exactly; Enter submits when it does.

After a successful delete the lists reload, and if the deleted project was
the selected one the selection is cleared so the app falls back to the
first remaining project.

## 2. API client — three calls (`api/client.js`)

```js
  renameProject: (project_id, name) =>
    request("/api/projects", { method: "POST", body: { project_id, name } }),
  projectDeletePlan: (project_id) =>
    request("/api/projects/delete-plan", { method: "POST", body: { project_id } }),
  deleteProject: (project_id, confirm) =>
    request("/api/projects/delete", { method: "POST", body: { project_id, confirm } }),
```

`archiveProject(project_id, archived)` and `projects()` already existed and
are reused.

## 3. Wiring into `ProjectSwitcher`

```jsx
  const [managing, setManaging] = useState(false);
  ...
          <button className="new" onClick={() => { setOpen(false); setManaging(true); }}>
            Manage projects…
          </button>
  ...
      {managing && <ManageProjects onClose={() => setManaging(false)} />}
```

## 4. The component (placed above `ProjectSwitcher` in `App.jsx`)

Uses what the switcher already has: `useProject()` → `{ reload, setProjectId, project }`,
`useApi(fn, deps)` → `{ data, reload }`, `api`, and the shared `Modal`
(`title`, `sub`, `onClose`, children; closes on Escape and backdrop click).

```jsx
// Rename / archive / delete, for every project including archived ones.
// Delete is two steps: the server's dry-run plan is shown first (what goes,
// what is shared and therefore kept), then the operator types the name.
function ManageProjects({ onClose }) {
  const { reload, setProjectId, project } = useProject();
  const { data, reload: reloadAll } = useApi(() => api.projects(), []);
  const all = data?.projects || [];
  const [editing, setEditing] = useState(null);      // project_id being renamed
  const [draft, setDraft] = useState("");
  const [deleting, setDeleting] = useState(null);    // { project, plan }
  const [typed, setTyped] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = async () => { await reloadAll(); await reload(); };

  const rename = async (p) => {
    if (!draft.trim() || draft.trim() === p.name) { setEditing(null); return; }
    setBusy(true); setErr("");
    try { await api.renameProject(p.project_id, draft); setEditing(null); await refresh(); }
    catch (e) { setErr(String(e.message || e)); }
    finally { setBusy(false); }
  };

  const archive = async (p) => {
    setBusy(true); setErr("");
    try { await api.archiveProject(p.project_id, !p.archived); await refresh(); }
    catch (e) { setErr(String(e.message || e)); }
    finally { setBusy(false); }
  };

  const planDelete = async (p) => {
    setBusy(true); setErr(""); setTyped("");
    try { setDeleting({ project: p, plan: await api.projectDeletePlan(p.project_id) }); }
    catch (e) { setErr(String(e.message || e)); }
    finally { setBusy(false); }
  };

  const doDelete = async () => {
    const p = deleting.project;
    setBusy(true); setErr("");
    try {
      await api.deleteProject(p.project_id, typed);
      setDeleting(null);
      await refresh();
      if (project?.project_id === p.project_id) setProjectId(null);
    } catch (e) { setErr(String(e.message || e)); }
    finally { setBusy(false); }
  };

  if (deleting) {
    const { project: p, plan } = deleting;
    const n = (k) => plan[k] || 0;
    const ig = plan.platforms?.instagram || {};
    const fb = plan.platforms?.facebook || {};
    return (
      <Modal title={`Delete “${p.name}”?`}
             sub="This cannot be undone. X only reaches back about a week, so purged posts older than that can never be collected again."
             onClose={() => setDeleting(null)}>
        <div className="plan">
          <div className="plan-h">Will be deleted</div>
          <ul>
            <li><b>{n("posts_deleted").toLocaleString()}</b> X posts that no other project's stream also matched</li>
            <li><b>{plan.streams_purged.length}</b> stream(s) only this project used
              {plan.streams_purged.length > 0 && <span className="mono"> — {plan.streams_purged.join(", ")}</span>}</li>
            <li><b>{n("watchlists")}</b> watchlist(s), <b>{n("collections")}</b> collection(s), <b>{n("labels")}</b> label(s)</li>
            <li><b>{n("delivery_targets")}</b> delivery target(s), <b>{n("alerts")}</b> alert(s)</li>
            {(ig.posts || ig.sources || fb.posts || fb.sources) ? (
              <li>Instagram: <b>{ig.sources || 0}</b> source(s), <b>{ig.posts || 0}</b> post(s) ·
                  Facebook: <b>{fb.sources || 0}</b> page(s), <b>{fb.posts || 0}</b> post(s)</li>
            ) : null}
          </ul>
          <div className="plan-h">Kept — shared with another project</div>
          <ul>
            <li><b>{n("posts_kept_shared").toLocaleString()}</b> X posts also matched by a surviving stream</li>
            {plan.streams_shared.length > 0 ? plan.streams_shared.map((s) => (
              <li key={s.stream_id}><span className="mono">{s.label}</span> — only this project's tag is removed; still in {s.also_in.join(", ")}</li>
            )) : <li>no shared streams</li>}
            {plan.streams_kept_config.length > 0 && (
              <li>Declared in config.toml, paused not deleted: <span className="mono">{plan.streams_kept_config.join(", ")}</span></li>
            )}
          </ul>
        </div>
        <div className="field">
          <label htmlFor="pdel">Type the project name to confirm</label>
          <input id="pdel" value={typed} autoFocus placeholder={p.name}
                 onChange={(e) => setTyped(e.target.value)}
                 onKeyDown={(e) => e.key === "Enter" && typed.trim() === p.name && doDelete()} />
        </div>
        {err && <div className="err">{err}</div>}
        <div className="row">
          <button className="btn btn-ghost" onClick={() => setDeleting(null)}>Cancel</button>
          <button className="btn btn-danger" disabled={busy || typed.trim() !== p.name} onClick={doDelete}>
            Delete project and its data
          </button>
        </div>
      </Modal>
    );
  }

  return (
    <Modal title="Manage projects" sub="Rename, archive (hide but keep), or delete a project and the data only it owns."
           onClose={onClose}>
      <div className="proj-manage">
        {all.map((p) => (
          <div key={p.project_id} className={`proj-row${p.archived ? " archived" : ""}`}>
            {editing === p.project_id ? (
              <input value={draft} autoFocus onChange={(e) => setDraft(e.target.value)}
                     onKeyDown={(e) => { if (e.key === "Enter") rename(p); if (e.key === "Escape") setEditing(null); }}
                     onBlur={() => rename(p)} />
            ) : (
              <div className="name">
                <b>{p.name}</b>
                <small>#{p.project_id} · {p.watchlists} watchlist(s) · {p.streams} stream(s){p.archived ? " · archived" : ""}</small>
              </div>
            )}
            <div className="acts">
              <button className="btn btn-ghost btn-sm" disabled={busy}
                      onClick={() => { setEditing(p.project_id); setDraft(p.name); }}>Rename</button>
              <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => archive(p)}>
                {p.archived ? "Unarchive" : "Archive"}
              </button>
              <button className="btn btn-danger btn-sm" disabled={busy || all.length < 2}
                      title={all.length < 2 ? "Create another project first" : ""}
                      onClick={() => planDelete(p)}>Delete…</button>
            </div>
          </div>
        ))}
      </div>
      {err && <div className="err">{err}</div>}
      <div className="row">
        <button className="btn btn-ghost" onClick={onClose}>Close</button>
      </div>
    </Modal>
  );
}
```

## 5. CSS (appended to `styles.css`)

Reuses `.btn`, `.btn-ghost`, `.btn-danger`, `.btn-sm`, `.field`, `.row`,
`.err` and the `.modal` rules that already exist.

```css
/* Manage projects modal */
.proj-manage { display: flex; flex-direction: column; gap: 6px; margin-top: 12px; }
.proj-row { display: flex; align-items: center; gap: 10px; padding: 8px 10px; border: 1px solid var(--ring); border-radius: 10px; }
.proj-row.archived { opacity: 0.6; }
.proj-row .name { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.proj-row .name small { color: var(--ink-3); font-size: 11.5px; }
.proj-row input { flex: 1; min-width: 0; padding: 6px 8px; border: 1px solid var(--ring); border-radius: 8px; font: inherit; }
.proj-row .acts { display: flex; gap: 6px; flex-shrink: 0; }
.plan { margin-top: 12px; font-size: 13px; }
.plan-h { font-size: 11px; letter-spacing: 0.8px; text-transform: uppercase; color: var(--ink-3); font-weight: 700; margin: 12px 0 4px; }
.plan ul { margin: 0; padding-left: 18px; }
.plan li { margin: 3px 0; }
.plan .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
```

## 6. What the modal expects from the server

Errors are `{"error": "…"}`; the client throws them and the modal shows the
message under the list. The delete-plan response is rendered directly, so
these keys are what matter — rename them in the JSX if the other tool's
backend uses different words:

```json
{
  "name": "Vishnu Deo Sai Ji",
  "posts_deleted": 14373,
  "posts_kept_shared": 212,
  "streams_purged": ["wl:5:0"],
  "streams_shared": [{"stream_id": 9, "label": "wl:2:0", "also_in": ["News Outlets"]}],
  "streams_kept_config": [],
  "watchlists": 2, "collections": 1, "labels": 0, "delivery_targets": 1, "alerts": 0,
  "platforms": {"instagram": {"sources": 0, "posts": 0}, "facebook": {"sources": 0, "posts": 0}}
}
```

`name` and the three list keys (`streams_purged`, `streams_shared`,
`streams_kept_config` — empty arrays are fine) must be present; the JSX
calls `.length`/`.map` on them. Every count defaults to 0 (`n(k)`) and
`platforms` to empty (`|| {}`), so a simpler backend can omit those and the
dialog still renders. If the other tool has no notion of streams, drop
those three `<li>` blocks and the list keys go away too.
`POST /api/projects/delete`
takes `{project_id, confirm}` and should refuse unless `confirm` equals the
name — the button-disable in the UI is a convenience, the server check is
the guard. Rename is `POST /api/projects {project_id, name}` → `{ok: true}`.
