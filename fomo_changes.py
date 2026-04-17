"""
fomo_changes.py — Time-window comparison module
=================================================
Standalone module that computes changes from snapshot files
written by fomo_server.py. Import and register with the main server.

Does NOT touch any core scan logic. If anything breaks here, the main
server is unaffected. Just comment out the import in fomo_server.py
to disable.
"""
import json
import time
import os

# Must match fomo_server.py paths
DATA_DIR = "/data" if os.path.isdir("/data") else "."
SNAPSHOTS_DIR = f"{DATA_DIR}/snapshots"


def snapshot_path(mint: str) -> str:
    safe = mint.replace("/", "_")
    return f"{SNAPSHOTS_DIR}/{safe}.json"


def load_snapshots(mint: str) -> list:
    try:
        with open(snapshot_path(mint)) as f:
            return json.load(f)
    except Exception:
        return []


def find_snapshot_at_age(snapshots: list, hours_ago: float):
    """Find the snapshot closest to `hours_ago` hours before now, not after."""
    if not snapshots:
        return None
    target_ts = time.time() - (hours_ago * 3600)
    best = None
    best_diff = float('inf')
    for s in snapshots:
        ts = s.get("ts", 0)
        if ts <= target_ts:
            diff = target_ts - ts
            if diff < best_diff:
                best_diff = diff
                best = s
    return best


def compute_changes(mint: str, hours_ago: float, labels: dict = None) -> dict:
    """Compare current snapshot to one N hours ago."""
    if labels is None:
        labels = {}

    snaps = load_snapshots(mint)
    if len(snaps) < 2:
        return {"available": False, "reason": "not enough history yet"}

    current = snaps[-1]
    past = find_snapshot_at_age(snaps, hours_ago)
    if not past:
        past = snaps[0]
    actual_age = (current.get("ts", 0) - past.get("ts", 0)) / 3600

    cur_holders = current.get("holders", {})
    past_holders = past.get("holders", {})
    total_supply = current.get("total_supply", 0) or past.get("total_supply", 0) or 1

    all_wallets = set(cur_holders.keys()) | set(past_holders.keys())
    wallet_changes = []
    new_entries = []
    exits = []

    for w in all_wallets:
        cur_amt = cur_holders.get(w, 0)
        past_amt = past_holders.get(w, 0)
        delta = cur_amt - past_amt
        label = labels.get(w, "")

        if past_amt == 0 and cur_amt > 0:
            new_entries.append({
                "wallet": w,
                "amount": round(cur_amt, 2),
                "label": label,
            })
        elif cur_amt == 0 and past_amt > 0:
            exits.append({
                "wallet": w,
                "amount": round(past_amt, 2),
                "label": label,
            })
        elif abs(delta) > 0.01:
            wallet_changes.append({
                "wallet": w,
                "before": round(past_amt, 2),
                "after": round(cur_amt, 2),
                "delta": round(delta, 2),
                "pct_change": round((delta / past_amt) * 100, 2) if past_amt > 0 else 0,
                "label": label,
            })

    wallet_changes.sort(key=lambda x: abs(x["delta"]), reverse=True)

    cur_supply = current.get("fomo_supply", 0)
    past_supply = past.get("fomo_supply", 0)
    supply_delta = cur_supply - past_supply
    supply_pct_change = (supply_delta / past_supply * 100) if past_supply > 0 else 0

    return {
        "available": True,
        "requested_hours_ago": hours_ago,
        "actual_hours_ago": round(actual_age, 2),
        "past_time": past.get("time"),
        "current_time": current.get("time"),
        "past_fomo_pct": past.get("fomo_pct", 0),
        "current_fomo_pct": current.get("fomo_pct", 0),
        "pct_delta": round(current.get("fomo_pct", 0) - past.get("fomo_pct", 0), 4),
        "past_holders_count": past.get("fomo_count", 0),
        "current_holders_count": current.get("fomo_count", 0),
        "count_delta": current.get("fomo_count", 0) - past.get("fomo_count", 0),
        "past_supply": round(past_supply, 2),
        "current_supply": round(cur_supply, 2),
        "supply_delta": round(supply_delta, 2),
        "supply_pct_change": round(supply_pct_change, 2),
        "new_entries": sorted(new_entries, key=lambda x: x["amount"], reverse=True),
        "exits": sorted(exits, key=lambda x: x["amount"], reverse=True),
        "wallet_changes": wallet_changes[:50],
    }


def handle_changes_request(path: str, labels: dict) -> dict:
    """
    Called by the main server's HTTP handler.
    Expects path like: /api/changes/{mint}?hours=4
    """
    parts = path.split("/api/changes/")[1].split("?")
    mint = parts[0]
    hours = 4.0
    if len(parts) > 1:
        for kv in parts[1].split("&"):
            if kv.startswith("hours="):
                try:
                    hours = float(kv.split("=")[1])
                except Exception:
                    pass
    return compute_changes(mint, hours, labels)
