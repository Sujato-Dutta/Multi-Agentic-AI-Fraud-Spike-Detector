/** Landing page: sign-in modal, authentication, and the hero video player. */

import { api, session } from "./api.js";
import { toast } from "./ui.js";

if (session.isAuthenticated) {
  window.location.replace("/pages/dashboard.html");
}

/* Sign-in modal ------------------------------------------------------------ */
const modal = document.getElementById("signin-modal");
const usernameInput = document.getElementById("username");
const passwordInput = document.getElementById("password");
let lastFocus = null;

function openSignin() {
  lastFocus = document.activeElement;
  modal.hidden = false;
  document.body.style.overflow = "hidden";
  window.requestAnimationFrame(() => usernameInput.focus());
}

function closeSignin() {
  modal.hidden = true;
  document.body.style.overflow = "";
  if (lastFocus instanceof HTMLElement) lastFocus.focus();
}

document.querySelectorAll("[data-open-signin]").forEach((trigger) => {
  trigger.addEventListener("click", openSignin);
});

document.querySelectorAll("[data-close-signin]").forEach((trigger) => {
  trigger.addEventListener("click", closeSignin);
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !modal.hidden) closeSignin();
});

/* Role quick-fill + authentication ---------------------------------------- */
const form = document.getElementById("login-form");
const status = document.getElementById("login-status");
const submit = document.getElementById("login-submit");

/* Seeded demo logins are served only in a local/dev environment and read from .env,
   so no credential is ever baked into the shipped frontend. */
let demoCredentials = null;
function loadDemoCredentials() {
  if (!demoCredentials) {
    demoCredentials = fetch("/api/auth/demo-credentials", { cache: "no-store" })
      .then((response) => (response.ok ? response.json() : null))
      .catch(() => null);
  }
  return demoCredentials;
}

document.querySelectorAll(".role-chip").forEach((chip) => {
  chip.addEventListener("click", async () => {
    document.querySelectorAll(".role-chip").forEach((other) => {
      other.setAttribute("aria-pressed", String(other === chip));
    });
    const role = chip.dataset.role;
    const creds = await loadDemoCredentials();
    const entry = creds && creds.roles ? creds.roles[role] : null;
    if (entry) {
      usernameInput.value = entry.username;
      passwordInput.value = entry.password;
      status.textContent = "Demo credentials filled from your local environment.";
    } else {
      usernameInput.value = role;
      passwordInput.focus();
    }
  });
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const username = usernameInput.value.trim();
  const password = passwordInput.value;
  if (!username || !password) {
    status.textContent = "Enter both a username and a password.";
    return;
  }

  submit.disabled = true;
  submit.innerHTML = "";
  submit.append(Object.assign(document.createElement("span"), { className: "spinner" }));
  submit.append(Object.assign(document.createElement("span"), { textContent: "Authenticating" }));
  status.textContent = "Verifying credentials against the API…";

  try {
    const result = await api.login(username, password);
    status.textContent = `Signed in as ${username} (${result.role}). Opening command center…`;
    document.body.style.transition = "opacity 240ms ease";
    document.body.style.opacity = "0";
    setTimeout(() => window.location.assign("/pages/dashboard.html"), 220);
  } catch (error) {
    submit.disabled = false;
    submit.innerHTML = "<span>Enter command center</span>";
    status.textContent = error.detail || "Sign-in failed.";
    toast(error.detail || "Sign-in failed", { tone: "critical", title: "Authentication" });
  }
});

/* Hero video: always autoplay on a muted loop; nudge play() if the attribute is ignored. */
const video = document.getElementById("demo-video");
if (video) {
  const ensurePlaying = () => {
    video.muted = true;
    const attempt = video.play();
    if (attempt && typeof attempt.catch === "function") attempt.catch(() => {});
  };
  video.addEventListener("loadedmetadata", ensurePlaying);
  video.addEventListener("canplay", ensurePlaying);
  ensurePlaying();
}
