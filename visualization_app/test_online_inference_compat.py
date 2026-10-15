import types
import unittest
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import online_inference


class _FullModel:
    def __init__(self, config):
        self.loaded = False

    def load_state_dict(self, state):
        if "core.baseline_gat.weight" in state:
            raise RuntimeError("full GAT model does not accept baseline_gat checkpoint")
        self.loaded = True


class _WithoutGatModel:
    def __init__(self, config):
        self.loaded = False

    def load_state_dict(self, state):
        if "core.baseline_gat.weight" not in state:
            raise RuntimeError("ablation model requires baseline_gat checkpoint")
        self.loaded = True


class OnlineInferenceCompatibilityTest(unittest.TestCase):
    def test_ablation_checkpoint_uses_matching_without_gat_model(self):
        module = types.SimpleNamespace(Model=_FullModel, Model_wo_GAT=_WithoutGatModel)
        model = online_inference._load_model_compatible(
            module,
            object(),
            {"core.baseline_gat.weight": object()},
            "Model",
        )
        self.assertIsInstance(model, _WithoutGatModel)
        self.assertTrue(model.loaded)

    def test_full_checkpoint_keeps_preferred_model(self):
        module = types.SimpleNamespace(Model=_FullModel, Model_wo_GAT=_WithoutGatModel)
        model = online_inference._load_model_compatible(
            module,
            object(),
            {"core.gat1.GConv1.weight": object()},
            "Model",
        )
        self.assertIsInstance(model, _FullModel)
        self.assertTrue(model.loaded)

    def test_missing_checkpoint_from_old_delivery_reports_diagnostic(self):
        with tempfile.TemporaryDirectory() as temporary:
            current = Path(temporary) / "models" / "new" / "i_T_G" / "checkpoint.pth"
            current.parent.mkdir(parents=True)
            current.write_bytes(b"checkpoint")
            stale = Path("F:/retired/delivery/AFP_v2.0.3/models/new/i_T_G/checkpoint.pth")
            with patch.object(online_inference, "_registered_checkpoint", return_value=current):
                with self.assertRaisesRegex(FileNotFoundError, "预测模型文件不存在"):
                    online_inference.inspect_prediction_model(
                        stale,
                        model_type="i_T_G",
                        schema_mode="new_collection_v11_3",
                    )

    def test_missing_custom_checkpoint_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            current = Path(temporary) / "models" / "new" / "i_T_G" / "checkpoint.pth"
            current.parent.mkdir(parents=True)
            current.write_bytes(b"checkpoint")
            custom = Path(temporary) / "user-models" / "missing.pth"
            with patch.object(online_inference, "_registered_checkpoint", return_value=current):
                with self.assertRaisesRegex(FileNotFoundError, "预测模型文件不存在"):
                    online_inference.inspect_prediction_model(
                        custom,
                        model_type="i_T_G",
                        schema_mode="new_collection_v11_3",
                    )


if __name__ == "__main__":
    unittest.main()
