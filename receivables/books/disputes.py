from ..primitives import identifier, day, money, integer, error


class Disputes:
    def __init__(self, database, ledger):
        self.db = database
        self.ledger = ledger

    def get(self, dispute_id):
        row = self.db.one("SELECT * FROM disputes WHERE id=?", (dispute_id,))
        if row is None:
            error("missing", "dispute not found")
        return row

    def open(self, dispute_id, invoice, amount, opened, reason, recorded=None):
        identifier(dispute_id)
        opened = day(opened)
        recorded = day(recorded) if recorded else opened
        if recorded < opened:
            error("input", "knowledge date cannot precede dispute open date")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            error("input", "bounded dispute reason required")
        with self.db.transaction() as con:
            document = self.ledger.document(invoice)
            if document["kind"] != "invoice":
                error("dispute", "only invoices may be disputed")
            minor = money(amount, document["currency"])
            old = self.db.one("SELECT * FROM disputes WHERE id=?", (dispute_id,))
            if old:
                if (old["invoice"], old["amount"], old["opened"], old["reason"], old["opened_recorded"]) != (
                    invoice,
                    minor,
                    opened,
                    reason,
                    recorded,
                ):
                    error("conflict", "dispute identity differs")
                return old
            self.ledger.check_period(opened)
            if not self.ledger.active_at(invoice, opened):
                error("dispute", "invoice inactive on dispute date")
            boundaries = {opened}
            for row in self.db.rows(
                "SELECT opened,closed FROM disputes WHERE invoice=?", (invoice,)
            ):
                for field in ("opened", "closed"):
                    if row[field] and row[field] >= opened:
                        boundaries.add(row[field])
            for boundary in boundaries:
                existing = self.db.one(
                    "SELECT COALESCE(SUM(amount),0) AS n FROM disputes WHERE invoice=? AND opened<=? AND (closed IS NULL OR closed>?)",
                    (invoice, boundary, boundary),
                )["n"]
                if existing + minor > document["amount"]:
                    error(
                        "dispute", "disputed principal exceeds invoice original amount"
                    )
            con.execute(
                "INSERT INTO disputes VALUES(?,?,?,?,?,?,?,?,?)",
                (dispute_id, invoice, minor, opened, None, reason, 1, recorded, None),
            )
            self.db.audit(
                "dispute_opened",
                dispute_id,
                {
                    "invoice": invoice,
                    "amount": minor,
                    "opened": opened,
                    "recorded": recorded,
                    "reason": reason,
                },
            )
        return self.get(dispute_id)

    def close(
        self,
        dispute_id,
        closed,
        expected_revision,
        reason,
        recorded=None,
    ):
        closed = day(closed)
        recorded = day(recorded) if recorded else closed
        if recorded < closed:
            error("input", "knowledge date cannot precede dispute close date")
        integer(expected_revision, 1)
        if not isinstance(reason, str) or not reason.strip():
            error("input", "resolution reason required")
        with self.db.transaction() as con:
            row = self.get(dispute_id)
            if row["revision"] != expected_revision:
                error("conflict", "dispute revision changed")
            if row["closed"] or closed < row["opened"]:
                error("state", "dispute already closed or resolution predates opening")
            self.ledger.check_period(closed)
            con.execute(
                "UPDATE disputes SET closed=?,revision=revision+1,closed_recorded=? WHERE id=?",
                (closed, recorded, dispute_id),
            )
            self.db.audit(
                "dispute_closed",
                dispute_id,
                {
                    "closed": closed,
                    "recorded": recorded,
                    "reason": reason,
                    "revision": expected_revision + 1,
                },
            )
        return self.get(dispute_id)

    def list(self, cutoff, party=None, knowledge_cutoff=None):
        cutoff = day(cutoff)
        knowledge_cutoff = day(knowledge_cutoff or cutoff)
        if party is not None:
            self.ledger.party(party)
        rows = self.db.rows(
            "SELECT d.*,i.party,i.currency FROM disputes d JOIN documents i ON i.id=d.invoice "
            "WHERE d.opened<=? AND d.opened_recorded<=? "
            "AND (d.closed IS NULL OR d.closed>? OR d.closed_recorded>?) "
            "ORDER BY d.opened,d.id",
            (cutoff, knowledge_cutoff, cutoff, knowledge_cutoff),
        )
        return [row for row in rows if party is None or row["party"] == party]
