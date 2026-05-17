"""
WAMP probe script – csatlakozik a TippMixPro sportsapi-hoz
és megnézi milyen adatokat küld.

Futtatás: python probe_wamp.py
Szükséges: pip install websocket-client
"""

import json
import time
import websocket

URL   = "wss://sportsapi.tippmixpro.hu/v2"
REALM = "www.tippmixpro.hu"

HELLO = [
    1,
    REALM,
    {
        "agent": "Wampy.js v6.2.2",
        "roles": {
            "publisher":  {"features": {"subscriber_blackwhite_listing": True, "publisher_exclusion": True}},
            "subscriber": {"features": {"pattern_based_subscription": True}},
            "caller":     {"features": {"caller_identification": True, "progressive_call_results": True, "call_canceling": True}},
            "callee":     {"features": {"caller_identification": True, "pattern_based_registration": True}},
        },
        "authmethods": ["wampcra"],
        "authid": "webapi-wampy",
    },
]

# Próba-feliratkozások (topic ötletek az URL struktúra alapján)
SUBSCRIBE_TOPICS = [
    "/registrationDismissed",
    "/sports/events",
    "/sport#getEvents",
    "/events",
    "/event",
    "com.tippmixpro.sports",
]

# Próba CALL eljárások
CALL_PROCEDURES = [
    "/sport#getEvents",
    "/sport#getEvent",
    "/events#getById",
    "/events#getBySport",
    "/sports#getEvents",
    "/sports#getEvent",
]

req_id = 10

def on_open(ws):
    print(f"[+] Kapcsolat létrejött: {URL}")
    print(f"[>] HELLO küldése (realm={REALM})...")
    ws.send(json.dumps(HELLO))

def on_message(ws, message):
    global req_id
    try:
        msg = json.loads(message)
    except Exception:
        print(f"[?] Nem JSON üzenet: {message[:100]}")
        return

    msg_type = msg[0] if msg else None
    print(f"\n[<] Üzenet type={msg_type}: {json.dumps(msg)[:400]}")

    # WELCOME (2) → kezdjük a próbákat
    if msg_type == 2:
        print("\n[+] WELCOME kapva – server elfogadta a kapcsolatot!")
        print("[>] Próba SUBSCRIBE-ok küldése...")
        for topic in SUBSCRIBE_TOPICS:
            sub_msg = [32, req_id, {}, topic]
            print(f"    SUBSCRIBE [{req_id}] → {topic}")
            ws.send(json.dumps(sub_msg))
            req_id += 1
            time.sleep(0.2)

        print("\n[>] Próba CALL-ok küldése...")
        for proc in CALL_PROCEDURES:
            call_msg = [48, req_id, {}, proc]
            print(f"    CALL [{req_id}] → {proc}")
            ws.send(json.dumps(call_msg))
            req_id += 1
            time.sleep(0.3)

    # CHALLENGE (4) → autentikáció szükséges
    elif msg_type == 4:
        print("\n[!] CHALLENGE – a szerver hitelesítést kér!")
        print(f"    Method: {msg[1]}, Extra: {msg[2]}")
        print("    → A sportsapi nem nyilvánosan elérhető, belépés kell.")

    # SUBSCRIBED (33) → sikeres feliratkozás
    elif msg_type == 33:
        print(f"    [✓] SUBSCRIBED: req={msg[1]} → sub_id={msg[2]}")

    # ERROR (8)
    elif msg_type == 8:
        print(f"    [✗] ERROR: {json.dumps(msg)}")

    # RESULT (50)
    elif msg_type == 50:
        print(f"    [✓] RESULT: {json.dumps(msg[4])[:500] if len(msg) > 4 else msg}")

    # EVENT (36)
    elif msg_type == 36:
        print(f"    [EVENT] sub_id={msg[1]}: {json.dumps(msg[4])[:500] if len(msg) > 4 else msg}")

def on_error(ws, error):
    print(f"[!] Hiba: {error}")

def on_close(ws, code, msg):
    print(f"[-] Kapcsolat lezárva (code={code})")

if __name__ == "__main__":
    print("=" * 60)
    print("TippMixPro WAMP sportsapi próba")
    print("=" * 60)

    ws = websocket.WebSocketApp(
        URL,
        subprotocols=["wamp.2.json"],
        header={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    print(f"Csatlakozás: {URL}")
    ws.run_forever(
        ping_interval=30,
        origin="https://www.tippmixpro.hu",   # <-- kritikus
    )
