// Presentation formatting. Do not substitute zero for a null measurement.
export const SERVICE_NAMES = {
  "product-service": "Product",
  "cart-service": "Cart",
  "order-service": "Order",
  "payment-service": "Payment",
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
export const INJECTOR_TYPES = {
  "traffic-spike": "TRAFFIC_SPIKE",
  "db-throttling": "DATABASE_THROTTLING",
  "lambda-degradation": "LAMBDA_DEGRADATION",
  "payment-failure": "DEPENDENCY_FAILURE",
};
export const SEVERITY_NAMES = { LOW: "Low", MEDIUM: "Medium", HIGH: "High" };
export const MISSING = "—";
const label = (map, value) => value == null ? MISSING : (map[value] ?? String(value));
export const serviceName = (value) => label(SERVICE_NAMES, value);
export const incidentName = (value) => label(INCIDENT_NAMES, value);
export const injectorName = (value) => label(INJECTOR_NAMES, value);
export const severityName = (value) => label(SEVERITY_NAMES, value);
const numeric = (v) => typeof v === "number" && Number.isFinite(v);
export const count = (v) => numeric(v) ? Math.round(v).toLocaleString("en-US") : MISSING;
export const percent = (v) => numeric(v) ? `${Number((v * 100).toFixed(1))}%` : MISSING;
export function latency(v) {
  if (!numeric(v)) return MISSING;
  return v < 1000 ? `${Math.round(v).toLocaleString("en-US")} ms` : `${Number((v / 1000).toFixed(2))} s`;
}
export function clock(iso, { seconds = false } = {}) {
  if (!iso) return MISSING;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return MISSING;
  return `${date.toISOString().slice(11, seconds ? 19 : 16)} UTC`;
}
export function dateTime(iso) {
  if (!iso) return MISSING;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return MISSING;
  return date.toISOString().replace("T", " · ").replace(/\.\d{3}Z$/, " UTC");
}
export function list(items) {
  if (!items?.length) return MISSING;
  if (items.length === 1) return items[0];
  if (items.length === 2) return `${items[0]} and ${items[1]}`;
  return `${items.slice(0, -1).join(", ")} and ${items.at(-1)}`;
}
