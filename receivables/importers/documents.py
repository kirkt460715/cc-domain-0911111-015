import csv
import io
from ..primitives import (
    identifier,
    obj,
    sequence,
    canonical,
    digest,
    decode,
    error,
    BookError,
)


class DocumentImporter:
    def __init__(self, database, ledger):
        self.db = database
        self.ledger = ledger

    def parse(self, content, format_name="jsonl"):
        if not isinstance(content, str) or len(content.encode()) > 16000000:
            error("input", "document import exceeds supported bytes")
        if format_name == "jsonl":
            result = []
            for line, text in enumerate(content.splitlines(), 1):
                if text.strip():
                    try:
                        result.append(decode(text))
                    except BookError as exc:
                        error(
                            "input",
                            "malformed document row",
                            line=line,
                            error=exc.as_dict(),
                        )
        elif format_name == "csv":
            reader = csv.DictReader(io.StringIO(content, newline=""), strict=True)
            if not reader.fieldnames or len(reader.fieldnames) != len(
                set(reader.fieldnames)
            ):
                error("input", "CSV header missing or duplicate")
            result = []
            try:
                for row in reader:
                    if None in row or any(value is None for value in row.values()):
                        error("input", "CSV row width differs")
                    if not row.get("due"):
                        row.pop("due", None)
                    result.append(row)
            except csv.Error as exc:
                error("input", "CSV syntax error", reason=str(exc))
        else:
            error("input", "format must be jsonl or csv")
        sequence(result, 1, 100000)
        return result

    def import_batch(self, batch_id, documents):
        identifier(batch_id)
        sequence(documents, 1, 100000)
        request = digest(documents)
        with self.db.transaction() as con:
            previous = self.db.one("SELECT * FROM batches WHERE id=?", (batch_id,))
            if previous:
                if previous["digest"] != request:
                    error("conflict", "batch identity differs")
                return {**decode(previous["result"]), "reused": True}
            seen = set()
            posted = []
            for row in documents:
                obj(row, required={"id"})
                if row["id"] in seen:
                    error("input", "duplicate document in import batch")
                seen.add(row["id"])
                posted.append(self.ledger.post(row))
            result = {
                "id": batch_id,
                "posted": sum(not item["existing"] for item in posted),
                "existing": sum(item["existing"] for item in posted),
                "documents": [item["id"] for item in posted],
                "reused": False,
            }
            con.execute(
                "INSERT INTO batches VALUES(?,?,?)",
                (batch_id, request, canonical(result)),
            )
            self.db.audit("batch_imported", batch_id, result)
        return result
