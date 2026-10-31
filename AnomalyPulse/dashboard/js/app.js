// The console: polls GET /dashboard, renders each reply, and keeps the last good
// reply on screen (marked stale) when a request fails.
import { CONFIG, SCENARIOS } from "./config.js";
import { FeedError, dashboardUrl, fetchReply } from "./feed.js";
import { clock } from "./format.js";
import { viewModel } from "./model.js";
import { render, renderNotice, renderUnknownStatus } from "./render.js";

const params = new URLSearchParams(location.search);
const config = { ...CONFIG };
if (params.get("api")) Object.assign(config, { source: "api", apiBaseUrl: params.get("api") });

const state = {
  scenario: SCENARIOS.some((s) => s.id === params.get("scenario")) ? params.get("scenario") : SCENARIOS[0].id,
  paused: false,
  last: null,          // the last reply that rendered, for the stale notice
  timer: null,
  inflight: null,
};

const $ = (id) => document.getElementById(id);

function setupControls() {
  const demo = $("demo");
  demo.hidden = !config.scenarios;
  $("source").textContent = config.source === "mock"
    ? "Mock data from this folder, not live telemetry."
    : "Mock data from the read API, not live telemetry.";
  const select = $("scenario");
  select.replaceChildren(...SCENARIOS.map((s) => new Option(s.label, s.id, false, s.id === state.scenario)));
  select.addEventListener("change", () => {
    state.scenario = select.value;
    const url = new URL(location.href);
    url.searchParams.set("scenario", state.scenario);
    history.replaceState(null, "", url);
    refresh();
  });
  $("refresh").addEventListener("click", () => refresh());
  $("pause").addEventListener("click", () => {
    state.paused = !state.paused;
    $("pause").textContent = state.paused ? "Resume updates" : "Pause updates";
    $("pause").setAttribute("aria-pressed", String(state.paused));
    if (state.paused) clearTimeout(state.timer); else refresh();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !state.paused) refresh();
  });
}

function schedule() {
  clearTimeout(state.timer);
  if (!state.paused) state.timer = setTimeout(refresh, config.pollMs);
}

async function refresh() {
  state.inflight?.abort();
  const controller = new AbortController();
  state.inflight = controller;
  try {
    const reply = await fetchReply(dashboardUrl(config, state.scenario),
      { timeoutMs: config.timeoutMs, signal: controller.signal });
    const vm = viewModel(reply);
    render(vm);
    state.last = reply;
    document.body.dataset.state = "live";
    renderNotice(null);                            // a stopped pipeline is the headline itself
  } catch (error) {
    if (controller.signal.aborted) return;         // a newer request replaced this one
    const reason = error instanceof FeedError ? error.message : "an unexpected error";
    const every = `Trying again every ${config.pollMs / 1000} s.`;
    renderUnknownStatus();
    if (state.last) {
      document.body.dataset.state = "stale";
      renderNotice(`Showing the reply from ${clock(state.last.generated_at, { seconds: true })}. ` +
        `The latest request failed (${reason}). ${every}`, "alarm");
    } else {
      document.body.dataset.state = "empty";
      renderNotice(`The console couldn't load its data (${reason}). ${every}`, "alarm");
    }
  } finally {
    if (state.inflight === controller) {
      state.inflight = null;
      schedule();
    }
  }
}

setupControls();
refresh();
