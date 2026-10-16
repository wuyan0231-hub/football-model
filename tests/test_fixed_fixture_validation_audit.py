import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone

PATH = Path(__file__).resolve().parents[1] / "scripts/fixed_fixture_validation_audit.py"
if not PATH.is_file():
    PATH = Path(__file__).with_name("fixed_fixture_validation_audit.py")
spec = importlib.util.spec_from_file_location("fixed_fixture_validation_audit", PATH)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class FixedFixtureAuditTests(unittest.TestCase):
    def test_commit_validation(self):
        self.assertEqual(audit.sha40("a" * 40), "a" * 40)
        for value in ["main", "a" * 39, "A" * 40, None, "a" * 40 + "\n"]:
            with self.subTest(value=value), self.assertRaises(audit.AuditError):
                audit.sha40(value)

    def test_path_rejections(self):
        with tempfile.TemporaryDirectory() as root:
            for value in ["../x", "a/../x", "/x", "a//x", "./x", "a\\x", "a\nx", "", None]:
                with self.subTest(value=value), self.assertRaises(audit.AuditError):
                    audit.safe_path(root, value)
            self.assertEqual(audit.safe_path(root, "a/b.json"), Path(root) / "a/b.json")

    def test_symlink_escape(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            (Path(root) / "link").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(audit.AuditError):
                audit.safe_path(root, "link/x")

    def test_cache_identity_algorithm_and_no_network(self):
        self.assertEqual(audit.cache_relative("/fixtures", {"id":1550994}),
            "cache/api_cache/68a8aad0e6f722ac575f71d925400ed5eef99e2f9eb354180e325593152c0ca5.json")
        with self.assertRaises(audit.AuditError):
            audit.cache_relative("/status", {})

    def reader(self, root):
        return audit.ReadOnlyCache(Path(root), [], datetime(2026, 1, 2, tzinfo=timezone.utc),
            lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")))

    def cache(self, root, **overrides):
        obj = {"endpoint":"/fixtures", "params":{"id":1}, "errors":[], "response":[], "fetched_at":"2026-01-01T00:00:00Z"}
        obj.update(overrides)
        path = Path(root) / audit.cache_relative("/fixtures", {"id":1})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj))
        return obj

    def test_missing_cache_fails_closed(self):
        with tempfile.TemporaryDirectory() as root, self.assertRaises(audit.AuditError):
            self.reader(root)._cached_request("/fixtures", {"id":1}, force=True)

    def test_exact_local_cache_records_raw_hash(self):
        with tempfile.TemporaryDirectory() as root:
            self.cache(root)
            reader = self.reader(root)
            result = reader._cached_request("/fixtures", {"id":1}, force=True)
            self.assertFalse(result["from_cache"])
            self.assertEqual(len(reader.evidence), 1)
            self.assertEqual(len(reader.evidence[0]["sha256"]), 64)

    def test_bad_cache_variants_fail_closed(self):
        for values in [{"params":{"id":2}}, {"endpoint":"/leagues"}, {"errors":["bad"]},
                       {"response":{}}, {"fetched_at":"2026-01-03T00:00:00Z"}]:
            with self.subTest(values=values), tempfile.TemporaryDirectory() as root:
                self.cache(root, **values)
                with self.assertRaises(audit.AuditError):
                    self.reader(root)._cached_request("/fixtures", {"id":1})

    def test_nonfinite_json_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "bad.json"
            path.write_text('{"v":NaN}')
            with self.assertRaises(audit.AuditError):
                audit.read_json(path, [])

    def test_restore_rejects_nonempty_runtime_before_git(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root); (p / "kept").write_text("original")
            with self.assertRaises(audit.AuditError):
                audit.restore_fixed(p, p, "a" * 40, None)
            self.assertEqual((p / "kept").read_text(), "original")


if __name__ == "__main__":
    unittest.main()
