import unittest
import json
import tempfile
from pathlib import Path
from receivables.service import Workbench
from receivables.primitives import BookError


class DeliveryCase(unittest.TestCase):
    def setUp(self):
        self.w = Workbench()
        self.addCleanup(self.w.close)
        self.w.ledger.register_party('a', 'Alpha')
        self.w.ledger.post(dict(id='i', kind='invoice', party='a', currency='CNY', amount='100',
                               issued='2026-01-01', due='2026-01-31', reference='R'))
        self.w.delivery.register('erp')
        self.lease = self.w.delivery.lease('erp', 'worker', 10, 60)
        self.generation = self.lease['generation']

    def prepare(self, ident='d', now=11, limit=100):
        return self.w.delivery.prepare(ident, 'erp', 'worker', self.generation, now, limit)

    def test_entry_is_indivisible_at_page_boundary(self):
        row = self.prepare(limit=1)
        self.assertEqual(len(row['payload']['postings']), 2)
        self.assertEqual(row['start'], 0)
        self.assertEqual(row['finish'], 2)
        self.assertEqual(row['payload']['control_totals'], {'CNY': 0})
        self.assertEqual(self.w.delivery.status('erp')['cursor'], 0)
        self.assertEqual(row['state'], 'prepared')

    def test_prepare_retry_does_not_change_payload(self):
        first = self.prepare()
        repeated = self.prepare()
        self.assertEqual(first, repeated)
        self.assertEqual(len(self.w.db.rows('SELECT * FROM deliveries')), 1)
        self.w.ledger.post(dict(id='p', kind='payment', party='a', currency='CNY', amount='20',
                               issued='2026-01-02', reference='R'))
        self.assertEqual(self.prepare()['digest'], first['digest'])
        self.assertEqual(self.prepare()['finish'], 2)

    def test_ack_moves_checkpoint_atomically(self):
        row = self.prepare()
        result = self.w.delivery.acknowledge('d', 'worker', self.generation, 12, row['digest'])
        self.assertEqual(result['state'], 'acknowledged')
        self.assertEqual(self.w.delivery.status('erp')['cursor'], 2)
        self.assertTrue(self.w.delivery.verify_checkpoint('erp')['verified'])
        again = self.w.delivery.acknowledge('d', 'worker', self.generation, 13, row['digest'])
        self.assertEqual(again, result)
        self.assertEqual(self.w.delivery.verify_checkpoint('erp')['deliveries'], 1)

    def test_bad_checksum_cannot_advance_checkpoint(self):
        row = self.prepare()
        with self.assertRaises(BookError) as caught:
            self.w.delivery.acknowledge('d', 'worker', self.generation, 12, 'bad')
        self.assertEqual(caught.exception.code, 'integrity')
        self.assertEqual(self.w.delivery.status('erp')['cursor'], 0)
        self.assertEqual(self.w.delivery.get('d')['state'], 'prepared')
        self.w.delivery.acknowledge('d', 'worker', self.generation, 12, row['digest'])
        self.assertEqual(self.w.delivery.status('erp')['cursor'], 2)

    def test_expired_worker_is_fenced_and_new_worker_resumes(self):
        row = self.prepare()
        new = self.w.delivery.lease('erp', 'replacement', 70)
        self.assertGreater(new['generation'], self.generation)
        with self.assertRaises(BookError):
            self.w.delivery.acknowledge('d', 'worker', self.generation, 71, row['digest'])
        with self.assertRaises(BookError):
            self.w.delivery.acknowledge('d', 'replacement', new['generation'], 71, row['digest'])
        resumed = self.w.delivery.resume('erp', 'replacement', new['generation'], 71)
        self.assertEqual(resumed['digest'], row['digest'])
        self.w.delivery.acknowledge('d', 'replacement', new['generation'], 72, row['digest'])
        self.assertEqual(self.w.delivery.status('erp')['cursor'], 2)

    def test_active_lease_cannot_be_stolen(self):
        with self.assertRaises(BookError) as caught:
            self.w.delivery.lease('erp', 'other', 20)
        self.assertEqual(caught.exception.code, 'lease')
        renewed = self.w.delivery.lease('erp', 'worker', 30)
        self.assertEqual(renewed['generation'], self.generation)
        self.assertEqual(renewed['lease_until'], 90)
        self.assertEqual(self.w.delivery.status('erp')['lease_owner'], 'worker')
        self.assertEqual(self.w.delivery.status('erp')['cursor'], 0)

    def test_release_and_reacquire_changes_generation(self):
        self.w.delivery.release('erp', 'worker', self.generation, 20)
        self.assertIsNone(self.w.delivery.status('erp')['lease_owner'])
        with self.assertRaises(BookError):
            self.prepare(now=21)
        lease = self.w.delivery.lease('erp', 'worker', 21)
        self.assertGreater(lease['generation'], self.generation)
        with self.assertRaises(BookError):
            self.w.delivery.release('erp', 'worker', self.generation, 22)

    def test_outstanding_delivery_must_resume_before_next(self):
        row = self.prepare()
        with self.assertRaises(BookError):
            self.prepare('other')
        self.assertEqual(self.w.delivery.resume('erp', 'worker', self.generation, 12)['id'], 'd')
        self.w.delivery.acknowledge('d', 'worker', self.generation, 13, row['digest'])
        self.assertIsNone(self.w.delivery.resume('erp', 'worker', self.generation, 14))
        empty = self.prepare('other', now=15)
        self.assertEqual(empty['payload']['postings'], [])
        self.assertEqual(empty['start'], empty['finish'])

    def test_empty_page_acknowledges_without_skipping(self):
        row = self.prepare()
        self.w.delivery.acknowledge('d', 'worker', self.generation, 12, row['digest'])
        empty = self.prepare('empty', now=13)
        self.w.delivery.acknowledge('empty', 'worker', self.generation, 14, empty['digest'])
        self.assertEqual(self.w.delivery.status('erp')['cursor'], 2)
        self.assertTrue(self.w.delivery.verify_checkpoint('erp')['verified'])
        self.assertEqual(self.w.delivery.verify_checkpoint('erp')['deliveries'], 2)
        self.assertTrue(self.w.delivery.verify_envelope(self.w.delivery.envelope('empty'))['verified'])

    def test_envelope_roundtrip_and_tamper(self):
        self.prepare()
        content = self.w.delivery.envelope('d')
        verified = self.w.delivery.verify_envelope(content)
        self.assertTrue(verified['verified'])
        self.assertEqual(verified['finish'], 2)
        envelope = json.loads(content)
        envelope['payload']['postings'][0]['amount'] += 1
        with self.assertRaises(BookError):
            self.w.delivery.verify_envelope(json.dumps(envelope))
        with self.assertRaises(BookError):
            self.w.delivery.verify_envelope('{}')

    def test_delivery_payload_corruption_is_detected(self):
        self.prepare()
        self.w.db.connection.execute("UPDATE deliveries SET digest='wrong' WHERE id='d'")
        with self.assertRaises(BookError):
            self.w.delivery.get('d')
        with self.assertRaises(BookError):
            self.w.delivery.envelope('d')
        self.assertEqual(self.w.delivery.status('erp')['cursor'], 0)
        self.assertEqual(len(self.w.db.rows('SELECT * FROM deliveries')), 1)

    def test_independent_consumers_have_independent_checkpoints(self):
        row = self.prepare()
        self.w.delivery.register('warehouse')
        lease = self.w.delivery.lease('warehouse', 'etl', 10)
        second = self.w.delivery.prepare('w', 'warehouse', 'etl', lease['generation'], 11)
        self.assertEqual(second['payload']['postings'], row['payload']['postings'])
        self.w.delivery.acknowledge('d', 'worker', self.generation, 12, row['digest'])
        self.assertEqual(self.w.delivery.status('erp')['cursor'], 2)
        self.assertEqual(self.w.delivery.status('warehouse')['cursor'], 0)
        with self.assertRaises(BookError):
            self.w.delivery.prepare('d', 'warehouse', 'etl', lease['generation'], 12)

    def test_trace_provenance_and_audit_pagination(self):
        row = dict(id='j', kind='invoice', party='a', currency='CNY', amount='20',
                   issued='2026-01-01', due='2026-01-31', reference='R')
        self.w.documents.import_batch('import', [row])
        report = self.w.trace.provenance('j')
        self.assertEqual(report['batches'][0]['id'], 'import')
        self.assertEqual(report['plans'], [])
        self.assertTrue(any(r['kind'] == 'batch_imported' for r in report['audit']))
        first = self.w.trace.audit_page(limit=2)
        self.assertEqual(first['next'], 2)
        second = self.w.trace.audit_page(after=2)
        self.assertEqual(second['rows'][0]['sequence'], 3)
        self.assertIsNone(second['next'])

    def test_checkpoint_tamper_is_detected(self):
        self.prepare()
        self.w.db.connection.execute("UPDATE consumers SET cursor=99 WHERE id='erp'")
        with self.assertRaises(BookError):
            self.w.delivery.verify_checkpoint('erp')
        with self.assertRaises(BookError):
            self.w.delivery.resume('erp', 'worker', self.generation, 12)
        self.assertEqual(self.w.delivery.get('d')['state'], 'prepared')
        self.assertEqual(self.w.delivery.get('d')['finish'], 2)

    def test_receiver_rejects_rehashed_sequence_gap(self):
        from receivables.primitives import digest
        self.prepare()
        envelope = json.loads(self.w.delivery.envelope('d'))
        envelope['payload']['postings'][0]['sequence'] = 9
        envelope['digest'] = digest(envelope['payload'])
        with self.assertRaises(BookError) as caught:
            self.w.delivery.verify_envelope(json.dumps(envelope))
        self.assertEqual(caught.exception.code, 'integrity')
        self.assertEqual(self.w.delivery.status('erp')['cursor'], 0)

    def test_receiver_rejects_rehashed_imbalance(self):
        from receivables.primitives import digest
        self.prepare()
        envelope = json.loads(self.w.delivery.envelope('d'))
        envelope['payload']['postings'][0]['amount'] += 1
        envelope['payload']['control_totals']['CNY'] = 1
        envelope['digest'] = digest(envelope['payload'])
        with self.assertRaises(BookError):
            self.w.delivery.verify_envelope(json.dumps(envelope))
        self.assertEqual(self.w.delivery.get('d')['payload']['control_totals']['CNY'], 0)

    def test_reopen_resumes_unacknowledged_delivery(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'book.db'
            with Workbench(path) as first:
                first.ledger.register_party('a', 'Alpha')
                first.ledger.post(dict(id='i', kind='invoice', party='a', currency='CNY', amount='10',
                                       issued='2026-01-01', due='2026-01-31', reference='R'))
                first.delivery.register('erp')
                lease = first.delivery.lease('erp', 'first', 0, 1)
                prepared = first.delivery.prepare('d', 'erp', 'first', lease['generation'], 0)
            with Workbench(path) as second:
                lease = second.delivery.lease('erp', 'second', 2)
                resumed = second.delivery.resume('erp', 'second', lease['generation'], 3)
                self.assertEqual(resumed['digest'], prepared['digest'])
                second.delivery.acknowledge('d', 'second', lease['generation'], 4, resumed['digest'])
                self.assertTrue(second.delivery.verify_checkpoint('erp')['verified'])
                self.assertEqual(second.delivery.status('erp')['cursor'], 2)
