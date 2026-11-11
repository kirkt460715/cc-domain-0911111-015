from datetime import date
from ..primitives import day, display, error, currency


class Aging:
    def __init__(self, database, ledger):
        self.db = database
        self.ledger = ledger

    def disputed(self, invoice, cutoff):
        return self.db.one(
            "SELECT COALESCE(SUM(amount),0) AS n FROM disputes WHERE invoice=? AND opened<=? AND (closed IS NULL OR closed>?)",
            (invoice, cutoff, cutoff),
        )["n"]

    def bucket(self, due, cutoff):
        late = (date.fromisoformat(cutoff) - date.fromisoformat(due)).days
        if late <= 0:
            return "not_due"
        if late <= 30:
            return "1_30"
        if late <= 60:
            return "31_60"
        if late <= 90:
            return "61_90"
        return "over_90"

    def invoices(self, cutoff, party=None, code=None, include_settled=False):
        cutoff = day(cutoff)
        if party is not None:
            self.ledger.party(party)
        if code is not None:
            currency(code)
        result = []
        for row in self.db.rows(
            "SELECT * FROM documents WHERE kind='invoice' AND issued<=? ORDER BY party,currency,due,id",
            (cutoff,),
        ):
            if party is not None and row["party"] != party:
                continue
            if code is not None and row["currency"] != code:
                continue
            if not self.ledger.active_at(row["id"], cutoff):
                continue
            balance = self.ledger.balance(row["id"], cutoff)
            if balance == 0 and not include_settled:
                continue
            disputed = min(balance, self.disputed(row["id"], cutoff))
            result.append(
                {
                    "id": row["id"],
                    "party": row["party"],
                    "currency": row["currency"],
                    "issued": row["issued"],
                    "due": row["due"],
                    "reference": row["reference"],
                    "original_minor": row["amount"],
                    "open_minor": balance,
                    "open": display(balance, row["currency"]),
                    "disputed_minor": disputed,
                    "collectible_minor": balance - disputed,
                    "bucket": self.bucket(row["due"], cutoff),
                }
            )
        return result

    def report(self, cutoff, party=None):
        cutoff = day(cutoff)
        invoices = self.invoices(cutoff, party)
        groups = {}
        for row in invoices:
            key = (row["party"], row["currency"])
            group = groups.setdefault(
                key,
                {
                    "party": key[0],
                    "currency": key[1],
                    "open_minor": 0,
                    "disputed_minor": 0,
                    "collectible_minor": 0,
                    "buckets": {
                        k: 0 for k in ["not_due", "1_30", "31_60", "61_90", "over_90"]
                    },
                    "invoices": 0,
                },
            )
            for field in ("open_minor", "disputed_minor", "collectible_minor"):
                group[field] += row[field]
            group["buckets"][row["bucket"]] += row["open_minor"]
            group["invoices"] += 1
        for group in groups.values():
            code = group["currency"]
            group["open"] = display(group["open_minor"], code)
            group["collectible"] = display(group["collectible_minor"], code)
            group["disputed"] = display(group["disputed_minor"], code)
            if sum(group["buckets"].values()) != group["open_minor"]:
                error("integrity", "aging bucket mismatch")
        return {"cutoff": cutoff, "groups": list(groups.values()), "invoices": invoices}

    def unapplied(self, cutoff, party=None):
        cutoff = day(cutoff)
        if party is not None:
            self.ledger.party(party)
        rows = []
        for row in self.db.rows(
            "SELECT * FROM documents WHERE kind IN ('payment','credit') AND issued<=? ORDER BY issued,id",
            (cutoff,),
        ):
            if party is not None and row["party"] != party:
                continue
            balance = self.ledger.balance(row["id"], cutoff)
            if balance > 0:
                rows.append(
                    {
                        "id": row["id"],
                        "kind": row["kind"],
                        "party": row["party"],
                        "currency": row["currency"],
                        "available_minor": balance,
                        "available": display(balance, row["currency"]),
                        "issued": row["issued"],
                    }
                )
        return rows

    def movement(self, invoice, start, end):
        start, end = day(start), day(end)
        if end < start:
            error("input", "movement interval is reversed")
        document = self.ledger.document(invoice)
        if document["kind"] != "invoice":
            error("input", "movement requires invoice")
        events = []
        for allocation in self.db.rows(
            "SELECT * FROM allocations WHERE invoice=? ORDER BY effective,id",
            (invoice,),
        ):
            if start < allocation["effective"] <= end:
                events.append(
                    {
                        "date": allocation["effective"],
                        "kind": "allocation",
                        "id": allocation["id"],
                        "change_minor": -allocation["amount"],
                    }
                )
            if allocation["reversed"] and start < allocation["reversed"] <= end:
                events.append(
                    {
                        "date": allocation["reversed"],
                        "kind": "reversal",
                        "id": allocation["id"],
                        "change_minor": allocation["amount"],
                    }
                )
        if start < document["issued"] <= end:
            events.append(
                {
                    "date": document["issued"],
                    "kind": "issue",
                    "id": invoice,
                    "change_minor": document["amount"],
                }
            )
        reversal = self.db.one(
            "SELECT * FROM document_reversals WHERE document=?", (invoice,)
        )
        if reversal and start < reversal["effective"] <= end:
            events.append(
                {
                    "date": reversal["effective"],
                    "kind": "document_reversal",
                    "id": reversal["reversal"],
                    "change_minor": -document["amount"],
                }
            )
        events.sort(key=lambda row: (row["date"], row["kind"], row["id"]))
        opening = self.ledger.balance(invoice, start)
        closing = self.ledger.balance(invoice, end)
        if opening + sum(row["change_minor"] for row in events) != closing:
            error("integrity", "invoice movement does not reconcile")
        return {
            "invoice": invoice,
            "currency": document["currency"],
            "start": start,
            "end": end,
            "opening_minor": opening,
            "closing_minor": closing,
            "events": events,
        }
