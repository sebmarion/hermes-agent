"""The launch profile is valid when explicitly selected by Desktop."""

import pytest

from hermes_cli import profiles
from hermes_constants import get_hermes_home
from tui_gateway import server


@pytest.fixture
def profile_homes(tmp_path, monkeypatch):
    launch = tmp_path / "launch"
    other = tmp_path / "other"
    launch.mkdir()
    other.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setattr(server, "_hermes_home", launch)
    monkeypatch.setattr(
        profiles,
        "get_profile_dir",
        lambda name: {
            "default": launch,
            "launch-name": launch,
            "other": other,
        }.get(name, tmp_path / "missing" / name),
    )
    return launch, other


@pytest.mark.parametrize("profile", ["default", "launch-name", " default "])
def test_explicit_launch_profile_is_accepted(profile_homes, profile):
    launch, _ = profile_homes
    handler = server._profile_scoped(lambda rid, params: get_hermes_home())
    assert handler(1, {"profile": profile}) == launch
    assert server._require_profile_home(profile) is None


def test_other_profile_is_scoped_and_then_released(profile_homes):
    launch, other = profile_homes
    handler = server._profile_scoped(lambda rid, params: get_hermes_home())
    assert handler(1, {"profile": "other"}) == other
    assert get_hermes_home() == launch


def test_unknown_profile_is_still_rejected(profile_homes):
    handler = server._profile_scoped(lambda rid, params: get_hermes_home())
    with pytest.raises(ValueError, match="Unknown Hermes profile"):
        handler(1, {"profile": "missing"})
