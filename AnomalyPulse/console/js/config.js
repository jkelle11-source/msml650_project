// Where the console reads from.
//   M8-5 (now)    source: "api", scenarios: false   live data, no picker
//   M7            source: "api", scenarios: true    the API deployed with DashboardSource=mock; the picker sends ?scenario=
// Overrides in the page's address, without editing this file:
//   ?source=mock      the snapshots of the API's replies in mock/ (no network needed: the demo's fallback)
//   ?api=<base URL>   another read API
export const CONFIG = {
  source: "api",
  // M7-2's read API (also in SSM at /anomalypulse/dashboard/url).
  apiBaseUrl: "https://ffc8nyi6jl.execute-api.us-east-2.amazonaws.com",
  scenarios: false,
  pollMs: 15000,
  timeoutMs: 8000,
};

export const SCENARIOS = [
  { id: "healthy", label: "Healthy" },
  { id: "diagnosing", label: "Diagnosing" },
  { id: "incident", label: "Incident" },
  { id: "cascade_early", label: "Cascade · early" },
  { id: "no_ground_truth", label: "No ground truth" },
  { id: "no_data", label: "No data" },
];
