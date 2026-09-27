#!/usr/bin/env python3
"""Figyeli egy adott MÁV vonat jegyeladását, és push értesítést küld (ntfy.sh),
amint a keresett viszonylaton szabad jegy jelenik meg.

A jegy.mav.hu weboldal nem dokumentált belső API-ját használja
(https://jegy-a.mav.hu/IK_API_PROD/api), amit közösségi reverse-engineering
projektek (pl. github.com/berenteb/mav-api-ts) alapján ismerünk.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

BASE_URL = "https://jegy-a.mav.hu/IK_API_PROD/api"
BUDAPEST_TZ = ZoneInfo("Europe/Budapest")

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


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value else default


def load_config() -> dict:
    now_bp = datetime.now(BUDAPEST_TZ)
    travel_date = env("TRAVEL_DATE", now_bp.strftime("%Y-%m-%d"))
    train_time = env("TRAIN_TIME", "15:25")
    return {
        "from_station": env("FROM_STATION", "Szentlőrinc"),
        "to_station": env("TO_STATION", "Budapest-Kelenföld"),
        "travel_date": travel_date,
        "train_time": train_time,
        "train_name_hint": env("TRAIN_NAME_HINT", "Mecsek"),
        "wanted_class": env("WANTED_CLASS", "2"),
        "ntfy_topic": env("NTFY_TOPIC"),
        "debug": env("DEBUG", "0") == "1",
        "time_tolerance_min": int(env("TIME_TOLERANCE_MIN", "5")),
    }


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
        data = resp.json()
        for ct in data.get("customerTypes", []):
            key = ct.get("key", "")
            name = (ct.get("name") or "").lower()
            if key.startswith("HU_44_") and "felnőtt" in name:
                return key
        for ct in data.get("customerTypes", []):
            if ct.get("key", "").startswith("HU_44_"):
                return ct["key"]
    except requests.RequestException:
        pass
    return DEFAULT_CUSTOMER_KEY


def build_offer_body(from_code: str, to_code: str, travel_dt_local: datetime, customer_key: str) -> dict:
    travel_dt_utc_iso = travel_dt_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "offerkind": "1",
        "startStationCode": from_code,
        "endStationCode": to_code,
        "innerStationsCodes": [],
        "passangers": [
            {
                "passengerCount": 1,
                "passengerId": 0,
                "customerTypeKey": customer_key,
                "customerDiscountsKeys": [],
            }
        ],
        "isOneWayTicket": True,
        "isTravelEndTime": False,
        "isSupplementaryTicketsOnly": False,
        "travelStartDate": travel_dt_utc_iso,
        "travelReturnDate": travel_dt_utc_iso,
        "selectedServices": [],
        "selectedSearchServices": [],
        "eszkozSzamok": [],
        "isOfDetailedSearch": False,
        "isFromTimeTable": False,
    }


def parse_departure_local(iso_str: str) -> datetime:
    normalized = iso_str.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        # A MÁV API a hazai viszonylatoknál általában helyi (budapesti) időt ad vissza offset nélkül.
        return dt.replace(tzinfo=BUDAPEST_TZ)
    return dt.astimezone(BUDAPEST_TZ)


def find_matching_route(routes: list[dict], target_dt: datetime, tolerance_min: int, name_hint: str) -> dict | None:
    best = None
    best_diff = timedelta(minutes=tolerance_min + 1)
    for route in routes:
        dep = route.get("departure", {}).get("time")
        if not dep:
            continue
        try:
            dep_local = parse_departure_local(dep)
        except ValueError:
            continue
        diff = abs(dep_local - target_dt)
        if diff <= timedelta(minutes=tolerance_min) and diff < best_diff:
            best, best_diff = route, diff
    if best and name_hint:
        haystack = " ".join(
            str(x) for x in [best.get("name"), best.get("details", {}).get("trainFullName")] if x
        ).lower()
        if name_hint.lower() not in haystack:
            print(f"[figyelmeztetés] az időben egyező vonat neve nem tartalmazza a '{name_hint}' szót: {haystack!r}")
    return best


def debug_route_summary(route: dict) -> str:
    details = route.get("details") or {}
    tickets = details.get("tickets") or []
    classes = [(c.get("name"), c.get("fullness")) for c in route.get("travelClasses") or []]
    return (
        f"  {route.get('departure', {}).get('time')} {route.get('name')!r} "
        f"train={details.get('trainFullName')!r} orderDisabled={route.get('orderDisabled')} "
        f"reason={route.get('orderDisabledReason')!r} szabadHely={route.get('szabadHelyAllapot')} "
        f"classes={classes} tickets={[(t.get('name'), t.get('fullness')) for t in tickets]}"
    )


def is_ticket_available(route: dict, wanted_class: str) -> tuple[bool, str]:
    # A MÁV csak azokat az osztályokat adja vissza a travelClasses-ben, amelyekre még lehet jegyet venni.
    classes = {c.get("name"): c for c in route.get("travelClasses") or []}
    seats = route.get("szabadHelyAllapot")
    summary = f"elérhető osztályok: {sorted(classes) or 'nincs'}, szabad hely: {seats}"
    if route.get("orderDisabled"):
        return False, f"a vásárlás letiltva ({route.get('orderDisabledReason') or '-'}); {summary}"
    if wanted_class == "any":
        hit = next(iter(classes.values()), None)
    else:
        hit = classes.get(wanted_class)
    if not hit:
        return False, summary
    price = (hit.get("price") or {}).get("amount")
    return True, f"{hit.get('name')}. osztály, {price} Ft; {summary}"


def send_ntfy(topic: str, title: str, message: str, click_url: str) -> None:
    resp = requests.post(
        "https://ntfy.sh/",
        json={
            "topic": topic,
            "title": title,
            "message": message,
            "priority": 5,
            "tags": ["rotating_light", "steam_locomotive"],
            "click": click_url,
        },
        timeout=20,
    )
    resp.raise_for_status()


def main() -> int:
    cfg = load_config()
    if not cfg["ntfy_topic"]:
        print("HIBA: az NTFY_TOPIC környezeti változó nincs beállítva.", file=sys.stderr)
        return 1

    target_dt = datetime.strptime(
        f"{cfg['travel_date']} {cfg['train_time']}", "%Y-%m-%d %H:%M"
    ).replace(tzinfo=BUDAPEST_TZ)

    now = datetime.now(BUDAPEST_TZ)
    if now > target_dt:
        print(f"A vonat ({target_dt}) már elindult, nincs mit figyelni.")
        return 0

    session = requests.Session()
    session.headers.update(HEADERS)

    stations = get_station_list(session)
    from_st = find_station(stations, cfg["from_station"])
    to_st = find_station(stations, cfg["to_station"])
    customer_key = get_adult_customer_key(session)
    if cfg["debug"]:
        print(f"Indulás: {from_st['name']} ({from_st['code']}), érkezés: {to_st['name']} ({to_st['code']}), "
              f"utastípus: {customer_key}")

    # Kicsit korábbról keresünk, hogy a célvonat biztosan benne legyen a találatokban.
    search_dt = target_dt - timedelta(minutes=30)
    body = build_offer_body(from_st["code"], to_st["code"], search_dt, customer_key)
    resp = session.post(f"{BASE_URL}/OfferRequestApi/GetOfferRequest", json=body, timeout=30)
    if cfg["debug"] and not resp.ok:
        print(f"GetOfferRequest HTTP {resp.status_code}: {resp.text[:2000]}")
    resp.raise_for_status()
    data = resp.json()

    routes = data.get("route") or []
    if cfg["debug"]:
        print(f"Válasz kulcsai: {list(data.keys())}, {len(routes)} útvonal")
        for r in routes:
            print(debug_route_summary(r))

    match = find_matching_route(routes, target_dt, cfg["time_tolerance_min"], cfg["train_name_hint"])
    if not match:
        print(f"Nem található vonat {target_dt.strftime('%H:%M')} körül a válaszban ({len(routes)} találat).")
        return 0

    available, detail = is_ticket_available(match, cfg["wanted_class"])
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {cfg['from_station']} -> {cfg['to_station']} "
          f"({target_dt.strftime('%H:%M')}): {'VAN JEGY' if available else 'nincs jegy'} - {detail}")

    if available:
        send_ntfy(
            cfg["ntfy_topic"],
            title="🚨 VAN JEGY! Vedd meg gyorsan!",
            message=(
                f"{cfg['from_station']} → {cfg['to_station']}, "
                f"{target_dt.strftime('%Y-%m-%d %H:%M')} ({cfg['train_name_hint']} IC)\n{detail}"
            ),
            click_url="https://jegy.mav.hu/",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
