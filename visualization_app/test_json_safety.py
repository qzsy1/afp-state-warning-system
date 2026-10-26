import math
import unittest

from json_safety import json_safe_value


class JsonSafetyTests(unittest.TestCase):
    def test_mapping_and_sequence_subtypes_keep_their_json_contract(self):
        from collections import UserDict, UserList
        payload = UserDict({1: UserList([float("nan"), (1, "x", True, None)])})
        self.assertEqual(json_safe_value(payload), {"1": [None, [1, "x", True, None]]})
        self.assertEqual(json_safe_value(b"abc"), b"abc")

    def test_non_finite_numbers_become_null_without_changing_other_values(self):
        payload = {"nan": float("nan"), "inf": float("inf"), "ok": 1.25, "items": [float("-inf"), "x"]}

        cleaned = json_safe_value(payload)

        self.assertIsNone(cleaned["nan"])
        self.assertIsNone(cleaned["inf"])
        self.assertEqual(cleaned["ok"], 1.25)
        self.assertIsNone(cleaned["items"][0])
        self.assertEqual(cleaned["items"][1], "x")
        self.assertTrue(math.isfinite(cleaned["ok"]))


if __name__ == "__main__":
    unittest.main()
