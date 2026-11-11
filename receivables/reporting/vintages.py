"""Frozen receivable cohorts (vintage roll-rate engine).

A cohort freezes the invoices that first entered the overdue collection queue at
a given month-end observation point. New invoices can never join an old cohort.
Every member is followed month by month through:

* due entry,
* cash collection (payment allocations),
* credit-note application,
* partial and full write-off,
* write-off / allocation reversals (history corrections),
* disputes,
* whole-document reversals (void / 冲销),
* settlement and right-censoring.

All events are evaluated on two time axes:

* business cutoff: the accounting date of the fact,
* knowledge cutoff: the date the fact became known.

A fact recorded after the knowledge cutoff cannot travel back to an earlier
observation, so late postings never cross an earlier frozen viewpoint.

Amounts are weighted by each member's exposure at the moment it entered the
cohort. Currencies are tracked independently and every flow path is checked to
conserve the original exposure: recovery, credit, write-off loss, document
reversal loss, disputed amount and remaining open exposure sum back to the
starting exposure at every observation.
"""

from datetime import date
from ..primitives import (
    identifier,
    day,
    integer,
    display,
    canonical,
    digest,
    decode,
    error,
    CURRENCIES,
)

TERMINAL_REVERSED = "reversed"
TERMINAL_SETTLED = "settled"
STATE_OPEN = "open"
STATE_DISPUTED = "disputed"
STATE_CREDIT_SETTLED = "credit_settled"
STATE_WRITEOFF_SETTLED = "writeoff_settled"
STATE_REVERSED = TERMINAL_REVERSED

# Flow buckets. Cash collection is deliberately separate from non-cash flows so
# a write-off can never be presented as a recovery.
FLOWS = (
    "payment",
    "credit",
    "writeoff",
    "writeoff_reinstated",
    "document_reversal",
    "other_reinstated",
)
COUNT_STATES = (
    STATE_OPEN,
    STATE_DISPUTED,
    STATE_CREDIT_SETTLED,
    STATE_WRITEOFF_SETTLED,
    TERMINAL_SETTLED,
    STATE_REVERSED,
)


def month_ends(start, end):
    """All calendar month-end dates from the month of start through end."""
    start, end = date.fromisoformat(start), date.fromisoformat(end)
    result = []
    year, month = start.year, start.month
    while True:
        if month == 12:
            next_first = date(year + 1, 1, 1)
        else:
            next_first = date(year, month + 1, 1)
        last = next_first.fromordinal(next_first.toordinal() - 1)
        if last >= start:
            result.append(last.isoformat())
        if last >= end:
            break
        year, month = next_first.year, next_first.month
    return result


def money_fields(values, code):
    return {key + "_minor": values[key] for key in values} | {
        key: display(values[key], code) for key in values
    }


class Vintages:
    def __init__(self, database, ledger):
        self.db = database
        self.ledger = ledger

    # ------------------------------------------------------------------ data
    def _invoices(self, business_cutoff, knowledge_cutoff):
        rows = self.db.rows(
            "SELECT * FROM documents WHERE kind='invoice' AND issued<=? AND recorded<=? ORDER BY id",
            (business_cutoff, knowledge_cutoff),
        )
        result = []
        for row in rows:
            reversal = self.db.one(
                "SELECT * FROM document_reversals WHERE document=?", (row["id"],)
            )
            if (
                reversal is not None
                and reversal["effective"] <= business_cutoff
                and reversal["recorded"] <= knowledge_cutoff
            ):
                # The invoice itself was voided; it still belongs to its cohort
                # and is tracked as a document-reversal loss.
                row["reversal"] = reversal
            else:
                row["reversal"] = None
            result.append(row)
        return result

    def _allocations(self, knowledge_cutoff):
        allocations = self.db.rows(
            "SELECT a.*,d.kind AS source_kind FROM allocations a "
            "JOIN documents d ON d.id=a.source WHERE a.recorded<=? ORDER BY a.effective,a.id",
            (knowledge_cutoff,),
        )
        reversals = {
            row["target"]: row
            for row in self.db.rows(
                "SELECT * FROM reversals WHERE kind='allocation' AND recorded<=?",
                (knowledge_cutoff,),
            )
        }
        for allocation in allocations:
            reversal = reversals.get(allocation["id"])
            if reversal is not None:
                allocation["reversal_recorded"] = reversal["recorded"]
                allocation["reversal_reason"] = reversal["reason"]
                allocation["reversal_id"] = reversal["id"]
            else:
                allocation["reversal_recorded"] = None
        return allocations

    def _disputes(self, knowledge_cutoff):
        return self.db.rows(
            "SELECT * FROM disputes WHERE opened_recorded<=?",
            (knowledge_cutoff,),
        )

    def _visible_allocation(self, allocation, cutoff, knowledge_cutoff):
        if allocation["effective"] > cutoff:
            return False
        if allocation["reversed"] is None:
            return True
        reversal_recorded = allocation["reversal_recorded"]
        if reversal_recorded is None or reversal_recorded > knowledge_cutoff:
            # Reversal not yet known at this viewpoint: allocation stays live.
            return True
        return allocation["reversed"] > cutoff

    def _member_snapshot(self, invoice, allocations, disputes, cutoff, knowledge_cutoff):
        """Point-in-time position of one invoice at a month-end observation."""
        visible = [
            row
            for row in allocations
            if row["invoice"] == invoice["id"]
            and self._visible_allocation(row, cutoff, knowledge_cutoff)
        ]
        by_kind = {"payment": 0, "credit": 0, "writeoff": 0}
        for row in visible:
            by_kind[row["source_kind"]] = by_kind.get(row["source_kind"], 0) + row["amount"]
        paid = by_kind["payment"]
        credited = by_kind["credit"]
        written = by_kind["writeoff"]
        reversed_doc = (
            invoice["reversal"] is not None
            and invoice["reversal"]["effective"] <= cutoff
        )
        if reversed_doc:
            open_balance = 0
        else:
            open_balance = invoice["amount"] - paid - credited - written
        disputed = 0
        for row in disputes:
            if row["invoice"] != invoice["id"]:
                continue
            if row["opened"] > cutoff or row["opened_recorded"] > knowledge_cutoff:
                continue
            if row["closed"] and row["closed"] <= cutoff and (
                row["closed_recorded"] is None
                or row["closed_recorded"] <= knowledge_cutoff
            ):
                continue
            disputed += row["amount"]
        disputed = max(0, min(disputed, open_balance))
        if reversed_doc:
            state = STATE_REVERSED
        elif open_balance == 0 and written > 0 and (paid + credited) == 0:
            state = STATE_WRITEOFF_SETTLED
        elif open_balance == 0 and credited > 0 and paid == 0:
            state = STATE_CREDIT_SETTLED
        elif open_balance == 0:
            state = TERMINAL_SETTLED
        elif disputed > 0:
            state = STATE_DISPUTED
        else:
            state = STATE_OPEN
        return {
            "open": open_balance,
            "paid": paid,
            "credited": credited,
            "written": written,
            "disputed": disputed,
            "reversed_doc": reversed_doc,
            "state": state,
        }

    # ------------------------------------------------------------- building
    def build(
        self,
        vintage_id,
        business_cutoff,
        knowledge_cutoff=None,
        party=None,
        currency=None,
    ):
        identifier(vintage_id)
        business_cutoff = day(business_cutoff)
        knowledge_cutoff = day(knowledge_cutoff or business_cutoff)
        # The two axes are independent: a business month can be closed while
        # only facts known by an earlier knowledge date are admitted.
        if party is not None:
            self.ledger.party(party)
        if currency is not None and currency not in CURRENCIES:
            error("currency", "unsupported currency", currency=currency)

        invoices = self._invoices(business_cutoff, knowledge_cutoff)
        allocations = self._allocations(knowledge_cutoff)
        disputes = self._disputes(knowledge_cutoff)
        grid = month_ends(
            min(row["due"] for row in invoices) if invoices else business_cutoff,
            business_cutoff,
        )
        # Month-ends later than the business cutoff are not observable.
        grid = [point for point in grid if point <= business_cutoff]

        members = {}
        for invoice in invoices:
            if party is not None and invoice["party"] != party:
                continue
            if currency is not None and invoice["currency"] != currency:
                continue
            entry = None
            entry_snapshot = None
            for point in grid:
                if invoice["due"] >= point:
                    continue
                snapshot = self._member_snapshot(
                    invoice, allocations, disputes, point, knowledge_cutoff
                )
                # First-listing rule: the first overdue month-end on which the
                # invoice still had open exposure freezes the cohort membership.
                if snapshot["open"] > 0:
                    entry = point
                    entry_snapshot = snapshot
                    break
            if entry is None:
                continue
            members[invoice["id"]] = {
                "invoice": invoice,
                "entry": entry,
                "entry_snapshot": entry_snapshot,
            }

        cohorts = {}
        for member_id, member in sorted(members.items()):
            invoice = member["invoice"]
            key = (member["entry"], invoice["currency"])
            cohort = cohorts.setdefault(
                key,
                {
                    "entry_month_end": member["entry"],
                    "currency": invoice["currency"],
                    "members": [],
                },
            )
            cohort["members"].append(member_id)

        payload_cohorts = []
        for (entry, code), cohort in sorted(cohorts.items()):
            payload_cohorts.append(
                self._build_cohort(
                    entry,
                    code,
                    cohort["members"],
                    members,
                    invoices,
                    allocations,
                    disputes,
                    grid,
                    business_cutoff,
                    knowledge_cutoff,
                )
            )

        payload = {
            "id": vintage_id,
            "format_version": 1,
            "business_cutoff": business_cutoff,
            "knowledge_cutoff": knowledge_cutoff,
            "party": party,
            "currency": currency,
            "grid": grid,
            "cohorts": payload_cohorts,
        }
        return payload

    def _build_cohort(
        self,
        entry,
        code,
        member_ids,
        members,
        invoices,
        allocations,
        disputes,
        grid,
        business_cutoff,
        knowledge_cutoff,
    ):
        invoice_by_id = {row["id"]: row for row in invoices}
        points = [point for point in grid if point >= entry]
        # Starting exposure is the open balance at the entry month-end.
        start_exposure = sum(
            members[member_id]["entry_snapshot"]["open"] for member_id in member_ids
        )
        original_amount = sum(
            invoice_by_id[member_id]["amount"] for member_id in member_ids
        )
        pre_entry = sum(
            members[member_id]["entry_snapshot"]["paid"]
            + members[member_id]["entry_snapshot"]["credited"]
            + members[member_id]["entry_snapshot"]["written"]
            for member_id in member_ids
        )

        months = []
        prior_snapshots = None
        for index, point in enumerate(points):
            snapshots = {
                member_id: self._member_snapshot(
                    invoice_by_id[member_id],
                    allocations,
                    disputes,
                    point,
                    knowledge_cutoff,
                )
                for member_id in member_ids
            }
            totals = {
                "open": 0,
                "paid": 0,
                "credited": 0,
                "written": 0,
                "disputed": 0,
                "document_reversal": 0,
            }
            state_counts = {state: 0 for state in COUNT_STATES}
            for member_id in member_ids:
                snapshot = snapshots[member_id]
                invoice = invoice_by_id[member_id]
                for field in ("open", "paid", "credited", "written", "disputed"):
                    totals[field] += snapshot[field]
                if snapshot["reversed_doc"]:
                    totals["document_reversal"] += invoice["amount"]
                state_counts[snapshot["state"]] += 1

            # Conservation against the original invoice amounts.
            accounted = (
                totals["open"]
                + totals["paid"]
                + totals["credited"]
                + totals["written"]
                + totals["document_reversal"]
            )
            if accounted != original_amount:
                error(
                    "integrity",
                    "cohort exposure path is not conserved",
                    cohort=entry,
                    currency=code,
                    month_end=point,
                    original_minor=original_amount,
                    accounted_minor=accounted,
                )

            flow = {name: 0 for name in FLOWS}
            if prior_snapshots is not None:
                for member_id in member_ids:
                    before = prior_snapshots[member_id]
                    after = snapshots[member_id]
                    flow["payment"] += after["paid"] - before["paid"]
                    flow["credit"] += after["credited"] - before["credited"]
                    net_writeoff = after["written"] - before["written"]
                    if net_writeoff >= 0:
                        flow["writeoff"] += net_writeoff
                    else:
                        flow["writeoff_reinstated"] += -net_writeoff
                    if after["reversed_doc"] and not before["reversed_doc"]:
                        flow["document_reversal"] += invoice_by_id[member_id]["amount"]
                    elif before["reversed_doc"] and not after["reversed_doc"]:
                        flow["other_reinstated"] += invoice_by_id[member_id]["amount"]
                # Interval flow must explain the open-balance movement.
                movement = totals["open"] - sum(
                    prior_snapshots[m]["open"] for m in member_ids
                )
                explained = -(
                    flow["payment"]
                    + flow["credit"]
                    + flow["writeoff"]
                    - flow["writeoff_reinstated"]
                    + flow["document_reversal"]
                    - flow["other_reinstated"]
                )
                if movement != explained:
                    error(
                        "integrity",
                        "cohort interval flow does not reconcile",
                        month_end=point,
                        movement_minor=movement,
                        explained_minor=explained,
                    )

            # Cumulative recovery curve with competing risks. A member is in
            # the risk set while it has not yet reached a terminal state.
            at_risk = [
                member_id
                for member_id in member_ids
                if snapshots[member_id]["state"]
                not in (TERMINAL_REVERSED, TERMINAL_SETTLED,
                         STATE_CREDIT_SETTLED, STATE_WRITEOFF_SETTLED)
            ]
            events_in_interval = {name: 0 for name in FLOWS}
            if prior_snapshots is not None:
                for name in FLOWS:
                    events_in_interval[name] = flow[name]
            # Right censoring: members still exposed at the final business
            # observation have not had time to reach a terminal endpoint.
            censored = 0
            if point == business_cutoff:
                censored = len(at_risk)

            month = {
                "month_end": point,
                "age_months": index,
                "risk_set_invoices": len(at_risk),
                "state_counts": state_counts,
                "cumulative": money_fields(
                    {
                        "recovered": totals["paid"],
                        "credit_applied": totals["credited"],
                        "writeoff_loss": totals["written"],
                        "document_reversal_loss": totals["document_reversal"],
                        "disputed": totals["disputed"],
                        "remaining_exposure": totals["open"],
                    },
                    code,
                ),
                "interval_flow_minor": flow,
                "competing_events_minor": events_in_interval,
                "right_censored_invoices": censored,
            }
            months.append(month)
            prior_snapshots = snapshots

        # Entry-weighted roll matrix: count migration between primary states.
        transitions = self._transitions(
            member_ids,
            invoice_by_id,
            allocations,
            disputes,
            points,
            knowledge_cutoff,
        )
        member_rows = []
        for member_id in member_ids:
            invoice = invoice_by_id[member_id]
            entry_snapshot = members[member_id]["entry_snapshot"]
            final_snapshot = self._member_snapshot(
                invoice, allocations, disputes, business_cutoff, knowledge_cutoff
            )
            member_rows.append(
                {
                    "invoice": member_id,
                    "party": invoice["party"],
                    "issued": invoice["issued"],
                    "due": invoice["due"],
                    "entry_month_end": entry,
                    "entry_exposure_minor": entry_snapshot["open"],
                    "entry_exposure": display(entry_snapshot["open"], code),
                    "original_minor": invoice["amount"],
                    "final_state": final_snapshot["state"],
                    "final_open_minor": final_snapshot["open"],
                    "weight": display(entry_snapshot["open"], code),
                }
            )

        # Re-check conservation at the entry observation against each member's
        # full original invoice amount (pre-entry reductions included).
        first = months[0]["cumulative"]
        entry_reconciled = (
            first["remaining_exposure_minor"]
            + first["recovered_minor"]
            + first["credit_applied_minor"]
            + first["writeoff_loss_minor"]
            + first["document_reversal_loss_minor"]
        )
        if entry_reconciled != original_amount:
            error("integrity", "entry exposure weighting does not conserve")

        return {
            "entry_month_end": entry,
            "currency": code,
            "members": len(member_ids),
            "original_amount_minor": original_amount,
            "original_amount": display(original_amount, code),
            "pre_entry_reduction_minor": pre_entry,
            "pre_entry_reduction": display(pre_entry, code),
            "start_exposure_minor": start_exposure,
            "start_exposure": display(start_exposure, code),
            "months": months,
            "state_transitions": transitions,
            "member_rows": member_rows,
        }

    def _transitions(
        self,
        member_ids,
        invoice_by_id,
        allocations,
        disputes,
        points,
        knowledge_cutoff,
    ):
        """Count migration of member primary states between observations."""
        previous = {}
        rows = []
        for point in points:
            current = {}
            for member_id in member_ids:
                current[member_id] = self._member_snapshot(
                    invoice_by_id[member_id],
                    allocations,
                    disputes,
                    point,
                    knowledge_cutoff,
                )["state"]
            if previous:
                matrix = {}
                for member_id in member_ids:
                    pair = (previous[member_id], current[member_id])
                    matrix[pair] = matrix.get(pair, 0) + 1
                for (from_state, to_state), count in sorted(matrix.items()):
                    if from_state != to_state:
                        rows.append(
                            {
                                "month_end": point,
                                "from_state": from_state,
                                "to_state": to_state,
                                "invoices": count,
                            }
                        )
            previous = current
        return rows

    # ------------------------------------------------------------ freeze API
    def freeze(
        self,
        vintage_id,
        business_cutoff,
        knowledge_cutoff=None,
        party=None,
        currency=None,
        note=None,
    ):
        identifier(vintage_id)
        if note is not None and (not isinstance(note, str) or len(note) > 1000):
            error("input", "freeze note must be bounded text")
        payload = self.build(
            vintage_id, business_cutoff, knowledge_cutoff, party, currency
        )
        payload["note"] = note
        with self.db.transaction() as con:
            last = self.db.one(
                "SELECT MAX(version) AS v FROM vintages WHERE id=?", (vintage_id,)
            )
            version = (last["v"] or 0) + 1
            payload["version"] = version
            body = canonical(payload)
            checksum = digest(payload)
            con.execute(
                "INSERT INTO vintages VALUES(?,?,?,?,?,?)",
                (
                    vintage_id,
                    version,
                    body,
                    checksum,
                    payload["business_cutoff"],
                    payload["knowledge_cutoff"],
                ),
            )
            self.db.audit(
                "vintage_frozen",
                vintage_id,
                {
                    "version": version,
                    "business_cutoff": payload["business_cutoff"],
                    "knowledge_cutoff": payload["knowledge_cutoff"],
                    "digest": checksum,
                },
            )
        return self.get(vintage_id, version)

    def get(self, vintage_id, version=None):
        identifier(vintage_id)
        if version is None:
            row = self.db.one(
                "SELECT * FROM vintages WHERE id=? ORDER BY version DESC LIMIT 1",
                (vintage_id,),
            )
        else:
            integer(version, 1)
            row = self.db.one(
                "SELECT * FROM vintages WHERE id=? AND version=?",
                (vintage_id, version),
            )
        if row is None:
            error("missing", "frozen vintage not found")
        payload = decode(row["payload"])
        if digest(payload) != row["digest"]:
            error("integrity", "vintage digest mismatch")
        return {**payload, "digest": row["digest"]}

    def list(self):
        rows = self.db.rows(
            "SELECT id,version,business_cutoff,knowledge_cutoff,digest "
            "FROM vintages ORDER BY id,version"
        )
        return rows

    def compare(self, vintage_id, left_version, right_version):
        """Explain the difference between two frozen versions.

        Events with the same business date but only present in the newer version
        are late knowledge (behaviour change); reversal/correction events are
        history corrections.
        """
        identifier(vintage_id)
        left = self.get(vintage_id, left_version)
        right = self.get(vintage_id, right_version)
        # The analytical scope must match, but business and knowledge cutoffs
        # are expected to advance between frozen versions.
        if left["party"] != right["party"] or left["currency"] != right["currency"]:
            error("input", "versions describe different cohort boundaries")
        differences = []
        left_cohorts = {(c["entry_month_end"], c["currency"]): c for c in left["cohorts"]}
        right_cohorts = {(c["entry_month_end"], c["currency"]): c for c in right["cohorts"]}
        for key in sorted(left_cohorts.keys() | right_cohorts.keys()):
            old = left_cohorts.get(key)
            new = right_cohorts.get(key)
            if old is None or new is None:
                differences.append(
                    {
                        "entry_month_end": key[0],
                        "currency": key[1],
                        "kind": "cohort_membership_changed",
                    }
                )
                continue
            if old["members"] != new["members"]:
                differences.append(
                    {
                        "entry_month_end": key[0],
                        "currency": key[1],
                        "kind": "cohort_membership_changed",
                        "left_members": old["members"],
                        "right_members": new["members"],
                    }
                )
            for old_month, new_month in zip(old["months"], new["months"]):
                for field in (
                    "recovered",
                    "credit_applied",
                    "writeoff_loss",
                    "document_reversal_loss",
                    "disputed",
                    "remaining_exposure",
                ):
                    before = old_month["cumulative"][field + "_minor"]
                    after = new_month["cumulative"][field + "_minor"]
                    if before != after:
                        differences.append(
                            {
                                "entry_month_end": key[0],
                                "currency": key[1],
                                "month_end": old_month["month_end"],
                                "kind": field + "_changed",
                                "left_minor": before,
                                "right_minor": after,
                                "delta_minor": after - before,
                            }
                        )
        late_events, corrections = self._explain_events(left, right)
        return {
            "vintage": vintage_id,
            "left_version": left_version,
            "right_version": right_version,
            "left_knowledge_cutoff": left["knowledge_cutoff"],
            "right_knowledge_cutoff": right["knowledge_cutoff"],
            "business_cutoff": left["business_cutoff"],
            "differences": differences,
            "late_knowledge_events": late_events,
            "history_corrections": corrections,
            "unchanged": not differences and not late_events and not corrections,
        }

    def _explain_events(self, left, right):
        """Classify underlying events visible only to the newer freeze."""
        earlier_knowledge = left["knowledge_cutoff"]
        later_knowledge = right["knowledge_cutoff"]
        business_cutoff = right["business_cutoff"]
        late_events = []
        corrections = []
        for allocation in self.db.rows(
            "SELECT a.*,d.kind AS source_kind FROM allocations a "
            "JOIN documents d ON d.id=a.source "
            "WHERE a.recorded>? AND a.recorded<=? AND a.effective<=?",
            (earlier_knowledge, later_knowledge, business_cutoff),
        ):
            late_events.append(
                {
                    "kind": "allocation:" + allocation["source_kind"],
                    "id": allocation["id"],
                    "invoice": allocation["invoice"],
                    "source": allocation["source"],
                    "amount_minor": allocation["amount"],
                    "effective": allocation["effective"],
                    "recorded": allocation["recorded"],
                }
            )
        for document in self.db.rows(
            "SELECT * FROM documents WHERE recorded>? AND recorded<=? AND issued<=?",
            (earlier_knowledge, later_knowledge, business_cutoff),
        ):
            late_events.append(
                {
                    "kind": "document:" + document["kind"],
                    "id": document["id"],
                    "effective": document["issued"],
                    "recorded": document["recorded"],
                }
            )
        for reversal in self.db.rows(
            "SELECT * FROM reversals WHERE recorded>? AND recorded<=? AND effective<=?",
            (earlier_knowledge, later_knowledge, business_cutoff),
        ):
            corrections.append(
                {
                    "kind": "reversal:" + reversal["kind"],
                    "id": reversal["id"],
                    "target": reversal["target"],
                    "effective": reversal["effective"],
                    "recorded": reversal["recorded"],
                    "reason": reversal["reason"],
                }
            )
        for dispute in self.db.rows(
            "SELECT * FROM disputes WHERE (opened_recorded>? AND opened_recorded<=?) "
            "OR (closed_recorded>? AND closed_recorded<=?)",
            (earlier_knowledge, later_knowledge, earlier_knowledge, later_knowledge),
        ):
            late_events.append(
                {
                    "kind": "dispute",
                    "id": dispute["id"],
                    "invoice": dispute["invoice"],
                    "opened": dispute["opened"],
                    "recorded": dispute["opened_recorded"],
                }
            )
        return late_events, corrections

    def trace_invoice(self, vintage_id, invoice, version=None):
        """Trace one frozen cohort member back to every underlying record."""
        identifier(vintage_id)
        snapshot = self.get(vintage_id, version)
        member = None
        cohort = None
        for group in snapshot["cohorts"]:
            for row in group["member_rows"]:
                if row["invoice"] == invoice:
                    member, cohort = row, group
                    break
        if member is None:
            error("missing", "invoice is not a member of this vintage")
        document = self.ledger.document(invoice)
        allocations = self.db.rows(
            "SELECT a.*,d.kind AS source_kind FROM allocations a "
            "JOIN documents d ON d.id=a.source WHERE a.invoice=? ORDER BY a.effective,a.id",
            (invoice,),
        )
        for allocation in allocations:
            reversal = self.db.one(
                "SELECT * FROM reversals WHERE target=? AND kind='allocation'",
                (allocation["id"],),
            )
            allocation["reversal_detail"] = reversal
        document_reversal = self.db.one(
            "SELECT * FROM document_reversals WHERE document=?", (invoice,)
        )
        if document_reversal:
            document_reversal["reversal"] = self.db.one(
                "SELECT * FROM reversals WHERE id=?",
                (document_reversal["reversal"],),
            )
        return {
            "vintage": vintage_id,
            "version": snapshot["version"],
            "entry_month_end": cohort["entry_month_end"],
            "currency": cohort["currency"],
            "member": member,
            "document": document,
            "allocations": allocations,
            "document_reversal": document_reversal,
            "disputes": self.db.rows(
                "SELECT * FROM disputes WHERE invoice=? ORDER BY opened,id", (invoice,)
            ),
        }

    def export(self, vintage_id, path, version=None):
        import os
        from pathlib import Path

        payload = self.get(vintage_id, version)
        target = Path(path)
        if target.exists() or target.is_symlink():
            error("conflict", "export destination exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".pending")
        content = canonical(payload).encode("utf-8")
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, target)
        except FileExistsError:
            error("conflict", "export or staging destination exists")
        finally:
            if temporary.exists() and temporary.read_bytes() == content:
                temporary.unlink()
        return {
            "path": str(target),
            "digest": payload["digest"],
            "bytes": len(content),
        }

    @staticmethod
    def verify(path):
        from pathlib import Path

        path = Path(path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 20_000_000:
            error("path", "invalid vintage file")
        payload = decode(path.read_text("utf-8"))
        if not isinstance(payload, dict) or "digest" not in payload:
            error("integrity", "vintage envelope missing digest")
        expected = payload.pop("digest")
        if digest(payload) != expected:
            error("integrity", "exported vintage differs")
        if payload.get("format_version") != 1:
            error("integrity", "unsupported vintage version")
        return {"id": payload["id"], "version": payload["version"], "verified": True}
