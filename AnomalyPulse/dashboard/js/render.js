// Puts a view model (model.js) on the status board. Text goes in with textContent only.

function el(tag, className, ...children) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    node.append(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

const $ = (id) => document.getElementById(id);

function renderBanner(vm) {
  const b = vm.banner;
  $("banner").className = `banner tone-${b.tone}`;
  $("banner-title").textContent = b.title;
  $("banner-facts").replaceChildren(...b.facts.map((f) => el("li", null, f)));
  $("updated").textContent = vm.generated;
  document.title = `${b.title}: AnomalyPulse`;
}

function renderHealth(vm) {
  $("kpis").replaceChildren(...vm.kpis.map((k) => {
    const item = el("div", "kpi", el("dt", null, k.label), el("dd", null, k.value));
    if (k.note) item.title = k.note;
    return item;
  }));
  $("services").replaceChildren(...vm.services.map((s) => el("li", `service tone-${s.tone}`,
    el("p", "service-head", el("span", "lamp", ""), el("span", "service-name", s.name)),
    el("p", "service-state", s.state),
    el("dl", "service-figures",
      el("div", null, el("dt", null, "Requests/min"), el("dd", null, s.requests)),
      el("div", null, el("dt", null, "Errors"), el("dd", null, s.errors)),
      el("div", null, el("dt", null, "p95"), el("dd", null, s.p95))))));
}

function row(label, ...value) {
  return el("div", "detail", el("dt", null, label), el("dd", null, ...value));
}

function renderIncident(vm) {
  const hasIncident = Boolean(vm.incident);
  $("incident-row").hidden = !hasIncident;
  $("check-section").hidden = !hasIncident;
  $("all-clear").hidden = hasIncident;
  if (!hasIncident) {
    $("all-clear").textContent = vm.noData
      ? "Incident details and the AI diagnosis appear here once data is arriving again."
      : "No active incident. Incident details, the AI diagnosis and the model check appear here when one is detected.";
    return;
  }
  const i = vm.incident;
  const meter = el("span", "meter");
  meter.setAttribute("aria-hidden", "true");
  const fill = el("span", "meter-fill");
  fill.style.width = `${Math.round(i.confidenceShare * 100)}%`;
  meter.append(fill);
  const severity = i.severity
    ? el("span", `severity sev-${i.severity.tone}`, i.severity.label)
    : el("span", "severity sev-pending", "Pending the diagnosis");
  $("incident").replaceChildren(
    row("Type", el("strong", "incident-type", i.type)),
    row("Confidence", el("span", "confidence", i.confidence), meter),
    row("Severity", severity),
    row("Affected", i.affected),
    row("Started", i.started),
    row("Detected", i.detected));
}

function renderDiagnosis(vm) {
  if (!vm.incident) return;
  if (vm.diagnosisPending) {
    $("diagnosis").replaceChildren(el("p", "pending",
      "Waiting for Bedrock. The explanation appears here as soon as it arrives."));
    return;
  }
  const d = vm.diagnosis;
  $("diagnosis").replaceChildren(
    el("p", "summary", d.summary),
    el("h3", null, "Root cause"), el("p", "cause", d.rootCause),
    el("h3", null, "Evidence"), el("ul", "plain-list", ...d.evidence.map((e) => el("li", null, e))),
    el("h3", null, "Recommended actions"), el("ol", "plain-list", ...d.actions.map((a) => el("li", null, a))),
    el("p", "caveat", "Where Bedrock orders symptoms, the order is an observed sequence from correlated " +
      "timestamps, not proof that one caused the other."));
}

const MARKS = { match: "✓", partial: "✓", miss: "✗", none: "–" };

function renderCheck(vm) {
  if (!vm.comparison) return;
  const c = vm.comparison;
  const side = (label, type, ...lines) => el("div", "side", el("p", "side-label", label),
    el("p", "side-type", type), ...lines.filter(Boolean).map((l) => el("p", "side-line", l)));
  const model = side("Model predicted", c.prediction.type, c.prediction.confidence, c.prediction.model);
  const truth = c.truth
    ? side("Simulator injected", c.truth.type, c.truth.severity, `Run ${c.truth.experiment}`)
    : side("Simulator injected", "Nothing", "No experiment was running.");
  truth.classList.add("side-truth");

  const mark = el("span", "mark", MARKS[c.verdict.kind]);
  mark.setAttribute("aria-hidden", "true");
  const verdict = el("div", `verdict verdict-${c.verdict.kind}`, mark, el("p", "verdict-label", c.verdict.label));

  const parts = [el("div", "versus", model, verdict, truth), el("p", "verdict-detail", c.verdict.detail)];
  if (c.detection) parts.push(el("p", "verdict-detail", c.detection));
  if (c.truth) {
    parts.push(el("h3", null, "Injected faults"), el("ul", "faults", ...c.truth.faults.map((f) =>
      el("li", `fault fault-${f.tone}`,
        el("span", "fault-dot", ""),
        el("span", "fault-name", f.name),
        el("span", "fault-state", f.label),
        el("span", "fault-detail", `${f.target}, ${f.window}${f.note ? `. ${f.note}` : ""}`)))));
  }
  $("check").replaceChildren(...parts);
}

export function render(vm) {
  renderBanner(vm);
  renderHealth(vm);
  renderIncident(vm);
  renderDiagnosis(vm);
  renderCheck(vm);
}

// When requests fail the board no longer knows the current state, so the banner stops claiming one.
export function renderUnknownStatus() {
  $("banner").className = "banner tone-none";
  $("banner-title").textContent = "Data out of date";
  $("banner-facts").replaceChildren(el("li", null, "The latest request failed; the board below is the last good reply"));
}

// The line under the top bar: a stale reply or a failed request.
export function renderNotice(text, tone = "warn") {
  const box = $("notice");
  box.hidden = !text;
  box.className = `notice notice-${tone}`;
  box.textContent = text ?? "";
}
