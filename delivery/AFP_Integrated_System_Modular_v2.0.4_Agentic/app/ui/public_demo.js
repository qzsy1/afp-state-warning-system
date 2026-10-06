"use strict";

(function publicDemoModule(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.AFP_PUBLIC_DEMO = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createPublicDemoApi() {
  const WINDOW_SIZE = 24;
  const displayRangeCache = new WeakMap();

  function finite(value, fallback = null) {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : fallback;
  }

  function clampIndex(bundle, index) {
    const points = Math.max(1, Number(bundle?.points || 1));
    return Math.max(0, Math.min(points - 1, Math.floor(Number(index) || 0)));
  }

  function validateBundle(bundle) {
    if (!bundle || bundle.schema_version !== "afp-public-demo-v1"
        || bundle.synthetic !== true || bundle.precomputed !== true) {
      throw new Error("预计算演示包标识无效");
    }
    const points = Number(bundle.points || 0);
    const rate = Number(bundle.sample_rate_hz || 0);
    if (!Number.isInteger(points) || points <= 0 || !Number.isFinite(rate) || rate <= 0) {
      throw new Error("预计算演示包的点数或采样率无效");
    }
    if (!Array.isArray(bundle.channels) || bundle.channels.length !== 17
        || !Array.isArray(bundle.interfaces) || bundle.interfaces.length !== 5) {
      throw new Error("预计算演示包的接口或通道数量无效");
    }
    for (const channel of bundle.channels) {
      if (!Array.isArray(channel.actual) || channel.actual.length !== points
          || !Array.isArray(channel.predicted) || channel.predicted.length !== points) {
        throw new Error(`预计算演示通道 ${channel?.name || channel?.id || "未知"} 长度无效`);
      }
    }
    return bundle;
  }

  function aggregateEvidence(windows) {
    if (!windows.length) return null;
    const health = windows.reduce((total, item) => total + finite(item.health, 1), 0) / windows.length;
    const mostSevere = windows.reduce((selected, item) => {
      const severity = {normal: 0, warning: 1, abnormal: 2};
      return (severity[item.state] || 0) >= (severity[selected.state] || 0) ? item : selected;
    }, windows[0]);
    return {
      state: mostSevere.state,
      state_label: mostSevere.state_label,
      health,
      evidence_count: windows.length,
      evidence_layers: 1,
      actual_layer_count: 1,
      effective_count: windows.length,
      maximum_weight: 1 / windows.length,
    };
  }

  function stableDisplayRanges(bundle) {
    const cached = displayRangeCache.get(bundle);
    if (cached) return cached;
    const ranges = new Map();
    for (const channel of bundle.channels || []) {
      const values = [...(channel.actual || []), ...(channel.predicted || [])]
        .map(Number).filter(Number.isFinite);
      let minimum = values.length ? Math.min(...values) : 0;
      let maximum = values.length ? Math.max(...values) : 1;
      const margin = Math.max((maximum - minimum) * 0.08, Math.abs(maximum) * 0.01, 1e-6);
      minimum -= margin;
      maximum += margin;
      if (minimum >= maximum) maximum = minimum + 1;
      ranges.set(String(channel.id || channel.name || ranges.size), {
        min: minimum,
        max: maximum,
      });
    }
    displayRangeCache.set(bundle, ranges);
    return ranges;
  }

  function buildRealtimePayload(rawBundle, index, options = {}) {
    const bundle = validateBundle(rawBundle);
    const safeIndex = clampIndex(bundle, index);
    const cursor = safeIndex + 1;
    const history = Math.max(1, Math.floor(Number(options.history || 240)));
    const horizon = Math.max(1, Math.floor(Number(options.horizon || 24)));
    const historyStart = Math.max(0, cursor - history);
    const selectedKey = String(options.selectedChannel || "");
    const displayRanges = stableDisplayRanges(bundle);
    const channels = bundle.channels.map((source, channelIndex) => {
      const actual = source.actual.slice(historyStart, cursor);
      const predictionObserved = source.predicted.slice(historyStart, cursor);
      const futureEnd = Math.min(bundle.points, cursor + horizon);
      const predictionFuture = source.predicted.slice(cursor, futureEnd);
      const xObserved = actual.map((_value, position) => position - actual.length + 1);
      const xFuture = predictionFuture.map((_value, position) => position + 1);
      const residuals = actual.map((value, position) => {
        const predicted = predictionObserved[position];
        return Number.isFinite(value) && Number.isFinite(predicted) ? value - predicted : null;
      }).filter(Number.isFinite);
      const rmse = residuals.length
        ? Math.sqrt(residuals.reduce((total, value) => total + value * value, 0) / residuals.length)
        : null;
      return {
        id: String(source.id || channelIndex),
        name: source.name,
        unit: source.unit || "",
        interface_id: source.interface_id,
        actual,
        x_observed: xObserved,
        actual_current: actual.at(-1),
        prediction_observed: predictionObserved,
        prediction_future: predictionFuture,
        x_future: xFuture,
        prediction_current: predictionObserved.at(-1),
        prediction_enabled: true,
        rmse,
        display_range: displayRanges.get(String(source.id || source.name || channelIndex)),
      };
    });
    const selected = channels.find((channel) => channel.id === selectedKey
      || channel.name === selectedKey) || channels[0];
    const totalWindows = Math.max(1, Math.ceil(bundle.points / WINDOW_SIZE));
    const windowIndex = Math.min(totalWindows - 1, Math.floor(safeIndex / WINDOW_SIZE));
    const sourceWindow = bundle.window_evidence?.[windowIndex] || {};
    const windowComplete = cursor >= Math.min(bundle.points, (windowIndex + 1) * WINDOW_SIZE);
    const currentWindow = {
      ...sourceWindow,
      id: sourceWindow.id || `W${String(windowIndex + 1).padStart(4, "0")}`,
      complete: windowComplete,
      score: windowComplete ? finite(sourceWindow.score, 0) : null,
      raw_realtime_score: windowComplete ? finite(sourceWindow.score, 0) : null,
      state: windowComplete ? (sourceWindow.state || "normal") : "pending",
      state_label: windowComplete ? (sourceWindow.state_label || "正常") : "等待完整窗口",
      optimized_warning_applied: false,
      type_probabilities: windowComplete
        ? (sourceWindow.type_probabilities || {normal: 1, warning: 0, abnormal: 0})
        : {normal: 0, warning: 0, abnormal: 0},
    };
    const completedCount = Math.floor(cursor / WINDOW_SIZE)
      + (cursor === bundle.points && bundle.points % WINDOW_SIZE ? 1 : 0);
    const completedWindows = (bundle.window_evidence || []).slice(0, completedCount);
    const aggregate = aggregateEvidence(completedWindows);
    const scores = Array.from({length: totalWindows}, (_item, position) => (
      position < completedCount ? finite(bundle.window_evidence?.[position]?.score, 0) : null
    ));
    const finished = cursor >= bundle.points;
    const acquisition = {
      acquisition_mode: "simulation",
      execution_host: "browser_precomputed_demo",
      running: !finished,
      sample_count: cursor,
      total_points: bundle.points,
      model_ready: true,
      capture_saved: false,
      synthetic: true,
      precomputed: true,
      interfaces: bundle.interfaces.map((item) => ({
        ...item,
        selected: true,
        ok: true,
        state: "precomputed_success",
        message: "预计算模拟接口已就绪（未连接物理设备）",
      })),
      sensors: bundle.channels.map((channel) => ({
        name: channel.name,
        selected: true,
        ok: true,
        state: "precomputed_success",
        message: "预计算模拟通道已就绪",
      })),
      config: {
        acquisition_mode: "simulation",
        processing_mode: "acquire_predict",
        selected_sensors: bundle.channels.map((channel) => channel.name),
        model_input_sensors: bundle.channels.map((channel) => channel.name),
      },
      save: {enabled: false, state: "disabled", reason: "public_precomputed_demo"},
      mysql: {enabled: false, state: "disabled", saved_rows: 0},
    };
    return {
      mode: "public_precomputed_demo",
      progress: {
        cursor,
        total_points: bundle.points,
        current_layer: 1,
        current_window: windowIndex + 1,
        total_windows_in_layer: totalWindows,
        sample_in_window: ((cursor - 1) % WINDOW_SIZE) + 1,
        finished,
      },
      selection: {
        sensor: selected.id,
        prediction_horizon: horizon,
        threshold: finite(options.threshold, 0.72),
        rho: finite(options.rho, 0.35),
        best_prediction_override: false,
      },
      channels,
      selected_channel: selected,
      window: currentWindow,
      layer: aggregate,
      specimen: aggregate ? {...aggregate, evidence_layers: 1, actual_layer_count: 1} : null,
      layers: [{
        display_layer: 1,
        status: finished ? "complete" : "active",
        completed_windows: completedCount,
        total_windows: totalWindows,
        aggregate,
      }],
      timeline: {
        scores,
        threshold: finite(options.threshold, 0.72),
        active_index: windowIndex,
        completed_count: completedCount,
      },
      forecast: {mode: "precomputed_synthetic_demo", requested_horizon: horizon},
      process: bundle.process,
      feature_generation: {
        mode: "precomputed_synthetic_demo",
        health_indicator_output_sensors: bundle.channels.map((channel) => channel.name),
        indicator_variant: {
          variant_id: "PRECOMPUTED-DEMO",
          construction: "预计算 synthetic 演示结果，不调用运行时模型",
          required_outputs: bundle.channels.map((channel) => channel.name),
        },
      },
      candidate: {
        indicator: "预计算演示",
        model: "logistic",
        recommended: true,
        validation_selection_score: 1,
        validation_window_balanced_accuracy: 1,
        validation_layer_balanced_accuracy: 1,
        validation_specimen_balanced_accuracy: 1,
      },
      official_final: {true_state_label: "synthetic 演示，无真实真值"},
      diagnosis: {
        ...bundle.diagnosis,
        ok: true,
        state: "precomputed_success",
      },
      evidence_scope: bundle.evidence_scope,
      acquisition,
    };
  }

  function createPlaybackController(options = {}) {
    const now = options.now || (() => performance.now());
    const setTimeoutFn = options.setTimeoutFn || ((callback, delay) => window.setTimeout(callback, delay));
    const clearTimeoutFn = options.clearTimeoutFn || ((handle) => window.clearTimeout(handle));
    const requestAnimationFrameFn = options.requestAnimationFrameFn
      || ((callback) => window.requestAnimationFrame(callback));
    const cancelAnimationFrameFn = options.cancelAnimationFrameFn
      || ((handle) => window.cancelAnimationFrame(handle));
    const render = options.render || (() => {});
    const onState = options.onState || (() => {});
    const renderIntervalMs = Math.max(200, Math.min(500, Number(options.renderIntervalMs || 250)));

    let bundle = null;
    let generation = 0;
    let running = false;
    let visible = true;
    let index = -1;
    let startedAt = 0;
    let pausedAt = null;
    let timerId = null;
    let frameId = null;

    const snapshot = () => ({
      ready: Boolean(bundle),
      running,
      visible,
      index,
      generation,
      points: Number(bundle?.points || 0),
      sample_rate_hz: Number(bundle?.sample_rate_hz || 0),
      timer_active: timerId !== null,
      frame_active: frameId !== null,
    });

    const publishState = () => onState(snapshot());

    const clearTasks = () => {
      if (timerId !== null) clearTimeoutFn(timerId);
      if (frameId !== null) cancelAnimationFrameFn(frameId);
      timerId = null;
      frameId = null;
    };

    const commit = (nextIndex, expectedGeneration, immediate = false) => {
      if (!running || expectedGeneration !== generation) return;
      const apply = () => {
        if (!running || expectedGeneration !== generation) return;
        index = nextIndex;
        render(nextIndex, bundle, snapshot());
        publishState();
      };
      if (immediate) {
        apply();
        return;
      }
      let handle = null;
      handle = requestAnimationFrameFn(() => {
        if (frameId === handle) frameId = null;
        apply();
      });
      frameId = handle || null;
    };

    const schedule = (expectedGeneration) => {
      if (!running || !visible || expectedGeneration !== generation) return;
      let handle = null;
      handle = setTimeoutFn(() => {
        clearTimeoutFn(handle);
        if (timerId === handle) timerId = null;
        if (!running || !visible || expectedGeneration !== generation) return;
        const rate = Math.max(0.1, Number(bundle.sample_rate_hz || 10));
        const elapsed = Math.max(0, now() - startedAt);
        const nextIndex = Math.min(Number(bundle.points) - 1, Math.floor(elapsed * rate / 1000));
        if (nextIndex !== index) {
          commit(nextIndex, expectedGeneration, nextIndex >= Number(bundle.points) - 1);
        }
        if (nextIndex >= Number(bundle.points) - 1) {
          running = false;
          publishState();
          return;
        }
        schedule(expectedGeneration);
      }, renderIntervalMs);
      timerId = handle;
    };

    const stop = () => {
      generation += 1;
      running = false;
      pausedAt = null;
      clearTasks();
      publishState();
      return snapshot();
    };

    const load = (nextBundle) => {
      stop();
      const points = Number(nextBundle?.points || 0);
      const rate = Number(nextBundle?.sample_rate_hz || 0);
      if (!Number.isInteger(points) || points <= 0 || !Number.isFinite(rate) || rate <= 0) {
        throw new Error("预计算演示包的点数或采样率无效");
      }
      bundle = nextBundle;
      index = -1;
      publishState();
      return snapshot();
    };

    const start = () => {
      if (!bundle) throw new Error("预计算演示包尚未准备完成");
      clearTasks();
      generation += 1;
      const currentGeneration = generation;
      running = true;
      visible = true;
      pausedAt = null;
      index = -1;
      startedAt = now();
      commit(0, currentGeneration, true);
      if (Number(bundle.points) > 1) schedule(currentGeneration);
      else running = false;
      publishState();
      return snapshot();
    };

    const setVisible = (nextVisible) => {
      const requested = Boolean(nextVisible);
      if (requested === visible) return snapshot();
      visible = requested;
      if (!running) {
        publishState();
        return snapshot();
      }
      if (!visible) {
        pausedAt = now();
        clearTasks();
      } else {
        if (pausedAt !== null) startedAt += Math.max(0, now() - pausedAt);
        pausedAt = null;
        schedule(generation);
      }
      publishState();
      return snapshot();
    };

    return {load, start, stop, setVisible, snapshot};
  }

  return {buildRealtimePayload, createPlaybackController, validateBundle};
});
