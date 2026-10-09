from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


_MUTABLE_TOP_LEVEL_DIRECTORIES = {
    "logs",
    "rollback",
    "runtime",
    "updates",
    "verification",
}
_UNMANAGED_SUFFIXES = {".bak", ".pid", ".pyc", ".pyo"}


@dataclass(frozen=True)
class DeliverySnapshot:
    root: Path
    files: tuple[str, ...]
    sha256: dict[str, str]


@dataclass(frozen=True)
class AssemblyResult:
    command: tuple[str, ...]
    returncode: int
    target: Path
    stdout: str
    stderr: str


def _is_managed(relative_path: Path) -> bool:
    parts = relative_path.parts
    if not parts or parts[0].lower() in _MUTABLE_TOP_LEVEL_DIRECTORIES:
        return False
    lowered = tuple(part.lower() for part in parts)
    if "__pycache__" in lowered:
        return False
    if len(lowered) >= 2 and lowered[:2] == ("app", "runtime"):
        return False
    if len(lowered) >= 2 and lowered[:2] == ("config", "local"):
        return False
    return relative_path.suffix.lower() not in _UNMANAGED_SUFFIXES


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def capture_delivery_snapshot(delivery_root: Path) -> DeliverySnapshot:
    root = Path(delivery_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"delivery root does not exist: {root}")
    paths = sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and _is_managed(path.relative_to(root))
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    files = tuple(path.relative_to(root).as_posix() for path in paths)
    return DeliverySnapshot(
        root=root,
        files=files,
        sha256={relative: _sha256_file(root / relative) for relative in files},
    )


def assert_delivery_unchanged(before: DeliverySnapshot, after: DeliverySnapshot) -> None:
    if before.root != after.root:
        raise AssertionError(f"delivery root changed: {before.root} -> {after.root}")
    before_files = set(before.files)
    after_files = set(after.files)
    added = sorted(after_files - before_files)
    removed = sorted(before_files - after_files)
    changed = sorted(
        path
        for path in before_files & after_files
        if before.sha256[path] != after.sha256[path]
    )
    if added or removed or changed:
        details = []
        if changed:
            details.append(f"changed: {', '.join(changed)}")
        if added:
            details.append(f"added: {', '.join(added)}")
        if removed:
            details.append(f"removed: {', '.join(removed)}")
        raise AssertionError(f"delivery changed: {'; '.join(details)}")


def source_inventory_issues(repo_root: Path, reference_release: Path) -> tuple[str, ...]:
    repo = Path(repo_root).resolve()
    source = repo / "visualization_app"
    legacy = Path(reference_release).resolve() / "app" / "legacy"
    issues: list[str] = []
    if not legacy.is_dir():
        return (f"reference legacy runtime is missing: {legacy}",)
    for delivery_path in sorted(legacy.glob("*.py"), key=lambda path: path.name):
        source_path = source / delivery_path.name
        if not source_path.is_file():
            issues.append(
                f"delivery-only runtime: app/legacy/{delivery_path.name} "
                f"has no authoritative source visualization_app/{delivery_path.name}"
            )
    delivery_static = legacy / "static"
    if delivery_static.is_dir():
        for delivery_path in sorted(
            (path for path in delivery_static.rglob("*") if path.is_file()),
            key=lambda path: path.relative_to(delivery_static).as_posix(),
        ):
            relative = delivery_path.relative_to(delivery_static)
            source_path = source / "static" / relative
            if not source_path.is_file():
                issues.append(
                    "delivery-only static asset: "
                    f"app/legacy/static/{relative.as_posix()} has no authoritative source "
                    f"visualization_app/static/{relative.as_posix()}"
                )
    return tuple(issues)


def run_isolated_assembly(
    repo_root: Path,
    reference_release: Path,
    launcher: Path,
    target_parent: Path,
) -> AssemblyResult:
    repo = Path(repo_root).resolve()
    reference = Path(reference_release).resolve()
    launcher_path = Path(launcher).resolve()
    parent = Path(target_parent).resolve()
    official_delivery = (repo / "delivery").resolve()
    if parent == official_delivery or official_delivery in parent.parents:
        raise ValueError(
            f"isolated assembly target must not be inside official delivery: {parent}"
        )
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / f"real-acquisition-{uuid.uuid4().hex}"
    script = repo / "modular_runtime" / "assemble_modular_delivery.ps1"
    command = (
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-ReferenceRelease",
        str(reference),
        "-TargetDir",
        str(target),
        "-ApplicationVersion",
        "real-acquisition-preflight",
        "-ExistingExecutable",
        str(launcher_path),
    )
    try:
        completed = subprocess.run(
            command,
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except OSError as exc:
        returncode = 127
        stdout = ""
        stderr = f"assembly command could not start: {exc}"
    return AssemblyResult(command, returncode, target, stdout, stderr)


_MANIFEST_LINE = re.compile(r"^([0-9A-Fa-f]{64}) [ *](.+)$")
_CANDIDATE_REQUIRED_FILES = (
    "AFP_Integrated_System_Modular.exe",
    "app/legacy/acquisition.py",
    "app/legacy/real_acquisition.py",
    "app/legacy/local_direct_acquisition.py",
    "app/ui/index.html",
    "app/ui/app.js",
    "app/ui/styles.css",
    "app/ui/mysql_visibility.js",
    "app/ui/process_parameters.js",
    "app/ui/public_demo.js",
    "app/ui/training.html",
    "app/ui/training.js",
)
_CANDIDATE_REQUIRED_DIRECTORIES = ("_internal",)


def validate_candidate_runtime(candidate_root: Path) -> tuple[str, ...]:
    root = Path(candidate_root).resolve()
    issues: list[str] = []
    for relative in _CANDIDATE_REQUIRED_FILES:
        if not (root / relative).is_file():
            issues.append(f"candidate file missing: {relative}")
    for relative in _CANDIDATE_REQUIRED_DIRECTORIES:
        if not (root / relative).is_dir():
            issues.append(f"candidate directory missing: {relative}")

    manifest_path = root / "SHA256SUMS.txt"
    manifest: dict[str, str] = {}
    if not manifest_path.is_file():
        issues.append("candidate file missing: SHA256SUMS.txt")
    else:
        try:
            lines = manifest_path.read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeError) as exc:
            issues.append(f"manifest unreadable: {exc}")
            lines = []
        for line_number, line in enumerate(lines, 1):
            match = _MANIFEST_LINE.fullmatch(line.strip())
            if not match:
                issues.append(f"manifest line {line_number} is invalid")
                continue
            digest, raw_relative = match.groups()
            relative = raw_relative.replace("\\", "/")
            relative_path = PurePosixPath(relative)
            resolved = (root / Path(*relative_path.parts)).resolve()
            if (
                relative_path.is_absolute()
                or ".." in relative_path.parts
                or re.match(r"^[A-Za-z]:/", relative)
                or (resolved != root and root not in resolved.parents)
            ):
                issues.append(f"manifest path escapes candidate: {relative}")
                continue
            if relative in manifest:
                issues.append(f"manifest path is duplicated: {relative}")
                continue
            manifest[relative] = digest.lower()
        for relative, expected in manifest.items():
            path = root / relative
            if not path.is_file():
                issues.append(f"manifest file missing: {relative}")
            elif _sha256_file(path) != expected:
                issues.append(f"manifest hash mismatch: {relative}")
        actual_managed = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
            and path.name != "SHA256SUMS.txt"
            and _is_managed(path.relative_to(root))
        }
        for relative in sorted(actual_managed - set(manifest)):
            issues.append(f"managed candidate file missing from manifest: {relative}")

    launcher = root / "AFP_Integrated_System_Modular.exe"
    if launcher.is_file():
        command = (str(launcher), "--self-test")
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            issues.append("candidate self-test timed out after 120 seconds")
        except OSError as exc:
            issues.append(f"candidate self-test could not start: {exc}")
        else:
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()
                issues.append(
                    f"candidate self-test failed (exit {completed.returncode}): "
                    f"{detail[:2000]}"
                )

    acquisition_path = root / "app" / "legacy" / "acquisition.py"
    real_path = root / "app" / "legacy" / "real_acquisition.py"
    if acquisition_path.is_file() and real_path.is_file():
        legacy = acquisition_path.parent
        environment = dict(os.environ)
        environment.pop("PYTHONHOME", None)
        environment["PYTHONPATH"] = str(legacy)
        completed = subprocess.run(
            [sys.executable, "-c", "import acquisition"],
            cwd=legacy,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            issues.append(f"candidate acquisition import failed: {detail}")
    return tuple(issues)
