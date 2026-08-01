import pytest
from alios_cli.main import main


def test_doctor_reports_runtime(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor"]) == 0
    assert "AliOS runtime: available" in capsys.readouterr().out
