// Existing M7-1 console controller: load, manual refresh, poll, visibility cleanup,
// scenario selection and stale-on-error. No infrastructure-changing controls.
import { CONFIG, SCENARIOS } from "./config.js";
import { dashboardUrl, fetchReply, FeedError, lastSnapshotText } from "./feed.js";
import { render, renderConnection, renderNoReply, renderNotice } from "./render.js";
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
  if (config.source === "mock") return "Local mock";
  if (config.apiBaseUrl !== CONFIG.apiBaseUrl) {   // ?api=: it could be mock or live, so name it
    try { return `API · ${new URL(config.apiBaseUrl).host}`; } catch { return "Other API"; }
  }
  return demoEnabled ? "Mock API" : "Live API";
}
function clearPolling() { clearTimeout(state.timer); state.timer = null; }
function stopInflight() { state.inflight?.abort(); state.inflight = null; }
function isActive() { return !state.stopped && !state.paused && !document.hidden; }
function schedule() {
  clearPolling();
  if (isActive()) state.timer = setTimeout(() => refresh(), config.pollMs);
}
// manual: the presenter asked (Refresh, a new scenario, Resume). Only then, or before the first
// reply, does the console show that it is fetching; a background poll changes nothing on screen
// until its reply arrives, so the console doesn't flicker every 15 s on a projector.
async function refresh({ manual = false } = {}) {
  if (state.stopped || document.hidden) return;
  clearPolling();
  stopInflight();
  const controller = new AbortController();
  state.inflight = controller;
  if (manual || !state.last) {
    $("refresh").disabled = true;
    renderConnection({ source: sourceLabel(), kind: state.last ? "refreshing" : "connecting" });
  }
  try {
    const reply = await fetchReply(dashboardUrl(config, state.scenario), { signal: controller.signal, timeoutMs: config.timeoutMs });
    if (state.inflight !== controller || state.stopped) return;
    state.last = reply;
    state.lastScenario = state.scenario;
    render(reply);
    updateTabs(reply);
    renderConnection({ source: sourceLabel(), kind: "connected" });
    renderNotice(null);
  } catch (error) {
    if (controller.signal.aborted || state.stopped) return;
    const reason = error instanceof FeedError ? error.message : "unexpected request failure";
    renderConnection({ source: sourceLabel(), kind: state.last ? "stale" : "offline" });
    if (!state.last) renderNoReply();
    const showing = lastSnapshotText(Boolean(state.last), state.lastScenario, demoEnabled);
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
      refresh({ manual: true });
    });
  }
  $("refresh").addEventListener("click", () => refresh({ manual: true }));
  $("pause").addEventListener("click", () => {
    state.paused = !state.paused;
    $("pause").textContent = state.paused ? "Resume" : "Pause";
    $("pause").title = state.paused ? "Resume auto-refresh" : "Pause auto-refresh";
    $("pause").setAttribute("aria-pressed", String(state.paused));
    if (state.paused) clearPolling(); else refresh({ manual: true });
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { clearPolling(); stopInflight(); $("refresh").disabled = false; }
    else if (isActive()) refresh();
  });
  window.addEventListener("pagehide", () => { state.stopped = true; clearPolling(); stopInflight(); });
  window.addEventListener("pageshow", (event) => { if (event.persisted) { state.stopped = false; if (isActive()) refresh(); } });
  window.addEventListener("beforeunload", () => { clearPolling(); stopInflight(); });
  initNav();
  $("pause").title = "Pause auto-refresh";
  renderConnection({ source: sourceLabel(), kind: "connecting" });
  refresh();
}
initialize();
