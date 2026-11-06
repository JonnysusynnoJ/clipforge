from __future__ import annotations

import shutil

import pytest
from typer.testing import CliRunner

from clipforge.cli import app
from clipforge.core import db, doctor
from clipforge.core.settings import Settings

runner = CliRunner()


def test_doctor_reports_uninitialized_db(settings: Settings) -> None:
    result = doctor.check_database(settings)
    assert result.status is doctor.Status.FAIL
    assert "db upgrade" in result.detail


def test_doctor_db_ok_after_upgrade(settings: Settings) -> None:
    settings.ensure_dirs()
    db.upgrade(settings.database_url)
    assert doctor.check_database(settings).status is doctor.Status.OK


def test_missing_ffmpeg_is_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    [result] = doctor.check_ffmpeg()
    assert result.status is doctor.Status.FAIL


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_has_required_filters() -> None:
    results = {r.name: r for r in doctor.check_ffmpeg()}
    assert results["ffmpeg filter 'loudnorm'"].status is doctor.Status.OK


def test_missing_service_account_file_is_fail(settings: Settings) -> None:
    s = settings.model_copy(update={"google_service_account_file": settings.paths.data_dir / "x"})
    sa = doctor.check_secrets(s)[0]
    assert sa.status is doctor.Status.FAIL


def test_cli_doctor_exit_code_reflects_failures() -> None:
    # fresh dir: database not initialized -> at least one FAIL -> exit 1
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "database" in result.output

    upgrade = runner.invoke(app, ["db", "upgrade"])
    assert upgrade.exit_code == 0, upgrade.output
    result = runner.invoke(app, ["doctor"])
    assert "migrations at head" in result.output
