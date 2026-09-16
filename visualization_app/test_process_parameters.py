from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from acquisition import (
    AcquisitionConfig,
    AcquisitionManager,
    read_real_process_parameters,
)


class ProcessParameterReaderTests(unittest.TestCase):
    def _simulation_config(self, source: Path) -> AcquisitionConfig:
        return AcquisitionConfig(
            acquisition_mode="simulation",
            simulation_source_type="single_csv",
            simulation_source_path=str(source),
            source_file=str(source),
            dataset_schema="new_collection_v11_3",
            selected_sensors=["温度"],
            model_input_sensors=["温度"],
            model_output_sensors=["温度"],
            prediction_sensors=["温度"],
            interfaces=[{
                "id": "simulator",
                "enabled": True,
                "role": "custom",
                "driver": "simulator",
                "endpoint": str(source),
            }],
        )

    def test_simulation_reads_all_four_parameters_from_first_complete_row(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "process.csv"
            source.write_text(
                "温度,initial_compaction_force_N,placement_speed_mm_s,pid_angle_deg,temperature_setpoint_C\n"
                "350,420,90,6,365\n",
                encoding="utf-8",
            )

            result = AcquisitionManager().read_process_parameters(
                self._simulation_config(source)
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["complete"])
        self.assertEqual(
            result["values"],
            {
                "initial_compaction_force_N": 420.0,
                "placement_speed_mm_s": 90.0,
                "pid_angle_deg": 6.0,
                "temperature_setpoint_C": 365.0,
            },
        )
        self.assertEqual(result["parameters"][0]["source"], "模拟数据文件")

    def test_simulation_keeps_missing_parameter_out_of_values(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "partial.csv"
            source.write_text(
                "温度,initial_compaction_force_N,placement_speed_mm_s\n"
                "350,410,85\n",
                encoding="utf-8",
            )

            result = AcquisitionManager().read_process_parameters(
                self._simulation_config(source)
            )

        self.assertTrue(result["ok"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["values"]["initial_compaction_force_N"], 410.0)
        self.assertNotIn("pid_angle_deg", result["values"])
        pid = next(item for item in result["parameters"] if item["key"] == "pid_angle_deg")
        self.assertFalse(pid["ok"])
        self.assertIn("缺少", pid["message"])

    def test_real_reader_uses_confirmed_plc_and_abb_setpoint_sources_only(self):
        interfaces = [
            {
                "id": "plc_process",
                "enabled": True,
                "role": "plc",
                "driver": "modbus_tcp",
                "endpoint": "192.168.125.5:502",
                "slave_id": 255,
            },
            {
                "id": "abb_motion",
                "enabled": True,
                "role": "robot",
                "driver": "abb_robot",
                "endpoint": "192.168.125.1",
            },
        ]

        def read_plc(_interface, address, quantity):
            self.assertEqual((address, quantity), (20, 1))
            return [70]

        def read_abb(_interface, task, module, variable):
            self.assertEqual((task, module, variable), ("T_ROB1", "MainModule", "zMovespeed"))
            return "[125,500,5000,1000]"

        result = read_real_process_parameters(
            interfaces,
            read_plc_registers=read_plc,
            read_abb_symbol=read_abb,
        )

        self.assertTrue(result["ok"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["values"]["pid_angle_deg"], 7.0)
        self.assertEqual(result["values"]["placement_speed_mm_s"], 125.0)
        self.assertNotIn("initial_compaction_force_N", result["values"])
        self.assertNotIn("temperature_setpoint_C", result["values"])
        unresolved = {
            item["key"]: item["message"]
            for item in result["parameters"]
            if not item["ok"]
        }
        self.assertIn("未配置", unresolved["initial_compaction_force_N"])
        self.assertIn("未配置", unresolved["temperature_setpoint_C"])

    def test_real_reader_keeps_other_success_when_one_controller_fails(self):
        interfaces = [
            {"id": "plc_process", "enabled": True, "role": "plc", "driver": "modbus_tcp"},
            {"id": "abb_motion", "enabled": True, "role": "robot", "driver": "abb_robot"},
        ]

        def read_plc(_interface, _address, _quantity):
            raise TimeoutError("PLC timeout")

        result = read_real_process_parameters(
            interfaces,
            read_plc_registers=read_plc,
            read_abb_symbol=lambda *_args: "[80,500,5000,1000]",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["values"], {"placement_speed_mm_s": 80.0})
        pid = next(item for item in result["parameters"] if item["key"] == "pid_angle_deg")
        self.assertFalse(pid["ok"])
        self.assertIn("PLC timeout", pid["message"])


if __name__ == "__main__":
    unittest.main()
