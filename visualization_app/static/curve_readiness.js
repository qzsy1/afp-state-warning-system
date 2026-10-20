(function (root) {
  "use strict";
  function inputTime(event, clock) {
    const now = clock.now();
    const raw = Number(event?.timeStamp);
    const candidates = [raw, raw - clock.timeOrigin];
    const timestamp = candidates.find(value => Number.isFinite(value) && value >= 0 && value <= now + 1);
    return {timestamp, handler: now, trusted: event?.isTrusted === true,
      valid: event?.isTrusted === true && timestamp !== undefined};
  }
  class CurveTrace {
    constructor(event, config, {clock = performance, id = crypto.randomUUID(), handlerEntered} = {}) {
      this.clock = clock;
      this.input = inputTime(event, clock);
      if (Number.isFinite(handlerEntered)) this.input.handler = handlerEntered;
      this.id = id;
      this.config = JSON.parse(JSON.stringify(config));
      const routed = new Set(Object.entries(config.interface_channel_assignments || {})
        .filter(([id]) => (config.interfaces || []).some(i => i.id === id && i.enabled !== false))
        .flatMap(([, names]) => names));
      this.channels = (config.selected_sensors || []).filter(name => routed.has(name));
      this.generation = "";
      this.events = [];
      this.drawn = new Map();
      this.proofs = new Map();
      this.presentations = new Map();
      this.complete = false;
      this.event("C_handler", {input_queue_ms: this.input.handler - this.input.timestamp});
    }
    event(stage, detail = {}) {
      if (this.events.length >= 256 && stage !== "C2") {
        const replace = this.events.findIndex(event => event.stage === "C2");
        if (replace >= 0) this.events.splice(replace, 1);
      }
      if (this.events.length < 256) this.events.push({stage, client_monotonic_ms: this.clock.now(), ...detail});
    }
    accepts(status) {
      if (status?.start_request_id !== this.id) return false;
      if (status.capture_generation) {
        if (this.generation && this.generation !== status.capture_generation) return false;
        this.generation = status.capture_generation;
      }
      return true;
    }
    received(payload) {
      if (!this.accepts(payload?.acquisition)) return false;
      if (!this.complete) this.event("C2", {capture_generation: this.generation, server_transport: payload.curve_transport});
      return true;
    }
    drawnChannel(channel, canvas, visible) {
      const channelName = channel.source_channel_name || channel.name;
      if (this.complete || this.drawn.has(channelName)) return false;
      if (!this.generation || !visible || !canvas?.isConnected || canvas.clientWidth <= 0 || canvas.clientHeight <= 0
          || !this.channels.includes(channelName)) return false;
      const observations = new Map();
      (channel.source_observations || []).forEach((source, index) => {
        if (source?.quality !== "measured_new" || source.after_start !== true || source.protocol_ok === false || source.stable === false
            || source.capture_generation !== this.generation || source.start_request_id !== this.id
            || source.source_sequence == null || !Number.isFinite(source.received_monotonic)
            || !Number.isFinite(channel.actual?.[index]) || !Number.isFinite(channel.x_observed?.[index])) return;
        observations.set(`${source.interface_id}:${source.source_sequence}:${source.received_monotonic}`, source);
      });
      if (observations.size < 2) return false;
      this.proofs.set(channelName, [...observations.values()].slice(-2));
      return true;
    }
    confirmDisplayed(names, visible, presentation = {}) {
      if (!visible || !this.input.valid) return;
      const at = this.clock.now();
      for (const name of names) if (this.proofs.has(name) && !this.drawn.has(name)) {
        this.drawn.set(name, at);
        this.presentations.set(name, {...presentation, displayed_at: at});
      }
      if (this.channels.length && this.channels.every(name => this.drawn.has(name)) && !this.complete) {
        this.complete = true;
        this.event("C3_all", {elapsed_ms: Math.max(...this.drawn.values()) - this.input.timestamp});
      }
    }
    snapshot() {
      const elapsed = this.complete ? Math.max(...this.drawn.values()) - this.input.timestamp : null;
      return {start_request_id: this.id, capture_generation: this.generation,
        clock_domain: "client_performance_ms", input: this.input,
        selected_channels: this.channels, complete: this.complete,
        elapsed_ms: elapsed, target_ms: 3000, maximum_ms: 3000,
        target_pass: this.complete && elapsed <= 3000,
        pass: this.complete && elapsed <= 3000,
        pending_channels: this.channels.filter(name => !this.drawn.has(name)),
        displayed: Object.fromEntries(this.drawn), observations: Object.fromEntries(this.proofs),
        presentations: Object.fromEntries(this.presentations), events: this.events};
    }
  }
  root.AFP_CURVE_READINESS = {inputTime, CurveTrace};
  if (typeof module !== "undefined") module.exports = root.AFP_CURVE_READINESS;
})(typeof window === "undefined" ? globalThis : window);
