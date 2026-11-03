# Generator versioning domain knowledge

A generator's output depends on two independent axes — its *code version*
(e.g. GENIE `R-3_06_00`) and its *config version* / physics parameter set
(GENIE calls this a *tune*, e.g. `G18_10a_02_11a`). Every generator version
entry in the config carries both keys: `code_version` and `config_version`
(the latter is `"default"` for generators without a distinct parameter-set
concept yet).

Each generator's **adapter class** is the single source of truth for its
versions:

- `CODE_VERSIONS` declares the known code versions (with build refs and,
  for statically-versioned generators, their `config_versions`).
- Adapter classmethods expose availability dynamically.

**GENIE tunes are discovered at runtime** from the cross-section splines
staged on disk (`available_config_versions` globs
`<software_root>/genie/genie_xsec/<tag-safe>/<tune>/xsecs.xml`), so there is
no hard-coded GENIE tune list.

Config validation is *availability-only* for GENIE (known `code_version` +
non-empty `config_version`) and strict against the static `config_versions`
for the other generators.

**Legacy config versions.** A retired config version moves from
`config_versions` to `legacy_config_versions`, a map from its name to the
reason it was retired (e.g. NuWro `default`). It still validates, with a
warning, so the configs of runs made with it keep loading for `merge`,
`check-status` and `analyze-kinematics`. `submit` and `retry` refuse it, and
`list-generators` does not offer it.

**Nucleus restrictions.** A config version whose physics model exists only for
some nuclei refuses the others during validation, through its translator's
`validate_target` (e.g. NuWro `sf`). It never falls back to a different model
for them, so all targets of one config version share the same physics.

The uniform version identifier is `f"{code_version}+{config_version}"`.

Version info is written to output HDF5 **metadata only** (keys
`code_version`, `config_version`, `generator_version_id`), never per event.

Use `neutrino-factory list-generators [--built] [--generator NAME] [--json]`
to inspect the catalog.
