/** Metric tiles, the risk-density trend, value summary, gauges, and drift rows. */

import { renderSparkline, renderTrend } from "../charts.js";
import { clear, el, emptyState, fmt, setMetric } from "../ui.js";

export function metricTile({ id, label, tone = "default", meta = "", spark = false }) {
  return el("article", { class: "metric", "data-tone": tone }, [
    el("div", { class: "metric__label" }, [
      el("span", { class: "live-dot", "data-tone": tone === "default" ? "idle" : tone, "aria-hidden": "true" }),
      el("span", { text: label }),
    ]),
    el("div", { class: "metric__value", id, text: "—" }),
    el("div", { class: "metric__meta", id: `${id}-meta`, text: meta }),
    spark ? el("canvas", { class: "metric__spark", id: `${id}-spark`, "aria-hidden": "true" }) : null,
  ]);
}

export function renderSummaryTiles(state) {
  const summary = state.summary;
  if (!summary) return;
  setMetric(document.getElementById("m-transactions"), summary.transactions, fmt.count);
  setMetric(document.getElementById("m-highrisk"), summary.high_risk_transactions, fmt.count);
  setMetric(document.getElementById("m-incidents"), summary.active_incidents, fmt.count);
  setMetric(document.getElementById("m-exposure"), summary.estimated_exposure_inr, (v) =>
    fmt.moneyCompact(v)
  );

  const service = summary.service || {};
  const cache = summary.cache || {};
  setText("m-transactions-meta", `${fmt.count(summary.scores)} scored · ${service.score_space || "score space unknown"}`);
  setText(
    "m-highrisk-meta",
    summary.transactions
      ? `${fmt.percent(summary.high_risk_transactions / summary.transactions)} of scored traffic`
      : "awaiting traffic"
  );
  setText("m-incidents-meta", `${fmt.count(summary.incidents)} total · detector ${service.detector_active ? "armed" : "idle"}`);
  setText(
    "m-exposure-meta",
    Number.isFinite(service.baseline_density)
      ? `baseline density ${fmt.ratio(service.baseline_density, 4)}`
      : "baseline warming up"
  );

  const hitRatio =
    (cache.hits ?? 0) + (cache.misses ?? 0) > 0
      ? (cache.hits ?? 0) / ((cache.hits ?? 0) + (cache.misses ?? 0))
      : null;
  setText(
    "cache-summary",
    hitRatio === null
      ? "Cache idle"
      : `Cache hit ratio ${fmt.percent(hitRatio)} · ${fmt.count(cache.fallbacks ?? 0)} local fallbacks`
  );

  if (service.model_degraded) {
    setText("model-note", "Primary fraud model unavailable — conservative fallback scoring active.");
    document.getElementById("model-note")?.setAttribute("data-tone", "warning");
  } else {
    setText("model-note", "Primary calibrated model active.");
  }
}

function setText(id, value) {
  const node = document.getElementById(id);
  if (node) node.textContent = value;
}

/** Aggregate raw transaction points into evenly spaced buckets for the trend. */
export function renderRiskTrend(state) {
  const canvas = document.getElementById("risk-trend");
  const status = document.getElementById("risk-trend-status");
  if (!canvas) return;
  const points = state.timeseries?.points || [];
  if (points.length < 4) {
    if (status) status.textContent = "Waiting for scored transactions to plot a trend.";
    return;
  }
  if (status) status.textContent = `${fmt.count(points.length)} scored transactions in view`;

  const bucketCount = Math.min(60, Math.max(12, Math.floor(points.length / 6)));
  const perBucket = Math.ceil(points.length / bucketCount);
  const density = [];
  const volume = [];
  const labels = [];
  const promoFlags = [];

  for (let i = 0; i < points.length; i += perBucket) {
    const slice = points.slice(i, i + perBucket);
    if (!slice.length) continue;
    density.push(slice.reduce((sum, p) => sum + p.risk_probability, 0) / slice.length);
    volume.push(slice.length);
    labels.push(fmt.time(slice[slice.length - 1].timestamp));
    promoFlags.push(slice.some((p) => p.known_promo_event));
  }

  const firstTs = new Date(points[0].timestamp).getTime();
  const lastTs = new Date(points[points.length - 1].timestamp).getTime();
  const span = Math.max(lastTs - firstTs, 1);
  const indexAt = (iso) => {
    const value = new Date(iso).getTime();
    return ((value - firstTs) / span) * (density.length - 1);
  };

  const shades = (state.timeseries?.windows || [])
    .map((window) => ({
      from: indexAt(window.window_start),
      to: indexAt(window.window_end),
      fill: "rgba(255, 84, 112, 0.13)",
      stroke: "rgba(255, 125, 146, 0.85)",
      label: window.incident_id,
    }))
    .filter((shade) => shade.to > 0 && shade.from < density.length);

  // Promo context is displayed, never used to suppress an alert.
  promoFlags.forEach((flagged, index) => {
    if (!flagged) return;
    shades.push({
      from: index - 0.5,
      to: index + 0.5,
      fill: "rgba(255, 179, 64, 0.1)",
    });
  });

  renderTrend(canvas, {
    series: density,
    secondary: volume,
    labels,
    shades,
    formatValue: (value) => value.toFixed(2),
  });

  const spark = document.getElementById("m-highrisk-spark");
  if (spark) renderSparkline(spark, density.slice(-24), "critical");
  const exposureSpark = document.getElementById("m-exposure-spark");
  if (exposureSpark) renderSparkline(exposureSpark, volume.slice(-24), "info");
}

export function renderValueSummary(state) {
  const container = document.getElementById("value-split");
  if (!container) return;
  const summary = state.summary;
  clear(container);
  if (!summary) {
    container.append(emptyState("No value summary yet"));
    return;
  }
  const cells = [
    {
      label: "Estimated exposure",
      value: fmt.money(summary.estimated_exposure_inr || 0),
      tone: "critical",
    },
    {
      label: "High-risk transactions",
      value: fmt.count(summary.high_risk_transactions || 0),
      tone: "warning",
    },
    { label: "Incidents raised", value: fmt.count(summary.incidents || 0), tone: "default" },
    {
      label: "Transactions scored",
      value: fmt.count(summary.scores || 0),
      tone: "positive",
    },
  ];
  cells.forEach((cell) => {
    container.append(
      el("div", { class: "value-cell", "data-tone": cell.tone }, [
        el("span", { text: cell.label }),
        el("b", { text: cell.value }),
      ])
    );
  });
}

export function gauge({ id, label }) {
  return el("div", { class: "gauge", id: `${id}-wrap` }, [
    el("div", { class: "gauge__ring", id: `${id}-ring` }, [
      el("span", { class: "gauge__value", id, text: "—" }),
    ]),
    el("span", { class: "gauge__label", text: label }),
  ]);
}

export function setGauge(id, ratio) {
  const ring = document.getElementById(`${id}-ring`);
  const value = document.getElementById(id);
  const wrap = document.getElementById(`${id}-wrap`);
  if (!ring || !value) return;
  if (!Number.isFinite(ratio)) {
    value.textContent = "—";
    ring.style.setProperty("--value", "0");
    return;
  }
  const percent = Math.max(0, Math.min(1, ratio)) * 100;
  ring.style.setProperty("--value", percent.toFixed(1));
  value.textContent = `${percent.toFixed(0)}%`;
  if (wrap) wrap.dataset.tone = percent >= 85 ? "accent" : percent >= 60 ? "warning" : "critical";
}

export function renderDrift(state) {
  const container = document.getElementById("drift-rows");
  const note = document.getElementById("drift-note");
  if (!container) return;
  clear(container);
  const drift = state.drift;
  if (!drift || drift.available === false) {
    container.append(
      emptyState(
        "Drift reference unavailable",
        drift?.reason ? fmt.words(drift.reason) : "Start the stream to build a reference"
      )
    );
    return;
  }
  if (note) {
    note.textContent = `PSI alert threshold ${drift.psi_alert_threshold} · rolling window ${fmt.count(
      drift.window_transactions
    )} transactions · advisory only, never auto-retrains`;
  }
  if (!drift.features?.length) {
    container.append(emptyState("No drift measurements yet"));
    return;
  }
  drift.features.forEach((feature) => {
    const ratio = Math.min(feature.psi / Math.max(drift.psi_alert_threshold * 2, 0.001), 1);
    const fill = el("span", { class: "drift-row__fill" });
    container.append(
      el("div", { class: "drift-row" }, [
        el("span", {}, [
          el("span", { text: fmt.words(feature.feature) }),
          feature.reason
            ? el("span", { class: "eyebrow", text: ` ${feature.reason}` })
            : null,
        ]),
        el("span", { class: "drift-row__track" }, [fill]),
        el("span", {
          class: "drift-row__value",
          "data-tone": feature.alert ? "critical" : "default",
          text: feature.psi.toFixed(3),
        }),
      ])
    );
    requestAnimationFrame(() => {
      fill.style.width = `${ratio * 100}%`;
    });
  });
}
