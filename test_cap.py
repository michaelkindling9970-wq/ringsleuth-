from datetime import date
import pytest, app as A

@pytest.fixture
def client(tmp_path):
    A.app.config.update(DATABASE=str(tmp_path / "t.db"), TESTING=True)
    A.today_fn = lambda: date(2026, 11, 1)
    c = A.app.test_client()
    c.post("/signup", data={"email": "m@x.com", "password": "secret1"})
    c.post("/checkout", data={"card": "4242 4242 4242 4242"})
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
    assert set(r) == {"npa","number","caller_name","carrier","line_type","location","spam_score","risk","source"}

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
        ev = {"type": "checkout.session.completed", "data": {"object": {"client_reference_id": kw["client_reference_id"]}}}
        assert c.post("/stripe/webhook", json=ev).status_code == 200
        assert c.post("/lookup", data={"number": "2125550100"}).status_code == 200
    assert c.get("/checkout/cancel").status_code == 200
