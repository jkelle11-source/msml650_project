// Contract v1 -> display-only view model. Never calculate backend health/status,
// derive system p95 from per-service p95, or add Payment to system requests.
import {
  INJECTOR_TYPES, MISSING, clock, count, dateTime, incidentName, injectorName,
  latency, list, percent, serviceName, severityName,
} from "./format.js";

export const CONTRACT_VERSION = 1;
export class ContractError extends Error {}
const SERVICE_IDS = ["product-service", "cart-service", "order-service", "payment-service"];
const INCIDENT_TYPES = ["TRAFFIC_SPIKE", "DATABASE_THROTTLING", "LAMBDA_DEGRADATION", "DEPENDENCY_FAILURE", "CASCADING_FAILURE"];
const HEALTH_STATUSES = ["HEALTHY", "INCIDENT", "NO_DATA"];
const SERVICE_STATUSES = ["HEALTHY", "DEGRADED", "IDLE", "NO_DATA"];
const SEVERITIES = ["LOW", "MEDIUM", "HIGH"];
const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const validNumber = (v) => typeof v === "number" && Number.isFinite(v);
function requireKeys(obj, keys, where) {
  if (!isObject(obj)) throw new ContractError(`${where} is not an object`);
  for (const key of keys) if (!Object.hasOwn(obj, key)) throw new ContractError(`${where} is missing ${key}`);
}
function expect(v, condition, path) {
  if (!condition) throw new ContractError(`${path} is invalid for dashboard contract v1`);
}
function string(v, path) { expect(v, typeof v === "string" && v.length > 0, path); }
function timestamp(v, path, allowNull = false) {
  expect(v, (allowNull && v === null) ||
    (typeof v === "string" && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/.test(v) && !Number.isNaN(Date.parse(v))), path);
}
function number(v, path, { nullable = false, min = 0, max = Infinity, integer = false } = {}) {
  expect(v, (nullable && v === null) || (validNumber(v) && v >= min && v <= max && (!integer || Number.isInteger(v))), path);
}
function oneOf(v, permitted, path) { expect(v, permitted.includes(v), path); }
function strings(v, permitted, path) {
  expect(v, Array.isArray(v) && v.every((item) => typeof item === "string" && (!permitted || permitted.includes(item))), path);
}

export function checkReply(reply) {
  requireKeys(reply, ["contract_version", "generated_at", "health", "incident"], "the reply");
  if (reply.contract_version !== CONTRACT_VERSION) throw new ContractError(
    `this console reads contract version ${CONTRACT_VERSION}; the API sent version ${reply.contract_version}`);
  timestamp(reply.generated_at, "generated_at");
  const h = reply.health;
  requireKeys(h, ["window_start", "status", "requests_per_min", "error_rate", "p95_latency_ms", "active_incidents", "services"], "health");
  oneOf(h.status, HEALTH_STATUSES, "health.status");
  timestamp(h.window_start, "health.window_start", h.status === "NO_DATA");
  number(h.requests_per_min, "health.requests_per_min", { nullable: h.status === "NO_DATA", integer: true });
  number(h.error_rate, "health.error_rate", { nullable: true, max: 1 });
  number(h.p95_latency_ms, "health.p95_latency_ms", { nullable: true });
  number(h.active_incidents, "health.active_incidents", { min: 0, max: 1, integer: true });
  expect(h.services, Array.isArray(h.services) && h.services.length === SERVICE_IDS.length, "health.services");
  h.services.forEach((s, i) => {
    const path = `health.services[${i}]`;
    requireKeys(s, ["service", "status", "requests_per_min", "error_rate", "p95_latency_ms"], path);
    expect(s.service, s.service === SERVICE_IDS[i], `${path}.service`);
    oneOf(s.status, SERVICE_STATUSES, `${path}.status`);
    number(s.requests_per_min, `${path}.requests_per_min`, { nullable: h.status === "NO_DATA", integer: true });
    number(s.error_rate, `${path}.error_rate`, { nullable: true, max: 1 });
    number(s.p95_latency_ms, `${path}.p95_latency_ms`, { nullable: true });
  });
  if (h.status === "NO_DATA") {
    expect(h.window_start, h.window_start === null, "health.window_start");
    expect(h.requests_per_min, h.requests_per_min === null, "health.requests_per_min");
    expect(h.error_rate, h.error_rate === null, "health.error_rate");
    expect(h.p95_latency_ms, h.p95_latency_ms === null, "health.p95_latency_ms");
    h.services.forEach((s, i) => {
      expect(s.status, s.status === "NO_DATA", `health.services[${i}].status`);
      for (const key of ["requests_per_min", "error_rate", "p95_latency_ms"])
        expect(s[key], s[key] === null, `health.services[${i}].${key}`);
    });
  }
  if (reply.incident === null) return reply;
  const i = reply.incident;
  requireKeys(i, ["incident_id", "prediction", "diagnosis", "ground_truth", "comparison"], "incident");
  string(i.incident_id, "incident.incident_id");
  const p = i.prediction;
  requireKeys(p, ["incident_type", "confidence", "started_at", "detected_at", "affected_services", "model"], "incident.prediction");
  oneOf(p.incident_type, INCIDENT_TYPES, "incident.prediction.incident_type");
  number(p.confidence, "incident.prediction.confidence", { max: 1 });
  timestamp(p.started_at, "incident.prediction.started_at");
  timestamp(p.detected_at, "incident.prediction.detected_at");
  strings(p.affected_services, SERVICE_IDS, "incident.prediction.affected_services");
  requireKeys(p.model, ["estimator", "job"], "incident.prediction.model");
  string(p.model.estimator, "incident.prediction.model.estimator");
  string(p.model.job, "incident.prediction.model.job");
  if (i.diagnosis !== null) {
    const d = i.diagnosis;
    requireKeys(d, ["severity", "summary", "root_cause", "evidence", "recommended_actions"], "incident.diagnosis");
    oneOf(d.severity, SEVERITIES, "incident.diagnosis.severity");
    for (const key of ["summary", "root_cause"]) string(d[key], `incident.diagnosis.${key}`);
    strings(d.evidence, null, "incident.diagnosis.evidence");
    strings(d.recommended_actions, null, "incident.diagnosis.recommended_actions");
  }
  if (i.ground_truth === null) {
    expect(i.comparison, i.comparison === null, "incident.comparison");
    return reply;
  }
  const g = i.ground_truth;
  requireKeys(g, ["experiment_id", "incident_type", "severity", "faults"], "incident.ground_truth");
  string(g.experiment_id, "incident.ground_truth.experiment_id");
  oneOf(g.incident_type, INCIDENT_TYPES, "incident.ground_truth.incident_type");
  oneOf(g.severity, SEVERITIES, "incident.ground_truth.severity");
  expect(g.faults, Array.isArray(g.faults), "incident.ground_truth.faults");
  g.faults.forEach((f, idx) => {
    const where = `incident.ground_truth.faults[${idx}]`;
    requireKeys(f, ["injector", "target", "active_from", "active_until", "active_now"], where);
    oneOf(f.injector, ["traffic-spike", "db-throttling", "lambda-degradation", "payment-failure", "retry-amplification"], `${where}.injector`);
    expect(f.target, f.target === null || SERVICE_IDS.includes(f.target), `${where}.target`);
    timestamp(f.active_from, `${where}.active_from`);
    timestamp(f.active_until, `${where}.active_until`);
    expect(f.active_now, typeof f.active_now === "boolean" || (h.window_start === null && f.active_now === null), `${where}.active_now`);
  });
  requireKeys(i.comparison, ["detection_latency_s"], "incident.comparison");
  number(i.comparison.detection_latency_s, "incident.comparison.detection_latency_s", { nullable: true });
  return reply;
}

const TONES = { HEALTHY: "ok", INCIDENT: "alarm", NO_DATA: "none", DEGRADED: "warn", IDLE: "idle" };
const STATES = { HEALTHY: "Healthy", INCIDENT: "Incident", NO_DATA: "No data", DEGRADED: "Degraded", IDLE: "Idle" };
const labels = (ids) => ids.map(serviceName);
export function verdict(prediction, truth) {
  if (!truth) return { kind: "none", label: "No ground truth", detail: "No injected-fault record is available for this window. This is not an error." };
  if (prediction.incident_type === truth.incident_type)
    return { kind: "match", label: "Same classification", detail: "The model's window class and the run's injected class agree." };
  const active = truth.faults.filter((f) => f.active_now === true && f.injector !== "retry-amplification");
  if (truth.incident_type === "CASCADING_FAILURE" && active.length === 1 && INJECTOR_TYPES[active[0].injector] === prediction.incident_type)
    return { kind: "partial", label: "Matches active fault", detail: "The run is a cascade, but only one of its injected faults is active in this minute. The window labeling policy uses that fault's class while it is the only one." };
  return { kind: "different", label: "Different classifications", detail: "The model class describes this observed window; the injected class describes the full experiment. Compare the active faults before interpreting the difference." };
}
function serviceRows(health, incident) {
  const affected = new Set(incident?.prediction.affected_services ?? []);
  return health.services.map((row) => ({
    name: serviceName(row.service), state: STATES[row.status], tone: TONES[row.status],
    affected: affected.has(row.service), requests: count(row.requests_per_min),
    errors: percent(row.error_rate), p95: latency(row.p95_latency_ms),
  }));
}
function incidentPanel(incident) {
  const p = incident.prediction;
  return {
    id: incident.incident_id, classCode: p.incident_type, type: incidentName(p.incident_type),
    confidence: percent(p.confidence), confidenceFraction: p.confidence,
    started: dateTime(p.started_at), detected: dateTime(p.detected_at),
    affected: list(labels(p.affected_services)),
    estimator: p.model.estimator, modelJob: p.model.job,
    severity: incident.diagnosis ? { label: severityName(incident.diagnosis.severity), tone: incident.diagnosis.severity.toLowerCase() } : null,
  };
}
function diagnosisPanel(d) {
  return d === null ? null : { severity: severityName(d.severity), rawSeverity: d.severity,
    summary: d.summary, rootCause: d.root_cause, evidence: [...d.evidence], actions: [...d.recommended_actions] };
}
function faultRow(f) {
  return { injector: f.injector, name: injectorName(f.injector),
    target: f.target === null ? "No single target" : serviceName(f.target),
    from: dateTime(f.active_from), until: dateTime(f.active_until),
    active: f.active_now, state: f.active_now === null ? "Activity unavailable" : f.active_now ? "Active this minute" : "Not active this minute",
    note: f.injector === "retry-amplification" ? "Load amplification, not counted as an injected fault" : null };
}
function comparisonPanel(incident) {
  const p = incident.prediction, g = incident.ground_truth;
  const latencyValue = incident.comparison?.detection_latency_s;
  return { prediction: {
    classCode: p.incident_type, type: incidentName(p.incident_type), confidence: percent(p.confidence),
    affected: list(labels(p.affected_services)), detected: dateTime(p.detected_at),
    estimator: p.model.estimator, modelJob: p.model.job,
  }, truth: g && { classCode: g.incident_type, type: incidentName(g.incident_type), severity: severityName(g.severity),
    experiment: g.experiment_id, faults: g.faults.map(faultRow) },
  verdict: verdict(p, g), detection: incident.comparison ? (latencyValue === null
    ? "Alarm occurred before any injected fault began."
    : `Detection latency: ${Number(latencyValue.toFixed(1))} s after first fault activation.`) : null };
}
function timeline(incident, generatedAt) {
  if (!incident) return [];
  const items = [];
  const add = (at, kind, label, detail = "", active = false, planned = false) => { if (at) items.push({ at, kind, label, detail, active, planned, time: dateTime(at) }); };
  const p = incident.prediction;
  add(p.started_at, "incident", "Incident start", "First classified incident window");
  add(p.detected_at, "detection", "ML detection", incidentName(p.incident_type));
  // A fault time after the reply was generated hasn't happened yet: while a run is going,
  // its manifest holds only the planned times.
  const planned = (at) => Date.parse(at) > Date.parse(generatedAt);
  incident.ground_truth?.faults.forEach((f, i) => {
    const name = injectorName(f.injector);
    add(f.active_from, "fault-start", `${name} ${planned(f.active_from) ? "planned start" : "start"}`, f.target ? serviceName(f.target) : "No single target", f.active_now === true, planned(f.active_from));
    add(f.active_until, "fault-end", `${name} ${planned(f.active_until) ? "planned end" : "end"}`, `Fault ${i + 1} · ${f.active_now === true ? "active in reported window" : "not active in reported window"}`, f.active_now === true, planned(f.active_until));
  });
  return items.sort((a, b) => a.at.localeCompare(b.at) || a.label.localeCompare(b.label));
}
function makeReport(health, incident) {
  if (health.status === "NO_DATA") return { headline: "Telemetry unavailable", deck: "No recent window is available. System health cannot be established from the latest response." };
  if (!incident) return { headline: "No active incident", deck: "The latest backend snapshot reports no active incident. Per-service telemetry is shown below." };
  const p = incident.prediction;
  const severity = incident.diagnosis ? `AI severity: ${severityName(incident.diagnosis.severity)}.` : "AI severity pending.";
  return { headline: `${incidentName(p.incident_type)} detected`,
    deck: `${percent(p.confidence)} model confidence. ${list(labels(p.affected_services))} ${p.affected_services.length === 1 ? "is" : "are"} affected. ${severity}` };
}
export function viewModel(reply) {
  checkReply(reply);
  const { health, incident } = reply;
  return {
    status: { raw: health.status, label: STATES[health.status], tone: TONES[health.status] },
    window: dateTime(health.window_start), generated: dateTime(reply.generated_at),
    report: makeReport(health, incident), noData: health.status === "NO_DATA",
    kpis: [
      { label: "System health", value: STATES[health.status], tone: TONES[health.status] },
      { label: "Requests / min", value: count(health.requests_per_min) },
      { label: "Error rate", value: percent(health.error_rate) },
      { label: "P95 latency", value: latency(health.p95_latency_ms) },
      { label: "Active incidents", value: count(health.active_incidents) },
    ],
    services: serviceRows(health, incident),
    incident: incident && incidentPanel(incident),
    diagnosis: incident && diagnosisPanel(incident.diagnosis),
    diagnosisPending: Boolean(incident) && incident.diagnosis === null,
    comparison: incident && comparisonPanel(incident),
    timeline: timeline(incident, reply.generated_at),
  };
}
