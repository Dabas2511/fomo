"""
fomo Holdings Dashboard — Multi-Token Server (Railway Edition)
==============================================================
NEW DETECTION METHOD — Token Account Scanning:
- Instead of scanning each wallet's entire history (could be 1000+ txns)
- We scan only the holder's TOKEN ACCOUNT for the specific mint
- This gives us just UNC-related txns (maybe 5-20 per wallet)
- Much faster and more accurate for whales

Setup on Railway:
    Set environment variable: HELIUS_API_KEY=your_key_here
"""

import requests
import json
import time
import threading
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
HELIUS_API_KEY   = os.environ.get("HELIUS_API_KEY", "")
REFRESH_INTERVAL = 180
PORT             = int(os.environ.get("PORT", 8765))
TOP_HOLDERS      = 100
PARALLEL_WORKERS = 10
TX_LIMIT         = 100   # txns per token account (should be plenty)
# ─────────────────────────────────────────────

FOMO_FEE_WALLET      = "R4rNJHaffSUotNmqSKNEfDcJE8A7zJUkaoM5Jkd7cYX"
FOMO_JITO_IDENTIFIER = "jitodontfront1111111111111111111TradeonFomo"

HELIUS_RPC_URL   = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_API_URL   = f"https://api.helius.xyz/v0"
TOKENS_FILE      = "fomo_tokens.json"

GLOBAL_FOMO_WALLETS_FILE = "global_fomo_wallets.json"
WALLET_LABELS_FILE       = "wallet_labels.json"

tokens_state  = {}
tokens_lock   = threading.Lock()
global_fomo   = {}
wallet_labels = {}
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

def load_globals():
    global global_fomo, wallet_labels
    try:
        with open(GLOBAL_FOMO_WALLETS_FILE) as f: global_fomo = json.load(f)
    except Exception: global_fomo = {}
    try:
        with open(WALLET_LABELS_FILE) as f: wallet_labels = json.load(f)
    except Exception: wallet_labels = {}
    print(f"  📂 {len(global_fomo)} known fomo wallets | {len(wallet_labels)} labels")

def save_global_fomo():
    try:
        with open(GLOBAL_FOMO_WALLETS_FILE, "w") as f: json.dump(global_fomo, f)
    except Exception: pass

def save_wallet_labels():
    try:
        with open(WALLET_LABELS_FILE, "w") as f: json.dump(wallet_labels, f, indent=2)
    except Exception: pass

def mark_as_fomo(wallet: str):
    with global_lock:
        if wallet not in global_fomo:
            global_fomo[wallet] = True
            save_global_fomo()

# ── Helius helpers ────────────────────────────

def get_token_info(mint: str) -> dict:
    info = {"name": mint[:8]+"...", "symbol": "", "decimals": 6, "supply": 0}
    try:
        resp = requests.post(HELIUS_RPC_URL, json={
            "jsonrpc": "2.0", "id": "asset",
            "method": "getAsset", "params": {"id": mint}
        }, timeout=15)
        data = resp.json().get("result", {})
        meta = data.get("content", {}).get("metadata", {})
        info["name"]   = meta.get("name", mint[:8]+"...")
        info["symbol"] = meta.get("symbol", "")
        token_info = data.get("token_info", {})
        info["decimals"] = token_info.get("decimals", 6)
        raw_supply       = token_info.get("supply", 0)
        info["supply"]   = raw_supply / (10 ** info["decimals"]) if raw_supply else 0
    except Exception as e:
        print(f"  ⚠️  Token info error: {e}")

    if info["supply"] == 0:
        try:
            resp2 = requests.post(HELIUS_RPC_URL, json={
                "jsonrpc": "2.0", "id": "mint", "method": "getAccountInfo",
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

def get_top_holders(mint: str, decimals: int) -> list:
    """
    Returns list of (wallet, token_account, amount) tuples.
    We need the TOKEN ACCOUNT (not just wallet) for targeted scanning.
    """
    holders = []
    try:
        resp = requests.post(HELIUS_RPC_URL, json={
            "jsonrpc": "2.0", "id": "h", "method": "getTokenAccounts",
            "params": {"mint": mint, "page": 1, "limit": TOP_HOLDERS, "displayOptions": {}}
        }, timeout=30)
        accounts = resp.json().get("result", {}).get("token_accounts", [])
        for acc in accounts:
            owner         = acc.get("owner", "")
            token_account = acc.get("address", "")
            raw_amount    = int(acc.get("amount", 0))
            if owner and raw_amount > 0 and token_account:
                amount = raw_amount / (10 ** decimals)
                holders.append((owner, token_account, amount))
    except Exception as e:
        print(f"  ⚠️  Holder fetch error: {e}")
    return holders

def tx_is_fomo(tx: dict) -> bool:
    """Check if a single transaction is a fomo transaction."""
    # USDC fee transfer to fomo wallet
    for t in tx.get("tokenTransfers", []):
        if t.get("toUserAccount") == FOMO_FEE_WALLET:
            return True

    # Jito identifier
    for acc in tx.get("accountData", []):
        if acc.get("account") == FOMO_JITO_IDENTIFIER:
            return True

    return False

def scan_token_account_for_fomo(token_account: str, wallet: str) -> bool:
    """
    Scan a token account's transactions for fomo markers.
    Token accounts have MUCH shorter history than wallets (only txns
    involving this specific mint), so this is fast and accurate.
    """
    DEBUG_WALLET = "ACFNvbyeyxRtTX29vYyp6ZURGyk1VUuTRmBjYCuSQZKL"
    is_debug = (wallet == DEBUG_WALLET)

    try:
        resp = requests.get(
            f"{HELIUS_API_URL}/addresses/{token_account}/transactions",
            params={"api-key": HELIUS_API_KEY, "limit": TX_LIMIT},
            timeout=30
        )

        if resp.status_code == 429:
            time.sleep(2)
            resp = requests.get(
                f"{HELIUS_API_URL}/addresses/{token_account}/transactions",
                params={"api-key": HELIUS_API_KEY, "limit": TX_LIMIT},
                timeout=30
            )

        if is_debug:
            print(f"  🔍 DEBUG {wallet[:12]} | token_acct={token_account[:12]} | status={resp.status_code}")

        if resp.status_code == 200:
            txns = resp.json()
            if is_debug:
                print(f"  🔍 DEBUG got {len(txns)} txns for {wallet[:12]}")
                swap_count = sum(1 for tx in txns if tx.get("type") == "SWAP")
                print(f"  🔍 DEBUG {swap_count} SWAPs out of {len(txns)} txns")
                for i, tx in enumerate(txns[:5]):
                    has_fomo = any(
                        t.get("toUserAccount") == FOMO_FEE_WALLET
                        for t in tx.get("tokenTransfers", [])
                    )
                    has_jito = any(
                        acc.get("account") == FOMO_JITO_IDENTIFIER
                        for acc in tx.get("accountData", [])
                    )
                    print(f"  🔍 DEBUG tx[{i}] type={tx.get('type')} fomo_fee={has_fomo} jito={has_jito}")

            for tx in txns:
                if tx_is_fomo(tx):
                    mark_as_fomo(wallet)
                    if is_debug:
                        print(f"  🔍 DEBUG ✅ FOUND FOMO for {wallet[:12]}")
                    return True
        else:
            if is_debug:
                print(f"  🔍 DEBUG bad response: {resp.text[:200]}")
    except Exception as e:
        print(f"  ⚠️  Scan error for {wallet[:8]}: {e}")

    if is_debug:
        print(f"  🔍 DEBUG NOT detected as fomo: {wallet[:12]}")

    return False

# ── Core refresh ──────────────────────────────

def refresh_token(mint: str):
    with tokens_lock:
        if mint not in tokens_state: return
        tokens_state[mint]["status"] = "refreshing"

    name = tokens_state.get(mint, {}).get("name", mint[:8])
    print(f"\n🔄 [{datetime.now().strftime('%H:%M:%S')}] Refreshing {name}")

    token_info   = get_token_info(mint)
    decimals     = token_info["decimals"]
    total_supply = token_info["supply"]
    token_name   = token_info.get("symbol") or token_info.get("name") or name

    print(f"  Decimals: {decimals} | Total supply: {total_supply:,.2f}")

    top_holders = get_top_holders(mint, decimals)
    print(f"  Top {len(top_holders)} holders fetched")

    fomo_holders = {}     # {wallet: amount}
    to_scan      = []     # list of (wallet, token_account, amount)

    with global_lock:
        labels_copy      = dict(wallet_labels)
        global_fomo_copy = dict(global_fomo)

    for wallet, token_account, amount in top_holders:
        if wallet in labels_copy or wallet in global_fomo_copy:
            fomo_holders[wallet] = amount
        else:
            to_scan.append((wallet, token_account, amount))

    print(f"  {len(fomo_holders)} from cache/labels | Scanning {len(to_scan)} wallets via token accounts (workers={PARALLEL_WORKERS})")

    def check_holder(info):
        wallet, token_account, amount = info
        is_fomo = scan_token_account_for_fomo(token_account, wallet)
        return wallet, amount, is_fomo

    completed = 0
    fomo_count = 0
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
        futures = {executor.submit(check_holder, h): h for h in to_scan}
        for future in as_completed(futures):
            completed += 1
            try:
                wallet, amount, is_fomo = future.result()
                if is_fomo:
                    fomo_holders[wallet] = amount
                    fomo_count += 1
                    print(f"  ✅ [{completed}/{len(to_scan)}] fomo: {wallet[:12]}... ({amount:,.2f})")
                elif completed % 10 == 0:
                    print(f"  Progress: {completed}/{len(to_scan)} ({fomo_count} found)")
            except Exception as e:
                print(f"  ⚠️  Error: {e}")

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

    with global_lock:
        total_cached = len(global_fomo)
    print(f"  ✅ DONE: {len(fomo_holders)} fomo | {fomo_pct:.2f}% | Global cache: {total_cached}")

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
    print(f"  Port: {PORT}")
    print(f"  Top {TOP_HOLDERS} holders | Token account scan ({TX_LIMIT} per account)")
    print(f"  Parallel workers: {PARALLEL_WORKERS}")
    print("=" * 50)

    load_globals()

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
        print("\n📭 No saved tokens.")

    print(f"\n🚀 Server starting on port {PORT}")
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Stopped.")

if __name__ == "__main__":
    main()
