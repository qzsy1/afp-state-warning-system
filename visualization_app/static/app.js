"use strict";

const state = {
  bootstrap: null,
  payload: null,
  defaults: null,
  playing: false,
  timer: null,
  requestTimer: null,
  livePollTimer: null,
  busy: false,
  reloadQueued: false,
  requestedHorizon: null,
  showResidual: false,
  manualPredictionModels: {},
};

const $ = (id) => document.getElementById(id);
const controls = {
  dataMode: $("dataModeSelect"),
  specimen: $("specimenSelect"),
  sensor: $("sensorSelect"),
  cursor: $("cursorInput"),
  speed: $("speedSelect"),
  streamStep: $("streamStepSelect"),
  history: $("lengthSelect"),
  step: $("stepSelect"),
  horizon: $("horizonInput"),
  horizonNumber: $("horizonNumber"),
  realtimePrediction: $("realtimePredictionInput"),
  loop: $("loopInput"),
  threshold: $("thresholdInput"),
  rho: $("rhoInput"),
  autoIndicator: $("autoIndicatorInput"),
  indicator: $("indicatorSelect"),
  model: $("modelSelect"),
  optimizedWarning: $("optimizedWarningInput"),
  bestPredictionOverride: $("bestPredictionOverrideInput"),
  processingMode: $("processingModeSelect"),
  datasetSchema: $("datasetSchemaSelect"),
  driver: $("driverSelect"),
  sourceFile: $("sourceFileInput"),
  endpoint: $("endpointInput"),
  baudrate: $("baudrateInput"),
  sampleRate: $("sampleRateInput"),
  runId: $("runIdInput"),
  liveSpecimen: $("liveSpecimenInput"),
  saveRoot: $("saveRootInput"),
  predictionModel: $("predictionModelInput"),
  livePower: $("livePowerInput"),
  liveSpeed: $("liveSpeedInput"),
  livePressure: $("livePressureInput"),
  liveLayer: $("liveLayerInput"),
  initialForce: $("initialForceInput"),
  placementSpeed: $("placementSpeedInput"),
  pidAngle: $("pidAngleInput"),
  temperatureSetpoint: $("temperatureSetpointInput"),
  conditionId: $("conditionIdInput"),
  replicate: $("replicateInput"),
  newLayer: $("newLayerInput"),
};

function option(value, text) {
  const node = document.createElement("option");
  node.value = value;
  node.textContent = text;
  return node;
}

function fmt(value, digits = 3) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(digits) : "—";
}

function toast(message) {
  const node = $("toast");
  node.textContent = message;
  node.classList.add("show");
  window.setTimeout(() => node.classList.remove("show"), 3500);
}

function selectedLiveSensors() {
  return [...document.querySelectorAll("#liveSensorChecklist .save-sensor-checkbox:checked")]
    .map((node) => node.value);
}

function selectedPredictionSensors() {
  return [...document.querySelectorAll("#liveSensorChecklist .predict-sensor-checkbox:checked")]
    .map((node) => node.value);
}

function selectedModelInputSensors() {
  return [...document.querySelectorAll("#liveSensorChecklist .model-input-sensor-checkbox:checked")]
    .map((node) => node.value);
}

function acquisitionConfig() {
  const newSchema = controls.datasetSchema.value === "new_collection_v11_3";
  return {
    processing_mode: controls.processingMode.value,
    dataset_schema: controls.datasetSchema.value || "legacy_original",
    use_best_prediction_override: controls.bestPredictionOverride.checked,
    driver: controls.driver.value,
    endpoint: controls.endpoint.value.trim(),
    baudrate: Number(controls.baudrate.value) || 115200,
    sample_rate_hz: Number(controls.sampleRate.value) || 10,
    selected_sensors: selectedLiveSensors(),
    prediction_sensors: selectedPredictionSensors(),
    model_input_sensors: selectedModelInputSensors(),
    model_output_sensors: selectedPredictionSensors(),
    prediction_model_file: controls.predictionModel.value.trim(),
    health_indicator: controls.indicator.value || "TC-HI",
    run_id: controls.runId.value.trim() || "LIVE_RUN",
    specimen_id: controls.liveSpecimen.value.trim() || "LIVE_SPECIMEN",
    condition_id: newSchema
      ? (controls.conditionId.value.trim() || "H06")
      : "LIVE",
    layer: Number(newSchema ? controls.newLayer.value : controls.liveLayer.value) || 0,
    cycle: 1,
    p: Number(controls.livePower.value) || 0,
    v: Number(controls.liveSpeed.value) || 0,
    pr: Number(controls.livePressure.value) || 0,
    root: "LIVE",
    source_file: controls.sourceFile.value.trim(),
    save_root: controls.saveRoot.value.trim(),
    initial_compaction_force_N: Number(controls.initialForce.value) || 0,
    placement_speed_mm_s: Number(controls.placementSpeed.value) || 0,
    pid_angle_deg: Number(controls.pidAngle.value) || 0,
    temperature_setpoint_C: Number(controls.temperatureSetpoint.value) || 0,
    replicate: Number(controls.replicate.value) || 1,
  };
}

async function postJson(url, payload = {}) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "请求失败");
  return result;
}

function renderAcquisitionStatus(status) {
  const node = $("acquisitionStatus");
  const selected = (status.sensors || []).filter((item) => item.selected);
  const healthy = selected.filter((item) => item.ok);
  const captureOnly = status.config?.processing_mode === "capture_only"
    || controls.processingMode.value === "capture_only";
  const expectedInputCount = status.config?.model_input_sensors?.length
    || selectedLiveSensors().length;
  node.classList.toggle("ok", status.running && !status.last_error);
  node.classList.toggle("error", Boolean(status.last_error));
  const readiness = captureOnly
    ? "仅采集保存，未加载预测模型"
    : status.model_ready
      ? "预测与预警输入已就绪"
      : `等待全部${expectedInputCount}个模型输入通道，预测至少24点、首次预警至少48点`;
  const predictionCount = selectedPredictionSensors().length;
  const locked = Boolean(status.running);
  controls.predictionModel.disabled =
    locked || controls.bestPredictionOverride.checked;
  $("selectPredictionModelButton").disabled =
    locked || controls.bestPredictionOverride.checked;
  document.querySelectorAll("#liveSensorChecklist input").forEach((input) => {
    const collectCheckbox = input.classList.contains("save-sensor-checkbox");
    const rowCollect = input.closest(".sensor-checklist-row")
      ?.querySelector(".save-sensor-checkbox");
    input.disabled = locked || (
      !collectCheckbox
      && (captureOnly || !rowCollect?.checked)
    );
  });
  node.textContent =
    `${status.running ? "采集中" : "已停止"} · ${status.sample_count || 0}点 · ` +
    `${captureOnly
      ? `采集通道 ${healthy.length}/${selected.length} 正常`
      : `模型输入 ${expectedInputCount}通道 · 模型输出 ${predictionCount}通道`} · ${readiness}` +
    `${status.layer_file ? ` · 分层文件：${status.layer_file}` : ""}` +
    `${status.full_specimen_file ? ` · 完整试样：${status.full_specimen_file}` : ""}` +
    `${status.last_error ? ` · 错误：${status.last_error}` : ""}`;
}

function applyPredictionModelProfile(profile, setSelections = true) {
  if (!profile) return;
  controls.predictionModel.value = profile.checkpoint || "";
  const inputSet = new Set(profile.input_sensors || []);
  const outputSet = new Set(profile.output_sensors || []);
  if (setSelections) {
    document.querySelectorAll(".save-sensor-checkbox").forEach((input) => {
      input.checked = input.checked || inputSet.has(input.value);
    });
    document.querySelectorAll(".model-input-sensor-checkbox").forEach((input) => {
      input.checked = inputSet.has(input.value);
      input.disabled = !input.closest(".sensor-checklist-row")
        ?.querySelector(".save-sensor-checkbox")?.checked;
    });
    document.querySelectorAll(".predict-sensor-checkbox").forEach((input) => {
      input.checked = outputSet.has(input.value);
      input.disabled = !inputSet.has(input.value);
    });
  }
  const status = $("predictionModelStatus");
  status.classList.remove("error");
  status.classList.add("ok");
  status.textContent =
    `${profile.name || "I-ModernTCN"} · 模型输入${inputSet.size}通道 · ` +
    `可输出${outputSet.size}通道 · 24点输入→24点预测`;
  if (state.bootstrap && controls.autoIndicator?.checked) {
    configureAutomaticIndicator(true);
  }
}

function configureBestPredictionOverride() {
  const schema = controls.datasetSchema.value || "legacy_original";
  const enabled = controls.bestPredictionOverride.checked;
  const bestProfile =
    state.bootstrap?.acquisition?.best_prediction_models?.[schema];
  const running = Boolean(state.payload?.acquisition?.running);
  if (enabled && bestProfile) {
    if (
      controls.predictionModel.value.trim()
      && controls.predictionModel.value.trim() !== bestProfile.checkpoint
    ) {
      state.manualPredictionModels[schema] =
        controls.predictionModel.value.trim();
    }
    applyPredictionModelProfile(bestProfile, true);
    const metric = Number(bestProfile.selection_metric_value);
    $("bestPredictionOverrideNote").textContent =
      `已覆盖为：${bestProfile.name}。选择依据：${bestProfile.selection_basis}` +
      `${Number.isFinite(metric) ? `；验证集标准化MSE=${metric.toFixed(5)}` : ""}。`;
  } else {
    const manualPath = state.manualPredictionModels[schema];
    if (manualPath) {
      controls.predictionModel.value = manualPath;
      inspectPredictionModel(true).catch(() => {});
    }
    $("bestPredictionOverrideNote").textContent =
      "未勾选：严格使用上方手动选择的模型；勾选：只按验证集误差选择同一采集方案内的最佳兼容模型，不使用测试集或未来真实值。";
  }
  controls.predictionModel.disabled = running || enabled;
  $("selectPredictionModelButton").disabled = running || enabled;
}

async function inspectPredictionModel(setSelections = true) {
  try {
    const profile = await postJson("/api/prediction-model/inspect", {
      path: controls.predictionModel.value.trim(),
    });
    applyPredictionModelProfile(profile, setSelections);
    return profile;
  } catch (error) {
    const status = $("predictionModelStatus");
    status.classList.remove("ok");
    status.classList.add("error");
    status.textContent = error.message;
    throw error;
  }
}

async function selectPredictionModel() {
  try {
    const result = await postJson("/api/prediction-model/select-file", {
      initial_path: controls.predictionModel.value.trim(),
    });
    if (result.selected) {
      applyPredictionModelProfile(result.model, true);
      toast("预测模型已选择，并已按模型元数据设置输入/输出通道");
    }
  } catch (error) {
    const status = $("predictionModelStatus");
    status.classList.remove("ok");
    status.classList.add("error");
    status.textContent = error.message;
    toast(error.message);
  }
}

async function selectSaveRoot() {
  try {
    const result = await postJson("/api/acquisition/select-folder", {
      initial_path: controls.saveRoot.value.trim(),
    });
    if (result.selected) {
      controls.saveRoot.value = result.path;
      toast(`保存位置已选择：${result.path}`);
    }
  } catch (error) {
    toast(`无法打开文件夹选择器：${error.message}`);
  }
}

async function testSensorConnection() {
  try {
    const result = await postJson("/api/acquisition/test", acquisitionConfig());
    if (controls.processingMode.value !== "capture_only") {
      applyPredictionModelProfile(result.prediction_model, false);
    }
    const selected = result.sensors.filter((item) => item.selected);
    const healthy = selected.filter((item) => item.ok);
    const node = $("acquisitionStatus");
    node.classList.toggle("ok", result.ok);
    node.classList.toggle("error", !result.ok);
    node.textContent = result.ok
      ? `连接检查通过：${healthy.length}/${selected.length} 个所选传感器收到有效数据`
      : `连接检查未通过：${healthy.length}/${selected.length} 正常；${result.errors.join("；") || "有传感器未收到数据"}`;
  } catch (error) {
    toast(error.message);
  }
}

async function startAcquisition() {
  try {
    const result = await postJson("/api/acquisition/start", acquisitionConfig());
    if (controls.processingMode.value !== "capture_only") {
      applyPredictionModelProfile(result.prediction_model, false);
    }
    renderAcquisitionStatus(result);
    controls.dataMode.value = "live";
    configureDataMode();
    await loadRealtime();
  } catch (error) {
    toast(error.message);
  }
}

async function stopAcquisition() {
  try {
    const layerControl = controls.datasetSchema.value === "new_collection_v11_3"
      ? controls.newLayer
      : controls.liveLayer;
    const completedLayer = Number(layerControl.value) || 0;
    const result = await postJson("/api/acquisition/stop", {});
    renderAcquisitionStatus(result);
    if (
      completedLayer < 4 &&
      Array.isArray(result.completed_layers) &&
      result.completed_layers.includes(completedLayer + 1)
    ) {
      layerControl.value = String(completedLayer + 1);
      toast(`第${completedLayer + 1}层已完成并保存；下次开始采集将进入第${completedLayer + 2}层`);
    } else if (completedLayer === 4) {
      toast("第5层已完成并保存，五层试样证据采集结束");
    }
    await loadRealtime();
  } catch (error) {
    toast(error.message);
  }
}

function buildSensorChecklist(sensorNames) {
  const sensorHeader = document.createElement("div");
  sensorHeader.className = "sensor-checklist-header";
  sensorHeader.innerHTML =
    "<span>通道</span><span>采集</span><span>输入</span><span>输出</span>";
  const sensorRows = sensorNames.map((name) => {
    const row = document.createElement("div");
    row.className = "sensor-checklist-row";
    const channelName = document.createElement("span");
    channelName.className = "sensor-checklist-name";
    channelName.textContent = name;

    const collectLabel = document.createElement("label");
    const collectInput = document.createElement("input");
    collectInput.type = "checkbox";
    collectInput.className = "save-sensor-checkbox";
    collectInput.value = name;
    collectInput.checked = true;
    collectLabel.append(collectInput, document.createTextNode("采"));

    const modelLabel = document.createElement("label");
    const modelInput = document.createElement("input");
    modelInput.type = "checkbox";
    modelInput.className = "model-input-sensor-checkbox";
    modelInput.value = name;
    modelInput.checked = true;
    modelLabel.append(modelInput, document.createTextNode("入"));

    const outputLabel = document.createElement("label");
    const outputInput = document.createElement("input");
    outputInput.type = "checkbox";
    outputInput.className = "predict-sensor-checkbox";
    outputInput.value = name;
    outputInput.checked = true;
    outputLabel.append(outputInput, document.createTextNode("出"));

    collectInput.addEventListener("change", () => {
      modelInput.disabled = !collectInput.checked
        || controls.processingMode.value === "capture_only";
      outputInput.disabled = !collectInput.checked
        || controls.processingMode.value === "capture_only";
      if (!collectInput.checked) {
        modelInput.checked = false;
        outputInput.checked = false;
      }
      if (controls.dataMode.value === "live") loadRealtime();
    });
    modelInput.addEventListener("change", () => {
      if (!modelInput.checked) outputInput.checked = false;
      outputInput.disabled = !modelInput.checked
        || controls.processingMode.value === "capture_only";
    });
    outputInput.addEventListener("change", () => {
      configureAutomaticIndicator(true);
      if (controls.dataMode.value === "live") loadRealtime();
    });
    row.append(
      channelName, collectLabel, modelLabel, outputLabel
    );
    return row;
  });
  $("liveSensorChecklist").replaceChildren(sensorHeader, ...sensorRows);
}

function activeInputSchemaId() {
  return controls.dataMode.value === "replay"
    ? "legacy_original"
    : (controls.datasetSchema.value || "legacy_original");
}

function populateIndicatorOptions(schemaId, preserveSelection = true) {
  const definitions = state.bootstrap.indicator_schemas?.[schemaId]
    || state.bootstrap.indicators;
  const previous = controls.indicator.value;
  controls.indicator.replaceChildren(...definitions.map((item) =>
    option(item.id, item.label ? `${item.id} · ${item.label}` : item.id)
  ));
  controls.indicator.value = preserveSelection && definitions.some(
    (item) => item.id === previous
  ) ? previous : (definitions.find((item) => item.id === "TC-HI")?.id
    || definitions[0]?.id || "");
  return definitions;
}

function configureAutomaticIndicator(useRecommendation = true) {
  if (!state.bootstrap) return;
  const automatic = controls.autoIndicator.checked;
  controls.indicator.disabled = automatic;
  const status = $("indicatorAutoStatus");
  if (!automatic) {
    status.classList.remove("warning");
    status.textContent = "手动模式：可自行选择健康指标及异常分数模型。";
    return;
  }

  const schemaId = activeInputSchemaId();
  const definitions = state.bootstrap.indicator_schemas?.[schemaId]
    || state.bootstrap.indicators;
  const schema = state.bootstrap.acquisition.schemas.find(
    (item) => item.id === schemaId
  );
  const availableOutputs = new Set(
    controls.dataMode.value === "replay"
      ? (schema?.sensors || [])
      : selectedPredictionSensors()
  );
  const preferredOrder = [
    "TC-HI", "T-HI", "C-HI", "RFHI", "PR-HI", "MPRF-HI",
    "PCA-SPE-HI", "KECA-SPE-HI", "McFS-AVAE-HI",
    "CNN-LSTM-AE-HI", "W-HI", "RMD-HI",
  ];
  const compatible = definitions.filter((item) => {
    const required = item.required_outputs || item.variant?.required_outputs || [];
    return required.every((name) => availableOutputs.has(name));
  });
  const selected = preferredOrder
    .map((id) => compatible.find((item) => item.id === id))
    .find(Boolean) || compatible[0] || definitions[0];
  if (!selected) return;

  const changed = controls.indicator.value !== selected.id;
  controls.indicator.value = selected.id;
  if (changed || useRecommendation) populateModels(true);
  const variant = selected.variant || {};
  const required = selected.required_outputs || variant.required_outputs || [];
  const missing = required.filter((name) => !availableOutputs.has(name));
  status.classList.toggle("warning", missing.length > 0);
  status.innerHTML = missing.length
    ? `<strong>${variant.variant_id || selected.id}</strong>：当前输入缺少 ${missing.join("、")}，请检查模型输出通道。`
    : `<strong>${variant.variant_id || selected.id}</strong>：${variant.construction || "按当前输入方案自动构建"}；使用 ${required.join("、") || "当前可用通道"}。`;
}

function configureProcessingMode() {
  const captureOnly = controls.processingMode.value === "capture_only";
  controls.autoIndicator.disabled = captureOnly;
  controls.indicator.disabled = captureOnly || controls.autoIndicator.checked;
  document.querySelectorAll(".prediction-setting").forEach((node) => {
    node.classList.toggle("hidden", captureOnly);
  });
  document.querySelectorAll(
    ".model-input-sensor-checkbox, .predict-sensor-checkbox"
  ).forEach((input) => {
    const collected = input.closest(".sensor-checklist-row")
      ?.querySelector(".save-sensor-checkbox")?.checked;
    input.disabled = captureOnly || !collected;
  });
  controls.optimizedWarning.disabled = captureOnly
    || controls.datasetSchema.value === "new_collection_v11_3";
  controls.bestPredictionOverride.disabled = captureOnly;
  if (captureOnly) {
    $("indicatorAutoStatus").textContent = "仅采集模式：不构建健康指标。";
    $("predictionModelStatus").textContent =
      "仅采集模式不加载模型，也不计算预测和健康指标";
  } else if (controls.predictionModel.value.trim()) {
    if (controls.bestPredictionOverride.checked) {
      configureBestPredictionOverride();
    } else {
      inspectPredictionModel(true).catch(() => {});
    }
  }
  if (!captureOnly) configureAutomaticIndicator(true);
  if (controls.dataMode.value === "live") loadRealtime();
}

function configureDatasetSchema(useDefaults = true) {
  const schema = state.bootstrap.acquisition.schemas.find(
    (item) => item.id === controls.datasetSchema.value
  );
  if (!schema) return;
  const isNew = schema.id === "new_collection_v11_3";
  const schemaIndicators = state.bootstrap.indicator_schemas?.[schema.id]
    || state.bootstrap.indicators;
  const previousIndicator = controls.indicator.value;
  controls.indicator.replaceChildren(...schemaIndicators.map((item) =>
    option(item.id, item.label ? `${item.id} · ${item.label}` : item.id)
  ));
  controls.indicator.value = schemaIndicators.some(
    (item) => item.id === previousIndicator
  ) ? previousIndicator : (schemaIndicators.find((item) => item.id === "TC-HI")?.id
    || schemaIndicators[0]?.id || "");
  populateModels(true);
  $("legacyParameterGrid").classList.toggle("hidden", isNew);
  $("newParameterGrid").classList.toggle("hidden", !isNew);
  document.querySelector(".save-rule-note").textContent = isNew
    ? "自动建立“试样名_F压实力_V速度_A角度_T设定温度”文件夹，同时保存分层文件、完整试样快照和采集记录。"
    : "自动建立“试样名_p功率_v速度_pr压实力”文件夹，同时保存分层文件、完整试样快照和采集记录。";
  buildSensorChecklist(schema.sensors);
  controls.sensor.replaceChildren(...schema.sensors.map((name, index) =>
    option(index, name)
  ));
  controls.sensor.value = "0";
  if (useDefaults) {
    if (isNew) {
      const demo = state.bootstrap.acquisition.new_collection_demo;
      controls.sourceFile.value = demo?.source_file || "";
      if (demo?.prediction_model) {
        applyPredictionModelProfile(demo.prediction_model, true);
        state.manualPredictionModels[schema.id] =
          demo.prediction_model.checkpoint;
      }
    } else {
      controls.sourceFile.value = "";
      applyPredictionModelProfile(
        state.bootstrap.acquisition.prediction_model,
        true
      );
      state.manualPredictionModels[schema.id] =
        state.bootstrap.acquisition.prediction_model.checkpoint;
    }
  }
  if (controls.bestPredictionOverride.checked) {
    configureBestPredictionOverride();
  }
  controls.optimizedWarning.disabled = isNew
    || controls.processingMode.value === "capture_only";
  if (isNew) controls.optimizedWarning.checked = false;
  configureProcessingMode();
  configureAutomaticIndicator(true);
}

function configureDataMode() {
  const live = controls.dataMode.value === "live";
  if (!live && controls.datasetSchema.value !== "legacy_original") {
    controls.datasetSchema.value = "legacy_original";
    configureDatasetSchema(false);
  }
  $("acquisitionSection").classList.toggle("hidden", !live);
  controls.specimen.disabled = live;
  controls.cursor.disabled = live;
  controls.realtimePrediction.disabled = live;
  controls.optimizedWarning.disabled = false;
  if (live) {
    stopPlayback();
    controls.realtimePrediction.checked = true;
    window.clearInterval(state.livePollTimer);
    state.livePollTimer = window.setInterval(() => {
      if (!state.busy) loadRealtime();
    }, 100);
  } else {
    window.clearInterval(state.livePollTimer);
    state.livePollTimer = null;
  }
  configureAutomaticIndicator(true);
  loadRealtime();
}

function queryString() {
  const predictionSensors = selectedPredictionSensors();
  return new URLSearchParams({
    specimen: controls.specimen.value,
    sensor: controls.sensor.value,
    cursor: controls.cursor.value,
    history: controls.history.value,
    step: controls.step.value,
    threshold: controls.threshold.value,
    rho: controls.rho.value,
    score_mode: "raw",
    indicator: controls.indicator.value,
    model: controls.model.value,
    prediction_horizon: state.requestedHorizon ?? controls.horizon.value,
    realtime_prediction: controls.realtimePrediction.checked,
    processing_mode: controls.processingMode.value,
    use_optimized_warning: controls.optimizedWarning.checked,
    prediction_sensors: predictionSensors.length
      ? predictionSensors.join(",")
      : "__none__",
  }).toString();
}

async function loadRealtime() {
  if (state.busy) {
    state.reloadQueued = true;
    return;
  }
  state.busy = true;
  try {
    const endpoint = controls.dataMode.value === "live" ? "/api/live" : "/api/realtime";
    const response = await fetch(`${endpoint}?${queryString()}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "实时数据请求失败");
    state.payload = payload;
    if (controls.dataMode.value === "replay") {
      controls.cursor.max = payload.progress.total_points;
      controls.cursor.value = payload.progress.cursor;
    }
    render(payload);
    $("connectionStatus").textContent = "实时数据服务已连接";
    document.querySelector(".status-dot").classList.add("connected");
    if (payload.acquisition) renderAcquisitionStatus(payload.acquisition);
    if (payload.progress.finished && state.playing) {
      if (controls.loop.checked) {
        controls.cursor.value = 1;
      } else {
        stopPlayback();
      }
    }
  } catch (error) {
    stopPlayback();
    toast(error.message);
    $("connectionStatus").textContent = "实时数据服务连接失败";
  } finally {
    state.busy = false;
    if (state.reloadQueued) {
      state.reloadQueued = false;
      window.setTimeout(loadRealtime, 0);
    }
  }
}

function scheduleLoad() {
  window.clearTimeout(state.requestTimer);
  state.requestTimer = window.setTimeout(loadRealtime, 70);
}

function playbackInterval() {
  const speed = Math.max(0.1, Number(controls.speed.value));
  return Math.max(35, 100 / speed);
}

function startPlayback() {
  if (controls.dataMode.value === "live") {
    toast("真实采集模式由传感器数据自动推进，无需启动回放");
    return;
  }
  if (state.playing) return;
  state.playing = true;
  $("playButton").textContent = "❚❚ 暂停";
  $("streamStatus").textContent = "实时回放运行中";
  document.querySelector(".live-dot").classList.add("active");
  state.timer = window.setInterval(async () => {
    if (state.busy) return;
    const maximum = Number(controls.cursor.max);
    let next = Number(controls.cursor.value) + Number(controls.streamStep.value);
    if (next > maximum) {
      if (controls.loop.checked) next = 1;
      else {
        next = maximum;
        stopPlayback();
      }
    }
    controls.cursor.value = String(next);
    await loadRealtime();
  }, playbackInterval());
}

function stopPlayback() {
  state.playing = false;
  window.clearInterval(state.timer);
  state.timer = null;
  $("playButton").textContent = "▶ 开始";
  $("streamStatus").textContent = "实时回放已暂停";
  document.querySelector(".live-dot").classList.remove("active");
}

function restartPlaybackTimer() {
  if (!state.playing) return;
  stopPlayback();
  startPlayback();
}

function statePill(node, label, stateKey) {
  node.textContent = label;
  node.classList.toggle("abnormal", !["normal", "pending"].includes(stateKey));
  node.classList.toggle("pending", stateKey === "pending");
}

function renderProcessParameters(process) {
  const grid = $("processParameterGrid");
  const parameters = Array.isArray(process?.display_parameters)
    ? process.display_parameters
    : [
        { label: "功率", unit: "W", value: process?.current_p, nominal: process?.p },
        { label: "铺放速度", unit: "mm/s", value: process?.current_v, nominal: process?.v },
        { label: "压实力", unit: "N", value: process?.current_pr, nominal: process?.pr },
      ];
  const columnCount = parameters.length === 4 ? 2 : Math.min(parameters.length, 3);
  grid.closest(".parameter-card")?.classList.toggle(
    "four-parameters", parameters.length === 4
  );
  grid.style.gridTemplateColumns = `repeat(${Math.max(columnCount, 1)}, minmax(0, 1fr))`;
  grid.replaceChildren(...parameters.map((parameter) => {
    const card = document.createElement("div");
    const label = document.createElement("span");
    label.textContent = parameter.label || parameter.key;
    const value = document.createElement("strong");
    value.textContent = `${fmt(parameter.value, 2)}${parameter.unit ? ` ${parameter.unit}` : ""}`;
    card.append(label, value);
    return card;
  }));
  const schemaLabels = {
    legacy_original: "旧数据格式（p / v / pr）",
    new_collection_v11_3: "新数据格式（压实力 / 速度 / 角度 / 设定温度）",
  };
  const sourceLabels = {
    input_data: "由输入数据自动提取",
    input_data_with_config_fallback: "由输入数据提取，缺失项使用采集设置",
    configuration: "尚无数据行，暂用采集设置",
  };
  $("parameterDetail").textContent =
    `${schemaLabels[process?.schema_id] || "自动识别的数据格式"} · ${sourceLabels[process?.parameter_source] || "自动提取"}` +
    `${Number(process?.injection_severity) > 0 ? ` · 异常强度 ${fmt(process.injection_severity, 2)}` : ""}`;
}

function configuredProcessPayload() {
  if (activeInputSchemaId() === "new_collection_v11_3") {
    return {
      schema_id: "new_collection_v11_3",
      parameter_source: "configuration",
      injection_severity: 0,
      display_parameters: [
        { label: "初始压实力", unit: "N", value: Number(controls.initialForce.value) },
        { label: "铺放速度", unit: "mm/s", value: Number(controls.placementSpeed.value) },
        { label: "PID角度", unit: "°", value: Number(controls.pidAngle.value) },
        { label: "设定温度", unit: "°C", value: Number(controls.temperatureSetpoint.value) },
      ],
    };
  }
  return {
    schema_id: "legacy_original",
    parameter_source: "configuration",
    injection_severity: 0,
    display_parameters: [
      { label: "功率", unit: "W", value: Number(controls.livePower.value) },
      { label: "铺放速度", unit: "mm/s", value: Number(controls.liveSpeed.value) },
      { label: "压实力", unit: "N", value: Number(controls.livePressure.value) },
    ],
  };
}

function renderIndicatorVariant(payload) {
  if (!controls.autoIndicator.checked || !payload?.feature_generation) return;
  const variant = payload.feature_generation.indicator_variant;
  if (!variant) return;
  if (
    controls.dataMode.value === "live"
    && payload.process?.schema_id !== activeInputSchemaId()
  ) return;
  const outputs = payload.feature_generation.health_indicator_output_sensors
    || variant.required_outputs || [];
  const status = $("indicatorAutoStatus");
  status.classList.remove("warning");
  status.innerHTML = `<strong>${variant.variant_id}</strong>：${variant.construction}；当前使用 ${outputs.join("、") || "可用输入通道"}。`;
}

function render(payload) {
  const captureOnly = payload.mode === "capture_only";
  const progress = payload.progress;
  const windowData = payload.window;
  const layer = payload.layer;
  const specimen = payload.specimen;
  const process = (
    controls.dataMode.value === "live"
    && payload.process?.schema_id !== activeInputSchemaId()
  ) ? configuredProcessPayload() : payload.process;

  $("progressValue").value = `${progress.cursor} / ${progress.total_points}`;
  const returnedHorizon = Number(payload.selection.prediction_horizon);
  const pendingHorizon = state.requestedHorizon;
  const horizonConfirmed = pendingHorizon === null
    || returnedHorizon === Number(pendingHorizon);
  if (horizonConfirmed) {
    $("horizonValue").value = returnedHorizon;
    controls.horizon.value = String(returnedHorizon);
    if (pendingHorizon !== null) state.requestedHorizon = null;
  }
  // The live poll runs every 100 ms. Do not overwrite a number while the
  // operator is typing it; otherwise multi-digit direct input is impossible.
  if (
    document.activeElement !== controls.horizonNumber
    && horizonConfirmed
  ) {
    controls.horizonNumber.value = String(returnedHorizon);
  }
  $("thresholdValue").value = Number(payload.selection.threshold).toFixed(2);
  $("rhoValue").value = Number(payload.selection.rho).toFixed(2);
  $("streamPosition").textContent =
    `第${progress.current_layer}层 · 窗口${progress.current_window}/${progress.total_windows_in_layer} · 点${progress.sample_in_window}/24`;
  if (captureOnly) {
    $("recommendationCard").classList.add("not-recommended");
    $("recommendationCard").innerHTML =
      "<div><strong>仅采集模式</strong><span>预测模型与预警算法均未运行</span></div>";
  } else {
    renderRecommendation(payload.candidate);
  }

  if (specimen) {
    statePill($("specimenState"), specimen.state_label, specimen.state);
    $("specimenScore").textContent = `HI ${fmt(specimen.health)}`;
  $("specimenDetail").textContent =
      `当前已有 ${specimen.evidence_layers}/5 层形成证据 · ${
        payload.mode === "live_acquisition"
          ? "真实采集不预设真值"
          : `最终离线真值：${payload.official_final.true_state_label}`
      }`;
  } else {
    statePill($("specimenState"), "等待数据", "pending");
    $("specimenScore").textContent = "—";
    $("specimenDetail").textContent = captureOnly
      ? "仅保存原始数据，不生成试样状态"
      : "至少需要1个完整窗口";
  }

  if (layer) {
    statePill($("layerState"), layer.state_label, layer.state);
    $("layerScore").textContent = `HI ${fmt(layer.health)}`;
    $("layerDetail").textContent =
      `第${progress.current_layer}层 · 已聚合 ${layer.evidence_count}/${progress.total_windows_in_layer} 个完整窗口`;
    $("layerPreviewBadge").textContent = `${layer.state_label} · HI ${fmt(layer.health)}`;
    $("poolingSummary").textContent =
      `因果CAP聚合：有效窗口数 ${fmt(layer.effective_count, 1)}；最大单窗权重 ${fmt(layer.maximum_weight * 100, 1)}%；未来窗口未参与`;
  } else {
    statePill($("layerState"), "等待数据", "pending");
    $("layerScore").textContent = "—";
    $("layerDetail").textContent = captureOnly
      ? `第${progress.current_layer}层 · 仅采集，不进行层级聚合`
      : `第${progress.current_layer}层 · 尚无完整24点窗口`;
    $("layerPreviewBadge").textContent = captureOnly ? "聚合未启用" : "等待完整窗口";
    $("poolingSummary").textContent = captureOnly
      ? "仅采集模式不计算窗口、层级和试样级健康指标"
      : "只有完整到达的24点窗口才进入层级聚合";
  }

  statePill($("windowState"), windowData.state_label, windowData.state);
  $("windowScore").textContent = windowData.complete ? fmt(windowData.score) : "—";
  $("windowDetail").textContent = captureOnly
    ? "预测与预警未启用；当前只显示并保存实测数据"
    : windowData.complete
    ? `${windowData.id} · 实时特征与分类分数 ${fmt(windowData.raw_realtime_score)} · ${
        windowData.optimized_warning_applied ? "最终状态采用原优化一致性结果" : "最终状态采用实时分类结果"
      }`
    : `当前窗口已到达 ${progress.sample_in_window}/24 点`;

  renderProcessParameters(process);
  renderIndicatorVariant(payload);

  const channel = payload.selected_channel;
  const predictionEnabled = channel.prediction_enabled !== false;
  $("seriesTitle").textContent =
    predictionEnabled
      ? `${channel.name}：实时采集、历史预测与未来${payload.forecast.requested_horizon}点预测（${channel.unit}）`
      : `${channel.name}：实时采集（未勾选显示预测，${channel.unit}）`;
  $("observedPredictionLegend").classList.toggle("hidden", !predictionEnabled);
  $("futurePredictionLegend").classList.toggle("hidden", !predictionEnabled);
  $("residualToggleLabel").classList.toggle("hidden", !predictionEnabled);
  $("residualToggle").disabled = !predictionEnabled;
  if (!predictionEnabled) {
    $("residualToggle").checked = false;
    state.showResidual = false;
  }
  const forecastLabels = {
    live_checkpoint_direct_24: "当前检查点实时前向推理（原生24点）",
    live_checkpoint_recursive: "当前检查点实时递归滚动预测",
    archived_direct_24: "历史归档预测（24点内）",
    archived_rolling_windows: "历史归档多窗口预测",
    waiting_for_24_points: "等待全部传感器累计24个有效点",
    capture_only: "仅采集，不执行预测",
  };
  const forecastLabel = forecastLabels[payload.forecast.mode] || payload.forecast.mode;
  const forecastSelectionLabel = payload.selection.best_prediction_override
    ? `${forecastLabel} · 验证集最佳模型覆盖`
    : forecastLabel;
  $("displayRange").textContent =
    predictionEnabled
      ? `历史 ${channel.actual.length} 个显示点 · 未来 ${channel.prediction_future.length} 点 · ${forecastSelectionLabel}`
      : `历史 ${channel.actual.length} 个显示点 · 该通道预测结果已隐藏`;
  const visiblePredictionCount = payload.channels.filter(
    (item) => item.prediction_enabled !== false
  ).length;
  $("allChannelHint").textContent =
    `${payload.channels.length}个通道显示实测；${visiblePredictionCount}个通道显示预测`;
  $("calculationModeNote").textContent =
    captureOnly
      ? "当前为仅采集模式：不加载模型，不生成预测、健康指标或三级预警。"
      : `健康指标特征与异常分数：${
      payload.feature_generation?.mode === "realtime_from_actual_and_live_prediction"
        ? "由本次实测与实时预测现场生成"
        : payload.feature_generation?.mode === "realtime_from_current_replay_window"
          ? "由当前回放实测/预测窗口重新生成"
          : "等待完整窗口"
    }；最终预警：${
      windowData.optimized_warning_applied
        ? payload.mode === "live_acquisition"
          ? "因果在线一致性v13.9（不使用未来层）"
          : "原优化五层一致性v13.8"
        : "实时窗口→层→试样聚合"
    }。预测：${forecastSelectionLabel}。`;

  renderSeriesChart(channel);
  renderTimeline(payload.timeline);
  renderProbabilities(windowData.type_probabilities);
  renderSensorCards(payload.channels, payload.selection.sensor);
  renderLayerProgress(payload.layers);
}

function setupCanvas(canvas, height) {
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  canvas.width = Math.max(1, Math.round(width * dpr));
  canvas.height = Math.round(height * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, width, height };
}

function renderSeriesChart(channel) {
  drawChannelChart($("seriesChart"), channel, 300, false);
}

function drawChannelChart(canvas, channel, height, compact) {
  if (!canvas?.isConnected) return;
  const setup = setupCanvas(canvas, height);
  const ctx = setup.ctx, width = setup.width;
  const pad = compact
    ? { left: 42, right: 12, top: 12, bottom: 24 }
    : { left: 58, right: 18, top: 20, bottom: 34 };
  const observedX = channel.x_observed;
  const futureX = channel.x_future;
  const predictionEnabled = channel.prediction_enabled !== false;
  const residual = channel.actual.map((value, index) =>
    Number.isFinite(value) && Number.isFinite(channel.prediction_observed[index])
      ? value - channel.prediction_observed[index]
      : null
  );
  const lines = [
    { x: observedX, values: channel.actual, color: "#4b8cff", width: 2.0 },
  ];
  if (predictionEnabled) {
    lines.push(
      { x: observedX, values: channel.prediction_observed, color: "#20d3d8", width: 1.35 },
      { x: futureX, values: channel.prediction_future, color: "#ffb24b", width: 2.0, dashed: true },
    );
  }
  if (state.showResidual && predictionEnabled) {
    lines.push({ x: observedX, values: residual, color: "#9b7bff", width: 1.1 });
  }
  const allValues = lines.flatMap((line) => line.values).filter(Number.isFinite);
  const allX = lines.flatMap((line) => line.x).filter(Number.isFinite);
  if (!allValues.length || !allX.length) return;
  let ymin = Math.min(...allValues), ymax = Math.max(...allValues);
  const margin = Math.max((ymax - ymin) * 0.08, Math.abs(ymax) * 0.01, 1e-6);
  ymin -= margin; ymax += margin;
  const xmin = Math.min(...allX), xmax = Math.max(...allX, 0.1);
  const px = (value) => pad.left + ((value - xmin) / Math.max(xmax - xmin, 1e-9)) * (width - pad.left - pad.right);
  const py = (value) => pad.top + ((ymax - value) / Math.max(ymax - ymin, 1e-9)) * (height - pad.top - pad.bottom);

  ctx.clearRect(0, 0, width, height);
  ctx.font = compact ? "9px Segoe UI" : "10px Segoe UI";
  ctx.strokeStyle = "#dce6ed";
  ctx.fillStyle = "#6b8193";
  ctx.lineWidth = 1;
  const gridCount = compact ? 3 : 5;
  for (let i = 0; i <= gridCount; i += 1) {
    const yy = pad.top + (i / gridCount) * (height - pad.top - pad.bottom);
    ctx.beginPath(); ctx.moveTo(pad.left, yy); ctx.lineTo(width - pad.right, yy); ctx.stroke();
    ctx.fillText((ymax - (i / gridCount) * (ymax - ymin)).toFixed(2), 3, yy + 3);
  }
  const xGridCount = compact ? 4 : 6;
  for (let i = 0; i <= xGridCount; i += 1) {
    const xx = pad.left + (i / xGridCount) * (width - pad.left - pad.right);
    ctx.beginPath(); ctx.moveTo(xx, pad.top); ctx.lineTo(xx, height - pad.bottom); ctx.stroke();
    ctx.fillText((xmin + (i / xGridCount) * (xmax - xmin)).toFixed(1), xx - 11, height - 8);
  }
  const nowX = px(0);
  ctx.strokeStyle = "#d9902f";
  ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.moveTo(nowX, pad.top); ctx.lineTo(nowX, height - pad.bottom); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "#b66b18";
  ctx.fillText("当前", nowX + 5, pad.top + 10);

  for (const line of lines) {
    if (!line.values.length) continue;
    ctx.beginPath();
    let drawing = false;
    line.values.forEach((value, index) => {
      if (!Number.isFinite(value) || !Number.isFinite(line.x[index])) {
        drawing = false;
        return;
      }
      const xx = px(line.x[index]), yy = py(value);
      if (!drawing) {
        ctx.moveTo(xx, yy);
        drawing = true;
      } else {
        ctx.lineTo(xx, yy);
      }
    });
    ctx.strokeStyle = line.color;
    ctx.lineWidth = line.width;
    ctx.setLineDash(line.dashed ? [6, 4] : []);
    ctx.stroke();
  }
  ctx.setLineDash([]);
}

function renderTimeline(data) {
  const canvas = $("timelineChart");
  const { ctx, width, height } = setupCanvas(canvas, 150);
  const pad = { left: 38, right: 12, top: 12, bottom: 26 };
  const scores = data.scores;
  ctx.clearRect(0, 0, width, height);
  ctx.strokeStyle = "#dce6ed";
  ctx.fillStyle = "#6b8193";
  ctx.font = "10px Segoe UI";
  for (let i = 0; i <= 4; i += 1) {
    const value = i / 4;
    const yy = height - pad.bottom - value * (height - pad.top - pad.bottom);
    ctx.beginPath(); ctx.moveTo(pad.left, yy); ctx.lineTo(width - pad.right, yy); ctx.stroke();
    ctx.fillText(value.toFixed(2), 3, yy + 3);
  }
  const usableWidth = width - pad.left - pad.right;
  const barWidth = Math.max(1, usableWidth / Math.max(scores.length, 1) - 1);
  scores.forEach((score, index) => {
    const x = pad.left + (index / Math.max(scores.length, 1)) * usableWidth;
    if (score === null) {
      ctx.fillStyle = index === data.active_index ? "rgba(232,150,46,.24)" : "rgba(117,143,164,.16)";
      ctx.fillRect(x, pad.top, barWidth, height - pad.top - pad.bottom);
      return;
    }
    const barHeight = Math.max(1, score * (height - pad.top - pad.bottom));
    ctx.fillStyle = score >= data.threshold ? "#d94c57" : "#1f6fb2";
    ctx.fillRect(x, height - pad.bottom - barHeight, barWidth, barHeight);
  });
  const thresholdY = height - pad.bottom - data.threshold * (height - pad.top - pad.bottom);
  ctx.setLineDash([5, 4]);
  ctx.strokeStyle = "#d9902f";
  ctx.beginPath(); ctx.moveTo(pad.left, thresholdY); ctx.lineTo(width - pad.right, thresholdY); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "#6b8193";
  ctx.fillText(`已完成 ${data.completed_count} 个窗口`, pad.left, height - 10);
}

function renderProbabilities(probabilities) {
  const labels = state.bootstrap.state_labels;
  const entries = Object.entries(probabilities).sort((a, b) => b[1] - a[1]);
  $("probabilityBars").replaceChildren(...entries.map(([key, value]) => {
    const row = document.createElement("div");
    row.className = "probability-row";
    row.innerHTML = `
      <span>${labels[key] || key}</span>
      <div class="probability-track"><div class="probability-fill" style="width:${Math.max(0, Math.min(100, value * 100))}%"></div></div>
      <span class="probability-value">${(value * 100).toFixed(1)}%</span>`;
    return row;
  }));
}

function renderSensorCards(channels, selectedId) {
  const cards = channels.map((channel) => {
    const card = document.createElement("div");
    const predictionEnabled = channel.prediction_enabled !== false;
    card.className =
      `sensor-card full-sensor-card${channel.id === selectedId ? " selected" : ""}` +
      `${predictionEnabled ? "" : " prediction-hidden"}`;
    const predictionMetrics = predictionEnabled
      ? `<span>预测 <strong>${fmt(channel.prediction_current, 2)}</strong></span>
         <span>RMSE <strong>${fmt(channel.rmse, 3)}</strong></span>`
      : `<span class="prediction-hidden-label">预测 已隐藏</span>`;
    card.innerHTML = `
      <div class="full-sensor-head">
        <div class="sensor-name">${channel.name}<span>${channel.unit}</span></div>
        <div class="sensor-live-values">
          <span>实测 <strong>${fmt(channel.actual_current, 2)}</strong></span>
          ${predictionMetrics}
        </div>
      </div>
      <canvas class="full-channel-chart" height="170"></canvas>`;
    card.addEventListener("click", () => {
      controls.sensor.value = String(channel.id);
      loadRealtime();
    });
    window.requestAnimationFrame(() =>
      drawChannelChart(card.querySelector("canvas"), channel, 170, true)
    );
    return card;
  });
  $("sensorCards").replaceChildren(...cards);
}

const MODEL_LABELS = {
  logistic: "逻辑回归",
  svm_rbf: "RBF-SVM",
  random_forest: "随机森林",
  extra_trees: "极端随机树",
};

function activeIndicatorDefinitions() {
  const schema = activeInputSchemaId();
  return state.bootstrap.indicator_schemas?.[schema]
    || state.bootstrap.indicators;
}

function selectedIndicatorDefinition() {
  return activeIndicatorDefinitions().find(
    (item) => item.id === controls.indicator.value
  );
}

function populateModels(useRecommendation) {
  const indicator = selectedIndicatorDefinition();
  if (!indicator) return;
  const previous = controls.model.value;
  controls.model.replaceChildren(...indicator.models.map((model) =>
    option(model.id, `${MODEL_LABELS[model.id] || model.id}${model.recommended ? "（推荐）" : ""}`)
  ));
  controls.model.value = useRecommendation || !indicator.models.some((item) => item.id === previous)
    ? indicator.recommended_model
    : previous;
  applyCandidateDefaults();
}

function selectedModelDefinition() {
  const indicator = selectedIndicatorDefinition();
  return indicator?.models.find((item) => item.id === controls.model.value);
}

function applyCandidateDefaults() {
  const model = selectedModelDefinition();
  if (!model) return;
  controls.threshold.value = Math.max(0.05, Math.min(0.95, model.window_threshold));
  controls.rho.value = Math.max(0, Math.min(1, model.cap_rho));
  $("thresholdValue").value = Number(controls.threshold.value).toFixed(2);
  $("rhoValue").value = Number(controls.rho.value).toFixed(2);
}

function renderRecommendation(candidate) {
  const node = $("recommendationCard");
  node.classList.toggle("not-recommended", !candidate.recommended);
  node.innerHTML = `
    <div><strong>${candidate.indicator} · ${MODEL_LABELS[candidate.model] || candidate.model}</strong>
      <span>${candidate.recommended ? "当前指标推荐模型" : "非推荐模型，可用于对比"}</span></div>
    <p>验证选择分数 ${fmt(candidate.validation_selection_score, 3)}</p>
    <div class="recommendation-metrics">
      <span>窗 ${fmt(candidate.validation_window_balanced_accuracy * 100, 1)}%</span>
      <span>层 ${fmt(candidate.validation_layer_balanced_accuracy * 100, 1)}%</span>
      <span>试样 ${fmt(candidate.validation_specimen_balanced_accuracy * 100, 1)}%</span>
    </div>`;
}

function renderLayerProgress(layers) {
  $("layerProgress").replaceChildren(...layers.map((layer) => {
    const node = document.createElement("div");
    const health = layer.aggregate?.health;
    const predictedState = layer.aggregate?.state_label;
    const stateKey = layer.aggregate?.state;
    const percent = layer.total_windows ? (layer.completed_windows / layer.total_windows) * 100 : 0;
    const statusLabel = layer.status === "complete"
      ? "已完成"
      : layer.status === "active"
        ? "采集中"
        : "等待";
    const predictionPrefix = layer.status === "complete"
      ? "完成层预测"
      : layer.status === "active"
        ? "当前预测"
        : "预测结果";
    node.className = `layer-progress-card ${layer.status}`;
    node.innerHTML = `
      <div class="layer-progress-head"><strong>第${layer.display_layer}层</strong><span>${statusLabel}</span></div>
      <div class="layer-progress-track"><i style="width:${percent}%"></i></div>
      <div class="layer-progress-result ${stateKey && stateKey !== "normal" ? "abnormal" : ""}">
        <span>${predictionPrefix}</span><strong>${predictedState || "等待"}</strong>
      </div>
      <div class="layer-progress-foot"><span>${layer.completed_windows}/${layer.total_windows} 窗口</span><strong>${health == null ? "HI —" : `HI ${fmt(health)}`}</strong></div>`;
    return node;
  }));
}

async function initialize() {
  try {
    const response = await fetch("/api/bootstrap", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "初始化失败");
    state.bootstrap = payload;
    state.defaults = payload.defaults;
    controls.specimen.replaceChildren(...payload.specimens.map((item) =>
      option(item.id, `${item.id} · ${item.true_state_label}`)
    ));
    controls.sensor.replaceChildren(...payload.sensors.map((item) =>
      option(item.id, `${item.name} · ${item.unit}`)
    ));
    controls.indicator.replaceChildren(...payload.indicators.map((item) =>
      option(item.id, item.id)
    ));
    controls.driver.replaceChildren(...payload.acquisition.drivers.map((item) =>
      option(item.id, item.label)
    ));
    controls.datasetSchema.replaceChildren(
      ...payload.acquisition.schemas.map((item) =>
        option(item.id, item.label)
      )
    );
    controls.datasetSchema.value = "legacy_original";
    configureDatasetSchema(true);
    controls.dataMode.value = payload.defaults.data_mode || "replay";
    controls.specimen.value = payload.defaults.specimen;
    controls.sensor.value = String(payload.defaults.sensor);
    controls.cursor.value = payload.defaults.cursor;
    controls.history.value = payload.defaults.history;
    controls.step.value = payload.defaults.step;
    controls.streamStep.value = payload.defaults.stream_step;
    controls.horizon.value = payload.defaults.prediction_horizon;
    controls.horizonNumber.value = payload.defaults.prediction_horizon;
    controls.realtimePrediction.checked = Boolean(payload.defaults.realtime_prediction);
    controls.optimizedWarning.checked = Boolean(payload.defaults.use_optimized_warning);
    controls.saveRoot.value = payload.acquisition.default_save_root || "";
    controls.threshold.value = payload.defaults.threshold;
    controls.rho.value = payload.defaults.rho;
    controls.indicator.value = payload.defaults.indicator;
    populateModels(true);
    configureAutomaticIndicator(true);
    $("datasetMeta").textContent =
      `实时回放源：${payload.manifest.specimen_count} 个试样 · 12个通道 · 24点预测窗口 · ${payload.manifest.sampling_hz} Hz`;
    $("acquisitionSection").classList.toggle(
      "hidden", controls.dataMode.value !== "live"
    );
    await loadRealtime();
  } catch (error) {
    toast(error.message);
    $("connectionStatus").textContent = "初始化失败";
  }
}

$("playButton").addEventListener("click", () => {
  if (state.playing) stopPlayback(); else startPlayback();
});
$("resetButton").addEventListener("click", () => {
  stopPlayback();
  controls.cursor.value = "1";
  loadRealtime();
});
controls.specimen.addEventListener("change", () => {
  stopPlayback();
  controls.cursor.value = "1";
  loadRealtime();
});
controls.dataMode.addEventListener("change", configureDataMode);
controls.processingMode.addEventListener("change", configureProcessingMode);
controls.datasetSchema.addEventListener("change", () =>
  configureDatasetSchema(true)
);
controls.autoIndicator.addEventListener("change", () => {
  configureAutomaticIndicator(true);
  loadRealtime();
});
controls.sensor.addEventListener("change", loadRealtime);
controls.history.addEventListener("change", loadRealtime);
controls.step.addEventListener("change", loadRealtime);
controls.indicator.addEventListener("change", () => {
  populateModels(true);
  loadRealtime();
});
controls.model.addEventListener("change", () => {
  applyCandidateDefaults();
  loadRealtime();
});
controls.realtimePrediction.addEventListener("change", loadRealtime);
controls.optimizedWarning.addEventListener("change", loadRealtime);
controls.bestPredictionOverride.addEventListener(
  "change", configureBestPredictionOverride
);
$("testSensorsButton").addEventListener("click", testSensorConnection);
$("selectSaveRootButton").addEventListener("click", selectSaveRoot);
$("selectPredictionModelButton").addEventListener("click", selectPredictionModel);
controls.predictionModel.addEventListener("change", () => {
  if (!controls.bestPredictionOverride.checked) {
    state.manualPredictionModels[
      controls.datasetSchema.value || "legacy_original"
    ] = controls.predictionModel.value.trim();
    inspectPredictionModel(false).catch(() => {});
  }
});
$("startAcquisitionButton").addEventListener("click", startAcquisition);
$("stopAcquisitionButton").addEventListener("click", stopAcquisition);
controls.speed.addEventListener("change", restartPlaybackTimer);
controls.cursor.addEventListener("input", scheduleLoad);
controls.horizon.addEventListener("input", () => {
  state.requestedHorizon = Number(controls.horizon.value);
  controls.horizonNumber.value = controls.horizon.value;
  $("horizonValue").value = controls.horizon.value;
  scheduleLoad();
});
function commitPredictionHorizon() {
  const value = Math.max(1, Math.min(600, Number(controls.horizonNumber.value) || 24));
  state.requestedHorizon = value;
  controls.horizon.value = String(value);
  controls.horizonNumber.value = String(value);
  $("horizonValue").value = String(value);
  loadRealtime();
}
controls.horizonNumber.addEventListener("input", () => {
  const rawValue = Number(controls.horizonNumber.value);
  if (!Number.isFinite(rawValue) || rawValue < 1 || rawValue > 600) return;
  const value = Math.trunc(rawValue);
  state.requestedHorizon = value;
  controls.horizon.value = String(value);
  $("horizonValue").value = String(value);
  scheduleLoad();
});
controls.horizonNumber.addEventListener("change", commitPredictionHorizon);
controls.horizonNumber.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    commitPredictionHorizon();
    controls.horizonNumber.blur();
  }
});
controls.threshold.addEventListener("input", scheduleLoad);
controls.rho.addEventListener("input", scheduleLoad);
$("residualToggle").addEventListener("change", (event) => {
  state.showResidual = event.target.checked;
  if (state.payload) renderSeriesChart(state.payload.selected_channel);
});
window.addEventListener("resize", () => {
  if (!state.payload) return;
  renderSeriesChart(state.payload.selected_channel);
  renderTimeline(state.payload.timeline);
  renderSensorCards(state.payload.channels, state.payload.selection.sensor);
});

const COLUMN_LAYOUT_STORAGE_KEY = "afp-state-monitor-column-layout-v1";

function redrawChartsAfterColumnResize() {
  if (!state.payload) return;
  window.requestAnimationFrame(() => {
    renderSeriesChart(state.payload.selected_channel);
    renderTimeline(state.payload.timeline);
    renderSensorCards(state.payload.channels, state.payload.selection.sensor);
  });
}

function initializeColumnResizers() {
  const workspace = document.querySelector(".workspace-grid");
  const leftHandle = $("leftColumnResizer");
  const rightHandle = $("rightColumnResizer");
  if (!workspace || !leftHandle || !rightHandle) return;

  const limits = {
    leftMin: 220,
    leftMax: 430,
    rightMin: 230,
    rightMax: 430,
    mainMin: 480,
  };

  function currentWidths() {
    return {
      left: document.querySelector(".sidebar").getBoundingClientRect().width,
      right: document.querySelector(".warning-rail").getBoundingClientRect().width,
    };
  }

  function availableSideWidth() {
    const styles = getComputedStyle(workspace);
    const horizontalPadding = parseFloat(styles.paddingLeft) + parseFloat(styles.paddingRight);
    const columnGap = parseFloat(styles.columnGap || styles.gap) * 4;
    return Math.max(
      limits.leftMin + limits.rightMin,
      workspace.clientWidth - horizontalPadding - columnGap - 16 - limits.mainMin,
    );
  }

  function applyWidths(left, right, persist = true, changedSide = "left") {
    let safeLeft = Math.max(limits.leftMin, Math.min(limits.leftMax, left));
    let safeRight = Math.max(limits.rightMin, Math.min(limits.rightMax, right));
    const sideLimit = availableSideWidth();
    if (safeLeft + safeRight > sideLimit) {
      if (changedSide === "right") {
        safeRight = Math.max(limits.rightMin, sideLimit - safeLeft);
      } else {
        safeLeft = Math.max(limits.leftMin, sideLimit - safeRight);
      }
    }
    workspace.style.setProperty("--settings-width", `${Math.round(safeLeft)}px`);
    workspace.style.setProperty("--warning-width", `${Math.round(safeRight)}px`);
    leftHandle.setAttribute("aria-valuemin", String(limits.leftMin));
    leftHandle.setAttribute("aria-valuemax", String(limits.leftMax));
    leftHandle.setAttribute("aria-valuenow", String(Math.round(safeLeft)));
    rightHandle.setAttribute("aria-valuemin", String(limits.rightMin));
    rightHandle.setAttribute("aria-valuemax", String(limits.rightMax));
    rightHandle.setAttribute("aria-valuenow", String(Math.round(safeRight)));
    if (persist) {
      try {
        localStorage.setItem(COLUMN_LAYOUT_STORAGE_KEY, JSON.stringify({ left: safeLeft, right: safeRight }));
      } catch (_error) {
        // Local storage is optional; dragging still works in restricted desktop runtimes.
      }
    }
    redrawChartsAfterColumnResize();
  }

  function resetWidths() {
    workspace.style.removeProperty("--settings-width");
    workspace.style.removeProperty("--warning-width");
    try { localStorage.removeItem(COLUMN_LAYOUT_STORAGE_KEY); } catch (_error) {}
    const widths = currentWidths();
    applyWidths(widths.left, widths.right, false);
  }

  function startDrag(event, side) {
    if (window.matchMedia("(max-width: 1120px)").matches) return;
    event.preventDefault();
    const handle = side === "left" ? leftHandle : rightHandle;
    const startX = event.clientX;
    const start = currentWidths();
    handle.classList.add("dragging");
    document.body.classList.add("resizing-columns");

    function move(pointerEvent) {
      const delta = pointerEvent.clientX - startX;
      if (side === "left") applyWidths(start.left + delta, start.right, false, "left");
      else applyWidths(start.left, start.right - delta, false, "right");
    }

    function end() {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", end);
      window.removeEventListener("pointercancel", end);
      handle.classList.remove("dragging");
      document.body.classList.remove("resizing-columns");
      const widths = currentWidths();
      applyWidths(widths.left, widths.right, true, side);
    }

    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end, { once: true });
    window.addEventListener("pointercancel", end, { once: true });
  }

  leftHandle.addEventListener("pointerdown", (event) => startDrag(event, "left"));
  rightHandle.addEventListener("pointerdown", (event) => startDrag(event, "right"));
  leftHandle.addEventListener("dblclick", resetWidths);
  rightHandle.addEventListener("dblclick", resetWidths);

  [leftHandle, rightHandle].forEach((handle) => {
    handle.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      const delta = event.key === "ArrowRight" ? 12 : -12;
      const widths = currentWidths();
      if (handle === leftHandle) applyWidths(widths.left + delta, widths.right, true, "left");
      else applyWidths(widths.left, widths.right - delta, true, "right");
    });
  });

  try {
    const saved = JSON.parse(localStorage.getItem(COLUMN_LAYOUT_STORAGE_KEY) || "null");
    if (saved && Number.isFinite(saved.left) && Number.isFinite(saved.right)) {
      applyWidths(saved.left, saved.right, false);
    } else {
      const widths = currentWidths();
      applyWidths(widths.left, widths.right, false);
    }
  } catch (_error) {
    const widths = currentWidths();
    applyWidths(widths.left, widths.right, false);
  }
}

const VERTICAL_LAYOUT_STORAGE_KEY = "afp-state-monitor-vertical-layout-v1";

function initializeVerticalPanelResizer() {
  const workspace = document.querySelector(".workspace-grid");
  const handle = $("verticalPanelResizer");
  const main = document.querySelector(".main-content");
  if (!workspace || !handle || !main) return;
  const upperMin = 280;
  const lowerMin = 180;

  function upperMax() {
    const styles = getComputedStyle(workspace);
    const padding = parseFloat(styles.paddingTop) + parseFloat(styles.paddingBottom);
    const gaps = parseFloat(styles.rowGap || styles.gap) * 2;
    return Math.max(upperMin, workspace.clientHeight - padding - gaps - 8 - lowerMin);
  }

  function applyHeight(value, persist = true) {
    const safe = Math.max(upperMin, Math.min(upperMax(), value));
    workspace.style.setProperty("--upper-row-height", `${Math.round(safe)}px`);
    handle.setAttribute("aria-valuemin", String(upperMin));
    handle.setAttribute("aria-valuemax", String(Math.round(upperMax())));
    handle.setAttribute("aria-valuenow", String(Math.round(safe)));
    if (persist) {
      try { localStorage.setItem(VERTICAL_LAYOUT_STORAGE_KEY, String(safe)); } catch (_error) {}
    }
    redrawChartsAfterColumnResize();
  }

  function resetHeight() {
    workspace.style.removeProperty("--upper-row-height");
    try { localStorage.removeItem(VERTICAL_LAYOUT_STORAGE_KEY); } catch (_error) {}
    window.requestAnimationFrame(() => {
      handle.setAttribute("aria-valuenow", String(Math.round(main.getBoundingClientRect().height)));
      redrawChartsAfterColumnResize();
    });
  }

  handle.addEventListener("pointerdown", (event) => {
    if (window.matchMedia("(max-width: 1120px)").matches) return;
    event.preventDefault();
    const startY = event.clientY;
    const startHeight = main.getBoundingClientRect().height;
    handle.classList.add("dragging");
    document.body.classList.add("resizing-rows");

    function move(pointerEvent) {
      applyHeight(startHeight + pointerEvent.clientY - startY, false);
    }

    function end() {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", end);
      window.removeEventListener("pointercancel", end);
      handle.classList.remove("dragging");
      document.body.classList.remove("resizing-rows");
      applyHeight(main.getBoundingClientRect().height, true);
    }

    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end, { once: true });
    window.addEventListener("pointercancel", end, { once: true });
  });

  handle.addEventListener("dblclick", resetHeight);
  handle.addEventListener("keydown", (event) => {
    if (!["ArrowUp", "ArrowDown"].includes(event.key)) return;
    event.preventDefault();
    const delta = event.key === "ArrowDown" ? 16 : -16;
    applyHeight(main.getBoundingClientRect().height + delta, true);
  });

  try {
    const saved = Number(localStorage.getItem(VERTICAL_LAYOUT_STORAGE_KEY));
    if (Number.isFinite(saved) && saved > 0) applyHeight(saved, false);
    else resetHeight();
  } catch (_error) {
    resetHeight();
  }
}

initializeColumnResizers();
initializeVerticalPanelResizer();
initialize();
