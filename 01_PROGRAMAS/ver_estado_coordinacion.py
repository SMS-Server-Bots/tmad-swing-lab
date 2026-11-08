from __future__ import annotations
import json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/"data_bot"))
import config
from coordination_bus import read_data_state, read_ack, read_heartbeat, heartbeat_age_seconds
state=read_data_state(config.COORDINATION_DB)
ack=read_ack(config.COORDINATION_DB,"alerts_bot")
data_hb=read_heartbeat(config.COORDINATION_DB,"data_bot")
alerts_hb=read_heartbeat(config.COORDINATION_DB,"alerts_bot")
out={
 "generation": int(state.get("generation") or 0),
 "bootstrap_id": state.get("bootstrap_id"),
 "bootstrap_complete": bool(state.get("bootstrap_complete")),
 "core_ready": bool(state.get("core_ready")),
 "universe": int(state.get("universe_tickers") or 0),
 "eligible": int(state.get("eligible_tickers") or 0),
 "excluded": int(state.get("excluded_tickers") or 0),
 "pending": int(state.get("pending_tickers") or 0),
 "remaining": int(state.get("remaining_tickers") or 0),
 "alerts_ack": {k: ack.get(k) for k in ("generation","status","attempt_count","message","updated_at_utc")},
 "data_heartbeat": {"status":data_hb.get("status"),"age_seconds":heartbeat_age_seconds(data_hb),"updated_at_utc":data_hb.get("updated_at_utc")},
 "alerts_heartbeat": {"status":alerts_hb.get("status"),"age_seconds":heartbeat_age_seconds(alerts_hb),"updated_at_utc":alerts_hb.get("updated_at_utc")},
}
print(json.dumps(out,ensure_ascii=False,indent=2))
