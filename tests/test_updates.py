import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build
import updates
from configlib import load_config


def config(package="libuv", version="1.2.3", dependencies=None):
    return {"name": package, "default_version": version,
            "source": {"url": "https://example.org/{name}-{version}.tar.gz"},
            "build": {"system": "cmake"}, "dependencies": dependencies or [],
            "updates": {"provider": "releases", "repository": "example/" + package,
                        "tag_pattern": r"v(\d+\.\d+\.\d+)"}}


def release(package, version, revision=1, draft=False):
    tag = f"{package}-{version}" + (f"-r{revision}" if revision > 1 else "")
    return {"tag_name": tag, "draft": draft, "assets": [
        {"name": name, "digest": "sha256:" + "a" * 64}
        for name in updates.archive_names(package, version).values()]}


class UpdateTests(unittest.TestCase):
    def test_stable_numeric_versions_and_lts_series(self):
        cfg = config("openssl", "3.5.7")
        cfg["updates"]["series"] = "3.5"
        entries = [{"tag_name": "v" + v} for v in ("3.5.9", "3.5.10", "3.6.0", "4.0.0", "3.5.11-beta1")]
        entries += [{"tag_name": "v3.5.11", "prerelease": True}, {"tag_name": "v3.5.12", "draft": True}]
        self.assertEqual(updates.latest_version(cfg, entries), "3.5.10")
        self.assertEqual(updates.latest_version(cfg, [{"tag_name": "v3.5.6"}]), "3.5.7")

    def test_dependency_order_and_cycles(self):
        configs = {"consumer": config("consumer", dependencies=[{"package": "dependency", "version": "1.0.0"}]),
                   "dependency": config("dependency")}
        self.assertEqual(updates.dependency_order(configs), ["dependency", "consumer"])
        configs["dependency"]["dependencies"] = [{"package": "consumer", "version": "1.0.0"}]
        with self.assertRaisesRegex(ValueError, "cycle"):
            updates.dependency_order(configs)
        del configs["dependency"]
        with self.assertRaisesRegex(ValueError, "Missing managed"):
            updates.dependency_order(configs)

    def test_revision_release_urls_keep_upstream_archive_version(self):
        dependency = {"package": "libxml2", "version": "2.15.3", "release_tag": "libxml2-2.15.3-r2"}
        target = build.TARGETS["arm64_v8a"]
        self.assertIn("/libxml2-2.15.3-r2/libxml2-2.15.3-aarch64-linux-android.tar.gz",
                      build.dependency_url(dependency, target))
        dependency["url"] = "https://example.org/{release_tag}/{package}-{version}-{triplet}.tar.gz"
        self.assertIn("libxml2-2.15.3-r2/libxml2-2.15.3-", build.dependency_url(dependency, target))

    def test_release_identity_and_incomplete_assets(self):
        self.assertEqual(updates.release_identity("libxslt", "libxslt-1.1.45-r12"), ("1.1.45", 12))
        self.assertIsNone(updates.release_identity("libxslt", "libxslt-1.1.45-rc1"))
        item = release("libuv", "1.2.3")
        self.assertTrue(updates.complete_release("libuv", "1.2.3", item))
        item["assets"].pop()
        self.assertFalse(updates.complete_release("libuv", "1.2.3", item))

    def test_resolved_json_config_and_manifest_provenance(self):
        cfg = config(dependencies=[{"package": "libxml2", "version": "2.15.3", "release_tag": "libxml2-2.15.3-r2"}])
        cfg["version"] = "1.2.3"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "build-config.json"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            self.assertEqual(load_config("libuv", path)[0], cfg)
            with self.assertRaises(SystemExit):
                load_config("openssl", path)
            build.write_manifest(cfg, "arm64_v8a", 24, Path(directory))
            manifest = json.loads((Path(directory) / "android-libs-manifest.json").read_text())
            self.assertEqual(manifest["dependencies"], cfg["dependencies"])

    def test_dependency_archive_checksum_is_target_specific(self):
        cfg = config(dependencies=[{"package": "libxml2", "version": "2.15.3",
                                    "sha256_by_target": {"arm64_v8a": "wrong"}}])
        with tempfile.TemporaryDirectory() as directory, patch.object(build, "download"), \
                patch.object(build, "sha256sum", return_value="actual"), patch.object(build, "extract_dependency_archive") as extract:
            with self.assertRaisesRegex(SystemExit, "sha256 mismatch"):
                build.install_dependency_archives(cfg, "arm64_v8a", Path(directory), Path(directory) / "install")
            extract.assert_not_called()

    def test_publish_dependency_then_consumer_and_skip_second_run(self):
        cfg_xml = config("libxml2", "2.15.3")
        cfg_xslt = config("libxslt", "1.1.45", [{"package": "libxml2", "version": "2.14.0"}])
        history = [release("libxslt", "1.1.45")]
        states, events = {}, []

        def fake_command(*args, **kwargs):
            if args[0] == sys.executable:
                effective = json.loads(Path(args[args.index("--config-file") + 1]).read_text())
                package, version = effective["name"], effective["version"]
                events.append(("build", package))
                if package == "libxslt":
                    self.assertIn(("publish", "libxml2-2.16.0"), events)
                    self.assertEqual(effective["dependencies"][0]["release_tag"], "libxml2-2.16.0")
                    self.assertEqual(set(effective["dependencies"][0]["sha256_by_target"]), set(build.TARGETS))
                for name in updates.archive_names(package, version).values():
                    (Path(args[args.index("--dist") + 1]) / name).write_bytes(b"built archive")
            elif args[:3] == ("gh", "release", "create"):
                tag = args[3]
                path = next(Path(a) for a in args if str(a).endswith(updates.STATE_ASSET))
                states[tag] = json.loads(path.read_text())
                self.assertIn("--draft", args)
                events.append(("draft", tag))
            elif args[:3] == ("gh", "release", "edit"):
                events.append(("publish", args[3]))
            else:
                raise AssertionError(args)

        with tempfile.TemporaryDirectory() as directory, patch.object(updates, "command", side_effect=fake_command), \
                patch.object(updates, "download_digest", return_value="b" * 64), \
                patch.dict(os.environ, {"GITHUB_SHA": "commit"}), \
                patch.object(updates, "read_state", side_effect=lambda r, _: states.get(r["tag_name"])):
            work = Path(directory)
            xml = updates.update_package("libxml2", cfg_xml, "2.16.0", {}, history, "owner/repo", work, False)
            xslt = updates.update_package("libxslt", cfg_xslt, "1.1.45", {"libxml2": xml}, history, "owner/repo", work, False)
            self.assertEqual(xslt["tag"], "libxslt-1.1.45-r2")
            for package, result in (("libxml2", xml), ("libxslt", xslt)):
                item = release(package, result["version"], 2 if package == "libxslt" else 1)
                item["assets"] = [{"name": name, **asset} for name, asset in result["assets"].items()] + [{"name": updates.STATE_ASSET}]
                history.append(item)
            previous_events = events.copy()
            updates.update_package("libxml2", cfg_xml, "2.16.0", {}, history, "owner/repo", work, False)
            updates.update_package("libxslt", cfg_xslt, "1.1.45", {"libxml2": xml}, history, "owner/repo", work, False)
            self.assertEqual(events, previous_events)
            # A dependency revision alone triggers a new consumer revision.
            xml["tag"] += "-r2"
            result = updates.update_package("libxslt", cfg_xslt, "1.1.45", {"libxml2": xml}, history, "owner/repo", work / "next", True)
            self.assertEqual(result["tag"], "libxslt-1.1.45-r3")

    def test_failed_build_never_publishes(self):
        def fail_build(*args, **kwargs):
            if args[0] == sys.executable:
                raise subprocess.CalledProcessError(1, args)
            self.fail("Unexpected publish command")
        with tempfile.TemporaryDirectory() as directory, patch.object(updates, "command", side_effect=fail_build), \
                patch.object(updates, "download_digest", return_value="b" * 64):
            with self.assertRaises(subprocess.CalledProcessError):
                updates.update_package("libuv", config(), "1.2.4", {}, [], "owner/repo", Path(directory), False)

    def test_source_checksum_mismatch_prevents_build(self):
        cfg = config()
        cfg["source"]["sha256_by_version"] = {"1.2.3": "a" * 64}
        with tempfile.TemporaryDirectory() as directory, patch.object(updates, "command") as command, \
                patch.object(updates, "download_digest", return_value="b" * 64):
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                updates.update_package("libuv", cfg, "1.2.3", {}, [], "owner/repo", Path(directory), False)
            command.assert_not_called()

    def test_failed_dependency_blocks_consumer_but_not_independent_package(self):
        configs = {"libxml2": config("libxml2"), "libxslt": config("libxslt", dependencies=[{"package": "libxml2", "version": "1.2.3"}]),
                   "openssl": config("openssl")}
        calls = []

        def update(package, *args):
            calls.append(package)
            if package == "libxml2":
                raise ValueError("compile failed")
            return {"tag": "openssl-1.2.3"}

        with tempfile.TemporaryDirectory() as directory, patch.object(updates, "CONFIG_DIR") as config_dir, \
                patch.object(updates, "load_config", side_effect=lambda p: (configs[p], None)), \
                patch.object(updates, "api_pages", return_value=[{"tag_name": "v1.2.3"}]), \
                patch.object(updates, "update_package", side_effect=update), \
                patch.object(sys, "argv", ["updates.py", "--repository", "owner/repo", "--output", directory]), \
                patch.dict(os.environ), contextlib.redirect_stderr(io.StringIO()):
            config_dir.glob.return_value = [Path(p + ".toml") for p in configs]
            self.assertEqual(updates.main(), 1)
            self.assertEqual(calls, ["libxml2", "openssl"])
            report = json.loads((Path(directory) / "update-report.json").read_text())
            self.assertIn("Blocked", report["errors"]["libxslt"])
            self.assertIn("openssl", report["resolved"])

    def test_selected_consumer_uses_dependency_job_result(self):
        configs = {"libxml2": config("libxml2"), "libxslt": config("libxslt", dependencies=[{"package": "libxml2", "version": "1.2.3"}])}
        upstream = {"libxml2": {"version": "2.15.4", "tag": "libxml2-2.15.4", "assets": {}}}
        with tempfile.TemporaryDirectory() as directory, patch.object(updates, "CONFIG_DIR") as config_dir, \
                patch.object(updates, "load_config", side_effect=lambda p: (configs[p], None)), \
                patch.object(updates, "api_pages", return_value=[{"tag_name": "v1.2.3"}]) as api, \
                patch.object(updates, "update_package", return_value={"tag": "libxslt-1.2.3"}) as update, \
                patch.dict(os.environ):
            dependencies = Path(directory) / "dependencies.json"
            dependencies.write_text(json.dumps(upstream))
            config_dir.glob.return_value = [Path(p + ".toml") for p in configs]
            args = ["updates.py", "--repository", "owner/repo", "--package", "libxslt", "--output", directory,
                    "--dependencies-file", str(dependencies), "--dry-run"]
            with patch.object(sys, "argv", args):
                self.assertEqual(updates.main(), 0)
            self.assertEqual(update.call_count, 1)
            self.assertEqual(update.call_args.args[0], "libxslt")
            self.assertEqual(update.call_args.args[3]["libxml2"], upstream["libxml2"])
            self.assertEqual(api.call_count, 2)  # own releases and selected upstream only
            update.reset_mock()
            dependencies.write_text("{}")
            with patch.object(sys, "argv", args), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(updates.main(), 1)
            update.assert_not_called()

    def test_local_patch_selection_preserves_manual_older_builds(self):
        cfg, _ = load_config("libuv")
        with tempfile.TemporaryDirectory() as directory, patch.object(build, "download_termux_patches", return_value=[]):
            cfg["version"] = "1.52.1"
            self.assertEqual(build.package_patches(cfg, Path(directory)), [])
            cfg["version"] = "1.53.0"
            selected = build.package_patches(cfg, Path(directory))
            self.assertEqual(len(selected), 1)
            self.assertTrue(selected[0].is_file())


if __name__ == "__main__":
    unittest.main()
