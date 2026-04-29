"""
Handles downloading, hashing, and initial setup of Minecraft server instances.
"""

import os
import hashlib
import requests
import logging
import subprocess
from utils import uuid7
from config import SERVERS_BASE_DIR, REQUEST_TIMEOUT, FABRIC_INSTALLER_PATH, QUILT_INSTALLER_PATH, JVM_PATH, VANILLA_MANIFEST, NEOFORGE_INSTALLER_URL, FORGE_INSTALLER_URL, MODRINTH_USER_AGENT, INSTALLER_TIMEOUT, TMP_DIR

log = logging.getLogger(__name__)

def download_file(url: str, dest_path: str, expected_hash: str = None, headers: dict = None) -> bool:
    """Download a file and optionally verify its SHA1 hash."""
    try:
        log.info("Downloading %s to %s", url, dest_path)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        
        resp = requests.get(url, stream=True, timeout=60, headers=headers)
        resp.raise_for_status()
        
        sha1 = hashlib.sha1()
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
                sha1.update(chunk)
        
        if expected_hash:
            actual_hash = sha1.hexdigest()
            if actual_hash != expected_hash:
                log.error("Hash mismatch! Expected %s, got %s", expected_hash, actual_hash)
                os.remove(dest_path)
                return False
        
        return True
    except Exception as e:
        log.error("Download failed: %s", e)
        return False

def extract_project_id_from_url(url: str) -> str | None:
    """Extract project_id from Modrinth CDN URL.
    URL format: https://cdn.modrinth.com/data/{project_id}/versions/{version_id}/{file_name}
    """
    try:
        parts = url.split('/')
        # Find 'data' in the path and get the next part
        for i, part in enumerate(parts):
            if part == 'data' and i + 1 < len(parts):
                return parts[i + 1]
    except Exception:
        pass
    return None

def get_modrinth_project_data(project_id: str, default_icon_url: str) -> dict:
    """Fetch project data from Modrinth API.
    Returns dict with icon_url, name, server_side. On failure returns defaults.
    """
    if not project_id:
        return {
            "icon_url": default_icon_url,
            "name": None,
            "server_side": "unknown"
        }
    
    try:
        from config import MODRINTH_PROJECT_URL_TPL, MODRINTH_USER_AGENT, REQUEST_TIMEOUT
        url = MODRINTH_PROJECT_URL_TPL.format(project_id=project_id)
        headers = {"User-Agent": MODRINTH_USER_AGENT}
        
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        
        data = resp.json()
        icon_url = data.get("icon_url") or default_icon_url
        name = data.get("name")
        server_side = data.get("server_side", "unknown")
        
        log.debug("Fetched project data for %s: name=%s, server_side=%s", project_id, name, server_side)
        return {
            "icon_url": icon_url,
            "name": name,
            "server_side": server_side
        }
    except Exception as e:
        log.warning("Failed to fetch project data for %s: %s", project_id, e)
    
    return {
        "icon_url": default_icon_url,
        "name": None,
        "server_side": "unknown"
    }

# def setup_server_directory(server_id: str, game_version: str, loader_version: str, loader_type: str, port: int) -> str | None:
#     """
#     Set up a new server directory using local fabric-installer.jar.
#     Returns the name of the jar file on success (fabric-server-launch.jar).
#     """
#     server_dir = os.path.join(SERVERS_BASE_DIR, server_id)
#     os.makedirs(server_dir, exist_ok=True)
    
#     # Check if installer exists
#     if not os.path.exists(FABRIC_INSTALLER_PATH):
#         log.error("Fabric installer not found at: %s", FABRIC_INSTALLER_PATH)
#         return None
    
#     # Check if JVM exists
#     if not os.path.exists(JVM_PATH):
#         log.error("JVM not found at: %s", JVM_PATH)
#         return None
    
#     # Run fabric installer
#     if loader_type == "fabric":
#         cmd = [
#             JVM_PATH, "-jar", os.path.abspath(FABRIC_INSTALLER_PATH), "server",
#             "-mcversion", game_version,
#             "-loader", loader_version,
#             "-downloadMinecraft"
#         ]
#     elif loader_type == "quilt":
#         cmd = [
#             JVM_PATH, "-jar", os.path.abspath(QUILT_INSTALLER_PATH), "install server",
#             game_version, loader_version,
#             "--install-dir=.",
#             "--download-server"
#         ]
    
#     log.info("Running installer: %s", " ".join(cmd))
    
#     try:
#         result = subprocess.run(
#             cmd,
#             cwd=server_dir,
#             capture_output=True,
#             text=True,
#             timeout=300  # 5 minutes timeout
#         )
        
#         if result.returncode != 0:
#             log.error("Fabric installer failed with return code %d", result.returncode)
#             log.error("STDOUT: %s", result.stdout)
#             log.error("STDERR: %s", result.stderr)
#             return None
        
#         log.info("Fabric installer completed successfully")
#         log.info("STDOUT: %s", result.stdout)
        
#         if loader_type == "fabric":
#             jar_name = "fabric-server-launch.jar"
#         elif loader_type == "quilt":
#             jar_name = "quilt-server-launch.jar"

#         print(os.path.abspath(server_dir), os.listdir(server_dir))
        
#         if not os.path.exists(os.path.join(server_dir, jar_name)):
#             log.error("Expected jar file not found after installation: %s", jar_name)
#             return None
        
#         # Accept EULA automatically
#         with open(os.path.join(server_dir, "eula.txt"), "w") as f:
#             f.write("eula=true\n")
        
#         # Initial server.properties with the port
#         with open(os.path.join(server_dir, "server.properties"), "w") as f:
#             f.write(f"server-port={port}\n")
#             f.write(f"query.port={port}\n")
        
#         return jar_name
        
#     except subprocess.TimeoutExpired:
#         log.error("Fabric installer timed out")
#         return None
#     except Exception as e:
#         log.error("Failed to run Fabric installer: %s", e)
#         return None

def setup_server_directory(server_id: str, game_version: str, loader_version: str, loader_type: str, port: int) -> tuple[str, str] | None:
    """
    Set up a new server directory using local fabric-installer.jar.
    Returns the name of the jar file on success (fabric-server-launch.jar).
    """
    
    try:
        server_dir = os.path.join(SERVERS_BASE_DIR, server_id)
        os.makedirs(server_dir, exist_ok=True)
        
        # Check if JVM exists
        if not os.path.exists(JVM_PATH):
            log.error("JVM not found at: %s", JVM_PATH)
            return None

        if loader_type in ["fabric", "quilt"]:
            if loader_type == "fabric":
                cmd = [
                    JVM_PATH, "-jar", os.path.abspath(FABRIC_INSTALLER_PATH), "server",
                    "-mcversion", game_version,
                    "-loader", loader_version,
                    "-downloadMinecraft"
                ]
            elif loader_type == "quilt":
                cmd = [
                    JVM_PATH, "-jar", os.path.abspath(QUILT_INSTALLER_PATH), "install", "server",
                    game_version, loader_version,
                    "--install-dir=.",
                    "--download-server"
                ]

            result = subprocess.run(
                cmd,
                cwd=server_dir,
                capture_output=True,
                text=True,
                timeout=INSTALLER_TIMEOUT
            )

            if result.returncode != 0:
                log.error("Fabric installer failed with return code %d", result.returncode)
                log.error("STDOUT: %s", result.stdout)
                log.error("STDERR: %s", result.stderr)
                return None

            log.info("Fabric installer completed successfully")
            log.info("STDOUT: %s", result.stdout)
            
            if loader_type == "fabric":
                entrypoint = "fabric-server-launch.jar"
            elif loader_type == "quilt":
                entrypoint = "quilt-server-launch.jar"
        elif loader_type == "vanilla":
            # Get version metadata url
            resp = requests.get(VANILLA_MANIFEST, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            for version in data["versions"]:
                if version["id"] == game_version:
                    version_meta_url = version["url"]
                    break
            else:
                log.error("Vanilla server jar metadata not found for version: %s", game_version)
                return None
            # Get vanilla server jar url
            resp = requests.get(version_meta_url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": MODRINTH_USER_AGENT})
            resp.raise_for_status()
            data = resp.json()
            download_url = data["downloads"]["server"]["url"]
            # Download vanilla server jar
            if not download_file(download_url, os.path.join(server_dir, "server.jar"), expected_hash=None, headers={"User-Agent": MODRINTH_USER_AGENT}):
                log.error("Failed to download vanilla server jar")
                return None
            entrypoint = "server.jar"
        elif loader_type in ['forge', 'neoforge']:
            if loader_type == 'forge':
                installer_url = FORGE_INSTALLER_URL.format(mc_version=game_version, loader_version=loader_version)
            elif loader_type == 'neoforge':
                installer_url = NEOFORGE_INSTALLER_URL.format(loader_version=loader_version)
            temp_installer_file_name = f"{loader_type}-installer-{loader_version}-{game_version}-{uuid7()}.jar"
            temp_installer_path = os.path.abspath(os.path.join(TMP_DIR, temp_installer_file_name))
            log.info(f"Downloading {loader_type} installer from {installer_url}...")
            try :
                if not download_file(installer_url, temp_installer_path, expected_hash=None, headers={"User-Agent": MODRINTH_USER_AGENT}):
                    log.error("Failed to download %s installer", loader_type)
                    return None
                log.info(f"Downloaded {loader_type} installer to {temp_installer_path}")
                command = [JVM_PATH, "-jar", temp_installer_path, "--installServer"]
                log.info(f"Running command: {' '.join(command)}")
                result = subprocess.run(command, cwd=server_dir, capture_output=True, text=True, timeout=INSTALLER_TIMEOUT)
                if result.returncode != 0:
                    log.error("Failed to run %s installer: %s", loader_type, result.stderr)
                    return None
                log.info("%s installer completed successfully", loader_type)

                # Get entrypoint
                entrypoint = "run.bat" if os.name == 'nt' else "run.sh"

                if os.path.exists(os.path.join(server_dir, entrypoint)):
                    pass  # run.sh/run.bat found, good
                else:
                    # Fall back to old-style forge jar
                    for file in os.listdir(server_dir):
                        if file.startswith("forge-") and file.endswith(".jar"):
                            entrypoint = file
                            break
                    else:
                        log.error("Failed to find entrypoint for %s", loader_type)
                        return None
                
                # Remove pause from batch file
                if os.name == 'nt':
                    remove_pause_from_bat(os.path.join(server_dir, entrypoint))
            finally:
                if os.path.exists(temp_installer_path):
                    log.info("Cleaning up temporary installer jar: %s", temp_installer_path)
                    os.remove(temp_installer_path)
            
        else:
            log.error("Unsupported loader type: %s", loader_type)
            return None
            
        # Check if jar file exists
        if not os.path.exists(os.path.join(server_dir, entrypoint)):
            log.error("Expected entrypoint file not found after installation: %s", entrypoint)
            return None

        # Accept EULA automatically
        with open(os.path.join(server_dir, "eula.txt"), "w") as f:
            f.write("eula=true\n")
        
        # Set server properties
        with open(os.path.join(server_dir, "server.properties"), "w") as f:
            f.write(f"server-port={port}\n")
            f.write(f"query.port={port}\n")

        java_version = get_java_version(game_version)

        return entrypoint, java_version
    except subprocess.TimeoutExpired:
        log.error("Installer timed out")
        return None
    except Exception as e:
        log.error("Failed to run installer: %s", e)
        return None

def parse_server_properties(server_dir: str) -> dict:
    """Read server.properties and return as a dictionary."""
    props = {}
    path = os.path.join(server_dir, "server.properties")
    if not os.path.exists(path):
        return props
    
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    props[key.strip()] = value.strip()
    except Exception as e:
        log.error("Failed to parse server.properties: %s", e)
    
    return props


def get_java_version(version_str: str) -> int:
    """
    Returns the required major Java version for a given Minecraft version string.
    Supports both legacy (1.x.y) and new (YY.RELEASE.PATCH e.g. 26.1) versioning.
    """
    try:
        parts = tuple(map(int, version_str.split('.')))
    except ValueError:
        return 8  # Safe fallback for malformed strings

    # New versioning scheme: 26.x, 27.x etc (year-based, starting 2026)
    if parts[0] >= 26:
        return 25  # 26.1+ requires Java 25

    # Legacy 1.x.y versioning
    if parts >= (1, 20, 5): return 21  # 1.20.5–1.21.x
    if parts >= (1, 18, 0): return 17  # 1.18–1.20.4
    if parts >= (1, 17, 0): return 16  # 1.17–1.17.1
    if parts >= (1, 12, 0): return 8   # 1.12–1.16.5
    if parts >= (1,  6, 1): return 6   # 1.6.1–1.11.2
    return 5                            # pre-classic to 1.5.2

def remove_pause_from_bat(file_path, output_path=None):
    # Use output_path to save to a new file (safer than overwriting)
    if output_path is None:
        output_path = file_path

    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()

    # 1. Strip trailing whitespace/newlines from the end of the file
    # This turns "pause\n\n" into "pause"
    trimmed_content = content.rstrip()

    # 2. Check if the file now ends with "pause" (case-insensitive)
    if trimmed_content.lower().endswith("pause"):
        # 3. Slice the content to remove the "pause" keyword
        new_content = trimmed_content[:-5].rstrip()
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print("Successfully removed 'pause' from the end of the file.")
    else:
        print("No 'pause' command found at the end of the file.")