"""
XAUUSD Bearish Engulfing Sell Strategy — Backtester
====================================================
Strategy Rules:
  - Previous candle : Green (Bullish)  close > open
  - Signal candle   : Red (Bearish)    close < open
  - Signal candle open  >= prev candle open   (body engulfs top)
  - Signal candle close <= prev candle close  (body engulfs bottom)
  - Upper/lower wicks : allowed
  - Entry : Sell at next 1m candle OPEN
  - SL    : 3–8 pips above entry  (grid search)
  - TP    : RR 1:3 – 1:8          (grid search)

Two modes:
  python backtest.py build   → tick data → candle cache
  python backtest.py run     → candle cache → 36 combo results
"""

import os, sys, csv, io, ctypes, ctypes.util
import itertools, json, re, struct
from datetime import datetime
from pathlib import Path

# ══════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════
GDRIVE_FOLDER_URL = os.environ.get("GDRIVE_FOLDER_URL", "")
DATA_DIR          = Path(os.environ.get("DATA_DIR",   "/tmp/xauusd_data"))
CACHE_DIR         = Path(os.environ.get("CACHE_DIR",  "cache"))
RESULT_DIR        = Path("backtest_results")
CACHE_FILE        = CACHE_DIR / "candles_1m.csv"

PIP      = 0.1          # XAUUSD: 1 pip = 0.1 price units
SL_RANGE = [3,4,5,6,7,8]
RR_RANGE = [3,4,5,6,7,8]
MAX_BARS = 300          # max candles to hold open trade

# ══════════════════════════════════════════════════════
# ZSTD  (libzstd already installed on ubuntu-latest)
# ══════════════════════════════════════════════════════
_zlib = None

def _load_zstd():
    global _zlib
    if _zlib is not None:
        return _zlib
    name = ctypes.util.find_library("zstd")
    if not name:
        raise RuntimeError("libzstd not found — run: sudo apt-get install libzstd-dev")
    lib = ctypes.CDLL(name)
    lib.ZSTD_decompress.restype           = ctypes.c_size_t
    lib.ZSTD_decompress.argtypes          = [ctypes.c_void_p, ctypes.c_size_t,
                                              ctypes.c_void_p, ctypes.c_size_t]
    lib.ZSTD_getFrameContentSize.restype  = ctypes.c_uint64
    lib.ZSTD_getFrameContentSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    lib.ZSTD_isError.restype              = ctypes.c_uint
    lib.ZSTD_isError.argtypes             = [ctypes.c_size_t]
    _zlib = lib
    return lib

def decompress_zst(data: bytes) -> bytes:
    lib  = _load_zstd()
    src  = ctypes.create_string_buffer(data)
    size = lib.ZSTD_getFrameContentSize(src, len(data))
    UNKNOWN = (1<<64)-1;  ERROR = (1<<64)-2
    buf  = len(data)*25 if size in (UNKNOWN, ERROR) else int(size)
    dst  = ctypes.create_string_buffer(buf)
    n    = lib.ZSTD_decompress(dst, buf, src, len(data))
    if lib.ZSTD_isError(n):
        raise RuntimeError(f"zstd error {n}")
    return dst.raw[:n]

# ══════════════════════════════════════════════════════
# GOOGLE DRIVE DOWNLOAD
# ══════════════════════════════════════════════════════
import urllib.request

def _folder_id(url: str) -> str:
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", url)
    if not m:
        raise ValueError(f"Cannot parse folder ID from URL: {url}")
    return m.group(1)

def _list_folder(folder_id: str):
    """Returns [(filename, file_id), ...] from public Google Drive folder."""
    url  = f"https://drive.google.com/drive/folders/{folder_id}"
    req  = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", "replace")

    # Pattern 1: ["filename","","file_id",...]
    files = re.findall(
        r'\["([\w. -]+?\.(?:csv\.zst|zst|csv))",(?:null|"[^"]*"),"([a-zA-Z0-9_-]{25,})"',
        html)
    if not files:
        # Pattern 2: file_id before name
        raw = re.findall(
            r'"([a-zA-Z0-9_-]{25,})","([\w. -]+?\.(?:csv\.zst|zst|csv))"',
            html)
        files = [(name, fid) for fid, name in raw]
    return files

def _download_gdrive(file_id: str, dest: Path):
    """Download one file from Google Drive (handles large-file confirmation)."""
    base = f"https://drive.google.com/uc?export=download&id={file_id}"
    headers = {"User-Agent": "Mozilla/5.0"}

    req = urllib.request.Request(base + "&confirm=t", headers=headers)
    with urllib.request.urlopen(req, timeout=600) as r:
        ct = r.headers.get("Content-Type", "")
        if "text/html" in ct:
            html = r.read().decode("utf-8", "replace")
            m = re.search(r'confirm=([^&"\']+)', html)
            confirm = m.group(1) if m else "t"
            url2 = base + f"&confirm={confirm}"
            req2 = urllib.request.Request(url2, headers=headers)
            with urllib.request.urlopen(req2, timeout=600) as r2:
                data = r2.read()
        else:
            data = r.read()

    dest.write_bytes(data)

def download_all_from_drive(folder_url: str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fid   = _folder_id(folder_url)
    print(f"📂 Google Drive folder: {fid}")
    files = _list_folder(fid)

    if not files:
        print("⚠️  Auto-detect failed. Checking DATA_DIR for existing files...")
        return

    print(f"   {len(files)} file(s) found")
    for name, file_id in files:
        dest = DATA_DIR / name
        if dest.exists():
            print(f"  ✓ {name}  (cached  {dest.stat().st_size/1e6:.0f} MB)")
            continue
        print(f"  ↓ {name} ...", end=" ", flush=True)
        _download_gdrive(file_id, dest)
        print(f"✅  {dest.stat().st_size/1e6:.1f} MB")

# ══════════════════════════════════════════════════════
# TICK → 1-MINUTE CANDLE
# ══════════════════════════════════════════════════════
_TS_FMTS = [
    "%Y.%m.%d %H:%M:%S.%f", "%Y.%m.%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
]

def _parse_dt(s: str) -> datetime:
    for f in _TS_FMTS:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            pass
    raise ValueError(f"Unknown datetime format: {s}")

def ticks_to_candles_from_bytes(raw: bytes) -> dict:
    """Parse tick CSV bytes → dict{datetime: [O,H,L,C]}"""
    text    = raw.decode("utf-8", "replace")
    candles = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2:
            continue
        try:
            bid = float(row[1])
        except ValueError:
            continue   # header
        try:
            dt  = _parse_dt(row[0].strip())
        except ValueError:
            continue
        key = dt.replace(second=0, microsecond=0)
        if key not in candles:
            candles[key] = [bid, bid, bid, bid]
        else:
            c = candles[key]
            if bid > c[1]: c[1] = bid
            if bid < c[2]: c[2] = bid
            c[3] = bid
    return candles

# ══════════════════════════════════════════════════════
# BUILD CACHE  (mode: build)
# ══════════════════════════════════════════════════════
def build_cache():
    print("=" * 55)
    print(" MODE: BUILD CANDLE CACHE")
    print("=" * 55)

    # Download from Drive if needed
    if GDRIVE_FOLDER_URL:
        download_all_from_drive(GDRIVE_FOLDER_URL)

    files = sorted(DATA_DIR.glob("*.csv.zst")) + sorted(DATA_DIR.glob("*.csv"))
    if not files:
        sys.exit(f"❌ No data files in {DATA_DIR}")

    print(f"\n📊 Processing {len(files)} file(s)...")
    all_candles = {}

    for fp in files:
        print(f"  📄 {fp.name} ...", end=" ", flush=True)
        raw = fp.read_bytes()
        if fp.suffix == ".zst":
            raw = decompress_zst(raw)
        c = ticks_to_candles_from_bytes(raw)
        print(f"{len(c):,} candles")
        all_candles.update(c)

    # Sort & save
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sorted_keys = sorted(all_candles.keys())
    print(f"\n💾 Saving cache → {CACHE_FILE}")
    print(f"   Total candles : {len(sorted_keys):,}")
    print(f"   Date range    : {sorted_keys[0]} → {sorted_keys[-1]}")

    with open(CACHE_FILE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close"])
        for k in sorted_keys:
            o, h, l, c = all_candles[k]
            w.writerow([k.strftime("%Y-%m-%d %H:%M:%S"), o, h, l, c])

    print("✅ Cache built successfully!")

# ══════════════════════════════════════════════════════
# LOAD CACHE  (mode: run)
# ══════════════════════════════════════════════════════
def load_cache() -> list:
    if not CACHE_FILE.exists():
        sys.exit(f"❌ Cache not found: {CACHE_FILE}\n   Run first: python backtest.py build")

    print(f"📂 Loading cache: {CACHE_FILE}")
    candles = []
    with open(CACHE_FILE, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            candles.append({
                "t": datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S"),
                "o": float(row["open"]),
                "h": float(row["high"]),
                "l": float(row["low"]),
                "c": float(row["close"]),
            })
    print(f"✅ {len(candles):,} candles loaded")
    return candles

# ══════════════════════════════════════════════════════
# STRATEGY: Bearish Engulfing
# ══════════════════════════════════════════════════════
def is_signal(prev: dict, curr: dict) -> bool:
    return (
        prev["c"] > prev["o"]    and   # prev = Green
        curr["c"] < curr["o"]    and   # curr = Red
        curr["o"] >= prev["o"]   and   # engulfs body top
        curr["c"] <= prev["c"]         # engulfs body bottom
    )

# ══════════════════════════════════════════════════════
# BACKTEST  (single SL / RR)
# ══════════════════════════════════════════════════════
def backtest(candles: list, sl_pips: int, rr: int) -> dict:
    sl_pts = sl_pips * PIP
    tp_pts = sl_pts  * rr
    trades = []
    n = len(candles)
    i = 1

    while i < n - 1:
        if is_signal(candles[i-1], candles[i]):
            entry   = candles[i+1]["o"]   # next candle open
            sl      = entry + sl_pts       # Sell: SL above
            tp      = entry - tp_pts       # Sell: TP below
            outcome = "timeout"
            exit_p  = candles[min(i+1+MAX_BARS-1, n-1)]["c"]
            exit_i  = min(i+1+MAX_BARS, n)

            for j in range(i+1, min(i+1+MAX_BARS, n)):
                if candles[j]["h"] >= sl:
                    outcome = "loss"; exit_p = sl; exit_i = j; break
                if candles[j]["l"] <= tp:
                    outcome = "win";  exit_p = tp; exit_i = j; break

            pnl = (entry - exit_p) / PIP
            trades.append({"outcome": outcome, "pnl": round(pnl, 2)})
            i = exit_i
        else:
            i += 1

    wins      = [t for t in trades if t["outcome"] == "win"]
    losses    = [t for t in trades if t["outcome"] == "loss"]
    total     = len(wins) + len(losses)
    wr        = len(wins) / total * 100 if total else 0
    gross_w   = sum(t["pnl"] for t in wins)
    gross_l   = abs(sum(t["pnl"] for t in losses))
    pf        = gross_w / gross_l if gross_l else 0
    total_pnl = gross_w - gross_l

    # max drawdown
    eq = pk = mdd = 0
    for t in trades:
        if t["outcome"] == "timeout":
            continue
        eq += t["pnl"]
        if eq > pk: pk = eq
        mdd = max(mdd, pk - eq)

    return dict(
        sl=sl_pips, rr=rr,
        trades=total, wins=len(wins), losses=len(losses),
        wr=round(wr, 2),
        pnl=round(total_pnl, 2),
        pf=round(pf, 3),
        mdd=round(mdd, 2),
        avg=round(total_pnl / total, 2) if total else 0,
    )

# ══════════════════════════════════════════════════════
# GRID SEARCH  36 combos
# ══════════════════════════════════════════════════════
def grid_search(candles: list) -> list:
    combos  = list(itertools.product(SL_RANGE, RR_RANGE))
    results = []
    print(f"\n🔍 Grid search: {len(combos)} combinations...\n")

    for idx, (sl, rr) in enumerate(combos, 1):
        r = backtest(candles, sl, rr)
        results.append(r)
        print(f"  [{idx:02}/{len(combos)}]  SL={sl}  RR=1:{rr}  "
              f"Trades={r['trades']:4}  WR={r['wr']:5.1f}%  "
              f"PnL={r['pnl']:8.1f} pips  PF={r['pf']:.3f}")
    return results

# ══════════════════════════════════════════════════════
# SAVE RESULTS
# ══════════════════════════════════════════════════════
def save_results(results: list, candles: list):
    RESULT_DIR.mkdir(exist_ok=True)
    tag  = datetime.utcnow().strftime("%Y%m%d_%H%M")
    d0   = candles[0]["t"].strftime("%Y-%m-%d")
    d1   = candles[-1]["t"].strftime("%Y-%m-%d")
    best = max(results, key=lambda x: x["pf"])

    # ── CSV ──────────────────────────────────────────
    csv_path = RESULT_DIR / f"results_{tag}.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=results[0].keys())
        w.writeheader()
        w.writerows(results)
    print(f"\n💾 CSV  → {csv_path}")

    # ── Markdown ─────────────────────────────────────
    md_path = RESULT_DIR / f"results_{tag}.md"
    lines = [
        "# XAUUSD Bearish-Engulfing Sell — Backtest Report",
        f"**Period:** {d0} → {d1}  |  **TF:** 1m  |  "
        f"**Run:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## Strategy Rules",
        "| Rule | Detail |",
        "|------|--------|",
        "| Signal | Bearish Engulfing: prev candle Green, curr candle Red, Red body fully covers Green body |",
        "| Entry  | Sell at next 1m candle open |",
        "| SL     | X pips above entry |",
        "| TP     | RR × SL below entry |",
        "",
        "## 🏆 Best Configuration",
        f"**SL = {best['sl']} pips  |  RR = 1:{best['rr']}**",
        f"- Win Rate     : {best['wr']}%",
        f"- Total PnL    : {best['pnl']} pips",
        f"- Profit Factor: {best['pf']}",
        f"- Total Trades : {best['trades']}",
        f"- Max Drawdown : {best['mdd']} pips",
        "",
        "## Full Grid (sorted by Profit Factor)",
        "| SL | RR | Trades | WR% | PnL (pips) | PF | Max DD |",
        "|----|-----|--------|-----|------------|----|--------|",
    ]
    for r in sorted(results, key=lambda x: x["pf"], reverse=True):
        flag = " 🏆" if r is best else ""
        lines.append(
            f"| {r['sl']} | 1:{r['rr']} | {r['trades']} | {r['wr']} | "
            f"{r['pnl']} | {r['pf']} | {r['mdd']}{flag} |"
        )
    md_path.write_text("\n".join(lines))
    print(f"💾 MD   → {md_path}")

    # ── Console summary ───────────────────────────────
    print("\n" + "="*55)
    print(f"🏆  BEST:  SL={best['sl']} pips  RR=1:{best['rr']}")
    print(f"    WinRate        = {best['wr']}%")
    print(f"    Total PnL      = {best['pnl']} pips")
    print(f"    Profit Factor  = {best['pf']}")
    print(f"    Total Trades   = {best['trades']}")
    print(f"    Max Drawdown   = {best['mdd']} pips")
    print("="*55)

# ══════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════
def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"

    if mode == "build":
        build_cache()

    elif mode == "run":
        print("=" * 55)
        print(" MODE: RUN STRATEGY BACKTEST")
        print("=" * 55)
        candles = load_cache()

        sigs = sum(1 for i in range(1, len(candles))
                   if is_signal(candles[i-1], candles[i]))
        print(f"🔔 Signals detected: {sigs:,}")

        results = grid_search(candles)
        save_results(results, candles)

    else:
        print("Usage:")
        print("  python backtest.py build   ← tick data → candle cache")
        print("  python backtest.py run     ← cache → backtest results")

if __name__ == "__main__":
    main()
