"""install_agent.sh kernel-cmdline management — the managed-parameter contract.

``_lm_apply_cmdline`` rewrites ``/etc/default/grub`` (GRUB) and
``/etc/kernel/cmdline`` (systemd-boot / proxmox-boot-tool) so every parameter in
``LM_KERNEL_PARAMS`` is present with the value we want: an existing key has its
VALUE replaced, a missing key is appended, and unmanaged parameters are left
alone. The function is deliberately awk-based so it behaves identically on GNU
and BSD userland — and so it can be exercised off-host, which is what this does.

Particular attention to ``nvme_core.default_ps_max_latency_us=0`` (disables NVMe
APST, whose deep power states some SSDs never wake from — the controller drops
off the bus and takes the datastore with it). Its key contains DOTS, which are
regex metacharacters; the awk ``esc()`` helper escapes them. Without that, the
key's regex would also match unrelated parameters.
"""
import pathlib
import re
import subprocess

import pytest

_INSTALLER = pathlib.Path(__file__).resolve().parents[1] / "install_agent.sh"

# The parameters install_agent.sh guarantees on every pxmx agent host.
_EXPECTED_PARAMS = [
    "pcie_aspm=off",
    "intel_iommu=on",
    "pcie_acs_override=downstream",
    "pcie_power_pm=off",
    "usbcore.autosuspend=-1",
    "nvme_core.default_ps_max_latency_us=0",
]


def _installer_text():
    return _INSTALLER.read_text()


def _declared_params():
    m = re.search(r'^LM_KERNEL_PARAMS="([^"]*)"', _installer_text(), re.M)
    assert m, "LM_KERNEL_PARAMS not found in install_agent.sh"
    return m.group(1).split()


def _harness(tmp_path):
    """A runnable script carrying ONLY LM_KERNEL_PARAMS + _lm_apply_cmdline,
    lifted verbatim from the installer so the test exercises shipping code."""
    text = _installer_text()
    params = re.search(r'^LM_KERNEL_PARAMS="[^"]*"', text, re.M).group(0)
    start = text.index("_lm_apply_cmdline() {")
    end = text.index("\n}\n", start) + len("\n}\n")
    harness = tmp_path / "harness.sh"
    harness.write_text(params + "\n" + text[start:end])
    return harness


def _apply(tmp_path, content, kind):
    """Run _lm_apply_cmdline over ``content``; returns (rc, new_content)."""
    harness = _harness(tmp_path)
    target = tmp_path / ("grub" if kind == "grub" else "cmdline")
    target.write_text(content)
    proc = subprocess.run(
        ["bash", "-c",
         f'. "{harness}"; _lm_apply_cmdline "{target}" "{kind}"'],
        capture_output=True, text=True, timeout=60,
    )
    return proc.returncode, target.read_text()


# ── 1. the parameter is declared ─────────────────────────────────────────────

def test_nvme_apst_is_disabled_in_the_managed_params():
    assert "nvme_core.default_ps_max_latency_us=0" in _declared_params()


def test_managed_params_are_exactly_the_expected_set():
    assert _declared_params() == _EXPECTED_PARAMS


def test_documented_target_line_matches_the_code():
    """The comment above LM_KERNEL_PARAMS shows the resulting GRUB line; drift
    there is how the next person learns the wrong target."""
    text = _installer_text()
    target = re.search(r'#\s+GRUB_CMDLINE_LINUX_DEFAULT="([^"]*)"', text)
    assert target, "documented target line not found"
    documented = target.group(1).split()
    assert documented[0] == "quiet"
    assert documented[1:] == _declared_params()


# ── 2. GRUB rewriting ────────────────────────────────────────────────────────

def test_appends_nvme_param_to_a_stock_grub_file(tmp_path):
    rc, out = _apply(
        tmp_path, 'GRUB_CMDLINE_LINUX_DEFAULT="quiet"\nGRUB_TIMEOUT=5\n', "grub")
    assert rc == 0, "file should have changed"
    line = [l for l in out.splitlines()
            if l.startswith("GRUB_CMDLINE_LINUX_DEFAULT=")][0]
    assert "nvme_core.default_ps_max_latency_us=0" in line
    assert "quiet" in line
    assert "GRUB_TIMEOUT=5" in out, "unrelated lines must survive"


def test_replaces_a_wrong_nvme_value(tmp_path):
    """A host set to the kernel default (or anything else) is corrected."""
    rc, out = _apply(
        tmp_path,
        'GRUB_CMDLINE_LINUX_DEFAULT="quiet nvme_core.default_ps_max_latency_us=5500"\n',
        "grub")
    assert rc == 0
    assert "nvme_core.default_ps_max_latency_us=0" in out
    assert "5500" not in out


def test_no_change_when_everything_is_already_set(tmp_path):
    """Idempotent: a second install must not rewrite the bootloader (rc != 0
    is what suppresses update-grub and the REBOOT REQUIRED notice)."""
    content = ('GRUB_CMDLINE_LINUX_DEFAULT="quiet '
               + " ".join(_EXPECTED_PARAMS) + '"\n')
    rc, out = _apply(tmp_path, content, "grub")
    assert rc != 0, "no-op run reported a change"
    assert out == content


def test_unmanaged_params_are_preserved(tmp_path):
    rc, out = _apply(
        tmp_path,
        'GRUB_CMDLINE_LINUX_DEFAULT="quiet nomodeset consoleblank=0"\n', "grub")
    assert rc == 0
    for keep in ("nomodeset", "consoleblank=0", "quiet"):
        assert keep in out, f"{keep} was dropped"
    assert "nvme_core.default_ps_max_latency_us=0" in out


def test_dotted_key_does_not_corrupt_similar_params(tmp_path):
    """The dots are regex metacharacters — unescaped, the key's pattern could
    chew through neighbouring parameters."""
    rc, out = _apply(
        tmp_path,
        'GRUB_CMDLINE_LINUX_DEFAULT="quiet nvme_core_default_ps_max_latency_us=99 '
        'nvme_core.io_timeout=255"\n', "grub")
    assert rc == 0
    assert "nvme_core.io_timeout=255" in out, "unmanaged nvme param was mangled"
    assert "nvme_core_default_ps_max_latency_us=99" in out, \
        "underscore-variant param was matched by the dotted key's regex"
    assert "nvme_core.default_ps_max_latency_us=0" in out


# ── 3. systemd-boot (proxmox-boot-tool) rewriting ────────────────────────────

def test_cmdline_file_gets_the_nvme_param(tmp_path):
    """ZFS/UEFI Proxmox installs boot from /etc/kernel/cmdline, not GRUB."""
    rc, out = _apply(tmp_path, "root=ZFS=rpool/ROOT/pve-1 boot=zfs\n", "cmdline")
    assert rc == 0
    assert "nvme_core.default_ps_max_latency_us=0" in out
    assert "root=ZFS=rpool/ROOT/pve-1" in out, "root= must survive"
    assert "boot=zfs" in out


def test_cmdline_is_idempotent(tmp_path):
    content = "root=ZFS=rpool/ROOT/pve-1 boot=zfs " + " ".join(_EXPECTED_PARAMS) + "\n"
    rc, out = _apply(tmp_path, content, "cmdline")
    assert rc != 0
    assert out == content


# ── 4. safety ────────────────────────────────────────────────────────────────

def test_a_backup_is_taken_before_rewriting(tmp_path):
    _apply(tmp_path, 'GRUB_CMDLINE_LINUX_DEFAULT="quiet"\n', "grub")
    backups = list(tmp_path.glob("grub.lm-bak-*"))
    assert backups, "no backup of the boot config was taken"
    assert 'GRUB_CMDLINE_LINUX_DEFAULT="quiet"' in backups[0].read_text()


def test_missing_file_is_a_no_op(tmp_path):
    harness = _harness(tmp_path)
    proc = subprocess.run(
        ["bash", "-c",
         f'. "{harness}"; _lm_apply_cmdline "{tmp_path}/nope" grub'],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0
    assert not (tmp_path / "nope").exists()
