from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
OBS=ROOT/"01_PROGRAMAS"/"observability"
ALERTS=ROOT/"01_PROGRAMAS"/"alerts_bot"
sys.path.insert(0,str(OBS))

from runtime_data_export import RESEARCH_SIDECARS,publish_research_sidecars


def main()->int:
    assert "latest_prospective_architecture_journal.json" in RESEARCH_SIDECARS

    journal=(ALERTS/"prospective_architecture_journal.py").read_text(encoding="utf-8-sig")
    assert '"journal_coverage": journal_coverage' in journal
    assert "GROUP BY journal_type,matured" in journal
    assert '"pending_rows"' in journal
    assert '"matured_rows"' in journal
    assert '"pending_dates"' in journal
    assert '"matured_dates"' in journal

    exporter=(OBS/"runtime_data_export.py").read_text(encoding="utf-8-sig")
    assert 'prospective_source_root = output_source_root / "prospective_architecture"' in exporter
    assert 'name == "latest_prospective_architecture_journal.json"' in exporter
    assert '"source_path": source.relative_to(output_source_root).as_posix()' in exporter

    with tempfile.TemporaryDirectory() as tmp:
        base=Path(tmp)
        data_root=base/"03_DATOS"
        hist=data_root/"alerts_bot"/"output"/"historical_validation"
        prospective=data_root/"alerts_bot"/"output"/"prospective_architecture"
        hist.mkdir(parents=True,exist_ok=True)
        prospective.mkdir(parents=True,exist_ok=True)
        output=base/"export"

        payload={
            "mode":"PROSPECTIVE_ARCHITECTURE_JOURNAL_V1",
            "scope":"PROSPECTIVE_SHADOW_ONLY",
            "live_mutation":False,
            "promotion_allowed":False,
            "core_direction_v2":{"status":"ACCUMULATING","sample":0,"mature_dates":0},
            "research_override_top1":{"status":"ACCUMULATING","sample":0,"mature_dates":0},
            "pending_rows":757,
            "journal_coverage":{
                "CORE_DIRECTION_V2":{
                    "captured_rows":750,"captured_dates":3,
                    "pending_rows":750,"pending_dates":3,
                    "matured_rows":0,"matured_dates":0,
                    "first_date":"2026-10-03","last_date":"2026-10-05",
                },
                "RESEARCH_OVERRIDE_TOP1":{
                    "captured_rows":7,"captured_dates":3,
                    "pending_rows":7,"pending_dates":3,
                    "matured_rows":0,"matured_dates":0,
                    "first_date":"2026-10-03","last_date":"2026-10-05",
                },
            },
        }
        name="latest_prospective_architecture_journal.json"
        (prospective/name).write_text(json.dumps(payload),encoding="utf-8")

        result=publish_research_sidecars(
            data_root,output,max_file_bytes=100000,max_total_bytes=1000000
        )
        meta=result["files"][name]
        assert meta["published"] is True,meta
        assert meta["source_path"]=="prospective_architecture/"+name,meta
        published=json.loads((output/"research"/name).read_text(encoding="utf-8"))
        assert published["pending_rows"]==757
        assert published["journal_coverage"]["CORE_DIRECTION_V2"]["pending_rows"]==750
        assert published["live_mutation"] is False
        assert published["promotion_allowed"] is False

    version=(ROOT/"VERSION.txt").read_text(encoding="utf-8-sig").strip()
    assert tuple(int(x) for x in version.split("."))>=(6,2,45),version
    print(
        "PASS | >=6.2.45 | prospective architecture summary published with "
        "per-challenger pending/matured coverage | LIVE unchanged"
    )
    return 0


if __name__=="__main__":
    raise SystemExit(main())
