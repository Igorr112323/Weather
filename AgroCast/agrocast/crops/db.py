import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

NUM_FIELDS = ("fao", "vp_days", "frost_tol_c", "frost_fatal_c", "yield_t_ha", "area_ha", "gdd")
TEXT_FIELDS = ("breeder", "maturity", "sow_from", "sow_to", "notes")
DEFAULTS = {"frost_tol_c": -2.0, "frost_fatal_c": -3.0, "sow_from": "04-20", "sow_to": "05-15"}

DDL = """
CREATE TABLE IF NOT EXISTS variety (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    breeder TEXT DEFAULT '',
    fao INTEGER,
    maturity TEXT DEFAULT '',
    gdd REAL,
    vp_days INTEGER,
    frost_tol_c REAL DEFAULT -2.0,
    frost_fatal_c REAL DEFAULT -3.0,
    sow_from TEXT DEFAULT '04-20',
    sow_to TEXT DEFAULT '05-15',
    yield_t_ha REAL,
    area_ha REAL,
    notes TEXT DEFAULT '',
    updated_at TEXT
)
"""


class CropDB:
    def __init__(self, path, seed_path=None):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()
        if seed_path and not self._all():
            self._seed(seed_path)

    def _init(self):
        c = sqlite3.connect(self.path)
        c.execute(DDL)
        c.commit()
        c.close()

    def _seed(self, seed_path):
        rows = json.loads(Path(seed_path).read_text())
        for r in rows:
            self.upsert(r)

    def _all(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute("SELECT * FROM variety ORDER BY COALESCE(fao, 999), name")]
        c.close()
        return rows

    def all(self):
        with self._lock:
            return self._all()

    def get(self, name):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        r = c.execute("SELECT * FROM variety WHERE name=?", (name,)).fetchone()
        c.close()
        return dict(r) if r else None

    @staticmethod
    def _clean(d):
        row = {k: d.get(k) for k in ("fao", "vp_days", "frost_tol_c", "frost_fatal_c", "yield_t_ha", "area_ha", "gdd")}
        for k in NUM_FIELDS:
            v = row[k]
            if v is None or v == "":
                row[k] = None
            elif k in ("fao", "vp_days"):
                row[k] = int(float(v))
            else:
                row[k] = float(v)
        for k in TEXT_FIELDS:
            row[k] = str(d.get(k) or "").strip()
        for k, v in DEFAULTS.items():
            if row[k] is None:
                row[k] = v
        return row

    @staticmethod
    def _validate(name, row):
        if row["gdd"] is not None and not (1500 <= row["gdd"] <= 3500):
            raise ValueError("САТ (группа) вне диапазона 1500–3500°C")
        if row["fao"] is not None and not (50 <= row["fao"] <= 650):
            raise ValueError("ФАО вне диапазона 50–650")
        if row["yield_t_ha"] is not None and not (0 < row["yield_t_ha"] <= 30):
            raise ValueError("урожайность вне диапазона 0–30 т/га")
        if row["vp_days"] is not None and not (60 <= row["vp_days"] <= 200):
            raise ValueError("вегетационный период вне диапазона 60–200 дней")
        if not (-8 <= row["frost_fatal_c"] <= row["frost_tol_c"] <= 0):
            raise ValueError("температуры мороза: гибель должна быть ниже переносимости (0…−8°C)")

    def upsert(self, d):
        name = str(d.get("name") or "").strip()
        if not name:
            raise ValueError("название сорта обязательно")
        row = self._clean(d)
        self._validate(name, row)
        now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
        cols = ["name", "breeder", "fao", "maturity", "gdd", "vp_days", "frost_tol_c", "frost_fatal_c",
                "sow_from", "sow_to", "yield_t_ha", "area_ha", "notes"]
        vals = [name] + [row[k] for k in cols[1:]]
        with self._lock:
            c = sqlite3.connect(self.path)
            ex = c.execute("SELECT id FROM variety WHERE name=?", (name,)).fetchone()
            if ex:
                sets = ", ".join(f"{k}=?" for k in cols[1:])
                c.execute(f"UPDATE variety SET {sets}, updated_at=? WHERE id=?", [*vals[1:], now, ex[0]])
            else:
                qs = ", ".join("?" * (len(cols) + 1))
                c.execute(f"INSERT INTO variety ({', '.join(cols)}, updated_at) VALUES ({qs})", [*vals, now])
            c.commit()
            c.close()
        return self.get(name)

    def remove(self, name):
        with self._lock:
            c = sqlite3.connect(self.path)
            n = c.execute("DELETE FROM variety WHERE name=?", (name,)).rowcount
            c.commit()
            c.close()
        return n > 0
