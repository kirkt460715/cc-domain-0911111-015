from tempfile import TemporaryDirectory
from pathlib import Path
from .service import Workbench
from .primitives import BookError


def run():
    with TemporaryDirectory() as folder:
        with Workbench(Path(folder) / "book.sqlite3") as work:
            work.ledger.register_party("factory", "合成仪器厂", ["Instrument Factory"])
            for ident, kind, amount, due in [
                ("inv1", "invoice", "120.50", "2026-02-01"),
                ("inv2", "invoice", "80.00", "2026-02-10"),
                ("pay", "payment", "150.00", None),
            ]:
                row = {
                    "id": ident,
                    "kind": kind,
                    "party": "factory",
                    "currency": "CNY",
                    "amount": amount,
                    "issued": "2026-01-10",
                    "reference": "LOCAL-001",
                }
                if due:
                    row["due"] = due
                work.ledger.post(row)
            plan = work.matching.propose("plan", "pay", "2026-02-11", "oldest_due")
            assert len(plan["allocations"]) == 2
            work.matching.confirm("plan", "reviewer")
            assert work.ledger.balance("inv2") == 5050
            work.disputes.open("dispute", "inv2", "10.00", "2026-02-12", "合成质量争议")
            statement = work.statements.freeze("statement", "factory", "2026-02-28")
            assert statement["aging"]["groups"][0]["collectible_minor"] == 4050
            work.bank.add(
                {
                    "id": "bank1",
                    "account": "cash",
                    "currency": "CNY",
                    "amount": "150.00",
                    "booked": "2026-01-11",
                    "reference": "LOCAL-001",
                    "payer": "Instrument Factory",
                }
            )
            work.bank.link("bank1", "pay", "reviewer")
            work.periods.create("feb", "2026-02-01", "2026-03-01")
            preview = work.periods.preview("feb")
            work.periods.close("feb", preview["digest"], "controller")
            try:
                work.ledger.reverse_allocation(
                    "invalid", "plan:0", "2026-02-20", "late correction"
                )
            except BookError as exc:
                assert exc.code == "period"
            else:
                raise AssertionError("closed-period write accepted")
            work.ledger.reverse_allocation(
                "reverse", "plan:0", "2026-03-01", "valid subsequent correction"
            )
            assert work.periods.verify("feb")["verified"]
            work.statements.export("statement", Path(folder) / "statement.json")
            assert work.statements.verify(Path(folder) / "statement.json")["verified"]
            assert work.controls.require_valid()["ok"]

            # Frozen collection cohorts: write-offs reduce exposure but are
            # never presented as cash recovery. The two freezes differ only in
            # their knowledge cutoff, proving late facts cannot travel back.
            work.ledger.post(
                {
                    "id": "wo1",
                    "kind": "writeoff",
                    "party": "factory",
                    "currency": "CNY",
                    "amount": "20.00",
                    "issued": "2026-03-05",
                    "reference": "LOCAL-WO",
                }
            )
            work.ledger.allocate("wo:0", "wo1", "inv2", "20.00", "2026-03-05")
            first_freeze = work.vintages.freeze(
                "queue", "2026-03-31", "2026-03-31", note="march knowledge"
            )
            cohort = next(
                row
                for row in first_freeze["cohorts"]
                if row["currency"] == "CNY"
            )
            final_month = cohort["months"][-1]["cumulative"]
            # The write-off reduces exposure but is reported as its own loss
            # bucket, never mixed into cash recovery.
            assert final_month["writeoff_loss"] == "20.00"
            assert final_month["recovered"] == "29.50"
            conserved = (
                final_month["remaining_exposure_minor"]
                + final_month["recovered_minor"]
                + final_month["credit_applied_minor"]
                + final_month["writeoff_loss_minor"]
                + final_month["document_reversal_loss_minor"]
            )
            assert conserved == cohort["original_amount_minor"]
            # A late-known payment (business date in March, known in April).
            work.ledger.post(
                {
                    "id": "late_pay",
                    "kind": "payment",
                    "party": "factory",
                    "currency": "CNY",
                    "amount": "10.00",
                    "issued": "2026-03-20",
                    "recorded": "2026-04-12",
                    "reference": "LATE",
                }
            )
            work.ledger.allocate(
                "late:0",
                "late_pay",
                "inv2",
                "10.00",
                "2026-03-20",
                recorded="2026-04-12",
            )
            work.vintages.freeze(
                "queue", "2026-04-30", "2026-04-30", note="april knowledge"
            )
            comparison = work.vintages.compare("queue", 1, 2)
            assert not comparison["unchanged"]
            assert any(
                event["kind"] == "allocation:payment"
                for event in comparison["late_knowledge_events"]
            )
            vintage_path = Path(folder) / "vintage.json"
            work.vintages.export("queue", vintage_path, 2)
            assert work.vintages.verify(vintage_path)["verified"]
            return {
                "status": "ok",
                "historical_open_minor": 5050,
                "current_open_minor": work.ledger.balance("inv1")
                + work.ledger.balance("inv2"),
                "closed_period_verified": True,
                "vintage_versions": 2,
                "writeoff_kept_separate_from_recovery": True,
                "offline": True,
            }
