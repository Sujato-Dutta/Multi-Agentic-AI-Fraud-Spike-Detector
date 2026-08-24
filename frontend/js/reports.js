/**
 * Held-out report view. Every number is read from the sealed results.json produced by
 * evaluation/heldout_report.py. Nothing on this page is hardcoded; when the evaluation
 * has not been run the view says so instead of inventing values.
 */

import { api } from "./api.js";
import { bootstrap } from "./app.js";
import { renderBars } from "./charts.js";
import { setState, subscribe } from "./state.js";
import { clear, el, emptyState, fmt } from "./ui.js";

bootstrap({
  title: "Held-out Evaluation",
  subtitle: "One sealed run against the test holdout",
  build,
  load,
  pollMs: 60000,
});

function build(main) {
  main.append(
    el("div", { class: "page-grid" }, [
      el("section", { class: "col-12 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [
            el("h2", { text: "Provenance" }),
            el("p", { id: "report-provenance", text: "Loading report…" }),
          ]),
          el("div", { class: "spacer" }),
          el("span", { class: "badge", id: "report-state", "data-tone": "muted" }, [
            el("span", { text: "checking" }),
          ]),
        ]),
        el("div", { class: "panel__body", id: "report-integrity" }),
      ]),
      el("section", { class: "col-6 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [el("h2", { text: "Transaction level" }), el("p", { text: "Chosen operating point" })]),
        ]),
        el("div", { class: "panel__body", id: "report-transaction" }),
      ]),
      el("section", { class: "col-6 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [el("h2", { text: "Spike / event level" }), el("p", { text: "Matched events and delay" })]),
        ]),
        el("div", { class: "panel__body", id: "report-events" }),
      ]),
      el("section", { class: "col-6 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [el("h2", { text: "Business outcome" }), el("p", { text: "Net risk benefit" })]),
        ]),
        el("div", { class: "panel__body", id: "report-business" }),
      ]),
      el("section", { class: "col-6 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [
            el("h2", { text: "Assumption sensitivity" }),
            el("p", { text: "Net risk benefit across assumption ranges" }),
          ]),
        ]),
        el("div", { class: "panel__body", id: "report-sensitivity" }),
      ]),
      el("section", { class: "col-12 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [
            el("h2", { text: "Policy and safety" }),
            el("p", { text: "Production vs candidate on the same held-out incidents" }),
          ]),
        ]),
        el("div", { class: "panel__body", id: "report-policy" }),
      ]),
    ])
  );

  subscribe(["heldout"], render);
}

async function load() {
  const heldout = await api.heldout();
  setState({ heldout });
}

function render(state) {
  const payload = state.heldout;
  const badge = document.getElementById("report-state");
  const provenance = document.getElementById("report-provenance");
  if (!payload) return;

  if (!payload.available) {
    badge.dataset.tone = "warning";
    badge.querySelector("span").textContent = "not run";
    provenance.textContent = `Expected at ${payload.path}`;
    ["report-integrity", "report-transaction", "report-events", "report-business", "report-sensitivity", "report-policy"].forEach(
      (id) => {
        const node = document.getElementById(id);
        if (!node) return;
        clear(node);
        node.append(
          emptyState(
            "Held-out evaluation has not been run",
            "Run evaluation/heldout_report.py once, at freeze time"
          )
        );
      }
    );
    return;
  }

  const report = payload.report;
  badge.dataset.tone = "positive";
  badge.querySelector("span").textContent = "sealed run";
  provenance.textContent = `Generated ${fmt.dateTime(report.generated_at)} · commit ${report.commit} · working tree ${report.working_tree}`;

  renderIntegrity(report);
  renderTransaction(report);
  renderEvents(report);
  renderBusiness(report);
  renderSensitivity(report);
  renderPolicy(report);
}

function renderIntegrity(report) {
  const node = document.getElementById("report-integrity");
  clear(node);
  const integrity = report.integrity || {};
  node.append(
    el("div", { class: "value-split" }, [
      cell(
        "Sealed label reads",
        Number.isFinite(integrity.access_log_entries)
          ? fmt.count(integrity.access_log_entries)
          : "—",
        integrity.access_log_entries === 1 ? "positive" : "warning"
      ),
      cell("Holdout rows", fmt.count(report.dataset?.rows ?? NaN), "default"),
      cell("Spike events", fmt.count(report.dataset?.spike_events ?? NaN), "default"),
      cell("Benign surges", fmt.count(report.dataset?.benign_events ?? NaN), "warning"),
    ])
  );
  node.append(
    el("p", { class: "report-note" }, [
      el("strong", { text: "Protocol: " }),
      el("span", {
        text:
          integrity.protocol ||
          "Fit on train, calibrate on the train tail, select thresholds on validation, touch the test holdout once.",
      }),
    ])
  );
  if (integrity.notes?.length) {
    integrity.notes.forEach((note) =>
      node.append(el("p", { class: "assumption-note", text: note }))
    );
  }
}

function renderTransaction(report) {
  const node = document.getElementById("report-transaction");
  clear(node);
  const point = report.transaction?.[report.transaction?.selected_operating_point || "precision_floor"];
  if (!point) {
    node.append(emptyState("No transaction metrics in report"));
    return;
  }
  const metrics = point.metrics || {};
  node.append(
    el("dl", { class: "kv" }, [
      row("Operating point", fmt.words(point.operating_point || "")),
      row("Threshold", fmt.ratio(point.threshold, 4)),
      row("Precision", fmt.ratio(metrics.precision)),
      row("Recall", fmt.ratio(metrics.recall)),
      row("F1", fmt.ratio(metrics.f1)),
      row("PR-AUC", fmt.ratio(metrics.pr_auc)),
      row("ROC-AUC", fmt.ratio(metrics.roc_auc)),
      row("False positives", fmt.count(metrics.false_positives)),
      row("False negatives", fmt.count(metrics.false_negatives)),
      row("FP rate", fmt.percent(metrics.false_positive_rate, 2)),
      row("FN rate", fmt.percent(metrics.false_negative_rate, 2)),
    ])
  );
}

function renderEvents(report) {
  const node = document.getElementById("report-events");
  clear(node);
  const metrics = report.events?.metrics;
  if (!metrics) {
    node.append(emptyState("No event metrics in report"));
    return;
  }
  node.append(
    el("dl", { class: "kv" }, [
      row("Event precision", fmt.ratio(metrics.precision)),
      row("Event recall", fmt.ratio(metrics.recall)),
      row("Matched events", `${metrics.matched_events} / ${metrics.total_events}`),
      row("False alerts", fmt.count(metrics.false_alerts)),
      row("False alerts in benign surges", fmt.count(metrics.benign_window_false_alerts)),
      row("Continuation alerts", fmt.count(metrics.continuation_alerts)),
      row("Median delay", fmt.minutes(metrics.median_delay_minutes)),
      row("P90 delay", fmt.minutes(metrics.p90_delay_minutes)),
    ])
  );
  node.append(
    el("p", {
      class: "report-note",
      text: "False alerts inside benign surge windows are the headline false-positive-pressure number, reported separately by design.",
    })
  );
}

function renderBusiness(report) {
  const node = document.getElementById("report-business");
  clear(node);
  const business = report.business;
  if (!business) {
    node.append(emptyState("No business metrics in report"));
    return;
  }
  node.append(
    el("div", { class: "value-split" }, [
      cell("Fraud value captured", fmt.money(business.fraud_exposure_captured_inr), "positive"),
      cell("Fraud value missed", fmt.money(business.fraud_loss_missed_inr), "critical"),
      cell("False-positive cost", fmt.money(business.false_positive_cost_inr), "warning"),
      cell("Net risk benefit", fmt.money(business.net_risk_benefit_inr), "positive"),
    ])
  );
  node.append(
    el("dl", { class: "kv" }, [
      row("Legitimate value disrupted", fmt.money(business.legitimate_value_disrupted_inr)),
      row("Analyst review cost", fmt.money(business.analyst_review_cost_inr)),
      row("Customer friction cost", fmt.money(business.customer_friction_cost_inr)),
    ])
  );
  node.append(
    el("p", {
      class: "assumption-note",
      text: "Analyst review, customer friction, and delay costs are documented assumptions from reports/COST_ASSUMPTIONS.md, not measured values.",
    })
  );
}

function renderSensitivity(report) {
  const node = document.getElementById("report-sensitivity");
  clear(node);
  const rows = report.sensitivity || [];
  if (!rows.length) {
    node.append(emptyState("No sensitivity analysis in report"));
    return;
  }
  const canvas = el("canvas", { id: "sensitivity-chart", role: "img", "aria-label": "Net risk benefit sensitivity" });
  node.append(el("div", { class: "chart-shell chart-shell--short" }, [canvas]));
  renderBars(
    canvas,
    rows.map((entry) => ({
      label: `${fmt.words(entry.parameter)} ${entry.value}`,
      value: entry.net_risk_benefit_inr,
      display: fmt.moneyCompact(entry.net_risk_benefit_inr),
    }))
  );
  node.append(
    el("p", {
      class: "report-note",
      text: "Each bar recomputes net risk benefit with one assumption changed, so the headline figure's dependence on our assumptions is explicit.",
    })
  );
}

function renderPolicy(report) {
  const node = document.getElementById("report-policy");
  clear(node);
  const policy = report.policy;
  if (!policy) {
    node.append(emptyState("No policy section in report"));
    return;
  }
  node.append(
    el("div", { class: "value-split" }, [
      cell("Safety policy violations", fmt.count(policy.safety_policy_violations ?? 0), "positive"),
      cell("Production action", fmt.words(policy.production_action || "n/a"), "default"),
      cell("Candidate action (shadow)", fmt.words(policy.candidate_action || "n/a"), "warning"),
      cell("Automatic promotion", policy.automatic_promotion === false ? "none" : "review", "positive"),
    ])
  );
  if (policy.notes?.length) {
    policy.notes.forEach((note) => node.append(el("p", { class: "report-note", text: note })));
  }
  if (report.agent) {
    node.append(
      el("dl", { class: "kv" }, Object.entries(report.agent).flatMap(([key, value]) => [
        el("dt", { text: fmt.words(key) }),
        el("dd", { text: typeof value === "number" ? fmt.ratio(value, 3) : String(value) }),
      ]))
    );
  }
}

function row(label, value) {
  const fragment = document.createDocumentFragment();
  fragment.append(el("dt", { text: label }), el("dd", { text: value }));
  return fragment;
}

function cell(label, value, tone) {
  return el("div", { class: "value-cell", "data-tone": tone }, [
    el("span", { text: label }),
    el("b", { text: value }),
  ]);
}
