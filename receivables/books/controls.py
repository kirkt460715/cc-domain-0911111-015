from ..primitives import day, display, digest, decode, error


class Controls:
    def __init__(self, database, ledger):
        self.db = database
        self.ledger = ledger

    def trial_balance(self, cutoff):
        cutoff = day(cutoff)
        groups = {}
        for row in self.db.rows(
            "SELECT p.*,d.currency FROM postings p JOIN documents d ON d.id=p.document WHERE p.effective<=? ORDER BY p.sequence",
            (cutoff,),
        ):
            accounts = groups.setdefault(row["currency"], {})
            accounts[row["account"]] = accounts.get(row["account"], 0) + row["amount"]
        result = []
        for code, accounts in sorted(groups.items()):
            result.append(
                {
                    "currency": code,
                    "accounts_minor": accounts,
                    "accounts": {
                        key: display(value, code) for key, value in accounts.items()
                    },
                    "difference_minor": sum(accounts.values()),
                }
            )
        return {"cutoff": cutoff, "currencies": result}

    def subledger(self, cutoff):
        cutoff = day(cutoff)
        expected = {}
        for document in self.db.rows("SELECT * FROM documents ORDER BY id"):
            amounts = expected.setdefault(
                document["currency"], {"receivable": 0, "unapplied": 0}
            )
            account = "receivable" if document["kind"] == "invoice" else "unapplied"
            sign = 1 if account == "receivable" else -1
            amounts[account] += sign * self.ledger.balance(document["id"], cutoff)
        actual = {
            row["currency"]: row["accounts_minor"]
            for row in self.trial_balance(cutoff)["currencies"]
        }
        differences = []
        for code, accounts in sorted(expected.items()):
            for account, value in accounts.items():
                differences.append(
                    {
                        "currency": code,
                        "account": account,
                        "expected_minor": value,
                        "actual_minor": actual.get(code, {}).get(account, 0),
                        "difference_minor": actual.get(code, {}).get(account, 0)
                        - value,
                    }
                )
        return {
            "cutoff": cutoff,
            "differences": differences,
            "balanced": all(row["difference_minor"] == 0 for row in differences),
        }

    def inspect(self):
        findings = []
        for document in self.db.rows("SELECT * FROM documents ORDER BY id"):
            if digest(decode(document["payload"])) != document["digest"]:
                findings.append({"kind": "document_digest", "id": document["id"]})
            boundaries = {document["issued"], "2200-12-31"}
            for allocation in self.db.rows(
                "SELECT * FROM allocations WHERE source=? OR invoice=?",
                (document["id"], document["id"]),
            ):
                boundaries.add(allocation["effective"])
                if allocation["reversed"]:
                    boundaries.add(allocation["reversed"])
            for boundary in sorted(boundaries):
                balance = self.ledger.balance(document["id"], boundary)
                if balance < 0 or balance > document["amount"]:
                    findings.append(
                        {
                            "kind": "historical_balance",
                            "id": document["id"],
                            "date": boundary,
                            "balance": balance,
                        }
                    )
        for row in self.db.rows(
            "SELECT entry,effective,SUM(amount) AS n FROM postings GROUP BY entry,effective HAVING SUM(amount)!=0"
        ):
            findings.append({"kind": "unbalanced_entry", **row})
        for link in self.db.rows("SELECT * FROM bank_links"):
            payment = self.ledger.document(link["payment"])
            line = self.db.one("SELECT * FROM bank_lines WHERE id=?", (link["line"],))
            if (
                line is None
                or payment["kind"] != "payment"
                or payment["amount"] != line["amount"]
                or payment["currency"] != line["currency"]
            ):
                findings.append({"kind": "bank_mismatch", "line": link["line"]})
        dates = {
            row["effective"]
            for row in self.db.rows("SELECT DISTINCT effective FROM postings")
        }
        for cutoff in sorted(dates):
            report = self.subledger(cutoff)
            if not report["balanced"]:
                findings.append({"kind": "subledger_difference", **report})
        audit = self.db.verify_audit()
        return {
            "ok": not findings,
            "findings": findings,
            "audit": audit,
            "dates_checked": len(dates),
        }

    def require_valid(self):
        report = self.inspect()
        if not report["ok"]:
            error(
                "integrity", "ledger control checks failed", findings=report["findings"]
            )
        return report

    def journal(self, start, end, party=None):
        start, end = day(start), day(end)
        if end <= start:
            error("input", "journal interval must increase")
        if party is not None:
            self.ledger.party(party)
        rows = self.db.rows(
            "SELECT p.*,d.party,d.currency FROM postings p JOIN documents d ON d.id=p.document WHERE p.effective>=? AND p.effective<? ORDER BY p.effective,p.sequence",
            (start, end),
        )
        return [
            dict(row, amount=display(row["amount"], row["currency"]))
            for row in rows
            if party is None or row["party"] == party
        ]
