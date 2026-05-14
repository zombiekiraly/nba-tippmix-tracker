# 🏀 NBA Tippmix Tracker

> Automatikus NBA statisztika-gyűjtő és fogadás-elemző dashboard —  
> **ingyenes API + Firebase Firestore + GitHub Actions**

---

## 📋 Tartalomjegyzék

1. [Architektúra áttekintés](#architektúra)
2. [Előfeltételek](#előfeltételek)
3. [Firebase beállítása](#1-firebase-beállítása)
4. [Projekt klónozása](#2-projekt-klónozása)
5. [Helyi futtatás](#3-helyi-futtatás)
6. [GitHub Actions automatizálás](#4-github-actions-automatizálás)
7. [Frontend megnyitása](#5-frontend-megnyitása)
8. [Firestore biztonsági szabályok](#6-firestore-biztonsági-szabályok)
9. [Statisztikák magyarázata](#statisztikák-magyarázata)
10. [Hibaelhárítás](#hibaelhárítás)

---

## Architektúra

```
┌─────────────────────────────────────────────────────────────────┐
│                        ADATFOLYAM                               │
│                                                                 │
│  stats.nba.com  ──►  Python fetcher  ──►  Firebase Firestore   │
│  (nba_api pkg)        (fetch_stats.py)    (player_averages,    │
│  INGYENES, kulcs       fut: helyi /        team_stats)         │
│  nélkül!              GitHub Actions)                           │
│                                │                                │
│                                ▼                                │
│                     HTML/JS Dashboard                           │
│                     (böngészőből nyitható,                      │
│                      Firebase SDK-val olvas)                    │
└─────────────────────────────────────────────────────────────────┘
```

### Adatforrás: `nba_api` Python csomag
- **Teljesen ingyenes**, nincs API kulcs
- Közvetlenül a `stats.nba.com` végpontjait éri el
- Végpontok: `LeagueDashPlayerStats`, `PlayerGameLogs`, `LeagueDashTeamStats`

### Firestore kollekciók
| Kollekció | Tartalom | Dokumentumok száma |
|---|---|---|
| `player_averages` | Szezon/L5/L10 átlagok + O/U trendek | ~400 aktív játékos |
| `team_stats` | Csapat statok + Pace + Rating | 30 csapat |
| `meta` | Utolsó frissítés időpontja | 1 |

---

## Előfeltételek

- **Python 3.11+** (`python --version`)
- **Git** (`git --version`)
- **Google-fiók** (Firebase-hez)
- **GitHub-fiók** (Actions automatizáláshoz)

---

## 1. Firebase beállítása

### 1.1 — Projekt létrehozása

1. Nyisd meg a [Firebase Console-t](https://console.firebase.google.com/)
2. Kattints **„Add project"** → Add nevet (pl. `nba-tippmix-tracker`)
3. Google Analytics: **letilthatod** (nem szükséges)
4. Kattints **„Create project"**

### 1.2 — Firestore adatbázis aktiválása

1. Bal oldali menü → **Firestore Database**
2. **„Create database"**
3. Mód: **Production mode** (biztonságos; szabályokat később állítjuk be)
4. Régió: `eur3 (europe-west)` (Magyarországhoz legközelebbi)
5. **„Enable"**

### 1.3 — Service Account letöltése (backend-hez)

1. **Project Settings** (fogaskerék ikon) → **Service accounts** fül
2. **„Generate new private key"** → **„Generate key"**
3. Mentsd el a letöltött `serviceAccountKey.json` fájlt
4. Másold a `fetcher/` mappába és **nevezd át**: `serviceAccount.json`

> ⚠️ **FONTOS:** Soha ne töltsd fel ezt a fájlt GitHub-ra!  
> A `.gitignore` már tartalmazza a kizárását.

### 1.4 — Web App konfiguráció (frontend-hez)

1. **Project Settings** → **General** fül → görgesd le a **„Your apps"** részhez
2. Kattints `</>` (Web) ikonra
3. App neve: pl. `nba-dashboard` → **„Register app"**
4. Másold ki a `firebaseConfig` objektumot:

```javascript
const firebaseConfig = {
  apiKey: "AIzaSy...",
  authDomain: "nba-tippmix-tracker.firebaseapp.com",
  projectId: "nba-tippmix-tracker",
  storageBucket: "nba-tippmix-tracker.appspot.com",
  messagingSenderId: "123456789",
  appId: "1:123456789:web:abc..."
};
```

5. Nyisd meg a `frontend/index.html` fájlt
6. Keresd meg a `⚠️ IDE ÍRD BE A SAJÁT FIREBASE KONFIGURÁCIÓDAT` részt
7. Cseréld le a placeholder értékeket a fentiekre

---

## 2. Projekt klónozása

```bash
# GitHub repo létrehozása és klónozása
git clone https://github.com/FELHASZNALONEVED/nba-tippmix-tracker.git
cd nba-tippmix-tracker
```

**Vagy töltsd le ZIP-ként** és helyezd a megfelelő könyvtárba.

A projekt struktúrája:
```
nba-tippmix-tracker/
├── fetcher/
│   ├── fetch_stats.py        ← Python adatgyűjtő script
│   ├── requirements.txt      ← Python függőségek
│   ├── .env.example          ← Környezeti változók sablon
│   └── serviceAccount.json   ← ⚠️ IDE MÁSOLD (ne commitold!)
├── frontend/
│   ├── index.html            ← Dashboard főoldal
│   ├── style.css             ← Stíluslap
│   └── app.js                ← Kiegészítő JS
├── .github/
│   └── workflows/
│       └── daily_fetch.yml   ← GitHub Actions workflow
└── .gitignore
```

---

## 3. Helyi futtatás

### 3.1 — Python környezet

```bash
cd fetcher

# Virtuális környezet (ajánlott)
python -m venv venv

# Aktiválás
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

# Függőségek telepítése
pip install -r requirements.txt
```

### 3.2 — Környezeti változók

```bash
# Másolj a sablonból
cp .env.example .env

# Nyisd meg és állítsd be (ha szükséges)
# NBA_SEASON=2025-26
# REQUEST_DELAY=1.5
# MIN_GAMES=10
# FIREBASE_SERVICE_ACCOUNT=serviceAccount.json
```

### 3.3 — Script futtatása

```bash
python fetch_stats.py
```

**Várható kimenet:**
```
════════════════════════════════════════════════════════════
  🏀  NBA Tippmix Tracker — Adatfrissítés
  Szezon: 2025-26  |  2026-05-14 10:30
════════════════════════════════════════════════════════════

08:30:01  INFO    ⬇  Szezonátlagok (összes játékos) (kísérlet 1/3)…
08:30:04  INFO       ✓ 487 játékos szezonátlaga betöltve
08:30:05  INFO    ⬇  Utolsó 5 meccs átlagok (kísérlet 1/3)…
08:30:08  INFO       ✓ 487 játékos L5 átlaga betöltve
...
08:31:45  INFO    ✅ Kész! Futási idő: 104.3s
════════════════════════════════════════════════════════════
```

> ⏱️ **Becsült futási idő:** 2–4 perc (NBA.com rate limit miatt)

---

## 4. GitHub Actions automatizálás

### 4.1 — Repo feltöltése GitHubra

```bash
git init
git add .
git commit -m "Initial commit: NBA Tippmix Tracker"
git branch -M main

# Hozz létre egy új repot github.com-on, majd:
git remote add origin https://github.com/FELHASZNALONEVED/nba-tippmix-tracker.git
git push -u origin main
```

### 4.2 — Firebase Secret beállítása

1. GitHub repód → **Settings** → **Secrets and variables** → **Actions**
2. **„New repository secret"**
3. Név: `FIREBASE_SERVICE_ACCOUNT_JSON`
4. Érték: a `serviceAccount.json` fájl **teljes tartalma** (másold be a JSON-t)
5. **„Add secret"**

### 4.3 — Workflow aktiválása

A `.github/workflows/daily_fetch.yml` automatikusan fut:
- **Naponta 08:00 UTC** (10:00 CEST) — az éjszakai meccsek után
- **Kézi indítás**: GitHub → Actions fül → „NBA Napi Adatfrissítés" → „Run workflow"

> 💡 **Ingyenes futtatás:** Publikus repókon a GitHub Actions **korlátlan** és ingyenes!

---

## 5. Frontend megnyitása

### Egyszerű módszer (helyi fájlként)

```bash
# Egyszerűen nyisd meg böngészőben:
open frontend/index.html        # macOS
start frontend/index.html       # Windows
xdg-open frontend/index.html    # Linux
```

> ⚠️ **Megjegyzés:** Firebase SDK helyi fájlból is működik,  
> de ha problémád van, használj lokális szervert:

```bash
# Python beépített szerver
cd frontend
python -m http.server 8080

# Majd nyisd meg: http://localhost:8080
```

### GitHub Pages-en (ingyenes hosztolás)

1. GitHub repód → **Settings** → **Pages**
2. Source: **Deploy from a branch**
3. Branch: `main` / `/ (root)` → de ha a `frontend/` mappából szeretnéd:
   - Mozgasd az `index.html`, `style.css`, `app.js` fájlokat a repo gyökerébe  
   - Vagy állíts be egy `/docs` mappát
4. **Save** → néhány perc múlva elérhető: `https://felhasznaloneved.github.io/nba-tippmix-tracker`

---

## 6. Firestore biztonsági szabályok

A Firebase Console-ban → **Firestore** → **Rules** fülön állítsd be:

```javascript
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {

    // Olvasás: mindenki számára engedélyezett (publikus dashboard)
    // Írás: csak a backend service account (Admin SDK) által
    match /player_averages/{document=**} {
      allow read: if true;
      allow write: if false;  // csak Admin SDK írhat
    }
    match /team_stats/{document=**} {
      allow read: if true;
      allow write: if false;
    }
    match /meta/{document=**} {
      allow read: if true;
      allow write: if false;
    }
  }
}
```

> 🔒 Az Admin SDK (service account) a szabályokat megkerüli — csak a böngészős JS SDK korlátozódik.

---

## Statisztikák magyarázata

### Játékos statisztikák

| Rövidítés | Magyar neve | Fogadási relevancia |
|---|---|---|
| **PTS** | Pontok | Leggyakoribb prop fogadás alapja |
| **2PM / 2PA** | 2 pontos dobás (sikeres/kísérlet) | Bentszorult dobás teljesítmény |
| **3PM / 3PA** | 3 pontos dobás (sikeres/kísérlet) | Kiemelt prop vonal |
| **AST** | Asszisztok | Playmaker prop fogadás |
| **OREB** | Támadólepattanó | Belsős játékos mutató |
| **DREB** | Védőlepattanó | Általános REB alap |
| **REB** | Összes lepattanó | Klasszikus prop vonal |
| **TOV** | Labdavesztés | Negatív mutató |
| **L5 / L10** | Utolsó 5 / 10 meccs átlaga | **Formamutató** — fogadásnál kulcsfontosságú |

### Csapat statisztikák (fogadási szempontból)

| Mutató | Fogadási cél |
|---|---|
| **PTS/G** | Csapat pontok prop |
| **OPP/G** | Ellenfél pontok (defense szintje) |
| **TOTAL** | Meccs összpontszám → Over/Under vonal |
| **PACE** | Tempó → magas pace = több O/U lehetőség |
| **OffRtg / DefRtg** | Hatékonyság (100 birtolásonként) |
| **NetRtg** | Csapat erőssége — élő fogadáshoz |

### O/U trendek értelmezése

```
≥70%  🔥  Erős OVER tendencia — megbízható fogadási szignál
60–69% ✅  OVER valószínű — érdemes figyelni
40–59% ⚖️  Semleges — nincs egyértelmű irány
30–40% ❄️  UNDER hajlamos — óvatosság ajánlott
≤30%  🧊  Erős UNDER tendencia
```

> ⚠️ **Felelősségkizárás:** A statisztikai trendek historikus adatok —  
> nem garantálnak jövőbeli eredményt. Felelős fogadást ajánlunk!

---

## Hibaelhárítás

### ❌ `ModuleNotFoundError: No module named 'nba_api'`
```bash
pip install nba_api firebase-admin python-dotenv
```

### ❌ `HTTPError: 429 Too Many Requests`
Növeld a késleltetést a `.env` fájlban:
```
REQUEST_DELAY=3.0
```

### ❌ `FileNotFoundError: serviceAccount.json`
Helyezd a Firebase service account JSON-t a `fetcher/` mappába `serviceAccount.json` névvel.

### ❌ Firebase `PERMISSION_DENIED` a böngészőben
Ellenőrizd a Firestore biztonsági szabályokat (6. pont) — a `read: if true` beállítás szükséges.

### ❌ `NoneType` hiba a statisztikáknál
Az NBA.com időnként üres mezőket ad vissza — a script kezel ezeket (`safe_float()`, `safe_int()`).  
Ha az egész lekérdezés sikertelen, várj 10 percet és próbáld újra.

### ❌ A GitHub Actions workflow nem fut
1. Győződj meg róla, hogy a `FIREBASE_SERVICE_ACCOUNT_JSON` secret be van állítva
2. Publikus repón az Actions ingyenes, privátban ellenőrizd a kvótát
3. Actions → workflow → „Run workflow" gombbal kézzel is indítható

---

## Fejlesztési lehetőségek

- [ ] **Értesítések:** GitHub Actions → email/Telegram értesítés fogadási szignálokról
- [ ] **Több API forrás:** `balldontlie.io` ALL-STAR ($9.99/hó) a valós idejű adatokért
- [ ] **Sportsradar odds:** fogadási odds integráció
- [ ] **Historikus összehasonlítás:** ellenfél-specifikus statisztikák
- [ ] **Mobilapp:** Progressive Web App (PWA) wrapper

---

*Utoljára frissítve: 2026-05-14*
