from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .channel import Channel, normalize_platform


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
        raw_profiles = config.get("profiles", {})
        if raw_profiles is None or raw_profiles == "":
            raw_profiles = {}
        if isinstance(raw_profiles, str):
            raw_profiles = json.loads(raw_profiles)
        if not isinstance(raw_profiles, dict):
            raise ValueError("profiles must be a JSON object")
        for name, profile in raw_profiles.items():
            if not isinstance(name, str) or not isinstance(profile, dict):
                raise ValueError("each profile must be a named object")
            platforms = profile.get("platforms", [])
            if not isinstance(platforms, list) or any(not isinstance(p, str) for p in platforms):
                raise ValueError(f"profile {name}: platforms must be a list of strings")
        self._profiles = copy.deepcopy(raw_profiles)
        self._bindings = self._make_bindings(self._profiles)
        self._path = data_dir / "profiles.json"
        self._state: dict[str, dict[str, Any]] = {}
        self._load()

    @staticmethod
    def _make_bindings(profiles: dict[str, Any]) -> dict[str, str]:
        result: dict[str, str] = {}
        for name, profile in profiles.items():
            for platform in (profile or {}).get("platforms", []) or []:
                key = normalize_platform(platform)
                if key in result and result[key] != name:
                    raise ValueError(f"platform {key} is bound to multiple profiles")
                result[key] = name
        return result

    def profile_name(self, channel: Channel) -> str:
        platform = normalize_platform(channel.platform)
        if platform in self._bindings:
            return self._bindings[platform]
        # Known platform profiles remain isolated even when the user leaves
        # `platforms` empty in the UI. Unknown platforms use the explicit default.
        if platform in self._profiles:
            return platform
        return self._bindings.get("*", "default")

    def get(self, profile_name: str) -> dict[str, Any]:
        return deep_merge(
            copy.deepcopy(self._profiles.get(profile_name) or {}),
            copy.deepcopy(self._state.get(profile_name) or {}),
        )

    def for_channel(self, channel: Channel) -> tuple[str, dict[str, Any]]:
        name = self.profile_name(channel)
        return name, self.get(name)

    def update(self, profile_name: str, section: str, values: dict[str, Any]) -> None:
        candidate = copy.deepcopy(self._state)
        candidate.setdefault(profile_name, {}).setdefault(section, {}).update(copy.deepcopy(values))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, filename = tempfile.mkstemp(prefix=".profiles-", suffix=".tmp", dir=self._path.parent)
        temporary = Path(filename)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(candidate, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self._path)
        finally:
            temporary.unlink(missing_ok=True)
        self._state = candidate

    def names(self) -> list[str]:
        return sorted(self._profiles.keys() | self._state.keys())

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or any(not isinstance(v, dict) for v in raw.values()):
                raise ValueError("persisted profiles must be named objects")
            if any(not isinstance(section, dict) for profile in raw.values() for section in profile.values()):
                raise ValueError("persisted profile sections must be objects")
            self._state = raw
        except FileNotFoundError:
            self._state = {}
