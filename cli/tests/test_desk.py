import subprocess

import pytest

from voxxy.commands import desk


class FakeSystemd:
    def __init__(self):
        self.calls = []
        self.enabled = self.active = False

    def __call__(self, argv):
        self.calls.append(argv[2:])
        verb = argv[2]
        if verb == "enable":
            self.enabled = self.active = True
        elif verb == "disable":
            self.enabled = self.active = False
        out = ""
        if verb == "is-enabled":
            out = "enabled" if self.enabled else "disabled"
        elif verb == "is-active":
            out = "active" if self.active else "inactive"
        return subprocess.CompletedProcess(argv, 0, out + "\n", "")


def test_on_writes_the_unit_with_the_baked_key_and_starts_it(tmp_path):
    sd = FakeSystemd()
    state = desk.turn_on("big-chungus", "https://vox.example", run=sd, home=tmp_path,
                         binary="/home/u/.local/bin/voxxy", platform="linux")
    text = desk.unit_path(tmp_path).read_text()
    assert "ExecStart=/home/u/.local/bin/voxxy listen --key big-chungus" in text
    assert "Environment=VOX_URL=https://vox.example" in text
    assert state == {"unit": desk.UNIT_NAME, "installed": True, "enabled": True,
                     "active": True, "key": "big-chungus"}
    assert ["daemon-reload"] in sd.calls and ["enable", "--now", desk.UNIT_NAME] in sd.calls


def test_on_again_with_the_same_unit_skips_the_reload(tmp_path):
    sd = FakeSystemd()
    desk.turn_on("k", "https://v", run=sd, home=tmp_path, binary="/b", platform="linux")
    sd.calls.clear()
    desk.turn_on("k", "https://v", run=sd, home=tmp_path, binary="/b", platform="linux")
    assert ["daemon-reload"] not in sd.calls


def test_off_disables_but_keeps_the_unit(tmp_path):
    sd = FakeSystemd()
    desk.turn_on("k", "https://v", run=sd, home=tmp_path, binary="/b", platform="linux")
    state = desk.turn_off(run=sd, home=tmp_path)
    assert (state["installed"], state["enabled"], state["active"]) == (True, False, False)


def test_off_with_no_unit_is_a_no_op(tmp_path):
    sd = FakeSystemd()
    assert desk.turn_off(run=sd, home=tmp_path)["installed"] is False
    assert sd.calls == []


def test_on_refuses_on_a_mac(tmp_path):
    with pytest.raises(desk.DeskError, match="HeyMa.app"):
        desk.turn_on("k", "https://v", run=FakeSystemd(), home=tmp_path, binary="/b", platform="darwin")


def test_a_failed_enable_is_an_error(tmp_path):
    def fail(argv):
        return subprocess.CompletedProcess(argv, 1, "", "Failed to connect to bus")
    with pytest.raises(desk.DeskError, match="Failed to connect to bus"):
        desk.turn_on("k", "https://v", run=fail, home=tmp_path, binary="/b", platform="linux")
