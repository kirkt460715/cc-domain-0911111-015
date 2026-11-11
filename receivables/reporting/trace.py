from ..primitives import day, identifier, integer, decode, error, display


class Trace:
    def __init__(self, database, ledger):
        self.db = database
        self.ledger = ledger

    def document(self, document_id, cutoff='2200-12-31'):
        cutoff = day(cutoff)
        document = self.ledger.document(document_id)
        allocations = self.db.rows(
            'SELECT * FROM allocations WHERE (source=? OR invoice=?) AND effective<=? ORDER BY effective,id',
            (document_id, document_id, cutoff))
        events = []
        if document['issued'] <= cutoff:
            events.append({'kind': 'issued', 'date': document['issued'], 'id': document_id,
                           'delta_minor': document['amount'], 'rank': 0})
        for row in allocations:
            events.append({'kind': 'allocated', 'date': row['effective'], 'id': row['id'],
                           'delta_minor': -row['amount'], 'rank': 1,
                           'counterparty_document': row['source'] if document['kind'] == 'invoice' else row['invoice']})
            if row['reversed'] and row['reversed'] <= cutoff:
                events.append({'kind': 'allocation_reversed', 'date': row['reversed'],
                               'id': row['id'], 'delta_minor': row['amount'], 'rank': 2})
        reversal = self.db.one('SELECT * FROM document_reversals WHERE document=?', (document_id,))
        if reversal and reversal['effective'] <= cutoff:
            events.append({'kind': 'document_reversed', 'date': reversal['effective'],
                           'id': reversal['reversal'], 'delta_minor': -document['amount'], 'rank': 3})
        events.sort(key=lambda row: (row['date'], row['rank'], row['id']))
        balance = 0
        for row in events:
            balance += row['delta_minor']
            row['balance_minor'] = balance
            row['balance'] = display(balance, document['currency'])
            row.pop('rank')
        actual = self.ledger.balance(document_id, cutoff)
        if balance != actual:
            error('integrity', 'document event trail differs from balance')
        bank = self.db.rows('SELECT l.*,b.booked,b.account FROM bank_links l JOIN bank_lines b ON b.id=l.line WHERE l.payment=?', (document_id,))
        disputes = self.db.rows('SELECT * FROM disputes WHERE invoice=? AND opened<=? ORDER BY opened,id', (document_id, cutoff))
        return {'document': document_id, 'currency': document['currency'], 'cutoff': cutoff,
                'events': events, 'balance_minor': actual, 'bank_evidence': bank, 'disputes': disputes}

    def provenance(self, document_id):
        self.ledger.document(document_id)
        batches = []
        for row in self.db.rows('SELECT * FROM batches ORDER BY id'):
            result = decode(row['result'])
            if document_id in result['documents']:
                batches.append({'id': row['id'], 'digest': row['digest']})
        plans = []
        for row in self.db.rows('SELECT * FROM plans ORDER BY id'):
            payload = decode(row['payload'])
            invoices = {item['invoice'] for item in payload['allocations']}
            if payload['source'] == document_id or document_id in invoices:
                plans.append({'id': row['id'], 'state': row['state'], 'digest': row['digest']})
        audit = []
        relevant = {document_id}
        relevant.update(row['id'] for row in batches)
        relevant.update(row['id'] for row in plans)
        for row in self.db.rows('SELECT * FROM audit ORDER BY sequence'):
            if row['entity'] in relevant:
                audit.append({'sequence': row['sequence'], 'kind': row['kind'],
                              'entity': row['entity'], 'digest': row['digest']})
        return {'document': document_id, 'batches': batches, 'plans': plans, 'audit': audit}

    def allocation_graph(self, party, cutoff, currency=None):
        self.ledger.party(party)
        cutoff = day(cutoff)
        if currency is not None:
            from ..primitives import currency as validate_currency
            validate_currency(currency)
        nodes = self.db.rows('SELECT id,kind,currency,amount FROM documents WHERE party=? AND issued<=? ORDER BY id', (party, cutoff))
        nodes = [row for row in nodes if currency is None or row['currency'] == currency]
        keys = {row['id'] for row in nodes}
        edges = [row for row in self.db.rows('SELECT * FROM allocations WHERE effective<=? AND (reversed IS NULL OR reversed>?) ORDER BY id', (cutoff, cutoff))
                 if row['source'] in keys and row['invoice'] in keys]
        neighbours = {key: set() for key in keys}
        for row in edges:
            neighbours[row['source']].add(row['invoice'])
            neighbours[row['invoice']].add(row['source'])
        components = []
        remaining = set(keys)
        while remaining:
            seed = min(remaining)
            pending = [seed]
            visited = set()
            while pending:
                key = pending.pop()
                if key in visited:
                    continue
                visited.add(key)
                pending.extend(neighbours[key] - visited)
            remaining -= visited
            components.append(sorted(visited))
        for row in nodes:
            row['balance_minor'] = self.ledger.balance(row['id'], cutoff)
        return {'party': party, 'cutoff': cutoff, 'nodes': nodes, 'edges': edges,
                'components': components}

    def audit_page(self, after=0, limit=100, kind=None):
        integer(after)
        integer(limit, 1, 1000)
        if kind is not None:
            identifier(kind)
        self.db.verify_audit()
        sql = 'SELECT * FROM audit WHERE sequence>?'
        args = [after]
        if kind is not None:
            sql += ' AND kind=?'
            args.append(kind)
        sql += ' ORDER BY sequence LIMIT ?'
        args.append(limit + 1)
        rows = self.db.rows(sql, args)
        page = rows[:limit]
        for row in page:
            row['payload'] = decode(row['payload'])
        return {'rows': page, 'next': page[-1]['sequence'] if len(rows) > limit else None}
