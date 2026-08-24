/**
 * Auto-reconnecting WebSocket with exponential backoff and jitter.
 * On every (re)connect it replays a full REST refresh so no state is silently stale.
 */

import { session } from "./api.js";
import { setState } from "./state.js";

const MAX_DELAY_MS = 15000;
const BASE_DELAY_MS = 600;

export class LiveFeed {
  constructor({ onMessage, onResync }) {
    this.onMessage = onMessage;
    this.onResync = onResync;
    this.socket = null;
    this.attempt = 0;
    this.stopped = false;
    this.timer = null;
  }

  start() {
    this.stopped = false;
    this.open();
    document.addEventListener("visibilitychange", this.handleVisibility);
    window.addEventListener("online", this.handleOnline);
  }

  stop() {
    this.stopped = true;
    clearTimeout(this.timer);
    document.removeEventListener("visibilitychange", this.handleVisibility);
    window.removeEventListener("online", this.handleOnline);
    if (this.socket) {
      this.socket.onclose = null;
      this.socket.close();
      this.socket = null;
    }
  }

  handleVisibility = () => {
    if (document.visibilityState === "visible" && this.socket?.readyState !== WebSocket.OPEN) {
      this.reconnectNow();
    }
  };

  handleOnline = () => this.reconnectNow();

  reconnectNow() {
    clearTimeout(this.timer);
    this.attempt = 0;
    this.open();
  }

  open() {
    if (this.stopped || !session.token) return;
    if (this.socket && this.socket.readyState <= WebSocket.OPEN) return;

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${protocol}//${window.location.host}/api/ws?token=${encodeURIComponent(session.token)}`;
    setState({ connection: this.attempt === 0 ? "connecting" : "reconnecting" });

    let socket;
    try {
      socket = new WebSocket(url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;

    socket.onopen = () => {
      this.attempt = 0;
      setState({ connection: "live" });
      // Replay authoritative state after any gap in the stream.
      this.onResync?.();
    };

    socket.onmessage = (event) => {
      try {
        this.onMessage?.(JSON.parse(event.data));
      } catch (error) {
        console.warn("unparseable live message", error);
      }
    };

    socket.onerror = () => socket.close();

    socket.onclose = () => {
      this.socket = null;
      if (this.stopped) return;
      setState({ connection: "reconnecting" });
      this.scheduleReconnect();
    };
  }

  scheduleReconnect() {
    this.attempt += 1;
    if (this.attempt > 8) setState({ connection: "offline" });
    const backoff = Math.min(BASE_DELAY_MS * 2 ** (this.attempt - 1), MAX_DELAY_MS);
    const jitter = Math.random() * backoff * 0.35;
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.open(), backoff + jitter);
  }
}
