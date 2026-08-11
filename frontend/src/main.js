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
 * Fetches all stats endpoints in parallel:
 *   GET /stats/summary         → four metric cards + weekly bar chart
 *   GET /stats/heatmap         → 365-day CSS-grid contribution heatmap
 *   GET /stats/repos           → per-repo horizontal bar chart (last 30 days)
 *   GET /stats/activity-pattern → hour-of-day + day-of-week bar charts
 *
 * Each new section renders independently — a failure in one doesn't block
 * the others.
 *
 * Sync
 * ----
 * POST /sync/github → loading state → re-fetch all stats on success.
 */

import {
  Chart,
  BarController,
  BarElement,
  CategoryScale,
  LinearScale,
  Tooltip,
} from 'chart.js';
import './style.css';

Chart.register(BarController, BarElement, CategoryScale, LinearScale, Tooltip);

// ── Config ─────────────────────────────────────────────────────────────────────

const BACKEND_URL = import.meta.env.VITE_BACKEND_URL;

if (!BACKEND_URL) {
  throw new Error('VITE_BACKEND_URL is not set. Add it to frontend/.env');
}

// ── DOM refs ────────────────────────────────────────────────────────────────────

const pageLoader       = document.getElementById('page-loader');
const authSection      = document.getElementById('auth-section');
const dashboardSection = document.getElementById('dashboard-section');
const loginBtn         = document.getElementById('login-btn');
const usernameDisplay  = document.getElementById('username-display');

const currentStreakEl  = document.getElementById('current-streak-value');
const longestStreakEl  = document.getElementById('longest-streak-value');
const weeklyVelocityEl = document.getElementById('weekly-velocity-value');
const activeReposEl    = document.getElementById('active-repos-value');
const lastUpdatedEl    = document.getElementById('last-updated');
const zeroDataPrompt   = document.getElementById('zero-data-prompt');

const syncBtn          = document.getElementById('sync-btn');
const syncBtnLabel     = document.getElementById('sync-btn-label');
const syncError        = document.getElementById('sync-error');

const heatmapGrid      = document.getElementById('heatmap-grid');
const heatmapError     = document.getElementById('heatmap-error');

const reposError       = document.getElementById('repos-error');
const reposEmpty       = document.getElementById('repos-empty');

const patternError     = document.getElementById('pattern-error');

// ── Chart instances (created once, updated on each stats fetch) ────────────────

let weeklyChart = null;
let reposChart  = null;
let hourChart   = null;
let dowChart    = null;

// ── Shared chart options factory ────────────────────────────────────────────────

function baseBarOptions(extraScaleY = {}) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: 'rgba(22, 27, 34, 0.95)',
        titleColor: '#e6edf3',
        bodyColor: '#2dd4bf',
        borderColor: 'rgba(255,255,255,0.08)',
        borderWidth: 1,
      },
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
          precision: 0,
          stepSize: 1,
        },
        ...extraScaleY,
      },
    },
    animation: { duration: 500, easing: 'easeInOutQuart' },
  };
}

// ── Weekly velocity chart (existing) ───────────────────────────────────────────

function buildWeeklyChart(velocity) {
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
    ...baseBarOptions(),
    plugins: {
      ...baseBarOptions().plugins,
      tooltip: {
        ...baseBarOptions().plugins.tooltip,
        callbacks: {
          label: (c) => ` ${c.raw} commits in the last 7 days`,
        },
      },
    },
  };

  if (weeklyChart) {
    weeklyChart.data.datasets[0].data = [velocity];
    weeklyChart.update();
  } else {
    weeklyChart = new Chart(ctx, { type: 'bar', data, options });
  }
}

// ── Heatmap ─────────────────────────────────────────────────────────────────────

/**
 * Map a commit count to one of 5 intensity classes (hc-0 … hc-4).
 * Thresholds loosely match GitHub's own scale.
 */
function heatLevel(count) {
  if (count === 0) return 0;
  if (count <= 2)  return 1;
  if (count <= 5)  return 2;
  if (count <= 10) return 3;
  return 4;
}

function renderHeatmap(days) {
  heatmapGrid.innerHTML = '';

  // days is already 365 entries oldest-first from the backend.
  // Build a map for O(1) lookup.
  const byDate = {};
  for (const d of days) byDate[d.date] = d.commit_count;

  // We want columns = weeks (Mon → Sun vertically).
  // Find the ISO date of the first day's Monday so grid aligns correctly.
  const firstDate = new Date(days[0].date + 'T00:00:00Z');
  // dayOfWeek: 0=Sun,1=Mon…6=Sat → convert to Mon=0..Sun=6
  const firstDow = (firstDate.getUTCDay() + 6) % 7; // 0=Mon

  // Prepend `firstDow` blank spacer cells so day-0 lands in the right row.
  for (let i = 0; i < firstDow; i++) {
    const spacer = document.createElement('span');
    spacer.className = 'heatmap-cell hc-empty';
    heatmapGrid.appendChild(spacer);
  }

  for (const d of days) {
    const count = d.commit_count;
    const cell  = document.createElement('span');
    cell.className = `heatmap-cell hc-${heatLevel(count)}`;
    cell.title = `${d.date}: ${count} commit${count !== 1 ? 's' : ''}`;
    cell.setAttribute('aria-label', cell.title);
    heatmapGrid.appendChild(cell);
  }
}

// ── Per-repo chart ──────────────────────────────────────────────────────────────

function renderReposChart(repos) {
  const hasData = repos.length > 0;

  reposEmpty.classList.toggle('hidden', hasData);
  document.querySelector('.chart-container--repos').classList.toggle('hidden', !hasData);

  if (!hasData) return;

  const labels = repos.map((r) => r.repo_name);
  const values = repos.map((r) => r.commit_count);
  const ctx    = document.getElementById('repos-chart').getContext('2d');

  const dataset = {
    label: 'Commits',
    data: values,
    backgroundColor: 'rgba(45, 212, 191, 0.65)',
    borderColor: 'rgba(45, 212, 191, 1)',
    borderWidth: 1,
    borderRadius: 6,
    hoverBackgroundColor: 'rgba(45, 212, 191, 0.85)',
  };

  if (reposChart) {
    reposChart.data.labels = labels;
    reposChart.data.datasets[0].data = values;
    reposChart.update();
  } else {
    reposChart = new Chart(ctx, {
      type: 'bar',
      data: { labels, datasets: [dataset] },
      options: baseBarOptions(),
    });
  }
}

// ── Activity pattern charts ──────────────────────────────────────────────────────

function renderPatternCharts(pattern) {
  // Hour chart
  const hourLabels  = pattern.by_hour_utc.map((h) => String(h.hour).padStart(2, '0'));
  const hourValues  = pattern.by_hour_utc.map((h) => h.commit_count);
  const hourCtx     = document.getElementById('hour-chart').getContext('2d');

  if (hourChart) {
    hourChart.data.labels = hourLabels;
    hourChart.data.datasets[0].data = hourValues;
    hourChart.update();
  } else {
    hourChart = new Chart(hourCtx, {
      type: 'bar',
      data: {
        labels: hourLabels,
        datasets: [{
          label: 'Commits',
          data: hourValues,
          backgroundColor: 'rgba(99, 179, 237, 0.65)',
          borderColor: 'rgba(99, 179, 237, 1)',
          borderWidth: 1,
          borderRadius: 4,
          hoverBackgroundColor: 'rgba(99, 179, 237, 0.85)',
        }],
      },
      options: baseBarOptions(),
    });
  }

  // Day-of-week chart
  const dowLabels = pattern.by_day_of_week.map((d) => d.day.slice(0, 3));
  const dowValues = pattern.by_day_of_week.map((d) => d.commit_count);
  const dowCtx    = document.getElementById('dow-chart').getContext('2d');

  if (dowChart) {
    dowChart.data.labels = dowLabels;
    dowChart.data.datasets[0].data = dowValues;
    dowChart.update();
  } else {
    dowChart = new Chart(dowCtx, {
      type: 'bar',
      data: {
        labels: dowLabels,
        datasets: [{
          label: 'Commits',
          data: dowValues,
          backgroundColor: 'rgba(167, 139, 250, 0.65)',
          borderColor: 'rgba(167, 139, 250, 1)',
          borderWidth: 1,
          borderRadius: 4,
          hoverBackgroundColor: 'rgba(167, 139, 250, 0.85)',
        }],
      },
      options: baseBarOptions(),
    });
  }
}

// ── Helpers ─────────────────────────────────────────────────────────────────────

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
    return `Last updated: ${new Date(isoString).toUTCString()}`;
  } catch {
    return `Last updated: ${isoString}`;
  }
}

function showPanelError(el, message) {
  el.textContent = `⚠ ${message}`;
  el.classList.remove('hidden');
}

function clearPanelError(el) {
  el.textContent = '';
  el.classList.add('hidden');
}

// ── Stats rendering ──────────────────────────────────────────────────────────────

function renderSummary(data) {
  const cs = data.current_streak  ?? 0;
  const ls = data.longest_streak  ?? 0;
  const wv = data.weekly_velocity ?? 0;
  const ar = data.active_repos    ?? 0;

  currentStreakEl.textContent  = cs;
  longestStreakEl.textContent  = ls;
  weeklyVelocityEl.textContent = wv;
  activeReposEl.textContent    = ar;

  buildWeeklyChart(wv);

  lastUpdatedEl.textContent = data.computed_at_utc
    ? formatUpdatedAt(data.computed_at_utc)
    : '';

  const allZero = cs === 0 && ls === 0 && wv === 0 && ar === 0;
  zeroDataPrompt.classList.toggle('hidden', !allZero);
}

// ── API helpers ──────────────────────────────────────────────────────────────────

async function apiFetch(path) {
  const resp = await fetch(`${BACKEND_URL}${path}`, { credentials: 'include' });
  if (!resp.ok) throw new Error(`${path} returned HTTP ${resp.status}`);
  return resp.json();
}

// ── Fetch all stats in parallel ──────────────────────────────────────────────────

async function fetchAllStats() {
  const [summaryResult, heatmapResult, reposResult, patternResult] =
    await Promise.allSettled([
      apiFetch('/stats/summary'),
      apiFetch('/stats/heatmap'),
      apiFetch('/stats/repos'),
      apiFetch('/stats/activity-pattern'),
    ]);

  // Summary (existing four cards + weekly chart)
  if (summaryResult.status === 'fulfilled') {
    renderSummary(summaryResult.value);
  } else {
    console.error('[stats/summary]', summaryResult.reason);
    // Don't show a panel error for summary — those cards are already visible
  }

  // Heatmap
  clearPanelError(heatmapError);
  if (heatmapResult.status === 'fulfilled') {
    renderHeatmap(heatmapResult.value.days ?? []);
  } else {
    console.error('[stats/heatmap]', heatmapResult.reason);
    showPanelError(heatmapError, 'Could not load heatmap data.');
  }

  // Per-repo chart
  clearPanelError(reposError);
  if (reposResult.status === 'fulfilled') {
    renderReposChart(reposResult.value.repos ?? []);
  } else {
    console.error('[stats/repos]', reposResult.reason);
    showPanelError(reposError, 'Could not load repo activity data.');
  }

  // Activity pattern
  clearPanelError(patternError);
  if (patternResult.status === 'fulfilled') {
    renderPatternCharts(patternResult.value);
  } else {
    console.error('[stats/activity-pattern]', patternResult.reason);
    showPanelError(patternError, 'Could not load activity pattern data.');
  }
}

// ── Auth ─────────────────────────────────────────────────────────────────────────

async function checkAuth() {
  const resp = await fetch(`${BACKEND_URL}/auth/github/me`, {
    credentials: 'include',
  });

  if (resp.ok) {
    const user = await resp.json();
    usernameDisplay.textContent = `@${user.github_username}`;
    showDashboard();
    await fetchAllStats();
  } else {
    loginBtn.href = `${BACKEND_URL}/auth/github/login`;
    showAuth();
  }
}

// ── Sync ──────────────────────────────────────────────────────────────────────────

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
      } catch { /* ignore */ }
      throw new Error(detail);
    }

    await fetchAllStats();
  } catch (err) {
    showSyncError(err.message ?? 'An unexpected error occurred during sync.');
  } finally {
    setSyncLoading(false);
  }
}

// ── Event listeners ───────────────────────────────────────────────────────────────

syncBtn.addEventListener('click', syncNow);

// ── Bootstrap ─────────────────────────────────────────────────────────────────────

(async () => {
  showLoader();
  try {
    await checkAuth();
  } catch (err) {
    console.error('[bootstrap] Auth check failed:', err);
    loginBtn.href = `${BACKEND_URL}/auth/github/login`;
    showAuth();
  } finally {
    hideLoader();
  }
})();
