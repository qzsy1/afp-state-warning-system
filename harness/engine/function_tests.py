from __future__ import annotations

import argparse
import importlib
import inspect
import sys
import types
import unittest
from collections.abc import Sequence
from typing import TextIO


def load_function_tests(module: types.ModuleType) -> unittest.TestSuite:
    """Adapt zero-argument ``test_*`` functions to the stdlib test runner."""
    suite = unittest.TestSuite()
    for name, function in inspect.getmembers(module, inspect.isfunction):
        if not name.startswith("test_") or function.__module__ != module.__name__:
            continue
        required = [
            parameter
            for parameter in inspect.signature(function).parameters.values()
            if parameter.default is inspect.Parameter.empty
            and parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        ]
        if required:
            raise ValueError(f"{module.__name__}.{name} has required arguments")
        suite.addTest(unittest.FunctionTestCase(function, description=f"{module.__name__}.{name}"))
    return suite


def run_function_tests(modules: Sequence[types.ModuleType], *, stream: TextIO | None = None) -> int:
    suite = unittest.TestSuite(load_function_tests(module) for module in modules)
    result = unittest.TextTestRunner(stream=stream or sys.stderr, verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run zero-argument module-level test functions with unittest reporting."
    )
    parser.add_argument("modules", nargs="+", help="Importable modules containing test_* functions")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        modules = [importlib.import_module(name) for name in args.modules]
        return run_function_tests(modules)
    except (ImportError, ValueError) as exc:
        print(f"function-tests: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
