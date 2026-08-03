"""Environment lookups that honour the pre-rename spelling.

The project was renamed Hermies -> Hermix. A live hub has HERMIES_* in its
systemd unit and a live agent has it in ~/.hermes/.env, so a rename that
silently drops the database path or the admin password would take the whole
network down on deploy. The new spelling always wins; the old one is only
consulted when the new name is absent (an explicitly empty value is a real
setting, not a miss).
"""
import os


def env(name, default=None):
    val = os.environ.get(name)
    if val is None and name.startswith("HERMIX_"):
        val = os.environ.get(name.replace("HERMIX_", "HERMIES_", 1))
    return default if val is None else val
