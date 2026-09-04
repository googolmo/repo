# Linux package repository

APT (Debian / Ubuntu) and Pacman (Arch Linux) **indexes** for Imprint, served
from Cloudflare Pages. Package files are **not** stored in this git tree:
Cloudflare 302s `.deb` (and Pacman `.pkg.tar.zst`) downloads to the matching
Imprint [GitHub Release](https://github.com/googolmo/imprint/releases) asset.

**Base URL:** https://googolmo.github.io/repo/

Connect this repository to **Cloudflare Pages** (build command empty, output
directory `/`) so `_redirects` is honoured. GitHub Pages cannot 302 `/pool`.

The `update-index` GitHub Action rebuilds `ubuntu/dists/*/Packages`, signs
`InRelease` with `GPG_PRIVATE_KEY`, rewrites `_redirects` + `pacman/repo.conf`,
and commits. Imprint's Release workflow dispatches that action when a tag
build finishes.

## Public key

| File | Format |
| --- | --- |
| [keys/repo.asc](https://googolmo.github.io/repo/keys/repo.asc) | ASCII-armored |
| [keys/repo.gpg](https://googolmo.github.io/repo/keys/repo.gpg) | Binary keyring |

- **Fingerprint:** `91FD A448 7920 8693 204E  90EE 9DF4 2B70 54F1 CB5B`
- **Key ID:** `9DF42B7054F1CB5B`

Verify after download:

```bash
gpg --show-keys --with-fingerprint keys/repo.asc
```

`update-index` signs `ubuntu/dists/*/InRelease` with the `GPG_PRIVATE_KEY`
Actions secret (must match `keys/repo.asc`). Optional `GPG_PASSPHRASE` if the
key is encrypted.

## Debian / Ubuntu (APT)

```bash
sudo mkdir -p /usr/share/keyrings
sudo curl -fsSL https://googolmo.github.io/repo/keys/repo.gpg \
  -o /usr/share/keyrings/repo-archive-keyring.gpg
sudo chmod 644 /usr/share/keyrings/repo-archive-keyring.gpg
sudo curl -fsSL https://googolmo.github.io/repo/ubuntu/repo.sources \
  -o /etc/apt/sources.list.d/repo.sources
sudo apt update
sudo apt install imprint
```

On older APT setups, install the one-line list instead:

```bash
sudo curl -fsSL https://googolmo.github.io/repo/ubuntu/repo.list \
  -o /etc/apt/sources.list.d/repo.list
sudo apt update
```

`ubuntu/repo.sources` uses suite `stable` (Ubuntu 22.04 / Debian 12+ `.deb`).
Suites `ubuntu24.04` and `ubuntu26.04` exist for the newer-glibc builds.
`Filename` in `Packages` is under `ubuntu/pool/github/`; Cloudflare redirects
it to `https://github.com/googolmo/imprint/releases/download/<tag>/`.

## Arch Linux (Pacman)

```bash
sudo curl -fsSL https://googolmo.github.io/repo/pacman/repo.conf \
  -o /etc/pacman.d/repo
echo -e '\nInclude = /etc/pacman.d/repo' | sudo tee -a /etc/pacman.conf
sudo pacman -Sy imprint
```

`repo.db` is under `pacman/x86_64/` and `pacman/aarch64/`. The `.pkg.tar.zst`
is downloaded from the Imprint GitHub Release (`pacman/repo.conf` lists that
URL as a second `Server=`).

## Updating the index

From [googolmo/imprint](https://github.com/googolmo/imprint) after a tag
release, or manually:

```bash
gh workflow run update-index.yml -R googolmo/repo \
  -f tag=v0.1.4 -f github_repo=googolmo/imprint
```

Secrets on this repository:

| Secret | Role |
| --- | --- |
| `GPG_PRIVATE_KEY` | OpenPGP secret matching `keys/repo.asc`; signs APT `InRelease` |
| `GPG_PASSPHRASE` | Optional passphrase for that key |

Imprint needs `LINUX_REPO_TOKEN` (a PAT that can dispatch workflows on this
repository) to trigger `update-index`.

## Layout

```
.
├── _redirects                 Cloudflare 302s for .deb / .pkg.tar.zst
├── index.html
├── 404.html
├── keys/
│   ├── repo.asc
│   └── repo.gpg
├── ubuntu/
│   ├── repo.sources
│   ├── repo.list
│   └── dists/{stable,ubuntu22.04,ubuntu24.04,ubuntu26.04}/
└── pacman/
    ├── repo.conf
    ├── x86_64/                repo.db only
    └── aarch64/
```
