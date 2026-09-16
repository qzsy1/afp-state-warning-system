from pathlib import Path
import unittest


class PublicTunnelWatchdogContractTests(unittest.TestCase):
    def test_watchdog_checks_origin_and_tunnel_health_before_restarting(self):
        source = Path(__file__).with_name("public_tunnel_watchdog.ps1")
        text = source.read_text(encoding="utf-8-sig")
        self.assertIn("/api/health", text)
        self.assertIn("20241/metrics", text)
        self.assertIn("cloudflared_tunnel_ha_connections", text)
        self.assertIn("Start-Process", text)
        self.assertIn("127.0.0.1:8770", text)

    def test_watchdog_registers_logon_and_startup_recovery(self):
        source = Path(__file__).with_name("public_tunnel_watchdog.ps1")
        text = source.read_text(encoding="utf-8-sig")
        self.assertIn("New-ScheduledTaskTrigger -AtLogOn", text)
        self.assertIn("New-ScheduledTaskTrigger -AtStartup", text)
        self.assertIn("Register-ScheduledTask", text)
        self.assertIn("Unregister-ScheduledTask", text)
        self.assertIn("CurrentVersion\\Run", text)
        self.assertIn("Set-ItemProperty", text)

    def test_watchdog_persists_current_quick_tunnel_url(self):
        source = Path(__file__).with_name("public_tunnel_watchdog.ps1")
        text = source.read_text(encoding="utf-8-sig")
        self.assertIn("trycloudflare", text)
        self.assertIn("quick-tunnel-url.txt", text)


if __name__ == "__main__":
    unittest.main()
