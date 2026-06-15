#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import NoReturn

from configlib import load_config, resolve_config_version, source_sha256

ROOT = Path(__file__).resolve().parents[1]

TARGETS = {
    "arm64_v8a": {
        "triplet": "aarch64-linux-android",
        "cmake_abi": "arm64-v8a",
    },
    "x86_64": {
        "triplet": "x86_64-linux-android",
        "cmake_abi": "x86_64",
    },
}


def log(message):
    print(message, flush=True)


def fail(message) -> NoReturn:
    msg = f"error: {message}"
    raise SystemExit(msg)


def template_values(config):
    version = config["version"]
    parts = version.split(".")
    return {
        "name": config["name"],
        "version": version,
        "version_major": parts[0],
        "version_minor": parts[1] if len(parts) > 1 else "",
        "version_major_minor": ".".join(parts[:2]),
    }


def render_template(value, config):
    return value.format(**template_values(config))


def render_dependency_template(value, dependency, target_info):
    package = dependency["package"]
    version = dependency["version"]
    parts = version.split(".")
    values = {
        "package": package,
        "name": package,
        "version": version,
        "version_major": parts[0],
        "version_minor": parts[1] if len(parts) > 1 else "",
        "version_major_minor": ".".join(parts[:2]),
        "target": target_info["cmake_abi"],
        "triplet": target_info["triplet"],
    }
    return value.format(**values)


def sha256sum(path):
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url, dest):
    log(f"Downloading {url}")
    with urllib.request.urlopen(url) as response, dest.open("wb") as fh:
        shutil.copyfileobj(response, fh)


def extract_archive(archive, dest):
    dest.mkdir(parents=True, exist_ok=True)

    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
        return first_child(dest)

    with tarfile.open(archive) as tf:
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, filter="data")
        else:
            tf.extractall(dest)
    return first_child(dest)


def extract_dependency_archive(archive, dest):
    dest.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
        return

    with tarfile.open(archive) as tf:
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, filter="data")
        else:
            tf.extractall(dest)


def first_child(path: Path):
    children = list(path.iterdir())
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return path


def run(cmd, cwd=None, env=None):
    log("+ " + " ".join(str(part) for part in cmd))
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def download_source(config, work_dir):
    source_url = render_template(config["source"]["url"], config)
    archive_name = source_url.rstrip("/").split("/")[-1]
    archive_path = work_dir / archive_name
    download(source_url, archive_path)

    expected = source_sha256(config)
    if expected:
        actual = sha256sum(archive_path)
        if actual != expected:
            fail(f"sha256 mismatch for {archive_name}: expected {expected}, got {actual}")
    else:
        log("No source sha256 configured; skipping checksum verification.")

    return extract_archive(archive_path, work_dir / "source")


def download_termux_patches(config, work_dir):
    termux = config.get("termux", {})
    patches = termux.get("patches", [])
    if not patches:
        return []

    ref = termux.get("ref", "master")
    package = termux["package"]
    patch_dir = work_dir / "termux-patches"
    patch_dir.mkdir(parents=True, exist_ok=True)

    downloaded = []
    for patch_name in patches:
        url = f"https://raw.githubusercontent.com/termux/termux-packages/{ref}/packages/{package}/{patch_name}"
        dest = patch_dir / Path(patch_name).name
        download(url, dest)
        downloaded.append(dest)

    return downloaded


def dependency_url(dependency, target_info):
    if "url" in dependency:
        return render_dependency_template(dependency["url"], dependency, target_info)

    package = dependency["package"]
    version = dependency["version"]
    triplet = target_info["triplet"]
    return (
        f"https://github.com/Noob-Lol/android-static-libs/releases/download/"
        f"{package}-{version}/{package}-{version}-{triplet}.tar.gz"
    )


def install_dependency_archives(config, target, work_dir, install_root):
    dependencies = config.get("dependencies", [])
    if not dependencies:
        return []

    target_info = TARGETS[target]
    dependency_dir = work_dir / "dependency-archives" / target
    dependency_dir.mkdir(parents=True, exist_ok=True)
    cmake_args = []

    for dependency in dependencies:
        url = dependency_url(dependency, target_info)
        archive_name = url.rstrip("/").split("/")[-1]
        archive_path = dependency_dir / archive_name
        download(url, archive_path)
        expected = dependency.get("sha256", "").strip().lower()
        if expected:
            actual = sha256sum(archive_path)
            if actual != expected:
                fail(f"sha256 mismatch for {archive_name}: expected {expected}, got {actual}")
        else:
            log(f"No dependency sha256 configured for {archive_name}; skipping checksum verification.")
        extract_dependency_archive(archive_path, install_root)
        cmake_package = dependency.get("cmake_package")
        cmake_dir = dependency.get("cmake_dir")
        if cmake_package and cmake_dir:
            cmake_args.append(f"-D{cmake_package}_DIR={install_root / cmake_dir}")

    return cmake_args


def apply_patches(source_dir, patches):
    for patch in patches:
        log(f"Applying Termux patch {patch.name}")
        try:
            run(["git", "apply", "--verbose", str(patch)], cwd=source_dir)
        except subprocess.CalledProcessError:
            run(["patch", "-p1", "-i", str(patch)], cwd=source_dir)


def validate_termux_patches(args):
    raw_config, config_path = load_config(args.package)
    patches = raw_config.get("termux", {}).get("patches", [])
    if not patches:
        log(f"OK: {config_path} has no Termux patches configured")
        return

    config = resolve_config_version(raw_config, args.version)

    log(f"Loaded {config_path}")
    with tempfile.TemporaryDirectory(prefix=f"{config['name']}-{config['version']}-patches-") as tmp:
        work_dir = Path(tmp)
        source_dir = download_source(config, work_dir)
        downloaded = download_termux_patches(config, work_dir)
        apply_patches(source_dir, downloaded)
        log(f"OK: fetched and applied {len(downloaded)} Termux patch(es)")


def find_ndk():
    for name in ("ANDROID_NDK_HOME", "ANDROID_NDK_ROOT", "ANDROID_NDK_LATEST_HOME"):
        value = os.environ.get(name)
        if value:
            path = Path(value)
            if (path / "build" / "cmake" / "android.toolchain.cmake").is_file():
                return path
    fail("ANDROID_NDK_HOME, ANDROID_NDK_ROOT, or ANDROID_NDK_LATEST_HOME must point to an Android NDK")


def cmake_define_args(defines):
    args = []
    for key, value in sorted(defines.items()):
        args.append(f"-D{key}={value}")
    return args


def build_target(config, source_dir, target: str, api, out_root: Path, keep_build, *, clean_install=True, cmake_args=None):
    system = config["build"].get("system", "cmake")
    if system == "openssl":
        return build_openssl_target(config, source_dir, target, api, out_root, keep_build, clean_install=clean_install)
    if system == "autotools":
        return build_autotools_target(config, source_dir, target, api, out_root, keep_build, clean_install=clean_install)
    return build_cmake_target(config, source_dir, target, api, out_root, keep_build, clean_install=clean_install, cmake_args=cmake_args)


def build_cmake_target(config, source_dir, target: str, api, out_root: Path, keep_build, *, clean_install=True, cmake_args=None):
    target_info = TARGETS[target]
    ndk = find_ndk()
    build_root = out_root / "build" / config["name"] / target
    install_root = install_root_for_target(out_root, target)

    if build_root.exists() and not keep_build:
        shutil.rmtree(build_root)
    if clean_install and install_root.exists():
        shutil.rmtree(install_root)

    source_subdir = config["build"].get("source_subdir", ".")
    cmake_source = source_dir / source_subdir
    defines = dict(config["build"].get("defines", {}))
    prefix_paths = [str(install_root)] if install_root.exists() else []

    configure_cmd = [
        "cmake",
        "-S",
        str(cmake_source),
        "-B",
        str(build_root),
        f"-DCMAKE_TOOLCHAIN_FILE={ndk / 'build' / 'cmake' / 'android.toolchain.cmake'}",
        f"-DANDROID_ABI={target_info['cmake_abi']}",
        f"-DANDROID_PLATFORM=android-{api}",
        f"-DCMAKE_INSTALL_PREFIX={install_root}",
        "-DCMAKE_BUILD_TYPE=Release",
        *(f"-DCMAKE_PREFIX_PATH={path}" for path in prefix_paths),
        *(cmake_args or []),
        *cmake_define_args(defines),
    ]

    run(configure_cmd)
    run(["cmake", "--build", str(build_root), "--config", "Release", "--parallel"])
    run(["cmake", "--install", str(build_root), "--config", "Release"])
    postprocess_install_tree(install_root)
    validate_static_install_tree(install_root)

    return install_root


def openssl_android_target(triplet):
    """Map an android-libs triplet to the OpenSSL Configure target name."""
    mapping = {
        "aarch64-linux-android": "android-arm64",
        "x86_64-linux-android": "android-x86_64",
    }
    result = mapping.get(triplet)
    if not result:
        fail(f"no OpenSSL Configure target known for triplet '{triplet}'")
    return result


def build_openssl_target(config, source_dir, target: str, api, out_root: Path, keep_build, *, clean_install=True):
    target_info = TARGETS[target]
    triplet = target_info["triplet"]
    ndk = find_ndk()
    toolchain_bin = ndk / "toolchains" / "llvm" / "prebuilt" / "linux-x86_64" / "bin"
    install_root = install_root_for_target(out_root, target)

    if clean_install and install_root.exists():
        shutil.rmtree(install_root)
    install_root.mkdir(parents=True, exist_ok=True)

    # OpenSSL's Configure script modifies the source tree, so we work in a
    # per-target copy to allow multiple targets from a single download.
    build_root = out_root / "build" / config["name"] / target
    if build_root.exists() and not keep_build:
        shutil.rmtree(build_root)
    if not build_root.exists():
        shutil.copytree(source_dir, build_root)

    openssl_target = openssl_android_target(triplet)
    configure_flags = config["build"].get("options", {}).get("configure_flags", [])

    # Build the environment: prepend NDK toolchain to PATH so Configure finds
    # the right clang / clang++ without manual CC/CXX overrides.
    env = os.environ.copy()
    env["PATH"] = str(toolchain_bin) + os.pathsep + env.get("PATH", "")
    env["ANDROID_NDK_ROOT"] = str(ndk)

    configure_cmd = [
        "perl",
        "Configure",
        openssl_target,
        f"-D__ANDROID_API__={api}",
        f"--prefix={install_root}",
        f"--openssldir={install_root}/ssl",
        *configure_flags,
    ]
    run(configure_cmd, cwd=build_root, env=env)
    run(["make", "-j", str(os.cpu_count() or 4)], cwd=build_root, env=env)
    run(["make", "install_sw"], cwd=build_root, env=env)

    # OpenSSL installs shared libs when built with modules; remove them so
    # the static-only validator does not trip (legacy provider is compiled
    # directly into libcrypto.a via enable-legacy + no-shared).
    _remove_openssl_shared_artifacts(install_root)

    postprocess_install_tree(install_root)
    validate_static_install_tree(install_root)
    return install_root


def _remove_openssl_shared_artifacts(install_root: Path):
    """Remove any .so / engine .so files that OpenSSL may install even with no-shared."""
    shared_patterns = ("*.so", "*.so.*")
    for pattern in shared_patterns:
        for path in install_root.rglob(pattern):
            log(f"Removing shared artifact: {path.relative_to(install_root)}")
            path.unlink()
    # Also prune empty directories left behind (e.g. lib/engines-3)
    for dirpath in sorted(install_root.rglob("*"), reverse=True):
        if dirpath.is_dir():
            try:
                dirpath.rmdir()  # only succeeds if empty
            except OSError:
                pass


def build_autotools_target(config, source_dir, target: str, api, out_root: Path, keep_build, *, clean_install=True):
    target_info = TARGETS[target]
    triplet = target_info["triplet"]
    ndk = find_ndk()

    # Locate toolchain bin directory dynamically
    prebuilt_dir = ndk / "toolchains" / "llvm" / "prebuilt"
    try:
        host_dir = next(prebuilt_dir.iterdir())
    except StopIteration:
        fail(f"no prebuilt host toolchain directory found under {prebuilt_dir}")
    toolchain_bin = host_dir / "bin"

    install_root = install_root_for_target(out_root, target)
    if clean_install and install_root.exists():
        shutil.rmtree(install_root)
    install_root.mkdir(parents=True, exist_ok=True)

    # Autotools configure modifies or generates files, so we compile in a per-target build root copy.
    build_root = out_root / "build" / config["name"] / target
    if build_root.exists() and not keep_build:
        shutil.rmtree(build_root)
    if not build_root.exists():
        shutil.copytree(source_dir, build_root)

    source_subdir = config["build"].get("source_subdir", ".")
    configure_dir = build_root / source_subdir
    configure_script = configure_dir / "configure"

    if not configure_script.is_file():
        fail(f"configure script not found at {configure_script}")

    # Ensure configure is executable (standard Unix permissions)
    try:
        configure_script.chmod(0o755)
    except OSError:
        pass

    env = os.environ.copy()
    env["PATH"] = str(toolchain_bin) + os.pathsep + env.get("PATH", "")
    env["CC"] = f"{triplet}{api}-clang"
    env["CXX"] = f"{triplet}{api}-clang++"
    env["AR"] = "llvm-ar"
    env["AS"] = f"{triplet}{api}-clang"
    env["RANLIB"] = "llvm-ranlib"
    env["STRIP"] = "llvm-strip"

    if install_root.exists():
        env["CFLAGS"] = f"-I{install_root}/include " + env.get("CFLAGS", "")
        env["CPPFLAGS"] = f"-I{install_root}/include " + env.get("CPPFLAGS", "")
        env["LDFLAGS"] = f"-L{install_root}/lib " + env.get("LDFLAGS", "")
        env["PKG_CONFIG_PATH"] = f"{install_root}/lib/pkgconfig" + (os.pathsep + env["PKG_CONFIG_PATH"] if "PKG_CONFIG_PATH" in env else "")

    configure_flags = config["build"].get("options", {}).get("configure_flags", [])
    configure_cmd = [
        "./configure",
        f"--host={triplet}",
        f"--prefix={install_root}",
        "--disable-shared",
        "--enable-static",
        *configure_flags,
    ]

    run(configure_cmd, cwd=configure_dir, env=env)
    run(["make", "-j", str(os.cpu_count() or 4)], cwd=configure_dir, env=env)
    run(["make", "install"], cwd=configure_dir, env=env)

    postprocess_install_tree(install_root)
    validate_static_install_tree(install_root)
    return install_root


def install_root_for_target(out_root: Path, target: str):
    return out_root / "install" / TARGETS[target]["triplet"]


def postprocess_install_tree(install_root: Path):
    # If libffi-style headers are installed under lib/libffi-<version>/include,
    # move them to include/
    lib_dir = install_root / "lib"
    if lib_dir.is_dir():
        for libffi_dir in lib_dir.glob("libffi-*"):
            if libffi_dir.is_dir():
                src_inc = libffi_dir / "include"
                if src_inc.is_dir():
                    dest_inc = install_root / "include"
                    dest_inc.mkdir(parents=True, exist_ok=True)
                    for item in src_inc.iterdir():
                        shutil.move(str(item), str(dest_inc / item.name))
                    shutil.rmtree(libffi_dir)

    pkgconfig_dir = install_root / "lib" / "pkgconfig"
    if pkgconfig_dir.is_dir():
        for pc_file in pkgconfig_dir.glob("*.pc"):
            rewrite_prefix_file(
                pc_file,
                {
                    "prefix=": "prefix=${pcfiledir}/../..",
                    "exec_prefix=": "exec_prefix=${prefix}",
                    "libdir=": "libdir=${prefix}/lib",
                    "includedir=": "includedir=${prefix}/include",
                },
            )

    bin_dir = install_root / "bin"
    if bin_dir.is_dir():
        for config_script in bin_dir.glob("*-config"):
            rewrite_prefix_file(
                config_script,
                {
                    "prefix=": 'prefix=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)',
                    "exec_prefix=": "exec_prefix=${prefix}",
                    "libdir=": "libdir=${prefix}/lib",
                    "includedir=": "includedir=${prefix}/include",
                },
            )


def rewrite_prefix_file(path: Path, replacements):
    lines = path.read_text(encoding="utf-8").splitlines()
    rewritten = []
    for line in lines:
        replacement = None
        for prefix, value in replacements.items():
            if line.startswith(prefix):
                replacement = value
                break
        rewritten.append(replacement if replacement is not None else line)
    path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")


def validate_static_install_tree(install_root: Path):
    static_libs = sorted((install_root / "lib").glob("*.a")) if (install_root / "lib").is_dir() else []
    if not static_libs:
        fail(f"{install_root}: no static libraries were installed")

    shared_patterns = ("*.so", "*.so.*", "*.dylib", "*.dll")
    shared_libs = []
    for pattern in shared_patterns:
        shared_libs.extend(install_root.rglob(pattern))
    if shared_libs:
        names = ", ".join(str(path.relative_to(install_root)) for path in sorted(shared_libs))
        fail(f"{install_root}: shared libraries were installed in static package: {names}")

    install_root_text = str(install_root)
    leaked_paths = []
    text_suffixes = {".cmake", ".pc", ".json", ".txt"}
    for path in install_root.rglob("*"):
        is_config_script = path.parent.name == "bin" and path.name.endswith("-config")
        if path.is_file() and (path.suffix in text_suffixes or is_config_script):
            content = path.read_text(encoding="utf-8", errors="ignore")
            if install_root_text in content:
                leaked_paths.append(path.relative_to(install_root))
    if leaked_paths:
        names = ", ".join(str(path) for path in leaked_paths)
        fail(f"{install_root}: install metadata contains non-relocatable absolute paths: {names}")


def write_manifest(config, target, api, install_root):
    manifest = {
        "name": config["name"],
        "version": config["version"],
        "target": target,
        "triplet": TARGETS[target]["triplet"],
        "android_api": api,
        "linkage": "static",
    }
    path = install_root / "android-libs-manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def archive_install(config, target, api, install_root: Path, dist_dir: Path):
    triplet = TARGETS[target]["triplet"]
    # dont include android api. `-android-api{api}` is bad
    name = f"{config['name']}-{config['version']}-{triplet}.tar.gz"
    archive_path = dist_dir / name
    dist_dir.mkdir(parents=True, exist_ok=True)

    with tarfile.open(archive_path, "w:gz") as tf:
        for item in install_root.iterdir():
            tf.add(item, arcname=item.name)
    log(f"Created {archive_path}")
    return archive_path


def parse_targets(values):
    if not values:
        values = ["arm64_v8a,x86_64"]

    targets = []
    for value in values:
        for item in value.split(","):
            item = item.strip()
            if item:
                targets.append(item)

    unknown = [target for target in targets if target not in TARGETS]
    if unknown:
        fail(f"unknown target(s): {', '.join(unknown)}; valid targets: {', '.join(TARGETS)}")

    return targets


def build_package(args):
    raw_config, config_path = load_config(args.package)
    config = resolve_config_version(raw_config, args.version)
    targets = parse_targets(args.target)
    dist_dir = Path(args.dist).resolve()

    log(f"Loaded {config_path}")
    with tempfile.TemporaryDirectory(prefix=f"{config['name']}-{config['version']}-") as tmp:
        work_dir = Path(tmp)
        source_dir = download_source(config, work_dir)
        patches = download_termux_patches(config, work_dir)
        apply_patches(source_dir, patches)

        out_root = work_dir / "out"
        for target in targets:
            install_root = install_root_for_target(out_root, target)
            if install_root.exists():
                shutil.rmtree(install_root)
            dependency_cmake_args = install_dependency_archives(config, target, work_dir, install_root)
            install_root = build_target(
                config,
                source_dir,
                target,
                args.api,
                out_root,
                args.keep_build,
                clean_install=False,
                cmake_args=dependency_cmake_args,
            )
            write_manifest(config, target, args.api, install_root)
            archive_install(config, target, args.api, install_root, dist_dir)


def main():
    parser = argparse.ArgumentParser(description="Build static Android native libraries.")
    parser.add_argument("--package", required=True, help="Package config name from configs/<package>.toml")
    parser.add_argument("--version", help="Upstream version. Overrides config default_version.")
    parser.add_argument("--target", action="append", help="Target name or comma-separated targets")
    parser.add_argument("--api", type=int, default=24, help="Android API level")
    parser.add_argument("--dist", default="dist", help="Output directory for tarballs")
    parser.add_argument(
        "--keep-build", action="store_true", help="Keep CMake build directories inside the temporary workspace"
    )
    parser.add_argument(
        "--validate-config", action="store_true", help="Validate config and exit without downloading or building"
    )
    parser.add_argument(
        "--validate-patches",
        action="store_true",
        help="Download source and configured Termux patches, then verify patches apply without building",
    )
    args = parser.parse_args()

    if args.validate_config:
        raw_config, path = load_config(args.package)
        version = args.version or raw_config.get("default_version")
        parse_targets(args.target)
        suffix = f" default_version={version}" if version else " no default_version"
        log(f"OK: {path} ({raw_config['name']};{suffix})")
        return

    if args.validate_patches:
        validate_termux_patches(args)
        return

    build_package(args)


if __name__ == "__main__":
    main()
