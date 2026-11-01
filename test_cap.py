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

def test_countdown_and_placeholder(client, monkeypatch):
    monkeypatch.delenv("ADSENSE_CLIENT", raising=False); monkeypatch.delenv("ADSENSE_SLOT", raising=False)
    b = do(client).get_data(as_text=True)
    assert "Your result is ready in" in b and 'id=ad-slot' in b and "Ad space" in b
    assert "adsbygoogle" not in b and "watch" not in b.lower()
    assert client.get("/ads.txt").status_code == 404

def test_adsense_enabled(client, monkeypatch):
    monkeypatch.setenv("ADSENSE_CLIENT", "ca-pub-1234567890123456"); monkeypatch.setenv("ADSENSE_SLOT", "9876543210")
    b = do(client).get_data(as_text=True)
    assert 'data-ad-client="ca-pub-1234567890123456"' in b and 'data-ad-slot="9876543210"' in b and "Ad space" not in b
    t = client.get("/ads.txt").get_data(as_text=True)
    assert t.strip() == "google.com, pub-1234567890123456, DIRECT, f08c47fec0942fa0"
