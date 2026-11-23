import pytest
import typer

from voxxy import client as client_mod
from voxxy import config as config_mod
from voxxy.commands import desk, setup


class Health:
    def __init__(self, status="ok"):
        self.status = status


def factory(health="ok", voices=None, error=None):
    class Fake:
        def __init__(self, base, api_key=client_mod._USE_ENV):
            self.base = base

        def healthz(self):
            if isinstance(health, Exception):
                raise health
            return Health(health)

        def list_voices(self):
            if error:
                raise error
            return voices or ["rick"]
    return Fake


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(desk, "local_state", lambda *a, **k: {"active": False, "key": None})
    monkeypatch.delenv("VOX_URL", raising=False)
    monkeypatch.delenv("VOX_API_KEY_REF", raising=False)
    monkeypatch.setattr(client_mod, "_REF_CACHE", {})
    return tmp_path


def test_writes_the_ref_and_proves_the_key(host, monkeypatch):
    monkeypatch.setenv("VOX_API_KEY", "k")
    report = setup.run_setup("https://vox.example/", "op://V/item/credential", None, client_factory=factory())
    cfg = config_mod.load_config()
    assert (cfg.default_url, cfg.api_key_ref) == ("https://vox.example", "op://V/item/credential")
    assert (report["exit"], report["key"]) == (0, "ok")


def test_no_key_is_degraded_not_failed(host, monkeypatch):
    monkeypatch.delenv("VOX_API_KEY", raising=False)
    report = setup.run_setup(None, None, None, client_factory=factory())
    assert (report["exit"], report["key"]) == (2, "missing")


def test_a_rejected_key_fails(host, monkeypatch):
    monkeypatch.setenv("VOX_API_KEY", "wrong")
    report = setup.run_setup(None, None, None,
                             client_factory=factory(error=client_mod.VoxUnauthorized("401")))
    assert (report["exit"], report["key"]) == (3, "rejected")


def test_an_unreachable_server_fails_fast(host, monkeypatch):
    report = setup.run_setup(None, None, None,
                             client_factory=factory(health=client_mod.VoxUnreachable("down")))
    assert report["exit"] == 3 and report["steps"][-1]["name"] == "server"


def test_a_raw_key_is_refused(host):
    with pytest.raises(typer.BadParameter):
        setup.run_setup(None, "vox_abc123", None, client_factory=factory())
