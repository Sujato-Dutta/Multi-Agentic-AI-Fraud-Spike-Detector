/**
 * Dependency-free canvas charts.
 *
 * Deliberate deviation from the plan's "vendored Chart.js UMD": shipping a real
 * third-party bundle is not something this build can honestly fabricate offline, and the
 * chart surface needed here (animated area/line with shaded event windows, sparklines,
 * horizontal bars) is small. This renderer is ~250 lines, has no dependency, works
 * offline, is DPI-aware, and honours prefers-reduced-motion.
 */

const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function readVar(name, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

function setupCanvas(canvas) {
  const ratio = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(rect.width, 1);
  const height = Math.max(rect.height, 1);
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return { ctx, width, height };
}

function easeOut(t) {
  return 1 - Math.pow(1 - t, 3);
}

/** Animate a 0..1 progress value; resolves immediately when motion is reduced. */
function animate(duration, onFrame) {
  if (REDUCED_MOTION || duration <= 0) {
    onFrame(1);
    return () => {};
  }
  let raf = 0;
  const start = performance.now();
  const step = (now) => {
    const progress = Math.min((now - start) / duration, 1);
    onFrame(easeOut(progress));
    if (progress < 1) raf = requestAnimationFrame(step);
  };
  raf = requestAnimationFrame(step);
  return () => cancelAnimationFrame(raf);
}

function niceMax(value) {
  if (!Number.isFinite(value) || value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const scaled = value / magnitude;
  const step = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
  return step * magnitude;
}

/**
 * Risk trend: primary area series (risk density), optional secondary line (volume),
 * dashed threshold, and shaded incident / promo windows.
 */
export function renderTrend(canvas, options) {
  const {
    series = [],
    secondary = [],
    labels = [],
    shades = [],
    threshold = null,
    formatValue = (v) => v.toFixed(2),
  } = options;

  if (canvas._cancelAnimation) canvas._cancelAnimation();
  const padding = { top: 16, right: 14, bottom: 24, left: 44 };
  const accent = readVar("--accent", "#37d6c4");
  const accentBright = readVar("--accent-bright", "#5ff0dd");
  const info = readVar("--info", "#4aa8ff");
  const grid = "rgba(148, 168, 214, 0.10)";
  const axisText = readVar("--text-tertiary", "#6f7d9c");

  const maxPrimary = niceMax(Math.max(...series, threshold ?? 0, 0.0001));
  const maxSecondary = niceMax(Math.max(...secondary, 0.0001));

  const draw = (progress) => {
    const { ctx, width, height } = setupCanvas(canvas);
    const plotW = Math.max(width - padding.left - padding.right, 1);
    const plotH = Math.max(height - padding.top - padding.bottom, 1);
    const xAt = (index) =>
      padding.left + (series.length <= 1 ? plotW / 2 : (index / (series.length - 1)) * plotW);
    const yAt = (value, max) => padding.top + plotH - (Math.min(value, max) / max) * plotH;

    // Shaded windows first so lines sit above them.
    shades.forEach((shade) => {
      const from = xAt(Math.max(shade.from, 0));
      const to = xAt(Math.min(shade.to, series.length - 1));
      if (!Number.isFinite(from) || !Number.isFinite(to) || to <= from) return;
      ctx.fillStyle = shade.fill;
      ctx.fillRect(from, padding.top, Math.max(to - from, 2), plotH);
      if (shade.label) {
        ctx.fillStyle = shade.stroke || shade.fill;
        ctx.font = `600 9px ${readVar("--font-mono", "monospace")}`;
        ctx.fillText(shade.label, from + 4, padding.top + 11);
      }
    });

    // Horizontal grid + y axis labels
    ctx.strokeStyle = grid;
    ctx.lineWidth = 1;
    ctx.fillStyle = axisText;
    ctx.font = `500 9px ${readVar("--font-mono", "monospace")}`;
    ctx.textAlign = "right";
    for (let i = 0; i <= 4; i += 1) {
      const y = padding.top + (plotH / 4) * i;
      ctx.beginPath();
      ctx.moveTo(padding.left, y);
      ctx.lineTo(padding.left + plotW, y);
      ctx.stroke();
      ctx.fillText(formatValue(maxPrimary * (1 - i / 4)), padding.left - 6, y + 3);
    }

    if (threshold !== null) {
      const y = yAt(threshold, maxPrimary);
      ctx.save();
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = axisText;
      ctx.beginPath();
      ctx.moveTo(padding.left, y);
      ctx.lineTo(padding.left + plotW, y);
      ctx.stroke();
      ctx.restore();
    }

    if (!series.length) return;

    const visible = Math.max(Math.ceil(series.length * progress), 1);

    // Secondary volume line
    if (secondary.length === series.length) {
      ctx.strokeStyle = info;
      ctx.globalAlpha = 0.55;
      ctx.lineWidth = 1.4;
      ctx.beginPath();
      for (let i = 0; i < visible; i += 1) {
        const x = xAt(i);
        const y = yAt(secondary[i], maxSecondary);
        i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
      }
      ctx.stroke();
      ctx.globalAlpha = 1;
    }

    // Primary area
    const gradient = ctx.createLinearGradient(0, padding.top, 0, padding.top + plotH);
    gradient.addColorStop(0, "rgba(55, 214, 196, 0.34)");
    gradient.addColorStop(1, "rgba(55, 214, 196, 0.01)");
    ctx.beginPath();
    ctx.moveTo(xAt(0), padding.top + plotH);
    for (let i = 0; i < visible; i += 1) ctx.lineTo(xAt(i), yAt(series[i], maxPrimary));
    ctx.lineTo(xAt(visible - 1), padding.top + plotH);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();

    ctx.beginPath();
    for (let i = 0; i < visible; i += 1) {
      const x = xAt(i);
      const y = yAt(series[i], maxPrimary);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.strokeStyle = accent;
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.stroke();

    // Leading marker
    const lastX = xAt(visible - 1);
    const lastY = yAt(series[visible - 1], maxPrimary);
    ctx.beginPath();
    ctx.arc(lastX, lastY, 3.2, 0, Math.PI * 2);
    ctx.fillStyle = accentBright;
    ctx.fill();
    ctx.beginPath();
    ctx.arc(lastX, lastY, 7, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(95, 240, 221, 0.18)";
    ctx.fill();

    // X axis labels: first, middle, last
    if (labels.length === series.length) {
      ctx.fillStyle = axisText;
      ctx.font = `500 9px ${readVar("--font-mono", "monospace")}`;
      const positions = [0, Math.floor((series.length - 1) / 2), series.length - 1];
      positions.forEach((index, order) => {
        ctx.textAlign = order === 0 ? "left" : order === 1 ? "center" : "right";
        ctx.fillText(labels[index], xAt(index), padding.top + plotH + 14);
      });
    }
  };

  canvas._cancelAnimation = animate(760, draw);
  canvas._redraw = () => draw(1);
}

/** Compact sparkline used inside metric tiles. */
export function renderSparkline(canvas, values, tone = "accent") {
  if (!values.length) return;
  const colors = {
    accent: readVar("--accent", "#37d6c4"),
    critical: readVar("--critical", "#ff5470"),
    warning: readVar("--warning", "#ffb340"),
    ai: readVar("--ai", "#8b7dfb"),
    info: readVar("--info", "#4aa8ff"),
  };
  const color = colors[tone] || colors.accent;
  const draw = (progress) => {
    const { ctx, width, height } = setupCanvas(canvas);
    const max = Math.max(...values, 0.0001);
    const min = Math.min(...values, 0);
    const range = max - min || 1;
    const visible = Math.max(Math.ceil(values.length * progress), 2);
    const xAt = (i) => (i / (values.length - 1 || 1)) * width;
    const yAt = (v) => height - ((v - min) / range) * (height - 4) - 2;

    ctx.beginPath();
    ctx.moveTo(0, height);
    for (let i = 0; i < visible; i += 1) ctx.lineTo(xAt(i), yAt(values[i]));
    ctx.lineTo(xAt(visible - 1), height);
    ctx.closePath();
    const gradient = ctx.createLinearGradient(0, 0, 0, height);
    gradient.addColorStop(0, `${color}44`);
    gradient.addColorStop(1, `${color}00`);
    ctx.fillStyle = gradient;
    ctx.fill();

    ctx.beginPath();
    for (let i = 0; i < visible; i += 1) {
      const x = xAt(i);
      const y = yAt(values[i]);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.stroke();
  };
  if (canvas._cancelAnimation) canvas._cancelAnimation();
  canvas._cancelAnimation = animate(620, draw);
  canvas._redraw = () => draw(1);
}

/** Horizontal grouped bars for reward / cost comparisons. */
export function renderBars(canvas, rows) {
  if (!rows.length) return;
  const draw = (progress) => {
    const { ctx, width, height } = setupCanvas(canvas);
    const labelWidth = 116;
    const gap = 8;
    const barHeight = Math.min(18, (height - gap * (rows.length - 1)) / rows.length);
    const max = niceMax(Math.max(...rows.map((row) => Math.abs(row.value)), 0.0001));
    ctx.font = `500 10px ${readVar("--font-sans", "sans-serif")}`;
    rows.forEach((row, index) => {
      const y = index * (barHeight + gap);
      ctx.fillStyle = readVar("--text-secondary", "#a7b3cf");
      ctx.textAlign = "left";
      ctx.fillText(row.label, 0, y + barHeight / 2 + 3);

      const trackX = labelWidth;
      const trackW = Math.max(width - labelWidth - 66, 10);
      ctx.fillStyle = readVar("--surface-void", "#05070d");
      ctx.fillRect(trackX, y, trackW, barHeight);

      const ratio = Math.min(Math.abs(row.value) / max, 1) * progress;
      const gradient = ctx.createLinearGradient(trackX, 0, trackX + trackW, 0);
      gradient.addColorStop(0, row.color || readVar("--ai", "#8b7dfb"));
      gradient.addColorStop(1, row.colorEnd || readVar("--accent", "#37d6c4"));
      ctx.fillStyle = gradient;
      ctx.fillRect(trackX, y, trackW * ratio, barHeight);

      ctx.fillStyle = readVar("--text-primary", "#eef2fb");
      ctx.textAlign = "right";
      ctx.font = `600 10px ${readVar("--font-mono", "monospace")}`;
      ctx.fillText(row.display ?? String(row.value), width, y + barHeight / 2 + 3);
      ctx.font = `500 10px ${readVar("--font-sans", "sans-serif")}`;
    });
  };
  if (canvas._cancelAnimation) canvas._cancelAnimation();
  canvas._cancelAnimation = animate(700, draw);
  canvas._redraw = () => draw(1);
}

/** Redraw every chart on resize without replaying entrance animations. */
let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    document.querySelectorAll("canvas").forEach((canvas) => canvas._redraw?.());
  }, 140);
});
