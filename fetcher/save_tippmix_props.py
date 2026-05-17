"""
Beolvassa a tippmix_*.json-t, kiszedi az összes player propot,
és elmenti a Firebase tippmix_props kollekcióba.

Futtatás: python save_tippmix_props.py [tippmix_YYYY-MM-DD.json]
"""
import json, sys, os, glob, datetime
import firebase_admin
from firebase_admin import credentials, firestore

SERVICE_ACCOUNT = os.path.join(os.path.dirname(__file__), "serviceAccount.json")

# TippMix fogadási típus → NBA stat kulcs
STAT_MAP = {
    "Játékosok hány pontot szerez?":           "pts",
    "Játékosok hány lepattanót szerez?":        "reb",
    "Játékosok hány asszisztot ad?":            "ast",
    "Játékosok hány 3-pontost szerez?":         "fg3m",
    "Játékosok Blokkok száma":                  "blk",
    "Játékosok labdaszerzések száma":           "stl",
    "Játékosok eladott labdák száma":           "tov",
    "Játékos Pontszám+lepattanó":               "pts_reb",
    "Játékos Assziszt+Lepattanó":              "ast_reb",
    "Játékosok Pontszám+assziszt":              "pts_ast",
    "Játékosok Pontszám+assziszt+lepattanó ":  "pts_ast_reb",
    "Játékosok Pontszám+assziszt+lepattanó":   "pts_ast_reb",
    "Játékosok Labdaszerzés+blokk ":           "stl_blk",
    "Játékosok Labdaszerzés+blokk":            "stl_blk",
}

def extract_player_name(outcome_name: str) -> str:
    """'Cade Cunningham: Több, mint 26.5' → 'Cade Cunningham'"""
    name = outcome_name.strip()
    if ":" in name:
        name = name.split(":")[0].strip()
    elif " Több," in name:
        name = name.split(" Több,")[0].strip()
    elif " Kevesebb," in name:
        name = name.split(" Kevesebb,")[0].strip()
    elif ": Igen" in name:
        name = name.split(": Igen")[0].strip()
    return name

def extract_direction(outcome_name: str) -> str:
    if "Több" in outcome_name or "Igen" in outcome_name:
        return "over"
    if "Kevesebb" in outcome_name:
        return "under"
    return "?"

def parse_all_props(raw: list) -> list[dict]:
    outcomes, markets, offers = {}, {}, []

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
        for r in payload.get("records", []):
            if not isinstance(r, dict):
                continue
            t = r.get("_type", r.get("entityType", ""))
            if t == "OUTCOME":
                outcomes[r.get("id")] = r
            elif t == "MARKET":
                markets[r.get("id")] = r
            elif t == "BETTING_OFFER":
                offers.append(r)

    props = []
    seen = set()
    for offer in offers:
        bt = offer.get("bettingTypeName", "")
        if "Játékos" not in bt:
            continue
        oid = offer.get("outcomeId", "")
        outcome = outcomes.get(oid, {})
        name = outcome.get("translatedName", "")
        line = outcome.get("paramFloat1")
        odds = offer.get("odds")
        if not name or not odds:
            continue

        player = extract_player_name(name)
        direction = extract_direction(name)
        stat_key = STAT_MAP.get(bt, "?")

        key = "%s|%s|%s|%s" % (player, stat_key, line, direction)
        if key in seen:
            continue
        seen.add(key)

        props.append({
            "player_name": player,
            "betting_type": bt,
            "stat_key": stat_key,
            "line": line,
            "direction": direction,
            "odds": odds,
            "outcome_name": name,
            "offer_id": offer.get("id"),
            "status": offer.get("statusId", "1"),
        })

    return props


def save_to_firebase(props: list[dict], match_name: str, match_date: str):
    cred = credentials.Certificate(SERVICE_ACCOUNT)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    db = firestore.client()

    fetched_at = datetime.datetime.utcnow().isoformat()
    batch = db.batch()

    for p in props:
        doc_id = "%s_%s_%s_%s_%s" % (
            match_date,
            p["player_name"].replace(" ", "_"),
            p["stat_key"],
            str(p["line"]).replace(".", "_"),
            p["direction"],
        )
        ref = db.collection("tippmix_props").document(doc_id)
        batch.set(ref, {
            **p,
            "match_name": match_name,
            "date": match_date,
            "fetched_at": fetched_at,
        })

    # Összesítő
    summary_ref = db.collection("tippmix_props").document("%s__summary" % match_date)
    batch.set(summary_ref, {
        "date": match_date,
        "match_name": match_name,
        "prop_count": len(props),
        "fetched_at": fetched_at,
    })

    batch.commit()
    print("Mentve: %d prop → tippmix_props" % len(props))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        files = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "..", "tippmix_*.json")))
        source = files[-1] if files else None
    else:
        source = sys.argv[1]

    if not source:
        print("Nincs fájl.")
        sys.exit(1)

    print("Fájl: %s" % source)
    with open(source, encoding="utf-8") as f:
        raw = json.load(f)

    props = parse_all_props(raw)
    print("Talált player prop: %d" % len(props))

    # Stat összesítő
    from collections import Counter
    for stat, cnt in Counter(p["stat_key"] for p in props).most_common():
        print("  %3d  %s" % (cnt, stat))

    match_date = datetime.date.today().isoformat()
    match_name = "Detroit - Cleveland"

    ans = input("\nMentsük Firebase-be? (i/n): ").strip().lower()
    if ans == "i":
        save_to_firebase(props, match_name, match_date)
