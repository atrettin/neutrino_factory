# MPP cluster usage

This repository is designed around a **local-first** workflow and an **MPP Slurm** submission path.

## Before you have cluster access

Use the local executor for all testing:

```bash
neutrino-factory validate-config --config configs/examples/power_law_numu_Ar.yaml
neutrino-factory submit --config configs/examples/power_law_numu_Ar.yaml --executor local
neutrino-factory submit --config configs/examples/power_law_numu_Ar.yaml --executor slurm --dry-run
```

## Environment variables

```bash
export NF_SOFTWARE_ROOT=/path/to/shared/software
export NF_OUTPUT_ROOT=/path/to/shared/output
export NF_WORK_ROOT=/path/to/shared/work
export NF_SCRATCH_ROOT=/ptmp/$USER/neutrino_factory
export NF_EXECUTION_MODE=local
```

## MPP-oriented execution model

- use **job arrays** instead of many individual `sbatch` calls,
- stage heavy temporary work into `/ptmp` or node-local scratch,
- copy final products back to `NF_OUTPUT_ROOT`,
- keep logs and manifests under `NF_WORK_ROOT`,
- record Slurm job IDs and task IDs for reproducibility.

## Later, on the cluster

When the repository is moved onto the MPP environment, follow `docs/slurm_submission_testing.md` for the first real submission test.
