#!/usr/bin/env python3
"""Build APT + Pacman indexes for one Imprint GitHub Release.

This tree is a Cloudflare Pages site. Package *bytes* stay on the Imprint
GitHub Release; this repo only commits indexes, source snippets, and
per-file ``_redirects`` rules. Each GitHub Release owns a fragment under
``ubuntu/_redirects.d`` / ``pacman/_redirects.d``; the root ``_redirects``
file is assembled from those fragments so updating one tag does not drop
older 302s.

  ubuntu/dists/{noble,resolute}/
  ubuntu/mosumi-repo.sources + ubuntu/mosumi-repo.list
  pacman/{x86_64,aarch64}/mosumi-repo.db + mosumi-repo.db.sig
  ubuntu/_redirects.d/<owner>/<repo>/<tag>/_redirects
  pacman/_redirects.d/<owner>/<repo>/<tag>/_redirects
  _redirects                 assembled Cloudflare 302s (do not edit)

Pool paths are virtual (not stored in git):

  /ubuntu/pool/github/<owner>/<repo>/<tag>/<asset>
    → https://github.com/<owner>/<repo>/releases/download/<tag>/<asset>

  /pacman/<arch>/<asset>
    → https://github.com/<owner>/<repo>/releases/download/<tag>/<asset>

Usage:
  .github/scripts/update-index.py --apply --gpg-private-key key.asc
  GPG_PRIVATE_KEY="$(cat key.asc)" .github/scripts/update-index.py --apply
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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PAGES_BASE = os.environ.get("REPO_BASE_URL", "https://repo-cr4.pages.dev").rstrip("/")
REPO_KEY_ID = "9DF42B7054F1CB5B"
REPO_KEY_FINGERPRINT_DISPLAY = "91FD A448 7920 8693 204E  90EE 9DF4 2B70 54F1 CB5B"
PACMAN_REPO_NAME = "mosumi-repo"
PACMAN_CONF_NAME = f"{PACMAN_REPO_NAME}.conf"
PACMAN_DB_NAME = f"{PACMAN_REPO_NAME}.db"
PACMAN_DB_TAR_NAME = f"{PACMAN_REPO_NAME}.db.tar.gz"
PACMAN_INCLUDE_PATH = f"/etc/pacman.d/{PACMAN_CONF_NAME}"

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

SUITE_BY_BUILD = {
    "ubuntu24.04": "noble",
    "ubuntu26.04": "resolute",
}
ALL_SUITES = ("noble", "resolute")
DEFAULT_SUITE = "noble"
ALL_DEB_ARCHES = ("amd64", "arm64")
ALL_PACMAN_ARCHES = ("x86_64", "aarch64")

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


def apt_suite_for_build(build: str) -> str:
    suite = SUITE_BY_BUILD.get(build)
    if suite is None:
        supported = ", ".join(SUITE_BY_BUILD)
        raise SystemExit(f"unsupported ubuntu build {build!r}; expected {supported}")
    return suite


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


# Cloudflare Pages only reads the root _redirects file (2,000 static rules).
CLOUDFLARE_STATIC_REDIRECT_LIMIT = 2000
REDIRECT_LINE = re.compile(
    r"^(?P<src>\S+)\s+(?P<dest>\S+)(?:\s+(?P<code>\d{3}))?\s*$"
)
GITHUB_ASSET_URL = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)"
    r"/releases/download/(?P<tag>[^/]+)/(?P<asset>[^/\s]+)$"
)
UBUNTU_POOL_SRC = re.compile(
    r"^/ubuntu/pool/github/(?P<owner>[^/]+)/(?P<repo>[^/]+)"
    r"/(?P<tag>[^/]+)/(?P<asset>[^/\s]+)$"
)
ROOT_REDIRECTS_HEADER = (
    "# Generated by .github/scripts/update-index.py — do not edit by hand.",
    "# Assembled from ubuntu/_redirects.d and pacman/_redirects.d.",
    "# Cloudflare Pages: one redirect per package file → GitHub Release asset.",
)


@dataclass(frozen=True)
class Redirect:
    src: str
    dest: str
    code: str = "302"

    def line(self) -> str:
        return f"{self.src} {self.dest} {self.code}"


def _safe_path_segment(value: str, what: str) -> str:
    value = value.strip()
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise SystemExit(f"invalid {what} {value!r}")
    return value


def split_github_repo(github_repo: str) -> tuple[str, str]:
    repo = github_repo.strip().strip("/")
    if repo.count("/") != 1:
        raise SystemExit(f"invalid GitHub repo {github_repo!r}")
    owner, name = repo.split("/")
    return _safe_path_segment(owner, "github owner"), _safe_path_segment(
        name, "github repo"
    )


def redirects_fragment_path(
    repo_dir: Path, kind: str, owner: str, name: str, tag: str
) -> Path:
    if kind not in {"ubuntu", "pacman"}:
        raise SystemExit(f"invalid redirect kind {kind!r}")
    return (
        repo_dir
        / kind
        / "_redirects.d"
        / _safe_path_segment(owner, "github owner")
        / _safe_path_segment(name, "github repo")
        / _safe_path_segment(tag, "tag")
        / "_redirects"
    )


def extra_redirects_path(repo_dir: Path) -> Path:
    return repo_dir / "_redirects.d" / "_extra" / "_redirects"


def parse_redirects_file(text: str) -> list[Redirect]:
    rules: list[Redirect] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = REDIRECT_LINE.match(stripped)
        if not match:
            continue
        rules.append(
            Redirect(match.group("src"), match.group("dest"), match.group("code") or "302")
        )
    return rules


def format_redirects_file(rules: list[Redirect], *, comments: list[str] | tuple[str, ...]) -> str:
    lines = list(comments)
    if lines and lines[-1] != "":
        lines.append("")
    for rule in rules:
        lines.append(rule.line())
    lines.append("")
    return "\n".join(lines)


def classify_redirect(rule: Redirect) -> tuple[str, str, str, str] | None:
    """Return (kind, owner, repo, tag) when the rule maps to a GitHub Release asset."""
    src = rule.src
    if src.startswith("/ubuntu/"):
        kind = "ubuntu"
    elif src.startswith("/pacman/"):
        kind = "pacman"
    else:
        kind = None
    match = GITHUB_ASSET_URL.match(rule.dest)
    if match and kind is not None:
        return kind, match.group("owner"), match.group("repo"), match.group("tag")
    match = UBUNTU_POOL_SRC.match(src)
    if match:
        return "ubuntu", match.group("owner"), match.group("repo"), match.group("tag")
    return None


def release_redirect_rules(
    *,
    github_repo: str,
    tag: str,
    deb_assets: list[str],
    pkg_assets: list[tuple[str, str]],
) -> tuple[list[Redirect], list[Redirect]]:
    """pkg_assets: list of (pacman_arch, filename). One 302 per file."""
    release = release_base_url(github_repo, tag)
    ubuntu = [
        Redirect(pool_redirect_src(github_repo, tag, name), f"{release}/{name}", "302")
        for name in sorted(deb_assets)
    ]
    pacman = [
        Redirect(f"/pacman/{arch}/{name}", f"{release}/{name}", "302")
        for arch, name in sorted(pkg_assets)
    ]
    return ubuntu, pacman


def render_redirects(
    *,
    github_repo: str,
    tag: str,
    deb_assets: list[str],
    pkg_assets: list[tuple[str, str]],
) -> str:
    """One release's ubuntu + pacman rules (no merge). Used by tests."""
    ubuntu, pacman = release_redirect_rules(
        github_repo=github_repo,
        tag=tag,
        deb_assets=deb_assets,
        pkg_assets=pkg_assets,
    )
    return format_redirects_file(ubuntu + pacman, comments=ROOT_REDIRECTS_HEADER)


def _is_extra_fragment(path: Path, repo_dir: Path) -> bool:
    try:
        rel = path.relative_to(repo_dir).as_posix()
    except ValueError:
        rel = path.as_posix()
    return rel.startswith("_redirects.d/_extra/")


def _fragment_sort_key(path: Path, repo_dir: Path) -> tuple[int, int, str]:
    try:
        rel = path.relative_to(repo_dir).as_posix()
    except ValueError:
        rel = path.as_posix()
    extra = 0 if _is_extra_fragment(path, repo_dir) else 1
    if rel.startswith("ubuntu/"):
        kind = 0
    elif rel.startswith("pacman/"):
        kind = 1
    else:
        kind = 2
    return (extra, kind, rel)


def iter_redirect_fragments(repo_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for base in (
        extra_redirects_path(repo_dir).parent,
        repo_dir / "ubuntu" / "_redirects.d",
        repo_dir / "pacman" / "_redirects.d",
    ):
        if not base.is_dir():
            continue
        paths.extend(p for p in base.rglob("_redirects") if p.is_file())
    return sorted(paths, key=lambda p: _fragment_sort_key(p, repo_dir))


def write_redirect_fragment(
    path: Path, rules: list[Redirect], *, comments: list[str] | tuple[str, ...]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(format_redirects_file(rules, comments=comments), encoding="utf-8")


def migrate_root_redirects(
    repo_dir: Path, *, skip: set[tuple[str, str, str, str]]
) -> None:
    """Split an existing root ``_redirects`` into per-release fragment files.

    Existing fragment files are left untouched so older tags stay as they were.
    ``skip`` is the current (kind, owner, repo, tag) set the caller will rewrite.
    """
    root = repo_dir / "_redirects"
    if not root.is_file():
        return
    extra: list[Redirect] = []
    grouped: dict[tuple[str, str, str, str], list[Redirect]] = defaultdict(list)
    for rule in parse_redirects_file(root.read_text(encoding="utf-8")):
        classified = classify_redirect(rule)
        if classified is None:
            extra.append(rule)
            continue
        grouped[classified].append(rule)
    for key, rules in grouped.items():
        if key in skip:
            continue
        kind, owner, name, tag = key
        path = redirects_fragment_path(repo_dir, kind, owner, name, tag)
        if path.is_file():
            continue
        write_redirect_fragment(
            path,
            rules,
            comments=[f"# {owner}/{name} {tag} — {kind} package redirects."],
        )
        print(f"migrated {len(rules)} redirect(s) → {path.relative_to(repo_dir)}")
    if not extra:
        return
    extra_path = extra_redirects_path(repo_dir)
    merged: dict[str, Redirect] = {}
    if extra_path.is_file():
        for rule in parse_redirects_file(extra_path.read_text(encoding="utf-8")):
            merged[rule.src] = rule
    changed = not extra_path.is_file()
    for rule in extra:
        if merged.get(rule.src) != rule:
            merged[rule.src] = rule
            changed = True
    if not changed:
        return
    write_redirect_fragment(
        extra_path,
        list(merged.values()),
        comments=["# Redirects that do not belong to a GitHub Release fragment."],
    )
    print(f"preserved {len(merged)} extra redirect(s) → {extra_path.relative_to(repo_dir)}")


def assemble_root_redirects(repo_dir: Path) -> str:
    """Merge every fragment into the root ``_redirects`` Cloudflare reads.

    Same source path: later fragments override (extras are merged first).
    First-seen source order is kept so older tags stay above newer ones.
    """
    winners: dict[str, Redirect] = {}
    order: list[str] = []
    for path in iter_redirect_fragments(repo_dir):
        for rule in parse_redirects_file(path.read_text(encoding="utf-8")):
            if rule.src not in winners:
                order.append(rule.src)
            winners[rule.src] = rule
    rules = [winners[src] for src in order]
    if len(rules) > CLOUDFLARE_STATIC_REDIRECT_LIMIT:
        print(
            f"warning: {len(rules)} redirects; Cloudflare Pages static limit is "
            f"{CLOUDFLARE_STATIC_REDIRECT_LIMIT}"
        )
    return format_redirects_file(rules, comments=ROOT_REDIRECTS_HEADER)


def write_redirects(
    repo_dir: Path,
    *,
    github_repo: str,
    tag: str,
    deb_assets: list[str],
    pkg_assets: list[tuple[str, str]],
) -> None:
    """Write this tag's fragments and reassemble root ``_redirects``.

    Older tag directories are not rewritten. Rules already in the root file
    are migrated into fragments first so they survive the assemble step.
    """
    owner, name = split_github_repo(github_repo)
    tag = _safe_path_segment(tag, "tag")
    migrate_root_redirects(
        repo_dir,
        skip={
            ("ubuntu", owner, name, tag),
            ("pacman", owner, name, tag),
        },
    )
    ubuntu_rules, pacman_rules = release_redirect_rules(
        github_repo=f"{owner}/{name}",
        tag=tag,
        deb_assets=deb_assets,
        pkg_assets=pkg_assets,
    )
    ubuntu_path = redirects_fragment_path(repo_dir, "ubuntu", owner, name, tag)
    pacman_path = redirects_fragment_path(repo_dir, "pacman", owner, name, tag)
    if ubuntu_rules:
        write_redirect_fragment(
            ubuntu_path,
            ubuntu_rules,
            comments=[f"# {owner}/{name} {tag} — ubuntu pool redirects."],
        )
        print(f"wrote {ubuntu_path.relative_to(repo_dir)}")
    elif ubuntu_path.is_file():
        ubuntu_path.unlink()
        print(f"removed empty {ubuntu_path.relative_to(repo_dir)}")
    if pacman_rules:
        write_redirect_fragment(
            pacman_path,
            pacman_rules,
            comments=[f"# {owner}/{name} {tag} — pacman redirects."],
        )
        print(f"wrote {pacman_path.relative_to(repo_dir)}")
    elif pacman_path.is_file():
        pacman_path.unlink()
        print(f"removed empty {pacman_path.relative_to(repo_dir)}")
    root = repo_dir / "_redirects"
    root.write_text(assemble_root_redirects(repo_dir), encoding="utf-8")
    print(f"wrote {root}")


def render_pacman_conf(github_repo: str, tag: str) -> str:
    release = release_base_url(github_repo, tag)
    return (
        "# MOSUMI Pacman repository.\n"
        "# Install:\n"
        f"#   curl -fsSL {PAGES_BASE}/keys/repo.asc | sudo pacman-key --add -\n"
        f"#   sudo pacman-key --lsign-key {REPO_KEY_ID}\n"
        f"#   sudo curl -fsSL {PAGES_BASE}/pacman/{PACMAN_CONF_NAME} \\\n"
        f"#     -o {PACMAN_INCLUDE_PATH}\n"
        f"#   echo 'Include = {PACMAN_INCLUDE_PATH}' | sudo tee -a /etc/pacman.conf\n"
        "#   sudo pacman -Sy imprint\n"
        "#\n"
        f"# First Server hosts {PACMAN_DB_NAME} + {PACMAN_DB_NAME}.sig "
        "(this Cloudflare Pages tree).\n"
        "# Package files 302 from /pacman/$arch/<file> to the Imprint GitHub Release.\n"
        f"[{PACMAN_REPO_NAME}]\n"
        "SigLevel = PackageOptional DatabaseRequired\n"
        f"Server = {PAGES_BASE}/pacman/$arch\n"
        f"Server = {release}\n"
    )


def render_repo_sources() -> str:
    return (
        "# Linux package repository (DEB822)\n"
        "# Install the keyring first:\n"
        f"#   sudo curl -fsSL {PAGES_BASE}/keys/repo.gpg \\\n"
        "#     -o /usr/share/keyrings/repo-archive-keyring.gpg\n"
        f"# Default suite is {DEFAULT_SUITE} (Ubuntu 24.04).\n"
        "# Use Suites: resolute for Ubuntu 26.04 builds instead.\n"
        "Types: deb\n"
        f"URIs: {PAGES_BASE}/ubuntu\n"
        f"Suites: {DEFAULT_SUITE}\n"
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
        f"deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/repo-archive-keyring.gpg] {PAGES_BASE}/ubuntu {DEFAULT_SUITE} main\n"
    )


def render_readme() -> str:
    return f"""# Linux package repository

APT (Debian / Ubuntu) and Pacman (Arch Linux) **indexes**, served
from Cloudflare Pages. Package files are **not** stored in this git tree:
Cloudflare 302s each `.deb` and Pacman `.pkg.tar.*` to the matching
GitHub Release asset.

**Base URL:** {PAGES_BASE}/

Connect this repository to **Cloudflare Pages** (build command empty, output
directory `/`) so the assembled root `_redirects` is honoured. Per-release
fragments live under `ubuntu/_redirects.d` and `pacman/_redirects.d` so
updating one tag keeps older 302s. GitHub Pages cannot 302 `/pool`.

## Public key

| File | Format |
| --- | --- |
| [keys/repo.asc]({PAGES_BASE}/keys/repo.asc) | ASCII-armored |
| [keys/repo.gpg]({PAGES_BASE}/keys/repo.gpg) | Binary keyring |

- **Fingerprint:** `{REPO_KEY_FINGERPRINT_DISPLAY}`
- **Key ID:** `{REPO_KEY_ID}`

`update-index` signs `ubuntu/dists/*/InRelease` and `pacman/$arch/{PACMAN_DB_NAME}`
with `--gpg-private-key` if given, otherwise `GPG_PRIVATE_KEY` (must match
`keys/repo.asc`). It does not use the local GnuPG keyring.

## Debian / Ubuntu (APT)

```bash
sudo mkdir -p /usr/share/keyrings
sudo curl -fsSL {PAGES_BASE}/keys/repo.gpg \\
  -o /usr/share/keyrings/repo-archive-keyring.gpg
sudo chmod 644 /usr/share/keyrings/repo-archive-keyring.gpg
sudo curl -fsSL {PAGES_BASE}/ubuntu/mosumi-repo.sources \\
  -o /etc/apt/sources.list.d/mosumi-repo.sources
sudo apt update
```

`ubuntu/mosumi-repo.sources` uses suite `{DEFAULT_SUITE}` (Ubuntu 24.04). Suite
`resolute` is Ubuntu 26.04 (amd64 and arm64). `Filename` in `Packages` is a
per-file pool path under `ubuntu/pool/github/`; Cloudflare 302s that exact
file to its GitHub Release asset.

## Arch Linux (Pacman)

```bash
curl -fsSL {PAGES_BASE}/keys/repo.asc | sudo pacman-key --add -
sudo pacman-key --lsign-key {REPO_KEY_ID}
sudo curl -fsSL {PAGES_BASE}/pacman/{PACMAN_CONF_NAME} \\
  -o {PACMAN_INCLUDE_PATH}
echo -e '\\nInclude = {PACMAN_INCLUDE_PATH}' | sudo tee -a /etc/pacman.conf
sudo pacman -Sy
```

`{PACMAN_DB_NAME}` and `{PACMAN_DB_NAME}.sig` are under `pacman/x86_64/` and
`pacman/aarch64/`. Each `.pkg.tar.zst` / `.pkg.tar.xz` is 302'd from
`/pacman/$arch/<file>` to its GitHub Release asset.

## Updating the index

A GitHub Release workflow can dispatch this repository's `update-index`
action with the new tag. Manual run (empty tag = latest):

```bash
gh workflow run update-index.yml -R googolmo/repo -f github_repo=OWNER/REPO -f tag=vX.Y.Z
```

Secrets on this repository:

| Secret | Role |
| --- | --- |
| `GPG_PRIVATE_KEY` | OpenPGP secret matching `keys/repo.asc`; signs APT `InRelease` and Pacman `{PACMAN_DB_NAME}` (overridden by `--gpg-private-key`) |
| `GPG_PASSPHRASE` | Optional passphrase for that key |

## Layout

```
.
├── _redirects                 assembled Cloudflare 302s (do not edit)
├── keys/
├── ubuntu/
│   ├── mosumi-repo.sources
│   ├── mosumi-repo.list
│   ├── dists/{{noble,resolute}}/
│   │   └── main/{{binary-amd64,binary-arm64,source}}/
│   ├── _redirects.d/<owner>/<repo>/<tag>/_redirects
│   └── pool/github/...        virtual; not stored, 302 per file
└── pacman/
    ├── mosumi-repo.conf
    ├── _redirects.d/<owner>/<repo>/<tag>/_redirects
    ├── x86_64/                mosumi-repo.db + mosumi-repo.db.sig
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


def emit_github_env(**values: str) -> None:
    """Append KEY=value lines to GITHUB_ENV when running in Actions."""
    path = os.environ.get("GITHUB_ENV")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for key, value in values.items():
            if any(ch in value for ch in "\r\n"):
                raise SystemExit(f"refusing to write multiline GitHub env {key}")
            fh.write(f"{key}={value}\n")


def is_index_asset(name: str) -> bool:
    if name.endswith(".sig"):
        return False
    if name.startswith("imprint_") and name.endswith(".deb"):
        return True
    if "_archlinux_" not in name:
        return False
    return name.endswith(".pkg.tar.zst") or name.endswith(".pkg.tar.xz")


DOWNLOAD_PATTERNS = (
    "imprint_*.deb",
    "imprint_*_archlinux_*.pkg.tar.zst",
    "imprint_*_archlinux_*.pkg.tar.xz",
)


def _gh_pattern_missing(output: str) -> bool:
    text = output.lower()
    return "no assets match" in text or "no assets to download" in text


def download_assets(github_repo: str, tag: str, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if shutil.which("gh"):
        # One pattern per call so a missing noble/resolute .deb
        # (or a missing pacman arch) does not fail the whole download.
        for pattern in DOWNLOAD_PATTERNS:
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
                pattern,
                "--clobber",
            ]
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            if proc.returncode == 0:
                continue
            out = (proc.stdout or "").strip()
            if _gh_pattern_missing(out):
                print(f"no release assets match {pattern!r}; skipping")
                continue
            raise SystemExit(f"gh release download failed for {pattern}: {out}")
        if not any(dest.iterdir()):
            raise SystemExit(f"no .deb / .pkg.tar.* assets on {github_repo} {tag}")
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
        print(f"no imprint_*.deb files in {assets_dir}; skipping ubuntu packages")
        return []
    expected_ver = version_from_tag(tag)
    for path in debs:
        file_ver, build, cpu = parse_deb_filename(path.name)
        if file_ver != expected_ver:
            raise SystemExit(
                f"{path.name}: version {file_ver} does not match tag {tag}"
            )
        suite = apt_suite_for_build(build)
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


def resolve_private_key_material(key_path: Path | None) -> tuple[str | None, str]:
    """Prefer --gpg-private-key; fall back to GPG_PRIVATE_KEY. Never the local keyring."""
    if key_path is not None:
        path = key_path.expanduser()
        if not path.is_file():
            raise SystemExit(f"GPG private key file not found: {path}")
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise SystemExit(f"GPG private key file is empty: {path}")
        return text, str(path)
    env = os.environ.get("GPG_PRIVATE_KEY", "").strip()
    if env:
        return env, "GPG_PRIVATE_KEY"
    return None, ""


@contextmanager
def gpg_passphrase_extra(passphrase: str | None) -> Iterator[list[str]]:
    env_file = os.environ.get("GPG_PASSPHRASE_FILE", "").strip()
    if env_file:
        yield ["--passphrase-file", env_file]
        return
    if not passphrase:
        yield []
        return
    handle = tempfile.NamedTemporaryFile("w", delete=False, prefix="gpg-pass-")
    handle.write(passphrase)
    handle.close()
    pass_file = Path(handle.name)
    os.chmod(pass_file, 0o600)
    try:
        yield ["--passphrase-file", str(pass_file)]
    finally:
        pass_file.unlink(missing_ok=True)


@contextmanager
def gpg_signing_home(key_material: str, *, pub_asc: Path) -> Iterator[str]:
    """Import the supplied secret into an isolated GNUPGHOME and yield its fingerprint."""
    if not pub_asc.is_file():
        raise SystemExit(f"public key not found: {pub_asc}")
    if not key_material.endswith("\n"):
        key_material += "\n"
    prev = os.environ.get("GNUPGHOME")
    tmp = Path(tempfile.mkdtemp(prefix="gnupg-sign-"))
    os.chmod(tmp, 0o700)
    os.environ["GNUPGHOME"] = str(tmp)
    try:
        imported = subprocess.run(
            ["gpg", "--batch", "--import"],
            input=key_material.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if imported.returncode != 0:
            detail = imported.stdout.decode("utf-8", "replace").strip()
            raise SystemExit(f"gpg failed to import private key: {detail}")
        pub = gpg_public_fingerprint(pub_asc)
        if not pub:
            raise SystemExit(f"could not read fingerprint from {pub_asc}")
        if pub not in gpg_secret_fingerprints():
            raise SystemExit(
                f"private key does not match {pub_asc} (expected fingerprint {pub})"
            )
        yield pub
    except FileNotFoundError as exc:
        raise SystemExit("gpg is required to sign repository indexes") from exc
    finally:
        subprocess.run(
            ["gpgconf", "--kill", "all"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if prev is None:
            os.environ.pop("GNUPGHOME", None)
        else:
            os.environ["GNUPGHOME"] = prev
        shutil.rmtree(tmp, ignore_errors=True)


def _gpg_sign_argv(fingerprint: str, extra: list[str]) -> list[str]:
    return [
        "gpg",
        "--batch",
        "--yes",
        "--pinentry-mode",
        "loopback",
        "--digest-algo",
        "SHA256",
        "--local-user",
        fingerprint,
        *extra,
    ]


def gpg_sign_release(
    release_path: Path, *, fingerprint: str, passphrase: str | None
) -> None:
    suite_dir = release_path.parent
    inrelease = suite_dir / "InRelease"
    detach = suite_dir / "Release.gpg"
    for leftover in (inrelease, detach):
        if leftover.exists():
            leftover.unlink()
    with gpg_passphrase_extra(passphrase) as extra:
        try:
            subprocess.check_call(
                _gpg_sign_argv(fingerprint, extra)
                + ["--clearsign", "-o", str(inrelease), str(release_path)]
            )
            subprocess.check_call(
                _gpg_sign_argv(fingerprint, extra)
                + [
                    "--detach-sign",
                    "--armor",
                    "-o",
                    str(detach),
                    str(release_path),
                ]
            )
        except FileNotFoundError as exc:
            raise SystemExit("gpg is required to sign APT InRelease") from exc
        except subprocess.CalledProcessError as exc:
            raise SystemExit(f"gpg failed signing {release_path}") from exc
    subprocess.check_call(["gpg", "--batch", "--verify", str(inrelease)])
    print(f"signed {inrelease}")


def gpg_sign_detached(
    path: Path, *, fingerprint: str, passphrase: str | None, armor: bool = False
) -> Path:
    dest = path.with_name(path.name + ".sig")
    if dest.exists():
        dest.unlink()
    argv = ["--detach-sign", "-o", str(dest), str(path)]
    if armor:
        argv = ["--detach-sign", "--armor", "-o", str(dest), str(path)]
    with gpg_passphrase_extra(passphrase) as extra:
        try:
            subprocess.check_call(_gpg_sign_argv(fingerprint, extra) + argv)
        except FileNotFoundError as exc:
            raise SystemExit(f"gpg is required to sign {path}") from exc
        except subprocess.CalledProcessError as exc:
            raise SystemExit(f"gpg failed signing {path}") from exc
    subprocess.check_call(["gpg", "--batch", "--verify", str(dest), str(path)])
    print(f"signed {dest}")
    return dest


def gpg_sign_pacman_db(
    dest: Path, *, fingerprint: str, passphrase: str | None
) -> None:
    db = dest / PACMAN_DB_NAME
    tarball = dest / PACMAN_DB_TAR_NAME
    db_sig = gpg_sign_detached(db, fingerprint=fingerprint, passphrase=passphrase)
    if tarball.exists():
        tar_sig = tarball.with_name(tarball.name + ".sig")
        if tarball.read_bytes() == db.read_bytes():
            shutil.copyfile(db_sig, tar_sig)
            print(f"signed {tar_sig}")
        else:
            gpg_sign_detached(tarball, fingerprint=fingerprint, passphrase=passphrase)


def clear_old_dists(dist_root: Path) -> None:
    if not dist_root.exists():
        return
    for child in dist_root.iterdir():
        if not child.is_dir():
            continue
        if child.name not in ALL_SUITES:
            shutil.rmtree(child)
            print(f"removed leftover ubuntu/dists/{child.name}")
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
    (dest / PACMAN_DB_TAR_NAME).write_bytes(data)
    (dest / PACMAN_DB_NAME).write_bytes(data)
    for leftover in dest.glob("*.pkg.tar.*"):
        leftover.unlink()
    for name in (
        f"{PACMAN_DB_NAME}.sig",
        f"{PACMAN_DB_TAR_NAME}.sig",
        "repo.db",
        "repo.db.sig",
        "repo.db.tar.gz",
        "repo.db.tar.gz.sig",
    ):
        stale = dest / name
        if name in (PACMAN_DB_NAME, PACMAN_DB_TAR_NAME):
            continue
        if stale.exists() or stale.is_symlink():
            stale.unlink()


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
    gpg_private_key: Path | None,
) -> str:
    github_repo = github_repo.strip().strip("/")
    resolved = resolve_tag(github_repo, tag)
    release_base_url(github_repo, resolved)
    emit_github_env(TAG=resolved)
    print(f"tag={resolved} repo={github_repo}")

    key_material: str | None = None
    key_source = ""
    if skip_sign:
        print(
            f"skipping APT InRelease and Pacman {PACMAN_DB_NAME} "
            "signatures (--skip-sign)"
        )
    else:
        key_material, key_source = resolve_private_key_material(gpg_private_key)
        if not key_material:
            raise SystemExit(
                "signing requires --gpg-private-key or GPG_PRIVATE_KEY "
                "(pass --skip-sign to write unsigned indexes)"
            )
        print(f"signing with private key from {key_source}")

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
            by_suite[stanza["_suite"]].append(stanza)
        for suite in ALL_SUITES:
            if not by_suite[suite]:
                print(f"no .deb assets for {suite}; writing empty suite")

        deb_names = [Path(s["Filename"]).name for s in stanzas]
        pkg_rows = collect_pkg_assets(assets_dir)
        if not pkg_rows:
            raise SystemExit(
                f"no imprint_*_archlinux_*.pkg.tar.zst/.xz files in {assets_dir}"
            )
        pkg_assets = [(arch, path.name) for arch, path in pkg_rows]

        write_redirects(
            repo_dir,
            github_repo=github_repo,
            tag=resolved,
            deb_assets=deb_names,
            pkg_assets=pkg_assets,
        )

        pacman_dir = repo_dir / "pacman"
        pacman_dir.mkdir(parents=True, exist_ok=True)
        conf = pacman_dir / PACMAN_CONF_NAME
        conf.write_text(render_pacman_conf(github_repo, resolved), encoding="utf-8")
        print(f"wrote {conf}")
        stale_conf = pacman_dir / "repo.conf"
        if stale_conf.exists() and stale_conf != conf:
            stale_conf.unlink()
            print(f"removed stale {stale_conf}")

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

        def write_indexes(fingerprint: str | None) -> None:
            for suite in ALL_SUITES:
                write_suite(dist_root, suite, by_suite[suite])
                if fingerprint is not None:
                    gpg_sign_release(
                        dist_root / suite / "Release",
                        fingerprint=fingerprint,
                        passphrase=passphrase,
                    )
            for arch in ALL_PACMAN_ARCHES:
                write_pacman_db(pacman_dir / arch, by_arch[arch])
                print(
                    f"wrote pacman/{arch}/{PACMAN_DB_NAME} "
                    f"({len(by_arch[arch])} package(s))"
                )
                if fingerprint is not None:
                    gpg_sign_pacman_db(
                        pacman_dir / arch,
                        fingerprint=fingerprint,
                        passphrase=passphrase,
                    )

        if key_material is not None:
            with gpg_signing_home(
                key_material, pub_asc=repo_dir / "keys" / "repo.asc"
            ) as fingerprint:
                write_indexes(fingerprint)
        else:
            write_indexes(None)

        sources = repo_dir / "ubuntu" / "mosumi-repo.sources"
        sources.write_text(render_repo_sources(), encoding="utf-8")
        print(f"wrote {sources}")
        repo_list = repo_dir / "ubuntu" / "mosumi-repo.list"
        repo_list.write_text(render_repo_list(), encoding="utf-8")
        print(f"wrote {repo_list}")

        readme = repo_dir / "README.md"
        readme.write_text(render_readme(), encoding="utf-8")
        print(f"wrote {readme}")
        rewrite_site_urls(repo_dir)
    finally:
        if tmp is not None:
            tmp.cleanup()
    return resolved


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
        "--gpg-private-key",
        type=Path,
        help="ASCII-armored OpenPGP secret (overrides GPG_PRIVATE_KEY)",
    )
    parser.add_argument(
        "--skip-sign",
        action="store_true",
        help=f"Do not sign ubuntu/dists/*/InRelease or pacman/*/{PACMAN_DB_NAME}",
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

    ver, suite, cpu = parse_deb_filename("imprint_0.1.4_ubuntu24.04_x86_64.deb")
    if (ver, suite, cpu) != ("0.1.4", "ubuntu24.04", "x86_64"):
        raise SystemExit("parse_deb_filename failed")
    try:
        parse_deb_filename("imprint_0.1.4_amd64.deb")
    except SystemExit:
        pass
    else:
        raise SystemExit("should reject untagged .deb names")
    if apt_suite_for_build("ubuntu24.04") != "noble":
        raise SystemExit("ubuntu24.04 must map to noble")
    if apt_suite_for_build("ubuntu26.04") != "resolute":
        raise SystemExit("ubuntu26.04 must map to resolute")
    try:
        apt_suite_for_build("ubuntu22.04")
    except SystemExit:
        pass
    else:
        raise SystemExit("ubuntu22.04 must be rejected")

    filename = pool_filename(repo, tag, "imprint_0.1.4_ubuntu24.04_x86_64.deb")
    if filename != "pool/github/googolmo/imprint/v0.1.4/imprint_0.1.4_ubuntu24.04_x86_64.deb":
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
        suite=DEFAULT_SUITE,
        files=hashed,
        date=datetime(2026, 9, 4, tzinfo=timezone.utc),
    )
    if f"Suite: {DEFAULT_SUITE}" not in release:
        raise SystemExit("Release missing suite")
    if "Architectures: amd64 arm64" not in release:
        raise SystemExit("Release missing architectures")
    if " s256               10 main/binary-amd64/Packages" not in release:
        raise SystemExit(f"Release SHA256 line mismatch:\n{release}")

    redirects = render_redirects(
        github_repo=repo,
        tag=tag,
        deb_assets=["imprint_0.1.4_ubuntu24.04_x86_64.deb"],
        pkg_assets=[("x86_64", "imprint_0.1.4_archlinux_x86_64.pkg.tar.zst")],
    )
    if "/ubuntu/pool/github/googolmo/imprint/v0.1.4/" not in redirects:
        raise SystemExit("redirects missing deb path")
    if f"{base}/imprint_0.1.4_ubuntu24.04_x86_64.deb 302" not in redirects:
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
    if "Assembled from ubuntu/_redirects.d" not in redirects:
        raise SystemExit("root _redirects must document fragment assemble")

    conf = render_pacman_conf(repo, tag)
    if f"Server = {PAGES_BASE}/pacman/$arch\n" not in conf:
        raise SystemExit("pacman must keep db on Pages")
    if f"Server = {base}\n" not in conf:
        raise SystemExit("pacman must list the GitHub Release as package Server")
    if "/releases/latest/download/" in conf:
        raise SystemExit("must not use latest redirect")
    if "SigLevel = PackageOptional DatabaseRequired\n" not in conf:
        raise SystemExit(f"pacman must require a signed {PACMAN_DB_NAME}")
    if f"[{PACMAN_REPO_NAME}]\n" not in conf:
        raise SystemExit(f"pacman snippet must use [{PACMAN_REPO_NAME}]")
    if f"{PAGES_BASE}/pacman/{PACMAN_CONF_NAME}" not in conf:
        raise SystemExit(f"pacman snippet must install {PACMAN_CONF_NAME}")
    if PACMAN_INCLUDE_PATH not in conf:
        raise SystemExit(f"pacman snippet must include {PACMAN_INCLUDE_PATH}")
    if "/etc/pacman.d/repo\n" in conf or "/etc/pacman.d/repo " in conf:
        raise SystemExit("pacman snippet must not use unbranded /etc/pacman.d/repo")
    if f"pacman-key --lsign-key {REPO_KEY_ID}" not in conf:
        raise SystemExit("pacman snippet must locally sign the repo key")

    sources = render_repo_sources()
    if f"URIs: {PAGES_BASE}/ubuntu\n" not in sources:
        raise SystemExit("mosumi-repo.sources URI mismatch")
    if "Architectures: amd64 arm64" not in sources:
        raise SystemExit("mosumi-repo.sources missing architectures")
    if f"Suites: {DEFAULT_SUITE}\n" not in sources:
        raise SystemExit("mosumi-repo.sources missing default suite")
    if f"deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/repo-archive-keyring.gpg] {PAGES_BASE}/ubuntu {DEFAULT_SUITE} main\n" not in render_repo_list():
        raise SystemExit("mosumi-repo.list mismatch")

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
        deb_path = root / "imprint_0.1.4_ubuntu24.04_x86_64.deb"
        deb_path.write_bytes(deb)
        parsed = deb_control_from_ar(deb_path)
        if parsed["Package"] != "imprint" or parsed["Version"] != "0.1.4":
            raise SystemExit(f"deb_control_from_ar failed: {parsed}")

        empty_dir = root / "no-debs"
        empty_dir.mkdir()
        if collect_debs(empty_dir, github_repo=repo, tag=tag) != []:
            raise SystemExit("collect_debs must ignore a missing ubuntu .deb set")

        only_noble = root / "only-noble"
        only_noble.mkdir()
        shutil.copyfile(deb_path, only_noble / deb_path.name)
        subset = collect_debs(only_noble, github_repo=repo, tag=tag)
        if len(subset) != 1 or subset[0]["_suite"] != "noble":
            raise SystemExit("collect_debs must map ubuntu24.04 builds to noble")
        if _gh_pattern_missing("no assets match the file pattern") is False:
            raise SystemExit("must treat gh 'no assets match' as a missing pattern")
        if _gh_pattern_missing("HTTP 403 forbidden"):
            raise SystemExit("must not ignore real gh download failures")

        dist = root / "ubuntu" / "dists"
        write_suite(dist, "resolute", [])
        for arch in ALL_DEB_ARCHES:
            pkg = dist / "resolute" / "main" / f"binary-{arch}" / "Packages"
            if not pkg.exists() or pkg.read_bytes() != b"":
                raise SystemExit(f"empty suite Packages missing for {arch}")
        sources_path = dist / "resolute" / "main" / "source" / "Sources"
        if not sources_path.exists():
            raise SystemExit("Sources missing")
        rel = (dist / "resolute" / "Release").read_text(encoding="utf-8")
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
        db = root / "pacman" / "x86_64" / PACMAN_DB_NAME
        if not db.exists() or db.stat().st_size == 0:
            raise SystemExit(f"{PACMAN_DB_NAME} not written")
        with tarfile.open(db, mode="r:gz") as tar:
            names = tar.getnames()
        if "imprint-0.1.4-1/desc" not in names:
            raise SystemExit(f"{PACMAN_DB_NAME} missing desc: {names}")

        leftover = dist / "stable"
        leftover.mkdir(parents=True, exist_ok=True)
        (leftover / "Release").write_text("gone", encoding="utf-8")
        clear_old_dists(dist)
        if leftover.exists():
            raise SystemExit("clear_old_dists must drop unknown suites")

        (root / "ubuntu" / "dists" / DEFAULT_SUITE).mkdir(parents=True, exist_ok=True)
        (root / "ubuntu" / "dists" / DEFAULT_SUITE / ".gitkeep").write_text("")
        (root / "_redirects").write_text(redirects)
        (root / "pacman").mkdir(exist_ok=True)
        (root / "pacman" / PACMAN_CONF_NAME).write_text(conf)
        if (root / "ubuntu" / "pool" / "main").exists():
            raise SystemExit("must not materialise pool/main debs")
        _self_test_redirect_merge(root)

        env_file = root / "github.env"
        prev_github_env = os.environ.get("GITHUB_ENV")
        os.environ["GITHUB_ENV"] = str(env_file)
        try:
            emit_github_env(TAG="v0.1.4")
        finally:
            if prev_github_env is None:
                os.environ.pop("GITHUB_ENV", None)
            else:
                os.environ["GITHUB_ENV"] = prev_github_env
        if env_file.read_text(encoding="utf-8") != "TAG=v0.1.4\n":
            raise SystemExit("emit_github_env did not write TAG")

        keyfile = root / "specified.asc"
        keyfile.write_text("FILEKEY\n", encoding="utf-8")
        prev_env = os.environ.get("GPG_PRIVATE_KEY")
        os.environ["GPG_PRIVATE_KEY"] = "ENVKEY"
        try:
            material, source = resolve_private_key_material(keyfile)
            if material != "FILEKEY" or source != str(keyfile):
                raise SystemExit(
                    "must prefer --gpg-private-key over GPG_PRIVATE_KEY"
                )
            material, source = resolve_private_key_material(None)
            if material != "ENVKEY" or source != "GPG_PRIVATE_KEY":
                raise SystemExit("must fall back to GPG_PRIVATE_KEY")
            missing = root / "missing.asc"
            try:
                resolve_private_key_material(missing)
            except SystemExit:
                pass
            else:
                raise SystemExit("missing specified key must not fall back to env")
            del os.environ["GPG_PRIVATE_KEY"]
            material, source = resolve_private_key_material(None)
            if material is not None or source:
                raise SystemExit("must not use a local keyring private key")
        finally:
            if prev_env is None:
                os.environ.pop("GPG_PRIVATE_KEY", None)
            else:
                os.environ["GPG_PRIVATE_KEY"] = prev_env

        _self_test_signing(root)

    print("self-test ok")


def _self_test_redirect_merge(root: Path) -> None:
    repo = "googolmo/imprint"
    old_tag = "v0.1.4"
    new_tag = "v0.1.5"
    extra_src = "/custom/keep-me"
    extra_dest = "https://example.com/keep"
    old_deb = "imprint_0.1.4_ubuntu22.04_x86_64.deb"
    old_pkg = "imprint_0.1.4_archlinux_x86_64.pkg.tar.zst"
    new_deb = "imprint_0.1.5_ubuntu24.04_amd64.deb"
    new_pkg = "imprint_0.1.5_archlinux_amd64.pkg.tar.zst"
    other_deb = "other_1.0_ubuntu22.04_amd64.deb"

    old = render_redirects(
        github_repo=repo,
        tag=old_tag,
        deb_assets=[old_deb],
        pkg_assets=[("x86_64", old_pkg)],
    )
    # Hand-written extra + another GitHub repo's rule must survive an imprint bump.
    old += (
        f"{extra_src} {extra_dest} 302\n"
        f"/ubuntu/pool/github/acme/other/v1.0/{other_deb} "
        f"https://github.com/acme/other/releases/download/v1.0/{other_deb} 302\n"
    )
    (root / "_redirects").write_text(old, encoding="utf-8")

    write_redirects(
        root,
        github_repo=repo,
        tag=new_tag,
        deb_assets=[new_deb],
        pkg_assets=[("x86_64", new_pkg)],
    )

    assembled = (root / "_redirects").read_text(encoding="utf-8")
    for needle in (
        f"/ubuntu/pool/github/googolmo/imprint/{old_tag}/{old_deb}",
        f"/ubuntu/pool/github/googolmo/imprint/{new_tag}/{new_deb}",
        f"/pacman/x86_64/{old_pkg}",
        f"/pacman/x86_64/{new_pkg}",
        extra_src,
        extra_dest,
        f"/ubuntu/pool/github/acme/other/v1.0/{other_deb}",
    ):
        if needle not in assembled:
            raise SystemExit(f"assembled _redirects missing {needle!r}:\n{assembled}")
    if assembled.count(" 302") < 6:
        raise SystemExit(f"expected preserved + new redirects, got:\n{assembled}")

    old_ubuntu = redirects_fragment_path(root, "ubuntu", "googolmo", "imprint", old_tag)
    new_ubuntu = redirects_fragment_path(root, "ubuntu", "googolmo", "imprint", new_tag)
    old_pacman = redirects_fragment_path(root, "pacman", "googolmo", "imprint", old_tag)
    new_pacman = redirects_fragment_path(root, "pacman", "googolmo", "imprint", new_tag)
    other_ubuntu = redirects_fragment_path(root, "ubuntu", "acme", "other", "v1.0")
    extra_path = extra_redirects_path(root)
    for path in (old_ubuntu, new_ubuntu, old_pacman, new_pacman, other_ubuntu, extra_path):
        if not path.is_file():
            raise SystemExit(f"missing redirect fragment {path}")

    old_ubuntu_text = old_ubuntu.read_text(encoding="utf-8")
    if old_deb not in old_ubuntu_text or new_deb in old_ubuntu_text:
        raise SystemExit("old ubuntu fragment must keep only the old tag")

    # Updating the new tag must not rewrite the old tag's fragment file.
    before = old_ubuntu.read_bytes()
    write_redirects(
        root,
        github_repo=repo,
        tag=new_tag,
        deb_assets=[new_deb, "imprint_0.1.5_ubuntu26.04_amd64.deb"],
        pkg_assets=[("x86_64", new_pkg)],
    )
    if old_ubuntu.read_bytes() != before:
        raise SystemExit("must not rewrite an older tag's _redirects fragment")
    assembled = (root / "_redirects").read_text(encoding="utf-8")
    if "imprint_0.1.5_ubuntu26.04_amd64.deb" not in assembled:
        raise SystemExit("re-applying a tag must update that tag's fragment")
    if old_deb not in assembled or extra_src not in assembled:
        raise SystemExit("re-applying a tag must still keep older redirects")

    # Same source path: the fragment written for this tag wins.
    clash_src = pool_redirect_src(repo, new_tag, new_deb)
    extra_path.write_text(
        f"{clash_src} https://example.com/stale 302\n",
        encoding="utf-8",
    )
    root_file = root / "_redirects"
    root_file.write_text(assemble_root_redirects(root), encoding="utf-8")
    clash_lines = [
        line
        for line in root_file.read_text(encoding="utf-8").splitlines()
        if line.startswith(clash_src + " ")
    ]
    if len(clash_lines) != 1:
        raise SystemExit(f"duplicate source paths in assembled _redirects: {clash_lines}")
    if "https://example.com/stale" in clash_lines[0]:
        raise SystemExit("release fragment must override extra on the same source")


def _self_test_signing(root: Path) -> None:
    if shutil.which("gpg") is None:
        print("self-test: gpg not found, skipping sign tests")
        return
    prev_home = os.environ.get("GNUPGHOME")
    gen_home = Path(tempfile.mkdtemp(prefix="gnupg-test-gen-"))
    os.chmod(gen_home, 0o700)
    os.environ["GNUPGHOME"] = str(gen_home)
    try:
        batch = (
            "Key-Type: EDDSA\n"
            "Key-Curve: Ed25519\n"
            "Key-Usage: sign\n"
            "Name-Real: MOSUMI test\n"
            "Name-Email: test@example.com\n"
            "Expire-Date: 0\n"
            "%no-protection\n"
            "%commit\n"
        )
        gen = subprocess.run(
            ["gpg", "--batch", "--generate-key"],
            input=batch.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if gen.returncode != 0:
            raise SystemExit(
                "gpg --generate-key failed:\n"
                + gen.stdout.decode("utf-8", "replace")
            )
        pub = subprocess.check_output(
            ["gpg", "--batch", "--armor", "--export", "test@example.com"]
        )
        priv = subprocess.check_output(
            ["gpg", "--batch", "--armor", "--export-secret-keys", "test@example.com"]
        )
    finally:
        subprocess.run(
            ["gpgconf", "--kill", "all"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if prev_home is None:
            os.environ.pop("GNUPGHOME", None)
        else:
            os.environ["GNUPGHOME"] = prev_home
        shutil.rmtree(gen_home, ignore_errors=True)

    pub_asc = root / "keys" / "repo.asc"
    pub_asc.parent.mkdir(parents=True, exist_ok=True)
    pub_asc.write_bytes(pub)
    release_path = root / "ubuntu" / "dists" / DEFAULT_SUITE / "Release"
    release_path.parent.mkdir(parents=True, exist_ok=True)
    release_path.write_text("Origin: test\n", encoding="utf-8")
    db_dir = root / "pacman" / "x86_64"
    db_dir.mkdir(parents=True, exist_ok=True)
    (db_dir / PACMAN_DB_NAME).write_bytes(b"db")
    (db_dir / PACMAN_DB_TAR_NAME).write_bytes(b"db")

    env_key = root / "env.asc"
    env_key.write_text("ENV-SHOULD-NOT-WIN\n", encoding="utf-8")
    specified = root / "specified-real.asc"
    specified.write_bytes(priv)
    prev_env = os.environ.get("GPG_PRIVATE_KEY")
    os.environ["GPG_PRIVATE_KEY"] = env_key.read_text(encoding="utf-8")
    try:
        material, source = resolve_private_key_material(specified)
        if source != str(specified):
            raise SystemExit("signing test must use the specified private key")
        with gpg_signing_home(material or "", pub_asc=pub_asc) as fingerprint:
            gpg_sign_release(
                release_path, fingerprint=fingerprint, passphrase=None
            )
            gpg_sign_pacman_db(
                db_dir, fingerprint=fingerprint, passphrase=None
            )
    finally:
        if prev_env is None:
            os.environ.pop("GPG_PRIVATE_KEY", None)
        else:
            os.environ["GPG_PRIVATE_KEY"] = prev_env

    if not (release_path.parent / "InRelease").is_file():
        raise SystemExit("APT InRelease was not signed")
    if not (release_path.parent / "Release.gpg").is_file():
        raise SystemExit("APT Release.gpg was not signed")
    if not (db_dir / f"{PACMAN_DB_NAME}.sig").is_file():
        raise SystemExit(f"Pacman {PACMAN_DB_NAME}.sig was not signed")
    if not (db_dir / f"{PACMAN_DB_TAR_NAME}.sig").is_file():
        raise SystemExit(f"Pacman {PACMAN_DB_TAR_NAME}.sig was not signed")

    os.environ["GPG_PRIVATE_KEY"] = priv.decode("utf-8")
    try:
        material, source = resolve_private_key_material(None)
        if source != "GPG_PRIVATE_KEY":
            raise SystemExit("must import from GPG_PRIVATE_KEY when no file is given")
        with gpg_signing_home(material or "", pub_asc=pub_asc) as fingerprint:
            if not fingerprint:
                raise SystemExit("GPG_PRIVATE_KEY did not import")
    finally:
        if prev_env is None:
            os.environ.pop("GPG_PRIVATE_KEY", None)
        else:
            os.environ["GPG_PRIVATE_KEY"] = prev_env


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
        gpg_private_key=args.gpg_private_key,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
