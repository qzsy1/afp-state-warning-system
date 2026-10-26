"""Compare ESR1001 startup waits without changing instrument parameters."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "visualization_app"))
import esr1001_usb as usb


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--idle-seconds", type=float, default=0)
    parser.add_argument("--port-settle", type=float, default=0)
    parser.add_argument("--start-settle", type=float, default=0)
    parser.add_argument("--policy", choices=("fixed", "bounded_response"), default="fixed")
    parser.add_argument("--read-pause", type=float, default=0)
    parser.add_argument("--stable-observations", type=int, default=1)
    parser.add_argument("--candidate-first", action="store_true")
    args = parser.parse_args()
    if args.output.resolve().drive.upper() != "D:":
        raise ValueError("Evidence must be on D:")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    original = usb.PORT_SETTLE_SECONDS, usb.START_SETTLE_SECONDS
    evidence = []
    try:
        for round_id in range(args.rounds):
            strategies = (("fixed", original), ("bounded_response", (args.port_settle, args.start_settle)))
            if args.candidate_first:
                strategies = tuple(reversed(strategies))
            for strategy, waits in strategies:
                if args.idle_seconds:
                    time.sleep(args.idle_seconds)
                usb.PORT_SETTLE_SECONDS, usb.START_SETTLE_SECONDS = waits
                driver = usb.Esr1001UsbDriver(args.endpoint, startup_policy=args.policy if strategy == "bounded_response" else "fixed")
                started = time.monotonic()
                row = dict(round=round_id, strategy=strategy, waits=waits, started_monotonic=started, observations=[], read_pause=args.read_pause)
                evidence.append(row)
                try:
                    driver.open()
                    deadline = started + 10
                    stable_sequences = set()
                    while time.monotonic() < deadline:
                        records = driver.read_samples()
                        row["observations"].extend(record for _, record in records)
                        if records and usb.current_stream_is_stable(driver, records[-1][1]["host_monotonic"]):
                            stable_sequences.update(record["receive_sequence"] for _, record in records)
                            if len(stable_sequences) >= args.stable_observations:
                                row.update(stable=True, elapsed_seconds=time.monotonic() - started,
                                           stable_sequences=sorted(stable_sequences))
                                break
                        time.sleep(args.read_pause)
                    else:
                        row["stable"] = False
                except Exception as exc:
                    row.update(stable=False, error=str(exc))
                finally:
                    try:
                        driver.close()
                    except Exception as exc:
                        row["close_error"] = str(exc)
                    row.update(events=driver.start_events, metadata=driver.quality_metadata,
                               commands=list(driver.command_evidence))
                    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({k: row[k] for k in ("round", "strategy", "stable", "elapsed_seconds", "error") if k in row}), flush=True)
    finally:
        usb.PORT_SETTLE_SECONDS, usb.START_SETTLE_SECONDS = original


if __name__ == "__main__":
    main()
