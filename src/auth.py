"""
Agent authentication — linking, key management, and JWT token refresh.
"""

import os
import time
import uuid
import base64
import requests
from nacl.signing import SigningKey
from nacl.public import PrivateKey

from config import (
    LINK_URL, REFRESH_URL_TPL, KEY_FILE, ID_FILE,
    WIREGUARD_KEY_FILE, WIREGUARD_CONF_PATH,
    TOKEN_REFRESH_INTERVAL, REQUEST_TIMEOUT,
)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def load_signing_key():
    """Load the Ed25519 signing key from disk, or return None."""
    if not os.path.exists(KEY_FILE):
        return None
    with open(KEY_FILE, "r") as f:
        return SigningKey(base64.b64decode(f.read().strip()))


def load_agent_id():
    """Load the persisted agent ID, or return None."""
    if not os.path.exists(ID_FILE):
        return None
    with open(ID_FILE, "r") as f:
        return f.read().strip()


def save_credentials(signing_key: SigningKey, agent_id: str):
    """Persist the private key and agent ID to disk."""
    priv_b64 = base64.b64encode(signing_key.encode()).decode("utf-8")
    with open(KEY_FILE, "w") as f:
        f.write(priv_b64)
    with open(ID_FILE, "w") as f:
        f.write(agent_id)


# ---------------------------------------------------------------------------
# Agent linking
# ---------------------------------------------------------------------------

def link_agent(name: str, code: str) -> tuple[str, dict]:
    """
    Register a new agent with the cloud panel.

    Generates Ed25519 and WireGuard keypairs, sends the public keys +
    linking code, and persists all credentials on success.

    Returns (agent_id, tunnel_config) where tunnel_config is:
        {server_wg_pubkey, assigned_ip, server_wg_ip, tunnel_endpoint}
    Raises RuntimeError on failure.
    """
    signing_key = SigningKey.generate()
    pub_b64 = base64.b64encode(signing_key.verify_key.encode()).decode("utf-8")

    # Generate WireGuard Curve25519 keypair
    wg_private = PrivateKey.generate()
    wg_public = wg_private.public_key
    wg_priv_b64 = base64.b64encode(wg_private.encode()).decode("utf-8")
    wg_pub_b64 = base64.b64encode(wg_public.encode()).decode("utf-8")

    payload = {
        "agent_name": name,
        "linking_code": code,
        "public_key": pub_b64,
        "tunnel_public_key": wg_pub_b64,
    }

    resp = requests.post(LINK_URL, json=payload, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError(f"Linking failed ({resp.status_code}): {resp.text}")

    data = resp.json()
    agent_id = data.get("agent_id")
    if not agent_id:
        raise RuntimeError("Server returned 200 but no agent_id in response")

    # Extract tunnel config from response
    server_wg_pubkey = "EQD0zD1Zb8McBqkVntAU/vQsSXIkOZHNLFNivKryulU="
    assigned_ip = data.get("tunnel_ip")
    server_wg_ip = "172.16.0.1"
    tunnel_endpoint = "209.38.57.183"

    if not all([server_wg_pubkey, assigned_ip, server_wg_ip, tunnel_endpoint]):
        raise RuntimeError("Server response missing WireGuard tunnel configuration")

    tunnel_config = {
        "server_wg_pubkey": server_wg_pubkey,
        "assigned_ip": assigned_ip,
        "server_wg_ip": server_wg_ip,
        "tunnel_endpoint": tunnel_endpoint,
    }

    save_credentials(signing_key, agent_id)

    # Save WireGuard private key
    with open(WIREGUARD_KEY_FILE, "w") as f:
        f.write(wg_priv_b64)

    # Write WireGuard conf file
    conf_content = (
        "[Interface]\n"
        f"PrivateKey = {wg_priv_b64}\n"
        f"Address = {assigned_ip}/32\n"
        "[Peer]\n"
        f"PublicKey = {server_wg_pubkey}\n"
        f"Endpoint = {tunnel_endpoint}:51820\n"
        f"AllowedIPs = {server_wg_ip}/32\n"
        "PersistentKeepalive = 25\n"
    )
    with open(WIREGUARD_CONF_PATH, "w") as f:
        f.write(conf_content)

    return agent_id, tunnel_config


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

def refresh_token(signing_key: SigningKey, agent_id: str) -> str:
    """
    Request a new JWT by signing agent_id:timestamp:nonce.

    Returns the JWT string.
    Raises RuntimeError on failure.
    """
    timestamp = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())
    message = f"{agent_id}:{timestamp}:{nonce}"

    sig = signing_key.sign(message.encode())
    sig_b64 = base64.b64encode(sig.signature).decode("utf-8")

    headers = {
        "x-agent-timestamp": timestamp,
        "x-agent-nonce": nonce,
        "x-agent-signature": sig_b64,
    }

    url = REFRESH_URL_TPL.format(agent_id)
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError(f"Token refresh failed ({resp.status_code}): {resp.text}")

    token = resp.text.strip().strip('"')
    if not token:
        raise RuntimeError("Empty token in refresh response")

    return token


# ---------------------------------------------------------------------------
# Stateful auth wrapper
# ---------------------------------------------------------------------------

class AgentAuth:
    """
    Holds agent credentials and manages JWT lifecycle.

    Call ``ensure_token()`` before any authenticated request —
    it refreshes the JWT if it's missing or about to expire.
    """

    def __init__(self, signing_key: SigningKey, agent_id: str):
        self.signing_key = signing_key
        self.agent_id = agent_id
        self._token: str | None = None
        self._token_obtained_at: float = 0.0

    @property
    def token(self) -> str | None:
        return self._token

    def token_expired(self) -> bool:
        if self._token is None:
            return True
        return (time.time() - self._token_obtained_at) >= TOKEN_REFRESH_INTERVAL

    def ensure_token(self) -> str:
        """Refresh the JWT if needed and return it."""
        if self.token_expired():
            self._token = refresh_token(self.signing_key, self.agent_id)
            self._token_obtained_at = time.time()
        return self._token

    def force_refresh(self) -> str:
        """Force an immediate token refresh."""
        self._token = None
        return self.ensure_token()

    def auth_header(self) -> dict:
        """Return an Authorization header dict with a valid Bearer token."""
        return {"Authorization": f"Bearer {self.ensure_token()}"}
