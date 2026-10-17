from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

import acquisition  # noqa: E402
from real_acquisition import (  # noqa: E402
    ChannelQuality,
    ChannelSample,
    UnifiedFrame,
    UnifiedFrameV2Receiver,
)
from thermocouple64 import (  # noqa: E402
    THERMOCOUPLE64_CHANNELS,
    THERMOCOUPLE64_MODE_ID,
    decode_derived_realtime_payload,
    derived_catalog,
    encode_derived_realtime_payload,
    raw_catalog,
    strict_group_means,
)


class Thermocouple64TransportTests(unittest.TestCase):
    def _catalog(self) -> dict:
        channels = acquisition.CURRENT_COLLECTION_SENSOR_COLUMNS
        units = {
            name: str((acquisition.SENSOR_CHANNEL_METADATA.get(name) or {}).get("unit") or "")
            for name in channels
        }
        return raw_catalog(channels, units)

    def _frame(self) -> UnifiedFrame:
        channels = {}
        for index, name in enumerate(acquisition.CURRENT_COLLECTION_SENSOR_COLUMNS, start=1):
            channels[name] = ChannelSample(
                interface_id="fixture",
                channel_name=name,
                value=float(index),
                quality=ChannelQuality.MEASURED_NEW,
                device_timestamp=100.0,
                received_wall_time=100.1,
                received_monotonic=50.0,
                source_sequence=3,
                protocol_ok=True,
            )
        return UnifiedFrame(
            capture_sequence=5,
            target_monotonic=50.0,
            target_wall_time=100.1,
            channels=channels,
            assembled_monotonic=50.01,
        )

    def test_v2_round_trip_restores_all_77_channels(self) -> None:
        catalog = self._catalog()
        envelope = self._frame().to_envelope_v2(
            session_id="session-1", mode_id=THERMOCOUPLE64_MODE_ID, catalog=catalog
        )
        restored = UnifiedFrame.from_envelope_v2(
            envelope,
            expected_session_id="session-1",
            expected_mode_id=THERMOCOUPLE64_MODE_ID,
            catalog=catalog,
        )
        self.assertEqual(len(restored.channels), 77)
        self.assertEqual(restored.values(), self._frame().values())
        self.assertEqual(restored.quality(), self._frame().quality())
        self.assertEqual(restored.channels["温度64"].source_sequence, 3)

    def test_receiver_rejects_unknown_catalog_lengths_duplicates_and_out_of_order(self) -> None:
        catalog = self._catalog()
        envelope = self._frame().to_envelope_v2(
            session_id="session-1", mode_id=THERMOCOUPLE64_MODE_ID, catalog=catalog
        )
        receiver = UnifiedFrameV2Receiver("session-1", THERMOCOUPLE64_MODE_ID, catalog)
        receiver.accept(envelope)
        for field, value, message in (
            ("catalog_id", "unknown", "catalog_id"),
            ("session_id", "other", "session_id"),
            ("mode_id", "other", "mode_id"),
        ):
            invalid = {**envelope, field: value, "frame_sequence": 6}
            with self.assertRaisesRegex(ValueError, message):
                receiver.accept(invalid)
        short = {**envelope, "frame_sequence": 6, "values": envelope["values"][:-1]}
        with self.assertRaisesRegex(ValueError, "length"):
            receiver.accept(short)
        long = {**envelope, "frame_sequence": 6, "values": [*envelope["values"], 1.0]}
        with self.assertRaisesRegex(ValueError, "length"):
            receiver.accept(long)
        with self.assertRaisesRegex(ValueError, "duplicate or out-of-order"):
            receiver.accept(envelope)
        older = {**envelope, "frame_sequence": 4}
        with self.assertRaisesRegex(ValueError, "duplicate or out-of-order"):
            receiver.accept(older)

    def test_rejected_frame_does_not_advance_receiver_state(self) -> None:
        catalog = self._catalog()
        receiver = UnifiedFrameV2Receiver("session-1", THERMOCOUPLE64_MODE_ID, catalog)
        envelope = self._frame().to_envelope_v2(
            session_id="session-1", mode_id=THERMOCOUPLE64_MODE_ID, catalog=catalog
        )
        invalid = copy.deepcopy(envelope)
        invalid["values"][0] = float("nan")
        with self.assertRaisesRegex(ValueError, "non-finite"):
            receiver.accept(invalid)
        self.assertIsNone(receiver.last_sequence)
        receiver.accept(envelope)
        self.assertEqual(receiver.last_sequence, 5)

    def test_derived_payload_is_separate_from_raw_catalog(self) -> None:
        values = {name: float(index) for index, name in enumerate(THERMOCOUPLE64_CHANNELS, start=1)}
        means = strict_group_means(values)
        catalog = derived_catalog("causal_so_far")
        payload = encode_derived_realtime_payload(sequence=9, catalog=catalog, means=means)
        restored = decode_derived_realtime_payload(payload, catalog)
        self.assertEqual(restored["热电偶均值1"]["value"], 4.5)
        self.assertEqual(restored["热电偶均值8"]["valid_count"], 8)
        with self.assertRaisesRegex(ValueError, "derived catalog"):
            encode_derived_realtime_payload(sequence=9, catalog=self._catalog(), means=means)

    def test_v1_history_contract_remains_readable(self) -> None:
        frame = self._frame()
        envelope = frame.to_envelope("capture-legacy")
        restored = UnifiedFrame.from_envelope(envelope)
        self.assertEqual(restored.capture_sequence, frame.capture_sequence)
        self.assertEqual(restored.values(), frame.values())


if __name__ == "__main__":
    unittest.main()
