/** Investigation detail: analyst journey, evidence, responses, and human decision. */

import { api } from "./api.js";
import { bootstrap } from "./app.js";
import { renderAgentTimeline } from "./components/agents.js";
import { setGauge } from "./components/metrics.js";
import { renderAuditTrail, renderPolicyGate, renderReviewPanel } from "./components/hitl.js";
import {
  renderEvidence,
  renderImpact,
  renderIncidentHeader,
  renderResponseComparison,
  renderSegments,
} from "./components/investigation.js";
import { setState, subscribe } from "./state.js";
import { el, fmt } from "./ui.js";

const incidentId = new URLSearchParams(window.location.search).get("incident");
let shouldNavigateToDecision = window.location.hash === "#hitl-panel";
let loadSequence = 0;

bootstrap({
  title: incidentId ? `Investigation · ${incidentId}` : "Investigation",
  subtitle: "Evidence-backed findings, response options, and human authorization",
  actions: [
    el("a", { class: "btn btn--ghost btn--sm", href: "/pages/incidents.html", text: "Incident queue" }),
  ],
  build,
  load,
  onLive: (message) => {
    if (!incidentId) return;
    const payload = message.payload || {};
    if (payload.incident_id && payload.incident_id !== incidentId) return;
    if (["incident_update", "decision_update", "audit_event"].includes(message.type)) load();
  },
  pollMs: 15000,
});

function build(main) {
  if (!incidentId) {
    main.append(
      el("section", { class: "panel anim-rise" }, [
        el("div", { class: "panel__body" }, [
          el("p", { text: "No incident selected." }),
          el("a", { class: "btn btn--primary", href: "/pages/incidents.html", text: "Choose an incident" }),
        ]),
      ])
    );
    return;
  }

  main.append(
    el("div", { class: "page-grid investigation-layout" }, [
      el("section", { class: "col-8 panel anim-rise", id: "incident-header" }),

      el("section", { class: "col-4 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [
            el("h2", { text: "Evidence coverage" }),
            el("p", { text: "Supported findings in this investigation" }),
          ]),
        ]),
        el("div", { class: "panel__body" }, [
          el("div", { class: "gauge", id: "grounding-wrap" }, [
            el("div", { class: "gauge__ring", id: "grounding-ring" }, [
              el("span", { class: "gauge__value", id: "grounding", text: "—" }),
            ]),
            el("span", { class: "gauge__label", text: "Evidence coverage" }),
          ]),
          el("p", { class: "eyebrow", id: "grounding-note", text: "Verification in progress" }),
        ]),
      ]),

      el("section", { class: "col-12 panel investigation-journey anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [
            el("h2", { text: "Investigation journey" }),
            el("p", { text: "What happened, what each phase established, and what requires a human decision" }),
          ]),
        ]),
        el("div", { class: "panel__body", id: "agent-timeline" }),
      ]),

      el("section", { class: "col-6 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [el("h2", { text: "Affected patterns" }), el("p", { text: "Where risk is most concentrated" })]),
        ]),
        el("div", { class: "panel__body", id: "segment-list" }),
      ]),

      el("section", { class: "col-6 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [el("h2", { text: "Evidence reviewed" }), el("p", { text: "Records supporting the findings" })]),
        ]),
        el("div", { class: "panel__body", id: "evidence-list" }),
      ]),

      el("section", { class: "col-12 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [
            el("h2", { text: "Response options" }),
            el("p", { text: "Ranked actions with rationale and supporting evidence" }),
          ]),
        ]),
        el("div", { class: "panel__body" }, [
          el("div", { class: "response-grid", id: "response-grid" }),
          el("p", { class: "assumption-note", id: "response-assumptions" }),
        ]),
      ]),

      el("section", { class: "col-5 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [el("h2", { text: "Impact assessment" }), el("p", { text: "Estimated fraud and customer exposure" })]),
        ]),
        el("div", { class: "panel__body", id: "impact-summary" }),
      ]),

      el("section", { class: "col-7 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [
            el("h2", { text: "Response safeguards" }),
            el("p", { text: "Whether the recommendation can proceed to an authorized reviewer" }),
          ]),
        ]),
        el("div", { class: "panel__body", id: "policy-gate" }),
      ]),

      el("section", {
        class: "col-7 hitl anim-rise",
        id: "hitl-panel",
        tabindex: "-1",
        "aria-label": "Human decision",
      }),

      el("section", { class: "col-5 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [el("h2", { text: "Decision history" }), el("p", { text: "Recorded analyst actions and outcomes" })]),
        ]),
        el("div", { class: "panel__body", id: "audit-trail" }),
      ]),
    ])
  );

  subscribe(["selectedIncident"], (state) => {
    renderIncidentHeader(state);
    renderSegments(state);
  });
  subscribe(["investigation"], (state) => {
    renderEvidence(state);
    renderGrounding(state);
  });
  subscribe(["selectedIncident", "investigation", "review", "audit"], renderAgentTimeline);
  subscribe(["review", "investigation"], (state) => {
    renderResponseComparison(state);
    renderImpact(state);
    renderPolicyGate(state);
  });
  subscribe(["review", "audit", "selectedIncident"], renderReviewPanel);
  subscribe(["audit"], renderAuditTrail);
}

async function load() {
  if (!incidentId) return;
  const sequence = ++loadSequence;
  const [incident, investigation, review, audit] = await Promise.all([
    api.incident(incidentId),
    api.investigation(incidentId).catch(() => null),
    api.review(incidentId).catch((error) => {
      if (error.status === 409 || error.status === 404) return null;
      throw error;
    }),
    api.audit(incidentId).catch(() => null),
  ]);
  if (sequence !== loadSequence) return;
  setState({ selectedIncident: incident, investigation, review, audit });
  navigateToActionOnce(review, audit);
  if (!review) {
    const note = document.getElementById("response-assumptions");
    if (note && !note.textContent) {
      note.textContent = "This incident has no pending review. Any recorded analyst decision appears in Decision history.";
    }
  }
}

function navigateToActionOnce(review, audit) {
  const decisions = audit?.decisions || [];
  const latest = decisions.length ? decisions.at(-1) : null;
  const outcomePending = latest?.status === "completed" && !latest.outcome;
  if ((!review && !outcomePending) || !shouldNavigateToDecision) return;
  shouldNavigateToDecision = false;
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
  window.requestAnimationFrame(() => {
    const panel = document.getElementById("hitl-panel");
    if (!panel) return;
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    panel.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
    if (!document.activeElement || document.activeElement === document.body) {
      panel.focus({ preventScroll: true });
    }
  });
}

function renderGrounding(state) {
  const outputs = state.investigation?.outputs || [];
  const verification = outputs.find((output) => output.agent_name === "evidence_verification");
  const result = verification?.payload?.result;
  const note = document.getElementById("grounding-note");
  if (!result || !Number.isFinite(result.grounding_score)) {
    setGauge("grounding", NaN);
    if (note) note.textContent = "Verification in progress";
    return;
  }
  setGauge("grounding", result.grounding_score);
  if (note) {
    const supported = (result.verdicts || []).filter((item) => item.verdict === "supported").length;
    note.textContent = `${fmt.count(supported)} of ${fmt.count((result.verdicts || []).length)} findings supported`;
  }
}
