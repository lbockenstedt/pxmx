"""Tests for PXMX drive health monitoring and spoke routing."""

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure pxmx/src and pxmx/agent/src are importable
PXMX_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(PXMX_DIR, "src")
AGENT_SRC_DIR = os.path.join(PXMX_DIR, "agent", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
if AGENT_SRC_DIR not in sys.path:
    sys.path.insert(0, AGENT_SRC_DIR)

import drive_health


SAMSUNG_SMARTCTL_OUTPUT = """
smartctl 7.2 2020-12-30 r5155 [x86_64-linux-5.15.0-46-generic]
Device Model:     Samsung SSD 860 EVO 500GB
Serial Number:    S2L9NY0M123456
Firmware Version: RLT03B6Q
User Capacity:    500,107,862,016 bytes [500 GB]
SMART Health Status: PASSED

SMART Attributes Data Structure:
ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE
  5 Reallocated_Sector_Ct   0x0033   100   100   010    Pre-fail  Always       -       0
  9 Power_On_Hours          0x0032   098   098   000    Old_age   Always       -       1234
 12 Power_Cycle_Count       0x0032   099   099   000    Old_age   Always       -       56
177 Wear_Leveling_Count     0x0013   085   085   000    Pre-fail  Always       -       15
194 Temperature_Celsius     0x0022   065   055   000    Old_age   Always       -       35
"""

NVME_SMARTCTL_OUTPUT = """
smartctl 7.2 2020-12-30 r5155 [x86_64-linux-5.15.0-46-generic]
Model Number:                       INTEL SSDPE2KX512G8
Serial Number:                      PHKE12345678
Firmware Version:                   VDV10170
Critical Warning:                   0x00
Temperature:                        38 Celsius
Available Spare:                    100%
Percentage Used:                    7%
Data Units Read:                    1,234,567 [632 GB]
Data Units Written:                 987,654 [505 GB]
"""

SAS_SMARTCTL_OUTPUT = """
smartctl 7.2 2020-12-30 r5155 [x86_64-linux-5.15.0-46-generic]
Vendor:               HP
Product:              MO0400KEFHN
Revision:             HPDA
Serial number:        S123456789
Percentage used endurance indicator: 2%
Current Drive Temperature:     33 C
SMART Health Status: OK
"""

CRITICAL_WEAR_OUTPUT = """
Device Model:     Old Crucial CT500MX500SSD1
Serial Number:    1845E1234567
177 Wear_Leveling_Count     0x0013   015   015   000    Pre-fail  Always       -       85
"""

WARNING_WEAR_OUTPUT = """
Device Model:     Midlife SSD
Serial Number:    MID1234567
Wear_Leveling_Count = 72
"""

LSSCSI_SAMPLE_OUTPUT = """
[0:0:0:0]    disk    HP       LOGICAL VOLUME   5.04  /dev/sda   /dev/sg0
[0:0:0:1]    disk    Samsung  SSD 860          RLT0  /dev/sdb   /dev/sg1
[1:0:0:0]    disk    NVMe     INTEL SSD        VDV1  /dev/nvme0n1 /dev/sg2
"""


class TestDriveHealthParsing:
    """Test smartctl attribute parsing across different drive vendors."""

    def test_samsung_sata_wear_parsing(self):
        wear = drive_health.parse_smartctl_wear(SAMSUNG_SMARTCTL_OUTPUT)
        assert wear == 15

    def test_nvme_wear_parsing(self):
        wear = drive_health.parse_smartctl_wear(NVME_SMARTCTL_OUTPUT)
        assert wear == 7

    def test_sas_endurance_parsing(self):
        wear = drive_health.parse_smartctl_wear(SAS_SMARTCTL_OUTPUT)
        assert wear == 2

    def test_wd_percentage_used_parsing(self):
        text = "PercentageUsed = 34"
        assert drive_health.parse_smartctl_wear(text) == 34

    def test_percentage_available_parsing(self):
        text = "PercentageAvailable = 85"
        assert drive_health.parse_smartctl_wear(text) == 15

    def test_available_spare_parsing(self):
        text = "Available Spare: 92%"
        assert drive_health.parse_smartctl_wear(text) == 8

    def test_legacy_kv_wear_parsing(self):
        text = "Wear_Leveling_Count = 45"
        assert drive_health.parse_smartctl_wear(text) == 45


@pytest.mark.asyncio
class TestSmartctlExecution:
    """Test get_smartctl_info with execution, fallback, and status thresholds."""

    async def test_smartctl_not_installed(self):
        with patch.object(drive_health, "check_smartctl_installed", return_value=False):
            info = await drive_health.get_smartctl_info("/dev/sda")
            assert info["success"] is False
            assert info["error"] == "smartctl not installed"

    async def test_cciss_fallback_to_auto(self):
        with patch.object(drive_health, "check_smartctl_installed", return_value=True):
            # First call (-d cciss) fails, second call (-a) succeeds
            fail_mock = MagicMock(returncode=1, stdout="", stderr="invalid device type cciss")
            succ_mock = MagicMock(returncode=0, stdout=SAMSUNG_SMARTCTL_OUTPUT, stderr="")
            with patch("subprocess.run", side_effect=[fail_mock, succ_mock]) as mock_run:
                info = await drive_health.get_smartctl_info("/dev/sda")
                assert mock_run.call_count == 2
                assert info["success"] is True
                assert info["wear_leveling_count"] == 15
                assert info["health_status"] == "healthy"
                assert info["model"] == "Samsung SSD 860 EVO 500GB"
                assert info["serial"] == "S2L9NY0M123456"

    async def test_critical_wear_threshold(self):
        with patch.object(drive_health, "check_smartctl_installed", return_value=True):
            succ_mock = MagicMock(returncode=0, stdout=CRITICAL_WEAR_OUTPUT, stderr="")
            with patch("subprocess.run", return_value=succ_mock):
                info = await drive_health.get_smartctl_info("/dev/sda")
                assert info["success"] is True
                assert info["wear_leveling_count"] == 85
                assert info["health_status"] == "critical"

    async def test_warning_wear_threshold(self):
        with patch.object(drive_health, "check_smartctl_installed", return_value=True):
            succ_mock = MagicMock(returncode=0, stdout=WARNING_WEAR_OUTPUT, stderr="")
            with patch("subprocess.run", return_value=succ_mock):
                info = await drive_health.get_smartctl_info("/dev/sda")
                assert info["success"] is True
                assert info["wear_leveling_count"] == 72
                assert info["health_status"] == "warning"


@pytest.mark.asyncio
class TestDeviceDiscovery:
    """Test device discovery with lsscsi and /sys/block fallback."""

    async def test_lsscsi_discovery(self):
        proc_mock = MagicMock(returncode=0, stdout=LSSCSI_SAMPLE_OUTPUT, stderr="")
        with patch("subprocess.run", return_value=proc_mock):
            devices = await drive_health.get_scsi_devices()
            assert len(devices) == 3
            assert devices[0]["block_device"] == "/dev/sda"
            assert devices[1]["block_device"] == "/dev/sdb"
            assert devices[2]["block_device"] == "/dev/nvme0n1"

    async def test_sys_block_fallback(self):
        fail_mock = MagicMock(returncode=1, stdout="", stderr="lsscsi: not found")
        with patch("subprocess.run", return_value=fail_mock), \
             patch("os.path.exists", return_value=True), \
             patch("os.listdir", return_value=["sda", "sda1", "sdb", "nvme0n1", "loop0", "ram0"]):
            devices = await drive_health.get_scsi_devices()
            block_devs = [d["block_device"] for d in devices]
            assert "/dev/sda" in block_devs
            assert "/dev/sdb" in block_devs
            assert "/dev/nvme0n1" in block_devs
            assert "/dev/sda1" not in block_devs
            assert "/dev/loop0" not in block_devs


@pytest.mark.asyncio
class TestUIOutputAndAlerts:
    """Test get_drive_health_for_ui aggregate outputs and alerts."""

    async def test_ui_alerts_and_summary(self):
        mock_devices = [
            {"block_device": "/dev/sda", "index": 0, "vendor": "Samsung", "model": "860", "serial": "S1"},
            {"block_device": "/dev/sdb", "index": 1, "vendor": "Crucial", "model": "MX500", "serial": "S2"},
            {"block_device": "/dev/nvme0n1", "index": 2, "vendor": "Intel", "model": "NVMe", "serial": "S3"}
        ]
        info_healthy = {"success": True, "wear_leveling_count": 10, "health_status": "healthy", "model": "860", "serial": "S1", "error": None}
        info_warning = {"success": True, "wear_leveling_count": 65, "health_status": "warning", "model": "MX500", "serial": "S2", "error": None}
        info_critical = {"success": True, "wear_leveling_count": 88, "health_status": "critical", "model": "NVMe", "serial": "S3", "error": None}

        with patch.object(drive_health, "get_scsi_devices", return_value=mock_devices), \
             patch.object(drive_health, "get_smartctl_info", side_effect=[info_healthy, info_warning, info_critical]), \
             patch.object(drive_health, "get_historical_trends", return_value={"drives": {}}):
            ui_data = await drive_health.get_drive_health_for_ui()
            assert len(ui_data["drives"]) == 3
            summary = ui_data["summary"]
            assert summary["total_drives"] == 3
            assert summary["healthy_drives"] == 1
            assert summary["warning_drives"] == 1
            assert summary["critical_drives"] == 1

            alerts = ui_data["alerts"]
            assert len(alerts) == 2
            alert_types = [a["alert_type"] for a in alerts]
            assert "warning" in alert_types
            assert "critical" in alert_types


class TestHistoryAndTrends:
    """Test history persistence and trend analysis."""

    def test_save_and_load_history(self, tmp_path):
        test_history_file = str(tmp_path / "test_history.json")
        with patch.object(drive_health, "HISTORY_FILE", test_history_file):
            fake_health = {
                "drives": [{"physical_index": 0, "wear_level": 15, "health_status": "healthy"}],
                "summary": {"total_drives": 1, "healthy_drives": 1}
            }
            assert drive_health.save_history(fake_health) is True
            loaded = drive_health.load_history()
            assert len(loaded.get("snapshots", [])) == 1

            # Test 100 snapshot limit
            for i in range(110):
                fake_health["drives"][0]["wear_level"] = 15 + (i % 10)
                drive_health.save_history(fake_health)

            loaded_capped = drive_health.load_history()
            assert len(loaded_capped.get("snapshots", [])) == 100


@pytest.mark.asyncio
class TestSpokeRouting:
    """Test spoke command routing for drive health commands."""

    async def test_spoke_drive_health_routing(self):
        from proxmox_spoke import ProxmoxSpoke

        fake_cp = MagicMock()
        fake_cp.connected_agents = {
            "agent-node1": {"cluster_name": "lab-cluster", "nodes": ["pve1"]}
        }
        fake_cp.send_to_agent = AsyncMock(return_value={
            "payload": {
                "data": {
                    "status": "SUCCESS",
                    "drives": [{"physical_index": 0, "wear_level": 12, "health_status": "healthy"}]
                }
            }
        })

        spoke = ProxmoxSpoke("px-1", {}, control_plane=fake_cp)
        with patch.object(spoke, "_agent_for_node", return_value="agent-node1"):
            result = await spoke.handle_command("PXMX_DRIVE_HEALTH", {"node": "pve1"})
            assert result["status"] == "SUCCESS"
            assert result["cluster"] == "lab-cluster"
            assert len(result["drives"]) == 1
            fake_cp.send_to_agent.assert_awaited_once_with(
                "PXMX_DRIVE_HEALTH", {}, agent_id="agent-node1", timeout=30.0
            )

    async def test_spoke_install_ssacli_routing(self):
        from proxmox_spoke import ProxmoxSpoke

        fake_cp = MagicMock()
        fake_cp.connected_agents = {
            "agent-node1": {"cluster_name": "lab-cluster", "nodes": ["pve1"]}
        }
        fake_cp.send_to_agent = AsyncMock(return_value={
            "payload": {
                "data": {
                    "status": "SUCCESS",
                    "installed": True
                }
            }
        })

        spoke = ProxmoxSpoke("px-1", {}, control_plane=fake_cp)
        with patch.object(spoke, "_agent_for_node", return_value="agent-node1"):
            result = await spoke.handle_command("PXMX_INSTALL_SSACLI", {"node": "pve1"})
            assert result["status"] == "SUCCESS"
            assert result["installed"] is True
            assert result["cluster"] == "lab-cluster"
            fake_cp.send_to_agent.assert_awaited_once_with(
                "PXMX_INSTALL_SSACLI", {}, agent_id="agent-node1", timeout=120.0
            )
