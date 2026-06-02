"""aalyria MCP server entrypoint."""

from __future__ import annotations

import os

from ..stdio_server import ServerConfig, serve
from .tools import build_tools

SERVER_NAME = "ax-aalyria-spacetime"
SERVER_VERSION = "0.1.0"
INSTRUCTIONS = (
    "Aalyria Spacetime connector. Orchestrate/inspect the temporospatial SDN "
    "network layer (satellites, ground stations, platforms) via the Spacetime "
    "gRPC API. Two tools:\n"
    "- spacetime_status(): report client install + config readiness (no RPCs).\n"
    "- spacetime_query_network_elements(): NBI query (scaffold — see README).\n"
    "Spacetime is a tool/connector, not a model. It is gRPC-first and needs "
    "the `spacetime-api` client plus SPACETIME_* connection/auth config. The "
    "private key + minted JWT are never logged or written to the workspace."
)


def main() -> None:
    config = ServerConfig(
        name=SERVER_NAME,
        version=SERVER_VERSION,
        instructions=INSTRUCTIONS,
        tools=build_tools(),
        debug=os.environ.get("AX_MCP_DEBUG", "").lower() in {"1", "true", "yes", "on"},
    )
    serve(config)


if __name__ == "__main__":
    main()
