import csv
import io
from datetime import datetime
from ..primitives import (
    identifier,
    obj,
    error,
    digest,
    canonical,
    decode,
    money,
    display,
    currency,
)


class ImportProfiles:
    def __init__(self, database, documents, bank):
        self.db = database
        self.documents = documents
        self.bank = bank
        self.db.connection.executescript("""
CREATE TABLE IF NOT EXISTS import_profiles(id TEXT PRIMARY KEY,version INTEGER NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS import_runs(id TEXT PRIMARY KEY,profile TEXT NOT NULL,profile_version INTEGER NOT NULL,digest TEXT NOT NULL,result TEXT NOT NULL);
""")

    def save(
        self,
        profile_id,
        target,
        mapping,
        date_format="%Y-%m-%d",
        decimal_mark=".",
        expected_version=0,
    ):
        identifier(profile_id)
        if target not in {"documents", "bank"}:
            error("input", "profile target must be documents or bank")
        obj(mapping)
        required = (
            {"id", "kind", "party", "currency", "amount", "issued", "reference"}
            if target == "documents"
            else {"id", "account", "currency", "amount", "booked", "reference", "payer"}
        )
        allowed = required | ({"due"} if target == "documents" else set())
        if not required <= mapping.keys() or mapping.keys() - allowed:
            error("input", "profile fields do not match target")
        if not all(
            isinstance(value, str) and value.strip() for value in mapping.values()
        ) or len(set(mapping.values())) != len(mapping):
            error("input", "mapping requires unique nonempty column names")
        if date_format not in {"%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y%m%d"}:
            error("input", "date format not supported")
        if decimal_mark not in {".", ","}:
            error("input", "invalid decimal separator")
        payload = {
            "target": target,
            "mapping": mapping,
            "date_format": date_format,
            "decimal_mark": decimal_mark,
        }
        with self.db.transaction() as con:
            old = self.db.one("SELECT * FROM import_profiles WHERE id=?", (profile_id,))
            version = old["version"] if old else 0
            if expected_version != version:
                error("conflict", "profile version changed")
            version += 1
            con.execute(
                "INSERT INTO import_profiles VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,payload=excluded.payload,digest=excluded.digest",
                (profile_id, version, canonical(payload), digest(payload)),
            )
            self.db.audit(
                "profile_saved",
                profile_id,
                {"version": version, "digest": digest(payload)},
            )
        return self.get(profile_id)

    def get(self, profile_id):
        row = self.db.one("SELECT * FROM import_profiles WHERE id=?", (profile_id,))
        if row is None:
            error("missing", "import profile not found")
        payload = decode(row["payload"])
        if digest(payload) != row["digest"]:
            error("integrity", "profile fingerprint differs")
        return {**payload, "id": profile_id, "version": row["version"]}

    def preview(self, profile_id, content, delimiter=","):
        if not isinstance(content, str) or len(content.encode()) > 10_000_000:
            error("input", "CSV input too large or not text")
        if delimiter not in {",", ";", chr(9)}:
            error("input", "unsupported CSV delimiter")
        profile = self.get(profile_id)
        reader = csv.DictReader(io.StringIO(content), delimiter=delimiter, strict=True)
        try:
            headers = reader.fieldnames
            if not headers or len(headers) != len(set(headers)):
                error("input", "CSV headers missing or duplicated")
            missing = set(profile["mapping"].values()) - set(headers)
            if missing:
                error("input", "mapped columns missing", columns=sorted(missing))
            rows = []
            failures = []
            for index, raw in enumerate(reader, 2):
                if index > 10002:
                    error("capacity", "CSV row limit exceeded")
                try:
                    if None in raw or any(value is None for value in raw.values()):
                        error("input", "CSV row width mismatch")
                    row = {
                        target: raw[source].strip()
                        for target, source in profile["mapping"].items()
                    }
                    for field in ("issued", "due", "booked"):
                        if field not in row:
                            continue
                        if not row[field] and field == "due":
                            row.pop(field)
                            continue
                        try:
                            parsed = datetime.strptime(
                                row[field], profile["date_format"]
                            )
                        except ValueError:
                            error("input", "invalid profile date", field=field)
                        if parsed.strftime(profile["date_format"]) != row[field]:
                            error(
                                "input",
                                "date is not in exact profile format",
                                field=field,
                            )
                        row[field] = parsed.date().isoformat()
                    value = row["amount"]
                    if profile["decimal_mark"] == ",":
                        if "." in value:
                            error("input", "grouping separators are not accepted")
                        value = value.replace(",", ".")
                    elif "," in value:
                        error("input", "grouping separators are not accepted")
                    code = currency(row["currency"])
                    row["amount"] = display(money(value, code), code)
                    identifier(row["id"])
                    rows.append(row)
                except Exception as exc:
                    from ..primitives import BookError

                    if not isinstance(exc, BookError):
                        raise
                    failures.append({"line": index, "error": exc.as_dict()})
        except csv.Error as exc:
            error("input", "malformed CSV", reason=str(exc))
        ids = [row["id"] for row in rows]
        if len(ids) != len(set(ids)):
            failures.append(
                {
                    "line": None,
                    "error": {
                        "code": "duplicate",
                        "message": "duplicate document or bank line identifiers",
                    },
                }
            )
        return {
            "profile": profile_id,
            "version": profile["version"],
            "target": profile["target"],
            "rows": rows,
            "errors": failures,
            "input_digest": digest(
                {"content": content, "delimiter": delimiter, "profile": profile}
            ),
        }

    def apply(self, run_id, profile_id, content, expected_digest, delimiter=","):
        identifier(run_id)
        with self.db.transaction() as con:
            preview = self.preview(profile_id, content, delimiter)
            if preview["input_digest"] != expected_digest:
                error("conflict", "import preview no longer matches")
            if preview["errors"]:
                error(
                    "input", "import preview contains errors", errors=preview["errors"]
                )
            old = self.db.one("SELECT * FROM import_runs WHERE id=?", (run_id,))
            if old:
                if old["digest"] != expected_digest:
                    error("conflict", "import run identity differs")
                return decode(old["result"])
            if preview["target"] == "documents":
                result = self.documents.import_batch(
                    "profile:" + run_id, preview["rows"]
                )
            else:
                results = [self.bank.add(row) for row in preview["rows"]]
                result = {
                    "rows": len(results),
                    "inserted": sum(not row["existing"] for row in results),
                    "existing": sum(row["existing"] for row in results),
                }
            result = {
                **result,
                "run": run_id,
                "profile": profile_id,
                "version": preview["version"],
            }
            con.execute(
                "INSERT INTO import_runs VALUES(?,?,?,?,?)",
                (
                    run_id,
                    profile_id,
                    preview["version"],
                    expected_digest,
                    canonical(result),
                ),
            )
            self.db.audit("profile_imported", run_id, result)
        return result
