# Android static native libraries

This repository builds static Android native libraries that can be reused while
building Python Android wheels. The initial package config is `libuv`, intended
for consumers such as `uvloop`. The same structure is meant to scale to
libraries such as `libxml2` and `libxslt` for consumers such as `lxml`.

## Package configs

Each package has one TOML file in `configs/<package>.toml`.

Example:

```toml
name = "libuv"
version = "1.48.0"

[source]
url = "https://dist.libuv.org/dist/v{version}/libuv-v{version}.tar.gz"
sha256 = "95b66faf3c19b021eb475c0a04c4febfe0442efbd88bca3174d32a1f8957cb71"

[termux]
package = "libuv"
ref = "master"
patches = []

[build]
system = "cmake"
source_subdir = "."

[build.defines]
BUILD_SHARED_LIBS = "OFF"
CMAKE_POSITION_INDEPENDENT_CODE = "ON"
```

Important fields:

- `name`: package name used for workflow inputs and release tags.
- `version`: upstream version.
- `source.url`: source archive URL. `{version}` and `{name}` are expanded.
- `source.sha256`: optional source checksum. Leave empty only while bootstrapping.
- `termux.package`: package directory under `termux/termux-packages/packages`.
- `termux.ref`: branch, tag, or commit from `termux/termux-packages`.
- `termux.patches`: patch filenames to download from Termux and apply before building.
- `build.system`: currently `cmake`.
- `build.defines`: CMake `-D` values.

Termux patches are downloaded from:

```text
https://raw.githubusercontent.com/termux/termux-packages/<ref>/packages/<termux.package>/<patch>
```

Use a fixed Termux commit in `termux.ref` once a package config is known good.
That makes rebuilds deterministic even if Termux changes or removes patches.

## Targets

The base workflow builds Android API 24 for:

- `arm64_v8a`, archived as `aarch64-linux-android`
- `x86_64`, archived as `x86_64-linux-android`

Output archive names use this format:

```text
<package>-<version>-android-api<api>-<triplet>.tar.gz
```

Each archive contains the CMake install tree and an
`android-libs-manifest.json` file describing the package, version, API, and
target triplet.

## GitHub Actions

Manual build:

1. Run **Build native Android libraries**.
2. Set `package` to a config name such as `libuv`.
3. Keep `api` as `24` unless a consumer requires a different minimum.

Release build:

1. Ensure `configs/<package>.toml` has the intended version.
2. Push a tag in the form `<package>-<version>`, for example:

```bash
git tag libuv-1.48.0
git push origin libuv-1.48.0
```

The workflow validates that the tag version matches the config version, builds
the static libraries, and uploads the tarballs to a GitHub release with the same
tag.

## Local validation

Config and target parsing can be checked without downloading or compiling:

```bash
python scripts/build.py --package libuv --target arm64_v8a,x86_64 --api 24 --validate-config
```

The config helper can validate all configs:

```bash
python scripts/config.py validate
```

Create a starter config without hand-writing TOML:

```bash
python scripts/config.py new libxml2 \
  --version 2.12.7 \
  --url "https://download.gnome.org/sources/libxml2/2.12/libxml2-{version}.tar.xz" \
  --termux-package libxml2 \
  --define LIBXML2_WITH_PYTHON=OFF
```

Inspect fields for scripts or release checks:

```bash
python scripts/config.py show libuv --field version
```

The full build requires an Android NDK and `ANDROID_NDK_HOME`,
`ANDROID_NDK_ROOT`, or `ANDROID_NDK_LATEST_HOME` pointing to it.
