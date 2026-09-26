// The shell: navbar (project switcher, project-scoped views, global section),
// routing, theme / sidebar state, keyboard shortcuts, shared project context.
import React, { createContext, useContext, useEffect, useMemo, useState } from "react";
import { NavLink, Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { api, sortBy, useApi } from "./api/client.js";
import { Modal, ShortcutsModal, ToastHost, WtBadge, icons, toast } from "./components/ui.jsx";
import LiveFeed from "./views/LiveFeed.jsx";
import Watchlists from "./views/Watchlists.jsx";
import Search from "./views/Search.jsx";
import Collections from "./views/Collections.jsx";
import Alerts from "./views/Alerts.jsx";
import Delivery from "./views/Delivery.jsx";
import Activity from "./views/Activity.jsx";
import Accounts from "./views/Accounts.jsx";
import Settings from "./views/Settings.jsx";
import Guard from "./views/Guard.jsx";
import StressTest from "./views/StressTest.jsx";

const ProjectCtx = createContext(null);
export const useProject = () => useContext(ProjectCtx);

// Rename / archive / delete. Delete shows the server's dry-run plan first,
// then asks for the project name.
function ManageProjects({ onClose }) {
  const { reload, setProjectId, project, consumers } = useProject();
  const { data, reload: reloadAll } = useApi(() => api.projects(), []);
  const all = sortBy(data?.projects || []);
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
            {(plan.watchlists_transferred || []).length > 0 && (
              <li>{plan.watchlists_transferred.length} shared watchlist(s) handed to the project that also uses them:
                {" "}{plan.watchlists_transferred.map((t) => `${t.name} → ${t.to_project}`).join(", ")}</li>
            )}
            {(plan.watchlists_detached || 0) > 0 && (
              <li>{plan.watchlists_detached} watchlist(s) added from other projects — unlinked, they stay where they were created</li>
            )}
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
            <WtBadge consumers={consumers} project={p} />
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
  const { projects, project, setProjectId, reload, consumers } = useProject();
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
      <button onClick={() => setOpen((o) => !o)} aria-expanded={open} title={project?.name}>
        <span className="pmark">{(project?.name || "?").slice(0, 1).toUpperCase()}</span>
        <span className="pname">{project ? project.name : "No project"}</span>
        {project && <WtBadge consumers={consumers} project={project} compact />}
        <svg className="caret" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6" /></svg>
      </button>
      {open && (
        <div className="proj-menu" onMouseLeave={() => setOpen(false)}>
          {projects.map((p) => (
            <button key={p.project_id}
                    className={p.project_id === project?.project_id ? "sel" : ""}
                    onClick={() => { setProjectId(p.project_id); setOpen(false); }}>
              <span className="pname">{p.name}{p.archived ? " (archived)" : ""}</span>
              <WtBadge consumers={consumers} project={p} />
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

const THEMES = ["system", "light", "dark"];
const THEME_ICON = { system: "auto", light: "sun", dark: "moon" };
const THEME_LABEL = { system: "Theme: follows system", light: "Theme: light", dark: "Theme: dark" };

const NAV = [
  { to: "/feed", icon: "feed", label: "Live Feed", key: "f" },
  { to: "/watchlists", icon: "watchlists", label: "Watchlists", key: "w" },
  { to: "/search", icon: "search", label: "Search", key: "s" },
  { to: "/collections", icon: "collections", label: "Collections", key: "c" },
  { to: "/alerts", icon: "alerts", label: "Alerts", key: "a" },
  { to: "/delivery", icon: "delivery", label: "Delivery", key: "d" },
  { to: "/activity", icon: "activity", label: "Activity Log", key: "l" },
];
const NAV_GLOBAL = [
  { to: "/accounts", icon: "accounts", label: "Accounts & Sessions", key: "u" },
  { to: "/guard", icon: "guard", label: "Guard", key: "g" },
  { to: "/settings", icon: "settings", label: "Settings", key: "t" },
  { to: "/stress", icon: "stress", label: "Stress Test" },
];

function Nav({ open, close, rail, toggleRail, theme, cycleTheme, showShortcuts }) {
  const { data: delivery } = useApi(() => api.delivery(), [], { every: 30_000 });
  const behind = (delivery?.targets || []).reduce((a, t) => a + (t.behind || 0), 0);
  const item = ({ to, icon, label, key }) => (
    <NavLink key={to} to={to} title={rail ? label : undefined}
             className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}
             onClick={close}>
      {icons[icon]}
      <span className="lbl">{label}</span>
      {key && <span className="kbd-hint">g {key}</span>}
      {to === "/delivery" && behind > 0 && <span className="pill warn" data-n={behind}><span>{behind} behind</span></span>}
    </NavLink>
  );
  return (
    <nav className={`side${open ? " open" : ""}`}>
      <div className="brand">
        <div className="logo">◎</div>
        <div className="txt">
          <b>Collector</b>
          <small>DATA → WATCH-TOWER</small>
        </div>
        <button className="nav-collapse" onClick={toggleRail}
                aria-label={rail ? "Expand sidebar" : "Collapse sidebar"} title={`${rail ? "Expand" : "Collapse"} sidebar  [`}>
          {icons.collapse}
        </button>
      </div>

      <div className="nav-label">Project</div>
      <ProjectSwitcher />
      <div style={{ height: 8 }} />
      {NAV.map(item)}

      <div className="nav-label">Global</div>
      <div className="nav-divider" />
      {NAV_GLOBAL.map(item)}

      <div className="nav-foot">
        <div className="avatar">C</div>
        <div className="who">
          <b>Collector</b>
          <small><a href="/logout" style={{ textDecoration: "none" }}>sign out →</a></small>
        </div>
      </div>
      <div className="nav-tools">
        <button className="icon-btn" onClick={cycleTheme} title={`${THEME_LABEL[theme]}  \\`} aria-label="Switch theme">
          {icons[THEME_ICON[theme]]}
        </button>
        <button className="icon-btn" onClick={showShortcuts} title="Keyboard shortcuts  ?" aria-label="Keyboard shortcuts">
          {icons.keyboard}
        </button>
        <a className="icon-btn logout" href="/logout" title="Sign out" aria-label="Sign out">{icons.logout}</a>
      </div>
    </nav>
  );
}

// Phone-width bottom bar: the four everyday pages plus the full menu.
function MobileNav({ openMenu }) {
  const { data: delivery } = useApi(() => api.delivery(), [], { every: 30_000 });
  const behind = (delivery?.targets || []).reduce((a, t) => a + (t.behind || 0), 0);
  const item = ({ to, icon, label }) => (
    <NavLink key={to} to={to} className={({ isActive }) => (isActive ? "active" : "")}>
      {icons[icon]}{label}
    </NavLink>
  );
  return (
    <nav className="mnav" aria-label="Quick navigation">
      {NAV.slice(0, 4).map(item)}
      <button onClick={openMenu} aria-label="Open full menu">
        {icons.more}Menu
        {behind > 0 && <span className="pill">{behind}</span>}
      </button>
    </nav>
  );
}

const ls = {
  get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch {} },
};

function useTheme() {
  const [theme, setTheme] = useState(() => (THEMES.includes(ls.get("collector.theme")) ? ls.get("collector.theme") : "system"));
  useEffect(() => {
    const el = document.documentElement;
    if (theme === "system") delete el.dataset.theme; else el.dataset.theme = theme;
    ls.set("collector.theme", theme);
  }, [theme]);
  const ref = React.useRef(theme);
  ref.current = theme;
  const cycle = React.useCallback(() => {
    const next = THEMES[(THEMES.indexOf(ref.current) + 1) % THEMES.length];
    setTheme(next);
    toast(THEME_LABEL[next], { ms: 1600 });
  }, []);
  return [theme, cycle];
}

// g + key navigation, "/" focuses the first filter box, "[" toggles the rail,
// "\\" cycles the theme, "?" opens help. Ignored while typing in a field.
function useShortcuts({ toggleRail, cycleTheme, showShortcuts }) {
  const navigate = useNavigate();
  useEffect(() => {
    let pendingG = 0;
    const typing = (e) => {
      const t = e.target;
      return t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
    };
    const h = (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey || typing(e)) return;
      const now = Date.now();
      if (pendingG && now - pendingG < 1200) {
        pendingG = 0;
        const hit = [...NAV, ...NAV_GLOBAL].find((n) => n.key === e.key);
        if (hit) { e.preventDefault(); navigate(hit.to); }
        return;
      }
      pendingG = 0;
      if (e.key === "g") { pendingG = now; return; }
      if (e.key === "/") {
        const box = document.querySelector('main input[type="search"], main .filters input, main .field input, main input');
        if (box) { e.preventDefault(); box.focus(); box.select?.(); }
      } else if (e.key === "[") { e.preventDefault(); toggleRail(); }
      else if (e.key === "\\") { e.preventDefault(); cycleTheme(); }
      else if (e.key === "?") { e.preventDefault(); showShortcuts(); }
    };
    addEventListener("keydown", h);
    return () => removeEventListener("keydown", h);
  }, [navigate, toggleRail, cycleTheme, showShortcuts]);
}

export default function App() {
  const { data, error, loading, reload } = useApi(() => api.projects(), []);
  const projects = useMemo(
    () => sortBy((data?.projects || []).filter((p) => !p.archived)),
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
  const [rail, setRail] = useState(() => ls.get("collector.nav") === "rail");
  useEffect(() => { ls.set("collector.nav", rail ? "rail" : "full"); }, [rail]);
  const toggleRail = React.useCallback(() => setRail((r) => !r), []);
  const [theme, cycleTheme] = useTheme();
  const [help, setHelp] = useState(false);
  const showShortcuts = React.useCallback(() => setHelp(true), []);
  useShortcuts({ toggleRail, cycleTheme, showShortcuts });

  // Who is pulling which project through the API (Watch-Tower). Polled so a
  // project going live or quiet shows within a minute.
  const { data: consumers } = useApi(() => api.consumers().catch(() => null), [], { every: 60_000 });

  const ctx = { projects, project, setProjectId, reload, projectsError: error, projectsLoading: loading, consumers };
  return (
    <ProjectCtx.Provider value={ctx}>
      <div className={`shell${rail ? " rail" : ""}`}>
        <Nav open={navOpen} close={() => setNavOpen(false)} rail={rail} toggleRail={toggleRail}
             theme={theme} cycleTheme={cycleTheme} showShortcuts={showShortcuts} />
        {navOpen && <div className="nav-scrim" onClick={() => setNavOpen(false)} />}
        <MobileNav openMenu={() => setNavOpen(true)} />
        <ToastHost />
        {help && <ShortcutsModal onClose={() => setHelp(false)} />}
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
            <Route path="/settings" element={<Settings onMenu={() => setNavOpen(true)} />} />
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
