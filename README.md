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
- `build.system`: `cmake`, `autotools`, or `openssl`.
- `build.defines`: CMake `-D` values.
- `build.patches_by_version`: optional version-keyed lists of repository patch
  paths, applied after Termux patches. libuv 1.53.0 uses a local patch to retain
  fork/exec on Android below API 28, where `posix_spawn` is unavailable.
- `dependencies`: optional list of prebuilt dependency archives to download
  and extract into the same target prefix before building this package. `url`
  supports `{package}`, `{version}`, `{target}`, `{triplet}`, and `{release_tag}`
  tokens. `release_tag` defaults to `<package>-<version>` and can select a build
  revision independently of the upstream version. When
  `url` is omitted, the build uses this repository's release archive format.
  `cmake_package` and `cmake_dir` can be used to pass
  `-D<cmake_package>_DIR=<prefix>/<cmake_dir>` for packages found via CMake
  config mode.
- `dependencies.sha256_by_target`: optional checksums keyed by `arm64_v8a` or
  `x86_64`, taking precedence over the fallback dependency `sha256`.
- `updates`: upstream discovery settings for the automatic updater, described below.

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

### Automatic updates and dependency rebuilds

**Update native Android libraries** runs daily at 06:23 UTC and can also be run
manually. Merge the workflow onto the default branch to enable its schedule.
It uses the built-in `GITHUB_TOKEN` with `contents: write`; no PAT is required.
Each library has its own job, logs, summary, and `update-<package>` artifact.
`libffi`, `libuv`, `openssl`, and `libxml2` run in parallel. `libxslt` waits only
for `libxml2`; failure in another package does not cancel it or other builds.
The reusable `update-package.yml` workflow builds and publishes directly, without
depending on tag-triggered workflows or the unused `release.yml` workflow.
When adding packages or dependency edges, update the jobs and `needs` entries in
`update-native-libs.yml` to match the config dependency graph.

Each managed package declares its upstream, for example:

```toml
[updates]
provider = "releases"
repository = "openssl/openssl"
tag_pattern = 'openssl-(\d+\.\d+\.\d+)'
series = "3.5"
```

`provider` is `releases` or `tags`. `tag_pattern` must match the entire tag and
capture exactly one stable `major.minor.patch` version. The optional `series`
limits updates to that major/minor line. OpenSSL tracks only 3.5.x. The GNOME
libraries use their GitHub mirrors' tags; the source archives still come from
GNOME. Drafts and prereleases are excluded, and versions are compared numerically.

The updater resolves upstream versions and walks the package dependency graph.
It builds and publishes `libxml2` before building `libxslt`, then replaces
`libxslt`'s configured dependency version and URL in its resolved build config
with that exact published release. Dependency archives are verified against
per-target SHA-256 checksums. No manual dependency version changes are needed.
Every dependency must have a package config and update settings in this repository.

Both architectures must build successfully before the updater uploads a draft
release and publishes it. If a dependency fails, its consumers are skipped and
retried on the next run; unrelated packages can still release. A source archive
that is not yet available, incompatible patches, or a build failure also leaves
that package unpublished and eligible for retry. Publishing failures may leave a
draft; subsequent runs reserve its revision and use the next one.

New upstream versions use `<package>-<version>` tags. When a dependency or build
recipe changes without an upstream version change, the updater creates
`<package>-<version>-r2`, then `-r3`, and so on. Archive filenames retain the upstream
version, e.g. `libxslt-1.1.45-aarch64-linux-android.tar.gz`; the release tag in the
download URL identifies the revision. Existing assets are never overwritten.
The updater reuses complete matching releases on subsequent runs. Existing
releases without update metadata are reused for standalone packages; consumers
are rebuilt once to establish dependency provenance. Build revisions are managed
by the updater, rather than the manual tag-build workflow.

Each automatic release includes `build-config.json` and `release-state.json`,
recording the resolved versions, dependency release tags, source checksum,
archive checksums, and repository commit. These release assets persist update
state; the updater does not commit changes to the TOML files. `default_version`
and dependency versions in TOML remain fallbacks for manual builds.

Known source checksums must match. For a new version without a configured
checksum, the updater records the first download's SHA-256 and verifies the
build's download against it; this pins the bytes but does not independently
authenticate the upstream archive. Pin `termux.ref` to a commit if exact patch
reproduction is required.

Select `dry_run` when running the workflow manually to inspect the release plan
without compiling or publishing. Each package's artifact contains its
`update-report.json`, resolved JSON configs, and `build.log`, including failures.
Locally, with Python 3.11+ and an authenticated
GitHub CLI:

```bash
python scripts/updates.py --dry-run --repository Noob-Lol/android-static-libs
```

The local CLI processes all packages in dependency order by default. Jobs use
`--package` to select one package and `--dependencies-file` to receive the exact
resolved release map from prerequisite jobs. A selected consumer fails if a
required dependency result is missing; it never silently uses its TOML fallback.

To rebuild from an attached resolved config, check out the commit recorded in
`release-state.json`, configure the Android NDK, and run:

```bash
python scripts/build.py --package libxslt --config-file build-config.json --dist dist
```

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

Run the updater's offline tests:

```bash
python -m unittest discover -s tests -v
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
