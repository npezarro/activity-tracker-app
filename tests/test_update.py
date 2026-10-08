from activitytracker import update


def test_version_compare():
    assert update.is_newer("0.2.0", "0.1.9")
    assert not update.is_newer("v0.1.0", "0.1.0")
    assert update.parse("1.2") == (1, 2, 0)


def test_asset_names_are_stable():
    assert update.asset_name("installer") == "ActivityTracker-Setup-x64.exe"
    assert update.asset_name("portable") == "ActivityTracker-windows-x64.zip"
    assert update.asset_name("mac").startswith("ActivityTracker-macos-")
