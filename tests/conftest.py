import pytest

from whichsdk import cli


@pytest.fixture(autouse=True)
def docker_is_up_and_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    # No test may reach a real docker, so by default it answers and no container is running.
    monkeypatch.setattr(cli, "check_docker", lambda: None)
    monkeypatch.setattr(cli, "running_containers", lambda: set())
