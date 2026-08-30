/** Analyst-facing investigation journey built from persisted, timestamped outcomes. */

import { clear, el, emptyState, fmt } from "../ui.js";

const OUTPUT_PHASES = {
  pattern: ["lead_spike_analysis", "segment_interpretation"],
  causes: ["root_cause_hypotheses"],
  verification: ["evidence_verification"],
  impact: ["deterministic_impact"],
  response: ["response_recommendations", "response_policy_shadow"],
};

export function renderAgentTimeline(state) {
  const container = document.getElementById("agent-timeline");
  if (!container) return;
  clear(container);
  if (!state.selectedIncident) {
    container.append(emptyState("Investigation journey is loading"));
    return;
  }

  const phases = buildPhases(state);
  container.append(
    el(
      "ol",
      { class: "phase-timeline", "aria-label": "Investigation phases" },
      phases.map((phase, index) => phaseNode(phase, index))
    )
  );
}

function buildPhases(state) {
  const incident = state.selectedIncident;
  const detector = incident.detector_output || {};
  const segments = incident.segments || [];
  const topSegment = segments[0];
  const hypotheses = outputResult(state, "root_cause_hypotheses", []);
  const verification = outputResult(state, "evidence_verification", {});
  const verdicts = Array.isArray(verification.verdicts) ? verification.verdicts : [];
  const supported = verdicts.filter((item) => item.verdict === "supported");
  const impact = state.review?.impact || outputResult(state, "deterministic_impact", {});
  const responses = responseOptions(state);
  const primaryResponse = state.review?.recommendation || responses[0] || {};
  const gate = state.review?.policy_gate || {};
  const decision = latestDecision(state);

  return [
    {
      title: "Signal detected",
      status: "complete",
      timestamp: incident.detected_at,
      summary: `A sustained concentration of high-risk activity was found in the ${fmt.time(
        incident.window_start
      )}–${fmt.time(incident.window_end)} window.`,
      outcome: `${fmt.multiplier(detector.density_lift)} risk-density lift across ${fmt.count(
        detector.transaction_count
      )} transactions, with ${fmt.money(incident.exposure_estimate_inr)} estimated exposure.`,
      details: facts([
        ["Risk concentration", fmt.multiplier(detector.density_lift)],
        ["Transaction volume", fmt.count(detector.transaction_count)],
        ["Volume context", fmt.multiplier(detector.volume_lift)],
        ["Incident status", fmt.words(incident.status)],
      ]),
    },
    {
      title: "Pattern scoped",
      status: segments.length ? "complete" : "waiting",
      timestamp: phaseTimestamp(state, OUTPUT_PHASES.pattern),
      summary:
        outputResult(state, "lead_spike_analysis", {}).summary ||
        outputResult(state, "segment_interpretation", {}).description ||
        "The investigation compared affected activity and isolated the strongest shared pattern.",
      outcome: topSegment
        ? `The leading pattern covers ${fmt.count(topSegment.support)} transactions at ${fmt.multiplier(
            topSegment.density_lift
          )} the normal risk concentration.`
        : "Pattern analysis is still in progress.",
      details: topSegment
        ? facts([
            ["Leading pattern", describeConditions(topSegment.conditions)],
            ["Affected transactions", fmt.count(topSegment.support)],
            ["Risk-density lift", fmt.multiplier(topSegment.density_lift)],
          ])
        : null,
    },
    {
      title: "Causes assessed",
      status: Array.isArray(hypotheses) ? "complete" : "waiting",
      timestamp: phaseTimestamp(state, OUTPUT_PHASES.causes),
      summary: "Potential explanations were tested against the evidence recorded for this incident.",
      outcome: hypotheses.length
        ? `${fmt.count(hypotheses.length)} evidence-supported explanation(s) remain.`
        : "No supported cause was identified; the signal remains valid but its cause is unresolved.",
      details: hypotheses.length ? hypothesisList(hypotheses) : null,
    },
    {
      title: "Findings verified",
      status: verdicts.length ? "complete" : "waiting",
      timestamp: phaseTimestamp(state, OUTPUT_PHASES.verification),
      summary: "Each material finding was checked against the evidence available for the incident.",
      outcome: verdicts.length
        ? `${fmt.count(supported.length)} of ${fmt.count(verdicts.length)} findings are supported (${fmt.percent(
            verification.grounding_score
          )} evidence coverage).`
        : "Evidence verification is still in progress.",
      details: verdicts.length ? verificationList(verdicts) : null,
    },
    {
      title: "Impact assessed",
      status: impact && Object.keys(impact).length ? "complete" : "waiting",
      timestamp: phaseTimestamp(state, OUTPUT_PHASES.impact),
      summary: "Potential fraud loss and customer friction were estimated for the affected activity.",
      outcome: impact?.fraud_exposure_inr !== undefined
        ? `${fmt.money(impact.fraud_exposure_inr)} fraud exposure and ${fmt.money(
            impact.false_positive_exposure_inr
          )} potential customer-impact exposure.`
        : "Impact assessment is still in progress.",
      details: impact?.fraud_exposure_inr !== undefined
        ? facts([
            ["Fraud exposure", fmt.money(impact.fraud_exposure_inr)],
            ["Customer-impact exposure", fmt.money(impact.false_positive_exposure_inr)],
            ["Transactions assessed", fmt.count(impact.transaction_count)],
          ])
        : null,
    },
    {
      title: "Response recommended",
      status: responses.length || primaryResponse.action ? "complete" : "waiting",
      timestamp: phaseTimestamp(state, OUTPUT_PHASES.response),
      summary:
        primaryResponse.rationale ||
        "Response options were compared against the verified findings and expected customer impact.",
      outcome: primaryResponse.action
        ? `Recommended action: ${fmt.words(primaryResponse.action)}. ${guardrailOutcome(gate)}`
        : "Response evaluation is still in progress.",
      details: responses.length ? responseList(responses.slice(0, 3)) : null,
    },
    decisionPhase(state, decision, primaryResponse),
  ];
}

function decisionPhase(state, decision, recommendation) {
  if (state.review) {
    return {
      title: "Human decision",
      status: "pending",
      timestamp: phaseTimestamp(state, OUTPUT_PHASES.response),
      summary: "An authorized reviewer must approve, modify, reject, or escalate the recommended response.",
      outcome: `Decision pending${recommendation.action ? ` · recommended ${fmt.words(recommendation.action)}` : ""}.`,
      details: el("a", { class: "btn btn--warning btn--sm", href: "#hitl-panel", text: "Go to decision controls" }),
    };
  }
  if (decision) {
    const outcomePending = !decision.outcome;
    return {
      title: "Human decision",
      status: outcomePending ? "pending" : "complete",
      statusText: outcomePending ? "Outcome required" : "Complete",
      timestamp: decision.decided_at,
      summary: `${decision.actor_username || "A reviewer"} recorded ${fmt.words(decision.decision)}.`,
      outcome: `Final action: ${fmt.words(decision.final_action?.action || "recorded")} · ${
        decision.outcome ? fmt.words(decision.outcome.outcome_code) : "outcome pending"
      }.`,
      details: outcomePending
        ? el("a", { class: "btn btn--warning btn--sm", href: "#hitl-panel", text: "Record outcome" })
        : decision.reason_text
          ? el("p", { class: "phase__note", text: decision.reason_text })
          : null,
    };
  }
  return {
    title: "Human decision",
    status: "waiting",
    timestamp: null,
    summary: "Decision controls will appear when the investigation is ready for review.",
    outcome: "Waiting for the preceding phases to complete.",
    details: null,
  };
}

function phaseNode(phase, index) {
  const statusText = phase.statusText || {
    complete: "Complete",
    pending: "Decision required",
    waiting: "In progress",
    attention: "Needs attention",
  }[phase.status];
  return el(
    "li",
    {
      class: "investigation-phase",
      "data-status": phase.status,
      ...(phase.status === "pending" ? { "aria-current": "step" } : {}),
    },
    [
      el("div", { class: "investigation-phase__rail", "aria-hidden": "true" }, [
        el("span", { class: "investigation-phase__marker", text: String(index + 1) }),
      ]),
      el("article", { class: "investigation-phase__card" }, [
        el("header", { class: "investigation-phase__head" }, [
          el("div", {}, [
            el("span", { class: "eyebrow", text: `Phase ${index + 1}` }),
            el("h3", { text: phase.title }),
          ]),
          el("div", { class: "investigation-phase__status" }, [
            phase.timestamp
              ? el("time", { datetime: phase.timestamp, text: fmt.dateTime(phase.timestamp) })
              : null,
            el("span", { class: "badge", "data-tone": statusTone(phase.status) }, [
              el("span", { text: statusText }),
            ]),
          ]),
        ]),
        el("p", { class: "investigation-phase__summary", text: phase.summary }),
        el("div", { class: "investigation-phase__outcome" }, [
          el("span", { text: "Outcome" }),
          el("strong", { text: phase.outcome }),
        ]),
        phase.details ? el("div", { class: "investigation-phase__details" }, [phase.details]) : null,
      ]),
    ]
  );
}

function outputResult(state, name, fallback) {
  const output = (state.investigation?.outputs || []).find((item) => item.agent_name === name);
  return output?.payload?.result ?? fallback;
}

function responseOptions(state) {
  const reviewResponses = state.review?.responses;
  if (Array.isArray(reviewResponses) && reviewResponses.length) return reviewResponses;
  const persisted = outputResult(state, "response_recommendations", []);
  return Array.isArray(persisted) ? persisted : [];
}

function phaseTimestamp(state, names) {
  const entries = state.selectedIncident?.timeline || [];
  const matches = entries.filter(
    (entry) => entry.kind === "agent_output" && names.includes(entry.payload?.agent_name)
  );
  return matches.at(-1)?.timestamp || null;
}

function latestDecision(state) {
  const decisions = state.audit?.decisions || [];
  return decisions.length ? decisions.at(-1) : null;
}

function facts(rows) {
  return el(
    "dl",
    { class: "phase-facts" },
    rows
      .filter(([, value]) => value !== undefined && value !== null && value !== "—")
      .flatMap(([label, value]) => [el("dt", { text: label }), el("dd", { text: value })])
  );
}

function hypothesisList(items) {
  return el(
    "div",
    { class: "phase-findings" },
    items.map((item) =>
      el("div", { class: "phase-finding" }, [
        el("p", { text: item.statement || item.hypothesis || "Supported explanation" }),
        evidenceLinks(item.evidence_ids),
      ])
    )
  );
}

function verificationList(verdicts) {
  return el(
    "div",
    { class: "phase-findings" },
    verdicts.map((item) =>
      el("div", { class: "phase-finding" }, [
        el("span", { class: "badge", "data-tone": item.verdict === "supported" ? "positive" : "warning" }, [
          el("span", { text: fmt.words(item.verdict || "unresolved") }),
        ]),
        evidenceLinks(item.resolved_evidence_ids),
      ])
    )
  );
}

function responseList(items) {
  return el(
    "div",
    { class: "phase-options" },
    items.map((item, index) =>
      el("div", { class: "phase-option" }, [
        el("span", { class: "phase-option__rank", text: String(index + 1) }),
        el("div", {}, [
          el("strong", { text: fmt.words(item.action || "response") }),
          item.rationale ? el("p", { text: item.rationale }) : null,
          evidenceLinks(item.evidence_ids),
        ]),
      ])
    )
  );
}

function evidenceLinks(ids = []) {
  if (!ids.length) return null;
  return el(
    "div",
    { class: "phase-citations", "aria-label": "Supporting evidence" },
    ids.map((id) =>
      el("a", { href: `#${evidenceDomId(id)}`, text: id, title: `View evidence ${id}` })
    )
  );
}

function evidenceDomId(id) {
  return `evidence-${String(id).replace(/[^a-zA-Z0-9_-]/g, "-")}`;
}

function describeConditions(conditions) {
  if (!conditions) return "Affected transaction pattern";
  const rows = Array.isArray(conditions) ? conditions : [conditions];
  return rows
    .flatMap((item) => {
      if (!item || typeof item !== "object") return [String(item)];
      if (Array.isArray(item)) return item.map(String);
      return Object.entries(item).map(([key, value]) => `${fmt.words(key)} ${formatValue(value)}`);
    })
    .join(" · ");
}

function formatValue(value) {
  if (typeof value === "boolean") return value ? "yes" : "no";
  return String(value ?? "");
}

function guardrailOutcome(gate) {
  if (!gate.decision) return "Ready for analyst review.";
  if (gate.decision === "deny") return "Safeguards require escalation before action.";
  if (gate.authorized) return "Safeguards permit an analyst-authorized response.";
  return "Additional review is required before action.";
}

function statusTone(status) {
  return status === "complete" ? "positive" : status === "pending" ? "warning" : "muted";
}
