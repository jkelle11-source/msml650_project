// Puts a view model (model.js) on the page as a report. Text goes in with textContent only.

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

function renderLede(vm) {
  const r = vm.report;
  $("state").textContent = r.label;
  $("state").className = `state state-${r.tone}`;
  $("dateline").textContent = r.dateline;
  $("headline").textContent = r.headline;
  $("deck").textContent = r.deck;
  $("updated").textContent = vm.generated;
  document.title = `${r.label}: AnomalyPulse`;
}

function renderSystem(vm) {
  $("figures").replaceChildren(...vm.kpis.map((k) => {
    const item = el("div", "figure", el("dt", null, k.label), el("dd", null, k.value));
    if (k.note) item.title = k.note;
    return item;
  }));
  $("services").replaceChildren(...vm.services.map((s) => {
    // On a phone the state column folds under the name (styles.css).
    const name = el("th", null, s.name, el("span", `state-inline state-${s.tone}`, s.state));
    name.scope = "row";
    return el("tr", null, name,
      el("td", `service-state state-${s.tone}`, s.state),
      el("td", "num", s.requests),
      el("td", "num", s.errors),
      el("td", "num", s.p95));
  }));
}

function renderDiagnosis(vm) {
  const section = $("diagnosis-section");
  section.hidden = !vm.incident;
  if (!vm.incident) return;
  if (vm.diagnosisPending) {
    $("diagnosis").replaceChildren(el("p", "pending",
      "Bedrock's explanation hasn't arrived yet. It appears here as soon as it does."));
    return;
  }
  const d = vm.diagnosis;
  $("diagnosis").replaceChildren(
    el("p", "summary", d.summary),
    el("p", "cause", el("span", "run-in", "Most likely cause. "), d.rootCause),
    el("div", "lists",
      el("div", null, el("h3", null, "Evidence"), el("ul", null, ...d.evidence.map((e) => el("li", null, e)))),
      el("div", null, el("h3", null, "What to try"), el("ol", null, ...d.actions.map((a) => el("li", null, a))))),
    el("p", "caveat", "Bedrock wrote this from the incident's metrics. Where it orders symptoms, the order is an " +
      "observed sequence from correlated timestamps, not proof that one caused the other."));
}

const MARKS = { match: "✓", partial: "✓", miss: "✗", none: "–" };

function renderComparison(vm) {
  const section = $("comparison-section");
  section.hidden = !vm.comparison;
  if (!vm.comparison) return;
  const c = vm.comparison;

  const model = el("div", "column column-model",
    el("h3", null, "The model said"),
    el("p", "type", c.prediction.type),
    el("p", "detail", c.prediction.confidence),
    c.prediction.model && el("p", "detail", c.prediction.model),
    c.detection && el("p", "detail", c.detection));

  const truth = c.truth
    ? el("div", "column column-truth",
      el("h3", null, "The simulator injected"),
      el("p", "type", c.truth.type),
      el("p", "detail", c.truth.severity),
      el("p", "detail", "Run ", el("span", "run-id", c.truth.experiment)),
      el("ul", "faults", ...c.truth.faults.map((f) => el("li", `fault fault-${f.tone}`,
        el("span", "fault-name", f.name), " ", el("span", "fault-state", f.label.toLowerCase()),
        el("span", "fault-detail", `${f.target}, ${f.window}.`),
        f.note && el("span", "fault-detail", f.note)))))
    : el("div", "column column-truth",
      el("h3", null, "The simulator injected"),
      el("p", "type type-none", "Nothing"),
      el("p", "detail", "No experiment was running at this time."));

  const mark = el("span", "mark", MARKS[c.verdict.kind]);
  mark.setAttribute("aria-hidden", "true");
  $("comparison").replaceChildren(
    el("div", "ledger", model, truth),
    el("p", `verdict verdict-${c.verdict.kind}`, mark, el("strong", null, `${c.verdict.label}. `), c.verdict.detail));
}

export function render(vm) {
  renderLede(vm);
  renderSystem(vm);
  renderDiagnosis(vm);
  renderComparison(vm);
}

// When requests fail the console no longer knows the current state, so it stops claiming one.
export function renderUnknownStatus() {
  $("state").textContent = "Out of date";
  $("state").className = "state state-none";
}

// The notice under the masthead: a stopped pipeline, a stale reply, or a failed request.
export function renderNotice(text, tone = "warn") {
  const box = $("notice");
  box.hidden = !text;
  box.className = `notice notice-${tone}`;
  box.textContent = text ?? "";
}
