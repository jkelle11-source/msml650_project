// DOM-only renderer. Strings from the response are text nodes, never HTML.
import { viewModel } from "./model.js";
import { MISSING, percent } from "./format.js";
const $ = (id) => document.getElementById(id);
function el(tag, className, ...children) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    if (typeof child === "string" || typeof child === "number") node.append(document.createTextNode(String(child)));
    else node.append(child);
  }
  return node;
}
function labelValue(label, value, className = "") {
  return el("div", `detail-cell ${className}`, el("dt", null, label), el("dd", null, value ?? MISSING));
}
// A tone is one meaning, one colour: ok, warn, alarm, none, accent, pending (styles.css).
function badge(text, tone = "none") { return el("span", `badge tone-${tone}`, text); }
function setStatus(text, tone) {
  $("system-label").textContent = text;
  $("system-label").className = `status-pill tone-${tone}`;
}
function renderHeader(vm) {
  $("window").textContent = vm.window;
  $("generated").textContent = vm.generated;
  setStatus(vm.status.banner, vm.status.tone);
  $("page-title").textContent = vm.report.headline;
  $("deck").textContent = vm.report.deck;
  document.title = `${vm.status.label} · AnomalyPulse`;
  document.body.dataset.health = vm.status.raw.toLowerCase().replaceAll("_", "-");
}
function renderKpis(vm) {
  $("figures").replaceChildren(...vm.kpis.map((k) => el("div", "kpi", el("dt", null, k.label), el("dd", null, k.value))));
}
// PROJECT_PLAN §14's service bar: the share of the service's requests that didn't return a 5xx.
// Its colour is the backend's status for the service, so the console invents no threshold.
function meter(share) {
  const track = el("span", share === null ? "meter meter-empty" : "meter");
  track.setAttribute("role", "img");
  if (share === null) { track.setAttribute("aria-label", "No requests in this minute"); return track; }
  const fill = el("span", "meter-fill");
  fill.style.width = `${(share * 100).toFixed(1)}%`;
  track.append(fill);
  track.setAttribute("aria-label", `${percent(share)} of requests succeeded`);
  track.title = `${percent(share)} of requests succeeded`;
  return track;
}
function renderServices(vm) {
  $("services").replaceChildren(...vm.services.map((s) => {
    const th = el("th", "service-name", el("span", "service-indicator"), s.name);
    th.scope = "row";
    const row = el("tr", `tone-${s.tone}`, th,
      el("td", "state-col", badge(s.state, s.tone)),
      el("td", "meter-col", meter(s.okShare)),
      el("td", "numeric", s.errors),
      el("td", "numeric", s.requests),
      el("td", "numeric", s.p95));
    // Attribute labels make each metric intelligible in the stacked phone layout.
    [...row.querySelectorAll("td")].forEach((td, index) => td.dataset.label = ["Status", "Requests OK", "Error rate", "Requests / min", "P95 latency"][index]);
    return row;
  }));
}
function renderIncident(vm) {
  const host = $("incident-panel");
  if (!vm.incident) {
    host.replaceChildren(el("div", `empty-state ${vm.noData ? "empty-unknown" : ""}`,
      el("span", "empty-orbit", vm.noData ? "?" : "✓"),
      el("h3", null, vm.noData ? "Incident status unavailable" : "No active incident"),
      el("p", null, vm.noData
        ? "The telemetry pipeline has no recent data. This is not confirmation of system health."
        : "The backend reports no active incident in this minute. Detection details appear here when one is reported.")));
    return;
  }
  const i = vm.incident;
  const progress = el("progress", "confidence-meter");
  progress.max = 100; progress.value = i.confidenceFraction * 100;
  progress.setAttribute("aria-label", `Model confidence ${i.confidence}`);
  host.replaceChildren(
    el("div", "incident-top",
      el("div", null, el("p", "incident-flag", "Active incident"), el("h3", "incident-name", i.type)),
      el("div", "incident-severity", el("span", "label", "AI severity"),
        i.severity ? badge(i.severity.label, i.severity.tone) : badge("Severity pending", "pending"))),
    el("dl", "incident-meta",
      labelValue("Started", i.started), labelValue("ML detected", i.detected),
      labelValue("Affected services", i.affected), labelValue("Model", i.model),
      labelValue("Incident ID", i.id, "is-mono"), labelValue("Model job", i.modelJob, "is-mono")),
    el("div", "confidence-wrap", el("div", "confidence-line", el("span", null, "Classification confidence"),
      el("strong", null, i.confidence)), progress));
}
function renderDiagnosis(vm) {
  const panel = $("diagnosis-body");
  $("diagnosis-section").hidden = !vm.incident;
  if (!vm.incident) { panel.replaceChildren(); return; }
  if (vm.diagnosisPending) {
    panel.replaceChildren(
      el("div", "diagnosis-head", el("h3", null, "Diagnosis pending"), badge("Severity pending", "pending")),
      el("p", "pending-text", "The incident is classified; no diagnosis is ready to show yet. Nothing is filled in from the injected ground truth."));
    return;
  }
  const d = vm.diagnosis;
  const evidence = el("ul", "evidence-list", ...d.evidence.map((text) => el("li", null, text)));
  const actions = el("ol", "action-list", ...d.actions.map((text) => el("li", null, text)));
  panel.replaceChildren(
    el("div", "diagnosis-head", el("h3", null, "AI assessment"),
      el("div", "severity-row", el("span", "label", "Severity"), badge(d.severity, d.severityTone))),
    el("div", "diagnosis-summary", el("p", "label", "Summary"), el("p", "summary-text", d.summary)),
    el("div", "root-cause", el("p", "label", "Likely root cause"), el("p", "root-text", d.rootCause)),
    el("div", "analysis-columns", el("div", "analysis-column", el("h4", null, "Supporting evidence"), evidence),
      el("div", "analysis-column", el("h4", null, "Prioritized actions"), actions)),
    el("p", "sequence-note", "Symptom ordering in the diagnosis is an observed sequence from correlated timestamps; it is not proof of causation."));
}
// The ground-truth reveal (PROJECT_PLAN §15 part 3) leads the page: the verdict first, the detail below.
function verdictBanner(c) {
  const v = c.verdict;
  const icon = el("span", "verdict-icon", v.icon);
  icon.setAttribute("aria-hidden", "true");
  return el("section", `verdict tone-${v.tone}`, icon,
    el("div", "verdict-body",
      el("p", "verdict-label", v.label),
      c.truth && el("p", "verdict-classes",
        el("span", "label", "Model"), " ", el("strong", null, c.prediction.type),
        el("span", "verdict-sep", "·"),
        el("span", "label", "Injected"), " ", el("strong", null, c.truth.type)),
      el("p", "verdict-detail", v.detail),
      c.detection && el("p", "verdict-latency", c.detection)));
}
function modelCard(c) {
  const p = c.prediction;
  return el("article", "compare-card compare-model", el("p", "card-label", "Model prediction"),
    el("h3", "compare-class", p.type),
    el("dl", "compare-fields", labelValue("Confidence", p.confidence), labelValue("Detected", p.detected),
      labelValue("Affected services", p.affected), labelValue("Model", p.model)));
}
function truthCard(c) {
  if (!c.truth) return el("article", "compare-card compare-truth", el("p", "card-label", "Injected ground truth"),
    el("h3", "compare-class", "Nothing injected"),
    el("p", "muted card-note", "No injected-fault record is available for this window. That is expected for a real incident, and is not an error."));
  const t = c.truth;
  const faults = el("ul", "fault-list", ...t.faults.map((f) => el("li", `fault-row ${f.active === true ? "fault-active" : ""}`,
    el("div", "fault-title", el("strong", null, f.name), badge(f.state, f.active === true ? "alarm" : "none")),
    el("p", "fault-description", `Target: ${f.target}`),
    el("p", "fault-description", `${f.from} → ${f.until}`),
    f.note && el("p", "fault-description", f.note))));
  return el("article", "compare-card compare-truth", el("p", "card-label", "Injected ground truth"),
    el("h3", "compare-class", t.type),
    el("dl", "compare-fields", labelValue("Severity", t.severity), labelValue("Experiment", t.experiment, "is-mono")),
    el("h4", "fault-heading", `Injected faults (${t.faults.length})`), faults);
}
function renderComparison(vm) {
  $("comparison-section").hidden = !vm.comparison;
  if (!vm.comparison) { $("comparison").replaceChildren(); return; }
  const c = vm.comparison;
  $("comparison").replaceChildren(verdictBanner(c), el("div", "compare-grid", modelCard(c), truthCard(c)));
}
function renderTimeline(vm) {
  $("timeline-section").hidden = !vm.incident;
  if (!vm.incident) { $("timeline-list").replaceChildren(); return; }
  $("timeline-list").replaceChildren(...vm.timeline.map((event) => el("li", `timeline-item timeline-${event.kind} ${event.active ? "timeline-active" : ""} ${event.planned ? "timeline-planned" : ""}`,
    el("span", "timeline-dot"),
    el("time", "timeline-time", event.time),
    el("div", "timeline-main", el("strong", null, event.label),
      event.active && badge("Active in window", "alarm"),
      event.detail && el("p", "muted", event.detail)))));
}
export function render(replyOrVm) {
  const vm = Object.hasOwn(replyOrVm, "contract_version") ? viewModel(replyOrVm) : replyOrVm;
  renderHeader(vm);
  renderKpis(vm);
  renderServices(vm);
  renderIncident(vm);
  renderDiagnosis(vm);
  renderComparison(vm);
  renderTimeline(vm);
}
// No reply has ever arrived: the console knows nothing, so it claims no state.
export function renderNoReply() {
  setStatus("No reply", "none");
  $("deck").textContent = "The read API hasn't answered, so system health is unknown.";
}
export function renderConnection({ source, kind }) {
  $("source-label").textContent = source;
  const labels = { connected: "Connected", connecting: "Connecting", refreshing: "Refreshing", stale: "Stale · last good data", offline: "Offline · no data" };
  $("connection-state").textContent = labels[kind] ?? kind;
  $("connection-state").className = `connection-chip connection-${kind}`;
  document.body.dataset.state = kind === "stale" ? "stale" : kind === "offline" ? "offline" : kind === "connected" ? "ready" : kind;
}
export function renderNotice(text, tone = "warn") {
  const notice = $("notice");
  notice.hidden = !text;
  notice.className = `notice notice-${tone}`;
  notice.textContent = text ?? "";
}
