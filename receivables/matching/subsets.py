from ..primitives import (
    day,
    integer,
    display,
    error,
    digest,
    canonical,
    decode,
    identifier,
)


class SubsetMatcher:
    def __init__(self, database, ledger):
        self.db = database
        self.ledger = ledger

    def search(self, source, effective, maximum_invoices=20, maximum_solutions=10):
        effective = day(effective)
        integer(maximum_invoices, 1, 28)
        integer(maximum_solutions, 1, 100)
        payment = self.ledger.document(source)
        if payment["kind"] not in {"payment", "credit"}:
            error("matching", "subset source must be payment or credit")
        target = self.ledger.available_from(source, effective)
        if target <= 0:
            error("matching", "source has no available amount")
        candidates = []
        for row in self.db.rows(
            "SELECT * FROM documents WHERE kind='invoice' AND party=? AND currency=? AND issued<=? ORDER BY due,id",
            (payment["party"], payment["currency"], effective),
        ):
            available = self.ledger.available_from(row["id"], effective)
            if 0 < available <= target:
                candidates.append(
                    {"id": row["id"], "minor": available, "due": row["due"]}
                )
        if len(candidates) > maximum_invoices:
            error(
                "capacity",
                "candidate limit exceeded; narrow source party or use manual matching",
                candidates=len(candidates),
                limit=maximum_invoices,
            )
        middle = len(candidates) // 2
        left = candidates[:middle]
        right = candidates[middle:]

        def enumerate_sums(items):
            sums = {0: [()]}
            for item in items:
                additions = {}
                for subtotal, choices in sums.items():
                    value = subtotal + item["minor"]
                    if value > target:
                        continue
                    for choice in choices:
                        additions.setdefault(value, []).append(choice + (item["id"],))
                for value, choices in additions.items():
                    sums.setdefault(value, []).extend(choices)
                    sums[value] = sums[value][: maximum_solutions + 1]
            return sums

        left_sums = enumerate_sums(left)
        right_sums = enumerate_sums(right)
        found = []
        truncated = False
        for subtotal in sorted(left_sums):
            for first in left_sums[subtotal]:
                for second in right_sums.get(target - subtotal, []):
                    choice = tuple(sorted(first + second))
                    if not choice:
                        continue
                    found.append(choice)
                    if len(found) > maximum_solutions:
                        truncated = True
                        break
                if truncated:
                    break
            if truncated:
                break
        found = sorted(set(found))
        lookup = {row["id"]: row for row in candidates}
        solutions = []
        for choice in found[:maximum_solutions]:
            items = [
                {
                    "invoice": key,
                    "amount_minor": lookup[key]["minor"],
                    "amount": display(lookup[key]["minor"], payment["currency"]),
                }
                for key in choice
            ]
            solutions.append(
                {
                    "invoices": items,
                    "sum_minor": sum(item["amount_minor"] for item in items),
                    "fingerprint": digest(items),
                }
            )
        return {
            "source": source,
            "effective": effective,
            "currency": payment["currency"],
            "target_minor": target,
            "candidates": candidates,
            "solutions": solutions,
            "ambiguous": len(solutions) > 1 or truncated,
            "truncated": truncated,
        }

    def propose(self, plan_id, source, effective, solution_fingerprint):
        identifier(plan_id)
        report = self.search(source, effective)
        selected = next(
            (
                row
                for row in report["solutions"]
                if row["fingerprint"] == solution_fingerprint
            ),
            None,
        )
        if selected is None:
            error("conflict", "selected subset is no longer available")
        with self.db.transaction() as con:
            old = self.db.one("SELECT payload FROM plans WHERE id=?", (plan_id,))
            payload = {
                "id": plan_id,
                "source": source,
                "effective": effective,
                "strategy": "selected_subset",
                "currency": report["currency"],
                "source_baseline": self.ledger.balance(source),
                "allocations": [
                    {**item, "baseline": self.ledger.balance(item["invoice"])}
                    for item in selected["invoices"]
                ],
                "unapplied_minor": 0,
                "unapplied": display(0, report["currency"]),
                "ambiguous": False,
                "selection": solution_fingerprint,
            }
            if old:
                if decode(old["payload"]) != payload:
                    error("conflict", "subset plan identity differs")
                return payload
            # Recheck at transaction boundary; the search does not hold a writer lock.
            if self.ledger.available_from(source, effective) != report["target_minor"]:
                error("conflict", "source changed during subset search")
            for item in payload["allocations"]:
                if (
                    self.ledger.available_from(item["invoice"], effective)
                    != item["amount_minor"]
                ):
                    error("conflict", "invoice changed during subset search")
            con.execute(
                "INSERT INTO plans VALUES(?,?,?,'proposed',NULL)",
                (plan_id, canonical(payload), digest(payload)),
            )
            self.db.audit(
                "subset_selected", plan_id, {"selection": solution_fingerprint}
            )
        return payload
