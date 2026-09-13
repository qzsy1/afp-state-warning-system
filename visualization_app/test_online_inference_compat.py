import types
import unittest
from pathlib import Path
import sys

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


if __name__ == "__main__":
    unittest.main()
