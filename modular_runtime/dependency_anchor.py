"""Import inventory used only by the one-time PyInstaller launcher build."""
from __future__ import annotations

import os
import sys


if getattr(sys, "frozen", False) and os.environ.get("AFP_FORCE_DEPENDENCY_IMPORT") == "1":
    import flask  # noqa: F401
    import joblib  # noqa: F401
    import mysql.connector  # noqa: F401
    import numpy  # noqa: F401
    import pandas  # noqa: F401
    import serial  # noqa: F401
    import sklearn  # noqa: F401
    import torch  # noqa: F401
    import torch_geometric  # noqa: F401
    import webview  # noqa: F401
