This patch bundle contains a ready-to-use replacement for `scheduled_runner_jcs.py`.

Files:
- `patches/scheduled_runner_jcs_v2.py` — enhanced runner (lock + guardian + idempotency).

How to apply (safe, manual):

1) Inspect the new file before applying. You can run a quick diff:

```bash
diff -u scheduled_runner_jcs.py patches/scheduled_runner_jcs_v2.py | sed -n '1,200p'
```

2) Make a backup of the current file:

```bash
cp scheduled_runner_jcs.py scheduled_runner_jcs.py.bak
```

3) Replace the runner with the new version:

```bash
mv patches/scheduled_runner_jcs_v2.py scheduled_runner_jcs.py
```

4) Run a quick syntax check:

```bash
python -m py_compile scheduled_runner_jcs.py
```

5) Run a dry-run of the script (if your runner supports `--dry-run`) or run in controlled environment:

```bash
python scheduled_runner_jcs.py
```

Notes:
- The patched runner uses `OANDA_ACCOUNT_ID` from `config.py` to scope a lock file at `/tmp/runner_{OANDA_ACCOUNT_ID}.lock`.
- The guardian is conservative: it fetches open trades via the OANDA client and attempts to attach missing SL/TP using `attach_sl_tp_to_open_trade` from `utils/trading_core`.
- Idempotency is enforced with a broker-authoritative check (`check_pair_level_strategy_position`) and a short poll loop to reduce race conditions.

If you'd like, I can also produce a git-format patch (``git diff`` style) instead of a full-file replacement — tell me which you prefer and I will generate it next.