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

def _player_params(last_n=0, measure="Base", location=""):
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
        "SeasonType":     "Regular Season",
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
# 2. UTOLSÓ N MECCS ÁTLAGAI
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_all_game_logs():
    """
    Liga-szintű meccsnapló EGYETLEN API hívással (az összes játékos, az egész szezon).
    Visszaad: { player_id: [game_dict, ...] } — DATE DESC sorrendben.
    Ebből számítjuk a L5/L10/L20 átlagokat, recent_games-t és ou_form-ot.
    """
    raw = nba_api_call(
        "leaguegamelog",
        "Liga-szintu meccsnaplo (teljes szezon)",
        {
            "Counter":      0,
            "DateFrom":     "",
            "DateTo":       "",
            "Direction":    "DESC",
            "LeagueID":     "00",
            "PlayerOrTeam": "P",
            "Season":       SEASON,
            "SeasonType":   "Regular Season",
            "Sorter":       "DATE",
        },
        timeout=120,
    )
    rows = parse_result_set(raw)

    result = {}
    for r in rows:
        pid = str(r.get("PLAYER_ID", ""))
        if not pid:
            continue
        if pid not in result:
            result[pid] = []
        fgm  = safe_float(r.get("FGM",  0))
        fg3m = safe_float(r.get("FG3M", 0))
        fga  = safe_float(r.get("FGA",  0))
        fg3a = safe_float(r.get("FG3A", 0))
        result[pid].append({
            "game_date":  r.get("GAME_DATE", ""),
            "matchup":    r.get("MATCHUP", ""),
            "wl":         r.get("WL", ""),
            "min":        safe_float(r.get("MIN",  0)),
            "pts":        safe_float(r.get("PTS",  0)),
            "fg2m":       safe_float(fgm  - fg3m),
            "fg2a":       safe_float(fga  - fg3a),
            "fg3m":       fg3m,
            "fg3a":       fg3a,
            "oreb":       safe_float(r.get("OREB", 0)),
            "dreb":       safe_float(r.get("DREB", 0)),
            "reb":        safe_float(r.get("REB",  0)),
            "ast":        safe_float(r.get("AST",  0)),
            "stl":        safe_float(r.get("STL",  0)),
            "blk":        safe_float(r.get("BLK",  0)),
            "tov":        safe_float(r.get("TOV",  0)),
            "plus_minus": safe_int(r.get("PLUS_MINUS", 0)),
        })

    log.info("   OK: %d jatekos meccs-naploja betoltve, ossz %d rekord", len(result), len(rows))
    return result


def avg_from_games(games, n):
    """Utolsó N meccs per-game átlaga (games: DATE DESC sorrendben)."""
    subset = games[:n]
    count  = len(subset)
    if count == 0:
        return {}
    keys = ["pts", "fg2m", "fg2a", "fg3m", "fg3a", "oreb", "dreb", "reb",
            "ast", "stl", "blk", "tov", "min"]
    return {k: safe_float(sum(g.get(k, 0) or 0 for g in subset) / count) for k in keys}


# ═══════════════════════════════════════════════════════════════════════════════
# 3. HAZAI / VENDÉG ÁTLAGOK
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_location_averages(location):
    """Hazai (Home) vagy vendég (Road) PerGame átlagok. {player_id: {...}}"""
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

def compute_ou_form(games, season_avg, l5_avg, l10_avg, l20_avg):
    """
    Valódi game-by-game O/U hit rate a teljes game log-ból.
    A 'avgs' mező tartalmazza az ablak-átlagokat (EV kalkulátorhoz).
    Struktúra: { stat: { line_str: { pct, hit, total, windows, over_count, avgs, trend } } }
    """
    result = {}
    avgs_windows = [
        ("season", season_avg),
        ("l20",    l20_avg),
        ("l10",    l10_avg),
        ("l5",     l5_avg),
    ]

    for stat, lines in OU_THRESHOLDS.items():
        result[stat] = {}
        game_vals = [g.get(stat, 0) or 0 for g in games]
        total     = len(game_vals)

        for line in lines:
            key = str(line)

            hit = sum(1 for v in game_vals if v > line)
            pct = safe_float(hit / total * 100 if total else 0)

            # Ablak-átlagok az EV kalkulátorhoz (season / l20 / l10 / l5)
            avgs = {}
            for wname, wdata in avgs_windows:
                val = wdata.get(stat)
                if val is not None:
                    avgs[wname] = val

            if pct >= 70:   trend = "hot"
            elif pct >= 55: trend = "good"
            elif pct >= 45: trend = "neutral"
            else:           trend = "cold"

            result[stat][key] = {
                "pct":        pct,
                "hit":        hit,
                "total":      total,
                "over_count": hit,    # alias — EV kalkulator kompatibilitás
                "windows":    total,  # alias — EV kalkulator kompatibilitás
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


def upload_player_averages(season_data, game_logs,
                           home_data=None, road_data=None):
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

        games   = game_logs.get(pid, [])          # DATE DESC
        l5_avg  = avg_from_games(games, 5)
        l10_avg = avg_from_games(games, 10)
        l20_avg = avg_from_games(games, 20)

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
            "recent_games":      games[:20],
            "ou_form":           compute_ou_form(
                                     games,
                                     s["season_avg"], l5_avg, l10_avg, l20_avg
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
    game_logs   = fetch_all_game_logs()     # 1 hívás helyettesíti a 3 LastNGames hívást
    home_data   = fetch_location_averages("Home")
    road_data   = fetch_location_averages("Road")
    team_list   = fetch_team_stats()
    schedule    = fetch_schedule_b2b()

    log.info("=== FIREBASE FELTOLTES ===")
    upload_player_averages(season_data, game_logs, home_data, road_data)
    upload_team_stats(team_list)
    upload_schedule(schedule)
    upload_meta()

    elapsed = round(time.time() - start, 1)
    print("\n" + "=" * 60)
    log.info("KESZ! Futasi ido: %ss  |  %s", elapsed, datetime.now().strftime("%H:%M:%S"))
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
