"""API keys live in the OS credential store (Windows Credential Manager / macOS
Keychain), not in the portable data folder. If no store is available, fall back to
data/secrets.json and say so in the UI."""
import json
import logging
import os

from . import paths

SERVICE = "ActivityTracker"
log = logging.getLogger(__name__)


def _fallback_path():
    return os.path.join(paths.data_dir(), "secrets.json")


def _read_fallback():
    try:
        with open(_fallback_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def backend_name():
    try:
        import keyring

        kr = type(keyring.get_keyring())
        name, where = kr.__name__, kr.__module__.lower()  # the "no backend" class is fail.Keyring
        return None if any(w in (name.lower() + " " + where) for w in ("fail", "null")) else name
    except Exception:
        return None


def get(name):
    if backend_name():
        try:
            import keyring

            value = keyring.get_password(SERVICE, name)
            if value:
                return value
        except Exception:
            log.warning("keyring read failed for %s", name, exc_info=True)
    return _read_fallback().get(name) or ""


def set(name, value):  # noqa: A001  (module-level API: keystore.set)
    value = (value or "").strip()
    if backend_name():
        try:
            import keyring

            if value:
                keyring.set_password(SERVICE, name, value)
            else:
                try:
                    keyring.delete_password(SERVICE, name)
                except Exception:
                    pass
            return "keychain"
        except Exception:
            log.warning("keyring write failed for %s; using data/secrets.json", name, exc_info=True)
    data = _read_fallback()
    if value:
        data[name] = value
    else:
        data.pop(name, None)
    with open(_fallback_path(), "w", encoding="utf-8") as f:
        json.dump(data, f)
    return "file"
