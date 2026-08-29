/** Human-in-the-loop approval panel, policy gate display, and audit trail. */

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

export function renderPolicyGate(state) {
  const container = document.getElementById("policy-gate");
  if (!container) return;
  clear(container);
  const gate = state.review?.policy_gate;
  if (!gate) {
    container.append(emptyState("No policy evaluation yet"));
    return;
  }
  const tone =
    gate.decision === "deny" ? "critical" : gate.decision === "allow" ? "positive" : "warning";
  container.append(
    el("div", { class: "stage__meta" }, [
      el("span", { class: "badge", "data-tone": tone }, [
        el("span", { text: fmt.words(gate.decision || "unknown") }),
      ]),
      el("span", { class: "badge", "data-tone": gate.authorized ? "positive" : "muted" }, [
        el("span", { text: gate.authorized ? "authorized" : "not authorized" }),
      ]),
      gate.rule_id ? el("span", { class: "eyebrow", text: `rule ${gate.rule_id}` }) : null,
      gate.policy_version ? el("span", { class: "eyebrow", text: gate.policy_version }) : null,
    ])
  );
  container.append(el("p", { class: "claim__text", text: gate.reason || "" }));
  container.append(
    el("p", {
      class: "eyebrow",
      text: "A deterministic Python policy authorizes actions. Model output is advisory only.",
    })
  );
}

export function renderReviewPanel(state) {
  const container = document.getElementById("hitl-panel");
  if (!container) return;
  clear(container);
  const review = state.review;
  if (!review) {
    delete container.dataset.pending;
    container.append(
      el("div", { class: "panel__body" }, [
        emptyState("No pending review", "Select an incident awaiting human review"),
      ])
    );
    return;
  }

  const recommendation = review.recommendation || {};
  const allowed = review.allowed_actions || [];
  const durable = review.checkpoint_durable !== false;
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
    placeholder: "Optional analyst note recorded in the audit chain",
    "aria-label": "Analyst note",
  });

  container.append(
    el("div", { class: "panel__head" }, [
      el("div", {}, [
        el("h2", { text: "Human decision required" }),
        el("p", { text: `Recommended: ${fmt.words(recommendation.action || "n/a")}` }),
      ]),
      el("div", { class: "spacer" }),
      el("span", { class: "badge", "data-tone": durable ? "positive" : "warning" }, [
        el("span", { text: durable ? "durable checkpoint" : "checkpoint not durable" }),
      ]),
    ])
  );

  container.append(
    el("div", { class: "panel__body" }, [
      el("p", { class: "claim__text", text: recommendation.rationale || "" }),
      el("div", { class: "stage__meta" }, [
        el("span", { class: "eyebrow", text: "Permitted for your role:" }),
        ...allowed.map((action) =>
          el("span", { class: "badge", "data-tone": "muted" }, [el("span", { text: fmt.words(action) })])
        ),
      ]),
      el("label", { class: "field" }, [el("span", { text: "Modify action" }), select]),
      el("label", { class: "field" }, [el("span", { text: "Reason code" }), reasonSelect]),
      el("label", { class: "field" }, [el("span", { text: "Note" }), notes]),
      !durable
        ? el("p", {
            class: "assumption-note",
            text: "Approval is disabled while durable checkpoints are unavailable. Reject and escalate remain available.",
          })
        : null,
      el("div", { class: "hitl__actions" }, [
        actionButton("approve", "Approve", "btn--primary", durable),
        actionButton("modify", "Modify", "", durable),
        actionButton("reject", "Reject", "btn--danger", true),
        actionButton("escalate", "Escalate", "btn--warning", true),
      ]),
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
    const [review, audit] = await Promise.all([
      api.review(incidentId).catch(() => null),
      api.audit(incidentId).catch(() => null),
    ]);
    setState({ review, audit });
  } catch (error) {
    recordError(error);
    toast(error.detail, { tone: "critical", title: "Decision rejected" });
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

export function renderAuditTrail(state) {
  const container = document.getElementById("audit-trail");
  if (!container) return;
  clear(container);
  const audit = state.audit;
  if (!audit || (!audit.events?.length && !audit.decisions?.length)) {
    container.append(emptyState("No audit records yet"));
    return;
  }
  audit.events.forEach((event) => {
    container.append(
      el("div", { class: "audit-item" }, [
        el("time", { text: fmt.dateTime(event.timestamp) }),
        el("div", {}, [
          el("strong", { text: fmt.words(event.event_type) }),
          el("span", { text: ` · ${event.actor}` }),
          event.payload?.gate_result
            ? el("p", {
                class: "eyebrow",
                text: `gate passed: ${event.payload.gate_result.passed}`,
              })
            : null,
        ]),
      ])
    );
  });
  audit.decisions.forEach((decision) => {
    container.append(
      el("div", { class: "audit-item" }, [
        el("time", { text: fmt.dateTime(decision.decided_at) }),
        el("div", {}, [
          el("strong", { text: `${fmt.words(decision.decision)} by ${decision.actor_username}` }),
          el("span", {
            text: ` · ${fmt.words(decision.final_action?.action || "n/a")} · ${decision.status}`,
          }),
          decision.outcome
            ? el("p", {
                class: "eyebrow",
                text: `outcome ${fmt.words(decision.outcome.outcome_code)}`,
              })
            : el("p", { class: "eyebrow", text: "outcome pending" }),
        ]),
      ])
    );
  });
}
