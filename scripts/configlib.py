import json
import re
from copy import deepcopy
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "configs"
REQUIRED_TOP_LEVEL = ("name", "source", "build")


def fail(message):
    msg = f"error: {message}"
    raise SystemExit(msg)


def config_path(package):
    return CONFIG_DIR / f"{package}.toml"


def load_config(package, config_file=None):
    path = Path(config_file) if config_file else config_path(package)
    if not path.is_file():
        fail(f"config not found: {path}")

    if config_file:
        config = json.loads(path.read_text(encoding="utf-8"))
    else:
        with path.open("rb") as fh:
            config = tomllib.load(fh)

    validate_config(config, path)
    if config["name"] != package:
        fail(f"{path}: config name must match package {package!r}")
    return config, path


def validate_config(config, path):
    for key in REQUIRED_TOP_LEVEL:
        if key not in config:
            fail(f"{path}: missing required field '{key}'")

    if config["build"].get("system") not in {"cmake", "openssl", "autotools"}:
        fail(f"{path}: build.system must be 'cmake', 'openssl', or 'autotools'")

    if "url" not in config["source"]:
        fail(f"{path}: source.url is required")

    updates = config.get("updates")
    if updates is not None:
        if not isinstance(updates, dict) or updates.get("provider") not in {"tags", "releases"}:
            fail(f"{path}: updates.provider must be 'tags' or 'releases'")
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", str(updates.get("repository", ""))):
            fail(f"{path}: updates.repository must be owner/repo")
        try:
            pattern = re.compile(updates["tag_pattern"])
        except (KeyError, TypeError, re.error):
            fail(f"{path}: updates.tag_pattern must be a valid regular expression")
        if pattern.groups != 1:
            fail(f"{path}: updates.tag_pattern must capture exactly one version")
        if "series" in updates and not re.fullmatch(r"\d+\.\d+", str(updates["series"])):
            fail(f"{path}: updates.series must be major.minor")

    sha256 = config["source"].get("sha256", "")
    if sha256 and not isinstance(sha256, str):
        fail(f"{path}: source.sha256 must be a string")

    sha256_by_version = config["source"].get("sha256_by_version", {})
    if not isinstance(sha256_by_version, dict):
        fail(f"{path}: source.sha256_by_version must be a table")
    for version, digest in sha256_by_version.items():
        if not isinstance(version, str) or not isinstance(digest, str):
            fail(f"{path}: source.sha256_by_version entries must be string = string")

    patches = config.get("termux", {}).get("patches", [])
    if not isinstance(patches, list):
        fail(f"{path}: termux.patches must be a list")

    if patches and not config.get("termux", {}).get("package"):
        fail(f"{path}: termux.package is required when termux.patches is not empty")

    defines = config["build"].get("defines", {})
    local_patches = config["build"].get("patches_by_version", {})
    if not isinstance(local_patches, dict) or not all(
        isinstance(version, str) and isinstance(files, list) and all(isinstance(f, str) for f in files)
        for version, files in local_patches.items()
    ):
        fail(f"{path}: build.patches_by_version must map version strings to lists of patch paths")
    if not isinstance(defines, dict):
        fail(f"{path}: build.defines must be a table")

    options = config["build"].get("options", {})
    if not isinstance(options, dict):
        fail(f"{path}: build.options must be a table")
    configure_flags = options.get("configure_flags", [])
    if not isinstance(configure_flags, list) or not all(isinstance(f, str) for f in configure_flags):
        fail(f"{path}: build.options.configure_flags must be a list of strings")

    dependencies = config.get("dependencies", [])
    if not isinstance(dependencies, list):
        fail(f"{path}: dependencies must be a list of tables")
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            fail(f"{path}: dependencies entries must be tables")
        if not dependency.get("package"):
            fail(f"{path}: dependencies entries require a package")
        if not isinstance(dependency.get("version"), str):
            fail(f"{path}: dependencies entries require a string version")
        if "url" in dependency and not isinstance(dependency["url"], str):
            fail(f"{path}: dependencies.url must be a string")
        if "sha256" in dependency and not isinstance(dependency["sha256"], str):
            fail(f"{path}: dependencies.sha256 must be a string")
        if "release_tag" in dependency and not isinstance(dependency["release_tag"], str):
            fail(f"{path}: dependencies.release_tag must be a string")
        checksums = dependency.get("sha256_by_target", {})
        if not isinstance(checksums, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in checksums.items()):
            fail(f"{path}: dependencies.sha256_by_target must be a string table")
        if "cmake_package" in dependency and not isinstance(dependency["cmake_package"], str):
            fail(f"{path}: dependencies.cmake_package must be a string")
        if "cmake_dir" in dependency and not isinstance(dependency["cmake_dir"], str):
            fail(f"{path}: dependencies.cmake_dir must be a string")


def resolve_config_version(config, version=None):
    resolved = deepcopy(config)
    effective_version = version or resolved.get("default_version")
    if not effective_version:
        fail("no version provided; pass --version or set default_version in the config")
    resolved["version"] = effective_version
    return resolved


def source_sha256(config):
    by_version = config["source"].get("sha256_by_version", {})
    if config["version"] in by_version:
        return by_version[config["version"]].strip().lower()
    return config["source"].get("sha256", "").strip().lower()


def resolve_field(config, field):
    value = config
    for part in field.split("."):
        if not isinstance(value, dict) or part not in value:
            fail(f"field not found: {field}")
        value = value[part]
    return value


def print_value(value):
    if isinstance(value, (dict, list)):
        print(json.dumps(value, indent=2, sort_keys=True))
    else:
        print(value)
