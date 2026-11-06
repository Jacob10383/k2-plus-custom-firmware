#!/usr/bin/env python3
"""Build the GitHub release that install.py and bootstrap --update download.

The release holds rootfs.ext2, kernel.img, swap, bootstrap and install.py.
`index` (written by release.py) pins the SHA-256 and size of the first four.
rootfs and kernel come from R2 by SHA, not from Git LFS, which bills bandwidth.
The tag is a hash of all five files, so it names the release's content.

  release_assets.py tag REPO          print the tag for a checkout
  release_assets.py collect REPO OUT  fetch and verify the files into OUT,
                                      then print tag=<tag> for $GITHUB_OUTPUT
"""

import hashlib
import json
import shutil
import sys
import urllib.request
from pathlib import Path

R2_BASE_URL = "https://firmware.jacobean.xyz"
# Release file -> its entry in index; None when the file is taken as committed.
FILES = {
    "rootfs.ext2": "rootfs",
    "kernel.img": "kernel",
    "swap": "swap",
    "bootstrap": "bootstrap",
    "install.py": None,
}
FROM_R2 = {"rootfs.ext2", "kernel.img"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_index(repo: Path) -> dict:
    index = json.loads((repo / "index").read_text())
    if index.get("format") != "k2-release-index-v1":
        raise SystemExit("index has an unsupported format")
    return index


def pinned(index: dict, name: str) -> tuple[str, int]:
    entry = index[FILES[name]]
    return entry["sha256"], entry["size"]


def check(path: Path, name: str, index: dict) -> None:
    expected = pinned(index, name)
    actual = (sha256(path), path.stat().st_size)
    if actual != expected:
        raise SystemExit(
            f"{name} is {actual[1]} bytes, sha256 {actual[0]}; "
            f"index pins {expected[1]} bytes, sha256 {expected[0]}"
        )


def release_tag(repo: Path) -> str:
    index = load_index(repo)
    digests = {}
    for name, key in FILES.items():
        if name in FROM_R2:
            digests[name] = pinned(index, name)[0]
        else:
            if key:
                check(repo / name, name, index)
            digests[name] = sha256(repo / name)
    content = hashlib.sha256(json.dumps(digests, sort_keys=True).encode()).hexdigest()
    return f"{index['version']}-{content[:12]}"


def collect(repo: Path, out: Path) -> str:
    index = load_index(repo)
    tag = release_tag(repo)
    out.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        dest = out / name
        if name in FROM_R2:
            url = f"{R2_BASE_URL}/{index['version']}/o/{pinned(index, name)[0]}"
            print(f"fetching {name} from {url}", file=sys.stderr)
            with urllib.request.urlopen(url, timeout=300) as response, dest.open("wb") as handle:
                shutil.copyfileobj(response, handle)
            check(dest, name, index)
        else:
            shutil.copyfile(repo / name, dest)
        print(f"{name}: {dest.stat().st_size} bytes, sha256 {sha256(dest)}", file=sys.stderr)
    return tag


def main() -> None:
    command, repo = sys.argv[1], Path(sys.argv[2])
    if command == "tag":
        print(release_tag(repo))
    elif command == "collect":
        print(f"tag={collect(repo, Path(sys.argv[3]))}")
    else:
        raise SystemExit(f"unknown command {command!r}")


if __name__ == "__main__":
    main()
