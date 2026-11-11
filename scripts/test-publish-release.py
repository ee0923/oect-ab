#!/usr/bin/env python3
"""Exercise publication state transitions without uploading firmware."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("publisher", Path(__file__).with_name("publish-release.py"))
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class FakeGitHub:
    def __init__(self):
        self.items = {}
        self.refs = {}
        self.next_id = 1
        self.events = []
        self.fail_upload = False
        self.corrupt_digest = False

    def releases(self):
        return copy.deepcopy(list(self.items.values()))

    def api(self, path, method="GET", data=None, missing=False):
        self.events.append((path, method, copy.deepcopy(data)))
        if path.startswith("git/ref/tags/"):
            sha = self.refs.get(path.removeprefix("git/ref/tags/"))
            return {"object": {"sha": sha}} if sha else None
        if path == "git/refs" or path.startswith("git/refs/tags/"):
            tag = data["ref"].removeprefix("refs/tags/") if path == "git/refs" else path.removeprefix("git/refs/tags/")
            self.refs[tag] = data["sha"]
            return {}
        if path == "releases":
            item = dict(data, id=self.next_id, assets=[])
            self.items[self.next_id] = item
            self.next_id += 1
            return copy.deepcopy(item)
        if path.startswith("releases/assets/"):
            for item in self.items.values():
                item["assets"] = [a for a in item["assets"] if a["id"] != int(path.split("/")[-1])]
            return None
        item_id = int(path.split("/")[-1])
        if method == "DELETE":
            del self.items[item_id]
            return None
        item = self.items[item_id]
        if method == "PATCH":
            item.update(data)
        return copy.deepcopy(item)

    def upload(self, tag, dist):
        item = next(r for r in self.items.values() if r["tag_name"] == tag)
        if not item["draft"]:
            raise AssertionError("Never expose a partially replaced release")
        self.events.append(("upload", tag, None))
        if self.fail_upload:
            raise RuntimeError("Upload interrupted")
        item["assets"] = [{"name": f.name, "id": item["id"] * 100 + n,
                           "state": "uploaded", "size": f.stat().st_size,
                           "digest": "sha256:" + ("0" * 64 if self.corrupt_digest else p.digest(f))}
                          for n, f in enumerate(sorted(dist.iterdir()))]


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dist = Path(self.temp.name)
        self.gh = FakeGitHub()
        self.identity = {"repository": "test/oect", "commit": "a" * 40,
                         "run_id": "100", "run_attempt": "1"}
        self.prepare()

    def prepare(self, version="26.11.0", kernel="69", date="2026.11.10"):
        for f in self.dist.iterdir():
            f.unlink()
        for name in p.EVIDENCE | {"privacy-audit.json"}:
            (self.dist / name).write_text("evidence\n")
        name = f"Armbian_{version}_rockchip_wxy-oect_trixie_6.12.{kernel}_server_{date}.img.gz"
        (self.dist / name).write_bytes(b"image fixture")
        (self.dist / "build-record.json").write_text(json.dumps(self.identity))
        self.checksums()

    def checksums(self):
        (self.dist / "SHA256SUMS").write_text("".join(
            f"{p.digest(f)}  {f.name}\n" for f in sorted(self.dist.iterdir()) if f.name != "SHA256SUMS"))

    def publish(self):
        p.publish(self.gh, self.dist, self.identity)

    def test_create_then_replace_same_version_and_changed_filename(self):
        self.publish()
        original_id = next(iter(self.gh.items))
        self.identity.update(commit="b" * 40, run_id="101")
        self.prepare(kernel="70", date="2026.11.11")
        self.publish()
        self.assertEqual(list(self.gh.items), [original_id])
        release = self.gh.items[original_id]
        self.assertEqual(release["tag_name"], "armbian-26.11.0")
        self.assertFalse(release["draft"])
        self.assertEqual(self.gh.refs[release["tag_name"]], "b" * 40)
        self.assertTrue(any(path.startswith("releases/assets/") and method == "DELETE"
                            for path, method, _ in self.gh.events))
        p.verify_assets(release, self.dist, {f.name: p.digest(f) for f in self.dist.iterdir()})

    def test_changed_armbian_version_is_separate(self):
        self.publish()
        self.prepare(version="26.12.0")
        self.publish()
        self.assertEqual({r["tag_name"] for r in self.gh.items.values()}, {"armbian-26.11.0", "armbian-26.12.0"})

    def test_checksum_failure_has_no_remote_mutations(self):
        (self.dist / "build.log").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self.publish()
        self.assertEqual(self.gh.events, [])

    def test_wrong_run_artifact_is_rejected(self):
        self.identity["run_attempt"] = "2"
        with self.assertRaisesRegex(ValueError, "does not belong"):
            self.publish()
        self.assertEqual(self.gh.events, [])

    def test_missing_checksum_entry_is_rejected(self):
        manifest = self.dist / "SHA256SUMS"
        manifest.write_text("\n".join(manifest.read_text().splitlines()[1:]) + "\n")
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            self.publish()

    def test_failed_upload_stays_draft_and_rerun_recovers(self):
        self.publish()
        self.identity["run_attempt"] = "2"
        self.prepare()
        self.gh.fail_upload = True
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.publish()
        self.assertTrue(self.gh.items[1]["draft"])
        self.gh.fail_upload = False
        self.publish()
        self.assertFalse(self.gh.items[1]["draft"])
        self.assertEqual(len(self.gh.items), 1)

    def test_server_digest_mismatch_never_publishes(self):
        self.gh.corrupt_digest = True
        with self.assertRaisesRegex(ValueError, "verification failed"):
            self.publish()
        self.assertTrue(self.gh.items[1]["draft"])

    def test_old_run_cannot_replace_newer_build(self):
        self.publish()
        self.identity["run_id"] = "99"
        self.prepare()
        self.gh.events.clear()
        with self.assertRaisesRegex(ValueError, "newer build"):
            self.publish()
        self.assertEqual(self.gh.events, [])

    def test_unknown_release_and_immutable_release_are_protected(self):
        self.publish()
        self.gh.items[1]["immutable"] = True
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.publish()
        self.gh.items[1]["immutable"] = False
        self.gh.items[1]["body"] = "manual release"
        with self.assertRaisesRegex(ValueError, "unrecognized"):
            self.publish()

    def legacy(self, tag, version="26.11.0"):
        # Generate realistic complete old releases using the same fixture assets.
        self.prepare(version=version)
        item = self.gh.api("releases", "POST", {"tag_name": tag, "draft": True, "body": "old release"})
        self.gh.upload(tag, self.dist)
        self.gh.items[item["id"]]["draft"] = False
        return item["id"]

    def test_migration_reuses_latest_and_only_deletes_matching_complete_releases(self):
        first = self.legacy("build-80-1")
        latest = self.legacy("build-90-1")
        other = self.legacy("build-70-1", "25.1.0")
        manual = self.legacy("manual")
        incomplete = self.legacy("build-95-1")
        self.gh.items[incomplete]["assets"].pop()
        self.prepare()
        self.gh.events.clear()
        self.publish()
        self.assertEqual(set(self.gh.items), {latest, other, manual, incomplete})
        self.assertEqual(self.gh.items[latest]["tag_name"], "armbian-26.11.0")
        delete = self.gh.events.index((f"releases/{first}", "DELETE", None))
        publish = next(i for i, (_, method, data) in enumerate(self.gh.events)
                       if method == "PATCH" and data.get("draft") is False)
        self.assertGreater(delete, publish)

    def test_failed_migration_does_not_delete_other_releases(self):
        first = self.legacy("build-80-1")
        latest = self.legacy("build-90-1")
        self.gh.fail_upload = True
        with self.assertRaises(RuntimeError):
            self.publish()
        self.assertEqual(set(self.gh.items), {first, latest})
        self.assertFalse(self.gh.items[first]["draft"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
