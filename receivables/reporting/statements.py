import csv
import io
import os
from pathlib import Path
from ..primitives import identifier, day, digest, canonical, decode, error


class Statements:
    def __init__(self, database, ledger, aging):
        self.db = database
        self.ledger = ledger
        self.aging = aging

    def freeze(self, statement_id, party, cutoff):
        identifier(statement_id)
        cutoff = day(cutoff)
        with self.db.transaction() as con:
            old = self.db.one("SELECT * FROM statements WHERE id=?", (statement_id,))
            if old:
                value = self.get(statement_id)
                if value["party"]["id"] != party or value["cutoff"] != cutoff:
                    error("conflict", "statement identity differs")
                return value
            report = self.aging.report(cutoff, party)
            payload = {
                "id": statement_id,
                "party": self.ledger.party(party),
                "cutoff": cutoff,
                "aging": report,
                "unapplied": self.aging.unapplied(cutoff, party),
                "format_version": 1,
            }
            con.execute(
                "INSERT INTO statements VALUES(?,?,?)",
                (statement_id, canonical(payload), digest(payload)),
            )
            self.db.audit(
                "statement_frozen",
                statement_id,
                {"digest": digest(payload), "party": party, "cutoff": cutoff},
            )
        return self.get(statement_id)

    def get(self, statement_id):
        row = self.db.one("SELECT * FROM statements WHERE id=?", (statement_id,))
        if row is None:
            error("missing", "statement not found")
        payload = decode(row["payload"])
        if digest(payload) != row["digest"]:
            error("integrity", "statement digest mismatch")
        return {**payload, "digest": row["digest"]}

    def compare(self, statement_id):
        statement = self.get(statement_id)
        current = self.aging.report(statement["cutoff"], statement["party"]["id"])
        old = {row["id"]: row for row in statement["aging"]["invoices"]}
        new = {row["id"]: row for row in current["invoices"]}
        changes = []
        for key in sorted(old.keys() | new.keys()):
            if old.get(key) != new.get(key):
                changes.append(
                    {"invoice": key, "before": old.get(key), "after": new.get(key)}
                )
        current_unapplied = self.aging.unapplied(
            statement["cutoff"], statement["party"]["id"]
        )
        return {
            "statement": statement_id,
            "invoice_changes": changes,
            "unapplied_changed": current_unapplied != statement["unapplied"],
            "unchanged": not changes and current_unapplied == statement["unapplied"],
        }

    def csv(self, statement_id):
        statement = self.get(statement_id)
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator=chr(10))
        writer.writerow(
            ["invoice", "currency", "issued", "due", "reference", "open", "bucket"]
        )
        for row in statement["aging"]["invoices"]:
            # Spreadsheet consumers must not interpret source references as formulas.
            reference = row["reference"]
            if reference.startswith(("=", "+", "-", "@", "\t", "\r")):
                reference = "'" + reference
            writer.writerow(
                [
                    row["id"],
                    row["currency"],
                    row["issued"],
                    row["due"],
                    reference,
                    row["open"],
                    row["bucket"],
                ]
            )
        return output.getvalue()

    def export(self, statement_id, path):
        payload = self.get(statement_id)
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
        return {"path": str(target), "digest": payload["digest"], "bytes": len(content)}

    @staticmethod
    def verify(path):
        path = Path(path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 20_000_000:
            error("path", "invalid statement file")
        payload = decode(path.read_text("utf-8"))
        if not isinstance(payload, dict) or "digest" not in payload:
            error("integrity", "statement envelope missing digest")
        expected = payload.pop("digest")
        if digest(payload) != expected:
            error("integrity", "exported statement differs")
        if payload.get("format_version") != 1:
            error("integrity", "unsupported statement version")
        return {"id": payload["id"], "digest": expected, "verified": True}
