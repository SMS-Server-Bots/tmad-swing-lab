from __future__ import annotations

"""Regression for atomic Reverse cache and validation-only 2x2 scoring."""

from pathlib import Path
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "alerts_bot") not in sys.path:
    sys.path.insert(0, str(ROOT / "alerts_bot"))

from research_streaming_io import atomic_stream_json
from research_factorial_diagnostics import ARMS, evaluate_factorial


def _models() -> dict:
    model = lambda name: {
        "feature": name, "cuts": [0.0],
        "bins": [{"score": -1.0}, {"score": 1.0}],
    }
    return {
        "node": [model("node.edge")],
        "market": [model("market.edge")],
        "relation": [model("relation.edge")],
    }


def _rows() -> list[dict]:
    result = []
    for day in ("2026-09-01", "2026-09-02"):
        for i in range(6):
            ticker = f"T{i}"
            # High context rank favours last symbols; CORE baseline uses
            # ascending research ranks, so arm selections are not identical.
            edge = float(i - 2.0)
            source = {
                "date": day, "ticker": ticker,
                "research_rank": [i], "stage": {"core": True},
                "current_direction": "SHORT" if i % 2 else "LONG",
                "long_path": {
                    "mfe_pct": float(2 + i * 2), "mae_pct": 1.0,
                    "horizon_close_return_pct": float(i - 2),
                },
                "short_path": {
                    "mfe_pct": float(9 - i), "mae_pct": 2.0,
                    "horizon_close_return_pct": float(2 - i),
                },
                "context_usable": True,
                "context_diagnostics": {
                    "market_regime": "MIXED_ROTATION",
                    "explicit_sector_etf": i % 2 == 0,
                },
            }
            for direction in ("LONG", "SHORT"):
                result.append({
                    **source,
                    "context_direction": direction,
                    "context_features": {
                        "node.edge": edge if direction == "LONG" else -edge,
                        "market.edge": 1.0,
                        "relation.edge": edge if direction == "LONG" else -edge,
                        "sector.relative_strength_vs_sector_aligned": 1.0,
                    },
                })
    return result


def main() -> int:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "reverse_canonical_cache_v1.json"
        original = {"schema_version": 1, "text": "áü👋", "rows": [{"x": i} for i in range(500)]}
        atomic_stream_json(path, original)
        assert json.loads(path.read_text(encoding="utf-8")) == original
        assert path.read_text(encoding="utf-8") == json.dumps(original, ensure_ascii=False, default=str)

        # A failed streamed write cannot replace a valid cache.
        old_iterencode = json.JSONEncoder.iterencode
        def fail_after_prefix(self, obj, _one_shot=False):
            yield '{"incomplete":'
            raise RuntimeError("simulated writer failure")
        try:
            json.JSONEncoder.iterencode = fail_after_prefix
            try:
                atomic_stream_json(path, {"replacement": True})
            except RuntimeError:
                pass
            else:
                raise AssertionError("expected write failure")
        finally:
            json.JSONEncoder.iterencode = old_iterencode
        assert json.loads(path.read_text(encoding="utf-8")) == original
        assert sorted(Path(temp).iterdir()) == [path], "leftover uncommitted temp file"

    view = evaluate_factorial(_rows(), _models(), capacity=5)
    assert view["status"] == "EVALUABLE", view
    assert view["selection"]["same_denominators"], view
    assert len(view["selection"]["same_dates"]) == 2, view
    assert all(view["arms"][arm]["sample"] == 10 for arm in ARMS), view
    assert view["selection"]["context_selection_direction_changes"] > 0, view
    assert view["methodology"]["validation_only_no_final_test_evaluation"] is True

    malformed = _rows()
    malformed = [r for r in malformed if not (r["ticker"] == "T2" and r["context_direction"] == "SHORT")]
    result = evaluate_factorial(malformed, _models(), capacity=5)
    assert result["selection"]["rejection_reasons"].get("MISSING_DIRECTION_HYPOTHESIS") == 2, result

    print("PASS | TMAD 6.2.59 | streamed atomic JSON; same-denominator validation-only factorial")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
