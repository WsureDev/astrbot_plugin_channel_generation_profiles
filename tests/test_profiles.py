import json
import tempfile
import unittest
from pathlib import Path

from core.channel import Channel, normalize_platform
from core.profiles import ProfileStore


class ProfileRoutingTests(unittest.TestCase):
    def test_platform_aliases_are_canonical(self):
        self.assertEqual(normalize_platform("aiocqhttp"), "qq")
        self.assertEqual(normalize_platform("napcat"), "qq")
        self.assertEqual(normalize_platform("telegram_bot"), "telegram")

    def test_qq_and_telegram_use_independent_state(self):
        config = {
            "profiles": json.dumps({
                "qq": {"platforms": ["qq"], "comfyui": {}},
                "telegram": {"platforms": ["telegram"], "comfyui": {}},
                "default": {"platforms": [], "comfyui": {}},
            })
        }
        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore(config, Path(directory))
            qq_name = store.profile_name(Channel("aiocqhttp"))
            tg_name = store.profile_name(Channel("telegram"))
            self.assertEqual((qq_name, tg_name), ("qq", "telegram"))
            store.update(qq_name, "comfyui", {"workflow": "5"})
            store.update(tg_name, "comfyui", {"workflow": "11"})
            self.assertEqual(store.get("qq")["comfyui"]["workflow"], "5")
            self.assertEqual(store.get("telegram")["comfyui"]["workflow"], "11")


if __name__ == "__main__":
    unittest.main()
