// DOM-only renderer. Strings from the response are text nodes, never HTML.
import { viewModel } from "./model.js";
import { dateTime, MISSING } from "./format.js";
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
function badge(text, tone = "neutral") { return el("span", `badge badge-${tone}`, text); }
function sectionTitle(title, subtitle) {
  return el("div", "panel-heading", el("h3", null, title), subtitle && el("span", "mono tiny muted", subtitle));
}
function renderHeader(vm) {
  $("window").textContent = vm.window;
  $("generated").textContent = vm.generated;
  $("system-state").textContent = `BACKEND STATUS / ${vm.status.label.toUpperCase()}`;
  $("system-label").textContent = vm.status.label.toUpperCase();
  $("system-label").className = `status-indicator status-${vm.status.tone}`;
  $("page-title").textContent = vm.report.headline;
  $("deck").textContent = vm.report.deck;
  document.title = `${vm.status.label} · AnomalyPulse`;
  document.body.dataset.health = vm.status.raw.toLowerCase().replaceAll("_", "-");
}
function renderKpis(vm) {
  $("figures").replaceChildren(...vm.kpis.map((k) => {
    const block = el("div", `kpi ${k.tone ? `kpi-${k.tone}` : ""}`,
      el("dt", null, k.label), el("dd", "numeric", k.value));
    return block;
  }));
}
function renderServices(vm) {
  $("services").replaceChildren(...vm.services.map((s) => {
    const th = el("th", "service-name", el("span", "service-name-inner", el("span", "service-indicator"), s.name));
    th.scope = "row";
    if (s.affected) th.append(el("span", "affected-tag", "Affected"));
    const row = el("tr", s.affected ? "is-affected" : "", th,
      el("td", "state-col", badge(s.state, s.tone)),
      el("td", "numeric", s.requests),
      el("td", "numeric", s.errors),
      el("td", "numeric", s.p95));
    // Attribute labels make each metric intelligible in stacked mobile layout.
    [...row.querySelectorAll("td")].forEach((td, index) => td.dataset.label = ["Status", "Requests / min", "Error rate", "P95 latency"][index]);
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
        : "The backend reports no active incident for this window. Detection details will appear here when an incident is reported.")));
    return;
  }
  const i = vm.incident;
  const progress = el("progress", "confidence-meter");
  progress.max = 100; progress.value = i.confidenceFraction * 100;
  progress.setAttribute("aria-label", `Model confidence ${i.confidence}`);
  host.replaceChildren(
    el("div", "incident-top", el("div", null,
      el("span", "mini-tag incident-flag", "ACTIVE INCIDENT"),
      el("h3", "incident-name", i.type),
      el("p", "mono incident-code", i.classCode)),
      el("div", "incident-severity", el("span", "muted mini-tag", "AI SEVERITY"),
        i.severity ? badge(i.severity.label, i.severity.tone) : badge("Severity pending", "warn"))),
    el("div", "incident-meta", labelValue("Incident ID", i.id, "mono"),
      labelValue("Started at", i.started), labelValue("ML detected at", i.detected),
      labelValue("Affected services", i.affected), labelValue("Estimator", i.estimator),
      labelValue("Model job", i.modelJob, "mono")),
    el("div", "confidence-wrap", el("div", "confidence-line", el("span", null, "Classification confidence"),
      el("strong", "numeric", i.confidence)), progress));
}
function renderDiagnosis(vm) {
  const panel = $("diagnosis-body");
  $("diagnosis-section").hidden = !vm.incident;
  if (!vm.incident) { panel.replaceChildren(); return; }
  if (vm.diagnosisPending) {
    panel.replaceChildren(
      sectionTitle("AI diagnosis pending.", "BEDROCK / WAITING"),
      el("p", "pending-label", "Severity pending"),
      el("p", "muted", "The incident is classified, but the diagnosis has not yet been returned. Nothing is filled from injected ground truth."));
    return;
  }
  const d = vm.diagnosis;
  const evidence = el("ul", "evidence-list", ...d.evidence.map((text) => el("li", null, text)));
  const actions = el("ol", "action-list", ...d.actions.map((text) => el("li", null, text)));
  panel.replaceChildren(
    el("div", "diagnosis-head", sectionTitle("AI assessment", "BEDROCK / RETURNED"),
      el("div", "severity-row", el("span", "muted small", "Severity"), badge(d.severity, d.rawSeverity.toLowerCase()))),
    el("div", "diagnosis-summary", el("p", "mini-tag", "SUMMARY"), el("p", null, d.summary)),
    el("div", "root-cause", el("p", "mini-tag", "LIKELY ROOT CAUSE"), el("p", "root-text", d.rootCause)),
    el("div", "analysis-columns", el("div", "analysis-column", el("h4", null, "Supporting evidence"), evidence),
      el("div", "analysis-column", el("h4", null, "Prioritized actions"), actions)),
    el("p", "sequence-note", "Symptom ordering in the diagnosis is an observed sequence from correlated timestamps; it is not proof of causation."));
}
function modelCard(c) {
  const p = c.prediction;
  return el("article", "compare-card compare-model", el("div", "compare-top", el("span", "mini-tag", "MODEL PREDICTION"), badge("ML", "cyan")),
    el("h3", "compare-class", p.type), el("p", "mono compare-code", p.classCode),
    el("dl", "compare-fields", labelValue("Confidence", p.confidence),
      labelValue("Affected services", p.affected), labelValue("Detection time", p.detected),
      labelValue("Estimator", p.estimator, "mono"), labelValue("Model job", p.modelJob, "mono")));
}
function truthCard(c) {
  if (!c.truth) return el("article", "compare-card compare-truth", el("span", "mini-tag", "INJECTED GROUND TRUTH"),
    el("h3", "compare-class", "No injected-fault record"),
    el("p", "muted", "No injected-fault record is available for this window. This is expected for real-world incidents and is not an error."));
  const t = c.truth;
  const faults = el("ul", "fault-list", ...t.faults.map((f) => el("li", `fault-row ${f.active === true ? "fault-active" : ""}`,
    el("div", "fault-title", el("strong", null, f.name), badge(f.state, f.active === true ? "alarm" : f.active === null ? "neutral" : "idle")),
    el("p", "fault-description", `Injector: ${f.injector} · Target: ${f.target}`),
    el("p", "fault-description", `Activation interval: ${f.from} → ${f.until}`),
    f.note && el("p", "fault-description", f.note))));
  return el("article", "compare-card compare-truth", el("span", "mini-tag", "INJECTED GROUND TRUTH"),
    el("h3", "compare-class", t.type), el("p", "mono compare-code", t.classCode),
    el("dl", "compare-fields", labelValue("Ground-truth severity", t.severity),
      labelValue("Experiment ID", t.experiment, "mono")),
    el("h4", "fault-heading", `Injected faults (${t.faults.length})`), faults);
}
function renderComparison(vm) {
  $("comparison-section").hidden = !vm.comparison;
  if (!vm.comparison) { $("comparison").replaceChildren(); return; }
  const c = vm.comparison;
  $("comparison").replaceChildren(el("div", "compare-grid", modelCard(c), truthCard(c)),
    el("div", `comparison-verdict verdict-${c.verdict.kind}`,
      el("span", "mini-tag", "COMPARISON"),
      el("strong", null, c.verdict.label),
      el("p", null, c.verdict.detail),
      c.detection && el("p", "detection-latency", c.detection)));
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
  $("system-state").textContent = "BACKEND STATUS / UNKNOWN";
  $("system-label").textContent = "NO REPLY";
  $("system-label").className = "status-indicator status-none";
  $("deck").textContent = "The read API hasn't answered, so system health is unknown.";
}
export function renderConnection({ source, kind, lastSuccess }) {
  $("source-label").textContent = source;
  const labels = { connected: "Connected", connecting: "Connecting", refreshing: "Refreshing", stale: "STALE · last good data", offline: "Offline · no data" };
  $("connection-state").textContent = labels[kind] ?? kind;
  $("connection-state").className = `connection-chip connection-${kind}`;
  $("last-success").textContent = lastSuccess ? dateTime(lastSuccess) : MISSING;
  document.body.dataset.state = kind === "stale" ? "stale" : kind === "offline" ? "offline" : kind === "connected" ? "ready" : kind;
}
export function renderNotice(text, tone = "warn") {
  const notice = $("notice");
  notice.hidden = !text;
  notice.className = `notice notice-${tone}`;
  notice.textContent = text ?? "";
}
