#!/usr/bin/env python3
"""Build APT + Pacman indexes for one Imprint GitHub Release.

This tree is a Cloudflare Pages site. Package *bytes* stay on the Imprint
GitHub Release; this repo only commits indexes, source snippets, and
per-file ``_redirects`` rules.

  ubuntu/dists/{stable,ubuntu22.04,ubuntu24.04,ubuntu26.04}/
  ubuntu/repo.sources + ubuntu/repo.list
  pacman/{x86_64,aarch64}/repo.db
  _redirects                 one 302 per .deb / .pkg.tar.*

Pool paths are virtual (not stored in git):

  /ubuntu/pool/github/<owner>/<repo>/<tag>/<asset>
    → https://github.com/<owner>/<repo>/releases/download/<tag>/<asset>

  /pacman/<arch>/<asset>
    → https://github.com/<owner>/<repo>/releases/download/<tag>/<asset>

Usage:
  .github/scripts/update-index.py --apply --github-repo googolmo/imprint
  .github/scripts/update-index.py --apply --tag v0.1.4 \\
      --github-repo googolmo/imprint --assets-dir /tmp/assets
  .github/scripts/update-index.py --self-test
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PAGES_BASE = os.environ.get("REPO_BASE_URL", "https://repo-cr4.pages.dev").rstrip("/")

DEB_NAME = re.compile(
    r"^imprint_(?P<version>[^_]+)_(?P<suite>ubuntu[\d.]+)_(?P<cpu>x86_64|amd64|arm64|aarch64)\.deb$"
)
PKG_NAME = re.compile(
    r"^imprint_(?P<version>[^_]+)_archlinux_(?P<cpu>x86_64|amd64|arm64|aarch64)\.pkg\.tar\.(?:zst|xz)$"
)

CPU_TO_DEB_ARCH = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "arm64": "arm64",
    "aarch64": "arm64",
}
CPU_TO_PACMAN_ARCH = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "arm64": "aarch64",
    "aarch64": "aarch64",
}

ALL_SUITES = ("stable", "ubuntu22.04", "ubuntu24.04", "ubuntu26.04")
ALL_DEB_ARCHES = ("amd64", "arm64")
ALL_PACMAN_ARCHES = ("x86_64", "aarch64")

# Suites written for each Ubuntu .deb tag. `stable` is the default APT suite
# (ubuntu22.04, widest glibc compatibility).
SUITE_ALIASES = {
    "ubuntu22.04": ("ubuntu22.04", "stable"),
    "ubuntu24.04": ("ubuntu24.04",),
    "ubuntu26.04": ("ubuntu26.04",),
}

PACKAGES_FIELD_ORDER = (
    "Package",
    "Version",
    "Architecture",
    "Maintainer",
    "Installed-Size",
    "Depends",
    "Recommends",
    "Suggests",
    "Section",
    "Priority",
    "Homepage",
    "Description",
    "Filename",
    "Size",
    "MD5sum",
    "SHA1",
    "SHA256",
)

PKGINFO_MULTI = {
    "depend",
    "optdepend",
    "replaces",
    "conflict",
    "provides",
    "license",
    "group",
    "makedepend",
    "checkdepend",
}

SITE_URL_FILES = (
    "index.html",
    "ubuntu/index.html",
    "pacman/index.html",
)


def release_base_url(github_repo: str, tag: str) -> str:
    repo = github_repo.strip().strip("/")
    tag = tag.strip()
    if not repo or "/" not in repo:
        raise SystemExit(f"invalid GitHub repo {github_repo!r}")
    if not tag:
        raise SystemExit("tag is required")
    if tag == "latest" or tag.endswith("/download") or "latest" in tag.split("/"):
        raise SystemExit(f"refusing tag {tag!r}; resolve latest to a version tag first")
    return f"https://github.com/{repo}/releases/download/{tag}"


def version_from_tag(tag: str) -> str:
    tag = tag.strip()
    return tag[1:] if tag.startswith("v") else tag


def parse_deb_filename(name: str) -> tuple[str, str, str]:
    match = DEB_NAME.match(name)
    if not match:
        raise SystemExit(
            f"unexpected .deb name {name!r}; expected "
            "imprint_<ver>_ubuntuXX.YY_<cpu>.deb"
        )
    return match.group("version"), match.group("suite"), match.group("cpu")


def parse_pkg_filename(name: str) -> tuple[str, str]:
    match = PKG_NAME.match(name)
    if not match:
        raise SystemExit(
            f"unexpected pacman package name {name!r}; expected "
            "imprint_<ver>_archlinux_<cpu>.pkg.tar.zst"
        )
    return match.group("version"), match.group("cpu")


def pool_filename(github_repo: str, tag: str, asset: str) -> str:
    return f"pool/github/{github_repo.strip('/')}/{tag.strip()}/{asset}"


def pool_redirect_src(github_repo: str, tag: str, asset: str) -> str:
    return f"/ubuntu/{pool_filename(github_repo, tag, asset)}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def hash_file(path: Path) -> tuple[int, str, str, str]:
    data = path.read_bytes()
    return len(data), md5_bytes(data), sha1_bytes(data), sha256_bytes(data)


def parse_control(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    key: str | None = None
    for line in text.splitlines():
        if key and (line.startswith(" ") or line.startswith("\t")):
            fields[key] += "\n" + line
            continue
        if not line.strip():
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        fields[key] = value.strip()
    return fields


def format_field(key: str, value: str) -> str:
    parts = value.split("\n")
    head = f"{key}: {parts[0]}"
    if len(parts) == 1:
        return head
    return head + "\n" + "\n".join(parts[1:])


def format_stanza(fields: dict[str, str]) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for key in PACKAGES_FIELD_ORDER:
        if key in fields:
            lines.append(format_field(key, fields[key]))
            seen.add(key)
    for key, value in fields.items():
        if key not in seen:
            lines.append(format_field(key, value))
    return "\n".join(lines) + "\n"


def gzip_bytes(data: bytes) -> bytes:
    return gzip.compress(data, mtime=0, compresslevel=9)


def format_release_hash_line(digest: str, size: int, name: str) -> str:
    return f" {digest} {size:>16} {name}"


def render_release(
    *,
    suite: str,
    files: dict[str, tuple[int, str, str, str]],
    date: datetime | None = None,
) -> str:
    """files: relative path → (size, md5, sha1, sha256)."""
    when = date or datetime.now(timezone.utc)
    arches = sorted(
        {
            name.split("binary-", 1)[1].split("/", 1)[0]
            for name in files
            if "/binary-" in name
        }
    ) or list(ALL_DEB_ARCHES)
    lines = [
        "Origin: MOSUMI",
        "Label: MOSUMI",
        f"Suite: {suite}",
        f"Codename: {suite}",
        f"Date: {format_datetime(when, usegmt=True)}",
        f"Architectures: {' '.join(arches)}",
        "Components: main",
        "Description: MOSUMI Linux package repository",
        "Acquire-By-Hash: no",
        "MD5Sum:",
    ]
    for name in sorted(files):
        size, md5, _, _ = files[name]
        lines.append(format_release_hash_line(md5, size, name))
    lines.append("SHA1:")
    for name in sorted(files):
        size, _, sha1, _ = files[name]
        lines.append(format_release_hash_line(sha1, size, name))
    lines.append("SHA256:")
    for name in sorted(files):
        size, _, _, sha256 = files[name]
        lines.append(format_release_hash_line(sha256, size, name))
    return "\n".join(lines) + "\n"


def render_packages(stanzas: list[dict[str, str]]) -> str:
    body = "\n".join(format_stanza(s) for s in stanzas)
    if not body.endswith("\n"):
        body += "\n"
    return body


def render_redirects(
    *,
    github_repo: str,
    tag: str,
    deb_assets: list[str],
    pkg_assets: list[tuple[str, str]],
) -> str:
    """pkg_assets: list of (pacman_arch, filename). One 302 per file."""
    release = release_base_url(github_repo, tag)
    lines = [
        "# Generated by .github/scripts/update-index.py — do not edit by hand.",
        "# Cloudflare Pages: one redirect per package file → GitHub Release asset.",
        "",
    ]
    for name in sorted(deb_assets):
        src = pool_redirect_src(github_repo, tag, name)
        dest = f"{release}/{name}"
        lines.append(f"{src} {dest} 302")
    for arch, name in sorted(pkg_assets):
        lines.append(f"/pacman/{arch}/{name} {release}/{name} 302")
    lines.append("")
    return "\n".join(lines)


def render_pacman_conf(github_repo: str, tag: str) -> str:
    release = release_base_url(github_repo, tag)
    return (
        "# Imprint Pacman repository.\n"
        "# Install:\n"
        f"#   sudo curl -fsSL {PAGES_BASE}/pacman/repo.conf \\\n"
        "#     -o /etc/pacman.d/repo\n"
        "#   echo 'Include = /etc/pacman.d/repo' | sudo tee -a /etc/pacman.conf\n"
        "#   sudo pacman -Sy imprint\n"
        "#\n"
        "# First Server hosts repo.db (this Cloudflare Pages tree). Package\n"
        "# files 302 from /pacman/$arch/<file> to the Imprint GitHub Release.\n"
        "[repo]\n"
        "SigLevel = Optional TrustAll\n"
        f"Server = {PAGES_BASE}/pacman/$arch\n"
        f"Server = {release}\n"
    )


def render_repo_sources() -> str:
    return (
        "# Linux package repository (DEB822)\n"
        "# Install the keyring first:\n"
        f"#   sudo curl -fsSL {PAGES_BASE}/keys/repo.gpg \\\n"
        "#     -o /usr/share/keyrings/repo-archive-keyring.gpg\n"
        "# Suite `stable` is the Ubuntu 22.04 / Debian 12+ .deb (widest glibc range).\n"
        "# Use Suites: ubuntu24.04 or ubuntu26.04 for those newer-glibc builds instead.\n"
        "Types: deb\n"
        f"URIs: {PAGES_BASE}/ubuntu\n"
        "Suites: stable\n"
        "Components: main\n"
        "Architectures: amd64 arm64\n"
        "Signed-By: /usr/share/keyrings/repo-archive-keyring.gpg\n"
    )


def render_repo_list() -> str:
    return (
        "# Linux package repository (one-line sources.list format)\n"
        "# Install the keyring first:\n"
        f"#   sudo curl -fsSL {PAGES_BASE}/keys/repo.gpg \\\n"
        "#     -o /usr/share/keyrings/repo-archive-keyring.gpg\n"
        f"deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/repo-archive-keyring.gpg] {PAGES_BASE}/ubuntu stable main\n"
    )


def render_readme(github_repo: str, tag: str) -> str:
    release = release_base_url(github_repo, tag)
    return f"""# Linux package repository

APT (Debian / Ubuntu) and Pacman (Arch Linux) **indexes** for Imprint, served
from Cloudflare Pages. Package files are **not** stored in this git tree:
Cloudflare 302s each `.deb` and Pacman `.pkg.tar.*` to the matching
[GitHub Release](https://github.com/{github_repo}/releases/tag/{tag}) asset.

**Base URL:** {PAGES_BASE}/

Current Imprint release: `{tag}` → `{release}/`

Connect this repository to **Cloudflare Pages** (build command empty, output
directory `/`) so `_redirects` is honoured. GitHub Pages cannot 302 `/pool`.

## Public key

| File | Format |
| --- | --- |
| [keys/repo.asc]({PAGES_BASE}/keys/repo.asc) | ASCII-armored |
| [keys/repo.gpg]({PAGES_BASE}/keys/repo.gpg) | Binary keyring |

- **Fingerprint:** `91FD A448 7920 8693 204E  90EE 9DF4 2B70 54F1 CB5B`
- **Key ID:** `9DF42B7054F1CB5B`

`update-index` signs `ubuntu/dists/*/InRelease` with the `GPG_PRIVATE_KEY`
Actions secret (must match `keys/repo.asc`).

## Debian / Ubuntu (APT)

```bash
sudo mkdir -p /usr/share/keyrings
sudo curl -fsSL {PAGES_BASE}/keys/repo.gpg \\
  -o /usr/share/keyrings/repo-archive-keyring.gpg
sudo chmod 644 /usr/share/keyrings/repo-archive-keyring.gpg
sudo curl -fsSL {PAGES_BASE}/ubuntu/repo.sources \\
  -o /etc/apt/sources.list.d/repo.sources
sudo apt update
sudo apt install imprint
```

`ubuntu/repo.sources` uses suite `stable` (the Ubuntu 22.04 / Debian 12+
`.deb`). Suites `ubuntu24.04` and `ubuntu26.04` exist for the newer-glibc
builds (amd64 and arm64). `Filename` in `Packages` is a per-file pool path
under `ubuntu/pool/github/`; Cloudflare 302s that exact file to `{release}/`.

## Arch Linux (Pacman)

```bash
sudo curl -fsSL {PAGES_BASE}/pacman/repo.conf \\
  -o /etc/pacman.d/repo
echo -e '\\nInclude = /etc/pacman.d/repo' | sudo tee -a /etc/pacman.conf
sudo pacman -Sy imprint
```

`repo.db` is under `pacman/x86_64/` and `pacman/aarch64/`. Each
`.pkg.tar.zst` / `.pkg.tar.xz` is 302'd from `/pacman/$arch/<file>` to
`{release}/`.

## Updating the index

Imprint's Release workflow dispatches this repository's `update-index` action
with the new tag. Manual run (empty tag = latest):

```bash
gh workflow run update-index.yml -R googolmo/repo -f github_repo={github_repo} -f tag={tag}
```

Secrets on this repository:

| Secret | Role |
| --- | --- |
| `GPG_PRIVATE_KEY` | OpenPGP secret matching `keys/repo.asc`; signs APT `InRelease` |
| `GPG_PASSPHRASE` | Optional passphrase for that key |

## Layout

```
.
├── _redirects                 one Cloudflare 302 per .deb / .pkg.tar.*
├── keys/
├── ubuntu/
│   ├── repo.sources
│   ├── repo.list
│   ├── dists/{{stable,ubuntu22.04,ubuntu24.04,ubuntu26.04}}/
│   │   └── main/{{binary-amd64,binary-arm64,source}}/
│   └── pool/github/...        virtual; not stored, 302 per file
└── pacman/
    ├── repo.conf
    ├── x86_64/                repo.db only
    └── aarch64/
```
"""


def zstd_decompress(data: bytes) -> bytes:
    try:
        import zstandard

        return zstandard.ZstdDecompressor().decompress(data)
    except ImportError:
        pass
    zstd = shutil.which("zstd")
    if not zstd:
        raise SystemExit("zstd is required to read zstd-compressed packages")
    proc = subprocess.run(
        [zstd, "-d", "-c"],
        input=data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")
        raise SystemExit(f"zstd -d failed: {err}")
    return proc.stdout


def open_tar_bytes(name: str, payload: bytes) -> tarfile.TarFile:
    lower = name.lower()
    if lower.endswith(".zst") or lower.endswith(".zstd"):
        payload = zstd_decompress(payload)
        return tarfile.open(fileobj=io.BytesIO(payload), mode="r:")
    if lower.endswith(".xz"):
        return tarfile.open(fileobj=io.BytesIO(payload), mode="r:xz")
    if lower.endswith(".gz"):
        return tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz")
    if lower.endswith(".bz2"):
        return tarfile.open(fileobj=io.BytesIO(payload), mode="r:bz2")
    return tarfile.open(fileobj=io.BytesIO(payload), mode="r:")


def parse_ar(data: bytes) -> list[tuple[str, bytes]]:
    magic = b"!<arch>\n"
    if not data.startswith(magic):
        raise ValueError("not a Unix ar archive")
    members: list[tuple[str, bytes]] = []
    off = len(magic)
    n = len(data)
    while off + 60 <= n:
        header = data[off : off + 60]
        if header.strip(b"\0") == b"":
            break
        name = header[0:16].decode("ascii", "replace").strip()
        size_s = header[48:58].decode("ascii", "replace").strip()
        if not size_s:
            break
        size = int(size_s)
        if header[58:60] != b"`\n":
            raise ValueError(f"invalid ar header magic {header[58:60]!r}")
        off += 60
        payload = data[off : off + size]
        off += size
        if size % 2 == 1:
            off += 1
        if name.startswith("#1/"):
            namelen = int(name[3:])
            name = payload[:namelen].decode("ascii", "replace").rstrip("\0")
            payload = payload[namelen:]
        members.append((name.rstrip("/"), payload))
    return members


def tar_member_bytes(tar: tarfile.TarFile, names: tuple[str, ...]) -> bytes | None:
    wanted = {n.lstrip("./") for n in names}
    for info in tar.getmembers():
        if not info.isfile():
            continue
        if info.name.lstrip("./") in wanted:
            handle = tar.extractfile(info)
            if handle is None:
                return None
            return handle.read()
    return None


def deb_control_from_ar(path: Path) -> dict[str, str]:
    members = parse_ar(path.read_bytes())
    control_payload: bytes | None = None
    control_name = ""
    for name, payload in members:
        base = name.split("/")[-1]
        if base.startswith("control.tar"):
            control_name = base
            control_payload = payload
            break
    if control_payload is None:
        raise ValueError(f"{path.name}: no control.tar in ar archive")
    with open_tar_bytes(control_name, control_payload) as tar:
        raw = tar_member_bytes(tar, ("control",))
    if raw is None:
        raise ValueError(f"{path.name}: control.tar has no control file")
    fields = parse_control(raw.decode("utf-8"))
    if "Package" not in fields or "Version" not in fields:
        raise ValueError(f"{path.name}: control is missing Package/Version")
    return fields


def dpkg_control(path: Path) -> dict[str, str]:
    try:
        return deb_control_from_ar(path)
    except (ValueError, tarfile.TarError, OSError):
        pass
    try:
        out = subprocess.check_output(
            ["dpkg-deb", "-f", str(path)],
            text=True,
            stderr=subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        raise SystemExit(
            f"could not read {path.name}: install dpkg-deb or fix the .deb parser"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"dpkg-deb -f {path.name} failed:\n{exc.output}") from exc
    fields = parse_control(out)
    if "Package" not in fields or "Version" not in fields:
        raise SystemExit(f"{path.name}: control is missing Package/Version")
    return fields


def parse_pkginfo(text: str) -> dict[str, str | list[str]]:
    fields: dict[str, str | list[str]] = {}
    for line in text.splitlines():
        if not line or line.startswith("#") or " = " not in line:
            continue
        key, _, value = line.partition(" = ")
        key = key.strip()
        value = value.strip()
        if key in PKGINFO_MULTI:
            current = fields.setdefault(key, [])
            assert isinstance(current, list)
            current.append(value)
        else:
            fields[key] = value
    return fields


def extract_pkginfo(path: Path) -> dict[str, str | list[str]]:
    data = path.read_bytes()
    name = path.name
    if name.endswith(".pkg.tar.zst"):
        data = zstd_decompress(data)
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:")
    elif name.endswith(".pkg.tar.xz"):
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:xz")
    else:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:")
    try:
        raw = tar_member_bytes(tar, (".PKGINFO", "PKGINFO"))
    finally:
        tar.close()
    if raw is None:
        raise ValueError(f"{path.name}: missing .PKGINFO")
    return parse_pkginfo(raw.decode("utf-8"))


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def github_api(path: str) -> dict:
    if shutil.which("gh"):
        env = os.environ.copy()
        env["NO_COLOR"] = "1"
        env["CLICOLOR_FORCE"] = "0"
        raw = subprocess.check_output(["gh", "api", path], text=True, env=env)
        raw = _ANSI.sub("", raw)
        return json.loads(raw)
    url = f"https://api.github.com/{path}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "mosumi-linux-repo",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def resolve_tag(github_repo: str, tag: str | None) -> str:
    github_repo = github_repo.strip().strip("/")
    tag = (tag or "").strip()
    if tag and tag != "latest":
        return tag
    data = github_api(f"repos/{github_repo}/releases/latest")
    resolved = str(data.get("tag_name") or "").strip()
    if not resolved:
        raise SystemExit(f"could not resolve latest release for {github_repo}")
    print(f"resolved latest → {resolved} ({github_repo})")
    return resolved


def is_index_asset(name: str) -> bool:
    if name.endswith(".sig"):
        return False
    if name.startswith("imprint_") and name.endswith(".deb"):
        return True
    if "_archlinux_" not in name:
        return False
    return name.endswith(".pkg.tar.zst") or name.endswith(".pkg.tar.xz")


def download_assets(github_repo: str, tag: str, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if shutil.which("gh"):
        cmd = [
            "gh",
            "release",
            "download",
            tag,
            "--repo",
            github_repo,
            "--dir",
            str(dest),
            "--pattern",
            "imprint_*.deb",
            "--pattern",
            "imprint_*_archlinux_*.pkg.tar.zst",
            "--pattern",
            "imprint_*_archlinux_*.pkg.tar.xz",
            "--clobber",
        ]
        subprocess.check_call(cmd)
        return
    release = github_api(f"repos/{github_repo}/releases/tags/{tag}")
    assets = release.get("assets") or []
    found = 0
    for asset in assets:
        name = str(asset.get("name") or "")
        if not is_index_asset(name):
            continue
        url = asset.get("browser_download_url")
        if not url:
            continue
        print(f"downloading {name}")
        urllib.request.urlretrieve(url, dest / name)
        found += 1
    if found == 0:
        raise SystemExit(f"no .deb / .pkg.tar.* assets on {github_repo} {tag}")


def collect_debs(assets_dir: Path, *, github_repo: str, tag: str) -> list[dict[str, str]]:
    stanzas: list[dict[str, str]] = []
    debs = sorted(assets_dir.glob("imprint_*.deb"))
    if not debs:
        raise SystemExit(f"no imprint_*.deb files in {assets_dir}")
    expected_ver = version_from_tag(tag)
    for path in debs:
        file_ver, suite, cpu = parse_deb_filename(path.name)
        if file_ver != expected_ver:
            raise SystemExit(
                f"{path.name}: version {file_ver} does not match tag {tag}"
            )
        if suite not in SUITE_ALIASES:
            raise SystemExit(f"{path.name}: unsupported ubuntu suite {suite}")
        size, md5, sha1, sha256 = hash_file(path)
        fields = dpkg_control(path)
        deb_arch = CPU_TO_DEB_ARCH[cpu]
        ctrl_arch = fields.get("Architecture", "")
        if ctrl_arch and ctrl_arch != deb_arch:
            raise SystemExit(
                f"{path.name}: control Architecture {ctrl_arch!r} != {deb_arch!r}"
            )
        fields["Architecture"] = deb_arch
        fields["Filename"] = pool_filename(github_repo, tag, path.name)
        fields["Size"] = str(size)
        fields["MD5sum"] = md5
        fields["SHA1"] = sha1
        fields["SHA256"] = sha256
        fields["_suite"] = suite
        stanzas.append(fields)
    return stanzas


def write_hashed_text(
    path: Path, body: bytes
) -> tuple[int, str, str, str]:
    gz = gzip_bytes(body)
    path.write_bytes(body)
    path.with_name(path.name + ".gz").write_bytes(gz)
    return (
        len(body),
        md5_bytes(body),
        sha1_bytes(body),
        sha256_bytes(body),
    )


def write_suite(dist_root: Path, suite: str, stanzas: list[dict[str, str]]) -> None:
    by_arch: dict[str, list[dict[str, str]]] = defaultdict(list)
    for stanza in stanzas:
        public = {k: v for k, v in stanza.items() if not k.startswith("_")}
        by_arch[stanza["Architecture"]].append(public)
    hashed: dict[str, tuple[int, str, str, str]] = {}
    for arch in ALL_DEB_ARCHES:
        binary = dist_root / suite / "main" / f"binary-{arch}"
        binary.mkdir(parents=True, exist_ok=True)
        arch_stanzas = by_arch.get(arch, [])
        body = render_packages(arch_stanzas).encode("utf-8") if arch_stanzas else b""
        hashed[f"main/binary-{arch}/Packages"] = write_hashed_text(
            binary / "Packages", body
        )
        gz = gzip_bytes(body)
        hashed[f"main/binary-{arch}/Packages.gz"] = (
            len(gz),
            md5_bytes(gz),
            sha1_bytes(gz),
            sha256_bytes(gz),
        )
        count = len(by_arch.get(arch, []))
        print(f"wrote ubuntu/dists/{suite}/main/binary-{arch}/Packages ({count} package(s))")

    source = dist_root / suite / "main" / "source"
    source.mkdir(parents=True, exist_ok=True)
    sources_body = b""
    hashed["main/source/Sources"] = write_hashed_text(source / "Sources", sources_body)
    gz = gzip_bytes(sources_body)
    hashed["main/source/Sources.gz"] = (
        len(gz),
        md5_bytes(gz),
        sha1_bytes(gz),
        sha256_bytes(gz),
    )
    print(f"wrote ubuntu/dists/{suite}/main/source/Sources")

    release_text = render_release(suite=suite, files=hashed)
    release_path = dist_root / suite / "Release"
    release_path.write_text(release_text, encoding="utf-8")
    print(f"wrote ubuntu/dists/{suite}/Release")


def gpg_public_fingerprint(asc_path: Path) -> str | None:
    try:
        out = subprocess.check_output(
            ["gpg", "--show-keys", "--with-colons", str(asc_path)],
            text=True,
            stderr=subprocess.STDOUT,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    for line in out.splitlines():
        if line.startswith("fpr:"):
            parts = line.split(":")
            if len(parts) > 9 and parts[9]:
                return parts[9]
    return None


def gpg_secret_fingerprints() -> set[str]:
    try:
        out = subprocess.check_output(
            ["gpg", "--list-secret-keys", "--with-colons"],
            text=True,
            stderr=subprocess.STDOUT,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return set()
    found: set[str] = set()
    for line in out.splitlines():
        if line.startswith("fpr:"):
            parts = line.split(":")
            if len(parts) > 9 and parts[9]:
                found.add(parts[9])
    return found


def can_sign(repo_dir: Path) -> bool:
    pub = gpg_public_fingerprint(repo_dir / "keys" / "repo.asc")
    if not pub:
        return False
    return pub in gpg_secret_fingerprints()


def gpg_sign_release(release_path: Path, *, passphrase: str | None) -> None:
    suite_dir = release_path.parent
    inrelease = suite_dir / "InRelease"
    detach = suite_dir / "Release.gpg"
    for leftover in (inrelease, detach):
        if leftover.exists():
            leftover.unlink()
    base = [
        "gpg",
        "--batch",
        "--yes",
        "--pinentry-mode",
        "loopback",
        "--digest-algo",
        "SHA256",
    ]
    pass_file: Path | None = None
    extra: list[str] = []
    env_file = os.environ.get("GPG_PASSPHRASE_FILE", "").strip()
    if env_file:
        extra += ["--passphrase-file", env_file]
    elif passphrase:
        handle = tempfile.NamedTemporaryFile("w", delete=False, prefix="gpg-pass-")
        handle.write(passphrase)
        handle.close()
        pass_file = Path(handle.name)
        os.chmod(pass_file, 0o600)
        extra += ["--passphrase-file", str(pass_file)]
    try:
        subprocess.check_call(
            base + extra + ["--clearsign", "-o", str(inrelease), str(release_path)]
        )
        subprocess.check_call(
            base + extra + ["--detach-sign", "--armor", "-o", str(detach), str(release_path)]
        )
    except FileNotFoundError as exc:
        raise SystemExit("gpg is required to sign APT InRelease") from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"gpg failed signing {release_path}") from exc
    finally:
        if pass_file is not None:
            pass_file.unlink(missing_ok=True)
    subprocess.check_call(["gpg", "--batch", "--verify", str(inrelease)])
    print(f"signed {inrelease}")


def clear_old_dists(dist_root: Path) -> None:
    if not dist_root.exists():
        return
    for child in dist_root.iterdir():
        if not child.is_dir():
            continue
        for path in child.rglob("*"):
            if path.is_file() and path.name != ".gitkeep":
                path.unlink()


def _desc_section(key: str, values: list[str]) -> list[str]:
    if not values:
        return []
    lines = [f"%{key}%"]
    lines.extend(values)
    lines.append("")
    return lines


def render_pacman_desc(
    *,
    filename: str,
    pkginfo: dict[str, str | list[str]],
    size: int,
    md5: str,
    sha256: str,
) -> str:
    def one(key: str, default: str = "") -> str:
        value = pkginfo.get(key, default)
        if isinstance(value, list):
            return value[0] if value else default
        return value or default

    def many(key: str) -> list[str]:
        value = pkginfo.get(key, [])
        if isinstance(value, list):
            return value
        return [value] if value else []

    name = one("pkgname", "imprint")
    version = one("pkgver")
    lines: list[str] = []
    lines += _desc_section("FILENAME", [filename])
    lines += _desc_section("NAME", [name])
    lines += _desc_section("BASE", [one("pkgbase", name)])
    lines += _desc_section("VERSION", [version])
    desc = one("pkgdesc")
    if desc:
        lines += _desc_section("DESC", [desc])
    lines += _desc_section("CSIZE", [str(size)])
    isize = one("size")
    if isize:
        lines += _desc_section("ISIZE", [isize])
    lines += _desc_section("MD5SUM", [md5])
    lines += _desc_section("SHA256SUM", [sha256])
    url = one("url")
    if url:
        lines += _desc_section("URL", [url])
    lines += _desc_section("LICENSE", many("license"))
    lines += _desc_section("ARCH", [one("arch")])
    built = one("builddate")
    if built:
        lines += _desc_section("BUILDDATE", [built])
    packager = one("packager")
    if packager:
        lines += _desc_section("PACKAGER", [packager])
    lines += _desc_section("REPLACES", many("replaces"))
    lines += _desc_section("CONFLICTS", many("conflict"))
    lines += _desc_section("PROVIDES", many("provides"))
    lines += _desc_section("DEPENDS", many("depend"))
    lines += _desc_section("OPTDEPENDS", many("optdepend"))
    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    return text


def write_pacman_db(dest: Path, packages: list[tuple[str, str]]) -> None:
    """packages: list of (dirname, desc_text)."""
    dest.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for dirname, desc in packages:
            dir_info = tarfile.TarInfo(f"{dirname}/")
            dir_info.type = tarfile.DIRTYPE
            dir_info.mode = 0o755
            dir_info.mtime = 0
            tar.addfile(dir_info)
            payload = desc.encode("utf-8")
            info = tarfile.TarInfo(f"{dirname}/desc")
            info.size = len(payload)
            info.mode = 0o644
            info.mtime = 0
            tar.addfile(info, io.BytesIO(payload))
    data = buf.getvalue()
    (dest / "repo.db.tar.gz").write_bytes(data)
    (dest / "repo.db").write_bytes(data)
    for leftover in dest.glob("*.pkg.tar.*"):
        leftover.unlink()


def collect_pkg_assets(assets_dir: Path) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for path in sorted(assets_dir.glob("imprint_*_archlinux_*.pkg.tar.*")):
        if path.name.endswith(".sig"):
            continue
        if not (path.name.endswith(".pkg.tar.zst") or path.name.endswith(".pkg.tar.xz")):
            continue
        _, cpu = parse_pkg_filename(path.name)
        found.append((CPU_TO_PACMAN_ARCH[cpu], path))
    return found


def rewrite_site_urls(repo_dir: Path) -> None:
    old = "https://googolmo.github.io/repo"
    for rel in SITE_URL_FILES:
        path = repo_dir / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        new = text.replace(old, PAGES_BASE)
        if new != text:
            path.write_text(new, encoding="utf-8")
            print(f"updated {rel}")


def apply(
    repo_dir: Path,
    *,
    github_repo: str,
    tag: str | None,
    assets_dir: Path | None,
    passphrase: str | None,
    skip_sign: bool,
) -> None:
    github_repo = github_repo.strip().strip("/")
    resolved = resolve_tag(github_repo, tag)
    release_base_url(github_repo, resolved)

    tmp: tempfile.TemporaryDirectory[str] | None = None
    if assets_dir is None:
        tmp = tempfile.TemporaryDirectory(prefix="imprint-assets-")
        assets_dir = Path(tmp.name)
        print(f"downloading {github_repo} {resolved} assets → {assets_dir}")
        download_assets(github_repo, resolved, assets_dir)
        for path in sorted(assets_dir.iterdir()):
            print(f"  {path.name} ({path.stat().st_size} bytes)")

    try:
        stanzas = collect_debs(assets_dir, github_repo=github_repo, tag=resolved)
        dist_root = repo_dir / "ubuntu" / "dists"
        clear_old_dists(dist_root)
        by_suite: dict[str, list[dict[str, str]]] = {suite: [] for suite in ALL_SUITES}
        for stanza in stanzas:
            origin = stanza["_suite"]
            for suite in SUITE_ALIASES[origin]:
                by_suite[suite].append(stanza)
        sign = (not skip_sign) and can_sign(repo_dir)
        if skip_sign:
            print("skipping APT InRelease (--skip-sign)")
        elif not sign:
            print("skipping APT InRelease (no matching GPG_PRIVATE_KEY in the keyring)")
        for suite in ALL_SUITES:
            write_suite(dist_root, suite, by_suite[suite])
            if sign:
                gpg_sign_release(dist_root / suite / "Release", passphrase=passphrase)

        deb_names = [Path(s["Filename"]).name for s in stanzas]
        pkg_rows = collect_pkg_assets(assets_dir)
        if not pkg_rows:
            raise SystemExit(
                f"no imprint_*_archlinux_*.pkg.tar.zst/.xz files in {assets_dir}"
            )
        pkg_assets = [(arch, path.name) for arch, path in pkg_rows]

        redirects = repo_dir / "_redirects"
        redirects.write_text(
            render_redirects(
                github_repo=github_repo,
                tag=resolved,
                deb_assets=deb_names,
                pkg_assets=pkg_assets,
            ),
            encoding="utf-8",
        )
        print(f"wrote {redirects}")

        pacman_dir = repo_dir / "pacman"
        pacman_dir.mkdir(parents=True, exist_ok=True)
        conf = pacman_dir / "repo.conf"
        conf.write_text(render_pacman_conf(github_repo, resolved), encoding="utf-8")
        print(f"wrote {conf}")

        by_arch: dict[str, list[tuple[str, str]]] = {arch: [] for arch in ALL_PACMAN_ARCHES}
        for arch, path in pkg_rows:
            size, md5, _, sha256 = hash_file(path)
            try:
                pkginfo = extract_pkginfo(path)
            except Exception as exc:
                ver, cpu = parse_pkg_filename(path.name)
                print(f"warning: {path.name}: {exc}; using filename metadata")
                pkginfo = {
                    "pkgname": "imprint",
                    "pkgver": f"{ver}-1",
                    "arch": CPU_TO_PACMAN_ARCH[cpu],
                }
            name = pkginfo.get("pkgname") or "imprint"
            version = pkginfo.get("pkgver") or f"{version_from_tag(resolved)}-1"
            if isinstance(name, list):
                name = name[0]
            if isinstance(version, list):
                version = version[0]
            desc = render_pacman_desc(
                filename=path.name,
                pkginfo=pkginfo,
                size=size,
                md5=md5,
                sha256=sha256,
            )
            by_arch[arch].append((f"{name}-{version}", desc))
        for arch in ALL_PACMAN_ARCHES:
            write_pacman_db(pacman_dir / arch, by_arch[arch])
            print(f"wrote pacman/{arch}/repo.db ({len(by_arch[arch])} package(s))")

        sources = repo_dir / "ubuntu" / "repo.sources"
        sources.write_text(render_repo_sources(), encoding="utf-8")
        print(f"wrote {sources}")
        repo_list = repo_dir / "ubuntu" / "repo.list"
        repo_list.write_text(render_repo_list(), encoding="utf-8")
        print(f"wrote {repo_list}")

        readme = repo_dir / "README.md"
        readme.write_text(render_readme(github_repo, resolved), encoding="utf-8")
        print(f"wrote {readme}")
        rewrite_site_urls(repo_dir)
    finally:
        if tmp is not None:
            tmp.cleanup()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Write APT Packages, Cloudflare _redirects, and Pacman source files.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write indexes into --repo-dir from a GitHub Release (or --assets-dir)",
    )
    parser.add_argument("--repo-dir", type=Path, default=REPO_ROOT)
    parser.add_argument(
        "--assets-dir",
        type=Path,
        help="Directory of downloaded Release assets (downloaded automatically if omitted)",
    )
    parser.add_argument(
        "--tag",
        default="latest",
        help="GitHub Release tag (default: latest, resolved to the actual tag)",
    )
    parser.add_argument(
        "--github-repo",
        default="googolmo/imprint",
        help="owner/name of the Imprint repository",
    )
    parser.add_argument(
        "--skip-sign",
        action="store_true",
        help="Do not sign ubuntu/dists/*/InRelease even if a matching secret key exists",
    )
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args(argv)


def _ar_member(name: str, payload: bytes) -> bytes:
    name_f = name.encode("ascii")
    if len(name_f) > 16:
        raise ValueError("ar member name too long")
    header = (
        name_f.ljust(16)
        + b"0".ljust(12)
        + b"0".ljust(6)
        + b"0".ljust(6)
        + b"100644".ljust(8)
        + str(len(payload)).encode("ascii").ljust(10)
        + b"`\n"
    )
    data = header + payload
    if len(payload) % 2 == 1:
        data += b"\n"
    return data


def _self_test() -> None:
    repo = "googolmo/imprint"
    tag = "v0.1.4"
    base = "https://github.com/googolmo/imprint/releases/download/v0.1.4"
    if release_base_url(repo, tag) != base:
        raise SystemExit("release_base_url mismatch")
    try:
        release_base_url(repo, "latest")
    except SystemExit:
        pass
    else:
        raise SystemExit("should refuse latest")
    if version_from_tag(tag) != "0.1.4":
        raise SystemExit("version_from_tag failed")

    ver, suite, cpu = parse_deb_filename("imprint_0.1.4_ubuntu22.04_x86_64.deb")
    if (ver, suite, cpu) != ("0.1.4", "ubuntu22.04", "x86_64"):
        raise SystemExit("parse_deb_filename failed")
    try:
        parse_deb_filename("imprint_0.1.4_amd64.deb")
    except SystemExit:
        pass
    else:
        raise SystemExit("should reject untagged .deb names")

    filename = pool_filename(repo, tag, "imprint_0.1.4_ubuntu22.04_x86_64.deb")
    if filename != "pool/github/googolmo/imprint/v0.1.4/imprint_0.1.4_ubuntu22.04_x86_64.deb":
        raise SystemExit(f"pool_filename mismatch: {filename}")

    control = parse_control(
        "Package: imprint\n"
        "Version: 0.1.4\n"
        "Architecture: amd64\n"
        "Description: short\n"
        " long line\n"
    )
    if control["Description"] != "short\n long line":
        raise SystemExit(f"parse_control description: {control['Description']!r}")
    stanza = {
        "Package": "imprint",
        "Version": "0.1.4",
        "Architecture": "amd64",
        "Description": "short\n long line",
        "Filename": filename,
        "Size": "12",
        "SHA256": "ab",
    }
    body = render_packages([stanza])
    if "Filename: pool/github/googolmo/imprint/v0.1.4/" not in body:
        raise SystemExit("Packages missing pool Filename")
    if "Description: short\n long line\n" not in body:
        raise SystemExit(f"Packages description wrap failed: {body!r}")

    hashed = {
        "main/binary-amd64/Packages": (10, "m", "s1", "s256"),
        "main/binary-amd64/Packages.gz": (4, "mg", "s1g", "s256g"),
        "main/binary-arm64/Packages": (0, "z", "z1", "z256"),
        "main/source/Sources": (0, "src", "src1", "src256"),
    }
    release = render_release(
        suite="stable",
        files=hashed,
        date=datetime(2026, 9, 4, tzinfo=timezone.utc),
    )
    if "Suite: stable" not in release:
        raise SystemExit("Release missing suite")
    if "Architectures: amd64 arm64" not in release:
        raise SystemExit("Release missing architectures")
    if " s256               10 main/binary-amd64/Packages" not in release:
        raise SystemExit(f"Release SHA256 line mismatch:\n{release}")

    redirects = render_redirects(
        github_repo=repo,
        tag=tag,
        deb_assets=["imprint_0.1.4_ubuntu22.04_x86_64.deb"],
        pkg_assets=[("x86_64", "imprint_0.1.4_archlinux_x86_64.pkg.tar.zst")],
    )
    if "/ubuntu/pool/github/googolmo/imprint/v0.1.4/" not in redirects:
        raise SystemExit("redirects missing deb path")
    if f"{base}/imprint_0.1.4_ubuntu22.04_x86_64.deb 302" not in redirects:
        raise SystemExit("redirects missing GitHub dest")
    if "/pacman/x86_64/imprint_0.1.4_archlinux_x86_64.pkg.tar.zst" not in redirects:
        raise SystemExit("redirects missing pacman path")
    if "/releases/latest/" in redirects:
        raise SystemExit("redirects must not use latest")
    if ":splat" in redirects or "/:owner/" in redirects or "/* " in redirects:
        raise SystemExit("redirects must be per-file, not a directory splat")
    redirect_lines = [line for line in redirects.splitlines() if line.endswith(" 302")]
    if len(redirect_lines) != 2:
        raise SystemExit(f"expected two per-file redirects, got:\n{redirects}")

    conf = render_pacman_conf(repo, tag)
    if f"Server = {PAGES_BASE}/pacman/$arch\n" not in conf:
        raise SystemExit("pacman must keep db on Pages")
    if f"Server = {base}\n" not in conf:
        raise SystemExit("pacman must list the GitHub Release as package Server")
    if "/releases/latest/download/" in conf:
        raise SystemExit("must not use latest redirect")

    sources = render_repo_sources()
    if f"URIs: {PAGES_BASE}/ubuntu\n" not in sources:
        raise SystemExit("repo.sources URI mismatch")
    if "Architectures: amd64 arm64" not in sources:
        raise SystemExit("repo.sources missing architectures")
    if f"deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/repo-archive-keyring.gpg] {PAGES_BASE}/ubuntu stable main\n" not in render_repo_list():
        raise SystemExit("repo.list mismatch")

    control_text = (
        "Package: imprint\nVersion: 0.1.4\nArchitecture: amd64\nDescription: test\n"
    )
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w:gz") as tar:
        payload = control_text.encode("utf-8")
        info = tarfile.TarInfo("./control")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    deb = b"!<arch>\n" + _ar_member("debian-binary", b"2.0\n") + _ar_member(
        "control.tar.gz", tar_buf.getvalue()
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        deb_path = root / "imprint_0.1.4_ubuntu22.04_x86_64.deb"
        deb_path.write_bytes(deb)
        parsed = deb_control_from_ar(deb_path)
        if parsed["Package"] != "imprint" or parsed["Version"] != "0.1.4":
            raise SystemExit(f"deb_control_from_ar failed: {parsed}")

        dist = root / "ubuntu" / "dists"
        write_suite(dist, "ubuntu26.04", [])
        for arch in ALL_DEB_ARCHES:
            pkg = dist / "ubuntu26.04" / "main" / f"binary-{arch}" / "Packages"
            if not pkg.exists() or pkg.read_bytes() != b"":
                raise SystemExit(f"empty suite Packages missing for {arch}")
        sources_path = dist / "ubuntu26.04" / "main" / "source" / "Sources"
        if not sources_path.exists():
            raise SystemExit("Sources missing")
        rel = (dist / "ubuntu26.04" / "Release").read_text(encoding="utf-8")
        if "Architectures: amd64 arm64" not in rel:
            raise SystemExit("empty suite Release missing architectures")
        if "main/source/Sources" not in rel:
            raise SystemExit("Release missing Sources")

        write_pacman_db(
            root / "pacman" / "x86_64",
            [
                (
                    "imprint-0.1.4-1",
                    render_pacman_desc(
                        filename="imprint_0.1.4_archlinux_x86_64.pkg.tar.zst",
                        pkginfo={
                            "pkgname": "imprint",
                            "pkgver": "0.1.4-1",
                            "arch": "x86_64",
                            "pkgdesc": "Imprint",
                        },
                        size=12,
                        md5="m",
                        sha256="s",
                    ),
                )
            ],
        )
        db = root / "pacman" / "x86_64" / "repo.db"
        if not db.exists() or db.stat().st_size == 0:
            raise SystemExit("repo.db not written")
        with tarfile.open(db, mode="r:gz") as tar:
            names = tar.getnames()
        if "imprint-0.1.4-1/desc" not in names:
            raise SystemExit(f"repo.db missing desc: {names}")

        (root / "ubuntu" / "dists" / "stable").mkdir(parents=True, exist_ok=True)
        (root / "ubuntu" / "dists" / "stable" / ".gitkeep").write_text("")
        (root / "_redirects").write_text(redirects)
        (root / "pacman").mkdir(exist_ok=True)
        (root / "pacman" / "repo.conf").write_text(conf)
        if (root / "ubuntu" / "pool" / "main").exists():
            raise SystemExit("must not materialise pool/main debs")

    print("self-test ok")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        _self_test()
        return 0
    if not args.apply:
        print("nothing to do; pass --apply or --self-test", file=sys.stderr)
        return 2
    passphrase = os.environ.get("GPG_PASSPHRASE") or None
    if passphrase == "":
        passphrase = None
    apply(
        args.repo_dir,
        github_repo=args.github_repo.strip(),
        tag=args.tag,
        assets_dir=args.assets_dir,
        passphrase=passphrase,
        skip_sign=args.skip_sign,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
