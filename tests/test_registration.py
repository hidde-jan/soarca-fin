"""Tests for registration persistence (soarca_fin.registration)."""

from __future__ import annotations

from pathlib import Path

from soarca_fin.registration import (
    FileRegistrationStore,
    FinRegistration,
    InMemoryRegistrationStore,
)


def _registration() -> FinRegistration:
    return FinRegistration(
        fin_id="fin-1",
        fin_token="token-1",
        poll_interval_seconds=5,
        long_poll_timeout_seconds=30,
        job_lease_seconds=60,
    )


def test_file_store_round_trip(tmp_path: Path) -> None:
    store = FileRegistrationStore(tmp_path / "registration.json")

    assert store.load() is None

    store.save(_registration())
    loaded = store.load()

    assert loaded == _registration()


def test_file_store_sets_owner_only_permissions(tmp_path: Path) -> None:
    path = tmp_path / "registration.json"
    store = FileRegistrationStore(path)
    store.save(_registration())

    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_file_store_clear(tmp_path: Path) -> None:
    path = tmp_path / "registration.json"
    store = FileRegistrationStore(path)
    store.save(_registration())

    store.clear()

    assert store.load() is None
    assert not path.exists()


def test_file_store_creates_parent_directories(tmp_path: Path) -> None:
    store = FileRegistrationStore(tmp_path / "nested" / "dir" / "registration.json")
    store.save(_registration())

    assert store.load() == _registration()


def test_in_memory_store_round_trip() -> None:
    store = InMemoryRegistrationStore()

    assert store.load() is None

    store.save(_registration())
    assert store.load() == _registration()

    store.clear()
    assert store.load() is None
