# Code Review Findings

Gefundene Schwächen im Code — priorisiert nach Risiko.

---

## 🔴 Mittel — Designfehler

### 1. `strategy.py` — `build_plan` ist nicht wirklich pure ✅ FIXED (2026-06-08)

**Datei:** `collectorcrypt/trader/strategy.py`, Zeile ~325

```python
cand.resell_usd = resale  # mutiert das Candidate-Objekt in-place
```

Das Modul behauptet in seinem Docstring "pure, side-effect-free", mutiert aber
`Candidate`-Objekte direkt innerhalb von `build_plan`. Das macht die Funktion
unpredictable wenn dieselbe Kandidatenliste mehrfach übergeben wird.

**Fix:** `resell_usd` beim Erstellen des `Candidate` setzen (in `_economics`)
oder eine Kopie anlegen bevor mutiert wird.

**Erledigt:** Beide Stages in `build_plan` nutzen jetzt `dataclasses.replace(cand,
resell_usd=resale)` statt In-Place-Mutation. Funktion ist wieder side-effect-free.
819 Tests grün.

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

**Erledigt (2026-06-08):** `_cache` ist jetzt ein `OrderedDict` mit LRU-Eviction
(`CACHE_MAX_ENTRIES=500`). `_cache_set` entfernt zuerst alle abgelaufenen Einträge
(aktiver TTL-Sweep statt nur beim Lesen) und kappt danach auf die Maximalgröße;
`_cache_get` markiert Treffer als zuletzt genutzt. Neue Tests in
`tests/test_api_cache.py`.

---

## 🟡 Niedrig — Defensiv

### 3. `risk.py` — Config-Fehler fallen lautlos auf 0 zurück — ⚪ KEIN BUG (verifiziert 2026-06-08)

**Datei:** `collectorcrypt/trader/risk.py`, `_limits()`

```python
getattr(self._cfg, "max_open_positions", 0)
```

Wenn ein Config-Attribut falsch benannt oder nicht gesetzt ist, wird
stillschweigend `0` (= deaktiviert) verwendet. Ein Tippfehler im Config-Key
schaltet damit unbemerkt ein Risiko-Limit ab.

**Verifikation:** `self._cfg` ist immer ein `TraderConfig` (frozen dataclass);
alle fünf abgefragten Felder sind als Dataclass-Felder garantiert vorhanden, der
`getattr`-Default kann zur Laufzeit nie greifen. Ein Tippfehler in einem
*Env-Var-Namen* deaktiviert kein Limit — das Feld behält schlicht seinen
dokumentierten Default. Über-defensiver, aber harmloser Code; nicht geändert.

---

---

## 🔴 Mittel — Logikfehler

### 4. `engine.py` — `_collect_listings` macht O(categories × pages) API-Calls für identische Daten ✅ FIXED (2026-06-08)

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

**Erledigt:** Äußere Kategorie-Schleife entfernt; jede Seite wird jetzt genau einmal
gefetcht und gegen das Set der konfigurierten Kategorien (`wanted`) gefiltert. Aus
O(categories × pages) wird O(pages). 819 Tests grün.

---

## 🟡 Niedrig — Zustandsfehler

### 5. `manager.py` — `_cycles` wird nach Neustart auf max. 200 gekappt ✅ FIXED (2026-06-08)

**Datei:** `collectorcrypt/trader/manager.py`, `_load_history()`, Zeile 346

```python
self._cycles = len(self._history)  # deque(maxlen=200) → max 200
```

Die `deque` hat `maxlen=200`, daher gibt `len()` nach einem Neustart maximal 200 zurück —
egal wie viele echte Zyklen in der DB stehen. Das UI zeigt dann "200 Zyklen" obwohl z. B.
500 gelaufen sind. Der echte Count steckt in der DB, wird aber nie abgefragt.

**Fix:** Echten Count aus dem Store lesen (`self._store.total_cycle_count()`) und
`_cycles` damit initialisieren.

**Erledigt:** Neue Store-Methode `cycle_count()` (`SELECT COUNT(*) FROM cycles`);
`_load_history()` initialisiert `_cycles` daraus (Fallback auf `len(history)` wenn die
Abfrage fehlschlägt). Neue Tests in `tests/test_store.py`.

---

## 🟡 Niedrig — Inkonsistenz

### 6. `reconcile.py` — `STALE_AFTER_SEC` wird einmalig beim Import gelesen ✅ DOKUMENTIERT (2026-06-08)

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

**Erledigt:** Kein funktionaler Bug — der `Reconciler` wird ohnehin nur einmal
(`manager.__init__`) konstruiert, würde den Wert also auch dann beim Bau einfrieren,
wenn er in `TraderConfig` läge; ein Restart ist so oder so nötig. Per Kommentar an der
Konstante dokumentiert (env-only, Restart erforderlich), wie vom Befund vorgeschlagen.

---

*Erstellt: 2026-06-08 | Reviewer: Copilot*
