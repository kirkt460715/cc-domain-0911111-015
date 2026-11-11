from datetime import date
from ..primitives import day, integer, display


class CollectionQueue:
    def __init__(self, database, ledger, aging):
        self.db = database
        self.ledger = ledger
        self.aging = aging

    def build(self, cutoff, min_days=1, limit=100):
        cutoff = day(cutoff)
        integer(min_days, 0, 36500)
        integer(limit, 1, 10000)
        rows = []
        for invoice in self.aging.invoices(cutoff):
            days = (
                date.fromisoformat(cutoff) - date.fromisoformat(invoice["due"])
            ).days
            if days < min_days or invoice["collectible_minor"] == 0:
                continue
            recent = self.db.one(
                "SELECT MAX(effective) AS day FROM allocations WHERE invoice=? AND effective<=?",
                (invoice["id"], cutoff),
            )["day"]
            rows.append(
                {
                    "invoice": invoice["id"],
                    "party": invoice["party"],
                    "currency": invoice["currency"],
                    "days_overdue": days,
                    "collectible_minor": invoice["collectible_minor"],
                    "collectible": display(
                        invoice["collectible_minor"], invoice["currency"]
                    ),
                    "disputed_minor": invoice["disputed_minor"],
                    "last_allocation": recent,
                }
            )
        # Currency values are never combined or compared across currency groups.
        rows.sort(
            key=lambda row: (
                row["currency"],
                -row["days_overdue"],
                -row["collectible_minor"],
                row["invoice"],
            )
        )
        return {
            "cutoff": cutoff,
            "eligible": len(rows),
            "rows": rows[:limit],
            "truncated": len(rows) > limit,
        }

    def exposure(self, party, cutoff):
        self.ledger.party(party)
        cutoff = day(cutoff)
        aging = self.aging.report(cutoff, party)
        unapplied = self.aging.unapplied(cutoff, party)
        codes = {row["currency"] for row in aging["groups"]} | {
            row["currency"] for row in unapplied
        }
        result = []
        for code in sorted(codes):
            group = next(
                (row for row in aging["groups"] if row["currency"] == code), None
            )
            gross = group["open_minor"] if group else 0
            available = sum(
                row["available_minor"] for row in unapplied if row["currency"] == code
            )
            result.append(
                {
                    "currency": code,
                    "gross_receivable_minor": gross,
                    "unapplied_minor": available,
                    "net_exposure_minor": gross - available,
                    "net_exposure": display(gross - available, code),
                    "disputed_minor": group["disputed_minor"] if group else 0,
                }
            )
        return {"party": party, "cutoff": cutoff, "currencies": result}

    def forecast(self, cutoff, horizon_days):
        from datetime import timedelta

        cutoff = day(cutoff)
        integer(horizon_days, 1, 366)
        end = (date.fromisoformat(cutoff) + timedelta(days=horizon_days)).isoformat()
        groups = {}
        for invoice in self.aging.invoices(cutoff):
            if invoice["due"] > end:
                continue
            expected = max(cutoff, invoice["due"])
            key = (expected, invoice["currency"])
            row = groups.setdefault(
                key,
                {
                    "date": expected,
                    "currency": invoice["currency"],
                    "collectible_minor": 0,
                    "disputed_minor": 0,
                    "invoices": [],
                },
            )
            row["collectible_minor"] += invoice["collectible_minor"]
            row["disputed_minor"] += invoice["disputed_minor"]
            row["invoices"].append(invoice["id"])
        return {
            "cutoff": cutoff,
            "through": end,
            "basis": "contractual_due_dates_not_payment_prediction",
            "rows": [groups[key] for key in sorted(groups)],
        }

    def exceptions(self, cutoff):
        cutoff = day(cutoff)
        result = []
        for row in self.aging.unapplied(cutoff):
            days = (date.fromisoformat(cutoff) - date.fromisoformat(row["issued"])).days
            if days > 30:
                result.append(
                    {
                        "type": "long_unapplied",
                        "id": row["id"],
                        "days": days,
                        "currency": row["currency"],
                        "amount_minor": row["available_minor"],
                    }
                )
        for row in self.db.rows(
            "SELECT id,payload FROM plans WHERE state='proposed' ORDER BY id"
        ):
            from ..primitives import decode

            plan = decode(row["payload"])
            if plan["effective"] > cutoff:
                continue
            if plan["ambiguous"]:
                result.append({"type": "ambiguous_plan", "id": row["id"]})
            elif self.ledger.balance(plan["source"]) != plan["source_baseline"]:
                result.append({"type": "stale_plan", "id": row["id"]})
        for row in self.db.rows(
            "SELECT b.id,b.booked FROM bank_lines b LEFT JOIN bank_links l ON l.line=b.id WHERE l.line IS NULL AND b.booked<=? ORDER BY b.id",
            (cutoff,),
        ):
            result.append(
                {"type": "unmatched_bank", "id": row["id"], "booked": row["booked"]}
            )
        return result
