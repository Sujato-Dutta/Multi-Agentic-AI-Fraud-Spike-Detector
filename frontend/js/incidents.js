/** Incident queue: filterable list with density-vs-volume contrast on every row. */

import { api } from "./api.js";
import { bootstrap } from "./app.js";
import { getState, setState, subscribe } from "./state.js";
import { clear, el, emptyState, fmt, severityTone, skeletonRows } from "./ui.js";

const FILTERS = [
  ["all", "All"],
  ["detected", "Detected"],
  ["investigating", "Investigating"],
  ["awaiting_human_review", "Awaiting review"],
  ["awaiting_outcome", "Awaiting outcome"],
  ["completed", "Completed"],
];

bootstrap({
  title: "Incident Queue",
  subtitle: "Every incident carries its detector evidence and ranked segments",
  build,
  load,
  onLive: (message) => {
    if (message.type === "alert" || message.type === "incident_update") load();
  },
});

function build(main) {
  main.append(
    el("div", { class: "page-grid" }, [
      el("section", { class: "col-12 panel anim-rise" }, [
        el("div", { class: "panel__head" }, [
          el("div", {}, [
            el("h2", { text: "Incidents" }),
            el("p", { id: "incident-count", text: "Loading…" }),
          ]),
          el("div", { class: "spacer" }),
          el("div", { class: "tabs", role: "tablist", id: "incident-filters" },
            FILTERS.map(([value, label]) =>
              el("button", {
                class: "tab",
                role: "tab",
                "data-filter": value,
                "aria-selected": String(value === "all"),
                text: label,
                onClick: () => applyFilter(value),
              })
            )
          ),
        ]),
        el("div", { class: "panel__body panel__body--tight" }, [
          el("table", { class: "table" }, [
            el("thead", {}, [
              el("tr", {}, [
                el("th", { text: "Incident" }),
                el("th", { text: "Status" }),
                el("th", { text: "Severity" }),
                el("th", { text: "Detected" }),
                el("th", { text: "Density lift" }),
                el("th", { text: "Volume lift" }),
                el("th", { text: "Exposure" }),
                el("th", { text: "Segments" }),
                el("th", { text: "" }),
              ]),
            ]),
            el("tbody", { id: "incident-rows" }, skeletonRows(6, 9)),
          ]),
        ]),
        el("div", { class: "panel__foot" }, [
          el("span", {
            text: "Alerts fire on calibrated risk density. A benign surge with high volume and normal density does not appear here.",
          }),
        ]),
      ]),
    ])
  );

  subscribe(["incidents", "incidentFilter"], renderRows);
}

async function load() {
  const filter = getState().incidentFilter;
  const result = await api.incidents({
    limit: 100,
    status: filter === "all" ? null : filter,
  });
  setState({ incidents: result.items || [] });
}

function applyFilter(value) {
  document.querySelectorAll("#incident-filters .tab").forEach((tab) => {
    tab.setAttribute("aria-selected", String(tab.dataset.filter === value));
  });
  setState({ incidentFilter: value });
  load().catch(() => {});
}

function renderRows(state) {
  const body = document.getElementById("incident-rows");
  const count = document.getElementById("incident-count");
  if (!body) return;
  clear(body);
  const incidents = state.incidents || [];
  if (count) {
    count.textContent = `${fmt.count(incidents.length)} incident(s) · filter: ${fmt.words(
      state.incidentFilter
    )}`;
  }
  if (!incidents.length) {
    body.append(el("tr", {}, [el("td", { colspan: "9" }, [emptyState("No incidents for this filter")])]));
    return;
  }
  incidents.forEach((incident) => {
    const detector = incident.detector_output || {};
    body.append(
      el("tr", {}, [
        el("td", { class: "mono", text: incident.incident_id }),
        el("td", {}, [
          el("span", { class: "badge", "data-tone": "muted" }, [
            el("span", { text: fmt.words(incident.status) }),
          ]),
        ]),
        el("td", {}, [
          el("span", { class: "badge", "data-tone": severityTone(incident.severity) }, [
            el("span", { text: incident.severity || "unrated" }),
          ]),
        ]),
        el("td", { class: "num", text: fmt.dateTime(incident.detected_at) }),
        el("td", { class: "num", "data-tone": "critical", text: fmt.multiplier(detector.density_lift) }),
        el("td", { class: "num", text: fmt.multiplier(detector.volume_lift) }),
        el("td", { class: "num", text: fmt.money(incident.exposure_estimate_inr) }),
        el("td", { class: "num", text: fmt.count((incident.segments || []).length) }),
        el("td", {}, [
          el("a", {
            class: "btn btn--sm",
            href: `/pages/investigation.html?incident=${encodeURIComponent(incident.incident_id)}`,
            text: "Investigate",
          }),
        ]),
      ])
    );
  });
}
