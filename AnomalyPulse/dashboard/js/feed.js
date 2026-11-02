// A single GET /dashboard snapshot. Backend remains the only data source.
import { checkReply } from "./model.js";
export class FeedError extends Error {}

export function dashboardUrl({ source, apiBaseUrl, scenarios }, scenario = "healthy") {
  if (source === "mock") return `./mock/${encodeURIComponent(scenario)}.json`;
  if (source !== "api") throw new FeedError(`Unknown dashboard source: ${source}`);
  if (!apiBaseUrl?.trim()) throw new FeedError("No read API base URL configured in js/config.js.");
  let url;
  try {
    url = new URL(apiBaseUrl);
    if (!/^https?:$/.test(url.protocol)) throw Error("Unsupported API scheme");
  } catch { throw new FeedError("Read API base URL must be an absolute HTTP(S) URL."); }
  const path = url.pathname.replace(/\/+$/, "");
  url.pathname = path.endsWith("/dashboard") ? path : `${path}/dashboard`;
  url.search = "";
  url.hash = "";
  if (scenarios) url.searchParams.set("scenario", scenario);
  return url.toString();
}

export async function fetchReply(url, { fetcher = fetch, timeoutMs = 8000, signal } = {}) {
  const timeout = AbortSignal.timeout(timeoutMs);
  const combined = signal ? AbortSignal.any([signal, timeout]) : timeout;
  let response;
  try {
    response = await fetcher(url, { signal: combined, cache: "no-store", headers: { Accept: "application/json" } });
  } catch (error) {
    if (signal?.aborted) throw error; // Newer refresh or page hide superseded this one.
    if (timeout.aborted) throw new FeedError(`timeout: no answer within ${timeoutMs / 1000} s`);
    throw new FeedError("network error: dashboard API unreachable");
  }
  if (!response.ok) {
    let reason = "";
    try { reason = String((await response.json()).error ?? ""); } catch { /* Non-JSON error body */ }
    const name = response.status === 400 ? "bad scenario/request" : response.status === 503 ? "service unavailable" : "request failed";
    throw new FeedError(`HTTP ${response.status} (${name})${reason ? `: ${reason}` : ""}`);
  }
  let data;
  try { data = await response.json(); } catch { throw new FeedError("the response is not valid JSON"); }
  try { return checkReply(data); } catch (error) { throw new FeedError(`incompatible dashboard data: ${error.message}`); }
}
