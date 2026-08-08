# -*- coding: utf-8 -*-
from __future__ import annotations

import numpy as np
import pandas as pd

from run_physics_guided_health_indicator_v13 import (
    PRESSURE,
    ROTATION,
    TEMP,
    VIBRATION,
    ANOMALY_TYPES,
    anomaly_observability,
    apply_physics_response,
    balanced_anomaly_schedule,
    contact_window_statistics,
    process_feature_names,
    process_features,
    response_feature_names,
    response_features,
    _normalized_compaction_violation,
)
from run_layer_specimen_health_indicator_v13_2 import (
    TARGET_STATE_COUNTS,
    allocate_specimen_splits_and_states,
    build_layer_ledger,
)
from run_hierarchical_specimen_health_indicator_v13_3 import (
    AggregationConfig,
    _cap_weights,
    _candidate_specs,
    aggregate_window_predictions,
)


def test_hierarchical_candidate_grid_is_complete_12_by_4() -> None:
    specs = _candidate_specs()
    assert len(specs) == 48
    assert len(set(specs)) == 48
    counts = pd.Series([family for family, _, _ in specs]).value_counts()
    assert set(counts.to_numpy()) == {4}
    assert {model for _, _, model in specs} == {
        "logistic", "svm_rbf", "random_forest", "extra_trees",
    }


def test_constrained_autopool_is_bounded_and_between_mean_and_max() -> None:
    scores = np.asarray([0.05, 0.10, 0.20, 0.95], dtype=float)
    weights, alpha = _cap_weights(scores, rho=1.0)
    pooled = float(np.dot(weights, scores))
    assert alpha > 0.0
    assert np.isclose(weights.sum(), 1.0)
    assert float(weights.max()) <= 0.5 + 1e-12
    assert float(scores.mean()) <= pooled <= float(scores.max())


def test_response_feature_layout_is_complete() -> None:
    names, groups = response_feature_names()
    window = np.zeros((4, 24, 12), dtype=float)
    window[:, :, TEMP] = 35.0
    window[:, :, PRESSURE] = 100.0
    features = response_features(window, window, ambient=20.0)
    assert features.shape == (4, len(names)) == (4, 42)
    assert np.isfinite(features).all()
    assert len(groups["thermal"]) == 25
    assert len(groups["coupling"]) == 3


def test_process_boundary_features_match_names() -> None:
    bounds = {"p": (550.0, 750.0), "v": (100.0, 110.0), "pr": (300.0, 900.0)}
    derived = {"line_heat": (5.0, 7.5), "compaction": (3.0, 9.0)}
    x = process_features(
        np.asarray([[650.0, 105.0, 600.0, 2.0], [825.0, 105.0, 600.0, 2.0]]),
        bounds, derived,
    )
    assert x.shape[1] == len(process_feature_names()) == 15
    assert x[0, 6] == 0.0
    assert x[1, 6] > 0.0


def test_counterfactual_response_has_afp_directionality() -> None:
    normal = np.zeros((24, 12), dtype=float)
    normal[:, TEMP] = 80.0
    normal[:, PRESSURE] = 200.0
    normal[:, ROTATION] = 4.0
    nominal = np.asarray([650.0, 105.0, 600.0, 2.0])
    high_power = apply_physics_response(
        normal, nominal, np.asarray([825.0, 105.0, 600.0, 2.0]), "power_high", 20.0
    )
    high_speed = apply_physics_response(
        normal, nominal, np.asarray([650.0, 125.0, 600.0, 2.0]), "speed_high", 20.0
    )
    high_force = apply_physics_response(
        normal, nominal, np.asarray([650.0, 105.0, 1020.0, 2.0]), "compaction_high", 20.0
    )
    assert high_power[:, TEMP].mean() > normal[:, TEMP].mean()
    assert high_speed[:, TEMP].mean() < normal[:, TEMP].mean()
    assert high_speed[:, ROTATION].mean() > normal[:, ROTATION].mean()
    assert high_force[:, PRESSURE].mean() > normal[:, PRESSURE].mean()


def test_compaction_without_contact_does_not_create_artificial_residual() -> None:
    normal = np.zeros((24, 12), dtype=float)
    normal[:, TEMP] = 18.0
    normal[:, PRESSURE] = 0.0
    normal[:, VIBRATION] = np.linspace(-0.1, 0.1, 24)
    nominal = np.asarray([650.0, 105.0, 600.0, 2.0])
    changed = apply_physics_response(
        normal, nominal, np.asarray([650.0, 105.0, 1020.0, 2.0]),
        "compaction_high", 20.0,
    )
    assert np.array_equal(changed, normal)


def test_observability_is_recorded_without_reassigning_type() -> None:
    pressure_only = np.zeros((24, 12), dtype=float)
    pressure_only[:, TEMP] = 21.0
    pressure_only[:, PRESSURE] = 100.0
    reason = anomaly_observability("power_high", pressure_only, 20.0)
    assert reason == "parameter_only_no_thermal_activity"

    thermal_only = np.zeros((24, 12), dtype=float)
    thermal_only[:, TEMP] = 60.0
    reason = anomaly_observability("compaction_low", thermal_only, 20.0)
    assert reason == "parameter_only_no_roller_contact"


def test_balanced_anomaly_schedule_differs_by_at_most_one() -> None:
    schedule = balanced_anomaly_schedule(1034, "test", 20260713)
    counts = np.asarray([np.sum(schedule == kind) for kind in ANOMALY_TYPES])
    assert counts.sum() == 1034
    assert counts.max() - counts.min() <= 1


def test_contact_gate_rejects_single_spike_and_accepts_two_points() -> None:
    windows = np.zeros((3, 24, 12), dtype=float)
    windows[0, 8, PRESSURE] = 800.0
    windows[1, 8:10, PRESSURE] = 50.0
    windows[2, [8, 10], PRESSURE] = 50.0
    stats = contact_window_statistics(
        windows, force_threshold_n=10.0, min_consecutive_points=2
    )
    assert stats["contact_event_eligible"].tolist() == [False, True, False]
    assert stats["contact_longest_consecutive_points"].tolist() == [1, 2, 1]


def test_compaction_boundary_uses_engineering_tolerance() -> None:
    bounds = {"pr": (300.0, 900.0)}
    assert _normalized_compaction_violation(299.999923, bounds)[2] == 0.0
    assert _normalized_compaction_violation(900.000077, bounds)[2] == 0.0
    assert _normalized_compaction_violation(299.0, bounds)[0] > 0.0
    assert _normalized_compaction_violation(901.0, bounds)[1] > 0.0


def _twenty_six_specimen_keys():
    conditions = [
        (600, 100, 450), (600, 100, 600), (600, 100, 750),
        (600, 100, 900), (600, 110, 300), (650, 100, 450),
        (650, 110, 600), (700, 100, 600), (700, 110, 450),
        (750, 100, 450), (750, 110, 600), (800, 100, 300),
        (800, 110, 450),
    ]
    return [
        (*condition, specimen)
        for condition in conditions
        for specimen in ("试件1", "试件2")
    ]


def test_layer_assignment_has_26_specimens_and_equal_anomaly_types() -> None:
    keys = _twenty_six_specimen_keys()
    incomplete = [(600, 100, 450, "试件2"), (600, 110, 300, "试件2")]
    assignment = allocate_specimen_splits_and_states(keys, incomplete, seed=2026)
    assert len(assignment) == 26
    assert assignment["dataset_split"].value_counts().to_dict() == {
        "train": 15,
        "test_extrapolation": 4,
        "validation": 4,
        "test_interpolation": 3,
    }
    assert assignment["health_state"].value_counts().to_dict() == TARGET_STATE_COUNTS
    anomaly_counts = assignment.loc[
        assignment["health_state"] != "normal", "health_state"
    ].value_counts()
    assert anomaly_counts.nunique() == 1
    assert anomaly_counts.iloc[0] == 3
    assert assignment.loc[
        assignment["full_specimen_id"].isin([
            "P600_V100_PR450_试件2", "P600_V110_PR300_试件2"
        ]), "dataset_split"
    ].eq("train").all()


def test_layer_ledger_is_exactly_26_times_5_and_group_consistent() -> None:
    keys = _twenty_six_specimen_keys()
    incomplete = [(600, 100, 450, "试件2"), (600, 110, 300, "试件2")]
    assignment = allocate_specimen_splits_and_states(keys, incomplete, seed=2026)
    rows = []
    bank_index = 0
    for p, v, pr, specimen in keys:
        for layer in range(5):
            if (p, v, pr, specimen) == incomplete[0] and layer == 3:
                continue
            if (p, v, pr, specimen) == incomplete[1] and layer in (0, 1, 2, 3):
                continue
            rows.append({
                "bank_index": bank_index,
                "p": p, "v": v, "pr": pr,
                "specimen_label": specimen, "layer": layer,
            })
            bank_index += 1
    ledger, _ = build_layer_ledger(pd.DataFrame(rows), assignment)
    assert len(ledger) == 130
    assert ledger["full_specimen_id"].nunique() == 26
    assert ledger.groupby("full_specimen_id").size().eq(5).all()
    assert ledger.groupby("full_specimen_id")["health_state"].nunique().eq(1).all()
    assert int(ledger["imputed_from_same_condition_other_specimen"].sum()) == 5
    assert ledger.loc[
        ledger["imputed_from_same_condition_other_specimen"], "dataset_split"
    ].eq("train").all()


def test_hierarchical_compaction_events_trigger_consistent_state() -> None:
    rows = []
    for layer in range(5):
        for window in range(2):
            rows.append({
                "window_sample_id": f"S1_L{layer}_W{window}",
                "layer_sample_id": f"S1_L{layer}",
                "full_specimen_id": "S1",
                "layer": layer,
                "dataset_split": "test_interpolation",
                "true_specimen_state": "compaction_low",
                "true_binary_label": 1,
                "observable_window_state": "compaction_low",
                "window_training_eligible": True,
                "evidence_reason": "observable_contact_response",
                "thermal_observed": False,
                "contact_observed": True,
                "raw_layer_present": True,
                "imputed_layer": False,
            })
    metadata = pd.DataFrame(rows)
    binary_scores = np.asarray([0.95, 0.90] + [0.05] * 8)
    type_probabilities = np.full((10, 6), 0.02)
    type_probabilities[:, 4] = 0.90  # compaction_low in ANOMALY_TYPES
    type_probabilities /= type_probabilities.sum(axis=1, keepdims=True)
    window, layer, specimen = aggregate_window_predictions(
        metadata, binary_scores, type_probabilities,
        window_threshold=0.5, layer_threshold=0.99, specimen_threshold=0.99,
    )
    assert int(window["predicted_compaction_event"].sum()) == 2
    assert layer["predicted_state"].eq("compaction_low").all()
    assert layer["final_decision_source"].eq(
        "five_layer_specimen_consistency"
    ).all()
    assert layer["local_predicted_state"].eq("normal").any()
    assert specimen["predicted_state"].iloc[0] == "compaction_low"
    assert int(specimen["predicted_binary_label"].iloc[0]) == 1
    assert (
        specimen["predicted_state"].eq("normal")
        == specimen["predicted_binary_label"].eq(0)
    ).all()
    _, cap_layer, cap_specimen = aggregate_window_predictions(
        metadata, binary_scores, type_probabilities,
        window_threshold=0.5, layer_threshold=0.99, specimen_threshold=0.99,
        aggregation=AggregationConfig("cap", 0.5),
    )
    assert cap_layer["aggregation_method"].eq("cap").all()
    assert cap_specimen["aggregation_method"].eq("cap").all()
    assert cap_layer["maximum_pooling_weight"].le(0.5 + 1e-12).all()
    assert cap_specimen["maximum_pooling_weight"].le(0.5 + 1e-12).all()
