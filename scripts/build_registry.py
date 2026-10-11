#!/usr/bin/env python3
"""Build immutable ZIP packages and two digest-pinned release catalogs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import zipfile

import install_tools as installer

ROOT = Path(__file__).resolve().parents[1]
REPO = "x0rz4/Artemis-registry"


def tree_hash(root):
    lines = []
    for p in sorted(root.rglob("*")):
        if p.is_symlink():
            raise ValueError("Package contains symlink")
        if p.is_file():
            lines.append((p.relative_to(root).as_posix(), installer.sha256(p)))
    raw = "".join(f"{digest}  {name}\n" for name, digest in sorted(lines)).encode()
    return hashlib.sha256(raw).hexdigest()


def pack(root, destination):
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError("Package contains symlink")
            if not path.is_file():
                continue
            info = zipfile.ZipInfo(path.relative_to(root).as_posix(), (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100755 if os.access(path, os.X_OK) else 0o100644) << 16
            with path.open("rb") as source, bundle.open(info, "w") as target:
                shutil.copyfileobj(source, target)
    with zipfile.ZipFile(destination) as bundle:
        if bundle.testzip() is not None:
            raise ValueError("ZIP validation failed")


def cached_download(asset):
    cache = ROOT / ".cache"
    cache.mkdir(exist_ok=True)
    path = cache / asset["sha256"]
    if not path.exists():
        temp = path.with_suffix(".part")
        try:
            installer.download(asset, temp)
            if installer.sha256(temp) != asset["sha256"]:
                raise ValueError("Download checksum mismatch")
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
    if installer.sha256(path) != asset["sha256"]:
        raise ValueError("Cache checksum mismatch")
    return path


def write_json(path, value):
    path.write_bytes(installer.json_bytes(value))


def build(release, out, installed_cache=None):
    if not re.fullmatch(r"v\d+\.\d+\.\d+", release):
        raise ValueError("Release must be vMAJOR.MINOR.PATCH")
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError("Output directory must be empty (published releases are immutable)")
    base = f"https://github.com/{REPO}/releases/download/{release}/"
    tools = json.loads((ROOT / "packaging/tools-lock.json").read_text(encoding="utf-8"))["tools"]
    catalogs = {"tools": {"release": release, "items": []}, "skills": {"release": release, "items": []}}
    for tool in tools:
        license_path = ROOT / tool["license_file"]
        if installer.sha256(license_path) != tool["license_sha256"]:
            raise ValueError("License checksum mismatch: " + tool["name"])
        item = {k: tool[k] for k in ("name", "version", "license", "changes")}
        item.update(description=tool["purpose"], upstream=tool["upstream"], assets=[])
        if source := tool.get("source_archive"):
            shutil.copyfile(cached_download(source), out / source["filename"])
            item["source_archive"] = {"url": base + source["filename"], "sha256": source["sha256"]}
        for platform, asset in tool["assets"].items():
            installer.load_plan(ROOT / "packaging/tools-lock.json", tool["name"], platform)
            with tempfile.TemporaryDirectory(prefix="registry-build-") as temp:
                root = Path(temp) / tool["name"]
                if installed_cache:
                    lib = installed_cache / platform.split("/")[1]
                    manifest = json.loads((lib / "manifest.json").read_text(encoding="utf-8"))
                    entry = next(e for e in manifest["tools"] if e["name"] == tool["name"])
                    source = lib / entry["path"]
                    if entry.get("download_sha256") != asset["sha256"] or installer.sha256(source) != entry["sha256"]:
                        raise ValueError("Installed cache is not the pinned asset")
                    shutil.copytree(source.parent, root)
                    binary = root / tool["name"]
                else:
                    binary = installer.extract(asset, cached_download(asset), root, tool["name"])
                installer.validate_binary(binary, platform)
                shutil.copyfile(license_path, root / "LICENSE")
                (root / "SOURCE.txt").write_text(f"{tool['upstream']}\n{tool['release']}\nUpstream asset SHA-256: {asset['sha256']}\n", encoding="utf-8")
                filename = f"tool-{tool['name']}-{tool['version']}-{platform.replace('/', '-')}.zip"
                pack(root, out / filename)
                item["assets"].append({"platform": platform, "url": base + filename, "sha256": installer.sha256(out / filename), "content_sha256": tree_hash(root), "entry": tool["name"]})
        catalogs["tools"]["items"].append(item)
    lock = json.loads((ROOT / "packaging/skills-lock.json").read_text(encoding="utf-8"))
    for item in lock["skills"]:
        root = ROOT / "skills" / item["name"]
        text = (root / "SKILL.md").read_text(encoding="utf-8")
        if not text.startswith("---\n") or "\n---\n" not in text[4:] or f"name: {item['name']}\n" not in text:
            raise ValueError("Invalid SKILL.md frontmatter: " + item["name"])
        if installer.sha256(root / "LICENSE") != item["license_sha256"]:
            raise ValueError("Skill license mismatch")
        filename = f"skill-{item['name']}-{item['version']}.zip"
        pack(root, out / filename)
        catalogs["skills"]["items"].append({**item, "assets": [{"platform": "any", "url": base + filename, "sha256": installer.sha256(out / filename), "content_sha256": tree_hash(root), "entry": "SKILL.md"}]})
    index = {"release": release}
    for kind, catalog in catalogs.items():
        filename = kind + ".json"
        write_json(out / filename, catalog)
        index[kind] = {"url": base + filename, "sha256": installer.sha256(out / filename)}
    write_json(out / "stable.json", index)
    (out / "SHA256SUMS").write_text("".join(f"{installer.sha256(p)}  {p.name}\n" for p in sorted(out.iterdir()) if p.is_file()), encoding="utf-8")


def smoke(out, platform):
    """Only help/version flags; never supply a target or start a scan."""
    catalog = json.loads((out / "tools.json").read_text(encoding="utf-8"))
    for item in catalog["items"]:
        for asset in item["assets"]:
            if asset["platform"] != platform:
                continue
            package = out / asset["url"].rsplit("/", 1)[1]
            if installer.sha256(package) != asset["sha256"]:
                raise ValueError("Package digest mismatch")
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with zipfile.ZipFile(package) as bundle:
                    for entry in bundle.infolist():
                        installer.relative_path(entry.filename)
                    bundle.extractall(root)
                if tree_hash(root) != asset["content_sha256"]:
                    raise ValueError("Content digest mismatch")
                binary = root / asset["entry"]
                binary.chmod(0o755)
                installer.validate_binary(binary, platform)
                flag = "-version" if item["name"] in {"httpx", "naabu", "subfinder", "katana", "nuclei"} else "-h"
                result = subprocess.run([str(binary), flag], cwd=root, capture_output=True, timeout=30)
                output = result.stdout + result.stderr
                if result.returncode not in (0, 1, 2) or not output or any(marker in output.lower() for marker in (b"exec format error", b"error while loading shared libraries", b"segmentation fault")):
                    raise ValueError(f"Help/version failed: {item['name']} ({result.returncode}): {output[:300]!r}")
                print(f"{platform}: {item['name']} {flag}: OK", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--release")
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--installed-cache", type=Path)
    parser.add_argument("--smoke", choices=("linux/amd64", "linux/arm64"))
    args = parser.parse_args()
    if args.smoke:
        smoke(args.output, args.smoke)
    else:
        build(args.release, args.output, args.installed_cache)
