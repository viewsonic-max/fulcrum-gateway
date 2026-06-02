"""Config + lazy client loader for Aalyria's Spacetime API.

Spacetime is a **gRPC** temporospatial SDN platform: clients are generated
from the protobufs in github.com/aalyria/api and shipped as the
`spacetime-api` Python metapackage on Aalyria's private package index
(`pip install spacetime-api --extra-index-url <aalyria index>`). Auth is a
self-signed JWT (private/public keypair) sent as a bearer token in the
gRPC `authorization` metadata.

Because Spacetime is gRPC-first (not REST), this connector can't use the
stdlib `urllib` path the lattice/starlink connectors use — it must drive the
official `spacetime-api` client. We therefore:

  * read connection + JWT-auth config from the environment,
  * lazy-import `spacetime-api` (a heavy, private-index optional dep), and
  * surface clear, actionable errors when the client isn't installed or the
    connection isn't configured.

The actual NBI/SBI RPC bindings are the remaining wiring (see
`tools.py` / the connector README): the generated stub method + message
shapes must be pinned against a specific `spacetime-api` version against the
operator's Spacetime environment before they're called, so we do not invent
them here. This mirrors how the repo ships its langgraph/strands bridges as
stubs pending real wiring.

Trust boundary: the private key path + minted JWT are used in-process only;
never logged, never written to the workspace.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class SpacetimeNotInstalled(Exception):
    """The `spacetime-api` client package is not importable."""


class SpacetimeNotConfigured(Exception):
    """Required Spacetime connection/auth env vars are missing."""


class SpacetimeNotWired(Exception):
    """A tool reached a documented but not-yet-bound RPC.

    Distinct from NotInstalled/NotConfigured: the client is present and
    configured, but the specific NBI/SBI RPC binding is intentionally left as
    a confirm-before-merge step (see connector README) rather than guessed.
    """


@dataclass
class AalyriaConfig:
    """Spacetime connection + JWT-auth configuration, read from the env."""

    host: str
    agent_email: str
    private_key_id: str
    private_key_file: str

    @classmethod
    def from_env(cls) -> "AalyriaConfig":
        """Build config from SPACETIME_* env vars, failing closed.

        Required:
          SPACETIME_HOST             gRPC endpoint (e.g. dns:///<env>:443)
          SPACETIME_AGENT_EMAIL      service-account email for the JWT subject
          SPACETIME_PRIVATE_KEY_ID   key id registered with Spacetime
          SPACETIME_PRIVATE_KEY_FILE path to the PEM private key
        """
        host = os.environ.get("SPACETIME_HOST", "").strip()
        agent_email = os.environ.get("SPACETIME_AGENT_EMAIL", "").strip()
        private_key_id = os.environ.get("SPACETIME_PRIVATE_KEY_ID", "").strip()
        private_key_file = os.environ.get("SPACETIME_PRIVATE_KEY_FILE", "").strip()
        missing = [
            name
            for name, value in (
                ("SPACETIME_HOST", host),
                ("SPACETIME_AGENT_EMAIL", agent_email),
                ("SPACETIME_PRIVATE_KEY_ID", private_key_id),
                ("SPACETIME_PRIVATE_KEY_FILE", private_key_file),
            )
            if not value
        ]
        if missing:
            raise SpacetimeNotConfigured(", ".join(missing) + " not set")
        return cls(
            host=host,
            agent_email=agent_email,
            private_key_id=private_key_id,
            private_key_file=private_key_file,
        )


def spacetime_client_available() -> bool:
    """True when the `spacetime-api` client package can be imported."""
    import importlib.util

    return importlib.util.find_spec("spacetime") is not None


def require_client() -> None:
    """Raise SpacetimeNotInstalled with an actionable message if the
    `spacetime-api` client is not importable."""
    if not spacetime_client_available():
        raise SpacetimeNotInstalled(
            "The `spacetime-api` client is not installed. Install it from Aalyria's "
            "package index: pip install spacetime-api --extra-index-url "
            "https://us-central1-python.pkg.dev/a5a-spacetime-artifacts/py-packages/simple"
        )
