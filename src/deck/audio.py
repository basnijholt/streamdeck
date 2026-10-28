"""Audio device name matching, shared by the PC (views) and the Mac executor."""


def matches(device: str, needle: str) -> bool:
    """Case-insensitive substring test. An empty needle matches nothing."""
    return bool(needle) and needle.casefold() in device.casefold()


def preferred_input(available: list[str], prefs: list[str]) -> str | None:
    """The available device matching the earliest preference, by its real name.

    Devices that match no preference (such as AirPods) are never returned.
    """
    for pref in prefs:
        for device in available:
            if matches(device, pref):
                return device
    return None
