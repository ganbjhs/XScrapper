// The shell: left navbar (project switcher on top, project-scoped views,
// global section below), routing, and the shared project context.
import React, { createContext, useContext, useEffect, useMemo, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api, useApi } from "./api/client.js";
import { Modal, icons } from "./components/ui.jsx";
import LiveFeed from "./views/LiveFeed.jsx";
import Watchlists from "./views/Watchlists.jsx";
import Search from "./views/Search.jsx";
import Collections from "./views/Collections.jsx";
import Alerts from "./views/Alerts.jsx";
import Delivery from "./views/Delivery.jsx";
import Activity from "./views/Activity.jsx";
import Accounts from "./views/Accounts.jsx";
import Guard from "./views/Guard.jsx";
import StressTest from "./views/StressTest.jsx";

const ProjectCtx = createContext(null);
export const useProject = () => useContext(ProjectCtx);

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

function ProjectSwitcher() {
  const { projects, project, setProjectId, reload } = useProject();
  const [open, setOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [managing, setManaging] = useState(false);
  const [name, setName] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const create = async () => {
    setBusy(true);
    setErr("");
    try {
      const made = await api.createProject(name);
      await reload();
      setProjectId(made.project_id);
      setCreating(false);
      setName("");
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="proj-switch">
      <button onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        {project ? project.name : "No project"}
        <span className="caret">▼</span>
      </button>
      {open && (
        <div className="proj-menu" onMouseLeave={() => setOpen(false)}>
          {projects.map((p) => (
            <button key={p.project_id}
                    className={p.project_id === project?.project_id ? "sel" : ""}
                    onClick={() => { setProjectId(p.project_id); setOpen(false); }}>
              {p.name}
              {p.archived ? " (archived)" : ""}
            </button>
          ))}
          <button className="new" onClick={() => { setOpen(false); setCreating(true); }}>
            + New project
          </button>
          <button className="new" onClick={() => { setOpen(false); setManaging(true); }}>
            Manage projects…
          </button>
        </div>
      )}
      {managing && <ManageProjects onClose={() => setManaging(false)} />}
      {creating && (
        <Modal title="New project" sub="A project groups watchlists, feeds and delivery."
               onClose={() => setCreating(false)}>
          <div className="field">
            <label htmlFor="pname">Name</label>
            <input id="pname" value={name} autoFocus
                   onChange={(e) => setName(e.target.value)}
                   onKeyDown={(e) => e.key === "Enter" && name.trim() && create()}
                   placeholder="e.g. Elections 2026" />
          </div>
          {err && <div className="err">{err}</div>}
          <div className="row">
            <button className="btn btn-ghost" onClick={() => setCreating(false)}>Cancel</button>
            <button className="btn btn-brand" disabled={!name.trim() || busy} onClick={create}>
              Create
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}

function Nav({ open, close }) {
  const { data: delivery } = useApi(() => api.delivery(), [], { every: 30_000 });
  const behind = (delivery?.targets || []).reduce((a, t) => a + (t.behind || 0), 0);
  const item = (to, icon, label, pill = null) => (
    <NavLink to={to} className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}
             onClick={close}>
      {icons[icon]}
      {label}
      {pill}
    </NavLink>
  );
  return (
    <nav className={`side${open ? " open" : ""}`}>
      <div className="brand">
        <div className="logo">◎</div>
        <div>
          <b>Collector</b>
          <small>DATA → WATCH-TOWER</small>
        </div>
      </div>

      <div className="nav-label">PROJECT</div>
      <ProjectSwitcher />
      <div style={{ height: 10 }} />
      {item("/feed", "feed", "Live Feed")}
      {item("/watchlists", "watchlists", "Watchlists")}
      {item("/search", "search", "Search")}
      {item("/collections", "collections", "Collections")}
      {item("/alerts", "alerts", "Alerts")}
      {item("/delivery", "delivery", "Delivery",
        behind > 0 ? <span className="pill warn">{behind} behind</span> : null)}
      {item("/activity", "activity", "Activity Log")}

      <div className="nav-label">GLOBAL</div>
      {item("/accounts", "accounts", "Accounts & Sessions")}
      {item("/guard", "guard", "Guard")}
      {item("/stress", "stress", "Stress Test")}

      <div className="nav-foot">
        <div className="avatar">C</div>
        <div>
          <b>Collector</b>
          <small><a href="/logout" style={{ textDecoration: "none" }}>sign out →</a></small>
        </div>
      </div>
    </nav>
  );
}

export default function App() {
  const { data, error, loading, reload } = useApi(() => api.projects(), []);
  const projects = useMemo(
    () => (data?.projects || []).filter((p) => !p.archived),
    [data],
  );
  const [projectId, setProjectId] = useState(() => {
    const v = Number(localStorage.getItem("collector.project"));
    return Number.isFinite(v) && v > 0 ? v : null;
  });
  useEffect(() => {
    if (projectId) localStorage.setItem("collector.project", String(projectId));
  }, [projectId]);

  // Fall back to the first project when the stored one is gone.
  const project =
    projects.find((p) => p.project_id === projectId) || projects[0] || null;
  useEffect(() => {
    if (project && project.project_id !== projectId) setProjectId(project.project_id);
  }, [project, projectId]);

  const [navOpen, setNavOpen] = useState(false);

  const ctx = { projects, project, setProjectId, reload, projectsError: error, projectsLoading: loading };
  return (
    <ProjectCtx.Provider value={ctx}>
      <div className="shell">
        <Nav open={navOpen} close={() => setNavOpen(false)} />
        {navOpen && <div className="nav-scrim" onClick={() => setNavOpen(false)} />}
        <main className="content">
          <Routes>
            <Route path="/" element={<Navigate to="/feed" replace />} />
            <Route path="/feed" element={<LiveFeed onMenu={() => setNavOpen(true)} />} />
            <Route path="/watchlists" element={<Watchlists onMenu={() => setNavOpen(true)} />} />
            <Route path="/search" element={<Search onMenu={() => setNavOpen(true)} />} />
            <Route path="/collections" element={<Collections onMenu={() => setNavOpen(true)} />} />
            <Route path="/alerts" element={<Alerts onMenu={() => setNavOpen(true)} />} />
            <Route path="/delivery" element={<Delivery onMenu={() => setNavOpen(true)} />} />
            <Route path="/activity" element={<Activity onMenu={() => setNavOpen(true)} />} />
            <Route path="/accounts" element={<Accounts onMenu={() => setNavOpen(true)} />} />
            <Route path="/guard" element={<Guard onMenu={() => setNavOpen(true)} />} />
            <Route path="/stress" element={<StressTest onMenu={() => setNavOpen(true)} />} />
            <Route path="*" element={<Navigate to="/feed" replace />} />
          </Routes>
        </main>
      </div>
    </ProjectCtx.Provider>
  );
}

// Shared page header with the mobile menu button.
export function PageHead({ title, sub, onMenu, children }) {
  return (
    <header className="top">
      <button className="menu-btn" onClick={onMenu} aria-label="Open navigation">
        {icons.menu}
      </button>
      <div>
        <h1>{title}</h1>
        {sub && <div className="sub">{sub}</div>}
      </div>
      <div className="grow" />
      {children}
    </header>
  );
}
