/**
 * GitHub Activity Analyst — Frontend entry-point
 *
 * Auth flow
 * ---------
 * 1. GET /auth/github/me  (credentials: 'include') to check session cookie.
 *    200 → show dashboard + fetch stats.
 *    401/403 → show login button.
 *
 * Dashboard
 * ---------
 * Renders four metrics from GET /stats/summary:
 *   - current_streak, longest_streak, active_repos  → numeric displays
 *   - weekly_velocity → single-bar Chart.js chart (7-day total)
 *
 * Sync
 * ----
 * POST /sync/github → loading state → re-fetch stats on success, inline
 * error on failure (never silent).
 */

import { Chart, BarController, BarElement, CategoryScale, LinearScale, Tooltip } from 'chart.js';
import './style.css';

Chart.register(BarController, BarElement, CategoryScale, LinearScale, Tooltip);

// ── Config ────────────────────────────────────────────────────────────────────

const BACKEND_URL = import.meta.env.VITE_BACKEND_URL;

if (!BACKEND_URL) {
  throw new Error('VITE_BACKEND_URL is not set. Add it to frontend/.env');
}

// ── DOM refs ──────────────────────────────────────────────────────────────────

const pageLoader        = document.getElementById('page-loader');
const authSection       = document.getElementById('auth-section');
const dashboardSection  = document.getElementById('dashboard-section');
const loginBtn          = document.getElementById('login-btn');
const usernameDisplay   = document.getElementById('username-display');

const currentStreakEl   = document.getElementById('current-streak-value');
const longestStreakEl   = document.getElementById('longest-streak-value');
const weeklyVelocityEl  = document.getElementById('weekly-velocity-value');
const activeReposEl     = document.getElementById('active-repos-value');
const lastUpdatedEl     = document.getElementById('last-updated');
const zeroDataPrompt    = document.getElementById('zero-data-prompt');

const syncBtn           = document.getElementById('sync-btn');
const syncBtnLabel      = document.getElementById('sync-btn-label');
const syncError         = document.getElementById('sync-error');

// ── Chart.js instance (created once, updated on each stats fetch) ─────────────

let weeklyChart = null;

function buildChart(velocity) {
  const ctx = document.getElementById('weekly-chart').getContext('2d');

  const data = {
    labels: ['Last 7 days'],
    datasets: [{
      label: 'Commits',
      data: [velocity],
      backgroundColor: 'rgba(45, 212, 191, 0.7)',
      borderColor: 'rgba(45, 212, 191, 1)',
      borderWidth: 2,
      borderRadius: 8,
      hoverBackgroundColor: 'rgba(45, 212, 191, 0.9)',
    }],
  };

  const options = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      tooltip: {
        callbacks: {
          label: (ctx) => ` ${ctx.raw} commits in the last 7 days`,
        },
        backgroundColor: 'rgba(22, 27, 34, 0.95)',
        titleColor: '#e6edf3',
        bodyColor: '#2dd4bf',
        borderColor: 'rgba(255,255,255,0.08)',
        borderWidth: 1,
      },
      legend: { display: false },
    },
    scales: {
      x: {
        grid:  { color: 'rgba(255,255,255,0.05)' },
        ticks: { color: '#8b949e', font: { family: 'Inter', size: 11 } },
      },
      y: {
        beginAtZero: true,
        grid:  { color: 'rgba(255,255,255,0.05)' },
        ticks: {
          color: '#8b949e',
          font: { family: 'Inter', size: 11 },
          stepSize: 1,
          precision: 0,
        },
      },
    },
    animation: { duration: 500, easing: 'easeInOutQuart' },
  };

  if (weeklyChart) {
    // Update existing chart in-place to avoid flickering
    weeklyChart.data.datasets[0].data = [velocity];
    weeklyChart.update();
  } else {
    weeklyChart = new Chart(ctx, { type: 'bar', data, options });
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function showLoader()  { pageLoader.classList.remove('hidden'); }
function hideLoader()  {
  pageLoader.classList.add('fade-out');
  setTimeout(() => pageLoader.classList.add('hidden'), 380);
}

function showAuth() {
  authSection.classList.remove('hidden');
  dashboardSection.classList.add('hidden');
}

function showDashboard() {
  authSection.classList.add('hidden');
  dashboardSection.classList.remove('hidden');
}

function setSyncLoading(loading) {
  syncBtn.disabled = loading;
  if (loading) {
    syncBtn.classList.add('syncing');
    syncBtnLabel.textContent = 'Syncing…';
  } else {
    syncBtn.classList.remove('syncing');
    syncBtnLabel.textContent = 'Sync now';
  }
}

function showSyncError(message) {
  syncError.textContent = `⚠ ${message}`;
  syncError.classList.remove('hidden');
}

function clearSyncError() {
  syncError.textContent = '';
  syncError.classList.add('hidden');
}

function formatUpdatedAt(isoString) {
  try {
    const d = new Date(isoString);
    return `Last updated: ${d.toUTCString()}`;
  } catch {
    return `Last updated: ${isoString}`;
  }
}

// ── Stats rendering ───────────────────────────────────────────────────────────

function renderStats(data) {
  const cs  = data.current_streak  ?? 0;
  const ls  = data.longest_streak  ?? 0;
  const wv  = data.weekly_velocity ?? 0;
  const ar  = data.active_repos    ?? 0;

  currentStreakEl.textContent  = cs;
  longestStreakEl.textContent  = ls;
  weeklyVelocityEl.textContent = wv;
  activeReposEl.textContent    = ar;

  buildChart(wv);

  lastUpdatedEl.textContent = data.computed_at_utc
    ? formatUpdatedAt(data.computed_at_utc)
    : '';

  // Zero-data state: all four metrics are 0 → prompt the user to sync
  const allZero = cs === 0 && ls === 0 && wv === 0 && ar === 0;
  zeroDataPrompt.classList.toggle('hidden', !allZero);
}

// ── API calls ─────────────────────────────────────────────────────────────────

async function fetchStats() {
  const resp = await fetch(`${BACKEND_URL}/stats/summary`, {
    credentials: 'include',
  });

  if (!resp.ok) {
    throw new Error(`/stats/summary returned ${resp.status}`);
  }

  const data = await resp.json();
  renderStats(data);
}

async function checkAuth() {
  const resp = await fetch(`${BACKEND_URL}/auth/github/me`, {
    credentials: 'include',
  });

  if (resp.ok) {
    const user = await resp.json();
    usernameDisplay.textContent = `@${user.github_username}`;
    showDashboard();
    await fetchStats();
  } else {
    // 401 or 403: show login screen
    loginBtn.href = `${BACKEND_URL}/auth/github/login`;
    showAuth();
  }
}

async function syncNow() {
  clearSyncError();
  setSyncLoading(true);

  try {
    const resp = await fetch(`${BACKEND_URL}/sync/github`, {
      method: 'POST',
      credentials: 'include',
    });

    if (!resp.ok) {
      let detail = `Sync failed (HTTP ${resp.status})`;
      try {
        const body = await resp.json();
        if (body?.detail) detail = body.detail;
      } catch { /* ignore JSON parse error */ }
      throw new Error(detail);
    }

    // Sync succeeded — refresh stats
    await fetchStats();
  } catch (err) {
    showSyncError(err.message ?? 'An unexpected error occurred during sync.');
  } finally {
    setSyncLoading(false);
  }
}

// ── Event listeners ───────────────────────────────────────────────────────────

syncBtn.addEventListener('click', syncNow);

// ── Bootstrap ─────────────────────────────────────────────────────────────────

(async () => {
  showLoader();
  try {
    await checkAuth();
  } catch (err) {
    // Catastrophic failure (network down etc.) — fall back to auth screen
    console.error('[bootstrap] Auth check failed:', err);
    loginBtn.href = `${BACKEND_URL}/auth/github/login`;
    showAuth();
  } finally {
    hideLoader();
  }
})();
