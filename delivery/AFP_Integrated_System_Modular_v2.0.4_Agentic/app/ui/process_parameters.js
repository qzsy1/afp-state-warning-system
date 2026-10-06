(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.ProcessParameterReader = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const CONTROL_KEYS = {
    initial_compaction_force_N: "initialForce",
    placement_speed_mm_s: "placementSpeed",
    pid_angle_deg: "pidAngle",
    temperature_setpoint_C: "temperatureSetpoint",
  };

  function applyResult(result, controls) {
    const parameters = Array.isArray(result && result.parameters)
      ? result.parameters : [];
    const values = result && typeof result.values === "object"
      ? result.values : {};
    let updated = 0;
    let failed = 0;
    const details = [];

    parameters.forEach((item) => {
      const key = String(item && item.key || "");
      const control = controls && controls[CONTROL_KEYS[key]];
      const value = Number(values[key]);
      if (item && item.ok && control && Number.isFinite(value)) {
        control.value = String(value);
        updated += 1;
        details.push(`${item.label || key}=${value}${item.unit || ""}（${item.source || "已读取"}）`);
      } else {
        failed += 1;
        details.push(`${item && item.label || key || "未知参数"}：${item && item.message || "读取结果无效"}`);
      }
    });

    const message = updated
      ? `已自动更新 ${updated} 项${failed ? `；${failed} 项读取失败，保留原值` : "，全部读取成功"}`
      : `未读取到有效工艺参数${failed ? "；现有输入值已保留" : ""}`;
    return {updated, failed, message, details};
  }

  return {applyResult};
}));
