from datetime import date, timedelta
from ..primitives import identifier, day, digest, error


class Periods:
    def __init__(self, database, ledger, controls):
        self.db = database
        self.ledger = ledger
        self.controls = controls

    def get(self, period_id):
        row = self.db.one("SELECT * FROM periods WHERE id=?", (period_id,))
        if row is None:
            error("missing", "period not found")
        return row

    def create(self, period_id, start, end):
        identifier(period_id)
        start, end = day(start), day(end)
        if end <= start:
            error("period", "period must be nonempty half-open interval")
        with self.db.transaction() as con:
            old = self.db.one("SELECT * FROM periods WHERE id=?", (period_id,))
            if old:
                if (old["start"], old["end"]) != (start, end):
                    error("conflict", "period definition differs")
                return old
            if self.db.one(
                "SELECT id FROM periods WHERE start<? AND end>?", (end, start)
            ):
                error("period", "period overlaps existing interval")
            con.execute(
                "INSERT INTO periods VALUES(?,?,?,'open',NULL)", (period_id, start, end)
            )
            self.db.audit("period_created", period_id, {"start": start, "end": end})
        return self.get(period_id)

    def payload(self, period_id):
        row = self.get(period_id)
        cutoff = (date.fromisoformat(row["end"]) - timedelta(days=1)).isoformat()
        postings = self.controls.journal(row["start"], row["end"])
        disputes = self.db.rows(
            "SELECT * FROM disputes WHERE opened<? ORDER BY id", (row["end"],)
        )
        # Future resolutions do not change evidence at the closed cutoff.
        for dispute in disputes:
            if dispute["closed"] and dispute["closed"] >= row["end"]:
                dispute["closed"] = None
            dispute.pop("revision")
        return {
            "id": period_id,
            "start": row["start"],
            "end": row["end"],
            "postings": postings,
            "trial_balance": self.controls.trial_balance(cutoff),
            "subledger": self.controls.subledger(cutoff),
            "disputes": disputes,
        }

    def preview(self, period_id):
        payload = self.payload(period_id)
        controls = self.controls.require_valid()
        return {
            "period": period_id,
            "digest": digest(payload),
            "payload": payload,
            "checks": controls,
            "state": self.get(period_id)["state"],
        }

    def close(self, period_id, expected_digest, operator):
        identifier(operator)
        with self.db.transaction() as con:
            row = self.get(period_id)
            if row["state"] == "closed":
                if row["digest"] != expected_digest:
                    error("conflict", "closed period digest differs")
                return row
            preview = self.preview(period_id)
            if preview["digest"] != expected_digest:
                error("conflict", "period changed since preview")
            con.execute(
                "UPDATE periods SET state='closed',digest=? WHERE id=?",
                (expected_digest, period_id),
            )
            self.db.audit(
                "period_closed",
                period_id,
                {"digest": expected_digest, "operator": operator},
            )
        return self.get(period_id)

    def verify(self, period_id):
        row = self.get(period_id)
        if row["state"] != "closed":
            error("state", "period is not closed")
        actual = digest(self.payload(period_id))
        if actual != row["digest"]:
            error("integrity", "closed period evidence differs")
        return {"period": period_id, "verified": True, "digest": actual}
