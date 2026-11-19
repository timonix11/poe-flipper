"""Stash contents + currency-exchange prices for the stash window (no UI).

Sources:
  * stash  - the website's own stash endpoint, authenticated with the user's
             session cookie from stash_config.json.  Unofficial: one request
             per tab, paced to the limits the site reports in its headers.
  * prices - poe.ninja's currency-exchange overview (their supported public
             endpoint), cached on disk and revalidated with ETags.

Nothing here talks to the game client.  The cookie is only ever sent to
www.pathofexile.com and is never logged or written to any file.
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR      # config / caches / state live here (tests repoint it)

STASH_ENDPOINT = "https://www.pathofexile.com/character-window/get-stash-items"
NINJA_BASE = "https://poe.ninja/poe1/api/economy"
USER_AGENT = "poe-stash-viewer/0.1 (personal tool)"

# every category poe.ninja documents for the PoE1 exchange overview
NINJA_TYPES = ("Currency", "Fragment", "Scarab", "Essence", "Fossil",
               "Resonator", "Oil", "DeliriumOrb", "DivinationCard", "Tattoo",
               "Omen", "Artifact", "Runegraft", "AllflameEmber", "Astrolabe",
               "Thread", "DjinnCoin", "Ducat", "EnshroudingCrystal")
PRICE_TTL = 30 * 60         # poe.ninja recomputes PoE1 overviews every ~15 min
PRICE_MIN_AGE = 5 * 60      # even a forced refresh waits this long (their
                            # responses are HTTP-cached for about that anyway)

TARGET = "Chaos Orb"                # what everything gets sold for
# never sold unless the user ticks them: divines are what the chaos gets
# converted into, and nobody wants a Mirror dumped by a default tick box
DEFAULT_UNCHECKED = ("Divine Orb", "Mirror of Kalandra")
# tab types that hold exchange goods; other tabs start switched off
SELL_TAB_TYPES = frozenset({
    "CurrencyStash", "FragmentStash", "EssenceStash", "DelveStash",
    "BlightStash", "DeliriumStash", "UltimatumStash", "DivinationCardStash",
})


class StashError(Exception):
    """A failure whose message is meant to be shown to the user."""


# ------------------------------------------------------------- plumbing ---

def _path(name):
    return DATA_DIR / name


def _read_json(name):
    try:
        raw = _path(name).read_bytes()
    except OSError:
        return None
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return json.loads(raw.decode(enc))
        except (UnicodeDecodeError, ValueError):
            continue
    return None


def _write_json(name, data):
    tmp = _path(name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_path(name))


def http_get(url, headers, timeout=30):
    """-> (status, headers, body).  HTTP errors come back as a status; only a
    dead network raises (OSError)."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def _hget(headers, name):
    """Case-insensitive header lookup that also works on a plain dict."""
    low = name.lower()
    for key in headers.keys():
        if key.lower() == low:
            return headers[key]
    return None


def _user_agent(cfg=None):
    contact = str((cfg or {}).get("contact", "")).strip()
    return f"{USER_AGENT} {contact}".strip()


# --------------------------------------------------------------- config ---

def load_config():
    """stash_config.json -> {account, league, poesessid, contact}.
    Raises StashError saying exactly what to fix."""
    cfg = _read_json("stash_config.json")
    if not isinstance(cfg, dict):
        raise StashError(
            "stash_config.json is missing or broken: it needs three quoted "
            "values - account, league, poesessid.")
    account = str(cfg.get("account", "")).strip()
    league = str(cfg.get("league", "")).strip()
    sid = str(cfg.get("poesessid", "")).strip().strip('"').strip()
    if sid.upper().startswith("POESESSID="):
        sid = sid.split("=", 1)[1].strip()
    problems = []
    if not account or account.startswith("YourAccount"):
        problems.append("account - the account name with its #1234 part")
    if not league:
        problems.append("league - the league name")
    if not sid or sid == "PASTE_HERE":
        problems.append("poesessid - the value of the POESESSID cookie")
    elif not re.fullmatch(r"[0-9a-fA-F]{32}", sid):
        problems.append("poesessid - must be exactly 32 characters "
                        f"(digits and letters a-f), it has {len(sid)}")
    if problems:
        raise StashError("Fix stash_config.json: " + "; ".join(problems))
    return {"account": account, "league": league, "poesessid": sid,
            "contact": str(cfg.get("contact", "")).strip()}


def config_league():
    """League to price against: the one named in stash_config.json (no valid
    cookie needed for that), else the one the cached stash came from."""
    cfg = _read_json("stash_config.json")
    league = str(cfg.get("league", "")).strip() if isinstance(cfg, dict) else ""
    if league:
        return league
    cache = _read_json("stash_cache.json")
    return (cache.get("league") if isinstance(cache, dict) else None) or None


# ---------------------------------------------------------- rate limits ---

def parse_limits(headers):
    """X-Rate-Limit-* headers -> [{scope, max, period, penalty, hits,
    restricted}], one entry per rule window."""
    out = []
    scopes = [s.strip() for s in (_hget(headers, "X-Rate-Limit-Rules")
                                  or "").split(",") if s.strip()]
    for scope in scopes:
        states = {}
        for part in (_hget(headers, f"X-Rate-Limit-{scope}-State")
                     or "").split(","):
            bits = [b.strip() for b in part.split(":")]
            if len(bits) == 3 and all(b.isdigit() for b in bits):
                states[int(bits[1])] = (int(bits[0]), int(bits[2]))
        for part in (_hget(headers, f"X-Rate-Limit-{scope}") or "").split(","):
            bits = [b.strip() for b in part.split(":")]
            if len(bits) != 3 or not all(b.isdigit() for b in bits):
                continue
            mx, period, penalty = (int(b) for b in bits)
            hits, restricted = states.get(period, (0, 0))
            out.append({"scope": scope, "max": mx, "period": period,
                        "penalty": penalty, "hits": hits,
                        "restricted": restricted})
    return out


BURST_MARGIN = 3    # requests left untouched in a short window


def pace(limits, remaining=None):
    """Seconds to wait before the next request.

    `remaining` is how many requests this refresh still has to make.  While
    every short window has room for all of them the answer is 0: a refresh
    is a handful of requests, far inside "30 per minute", and spacing them
    out only made the button feel slow.  Otherwise (or when `remaining` is
    unknown) requests are spread evenly over the tightest short window, and
    a window that is nearly full is sat out whole.  Long windows are
    budgeted up front instead (see `budget`)."""
    if not limits:
        return 1.0          # the site said nothing about limits: be gentle
    delay = 1.0 if remaining is None else 0.0
    for rule in limits:
        if rule["max"] <= 0 or rule["period"] > 120:
            continue
        room = rule["max"] - rule["hits"] - BURST_MARGIN
        if remaining is not None and remaining <= room:
            continue
        delay = max(delay, rule["period"] / rule["max"] * 1.25)
        if rule["hits"] >= rule["max"] - 2:
            delay = max(delay, float(rule["period"]))
    return delay


def budget(limits):
    """Requests still available in the long windows (which pacing cannot
    wait out), or None when the site sent no such rule."""
    left = [rule["max"] - rule["hits"] for rule in limits
            if rule["period"] > 120]
    return min(left) if left else None


def _explain(status, headers, body):
    try:
        err = json.loads(body.decode("utf-8", "replace")).get("error") or {}
    except (ValueError, AttributeError):
        err = {}
    detail = f" ({err.get('message')})" if err.get("message") else ""
    if status in (401, 403):
        return ("The site refused" + detail + ": the cookie has expired (log in at "
                "pathofexile.com again and copy POESESSID anew), or the account "
                "name or league in stash_config.json is wrong.")
    if status == 429:
        wait = _hget(headers, "Retry-After")
        return ("Too many requests - the site asks to wait "
                + (f"{wait} s." if wait else "a few minutes."))
    if status == 404:
        return "The site answered 404: this endpoint seems to be gone."
    return f"The site answered HTTP {status}{detail}."


# ---------------------------------------------------------------- stash ---

def tab_entry(raw, position):
    try:
        index = int(raw.get("i", position))
    except (TypeError, ValueError):
        index = position
    return {"id": str(raw.get("id") or f"index-{index}"), "index": index,
            "name": str(raw.get("n", "")), "type": str(raw.get("type", ""))}


def item_entry(raw):
    """One stash item -> {name, qty}, or None when it has no usable name."""
    name = str(raw.get("typeLine") or raw.get("baseType") or "").strip()
    if not name:
        return None
    try:
        qty = int(raw.get("stackSize", 1))
    except (TypeError, ValueError):
        qty = 1
    return {"name": name, "qty": max(qty, 1)}


def fetch_stash(cfg, tab_wanted, progress=None, get=http_get,
                sleep=time.sleep, now=time.time):
    """Download the tab list plus the items of every tab `tab_wanted(tab)`
    accepts; returns the cache dict and saves it as stash_cache.json."""
    headers = {"User-Agent": _user_agent(cfg), "Accept": "application/json",
               "Cookie": f"POESESSID={cfg['poesessid']}"}

    def request(index, with_tabs):
        query = urllib.parse.urlencode({
            "accountName": cfg["account"], "realm": "pc",
            "league": cfg["league"], "tabs": 1 if with_tabs else 0,
            "tabIndex": index})
        try:
            status, hdrs, body = get(f"{STASH_ENDPOINT}?{query}", headers)
        except OSError as e:
            raise StashError(f"The site did not answer: {e}") from None
        if status != 200:
            raise StashError(_explain(status, hdrs, body))
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or "error" in payload:
            raise StashError("The site sent something that is not stash data.")
        return payload, parse_limits(hdrs)

    if progress:
        progress("tab list...")
    first, limits = request(0, True)
    tabs = [tab_entry(t, n) for n, t in enumerate(first.get("tabs") or [])]
    if not tabs:
        raise StashError(
            f"The site shows no stash tabs in league {cfg['league']} - "
            "check the league name in stash_config.json.")
    wanted = [t for t in tabs if tab_wanted(t)]
    need = sum(1 for t in wanted if t["index"] != 0)
    left = budget(limits)
    if left is not None and need > left - 2:
        raise StashError(
            f"Site rate limit: {max(left, 0)} requests left until it resets, the "
            f"selected tabs need {need}. Wait, or untick some "
            "tabs.")

    raw_items = {0: first.get("items") or []}   # tab 0 came with the list
    todo = need
    for n, tab in enumerate(wanted, 1):
        if tab["index"] not in raw_items:
            delay = pace(limits, remaining=todo)
            if delay:
                sleep(delay)
            todo -= 1
            payload, limits = request(tab["index"], False)
            raw_items[tab["index"]] = payload.get("items") or []
        if progress:
            progress(f"tab {n} of {len(wanted)}: {tab['name']}")

    cache = {"version": 1, "fetched_at": now(), "league": cfg["league"],
             "tabs": []}
    for tab in tabs:
        got = tab["index"] in raw_items
        items = [e for e in map(item_entry, raw_items.get(tab["index"], []))
                 if e]
        cache["tabs"].append(dict(tab, fetched=got, items=items))
    _write_json("stash_cache.json", cache)
    return cache


def load_stash_cache():
    cache = _read_json("stash_cache.json")
    if isinstance(cache, dict) and isinstance(cache.get("tabs"), list):
        return cache
    return None


# --------------------------------------------------------------- prices ---

def parse_overview(payload):
    """Exchange overview -> {item name: [chaos each, traded volume]}."""
    names = {}
    for src in ((payload.get("core") or {}).get("items") or [],
                payload.get("items") or []):
        for it in src:
            if it.get("id") and it.get("name"):
                names[it["id"]] = it["name"]
    out = {}
    for line in payload.get("lines") or []:
        name = names.get(line.get("id"))
        value = line.get("primaryValue")
        if not name or not isinstance(value, (int, float)) or value <= 0:
            continue
        volume = line.get("volumePrimaryValue")
        out[name] = [float(value),
                     float(volume) if isinstance(volume, (int, float)) else 0.0]
    return out


def load_prices(league, force=False, progress=None, get=http_get,
                sleep=time.sleep, now=time.time, cfg=None):
    """Price cache for `league`, refreshed from poe.ninja when stale.
    A fresh cache is returned without any request; `force` shortens the
    freshness window to PRICE_MIN_AGE.  Categories that fail keep their
    previous data and are listed under 'errors'."""
    cache = _read_json("price_cache.json")
    if not isinstance(cache, dict) or cache.get("league") != league or \
            not isinstance(cache.get("types"), dict):
        cache = {"version": 1, "league": league, "fetched_at": 0, "types": {}}
    age = now() - cache.get("fetched_at", 0)
    if cache["types"] and age < (PRICE_MIN_AGE if force else PRICE_TTL):
        return cache

    errors = []
    for n, kind in enumerate(NINJA_TYPES):
        if progress:
            progress(f"prices {n + 1} of {len(NINJA_TYPES)}: {kind}")
        old = cache["types"].get(kind) or {}
        headers = {"User-Agent": _user_agent(cfg),
                   "Accept": "application/json"}
        if old.get("etag"):
            headers["If-None-Match"] = old["etag"]
        url = (f"{NINJA_BASE}/exchange/current/overview?"
               + urllib.parse.urlencode({"league": league, "type": kind}))
        try:
            status, hdrs, body = get(url, headers)
        except OSError:
            errors.append(kind)
            continue
        if status == 304:
            continue                    # unchanged since the last fetch
        if status != 200:
            errors.append(kind)
            continue
        try:
            items = parse_overview(json.loads(body.decode("utf-8", "replace")))
        except (ValueError, AttributeError):
            errors.append(kind)
            continue
        cache["types"][kind] = {"etag": _hget(hdrs, "ETag"), "items": items}
        if n + 1 < len(NINJA_TYPES):
            sleep(0.25)
    if len(errors) == len(NINJA_TYPES) and not cache["types"]:
        raise StashError("poe.ninja did not answer - no prices yet.")
    if len(errors) < len(NINJA_TYPES):
        cache["fetched_at"] = now()
    cache["errors"] = errors
    _write_json("price_cache.json", cache)
    return cache


def price_table(cache):
    """Flatten the price cache -> {name: (chaos each, volume, category)}."""
    table = {}
    for kind in NINJA_TYPES:
        for name, pv in ((cache or {}).get("types", {}).get(kind) or {}).get(
                "items", {}).items():
            table.setdefault(name, (pv[0], pv[1], kind))
    return table


def divine_price(table):
    entry = table.get("Divine Orb")
    return entry[0] if entry else None


# ---------------------------------------------------------------- state ---

def default_state():
    return {"version": 1, "unchecked": list(DEFAULT_UNCHECKED), "tabs": {},
            "min_total": 1.0, "show_unpriced": False,
            # selling: both off = ask quantity and price like flipper does
            "sell_all": False, "undercut": False, "sell_hotkey": "alt+w"}


def load_state():
    state = default_state()
    saved = _read_json("stash_state.json")
    if isinstance(saved, dict):
        if isinstance(saved.get("unchecked"), list):
            state["unchecked"] = [str(x) for x in saved["unchecked"]]
        if isinstance(saved.get("tabs"), dict):
            state["tabs"] = {str(k): bool(v) for k, v in saved["tabs"].items()}
        try:
            state["min_total"] = max(0.0, float(saved.get("min_total", 1.0)))
        except (TypeError, ValueError):
            pass
        state["show_unpriced"] = bool(saved.get("show_unpriced", False))
        state["sell_all"] = bool(saved.get("sell_all", False))
        state["undercut"] = bool(saved.get("undercut", False))
        hotkey = str(saved.get("sell_hotkey") or "").strip()
        if hotkey:
            state["sell_hotkey"] = hotkey
    return state


def save_state(state):
    try:
        _write_json("stash_state.json", state)
    except OSError:
        pass


def tab_enabled(state, tab):
    choice = state["tabs"].get(tab["id"])
    return tab["type"] in SELL_TAB_TYPES if choice is None else choice


# ----------------------------------------------------------------- rows ---

def aggregate(cache, state):
    """Sum quantities by item name over the enabled, downloaded tabs."""
    totals = {}
    for tab in (cache or {}).get("tabs", []):
        if not tab.get("fetched") or not tab_enabled(state, tab):
            continue
        for it in tab.get("items", []):
            entry = totals.setdefault(it["name"], {"qty": 0, "tabs": []})
            entry["qty"] += it["qty"]
            if tab["name"] not in entry["tabs"]:
                entry["tabs"].append(tab["name"])
    return totals


def build_rows(cache, table, state):
    """-> (priced rows, most valuable first; unpriced rows, biggest stack
    first).  `sell` is the tick box; the target currency itself is `fixed`
    (nothing to sell it for)."""
    unchecked = set(state["unchecked"])
    priced, unpriced = [], []
    for name, entry in aggregate(cache, state).items():
        price = table.get(name)
        if price is None and name != TARGET:
            unpriced.append({"name": name, "qty": entry["qty"],
                             "tabs": entry["tabs"]})
            continue
        fixed = name == TARGET
        each = 1.0 if fixed else price[0]
        priced.append({
            "name": name, "qty": entry["qty"], "each": each,
            "total": each * entry["qty"], "tabs": entry["tabs"],
            "volume": 0.0 if price is None else price[1], "fixed": fixed,
            "sell": not fixed and name not in unchecked})
    priced.sort(key=lambda r: (-r["total"], r["name"]))
    unpriced.sort(key=lambda r: (-r["qty"], r["name"]))
    return priced, unpriced


def is_small(row, min_total):
    """Small change: a stack worth less than the threshold is not worth an
    order — it is hidden from the list and left out of the sell total."""
    return not row["fixed"] and row["total"] < min_total


def summarize(rows, min_total=0.0):
    selling = [r for r in rows if r["sell"] and not is_small(r, min_total)]
    small = [r for r in rows if is_small(r, min_total)]
    return {"all": sum(r["total"] for r in rows),
            "selling": sum(r["total"] for r in selling),
            "selling_count": len(selling),
            "small": sum(r["total"] for r in small),
            "small_count": len(small),
            "chaos": sum(r["qty"] for r in rows if r["fixed"])}
