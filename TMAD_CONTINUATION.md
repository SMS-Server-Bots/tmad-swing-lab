# TMAD continuation / strategic handoff

_Last updated: 2026-10-04. Read this file before continuing strategic TMAD changes._

## Current repository state

- **Repository version prepared:** **6.2.55**
- **Last operational reconstruction explicitly confirmed valid in the working history:** **6.2.46** (FULL RECOMPUTED; 60 dates; 362 FUSION ENTER; Top-5/OPEN 7; 6 closed trades; WR 66.67%; avg +7.01%; PF 8.96; semantic quality VALID; duration 4h48m29s). Waterfall shows Scanner contains the mover universe while the dominant loss is CORE/direction. 6.2.48 therefore shifts research from preserving the current module chain to precision-first contextual mover discovery.
- **6.2.28:** introduced direction-aware Scanner candidate calibration, research-only.
- **6.2.28 deployment/update validation failed** before production deployment because the research gate treated undefined baseline metrics as hard failures.
- **6.2.29:** fixed that validation/evidence-semantics bug and the historical reconstruction completed VALID with champion metrics unchanged. It did **not** change LIVE trading behavior.
- **6.2.30:** observability-only projection of selected historical-validation JSON artifacts to `runtime-data/research/`, so future sessions can inspect the exact replay evidence from GitHub.
- **6.2.31:** fixes prospective architecture journal capture. The authoritative deep close is intentionally materialized with `historical=True` to suppress LIVE side effects; 6.2.22 had required `not historical` for journal capture, leaving the prospective CORE/Research journal at sample=0. 6.2.31 adds a separate explicit capture permission only from `_daily_recalibration`.
- **6.2.32:** fixes a control-repo Git race discovered while updating 6.2.30→6.2.31. `%update`, runtime-logs, runtime-data and the manual updater now fetch remote main into separate private refs instead of competing on `refs/remotes/origin/main`.
- **6.2.33:** historical GEX gate calibration. Separates missing GEX coverage from actual gate rejection and tests one fixed contradiction-only abstention challenger in rolling + fixed lanes; LIVE GEX remains unchanged.
- **6.2.34:** research-only FUSION state calibration. Keeps READY/RETEST/ENTER as control, tests one fixed `EARLY_RECOVERY_V1` relaxation, preserves explicit safety-block states, and runs as a non-blocking frozen one-shot lab; LIVE FUSION remains unchanged.
- **6.2.35:** startup/observability cleanup for that FUSION lab. Removes the duplicate replay invocation and exposes granular `fusion_gate_calibration_phase` telemetry; candidate semantics and LIVE behavior are unchanged.
- **6.2.36:** storage-safe FUSION research execution. Replaces the full Alerts DB snapshot with before/after stable-read watermarks over mature inputs, cleans orphan disposable snapshots, and performs startup WAL TRUNCATE only while the prior Alerts process is stopped. LIVE economics remain unchanged.
- **6.2.37:** Alerts cold-history retrieval plumbing. Existing GitHub archive releases/index remain the remote source; verified cold layer ZIPs can be materialized into `04_RUNTIME/history_cache/alerts_layers`, and historical readers merge warm local + cold cache. Automatic deletion of local Alerts ZIPs is not enabled in this release.
- **6.2.38:** fixes the 6.2.36 FUSION label bridge. `sessions_json` stores dates only, not OHLC; the lab now consumes complete horizon-5 `observation_outcomes` aggregate columns directly and derives LONG/SHORT MFE/MAE/close returns with the same simple-return math used by the waterfall. Stable-read protection remains mandatory; LIVE unchanged.
- **6.2.39:** completes the Alerts cold-storage lifecycle. The existing history manager now applies its already-validated deletion path to mature Alerts layer ZIPs only after 45d warm retention + remote index SHA + verified layer-catalog SHA + complete manifest; deploy rehydrates verified cold cache before historical replay. LIVE economics unchanged.
- **6.2.40:** closes a purge/replay race before 6.2.39 production rollout. The history manager checks `runtime_state` read-only before any storage mutation and exits fail-closed when replay/FUSION research is active or unverifiable; `-FromDeploy` no longer starts the manager after replay. Daily 05:15 retention remains installed; LIVE unchanged.
- **6.2.41:** compacts the immutable Alerts T0 snapshot used by full historical replay. All tables/indexes are copied transaction-consistently, but `feature_observations` excludes only unlabeled ephemeral intraday rows outside daily/core/canonical/outcome-linked/exact mature operational runs. Scanner/GEX backup behavior is unchanged; LIVE economics unchanged.
- **6.2.42:** fixes FUSION startup orchestration after the 6.2.41 FULL replay exceeded the old 4h wait. The lab now waits up to 8h, records explicit timeout state, republishes reused sidecars, persists research validity before publication and stamps direct-H5 sidecars with producer/version/timestamps. LIVE economics unchanged.
- **6.2.43:** repairs FUSION historical direction linkage. Direct-H5 calibration now anchors on the actual FUSION signal direction (GEX → CORE → GRAPH), preserves GEX_STANDALONE/divergence paths and requires exact ENTER/RETEST ↔ same-direction Timing reconciliation. Coverage mismatch becomes INVALID_COVERAGE_RECONCILIATION; LIVE unchanged.
- **6.2.44:** moves `EARLY_RECOVERY_V1` into the FULL replay and evaluates it on the same `trusted_reverse_series` / fixed-benchmark trusted lane as Reverse Stress and the Economic Waterfall. Physical `observation_id` retention is no longer the economic coverage boundary; the detached lab only republishes the FULL artifact. LIVE unchanged.
- **6.2.45:** prospective-observability only. Publishes `latest_prospective_architecture_journal.json` to runtime-data and exposes per-challenger captured/pending/matured rows+dates for CORE Direction V2 and Research Override Top-1. LIVE economics unchanged.
- **6.2.46:** replay-completion observability only. Persists per-phase replay timings and forces one runtime-data publication when the event-driven log watcher observes terminal replay state COMPLETE/ERROR/REUSED, preventing stale PENDING sidecars after a valid replay. LIVE economics unchanged.
- **6.2.47:** research-snapshot performance only. Replaces multi-GB Python row marshaling in the compact Alerts T0 copy with one transaction of SQLite-native ATTACH + INSERT..SELECT, while preserving the exact `RESEARCH_RELEVANT_FEATURE_ROWS_V2` row policy and T0 isolation. Adds per-table snapshot timings and an equivalence/concurrent-WAL regression test. LIVE economics unchanged.
- **6.2.48:** context-aware mover research. Existing modules become feature producers/controls rather than mandatory architecture. Scores independent LONG/SHORT hypotheses using same-run ticker + SPY/QQQ/IWM/DIA + VIX/UVIX-family + sector ETF + relational context and new interpretable interactions. Searches Top-1/3/5/10 with train→validation→untouched final test in rolling+fixed. Precision/PF/return take priority over raw recall. LIVE unchanged.
- **6.2.49:** Autonomous Research Lab V1. Reuses the frozen 6.2.48 contextual dataset to evaluate a bounded Generation-1 candidate grammar plus Generation-2 ablations, applies a temporal critic, lets only one TRAIN+VALIDATION winner touch FINAL TEST, persists experiment memory under 04_RUNTIME and requires rolling+fixed agreement before prospective shadow. LIVE and source/config mutation remain disabled.
- **6.2.50:** observability-only follow-up prepared while 6.2.49 reconstructs. Adds compact autonomous-research summary, tournament phase timings and a read-only historical replay progress projection to runtime-data. Candidate grammar/economics/LIVE remain identical to 6.2.49.
- **6.2.51:** Fast Research Cache. A FULL freezes compact rolling/fixed contextual datasets into preserved 04_RUNTIME; subsequent autonomous screenings can refit and rerun generations without rebuilding the full historical validation. Autonomous FINAL TEST is locked during FULL and can only be explicitly consumed once per frozen lane, with an audit ledger. LIVE remains unchanged.
- **6.2.52:** Stability-driven Generation 3. Candidates are scored on chronological validation folds with worst-fold reliability prioritized; Generation 3 explores compound ablations/capacity sensitivity; rolling+fixed choose one shared stability consensus before a paired one-use FINAL TEST unlock. LIVE remains unchanged.
- **6.2.53:** Adaptive Validation Search. Reuses the frozen rolling/fixed cache for bounded multi-round rule mutations, evaluating identical specs in both lanes and ranking by worst-case cross-lane stability. FINAL TEST stays locked; LIVE unchanged.
- **6.2.54:** Post-FULL Autonomous Handoff. A strategically valid FULL with both fast-cache lanes OK launches `adaptive_research_cycle.py` detached after replay completion; duplicate cache pairs are skipped. FINAL TEST/LIVE/deploy/promotion remain locked.
- **6.2.55:** Replay speed + checkpoints. Routine compact snapshot verification uses exact copied-row counts + SQLite `quick_check` (full `integrity_check` remains opt-in), and OPERATIONAL_VALIDATION publishes granular subphases so long work is distinguishable from a stall. LIVE unchanged.

6.2.33 replay completed VALID. GEX `CONTRADICTION_ONLY_ABSTENTION_V1` failed historical follow-up in both lanes: validation failed on MAE despite improved recall/return/PF, while final test passed. Strong opposite GEX contradictions changed sign across periods (train deeply negative; final test strongly positive), so do not turn GEX into a simple veto and do not promote the challenger. 6.2.36 is deployed. Storage recovery succeeded: Alerts WAL fell from ~4.24 GB to ~128 KB and host free disk recovered from ~5.7 GB to ~20.7 GB. 6.2.36 storage recovery succeeded, but its first FUSION sidecar exposed a label-bridge bug: ~16.2k mature outcomes matched observation IDs, yet `tickers=0/session_rows=0` because `sessions_json` contains only dates. 6.2.38 fixes that by using mature H5 aggregate outcome columns directly. 6.2.37 adds verified cold-history rehydration for Alerts layer archives so future research can read warm local + cached cold ZIPs without multi-GB DB copies. 6.2.39 completes the lifecycle: mature Alerts ZIPs can now leave the VPS through the existing storage-manager deletion path only after warm retention, remote-index SHA agreement, verified local catalog SHA agreement and a complete archive manifest. 6.2.40 closes the remaining concurrency race: the manager fails closed before any cache/archive mutation whenever replay/FUSION research is active or research state cannot be verified.

## Strategic path and invariants

The purpose of the current workstream is to improve Scanner opportunity capture without sacrificing downstream directional economics.

Do not bypass these invariants:

1. Static mandatory Scanner names remain preserved.
2. Candidate families are fixed; do not optimize free weights against hindsight.
3. Future prices are labels only.
4. Scanner LIVE remains unchanged until evidence supports promotion.
5. CORE LIVE remains unchanged.
6. Research LIVE remains unchanged.
7. GEX, timing, FUSION and lifecycle remain unchanged.
8. A candidate must first survive chronological train/validation/final-test research.
9. The same candidate must survive both **ROLLING_LATEST** and **FIXED_BENCHMARK** lanes.
10. Even dual-lane historical survival only authorizes **prospective shadow**, not a LIVE mutation.

## Version history for this research chain

| Version | Main purpose | Outcome / next implication |
|---|---|---|
| 6.2.19 | Economic architecture challengers, same-identity direction, equal-capacity rankers, Eligibility evidence | Established architecture comparison baseline |
| 6.2.20–6.2.21 | Packaging/version/validator hotfixes | No intended new economic logic |
| 6.2.22 | Prospective CORE direction + Research override journal | Created prospective evidence path |
| 6.2.23 | Exact frozen-config historical simulation | Tested combined frozen architecture |
| 6.2.24 | Strict CORE / Research isolation safeguards | Prevented harmful CORE V2 + Research stacking |
| 6.2.25 | Scanner recall/ranking calibration, Top-180 | Showed room to improve opportunity capture |
| 6.2.26 | Scanner directional economic sanity | Exposed that recall gains can damage directional economics |
| 6.2.27 | Mandatory-preserving Scanner -> CORE interaction simulation | Confirmed interactions must be judged economically |
| 6.2.28 | Direction-aware Scanner fixed candidate families | Update validation failed because None baseline metrics were misclassified as regressions |
| **6.2.29** | Missing-metric/evidence semantics + regression guard | Re-run validation; keep LIVE unchanged |
| **6.2.30** | Publish bounded strategic replay artifacts to runtime-data | Inspect exact 6.2.29 decision without rerunning history; LIVE unchanged |
| **6.2.31** | Repair prospective architecture journal capture boundary | Start accumulating CORE Direction V2 + Research Override Top-1 shadow evidence; LIVE unchanged |
| **6.2.32** | Isolate Git main fetch refs across updater/runtime publishers | Prevent concurrent `origin/main` ref-lock failures; includes 6.2.31 shadow fix; LIVE unchanged |
| **6.2.33** | GEX gate coverage + contradiction-only calibration | Quantify coverage loss vs true GEX rejection; only dual-lane PASS may proceed to prospective shadow |
| **6.2.34** | FUSION state-gate calibration | Test fixed EARLY-only recovery after Timing; preserve DIVERGENCE/MARKET_BLOCKED/EXTENDED/CAPPED; LIVE unchanged |
| **6.2.35** | FUSION lab startup + phase telemetry cleanup | Run only the lab after historical reuse and expose progress; no economic changes |
| **6.2.36** | FUSION stable-read + storage recovery | Avoid multi-GB DB duplication; discard runs if mature inputs drift; recover oversized WAL at safe startup |
| **6.2.37** | Alerts cold-history cache plumbing | Verified GitHub rehydration + warm/cold reader; no automatic local Alerts ZIP purge yet |
| **6.2.38** | FUSION direct mature H5 outcomes | Fix zero-sample label bridge; use aggregate outcome economics directly under stable-read guard; LIVE unchanged |
| **6.2.39** | Complete Alerts warm→GitHub→purge→cold-cache lifecycle | Reuse existing verified storage-manager deletion path; hydrate verified cold cache before replay; LIVE unchanged |
| **6.2.40** | Fail-closed cold-storage/research lock | No cache/archive mutation during active or unverifiable replay/FUSION research; daily retention preserved; LIVE unchanged |
| **6.2.41** | Compact immutable Alerts research snapshot | Avoid ~6.4GB full DB copy; preserve all research-relevant feature rows and exact operational-run semantics; LIVE unchanged |
| **6.2.42** | FUSION orchestration + sidecar freshness | Wait through long FULL replays, expose timeout explicitly and guarantee current direct-H5 sidecar metadata; LIVE unchanged |
| **6.2.43** | FUSION direction linkage + coverage reconciliation | Use actual FUSION direction, preserve GEX_STANDALONE/divergence and fail closed on ENTER/RETEST timing mismatch; LIVE unchanged |
| **6.2.44** | FUSION trusted FULL replay calibration | Evaluate unchanged EARLY_RECOVERY on trusted <=5D price paths inside FULL replay; reconcile generation/Timing; detached lab republishes only; LIVE unchanged |
| **6.2.45** | Prospective architecture observability | Publish CORE Direction V2 / Research Override progress by pending/matured rows and dates; no strategy mutation |
| **6.2.46** | Replay completion observability | Publish terminal replay state immediately and persist phase timings so long FULL runs can be diagnosed; LIVE unchanged |
| **6.2.47** | Fast compact Alerts T0 snapshot | Preserve identical research rows/T0 freeze while replacing Python multi-GB copy with SQLite-native bulk copy; LIVE unchanged |
| **6.2.48** | Context-aware mover precision research | Discover reliable mover rules directly from T0 ticker + market/VIX/ETF/relationship context, free from current module boundaries; LIVE unchanged |

## Exact 6.2.28 failure and 6.2.29 fix

The failure contained a final control sample of **540 rows / 3 dates / 0 selected movers**. Therefore profit factor and best-mover direction accuracy were undefined. The old gate required both sides to be numeric, so an undefined control metric forced `False` and eliminated otherwise evaluable challengers.

6.2.29 distinguishes:
- `None vs None`: not comparable, neutral;
- challenger value vs control `None`: new evidence, not a regression;
- challenger `None` vs defined control: failure;
- no target movers in the period: failure, preventing vacuous promotion.

## What the next session should do

1. Confirm the deployed version and validation output after **%update**.
2. Confirm `runtime-data/research/latest_prospective_architecture_journal.json` is published and matches `runtime_state.prospective_architecture_journal_snapshot`.
3. On each authoritative market deep close, verify journal rows are inserted while LIVE output remains unchanged.
4. Do **not** promote CORE Direction V2 before at least 10 mature dates + 1000 mature rows and a passing prospective screen.
5. Do **not** promote Research Override Top-1 before at least 20 mature dates and its return/PF/mover gates pass.
6. Keep CORE V2 and Research Override isolated; 6.2.23 showed stacking them can regress economics.
7. Direction-aware Scanner 6.2.29 has no dual-lane survivor; do not revisit it by loosening gates.
8. GEX 6.2.33 challenger is rejected; keep LIVE unchanged and treat regime-conditioned GEX as a later research question.
9. Close FUSION `EARLY_RECOVERY_V1` as rejected: rolling and fixed are EVALUABLE + VALID_RECONCILED but fail validation/final economics. Keep FUSION LIVE unchanged and shift research attention to the two dual-lane architecture survivors already in prospective shadow.
10. After 6.2.40 deployment, verify `history_storage_status.json`, GitHub-backed cold materialization, and that any Alerts ZIP purge is only for dates older than 45d with matching remote/catalog SHA and `manifest.source_counts.complete=true`.
11. Confirm manager reports `SKIPPED_RESEARCH_ACTIVE` or `SKIPPED_RESEARCH_STATE_UNAVAILABLE` rather than mutating cache/archives when research is active or cannot be verified.
12. Do not wait for prospective data to discover architecture. The existing trusted historical mover labels are sufficient for rule discovery/falsification. Use prospective shadow only after a rule survives chronological train→validation→final-test in both rolling and fixed lanes. 6.2.48 tests a new precision-first contextual architecture independent of current CORE direction and capacity.

## Key commits

- 6.2.21: `a247db7c4335`
- 6.2.22: `ec9d2d2f49c8`
- 6.2.23 final chain: `3fc6bddf258f`
- 6.2.24: `6ea885659592`
- 6.2.25 final chain: `44a722311b34`
- 6.2.26: `4fba6b233c5c`
- 6.2.27: `a83cc9197d96`
- 6.2.28: `f5086fa31e6e`
- 6.2.29: `c5af8bc8447d`
- 6.2.30: `24ab097e95d4`
- 6.2.31: `cf91e475e5a9`
- 6.2.32: `10665d2ecaa5`
- 6.2.33: `c60d1fd5ea46`
- 6.2.34: `df7ff9b8cde0`
- 6.2.35: `e695a4033e4d`
- 6.2.36: `9cfbbd046272`
- 6.2.37: `ed06a2ff25bb8`
- 6.2.38: `6b49cd4e3e52`
- 6.2.39: `25128065a3e0`
- 6.2.40: `427c99662ef3`
- 6.2.41: `512bc4c75db0`
- 6.2.42: `08781487b5fc`
- 6.2.43: `375e3ad78d85`
- 6.2.44: `7c74f113d9f2`
- 6.2.45: `73092eb1ffc8`
- 6.2.46: `5af4d6feef78`
- 6.2.47: `2aad25199478`
- 6.2.48: use repository HEAD after the context-precision merge.
