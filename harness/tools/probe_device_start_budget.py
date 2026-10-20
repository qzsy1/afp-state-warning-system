"""Record read-only TC64 scans and PLC/ABB route validation on this host."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "visualization_app"))
from acquisition import require_network_path, _network_adapter_snapshot
from hp_thermocouple64 import HpThermocouple64Driver


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--tc64", default="COM7")
    parser.add_argument("--interface", default="ethernet:以太网")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve().drive.upper() != "D:":
        raise ValueError("Evidence must be on D:")
    result = {"network": [], "tc64": []}
    for repeat in range(2):
        started = time.perf_counter()
        adapters = _network_adapter_snapshot()
        enumeration = time.perf_counter() - started
        for role, endpoint in (("plc", "192.168.125.5:502"), ("robot", "192.168.125.1")):
            started = time.perf_counter()
            path = require_network_path(dict(role=role, endpoint=endpoint, physical_interface_id=args.interface))
            result["network"].append(dict(repeat=repeat, enumeration_seconds=enumeration,
                                          path_seconds=time.perf_counter() - started, path=path, adapters=adapters))
    driver = HpThermocouple64Driver(args.tc64, read_profile="full_record_block")
    started = time.monotonic()
    try:
        driver.open()
        for scan in range(2):
            values = driver.read_sample()
            result["tc64"].append(dict(scan=scan, elapsed_seconds=time.monotonic() - started,
                                       values=values, metadata=dict(driver.quality_metadata)))
    finally:
        driver.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(dict(network=[r["path_seconds"] for r in result["network"]],
                          tc64=[r["elapsed_seconds"] for r in result["tc64"]])))


if __name__ == "__main__":
    main()
