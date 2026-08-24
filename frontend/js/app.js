/**
 * Common page bootstrap. Each page supplies its chrome copy, a build() that renders
 * static structure, a load() that fetches data, and an optional live handler.
 */

import { mountShell, refreshShared, requireSession, startLiveFeed, startPolling } from "./shell.js";
import { recordError } from "./state.js";
import { toast } from "./ui.js";

export function bootstrap({ title, subtitle, actions = [], build, load, onLive, pollMs = 12000 }) {
  if (!requireSession()) return;

  const main = mountShell({ title, subtitle, actions });
  build?.(main);

  const runLoad = async () => {
    try {
      await load?.();
    } catch (error) {
      recordError(error);
      if (error.code === "session_expired") {
        window.location.replace("/index.html");
        return;
      }
      toast(error.detail || "Could not load this view", { tone: "critical", title: "Load failed" });
    }
  };

  startPolling(() => {
    refreshShared();
    runLoad();
  }, pollMs);

  startLiveFeed({ onMessage: onLive, onResync: runLoad });
  return main;
}
