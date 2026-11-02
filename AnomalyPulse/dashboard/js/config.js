// Where the console reads from.
//
//   M7 (now)      source: "api", scenarios: true    Jake's mock API, ?scenario=<name> picks the state
//   M8-5, live    source: "api", scenarios: false   live data, no picker
//
// Two overrides in the page's address, without editing this file:
//   ?source=mock      read the local copies in mock/ (works with no network: the demo's fallback)
//   ?api=<base URL>   read another API
export const CONFIG = {
  source: "api",
  // M7-2's read API (also in SSM at /anomalypulse/dashboard/url).
  apiBaseUrl: "https://ffc8nyi6jl.execute-api.us-east-2.amazonaws.com",
  scenarios: true,
  pollMs: 15000,
  timeoutMs: 8000,
};

// The read API's fixed mock states (Docs/DASHBOARD_CONTRACT.md, "Mock scenarios").
export const SCENARIOS = [
  { id: "healthy", label: "Healthy" },
  { id: "diagnosing", label: "Incident, diagnosis pending" },
  { id: "incident", label: "Incident, diagnosed" },
  { id: "cascade_early", label: "Cascade, first minutes" },
  { id: "no_ground_truth", label: "Incident with no injected fault" },
  { id: "no_data", label: "Pipeline stopped" },
];
