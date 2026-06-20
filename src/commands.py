"""
Command types, parsing, and thread-safe command queue.
"""

from __future__ import annotations

import queue
import logging
from enum import Enum
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


class CommandType(Enum):
    """Known command types the agent can handle."""
    RESTART_SERVER    = "restart_server"
    START_SERVER      = "start_server"
    STOP_SERVER       = "stop_server"
    KILL_SERVER       = "kill_server"
    CREATE_SERVER     = "create_server"
    MC_COMMAND        = "mc_command"
    INSTALL_MODULES      = "install_modules"
    DELETE_MODULE       = "delete_module"
    ENABLE_MODULE       = "enable_module"
    DISABLE_MODULE      = "disable_module"
    CREATE_MODPACK    = "create_modpack"
    START_SERVER_LOG_STREAM  = "start_server_log_stream"
    STOP_SERVER_LOG_STREAM   = "stop_server_log_stream"
    START_AGENT_LOG_STREAM  = "start_agent_log_stream"
    STOP_AGENT_LOG_STREAM   = "stop_agent_log_stream"
    SEND_SERVER_LOGS  = "send_server_logs"
    SEND_AGENT_LOGS  = "send_agent_logs"
    DELETE_SERVER     = "delete_server"
    UNKNOWN           = "unknown"


@dataclass
class Command:
    """A single parsed command destined for execution."""
    id: str
    server_id: str | None
    type: CommandType
    payload: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


def _parse_type(raw_type: str) -> CommandType:
    """Map an API type string to a CommandType enum member."""
    try:
        return CommandType(raw_type)
    except ValueError:
        return CommandType.UNKNOWN


def parse_command(obj: dict) -> Command:
    """
    Parse a single command object from the API response.

    Expected shape:
        {"command_id": "...", "command": {"type": "restart_server", ...}}

    The ``type`` field is optional inside the command dict — if missing,
    the command is classified as UNKNOWN.
    """
    cmd_body = obj.get("command", {})
    return Command(
        id=str(obj.get("command_id", "")),
        server_id=obj.get("server_id") or cmd_body.get("server_id"),
        type=_parse_type(cmd_body.get("type", "unknown")),
        payload=cmd_body,
        raw=obj,
    )


class CommandQueue:
    """
    Thread-safe queue of ``Command`` objects.

    The poller pushes commands in; the main loop (or executor) pulls them out.
    """

    def __init__(self):
        self._q: queue.Queue[Command] = queue.Queue()

    def put(self, cmd: Command) -> None:
        self._q.put(cmd)

    def get(self, block: bool = True, timeout: float | None = None) -> Command:
        """
        Get the next command.  Raises ``queue.Empty`` if non-blocking
        and nothing is available (or timeout expires).
        """
        return self._q.get(block=block, timeout=timeout)

    @property
    def pending(self) -> int:
        return self._q.qsize()

    def enqueue_from_api(self, raw_list: list[dict]) -> list[Command]:
        """
        Parse a list of raw API command objects and enqueue them.

        Returns the list of parsed ``Command`` instances.
        """
        commands = []
        for obj in raw_list:
            cmd = parse_command(obj)
            self._q.put(cmd)
            commands.append(cmd)
            log.debug("Enqueued command %s (%s)", cmd.id, cmd.type.value)
        return commands
