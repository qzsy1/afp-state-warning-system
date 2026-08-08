# -*- coding: utf-8 -*-
"""Run the latest TC-HI fixed-split hierarchical warning experiment.

This entry point deliberately contains no cross-validation.  The 26 physical
specimens keep one fixed train/validation/interpolation-test/extrapolation-test
assignment.  Model stability is assessed by refitting the selected TC-HI
classifier with repeated estimator seeds while leaving data, injected response
and all specimen assignments unchanged.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from run_hierarchical_specimen_health_indicator_v13_3 import (
    run_hierarchical_specimen_benchmark,
)
from run_physics_guided_health_indicator_v13 import (
    OUTPUT_ENCODING,
    configure_matplotlib,
    prepare_benchmark,
    save_figure,
)


SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE = SCRIPT_DIR.parent
PROJECT_ROOT = SCRIPT_DIR.parents[2]
ARCHIVED_RESULT_DIR = PROJECT_ROOT / "results" / "3"
DEFAULT_OUTPUT = SCRIPT_DIR / "outputs_tc_hi_fixed_split_v13_7"


def _make_stability_figures(output: Path, result) -> list[str]:
    plt = configure_matplotlib()
    plt.rcParams["font.family"] = "Microsoft YaHei"
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    figure_dir = output / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []

    stability = result.fixed_split_stability.copy()
    metrics = [
        ("test_final_window_balanced_accuracy", "窗口平衡准确率"),
        ("test_final_window_state_accuracy", "窗口七状态准确率"),
        ("test_final_layer_balanced_accuracy", "层平衡准确率"),
        ("test_final_layer_state_accuracy", "层七状态准确率"),
        ("test_specimen_balanced_accuracy", "试样平衡准确率"),
        ("test_specimen_state_accuracy", "试样七状态准确率"),
    ]
    fig, axis = plt.subplots(figsize=(10.8, 5.8))
    positions = np.arange(len(metrics))
    values = [stability[column].to_numpy(dtype=float) for column, _ in metrics]
    box = axis.boxplot(
        values, positions=positions, widths=0.52, patch_artist=True,
        showmeans=True,
        meanprops={"marker": "D", "markerfacecolor": "#B33A3A",
                   "markeredgecolor": "#B33A3A", "markersize": 4},
    )
    for patch in box["boxes"]:
        patch.set_facecolor("#BFD7EA")
        patch.set_edgecolor("#2F5597")
    for index, scores in enumerate(values):
        jitter = np.linspace(-0.13, 0.13, len(scores))
        axis.scatter(
            np.full(len(scores), index) + jitter, scores, s=18,
            color="#2F5597", alpha=0.65, zorder=3,
        )
    axis.axhline(0.90, color="#B33A3A", linestyle="--", linewidth=1.2,
                 label="90%稳定性要求")
    axis.set_xticks(positions, [label for _, label in metrics], rotation=18, ha="right")
    axis.set_ylim(0.84, 1.01)
    axis.set_ylabel("准确率")
    axis.set_title(
        f"TC-HI固定划分重复模型种子稳定性（n={len(stability)}，无交叉验证）"
    )
    axis.grid(axis="y", color="#E5E7EB", linewidth=0.7)
    axis.legend(frameon=False, loc="lower right")
    fig.tight_layout()
    saved += save_figure(fig, figure_dir, "Fig19_TC_HI_Fixed_Split_Stability")
    plt.close(fig)

    test = result.level_metrics[result.level_metrics["dataset"].eq("test_all")].copy()
    final_values = test.set_index("level").loc[
        ["window", "layer", "specimen"], "balanced_accuracy"
    ].to_numpy(dtype=float)
    local_values = np.asarray([
        stability["test_local_window_balanced_accuracy"].iloc[0],
        stability["test_local_layer_balanced_accuracy"].iloc[0],
        final_values[2],
    ])
    x = np.arange(3)
    fig, axis = plt.subplots(figsize=(8.8, 5.4))
    axis.bar(x - 0.18, local_values, 0.36, label="局部独立初判",
             color="#9ECAE1")
    axis.bar(x + 0.18, final_values, 0.36, label="五层试样一致性后的最终预警",
             color="#2F5597")
    axis.axhline(0.90, color="#B33A3A", linestyle="--", linewidth=1.2)
    axis.set_xticks(x, ["窗口", "层", "试样"])
    axis.set_ylim(0.0, 1.08)
    axis.set_ylabel("平衡准确率")
    axis.set_title("TC-HI局部证据与最终层次化预警结果")
    axis.legend(frameon=False)
    axis.grid(axis="y", color="#E5E7EB", linewidth=0.7)
    for offset, series in [(-0.18, local_values), (0.18, final_values)]:
        for xpos, value in zip(x + offset, series):
            axis.text(xpos, value + 0.018, f"{value:.3f}",
                      ha="center", va="bottom", fontsize=9)
    fig.tight_layout()
    saved += save_figure(fig, figure_dir, "Fig20_TC_HI_Local_vs_Final_Hierarchy")
    plt.close(fig)
    return saved


def _write_latest_summary(output: Path, result, figures: list[str]) -> None:
    level = result.level_metrics.copy()
    test = level[level["dataset"].eq("test_all")].set_index("level")
    stability = result.fixed_split_stability
    summary = {
        **result.summary,
        "cross_validation_used": False,
        "fixed_split_definition": (
            "15 train specimens / 4 validation specimens / "
            "3 interpolation-test specimens / 4 extrapolation-test specimens"
        ),
        "all_reported_final_test_warning_metrics_at_least_90_percent": bool(
            min(
                float(test.loc["window", "balanced_accuracy"]),
                float(test.loc["window", "state_accuracy"]),
                float(test.loc["layer", "balanced_accuracy"]),
                float(test.loc["layer", "state_accuracy"]),
                float(test.loc["specimen", "balanced_accuracy"]),
                float(test.loc["specimen", "state_accuracy"]),
            ) >= 0.90
        ),
        "figures": figures,
    }
    (output / "TC_HI_latest_method_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    selected = result.candidate_metrics[
        result.candidate_metrics["selected_hierarchical_indicator"]
    ].copy()
    selected.insert(0, "result_version", "v13.7 fixed split")
    selected.insert(1, "cross_validation_used", False)
    selected.to_csv(
        output / "TC_HI_latest_selected_result.csv",
        index=False, encoding=OUTPUT_ENCODING,
    )

    stability_summary_rows = []
    metric_columns = [
        column for column in stability.columns
        if column.endswith("accuracy") and pd.api.types.is_numeric_dtype(stability[column])
    ]
    for column in metric_columns:
        values = stability[column].to_numpy(dtype=float)
        stability_summary_rows.append({
            "indicator": "TC-HI",
            "model": str(stability["model"].iloc[0]),
            "protocol": "fixed split, repeated estimator seeds, no cross-validation",
            "metric": column,
            "mean": float(np.mean(values)),
            "std": float(np.std(values, ddof=0)),
            "minimum": float(np.min(values)),
            "maximum": float(np.max(values)),
            "all_runs_at_least_90_percent": bool(np.min(values) >= 0.90),
        })
    pd.DataFrame(stability_summary_rows).to_csv(
        output / "TC_HI_fixed_split_stability_summary.csv",
        index=False, encoding=OUTPUT_ENCODING,
    )

    def records(frame: pd.DataFrame) -> list[dict]:
        return json.loads(frame.to_json(orient="records", force_ascii=False))

    layer_columns = [
        "layer_sample_id", "full_specimen_id", "layer", "dataset_split",
        "true_state", "local_predicted_state", "local_prediction_correct",
        "predicted_state", "prediction_correct", "layer_health_index",
        "decision_threshold", "final_decision_source", "aggregation_label",
        "compaction_event_count",
    ]
    specimen_columns = [
        "full_specimen_id", "dataset_split", "true_state", "predicted_state",
        "prediction_correct", "specimen_health_index", "decision_threshold",
        "aggregation_label", "compaction_event_count",
    ]
    z_value = 1.959963984540054
    n_test_specimens = int(result.summary["test_full_specimens"])
    denominator = 1.0 + z_value ** 2 / n_test_specimens
    centre = (1.0 + z_value ** 2 / (2.0 * n_test_specimens)) / denominator
    half_width = (
        z_value * math.sqrt(z_value ** 2 / (4.0 * n_test_specimens ** 2))
        / denominator
    )
    workbook_payload = {
        "summary": summary,
        "wilson_7_of_7_lower": centre - half_width,
        "candidates": records(result.candidate_metrics),
        "levels": records(result.level_metrics),
        "stability": records(result.fixed_split_stability),
        "stability_summary": records(pd.DataFrame(stability_summary_rows)),
        "layers": records(result.layer_results[layer_columns]),
        "specimens": records(result.specimen_results[specimen_columns]),
    }
    (output / "workbook_payload_tc_hi_v13_7.json").write_text(
        json.dumps(workbook_payload, ensure_ascii=False), encoding="utf-8"
    )

    readme = f"""# TC-HI 固定划分状态预警 v13.7

- 不使用分组交叉验证，也不使用普通交叉验证。
- 固定划分：15个训练试样、4个验证试样、3个内推测试试样、4个外推测试试样。
- 训练集拟合模型；验证集选择分类器、CAP参数与阈值；锁定测试集只用于最终评价。
- 稳定性：固定数据、状态分配与异常响应，仅改变模型随机种子，共 {len(stability)} 次。
- 局部窗口/层初判保存在 `local_*` 列；最终窗口/层状态由五层试样状态一致性约束得到。
- 所有重复是否均满足最终结构化测试预警指标不低于90%：{bool(stability['all_test_final_warning_accuracies_at_least_90_percent'].all())}。
- 所有重复是否均满足局部独立测试指标不低于90%：{bool(stability['all_test_local_warning_accuracies_at_least_90_percent'].all())}。
- 重复实验中最低的最终结构化测试准确率：{float(stability['minimum_test_final_warning_accuracy'].min()):.4f}。
- 重复实验中最低的局部独立测试准确率：{float(stability['minimum_test_local_warning_accuracy'].min()):.4f}。

注意：当前异常响应仍由正常实测数据上的AFP机理约束反事实注入得到。高准确率证明的是该仿真协议下的可分性，不能替代后续真实异常试样的独立外部验证。
"""
    (output / "README_TC_HI固定划分_v13_7.md").write_text(readme, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="TC-HI fixed-split hierarchical warning without grouped CV"
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--seed-repeats", type=int, default=30)
    parser.add_argument("--stride", type=int, default=24)
    parser.add_argument("--no-plots", action="store_true")
    options = parser.parse_args()
    output = options.output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    if not ARCHIVED_RESULT_DIR.is_dir():
        raise FileNotFoundError(
            f"Archived I-ModernTCN prediction directory is missing: {ARCHIVED_RESULT_DIR}"
        )
    train_csv = WORKSPACE / "health_split_v3_accuracy" / "train_normal.csv"
    manifest_csv = WORKSPACE / "health_split_v3_accuracy" / "split_manifest.csv"
    _, _, _, _, scaler, physics = prepare_benchmark(
        ARCHIVED_RESULT_DIR, train_csv, manifest_csv,
        options.stride, options.seed,
    )
    result = run_hierarchical_specimen_benchmark(
        result_dir=ARCHIVED_RESULT_DIR,
        split_root=train_csv.parent,
        scaler=scaler,
        bounds=physics["bounds"],
        ambient=physics["ambient"],
        output=output,
        seed=options.seed,
        stride=options.stride,
        make_plots=not options.no_plots,
        indicator_families=("TC-HI",),
        stability_repeats=options.seed_repeats,
    )
    extra_figures = [] if options.no_plots else _make_stability_figures(output, result)
    figures = list(result.figures) + extra_figures
    _write_latest_summary(output, result, figures)
    print(f"Selected: {result.selected_sensor_candidate}")
    print(f"Cross-validation used: False")
    print(f"Fixed-split repeats: {len(result.fixed_split_stability)}")
    print(
        "All repeats and final warning accuracies >= 90%: "
        f"{result.fixed_split_stability['all_test_final_warning_accuracies_at_least_90_percent'].all()}"
    )
    print(
        "Minimum final warning accuracy across repeats: "
        f"{result.fixed_split_stability['minimum_test_final_warning_accuracy'].min():.6f}"
    )
    print(f"Output: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
