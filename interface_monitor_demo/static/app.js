"use strict";

const state = {
  bootstrap: null,
  snapshot: null,
  activeEvent: null,
  diagnosing: false,
};

const $ = (id) => document.getElementById(id);
const apiKeyInput = $("apiKeyInput");
const modelNameInput = $("modelNameInput");
const diagnoseButton = $("diagnoseButton");

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function getJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "请求失败");
  return payload;
}

async function postJson(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "请求失败");
  return result;
}

function toast(message, error = false) {
  const node = $("toast");
  node.textContent = message;
  node.classList.toggle("error", error);
  node.classList.add("show");
  window.setTimeout(() => node.classList.remove("show"), 2400);
}

function stateLabel(item) {
  const labels = {
    healthy: "运行正常",
    not_found: "接口未发现",
    open_failed: "打开失败",
    timeout: "通信超时",
    stale: "数据过期",
    invalid_data: "数据异常",
    partial_channels: "通道缺失",
  };
  return labels[item.state] || item.status_label || item.state;
}

function ageLabel(value) {
  if (value === null || value === undefined) return "—";
  if (value >= 1000) return `${(value / 1000).toFixed(1)} s`;
  return `${value} ms`;
}

function renderInterfaces(interfaces) {
  const healthy = interfaces.filter((item) => item.state === "healthy").length;
  $("healthyCount").textContent = `${healthy} / ${interfaces.length}`;
  $("eventCount").textContent = String(state.snapshot?.events?.length || 0);
  $("interfaceGrid").innerHTML = interfaces.map((item) => {
    const className = item.severity === "critical" ? "critical" : item.severity === "warning" ? "warning" : "healthy";
    const missing = item.missing_channels?.length ? `缺失 ${item.missing_channels.join("、")}` : `${item.received_samples || 0} samples`;
    return `
      <article class="interface-card ${className}" data-interface-id="${escapeHtml(item.id)}">
        <div class="interface-top">
          <h3 class="interface-name">${escapeHtml(item.label)}</h3>
          <span class="status-chip">${escapeHtml(stateLabel(item))}</span>
        </div>
        <p class="interface-meta">${escapeHtml(item.protocol)} · ${escapeHtml(item.endpoint)}</p>
        <div class="channel-list">${item.channels.map((channel) => `<span>${escapeHtml(channel)}</span>`).join("")}</div>
        <div class="interface-foot"><span>${escapeHtml(missing)}</span><span>${ageLabel(item.last_sample_age_ms)}</span></div>
      </article>`;
  }).join("");
}

function updateGate() {
  const keyPresent = Boolean(apiKeyInput.value.trim());
  const modelPresent = Boolean(modelNameInput.value.trim());
  const eventPresent = Boolean(state.activeEvent);
  const ready = keyPresent && modelPresent && eventPresent && !state.diagnosing;
  diagnoseButton.disabled = !ready;
  const label = $("agentEnabledState");
  label.classList.toggle("ready", ready);
  label.classList.toggle("off", !ready);
  if (!keyPresent || !modelPresent) label.textContent = "等待配置";
  else if (!eventPresent) label.textContent = "等待异常";
  else if (state.diagnosing) label.textContent = "诊断中";
  else label.textContent = "可以诊断";
}

function updateScenarioOptions() {
  const selected = $("scenarioInterface").value;
  const parseOption = [...$("scenarioType").options].find((option) => option.value === "parse_error");
  if (parseOption) {
    parseOption.disabled = selected !== "m3232_pressure";
    if (parseOption.disabled && $("scenarioType").value === "parse_error") {
      $("scenarioType").value = "timeout";
    }
  }
}

function resetFlow() {
  document.querySelectorAll(".flow-node").forEach((node) => node.classList.remove("active", "done"));
  $("toolTrace").className = "tool-trace empty-state";
  $("toolTrace").textContent = "运行诊断后，这里会按实际顺序显示 LangChain 本地工具调用。";
  $("diagnosisPanel").className = "diagnosis-panel empty-state";
  $("diagnosisPanel").textContent = "当前没有诊断结果。模拟结果只用于演示流程，不代表真实硬件故障概率。";
  $("diagnosisStatus").className = "result-state";
  $("diagnosisStatus").textContent = "等待诊断";
}

function markScenarioFlow() {
  $("flow-monitor").classList.add("done");
  $("flow-event").classList.add("active");
}

function traceToFlow(stepId) {
  if (stepId === "event_received") return "flow-event";
  if (stepId === "gate_checked") return "flow-gate";
  if (["get_interface_config", "inspect_latest_observation", "lookup_fault_rule", "compose_diagnostic_report"].includes(stepId)) return "flow-langchain";
  if (stepId === "complete") return "flow-report";
  return "flow-monitor";
}

function renderTrace(steps, visibleCount) {
  const shown = steps.slice(0, visibleCount);
  $("toolTrace").className = "tool-trace";
  $("toolTrace").innerHTML = `<div class="trace-list">${shown.map((step, index) => `
    <div class="trace-step">
      <span>${index + 1}</span>
      <div><b>${escapeHtml(step.title)}</b><small>${escapeHtml(step.detail)}</small></div>
    </div>`).join("")}</div>`;
}

function renderDiagnosis(result) {
  const diagnosis = result.diagnosis;
  const evidence = Object.entries(diagnosis.evidence)
    .filter(([, value]) => value !== null && value !== undefined && JSON.stringify(value) !== "[]")
    .map(([key, value]) => `${escapeHtml(key)}: ${escapeHtml(Array.isArray(value) ? value.join("、") : value)}`)
    .join("<br>");
  $("diagnosisPanel").className = "diagnosis-panel";
  $("diagnosisPanel").innerHTML = `
    <div class="diagnosis-content">
      <div class="diagnosis-title"><strong>${escapeHtml(diagnosis.fault_type)}</strong><span>${escapeHtml(diagnosis.confidence_label)}</span></div>
      <div class="diagnosis-grid">
        <div class="diagnosis-block"><h3>定位</h3><p>${escapeHtml(diagnosis.sensor_name)}<br>${escapeHtml(diagnosis.channels.join("、"))}</p></div>
        <div class="diagnosis-block"><h3>证据</h3><p>${evidence || "当前事件未提供额外证据"}</p></div>
        <div class="diagnosis-block"><h3>可能原因</h3><ul>${diagnosis.possible_causes.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>
        <div class="diagnosis-block"><h3>处理建议</h3><ul>${diagnosis.recommended_actions.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>
      </div>
      <div class="boundary">${escapeHtml(diagnosis.evidence_boundary)} · 模型标签：${escapeHtml(result.model_name)}</div>
    </div>`;
  $("diagnosisStatus").className = "result-state ready";
  $("diagnosisStatus").textContent = "本地模拟完成";
}

async function animateDiagnosis(result) {
  const steps = result.trace || [];
  const touched = new Set(["flow-monitor"]);
  $("flow-monitor").classList.add("done");
  for (let index = 0; index < steps.length; index += 1) {
    const nodeId = traceToFlow(steps[index].id);
    document.querySelectorAll(".flow-node.active").forEach((node) => node.classList.remove("active"));
    touched.forEach((id) => $(id).classList.add("done"));
    $(nodeId).classList.add("active");
    touched.add(nodeId);
    renderTrace(steps, index + 1);
    await new Promise((resolve) => window.setTimeout(resolve, 210));
  }
  document.querySelectorAll(".flow-node.active").forEach((node) => node.classList.remove("active"));
  touched.forEach((id) => $(id).classList.add("done"));
  renderDiagnosis(result);
}

async function refreshStatus() {
  state.snapshot = await getJson("/api/status");
  state.activeEvent = state.snapshot.active_event;
  renderInterfaces(state.snapshot.interfaces);
  updateGate();
}

async function injectScenario() {
  try {
    const result = await postJson("/api/scenario", {
      interface_id: $("scenarioInterface").value,
      scenario: $("scenarioType").value,
    });
    await refreshStatus();
    resetFlow();
    if (result.event_id) {
      state.activeEvent = result;
      markScenarioFlow();
      toast(`已注入：${result.sensor_name} · ${result.summary}`);
    } else {
      toast("目标接口已恢复正常");
    }
    updateGate();
  } catch (error) {
    toast(error.message, true);
  }
}

async function resetAll() {
  try {
    state.snapshot = await postJson("/api/reset", {});
    state.activeEvent = null;
    renderInterfaces(state.snapshot.interfaces);
    resetFlow();
    updateGate();
    toast("全部接口已恢复为模拟正常状态");
  } catch (error) {
    toast(error.message, true);
  }
}

async function runDiagnosis() {
  if (!state.activeEvent) return;
  state.diagnosing = true;
  updateGate();
  try {
    const result = await postJson("/api/diagnose", {
      api_key_present: Boolean(apiKeyInput.value.trim()),
      model_name: modelNameInput.value.trim(),
      event_id: state.activeEvent.event_id,
    });
    await animateDiagnosis(result);
    toast("LangChain 本地诊断流程已完成");
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.diagnosing = false;
    updateGate();
  }
}

async function initialize() {
  apiKeyInput.value = "";
  modelNameInput.value = "";
  state.bootstrap = await getJson("/api/bootstrap");
  state.snapshot = state.bootstrap;
  state.activeEvent = null;
  $("scenarioInterface").replaceChildren(...state.bootstrap.interfaces.map((item) => {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = `${item.label} · ${item.endpoint}`;
    return option;
  }));
  $("scenarioType").replaceChildren(...state.bootstrap.scenarios.filter((item) => item.id !== "healthy").map((item) => {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.label;
    return option;
  }));
  renderInterfaces(state.bootstrap.interfaces);
  updateScenarioOptions();
  resetFlow();
  updateGate();
}

apiKeyInput.addEventListener("input", updateGate);
modelNameInput.addEventListener("input", updateGate);
$("scenarioInterface").addEventListener("change", updateScenarioOptions);
$("injectScenarioButton").addEventListener("click", injectScenario);
$("resetButton").addEventListener("click", resetAll);
diagnoseButton.addEventListener("click", runDiagnosis);

initialize().catch((error) => toast(`页面初始化失败：${error.message}`, true));
