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
        "ntfy_topic": env("NTFY_TOPIC"),
        "debug": env("DEBUG", "0") == "1",
        "time_tolerance_min": int(env("TIME_TOLERANCE_MIN", "5")),
    }


def get_station_list(session: requests.Session) -> list[dict]:
    resp = session.post(f"{BASE_URL}/OfferRequestApi/GetStationList", json=None, timeout=20)
    resp.raise_for_status()
    return resp.json()


def find_station_code(stations: list[dict], name_query: str) -> str:
    query = name_query.strip().lower()
    candidates = [
        s for s in stations
        if s.get("name") and s.get("canUseForOfferRequest") and query in s["name"].lower()
    ]
    if not candidates:
        raise RuntimeError(f"Nem található állomás ezzel a névvel: {name_query!r}")
    candidates.sort(key=lambda s: len(s["name"]))
    return candidates[0]["code"]


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


def is_ticket_available(route: dict) -> tuple[bool, str]:
    if route.get("orderDisabled"):
        reason = route.get("orderDisabledReason") or "orderDisabled=true"
        return False, f"a foglalás letiltva ({reason})"
    tickets = route.get("details", {}).get("tickets") or []
    if not tickets:
        return False, "nincs elérhető jegytípus (tickets üres)"
    usable = [t for t in tickets if t.get("fullness", 100) < 100]
    if not usable:
        return False, "minden jegytípus betelt (fullness=100)"
    return True, f"{len(usable)} jegytípus elérhető"


def send_ntfy(topic: str, title: str, message: str, click_url: str) -> None:
    requests.post(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": "urgent",
            "Tags": "rotating_light,steam_locomotive",
            "Click": click_url,
        },
        timeout=20,
    )


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
    from_code = find_station_code(stations, cfg["from_station"])
    to_code = find_station_code(stations, cfg["to_station"])
    customer_key = get_adult_customer_key(session)

    body = build_offer_body(from_code, to_code, target_dt, customer_key)
    resp = session.post(f"{BASE_URL}/OfferRequestApi/GetOfferRequest", json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if cfg["debug"]:
        print(json.dumps(data, ensure_ascii=False, indent=2))

    routes = data.get("route") or []
    match = find_matching_route(routes, target_dt, cfg["time_tolerance_min"], cfg["train_name_hint"])
    if not match:
        print(f"Nem található vonat {target_dt.strftime('%H:%M')} körül a válaszban ({len(routes)} találat).")
        return 0

    available, detail = is_ticket_available(match)
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
