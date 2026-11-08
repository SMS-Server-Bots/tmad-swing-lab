from pathlib import Path

root = Path(__file__).resolve().parents[2]
sync_path = root / "SYNC_RUNTIME_LOGS_TMAD.ps1"
classifier_path = root / ".github" / "scripts" / "classify_release_impact.py"
version_path = root / "VERSION.txt"

sync = sync_path.read_text(encoding="utf-8-sig")
classifier = classifier_path.read_text(encoding="utf-8")
version = version_path.read_text(encoding="utf-8-sig").strip()

required_sync_markers = [
    "function Get-ProcessStartMadrid([int]$ProcessId)",
    "[datetime]$CurrentProcessStartMadrid = [datetime]::MinValue",
    '$classification = "STALE"',
    '$classificationReason = "event_before_current_process_start"',
    '$classificationReason = "event_at_or_after_current_process_start"',
    "event_after_process_start = $eventAfterProcessStart",
    "classification_reason = $classificationReason",
    "event_version = $eventVersion",
    '$alertsError = Get-LatestSnapshotEvent "tmad_alerts_error_*.log" "(?i)(error|exception|traceback|🟥)" $alertsPid $alertsProcessStartMadrid',
    '$dataError = Get-LatestSnapshotEvent "tmad_data_error_*.log" "(?i)(error|exception|traceback|🟥)" $dataPid $dataProcessStartMadrid',
    "alerts_process_start_madrid = if ($alertsProcessStartMadrid",
    "data_process_start_madrid = if ($dataProcessStartMadrid",
    "schema_version = 2",
]
for marker in required_sync_markers:
    assert marker in sync, marker

assert 'if ($eventDate -eq $madridNow.Date) { $classification = "CURRENT" }' not in sync
assert "Get-ProcessStartMadrid([int]$Pid)" not in sync, "PowerShell automatic $PID must not be shadowed"
assert '"SYNC_RUNTIME_LOGS_TMAD.ps1",' in classifier, "runtime-log publisher must remain control-only"
assert tuple(int(x) for x in version.split(".")) >= (6, 2, 56), version

print("PASS | >=6.2.56 | runtime errors use live-process provenance; pre-start same-day errors become STALE")
