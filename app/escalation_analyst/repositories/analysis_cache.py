"""Immutable content/config-addressed analysis checkpoints, shared by SQLite and PostgreSQL."""
import json
import time


class AnalysisCache:
    def __init__(self, db):
        self.db = db

    def get(self, key):
        rows = self.db.read("SELECT * FROM analysis_cache WHERE cache_key=?", (key,))
        return {**rows[0], "payload": json.loads(rows[0]["payload"])} if rows else None

    def save(self, key, group_id, message_hash, config_hash, payload, report_id):
        self.db.write(
            "INSERT INTO analysis_cache(cache_key,group_id,message_hash,config_hash,payload,report_id,created_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(cache_key) DO NOTHING",
            (key, group_id, message_hash, config_hash, json.dumps(payload), report_id, int(time.time())),
        )

    def observe(self, group_id, payload):
        self.db.write("INSERT INTO analysis_observations(group_id,ts,payload) VALUES(?,?,?)",
                      (group_id, int(time.time()), json.dumps(payload)))

    def units(self, messages):
        from ..analysis.hybrid import unit_key, message_facts
        quotes = {unit_key(m.get("body") or ""): m.get("body") or "" for m in messages}
        keys, found = list(quotes), {}
        for start in range(0, len(keys), 400):
            batch = keys[start:start + 400]
            rows = self.db.read("SELECT unit_key,payload FROM analysis_units WHERE unit_key IN (" + ",".join("?" for _ in batch) + ")", tuple(batch))
            found.update({row["unit_key"]: json.loads(row["payload"]) for row in rows})
        missing = {key: message_facts(quote) for key, quote in quotes.items() if key not in found}
        if missing:
            with self.db.connection() as conn:
                for key, payload in missing.items():
                    conn.execute(self.db.sql("INSERT INTO analysis_units(unit_key,payload) VALUES(?,?) ON CONFLICT(unit_key) DO NOTHING"), (key, json.dumps(payload)))
        return {**found, **missing}, len(found)

    def reserve_ai(self, key):
        created = self.db.write("INSERT INTO analysis_ai_calls(call_key,status,created_at) VALUES(?,'inflight',?) ON CONFLICT(call_key) DO NOTHING", (key, int(time.time())))
        row = self.db.read("SELECT * FROM analysis_ai_calls WHERE call_key=?", (key,))[0]
        return row, bool(created)

    def complete_ai(self, key, response, usage):
        self.db.write("UPDATE analysis_ai_calls SET status='complete',response=?,usage_json=? WHERE call_key=? AND status='inflight'",
                      (response, json.dumps(usage), key))
