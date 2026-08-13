"""Command-line entry point for the native training center.

This is also a smoke-testable backend entry point. The native GUI can call the
same TrainingCenter class without any Flask/browser dependency.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from training_center import TrainingCenter


def main() -> int:
    parser = argparse.ArgumentParser(description="AFP 原生模型训练中心")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="Excel/CSV 文件或文件夹")
    source.add_argument("--demo", action="store_true", help="使用内置新数据集进行导入预检")
    parser.add_argument("--output", type=Path, default=Path("trained_models"))
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--patience", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--learning-rate", type=float, default=8e-4)
    parser.add_argument("--device", default="auto")
    args = parser.parse_args()
    events: list[dict] = []
    center = TrainingCenter(args.output, events.append)
    if args.demo:
        source_path = Path(__file__).resolve().parent / "new_collection_demo_v11_3" / "raw"
    else:
        source_path = args.input.resolve()
    result = center.import_files(source_path)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    dataset = center.prepare_dataset(args.output / "dataset")
    print(json.dumps({"dataset": str(dataset), "validation": center.last_validation}, ensure_ascii=False, indent=2, default=str))
    if not args.prepare_only:
        center.start_training(dataset, args.epochs, args.patience, args.batch_size, args.learning_rate, args.device)
        assert center._thread is not None
        center._thread.join()
    print(json.dumps(events[-1] if events else {}, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
