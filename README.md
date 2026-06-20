# Fluxite Agent

The device-side agent for [Fluxite](https://fluxite.io) — a Minecraft server manager that lets users host servers on their own hardware and expose them to players through managed, subdomain-routed tunnels.

The agent runs on a user's machine, registers with the Fluxite control plane, manages local Minecraft server processes and modpack installs, and maintains the WireGuard tunnel that makes those servers reachable through Fluxite's relay.

> This README is for contributors and developers. If you're an end user looking to run Fluxite, download the installer from [fluxite.io](https://fluxite.io) — you don't need this repository.

## Status & platform support

| Platform | Status |
| --- | --- |
| Windows | Supported |
| Linux | Planned |
| Docker | Planned |
| macOS | Not planned |

**Minimum Windows version:** Windows 10 (build 1507) or later — this is the floor required by the current WireGuard for Windows client, which the agent depends on for tunnelling.

## How it works

The agent is a single codebase that runs in two modes:

- **Setup mode** (`--setup`) — a one-time, elevated first run invoked by the installer. It performs all privileged operations: registering the agent with the control plane via a linking code, generating the WireGuard keypair, downloading JDK runtimes, creating firewall rules for the Java executables, and installing the WireGuard tunnel service.
- **Service mode** (default) — the normal runtime, which runs as a Windows service under the Network Service account. It listens for commands from the control plane, manages Minecraft server processes, and handles modpack installation. It deliberately runs unprivileged; all privileged work is confined to setup mode.

This split lets the agent do its dangerous, one-time work under elevation during install, then drop to a low-privilege account for everything it does day to day.

## Architecture

Source lives in `src/`. The main modules:

| Module | Responsibility |
| --- | --- |
| `main.py` | Entry point; mode selection (`--setup` vs service), top-level lifecycle |
| `installer.py` | Setup-mode logic: registration, JDK download, firewall rules, tunnel service install |
| `auth.py` | Agent authentication and JWT renewal against the control plane |
| `poller.py` | Receives commands from the control plane (SSE / polling) |
| `commands.py` | Dispatches and executes received commands |
| `server_manager.py` | Minecraft server process lifecycle (start/stop/monitor) |
| `modpack_installer.py` | Modpack and mod installation |
| `config.py` | Configuration loading and persistence |
| `models.py` | Shared data models |
| `agent_log_manager.py` | Log capture and management |
| `utils.py` | Shared helpers |

Runtime state files (generated, not committed): `agent_id.txt`, `agent.key`, `servers.json`, `wgfluxite.conf`.

`reset_agent.py` is a development helper for clearing local agent state. `setup.iss` is the Inno Setup installer script; `.spec` is the PyInstaller build spec.

## Running from source

The agent runs directly from a virtual environment without being compiled — useful for development.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r src/requirements.txt
```

Run in service mode:

```bash
python src/main.py
```

Run setup mode (requires elevation, and a linking code from the control plane):

```bash
python src/main.py --setup --linking-code <code> --agent-name <name>
```

> Setup mode performs privileged operations (driver install, firewall rules). Only run it in an environment you're willing to have modified — a disposable VM is recommended for testing. See `CONTRIBUTING.md` for the recommended test setup.

## Building

Compiled releases are produced by the GitHub Actions workflow in `.github/workflows/build-exe.yml`, which runs PyInstaller against the `.spec` file and packages the result with Inno Setup. Refer to that workflow for the authoritative build steps.

## Requirements

- Python 3.11
- WireGuard for Windows (installed by the agent's installer in production; install manually for from-source development)
- Dependencies pinned in `src/requirements.txt`

## Contributing

See [`CONTRIBUTING.md`](./CONTRIBUTING.md). Security issues should follow the process in [`SECURITY.md`](./SECURITY.md) rather than being filed as public issues.

## License

Licensed under the Apache License 2.0. See [`LICENSE`](./LICENSE).
