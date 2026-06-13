import json
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


def load_config(package):
    path = config_path(package)
    if not path.is_file():
        fail(f"config not found: {path}")

    with path.open("rb") as fh:
        config = tomllib.load(fh)

    validate_config(config, path)
    return config, path


def validate_config(config, path):
    for key in REQUIRED_TOP_LEVEL:
        if key not in config:
            fail(f"{path}: missing required field '{key}'")

    if config["build"].get("system") not in {"cmake", "openssl"}:
        fail(f"{path}: build.system must be 'cmake' or 'openssl'")

    if "url" not in config["source"]:
        fail(f"{path}: source.url is required")

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
