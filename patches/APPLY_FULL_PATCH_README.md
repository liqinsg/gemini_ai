Full patch bundle for JPY Strength Runner safety features

Files included in this bundle:
- `patches/scheduled_runner_jcs_v3.py`  : patched runner with guardian, lock hooks, idempotency usage
- `patches/utils_safety_helpers.py`     : new helper module to implement advisory lock, idempotency checks, SL/TP guardian, and a verified trade execution wrapper
- `patches/generate_full_patch.sh`      : script to create a git-format patch from the patched files

How to create the patch file (from repository root):

```bash
chmod +x patches/generate_full_patch.sh
./patches/generate_full_patch.sh
```

This will produce `patches/scheduled_runner_jcs_v3.patch`.

How to apply the patch (recommended):
1. Inspect the generated patch file carefully.
2. From the repo root, run:

```bash
git apply --check patches/scheduled_runner_jcs_v3.patch
# if check passes
git apply patches/scheduled_runner_jcs_v3.patch
```

If `git apply` fails due to context mismatch, you can manually copy the files from `patches/` into place after taking backups.

Notes:
- The helper `utils/safety_helpers.py` is conservative and attempts to call existing helpers in `utils/trading_core.py` or other OANDA wrapper modules if available. Please inspect and adapt names if your repository exposes different function names.
- The patch intentionally avoids editing `utils/trading_core.py` directly to reduce risk; instead it ships a helper module that uses existing public helpers.
- After applying the patch, run `python -m py_compile scheduled_runner_jcs.py` to ensure syntax is valid.
- Run the smoke tests described in the project TODOs before using in any live account.
