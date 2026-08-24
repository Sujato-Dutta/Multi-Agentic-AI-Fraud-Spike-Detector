/** Investigation detail: agent timeline, evidence, responses, HITL panel, audit chain. */

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
import { el, fmt, toast } from "./ui.js";

const incidentId = new URLSearchParams(window.location.search).get("incident");

bootstrap({
  title: incidentId ? `Investigation · ${incidentId}` : "Investigation",
  subtitle: "Claim-level verification, deterministic impact, human authorization",
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
    el("div", { class: "page-grid" }, [
      el("section", { class: "col-8 panel anim-rise", id: "incident-header" }),

      el("section", { class: "col-4 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [el("h2", { text: "Grounding" }), el("p", { text: "Verified claims / material claims" })]),
        ]),
        el("div", { class: "panel__body" }, [
          el("div", { class: "gauge", id: "grounding-wrap" }, [
            el("div", { class: "gauge__ring", id: "grounding-ring" }, [
              el("span", { class: "gauge__value", id: "grounding", text: "—" }),
            ]),
            el("span", { class: "gauge__label", text: "Grounding score" }),
          ]),
          el("p", { class: "eyebrow", id: "grounding-note", text: "Awaiting verification output" }),
        ]),
      ]),

      el("section", { class: "col-7 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [
            el("h2", { text: "Agent investigation timeline" }),
            el("p", { text: "Each stage records its model, prompt version, and evidence hash" }),
          ]),
        ]),
        el("div", { class: "panel__body" }, [el("div", { class: "timeline", id: "agent-timeline" })]),
      ]),

      el("section", { class: "col-5" }, [
        el("div", { class: "panel anim-rise", style: "margin-bottom: var(--space-4)" }, [
          el("div", { class: "panel__head" }, [
            el("div", {}, [el("h2", { text: "Ranked segments" }), el("p", { text: "Deterministic discovery, no model involved" })]),
          ]),
          el("div", { class: "panel__body panel__body--scroll", id: "segment-list" }),
        ]),
        el("div", { class: "panel anim-rise" }, [
          el("div", { class: "panel__head" }, [
            el("div", {}, [el("h2", { text: "Evidence store" }), el("p", { text: "Resolvable evidence records" })]),
          ]),
          el("div", { class: "panel__body panel__body--scroll", id: "evidence-list" }),
        ]),
      ]),

      el("section", { class: "col-12 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [
            el("h2", { text: "Response comparison" }),
            el("p", { text: "Production policy ranking; the model only explains it" }),
          ]),
        ]),
        el("div", { class: "panel__body" }, [
          el("div", { class: "response-grid", id: "response-grid" }),
          el("p", { class: "assumption-note", id: "response-assumptions" }),
        ]),
      ]),

      el("section", { class: "col-5 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [el("h2", { text: "Deterministic impact" }), el("p", { text: "Computed server-side" })]),
        ]),
        el("div", { class: "panel__body", id: "impact-summary" }),
      ]),

      el("section", { class: "col-7 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [el("h2", { text: "Policy gate" }), el("p", { text: "Deterministic authorization" })]),
        ]),
        el("div", { class: "panel__body", id: "policy-gate" }),
      ]),

      el("section", { class: "col-6 hitl anim-rise", id: "hitl-panel" }),

      el("section", { class: "col-6 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [el("h2", { text: "Audit trail" }), el("p", { text: "Append-only decision history" })]),
        ]),
        el("div", { class: "panel__body panel__body--scroll", id: "audit-trail" }),
      ]),
    ])
  );

  subscribe(["selectedIncident"], (state) => {
    renderIncidentHeader(state);
    renderSegments(state);
  });
  subscribe(["investigation"], (state) => {
    renderAgentTimeline(state);
    renderEvidence(state);
    renderGrounding(state);
  });
  subscribe(["review"], (state) => {
    renderResponseComparison(state);
    renderImpact(state);
    renderPolicyGate(state);
    renderReviewPanel(state);
  });
  subscribe(["audit"], renderAuditTrail);
}

async function load() {
  if (!incidentId) return;
  const [incident, investigation, review, audit] = await Promise.all([
    api.incident(incidentId),
    api.investigation(incidentId).catch(() => null),
    api.review(incidentId).catch((error) => {
      if (error.status === 409 || error.status === 404) return null;
      throw error;
    }),
    api.audit(incidentId).catch(() => null),
  ]);
  setState({ selectedIncident: incident, investigation, review, audit });
  if (!review) {
    const note = document.getElementById("response-assumptions");
    if (note && !note.textContent) {
      note.textContent = "This incident is not awaiting human review; the recorded decision is in the audit trail.";
    }
  }
}

function renderGrounding(state) {
  const outputs = state.investigation?.outputs || [];
  const verification = outputs.find((output) => output.agent_name === "evidence_verification");
  const result = verification?.payload?.result;
  const note = document.getElementById("grounding-note");
  if (!result || !Number.isFinite(result.grounding_score)) {
    setGauge("grounding", NaN);
    if (note) note.textContent = "Awaiting verification output";
    return;
  }
  setGauge("grounding", result.grounding_score);
  if (note) {
    const supported = (result.verdicts || []).filter((v) => v.verdict === "supported").length;
    note.textContent = `${supported}/${(result.verdicts || []).length} claims supported · unsupported claims are stripped from this view`;
  }
}
