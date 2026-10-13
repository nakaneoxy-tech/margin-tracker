"""JPX 信用取引残高の取得・パース・保存。

  python tracker.py            # 未取得分を取り込み
  python tracker.py --force    # 掲載中の全ファイルを取り込み直す

保存の実体は data/daily/YYYY-MM-DD.csv（1日1ファイル・リポジトリにコミット）。
data/margin.db は CSV から毎回作り直せる作業用。

データ元:
  銘柄別信用取引残高 (PDF, 全銘柄・株数+金額)  .../margin/01.html
  日々公表銘柄等信用取引残高 (xlsx, 対象銘柄の一覧) .../margin/index.html
平均建玉単価 = 買残高(金額) / 買残高(株数)
"""
import csv
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "https://www.jpx.co.jp"
PAGE_ALL = BASE + "/markets/statistics-equities/margin/01.html"
PAGE_DAILY = BASE + "/markets/statistics-equities/margin/index.html"
UA = {"User-Agent": "Mozilla/5.0 (margin-tracker)"}

ROOT = Path(__file__).parent
RAW = ROOT / "data" / "raw"
DAILY = ROOT / "data" / "daily"
DB = ROOT / "data" / "margin.db"
JST = timezone(timedelta(hours=9))

SCHEMA = """
CREATE TABLE IF NOT EXISTS balances (
  date TEXT, code TEXT, name TEXT, market TEXT, kind TEXT,
  sell_sh INTEGER, sell_val INTEGER,
  buy_sh INTEGER, buy_chg_sh INTEGER, buy_val INTEGER, buy_chg_val INTEGER,
  buy_ratio REAL,
  buy_neg_sh INTEGER, buy_neg_val INTEGER, buy_std_sh INTEGER, buy_std_val INTEGER,
  PRIMARY KEY (date, code)
);
CREATE TABLE IF NOT EXISTS daily_flags (
  date TEXT, code TEXT, flag TEXT, note TEXT,
  PRIMARY KEY (date, code)
);
CREATE TABLE IF NOT EXISTS prices (
  date TEXT, code TEXT, close REAL,
  PRIMARY KEY (date, code)
);
CREATE TABLE IF NOT EXISTS files (name TEXT PRIMARY KEY, fetched_at TEXT);
"""


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def http_get(url, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def find_links(page_url, pattern):
    html = http_get(page_url).decode("utf-8", "replace")
    return sorted(set(BASE + m for m in re.findall(pattern, html)))


def pdftotext_path():
    exe = shutil.which("pdftotext")
    if exe:
        return exe
    for p in (r"C:\Program Files\Git\mingw64\bin\pdftotext.exe",
              r"C:\Program Files (x86)\Git\mingw64\bin\pdftotext.exe"):
        if Path(p).exists():
            return p
    raise RuntimeError("pdftotext が見つかりません（Git for Windows か poppler が必要）")


# ---------- PDF（全銘柄・株数+金額） ----------

ROW_SH = re.compile(r"^(.*?)\b([0-9][0-9A-Z]{3}[0-9]) ([A-Z]{2}[0-9A-Z]{10}) 株数 Shs\. (.+)$")
ROW_VAL = re.compile(r"\b([0-9][0-9A-Z]{3}[0-9]) ([A-Z]{2}[0-9A-Z]{10}) 金額 Val\. (.+)$")
AS_OF = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2}) 申込み現在")


def _nums(s):
    """'10,900 ▲ 2,600 0.1% * -' → [10900, -2600, 0.1, None, None]"""
    out = []
    for t in re.sub(r"▲\s*", "-", s).split():
        t = t.replace(",", "").rstrip("%")
        try:
            out.append(float(t) if "." in t else int(t))
        except ValueError:
            out.append(None)
    return out


def parse_pdf(path):
    text = subprocess.run([pdftotext_path(), "-raw", "-enc", "UTF-8", str(path), "-"],
                          capture_output=True, check=True).stdout.decode("utf-8", "replace")
    m = AS_OF.search(text)
    if not m:
        raise RuntimeError(f"{path.name}: 申込み日が読めません")
    date = "%04d-%02d-%02d" % tuple(int(x) for x in m.groups())

    rows, pending, prev = {}, None, ""
    for line in text.splitlines():
        line = line.strip()
        m = ROW_SH.match(line)
        if m:
            head, code, _, rest = m.groups()
            n = _nums(rest)
            if len(n) == 14:
                parts = head.split()
                kind = parts.pop() if parts and parts[-1] in ("貸", "制") else ""
                market = parts.pop() if parts else ""
                name = " ".join(parts) or prev      # 長い銘柄名は前の行に折り返される
                name = re.sub(r"^[A-Z] ", "", unicodedata.normalize("NFKC", name))
                name = re.sub(r" 普通株式$", "", name)
                pending = dict(code=code, name=name, market=market, kind=kind,
                               sell_sh=n[0], buy_sh=n[3], buy_chg_sh=n[4], buy_ratio=n[5],
                               buy_neg_sh=n[10], buy_std_sh=n[12])
            prev = line
            continue
        m = ROW_VAL.search(line)
        if m and pending and pending["code"] == m.group(1):
            n = _nums(m.group(3))
            if len(n) == 14:
                pending.update(sell_val=n[0], buy_val=n[3], buy_chg_val=n[4],
                               buy_neg_val=n[10], buy_std_val=n[12])
                rows[pending["code"]] = pending
            pending = None
        prev = line
    return date, list(rows.values())


def store_balances(con, date, rows):
    con.executemany(
        """INSERT OR REPLACE INTO balances VALUES
           (:date,:code,:name,:market,:kind,:sell_sh,:sell_val,:buy_sh,:buy_chg_sh,
            :buy_val,:buy_chg_val,:buy_ratio,:buy_neg_sh,:buy_neg_val,:buy_std_sh,:buy_std_val)""",
        [dict(r, date=date) for r in rows])


# ---------- xlsx（日々公表銘柄等の一覧） ----------

def parse_daily_xlsx(path):
    import openpyxl
    ws = openpyxl.load_workbook(path, data_only=True, read_only=True).worksheets[0]
    date, rows = None, []
    for r in ws.iter_rows(values_only=True):
        r = list(r) + [None] * 8
        if date is None and isinstance(r[1], str) and "申込み現在" in str(r[3] or ""):
            y, mo, d = (int(x) for x in r[1].split("/"))
            date = "%04d-%02d-%02d" % (y, mo, d)
        code = str(r[6] or "")
        if re.fullmatch(r"[0-9][0-9A-Z]{3}[0-9]", code) and r[1]:
            rows.append((code, str(r[1]), str(r[2] or "")))
    if not date:
        raise RuntimeError(f"{path.name}: 申込み日が読めません")
    return date, rows


# ---------- 株価（Yahoo Finance・失敗しても残高取り込みは止めない） ----------

def fetch_prices(code, rng="3mo"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code[:4]}.T?range={rng}&interval=1d"
    try:
        body = http_get(url, timeout=15)
    except Exception:
        time.sleep(2)                    # 混雑時の一時的な拒否に1回だけ再試行
        body = http_get(url, timeout=15)
    res = json.loads(body)["chart"]["result"][0]
    closes = res["indicators"]["quote"][0]["close"]
    out = []
    for ts, c in zip(res.get("timestamp") or [], closes):
        if c is not None:
            out.append((datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d"), code, round(c, 1)))
    return out


def update_prices(con, codes, rng="1mo"):
    def one(code):
        try:
            return fetch_prices(code, rng)
        except Exception:
            return []
    with ThreadPoolExecutor(16) as ex:
        for rows in ex.map(one, codes):
            con.executemany("INSERT OR REPLACE INTO prices VALUES (?,?,?)", rows)
    con.commit()


# ---------- CSV（リポジトリに残す実体） ----------

CSV_COLS = ["code", "name", "market", "kind", "sell_sh", "sell_val", "buy_sh", "buy_chg_sh",
            "buy_val", "buy_chg_val", "buy_ratio", "buy_neg_sh", "buy_neg_val",
            "buy_std_sh", "buy_std_val", "flag", "note", "close"]


def export_csv(con, date):
    DAILY.mkdir(parents=True, exist_ok=True)
    rows = con.execute(
        """SELECT b.*, f.flag, f.note, p.close FROM balances b
           LEFT JOIN daily_flags f ON f.code=b.code AND f.date=b.date
           LEFT JOIN prices p ON p.code=b.code AND p.date=b.date
           WHERE b.date=? ORDER BY b.code""", (date,)).fetchall()
    with open(DAILY / f"{date}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLS)
        for r in rows:
            w.writerow(["" if r[c] is None else r[c] for c in CSV_COLS])


def import_csvs(con):
    """DB に無い日付の CSV を読み込む（Actions では毎回ここから DB を作り直す）。"""
    have = {r[0] for r in con.execute("SELECT DISTINCT date FROM balances")}
    for path in sorted(DAILY.glob("*.csv")):
        date = path.stem
        if date in have:
            continue
        with open(path, newline="", encoding="utf-8") as f:
            rows = [{k: (v if v != "" else None) for k, v in r.items()} for r in csv.DictReader(f)]
        store_balances(con, date, rows)
        con.executemany("INSERT OR REPLACE INTO daily_flags VALUES (?,?,?,?)",
                        [(date, r["code"], r["flag"], r["note"] or "") for r in rows if r["flag"]])
        con.executemany("INSERT OR REPLACE INTO prices VALUES (?,?,?)",
                        [(date, r["code"], r["close"]) for r in rows if r["close"]])
    con.commit()


# ---------- 取り込み ----------

def file_date(name):
    return f"{name[:4]}-{name[4:6]}-{name[6:8]}"


def update(force=False, log=print):
    RAW.mkdir(parents=True, exist_ok=True)
    con = connect()
    import_csvs(con)
    have_bal = {r[0] for r in con.execute("SELECT DISTINCT date FROM balances")}
    have_flag = {r[0] for r in con.execute("SELECT DISTINCT date FROM daily_flags")}
    changed = set()

    links = find_links(PAGE_ALL, r'href="([^"]+_mtall\.pdf)"') + \
            find_links(PAGE_DAILY, r'href="([^"]+_mtdaily\.xlsx)"')
    for url in links:
        name = url.rsplit("/", 1)[1]
        is_pdf = name.endswith(".pdf")
        if not force and file_date(name) in (have_bal if is_pdf else have_flag):
            continue
        path = RAW / name
        if force or not path.exists():
            path.write_bytes(http_get(url))
        if is_pdf:
            date, rows = parse_pdf(path)
            if len(rows) < 3000:
                raise RuntimeError(f"{name}: {len(rows)} 銘柄しか読めません（PDFの形式が変わった可能性）")
            store_balances(con, date, rows)
            log(f"{name}: {date} 残高 {len(rows)} 銘柄")
        else:
            date, rows = parse_daily_xlsx(path)
            con.executemany("INSERT OR REPLACE INTO daily_flags VALUES (?,?,?,?)",
                            [(date, c, f, n) for c, f, n in rows])
            log(f"{name}: {date} 日々公表銘柄等 {len(rows)} 銘柄")
        con.commit()
        changed.add(date)

    latest = con.execute("SELECT MAX(date) FROM balances").fetchone()[0]
    if latest:
        n, priced = con.execute(
            """SELECT COUNT(*), COUNT(p.close) FROM balances b
               LEFT JOIN prices p ON p.code=b.code AND p.date=b.date WHERE b.date=?""",
            (latest,)).fetchone()
        if changed or priced < n * 0.9:          # 前回の株価取得が失敗していたら取り直す
            codes = [r[0] for r in con.execute("SELECT code FROM balances WHERE date=?", (latest,))]
            log(f"株価を取得中… {len(codes)} 銘柄")
            update_prices(con, codes)
            changed |= {r[0] for r in con.execute("SELECT DISTINCT date FROM balances")}
    if not changed:
        log("新しいファイルはありません")
    for date in sorted(changed):
        if con.execute("SELECT 1 FROM balances WHERE date=? LIMIT 1", (date,)).fetchone():
            export_csv(con, date)
    con.close()
    return len(changed)


if __name__ == "__main__":
    update(force="--force" in sys.argv)
