from ..primitives import (
    identifier,
    day,
    currency,
    money,
    obj,
    sequence,
    digest,
    canonical,
    decode,
    error,
    integer,
)

DDL = """
CREATE TABLE IF NOT EXISTS confirmations(id TEXT PRIMARY KEY,party TEXT NOT NULL,cutoff TEXT NOT NULL,
 payload TEXT NOT NULL,digest TEXT NOT NULL,state TEXT NOT NULL,revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS confirmation_decisions(confirmation TEXT NOT NULL,item TEXT NOT NULL,
 category TEXT NOT NULL,reason TEXT NOT NULL,operator TEXT NOT NULL,PRIMARY KEY(confirmation,item));
"""


class Confirmations:
    def __init__(self, database, ledger, aging):
        self.db = database
        self.ledger = ledger
        self.aging = aging
        self.db.connection.executescript(DDL)

    def submit(self, confirmation_id, party, cutoff, rows):
        identifier(confirmation_id)
        cutoff = day(cutoff)
        self.ledger.party(party)
        sequence(rows, 0, 10000)
        normalized = []
        seen = set()
        for row in rows:
            obj(
                row,
                {"key", "invoice", "reference", "currency", "open"},
                {"key", "currency", "open"},
            )
            key = identifier(row["key"])
            if key in seen:
                error("duplicate", "duplicate customer statement row")
            seen.add(key)
            code = currency(row["currency"])
            amount = money(row["open"], code, True)
            invoice = row.get("invoice")
            reference = row.get("reference", "")
            if invoice is not None:
                identifier(invoice)
            if not isinstance(reference, str) or len(reference) > 256:
                error("input", "invalid customer reference")
            if invoice is None and not reference.strip():
                error("input", "invoice or reference is required")
            normalized.append(
                {
                    "key": key,
                    "invoice": invoice,
                    "reference": reference.strip(),
                    "currency": code,
                    "open_minor": amount,
                }
            )
        normalized.sort(key=lambda row: row["key"])
        payload = {"party": party, "cutoff": cutoff, "rows": normalized}
        with self.db.transaction() as con:
            old = self.db.one(
                "SELECT digest FROM confirmations WHERE id=?", (confirmation_id,)
            )
            if old:
                if old["digest"] != digest(payload):
                    error("conflict", "customer confirmation differs")
                return self.get(confirmation_id)
            con.execute(
                "INSERT INTO confirmations VALUES(?,?,?,?,?,'open',1)",
                (confirmation_id, party, cutoff, canonical(payload), digest(payload)),
            )
            self.db.audit(
                "confirmation_submitted",
                confirmation_id,
                {"party": party, "cutoff": cutoff, "rows": len(rows)},
            )
        return self.get(confirmation_id)

    def get(self, confirmation_id):
        row = self.db.one("SELECT * FROM confirmations WHERE id=?", (confirmation_id,))
        if row is None:
            error("missing", "customer confirmation not found")
        payload = decode(row["payload"])
        if digest(payload) != row["digest"]:
            error("integrity", "customer confirmation fingerprint differs")
        return {
            **payload,
            "id": row["id"],
            "state": row["state"],
            "revision": row["revision"],
            "digest": row["digest"],
        }

    def reconcile(self, confirmation_id):
        confirmation = self.get(confirmation_id)
        local = self.aging.invoices(
            confirmation["cutoff"], confirmation["party"], include_settled=True
        )
        by_id = {row["id"]: row for row in local}
        used = set()
        rows = []
        for external in confirmation["rows"]:
            if external["invoice"]:
                candidate = by_id.get(external["invoice"])
                candidates = [candidate] if candidate else []
            else:
                candidates = [
                    row
                    for row in local
                    if row["reference"] == external["reference"]
                    and row["currency"] == external["currency"]
                ]
            item = {
                "key": external["key"],
                "external": external,
                "local": None,
                "difference_minor": None,
            }
            if len(candidates) > 1:
                item["status"] = "ambiguous_reference"
                item["candidates"] = [row["id"] for row in candidates]
            elif not candidates:
                item["status"] = "customer_only"
            else:
                candidate = candidates[0]
                item["local"] = candidate
                if candidate["id"] in used:
                    item["status"] = "duplicate_match"
                elif candidate["currency"] != external["currency"]:
                    item["status"] = "currency_mismatch"
                else:
                    used.add(candidate["id"])
                    difference = external["open_minor"] - candidate["open_minor"]
                    item["difference_minor"] = difference
                    item["status"] = (
                        "matched" if difference == 0 else "amount_difference"
                    )
            rows.append(item)
        for invoice in local:
            if invoice["id"] not in used and invoice["open_minor"]:
                rows.append(
                    {
                        "key": "local:" + invoice["id"],
                        "status": "local_only",
                        "external": None,
                        "local": invoice,
                        "difference_minor": -invoice["open_minor"],
                    }
                )
        decisions = {
            row["item"]: row
            for row in self.db.rows(
                "SELECT * FROM confirmation_decisions WHERE confirmation=?",
                (confirmation_id,),
            )
        }
        for row in rows:
            row["decision"] = decisions.get(row["key"])
        return {
            "id": confirmation_id,
            "revision": confirmation["revision"],
            "state": confirmation["state"],
            "rows": rows,
            "unresolved": sum(
                row["status"] != "matched" and row["decision"] is None for row in rows
            ),
            "baseline": digest(rows),
        }

    def decide(
        self, confirmation_id, item, category, reason, operator, expected_revision
    ):
        identifier(operator)
        integer(expected_revision, 1)
        if category not in {
            "timing",
            "missing_document",
            "customer_error",
            "internal_investigation",
        }:
            error("input", "unknown reconciliation category")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            error("input", "decision requires bounded explanation")
        with self.db.transaction() as con:
            confirmation = self.get(confirmation_id)
            if (
                confirmation["state"] != "open"
                or confirmation["revision"] != expected_revision
            ):
                error("conflict", "confirmation state or revision changed")
            report = self.reconcile(confirmation_id)
            row = next((row for row in report["rows"] if row["key"] == item), None)
            if row is None or row["status"] == "matched":
                error("input", "decision requires an actual discrepancy")
            con.execute(
                "INSERT INTO confirmation_decisions VALUES(?,?,?,?,?) ON CONFLICT(confirmation,item) DO UPDATE SET category=excluded.category,reason=excluded.reason,operator=excluded.operator",
                (confirmation_id, item, category, reason, operator),
            )
            con.execute(
                "UPDATE confirmations SET revision=revision+1 WHERE id=?",
                (confirmation_id,),
            )
            self.db.audit(
                "confirmation_decided",
                confirmation_id,
                {
                    "item": item,
                    "category": category,
                    "reason": reason,
                    "operator": operator,
                },
            )
        return self.reconcile(confirmation_id)

    def close(self, confirmation_id, expected_revision, baseline, operator):
        identifier(operator)
        integer(expected_revision, 1)
        with self.db.transaction() as con:
            confirmation = self.get(confirmation_id)
            if (
                confirmation["state"] != "open"
                or confirmation["revision"] != expected_revision
            ):
                error("conflict", "confirmation state or revision changed")
            report = self.reconcile(confirmation_id)
            if report["baseline"] != baseline:
                error("conflict", "reconciliation changed since review")
            if report["unresolved"]:
                error("state", "unresolved discrepancies remain")
            con.execute(
                "UPDATE confirmations SET state='closed',revision=revision+1 WHERE id=?",
                (confirmation_id,),
            )
            self.db.audit(
                "confirmation_closed",
                confirmation_id,
                {"baseline": baseline, "operator": operator},
            )
        return self.get(confirmation_id)
