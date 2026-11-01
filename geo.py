"""Visitor geolocation -> local area codes -> small per-region FTC cache.

* Region comes from the client IP (first public address in Render's X-Forwarded-For), resolved with
  the free, no-key ip-api.com service (1.5 s timeout). Results are cached in memory only, keyed by a
  salted hash of the IP; raw IPs are never written to disk or logs.
* Region -> area codes uses area_codes.json (NANPA area-code list) plus libphonenumber's geocoder.
* Only the visitor region's FTC rows are ever held in RAM (bounded LRU of regions). Everything else is
  looked up on demand from the SQLite file on disk. GEO_STRICT=1 hides non-local results instead.
* If geolocation fails: no preload, plain on-demand lookup.
"""
import hashlib, ipaddress, json, logging, os, threading, time, urllib.request
from collections import OrderedDict

log = logging.getLogger("ringsleuth.geo")
_SALT = os.urandom(16)
_ip_cache = OrderedDict(); _IP_CACHE_MAX = 5000; _IP_TTL = 6 * 3600
_region_cache = OrderedDict(); _REGION_CACHE_MAX = int(os.environ.get("GEO_REGION_CACHE", "3"))
_lock = threading.Lock()

US_STATES = {"AL":"Alabama","AK":"Alaska","AZ":"Arizona","AR":"Arkansas","CA":"California","CO":"Colorado","CT":"Connecticut","DE":"Delaware","DC":"District of Columbia","FL":"Florida","GA":"Georgia","HI":"Hawaii","ID":"Idaho","IL":"Illinois","IN":"Indiana","IA":"Iowa","KS":"Kansas","KY":"Kentucky","LA":"Louisiana","ME":"Maine","MD":"Maryland","MA":"Massachusetts","MI":"Michigan","MN":"Minnesota","MS":"Mississippi","MO":"Missouri","MT":"Montana","NE":"Nebraska","NV":"Nevada","NH":"New Hampshire","NJ":"New Jersey","NM":"New Mexico","NY":"New York","NC":"North Carolina","ND":"North Dakota","OH":"Ohio","OK":"Oklahoma","OR":"Oregon","PA":"Pennsylvania","RI":"Rhode Island","SC":"South Carolina","SD":"South Dakota","TN":"Tennessee","TX":"Texas","UT":"Utah","VT":"Vermont","VA":"Virginia","WA":"Washington","WV":"West Virginia","WI":"Wisconsin","WY":"Wyoming","PR":"Puerto Rico"}
_NAME_TO_CODE = {v: k for k, v in US_STATES.items()}

def strict(): return os.environ.get("GEO_STRICT") == "1"

def client_ip(headers, remote_addr=None):
    """First public IP in X-Forwarded-For (Render puts the real client first), else remote_addr."""
    for part in [p.strip() for p in (headers.get("X-Forwarded-For") or "").split(",")] + [remote_addr or ""]:
        try:
            ip = ipaddress.ip_address(part)
            if ip.is_global: return str(ip)
        except ValueError: continue
    return None

def _ip_api(ip):
    url = f"http://ip-api.com/json/{ip}?fields=status,countryCode,region"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "RingSleuth/1.0"}), timeout=1.5) as r:
        d = json.loads(r.read(4096))
    if d.get("status") == "success" and d.get("countryCode") == "US" and d.get("region") in US_STATES: return d["region"]
    return None

def region_for_ip(ip, resolver=_ip_api):
    """2-letter US state code or None. Cached in memory under a salted hash (never the raw IP)."""
    if not ip: return None
    key = hashlib.sha256(_SALT + ip.encode()).hexdigest()[:20]; now = time.time()
    with _lock:
        hit = _ip_cache.get(key)
        if hit and now - hit[1] < _IP_TTL: _ip_cache.move_to_end(key); return hit[0]
    try: reg = resolver(ip)
    except Exception as e: log.info("geolocation failed: %s", type(e).__name__); reg = None
    with _lock:
        _ip_cache[key] = (reg, now)
        while len(_ip_cache) > _IP_CACHE_MAX: _ip_cache.popitem(last=False)
    return reg

_npa_by_state = None
def area_codes_for(state, area_codes=None):
    """Area codes (NPAs) serving a state, from the NANPA list in area_codes.json + libphonenumber geocoder."""
    global _npa_by_state
    custom = area_codes is not None
    if not custom and _npa_by_state is not None: return sorted(_npa_by_state.get(state, ()))
    if not custom:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "area_codes.json")) as f: area_codes = json.load(f)
    m = {}
    for npa, rec in area_codes.items():
        code = _NAME_TO_CODE.get(rec[1]) if len(rec) > 1 else None
        if code: m.setdefault(code, set()).add(npa)
    try:  # fill gaps (newer overlays) from the libphonenumber geocoder snapshot npa_geo.json ("City, ST" or state name)
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "npa_geo.json")) as f: npa_geo = json.load(f)
        for s, desc in npa_geo.items():
            if s in area_codes: continue
            code = desc.rsplit(", ", 1)[-1] if ", " in desc else _NAME_TO_CODE.get(desc)
            if code in US_STATES: m.setdefault(code, set()).add(s)
    except (OSError, ValueError): pass
    if not custom: _npa_by_state = m
    return sorted(m.get(state, ()))

def region_cache(state, loader):
    """In-memory FTC rows for one region's area codes only (LRU of a few regions)."""
    if not state: return None
    with _lock:
        if state in _region_cache: _region_cache.move_to_end(state); return _region_cache[state]
    rows = loader(area_codes_for(state))
    with _lock:
        _region_cache[state] = rows
        while len(_region_cache) > _REGION_CACHE_MAX: _region_cache.popitem(last=False)
    return rows

def clear_caches():
    with _lock: _region_cache.clear(); _ip_cache.clear()
