from __future__ import annotations

import json
import math
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from acquisition import (
    AcquisitionConfig,
    AcquisitionManager,
    DEFAULT_SIMULATOR_FILE,
    NEW_COLLECTION_COLUMNS,
    NEW_COLLECTION_SENSOR_COLUMNS,
    ORIGINAL_COLUMNS,
    SENSOR_COLUMNS,
)
from app import (
    DashboardData,
    NEW_DEMO_CHECKPOINT,
    NEW_DEMO_SOURCE,
    cap_pool,
)
from online_inference import inspect_prediction_model


class PoolingTests(unittest.TestCase):
    def test_cap_pool_is_normalized(self) -> None:
        health, weights = cap_pool(np.asarray([0.1, 0.4, 0.9]), 0.5)
        self.assertAlmostEqual(float(weights.sum()), 1.0)
        self.assertGreaterEqual(health, 0.1)
        self.assertLessEqual(health, 0.9)
        self.assertGreater(weights[-1], weights[0])


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dashboard = DashboardData()

    def test_bootstrap_and_view(self) -> None:
        bootstrap = self.dashboard.bootstrap()
        defaults = bootstrap["defaults"]
        payload = self.dashboard.view(
            defaults["specimen"],
            defaults["layer"],
            defaults["sensor"],
            defaults["distance"],
            defaults["length"],
            defaults["step"],
            defaults["threshold"],
            defaults["rho"],
            defaults["score_mode"],
        )
        self.assertEqual(len(payload["available_layers"]), 5)
        self.assertEqual(len(payload["series"]["actual"]), payload["selection"]["displayed_points"])
        self.assertTrue(math.isfinite(payload["preview"]["specimen"]["health"]))
        self.assertEqual(len(payload["sensor_stats"]), 12)

    def test_prediction_model_input_output_compatibility(self) -> None:
        bootstrap = self.dashboard.bootstrap()
        profile = bootstrap["acquisition"]["prediction_model"]
        required_outputs = bootstrap["acquisition"][
            "indicator_required_outputs"
        ]["TC-HI"]
        compatible = AcquisitionConfig(
            selected_sensors=SENSOR_COLUMNS.copy(),
            model_input_sensors=SENSOR_COLUMNS.copy(),
            prediction_sensors=required_outputs.copy(),
            model_output_sensors=required_outputs.copy(),
            prediction_model_file=profile["checkpoint"],
            health_indicator="TC-HI",
        )
        validation = self.dashboard.validate_prediction_setup(
            compatible, load_model=False
        )
        self.assertTrue(validation["compatible"])
        self.assertEqual(
            set(validation["selected_input_sensors"]),
            set(SENSOR_COLUMNS),
        )
        incompatible_inputs = AcquisitionConfig(
            selected_sensors=SENSOR_COLUMNS[:-1],
            model_input_sensors=SENSOR_COLUMNS[:-1],
            prediction_sensors=required_outputs.copy(),
            model_output_sensors=required_outputs.copy(),
            prediction_model_file=profile["checkpoint"],
            health_indicator="TC-HI",
        )
        with self.assertRaisesRegex(ValueError, "输入不一致"):
            self.dashboard.validate_prediction_setup(
                incompatible_inputs, load_model=False
            )
        incompatible_outputs = AcquisitionConfig(
            selected_sensors=SENSOR_COLUMNS.copy(),
            model_input_sensors=SENSOR_COLUMNS.copy(),
            prediction_sensors=["温度1"],
            model_output_sensors=["温度1"],
            prediction_model_file=profile["checkpoint"],
            health_indicator="TC-HI",
        )
        with self.assertRaisesRegex(ValueError, "健康指标需要模型输出"):
            self.dashboard.validate_prediction_setup(
                incompatible_outputs, load_model=False
            )

    def test_custom_prediction_model_metadata_can_define_sensor_subset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint = root / "subset_model.pth"
            checkpoint.write_bytes(b"metadata-only-test")
            inputs = [f"温度{index}" for index in range(1, 9)]
            columns = [*inputs, "cycle", "v", "p", "pr", "l"]
            metadata = {
                "name": "temperature-only-I-ModernTCN",
                "architecture": "I-ModernTCN",
                "enc_in": len(columns),
                "seq_len": 24,
                "pred_len": 24,
                "input_sensors": inputs,
                "output_sensors": inputs,
                "model_columns": columns,
                "scaler_mean": [0.0] * len(columns),
                "scaler_scale": [1.0] * len(columns),
            }
            (root / "subset_model.pth.json").write_text(
                json.dumps(metadata, ensure_ascii=False),
                encoding="utf-8",
            )
            profile = inspect_prediction_model(checkpoint)
            self.assertEqual(profile["input_sensors"], inputs)
            self.assertEqual(profile["output_sensors"], inputs)
            config = AcquisitionConfig(
                selected_sensors=inputs,
                model_input_sensors=inputs,
                prediction_sensors=inputs,
                model_output_sensors=inputs,
                prediction_model_file=str(checkpoint),
                health_indicator="T-HI",
            )
            validation = self.dashboard.validate_prediction_setup(
                config, load_model=False
            )
            self.assertTrue(validation["compatible"])

    def test_realtime_is_causal_and_all_channels_are_present(self) -> None:
        defaults = self.dashboard.bootstrap()["defaults"]
        before_window = self.dashboard.realtime(
            defaults["specimen"], defaults["sensor"], 12, 120, 1, 0.5, 0.5, "soft"
        )
        after_window = self.dashboard.realtime(
            defaults["specimen"], defaults["sensor"], 24, 120, 1, 0.5, 0.5, "soft"
        )
        self.assertEqual(len(before_window["channels"]), 12)
        self.assertEqual(len(self.dashboard.bootstrap()["indicators"]), 12)
        self.assertFalse(before_window["window"]["complete"])
        self.assertIsNone(before_window["layer"])
        self.assertTrue(after_window["window"]["complete"])
        self.assertEqual(after_window["layer"]["evidence_count"], 1)
        self.assertEqual(after_window["specimen"]["evidence_layers"], 1)
        self.assertLessEqual(len(after_window["selected_channel"]["prediction_future"]), 24)

    def test_candidate_recommendation_and_600_point_forecast(self) -> None:
        defaults = self.dashboard.bootstrap()["defaults"]
        payload = self.dashboard.realtime(
            defaults["specimen"],
            defaults["sensor"],
            240,
            120,
            1,
            0.5,
            0.5,
            "raw",
            indicator="KECA-SPE-HI",
            model_kind="svm_rbf",
            prediction_horizon=600,
        )
        self.assertEqual(payload["candidate"]["indicator"], "KECA-SPE-HI")
        self.assertEqual(payload["candidate"]["model"], "svm_rbf")
        self.assertTrue(payload["candidate"]["recommended"])
        self.assertEqual(payload["forecast"]["requested_horizon"], 600)
        self.assertEqual(payload["forecast"]["returned_horizon"], 600)
        self.assertEqual(payload["forecast"]["mode"], "archived_rolling_windows")

    def test_live_prediction_and_warning_toggles(self) -> None:
        defaults = self.dashboard.bootstrap()["defaults"]
        raw = self.dashboard.realtime(
            defaults["specimen"], defaults["sensor"], 240, 120, 1, 0.5, 0.5,
            "raw", "TC-HI", "random_forest", 24, False, False,
        )
        optimized = self.dashboard.realtime(
            defaults["specimen"], defaults["sensor"], 240, 120, 1, 0.5, 0.5,
            "raw", "TC-HI", "random_forest", 48, True, True,
        )
        self.assertEqual(raw["forecast"]["mode"], "archived_direct_24")
        self.assertEqual(optimized["forecast"]["mode"], "live_checkpoint_recursive")
        self.assertEqual(optimized["forecast"]["returned_horizon"], 48)
        self.assertEqual(
            raw["window"]["decision_mode"],
            "realtime_features_archived_prediction",
        )
        self.assertEqual(
            optimized["window"]["decision_mode"],
            "optimized_v13_8_soft_consistency",
        )
        self.assertEqual(
            optimized["feature_generation"]["prediction_source"],
            "live_checkpoint",
        )

    def test_online_features_are_generated_from_current_windows(self) -> None:
        indices = np.asarray([0, 17, 103], dtype=int)
        for feature_key, cached in self.dashboard.candidate_features.items():
            generated = self.dashboard.online_feature_engine.transform(
                self.dashboard.actual[indices],
                self.dashboard.prediction[indices],
                requested_key=feature_key,
            )[feature_key]
            np.testing.assert_allclose(
                generated,
                cached[indices],
                rtol=2e-4,
                atol=2e-5,
                err_msg=feature_key,
            )

    def test_causal_online_accuracy_is_close_to_offline_method(self) -> None:
        metrics_path = (
            Path(__file__).resolve().parent.parent
            / "outputs_causal_online_consistency_v13_9"
            / "causal_online_level_metrics.csv"
        )
        metrics = pd.read_csv(metrics_path)
        test = metrics.loc[metrics["dataset"].eq("test_all")].set_index("level")
        self.assertGreaterEqual(
            float(test.loc["window", "balanced_accuracy"]), 0.95
        )
        self.assertGreaterEqual(
            float(test.loc["layer", "balanced_accuracy"]), 0.90
        )
        self.assertGreaterEqual(
            float(test.loc["layer", "seven_state_accuracy"]), 0.90
        )
        self.assertEqual(
            float(test.loc["specimen", "seven_state_accuracy"]), 1.0
        )

    def test_real_acquisition_pipeline_saves_original_format(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            original_manager = self.dashboard.acquisition
            original_health_path = self.dashboard.live_layer_health_path
            original_health = self.dashboard.live_layer_health
            manager = AcquisitionManager(temporary_path)
            self.dashboard.acquisition = manager
            self.dashboard.live_layer_health_path = (
                temporary_path / "specimen_layer_health.json"
            )
            self.dashboard.live_layer_health = {}
            config = AcquisitionConfig(
                driver="simulator",
                source_file=str(DEFAULT_SIMULATOR_FILE),
                sample_rate_hz=1000.0,
                selected_sensors=SENSOR_COLUMNS.copy(),
                prediction_sensors=[
                    *[f"温度{index}" for index in range(1, 9)],
                    "压力",
                ],
                run_id="TEST_RUN",
                specimen_id="TEST_SPECIMEN",
                layer=0,
            )
            stopped = None
            try:
                self.assertTrue(manager.test_connection(config)["ok"])
                manager.start(config)
                deadline = time.time() + 5.0
                while manager.status()["sample_count"] < 50 and time.time() < deadline:
                    time.sleep(0.02)
                self.assertGreaterEqual(manager.status()["sample_count"], 48)
                payload = self.dashboard.live(
                    sensor_id=0,
                    history=120,
                    step=1,
                    threshold=0.5,
                    rho=0.5,
                    indicator="TC-HI",
                    model_kind="random_forest",
                    prediction_horizon=24,
                )
                self.assertEqual(payload["forecast"]["returned_horizon"], 24)
                self.assertTrue(payload["window"]["complete"])
                self.assertTrue(payload["window"]["optimized_warning_applied"])
                self.assertEqual(
                    payload["feature_generation"]["warning_optimization"],
                    "causal_online_v13_9",
                )
                self.assertEqual(
                    payload["feature_generation"]["mode"],
                    "realtime_from_actual_and_live_prediction",
                )
                self.assertEqual(
                    payload["feature_generation"]["historical_prediction_mode"],
                    "causal_first_display_frozen",
                )
                self.assertTrue(
                    any(
                        predictions
                        for predictions in
                        self.dashboard.live_rolling_prediction_cache.values()
                    )
                )
                self.assertEqual(payload["specimen"]["evidence_layers"], 1)
                self.assertFalse(payload["specimen"]["complete"])
                channels = {
                    item["name"]: item for item in payload["channels"]
                }
                self.assertTrue(channels["温度1"]["prediction_enabled"])
                self.assertEqual(
                    len(channels["温度1"]["prediction_future"]), 24
                )
                pressure_display_name = self.dashboard.sensors[10]["name"]
                self.assertEqual(
                    channels[pressure_display_name]["prediction_source_name"],
                    "压力",
                )
                self.assertTrue(
                    channels[pressure_display_name]["prediction_enabled"]
                )
                self.assertEqual(
                    len(channels[pressure_display_name]["prediction_future"]),
                    24,
                )
                self.assertFalse(channels["转速"]["prediction_enabled"])
                self.assertEqual(channels["转速"]["prediction_future"], [])
                self.assertGreater(len(channels["转速"]["actual"]), 0)
                override_payload = self.dashboard.live(
                    sensor_id=0,
                    history=120,
                    step=1,
                    threshold=0.5,
                    rho=0.5,
                    indicator="TC-HI",
                    model_kind="random_forest",
                    prediction_horizon=24,
                    prediction_sensors=[
                        "转速",
                        *[f"温度{index}" for index in range(1, 9)],
                        "压力",
                    ],
                )
                override_channels = {
                    item["name"]: item
                    for item in override_payload["channels"]
                }
                self.assertTrue(
                    override_channels["转速"]["prediction_enabled"]
                )
                self.assertTrue(
                    override_channels["温度1"]["prediction_enabled"]
                )
                self.assertTrue(
                    override_channels[pressure_display_name]["prediction_enabled"]
                )
                self.assertFalse(
                    override_channels["振动"]["prediction_enabled"]
                )
                stopped = manager.stop()
                self.dashboard.live(
                    sensor_id=0,
                    history=120,
                    step=1,
                    threshold=0.5,
                    rho=0.5,
                    indicator="TC-HI",
                    model_kind="random_forest",
                    prediction_horizon=24,
                    prediction_sensors=[
                        "转速",
                        *[f"温度{index}" for index in range(1, 9)],
                        "压力",
                    ],
                )
                cached_window_count = len(
                    self.dashboard.live_window_result_cache
                )
                cached_forecast_count = len(
                    self.dashboard.live_forecast_cache
                )
                repeated_payload = self.dashboard.live(
                    sensor_id=0,
                    history=120,
                    step=1,
                    threshold=0.5,
                    rho=0.5,
                    indicator="TC-HI",
                    model_kind="random_forest",
                    prediction_horizon=24,
                    prediction_sensors=[
                        "转速",
                        *[f"温度{index}" for index in range(1, 9)],
                        "压力",
                    ],
                )
                self.assertTrue(
                    repeated_payload["feature_generation"]["incremental_cache"]
                )
                self.assertEqual(
                    len(self.dashboard.live_window_result_cache),
                    cached_window_count,
                )
                self.assertEqual(
                    len(self.dashboard.live_forecast_cache),
                    cached_forecast_count,
                )
                completed_payload = self.dashboard.live(
                    sensor_id=0,
                    history=120,
                    step=1,
                    threshold=0.5,
                    rho=0.5,
                    indicator="TC-HI",
                    model_kind="random_forest",
                    prediction_horizon=24,
                )
                self.assertEqual(
                    completed_payload["layers"][0]["status"], "complete"
                )
                self.assertTrue(
                    completed_payload["layers"][0]["aggregate"]["state_label"]
                )
            finally:
                if stopped is None:
                    stopped = manager.stop()
                self.dashboard.acquisition = original_manager
                self.dashboard.live_layer_health_path = original_health_path
                self.dashboard.live_layer_health = original_health
            raw = pd.read_csv(stopped["raw_file"], encoding="gb18030")
            self.assertEqual(raw.columns.tolist(), ORIGINAL_COLUMNS)
            self.assertGreaterEqual(len(raw), 48)
            self.assertEqual(
                {
                    item["name"]
                    for item in stopped["sensors"]
                    if item["received_samples"] > 0
                },
                set(SENSOR_COLUMNS),
            )

    def test_selected_save_root_keeps_layer_and_whole_specimen_files(self) -> None:
        def capture_layer(root: Path, layer: int) -> dict:
            manager = AcquisitionManager(root / "unused_default")
            config = AcquisitionConfig(
                driver="simulator",
                source_file=str(DEFAULT_SIMULATOR_FILE),
                sample_rate_hz=1000.0,
                selected_sensors=SENSOR_COLUMNS.copy(),
                specimen_id="试样A",
                layer=layer,
                p=600,
                v=100,
                pr=600,
                save_root=str(root / "用户选择目录"),
            )
            manager.start(config)
            deadline = time.time() + 5.0
            while manager.status()["sample_count"] < 12 and time.time() < deadline:
                time.sleep(0.02)
            return manager.stop()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = capture_layer(root, 0)
            second = capture_layer(root, 1)
            specimen_dir = (
                root / "用户选择目录" / "试样A_p600_v100_pr600"
            )
            layer_1 = specimen_dir / "试样A_p600_v100_pr600_第1层.CSV"
            layer_2 = specimen_dir / "试样A_p600_v100_pr600_第2层.CSV"
            whole_1 = (
                specimen_dir
                / "试样A_p600_v100_pr600_完整试样_已采1层.CSV"
            )
            whole_2 = (
                specimen_dir
                / "试样A_p600_v100_pr600_完整试样_已采2层.CSV"
            )
            self.assertEqual(Path(first["raw_file"]), layer_1)
            self.assertEqual(Path(second["raw_file"]), layer_2)
            self.assertEqual(Path(first["full_specimen_file"]), whole_1)
            self.assertEqual(Path(second["full_specimen_file"]), whole_2)
            self.assertTrue(layer_1.exists())
            self.assertTrue(layer_2.exists())
            self.assertTrue(whole_1.exists())
            self.assertTrue(whole_2.exists())
            first_rows = pd.read_csv(layer_1, encoding="gb18030")
            second_rows = pd.read_csv(layer_2, encoding="gb18030")
            combined = pd.read_csv(whole_2, encoding="gb18030")
            self.assertEqual(combined.columns.tolist(), ORIGINAL_COLUMNS)
            self.assertEqual(len(combined), len(first_rows) + len(second_rows))
            self.assertEqual(second["completed_layers"], [1, 2])

    def test_new_collection_capture_only_never_loads_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            original_manager = self.dashboard.acquisition
            manager = AcquisitionManager(Path(temporary))
            self.dashboard.acquisition = manager
            config = AcquisitionConfig(
                processing_mode="capture_only",
                dataset_schema="new_collection_v11_3",
                driver="simulator",
                source_file=str(NEW_DEMO_SOURCE),
                prediction_model_file="Z:/不存在的模型.pth",
                selected_sensors=NEW_COLLECTION_SENSOR_COLUMNS.copy(),
                sample_rate_hz=1000.0,
                specimen_id="NEW_CAPTURE_ONLY",
                condition_id="H06",
            )
            stopped = None
            try:
                validation = self.dashboard.validate_prediction_setup(
                    config, load_model=True
                )
                self.assertFalse(validation["model_required"])
                manager.start(config)
                deadline = time.time() + 5.0
                while (
                    manager.status()["sample_count"] < 12
                    and time.time() < deadline
                ):
                    time.sleep(0.01)
                payload = self.dashboard.live(
                    0, 120, 1, 0.5, 0.5,
                    "TC-HI", "random_forest", 24,
                )
                self.assertEqual(payload["mode"], "capture_only")
                self.assertEqual(len(payload["channels"]), 19)
                self.assertEqual(payload["forecast"]["returned_horizon"], 0)
                self.assertEqual(
                    payload["feature_generation"]["mode"], "capture_only"
                )
                stopped = manager.stop()
            finally:
                if stopped is None:
                    stopped = manager.stop()
                self.dashboard.acquisition = original_manager
            raw = pd.read_csv(stopped["raw_file"], encoding="gb18030")
            self.assertEqual(raw.columns.tolist(), NEW_COLLECTION_COLUMNS)
            self.assertEqual(
                set(raw["condition_id"].astype(str)), {"H06"}
            )

    def test_new_collection_trained_model_runs_live_prediction(self) -> None:
        self.assertTrue(NEW_DEMO_CHECKPOINT.exists())
        self.assertTrue(NEW_DEMO_SOURCE.exists())
        original_checkpoint = self.dashboard.online_predictor.checkpoint
        original_manager = self.dashboard.acquisition
        with tempfile.TemporaryDirectory() as temporary:
            manager = AcquisitionManager(Path(temporary))
            self.dashboard.acquisition = manager
            profile = self.dashboard.inspect_prediction_model(
                str(NEW_DEMO_CHECKPOINT)
            )
            self.assertEqual(profile["enc_in"], 23)
            config = AcquisitionConfig(
                processing_mode="prediction_warning",
                dataset_schema="new_collection_v11_3",
                driver="simulator",
                source_file=str(NEW_DEMO_SOURCE),
                selected_sensors=NEW_COLLECTION_SENSOR_COLUMNS.copy(),
                model_input_sensors=profile["input_sensors"],
                model_output_sensors=profile["output_sensors"],
                prediction_sensors=profile["output_sensors"],
                prediction_model_file=str(NEW_DEMO_CHECKPOINT),
                health_indicator="TC-HI",
                sample_rate_hz=1000.0,
                specimen_id="NEW_PREDICTION",
                condition_id="H06",
            )
            stopped = None
            try:
                validation = self.dashboard.validate_prediction_setup(
                    config, load_model=True
                )
                self.assertTrue(validation["compatible"])
                manager.start(config)
                deadline = time.time() + 5.0
                while (
                    manager.status()["sample_count"] < 50
                    and time.time() < deadline
                ):
                    time.sleep(0.01)
                payload = self.dashboard.live(
                    0, 120, 1, 0.5, 0.5,
                    "TC-HI", "random_forest", 24,
                )
                self.assertEqual(payload["mode"], "live_acquisition")
                self.assertEqual(len(payload["channels"]), 19)
                self.assertEqual(payload["forecast"]["returned_horizon"], 24)
                self.assertTrue(payload["window"]["complete"])
                self.assertEqual(
                    payload["forecast"]["checkpoint"],
                    str(NEW_DEMO_CHECKPOINT.resolve()),
                )
                stopped = manager.stop()
            finally:
                if stopped is None:
                    stopped = manager.stop()
                self.dashboard.acquisition = original_manager
                self.dashboard.online_predictor.configure(
                    original_checkpoint
                )

    def test_best_prediction_override_uses_validation_selected_model(self) -> None:
        best = self.dashboard.best_prediction_profile(
            "new_collection_v11_3"
        )
        config = AcquisitionConfig(
            processing_mode="prediction_warning",
            dataset_schema="new_collection_v11_3",
            use_best_prediction_override=True,
            prediction_model_file="Z:/手动选择但不存在的模型.pth",
            selected_sensors=NEW_COLLECTION_SENSOR_COLUMNS.copy(),
            model_input_sensors=best["input_sensors"],
            model_output_sensors=best["output_sensors"],
            prediction_sensors=best["output_sensors"],
            health_indicator="TC-HI",
        )
        result = self.dashboard.validate_prediction_setup(
            config, load_model=False
        )
        self.assertTrue(result["best_prediction_override"])
        self.assertEqual(
            result["checkpoint"], str(NEW_DEMO_CHECKPOINT.resolve())
        )
        self.assertEqual(
            result["selection_metric"],
            "validation_mse_standardized",
        )
        self.assertAlmostEqual(
            float(result["selection_metric_value"]),
            0.035947587341070175,
        )
        self.assertNotIn("test", result["selection_basis"].lower())


if __name__ == "__main__":
    unittest.main()
