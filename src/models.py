import json
import os
import socket
from dataclasses import dataclass, asdict, field
from typing import Dict, Any
from config import SERVER_REGISTRY_FILE

@dataclass
class ServerMetadata:
    id: str
    name: str
    port: int
    path: str
    entrypoint: str
    java_version: str
    status: str = "offline"
    properties: Dict[str, Any] = field(default_factory=dict)
    modules: list[dict[str, Any]] = field(default_factory=list)
    modpack: bool = False
    modpack_source: str = None
    server_thumbnail: str = None

class ServerRegistry:
    """
    Persists and manages metadata for all server instances.
    """

    def __init__(self):
        self.servers: Dict[str, ServerMetadata] = {}
        self.load()

    def load(self):
        """Load the registry from disk."""
        if not os.path.exists(SERVER_REGISTRY_FILE):
            self.servers = {}
            return

        try:
            with open(SERVER_REGISTRY_FILE, "r") as f:
                data = json.load(f)
                self.servers = {
                    sid: ServerMetadata(**sdata) 
                    for sid, sdata in data.items()
                }
        except Exception:
            self.servers = {}

    def save(self):
        """Save the registry to disk."""
        data = {sid: asdict(meta) for sid, meta in self.servers.items()}
        with open(SERVER_REGISTRY_FILE, "w") as f:
            json.dump(data, f, indent=4)

    def add_server(self, meta: ServerMetadata):
        """Add or update a server in the registry."""
        self.servers[meta.id] = meta
        self.save()

    def get_server(self, server_id: str) -> ServerMetadata | None:
        """Retrieve server metadata by ID."""
        return self.servers.get(server_id)

    def remove_server(self, server_id: str):
        """Remove a server from the registry."""
        if server_id in self.servers:
            del self.servers[server_id]
            self.save()

    def list_servers(self) -> list[ServerMetadata]:
        """Return a list of all managed servers."""
        return list(self.servers.values())

    def _is_port_locally_free(self, port: int) -> bool:
        """Check if a port is actually free on the machine."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(('localhost', port)) != 0

    def get_next_available_port(self, start_port: int = 25565) -> int:
        """Find the next available port by checking both registry and system."""
        used_ports = {s.port for s in self.servers.values()}
        port = start_port
        while port in used_ports or not self._is_port_locally_free(port):
            port += 1
        return port
