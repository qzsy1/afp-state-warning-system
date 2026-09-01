"""Stable executable entry point.

Only this small loader is frozen into the EXE.  Product logic remains in the
external ``app`` directory and can be updated independently.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

import dependency_anchor  # noqa: F401 - makes third-party runtime discoverable to PyInstaller


def application_root(explicit: str = "") -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    if os.environ.get("AFP_MODULAR_ROOT"):
        return Path(os.environ["AFP_MODULAR_ROOT"]).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def load_bootstrap(root: Path):
    path = root / "app" / "bootstrap.py"
    if not path.exists():
        raise FileNotFoundError(
            f"模块化应用入口缺失：{path}\n请保持 EXE、app、config 目录位于同一软件目录。"
        )
    spec = importlib.util.spec_from_file_location("afp_external_bootstrap", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load application bootstrap: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    parser = argparse.ArgumentParser(description="AFP modular native desktop launcher")
    parser.add_argument("--app-root", default="")
    args, remainder = parser.parse_known_args()
    root = application_root(args.app_root)
    bootstrap = load_bootstrap(root)
    bootstrap.main(root, remainder)


if __name__ == "__main__":
    main()

