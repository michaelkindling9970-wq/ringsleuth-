"""FTC Do Not Call complaint data -> small local SQLite table (one row per calling number).

Source: FTC "Do Not Call (DNC) Reported Calls Data" daily CSVs (public, no key):
  https://search.ftc.gov/sites/default/files/DNC_Complaint_Numbers_YYYY-MM-DD.csv
Only the calling (company) number, complaint count, last complaint date, robocall flag and the
most common subject are kept. Complainant city/state/area code are read but never stored.
Run `python3 ftc_data.py` to refresh manually.
"""
import csv, io, logging, os, sqlite3, threading, time, urllib.request
from collections import Counter
from datetime import date, datetime, timedelta

log = logging.getLogger("ringsleuth.ftc")
DB_PATH = os.environ.get("FTC_DB") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "ftc.db")
URL = "https://search.ftc.gov/sites/default/files/DNC_Complaint_Numbers_{}.csv"
WINDOW_DAYS = int(os.environ.get("FTC_WINDOW_DAYS", "45"))
MAX_AGE_DAYS = 7
_lock = threading.Lock()

def _conn(path=None):
    c = sqlite3.connect(path or DB_PATH, timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS ftc(number TEXT PRIMARY KEY, n INTEGER, last TEXT, robo INTEGER, topic TEXT) WITHOUT ROWID")
    c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    return c

def info(path=None):
    try:
        c = _conn(path); m = dict(c.execute("SELECT k,v FROM meta")); m["rows"] = c.execute("SELECT count(*) FROM ftc").fetchone()[0]; c.close(); return m
    except sqlite3.Error: return {}

def lookup(digits10, path=None):
    try:
        c = _conn(path); r = c.execute("SELECT n,last,robo,topic FROM ftc WHERE number=?", (digits10,)).fetchone(); c.close()
    except sqlite3.Error: return None
    return None if not r else {"count": r[0], "last": r[1], "robocall": bool(r[2]), "topic": r[3]}

def _fetch(d):
    req = urllib.request.Request(URL.format(d.isoformat()), headers={"User-Agent": "RingSleuth/1.0 (+https://ringsleuth.onrender.com)"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r: return r.read().decode("utf-8", "replace")
    except Exception: return None

def build(days=WINDOW_DAYS, today=None, fetch=_fetch, path=None):
    """Download the last `days` days of daily files and atomically replace the table."""
    today = today or date.today(); path = path or DB_PATH
    tmp = path + ".tmp"
    if os.path.exists(tmp): os.remove(tmp)
    c = _conn(tmp)  # stream rows into a temp staging table so memory stays small on Render's free tier
    c.execute("CREATE TEMP TABLE raw(number TEXT, d TEXT, robo INTEGER, subject TEXT)")
    files = []
    for i in range(days + 1):
        d = today - timedelta(days=i)
        if d.weekday() >= 5: continue
        text = fetch(d)
        if not text or not text.startswith("Company_Phone_Number"): continue
        files.append(d.isoformat()); rows = []
        for row in csv.DictReader(io.StringIO(text)):
            num = "".join(ch for ch in (row.get("Company_Phone_Number") or "") if ch.isdigit())[-10:]
            if len(num) != 10: continue
            s = (row.get("Subject") or "").strip()
            rows.append((num, (row.get("Created_Date") or "")[:10], 1 if (row.get("Recorded_Message_Or_Robocall") or "").strip().upper() == "Y" else 0,
                         None if not s or s == "No Subject Provided" else s))
        c.executemany("INSERT INTO raw VALUES(?,?,?,?)", rows); del text, rows
    if not files:
        c.close(); os.remove(tmp); raise RuntimeError("no FTC files could be downloaded")
    c.execute("CREATE TEMP TABLE subj AS SELECT number, subject, count(*) k FROM raw WHERE subject IS NOT NULL GROUP BY number, subject")
    c.execute("CREATE TEMP TABLE top AS SELECT number, subject FROM (SELECT number, subject, row_number() OVER (PARTITION BY number ORDER BY k DESC, subject) rn FROM subj) WHERE rn=1")
    c.execute("CREATE INDEX temp.top_i ON top(number)")
    c.execute("INSERT INTO ftc SELECT a.number, a.n, a.last, a.robo, t.subject FROM (SELECT number, count(*) n, max(d) last, max(robo) robo FROM raw GROUP BY number) a LEFT JOIN top t ON t.number=a.number")
    c.execute("DROP TABLE subj"); c.execute("DROP TABLE top")
    n = c.execute("SELECT count(*) FROM ftc").fetchone()[0]
    c.execute("DROP TABLE raw")
    c.executemany("INSERT INTO meta VALUES(?,?)", [("built", datetime.now().isoformat(timespec="seconds")), ("newest_file", max(files)), ("oldest_file", min(files)), ("files", str(len(files)))])
    c.commit(); c.execute("VACUUM"); c.close()
    os.replace(tmp, path)
    log.info("FTC data built: %d numbers from %d files (%s..%s)", n, len(files), min(files), max(files))
    return n

def stale(path=None):
    b = info(path).get("built")
    return not b or datetime.now() - datetime.fromisoformat(b) > timedelta(days=MAX_AGE_DAYS)

def ensure_fresh_async(path=None):
    """Lazy refresh: build in a background thread if missing or older than 7 days."""
    if not stale(path): return False
    def run():
        if not _lock.acquire(blocking=False): return
        lockf = (path or DB_PATH) + ".building"
        try:
            if os.path.exists(lockf) and time.time() - os.path.getmtime(lockf) < 1800: return  # another worker is on it
            open(lockf, "w").close()
            build(path=path)
        except Exception as e: log.warning("FTC refresh failed: %s", e)
        finally:
            try: os.remove(lockf)
            except OSError: pass
            _lock.release()
    threading.Thread(target=run, daemon=True).start()
    return True

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(build(), info())
