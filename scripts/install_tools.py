#!/usr/bin/env python3
"""Install pinned tools into an offline Artemis library; never execute downloads."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MAX_DOWNLOAD = 256 * 1024 * 1024
MAX_EXTRACT = 512 * 1024 * 1024
NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
SHA256 = re.compile(r"[a-f0-9]{64}\Z")


def target_platform(value=None):
    value = value or (platform.system().lower() + "/" + platform.machine().lower())
    parts = value.lower().split("/")
    if len(parts) != 2:
        raise ValueError("平台格式应为 linux/amd64 或 linux/arm64")
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(parts[1], parts[1])
    result = parts[0] + "/" + arch
    if result not in ("linux/amd64", "linux/arm64"):
        raise ValueError("工具基线暂不支持 " + result + "；可用 --platform 为 Linux 部署准备文件")
    return result


def relative_path(value):
    if not isinstance(value, str) or not value or any(c in value for c in "\\:%"):
        raise ValueError("非法相对路径: " + repr(value))
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ("", ".", "..") for p in value.split("/")):
        raise ValueError("非法相对路径: " + value)
    if any(ord(c) < 32 for c in value):
        raise ValueError("路径含控制字符")
    return path


def library_path(root, value):
    parts = relative_path(value).parts
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("工具库路径不能是链接: " + str(path))
    if os.path.commonpath((str(root), str(path.resolve()))) != str(root):
        raise ValueError("工具库路径越界")
    return path


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_plan(lock_file, names, target):
    lock = json.loads(Path(lock_file).read_text(encoding="utf-8"))
    if not isinstance(lock, dict) or lock.get("version") != 1 or not isinstance(lock.get("tools"), list):
        raise ValueError("下载锁文件格式无效")
    catalog = {}
    for tool in lock["tools"]:
        if not isinstance(tool, dict):
            raise ValueError("下载锁文件工具条目必须为对象")
        name = tool.get("name", "")
        if not NAME.fullmatch(name) or name in catalog:
            raise ValueError("工具名无效或重复: " + name)
        catalog[name] = tool
    selected = list(catalog) if names == "baseline" else names.split(",")
    if not selected or len(set(selected)) != len(selected):
        raise ValueError("工具列表不能为空或重复")
    plan = []
    for name in selected:
        if name not in catalog:
            raise ValueError("未知工具: " + name)
        tool = catalog[name]
        asset = tool.get("assets", {}).get(target)
        if not asset:
            raise ValueError(name + " 没有锁定的 " + target + " 资产；请显式选择该平台支持的工具")
        if not tool.get("version") or not SHA256.fullmatch(asset.get("sha256", "")):
            raise ValueError(name + " 缺少固定版本或有效 SHA-256")
        url = urllib.parse.urlsplit(asset["url"])
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError(name + " 下载地址必须为无凭据的 HTTPS")
        if asset.get("format") not in ("zip", "tar.gz", "binary"):
            raise ValueError(name + " 归档格式不支持")
        relative_path(asset["member"])
        plan.append((tool, asset))
    return plan


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise ValueError("拒绝非 HTTPS 下载重定向")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(asset, destination):
    opener = urllib.request.build_opener(HTTPSRedirect())
    request = urllib.request.Request(asset["url"], headers={"User-Agent": "Artemis-tool-installer/1"})
    with opener.open(request, timeout=60) as response, destination.open("wb") as stream:
        total = 0
        for block in iter(lambda: response.read(1024 * 1024), b""):
            total += len(block)
            if total > MAX_DOWNLOAD:
                raise ValueError("下载超过 256 MiB 限制")
            stream.write(block)


def extract(asset, archive, destination, name):
    """Read only selected ordinary files; never use extractall or follow links."""
    destination.mkdir()
    binary = destination / name
    if asset["format"] == "binary":
        shutil.copyfile(archive, binary)
    else:
        is_zip = asset["format"] == "zip"
        opener = zipfile.ZipFile if is_zip else lambda p: tarfile.open(p, "r:gz")
        with opener(archive) as bundle:
            members = bundle.infolist() if is_zip else bundle.getmembers()
            if len(members) > 10000:
                raise ValueError("归档文件数超过限制")
            total, seen = 0, set()
            for member in members:
                raw = member.filename if is_zip else member.name
                # Tar producers commonly prefix members with './'.
                if raw.startswith("./"):
                    raw = raw[2:]
                raw = raw.rstrip("/")
                if not raw:
                    continue
                path = relative_path(raw)
                if raw.casefold() in seen:
                    raise ValueError("归档含重复路径: " + raw)
                seen.add(raw.casefold())
                if is_zip:
                    mode = member.external_attr >> 16
                    if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                        raise ValueError("归档包含链接或特殊文件: " + raw)
                    is_dir, size = member.is_dir(), member.file_size
                else:
                    if not (member.isfile() or member.isdir()):
                        raise ValueError("归档包含链接或特殊文件: " + raw)
                    is_dir, size = member.isdir(), member.size
                total += size
                if total > MAX_EXTRACT:
                    raise ValueError("归档解压大小超过限制")
                if is_dir:
                    continue
                if raw == asset["member"]:
                    out = binary
                elif path.name.upper().startswith(("LICENSE", "NOTICE", "COPYING")):
                    out = destination / "licenses" / Path(*path.parts)
                    out.parent.mkdir(parents=True, exist_ok=True)
                else:
                    continue
                source = bundle.open(member) if is_zip else bundle.extractfile(member)
                with source, out.open("xb") as stream:
                    shutil.copyfileobj(source, stream)
        if not binary.is_file():
            raise ValueError("归档缺少指定可执行文件: " + asset["member"])
    binary.chmod(0o755)
    return binary


def validate_binary(path, target):
    with path.open("rb") as stream:
        header = stream.read(20)
    machine = 62 if target == "linux/amd64" else 183
    if (len(header) < 20 or header[:6] != b"\x7fELF\x02\x01"
            or int.from_bytes(header[18:20], "little") != machine):
        raise ValueError("可执行文件不是目标平台的 64 位 ELF: " + target)


def atomic_write(path, data):
    fd, temp = tempfile.mkstemp(prefix=".install-write-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, 0o644)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def read_manifest(path):
    before = path.read_bytes() if path.exists() else None
    manifest = json.loads(before) if before is not None else {"version": "1", "sources": [], "tools": []}
    if (not isinstance(manifest, dict) or not isinstance(manifest.get("tools"), list)
            or not isinstance(manifest.get("sources", []), list)):
        raise ValueError("已有 manifest 格式无效，拒绝覆盖")
    names, keys = set(), set()
    for entry in manifest["tools"]:
        if not isinstance(entry, dict):
            raise ValueError("已有 manifest 工具条目必须为对象")
        name = entry.get("name")
        key = entry.get("key") or name
        if not isinstance(name, str) or name in names or key in keys:
            raise ValueError("已有 manifest 工具名称或 key 无效/重复")
        names.add(name)
        keys.add(key)
    return before, manifest


@contextmanager
def install_lock(root):
    path = library_path(root, ".install-tools.lock")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError("已有工具安装锁；请等安装结束，异常退出后确认无安装进程再移除 " + str(path)) from None
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        path.unlink()


def snapshot_manifest(root, before):
    if before is None:
        return
    snapshots = library_path(root, ".snapshots")
    snapshots.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    ident = now.strftime("%Y%m%dT%H%M%S.%f") + "-install-" + uuid.uuid4().hex[:8]
    folder = snapshots / ident
    folder.mkdir()
    (folder / "manifest.json").write_bytes(before)
    (folder / "meta.json").write_bytes(json_bytes({"id": ident, "created_at": now.isoformat(),
        "reason": "install-tools", "items": ["manifest.json"], "bytes": len(before)}))
    atomic_write(library_path(root, "manifest.json.bak"), before)
    # Same five-snapshot retention as the server; never traverse linked directories.
    dirs = sorted((p for p in snapshots.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    for old in dirs[5:]:
        safe = library_path(root, ".snapshots/" + old.name)
        shutil.rmtree(safe)


def install(plan, root, target, fetch=download):
    root = Path(root)
    if root.is_symlink():
        raise ValueError("工具库根目录不能是链接")
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    with install_lock(root):
        manifest_path = library_path(root, "manifest.json")
        before, manifest = read_manifest(manifest_path)
        existing = {e["name"]: e for e in manifest["tools"]}
        updated = dict(existing)
        with tempfile.TemporaryDirectory(prefix=".install-stage-", dir=str(root)) as temp:
            stage = Path(temp)
            for tool, asset in plan:
                name = tool["name"]
                old = existing.get(name)
                prefix = "baseline/" + name + "/" + asset["sha256"] + "/"
                if (old and old.get("path", "").startswith(prefix)
                        and old.get("version") == tool["version"] and old.get("platform") == target):
                    binary = library_path(root, old["path"])
                    if binary.is_file() and sha256(binary) == old.get("sha256"):
                        validate_binary(binary, target)
                        if os.name != "nt" and not os.access(binary, os.X_OK):
                            raise ValueError(name + " 缺少执行权限，请恢复权限后重试")
                        print("[tools] 保留 " + name + " " + tool["version"])
                        continue
                print("[tools] 下载并校验 " + name + " " + tool["version"], flush=True)
                archive = stage / (name + ".download")
                fetch(asset, archive)
                if sha256(archive) != asset["sha256"]:
                    raise ValueError(name + " 下载 SHA-256 不符，未更新清单")
                folder = stage / name
                binary = extract(asset, archive, folder, name)
                validate_binary(binary, target)
                digest = sha256(binary)
                relative = prefix + digest
                destination = library_path(root, relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    installed = library_path(root, relative + "/" + name)
                    if not installed.is_file() or sha256(installed) != digest:
                        raise ValueError(name + " 已有内容目录损坏，请先恢复备份或选择新工具库目录")
                else:
                    os.rename(folder, destination)
                entry = dict(old) if old else {"name": name, "key": name, "enabled": False, "source": "uploaded"}
                entry.update(version=tool["version"], purpose=tool["purpose"], platform=target,
                             path=relative + "/" + name, kind="binary", runtime="", entry="",
                             category="arsenal", sha256=digest, download_sha256=asset["sha256"],
                             upstream=tool["upstream"], release=tool["release"])
                updated[name] = entry
            manifest["tools"] = list(updated.values())
            keys = [e.get("key") or re.sub(r"[^a-z0-9_]", "_", e["name"].lower()) for e in manifest["tools"]]
            if len(set(keys)) != len(keys):
                raise ValueError("新条目与已有工具 key 冲突，未更新清单")
            if before is not None and manifest == json.loads(before):
                print("[tools] 清单无需更新")
                return manifest
            if (manifest_path.read_bytes() if manifest_path.exists() else None) != before:
                raise ValueError("安装期间 manifest 已变化，请停止 Artemis 后重试")
            snapshot_manifest(root, before)
            manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
            atomic_write(manifest_path, json_bytes(manifest))
        print("[tools] 已保存 " + str(manifest_path) + "；在工具库页面导入并设置 Agent 绑定后启用")
        return manifest


def main():
    parser = argparse.ArgumentParser(description="离线安装 Artemis 基线工具；请先停止使用该目录的 Artemis。")
    parser.add_argument("--tools", default="baseline", help="baseline 或逗号分隔工具名")
    parser.add_argument("--platform", help="默认宿主平台；可指定 linux/amd64 或 linux/arm64")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "tools")
    parser.add_argument("--lock-file", type=Path, default=ROOT / "packaging" / "tools-lock.json")
    parser.add_argument("--check", action="store_true", help="仅检查选择与下载锁，不下载或写入")
    args = parser.parse_args()
    try:
        target = target_platform(args.platform)
        plan = load_plan(args.lock_file, args.tools, target)
        if args.check:
            print("[tools] 已确认 " + target + ": " + ", ".join(t["name"] for t, _ in plan))
        else:
            install(plan, args.output, target)
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, zipfile.BadZipFile) as error:
        print("[tools] 安装失败: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
