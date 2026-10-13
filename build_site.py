"""data/daily/*.csv から静的サイト (_site/) を作る。  python build_site.py

  _site/index.html
  _site/data/list.json         最新日の全銘柄一覧
  _site/data/hist/<code>.json  銘柄ごとの推移
"""
import json
import shutil
from datetime import datetime
from pathlib import Path

import tracker

ROOT = Path(__file__).parent
SITE = ROOT / "_site"


def avg(val, sh):
    return round(val / sh, 1) if sh and val is not None else None


def dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def build():
    con = tracker.connect()
    tracker.import_csvs(con)
    hist_dir = SITE / "data" / "hist"
    if hist_dir.exists():
        shutil.rmtree(hist_dir)
    hist_dir.mkdir(parents=True)
    shutil.copy(ROOT / "index.html", SITE / "index.html")

    dates = [r[0] for r in con.execute("SELECT DISTINCT date FROM balances ORDER BY date")]
    flag_date = con.execute("SELECT MAX(date) FROM daily_flags").fetchone()[0]
    flags = {r["code"]: r["flag"] for r in con.execute(
        "SELECT code, flag FROM daily_flags WHERE date=?", (flag_date,))}

    hist = {}
    for r in con.execute(
            """SELECT b.*, p.close FROM balances b
               LEFT JOIN prices p ON p.code=b.code AND p.date=b.date
               ORDER BY b.code, b.date"""):
        hist.setdefault(r["code"], []).append(dict(r))
    con.close()

    latest = dates[-1] if dates else None
    rows = []
    for code, h in hist.items():
        last = h[-1]
        dump(hist_dir / f"{code}.json", {
            "code": code, "name": last["name"], "market": last["market"], "kind": last["kind"],
            "rows": [{"date": r["date"], "close": r["close"],
                      "avg": avg(r["buy_val"], r["buy_sh"]),
                      "avg_neg": avg(r["buy_neg_val"], r["buy_neg_sh"]),
                      "avg_std": avg(r["buy_std_val"], r["buy_std_sh"]),
                      "buy_val": r["buy_val"], "buy_neg_sh": r["buy_neg_sh"],
                      "buy_std_sh": r["buy_std_sh"]} for r in h]})
        if last["date"] != latest:
            continue                      # 最新の残高表から消えた銘柄は一覧に出さない
        prev = h[-2] if len(h) > 1 else None
        close = next((r["close"] for r in reversed(h) if r["close"] is not None), None)
        rows.append({
            "code": code, "name": last["name"], "market": last["market"], "kind": last["kind"],
            "flag": flags.get(code), "avg": avg(last["buy_val"], last["buy_sh"]),
            "avg_prev": avg(prev["buy_val"], prev["buy_sh"]) if prev else None,
            "close": close, "buy_val": last["buy_val"], "buy_sh": last["buy_sh"],
            "buy_chg_sh": last["buy_chg_sh"], "buy_ratio": last["buy_ratio"]})

    dump(SITE / "data" / "list.json", {
        "date": latest, "days": len(dates),
        "updated": datetime.now(tracker.JST).strftime("%Y-%m-%d %H:%M"), "rows": rows})
    print(f"_site: {latest} 現在 {len(rows)} 銘柄 / {len(dates)} 日分")


if __name__ == "__main__":
    build()
