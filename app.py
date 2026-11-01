"""RingSleuth STAGING prototype. Mock data only. No real Twilio/Stripe calls."""
import calendar, hashlib, os, sqlite3
from datetime import date, datetime
from flask import Flask, g, redirect, render_template_string, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

LOOKUP_CAP = 50
PRICE = "4.99"

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

BASE = """<!doctype html><html><head><title>RingSleuth</title><style>
body{font-family:system-ui;max-width:760px;margin:auto;padding:20px;background:#0f172a;color:#e2e8f0}
a{color:#38bdf8}.card{background:#1e293b;padding:18px;border-radius:12px;margin:14px 0}
input,button{padding:9px;border-radius:8px;border:0;margin:4px 0}button{background:#38bdf8;font-weight:bold;cursor:pointer}
.banner{background:#facc15;color:#000;padding:6px;text-align:center;border-radius:6px}.err{background:#7f1d1d;padding:12px;border-radius:8px}
table{width:100%}td{padding:4px}</style></head><body>
<div class=banner>DEMO / TEST MODE — no real charges · sample data only</div>
<h1>📞 RingSleuth</h1><nav><a href="/">Home</a> · {% if u %}<a href="/lookup">Lookup</a> · <a href="/history">History</a> · <a href="/logout">Log out ({{u.email}})</a>{% else %}<a href="/signup">Sign up</a> · <a href="/login">Log in</a>{% endif %}</nav>
{% if u and u.sub_start %}<p><b>{{used}} of 50 lookups used</b> this cycle · resets {{reset}}</p>{% endif %}
{{ body|safe }}</body></html>"""

def page(body, **kw):
    u = user(); used = reset = None
    if u and u["sub_start"]:
        used, r = usage(u); reset = r.strftime("%B %-d, %Y")
    return render_template_string(BASE, body=render_template_string(body, u=u, **kw), u=u, used=used, reset=reset)

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
{% if r %}<div class=card><h3>{{r.number}}</h3><table><tr><td>Caller ID</td><td>{{r.caller_name}}</td></tr><tr><td>Carrier</td><td>{{r.carrier}}</td></tr>
<tr><td>Line type</td><td>{{r.line_type}}</td></tr><tr><td>Location</td><td>{{r.location}}</td></tr><tr><td>Spam risk</td><td>{{r.risk}} ({{r.spam_score}}/100)</td></tr></table><small>{{r.source}}</small></div>{% endif %}""", err=err, r=result), status

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
