"""
Minecraft server process management with robust status reporting (Watchdog).
"""

import subprocess
import threading
import logging
import time
import os
import requests
import socket
from pathlib import Path
from config import SERVER_STATUS_URL_TPL, REQUEST_TIMEOUT, \
    SERVER_LOGS_URL_TPL, LOG_STREAM_MAX_DURATION, LOG_STREAM_BATCH_INTERVAL, LOG_STREAM_MAX_CHARS, \
    JVM_PATH, get_jvm_path_for_version
from models import ServerMetadata
from auth import AgentAuth

log = logging.getLogger(__name__)

class MCServerManager:
    """
    Manages a single Minecraft server instance and reports status via Watchdog.
    """

    def __init__(self, meta: ServerMetadata, auth: AgentAuth):
        self.meta = meta
        self.auth = auth
        self.process: subprocess.Popen | None = None
        self.log_thread: threading.Thread | None = None
        
        self._wd_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._is_stopping = False
        self._last_reported_status = None
        
        self._log_buffer: list[str] = []
        self._log_buffer_start_line: int = 0
        self._log_line_counter: int = 0   # 1-based counter, resets on rotation
        self._log_lines: list[str] = []   # all lines since last rotation (index = line_number - 1)
        self._log_lock = threading.Lock()
        self._active_log_requests: dict[str, float] = {}
        self._log_stream_thread: threading.Thread | None = None

        # Start the watchdog immediately to ensure "offline" is known if not running
        self._start_watchdog()
        # Start the always-on log file tailer
        self._start_log_tailer()

    def is_running(self) -> bool:
        """Check if the server process is currently running."""
        return self.process is not None and self.process.poll() is None

    def _is_port_open(self) -> bool:
        """Check if the Minecraft server port is accepting TCP connections."""
        try:
            with socket.create_connection(("127.0.0.1", self.meta.port), timeout=1.0):
                return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            return False

    def _is_old_version(self, version_str: str) -> bool:
        """Check if Minecraft version is older than 1.7.2 (uses server.log instead of logs/latest.log)."""
        try:
            # Parse version string like "1.6.4" or "1.7.2"
            parts = version_str.split('.')
            if len(parts) < 2:
                return False
            
            major = int(parts[0]) if parts[0].isdigit() else 0
            minor = int(parts[1]) if parts[1].isdigit() else 0
            patch = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            
            # Versions older than 1.7.2 use server.log
            if major < 1:
                return True
            if major == 1 and minor < 7:
                return True
            if major == 1 and minor == 7 and patch < 2:
                return True
            return False
        except (ValueError, IndexError):
            return False

    def _report_status(self, status: str):
        """Report server status to the cloud panel API on transition."""
        if status == self._last_reported_status:
            return

        self.meta.status = status
        url = SERVER_STATUS_URL_TPL.format(self.meta.id)
        payload = {"server_status": status}
        
        try:
            headers = self.auth.auth_header()
            resp = requests.put(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            self._last_reported_status = status
            log.info("Status transition: Server %s is now '%s'", self.meta.id, status)
        except Exception as e:
            # Still record the status as "reported" (or at least "tried") to avoid spamming
            # unless the user explicitly wants retries on every watchdog cycle.
            # Given the request to "do not send updates if no change", we'll treat
            # even a failed attempt as the "last reported" to stop the spam.
            self._last_reported_status = status
            log.error("Failed to report status for server %s (target: %s): %s", self.meta.id, status, e)

    def _start_watchdog(self):
        """Start the background status watchdog."""
        if self._wd_thread and self._wd_thread.is_alive():
            return
        self._stop_event.clear()
        self._wd_thread = threading.Thread(target=self._watchdog_loop, daemon=True, name=f"wd-{self.meta.id}")
        self._wd_thread.start()

    def _watchdog_loop(self):
        """Background loop that checks server health every few seconds."""
        log.debug("Watchdog started for %s", self.meta.id)
        while not self._stop_event.is_set():
            try:
                alive = self.is_running()
                
                if not alive:
                    self._is_stopping = False
                    self._report_status("offline")
                elif self._is_stopping:
                    self._report_status("stopping")
                else:
                    # Process is up, check the port
                    if self._is_port_open():
                        self._report_status("online")
                    else:
                        self._report_status("starting")
                
            except Exception as e:
                log.error("Error in watchdog for %s: %s", self.meta.id, e)
            
            # Sleep for a bit before checking again
            time.sleep(1)
        log.debug("Watchdog stopped for %s", self.meta.id)

    def start(self) -> bool:
        """Start the Minecraft server with correct Java version."""
        if self.is_running():
            log.warning("Server %s is already running", self.meta.id)
            return False

        self._is_stopping = False
        log.info("Starting Minecraft server %s (%s)...", self.meta.name, self.meta.id)
        
        try:
            
            entrypoint_path = os.path.abspath(os.path.join(self.meta.path, self.meta.entrypoint))
            jvm_path = get_jvm_path_for_version(self.meta.java_version)
            
            # Check if JVM exists
            if not os.path.exists(jvm_path):
                log.error("JVM not found at %s", jvm_path)
                return False
            
            log.info("Using JVM: %s", jvm_path)

            server_env = os.environ.copy()
            server_env["_JAVA_OPTIONS"] = "-Xmx4G -Xms4G -XX:+UseG1GC -Djava.awt.headless=true"
            server_env["PATH"] = str(Path(jvm_path).parent) + os.pathsep + server_env.get("PATH", "")

            if entrypoint_path.lower().endswith(('.bat', '.ps1', '.sh')):
                log.info(f"Starting server with script: {entrypoint_path}")
                self.process = subprocess.Popen(
                    [entrypoint_path],
                    cwd=self.meta.path,
                    env=server_env,
                    stdin=subprocess.PIPE,
                    stdout=None,
                    stderr=None,
                    text=True,
                    bufsize=1
                )
            elif entrypoint_path.lower().endswith(('.jar')):
                log.info(f"Starting server with jar: {entrypoint_path} using JVM: {jvm_path}")
                self.process = subprocess.Popen(
                    [jvm_path, '-jar', entrypoint_path, 'nogui'],
                    cwd=self.meta.path,
                    env=server_env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1
                )
            else:
                log.error("Unknown entrypoint type: %s", entrypoint_path)
                return False
            
            return True
        except Exception as e:
            log.error("Failed to start server %s: %s", self.meta.name, e)
            self.process = None
            return False

    def stop(self, timeout: int = 30) -> bool:
        """Stop the Minecraft server gracefully."""
        if not self.is_running():
            return True

        self._is_stopping = True
        log.info("Stopping server %s gracefully...", self.meta.name)
        
        try:
            self.send_command("stop")
            
            # Wait for exit
            start_time = time.time()
            while time.time() - start_time < timeout:
                if not self.is_running():
                    log.info("Server %s exited gracefully", self.meta.name)
                    self.process = None
                    self._is_stopping = False
                    return True
                time.sleep(0.5)
            
            log.warning("Server %s timed out. Force killing...", self.meta.name)
            self.process.kill()
            self.process.wait()
            self.process = None
            self._is_stopping = False
            return True
        except Exception as e:
            log.error("Error during server %s shutdown: %s", self.meta.name, e)
            if self.process:
                self.process.kill()
            self.process = None
            self._is_stopping = False
            return False

    def kill(self) -> bool:
        """Kill the Minecraft server immediately without graceful shutdown."""
        if not self.is_running():
            return True

        log.warning("Killing server %s immediately...", self.meta.name)
        
        try:
            self.process.kill()
            self.process.wait()
            self.process = None
            self._is_stopping = False
            log.info("Server %s killed successfully", self.meta.name)
            return True
        except Exception as e:
            log.error("Error killing server %s: %s", self.meta.name, e)
            return False

    def restart(self) -> bool:
        """Restart the Minecraft server."""
        log.info("Restarting server %s...", self.meta.name)
        self.stop()
        return self.start()

    def send_command(self, command: str) -> bool:
        """Send a command to the Minecraft server console."""
        if not self.is_running():
            log.error("Cannot send command: Server %s is not running", self.meta.name)
            return False

        try:
            log.debug("[%s] Sending command: %s", self.meta.name, command)
            self.process.stdin.write(command + "\n")
            self.process.stdin.flush()
            return True
        except Exception as e:
            log.error("Failed to send command to server %s: %s", self.meta.name, e)
            return False

    def _start_log_tailer(self):
        """Start the always-on log file tailer thread."""
        if self.log_thread and self.log_thread.is_alive():
            return
        self.log_thread = threading.Thread(
            target=self._output_reader,
            daemon=True,
            name=f"log-{self.meta.name}"
        )
        self.log_thread.start()

    def _output_reader(self):
        """Always-on background thread that tails logs/latest.log.

        Survives server stop/start cycles. Detects file truncation and
        rotation so manual edits or MC log rotation are handled correctly.
        Only emits complete lines (terminated by \\n).
        Line numbering is cumulative and survives rotations.
        """
        # Determine log path based on Minecraft version
        # Versions older than 1.7.2 use server.log instead of logs/latest.log
        mc_version = self.meta.properties.get("mc_version", "")
        if mc_version and self._is_old_version(mc_version):
            log_path = os.path.join(self.meta.path, "server.log")
        else:
            log_path = os.path.join(self.meta.path, "logs", "latest.log")
        mc_logger = logging.getLogger(f"mc.{self.meta.name}")
        log.debug("Log tailer started for %s", self.meta.name)

        while not self._stop_event.is_set():
            # Wait for the log file to exist
            if not os.path.exists(log_path):
                self._stop_event.wait(0.1)
                continue

            partial = ""
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    # Start at the end so we only see new content
                    f.seek(0, 2)
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
                                    mc_logger.info(cleaned)
                            last_pos = f.tell()
                        else:
                            # No new data — check for truncation / rotation
                            try:
                                current_size = os.path.getsize(log_path)
                            except OSError:
                                # File deleted; break to outer loop to wait for it again
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
                                    
                                    log.debug("Log file for %s was truncated/rotated, resetting line counter (was %d)", self.meta.name, self._log_line_counter)
                                    self._log_line_counter = 0
                                    self._log_lines.clear()
                                    self._log_buffer_start_line = 0
                                
                                # Send the flushed logs outside the lock
                                if logs_to_send:
                                    try:
                                        resp = requests.post(
                                            SERVER_LOGS_URL_TPL.format(self.meta.id),
                                            json={"logs": logs_to_send, "logs_start_line": batch_start_line},
                                            headers=self.auth.auth_header(),
                                            timeout=REQUEST_TIMEOUT
                                        )
                                        resp.raise_for_status()
                                        log.debug("Flushed logs with rotation marker for %s", self.meta.name)
                                    except Exception as e:
                                        log.error("Failed to flush logs before rotation for %s: %s", self.meta.name, e)
                                
                                partial = ""
                                break

                            self._stop_event.wait(0.1)
            except OSError as e:
                log.debug("Error reading log file for %s: %s", self.meta.name, e)
                self._stop_event.wait(1.0)

        log.debug("Log tailer stopped for %s", self.meta.name)

    def start_log_stream(self, request_id: str, logs_history_lines: int = 0) -> bool:
        """Starts a log stream forwarding log file changes to the API.
        
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
            
        log.info("Starting log stream for server %s (request %s)", self.meta.id, request_id)
        self._log_stream_thread = threading.Thread(
            target=self._log_streamer_loop,
            daemon=True,
            name=f"log-stream-{self.meta.name}"
        )
        self._log_stream_thread.start()
        return True

    def _send_history_logs(self, request_id: str, lines_count: int) -> bool:
        """Send the last N lines of historical logs immediately via POST."""
        with self._log_lock:
            total_lines = self._log_line_counter
            if total_lines == 0:
                log.debug("No historical logs available for server %s request %s", self.meta.id, request_id)
                return True
            
            # Calculate range: go back lines_count from the end, or start from 1 if not enough lines
            end_line = total_lines
            start_line = max(1, end_line - lines_count + 1)
            actual_count = end_line - start_line + 1
            
            # Get the lines (0-indexed in list, so subtract 1)
            lines_to_send = self._log_lines[start_line - 1:end_line]
        
        if not lines_to_send:
            log.debug("No historical logs to send for server %s request %s", self.meta.id, request_id)
            return True
        
        url = SERVER_LOGS_URL_TPL.format(self.meta.id)
        headers = self.auth.auth_header()
        payload = {
            "logs": "\n".join(lines_to_send),
            "logs_start_line": start_line,
            "request_id": request_id,
        }
        
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            log.info("Sent %d historical server log lines (%d-%d) for server %s request %s", 
                     actual_count, start_line, end_line, self.meta.id, request_id)
            return True
        except Exception as e:
            log.error("Failed to send historical server logs for %s request %s: %s", self.meta.id, request_id, e)
            return False

    def stop_log_stream(self, request_id: str) -> bool:
        """Stops the log stream gracefully."""
        with self._log_lock:
            if request_id in self._active_log_requests:
                del self._active_log_requests[request_id]
                log.info("Stopping log stream request %s for server %s", request_id, self.meta.id)
            else:
                log.warning("Received stop_log_stream for unknown request %s on server %s", request_id, self.meta.id)
        return True

    def _log_streamer_loop(self):
        """Background loop for sending batched logs to API."""
        last_sent_time = time.time()
        url = SERVER_LOGS_URL_TPL.format(self.meta.id)
        headers = self.auth.auth_header()

        while True:
            current_time = time.time()
            
            with self._log_lock:
                expired_requests = [rid for rid, st in self._active_log_requests.items() 
                                    if current_time - st > LOG_STREAM_MAX_DURATION]
                for rid in expired_requests:
                    log.info("Log stream request %s for %s reached max duration, stopping.", rid, self.meta.id)
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
                        resp = requests.post(url, json={"logs": logs_to_send, "logs_start_line": batch_start_line}, headers=headers, timeout=REQUEST_TIMEOUT)
                        resp.raise_for_status()
                    except Exception as e:
                        log.error("Failed to send logs to API, stopping all log streams for %s: %s", self.meta.id, e)
                        with self._log_lock:
                            self._active_log_requests.clear()
                        break
                
                last_sent_time = time.time()
            
            time.sleep(0.5)

    def send_logs_range(self, start_line: int, end_line: int) -> bool:
        """Send a range of log lines (1-based, inclusive) to the API.

        Returns False if the requested range is out of bounds.
        Lines are relative to the current rotation — the counter resets on each rotation.
        """
        with self._log_lock:
            total = self._log_line_counter
            if start_line < 1 or end_line < start_line or end_line > total:
                log.warning(
                    "send_logs_range for %s: range [%d, %d] out of bounds (total lines: %d)",
                    self.meta.id, start_line, end_line, total
                )
                return False
            # _log_lines is 0-indexed, line numbers are 1-based
            lines_to_send = self._log_lines[start_line - 1:end_line]

        url = SERVER_LOGS_URL_TPL.format(self.meta.id)
        headers = self.auth.auth_header()
        payload = {
            "logs": "\n".join(lines_to_send),
            "logs_start_line": start_line,
        }
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            log.info("Sent log lines %d-%d for server %s", start_line, end_line, self.meta.id)
            return True
        except Exception as e:
            log.error("Failed to send log range for server %s: %s", self.meta.id, e)
            return False

    def shutdown(self):
        """Cleanly stop the watchdog and the server."""
        self._stop_event.set()
        self.stop()
