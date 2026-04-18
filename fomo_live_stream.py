"""
fomo_live_stream.py — Real-time transaction monitoring
========================================================
Subscribes to Helius websocket for token transactions,
filters for fomo wallet activity, maintains 24h activity feed.
"""
import json
import time
import threading
import asyncio
import websockets
from collections import deque
from datetime import datetime, timedelta

# Helius websocket endpoint
HELIUS_WS_URL = "wss://atlas-mainnet.helius-rpc.com/?api-key={api_key}"

# Activity storage: {mint: deque([event, event, ...])}
# Each event: {ts, time, kind, wallet, amount, tx_sig}
live_activity = {}
activity_lock = threading.Lock()

# Active websocket connections: {mint: ws_task}
active_streams = {}
stream_lock = threading.Lock()


def get_live_activity(mint: str, fomo_wallets: set) -> dict:
    """
    Return recent activity for a token, filtered to fomo wallets only.
    Returns last 24 hours of events.
    """
    with activity_lock:
        events = live_activity.get(mint, deque())
        cutoff = time.time() - (24 * 3600)
        
        # Filter to last 24h and fomo wallets only
        recent = [
            e for e in events 
            if e["ts"] > cutoff and e["wallet"] in fomo_wallets
        ]
        
        return {
            "available": True,
            "events": recent[-200:]  # cap at 200 most recent
        }


def add_activity_event(mint: str, event: dict):
    """Add a new transaction event to the activity feed."""
    with activity_lock:
        if mint not in live_activity:
            live_activity[mint] = deque(maxlen=500)  # keep max 500 events
        live_activity[mint].append(event)


async def subscribe_to_token(mint: str, api_key: str, fomo_wallets: set):
    """
    Subscribe to transaction stream for a token.
    Filters for fomo wallet activity and adds to feed.
    """
    uri = HELIUS_WS_URL.format(api_key=api_key)
    
    try:
        async with websockets.connect(uri) as ws:
            # Subscribe to account updates for the token mint
            subscribe_msg = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "transactionSubscribe",
                "params": [
                    {
                        "accountInclude": [mint]
                    },
                    {
                        "commitment": "confirmed",
                        "encoding": "jsonParsed",
                        "transactionDetails": "full",
                        "showRewards": False,
                        "maxSupportedTransactionVersion": 0
                    }
                ]
            }
            await ws.send(json.dumps(subscribe_msg))
            print(f"  🔴 Live stream started: {mint[:8]}...")
            
            # Listen for transactions
            async for message in ws:
                try:
                    data = json.loads(message)
                    
                    # Parse transaction
                    if "params" in data and "result" in data["params"]:
                        tx = data["params"]["result"]
                        parsed = parse_swap_transaction(tx, mint, fomo_wallets)
                        
                        if parsed:
                            add_activity_event(mint, parsed)
                            
                except Exception as e:
                    print(f"  ⚠️  Parse error: {e}")
                    
    except Exception as e:
        print(f"  ⚠️  Stream error for {mint[:8]}: {e}")


def parse_swap_transaction(tx_data: dict, mint: str, fomo_wallets: set) -> dict:
    """
    Parse a transaction to extract swap info.
    Returns event dict if it's a fomo wallet swap, else None.
    
    This is simplified - in production you'd parse Jupiter/Raydium/etc
    instruction data properly. For now, we look for token transfers.
    """
    try:
        # Get transaction signature
        signature = tx_data.get("transaction", {}).get("signatures", [""])[0]
        
        # Get account keys
        tx_obj = tx_data.get("transaction", {})
        meta = tx_data.get("meta", {})
        
        # Look for token balance changes (pre/post)
        pre_balances = meta.get("preTokenBalances", [])
        post_balances = meta.get("postTokenBalances", [])
        
        # Find changes for our mint
        for pre in pre_balances:
            if pre.get("mint") != mint:
                continue
                
            owner = pre.get("owner")
            if not owner or owner not in fomo_wallets:
                continue
            
            # Find matching post balance
            pre_amt = float(pre.get("uiTokenAmount", {}).get("uiAmount", 0))
            post_amt = 0
            
            for post in post_balances:
                if (post.get("mint") == mint and 
                    post.get("owner") == owner and
                    post.get("accountIndex") == pre.get("accountIndex")):
                    post_amt = float(post.get("uiTokenAmount", {}).get("uiAmount", 0))
                    break
            
            delta = post_amt - pre_amt
            
            # Ignore tiny changes
            if abs(delta) < 0.01:
                continue
            
            # Determine kind
            kind = "buy" if delta > 0 else "sell"
            
            return {
                "ts": time.time(),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "kind": kind,
                "wallet": owner,
                "amount": round(abs(delta), 2),
                "delta": round(delta, 2),
                "tx_sig": signature[:16] + "..."
            }
            
    except Exception as e:
        # Silently fail - most transactions won't be swaps
        pass
    
    return None


def start_stream(mint: str, api_key: str, fomo_wallets: set):
    """Start a websocket stream for a token in a background thread."""
    with stream_lock:
        if mint in active_streams:
            return  # already running
        
        def run_async_stream():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(subscribe_to_token(mint, api_key, fomo_wallets))
        
        thread = threading.Thread(target=run_async_stream, daemon=True)
        thread.start()
        active_streams[mint] = thread


def stop_stream(mint: str):
    """Stop the stream for a token (when token is removed)."""
    with stream_lock:
        if mint in active_streams:
            # Thread is daemon, will die when main process ends
            # In production, you'd send a proper close signal to the websocket
            del active_streams[mint]
    
    with activity_lock:
        if mint in live_activity:
            del live_activity[mint]
