from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def test_target_mysql_details_follow_target_save_checkbox() -> None:
    """Catches a target-detail panel that remains visible while unchecked."""
    module_path = ROOT / "static" / "mysql_visibility.js"
    script = r"""
const visibility = require(process.argv[1]);
const details = {
  hidden: false,
  attributes: {},
  setAttribute(name, value) { this.attributes[name] = String(value); }
};
const checkbox = {checked: false};
visibility.syncTargetMysqlVisibility(checkbox, details);
const unchecked = {hidden: details.hidden, ariaHidden: details.attributes["aria-hidden"]};
checkbox.checked = true;
visibility.syncTargetMysqlVisibility(checkbox, details);
const checked = {hidden: details.hidden, ariaHidden: details.attributes["aria-hidden"]};
console.log(JSON.stringify({unchecked, checked}));
"""
    completed = subprocess.run(
        ["node", "-e", script, str(module_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "unchecked": {"hidden": True, "ariaHidden": "true"},
        "checked": {"hidden": False, "ariaHidden": "false"},
    }


def test_local_mysql_details_follow_local_save_checkbox() -> None:
    """Catches a local-detail panel that remains visible while unchecked."""
    module_path = ROOT / "static" / "mysql_visibility.js"
    script = r"""
const visibility = require(process.argv[1]);
const details = {
  hidden: false,
  attributes: {},
  setAttribute(name, value) { this.attributes[name] = String(value); }
};
const checkbox = {checked: false};
visibility.syncLocalMysqlVisibility(checkbox, details);
const unchecked = {hidden: details.hidden, ariaHidden: details.attributes["aria-hidden"]};
checkbox.checked = true;
visibility.syncLocalMysqlVisibility(checkbox, details);
const checked = {hidden: details.hidden, ariaHidden: details.attributes["aria-hidden"]};
console.log(JSON.stringify({unchecked, checked}));
"""
    completed = subprocess.run(
        ["node", "-e", script, str(module_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "unchecked": {"hidden": True, "ariaHidden": "true"},
        "checked": {"hidden": False, "ariaHidden": "false"},
    }
