from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
OBS = ROOT / "01_PROGRAMAS" / "observability"
sys.path.insert(0, str(OBS))

from runtime_data_export import (
    RESEARCH_SIDECARS,
    publish_research_sidecars,
)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        data_root = base / "03_DATOS"
        source = data_root / "alerts_bot" / "output" / "historical_validation"
        output = base / "export"
        source.mkdir(parents=True, exist_ok=True)

        primary = "latest_scanner_direction_aware_ranking.json"
        (source / primary).write_text(
            json.dumps({
                "decision": {"dual_lane_followup": False},
                "token": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
                "long_text": "x" * 9000,
            }),
            encoding="utf-8",
        )

        malformed = "latest_scanner_core_interaction_simulation.json"
        (source / malformed).write_text("{not-json", encoding="utf-8")

        oversized = "latest_scanner_recall_ranking_calibration.json"
        (source / oversized).write_text(
            json.dumps({"payload": "z" * 15000}),
            encoding="utf-8",
        )

        result = publish_research_sidecars(
            data_root,
            output,
            max_file_bytes=12000,
            max_total_bytes=24000,
        )

        assert result["requested_count"] == len(RESEARCH_SIDECARS), result
        primary_meta = result["files"][primary]
        assert primary_meta["published"] is True, result
        published = json.loads((output / "research" / primary).read_text(encoding="utf-8"))
        assert published["decision"]["dual_lane_followup"] is False
        assert published["token"] == "[REDACTED_GITHUB_TOKEN]", published
        assert published["long_text"].startswith("x" * 100), published
        assert "TRUNCATED" in published["long_text"], published

        malformed_meta = result["files"][malformed]
        assert malformed_meta["published"] is False, malformed_meta
        assert malformed_meta["status"] == "ERROR", malformed_meta

        oversized_meta = result["files"][oversized]
        assert oversized_meta["published"] is False, oversized_meta
        assert oversized_meta["status"] == "SKIPPED_FILE_TOO_LARGE", oversized_meta

        missing = "latest_historical_validation_summary.json"
        assert result["files"][missing]["status"] == "MISSING", result["files"][missing]

    version = (ROOT / "VERSION.txt").read_text(encoding="utf-8-sig").strip()
    assert tuple(int(x) for x in version.split(".")) >= (6, 2, 30), version
    print("PASS | >=6.2.30 | bounded sanitized strategic replay sidecars | no LIVE mutation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
