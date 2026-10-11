#!/usr/bin/env python3
"""Never overwrite a release; advance stable only after round-trip verification."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from build_registry import REPO
from install_tools import sha256


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def publish(release):
    if not re.fullmatch(r"v\d+\.\d+\.\d+", release):
        raise ValueError("Invalid release")
    index = json.loads(Path("dist/stable.json").read_text())
    if index["release"] != release:
        raise ValueError("Catalog release mismatch")
    # Refuse both published versions and unfinished drafts, including reruns.
    existing = json.loads(run("gh", "api", f"repos/{REPO}/releases", "--paginate", "--slurp"))
    if any(r["tag_name"] == release for page in existing for r in page):
        raise ValueError("Release exists; create a new version instead of overwriting")
    run("gh", "release", "create", release, "--repo", REPO, "--verify-tag", "--draft", "--title", release,
        "--notes", "固定上游版本的工具与 Skill。两种 Linux 架构已完成摘要、许可证、包结构与帮助/版本命令验证。更新说明见 tools.json 和 skills.json。")
    assets = sorted(p for p in Path("dist").iterdir() if p.is_file())
    run("gh", "release", "upload", release, "--repo", REPO, *(str(p) for p in assets))
    with tempfile.TemporaryDirectory() as temp:
        run("gh", "release", "download", release, "--repo", REPO, "--dir", temp)
        downloaded = {p.name: p for p in Path(temp).iterdir()}
        if set(downloaded) != {p.name for p in assets}:
            raise ValueError("Uploaded asset set mismatch; stable unchanged")
        for p in assets:
            if sha256(p) != sha256(downloaded[p.name]):
                raise ValueError("Uploaded digest mismatch; stable unchanged")
    run("gh", "release", "edit", release, "--repo", REPO, "--draft=false", "--latest")
    run("git", "fetch", "origin", "main")
    run("git", "checkout", "-B", "registry-channel", "origin/main")
    Path("channels").mkdir(exist_ok=True)
    shutil.copyfile("dist/stable.json", "channels/stable.json")
    run("git", "config", "user.name", "github-actions[bot]")
    run("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    run("git", "add", "channels/stable.json")
    run("git", "commit", "-m", f"发布稳定索引 {release}")
    # No force push. A concurrent main update leaves the previous index intact.
    run("git", "push", "origin", "HEAD:main")
    print(f"Published {release}; all assets verified; stable updated")


if __name__ == "__main__":
    publish(sys.argv[1])
