/**
 * Typed fetch layer. Every call is authenticated with the stored bearer token and
 * every failure surfaces the backend's structured {code, detail} error.
 */

const TOKEN_KEY = "fsd.token";
const ROLE_KEY = "fsd.role";
const USER_KEY = "fsd.username";

export class ApiError extends Error {
  constructor(status, code, detail) {
    super(detail || `Request failed with status ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.code = code || "request_failed";
    this.detail = detail || this.message;
  }
}

export const session = {
  get token() {
    return localStorage.getItem(TOKEN_KEY);
  },
  get role() {
    return localStorage.getItem(ROLE_KEY) || "analyst";
  },
  get username() {
    return localStorage.getItem(USER_KEY) || "analyst";
  },
  get isAuthenticated() {
    return Boolean(localStorage.getItem(TOKEN_KEY));
  },
  save(token, role, username) {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(ROLE_KEY, role);
    localStorage.setItem(USER_KEY, username);
  },
  clear() {
    [TOKEN_KEY, ROLE_KEY, USER_KEY].forEach((key) => localStorage.removeItem(key));
  },
};

async function request(path, { method = "GET", body, auth = true, signal } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (auth) {
    const token = session.token;
    if (!token) throw new ApiError(401, "not_authenticated", "Sign in to continue");
    headers.Authorization = `Bearer ${token}`;
  }

  let response;
  try {
    response = await fetch(`/api${path}`, {
      method,
      headers,
      signal,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ApiError(0, "network_unreachable", "The API is unreachable from this browser");
  }

  if (response.status === 401 && auth) {
    session.clear();
    throw new ApiError(401, "session_expired", "Session expired; sign in again");
  }

  const text = await response.text();
  const payload = text ? safeJson(text) : null;
  if (!response.ok) {
    const error = payload && payload.error ? payload.error : {};
    throw new ApiError(response.status, error.code, error.detail || response.statusText);
  }
  return payload;
}

function safeJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return { raw: text };
  }
}

export const api = {
  async login(username, password) {
    const result = await request("/auth/token", {
      method: "POST",
      auth: false,
      body: { username, password },
    });
    session.save(result.access_token, result.role, username);
    return result;
  },
  health: () => request("/health"),
  summary: () => request("/metrics/summary"),
  timeseries: (buckets = 60) => request(`/metrics/timeseries?buckets=${buckets}`),
  drift: () => request("/metrics/drift"),
  heldout: () => request("/metrics/heldout"),
  demoStream: () => request("/demo/stream"),
  startDemoStream: () =>
    request("/demo/stream", {
      method: "POST",
      body: { scenario: "validation_spike_val_s1" },
    }),
  incidents: (params = {}) => {
    const query = new URLSearchParams();
    if (params.status) query.set("status", params.status);
    query.set("limit", String(params.limit ?? 50));
    return request(`/incidents?${query.toString()}`);
  },
  incident: (id) => request(`/incidents/${encodeURIComponent(id)}`),
  investigation: (id) => request(`/incidents/${encodeURIComponent(id)}/investigation`),
  review: (id) => request(`/decisions/${encodeURIComponent(id)}/review`),
  audit: (id) => request(`/decisions/${encodeURIComponent(id)}/audit`),
  decide: (id, payload) =>
    request(`/decisions/${encodeURIComponent(id)}`, { method: "POST", body: payload }),
  outcome: (decisionId, payload) =>
    request(`/feedback/${encodeURIComponent(decisionId)}/outcome`, {
      method: "POST",
      body: payload,
    }),
  models: () => request("/models"),
  policies: () => request("/models/policies"),
  policyComparison: () => request("/models/policies/comparison"),
  promotePolicy: (versionId) =>
    request(`/models/policies/${versionId}/promote`, { method: "POST" }),
  rollbackPolicy: (versionId) =>
    request(`/models/policies/${versionId}/rollback`, { method: "POST" }),
};
