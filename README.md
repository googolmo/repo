# Linux package repository

GitHub Pages host for an APT (Debian / Ubuntu) source and a Pacman (Arch Linux) source, plus the OpenPGP public key used to verify package signatures.

**Base URL:** https://googolmo.github.io/repo/

Enable GitHub Pages for this repository (Deploy from branch: `main` / `/`) so the files below are served from that URL.

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

## Debian / Ubuntu (APT)

Install the keyring, then add the DEB822 source file:

```bash
sudo mkdir -p /usr/share/keyrings
sudo curl -fsSL https://googolmo.github.io/repo/keys/repo.gpg \
  -o /usr/share/keyrings/repo-archive-keyring.gpg
sudo chmod 644 /usr/share/keyrings/repo-archive-keyring.gpg
sudo curl -fsSL https://googolmo.github.io/repo/ubuntu/repo.sources \
  -o /etc/apt/sources.list.d/repo.sources
sudo apt update
```

On older APT setups, install the one-line list instead:

```bash
sudo curl -fsSL https://googolmo.github.io/repo/ubuntu/repo.list \
  -o /etc/apt/sources.list.d/repo.list
sudo apt update
```

The source points at:

```
deb https://googolmo.github.io/repo/ubuntu stable main
```

Packages belong under `ubuntu/pool/` with metadata under `ubuntu/dists/stable/`.

## Arch Linux (Pacman)

Import and locally sign the key, then include the repository snippet:

```bash
curl -fsSL https://googolmo.github.io/repo/keys/repo.asc | sudo pacman-key --add -
sudo pacman-key --lsign-key 91FDA44879208693204E90EE9DF42B7054F1CB5B
sudo curl -fsSL https://googolmo.github.io/repo/pacman/repo.conf \
  -o /etc/pacman.d/repo
echo -e '\nInclude = /etc/pacman.d/repo' | sudo tee -a /etc/pacman.conf
sudo pacman -Sy
```

The snippet adds:

```ini
[repo]
SigLevel = Required DatabaseOptional
Server = https://googolmo.github.io/repo/pacman/$arch
```

Place packages in `pacman/x86_64/` or `pacman/aarch64/`.

## Layout

```
.
├── index.html
├── 404.html
├── keys/
│   ├── repo.asc
│   └── repo.gpg
├── ubuntu/
│   ├── repo.sources
│   ├── repo.list
│   ├── dists/stable/
│   └── pool/main/
└── pacman/
    ├── repo.conf
    ├── x86_64/
    └── aarch64/
```
