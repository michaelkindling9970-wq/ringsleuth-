from datetime import date
import pytest, app as A

@pytest.fixture
def client(tmp_path):
    A.app.config.update(DATABASE=str(tmp_path / "t.db"), TESTING=True)
    A.today_fn = lambda: date(2026, 11, 1)
    c = A.app.test_client()
    c.post("/signup", data={"email": "m@x.com", "password": "secret1"})
    c.post("/checkout", data={"card": "4242 4242 4242 4242"})
    c.post("/lookup/gate")
    return c

def do(c, n="2125550100"): return c.post("/lookup", data={"number": n})

def test_cycle_bounds():
    assert A.cycle_bounds(date(2026,11,1), date(2026,11,15)) == (date(2026,11,1), date(2026,12,1))
    assert A.cycle_bounds(date(2026,1,31), date(2026,2,10)) == (date(2026,1,31), date(2026,2,28))
    assert A.cycle_bounds(date(2026,1,31), date(2026,3,5)) == (date(2026,2,28), date(2026,3,31))

def test_counter_and_cap(client):
    for i in range(50): assert do(client).status_code == 200
    r = do(client); body = r.get_data(as_text=True)
    assert r.status_code == 429
    assert "50 of 50 lookups used" in body
    assert "50 lookups per billing cycle" in body and "December 1, 2026" in body

def test_counter_display(client):
    for _ in range(12): do(client)
    assert "12 of 50 lookups used" in client.get("/lookup").get_data(as_text=True)

def test_reset_next_cycle(client):
    for _ in range(50): do(client)
    assert do(client).status_code == 429
    A.today_fn = lambda: date(2026, 11, 30)
    assert do(client).status_code == 429
    A.today_fn = lambda: date(2026, 12, 1)
    r = do(client)
    assert r.status_code == 200 and "1 of 50 lookups used" in r.get_data(as_text=True)
    assert "January 1, 2027" in r.get_data(as_text=True)

def test_blocked_lookup_not_recorded(client):
    for _ in range(52): do(client)
    assert client.get("/history").get_data(as_text=True).count("<tr>") == 50

def test_no_people_search_fields():
    r = A.lookup_provider.lookup("3055550199")
    assert set(r) == {"npa","number","valid","caller_name","carrier","line_type","location","timezone","ftc","ftc_count","spam_score","risk","source"}
    assert r["caller_name"] == "Caller name not available"

def test_countdown_no_ads(client):
    b = do(client).get_data(as_text=True)
    assert "Your result is ready in" in b and 'id=result' in b
    for bad in ("Advertisement", "Ad space", "adsbygoogle", "googlesyndication", "ad-slot"): assert bad not in b
    assert client.get("/ads.txt").status_code == 404

def test_map_markup(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_SITE_VERIFICATION", raising=False)
    b = do(client).get_data(as_text=True)
    for s in ("id=map", "leaflet@1.9.4", "openstreetmap.org/copyright", "darktiles", "prefers-reduced-motion", "class=legend", "Approximate area (from number registration, not live location)",
              "Illustrative routing - not actual carrier trunk data", "Illustrative call path", "radius_m"): assert s in b
    assert "google" not in b.lower()

def test_map_data_approximate():
    for npa, city in (("212","New York"),("215","Philadelphia"),("412","Pittsburgh"),("310","Los Angeles"),("312","Chicago"),("814","Johnstown")):
        m = A.map_data(npa)
        assert city in m["city"] and m["radius_m"] == 25000 and 1 <= len(m["nodes"]) - 1 <= 2
        assert len({n["name"] for n in m["nodes"]}) == len(m["nodes"])
    assert abs(A.map_data("814")["lat"] - 40.33) < 0.05 and abs(A.map_data("814")["lon"] + 78.92) < 0.05
    assert len(A.AREA_CODES) > 300

def test_unknown_area_code(client):
    b = do(client, "9995550100").get_data(as_text=True)
    assert "Location unknown for this area code" in b and "id=map>" not in b

def test_814_lookup(client):
    b = do(client, "8145550123").get_data(as_text=True)
    assert "Johnstown" in b and "area code 814" in b

def test_seo_noindex_default(client, monkeypatch):
    monkeypatch.delenv("SEO_INDEXABLE", raising=False); monkeypatch.setenv("GOOGLE_SITE_VERIFICATION", "abc123")
    r = client.get("/"); b = r.get_data(as_text=True)
    assert r.headers["X-Robots-Tag"] == "noindex, nofollow" and 'content="noindex,nofollow"' in b
    assert '<meta name="google-site-verification" content="abc123">' in b
    assert b.count("<h1") == 1 and "rel=canonical" in b and "og:title" in b and "twitter:card" in b
    assert '"SearchAction"' in b and '"FAQPage"' in b and "DEMO / TEST MODE" in b
    assert "google" not in b.lower().replace("google-site-verification", "")

def test_seo_indexable_flag(client, monkeypatch):
    monkeypatch.setenv("SEO_INDEXABLE", "1")
    r = client.get("/area-code/814"); b = r.get_data(as_text=True)
    assert "X-Robots-Tag" not in r.headers and 'content="index,follow"' in b
    assert client.get("/lookup").headers["X-Robots-Tag"] == "noindex, nofollow"

def test_area_pages(client):
    b = client.get("/area-code/814").get_data(as_text=True)
    assert "<title>814 Area Code" in b and "Johnstown" in b and "Eastern Time" in b and b.count("<h1") == 1 and '"FAQPage"' in b
    assert "Approximate area (from number registration, not live location)" in b
    assert client.get("/area-code/999").status_code == 404
    idx = client.get("/area-code/").get_data(as_text=True)
    assert all(f'href="/area-code/{n}"' in idx for n in A.AREA_CODES)
    sm = client.get("/sitemap.xml").get_data(as_text=True)
    assert sm.count("<loc>") == len(A.AREA_CODES) + 2
    assert "Sitemap:" in client.get("/robots.txt").get_data(as_text=True)
    assert client.get("/favicon.svg").status_code == 200
    assert client.get("/area-code/search?q=412").headers["Location"].endswith("/area-code/412")


# ---------- Stripe switch ----------
from unittest import mock

def test_checkout_off_is_fake(client, monkeypatch):
    for k in ("CHECKOUT_ENABLED", "STRIPE_SECRET_KEY"): monkeypatch.delenv(k, raising=False)
    assert A.stripe_mode() == "fake"
    c = A.app.test_client(); c.post("/signup", data={"email": "f@x.com", "password": "secret1"})
    assert "4242 4242 4242 4242" in c.get("/checkout").get_data(as_text=True)
    assert c.post("/stripe/webhook", data="{}").status_code == 404

def test_live_key_refused(monkeypatch):
    monkeypatch.setenv("CHECKOUT_ENABLED", "1"); monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_abc")
    assert A.stripe_mode() == "fake"
    monkeypatch.setenv("STRIPE_SECRET_KEY", ""); assert A.stripe_mode() == "fake"

def test_checkout_on_mocked(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKOUT_ENABLED", "1"); monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
    monkeypatch.delenv("STRIPE_PRICE_ID", raising=False); monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    A.app.config.update(DATABASE=str(tmp_path / "s.db"), TESTING=True)
    c = A.app.test_client(); c.post("/signup", data={"email": "s@x.com", "password": "secret1"})
    fake = mock.MagicMock(); fake.checkout.Session.create.return_value = mock.Mock(url="https://checkout.stripe.com/c/pay/cs_test_1")
    with mock.patch.object(A, "_stripe", return_value=fake):
        assert "Continue to checkout" in c.get("/checkout").get_data(as_text=True)
        r = c.post("/checkout"); assert r.status_code == 303 and r.headers["Location"].startswith("https://checkout.stripe.com")
        kw = fake.checkout.Session.create.call_args.kwargs
        assert kw["mode"] == "subscription" and kw["line_items"][0]["price_data"]["unit_amount"] == 499
        assert c.post("/lookup", data={"number": "2125550100"}).status_code == 302
        c.post("/lookup/gate")
        ev = {"type": "checkout.session.completed", "data": {"object": {"client_reference_id": kw["client_reference_id"]}}}
        assert c.post("/stripe/webhook", json=ev).status_code == 200
        assert c.post("/lookup", data={"number": "2125550100"}).status_code == 200
    assert c.get("/checkout/cancel").status_code == 200


def _fake_csv(d):
    return ("Company_Phone_Number,Created_Date,Violation_Date,Consumer_City,Consumer_State,Consumer_Area_Code,Subject,Recorded_Message_Or_Robocall\n"
            "8337661884,%s 00:00:43,%s 12:42:00,Secretville,Georgia,678,Medicare & prescriptions,Y\n"
            "8337661884,%s 01:00:00,%s 12:42:00,Hiddentown,Ohio,614,Medicare & prescriptions,N\n"
            ",%s 01:00:00,,X,Ohio,614,Other,Y\n") % ((d.isoformat(),) * 5)

@pytest.fixture
def ftcdb(tmp_path, monkeypatch):
    p = str(tmp_path / "ftc.db")
    A.ftc_data.build(days=3, today=date(2026, 10, 30), fetch=lambda d: _fake_csv(d) if d == date(2026, 10, 30) else None, path=p)
    monkeypatch.setattr(A.ftc_data, "DB_PATH", p)
    return p

def test_ftc_build_minimal_columns(ftcdb):
    import sqlite3
    c = sqlite3.connect(ftcdb)
    assert [x[1] for x in c.execute("PRAGMA table_info(ftc)")] == ["number", "n", "last", "robo", "topic"]
    assert c.execute("SELECT * FROM ftc").fetchall() == [("8337661884", 2, "2026-10-30", 1, "Medicare & prescriptions")]

def test_ftc_result_shown_no_complainant_info(client, ftcdb):
    b = do(client, "8337661884").get_data(as_text=True)
    assert "Reported to the FTC 2 times, last on Oct 30, 2026, robocall yes, topic: Medicare &amp; prescriptions" in b
    for bad in ("Secretville", "Hiddentown", "Georgia", "SAMPLE DATA"): assert bad not in b
    r = A.lookup_provider.lookup("8337661884"); assert r["spam_score"] == A.spam_score(2) and r["ftc_count"] == 2

def test_no_ftc_complaints(client, ftcdb):
    b = do(client, "2125550100").get_data(as_text=True)
    assert "No FTC complaints on file" in b and "Caller name not available" in b
    r = A.lookup_provider.lookup("2125550100"); assert r["spam_score"] == 0 and r["risk"] == "Low"

def test_spam_score_counts_only():
    assert [A.spam_score(n) for n in (0, 1, 3, 7, 100)] == [0, 25, 50, 75, 99]

def test_real_phonenumber_fields(ftcdb):
    r = A.lookup_provider.lookup("8145550123")
    assert "Johnstown" in r["location"] or "PA" in r["location"] or "Pennsylvania" in r["location"]
    assert r["timezone"] == "Eastern Time (ET)"
    t = A.lookup_provider.lookup("8005551234"); assert t["line_type"] == "Toll-free"
    bad = A.lookup_provider.lookup("1235550100"); assert bad["valid"] is False

def test_ftc_stale_logic(ftcdb):
    assert not A.ftc_data.stale(ftcdb)
    assert A.ftc_data.stale(ftcdb + ".missing")

def test_fcra_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    A.app.config.update(DATABASE=str(tmp_path / "g.db"), TESTING=True)
    c = A.app.test_client(); c.post("/signup", data={"email": "g@x.com", "password": "secret1"})
    c.post("/checkout", data={"card": "4242 4242 4242 4242"})
    r = c.post("/lookup", data={"number": "2125550100"}); assert r.status_code == 302 and r.headers["Location"].endswith("/lookup/gate")
    assert A.GATE_NOTICE in c.get("/lookup/gate").get_data(as_text=True)
    c.post("/lookup/gate")
    assert c.post("/lookup", data={"number": "2125550100"}).status_code == 200
    files = list((tmp_path / "gate_records").rglob("*.txt")); assert len(files) == 1
    txt = files[0].read_text(); assert "g@x.com" in txt and A.GATE_NOTICE in txt and A.GATE_BUTTON in txt


# ---------- geo scoping ----------
def _geo_csv(d):
    h = "Company_Phone_Number,Created_Date,Violation_Date,Consumer_City,Consumer_State,Consumer_Area_Code,Subject,Recorded_Message_Or_Robocall\n"
    t = d.isoformat()
    return h + "".join(f"{n},{t} 01:00:00,,X,Ohio,614,Other,{r}\n" for n, r in
                       [("8337661884", "Y"), ("7209534126", "N"), ("2125550111", "N"), ("3105550100", "N"), ("3105550100", "N"), ("3105550100", "N")])

@pytest.fixture
def geodb(tmp_path, monkeypatch):
    p = str(tmp_path / "ftc.db")
    A.ftc_data.build(days=1, today=date(2026, 10, 30), fetch=lambda d: _geo_csv(d) if d == date(2026, 10, 30) else None, path=p)
    monkeypatch.setattr(A.ftc_data, "DB_PATH", p); A.geoloc.clear_caches()
    yield p
    A.geoloc.clear_caches()

def test_geo_area_code_mapping():
    co = A.geoloc.area_codes_for("CO"); assert {"303", "720", "719", "970"} <= set(co) and "212" not in co
    assert {"212", "718", "917"} <= set(A.geoloc.area_codes_for("NY"))
    assert A.geoloc.area_codes_for("XX") == []

def test_client_ip_and_region_cache():
    h = {"X-Forwarded-For": "10.0.0.1, 8.8.8.8, 1.1.1.1"}
    assert A.geoloc.client_ip(h, "127.0.0.1") == "8.8.8.8"
    assert A.geoloc.client_ip({}, "127.0.0.1") is None
    calls = []
    A.geoloc.clear_caches()
    assert A.geoloc.region_for_ip("8.8.8.8", resolver=lambda ip: calls.append(ip) or "CO") == "CO"
    assert A.geoloc.region_for_ip("8.8.8.8", resolver=lambda ip: calls.append(ip) or "NY") == "CO" and len(calls) == 1
    assert all("8.8.8.8" not in k for k in A.geoloc._ip_cache)  # raw IP never used as key

def test_geo_fallback_on_failure(client, geodb, monkeypatch):
    def boom(ip): raise TimeoutError()
    assert A.geoloc.region_for_ip("9.9.9.9", resolver=boom) is None
    monkeypatch.setattr(A.geoloc, "region_for_ip", lambda ip, **k: None)
    monkeypatch.setenv("GEO_STRICT", "1")
    b = do(client, "2125550111").get_data(as_text=True)
    assert "Reported to the FTC 1 time" in b and not A.geoloc._region_cache  # no preload, on-demand

def test_region_cache_holds_only_local_rows(geodb):
    r = A.lookup_provider.lookup("7209534126", region="CO")
    assert r["ftc_count"] == 1 and set(A.geoloc._region_cache["CO"]) == {"7209534126"}

def test_nonlocal_on_demand_when_not_strict(geodb, monkeypatch):
    monkeypatch.delenv("GEO_STRICT", raising=False)
    r = A.lookup_provider.lookup("2125550111", region="CO")
    assert r["ftc_count"] == 1 and not r.get("geo_hidden")

def test_strict_hides_nonlocal_nonspam(geodb, monkeypatch):
    monkeypatch.setenv("GEO_STRICT", "1")
    r = A.lookup_provider.lookup("2125550111", region="CO")
    assert r["geo_hidden"] and "outside your area" in r["source"]
    assert not A.lookup_provider.lookup("7209534126", region="CO").get("geo_hidden")

def test_known_spam_always_served_even_strict(geodb, monkeypatch):
    monkeypatch.setenv("GEO_STRICT", "1")
    robo = A.lookup_provider.lookup("8337661884", region="CO")   # robocall-flagged, out of area
    assert not robo.get("geo_hidden") and "robocall yes" in robo["ftc"] and robo["ftc_count"] == 1
    heavy = A.lookup_provider.lookup("3105550100", region="CO")  # high complaint count, out of area
    assert not heavy.get("geo_hidden") and heavy["ftc_count"] == 3 and heavy["spam_score"] > 0

def test_gate_records_have_no_ip(client, tmp_path, monkeypatch):
    import os
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    client.post("/lookup/gate", environ_base={"REMOTE_ADDR": "8.8.4.4"}, headers={"X-Forwarded-For": "8.8.4.4"})
    for root, _, fs in os.walk(tmp_path):
        for f in fs:
            if f.endswith(".txt"): assert "8.8.4.4" not in open(os.path.join(root, f)).read()

def test_stale_lock_taken_over(tmp_path):
    import os, time
    lf = str(tmp_path / "x.building")
    open(lf, "w").write("999999"); os.utime(lf, (time.time() - 4000,) * 2)
    assert A.ftc_data.acquire_file_lock(lf)
    assert not A.ftc_data.acquire_file_lock(lf)  # held by live pid (us)
