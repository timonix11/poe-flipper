"""Stash window data layer: config, rate limits, stash download, prices,
row building.  No network — every request goes through a fake `get`."""
import json
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import stash_data as sd

SID = "0123456789abcdef0123456789abcdef"     # made up, not a real session

LIMITS = {  # headers exactly as the site sends them with a session
    "X-Rate-Limit-Policy": "backend-item-request-limit",
    "X-Rate-Limit-Rules": "Account,Ip",
    "X-Rate-Limit-Account": "30:60:60,100:1800:600",
    "X-Rate-Limit-Account-State": "1:60:0,1:1800:0",
    "X-Rate-Limit-Ip": "45:60:120,180:1800:600",
    "X-Rate-Limit-Ip-State": "1:60:0,3:1800:0",
}

TABS = [
    {"n": "Curr", "i": 0, "id": "aaa", "type": "CurrencyStash"},
    {"n": "shop", "i": 1, "id": "bbb", "type": "QuadStash"},
    {"n": "9", "i": 2, "id": "ccc", "type": "FragmentStash"},
    {"n": "16", "i": 3, "id": "ddd", "type": "MapStash"},
]
ITEMS = {
    0: [{"typeLine": "Chaos Orb", "stackSize": 4312},
        {"typeLine": "Divine Orb", "stackSize": 7},
        {"typeLine": "Stacked Deck", "stackSize": 850},
        {"typeLine": "Viridian Jewel"},
        {"typeLine": "", "baseType": ""}],
    1: [{"typeLine": "Stacked Deck", "stackSize": 150}],
    2: [{"typeLine": "Cursed Poppet", "stackSize": 3}],
}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = sd.DATA_DIR
        sd.DATA_DIR = Path(self._tmp.name)
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        sd.DATA_DIR = self._old
        self._tmp.cleanup()

    def write(self, name, data, encoding="utf-8"):
        text = data if isinstance(data, str) else json.dumps(data)
        (self.dir / name).write_text(text, encoding=encoding)

    def config(self, **over):
        cfg = {"account": "Tester#1234", "league": "Covens", "poesessid": SID}
        cfg.update(over)
        self.write("stash_config.json", cfg)


class FakeSite:
    """Stands in for http_get on the stash endpoint."""

    def __init__(self, limits=None, status=200):
        self.calls = []
        self.limits = dict(limits or LIMITS)
        self.status = status

    def __call__(self, url, headers, timeout=30):
        self.calls.append((url, dict(headers)))
        if self.status != 200:
            return self.status, self.limits, json.dumps(
                {"error": {"code": 6, "message": "Forbidden"}}).encode()
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        index = int(q["tabIndex"][0])
        payload = {"numTabs": len(TABS), "items": ITEMS.get(index, [])}
        if q["tabs"][0] == "1":
            payload["tabs"] = TABS
        return 200, self.limits, json.dumps(payload).encode()

    def indexes(self):
        return [int(urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)
                    ["tabIndex"][0]) for u, _h in self.calls]


class TestLimits(unittest.TestCase):
    def test_real_headers(self):
        rules = sd.parse_limits(LIMITS)
        self.assertEqual(len(rules), 4)
        self.assertEqual(rules[0], {"scope": "Account", "max": 30,
                                    "period": 60, "penalty": 60, "hits": 1,
                                    "restricted": 0})
        self.assertEqual(sd.pace(rules), 2.5)       # 30 per 60 s, with margin
        self.assertEqual(sd.budget(rules), 99)      # 100 per half hour, 1 used

    def test_nearly_full_short_window_waits_it_out(self):
        rules = sd.parse_limits(dict(
            LIMITS, **{"X-Rate-Limit-Account-State": "28:60:0,40:1800:0"}))
        self.assertEqual(sd.pace(rules), 60.0)
        self.assertEqual(sd.pace(rules, remaining=1), 60.0)
        self.assertEqual(sd.budget(rules), 60)

    def test_small_refresh_goes_out_in_one_burst(self):
        rules = sd.parse_limits(LIMITS)             # 1 of 30 used this minute
        self.assertEqual(sd.pace(rules, remaining=8), 0.0)
        self.assertEqual(sd.pace(rules, remaining=26), 0.0)
        # more than the minute has room for (30 - 1 used - 3 spare): spread
        self.assertEqual(sd.pace(rules, remaining=27), 2.5)
        # a second refresh right after a big one no longer fits either
        busy = sd.parse_limits(dict(
            LIMITS, **{"X-Rate-Limit-Account-State": "20:60:0,20:1800:0"}))
        self.assertEqual(sd.pace(busy, remaining=8), 2.5)
        self.assertEqual(sd.pace(busy, remaining=7), 0.0)

    def test_no_headers(self):
        self.assertEqual(sd.parse_limits({}), [])
        self.assertEqual(sd.pace([]), 1.0)
        self.assertEqual(sd.pace([], remaining=3), 1.0)
        self.assertIsNone(sd.budget([]))

    def test_header_names_are_case_insensitive(self):
        low = {k.lower(): v for k, v in LIMITS.items()}
        self.assertEqual(sd.parse_limits(low), sd.parse_limits(LIMITS))


class TestConfig(Base):
    def test_missing_file(self):
        with self.assertRaises(sd.StashError):
            sd.load_config()

    def test_placeholders_are_rejected(self):
        self.write("stash_config.json", {"account": "YourAccount#0000",
                                         "league": "Covens",
                                         "poesessid": "PASTE_HERE"})
        with self.assertRaises(sd.StashError) as cm:
            sd.load_config()
        self.assertIn("account", str(cm.exception))
        self.assertIn("poesessid", str(cm.exception))

    def test_wrong_length_never_reaches_the_site(self):
        self.config(poesessid="abc123")
        with self.assertRaises(sd.StashError) as cm:
            sd.load_config()
        self.assertIn("32", str(cm.exception))
        self.assertNotIn("abc123", str(cm.exception))

    def test_pasted_with_prefix_and_bom(self):
        self.write("stash_config.json", json.dumps(
            {"account": " Tester#1234 ", "league": "Covens",
             "poesessid": "POESESSID=" + SID}), encoding="utf-8-sig")
        cfg = sd.load_config()
        self.assertEqual(cfg["poesessid"], SID)
        self.assertEqual(cfg["account"], "Tester#1234")


class TestFetch(Base):
    def fetch(self, site, state=None):
        self.config()
        state = state or sd.default_state()
        self.sleeps = []
        return sd.fetch_stash(sd.load_config(),
                              lambda t: sd.tab_enabled(state, t), get=site,
                              sleep=self.sleeps.append, now=lambda: 1000.0)

    def test_only_enabled_tabs_are_requested(self):
        site = FakeSite()
        cache = self.fetch(site)
        self.assertEqual(site.indexes(), [0, 2])    # shop + maps left alone
        self.assertEqual(self.sleeps, [])           # fits the minute: no wait
        for url, headers in site.calls:
            self.assertTrue(url.startswith(sd.STASH_ENDPOINT + "?"))
            self.assertIn("accountName=Tester%231234", url)
            self.assertEqual(headers["Cookie"], "POESESSID=" + SID)
        self.assertEqual([(t["name"], t["fetched"]) for t in cache["tabs"]],
                         [("Curr", True), ("shop", False), ("9", True),
                          ("16", False)])
        self.assertEqual(cache["tabs"][0]["items"],
                         [{"name": "Chaos Orb", "qty": 4312},
                          {"name": "Divine Orb", "qty": 7},
                          {"name": "Stacked Deck", "qty": 850},
                          {"name": "Viridian Jewel", "qty": 1}])
        self.assertEqual(cache["fetched_at"], 1000.0)

    def test_cache_is_saved_without_the_cookie(self):
        cache = self.fetch(FakeSite())
        saved = (self.dir / "stash_cache.json").read_text(encoding="utf-8")
        self.assertEqual(json.loads(saved), cache)
        self.assertEqual(sd.load_stash_cache(), cache)
        self.assertNotIn(SID, saved)
        self.assertNotIn("Tester", saved)

    def test_requests_are_spaced_when_the_minute_is_nearly_used(self):
        site = FakeSite(dict(
            LIMITS, **{"X-Rate-Limit-Account-State": "27:60:0,27:1800:0"}))
        self.fetch(site)
        self.assertEqual(site.indexes(), [0, 2])
        # 27 of 30 used: the spare margin is all that is left, so wait
        self.assertEqual(self.sleeps, [2.5])

    def test_user_enabled_tab_is_requested(self):
        state = sd.default_state()
        state["tabs"] = {"bbb": True, "ccc": False}
        site = FakeSite()
        self.fetch(site, state)
        self.assertEqual(site.indexes(), [0, 1])

    def test_rejected_session(self):
        with self.assertRaises(sd.StashError) as cm:
            self.fetch(FakeSite(status=403))
        self.assertIn("cookie", str(cm.exception))
        self.assertNotIn(SID, str(cm.exception))
        self.assertFalse((self.dir / "stash_cache.json").exists())

    def test_long_window_budget_stops_before_spending_it(self):
        site = FakeSite(dict(
            LIMITS, **{"X-Rate-Limit-Account-State": "1:60:0,98:1800:0"}))
        with self.assertRaises(sd.StashError) as cm:
            self.fetch(site)
        self.assertIn("rate limit", str(cm.exception))
        self.assertEqual(site.indexes(), [0])       # nothing after the list

    def test_network_down(self):
        def dead(url, headers, timeout=30):
            raise OSError("no route")
        with self.assertRaises(sd.StashError):
            self.fetch(dead)


def overview(pairs, core=True):
    """Minimal exchange-overview payload: pairs of (name, chaos value)."""
    payload = {"items": [], "lines": []}
    if core:
        payload["core"] = {"primary": "chaos", "items": [
            {"id": "chaos", "name": "Chaos Orb"},
            {"id": "divine", "name": "Divine Orb"}]}
    for name, value in pairs:
        ident = name.lower().replace(" ", "-")
        if ident not in ("chaos-orb", "divine-orb"):
            payload["items"].append({"id": ident, "name": name})
        else:
            ident = ident.split("-")[0]
        payload["lines"].append({"id": ident, "primaryValue": value,
                                 "volumePrimaryValue": 100.0})
    return payload


class FakeNinja:
    def __init__(self):
        self.calls = []
        self.mode = "ok"
        self.fail = set()

    def __call__(self, url, headers, timeout=30):
        kind = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["type"][0]
        self.calls.append((kind, dict(headers)))
        if self.mode == "down":
            raise OSError("offline")
        if kind in self.fail:
            return 500, {}, b"oops"
        if self.mode == "unchanged":
            return 304, {}, b""
        pairs = {"Currency": [("Chaos Orb", 1), ("Divine Orb", 214.5),
                              ("Stacked Deck", 3.4)],
                 "Fragment": [("Cursed Poppet", 915.9)]}.get(kind, [])
        return 200, {"ETag": f'"{kind}-v1"'}, json.dumps(overview(pairs)).encode()


class TestPrices(Base):
    def load(self, ninja, t, force=False):
        return sd.load_prices("Covens", force=force, get=ninja,
                              sleep=lambda s: None, now=lambda: t)

    def test_parse_overview(self):
        payload = overview([("Divine Orb", 214.5), ("Stacked Deck", 3.4),
                            ("Dead Item", 0)])
        payload["lines"].append({"id": "ghost", "primaryValue": 5})
        self.assertEqual(sd.parse_overview(payload),
                         {"Divine Orb": [214.5, 100.0],
                          "Stacked Deck": [3.4, 100.0]})

    def test_fetch_then_cache_then_revalidate(self):
        ninja = FakeNinja()
        cache = self.load(ninja, 10_000)
        self.assertEqual([k for k, _h in ninja.calls], list(sd.NINJA_TYPES))
        table = sd.price_table(cache)
        self.assertEqual(table["Stacked Deck"], (3.4, 100.0, "Currency"))
        self.assertEqual(table["Cursed Poppet"][2], "Fragment")
        self.assertEqual(sd.divine_price(table), 214.5)

        ninja.calls.clear()
        self.load(ninja, 10_000 + sd.PRICE_TTL - 1)             # still fresh
        self.load(ninja, 10_000 + sd.PRICE_MIN_AGE - 1, force=True)
        self.assertEqual(ninja.calls, [])

        ninja.mode = "unchanged"                                # stale: 304s
        cache = self.load(ninja, 10_000 + sd.PRICE_TTL + 1)
        sent = dict(ninja.calls)
        self.assertEqual(sent["Currency"]["If-None-Match"], '"Currency-v1"')
        self.assertEqual(sd.price_table(cache)["Stacked Deck"][0], 3.4)
        self.assertEqual(cache["fetched_at"], 10_000 + sd.PRICE_TTL + 1)

    def test_failed_category_keeps_old_prices(self):
        ninja = FakeNinja()
        self.load(ninja, 10_000)
        ninja.fail = {"Fragment"}
        cache = self.load(ninja, 20_000, force=True)
        self.assertEqual(cache["errors"], ["Fragment"])
        self.assertEqual(sd.price_table(cache)["Cursed Poppet"][0], 915.9)

    def test_offline_without_cache_is_an_error(self):
        ninja = FakeNinja()
        ninja.mode = "down"
        with self.assertRaises(sd.StashError):
            self.load(ninja, 10_000)

    def test_offline_with_cache_keeps_it_and_its_age(self):
        ninja = FakeNinja()
        self.load(ninja, 10_000)
        ninja.mode = "down"
        cache = self.load(ninja, 50_000)
        self.assertEqual(cache["fetched_at"], 10_000)   # still the old data
        self.assertEqual(len(cache["errors"]), len(sd.NINJA_TYPES))
        self.assertEqual(sd.price_table(cache)["Stacked Deck"][0], 3.4)

    def test_other_league_starts_clean(self):
        ninja = FakeNinja()
        self.load(ninja, 10_000)
        ninja.calls.clear()
        sd.load_prices("Standard", get=ninja, sleep=lambda s: None,
                       now=lambda: 10_001)
        self.assertEqual(len(ninja.calls), len(sd.NINJA_TYPES))
        self.assertNotIn("If-None-Match", ninja.calls[0][1])


class TestRows(Base):
    CACHE = {"version": 1, "fetched_at": 1.0, "league": "Covens", "tabs": [
        dict(sd.tab_entry(TABS[0], 0), fetched=True, items=[
            {"name": "Chaos Orb", "qty": 4312}, {"name": "Divine Orb", "qty": 7},
            {"name": "Stacked Deck", "qty": 850},
            {"name": "Viridian Jewel", "qty": 1}]),
        dict(sd.tab_entry(TABS[1], 1), fetched=True, items=[
            {"name": "Stacked Deck", "qty": 150}]),
        dict(sd.tab_entry(TABS[2], 2), fetched=True, items=[
            {"name": "Cursed Poppet", "qty": 3},
            {"name": "Mystery Shard", "qty": 40}]),
    ]}
    TABLE = {"Chaos Orb": (1.0, 0.0, "Currency"),
             "Divine Orb": (214.5, 9.0, "Currency"),
             "Stacked Deck": (3.4, 5.0, "Currency"),
             "Cursed Poppet": (915.9, 2.0, "Fragment")}

    def test_shop_tab_is_off_until_enabled(self):
        state = sd.default_state()
        self.assertEqual(sd.aggregate(self.CACHE, state)["Stacked Deck"],
                         {"qty": 850, "tabs": ["Curr"]})
        state["tabs"]["bbb"] = True
        self.assertEqual(sd.aggregate(self.CACHE, state)["Stacked Deck"],
                         {"qty": 1000, "tabs": ["Curr", "shop"]})

    def test_rows_sorted_expensive_first(self):
        rows, unpriced = sd.build_rows(self.CACHE, self.TABLE,
                                       sd.default_state())
        self.assertEqual([r["name"] for r in rows],
                         ["Chaos Orb", "Stacked Deck", "Cursed Poppet",
                          "Divine Orb"])
        self.assertEqual([round(r["total"], 1) for r in rows],
                         [4312.0, 2890.0, 2747.7, 1501.5])
        by = {r["name"]: r for r in rows}
        self.assertTrue(by["Chaos Orb"]["fixed"])       # nothing to sell it for
        self.assertFalse(by["Chaos Orb"]["sell"])
        self.assertFalse(by["Divine Orb"]["sell"])      # kept by default
        self.assertTrue(by["Stacked Deck"]["sell"])
        self.assertEqual([(r["name"], r["qty"]) for r in unpriced],
                         [("Mystery Shard", 40), ("Viridian Jewel", 1)])

    def test_summary_counts_only_ticked_rows(self):
        state = sd.default_state()
        state["unchecked"].append("Cursed Poppet")
        rows, _ = sd.build_rows(self.CACHE, self.TABLE, state)
        s = sd.summarize(rows)
        self.assertAlmostEqual(s["all"], 4312 + 2890 + 2747.7 + 1501.5)
        self.assertAlmostEqual(s["selling"], 2890.0)
        self.assertEqual(s["selling_count"], 1)
        self.assertEqual(s["chaos"], 4312)
        self.assertEqual(s["small_count"], 0)

    def test_small_change_is_left_out_of_the_sell_total(self):
        rows, _ = sd.build_rows(self.CACHE, self.TABLE, sd.default_state())
        s = sd.summarize(rows, min_total=2800)
        # Cursed Poppet (2747.7) and the kept Divines (1501.5) fall under it
        self.assertAlmostEqual(s["selling"], 2890.0)
        self.assertEqual(s["selling_count"], 1)
        self.assertEqual(s["small_count"], 2)
        self.assertAlmostEqual(s["small"], 2747.7 + 1501.5)
        # chaos itself is never "small change", whatever the threshold
        self.assertFalse(sd.is_small(rows[0], 10 ** 9))

    def test_league_comes_from_config_then_cache(self):
        self.assertIsNone(sd.config_league())
        self.write("stash_cache.json", self.CACHE)
        self.assertEqual(sd.config_league(), "Covens")
        self.write("stash_config.json", {"league": "Standard",
                                         "poesessid": "PASTE_HERE"})
        self.assertEqual(sd.config_league(), "Standard")

    def test_state_survives_restart(self):
        state = sd.default_state()
        state["unchecked"] += ["Cursed Poppet"]
        state["tabs"]["bbb"] = True
        state["min_total"] = 5
        sd.save_state(state)
        again = sd.load_state()
        self.assertEqual(again["unchecked"],
                         list(sd.DEFAULT_UNCHECKED) + ["Cursed Poppet"])
        self.assertIn("Divine Orb", sd.DEFAULT_UNCHECKED)
        self.assertIn("Mirror of Kalandra", sd.DEFAULT_UNCHECKED)
        self.assertEqual(again["tabs"], {"bbb": True})
        self.assertEqual(again["min_total"], 5.0)

    def test_broken_state_file_falls_back_to_defaults(self):
        self.write("stash_state.json", "{not json")
        self.assertEqual(sd.load_state(), sd.default_state())


if __name__ == "__main__":
    unittest.main()
