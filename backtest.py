import os,sys,csv,io,ctypes,ctypes.util,itertools,subprocess,re
from datetime import datetime
from pathlib import Path

GDRIVE_FOLDER_URL=os.environ.get("GDRIVE_FOLDER_URL","")
DATA_DIR=Path(os.environ.get("DATA_DIR","/tmp/xauusd_data"))
CACHE_DIR=Path(os.environ.get("CACHE_DIR","cache"))
RESULT_DIR=Path("backtest_results")
CACHE_FILE=CACHE_DIR/"candles_1m.csv"
PIP=0.1
SL_RANGE=[3,4,5,6,7,8]
RR_RANGE=[3,4,5,6,7,8]
MAX_BARS=300

def decompress_zst(data:bytes)->bytes:
    lib=ctypes.CDLL(ctypes.util.find_library("zstd"))
    class IB(ctypes.Structure):
        _fields_=[("src",ctypes.c_void_p),("size",ctypes.c_size_t),("pos",ctypes.c_size_t)]
    class OB(ctypes.Structure):
        _fields_=[("dst",ctypes.c_void_p),("size",ctypes.c_size_t),("pos",ctypes.c_size_t)]
    lib.ZSTD_createDStream.restype=ctypes.c_void_p
    lib.ZSTD_createDStream.argtypes=[]
    lib.ZSTD_freeDStream.restype=ctypes.c_size_t
    lib.ZSTD_freeDStream.argtypes=[ctypes.c_void_p]
    lib.ZSTD_initDStream.restype=ctypes.c_size_t
    lib.ZSTD_initDStream.argtypes=[ctypes.c_void_p]
    lib.ZSTD_isError.restype=ctypes.c_uint
    lib.ZSTD_isError.argtypes=[ctypes.c_size_t]
    lib.ZSTD_DStreamOutSize.restype=ctypes.c_size_t
    lib.ZSTD_decompressStream.restype=ctypes.c_size_t
    lib.ZSTD_decompressStream.argtypes=[ctypes.c_void_p,ctypes.POINTER(OB),ctypes.POINTER(IB)]
    ds=lib.ZSTD_createDStream()
    lib.ZSTD_initDStream(ds)
    chunk=lib.ZSTD_DStreamOutSize()
    obuf=ctypes.create_string_buffer(chunk)
    src=ctypes.create_string_buffer(data)
    ib=IB(ctypes.cast(src,ctypes.c_void_p),len(data),0)
    out=io.BytesIO()
    while ib.pos<ib.size:
        ob=OB(ctypes.cast(obuf,ctypes.c_void_p),chunk,0)
        r=lib.ZSTD_decompressStream(ds,ctypes.byref(ob),ctypes.byref(ib))
        if lib.ZSTD_isError(r):raise RuntimeError(f"zstd err {r}")
        out.write(obuf.raw[:ob.pos])
    lib.ZSTD_freeDStream(ds)
    return out.getvalue()

def install_gdown():
    subprocess.run([sys.executable,"-m","pip","install","gdown","-q"],check=True)

def download_folder(url:str):
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    try:
        import gdown
    except ImportError:
        install_gdown()
        import gdown
    print(f"Downloading from Google Drive...")
    gdown.download_folder(url=url,output=str(DATA_DIR),quiet=False,use_cookies=False)
    print(f"Downloaded {len(list(DATA_DIR.glob('*')))} files")

_FMTS=["%Y.%m.%d %H:%M:%S.%f","%Y.%m.%d %H:%M:%S",
       "%Y-%m-%d %H:%M:%S.%f","%Y-%m-%d %H:%M:%S"]

def _dt(s):
    for f in _FMTS:
        try:return datetime.strptime(s,f)
        except:pass
    raise ValueError(s)

def ticks_to_candles(raw:bytes)->dict:
    candles={}
    for row in csv.reader(io.StringIO(raw.decode("utf-8","replace"))):
        if len(row)<2:continue
        try:bid=float(row[1])
        except:continue
        try:dt=_dt(row[0].strip())
        except:continue
        k=dt.replace(second=0,microsecond=0)
        if k not in candles:candles[k]=[bid,bid,bid,bid]
        else:
            c=candles[k]
            if bid>c[1]:c[1]=bid
            if bid<c[2]:c[2]=bid
            c[3]=bid
    return candles

def build_cache():
    print("="*50)
    print("BUILD CANDLE CACHE")
    print("="*50)
    if GDRIVE_FOLDER_URL:
        download_folder(GDRIVE_FOLDER_URL)
    files=(sorted(DATA_DIR.glob("*.csv.zst"))+
           sorted(DATA_DIR.glob("*.zst"))+
           sorted(DATA_DIR.glob("*.csv")))
    if not files:sys.exit(f"No data files in {DATA_DIR}")
    print(f"\nProcessing {len(files)} files...")
    all_candles={}
    for fp in files:
        print(f"  {fp.name}...",end=" ",flush=True)
        raw=fp.read_bytes()
        if fp.suffix==".zst":raw=decompress_zst(raw)
        c=ticks_to_candles(raw)
        print(f"{len(c):,} candles")
        all_candles.update(c)
    keys=sorted(all_candles.keys())
    CACHE_DIR.mkdir(parents=True,exist_ok=True)
    print(f"\nSaving {len(keys):,} candles to {CACHE_FILE}")
    print(f"Range: {keys[0]} to {keys[-1]}")
    with open(CACHE_FILE,"w",newline="") as f:
        w=csv.writer(f)
        w.writerow(["time","open","high","low","close"])
        for k in keys:
            o,h,l,c=all_candles[k]
            w.writerow([k.strftime("%Y-%m-%d %H:%M:%S"),o,h,l,c])
    print("Cache built successfully!")

def load_cache()->list:
    if not CACHE_FILE.exists():
        sys.exit(f"No cache found. Run: python backtest.py build")
    print(f"Loading {CACHE_FILE}...")
    out=[]
    with open(CACHE_FILE,newline="") as f:
        for row in csv.DictReader(f):
            out.append({"t":datetime.strptime(row["time"],"%Y-%m-%d %H:%M:%S"),
                        "o":float(row["open"]),"h":float(row["high"]),
                        "l":float(row["low"]),"c":float(row["close"])})
    print(f"Loaded {len(out):,} candles")
    return out

def is_signal(p,c)->bool:
    return(p["c"]>p["o"] and c["c"]<c["o"] and
           c["o"]>=p["o"] and c["c"]<=p["c"])

def backtest(candles,sl_pips,rr)->dict:
    sl_pts=sl_pips*PIP;tp_pts=sl_pts*rr
    trades=[];n=len(candles);i=1
    while i<n-1:
        if is_signal(candles[i-1],candles[i]):
            entry=candles[i+1]["o"]
            sl=entry+sl_pts;tp=entry-tp_pts
            outcome="timeout"
            exit_p=candles[min(i+1+MAX_BARS-1,n-1)]["c"]
            exit_i=min(i+1+MAX_BARS,n)
            for j in range(i+1,min(i+1+MAX_BARS,n)):
                if candles[j]["h"]>=sl:outcome="loss";exit_p=sl;exit_i=j;break
                if candles[j]["l"]<=tp:outcome="win";exit_p=tp;exit_i=j;break
            trades.append({"outcome":outcome,"pnl":round((entry-exit_p)/PIP,2)})
            i=exit_i
        else:i+=1
    wins=[t for t in trades if t["outcome"]=="win"]
    loss=[t for t in trades if t["outcome"]=="loss"]
    tot=len(wins)+len(loss)
    gw=sum(t["pnl"] for t in wins)
    gl=abs(sum(t["pnl"] for t in loss))
    pnl=gw-gl
    eq=pk=mdd=0
    for t in trades:
        if t["outcome"]=="timeout":continue
        eq+=t["pnl"]
        if eq>pk:pk=eq
        mdd=max(mdd,pk-eq)
    return dict(sl=sl_pips,rr=rr,trades=tot,wins=len(wins),losses=len(loss),
                wr=round(len(wins)/tot*100 if tot else 0,2),
                pnl=round(pnl,2),pf=round(gw/gl if gl else 0,3),
                mdd=round(mdd,2),avg=round(pnl/tot if tot else 0,2))

def grid_search(candles):
    combos=list(itertools.product(SL_RANGE,RR_RANGE))
    results=[]
    print(f"\nGrid search: {len(combos)} combinations...\n")
    for i,(sl,rr) in enumerate(combos,1):
        r=backtest(candles,sl,rr)
        results.append(r)
        print(f"  [{i:02}/{len(combos)}] SL={sl} RR=1:{rr} "
              f"Trades={r['trades']:4} WR={r['wr']:5.1f}% "
              f"PnL={r['pnl']:8.1f} PF={r['pf']:.3f}")
    return results

def save(results,candles):
    RESULT_DIR.mkdir(exist_ok=True)
    tag=datetime.utcnow().strftime("%Y%m%d_%H%M")
    d0=candles[0]["t"].strftime("%Y-%m-%d")
    d1=candles[-1]["t"].strftime("%Y-%m-%d")
    best=max(results,key=lambda x:x["pf"])
    cp=RESULT_DIR/f"results_{tag}.csv"
    with open(cp,"w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=results[0].keys())
        w.writeheader();w.writerows(results)
    mp=RESULT_DIR/f"results_{tag}.md"
    lines=[f"# XAUUSD Bearish Engulfing Backtest",
           f"Period: {d0} to {d1} | TF: 1m | Run: {tag}","",
           f"## BEST: SL={best['sl']} pips | RR=1:{best['rr']}",
           f"WR={best['wr']}% | PnL={best['pnl']} pips | PF={best['pf']} | Trades={best['trades']}","",
           "## Full Results",
           "| SL | RR | Trades | WR% | PnL | PF | MaxDD |",
           "|----|-----|--------|-----|-----|-----|-------|"]
    for r in sorted(results,key=lambda x:x["pf"],reverse=True):
        flag="BEST" if r is best else ""
        lines.append(f"| {r['sl']} | 1:{r['rr']} | {r['trades']} | "
                     f"{r['wr']} | {r['pnl']} | {r['pf']} | {r['mdd']} {flag}|")
    mp.write_text("\n".join(lines))
    print(f"\nCSV: {cp}")
    print(f"MD:  {mp}")
    print(f"\n{'='*50}")
    print(f"BEST: SL={best['sl']} pips  RR=1:{best['rr']}")
    print(f"WR={best['wr']}%  PnL={best['pnl']} pips  PF={best['pf']}")
    print(f"Trades={best['trades']}  MaxDD={best['mdd']} pips")
    print(f"{'='*50}")

def main():
    mode=sys.argv[1] if len(sys.argv)>1 else "run"
    if mode=="build":
        build_cache()
    elif mode=="run":
        print("="*50)
        print("RUN STRATEGY BACKTEST")
        print("="*50)
        candles=load_cache()
        sigs=sum(1 for i in range(1,len(candles)) if is_signal(candles[i-1],candles[i]))
        print(f"Signals detected: {sigs:,}")
        save(grid_search(candles),candles)
    else:
        print("Usage:\n  python backtest.py build\n  python backtest.py run")

if __name__=="__main__":
    main()
