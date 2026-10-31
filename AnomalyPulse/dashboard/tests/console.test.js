// npm test, or node --test tests/*.test.js (Node 20+; no packages to install)
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { test } from "node:test";

import { SCENARIOS } from "../js/config.js";
import { FeedError, dashboardUrl, fetchReply } from "../js/feed.js";
import { clock, count, latency, list, percent } from "../js/format.js";
import { ContractError, checkReply, verdict, viewModel } from "../js/model.js";

const mockDir = new URL("../mock/", import.meta.url);
const mock = (name) => JSON.parse(readFileSync(new URL(`${name}.json`, mockDir), "utf8"));
const copy = (value) => structuredClone(value);
const SERVICES = ["product-service", "cart-service", "order-service", "payment-service"];

// --- formatting ------------------------------------------------------------

test("numbers are formatted for reading, and null is a dash", () => {
  assert.equal(count(1200), "1,200");
  assert.equal(percent(0.025), "2.5%");
  assert.equal(percent(0.1667), "16.7%");
  assert.equal(percent(0.5), "50%");
  assert.equal(percent(0), "0%");
  assert.equal(latency(260), "260 ms");
  assert.equal(latency(2950), "2.95 s");
  assert.equal(clock("2026-10-30T14:33:04.000Z"), "14:33 UTC");
  assert.equal(clock("2026-10-30T14:33:04.000Z", { seconds: true }), "14:33:04 UTC");
  for (const f of [count, percent, latency, clock]) assert.equal(f(null), "—");
  assert.equal(list(["Orders", "Payments"]), "Orders and Payments");
  assert.equal(list(["Product", "Cart", "Orders"]), "Product, Cart and Orders");
});

// --- the mocks follow the contract --------------------------------------------

test("there is one mock per scenario, and nothing else", () => {
  const files = readdirSync(mockDir).filter((f) => f.endsWith(".json")).map((f) => f.slice(0, -5)).sort();
  assert.deepEqual(files, SCENARIOS.map((s) => s.id).sort());
});

for (const { id } of SCENARIOS) {
  test(`mock ${id} keeps the contract's rules`, () => {
    const reply = checkReply(mock(id));
    const { health, incident } = reply;
    assert.deepEqual(health.services.map((s) => s.service), SERVICES);
    assert.equal(health.active_incidents, incident ? 1 : 0);
    if (health.status !== "NO_DATA") assert.equal(health.status, incident ? "INCIDENT" : "HEALTHY");
    if (incident) {
      assert.equal(incident.ground_truth === null, incident.comparison === null);
      assert.notEqual(incident.prediction.incident_type, "NORMAL");
      for (const service of health.services) {
        const affected = incident.prediction.affected_services.includes(service.service);
        assert.equal(service.status, affected ? "DEGRADED" : "HEALTHY", service.service);
      }
    }
  });

  test(`mock ${id} renders with no missing text`, () => {
    const text = JSON.stringify(viewModel(mock(id)));
    for (const bad of ["undefined", "NaN", "[object", "Infinity"]) assert.ok(!text.includes(bad), `${id}: ${bad}`);
  });
}

// --- what each scenario shows --------------------------------------------------

test("healthy: no incident, nothing invented", () => {
  const vm = viewModel(mock("healthy"));
  assert.equal(vm.status.label, "Healthy");
  assert.equal(vm.incident, null);
  assert.equal(vm.comparison, null);
});

test("incident: Jake's example reads as on the contract's page", () => {
  const vm = viewModel(mock("incident"));
  assert.equal(vm.status.label, "Incident");
  assert.deepEqual(vm.kpis.map((k) => k.value), ["1,200", "2.5%", "260 ms", "1"]);
  assert.equal(vm.incident.type, "Dependency failure");
  assert.equal(vm.incident.confidence, "93%");
  assert.equal(vm.incident.affected, "Orders and Payments");
  assert.deepEqual(vm.incident.severity, { label: "High", tone: "high" });
  assert.equal(vm.comparison.verdict.label, "Match");
  assert.equal(vm.comparison.detection, "Detected 69 s after the first fault began.");
});

test("diagnosing: severity is pending, never borrowed from ground truth", () => {
  const vm = viewModel(mock("diagnosing"));
  assert.equal(vm.diagnosisPending, true);
  assert.equal(vm.diagnosis, null);
  assert.equal(vm.incident.severity, null);
  assert.equal(vm.comparison.truth.severity, "High severity");     // shown only on the truth side
});

test("cascade_early: predicting the first fault matches the active fault, not a mismatch", () => {
  const vm = viewModel(mock("cascade_early"));
  assert.equal(vm.comparison.prediction.type, "Traffic spike");
  assert.equal(vm.comparison.truth.type, "Cascading failure");
  assert.equal(vm.comparison.verdict.label, "Matches the active fault");
  assert.deepEqual(vm.comparison.truth.faults.map((f) => f.label),
    ["Active this minute", "Not active this minute", "Not active this minute", "Not active this minute"]);
  assert.equal(vm.comparison.truth.faults[0].target, "No single target");
});

test("no_ground_truth: the prediction and diagnosis stand alone", () => {
  const vm = viewModel(mock("no_ground_truth"));
  assert.equal(vm.comparison.truth, null);
  assert.equal(vm.comparison.verdict.label, "No ground truth");
  assert.equal(vm.comparison.detection, null);
  assert.ok(vm.diagnosis.summary);
});

test("no_data: never says healthy, every number a dash", () => {
  const vm = viewModel(mock("no_data"));
  assert.equal(vm.status.label, "No data");
  assert.equal(vm.noData, true);
  assert.deepEqual(vm.kpis.slice(0, 3).map((k) => k.value), ["—", "—", "—"]);
  assert.ok(vm.services.every((s) => s.state === "No data" && s.requests === "—" && s.success === null));
});

// --- the verdict ---------------------------------------------------------------

test("verdict: a second overlapping fault makes a first-fault prediction a mismatch", () => {
  const reply = mock("cascade_early");
  reply.incident.ground_truth.faults[1].active_now = true;           // throttling now overlaps
  assert.equal(verdict(reply.incident.prediction, reply.incident.ground_truth).label, "Mismatch");
});

test("verdict: retry amplification never counts as a second fault", () => {
  const reply = mock("cascade_early");
  reply.incident.ground_truth.faults[3].active_now = true;           // only retries join the spike
  assert.equal(verdict(reply.incident.prediction, reply.incident.ground_truth).label, "Matches the active fault");
});

test("verdict: a wrong class is a mismatch", () => {
  const reply = mock("incident");
  reply.incident.prediction.incident_type = "TRAFFIC_SPIKE";
  assert.equal(verdict(reply.incident.prediction, reply.incident.ground_truth).label, "Mismatch");
});

test("an alarm before any injected fault says so instead of a negative latency", () => {
  const reply = mock("incident");
  reply.incident.comparison.detection_latency_s = null;
  assert.equal(viewModel(reply).comparison.detection, "The alarm came before any fault was injected.");
});

// --- contract checks ---------------------------------------------------------------

test("a missing key or another contract version is refused", () => {
  const noHealth = copy(mock("healthy")); delete noHealth.health;
  assert.throws(() => checkReply(noHealth), ContractError);
  const noStatus = copy(mock("healthy")); delete noStatus.health.status;
  assert.throws(() => checkReply(noStatus), /health is missing status/);
  const v2 = copy(mock("healthy")); v2.contract_version = 2;
  assert.throws(() => checkReply(v2), /version 2/);
});

test("an added field is fine: the contract only versions removals and renames", () => {
  const extra = copy(mock("incident")); extra.health.new_metric = 5; extra.incident.history = [];
  assert.doesNotThrow(() => viewModel(extra));
});

test("Bedrock's text is kept as text, markup and all", () => {
  const reply = mock("incident");
  reply.incident.diagnosis.summary = "<img src=x onerror=alert(1)>";
  assert.equal(viewModel(reply).diagnosis.summary, "<img src=x onerror=alert(1)>");   // render.js sets textContent
});

// --- fetching -----------------------------------------------------------------------

test("the URL: mock files, the mock API with ?scenario, the live API without", () => {
  assert.equal(dashboardUrl({ source: "mock" }, "incident"), "./mock/incident.json");
  assert.equal(dashboardUrl({ source: "api", apiBaseUrl: "https://x.example/dev/", scenarios: true }, "no_data"),
    "https://x.example/dev/dashboard?scenario=no_data");
  assert.equal(dashboardUrl({ source: "api", apiBaseUrl: "https://x.example/dev", scenarios: false }, "no_data"),
    "https://x.example/dev/dashboard");
  assert.throws(() => dashboardUrl({ source: "api", apiBaseUrl: "" }, "healthy"), FeedError);
});

const reply = (status, body) => async () => new Response(typeof body === "string" ? body : JSON.stringify(body), { status });

test("fetchReply returns a checked reply", async () => {
  assert.equal((await fetchReply("u", { fetcher: reply(200, mock("incident")) })).contract_version, 1);
});

test("fetchReply reports a 503 with the API's reason", async () => {
  await assert.rejects(fetchReply("u", { fetcher: reply(503, { error: "Incidents table unreachable" }) }),
    { name: "Error", message: "HTTP 503: Incidents table unreachable" });
});

test("fetchReply reports a reply that isn't JSON, or breaks the contract", async () => {
  await assert.rejects(fetchReply("u", { fetcher: reply(200, "<html>") }), /isn't JSON/);
  await assert.rejects(fetchReply("u", { fetcher: reply(200, { contract_version: 1 }) }), FeedError);
});

test("fetchReply gives up after the timeout", async () => {
  const hang = (url, { signal }) => new Promise((_, reject) => signal.addEventListener("abort", () => reject(signal.reason)));
  // AbortSignal.timeout's timer doesn't keep Node running (a browser tab stays open anyway).
  const keepAlive = setTimeout(() => {}, 1000);
  try {
    await assert.rejects(fetchReply("u", { fetcher: hang, timeoutMs: 50 }), /no answer within 0.05 s/);
  } finally {
    clearTimeout(keepAlive);
  }
});

// --- the report's opening sentences ------------------------------------------------

test("the headline says what is happening, where, in plain words", () => {
  const r = (name) => viewModel(mock(name)).report;
  assert.equal(r("incident").headline, "A dependency failure is affecting Orders and Payments.");
  assert.equal(r("incident").deck, "The model is 93% confident. It started at 14:32 UTC and was detected " +
    "at 14:33:04 UTC. Bedrock rates it high severity.");
  assert.match(r("diagnosing").deck, /Bedrock's explanation hasn't arrived yet\.$/);
  assert.equal(r("cascade_early").headline, "A traffic spike is hitting Product, Cart and Orders.");
  assert.equal(r("healthy").headline, "All four services are healthy.");
  assert.equal(r("healthy").deck, "They handled 1,200 requests in the minute, with no errors and a p95 latency of 170 ms.");
  assert.equal(r("no_data").headline, "No telemetry has arrived for more than three minutes.");
  assert.equal(r("no_data").label, "No data");
});

test("the headline never claims health while a service is idle or degraded", () => {
  const reply = mock("healthy");
  reply.health.services[1].status = "IDLE";
  assert.equal(viewModel(reply).report.headline, "No incident is active.");
});
