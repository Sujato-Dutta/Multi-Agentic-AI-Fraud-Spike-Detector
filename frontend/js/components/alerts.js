/** Alert center: live spike, incident, decision, and degradation notices. */

import { clear, el, emptyState, fmt } from "../ui.js";

export function renderAlertCenter(state) {
  const container = document.getElementById("alert-list");
  if (!container) return;
  clear(container);

  const degraded = Object.entries(state.dependencies || {}).filter(
    ([, health]) => health.status !== "healthy"
  );

  if (!state.alerts.length && !degraded.length) {
    container.append(emptyState("No active alerts", "Dependencies healthy, no spikes raised"));
    return;
  }

  degraded.forEach(([name, health]) => {
    container.append(
      el("div", { class: "alert-item", "data-tone": health.status === "down" ? "critical" : "warning" }, [
        el("span", { "aria-hidden": "true" }),
        el("div", {}, [
          el("p", { class: "alert-item__title", text: `${fmt.words(name)} ${health.status}` }),
          el("p", {
            class: "alert-item__detail",
            text: health.reason || "Degraded state is visible and metered; processing continues.",
          }),
          el("time", { text: fmt.dateTime(health.changed_at) }),
        ]),
      ])
    );
  });

  state.alerts.forEach((alert) => {
    container.append(
      el("div", { class: "alert-item", "data-tone": alert.tone }, [
        el("span", { "aria-hidden": "true" }),
        el("div", {}, [
          el("p", { class: "alert-item__title", text: alert.title }),
          el("p", { class: "alert-item__detail", text: alert.detail }),
          el("time", { text: fmt.dateTime(alert.at) }),
        ]),
      ])
    );
  });
}
