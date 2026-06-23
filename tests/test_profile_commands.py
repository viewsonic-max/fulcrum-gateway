import hashlib
import socket
from unittest.mock import MagicMock, patch

import httpx
from typer.testing import CliRunner

from ax_cli.commands import profile
from ax_cli.main import app

runner = CliRunner()


def _write_profile(
    tmp_path,
    *,
    name="dev",
    agent_id=None,
    token="axp_a_TestKey.TestSecret",
    host_binding=None,
    workdir_hash=None,
    workdir_path=None,
):
    profiles_dir = tmp_path / "profiles"
    token_file = tmp_path / "token.pat"
    token_file.write_text(token)
    token_sha = hashlib.sha256(token_file.read_text().strip().encode()).hexdigest()
    wdir_hash = workdir_hash or hashlib.sha256(str(tmp_path.resolve()).encode()).hexdigest()
    profile_dir = profiles_dir / name
    profile_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f'name = "{name}"',
        'base_url = "https://dev.paxai.app"',
        'agent_name = "chatgpt"',
        f'token_file = "{token_file}"',
        f'token_sha256 = "{token_sha}"',
        f'host_binding = "{host_binding or socket.gethostname()}"',
        f'workdir_hash = "{wdir_hash}"',
        'space_id = "space-1"',
    ]
    if workdir_path is not None:
        lines.append(f'workdir_path = "{workdir_path}"')
    if agent_id is not None:
        lines.append(f'agent_id = "{agent_id}"')
    (profile_dir / "profile.toml").write_text("\n".join(lines) + "\n")
    return profiles_dir


def test_profile_env_exports_agent_id_when_present(monkeypatch, tmp_path):
    profiles_dir = _write_profile(tmp_path, agent_id="agent-1")
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["profile", "env", "dev"])

    assert result.exit_code == 0
    assert "export AX_AGENT_ID=agent-1" in result.stdout


def test_profile_env_clears_stale_agent_id_when_missing(monkeypatch, tmp_path):
    profiles_dir = _write_profile(tmp_path, agent_id=None)
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["profile", "env", "dev"])

    assert result.exit_code == 0
    assert "export AX_AGENT_ID=none" in result.stdout


def test_profile_env_shell_quotes_export_values(monkeypatch, tmp_path):
    profiles_dir = _write_profile(
        tmp_path,
        agent_id="agent-1",
        token='axp_a_TestKey.$(echo unsafe)"',
    )
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["profile", "env", "dev"])

    assert result.exit_code == 0
    assert "export AX_TOKEN='axp_a_TestKey.$(echo unsafe)\"'" in result.stdout


def test_profile_env_outputs_shell_failure_when_verification_fails(monkeypatch, tmp_path):
    profiles_dir = _write_profile(tmp_path, agent_id="agent-1")
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    monkeypatch.chdir(other_dir)

    result = runner.invoke(app, ["profile", "env", "dev"])

    assert result.exit_code == 1
    assert "Working directory mismatch" in result.stderr
    assert "false # ax profile env failed verification" in result.stdout


def test_profiles_dir_respects_ax_config_dir(monkeypatch, tmp_path):
    config_dir = tmp_path / "custom-ax-config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(profile, "PROFILES_DIR", None)

    assert profile._profiles_dir() == config_dir / "profiles"
    assert (config_dir / "profiles").is_dir()


# --- _load_profile ---


def test_load_profile_returns_dict(monkeypatch, tmp_path):
    profiles_dir = _write_profile(tmp_path)
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)

    data = profile._load_profile("dev")
    assert data["name"] == "dev"
    assert data["base_url"] == "https://dev.paxai.app"


def test_load_profile_missing_exits(monkeypatch, tmp_path):
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)

    import pytest
    from typer import Exit

    with pytest.raises(Exit):
        profile._load_profile("nonexistent")


# --- _token_sha256 ---


def test_token_sha256_hashes_content(tmp_path):
    tf = tmp_path / "token.pat"
    tf.write_text("my-secret-token\n")
    result = profile._token_sha256(str(tf))
    expected = hashlib.sha256(b"my-secret-token").hexdigest()
    assert result == expected


# --- _workdir_hash ---


def test_workdir_hash_with_directory(tmp_path):
    result = profile._workdir_hash(str(tmp_path))
    expected = hashlib.sha256(str(tmp_path.resolve()).encode()).hexdigest()
    assert result == expected


def test_workdir_hash_defaults_to_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    result = profile._workdir_hash()
    expected = hashlib.sha256(str(tmp_path.resolve()).encode()).hexdigest()
    assert result == expected


# --- _write_toml ---


def test_write_toml_creates_file(tmp_path):
    path = tmp_path / "sub" / "file.toml"
    data = {"name": "test", "count": 42, "enabled": True, "disabled": False}
    profile._write_toml(path, data)

    content = path.read_text()
    assert 'name = "test"' in content
    assert "count = 42" in content
    assert "enabled = true" in content
    assert "disabled = false" in content
    assert (path.stat().st_mode & 0o777) == 0o600


# --- _active_profile / _set_active ---


def test_active_profile_returns_none_when_no_marker(monkeypatch, tmp_path):
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)

    assert profile._active_profile() is None


def test_set_active_and_read_back(monkeypatch, tmp_path):
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)

    profile._set_active("my-profile")
    assert profile._active_profile() == "my-profile"


# --- _verify_profile ---


def test_verify_profile_all_ok(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    tf = tmp_path / "tok.pat"
    tf.write_text("secret")
    sha = hashlib.sha256(b"secret").hexdigest()
    wdir = hashlib.sha256(str(tmp_path.resolve()).encode()).hexdigest()
    hostname = socket.gethostname()

    prof = {
        "token_file": str(tf),
        "token_sha256": sha,
        "host_binding": hostname,
        "workdir_hash": wdir,
    }
    assert profile._verify_profile(prof) == []


def test_verify_profile_token_file_missing():
    prof = {"token_file": "/nonexistent/token.pat"}
    failures = profile._verify_profile(prof)
    assert len(failures) == 1
    assert "Token file missing" in failures[0]


def test_verify_profile_token_fingerprint_mismatch(tmp_path):
    tf = tmp_path / "tok.pat"
    tf.write_text("original")
    prof = {
        "token_file": str(tf),
        "token_sha256": "wrong-hash",
    }
    failures = profile._verify_profile(prof)
    assert any("fingerprint mismatch" in f for f in failures)


def test_verify_profile_host_mismatch(tmp_path):
    tf = tmp_path / "tok.pat"
    tf.write_text("secret")
    sha = hashlib.sha256(b"secret").hexdigest()

    prof = {
        "token_file": str(tf),
        "token_sha256": sha,
        "host_binding": "other-host-that-does-not-match",
    }
    failures = profile._verify_profile(prof)
    assert any("Host mismatch" in f for f in failures)


def test_verify_profile_workdir_mismatch(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    tf = tmp_path / "tok.pat"
    tf.write_text("secret")
    sha = hashlib.sha256(b"secret").hexdigest()

    prof = {
        "token_file": str(tf),
        "token_sha256": sha,
        "host_binding": socket.gethostname(),
        "workdir_hash": "definitely-wrong-hash",
    }
    failures = profile._verify_profile(prof)
    assert any("Working directory mismatch" in f for f in failures)


# --- _register_fingerprint ---


def test_register_fingerprint_returns_none_when_missing_fields():
    assert profile._register_fingerprint({}) is None
    assert profile._register_fingerprint({"base_url": "http://x"}) is None
    assert profile._register_fingerprint({"agent_id": "abc"}) is None


def test_register_fingerprint_returns_none_when_token_file_missing():
    prof = {"base_url": "http://x", "agent_id": "abc", "token_file": "/no/such/file"}
    assert profile._register_fingerprint(prof) is None


def test_register_fingerprint_success(tmp_path):
    tf = tmp_path / "token.pat"
    tf.write_text("secret-token")
    prof = {
        "base_url": "http://localhost:8001",
        "agent_id": "agent-1",
        "token_file": str(tf),
        "token_sha256": "somehash",
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    with patch.object(httpx, "post", return_value=mock_resp):
        assert profile._register_fingerprint(prof) is None


def test_register_fingerprint_non_200(tmp_path):
    tf = tmp_path / "token.pat"
    tf.write_text("secret-token")
    prof = {
        "base_url": "http://localhost:8001",
        "agent_id": "agent-1",
        "token_file": str(tf),
        "token_sha256": "somehash",
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    with patch.object(httpx, "post", return_value=mock_resp):
        result = profile._register_fingerprint(prof)
        assert "403" in result


def test_register_fingerprint_connect_error(tmp_path):
    tf = tmp_path / "token.pat"
    tf.write_text("secret-token")
    prof = {
        "base_url": "http://localhost:8001",
        "agent_id": "agent-1",
        "token_file": str(tf),
        "token_sha256": "somehash",
    }
    with patch.object(httpx, "post", side_effect=httpx.ConnectError("fail")):
        assert profile._register_fingerprint(prof) is None


# --- add command ---


def test_add_command_creates_profile(monkeypatch, tmp_path):
    profiles_dir = tmp_path / "profiles"
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)
    monkeypatch.chdir(tmp_path)

    tf = tmp_path / "my-token.pat"
    tf.write_text("axp_a_mykey.mysecret")

    result = runner.invoke(
        app,
        [
            "profile",
            "add",
            "test-profile",
            "--url",
            "https://paxai.app",
            "--token-file",
            str(tf),
            "--agent-name",
            "myagent",
            "--agent-id",
            "agent-123",
            "--space-id",
            "space-456",
        ],
    )
    assert result.exit_code == 0
    assert "test-profile" in result.output
    assert (profiles_dir / "test-profile" / "profile.toml").exists()


def test_add_command_fails_when_token_file_missing(monkeypatch, tmp_path):
    profiles_dir = tmp_path / "profiles"
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)

    result = runner.invoke(
        app,
        [
            "profile",
            "add",
            "test-profile",
            "--url",
            "https://paxai.app",
            "--token-file",
            "/no/such/file",
            "--agent-name",
            "myagent",
        ],
    )
    assert result.exit_code == 1
    assert "not found" in result.output


# --- use command ---


def test_use_command_sets_active_profile(monkeypatch, tmp_path):
    profiles_dir = _write_profile(tmp_path, agent_id="agent-1")
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)
    monkeypatch.chdir(tmp_path)

    with patch.object(httpx, "post", return_value=MagicMock(status_code=200)):
        result = runner.invoke(app, ["profile", "use", "dev"])

    assert result.exit_code == 0
    assert "Active profile" in result.output
    assert profile._active_profile() == "dev"


def test_use_command_fails_verification(monkeypatch, tmp_path):
    profiles_dir = _write_profile(tmp_path, agent_id="agent-1")
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)

    result = runner.invoke(app, ["profile", "use", "dev"])
    assert result.exit_code == 1
    assert "verification failed" in result.output


def test_use_command_shows_backend_registration_error(monkeypatch, tmp_path):
    profiles_dir = _write_profile(tmp_path, agent_id="agent-1")
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)
    monkeypatch.chdir(tmp_path)

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    with patch.object(httpx, "post", return_value=mock_resp):
        result = runner.invoke(app, ["profile", "use", "dev"])

    assert result.exit_code == 0
    assert "Backend" in result.output


def test_use_command_shows_fingerprint_registered(monkeypatch, tmp_path):
    profiles_dir = _write_profile(tmp_path, agent_id="agent-1")
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)
    monkeypatch.chdir(tmp_path)

    mock_resp = MagicMock()
    mock_resp.status_code = 201
    with patch.object(httpx, "post", return_value=mock_resp):
        result = runner.invoke(app, ["profile", "use", "dev"])

    assert result.exit_code == 0
    assert "Fingerprint registered" in result.output


# --- list command ---


def test_list_profiles_empty(monkeypatch, tmp_path):
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)

    result = runner.invoke(app, ["profile", "list"])
    assert result.exit_code == 0
    assert "No profiles" in result.output


def test_list_profiles_shows_entries(monkeypatch, tmp_path):
    profiles_dir = _write_profile(tmp_path, name="dev", agent_id="agent-1")
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["profile", "list"])
    assert result.exit_code == 0
    assert "dev" in result.output
    assert "chatgpt" in result.output


def test_list_profiles_marks_active(monkeypatch, tmp_path):
    profiles_dir = _write_profile(tmp_path, name="dev", agent_id="agent-1")
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)
    monkeypatch.chdir(tmp_path)
    profile._set_active("dev")

    result = runner.invoke(app, ["profile", "list"])
    assert result.exit_code == 0


# --- verify command ---


def test_verify_command_success(monkeypatch, tmp_path):
    profiles_dir = _write_profile(tmp_path, agent_id="agent-1")
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["profile", "verify", "dev"])
    assert result.exit_code == 0
    assert "verified" in result.output


def test_verify_command_uses_active_profile(monkeypatch, tmp_path):
    profiles_dir = _write_profile(tmp_path, agent_id="agent-1")
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)
    monkeypatch.chdir(tmp_path)
    profile._set_active("dev")

    result = runner.invoke(app, ["profile", "verify"])
    assert result.exit_code == 0
    assert "verified" in result.output


def test_verify_command_no_active_profile(monkeypatch, tmp_path):
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)

    result = runner.invoke(app, ["profile", "verify"])
    assert result.exit_code == 1
    assert "No active profile" in result.output


def test_verify_command_failure(monkeypatch, tmp_path):
    profiles_dir = _write_profile(tmp_path, agent_id="agent-1")
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)

    result = runner.invoke(app, ["profile", "verify", "dev"])
    assert result.exit_code == 1
    assert "failed verification" in result.output


def test_verify_shows_workdir_path_on_success(monkeypatch, tmp_path):
    profiles_dir = _write_profile(tmp_path, agent_id="agent-1", workdir_path=str(tmp_path))
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["profile", "verify", "dev"])
    assert result.exit_code == 0


# --- remove command ---


def test_remove_command(monkeypatch, tmp_path):
    profiles_dir = _write_profile(tmp_path, name="dev")
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)

    result = runner.invoke(app, ["profile", "remove", "dev"])
    assert result.exit_code == 0
    assert "Removed" in result.output
    assert not (profiles_dir / "dev" / "profile.toml").exists()


def test_remove_active_profile_clears_marker(monkeypatch, tmp_path):
    profiles_dir = _write_profile(tmp_path, name="dev")
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)
    profile._set_active("dev")

    result = runner.invoke(app, ["profile", "remove", "dev"])
    assert result.exit_code == 0
    assert "was active" in result.output
    assert profile._active_profile() is None


def test_remove_nonexistent_profile(monkeypatch, tmp_path):
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)

    result = runner.invoke(app, ["profile", "remove", "nonexistent"])
    assert result.exit_code == 1
    assert "not found" in result.output


# --- env command ---


def test_env_command_no_active_profile(monkeypatch, tmp_path):
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)

    result = runner.invoke(app, ["profile", "env"])
    assert result.exit_code == 1
    assert "No active profile" in result.output


def test_env_command_exports_space_id(monkeypatch, tmp_path):
    profiles_dir = _write_profile(tmp_path, agent_id="agent-1")
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["profile", "env", "dev"])
    assert result.exit_code == 0
    assert "export AX_SPACE_ID=" in result.stdout
    assert "space-1" in result.stdout
