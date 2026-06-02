"""Thin stdlib HTTP client for SpaceX's Starlink Enterprise API.

No third-party deps (stdlib `urllib` only), matching the svg_viz / lattice
"no runtime deps beyond stdlib" property. The Starlink Enterprise API is a
REST service for managing accounts, service lines, user terminals, and
device telemetry for business/enterprise customers.

Auth: OAuth2 **client-credentials**. An account admin (or someone with
"Service Account Management" permission) creates a service account, yielding
a client id + secret. We exchange those at the token endpoint for a short
lived bearer token, then send it as `Authorization: Bearer <token>`.

Trust boundary: the client id/secret + minted token are read from the
environment by the caller and used for in-process requests only. They are
never logged and never written to the workspace — this is a Gateway-brokered
passthrough, not a credential store.

NOTE (confirm before merge): base URLs and resource paths here follow
SpaceX's public Starlink Enterprise API (web-api.starlink.com/enterprise) and
community client conventions. Starlink versions resource paths, and the
military Starshield surface is separate/classified — confirm the exact
token URL and resource paths against the operator's Enterprise API Swagger
(web-api.starlink.com/enterprise/swagger) before production use.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_TOKEN_URL = "https://www.starlink.com/api/auth/connect/token"
DEFAULT_BASE_URL = "https://web-api.starlink.com/enterprise"

_DEFAULT_TIMEOUT = 30


class StarlinkError(Exception):
    """Raised on transport failure or a non-2xx response from Starlink."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status


class StarlinkClient:
    """Minimal Enterprise-API client over `urllib` with OAuth2 client-credentials.

    Args:
        client_id / client_secret: service-account credentials.
        base_url: Enterprise API base (no trailing slash needed).
        token_url: OAuth2 token endpoint.
        timeout: per-request timeout in seconds.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        token_url: str = DEFAULT_TOKEN_URL,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._base_url = base_url.rstrip("/")
        self._token_url = token_url
        self._timeout = timeout
        self._token: str | None = None

    # ── auth ────────────────────────────────────────────────────────────────

    def _fetch_token(self) -> str:
        """Exchange client credentials for a bearer token (cached on self)."""
        form = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._token_url,
            data=form,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Do not echo the form body (carries the client secret).
            raise StarlinkError(
                f"Starlink token request failed (HTTP {exc.code}). Check STARLINK_CLIENT_ID/SECRET.",
                status=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise StarlinkError(f"Could not reach Starlink token endpoint: {exc.reason}") from exc

        token = payload.get("access_token")
        if not token:
            raise StarlinkError("Starlink token response did not contain an access_token")
        self._token = token
        return token

    def _auth_header(self) -> str:
        return f"Bearer {self._token or self._fetch_token()}"

    # ── requests ────────────────────────────────────────────────────────────

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = f"{self._base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": self._auth_header(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:500]
            except Exception:  # noqa: BLE001 - best-effort body read
                detail = ""
            raise StarlinkError(
                f"Starlink returned HTTP {exc.code}{f': {detail}' if detail else ''}",
                status=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise StarlinkError(f"Could not reach Starlink at {self._base_url}: {exc.reason}") from exc

        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StarlinkError(f"Starlink returned non-JSON response: {raw[:200]}") from exc

    # ── resources ───────────────────────────────────────────────────────────

    def get_service_lines(self, account_number: str) -> Any:
        """List the service lines on an enterprise account."""
        return self._request("GET", f"/v1/account/{account_number}/service-lines")

    def get_account(self, account_number: str) -> Any:
        """Fetch one enterprise account summary."""
        return self._request("GET", f"/v1/account/{account_number}")

    def query_telemetry(self, body: dict[str, Any]) -> Any:
        """Query device telemetry. `body` is passed through to the telemetry
        endpoint (e.g. {"accountNumber": "...", "batchSize": N}); the exact
        schema is operator/version specific — see the connector README."""
        return self._request("POST", "/v1/telemetry", body=body)
