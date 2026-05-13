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
default_version = "1.48.0"

[source]
url = "https://dist.libuv.org/dist/v{version}/libuv-v{version}.tar.gz"
sha256 = ""

[source.sha256_by_version]
# "1.48.0" = "..."

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
- `default_version`: optional fallback version for manual builds. Release tags
  and `--version` override it.
- `source.url`: source archive URL. `{version}`, `{name}`,
  `{version_major}`, `{version_minor}`, and `{version_major_minor}` are
  expanded.
- `source.sha256`: optional fallback source checksum.
- `source.sha256_by_version`: optional version-keyed checksums. Missing
  entries warn and skip checksum verification instead of blocking other
  versions.
- `termux.package`: package directory under `termux/termux-packages/packages`.
- `termux.ref`: branch, tag, or commit from `termux/termux-packages`.
- `termux.patches`: patch filenames to download from Termux and apply before building.
- `build.system`: currently `cmake`.
- `build.defines`: CMake `-D` values.
- `dependencies`: optional list of prebuilt dependency archives to download
  and extract into the same target prefix before building this package. `url`
  supports `{package}`, `{version}`, `{target}`, and `{triplet}` tokens. When
  `url` is omitted, the build uses this repository's release archive format.

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
<package>-<version>-<triplet>.tar.gz
```

Each archive contains the CMake install tree and an
`android-libs-manifest.json` file describing the package, version, API, and
target triplet.

## GitHub Actions

Manual build:

1. Run **Build native Android libraries**.
2. Set `package` to a config name such as `libuv`.
3. Set `version`, or leave it empty to use `default_version`.
4. Keep `api` as `24` unless a consumer requires a different minimum.

Release build:

1. Push a tag in the form `<package>-<version>`, for example:

```bash
git tag libuv-1.48.0
git push origin libuv-1.48.0
```

The workflow uses the tag version directly, builds the static libraries, and
uploads the tarballs to a GitHub release with the same tag.

## Local validation

Config and target parsing can be checked without downloading or compiling:

```bash
python scripts/build.py --package libuv --target arm64_v8a,x86_64 --api 24 --validate-config
```

Validate configured Termux patches without compiling:

```bash
python scripts/build.py --package libuv --version 1.48.0 --validate-patches
```

The config helper can validate all configs:

```bash
python scripts/config.py validate
```

Create a starter config without hand-writing TOML:

```bash
python scripts/config.py new libxml2 \
  --default-version 2.12.7 \
  --url "https://download.gnome.org/sources/libxml2/2.12/libxml2-{version}.tar.xz" \
  --termux-package libxml2 \
  --define LIBXML2_WITH_PYTHON=OFF
```

Inspect fields for scripts or release checks:

```bash
python scripts/config.py show libuv --version 1.49.2 --field version
```

The full build requires an Android NDK and `ANDROID_NDK_HOME`,
`ANDROID_NDK_ROOT`, or `ANDROID_NDK_LATEST_HOME` pointing to it.
