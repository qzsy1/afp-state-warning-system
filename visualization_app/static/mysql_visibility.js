(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.MysqlVisibility = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function syncTargetMysqlVisibility(checkbox, details) {
    if (!details) return false;
    const visible = Boolean(checkbox && checkbox.checked);
    details.hidden = !visible;
    details.setAttribute("aria-hidden", visible ? "false" : "true");
    return visible;
  }

  return {syncTargetMysqlVisibility};
}));
