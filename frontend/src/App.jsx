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

const ProjectCtx = createContext(null);
export const useProject = () => useContext(ProjectCtx);

function ProjectSwitcher() {
  const { projects, project, setProjectId, reload } = useProject();
  const [open, setOpen] = useState(false);
  const [creating, setCreating] = useState(false);
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
        </div>
      )}
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

      <div className="nav-foot">
        <div className="avatar">C</div>
        <div>
          <b>Collector</b>
          <small><a href="/" style={{ textDecoration: "none" }}>classic dashboard →</a></small>
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
