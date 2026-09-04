# Linux package repository

APT (Debian / Ubuntu) and Pacman (Arch Linux) **indexes**, served
from Cloudflare Pages. Package files are **not** stored in this git tree:
Cloudflare 302s each `.deb` and Pacman `.pkg.tar.*` to the matching
GitHub Release asset.

**Base URL:** https://repo-cr4.pages.dev/

Connect this repository to **Cloudflare Pages** (build command empty, output
directory `/`) so the assembled root `_redirects` is honoured. Per-release
fragments live under `ubuntu/_redirects.d` and `pacman/_redirects.d` so
updating one tag keeps older 302s. GitHub Pages cannot 302 `/pool`.

## Public key

| File | Format |
| --- | --- |
| [keys/repo.asc](https://repo-cr4.pages.dev/keys/repo.asc) | ASCII-armored |
| [keys/repo.gpg](https://repo-cr4.pages.dev/keys/repo.gpg) | Binary keyring |

- **Fingerprint:** `91FD A448 7920 8693 204E  90EE 9DF4 2B70 54F1 CB5B`
- **Key ID:** `9DF42B7054F1CB5B`

`update-index` signs `ubuntu/dists/*/InRelease` and `pacman/$arch/repo.db`
with `--gpg-private-key` if given, otherwise `GPG_PRIVATE_KEY` (must match
`keys/repo.asc`). It does not use the local GnuPG keyring.

## Debian / Ubuntu (APT)

```bash
sudo mkdir -p /usr/share/keyrings
sudo curl -fsSL https://repo-cr4.pages.dev/keys/repo.gpg \
  -o /usr/share/keyrings/repo-archive-keyring.gpg
sudo chmod 644 /usr/share/keyrings/repo-archive-keyring.gpg
sudo curl -fsSL https://repo-cr4.pages.dev/ubuntu/repo.sources \
  -o /etc/apt/sources.list.d/repo.sources
sudo apt update
```

`ubuntu/repo.sources` uses suite `noble` (Ubuntu 24.04). Suite
`resolute` is Ubuntu 26.04 (amd64 and arm64). `Filename` in `Packages` is a
per-file pool path under `ubuntu/pool/github/`; Cloudflare 302s that exact
file to its GitHub Release asset.

## Arch Linux (Pacman)

```bash
curl -fsSL https://repo-cr4.pages.dev/keys/repo.asc | sudo pacman-key --add -
sudo pacman-key --lsign-key 9DF42B7054F1CB5B
sudo curl -fsSL https://repo-cr4.pages.dev/pacman/repo.conf \
  -o /etc/pacman.d/repo
echo -e '\nInclude = /etc/pacman.d/repo' | sudo tee -a /etc/pacman.conf
sudo pacman -Sy
```

`repo.db` and `repo.db.sig` are under `pacman/x86_64/` and
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
| `GPG_PRIVATE_KEY` | OpenPGP secret matching `keys/repo.asc`; signs APT `InRelease` and Pacman `repo.db` (overridden by `--gpg-private-key`) |
| `GPG_PASSPHRASE` | Optional passphrase for that key |

## Layout

```
.
├── _redirects                 assembled Cloudflare 302s (do not edit)
├── keys/
├── ubuntu/
│   ├── repo.sources
│   ├── repo.list
│   ├── dists/{noble,resolute}/
│   │   └── main/{binary-amd64,binary-arm64,source}/
│   ├── _redirects.d/<owner>/<repo>/<tag>/_redirects
│   └── pool/github/...        virtual; not stored, 302 per file
└── pacman/
    ├── repo.conf
    ├── _redirects.d/<owner>/<repo>/<tag>/_redirects
    ├── x86_64/                repo.db + repo.db.sig
    └── aarch64/
```
