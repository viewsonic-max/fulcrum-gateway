"""Gateway asset-type inference and Hermes/Ollama setup detection + operator profile.

Extracted from ``ax_cli/gateway.py`` (issue #28 Phase 2).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from .gateway_constants import (
    _CONTROLLED_ACTIVATIONS,
    _CONTROLLED_PLACEMENTS,
    _CONTROLLED_REPLY_MODES,
    _CONTROLLED_TELEMETRY_LEVELS,
    DEFAULT_OLLAMA_BASE_URL,
    _normalized_controlled,
    _override_fields,
    _template_operator_defaults,
)


def _hermes_repo_candidates(entry: dict[str, Any] | None = None) -> list[Path]:
    entry = entry or {}
    candidates: list[Path] = []
    seen: set[str] = set()

    def add(path_value: object) -> None:
        raw = str(path_value or "").strip()
        if not raw:
            return
        expanded = Path(raw).expanduser()
        key = str(expanded)
        if key in seen:
            return
        seen.add(key)
        candidates.append(expanded)

    add(entry.get("hermes_repo_path"))
    add(os.environ.get("HERMES_REPO_PATH"))

    workdir_raw = str(entry.get("workdir") or "").strip()
    if workdir_raw:
        workdir = Path(workdir_raw).expanduser()
        add(workdir.parent / "hermes-agent")
        if str(workdir).startswith("/home/ax-agent/agents"):
            add("/home/ax-agent/shared/repos/hermes-agent")

    add(Path.home() / "hermes-agent")
    return candidates


def hermes_setup_status(entry: dict[str, Any]) -> dict[str, Any]:
    template_id = str(entry.get("template_id") or "").strip().lower()
    runtime_type = str(entry.get("runtime_type") or "").strip().lower()
    # hermes_plugin invokes `hermes gateway run` resolved via _hermes_bin
    # (entry override / HERMES_BIN / $PATH / fallback) and loads the aX
    # platform plugin from this repo, so it has no hermes-agent checkout
    # dependency and must short-circuit the gate — even when template_id
    # is "hermes" (the plugin is now the default template runtime).
    if runtime_type == "hermes_plugin":
        return {"ready": True, "template_id": template_id}
    # sentinel_inference_sdk ships bundled with ax-cli and uses a gateway-owned
    # venv under ~/.ax/runtimes/sentinel_inference_sdk/<client> — no hermes-agent
    # checkout required. The venv is checked separately via `runtime status`.
    if runtime_type == "sentinel_inference_sdk":
        return {"ready": True, "template_id": template_id}
    if template_id != "hermes":
        return {"ready": True, "template_id": template_id}

    candidates = _hermes_repo_candidates(entry)
    resolved = next((candidate for candidate in candidates if candidate.exists()), None)
    if resolved is not None:
        return {
            "ready": True,
            "template_id": template_id,
            "resolved_path": str(resolved),
            "summary": f"Hermes checkout found at {resolved}.",
        }

    expected = candidates[0] if candidates else (Path.home() / "hermes-agent")
    return {
        "ready": False,
        "template_id": template_id,
        "resolved_path": None,
        "expected_path": str(expected),
        "summary": f"Hermes checkout not found at {expected}.",
        "detail": (
            f"Hermes checkout not found at {expected}. Set HERMES_REPO_PATH or clone hermes-agent to ~/hermes-agent."
        ),
    }


def _ollama_model_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    models = payload.get("models")
    if not isinstance(models, list):
        return rows
    for item in models:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("model") or "").strip()
        if not name:
            continue
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        families = details.get("families") if isinstance(details.get("families"), list) else []
        family_values = [str(value).strip() for value in families if str(value).strip()]
        remote_host = str(item.get("remote_host") or "").strip() or None
        lowered_name = name.lower()
        is_embedding = "embed" in lowered_name or any("bert" in family.lower() for family in family_values)
        rows.append(
            {
                "name": name,
                "family": str(details.get("family") or "").strip() or None,
                "families": family_values,
                "parameter_size": str(details.get("parameter_size") or "").strip() or None,
                "modified_at": str(item.get("modified_at") or "").strip() or None,
                "remote_host": remote_host,
                "is_cloud": bool(remote_host or lowered_name.endswith(":cloud") or lowered_name.endswith("-cloud")),
                "is_embedding": is_embedding,
            }
        )
    return rows


def _recommended_ollama_model(rows: list[dict[str, Any]]) -> str | None:
    if not rows:
        return None

    def pick(candidates: list[dict[str, Any]]) -> str | None:
        if not candidates:
            return None
        ordered = sorted(
            candidates,
            key=lambda item: (
                str(item.get("modified_at") or ""),
                str(item.get("parameter_size") or ""),
                str(item.get("name") or ""),
            ),
            reverse=True,
        )
        return str(ordered[0].get("name") or "").strip() or None

    local_rows = [item for item in rows if not bool(item.get("is_cloud"))]
    local_chat_rows = [item for item in local_rows if not bool(item.get("is_embedding"))]
    chat_rows = [item for item in rows if not bool(item.get("is_embedding"))]
    return pick(local_chat_rows) or pick(local_rows) or pick(chat_rows) or pick(rows)


def ollama_setup_status(*, preferred_model: str | None = None) -> dict[str, Any]:
    base_url = DEFAULT_OLLAMA_BASE_URL
    endpoint = f"{base_url}/api/tags"
    preferred = str(preferred_model or "").strip() or None
    try:
        response = httpx.get(endpoint, timeout=3.0)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Ollama returned a non-object response.")
    except Exception as exc:
        return {
            "ready": False,
            "server_reachable": False,
            "base_url": base_url,
            "endpoint": endpoint,
            "preferred_model": preferred,
            "preferred_model_available": False,
            "recommended_model": None,
            "available_models": [],
            "local_models": [],
            "models": [],
            "summary": f"Ollama server not reachable at {base_url}.",
            "detail": str(exc),
        }

    rows = _ollama_model_rows(payload)
    available_models = [str(item.get("name") or "") for item in rows if str(item.get("name") or "").strip()]
    local_models = [str(item.get("name") or "") for item in rows if not bool(item.get("is_cloud"))]
    preferred_available = bool(preferred and preferred in available_models)
    recommended_model = preferred if preferred_available else _recommended_ollama_model(rows)
    ready = bool(available_models)
    if preferred and not preferred_available:
        summary = f"Ollama is reachable, but {preferred} is not installed locally."
    elif recommended_model:
        summary = f"Ollama is reachable. Recommended model: {recommended_model}."
    elif available_models:
        summary = f"Ollama is reachable with {len(available_models)} model(s) available."
    else:
        summary = "Ollama is reachable, but no models are installed yet."
    return {
        "ready": ready,
        "server_reachable": True,
        "base_url": base_url,
        "endpoint": endpoint,
        "preferred_model": preferred,
        "preferred_model_available": preferred_available,
        "recommended_model": recommended_model,
        "available_models": available_models,
        "local_models": local_models,
        "models": rows,
        "summary": summary,
        "detail": None,
    }


def infer_operator_profile(snapshot: dict[str, Any]) -> dict[str, str]:
    defaults = _template_operator_defaults(
        str(snapshot.get("template_id") or "").strip() or None, snapshot.get("runtime_type")
    )
    overrides = _override_fields(snapshot, domain="operator")
    return {
        "placement": _normalized_controlled(
            snapshot.get("placement"), _CONTROLLED_PLACEMENTS, fallback=defaults["placement"]
        )
        if "placement" in overrides
        else defaults["placement"],
        "activation": _normalized_controlled(
            snapshot.get("activation"), _CONTROLLED_ACTIVATIONS, fallback=defaults["activation"]
        )
        if "activation" in overrides
        else defaults["activation"],
        "reply_mode": _normalized_controlled(
            snapshot.get("reply_mode"), _CONTROLLED_REPLY_MODES, fallback=defaults["reply_mode"]
        )
        if "reply_mode" in overrides
        else defaults["reply_mode"],
        "telemetry_level": _normalized_controlled(
            snapshot.get("telemetry_level"),
            _CONTROLLED_TELEMETRY_LEVELS,
            fallback=defaults["telemetry_level"],
        )
        if "telemetry_level" in overrides
        else defaults["telemetry_level"],
    }


def _attached_session_log_is_ready(path: object) -> bool:
    if not path:
        return False
    try:
        content = Path(str(path)).read_text(errors="ignore")[-8000:]
    except OSError:
        return False
    return "Listening for channel messages" in content or "ax-channel" in content
