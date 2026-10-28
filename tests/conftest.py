import pytest


@pytest.fixture(autouse=True)
def runtime_dir(tmp_path, monkeypatch):
    """Keep the daemon's default control socket out of the real `$XDG_RUNTIME_DIR`."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    return tmp_path
