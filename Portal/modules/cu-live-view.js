// CU live-view panel + active-sessions badge (M9 / D11, reframed by D14).
// One shared live view; any BlackBox user may watch. Virtual CU sessions are
// CONCURRENT (cap 3), so the badge shows a COUNT ("N agents running — watch"),
// not an exclusive lock — no per-operator gating. (An exclusive "desktop in use"
// warning is native-mode only, driven by display_arbiter, not this badge.)
const POLL_MS = 4000;
let _timer = null;
// Latest sessions snapshot, read at click time so the pill's handler never
// captures a stale session list (sessions rotate while the pill persists).
let latest = { active: false, sessions: [] };

async function fetchSessions() {
  try {
    const r = await fetch('/cu/sessions', { cache: 'no-store' });
    if (!r.ok) return { active: false, sessions: [] };
    return await r.json();
  } catch { return { active: false, sessions: [] }; }
}

function renderPill(state) {
  latest = state;
  let pill = document.getElementById('cuInUsePill');
  if (!state.active) { if (pill) pill.remove(); return; }
  if (!pill) {
    pill = document.createElement('button');
    pill.id = 'cuInUsePill';
    pill.className = 'cu-inuse-pill';
    // Read `latest` at click time — never close over this render's snapshot,
    // which would go stale as sessions rotate while the pill persists.
    pill.onclick = () => openStreamPanel(latest.sessions && latest.sessions[0]);
    (document.getElementById('statusLine') || document.body).appendChild(pill);
  }
  // D14: concurrent virtual sessions → a COUNT badge, not an exclusive lock.
  const n = state.count || state.sessions.length;
  pill.textContent = `● ${n} agent${n === 1 ? '' : 's'} running — watch`;
  pill.title = state.sessions
    .map(s => `${s.operator} (${s.backend} ${s.width}×${s.height})`).join(' · ');
}

// Open the streaming client panel for a /cu/sessions entry. Exported so
// cu-viewer-route.js (M4 entry-point routing: drawer "Live" button, in-bubble
// screenshot click, task-pill "Live") can reuse the one shared panel.
export function openStreamPanel(session) {
  if (!session) return;
  let panel = document.getElementById('cuLiveViewPanel');
  let frame = document.getElementById('cuLiveViewFrame');
  if (!panel || !frame) return;
  frame.src = session.view_url;            // /cu/view/{session_id}
  panel.style.display = 'block';
}

export function initCuLiveView() {
  // Re-entrancy guard: never leak a second polling interval on a re-init.
  if (_timer !== null) { clearInterval(_timer); _timer = null; }
  const closeBtn = document.getElementById('cuLiveViewClose');
  if (closeBtn) closeBtn.onclick = () => {
    const panel = document.getElementById('cuLiveViewPanel');
    const frame = document.getElementById('cuLiveViewFrame');
    if (frame) frame.src = 'about:blank';
    if (panel) panel.style.display = 'none';
  };
  const tick = async () => renderPill(await fetchSessions());
  tick();
  _timer = setInterval(tick, POLL_MS);
}
