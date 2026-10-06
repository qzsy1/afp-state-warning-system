import io
import types
import unittest

from harness.engine.function_tests import load_function_tests, run_function_tests


class FunctionTestAdapterTests(unittest.TestCase):
    def test_collects_only_zero_argument_test_functions_in_name_order(self) -> None:
        calls: list[str] = []
        module = types.ModuleType("sample_contracts")

        def helper() -> None:
            calls.append("helper")

        def test_zeta() -> None:
            calls.append("zeta")

        def test_alpha() -> None:
            calls.append("alpha")

        helper.__module__ = module.__name__
        test_zeta.__module__ = module.__name__
        test_alpha.__module__ = module.__name__
        module.helper = helper
        module.test_zeta = test_zeta
        module.test_alpha = test_alpha

        suite = load_function_tests(module)
        result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)

        self.assertTrue(result.wasSuccessful())
        self.assertEqual(result.testsRun, 2)
        self.assertEqual(calls, ["alpha", "zeta"])

    def test_rejects_test_function_with_required_arguments(self) -> None:
        module = types.ModuleType("bad_contracts")

        def test_needs_fixture(value: object) -> None:
            del value

        test_needs_fixture.__module__ = module.__name__
        module.test_needs_fixture = test_needs_fixture

        with self.assertRaisesRegex(ValueError, "required arguments"):
            load_function_tests(module)

    def test_runner_returns_nonzero_for_assertion_failure(self) -> None:
        module = types.ModuleType("failing_contracts")

        def test_failure() -> None:
            raise AssertionError("expected failure")

        test_failure.__module__ = module.__name__
        module.test_failure = test_failure

        exit_code = run_function_tests([module], stream=io.StringIO())

        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
