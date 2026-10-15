"""Environment variables for credentials (same as the MCP server's envvars.py)."""

from __future__ import annotations

import os
import sys


def get_env(name: str) -> str | None:
    """Environment variable, also reading the user's saved variables on Windows.

    Values saved during /coverity-setup are visible without restarting VS Code.
    """
    value = os.environ.get(name)
    if value:
        return value
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                stored, _ = winreg.QueryValueEx(key, name)
                return str(stored) or None
        except OSError:
            return None
    return None


def can_set_user_env() -> bool:
    """Saving a variable for later sessions is only done on Windows (the PCs this plugin is for)."""
    return sys.platform == "win32"


def set_user_env(name: str, value: str) -> None:
    """Save a user environment variable on Windows, like [Environment]::SetEnvironmentVariable(.., 'User').

    Programs started later see it; this process sees it at once.
    """
    if not can_set_user_env():
        raise OSError("環境変数を保存できるのは Windows だけです")
    import ctypes
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    os.environ[name] = value
    # Tell running programs (Explorer, new terminals) that the variables changed.
    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result))
