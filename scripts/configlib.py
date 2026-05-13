import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "configs"
REQUIRED_TOP_LEVEL = ("name", "version", "source", "build")


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

    patches = config.get("termux", {}).get("patches", [])
    if not isinstance(patches, list):
        fail(f"{path}: termux.patches must be a list")

    if patches and not config.get("termux", {}).get("package"):
        fail(f"{path}: termux.package is required when termux.patches is not empty")

    defines = config["build"].get("defines", {})
    if not isinstance(defines, dict):
        fail(f"{path}: build.defines must be a table")


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
