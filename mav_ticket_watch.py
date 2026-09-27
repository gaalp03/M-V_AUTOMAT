#!/usr/bin/env python3
"""Figyeli a config.json-ban megadott heti MÁV vonatok jegyeladását, és push
értesítést küld (ntfy.sh), amint valamelyiken szabad jegy van.

A jegy.mav.hu weboldal nem dokumentált belső API-ját használja
(https://jegy-a.mav.hu/IK_API_PROD/api).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import requests

BASE_URL = "https://jegy-a.mav.hu/IK_API_PROD/api"
BUDAPEST_TZ = ZoneInfo("Europe/Budapest")
CONFIG_FILE = "config.json"
STATE_FILE = ".mav_state.json"
TIME_TOLERANCE = timedelta(minutes=5)

HEADERS = {
    "Content-Type": "application/json",
    "Language": "hu",
    "UserSessionId": "1",
    "Origin": "https://jegy.mav.hu",
    "Referer": "https://jegy.mav.hu/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
}

DEFAULT_CUSTOMER_KEY = "HU_44_026-065"  # felnőtt, teljes árú - fallback, ha a lookup nem sikerül

WEEKDAYS = {"hetfo": 0, "kedd": 1, "szerda": 2, "csutortok": 3, "pentek": 4, "szombat": 5, "vasarnap": 6}
WEEKDAY_NAMES = ["hétfő", "kedd", "szerda", "csütörtök", "péntek", "szombat", "vasárnap"]
MONTH_NAMES = ["január", "február", "március", "április", "május", "június", "július",
               "augusztus", "szeptember", "október", "november", "december"]

# A workflow legördülő menüjének értékei -> config.json "figyeles" értékei
MODE_ALIASES = {
    "vasárnap": "vasarnap", "vasarnap": "vasarnap",
    "péntek": "pentek", "pentek": "pentek",
    "mindkettő": "mindketto", "mindketto": "mindketto",
    "kikapcsolva": "ki", "ki": "ki",
}


def load_json(path: str, default: dict) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def next_departure(train: dict, now: datetime) -> datetime:
    hour, minute = map(int, train["indulas"].split(":"))
    days_ahead = (WEEKDAYS[train["nap"]] - now.weekday()) % 7
    dep = datetime.combine(now.date() + timedelta(days=days_ahead), time(hour, minute), tzinfo=BUDAPEST_TZ)
    if dep <= now:
        dep += timedelta(days=7)
    return dep


def train_label(train: dict, dep: datetime) -> str:
    day = f"{MONTH_NAMES[dep.month - 1]} {dep.day}. ({WEEKDAY_NAMES[dep.weekday()]})"
    return f"{day} {train['indulas']}, {train['honnan']} → {train['hova']}"


def trains_for_mode(cfg: dict, mode: str) -> list[str]:
    if mode == "ki":
        return []
    if mode == "mindketto":
        return list(cfg["vonatok"])
    return [mode]


def mode_description(cfg: dict, mode: str, now: datetime) -> str:
    names = trains_for_mode(cfg, mode)
    if not names:
        return "A figyelés ki van kapcsolva."
    lines = [train_label(cfg["vonatok"][n], next_departure(cfg["vonatok"][n], now)) for n in names]
    return "Mostantól figyelem:\n" + "\n".join(lines)


def get_station_list(session: requests.Session) -> list[dict]:
    resp = session.post(f"{BASE_URL}/OfferRequestApi/GetStationList", json={}, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return data["stations"] if isinstance(data, dict) else data


def is_rail(station: dict) -> bool:
    return any(m.get("code") == 100 for m in station.get("modalities") or [])


def find_station(stations: list[dict], name_query: str) -> dict:
    query = name_query.strip().lower()
    usable = [
        s for s in stations
        if isinstance(s, dict) and s.get("name") and s.get("canUseForOfferRequest") and not s.get("isAlias")
    ]
    exact = [s for s in usable if s["name"].lower() == query]
    partial = [s for s in usable if query in s["name"].lower()]
    for group in (exact, partial):
        rail = [s for s in group if is_rail(s)]
        if rail or group:
            return sorted(rail or group, key=lambda s: len(s["name"]))[0]
    raise RuntimeError(f"Nem található állomás ezzel a névvel: {name_query!r}")


def get_adult_customer_key(session: requests.Session) -> str:
    try:
        resp = session.post(
            f"{BASE_URL}/OfferRequestApi/GetCustomersAndDiscounts",
            json={"offerKind": "InternalTicket"},
            timeout=20,
        )
        resp.raise_for_status()
        customer_types = resp.json().get("customerTypes", [])
        for ct in customer_types:
            if ct.get("key", "").startswith("HU_44_") and "felnőtt" in (ct.get("name") or "").lower():
                return ct["key"]
        for ct in customer_types:
            if ct.get("key", "").startswith("HU_44_"):
                return ct["key"]
    except requests.RequestException:
        pass
    return DEFAULT_CUSTOMER_KEY


def build_offer_body(from_code: str, to_code: str, travel_dt: datetime, customer_key: str) -> dict:
    travel_dt_utc = travel_dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "offerkind": "1",
        "startStationCode": from_code,
        "endStationCode": to_code,
        "innerStationsCodes": [],
        "passangers": [
            {"passengerCount": 1, "passengerId": 0, "customerTypeKey": customer_key, "customerDiscountsKeys": []}
        ],
        "isOneWayTicket": True,
        "isTravelEndTime": False,
        "isSupplementaryTicketsOnly": False,
        "travelStartDate": travel_dt_utc,
        "travelReturnDate": travel_dt_utc,
        "selectedServices": [],
        "selectedSearchServices": [],
        "eszkozSzamok": [],
        "isOfDetailedSearch": False,
        "isFromTimeTable": False,
    }


def parse_departure(iso_str: str) -> datetime:
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=BUDAPEST_TZ)
    return dt.astimezone(BUDAPEST_TZ)


def find_matching_route(routes: list[dict], target: datetime) -> dict | None:
    best, best_diff = None, TIME_TOLERANCE + timedelta(seconds=1)
    for route in routes:
        dep = (route.get("departure") or {}).get("time")
        if not dep:
            continue
        try:
            diff = abs(parse_departure(dep) - target)
        except ValueError:
            continue
        if diff < best_diff:
            best, best_diff = route, diff
    return best


def debug_route_summary(route: dict) -> str:
    details = route.get("details") or {}
    tickets = details.get("tickets") or []
    classes = [(c.get("name"), (c.get("price") or {}).get("amount")) for c in route.get("travelClasses") or []]
    return (
        f"  {(route.get('departure') or {}).get('time')} {details.get('trainFullName')!r} "
        f"orderDisabled={route.get('orderDisabled')} szabadHely={route.get('szabadHelyAllapot')} "
        f"osztályok(ár)={classes} jegyek={[t.get('name') for t in tickets]}"
    )


def class_price(travel_class: dict) -> float:
    return (travel_class.get("price") or {}).get("amount") or 0


def available_classes(route: dict, wanted: str) -> list[str]:
    # Élő válaszok alapján: teljesen betelt vonatnál szabadHelyAllapot="Nincs", a jegylista üres és az
    # osztály ára 0; részben betelt vonatnál csak a még megvehető osztály szerepel a travelClasses-ben.
    if route.get("orderDisabled") or route.get("szabadHelyAllapot") == "Nincs":
        return []
    if not (route.get("details") or {}).get("tickets"):
        return []
    names = sorted({c.get("name") for c in route.get("travelClasses") or [] if class_price(c) > 0})
    return names if wanted == "barmelyik" else [n for n in names if n == wanted]


def classes_text(names: list[str]) -> str:
    return " és ".join(f"{n}." for n in names) + " osztály"


def check_train(session: requests.Session, stations: list[dict], customer_key: str,
                train: dict, dep: datetime, wanted: str, debug: bool) -> list[str] | None:
    from_st = find_station(stations, train["honnan"])
    to_st = find_station(stations, train["hova"])
    # Kicsit korábbról keresünk, hogy a célvonat biztosan benne legyen a találatokban.
    body = build_offer_body(from_st["code"], to_st["code"], dep - timedelta(minutes=30), customer_key)
    resp = session.post(f"{BASE_URL}/OfferRequestApi/GetOfferRequest", json=body, timeout=30)
    if debug and not resp.ok:
        print(f"GetOfferRequest HTTP {resp.status_code}: {resp.text[:2000]}")
    resp.raise_for_status()
    routes = resp.json().get("route") or []
    if debug:
        print(f"{from_st['name']} ({from_st['code']}) -> {to_st['name']} ({to_st['code']}), {len(routes)} vonat:")
        for r in routes:
            print(debug_route_summary(r))
    match = find_matching_route(routes, dep)
    return None if match is None else available_classes(match, wanted)


def send_ntfy(topic: str, title: str, message: str, urgent: bool = True) -> None:
    resp = requests.post(
        "https://ntfy.sh/",
        json={
            "topic": topic,
            "title": title,
            "message": message,
            "priority": 5 if urgent else 3,
            "tags": ["rotating_light", "train"] if urgent else ["train"],
            "click": "https://jegy.mav.hu/",
        },
        timeout=20,
    )
    resp.raise_for_status()


def set_mode(cfg: dict, raw_mode: str, topic: str | None) -> int:
    mode = MODE_ALIASES.get(raw_mode.strip().lower())
    if mode is None or (mode not in ("ki", "mindketto") and mode not in cfg["vonatok"]):
        print(f"Ismeretlen figyelési mód: {raw_mode!r}", file=sys.stderr)
        return 1
    cfg["figyeles"] = mode
    save_json(CONFIG_FILE, cfg)
    text = mode_description(cfg, mode, datetime.now(BUDAPEST_TZ))
    print(text)
    if topic:
        send_ntfy(topic, "Figyelés átállítva", text, urgent=False)
    return 0


def run_check(cfg: dict, topic: str, debug: bool, test_notify: bool) -> int:
    mode = cfg.get("figyeles", "ki")
    names = trains_for_mode(cfg, mode)
    notify = bool(names)
    if not names:
        if not (debug or test_notify):
            print("A figyelés ki van kapcsolva.")
            return 0
        names = list(cfg["vonatok"])  # csak diagnosztika, értesítés nélkül

    now = datetime.now(BUDAPEST_TZ)
    session = requests.Session()
    session.headers.update(HEADERS)
    stations = get_station_list(session)
    customer_key = get_adult_customer_key(session)
    wanted = cfg.get("osztaly", "barmelyik")

    state = load_json(STATE_FILE, {})
    new_state, status_lines = {}, []
    for name in names:
        train = cfg["vonatok"][name]
        dep = next_departure(train, now)
        label = train_label(train, dep)
        classes = check_train(session, stations, customer_key, train, dep, wanted, debug)
        if classes is None:
            status = "nem találom ezt a vonatot"
        elif classes:
            status = f"van jegy ({classes_text(classes)})"
        else:
            status = "nincs jegy"
        print(f"[{now:%H:%M}] {label}: {status}")
        status_lines.append(f"{label}: {status}")

        key = f"{name}|{dep.date()}"
        new_state[key] = bool(classes)
        if classes and notify and not state.get(key):
            send_ntfy(topic, "Van szabad jegy!", f"{label}\nVan jegy: {classes_text(classes)}")
        elif classes and notify:
            print("  (erről már ment értesítés)")

    save_json(STATE_FILE, new_state)
    if test_notify:
        header = "Figyelés: kikapcsolva" if not notify else "Figyelés bekapcsolva"
        send_ntfy(topic, "Teszt: a figyelő működik", header + "\n" + "\n".join(status_lines), urgent=False)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set-mode", help="vasárnap | péntek | mindkettő | kikapcsolva")
    args = parser.parse_args()

    cfg = load_json(CONFIG_FILE, {})
    topic = os.environ.get("NTFY_TOPIC")
    if args.set_mode:
        return set_mode(cfg, args.set_mode, topic)
    if not topic:
        print("HIBA: az NTFY_TOPIC környezeti változó nincs beállítva.", file=sys.stderr)
        return 1
    try:
        return run_check(cfg, topic, os.environ.get("DEBUG") == "1", os.environ.get("TEST_NOTIFY") == "1")
    except (requests.ConnectionError, requests.Timeout) as exc:
        print(f"A MÁV szerver most nem érhető el, a következő futás újrapróbálja: {exc}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
