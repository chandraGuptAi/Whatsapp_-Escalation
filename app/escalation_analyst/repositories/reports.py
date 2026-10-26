"""Saved analysis and model usage; every SQL query stays in this repository."""

from __future__ import annotations

import json
import time
from ..errors import EscalationError


class Reports:
    def __init__(self, db):
        self.db = db

    def save(self, values: dict) -> int:
        columns = [
            "group_id",
            "group_name",
            "report",
            "message_count",
            "window_label",
            "period_start",
            "period_end",
            "model",
            "trigger",
            "scope",
            "metrics_json",
            "cluster_id",
            "base_report_id",
            "max_message_id",
            "prompt_version",
            "message_hash",
        ]
        self.db.initialize()
        with self.db.connection() as conn:
            row = conn.execute(self.db.sql("INSERT INTO reports("
            + ",".join(columns)
            + ",created_at) VALUES("
            + ",".join("?" for _ in range(len(columns) + 1))
            + ") RETURNING id"), tuple(values.get(k) for k in columns) + (int(time.time()),)).fetchone()
            report_id = row["id"]
            for group_id, child_id in values.get("group_reports", {}).items():
                conn.execute(self.db.sql("INSERT INTO report_groups(master_report_id,group_id,group_report_id) VALUES(?,?,?)"),
                             (report_id, group_id, child_id))
            return report_id

    def children(self, report_id):
        return self.db.read("SELECT r.* FROM reports r JOIN report_groups g ON r.id=g.group_report_id WHERE g.master_report_id=? ORDER BY g.group_id", (report_id,))

    def get(self, report_id: int) -> dict:
        rows = self.db.read("SELECT * FROM reports WHERE id=?", (report_id,))
        if not rows:
            raise EscalationError(404, "Report not found")
        return rows[0]

    def history(
        self, group_id: str | None = None, scope: str = "all", search: str = "", window: dict | None = None
    ) -> list[dict]:
        conditions, args = [], []
        if group_id:
            conditions.append("r.group_id=?")
            args.append(group_id)
        if scope != "all":
            conditions.append("r.scope=?")
            args.append(scope)
        if search:
            conditions.append("(LOWER(r.group_name) LIKE ? OR LOWER(r.report) LIKE ?)")
            args.extend(("%" + search.lower() + "%",) * 2)
        if window:
            conditions.append("r.created_at>=? AND r.created_at<?")
            args.extend((window["from"], window["to"]))
        return self.db.read(
            "SELECT r.*,COALESCE(o.name,l.name,r.group_name,r.group_id) AS display_name FROM reports r LEFT JOIN group_name_overrides o ON r.group_id=o.group_id LEFT JOIN group_names_learned l ON r.group_id=l.group_id"
            + (" WHERE " + " AND ".join(conditions) if conditions else "")
            + " ORDER BY r.created_at DESC,r.id DESC",
            tuple(args),
        )

    def context(self, group_id: str, before: int, limit: int) -> list[dict]:
        return self.db.read(
            "SELECT * FROM reports WHERE group_id=? AND period_end<=? ORDER BY period_end DESC,id DESC LIMIT ?",
            (group_id, before, limit),
        )

    def window_reports(self, group_ids: set[str], start: int, end: int) -> list[dict]:
        """Latest group report per group whose period lies inside [start, end)."""
        rows = self.db.read(
            "SELECT * FROM reports WHERE scope='group' AND period_start>=? AND period_end<? "
            "ORDER BY created_at DESC,id DESC",
            (start, end),
        )
        latest = {}
        for row in rows:
            if row["group_id"] in group_ids:
                latest.setdefault(row["group_id"], row)
        return list(latest.values())

    def overlapping_reports(self, group_ids: set[str], start: int, end: int) -> list[dict]:
        """Latest saved group report per group whose period overlaps [start, end]."""
        rows = self.db.read(
            "SELECT * FROM reports WHERE scope='group' AND period_end>=? AND period_start<=? "
            "ORDER BY created_at DESC,id DESC",
            (start, end),
        )
        latest = {}
        for row in rows:
            if row["group_id"] in group_ids:
                latest.setdefault(row["group_id"], row)
        return list(latest.values())

    def base(
        self, group_id: str, window: dict, messages: list[dict], prompt_version: str | None = None
    ) -> dict | None:
        rows = self.db.read(
            "SELECT * FROM reports WHERE group_id=? AND period_start<=? AND period_end<=? ORDER BY period_end DESC,id DESC",
            (group_id, window["from"], window["to"]),
        )
        for row in rows:
            # A report made with a different analysis prompt is not compatible; unversioned legacy rows are.
            if row.get("message_hash"):
                from ..analysis.hybrid import digest
                previous = [m for m in messages if m["id"] <= (row["max_message_id"] or 0)
                            and row["period_start"] <= m["timestamp"] <= row["period_end"]]
                if digest(previous) != row["message_hash"]:
                    continue
            if prompt_version and row.get("prompt_version") not in (None, prompt_version):
                continue
            if row["period_start"] == window["from"]:
                return row
            if window.get("rolling") and row["window_label"] == window["label"]:
                lost = self.db.read(
                    "SELECT COUNT(*) AS n FROM messages WHERE group_id=? AND timestamp>=? AND timestamp<?",
                    (group_id, row["period_start"], window["from"]),
                )[0]["n"]
                if not lost:
                    return row
        return None

    def usage(self, values: dict) -> None:
        keys = (
            "scope",
            "group_id",
            "group_name",
            "model",
            "input_tokens",
            "output_tokens",
            "cost_usd",
            "cost_inr",
            "reasoning_tokens",
            "latency_ms",
            "retry_count",
        )
        self.db.write(
            "INSERT INTO usage_log(ts,"
            + ",".join(keys)
            + ") VALUES("
            + ",".join("?" for _ in range(len(keys) + 1))
            + ")",
            (int(time.time()),) + tuple(values.get(k) for k in keys),
        )

    def usage_log(self, limit: int) -> list[dict]:
        return self.db.read("SELECT * FROM usage_log ORDER BY ts DESC,id DESC LIMIT ?", (limit,))

    def usage_totals(self, since: int = 0) -> dict:
        return self.db.read(
            "SELECT COUNT(*) AS calls,COALESCE(SUM(input_tokens),0) AS input_tokens,COALESCE(SUM(output_tokens),0) AS output_tokens,COALESCE(SUM(cost_usd),0) AS cost_usd,COALESCE(SUM(cost_inr),0) AS cost_inr FROM usage_log WHERE ts>=?",
            (since,),
        )[0]
