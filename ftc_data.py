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
DB_PATH = os.environ.get("FTC_DB") or os.path.join(os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__)), "ftc.db")
URL = "https://search.ftc.gov/sites/default/files/DNC_Complaint_Numbers_{}.csv"
WINDOW_DAYS = int(os.environ.get("FTC_WINDOW_DAYS", "45"))
MAX_AGE_DAYS = 7
_lock = threading.Lock()

def lookup_many(numbers_prefix_npas, path=None, limit=50000):
    """Rows whose number starts with one of the given area codes (used for the per-region cache)."""
    out = {}
    try:
        c = _conn(path)
        for npa in numbers_prefix_npas:
            for num, n, last, robo, topic in c.execute("SELECT number,n,last,robo,topic FROM ftc WHERE number>=? AND number<? LIMIT ?", (npa, npa[:-1] + chr(ord(npa[-1]) + 1), limit)):
                out[num] = {"count": n, "last": last, "robocall": bool(robo), "topic": topic}
                if len(out) >= limit: break
        c.close()
    except sqlite3.Error: pass
    return out

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
    """Yield CSV lines one at a time straight off the network socket (never the whole file in RAM)."""
    req = urllib.request.Request(URL.format(d.isoformat()), headers={"User-Agent": "RingSleuth/1.0 (+https://ringsleuth.onrender.com)"})
    try: r = urllib.request.urlopen(req, timeout=60)
    except Exception: return None
    def lines():
        with r:
            for line in io.TextIOWrapper(r, encoding="utf-8", errors="replace", newline=""): yield line
    return lines()

def _iter_lines(src):
    # tests may pass a whole string; production passes a line iterator
    return io.StringIO(src) if isinstance(src, str) else src

BATCH = 2000

def build(days=WINDOW_DAYS, today=None, fetch=_fetch, path=None):
    """Download the last `days` days of daily files and atomically replace the table."""
    today = today or date.today(); path = path or DB_PATH
    tmp = path + ".tmp"
    if os.path.exists(tmp): os.remove(tmp)
    c = _conn(tmp)
    # keep SQLite's own RAM small: temp tables/sorts spill to disk, ~2MB page cache
    c.execute("PRAGMA temp_store=FILE"); c.execute("PRAGMA cache_size=-2000"); c.execute("PRAGMA journal_mode=OFF")
    # stream rows into a temp staging table so memory stays small on Render's free tier
    c.execute("CREATE TEMP TABLE raw(number TEXT, d TEXT, robo INTEGER, subject TEXT)")
    files = []
    for i in range(days + 1):
        d = today - timedelta(days=i)
        if d.weekday() >= 5: continue
        src = fetch(d)
        if src is None: continue
        it = iter(_iter_lines(src))
        header = next(it, "")
        if not header.startswith("Company_Phone_Number"): continue
        files.append(d.isoformat()); batch = []
        for row in csv.DictReader(it, fieldnames=next(csv.reader([header]))):
            num = "".join(ch for ch in (row.get("Company_Phone_Number") or "") if ch.isdigit())[-10:]
            if len(num) != 10: continue
            s = (row.get("Subject") or "").strip()
            batch.append((num, (row.get("Created_Date") or "")[:10], 1 if (row.get("Recorded_Message_Or_Robocall") or "").strip().upper() == "Y" else 0,
                          None if not s or s == "No Subject Provided" else s))
            if len(batch) >= BATCH: c.executemany("INSERT INTO raw VALUES(?,?,?,?)", batch); batch = []
        if batch: c.executemany("INSERT INTO raw VALUES(?,?,?,?)", batch)
        c.commit(); del batch, it, src
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

STALE_LOCK_SECS = 1800

def acquire_file_lock(lockf, now=None):
    """Cross-process lock (gunicorn workers / restarts). A lock older than STALE_LOCK_SECS, or whose
    owning PID is gone, is treated as stale (e.g. the process was OOM-killed mid-build) and taken over."""
    now = now or time.time()
    for _ in range(2):
        try:
            fd = os.open(lockf, os.O_CREAT | os.O_EXCL | os.O_WRONLY); os.write(fd, str(os.getpid()).encode()); os.close(fd); return True
        except FileExistsError:
            try:
                age = now - os.path.getmtime(lockf); pid = int(open(lockf).read().strip() or 0)
            except (OSError, ValueError): age, pid = STALE_LOCK_SECS + 1, 0
            alive = False
            if pid:
                try: os.kill(pid, 0); alive = True
                except OSError: alive = False
            if age < STALE_LOCK_SECS and alive: return False
            log.info("removing stale FTC build lock (age %ds, pid %s)", age, pid)
            try: os.remove(lockf)
            except OSError: pass
    return False

def ensure_fresh_async(path=None):
    """Lazy refresh: build in a background thread if missing or older than 7 days."""
    if not stale(path): return False
    def run():
        if not _lock.acquire(blocking=False): return
        lockf = (path or DB_PATH) + ".building"
        got = False
        try:
            got = acquire_file_lock(lockf)
            if got: build(path=path)
        except Exception as e: log.warning("FTC refresh failed: %s", e)
        finally:
            if got:
                try: os.remove(lockf)
                except OSError: pass
            _lock.release()
    threading.Thread(target=run, daemon=True).start()
    return True

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(build(), info())
