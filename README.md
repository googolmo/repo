# Linux package repository

APT (Debian / Ubuntu) and Pacman (Arch Linux) **indexes** for Imprint, served
from Cloudflare Pages. Package files are **not** stored in this git tree:
Cloudflare 302s each `.deb` and Pacman `.pkg.tar.*` to the matching
[GitHub Release](https://github.com/googolmo/imprint/releases/tag/v0.1.4) asset.

**Base URL:** https://repo-cr4.pages.dev/

Current Imprint release: `v0.1.4` → `https://github.com/googolmo/imprint/releases/download/v0.1.4/`

Connect this repository to **Cloudflare Pages** (build command empty, output
directory `/`) so `_redirects` is honoured. GitHub Pages cannot 302 `/pool`.

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
sudo apt install imprint
```

`ubuntu/repo.sources` uses suite `ubuntu22.04` (Debian 12+ / widest
glibc). Suites `ubuntu24.04` and `ubuntu26.04` exist for the newer-glibc
builds (amd64 and arm64). `Filename` in `Packages` is a per-file pool path
under `ubuntu/pool/github/`; Cloudflare 302s that exact file to `https://github.com/googolmo/imprint/releases/download/v0.1.4/`.

## Arch Linux (Pacman)

```bash
curl -fsSL https://repo-cr4.pages.dev/keys/repo.asc | sudo pacman-key --add -
sudo pacman-key --lsign-key 9DF42B7054F1CB5B
sudo curl -fsSL https://repo-cr4.pages.dev/pacman/repo.conf \
  -o /etc/pacman.d/repo
echo -e '\nInclude = /etc/pacman.d/repo' | sudo tee -a /etc/pacman.conf
sudo pacman -Sy imprint
```

`repo.db` and `repo.db.sig` are under `pacman/x86_64/` and
`pacman/aarch64/`. Each `.pkg.tar.zst` / `.pkg.tar.xz` is 302'd from
`/pacman/$arch/<file>` to `https://github.com/googolmo/imprint/releases/download/v0.1.4/`.

## Updating the index

Run **Actions → Update index → Run workflow**. Leave `tag` as `latest` (or
empty) to use the newest Imprint release, or pass a tag such as `v0.1.4`.
The job checks out `main`, calls `.github/scripts/update-index.py --apply`,
then commits and pushes to `main`. Imprint's Release workflow can dispatch
the same action.

```bash
gh workflow run update-index.yml -R googolmo/repo -f github_repo=googolmo/imprint -f tag=v0.1.4
```

Secrets on this repository:

| Secret | Role |
| --- | --- |
| `GPG_PRIVATE_KEY` | OpenPGP secret matching `keys/repo.asc`; signs APT `InRelease` and Pacman `repo.db` (overridden by `--gpg-private-key`) |
| `GPG_PASSPHRASE` | Optional passphrase for that key |

## Layout

```
.
├── _redirects                 one Cloudflare 302 per .deb / .pkg.tar.*
├── keys/
├── ubuntu/
│   ├── repo.sources
│   ├── repo.list
│   ├── dists/{ubuntu22.04,ubuntu24.04,ubuntu26.04}/
│   │   └── main/{binary-amd64,binary-arm64,source}/
│   └── pool/github/...        virtual; not stored, 302 per file
└── pacman/
    ├── repo.conf
    ├── x86_64/                repo.db + repo.db.sig
    └── aarch64/
```
