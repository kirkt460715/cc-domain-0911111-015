from ..primitives import (
    identifier,
    day,
    currency,
    money,
    display,
    obj,
    canonical,
    digest,
    error,
)


class BankEvidence:
    def __init__(self, database, ledger):
        self.db = database
        self.ledger = ledger

    def add(self, data):
        fields = {"id", "account", "currency", "amount", "booked", "reference", "payer"}
        obj(data, fields, fields)
        ident = identifier(data["id"])
        code = currency(data["currency"])
        amount = money(data["amount"], code)
        booked = day(data["booked"])
        for field in ("reference", "payer"):
            if not isinstance(data[field], str) or len(data[field]) > 512:
                error("input", "bank reference and payer must be bounded text")
        payload = {
            **data,
            "amount": display(amount, code),
            "booked": booked,
            "account": identifier(data["account"]),
        }
        with self.db.transaction() as con:
            old = self.db.one("SELECT digest FROM bank_lines WHERE id=?", (ident,))
            if old:
                if old["digest"] != digest(payload):
                    error("conflict", "bank line identity differs")
                return {"id": ident, "existing": True}
            con.execute(
                "INSERT INTO bank_lines VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    payload["account"],
                    code,
                    amount,
                    booked,
                    data["reference"],
                    data["payer"],
                    canonical(payload),
                    digest(payload),
                ),
            )
            self.db.audit("bank_line_added", ident, payload)
        return {"id": ident, "existing": False}

    def line(self, line_id):
        row = self.db.one("SELECT * FROM bank_lines WHERE id=?", (line_id,))
        if row is None:
            error("missing", "bank line not found")
        return row

    def candidates(self, line_id):
        from datetime import date

        line = self.line(line_id)
        if self.db.one("SELECT line FROM bank_links WHERE line=?", (line_id,)):
            return {"line": line_id, "candidates": [], "already_linked": True}
        result = []
        for payment in self.db.rows(
            "SELECT * FROM documents WHERE kind='payment' AND currency=? AND amount=? ORDER BY id",
            (line["currency"], line["amount"]),
        ):
            if not self.ledger.active_at(payment["id"], "2200-12-31"):
                continue
            if self.db.one(
                "SELECT line FROM bank_links WHERE payment=?", (payment["id"],)
            ):
                continue
            party = self.ledger.party(payment["party"])
            score = 0
            evidence = []
            if (
                line["reference"]
                and line["reference"].casefold() == payment["reference"].casefold()
            ):
                score += 70
                evidence.append("exact_reference")
            aliases = {party["name"].casefold(), *party["aliases"]}
            if line["payer"].strip().casefold() in aliases:
                score += 20
                evidence.append("payer_alias")
            delta = abs(
                (
                    date.fromisoformat(line["booked"])
                    - date.fromisoformat(payment["issued"])
                ).days
            )
            if delta <= 3:
                score += 10 - delta
                evidence.append("near_date")
            result.append(
                {
                    "payment": payment["id"],
                    "party": payment["party"],
                    "score": score,
                    "evidence": evidence,
                    "day_difference": delta,
                }
            )
        result.sort(key=lambda item: (-item["score"], item["payment"]))
        ambiguous = len(result) > 1 and result[0]["score"] == result[1]["score"]
        return {
            "line": line_id,
            "candidates": result,
            "ambiguous": ambiguous,
            "already_linked": False,
        }

    def link(self, line_id, payment_id, operator):
        identifier(operator)
        with self.db.transaction() as con:
            line = self.line(line_id)
            payment = self.ledger.document(payment_id)
            if (
                payment["kind"] != "payment"
                or payment["currency"] != line["currency"]
                or payment["amount"] != line["amount"]
            ):
                error("bank", "bank evidence amount/currency/payment kind mismatch")
            if not self.ledger.active_at(payment_id, "2200-12-31"):
                error("bank", "cannot link reversed payment")
            existing = self.db.one("SELECT * FROM bank_links WHERE line=?", (line_id,))
            if existing:
                if existing["payment"] != payment_id:
                    error("conflict", "bank line linked to other payment")
                return {**existing, "existing": True}
            if self.db.one(
                "SELECT line FROM bank_links WHERE payment=?", (payment_id,)
            ):
                error("conflict", "payment already linked to another bank line")
            con.execute(
                "INSERT INTO bank_links VALUES(?,?,?)", (line_id, payment_id, operator)
            )
            self.db.audit(
                "bank_linked", line_id, {"payment": payment_id, "operator": operator}
            )
        return {
            "line": line_id,
            "payment": payment_id,
            "linked_by": operator,
            "existing": False,
        }

    def unlink(self, line_id, reason):
        if not isinstance(reason, str) or not reason.strip():
            error("input", "unlink requires reason")
        with self.db.transaction() as con:
            link = self.db.one("SELECT * FROM bank_links WHERE line=?", (line_id,))
            if link is None:
                error("missing", "bank link not found")
            con.execute("DELETE FROM bank_links WHERE line=?", (line_id,))
            self.db.audit(
                "bank_unlinked", line_id, {"payment": link["payment"], "reason": reason}
            )
        return link

    def unmatched(self, account=None):
        rows = self.db.rows(
            "SELECT b.* FROM bank_lines b LEFT JOIN bank_links l ON l.line=b.id WHERE l.line IS NULL ORDER BY b.booked,b.id"
        )
        return [row for row in rows if account is None or row["account"] == account]

    def reconcile(self, account, start, end):
        start, end = day(start), day(end)
        if end <= start:
            error("input", "reconciliation interval must increase")
        groups = {}
        for line in self.db.rows(
            "SELECT * FROM bank_lines WHERE account=? AND booked>=? AND booked<? ORDER BY booked,id",
            (account, start, end),
        ):
            values = groups.setdefault(
                line["currency"], {"total": 0, "linked": 0, "unmatched": 0, "lines": 0}
            )
            values["total"] += line["amount"]
            values["lines"] += 1
            linked = self.db.one(
                "SELECT payment FROM bank_links WHERE line=?", (line["id"],)
            )
            values["linked" if linked else "unmatched"] += line["amount"]
        return {
            "account": account,
            "start": start,
            "end": end,
            "currencies": {
                code: {
                    key: display(value, code) if key != "lines" else value
                    for key, value in values.items()
                }
                for code, values in groups.items()
            },
        }
