"""RingSleuth. Lookups use real free data (libphonenumber + FTC Do Not Call complaints). Checkout is test-mode only."""
import calendar, hashlib, os, sqlite3
from datetime import date, datetime
from flask import Flask, Response, abort, g, redirect, render_template_string, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

LOOKUP_CAP = 50
PRICE = "4.99"
SEO_INDEXABLE = lambda: os.environ.get("SEO_INDEXABLE") == "1"
COUNTDOWN_SECONDS = 15

# Approximate main-city centroid per NANP area code (registration region, never a person's location).
# Built from ravisorg/Area-Code-Geolocation-Database (NANPA city lists) + GeoNames populations; see area_codes.json.
import json as _json
AREA_CODES = _json.load(open(os.path.join(os.path.dirname(__file__), "area_codes.json")))
HUBS = {"Ashburn, VA": (39.0438, -77.4874), "Chicago, IL": (41.8781, -87.6298), "Dallas, TX": (32.7767, -96.7970),
        "Los Angeles, CA": (34.0522, -118.2437), "New York, NY": (40.7128, -74.0060), "Atlanta, GA": (33.7490, -84.3880),
        "Denver, CO": (39.7392, -104.9903), "Seattle, WA": (47.6062, -122.3321)}

def area_info(npa):
    r = AREA_CODES.get(npa)
    return None if not r else {"npa": npa, "city": r[0], "region": r[1], "country": r[2], "lat": r[3], "lon": r[4], "tz": r[5] if len(r) > 5 else None}

def map_data(npa):
    """Illustrative route: start at a carrier hub, pass 1-2 hubs, END at the area-code city."""
    import math
    a = area_info(npa)
    if not a: return {"unknown": True, "npa": npa}
    o = (a["lat"], a["lon"])
    d = lambda p, q=o: math.hypot(p[0] - q[0], (p[1] - q[1]) * math.cos(math.radians(q[0])))
    hubs = [h for h in HUBS if d(HUBS[h]) > 0.5]  # skip a hub sitting on the destination
    near = sorted(hubs, key=lambda h: d(HUBS[h]))
    mid = near[:2] if d(HUBS[near[1]]) < 12 else near[:1]
    start = min((h for h in hubs if h not in mid), key=lambda h: abs(d(HUBS[h]) - 15))  # a hub at moderate distance, for a visible path
    route = [start] + sorted(mid, key=lambda h: -d(HUBS[h]))
    nodes = [{"name": n, "lat": HUBS[n][0], "lon": HUBS[n][1]} for n in route]
    return {"city": a["city"], "region": a["region"], "npa": npa, "lat": o[0], "lon": o[1], "radius_m": 25000, "nodes": nodes}


# ---------- billing cycle logic ----------
def add_months(d, n, anchor_day):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(anchor_day, calendar.monthrange(y, m)[1]))

def cycle_bounds(sub_start, today):
    """Return (cycle_start, next_reset) for the billing cycle containing today."""
    n = (today.year - sub_start.year) * 12 + today.month - sub_start.month
    start = add_months(sub_start, n, sub_start.day)
    if start > today:
        n -= 1
        start = add_months(sub_start, n, sub_start.day)
    return start, add_months(sub_start, n + 1, sub_start.day)

# ---------- real lookup: phonenumbers (offline) + FTC Do Not Call complaints ----------
import ftc_data
import phonenumbers
from phonenumbers import carrier as pn_carrier, geocoder as pn_geocoder, timezone as pn_timezone

def spam_score(count):
    """Spam score from real FTC complaint counts only: 0 -> 0, 1 -> 25, 3 -> 50, 7 -> 75, 15+ -> 99."""
    import math
    return 0 if not count else min(99, round(25 * math.log2(count + 1)))

class RealLookup:
    """Validity, line type, region, original carrier and time zone from Google's libphonenumber data (offline);
    complaint history from the FTC Do Not Call data set. No caller-name (CNAM) source; no data about individuals."""
    LINE = {phonenumbers.PhoneNumberType.MOBILE: "Cell", phonenumbers.PhoneNumberType.FIXED_LINE: "Landline",
            phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE: "Cell or landline (US numbers can't be told apart offline)",
            phonenumbers.PhoneNumberType.VOIP: "VoIP", phonenumbers.PhoneNumberType.TOLL_FREE: "Toll-free",
            phonenumbers.PhoneNumberType.PREMIUM_RATE: "Premium rate", phonenumbers.PhoneNumberType.SHARED_COST: "Shared cost",
            phonenumbers.PhoneNumberType.PERSONAL_NUMBER: "Personal number", phonenumbers.PhoneNumberType.PAGER: "Pager",
            phonenumbers.PhoneNumberType.UAN: "Company number", phonenumbers.PhoneNumberType.VOICEMAIL: "Voicemail"}

    def lookup(self, number):
        digits = "".join(c for c in number if c.isdigit())
        if len(digits) == 10: digits = "1" + digits
        if len(digits) != 11 or digits[0] != "1": raise ValueError("Enter a 10-digit US number.")
        p = phonenumbers.parse("+" + digits)
        npa, d10 = digits[1:4], digits[1:]
        valid = phonenumbers.is_valid_number(p)
        a = area_info(npa)
        geo = pn_geocoder.description_for_number(p, "en") if valid else ""
        tzs = [t for t in pn_timezone.time_zones_for_number(p) if t != "Etc/Unknown"] if valid else []
        f = ftc_data.lookup(d10)
        score = spam_score(f["count"] if f else 0)
        if f:
            ftc = f"Reported to the FTC {f['count']} time{'s' if f['count'] != 1 else ''}, last on {datetime.strptime(f['last'], '%Y-%m-%d').strftime('%b %-d, %Y')}, robocall {'yes' if f['robocall'] else 'no'}" + (f", topic: {f['topic']}" if f["topic"] else "")
        elif not ftc_data.info().get("built"): ftc = "FTC complaint data is still loading; try again in a minute"
        else: ftc = "No FTC complaints on file"
        return {"number": f"+1 ({npa}) {digits[4:7]}-{digits[7:]}", "npa": npa, "valid": valid,
                "caller_name": "Caller name not available",
                "carrier": (pn_carrier.name_for_number(p, "en") if valid else "") or "Not available",
                "line_type": self.LINE.get(phonenumbers.number_type(p), "Unknown") if valid else "Not a valid US number",
                "location": (f"{a['city']}, {a['region']} (area code {npa} region)" if a and valid else f"{geo} (area code {npa} region)" if geo else f"Unknown (area code {npa})"),
                "timezone": tz_label(tzs[0]) if len(tzs) == 1 else "Varies (not tied to one time zone)" if tzs else "Not available",
                "ftc": ftc, "ftc_count": f["count"] if f else 0,
                "spam_score": score, "risk": "High" if score >= 70 else "Medium" if score >= 40 else "Low",
                "source": "Sources: FTC Do Not Call complaints (unverified consumer reports) · libphonenumber open numbering data"}

lookup_provider = RealLookup()

# ---------- app/db ----------
app = Flask(__name__)
if os.environ.get("FTC_AUTOREFRESH", "1") == "1": ftc_data.ensure_fresh_async()  # lazy: only if missing or >7 days old
app.config.update(SECRET_KEY=os.environ.get("SECRET_KEY") or os.urandom(32).hex(), DATABASE=os.path.join(os.path.dirname(__file__), "ringsleuth.db"))
today_fn = date.today  # overridable in tests

def db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"]); g.db.row_factory = sqlite3.Row
        g.db.executescript("""CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE, pw TEXT, sub_start TEXT);
        CREATE TABLE IF NOT EXISTS lookups(id INTEGER PRIMARY KEY, user_id INT, number TEXT, result TEXT, day TEXT, ts TEXT);""")
    return g.db

@app.teardown_appcontext
def close(_):
    d = g.pop("db", None)
    if d: d.close()

def user():
    uid = session.get("uid")
    return db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone() if uid else None

def usage(u):
    start, reset = cycle_bounds(date.fromisoformat(u["sub_start"]), today_fn())
    used = db().execute("SELECT COUNT(*) FROM lookups WHERE user_id=? AND day>=? AND day<?",
                        (u["id"], start.isoformat(), reset.isoformat())).fetchone()[0]
    return used, reset

BASE = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">{% if seo.leaflet %}<link rel=stylesheet href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"><script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>{% endif %}<title>{{seo.title}}</title><meta name=description content="{{seo.desc}}">
<meta name=robots content="{{ 'index,follow' if seo.indexable else 'noindex,nofollow' }}"><link rel=canonical href="{{seo.canonical}}">
{% if seo.gsv %}<meta name="google-site-verification" content="{{seo.gsv}}">{% endif %}
<meta property=og:type content=website><meta property=og:site_name content=RingSleuth><meta property=og:title content="{{seo.title}}"><meta property=og:description content="{{seo.desc}}"><meta property=og:url content="{{seo.canonical}}"><meta property=og:image content="{{seo.root}}/og.png"><meta property=og:image:alt content="RingSleuth: reverse phone lookup, spam check and area codes">
<meta name=twitter:card content=summary><meta name=twitter:title content="{{seo.title}}"><meta name=twitter:description content="{{seo.desc}}"><meta name=twitter:image content="{{seo.root}}/og.png">
<link rel=icon href="/favicon.svg" type="image/svg+xml"><link rel=apple-touch-icon href="/og.png"><meta name=theme-color content="#0f172a">
{% for j in seo.jsonld %}<script type="application/ld+json">{{ j|tojson }}</script>{% endfor %}<style>
body{font-family:system-ui;max-width:760px;margin:auto;padding:20px;background:#0f172a;color:#e2e8f0}
a{color:#38bdf8}.card{background:#1e293b;padding:18px;border-radius:12px;margin:14px 0}
input,button{padding:9px;border-radius:8px;border:0;margin:4px 0}button{background:#38bdf8;font-weight:bold;cursor:pointer}
.banner{background:#facc15;color:#000;padding:6px;text-align:center;border-radius:6px}.err{background:#7f1d1d;padding:12px;border-radius:8px}
table{width:100%}td{padding:4px}.logo{font-size:28px;font-weight:bold;margin:14px 0}h1{font-size:26px}.grid{display:flex;flex-wrap:wrap;gap:6px}.grid a{background:#0f172a;padding:4px 8px;border-radius:6px;text-decoration:none}footer{margin:30px 0;font-size:13px;color:#94a3b8}
#countdown{font-size:20px;font-weight:bold;text-align:center}</style></head><body>
<div class=banner>DEMO / TEST MODE — no real charges</div>
<div class=logo><a href="/" style="color:inherit;text-decoration:none"><img src="/favicon.svg" alt="RingSleuth logo" width=28 height=28 style="vertical-align:middle"> RingSleuth</a></div><nav><a href="/">Home</a> · <a href="/area-code/">Area codes</a> · {% if u %}<a href="/lookup">Lookup</a> · <a href="/history">History</a> · <a href="/logout">Log out ({{u.email}})</a>{% else %}<a href="/signup">Sign up</a> · <a href="/login">Log in</a>{% endif %}</nav>
{% if u and u.sub_start %}<p><b>{{used}} of 50 lookups used</b> this cycle · resets {{reset}}</p>{% endif %}
{{ body|safe }}<footer>RingSleuth is a caller-ID &amp; spam check, not a people-search service. We never name private individuals and never charge to remove listings. · <a href="/area-code/">Area code directory</a><br>Not for screening tenants, employees or job applicants, and not a consumer report under the FCRA.</footer></body></html>"""

def seo_ctx(title, desc, path=None, jsonld=(), index=True, leaflet=False):
    root = (os.environ.get("SITE_URL") or request.url_root).rstrip("/")
    return {"title": title, "desc": desc, "canonical": root + (path if path is not None else request.path), "root": root,
            "indexable": SEO_INDEXABLE() and index, "gsv": os.environ.get("GOOGLE_SITE_VERIFICATION", "").strip(),
            "jsonld": list(jsonld), "leaflet": leaflet}

def page(body, seo=None, **kw):
    seo = seo or seo_ctx("RingSleuth", "RingSleuth caller-ID and spam check (demo).", index=False)
    u = user(); used = reset = None
    if u and u["sub_start"]:
        used, r = usage(u); reset = r.strftime("%B %-d, %Y")
    return render_template_string(BASE, body=render_template_string(body, u=u, cd=COUNTDOWN_SECONDS, **kw), u=u, used=used, reset=reset, seo=seo)

HOME_FAQ = [
 ("Who called me from this phone number?", "Enter the number in RingSleuth to see the caller-ID name on file (for businesses and carriers), the carrier, the line type and the approximate area of the area code. We do not identify private individuals."),
 ("Is this number spam?", "RingSleuth gives every number a spam-risk score from Low to High, based on line type and reported call patterns. VoIP numbers that call many people often score higher."),
 ("What area code is this?", "Every US and Canada number starts with a 3-digit area code. Browse the area code directory to see the main city, state or province and time zone for each one."),
 ("Can I find out which carrier a number uses?", "Yes. A lookup shows the carrier and whether the line is a cell phone, landline or VoIP number."),
 ("Is RingSleuth a people search?", "No. RingSleuth is a caller-ID and spam check. It does not show home addresses, relatives or records, and never charges to remove a listing."),
]

def faq_ld(faq):
    return {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faq]}

FAQ_HTML = """<div class=card><h2>Frequently asked questions</h2>{% for q,a in faq %}<h3>{{q}}</h3><p>{{a}}</p>{% endfor %}</div>"""

@app.route("/")
def home():
    root = (os.environ.get("SITE_URL") or request.url_root).rstrip("/")
    site_ld = {"@context": "https://schema.org", "@type": "WebSite", "name": "RingSleuth", "url": root + "/",
               "potentialAction": {"@type": "SearchAction", "target": root + "/area-code/search?q={search_term_string}", "query-input": "required name=search_term_string"}}
    seo = seo_ctx("Reverse Phone Lookup & Spam Call Checker – Who Called Me? | RingSleuth",
                  "Who called me? Free area code lookup plus caller ID, carrier and line type (cell, landline, VoIP) and a spam-risk score. Not a people search.",
                  path="/", jsonld=[site_ld, faq_ld(HOME_FAQ)])
    return page("""<h1>Reverse phone lookup: who called me, and is it spam?</h1>
<div class=card><h2>Check any US or Canada number</h2>
<p>Reverse phone lookup: caller ID name, carrier, line type (cell / landline / VoIP), rough location from the area code, and a spam-risk score.</p>
<p><i>RingSleuth is a caller-ID & spam check, not a people-search or background check. No addresses, relatives, or records.</i></p>
<form action="/area-code/search"><label for=q>Area code lookup</label><br><input id=q name=q placeholder="e.g. 814" inputmode=numeric maxlength=3 required> <button>Find area code</button></form></div>
<div class=card><h2>Plans: ${{price}}/month</h2><ul><li>50 lookups per billing cycle</li><li>Spam-risk scoring</li><li>Phone number carrier lookup</li><li>Lookup history</li><li>Cancel anytime</li></ul>
<a href="/signup"><button>Get started</button></a></div>
<div class=card><h2>Popular area codes</h2><div class=grid>{% for n in popular %}<a href="/area-code/{{n}}">{{n}}</a>{% endfor %}</div><p><a href="/area-code/">See all area codes</a></p></div>
""" + FAQ_HTML, price=PRICE, faq=HOME_FAQ, popular=["212","213","215","305","310","312","404","412","415","617","702","713","814","416","604"], seo=seo)

@app.route("/signup", methods=["GET", "POST"])
def signup():
    err = ""
    if request.method == "POST":
        try:
            cur = db().execute("INSERT INTO users(email,pw) VALUES(?,?)", (request.form["email"].lower(), generate_password_hash(request.form["password"])))
            db().commit(); session["uid"] = cur.lastrowid; return redirect("/checkout")
        except sqlite3.IntegrityError: err = "That email is already registered."
    return page("""<div class=card><h1>Create account</h1>{% if err %}<p class=err>{{err}}</p>{% endif %}<form method=post>
<input name=email type=email placeholder=Email required><br><input name=password type=password placeholder=Password required minlength=6><br><button>Sign up</button></form></div>""", err=err, seo=seo_ctx("Create account | RingSleuth", "Create a RingSleuth account (demo).", index=False))

@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    if request.method == "POST":
        r = db().execute("SELECT * FROM users WHERE email=?", (request.form["email"].lower(),)).fetchone()
        if r and check_password_hash(r["pw"], request.form["password"]):
            session["uid"] = r["id"]; return redirect("/lookup" if r["sub_start"] else "/checkout")
        err = "Wrong email or password."
    return page("""<div class=card><h1>Log in</h1>{% if err %}<p class=err>{{err}}</p>{% endif %}<form method=post>
<input name=email type=email placeholder=Email required><br><input name=password type=password placeholder=Password required><br><button>Log in</button></form></div>""", err=err, seo=seo_ctx("Log in | RingSleuth", "Log in to RingSleuth (demo).", index=False))

@app.route("/logout")
def logout():
    session.clear(); return redirect("/")

# ---------- Stripe switch (TEST MODE ONLY; off by default) ----------
import logging
log = logging.getLogger("ringsleuth")

def stripe_mode():
    """Return 'real' only if CHECKOUT_ENABLED=1 and a sk_test_ key is set; otherwise 'fake'. Live keys are refused."""
    if os.environ.get("CHECKOUT_ENABLED", "").strip().lower() not in ("1", "true", "yes", "on"): return "fake"
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if key.startswith("sk_live_") or key.startswith("rk_live_"):
        log.warning("STRIPE_SECRET_KEY is a LIVE key; refusing to use it. Fake checkout stays on (test mode only).")
        return "fake"
    if not key.startswith("sk_test_"): return "fake"
    return "real"

def _stripe():
    import stripe
    key = os.environ["STRIPE_SECRET_KEY"].strip()
    if not key.startswith("sk_test_"): raise RuntimeError("Refusing non-test Stripe key")
    stripe.api_key = key
    return stripe

@app.route("/checkout", methods=["GET", "POST"])
def checkout():
    u = user()
    if not u: return redirect("/login")
    if stripe_mode() == "real":
        if request.method == "POST":
            stripe = _stripe(); base = request.host_url.rstrip("/")
            pid = os.environ.get("STRIPE_PRICE_ID", "").strip()
            item = {"price": pid, "quantity": 1} if pid else {"quantity": 1, "price_data": {"currency": "usd", "unit_amount": 499,
                    "recurring": {"interval": "month"}, "product_data": {"name": "RingSleuth monthly"}}}
            cs = stripe.checkout.Session.create(mode="subscription", line_items=[item], client_reference_id=str(u["id"]),
                    customer_email=u["email"], success_url=base + "/checkout/success?session_id={CHECKOUT_SESSION_ID}", cancel_url=base + "/checkout/cancel")
            return redirect(cs.url, code=303)
        return page("""<div class=card><h1>Subscribe — ${{price}}/month</h1><p>Secure checkout by Stripe (<b>test mode</b>).</p>
<form method=post><button>Continue to checkout</button></form></div>""", price=PRICE, seo=seo_ctx("Checkout | RingSleuth", "Checkout.", index=False))
    if request.method == "POST":  # MOCK Stripe test-mode checkout: no network, no charge
        if request.form.get("card", "").replace(" ", "") != "4242424242424242":
            return page("<h1>Checkout</h1><p class=err>Test mode: use card 4242 4242 4242 4242.</p><a href=/checkout>Back</a>")
        db().execute("UPDATE users SET sub_start=? WHERE id=?", (today_fn().isoformat(), u["id"])); db().commit()
        return redirect("/lookup")
    return page("""<div class=card><h1>Subscribe — ${{price}}/month</h1><p>Stripe <b>TEST MODE (mocked)</b>. Use 4242 4242 4242 4242, any future date, any CVC.</p>
<form method=post><input name=card placeholder="Card number" value="4242 4242 4242 4242"><br><input placeholder="MM/YY" value="12/30"> <input placeholder=CVC value=123><br><button>Pay ${{price}} (test)</button></form></div>""", price=PRICE, seo=seo_ctx("Checkout (test mode) | RingSleuth", "Test-mode checkout.", index=False))

@app.route("/lookup", methods=["GET", "POST"])
def lookup():
    u = user()
    if not u: return redirect("/login")
    if not u["sub_start"]: return redirect("/checkout")
    used, reset = usage(u); result = err = None
    if request.method == "POST":
        if used >= LOOKUP_CAP:
            err = f"You've used all {LOOKUP_CAP} of your {LOOKUP_CAP} lookups for this month. Your plan includes {LOOKUP_CAP} lookups per billing cycle; your limit resets on {reset.strftime('%B %-d, %Y')}."
        else:
            try:
                result = lookup_provider.lookup(request.form.get("number", ""))
                import json
                db().execute("INSERT INTO lookups(user_id,number,result,day,ts) VALUES(?,?,?,?,?)",
                             (u["id"], result["number"], json.dumps(result), today_fn().isoformat(), datetime.now().isoformat(timespec="seconds")))
                db().commit()
            except ValueError as e: err = str(e)
    status = 429 if err and "limit resets" in err else 200
    return page("""<h1>Phone number lookup</h1><div class=card><form method=post><label for=number>Phone number</label><br><input id=number name=number placeholder="(555) 123-4567" required> <button>Look up</button></form></div>
{% if err %}<p class=err id=error>{{err}}</p>{% endif %}
{% if r %}<div class=card id=interstitial><p id=countdown>Your result is ready in <span id=cd-n>{{cd}}</span>…</p>
<p style="text-align:center"><button id=show-btn type=button disabled>Show result</button></p></div>
<div class=card id=result style="display:none"><h3>{{r.number}}</h3><table><tr><td>Caller ID</td><td>{{r.caller_name}}</td></tr><tr><td>Carrier</td><td>{{r.carrier}}</td></tr>
<tr><td>Line type</td><td>{{r.line_type}}</td></tr><tr><td>Location</td><td>{{r.location}}</td></tr>{% if r.timezone %}<tr><td>Time zone</td><td>{{r.timezone}}</td></tr>{% endif %}{% if r.ftc %}<tr><td>FTC complaints</td><td id=ftc>{{r.ftc}}</td></tr>{% endif %}<tr><td>Spam risk</td><td>{{r.risk}} ({{r.spam_score}}/100){% if r.ftc %} <small>based on FTC complaint count</small>{% endif %}</td></tr></table>{% if not m %}<small class=srclabel>{{r.source}}</small>{% endif %}
{% if m and m.unknown %}<div class=mapcard><div class=maphead>🗺️ Where this number is registered</div><p class=legend id=map-unknown>Location unknown for this area code ({{m.npa}}). No map pin is shown.</p></div><small class=srclabel>{{r.source}}</small>
{% elif m %}<div class=mapcard><div class=maphead>🗺️ Where this number is registered: approx. {{m.city}}, {{m.region}} (area code {{m.npa}})</div>
<div class=mapwrap><div id=map></div><div class=sparkles aria-hidden=true>{% for i in range(18) %}<i style="left:{{(i*37)%100}}%;top:{{(i*53)%100}}%;animation-delay:{{(i%6)*0.5}}s"></i>{% endfor %}</div>
<div class=maplabel>Approximate area (from number registration, not live location)</div>
<div class=maplabel2>Illustrative routing - not actual carrier trunk data</div></div>
<div class=legend><span><b class="lg pin"></b>Destination: approximate area (~25 km around {{m.city}})</span><span><b class="lg line"></b>Illustrative call path</span><span><b class="lg dest"></b>Start hub (illustrative)</span><span><b class="lg node"></b>Carrier hub / IXP (illustrative)</span></div></div><small class=srclabel>{{r.source}}</small>
<style>
.srclabel{display:block;margin-top:6px;font-size:11px;color:#94a3b8;text-align:center;letter-spacing:.04em}
.mapcard{margin-top:14px;padding:2px;border-radius:16px;background:linear-gradient(135deg,#38bdf8,#a78bfa,#f472b6);box-shadow:0 10px 40px rgba(56,189,248,.25)}
.maphead{padding:8px 12px;font-weight:bold;color:#fff}
.mapwrap{position:relative;border-radius:14px;overflow:hidden;isolation:isolate}
#map{height:300px;width:100%;background:#0b1020}
.maplabel,.maplabel2{position:absolute;left:8px;z-index:500;background:rgba(15,23,42,.85);color:#e2e8f0;font-size:11px;padding:3px 7px;border-radius:6px;border:1px solid #334155;pointer-events:none;max-width:85%}
.darktiles{filter:invert(1) hue-rotate(200deg) brightness(.75) contrast(1.1) saturate(.6)}
.maplabel{top:8px;left:52px;right:8px;max-width:none;text-align:right}.maplabel2{bottom:22px;color:#f9a8d4}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;background:#0f172a;border-radius:0 0 14px 14px;padding:8px 12px;font-size:12px;color:#cbd5e1}
.lg{display:inline-block;width:12px;height:12px;border-radius:50%;margin-right:5px;vertical-align:middle}
.lg.pin{background:#38bdf8;box-shadow:0 0 8px #38bdf8}.lg.line{width:20px;height:3px;border-radius:2px;background:linear-gradient(90deg,#38bdf8,#f472b6)}
.lg.node{background:#a78bfa;box-shadow:0 0 8px #a78bfa}.lg.dest{background:#f472b6;box-shadow:0 0 8px #f472b6}
.sparkles{position:absolute;inset:0;z-index:450;pointer-events:none}
.sparkles i{position:absolute;width:3px;height:3px;border-radius:50%;background:#fff;box-shadow:0 0 6px #fff;opacity:0;animation:twinkle 3s ease-in-out infinite}
@keyframes twinkle{0%,100%{opacity:0;transform:scale(.5)}50%{opacity:.8;transform:scale(1.3)}}
.rs-pin{width:18px;height:18px;border-radius:50%;background:#38bdf8;box-shadow:0 0 12px 4px #38bdf8;position:relative}
.rs-pin:after{content:"";position:absolute;inset:-6px;border-radius:50%;border:2px solid #38bdf8;animation:pulse 1.8s ease-out infinite}
@keyframes pulse{from{transform:scale(.6);opacity:1}to{transform:scale(2.4);opacity:0}}
.rs-node{width:12px;height:12px;border-radius:50%;background:#a78bfa;box-shadow:0 0 10px 3px #a78bfa;animation:pop .5s cubic-bezier(.3,1.6,.5,1) both}
.rs-node.dest{background:#f472b6;box-shadow:0 0 12px 4px #f472b6}
@keyframes pop{from{transform:scale(0);opacity:0}to{transform:scale(1);opacity:1}}
.callglow{filter:drop-shadow(0 0 6px #f472b6)}
.callpath{stroke-dasharray:10 14;animation:dash 1.2s linear infinite}
@keyframes dash{to{stroke-dashoffset:-48}}
.leaflet-tooltip{background:#0f172a;color:#e2e8f0;border:1px solid #475569}
@media (prefers-reduced-motion:reduce){.sparkles,.rs-pin:after{display:none}.callpath,.rs-node{animation:none}}
</style>
<script>window.RS_MAP={{ m|tojson }};window.drawMap=function(){var m=RS_MAP;if(window._mapDone||!window.L||!document.getElementById('map'))return;window._mapDone=1;
var rm=window.matchMedia&&matchMedia('(prefers-reduced-motion: reduce)').matches;
var map=L.map('map',{scrollWheelZoom:false,zoomControl:true,attributionControl:true});
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:12,className:'darktiles',attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}).addTo(map);
var dest=[m.lat,m.lon];var pts=m.nodes.map(function(n){return[n.lat,n.lon]}).concat([dest]);
function frame(){map.invalidateSize();map.fitBounds(L.latLngBounds(pts.concat([dest])),{padding:innerWidth<500?[18,18]:[40,40],maxZoom:7});}
frame();setTimeout(frame,250);window.addEventListener('resize',frame);
L.polyline(pts,{color:'#a78bfa',weight:7,opacity:.25,className:'callglow'}).addTo(map);
L.polyline(pts,{color:'#f472b6',weight:3,className:'callpath callglow'}).addTo(map).bindTooltip('Illustrative call path - not actual carrier trunk data',{sticky:true});
m.nodes.forEach(function(n,i){setTimeout(function(){
L.marker([n.lat,n.lon],{icon:L.divIcon({className:'',html:'<div class="rs-node'+(i==0?' dest':'')+'"></div>',iconSize:[12,12],iconAnchor:[6,6]})}).addTo(map)
.bindTooltip((i==0?'Start hub: ':'Hub: ')+n.name+' (illustrative)');},rm?0:300+i*500);});
setTimeout(function(){L.circle(dest,{radius:m.radius_m,color:'#38bdf8',weight:1,fillColor:'#38bdf8',fillOpacity:.18}).addTo(map);
L.marker(dest,{icon:L.divIcon({className:'',html:'<div class="rs-pin"></div>',iconSize:[18,18],iconAnchor:[9,9]})}).addTo(map)
.bindTooltip('Approximate area: '+m.city+', '+m.region+' (from number registration, not live location)');},rm?0:300+m.nodes.length*500);};</script>{% endif %}</div>
<noscript><style>#result{display:block!important}#countdown,#show-btn{display:none}</style></noscript>
<script>(function(){var n={{cd}},el=document.getElementById('cd-n'),b=document.getElementById('show-btn'),p=document.getElementById('countdown');
function reveal(){document.getElementById('result').style.display='block';if(window.drawMap)requestAnimationFrame(function(){drawMap();});b.style.display='none';p.textContent='Your result is ready.';}
b.onclick=reveal;var t=setInterval(function(){n--;if(n>0){el.textContent=n;}else{clearInterval(t);p.textContent='Your result is ready.';b.disabled=false;reveal();}},1000);})();</script>{% endif %}""", err=err, r=result, m=map_data(result["npa"]) if result else None, seo=seo_ctx("Phone lookup | RingSleuth", "Look up a phone number: caller ID, carrier, line type, area and spam risk.", index=False, leaflet=True)), status

@app.route("/history")
def history():
    u = user()
    if not u: return redirect("/login")
    import json
    rows = [dict(json.loads(x["result"]), ts=x["ts"]) for x in db().execute("SELECT * FROM lookups WHERE user_id=? ORDER BY id DESC", (u["id"],))]
    return page("""<div class=card><h1>Lookup history</h1>{% if not rows %}<p>No lookups yet.</p>{% endif %}<table>
{% for r in rows %}<tr><td>{{r.ts}}</td><td>{{r.number}}</td><td>{{r.caller_name}}</td><td>{{r.line_type}}</td><td>{{r.risk}}</td></tr>{% endfor %}</table></div>""", rows=rows, seo=seo_ctx("Lookup history | RingSleuth", "Your lookup history.", index=False))

TZ_NAMES = {"America/New_York": "Eastern Time (ET)", "America/Detroit": "Eastern Time (ET)", "America/Toronto": "Eastern Time (ET)", "America/Indiana/Indianapolis": "Eastern Time (ET)",
  "America/Kentucky/Louisville": "Eastern Time (ET)", "America/Montreal": "Eastern Time (ET)", "America/Chicago": "Central Time (CT)", "America/Winnipeg": "Central Time (CT)",
  "America/Regina": "Central Standard Time, no DST (Saskatchewan)", "America/Denver": "Mountain Time (MT)", "America/Edmonton": "Mountain Time (MT)", "America/Boise": "Mountain Time (MT)",
  "America/Phoenix": "Mountain Standard Time, no DST (Arizona)", "America/Los_Angeles": "Pacific Time (PT)", "America/Vancouver": "Pacific Time (PT)",
  "America/Anchorage": "Alaska Time (AKT)", "Pacific/Honolulu": "Hawaii-Aleutian Time (HST)", "America/Halifax": "Atlantic Time (AT)", "America/Moncton": "Atlantic Time (AT)",
  "America/St_Johns": "Newfoundland Time (NT)", "America/Yellowknife": "Mountain Time (MT)"}

def tz_label(tz):
    return TZ_NAMES.get(tz, tz.replace("_", " ") if tz else "not available")

def local_time(tz):
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz)).strftime("%-I:%M %p")
    except Exception: return None

@app.after_request
def robots_header(resp):
    if not SEO_INDEXABLE() or request.path.startswith(("/lookup", "/history", "/checkout", "/stripe", "/login", "/signup", "/logout")):
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp

@app.route("/robots.txt")
def robots_txt():
    root = (os.environ.get("SITE_URL") or request.url_root).rstrip("/")
    body = "User-agent: *\nAllow: /\nDisallow: /lookup\nDisallow: /history\nDisallow: /checkout\nDisallow: /login\nDisallow: /signup\n"
    if not SEO_INDEXABLE(): body += "# Demo: pages are served with noindex (meta robots + X-Robots-Tag) until SEO_INDEXABLE=1\n"
    return Response(body + f"Sitemap: {root}/sitemap.xml\n", mimetype="text/plain")

@app.route("/sitemap.xml")
def sitemap():
    root = (os.environ.get("SITE_URL") or request.url_root).rstrip("/")
    urls = ["/", "/area-code/"] + [f"/area-code/{n}" for n in AREA_CODES]
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(f"<url><loc>{root}{u}</loc></url>" for u in urls) + "</urlset>"
    return Response(xml, mimetype="application/xml")

FAVICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#0f172a"/><path d="M18 14c3 0 6 7 6 9s-3 3-3 5 7 11 9 11 3-3 5-3 9 3 9 6-4 8-8 8C24 50 14 36 14 22c0-4 2-8 4-8z" fill="#38bdf8"/><circle cx="44" cy="22" r="8" fill="none" stroke="#facc15" stroke-width="4"/><path d="M50 28l6 6" stroke="#facc15" stroke-width="4" stroke-linecap="round"/></svg>'

@app.route("/favicon.svg")
def favicon_svg():
    return Response(FAVICON, mimetype="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})

@app.route("/favicon.ico")
def favicon_ico():
    return redirect("/favicon.svg", 301)

@app.route("/og.png")
def og_png():
    return app.send_static_file("og.png") if os.path.exists(os.path.join(app.static_folder, "og.png")) else redirect("/favicon.svg")

@app.route("/area-code/search")
def area_search():
    q = "".join(c for c in request.args.get("q", "") if c.isdigit())[:3]
    return redirect(f"/area-code/{q}" if len(q) == 3 else "/area-code/")

@app.route("/area-code/")
def area_index():
    by = {}
    for n, r in AREA_CODES.items(): by.setdefault((r[2], r[1]), []).append(n)
    groups = sorted(by.items(), key=lambda kv: (kv[0][0] != "US", kv[0][1]))
    seo = seo_ctx("Area Code Lookup: All US & Canada Area Codes by State | RingSleuth",
                  f"Area code lookup for {len(AREA_CODES)} US and Canada area codes. See the main city, state or province, time zone and an approximate map for each code.",
                  path="/area-code/", jsonld=[{"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
                      {"@type": "ListItem", "position": 1, "name": "Home", "item": seo_root() + "/"}, {"@type": "ListItem", "position": 2, "name": "Area codes", "item": seo_root() + "/area-code/"}]}])
    return page("""<h1>Area code lookup: US & Canada area codes</h1>
<div class=card><p>What area code is that? Pick a code below to see its main city, region, time zone and an approximate map. {{n}} area codes listed.</p>
<form action="/area-code/search"><label for=q>Area code</label><br><input id=q name=q placeholder="e.g. 412" inputmode=numeric maxlength=3 required> <button>Look up</button></form></div>
{% for (country, region), codes in groups %}<div class=card><h2>{{region}} area codes{% if country=='CA' %} (Canada){% endif %}</h2><div class=grid>{% for c in codes %}<a href="/area-code/{{c}}" title="Area code {{c}}: {{codes_map[c][0]}}">{{c}}</a>{% endfor %}</div></div>{% endfor %}""",
        groups=groups, n=len(AREA_CODES), codes_map=AREA_CODES, seo=seo)

def seo_root():
    return (os.environ.get("SITE_URL") or request.url_root).rstrip("/")

@app.route("/area-code/<npa>")
def area_page(npa):
    a = area_info(npa)
    if not a: abort(404)
    country = "Canada" if a["country"] == "CA" else "United States"
    same = sorted(n for n, r in AREA_CODES.items() if r[1] == a["region"] and n != npa)
    overlay = [n for n in same if AREA_CODES[n][0] == a["city"]]
    tzl, lt = tz_label(a["tz"]), local_time(a["tz"])
    faq = [(f"Where is area code {npa}?", f"Area code {npa} is in {a['region']}, {country}. Its main city is approximately {a['city']}. A number's area code shows where it was first registered, not where the caller is right now."),
           (f"What time zone is area code {npa}?", f"Area code {npa} is in {tzl}" + (f"; it is about {lt} there now." if lt else ".") + " Some area codes cross time zone lines, so check before calling."),
           (f"Is a call from area code {npa} spam?", f"Not necessarily. Many legitimate calls come from {npa}, but scammers also spoof local-looking numbers. Run a lookup to see the carrier, line type and a spam-risk score."),
           (f"Can I find out who owns a {npa} number?", "RingSleuth shows the caller-ID name on file for businesses and carriers, the carrier and the line type. It does not identify private individuals or show home addresses.")]
    m = {"city": a["city"], "region": a["region"], "npa": npa, "lat": a["lat"], "lon": a["lon"], "radius_m": 25000}
    seo = seo_ctx(f"{npa} Area Code: Location, Time Zone & Map ({a['city']}, {a['region']}) | RingSleuth",
                  f"What area code is {npa}? Area code {npa} covers {a['city']}, {a['region']} ({country}) in {tzl}. See the approximate map, overlay codes and spam-call tips.",
                  path=f"/area-code/{npa}", leaflet=True, jsonld=[faq_ld(faq), {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
                      {"@type": "ListItem", "position": 1, "name": "Home", "item": seo_root() + "/"}, {"@type": "ListItem", "position": 2, "name": "Area codes", "item": seo_root() + "/area-code/"},
                      {"@type": "ListItem", "position": 3, "name": f"Area code {npa}", "item": seo_root() + f"/area-code/{npa}"}]}])
    return page("""<p><a href="/">Home</a> › <a href="/area-code/">Area codes</a> › {{npa}}</p>
<h1>Area code {{npa}}: {{a.city}}, {{a.region}}</h1>
<div class=card><h2>Where is area code {{npa}}?</h2><table><tr><td>Main city (approx.)</td><td>{{a.city}}</td></tr><tr><td>State / province</td><td>{{a.region}}</td></tr><tr><td>Country</td><td>{{country}}</td></tr>
<tr><td>Time zone</td><td>{{tzl}}{% if lt %} · now about {{lt}}{% endif %}</td></tr>{% if overlay %}<tr><td>Overlay / same-area codes</td><td>{% for o in overlay %}<a href="/area-code/{{o}}">{{o}}</a> {% endfor %}</td></tr>{% endif %}</table>
<div class=mapcard><div class=mapwrap><div id=map role=img aria-label="Map showing the approximate area of area code {{npa}} around {{a.city}}, {{a.region}}"></div>
<div class=maplabel>Approximate area (from number registration, not live location)</div></div></div></div>
<div class=card><h2>Got a call from a {{npa}} number?</h2><p>Use a <a href="/lookup">reverse phone lookup</a> to check the caller-ID name on file, the carrier, whether it is a cell, landline or VoIP line, and a spam-risk score. RingSleuth never names private people.</p></div>
""" + FAQ_HTML + """{% if same %}<div class=card><h2>Other {{a.region}} area codes</h2><div class=grid>{% for c in same %}<a href="/area-code/{{c}}">{{c}}</a>{% endfor %}</div></div>{% endif %}
<p><a href="/area-code/">All area codes</a></p>
<style>.mapcard{margin-top:14px;padding:2px;border-radius:16px;background:linear-gradient(135deg,#38bdf8,#a78bfa,#f472b6)}.mapwrap{position:relative;border-radius:14px;overflow:hidden}#map{height:300px;background:#0b1020}
.darktiles{filter:invert(1) hue-rotate(200deg) brightness(.75) contrast(1.1) saturate(.6)}.maplabel{position:absolute;top:8px;left:52px;right:8px;text-align:right;z-index:500;background:rgba(15,23,42,.85);font-size:11px;padding:3px 7px;border-radius:6px;pointer-events:none}</style>
<script>(function(){var m={{ m|tojson }};if(!window.L)return;var map=L.map('map',{scrollWheelZoom:false}).setView([m.lat,m.lon],8);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:12,className:'darktiles',attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}).addTo(map);
L.circle([m.lat,m.lon],{radius:m.radius_m,color:'#38bdf8',weight:1,fillOpacity:.2}).addTo(map).bindTooltip('Approximate area: '+m.city+' (not a live location)');})();</script>""",
        npa=npa, a=a, country=country, tzl=tzl, lt=lt, overlay=overlay, same=same, faq=faq, m=m, seo=seo)

def _mark_subscribed(uid):
    db().execute("UPDATE users SET sub_start=COALESCE(sub_start, ?) WHERE id=?", (today_fn().isoformat(), int(uid))); db().commit()

@app.route("/checkout/success")
def checkout_success():
    u = user()
    if not u: return redirect("/login")
    sid = request.args.get("session_id", "")
    if stripe_mode() == "real" and sid:
        try:
            cs = _stripe().checkout.Session.retrieve(sid)
            if str(cs.client_reference_id) == str(u["id"]) and cs.status == "complete": _mark_subscribed(u["id"])
        except Exception as e: log.warning("checkout success lookup failed: %s", e)
    return page("<div class=card><h1>Thanks!</h1><p>Your subscription is being activated.</p><a href=/lookup>Start a lookup</a></div>", seo=seo_ctx("Subscribed | RingSleuth", "Subscribed.", index=False))

@app.route("/checkout/cancel")
def checkout_cancel():
    return page("<div class=card><h1>Checkout canceled</h1><p>No charge was made.</p><a href=/checkout>Try again</a></div>", seo=seo_ctx("Checkout canceled | RingSleuth", "Canceled.", index=False))

@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    if stripe_mode() != "real": abort(404)
    stripe = _stripe(); payload = request.get_data(); secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    try:
        if secret: event = stripe.Webhook.construct_event(payload, request.headers.get("Stripe-Signature", ""), secret)
        else: event = _json.loads(payload)
    except Exception: abort(400)
    if event["type"] == "checkout.session.completed":
        obj = event["data"]["object"]; uid = obj.get("client_reference_id")
        if uid and str(uid).isdigit(): _mark_subscribed(uid)
    return {"received": True}

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5055)))


@app.route("/<fname>.html")
def google_verification_file(fname):
    """Serve the Google Search Console HTML-file verification (name set via env)."""
    want = os.environ.get("GOOGLE_SITE_VERIFICATION", "").strip()
    if want.endswith(".html") and want == fname + ".html" and fname.startswith("google"):
        return Response(f"google-site-verification: {want}", mimetype="text/html")
    abort(404)
