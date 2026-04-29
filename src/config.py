"""
Agent configuration — API endpoints, file paths, and defaults.
"""

import os
import sys

API_BASE = os.getenv("MC_API_BASE", "https://gnalin.xyz/api")
IS_FROZEN = getattr(sys, 'frozen', False)

if IS_FROZEN:
    # running as installed exe - use APPDATA
    BASE_DIR = os.path.join(os.environ.get('APPDATA'), 'MCAgent')
else:
    # running in dev - use project root
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
KEY_FILE = "agent.key"
ID_FILE = "agent_id.txt"

# Timouts
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

# API Endpoints
CREATE_SERVER_URL = f"{API_BASE}/server"
SERVER_STATUS_URL_TPL = API_BASE + "/server/{}/status"
COMMAND_STATUS_URL_TPL = API_BASE + "/command/{}/status"
COMMAND_FEEDBACK_URL_TPL = API_BASE + "/command/{}/feedback"

# Polling
TOKEN_REFRESH_INTERVAL = 60 * 30   # 30 minutes
REQUEST_TIMEOUT = 10               # seconds
