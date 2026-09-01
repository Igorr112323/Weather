import sqlite3
import json
import datetime as dt
from pathlib import Path

SCHEMA = """
create table if not exists files(source text, name text, last_time text, updated_at text, status text, primary key(source, name));
create table if not exists forecasts(id integer primary key autoincrement, created_at text, lat real, lon real, start_year int, start_month int, horizon int, payload text);
create table if not exists subscriptions(id integer primary key autoincrement, name text, lat real, lon real, horizon int, variables text, created_at text, active int default 1);
create table if not exists scores(id integer primary key autoincrement, forecast_id int, variable text, target_year int, target_month int, lead int, p0 real, p1 real, p2 real, obs_tercile int, created_at text);
create table if not exists events(id integer primary key autoincrement, at text, kind text, message text);
"""


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


class Registry:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript(SCHEMA)
        try:
            self.conn.execute("alter table subscriptions add column mode text default 'seasonal'")
        except sqlite3.OperationalError:
            pass
        self.conn.commit()

    def upsert_file(self, source, name, last_time, status="ok"):
        self.conn.execute(
            "insert or replace into files(source, name, last_time, updated_at, status) values(?,?,?,?,?)",
            (source, name, str(last_time), _now(), status),
        )
        self.conn.commit()

    def file(self, source, name):
        cur = self.conn.execute(
            "select last_time, updated_at, status from files where source=? and name=?", (source, name)
        )
        return cur.fetchone()

    def log_event(self, kind, message):
        self.conn.execute(
            "insert into events(at, kind, message) values(?,?,?)", (_now(), str(kind), str(message)[:500])
        )
        self.conn.commit()

    def recent_events(self, limit=20):
        cur = self.conn.execute("select at, kind, message from events order by id desc limit ?", (limit,))
        return cur.fetchall()

    def save_forecast(self, lat, lon, start_year, start_month, horizon, payload):
        cur = self.conn.execute(
            "insert into forecasts(created_at, lat, lon, start_year, start_month, horizon, payload) values(?,?,?,?,?,?,?)",
            (_now(), float(lat), float(lon), int(start_year), int(start_month), int(horizon), json.dumps(payload)),
        )
        self.conn.commit()
        return cur.lastrowid

    def forecasts(self):
        cur = self.conn.execute("select id, created_at, lat, lon, start_year, start_month, horizon, payload from forecasts order by id")
        cols = ["id", "created_at", "lat", "lon", "start_year", "start_month", "horizon", "payload"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def save_scores(self, rows):
        ts = _now()
        self.conn.executemany(
            "insert into scores(forecast_id, variable, target_year, target_month, lead, p0, p1, p2, obs_tercile, created_at) values(?,?,?,?,?,?,?,?,?,?)",
            [(r["forecast_id"], r["variable"], r["target_year"], r["target_month"], r["lead"], r["p0"], r["p1"], r["p2"], r["obs_tercile"], ts) for r in rows],
        )
        self.conn.commit()

    def scores_table(self):
        cur = self.conn.execute("select forecast_id, variable, target_year, target_month, lead, p0, p1, p2, obs_tercile from scores")
        cols = ["forecast_id", "variable", "target_year", "target_month", "lead", "p0", "p1", "p2", "obs_tercile"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def add_subscription(self, name, lat, lon, horizon, variables, mode="seasonal"):
        self.conn.execute(
            "insert into subscriptions(name, lat, lon, horizon, variables, created_at, active, mode) values(?,?,?,?,?,?,1,?)",
            (str(name), float(lat), float(lon), int(horizon), json.dumps(list(variables)), _now(), str(mode)),
        )
        self.conn.commit()

    def subscriptions(self):
        cur = self.conn.execute("select id, name, lat, lon, horizon, variables, mode from subscriptions where active=1")
        cols = ["id", "name", "lat", "lon", "horizon", "variables", "mode"]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["variables"] = tuple(json.loads(d["variables"]))
            out.append(d)
        return out
