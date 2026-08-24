/** Models and policies: registry, drift, and production-vs-candidate with human gating. */

import { api, session } from "./api.js";
import { bootstrap } from "./app.js";
import { renderDrift } from "./components/metrics.js";
import { recordError, setState, subscribe } from "./state.js";
import { clear, el, emptyState, fmt, toast } from "./ui.js";

const COMPARE_ROWS = [
  ["expected_reward_inr", "Expected reward", "money", "up"],
  ["recall", "Recall", "ratio", "up"],
  ["precision", "Precision", "ratio", "up"],
  ["false_positive_cost_inr", "False-positive cost", "money", "down"],
  ["fraud_value_captured_inr", "Fraud value captured", "money", "up"],
  ["escalation_rate", "Escalation rate", "ratio", "down"],
  ["safety_violations", "Safety violations", "count", "down"],
  ["evaluated_incidents", "Evaluated incidents", "count", "flat"],
];

bootstrap({
  title: "Models & Policies",
  subtitle: "Shadow comparison with explicit human-gated promotion",
  build,
  load,
  pollMs: 20000,
});

function build(main) {
  main.append(
    el("div", { class: "page-grid" }, [
      el("section", { class: "col-7 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [
            el("h2", { text: "Production vs candidate policy" }),
            el("p", { id: "policy-status", text: "Loading comparison…" }),
          ]),
          el("div", { class: "spacer" }),
          el("span", { class: "badge", "data-tone": "ai" }, [
            el("span", { text: "candidate is shadow-only" }),
          ]),
        ]),
        el("div", { class: "panel__body panel__body--tight" }, [
          el("div", { class: "compare", id: "policy-compare" }),
        ]),
        el("div", { class: "panel__foot" }, [
          el("span", {
            text: "No automatic promotion path exists. Promotion and rollback require an authenticated admin.",
          }),
        ]),
      ]),

      el("section", { class: "col-5 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [el("h2", { text: "Promotion gate" }), el("p", { text: "Deterministic checks" })]),
        ]),
        el("div", { class: "panel__body" }, [
          el("div", { class: "gate-list", id: "gate-list" }),
          el("p", { class: "eyebrow", id: "gate-note", text: "" }),
          el("div", { class: "hitl__actions", id: "policy-actions" }),
        ]),
      ]),

      el("section", { class: "col-7 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [el("h2", { text: "Response policy versions" }), el("p", { text: "Immutable lifecycle" })]),
        ]),
        el("div", { class: "panel__body panel__body--tight" }, [
          el("table", { class: "table" }, [
            el("thead", {}, [
              el("tr", {}, [
                el("th", { text: "Version" }),
                el("th", { text: "Status" }),
                el("th", { text: "Family" }),
                el("th", { text: "Artifact checksum" }),
                el("th", { text: "Approved by" }),
                el("th", { text: "Activated" }),
              ]),
            ]),
            el("tbody", { id: "policy-rows" }),
          ]),
        ]),
      ]),

      el("section", { class: "col-5 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [el("h2", { text: "Model drift" }), el("p", { id: "drift-note", text: "Advisory only" })]),
        ]),
        el("div", { class: "panel__body", id: "drift-rows" }),
      ]),

      el("section", { class: "col-12 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [el("h2", { text: "Fraud model registry" }), el("p", { text: "Registered artifacts" })]),
        ]),
        el("div", { class: "panel__body panel__body--tight" }, [
          el("table", { class: "table" }, [
            el("thead", {}, [
              el("tr", {}, [
                el("th", { text: "Name" }),
                el("th", { text: "Version" }),
                el("th", { text: "Type" }),
                el("th", { text: "Status" }),
                el("th", { text: "Threshold space" }),
                el("th", { text: "Risk density space" }),
                el("th", { text: "Registered" }),
              ]),
            ]),
            el("tbody", { id: "model-rows" }),
          ]),
        ]),
      ]),
    ])
  );

  subscribe(["comparison"], renderComparison);
  subscribe(["policies"], renderPolicies);
  subscribe(["models"], renderModels);
  subscribe(["drift"], renderDrift);
}

async function load() {
  const [comparison, policies, models, drift] = await Promise.all([
    api.policyComparison().catch(() => null),
    api.policies().catch(() => null),
    api.models().catch(() => null),
    api.drift().catch(() => null),
  ]);
  setState({ comparison, policies, models, drift });
}

function renderComparison(state) {
  const container = document.getElementById("policy-compare");
  const status = document.getElementById("policy-status");
  const gateList = document.getElementById("gate-list");
  const gateNote = document.getElementById("gate-note");
  const actions = document.getElementById("policy-actions");
  if (!container) return;
  clear(container);
  clear(gateList);
  clear(actions);

  const comparison = state.comparison;
  if (!comparison) {
    container.append(emptyState("Comparison unavailable"));
    return;
  }

  const production = comparison.production;
  const candidate = comparison.candidate;
  if (status) {
    status.textContent = production
      ? `Production v${production.version} · candidate ${candidate ? `v${candidate.version}` : "none registered"} · evidence: ${fmt.words(
          comparison.promotion_evidence || "unknown"
        )}`
      : "No production policy version registered yet";
  }

  ["Metric", "Production", "Candidate (shadow)", "Δ"].forEach((label) =>
    container.append(el("div", { class: "compare__head", text: label }))
  );

  const productionMetrics = metricsOf(production);
  const candidateMetrics = metricsOf(candidate);

  COMPARE_ROWS.forEach(([key, label, kind, better]) => {
    const pv = productionMetrics?.[key];
    const cv = candidateMetrics?.[key];
    container.append(el("div", { class: "compare__metric", text: label }));
    container.append(el("div", { class: "compare__value", text: format(pv, kind) }));
    container.append(el("div", { class: "compare__value", text: format(cv, kind) }));
    container.append(
      el("div", {
        class: "compare__delta",
        "data-dir": direction(pv, cv, better),
        text: delta(pv, cv, kind),
      })
    );
  });

  const gate = comparison.promotion_gate;
  if (gate) {
    Object.entries(gate.checks || {}).forEach(([name, passed]) => {
      gateList.append(
        el("span", { class: "badge", "data-tone": passed ? "positive" : "critical" }, [
          el("span", { text: `${fmt.words(name)}: ${passed ? "pass" : "fail"}` }),
        ])
      );
    });
    if (gateNote) {
      gateNote.textContent = gate.passed
        ? "All deterministic gates pass. An admin must still approve promotion explicitly."
        : `Blocked by: ${(gate.reasons || []).map(fmt.words).join(", ")}`;
    }
  } else if (gateNote) {
    gateNote.textContent =
      comparison.promotion_evidence === "valid"
        ? "No gate result available."
        : "Bound same-holdback evaluation evidence is required before a gate can be computed.";
  }

  const isAdmin = session.role === "admin";
  if (candidate) {
    actions.append(
      el("button", {
        class: "btn btn--primary",
        text: isAdmin ? "Promote candidate" : "Promote (admin only)",
        disabled: !isAdmin,
        onClick: (event) => act(() => api.promotePolicy(candidate.policy_version_id), event.currentTarget, "Promoted"),
      })
    );
  }
  const retired = (state.policies?.items || []).filter((row) => row.status === "retired");
  if (retired.length) {
    actions.append(
      el("button", {
        class: "btn btn--warning",
        text: isAdmin ? `Rollback to v${retired[0].version}` : "Rollback (admin only)",
        disabled: !isAdmin,
        onClick: (event) =>
          act(() => api.rollbackPolicy(retired[0].policy_version_id), event.currentTarget, "Rolled back"),
      })
    );
  }
  if (!isAdmin) {
    actions.append(
      el("p", { class: "eyebrow", text: "Signed in without admin permission; lifecycle controls are disabled." })
    );
  }
}

function metricsOf(version) {
  if (!version) return null;
  const evidence = version.metrics?.promotion_evidence;
  if (evidence?.candidate_metrics && version.status !== "production") return evidence.candidate_metrics;
  if (evidence?.production_metrics && version.status === "production") return evidence.production_metrics;
  return version.metrics && !version.metrics.promotion_evidence ? version.metrics : null;
}

function format(value, kind) {
  if (value === null || value === undefined) return "—";
  if (kind === "money") return fmt.money(value);
  if (kind === "ratio") return fmt.ratio(value, 3);
  return fmt.count(value);
}

function delta(a, b, kind) {
  if (!Number.isFinite(a) || !Number.isFinite(b)) return "—";
  const diff = b - a;
  const prefix = diff > 0 ? "+" : "";
  if (kind === "money") return `${prefix}${fmt.money(diff)}`;
  if (kind === "ratio") return `${prefix}${diff.toFixed(3)}`;
  return `${prefix}${fmt.count(diff)}`;
}

function direction(a, b, better) {
  if (!Number.isFinite(a) || !Number.isFinite(b) || a === b) return "flat";
  const improved = better === "up" ? b > a : b < a;
  return improved ? "up" : "down";
}

async function act(operation, button, verb) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Working…";
  try {
    const result = await operation();
    toast(`${verb} response policy v${result.version}`, { tone: "accent", title: "Policy lifecycle" });
    await load();
  } catch (error) {
    recordError(error);
    toast(error.detail, { tone: "critical", title: "Lifecycle change rejected" });
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

function renderPolicies(state) {
  const body = document.getElementById("policy-rows");
  if (!body) return;
  clear(body);
  const items = state.policies?.items || [];
  if (!items.length) {
    body.append(
      el("tr", {}, [
        el("td", { colspan: "6" }, [
          emptyState("No policy versions registered", "Register a trained candidate to enable comparison"),
        ]),
      ])
    );
    return;
  }
  items.forEach((row) => {
    body.append(
      el("tr", {}, [
        el("td", { class: "mono", text: `v${row.version}` }),
        el("td", {}, [
          el("span", {
            class: "badge",
            "data-tone": row.status === "production" ? "positive" : row.status === "retired" ? "muted" : "ai",
          }, [el("span", { text: row.status })]),
        ]),
        el("td", { text: fmt.words(row.rules?.family || "unknown") }),
        el("td", { class: "mono", text: row.artifact_checksum ? row.artifact_checksum.slice(0, 16) : "—" }),
        el("td", { text: row.approved_by || "—" }),
        el("td", { class: "num", text: row.activated_at ? fmt.dateTime(row.activated_at) : "—" }),
      ])
    );
  });
}

function renderModels(state) {
  const body = document.getElementById("model-rows");
  if (!body) return;
  clear(body);
  const items = state.models?.items || [];
  if (!items.length) {
    body.append(el("tr", {}, [el("td", { colspan: "7" }, [emptyState("No registered model versions")])]));
    return;
  }
  items.forEach((row) => {
    body.append(
      el("tr", {}, [
        el("td", { text: row.name }),
        el("td", { class: "mono", text: row.version }),
        el("td", { text: row.model_type }),
        el("td", {}, [
          el("span", { class: "badge", "data-tone": row.status === "active" ? "positive" : "muted" }, [
            el("span", { text: row.status }),
          ]),
        ]),
        el("td", { class: "mono", text: row.threshold_score_space || "—" }),
        el("td", { class: "mono", text: row.risk_density_score_space || "—" }),
        el("td", { class: "num", text: fmt.dateTime(row.registered_at) }),
      ])
    );
  });
}
