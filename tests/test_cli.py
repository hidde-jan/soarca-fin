import sys
from unittest.mock import Mock

import pytest

from soarca_fin import cli
from soarca_fin.app import Fin
from soarca_fin.registration import FinRegistration


def _write_module(tmp_path, name: str, body: str) -> None:
    (tmp_path / f"{name}.py").write_text(body)


@pytest.fixture
def isolated_sys_path(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    yield tmp_path
    for name in list(sys.modules):
        if name.startswith("cli_fixture_"):
            del sys.modules[name]


def test_load_fin_default_attribute(isolated_sys_path):
    _write_module(
        isolated_sys_path,
        "cli_fixture_default",
        'from soarca_fin import Fin\n\nfin = Fin("http://example.test")\n',
    )
    fin = cli._load_fin("cli_fixture_default")
    assert isinstance(fin, Fin)


def test_load_fin_explicit_attribute(isolated_sys_path):
    _write_module(
        isolated_sys_path,
        "cli_fixture_explicit",
        'from soarca_fin import Fin\n\nmy_fin = Fin("http://example.test")\n',
    )
    fin = cli._load_fin("cli_fixture_explicit:my_fin")
    assert isinstance(fin, Fin)


def test_load_fin_no_candidates_raises(isolated_sys_path):
    _write_module(isolated_sys_path, "cli_fixture_empty", "x = 1\n")
    with pytest.raises(SystemExit, match="no Fin instance found"):
        cli._load_fin("cli_fixture_empty")


def test_load_fin_multiple_candidates_raises(isolated_sys_path):
    _write_module(
        isolated_sys_path,
        "cli_fixture_multi",
        'from soarca_fin import Fin\n\na = Fin("http://a.test")\nb = Fin("http://b.test")\n',
    )
    with pytest.raises(SystemExit, match="multiple Fin instances found"):
        cli._load_fin("cli_fixture_multi")


def test_load_fin_missing_attribute_raises(isolated_sys_path):
    _write_module(isolated_sys_path, "cli_fixture_missing_attr", "x = 1\n")
    with pytest.raises(SystemExit, match="has no attribute"):
        cli._load_fin("cli_fixture_missing_attr:fin")


def test_load_fin_wrong_type_raises(isolated_sys_path):
    _write_module(isolated_sys_path, "cli_fixture_wrong_type", "fin = 42\n")
    with pytest.raises(SystemExit, match="is not a soarca_fin.Fin instance"):
        cli._load_fin("cli_fixture_wrong_type:fin")


def test_load_fin_import_error_raises():
    with pytest.raises(SystemExit, match="could not import"):
        cli._load_fin("no_such_module_at_all")


def test_resolve_app_spec_from_arg():
    assert cli._resolve_app_spec("my_fin:fin") == "my_fin:fin"


def test_resolve_app_spec_from_env(monkeypatch):
    monkeypatch.setenv("SOARCA_FIN_APP", "my_fin:fin")
    assert cli._resolve_app_spec(None) == "my_fin:fin"


def test_resolve_app_spec_missing_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("SOARCA_FIN_APP", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="no app found"):
        cli._resolve_app_spec(None)


def test_resolve_app_spec_discovers_fin_py(monkeypatch, tmp_path):
    monkeypatch.delenv("SOARCA_FIN_APP", raising=False)
    (tmp_path / "fin.py").write_text("fin = 1\n")
    monkeypatch.chdir(tmp_path)
    assert cli._resolve_app_spec(None) == "fin"


def test_resolve_app_spec_discovers_app_py_when_no_fin_py(monkeypatch, tmp_path):
    monkeypatch.delenv("SOARCA_FIN_APP", raising=False)
    (tmp_path / "app.py").write_text("fin = 1\n")
    monkeypatch.chdir(tmp_path)
    assert cli._resolve_app_spec(None) == "app"


def test_resolve_app_spec_prefers_fin_py_over_app_py(monkeypatch, tmp_path):
    monkeypatch.delenv("SOARCA_FIN_APP", raising=False)
    (tmp_path / "fin.py").write_text("fin = 1\n")
    (tmp_path / "app.py").write_text("fin = 1\n")
    monkeypatch.chdir(tmp_path)
    assert cli._resolve_app_spec(None) == "fin"


def test_load_fin_default_discovery_finds_app_attr(tmp_path, monkeypatch):
    _write_module(
        tmp_path,
        "app",
        'from soarca_fin import Fin\n\napp = Fin("http://example.test")\n',
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        fin = cli._load_fin(cli._resolve_app_spec(None))
        assert isinstance(fin, Fin)
    finally:
        sys.modules.pop("app", None)


def test_main_register_dispatches_with_token(isolated_sys_path, monkeypatch):
    _write_module(
        isolated_sys_path,
        "cli_fixture_register",
        'from soarca_fin import Fin\n\nfin = Fin("http://example.test")\n',
    )
    registration = FinRegistration(
        fin_id="fin-1",
        fin_token="tok",
        poll_interval_seconds=5,
        long_poll_timeout_seconds=30,
        job_lease_seconds=60,
    )
    mock_register = Mock(return_value=registration)
    monkeypatch.setattr(Fin, "register", mock_register)

    cli.main(["--app", "cli_fixture_register", "register", "--token", "my-secret"])

    mock_register.assert_called_once_with("my-secret")


def test_main_register_falls_back_to_env_token(isolated_sys_path, monkeypatch):
    _write_module(
        isolated_sys_path,
        "cli_fixture_register_env",
        'from soarca_fin import Fin\n\nfin = Fin("http://example.test")\n',
    )
    registration = FinRegistration(
        fin_id="fin-1",
        fin_token="tok",
        poll_interval_seconds=5,
        long_poll_timeout_seconds=30,
        job_lease_seconds=60,
    )
    mock_register = Mock(return_value=registration)
    monkeypatch.setattr(Fin, "register", mock_register)
    monkeypatch.setenv("SOARCA_FIN_REGISTRATION_TOKEN", "env-secret")

    cli.main(["--app", "cli_fixture_register_env", "register"])

    mock_register.assert_called_once_with("env-secret")


def test_main_run_dispatches_with_fin_token(isolated_sys_path, monkeypatch):
    _write_module(
        isolated_sys_path,
        "cli_fixture_run",
        'from soarca_fin import Fin\n\nfin = Fin("http://example.test")\n',
    )
    mock_run = Mock(return_value=None)
    monkeypatch.setattr(Fin, "run", mock_run)

    cli.main(["--app", "cli_fixture_run", "run", "--fin-token", "abc"])

    mock_run.assert_called_once_with(fin_token="abc")


def test_main_run_passes_timing_overrides(isolated_sys_path, monkeypatch):
    _write_module(
        isolated_sys_path,
        "cli_fixture_run_timing",
        'from soarca_fin import Fin\n\nfin = Fin("http://example.test")\n',
    )
    mock_run = Mock(return_value=None)
    monkeypatch.setattr(Fin, "run", mock_run)

    cli.main(
        [
            "--app",
            "cli_fixture_run_timing",
            "run",
            "--poll-interval",
            "1",
            "--long-poll-timeout",
            "2",
            "--job-lease-seconds",
            "3",
        ]
    )

    mock_run.assert_called_once_with(
        poll_interval_seconds=1, long_poll_timeout_seconds=2, job_lease_seconds=3
    )


def test_main_requires_command(isolated_sys_path):
    _write_module(
        isolated_sys_path,
        "cli_fixture_no_command",
        'from soarca_fin import Fin\n\nfin = Fin("http://example.test")\n',
    )
    with pytest.raises(SystemExit):
        cli.main(["--app", "cli_fixture_no_command"])
