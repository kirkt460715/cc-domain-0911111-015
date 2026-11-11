from ..primitives import identifier, canonical, decode, digest, error, integer, sequence


class Inbox:
    def __init__(self, database, documents):
        self.db = database
        self.documents = documents
        self.db.connection.executescript('''
CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY,source TEXT NOT NULL,
 payload TEXT NOT NULL,digest TEXT NOT NULL,state TEXT NOT NULL,revision INTEGER NOT NULL,
 review_digest TEXT,reviewer TEXT,result TEXT);
''')

    def stage(self, package_id, source, rows):
        identifier(package_id)
        identifier(source)
        sequence(rows, 1, 10000)
        payload = {'source': source, 'rows': rows}
        checksum = digest(payload)
        with self.db.transaction() as con:
            old = self.db.one('SELECT * FROM inbox WHERE id=?', (package_id,))
            if old:
                if old['digest'] != checksum:
                    error('conflict', 'inbox identity differs')
                return self.get(package_id)
            con.execute('INSERT INTO inbox VALUES(?,?,?,?,?,1,NULL,NULL,NULL)',
                        (package_id, source, canonical(payload), checksum, 'staged'))
            self.db.audit('inbox_staged', package_id, {'digest': checksum})
        return self.get(package_id)

    def get(self, package_id):
        row = self.db.one('SELECT * FROM inbox WHERE id=?', (package_id,))
        if not row:
            error('missing', 'inbox package not found')
        payload = decode(row['payload'])
        if digest(payload) != row['digest']:
            error('integrity', 'inbox payload changed')
        return {**row, 'payload': payload,
                'result': decode(row['result']) if row['result'] else None}

    def inspect(self, package_id):
        package = self.get(package_id)
        failures = []
        existing = []
        seen = set()
        for index, row in enumerate(package['payload']['rows']):
            from ..primitives import BookError, obj
            try:
                obj(row, required={'id'})
                identifier(row['id'])
                if row['id'] in seen:
                    error('duplicate', 'duplicate row identity')
                seen.add(row['id'])
                old = self.db.one('SELECT digest FROM documents WHERE id=?', (row['id'],))
                if old:
                    existing.append({'id': row['id'], 'digest': old['digest']})
                # Validate the real posting path in a savepoint, then undo it.
                class RollbackPreview(Exception):
                    pass
                try:
                    with self.db.transaction():
                        self.documents.ledger.post(row)
                        raise RollbackPreview()
                except RollbackPreview:
                    pass
            except BookError as exc:
                failures.append({'row': index, 'error': exc.as_dict()})
        evidence = {'package': package['digest'], 'existing': existing,
                    'failures': failures, 'revision': package['revision']}
        return {'id': package_id, 'failures': failures, 'existing': existing,
                'valid': not failures, 'review_digest': digest(evidence),
                'revision': package['revision'], 'state': package['state']}

    def approve(self, package_id, expected_revision, review_digest, reviewer):
        identifier(reviewer)
        integer(expected_revision, 1)
        with self.db.transaction() as con:
            package = self.get(package_id)
            if package['state'] != 'staged' or package['revision'] != expected_revision:
                error('conflict', 'inbox state changed')
            preview = self.inspect(package_id)
            if not preview['valid']:
                error('input', 'inbox contains invalid rows', failures=preview['failures'])
            if preview['review_digest'] != review_digest:
                error('conflict', 'review evidence changed')
            con.execute("UPDATE inbox SET state='approved',revision=revision+1,review_digest=?,reviewer=? WHERE id=?",
                        (review_digest, reviewer, package_id))
            self.db.audit('inbox_approved', package_id, {'reviewer': reviewer})
        return self.get(package_id)

    def commit(self, package_id, expected_revision):
        integer(expected_revision, 1)
        with self.db.transaction() as con:
            package = self.get(package_id)
            if package['state'] == 'committed':
                if expected_revision != package['revision'] - 1:
                    error('conflict', 'commit revision differs')
                return package
            if package['state'] != 'approved' or package['revision'] != expected_revision:
                error('conflict', 'inbox is not approved at this revision')
            result = self.documents.import_batch('inbox:' + package_id,
                                                 package['payload']['rows'])
            con.execute("UPDATE inbox SET state='committed',revision=revision+1,result=? WHERE id=?",
                        (canonical(result), package_id))
            self.db.audit('inbox_committed', package_id, result)
        return self.get(package_id)

    def reject(self, package_id, expected_revision, reason, reviewer):
        identifier(reviewer)
        integer(expected_revision, 1)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            error('input', 'rejection requires bounded reason')
        with self.db.transaction() as con:
            package = self.get(package_id)
            if package['state'] not in {'staged', 'approved'}:
                error('state', 'inbox cannot be rejected')
            if package['revision'] != expected_revision:
                error('conflict', 'inbox revision changed')
            result = {'reason': reason, 'reviewer': reviewer}
            con.execute("UPDATE inbox SET state='rejected',revision=revision+1,result=? WHERE id=?",
                        (canonical(result), package_id))
            self.db.audit('inbox_rejected', package_id, result)
        return self.get(package_id)

    def pending(self, source=None, after='', limit=100):
        integer(limit, 1, 1000)
        if source is not None:
            identifier(source)
        rows = self.db.rows("SELECT id,source,state,revision FROM inbox WHERE id>? AND state IN ('staged','approved') ORDER BY id",
                            (after,))
        rows = [row for row in rows if source is None or row['source'] == source]
        page = rows[:limit]
        return {'rows': page, 'next': page[-1]['id'] if len(rows) > limit else None}
