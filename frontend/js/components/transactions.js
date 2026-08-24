/** Live transaction ticker. Newest row animates in; high-risk rows are tinted. */

import { clear, el, emptyState, fmt } from "../ui.js";

const MAX_ROWS = 40;

export function tickerShell() {
  return el("div", { class: "ticker", id: "ticker" }, [
    el("table", { class: "table" }, [
      el("thead", {}, [
        el("tr", {}, [
          el("th", { text: "Time" }),
          el("th", { text: "Transaction" }),
          el("th", { text: "Amount" }),
          el("th", { text: "Risk" }),
        ]),
      ]),
      el("tbody", { id: "ticker-body" }),
    ]),
  ]);
}

/** Seed the ticker from the REST trend so the panel is never empty on load. */
export function seedTicker(state) {
  const body = document.getElementById("ticker-body");
  if (!body) return;
  const points = (state.timeseries?.points || []).slice(-MAX_ROWS).reverse();
  clear(body);
  if (!points.length) {
    body.append(
      el("tr", {}, [
        el("td", { colspan: "4" }, [emptyState("No scored transactions yet", "Start the replay stream")]),
      ])
    );
    return;
  }
  points.forEach((point, index) => body.append(row(point, index === 0)));
}

export function appendLiveTransaction(payload) {
  const body = document.getElementById("ticker-body");
  if (!body || !payload) return;
  const placeholder = body.querySelector("td[colspan]");
  if (placeholder) clear(body);
  body.prepend(
    row(
      {
        timestamp: payload.timestamp || new Date().toISOString(),
        transaction_id: payload.transaction_id,
        amount_inr: Number(payload.amount_inr ?? 0),
        risk_probability: Number(payload.risk_probability ?? 0),
        high_risk: Boolean(
          payload.high_risk ??
            (Number(payload.decision_score ?? 0) >= Number(payload.decision_threshold ?? 1))
        ),
      },
      true
    )
  );
  while (body.children.length > MAX_ROWS) body.lastElementChild.remove();
}

function row(point, entering) {
  const risk = Number(point.risk_probability ?? 0);
  const bar = el("b");
  requestAnimationFrame(() => {
    bar.style.width = `${Math.max(3, Math.min(risk, 1) * 100)}%`;
  });
  return el(
    "tr",
    {
      "data-high-risk": String(Boolean(point.high_risk)),
      class: entering ? "row-entering" : null,
    },
    [
      el("td", { class: "num", text: fmt.time(point.timestamp) }),
      el("td", { class: "mono", text: point.transaction_id || "—" }),
      el("td", { class: "num", text: fmt.money(Number(point.amount_inr ?? 0)) }),
      el("td", {}, [
        el("span", { class: "risk-pill" }, [
          el("span", { text: risk.toFixed(3) }),
          el("i", {}, [bar]),
        ]),
      ]),
    ]
  );
}
