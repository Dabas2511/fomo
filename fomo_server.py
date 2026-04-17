"""
fomo Holdings Dashboard — Multi-Token Server (Railway Edition)
==============================================================
Tracks multiple Solana tokens and serves data via HTTP API.
Designed to run on Railway.app (or any cloud server).

Setup on Railway:
    Set environment variable: HELIUS_API_KEY=your_key_here
"""

import requests
import json
import time
import threading
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# ─────────────────────────────────────────────
# CONFIG — Set HELIUS_API_KEY as environment variable on Railway
# ─────────────────────────────────────────────
HELIUS_API_KEY   = os.environ.get("HELIUS_API_KEY", "")
REFRESH_INTERVAL = 180
PORT             = int(os.environ.get("PORT", 8765))  # Railway sets PORT automatically
TOP_HOLDERS      = 100                                 # Only scan top N holders
# ─────────────────────────────────────────────

FOMO_FEE_WALLET  = "R4rNJHaffSUotNmqSKNEfDcJE8A7zJUkaoM5Jkd7cYX"
HELIUS_RPC_URL   = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_API_URL   = f"https://api.helius.xyz/v0"
TOKENS_FILE      = "fomo_tokens.json"
CACHE_DIR        = "fomo_cache"

os.makedirs(CACHE_DIR, exist_ok=True)

tokens_state = {}
tokens_lock  = threading.Lock()

# ── Token list persistence ────────────────────

def load_tokens() -> dict:
    try:
        with open(TOKENS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_tokens(tokens: dict):
    try:
        with open(TOKENS_FILE, "w") as f:
            json.dump(tokens, f, indent=2)
    except Exception:
        pass

def cache_path(mint: str) -> str:
    return os.path.join(CACHE_DIR, f"{mint[:16]}.json")

def load_cache(mint: str) -> dict:
    try:
        with open(cache_path(mint)) as f:
            return json.load(f)
    except Exception:
        return {}

def save_cache(mint: str, fomo_holders: dict):
    try:
        with open(cache_path(mint), "w") as f:
            json.dump(fomo_holders, f)
    except Exception:
        pass

# ── Helius helpers ────────────────────────────

def get_token_name(mint: str) -> str:
    try:
        resp = requests.post(HELIUS_RPC_URL, json={
            "jsonrpc": "2.0", "id": "meta",
            "method": "getAsset",
            "params": {"id": mint}
        }, timeout=15)
        data = resp.json().get("result", {})
        symbol = data.get("content", {}).get("metadata", {}).get("symbol", "")
        name   = data.get("content", {}).get("metadata", {}).get("name", "")
        return symbol or name or mint[:8] + "..."
    except Exception:
        return mint[:8] + "..."

def get_all_holders(mint: str) -> dict:
    """Fetch only the top TOP_HOLDERS holders (largest balances first)."""
    holders = {}
    try:
        resp = requests.post(HELIUS_RPC_URL, json={
            "jsonrpc": "2.0", "id": "h", "method": "getTokenAccounts",
            "params": {"mint": mint, "page": 1, "limit": TOP_HOLDERS, "displayOptions": {}}
        }, timeout=30)
        accounts = resp.json().get("result", {}).get("token_accounts", [])
        for acc in accounts:
            owner  = acc.get("owner", "")
            amount = int(acc.get("amount", 0))
            if owner and amount > 0:
                holders[owner] = amount
    except Exception as e:
        print(f"  ⚠️  Holder fetch error: {e}")
    return holders

def is_fomo_wallet(wallet: str) -> bool:
    try:
        resp = requests.get(
            f"{HELIUS_API_URL}/addresses/{wallet}/transactions",
            params={"api-key": HELIUS_API_KEY, "limit": 20},
            timeout=20
        )
        if resp.status_code == 200:
            for tx in resp.json():
                if tx.get("feePayer") == FOMO_FEE_WALLET:
                    return True
                for acc in tx.get("accountData", []):
                    if acc.get("account") == FOMO_FEE_WALLET:
                        return True
                for t in tx.get("nativeTransfers", []):
                    if t.get("toUserAccount") == FOMO_FEE_WALLET:
                        return True
    except Exception:
        pass
    try:
        sigs = requests.post(HELIUS_RPC_URL, json={
            "jsonrpc": "2.0", "id": "s", "method": "getSignaturesForAddress",
            "params": [wallet, {"limit": 20}]
        }, timeout=20).json().get("result", [])
        for s in sigs:
            sig = s.get("signature", "")
            if not sig:
                continue
            tx = requests.post(HELIUS_RPC_URL, json={
                "jsonrpc": "2.0", "id": "t", "method": "getTransaction",
                "params": [sig, {"encoding": "json", "maxSupportedTransactionVersion": 0}]
            }, timeout=20).json().get("result")
            if tx:
                keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
                if FOMO_FEE_WALLET in keys:
                    return True
            time.sleep(0.02)
    except Exception:
        pass
    return False

# ── Core refresh ──────────────────────────────

def refresh_token(mint: str):
    with tokens_lock:
        if mint not in tokens_state:
            return
        tokens_state[mint]["status"] = "refreshing"

    name = tokens_state.get(mint, {}).get("name", mint[:8])
    print(f"\n🔄 [{datetime.now().strftime('%H:%M:%S')}] Refreshing {name}")

    all_holders  = get_all_holders(mint)
    total_supply = sum(all_holders.values())
    cached_fomo  = load_cache(mint)
    fomo_holders = {}

    for wallet in cached_fomo:
        if wallet in all_holders:
            fomo_holders[wallet] = all_holders[wallet]

    new_wallets = [w for w in all_holders if w not in cached_fomo]
    print(f"  Top {TOP_HOLDERS} holders | Scanning {len(new_wallets)} new wallets")

    for i, wallet in enumerate(new_wallets, 1):
        if i % 20 == 0:
            print(f"  Progress: {i}/{len(new_wallets)}")
        if is_fomo_wallet(wallet):
            fomo_holders[wallet] = all_holders[wallet]
        time.sleep(0.02)

    save_cache(mint, fomo_holders)

    fomo_supply = sum(fomo_holders.values())
    fomo_pct    = round(fomo_supply / total_supply * 100, 4) if total_supply > 0 else 0
    now_str     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    top_fomo = sorted(
        [{"wallet": w, "amount": a,
          "pct": round(a / total_supply * 100, 4) if total_supply > 0 else 0}
         for w, a in fomo_holders.items()],
        key=lambda x: x["amount"], reverse=True
    )[:50]

    with tokens_lock:
        if mint not in tokens_state:
            return
        history = tokens_state[mint].get("history", [])
        history.append({"time": now_str, "fomo_pct": fomo_pct,
                        "fomo_count": len(fomo_holders), "fomo_supply": fomo_supply})
        history = history[-100:]
        tokens_state[mint].update({
            "status": "ready", "last_updated": now_str,
            "total_holders": len(all_holders), "total_supply": total_supply,
            "fomo_holders_count": len(fomo_holders), "fomo_supply": fomo_supply,
            "fomo_pct": fomo_pct, "non_fomo_pct": round(100 - fomo_pct, 4),
            "top_fomo_holders": top_fomo, "history": history,
        })

    print(f"  ✅ {len(fomo_holders)} fomo holders ({fomo_pct:.2f}%)")

def token_loop(mint: str):
    refresh_token(mint)
    while True:
        time.sleep(REFRESH_INTERVAL)
        with tokens_lock:
            if mint not in tokens_state:
                break
        refresh_token(mint)

def add_token(mint: str):
    with tokens_lock:
        if mint in tokens_state:
            return False, "Already tracking this token"
        tokens_state[mint] = {
            "status": "initializing", "name": mint[:8] + "...",
            "last_updated": None, "total_holders": 0, "total_supply": 0,
            "fomo_holders_count": 0, "fomo_supply": 0,
            "fomo_pct": 0, "non_fomo_pct": 100,
            "top_fomo_holders": [], "history": [],
        }

    def fetch_name():
        name = get_token_name(mint)
        with tokens_lock:
            if mint in tokens_state:
                tokens_state[mint]["name"] = name
        toks = load_tokens()
        toks[mint] = name
        save_tokens(toks)

    threading.Thread(target=fetch_name, daemon=True).start()
    threading.Thread(target=token_loop, args=(mint,), daemon=True).start()
    return True, "Token added"

def remove_token(mint: str):
    with tokens_lock:
        if mint not in tokens_state:
            return False, "Token not found"
        del tokens_state[mint]
    toks = load_tokens()
    toks.pop(mint, None)
    save_tokens(toks)
    return True, "Token removed"

# ── HTTP Server ───────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_GET(self):
        if self.path == "/api/tokens":
            with tokens_lock:
                self.send_json({mint: dict(s) for mint, s in tokens_state.items()})
        elif self.path.startswith("/api/token/"):
            mint = self.path.split("/api/token/")[1]
            with tokens_lock:
                data = tokens_state.get(mint)
            self.send_json(data if data else {"error": "Not found"}, 200 if data else 404)
        elif self.path.startswith("/api/refresh/"):
            mint = self.path.split("/api/refresh/")[1]
            with tokens_lock:
                exists = mint in tokens_state
            if exists:
                threading.Thread(target=refresh_token, args=(mint,), daemon=True).start()
                self.send_json({"message": "Refresh started"})
            else:
                self.send_json({"error": "Not found"}, 404)
        elif self.path == "/health":
            self.send_json({"status": "ok"})
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == "/api/tokens":
            body = self.read_body()
            mint = body.get("mint", "").strip()
            if not mint:
                self.send_json({"error": "mint required"}, 400); return
            ok, msg = add_token(mint)
            self.send_json({"ok": ok, "message": msg})
        else:
            self.send_response(404); self.end_headers()

    def do_DELETE(self):
        if self.path.startswith("/api/token/"):
            mint = self.path.split("/api/token/")[1]
            ok, msg = remove_token(mint)
            self.send_json({"ok": ok, "message": msg})
        else:
            self.send_response(404); self.end_headers()

# ── Main ──────────────────────────────────────

def main():
    if not HELIUS_API_KEY:
        print("❗ HELIUS_API_KEY environment variable not set.")
        print("   On Railway: go to your project → Variables → add HELIUS_API_KEY")
        return

    print("=" * 50)
    print("  fomo Multi-Token Dashboard — Server")
    print(f"  Port: {PORT}")
    print(f"  Scanning top {TOP_HOLDERS} holders only")
    print("=" * 50)

    saved = load_tokens()
    if saved:
        print(f"\n📂 Restoring {len(saved)} saved token(s)...")
        for mint, name in saved.items():
            with tokens_lock:
                tokens_state[mint] = {
                    "status": "initializing", "name": name,
                    "last_updated": None, "total_holders": 0, "total_supply": 0,
                    "fomo_holders_count": 0, "fomo_supply": 0,
                    "fomo_pct": 0, "non_fomo_pct": 100,
                    "top_fomo_holders": [], "history": [],
                }
            threading.Thread(target=token_loop, args=(mint,), daemon=True).start()
    else:
        print("\n📭 No saved tokens. Add tokens from the dashboard.")

    print(f"\n🚀 Server starting on port {PORT}")
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Stopped.")

if __name__ == "__main__":
    main()
