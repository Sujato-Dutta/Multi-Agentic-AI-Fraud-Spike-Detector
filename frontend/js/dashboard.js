/** Risk dashboard: live traffic, risk-density trend, incident cards, alerts, drift. */

import { api } from "./api.js";
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
import { clear, el, emptyState, fmt, severityTone } from "./ui.js";

bootstrap({
  title: "Risk Operations",
  subtitle: "Risk-density spike detection · volume is context only",
  build,
  load,
  onLive: handleLive,
});

function build(main) {
  main.append(
    el("div", { class: "page-grid" }, [
      el("section", { class: "col-12 metric-grid stagger", "aria-label": "Key metrics" }, [
        metricTile({ id: "m-transactions", label: "Transactions scored", tone: "accent" }),
        metricTile({ id: "m-highrisk", label: "High-risk transactions", tone: "critical", spark: true }),
        metricTile({ id: "m-incidents", label: "Active incidents", tone: "warning" }),
        metricTile({ id: "m-exposure", label: "Estimated exposure", tone: "ai", spark: true }),
      ]),

      el("section", { class: "col-8 panel anim-rise" }, [
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

      el("section", { class: "col-4 panel anim-rise" }, [
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

      el("section", { class: "col-8 panel anim-rise" }, [
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

      el("section", { class: "col-4 panel anim-rise" }, [
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
  subscribe(["alerts", "dependencies"], renderAlertCenter);
  subscribe(["drift"], renderDrift);
}

async function load() {
  const [timeseries, incidents, drift] = await Promise.all([
    api.timeseries(60),
    api.incidents({ limit: 12 }),
    api.drift().catch(() => null),
  ]);
  setState({ timeseries, incidents: incidents.items || [], drift });
}

function handleLive(message) {
  if (message.type === "txn") appendLiveTransaction(message.payload);
  if (message.type === "alert" || message.type === "incident_update") {
    api
      .incidents({ limit: 12 })
      .then((result) => setState({ incidents: result.items || [] }))
      .catch(() => {});
  }
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
