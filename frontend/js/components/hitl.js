/** Human decision controls, outcome capture, safeguards, and analyst history. */

import { api } from "../api.js";
import { recordError, setState } from "../state.js";
import { clear, el, emptyState, fmt, toast } from "../ui.js";

const REASON_CODES = [
  "confirmed_risk",
  "false_positive",
  "insufficient_evidence",
  "customer_impact",
  "segment_too_broad",
  "policy_violation",
  "needs_specialist",
  "other",
];

const OUTCOME_CODES = [
  ["fraud_confirmed", "Fraud confirmed"],
  ["legitimate", "Legitimate activity"],
  ["mixed", "Mixed result"],
  ["prevented_loss", "Loss prevented"],
  ["no_loss", "No loss observed"],
  ["unknown", "Outcome unknown"],
];

export function renderPolicyGate(state) {
  const container = document.getElementById("policy-gate");
  if (!container) return;
  clear(container);
  const gate = state.review?.policy_gate;
  if (!gate) {
    container.append(emptyState("Safeguard review is not available yet"));
    return;
  }
  const tone = gate.decision === "deny" ? "critical" : gate.authorized ? "positive" : "warning";
  container.append(
    el("div", { class: "stage__meta" }, [
      el("span", { class: "badge", "data-tone": tone }, [
        el("span", { text: gate.authorized ? "Ready for authorized review" : "Additional review required" }),
      ]),
    ])
  );
  container.append(
    el("p", {
      class: "claim__text",
      text:
        gate.reason ||
        (gate.authorized
          ? "The recommended response may proceed to an authorized analyst."
          : "The recommendation cannot proceed without additional review."),
    })
  );
}

export function renderReviewPanel(state) {
  const container = document.getElementById("hitl-panel");
  if (!container) return;
  const review = state.review;
  const decision = latestDecision(state);
  const reviewKey = review
    ? JSON.stringify([
        "review",
        review.incident_id || "pending",
        review.recommendation?.action || "",
        review.allowed_actions || [],
        review.checkpoint_durable !== false,
      ])
    : decision
      ? JSON.stringify([
          "outcome",
          decision.decision_id,
          decision.status,
          decision.outcome || null,
          decision.outcome_recorded_at || null,
        ])
      : "none";
  if (container.dataset.reviewKey === reviewKey) return;
  container.dataset.reviewKey = reviewKey;
  clear(container);

  if (review) {
    renderDecisionForm(container, review);
    return;
  }
  if (decision?.outcome) {
    renderRecordedOutcome(container, decision);
    return;
  }
  if (decision?.status === "completed") {
    renderOutcomeForm(container, decision);
    return;
  }

  delete container.dataset.pending;
  container.append(
    el("div", { class: "panel__body" }, [
      decision
        ? emptyState("Decision processing", "Outcome capture will open when the recorded decision is ready")
        : emptyState("No pending human action", "A decision or outcome form appears here when action is required"),
    ])
  );
}

function renderDecisionForm(container, review) {
  const recommendation = review.recommendation || {};
  const allowed = review.allowed_actions || [];
  const approvalAvailable = review.checkpoint_durable !== false;
  container.dataset.pending = "true";

  const select = el(
    "select",
    { id: "hitl-modified-action", "aria-label": "Modified action" },
    [el("option", { value: "", text: "— keep recommended action —" })].concat(
      allowed.map((action) => el("option", { value: action, text: fmt.words(action) }))
    )
  );
  const reasonSelect = el(
    "select",
    { id: "hitl-reason", "aria-label": "Reason code" },
    REASON_CODES.map((code) => el("option", { value: code, text: fmt.words(code) }))
  );
  const notes = el("textarea", {
    id: "hitl-notes",
    maxlength: "1000",
    placeholder: "Optional analyst note recorded with the decision",
    "aria-label": "Analyst note",
  });

  container.append(
    el("div", { class: "panel__head" }, [
      el("div", {}, [
        el("h2", { text: "Human decision required" }),
        el("p", { text: `Recommended: ${fmt.words(recommendation.action || "n/a")}` }),
      ]),
      el("div", { class: "spacer" }),
      el("span", { class: "badge", "data-tone": approvalAvailable ? "positive" : "warning" }, [
        el("span", { text: approvalAvailable ? "Ready for decision" : "Limited actions available" }),
      ]),
    ])
  );

  container.append(
    el("div", { class: "panel__body" }, [
      el("p", { class: "claim__text", text: recommendation.rationale || "Review the verified findings before acting." }),
      el("div", { class: "stage__meta" }, [
        el("span", { class: "eyebrow", text: "Available actions:" }),
        ...allowed.map((action) =>
          el("span", { class: "badge", "data-tone": "muted" }, [el("span", { text: fmt.words(action) })])
        ),
      ]),
      el("label", { class: "field" }, [el("span", { text: "Modify action" }), select]),
      el("label", { class: "field" }, [el("span", { text: "Reason" }), reasonSelect]),
      el("label", { class: "field" }, [el("span", { text: "Analyst note" }), notes]),
      !approvalAvailable
        ? el("p", {
            class: "assumption-note",
            text: "Approval is temporarily unavailable. Reject and escalate remain available.",
          })
        : null,
      el("div", { class: "hitl__actions" }, [
        actionButton("approve", "Approve", "btn--primary", approvalAvailable),
        actionButton("modify", "Modify", "", approvalAvailable),
        actionButton("reject", "Reject", "btn--danger", true),
        actionButton("escalate", "Escalate", "btn--warning", true),
      ]),
    ])
  );
}

function renderOutcomeForm(container, decision) {
  const finalAction = decision.final_action?.action || "recorded action";
  const escalated = finalAction === "human_escalation";
  container.dataset.pending = "true";

  const outcomeSelect = el("select", { id: "hitl-outcome-code", "aria-label": "Observed outcome", required: true }, [
    el("option", { value: "", text: "— choose the observed outcome —" }),
    ...OUTCOME_CODES.map(([value, label]) => el("option", { value, text: label })),
  ]);
  const fraudLoss = el("input", {
    id: "hitl-fraud-loss",
    type: "number",
    min: "0",
    step: "0.01",
    inputmode: "decimal",
    placeholder: "Enter 0 if none",
    required: true,
    "aria-label": "Fraud loss or prevented amount in rupees",
  });
  const falsePositiveCost = el("input", {
    id: "hitl-fp-cost",
    type: "number",
    min: "0",
    step: "0.01",
    inputmode: "decimal",
    placeholder: "Enter 0 if none",
    required: true,
    "aria-label": "False-positive cost in rupees",
  });
  const notes = el("textarea", {
    id: "hitl-outcome-notes",
    maxlength: "1000",
    placeholder: "Optional operational context or downstream review result",
    "aria-label": "Outcome notes",
  });

  container.append(
    el("div", { class: "panel__head" }, [
      el("div", {}, [
        el("h2", { text: "Record outcome" }),
        el("p", { text: `Decision saved · final action: ${fmt.words(finalAction)}` }),
      ]),
      el("div", { class: "spacer" }),
      el("span", { class: "badge", "data-tone": "warning" }, [el("span", { text: "Outcome required" })]),
    ])
  );
  container.append(
    el("div", { class: "panel__body" }, [
      el("p", {
        class: "claim__text",
        text: escalated
          ? "Escalated for human handling. Record the outcome after the downstream review resolves."
          : "Record the observed result after the approved response has taken effect.",
      }),
      el("p", {
        class: "assumption-note",
        text: "Use verified operational results only. Recording an outcome completes the incident and cannot be changed.",
      }),
      el("label", { class: "field" }, [el("span", { text: "Observed outcome" }), outcomeSelect]),
      el("div", { class: "outcome-fields" }, [
        el("label", { class: "field" }, [
          el("span", { text: "Fraud loss / prevented amount (₹)" }),
          fraudLoss,
        ]),
        el("label", { class: "field" }, [
          el("span", { text: "False-positive cost (₹)" }),
          falsePositiveCost,
        ]),
      ]),
      el("p", {
        class: "eyebrow",
        text: "For Loss prevented, enter the verified prevented amount in the fraud loss / prevented amount field.",
      }),
      el("label", { class: "field" }, [el("span", { text: "Outcome notes" }), notes]),
      el("div", { class: "hitl__actions" }, [
        el("button", {
          class: "btn btn--primary",
          type: "button",
          text: "Record outcome and complete incident",
          onClick: (event) => submitOutcome(decision, event.currentTarget),
        }),
      ]),
    ])
  );
}

function renderRecordedOutcome(container, decision) {
  const outcome = decision.outcome || {};
  delete container.dataset.pending;
  container.append(
    el("div", { class: "panel__head" }, [
      el("div", {}, [
        el("h2", { text: "Outcome recorded" }),
        el("p", { text: `Final action: ${fmt.words(decision.final_action?.action || "recorded")}` }),
      ]),
      el("div", { class: "spacer" }),
      el("span", { class: "badge", "data-tone": "positive" }, [el("span", { text: "Incident completed" })]),
    ])
  );
  container.append(
    el("div", { class: "panel__body" }, [
      el("p", { class: "claim__text", text: "The verified operational result is stored with this decision." }),
      el("dl", { class: "kv outcome-summary" }, [
        el("dt", { text: "Observed outcome" }),
        el("dd", { text: fmt.words(outcome.outcome_code) }),
        el("dt", { text: "Fraud loss / prevented amount" }),
        el("dd", { text: fmt.money(Number(outcome.fraud_loss_inr), { precise: true }) }),
        el("dt", { text: "False-positive cost" }),
        el("dd", { text: fmt.money(Number(outcome.false_positive_cost_inr), { precise: true }) }),
        decision.outcome_recorded_at ? el("dt", { text: "Recorded" }) : null,
        decision.outcome_recorded_at
          ? el("dd", { text: fmt.dateTime(decision.outcome_recorded_at) })
          : null,
      ]),
      outcome.notes ? el("p", { class: "assumption-note", text: outcome.notes }) : null,
      el("p", { class: "eyebrow", text: "Recorded outcomes are immutable." }),
    ])
  );
}

function actionButton(decision, label, variant, enabled) {
  return el("button", {
    class: `btn ${variant}`.trim(),
    type: "button",
    text: label,
    disabled: !enabled,
    onClick: (event) => submitDecision(decision, event.currentTarget),
  });
}

async function submitDecision(decision, button) {
  const incidentId = new URLSearchParams(window.location.search).get("incident");
  if (!incidentId) {
    toast("No incident selected", { tone: "warning" });
    return;
  }
  const modified = document.getElementById("hitl-modified-action")?.value || null;
  const reasonCode = document.getElementById("hitl-reason")?.value || "other";
  const note = document.getElementById("hitl-notes")?.value?.trim() || null;
  if (reasonCode === "other" && !note) {
    toast("The 'other' reason requires a note", { tone: "warning", title: "Reason required" });
    return;
  }

  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Submitting…";
  try {
    const result = await api.decide(incidentId, {
      decision,
      reason_code: reasonCode,
      reason_text: note,
      modified_action: decision === "modify" ? modified : null,
    });
    toast(`Decision recorded: ${fmt.words(result.decision)} → ${fmt.words(result.final_action?.action)}`, {
      tone: "accent",
      title: "Human review complete",
    });
    const [incident, review, audit] = await Promise.all([
      api.incident(incidentId).catch(() => null),
      api.review(incidentId).catch(() => null),
      api.audit(incidentId).catch(() => null),
    ]);
    setState({ ...(incident ? { selectedIncident: incident } : {}), review, audit });
  } catch (error) {
    recordError(error);
    toast(error.detail, { tone: "critical", title: "Decision rejected" });
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function submitOutcome(decision, button) {
  const incidentId = decision.incident_id || new URLSearchParams(window.location.search).get("incident");
  const outcomeCode = document.getElementById("hitl-outcome-code")?.value || "";
  const fraudLossRaw = document.getElementById("hitl-fraud-loss")?.value?.trim() || "";
  const falsePositiveCostRaw = document.getElementById("hitl-fp-cost")?.value?.trim() || "";
  const notes = document.getElementById("hitl-outcome-notes")?.value?.trim() || null;

  if (!outcomeCode) {
    toast("Choose the observed outcome", { tone: "warning", title: "Outcome required" });
    return;
  }
  const fraudLoss = Number(fraudLossRaw);
  const falsePositiveCost = Number(falsePositiveCostRaw);
  if (!fraudLossRaw || !Number.isFinite(fraudLoss) || fraudLoss < 0) {
    toast("Enter a non-negative fraud loss or prevented amount", { tone: "warning", title: "Amount required" });
    return;
  }
  if (!falsePositiveCostRaw || !Number.isFinite(falsePositiveCost) || falsePositiveCost < 0) {
    toast("Enter a non-negative false-positive cost", { tone: "warning", title: "Amount required" });
    return;
  }

  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Recording…";
  try {
    await api.outcome(decision.decision_id, {
      outcome_code: outcomeCode,
      fraud_loss_inr: fraudLoss,
      false_positive_cost_inr: falsePositiveCost,
      notes,
    });
    const [incident, audit] = await Promise.all([
      api.incident(incidentId).catch(() => null),
      api.audit(incidentId).catch(() => null),
    ]);
    setState({ ...(incident ? { selectedIncident: incident } : {}), review: null, audit });
    toast("Outcome recorded and incident completed", { tone: "accent", title: "Lifecycle complete" });
  } catch (error) {
    recordError(error);
    toast(error.detail, { tone: "critical", title: "Outcome not recorded" });
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

export function renderAuditTrail(state) {
  const container = document.getElementById("audit-trail");
  if (!container) return;
  clear(container);
  const decisions = state.audit?.decisions || [];
  if (!decisions.length) {
    container.append(emptyState("No analyst decision recorded yet"));
    return;
  }
  decisions.forEach((decision) => {
    const outcome = decision.outcome;
    container.append(
      el("article", { class: "audit-item" }, [
        el("time", { datetime: decision.decided_at, text: fmt.dateTime(decision.decided_at) }),
        el("div", {}, [
          el("strong", { text: `${fmt.words(decision.decision)} by ${decision.actor_username}` }),
          el("p", { text: `Final action: ${fmt.words(decision.final_action?.action || "recorded")}` }),
          decision.reason_text ? el("p", { class: "eyebrow", text: decision.reason_text }) : null,
          el("span", {
            class: "badge",
            "data-tone": outcome ? "positive" : "muted",
          }, [
            el("span", {
              text: outcome ? fmt.words(outcome.outcome_code) : "Outcome pending",
            }),
          ]),
          outcome
            ? el("p", {
                class: "eyebrow",
                text: `Fraud loss / prevented: ${fmt.money(Number(outcome.fraud_loss_inr), {
                  precise: true,
                })} · False-positive cost: ${fmt.money(Number(outcome.false_positive_cost_inr), {
                  precise: true,
                })}`,
              })
            : null,
        ]),
      ])
    );
  });
}

function latestDecision(state) {
  const decisions = state.audit?.decisions || [];
  return decisions.length ? decisions.at(-1) : null;
}
