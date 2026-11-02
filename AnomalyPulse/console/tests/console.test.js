import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { test } from "node:test";
import { SCENARIOS } from "../js/config.js";
import { dashboardUrl, fetchReply, FeedError } from "../js/feed.js";
import { clock, count, dateTime, latency, list, percent } from "../js/format.js";
import { checkReply, ContractError, verdict, viewModel } from "../js/model.js";
const fixture = (name) => JSON.parse(readFileSync(new URL(`../mock/${name}.json`, import.meta.url), "utf8"));
const clone = structuredClone;
const SERVICE_IDS = ["product-service", "cart-service", "order-service", "payment-service"];
const STATUS_LABELS = { HEALTHY: "Healthy", DEGRADED: "Degraded", IDLE: "Idle", NO_DATA: "No data" };

test("all six fixture files are present", () => {
  const files = readdirSync(new URL("../mock/", import.meta.url)).filter(x => x.endsWith(".json")).map(x => x.slice(0,-5)).sort();
  assert.deepEqual(files, SCENARIOS.map(s => s.id).sort());
});
test("null measurements display as em-dashes, numeric zero stays zero", () => {
  for (const fn of [clock, dateTime, count, percent, latency]) assert.equal(fn(null), "—");
  assert.equal(percent(0), "0%"); assert.equal(count(0), "0"); assert.equal(latency(0), "0 ms");
  assert.equal(percent(.025), "2.5%"); assert.equal(percent(.167), "16.7%"); assert.equal(percent(.5), "50%");
  assert.equal(count(1200), "1,200"); assert.equal(latency(2950), "2.95 s");
  assert.equal(clock("2026-10-30T14:33:04.000Z", { seconds:true }), "14:33:04 UTC");
  assert.equal(list([]), "—");
});
for (const { id } of SCENARIOS) {
  test(`${id}: fixture conforms to v1 and renders no bogus missing data`, () => {
    const data = checkReply(fixture(id));
    const vm = viewModel(data);
    assert.equal(data.contract_version, 1);
    assert.deepEqual(data.health.services.map(s => s.service), SERVICE_IDS);
    assert.equal(vm.kpis.length, 5);
    assert.equal(vm.services.length, 4);
    assert.deepEqual(vm.services.map(s => s.state), data.health.services.map(s => STATUS_LABELS[s.status]));
    for (const bad of ["undefined", "NaN", "[object Object]", "Infinity"])
      assert.ok(!JSON.stringify(vm).includes(bad), `${id} contains ${bad}`);
  });
}
test("healthy: no incident, healthy backend status, empty timeline", () => {
  const vm=viewModel(fixture("healthy"));
  assert.equal(vm.status.raw,"HEALTHY"); assert.equal(vm.incident,null);
  assert.equal(vm.comparison,null);assert.deepEqual(vm.timeline,[]);
  assert.deepEqual(vm.kpis.map(k=>k.value),["Healthy","1,200","0%","64 ms","0"]);
});
test("diagnosing: AI diagnosis and severity stay pending, never use ground-truth severity", () => {
  const vm=viewModel(fixture("diagnosing"));
  assert.equal(vm.diagnosisPending,true);assert.equal(vm.diagnosis,null);
  assert.equal(vm.incident.severity,null);assert.equal(vm.comparison.truth.severity,"High");
});
test("incident: full diagnosis, evidence, actions, model identity and ground truth", () => {
  const vm=viewModel(fixture("incident"));
  assert.equal(vm.incident.type,"Dependency failure");
  assert.equal(vm.incident.confidence,"93%");
  assert.equal(vm.incident.estimator,"logistic_regression");
  assert.equal(vm.incident.affected,"Order and Payment");
  assert.equal(vm.diagnosis.severity,"High");
  assert.equal(vm.diagnosis.evidence.length,2); assert.equal(vm.diagnosis.actions.length,3);
  assert.equal(vm.comparison.truth.experiment,"exp-mock-payment-failure");
  assert.equal(vm.comparison.truth.faults[0].injector,"payment-failure");
  assert.equal(vm.comparison.verdict.kind,"match");
  assert.match(vm.comparison.detection,/69 s/);
});
test("cascade_early: model and run classes differ legitimately with one active fault", () => {
  const vm=viewModel(fixture("cascade_early"));
  assert.equal(vm.comparison.prediction.classCode,"TRAFFIC_SPIKE");
  assert.equal(vm.comparison.truth.classCode,"CASCADING_FAILURE");
  assert.equal(vm.comparison.verdict.kind,"partial");
  assert.deepEqual(vm.comparison.truth.faults.map(f=>f.active),[true,false]);
  assert.equal(vm.comparison.truth.faults.length,2);
  assert.equal(vm.timeline.filter(x=>x.kind==="fault-start").length,2);
  assert.equal(vm.timeline.filter(x=>x.kind==="fault-end").length,2);
  assert.equal(vm.timeline.length,6);
  assert.deepEqual(vm.timeline.map(e=>e.at),[...vm.timeline.map(e=>e.at)].sort());
});
test("cascade_early timeline: fault times after generated_at are planned, earlier ones are not", () => {
  // generated_at 04:51:48.342Z; the spike runs 04:48:55-04:58:55, the payment failure is planned 04:52:00-04:57:00
  const vm=viewModel(fixture("cascade_early"));
  assert.deepEqual(vm.timeline.map(e=>[e.time,e.label,e.planned]),[
    ["2026-11-02 · 04:48:55 UTC","Traffic spike start",false],
    ["2026-11-02 · 04:49:00 UTC","Incident start",false],
    ["2026-11-02 · 04:50:04 UTC","ML detection",false],
    ["2026-11-02 · 04:52:00 UTC","Payment failure planned start",true],
    ["2026-11-02 · 04:57:00 UTC","Payment failure planned end",true],
    ["2026-11-02 · 04:58:55 UTC","Traffic spike planned end",true],
  ]);
});
test("a fault that has ended reads as an end, not a planned one", () => {
  const data=fixture("incident");
  data.generated_at="2026-11-02T05:00:00.000Z";               // after the fault's 04:53:55 end
  const end=viewModel(data).timeline.find(e=>e.kind==="fault-end");
  assert.equal(end.label,"Payment failure end"); assert.equal(end.planned,false);
});
test("no_ground_truth: missing injection is not an error or fabricated fault", () => {
  const vm=viewModel(fixture("no_ground_truth"));
  assert.equal(vm.comparison.truth,null);
  assert.equal(vm.comparison.verdict.kind,"none");
  assert.equal(vm.comparison.detection,null);
  assert.equal(vm.timeline.length,2);
});
test("no_data: never represented as healthy, every null measurement is —", () => {
  const vm=viewModel(fixture("no_data"));
  assert.equal(vm.status.raw,"NO_DATA");assert.equal(vm.noData,true);
  assert.deepEqual(vm.kpis.map(k=>k.value),["No data","—","—","—","0"]);
  assert.ok(vm.services.every(s=>s.state==="No data"&&s.requests==="—"&&s.errors==="—"&&s.p95==="—"));
  assert.equal(vm.incident,null);
});
test("null error rates and p95 of an idle service remain —; status is backend-supplied", () => {
  const data=fixture("healthy");
  data.health.services[1].status="IDLE";
  data.health.services[1].requests_per_min=0;
  data.health.services[1].error_rate=null;
  data.health.services[1].p95_latency_ms=null;
  data.health.error_rate=null;
  data.health.p95_latency_ms=null;
  const vm=viewModel(data);
  assert.equal(vm.services[1].state,"Idle");assert.equal(vm.services[1].requests,"0");
  assert.equal(vm.services[1].errors,"—");assert.equal(vm.services[1].p95,"—");
  assert.equal(vm.kpis[2].value,"—");assert.equal(vm.kpis[3].value,"—");
});
test("system requests and p95 use only health values, not sums/percentile averaging",()=>{
  const data=fixture("incident");
  data.health.requests_per_min=11;data.health.p95_latency_ms=123;
  data.health.services[3].requests_per_min=900000;
  assert.equal(viewModel(data).kpis[1].value,"11");
  assert.equal(viewModel(data).kpis[3].value,"123 ms");
});
test("affected highlighting uses prediction list, not error thresholds",()=>{
  const data=fixture("incident");
  data.health.services[0].error_rate=1;
  const vm=viewModel(data);
  assert.deepEqual(vm.services.map(s=>s.affected),[false,false,true,true]);
});
test("response must contain all nested contract fields, and version must be compatible",()=>{
  const data=fixture("incident");delete data.incident.prediction.model.job;
  assert.throws(()=>checkReply(data),/model is missing job/);
  const wrong=fixture("healthy");wrong.contract_version=2;
  assert.throws(()=>checkReply(wrong),/version 2/);
  const missing=fixture("healthy");delete missing.health.status;
  assert.throws(()=>checkReply(missing),ContractError);
});
test("unknown extra backend fields are tolerated; values aren't synthesized",()=>{
  const data=fixture("incident");data.incident.history=[];data.health.new_feature=17;
  assert.doesNotThrow(()=>viewModel(data));
});
test("Bedrock text passes through unchanged for DOM textContent (not HTML)",()=>{
  const data=fixture("incident");data.incident.diagnosis.summary="<img src=x onerror=alert(1)>";
  assert.equal(viewModel(data).diagnosis.summary,"<img src=x onerror=alert(1)>");
});
test("different classification is neutral, does not claim the model is wrong",()=>{
  const data=fixture("cascade_early");data.incident.ground_truth.faults[1].active_now=true;
  const v=verdict(data.incident.prediction,data.incident.ground_truth);
  assert.equal(v.kind,"different");assert.match(v.detail,/observed window/);
});
test("retry amplification does not count as an active second fault",()=>{
  const data=fixture("cascade_early");
  data.incident.ground_truth.faults.push({injector:"retry-amplification",target:null,
    active_from:"2026-11-02T04:49:30.000Z",active_until:"2026-11-02T04:56:00.000Z",active_now:true});
  assert.equal(verdict(data.incident.prediction,data.incident.ground_truth).kind,"partial");
});
test("a cascade's later single fault matches too, and the text doesn't call it the first",()=>{
  // The spike has stopped; only the payment failure (DEPENDENCY_FAILURE) is active.
  const data=fixture("cascade_early");
  data.incident.ground_truth.faults[0].active_now=false;
  data.incident.ground_truth.faults[1].active_now=true;
  data.incident.prediction.incident_type="DEPENDENCY_FAILURE";
  const v=verdict(data.incident.prediction,data.incident.ground_truth);
  assert.equal(v.kind,"partial");
  assert.match(v.detail,/only one of its injected faults is active/);
  assert.doesNotMatch(v.detail,/first/);
});
test("the overview leads with the incident: its name, confidence, affected services and AI severity",()=>{
  assert.deepEqual(viewModel(fixture("incident")).report,{headline:"Dependency failure detected",
    deck:"93% model confidence. Order and Payment are affected. AI severity: High."});
  assert.equal(viewModel(fixture("diagnosing")).report.deck,"93% model confidence. Order and Payment are affected. AI severity pending.");
  assert.equal(viewModel(fixture("cascade_early")).report.headline,"Traffic spike detected");
  assert.equal(viewModel(fixture("healthy")).report.headline,"No active incident");
  assert.equal(viewModel(fixture("no_data")).report.headline,"Telemetry unavailable");
});
test("before any reply, the page claims no state: no 'No active incident', no healthy check",()=>{
  const html=readFileSync(new URL("../index.html",import.meta.url),"utf8");
  assert.doesNotMatch(html,/No active incident/);
  assert.doesNotMatch(html,/✓/);
  assert.match(html,/No reply yet/);
});
test("null detection latency does not become zero",()=>{
  const data=fixture("incident");data.incident.comparison.detection_latency_s=null;
  assert.match(viewModel(data).comparison.detection,/before any injected fault/);
});
test("URL creation uses ?scenario for mock API and none for live API",()=>{
  assert.equal(dashboardUrl({source:"mock"},"incident"),"./mock/incident.json");
  assert.equal(dashboardUrl({source:"api",apiBaseUrl:"https://example.org/dev/",scenarios:true},"no_data"),"https://example.org/dev/dashboard?scenario=no_data");
  assert.equal(dashboardUrl({source:"api",apiBaseUrl:"https://example.org/dev",scenarios:false},"incident"),"https://example.org/dev/dashboard");
  assert.equal(dashboardUrl({source:"api",apiBaseUrl:"https://example.org/dev/dashboard",scenarios:true},"incident"),"https://example.org/dev/dashboard?scenario=incident");
  assert.throws(()=>dashboardUrl({source:"api",apiBaseUrl:""},"healthy"),FeedError);
});
const response=(status,body)=>async()=>new Response(typeof body==="string"?body:JSON.stringify(body),{status});
test("200 returns checked dashboard object",async()=>{
  assert.equal((await fetchReply("test",{fetcher:response(200,fixture("incident"))})).contract_version,1);
});
test("400 reports invalid scenario/request",async()=>{
  await assert.rejects(fetchReply("test",{fetcher:response(400,{error:"unknown scenario"})}),/HTTP 400.*unknown scenario/);
});
test("503 reports API's reason",async()=>{
  await assert.rejects(fetchReply("test",{fetcher:response(503,{error:"Incidents table unreachable"})}),/HTTP 503.*Incidents table unreachable/);
});
test("network failure reported independently",async()=>{
  await assert.rejects(fetchReply("test",{fetcher:async()=>{throw new TypeError("Failed to fetch");}}),/network error/);
});
test("malformed JSON or incompatible contract fails without fabricating data",async()=>{
  await assert.rejects(fetchReply("test",{fetcher:response(200,"<html>")}),/not valid JSON/);
  await assert.rejects(fetchReply("test",{fetcher:response(200,{contract_version:1})}),/incompatible dashboard data/);
});
test("timeout is identified",async()=>{
  const hang=(url,{signal})=>new Promise((_,reject)=>signal.addEventListener("abort",()=>reject(signal.reason),{once:true}));
  const keep=setTimeout(()=>{},1000);
  try{await assert.rejects(fetchReply("test",{fetcher:hang,timeoutMs:40}),/timeout: no answer within 0.04 s/);}finally{clearTimeout(keep);}
});

// --- what Jake's mock API sends (#27) -------------------------------------------------

const replyWith = (status, body) => async () => new Response(JSON.stringify(body), { status });

test("an unknown scenario's 400 shows the API's own reason", async () => {
  const body = { error: "unknown scenario 'x'; use one of: healthy, diagnosing, incident" };
  await assert.rejects(fetchReply("u", { fetcher: replyWith(400, body) }),
    { message: `HTTP 400 (bad scenario/request): ${body.error}` });
});

test("the API's unrounded shares and its own ids are accepted and read the same", () => {
  const api = fixture("incident");
  api.health.services[2].error_rate = 30 / 180;                 // 0.1666...
  api.incident.incident_id = "inc-mock-payment-failure";
  api.incident.ground_truth.experiment_id = "exp-mock-payment-failure";
  assert.doesNotThrow(() => checkReply(api));
  assert.equal(percent(api.health.services[2].error_rate), "16.7%");
  assert.doesNotThrow(() => viewModel(api));
});

test("the console reads Jake's API by default, with ?scenario on", async () => {
  const { CONFIG } = await import("../js/config.js");
  assert.equal(dashboardUrl(CONFIG, "incident"),
    "https://ffc8nyi6jl.execute-api.us-east-2.amazonaws.com/dashboard?scenario=incident");
});

// --- pages ------------------------------------------------------------------------------

import { PAGES, neighbours, pageFromHash, tabStatus } from "../js/nav.js";

test("the hash picks the page, and anything else opens the overview", () => {
  assert.equal(pageFromHash("#diagnosis"), "diagnosis");
  assert.equal(pageFromHash("#verify"), "verify");
  assert.equal(pageFromHash(""), "overview");
  assert.equal(pageFromHash("#nope"), "overview");
});

test("pages run overview, incident, diagnosis, ground truth, timeline, with previous and next", () => {
  assert.deepEqual(PAGES.map((p) => p.id), ["overview", "incident", "diagnosis", "verify", "timeline"]);
  assert.deepEqual(neighbours("overview"), { prev: null, next: PAGES[1] });
  assert.deepEqual(neighbours("timeline"), { prev: PAGES[3], next: null });
});

test("no element id is a page name, so opening #incident doesn't scroll the page past the header", () => {
  const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]);
  for (const { id } of PAGES) assert.ok(!ids.includes(id), `an element has id="${id}"`);
});

test("tab dots say what each page holds before it is opened", () => {
  assert.deepEqual(tabStatus(fixture("healthy")),
    { overview: "ok", incident: "none", diagnosis: "none", verify: "none", timeline: "none" });
  assert.deepEqual(tabStatus(fixture("incident")),
    { overview: "alarm", incident: "alarm", diagnosis: "info", verify: "info", timeline: "info" });
  assert.equal(tabStatus(fixture("diagnosing")).diagnosis, "pending");
  assert.equal(tabStatus(fixture("no_ground_truth")).verify, "none");
  assert.equal(tabStatus(fixture("no_data")).overview, "none");
});
