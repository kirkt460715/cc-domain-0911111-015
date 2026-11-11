from ..primitives import (
    identifier,
    day,
    display,
    canonical,
    digest,
    decode,
    error,
)


class Matcher:
    def __init__(self, database, ledger):
        self.db = database
        self.ledger = ledger

    def candidates(self, source, effective):
        effective = day(effective)
        payment = self.ledger.document(source)
        if payment["kind"] not in {"payment", "credit"}:
            error("matching", "matching source must be payment or credit")
        available = self.ledger.available_from(source, effective)
        candidates = []
        for invoice in self.db.rows(
            "SELECT * FROM documents WHERE kind='invoice' AND party=? AND currency=? AND issued<=? ORDER BY due,id",
            (payment["party"], payment["currency"], effective),
        ):
            outstanding = self.ledger.balance(invoice["id"], effective)
            current = self.ledger.balance(invoice["id"])
            outstanding = self.ledger.available_from(invoice["id"], effective)
            if outstanding <= 0:
                continue
            score = 0
            reasons = []
            if (
                payment["reference"]
                and payment["reference"].casefold() == invoice["reference"].casefold()
            ):
                score += 100
                reasons.append("exact_reference")
            if outstanding == available:
                score += 50
                reasons.append("exact_open_amount")
            if invoice["due"] <= effective:
                score += 10
                reasons.append("due")
            candidates.append(
                {
                    "invoice": invoice["id"],
                    "amount_minor": outstanding,
                    "amount": display(outstanding, payment["currency"]),
                    "due": invoice["due"],
                    "score": score,
                    "reasons": reasons,
                }
            )
        candidates.sort(key=lambda item: (-item["score"], item["due"], item["invoice"]))
        return {
            "source": source,
            "currency": payment["currency"],
            "available": display(available, payment["currency"]),
            "available_minor": available,
            "candidates": candidates,
            "ambiguous": len(candidates) > 1
            and candidates[0]["score"] == candidates[1]["score"],
        }

    def propose(self, plan_id, source, effective, strategy="exact"):
        identifier(plan_id)
        if strategy not in {"exact", "oldest_due", "reference"}:
            error("matching", "unknown matching strategy")
        effective = day(effective)
        with self.db.transaction() as con:
            old = self.db.one("SELECT * FROM plans WHERE id=?", (plan_id,))
            if old:
                payload = decode(old["payload"])
                if (payload["source"], payload["effective"], payload["strategy"]) != (
                    source,
                    effective,
                    strategy,
                ):
                    error("conflict", "matching plan identity differs")
                return self.get(plan_id)
            report = self.candidates(source, effective)
            remaining = min(report["available_minor"], self.ledger.balance(source))
            candidates = report["candidates"]
            ambiguity = False
            if strategy == "exact":
                candidates = [
                    item for item in candidates if item["amount_minor"] == remaining
                ]
                ambiguity = len(candidates) > 1
                if ambiguity:
                    candidates = []
            elif strategy == "reference":
                candidates = [
                    item for item in candidates if "exact_reference" in item["reasons"]
                ]
                ambiguity = len(candidates) > 1
                if ambiguity:
                    candidates = []
            else:
                candidates = sorted(
                    candidates, key=lambda item: (item["due"], item["invoice"])
                )
            allocations = []
            for candidate in candidates:
                taken = min(remaining, candidate["amount_minor"])
                if taken > 0:
                    allocations.append(
                        {
                            "invoice": candidate["invoice"],
                            "amount_minor": taken,
                            "amount": display(taken, report["currency"]),
                            "baseline": self.ledger.balance(candidate["invoice"]),
                        }
                    )
                    remaining -= taken
                if not remaining:
                    break
            payload = {
                "id": plan_id,
                "source": source,
                "effective": effective,
                "strategy": strategy,
                "currency": report["currency"],
                "source_baseline": self.ledger.balance(source),
                "allocations": allocations,
                "unapplied_minor": remaining,
                "unapplied": display(remaining, report["currency"]),
                "ambiguous": ambiguity,
            }
            con.execute(
                "INSERT INTO plans VALUES(?,?,?,'proposed',NULL)",
                (plan_id, canonical(payload), digest(payload)),
            )
            self.db.audit("matching_proposed", plan_id, payload)
        return self.get(plan_id)

    def get(self, plan_id):
        row = self.db.one("SELECT * FROM plans WHERE id=?", (plan_id,))
        if row is None:
            error("missing", "matching plan not found")
        payload = decode(row["payload"])
        if digest(payload) != row["digest"]:
            error("integrity", "matching plan fingerprint differs")
        return {
            **payload,
            "state": row["state"],
            "result": decode(row["result"]) if row["result"] else None,
        }

    def confirm(self, plan_id, operator):
        identifier(operator)
        with self.db.transaction() as con:
            plan = self.get(plan_id)
            if plan["state"] == "confirmed":
                return plan
            if (
                plan["state"] != "proposed"
                or plan["ambiguous"]
                or not plan["allocations"]
            ):
                error("matching", "plan cannot be confirmed")
            if self.ledger.balance(plan["source"]) != plan["source_baseline"]:
                error("conflict", "source balance changed")
            for item in plan["allocations"]:
                if self.ledger.balance(item["invoice"]) != item["baseline"]:
                    error(
                        "conflict", "invoice balance changed", invoice=item["invoice"]
                    )
            ids = []
            for index, item in enumerate(plan["allocations"]):
                allocation_id = plan_id + ":" + str(index)
                self.ledger.allocate(
                    allocation_id,
                    plan["source"],
                    item["invoice"],
                    item["amount"],
                    plan["effective"],
                    "plan:" + plan_id,
                )
                ids.append(allocation_id)
            result = {"operator": operator, "allocations": ids}
            con.execute(
                "UPDATE plans SET state='confirmed',result=? WHERE id=?",
                (canonical(result), plan_id),
            )
            self.db.audit("matching_confirmed", plan_id, result)
        return self.get(plan_id)

    def cancel(self, plan_id, reason):
        if not isinstance(reason, str) or not reason.strip():
            error("input", "matching cancellation requires reason")
        with self.db.transaction() as con:
            plan = self.get(plan_id)
            if plan["state"] != "proposed":
                error("state", "only proposed plan can be cancelled")
            con.execute(
                "UPDATE plans SET state='cancelled',result=? WHERE id=?",
                (canonical({"reason": reason}), plan_id),
            )
            self.db.audit("matching_cancelled", plan_id, {"reason": reason})
        return self.get(plan_id)
