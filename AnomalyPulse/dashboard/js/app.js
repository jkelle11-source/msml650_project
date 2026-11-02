// Existing M7-1 console controller: load, manual refresh, poll, visibility cleanup,
// scenario selection and stale-on-error. No infrastructure-changing controls.
import { CONFIG, SCENARIOS } from "./config.js";
import { dashboardUrl, fetchReply, FeedError } from "./feed.js";
import { render, renderConnection, renderNotice } from "./render.js";
import { initNav, updateTabs } from "./nav.js";

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const config = { ...CONFIG };
if (params.has("api") && params.get("api")) Object.assign(config, { source: "api", apiBaseUrl: params.get("api") });
if (params.get("source") === "mock") config.source = "mock";   // offline fallback for the demo
const demoEnabled = config.source === "mock" || (config.source === "api" && config.scenarios === true);
const chosen = params.get("scenario");
const state = {
  scenario: SCENARIOS.some((x) => x.id === chosen) ? chosen : "healthy",
  timer: null, inflight: null, last: null, lastScenario: null,
  paused: false, stopped: false,
};

function sourceLabel() {
  if (config.source === "mock") return "LOCAL MOCK";
  return demoEnabled ? "MOCK API" : "LIVE API";
}
function clearPolling() { clearTimeout(state.timer); state.timer = null; }
function stopInflight() { state.inflight?.abort(); state.inflight = null; }
function isActive() { return !state.stopped && !state.paused && !document.hidden; }
function schedule() {
  clearPolling();
  if (isActive()) state.timer = setTimeout(() => refresh(), config.pollMs);
}
async function refresh() {
  if (state.stopped || document.hidden) return;
  clearPolling();
  stopInflight();
  const controller = new AbortController();
  state.inflight = controller;
  $("refresh").disabled = true;
  renderConnection({ source: sourceLabel(), kind: state.last ? "refreshing" : "connecting", lastSuccess: state.lastSuccess });
  try {
    const reply = await fetchReply(dashboardUrl(config, state.scenario), { signal: controller.signal, timeoutMs: config.timeoutMs });
    if (state.inflight !== controller || state.stopped) return;
    state.last = reply;
    state.lastScenario = state.scenario;
    state.lastSuccess = new Date().toISOString();
    render(reply);
    updateTabs(reply);
    renderConnection({ source: sourceLabel(), kind: "connected", lastSuccess: state.lastSuccess });
    renderNotice(null);
  } catch (error) {
    if (controller.signal.aborted || state.stopped) return;
    const reason = error instanceof FeedError ? error.message : "unexpected request failure";
    renderConnection({ source: sourceLabel(), kind: state.last ? "stale" : "offline", lastSuccess: state.lastSuccess });
    const showing = state.last ? `The last successful ${state.lastScenario} snapshot remains visible.` : "No successful snapshot is available.";
    renderNotice(`${showing} Refresh failed (${reason}). The console will retry when visible.`, "error");
  } finally {
    if (state.inflight === controller) {
      state.inflight = null;
      $("refresh").disabled = false;
      schedule();
    }
  }
}

function initialize() {
  document.body.dataset.source = config.source === "mock" ? "local-mock" : demoEnabled ? "mock-api" : "live-api";
  const demo = $("demo");
  demo.hidden = !demoEnabled;
  if (demoEnabled) {
    const select = $("scenario");
    select.replaceChildren(...SCENARIOS.map((s) => new Option(s.label, s.id, false, s.id === state.scenario)));
    select.addEventListener("change", () => {
      state.scenario = select.value;
      const url = new URL(location.href);
      url.searchParams.set("scenario", state.scenario);
      history.replaceState(null, "", url);
      refresh();
    });
  }
  $("refresh").addEventListener("click", () => refresh());
  $("pause").addEventListener("click", () => {
    state.paused = !state.paused;
    $("pause").textContent = state.paused ? "Resume auto-refresh" : "Pause auto-refresh";
    $("pause").setAttribute("aria-pressed", String(state.paused));
    if (state.paused) clearPolling(); else refresh();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { clearPolling(); stopInflight(); $("refresh").disabled = false; }
    else if (isActive()) refresh();
  });
  window.addEventListener("pagehide", () => { state.stopped = true; clearPolling(); stopInflight(); });
  window.addEventListener("pageshow", (event) => { if (event.persisted) { state.stopped = false; if (isActive()) refresh(); } });
  window.addEventListener("beforeunload", () => { clearPolling(); stopInflight(); });
  initNav();
  renderConnection({ source: sourceLabel(), kind: "connecting", lastSuccess: null });
  refresh();
}
initialize();
