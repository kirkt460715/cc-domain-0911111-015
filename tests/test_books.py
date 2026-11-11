import unittest
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from receivables.service import Workbench
from receivables.primitives import BookError, money, day, decode, display, identifier, integer


class BookCase(unittest.TestCase):
    def setUp(self):
        self.work = Workbench()
        self.addCleanup(self.work.close)
        self.work.ledger.register_party('a', 'Alpha', ['Alpha Ltd'])
        self.work.ledger.register_party('b', 'Beta')

    def post(self, ident='i', kind='invoice', amount='100.00', **extra):
        row = dict(id=ident, kind=kind, amount=amount, currency='CNY',
                   party='a', issued='2026-01-01', reference='REF')
        if kind == 'invoice':
            row['due'] = '2026-01-31'
        row.update(extra)
        self.work.ledger.post(row)
        return row

    def test_currency_precision_and_roundtrip(self):
        self.assertEqual(money('1.01', 'CNY'), 101)
        self.assertEqual(money('2', 'JPY'), 2)
        self.assertEqual(money('1.001', 'KWD'), 1001)
        self.assertEqual(display(1001, 'KWD'), '1.001')
        with self.assertRaises(BookError) as caught:
            money('1.001', 'CNY')
        self.assertEqual(caught.exception.code, 'precision')
        self.assertEqual(money('0', 'CNY', True), 0)

    def test_reject_nonfinite_and_inexact_money(self):
        for value in ['NaN', 'Infinity', '-1', '0', '1e20', True, 1.5, None]:
            with self.subTest(value=value):
                with self.assertRaises(BookError):
                    money(value, 'USD')
        with self.assertRaises(BookError):
            money('10', 'GBP')
        self.assertEqual(money('1e2', 'USD'), 10000)

    def test_strict_dates_identifiers_and_integers(self):
        for value in ['2026-02-29', '20260101', '2026-1-1', '1969-12-31', None]:
            with self.assertRaises(BookError):
                day(value)
        self.assertEqual(day('2024-02-29'), '2024-02-29')
        for value in ['', ' a', '../x', True, 'x' * 101]:
            with self.assertRaises(BookError):
                identifier(value)
        for value in [True, -1, 2.2, '2']:
            with self.assertRaises(BookError):
                integer(value)

    def test_duplicate_json_and_nonfinite_are_not_accepted(self):
        for content in ['{"a":1,"a":2}', '{"x":NaN}', '{"x":Infinity}', '{']:
            with self.assertRaises(BookError):
                decode(content)
        self.assertEqual(decode('{"a":{"b":2}}'), {'a': {'b': 2}})
        self.assertEqual(decode('[1,2]'), [1, 2])

    def test_party_registration_is_immutable(self):
        repeat = self.work.ledger.register_party('a', 'Alpha', ['Alpha Ltd'])
        self.assertEqual(repeat['aliases'], ['alpha ltd'])
        with self.assertRaises(BookError):
            self.work.ledger.register_party('a', 'Changed')
        self.assertEqual(self.work.ledger.party('a')['name'], 'Alpha')
        with self.assertRaises(BookError):
            self.work.ledger.party('missing')
        self.assertEqual(self.work.db.verify_audit()['entries'], 2)

    def test_document_replay_is_idempotent(self):
        row = self.post()
        count = self.work.db.verify_audit()['entries']
        result = self.work.ledger.post(row)
        self.assertTrue(result['existing'])
        self.assertEqual(self.work.ledger.balance('i'), 10000)
        self.assertEqual(self.work.db.verify_audit()['entries'], count)
        row['amount'] = '99.00'
        with self.assertRaises(BookError):
            self.work.ledger.post(row)
        self.assertEqual(self.work.ledger.balance('i'), 10000)

    def test_document_validation_rolls_back(self):
        for values in [dict(due='2025-12-31'), dict(party='absent'),
                       dict(currency='XXX'), dict(amount='1.001'), dict(extra=True)]:
            with self.assertRaises(BookError):
                self.post(**values)
        self.assertEqual(self.work.db.rows('SELECT * FROM documents'), [])
        self.assertEqual(self.work.db.rows('SELECT * FROM postings'), [])
        self.assertTrue(self.work.controls.inspect()['ok'])
        with self.assertRaises(BookError):
            self.post('p', 'payment', due='2026-01-01')

    def test_allocate_then_reverse_preserves_history(self):
        self.post()
        self.post('p', 'payment', '60.00')
        result = self.work.ledger.allocate('a1', 'p', 'i', '40.00', '2026-02-01')
        self.assertFalse(result['existing'])
        self.assertEqual(self.work.ledger.balance('i', '2026-01-31'), 10000)
        self.assertEqual(self.work.ledger.balance('i', '2026-02-01'), 6000)
        self.work.ledger.reverse_allocation('r1', 'a1', '2026-03-01', 'correction')
        self.assertEqual(self.work.ledger.balance('i', '2026-02-28'), 6000)
        self.assertEqual(self.work.ledger.balance('i', '2026-03-01'), 10000)
        self.assertTrue(self.work.controls.require_valid()['ok'])

    def test_allocation_replay_and_identity_conflict(self):
        self.post()
        self.post('p', 'payment')
        self.work.ledger.allocate('x', 'p', 'i', '20', '2026-02-01')
        self.assertTrue(self.work.ledger.allocate('x', 'p', 'i', '20.00', '2026-02-01')['existing'])
        with self.assertRaises(BookError):
            self.work.ledger.allocate('x', 'p', 'i', '21', '2026-02-01')
        self.assertEqual(self.work.ledger.balance('p'), 8000)
        self.assertEqual(len(self.work.db.rows('SELECT * FROM allocations')), 1)
        self.assertTrue(self.work.controls.subledger('2026-02-01')['balanced'])

    def test_currency_party_and_kind_boundaries(self):
        self.post()
        self.post('p', 'payment', party='b')
        self.post('u', 'payment', currency='USD')
        for source, invoice in [('p', 'i'), ('u', 'i'), ('i', 'p'), ('i', 'i')]:
            with self.assertRaises(BookError):
                self.work.ledger.allocate('bad', source, invoice, '10', '2026-02-01')
        self.assertEqual(self.work.db.rows('SELECT * FROM allocations'), [])
        self.assertTrue(self.work.controls.inspect()['ok'])

    def test_allocation_before_issue_and_overdraw(self):
        self.post()
        self.post('p', 'payment', '10')
        with self.assertRaises(BookError):
            self.work.ledger.allocate('x', 'p', 'i', '1', '2025-12-31')
        with self.assertRaises(BookError):
            self.work.ledger.allocate('x', 'p', 'i', '11', '2026-02-01')
        self.work.ledger.allocate('x', 'p', 'i', '10', '2026-02-01')
        self.assertEqual(self.work.ledger.balance('p'), 0)
        self.assertEqual(self.work.ledger.balance('i'), 9000)

    def test_intermediate_reservation_is_not_spent_twice(self):
        self.post()
        self.post('p', 'payment')
        self.work.ledger.allocate('x', 'p', 'i', '80', '2026-02-01')
        self.work.ledger.reverse_allocation('rx', 'x', '2026-04-01', 'return')
        self.assertEqual(self.work.ledger.balance('p'), 10000)
        self.assertEqual(self.work.ledger.available_from('p', '2026-01-15'), 2000)
        with self.assertRaises(BookError):
            self.work.ledger.allocate('y', 'p', 'i', '21', '2026-01-15')
        self.work.ledger.allocate('y', 'p', 'i', '20', '2026-01-15')
        self.assertEqual(self.work.ledger.balance('i', '2026-03-01'), 0)
        self.assertTrue(self.work.controls.require_valid()['ok'])

    def test_document_reversal_requires_all_allocations_reversed(self):
        self.post()
        self.post('p', 'payment')
        self.work.ledger.allocate('x', 'p', 'i', '10', '2026-01-10')
        with self.assertRaises(BookError):
            self.work.ledger.reverse_document('r', 'i', '2026-02-01', 'void')
        self.work.ledger.reverse_allocation('rx', 'x', '2026-02-01', 'void')
        self.work.ledger.reverse_document('r', 'i', '2026-02-01', 'void')
        self.assertEqual(self.work.ledger.balance('i'), 0)
        self.assertEqual(self.work.ledger.balance('i', '2026-01-31'), 9000)
        self.assertTrue(self.work.controls.require_valid()['ok'])

    def test_document_reversal_replay_and_conflict(self):
        self.post()
        first = self.work.ledger.reverse_document('r', 'i', '2026-02-01', 'void')
        self.assertFalse(first['existing'])
        self.assertTrue(self.work.ledger.reverse_document('r', 'i', '2026-02-01', 'void')['existing'])
        with self.assertRaises(BookError):
            self.work.ledger.reverse_document('r2', 'i', '2026-02-02', 'void')
        with self.assertRaises(BookError):
            self.work.ledger.reverse_document('r', 'i', '2026-02-01', 'different')
        self.assertEqual(self.work.ledger.balance('i'), 0)

    def test_credit_note_is_not_cash(self):
        self.post()
        self.post('c', 'credit', '25')
        self.work.ledger.allocate('x', 'c', 'i', '25', '2026-01-15')
        report = self.work.controls.trial_balance('2026-02-01')
        accounts = report['currencies'][0]['accounts_minor']
        self.assertEqual(accounts['receivable'], 7500)
        self.assertEqual(accounts['credit_control'], 2500)
        self.assertEqual(accounts.get('cash', 0), 0)
        self.assertEqual(report['currencies'][0]['difference_minor'], 0)

    def test_nested_transaction_undo_is_local(self):
        with self.work.db.transaction():
            self.post('outer')
            try:
                with self.work.db.transaction():
                    self.post('inner')
                    raise RuntimeError('crash')
            except RuntimeError:
                pass
            self.post('after')
        self.assertEqual([r['id'] for r in self.work.db.rows('SELECT id FROM documents ORDER BY id')], ['after', 'outer'])
        self.assertTrue(self.work.controls.require_valid()['ok'])

    def test_outer_rollback_includes_audit(self):
        before = self.work.db.verify_audit()
        with self.assertRaises(RuntimeError):
            with self.work.db.transaction():
                self.post()
                raise RuntimeError('abort')
        self.assertEqual(self.work.db.verify_audit(), before)
        self.assertEqual(self.work.db.rows('SELECT * FROM documents'), [])
        self.assertEqual(self.work.db.depth, 0)
        self.post()
        self.assertEqual(self.work.ledger.balance('i'), 10000)

    def test_audit_tamper_is_detected(self):
        self.post()
        self.work.db.connection.execute("UPDATE audit SET payload='{}' WHERE sequence=3")
        with self.assertRaises(BookError) as caught:
            self.work.db.verify_audit()
        self.assertEqual(caught.exception.code, 'integrity')
        with self.assertRaises(BookError):
            self.work.controls.inspect()

    def test_document_tamper_surfaces_control_finding(self):
        self.post()
        self.work.db.connection.execute("UPDATE documents SET digest='bad' WHERE id='i'")
        report = self.work.controls.inspect()
        self.assertFalse(report['ok'])
        self.assertIn({'kind': 'document_digest', 'id': 'i'}, report['findings'])
        with self.assertRaises(BookError):
            self.work.controls.require_valid()

    def test_backup_reopens_with_same_balances(self):
        self.post()
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'copy.db'
            self.work.db.backup(target)
            with Workbench(target) as reopened:
                self.assertEqual(reopened.ledger.balance('i'), 10000)
                self.assertEqual(reopened.db.verify_audit(), self.work.db.verify_audit())
            with self.assertRaises(BookError):
                self.work.db.backup(target)
            with self.work.db.transaction():
                with self.assertRaises(BookError):
                    self.work.db.backup(Path(folder) / 'unsafe.db')

    def test_concurrent_workers_cannot_overallocate(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'shared.db'
            with Workbench(db) as initial:
                initial.ledger.register_party('a', 'Alpha')
                for ident, kind in [('i', 'invoice'), ('p', 'payment')]:
                    row = dict(id=ident, kind=kind, amount='100', currency='CNY', party='a', issued='2026-01-01', reference='R')
                    if kind == 'invoice':
                        row['due'] = '2026-01-31'
                    initial.ledger.post(row)
            def attempt(ident):
                with Workbench(db) as other:
                    try:
                        other.ledger.allocate(ident, 'p', 'i', '70', '2026-02-01')
                        return 'ok'
                    except BookError as exc:
                        return exc.code
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(attempt, ['one', 'two']))
            self.assertEqual(results.count('ok'), 1)
            self.assertEqual(results.count('allocation'), 1)
            with Workbench(db) as final:
                self.assertEqual(final.ledger.balance('i'), 3000)
                self.assertTrue(final.controls.require_valid()['ok'])

    def test_trace_matches_historical_event_balance(self):
        self.post()
        self.post('p', 'payment')
        self.work.ledger.allocate('x', 'p', 'i', '20', '2026-02-01')
        self.work.ledger.reverse_allocation('r', 'x', '2026-03-01', 'error')
        earlier = self.work.trace.document('i', '2026-02-15')
        self.assertEqual(earlier['balance_minor'], 8000)
        self.assertEqual([x['kind'] for x in earlier['events']], ['issued', 'allocated'])
        later = self.work.trace.document('i')
        self.assertEqual(later['balance_minor'], 10000)
        self.assertEqual(later['events'][-1]['kind'], 'allocation_reversed')

    def test_graph_components_change_on_reversal(self):
        self.post()
        self.post('j')
        self.post('p', 'payment')
        self.work.ledger.allocate('x', 'p', 'i', '20', '2026-02-01')
        report = self.work.trace.allocation_graph('a', '2026-02-15')
        self.assertEqual(report['components'], [['i', 'p'], ['j']])
        self.work.ledger.reverse_allocation('r', 'x', '2026-03-01', 'error')
        self.assertEqual(self.work.trace.allocation_graph('a', '2026-03-01')['components'], [['i'], ['j'], ['p']])
        self.assertEqual(self.work.trace.allocation_graph('a', '2026-03-01', 'USD')['nodes'], [])
