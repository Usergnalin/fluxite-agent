"""
Agent log management — rotation, tailing, streaming, and on-demand retrieval.

Mirrors the MC server log handling in server_manager.py but operates on the
agent's own ``logs/latest.log`` file.
"""

import os
import time
import threading
import logging
import requests
from datetime import datetime

from config import (
    AGENT_LOGS_URL,
    AGENT_LOGS_DIR,
    AGENT_LOG_MAX_AGE_DAYS,
    LOG_STREAM_MAX_DURATION,
    LOG_STREAM_BATCH_INTERVAL,
    LOG_STREAM_MAX_CHARS,
    REQUEST_TIMEOUT,
)
from auth import AgentAuth

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Log rotation (called once at startup, before the file handler is attached)
# ---------------------------------------------------------------------------

def rotate_agent_logs() -> str:
    """Rotate ``latest.log`` and clean up old log files.

    Returns the absolute path to the (now empty) ``latest.log`` ready for the
    new file handler.
    """
    os.makedirs(AGENT_LOGS_DIR, exist_ok=True)
    latest = os.path.join(AGENT_LOGS_DIR, "latest.log")

    if os.path.exists(latest) and os.path.getsize(latest) > 0:
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        rotated = os.path.join(AGENT_LOGS_DIR, f"{ts}.log")
        os.rename(latest, rotated)

    # Purge files older than the configured max age
    cutoff = time.time() - (AGENT_LOG_MAX_AGE_DAYS * 86400)
    for fname in os.listdir(AGENT_LOGS_DIR):
        if fname == "latest.log":
            continue
        fpath = os.path.join(AGENT_LOGS_DIR, fname)
        if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
            os.remove(fpath)
            log.debug("Deleted old agent log: %s", fname)

    return latest


# ---------------------------------------------------------------------------
# Logging filter — exclude mc.* loggers from the agent log file
# ---------------------------------------------------------------------------

class _ExcludeMCFilter(logging.Filter):
    """Reject log records originating from ``mc.*`` loggers."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith("mc.")


def create_agent_file_handler(log_path: str) -> logging.FileHandler:
    """Create a file handler that writes agent-only logs to *log_path*."""
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    handler.addFilter(_ExcludeMCFilter())
    return handler


# ---------------------------------------------------------------------------
# AgentLogManager — tailing, streaming, and on-demand log retrieval
# ---------------------------------------------------------------------------

class AgentLogManager:
    """Manages reading and streaming the agent's own log file.

    Modelled after the log-handling parts of ``MCServerManager``.
    """

    def __init__(self, auth: AgentAuth):
        self.auth = auth
        self._log_path = os.path.join(AGENT_LOGS_DIR, "latest.log")

        # Line tracking (resets on rotation, i.e. agent restart)
        self._log_line_counter: int = 0
        self._log_lines: list[str] = []

        # Streaming buffer
        self._log_buffer: list[str] = []
        self._log_buffer_start_line: int = 0

        self._log_lock = threading.Lock()
        self._stop_event = threading.Event()

        # Request-based session tracking (request_id → start_time)
        self._active_log_requests: dict[str, float] = {}
        self._log_stream_thread: threading.Thread | None = None

        # Kick off the tailer
        self._tailer_thread = threading.Thread(
            target=self._tailer_loop,
            daemon=True,
            name="agent-log-tailer",
        )
        self._tailer_thread.start()

    # ----- tailing ---------------------------------------------------------

    def _tailer_loop(self):
        """Tail ``logs/latest.log`` from the beginning, tracking lines."""
        log.debug("Agent log tailer started")

        while not self._stop_event.is_set():
            if not os.path.exists(self._log_path):
                self._stop_event.wait(1.0)
                continue

            partial = ""
            try:
                with open(self._log_path, "r", encoding="utf-8", errors="replace") as f:
                    # Start from the beginning so we capture everything
                    last_pos = f.tell()

                    while not self._stop_event.is_set():
                        chunk = f.read(8192)
                        if chunk:
                            partial += chunk
                            while "\n" in partial:
                                line, partial = partial.split("\n", 1)
                                cleaned = line.strip()
                                if cleaned:
                                    with self._log_lock:
                                        self._log_line_counter += 1
                                        self._log_lines.append(cleaned)
                                        if self._active_log_requests:
                                            if not self._log_buffer:
                                                self._log_buffer_start_line = self._log_line_counter
                                            self._log_buffer.append(cleaned)
                            last_pos = f.tell()
                        else:
                            # Check for truncation / rotation
                            try:
                                current_size = os.path.getsize(self._log_path)
                            except OSError:
                                break

                            if current_size < last_pos:
                                # File was truncated or rotated — flush remaining buffer first, then reset
                                with self._log_lock:
                                    # Flush any pending logs to API before resetting
                                    has_active_streams = bool(self._active_log_requests)
                                    if has_active_streams and self._log_buffer:
                                        logs_to_send = "\n".join(self._log_buffer) + "\nlog_rotation"
                                        batch_start_line = self._log_buffer_start_line
                                        self._log_buffer.clear()
                                    elif has_active_streams:
                                        # No pending logs but have active streams — send rotation marker only
                                        logs_to_send = "log_rotation"
                                        batch_start_line = self._log_line_counter + 1  # Next line number
                                    else:
                                        logs_to_send = None
                                    
                                    log.debug(
                                        "Agent log file truncated/rotated, resetting counter (was %d)",
                                        self._log_line_counter,
                                    )
                                    self._log_line_counter = 0
                                    self._log_lines.clear()
                                    self._log_buffer_start_line = 0
                                
                                # Send the flushed logs outside the lock
                                if logs_to_send:
                                    try:
                                        resp = requests.post(
                                            AGENT_LOGS_URL.format(self.auth.agent_id),
                                            json={"logs": logs_to_send, "logs_start_line": batch_start_line},
                                            headers=self.auth.auth_header(),
                                            timeout=REQUEST_TIMEOUT
                                        )
                                        resp.raise_for_status()
                                        log.debug("Flushed logs with rotation marker for agent")
                                    except Exception as e:
                                        log.error("Failed to flush agent logs before rotation: %s", e)
                                
                                partial = ""
                                break

                            self._stop_event.wait(0.1)
            except OSError as e:
                log.debug("Error reading agent log file: %s", e)
                self._stop_event.wait(1.0)

        log.debug("Agent log tailer stopped")

    # ----- streaming -------------------------------------------------------

    def start_log_stream(self, request_id: str, logs_history_lines: int = 0) -> bool:
        """Register a streaming request; start the streamer if not running.
        
        Args:
            request_id: Unique identifier for this streaming request
            logs_history_lines: Number of historical lines to send immediately before streaming (0 = none)
        """
        # Send historical logs first if requested
        if logs_history_lines > 0:
            self._send_history_logs(request_id, logs_history_lines)
        
        with self._log_lock:
            self._active_log_requests[request_id] = time.time()
            if self._log_stream_thread and self._log_stream_thread.is_alive():
                return True

        log.info("Starting agent log stream (request %s)", request_id)
        self._log_stream_thread = threading.Thread(
            target=self._streamer_loop,
            daemon=True,
            name="agent-log-stream",
        )
        self._log_stream_thread.start()
        return True

    def _send_history_logs(self, request_id: str, lines_count: int) -> bool:
        """Send the last N lines of historical logs immediately via POST."""
        with self._log_lock:
            total_lines = self._log_line_counter
            if total_lines == 0:
                log.debug("No historical logs available for request %s", request_id)
                return True
            
            # Calculate range: go back lines_count from the end, or start from 1 if not enough lines
            end_line = total_lines
            start_line = max(1, end_line - lines_count + 1)
            actual_count = end_line - start_line + 1
            
            # Get the lines (0-indexed in list, so subtract 1)
            lines_to_send = self._log_lines[start_line - 1:end_line]
        
        if not lines_to_send:
            log.debug("No historical logs to send for request %s", request_id)
            return True
        
        url = AGENT_LOGS_URL.format(self.auth.agent_id)
        headers = self.auth.auth_header()
        payload = {
            "logs": "\n".join(lines_to_send),
            "logs_start_line": start_line,
            "request_id": request_id,
        }
        
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            log.info("Sent %d historical agent log lines (%d-%d) for request %s", 
                     actual_count, start_line, end_line, request_id)
            return True
        except Exception as e:
            log.error("Failed to send historical agent logs for request %s: %s", request_id, e)
            return False

    def stop_log_stream(self, request_id: str) -> bool:
        """Remove a streaming request."""
        with self._log_lock:
            if request_id in self._active_log_requests:
                del self._active_log_requests[request_id]
                log.info("Stopping agent log stream request %s", request_id)
            else:
                log.warning("Received stop_agent_log_stream for unknown request %s", request_id)
        return True

    def _streamer_loop(self):
        """Background loop that batches and sends agent logs to the API."""
        last_sent_time = time.time()
        url = AGENT_LOGS_URL.format(self.auth.agent_id)
        headers = self.auth.auth_header()

        while True:
            current_time = time.time()

            with self._log_lock:
                expired = [
                    rid for rid, st in self._active_log_requests.items()
                    if current_time - st > LOG_STREAM_MAX_DURATION
                ]
                for rid in expired:
                    log.info("Agent log stream request %s reached max duration, stopping.", rid)
                    del self._active_log_requests[rid]

                if not self._active_log_requests:
                    break

            time_elapsed = current_time - last_sent_time

            with self._log_lock:
                chars_count = sum(len(line) for line in self._log_buffer)

            if time_elapsed >= LOG_STREAM_BATCH_INTERVAL or chars_count >= LOG_STREAM_MAX_CHARS:
                with self._log_lock:
                    logs_to_send = "\n".join(self._log_buffer)
                    batch_start_line = self._log_buffer_start_line
                    self._log_buffer.clear()

                if logs_to_send:
                    try:
                        resp = requests.post(
                            url,
                            json={"logs": logs_to_send, "logs_start_line": batch_start_line},
                            headers=headers,
                            timeout=REQUEST_TIMEOUT,
                        )
                        resp.raise_for_status()
                    except Exception as e:
                        log.error("Failed to send agent logs to API, stopping all streams: %s", e)
                        with self._log_lock:
                            self._active_log_requests.clear()
                        break

                last_sent_time = time.time()

            time.sleep(0.5)

    # ----- on-demand retrieval ---------------------------------------------

    def send_logs_range(self, start_line: int, end_line: int) -> bool:
        """Send a range of agent log lines (1-based, inclusive) to the API.

        Returns False if the range is out of bounds.
        """
        with self._log_lock:
            total = self._log_line_counter
            if start_line < 1 or end_line < start_line or end_line > total:
                log.warning(
                    "send_agent_logs: range [%d, %d] out of bounds (total lines: %d)",
                    start_line, end_line, total,
                )
                return False
            lines_to_send = self._log_lines[start_line - 1:end_line]

        url = AGENT_LOGS_URL.format(self.auth.agent_id)
        headers = self.auth.auth_header()
        payload = {
            "logs": "\n".join(lines_to_send),
            "logs_start_line": start_line,
        }
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            log.info("Sent agent log lines %d-%d", start_line, end_line)
            return True
        except Exception as e:
            log.error("Failed to send agent log range: %s", e)
            return False

    # ----- lifecycle -------------------------------------------------------

    def shutdown(self):
        """Signal the tailer to stop."""
        self._stop_event.set()
