from ..primitives import (
    identifier,
    day,
    currency,
    money,
    display,
    obj,
    sequence,
    canonical,
    digest,
    decode,
    error,
)


class Ledger:
    def __init__(self, database):
        self.db = database

    def party(self, party_id):
        row = self.db.one("SELECT * FROM parties WHERE id=?", (party_id,))
        if row is None:
            error("missing", "party not registered")
        row["aliases"] = decode(row["aliases"])
        return row

    def register_party(self, party_id, name, aliases=None):
        identifier(party_id)
        if not isinstance(name, str) or not name.strip() or len(name) > 512:
            error("input", "party name must be bounded text")
        aliases = sorted(
            {
                value.strip().casefold()
                for value in sequence(aliases or [], 0, 100)
                if isinstance(value, str) and value.strip()
            }
        )
        payload = {"id": party_id, "name": name.strip(), "aliases": aliases}
        with self.db.transaction() as con:
            old = self.db.one("SELECT * FROM parties WHERE id=?", (party_id,))
            if old:
                if self.party(party_id) != payload:
                    error("conflict", "party definition is immutable")
                return payload
            con.execute(
                "INSERT INTO parties VALUES(?,?,?)",
                (party_id, name.strip(), canonical(aliases)),
            )
            self.db.audit("party_registered", party_id, payload)
        return payload

    def check_period(self, effective):
        closed = self.db.one(
            "SELECT id FROM periods WHERE state='closed' AND start<=? AND end>?",
            (effective, effective),
        )
        if closed:
            error("period", "effective date is in closed period", period=closed["id"])

    def document(self, document_id):
        row = self.db.one("SELECT * FROM documents WHERE id=?", (document_id,))
        if row is None:
            error("missing", "document not found", document=document_id)
        return row

    def post(self, data):
        fields = {
            "id",
            "kind",
            "party",
            "currency",
            "amount",
            "issued",
            "due",
            "reference",
        }
        obj(data, fields, fields - {"due"})
        ident = identifier(data["id"])
        kind = data["kind"]
        if kind not in {"invoice", "payment", "credit"}:
            error("input", "document kind must be invoice payment or credit")
        code = currency(data["currency"])
        minor = money(data["amount"], code)
        issued = day(data["issued"])
        due = data.get("due")
        if kind == "invoice":
            due = day(due)
            if due < issued:
                error("input", "invoice due date precedes issue date")
        elif due is not None:
            error("input", "only invoices have due dates")
        reference = data["reference"]
        if (
            not isinstance(reference, str)
            or len(reference) > 256
            or "\x00" in reference
        ):
            error("input", "invalid document reference")
        payload = {
            "id": ident,
            "kind": kind,
            "party": identifier(data["party"]),
            "currency": code,
            "amount": display(minor, code),
            "issued": issued,
            "due": due,
            "reference": reference.strip(),
        }
        with self.db.transaction() as con:
            self.party(payload["party"])
            old = self.db.one("SELECT digest FROM documents WHERE id=?", (ident,))
            if old:
                if old["digest"] != digest(payload):
                    error("conflict", "document identity differs")
                return {"id": ident, "existing": True}
            self.check_period(issued)
            con.execute(
                "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    kind,
                    payload["party"],
                    code,
                    minor,
                    issued,
                    due,
                    payload["reference"],
                    canonical(payload),
                    digest(payload),
                ),
            )
            if kind == "invoice":
                postings = [("receivable", minor), ("revenue_control", -minor)]
            elif kind == "payment":
                postings = [("cash", minor), ("unapplied", -minor)]
            else:
                postings = [("credit_control", minor), ("unapplied", -minor)]
            self.entry(ident, ident, postings, issued, "document")
            self.db.audit("document_posted", ident, payload)
        return {"id": ident, "existing": False}

    def entry(self, entry_id, document, postings, effective, reason):
        if sum(value for account, value in postings) != 0:
            error("integrity", "entry must balance")
        for account, value in postings:
            if value:
                self.db.connection.execute(
                    "INSERT INTO postings(entry,document,account,amount,effective,reason) VALUES(?,?,?,?,?,?)",
                    (entry_id, document, account, value, effective, reason),
                )

    def active_at(self, document_id, cutoff):
        document = self.document(document_id)
        if document["issued"] > cutoff:
            return False
        reversal = self.db.one(
            "SELECT effective FROM document_reversals WHERE document=?", (document_id,)
        )
        return reversal is None or reversal["effective"] > cutoff

    def balance(self, document_id, cutoff="2200-12-31"):
        cutoff = day(cutoff)
        document = self.document(document_id)
        if not self.active_at(document_id, cutoff):
            return 0
        field = "invoice" if document["kind"] == "invoice" else "source"
        allocated = self.db.one(
            "SELECT COALESCE(SUM(amount),0) AS n FROM allocations WHERE "
            + field
            + "=? AND effective<=? AND (reversed IS NULL OR reversed>?)",
            (document_id, cutoff, cutoff),
        )["n"]
        return document["amount"] - allocated

    def allocate(
        self,
        allocation_id,
        source,
        invoice,
        amount,
        effective,
        reason="confirmed matching",
    ):
        identifier(allocation_id)
        effective = day(effective)
        with self.db.transaction() as con:
            credit = self.document(source)
            debit = self.document(invoice)
            minor = money(amount, credit["currency"])
            if (
                credit["kind"] not in {"payment", "credit"}
                or debit["kind"] != "invoice"
            ):
                error("allocation", "allocation requires payment or credit and invoice")
            if (credit["party"], credit["currency"]) != (
                debit["party"],
                debit["currency"],
            ):
                error("allocation", "party and currency must match")
            if effective < max(credit["issued"], debit["issued"]):
                error("allocation", "allocation cannot precede documents")
            existing = self.db.one(
                "SELECT * FROM allocations WHERE id=?", (allocation_id,)
            )
            if existing:
                if (
                    existing["source"],
                    existing["invoice"],
                    existing["amount"],
                    existing["effective"],
                    existing["reason"],
                ) != (source, invoice, minor, effective, reason):
                    error("conflict", "allocation identity differs")
                return {"id": allocation_id, "existing": True}
            self.check_period(effective)
            if not self.active_at(source, "2200-12-31") or not self.active_at(
                invoice, "2200-12-31"
            ):
                error("state", "cannot allocate reversed document")
            if minor > min(
                self.available_from(source, effective),
                self.available_from(invoice, effective),
            ):
                error("allocation", "allocation exceeds available balance")
            con.execute(
                "INSERT INTO allocations VALUES(?,?,?,?,?,NULL,?)",
                (allocation_id, source, invoice, minor, effective, reason),
            )
            self.entry(
                allocation_id,
                source,
                [("unapplied", minor), ("receivable", -minor)],
                effective,
                "allocation:" + invoice,
            )
            self.db.audit(
                "allocated",
                allocation_id,
                {
                    "source": source,
                    "invoice": invoice,
                    "amount": minor,
                    "effective": effective,
                },
            )
        return {"id": allocation_id, "existing": False}

    def reverse_allocation(self, reversal_id, allocation_id, effective, reason):
        identifier(reversal_id)
        effective = day(effective)
        if not isinstance(reason, str) or not reason.strip():
            error("input", "reversal requires reason")
        with self.db.transaction() as con:
            old = self.db.one("SELECT * FROM reversals WHERE id=?", (reversal_id,))
            if old:
                if (old["target"], old["kind"], old["effective"], old["reason"]) != (
                    allocation_id,
                    "allocation",
                    effective,
                    reason,
                ):
                    error("conflict", "reversal identity differs")
                return {"id": reversal_id, "existing": True}
            allocation = self.db.one(
                "SELECT * FROM allocations WHERE id=?", (allocation_id,)
            )
            if allocation is None:
                error("missing", "allocation not found")
            if (
                allocation["reversed"] is not None
                or effective < allocation["effective"]
            ):
                error("state", "allocation reversed already or reversal date too early")
            self.check_period(effective)
            con.execute(
                "UPDATE allocations SET reversed=? WHERE id=?",
                (effective, allocation_id),
            )
            con.execute(
                "INSERT INTO reversals VALUES(?,?,?,?,?)",
                (reversal_id, allocation_id, "allocation", effective, reason),
            )
            self.entry(
                reversal_id,
                allocation["source"],
                [
                    ("unapplied", -allocation["amount"]),
                    ("receivable", allocation["amount"]),
                ],
                effective,
                "reverse:" + allocation_id,
            )
            self.db.audit(
                "allocation_reversed",
                reversal_id,
                {"allocation": allocation_id, "effective": effective, "reason": reason},
            )
        return {"id": reversal_id, "existing": False}

    def reverse_document(self, reversal_id, document_id, effective, reason):
        identifier(reversal_id)
        effective = day(effective)
        if not isinstance(reason, str) or not reason.strip():
            error("input", "document reversal requires reason")
        with self.db.transaction() as con:
            document = self.document(document_id)
            old = self.db.one("SELECT * FROM reversals WHERE id=?", (reversal_id,))
            if old:
                if (old["target"], old["kind"], old["effective"], old["reason"]) != (
                    document_id,
                    "document",
                    effective,
                    reason,
                ):
                    error("conflict", "reversal identity differs")
                return {"id": reversal_id, "existing": True}
            if self.db.one(
                "SELECT document FROM document_reversals WHERE document=?",
                (document_id,),
            ):
                error("state", "document already reversed")
            if effective < document["issued"]:
                error("state", "document reversal predates issue")
            self.check_period(effective)
            active = self.db.rows(
                "SELECT id FROM allocations WHERE (source=? OR invoice=?) AND (reversed IS NULL OR reversed>?)",
                (document_id, document_id, effective),
            )
            if active:
                error("state", "reverse all allocations before document reversal")
            if self.db.one(
                "SELECT line FROM bank_links WHERE payment=?", (document_id,)
            ):
                error("state", "unlink bank evidence before payment reversal")
            con.execute(
                "INSERT INTO reversals VALUES(?,?,?,?,?)",
                (reversal_id, document_id, "document", effective, reason),
            )
            con.execute(
                "INSERT INTO document_reversals VALUES(?,?,?)",
                (document_id, effective, reversal_id),
            )
            original = self.db.rows(
                "SELECT account,amount FROM postings WHERE entry=? AND reason='document'",
                (document_id,),
            )
            self.entry(
                reversal_id,
                document_id,
                [(row["account"], -row["amount"]) for row in original],
                effective,
                "reverse_document",
            )
            self.db.audit(
                "document_reversed",
                reversal_id,
                {"document": document_id, "effective": effective, "reason": reason},
            )
        return {"id": reversal_id, "existing": False}

    def available_from(self, document_id, effective):
        effective = day(effective)
        document = self.document(document_id)
        field = "invoice" if document["kind"] == "invoice" else "source"
        boundaries = {effective, "2200-12-31"}
        for row in self.db.rows(
            "SELECT effective,reversed FROM allocations WHERE " + field + "=?",
            (document_id,),
        ):
            for key in ("effective", "reversed"):
                if row[key] and row[key] >= effective:
                    boundaries.add(row[key])
        return min(self.balance(document_id, boundary) for boundary in boundaries)
