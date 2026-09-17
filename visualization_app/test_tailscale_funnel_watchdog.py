from pathlib import Path
import unittest


class TailscaleFunnelWatchdogContractTests(unittest.TestCase):
    def setUp(self):
        self.source = Path(__file__).with_name("tailscale_funnel_watchdog.ps1")

    def test_watchdog_uses_persistent_funnel_for_existing_origin(self):
        self.assertEqual(self.source.read_bytes()[:3], b"\xef\xbb\xbf")
        text = self.source.read_text(encoding="utf-8-sig")
        self.assertIn("tailscale.exe", text)
        self.assertIn("funnel --bg --https=443 --yes http://127.0.0.1:8770", text)
        self.assertIn("127.0.0.1:8770", text)
        self.assertNotIn("trycloudflare", text)

    def test_watchdog_derives_and_persists_stable_tsnet_url(self):
        text = self.source.read_text(encoding="utf-8-sig")
        self.assertIn("status --json", text)
        self.assertIn(".ts.net", text)
        self.assertIn("funnel-url.txt", text)
        self.assertIn("-Encoding ASCII", text)

    def test_watchdog_requires_origin_funnel_and_public_health(self):
        text = self.source.read_text(encoding="utf-8-sig")
        self.assertIn("function Test-Origin", text)
        self.assertIn("function Test-Funnel", text)
        self.assertIn("function Test-PublicFunnel", text)
        self.assertIn('/api/health', text)

    def test_watchdog_keeps_app_helper_and_funnel_available_after_logon(self):
        text = self.source.read_text(encoding="utf-8-sig")
        self.assertIn("AFP_Integrated_System_Modular.exe", text)
        self.assertIn("AFP_Local_Capture_Helper.exe", text)
        self.assertIn("function Ensure-Helper", text)
        self.assertIn("New-ScheduledTaskTrigger -AtLogOn", text)
        self.assertIn("CurrentVersion\\Run", text)

    def test_watchdog_starts_origin_without_opening_a_second_desktop_window(self):
        text = self.source.read_text(encoding="utf-8-sig")
        self.assertIn('-ArgumentList "--server-only"', text)

    def test_watchdog_records_actionable_authentication_state(self):
        text = self.source.read_text(encoding="utf-8-sig")
        self.assertIn("funnel-status.json", text)
        self.assertIn("tailscale_login_required", text)
        self.assertIn("funnel_approval_required", text)

    def test_first_funnel_approval_wait_is_bounded(self):
        text = self.source.read_text(encoding="utf-8-sig")
        self.assertIn("WaitForExit", text)
        self.assertIn("Stop-Process -Id $process.Id -Force", text)

    def test_public_health_has_windows_tls_fallback(self):
        text = self.source.read_text(encoding="utf-8-sig")
        self.assertIn("curl.exe", text)
        self.assertIn("--fail", text)
        self.assertIn("--max-time", text)

    def test_helper_build_updates_watchdog_in_existing_delivery(self):
        build = Path(__file__).with_name("build_local_capture_helper.ps1")
        text = build.read_text(encoding="utf-8-sig")
        self.assertIn("tailscale_funnel_watchdog.ps1", text)
        self.assertIn("Copy-Item", text)


if __name__ == "__main__":
    unittest.main()
