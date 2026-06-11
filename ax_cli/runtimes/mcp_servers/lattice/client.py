"""Thin stdlib HTTP client for Anduril's Lattice Entities REST API.

No third-party deps (stdlib `urllib` only), matching the svg_viz server's
"no runtime deps beyond stdlib" property. The Lattice SDKs (anduril-lattice-sdk
for Python, @anduril-industries/lattice-sdk for JS) are heavier OAuth + gRPC
clients; this MCP connector deliberately wraps just the two documented REST
Entity-Manager operations an aX agent needs to read and publish tracks.

Trust boundary: the bearer token is read from the environment by the caller
and passed in here for a single request. It is never logged and never written
to the workspace — the connector is a Gateway-brokered passthrough, not a
credential store.

Endpoint shape (Lattice Entity Manager REST API, developer.anduril.com):

    GET  {base_url}/api/v1/entities/{entity_id}   — fetch one entity
    PUT  {base_url}/api/v1/entities/{entity_id}   — publish/update one entity

NOTE (confirm before merge): the `/api/v1/entities/{id}` path here is taken
from Anduril's public Entity-Manager REST reference, but Lattice environments
are versioned per deployment. Confirm the exact path + entity JSON schema
against the operator's Lattice environment (or the pinned anduril-lattice-sdk
version) before relying on this in production.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# Header Lattice sandboxes require in addition to the bearer token. Harmless
# to send against non-sandbox environments only if set; omitted otherwise.
SANDBOX_HEADER = "anduril-sandbox-authorization"

_DEFAULT_TIMEOUT = 30


class LatticeError(Exception):
    """Raised on transport failure or a non-2xx response from Lattice.

    Carries `status` (HTTP code, or None for transport-level failures) and a
    cleaned-up `message` so the MCP layer can surface something actionable to
    the operator instead of a bare traceback.
    """

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status


class LatticeClient:
    """Minimal Entities-API client over `urllib`.

    Args:
        base_url: Lattice environment base URL (no trailing slash needed).
        token: bearer token for `Authorization: Bearer <token>`.
        sandbox_token: optional value for the sandbox authorization header,
            required only by Lattice developer sandboxes.
        timeout: per-request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        sandbox_token: str | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._sandbox_token = (sandbox_token or "").strip() or None
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._sandbox_token:
            headers[SANDBOX_HEADER] = self._sandbox_token
        return headers

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = f"{self._base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # Surface status + a short slice of the response body. Do NOT echo
            # request headers (they carry the bearer token).
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:500]
            except Exception:  # noqa: BLE001 - best-effort body read
                detail = ""
            raise LatticeError(
                f"Lattice returned HTTP {exc.code}{f': {detail}' if detail else ''}",
                status=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise LatticeError(f"Could not reach Lattice at {self._base_url}: {exc.reason}") from exc

        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LatticeError(f"Lattice returned non-JSON response: {raw[:200]}") from exc

    def get_entity(self, entity_id: str) -> Any:
        """GET a single entity by id."""
        # entity_id can be model-supplied; quote it (safe="") so a value with
        # '/' or '..' cannot escape the /api/v1/entities/<id> path segment.
        return self._request("GET", f"/api/v1/entities/{urllib.parse.quote(entity_id, safe='')}")

    def publish_entity(self, entity_id: str, entity: dict[str, Any]) -> Any:
        """PUT (create-or-update) a single entity by id.

        The entity body must follow the Lattice Entity schema. The server
        validates at call time and returns an error for an invalid entity,
        which `_request` surfaces as a LatticeError carrying the status code.
        """
        return self._request("PUT", f"/api/v1/entities/{urllib.parse.quote(entity_id, safe='')}", body=entity)
