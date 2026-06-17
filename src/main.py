#!/usr/bin/env python3
"""
MC Server Agent — entry point.

1. Loads or creates agent credentials.
2. Obtains a JWT via signed token refresh.
3. Starts the command poller at 1 Hz.
4. Reads commands from the queue and logs them (execution to be added later).
"""

import logging
import queue
import sys
import os
import subprocess
import requests
import shutil

from auth import AgentAuth, link_agent, load_signing_key, load_agent_id, load_init_config
from commands import CommandQueue, CommandType
from poller import CommandPoller
from server_manager import MCServerManager
from models import ServerRegistry, ServerMetadata
from installer import setup_server_directory, parse_server_properties
from config import SERVERS_BASE_DIR, CREATE_SERVER_URL, UPDATE_SERVER_THUMBNAIL, REQUEST_TIMEOUT, TMP_DIR
from agent_log_manager import AgentLogManager, rotate_agent_logs, create_agent_file_handler
from utils import uuid7

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("agent")

# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

def init_auth() -> AgentAuth:
    """Load saved credentials or run the interactive linking flow."""
    init_config = load_init_config()
    signing_key = load_signing_key()
    agent_id = load_agent_id()

    if not (signing_key and agent_id):
        log.info("No saved credentials found — starting linking flow")
        log.info(init_config)
        if init_config["linking_code"]:
            code = init_config["linking_code"]
            name = init_config["agent_name"] if init_config["agent_name"] else os.environ.get('COMPUTERNAME')
        else:
            name = input("Agent name: ").strip()
            code = input("Linking code: ").strip()
        if not name or not code:
            log.error("Agent name and linking code are required")
            sys.exit(1)
        if not os.path.exists("C:/Program Files/WireGuard/wireguard.exe"):
            log.error("WireGuard not installed")
            sys.exit(1)

        agent_id, tunnel_config = link_agent(name, code)
        # Write WireGuard conf file
        conf_content = (
            "[Interface]\n"
            f"PrivateKey = {tunnel_config['wg_priv_b64']}\n"
            f"Address = {tunnel_config['assigned_ip']}/32\n"
            "[Peer]\n"
            f"PublicKey = {tunnel_config['server_wg_pubkey']}\n"
            f"Endpoint = {tunnel_config['tunnel_endpoint']}:51820\n"
            f"AllowedIPs = {tunnel_config['server_wg_ip']}/32\n"
            "PersistentKeepalive = 25\n"
        )
        wireguard_conf_path = os.path.join(TMP_DIR, "wgfluxite.conf")
        if not os.path.exists(TMP_DIR):
            os.makedirs(TMP_DIR, exist_ok=True)
        with open(wireguard_conf_path, "w") as f:
            f.write(conf_content)

        signing_key = load_signing_key()

        # Register WireGuard tunnel with the Windows service (requires admin privileges)
        try:
            subprocess.run(
                [
                    "powershell",
                    "-Command",
                    f'Start-Process -FilePath "C:/Program Files/WireGuard/wireguard.exe" -ArgumentList "/uninstalltunnelservice", "wgfluxite" -Verb RunAs -Wait'
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False  # Don't raise error if tunnel doesn't exist
            )

            # Install the new tunnel
            result = subprocess.run(
                [
                    "powershell",
                    "-Command",
                    f'Start-Process -FilePath "C:/Program Files/WireGuard/wireguard.exe" -ArgumentList "/installtunnelservice", "{wireguard_conf_path}" -Verb RunAs -Wait'
                ],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0:
                log.info("WireGuard tunnel installed via wireguard.exe")
            else:
                log.warning("wireguard.exe returned %d: %s", result.returncode, result.stderr.strip())
        except FileNotFoundError:
            log.warning("wireguard.exe not found — tunnel not installed")
        except Exception as e:
            log.warning("Failed to install WireGuard tunnel: %s", e)

        os.remove(wireguard_conf_path)

    auth = AgentAuth(signing_key, agent_id)
    auth.ensure_token()
    return auth

# ---------------------------------------------------------------------------
# Server Management Helpers
# ---------------------------------------------------------------------------

def register_server_with_api(auth: AgentAuth, meta: ServerMetadata):
    """Register the newly created server with the cloud panel API."""
    payload = {
        "server_id": meta.id,
        "server_name": meta.name,
        "server_port": meta.port,
        "properties": meta.properties
    }
    try:
        headers = auth.auth_header()
        create_server_resp = requests.post(CREATE_SERVER_URL, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
        create_server_resp.raise_for_status()
        log.info("Server %s successfully registered with API", meta.id)
        update_server_thumbnail_resp = requests.put(UPDATE_SERVER_THUMBNAIL.format(meta.id), json={"server_thumbnail": meta.server_thumbnail}, headers=headers, timeout=REQUEST_TIMEOUT)
        update_server_thumbnail_resp.raise_for_status()
        log.info("Server %s thumbnail updated", meta.id)
    except Exception as e:
        log.error("Failed to register server %s with API: %s", meta.id, e)

# ---------------------------------------------------------------------------
# Command processing
# ---------------------------------------------------------------------------

def handle_command(cmd, poller: CommandPoller, registry: ServerRegistry, 
                   instances: dict[str, MCServerManager], auth: AgentAuth,
                   agent_log_mgr: AgentLogManager) -> tuple[bool, str]:
    """
    Process a single command from the queue.
    Returns tuple of (success, feedback_message).
    """
    
    # Global commands (no server_id required)

    # Agent log commands
    if cmd.type == CommandType.START_AGENT_LOG_STREAM:
        request_id = cmd.payload.get("request_id")
        logs_history_lines = cmd.payload.get("logs_history_lines", 0)
        if request_id:
            try:
                lines_count = int(logs_history_lines) if logs_history_lines else 0
            except (ValueError, TypeError):
                lines_count = 0
            if agent_log_mgr.start_log_stream(request_id, lines_count):
                return True, "Agent log stream started successfully"
            else:
                return False, "Log stream failed to start: Unable to start stream thread"
        else:
            log.warning("START_AGENT_LOG_STREAM missing 'request_id' in payload")
            return False, "Log stream failed to start: Missing request_id"
    elif cmd.type == CommandType.STOP_AGENT_LOG_STREAM:
        request_id = cmd.payload.get("request_id")
        if request_id:
            if agent_log_mgr.stop_log_stream(request_id):
                return True, "Agent log stream stopped successfully"
            else:
                return False, "Log stream stop failed: Request_id not found"
        else:
            log.warning("STOP_AGENT_LOG_STREAM missing 'request_id' in payload")
            return False, "Log stream stop failed: Missing request_id"
    elif cmd.type == CommandType.SEND_AGENT_LOGS:
        start_line = cmd.payload.get("logs_start_line")
        end_line = cmd.payload.get("logs_end_line")
        if start_line is None or end_line is None:
            log.warning("SEND_AGENT_LOGS missing 'logs_start_line' or 'logs_end_line'")
            return False, "Failed to send logs: Missing logs_start_line or logs_end_line"
        if agent_log_mgr.send_logs_range(int(start_line), int(end_line)):
            return True, f"Agent logs sent successfully (lines {start_line}-{end_line})"
        else:
            return False, f"Failed to send logs: Invalid log range [{start_line}, {end_line}]"

    # Server Creation
    elif cmd.type == CommandType.CREATE_SERVER:
        log.info("Processing CREATE_SERVER command...")
        from config import SERVERS_BASE_DIR
        from installer import setup_server_directory, parse_server_properties
        
        server_name = cmd.payload.get("name")
        mc_version = cmd.payload.get("mc_version")
        loader_type = cmd.payload.get("loader_type")
        thumbnail_url = cmd.payload.get("server_thumbnail")
        if loader_type != "vanilla":
            loader_version = cmd.payload.get("loader_version")
            if not server_name or not mc_version or not loader_version or not loader_type:
                log.error("CREATE_SERVER failed: 'name', 'mc_version', 'loader_version' and 'loader_type' are required.")
                return False, "Server creation failed: Missing required parameters (name, mc_version, loader_version, loader_type)"
        else:
            loader_version = None
            if not server_name or not mc_version or not loader_type:
                log.error("CREATE_SERVER failed: 'name', 'mc_version' and 'loader_type' are required for vanilla servers.")
                return False, "Server creation failed: Missing required parameters (name, mc_version, loader_type)"

        server_id = uuid7()
        port = registry.get_next_available_port()
        server_path = os.path.join(SERVERS_BASE_DIR, server_id)
        
        log.info("Installing server %s (%s) on port %d...", server_name, server_id, port)
        try:
            entrypoint, java_version = setup_server_directory(
                server_id, mc_version, loader_version, loader_type, port, server_name, thumbnail_url
            )
            
            if not entrypoint:
                log.error("Failed to install server %s", server_id)
                if os.path.exists(server_path):
                    log.info("Cleaning up failed installation directory: %s", server_path)
                    shutil.rmtree(server_path)
                return False, f"Server creation failed: Failed to download and install server JAR for {server_name}"
        except Exception as e:
            log.error("Exception during server %s installation: %s", server_id, e)
            if os.path.exists(server_path):
                log.info("Cleaning up after exception in %s: %s", server_id, server_path)
                shutil.rmtree(server_path)
            return False, f"Server creation failed: Exception during installation - {str(e)}"

        server_path = os.path.join(SERVERS_BASE_DIR, server_id)
        properties = parse_server_properties(server_path)
        
        # Add mc_version and loader_type to properties
        properties["mc_version"] = mc_version
        properties["loader_type"] = loader_type
        
        meta = ServerMetadata(
            id=server_id,
            name=server_name,
            port=port,
            path=server_path,
            entrypoint=entrypoint,
            server_thumbnail=thumbnail_url,
            java_version=java_version,
            properties=properties
        )
        
        registry.add_server(meta)
        register_server_with_api(auth, meta)
        
        mgr = MCServerManager(meta, auth)
        instances[server_id] = mgr
        if mgr.start():
            return True, f"Server {server_name} created and started successfully"
        else:
            return True, f"Server {server_name} created successfully but failed to start"

    # Modpack Creation
    elif cmd.type == CommandType.CREATE_MODPACK:
        log.info("Processing CREATE_MODPACK command...")
        from config import MODPACK_DOWNLOAD_URL_TPL, MODRINTH_USER_AGENT, TMP_DIR, SERVERS_BASE_DIR
        from installer import download_file, setup_server_directory, parse_server_properties
        from modpack_installer import (
            extract_manifest_from_mrpack,
            parse_manifest_dependencies,
            download_manifest_files,
            apply_overrides,
            cleanup_server_directory
        )
        
        modpack_name = cmd.payload.get("name")
        project_id = cmd.payload.get("project_id")
        version_id = cmd.payload.get("version_id")
        file_name = cmd.payload.get("file_name")
        expected_hash = cmd.payload.get("manifest_hash")
        
        if not modpack_name or not project_id or not version_id or not file_name or not expected_hash:
            log.error("CREATE_MODPACK failed: 'name', 'project_id', 'version_id', 'file_name', and 'manifest_hash' are all required.")
            return False, "Modpack creation failed: Missing required parameters (name, project_id, version_id, file_name, manifest_hash)"
        
        # Create tmp directory if it doesn't exist
        tmp_path = os.path.join(TMP_DIR, "modpacks")
        os.makedirs(tmp_path, exist_ok=True)
        
        # Construct download URL
        url = MODPACK_DOWNLOAD_URL_TPL.format(
            project_id=project_id,
            version_id=version_id,
            file_name=file_name
        )
        
        # Download mrpack manifest
        mrpack_path = os.path.join(tmp_path, file_name)
        
        log.info("Downloading modpack %s from %s...", modpack_name, url)
        
        headers = {"User-Agent": MODRINTH_USER_AGENT}
        if not download_file(url, mrpack_path, expected_hash=expected_hash, headers=headers):
            log.error("Failed to download modpack mrpack %s", modpack_name)
            return False, f"Modpack creation failed: Failed to download mrpack {file_name}"
        
        log.info("Modpack mrpack downloaded successfully to %s", mrpack_path)
        
        # Extract and validate manifest
        manifest = extract_manifest_from_mrpack(mrpack_path)
        if not manifest:
            return False, "Modpack creation failed: Could not extract manifest from mrpack"
        
        # Extract info from manifest
        try:
            loader_type, mc_version, loader_version = parse_manifest_dependencies(manifest)
        except Exception as e:
            log.error("Failed to parse manifest dependencies: %s", e)
            return False, f"Modpack creation failed: Failed to parse manifest dependencies - {e}"
        
        log.info("Modpack info - Minecraft: %s, %s Loader: %s", mc_version, loader_type.capitalize(), loader_version)
        
        # Generate server ID and port
        server_id = uuid7()
        port = registry.get_next_available_port()
        
        log.info("Installing modpack server %s (%s) on port %d...", modpack_name, server_id, port)
        
        try:
            # Install Server with loader
            entrypoint, java_version = setup_server_directory(
                server_id, mc_version, loader_version, loader_type, port, modpack_name, thumbnail_url
            )
            
            if not entrypoint:
                cleanup_server_directory(server_id)
                return False, "Modpack installation failed: Could not find server entrypoint"
            
            server_path = os.path.join(SERVERS_BASE_DIR, server_id)
            
            # Download all files from manifest
            success, message, modules_metadata = download_manifest_files(manifest, server_path, headers)
            if not success:
                cleanup_server_directory(server_id)
                return False, f"Modpack creation failed: {message}"
            
            log.info("Downloaded %d files from manifest", len(modules_metadata))
            
            # Apply overrides
            success, message = apply_overrides(mrpack_path, server_path)
            if not success:
                cleanup_server_directory(server_id)
                return False, f"Modpack creation failed: {message}"
            
            log.info("%s", message)
            
            # Parse server properties
            properties = parse_server_properties(server_path)
            
            # Add mc_version and loader_type to properties
            properties["mc_version"] = mc_version
            properties["loader_type"] = "fabric"
            
            # Create server metadata with modules
            meta = ServerMetadata(
                id=server_id,
                name=modpack_name,
                port=port,
                path=server_path,
                entrypoint=entrypoint,
                server_thumbnail=None,
                java_version=java_version,
                properties=properties,
                modules=modules_metadata,
                modpack=True,
                modpack_source=file_name
            )
            
            # Register with registry and API
            registry.add_server(meta)
            register_server_with_api(auth, meta)
            
            # Report installed modules to API
            report_installed_modules(auth, server_id, modules_metadata)
            
            # Create server manager and start
            mgr = MCServerManager(meta, auth)
            instances[server_id] = mgr
            
            # Cleanup temporary mrpack file
            if os.path.exists(mrpack_path):
                log.info("Cleaning up temporary mrpack file: %s", mrpack_path)
                os.remove(mrpack_path)
            
            if mgr.start():
                return True, f"Modpack server {modpack_name} installed and started successfully"
            else:
                return True, f"Modpack server {modpack_name} installed successfully but failed to start"
        
        except Exception as e:
            log.error("Exception during modpack installation: %s", e)
            cleanup_server_directory(server_id)
            # Also cleanup mrpack on failure
            if os.path.exists(mrpack_path):
                log.info("Cleaning up temporary mrpack file after failure: %s", mrpack_path)
                os.remove(mrpack_path)
            return False, f"Modpack creation failed: {str(e)}"

    # Server-specific commands
    if not cmd.server_id:
        log.warning("Received server command %s without server_id", cmd.type.value)
        return False, f"Command failed: Missing server_id for {cmd.type.value}"

    mgr = instances.get(cmd.server_id)
    if not mgr:
        log.error("Received command %s for unknown server_id: %s", cmd.type.value, cmd.server_id)
        return False, f"Command failed: Server {cmd.server_id} not found"

    if cmd.type == CommandType.START_SERVER:
        if mgr.is_running():
            return True, "Server was already running"
        if mgr.start():
            return True, "Server start attempted successfully"
        else:
            return False, "Failed to start server: Unable to start server process"
    elif cmd.type == CommandType.STOP_SERVER:
        if not mgr.is_running():
            return True, "Server was already stopped"
        if mgr.stop():
            return True, "Server stopped successfully"
        else:
            return False, "Server stop failed: Server process is running but couldn't be stopped within timeout"
    elif cmd.type == CommandType.KILL_SERVER:
        if not mgr.is_running():
            return True, "Server was already stopped"
        if mgr.kill():
            return True, "Server killed successfully"
        else:
            return False, "Server kill failed: Unable to kill server process"
    elif cmd.type == CommandType.RESTART_SERVER:
        # Check if we can stop the server first
        was_running = mgr.is_running()
        if was_running and not mgr.stop():
            return False, "Server restart failed: Failed to stop existing server"
        # Now attempt to start
        if mgr.start():
            return True, "Server restart attempted successfully"
        else:
            return False, "Server restart failed: Failed to start new server process"
    elif cmd.type == CommandType.START_SERVER_LOG_STREAM:
        request_id = cmd.payload.get("request_id")
        logs_history_lines = cmd.payload.get("logs_history_lines", 0)
        if request_id:
            try:
                lines_count = int(logs_history_lines) if logs_history_lines else 0
            except (ValueError, TypeError):
                lines_count = 0
            if mgr.start_log_stream(request_id, lines_count):
                return True, "Server log stream started successfully"
            else:
                return False, "Log stream failed to start: Unable to start stream thread"
        else:
            log.warning("START_SERVER_LOG_STREAM for %s missing 'request_id' in payload", cmd.server_id)
            return False, "Log stream failed to start: Missing request_id"
    elif cmd.type == CommandType.STOP_SERVER_LOG_STREAM:
        request_id = cmd.payload.get("request_id")
        if request_id:
            if mgr.stop_log_stream(request_id):
                return True, "Server log stream stopped successfully"
            else:
                return False, "Log stream stop failed: Request_id not found"
        else:
            log.warning("STOP_SERVER_LOG_STREAM for %s missing 'request_id' in payload", cmd.server_id)
            return False, "Log stream stop failed: Missing request_id"
    elif cmd.type == CommandType.MC_COMMAND:
        mc_cmd = cmd.payload.get("command")
        if mc_cmd:
            if not mgr.is_running():
                return False, "Command failed: Server not running"
            if mgr.send_command(mc_cmd):
                return True, f"Command '{mc_cmd}' sent successfully"
            else:
                return False, "Command failed: Unable to send to server"
        else:
            log.warning("MC_COMMAND for %s missing 'command' in payload", cmd.server_id)
            return False, "Command failed: Missing command in payload"
    elif cmd.type == CommandType.INSTALL_MODULES:
        modules_to_install = cmd.payload.get("modules")
        if not modules_to_install:
            log.warning("INSTALL_MODULES for %s missing 'modules' in payload", cmd.server_id)
            return False, "Module installation failed: Missing modules in payload"
            
        server_path = mgr.meta.path
        
        from config import MODULE_DOWNLOAD_URL_TPL, MODRINTH_USER_AGENT, VALID_MODULE_TYPES, MODULE_TYPE_FOLDERS, DEFAULT_MODULE_ICON_URL
        from installer import get_modrinth_project_data
        
        downloaded_files = []
        new_modules_metadata = []
        
        try:
            for module in modules_to_install:
                project_id = module.get("project_id")
                version_id = module.get("version_id")
                file_name = module.get("file_name")
                file_hash = module.get("hash")
                module_type = module.get("module_type")
                module_id = uuid7()
                
                # Validate required fields
                if not all([project_id, version_id, file_name, file_hash]):
                    log.error("Invalid module data in INSTALL_MODULES: %s", module)
                    raise ValueError("Invalid module data")
                
                # Validate module_type
                if not module_type or module_type not in VALID_MODULE_TYPES:
                    log.error("Invalid or missing module_type in INSTALL_MODULES: %s (valid types: %s)", 
                              module_type, VALID_MODULE_TYPES)
                    raise ValueError(f"Invalid module_type: {module_type}")
                
                # Get destination folder based on module type
                folder = MODULE_TYPE_FOLDERS.get(module_type, 'modules')
                dest_folder = os.path.join(server_path, folder)
                os.makedirs(dest_folder, exist_ok=True)
                
                url = MODULE_DOWNLOAD_URL_TPL.format(
                    project_id=project_id,
                    version_id=version_id,
                    file_name=file_name
                )
                dest_path = os.path.join(dest_folder, module_id + ".jar")
                
                headers = {"User-Agent": MODRINTH_USER_AGENT}
                from installer import download_file
                if download_file(url, dest_path, expected_hash=file_hash, headers=headers):
                    # Fetch project data from Modrinth API
                    project_data = get_modrinth_project_data(project_id, DEFAULT_MODULE_ICON_URL)
                    
                    # Determine module name: use project name if available, otherwise file_name
                    module_name = project_data.get("name") or module.get("module_name", file_name)
                    icon_url = project_data.get("icon_url")
                    
                    # All mods treated as required/enabled
                    module_enabled = True
                    
                    downloaded_files.append(dest_path)
                    new_modules_metadata.append({
                        "module_id": module_id,
                        "module_type": module_type,
                        "module_name": module_name,
                        "module_enabled": module_enabled,
                        "module_metadata": {
                            "icon_url": icon_url,
                            "project_id": project_id,
                            "version_id": version_id,
                            "file_name": file_name
                        }
                    })
                else:
                    raise Exception(f"Failed to download module: {file_name}")
            
            # All downloads succeeded
            mgr.meta.modules.extend(new_modules_metadata)
            registry.save()
            
            # Report to API
            report_installed_modules(auth, cmd.server_id, new_modules_metadata)
            return True, f"Successfully installed {len(new_modules_metadata)} modules"
            
        except Exception as e:
            log.error("Installation of modules failed for server %s: %s", cmd.server_id, e)
            # Rollback: Delete files downloaded in this batch
            for path in downloaded_files:
                if os.path.exists(path):
                    log.info("Rolling back module: %s", path)
                    os.remove(path)
            return False, f"Module installation failed: {str(e)}"
    elif cmd.type == CommandType.DELETE_MODULE:
        module_id = cmd.payload.get("module_id")
        if not module_id:
            log.warning("DELETE_MODULE for %s missing 'module_id' in payload", cmd.server_id)
            return False, "Module deletion failed: Missing module_id"
            
        server_path = mgr.meta.path
        
        # Find module in registry to get module_type
        module_entry = None
        for m in mgr.meta.modules:
            if m.get("module_id") == module_id:
                module_entry = m
                break
        
        try:
            # Determine module path based on module_type from registry
            if module_entry and module_entry.get("module_type"):
                module_type = module_entry.get("module_type")
                from config import MODULE_TYPE_FOLDERS
                folder = MODULE_TYPE_FOLDERS.get(module_type, 'modules')
                module_path = os.path.join(server_path, folder, f"{module_id}.jar")
            else:
                # Fallback: search all module folders
                from config import MODULE_TYPE_FOLDERS
                module_path = None
                for folder in MODULE_TYPE_FOLDERS.values():
                    candidate_path = os.path.join(server_path, folder, f"{module_id}.jar")
                    if os.path.exists(candidate_path):
                        module_path = candidate_path
                        break
                
                # Last resort: check legacy 'modules' folder
                if not module_path:
                    module_path = os.path.join(server_path, "modules", f"{module_id}.jar")
            
            # 1. Delete the file
            if os.path.exists(module_path):
                os.remove(module_path)
                log.info("Deleted module file: %s", module_path)
            else:
                log.warning("Module file not found for deletion: %s", module_path)
            
            # 2. Remove from registry
            mgr.meta.modules = [m for m in mgr.meta.modules if m.get("module_id") != module_id]
            registry.save()
            
            # 3. Report to API
            report_module_deleted(auth, module_id)
            return True, f"Module {module_id} deleted successfully"
            
        except Exception as e:
            log.error("Failed to delete module %s for server %s: %s", module_id, cmd.server_id, e)
            return False, f"Module deletion failed: {str(e)}"
    elif cmd.type == CommandType.ENABLE_MODULE:
        module_id = cmd.payload.get("module_id")
        if not module_id:
            log.warning("ENABLE_MODULE for %s missing 'module_id' in payload", cmd.server_id)
            return False, "Module enable failed: Missing module_id"
        
        try:
            # Find module in registry
            module_entry = None
            for m in mgr.meta.modules:
                if m.get("module_id") == module_id:
                    module_entry = m
                    break
            
            if not module_entry:
                return False, f"Module {module_id} not found"
            
            # Check if already enabled
            if module_entry.get("module_enabled", True):
                return True, f"Module {module_id} is already enabled"
            
            # Find the disabled file
            module_type = module_entry.get("module_type", "mod")
            from config import MODULE_TYPE_FOLDERS
            folder = MODULE_TYPE_FOLDERS.get(module_type, 'mods')
            server_path = mgr.meta.path
            
            disabled_path = os.path.join(server_path, folder, f"{module_id}.jar.disabled")
            enabled_path = os.path.join(server_path, folder, f"{module_id}.jar")
            
            if os.path.exists(disabled_path):
                os.rename(disabled_path, enabled_path)
                log.info("Enabled module %s: %s -> %s", module_id, disabled_path, enabled_path)
            else:
                log.warning("Disabled module file not found: %s", disabled_path)
                # Check if already enabled
                if not os.path.exists(enabled_path):
                    return False, f"Module file not found for {module_id}"
            
            # Update registry
            module_entry["module_enabled"] = True
            registry.save()
            
            # Report to API
            report_module_status(auth, module_id, True)
            return True, f"Module {module_id} enabled successfully"
            
        except Exception as e:
            log.error("Failed to enable module %s for server %s: %s", module_id, cmd.server_id, e)
            return False, f"Module enable failed: {str(e)}"
    elif cmd.type == CommandType.DISABLE_MODULE:
        module_id = cmd.payload.get("module_id")
        if not module_id:
            log.warning("DISABLE_MODULE for %s missing 'module_id' in payload", cmd.server_id)
            return False, "Module disable failed: Missing module_id"
        
        try:
            # Find module in registry
            module_entry = None
            for m in mgr.meta.modules:
                if m.get("module_id") == module_id:
                    module_entry = m
                    break
            
            if not module_entry:
                return False, f"Module {module_id} not found"
            
            # Check if already disabled
            if not module_entry.get("module_enabled", True):
                return True, f"Module {module_id} is already disabled"
            
            # Find the enabled file
            module_type = module_entry.get("module_type", "mod")
            from config import MODULE_TYPE_FOLDERS
            folder = MODULE_TYPE_FOLDERS.get(module_type, 'mods')
            server_path = mgr.meta.path
            
            enabled_path = os.path.join(server_path, folder, f"{module_id}.jar")
            disabled_path = os.path.join(server_path, folder, f"{module_id}.jar.disabled")
            
            if os.path.exists(enabled_path):
                os.rename(enabled_path, disabled_path)
                log.info("Disabled module %s: %s -> %s", module_id, enabled_path, disabled_path)
            else:
                log.warning("Enabled module file not found: %s", enabled_path)
                # Check if already disabled
                if not os.path.exists(disabled_path):
                    return False, f"Module file not found for {module_id}"
            
            # Update registry
            module_entry["module_enabled"] = False
            registry.save()
            
            # Report to API
            report_module_status(auth, module_id, False)
            return True, f"Module {module_id} disabled successfully"
            
        except Exception as e:
            log.error("Failed to disable module %s for server %s: %s", module_id, cmd.server_id, e)
            return False, f"Module disable failed: {str(e)}"
    elif cmd.type == CommandType.SEND_SERVER_LOGS:
        start_line = cmd.payload.get("logs_start_line")
        end_line = cmd.payload.get("logs_end_line")
        if start_line is None or end_line is None:
            log.warning("SEND_SERVER_LOGS for %s missing 'logs_start_line' or 'logs_end_line'", cmd.server_id)
            return False, "Failed to send logs: Missing logs_start_line or logs_end_line"
        if mgr.send_logs_range(int(start_line), int(end_line)):
            return True, f"Server logs sent successfully (lines {start_line}-{end_line})"
        else:
            return False, f"Failed to send logs: Invalid log range [{start_line}, {end_line}]"
    elif cmd.type == CommandType.DELETE_SERVER:
        server_id = cmd.server_id
        if not server_id:
            log.warning("DELETE_SERVER command missing server_id")
            return False, "Server deletion failed: Missing server_id"
            
        mgr = instances.get(server_id)
        if mgr:
            log.info("Stopping server %s before deletion...", server_id)
            mgr.stop()
            mgr.shutdown()
            del instances[server_id]
        
        meta = registry.get_server(server_id)
        if meta and os.path.exists(meta.path):
            log.info("Deleting server directory: %s", meta.path)
            shutil.rmtree(meta.path)
        
        registry.remove_server(server_id)
        report_server_deleted(auth, server_id)
        return True, f"Server {server_id} deleted successfully"
    else:
        log.info("Received unhandled command [%s] type=%s", cmd.id, cmd.type.value)
        return False, f"Command failed: Unknown command type {cmd.type.value}"

def report_command_status(auth: AgentAuth, command_id: str, success: bool):
    """Report the success or failure of a command to the API."""
    from config import COMMAND_STATUS_URL_TPL
    
    url = COMMAND_STATUS_URL_TPL.format(command_id)
    payload = {"command_status": "success" if success else "failure"}
    
    try:
        headers = auth.auth_header()
        resp = requests.put(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        log.info("Reported command status for command %s: %s", command_id, payload["command_status"])
    except Exception as e:
        log.error("Failed to report command status for command %s: %s", command_id, e)

def report_command_feedback(auth: AgentAuth, command_id: str, feedback: str):
    """Send user-friendly feedback for a command."""
    from config import COMMAND_FEEDBACK_URL_TPL
    
    url = COMMAND_FEEDBACK_URL_TPL.format(command_id)
    payload = {"command_feedback": feedback}
    
    try:
        headers = auth.auth_header()
        resp = requests.put(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        log.info("Sent feedback for command %s", command_id)
    except Exception as e:
        log.error("Failed to send feedback for command %s: %s", command_id, e)

def report_agent_offline(auth: AgentAuth):
    """Report that the agent is going offline."""
    from config import AGENT_URL
    payload = {"agent_status": "offline"}
    try:
        headers = auth.auth_header()
        resp = requests.put(AGENT_URL, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 204:
            log.info("Agent successfully reported offline status.")
        else:
            log.warning("Agent report offline status returned %d: %s", resp.status_code, resp.text)
    except Exception as e:
        log.error("Failed to report agent offline status: %s", e)

def report_installed_modules(auth: AgentAuth, server_id: str, modules: list[dict]):
    """Report the list of newly installed modules to the API."""
    from config import REPORT_MODULES_URL_TPL
    url = REPORT_MODULES_URL_TPL.format(server_id)
    try:
        headers = auth.auth_header()
        resp = requests.post(url, json=modules, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        log.info("Reported %d installed modules for server %s", len(modules), server_id)
    except Exception as e:
        log.error("Failed to report installed modules for server %s: %s", server_id, e)

def report_module_deleted(auth: AgentAuth, module_id: str):
    """Report the deletion of a module to the API."""
    from config import DELETE_MODULE_URL_TPL
    url = DELETE_MODULE_URL_TPL.format(module_id)
    try:
        headers = auth.auth_header()
        resp = requests.delete(url, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        log.info("Reported deleted module %s to API", module_id)
    except Exception as e:
        log.error("Failed to report deleted module %s to API: %s", module_id, e)

def report_module_status(auth: AgentAuth, module_id: str, module_enabled: bool):
    """Report the enabled/disabled status of a module to the API via PATCH."""
    from config import UPDATE_MODULE_URL_TPL
    url = UPDATE_MODULE_URL_TPL.format(module_id)
    payload = {"module_enabled": "true" if module_enabled else "false"}
    try:
        headers = auth.auth_header()
        resp = requests.patch(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        log.info("Reported module %s status as %s to API", module_id, payload["module_enabled"])
    except Exception as e:
        log.error("Failed to report module %s status to API: %s", module_id, e)

def report_server_deleted(auth: AgentAuth, server_id: str):
    """Report the deletion of a server to the API."""
    from config import DELETE_SERVER_URL_TPL
    url = DELETE_SERVER_URL_TPL.format(server_id)
    try:
        headers = auth.auth_header()
        resp = requests.delete(url, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        log.info("Reported deleted server %s to API", server_id)
    except Exception as e:
        log.error("Failed to report deleted server %s to API: %s", server_id, e)

# ---------------------------------------------------------------------------
# Required Files Initialization
# ---------------------------------------------------------------------------

def ensure_required_files() -> bool:
    """Download required installer files and JDKs on startup if they don't exist.
    
    Returns True if all files are present/ downloaded successfully,
    False if any download failed.
    """
    from config import REQUIRED_DOWNLOADS, JDK_VERSIONS, RUNTIMES_DIR, get_jdk_download_config
    from installer import download_file, download_and_extract
    
    log.info("Checking required files...")
    all_ok = True
    
    # Check and download installer files (hashes fetched dynamically)
    for local_path, (url,) in REQUIRED_DOWNLOADS.items():
        if os.path.exists(local_path):
            continue
        
        log.info("Downloading required file: %s", os.path.basename(local_path))
        if download_file(url, local_path):
            pass
        else:
            log.error("Failed to download required file: %s", local_path)
            all_ok = False
    
    # Check and download JDKs
    log.info("Checking required JDKs...")
    for java_version in JDK_VERSIONS:
        jdk_path = os.path.join(RUNTIMES_DIR, f"jdk{java_version}")
        java_executable = os.path.join(jdk_path, "bin", "java" + (".exe" if sys.platform == "win32" else ""))
        
        # Check if JDK is already installed
        if os.path.exists(java_executable):
            log.debug("JDK %d found at %s", java_version, jdk_path)
            continue
        
        log.info("JDK %d not found, downloading...", java_version)
        url, expected_hash, archive_type = get_jdk_download_config(java_version)
        
        if download_and_extract(url, jdk_path, expected_hash=expected_hash, archive_type=archive_type):
            log.info("JDK %d downloaded and extracted successfully", java_version)
        else:
            log.error("Failed to download/extract JDK %d", java_version)
            all_ok = False
    
    return all_ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=== MC Server Multi-Agent ===")
    
    if not os.path.exists(SERVERS_BASE_DIR):
        os.makedirs(SERVERS_BASE_DIR)

    # Rotate agent logs and attach a file handler
    agent_log_path = rotate_agent_logs()
    file_handler = create_agent_file_handler(agent_log_path)
    logging.getLogger().addHandler(file_handler)
    log.info("Agent logs writing to %s", agent_log_path)

    auth = init_auth()
    
    # Download required files if missing
    if not ensure_required_files():
        log.error("Failed to download required files. Continuing anyway...")
    
    registry = ServerRegistry()
    cmd_queue = CommandQueue()
    poller = CommandPoller(auth, cmd_queue)
    
    # Initialize agent log manager
    agent_log_mgr = AgentLogManager(auth)

    # Initialize existing server managers
    instances: dict[str, MCServerManager] = {}
    for meta in registry.list_servers():
        instances[meta.id] = MCServerManager(meta, auth)
        log.info("Initialized manager for server %s (%s)", meta.name, meta.id)

    poller.start()
    log.info("Poller running (SSE mode). Press Ctrl+C to stop.")

    try:
        while True:
            try:
                cmd = cmd_queue.get(block=True, timeout=1.0)
            except queue.Empty:
                continue
            success, feedback = handle_command(cmd, poller, registry, instances, auth, agent_log_mgr)
            if cmd.id:
                report_command_status(auth, cmd.id, success)
                report_command_feedback(auth, cmd.id, feedback)
    except KeyboardInterrupt:
        print()
        log.info("Shutting down...")
    finally:
        poller.stop()
        agent_log_mgr.shutdown()
        for mgr in instances.values():
            mgr.shutdown()
        report_agent_offline(auth)
        log.info("Agent stopped. (%d commands pending)", cmd_queue.pending)

if __name__ == "__main__":
    main()
