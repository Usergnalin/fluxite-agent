"""
Handles modpack installation from .mrpack files.
"""

import os
import json
import zipfile
import logging
import shutil
from typing import Dict, Any
from installer import download_file
from config import SERVERS_BASE_DIR, MODRINTH_USER_AGENT, REQUEST_TIMEOUT, MODULE_TYPE_FOLDERS, MODRINTH_BULK_PROJECTS_URL, MODRINTH_BULK_VERSIONS_URL, MODRINTH_BULK_BATCH_SIZE
import requests
from collections import deque

log = logging.getLogger(__name__)


def extract_manifest_from_mrpack(mrpack_path: str) -> dict | None:
    """Extract and parse modrinth.index.json from mrpack zip."""
    try:
        with zipfile.ZipFile(mrpack_path, 'r') as zf:
            if 'modrinth.index.json' not in zf.namelist():
                log.error("modrinth.index.json not found in mrpack")
                return None
            
            manifest_data = zf.read('modrinth.index.json')
            return json.loads(manifest_data)
    except Exception as e:
        log.error("Failed to extract manifest from mrpack: %s", e)
        return None

def parse_manifest_dependencies(manifest: dict) -> tuple[str, str, str | None]:
    """
    Parse loader type and versions from a modrinth.index.json manifest.
    Returns (loader_type, mc_version, loader_version)
    where loader_type is one of: 'vanilla', 'fabric', 'quilt', 'forge', 'neoforge'
    and loader_version is None for vanilla.
    Raises: ValueError on missing/ambiguous dependencies.
    """
    dependencies = manifest.get('dependencies', {})

    if not dependencies:
        raise ValueError("Manifest contains no dependencies field")

    mc_version_raw = dependencies.get('minecraft')
    if not mc_version_raw:
        raise ValueError("Manifest is missing required 'minecraft' dependency")

    mc_version = mc_version_raw.lstrip('~')

    LOADER_MAP = {
        'fabric-loader': 'fabric',
        'quilt-loader':  'quilt',
        'forge':         'forge',
        'neoforge':      'neoforge',
    }

    found = [(dep_key, loader) for dep_key, loader in LOADER_MAP.items() if dep_key in dependencies]

    if len(found) > 1:
        raise ValueError(f"Manifest specifies multiple loaders: {[l for _, l in found]}")

    if len(found) == 1:
        dep_key, loader_type = found[0]
        return loader_type, mc_version, dependencies[dep_key]

    return 'vanilla', mc_version, None


def get_module_type_from_path(path: str) -> str | None:
    """Determine module_type from manifest file path.
    Returns None for unknown types (will be skipped).
    
    Path format examples:
    - mods/fabric-api.jar → mod
    - resourcepacks/pack.zip → resource_pack
    - world/datapacks/pack.zip → data_pack
    - plugins/plugin.jar → plugin
    - config/... → None (skipped)
    """
    # Normalize path and split into parts
    path_parts = path.replace('\\', '/').split('/')
    
    if not path_parts:
        return None
    
    first_part = path_parts[0].lower()
    
    # Check for single-directory types
    if first_part == 'mods':
        return 'mod'
    if first_part == 'resourcepacks':
        return 'resource_pack'
    if first_part == 'plugins':
        return 'plugin'
    
    # Check for nested path: world/datapacks/
    if first_part == 'world' and len(path_parts) > 1 and path_parts[1].lower() == 'datapacks':
        return 'data_pack'
    
    # Unknown type - will be skipped
    return None


def is_valid_modrinth_id(id_str: str) -> bool:
    """Check if string is a valid Modrinth ID (base64 8 characters).
    Valid IDs contain only alphanumeric chars, hyphens, and underscores.
    """
    if not id_str:
        return False
    # Modrinth IDs are exactly 8 chars, base64-like (alphanumeric + -_)
    if len(id_str) != 8:
        return False
    valid_chars = set('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_')
    return all(c in valid_chars for c in id_str)


def resolve_version_id_from_strings(project_str: str, version_str: str, headers: dict) -> tuple[str | None, str | None]:
    """Resolve actual IDs from project/version strings via Modrinth API.
    URL: https://api.modrinth.com/v2/project/{project_str}/version/{version_str}
    Returns (project_id, version_id) or (None, None) on failure.
    """
    try:
        url = f"https://api.modrinth.com/v2/project/{project_str}/version/{version_str}"
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        
        version_id = data.get("id")
        project_id = data.get("project_id")
        
        if version_id and project_id:
            log.debug("Resolved strings '%s'/'%s' to IDs: %s/%s", project_str, version_str, project_id, version_id)
            return project_id, version_id
    except Exception as e:
        log.warning("Failed to resolve version strings '%s'/'%s': %s", project_str, version_str, e)
    
    return None, None


def extract_mod_info_from_url(url: str, headers: dict) -> tuple[str | None, str | None]:
    """Extract project_id and version_id from Modrinth CDN URL.
    URL format: https://cdn.modrinth.com/data/{project_id}/versions/{version_id}/{file_name}
    If the extracted identifiers are not valid Modrinth IDs (e.g., version string like "1.3.0"),
    resolves them via the Modrinth API.
    Returns (project_id, version_id) or (None, None) if invalid.
    """
    try:
        parts = url.split('/')
        if len(parts) >= 4:
            project_str = parts[-4] if parts[-4] != 'data' else parts[-3]
            version_str = parts[-2]
            
            # Check if both are valid IDs
            if is_valid_modrinth_id(project_str) and is_valid_modrinth_id(version_str):
                return project_str, version_str
            
            # One or both are strings - need to resolve via API
            log.debug("Invalid ID format detected in URL: project='%s', version='%s'. Resolving via API.", 
                      project_str, version_str)
            return resolve_version_id_from_strings(project_str, version_str, headers)
    except Exception:
        pass
    return None, None


def get_bulk_project_data(project_ids: list[str], headers: dict) -> dict[str, dict]:
    """Bulk fetch project data from Modrinth API.
    Returns dict[project_id, {"name": str, "icon_url": str, "server_side": str}].
    Batches requests to respect MODRINTH_BULK_BATCH_SIZE limit.
    Raises exception on API failure.
    """
    if not project_ids:
        return {}
    
    result = {}
    # Process in batches of MODRINTH_BULK_BATCH_SIZE
    for i in range(0, len(project_ids), MODRINTH_BULK_BATCH_SIZE):
        batch = project_ids[i:i + MODRINTH_BULK_BATCH_SIZE]
        params = {"ids": json.dumps(batch)}
        resp = requests.get(MODRINTH_BULK_PROJECTS_URL, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        
        data = resp.json()
        for proj in data:
            result[proj["id"]] = {
                "name": proj.get("title") or proj.get("name"),
                "icon_url": proj.get("icon_url"),
                "server_side": proj.get("server_side", "unknown")
            }
        
        log.debug("Fetched batch %d/%d of projects (%d items)", 
                  (i // MODRINTH_BULK_BATCH_SIZE) + 1, 
                  (len(project_ids) + MODRINTH_BULK_BATCH_SIZE - 1) // MODRINTH_BULK_BATCH_SIZE,
                  len(batch))
    
    return result


def get_version_dependencies(version_ids: list[str], headers: dict) -> dict[str, dict]:
    """Bulk fetch version dependencies from Modrinth API.
    Returns dict[version_id, {"project_id": str, "version_deps": list[str], "project_deps": list[str]}].
    version_deps: dep version_ids (resolved via version_id)
    project_deps: dep project_ids (resolved via project_id only, no specific version)
    Batches requests to respect MODRINTH_BULK_BATCH_SIZE limit.
    Raises exception on API failure.
    """
    if not version_ids:
        return {}
    
    result = {}
    # Process in batches of MODRINTH_BULK_BATCH_SIZE
    for i in range(0, len(version_ids), MODRINTH_BULK_BATCH_SIZE):
        batch = version_ids[i:i + MODRINTH_BULK_BATCH_SIZE]
        params = {"ids": json.dumps(batch)}
        resp = requests.get(MODRINTH_BULK_VERSIONS_URL, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        
        data = resp.json()
        for ver in data:
            version_deps = []  # deps with specific version_id
            project_deps = []  # deps with only project_id (no version specified)
            
            for dep in ver.get("dependencies", []):
                if dep.get("version_id"):
                    version_deps.append(dep["version_id"])
                elif dep.get("project_id"):
                    # Project-level dependency without specific version
                    project_deps.append(dep["project_id"])
            
            result[ver["id"]] = {
                "project_id": ver["project_id"],
                "version_deps": version_deps,
                "project_deps": project_deps
            }
        
        log.debug("Fetched batch %d/%d of versions (%d items)", 
                  (i // MODRINTH_BULK_BATCH_SIZE) + 1, 
                  (len(version_ids) + MODRINTH_BULK_BATCH_SIZE - 1) // MODRINTH_BULK_BATCH_SIZE,
                  len(batch))
    
    return result


def resolve_server_mods(
    mod_list: list[tuple[str, str]],  # [(project_id, version_id), ...]
    headers: dict
) -> tuple[set[str], dict[str, dict]]:
    """Resolve which mods should be enabled on server.
    Uses BFS to find all server-side mods and their transitive dependencies.
    Returns tuple of (enabled_project_ids, project_data_dict).
    Client-only mods (server_side == "unsupported") are not included in enabled set.
    Raises exception on API failure.
    """
    if not mod_list:
        return set(), {}
    
    project_ids = [p for p, v in mod_list]
    version_ids = [v for p, v in mod_list]
    
    # Step 1: Bulk fetch project data and dependencies (2 API calls)
    log.debug("Fetching project data for %d projects", len(project_ids))
    project_data = get_bulk_project_data(project_ids, headers)
    
    log.debug("Fetching dependencies for %d versions", len(version_ids))
    dep_data = get_version_dependencies(version_ids, headers)
    
    # Step 2: Build lookup maps
    project_to_version = {p: v for p, v in mod_list}
    
    # Map version_id -> project_id for version-based deps
    dep_version_to_project = {
        v: dep_data[v]["project_id"]
        for v in version_ids
        if v in dep_data
    }
    
    # Build set of all known project_ids from the manifest for quick lookup
    known_projects = set(project_ids)
    
    def is_server_side(project_id: str) -> bool:
        # unsupported = client-only, everything else is kept
        return project_data.get(project_id, {}).get("server_side", "unknown") != "unsupported"
    
    # Step 3: Root set — projects that pass the environment filter
    server_projects = {p for p in project_ids if is_server_side(p)}
    log.info("Found %d server-side mods out of %d total", len(server_projects), len(project_ids))
    
    # Step 4: BFS — walk deps transitively
    # Handles both version-based deps and project-based deps
    required_projects: set[str] = set()
    queue = deque(server_projects)
    
    while queue:
        project_id = queue.popleft()
        if project_id in required_projects:
            continue
        required_projects.add(project_id)
        
        version_id = project_to_version.get(project_id)
        if not version_id or version_id not in dep_data:
            continue
        
        ver_dep_data = dep_data[version_id]
        
        # Handle version-specific dependencies (resolved via version_id -> project_id)
        for dep_ver in ver_dep_data["version_deps"]:
            dep_project = dep_version_to_project.get(dep_ver)
            if dep_project and dep_project not in required_projects:
                queue.append(dep_project)
        
        # Handle project-level dependencies (resolved directly via project_id)
        for dep_proj in ver_dep_data["project_deps"]:
            if dep_proj in known_projects and dep_proj not in required_projects:
                queue.append(dep_proj)
    
    log.info("Resolved %d mods to install (including %d dependencies)", 
             len(required_projects), len(required_projects) - len(server_projects))
    return required_projects, project_data


def download_manifest_files(manifest: dict, server_dir: str, headers: dict) -> tuple[bool, str, list[dict]]:
    """Download all files from manifest files[] array.
    Uses bulk API calls (2 total) for dependency resolution.
    Returns (success, error_message, modules_metadata)."""
    from utils import uuid7
    from config import DEFAULT_MODULE_ICON_URL
    
    files = manifest.get('files', [])
    if not files:
        return True, "No files to download", []
    
    # Step 1: Extract mod info from all files
    # First pass: extract raw identifiers
    raw_entries = []  # [(project_str, version_str, file_entry, needs_resolution), ...]
    for file_entry in files:
        path = file_entry.get('path', '')
        downloads = file_entry.get('downloads', [])
        
        if not downloads:
            continue
        
        # Only process mod-type files
        module_type = get_module_type_from_path(path)
        if module_type is None:
            continue
        
        # Extract raw strings from URL
        try:
            parts = downloads[0].split('/')
            if len(parts) >= 4:
                project_str = parts[-4] if parts[-4] != 'data' else parts[-3]
                version_str = parts[-2]
                needs_resolution = not (is_valid_modrinth_id(project_str) and is_valid_modrinth_id(version_str))
                raw_entries.append((project_str, version_str, file_entry, needs_resolution))
        except Exception:
            continue
    
    # Step 2: Resolve any string identifiers to actual IDs
    mod_entries = []  # [(project_id, version_id, file_entry), ...]
    for project_str, version_str, file_entry, needs_resolution in raw_entries:
        if needs_resolution:
            project_id, version_id = resolve_version_id_from_strings(project_str, version_str, headers)
            if project_id and version_id:
                mod_entries.append((project_id, version_id, file_entry))
            else:
                log.error("Failed to resolve identifiers for %s: project='%s', version='%s'", 
                          file_entry.get('path'), project_str, version_str)
                return False, f"Failed to resolve identifiers for {file_entry.get('path')}", []
        else:
            mod_entries.append((project_str, version_str, file_entry))
    
    if not mod_entries:
        return True, "No mod files to download", []
    
    # Step 2: Resolve which mods should be enabled and get project data (2 bulk API calls)
    try:
        mod_list = [(p, v) for p, v, _ in mod_entries]
        enabled_projects, project_data_cache = resolve_server_mods(mod_list, headers)
    except Exception as e:
        log.error("Failed to resolve mod dependencies: %s", e)
        raise Exception(f"Mod dependency resolution failed: {str(e)}")
    
    # Step 3: Download only enabled mods
    modules_metadata = []
    for project_id, version_id, file_entry in mod_entries:
        path = file_entry.get('path')
        hashes = file_entry.get('hashes', {})
        downloads = file_entry.get('downloads', [])
        file_name = os.path.basename(path)
        
        # Check if this mod should be enabled
        is_enabled = project_id in enabled_projects
        
        # Generate UUID for this file
        module_id = uuid7()
        
        # Determine module type from path
        module_type = get_module_type_from_path(path)
        folder = MODULE_TYPE_FOLDERS.get(module_type, 'modules')
        dest_path = os.path.join(server_dir, folder, f"{module_id}.jar")
        
        # Create parent directories
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        
        # Get expected hash
        expected_hash = hashes.get('sha1')
        
        # Download the file
        url = downloads[0]
        log.info("Downloading %s from %s (type: %s, enabled: %s)", path, url, module_type, is_enabled)
        
        if not download_file(url, dest_path, expected_hash=expected_hash, headers=headers):
            return False, f"Failed to download {path}", []
        
        # If disabled, rename to .disabled
        if not is_enabled and os.path.exists(dest_path):
            disabled_path = dest_path + ".disabled"
            try:
                os.rename(dest_path, disabled_path)
                log.info("Disabled client-only mod %s, renamed to %s", file_name, disabled_path)
                dest_path = disabled_path
            except Exception as e:
                log.error("Failed to disable mod %s: %s", file_name, e)
        
        # Use cached project data from bulk API call (no individual API calls needed)
        cached_data = project_data_cache.get(project_id, {})
        module_name = cached_data.get("name") or file_name
        icon_url = cached_data.get("icon_url") or DEFAULT_MODULE_ICON_URL
        
        modules_metadata.append({
            "module_id": module_id,
            "module_type": module_type,
            "module_name": module_name,
            "module_enabled": is_enabled,
            "module_metadata": {
                "icon_url": icon_url,
                "project_id": project_id,
                "version_id": version_id,
                "file_name": file_name
            }
        })
    
    return True, f"Downloaded {len(modules_metadata)} files", modules_metadata


def apply_overrides(mrpack_path: str, server_dir: str) -> tuple[bool, str]:
    """Extract overrides folder from mrpack to server directory.
    Returns (success, error_message)."""
    try:
        with zipfile.ZipFile(mrpack_path, 'r') as zf:
            overrides_prefix = 'overrides/'
            overrides_files = [name for name in zf.namelist() if name.startswith(overrides_prefix)]
            
            if not overrides_files:
                log.info("No overrides folder found in mrpack")
                return True, "No overrides to apply"
            
            for override_path in overrides_files:
                # Skip directories
                if override_path.endswith('/'):
                    continue
                
                # Remove 'overrides/' prefix to get relative path
                relative_path = override_path[len(overrides_prefix):]
                dest_path = os.path.join(server_dir, relative_path)
                
                # Create parent directories
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                
                # Extract file
                with zf.open(override_path) as src, open(dest_path, 'wb') as dst:
                    shutil.copyfileobj(src, dst)
                
                log.debug("Applied override: %s", relative_path)
            
            log.info("Applied %d override files", len(overrides_files))
            return True, f"Applied {len(overrides_files)} override files"
    
    except Exception as e:
        log.error("Failed to apply overrides: %s", e)
        return False, f"Failed to apply overrides: {str(e)}"


def cleanup_server_directory(server_id: str):
    """Remove server directory on failure."""
    server_dir = os.path.join(SERVERS_BASE_DIR, server_id)
    if os.path.exists(server_dir):
        log.info("Cleaning up server directory: %s", server_dir)
        shutil.rmtree(server_dir)
