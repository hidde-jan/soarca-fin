"""Tests for credential persistence (soarca_fin.credentials)."""

from __future__ import annotations

from pathlib import Path

from soarca_fin.credentials import Credentials, FileCredentialStore, InMemoryCredentialStore


def _credentials() -> Credentials:
    return Credentials(
        fin_id="fin-1",
        fin_token="token-1",
        poll_interval_seconds=5,
        long_poll_timeout_seconds=30,
        job_lease_seconds=60,
    )


def test_file_store_round_trip(tmp_path: Path) -> None:
    store = FileCredentialStore(tmp_path / "creds.json")

    assert store.load() is None

    store.save(_credentials())
    loaded = store.load()

    assert loaded == _credentials()


def test_file_store_sets_owner_only_permissions(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    store = FileCredentialStore(path)
    store.save(_credentials())

    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_file_store_clear(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    store = FileCredentialStore(path)
    store.save(_credentials())

    store.clear()

    assert store.load() is None
    assert not path.exists()


def test_file_store_creates_parent_directories(tmp_path: Path) -> None:
    store = FileCredentialStore(tmp_path / "nested" / "dir" / "creds.json")
    store.save(_credentials())

    assert store.load() == _credentials()


def test_in_memory_store_round_trip() -> None:
    store = InMemoryCredentialStore()

    assert store.load() is None

    store.save(_credentials())
    assert store.load() == _credentials()

    store.clear()
    assert store.load() is None
