#!/usr/bin/env python3
"""Resolve an ophub server base, verify its digest, and record exact input evidence."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
BASE_NAME = re.compile(r"Armbian_[A-Za-z0-9._+-]+-trunk_[A-Za-z0-9._+-]+\.img\.gz")


def api(path):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "oect-build"}
    if os.environ.get("GH_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GH_TOKEN"]
    request = urllib.request.Request("https://api.github.com/repos/ophub/amlogic-s9xxx-armbian/" + path,
                                     headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def select_asset(releases):
    candidates = [asset for release in releases
                  if not release["draft"] and not release["prerelease"]
                  and release["tag_name"].startswith("Armbian_bookworm_arm64_server_")
                  for asset in release["assets"] if BASE_NAME.fullmatch(asset["name"])]
    if not candidates:
        return None
    return max(candidates, key=lambda asset: (asset["updated_at"], asset["id"]))


def main():
    config = json.loads((ROOT / "build.json").read_text())
    url, expected = config["base_image_url"], config["base_image_sha256"]
    evidence = {"selection": "explicit"}
    if not url and not expected:
        releases = []
        for page in range(1, 11):
            batch = api(f"releases?per_page=100&page={page}")
            releases.extend(batch)
            if len(batch) < 100:
                break
        else:
            raise SystemExit("Release pagination limit reached; configure an exact base URL and SHA256")
        asset = select_asset(releases)
        if asset is None:
            raise SystemExit("No Debian 12 ARM64 server base found; configure an exact base URL and SHA256")
        url = asset["browser_download_url"]
        digest = asset.get("digest") or ""
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise SystemExit("Selected base lacks a GitHub SHA256 digest; configure an exact URL and SHA256")
        expected = digest.removeprefix("sha256:")
        evidence = {"selection": "release-api", "asset_id": asset["id"], "updated_at": asset["updated_at"]}
    parsed = urllib.parse.urlsplit(url)
    name = Path(parsed.path).name
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not BASE_NAME.fullmatch(name) or not re.fullmatch(r"[0-9a-f]{64}", expected)):
        raise SystemExit("Invalid base URL/name/SHA256; use a public HTTPS -trunk_ .img.gz base")
    images = ROOT / "source/build/output/images"
    images.mkdir(parents=True, exist_ok=False)
    output = images / name
    subprocess.run(["curl", "--proto", "=https", "--proto-redir", "=https", "--fail", "--location",
                    "--retry", "3", "--connect-timeout", "30", "--max-time", "1800",
                    "--output", str(output), url], check=True)
    with output.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected:
        output.unlink()
        raise SystemExit("Base image SHA256 mismatch")
    evidence.update(url=url, name=name, sha256=actual)
    (ROOT / "base-input.json").write_text(json.dumps(evidence, indent=2) + "\n")
    subprocess.run(["gzip", "-d", str(output)], check=True)


if __name__ == "__main__":
    main()
