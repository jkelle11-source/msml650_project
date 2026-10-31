// Where the console reads from. Two settings change between milestones:
//
//   now (M7, no API yet)    source: "mock"                          reads mock/<scenario>.json
//   M7, Jake's mock API     source: "api", apiBaseUrl: "<his URL>"  sends ?scenario=<name>
//   M8-5, live data         source: "api", scenarios: false         no picker, no ?scenario
//
// For a quick check without editing this file, add ?api=<base URL> to the page's address.
export const CONFIG = {
  source: "mock",
  apiBaseUrl: "",
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
