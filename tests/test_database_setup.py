import json
import sys
from pathlib import Path

import psycopg
import pytest

import configure_database
import local_secrets


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(local_secrets, "ROOT", tmp_path)
    monkeypatch.setattr(local_secrets, "GLOBAL_DB", tmp_path / "legacy.json")
    monkeypatch.delenv("COMPANION_DB_CREDENTIALS", raising=False)
    (tmp_path / "local_config.json.example").write_text('{"port":6185}', "utf-8")
    return tmp_path


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows current-user encryption")
def test_project_credentials_roundtrip_does_not_overwrite_legacy(isolated):
    legacy = isolated / "legacy.json"
    legacy.write_text("legacy sentinel", "utf-8")
    path = isolated / "runtime/secrets/postgresql.json"
    local_secrets.save_database("127.0.0.1", 5432, "fixture_user", "fixture-secret", path=path)
    (isolated / "local_config.json").write_text(
        json.dumps({"database_credentials_file": "runtime/secrets/postgresql.json"}), "utf-8"
    )
    assert "fixture-secret" not in path.read_text("utf-8")
    assert local_secrets.database_parameters()["password"] == "fixture-secret"
    assert legacy.read_text("utf-8") == "legacy sentinel"


def test_old_install_keeps_global_fallback_and_override_is_explicit(isolated, monkeypatch):
    assert local_secrets.credential_path() == isolated / "legacy.json"
    (isolated / "local_config.json").write_text(
        json.dumps({"database_credentials_file": "runtime/project.json"}), "utf-8"
    )
    assert local_secrets.credential_path() == isolated / "runtime/project.json"
    monkeypatch.setenv("COMPANION_DB_CREDENTIALS", "runtime/explicit.json")
    assert local_secrets.credential_path() == isolated / "runtime/explicit.json"


def test_failed_login_never_creates_config_or_credentials(isolated, monkeypatch):
    def fail(**kwargs):
        assert kwargs["dbname"] == "companion_bot"
        raise psycopg.OperationalError("synthetic login failure")

    monkeypatch.setattr(configure_database.psycopg, "connect", fail)
    with pytest.raises(psycopg.OperationalError):
        configure_database.configure("127.0.0.1", 5432, "fixture", "fixture-secret", isolated)
    assert not (isolated / "local_config.json").exists()
    assert not (isolated / "runtime/secrets/postgresql.json").exists()


def test_missing_vector_does_not_persist_credentials(isolated, monkeypatch):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query):
            assert "pg_extension" in query
            return self

        def fetchone(self):
            return None

    monkeypatch.setattr(configure_database.psycopg, "connect", lambda **kwargs: Connection())
    with pytest.raises(ValueError, match="pgvector"):
        configure_database.configure("127.0.0.1", 5432, "fixture", "fixture-secret", isolated)
    assert not (isolated / "local_config.json").exists()


def test_deployment_database_login_and_safe_save_in_temporary_root(tmp_path):
    params = local_secrets.database_parameters()
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "local_config.json.example").write_text(
        (root / "local_config.json.example").read_text("utf-8"), "utf-8"
    )
    target, version = configure_database.configure(
        params["host"], params["port"], params["user"], params["password"], tmp_path
    )
    assert version and target.is_relative_to(tmp_path)
    protected = json.loads(target.read_text("utf-8"))
    assert "password" not in protected and "password_dpapi" in protected
    config = json.loads((tmp_path / "local_config.json").read_text("utf-8"))
    assert config["database_credentials_file"] == "runtime/secrets/postgresql.json"
