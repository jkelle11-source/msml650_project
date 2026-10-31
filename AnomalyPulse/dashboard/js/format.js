// Display names and number formats. Pure functions: no DOM, so the tests run them in Node.

export const SERVICE_NAMES = {
  "product-service": "Product",
  "cart-service": "Cart",
  "order-service": "Orders",
  "payment-service": "Payments",
};

export const INCIDENT_NAMES = {
  NORMAL: "Normal",
  TRAFFIC_SPIKE: "Traffic spike",
  DATABASE_THROTTLING: "Database throttling",
  LAMBDA_DEGRADATION: "Lambda degradation",
  DEPENDENCY_FAILURE: "Dependency failure",
  CASCADING_FAILURE: "Cascading failure",
};

export const INJECTOR_NAMES = {
  "traffic-spike": "Traffic spike",
  "db-throttling": "Database throttling",
  "lambda-degradation": "Lambda slowdown",
  "payment-failure": "Payment failure",
  "retry-amplification": "Retry amplification",
};

// The incident type a single injector produces (simulator/injectors/*.py).
export const INJECTOR_TYPES = {
  "traffic-spike": "TRAFFIC_SPIKE",
  "db-throttling": "DATABASE_THROTTLING",
  "lambda-degradation": "LAMBDA_DEGRADATION",
  "payment-failure": "DEPENDENCY_FAILURE",
};

export const SEVERITY_NAMES = { LOW: "Low", MEDIUM: "Medium", HIGH: "High" };

// Shown wherever the contract sends null for a number.
export const MISSING = "—";

const fallback = (names, key) => names[key] ?? String(key);
export const serviceName = (key) => fallback(SERVICE_NAMES, key);
export const incidentName = (key) => fallback(INCIDENT_NAMES, key);
export const injectorName = (key) => fallback(INJECTOR_NAMES, key);
export const severityName = (key) => fallback(SEVERITY_NAMES, key);

const isNumber = (value) => typeof value === "number" && Number.isFinite(value);

export function count(value) {
  return isNumber(value) ? Math.round(value).toLocaleString("en-US") : MISSING;
}

// A 0-1 share as a percentage: 0.025 -> "2.5%", 0.1667 -> "16.7%", 0 -> "0%".
export function percent(share) {
  if (!isNumber(share)) return MISSING;
  return `${Math.round(share * 1000) / 10}%`;
}

// Latency: "260 ms" under a second, "2.95 s" above it.
export function latency(ms) {
  if (!isNumber(ms)) return MISSING;
  if (ms < 1000) return `${Math.round(ms).toLocaleString("en-US")} ms`;
  return `${Number((ms / 1000).toFixed(2))} s`;
}

// "14:32 UTC" or, with seconds, "14:33:04 UTC". Null stays a dash.
export function clock(iso, { seconds = false } = {}) {
  if (!iso) return MISSING;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return MISSING;
  const text = date.toISOString().slice(11, seconds ? 19 : 16);
  return `${text} UTC`;
}

export function list(items) {
  if (items.length <= 1) return items.join("");
  if (items.length === 2) return `${items[0]} and ${items[1]}`;
  return `${items.slice(0, -1).join(", ")} and ${items.at(-1)}`;
}
