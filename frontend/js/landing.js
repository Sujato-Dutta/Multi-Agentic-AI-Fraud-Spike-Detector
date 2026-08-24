/** Landing page: role quick-fill, authentication, and the video/animated preview swap. */

import { api, session } from "./api.js";
import { attachRipples, toast } from "./ui.js";

const form = document.getElementById("login-form");
const status = document.getElementById("login-status");
const submit = document.getElementById("login-submit");
const usernameInput = document.getElementById("username");
const passwordInput = document.getElementById("password");

attachRipples(document);

if (session.isAuthenticated) {
  window.location.replace("/pages/dashboard.html");
}

document.querySelectorAll(".role-chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    document.querySelectorAll(".role-chip").forEach((other) => {
      other.setAttribute("aria-pressed", String(other === chip));
    });
    usernameInput.value = chip.dataset.username;
    passwordInput.focus();
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

/* Use a real demo clip when one is committed; otherwise keep the animated preview. */
const video = document.getElementById("demo-video");
const fallback = document.getElementById("showcase-fallback");
const caption = document.getElementById("showcase-caption");

fetch("/assets/videos/demo.mp4", { method: "HEAD" })
  .then((response) => {
    if (!response.ok) return;
    video.hidden = false;
    fallback.hidden = true;
    caption.textContent = "Recorded product walkthrough";
    video.play().catch(() => {
      /* autoplay blocked; the poster frame still reads well */
    });
  })
  .catch(() => {
    /* no clip committed: the animated preview stays, which is the honest default */
  });
