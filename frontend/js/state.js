/**
 * One tiny observable store. Views subscribe to slices and re-render on change.
 * No framework, no proxies, no magic: explicit set() calls and shallow comparison.
 */

const listeners = new Map();
let nextId = 1;

const store = {
  connection: "connecting", // connecting | live | reconnecting | offline
  dependencies: {},
  summary: null,
  timeseries: { points: [], windows: [] },
  drift: null,
  demoStream: null,
  incidents: [],
  pendingReviews: { items: [], count: 0 },
  incidentFilter: "all",
  selectedIncident: null,
  investigation: null,
  review: null,
  audit: null,
  policies: null,
  comparison: null,
  models: null,
  heldout: null,
  alerts: [],
  ticker: [],
  errors: [],
  loading: {},
};

export function getState() {
  return store;
}

export function subscribe(keys, handler) {
  const id = nextId++;
  listeners.set(id, { keys: Array.isArray(keys) ? keys : [keys], handler });
  return () => listeners.delete(id);
}

export function setState(patch) {
  const changed = [];
  for (const [key, value] of Object.entries(patch)) {
    if (store[key] === value) continue;
    store[key] = value;
    changed.push(key);
  }
  if (!changed.length) return;
  for (const { keys, handler } of listeners.values()) {
    if (keys.some((key) => changed.includes(key))) {
      try {
        handler(store, changed);
      } catch (error) {
        console.error("state listener failed", error);
      }
    }
  }
}

export function setLoading(key, value) {
  setState({ loading: { ...store.loading, [key]: value } });
}

/** Prepend into a bounded list so live feeds cannot grow without limit. */
export function pushBounded(key, item, limit = 60) {
  const next = [item, ...store[key]].slice(0, limit);
  setState({ [key]: next });
}

export function recordError(error) {
  const entry = {
    code: error?.code || "unknown_error",
    detail: error?.detail || String(error),
    at: new Date().toISOString(),
  };
  pushBounded("errors", entry, 20);
  return entry;
}
