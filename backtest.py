import os, sys, csv, io, itertools, subprocess, zipfile
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from xml.sax.saxutils import escape

GDRIVE_FOLDER_URL = os.environ.get("GDRIVE_FOLDER_URL", "")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/tmp/xauusd_data"))
CACHE_DIR = Path(os.environ.get("CACHE_DIR", "cache"))
RESULT_DIR = Path("backtest_results")
CACHE_FILE = CACHE_DIR / "candles_1m.csv"
PIP = 0.1
INITIAL_CAPITAL_INR = 100000.0
USDINR = 83.0
PIP_VALUE_USD_001_LOT = 0.10
LOT_SIZE = 0.01
SL_RANGE = [3, 4, 5, 6, 7, 8]
RR_RANGE = [3, 4, 5, 6, 7, 8]
MAX_BARS = 300

def decompress_zst(zst_path):
    tmp = Path(str(zst_path) + ".out.csv")
    r = subprocess.run(["zstd", "-d", str(zst_path), "-o", str(tmp), "-f", "--no-progress"], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"zstd error: {r.stderr.strip()}")
    data = tmp.read_bytes()
    try: tmp.unlink()
    except: pass
    return data

def install_gdown():
    subprocess.run([sys.executable, "-m", "pip", "install", "gdown", "-q"], check=True)

def download_folder(url):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try: import gdown
    except ImportError:
        install_gdown()
        import gdown
    print("Downloading Google Drive folder...")
    gdown.download_folder(url=url, output=str(DATA_DIR), quiet=False, use_cookies=False)
    print(f"Downloaded files: {len(list(DATA_DIR.glob('*')))}")

_DT_FORMATS = ["%Y.%m.%d %H:%M:%S.%f", "%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"]

def parse_dt(s):
    for fmt in _DT_FORMATS:
        try: return datetime.strptime(s, fmt)
        except: pass
    raise ValueError(s)

def ticks_to_candles(raw):
    candles = {}
    for row in csv.reader(io.StringIO(raw.decode("utf-8", errors="replace"))):
        if len(row) < 2: continue
        try: bid = float(row[1])
        except: continue
        try: dt = parse_dt(row[0].strip())
        except: continue
        k = dt.replace(second=0, microsecond=0)
        if k not in candles: candles[k] = [bid, bid, bid, bid]
        else:
            c = candles[k]
            if bid > c[1]: c[1] = bid
            if bid < c[2]: c[2] = bid
            c[3] = bid
    return candles

def build_cache():
    print("="*70)
    print("BUILD CANDLE CACHE")
    print("="*70)
    if GDRIVE_FOLDER_URL:
        download_folder(GDRIVE_FOLDER_URL)
    files = []
    for pattern in ["*.csv.zst", "*.zst", "*.csv"]:
        for p in sorted(DATA_DIR.glob(pattern)):
            if p not in files: files.append(p)
    if not files: sys.exit(f"No data files found in {DATA_DIR}")
    print(f"\nProcessing files: {len(files)}")
    all_candles = {}
    skipped = []
    for fp in files:
        print(f"  {fp.name} ...", end=" ", flush=True)
        try:
            if fp.suffix == ".zst": raw = decompress_zst(fp)
            else: raw = fp.read_bytes()
            c = ticks_to_candles(raw)
            all_candles.update(c)
            print(f"{len(c):,} candles")
        except Exception as e:
            print(f"SKIPPED: {e}")
            skipped.append((fp.name, str(e)))
    if not all_candles: sys.exit("No candles created.")
    keys = sorted(all_candles.keys())
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\nSaving cache: {CACHE_FILE}")
    print(f"Total candles: {len(keys):,}")
    print(f"Date range: {keys[0]} to {keys[-1]}")
    with open(CACHE_FILE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close"])
        for k in keys:
            o, h, l, c = all_candles[k]
            w.writerow([k.strftime("%Y-%m-%d %H:%M:%S"), o, h, l, c])
    if skipped:
        print("\nWARNING: Some files skipped:")
        for name, err in skipped: print(f"  - {name}: {err}")
    print("\nCACHE BUILT SUCCESSFULLY")

def load_cache():
    if not CACHE_FILE.exists(): sys.exit("Cache not found. First run Build Candle Cache workflow.")
    print(f"Loading candle cache: {CACHE_FILE}")
    candles = []
    with open(CACHE_FILE, newline="") as f:
        for row in csv.DictReader(f):
            candles.append({"t": datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S"), "o": float(row["open"]), "h": float(row["high"]), "l": float(row["low"]), "c": float(row["close"])})
    print(f"Loaded candles: {len(candles):,}")
    print(f"Range: {candles[0]['t']} to {candles[-1]['t']}")
    return candles

def is_signal(prev_candle, curr_candle):
    return (prev_candle["c"] > prev_candle["o"] and curr_candle["c"] < curr_candle["o"] and curr_candle["o"] >= prev_candle["o"] and curr_candle["c"] <= prev_candle["c"])

def money_from_pips(pips):
    usd = pips * PIP_VALUE_USD_001_LOT
    inr = usd * USDINR
    return usd, inr

def run_backtest(candles, sl_pips, rr, keep_trades=False):
    sl_points = sl_pips * PIP
    tp_points = sl_points * rr
    n = len(candles)
    i = 1
    wins = losses = timeouts = 0
    gross_win_pips = gross_loss_pips = net_pips = 0.0
    equity_pips = peak_pips = max_dd_pips = 0.0
    trades = []
    while i < n - 1:
        if is_signal(candles[i-1], candles[i]):
            entry_index = i + 1
            entry_candle = candles[entry_index]
            entry = entry_candle["o"]
            sl = entry + sl_points
            tp = entry - tp_points
            outcome = "timeout"
            exit_price = candles[min(entry_index + MAX_BARS - 1, n-1)]["c"]
            exit_index = min(entry_index + MAX_BARS - 1, n-1)
            for j in range(entry_index, min(entry_index + MAX_BARS, n)):
                c = candles[j]
                if c["h"] >= sl: outcome="loss"; exit_price=sl; exit_index=j; break
                if c["l"] <= tp: outcome="win";  exit_price=tp; exit_index=j; break
            if outcome == "win":
                pnl_pips = round((entry - exit_price) / PIP, 2)
                wins += 1; gross_win_pips += pnl_pips; net_pips += pnl_pips
                equity_pips += pnl_pips
                if equity_pips > peak_pips: peak_pips = equity_pips
                max_dd_pips = max(max_dd_pips, peak_pips - equity_pips)
            elif outcome == "loss":
                pnl_pips = round((entry - exit_price) / PIP, 2)
                losses += 1; gross_loss_pips += abs(pnl_pips); net_pips += pnl_pips
                equity_pips += pnl_pips
                if equity_pips > peak_pips: peak_pips = equity_pips
                max_dd_pips = max(max_dd_pips, peak_pips - equity_pips)
            else:
                pnl_pips = 0.0; timeouts += 1
            if keep_trades:
                usd, inr = money_from_pips(pnl_pips)
                trades.append({"entry_time": entry_candle["t"], "exit_time": candles[exit_index]["t"], "side": "SELL", "entry": round(entry,3), "sl": round(sl,3), "tp": round(tp,3), "exit": round(exit_price,3), "outcome": outcome, "pnl_pips": pnl_pips, "profit_usd": round(usd,2), "profit_inr": round(inr,2), "bars_held": exit_index - entry_index + 1})
            i = max(exit_index, i+1)
        else: i += 1
    total_closed = wins + losses
    win_rate = (wins / total_closed * 100) if total_closed else 0.0
    profit_factor = (gross_win_pips / gross_loss_pips) if gross_loss_pips else 0.0
    profit_usd, profit_inr = money_from_pips(net_pips)
    dd_usd, dd_inr = money_from_pips(max_dd_pips)
    final_capital = INITIAL_CAPITAL_INR + profit_inr
    roi = (profit_inr / INITIAL_CAPITAL_INR * 100) if INITIAL_CAPITAL_INR else 0.0
    stats = {"sl": sl_pips, "rr": rr, "trades": total_closed, "wins": wins, "losses": losses, "timeouts": timeouts, "wr": round(win_rate,2), "pnl_pips": round(net_pips,2), "profit_usd": round(profit_usd,2), "profit_inr": round(profit_inr,2), "final_capital_inr": round(final_capital,2), "roi_percent": round(roi,2), "pf": round(profit_factor,3), "max_dd_pips": round(max_dd_pips,2), "max_dd_inr": round(dd_inr,2), "avg_pips": round(net_pips/total_closed,2) if total_closed else 0.0}
    return stats, trades

def grid_search(candles):
    combos = list(itertools.product(SL_RANGE, RR_RANGE))
    results = []
    print("\n"+"="*70)
    print(f"GRID SEARCH: {len(combos)} COMBINATIONS")
    print("="*70)
    for idx, (sl, rr) in enumerate(combos, 1):
        stats, _ = run_backtest(candles, sl, rr, keep_trades=False)
        results.append(stats)
        print(f"[{idx:02}/{len(combos)}] SL={sl} RR=1:{rr} | Trades={stats['trades']:,} | WR={stats['wr']}% | PnL={stats['pnl_pips']} pips | Profit Rs{stats['profit_inr']:,} | ROI={stats['roi_percent']}% | PF={stats['pf']}")
    return results

def save_csv(path, rows, headers):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        for r in rows: w.writerow({h: r.get(h, "") for h in headers})

def analyze_monthly(trades):
    d = defaultdict(lambda: {"trades":0,"wins":0,"losses":0,"timeouts":0,"pnl_pips":0.0,"profit_usd":0.0,"profit_inr":0.0})
    for t in trades:
        key = t["entry_time"].strftime("%Y-%m")
        row = d[key]; row["trades"] += 1
        if t["outcome"]=="win": row["wins"] += 1
        elif t["outcome"]=="loss": row["losses"] += 1
        else: row["timeouts"] += 1
        row["pnl_pips"] += t["pnl_pips"]; row["profit_usd"] += t["profit_usd"]; row["profit_inr"] += t["profit_inr"]
    out = []
    for key in sorted(d.keys()):
        r = d[key]; closed = r["wins"]+r["losses"]
        out.append({"month":key,"trades":r["trades"],"wins":r["wins"],"losses":r["losses"],"timeouts":r["timeouts"],"win_rate":round(r["wins"]/closed*100 if closed else 0,2),"pnl_pips":round(r["pnl_pips"],2),"profit_usd":round(r["profit_usd"],2),"profit_inr":round(r["profit_inr"],2),"roi_percent_on_1lakh":round(r["profit_inr"]/INITIAL_CAPITAL_INR*100,2)})
    return out

def analyze_yearly(trades):
    d = defaultdict(lambda: {"trades":0,"wins":0,"losses":0,"timeouts":0,"pnl_pips":0.0,"profit_usd":0.0,"profit_inr":0.0})
    for t in trades:
        key = t["entry_time"].strftime("%Y"); row = d[key]; row["trades"] += 1
        if t["outcome"]=="win": row["wins"] += 1
        elif t["outcome"]=="loss": row["losses"] += 1
        else: row["timeouts"] += 1
        row["pnl_pips"] += t["pnl_pips"]; row["profit_usd"] += t["profit_usd"]; row["profit_inr"] += t["profit_inr"]
    out = []
    for key in sorted(d.keys()):
        r = d[key]; closed = r["wins"]+r["losses"]
        out.append({"year":key,"trades":r["trades"],"wins":r["wins"],"losses":r["losses"],"timeouts":r["timeouts"],"win_rate":round(r["wins"]/closed*100 if closed else 0,2),"pnl_pips":round(r["pnl_pips"],2),"profit_usd":round(r["profit_usd"],2),"profit_inr":round(r["profit_inr"],2),"roi_percent_on_1lakh":round(r["profit_inr"]/INITIAL_CAPITAL_INR*100,2)})
    return out

def analyze_streaks(trades):
    max_loss=max_win=cur_loss=cur_win=0
    worst_pips=temp_pips=0.0
    for t in trades:
        if t["outcome"]=="loss":
            cur_loss+=1; cur_win=0; temp_pips+=t["pnl_pips"]
            if cur_loss>max_loss: max_loss=cur_loss; worst_pips=temp_pips
        elif t["outcome"]=="win":
            cur_win+=1; cur_loss=0; temp_pips=0.0
            if cur_win>max_win: max_win=cur_win
        else: cur_loss=cur_win=0; temp_pips=0.0
    _,worst_inr=money_from_pips(worst_pips)
    return [{"metric":"max_consecutive_losses","value":max_loss,"pips":round(worst_pips,2),"inr":round(worst_inr,2)},{"metric":"max_consecutive_wins","value":max_win,"pips":"","inr":""}]

def trade_rows_with_equity(trades):
    rows=[]; equity=INITIAL_CAPITAL_INR
    for idx,t in enumerate(trades,1):
        equity+=t["profit_inr"]
        rows.append({"no":idx,"entry_time":t["entry_time"].strftime("%Y-%m-%d %H:%M:%S"),"exit_time":t["exit_time"].strftime("%Y-%m-%d %H:%M:%S"),"side":t["side"],"entry":t["entry"],"sl":t["sl"],"tp":t["tp"],"exit":t["exit"],"outcome":t["outcome"],"pnl_pips":t["pnl_pips"],"profit_usd":t["profit_usd"],"profit_inr":t["profit_inr"],"equity_inr":round(equity,2),"bars_held":t["bars_held"]})
    return rows

def col_name(n):
    name=""
    while n:
        n,rem=divmod(n-1,26); name=chr(65+rem)+name
    return name

def cell_xml(row_num,col_num,value):
    ref=f"{col_name(col_num)}{row_num}"
    if value is None: return f'<c r="{ref}"/>'
    if isinstance(value,(int,float)) and not isinstance(value,bool): return f'<c r="{ref}"><v>{value}</v></c>'
    text=escape(str(value))
    return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'

def sheet_xml(headers,rows):
    lines=['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>']
    lines.append('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">')
    lines.append("<sheetData>")
    lines.append('<row r="1">')
    for c,h in enumerate(headers,1): lines.append(cell_xml(1,c,h))
    lines.append("</row>")
    for r_idx,row in enumerate(rows,2):
        lines.append(f'<row r="{r_idx}">')
        for c_idx,h in enumerate(headers,1):
            v=row.get(h,"") if isinstance(row,dict) else (row[c_idx-1] if c_idx-1<len(row) else "")
            lines.append(cell_xml(r_idx,c_idx,v))
        lines.append("</row>")
    lines.append("</sheetData></worksheet>")
    return "\n".join(lines)

def write_xlsx(path,sheets):
    clean=[]
    for name,headers,rows in sheets:
        safe=name[:31].replace("/","_").replace("\\","_").replace("?","_").replace("*","_").replace("[","_").replace("]","_")
        clean.append((safe,headers,rows))
    with zipfile.ZipFile(path,"w",zipfile.ZIP_DEFLATED) as z:
        overrides="".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1,len(clean)+1))
        z.writestr("[Content_Types].xml",f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>{overrides}</Types>')
        z.writestr("_rels/.rels",'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        sheet_tags="".join(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>' for i,(name,_,_) in enumerate(clean,1))
        z.writestr("xl/workbook.xml",f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{sheet_tags}</sheets></workbook>')
        rels=["<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>","<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"]
        for i in range(1,len(clean)+1): rels.append(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>')
        rels.append("</Relationships>")
        z.writestr("xl/_rels/workbook.xml.rels","\n".join(rels))
        for i,(_,headers,rows) in enumerate(clean,1): z.writestr(f"xl/worksheets/sheet{i}.xml",sheet_xml(headers,rows))

def save_reports(results,candles,best_profit_stats,best_pf_stats,best_trades):
    RESULT_DIR.mkdir(parents=True,exist_ok=True)
    tag=datetime.utcnow().strftime("%Y%m%d_%H%M")
    d0=candles[0]["t"].strftime("%Y-%m-%d"); d1=candles[-1]["t"].strftime("%Y-%m-%d")
    master_headers=["sl","rr","trades","wins","losses","timeouts","wr","pnl_pips","profit_usd","profit_inr","final_capital_inr","roi_percent","pf","max_dd_pips","max_dd_inr","avg_pips","best_tag"]
    master_rows=[]
    for r in results:
        row=dict(r); tags=[]
        if r["sl"]==best_profit_stats["sl"] and r["rr"]==best_profit_stats["rr"]: tags.append("BEST_PROFIT")
        if r["sl"]==best_pf_stats["sl"] and r["rr"]==best_pf_stats["rr"]: tags.append("BEST_PF")
        row["best_tag"]=" + ".join(tags); master_rows.append(row)
    master_rows_sorted=sorted(master_rows,key=lambda x:x["profit_inr"],reverse=True)
    monthly_rows=analyze_monthly(best_trades); yearly_rows=analyze_yearly(best_trades)
    streak_rows=analyze_streaks(best_trades); trade_rows=trade_rows_with_equity(best_trades)
    master_csv=RESULT_DIR/f"master_compare_{tag}.csv"; trades_csv=RESULT_DIR/f"best_trades_{tag}.csv"
    monthly_csv=RESULT_DIR/f"monthly_summary_{tag}.csv"; yearly_csv=RESULT_DIR/f"yearly_summary_{tag}.csv"
    streak_csv=RESULT_DIR/f"streaks_{tag}.csv"
    save_csv(master_csv,master_rows_sorted,master_headers)
    trade_headers=["no","entry_time","exit_time","side","entry","sl","tp","exit","outcome","pnl_pips","profit_usd","profit_inr","equity_inr","bars_held"]
    save_csv(trades_csv,trade_rows,trade_headers)
    monthly_headers=["month","trades","wins","losses","timeouts","win_rate","pnl_pips","profit_usd","profit_inr","roi_percent_on_1lakh"]
    save_csv(monthly_csv,monthly_rows,monthly_headers)
    yearly_headers=["year","trades","wins","losses","timeouts","win_rate","pnl_pips","profit_usd","profit_inr","roi_percent_on_1lakh"]
    save_csv(yearly_csv,yearly_rows,yearly_headers)
    save_csv(streak_csv,streak_rows,["metric","value","pips","inr"])
    summary_rows=[{"item":"Period","value":f"{d0} to {d1}"},{"item":"Timeframe","value":"1 minute"},{"item":"Initial Capital INR","value":INITIAL_CAPITAL_INR},{"item":"Lot Size","value":LOT_SIZE},{"item":"USDINR","value":USDINR},{"item":"Best Profit SL","value":best_profit_stats["sl"]},{"item":"Best Profit RR","value":f"1:{best_profit_stats['rr']}"},{"item":"Best Profit INR","value":best_profit_stats["profit_inr"]},{"item":"Best Profit ROI%","value":best_profit_stats["roi_percent"]},{"item":"Best PF","value":best_pf_stats["pf"]},{"item":"Best PF SL","value":best_pf_stats["sl"]},{"item":"Best PF RR","value":f"1:{best_pf_stats['rr']}"}]
    xlsx_path=RESULT_DIR/f"full_report_{tag}.xlsx"
    write_xlsx(xlsx_path,[("SUMMARY",["item","value"],summary_rows),("MASTER_COMPARE",master_headers,master_rows_sorted),("YEARLY",yearly_headers,yearly_rows),("MONTHLY",monthly_headers,monthly_rows),("STREAKS",["metric","value","pips","inr"],streak_rows),("BEST_TRADES",trade_headers,trade_rows)])
    md_path=RESULT_DIR/f"results_{tag}.md"
    lines=["# XAUUSD Strategy Backtest Full Report","",f"**Period:** {d0} to {d1}",f"**Initial Capital:** Rs{INITIAL_CAPITAL_INR:,.0f}",f"**Lot Size:** {LOT_SIZE}","","## Best by Highest Profit",f"- SL: **{best_profit_stats['sl']} pips**",f"- RR: **1:{best_profit_stats['rr']}**",f"- Trades: **{best_profit_stats['trades']:,}**",f"- Win Rate: **{best_profit_stats['wr']}%**",f"- Profit: **{best_profit_stats['pnl_pips']:,} pips**",f"- Profit INR: **Rs{best_profit_stats['profit_inr']:,.2f}**",f"- Final Capital: **Rs{best_profit_stats['final_capital_inr']:,.2f}**",f"- ROI: **{best_profit_stats['roi_percent']}%**",f"- PF: **{best_profit_stats['pf']}**","","## Best by Profit Factor",f"- SL: **{best_pf_stats['sl']} pips**",f"- RR: **1:{best_pf_stats['rr']}**",f"- PF: **{best_pf_stats['pf']}**","","## Files",f"- Excel: full_report_{tag}.xlsx",f"- Trades: best_trades_{tag}.csv",f"- Monthly: monthly_summary_{tag}.csv",f"- Yearly: yearly_summary_{tag}.csv"]
    md_path.write_text("\n".join(lines),encoding="utf-8")
    print("\n"+"="*70)
    print("REPORT FILES CREATED")
    print("="*70)
    print(f"Excel:   {xlsx_path}")
    print(f"Trades:  {trades_csv} ({len(trade_rows):,} rows)")
    print(f"Monthly: {monthly_csv}")
    print(f"Yearly:  {yearly_csv}")
    print("\n"+"="*70)
    print("BEST RESULT BY PROFIT")
    print("="*70)
    print(f"SL={best_profit_stats['sl']} pips | RR=1:{best_profit_stats['rr']}")
    print(f"Profit: {best_profit_stats['pnl_pips']} pips")
    print(f"Profit INR: Rs{best_profit_stats['profit_inr']:,.2f}")
    print(f"Final Capital: Rs{best_profit_stats['final_capital_inr']:,.2f}")
    print(f"ROI: {best_profit_stats['roi_percent']}%")
    print(f"PF: {best_profit_stats['pf']}")
    print("="*70)

def run_mode():
    print("="*70)
    print("RUN STRATEGY BACKTEST — FULL DETAILS")
    print("="*70)
    candles=load_cache()
    signals=sum(1 for i in range(1,len(candles)) if is_signal(candles[i-1],candles[i]))
    print(f"Total signals: {signals:,}")
    results=grid_search(candles)
    best_profit=max(results,key=lambda x:(x["profit_inr"],x["pf"]))
    best_pf=max(results,key=lambda x:(x["pf"],x["profit_inr"]))
    print("\nRe-running best combo for full trade details...")
    best_profit_stats,best_trades=run_backtest(candles,best_profit["sl"],best_profit["rr"],keep_trades=True)
    save_reports(results,candles,best_profit_stats,best_pf,best_trades)

def main():
    mode=sys.argv[1] if len(sys.argv)>1 else "run"
    if mode=="build": build_cache()
    elif mode=="run": run_mode()
    else: print("Usage:\n  python backtest.py build\n  python backtest.py run")

if __name__=="__main__":
    main()
