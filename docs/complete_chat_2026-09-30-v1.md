

# Scheduled Runner v3 Optimization

**Date**: 2026-09-30 |  **Project**: gemini_ai / OANDA Forex Bot

---


## Phase 1 — Parameter Tuning & Audit Framework

Delivered:

- **`tuning_audit_checklist_v1.md`** — standards, weekly tracking, pre-LIVE checklist
- Key parameter changes:
  - `OVERRIDE_MA_VOTE_THRESHOLD`: 1.8 → 1.65 (catch near-consensus signals)
  - `OVERRIDE_MA_H4_WEIGHT`: 2.0 → 1.8 (respect H4 without over-weighting)
  - `MAX_POSITIONS`: 2 → 3
  - Reentry rules + MACD noise calibration
  - `EXPECT_ACCOUNT_ID` added

---

## Phase 2 — Parallel Validation Suite

Deployed side-by-side comparison system:

```
config/
  run.env.baseline    # original thresholds
  run.env.tuned       # 1.65 + reentry
scripts/
  parallel_runner.sh  # launch both in parallel
  diff_analysis.py    # console comparison
  weekly_summary.py   # 7-day aggregated audit
logs/
  baseline/
  tuned/
docs/
  validation_audit.md # auto-updating report
```

- Both configs run simultaneously → identical market conditions → fair comparison
- Daily diff highlights exactly what changed → `← DIFF` marker
- Weekly summary tracks signal count, override triggers, SKIP_HELD rate

---

## Phase 3 — Scheduled Automation

- `scheduled_validation.sh` — master orchestrator
- Cron schedule: **every 4 hours at :00 UTC** (aligned with H4 cycle)
- Auto-commit to Git → full audit trail
- Log rotation (14-day retention)

---

## Phase 4 — Final LIVE Activation Package

Delivered:

- **Pre-LIVE Safety Gates** — 7 mandatory checks before going live
- **`run.env.live`** — production config with all validated parameters
- **Emergency controls**:
  - `pkill -f scheduled_runner_v3.py` — immediate stop
  - Cron comment-out → pause
  - OANDA UI close-all → broker-level exit
- **First 24-hour watchlist** — metrics & alert thresholds
- **Sign-off sheet**

---

## Complete File Manifest

| File                                  | Purpose                                            |
| ------------------------------------- | -------------------------------------------------- |
| `docs/tuning_audit_checklist_v1.md` | Standards, params, weekly template, pre-LIVE gates |
| `docs/validation_audit.md`          | Auto-generated daily/weekly comparison             |
| `docs/complete_chat_2026-09-30.md`  | This file — full conversation & roadmap           |
| `config/run.env.baseline`           | Conservative reference config                      |
| `config/run.env.tuned`              | Validated pre-LIVE config                          |
| `config/run.env.live`               | Production activation config                       |
| `scripts/parallel_runner.sh`        | Dual-config launcher                               |
| `scripts/diff_analysis.py`          | Console comparison tool                            |
| `scripts/weekly_summary.py`         | 7-day audit aggregator                             |
| `scripts/scheduled_validation.sh`   | Cron entry point — full pipeline                  |
| `logs/cron_master.log`              | Scheduled run history                              |

---

## Recommended Next Steps

1. Run `./scripts/scheduled_validation.sh` → seed your first comparison
3. Review `docs/validation_audit.md`
4. After **7 consistent days** → work through Pre-LIVE Gates → activate `run.env.live`
5. Keep `--dry-run` until all safety boxes checked
