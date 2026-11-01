"""RingSleuth STAGING prototype. Mock data only. No real Twilio/Stripe calls."""
import calendar, hashlib, os, sqlite3
from datetime import date, datetime
from flask import Flask, g, redirect, render_template_string, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

LOOKUP_CAP = 50
PRICE = "4.99"
COUNTDOWN_SECONDS = 15

# Approximate city centroids for sample data (area-code region, never a person's location)
AREA_COORDS = {"New York, NY": (40.7128, -74.0060), "Miami, FL": (25.7617, -80.1918), "Austin, TX": (30.2672, -97.7431),
               "Denver, CO": (39.7392, -104.9903), "Seattle, WA": (47.6062, -122.3321), "Columbus, OH": (39.9612, -82.9988)}
HUBS = {"Ashburn, VA": (39.0438, -77.4874), "Chicago, IL": (41.8781, -87.6298), "Dallas, TX": (32.7767, -96.7970),
        "Los Angeles, CA": (34.0522, -118.2437), "New York, NY": (40.7128, -74.0060), "Atlanta, GA": (33.7490, -84.3880),
        "Denver, CO": (39.7392, -104.9903), "Seattle, WA": (47.6062, -122.3321)}

def map_data(location):
    import math
    city = location.split(" (")[0]
    if city not in AREA_COORDS: return None
    o = AREA_COORDS[city]
    d = lambda p: math.hypot(p[0] - o[0], (p[1] - o[1]) * math.cos(math.radians(o[0])))
    near = [h for h in sorted(HUBS, key=lambda h: d(HUBS[h])) if HUBS[h] != o][:2]
    dest = next(h for h in ("Ashburn, VA", "Chicago, IL", "Dallas, TX") if h not in near and HUBS[h] != o)
    nodes = [{"name": n, "lat": HUBS[n][0], "lon": HUBS[n][1]} for n in near + [dest]]
    return {"city": city, "lat": o[0], "lon": o[1], "radius_m": 25000, "nodes": nodes}


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

# ---------- mock Twilio Lookup adapter ----------
class MockTwilioLookup:
    """Stand-in for Twilio Lookup v2 (caller_name, line_type_intelligence). Never hits the network."""
    NAMES = ["WIRELESS CALLER", "ACME PLUMBING LLC", "PRIVATE CALLER", "CITY CLINIC", "UNKNOWN", "TELEMARKETING SVC"]
    CARRIERS = ["Verizon Wireless", "T-Mobile USA", "AT&T Mobility", "Bandwidth.com (VoIP)", "Comcast Phone", "Google Voice"]
    TYPES = ["mobile", "landline", "voip"]
    PLACES = ["New York, NY", "Miami, FL", "Austin, TX", "Denver, CO", "Seattle, WA", "Columbus, OH"]

    def lookup(self, number):
        digits = "".join(c for c in number if c.isdigit())
        if len(digits) == 10: digits = "1" + digits
        if len(digits) != 11: raise ValueError("Enter a 10-digit US number.")
        h = int(hashlib.sha256(digits.encode()).hexdigest(), 16)
        lt = self.TYPES[h % 3]
        score = (h >> 8) % 100
        if lt == "voip": score = min(99, score + 20)
        return {"number": f"+{digits[0]} ({digits[1:4]}) {digits[4:7]}-{digits[7:]}",
                "caller_name": self.NAMES[(h >> 16) % 6], "carrier": self.CARRIERS[(h >> 24) % 6],
                "line_type": {"mobile": "Cell", "landline": "Landline", "voip": "VoIP"}[lt],
                "location": self.PLACES[(h >> 32) % 6] + " (area code region)",
                "spam_score": score,
                "risk": "High" if score >= 70 else "Medium" if score >= 40 else "Low",
                "source": "SAMPLE DATA (mock)"}

lookup_provider = MockTwilioLookup()

# ---------- app/db ----------
app = Flask(__name__)
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

BASE = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>RingSleuth</title><style>
body{font-family:system-ui;max-width:760px;margin:auto;padding:20px;background:#0f172a;color:#e2e8f0}
a{color:#38bdf8}.card{background:#1e293b;padding:18px;border-radius:12px;margin:14px 0}
input,button{padding:9px;border-radius:8px;border:0;margin:4px 0}button{background:#38bdf8;font-weight:bold;cursor:pointer}
.banner{background:#facc15;color:#000;padding:6px;text-align:center;border-radius:6px}.err{background:#7f1d1d;padding:12px;border-radius:8px}
table{width:100%}td{padding:4px}
#countdown{font-size:20px;font-weight:bold;text-align:center}</style></head><body>
<div class=banner>DEMO / TEST MODE — no real charges · sample data only</div>
<h1>📞 RingSleuth</h1><nav><a href="/">Home</a> · {% if u %}<a href="/lookup">Lookup</a> · <a href="/history">History</a> · <a href="/logout">Log out ({{u.email}})</a>{% else %}<a href="/signup">Sign up</a> · <a href="/login">Log in</a>{% endif %}</nav>
{% if u and u.sub_start %}<p><b>{{used}} of 50 lookups used</b> this cycle · resets {{reset}}</p>{% endif %}
{{ body|safe }}</body></html>"""

def page(body, **kw):
    u = user(); used = reset = None
    if u and u["sub_start"]:
        used, r = usage(u); reset = r.strftime("%B %-d, %Y")
    return render_template_string(BASE, body=render_template_string(body, u=u, cd=COUNTDOWN_SECONDS, **kw), u=u, used=used, reset=reset)

@app.route("/")
def home():
    return page("""<div class=card><h2>Who's calling? Is it safe to answer?</h2>
<p>Reverse phone lookup: caller ID name, carrier, line type (cell / landline / VoIP), rough location, and a spam-risk score.</p>
<p><i>RingSleuth is a caller-ID & spam check, not a people-search or background check. No addresses, relatives, or records.</i></p></div>
<div class=card><h2>${{price}}/month</h2><ul><li>50 lookups per billing cycle</li><li>Spam-risk scoring</li><li>Lookup history</li><li>Cancel anytime</li></ul>
<a href="/signup"><button>Get started</button></a></div>""", price=PRICE)

@app.route("/signup", methods=["GET", "POST"])
def signup():
    err = ""
    if request.method == "POST":
        try:
            cur = db().execute("INSERT INTO users(email,pw) VALUES(?,?)", (request.form["email"].lower(), generate_password_hash(request.form["password"])))
            db().commit(); session["uid"] = cur.lastrowid; return redirect("/checkout")
        except sqlite3.IntegrityError: err = "That email is already registered."
    return page("""<div class=card><h2>Create account</h2>{% if err %}<p class=err>{{err}}</p>{% endif %}<form method=post>
<input name=email type=email placeholder=Email required><br><input name=password type=password placeholder=Password required minlength=6><br><button>Sign up</button></form></div>""", err=err)

@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    if request.method == "POST":
        r = db().execute("SELECT * FROM users WHERE email=?", (request.form["email"].lower(),)).fetchone()
        if r and check_password_hash(r["pw"], request.form["password"]):
            session["uid"] = r["id"]; return redirect("/lookup" if r["sub_start"] else "/checkout")
        err = "Wrong email or password."
    return page("""<div class=card><h2>Log in</h2>{% if err %}<p class=err>{{err}}</p>{% endif %}<form method=post>
<input name=email type=email placeholder=Email required><br><input name=password type=password placeholder=Password required><br><button>Log in</button></form></div>""", err=err)

@app.route("/logout")
def logout():
    session.clear(); return redirect("/")

@app.route("/checkout", methods=["GET", "POST"])
def checkout():
    u = user()
    if not u: return redirect("/login")
    if request.method == "POST":  # MOCK Stripe test-mode checkout: no network, no charge
        if request.form.get("card", "").replace(" ", "") != "4242424242424242":
            return page("<p class=err>Test mode: use card 4242 4242 4242 4242.</p><a href=/checkout>Back</a>")
        db().execute("UPDATE users SET sub_start=? WHERE id=?", (today_fn().isoformat(), u["id"])); db().commit()
        return redirect("/lookup")
    return page("""<div class=card><h2>Subscribe — ${{price}}/month</h2><p>Stripe <b>TEST MODE (mocked)</b>. Use 4242 4242 4242 4242, any future date, any CVC.</p>
<form method=post><input name=card placeholder="Card number" value="4242 4242 4242 4242"><br><input placeholder="MM/YY" value="12/30"> <input placeholder=CVC value=123><br><button>Pay ${{price}} (test)</button></form></div>""", price=PRICE)

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
    return page("""<div class=card><form method=post><input name=number placeholder="(555) 123-4567" required> <button>Look up</button></form></div>
{% if err %}<p class=err id=error>{{err}}</p>{% endif %}
{% if r %}<div class=card id=interstitial><p id=countdown>Your result is ready in <span id=cd-n>{{cd}}</span>…</p>
<p style="text-align:center"><button id=show-btn type=button disabled>Show result</button></p></div>
<div class=card id=result style="display:none"><h3>{{r.number}}</h3><table><tr><td>Caller ID</td><td>{{r.caller_name}}</td></tr><tr><td>Carrier</td><td>{{r.carrier}}</td></tr>
<tr><td>Line type</td><td>{{r.line_type}}</td></tr><tr><td>Location</td><td>{{r.location}}</td></tr><tr><td>Spam risk</td><td>{{r.risk}} ({{r.spam_score}}/100)</td></tr></table><small>{{r.source}}</small>
{% if m %}<div class=mapcard><div class=maphead>🗺️ Where this number is registered</div>
<div class=mapwrap><div id=map></div><div class=sparkles aria-hidden=true>{% for i in range(18) %}<i style="left:{{(i*37)%100}}%;top:{{(i*53)%100}}%;animation-delay:{{(i%6)*0.5}}s"></i>{% endfor %}</div>
<div class=maplabel>Approximate area (from number registration, not live location)</div>
<div class=maplabel2>Illustrative routing - not actual carrier trunk data</div></div>
<div class=legend><span><b class="lg pin"></b>Approximate area (~25 km around {{m.city}})</span><span><b class="lg line"></b>Illustrative call path</span><span><b class="lg node"></b>Carrier hub / IXP (illustrative)</span><span><b class="lg dest"></b>Destination node</span></div></div>
<link rel=stylesheet href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" crossorigin="">
<style>
.mapcard{margin-top:14px;padding:2px;border-radius:16px;background:linear-gradient(135deg,#38bdf8,#a78bfa,#f472b6);box-shadow:0 10px 40px rgba(56,189,248,.25)}
.maphead{padding:8px 12px;font-weight:bold;color:#fff}
.mapwrap{position:relative;border-radius:14px;overflow:hidden}
#map{height:300px;width:100%;background:#0b1020}
.maplabel,.maplabel2{position:absolute;left:8px;z-index:500;background:rgba(15,23,42,.85);color:#e2e8f0;font-size:11px;padding:3px 7px;border-radius:6px;border:1px solid #334155;pointer-events:none;max-width:85%}
.darktiles{filter:invert(1) hue-rotate(200deg) brightness(.75) contrast(1.1) saturate(.6)}
.maplabel{top:8px;left:auto;right:8px}.maplabel2{bottom:22px;color:#f9a8d4}
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
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" crossorigin=""></script>
<script>window.RS_MAP={{ m|tojson }};window.drawMap=function(){var m=RS_MAP;if(window._mapDone||!window.L)return;window._mapDone=1;
var rm=window.matchMedia&&matchMedia('(prefers-reduced-motion: reduce)').matches;
var map=L.map('map',{scrollWheelZoom:false,zoomControl:true});
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:12,className:'darktiles',attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}).addTo(map);
var o=[m.lat,m.lon];L.circle(o,{radius:m.radius_m,color:'#38bdf8',weight:1,fillColor:'#38bdf8',fillOpacity:.18}).addTo(map);
L.marker(o,{icon:L.divIcon({className:'',html:'<div class=rs-pin></div>',iconSize:[18,18]})}).addTo(map).bindTooltip('Approximate area (from number registration, not live location)');
var pts=[o].concat(m.nodes.map(function(n){return[n.lat,n.lon]}));
L.polyline(pts,{color:'#a78bfa',weight:7,opacity:.25,className:'callglow'}).addTo(map);
L.polyline(pts,{color:'#f472b6',weight:3,className:'callpath callglow'}).addTo(map).bindTooltip('Illustrative call path - not actual carrier trunk data',{sticky:true});
m.nodes.forEach(function(n,i){var last=i==m.nodes.length-1;setTimeout(function(){
L.marker([n.lat,n.lon],{icon:L.divIcon({className:'',html:'<div class="rs-node'+(last?' dest':'')+'"></div>',iconSize:[12,12]})}).addTo(map)
.bindTooltip((last?'Destination node: ':'Hub: ')+n.name+' (illustrative)');},rm?0:400+i*500);});
map.fitBounds(L.latLngBounds(pts).pad(.25));};</script>{% endif %}</div>
<noscript><style>#result{display:block!important}#countdown,#show-btn{display:none}</style></noscript>
<script>(function(){var n={{cd}},el=document.getElementById('cd-n'),b=document.getElementById('show-btn'),p=document.getElementById('countdown');
function reveal(){document.getElementById('result').style.display='block';if(window.drawMap)setTimeout(drawMap,50);b.style.display='none';p.textContent='Your result is ready.';}
b.onclick=reveal;var t=setInterval(function(){n--;if(n>0){el.textContent=n;}else{clearInterval(t);p.textContent='Your result is ready.';b.disabled=false;reveal();}},1000);})();</script>{% endif %}""", err=err, r=result, m=map_data(result["location"]) if result else None), status

@app.route("/history")
def history():
    u = user()
    if not u: return redirect("/login")
    import json
    rows = [dict(json.loads(x["result"]), ts=x["ts"]) for x in db().execute("SELECT * FROM lookups WHERE user_id=? ORDER BY id DESC", (u["id"],))]
    return page("""<div class=card><h2>Lookup history</h2>{% if not rows %}<p>No lookups yet.</p>{% endif %}<table>
{% for r in rows %}<tr><td>{{r.ts}}</td><td>{{r.number}}</td><td>{{r.caller_name}}</td><td>{{r.line_type}}</td><td>{{r.risk}}</td></tr>{% endfor %}</table></div>""", rows=rows)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5055)))
