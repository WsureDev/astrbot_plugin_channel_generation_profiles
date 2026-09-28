from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .channel import Channel


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class ProfileStore:
    """Generic profile storage. It knows nothing about any target plugin."""

    def __init__(self, config: dict[str, Any], data_dir: Path):
        self._profiles = copy.deepcopy(config.get("profiles") or {})
        self._bindings = self._make_bindings(self._profiles)
        self._path = data_dir / "profiles.json"
        self._state: dict[str, dict[str, Any]] = {}
        self._load()

    @staticmethod
    def _make_bindings(profiles: dict[str, Any]) -> dict[str, str]:
        result: dict[str, str] = {}
        for name, profile in profiles.items():
            for platform in (profile or {}).get("platforms", []) or []:
                result[str(platform).strip().lower()] = str(name)
        return result

    def profile_name(self, channel: Channel) -> str:
        return self._bindings.get(channel.platform, self._bindings.get("*", "default"))

    def get(self, profile_name: str) -> dict[str, Any]:
        return deep_merge(
            copy.deepcopy(self._profiles.get(profile_name) or {}),
            copy.deepcopy(self._state.get(profile_name) or {}),
        )

    def for_channel(self, channel: Channel) -> tuple[str, dict[str, Any]]:
        name = self.profile_name(channel)
        return name, self.get(name)

    def update(self, profile_name: str, section: str, values: dict[str, Any]) -> None:
        self._state.setdefault(profile_name, {}).setdefault(section, {}).update(values)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self._path)

    def names(self) -> list[str]:
        return sorted(self._profiles)

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._state = raw
        except (OSError, json.JSONDecodeError):
            self._state = {}
