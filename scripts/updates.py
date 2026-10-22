#!/usr/bin/env python3
"""Resolve upstream updates, then build and release in dependency order."""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.request

from build import ROOT, TARGETS, render_template, sha256sum
from configlib import CONFIG_DIR, load_config, resolve_config_version, source_sha256

STATE_ASSET = "release-state.json"


def command(*args, capture=False):
    result = subprocess.run(args, cwd=ROOT, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout if capture else None


def api_pages(endpoint):
    pages = json.loads(command("gh", "api", "--paginate", "--slurp", endpoint, capture=True))
    return [item for page in pages for item in page]


def version_key(value):
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError(f"Expected a stable three-part version, got {value!r}")
    return tuple(map(int, value.split(".")))


def latest_version(config, entries):
    settings = config["updates"]
    pattern = re.compile(settings["tag_pattern"])
    candidates = []
    for entry in entries:
        if entry.get("draft") or entry.get("prerelease"):
            continue
        match = pattern.fullmatch(entry.get("tag_name", entry.get("name", "")))
        if not match:
            continue
        version = match.group(1)
        version_key(version)
        if settings.get("series") and not version.startswith(settings["series"] + "."):
            continue
        candidates.append(version)
    if not candidates:
        raise ValueError(f"{config['name']}: no matching stable upstream versions")
    # Never downgrade a configured baseline when a mirror is behind.
    baseline = config.get("default_version")
    if baseline:
        candidates.append(baseline)
    return max(candidates, key=version_key)


def release_identity(package, tag):
    match = re.fullmatch(re.escape(package) + r"-(\d+\.\d+\.\d+)(?:-r([2-9]\d*|1\d+))?", tag)
    return (match.group(1), int(match.group(2) or 1)) if match else None


def package_releases(package, releases):
    found = [(release_identity(package, r["tag_name"]), r) for r in releases]
    return sorted(((identity, r) for identity, r in found if identity),
                  key=lambda pair: (version_key(pair[0][0]), pair[0][1]), reverse=True)


def dependency_order(configs):
    result, visiting, visited = [], set(), set()

    def visit(package):
        if package in visiting:
            raise ValueError(f"Dependency cycle involving {package}")
        if package in visited:
            return
        if package not in configs:
            raise ValueError(f"Missing managed config for dependency {package}")
        visiting.add(package)
        for dependency in configs[package].get("dependencies", []):
            visit(dependency["package"])
        visiting.remove(package)
        visited.add(package)
        result.append(package)

    for package in sorted(configs):
        visit(package)
    return result


def recipe_key(config):
    recipe = deepcopy(config)
    for key in ("default_version", "version", "release_tag", "updates"):
        recipe.pop(key, None)
    # Checksums are recorded separately; the recipe must not change merely
    # because a version-specific checksum was added to the source TOML.
    recipe["source"].pop("sha256", None)
    recipe["source"].pop("sha256_by_version", None)
    return hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest()


def read_state(release, directory):
    if not any(a["name"] == STATE_ASSET for a in release["assets"]):
        return None
    command("gh", "release", "download", release["tag_name"], "--pattern", STATE_ASSET,
            "--dir", str(directory), "--clobber")
    return json.loads((directory / STATE_ASSET).read_text(encoding="utf-8"))


def archive_names(package, version, release_tag=None):
    tag = release_tag or f"{package}-{version}"
    return {target: f"{tag}-{info['triplet']}.tar.gz"
            for target, info in TARGETS.items()}


def published_archive_names(package, version, tag, assets):
    # Prefer revision-bearing assets, but continue to consume old releases.
    for names in (archive_names(package, version, tag), archive_names(package, version)):
        if all(name in assets for name in names.values()):
            return names
    raise ValueError(f"{tag}: missing target archives")


def complete_release(package, version, release):
    names = {asset["name"] for asset in release["assets"]}
    if release["draft"] or release.get("prerelease"):
        return False
    try:
        published_archive_names(package, version, release["tag_name"], names)
    except ValueError:
        return False
    return True


def download_digest(url):
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=120) as response:
        for chunk in iter(lambda: response.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dependency_config(dependency, resolved, repository, dry_run):
    result = deepcopy(dependency)
    result.update(version=resolved["version"], release_tag=resolved["tag"])
    names = archive_names(dependency["package"], resolved["version"], resolved["tag"])
    if resolved["assets"]:
        names = published_archive_names(dependency["package"], resolved["version"], resolved["tag"], resolved["assets"])
    prefix = ("{release_tag}" if names == archive_names(dependency["package"], resolved["version"], resolved["tag"])
              else "{package}-{version}")
    result["url"] = (f"https://github.com/{repository}/releases/download/"
                     "{release_tag}/" + prefix + "-{triplet}.tar.gz")
    result.pop("sha256", None)
    checksums = {}
    for target, name in names.items():
        if dry_run:
            continue
        asset = resolved["assets"][name]
        digest = asset.get("digest") or ""
        checksums[target] = (digest.removeprefix("sha256:") if digest.startswith("sha256:")
                             else download_digest(asset["browser_download_url"]))
    if not dry_run:
        result["sha256_by_target"] = checksums
    else:
        # Dry runs compare dependency identity without downloading archives.
        result.pop("sha256_by_target", None)
    return result


def matching_state(state, version, config, dry_run):
    if not state or state["version"] != version:
        return False
    if dry_run:
        previous = deepcopy(state["config"])
        for dependency in previous.get("dependencies", []):
            dependency.pop("sha256_by_target", None)
        return recipe_key(previous) == recipe_key(config)
    return state["recipe_sha256"] == recipe_key(config)


def update_package(package, raw_config, version, resolved, releases, repository, work, dry_run):
    config = resolve_config_version(raw_config, version)
    config["default_version"] = version
    local_patches = config["build"].get("patches_by_version", {}).get(version, [])
    if local_patches:
        config["build"]["patch_sha256"] = {filename: sha256sum(ROOT / filename) for filename in local_patches}
    config["dependencies"] = [dependency_config(d, resolved[d["package"]], repository, dry_run)
                              for d in config.get("dependencies", [])]
    history = package_releases(package, releases)
    # A previous published release may be ahead of an upstream mirror.
    published = [(identity, r) for identity, r in history
                 if complete_release(package, identity[0], r)]
    if published and version_key(published[0][0][0]) > version_key(version):
        raise ValueError(f"{package}: upstream discovery is behind published {published[0][0][0]}")
    for (released_version, _), release in published:
        if released_version != version:
            continue
        state = read_state(release, work)
        # Legacy standalone releases can be reused; dependency consumers are
        # rebuilt once to establish their exact dependency provenance.
        if matching_state(state, version, config, dry_run) or (state is None and not config["dependencies"]):
            print(f"{package}: up to date ({release['tag_name']})", flush=True)
            return {"version": version, "tag": release["tag_name"],
                    "assets": {a["name"]: a for a in release["assets"]}}
        # Never roll back to an older recipe that happens to match.
        break
    revision = 1 + max((identity[1] for identity, _ in history if identity[0] == version), default=0)
    tag = f"{package}-{version}" + (f"-r{revision}" if revision > 1 else "")
    print(f"{package}: {'would release' if dry_run else 'building'} {tag}", flush=True)
    if dry_run:
        return {"version": version, "tag": tag, "assets": {}}

    config["release_tag"] = tag
    directory = work / package
    directory.mkdir()
    # Pin the exact downloaded source for both target builds. A known upstream
    # checksum must still match; a first-seen hash provides reproducibility,
    # not independent upstream authentication.
    actual = download_digest(render_template(config["source"]["url"], config))
    expected = source_sha256(config)
    if expected and actual != expected:
        raise ValueError(f"{package}: source checksum mismatch: expected {expected}, got {actual}")
    config["source"].setdefault("sha256_by_version", {})[version] = actual
    config_file = directory / "build-config.json"
    config_file.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    command(sys.executable, "scripts/build.py", "--package", package, "--version", version,
            "--config-file", str(config_file), "--api", "24", "--dist", str(directory))
    assets = {}
    for name in archive_names(package, version, tag).values():
        path = directory / name
        if not path.is_file():
            raise ValueError(f"Missing build archive {path}")
        assets[name] = {"digest": "sha256:" + sha256sum(path)}
    state = {"schema": 1, "package": package, "version": version, "tag": tag,
             "recipe_sha256": recipe_key(config), "config": config,
             "commit": os.environ.get("GITHUB_SHA") or command("git", "rev-parse", "HEAD", capture=True).strip(),
             "assets": assets}
    state_file = directory / STATE_ASSET
    state_file.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    notes_file = directory / "release-notes.md"
    dependencies = ", ".join(d["release_tag"] for d in config["dependencies"]) or "none"
    notes_file.write_text(f"Static Android API 24 libraries for {package} {version}.\n\n"
                          f"Dependencies: {dependencies}.\n\n"
                          "Resolved build configuration and checksums are attached.\n", encoding="utf-8")
    # Draft first: consumers cannot see a published release with half its assets.
    command("gh", "release", "create", tag, *[str(directory / name) for name in assets],
            str(config_file), str(state_file), "--draft", "--target", state["commit"],
            "--title", tag, "--notes-file", str(notes_file))
    command("gh", "release", "edit", tag, "--draft=false", "--latest=false")
    return {"version": version, "tag": tag, "assets": assets}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Discover updates without builds or publishing")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"), help="owner/repo to publish to")
    parser.add_argument("--output", default="dist/updates", help="Build output and diagnostic directory")
    parser.add_argument("--package", help="Update one package; dependency results must be supplied separately")
    parser.add_argument("--dependencies-file", help="JSON map of dependency results from preceding jobs")
    args = parser.parse_args()
    repository = args.repository or command("gh", "repo", "view", "--json", "nameWithOwner",
                                           "--jq", ".nameWithOwner", capture=True).strip()
    os.environ["GH_REPO"] = repository
    configs = {p.stem: load_config(p.stem)[0] for p in sorted(CONFIG_DIR.glob("*.toml"))}
    order = dependency_order(configs)
    if args.package:
        if args.package not in configs:
            parser.error(f"Unknown package: {args.package}")
        order = [args.package]
    releases = api_pages(f"repos/{repository}/releases?per_page=100")
    versions, errors, resolved = {}, {}, {}
    if args.dependencies_file:
        resolved = json.loads(Path(args.dependencies_file).read_text(encoding="utf-8"))
    for package in order:
        try:
            settings = configs[package]["updates"]
            entries = api_pages(f"repos/{settings['repository']}/{settings.get('provider', 'releases')}?per_page=100")
            versions[package] = latest_version(configs[package], entries)
        except (KeyError, ValueError, OSError, subprocess.CalledProcessError) as exc:
            errors[package] = str(exc)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Isolate each invocation so stale artifacts cannot be published on a retry.
    for package in order:
        blocked = [d["package"] for d in configs[package].get("dependencies", [])
                   if d["package"] not in resolved]
        if blocked:
            errors[package] = f"Blocked by failed dependencies: {', '.join(blocked)}"
        if package in errors:
            continue
        try:
            run_dir = Path(tempfile.mkdtemp(prefix=package + "-", dir=output))
            resolved[package] = update_package(package, configs[package], versions[package], resolved,
                                               releases, repository, run_dir, args.dry_run)
        except (ValueError, OSError, subprocess.CalledProcessError, SystemExit) as exc:
            errors[package] = str(exc)
    report = {"dry_run": args.dry_run, "resolved": resolved, "errors": errors}
    (output / "update-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for package, error in errors.items():
        print(f"ERROR {package}: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
