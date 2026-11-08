@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"

if "%LOCALAPPDATA%"=="" (
  set "TMAD_VALIDATION_VENV=%TEMP%\TMAD\validation_venv"
) else (
  set "TMAD_VALIDATION_VENV=%LOCALAPPDATA%\TMAD\validation_venv"
)

echo [SETUP] Preparando entorno de validacion TMAD...
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "01_PROGRAMAS\validation_support\PREPARAR_VALIDATION_ENV_TMAD.ps1" -RepoRoot "%CD%" -VenvPath "%TMAD_VALIDATION_VENV%"
if errorlevel 1 goto :bootstrap_fail
set "TMAD_VALIDATION_PYTHON=%TMAD_VALIDATION_VENV%\Scripts\python.exe"
if not exist "%TMAD_VALIDATION_PYTHON%" goto :bootstrap_fail

set "PY="
if exist "%TMAD_VALIDATION_PYTHON%" set "PY="%TMAD_VALIDATION_PYTHON%" -B"
if not defined PY (
  set "PY=python -B"
  where python >nul 2>nul || set "PY=py -3 -B"
)

set "TMAD_VALIDATION_MODE=1"
set "TMAD_NETWORK_ALLOWED=0"
set "VALROOT=%TEMP%\tmad_validation_current_%RANDOM%_%RANDOM%"
mkdir "%VALROOT%\02_CONFIG" "%VALROOT%\03_DATOS" "%VALROOT%\04_RUNTIME" >nul 2>nul
set "TMAD_INSTALL_ROOT=%VALROOT%"
set "TMAD_CONFIG_DIR=%VALROOT%\02_CONFIG"
set "TMAD_DATA_DIR=%VALROOT%\03_DATOS"
set "TMAD_RUNTIME_DIR=%VALROOT%\04_RUNTIME"
set "PYTHONPATH=%CD%\01_PROGRAMAS\validation_support;%CD%\01_PROGRAMAS"

echo.
echo =========================================
echo  VALIDACION TMAD CURRENT - MODO AISLADO
echo =========================================

%PY% -c "import dotenv, requests, httpx; print('PASS ^| deps validacion ^| dotenv+requests+httpx')" || goto :fail
%PY% -c "from pathlib import Path; import re; v=Path('VERSION.txt').read_text(encoding='utf-8').strip(); assert re.fullmatch(r'\d+\.\d+\.\d+',v),v; a=Path(r'01_PROGRAMAS/alerts_bot/config.py').read_text(encoding='utf-8-sig'); d=Path(r'01_PROGRAMAS/data_bot/config.py').read_text(encoding='utf-8-sig'); assert ('BOT_VERSION = \"'+v+'\"') in a; assert ('BOT_VERSION = \"'+v+'\"') in d; print('PASS ^| versiones sincronizadas ^| root/data/alerts='+v)" || goto :fail

echo [RUN] routing + control Discord current
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_routing_current.py" || goto :fail
echo [RUN] coordinacion current
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_coordinacion_v6_1_26.py" || goto :fail
echo [RUN] core incremental
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_core_incremental_v6_1_25.py" || goto :fail
echo [RUN] handoff ready gate
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_handoff_ready_gate_v6_1_25.py" || goto :fail
echo [RUN] logging granular
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_logging_v6_1_25.py" || goto :fail
echo [RUN] market regime
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_market_regime_v6_1_25.py" || goto :fail
echo [RUN] ventana operativa
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_ventana_operativa_v6_1_25.py" || goto :fail
echo [RUN] prioridad operativa
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_prioridad_operativa_v6_1_25.py" || goto :fail
echo [RUN] market outcome auditor
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_market_outcome_auditor_v6_1_25.py" || goto :fail
echo [RUN] adaptive learning
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_adaptive_learning_v6_1_25.py" || goto :fail
echo [RUN] quality + observability v6.1.37
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_quality_v6_1_37.py" || goto :fail
echo [RUN] runtime-data + dual-layout v6.1.38
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_runtime_data_v6_1_38.py" || goto :fail
echo [RUN] latencia + lifecycle + deploy v6.1.39
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_v6_1_39.py" || goto :fail
echo [RUN] validation env v6.1.40
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_v6_1_40.py" || goto :fail
echo [RUN] storage + observability v6.1.41
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_v6_1_41.py" || goto :fail
echo [RUN] production layout v6.1.42
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_v6_1_42.py" || goto :fail
echo [RUN] scanner recovery + batch persistence + position risk v6.1.43
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_v6_1_43.py" || goto :fail
echo [RUN] runtime publication watchdog v6.1.45
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_v6_1_45.py" || goto :fail
echo [RUN] watchdog deploy safety v6.1.46
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_v6_1_46.py" || goto :fail
echo [RUN] stable updater + detached runtime worktrees v6.1.48
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_v6_1_48.py" || goto :fail
echo [RUN] scanner alpha observability v6.1.49
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_scanner_alpha_observability_v6_1_49.py" || goto :fail
echo [RUN] EOD options alpha lab v6.1.50
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_eod_options_lab_v6_1_50.py" || goto :fail
echo [RUN] EOD options runtime v6.1.51
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_eod_options_runtime_v6_1_51.py" || goto :fail
echo [RUN] connected intraday to EOD lifecycle v6.1.52
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_intraday_eod_lifecycle_v6_1_52.py" || goto :fail
echo [RUN] EOD current-day readiness + archive retention v6.1.53
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_eod_readiness_v6_1_53.py" || goto :fail
echo [RUN] Alpha SHADOW maturity safety v6.1.54
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_alpha_shadow_safety_v6_1_54.py" || goto :fail
echo [RUN] EOD option tradeability v6.1.54
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_eod_tradeability_v6_1_54.py" || goto :fail
echo [RUN] EOD finalization v6.1.54
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_eod_finalization_v6_1_54.py" || goto :fail
echo [RUN] dedicated EOD Discord v6.1.55
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_eod_discord_v6_1_55.py" || goto :fail
echo [RUN] Alpha PnL attribution v6.1.57
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_alpha_pnl_attribution_v6_1_57.py" || goto :fail
echo [RUN] Alpha challengers v6.1.58
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_alpha_challengers_v6_1_58.py" || goto :fail
echo [RUN] Discord delivery observability v6.1.60
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_discord_observability_v6_1_60.py" || goto :fail
echo [RUN] Economic challengers v6.1.62
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_economic_challengers_v6_1_62.py" || goto :fail
echo [RUN] Scanner rank economics SHADOW v6.1.63
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_scanner_rank_economics_v6_1_63.py" || goto :fail
echo [RUN] Discord Alpha clarity + traceability v6.1.63
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_discord_traceability_v6_1_63.py" || goto :fail

echo [RUN] canonical economic history + GEX + lifecycle v6.1.75
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_economic_history_v6_1_75.py" || goto :fail

echo [RUN] FULL canonical-history SHADOW bridge v6.1.76
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_full_shadow_bridge_v6_1_76.py" || goto :fail

echo [RUN] control-plane resilience v6.1.77
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_control_plane_resilience_v6_1_77.py" || goto :fail

echo [RUN] preventive observability + historical GEX v6.1.78
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_preventive_observability_v6_1_78.py" || goto :fail

echo [RUN] production-layout validator regression v6.1.79
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_production_layout_v6_1_79.py" || goto :fail

echo [RUN] economic dry-run regression v6.1.80
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_economic_dry_run_v6_1_80.py" || goto :fail
echo [RUN] scanner pagination recovery v6.1.84
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_scanner_pagination_recovery_v6_1_84.py" || goto :fail
echo [RUN] compact economic QA summary v6.1.85
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_economic_summary_v6_1_85.py" || goto :fail
echo [RUN] inverse Scanner challenger v6.1.86
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_inverse_scanner_challenger_v6_1_86.py" || goto :fail
echo [RUN] daily reverse Alpha audit v6.1.87
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_daily_reverse_alpha_v6_1_87.py" || goto :fail
echo [RUN] opportunity ledger + daily Alpha scorecard v6.1.88
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_opportunity_ledger_v6_1_88.py" || goto :fail
echo [RUN] deep recovery + economic scorecard v6.1.89
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_deep_recovery_scorecard_v6_1_89.py" || goto :fail

echo [RUN] FULL-DEEP Reverse Alpha bridge v6.1.90
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_full_deep_reverse_alpha_v6_1_90.py" || goto :fail

echo [RUN] history query worker structure v6.1.95
%PY% -c "from pathlib import Path; p=Path(r'01_PROGRAMAS/history_query_worker/server.py'); t=p.read_text(encoding='utf-8'); assert p.is_file(); assert '/v1/sqlite/query' in t and '/v1/csv/query' in t and 'mode=ro&immutable=1' in t; r=Path(r'01_PROGRAMAS/history_query_worker/requirements.txt').read_text(encoding='utf-8'); assert 'boto3' in r and 'fastapi' in r and 'uvicorn' in r; print('PASS ^| history query worker ^| read-only SQLite + streaming CSV + R2 cache')" || goto :fail

echo [RUN] History tunnel lifecycle + updater lock v6.1.95
%PY% "01_PROGRAMAS\validation_support\validar_history_tunnel_v6_1_95.py" || goto :fail

echo [RUN] control repo normalization v6.1.96
%PY% "01_PROGRAMAS\validation_support\validar_control_repo_sync_v6_1_96.py" || goto :fail

echo [RUN] updater deploy IO v6.1.97
%PY% "01_PROGRAMAS\validation_support\validar_updater_deploy_io_v6_1_97.py" || goto :fail

echo [RUN] production-safe root parse v6.1.98
%PY% "01_PROGRAMAS\validation_support\validar_production_parse_v6_1_98.py" || goto :fail

echo [RUN] economic edge v6.1.99
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_economic_edge_v6_1_99.py" || goto :fail

echo [RUN] economic archive bridge v6.1.100
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_economic_bridge_v6_1_100.py" || goto :fail

echo [RUN] prospective economic edge evidence v6.1.102
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_prospective_edge_v6_1_102.py" || goto :fail

echo [RUN] economic cache version invalidation v6.1.104
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_economic_cache_version_v6_1_104.py" || goto :fail

echo [RUN] targeted option outcome bridge v6.1.105
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_option_outcome_bridge_v6_1_105.py" || goto :fail

echo [RUN] indexed option outcome bridge v6.1.106
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_fast_option_outcome_bridge_v6_1_106.py" || goto :fail

echo [RUN] fast signal path + EOD price outcomes v6.1.107
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_fast_signal_path_v6_1_107.py" || goto :fail

echo [RUN] Alpha Swing product + fast previous-run lookup v6.1.108
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_alpha_swing_product_v6_1_108.py" || goto :fail

echo [RUN] Alpha Swing lifecycle hardening v6.1.109
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_alpha_swing_product_v6_1_109.py" || goto :fail

echo [RUN] Reverse Alpha forensic learning bridge v6.1.110
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_reverse_alpha_learning_v6_1_110.py" || goto :fail

echo [RUN] EOD validator compatibility + CI parity v6.1.111
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_eod_validator_ci_parity_v6_1_111.py" || goto :fail

echo [RUN] Alpha Discord semantics + lineage v6.1.112
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_alpha_discord_semantics_v6_1_112.py" || goto :fail

echo [RUN] production post-check dual-layout v6.1.113
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_production_postcheck_v6_1_113.py" || goto :fail

echo [RUN] champion vs challenger v6.1.114
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_champion_challenger_v6_1_114.py" || goto :fail

echo [RUN] historical swing refinement v6.1.115
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_historical_swing_refinement_v6_1_115.py" || goto :fail

echo [RUN] historical model change-point v6.1.116
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_model_change_point_v6_1_116.py" || goto :fail

echo [RUN] regime x horizon swing v6.1.117
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_regime_horizon_swing_v6_1_117.py" || goto :fail

echo [RUN] observation history bridge v6.1.118
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_observation_history_bridge_v6_1_118.py" || goto :fail

echo [RUN] nested OOT regime router v6.1.119
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_regime_router_v6_1_119.py" || goto :fail

echo [RUN] full historical swing lab v6.1.120
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_full_history_swing_lab_v6_1_120.py" || goto :fail

echo [RUN] multi-window stability + deploy replay v6.1.121
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_stability_replay_v6_1_121.py" || goto :fail

echo [RUN] replay deploy safety v6.1.122
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_replay_deploy_safety_v6_1_122.py" || goto :fail

echo [RUN] operational history + reverse stress v6.1.123
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_operational_history_v6_1_123.py" || goto :fail

echo [RUN] historical replay runtime venv v6.1.124
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_historical_replay_runtime_venv_v6_1_124.py" || goto :fail

echo [RUN] production-first async historical replay v6.1.125
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_async_historical_replay_v6_1_125.py" || goto :fail

echo [RUN] deploy parser + release quarantine v6.1.126
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_deploy_parser_quarantine_v6_1_126.py" || goto :fail

echo [RUN] replay Discord lifecycle + tuple fix v6.1.127
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_replay_discord_lifecycle_v6_1_127.py" || goto :fail

echo [RUN] valid dual-book operational replay v6.1.128
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_valid_operational_replay_v6_1_128.py" || goto :fail

echo [RUN] economic validation closure v6.1.129
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_economic_validation_closure_v6_1_129.py" || goto :fail

echo [RUN] ENTER-first + session-count challengers v6.1.130
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_enter_first_session_challengers_v6_1_130.py" || goto :fail

echo [RUN] fast historical replay v6.1.131
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_fast_historical_replay_v6_1_131.py" || goto :fail

echo [RUN] corrected lifecycle + quality ENTER + cache migration v6.1.132
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_quality_lifecycle_cache_v6_1_132.py" || goto :fail

echo [RUN] current bot history + reverse forensics v6.1.133
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_current_bot_history_reverse_v6_1_133.py" || goto :fail

echo [RUN] canonical Reverse cache v6.1.134
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_canonical_reverse_cache_v6_1_134.py" || goto :fail

echo [RUN] SHORT return + history integrity v6.1.135
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_short_return_history_integrity_v6_1_135.py" || goto :fail

echo [RUN] trusted Reverse data lane v6.1.136
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_trusted_reverse_data_lane_v6_1_136.py" || goto :fail

echo [RUN] Thesis Quality OOT v6.1.142
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_full_universe_rank_ablation_v6_1_143.py" || goto :fail

echo [RUN] Swing V2 horizon + forward verification v6.2.0
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_swing_v2_v6_2_0.py" || goto :fail

echo [RUN] deploy liveness v6.2.1
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_deploy_liveness_v6_2_1.py" || goto :fail

echo [RUN] SQLite startup serialization v6.2.2
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_sqlite_startup_v6_2_2.py" || goto :fail

echo [RUN] replay progress + Swing V2 performance v6.2.3
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_replay_progress_v6_2_3.py" || goto :fail

echo [RUN] attribution performance v6.2.4
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_attribution_performance_v6_2_4.py" || goto :fail

echo [RUN] frozen replay inputs v6.2.5
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_frozen_replay_inputs_v6_2_5.py" || goto :fail

echo [RUN] snapshot + regime performance v6.2.6
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_snapshot_regime_performance_v6_2_6.py" || goto :fail

echo [RUN] Eligibility fixed-horizon falsification v6.2.8
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_entry_eligibility_fixed_horizon_v6_2_8.py" || goto :fail

echo [RUN] Momentum direction audit v6.2.9
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_momentum_direction_audit_v6_2_9.py" || goto :fail

echo [RUN] Momentum direction replay scope v6.2.10
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_momentum_direction_scope_v6_2_10.py" || goto :fail

echo [RUN] Momentum edge attrition v6.2.11
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_momentum_edge_attrition_v6_2_11.py" || goto :fail

echo [RUN] Frozen journal + momentum slot audit v6.2.12
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_research_freeze_and_slot_audit_v6_2_12.py" || goto :fail

echo [RUN] Momentum slot replay reporting v6.2.13
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_momentum_slot_reporting_v6_2_13.py" || goto :fail

echo [RUN] Momentum reconciliation v6.2.14
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_momentum_reconciliation_v6_2_14.py" || goto :fail

echo [RUN] Reconciliation coverage + Discord transient hardening v6.2.15
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_reconciliation_discord_v6_2_15.py" || goto :fail

echo [RUN] Fixed benchmark + trusted momentum reconciliation v6.2.16
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_fixed_benchmark_trusted_reconciliation_v6_2_16.py" || goto :fail

echo [RUN] Eligibility dual-lane falsification v6.2.17
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_entry_eligibility_falsification_v6_2_17.py" || goto :fail

echo [RUN] Economic waterfall calibration <=5D v6.2.18
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_economic_waterfall_calibration_v6_2_18.py" || goto :fail

echo [RUN] Economic architecture challengers <=5D v6.2.19
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_economic_architecture_challengers_v6_2_19.py" || goto :fail

echo [RUN] Prospective CORE direction + Research override journal v6.2.22
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_prospective_architecture_journal_v6_2_22.py" || goto :fail

echo [RUN] Frozen config historical simulation v6.2.23
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_frozen_config_historical_simulation_v6_2_23.py" || goto :fail

echo [RUN] Strict CORE + Research isolation safeguards v6.2.24
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_architecture_shadow_safeguards_v6_2_24.py" || goto :fail

echo [RUN] Scanner recall + ranking calibration v6.2.25
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_scanner_recall_ranking_calibration_v6_2_25.py" || goto :fail

echo [RUN] Scanner directional economic sanity v6.2.26
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_scanner_directional_sanity_v6_2_26.py" || goto :fail

echo [RUN] Scanner to CORE interaction simulation v6.2.27
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_scanner_core_interaction_simulation_v6_2_27.py" || goto :fail

echo [RUN] Scanner direction-aware calibration + missing-metric gate v6.2.29
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validar_scanner_direction_aware_ranking_v6_2_29.py" || goto :fail

echo [RUN] Runtime strategic replay sidecars v6.2.30
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_runtime_research_sidecars_v6_2_30.py" || goto :fail

echo [RUN] Prospective architecture capture boundary v6.2.31
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_prospective_architecture_capture_v6_2_31.py" || goto :fail

echo [RUN] Git main-ref isolation v6.2.32
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_git_fetch_isolation_v6_2_32.py" || goto :fail

echo [RUN] GEX gate calibration v6.2.33
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_gex_gate_calibration_v6_2_33.py" || goto :fail

echo [RUN] Prospective architecture observability v6.2.45
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_prospective_architecture_observability_v6_2_45.py" || goto :fail

echo [RUN] Replay completion observability v6.2.46
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_replay_completion_observability_v6_2_46.py" || goto :fail

echo [RUN] Fast compact research snapshot v6.2.47
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_fast_compact_snapshot_v6_2_47.py" || goto :fail

echo [RUN] CORE context precision optimizer v6.2.48
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_core_context_precision_v6_2_48.py" || goto :fail

echo [RUN] Autonomous Research Lab v6.2.49
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_autonomous_research_lab_v6_2_49.py" || goto :fail

echo [RUN] Autonomous Research observability v6.2.50
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_autonomous_research_observability_v6_2_50.py" || goto :fail

echo [RUN] Fast Research Cache + lockbox v6.2.51
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_fast_research_cache_v6_2_51.py" || goto :fail

echo [RUN] Stability Generation 3 v6.2.52
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_stability_generation3_v6_2_52.py" || goto :fail

echo [RUN] Adaptive Research Cycle v6.2.53
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_adaptive_research_cycle_v6_2_53.py" || goto :fail

echo [RUN] Post-FULL Adaptive Handoff v6.2.54
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_post_full_adaptive_handoff_v6_2_54.py" || goto :fail

echo [RUN] Replay Speed + Checkpoints v6.2.55
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_replay_speed_checkpoints_v6_2_55.py" || goto :fail

echo [RUN] Production recovery + cache-first FULL v6.2.56
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_production_recovery_fast_full_v6_2_56.py" || goto :fail

echo [RUN] Gen4 Rolling failure + regime research v6.2.57
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_gen4_rolling_regimes_v6_2_57.py" || goto :fail

echo [RUN] Runtime error provenance v6.2.56 control
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_runtime_error_provenance_v6_2_56.py" || goto :fail

echo [RUN] Snapshot verification without routine quick_check v6.2.58
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_snapshot_verification_v6_2_58.py" || goto :fail

echo [RUN] parse PowerShell runtime + optional control-plane v6.1.98
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$files=@('01_PROGRAMAS\observability\runtime_publication_watchdog.ps1','01_PROGRAMAS\launcher\launcher.ps1'); foreach($optional in @('DESPLEGAR_TMAD.ps1','TMAD_UPDATE_ONCE.py','INSTALAR_SYNC_RUNTIME_LOGS_TMAD.ps1','SYNC_RUNTIME_LOGS_TMAD.ps1','SYNC_RUNTIME_DATA_TMAD.ps1','MIGRAR_HISTORICO_LEGACY_TMAD.ps1','MANAGE_TMAD_HISTORY_STORAGE.ps1','INSTALAR_HISTORICO_TMAD.ps1')){if(Test-Path $optional){$files+=$optional}}; foreach($f in $files){if($f -like '*.py'){continue};$tokens=$null;$errors=$null;[System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $f),[ref]$tokens,[ref]$errors)|Out-Null;if($errors.Count){$errors|%%{Write-Host $_.Message -ForegroundColor Red};exit 1}};Write-Host 'PASS ^| PowerShell parse v6.1.113'" || goto :fail

echo [RUN] compileall alerts_bot + data_bot + observability
%PY% -m compileall -q "01_PROGRAMAS\alerts_bot" "01_PROGRAMAS\data_bot" "01_PROGRAMAS\history_query_worker" "01_PROGRAMAS\observability" || goto :fail

rd /s /q "%VALROOT%" >nul 2>nul
echo.
echo VALIDACION COMPLETA CORRECTA - CURRENT
echo.
exit /b 0

:bootstrap_fail
echo.
echo VALIDACION FALLIDA - no se pudo preparar el entorno Python de validacion
exit /b 1


echo [RUN] FUSION gate calibration v6.2.34
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_fusion_gate_calibration_v6_2_34.py" || goto :fail


echo [RUN] FUSION lab observability v6.2.35
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_fusion_lab_observability_v6_2_35.py" || goto :fail


echo [RUN] FUSION stable-read + storage safety v6.2.36
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_fusion_stable_read_v6_2_36.py" || goto :fail


echo [RUN] Alerts cold history v6.2.37
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_alerts_cold_history_v6_2_37.py" || goto :fail


echo [RUN] FUSION direct outcomes v6.2.38
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_fusion_direct_outcomes_v6_2_38.py" || goto :fail


echo [RUN] Alerts verified cold purge v6.2.39
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_alerts_cold_purge_v6_2_39.py" || goto :fail


echo [RUN] History research lock v6.2.40
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_history_research_lock_v6_2_40.py" || goto :fail


echo [RUN] Compact research snapshot v6.2.41
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_compact_research_snapshot_v6_2_41.py" || goto :fail

echo [RUN] FUSION orchestration + sidecar freshness v6.2.42
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_fusion_orchestration_v6_2_42.py" || goto :fail

echo [RUN] FUSION direction linkage + coverage reconciliation v6.2.43
%PY% "01_PROGRAMAS\validation_support\validation_runner.py" "01_PROGRAMAS\validation_support\validar_fusion_direction_reconciliation_v6_2_43.py" || goto :fail

:fail
set "ERR=%ERRORLEVEL%"
echo.
echo VALIDACION FALLIDA
rd /s /q "%VALROOT%" >nul 2>nul
exit /b %ERR%
