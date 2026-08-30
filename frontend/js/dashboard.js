/** Risk dashboard: live traffic, risk-density trend, incident cards, alerts, drift. */

import { api, session } from "./api.js";
import { bootstrap } from "./app.js";
import { renderAlertCenter } from "./components/alerts.js";
import {
  metricTile,
  renderDrift,
  renderRiskTrend,
  renderSummaryTiles,
  renderValueSummary,
} from "./components/metrics.js";
import { appendLiveTransaction, seedTicker, tickerShell } from "./components/transactions.js";
import { setState, subscribe } from "./state.js";
import { clear, el, emptyState, fmt, severityTone, toast } from "./ui.js";

let demoPollTimer = null;
let incidentRefreshSequence = 0;
const demoControl =
  session.role === "admin"
    ? el("div", { class: "demo-stream-control" }, [
        el("button", {
          class: "btn btn--primary btn--sm",
          id: "demo-stream-button",
          type: "button",
          text: "Start demo stream",
          onClick: startDemoStream,
        }),
        el("span", { class: "demo-stream-status", id: "demo-stream-status", text: "Ready" }),
      ])
    : null;

bootstrap({
  title: "Risk Operations",
  subtitle: "Risk-density spike detection · volume is context only",
  actions: demoControl ? [demoControl] : [],
  build,
  load,
  onLive: handleLive,
});

function build(main) {
  main.append(
    el("div", { class: "page-grid" }, [
      el("section", {
        class: "col-12 spike-review-banner",
        id: "pending-review-warning",
        role: "alert",
        "aria-live": "assertive",
        "aria-atomic": "true",
        hidden: true,
      }),
      el("section", { class: "col-12 metric-grid stagger", "aria-label": "Key metrics" }, [
        metricTile({ id: "m-transactions", label: "Transactions scored", tone: "accent" }),
        metricTile({ id: "m-highrisk", label: "High-risk transactions", tone: "critical", spark: true }),
        metricTile({ id: "m-incidents", label: "Active incidents", tone: "warning" }),
        metricTile({ id: "m-exposure", label: "Estimated exposure", tone: "ai", spark: true }),
      ]),

      el("section", { class: "col-12 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [
            el("h2", { text: "Active incidents" }),
            el("p", { text: "Ranked by detection time" }),
          ]),
          el("div", { class: "spacer" }),
          el("a", { class: "btn btn--ghost btn--sm", href: "/pages/incidents.html", text: "All incidents" }),
        ]),
        el("div", { class: "panel__body" }, [
          el("div", { class: "response-grid", id: "incident-cards" }),
        ]),
      ]),

      el("section", { class: "col-12 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [
            el("h2", { text: "Risk density trend" }),
            el("p", { id: "risk-trend-status", text: "Loading scored transactions…" }),
          ]),
          el("div", { class: "spacer" }),
          el("span", { class: "badge", "data-tone": "accent" }, [
            el("span", { class: "live-dot", "aria-hidden": "true" }),
            el("span", { text: "density triggers alerts" }),
          ]),
        ]),
        el("div", { class: "chart-legend" }, [
          el("span", {}, [el("i", { "data-series": "density" }), el("span", { text: "Mean risk density" })]),
          el("span", {}, [el("i", { "data-series": "volume" }), el("span", { text: "Volume (context)" })]),
          el("span", {}, [el("i", { "data-series": "spike" }), el("span", { text: "Incident window" })]),
          el("span", {}, [el("i", { "data-series": "promo" }), el("span", { text: "Promotion context" })]),
        ]),
        el("div", { class: "panel__body" }, [
          el("div", { class: "chart-shell chart-shell--tall" }, [
            el("canvas", { id: "risk-trend", role: "img", "aria-label": "Risk density trend with shaded incident windows" }),
          ]),
        ]),
        el("div", { class: "panel__foot" }, [
          el("span", {
            id: "model-note",
            text: "Model status loading…",
          }),
        ]),
      ]),

      el("section", { class: "col-6 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [
            el("h2", { text: "Live transactions" }),
            el("p", { text: "Newest first" }),
          ]),
          el("div", { class: "spacer" }),
          el("span", { class: "live-dot", "aria-hidden": "true" }),
        ]),
        el("div", { class: "panel__body panel__body--tight" }, [tickerShell()]),
        el("div", { class: "panel__foot" }, [el("span", { id: "cache-summary", text: "Cache idle" })]),
      ]),

      el("section", { class: "col-6 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [el("h2", { text: "Alert center" }), el("p", { text: "Live operational notices" })]),
        ]),
        el("div", { class: "panel__body panel__body--scroll", id: "alert-list" }),
      ]),

      el("section", { class: "col-6 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [
            el("h2", { text: "Exposure and cost summary" }),
            el("p", { text: "Deterministic values from dataset cost fields" }),
          ]),
        ]),
        el("div", { class: "panel__body" }, [el("div", { class: "value-split", id: "value-split" })]),
        el("div", { class: "panel__foot" }, [
          el("span", {
            text: "Operational costs (analyst review, customer friction, delay) are documented assumptions, not measured values.",
          }),
        ]),
      ]),

      el("section", { class: "col-6 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [
            el("h2", { text: "Model drift" }),
            el("p", { id: "drift-note", text: "Advisory only" }),
          ]),
        ]),
        el("div", { class: "panel__body", id: "drift-rows" }),
      ]),
    ])
  );

  subscribe(["summary"], (state) => {
    renderSummaryTiles(state);
    renderValueSummary(state);
  });
  subscribe(["timeseries"], (state) => {
    renderRiskTrend(state);
    seedTicker(state);
  });
  subscribe(["incidents"], renderIncidentCards);
  subscribe(["pendingReviews"], renderPendingReview);
  subscribe(["demoStream"], renderDemoStream);
  subscribe(["alerts", "dependencies"], renderAlertCenter);
  subscribe(["drift"], renderDrift);
}

async function fetchIncidentViews() {
  const sequence = ++incidentRefreshSequence;
  const [recent, pending] = await Promise.all([
    api.incidents({ limit: 12 }),
    api.incidents({ status: "awaiting_human_review", limit: 12 }),
  ]);
  if (sequence !== incidentRefreshSequence) return null;
  return {
    incidents: recent.items || [],
    pendingReviews: {
      items: pending.items || [],
      count: Number.isFinite(pending.count) ? pending.count : (pending.items || []).length,
    },
  };
}

async function load() {
  const [timeseries, incidentViews, drift, demoStream] = await Promise.all([
    api.timeseries(60),
    fetchIncidentViews(),
    api.drift().catch(() => null),
    session.role === "admin"
      ? api.demoStream().catch((error) => {
          if (error.status === 404 || error.status === 403) return { state: "unavailable" };
          throw error;
        })
      : null,
  ]);
  setState({ timeseries, ...(incidentViews || {}), drift, demoStream });
  if (["queued", "running"].includes(demoStream?.state)) scheduleDemoPoll();
}

function handleLive(message) {
  if (message.type === "txn") appendLiveTransaction(message.payload);
  if (["alert", "incident_update", "decision_update"].includes(message.type)) {
    fetchIncidentViews()
      .then((views) => {
        if (views) setState(views);
      })
      .catch(() => {});
  }
}

async function startDemoStream(event) {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const result = await api.startDemoStream();
    setState({ demoStream: result });
    toast("VAL_S1 is streaming through Redpanda", {
      tone: "accent",
      title: "Demo stream started",
    });
    scheduleDemoPoll();
  } catch (error) {
    toast(error.detail || "Could not start the demo stream", {
      tone: "critical",
      title: "Stream not started",
    });
    button.disabled = false;
  }
}

function scheduleDemoPoll() {
  clearTimeout(demoPollTimer);
  demoPollTimer = window.setTimeout(async () => {
    try {
      const result = await api.demoStream();
      setState({ demoStream: result });
      if (["queued", "running"].includes(result.state)) scheduleDemoPoll();
    } catch {
      clearTimeout(demoPollTimer);
    }
  }, 1000);
}

function renderDemoStream(state) {
  const control = document.querySelector(".demo-stream-control");
  const button = document.getElementById("demo-stream-button");
  const status = document.getElementById("demo-stream-status");
  if (!control || !button || !status) return;
  const stream = state.demoStream;
  if (stream?.state === "unavailable") {
    control.hidden = true;
    return;
  }
  control.hidden = false;
  const active = ["queued", "running"].includes(stream?.state);
  const completed = stream?.state === "completed";
  button.disabled = active;
  button.textContent =
    stream?.state === "failed"
      ? "Retry after reset"
      : completed
        ? "Replay demo stream"
        : active
          ? "Streaming VAL_S1…"
          : "Start demo stream";
  status.textContent = active
    ? `${fmt.count(stream.published)} / ${fmt.count(stream.total || 0)} · ${stream.percent || 0}%`
    : completed
      ? "Run reset_demo.py before replay"
      : stream?.state === "failed"
        ? `${stream.error?.detail || "Stream failed"} · reset required`
        : "Known fraud spike · 600×";
}

function renderPendingReview(state) {
  const banner = document.getElementById("pending-review-warning");
  if (!banner) return;
  const pending = state.pendingReviews?.items || [];
  const pendingCount = Number.isFinite(state.pendingReviews?.count)
    ? state.pendingReviews.count
    : pending.length;
  clear(banner);
  if (!pending.length) {
    banner.hidden = true;
    return;
  }
  const incident = pending[0];
  const detector = incident.detector_output || {};
  banner.append(
    el("div", { class: "spike-review-banner__signal", "aria-hidden": "true", text: "!" }),
    el("div", { class: "spike-review-banner__copy" }, [
      el("span", { class: "eyebrow", text: "Fraud spike detected" }),
      el("h2", { text: "Human decision required" }),
      el("p", {
        text: `${incident.incident_id} · ${incident.reason} · ${fmt.multiplier(detector.density_lift)} density lift · ${fmt.moneyCompact(incident.exposure_estimate_inr)} exposure`,
      }),
    ]),
    ...(pendingCount > 1
      ? [
          el("span", { class: "badge", "data-tone": "critical" }, [
            el("span", { text: `${fmt.count(pendingCount)} pending` }),
          ]),
        ]
      : []),
    el("a", {
      class: "btn btn--danger",
      href: `/pages/investigation.html?incident=${encodeURIComponent(incident.incident_id)}#hitl-panel`,
      "aria-label": `Review decision for incident ${incident.incident_id}`,
      text: "Review decision now",
    })
  );
  banner.hidden = false;
}

function renderIncidentCards(state) {
  const container = document.getElementById("incident-cards");
  if (!container) return;
  clear(container);
  const incidents = state.incidents || [];
  if (!incidents.length) {
    container.append(emptyState("No incidents raised", "Benign surges do not create incidents"));
    return;
  }
  incidents.slice(0, 6).forEach((incident) => {
    const detector = incident.detector_output || {};
    container.append(
      el(
        "a",
        {
          class: "incident-card",
          "data-severity": incident.severity || "low",
          href: `/pages/investigation.html?incident=${encodeURIComponent(incident.incident_id)}`,
        },
        [
          el("div", { class: "incident-card__top" }, [
            el("span", { class: "incident-card__id", text: incident.incident_id }),
            el("span", { class: "badge", "data-tone": severityTone(incident.severity) }, [
              el("span", { text: incident.severity || "unrated" }),
            ]),
            el("span", { class: "badge", "data-tone": "muted" }, [
              el("span", { text: fmt.words(incident.status) }),
            ]),
          ]),
          el("p", { class: "incident-card__reason", text: incident.reason }),
          el("div", { class: "incident-card__stats" }, [
            el("div", {}, [el("span", { text: "Density lift" }), el("b", { text: fmt.multiplier(detector.density_lift) })]),
            el("div", {}, [el("span", { text: "Volume lift" }), el("b", { text: fmt.multiplier(detector.volume_lift) })]),
            el("div", {}, [
              el("span", { text: "Exposure" }),
              el("b", { text: fmt.moneyCompact(incident.exposure_estimate_inr) }),
            ]),
          ]),
        ]
      )
    );
  });
}
