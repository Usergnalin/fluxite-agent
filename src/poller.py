"""
Background command poller — periodically fetches commands from the API.
"""

import threading
import logging
import time
import requests
import json

from auth import AgentAuth
from commands import CommandQueue
from config import COMMAND_URL_TPL, COMMAND_STREAM_URL_TPL, REQUEST_TIMEOUT

log = logging.getLogger(__name__)


class CommandPoller:
    """
    Manages command fetching via SSE (Server-Sent Events).
    
    On start (or after disconnect), it performs a full GET poll once,
    then establishes a persistent stream connection.
    """

    def __init__(self, auth: AgentAuth, cmd_queue: CommandQueue):
        self._auth = auth
        self._queue = cmd_queue
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._reconnect_delay = 5.0  # seconds

    # ---- control -----------------------------------------------------------

    def start(self) -> None:
        """Spin up the poller thread."""
        if self._thread and self._thread.is_alive():
            log.warning("Poller already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="cmd-poller")
        self._thread.start()
        log.info("Poller started (SSE mode)")

    def stop(self) -> None:
        """Signal the poller thread to stop."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        log.info("Poller stopped")

    # ---- internal ----------------------------------------------------------

    def _run(self) -> None:
        """Main loop — handles initial poll and streaming reconnection."""
        while not self._stop_event.is_set():
            retry_immediately = False
            try:
                # 1. Full poll to catch any missed commands during downtime
                auth_error = self._full_poll()
                if auth_error:
                    retry_immediately = True
                    continue

                # 2. Start streaming
                if not self._stop_event.is_set():
                    auth_error = self._stream_commands()
                    if auth_error:
                        retry_immediately = True
            except Exception:
                log.exception("Unexpected error in command poller loop")

            if not self._stop_event.is_set() and not retry_immediately:
                log.info("API downtime detection — waiting %.1fs before retry", self._reconnect_delay)
                self._stop_event.wait(timeout=self._reconnect_delay)

    def _full_poll(self) -> bool:
        """
        Fetch all pending commands once via standard GET.
        Returns True if an auth error occurred (requiring immediate retry).
        """
        url = COMMAND_URL_TPL.format(self._auth.agent_id)
        log.info("Performing initial full poll...")

        try:
            headers = self._auth.auth_header()
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            
            if resp.status_code == 401:
                log.warning("Full poll auth failed (401), forcing refresh")
                self._auth.force_refresh()
                return True

            if resp.status_code == 200:
                data = resp.json()
                cmd_list = data if isinstance(data, list) else data.get("commands", [])
                if cmd_list:
                    enqueued = self._queue.enqueue_from_api(cmd_list)
                    log.info("Full poll: enqueued %d command(s)", len(enqueued))
            else:
                log.error("Full poll failed: %d %s", resp.status_code, resp.text[:100])
        except Exception as e:
            log.error("Error during full poll: %s", e)
        
        return False

    def _stream_commands(self) -> bool:
        """
        Establish a persistent SSE connection and process events.
        Returns True if an auth error occurred (requiring immediate retry).
        """
        url = COMMAND_STREAM_URL_TPL.format(self._auth.agent_id)
        log.info("Connecting to command stream...")

        try:
            headers = self._auth.auth_header()
            # SSE should have a long timeout or no timeout for the read
            with requests.get(url, headers=headers, stream=True, timeout=(REQUEST_TIMEOUT, None)) as resp:
                if resp.status_code == 401:
                    log.warning("Stream auth failed (401), forcing refresh")
                    self._auth.force_refresh()
                    return True

                if resp.status_code != 200:
                    log.error("Stream connection failed: %d %s", resp.status_code, resp.text[:100])
                    return False

                log.info("Command stream established")
                
                # Iterate over the lines of the response
                for line in resp.iter_lines():
                    if self._stop_event.is_set():
                        break
                    
                    if not line:
                        continue
                    
                    decoded_line = line.decode('utf-8').strip()
                    
                    if decoded_line.startswith("data: "):
                        content = decoded_line[len("data: "):].strip()
                        try:
                            data_obj = json.loads(content)
                            
                            # Check for session expiry message from the stream
                            if data_obj.get("message") == "Session expired":
                                log.warning("Received 'Session expired' from stream, forcing refresh")
                                self._auth.force_refresh()
                                return True
                            
                            # Otherwise treat as a command if it has the right shape
                            if "command_id" in data_obj or "command" in data_obj:
                                self._queue.enqueue_from_api([data_obj])
                        except json.JSONDecodeError:
                            log.error("Failed to parse SSE data: %s", content)
                    elif decoded_line.startswith(": keep-alive"):
                        log.debug("Stream keep-alive")
                    else:
                        log.debug("Stream meta/other: %s", decoded_line)

        except requests.RequestException as e:
            log.error("Stream connection error: %s", e)
        except Exception as e:
            log.exception("Unexpected error in stream handler: %s", e)
        
        return False
