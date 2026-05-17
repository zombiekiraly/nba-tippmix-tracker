"""
TippMixPro odds parser – feldolgozza az extension által letöltött JSON-t,
kiszedi az NBA meccsek oddsait (AGGREGATOR + INITIAL_DUMP formátum),
és elmenti Firebase Firestore-ba.

Futtatás: python parse_tippmix.py [tippmix_YYYY-MM-DD.json]
"""

import json
import sys
import os
import datetime
import firebase_admin
from firebase_admin import credentials, firestore

SERVICE_ACCOUNT = os.path.join(os.path.dirname(__file__), "serviceAccount.json")


def parse_nba_odds(raw_entries: list) -> list[dict]:
    matches = {}

    for entry in raw_entries:
        url = entry.get("url", "")
        if "sportsapi" not in url or not url.startswith("ws_in"):
            continue

        d = entry.get("data")
        if not d or not isinstance(d, list):
            continue

        # type=50 RESULT → payload d[4]
        # type=68 EVENT  → payload d[5]
        if d[0] == 50 and len(d) > 4:
            payload = d[4]
        elif d[0] == 68 and len(d) > 5:
            payload = d[5]
        else:
            continue

        if not isinstance(payload, dict):
            continue

        records = payload.get("records", [])
        fmt = payload.get("format", "")

        # ── AGGREGATOR formátum (korábbi struktúra) ──────────────────────────
        if fmt == "AGGREGATOR":
            for record in records:
                if not isinstance(record, dict):
                    continue
                match = record.get("match", {})
                if not match:
                    continue
                tournament = record.get("tournament", {})
                if "NBA" not in tournament.get("name", "") and "NBA" not in tournament.get("templateName", ""):
                    continue
                _process_aggregator_record(record, matches)

        # ── BASIC formátum (INITIAL_DUMP / UPDATE) ───────────────────────────
        elif fmt == "BASIC":
            _process_basic_records(records, matches)

    return sorted(matches.values(), key=lambda x: x["start_ts"])


def _process_aggregator_record(record: dict, matches: dict):
    match = record.get("match", {})
    tournament = record.get("tournament", {})
    mid = match.get("id")
    if not mid:
        return

    _ensure_match(mid, match, tournament, matches)

    outcomes_by_id = {o["id"]: o for o in record.get("outcomes", [])}

    for offer in record.get("bettingOffers", []):
        oid = offer.get("outcomeId", "")
        outcome = outcomes_by_id.get(oid, {})
        _add_offer(mid, offer, outcome, matches)


# Átmeneti tárolás BASIC parse-hoz (offer ID → adatok összeillesztése)
_basic_outcomes: dict = {}    # outcomeId → outcome dict
_basic_markets:  dict = {}    # marketId  → market dict
_basic_match_map: dict = {}   # matchId   → match dict (NBA)
_basic_tournament_map: dict = {} # matchId → tournament dict


def _process_basic_records(records: list, matches: dict):
    global _basic_outcomes, _basic_markets, _basic_match_map, _basic_tournament_map

    # Első pass: indexeljük az összes entitást
    for r in records:
        if not isinstance(r, dict):
            continue
        rtype = r.get("_type", r.get("entityType", ""))

        if rtype == "MATCH":
            mid = r.get("id")
            sport_id = r.get("sportId", "")
            parent_name = r.get("parentName", "")
            template_id = r.get("parentTemplateId", "")
            # Csak NBA (sportId=8, NBA template)
            if sport_id == "8" and ("NBA" in parent_name or template_id == "98"):
                _basic_match_map[mid] = r
                _basic_tournament_map[mid] = {
                    "name": parent_name,
                    "templateName": "NBA",
                }
                _ensure_match(mid, r, _basic_tournament_map[mid], matches)

        elif rtype == "OUTCOME":
            oid = r.get("id")
            if oid:
                _basic_outcomes[oid] = r

        elif rtype == "MARKET":
            mktid = r.get("id")
            if mktid:
                _basic_markets[mktid] = r

        elif rtype == "BETTING_OFFER":
            oid = r.get("outcomeId", "")
            outcome = _basic_outcomes.get(oid, {})
            # Melyik meccshez tartozik?
            event_id = outcome.get("eventId") or r.get("eventId")
            if event_id and event_id in matches:
                _add_offer(event_id, r, outcome, matches)

        elif rtype == "BETTING_OFFER" or (rtype == "" and r.get("changeType") == "UPDATE" and r.get("entityType") == "BETTING_OFFER"):
            # Live odds update
            offer_id = r.get("id")
            changed = r.get("changedProperties", {})
            new_odds = changed.get("odds")
            if new_odds and offer_id:
                for m in matches.values():
                    for offer in m.get("betting_offers", {}).values():
                        if offer.get("offer_id") == offer_id:
                            offer["odds"] = new_odds


def _ensure_match(mid: str, match: dict, tournament: dict, matches: dict):
    if mid in matches:
        return
    start_ts = match.get("startTime", 0)
    matches[mid] = {
        "match_id": mid,
        "name": match.get("name", "?"),
        "home_team": match.get("homeParticipantName", "?"),
        "away_team": match.get("awayParticipantName", "?"),
        "start_time": datetime.datetime.fromtimestamp(start_ts / 1000).isoformat() if start_ts else None,
        "start_ts": start_ts,
        "tournament": tournament.get("name", "?"),
        "is_live": match.get("isLive", False),
        "betting_offers": {},
    }


def _add_offer(mid: str, offer: dict, outcome: dict, matches: dict):
    if mid not in matches:
        return

    oid = offer.get("id") or offer.get("outcomeId")
    if not oid:
        return

    outcome_name = (
        outcome.get("translatedName")
        or outcome.get("paramParticipantName1", "")
        or outcome.get("typeName", "?")
    )
    line = outcome.get("paramFloat1")
    header_key = outcome.get("headerNameKey", "")

    # Over/Under meghatározása
    type_name = outcome.get("typeName", "")
    if "Over" in type_name or "Több" in outcome_name:
        direction = "over"
    elif "Under" in type_name or "Kevesebb" in outcome_name:
        direction = "under"
    else:
        direction = header_key or "?"

    key = f"{offer.get('bettingTypeId','?')}_{outcome.get('id', oid)}_{direction}"
    existing = matches[mid]["betting_offers"].get(key)
    last_changed = offer.get("lastChangedTime", 0)

    if not existing or last_changed > existing.get("last_changed", 0):
        matches[mid]["betting_offers"][key] = {
            "offer_id": offer.get("id"),
            "outcome_id": outcome.get("id"),
            "outcome_name": outcome_name,
            "betting_type": offer.get("bettingTypeName", "?"),
            "odds": offer.get("odds"),
            "is_live": offer.get("isLive", False),
            "status": offer.get("statusId", "1"),
            "line": line,
            "direction": direction,
            "last_changed": last_changed,
        }


def finalize(matches: dict) -> list[dict]:
    result = []
    for m in matches.values():
        m["betting_offers"] = [
            o for o in m["betting_offers"].values()
            if o.get("odds") and o.get("status") == "1"
        ]
        result.append(m)
    return sorted(result, key=lambda x: x["start_ts"])


def print_summary(matches: list[dict]):
    print(f"\n{'='*60}")
    print(f"NBA meccsek: {len(matches)}")
    print(f"{'='*60}")
    for m in matches:
        live_tag = " [LIVE]" if m["is_live"] else ""
        offers = m["betting_offers"]
        player_offers = [o for o in offers if ":" in o.get("outcome_name", "")]
        print(f"\n{m['name']}{live_tag}  ({m['start_time']})")
        print(f"  Összes odds: {len(offers)}  |  Player prop: {len(player_offers)}")
        for o in player_offers[:15]:
            line_str = f" {o['line']:+.1f}" if o["line"] is not None else ""
            print(f"    {o['betting_type']}{line_str} | {o['outcome_name']} | {o['odds']}")


def save_to_firestore(matches: list[dict], source_file: str):
    cred = credentials.Certificate(SERVICE_ACCOUNT)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    db = firestore.client()

    today = datetime.date.today().isoformat()
    fetched_at = datetime.datetime.utcnow().isoformat()
    batch = db.batch()

    for m in matches:
        doc_id = f"{today}_{m['match_id']}"
        ref = db.collection("tippmix_nba_odds").document(doc_id)
        batch.set(ref, {
            **m,
            "date": today,
            "fetched_at": fetched_at,
            "source_file": os.path.basename(source_file),
        }, merge=True)
        print(f"  ✓ {m['name']} – {len(m['betting_offers'])} odds")

    summary_ref = db.collection("tippmix_nba_odds").document(f"{today}_summary")
    batch.set(summary_ref, {
        "date": today,
        "fetched_at": fetched_at,
        "match_count": len(matches),
        "source_file": os.path.basename(source_file),
        "match_names": [m["name"] for m in matches],
    }, merge=True)

    batch.commit()
    print(f"\n✅ {len(matches)} meccs mentve → tippmix_nba_odds")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        import glob
        files = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "..", "tippmix_*.json")))
        if not files:
            print("Használat: python parse_tippmix.py <tippmix_YYYY-MM-DD.json>")
            sys.exit(1)
        source = files[-1]
        print(f"Fájl: {source}")
    else:
        source = sys.argv[1]

    with open(source, encoding="utf-8") as f:
        raw = json.load(f)

    print(f"Beolvasva: {len(raw)} capture bejegyzés")

    matches_dict = {}
    parse_nba_odds.__globals__["_basic_outcomes"] = {}
    parse_nba_odds.__globals__["_basic_markets"] = {}
    parse_nba_odds.__globals__["_basic_match_map"] = {}
    parse_nba_odds.__globals__["_basic_tournament_map"] = {}

    # Két körös parse: először indexelünk, aztán összeillesztünk
    for entry in raw:
        url = entry.get("url", "")
        if "sportsapi" not in url or not url.startswith("ws_in"):
            continue
        d = entry.get("data")
        if not d or not isinstance(d, list):
            continue
        if d[0] == 50 and len(d) > 4:
            payload = d[4]
        elif d[0] == 68 and len(d) > 5:
            payload = d[5]
        else:
            continue
        if not isinstance(payload, dict):
            continue
        fmt = payload.get("format", "")
        records = payload.get("records", [])
        if fmt == "AGGREGATOR":
            for record in records:
                if not isinstance(record, dict):
                    continue
                match = record.get("match", {})
                tournament = record.get("tournament", {})
                if not match:
                    continue
                if "NBA" not in tournament.get("name", "") and "NBA" not in tournament.get("templateName", ""):
                    continue
                _process_aggregator_record(record, matches_dict)
        elif fmt == "BASIC":
            _process_basic_records(records, matches_dict)

    matches = finalize(matches_dict)
    print_summary(matches)

    if not matches:
        print("\n⚠️  Nem találtam NBA meccseket. Navigálj az NBA részlegre és a Játékosok fülre.")
        sys.exit(0)

    answer = input("\nMentsük Firebase-be? (i/n): ").strip().lower()
    if answer == "i":
        save_to_firestore(matches, source)
    else:
        print("Mentés kihagyva.")
