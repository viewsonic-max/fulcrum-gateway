"""starlink MCP server entrypoint."""

from __future__ import annotations

import os

from ..stdio_server import ServerConfig, serve
from .tools import build_tools

SERVER_NAME = "ax-starlink"
SERVER_VERSION = "0.1.0"
INSTRUCTIONS = (
    "SpaceX Starlink Enterprise connector. Inspect connectivity assets and "
    "device telemetry via the Starlink Enterprise REST API. Three tools:\n"
    "- starlink_get_service_lines(account_number): list service lines.\n"
    "- starlink_get_account(account_number): account summary.\n"
    "- starlink_query_telemetry(query): query device telemetry.\n"
    "Starlink is a tool/connector (the comms/transport layer), not a model. "
    "Requires STARLINK_CLIENT_ID and STARLINK_CLIENT_SECRET (OAuth2 "
    "service-account client credentials). Credentials are never logged or "
    "written to the workspace."
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
