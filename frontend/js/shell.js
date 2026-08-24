/**
 * Application chrome shared by every page: dependency health strip, side rail,
 * header, live feed wiring, and periodic refresh. Pages register a bootstrap
 * function and a live-message handler; the shell owns everything else.
 */

import { api, session } from "./api.js";
import { getState, pushBounded, recordError, setState, subscribe } from "./state.js";
import { LiveFeed } from "./websocket.js";
import { attachRipples, clear, el, fmt, statusTone, toast } from "./ui.js";

const NAV = [
  {
    section: "Operations",
    items: [
      { href: "/pages/dashboard.html", label: "Risk Dashboard", icon: "gauge" },
      { href: "/pages/incidents.html", label: "Incidents", icon: "alert", badge: "incidents" },
      { href: "/pages/investigation.html", label: "Investigation", icon: "search" },
    ],
  },
  {
    section: "Governance",
    items: [
      { href: "/pages/models.html", label: "Models & Policies", icon: "layers" },
      { href: "/pages/reports.html", label: "Held-out Report", icon: "report" },
    ],
  },
];

const ICONS = {
  gauge:
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M12 13.5V8"/><circle cx="12" cy="13.5" r="8"/><path d="m17 8.5-1.8 1.8"/></svg>',
  alert:
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M12 4 3 19h18L12 4Z"/><path d="M12 10v4M12 17h.01"/></svg>',
  search:
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/></svg>',
  layers:
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3 8 4.5-8 4.5-8-4.5L12 3Z"/><path d="m4 12 8 4.5 8-4.5"/></svg>',
  report:
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M6 3h9l4 4v14H6z"/><path d="M9 12h6M9 16h6M9 8h3"/></svg>',
  shield:
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2 4 5.5v6c0 5 3.4 9.2 8 10.5 4.6-1.3 8-5.5 8-10.5v-6L12 2Z"/><path d="m8.4 12.2 2.3 2.4 4.6-4.8"/></svg>',
};

const DEP_LABELS = {
  postgres: "Postgres",
  redis: "Redis",
  stream: "Redpanda",
  fraud_model: "Fraud model",
  llm: "LLM",
  checkpoint: "Checkpoint",
  reward_model: "Reward model",
  response_policy: "Response policy",
};

let feed = null;

export function requireSession() {
  if (session.isAuthenticated) return true;
  window.location.replace("/index.html");
  return false;
}

export function mountShell({ title, subtitle, actions = [] }) {
  const app = document.getElementById("app");
  const strip = el("div", { class: "health-strip", id: "health-strip" }, [
    el("span", { class: "health-strip__label", text: "Dependencies" }),
    el("div", { class: "health-strip__items", id: "health-items", "aria-live": "polite" }, [
      el("span", { class: "skeleton", text: "loading dependency health" }),
    ]),
    el("span", { class: "badge", id: "connection-badge", "data-tone": "muted" }, [
      el("span", { class: "live-dot", "data-tone": "idle", "aria-hidden": "true" }),
      el("span", { id: "connection-text", text: "connecting" }),
    ]),
  ]);

  const rail = el("nav", { class: "rail", id: "rail", "aria-label": "Primary" }, [
    el("a", { class: "rail__brand", href: "/pages/dashboard.html" }, [
      el("span", { class: "brand-mark", html: ICONS.shield, "aria-hidden": "true" }),
      el("span", { class: "brand-text" }, [
        el("strong", { text: "Sentinel Risk" }),
        el("span", { text: "Fraud Spike Command" }),
      ]),
    ]),
    ...NAV.flatMap((group) => [
      el("p", { class: "rail__section", text: group.section }),
      el(
        "div",
        { class: "rail__nav" },
        group.items.map((item) =>
          el(
            "a",
            {
              class: "nav-link",
              href: item.href,
              "aria-current": window.location.pathname.endsWith(item.href.split("/").pop())
                ? "page"
                : null,
            },
            [
              el("span", { html: ICONS[item.icon], "aria-hidden": "true" }),
              el("span", { text: item.label }),
              item.badge
                ? el("span", { class: "nav-link__badge", id: `nav-badge-${item.badge}`, hidden: true })
                : null,
            ]
          )
        )
      ),
    ]),
    el("div", { class: "rail__footer" }, [
      el("a", { href: "http://localhost:3000", target: "_blank", rel: "noreferrer", text: "Grafana dashboards" }),
      el("a", { href: "http://localhost:5000", target: "_blank", rel: "noreferrer", text: "MLflow registry" }),
      el("a", { href: "/metrics", target: "_blank", rel: "noreferrer", text: "Prometheus metrics" }),
      el("span", { class: "eyebrow", text: "Costs marked as assumptions" }),
    ]),
  ]);

  const header = el("header", { class: "header" }, [
    el("button", {
      class: "btn btn--ghost btn--sm rail-toggle",
      id: "rail-toggle",
      "aria-label": "Toggle navigation",
      html: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 7h16M4 12h16M4 17h16"/></svg>',
    }),
    el("div", { class: "header__title" }, [
      el("h1", { text: title }),
      el("p", { id: "header-subtitle", text: subtitle }),
    ]),
    el("div", { class: "header__spacer" }),
    el("div", { class: "header__actions" }, [
      ...actions,
      el("span", { class: "clock" }, [
        el("span", { class: "live-dot", "aria-hidden": "true" }),
        el("span", { id: "shell-clock", text: "--:--:--" }),
      ]),
      el("div", { class: "identity" }, [
        el("span", { class: "identity__avatar", text: session.username.slice(0, 1).toUpperCase() }),
        el("span", { text: session.role }),
        el("button", {
          class: "btn btn--ghost btn--sm",
          text: "Sign out",
          onClick: () => {
            feed?.stop();
            session.clear();
            window.location.assign("/index.html");
          },
        }),
      ]),
    ]),
  ]);

  const main = el("main", { class: "main", id: "main" });
  app.append(strip, rail, header, main);
  attachRipples(document);

  document.getElementById("rail-toggle").addEventListener("click", () => {
    const open = rail.dataset.open === "true";
    rail.dataset.open = String(!open);
  });

  setInterval(() => {
    const clock = document.getElementById("shell-clock");
    if (clock) clock.textContent = new Date().toLocaleTimeString("en-GB", { hour12: false });
  }, 1000);

  subscribe(["dependencies"], renderHealthStrip);
  subscribe(["connection"], renderConnection);
  subscribe(["incidents"], renderNavBadges);
  return main;
}

function renderHealthStrip(state) {
  const container = document.getElementById("health-items");
  if (!container) return;
  const entries = Object.entries(state.dependencies || {});
  clear(container);
  if (!entries.length) {
    container.append(el("span", { class: "eyebrow", text: "No dependency data yet" }));
    return;
  }
  entries.forEach(([name, health]) => {
    container.append(
      el(
        "span",
        {
          class: "dep",
          "data-status": health.status,
          title: health.reason ? `${name}: ${health.reason}` : `${name}: ${health.status}`,
        },
        [
          el("span", { class: "dep__dot", "aria-hidden": "true" }),
          el("span", { text: DEP_LABELS[name] || fmt.words(name) }),
        ]
      )
    );
  });
}

function renderConnection(state) {
  const badge = document.getElementById("connection-badge");
  const text = document.getElementById("connection-text");
  if (!badge || !text) return;
  const map = {
    live: ["accent", "live stream"],
    connecting: ["muted", "connecting"],
    reconnecting: ["warning", "reconnecting"],
    offline: ["critical", "stream offline"],
  };
  const [tone, label] = map[state.connection] || map.connecting;
  badge.dataset.tone = tone;
  badge.querySelector(".live-dot").dataset.tone =
    state.connection === "live" ? "accent" : state.connection === "offline" ? "critical" : "idle";
  text.textContent = label;
}

function renderNavBadges(state) {
  const badge = document.getElementById("nav-badge-incidents");
  if (!badge) return;
  const open = (state.incidents || []).filter(
    (incident) => incident.status !== "closed" && incident.status !== "completed"
  ).length;
  badge.hidden = open === 0;
  badge.textContent = String(open);
}

/** Pull the shared slices every page needs. Errors degrade visibly, never silently. */
export async function refreshShared() {
  try {
    const summary = await api.summary();
    setState({ summary, dependencies: summary.dependencies || {} });
  } catch (error) {
    recordError(error);
    setState({ connection: error.status === 0 ? "offline" : getState().connection });
    if (error.code === "session_expired") {
      window.location.replace("/index.html");
      return;
    }
    // The summary endpoint touches Postgres, so it fails during exactly the outage the
    // operator most needs to see. /api/health probes dependencies and reports instead of
    // raising, so fall back to it rather than leaving a stale healthy strip on screen.
    await refreshDependencyHealth(error);
  }
}

async function refreshDependencyHealth(cause) {
  try {
    const health = await api.health();
    setState({ dependencies: health.dependencies || {} });
    const unhealthy = Object.entries(health.dependencies || {})
      .filter(([, item]) => item.status !== "healthy")
      .map(([name]) => name);
    toast(
      unhealthy.length
        ? `Degraded: ${unhealthy.join(", ")}. Detection continues where possible.`
        : cause.detail,
      { tone: unhealthy.length ? "warning" : "critical", title: "Dependency health" }
    );
  } catch (healthError) {
    // Both endpoints are unreachable: mark everything unknown rather than showing stale green.
    // Seed from the known dependency set so a cold start during an outage still names them.
    const previous = getState().dependencies || {};
    const names = Object.keys(previous).length ? Object.keys(previous) : Object.keys(DEP_LABELS);
    const unknown = Object.fromEntries(
      names.map((name) => [
        name,
        { status: "down", reason: "health probe unreachable", changed_at: new Date().toISOString() },
      ])
    );
    setState({ dependencies: unknown, connection: "offline" });
    recordError(healthError);
    toast(cause.detail || healthError.detail, {
      tone: "critical",
      title: "API unreachable",
    });
  }
}

export function startLiveFeed({ onMessage, onResync }) {
  feed = new LiveFeed({
    onMessage: (message) => {
      handleShellMessage(message);
      onMessage?.(message);
    },
    onResync: () => {
      refreshShared();
      onResync?.();
    },
  });
  feed.start();
  return feed;
}

function handleShellMessage(message) {
  if (message.type === "degradation" && message.payload) {
    setState({ dependencies: message.payload.dependencies || getState().dependencies });
  }
  if (message.type === "alert" || message.type === "incident_update") {
    const payload = message.payload || {};
    const incidentId = payload.incident_id || payload.alert_id || "incident";
    pushBounded(
      "alerts",
      {
        id: `${incidentId}-${message.timestamp}`,
        tone: payload.severity === "critical" ? "critical" : "warning",
        title:
          message.type === "alert"
            ? `Risk spike alert · ${incidentId}`
            : `Incident update · ${incidentId}`,
        detail: payload.reason || fmt.words(payload.status || "updated"),
        at: message.timestamp,
      },
      40
    );
    if (message.type === "alert") {
      toast(payload.reason || `Spike alert raised for ${incidentId}`, {
        tone: "critical",
        title: "Fraud spike detected",
      });
    }
  }
  if (message.type === "decision_update" && message.payload) {
    pushBounded(
      "alerts",
      {
        id: `decision-${message.payload.decision_id}-${message.timestamp}`,
        tone: "accent",
        title: `Analyst ${fmt.words(message.payload.decision)} · ${message.payload.incident_id}`,
        detail: `Final action: ${fmt.words(message.payload.final_action?.action || "n/a")}`,
        at: message.timestamp,
      },
      40
    );
  }
}

/** Poll interval for REST slices; the websocket covers the fast path. */
export function startPolling(fn, intervalMs = 12000) {
  fn();
  const timer = setInterval(() => {
    if (document.visibilityState === "visible") fn();
  }, intervalMs);
  return () => clearInterval(timer);
}
