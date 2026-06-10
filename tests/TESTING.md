# Tests ausführen

## Voraussetzungen

Virtual environment aktivieren und Abhängigkeiten installieren:

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/macOS

pip install -r requirements.txt
pip install pytest
```

---

## Alle Tests ausführen

```bash
pytest
```

> `pytest.ini` setzt `testpaths = tests`, daher reicht dieser Befehl aus dem Projektstamm.

---

## Gezielt testen

```bash
# Einzelne Datei
pytest tests/test_engine_live.py

# Einzelne Funktion
pytest tests/test_risk.py::test_max_position_enforced

# Mehrere Dateien
pytest tests/test_risk.py tests/test_orders.py
```

---

## Nützliche Flags

| Flag | Beschreibung |
|------|-------------|
| `-v` | Verbose – zeigt jeden Testnamen |
| `-q` | Quiet – nur Zusammenfassung (Standard via `pytest.ini`) |
| `-x` | Stoppt beim ersten Fehler |
| `-k "keyword"` | Nur Tests, die `keyword` im Namen enthalten |
| `--tb=short` | Kurze Traceback-Ausgabe |
| `--tb=long` | Vollständiger Traceback |
| `--tb=no` | Kein Traceback |
| `-s` | `print()`-Ausgaben nicht unterdrücken |
| `--lf` | Nur zuletzt fehlgeschlagene Tests wiederholen |
| `--co` | Nur Testnamen anzeigen, nicht ausführen |

### Beispiele

```bash
# Verbose mit sofortigem Stopp bei Fehler
pytest -vx

# Nur Tests mit "risk" im Namen
pytest -k "risk"

# Nur zuletzt fehlgeschlagene Tests erneut ausführen
pytest --lf

# Alle Tests auflisten ohne sie auszuführen
pytest --co -q

# Nur die Zusammenfassungszeile anzeigen
# Out-String puffert die gesamte Ausgabe, bevor sie verarbeitet wird –
# verhindert, dass die letzte Zeile beim Pipen verloren geht.
(pytest --tb=no -q 2>&1 | Out-String).Trim().Split("`n") | Where-Object { $_ -match '\S' } | Select-Object -Last 1
```

**Beispielausgabe:**
```
62 passed in 1.45s
```

Bei Fehlern:
```
2 failed, 60 passed in 1.52s
```

> **Warum nicht einfach `| Select-Object -Last 1`?**  
> pytest erkennt, dass es in eine Pipe schreibt (kein TTY), und streamt die Ausgabe
> zeilenweise. Die Zusammenfassungszeile kann dabei verloren gehen oder die letzte
> sichtbare Zeile ist eine ERROR-Zeile aus dem Short-Summary-Block.  
> `Out-String` sammelt alles erst vollständig, dann wird gefiltert.

---

## Coverage (optional)

```bash
pip install pytest-cov

pytest --cov=collectorcrypt --cov-report=term-missing
```

---

## Hinweise

- Alle Tests sind hermetic: kein Netzwerk, kein echter Wallet-Schlüssel, jede Store-Interaktion nutzt eine isolierte Temp-Datenbank (`tmp_path`-Fixture).
- Live-Mode-Tests (`test_engine_live.py`, `test_executor_live.py`) testen das vollständige Trader-Verhalten mit gefakten Abhängigkeiten – kein echtes Geld involviert.
