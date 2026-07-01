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
