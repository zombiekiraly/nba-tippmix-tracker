"""
╔══════════════════════════════════════════════════════════════════════╗
║   NBA Tippmix Tracker — Adatgyűjtő Backend Script                   ║
║   Adatforrás : nba_api (stats.nba.com) — ingyenes, API kulcs nélkül ║
║   Tárolás    : Firebase Firestore                                    ║
║   Futtatás   : python fetch_stats.py  (vagy GitHub Actions cron)    ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import os
import sys
import time
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv

import firebase_admin
from firebase_admin import credentials, firestore

from nba_api.stats.endpoints import (
    leaguedashplayerstats,
    leaguedashteamstats,
)

# ═══════════════════════════════════════════════════════════════════════════════
# KONFIGURÁCIÓ
# ═══════════════════════════════════════════════════════════════════════════════

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("nba-tracker")

SEASON               = os.getenv("NBA_SEASON", "2025-26")
SERVICE_ACCOUNT_PATH = os.getenv("FIREBASE_SERVICE_ACCOUNT", "serviceAccount.json")
REQUEST_DELAY        = float(os.getenv("REQUEST_DELAY", "2.0"))
MIN_GAMES            = int(os.getenv("MIN_GAMES", "10"))
BATCH_SIZE           = 450
TIMEOUT_DEFAULT      = 45

# ── NBA.com böngésző-fejlécek ──────────────────────────────────────────────
NBA_HEADERS = {
    "Accept":             "application/json, text/plain, */*",
    "Accept-Encoding":    "gzip, deflate, br",
    "Accept-Language":    "en-US,en;q=0.9",
    "Connection":         "keep-alive",
    "Host":               "stats.nba.com",
    "Origin":             "https://www.nba.com",
    "Referer":            "https://www.nba.com/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token":  "true",
}

# ── Over/Under küszöbértékek (fogadási vonalak) ────────────────────────────
OU_THRESHOLDS = {
    "pts":  [15.5, 20.5, 25.5, 30.5],
    "ast":  [4.5,  6.5,  8.5, 10.5],
    "reb":  [4.5,  6.5,  8.5, 10.5],
    "fg3m": [1.5,  2.5,  3.5],
    "fg2m": [3.5,  5.5,  7.5],
    "stl":  [0.5,  1.5],
    "blk":  [0.5,  1.5],
}

# ── Globális Firestore kliens (init_firebase() állítja be) ─────────────────
db = None


# ═══════════════════════════════════════════════════════════════════════════════
# SEGÉDFÜGGVÉNYEK
# ═══════════════════════════════════════════════════════════════════════════════

def safe_float(val, digits=1):
    try:
        return round(float(val), digits)
    except (TypeError, ValueError):
        return 0.0

def safe_int(val):
    try:
        return int(val or 0)
    except (TypeError, ValueError):
        return 0

def nba_api_call(endpoint_fn, description, timeout=TIMEOUT_DEFAULT, **kwargs):
    """
    Újrapróbálkozó wrapper NBA API hívásokhoz.
    Böngésző-fejléceket küld (NBA.com bot-szűrő megkerülése).
    Max 4 kísérlet, exponenciális visszalépéssel.
    """
    for attempt in range(1, 5):
        try:
            log.info("   %s (kísérlet %d/4)...", description, attempt)
            time.sleep(REQUEST_DELAY if attempt == 1 else REQUEST_DELAY * 2)
            return endpoint_fn(
                headers=NBA_HEADERS,
                timeout=timeout,
                **kwargs
            ).get_dict()
        except Exception as exc:
            short = str(exc)[:100]
            wait  = min(attempt * 15, 60)
            log.warning("   Hiba: %s — %ds varako...", short, wait)
            if attempt == 4:
                log.error("   SIKERTELEN: %s", description)
                raise
            time.sleep(wait)

def parse_result_set(raw, index=0):
    rs = raw["resultSets"][index]
    return [dict(zip(rs["headers"], row)) for row in rs["rowSet"]]

def extract_avg(r):
    """Egy LeagueDashPlayerStats sor -> stats szótár."""
    fgm  = safe_float(r.get("FGM",  0))
    fg3m = safe_float(r.get("FG3M", 0))
    fga  = safe_float(r.get("FGA",  0))
    fg3a = safe_float(r.get("FG3A", 0))
    return {
        "pts":     safe_float(r.get("PTS",  0)),
        "fgm":     fgm,
        "fga":     fga,
        "fg_pct":  safe_float((r.get("FG_PCT",  0) or 0) * 100),
        "fg2m":    safe_float(fgm - fg3m),
        "fg2a":    safe_float(fga - fg3a),
        "fg3m":    fg3m,
        "fg3a":    fg3a,
        "fg3_pct": safe_float((r.get("FG3_PCT", 0) or 0) * 100),
        "ftm":     safe_float(r.get("FTM",  0)),
        "fta":     safe_float(r.get("FTA",  0)),
        "ft_pct":  safe_float((r.get("FT_PCT",  0) or 0) * 100),
        "oreb":    safe_float(r.get("OREB", 0)),
        "dreb":    safe_float(r.get("DREB", 0)),
        "reb":     safe_float(r.get("REB",  0)),
        "ast":     safe_float(r.get("AST",  0)),
        "stl":     safe_float(r.get("STL",  0)),
        "blk":     safe_float(r.get("BLK",  0)),
        "tov":     safe_float(r.get("TOV",  0)),
        "min":     safe_float(r.get("MIN",  0)),
        "plus_minus": safe_float(r.get("PLUS_MINUS", 0)),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 1. SZEZONÁTLAGOK
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_season_averages():
    """Összes játékos aktuális szezon PerGame átlaga. {player_id: {...}}"""
    raw  = nba_api_call(
        leaguedashplayerstats.LeagueDashPlayerStats,
        "Szezonátlagok (összes játékos)",
        season=SEASON,
        per_mode_detailed="PerGame",
        measure_type_detailed_defense="Base",
    )
    rows = parse_result_set(raw)
    result = {}
    for r in rows:
        pid = str(r["PLAYER_ID"])
        result[pid] = {
            "player_id":         pid,
            "player_name":       r.get("PLAYER_NAME", ""),
            "team_id":           str(r.get("TEAM_ID", "")),
            "team_abbreviation": r.get("TEAM_ABBREVIATION", ""),
            "games_played":      safe_int(r.get("GP", 0)),
            "season_avg":        extract_avg(r),
        }
    log.info("   OK: %d jatekos szezonatlaga betoltve", len(result))
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# 2. UTOLSÓ N MECCS ÁTLAGAI
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_last_n_averages(n):
    """Összes játékos utolsó N meccsének PerGame átlaga. {player_id: {...}}"""
    raw  = nba_api_call(
        leaguedashplayerstats.LeagueDashPlayerStats,
        "Utolso %d meccs atlagok" % n,
        season=SEASON,
        per_mode_detailed="PerGame",
        measure_type_detailed_defense="Base",
        last_n_games=n,
    )
    rows = parse_result_set(raw)
    result = {}
    for r in rows:
        result[str(r["PLAYER_ID"])] = extract_avg(r)
    log.info("   OK: %d jatekos L%d atlaga betoltve", len(result), n)
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# 3. FORMA-ALAPÚ O/U TRENDEK (PlayerGameLogs nélkül)
#
#  Az NBA.com stats/playergamelogs végpont az egész szezon ~30 000 sorát
#  nem tudja megbízhatóan visszaadni (timeout). Helyette:
#  4 ablakból (Szezon / L20 / L10 / L5) megvizsgáljuk, hogy az átlag
#  meghaladja-e a küszöbértéket. Ez adja a "forma-alapú O/U score"-t.
#
#  Példa: ha PTS átlag >20.5 a szezon, L20, L10 és L5 ablakokban is
#         → score = 4/4 = 100% → erős OVER tendencia
# ═══════════════════════════════════════════════════════════════════════════════

def compute_ou_form(season_avg, l5_avg, l10_avg, l20_avg):
    """
    Forma-alapú O/U score kiszámítása 4 időablak alapján.

    Visszatérési érték:
    {
        "pts": {
            "20.5": {
                "windows":    4,        # hány ablakban vizsgáltuk
                "over_count": 3,        # hány ablakban volt az átlag > küszöb
                "pct":        75.0,     # over_count/windows * 100
                "avgs": {               # az átlagok az egyes ablakokban
                    "season": 22.1,
                    "l20":    24.3,
                    "l10":    26.5,
                    "l5":     27.8,
                },
                "trend": "hot"          # hot / good / neutral / cold / under
            },
            ...
        },
        ...
    }
    """
    result = {}
    windows = [
        ("season", season_avg),
        ("l20",    l20_avg),
        ("l10",    l10_avg),
        ("l5",     l5_avg),
    ]

    for stat, lines in OU_THRESHOLDS.items():
        result[stat] = {}
        for line in lines:
            key        = str(line)
            avgs       = {}
            over_count = 0
            valid_win  = 0

            for wname, wdata in windows:
                val = wdata.get(stat)
                if val is None:
                    continue
                avgs[wname] = val
                valid_win  += 1
                if val > line:
                    over_count += 1

            pct = safe_float(over_count / valid_win * 100 if valid_win else 0)

            if pct >= 75:   trend = "hot"
            elif pct >= 50: trend = "good"
            elif pct >= 25: trend = "neutral"
            else:           trend = "cold"

            result[stat][key] = {
                "windows":    valid_win,
                "over_count": over_count,
                "pct":        pct,
                "avgs":       avgs,
                "trend":      trend,
            }
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# 4. CSAPATSTATISZTIKÁK
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_team_stats():
    """Csapat alap + haladó + ellenfél statisztikák."""
    raw_base = nba_api_call(
        leaguedashteamstats.LeagueDashTeamStats,
        "Csapat alapstatisztikák (PerGame)",
        season=SEASON,
        per_mode_detailed="PerGame",
        measure_type_detailed_defense="Base",
    )
    raw_adv = nba_api_call(
        leaguedashteamstats.LeagueDashTeamStats,
        "Csapat halado statisztikák (pace, rating)",
        season=SEASON,
        per_mode_detailed="PerGame",
        measure_type_detailed_defense="Advanced",
    )
    raw_opp = nba_api_call(
        leaguedashteamstats.LeagueDashTeamStats,
        "Ellenfél statisztikák (kapott pontok)",
        season=SEASON,
        per_mode_detailed="PerGame",
        measure_type_detailed_defense="Opponent",
    )

    def to_map(raw):
        rows = parse_result_set(raw)
        return {str(r["TEAM_ID"]): r for r in rows}

    base_map = to_map(raw_base)
    adv_map  = to_map(raw_adv)
    opp_map  = to_map(raw_opp)

    team_list = []
    for tid, b in base_map.items():
        a = adv_map.get(tid, {})
        o = opp_map.get(tid, {})
        pts_for   = safe_float(b.get("PTS", 0))
        opp_pts   = safe_float(o.get("OPP_PTS", 0))
        team_list.append({
            "team_id":            tid,
            "team_name":          b.get("TEAM_NAME", ""),
            "team_abbreviation":  b.get("TEAM_ABBREVIATION", ""),
            "games_played":       safe_int(b.get("GP", 0)),
            "wins":               safe_int(b.get("W",  0)),
            "losses":             safe_int(b.get("L",  0)),
            "pts_per_game":       pts_for,
            "opp_pts_per_game":   opp_pts,
            "total_pts_per_game": safe_float(pts_for + opp_pts),
            "pace":               safe_float(a.get("PACE",       0)),
            "off_rating":         safe_float(a.get("OFF_RATING", 0)),
            "def_rating":         safe_float(a.get("DEF_RATING", 0)),
            "net_rating":         safe_float(a.get("NET_RATING", 0)),
            "ts_pct":             safe_float((a.get("TS_PCT", 0) or 0) * 100),
            "fg_pct":             safe_float((b.get("FG_PCT",  0) or 0) * 100),
            "fg3_pct":            safe_float((b.get("FG3_PCT", 0) or 0) * 100),
            "ft_pct":             safe_float((b.get("FT_PCT",  0) or 0) * 100),
            "oreb_per_game":      safe_float(b.get("OREB", 0)),
            "dreb_per_game":      safe_float(b.get("DREB", 0)),
            "ast_per_game":       safe_float(b.get("AST",  0)),
            "tov_per_game":       safe_float(b.get("TOV",  0)),
            "blk_per_game":       safe_float(b.get("BLK",  0)),
            "stl_per_game":       safe_float(b.get("STL",  0)),
            "updated_at":         datetime.now(timezone.utc).isoformat(),
            "season":             SEASON,
        })
    log.info("   OK: %d csapat statisztikaja betoltve", len(team_list))
    return team_list


# ═══════════════════════════════════════════════════════════════════════════════
# 5. FIREBASE FELTÖLTÉS
# ═══════════════════════════════════════════════════════════════════════════════

def upload_player_averages(season_data, last5_data, last10_data, last20_data):
    """
    Összerakja és feltölti az összes játékos-dokumentumot a
    Firestore player_averages kollekcióba. Batch írással.
    """
    log.info("Jatekos adatok feltoltese Firebase-be...")
    db_ref  = db.collection("player_averages")
    batch   = db.batch()
    count   = 0
    skipped = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for pid, s in season_data.items():
        if s["games_played"] < MIN_GAMES:
            skipped += 1
            continue

        l5_avg  = last5_data.get(pid,  {})
        l10_avg = last10_data.get(pid, {})
        l20_avg = last20_data.get(pid, {})

        doc = {
            "player_id":         pid,
            "player_name":       s["player_name"],
            "team_id":           s["team_id"],
            "team_abbreviation": s["team_abbreviation"],
            "games_played":      s["games_played"],
            "season_avg":        s["season_avg"],
            "last5_avg":         l5_avg,
            "last10_avg":        l10_avg,
            "last20_avg":        l20_avg,
            "ou_form":           compute_ou_form(
                                     s["season_avg"], l5_avg, l10_avg, l20_avg
                                 ),
            "updated_at":        now_iso,
            "season":            SEASON,
        }

        batch.set(db_ref.document(pid), doc)
        count += 1

        if count % BATCH_SIZE == 0:
            batch.commit()
            log.info("   ... %d jatekos feltoltve", count)
            batch = db.batch()
            time.sleep(0.3)

    batch.commit()
    log.info("   OK: %d jatekos feltoltve, %d kihagyva (keves meccs)", count, skipped)


def upload_team_stats(team_list):
    log.info("Csapat adatok feltoltese Firebase-be...")
    batch = db.batch()
    for team in team_list:
        batch.set(db.collection("team_stats").document(team["team_id"]), team)
    batch.commit()
    log.info("   OK: %d csapat feltoltve", len(team_list))


def upload_meta():
    db.collection("meta").document("last_update").set({
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "season":     SEASON,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# BELÉPÉSI PONT
# ═══════════════════════════════════════════════════════════════════════════════

def init_firebase():
    global db
    if not os.path.exists(SERVICE_ACCOUNT_PATH):
        log.error("Firebase service account nem talalhato: %s", SERVICE_ACCOUNT_PATH)
        sys.exit(1)
    cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    log.info("Firebase inicializalva")


def main():
    print("\n" + "=" * 60)
    print("  NBA Tippmix Tracker — Adatfrissites")
    print("  Szezon: %s  |  %s" % (SEASON, datetime.now().strftime("%Y-%m-%d %H:%M")))
    print("=" * 60 + "\n")

    init_firebase()
    start = time.time()

    log.info("=== ADATLETOLTES ===")
    season_data = fetch_season_averages()
    last5_data  = fetch_last_n_averages(5)
    last10_data = fetch_last_n_averages(10)
    last20_data = fetch_last_n_averages(20)
    team_list   = fetch_team_stats()

    log.info("=== FIREBASE FELTOLTES ===")
    upload_player_averages(season_data, last5_data, last10_data, last20_data)
    upload_team_stats(team_list)
    upload_meta()

    elapsed = round(time.time() - start, 1)
    print("\n" + "=" * 60)
    log.info("KESZ! Futasi ido: %ss  |  %s", elapsed, datetime.now().strftime("%H:%M:%S"))
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
