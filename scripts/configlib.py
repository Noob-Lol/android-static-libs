import json
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "configs"
REQUIRED_TOP_LEVEL = ("name", "source", "build")


try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - GitHub runners use Python 3.11+
    tomllib = None


def fail(message):
    raise SystemExit(f"error: {message}")


def config_path(package):
    return CONFIG_DIR / f"{package}.toml"


def load_config(package):
    path = config_path(package)
    if not path.is_file():
        fail(f"config not found: {path}")

    if tomllib is None:
        fail("Python 3.11+ is required to read TOML package configs")

    with path.open("rb") as fh:
        config = tomllib.load(fh)

    validate_config(config, path)
    return config, path


def validate_config(config, path):
    for key in REQUIRED_TOP_LEVEL:
        if key not in config:
            fail(f"{path}: missing required field '{key}'")

    if config["build"].get("system") != "cmake":
        fail(f"{path}: only build.system='cmake' is currently implemented")

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
