from __future__ import annotations

import html
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

from .diagnostics import DiagnosticIssue
from .reporting import ReportWriteError


def invalidate_latest(report_dir: Path) -> None:
    try:
        (Path(report_dir) / "latest-errors.html").unlink(missing_ok=True)
    except OSError as exc:
        raise ReportWriteError(f"could not invalidate old error report: {exc}") from exc


def write_machine_logs(payload: Mapping[str, object], run_dir: Path) -> None:
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "state.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        for raw in payload.get("checks", []):
            if not isinstance(raw, Mapping):
                continue
            check_id = str(raw.get("check_id", "unknown"))
            body = "\n".join((
                f"command: {' '.join(str(x) for x in raw.get('command', []))}",
                f"exit_code: {raw.get('exit_code')}",
                "", "[stdout]", str(raw.get("stdout", "")),
                "", "[stderr]", str(raw.get("stderr", "")),
                "", "[error]", str(raw.get("error", "")),
            ))
            (run_dir / f"{check_id}.log").write_text(body, encoding="utf-8")
    except (OSError, TypeError, ValueError) as exc:
        raise ReportWriteError(f"could not write harness machine logs: {exc}") from exc


def write_error_report(
    issues: Sequence[DiagnosticIssue],
    report_dir: Path,
    metadata: Mapping[str, object],
) -> Path | None:
    report_dir = Path(report_dir)
    if not issues:
        invalidate_latest(report_dir)
        return None
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    history = report_dir / f"errors-{timestamp}.html"
    latest = report_dir / "latest-errors.html"
    cards: list[str] = []
    for issue in issues:
        location = ""
        if issue.file:
            location = f"<p><b>Location:</b> {html.escape(issue.file)}"
            if issue.line is not None:
                location += f":{issue.line}"
            location += "</p>"
        cards.append(
            "<section class='issue'>"
            f"<h2>{html.escape(issue.check_id)} - {html.escape(issue.title)}</h2>"
            f"<p><b>Category:</b> {html.escape(issue.category)} | <b>Severity:</b> {html.escape(issue.severity)}</p>"
            f"<p><b>Reason:</b> {html.escape(issue.message)}</p>{location}"
            f"<p><b>Exit code:</b> {html.escape(str(issue.exit_code))}</p>"
            f"<p><b>Log:</b> <code>{html.escape(issue.log_path)}</code></p>"
            f"<p><b>Rerun:</b> <code>{html.escape(issue.rerun_command)}</code></p>"
            "</section>"
        )
    document = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>AFP Harness Errors</title><style>
body{font-family:"Microsoft YaHei",sans-serif;background:#f5f7fa;color:#172033;margin:24px}
main{max-width:1100px;margin:auto}.issue{background:#fff;border-left:5px solid #c62828;padding:14px 18px;margin:14px 0;box-shadow:0 2px 8px #0001}
code{word-break:break-all;background:#eef2f7;padding:2px 5px}h1{color:#8e1717}
</style></head><body><main><h1>AFP Harness problem report</h1>"""
    document += f"<p>Profile: {html.escape(str(metadata.get('profile', 'unknown')))} | Outcome: {html.escape(str(metadata.get('outcome', 'unknown')))}</p>"
    document += "".join(cards) + "</main></body></html>"
    token = uuid.uuid4().hex
    temp_history = report_dir / f".{history.name}.{token}.tmp"
    temp_latest = report_dir / f".{latest.name}.{token}.tmp"
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        temp_history.write_text(document, encoding="utf-8")
        temp_latest.write_text(document, encoding="utf-8")
        temp_history.replace(history)
        temp_latest.replace(latest)
    except OSError as exc:
        for path in (temp_history, temp_latest):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise ReportWriteError(f"could not write HTML error report: {exc}") from exc
    return latest
