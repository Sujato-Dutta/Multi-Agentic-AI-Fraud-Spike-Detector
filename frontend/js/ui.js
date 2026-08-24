/** Shared presentation helpers: formatting, toasts, ripples, animated counters. */

const inr = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 0,
});
const inrPrecise = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 2,
});
const compact = new Intl.NumberFormat("en-IN", { notation: "compact", maximumFractionDigits: 1 });

export const fmt = {
  money(value, { precise = false } = {}) {
    if (!Number.isFinite(value)) return "—";
    return precise ? inrPrecise.format(value) : inr.format(value);
  },
  moneyCompact(value) {
    if (!Number.isFinite(value)) return "—";
    return `₹${compact.format(value)}`;
  },
  count(value) {
    if (!Number.isFinite(value)) return "—";
    return new Intl.NumberFormat("en-IN").format(value);
  },
  ratio(value, digits = 3) {
    if (!Number.isFinite(value)) return "—";
    return value.toFixed(digits);
  },
  percent(value, digits = 1) {
    if (!Number.isFinite(value)) return "—";
    return `${(value * 100).toFixed(digits)}%`;
  },
  multiplier(value) {
    if (!Number.isFinite(value)) return "—";
    return `×${value.toFixed(2)}`;
  },
  time(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return date.toLocaleTimeString("en-GB", { hour12: false });
  },
  dateTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return `${date.toLocaleDateString("en-GB")} ${date.toLocaleTimeString("en-GB", { hour12: false })}`;
  },
  minutes(value) {
    if (!Number.isFinite(value)) return "—";
    return `${value.toFixed(0)} min`;
  },
  words(value) {
    return String(value || "")
      .replace(/[_-]+/g, " ")
      .replace(/\b\w/g, (char) => char.toUpperCase());
  },
};

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) {
  while (node?.firstChild) node.removeChild(node.firstChild);
  return node;
}

/** Toast notifications; auto-dismiss with an exit animation. */
export function toast(message, { tone = "info", title = null, timeout = 5200 } = {}) {
  const stack = document.getElementById("toast-stack");
  if (!stack) return;
  const node = el("div", { class: "toast", "data-tone": tone, role: "status" }, [
    el("div", {}, [title ? el("strong", { text: title }) : null, el("span", { text: message })]),
  ]);
  stack.append(node);
  const remove = () => {
    node.classList.add("is-leaving");
    setTimeout(() => node.remove(), 220);
  };
  node.addEventListener("click", remove);
  if (timeout) setTimeout(remove, timeout);
}

/** Ripple feedback on button press. */
export function attachRipples(root = document) {
  root.addEventListener("pointerdown", (event) => {
    const button = event.target.closest(".btn");
    if (!button || button.disabled) return;
    const rect = button.getBoundingClientRect();
    const size = Math.max(rect.width, rect.height);
    const ripple = el("span", { class: "ripple" });
    ripple.style.width = `${size}px`;
    ripple.style.height = `${size}px`;
    ripple.style.left = `${event.clientX - rect.left - size / 2}px`;
    ripple.style.top = `${event.clientY - rect.top - size / 2}px`;
    button.append(ripple);
    setTimeout(() => ripple.remove(), 620);
  });
}

const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/**
 * Tween a numeric readout. Falls back to an immediate set when motion is reduced so the
 * displayed value is always the true value, never an animation artifact.
 */
export function setMetric(node, value, formatter = fmt.count) {
  if (!node) return;
  const target = Number(value);
  if (!Number.isFinite(target)) {
    node.textContent = "—";
    return;
  }
  const previous = Number(node.dataset.value);
  node.dataset.value = String(target);
  if (REDUCED_MOTION || !Number.isFinite(previous) || previous === target) {
    node.textContent = formatter(target);
    if (Number.isFinite(previous) && previous !== target) flash(node);
    return;
  }
  flash(node);
  const duration = 520;
  const start = performance.now();
  const step = (now) => {
    const progress = Math.min((now - start) / duration, 1);
    const eased = 1 - Math.pow(1 - progress, 3);
    node.textContent = formatter(previous + (target - previous) * eased);
    if (progress < 1) requestAnimationFrame(step);
    else node.textContent = formatter(target);
  };
  requestAnimationFrame(step);
}

function flash(node) {
  node.classList.remove("value-changed");
  void node.offsetWidth;
  node.classList.add("value-changed");
}

export function statusTone(status) {
  if (status === "healthy") return "positive";
  if (status === "degraded") return "warning";
  if (status === "down") return "critical";
  return "muted";
}

export function severityTone(severity) {
  if (severity === "critical") return "critical";
  if (severity === "high") return "warning";
  if (severity === "medium") return "info";
  return "muted";
}

export function verdictTone(verdict) {
  if (verdict === "supported") return "positive";
  if (verdict === "contradicted") return "critical";
  return "warning";
}

export function skeletonRows(count, columns) {
  return Array.from({ length: count }, () =>
    el(
      "tr",
      {},
      Array.from({ length: columns }, () => el("td", {}, [el("span", { class: "skeleton", text: "0000" })]))
    )
  );
}

export function emptyState(message, hint = null) {
  return el("div", { class: "empty" }, [
    el("span", {
      html:
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="9"/><path d="M9 12h6"/></svg>',
    }),
    el("span", { text: message }),
    hint ? el("span", { class: "eyebrow", text: hint }) : null,
  ]);
}
