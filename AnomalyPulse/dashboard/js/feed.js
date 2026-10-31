// Fetching one reply. Pure enough to test: the fetch function is passed in.
import { checkReply } from "./model.js";

export class FeedError extends Error {}

export function dashboardUrl({ source, apiBaseUrl, scenarios }, scenario) {
  if (source === "mock") return `./mock/${encodeURIComponent(scenario)}.json`;
  if (!apiBaseUrl) throw new FeedError("No read API address is set (apiBaseUrl in js/config.js).");
  const url = `${apiBaseUrl.replace(/\/+$/, "")}/dashboard`;
  return scenarios ? `${url}?scenario=${encodeURIComponent(scenario)}` : url;
}

export async function fetchReply(url, { fetcher = fetch, timeoutMs = 8000, signal } = {}) {
  const timeout = AbortSignal.timeout(timeoutMs);
  const combined = signal ? AbortSignal.any([signal, timeout]) : timeout;
  let response;
  try {
    response = await fetcher(url, { signal: combined, cache: "no-store", headers: { Accept: "application/json" } });
  } catch (error) {
    if (signal?.aborted) throw error;                     // superseded by a newer request
    if (timeout.aborted) throw new FeedError(`no answer within ${timeoutMs / 1000} s`);
    throw new FeedError("the request didn't reach the API");
  }
  if (!response.ok) {
    let reason = "";
    try { reason = (await response.json()).error ?? ""; } catch { /* not JSON */ }
    throw new FeedError(`HTTP ${response.status}${reason ? `: ${reason}` : ""}`);
  }
  let reply;
  try { reply = await response.json(); } catch { throw new FeedError("the reply isn't JSON"); }
  try { return checkReply(reply); } catch (error) { throw new FeedError(error.message); }
}
