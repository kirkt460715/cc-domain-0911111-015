import unittest
import json
from receivables.service import Workbench
from receivables.primitives import BookError


class ReviewCase(unittest.TestCase):
    def setUp(self):
        self.w = Workbench()
        self.addCleanup(self.w.close)
        self.w.ledger.register_party('a', 'Alpha', ['A Ltd'])

    def row(self, ident='i', amount='100', kind='invoice', **extra):
        row = dict(id=ident, kind=kind, amount=amount, currency='CNY', party='a',
                   issued='2026-01-01', reference='R')
        if kind == 'invoice':
            row['due'] = '2026-01-31'
        row.update(extra)
        return row

    def test_batch_replay_preserves_audit(self):
        rows = [self.row(), self.row('p', '50', 'payment')]
        first = self.w.documents.import_batch('b', rows)
        self.assertEqual(first['posted'], 2)
        before = self.w.db.verify_audit()
        again = self.w.documents.import_batch('b', rows)
        self.assertTrue(again['reused'])
        self.assertEqual(before, self.w.db.verify_audit())
        with self.assertRaises(BookError):
            self.w.documents.import_batch('b', [self.row()])
        self.assertEqual(self.w.ledger.balance('i'), 10000)

    def test_batch_bad_second_row_rolls_back_first(self):
        before = self.w.db.verify_audit()
        rows = [self.row(), self.row('j', 'not-money')]
        with self.assertRaises(BookError):
            self.w.documents.import_batch('b', rows)
        self.assertEqual(self.w.db.rows('SELECT * FROM documents'), [])
        self.assertEqual(self.w.db.rows('SELECT * FROM batches'), [])
        self.assertEqual(before, self.w.db.verify_audit())
        self.assertEqual(self.w.documents.import_batch('b', [self.row()])['posted'], 1)

    def test_duplicate_batch_identity_is_rejected(self):
        rows = [self.row(), self.row()]
        with self.assertRaises(BookError):
            self.w.documents.import_batch('b', rows)
        self.assertEqual(self.w.db.rows('SELECT * FROM documents'), [])
        self.assertEqual(self.w.documents.import_batch('b', [self.row()])['posted'], 1)
        result = self.w.documents.import_batch('c', [self.row()])
        self.assertEqual(result['existing'], 1)
        self.assertEqual(result['posted'], 0)

    def test_jsonl_and_csv_input_protocols(self):
        content = json.dumps(self.row()) + '\n\n' + json.dumps(self.row('j'))
        parsed = self.w.documents.parse(content)
        self.assertEqual([r['id'] for r in parsed], ['i', 'j'])
        csv = 'id,kind,party,currency,amount,issued,due,reference\ni,invoice,a,CNY,12.50,2026-01-01,2026-01-31,R\n'
        parsed = self.w.documents.parse(csv, 'csv')
        self.assertEqual(parsed[0]['amount'], '12.50')
        self.assertEqual(self.w.documents.import_batch('csv', parsed)['posted'], 1)
        self.assertEqual(self.w.ledger.balance('i'), 1250)

    def test_protocol_failures_have_no_side_effects(self):
        for content, fmt in [('id,id\nx,y', 'csv'), ('id,x\na', 'csv'),
                             ('{bad}', 'jsonl'), ('', 'jsonl'), ('x', 'xml')]:
            with self.subTest(fmt=fmt, content=content):
                with self.assertRaises(BookError):
                    self.w.documents.parse(content, fmt)
        self.assertEqual(self.w.db.rows('SELECT * FROM documents'), [])
        self.assertEqual(self.w.db.verify_audit()['entries'], 1)

    def test_inbox_dry_run_does_not_post(self):
        self.w.inbox.stage('pkg', 'erp', [self.row()])
        before = self.w.db.verify_audit()
        preview = self.w.inbox.inspect('pkg')
        self.assertTrue(preview['valid'])
        self.assertEqual(preview['failures'], [])
        self.assertEqual(self.w.db.rows('SELECT * FROM documents'), [])
        self.assertEqual(before, self.w.db.verify_audit())
        self.assertEqual(self.w.inbox.get('pkg')['revision'], 1)
        self.assertEqual(self.w.inbox.pending()['rows'][0]['id'], 'pkg')

    def test_review_approve_commit_and_retry(self):
        self.w.inbox.stage('pkg', 'erp', [self.row()])
        review = self.w.inbox.inspect('pkg')
        approved = self.w.inbox.approve('pkg', 1, review['review_digest'], 'reviewer')
        self.assertEqual(approved['revision'], 2)
        self.assertEqual(approved['state'], 'approved')
        committed = self.w.inbox.commit('pkg', 2)
        self.assertEqual(committed['state'], 'committed')
        self.assertEqual(committed['result']['posted'], 1)
        self.assertEqual(self.w.inbox.commit('pkg', 2), committed)
        self.assertEqual(self.w.inbox.pending()['rows'], [])
        self.assertEqual(self.w.ledger.balance('i'), 10000)

    def test_inbox_stale_review_requires_new_evidence(self):
        self.w.inbox.stage('pkg', 'erp', [self.row()])
        review = self.w.inbox.inspect('pkg')
        self.w.ledger.post(self.row())
        with self.assertRaises(BookError) as caught:
            self.w.inbox.approve('pkg', 1, review['review_digest'], 'reviewer')
        self.assertEqual(caught.exception.code, 'conflict')
        refreshed = self.w.inbox.inspect('pkg')
        self.assertNotEqual(refreshed['review_digest'], review['review_digest'])
        self.w.inbox.approve('pkg', 1, refreshed['review_digest'], 'reviewer')
        self.assertEqual(self.w.inbox.commit('pkg', 2)['result']['existing'], 1)

    def test_invalid_inbox_reports_all_rows(self):
        self.w.inbox.stage('bad', 'erp', [self.row('i', 'bad'), self.row('j', '-1')])
        report = self.w.inbox.inspect('bad')
        self.assertFalse(report['valid'])
        self.assertEqual([x['row'] for x in report['failures']], [0, 1])
        with self.assertRaises(BookError):
            self.w.inbox.approve('bad', 1, report['review_digest'], 'reviewer')
        self.assertEqual(self.w.inbox.get('bad')['state'], 'staged')
        self.assertEqual(self.w.db.rows('SELECT * FROM documents'), [])

    def test_inbox_rejection_and_pagination(self):
        for ident in ['a', 'b', 'c']:
            self.w.inbox.stage(ident, 'erp', [self.row(ident)])
        page = self.w.inbox.pending(limit=2)
        self.assertEqual([r['id'] for r in page['rows']], ['a', 'b'])
        self.assertEqual(page['next'], 'b')
        self.assertEqual(self.w.inbox.pending(after='b')['rows'][0]['id'], 'c')
        self.w.inbox.reject('a', 1, 'wrong source', 'checker')
        with self.assertRaises(BookError):
            self.w.inbox.commit('a', 2)
        self.assertEqual(len(self.w.inbox.pending()['rows']), 2)
        self.assertEqual(self.w.inbox.pending(source='other')['rows'], [])

    def test_commit_conflict_is_atomic_and_recoverable(self):
        self.w.inbox.stage('pkg', 'erp', [self.row('i'), self.row('j')])
        report = self.w.inbox.inspect('pkg')
        self.w.inbox.approve('pkg', 1, report['review_digest'], 'reviewer')
        self.w.ledger.post(self.row('j', '99'))
        with self.assertRaises(BookError):
            self.w.inbox.commit('pkg', 2)
        with self.assertRaises(BookError):
            self.w.ledger.document('i')
        self.assertEqual(self.w.inbox.get('pkg')['state'], 'approved')
        self.assertEqual(self.w.ledger.balance('j'), 9900)
        self.assertTrue(self.w.controls.require_valid()['ok'])

    def test_profile_version_and_localized_numbers(self):
        mapping = {key: key for key in self.row()}
        profile = self.w.profiles.save('erp', 'documents', mapping, '%d/%m/%Y', ',')
        self.assertEqual(profile['version'], 1)
        csv = 'id;kind;amount;currency;party;issued;reference;due\ni;invoice;12,50;CNY;a;01/01/2026;R;31/01/2026\n'
        report = self.w.profiles.preview('erp', csv, ';')
        self.assertEqual(report['rows'][0]['amount'], '12.50')
        self.assertEqual(report['rows'][0]['issued'], '2026-01-01')
        with self.assertRaises(BookError):
            self.w.profiles.save('erp', 'documents', mapping)
        changed = self.w.profiles.save('erp', 'documents', mapping, expected_version=1)
        self.assertEqual(changed['version'], 2)

    def test_profile_rejects_bad_mapping_and_headers(self):
        with self.assertRaises(BookError):
            self.w.profiles.save('p', 'documents', {'id': 'id'})
        mapping = {key: key for key in self.row()}
        self.w.profiles.save('p', 'documents', mapping)
        with self.assertRaises(BookError):
            self.w.profiles.preview('p', 'id,id\na,b')
        with self.assertRaises(BookError):
            self.w.profiles.preview('p', 'id,kind\na,invoice')
        with self.assertRaises(BookError):
            self.w.profiles.preview('p', '', '|')

    def test_exact_match_refuses_ambiguous_invoices(self):
        for row in [self.row('i'), self.row('j'), self.row('p', '100', 'payment')]:
            self.w.ledger.post(row)
        report = self.w.matching.candidates('p', '2026-02-01')
        self.assertTrue(report['ambiguous'])
        self.assertEqual([r['invoice'] for r in report['candidates']], ['i', 'j'])
        plan = self.w.matching.propose('plan', 'p', '2026-02-01')
        self.assertTrue(plan['ambiguous'])
        self.assertEqual(plan['allocations'], [])
        with self.assertRaises(BookError):
            self.w.matching.confirm('plan', 'operator')
        self.assertEqual(self.w.ledger.balance('p'), 10000)

    def test_oldest_due_plan_partial_then_idempotent_confirm(self):
        self.w.ledger.post(self.row('i', '40', due='2026-01-10'))
        self.w.ledger.post(self.row('j', '80', due='2026-01-20'))
        self.w.ledger.post(self.row('p', '100', 'payment'))
        plan = self.w.matching.propose('plan', 'p', '2026-02-01', 'oldest_due')
        self.assertEqual([r['amount_minor'] for r in plan['allocations']], [4000, 6000])
        result = self.w.matching.confirm('plan', 'operator')
        self.assertEqual(result['state'], 'confirmed')
        self.assertEqual(self.w.ledger.balance('j'), 2000)
        self.assertEqual(self.w.matching.confirm('plan', 'operator'), result)
        self.assertEqual(len(self.w.db.rows('SELECT * FROM allocations')), 2)

    def test_stale_matching_plan_does_not_partially_apply(self):
        for row in [self.row('i', '60'), self.row('j', '60'), self.row('p', '100', 'payment'), self.row('q', '10', 'payment')]:
            self.w.ledger.post(row)
        self.w.matching.propose('plan', 'p', '2026-02-01', 'oldest_due')
        self.w.ledger.allocate('other', 'q', 'j', '10', '2026-02-01')
        with self.assertRaises(BookError):
            self.w.matching.confirm('plan', 'operator')
        self.assertEqual(self.w.ledger.balance('i'), 6000)
        self.assertEqual(self.w.ledger.balance('p'), 10000)
        self.assertEqual(self.w.matching.get('plan')['state'], 'proposed')
        self.assertTrue(self.w.controls.require_valid()['ok'])

    def test_cancellation_prevents_confirmation(self):
        self.w.ledger.post(self.row())
        self.w.ledger.post(self.row('p', '100', 'payment'))
        self.w.matching.propose('plan', 'p', '2026-02-01')
        self.assertEqual(self.w.matching.cancel('plan', 'duplicate')['state'], 'cancelled')
        with self.assertRaises(BookError):
            self.w.matching.confirm('plan', 'operator')
        with self.assertRaises(BookError):
            self.w.matching.cancel('plan', 'again')
        self.assertEqual(self.w.ledger.balance('i'), 10000)

    def test_subset_selection_conserves_exact_amount(self):
        for ident, amount in [('i', '30'), ('j', '70'), ('k', '50')]:
            self.w.ledger.post(self.row(ident, amount))
        self.w.ledger.post(self.row('p', '100', 'payment'))
        report = self.w.subsets.search('p', '2026-02-01')
        self.assertEqual(len(report['solutions']), 1)
        choice = report['solutions'][0]
        self.assertEqual([r['invoice'] for r in choice['invoices']], ['i', 'j'])
        self.w.subsets.propose('subset', 'p', '2026-02-01', choice['fingerprint'])
        self.w.matching.confirm('subset', 'reviewer')
        self.assertEqual(self.w.ledger.balance('p'), 0)
        self.assertEqual(self.w.ledger.balance('k'), 5000)
        self.assertTrue(self.w.controls.require_valid()['ok'])

    def test_subset_ambiguity_capacity_and_stale_choice(self):
        for ident in ['i', 'j', 'k']:
            self.w.ledger.post(self.row(ident, '50'))
        self.w.ledger.post(self.row('p', '100', 'payment'))
        with self.assertRaises(BookError):
            self.w.subsets.search('p', '2026-02-01', maximum_invoices=2)
        report = self.w.subsets.search('p', '2026-02-01', maximum_solutions=1)
        self.assertTrue(report['truncated'])
        self.assertTrue(report['ambiguous'])
        with self.assertRaises(BookError):
            self.w.subsets.propose('p1', 'p', '2026-02-01', 'absent')
        self.assertEqual(self.w.db.rows('SELECT * FROM plans'), [])

    def test_confirmation_difference_requires_resolution(self):
        self.w.ledger.post(self.row())
        self.w.confirmations.submit('c', 'a', '2026-02-01',
                                    [{'key': 'x', 'invoice': 'i', 'currency': 'CNY', 'open': '90'}])
        report = self.w.confirmations.reconcile('c')
        self.assertEqual(report['unresolved'], 1)
        self.assertEqual(report['rows'][0]['difference_minor'], -1000)
        with self.assertRaises(BookError):
            self.w.confirmations.close('c', 1, report['baseline'], 'reviewer')
        resolved = self.w.confirmations.decide('c', 'x', 'timing', 'cutoff', 'reviewer', 1)
        self.assertEqual(resolved['unresolved'], 0)
        self.assertEqual(self.w.confirmations.close('c', 2, resolved['baseline'], 'reviewer')['state'], 'closed')

    def test_confirmation_exact_match_and_local_only(self):
        self.w.ledger.post(self.row())
        self.w.ledger.post(self.row('j', '50', reference='SECOND'))
        self.w.confirmations.submit('c', 'a', '2026-02-01',
                                    [{'key': 'x', 'reference': 'R', 'currency': 'CNY', 'open': '100'}])
        report = self.w.confirmations.reconcile('c')
        self.assertEqual([r['status'] for r in report['rows']], ['matched', 'local_only'])
        self.assertEqual(report['unresolved'], 1)
        with self.assertRaises(BookError):
            self.w.confirmations.decide('c', 'x', 'timing', 'wrong', 'operator', 1)
        self.assertEqual(self.w.confirmations.get('c')['revision'], 1)

    def test_confirmation_stale_revision_and_snapshot(self):
        self.w.ledger.post(self.row())
        self.w.confirmations.submit('c', 'a', '2026-02-01', [])
        self.w.confirmations.decide('c', 'local:i', 'customer_error', 'omitted', 'operator', 1)
        report = self.w.confirmations.reconcile('c')
        with self.assertRaises(BookError):
            self.w.confirmations.close('c', 1, report['baseline'], 'operator')
        self.w.ledger.post(self.row('j'))
        with self.assertRaises(BookError):
            self.w.confirmations.close('c', 2, report['baseline'], 'operator')
        self.assertEqual(self.w.confirmations.get('c')['state'], 'open')

    def test_dispatch_exposes_added_business_operations(self):
        result = self.w.dispatch({'action': 'inbox.stage', 'arguments':
                                 {'package_id': 'pkg', 'source': 'erp', 'rows': [self.row()]}})
        self.assertEqual(result['state'], 'staged')
        self.assertTrue(self.w.dispatch({'action': 'inbox.inspect', 'arguments': {'package_id': 'pkg'}})['valid'])
        with self.assertRaises(BookError):
            self.w.dispatch({'action': 'not-an-action'})
        with self.assertRaises(BookError):
            self.w.dispatch({'action': 'document.get', 'arguments': {'wrong': 'i'}})
        self.assertEqual(self.w.db.rows('SELECT * FROM documents'), [])

    def test_profile_apply_checks_preview_and_is_replayable(self):
        mapping = {key: key for key in self.row()}
        self.w.profiles.save('erp', 'documents', mapping)
        content = 'id,kind,amount,currency,party,issued,reference,due\ni,invoice,12.50,CNY,a,2026-01-01,R,2026-01-31\n'
        preview = self.w.profiles.preview('erp', content)
        with self.assertRaises(BookError):
            self.w.profiles.apply('run', 'erp', content, 'wrong')
        self.assertEqual(self.w.db.rows('SELECT * FROM documents'), [])
        result = self.w.profiles.apply('run', 'erp', content, preview['input_digest'])
        self.assertEqual(self.w.ledger.balance('i'), 1250)
        self.assertEqual(self.w.profiles.apply('run', 'erp', content, preview['input_digest']), result)
        self.assertTrue(self.w.controls.require_valid()['ok'])

    def test_profile_reports_bad_amount_and_date_together(self):
        mapping = {key: key for key in self.row()}
        self.w.profiles.save('erp', 'documents', mapping)
        content = 'id,kind,amount,currency,party,issued,reference,due\ni,invoice,no,CNY,a,2026-01-01,R,2026-01-31\nj,invoice,12,CNY,a,2026-02-30,R,2026-03-31\n'
        preview = self.w.profiles.preview('erp', content)
        self.assertEqual(len(preview['errors']), 2)
        self.assertEqual(preview['rows'], [])
        self.assertEqual([r['line'] for r in preview['errors']], [2, 3])
        with self.assertRaises(BookError):
            self.w.profiles.apply('run', 'erp', content, preview['input_digest'])
        self.assertEqual(self.w.db.rows('SELECT * FROM import_runs'), [])

    def test_reference_matching_does_not_choose_different_reference(self):
        self.w.ledger.post(self.row('i', '100', reference='OTHER'))
        self.w.ledger.post(self.row('j', '70', reference='R'))
        self.w.ledger.post(self.row('p', '100', 'payment'))
        plan = self.w.matching.propose('ref', 'p', '2026-02-01', 'reference')
        self.assertEqual([r['invoice'] for r in plan['allocations']], ['j'])
        self.assertEqual(plan['unapplied_minor'], 3000)
        self.w.matching.confirm('ref', 'operator')
        self.assertEqual(self.w.ledger.balance('i'), 10000)
        self.assertEqual(self.w.ledger.balance('j'), 0)
        self.assertEqual(self.w.ledger.balance('p'), 3000)

    def test_reference_ambiguity_is_never_auto_confirmed(self):
        self.w.ledger.post(self.row('i', '40'))
        self.w.ledger.post(self.row('j', '60'))
        self.w.ledger.post(self.row('p', '100', 'payment'))
        plan = self.w.matching.propose('ref', 'p', '2026-02-01', 'reference')
        self.assertTrue(plan['ambiguous'])
        self.assertEqual(plan['allocations'], [])
        with self.assertRaises(BookError):
            self.w.matching.confirm('ref', 'operator')
        self.assertEqual(self.w.ledger.balance('p'), 10000)
        self.assertEqual(self.w.db.rows('SELECT * FROM allocations'), [])

    def test_customer_confirmation_duplicate_row_is_atomic(self):
        self.w.ledger.post(self.row())
        row = {'key': 'x', 'invoice': 'i', 'currency': 'CNY', 'open': '100'}
        before = self.w.db.verify_audit()
        with self.assertRaises(BookError):
            self.w.confirmations.submit('c', 'a', '2026-02-01', [row, row])
        self.assertEqual(self.w.db.rows('SELECT * FROM confirmations'), [])
        self.assertEqual(self.w.db.verify_audit(), before)
        self.w.confirmations.submit('c', 'a', '2026-02-01', [row])
        self.assertEqual(self.w.confirmations.reconcile('c')['unresolved'], 0)
        self.assertEqual(self.w.confirmations.get('c')['state'], 'open')

    def test_inbox_identity_and_revision_are_immutable(self):
        self.w.inbox.stage('pkg', 'erp', [self.row()])
        before = self.w.db.verify_audit()
        self.assertEqual(self.w.inbox.stage('pkg', 'erp', [self.row()])['revision'], 1)
        self.assertEqual(before, self.w.db.verify_audit())
        with self.assertRaises(BookError):
            self.w.inbox.stage('pkg', 'erp', [self.row('j')])
        review = self.w.inbox.inspect('pkg')
        with self.assertRaises(BookError):
            self.w.inbox.approve('pkg', 2, review['review_digest'], 'reviewer')
        self.assertEqual(self.w.inbox.get('pkg')['state'], 'staged')
        self.assertEqual(self.w.db.rows('SELECT * FROM documents'), [])

    def test_future_reserved_amount_limits_match_proposal(self):
        self.w.ledger.post(self.row())
        self.w.ledger.post(self.row('p', '100', 'payment'))
        self.w.ledger.allocate('x', 'p', 'i', '80', '2026-02-01')
        self.w.ledger.reverse_allocation('rx', 'x', '2026-04-01', 'correction')
        report = self.w.matching.candidates('p', '2026-01-15')
        self.assertEqual(report['available_minor'], 2000)
        self.assertEqual(report['candidates'][0]['amount_minor'], 2000)
        plan = self.w.matching.propose('safe', 'p', '2026-01-15', 'oldest_due')
        self.assertEqual(plan['allocations'][0]['amount_minor'], 2000)
        self.w.matching.confirm('safe', 'operator')
        self.assertEqual(self.w.ledger.balance('i', '2026-03-01'), 0)
        self.assertTrue(self.w.controls.require_valid()['ok'])

    def test_duplicate_inbox_rows_never_pass_review(self):
        self.w.inbox.stage('pkg', 'erp', [self.row(), self.row()])
        review = self.w.inbox.inspect('pkg')
        self.assertFalse(review['valid'])
        self.assertEqual(len(review['failures']), 1)
        self.assertEqual(review['failures'][0]['row'], 1)
        self.assertEqual(review['failures'][0]['error']['code'], 'duplicate')
        with self.assertRaises(BookError):
            self.w.inbox.approve('pkg', 1, review['review_digest'], 'reviewer')
        self.assertEqual(self.w.db.rows('SELECT * FROM documents'), [])
        self.assertEqual(self.w.inbox.get('pkg')['state'], 'staged')

    def test_closed_period_blocks_approved_package_without_partial_post(self):
        self.w.inbox.stage('pkg', 'erp', [self.row()])
        review = self.w.inbox.inspect('pkg')
        self.w.inbox.approve('pkg', 1, review['review_digest'], 'reviewer')
        self.w.periods.create('jan', '2026-01-01', '2026-02-01')
        close = self.w.periods.preview('jan')
        self.w.periods.close('jan', close['digest'], 'controller')
        with self.assertRaises(BookError) as caught:
            self.w.inbox.commit('pkg', 2)
        self.assertEqual(caught.exception.code, 'period')
        self.assertEqual(self.w.inbox.get('pkg')['state'], 'approved')
        self.assertEqual(self.w.db.rows('SELECT * FROM documents'), [])
        self.assertTrue(self.w.periods.verify('jan')['verified'])
