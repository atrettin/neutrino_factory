# Adding a new generator

To add a new backend, keep changes isolated to four places:

1. add a setup script under `setup/`
2. add a translator under `src/neutrino_factory/translators/`
3. add a normalizer under `src/neutrino_factory/normalizers/`
4. add an adapter and registry entry under `src/neutrino_factory/generators/`

## Minimal checklist

- create `setup/setup_<name>.sh`
- create `src/neutrino_factory/translators/<name>.py`
- create `src/neutrino_factory/normalizers/<name>.py`
- create `src/neutrino_factory/generators/<name>.py`
- register the adapter in `src/neutrino_factory/generators/registry.py`
- add a config example or defaults if needed
- add at least one local smoke test

The rest of the orchestration layer should not need generator-specific branching.

## Apptainer payload (cluster runtime)

The composed cluster image `nf-base.sif` is assembled from per-generator *payload*
SIFs. Adding a generator or a new version of an existing one requires **no edit to
`nf-base.def` or `build_apptainer_images.sh`** — the build script discovers
payloads from the catalog and composes every built SIF automatically.

To add a new generator payload, write `setup/apptainer/<name>.def` following the
existing payloads' contract:

1. **Version-namespaced, self-contained staging.** Stage everything the generator
   needs at run time (binaries, ROOT, data) under one tree:
   `/opt/nf/generators/<name>/<code_version>/…`. Do not depend on files outside
   that tree except the fixed base runtime libs installed by `nf-base.def`
   (`libgfortran5 libgomp1 libfreetype6 g++ libpcre3`); bundle anything else.
2. **Ship a wrapper per binary** at
   `/opt/nf/generators/<name>/<code_version>/bin/<binary>`. It is the single
   source of truth for that generator's `PATH`/`LD_LIBRARY_PATH`/launch logic.
   Make it self-locating so its text is version-independent:
   `prefix="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"`.
3. **Ship a descriptor** `nf-payload.json` in the namespaced root:
   `{"schema_version":1,"generator":"<name>","code_version":"<cv>","binaries":[…],"default_binary":"<bin>","smoke_paths":[…]}`.
   The build script reads it for smoke tests and to install default-version
   symlinks; `smoke_paths` (optional) are files verified present after compose.
4. **Declare the build arg** on the adapter class: set `build_arg_name` (and
   override `build_arg()` if the value is a transform of the code version, as
   GiBUU does). The catalog surfaces it so the build script passes
   `--build-arg NAME=value` without hard-coding.

Add code versions by adding entries to the adapter's `CODE_VERSIONS`; build the
payload (`setup/build_apptainer_images.sh --only <name>`) and recompose. Two
versions of one generator with both payload SIFs present are composed side by
side; a task selects one via `nf-run <generator> <code_version> <binary>`
(emitted automatically by the adapter under the apptainer runtime), while bare
binary names resolve to the default (highest) version.
