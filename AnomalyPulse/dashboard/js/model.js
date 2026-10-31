// Turns one GET /dashboard reply (Docs/DASHBOARD_CONTRACT.md, version 1) into the
// text the console shows. Pure: no DOM, no fetch. Every string here is placed with
// textContent by render.js, never parsed as HTML, so Bedrock's text can't inject markup.
import {
  INJECTOR_TYPES, MISSING, clock, count, incidentName, injectorName, latency, list, percent,
  serviceName, severityName,
} from "./format.js";

export const CONTRACT_VERSION = 1;
const TOP_LEVEL = ["contract_version", "generated_at", "health", "incident"];
const HEALTH = ["window_start", "status", "requests_per_min", "error_rate", "p95_latency_ms",
  "active_incidents", "services"];
const INCIDENT = ["incident_id", "prediction", "diagnosis", "ground_truth", "comparison"];

export class ContractError extends Error {}

function requireKeys(object, keys, where) {
  if (object === null || typeof object !== "object" || Array.isArray(object)) {
    throw new ContractError(`${where} is not an object`);
  }
  const missing = keys.filter((key) => !Object.hasOwn(object, key));
  if (missing.length) throw new ContractError(`${where} is missing ${missing.join(", ")}`);
}

// The checks the console relies on. Unknown extra keys are fine: adding a field
// doesn't change contract_version.
export function checkReply(reply) {
  requireKeys(reply, TOP_LEVEL, "the reply");
  if (reply.contract_version !== CONTRACT_VERSION) {
    throw new ContractError(`this console reads contract version ${CONTRACT_VERSION}; ` +
      `the API sent version ${reply.contract_version}`);
  }
  requireKeys(reply.health, HEALTH, "health");
  if (!Array.isArray(reply.health.services)) throw new ContractError("health.services is not a list");
  if (reply.incident !== null) requireKeys(reply.incident, INCIDENT, "incident");
  return reply;
}

const TONES = { HEALTHY: "ok", DEGRADED: "warn", IDLE: "idle", NO_DATA: "none", INCIDENT: "alarm" };
const SERVICE_STATES = { HEALTHY: "Healthy", DEGRADED: "Degraded", IDLE: "Idle", NO_DATA: "No data" };

function systemStatus(health) {
  if (health.status === "INCIDENT") return { tone: "alarm", label: "Incident" };
  if (health.status === "HEALTHY") return { tone: "ok", label: "Healthy" };
  return { tone: "none", label: "No data" };
}

// The key figures line: the top-line numbers, each a value and what it counts.
function kpis(health) {
  const incidents = health.active_incidents;
  return [
    { label: "requests a minute", value: count(health.requests_per_min),
      note: "To Product, Cart and Order. Payment is reached through Order, so it isn't counted twice." },
    { label: "of requests failed", value: percent(health.error_rate), note: "Share that returned a 5xx." },
    { label: "p95 latency", value: latency(health.p95_latency_ms), note: "End to end, over all of those requests." },
    { label: incidents === 1 ? "active incident" : "active incidents", value: count(incidents), note: null },
  ];
}

// How the headline sentence starts, per incident type.
const HEADLINE_VERBS = {
  TRAFFIC_SPIKE: "A traffic spike is hitting",
  DATABASE_THROTTLING: "Database throttling is slowing",
  LAMBDA_DEGRADATION: "A Lambda slowdown is affecting",
  DEPENDENCY_FAILURE: "A dependency failure is affecting",
  CASCADING_FAILURE: "A cascading failure is spreading through",
};

// The report's opening: a dateline, one headline sentence and a short deck under it.
export function report(health, incident) {
  const status = systemStatus(health);
  const dateline = health.window_start ? `The minute from ${clock(health.window_start)}` : "No recent minute of data";
  if (health.status === "NO_DATA") {
    return { ...status, dateline,
      headline: "No telemetry has arrived for more than three minutes.",
      deck: "The console can't tell whether the system is healthy until data starts arriving again." };
  }
  if (!incident) {
    const allHealthy = health.services.every((s) => s.status === "HEALTHY");
    const errors = health.error_rate === 0 ? "no errors" : `${percent(health.error_rate)} of them failing`;
    return { ...status, dateline,
      headline: allHealthy ? "All four services are healthy." : "No incident is active.",
      deck: `They handled ${count(health.requests_per_min)} requests in the minute, with ${errors} ` +
        `and a p95 latency of ${latency(health.p95_latency_ms)}.` };
  }
  const p = incident.prediction;
  const where = p.affected_services.length ? list(p.affected_services.map(serviceName)) : "the system";
  const opening = HEADLINE_VERBS[p.incident_type] ?? `${incidentName(p.incident_type)} is affecting`;
  const severity = incident.diagnosis?.severity
    ? `Bedrock rates it ${severityName(incident.diagnosis.severity).toLowerCase()} severity.`
    : "Bedrock's explanation hasn't arrived yet.";
  return { ...status, dateline,
    headline: `${opening} ${where}.`,
    deck: `The model is ${percent(p.confidence)} confident. It started at ${clock(p.started_at)} ` +
      `and was detected at ${clock(p.detected_at, { seconds: true })}. ${severity}` };
}

function services(health) {
  return health.services.map((row) => ({
    name: serviceName(row.service),
    tone: TONES[row.status] ?? "none",
    state: SERVICE_STATES[row.status] ?? row.status,
    requests: count(row.requests_per_min),
    errors: percent(row.error_rate),
    p95: latency(row.p95_latency_ms),
    // The bar: share of requests that succeeded. Null when there's nothing to measure.
    success: typeof row.error_rate === "number" ? Math.max(0, Math.min(1, 1 - row.error_rate)) : null,
  }));
}

function incidentPanel(incident) {
  const p = incident.prediction;
  const severity = incident.diagnosis?.severity;
  return {
    type: incidentName(p.incident_type),
    // Severity is Bedrock's judgment. Until the diagnosis arrives it is pending; the
    // injected severity is ground truth and never fills it.
    severity: severity ? { label: severityName(severity), tone: severity.toLowerCase() } : null,
    confidence: percent(p.confidence),
    started: clock(p.started_at),
    detected: clock(p.detected_at, { seconds: true }),
    affected: p.affected_services.length ? list(p.affected_services.map(serviceName)) : MISSING,
  };
}

function diagnosisPanel(diagnosis) {
  if (diagnosis === null) return null;
  return {
    summary: diagnosis.summary,
    rootCause: diagnosis.root_cause,
    evidence: [...diagnosis.evidence],
    actions: [...diagnosis.recommended_actions],
  };
}

// How the prediction compares with what was injected.
export function verdict(prediction, truth) {
  if (!truth) {
    return { kind: "none", label: "No ground truth",
      detail: "Nothing was injected at this time, as in production, so there is nothing to compare against." };
  }
  if (prediction.incident_type === truth.incident_type) {
    return { kind: "match", label: "Match", detail: "The model's class is the injected incident type." };
  }
  // In a cascade's first minutes only one fault is active, and the Window Labeling
  // Policy labels those windows by that fault, so predicting it is correct.
  // Retry amplification is listed but never counts as a fault.
  const active = truth.faults.filter((f) => f.active_now === true && f.injector !== "retry-amplification");
  if (truth.incident_type === "CASCADING_FAILURE" && active.length === 1 &&
      INJECTOR_TYPES[active[0].injector] === prediction.incident_type) {
    return { kind: "partial", label: "Matches the active fault",
      detail: "The run is a cascade, but only its first fault is active in this minute. " +
        "Until a second fault overlaps, the window is labeled by that fault." };
  }
  return { kind: "miss", label: "Mismatch", detail: "The model's class differs from the injected incident type." };
}

function faultRow(fault) {
  const state = fault.active_now === true ? { tone: "on", label: "Active this minute" }
    : fault.active_now === false ? { tone: "off", label: "Not active this minute" }
    : { tone: "none", label: "Activity unknown" };
  return {
    name: injectorName(fault.injector),
    target: fault.target ? serviceName(fault.target) : "No single target",
    window: `${clock(fault.active_from, { seconds: true })} to ${clock(fault.active_until, { seconds: true })}`,
    ...state,
    note: fault.injector === "retry-amplification" ? "Adds load only; not counted as a fault." : null,
  };
}

function comparisonPanel(incident) {
  const { prediction, ground_truth: truth, comparison } = incident;
  let detection = null;
  if (comparison) {
    detection = comparison.detection_latency_s === null
      ? "The alarm came before any fault was injected."
      : `Detected ${Math.round(comparison.detection_latency_s)} s after the first fault began.`;
  }
  return {
    prediction: {
      type: incidentName(prediction.incident_type),
      confidence: `${percent(prediction.confidence)} confidence`,
      model: prediction.model?.estimator ? `Model: ${prediction.model.estimator.replaceAll("_", " ")}` : null,
    },
    truth: truth && {
      type: incidentName(truth.incident_type),
      severity: `${severityName(truth.severity)} severity`,
      experiment: truth.experiment_id,
      faults: truth.faults.map(faultRow),
    },
    verdict: verdict(prediction, truth),
    detection,
  };
}

export function viewModel(reply) {
  checkReply(reply);
  const { health, incident } = reply;
  return {
    status: systemStatus(health),
    report: report(health, incident),
    window: health.window_start ? `Minute starting ${clock(health.window_start)}` : "No recent minute of data",
    generated: `Updated ${clock(reply.generated_at, { seconds: true })}`,
    noData: health.status === "NO_DATA",
    kpis: kpis(health),
    services: services(health),
    incident: incident && incidentPanel(incident),
    diagnosis: incident ? diagnosisPanel(incident.diagnosis) : null,
    diagnosisPending: Boolean(incident) && incident.diagnosis === null,
    comparison: incident && comparisonPanel(incident),
  };
}
