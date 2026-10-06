from __future__ import annotations

import json
import socket
import subprocess
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


CLIENT_LAB_DIR = Path(__file__).resolve().parent
WSB_TEMPLATE = CLIENT_LAB_DIR / "AFP-Client-Validation.wsb.template"
STAGE_SCRIPT = CLIENT_LAB_DIR / "New-ClientLabStage.ps1"
INITIALIZE_SCRIPT = CLIENT_LAB_DIR / "Initialize-ClientLab.ps1"
TCP_SENSOR_SCRIPT = CLIENT_LAB_DIR / "Start-VirtualTcpSensor.ps1"
EVIDENCE_SCRIPT = CLIENT_LAB_DIR / "Export-ClientLabEvidence.ps1"
MYSQL_TEMPLATE = CLIENT_LAB_DIR / "lab-mysql.ini.template"


def run_powershell(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(STAGE_SCRIPT),
            *arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8-sig",
    )


def run_script(script: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8-sig",
    )


class ClientLabAssetContractTests(unittest.TestCase):
    def test_wsb_template_enforces_resource_and_redirection_boundaries(self) -> None:
        """Catches an unsafe Sandbox template that exposes host resources."""
        root = ET.parse(WSB_TEMPLATE).getroot()

        self.assertEqual(root.findtext("MemoryInMB"), "4096")
        self.assertEqual(root.findtext("Networking"), "Enable")
        self.assertEqual(root.findtext("VGpu"), "Disable")
        self.assertEqual(root.findtext("AudioInput"), "Disable")
        self.assertEqual(root.findtext("VideoInput"), "Disable")
        self.assertEqual(root.findtext("PrinterRedirection"), "Disable")
        self.assertEqual(root.findtext("ClipboardRedirection"), "Disable")

        mappings = root.findall("./MappedFolders/MappedFolder")
        self.assertEqual(len(mappings), 2)
        by_sandbox_path = {
            item.findtext("SandboxFolder"): item for item in mappings
        }
        self.assertEqual(
            by_sandbox_path["C:\\AFP-Lab\\Input"].findtext("ReadOnly").lower(),
            "true",
        )
        self.assertEqual(
            by_sandbox_path["C:\\AFP-Lab\\Results"].findtext("ReadOnly").lower(),
            "false",
        )
        self.assertEqual(
            root.findtext("./LogonCommand/Command"),
            "powershell.exe -NoProfile -ExecutionPolicy Bypass -File "
            "C:\\AFP-Lab\\Input\\scripts\\Initialize-ClientLab.ps1",
        )

    def test_stage_contract_pins_inputs_and_allows_only_minimal_mysql_runtime(self) -> None:
        """Catches a staging change that copies mutable data or weakens hash pins."""
        completed = run_powershell("-DescribeContract")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        contract = json.loads(completed.stdout)
        self.assertEqual(contract["lab_root"], "F:\\AFP_Client_Validation_Lab")
        self.assertEqual(
            contract["helper_sha256"],
            "5216A6D110164BB23D8D1B2D29DBB335D998DA1DB176CB227339054586BCA5D6",
        )
        self.assertEqual(
            contract["fixture_sha256"],
            "58C9290EFD79B8967DD4806FE9CA13B15B4E5508835D3F019A5E943BADD78267",
        )
        self.assertEqual(
            contract["mysql_entries"], ["bin", "lib", "share", "LICENSE"]
        )
        self.assertEqual(
            contract["prerequisite_entries"], ["vc_redist.x64.exe"]
        )
        self.assertTrue(contract["prerequisite_signature_required"])
        self.assertEqual(
            contract["excluded_names"],
            [
                "data",
                "my.ini",
                "runtime",
                "logs",
                "*.sqlite3",
                "config.json",
            ],
        )

    def test_stage_rejects_wrong_helper_hash_before_creating_lab(self) -> None:
        """Catches accidental or substituted helper binaries before any staging write."""
        with tempfile.TemporaryDirectory() as temp_dir:
            bad_helper = Path(temp_dir) / "AFP_Local_Capture_Helper.exe"
            bad_helper.write_bytes(b"not-the-pinned-helper")
            completed = run_powershell("-HelperSource", str(bad_helper))

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("helper SHA-256 mismatch", completed.stderr + completed.stdout)

    def test_mysql_template_is_loopback_only_and_utf8mb4(self) -> None:
        """Catches accidental exposure of the Sandbox database to the network."""
        settings = {}
        for raw_line in MYSQL_TEMPLATE.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("#", ";", "[")):
                continue
            key, value = line.split("=", 1)
            settings[key.strip()] = value.strip()

        self.assertEqual(settings["bind-address"], "127.0.0.1")
        self.assertEqual(settings["port"], "3306")
        self.assertEqual(settings["character-set-server"], "utf8mb4")
        self.assertEqual(settings["skip-name-resolve"], "1")

    def test_initialization_contract_uses_clean_mysql_tree(self) -> None:
        """Catches reuse of the host database or export of generated credentials."""
        completed = run_script(INITIALIZE_SCRIPT, "-DescribeContract")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        contract = json.loads(completed.stdout)
        self.assertEqual(contract["mysql_entries"], ["bin", "lib", "share", "LICENSE"])
        self.assertEqual(contract["mysql_host"], "127.0.0.1")
        self.assertEqual(contract["mysql_port"], 3306)
        self.assertEqual(contract["database"], "afp_state_warning")
        self.assertFalse(contract["credentials_exported"])
        self.assertEqual(contract["vc_runtime_prerequisite"], "vc_redist.x64.exe")
        self.assertTrue(contract["vc_runtime_signature_required"])

    def test_initialization_does_not_treat_mysql_console_output_as_powershell_error(self) -> None:
        """Catches direct native invocation that turns normal MySQL stderr into a stop error."""
        script = INITIALIZE_SCRIPT.read_text(encoding="utf-8")

        self.assertNotIn(
            '& $mysqld "--defaults-file=$mysqlIni" "--initialize-insecure" "--console"',
            script,
        )
        self.assertIn("-RedirectStandardError $mysqlInitializeError", script)
        self.assertIn("-RedirectStandardOutput $mysqlInitializeOutput", script)

    def test_initialization_bootstraps_loopback_account_without_root_tcp_login(self) -> None:
        """Catches root@localhost login attempts that fail with skip-name-resolve enabled."""
        script = INITIALIZE_SCRIPT.read_text(encoding="utf-8")

        self.assertIn('"--init-file=$mysqlBootstrap"', script)
        self.assertNotIn(
            '& $mysql "--protocol=TCP" "--host=127.0.0.1" "--port=3306" "--user=root"',
            script,
        )
        self.assertIn("MySQL loopback application account verification failed", script)

    def test_initialization_grants_references_for_helper_created_foreign_keys(self) -> None:
        """The helper's first-use schema creation must be allowed to add foreign keys."""
        script = INITIALIZE_SCRIPT.read_text(encoding="utf-8")

        self.assertRegex(
            script,
            r"GRANT\s+SELECT,\s*INSERT,\s*UPDATE,\s*DELETE,\s*CREATE,\s*ALTER,\s*INDEX,\s*REFERENCES,\s*CREATE VIEW,\s*SHOW VIEW,\s*DROP\s+ON",
        )

    def test_initialization_uses_sandbox_available_ui_and_idempotent_helper_start(self) -> None:
        """Catches assumptions that Store Notepad or PATH-resolved Edge exists in Sandbox."""
        script = INITIALIZE_SCRIPT.read_text(encoding="utf-8")

        self.assertNotIn('Start-Process -FilePath "notepad.exe"', script)
        self.assertIn('C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe', script)
        self.assertIn('$existingHelper = Get-Process -Name "AFP_Local_Capture_Helper"', script)
        self.assertIn('$existingExporter = Get-CimInstance Win32_Process', script)

    def test_tcp_sensor_emits_deterministic_json_lines_and_counter_only_evidence(self) -> None:
        """Catches wrong row counts, non-loopback output, or evidence containing samples."""
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]

        with tempfile.TemporaryDirectory() as temp_dir:
            evidence_path = Path(temp_dir) / "feeder-status.json"
            process = subprocess.Popen(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(TCP_SENSOR_SCRIPT),
                    "-Port",
                    str(port),
                    "-RateHz",
                    "100",
                    "-TotalRows",
                    "5",
                    "-EvidencePath",
                    str(evidence_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8-sig",
            )
            client = socket.socket()
            try:
                deadline = time.monotonic() + 5
                while True:
                    try:
                        client.connect(("127.0.0.1", port))
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            self.fail("virtual TCP sensor did not start listening")
                        time.sleep(0.05)
                with client.makefile("r", encoding="utf-8") as stream:
                    rows = [json.loads(stream.readline()) for _ in range(5)]
            finally:
                client.close()
            stdout, stderr = process.communicate(timeout=10)

            self.assertEqual(process.returncode, 0, stderr or stdout)
            self.assertEqual([row["sequence"] for row in rows], [1, 2, 3, 4, 5])
            self.assertTrue(all(row["synthetic_client_lab"] is True for row in rows))
            self.assertTrue(all(len(row["channels"]) == 17 for row in rows))
            self.assertTrue(all("defect" not in json.dumps(row).lower() for row in rows))
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            self.assertEqual(evidence["produced_rows"], 5)
            self.assertEqual(evidence["last_sequence"], 5)
            self.assertNotIn("channels", evidence)
            self.assertNotIn("samples", evidence)

    def test_evidence_exporter_writes_one_redacted_metric_record(self) -> None:
        """Catches invalid JSONL output or accidental credential/sample export."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            feeder = root / "feeder-status.json"
            feeder.write_text(
                json.dumps({"produced_rows": 12, "last_sequence": 12}),
                encoding="utf-8",
            )
            completed = run_script(
                EVIDENCE_SCRIPT,
                "-ResultsRoot",
                str(root),
                "-FeederEvidencePath",
                str(feeder),
                "-Once",
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = root / "minute-metrics.jsonl"
            records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["feeder_produced_count"], 12)
            self.assertIn("helper_running", records[0])
            self.assertIn("mysql_row_count", records[0])
            self.assertIn("csv_row_count", records[0])
            serialized = json.dumps(records[0]).lower()
            for forbidden in ("password", "pairing_token", "api_key", "private_key"):
                self.assertNotIn(forbidden, serialized)

    def test_runtime_assets_do_not_embed_host_state_or_secret_fields(self) -> None:
        """Catches copied host state or reusable secret fields in staged scripts."""
        assets = [
            INITIALIZE_SCRIPT,
            TCP_SENSOR_SCRIPT,
            EVIDENCE_SCRIPT,
            CLIENT_LAB_DIR / "Invoke-HelperNetworkInterruption.ps1",
            MYSQL_TEMPLATE,
        ]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in assets).lower()
        for forbidden in (
            "server_target_password",
            "browser_password",
            "pairing_token",
            "openai_api_key=",
            "f:\\softwawre\\mysql\\data",
            "f:\\afp_integrated_modular_v2\\delivery",
            "config.json",
        ):
            self.assertNotIn(forbidden, combined)


if __name__ == "__main__":
    unittest.main()
