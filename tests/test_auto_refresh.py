"""2 Nov 2026: Pack Station checks for new orders by itself (weekends included), never prints,
and warns on screen when the last check is old."""
from datetime import datetime, timedelta

from packstation.engine import Engine
from packstation.sources.demo import DemoSource
from tests.test_fixes import write_cfg


def eng_with(tmp_path, extra=""):
    cfg = write_cfg(tmp_path)
    if extra:
        p = tmp_path / "config.toml"
        p.write_text(p.read_text().replace("[app]\n", "[app]\n" + extra + "\n", 1))
        from packstation import config as config_mod
        cfg = config_mod.load(p)
    return Engine(cfg, sources=[DemoSource()])


def test_default_is_hourly_and_due_after_a_weekend(tmp_path):
    eng = eng_with(tmp_path)
    assert eng.auto_refresh_minutes == 60
    assert eng.auto_refresh_due()                     # never checked yet
    eng.scan()
    assert not eng.auto_refresh_due()                 # just checked
    eng.last_scan = datetime.now() - timedelta(days=2, hours=3)   # Mac asleep Sat-Sun
    assert eng.auto_refresh_due()
    assert eng.is_stale()


def test_tick_refreshes_and_never_prints(tmp_path):
    eng = eng_with(tmp_path)
    job = eng.auto_refresh_tick()
    assert job is not None and job.kind == "refresh" and job.title == "Automatic refresh"
    eng.wait(job)
    assert job.state == "done", job.error
    assert job.summary.startswith("Automatic refresh successful")
    assert eng.orders and eng.last_scan is not None and not eng.is_stale()
    assert eng.store.printed() == {}                  # nothing marked printed
    assert all(j.kind == "refresh" for j in eng.jobs)  # no print job was started
    assert eng.auto_refresh_tick() is None            # not due again straight away


def test_skips_while_printing_and_backs_off_after_a_failed_try(tmp_path):
    eng = eng_with(tmp_path)
    eng.scan()
    eng.last_scan = datetime.now() - timedelta(hours=5)
    eng.running = lambda: True                        # a print job is in progress
    assert not eng.auto_refresh_due()
    del eng.running
    eng.last_auto_try = datetime.now() - timedelta(minutes=1)    # tried a minute ago and failed
    assert not eng.auto_refresh_due()
    eng.last_auto_try = datetime.now() - timedelta(minutes=6)
    assert eng.auto_refresh_due()


def test_can_be_switched_off(tmp_path):
    eng = eng_with(tmp_path, "auto_refresh_minutes = 0")
    assert eng.auto_refresh_minutes == 0
    assert not eng.auto_refresh_due() and eng.auto_refresh_tick() is None


def test_state_reports_stale_and_auto(tmp_path):
    from packstation.web import create_app
    eng = eng_with(tmp_path)
    c = create_app(eng).test_client()
    s = c.get("/api/state").get_json()
    assert s["stale"] is False and s["auto_refresh"]["minutes"] == 60
    eng.last_scan = datetime(2026, 10, 26, 12, 40)
    s = c.get("/api/state").get_json()
    assert s["stale"] is True
    html = c.get("/").get_data(as_text=True)
    assert "staleBar" in html and "haven't been checked since" in html
