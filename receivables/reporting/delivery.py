from ..primitives import identifier, integer, canonical, decode, digest, error


class JournalDelivery:
    def __init__(self, database):
        self.db = database
        self.db.connection.executescript('''
CREATE TABLE IF NOT EXISTS consumers(id TEXT PRIMARY KEY,cursor INTEGER NOT NULL,
 lease_owner TEXT,lease_until INTEGER NOT NULL,generation INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,consumer TEXT NOT NULL,
 start INTEGER NOT NULL,finish INTEGER NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,
 state TEXT NOT NULL,generation INTEGER NOT NULL);
''')

    def register(self, consumer):
        identifier(consumer)
        with self.db.transaction() as con:
            con.execute('INSERT OR IGNORE INTO consumers VALUES(?,0,NULL,0,0)', (consumer,))
        return self.status(consumer)

    def status(self, consumer):
        row = self.db.one('SELECT * FROM consumers WHERE id=?', (consumer,))
        if row is None:
            error('missing', 'consumer not registered')
        return row

    def lease(self, consumer, owner, now, seconds=60):
        identifier(owner)
        integer(now)
        integer(seconds, 1, 3600)
        with self.db.transaction() as con:
            row = self.status(consumer)
            if row['lease_owner'] is not None and row['lease_until'] > now:
                if row['lease_owner'] != owner:
                    error('lease', 'consumer leased by another worker')
                generation = row['generation']
            else:
                generation = row['generation'] + 1
            con.execute('UPDATE consumers SET lease_owner=?,lease_until=?,generation=? WHERE id=?',
                        (owner, now + seconds, generation, consumer))
        return self.status(consumer)

    def check(self, consumer, owner, generation, now):
        identifier(owner)
        integer(generation, 1)
        integer(now)
        row = self.status(consumer)
        if (row['lease_owner'] != owner or row['generation'] != generation
                or row['lease_until'] <= now):
            error('lease', 'worker lease expired or fenced')
        return row

    def release(self, consumer, owner, generation, now):
        with self.db.transaction() as con:
            self.check(consumer, owner, generation, now)
            con.execute('UPDATE consumers SET lease_owner=NULL,lease_until=0 WHERE id=?', (consumer,))
        return self.status(consumer)

    def prepare(self, delivery_id, consumer, owner, generation, now, limit=100):
        identifier(delivery_id)
        integer(limit, 1, 10000)
        with self.db.transaction() as con:
            state = self.check(consumer, owner, generation, now)
            old = self.db.one('SELECT * FROM deliveries WHERE id=?', (delivery_id,))
            if old:
                if old['consumer'] != consumer:
                    error('conflict', 'delivery belongs to another consumer')
                return self.get(delivery_id)
            outstanding = self.db.one("SELECT id FROM deliveries WHERE consumer=? AND state='prepared'", (consumer,))
            if outstanding:
                error('state', 'resume outstanding delivery before preparing another', delivery=outstanding['id'])
            rows = self.db.rows('SELECT p.*,d.party,d.currency FROM postings p JOIN documents d ON d.id=p.document WHERE p.sequence>? ORDER BY p.sequence LIMIT ?',
                                (state['cursor'], limit))
            # A journal entry is indivisible even when the requested page ends in its middle.
            if rows:
                last = rows[-1]
                following = self.db.rows('SELECT p.*,d.party,d.currency FROM postings p JOIN documents d ON d.id=p.document WHERE p.sequence>? ORDER BY p.sequence',
                                         (last['sequence'],))
                for row in following:
                    if (row['entry'], row['effective'], row['reason']) != (last['entry'], last['effective'], last['reason']):
                        break
                    rows.append(row)
            end = rows[-1]['sequence'] if rows else state['cursor']
            payload = {'version': 1, 'consumer': consumer, 'start': state['cursor'],
                       'finish': end, 'postings': rows}
            totals = {}
            for row in rows:
                totals[row['currency']] = totals.get(row['currency'], 0) + row['amount']
            if any(totals.values()):
                error('integrity', 'journal delivery is not balanced by currency')
            payload['control_totals'] = totals
            con.execute("INSERT INTO deliveries VALUES(?,?,?,?,?,?,'prepared',?)",
                        (delivery_id, consumer, state['cursor'], end, canonical(payload), digest(payload), generation))
        return self.get(delivery_id)

    def get(self, delivery_id):
        row = self.db.one('SELECT * FROM deliveries WHERE id=?', (delivery_id,))
        if row is None:
            error('missing', 'delivery not found')
        payload = decode(row['payload'])
        if digest(payload) != row['digest']:
            error('integrity', 'delivery payload differs')
        return {**row, 'payload': payload}

    def resume(self, consumer, owner, generation, now):
        with self.db.transaction() as con:
            state = self.check(consumer, owner, generation, now)
            row = self.db.one("SELECT id,start FROM deliveries WHERE consumer=? AND state='prepared'", (consumer,))
            if row is None:
                return None
            if row['start'] != state['cursor']:
                error('integrity', 'delivery cursor discontinuity')
            con.execute('UPDATE deliveries SET generation=? WHERE id=?', (generation, row['id']))
        return self.get(row['id'])

    def acknowledge(self, delivery_id, owner, generation, now, checksum):
        with self.db.transaction() as con:
            delivery = self.get(delivery_id)
            state = self.check(delivery['consumer'], owner, generation, now)
            if checksum != delivery['digest']:
                error('integrity', 'receiver checksum differs')
            if delivery['state'] == 'acknowledged':
                return delivery
            if delivery['generation'] != generation:
                error('lease', 'resume delivery with current fencing generation')
            if state['cursor'] != delivery['start']:
                error('conflict', 'delivery checkpoint changed')
            con.execute('UPDATE consumers SET cursor=? WHERE id=?',
                        (delivery['finish'], delivery['consumer']))
            con.execute("UPDATE deliveries SET state='acknowledged' WHERE id=?", (delivery_id,))
            self.db.audit('journal_acknowledged', delivery_id,
                          {'consumer': delivery['consumer'], 'finish': delivery['finish'], 'digest': checksum})
        return self.get(delivery_id)

    def verify_checkpoint(self, consumer):
        state = self.status(consumer)
        deliveries = self.db.rows("SELECT id,start,finish FROM deliveries WHERE consumer=? AND state='acknowledged' ORDER BY start,finish,id", (consumer,))
        cursor = 0
        for row in deliveries:
            item = self.get(row['id'])
            if row['start'] != cursor:
                error('integrity', 'acknowledged journal has gap')
            postings = item['payload']['postings']
            if postings and postings[-1]['sequence'] != row['finish']:
                error('integrity', 'delivery last sequence differs')
            cursor = row['finish']
        if cursor != state['cursor']:
            error('integrity', 'consumer checkpoint differs from deliveries')
        return {'consumer': consumer, 'cursor': cursor, 'deliveries': len(deliveries), 'verified': True}

    def envelope(self, delivery_id):
        row = self.get(delivery_id)
        return canonical({'id': delivery_id, 'digest': row['digest'], 'payload': row['payload']})

    @staticmethod
    def verify_envelope(content):
        from ..primitives import obj
        if not isinstance(content, str) or len(content.encode()) > 30_000_000:
            error('input', 'invalid journal envelope size')
        row = decode(content)
        obj(row, {'id', 'digest', 'payload'}, {'id', 'digest', 'payload'})
        identifier(row['id'])
        payload = row['payload']
        obj(payload, {'version', 'consumer', 'start', 'finish', 'postings', 'control_totals'},
            {'version', 'consumer', 'start', 'finish', 'postings', 'control_totals'})
        if digest(payload) != row['digest'] or payload['version'] != 1:
            error('integrity', 'journal envelope fingerprint or version differs')
        cursor = integer(payload['start'])
        totals = {}
        for posting in payload['postings']:
            if integer(posting['sequence'], 1) != cursor + 1:
                error('integrity', 'noncontiguous posting sequence')
            cursor += 1
            amount = posting['amount']
            if isinstance(amount, bool) or not isinstance(amount, int):
                error('integrity', 'posting amount must be integer')
            code = posting['currency']
            totals[code] = totals.get(code, 0) + amount
        if cursor != payload['finish'] or totals != payload['control_totals'] or any(totals.values()):
            error('integrity', 'journal control totals differ')
        return {'id': row['id'], 'verified': True, 'finish': cursor}
