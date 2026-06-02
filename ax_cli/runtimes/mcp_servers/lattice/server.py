"""lattice MCP server entrypoint."""

from __future__ import annotations

import os

from ..stdio_server import ServerConfig, serve
from .tools import build_tools

SERVER_NAME = "ax-lattice"
SERVER_VERSION = "0.1.0"
INSTRUCTIONS = (
    "Anduril Lattice connector. Read and publish Common Operational Picture "
    "entities via the Lattice Entities REST API. Two tools:\n"
    "- lattice_get_entity(entity_id): fetch one entity, returns entity JSON.\n"
    "- lattice_publish_entity(entity_id, entity): create/update one entity.\n"
    "Lattice is a tool/connector, not a model backend. Requires "
    "LATTICE_BASE_URL and LATTICE_BEARER_TOKEN in the environment "
    "(LATTICE_SANDBOX_TOKEN for developer sandboxes). The bearer token is "
    "never logged or written to the workspace."
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
