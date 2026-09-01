from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
import zipfile
from pathlib import Path


API_VERSION = "2.0"
MAPPINGS = (
    ("modular_runtime/app/", "app/"),
    ("modular_runtime/config/runtime.delivery.json", "config/runtime.json"),
    ("visualization_app/static/", "app/ui/"),
    ("visualization_app/model_runtime/", "app/legacy/model_runtime/"),
    ("visualization_app/", "app/legacy/"),
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def target_path(source: str) -> str | None:
    for prefix, target in MAPPINGS:
        if source == prefix:
            return target
        if source.startswith(prefix):
            relative = source[len(prefix):]
            if prefix == "visualization_app/" and "/" in relative:
                return None
            return target + relative
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an AFP module patch from a Git diff")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--base", default="HEAD~1")
    parser.add_argument("--output", required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    repo = Path(args.repo).resolve()
    names = subprocess.check_output(
        ["git", "-C", str(repo), "diff", "--name-only", "--diff-filter=AM", args.base, "HEAD"],
        text=True,
        encoding="utf-8",
    ).splitlines()
    files: list[tuple[Path, str]] = []
    for name in names:
        destination = target_path(name.replace("\\", "/"))
        source = repo / name
        if destination and source.is_file():
            files.append((source, destination))
    if not files:
        raise SystemExit("No independently updateable module/UI/config files changed")
    manifest = {
        "patch_version": args.version,
        "api_version": API_VERSION,
        "base_ref": args.base,
        "files": [{"path": destination, "sha256": digest(source)} for source, destination in files],
    }
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("patch_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for source, destination in files:
            archive.write(source, f"payload/{destination}")
    print(output)


if __name__ == "__main__":
    main()

