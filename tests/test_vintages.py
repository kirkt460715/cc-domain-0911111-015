import unittest
import tempfile
from pathlib import Path
from receivables.service import Workbench
from receivables.primitives import BookError, display


class VintageCase(unittest.TestCase):
    def setUp(self):
        self.w = Workbench()
        self.addCleanup(self.w.close)
        self.w.ledger.register_party("a", "Alpha", ["A Ltd"])
        self.w.ledger.register_party("b", "Beta")

    def invoice(self, ident, amount="100", due="2026-01-31", **extra):
        row = dict(id=ident, kind="invoice", party="a", currency="CNY",
                   amount=amount, issued="2026-01-01", reference="R", due=due)
        row.update(extra)
        self.w.ledger.post(row)
        return row

    def source(self, ident, kind, amount, **extra):
        row = dict(id=ident, kind=kind, party="a", currency="CNY",
                   amount=amount, issued="2026-02-15", reference="R")
        row.update(extra)
        self.w.ledger.post(row)
        return row

    def cohort(self, payload, entry, code="CNY"):
        rows = [c for c in payload["cohorts"]
                if c["entry_month_end"] == entry and c["currency"] == code]
        self.assertEqual(len(rows), 1, "cohort not found")
        return rows[0]

    # ------------------------------------------------------------- freezing
    def test_cohort_freezes_on_first_overdue_month_end(self):
        # Due 2026-01-31 is not overdue at the 2026-01-31 month-end; it first
        # enters the queue at 2026-02-28.
        self.invoice("i1", "100", due="2026-01-31")
        self.invoice("i2", "200", due="2026-01-20")
        payload = self.w.vintages.build("q", "2026-03-31")
        keys = sorted((c["entry_month_end"], c["members"]) for c in payload["cohorts"])
        self.assertEqual(keys, [("2026-01-31", 1), ("2026-02-28", 1)])
        jan = self.cohort(payload, "2026-01-31")
        self.assertEqual(jan["member_rows"][0]["invoice"], "i2")
        feb = self.cohort(payload, "2026-02-28")
        self.assertEqual(feb["member_rows"][0]["invoice"], "i1")

    def test_new_invoice_never_joins_old_cohort(self):
        self.invoice("old", "100", due="2026-01-31")
        # Issued after the first cohort already existed.
        self.invoice("new", "100", issued="2026-03-05", due="2026-03-20")
        payload = self.w.vintages.build("q", "2026-04-30")
        old = self.cohort(payload, "2026-02-28")
        self.assertEqual([m["invoice"] for m in old["member_rows"]], ["old"])
        new = self.cohort(payload, "2026-03-31")
        self.assertEqual([m["invoice"] for m in new["member_rows"]], ["new"])

    def test_invoice_settled_before_first_overdue_month_never_queues(self):
        self.invoice("i", "100", due="2026-01-31")
        self.source("p", "payment", "100", issued="2026-01-20")
        self.w.ledger.allocate("al", "p", "i", "100", "2026-01-25")
        payload = self.w.vintages.build("q", "2026-03-31")
        self.assertEqual(payload["cohorts"], [])

    def test_party_and_currency_filters_apply(self):
        self.invoice("i", "100")
        self.invoice("u", "50", currency="USD")
        self.w.ledger.post(dict(id="b1", kind="invoice", party="b", currency="CNY",
                                amount="70", issued="2026-01-01", due="2026-01-31",
                                reference="R"))
        payload = self.w.vintages.build("q", "2026-03-31", party="a", currency="USD")
        self.assertEqual(len(payload["cohorts"]), 1)
        self.assertEqual(payload["cohorts"][0]["currency"], "USD")
        with self.assertRaises(BookError):
            self.w.vintages.build("q", "2026-03-31", currency="GBP")

    # ------------------------------------------------------------- flow paths
    def test_recovery_credit_writeoff_and_remaining_conserve(self):
        self.invoice("i", "100", due="2026-01-31")
        self.source("p", "payment", "40", issued="2026-03-02")
        self.w.ledger.allocate("al1", "p", "i", "40", "2026-03-02")
        self.source("c", "credit", "20", issued="2026-04-02")
        self.w.ledger.allocate("al2", "c", "i", "20", "2026-04-02")
        self.source("w", "writeoff", "25", issued="2026-05-02")
        self.w.ledger.allocate("al3", "w", "i", "25", "2026-05-02")
        payload = self.w.vintages.build("q", "2026-05-31")
        cohort = self.cohort(payload, "2026-02-28")
        final = cohort["months"][-1]["cumulative"]
        self.assertEqual(final["recovered_minor"], 4000)
        self.assertEqual(final["credit_applied_minor"], 2000)
        self.assertEqual(final["writeoff_loss_minor"], 2500)
        self.assertEqual(final["remaining_exposure_minor"], 1500)
        # Conservation against the original 100 invoice amount.
        self.assertEqual(
            final["recovered_minor"] + final["credit_applied_minor"]
            + final["writeoff_loss_minor"] + final["remaining_exposure_minor"]
            + final["document_reversal_loss_minor"],
            cohort["original_amount_minor"],
        )

    def test_writeoff_is_never_counted_as_recovery(self):
        self.invoice("i", "100")
        self.source("w", "writeoff", "100", issued="2026-03-02")
        self.w.ledger.allocate("al", "w", "i", "100", "2026-03-02")
        payload = self.w.vintages.build("q", "2026-03-31")
        cohort = self.cohort(payload, "2026-02-28")
        final = cohort["months"][-1]
        self.assertEqual(final["cumulative"]["recovered_minor"], 0)
        self.assertEqual(final["cumulative"]["writeoff_loss_minor"], 10000)
        self.assertEqual(final["state_counts"]["writeoff_settled"], 1)
        self.assertEqual(final["state_counts"]["settled"], 0)

    def test_partial_writeoff_reinstatement_flows_back_to_exposure(self):
        self.invoice("i", "100")
        self.source("w", "writeoff", "60", issued="2026-03-02")
        self.w.ledger.allocate("al", "w", "i", "60", "2026-03-02")
        self.w.ledger.reverse_allocation("ral", "al", "2026-04-10", "write-off reinstated")
        payload = self.w.vintages.build("q", "2026-04-30")
        cohort = self.cohort(payload, "2026-02-28")
        final = cohort["months"][-1]
        self.assertEqual(final["cumulative"]["writeoff_loss_minor"], 0)
        self.assertEqual(final["cumulative"]["remaining_exposure_minor"], 10000)
        self.assertEqual(final["interval_flow_minor"]["writeoff_reinstated"], 6000)

    def test_document_reversal_is_loss_not_recovery(self):
        self.invoice("i", "100")
        self.w.ledger.reverse_document("ri", "i", "2026-03-15", "void invoice")
        payload = self.w.vintages.build("q", "2026-04-30")
        cohort = self.cohort(payload, "2026-02-28")
        final = cohort["months"][-1]
        self.assertEqual(final["cumulative"]["document_reversal_loss_minor"], 10000)
        self.assertEqual(final["cumulative"]["recovered_minor"], 0)
        self.assertEqual(final["cumulative"]["remaining_exposure_minor"], 0)
        self.assertEqual(final["state_counts"]["reversed"], 1)

    def test_dispute_amount_tracks_separately_and_caps_at_open_balance(self):
        self.invoice("i", "100")
        self.source("p", "payment", "60", issued="2026-03-02")
        self.w.ledger.allocate("al", "p", "i", "60", "2026-03-02")
        self.w.disputes.open("d", "i", "90", "2026-03-20", "quality")
        payload = self.w.vintages.build("q", "2026-03-31")
        cohort = self.cohort(payload, "2026-02-28")
        final = cohort["months"][-1]
        self.assertEqual(final["cumulative"]["disputed_minor"], 4000)
        self.assertEqual(final["state_counts"]["disputed"], 1)
        self.w.disputes.close("d", "2026-04-15", 1, "resolved")
        payload = self.w.vintages.build("q", "2026-04-30")
        cohort = self.cohort(payload, "2026-02-28")
        self.assertEqual(cohort["months"][-1]["cumulative"]["disputed_minor"], 0)
        self.assertEqual(cohort["months"][-1]["state_counts"]["open"], 1)

    # ------------------------------------------------------- risks/censoring
    def test_risk_set_and_competing_events(self):
        self.invoice("i1", "100")
        self.invoice("i2", "100", due="2026-01-20")
        self.source("p", "payment", "100", issued="2026-03-02")
        self.w.ledger.allocate("al1", "p", "i1", "100", "2026-03-02")
        self.source("w", "writeoff", "100", issued="2026-03-05")
        self.w.ledger.allocate("al2", "w", "i2", "100", "2026-03-05")
        payload = self.w.vintages.build("q", "2026-03-31")
        feb = self.cohort(payload, "2026-02-28")
        march = feb["months"][-1]
        self.assertEqual(march["risk_set_invoices"], 0)
        self.assertEqual(march["competing_events_minor"]["payment"], 10000)
        self.assertEqual(march["competing_events_minor"]["writeoff"], 0)

    def test_right_censoring_at_final_observation(self):
        self.invoice("i1", "100")
        self.invoice("i2", "100", due="2026-01-20")
        self.source("p", "payment", "100", issued="2026-03-02")
        self.w.ledger.allocate("al", "p", "i1", "100", "2026-03-02")
        payload = self.w.vintages.build("q", "2026-04-30")
        for cohort in payload["cohorts"]:
            final = cohort["months"][-1]
            # Only the still-exposed member is censored; the settled one is not.
            self.assertEqual(final["right_censored_invoices"],
                             1 if cohort["entry_month_end"] == "2026-01-31" else 0)

    # --------------------------------------------------------- bitemporal
    def test_late_known_cash_cannot_cross_earlier_knowledge_cutoff(self):
        self.invoice("i", "100")
        self.source("p", "payment", "40", issued="2026-03-10",
                    recorded="2026-05-20")
        self.w.ledger.allocate("al", "p", "i", "40", "2026-03-10",
                               recorded="2026-05-20")
        early = self.w.vintages.build("q", "2026-04-30", "2026-04-30")
        cohort = self.cohort(early, "2026-02-28")
        self.assertEqual(cohort["months"][-1]["cumulative"]["recovered_minor"], 0)
        self.assertEqual(
            cohort["months"][-1]["cumulative"]["remaining_exposure_minor"], 10000
        )
        later = self.w.vintages.build("q", "2026-05-31", "2026-05-31")
        cohort = self.cohort(later, "2026-02-28")
        # Same business month of March now reflects the late-known cash.
        march = next(m for m in cohort["months"] if m["month_end"] == "2026-03-31")
        self.assertEqual(march["cumulative"]["recovered_minor"], 4000)

    def test_late_reversal_does_not_change_earlier_viewpoint(self):
        self.invoice("i", "100")
        self.source("w", "writeoff", "60", issued="2026-03-02")
        self.w.ledger.allocate("al", "w", "i", "60", "2026-03-02")
        # Reversal business-dated in March but only known in May.
        self.w.ledger.reverse_allocation("ral", "al", "2026-03-20",
                                         "correction", recorded="2026-05-15")
        april = self.w.vintages.build("q", "2026-04-30", "2026-04-30")
        cohort = self.cohort(april, "2026-02-28")
        self.assertEqual(
            cohort["months"][-1]["cumulative"]["writeoff_loss_minor"], 6000
        )
        may = self.w.vintages.build("q", "2026-05-31", "2026-05-31")
        cohort = self.cohort(may, "2026-02-28")
        self.assertEqual(cohort["months"][-1]["cumulative"]["writeoff_loss_minor"], 0)

    def test_knowledge_date_validation(self):
        with self.assertRaises(BookError):
            self.w.ledger.post(dict(id="p", kind="payment", party="a", currency="CNY",
                                    amount="10", issued="2026-03-01",
                                    recorded="2026-02-01", reference="R"))
        self.invoice("i", "100")
        self.source("p", "payment", "10")
        with self.assertRaises(BookError):
            self.w.ledger.allocate("al", "p", "i", "10", "2026-03-01",
                                   recorded="2026-02-01")

    # ------------------------------------------------------------ versions
    def test_freeze_versions_compare_late_knowledge_vs_correction(self):
        self.invoice("i", "100")
        v1 = self.w.vintages.freeze("q", "2026-04-30", "2026-04-30", note="v1")
        self.assertEqual(v1["version"], 1)
        # Late-known cash, business date inside the already-frozen window.
        self.source("p", "payment", "30", issued="2026-03-15",
                    recorded="2026-05-18")
        self.w.ledger.allocate("al", "p", "i", "30", "2026-03-15",
                               recorded="2026-05-18")
        v2 = self.w.vintages.freeze("q", "2026-05-31", "2026-05-31", note="v2")
        self.assertEqual(v2["version"], 2)
        comparison = self.w.vintages.compare("q", 1, 2)
        self.assertFalse(comparison["unchanged"])
        late_kinds = {event["kind"] for event in comparison["late_knowledge_events"]}
        self.assertIn("allocation:payment", late_kinds)
        self.assertEqual(comparison["history_corrections"], [])
        # A reversal is classified as a history correction.
        self.w.ledger.reverse_allocation("ral", "al", "2026-06-05", "undo")
        self.w.vintages.freeze("q", "2026-06-30", "2026-06-30")
        comparison = self.w.vintages.compare("q", 2, 3)
        self.assertEqual(
            comparison["history_corrections"][0]["kind"], "reversal:allocation"
        )
        self.assertEqual(self.w.vintages.get("q", 1)["digest"], v1["digest"])

    def test_freeze_is_idempotent_per_arguments_and_immutable(self):
        self.invoice("i", "100")
        first = self.w.vintages.freeze("q", "2026-03-31")
        second = self.w.vintages.freeze("q", "2026-03-31")
        self.assertEqual(second["version"], first["version"] + 1)
        self.assertNotEqual(second["digest"], first["digest"])
        self.assertEqual([row["version"] for row in self.w.vintages.list()], [1, 2])

    def test_trace_invoice_follows_writeoff_and_reversal_records(self):
        self.invoice("i", "100")
        self.source("w", "writeoff", "60", issued="2026-03-02")
        self.w.ledger.allocate("al", "w", "i", "60", "2026-03-02")
        self.w.ledger.reverse_allocation("ral", "al", "2026-04-05", "reinstated")
        self.w.vintages.freeze("q", "2026-04-30")
        trace = self.w.vintages.trace_invoice("q", "i")
        self.assertEqual(trace["allocations"][0]["source_kind"], "writeoff")
        self.assertEqual(
            trace["allocations"][0]["reversal_detail"]["reason"], "reinstated"
        )
        with self.assertRaises(BookError):
            self.w.vintages.trace_invoice("q", "missing")

    # --------------------------------------------------------- multi-currency
    def test_currencies_are_separate_conservation_paths(self):
        self.invoice("cny", "100", currency="CNY")
        self.invoice("usd", "80", currency="USD")
        payload = self.w.vintages.build("q", "2026-03-31")
        codes = sorted(c["currency"] for c in payload["cohorts"])
        self.assertEqual(codes, ["CNY", "USD"])
        for cohort in payload["cohorts"]:
            final = cohort["months"][-1]
            total = sum(final["cumulative"][key + "_minor"] for key in (
                "recovered", "credit_applied", "writeoff_loss",
                "document_reversal_loss", "remaining_exposure"))
            self.assertEqual(total, cohort["original_amount_minor"])

    # ------------------------------------------------------------- migration
    def test_v1_database_migrates_and_preserves_balances(self):
        # Build a database through the current schema, then emulate a legacy v1
        # database by dropping the bitemporal columns and the version marker.
        self.invoice("i", "100")
        self.source("p", "payment", "40")
        self.w.ledger.allocate("al", "p", "i", "40", "2026-03-02")
        self.w.disputes.open("d", "i", "10", "2026-03-10", "quality")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy.sqlite3"
            self.w.db.backup(path)
            import sqlite3

            con = sqlite3.connect(path)
            for statement in (
                "ALTER TABLE documents DROP COLUMN recorded",
                "ALTER TABLE allocations DROP COLUMN recorded",
                "ALTER TABLE reversals DROP COLUMN recorded",
                "ALTER TABLE document_reversals DROP COLUMN recorded",
                "ALTER TABLE disputes DROP COLUMN opened_recorded",
                "ALTER TABLE disputes DROP COLUMN closed_recorded",
                "UPDATE meta SET value='1' WHERE key='version'",
            ):
                con.execute(statement)
            con.commit()
            con.close()
            with Workbench(path) as reopened:
                self.assertEqual(
                    reopened.db.one("SELECT value FROM meta WHERE key='version'")["value"],
                    "2",
                )
                self.assertEqual(reopened.ledger.balance("i"), 6000)
                report = reopened.controls.require_valid()
                self.assertTrue(report["ok"])
                payload = reopened.vintages.build("q", "2026-03-31")
                cohort = payload["cohorts"][0]
                self.assertEqual(
                    cohort["months"][-1]["cumulative"]["recovered_minor"], 4000
                )

    def test_export_roundtrip_and_tamper_detection(self):
        self.invoice("i", "100")
        self.w.vintages.freeze("q", "2026-03-31")
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "vintage.json"
            self.w.vintages.export("q", target)
            self.assertTrue(self.w.vintages.verify(target)["verified"])
            target.write_text(target.read_text().replace('"CNY"', '"USD"', 1))
            with self.assertRaises(BookError):
                self.w.vintages.verify(target)

    def test_weighted_flow_uses_entry_exposure(self):
        # An invoice partly collected before entry must weight from its entry
        # exposure while conservation still reconciles to the original amount.
        self.invoice("i", "100", due="2026-01-31")
        self.source("p", "payment", "30", issued="2026-02-10")
        self.w.ledger.allocate("al", "p", "i", "30", "2026-02-10")
        payload = self.w.vintages.build("q", "2026-03-31")
        cohort = self.cohort(payload, "2026-02-28")
        self.assertEqual(cohort["start_exposure_minor"], 7000)
        self.assertEqual(cohort["pre_entry_reduction_minor"], 3000)
        self.assertEqual(cohort["original_amount_minor"], 10000)
        self.assertEqual(
            cohort["member_rows"][0]["entry_exposure_minor"], 7000
        )

    def test_dispatch_exposes_vintage_actions(self):
        self.invoice("i", "100")
        result = self.w.dispatch({
            "action": "vintage.freeze",
            "arguments": {
                "vintage_id": "q",
                "business_cutoff": "2026-03-31",
            },
        })
        self.assertEqual(result["version"], 1)
        got = self.w.dispatch({
            "action": "vintage.get",
            "arguments": {"vintage_id": "q"},
        })
        self.assertEqual(got["id"], "q")
        with self.assertRaises(BookError):
            self.w.dispatch({"action": "vintage.unknown"})


if __name__ == "__main__":
    unittest.main()
