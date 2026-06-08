# Code Review Findings

Gefundene Schwächen im Code — priorisiert nach Risiko.

---

## 🔴 Mittel — Designfehler

### 1. `strategy.py` — `build_plan` ist nicht wirklich pure

**Datei:** `collectorcrypt/trader/strategy.py`, Zeile ~325

```python
cand.resell_usd = resale  # mutiert das Candidate-Objekt in-place
```

Das Modul behauptet in seinem Docstring "pure, side-effect-free", mutiert aber
`Candidate`-Objekte direkt innerhalb von `build_plan`. Das macht die Funktion
unpredictable wenn dieselbe Kandidatenliste mehrfach übergeben wird.

**Fix:** `resell_usd` beim Erstellen des `Candidate` setzen (in `_economics`)
oder eine Kopie anlegen bevor mutiert wird.

---

## 🟡 Niedrig — Ressourcen

### 2. `api.py` — Cache ohne Size-Limit

**Datei:** `collectorcrypt/api.py`, `_cache` dict

```python
self._cache: dict[tuple, tuple[float, Any]] = {}
```

Der Cache wächst unbegrenzt. Bei einem langen Scanner-Lauf (viele Seiten,
viele Kategorien) akkumuliert sich der gesamte API-Response-Verlauf im RAM.
TTL-Expiry wird nur beim Lesen geprüft — abgelaufene Einträge werden nie
aktiv entfernt.

**Fix:** Max-Größe einführen (z. B. `maxsize=500`) und/oder einen
periodischen Cleanup-Sweep beim `_cache_set`.

---

## 🟡 Niedrig — Defensiv

### 3. `risk.py` — Config-Fehler fallen lautlos auf 0 zurück

**Datei:** `collectorcrypt/trader/risk.py`, `_limits()`

```python
getattr(self._cfg, "max_open_positions", 0)
```

Wenn ein Config-Attribut falsch benannt oder nicht gesetzt ist, wird
stillschweigend `0` (= deaktiviert) verwendet. Ein Tippfehler im Config-Key
schaltet damit unbemerkt ein Risiko-Limit ab.

**Fix:** Beim Start explizit validieren dass alle erwarteten Attribute
vorhanden sind, oder zumindest ein Warning loggen wenn ein Fallback greift.

---

---

## 🔴 Mittel — Logikfehler

### 4. `engine.py` — `_collect_listings` macht O(categories × pages) API-Calls für identische Daten

**Datei:** `collectorcrypt/trader/engine.py`, `_collect_listings()`, Zeile ~150

```python
for category in self._cfg.categories:        # äußere Schleife
    for page in range(1, self._cfg.max_pages + 1):
        data = self._client.fetch_marketplace_page_with_retry(page, ...)  # API-Call
```

Die API ignoriert den `category`-Parameter (Kommentar sagt das selbst), also werden
bei 3 Kategorien + 10 Seiten exakt **30 identische API-Calls** gemacht. Das `seen`-Set
dedupliziert zwar Karten korrekt, aber die Pages werden trotzdem pro Kategorie neu
abgerufen. Mit Cache TTL kann das in einer Session noch glimpflich ausgehen — aber im
`--loop`-Modus oder bei einem frischen Cache ist es echter Overhead.

**Fix:** Pages **einmal** fetchen, dann alle Kategorien in einem einzigen Durchlauf
client-seitig partitionieren.

---

## 🟡 Niedrig — Zustandsfehler

### 5. `manager.py` — `_cycles` wird nach Neustart auf max. 200 gekappt

**Datei:** `collectorcrypt/trader/manager.py`, `_load_history()`, Zeile 346

```python
self._cycles = len(self._history)  # deque(maxlen=200) → max 200
```

Die `deque` hat `maxlen=200`, daher gibt `len()` nach einem Neustart maximal 200 zurück —
egal wie viele echte Zyklen in der DB stehen. Das UI zeigt dann "200 Zyklen" obwohl z. B.
500 gelaufen sind. Der echte Count steckt in der DB, wird aber nie abgefragt.

**Fix:** Echten Count aus dem Store lesen (`self._store.total_cycle_count()`) und
`_cycles` damit initialisieren.

---

## 🟡 Niedrig — Inkonsistenz

### 6. `reconcile.py` — `STALE_AFTER_SEC` wird einmalig beim Import gelesen

**Datei:** `collectorcrypt/trader/reconcile.py`, Zeile 41

```python
STALE_AFTER_SEC = float(os.environ.get("TRADER_RECONCILE_STALE_SEC", "900"))
```

Dieser Wert wird als **Modul-Konstante** beim ersten Import eingefroren. Alle anderen
Trader-Settings werden über `load_config()` pro Zyklus neu gelesen (Hot-Reload). Dieser
eine Wert bleibt jedoch für die gesamte Prozesslaufzeit fest — eine Änderung der
Umgebungsvariable erfordert einen Neustart des Prozesses, ohne dass das irgendwo
dokumentiert ist.

**Fix:** Entweder in `TraderConfig` aufnehmen oder zumindest in einem Kommentar
dokumentieren dass ein Neustart nötig ist.

---

*Erstellt: 2026-06-08 | Reviewer: Copilot*
