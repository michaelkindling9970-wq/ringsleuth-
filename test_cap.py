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
    assert set(r) == {"number","caller_name","carrier","line_type","location","spam_score","risk","source"}

def test_countdown_no_ads(client):
    b = do(client).get_data(as_text=True)
    assert "Your result is ready in" in b and 'id=result' in b
    for bad in ("Advertisement", "Ad space", "adsbygoogle", "googlesyndication", "ad-slot"): assert bad not in b
    assert client.get("/ads.txt").status_code == 404

def test_map_markup(client):
    b = do(client).get_data(as_text=True)
    for s in ("id=map", "leaflet@1.9.4", "openstreetmap.org/copyright", "darktiles", "prefers-reduced-motion", "class=legend", "Approximate area (from number registration, not live location)",
              "Illustrative routing - not actual carrier trunk data", "Illustrative call path", "radius_m"): assert s in b
    assert "google" not in b.lower()

def test_map_data_approximate():
    for city in A.AREA_COORDS:
        m = A.map_data(city + " (area code region)")
        assert m["radius_m"] == 25000 and len(m["nodes"]) == 3 and m["nodes"][-1]["name"] in ("Ashburn, VA", "Chicago, IL", "Dallas, TX")
        assert len({n["name"] for n in m["nodes"]}) == 3
