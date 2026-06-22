"""
Agent configuration — API endpoints, file paths, and defaults.
"""

import os
import sys

API_BASE = os.getenv("API_BASE", "https://api.gnalin.xyz")
IS_FROZEN = getattr(sys, 'frozen', False)

if IS_FROZEN:
    # running as installed exe - use PROGRAMDATA
    BASE_DIR = os.path.join(os.environ.get('PROGRAMDATA', 'C:\\ProgramData'), 'FluxiteAgent')
else:
    # running in dev - use project root
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Base directories
SERVERS_BASE_DIR = os.path.join(BASE_DIR, "servers")
IMPORT_DIR = os.path.join(BASE_DIR, "import")
TMP_DIR = os.path.join(BASE_DIR, "tmp")
RUNTIMES_DIR = os.path.join(BASE_DIR, "runtimes")

# Agent linking
LINK_URL = f"{API_BASE}/agent"

# Token refresh (format with agent_id)
REFRESH_URL_TPL = API_BASE + "/agent/{}/refresh"

# Command polling (format with agent_id)
COMMAND_URL_TPL = API_BASE + "/command/{}"
COMMAND_STREAM_URL_TPL = API_BASE + "/command/{}/stream"
AGENT_URL = API_BASE + "/agent"
REPORT_MODULES_URL_TPL = API_BASE + "/module/{}"
DELETE_MODULE_URL_TPL = API_BASE + "/module/{}"
UPDATE_MODULE_URL_TPL = API_BASE + "/module/{}"
DELETE_SERVER_URL_TPL = API_BASE + "/server/{}"
SERVER_LOGS_URL_TPL = API_BASE + "/server/logs/{}"
DELETE_AGENT_URL = API_BASE + "/agent"

LOG_STREAM_MAX_DURATION = 3600
LOG_STREAM_BATCH_INTERVAL = 0.5
LOG_STREAM_MAX_CHARS = 500

# Agent logging
AGENT_LOGS_DIR = os.path.join(BASE_DIR, "logs")
AGENT_LOG_MAX_AGE_DAYS = 30
AGENT_LOGS_URL = API_BASE + "/agent/logs/{}"

# Modrinth settings (MOCKED for testing)
MODULE_DOWNLOAD_URL_TPL = "https://cdn.modrinth.com/data/{project_id}/versions/{version_id}/{file_name}"
MODPACK_DOWNLOAD_URL_TPL = "https://cdn.modrinth.com/data/{project_id}/versions/{version_id}/{file_name}"
MODRINTH_USER_AGENT = "Usergnalin/mc_manager_api (usernilang@gmail.com)"

# Modrinth API endpoints
MODRINTH_PROJECT_URL_TPL = "https://api.modrinth.com/v2/project/{project_id}"
MODRINTH_BULK_PROJECTS_URL = "https://api.modrinth.com/v2/projects"
MODRINTH_BULK_VERSIONS_URL = "https://api.modrinth.com/v2/versions"
MODRINTH_BULK_BATCH_SIZE = 50  # Max IDs per bulk API request
DEFAULT_MODULE_ICON_URL = "https://cdn.modrinth.com/data/placeholder/icon.png"

# Other API endpoints
VANILLA_MANIFEST = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"
NEOFORGE_INSTALLER_URL = "https://maven.neoforged.net/releases/net/neoforged/neoforge/{loader_version}/neoforge-{loader_version}-installer.jar"
FORGE_INSTALLER_URL = "https://maven.minecraftforge.net/net/minecraftforge/forge/{mc_version}-{loader_version}/forge-{mc_version}-{loader_version}-installer.jar"

# Temporary directory for modpack processing
TMP_DIR = os.path.join(BASE_DIR, "tmp")

# Local credential files
KEY_FILE = os.path.join(BASE_DIR, "agent.key")
ID_FILE = os.path.join(BASE_DIR, "agent_id.txt")

# Timeouts
INSTALLER_TIMEOUT = 300

# MC Server management
SERVERS_BASE_DIR = os.path.join(BASE_DIR, "servers")
SERVER_REGISTRY_FILE = os.path.join(BASE_DIR, "servers.json")
FABRIC_INSTALLER_PATH = os.path.join(BASE_DIR, "installers", "fabric-installer.jar")
QUILT_INSTALLER_PATH = os.path.join(BASE_DIR, "installers", "quilt-installer.jar")

# JVM Runtime settings
JVM_VERSION = "21"

def _get_java_executable(name: str, java_version: str = JVM_VERSION) -> str:
    """Get Java executable path with correct extension for platform.
    Windows uses .exe, Linux/macOS use no extension.
    """
    suffix = ".exe" if sys.platform == "win32" else ""
    return os.path.abspath(os.path.join(BASE_DIR, f"runtimes/jdk{java_version}/bin/{name}{suffix}"))

JVM_PATH = _get_java_executable("java")

def get_jvm_path_for_version(java_version: str) -> str:
    """Get the JVM executable path for a specific Java version."""
    return _get_java_executable("java", java_version)

# Module types and folder mapping
VALID_MODULE_TYPES = ['mod', 'resource_pack', 'data_pack', 'plugin']
MODULE_TYPE_FOLDERS = {
    'mod': 'mods',
    'resource_pack': 'resourcepacks',
    'data_pack': 'world/datapacks',
    'plugin': 'plugins'
}

# Required files to download on startup if missing
# Hashes are fetched dynamically from Maven (.sha1) at download time.
REQUIRED_DOWNLOADS = {
    FABRIC_INSTALLER_PATH: (
        "https://maven.fabricmc.net/net/fabricmc/fabric-installer/1.1.1/fabric-installer-1.1.1.jar",
    ),
    QUILT_INSTALLER_PATH: (
        "https://maven.quiltmc.org/repository/release/org/quiltmc/quilt-installer/0.14.1/quilt-installer-0.14.1.jar",
    ),
}

# JDK versions to auto-download on startup
JDK_VERSIONS = [8, 11, 17, 21, 25]

# Eclipse Temurin JDK download configuration
# Direct GitHub release URLs for specific versions
def get_jdk_download_config(java_version: int) -> tuple[str, str | None, str]:
    """Get JDK download URL, archive extension, and expected hash (now fetched dynamically).
    
    Returns:
        tuple: (download_url, expected_sha1_hash_or_None, archive_extension)
        The hash is None — it will be fetched dynamically from {url}.sha256.txt at download time.
    """
    is_windows = sys.platform == "win32"
    os_name = "windows" if is_windows else "linux"
    ext = "zip" if is_windows else "tar.gz"
    
    # URLs only — hashes are fetched dynamically from {url}.sha256.txt
    download_map = {
        "windows": {
            25: "https://github.com/adoptium/temurin25-binaries/releases/download/jdk-25.0.2%2B10/OpenJDK25U-jdk_x64_windows_hotspot_25.0.2_10.zip",
            21: "https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.11%2B10/OpenJDK21U-jdk_x64_windows_hotspot_21.0.11_10.zip",
            17: "https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.18%2B8/OpenJDK17U-jdk_x64_windows_hotspot_17.0.18_8.zip",
            11: "https://github.com/adoptium/temurin11-binaries/releases/download/jdk-11.0.31%2B11/OpenJDK11U-jdk_x64_windows_hotspot_11.0.31_11.zip",
            8: "https://github.com/adoptium/temurin8-binaries/releases/download/jdk8u482-b08/OpenJDK8U-jdk_x64_windows_hotspot_8u482b08.zip",
        },
        "linux": {
            25: "https://github.com/adoptium/temurin25-binaries/releases/download/jdk-25.0.3%2B9/OpenJDK25U-jdk_x64_linux_hotspot_25.0.3_9.tar.gz",
            21: "https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.11%2B10/OpenJDK21U-jdk_x64_linux_hotspot_21.0.11_10.tar.gz",
            17: "https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.19%2B10/OpenJDK17U-jdk_x64_linux_hotspot_17.0.19_10.tar.gz",
            11: "https://github.com/adoptium/temurin11-binaries/releases/download/jdk-11.0.31%2B11/OpenJDK11U-jdk_x64_linux_hotspot_11.0.31_11.tar.gz",
            8: "https://github.com/adoptium/temurin8-binaries/releases/download/jdk8u482-b08/OpenJDK8U-jdk_x64_linux_hotspot_8u482b08.tar.gz",
        }
    }
    
    if java_version not in download_map[os_name]:
        raise ValueError(f"Unsupported Java version: {java_version}")
    
    url = download_map[os_name][java_version]
    expected_hash = None  # Will be fetched dynamically from {url}.sha256.txt
    
    return url, expected_hash, ext

# API Endpoints
CREATE_SERVER_URL = API_BASE + "/server"
UPDATE_SERVER_THUMBNAIL = API_BASE + "/server/{}/thumbnail/agent"
SERVER_STATUS_URL_TPL = API_BASE + "/server/{}/status"
COMMAND_STATUS_URL_TPL = API_BASE + "/command/{}/status"
COMMAND_FEEDBACK_URL_TPL = API_BASE + "/command/{}/feedback"

# Polling
TOKEN_REFRESH_INTERVAL = 60 * 30   # 30 minutes
REQUEST_TIMEOUT = 10               # seconds

WG_INTERFACE = "wgfluxite"
MC_PORT_LOW = 25565
MC_PORT_HIGH = 25600

FIREWALL_RULES = [
    ("Fluxite WG Block UDP", "UDP", None, None),
    ("Fluxite WG Block Low TCP", "TCP", 1, MC_PORT_LOW - 1),
    ("Fluxite WG Block High TCP", "TCP", MC_PORT_HIGH + 1, 65535),
]