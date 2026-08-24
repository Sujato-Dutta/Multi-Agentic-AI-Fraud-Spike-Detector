/** Incident header, segment evidence, and the counterfactual response comparison. */

import { clear, el, emptyState, fmt, severityTone } from "../ui.js";

export function renderIncidentHeader(state) {
  const container = document.getElementById("incident-header");
  if (!container) return;
  clear(container);
  const incident = state.selectedIncident;
  if (!incident) {
    container.append(emptyState("Select an incident to investigate"));
    return;
  }
  const detector = incident.detector_output || {};
  container.append(
    el("div", { class: "panel__head" }, [
      el("div", {}, [
        el("h2", { text: incident.incident_id }),
        el("p", { text: incident.reason }),
      ]),
      el("div", { class: "spacer" }),
      el("span", { class: "badge", "data-tone": severityTone(incident.severity) }, [
        el("span", { text: incident.severity || "unrated" }),
      ]),
      el("span", { class: "badge", "data-tone": "muted" }, [
        el("span", { text: fmt.words(incident.status) }),
      ]),
    ])
  );
  container.append(
    el("div", { class: "panel__body" }, [
      el("dl", { class: "kv" }, [
        el("dt", { text: "Detected at" }),
        el("dd", { text: fmt.dateTime(incident.detected_at) }),
        el("dt", { text: "Window" }),
        el("dd", { text: `${fmt.time(incident.window_start)} → ${fmt.time(incident.window_end)}` }),
        el("dt", { text: "Risk density lift" }),
        el("dd", { text: fmt.multiplier(detector.density_lift) }),
        el("dt", { text: "Volume lift (context only)" }),
        el("dd", { text: fmt.multiplier(detector.volume_lift) }),
        el("dt", { text: "Transactions in window" }),
        el("dd", { text: fmt.count(detector.transaction_count) }),
        el("dt", { text: "Significance (p)" }),
        el("dd", { text: Number.isFinite(detector.p_value) ? detector.p_value.toExponential(2) : "—" }),
        el("dt", { text: "Estimated exposure" }),
        el("dd", { text: fmt.money(incident.exposure_estimate_inr) }),
        el("dt", { text: "Promo share in window" }),
        el("dd", { text: fmt.percent(detector.promo_share ?? 0) }),
      ]),
      liftBars(detector),
      detector.promo_share > 0
        ? el("p", {
            class: "assumption-note",
            text:
              "Promotion context raises the required density lift; it can never veto a qualifying spike.",
          })
        : null,
    ])
  );
}

function liftBars(detector) {
  const density = Number(detector.density_lift ?? 0);
  const volume = Number(detector.volume_lift ?? 0);
  const scale = Math.max(density, volume, 1);
  const rows = [
    { kind: "density", label: "Risk density", value: density },
    { kind: "volume", label: "Volume", value: volume },
  ];
  return el(
    "div",
    { class: "lift-bars" },
    rows.map((row) => {
      const fill = el("span", { class: "lift-bar__fill" });
      requestAnimationFrame(() => {
        fill.style.width = `${(row.value / scale) * 100}%`;
      });
      return el("div", { class: "lift-bar", "data-kind": row.kind }, [
        el("span", { text: row.label }),
        el("span", { class: "lift-bar__track" }, [fill]),
        el("b", { text: fmt.multiplier(row.value) }),
      ]);
    })
  );
}

export function renderSegments(state) {
  const container = document.getElementById("segment-list");
  if (!container) return;
  clear(container);
  const segments = state.selectedIncident?.segments || [];
  if (!segments.length) {
    container.append(emptyState("No ranked segments recorded"));
    return;
  }
  segments.forEach((segment) => {
    container.append(
      el("div", { class: "claim" }, [
        el("div", { class: "claim__head" }, [
          el("span", { class: "badge", "data-tone": "accent" }, [el("span", { text: `rank ${segment.rank}` })]),
          el("span", { class: "eyebrow", text: `support ${fmt.count(segment.support)}` }),
          el("span", { class: "eyebrow", text: `lift ${fmt.multiplier(segment.density_lift)}` }),
        ]),
        el("p", { class: "claim__text", text: describeConditions(segment.conditions) }),
        el("dl", { class: "kv" }, [
          el("dt", { text: "Risk density" }),
          el("dd", { text: fmt.ratio(segment.risk_density, 4) }),
          el("dt", { text: "Baseline density" }),
          el("dd", { text: fmt.ratio(segment.baseline_risk_density, 4) }),
          el("dt", { text: "Excess risk contribution" }),
          el("dd", { text: fmt.ratio(segment.excess_risk_contribution, 4) }),
          el("dt", { text: "p-value" }),
          el("dd", { text: Number.isFinite(segment.p_value) ? segment.p_value.toExponential(2) : "—" }),
        ]),
      ])
    );
  });
}

function describeConditions(conditions) {
  if (!conditions) return "—";
  if (Array.isArray(conditions)) {
    return conditions
      .map((item) =>
        typeof item === "object" && item !== null
          ? `${item.feature ?? item.dimension ?? "field"} = ${item.value ?? item.bin ?? ""}`
          : String(item)
      )
      .join("  ·  ");
  }
  if (typeof conditions === "object") {
    return Object.entries(conditions)
      .map(([key, value]) => `${key} = ${value}`)
      .join("  ·  ");
  }
  return String(conditions);
}

export function renderEvidence(state) {
  const container = document.getElementById("evidence-list");
  if (!container) return;
  clear(container);
  const evidence = state.investigation?.evidence || [];
  if (!evidence.length) {
    container.append(emptyState("No evidence records"));
    return;
  }
  evidence.forEach((item) => {
    container.append(
      el("div", { class: "claim" }, [
        el("div", { class: "claim__head" }, [
          el("span", { class: "evidence-chip", text: item.evidence_id }),
          el("span", { class: "badge", "data-tone": "muted" }, [
            el("span", { text: fmt.words(item.evidence_type) }),
          ]),
          item.strength
            ? el("span", { class: "badge", "data-tone": "info" }, [el("span", { text: item.strength })])
            : null,
        ]),
        el("p", { class: "claim__text", text: item.source }),
        el("p", { class: "eyebrow", text: summarisePayload(item.payload) }),
      ])
    );
  });
}

function summarisePayload(payload) {
  if (!payload || typeof payload !== "object") return "";
  return Object.entries(payload)
    .slice(0, 4)
    .map(([key, value]) => {
      if (typeof value === "number") return `${key} ${Number(value.toFixed(4))}`;
      if (typeof value === "object") return `${key} …`;
      return `${key} ${value}`;
    })
    .join("  ·  ");
}

/** Counterfactual response cards; assumptions are rendered next to every number. */
export function renderResponseComparison(state) {
  const container = document.getElementById("response-grid");
  if (!container) return;
  clear(container);
  const responses = state.review?.responses || [];
  if (!responses.length) {
    container.append(emptyState("No ranked responses yet"));
    return;
  }
  responses.forEach((response) => {
    container.append(
      el("article", { class: "response-card", "data-rank": String(response.rank) }, [
        el("span", { class: "response-card__rank", text: `#${response.rank}` }),
        el("h4", { class: "response-card__action", text: fmt.words(response.action) }),
        el("p", { class: "response-card__rationale", text: response.rationale }),
        el("div", { class: "stage__meta" }, [
          el("span", { class: "badge", "data-tone": "warning" }, [
            el("span", { text: "human review required" }),
          ]),
        ]),
        el(
          "div",
          { class: "claim__evidence" },
          (response.evidence_ids || []).map((id) => el("span", { class: "evidence-chip", text: id }))
        ),
      ])
    );
  });
  const note = document.getElementById("response-assumptions");
  if (note) {
    note.textContent =
      "Ranking comes from the production response policy, not the language model. Counterfactual effects use the versioned action-effects assumptions, not observed treatment effects.";
  }
}

export function renderImpact(state) {
  const container = document.getElementById("impact-summary");
  if (!container) return;
  clear(container);
  const impact = state.review?.impact;
  if (!impact) {
    container.append(emptyState("No deterministic impact recorded"));
    return;
  }
  container.append(
    el("div", { class: "value-split" }, [
      el("div", { class: "value-cell", "data-tone": "critical" }, [
        el("span", { text: "Fraud exposure" }),
        el("b", { text: fmt.money(impact.fraud_exposure_inr) }),
      ]),
      el("div", { class: "value-cell", "data-tone": "warning" }, [
        el("span", { text: "False-positive exposure" }),
        el("b", { text: fmt.money(impact.false_positive_exposure_inr) }),
      ]),
      el("div", { class: "value-cell" }, [
        el("span", { text: "Method" }),
        el("b", { text: "deterministic" }),
      ]),
    ])
  );
  container.append(
    el("p", {
      class: "assumption-note",
      text: "Every financial figure is computed server-side from dataset cost fields. No language model produces a number in this system.",
    })
  );
}
