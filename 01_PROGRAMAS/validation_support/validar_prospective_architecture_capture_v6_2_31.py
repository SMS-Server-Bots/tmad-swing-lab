from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALERTS = ROOT / "01_PROGRAMAS" / "alerts_bot"


def _kw(call: ast.Call, name: str):
    for item in call.keywords:
        if item.arg == name:
            return item.value
    return None


def _is_true(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _is_false(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def main() -> int:
    engine_path = ALERTS / "alerts_engine.py"
    main_path = ALERTS / "main.py"
    engine_source = engine_path.read_text(encoding="utf-8-sig")
    main_source = main_path.read_text(encoding="utf-8-sig")

    engine_tree = ast.parse(engine_source)
    run_defs = [
        node for node in ast.walk(engine_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "run_alert_scan"
    ]
    assert len(run_defs) == 1, len(run_defs)
    run_def = run_defs[0]
    args = run_def.args.args
    names = [arg.arg for arg in args]
    assert "capture_prospective_architecture" in names, names
    capture_index = names.index("capture_prospective_architecture")
    defaults_start = len(args) - len(run_def.args.defaults)
    assert capture_index >= defaults_start, (capture_index, defaults_start)
    default_node = run_def.args.defaults[capture_index - defaults_start]
    assert _is_false(default_node), ast.dump(default_node)

    # The engine must make architecture persistence depend on the explicit flag
    # when historical=True. This source assertion complements the call-site audit.
    assert "architecture_capture_allowed" in engine_source
    assert "(not historical) or capture_prospective_architecture" in engine_source
    assert 'if architecture_capture_allowed and "deep" in str(run.get("session_type") or "").lower()' in engine_source

    # Audit every Python call site in Alerts. Exactly one call may explicitly grant
    # capture permission, and it must be the authoritative daily-close materializer.
    granted: list[tuple[str, int, bool]] = []
    explicit_false: list[tuple[str, int]] = []
    for path in sorted(ALERTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else ""
            )
            if name != "run_alert_scan":
                continue
            capture = _kw(node, "capture_prospective_architecture")
            if capture is None:
                continue
            historical = _kw(node, "historical")
            if _is_true(capture):
                granted.append((path.name, int(node.lineno), _is_true(historical)))
            elif _is_false(capture):
                explicit_false.append((path.name, int(node.lineno)))
            else:
                raise AssertionError(
                    f"dynamic capture permission forbidden: {path.name}:{node.lineno}"
                )

    assert len(granted) == 1, granted
    assert granted[0][0] == "main.py", granted
    assert granted[0][2] is True, granted

    main_tree = ast.parse(main_source)
    daily_defs = [
        node for node in ast.walk(main_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_daily_recalibration"
    ]
    assert len(daily_defs) == 1
    daily_calls = [
        node for node in ast.walk(daily_defs[0])
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "run_alert_scan")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "run_alert_scan")
        )
    ]
    assert len(daily_calls) == 1, len(daily_calls)
    daily_call = daily_calls[0]
    assert _is_true(_kw(daily_call, "historical")), ast.dump(daily_call)
    assert _is_true(_kw(daily_call, "capture_prospective_architecture")), ast.dump(daily_call)

    exporter_source = (ROOT / "01_PROGRAMAS" / "observability" / "runtime_data_export.py").read_text(
        encoding="utf-8-sig"
    )
    assert 'lower == "prospective_architecture_journal"' in exporter_source

    version = (ROOT / "VERSION.txt").read_text(encoding="utf-8-sig").strip()
    assert tuple(int(x) for x in version.split(".")) >= (6, 2, 31), version

    print(
        "PASS | >=6.2.31 | authoritative daily close captures prospective architecture "
        "while ordinary historical replay remains write-disabled | LIVE unchanged"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
