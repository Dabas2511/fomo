"""
fomo Holdings Dashboard — Multi-Token Server (Railway Edition)
==============================================================
Fixes:
- Token decimals applied correctly (raw amount / 10^decimals)
- Total supply fetched from mint info, not just top 100 holders
- Non-fomo cache is NOT permanent — wallets are re-checked each scan
  (only confirmed fomo wallets are cached permanently)
- Top 100 holders scanned, % calculated against real total supply

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
# CONFIG
# ─────────────────────────────────────────────
HELIUS_API_KEY   = os.environ.get("HELIUS_API_KEY", "")
REFRESH_INTERVAL = 180
PORT             = int(os.environ.get("PORT", 8765))
TOP_HOLDERS      = 100
# ─────────────────────────────────────────────

FOMO_FEE_WALLET  = "R4rNJHaffSUotNmqSKNEfDcJE8A7zJUkaoM5Jkd7cYX"
HELIUS_RPC_URL   = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_API_URL   = f"https://api.helius.xyz/v0"
TOKENS_FILE      = "fomo_tokens.json"
CACHE_DIR        = "fomo_cache"

GLOBAL_FOMO_WALLETS_FILE = "global_fomo_wallets.json"   # permanent — confirmed fomo
WALLET_LABELS_FILE       = "wallet_labels.json"          # manual labels

os.makedirs(CACHE_DIR, exist_ok=True)

tokens_state  = {}
tokens_lock   = threading.Lock()

global_fomo   = {}   # { wallet: True } — confirmed fomo wallets (permanent cache)
wallet_labels = {}   # { wallet: "name" }
global_lock   = threading.Lock()

# ── Persistence ───────────────────────────────

def load_tokens() -> dict:
    try:
        with open(TOKENS_FILE) as f: return json.load(f)
    except Exception: return {}

def save_tokens(tokens: dict):
    try:
        with open(TOKENS_FILE, "w") as f: json.dump(tokens, f, indent=2)
    except Exception: pass

def cache_path(mint: str) -> str:
    return os.path.join(CACHE_DIR, f"{mint[:16]}.json")

def load_cache(mint: str) -> dict:
    try:
        with open(cache_path(mint)) as f: return json.load(f)
    except Exception: return {}

def save_cache(mint: str, fomo_holders: dict):
    try:
        with open(cache_path(mint), "w") as f: json.dump(fomo_holders, f)
    except Exception: pass

def load_global_wallets():
    global global_fomo, wallet_labels
    try:
        with open(GLOBAL_FOMO_WALLETS_FILE) as f:
            global_fomo = json.load(f)
    except Exception:
        global_fomo = {}
    try:
        with open(WALLET_LABELS_FILE) as f:
            wallet_labels = json.load(f)
    except Exception:
        wallet_labels = {}
    print(f"  📂 Loaded {len(global_fomo)} known fomo wallets | {len(wallet_labels)} labels")

def save_global_fomo():
    try:
        with open(GLOBAL_FOMO_WALLETS_FILE, "w") as f: json.dump(global_fomo, f)
    except Exception: pass

def save_wallet_labels():
    try:
        with open(WALLET_LABELS_FILE, "w") as f: json.dump(wallet_labels, f, indent=2)
    except Exception: pass

# ── Helius helpers ────────────────────────────

def get_token_info(mint: str) -> dict:
    """
    Returns { name, symbol, decimals, supply } for a token.
    Supply is the REAL total supply (not just top holders).
    """
    info = {"name": mint[:8]+"...", "symbol": "", "decimals": 6, "supply": 0}
    try:
        # Get metadata + supply from getAsset
        resp = requests.post(HELIUS_RPC_URL, json={
            "jsonrpc": "2.0", "id": "asset",
            "method": "getAsset",
            "params": {"id": mint}
        }, timeout=15)
        data = resp.json().get("result", {})
        meta = data.get("content", {}).get("metadata", {})
        info["name"]   = meta.get("name", mint[:8]+"...")
        info["symbol"] = meta.get("symbol", "")

        # Get decimals and supply from token_info
        token_info = data.get("token_info", {})
        info["decimals"] = token_info.get("decimals", 6)
        raw_supply       = token_info.get("supply", 0)
        info["supply"]   = raw_supply / (10 ** info["decimals"]) if raw_supply else 0

    except Exception as e:
        print(f"  ⚠️  Token info error: {e}")

    # Fallback: use getMintAccountInfo for supply if still 0
    if info["supply"] == 0:
        try:
            resp2 = requests.post(HELIUS_RPC_URL, json={
                "jsonrpc": "2.0", "id": "mint",
                "method": "getAccountInfo",
                "params": [mint, {"encoding": "jsonParsed"}]
            }, timeout=15)
            parsed = resp2.json().get("result", {}).get("value", {}).get("data", {}).get("parsed", {}).get("info", {})
            decimals   = parsed.get("decimals", info["decimals"])
            raw_supply = int(parsed.get("supply", 0))
            info["decimals"] = decimals
            info["supply"]   = raw_supply / (10 ** decimals) if raw_supply else 0
        except Exception as e:
            print(f"  ⚠️  Mint fallback error: {e}")

    return info

def get_top_holders(mint: str, decimals: int) -> dict:
    """
    Fetch top TOP_HOLDERS holders.
    Returns { owner: human_readable_amount }
    """
    holders = {}
    try:
        resp = requests.post(HELIUS_RPC_URL, json={
            "jsonrpc": "2.0", "id": "h", "method": "getTokenAccounts",
            "params": {"mint": mint, "page": 1, "limit": TOP_HOLDERS, "displayOptions": {}}
        }, timeout=30)
        accounts = resp.json().get("result", {}).get("token_accounts", [])
        for acc in accounts:
            owner     = acc.get("owner", "")
            raw_amount = int(acc.get("amount", 0))
            if owner and raw_amount > 0:
                # Convert raw amount to human readable using decimals
                holders[owner] = raw_amount / (10 ** decimals)
    except Exception as e:
        print(f"  ⚠️  Holder fetch error: {e}")
    return holders

def is_fomo_wallet(wallet: str) -> bool:
    """
    Check global fomo cache first (only fomo-confirmed wallets are cached).
    All others are re-scanned every time to avoid false negatives.
    """
    with global_lock:
        if wallet in global_fomo:
            return True

    # Scan transactions
    result = scan_wallet_for_fomo(wallet)

    if result:
        with global_lock:
            global_fomo[wallet] = True
        save_global_fomo()

    return result

def scan_wallet_for_fomo(wallet: str) -> bool:
    """Scan a wallet's transactions for fomo fee wallet."""
    # Enhanced API (faster)
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

    # RPC fallback
    try:
        sigs = requests.post(HELIUS_RPC_URL, json={
            "jsonrpc": "2.0", "id": "s", "method": "getSignaturesForAddress",
            "params": [wallet, {"limit": 20}]
        }, timeout=20).json().get("result", [])
        for s in sigs:
            sig = s.get("signature", "")
            if not sig: continue
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
        if mint not in tokens_state: return
        tokens_state[mint]["status"] = "refreshing"

    name = tokens_state.get(mint, {}).get("name", mint[:8])
    print(f"\n🔄 [{datetime.now().strftime('%H:%M:%S')}] Refreshing {name}")

    # Step 1: Get real token info (decimals + total supply)
    token_info   = get_token_info(mint)
    decimals     = token_info["decimals"]
    total_supply = token_info["supply"]
    token_name   = token_info.get("symbol") or token_info.get("name") or name

    print(f"  Decimals: {decimals} | Total supply: {total_supply:,.2f}")

    # Step 2: Get top 100 holders with correct amounts
    top_holders = get_top_holders(mint, decimals)
    print(f"  Top {len(top_holders)} holders fetched")

    # Step 3: Load previously confirmed fomo wallets for this token
    cached_fomo  = load_cache(mint)
    fomo_holders = {}

    # Keep previously confirmed fomo wallets still in top holders
    for wallet in cached_fomo:
        if wallet in top_holders:
            fomo_holders[wallet] = top_holders[wallet]

    # Also include manually labeled wallets
    with global_lock:
        labels_copy = dict(wallet_labels)
    for wallet in labels_copy:
        if wallet in top_holders and wallet not in fomo_holders:
            fomo_holders[wallet] = top_holders[wallet]

    # Step 4: Scan wallets not yet confirmed as fomo
    new_wallets = [w for w in top_holders if w not in fomo_holders]
    print(f"  {len(fomo_holders)} from cache/labels | Scanning {len(new_wallets)} wallets")

    for i, wallet in enumerate(new_wallets, 1):
        if i % 20 == 0:
            print(f"  Progress: {i}/{len(new_wallets)}")
        if is_fomo_wallet(wallet):
            fomo_holders[wallet] = top_holders[wallet]
        time.sleep(0.02)

    save_cache(mint, fomo_holders)

    # Step 5: Calculate % against REAL total supply
    fomo_supply = sum(fomo_holders.values())
    fomo_pct    = round(fomo_supply / total_supply * 100, 4) if total_supply > 0 else 0
    now_str     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    top_fomo = sorted(
        [{"wallet": w, "amount": round(a, 2),
          "pct": round(a / total_supply * 100, 4) if total_supply > 0 else 0,
          "label": labels_copy.get(w, "")}
         for w, a in fomo_holders.items()],
        key=lambda x: x["amount"], reverse=True
    )[:50]

    with tokens_lock:
        if mint not in tokens_state: return
        history = tokens_state[mint].get("history", [])
        history.append({"time": now_str, "fomo_pct": fomo_pct,
                        "fomo_count": len(fomo_holders), "fomo_supply": fomo_supply})
        history = history[-100:]
        tokens_state[mint].update({
            "status": "ready", "last_updated": now_str,
            "name": token_name,
            "total_holders": len(top_holders),
            "total_supply": round(total_supply, 2),
            "decimals": decimals,
            "fomo_holders_count": len(fomo_holders),
            "fomo_supply": round(fomo_supply, 2),
            "fomo_pct": fomo_pct,
            "non_fomo_pct": round(100 - fomo_pct, 4),
            "top_fomo_holders": top_fomo,
            "history": history,
        })

    print(f"  ✅ {len(fomo_holders)} fomo holders | {fomo_pct:.2f}% of total supply | Cache: {len(global_fomo)} fomo wallets")

def token_loop(mint: str):
    refresh_token(mint)
    while True:
        time.sleep(REFRESH_INTERVAL)
        with tokens_lock:
            if mint not in tokens_state: break
        refresh_token(mint)

def add_token(mint: str):
    with tokens_lock:
        if mint in tokens_state:
            return False, "Already tracking this token"
        tokens_state[mint] = {
            "status": "initializing", "name": mint[:8]+"...",
            "last_updated": None, "total_holders": 0, "total_supply": 0,
            "decimals": 6, "fomo_holders_count": 0, "fomo_supply": 0,
            "fomo_pct": 0, "non_fomo_pct": 100,
            "top_fomo_holders": [], "history": [],
        }
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
            with tokens_lock: data = tokens_state.get(mint)
            self.send_json(data if data else {"error": "Not found"}, 200 if data else 404)
        elif self.path.startswith("/api/refresh/"):
            mint = self.path.split("/api/refresh/")[1]
            with tokens_lock: exists = mint in tokens_state
            if exists:
                threading.Thread(target=refresh_token, args=(mint,), daemon=True).start()
                self.send_json({"message": "Refresh started"})
            else:
                self.send_json({"error": "Not found"}, 404)
        elif self.path == "/api/labels":
            with global_lock: self.send_json(dict(wallet_labels))
        elif self.path == "/api/cache/stats":
            with global_lock:
                self.send_json({"fomo_wallets": len(global_fomo), "labeled_wallets": len(wallet_labels)})
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
        elif self.path == "/api/labels":
            body = self.read_body()
            wallet = body.get("wallet", "").strip()
            name   = body.get("name", "").strip()
            if not wallet:
                self.send_json({"error": "wallet required"}, 400); return
            with global_lock:
                if name:
                    wallet_labels[wallet] = name
                    global_fomo[wallet] = True
                else:
                    wallet_labels.pop(wallet, None)
            save_wallet_labels()
            save_global_fomo()
            self.send_json({"ok": True, "wallet": wallet, "name": name})
        else:
            self.send_response(404); self.end_headers()

    def do_DELETE(self):
        if self.path.startswith("/api/token/"):
            mint = self.path.split("/api/token/")[1]
            ok, msg = remove_token(mint)
            self.send_json({"ok": ok, "message": msg})
        elif self.path.startswith("/api/label/"):
            wallet = self.path.split("/api/label/")[1]
            with global_lock: wallet_labels.pop(wallet, None)
            save_wallet_labels()
            self.send_json({"ok": True})
        else:
            self.send_response(404); self.end_headers()

# ── Main ──────────────────────────────────────

def main():
    if not HELIUS_API_KEY:
        print("❗ HELIUS_API_KEY environment variable not set.")
        return

    print("=" * 50)
    print("  fomo Multi-Token Dashboard — Server")
    print(f"  Port: {PORT} | Top {TOP_HOLDERS} holders per token")
    print("=" * 50)

    load_global_wallets()

    saved = load_tokens()
    if saved:
        print(f"\n📂 Restoring {len(saved)} saved token(s)...")
        for mint, name in saved.items():
            with tokens_lock:
                tokens_state[mint] = {
                    "status": "initializing", "name": name,
                    "last_updated": None, "total_holders": 0, "total_supply": 0,
                    "decimals": 6, "fomo_holders_count": 0, "fomo_supply": 0,
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
