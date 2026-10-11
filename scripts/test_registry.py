import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from build_registry import ROOT, pack, tree_hash
import install_tools as installer


class RegistryTests(unittest.TestCase):
    def test_platform_inventory_and_licenses(self):
        lock = json.loads((ROOT / "packaging/tools-lock.json").read_text(encoding="utf-8"))
        self.assertEqual(len(lock["tools"]), 8)
        for platform, count in [("linux/amd64", 8), ("linux/arm64", 7)]:
            names = [t["name"] for t in lock["tools"] if platform in t["assets"]]
            self.assertEqual(len(names), count)
            installer.load_plan(ROOT / "packaging/tools-lock.json", ",".join(names), platform)
        for tool in lock["tools"]:
            self.assertEqual(installer.sha256(ROOT / tool["license_file"]), tool["license_sha256"])
            if tool["license"].startswith("GPL"):
                self.assertRegex(tool["source_archive"]["sha256"], r"^[a-f0-9]{64}$")

    def test_deterministic_package_and_tree_digest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            root.mkdir()
            (root / "x").write_bytes(b"x")
            (root / "LICENSE").write_bytes(b"license")
            expected = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in [("LICENSE", b"license"), ("x", b"x")])
            self.assertEqual(tree_hash(root), hashlib.sha256(expected.encode()).hexdigest())
            first, second = Path(temp) / "first.zip", Path(temp) / "second.zip"
            pack(root, first)
            pack(root, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as bundle:
                self.assertEqual(bundle.namelist(), ["LICENSE", "x"])
            (root / "x").write_bytes(b"edited")
            self.assertNotEqual(tree_hash(root), hashlib.sha256(expected.encode()).hexdigest())

    def test_bad_architecture_and_paths(self):
        for path in ["../escape", "/etc/passwd", "x\\y", "C:/x", "x/../y"]:
            with self.assertRaises(ValueError):
                installer.relative_path(path)
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / "binary"
            p.write_bytes(b"not-an-executable")
            with self.assertRaises(ValueError):
                installer.validate_binary(p, "linux/amd64")


if __name__ == "__main__":
    unittest.main()
