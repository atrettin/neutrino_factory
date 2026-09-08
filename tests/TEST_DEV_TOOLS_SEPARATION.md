# Testing Instructions for Dev Tools Separation

## Summary of Changes

1. **Removed** VSCode remote development packages (`curl`, `git`, `bash`, `openssh-server`) from `setup/apptainer/nf-base.def`
2. **Created** new `setup/apptainer/nf-dev.def` that builds on `nf-base.sif` with full development tools
3. **Updated** `setup/build_apptainer_images.sh` with `--dev-tools` flag to build `nf-dev.sif`
4. **Removed** SSH server start script (not needed for VSCode workflow)

## VSCode Remote Development Workflow

1. Build dev image: `bash setup/build_apptainer_images.sh --dev-tools`
2. Create cenv: `cenv --create nf-dev-env "$NF_IMAGE_ROOT/nf-dev.sif"`
3. Add to local `~/.ssh/config`:
   ```
   Host nf-dev-remote
       HostName <cluster-host>
       User <username>
       RemoteCommand ~/.local/bin/cenv nf-dev-env
       RequestTTY yes
   ```
4. Connect from VSCode: Remote-SSH → nf-dev-remote
5. VSCode automatically runs inside nf-dev container; shell is pre-entered

## Test Plan (Run on ODSL cluster node: odslserv01/02)

### 1. Verify nf-base.def no longer contains dev tools

```bash
cd /ptmp/mpp/$USER/neutrino_factory/repo
grep -E "(curl|git|openssh)" setup/apptainer/nf-base.def
```

**Expected**: Only matches in bash script content (e.g., `#!/usr/bin/env bash`), not package names.

### 2. Build base image (without dev tools)

```bash
NF_IMAGE_ROOT=/ptmp/mpp/$USER/neutrino_factory/images
bash setup/build_apptainer_images.sh --bootstrap --accept-defaults
```

**Verify**: Build completes successfully, `nf-base-bootstrap.sif` created.

### 3. Build full base image (no dev tools)

```bash
bash setup/build_apptainer_images.sh --accept-defaults
```

**Verify**:
- `nf-base.sif` built from payloads
- No dev tools installed in `nf-base.sif`

Test with:
```bash
apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" command -v git
apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" command -v curl
apptainer exec "$NF_IMAGE_ROOT/nf-base.sif" command -v vim
```

**Expected**: All commands return "not found" (127).

### 4. Build dev tools image

```bash
bash setup/build_apptainer_images.sh --dev-tools --accept-defaults
```

**Verify**:
- `nf-dev.sif` built successfully
- Smoke test passes (checks for git and pytest)

Test with:
```bash
apptainer exec "$NF_IMAGE_ROOT/nf-dev.sif" command -v git
apptainer exec "$NF_IMAGE_ROOT/nf-dev.sif" command -v vim
apptainer exec "$NF_IMAGE_ROOT/nf-dev.sif" command -v pytest
apptainer exec "$NF_IMAGE_ROOT/nf-dev.sif" command -v pyright
apptainer exec "$NF_IMAGE_ROOT/nf-dev.sif" command -v black
```

**Expected**: All commands found and return version info.

### 5. Verify dev tools image is built on top of base

```bash
apptainer exec "$NF_IMAGE_ROOT/nf-dev.sif" python3 -c "import yaml, h5py, numpy, uproot, awkward, matplotlib"
apptainer exec "$NF_IMAGE_ROOT/nf-dev.sif" command -v nf-run
```

**Expected**: Both succeed (nf-dev inherits all base functionality).

### 6. Test cenv session with dev image

```bash
# Create dev environment (on odslserv node, host shell)
cenv --create nf-dev-env "$NF_IMAGE_ROOT/nf-dev.sif"
cenv nf-dev-env

# Inside cenv:
which git
which vim
which pytest
nf-dev-check
```

**Expected**: All tools available and `nf-dev-check` shows healthy environment.

### 7. Verify SSH workflow documentation

Check that `~/.ssh/config` entry in documentation:
```bash
grep -A5 "RemoteCommand ~/.local/bin/cenv" docs/mpp_cluster_usage.md
```

**Expected**: Shows the correct `RemoteCommand` directive for automatic container entry.

## Files Modified

1. `setup/apptainer/nf-base.def` - Removed lines 47-54 (VSCode dev packages)
2. `setup/apptainer/nf-dev.def` - **NEW** - Full development environment
3. `setup/build_apptainer_images.sh` - Added `--dev-tools` flag
4. `docs/apptainer_image.md` - Updated with dev tools section and SSH workflow
5. `docs/mpp_cluster_usage.md` - Updated first-time setup with SSH config
6. `AGENTS.md` - Added nf-dev.sif workflow note and agent instructions

## Rollback Plan

If issues occur, the changes are fully reversible:

```bash
# Revert nf-base.def to include dev tools (restore original lines 47-54)
# Remove nf-dev.def
# Remove --dev-tools flag from build script
```

## Known Limitations

- Cannot test SSH workflow on local laptop (requires remote cluster access)
- Testing requires cluster access on ODSL interactive nodes (odslserv01/02)