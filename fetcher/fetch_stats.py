"""
╔══════════════════════════════════════════════════════════════════════╗
║   NBA Tippmix Tracker — Adatgyűjtő Backend Script                   ║
║   Adatforrás : stats.nba.com (direct requests, nba_api nélkül)      ║
║   Tárolás    : Firebase Firestore                                    ║
║   Futtatás   : python fetch_stats.py  (vagy GitHub Actions cron)    ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import os
import sys
import time
import random
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv

import requests
import firebase_admin
from firebase_admin import credentials, firestore

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
MIN_GAMES            = int(os.getenv("MIN_GAMES", "10"))
BATCH_SIZE           = 100

NBA_STATS_BASE = "https://stats.nba.com/stats"

# Teljes Chrome 125 fejléckészlet — kötelező sorrendben
NBA_HEADERS = {
    "Accept":              "application/json, text/plain, */*",
    "Accept-Encoding":     "gzip, deflate, br",
    "Accept-Language":     "en-US,en;q=0.9",
    "Cache-Control":       "no-cache",
    "Connection":          "keep-alive",
    "DNT":                 "1",
    "Origin":              "https://www.nba.com",
    "Pragma":              "no-cache",
    "Referer":             "https://www.nba.com/",
    "Sec-Ch-Ua":           '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
    "Sec-Ch-Ua-Mobile":    "?0",
    "Sec-Ch-Ua-Platform":  '"macOS"',
    "Sec-Fetch-Dest":      "empty",
    "Sec-Fetch-Mode":      "cors",
    "Sec-Fetch-Site":      "same-site",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "x-nba-stats-origin":  "stats",
    "x-nba-stats-token":   "true",
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

# ── Globális állapot ───────────────────────────────────────────────────────
db          = None
nba_session = None


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

def parse_result_set(raw, index=0):
    rs = raw["resultSets"][index]
    return [dict(zip(rs["headers"], row)) for row in rs["rowSet"]]


# ═══════════════════════════════════════════════════════════════════════════════
# NBA.COM SESSION + DIREKT API HÍVÁS
# ═══════════════════════════════════════════════════════════════════════════════

def init_nba_session():
    """
    Létrehoz egy requests.Session-t, majd először meglátogatja
    www.nba.com-ot, hogy megszerezze a session cookie-kat.
    Ez döntő fontosságú: a stats.nba.com ellenőrzi, hogy van-e
    aktív nba.com session a kérés előtt.
    """
    global nba_session
    nba_session = requests.Session()

    warmup_headers = {
        "User-Agent":      NBA_HEADERS["User-Agent"],
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection":      "keep-alive",
        "DNT":             "1",
        "Sec-Ch-Ua":       NBA_HEADERS["Sec-Ch-Ua"],
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest":  "document",
        "Sec-Fetch-Mode":  "navigate",
        "Sec-Fetch-Site":  "none",
        "Upgrade-Insecure-Requests": "1",
    }

    log.info("   Warmup: www.nba.com felkerese (cookie megszerzese)...")
    try:
        resp = nba_session.get(
            "https://www.nba.com",
            headers=warmup_headers,
            timeout=30,
            allow_redirects=True,
        )
        log.info("   Warmup OK: HTTP %d, %d cookie", resp.status_code, len(nba_session.cookies))
    except Exception as exc:
        log.warning("   Warmup sikertelen (%s) — folytatjuk cookie nelkul", str(exc)[:80])

    # Rövid szünet, mintha egy ember böngészne
    time.sleep(random.uniform(3.0, 6.0))


def nba_api_call(endpoint, description, params, timeout=60):
    """
    Direkt GET kérés a stats.nba.com-ra.
    Max 4 kísérlet, exponenciális + véletlen visszalépéssel.
    """
    global nba_session
    if nba_session is None:
        init_nba_session()

    url = f"{NBA_STATS_BASE}/{endpoint}"

    for attempt in range(1, 5):
        base_wait = random.uniform(6.0, 12.0) if attempt == 1 else random.uniform(20.0, 40.0)
        log.info("   %s (kiserlet %d/4, %.0fs varakozas)...", description, attempt, base_wait)
        time.sleep(base_wait)

        try:
            resp = nba_session.get(
                url,
                params=params,
                headers=NBA_HEADERS,
                timeout=timeout,
            )
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 60))
                log.warning("   Rate limit (429) — %ds varakozas...", retry_after)
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            return resp.json()

        except requests.exceptions.Timeout:
            wait = min(attempt * 25, 90)
            log.warning("   Timeout — %ds varakozas...", wait)
            if attempt == 4:
                log.error("   SIKERTELEN (timeout): %s", description)
                raise
            time.sleep(wait)

        except requests.exceptions.ConnectionError as exc:
            wait = min(attempt * 25, 90)
            log.warning("   Kapcsolati hiba: %s — %ds varakozas...", str(exc)[:80], wait)
            if attempt == 4:
                log.error("   SIKERTELEN (connection): %s", description)
                raise
            # Új session próbálkozás következő kísérlet előtt
            nba_session = None
            time.sleep(wait)

        except Exception as exc:
            wait = min(attempt * 20, 60)
            log.warning("   Hiba: %s — %ds varakozas...", str(exc)[:80], wait)
            if attempt == 4:
                log.error("   SIKERTELEN: %s", description)
                raise
            time.sleep(wait)


# ── Alap paraméter-sablon (minden üres mezőt ki kell tölteni) ─────────────

def _player_params(last_n=0, measure="Base", location="", season_type="Regular Season"):
    return {
        "College":        "",
        "Conference":     "",
        "Country":        "",
        "DateFrom":       "",
        "DateTo":         "",
        "Division":       "",
        "DraftPick":      "",
        "DraftYear":      "",
        "GameScope":      "",
        "GameSegment":    "",
        "Height":         "",
        "ISTRound":       "",
        "LastNGames":     last_n,
        "LeagueID":       "00",
        "Location":       location,
        "MeasureType":    measure,
        "Month":          0,
        "OpponentTeamID": 0,
        "Outcome":        "",
        "PORound":        0,
        "PaceAdjust":     "N",
        "PerMode":        "PerGame",
        "Period":         0,
        "PlayerExperience": "",
        "PlayerPosition": "",
        "PlusMinus":      "N",
        "Rank":           "N",
        "Season":         SEASON,
        "SeasonSegment":  "",
        "SeasonType":     season_type,
        "ShotClockRange": "",
        "StarterBench":   "",
        "TeamID":         0,
        "TwoWay":         0,
        "VsConference":   "",
        "VsDivision":     "",
        "Weight":         "",
    }

def _team_params(measure="Base"):
    return {
        "Conference":     "",
        "DateFrom":       "",
        "DateTo":         "",
        "Division":       "",
        "GameScope":      "",
        "GameSegment":    "",
        "ISTRound":       "",
        "LastNGames":     0,
        "LeagueID":       "00",
        "Location":       "",
        "MeasureType":    measure,
        "Month":          0,
        "OpponentTeamID": 0,
        "Outcome":        "",
        "PORound":        0,
        "PaceAdjust":     "N",
        "PerMode":        "PerGame",
        "Period":         0,
        "PlayerExperience": "",
        "PlayerPosition": "",
        "PlusMinus":      "N",
        "Rank":           "N",
        "Season":         SEASON,
        "SeasonSegment":  "",
        "SeasonType":     "Regular Season",
        "ShotClockRange": "",
        "StarterBench":   "",
        "TeamID":         0,
        "TwoWay":         0,
        "VsConference":   "",
        "VsDivision":     "",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# ADATKINYERŐ FÜGGVÉNYEK
# ═══════════════════════════════════════════════════════════════════════════════

def extract_avg(r):
    fgm  = safe_float(r.get("FGM",  0))
    fg3m = safe_float(r.get("FG3M", 0))
    fga  = safe_float(r.get("FGA",  0))
    fg3a = safe_float(r.get("FG3A", 0))
    return {
        "pts":        safe_float(r.get("PTS",  0)),
        "fgm":        fgm,
        "fga":        fga,
        "fg_pct":     safe_float((r.get("FG_PCT",  0) or 0) * 100),
        "fg2m":       safe_float(fgm - fg3m),
        "fg2a":       safe_float(fga - fg3a),
        "fg3m":       fg3m,
        "fg3a":       fg3a,
        "fg3_pct":    safe_float((r.get("FG3_PCT", 0) or 0) * 100),
        "ftm":        safe_float(r.get("FTM",  0)),
        "fta":        safe_float(r.get("FTA",  0)),
        "ft_pct":     safe_float((r.get("FT_PCT",  0) or 0) * 100),
        "oreb":       safe_float(r.get("OREB", 0)),
        "dreb":       safe_float(r.get("DREB", 0)),
        "reb":        safe_float(r.get("REB",  0)),
        "ast":        safe_float(r.get("AST",  0)),
        "stl":        safe_float(r.get("STL",  0)),
        "blk":        safe_float(r.get("BLK",  0)),
        "tov":        safe_float(r.get("TOV",  0)),
        "min":        safe_float(r.get("MIN",  0)),
        "plus_minus": safe_float(r.get("PLUS_MINUS", 0)),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 1. SZEZONÁTLAGOK
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_season_averages():
    raw  = nba_api_call(
        "leaguedashplayerstats",
        "Szezonátlagok (összes játékos)",
        _player_params(last_n=0),
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
# 2. UTOLSÓ N MECCS ÁTLAGAI  (Regular Season + Playoffs)
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_last_n_averages(n, season_type="Regular Season"):
    """leaguedashplayerstats LastNGames átlag. {player_id: avg_dict}"""
    label = "L%d atlagok (%s)" % (n, season_type)
    raw   = nba_api_call(
        "leaguedashplayerstats", label,
        _player_params(last_n=n, season_type=season_type),
    )
    rows   = parse_result_set(raw)
    result = {}
    for r in rows:
        result[str(r["PLAYER_ID"])] = extract_avg(r)
    log.info("   OK: %d jatekos %s", len(result), label)
    return result


def merge_rs_po(rs_data, po_data):
    """
    Összefésüli az alapszakasz és playoff adatokat.
    Ahol van playoff adat, az felülírja az alapszakasz értéket.
    """
    merged = dict(rs_data)
    merged.update(po_data)
    return merged


# ═══════════════════════════════════════════════════════════════════════════════
# 3. JÁTÉKOS MECCSNAPLÓ  (egyedi meccsek + pontos L5/L10 átlagok)
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_player_game_logs(n, season_type="Regular Season"):
    """
    playergamelogs endpoint — visszaadja az összes játékos utolsó N meccsét
    egyenként (nem átlagolva). {player_id: [game_dict, ...]}
    """
    label  = "Jatekos meccsnaplo L%d (%s)" % (n, season_type)
    params = {
        "DateFrom":       "",
        "DateTo":         "",
        "GameSegment":    "",
        "ISTRound":       "",
        "LastNGames":     n,
        "LeagueID":       "00",
        "Location":       "",
        "MeasureType":    "Base",
        "Month":          0,
        "OpponentTeamID": 0,
        "Outcome":        "",
        "PORound":        0,
        "PerMode":        "PerGame",
        "PlayerID":       0,
        "Season":         SEASON,
        "SeasonSegment":  "",
        "SeasonType":     season_type,
        "ShotClockRange": "",
        "VsConference":   "",
        "VsDivision":     "",
    }
    raw  = nba_api_call("playergamelogs", label, params)
    rows = parse_result_set(raw)

    result = {}
    for r in rows:
        pid = str(r["PLAYER_ID"])
        fgm  = safe_int(r.get("FGM",  0))
        fg3m = safe_int(r.get("FG3M", 0))
        fga  = safe_int(r.get("FGA",  0))
        fg3a = safe_int(r.get("FG3A", 0))
        game = {
            "game_date":  r.get("GAME_DATE", ""),
            "matchup":    r.get("MATCHUP",   ""),
            "wl":         r.get("WL",        ""),
            "min":        safe_float(r.get("MIN",        0)),
            "pts":        safe_int(  r.get("PTS",        0)),
            "fgm":        fgm,
            "fga":        fga,
            "fg2m":       fgm  - fg3m,
            "fg2a":       fga  - fg3a,
            "fg3m":       fg3m,
            "fg3a":       fg3a,
            "ftm":        safe_int(r.get("FTM",  0)),
            "fta":        safe_int(r.get("FTA",  0)),
            "oreb":       safe_int(r.get("OREB", 0)),
            "dreb":       safe_int(r.get("DREB", 0)),
            "reb":        safe_int(r.get("REB",  0)),
            "ast":        safe_int(r.get("AST",  0)),
            "stl":        safe_int(r.get("STL",  0)),
            "blk":        safe_int(r.get("BLK",  0)),
            "tov":        safe_int(r.get("TOV",  0)),
            "plus_minus": safe_int(r.get("PLUS_MINUS", 0)),
        }
        result.setdefault(pid, []).append(game)

    # Dátum szerint rendezés (legfrissebb elöl), max N megőrzése
    for pid in result:
        result[pid].sort(key=lambda g: g["game_date"], reverse=True)
        result[pid] = result[pid][:n]

    log.info("   OK: %d jatekos meccsnaplo betoltve (%s)", len(result), label)
    return result


def avg_from_games(games, n=None):
    """Pontosan kiszámolt átlag egyedi meccsekből."""
    if n:
        games = games[:n]
    if not games:
        return {}
    count = len(games)
    stat_keys = [
        "pts", "fgm", "fga", "fg2m", "fg2a", "fg3m", "fg3a",
        "ftm", "fta", "oreb", "dreb", "reb", "ast", "stl", "blk",
        "tov", "min", "plus_minus",
    ]
    result = {}
    for k in stat_keys:
        result[k] = safe_float(sum(g.get(k, 0) for g in games) / count)
    # fg_pct-ek
    result["fg_pct"]  = safe_float(result["fga"]  and result["fgm"]  / result["fga"]  * 100)
    result["fg3_pct"] = safe_float(result["fg3a"] and result["fg3m"] / result["fg3a"] * 100)
    result["ft_pct"]  = safe_float(result["fta"]  and result["ftm"]  / result["fta"]  * 100)
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# 4. HAZAI / VENDÉG ÁTLAGOK
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_location_averages(location):
    """Hazai (Home) vagy vendég (Road) PerGame atlagok. {player_id: {...}}"""
    raw  = nba_api_call(
        "leaguedashplayerstats",
        "%s atlagok" % location,
        _player_params(location=location),
    )
    rows = parse_result_set(raw)
    result = {}
    for r in rows:
        result[str(r["PLAYER_ID"])] = extract_avg(r)
    log.info("   OK: %d jatekos %s atlaga betoltve", len(result), location)
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# 4. FORMA-ALAPÚ O/U TRENDEK
# ═══════════════════════════════════════════════════════════════════════════════

def compute_ou_form(season_avg, l5_avg, l10_avg, l20_avg, games=None):
    """
    O/U forma kiszámítása.
    Ha adott az egyedi meccsek listája (games), valódi game-by-game hit rate-t számol.
    Különben ablak-átlag alapú közelítést használ (szezon/L20/L10/L5 átlagok vs vonal).
    """
    result = {}

    for stat, lines in OU_THRESHOLDS.items():
        result[stat] = {}
        for line in lines:
            key = str(line)

            if games:
                # ── Valódi meccs-alapú O/U ──────────────────────────────────
                l5g  = games[:5]
                l10g = games[:10]

                def _hit(gs):
                    return sum(1 for g in gs if g.get(stat, 0) > line)

                hit_l5  = _hit(l5g)
                hit_l10 = _hit(l10g)
                total   = len(l10g) if l10g else len(l5g)
                hit     = hit_l10   if l10g else hit_l5
                pct     = safe_float(hit / total * 100 if total else 0)

                def _avg(gs):
                    return safe_float(sum(g.get(stat, 0) for g in gs) / len(gs)) if gs else None

                avgs = {}
                if season_avg.get(stat) is not None: avgs["season"] = season_avg[stat]
                if l20_avg.get(stat)    is not None: avgs["l20"]    = l20_avg[stat]
                a10 = _avg(l10g)
                a5  = _avg(l5g)
                if a10 is not None: avgs["l10"] = a10
                if a5  is not None: avgs["l5"]  = a5

            else:
                # ── Ablak-átlag alapú fallback ───────────────────────────────
                windows = [
                    ("season", season_avg),
                    ("l20",    l20_avg),
                    ("l10",    l10_avg),
                    ("l5",     l5_avg),
                ]
                avgs      = {wn: wd.get(stat) for wn, wd in windows if wd.get(stat) is not None}
                hit       = sum(1 for v in avgs.values() if v > line)
                total     = len(avgs)
                pct       = safe_float(hit / total * 100 if total else 0)

            if pct >= 70:   trend = "hot"
            elif pct >= 55: trend = "good"
            elif pct >= 45: trend = "neutral"
            else:           trend = "cold"

            result[stat][key] = {
                "pct":        pct,
                "hit":        hit,
                "total":      total,
                "over_count": hit,
                "avgs":       avgs,
                "trend":      trend,
            }
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# 5. CSAPATSTATISZTIKÁK
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_team_stats():
    raw_base = nba_api_call("leaguedashteamstats", "Csapat alapstatisztikák",  _team_params("Base"))
    raw_adv  = nba_api_call("leaguedashteamstats", "Csapat haladó statisztikák", _team_params("Advanced"))
    raw_opp  = nba_api_call("leaguedashteamstats", "Ellenfél statisztikák",    _team_params("Opponent"))

    def to_map(raw):
        rows = parse_result_set(raw)
        return {str(r["TEAM_ID"]): r for r in rows}

    base_map = to_map(raw_base)
    adv_map  = to_map(raw_adv)
    opp_map  = to_map(raw_opp)

    team_list = []
    for tid, b in base_map.items():
        a         = adv_map.get(tid, {})
        o         = opp_map.get(tid, {})
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
            "opp_fg_pct":         safe_float((o.get("OPP_FG_PCT",  0) or 0) * 100),
            "opp_fg3_pct":        safe_float((o.get("OPP_FG3_PCT", 0) or 0) * 100),
            "opp_ast":            safe_float(o.get("OPP_AST", 0)),
            "opp_reb":            safe_float(o.get("OPP_REB", 0)),
            "opp_tov":            safe_float(o.get("OPP_TOV", 0)),
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
# 6. MAI MECCSEK + B2B AZONOSÍTÁS
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_schedule_b2b():
    """
    Mai meccsek lekérése és B2B csapatok azonosítása.
    Ha egy csapat tegnap is játszott, ma B2B-n van.
    """
    from datetime import timedelta

    today     = datetime.now()
    yesterday = today - timedelta(days=1)

    def get_games(date_obj, label):
        date_str = date_obj.strftime("%m/%d/%Y")
        try:
            raw = nba_api_call(
                "scoreboardv2",
                "%s meccsek (%s)" % (label, date_str),
                {"DayOffset": 0, "GameDate": date_str, "LeagueID": "00"},
                timeout=30,
            )
            rs = raw.get("resultSets", [])
            game_header = next(
                (x for x in rs if x.get("name") == "GameHeader"), None
            )
            if not game_header or not game_header.get("rowSet"):
                return []
            return [dict(zip(game_header["headers"], row))
                    for row in game_header["rowSet"]]
        except Exception as exc:
            log.warning("   Schedule fetch sikertelen (%s): %s", label, str(exc)[:80])
            return []

    yesterday_games = get_games(yesterday, "tegnapi")
    today_games     = get_games(today,     "mai")

    b2b_ids = set()
    for g in yesterday_games:
        b2b_ids.add(str(g.get("HOME_TEAM_ID",    "")))
        b2b_ids.add(str(g.get("VISITOR_TEAM_ID", "")))
    b2b_ids.discard("")

    games        = []
    b2b_team_ids = []
    for g in today_games:
        home_id = str(g.get("HOME_TEAM_ID",    ""))
        away_id = str(g.get("VISITOR_TEAM_ID", ""))
        home_b2b = home_id in b2b_ids
        away_b2b = away_id in b2b_ids
        if home_b2b: b2b_team_ids.append(home_id)
        if away_b2b: b2b_team_ids.append(away_id)
        games.append({
            "game_id":      g.get("GAME_ID", ""),
            "home_team_id": home_id,
            "away_team_id": away_id,
            "status":       g.get("GAME_STATUS_TEXT", "").strip(),
            "b2b_home":     home_b2b,
            "b2b_away":     away_b2b,
        })

    log.info("   OK: %d mai meccs, %d B2B csapat", len(games), len(set(b2b_team_ids)))
    return {
        "date":         today.strftime("%Y-%m-%d"),
        "games":        games,
        "b2b_team_ids": list(set(b2b_team_ids)),
        "updated_at":   datetime.now(timezone.utc).isoformat(),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 7. FIREBASE FELTÖLTÉS
# ═══════════════════════════════════════════════════════════════════════════════

def commit_batch_with_retry(batch, label="batch"):
    for attempt in range(1, 5):
        try:
            batch.commit()
            return
        except Exception as exc:
            wait = attempt * 5
            log.warning("   Firebase commit hiba (%s, kiserlet %d/4): %s — %ds varakozas",
                        label, attempt, str(exc)[:80], wait)
            if attempt == 4:
                raise
            time.sleep(wait)


def upload_player_averages(season_data, l5_data, l10_data, l20_data,
                           home_data=None, road_data=None, game_logs=None):
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

        l5_avg  = l5_data.get(pid,  {})
        l10_avg = l10_data.get(pid, {})
        l20_avg = l20_data.get(pid, {})

        recent = (game_logs or {}).get(pid, [])

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
            "home_avg":          (home_data or {}).get(pid, {}),
            "road_avg":          (road_data or {}).get(pid, {}),
            "recent_games":      recent[:10],
            "ou_form":           compute_ou_form(
                                     s["season_avg"], l5_avg, l10_avg, l20_avg,
                                     games=recent if recent else None
                                 ),
            "updated_at":        now_iso,
            "season":            SEASON,
        }

        batch.set(db_ref.document(pid), doc)
        count += 1

        if count % BATCH_SIZE == 0:
            commit_batch_with_retry(batch, label=f"{count} jatekos")
            log.info("   ... %d jatekos feltoltve", count)
            batch = db.batch()
            time.sleep(1.0)

    commit_batch_with_retry(batch, label="vegso batch")
    log.info("   OK: %d jatekos feltoltve, %d kihagyva (keves meccs)", count, skipped)


def upload_team_stats(team_list):
    log.info("Csapat adatok feltoltese Firebase-be...")
    batch = db.batch()
    for team in team_list:
        batch.set(db.collection("team_stats").document(team["team_id"]), team)
    commit_batch_with_retry(batch, label="team_stats")
    log.info("   OK: %d csapat feltoltve", len(team_list))


def upload_schedule(data):
    db.collection("schedule").document("today").set(data)
    log.info("   OK: %d mai meccs, B2B csapatok: %s",
             len(data["games"]), data["b2b_team_ids"] or "nincs")


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

    log.info("=== NBA SESSION INIT ===")
    init_nba_session()

    log.info("=== ADATLETOLTES ===")
    season_data = fetch_season_averages()

    # RS alapszakasz L5/L10/L20
    rs_l5  = fetch_last_n_averages(5,  "Regular Season")
    rs_l10 = fetch_last_n_averages(10, "Regular Season")
    rs_l20 = fetch_last_n_averages(20, "Regular Season")

    # Playoff L5/L10 — ha van, felülírja az RS adatot (playoff játékosoknál aktuálisabb)
    try:
        po_l5  = fetch_last_n_averages(5,  "Playoffs")
        po_l10 = fetch_last_n_averages(10, "Playoffs")
        l5_data  = merge_rs_po(rs_l5,  po_l5)
        l10_data = merge_rs_po(rs_l10, po_l10)
        log.info("   Playoff L5/L10 beolvasztva: %d / %d jatekos", len(po_l5), len(po_l10))
    except Exception as exc:
        log.warning("   Playoff atlagok nem elerhetok (%s) — csak RS adat", str(exc)[:80])
        l5_data  = rs_l5
        l10_data = rs_l10
    l20_data = rs_l20

    # ── Játékos meccsnapló: egyedi meccsek a modal táblához + pontos L5/L10 ──
    game_logs = {}
    try:
        log.info("   Jatekos meccsnaplo letoltese (RS L10)...")
        rs_logs = fetch_player_game_logs(10, "Regular Season")
        game_logs = dict(rs_logs)

        # Playoff meccsnaplók, ha vannak
        try:
            log.info("   Jatekos meccsnaplo letoltese (Playoffs L10)...")
            po_logs = fetch_player_game_logs(10, "Playoffs")
            game_logs.update(po_logs)           # PO felülírja RS-t
            log.info("   Playoff game logs beolvasztva: %d jatekos", len(po_logs))
        except Exception as po_exc:
            log.warning("   Playoff game logs nem elerhetok (%s)", str(po_exc)[:80])

        # Pontos L5 / L10 átlagok a valódi meccsekből
        gl_l5  = {pid: avg_from_games(games, 5)  for pid, games in game_logs.items()}
        gl_l10 = {pid: avg_from_games(games, 10) for pid, games in game_logs.items()}

        # Kiegészítés: akikre nincs game log, ott marad a leaguedashplayerstats adat
        for pid in l5_data:
            if pid not in gl_l5:
                gl_l5[pid] = l5_data[pid]
        for pid in l10_data:
            if pid not in gl_l10:
                gl_l10[pid] = l10_data[pid]

        l5_data  = gl_l5
        l10_data = gl_l10
        log.info("   Pontos L5/L10 atlagok kiszamitva %d jatekosra", len(gl_l10))

    except Exception as exc:
        log.warning("   Game logs fetch sikertelen (%s) — leaguedashplayerstats adat hasznalva", str(exc)[:80])
        game_logs = {}

    home_data   = fetch_location_averages("Home")
    road_data   = fetch_location_averages("Road")
    team_list   = fetch_team_stats()
    schedule    = fetch_schedule_b2b()

    log.info("=== FIREBASE FELTOLTES ===")
    upload_player_averages(season_data, l5_data, l10_data, l20_data,
                           home_data, road_data, game_logs)
    upload_team_stats(team_list)
    upload_schedule(schedule)
    upload_meta()

    elapsed = round(time.time() - start, 1)
    print("\n" + "=" * 60)
    log.info("KESZ! Futasi ido: %ss  |  %s", elapsed, datetime.now().strftime("%H:%M:%S"))
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
