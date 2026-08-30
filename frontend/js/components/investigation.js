/** Analyst-facing incident, pattern, evidence, response, and impact views. */

import { clear, el, emptyState, fmt, severityTone } from "../ui.js";

const EVIDENCE_LABELS = {
  window_statistics: "Incident window",
  segment_statistics: "Affected pattern",
  historical_baseline: "Normal activity baseline",
  similar_incidents: "Related incidents",
  incident_memory: "Prior analyst outcomes",
  impact_estimate: "Impact assessment",
};

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
      el("div", {}, [el("h2", { text: incident.incident_id }), el("p", { text: incident.reason })]),
      el("div", { class: "spacer" }),
      el("span", { class: "badge", "data-tone": incident.severity ? severityTone(incident.severity) : "muted" }, [
        el("span", { text: incident.severity || "assessment pending" }),
      ]),
      el("span", { class: "badge", "data-tone": "muted" }, [el("span", { text: fmt.words(incident.status) })]),
    ])
  );
  container.append(
    el("div", { class: "panel__body" }, [
      el("dl", { class: "kv" }, [
        el("dt", { text: "Detected" }),
        el("dd", { text: fmt.dateTime(incident.detected_at) }),
        el("dt", { text: "Incident window" }),
        el("dd", { text: `${fmt.time(incident.window_start)} → ${fmt.time(incident.window_end)}` }),
        el("dt", { text: "Risk-density lift" }),
        el("dd", { text: fmt.multiplier(detector.density_lift) }),
        el("dt", { text: "Transactions assessed" }),
        el("dd", { text: fmt.count(detector.transaction_count) }),
        el("dt", { text: "Estimated exposure" }),
        el("dd", { text: fmt.money(incident.exposure_estimate_inr) }),
      ]),
      liftBars(detector),
    ])
  );
}

function liftBars(detector) {
  const density = Number(detector.density_lift ?? 0);
  const volume = Number(detector.volume_lift ?? 0);
  const scale = Math.max(density, volume, 1);
  return el(
    "div",
    { class: "lift-bars" },
    [
      { kind: "density", label: "Risk density", value: density },
      { kind: "volume", label: "Volume context", value: volume },
    ].map((row) => {
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
    container.append(emptyState("No affected patterns recorded"));
    return;
  }
  segments.forEach((segment) => {
    container.append(
      el("article", { class: "claim" }, [
        el("div", { class: "claim__head" }, [
          el("span", { class: "badge", "data-tone": "accent" }, [el("span", { text: `Pattern ${segment.rank}` })]),
          el("span", { class: "eyebrow", text: `${fmt.count(segment.support)} transactions` }),
          el("span", { class: "eyebrow", text: `${fmt.multiplier(segment.density_lift)} risk lift` }),
        ]),
        el("p", { class: "claim__text", text: describeConditions(segment.conditions) }),
      ])
    );
  });
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
      el("article", { class: "claim evidence-record", id: evidenceDomId(item.evidence_id) }, [
        el("div", { class: "claim__head" }, [
          el("span", { class: "badge", "data-tone": "info" }, [
            el("span", { text: EVIDENCE_LABELS[item.evidence_type] || "Supporting evidence" }),
          ]),
          item.strength
            ? el("span", { class: "badge", "data-tone": "muted" }, [el("span", { text: fmt.words(item.strength) })])
            : null,
        ]),
        el("p", { class: "claim__text", text: evidenceSummary(item) }),
        el("span", { class: "evidence-reference", text: item.evidence_id }),
      ])
    );
  });
}

export function renderResponseComparison(state) {
  const container = document.getElementById("response-grid");
  if (!container) return;
  clear(container);
  const reviewResponses = state.review?.responses;
  const persisted = outputResult(state, "response_recommendations", []);
  const responses = Array.isArray(reviewResponses) && reviewResponses.length ? reviewResponses : persisted;
  if (!Array.isArray(responses) || !responses.length) {
    container.append(emptyState("No ranked responses yet"));
    return;
  }
  responses.forEach((response) => {
    container.append(
      el("article", { class: "response-card", "data-rank": String(response.rank) }, [
        el("span", { class: "response-card__rank", text: `#${response.rank}` }),
        el("h4", { class: "response-card__action", text: fmt.words(response.action) }),
        el("p", { class: "response-card__rationale", text: response.rationale }),
        evidenceLinks(response.evidence_ids),
      ])
    );
  });
  const note = document.getElementById("response-assumptions");
  if (note) {
    note.textContent = "Options are ordered by fit to verified evidence, expected risk reduction, and customer impact.";
  }
}

export function renderImpact(state) {
  const container = document.getElementById("impact-summary");
  if (!container) return;
  clear(container);
  const impact = state.review?.impact || outputResult(state, "deterministic_impact", null);
  if (!impact) {
    container.append(emptyState("Impact assessment is still in progress"));
    return;
  }
  const values = [
    ["Fraud exposure", impact.fraud_exposure_inr, "critical"],
    ["Customer-impact exposure", impact.false_positive_exposure_inr, "warning"],
    ["Affected legitimate value", impact.affected_legitimate_value_inr, ""],
  ].filter(([, value]) => Number.isFinite(Number(value)));
  container.append(
    el(
      "div",
      { class: "value-split" },
      values.map(([label, value, tone]) =>
        el("div", { class: "value-cell", ...(tone ? { "data-tone": tone } : {}) }, [
          el("span", { text: label }),
          el("b", { text: fmt.money(value) }),
        ])
      )
    )
  );
  container.append(
    el("p", {
      class: "assumption-note",
      text: "Estimates reflect the affected transaction window and recorded operating-cost assumptions.",
    })
  );
}

function outputResult(state, name, fallback) {
  const output = (state.investigation?.outputs || []).find((item) => item.agent_name === name);
  return output?.payload?.result ?? fallback;
}

function evidenceLinks(ids = []) {
  if (!ids.length) return null;
  return el(
    "div",
    { class: "claim__evidence", "aria-label": "Supporting evidence" },
    ids.map((id) => el("a", { class: "evidence-chip", href: `#${evidenceDomId(id)}`, text: id }))
  );
}

function evidenceSummary(item) {
  const summary = item.summary || {};
  switch (item.evidence_type) {
    case "window_statistics":
      return `${fmt.count(summary.transaction_count)} transactions were assessed; risk concentration reached ${fmt.multiplier(
        summary.density_lift
      )}.`;
    case "segment_statistics":
      return `${fmt.count(summary.support)} transactions matched this pattern at ${fmt.multiplier(
        summary.density_lift
      )} the normal risk concentration.`;
    case "historical_baseline":
      return "Observed activity was compared with the established normal-risk baseline.";
    case "similar_incidents":
      return `${fmt.count(summary.count)} related prior incident(s) were reviewed.`;
    case "incident_memory":
      return `${fmt.count(summary.count)} prior analyst outcome(s) were considered.`;
    case "impact_estimate":
      return `The exposure estimate covers ${fmt.count(summary.transaction_count)} affected transactions.`;
    default:
      return "This record supports one or more verified investigation findings.";
  }
}

function describeConditions(conditions) {
  if (!conditions) return "Affected transaction pattern";
  const rows = Array.isArray(conditions) ? conditions : [conditions];
  return rows
    .flatMap((item) => {
      if (!item || typeof item !== "object") return [String(item)];
      if (Array.isArray(item)) return item.map(String);
      return Object.entries(item).map(([key, value]) => `${fmt.words(key)} = ${formatValue(value)}`);
    })
    .join(" · ");
}

function formatValue(value) {
  if (typeof value === "boolean") return value ? "yes" : "no";
  return String(value ?? "");
}

function evidenceDomId(id) {
  return `evidence-${String(id).replace(/[^a-zA-Z0-9_-]/g, "-")}`;
}
