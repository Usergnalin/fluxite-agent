import os
import logging
import time
import subprocess
import requests
from config import WG_INTERFACE, FIREWALL_RULES, REQUEST_TIMEOUT

log = logging.getLogger(__name__)

def find_wireguard_exe() -> str | None:
    """
    Find wireguard.exe using multiple methods. Returns path string or None.
    Privileged function
    """

    # Method 1: Check common/env-expanded paths
    candidates = [
        r"C:\Program Files\WireGuard\wireguard.exe",
        r"C:\Program Files (x86)\WireGuard\wireguard.exe",
        r"C:\WireGuard\wireguard.exe",
        r"C:\ProgramData\WireGuard\wireguard.exe",
        os.path.expandvars(r"%APPDATA%\WireGuard\wireguard.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\WireGuard\wireguard.exe"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path

    # Method 2: Search WireGuard subdirs under Program Files variants
    pf_dirs = filter(None, [
        os.environ.get('ProgramFiles'),
        os.environ.get('ProgramFiles(x86)'),
        os.environ.get('ProgramW6432'),
    ])
    for base in pf_dirs:
        exe = os.path.join(base, 'WireGuard', 'wireguard.exe')
        if os.path.isfile(exe):
            return exe

    # Method 3: Walk PATH
    for dir_ in os.environ.get('PATH', '').split(os.pathsep):
        exe = os.path.join(dir_, 'wireguard.exe')
        if os.path.isfile(exe):
            return exe

    # Method 4: `where` command (Windows only)
    try:
        result = subprocess.run(
            ['where', 'wireguard.exe'],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            first = result.stdout.strip().splitlines()[0]
            if os.path.isfile(first):
                return first
    except (FileNotFoundError, OSError):
        pass

    # Method 5: Registry lookup (Windows only)
    try:
        import winreg
        reg_keys = [
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\wireguard.exe"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\wireguard.exe"),
        ]
        for hive, subkey in reg_keys:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    exe, _ = winreg.QueryValueEx(key, "")
                if os.path.isfile(exe):
                    return exe
            except OSError:
                continue
    except ImportError:
        pass

    return None

def install_wireguard_tunnel(wireguard_exe_path: str, conf_path: str) -> None:
    """
    Uninstall any existing tunnel, then install the new one.
    Priviledged function
    """
    result = subprocess.run(
        [wireguard_exe_path, "/uninstalltunnelservice", WG_INTERFACE],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode == 0:
        log.info("Old WireGuard tunnel service uninstalled")
    else:
        log.warning(
            "WireGuard uninstall returned %d: %s",
            result.returncode, result.stderr.strip()
        )
    time.sleep(2)
    result = subprocess.run(
        [wireguard_exe_path, "/installtunnelservice", conf_path],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode == 0:
        log.info("WireGuard tunnel service installed")
    else:
        log.error("WireGuard install failed: %s", result.stderr.strip())
        raise RuntimeError(f"WireGuard tunnel installation failed (exit {result.returncode})")
    _set_firewall()
    
def uninstall_wireguard_tunnel(wireguard_exe_path: str) -> None:
    """
    Uninstall any existing tunnel
    Priviledged function
    """
    result = subprocess.run(
        [wireguard_exe_path, "/uninstalltunnelservice", WG_INTERFACE],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode == 0:
        log.info("WireGuard tunnel service uninstalled")
    else:
        log.warning(
            "WireGuard uninstall returned %d: %s",
            result.returncode, result.stderr.strip()
        )
    _remove_firewall()

def _set_firewall() -> None:
    """
    Set firewalls specified in config
    Priviledged function
    """
    for display_name, protocol, port_low, port_high in FIREWALL_RULES:
        port_arg = f"-LocalPort {port_low}-{port_high}" if port_low else ""
        command = (
            f'New-NetFirewallRule '
            f'-DisplayName "{display_name}" '
            f'-Direction Inbound '
            f'-Protocol {protocol} '
            f'{port_arg} '
            f'-InterfaceAlias {WG_INTERFACE} '
            f'-Action Block '
            f'-Profile Any'
        )
        result = subprocess.run(
            ["powershell", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            log.error("Failed to create firewall rule %s", display_name)
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        log.info("Created firewall rule: %s", display_name)

def _remove_firewall() -> None:
    """
    Remove firewalls specified in config
    Priviledged function
    """
    for display_name, _, _, _ in FIREWALL_RULES:
        command = f'Remove-NetFirewallRule -DisplayName "{display_name}" -ErrorAction SilentlyContinue'
        result = subprocess.run(
            ["powershell", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            log.warning("Failed to remove firewall rule %s", display_name)
        else:
            log.info("Removed firewall rule: %s", display_name)

def request_with_retry(
    url: str,
    method: str = "GET",
    json: dict | None = None,
    headers: dict | None = None,
    max_retries: int = 3,
) -> requests.Response:
    """
    Make an HTTP request, retrying on 429 using the Retry-After header.
    Raises RuntimeError if max retries exceeded.
    Raises requests.HTTPError on other non-2xx responses.
    """
    for attempt in range(max_retries):
        response = requests.request(
            method,
            url,
            json=json,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code == 429:
            retry_after = int(response.headers.get("retry-after", 60))
            if attempt < max_retries - 1:
                log.warning(
                    "Rate limited (attempt %d/%d), retrying after %ds",
                    attempt + 1, max_retries, retry_after,
                )
                time.sleep(retry_after)
                continue
            else:
                raise RuntimeError(
                    f"Rate limited after {max_retries} attempts. "
                    f"Try again in {retry_after}s."
                )

        response.raise_for_status()
        return response

    raise RuntimeError(f"Request failed after {max_retries} attempts")