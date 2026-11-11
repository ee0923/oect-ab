#!/usr/bin/env python3
"""Replace the single release for an Armbian version with verified build assets."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

IMAGE = re.compile(r"Armbian_(?P<version>[0-9]+(?:\.[0-9]+)+(?:-[A-Za-z0-9.+-]+)?)_rockchip_wxy-oect_(?:bookworm|trixie)_6\.12\.[0-9]+_server_[0-9.]+\.img\.gz")
EVIDENCE = {"SHA256SUMS", "base-input.json", "build-record.json", "build.json",
            "build.log", "customize.sha256", "hardware-inputs.sha256", "sources.lock.json"}
MARKER = re.compile(r"<!-- oect-build: ([1-9][0-9]*) ([1-9][0-9]*) ([0-9a-f]{40}) -->")


class GitHub:
    def __init__(self, repository):
        self.root = f"repos/{repository}"

    def api(self, path, method="GET", data=None, missing=False):
        args = ["gh", "api", "--method", method, f"{self.root}/{path}"]
        if data is not None:
            args += ["--input", "-"]
        result = subprocess.run(args, input=json.dumps(data) if data is not None else None,
                                text=True, capture_output=True)
        if result.returncode:
            if missing and "(HTTP 404)" in result.stderr:
                return None
            raise RuntimeError(result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def releases(self):
        result = subprocess.check_output(
            ["gh", "api", "--paginate", "--slurp", f"{self.root}/releases?per_page=100"], text=True)
        return [release for page in json.loads(result) for release in page]

    def upload(self, tag, dist):
        subprocess.run(["gh", "release", "upload", tag, "--clobber",
                        *map(str, sorted(dist.iterdir()))], check=True)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inspect_dist(dist, identity):
    files = list(dist.iterdir())
    if any(not f.is_file() or f.is_symlink() or not f.stat().st_size for f in files):
        raise ValueError("Invalid or empty build asset")
    images = [f for f in files if IMAGE.fullmatch(f.name)]
    if len(images) != 1 or "_trixie_" not in images[0].name:
        raise ValueError("Expected exactly one OECT Debian 13 image")
    expected = EVIDENCE | {"privacy-audit.json", images[0].name}
    if {f.name for f in files} != expected:
        raise ValueError("Unexpected build assets")
    checksums = {}
    for line in (dist / "SHA256SUMS").read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9._+-]+)", line)
        if not match or match[2] in checksums:
            raise ValueError("Invalid checksum manifest")
        checksums[match[2]] = match[1]
    if checksums.keys() != expected - {"SHA256SUMS"}:
        raise ValueError("Incomplete checksum manifest")
    actual = {f.name: digest(f) for f in files}
    if any(actual[name] != value for name, value in checksums.items()):
        raise ValueError("Build checksum mismatch")
    record = json.loads((dist / "build-record.json").read_text())
    if any(record.get(key) != value for key, value in identity.items()):
        raise ValueError("Artifact does not belong to this run, attempt and commit")
    return IMAGE.fullmatch(images[0].name)["version"], actual


def legacy_version(release):
    """Only recognize complete, generated OECT build releases for migration."""
    if release.get("draft") or not re.fullmatch(r"build-[1-9][0-9]*-[1-9][0-9]*", release["tag_name"]):
        return None
    assets = release.get("assets", [])
    images = [IMAGE.fullmatch(a["name"]) for a in assets if IMAGE.fullmatch(a["name"])]
    names = {a["name"] for a in assets}
    if len(images) != 1 or len(names) != len(assets):
        return None
    if names not in (EVIDENCE | {images[0][0]}, EVIDENCE | {images[0][0], "privacy-audit.json"}):
        return None
    if not all(a["state"] == "uploaded" and a["size"] > 0 for a in assets):
        return None
    return images[0]["version"]


def verify_assets(release, dist, hashes):
    assets = release["assets"]
    if len(assets) != len(hashes) or {a["name"] for a in assets} != hashes.keys():
        raise ValueError("Release asset set differs from the verified build")
    for asset in assets:
        name = asset["name"]
        if (asset["state"] != "uploaded" or asset["size"] != (dist / name).stat().st_size
                or asset.get("digest") != f"sha256:{hashes[name]}"):
            raise ValueError(f"Release asset verification failed: {name}")


def publish(github, dist, identity):
    version, hashes = inspect_dist(dist, identity)
    tag = f"armbian-{version}"
    order = (int(identity["run_id"]), int(identity["run_attempt"]))
    releases = github.releases()
    stable = [r for r in releases if r["tag_name"] == tag]
    legacy = [r for r in releases if legacy_version(r) == version]
    release = stable[0] if stable else (max(legacy, key=lambda r: tuple(map(int, r["tag_name"].split("-")[1:]))) if legacy else None)
    if release:
        previous = MARKER.search(release.get("body") or "")
        previous_order = tuple(map(int, previous.group(1, 2))) if previous else (
            tuple(map(int, release["tag_name"].split("-")[1:])) if release in legacy else None)
        if previous_order is None:
            raise ValueError("Refusing to overwrite an unrecognized version release")
        if previous_order > order:
            raise ValueError("A newer build already owns this version release")
        if release.get("immutable"):
            raise ValueError("Version release is immutable; disable release immutability to allow replacement")
        # Hide the mutable release until every uploaded asset has been verified.
        github.api(f"releases/{release['id']}", "PATCH", {"draft": True})
    ref = github.api(f"git/ref/tags/{tag}", missing=True)
    if ref:
        github.api(f"git/refs/tags/{tag}", "PATCH", {"sha": identity["commit"], "force": True})
    else:
        github.api("git/refs", "POST", {"ref": f"refs/tags/{tag}", "sha": identity["commit"]})
    body = (f"OECT Armbian {version}, Debian 13 server, original wxy-oect, rk35xx 6.12.\n\n"
            "This release always contains the latest verified build for this Armbian version. "
            "Privacy patch revisions replace the assets; they are not new versions. "
            "Image and privacy inspection passed; hardware boot is not tested.\n\n"
            f"Build: https://github.com/{identity['repository']}/actions/runs/{identity['run_id']} "
            f"(attempt {identity['run_attempt']})\nCommit: `{identity['commit']}`\n\n"
            f"<!-- oect-build: {identity['run_id']} {identity['run_attempt']} {identity['commit']} -->")
    data = {"tag_name": tag, "target_commitish": identity["commit"],
            "name": f"OECT Armbian {version}", "body": body, "draft": True, "prerelease": False}
    release = github.api(f"releases/{release['id']}", "PATCH", data) if release else github.api("releases", "POST", data)
    # --clobber only replaces equal filenames; remove obsolete kernel/date filenames too.
    for asset in release["assets"]:
        if asset["name"] not in hashes:
            github.api(f"releases/assets/{asset['id']}", "DELETE")
    github.upload(tag, dist)
    verify_assets(github.api(f"releases/{release['id']}"), dist, hashes)
    github.api(f"releases/{release['id']}", "PATCH", {"draft": False, "make_latest": "true"})
    final = github.api(f"releases/{release['id']}")
    verify_assets(final, dist, hashes)
    if final["draft"] or github.api(f"git/ref/tags/{tag}")["object"]["sha"] != identity["commit"]:
        raise ValueError("Release publication or tag verification failed")
    # Consolidate only matching generated releases, after the replacement is public.
    for old in legacy:
        if old["id"] != release["id"]:
            github.api(f"releases/{old['id']}", "DELETE")
    print(f"Published {tag}: run {identity['run_id']} attempt {identity['run_attempt']}")


if __name__ == "__main__":
    identity = {key: os.environ[env] for key, env in (
        ("repository", "GITHUB_REPOSITORY"), ("commit", "GITHUB_SHA"),
        ("run_id", "GITHUB_RUN_ID"), ("run_attempt", "GITHUB_RUN_ATTEMPT"))}
    publish(GitHub(identity["repository"]), Path("dist"), identity)
