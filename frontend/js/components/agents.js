/** Agent investigation timeline: one stage per persisted agent output. */

import { clear, el, emptyState, fmt, verdictTone } from "../ui.js";

const STAGE_ORDER = [
  ["lead_spike_analysis", "Lead spike analysis"],
  ["segment_interpretation", "Segment interpretation"],
  ["root_cause_hypotheses", "Root-cause hypotheses"],
  ["evidence_verification", "Evidence verification"],
  ["deterministic_impact", "Deterministic impact"],
  ["response_recommendations", "Response recommendations"],
  ["response_policy_shadow", "Response policy (shadow)"],
  ["lead_synthesis", "Lead synthesis"],
  ["alert_explanation", "Analyst explanation"],
];

export function renderAgentTimeline(state) {
  const container = document.getElementById("agent-timeline");
  if (!container) return;
  clear(container);
  const outputs = state.investigation?.outputs || [];
  if (!outputs.length) {
    container.append(
      emptyState("No agent output yet", "Investigations run automatically after an incident is raised")
    );
    return;
  }

  const byName = new Map(outputs.map((output) => [output.agent_name, output]));
  STAGE_ORDER.forEach(([name, label], index) => {
    const output = byName.get(name);
    if (!output) return;
    const provenance = output.payload?.provenance || {};
    container.append(
      el("div", { class: "stage", "data-status": output.status }, [
        el("div", { class: "stage__marker", text: String(index + 1) }),
        el("div", { class: "stage__body" }, [
          el("div", { class: "stage__title" }, [
            el("span", { text: label }),
            el("span", { class: "badge", "data-tone": output.status === "completed" ? "positive" : "warning" }, [
              el("span", { text: output.status }),
            ]),
            provenance.provider_degraded
              ? el("span", { class: "badge", "data-tone": "warning" }, [el("span", { text: "provider degraded" })])
              : null,
          ]),
          el("div", { class: "stage__meta" }, [
            el("span", { text: output.model_name || "deterministic-python" }),
            el("span", { text: output.prompt_version || "n/a" }),
            provenance.tier ? el("span", { text: `tier ${provenance.tier}` }) : null,
            output.evidence_hash ? el("span", { text: `evidence ${output.evidence_hash.slice(0, 10)}` }) : null,
          ]),
          el("div", { class: "stage__progress" }, [el("i")]),
          el("div", { class: "stage__content" }, [stageContent(name, output.payload?.result)]),
        ]),
      ])
    );
  });
}

function stageContent(name, result) {
  if (result === null || result === undefined) return el("span", { class: "eyebrow", text: "no payload" });

  if (name === "root_cause_hypotheses" && Array.isArray(result)) {
    if (!result.length) {
      return el("span", {
        class: "eyebrow",
        text:
          "No verified hypotheses to show. Either none were generated, or every claim was " +
          "stripped because its evidence could not be resolved.",
      });
    }
    return el(
      "div",
      {},
      result.map((item) =>
        el("div", { class: "claim" }, [
          el("div", { class: "claim__head" }, [
            el(
              "span",
              { class: "badge", "data-tone": verdictTone(item.verification_verdict) },
              [el("span", { text: item.verification_verdict || "unverified" })]
            ),
            item.strength ? el("span", { class: "badge", "data-tone": "muted" }, [el("span", { text: item.strength })]) : null,
            el("span", { class: "eyebrow", text: item.claim_id || "" }),
          ]),
          el("p", { class: "claim__text", text: item.statement || item.hypothesis || "" }),
          el(
            "div",
            { class: "claim__evidence" },
            (item.evidence_ids || []).map((id) => el("span", { class: "evidence-chip", text: id }))
          ),
        ])
      )
    );
  }

  if (name === "evidence_verification" && result.verdicts) {
    return el("div", {}, [
      el("div", { class: "stage__meta" }, [
        el(
          "span",
          {
            class: "badge",
            // Tone reflects whether any claim was stripped. The authoritative floor lives in
            // infrastructure/policies.yaml and is enforced server-side, never duplicated here.
            "data-tone": result.grounding_score >= 1 ? "positive" : "warning",
          },
          [el("span", { text: `grounding ${fmt.ratio(result.grounding_score, 2)}` })]
        ),
        el("span", { text: `${result.verdicts.length} claim(s) checked` }),
      ]),
      el(
        "div",
        {},
        result.verdicts.map((verdict) =>
          el("div", { class: "claim" }, [
            el("div", { class: "claim__head" }, [
              el(
                "span",
                {
                  class: "badge",
                  "data-tone":
                    verdict.verdict === "supported"
                      ? "positive"
                      : verdict.verdict === "contradicted"
                        ? "critical"
                        : "warning",
                },
                [el("span", { text: verdict.verdict })]
              ),
              el("span", { class: "eyebrow", text: verdict.claim_id }),
            ]),
            el(
              "div",
              { class: "claim__evidence" },
              (verdict.resolved_evidence_ids || []).map((id) =>
                el("span", { class: "evidence-chip", text: id })
              )
            ),
          ])
        )
      ),
    ]);
  }

  if (name === "deterministic_impact") {
    return el("dl", { class: "kv" }, [
      el("dt", { text: "Fraud exposure" }),
      el("dd", { text: fmt.money(result.fraud_exposure_inr) }),
      el("dt", { text: "False-positive exposure" }),
      el("dd", { text: fmt.money(result.false_positive_exposure_inr) }),
      el("dt", { text: "Method" }),
      el("dd", { text: fmt.words(result.calculation_method || "deterministic") }),
    ]);
  }

  if (name === "response_policy_shadow") {
    return el("div", {}, [
      el("div", { class: "stage__meta" }, [
        el("span", { class: "badge", "data-tone": "accent" }, [
          el("span", { text: `operative ${fmt.words(result.operative_action || "n/a")}` }),
        ]),
        el("span", { class: "badge", "data-tone": "ai" }, [
          el("span", {
            text: result.candidate_ranking
              ? `candidate ${fmt.words(result.candidate_ranking[0]?.action || "n/a")} (shadow only)`
              : "candidate unavailable (shadow)",
          }),
        ]),
        result.degraded
          ? el("span", { class: "badge", "data-tone": "warning" }, [
              el("span", { text: "conservative fallback ranking" }),
            ])
          : null,
      ]),
      el("p", {
        class: "eyebrow",
        text: "Candidate output is observational; only the production ranking is operative.",
      }),
    ]);
  }

  if (Array.isArray(result)) {
    return el("span", { text: `${result.length} item(s)` });
  }

  if (typeof result === "object") {
    const summary = result.summary || result.analyst_summary || result.title || result.name;
    if (summary) {
      return el("div", {}, [
        el("p", { text: summary }),
        result.confidence
          ? el("span", { class: "badge", "data-tone": result.confidence === "high" ? "positive" : "warning" }, [
              el("span", { text: `confidence ${result.confidence}` }),
            ])
          : null,
      ]);
    }
    return el("dl", { class: "kv" }, Object.entries(result).slice(0, 6).flatMap(([key, value]) => [
      el("dt", { text: fmt.words(key) }),
      el("dd", { text: typeof value === "number" ? fmt.ratio(value, 3) : String(value).slice(0, 60) }),
    ]));
  }

  return el("p", { text: String(result) });
}
