#!/usr/bin/env python3
import argparse

from configlib import CONFIG_DIR, config_path, fail, load_config, print_value, resolve_field


DEFAULT_DEFINES = {
    "BUILD_SHARED_LIBS": "OFF",
    "CMAKE_POSITION_INDEPENDENT_CODE": "ON",
}


def quote(value):
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def parse_key_value(items, option):
    values = {}
    for item in items or []:
        if "=" not in item:
            fail(f"{option} must use KEY=VALUE syntax: {item}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            fail(f"{option} contains an empty key: {item}")
        values[key] = value.strip()
    return values


def render_config(args):
    termux_package = args.termux_package or args.package
    defines = DEFAULT_DEFINES.copy()
    defines.update(parse_key_value(args.define, "--define"))
    patches = ", ".join(quote(patch) for patch in args.patch)

    lines = [
        '# Upstream package identity. Release tags must be "<name>-<version>".',
        f"name = {quote(args.package)}",
        f"version = {quote(args.version)}",
        "",
        "[source]",
        f"url = {quote(args.url)}",
        f"sha256 = {quote(args.sha256 or '')}",
        "",
        "[termux]",
        f"package = {quote(termux_package)}",
        "# Pin this to a Termux commit once patches are known good.",
        f"ref = {quote(args.termux_ref)}",
        f"patches = [{patches}]",
        "",
        "[build]",
        'system = "cmake"',
        f"source_subdir = {quote(args.source_subdir)}",
        "",
        "[build.defines]",
    ]

    for key in sorted(defines):
        lines.append(f"{key} = {quote(defines[key])}")

    return "\n".join(lines) + "\n"


def cmd_new(args):
    path = config_path(args.package)
    if path.exists() and not args.force:
        fail(f"{path} already exists; pass --force to overwrite")

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(render_config(args), encoding="utf-8")
    print(f"Wrote {path}")


def cmd_validate(args):
    packages = args.package or [path.stem for path in sorted(CONFIG_DIR.glob("*.toml"))]
    if not packages:
        fail(f"no configs found in {CONFIG_DIR}")

    for package in packages:
        config, path = load_config(package)
        print(f"OK: {path} ({config['name']} {config['version']})")


def cmd_show(args):
    config, _ = load_config(args.package)
    value = resolve_field(config, args.field) if args.field else config
    print_value(value)


def main():
    parser = argparse.ArgumentParser(description="Create and inspect package configs.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    new = subparsers.add_parser("new", help="Create a new configs/<package>.toml file")
    new.add_argument("package", help="Package/config name, for example libxml2")
    new.add_argument("--version", required=True, help="Upstream version")
    new.add_argument("--url", required=True, help="Source archive URL; {version} and {name} may be used")
    new.add_argument("--sha256", default="", help="Source archive SHA-256")
    new.add_argument("--termux-package", help="Termux package directory name; defaults to package")
    new.add_argument("--termux-ref", default="master", help="Termux branch, tag, or commit")
    new.add_argument("--patch", action="append", default=[], help="Termux patch filename; may be repeated")
    new.add_argument("--source-subdir", default=".", help="Source subdirectory passed to CMake")
    new.add_argument("--define", action="append", help="CMake define as KEY=VALUE; may be repeated")
    new.add_argument("--force", action="store_true", help="Overwrite an existing config")
    new.set_defaults(func=cmd_new)

    validate = subparsers.add_parser("validate", help="Validate one config, or all configs when omitted")
    validate.add_argument("package", nargs="*", help="Package/config name")
    validate.set_defaults(func=cmd_validate)

    show = subparsers.add_parser("show", help="Print a config or one dotted field")
    show.add_argument("package", help="Package/config name")
    show.add_argument("--field", help="Dotted field path, for example version or source.url")
    show.set_defaults(func=cmd_show)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
