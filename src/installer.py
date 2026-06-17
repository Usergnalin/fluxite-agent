"""
Handles downloading, hashing, and initial setup of Minecraft server instances.
"""

import os
import sys
import hashlib
import zipfile
import tarfile
import requests
import logging
import subprocess
import tempfile
import shutil
import re
from PIL import Image
import io
from utils import uuid7
from config import SERVERS_BASE_DIR, REQUEST_TIMEOUT, FABRIC_INSTALLER_PATH, QUILT_INSTALLER_PATH, JVM_PATH, VANILLA_MANIFEST, NEOFORGE_INSTALLER_URL, FORGE_INSTALLER_URL, MODRINTH_USER_AGENT, INSTALLER_TIMEOUT, TMP_DIR

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dynamic hash fetching
# ---------------------------------------------------------------------------

def fetch_expected_hash(url: str, headers: dict = None) -> str | None:
    """Fetch the expected hash for a download URL from its provider.
    
    Detects the hash source based on the URL pattern:
    - Maven artifacts: fetches {url}.sha1
    - GitHub releases: fetches {url}.sha256.txt
    - Other: returns None (hash checking skipped)
    
    Returns:
        The hex-encoded hash string, or None if unavailable.
    """
    try:
        # GitHub release artifacts provide SHA256 checksum files
        if "github.com/" in url and "/releases/download/" in url:
            hash_url = url + ".sha256.txt"
            log.debug("Fetching hash from %s", hash_url)
            resp = requests.get(hash_url, timeout=30, headers=headers)
            resp.raise_for_status()
            # Format: "<sha256_hash> <filename>" — extract the first token
            match = re.match(r'^([a-fA-F0-9]{64})\s', resp.text.strip())
            if match:
                return match.group(1).lower()
            # Fallback: try treating the whole text as just the hash
            first_line = resp.text.strip().split('\n')[0].strip()
            if re.match(r'^[a-fA-F0-9]{64}$', first_line):
                return first_line.lower()
            log.warning("Could not parse hash from %s: %s", hash_url, resp.text[:200])
            return None
        
        # Maven repositories provide SHA1 checksum files
        if "maven." in url or "/maven/" in url:
            hash_url = url + ".sha1"
            log.debug("Fetching hash from %s", hash_url)
            resp = requests.get(hash_url, timeout=30, headers=headers)
            resp.raise_for_status()
            # Maven .sha1 files are just the hex hash (40 chars)
            hash_text = resp.text.strip()
            if re.match(r'^[a-fA-F0-9]{40}$', hash_text):
                return hash_text.lower()
            log.warning("Unexpected SHA1 format from %s: %s", hash_url, hash_text[:200])
            return None
        
        log.debug("No hash provider detected for URL: %s", url)
        return None
    except Exception as e:
        log.warning("Failed to fetch hash for %s: %s — skipping hash verification", url, e)
        return None


def download_file(url: str, dest_path: str, expected_hash: str = None, headers: dict = None) -> bool:
    """Download a file and optionally verify its hash.
    
    If expected_hash is None, the hash will be fetched dynamically from the
    provider (Maven .sha1 or GitHub .sha256.txt).
    """
    try:
        log.info("Downloading %s to %s", url, dest_path)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        
        # Hash algorithm detection: SHA-1 (40 chars) vs SHA-256 (64 chars)
        # Default to SHA-1 for backward compatibility; SHA-256 for GitHub.
        if "github.com/" in url and "/releases/download/" in url:
            hash_algo = "sha256"
        else:
            hash_algo = "sha1"
        
        resp = requests.get(url, stream=True, timeout=60, headers=headers)
        resp.raise_for_status()
        
        algo = hashlib.sha256() if hash_algo == "sha256" else hashlib.sha1()
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
                algo.update(chunk)
        
        # Resolve hash: use provided, or fetch dynamically
        verify_hash = expected_hash
        if verify_hash is None:
            verify_hash = fetch_expected_hash(url, headers)
        
        if verify_hash:
            actual_hash = algo.hexdigest()
            if actual_hash != verify_hash:
                log.error("Hash mismatch! Expected %s, got %s", verify_hash, actual_hash)
                os.remove(dest_path)
                return False
            log.debug("Hash verified: %s", verify_hash)
        else:
            log.info("No hash available — skipping verification for %s", url)
        
        return True
    except Exception as e:
        log.error("Download failed: %s", e)
        return False


def download_and_extract(url: str, extract_to: str, expected_hash: str = None, archive_type: str = "auto") -> bool:
    """Download an archive file, verify its hash, and extract it to the specified directory.
    
    Args:
        url: URL to download from
        extract_to: Directory to extract the archive to
        expected_hash: Expected hash (None to fetch dynamically — SHA256 for GitHub, SHA1 for Maven).
        archive_type: Archive type - "zip", "tar.gz", or "auto" to detect from URL
        
    Returns:
        True on success, False on failure
    """
    try:
        # Determine archive type
        if archive_type == "auto":
            if url.endswith(".zip"):
                archive_type = "zip"
            elif url.endswith(".tar.gz") or url.endswith(".tgz"):
                archive_type = "tar.gz"
            else:
                log.error("Cannot determine archive type from URL: %s", url)
                return False
        
        # Create temp file for download
        suffix = ".zip" if archive_type == "zip" else ".tar.gz"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_file:
            download_path = tmp_file.name
        
        log.info("Downloading archive to temp file: %s", download_path)
        
        # Download with hash computation
        # Use SHA-256 for GitHub releases, SHA-1 for everything else
        use_sha256 = "github.com/" in url and "/releases/download/" in url
        algo = hashlib.sha256() if use_sha256 else hashlib.sha1()
        
        resp = requests.get(url, stream=True, timeout=120, allow_redirects=True)
        resp.raise_for_status()
        
        with open(download_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
                algo.update(chunk)
        
        # Resolve hash: use provided, or fetch dynamically
        verify_hash = expected_hash
        if verify_hash is None:
            verify_hash = fetch_expected_hash(url)
        
        # Verify hash
        if verify_hash:
            actual_hash = algo.hexdigest()
            if actual_hash != verify_hash:
                log.error("Hash mismatch! Expected %s, got %s", verify_hash, actual_hash)
                os.remove(download_path)
                return False
            log.debug("Hash verified: %s", verify_hash)
        else:
            log.info("No hash available — skipping verification for %s", url)
        
        # Prepare extraction directory
        os.makedirs(extract_to, exist_ok=True)
        
        log.info("Extracting archive to: %s", extract_to)
        
        # Extract based on archive type
        if archive_type == "zip":
            with zipfile.ZipFile(download_path, 'r') as zip_ref:
                zip_ref.extractall(extract_to)
        elif archive_type == "tar.gz":
            with tarfile.open(download_path, 'r:gz') as tar_ref:
                tar_ref.extractall(extract_to)
        else:
            log.error("Unsupported archive type: %s", archive_type)
            os.remove(download_path)
            return False
        
        # Clean up temp file
        os.remove(download_path)
        
        # Flatten directory structure: Eclipse Temurin archives have a single nested directory
        # like jdk8u442-b06 that should be flattened to extract_to root
        extracted_items = os.listdir(extract_to)
        if len(extracted_items) == 1 and os.path.isdir(os.path.join(extract_to, extracted_items[0])):
            nested_dir = os.path.join(extract_to, extracted_items[0])
            log.info("Flattening extracted directory structure: %s -> %s", extracted_items[0], extract_to)
            
            # Move all contents from nested dir to extract_to
            for item in os.listdir(nested_dir):
                src = os.path.join(nested_dir, item)
                dst = os.path.join(extract_to, item)
                shutil.move(src, dst)
            
            # Remove empty nested directory
            os.rmdir(nested_dir)
        
        log.info("Successfully downloaded and extracted to: %s", extract_to)
        return True
        
    except Exception as e:
        log.error("Download and extract failed: %s", e)
        # Clean up on failure
        if 'download_path' in locals() and os.path.exists(download_path):
            os.remove(download_path)
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

def setup_server_directory(server_id: str, game_version: str, loader_version: str, loader_type: str, port: int, motd: str, server_thumnbnail: str) -> tuple[str, str] | None:
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
            f.write(f"motd={motd}\n")
        
        # Set server icon
        set_server_icon(server_dir, server_thumnbnail)
        
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

def set_server_icon(server_dir: str, server_thumbnail: str) -> bool:
    """
    Downloads, validates, and converts an image to a valid 64x64 PNG server icon.
    Returns True if successful, False otherwise.
    """
    if not server_thumbnail:
        return False

    try:
        response = requests.get(server_thumbnail, timeout=10)
        response.raise_for_status()
        raw = response.content
    except requests.RequestException as e:
        log.error("Failed to download server icon from %s: %s", server_thumbnail, e)
        return False

    try:
        img = Image.open(io.BytesIO(raw))
        original_format = img.format
        original_size = img.size

        # Convert to RGBA first to handle formats like JPEG that don't support transparency
        img = img.convert("RGBA")

        if original_format != "PNG":
            log.warning(
                "Server icon was %s, not PNG — converting automatically",
                original_format or "unknown format"
            )

        if original_size != (64, 64):
            log.warning(
                "Server icon was %dx%d, expected 64x64 — resizing with LANCZOS",
                original_size[0], original_size[1]
            )
            img = img.resize((64, 64), Image.LANCZOS)

        # Write as valid PNG
        out = io.BytesIO()
        img.save(out, format="PNG")
        out.seek(0)

        icon_path = os.path.join(server_dir, "server-icon.png")
        with open(icon_path, "wb") as f:
            f.write(out.read())

        log.info("Server icon saved successfully (%s, %dx%d → 64x64 PNG)", 
                 original_format, original_size[0], original_size[1])
        return True

    except Exception as e:
        log.error("Failed to process server icon (invalid or corrupt image): %s", e)
        return False