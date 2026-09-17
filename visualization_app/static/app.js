"use strict";

const state = {
  bootstrap: null,
  payload: null,
  defaults: null,
  playing: false,
  timer: null,
  requestTimer: null,
  livePollTimer: null,
  liveSocket: null,
  liveSocketQuery: "",
  liveSocketReconnectTimer: null,
  liveSocketReconnectAttempt: 0,
  busy: false,
  reloadQueued: false,
  requestedHorizon: null,
  showResidual: false,
  showLayerEvidence: true,
  manualPredictionModels: {},
  liveScopeKey: null,
  interfaceCatalog: [],
  availableInterfaces: [],
  interfaceAssignments: {},
  sensorTypeProfiles: [],
  acquisitionStatus: null,
  hardwareCheck: null,
  hardwareCheckFingerprint: "",
  hardwareCheckInProgress: false,
  hardwareCheckController: null,
  hardwareCheckTimer: null,
  mysqlConnectionTests: {local: null, target: null},
  agentEvents: [],
  agentResult: null,
  agentFingerprint: "",
  agentBusy: false,
  agentController: null,
  agentJobId: "",
  agentRequestId: 0,
  agentDefaultKeyAvailable: false,
  physicalInterfaces: [],
  // Keep the last real-mode mapping out of the simulation catalog.  Mode
  // switches must not force a second pairing just to redraw the cards.
  realInterfaceSnapshot: null,
  realInterfaceSnapshotAt: 0,
  lanStatusTimer: null,
  accessRole: "guest",
  authenticated: false,
  secureTransport: false,
  csrf: "",
  modelAccess: false,
  realControlOwner: null,
  realHeartbeatTimer: null,
  guestSimulationStarted: false,
  guestSimulationStoppedByUser: false,
  simulationDatasetCache: null,
  simulationDatasetPromise: null,
  simulationPlaybackIndex: -1,
  simulationLocalReplay: false,
  simulationLocalReplayTimer: null,
  localSaveDirectoryHandle: null,
  localSaveAuthorized: false,
  localSaveBusy: false,
  localSaveNameDirty: false,
  saveStatusTimer: null,
  simulationSourceChannels: [],
  helperStatus: {paired: false, online: false, capabilities: {}},
  helperPairingChallenge: "",
  helperStatusTimer: null,
  processParameterBusy: false,
};

const $ = (id) => document.getElementById(id);

const agentApiKeyInput = $("agentApiKeyInput");
const agentModelNameInput = $("agentModelNameInput");
const agentDiagnoseButton = $("agentDiagnoseButton");

function readCookie(name) {
  const prefix = `${name}=`;
  return document.cookie.split(";").map((item) => item.trim())
    .find((item) => item.startsWith(prefix))?.slice(prefix.length) || "";
}

function renderAccessState() {
  const badge = $("access-mode-badge");
  const unlock = $("unlock-real-mode");
  const lock = $("lock-real-mode");
  const adminSettings = $("admin-security-settings");
  const acquisitionMode = $("acquisitionModeSelect");
  const agentAccess = $("agent-model-access");
  const note = $("real-access-transport-note");
  if (badge) {
    badge.textContent = state.accessRole === "local_admin" ? "本机管理模式"
      : state.accessRole === "lan_operator" ? "局域网边缘采集模式"
      : state.accessRole === "authorized" ? "真实模式已解锁" : "访客模拟模式";
    badge.className = `access-mode-badge ${state.accessRole}`;
  }
  unlock?.classList.toggle("hidden", state.accessRole !== "guest");
  lock?.classList.toggle("hidden", state.accessRole === "guest");
  adminSettings?.classList.toggle("hidden", state.accessRole !== "local_admin");
  if (acquisitionMode && state.accessRole === "guest") {
    acquisitionMode.value = "simulation";
    acquisitionMode.disabled = true;
  } else if (acquisitionMode) {
    acquisitionMode.disabled = false;
  }
  if (agentAccess) {
    agentAccess.textContent = state.modelAccess ? "服务器模型：已授权可用" : "服务器模型：未授权（本地规则）";
  }
  if (note) note.textContent = state.secureTransport
    ? "请输入管理员授权密码。密码仅通过 HTTPS 提交。"
    : "当前不是 HTTPS 公网地址，不能提交密码；可继续使用访客模拟模式。";
  renderAgentGate();
}

async function loadAccessSession() {
  const response = await fetch("/api/auth/session", {
    cache: "no-store", credentials: "same-origin",
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "授权状态读取失败");
  state.accessRole = payload.role || "guest";
  state.authenticated = Boolean(payload.authenticated);
  state.modelAccess = Boolean(payload.model_access);
  state.secureTransport = Boolean(payload.secure_transport);
  state.csrf = readCookie("afp_csrf");
  renderAccessState();
  return payload;
}

function usesLocalCaptureHelper() {
  return state.accessRole === "lan_operator" || state.accessRole === "authorized";
}

function renderHelperStatus() {
  const badge = $("helperStatus");
  const note = $("helperStatusNote");
  const code = $("helperPairingCode");
  const button = $("pairHelperButton");
  if (!badge) return;
  if (state.accessRole === "local_admin") {
    badge.textContent = "内置采集服务";
    badge.className = "helper-status ok";
    if (note) note.textContent = "服务器本机采集，无需辅助程序。";
    button?.classList.toggle("hidden", true);
    code?.classList.toggle("hidden", true);
    return;
  }
  const helper = state.helperStatus || {};
  badge.textContent = helper.online ? "已连接" : helper.paired ? "已配对，等待连接" : "未配对";
  badge.className = `helper-status ${helper.online ? "ok" : helper.paired ? "pending" : "error"}`;
  if (note) {
    note.textContent = state.accessRole === "guest"
      ? (state.secureTransport
        ? "当前为访客模拟模式；请先解锁真实模式，再生成配对码。"
        : "当前为访客模拟模式；请使用 HTTPS 公网地址解锁真实模式后，再生成配对码。")
      : helper.online
        ? "本机辅助程序已连接，可识别本机接口并执行真实采集。"
        : helper.paired
          ? "已生成配对信息，请在本机辅助程序中输入配对码并连接。"
          : "真实采集需要在访问此网页的电脑上运行本地采集辅助程序。";
  }
  // Keep the entry visible in guest mode.  Hiding it made a normal
  // permission prerequisite look like a broken pairing-code generator.
  button?.classList.toggle("hidden", false);
  if (button) {
    button.textContent = state.accessRole === "guest" ? "解锁后生成配对码" : "生成配对码";
    button.title = state.accessRole === "guest"
      ? "先解锁真实模式，再生成配对码"
      : "为本机采集辅助程序生成一次性配对码";
  }
  if (code) {
    code.textContent = state.helperPairingChallenge ? `配对码：${state.helperPairingChallenge}` : "";
    code.classList.toggle("hidden", !state.helperPairingChallenge);
  }
}

async function loadHelperStatus({deferDiscovery = false} = {}) {
  const wasOnline = Boolean(state.helperStatus?.online);
  if (state.accessRole === "guest") {
    state.helperStatus = {paired: false, online: false, capabilities: {}};
    renderHelperStatus();
    return;
  }
  try {
    const result = await fetch("/api/helper/status", {cache: "no-store"}).then((response) => response.json());
    if (result && !result.error) state.helperStatus = result;
  } catch (_error) {
    state.helperStatus = {paired: false, online: false, capabilities: {}, lastError: "辅助程序状态读取失败"};
  }
  renderHelperStatus();
  // A public page can finish loading before the helper reconnects.  Once the
  // helper heartbeat becomes online, refresh the physical-interface mapping
  // automatically so the panel does not remain on the initial "未分配" state.
  if (
    usesLocalCaptureHelper()
    && state.helperStatus.online
    && !wasOnline
  ) {
    if (deferDiscovery) {
      void discoverInterfaces().catch(() => {});
    } else {
      await discoverInterfaces();
    }
  }
}

async function pairLocalHelper() {
  const button = $("pairHelperButton");
  const note = $("helperStatusNote");
  if (state.accessRole === "guest") {
    if (note) note.textContent = state.secureTransport
      ? "当前是访客模式；请先解锁真实模式，再生成配对码。"
      : "当前是访客模式；请使用 HTTPS 公网地址解锁真实模式，再生成配对码。";
    if (state.secureTransport) showRealAccessModal();
    return;
  }
  // A login in another tab or a restored session can leave the in-memory
  // role/CSRF value stale.  Refresh once before the protected pairing call.
  if (!state.csrf) await loadAccessSession();
  if (state.accessRole === "guest") {
    if (note) note.textContent = "当前网页会话未解锁，无法生成配对码。";
    return;
  }
  if (button) {
    button.disabled = true;
    button.textContent = "正在生成配对码…";
  }
  if (note) note.textContent = "正在生成一次性配对码，请稍候……";
  try {
    let result;
    try {
      result = await postJson("/api/helper/pair/start", {});
    } catch (error) {
      // A long-lived page can retain an old CSRF cookie after a login or
      // public-tunnel reconnect. Refresh the session and retry once.
      if (error?.code !== "csrf_failed") throw error;
      await loadAccessSession();
      if (state.accessRole === "guest") throw error;
      result = await postJson("/api/helper/pair/start", {});
    }
    if (!result?.ok) throw new Error(result?.error || "配对码生成失败");
    state.helperPairingChallenge = String(result.challenge || "");
    state.helperStatus = {...state.helperStatus, paired: false, online: false, lastError: ""};
    renderHelperStatus();
    if (note) note.textContent = "配对码已生成；请将下方代码输入本机辅助程序，保持辅助程序运行。";
    toast("配对码已生成，请在本机采集辅助程序中输入");
  } catch (error) {
    state.helperStatus = {...state.helperStatus, lastError: error.message || String(error)};
    renderHelperStatus();
    if (note) note.textContent = `配对码生成失败：${error.message || error}`;
    throw error;
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "重新生成配对码";
    }
  }
}

function showRealAccessModal() {
  const modal = $("real-access-modal");
  if (!modal) return;
  modal.classList.remove("hidden");
  const input = $("real-access-password");
  if (input) { input.value = ""; input.focus(); }
}

function hideRealAccessModal() { $("real-access-modal")?.classList.add("hidden"); }

async function showAdminSecuritySettings() {
  if (state.accessRole !== "local_admin") return;
  const modal = $("admin-security-modal");
  const status = $("admin-security-status");
  try {
    const response = await fetch("/api/admin/security/settings", {cache: "no-store", credentials: "same-origin"});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "无法读取安全设置");
    $("admin-model-name").value = payload.security?.model_name || "deepseek-ai/DeepSeek-V3";
    $("admin-api-key").value = "";
    $("admin-clear-api-key").checked = false;
    if (status) status.textContent = payload.security?.configured
      ? `已配置：${payload.security.model_configured ? "模型 Key 可用" : "仅密码授权"}`
      : "尚未配置，请设置授权密码。";
    modal?.classList.remove("hidden");
    $("admin-password")?.focus();
  } catch (error) { toast(error.message); }
}

function hideAdminSecuritySettings() { $("admin-security-modal")?.classList.add("hidden"); }

async function saveAdminSecuritySettings() {
  const password = $("admin-password")?.value || "";
  const modelName = $("admin-model-name")?.value.trim() || "";
  const key = $("admin-api-key")?.value || "";
  const clearKey = Boolean($("admin-clear-api-key")?.checked);
  if (password.length < 12) { toast("授权密码至少需要 12 个字符"); return; }
  if (!modelName) { toast("模型名称不能为空"); return; }
  const status = $("admin-security-status");
  try {
    if (status) status.textContent = "正在保存安全设置……";
    await postJson("/api/admin/security/settings", {
      password, model_name: modelName, ...(clearKey ? {clear_api_key: true} : key ? {api_key: key} : {}),
    });
    if (status) status.textContent = "保存成功；公共网页现在可以使用新密码解锁。";
    $("admin-password").value = "";
    $("admin-api-key").value = "";
    toast("安全设置已保存");
  } catch (error) { if (status) status.textContent = error.message; }
}

async function unlockRealMode() {
  if (!state.secureTransport) {
    toast("请使用公网 HTTPS 地址登录，局域网 HTTP 不提交密码");
    return;
  }
  const password = $("real-access-password")?.value || "";
  if (!password) { toast("请输入授权密码"); return; }
  const submit = $("real-access-submit");
  const note = $("real-access-transport-note");
  if (submit?.disabled) return;
  if (submit) {
    submit.disabled = true;
    submit.textContent = "正在解锁…";
  }
  if (note) note.textContent = "正在验证授权密码，请稍候……";
  try {
    const result = await postJson("/api/auth/login", {password}, {timeoutMs: 15000});
    hideRealAccessModal();
    // Apply the login response immediately; the follow-up session/defaults
    // requests are reconciled in the background to avoid tunnel round-trips
    // blocking the unlocked controls.
    state.accessRole = result.role || "authorized";
    state.authenticated = result.authenticated !== false;
    state.modelAccess = Boolean(result.model_access);
    state.csrf = readCookie("afp_csrf");
    renderAccessState();
    state.mysqlConnectionTests.target = null;
    syncTargetMysqlSection();
    toast(result.model_access ? "真实模式已解锁，服务器模型可用" : "真实模式已解锁，将使用本地规则");
    void loadAccessSession().catch(() => {});
    void loadMysqlDefaults().then(() => syncTargetMysqlSection()).catch(() => {});
  } catch (error) {
    if (note) note.textContent = error.message || "授权失败，请检查密码后重试";
    toast(error.message);
  } finally {
    if (submit) {
      submit.disabled = false;
      submit.textContent = "确认解锁";
    }
  }
}

async function lockRealMode() {
  try { await postJson("/api/auth/logout", {}); } catch (error) { toast(error.message); }
  state.realControlOwner = null;
  state.guestSimulationStarted = false;
  state.guestSimulationStoppedByUser = false;
  window.clearInterval(state.realHeartbeatTimer);
  state.realHeartbeatTimer = null;
  await loadAccessSession().catch(() => {});
}

async function acquireRealControl() {
  if (state.accessRole === "guest") { showRealAccessModal(); return false; }
  if (state.accessRole === "local_admin") return true;
  const result = await postJson("/api/real/control/acquire", {});
  if (!result.granted) throw new Error("真实采集控制权正在被其他会话占用");
  state.realControlOwner = result.control?.owner_id || true;
  window.clearInterval(state.realHeartbeatTimer);
  state.realHeartbeatTimer = window.setInterval(() => {
    postJson("/api/real/control/heartbeat", {}).catch(() => {});
  }, 10000);
  return true;
}

function setLanWebStatus(mode, url = "", stateClass = "") {
  const panel = $("lan-web-status");
  const modeNode = $("lan-web-mode");
  const urlNode = $("lan-web-url");
  if (modeNode) modeNode.textContent = mode;
  if (urlNode) urlNode.textContent = url || "请查看采集电脑网络设置";
  panel?.classList.toggle("connected", stateClass === "connected");
  panel?.classList.toggle("error", stateClass === "error");
}

async function refreshLanWebStatus() {
  const response = await fetch("/api/network/status", {cache: "no-store"});
  const status = await response.json();
  if (!response.ok) throw new Error(status.error || "局域网状态读取失败");
  const urls = Array.isArray(status.urls) ? status.urls : [];
  const mode = status.error ? "启动失败" : status.enabled ? "局域网已开启" : "仅本机";
  setLanWebStatus(mode, urls[0] || status.desktop_url, status.error ? "error" : "connected");
  return status;
}

function markServerDisconnected() {
  setLanWebStatus("服务器连接中断，采集仍在服务器运行", "", "error");
}

async function copyLanWebUrl() {
  const url = $("lan-web-url")?.textContent?.trim();
  if (!url || url.startsWith("请查看")) {
    toast("当前还没有可复制的局域网网址");
    return;
  }
  try {
    await navigator.clipboard.writeText(url);
    toast("局域网网址已复制");
  } catch (_error) {
    const fallback = document.createElement("textarea");
    fallback.value = url;
    fallback.style.position = "fixed";
    fallback.style.opacity = "0";
    document.body.appendChild(fallback);
    fallback.select();
    document.execCommand("copy");
    fallback.remove();
    toast("局域网网址已复制");
  }
}

// Keep the alignment control beside the horizon control even when an older
// cached index.html is opened by the browser or an older packaged build.
function ensureForecastLeadControls() {
  if ($("leadInput") || !$("horizonNumber")) return;
  const horizonNumber = $("horizonNumber");
  if (!horizonNumber?.parentElement) return;
  const label = document.createElement("label");
  label.innerHTML =
    '<span>历史/预警固定提前量 <output id="leadValue">1</output> 点</span>' +
    '<input id="leadInput" type="range" min="1" max="24" value="1">';
  const row = document.createElement("div");
  row.className = "range-number-row";
  row.innerHTML =
    '<span>1</span><input id="leadNumber" type="number" min="1" max="24" step="1" value="1" aria-label="固定预测提前量" title="输入1至24"><span>24</span>';
  const note = document.createElement("p");
  note.className = "control-note forecast-alignment-note";
  note.textContent =
    "未来曲线使用最新滚动预测；历史曲线和实时对齐评价使用冻结的因果提前量。";
  horizonNumber.parentElement.after(label, row, note);
}
ensureForecastLeadControls();
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
  forecastLead: $("leadInput"),
  forecastLeadNumber: $("leadNumber"),
  forecastLeadValue: $("leadValue"),
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
  firstInterfaceRole: $("firstInterfaceRole"),
  interfacePanel: $("interfacePanel"),
  discoverInterfaces: $("discoverInterfacesButton"),
  addInterface: $("addInterfaceButton"),
  interfaceDiscoveryStatus: $("interfaceDiscoveryStatus"),
  sourceFile: $("sourceFileInput"),
  endpoint: $("endpointInput"),
  baudrate: $("baudrateInput"),
  sampleRate: $("sampleRateInput"),
  runId: $("runIdInput"),
  liveSpecimen: $("liveSpecimenInput"),
  saveRoot: $("saveRootInput"),
  saveRootStatus: $("saveRootStatus"),
  confirmLocalSave: $("confirmLocalSaveButton"),
  localSaveStatus: $("localSaveStatus"),
  mysqlEnabled: $("mysqlEnabledInput"),
  mysqlLocalDetails: $("mysqlLocalDetails"),
  mysqlTargetDetails: $("mysqlTargetDetails"),
  mysqlLocalEnabled: $("mysqlLocalEnabledInput"),
  mysqlLocalHost: $("mysqlLocalHostInput"),
  mysqlLocalPort: $("mysqlLocalPortInput"),
  mysqlLocalUser: $("mysqlLocalUserInput"),
  mysqlLocalPassword: $("mysqlLocalPasswordInput"),
  mysqlLocalDatabase: $("mysqlLocalDatabaseInput"),
  mysqlHost: $("mysqlHostInput"),
  mysqlPort: $("mysqlPortInput"),
  mysqlUser: $("mysqlUserInput"),
  mysqlPassword: $("mysqlPasswordInput"),
  mysqlDatabase: $("mysqlDatabaseInput"),
  testMysql: $("testMysqlButton"),
  remoteMysqlQuery: $("remoteMysqlQueryInput"),
  remoteMysqlLimit: $("remoteMysqlLimitInput"),
  remoteMysqlStatus: $("remoteMysqlStatus"),
  remoteMysqlPreview: $("remoteMysqlPreviewTable"),
  predictionModel: $("predictionModelInput"),
  predictionModelType: $("predictionModelTypeSelect"),
  livePower: $("livePowerInput"),
  liveSpeed: $("liveSpeedInput"),
  livePressure: $("livePressureInput"),
  liveLayer: $("liveLayerInput"),
  initialForce: $("initialForceInput"),
  placementSpeed: $("placementSpeedInput"),
  pidAngle: $("pidAngleInput"),
  temperatureSetpoint: $("temperatureSetpointInput"),
  autoProcessParameters: $("autoProcessParametersInput"),
  readProcessParameters: $("readProcessParametersButton"),
  processParameterReadStatus: $("processParameterReadStatus"),
  conditionId: $("conditionIdInput"),
  replicate: $("replicateInput"),
  newLayer: $("newLayerInput"),
  showLayerEvidence: $("showLayerEvidenceInput"),
  autoHardwareCheck: $("autoHardwareCheckInput"),
  resetSensorCheck: $("resetSensorCheckButton"),
  hardwareCheckStatus: $("hardwareCheckStatus"),
};

function syncTargetMysqlSection() {
  return window.MysqlVisibility?.syncTargetMysqlVisibility(
    controls.mysqlEnabled,
    controls.mysqlTargetDetails,
  );
}

function syncLocalMysqlSection() {
  return window.MysqlVisibility?.syncLocalMysqlVisibility(
    controls.mysqlLocalEnabled,
    controls.mysqlLocalDetails,
  );
}

function unifiedMysqlSettings(extra = {}) {
  return {
    mysql_enabled: true,
    mysql_host: controls.mysqlHost?.value.trim() || "192.168.101.31",
    mysql_port: Number(controls.mysqlPort?.value) || 3306,
    mysql_user: controls.mysqlUser?.value.trim() || "afp_app",
    mysql_password: controls.mysqlPassword?.value ?? "",
    mysql_database: controls.mysqlDatabase?.value.trim() || "afp_state_warning",
    mysql_charset: "utf8mb4",
    ...extra,
  };
}

function unifiedLocalMysqlSettings(extra = {}) {
  return {
    mysql_enabled: true,
    mysql_host: controls.mysqlLocalHost?.value.trim() || "127.0.0.1",
    mysql_port: Number(controls.mysqlLocalPort?.value) || 3306,
    mysql_user: controls.mysqlLocalUser?.value.trim() || "root",
    mysql_password: controls.mysqlLocalPassword?.value ?? "",
    mysql_database: controls.mysqlLocalDatabase?.value.trim() || "afp_state_warning",
    mysql_charset: "utf8mb4",
    ...extra,
  };
}

async function loadMysqlDefaults() {
  try {
    const response = await fetch("/api/mysql/defaults", {cache: "no-store"});
    const profile = await response.json();
    if (!response.ok) return;
    const target = profile.target || {};
    const local = profile.local || {};
    const assign = (control, value) => {
      if (control && value !== undefined && value !== null) control.value = String(value);
    };
    assign(controls.mysqlHost, target.host);
    assign(controls.mysqlPort, target.port);
    assign(controls.mysqlUser, target.user);
    assign(controls.mysqlPassword, target.password);
    assign(controls.mysqlDatabase, target.database);
    // The server profile describes the acquisition host.  A public page's
    // local-MySQL section belongs to the visitor computer and is executed by
    // its paired helper, so never overwrite those fields with server-local
    // credentials.  The desktop/LAN admin page is the one case where both
    // sides are the same machine and the profile is authoritative.
    if (state.accessRole === "local_admin") {
      assign(controls.mysqlLocalHost, local.host);
      assign(controls.mysqlLocalPort, local.port);
      assign(controls.mysqlLocalUser, local.user);
      assign(controls.mysqlLocalPassword, local.password);
      assign(controls.mysqlLocalDatabase, local.database);
    }
  } catch (_) {
    // Static defaults remain usable when a legacy backend lacks this endpoint.
  }
}

const LAYER_EVIDENCE_VISIBILITY_KEY = "afp-show-layer-evidence-v1";

function applyLayerEvidenceVisibility() {
  const visible = Boolean(state.showLayerEvidence);
  const evidenceDock = $("evidenceDock");
  if (evidenceDock) evidenceDock.classList.toggle("hidden", !visible);
  $("layerScore")?.classList.toggle("hidden", !visible);
  $("specimenScore")?.classList.toggle("hidden", !visible);
  if (controls.showLayerEvidence) controls.showLayerEvidence.checked = visible;
}

function option(value, text) {
  const node = document.createElement("option");
  node.value = value;
  node.textContent = text;
  return node;
}

function sensorTypeProfile(role) {
  const canonical = role === "thermal"
    ? "thermal_uvc"
    : (["other", "new_sensor"].includes(role) ? "custom" : role);
  return state.sensorTypeProfiles.find((item) => item.id === canonical)
    || state.sensorTypeProfiles.find((item) => item.id === "custom")
    || {
      id: "custom", label: "自定义JSON传感器", driver: "serial_json",
      endpoint: "COM4", channels: [], editable_driver: true,
    };
}

function applySensorTypeProfile(row, resetEndpoint = true) {
  const role = row.querySelector(".interface-role");
  const driver = row.querySelector(".interface-driver");
  const endpoint = row.querySelector(".interface-endpoint");
  const map = row.querySelector(".interface-map");
  const mapLabel = map?.closest(".interface-map-label");
  const detail = row.querySelector(".interface-profile-detail");
  const profile = sensorTypeProfile(role?.value || "custom");
  if (driver) {
    driver.value = profile.driver || "serial_json";
    driver.disabled = !profile.editable_driver;
  }
  if (endpoint && (resetEndpoint || !endpoint.value.trim())) {
    endpoint.value = profile.endpoint || "";
  }
  if (map) map.disabled = !profile.editable_driver;
  if (mapLabel) mapLabel.classList.toggle("hidden", !profile.editable_driver);
  if (detail) {
    const channels = (profile.channels || []).join("、")
      || (profile.id === "thermal_rtsp" ? "无数值通道（仅视频）" : "需显式映射");
    detail.textContent = `数据通道：${channels}；${profile.processing || ""}`;
  }
}

function populatePredictionModelTypes() {
  if (!controls.predictionModelType) return;
  const models = state.bootstrap?.acquisition?.prediction_models || [];
  const schemaId = controls.datasetSchema?.value || "legacy_original";
  const current = state.bootstrap?.acquisition?.prediction_model?.model_type || "i_T_G";
  const nodes = models.map((item) => {
    const available = item.available_by_schema?.[schemaId] !== false;
    const node = option(
      item.id,
      `${item.label || item.id}${available ? "" : "（当前方案无权重）"}`
    );
    node.disabled = !available;
    return node;
  });
  controls.predictionModelType.replaceChildren(...nodes);
  const availableModels = models.filter(
    (item) => item.available_by_schema?.[schemaId] !== false
  );
  controls.predictionModelType.value = availableModels.some(
    (item) => item.id === current
  ) ? current : (availableModels[0]?.id || "i_T_G");
}

function fmt(value, digits = 3) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(digits) : "—";
}

// v2 intentionally starts clean so an older installation cannot restore a
// source-tree/internal checkpoint instead of the matching EXE-local weight.
// New manual selections are still remembered from this version onward.
const MODEL_SELECTION_HISTORY_KEY = "afp-model-selection-history-v2";
function modelSelectionKey(schemaMode, modelType) {
  return `${schemaMode || "legacy_original"}::${modelType || "i_T_G"}`;
}
function readModelSelectionHistory() {
  try {
    const value = JSON.parse(localStorage.getItem(MODEL_SELECTION_HISTORY_KEY) || "{}");
    return value && typeof value === "object" ? value : {};
  } catch (_) {
    return {};
  }
}
function rememberModelPath(schemaMode, modelType, checkpoint) {
  if (!checkpoint) return;
  const history = readModelSelectionHistory();
  history[modelSelectionKey(schemaMode, modelType)] = checkpoint;
  try {
    localStorage.setItem(MODEL_SELECTION_HISTORY_KEY, JSON.stringify(history));
  } catch (_) {
    // Browser storage can be disabled; the server-side history remains active.
  }
}
function lastModelPath(schemaMode, modelType) {
  return readModelSelectionHistory()[modelSelectionKey(schemaMode, modelType)] || "";
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














function liveEvidenceScopeKey() {
  const newSchema = controls.datasetSchema.value === "new_collection_v11_3";
  return JSON.stringify({
    schema: controls.datasetSchema.value || "legacy_original",
    specimen: controls.liveSpecimen.value.trim() || "LIVE_SPECIMEN",
    run: controls.runId.value.trim() || "LIVE_RUN",
    condition: newSchema
      ? (controls.conditionId.value.trim() || "H06")
      : "LIVE",
    replicate: Number(controls.replicate.value) || 1,
    indicator: controls.indicator.value || "TC-HI",
    model: controls.model.value || "random_forest",
    power: Number(controls.livePower.value) || 0,
    speed: Number(controls.liveSpeed.value) || 0,
    pressure: Number(controls.livePressure.value) || 0,
    initialForce: Number(controls.initialForce.value) || 0,
    placementSpeed: Number(controls.placementSpeed.value) || 0,
    pidAngle: Number(controls.pidAngle.value) || 0,
    temperatureSetpoint: Number(controls.temperatureSetpoint.value) || 0,
  });
}

function resetLiveEvidenceDisplay() {
  if (controls.dataMode.value !== "live") return;
  renderLayerProgress([]);
  state.payload = null;
  state.liveScopeKey = liveEvidenceScopeKey();
  const layerControl = controls.datasetSchema.value === "new_collection_v11_3"
    ? controls.newLayer
    : controls.liveLayer;
  if (layerControl && !layerControl.disabled) layerControl.value = "0";
  statePill($("layerState"), "等待第1层", "pending");
  statePill($("specimenState"), "等待新试样", "pending");
  $("layerScore").textContent = "—";
  $("specimenScore").textContent = "—";
  $("layerDetail").textContent = "新试样尚未采集，第1层证据为0";
  $("specimenDetail").textContent = "当前试样尚无铺层证据";
  $("layerPreviewBadge").textContent = "等待第1层证据";
  $("streamStatus").textContent = "等待新试样采集";
  document.querySelector(".live-dot")?.classList.remove("active");
}

function markLiveScopeChanged() {
  if (controls.dataMode.value !== "live") return;
  const next = liveEvidenceScopeKey();
  if (state.liveScopeKey !== null && next !== state.liveScopeKey) {
    resetLiveEvidenceDisplay();
  } else {
    state.liveScopeKey = next;
  }
}

function enforceStepOneInteger(control, minimum) {
  if (!control) return;
  const normalize = () => {
    const raw = Number(control.value);
    const value = Number.isFinite(raw)
      ? Math.max(minimum, Math.trunc(raw))
      : minimum;
    control.value = String(value);
  };
  // The native number spinner is step=1; normalization also handles pasted
  // decimals or text without imposing an upper limit on the count.
  control.addEventListener("change", normalize);
  control.addEventListener("blur", normalize);
}

async function postJson(url, payload = {}, {timeoutMs = 30000, controller = null} = {}) {
  const requestController = controller || (typeof AbortController === "function" ? new AbortController() : null);
  const effectiveTimeout = Math.max(1000, Number(timeoutMs) || 30000);
  const timer = window.setTimeout(() => requestController?.abort(), effectiveTimeout);
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-AFP-CSRF": state.csrf || readCookie("afp_csrf"),
      },
      body: JSON.stringify(payload),
      credentials: "same-origin",
      ...(requestController ? {signal: requestController.signal} : {}),
    });
    const result = await response.json();
    if (!response.ok) {
      const code = String(result.error || "");
      const messages = {
        csrf_failed: "网页会话已刷新，请重试当前操作",
        real_access_required: "请先解锁真实采集模式",
        authorized_session_required: "真实采集会话未建立，请重新解锁真实模式",
        helper_offline: "本机采集辅助程序未连接",
      };
      const error = new Error(messages[code] || code || "请求失败");
      error.code = code;
      throw error;
    }
    return result;
  } catch (error) {
    if (error?.name === "AbortError") {
      throw new Error(`请求超时（${Math.round(effectiveTimeout / 1000)}秒），请检查设备连接后重试`);
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

async function requestLocalHelper(command, payload = {}, {timeoutMs = 30000} = {}) {
  if (!usesLocalCaptureHelper() || !state.helperStatus?.paired) {
    throw new Error("尚未配对访问者电脑上的本地采集辅助程序");
  }
  const queued = await postJson("/api/helper/command", {command, payload});
  if (!queued?.ok || !queued?.queued) {
    throw new Error(queued?.error === "helper_offline"
      ? "本地采集辅助程序未连接"
      : queued?.error || "本地辅助程序命令发送失败");
  }
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const response = await fetch(`/api/helper/result?request_id=${encodeURIComponent(queued.request_id)}`, {cache: "no-store"});
    const result = await response.json();
    if (result?.ok && result.payload !== null && result.payload !== undefined) return result.payload;
    await new Promise((resolve) => window.setTimeout(resolve, 250));
  }
  throw new Error("本地采集辅助程序响应超时");
}

function renderAcquisitionStatus(status) {
  state.acquisitionStatus = status;
  if (status?.save_status) renderSaveRootStatus(status.save_status);
  const node = $("acquisitionStatus");
  const selected = (status.sensors || []).filter((item) => item.selected);
  // The backend applies the authoritative interface-to-channel routing.  In
  // simulation mode mirror that effective list back into the checklist so a
  // channel without an enabled interface cannot look as if it was collected.
  if (
    status.running
    && status.config?.acquisition_mode === "simulation"
    && Array.isArray(status.config?.selected_sensors)
  ) {
    const effective = new Set(status.config.selected_sensors);
    document.querySelectorAll("#liveSensorChecklist .sensor-checklist-row").forEach((row) => {
      const channel = row.querySelector(".save-sensor-checkbox")?.value;
      const collected = effective.has(channel);
      const collect = row.querySelector(".save-sensor-checkbox");
      const input = row.querySelector(".model-input-sensor-checkbox");
      const output = row.querySelector(".predict-sensor-checkbox");
      if (collect) collect.checked = collected;
      if (!collected) {
        if (input) input.checked = false;
        if (output) output.checked = false;
      }
    });
  }
  renderMysqlStatus(status.mysql);
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
  // The data scheme belongs to the current acquisition session.  Lock it only
  // while the stream is actually running; after stop() has completed, both
  // real and simulated acquisitions must be able to choose a new scheme even
  // when the previous session left rows/files on disk.
  if (controls.datasetSchema) controls.datasetSchema.disabled = locked;
  if (controls.acquisitionMode) controls.acquisitionMode.disabled = locked;
  if (controls.processingMode) controls.processingMode.disabled = locked;
  // Layer and replicate identifiers are frozen for the whole acquisition.
  // They become editable again only after stop() has completed the file save,
  // preventing a running stream from silently changing its physical label.
  [controls.liveLayer, controls.newLayer, controls.replicate]
    .filter(Boolean)
    .forEach((control) => {
      control.disabled = locked;
    });
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
  const captureUuid = status.capture_uuid
    ? String(status.capture_uuid)
    : "";
  const quality = status.data_quality || {};
  const qualityText = !status.running && Number(quality.sample_count) > 1
    ? ` · 实际采样 ${Number(quality.effective_sample_rate_hz || 0).toFixed(2)} Hz` +
      ` · 最大间隔 ${Number(quality.maximum_gap_ms || 0).toFixed(1)} ms`
    : "";
  node.textContent =
    `${status.running ? "采集中" : "已停止"} · ${status.sample_count || 0}点 · ` +
    `${captureOnly
      ? `采集通道 ${healthy.length}/${selected.length} 正常`
      : `模型输入 ${expectedInputCount}通道 · 模型输出 ${predictionCount}通道`} · ${readiness}` +
    `${captureUuid ? ` · 试样会话：${captureUuid}` : ""}` +
    qualityText +
    `${status.archived_previous_session ? " · 已自动归档上一试样，避免混层" : ""}` +
    `${status.layer_file ? ` · 分层文件：${status.layer_file}` : ""}` +
    `${status.full_specimen_file ? ` · 完整试样：${status.full_specimen_file}` : ""}` +
    `${status.last_error ? ` · 错误：${status.last_error}` : ""}`;
}

async function waitForEdgeFirstSample(captureUuid, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const response = await fetch("/api/acquisition/status?acquisition_mode=real", {
      cache: "no-store",
      credentials: "same-origin",
    });
    const status = await response.json();
    if (!response.ok) throw new Error(status.error || "无法读取边缘采集状态");
    if (
      status.capture_uuid === captureUuid
      && status.first_sample_received
    ) return status;
    if (!state.helperStatus?.online) {
      throw new Error("本地采集辅助程序已离线，尚未收到首批数据");
    }
    await new Promise((resolve) => window.setTimeout(resolve, 250));
  }
  throw new Error("辅助程序已启动，但服务器在15秒内未收到首批有效采集数据");
}

function renderRuntimeStatus(payload = state.payload) {
  const node = $("streamStatus");
  const dot = document.querySelector(".live-dot");
  if (!node || !dot) return;

  const liveMode = controls.dataMode.value === "live";
  if (!liveMode) {
    node.textContent = state.playing ? "实时数据运行中" : "实时数据已暂停";
    dot.classList.toggle("active", state.playing);
    return;
  }

  const acquisition = payload?.acquisition || {};
  const captureOnly = acquisition.config?.processing_mode === "capture_only"
    || controls.processingMode.value === "capture_only";
  const simulation = acquisition.config?.acquisition_mode === "simulation";
  const windowData = payload?.window || {};
  const warningState = windowData.state_label || "等待窗口证据";
  let label;
  if (acquisition.last_error) {
    label = `采集错误：${acquisition.last_error}`;
  } else if (acquisition.running) {
    if (captureOnly) {
      label = `${simulation ? "模拟采集" : "真实采集"}中（仅保存） · ${acquisition.sample_count || 0}点`;
    } else if (!acquisition.model_ready) {
      label = `${simulation ? "模拟采集" : "真实采集"}中（等待预测输入） · ${acquisition.sample_count || 0}点`;
    } else if (windowData.complete) {
      label = `采集＋预测预警运行中 · 窗口${warningState}`;
    } else {
      label = `采集＋预测预警运行中 · 等待完整窗口`;
    }
  } else if (captureOnly) {
    label = acquisition.sample_count
      ? `采集已停止（仅保存） · ${acquisition.sample_count}点`
      : "真实采集已停止";
  } else if (windowData.complete) {
    label = `预测预警已停止 · 窗口${warningState}`;
  } else {
    label = "真实采集已停止";
  }
  node.textContent = label;
  dot.classList.toggle("active", Boolean(acquisition.running));
}

function applyPredictionModelProfile(profile, setSelections = true) {
  if (!profile) return;
  controls.predictionModel.value = profile.checkpoint || "";
  if (controls.predictionModelType && profile.model_type) {
    controls.predictionModelType.value = profile.model_type;
  }
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
  updateDatasetMeta();
  if (state.bootstrap && controls.autoIndicator?.checked) {
    configureAutomaticIndicator(true);
  }
}

function updateDatasetMeta() {
  if (!state.bootstrap) return;
  const node = $("datasetMeta");
  if (!node) return;
  if (controls.dataMode.value !== "live") {
    node.textContent =
      `历史数据源：${state.bootstrap.manifest.specimen_count} 个试样 · ` +
      `12个通道 · 24点预测窗口 · ${state.bootstrap.manifest.sampling_hz} Hz`;
    return;
  }
  const schema = state.bootstrap.acquisition.schemas.find(
    (item) => item.id === (controls.datasetSchema.value || "legacy_original")
  );
  const sensorCount = schema?.sensors?.length || 0;
  const schemaLabel = schema?.label || controls.datasetSchema.value;
  const captureOnly = controls.processingMode.value === "capture_only";
  const algorithm = controls.predictionModelType?.selectedOptions?.[0]?.textContent
    || controls.predictionModelType?.value
    || "I-ModernTCN";
  node.textContent = captureOnly
    ? `${schemaLabel} · ${sensorCount}个传感器 · 仅采集保存`
    : `${schemaLabel} · ${sensorCount}个传感器 · ${algorithm} · 实时预测与预警`;
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
    const schemaMode = controls.datasetSchema.value || "legacy_original";
    const modelType = controls.predictionModelType?.value || "i_T_G";
    const profile = await postJson("/api/prediction-model/inspect", {
      path: controls.predictionModel.value.trim(),
      model_type: modelType,
      schema_mode: schemaMode,
    });
    applyPredictionModelProfile(profile, setSelections);
    rememberModelPath(schemaMode, modelType, profile.checkpoint || "");
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
    const schemaMode = controls.datasetSchema.value || "legacy_original";
    const modelType = controls.predictionModelType?.value || "i_T_G";
    const result = await postJson("/api/prediction-model/select-file", {
      initial_path: controls.predictionModel.value.trim(),
      model_type: modelType,
      schema_mode: schemaMode,
    });
    if (result.selected) {
      applyPredictionModelProfile(result.model, true);
      rememberModelPath(schemaMode, modelType, result.path || result.model?.checkpoint || "");
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
  if (state.accessRole === "guest") {
    await confirmLocalSave();
    return;
  }
  if (state.accessRole !== "local_admin" && !usesLocalCaptureHelper()) {
    toast("网页本地保存请先填写目录名称，再点击“确认并授权本地保存”");
    return;
  }
  try {
    const initialPath = controls.saveRoot.value.trim();
    const result = usesLocalCaptureHelper()
      ? await requestLocalHelper("select_folder", {initial_path: initialPath})
      : await postJson("/api/acquisition/select-folder", {initial_path: initialPath});
    if (result.selected) {
      controls.saveRoot.value = result.path;
      await refreshSaveRootStatus(result.path);
      toast(`保存位置已选择：${result.path}`);
    }
  } catch (error) {
    toast(`无法打开文件夹选择器：${error.message}`);
  }
}

function updateLocalSaveStatus(message, error = false) {
  const node = $("localSaveStatus");
  if (!node) return;
  node.textContent = message;
  node.classList.toggle("error", Boolean(error));
  node.classList.toggle("ok", !error && state.localSaveAuthorized);
}

function renderSaveRootStatus(status) {
  const node = controls.saveRootStatus;
  if (!node) return;
  let payload = status || {};
  // Guest acquisition always has a writable server session directory, but
  // that is not the visitor's local folder.  Never let that server status
  // make an empty/un-authorized local path appear green.
  if (state.accessRole === "guest") {
    const localPath = controls.saveRoot?.value.trim() || "";
    payload = state.localSaveAuthorized && state.localSaveDirectoryHandle
      ? {
        ok: true,
        message: `已授权本地目录“${state.localSaveDirectoryHandle.name || localPath}”，采集完成后可保存到本机`,
      }
      : {
        ok: false,
        message: localPath
          ? "已填写保存位置，但尚未授权本机文件夹；当前采集不保存到本机"
          : "保存位置为空，当前采集不保存数据",
      };
  }
  node.textContent = payload.message || "保存位置为空，当前采集不保存数据";
  node.classList.toggle("error", !payload.ok);
  node.classList.toggle("ok", Boolean(payload.ok));
}

async function refreshSaveRootStatus(path = controls.saveRoot?.value.trim() || "") {
  if (!path) {
    renderSaveRootStatus({ok: false, message: "保存位置为空，当前采集不保存数据"});
    return;
  }
  if (state.localSaveAuthorized && state.localSaveDirectoryHandle) {
    renderSaveRootStatus({
      ok: true,
      message: `已授权本地目录“${state.localSaveDirectoryHandle.name || path}”，采集完成后可保存到本机`,
    });
    return;
  }
  if (state.accessRole === "guest") {
    renderSaveRootStatus({ok: false, message: "已填写保存位置，但尚未授权本机文件夹；当前采集不保存到本机"});
    return;
  }
  try {
    if (usesLocalCaptureHelper()) {
      const result = await requestLocalHelper("check_save_root", {path});
      renderSaveRootStatus(result);
      return;
    }
    const response = await fetch(`/api/acquisition/save-status?path=${encodeURIComponent(path)}`, {
      cache: "no-store", credentials: "same-origin",
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "保存目录检查失败");
    renderSaveRootStatus(result);
  } catch (error) {
    renderSaveRootStatus({ok: false, message: `保存目录检查失败，当前采集不保存数据：${error.message}`});
  }
}

async function confirmLocalSave() {
  const requestedName = controls.saveRoot?.value.trim() || "";
  if (typeof window.showDirectoryPicker !== "function" || !window.isSecureContext) {
    updateLocalSaveStatus("当前浏览器不支持本地目录授权；请使用 HTTPS 或 localhost/127.0.0.1 访问。", true);
    toast("当前地址不支持浏览器本地目录授权");
    return;
  }
  try {
    const handle = await window.showDirectoryPicker({mode: "readwrite"});
    state.localSaveDirectoryHandle = handle;
    state.localSaveAuthorized = true;
    state.localSaveNameDirty = false;
    controls.saveRoot.value = handle.name || requestedName;
    updateLocalSaveStatus(`已授权本地目录“${handle.name || requestedName}”；停止并保存后按原软件规则写入。`);
    renderSaveRootStatus({
      ok: true,
      message: `已授权本地目录“${handle.name || requestedName}”，采集完成后可保存到本机`,
    });
    toast("本地保存目录已授权");
  } catch (error) {
    if (error?.name === "AbortError") return;
    state.localSaveDirectoryHandle = null;
    state.localSaveAuthorized = false;
    state.localSaveNameDirty = false;
    updateLocalSaveStatus(`本地目录授权失败：${error.message || error}`, true);
    toast("未完成本地目录授权");
  }
}

async function saveFinishedCaptureLocally() {
  // In real helper-backed mode the edge gateway writes the capture directly
  // on the visitor PC.  A second server export would duplicate files and may
  // accidentally export another session's data.
  if (usesLocalCaptureHelper() && state.acquisitionMode !== "simulation") return;
  if (!state.localSaveAuthorized || !state.localSaveDirectoryHandle || state.localSaveBusy) return;
  state.localSaveBusy = true;
  try {
    const base = state.accessRole === "guest" ? "/api/simulation" : "/api/acquisition";
    const manifestResponse = await fetch(`${base}/export-manifest`, {cache: "no-store", credentials: "same-origin"});
    const manifest = await manifestResponse.json();
    if (!manifestResponse.ok) throw new Error(manifest.error || "读取采集文件清单失败");
    if (!manifest.ready || !Array.isArray(manifest.files) || !manifest.files.length) {
      updateLocalSaveStatus("本次没有生成有效采集文件，因此未保存到本地。", true);
      return;
    }
    for (const item of manifest.files) {
      const parts = String(item.path || "").split("/").filter(Boolean);
      if (parts.length < 2) continue;
      let directory = state.localSaveDirectoryHandle;
      for (const part of parts.slice(0, -1)) directory = await directory.getDirectoryHandle(part, {create: true});
      const fileHandle = await directory.getFileHandle(parts.at(-1), {create: true});
      const writable = await fileHandle.createWritable();
      try {
        const response = await fetch(`${base}/export-file?path=${encodeURIComponent(item.path)}`, {cache: "no-store", credentials: "same-origin"});
        if (!response.ok) throw new Error(`读取 ${item.path} 失败`);
        await writable.write(await response.arrayBuffer());
      } finally {
        await writable.close();
      }
    }
    updateLocalSaveStatus(`已保存 ${manifest.files.length} 个文件到“${state.localSaveDirectoryHandle.name}”，文件夹层级和命名与本地 EXE 一致。`);
    toast(`采集文件已保存到 ${state.localSaveDirectoryHandle.name}`);
  } catch (error) {
    if (error?.name === "NotAllowedError") {
      state.localSaveAuthorized = false;
      state.localSaveDirectoryHandle = null;
      state.localSaveNameDirty = false;
    }
    updateLocalSaveStatus(`本地保存失败：${error.message || error}`, true);
    toast("本地保存失败，请重新授权目录");
  } finally {
    state.localSaveBusy = false;
  }
}

async function testMysqlConnection(local = false) {
  const scope = local ? "local" : "target";
  const label = local ? "本机" : "目标电脑";
  const status = local ? $("mysqlLocalStatus") : $("mysqlTargetStatus");
  try {
    status.textContent = `正在检查${label} MySQL 数据库（不会创建或修改表）……`;
    const settings = local ? unifiedLocalMysqlSettings() : unifiedMysqlSettings();
    const result = local && usesLocalCaptureHelper()
      ? await requestLocalHelper("mysql_preflight", {
        mysql_enabled: true,
        mysql_host: settings.mysql_host,
        mysql_port: settings.mysql_port,
        mysql_user: settings.mysql_user,
        mysql_password: settings.mysql_password,
        mysql_database: settings.mysql_database,
        require_schema: true,
        write_test: false,
      }, {timeoutMs: 20000})
      : await postJson("/api/mysql/test", {...settings, read_only: true});
    state.mysqlConnectionTests[scope] = result;
    status.classList.toggle("ok", Boolean(result.ok));
    status.classList.toggle("error", !result.ok);
    const errorText = String(result.error || "未知错误");
    const detail = result.error_detail || {};
    const friendlyError = detail.message
      ? `[${detail.code || detail.category}] ${detail.message}`
      : (errorText.includes("1045") || errorText.toLowerCase().includes("access denied")
        ? "账号或密码错误：请确认 MySQL 用户名和密码设置正确"
        : errorText);
    status.textContent = result.ok
      ? `${label} MySQL 已连接：${settings.mysql_host}:${settings.mysql_port}/${result.database}（${result.driver}）${result.schema_ready === false ? "，但AFP表结构不完整" : ""}`
      : `${label} MySQL 连接失败：${friendlyError}`;
    return result;
  } catch (error) {
    const failed = {ok: false, error: error.message};
    state.mysqlConnectionTests[scope] = failed;
    status.classList.remove("ok");
    status.classList.add("error");
    status.textContent = `${label} MySQL 连接失败：${error.message}`;
    return failed;
  }
}

async function validateEnabledMysqlBeforeStart() {
  const failures = [];
  if (controls.mysqlEnabled?.checked) {
    const target = await testMysqlConnection(false);
    if (!target?.ok) failures.push(`目标电脑 MySQL：${target?.error || "连接失败"}`);
  }
  if (controls.mysqlLocalEnabled?.checked) {
    const local = await testMysqlConnection(true);
    if (!local?.ok) failures.push(`本机 MySQL：${local?.error || "连接失败"}`);
  }
  if (failures.length) {
    throw new Error(`MySQL 保存预检未通过；${failures.join("；")}`);
  }
}

async function refreshRelationMap(scope) {
  const local = scope === "local";
  const label = local ? "本机" : "目标电脑";
  const table = $(local ? "localRelationMapTable" : "targetRelationMapTable");
  const body = table?.querySelector("tbody");
  if (!body) return;
  const settings = local ? unifiedLocalMysqlSettings() : unifiedMysqlSettings();
  const database = settings.mysql_database;
  const status = local ? $("mysqlLocalStatus") : $("mysqlTargetStatus");
  try {
    if (status) status.textContent = `正在读取${label}数据库 ${settings.mysql_host}/${database} 的关系表……`;
    const result = local && usesLocalCaptureHelper()
      ? await requestLocalHelper("mysql_relation_map", {
        mysql_enabled: true,
        mysql_host: settings.mysql_host,
        mysql_port: settings.mysql_port,
        mysql_user: settings.mysql_user,
        mysql_password: settings.mysql_password,
        mysql_database: settings.mysql_database,
        limit: 1000,
      }, {timeoutMs: 20000})
      : await postJson("/api/mysql/relation-map", {...settings, limit: 1000});
    body.replaceChildren();
    if (!result.ok || !result.rows?.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 6;
      const detail = result.error_detail || {};
      const errorMessage = detail.message
        ? `[${detail.code || detail.category}] ${detail.message}`
        : result.error;
      cell.textContent = errorMessage
        ? `${label}数据库 ${database} 读取失败：${errorMessage}`
        : `${label}数据库 ${database} 已连接，但当前没有已保存的工况—试样—铺层关系`;
      row.appendChild(cell);
      body.appendChild(row);
      if (status && result.ok) {
        const initializedNote = result.auto_initialized
          ? " 已自动创建/补齐 AFP 数据库关系结构。"
          : "";
        status.textContent = `${label}数据库 ${settings.mysql_host}/${database} 已连接，关系表当前为 0 行。${initializedNote}`;
        status.classList.remove("error");
      }
      return;
    }
    result.rows.forEach((item) => {
      const row = document.createElement("tr");
      [
        item.condition_id,
        item.specimen_id,
        item.replicate_no,
        item.layer_no ?? "—",
        item.sample_count ?? "—",
        item.saved_at || "—",
      ].forEach((value) => {
        const cell = document.createElement("td");
        cell.textContent = String(value ?? "—");
        row.appendChild(cell);
      });
      body.appendChild(row);
    });
    if (status) {
      const initializedNote = result.auto_initialized
        ? "（已自动创建/补齐 AFP 数据库关系结构）"
        : "";
      status.textContent = `${label}数据库 ${settings.mysql_host}/${database} 已刷新：${result.count} 条铺层关系${initializedNote}`;
      status.classList.add("ok");
      status.classList.remove("error");
    }
  } catch (error) {
    body.replaceChildren();
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 6;
    cell.textContent = `${label}数据库 ${settings.mysql_host}/${database} 关系表读取失败：${error.message}`;
    row.appendChild(cell);
    body.appendChild(row);
  }
}

function renderRemoteMysqlPreview(columns, rows) {
  const table = controls.remoteMysqlPreview;
  if (!table) return;
  table.replaceChildren();
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  columns.forEach((name) => {
    const cell = document.createElement("th");
    cell.textContent = name;
    headRow.appendChild(cell);
  });
  head.appendChild(headRow);
  const body = document.createElement("tbody");
  rows.forEach((item) => {
    const row = document.createElement("tr");
    columns.forEach((name) => {
      const cell = document.createElement("td");
      cell.textContent = String(item?.[name] ?? "");
      row.appendChild(cell);
    });
    body.appendChild(row);
  });
  if (!rows.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = Math.max(1, columns.length);
    cell.textContent = "查询成功，当前没有匹配数据";
    row.appendChild(cell);
    body.appendChild(row);
  }
  table.append(head, body);
}

async function previewRemoteMysqlData() {
  const status = controls.remoteMysqlStatus;
  try {
    if (status) status.textContent = "正在从统一 MySQL 数据库读取预览……";
    const result = await postJson("/api/mysql/query", unifiedMysqlSettings({
      query: controls.remoteMysqlQuery?.value.trim() || "",
      limit: Number(controls.remoteMysqlLimit?.value) || 200,
    }));
    renderRemoteMysqlPreview(result.columns || [], result.rows || []);
    state.remoteMysqlPreview = result;
    if (status) status.textContent = `已读取 ${result.count || 0} 行${result.truncated ? "（结果已截断）" : ""}。`;
  } catch (error) {
    if (status) status.textContent = `读取失败：${error.message}`;
  }
}

async function copyRemoteMysqlPreview() {
  const result = state.remoteMysqlPreview;
  if (!result?.columns?.length) {
    toast("请先预览远程数据");
    return;
  }
  const escapeCell = (value) => {
    const text = String(value ?? "");
    return /[\t\r\n]/.test(text) ? JSON.stringify(text) : text;
  };
  const lines = [result.columns.join("\t")];
  (result.rows || []).forEach((row) => lines.push(result.columns.map((name) => escapeCell(row[name])).join("\t")));
  try {
    await navigator.clipboard.writeText(lines.join("\n"));
    toast("预览数据已复制，可直接粘贴到 Excel");
  } catch (error) {
    toast(`复制失败：${error.message}`);
  }
}

async function downloadRemoteMysqlCsv() {
  const status = controls.remoteMysqlStatus;
  try {
    if (status) status.textContent = "正在生成远程数据库 CSV……";
    const response = await fetch("/api/mysql/export-csv", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(unifiedMysqlSettings({
        query: controls.remoteMysqlQuery?.value.trim() || "",
        limit: 200000,
      })),
    });
    if (!response.ok) {
      let message = `请求失败（${response.status}）`;
      try { message = (await response.json()).error || message; } catch (_) {}
      throw new Error(message);
    }
    const blob = await response.blob();
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `afp_${controls.mysqlDatabase?.value.trim() || "database"}_export.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(link.href);
    if (status) status.textContent = "CSV 已生成并开始下载。";
  } catch (error) {
    if (status) status.textContent = `下载失败：${error.message}`;
  }
}

function renderMysqlStatus(mysql) {
  const status = $("mysqlStatus");
  if (!status || !mysql) return;
  const selectedInUi = Boolean(
    controls.mysqlEnabled?.checked || controls.mysqlLocalEnabled?.checked
  );
  if (!mysql.enabled && selectedInUi) {
    const tested = state.mysqlConnectionTests.target || state.mysqlConnectionTests.local;
    status.classList.toggle("ok", Boolean(tested?.ok));
    status.classList.toggle("error", Boolean(tested && tested.ok === false));
    if (tested?.ok) {
      status.textContent =
        `MySQL 已连接：${tested.database}（${tested.driver}）；采集完成后将批量写入。`;
    } else if (tested?.error) {
      status.textContent = `MySQL 连接失败：${tested.error}`;
    } else {
      status.textContent = "MySQL 保存已勾选；请点击“检查 MySQL”确认连接，采集完成后批量写入。";
    }
    return;
  }
  status.classList.toggle("ok", Boolean(mysql.ok));
  status.classList.toggle(
    "error",
    Boolean(mysql.enabled && mysql.ok === false && mysql.state !== "pending")
  );
  if (!mysql.enabled) {
    status.textContent = "MySQL 保存未启用；原始文件已完成本地保存。";
  } else if (mysql.state === "pending" || mysql.ok === null || mysql.ok === undefined) {
    status.textContent = "MySQL 已启用；等待本次采集完成后批量写入。";
  } else if (mysql.ok) {
    const retried = Number(mysql.pending_retry?.succeeded || 0);
    const destinationText = mysql.destination_count
      ? `${mysql.successful_destinations || 0}/${mysql.destination_count} 个数据库目标`
      : (mysql.database || "MySQL");
    status.textContent = `第${mysql.layer || ""}层已写入 ${destinationText}，共 ${mysql.saved_rows || 0} 行` +
      `${retried > 0 ? `；同时自动补传 ${retried} 条历史待同步记录` : ""}`;
  } else if (mysql.error) {
    const errorText = String(mysql.error);
    const friendlyError = errorText.includes("1045") || errorText.toLowerCase().includes("access denied")
      ? "账号或密码错误，请确认 MySQL 用户名和密码设置正确"
      : errorText;
    status.textContent = `MySQL 写入失败，已保留本地文件并生成待补写记录：${friendlyError}`;
  }
}

function agentEscapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function buildAgentEvents(hardwareResult) {
  const interfaceLabels = {
    thermocouple_8ch: "SMRF 八通道热电偶",
    plc_process: "松下 PLC",
    uvc_temperature: "BSV UVC 热像仪",
    abb_motion: "ABB 机器人",
    m3232_pressure: "M3232 薄膜压力",
  };
  const badInterfaces = (hardwareResult?.interfaces || [])
    .filter((item) => item && item.enabled !== false && !item.ok);
  const badSensors = (hardwareResult?.sensors || [])
    .filter((item) => item && item.selected && !item.ok);
  const sensorByName = new Map(badSensors.map((item) => [String(item.name), item]));
  const events = [];
  const coveredSensors = new Set();

  const makeEvent = (interfaceItem = {}, sensorName = "接口", channelNames = null) => {
    const sensor = sensorByName.get(sensorName) || {};
    const interfaceId = String(interfaceItem.id || "sensor_channel");
    const expected = (interfaceItem.expected_channels || []).map(String);
    const channels = (channelNames || (sensorName === "接口" ? expected : [sensorName]))
      .map(String).filter(Boolean);
    const sensorStates = {};
    channels.forEach((name) => {
      const channel = sensorByName.get(name);
      if (!channel) return;
      sensorStates[name] = {
        state: channel.state,
        message: channel.message,
        received_samples: channel.received_samples,
        invalid_samples: channel.invalid_samples,
        last_sample_age_seconds: channel.last_sample_age_seconds,
      };
    });
    return {
      interface_id: interfaceId,
      interface_label: String(interfaceItem.label || interfaceLabels[interfaceId] || "传感器接口"),
      role: String(interfaceItem.role || "custom"),
      driver: String(interfaceItem.driver || ""),
      endpoint: String(interfaceItem.endpoint || "未填写地址"),
      physical_interface_id: String(interfaceItem.physical_interface_id || ""),
      physical_interface_kind: String(interfaceItem.physical_interface_kind || ""),
      protocol: String(interfaceItem.protocol || interfaceItem.driver || ""),
      physical_fallback: Boolean(interfaceItem.physical_fallback),
      physical_warning: String(interfaceItem.physical_warning || ""),
      sensor_name: sensorName,
      channels,
      state: String(interfaceItem.state || sensor.state || "no_data"),
      message: String(interfaceItem.message || sensor.message || "接口或通道未返回有效数据"),
      evidence: {
        expected_channels: expected,
        detected_channels: (interfaceItem.detected_channels || []).map(String),
        missing_channels: (interfaceItem.missing_channels || []).map(String),
        invalid_channels: (interfaceItem.invalid_channels || []).map(String),
        sample_counts: interfaceItem.sample_counts || {},
        invalid_sample_counts: interfaceItem.invalid_sample_counts || {},
        received_samples: sensor.received_samples,
        invalid_samples: sensor.invalid_samples,
        last_sample_age_seconds: sensor.last_sample_age_seconds,
        sensor_states: sensorStates,
      },
      simulated: Boolean(hardwareResult?.simulated),
    };
  };

  badInterfaces.forEach((interfaceItem) => {
    const abnormalChannels = [...new Set([
      ...(interfaceItem.invalid_channels || []),
      ...(interfaceItem.missing_channels || []),
    ].map(String).filter(Boolean))];
    const channels = abnormalChannels.length
      ? abnormalChannels
      : (interfaceItem.expected_channels || []).map(String).filter((name) => sensorByName.has(name));
    if (!channels.length) {
      events.push(makeEvent(interfaceItem));
      return;
    }
    events.push(makeEvent(interfaceItem, channels[0] || "接口", channels));
    channels.forEach((name) => coveredSensors.add(name));
  });
  badSensors.forEach((sensor) => {
    const name = String(sensor.name || "传感器通道");
    if (!coveredSensors.has(name)) events.push(makeEvent({}, name));
  });

  const unique = new Map();
  events.forEach((event) => unique.set(`${event.interface_id}\u0000${event.sensor_name}`, event));
  return [...unique.values()];
}

function renderAgentGate() {
  const explicitKey = agentApiKeyInput?.value.trim() || "";
  const explicitModel = agentModelNameInput?.value.trim() || "";
  const keyPresent = state.accessRole !== "guest" && (Boolean(explicitKey) || state.modelAccess);
  const modelPresent = state.accessRole !== "guest" && (Boolean(explicitModel) || state.modelAccess);
  const eventPresent = state.agentEvents.length > 0;
  const ready = eventPresent && !state.agentBusy;
  const status = $("agentGateStatus");
  if (status) {
    const configured = keyPresent && modelPresent;
    status.className = `agent-gate-status ${ready ? "ready" : ""}`;
    status.textContent = state.agentBusy
      ? "诊断中"
      : !eventPresent ? "等待异常"
        : configured ? "模型工具诊断" : (keyPresent || modelPresent) ? "离线诊断（未调用模型）" : "离线测试诊断";
  }
  if (agentDiagnoseButton) agentDiagnoseButton.disabled = !ready;
  return ready;
}

function appendAgentDiagnostics(node) {
  if (!node || (!state.agentBusy && !state.agentResult)) return;
  const container = document.createElement("div");
  container.className = "hardware-agent-results";
  const result = state.agentResult;
  const heading = document.createElement("div");
  heading.className = "hardware-agent-header";
  if (state.agentBusy) {
    heading.textContent = `LangChain 正在诊断 ${state.agentEvents.length} 项异常…`;
    container.appendChild(heading);
    node.appendChild(container);
    return;
  }
  const statusText = result.execution_mode === "siliconflow_agent"
    ? "硅基流动模型已自主选用诊断工具"
    : result.execution_mode === "siliconflow_structured"
      ? "硅基流动模型已基于本地证据完成结构化诊断（兼容模式）"
    : result.model_status === "failed_offline_fallback"
      ? "模型工具调用失败，已完成离线测试诊断"
      : result.execution_mode === "offline_test"
        ? "内置离线测试诊断器已按现象选用工具"
        : "本地规则兜底诊断";
  heading.textContent = `LangChain：${statusText}（${(result.diagnoses || []).length} 项）`;
  container.appendChild(heading);
  if (result.model_message) {
    const message = document.createElement("div");
    message.className = "hardware-agent-message";
    message.textContent = result.model_message;
    container.appendChild(message);
  }
  (result.diagnoses || []).forEach((diagnosis, index) => {
    const item = document.createElement("div");
    item.className = "hardware-agent-item";
    const facts = (diagnosis.observed_facts || []).map((value) =>
      `<li>${agentEscapeHtml(value?.text || value)}</li>`).join("");
    const hypotheses = (diagnosis.hypotheses || []).map((value) => {
      const confidence = Number(value?.confidence);
      const suffix = Number.isFinite(confidence) ? `（可信度 ${Math.round(confidence * 100)}%）` : "";
      return `<li>${agentEscapeHtml(value?.cause || value)}${agentEscapeHtml(suffix)}</li>`;
    }).join("");
    const legacyCauses = (diagnosis.possible_causes || []).map((value) => `<li>${agentEscapeHtml(value)}</li>`).join("");
    const actions = (diagnosis.recommended_actions || []).map((value, actionIndex) => {
      if (value && typeof value === "object") {
        const action = value.action || "待确认操作";
        const reason = value.reason ? `——${value.reason}` : "";
        return `<li>${agentEscapeHtml(value.priority || actionIndex + 1)}. ${agentEscapeHtml(action)}${agentEscapeHtml(reason)}</li>`;
      }
      return `<li>${agentEscapeHtml(value)}</li>`;
    }).join("");
    const crossFindings = (diagnosis.cross_interface_findings || []).map((value) => `<li>${agentEscapeHtml(value)}</li>`).join("");
    const unknowns = (diagnosis.unknowns || []).map((value) => `<li>${agentEscapeHtml(value)}</li>`).join("");
    const sources = (diagnosis.evidence_sources || []).map((value) => agentEscapeHtml(value)).join("、") || "本次接口检查";
    item.innerHTML = `
      <div class="hardware-agent-title"><strong>${index + 1}. ${agentEscapeHtml(diagnosis.interface_label)} / ${agentEscapeHtml(diagnosis.sensor_name)}</strong><span>${agentEscapeHtml(diagnosis.fault_type)}</span></div>
      <div><b>原始报错：</b>${agentEscapeHtml(diagnosis.original_error || diagnosis.error_message || diagnosis.summary)}</div>
      <div><b>接口/通道：</b>${agentEscapeHtml((diagnosis.channels || []).join("、") || "接口级异常")}</div>
      <div class="hardware-agent-grid">
        <div><b>已观察事实</b><ul>${facts || "<li>没有取得更多可验证事实</li>"}</ul></div>
        <div><b>原因判断</b><ul>${hypotheses || legacyCauses || "<li>证据不足，暂不判断具体原因</li>"}</ul></div>
        ${crossFindings ? `<div><b>跨接口判断</b><ul>${crossFindings}</ul></div>` : ""}
        <div><b>建议操作</b><ul>${actions || "<li>保持当前配置并取得更多诊断证据</li>"}</ul></div>
        <div><b>仍待确认</b><ul>${unknowns || "<li>需要连接真实设备后复核</li>"}</ul></div>
      </div>
      <div class="hardware-agent-evidence"><b>证据来源：</b>${sources}</div>
      <small>${agentEscapeHtml(diagnosis.evidence_boundary)}</small>`;
    container.appendChild(item);
  });
  node.appendChild(container);
}

function handleAgentInputChange() {
  renderAgentGate();
  const autoStatus = $("agentAutoStatus");
  const keyPresent = state.accessRole !== "guest"
    && (Boolean(agentApiKeyInput?.value.trim()) || state.modelAccess);
  const modelPresent = state.accessRole !== "guest"
    && Boolean(agentModelNameInput?.value.trim() || state.modelAccess);
  if (autoStatus && state.agentEvents.length) {
    autoStatus.textContent = keyPresent && modelPresent
      ? "已授权；服务器模型会按异常现象自主选择只读取证工具。"
      : "当前使用内置本地规则诊断，不调用外部服务。";
  }
}

async function loadAgentDefaults() {
  try {
    const response = await fetch("/api/agent/defaults", {cache: "no-store"});
    const defaults = await response.json();
    if (!response.ok) return;
    state.agentDefaultKeyAvailable = Boolean(defaults.default_key_available);
    const configuredModel = defaults.model_name || "deepseek-ai/DeepSeek-V3";
    state.modelAccess = Boolean(defaults.model_access);
    state.agentModelName = configuredModel;
    if (agentModelNameInput) agentModelNameInput.value = configuredModel;
    handleAgentInputChange();
  } catch (_error) {
    // The UI remains usable with explicit fields and local-rule fallback.
  }
}

function updateAgentFromHardwareResult(result, {automatic = false} = {}) {
  const events = buildAgentEvents(result);
  const fingerprint = events.length ? JSON.stringify(events) : "";
  const changed = fingerprint !== state.agentFingerprint;
  state.agentEvents = events;
  if (changed) {
    state.agentRequestId += 1;
    state.agentController?.abort();
    state.agentController = null;
    state.agentBusy = false;
    state.agentJobId = "";
    state.agentFingerprint = fingerprint;
    state.agentResult = null;
    const autoStatus = $("agentAutoStatus");
    if (autoStatus) autoStatus.textContent = events.length
      ? `发现 ${events.length} 项异常，正在自动诊断全部异常。`
      : "等待接口异常；未配置模型时使用内置离线测试诊断。";
  }
  renderAgentGate();
  if (events.length && changed && !state.agentResult) runAgentDiagnosis({automatic: true});
}

async function pollAgentDiagnosisJob(jobId, requestId, {timeoutMs = 240000, intervalMs = 1200} = {}) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (requestId !== state.agentRequestId) return null;
    const response = await fetch(`/api/agent/diagnose/result?job_id=${encodeURIComponent(jobId)}`, {
      cache: "no-store",
      credentials: "same-origin",
    });
    let snapshot = null;
    try { snapshot = await response.json(); } catch (_error) { snapshot = {}; }
    if (!response.ok) throw new Error(snapshot.error || `诊断任务查询失败（${response.status}）`);
    if (snapshot.state === "success") return snapshot.result || {};
    if (snapshot.state === "failed") throw new Error(snapshot.error || "诊断任务执行失败");
    await new Promise((resolve) => window.setTimeout(resolve, intervalMs));
  }
  throw new Error("诊断任务仍在执行，请稍后查看结果");
}

async function runAgentDiagnosis({automatic = false} = {}) {
  if (!state.agentEvents.length || !renderAgentGate()) return null;
  const requestId = ++state.agentRequestId;
  state.agentBusy = true;
  state.agentJobId = "";
  renderAgentGate();
  if (state.hardwareCheck) renderHardwareCheckResult(state.hardwareCheck, {automatic});
  const autoStatus = $("agentAutoStatus");
  if (autoStatus) autoStatus.textContent = automatic ? "新异常已提交 LangChain 诊断任务……" : "正在提交诊断任务……";
  try {
    if (state.accessRole === "guest") {
      if (autoStatus) autoStatus.textContent = "正在执行本地规则诊断……";
      const result = await postJson("/api/agent/diagnose", {
        api_key: "",
        model_name: "",
        events: state.agentEvents,
        hardware_result: state.hardwareCheck,
      }, {timeoutMs: 30000});
      if (requestId !== state.agentRequestId) return null;
      state.agentResult = result;
      if (autoStatus) autoStatus.textContent = result.model_message || "本地诊断已完成。";
      return result;
    }
    const started = await postJson("/api/agent/diagnose/start", {
      api_key: state.accessRole === "guest" ? "" : (agentApiKeyInput?.value.trim() || ""),
      model_name: state.accessRole === "guest" ? "" : (agentModelNameInput?.value.trim() || ""),
      events: state.agentEvents,
      hardware_result: state.hardwareCheck,
    }, {timeoutMs: 30000});
    if (requestId !== state.agentRequestId) return null;
    state.agentJobId = started.job_id || "";
    if (!state.agentJobId) throw new Error("服务器未返回诊断任务编号");
    if (autoStatus) autoStatus.textContent = "诊断任务已提交，正在等待模型结果……";
    const result = await pollAgentDiagnosisJob(state.agentJobId, requestId);
    if (requestId !== state.agentRequestId || !result) return null;
    state.agentResult = result;
    if (autoStatus) autoStatus.textContent = result.model_message || "LangChain 诊断已完成。";
    return result;
  } catch (error) {
    if (requestId === state.agentRequestId && autoStatus) autoStatus.textContent = `诊断未完成：${error.message}`;
    return null;
  } finally {
    if (requestId === state.agentRequestId) {
      state.agentBusy = false;
      state.agentJobId = "";
      renderAgentGate();
      if (state.hardwareCheck) renderHardwareCheckResult(state.hardwareCheck, {automatic});
    }
  }
}

async function testSensorConnection({automatic = false} = {}) {
  try {
    const result = await postJson("/api/acquisition/test", acquisitionConfig(), {timeoutMs: 20000});
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
    /*
    const interfaceSummary = (result.interfaces || []).map((item) => {
      const channels = (item.detected_channels || []).join("、") || "未识别通道";
      return `${item.id || item.endpoint}: ${item.ok ? "正常" : "未收到数据"}（${channels}）`;
    }).join("；");
    if (interfaceSummary) node.textContent += ` 接口：${interfaceSummary}`;
    */
    const interfaceSummaryText = (result.interfaces || []).map((item) => {
      const names = Array.isArray(item.detected_channels) ? item.detected_channels.join(", ") : "";
      return String(item.id || item.endpoint || "interface") + ": " + (item.ok ? "ok" : "no data") + " (" + (names || "no channels") + ")";
    }).join("; ");
    if (interfaceSummaryText) node.textContent += " interfaces: " + interfaceSummaryText;
    updateAgentFromHardwareResult(result, {automatic});
  } catch (error) {
    const status = $("acquisitionStatus");
    if (status) {
      status.classList.add("error");
      status.classList.remove("ok");
      status.textContent = `采集启动失败：${error.message}`;
    }
    toast(error.message);
  }
}

async function startAcquisition() {
  try {
    if (controls.autoProcessParameters?.checked) {
      await readProcessParameters({automatic: true});
    }
    if (state.accessRole === "guest") {
      // Load the selected/default dataset once, then replay it locally in the
      // browser.  The server still owns the session/save lifecycle; only the
      // high-frequency display path leaves the public request loop.
      await loadSimulationDatasetOnce();
      const guestResult = await postJson("/api/simulation/start", acquisitionConfig());
      state.guestSimulationStarted = true;
      state.guestSimulationStoppedByUser = false;
      renderAcquisitionStatus(guestResult);
      controls.dataMode.value = "live";
      configureDataMode();
      if (!state.payload?.channels?.length) {
        await loadSimulationTemplate();
      }
      startLocalSimulationReplay();
      return;
    }
    await validateEnabledMysqlBeforeStart();
    await acquireRealControl();
    if (state.hardwareCheckInProgress) {
      throw new Error("接口与传感器通道检查正在进行，请等待检查完成");
    }
    if (controls.acquisitionMode?.value !== "simulation") {
      const fingerprint = hardwareConfigFingerprint();
      if (!state.hardwareCheck || state.hardwareCheckFingerprint !== fingerprint || !state.hardwareCheck.ok) {
        const check = await testSensorConnection({automatic: false});
        if (!check?.ok) {
          throw new Error("接口或传感器通道检查未通过，已阻止开始采集；请按上方异常明细处理后重检");
        }
      }
    }
    const nextScope = liveEvidenceScopeKey();
    if (state.liveScopeKey !== null && nextScope !== state.liveScopeKey) {
      // A changed specimen/condition is a new physical evidence stream.  Do
      // not carry the previous layer number into the new acquisition.
      resetLiveEvidenceDisplay();
    }
    state.liveScopeKey = nextScope;
    const helperBackedReal = usesLocalCaptureHelper() && controls.acquisitionMode?.value !== "simulation";
    const result = helperBackedReal
      ? await requestLocalHelper("start_capture", acquisitionConfig(), {timeoutMs: 30000})
      : await postJson("/api/acquisition/start", acquisitionConfig());
    if (controls.processingMode.value !== "capture_only") {
      applyPredictionModelProfile(result.prediction_model, false);
    }
    const activeStatus = helperBackedReal
      ? await waitForEdgeFirstSample(result.capture_uuid)
      : result;
    renderAcquisitionStatus(activeStatus);
    controls.dataMode.value = "live";
    configureDataMode();
    await loadRealtime();
  } catch (error) {
    const status = $("acquisitionStatus");
    if (status) {
      status.classList.add("error");
      status.classList.remove("ok");
      status.textContent = `采集启动失败：${error.message}`;
    }
    toast(error.message);
  }
}

async function stopAcquisition() {
  try {
    stopLocalSimulationReplay();
    const layerControl = controls.datasetSchema.value === "new_collection_v11_3"
      ? controls.newLayer
      : controls.liveLayer;
    const completedLayer = Number(layerControl.value) || 0;
    const result = usesLocalCaptureHelper() && controls.acquisitionMode?.value !== "simulation"
      ? await requestLocalHelper("stop_capture", {}, {timeoutMs: 30000})
      : await postJson(
        state.accessRole === "guest" ? "/api/simulation/stop" : "/api/acquisition/stop",
        {},
      );
    if (state.accessRole === "guest") {
      state.guestSimulationStarted = false;
      state.guestSimulationStoppedByUser = true;
    }
    renderAcquisitionStatus(result);
    renderMysqlStatus(result.mysql);
    if (
      Array.isArray(result.completed_layers) &&
      result.completed_layers.includes(completedLayer + 1)
    ) {
      layerControl.value = String(completedLayer + 1);
      toast(`第${completedLayer + 1}层已完成并保存；下次开始采集将进入第${completedLayer + 2}层`);
    } else if (Array.isArray(result.completed_layers) && result.completed_layers.length) {
      toast(`已完成第${completedLayer + 1}层并保存；当前试样已采集${result.completed_layers.length}层`);
    }
    await saveFinishedCaptureLocally();
    await loadRealtime();
  } catch (error) {
    toast(error.message);
  }
}

function buildSensorChecklist(sensorNames) {
  const sensorHeader = document.createElement("div");
  sensorHeader.className = "sensor-checklist-header";
  sensorHeader.innerHTML = "<span>通道</span><span>采集</span><span>输入</span><span>输出</span><span>接口</span>";
  const sensorRows = sensorNames.map((name) => {
    const row = document.createElement("div");
    row.className = "sensor-checklist-row";
    const channelName = document.createElement("span");
    channelName.className = "sensor-checklist-name";
    channelName.textContent = name;
    channelName.title = name;

    const routeLabel = document.createElement("label");
    const routeSelect = document.createElement("select");
    routeSelect.className = "interface-route-select";
    routeSelect.dataset.channel = name;
    routeLabel.append(routeSelect);

    const collectLabel = document.createElement("label");
    const collectInput = document.createElement("input");
    collectInput.type = "checkbox";
    collectInput.title = "采集并保存该通道";
    collectInput.className = "save-sensor-checkbox";
    collectInput.value = name;
    collectInput.checked = true;
    collectLabel.append(collectInput, document.createTextNode("采"));

    const modelLabel = document.createElement("label");
    const modelInput = document.createElement("input");
    modelInput.type = "checkbox";
    modelInput.title = "将该通道作为模型输入";
    modelInput.className = "model-input-sensor-checkbox";
    modelInput.value = name;
    modelInput.checked = true;
    modelLabel.append(modelInput, document.createTextNode("入"));

    const outputLabel = document.createElement("label");
    const outputInput = document.createElement("input");
    outputInput.type = "checkbox";
    outputInput.title = "显示预测结果并用于健康指标";
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
      // Rebuild the interface-side routing before refreshing live data.  The
      // checklist change event bubbles afterwards, but loadRealtime() starts
      // its request immediately; doing it here prevents one request from
      // carrying the just-unchecked channel.
      refreshInterfaceCardsForSelection();
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
    row.append(channelName, collectLabel, modelLabel, outputLabel, routeLabel);
    return row;
  });
  $("liveSensorChecklist").replaceChildren(sensorHeader, ...sensorRows);
  rebuildInterfaceEditors();
  refreshChannelInterfaceOptions();
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
  controls.optimizedWarning.disabled = captureOnly;
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
  updateDatasetMeta();
  if (controls.dataMode.value === "live") loadRealtime();
}

function configureDatasetSchema(useDefaults = true) {
  const schema = state.bootstrap.acquisition.schemas.find(
    (item) => item.id === controls.datasetSchema.value
  );
  if (!schema) return;
  const isNew = schema.id === "new_collection_v11_3";
  populatePredictionModelTypes();
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
  document.querySelector(".save-rule-note").textContent =
    "文件夹按工况与独立重复命名；每层保留分层文件，完整试样始终覆盖为同一份当前数据文件。";
  buildSensorChecklist(schema.sensors);
  if (controls.acquisitionMode?.value === "simulation" && state.simulationSourceChannels.length) {
    autoEnableSimulationChannels(state.simulationSourceChannels);
  }
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
    const rememberedPath = lastModelPath(
      schema.id,
      controls.predictionModelType?.value || "i_T_G"
    );
    if (rememberedPath) {
      controls.predictionModel.value = rememberedPath;
      inspectPredictionModel(true).catch(() => {});
    }
  }
  // Rebuild indicator/model selections after the channel checklist has been
  // replaced.  This prevents a new-collection indicator or output checkbox
  // from surviving a switch back to the legacy 12-channel schema.
  const compatibleDefault = schemaIndicators.find((item) => item.id === "TC-HI")
    || schemaIndicators[0];
  if (compatibleDefault) controls.indicator.value = compatibleDefault.id;
  populateModels(true);
  if (controls.bestPredictionOverride.checked) {
    configureBestPredictionOverride();
  }
  controls.optimizedWarning.disabled =
    controls.processingMode.value === "capture_only";
  const optimizedLabel = $("optimizedWarningLabel");
  if (optimizedLabel) {
    optimizedLabel.textContent = isNew
      ? "使用验证集校准的 CAP 在线优化（16传感器方案）"
      : "使用优化预警（历史数据 v13.8 / 实时因果 v13.9）";
  }
  configureProcessingMode();
  configureAutomaticIndicator(true);
  updateDatasetMeta();
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
  controls.optimizedWarning.disabled =
    controls.processingMode.value === "capture_only";
  if (live) {
    stopPlayback();
    controls.realtimePrediction.checked = true;
    window.clearInterval(state.livePollTimer);
    if (usePublicLiveWebSocket()) {
      openLiveWebSocket();
    } else {
      state.livePollTimer = window.setInterval(() => {
        if (!state.busy) loadRealtime();
      }, livePollIntervalMs());
    }
  } else {
    stopLocalSimulationReplay();
    window.clearInterval(state.livePollTimer);
    state.livePollTimer = null;
    closeLiveWebSocket();
  }
  updateRealAcquisitionVisibility();
  configureAutomaticIndicator(true);
  updateDatasetMeta();
  if (!usePublicLiveWebSocket()) loadRealtime();
}

function livePollIntervalMs() {
  // A public HTTPS tunnel adds a variable round trip.  Polling faster than
  // that only queues stale requests and makes the chart appear to trickle in.
  // Keep LAN/local refresh responsive while pacing public refreshes.
  const host = String(window.location.hostname || "").toLowerCase();
  const isPrivateHost = host === "localhost"
    || host === "127.0.0.1"
    || host === "::1"
    || /^10\./.test(host)
    || /^192\.168\./.test(host)
    || /^172\.(1[6-9]|2\d|3[0-1])\./.test(host);
  return window.location.protocol === "https:" && !isPrivateHost ? 250 : 100;
}

function usePublicLiveWebSocket() {
  const host = String(window.location.hostname || "").toLowerCase();
  const privateHost = host === "localhost"
    || host === "127.0.0.1"
    || host === "::1"
    || /^10\./.test(host)
    || /^192\.168\./.test(host)
    || /^172\.(1[6-9]|2\d|3[0-1])\./.test(host);
  return typeof window.WebSocket === "function"
    && window.location.protocol === "https:"
    && !privateHost;
}

function closeLiveWebSocket() {
  window.clearTimeout(state.liveSocketReconnectTimer);
  state.liveSocketReconnectTimer = null;
  state.liveSocketQuery = "";
  state.liveSocketReconnectAttempt = 0;
  const socket = state.liveSocket;
  state.liveSocket = null;
  if (socket) {
    socket.onclose = null;
    socket.close();
  }
}

function applyRealtimePayload(payload) {
  if (!payload || payload.type === "error") {
    throw new Error(payload?.error || "实时数据服务异常");
  }
  state.payload = payload;
  if (controls.dataMode.value === "replay") {
    controls.cursor.max = payload.progress.total_points;
    controls.cursor.value = payload.progress.cursor;
  }
  render(payload);
  $("connectionStatus").textContent = "实时数据服务已连接";
  document.querySelector(".status-dot").classList.add("connected");
  if (payload.acquisition) renderAcquisitionStatus(payload.acquisition);
  renderRuntimeStatus(payload);
  if (payload.progress.finished && state.playing) {
    if (controls.loop.checked) {
      controls.cursor.value = 1;
    } else {
      stopPlayback();
    }
  }
}

const SIMULATION_PLAYBACK_CACHE_ROWS = 20;

function isGuestSimulationMode() {
  return state.accessRole === "guest"
    && controls.acquisitionMode?.value === "simulation";
}

async function loadSimulationDatasetOnce({force = false} = {}) {
  if (!isGuestSimulationMode()) return null;
  if (!force && state.simulationDatasetCache) return state.simulationDatasetCache;
  if (!force && state.simulationDatasetPromise) return state.simulationDatasetPromise;
  state.simulationDatasetPromise = (async () => {
    const response = await fetch("/api/simulation/dataset", {
      cache: "no-store",
      credentials: "same-origin",
    });
    const payload = await response.json();
    if (!response.ok || !payload?.ok) {
      throw new Error(payload?.error || "默认模拟数据加载失败");
    }
    const rows = Array.isArray(payload.rows) ? payload.rows : [];
    if (!rows.length) throw new Error("默认模拟数据没有有效数据行");
    state.simulationDatasetCache = {
      ...payload,
      rows,
      total_rows: Number(payload.total_rows || rows.length),
      cache_start: 0,
      cache_rows: rows.slice(0, SIMULATION_PLAYBACK_CACHE_ROWS),
    };
    state.simulationSourceChannels = Array.isArray(payload.columns)
      ? payload.columns : state.simulationSourceChannels;
    if (controls.cursor) {
      controls.cursor.max = String(state.simulationDatasetCache.total_rows);
      if (!state.guestSimulationStarted) controls.cursor.value = "1";
    }
    updateDatasetMeta();
    autoEnableSimulationChannels(state.simulationSourceChannels);
    return state.simulationDatasetCache;
  })();
  try {
    return await state.simulationDatasetPromise;
  } finally {
    state.simulationDatasetPromise = null;
  }
}

function simulationCacheFor(index) {
  const dataset = state.simulationDatasetCache;
  if (!dataset) return [];
  const total = dataset.rows.length;
  const safeIndex = Math.max(0, Math.min(total - 1, Number(index) || 0));
  // Keep a small look-ahead block ready in memory.  The full dataset is
  // loaded once, while playback/rendering advances through this rolling
  // 20-row block instead of issuing another network request per sample.
  const start = Math.floor(safeIndex / SIMULATION_PLAYBACK_CACHE_ROWS)
    * SIMULATION_PLAYBACK_CACHE_ROWS;
  dataset.cache_start = start;
  dataset.cache_rows = dataset.rows.slice(start, start + SIMULATION_PLAYBACK_CACHE_ROWS);
  return dataset.cache_rows;
}

function buildCachedSimulationPayload(index) {
  const dataset = state.simulationDatasetCache;
  const template = state.payload;
  if (!dataset || !template) return null;
  const rows = simulationCacheFor(index);
  const history = Math.max(1, Number(controls.history?.value || 240));
  const safeIndex = Math.max(0, Math.min(dataset.rows.length - 1, Number(index) || 0));
  const currentRow = rows[safeIndex - dataset.cache_start] || dataset.rows[safeIndex];
  const allRows = dataset.rows.slice(0, safeIndex + 1);
  const visibleRows = allRows.slice(-history);
  const channels = (template.channels || []).map((channel) => {
    const values = visibleRows.map((row) => {
      const value = Number(row?.[channel.name]);
      return Number.isFinite(value) ? value : null;
    });
    const cachedCurrent = Number(currentRow?.[channel.name]);
    const current = Number.isFinite(cachedCurrent)
      ? cachedCurrent
      : (values.length ? values[values.length - 1] : null);
    return {
      ...channel,
      actual: values,
      x_observed: values.map((_value, position) => position + 1),
      actual_current: current,
      prediction_observed: values.map(() => null),
      prediction_future: [],
      x_future: [],
      prediction_current: null,
      rmse: null,
    };
  });
  const selectedId = Number(controls.sensor?.value || 0);
  const selected = channels.find((channel) => Number(channel.id) === selectedId)
    || channels[0]
    || template.selected_channel;
  const total = dataset.rows.length;
  const cursor = Math.min(total, Number(index) + 1);
  const windowPosition = ((cursor - 1) % 24) + 1;
  return {
    ...template,
    channels,
    selected_channel: selected,
    progress: {
      ...template.progress,
      cursor,
      total_points: total,
      sample_in_window: windowPosition,
      current_window: Math.floor((cursor - 1) / 24) + 1,
      total_windows_in_layer: Math.max(1, Math.ceil(total / 24)),
      finished: cursor >= total,
    },
  };
}

function renderCachedSimulationSample(index) {
  const payload = buildCachedSimulationPayload(index);
  if (!payload) return false;
  state.simulationPlaybackIndex = Number(index);
  state.payload = payload;
  render(payload);
  return true;
}

function startLocalSimulationReplay() {
  if (!isGuestSimulationMode() || !state.simulationDatasetCache || !state.payload) return;
  state.simulationLocalReplay = true;
  // configureDataMode starts the normal LAN poller for live mode.  Once the
  // complete dataset is in browser memory that poller must be stopped, or it
  // would overwrite the locally rendered cursor on every response.
  window.clearInterval(state.livePollTimer);
  state.livePollTimer = null;
  closeLiveWebSocket();
  stopPlayback();
  const total = state.simulationDatasetCache.rows.length;
  state.simulationPlaybackIndex = -1;
  const tick = () => {
    if (!state.simulationLocalReplay || !state.guestSimulationStarted) return;
    const next = Math.min(total - 1, state.simulationPlaybackIndex + 1);
    renderCachedSimulationSample(next);
    if (next >= total - 1) {
      state.simulationLocalReplay = false;
      $("streamStatus").textContent = "模拟数据已播放完成";
      document.querySelector(".live-dot")?.classList.remove("active");
      return;
    }
    state.simulationLocalReplayTimer = window.setTimeout(tick, playbackInterval());
  };
  window.clearTimeout(state.simulationLocalReplayTimer);
  $("streamStatus").textContent = "模拟数据本地回放中";
  document.querySelector(".live-dot")?.classList.add("active");
  tick();
}

function stopLocalSimulationReplay() {
  state.simulationLocalReplay = false;
  window.clearTimeout(state.simulationLocalReplayTimer);
  state.simulationLocalReplayTimer = null;
}

function openLiveWebSocket() {
  if (!usePublicLiveWebSocket() || controls.dataMode.value !== "live") return;
  const query = queryString();
  if (
    state.liveSocket
    && state.liveSocketQuery === query
    && (state.liveSocket.readyState === WebSocket.OPEN
      || state.liveSocket.readyState === WebSocket.CONNECTING)
  ) return;
  if (state.liveSocket) {
    state.liveSocket.onclose = null;
    state.liveSocket.close();
    state.liveSocket = null;
  }
  state.liveSocketQuery = query;
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  const endpoint = state.accessRole === "guest" ? "/api/simulation/ws" : "/api/live/ws";
  const socket = new WebSocket(`${scheme}//${window.location.host}${endpoint}?${query}`);
  state.liveSocket = socket;
  socket.onopen = () => {
    state.liveSocketReconnectAttempt = 0;
    $("connectionStatus").textContent = "实时数据推送已连接";
    document.querySelector(".status-dot").classList.add("connected");
  };
  socket.onmessage = (event) => {
    try {
      applyRealtimePayload(JSON.parse(event.data));
    } catch (error) {
      $("connectionStatus").textContent = `实时数据服务异常：${error.message}`;
    }
  };
  socket.onerror = () => {
    $("connectionStatus").textContent = "实时数据推送连接异常";
  };
  socket.onclose = () => {
    if (state.liveSocket === socket) state.liveSocket = null;
    if (!usePublicLiveWebSocket() || controls.dataMode.value !== "live") return;
    const attempt = Math.min(6, state.liveSocketReconnectAttempt + 1);
    state.liveSocketReconnectAttempt = attempt;
    state.liveSocketReconnectTimer = window.setTimeout(
      openLiveWebSocket,
      Math.min(5000, 500 * (2 ** (attempt - 1))),
    );
  };
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
    forecast_lead: controls.forecastLead?.value || "1",
    realtime_prediction: controls.realtimePrediction.checked,
    acquisition_mode: controls.acquisitionMode.value,
    processing_mode: controls.processingMode.value,
    use_optimized_warning: controls.optimizedWarning.checked,
    dataset_schema: activeInputSchemaId(),
    prediction_model_type: controls.predictionModelType?.value || "i_T_G",
    prediction_sensors: predictionSensors.length
      ? predictionSensors.join(",")
      : "__none__",
  }).toString();
}

async function loadRealtime() {
  if (state.simulationLocalReplay) return state.payload;
  if (usePublicLiveWebSocket()) {
    openLiveWebSocket();
    return;
  }
  if (state.busy) {
    state.reloadQueued = true;
    return;
  }
  state.busy = true;
  try {
    const endpoint = controls.dataMode.value === "live"
      ? (state.accessRole === "guest" ? "/api/simulation/live" : "/api/live")
      : "/api/realtime";
    const response = await fetch(`${endpoint}?${queryString()}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "实时数据请求失败");
    applyRealtimePayload(payload);
  } catch (error) {
    stopPlayback();
    toast(error.message);
    markServerDisconnected();
    $("connectionStatus").textContent = "实时数据服务连接失败";
    $("streamStatus").textContent = "数据服务异常";
    document.querySelector(".live-dot")?.classList.remove("active");
  } finally {
    state.busy = false;
    if (state.reloadQueued) {
      state.reloadQueued = false;
      window.setTimeout(loadRealtime, controls.dataMode.value === "live" ? livePollIntervalMs() : 0);
    }
  }
}

async function loadSimulationTemplate() {
  const response = await fetch(`/api/simulation/live?${queryString()}`, {
    cache: "no-store",
    credentials: "same-origin",
  });
  const payload = await response.json();
  if (!response.ok || payload?.error) {
    throw new Error(payload?.error || "模拟采集模板加载失败");
  }
  applyRealtimePayload(payload);
  return payload;
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
    toast("真实采集模式由传感器数据自动推进，无需启动数据流");
    return;
  }
  if (state.playing) return;
  state.playing = true;
  $("playButton").textContent = "❚❚ 暂停";
  $("streamStatus").textContent = "实时数据运行中";
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
  $("streamStatus").textContent = "实时数据已暂停";
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
    $("recommendationCard").classList.remove("not-recommended");
    $("recommendationCard").innerHTML =
      "<div><strong>仅采集模式</strong><span>预测模型与预警算法均未运行</span></div>";
  } else {
    renderRecommendation(payload.candidate);
  }

  if (specimen) {
    statePill($("specimenState"), specimen.state_label, specimen.state);
    $("specimenScore").textContent = `HI ${fmt(specimen.health)}`;
  $("specimenDetail").textContent =
      `当前已有 ${specimen.actual_layer_count ?? specimen.evidence_layers ?? 0} 层形成证据 · ${
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
          ? "由当前历史数据实测/预测窗口重新生成"
          : "等待完整窗口"
    }；最终预警：${
      windowData.optimized_warning_applied
        ? payload.mode === "live_acquisition"
          ? "因果在线一致性v13.9（不使用未来层）"
          : "原优化层级一致性v13.8"
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

function renderSensorCardsInto(target, channels, selectedId) {
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
  if (target) target.replaceChildren(...cards);
}

function renderSensorCards(channels, selectedId) {
  renderSensorCardsInto($("sensorCards"), channels, selectedId);
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
  node.classList.remove("not-recommended");
  node.innerHTML = `
    <div><strong>${candidate.indicator} · ${MODEL_LABELS[candidate.model] || candidate.model}</strong>
      <span>${candidate.recommended ? "当前指标推荐模型" : "非推荐模型，可用于对比"}</span></div>
    <p>验证选择分数 ${fmt(candidate.validation_selection_score, 3)}</p>
    <div class="recommendation-metrics">
      <span>窗 ${fmt(candidate.validation_window_balanced_accuracy * 100, 1)}%</span>
      <span>层 ${fmt(candidate.validation_layer_balanced_accuracy * 100, 1)}%</span>
      <span>试样 ${fmt(candidate.validation_specimen_balanced_accuracy * 100, 1)}%</span>
    </div>`;
  if (!candidate.recommended) {
    const label = node.querySelector("div span");
    if (label) label.remove();
  }
}

function renderLayerProgress(layers) {
  const target = $("layerProgress");
  if (!state.showLayerEvidence) {
    target.className = "evidence-sensor-panel";
    const toolbar = document.createElement("div");
    toolbar.className = "evidence-restore-bar";
    toolbar.innerHTML =
      '<span>证据进度与健康指标已隐藏，当前显示全部传感器通道</span>' +
      '<button type="button" class="secondary-button compact-button">恢复证据进度</button>';
    const restoreButton = toolbar.querySelector("button");
    restoreButton.addEventListener("click", () => {
      state.showLayerEvidence = true;
      try {
        localStorage.setItem(LAYER_EVIDENCE_VISIBILITY_KEY, "1");
      } catch (_error) {}
      applyLayerEvidenceVisibility();
      if (state.payload) renderLayerProgress(state.payload.layers);
    });
    const grid = document.createElement("div");
    grid.className = "sensor-grid evidence-sensor-grid";
    renderSensorCardsInto(
      grid,
      state.payload?.channels || [],
      state.payload?.selection?.sensor,
    );
    target.replaceChildren(toolbar, grid);
    return;
  }
  target.className = "layer-progress";
  target.replaceChildren(...layers.map((layer) => {
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
    refreshLanWebStatus().catch(() => markServerDisconnected());
    window.clearInterval(state.lanStatusTimer);
    state.lanStatusTimer = window.setInterval(() => {
      refreshLanWebStatus().catch(() => markServerDisconnected());
    }, 10000);
    // Start the public bootstrap request immediately.  The other independent
    // defaults/status requests no longer serialize behind it over the tunnel.
    const bootstrapPromise = fetch("/api/bootstrap", { cache: "no-store" });
    await loadAccessSession();
    renderHelperStatus();
    await Promise.all([
      loadHelperStatus({deferDiscovery: true}),
      loadAgentDefaults(),
      loadMysqlDefaults(),
    ]);
    window.clearInterval(state.helperStatusTimer);
    state.helperStatusTimer = window.setInterval(() => {
      loadHelperStatus({deferDiscovery: true}).catch(() => {});
    }, 5000);
    syncLocalMysqlSection();
    syncTargetMysqlSection();
    const response = await bootstrapPromise;
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "初始化失败");
    state.bootstrap = payload;
    state.defaults = payload.defaults;
    state.sensorTypeProfiles = payload.acquisition.sensor_types || [];
    populatePredictionModelTypes();
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
    ensureFirstInterfaceRole();
    ensureFirstInterfaceSummary();
    placeSecondInterfaceAfterFirst();
      if (state.accessRole !== "guest") {
        await discoverInterfaces();
      } else {
        const discovery = payload.acquisition?.interface_discovery || {};
        state.physicalInterfaces = Array.isArray(discovery.physical_interfaces)
          ? discovery.physical_interfaces : [];
        state.interfaceCatalog = autoAssignPhysicalInterfaces(
          (payload.acquisition?.interface_defaults || defaultInterfaceCatalog()).map((item) => ({...item})),
          {allowSerialFallback: false},
        );
        renderInterfacePanel(state.interfaceCatalog);
        const assigned = (state.interfaceCatalog || [])
          .filter((item) => item.enabled && item.physical_interface_id)
          .map((item) => `${item.id}→${item.physical_interface_id}`);
        if (controls.interfaceDiscoveryStatus) {
          controls.interfaceDiscoveryStatus.textContent = assigned.length
            ? `访客模拟模式：已读取服务器接口映射（只识别接口，不检查传感器连接）：${assigned.join("、")}`
            : "访客模拟模式：暂无服务器接口清单，使用逻辑模拟接口映射";
        }
      }
    controls.datasetSchema.replaceChildren(
      ...payload.acquisition.schemas.map((item) =>
        option(item.id, item.label)
      )
    );
    controls.datasetSchema.value = payload.acquisition.schemas.some(
      (item) => item.id === "new_collection_v11_3"
    ) ? "new_collection_v11_3" : "legacy_original";
    configureDatasetSchema(true);
    state.liveScopeKey = liveEvidenceScopeKey();
    controls.dataMode.value = payload.defaults.data_mode || "live";
    controls.specimen.value = payload.defaults.specimen;
    controls.sensor.value = String(payload.defaults.sensor);
    controls.cursor.value = payload.defaults.cursor;
    controls.history.value = payload.defaults.history;
    controls.step.value = payload.defaults.step;
    controls.streamStep.value = payload.defaults.stream_step;
    controls.horizon.value = payload.defaults.prediction_horizon;
    controls.horizonNumber.value = payload.defaults.prediction_horizon;
    if (controls.forecastLead) {
      const lead = Number(payload.defaults.forecast_lead || 1);
      controls.forecastLead.value = String(lead);
      controls.forecastLeadNumber.value = String(lead);
      controls.forecastLeadValue.textContent = String(lead);
    }
    controls.realtimePrediction.checked = Boolean(payload.defaults.realtime_prediction);
    controls.optimizedWarning.checked = Boolean(payload.defaults.use_optimized_warning);
    controls.saveRoot.value = payload.acquisition.default_save_root || "";
    state.localSaveDirectoryHandle = null;
    state.localSaveAuthorized = false;
    state.localSaveNameDirty = false;
    updateLocalSaveStatus("未确认本地目录；当前不会保存到访问网页的电脑。");
    refreshSaveRootStatus();
    state.simulationSourceChannels = Array.isArray(payload.acquisition.simulation_source_channels)
      ? payload.acquisition.simulation_source_channels : [];
    document.querySelectorAll(".interface-config-row").forEach((row) => refreshPhysicalInterfaceOptions(row));
    if (controls.simulationSourceType && payload.acquisition.simulation_source_type) {
      controls.simulationSourceType.value = payload.acquisition.simulation_source_type;
    }
    if (controls.simulationSourcePath && payload.acquisition.simulation_source_name) {
      controls.simulationSourcePath.value = payload.acquisition.simulation_source_name;
    }
    controls.threshold.value = payload.defaults.threshold;
    controls.rho.value = payload.defaults.rho;
    controls.indicator.value = payload.defaults.indicator;
    try {
      state.showLayerEvidence = localStorage.getItem(
        LAYER_EVIDENCE_VISIBILITY_KEY
      ) !== "0";
    } catch (_error) {
      state.showLayerEvidence = true;
    }
    applyLayerEvidenceVisibility();
    populateModels(true);
    configureAutomaticIndicator(true);
    updateDatasetMeta();
    $("acquisitionSection").classList.toggle(
      "hidden", controls.dataMode.value !== "live"
    );
    updateSimulationSettings();
    if (isGuestSimulationMode()) {
      await loadSimulationDatasetOnce();
    }
    await loadRealtime();
    scheduleAutomaticHardwareCheck(800);
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
controls.dataMode.addEventListener("change", () => {
  const previousScope = state.liveScopeKey;
  configureDataMode();
  if (controls.dataMode.value === "live") {
    const nextScope = liveEvidenceScopeKey();
    if (previousScope !== null && previousScope !== nextScope) {
      resetLiveEvidenceDisplay();
    } else {
      state.liveScopeKey = nextScope;
    }
  }
});
controls.processingMode.addEventListener("change", () => {
  configureProcessingMode();
  markLiveScopeChanged();
});
controls.datasetSchema.addEventListener("change", () => {
  configureDatasetSchema(true);
  markLiveScopeChanged();
  markHardwareCheckStale("数据方案与默认通道已变化");
  if (!controls.bestPredictionOverride.checked && controls.predictionModelType) {
    const schemaMode = controls.datasetSchema.value || "legacy_original";
    const modelType = controls.predictionModelType.value || "i_T_G";
    controls.predictionModel.value = lastModelPath(schemaMode, modelType);
    inspectPredictionModel(true).catch(() => {});
  }
});
controls.driver.addEventListener("change", updateSecondInterfaceVisibility);
controls.endpoint.addEventListener("input", updateSecondInterfaceVisibility);
controls.autoIndicator.addEventListener("change", () => {
  configureAutomaticIndicator(true);
  loadRealtime();
});
controls.sensor.addEventListener("change", loadRealtime);
controls.history.addEventListener("change", loadRealtime);
controls.step.addEventListener("change", loadRealtime);
controls.indicator.addEventListener("change", () => {
  populateModels(true);
  markLiveScopeChanged();
  loadRealtime();
});
controls.model.addEventListener("change", () => {
  applyCandidateDefaults();
  markLiveScopeChanged();
  loadRealtime();
});
controls.realtimePrediction.addEventListener("change", loadRealtime);
controls.optimizedWarning.addEventListener("change", loadRealtime);
controls.bestPredictionOverride.addEventListener(
  "change", configureBestPredictionOverride
);
$("testSensorsButton").addEventListener("click", () => testSensorConnection({automatic: false}));
agentApiKeyInput?.addEventListener("input", handleAgentInputChange);
agentModelNameInput?.addEventListener("input", handleAgentInputChange);
agentDiagnoseButton?.addEventListener("click", () => runAgentDiagnosis({automatic: false}));
$("unlock-real-mode")?.addEventListener("click", showRealAccessModal);
$("pairHelperButton")?.addEventListener("click", () => pairLocalHelper().catch((error) => toast(error.message)));
$("lock-real-mode")?.addEventListener("click", lockRealMode);
$("real-access-submit")?.addEventListener("click", unlockRealMode);
$("real-access-cancel")?.addEventListener("click", hideRealAccessModal);
$("real-access-password")?.addEventListener("keydown", (event) => {
  if (event.key === "Enter") unlockRealMode();
});
$("admin-security-settings")?.addEventListener("click", showAdminSecuritySettings);
$("admin-security-save")?.addEventListener("click", saveAdminSecuritySettings);
$("admin-security-cancel")?.addEventListener("click", hideAdminSecuritySettings);
$("lan-web-copy")?.addEventListener("click", copyLanWebUrl);
controls.resetSensorCheck?.addEventListener("click", resetAndCheckHardware);
controls.autoHardwareCheck?.addEventListener("change", () => {
  if (!controls.autoHardwareCheck.checked && state.hardwareCheckTimer) {
    window.clearTimeout(state.hardwareCheckTimer);
    state.hardwareCheckTimer = null;
  }
});
controls.discoverInterfaces?.addEventListener("click", discoverInterfaces);
controls.addInterface?.addEventListener("click", addInterface);
controls.autoProcessParameters?.addEventListener("change", () => {
  if (controls.autoProcessParameters.checked) {
    readProcessParameters({automatic: true});
  } else if (controls.processParameterReadStatus) {
    controls.processParameterReadStatus.classList.remove("ok", "error");
    controls.processParameterReadStatus.textContent =
      "未启用自动读取；当前使用下方手动输入值。";
    controls.processParameterReadStatus.title = "";
  }
});
controls.readProcessParameters?.addEventListener(
  "click", () => readProcessParameters()
);
$("testMysqlButton")?.addEventListener("click", () => testMysqlConnection(false));
$("testLocalMysqlButton")?.addEventListener("click", () => testMysqlConnection(true));
$("refreshLocalRelationMapButton")?.addEventListener("click", () => refreshRelationMap("local"));
$("refreshTargetRelationMapButton")?.addEventListener("click", () => refreshRelationMap("target"));
$("previewMysqlDataButton")?.addEventListener("click", previewRemoteMysqlData);
$("copyMysqlPreviewButton")?.addEventListener("click", copyRemoteMysqlPreview);
$("downloadMysqlCsvButton")?.addEventListener("click", downloadRemoteMysqlCsv);
controls.mysqlEnabled?.addEventListener("change", () => {
  syncTargetMysqlSection();
  if (!controls.mysqlEnabled.checked) state.mysqlConnectionTests.target = null;
  renderMysqlStatus({enabled: false, ok: false, saved_rows: 0});
});
controls.mysqlLocalEnabled?.addEventListener("change", () => {
  syncLocalMysqlSection();
  if (!controls.mysqlLocalEnabled.checked) state.mysqlConnectionTests.local = null;
  renderMysqlStatus({enabled: false, ok: false, saved_rows: 0});
});
[controls.mysqlHost, controls.mysqlPort, controls.mysqlUser,
  controls.mysqlPassword, controls.mysqlDatabase, controls.mysqlLocalHost,
  controls.mysqlLocalPort, controls.mysqlLocalUser, controls.mysqlLocalPassword,
  controls.mysqlLocalDatabase].forEach((control) => {
  control?.addEventListener("input", () => {
    if ([controls.mysqlLocalHost, controls.mysqlLocalPort, controls.mysqlLocalUser,
      controls.mysqlLocalPassword, controls.mysqlLocalDatabase].includes(control)) {
      state.mysqlConnectionTests.local = null;
    } else {
      state.mysqlConnectionTests.target = null;
    }
    if (controls.mysqlEnabled?.checked) {
      renderMysqlStatus({enabled: false, ok: false, saved_rows: 0});
    }
  });
});
controls.mysqlDatabase?.addEventListener("change", () => {
  const status = $("mysqlStatus");
  if (status) {
    const database = controls.mysqlDatabase.value.trim() || "afp_state_warning";
    status.textContent = `当前选择数据库：${database}；点击“刷新关系表”读取该数据库。`;
  }
});
$("selectSaveRootButton").addEventListener("click", selectSaveRoot);
controls.confirmLocalSave?.addEventListener("click", confirmLocalSave);
controls.saveRoot?.addEventListener("input", () => {
  state.localSaveNameDirty = true;
  if (state.localSaveAuthorized) {
    state.localSaveAuthorized = false;
    state.localSaveDirectoryHandle = null;
    updateLocalSaveStatus("保存位置已改变，请重新确认并授权本地保存。", true);
  }
  if (state.saveStatusTimer) window.clearTimeout(state.saveStatusTimer);
  state.saveStatusTimer = window.setTimeout(() => {
    state.saveStatusTimer = null;
    refreshSaveRootStatus();
  }, 300);
});
$("selectPredictionModelButton").addEventListener("click", selectPredictionModel);
controls.predictionModel.addEventListener("change", () => {
  if (!controls.bestPredictionOverride.checked) {
    const schemaMode = controls.datasetSchema.value || "legacy_original";
    const modelType = controls.predictionModelType?.value || "i_T_G";
    rememberModelPath(schemaMode, modelType, controls.predictionModel.value.trim());
    state.manualPredictionModels[
      schemaMode
    ] = controls.predictionModel.value.trim();
    inspectPredictionModel(false).catch(() => {});
  }
});
controls.predictionModelType?.addEventListener("change", () => {
  if (controls.bestPredictionOverride.checked) return;
  const schemaMode = controls.datasetSchema.value || "legacy_original";
  const modelType = controls.predictionModelType.value || "i_T_G";
  controls.predictionModel.value = lastModelPath(schemaMode, modelType);
  inspectPredictionModel(true)
    .then(() => toast(`已切换预测算法：${controls.predictionModelType.value}`))
    .catch(() => {});
});
$("startAcquisitionButton").addEventListener("click", startAcquisition);
$("stopAcquisitionButton").addEventListener("click", stopAcquisition);
[
  controls.liveSpecimen,
  controls.runId,
  controls.conditionId,
  controls.replicate,
  controls.livePower,
  controls.liveSpeed,
  controls.livePressure,
  controls.initialForce,
  controls.placementSpeed,
  controls.pidAngle,
  controls.temperatureSetpoint,
].filter(Boolean).forEach((control) => {
  control.addEventListener("change", markLiveScopeChanged);
});
enforceStepOneInteger(controls.replicate, 1);
enforceStepOneInteger(controls.liveLayer, 0);
enforceStepOneInteger(controls.newLayer, 0);
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
function commitForecastLead() {
  if (!controls.forecastLead || !controls.forecastLeadNumber) return;
  const value = Math.max(
    1,
    Math.min(24, Number(controls.forecastLeadNumber.value) || 1)
  );
  controls.forecastLead.value = String(value);
  controls.forecastLeadNumber.value = String(value);
  controls.forecastLeadValue.textContent = String(value);
  loadRealtime();
}
if (controls.forecastLead) {
  controls.forecastLead.addEventListener("input", () => {
    const value = Number(controls.forecastLead.value);
    controls.forecastLeadNumber.value = String(value);
    controls.forecastLeadValue.textContent = String(value);
    scheduleLoad();
  });
  controls.forecastLeadNumber.addEventListener("input", () => {
    const rawValue = Number(controls.forecastLeadNumber.value);
    if (!Number.isFinite(rawValue) || rawValue < 1 || rawValue > 24) return;
    const value = Math.trunc(rawValue);
    controls.forecastLead.value = String(value);
    controls.forecastLeadValue.textContent = String(value);
    scheduleLoad();
  });
  controls.forecastLeadNumber.addEventListener("change", commitForecastLead);
  controls.forecastLeadNumber.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      commitForecastLead();
      controls.forecastLeadNumber.blur();
    }
  });
}
controls.threshold.addEventListener("input", scheduleLoad);
controls.rho.addEventListener("input", scheduleLoad);
$("residualToggle").addEventListener("change", (event) => {
  state.showResidual = event.target.checked;
  if (state.payload) renderSeriesChart(state.payload.selected_channel);
});
controls.showLayerEvidence?.addEventListener("change", (event) => {
  state.showLayerEvidence = Boolean(event.target.checked);
  try {
    localStorage.setItem(
      LAYER_EVIDENCE_VISIBILITY_KEY,
      state.showLayerEvidence ? "1" : "0"
    );
  } catch (_error) {
    // Visibility preference is optional in restricted desktop runtimes.
  }
  applyLayerEvidenceVisibility();
  if (state.payload) renderLayerProgress(state.payload.layers);
});
window.addEventListener("resize", () => {
  if (!state.payload) return;
  renderSeriesChart(state.payload.selected_channel);
  renderTimeline(state.payload.timeline);
  renderSensorCards(state.payload.channels, state.payload.selection.sensor);
  if (!state.showLayerEvidence) renderLayerProgress(state.payload.layers);
});

// Collapsible modules need a redraw after opening so canvases measure their
// visible width instead of the zero-width closed state.
document.querySelectorAll("details.collapsible-panel, details.collapsible-status-card, details.collapsible-rail-block")
  .forEach((module) => {
    module.addEventListener("toggle", () => {
      if (module.open) window.requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
    });
  });
document.querySelectorAll(".collapsible-panel-summary .legend").forEach((legend) => {
  legend.addEventListener("click", (event) => event.stopPropagation());
});

const COLUMN_LAYOUT_STORAGE_KEY = "afp-state-monitor-column-layout-v1";

function redrawChartsAfterColumnResize() {
  if (!state.payload) return;
  window.requestAnimationFrame(() => {
    renderSeriesChart(state.payload.selected_channel);
    renderTimeline(state.payload.timeline);
    renderSensorCards(state.payload.channels, state.payload.selection.sensor);
    if (!state.showLayerEvidence) renderLayerProgress(state.payload.layers);
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
  // Permit the upper monitoring/health-indicator area to cover the evidence
  // row completely.  A double-click still restores the default split.
  const lowerMin = 0;

  function upperMax() {
    const styles = getComputedStyle(workspace);
    const padding = parseFloat(styles.paddingTop) + parseFloat(styles.paddingBottom);
    const gaps = parseFloat(styles.rowGap || styles.gap) * 2;
    return Math.max(upperMin, workspace.clientHeight - padding - gaps - 4 - lowerMin);
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

const SENSOR_CHECKLIST_HEIGHT_STORAGE_KEY = "afp-state-monitor-sensor-list-height-v1";

function initializeSensorChecklistResizer() {
  const checklist = $("liveSensorChecklist");
  const handle = $("sensorChecklistResizer");
  if (!checklist || !handle) return;

  const minimum = 260;
  const maximum = 1200;
  const defaultHeight = 760;

  function applyHeight(value, persist = true) {
    const safe = Math.max(minimum, Math.min(maximum, Number(value) || defaultHeight));
    checklist.style.height = `${Math.round(safe)}px`;
    handle.setAttribute("aria-valuemin", String(minimum));
    handle.setAttribute("aria-valuemax", String(maximum));
    handle.setAttribute("aria-valuenow", String(Math.round(safe)));
    if (persist) {
      try { localStorage.setItem(SENSOR_CHECKLIST_HEIGHT_STORAGE_KEY, String(safe)); } catch (_error) {}
    }
  }

  function resetHeight() {
    try { localStorage.removeItem(SENSOR_CHECKLIST_HEIGHT_STORAGE_KEY); } catch (_error) {}
    applyHeight(defaultHeight, false);
  }

  handle.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    const startY = event.clientY;
    const startHeight = checklist.getBoundingClientRect().height;
    handle.classList.add("dragging");
    document.body.classList.add("resizing-sensor-list");

    function move(pointerEvent) {
      applyHeight(startHeight + pointerEvent.clientY - startY, false);
    }

    function end() {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", end);
      window.removeEventListener("pointercancel", end);
      handle.classList.remove("dragging");
      document.body.classList.remove("resizing-sensor-list");
      applyHeight(checklist.getBoundingClientRect().height, true);
    }

    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end, { once: true });
    window.addEventListener("pointercancel", end, { once: true });
  });

  handle.addEventListener("dblclick", resetHeight);
  handle.addEventListener("keydown", (event) => {
    if (!["ArrowUp", "ArrowDown"].includes(event.key)) return;
    event.preventDefault();
    const delta = event.key === "ArrowDown" ? 24 : -24;
    applyHeight(checklist.getBoundingClientRect().height + delta, true);
  });

  try {
    const saved = Number(localStorage.getItem(SENSOR_CHECKLIST_HEIGHT_STORAGE_KEY));
    if (Number.isFinite(saved) && saved >= minimum) applyHeight(saved, false);
    else resetHeight();
  } catch (_error) {
    resetHeight();
  }
}

initializeColumnResizers();
initializeVerticalPanelResizer();
initializeSensorChecklistResizer();
initialize();

// Unified sensor-interface cards.  The legacy driver/endpoint fields remain
// hidden compatibility fields for older API payloads, while every visible
// interface (including the first one) is rendered by the same card renderer.
function hideLegacyInterfaceFields() {
  [controls.driver, controls.endpoint, controls.baudrate].forEach((node) => {
    node?.closest("label")?.classList.add("legacy-interface-field");
  });
}

function recognizedInterfacePorts() {
  const ports = Array.isArray(state.availableInterfaces) ? state.availableInterfaces : [];
  const seen = new Set();
  return ports.filter((port) => {
    const endpoint = String(port.endpoint || port.id || "").trim();
    const key = endpoint.toUpperCase();
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function interfaceConfigs() {
  const rows = [...(controls.interfacePanel?.querySelectorAll(".interface-config-row") || [])];
  const interfaces = rows.map((row, index) => {
    const mapText = row.querySelector(".interface-map")?.value || "";
    let channelMap = {};
    try { channelMap = mapText.trim() ? JSON.parse(mapText) : {}; } catch (_) { channelMap = {}; }
    return {
      id: row.dataset.interfaceId || `interface_${index + 1}`,
      enabled: row.querySelector(".interface-enabled")?.checked !== false,
      role: row.querySelector(".interface-role")?.value || (index === 0 ? "thermocouple" : "other"),
      driver: row.querySelector(".interface-driver")?.value || "serial_json",
      endpoint: row.querySelector(".interface-endpoint")?.value?.trim() || "",
      baudrate: Number(row.querySelector(".interface-baudrate")?.value) || 115200,
      physical_interface_id: row.querySelector(".interface-physical")?.value || "",
      physical_interface_kind: row.querySelector(".interface-physical")?.selectedOptions?.[0]?.dataset?.kind || "",
      physical_verified: row.querySelector(".interface-physical")?.selectedOptions?.[0]?.dataset?.detected === "true",
      physical_fallback: row.querySelector(".interface-physical")?.selectedOptions?.[0]?.dataset?.fallback === "true",
      channel_map: channelMap,
    };
  });
  const first = interfaces[0];
  if (first) {
    if (controls.driver) controls.driver.value = first.driver;
    if (controls.endpoint) controls.endpoint.value = first.endpoint;
    if (controls.baudrate) controls.baudrate.value = String(first.baudrate || 115200);
  }
  const assignments = {};
  const selectedChannels = new Set(selectedAcquisitionChannelsForInterfaces());
  document.querySelectorAll(".interface-route-select").forEach((select) => {
    const id = select.value;
    const channel = select.dataset.channel;
    // A channel that is not selected for acquisition must never remain in the
    // interface mapping submitted to the backend.  Keeping this guard here
    // makes the submitted configuration authoritative even if a stale DOM
    // node survives a checklist refresh.
    if (selectedChannels.has(channel) && id && id !== "__unassigned__" && channel) {
      (assignments[id] ||= []).push(channel);
    }
  });
  state.interfaceAssignments = assignments;
  return { interfaces, assignments };
}

function ensureFirstInterfaceRole() {}
function ensureFirstInterfaceSummary() {}
function placeSecondInterfaceAfterFirst() {}

function updateSecondInterfaceVisibility() {
  controls.interfacePanel?.classList.remove("hidden");
  document.querySelector(".interface-toolbar")?.classList.remove("hidden");
  controls.interfacePanel?.nextElementSibling?.classList.remove("hidden");
  refreshChannelInterfaceOptions();
}

function currentInterfaceItems() {
  return [...(controls.interfacePanel?.querySelectorAll(".interface-config-row") || [])]
    .map((row) => ({
      id: row.dataset.interfaceId,
      role: row.querySelector(".interface-role")?.value || "other",
      driver: row.querySelector(".interface-driver")?.value || "serial_json",
      endpoint: row.querySelector(".interface-endpoint")?.value?.trim() || "",
      enabled: row.querySelector(".interface-enabled")?.checked !== false,
    }))
    .filter((item) => item.enabled);
}

function defaultInterfaceForChannel(channel) {
  const items = currentInterfaceItems();
  const preferred = items.find((item) => {
    const profile = sensorTypeProfile(item.role);
    return (profile.channels || []).includes(channel);
  }) || items.find((item) => sensorTypeProfile(item.role).editable_driver);
  return (preferred || items[0])?.id || "";
}

function refreshInterfaceEndpointOptions() {
  const rows = [...(controls.interfacePanel?.querySelectorAll(".interface-config-row") || [])];
    rows.forEach((row) => {
      const endpoint = row.querySelector(".interface-endpoint");
      if (!endpoint) return;
      endpoint.disabled = false;
    });
}

function physicalCandidatesForRole(role) {
  const profile = sensorTypeProfile(role);
  const kind = profile.physical_kind || "";
  const candidates = (Array.isArray(state.physicalInterfaces) ? state.physicalInterfaces : [])
    .map((item) => ({...item, kind: item.kind || item.interface_kind || item.type || ""}))
    .filter((item) => !kind || item.kind === kind || (kind === "ethernet" && item.kind === "ethernet_adapter") || (kind === "serial" && item.kind === "com"))
    .filter((item) => item.detected !== false || item.driver_available || item.auto_assignable);
  if (candidates.some((item) => item.detected !== false)) return candidates;
  const serialFallbacks = (Array.isArray(state.physicalInterfaces) ? state.physicalInterfaces : [])
    .map((item) => ({...item, kind: item.kind || item.interface_kind || item.type || ""}))
    .filter((item) => (item.kind === "serial" || item.kind === "com") && item.detected !== false);
  return [...candidates, ...serialFallbacks];
}

function autoAssignPhysicalInterfaces(configs, {allowSerialFallback = true} = {}) {
  const candidates = Array.isArray(state.physicalInterfaces) ? state.physicalInterfaces : [];
  const used = new Set();
  const assigned = {};
  const normalizedKind = (item) => item.kind || item.interface_kind || item.type || "";
  const compatible = (item, profile) => {
    const kind = normalizedKind(item);
    return !profile.physical_kind || kind === profile.physical_kind
      || (profile.physical_kind === "ethernet" && kind === "ethernet_adapter")
      || (profile.physical_kind === "serial" && kind === "com");
  };
  (configs || []).forEach((item) => {
    const profile = sensorTypeProfile(item.role || "custom");
    const role = item.role || "custom";
    let selected = candidates.find((candidate) => candidate.id === item.physical_interface_id && compatible(candidate, profile));
    if (!selected && (role === "plc" || role === "robot")) {
      selected = assigned.ethernet || candidates.find((candidate) => compatible(candidate, profile) && candidate.detected !== false);
    }
    let fallback = false;
    if (!selected) {
      selected = candidates.find((candidate) => compatible(candidate, profile) && candidate.detected !== false && !used.has(candidate.id));
    }
    // A serial fallback is only meaningful for serial protocols.  In real
    // mode a missing USB HID/UVC device must remain unassigned instead of
    // silently binding to an unrelated COM port.
    if (!selected && allowSerialFallback && profile.physical_kind === "serial") {
      selected = candidates.find((candidate) => (normalizedKind(candidate) === "serial" || normalizedKind(candidate) === "com") && candidate.detected !== false && !used.has(candidate.id));
      fallback = Boolean(selected);
    }
    if (!selected) {
      selected = candidates.find((candidate) => compatible(candidate, profile)
        && (candidate.auto_assignable || candidate.driver_available)
        && !used.has(candidate.id));
    }
    if (!selected) return;
    item.physical_interface_id = selected.id;
    item.physical_interface_kind = normalizedKind(selected);
    item.physical_verified = selected.detected !== false;
    item.physical_fallback = fallback || (item.physical_interface_kind !== profile.physical_kind);
    if ((fallback || item.physical_interface_kind === "serial" || item.physical_interface_kind === "com") && selected.endpoint) {
      item.endpoint = selected.endpoint;
    }
    item.enabled = true;
    if (normalizedKind(selected) === "ethernet" || normalizedKind(selected) === "ethernet_adapter") assigned.ethernet = selected;
    if (!(role === "plc" || role === "robot")) used.add(selected.id);
  });
  return configs;
}

function refreshPhysicalInterfaceOptions(row, preferredId = "") {
  const select = row.querySelector(".interface-physical");
  const role = row.querySelector(".interface-role")?.value || "custom";
  if (!select) return;
  const candidates = physicalCandidatesForRole(role);
  const current = preferredId || select.value;
  const profile = sensorTypeProfile(role);
  if (controls.acquisitionMode?.value === "simulation") {
    const options = candidates.map((item) => {
      const node = option(item.id, `${item.label || item.endpoint || item.id} · 模拟映射`);
      node.dataset.kind = item.kind || "";
      node.dataset.detected = String(item.detected !== false);
      node.dataset.assignable = String(Boolean(item.auto_assignable || item.driver_available));
      node.dataset.fallback = String(item.kind === "serial" && profile.physical_kind !== "serial");
      node.dataset.endpoint = item.endpoint || "";
      return node;
    });
    if (options.length) {
      select.replaceChildren(...options);
      select.value = options.some((node) => node.value === current)
        ? current : options[0].value;
      select.disabled = false;
    } else {
      select.replaceChildren(option("simulation_source", "逻辑模拟接口（未发现实际接口）"));
      select.value = "simulation_source";
      select.disabled = false;
    }
    const enabled = row.querySelector(".interface-enabled");
    if (enabled) {
      enabled.disabled = false;
      // Simulation mode validates the imported data mapping, not whether a
      // physical sensor is plugged in.  All logical interface slots remain
      // enabled so the supplied CSV can be replayed normally.
      enabled.checked = true;
    }
    let warning = row.querySelector(".interface-physical-warning");
    if (!warning) {
      warning = document.createElement("div");
      warning.className = "interface-physical-warning control-note";
      row.append(warning);
    }
    const selected = select.selectedOptions?.[0];
    warning.textContent = selected?.value === "simulation_source"
      ? "模拟采集：未检查传感器是否接入，使用逻辑模拟接口和导入数据"
      : `模拟映射：${selected.textContent}；不检查传感器是否接入`;
    warning.classList.remove("hidden");
    return;
  }
  const kindLabels = {usb_hid: "USB HID", usb_uvc: "USB/UVC", ethernet: "网卡", serial: "串口"};
  const emptyText = candidates.length
    ? "请选择已识别的实际接口"
    : `未发现匹配的实际接口（需要${kindLabels[profile.physical_kind] || profile.physical_kind || "对应协议"}）`;
  select.replaceChildren(option("", emptyText));
  candidates.forEach((item) => {
    const node = option(item.id, item.label || item.endpoint || item.id);
    node.dataset.kind = item.kind || "";
    node.dataset.detected = String(item.detected !== false);
    node.dataset.assignable = String(Boolean(item.auto_assignable || item.driver_available));
    node.dataset.fallback = String(item.kind === "serial" && profile.physical_kind !== "serial");
    node.dataset.endpoint = item.endpoint || "";
    select.append(node);
  });
  if (current && candidates.some((item) => item.id === current)) select.value = current;
  const selected = select.selectedOptions?.[0];
  select.disabled = !candidates.length;
  select.title = selected?.value
    ? `${selected.textContent}；协议类型：${sensorTypeProfile(role).protocol || "自定义"}`
    : "必须选择自动识别到的实际接口后才能启用";
  const enabled = row.querySelector(".interface-enabled");
  if (enabled) {
    enabled.disabled = !selected?.value
      || (selected.dataset.detected !== "true" && selected.dataset.assignable !== "true");
    if (enabled.disabled) enabled.checked = false;
  }
  let warning = row.querySelector(".interface-physical-warning");
  if (!warning) {
    warning = document.createElement("div");
    warning.className = "interface-physical-warning control-note";
    row.append(warning);
  }
  warning.textContent = selected?.dataset?.fallback === "true"
    ? "警告：当前未识别到匹配协议，临时分配串口，仅用于测试"
    : "";
  warning.classList.toggle("hidden", selected?.dataset?.fallback !== "true");
}

function itemEnabledForSimulation(row) {
  return row.querySelector(".interface-enabled")?.checked !== false;
}

function defaultInterfaceCatalog() {
  return [
    {
      id: "thermocouple_8ch", enabled: true, role: "thermocouple", driver: "smrf_hid",
      endpoint: "SMRFCT08B", channel_types: ["K", "K", "K", "K", "K", "K", "K", "K"], channel_map: {},
    },
    { id: "plc_process", enabled: true, role: "plc", driver: "modbus_tcp", endpoint: "192.168.125.5:502", channel_map: {} },
    { id: "uvc_temperature", enabled: true, role: "thermal_uvc", driver: "uvc_thermal", endpoint: "BSV UVC (WinUSB)", channel_map: {} },
    { id: "abb_motion", enabled: true, role: "robot", driver: "abb_robot", endpoint: "192.168.125.1", channel_map: {} },
    { id: "m3232_pressure", enabled: true, role: "pressure", driver: "m3232_pressure", endpoint: "COM8", baudrate: 115200, channel_map: {} },
  ];
}

// Simulation owns its logical interface configuration.  Physical bindings
// are only a read-only display aid and must never carry real-mode state back
// into the simulation form when the operator switches modes.
function buildSimulationInterfaceCatalog() {
  return defaultInterfaceCatalog().map((item) => ({
    ...item,
    enabled: true,
    physical_interface_id: "",
    physical_interface_kind: "",
    physical_verified: false,
    physical_fallback: false,
  }));
}

function renderInterfacePanel(configs) {
  if (!controls.interfacePanel) return;
  const defaults = Array.isArray(configs) ? configs : [];
  const initial = defaults.length ? defaults : defaultInterfaceCatalog();
  const unique = [];
  const usedEndpoints = new Set();
  initial.forEach((item, index) => {
    const endpoint = String(item.endpoint || "").trim();
    const key = endpoint.toUpperCase();
    if (key && usedEndpoints.has(key)) return;
    if (key) usedEndpoints.add(key);
    unique.push({...item, id: item.id || `interface_${index + 1}`});
  });
  controls.interfacePanel.replaceChildren(...unique.map((item, index) => {
    const row = document.createElement("div");
    row.className = "interface-config-row";
    row.dataset.interfaceId = item.id || `interface_${index + 1}`;
    const addLabel = (text, node, className = "") => {
      const label = document.createElement("label");
      if (className) label.className = className;
      label.append(document.createTextNode(text), node);
      return label;
    };
    const enabled = document.createElement("input");
    enabled.type = "checkbox"; enabled.className = "interface-enabled"; enabled.checked = item.enabled !== false;
    const role = document.createElement("select"); role.className = "interface-role";
    (state.sensorTypeProfiles.length ? state.sensorTypeProfiles : [{id: "custom", label: "自定义JSON传感器"}]).forEach((profile) => {
      const option = document.createElement("option"); option.value = profile.id; option.textContent = profile.label; role.append(option);
    });
    const canonicalRole = item.role === "thermal" ? (item.driver === "rtsp_thermal" ? "thermal_rtsp" : "thermal_uvc") : (["other", "new_sensor"].includes(item.role) ? "custom" : item.role);
    role.value = state.sensorTypeProfiles.some((profile) => profile.id === canonicalRole) ? canonicalRole : "custom";
    const driver = document.createElement("select"); driver.className = "interface-driver";
    [["smrf_hid", "SMRF USB HID"], ["serial_json", "串口 JSON"], ["tcp_json", "TCP JSON"], ["modbus_tcp", "Modbus TCP(PLC)"], ["abb_robot", "ABB RWS"], ["uvc_thermal", "BSV UVC热像仪"], ["rtsp_thermal", "IP热像仪 RTSP"], ["m3232_pressure", "M3232薄膜压力"], ["simulator", "CSV 模拟"]].forEach(([value, text]) => {
      const option = document.createElement("option"); option.value = value; option.textContent = text; driver.append(option);
    });
    driver.value = item.driver || "serial_json";
    const endpoint = document.createElement("input"); endpoint.className = "interface-endpoint"; endpoint.type = "text";
    const currentEndpoint = String(item.endpoint || "");
    endpoint.value = currentEndpoint;
    const baud = document.createElement("input"); baud.className = "interface-baudrate"; baud.type = "number"; baud.min = "1200"; baud.value = String(Number(item.baudrate || 115200));
    const physical = document.createElement("select"); physical.className = "interface-physical";
    const map = document.createElement("input"); map.className = "interface-map"; map.type = "text"; map.value = typeof item.channel_map === "string" ? item.channel_map : JSON.stringify(item.channel_map || {}); map.placeholder = '{"force":"压力"}';
    const summary = document.createElement("div"); summary.className = "interface-channel-summary";
    const profileDetail = document.createElement("div"); profileDetail.className = "interface-profile-detail control-note";
    const removeButton = document.createElement("button");
    removeButton.type = "button";
    removeButton.className = "secondary-button compact-button interface-remove-button";
    removeButton.textContent = "删除接口";
    removeButton.addEventListener("click", () => {
      const count = controls.interfacePanel?.querySelectorAll(".interface-config-row").length || 0;
      if (count <= 1) { toast("至少保留一个接口"); return; }
      row.remove();
      refreshChannelInterfaceOptions();
      refreshInterfaceEndpointOptions();
      markHardwareCheckStale("接口配置已删除");
    });
    row.append(addLabel(`接口 ${index + 1} · 启用`, enabled), addLabel("传感器类型", role), addLabel("实际物理接口", physical, "interface-physical-label"), addLabel("自动驱动/协议", driver), addLabel("连接地址", endpoint), addLabel("波特率", baud), addLabel("通道映射（仅自定义JSON）", map, "interface-map-label"), profileDetail, summary, removeButton);
    role.addEventListener("change", () => { applySensorTypeProfile(row, true); refreshPhysicalInterfaceOptions(row); refreshChannelInterfaceOptions(); markHardwareCheckStale("接口类型已变化"); });
    driver.addEventListener("change", () => {
      if (controls.acquisitionMode?.value !== "simulation" && driver.value === "simulator") {
        driver.value = "serial_json";
        toast("真实接口采集模式不允许使用本地模拟驱动");
      }
      refreshInterfaceEndpointOptions();
      refreshChannelInterfaceOptions();
    });
    endpoint.addEventListener("change", () => { refreshInterfaceEndpointOptions(); refreshChannelInterfaceOptions(); });
    physical.addEventListener("change", () => {
      const selected = physical.selectedOptions?.[0];
      const profile = sensorTypeProfile(role.value);
      if (selected?.dataset.endpoint && ["serial", "usb_hid", "usb_uvc"].includes(selected.dataset.kind)) {
        endpoint.value = selected.dataset.endpoint;
      } else if (profile.endpoint && !endpoint.value.trim()) {
        endpoint.value = profile.endpoint;
      }
      refreshPhysicalInterfaceOptions(row, physical.value);
      refreshChannelInterfaceOptions();
      markHardwareCheckStale("实际物理接口已变化");
    });
    enabled.addEventListener("change", refreshChannelInterfaceOptions);
    createInterfaceChannelEditor(row, row.dataset.interfaceId);
    applySensorTypeProfile(row, false);
    refreshPhysicalInterfaceOptions(row, item.physical_interface_id || "");
    if (item.physical_interface_id && physical.value === item.physical_interface_id) {
      enabled.checked = item.enabled !== false;
    }
    return row;
  }));
  hideLegacyInterfaceFields();
  refreshChannelInterfaceOptions();
}

async function discoverInterfaces() {
  if (controls.acquisitionMode?.value === "simulation") {
    rememberRealInterfaceSnapshot();
    state.availableInterfaces = [];
    state.physicalInterfaces = [];
    if (controls.interfaceDiscoveryStatus) {
      controls.interfaceDiscoveryStatus.textContent =
        "模拟采集不需要识别物理接口；仅使用当前选择的 CSV/文件夹/MySQL 数据源";
    }
    return;
  }
  if (usesLocalCaptureHelper()) {
    if (!state.helperStatus?.paired) {
      // Restore the cached real mapping while the helper reconnects (cached real mapping);
      // this is
      // intentionally display-only and is never used to start capture.
      if (!restoreCachedRealInterfaceSnapshot()) {
        state.interfaceCatalog = buildSimulationInterfaceCatalog().map((item) => ({
          ...item,
          enabled: false,
          physical_interface_id: "",
          physical_verified: false,
        }));
        renderInterfacePanel(state.interfaceCatalog);
      }
      if (controls.interfaceDiscoveryStatus) {
        controls.interfaceDiscoveryStatus.textContent =
          state.realInterfaceSnapshot
            ? "辅助程序暂时离线；已恢复最后一次真实接口映射，等待自动重连"
            : "真实采集等待本机采集辅助程序配对；未读取服务器电脑接口";
      }
      return;
    }
    try {
      const helper = await requestLocalHelper("discover", {}, {timeoutMs: 15000});
      const physical = Array.isArray(helper.interfaces) ? helper.interfaces : [];
      const defaults = Array.isArray(helper.raw_discovery?.defaults) && helper.raw_discovery.defaults.length
        ? helper.raw_discovery.defaults : defaultInterfaceCatalog();
      const bindings = new Map(
        (helper.sensor_bindings || []).map((item) => [String(item.role || ""), item]),
      );
      state.physicalInterfaces = physical;
      state.availableInterfaces = recognizedInterfacePortsFrom(helper.raw_discovery?.ports || []);
      // Older helpers may return the physical inventory but omit the binding
      // list.  Run the same protocol-aware allocator used by the LAN path so
      // every compatible (including explicitly auto-assignable placeholder)
      // interface gets a deterministic mapping before the cards render.
      const allocated = autoAssignPhysicalInterfaces(
        defaults.map((item) => ({...item})),
        {allowSerialFallback: true},
      );
      state.interfaceCatalog = allocated.map((item) => {
        const binding = bindings.get(String(item.role || ""));
        const physicalId = binding?.physical_interface_id || item.physical_interface_id || "";
        return {
          ...item,
          physical_interface_id: physicalId,
          physical_interface_kind: binding?.physical_kind || item.physical_interface_kind || "",
          physical_verified: Boolean(binding?.interface_detected || binding?.driver_available),
          enabled: Boolean(physicalId),
        };
      });
      state.realInterfaceSnapshot = {
        catalog: state.interfaceCatalog.map((item) => ({...item})),
        physical: physical.map((item) => ({...item})),
        available: state.availableInterfaces.map((item) => ({...item})),
      };
      state.realInterfaceSnapshotAt = Date.now();
      renderInterfacePanel(state.interfaceCatalog);
      markHardwareCheckStale("本机辅助程序接口识别结果已更新");
      if (controls.interfaceDiscoveryStatus) {
        controls.interfaceDiscoveryStatus.textContent = physical.length
          ? `已由本机辅助程序识别 ${physical.length} 个实际接口并完成传感器映射`
          : "本机辅助程序未发现兼容接口";
      }
      return;
    } catch (error) {
      restoreCachedRealInterfaceSnapshot();
      if (controls.interfaceDiscoveryStatus) {
        controls.interfaceDiscoveryStatus.textContent = state.realInterfaceSnapshot
          ? `辅助程序暂时离线，已显示最后一次真实映射：${error.message}`
          : `本机辅助程序识别失败：${error.message}`;
      }
      return;
    }
  }
  try {
    const result = await fetch("/api/acquisition/discover", {cache: "no-store"}).then((response) => response.json());
    state.availableInterfaces = recognizedInterfacePortsFrom(result.ports || []);
    state.physicalInterfaces = Array.isArray(result.physical_interfaces)
      ? result.physical_interfaces : [];
    const defaults = result.defaults || [];
    state.interfaceCatalog = autoAssignPhysicalInterfaces(
      (defaults.length ? defaults : defaultInterfaceCatalog()).map((item) => ({...item}))
    );
    renderInterfacePanel(state.interfaceCatalog);
    markHardwareCheckStale("接口识别结果已更新");
    const ports = state.physicalInterfaces;
    const assigned = (state.interfaceCatalog || [])
      .filter((item) => item.enabled && item.physical_interface_id)
      .map((item) => `${item.id}→${item.physical_interface_id}`);
    if (controls.interfaceDiscoveryStatus) controls.interfaceDiscoveryStatus.textContent = ports.length
      ? `已自动识别 ${ports.length} 个实际接口并分配默认绑定：${assigned.join("、")}`
      : "未发现物理接口；请检查 USB/串口/网卡驱动后重试";
  } catch (error) {
    if (controls.interfaceDiscoveryStatus) controls.interfaceDiscoveryStatus.textContent = `接口识别失败：${error.message}`;
  }
}

function rememberRealInterfaceSnapshot() {
  if (!usesLocalCaptureHelper() || !Array.isArray(state.interfaceCatalog) || !state.interfaceCatalog.length) return;
  state.realInterfaceSnapshot = {
    catalog: state.interfaceCatalog.map((item) => ({...item})),
    physical: (state.physicalInterfaces || []).map((item) => ({...item})),
    available: (state.availableInterfaces || []).map((item) => ({...item})),
  };
  state.realInterfaceSnapshotAt = Date.now();
}

function restoreCachedRealInterfaceSnapshot() {
  const snapshot = state.realInterfaceSnapshot;
  if (!snapshot?.catalog?.length) return false;
  state.interfaceCatalog = snapshot.catalog.map((item) => ({...item}));
  state.physicalInterfaces = (snapshot.physical || []).map((item) => ({...item}));
  state.availableInterfaces = (snapshot.available || []).map((item) => ({...item}));
  renderInterfacePanel(state.interfaceCatalog);
  return true;
}

function recognizedInterfacePortsFrom(ports) {
  const seen = new Set();
  return (Array.isArray(ports) ? ports : []).filter((port) => {
    const endpoint = String(port.endpoint || port.id || "").trim();
    const key = endpoint.toUpperCase();
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function addInterface() {
  const current = interfaceConfigs().interfaces;
  if (current.length >= 8) {
    toast("最多配置 8 个传感器接口");
    return;
  }
  const used = new Set(current.map((item) => item.physical_interface_id).filter(Boolean));
  const nextPhysical = (state.physicalInterfaces || []).find((item) => !used.has(item.id) && item.detected !== false);
  current.push({id: `interface_${current.length + 1}`, enabled: Boolean(nextPhysical), role: "custom", driver: "serial_json", endpoint: nextPhysical?.endpoint || "", baudrate: 115200, channel_map: {}, physical_interface_id: nextPhysical?.id || "", physical_interface_kind: nextPhysical?.kind || "", physical_verified: Boolean(nextPhysical)});
  state.interfaceCatalog = current;
  renderInterfacePanel(current);
  markHardwareCheckStale("已增加接口配置");
}

Object.assign(controls, {
  acquisitionMode: $("acquisitionModeSelect"),
  simulationSettings: $("simulationSettings"),
  simulationSourceType: $("simulationSourceTypeSelect"),
  simulationSourcePath: $("simulationSourcePathInput"),
  selectSimulationSource: $("selectSimulationSourceButton"),
  simulationSourceFile: $("simulationSourceFileInput"),
  simulationSourceNote: $("simulationSourceNote"),
  simulationSourcePathLabel: $("simulationSourcePathLabel"),
  simulationMysqlSettings: $("simulationMysqlSettings"),
  simulationMysqlHost: $("simulationMysqlHostInput"),
  simulationMysqlPort: $("simulationMysqlPortInput"),
  simulationMysqlUser: $("simulationMysqlUserInput"),
  simulationMysqlPassword: $("simulationMysqlPasswordInput"),
  simulationMysqlDatabase: $("simulationMysqlDatabaseInput"),
  simulationMysqlQuery: $("simulationMysqlQueryInput"),
  integrationSourceType: $("integrationSourceTypeSelect"),
  integrationFolder: $("integrationFolderInput"),
  selectIntegrationFolder: $("selectIntegrationFolderButton"),
  integrationFolderLabel: $("integrationFolderLabel"),
  integrationMysqlSettings: $("integrationMysqlSettings"),
  integrationMysqlHost: $("integrationMysqlHostInput"),
  integrationMysqlPort: $("integrationMysqlPortInput"),
  integrationMysqlUser: $("integrationMysqlUserInput"),
  integrationMysqlPassword: $("integrationMysqlPasswordInput"),
  integrationMysqlDatabase: $("integrationMysqlDatabaseInput"),
  integrationMysqlQuery: $("integrationMysqlQueryInput"),
  integrationOutput: $("integrationOutputInput"),
  runIntegration: $("runIntegrationButton"),
  integrationStatus: $("integrationStatus"),
});

function updateRealAcquisitionVisibility() {
  const liveAcquisition = controls.dataMode?.value === "live";
  // Neither real nor simulated acquisition selects a historical specimen.
  // Both modes build evidence from the current acquisition stream.
  controls.specimen?.closest("label")?.classList.toggle("hidden", liveAcquisition);
  $("specimenState")?.closest(".status-card")?.classList.toggle("hidden", liveAcquisition);
  controls.runId?.closest(".compact-input-grid")?.classList.add("hidden");
}

function updateSimulationSettings() {
  const simulation = controls.acquisitionMode?.value === "simulation";
  const saveRuleNote = document.querySelector(".save-rule-note");
  if (saveRuleNote) {
    saveRuleNote.textContent =
      "文件夹按工况与独立重复命名；每层保留分层文件，完整试样始终覆盖为同一份当前数据文件。";
  }
  controls.simulationSettings?.classList.toggle("hidden", !simulation);
  if (controls.interfaceDiscoveryStatus && simulation) {
    controls.interfaceDiscoveryStatus.textContent =
      "模拟采集不需要识别物理接口；仅使用当前选择的 CSV/文件夹/MySQL 数据源";
  }
  document.querySelector("#sourceFileInput")?.closest("label")?.classList.toggle("hidden", true);
  if (!simulation) {
    document.querySelectorAll(".interface-driver").forEach((node) => {
      if (node.value === "simulator") node.value = "serial_json";
    });
  }
  const mysql = controls.simulationSourceType?.value === "mysql";
  controls.simulationMysqlSettings?.classList.toggle("hidden", !simulation || !mysql);
  controls.simulationSourcePathLabel?.classList.toggle("hidden", !simulation || mysql);
  if (controls.simulationSourceNote) {
    controls.simulationSourceNote.textContent = state.accessRole === "guest"
      ? (mysql
        ? "访客 MySQL 使用本机管理员预先配置的数据源；如需导入本机文件，请切换为单 CSV或CSV文件夹并点击“选择”。"
        : "访客模式已自动载入管理员提供的模拟 CSV；数据直接用于采集、预测和预警。点击“选择”可导入本机 CSV或文件夹，导入后请点击“开始采集”。")
      : "模拟模式使用所选文件夹、CSV或MySQL数据逐行读取；真实接口模式不会读取本地文件。";
  }
  if (controls.simulationSourcePath) {
    controls.simulationSourcePath.placeholder = controls.simulationSourceType?.value === "folder_csv"
      ? "选择采集保存格式的数据文件夹"
      : "选择单个 CSV 文件";
  }
  updateRealAcquisitionVisibility();
  if (simulation) {
    rememberRealInterfaceSnapshot();
    const current = buildSimulationInterfaceCatalog();
    if (state.physicalInterfaces.length) {
      state.interfaceCatalog = autoAssignPhysicalInterfaces(current, {allowSerialFallback: false});
    } else {
      state.interfaceCatalog = current;
    }
    renderInterfacePanel(state.interfaceCatalog);
  } else {
    // Repaint the last real mapping immediately, then refresh status and
    // physical interfaces in the background.  This avoids a blank/unassigned
    // interval when switching back from simulation. The realInterfaceSnapshot
    // is display-only until the helper confirms a fresh discovery.
    restoreCachedRealInterfaceSnapshot();
    loadHelperStatus().catch(() => {});
    discoverInterfaces().catch(() => {});
  }
}

function updateIntegrationSource() {
  const mysql = controls.integrationSourceType?.value === "mysql";
  controls.integrationFolderLabel?.classList.toggle("hidden", mysql);
  controls.integrationMysqlSettings?.classList.toggle("hidden", !mysql);
}

async function selectIntegrationFolder() {
  try {
    const result = await postJson("/api/acquisition/select-folder", {
      initial_path: controls.integrationFolder?.value.trim() || "",
    });
    if (result.selected) {
      controls.integrationFolder.value = result.path;
      if (controls.integrationOutput && !controls.integrationOutput.value.trim()) {
        controls.integrationOutput.placeholder = result.path + "\\整合数据.csv";
      }
    }
  } catch (error) {
    toast("无法选择整合文件夹：" + error.message);
  }
}

async function runIntegration() {
  const mysql = controls.integrationSourceType?.value === "mysql";
  const status = controls.integrationStatus;
  if (!status) return;
  status.textContent = "正在读取并整合数据……";
  controls.runIntegration.disabled = true;
  try {
    const payload = {
      source_type: mysql ? "mysql" : "folder_csv",
      source_path: controls.integrationFolder?.value.trim() || "",
      output_file: controls.integrationOutput?.value.trim() || "",
      query: controls.integrationMysqlQuery?.value.trim() || "",
      mysql_settings: unifiedMysqlSettings(),
    };
    const result = await postJson("/api/acquisition/integrate", payload);
    status.textContent =
      "整合完成：" + result.rows + " 行，" + result.specimens + " 个试样，"
      + result.source_files + " 个来源文件；已保存：" + result.output_file;
  } catch (error) {
    status.textContent = "整合失败：" + error.message;
  } finally {
    controls.runIntegration.disabled = false;
  }
}

async function selectSimulationSource() {
  const sourceType = controls.simulationSourceType?.value || "single_csv";
  if (state.accessRole === "guest") {
    if (sourceType === "mysql") {
      toast("访客模式的 MySQL 使用本机管理员配置；请在本机管理页面设置数据源");
      return;
    }
    const picker = controls.simulationSourceFile;
    if (!picker) return;
    picker.multiple = sourceType === "folder_csv";
    picker.setAttribute("accept", ".csv,text/csv");
    if (sourceType === "folder_csv") picker.setAttribute("webkitdirectory", "");
    else picker.removeAttribute("webkitdirectory");
    picker.value = "";
    picker.click();
    return;
  }
  if (sourceType === "mysql") return;
  try {
    const endpoint = state.accessRole === "guest"
      ? "/api/simulation/select-source"
      : "/api/acquisition/select-source";
    const result = await postJson(endpoint, {
      source_type: sourceType,
      initial_path: controls.simulationSourcePath?.value.trim() || "",
    });
    if (result.selected) {
      controls.simulationSourcePath.value = result.path || result.name || "";
      if (controls.autoProcessParameters?.checked) {
        await readProcessParameters({automatic: true});
      }
      toast("模拟采集数据源已选择");
    }
  } catch (error) { toast(`无法选择模拟数据源：${error.message}`); }
}

function readSimulationFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`无法读取 ${file.name}`));
    reader.onload = () => {
      const value = String(reader.result || "");
      const comma = value.indexOf(",");
      resolve({
        name: file.webkitRelativePath || file.name,
        data: comma >= 0 ? value.slice(comma + 1) : value,
      });
    };
    reader.readAsDataURL(file);
  });
}

async function uploadSimulationSource() {
  const picker = controls.simulationSourceFile;
  const files = [...(picker?.files || [])];
  if (!files.length) return;
  const sourceType = controls.simulationSourceType?.value || "single_csv";
  if (sourceType === "single_csv" && files.length !== 1) {
    toast("单 CSV 模式只能选择一个文件");
    return;
  }
  if (files.some((file) => !file.name.toLowerCase().endsWith(".csv"))) {
    toast("模拟数据只支持 CSV 文件");
    return;
  }
  if (state.accessRole === "guest" && state.acquisitionStatus?.running) {
    toast("当前正在采集，请先停止并保存后再导入模拟数据");
    picker.value = "";
    return;
  }
  const button = controls.selectSimulationSource;
  if (button) button.disabled = true;
  try {
    if (controls.simulationSourceNote) controls.simulationSourceNote.textContent = "正在上传并校验模拟数据……";
    const uploaded = await Promise.all(files.map(readSimulationFile));
    const result = await postJson("/api/simulation/upload-source", {
      source_type: sourceType,
      files: uploaded,
    }, {timeoutMs: 120000});
    controls.simulationSourcePath.value = result.name || result.path || "已导入模拟数据";
    state.simulationSourceChannels = Array.isArray(result.channels) ? result.channels : [];
    if (controls.simulationSourceNote) controls.simulationSourceNote.textContent =
      `已导入 ${result.name || "模拟数据"}；请点击“开始采集”后才开始读取，预测、预警和保存均基于该数据。`;
    state.guestSimulationStarted = false;
    state.guestSimulationStoppedByUser = false;
    document.querySelectorAll(".interface-config-row").forEach((row) => refreshPhysicalInterfaceOptions(row));
    autoEnableSimulationChannels(state.simulationSourceChannels);
    stopLocalSimulationReplay();
    state.simulationDatasetCache = null;
    await loadSimulationDatasetOnce({force: true});
    if (controls.autoProcessParameters?.checked) {
      await readProcessParameters({automatic: true});
    }
    toast("模拟数据已载入");
  } catch (error) {
    if (controls.simulationSourceNote) controls.simulationSourceNote.textContent = `模拟数据上传失败：${error.message}`;
    toast(`无法上传模拟数据：${error.message}`);
  } finally {
    if (button) button.disabled = false;
    picker.value = "";
  }
}

function acquisitionConfig() {
  const newSchema = controls.datasetSchema.value === "new_collection_v11_3";
  const interfaceState = interfaceConfigs();
  validatePhysicalInterfaceBindings(interfaceState.interfaces, controls.acquisitionMode?.value !== "simulation");
  const first = interfaceState.interfaces[0] || {};
  const simulation = controls.acquisitionMode?.value === "simulation";
  return {
    processing_mode: controls.processingMode.value,
    acquisition_mode: simulation ? "simulation" : "real",
    simulation_source_type: controls.simulationSourceType?.value || "single_csv",
    simulation_source_path: simulation ? (controls.simulationSourcePath?.value.trim() || "") : "",
    simulation_mysql_query: controls.simulationMysqlQuery?.value.trim() || "",
    simulation_mysql_host: controls.mysqlHost?.value.trim() || "192.168.101.31",
    simulation_mysql_port: Number(controls.mysqlPort?.value) || 3306,
    simulation_mysql_user: controls.mysqlUser?.value.trim() || "afp_app",
    simulation_mysql_password: controls.mysqlPassword?.value ?? "",
    simulation_mysql_database: controls.mysqlDatabase?.value.trim() || "afp_state_warning",
    dataset_schema: controls.datasetSchema.value || "legacy_original",
    use_best_prediction_override: controls.bestPredictionOverride.checked,
    driver: simulation ? "simulator" : (first.driver || controls.driver.value),
    endpoint: first.endpoint || "",
    baudrate: Number(first.baudrate || controls.baudrate.value) || 115200,
    interfaces: interfaceState.interfaces,
    interface_channel_assignments: interfaceState.assignments,
    sample_rate_hz: Number(controls.sampleRate.value) || 10,
    selected_sensors: selectedLiveSensors(),
    prediction_sensors: selectedPredictionSensors(),
    model_input_sensors: selectedModelInputSensors(),
    model_output_sensors: selectedPredictionSensors(),
    prediction_model_file: controls.predictionModel.value.trim(),
    prediction_model_type: controls.predictionModelType?.value || "i_T_G",
    health_indicator: controls.indicator.value || "TC-HI",
    run_id: controls.runId.value.trim() || "LIVE_RUN",
    specimen_id: controls.liveSpecimen.value.trim() || "LIVE_SPECIMEN",
    condition_id: newSchema ? (controls.conditionId.value.trim() || "H06") : "LIVE",
    layer: Number(newSchema ? controls.newLayer.value : controls.liveLayer.value) || 0,
    cycle: 1,
    p: Number(controls.livePower.value) || 0,
    v: Number(controls.liveSpeed.value) || 0,
    pr: Number(controls.livePressure.value) || 0,
    root: "LIVE",
    source_file: simulation ? (controls.simulationSourcePath?.value.trim() || "") : "",
    save_root: controls.saveRoot.value.trim(),
    mysql_enabled: Boolean(controls.mysqlEnabled?.checked),
    mysql_host: controls.mysqlHost?.value.trim() || "192.168.101.31",
    mysql_port: Number(controls.mysqlPort?.value) || 3306,
    mysql_user: controls.mysqlUser?.value.trim() || "afp_app",
    mysql_password: controls.mysqlPassword?.value ?? "",
    mysql_database: controls.mysqlDatabase?.value.trim() || "afp_state_warning",
    mysql_local_enabled: Boolean(controls.mysqlLocalEnabled?.checked),
    mysql_local_host: controls.mysqlLocalHost?.value.trim() || "127.0.0.1",
    mysql_local_port: Number(controls.mysqlLocalPort?.value) || 3306,
    mysql_local_user: controls.mysqlLocalUser?.value.trim() || "root",
    mysql_local_password: controls.mysqlLocalPassword?.value ?? "",
    mysql_local_database: controls.mysqlLocalDatabase?.value.trim() || "afp_state_warning",
    mysql_charset: "utf8mb4",
    mysql_connect_timeout: 5,
    initial_compaction_force_N: Number(controls.initialForce.value) || 0,
    placement_speed_mm_s: Number(controls.placementSpeed.value) || 0,
    pid_angle_deg: Number(controls.pidAngle.value) || 0,
    temperature_setpoint_C: Number(controls.temperatureSetpoint.value) || 0,
    replicate: Number(controls.replicate.value) || 1,
  };
}

function renderProcessParameterReadStatus(summary, result) {
  const status = controls.processParameterReadStatus;
  if (!status) return;
  const details = Array.isArray(summary?.details) ? summary.details : [];
  status.textContent = summary?.message || "工艺参数读取完成";
  status.title = details.join("\n");
  status.classList.toggle("ok", Boolean(summary?.updated));
  status.classList.toggle("error", !summary?.updated);
  if (result?.complete) {
    status.textContent += "。四项工艺参数均已读取。";
  } else if (summary?.updated) {
    status.textContent += "。将鼠标移到此处可查看各项来源或失败原因。";
  }
}

async function readProcessParameters({automatic = false} = {}) {
  if (state.processParameterBusy) return false;
  if (controls.datasetSchema?.value !== "new_collection_v11_3") return false;
  const status = controls.processParameterReadStatus;
  const button = controls.readProcessParameters;
  state.processParameterBusy = true;
  if (button) button.disabled = true;
  if (status) {
    status.classList.remove("ok", "error");
    status.textContent = automatic
      ? "开始采集前正在刷新工艺参数……"
      : "正在读取工艺参数……";
  }
  try {
    const config = acquisitionConfig();
    const simulation = config.acquisition_mode === "simulation";
    let result;
    if (simulation && state.accessRole === "guest") {
      result = await postJson(
        "/api/simulation/process-parameters", config, {timeoutMs: 20000}
      );
    } else if (!simulation && usesLocalCaptureHelper()) {
      result = await requestLocalHelper(
        "read_process_parameters", config, {timeoutMs: 20000}
      );
    } else {
      result = await postJson(
        "/api/acquisition/process-parameters", config, {timeoutMs: 20000}
      );
    }
    const summary = window.ProcessParameterReader?.applyResult(result, controls);
    if (!summary) throw new Error("工艺参数读取组件未加载");
    renderProcessParameterReadStatus(summary, result);
    markLiveScopeChanged();
    return Boolean(summary.updated);
  } catch (error) {
    if (status) {
      status.classList.remove("ok");
      status.classList.add("error");
      status.textContent = `工艺参数读取失败：${error.message || error}；现有输入值已保留。`;
      status.title = status.textContent;
    }
    if (!automatic) toast(error.message || String(error));
    return false;
  } finally {
    state.processParameterBusy = false;
    if (button) button.disabled = false;
  }
}

function validatePhysicalInterfaceBindings(items, realMode) {
  if (!realMode) return;
  const seen = new Map();
  for (const item of items.filter((entry) => entry.enabled)) {
    const profile = sensorTypeProfile(item.role);
    if (!item.physical_interface_id) {
      throw new Error(`接口“${item.id}”未选择实际物理接口，不能启用`);
    }
    const expectedKind = profile.physical_kind || "";
    if (item.physical_interface_kind && expectedKind && item.physical_interface_kind !== expectedKind && !item.physical_fallback) {
      throw new Error(`接口“${item.id}”的物理接口类型与协议不匹配`);
    }
    const previous = seen.get(item.physical_interface_id);
    if (!previous) { seen.set(item.physical_interface_id, item); continue; }
    const shared = new Set([previous.role, item.role]);
    if (!(shared.has("plc") && shared.has("robot") && shared.size === 2 && expectedKind === "ethernet" && previous.physical_interface_kind === "ethernet")) {
      throw new Error(`物理接口“${item.physical_interface_id}”重复绑定；仅允许PLC与ABB共享同一网卡`);
    }
  }
}

controls.acquisitionMode?.addEventListener("change", () => {
  updateSimulationSettings();
  markHardwareCheckStale("采集模式已变化");
  if (controls.autoProcessParameters?.checked) {
    readProcessParameters({automatic: true});
  }
});
controls.simulationSourceType?.addEventListener("change", updateSimulationSettings);
controls.selectSimulationSource?.addEventListener("click", selectSimulationSource);
controls.simulationSourceFile?.addEventListener("change", uploadSimulationSource);
controls.integrationSourceType?.addEventListener("change", updateIntegrationSource);
controls.selectIntegrationFolder?.addEventListener("click", selectIntegrationFolder);
controls.runIntegration?.addEventListener("click", runIntegration);
updateIntegrationSource();
updateSimulationSettings();

// Keep the interface-side channel list aligned with the acquisition checklist.
function selectedAcquisitionChannelsForInterfaces() {
  const checks = [...document.querySelectorAll("#liveSensorChecklist .save-sensor-checkbox")];
  if (!checks.length) {
    const schemaId = controls.datasetSchema?.value || "legacy_original";
    return state.bootstrap?.acquisition?.schemas?.find((item) => item.id === schemaId)?.sensors || [];
  }
  return checks.filter((node) => node.checked).map((node) => node.value);
}

// In simulation mode the imported file is the source of truth for which
// sensor channels are available.  Match those channels to the collection
// checklist once a source is loaded; users can still manually uncheck any
// channel afterwards.
function autoEnableSimulationChannels(sourceChannels = []) {
  if (controls.acquisitionMode?.value !== "simulation") return;
  const available = new Set((Array.isArray(sourceChannels) ? sourceChannels : [])
    .map((channel) => String(channel || "").trim())
    .filter(Boolean));
  const rows = [...document.querySelectorAll("#liveSensorChecklist .sensor-checklist-row")];
  if (!rows.length) return;
  rows.forEach((row) => {
    const collect = row.querySelector(".save-sensor-checkbox");
    if (!collect) return;
    const present = available.has(String(collect.value || "").trim());
    collect.checked = present;
    const modelInput = row.querySelector(".model-input-sensor-checkbox");
    const outputInput = row.querySelector(".predict-sensor-checkbox");
    if (!present) {
      if (modelInput) modelInput.checked = false;
      if (outputInput) outputInput.checked = false;
    }
    const captureOnly = controls.processingMode?.value === "capture_only";
    if (modelInput) modelInput.disabled = !present || captureOnly;
    if (outputInput) outputInput.disabled = !present || captureOnly;
  });
  refreshInterfaceCardsForSelection();
}

function isTemperatureChannel(channel) {
  return /^温度[1-8]$/.test(String(channel || ""));
}

function interfaceAcceptsChannel(item, channel) {
  if (!item) return false;
  const profile = sensorTypeProfile(item.role);
  return Boolean(profile.editable_driver)
    || (profile.channels || []).includes(channel);
}

function preferredInterfaceForChannel(channel, excludeId = "") {
  const items = currentInterfaceItems().filter((item) => item.id !== excludeId);
  return items.find((item) => interfaceAcceptsChannel(item, channel)) || null;
}

function createInterfaceChannelEditor(row, interfaceId) {
  const editor = document.createElement("div");
  editor.className = "interface-channel-editor";
  const role = row.querySelector(".interface-role")?.value || "custom";
  const profile = sensorTypeProfile(role);
  const channels = selectedAcquisitionChannelsForInterfaces().filter(
    (channel) => Boolean(profile.editable_driver)
      || (profile.channels || []).includes(channel)
  );
  channels.forEach((channel) => {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.dataset.channel = channel;
    input.dataset.interface = interfaceId;
    input.addEventListener("change", () => {
      const route = document.querySelector(`.interface-route-select[data-channel="${CSS.escape(channel)}"]`);
      if (!route) return;
      if (input.checked) route.value = interfaceId;
      else if (route.value === interfaceId) route.value = preferredInterfaceForChannel(channel)?.id || "__unassigned__";
      syncInterfaceSummaries();
    });
    label.append(input, document.createTextNode(channel));
    editor.append(label);
  });
  row.append(editor);
}

function refreshInterfaceEditors() {
  const assigned = {};
  document.querySelectorAll(".interface-route-select").forEach((select) => {
    assigned[select.dataset.channel] = select.value;
  });
  document.querySelectorAll(".interface-channel-editor input").forEach((input) => {
    input.checked = assigned[input.dataset.channel] === input.dataset.interface;
  });
}

function rebuildInterfaceEditors() {
  document.querySelectorAll(".interface-config-row").forEach((row) => {
    row.querySelector(".interface-channel-editor")?.remove();
    createInterfaceChannelEditor(row, row.dataset.interfaceId);
  });
  refreshInterfaceEditors();
}

function syncInterfaceSummaries() {
  const grouped = {};
  document.querySelectorAll(".interface-route-select").forEach((select) => {
    if (select.value && select.value !== "__unassigned__") (grouped[select.value] ||= []).push(select.dataset.channel);
  });
  document.querySelectorAll(".interface-config-row").forEach((row) => {
    const summary = row.querySelector(".interface-channel-summary");
    if (summary) summary.textContent = `通道：${(grouped[row.dataset.interfaceId] || []).join("、") || "未分配"}`;
  });
  refreshInterfaceEditors();
}

function refreshChannelInterfaceOptions() {
  const items = currentInterfaceItems();
  const selectedChannels = new Set(selectedAcquisitionChannelsForInterfaces());
  document.querySelectorAll(".interface-route-select").forEach((select) => {
    const previous = select.value;
    const channel = select.dataset.channel || "";
    if (!selectedChannels.has(channel)) {
      // Keep the row visible for editing, but make the state explicit: this
      // channel is not acquired and cannot be routed to an interface.
      const option = document.createElement("option");
      option.value = "__unassigned__";
      option.textContent = "未采集";
      select.replaceChildren(option);
      select.value = "__unassigned__";
      select.disabled = true;
      return;
    }
    select.disabled = false;
    const compatible = items.filter((item) => interfaceAcceptsChannel(item, channel));
    select.replaceChildren(...[
      {value: "__unassigned__", text: "未分配"},
      ...compatible.map((item) => ({
        value: item.id,
        text: `${item.endpoint || item.id} · ${sensorTypeProfile(item.role).label}`,
      })),
    ].map((item) => {
      const option = document.createElement("option"); option.value = item.value; option.textContent = item.text; return option;
    }));
    const previousItem = compatible.find((item) => item.id === previous);
    const preferred = compatible[0];
    select.value = previousItem && interfaceAcceptsChannel(previousItem, channel)
      ? previous
      : (preferred?.id || "__unassigned__");
    select.onchange = syncInterfaceSummaries;
  });
  syncInterfaceSummaries();
  refreshInterfaceEndpointOptions();
}

function applyRoleChannelDefaults(row) {
  const id = row?.dataset.interfaceId;
  const item = currentInterfaceItems().find((candidate) => candidate.id === id);
  if (!item) return;
  const selected = new Set(selectedAcquisitionChannelsForInterfaces());
  document.querySelectorAll(".interface-route-select").forEach((select) => {
    const channel = select.dataset.channel;
    if (!selected.has(channel)) return;
    if (interfaceAcceptsChannel(item, channel)) select.value = id;
    else if (select.value === id) select.value = preferredInterfaceForChannel(channel, id)?.id || "__unassigned__";
  });
}

function refreshInterfaceCardsForSelection() {
  rebuildInterfaceEditors();
  refreshChannelInterfaceOptions();
}

document.querySelector("#liveSensorChecklist")?.addEventListener("change", (event) => {
  if (event.target?.classList?.contains("save-sensor-checkbox")) refreshInterfaceCardsForSelection();
  markHardwareCheckStale("传感器通道选择已变化");
});
controls.interfacePanel?.addEventListener("change", (event) => {
  if (event.target?.classList?.contains("interface-role")) {
    applyRoleChannelDefaults(event.target.closest(".interface-config-row"));
    rebuildInterfaceEditors();
    refreshChannelInterfaceOptions();
  }
  markHardwareCheckStale("接口或通道映射已变化");
});

function hardwareConfigFingerprint() {
  try {
    const config = acquisitionConfig();
    return JSON.stringify({
      acquisition_mode: config.acquisition_mode,
      dataset_schema: config.dataset_schema,
      selected_sensors: [...(config.selected_sensors || [])].sort(),
      interfaces: (config.interfaces || []).map((item) => ({
        id: item.id, enabled: item.enabled, role: item.role,
        driver: item.driver, endpoint: item.endpoint, baudrate: item.baudrate,
        physical_interface_id: item.physical_interface_id,
        physical_interface_kind: item.physical_interface_kind,
      })),
      assignments: config.interface_channel_assignments || {},
      source_file: config.source_file || "",
    });
  } catch (_error) {
    return "";
  }
}

function hardwareStateLabel(value) {
  return ({
    ok: "正常", disabled: "已停用", video_only: "仅视频",
    no_channels: "无已选通道", waiting: "等待数据",
    not_connected: "未连接", no_data: "没有采集数据",
    identity_unconfirmed: "目标设备身份未确认",
    endpoint_unreachable: "目标网络端点不可达",
    open_failed: "接口无法打开或读取",
    invalid_protocol: "协议数据无效",
    invalid_data: "采集数据无效", partial: "部分通道异常", partial_data: "部分通道异常",
    stale: "数据已中断", not_selected: "未选择",
  })[value] || "异常";
}

function clearHardwareRowStates() {
  document.querySelectorAll(".interface-config-row, .sensor-checklist-row").forEach((row) => {
    row.classList.remove("check-ok", "check-error", "check-waiting");
    row.removeAttribute("data-check-state");
  });
}

function renderHardwareCheckResult(result, {automatic = false, live = false} = {}) {
  const node = controls.hardwareCheckStatus;
  if (!node) return;
  clearHardwareRowStates();
  const interfaces = Array.isArray(result?.interfaces) ? result.interfaces : [];
  const sensors = (Array.isArray(result?.sensors) ? result.sensors : [])
    .filter((item) => item.selected);
  const badInterfaces = interfaces.filter((item) => item.enabled !== false && !item.ok);
  const fallbackInterfaces = interfaces.filter((item) => item.enabled !== false && item.physical_warning);
  const badSensors = sensors.filter((item) => !item.ok && item.blocking !== false);
  const nonBlockingMissingSensors = sensors.filter((item) => !item.ok && item.blocking === false);
  const waiting = [...interfaces, ...sensors].some((item) => item.state === "waiting");
  const ok = Boolean(result?.ok ?? (!badInterfaces.length && !badSensors.length && !waiting));
  node.className = `hardware-check-status ${waiting ? "checking" : ok ? "ok" : "error"}`;

  const title = document.createElement("strong");
  if (waiting) title.textContent = "正在等待各通道首个数据";
  else if (ok) title.textContent = `${live ? "持续监控" : automatic ? "自动检查" : "手动检查"}通过`;
  else title.textContent = `${live ? "持续监控发现异常" : automatic ? "自动检查未通过" : "手动检查未通过"}`;
  node.replaceChildren(title);

  const summary = document.createElement("div");
  summary.textContent = `接口 ${interfaces.filter((item) => item.ok).length}/${interfaces.length} 正常；通道 ${sensors.filter((item) => item.ok).length}/${sensors.length} 正常`;
  node.appendChild(summary);

  const appendDetails = (label, items, formatter) => {
    if (!items.length) return;
    const detail = document.createElement("div");
    detail.className = "hardware-check-detail";
    detail.textContent = `${label}：${items.map(formatter).join("；")}`;
    node.appendChild(detail);
  };
  appendDetails("异常接口", badInterfaces, (item) => {
    const profile = sensorTypeProfile(item.role || "custom");
    const physical = item.physical_interface_id ? ` · 实际${item.physical_interface_id}` : "";
    return `${profile.label || item.role || "接口"} ${item.endpoint || item.id || "未填写地址"}${physical}（${item.message || hardwareStateLabel(item.state)}）`;
  });
  appendDetails("接口绑定警告", fallbackInterfaces, (item) => `${item.id || "接口"}：${item.physical_warning}`);
  appendDetails("异常通道", badSensors, (item) => `${item.name}（${item.message || hardwareStateLabel(item.state)}）`);
  appendDetails(
    "未采集通道（不阻止启动）",
    nonBlockingMissingSensors,
    (item) => `${item.name}（${item.message || hardwareStateLabel(item.state)}）`,
  );
  if (
    Array.isArray(result?.errors)
    && result.errors.length
    && !badInterfaces.length
    && !badSensors.length
  ) {
    appendDetails("检查信息", result.errors, (item) => String(item));
  }
  const timeNode = document.createElement("small");
  timeNode.textContent = `${live ? "实时更新" : `检查用时 ${Number(result?.elapsed_seconds || 0).toFixed(1)} 秒`} · ${new Date().toLocaleTimeString()}`;
  node.appendChild(timeNode);
  appendAgentDiagnostics(node);

  interfaces.forEach((item) => {
    const row = [...document.querySelectorAll(".interface-config-row")]
      .find((candidate) => candidate.dataset.interfaceId === String(item.id || ""));
    if (!row) return;
    const className = item.state === "waiting" || (!item.ok && item.blocking === false)
      ? "check-waiting"
      : item.ok ? "check-ok" : "check-error";
    row.classList.add(className);
    row.dataset.checkState = hardwareStateLabel(item.state);
    row.title = item.message || hardwareStateLabel(item.state);
  });
  sensors.forEach((item) => {
    const row = [...document.querySelectorAll("#liveSensorChecklist .sensor-checklist-row")]
      .find((candidate) => candidate.querySelector(".save-sensor-checkbox")?.value === item.name);
    if (!row) return;
    const className = item.state === "waiting" ? "check-waiting" : item.ok ? "check-ok" : "check-error";
    row.classList.add(className);
    row.dataset.checkState = hardwareStateLabel(item.state);
    row.title = item.message || hardwareStateLabel(item.state);
  });
}

function markHardwareCheckStale(reason = "配置已变化") {
  state.agentRequestId += 1;
  state.agentController?.abort();
  state.agentController = null;
  state.agentBusy = false;
  state.agentJobId = "";
  state.agentEvents = [];
  state.agentResult = null;
  state.agentFingerprint = "";
  state.hardwareCheck = null;
  state.hardwareCheckFingerprint = "";
  clearHardwareRowStates();
  const node = controls.hardwareCheckStatus;
  if (node) {
    node.className = "hardware-check-status stale";
    node.textContent = `${reason}，需要重新检查接口和传感器通道。`;
  }
}

function scheduleAutomaticHardwareCheck(delay = 700) {
  if (!controls.autoHardwareCheck?.checked || state.hardwareCheckInProgress) return;
  if (state.acquisitionStatus?.running) return;
  if (state.hardwareCheckTimer) window.clearTimeout(state.hardwareCheckTimer);
  state.hardwareCheckTimer = window.setTimeout(() => {
    state.hardwareCheckTimer = null;
    testSensorConnection({automatic: true});
  }, Math.max(0, delay));
}

async function testSensorConnection({automatic = false} = {}) {
  if (state.hardwareCheckInProgress) return state.hardwareCheck;
  if (state.acquisitionStatus?.running) {
    toast("采集运行中正在持续监控，无需另开接口检查");
    return null;
  }
  if (state.accessRole === "guest" && controls.acquisitionMode?.value === "simulation") {
    const sourceChannels = new Set(state.simulationSourceChannels || []);
    const selected = selectedAcquisitionChannelsForInterfaces();
    const sensors = selected.map((name) => ({
      name,
      selected: true,
      state: sourceChannels.has(name) ? "ok" : "no_data",
      message: sourceChannels.has(name) ? "模拟数据已匹配" : "模拟数据未包含该通道",
      observed_samples: 0,
      received_samples: 0,
      ok: sourceChannels.has(name),
      blocking: false,
    }));
    const interfaces = [...(controls.interfacePanel?.querySelectorAll(".interface-config-row") || [])].map((row) => {
      const role = row.querySelector(".interface-role")?.value || "custom";
      const profile = sensorTypeProfile(role);
      const expected = (profile.channels || []).filter((name) => selected.includes(name));
      const matched = expected.filter((name) => sourceChannels.has(name));
      return {
        id: row.dataset.interfaceId,
        role,
        driver: "simulator",
        endpoint: "模拟数据源",
        enabled: row.querySelector(".interface-enabled")?.checked !== false,
        expected_channels: expected,
        detected_channels: matched,
        missing_channels: expected.filter((name) => !sourceChannels.has(name)),
        state: matched.length || !expected.length ? "ok" : "no_data",
        message: matched.length ? `已匹配 ${matched.join("、")}` : "模拟数据未包含该接口通道",
        ok: Boolean(matched.length || !expected.length),
      };
    });
    const result = {ok: sensors.every((item) => item.ok), sensors, interfaces, errors: []};
    state.hardwareCheck = result;
    state.hardwareCheckFingerprint = hardwareConfigFingerprint();
    renderHardwareCheckResult(result, {automatic});
    updateAgentFromHardwareResult(result, {automatic});
    return result;
  }
  if (usesLocalCaptureHelper() && controls.acquisitionMode?.value !== "simulation") {
    state.hardwareCheckInProgress = true;
    const node = controls.hardwareCheckStatus;
    const button = $("testSensorsButton");
    if (button) button.disabled = true;
    if (node) {
      node.className = "hardware-check-status checking";
      node.textContent = `${automatic ? "正在自动检查" : "正在检查"}访问者电脑上的接口和传感器…`;
    }
    try {
      // A real-interface probe may have to wait for serial/USB/PLC/ABB
      // drivers to time out one by one.  The helper keeps its heartbeat on a
      // separate thread, so allow the command enough time to finish instead
      // of reporting a client-side timeout while the check is still running.
      const result = await requestLocalHelper("check_capture", acquisitionConfig(), {timeoutMs: 120000});
      state.hardwareCheck = result;
      state.hardwareCheckFingerprint = hardwareConfigFingerprint();
      renderHardwareCheckResult(result, {automatic});
      updateAgentFromHardwareResult(result, {automatic});
      return result;
    } catch (error) {
      if (node) {
        node.className = "hardware-check-status error";
        node.textContent = `本机辅助程序检查失败：${error.message}`;
      }
      if (!automatic) toast(error.message);
      return null;
    } finally {
      state.hardwareCheckInProgress = false;
      if (button) button.disabled = false;
    }
  }
  state.hardwareCheckInProgress = true;
  const node = controls.hardwareCheckStatus;
  const button = $("testSensorsButton");
  const resetButton = controls.resetSensorCheck;
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  state.hardwareCheckController = controller;
  if (button) button.disabled = true;
  // Keep reset available so an operator can cancel a driver that stopped
  // responding instead of waiting for the timeout.
  if (resetButton) resetButton.disabled = false;
  if (node) {
    node.className = "hardware-check-status checking";
    node.textContent = `${automatic ? "正在自动检查" : "正在检查"}，将逐一读取每个接口的全部已选通道…`;
  }
  try {
    const result = await postJson(
      state.accessRole === "guest" ? "/api/simulation/start" : "/api/acquisition/test",
      acquisitionConfig(),
      {timeoutMs: 20000, controller},
    );
    if (state.accessRole === "guest") {
      state.guestSimulationStarted = true;
      state.guestSimulationStoppedByUser = false;
    }
    if (controls.processingMode.value !== "capture_only") {
      applyPredictionModelProfile(result.prediction_model, false);
    }
    state.hardwareCheck = result;
    state.hardwareCheckFingerprint = hardwareConfigFingerprint();
    renderHardwareCheckResult(result, {automatic});
    updateAgentFromHardwareResult(result, {automatic});
    if (!result.ok && !automatic) toast("检查发现接口或传感器通道异常，详情已列出");
    return result;
  } catch (error) {
    state.hardwareCheck = null;
    state.hardwareCheckFingerprint = "";
    if (node) {
      node.className = "hardware-check-status error";
      node.textContent = `检查失败：${error.message}`;
    }
    if (!automatic) toast(error.message);
    return null;
  } finally {
    state.hardwareCheckInProgress = false;
    if (state.hardwareCheckController === controller) state.hardwareCheckController = null;
    if (button) button.disabled = false;
    if (resetButton) resetButton.disabled = false;
  }
}

async function resetAndCheckHardware() {
  if (state.acquisitionStatus?.running) {
    toast("请先停止并保存当前采集，再重置检查状态");
    return;
  }
  state.agentRequestId += 1;
  state.agentController?.abort();
  state.agentController = null;
  state.agentBusy = false;
  state.agentEvents = [];
  state.agentResult = null;
  state.agentFingerprint = "";
  if (state.hardwareCheckInProgress) {
    state.hardwareCheckController?.abort();
    const deadline = Date.now() + 1000;
    while (state.hardwareCheckInProgress && Date.now() < deadline) {
      await new Promise((resolve) => window.setTimeout(resolve, 20));
    }
    }
    try {
      if (state.accessRole !== "guest") {
        await acquireRealControl();
        if (!usesLocalCaptureHelper()) {
          await postJson("/api/acquisition/reset-check", {});
        }
    } else {
      state.guestSimulationStarted = false;
      state.guestSimulationStoppedByUser = false;
    }
    state.hardwareCheck = null;
    state.hardwareCheckFingerprint = "";
    clearHardwareRowStates();
    if (controls.hardwareCheckStatus) {
      controls.hardwareCheckStatus.className = "hardware-check-status checking";
      controls.hardwareCheckStatus.textContent = "检查状态已重置，正在按当前接口地址重新识别数据…";
    }
    await testSensorConnection({automatic: false});
  } catch (error) {
    toast(error.message);
  }
}

function renderLiveHardwareMonitor(status) {
  const selected = (status.sensors || []).filter((item) => item.selected);
  const interfaces = status.interfaces || [];
  const result = {
    ok: !status.last_error
      && interfaces.every((item) => item.ok || item.state === "waiting"),
    sensors: selected,
    interfaces,
    errors: status.last_error ? [status.last_error] : [],
  };
  renderHardwareCheckResult(result, {live: true});
}
