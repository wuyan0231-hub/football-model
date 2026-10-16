"""Keep the complete current framework and its entry points aligned."""
from pathlib import Path
import hashlib
import json
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RuleVersionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.framework = (ROOT / "docs/football-framework-v1.7.md").read_text(encoding="utf-8")
        cls.revision = re.search(r"football-analysis-v1\.7-r(\d+)", cls.framework).group(1)

    def test_current_entry_points_match_rule_header_without_pinning_reading_spec(self):
        for filename, separator in (("AGENTS.md", "## 每场必须实际执行"),
                                    ("docs/model-access.md", "## 为什么"),
                                    ("docs/model-integration.md", "## 先读接入"),
                                    ("README.md", "## ")):
            intro = (ROOT / filename).read_text(encoding="utf-8").split(separator)[0]
            self.assertIn(f"v1.7 修订批次{self.revision}", intro)
            self.assertIn("唯一现行《赔率截图读取规范》", intro)
            self.assertNotRegex(intro, r"v1\.2.*修订批次[0-9]")

    def test_complete_body_has_all_execution_and_preservation_sections(self):
        sections = re.findall(r"(?m)^## (\d+)\. ", self.framework)
        self.assertEqual(sections, [str(i) for i in range(1, 19)])
        for requirement in ("S00", "S11", "S13", "原始首输出", "四份台账", "prepared_not_active"):
            self.assertIn(requirement, self.framework)

    def test_publish_gate_and_f5c_access_preserve_distinct_qualification(self):
        gate = self.framework.split("### 10.1 ", 1)[1].split("### 10.2 ", 1)[0]
        for key in ("G1", "G2", "G3", "G4", "G5"):
            self.assertIn(key, gate)
        f5c = self.framework.split("### 6.5 ", 1)[1].split("## 7. ", 1)[0]
        for requirement in ("同一生产run", "α=0", "完整原预测行", "pending_state_commit_verification", "晚于截点"):
            self.assertIn(requirement, f5c)
        self.assertIn("整个协议周期的容量及持久恢复", self.framework)

    def test_r13_parent_rules_are_frozen_and_every_nonempty_unit_is_accounted_for(self):
        record = json.loads((ROOT / "docs/evidence/advanced-models-rule-inheritance-r13-20261016.json").read_text())
        self.assertEqual(record["framework_version"], "football-analysis-v1.7-r" + self.revision)
        self.assertEqual(record["direct_parent_framework_version"], "football-analysis-v1.7-r12")
        self.assertEqual(record["unaccounted_deleted_units"], 0)
        parents = {}
        for entry in record["parent_documents"]:
            raw = (ROOT / entry["frozen_path"]).read_bytes()
            self.assertEqual(len(raw), entry["bytes"])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), entry["sha256"])
            self.assertEqual(hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest(), entry["git_blob_sha"])
            parents[entry["path"]] = raw.decode().splitlines()
        units = {(u["document"], u["parent_line"]): u for u in record["requirement_units"]}
        self.assertEqual(len(units), record["requirement_unit_count"])
        for path, lines in parents.items():
            current = (ROOT / path).read_text().splitlines()
            for n, line in enumerate(lines, 1):
                if not line.strip():
                    continue
                item = units[(path, n)]
                self.assertEqual(hashlib.sha256(line.encode()).hexdigest(), item["parent_sha256"])
                self.assertIn(item["status"], ("preserved_exact", "explicit_replacement"))
                self.assertTrue(item["current_lines"])
                actual = [hashlib.sha256(current[i - 1].encode()).hexdigest() for i in item["current_lines"]]
                self.assertEqual(actual, item["current_sha256"])
                if item["status"] == "preserved_exact":
                    self.assertTrue(all(current[i - 1] == line for i in item["current_lines"]))
                else:
                    self.assertTrue(item["reason"].strip())

    def test_current_document_fingerprints_and_original_protocols_match_manifest(self):
        record = json.loads((ROOT / "docs/evidence/advanced-models-rule-inheritance-r13-20261016.json").read_text())
        entries = record["current_documents"] + list(record["unchanged_protocols"].values())
        entries += [record["goal_matrix"], record["handoff"], record["unchanged_independent_odds"]["repository_file"]]
        for entry in entries:
            self.assertEqual(hashlib.sha256((ROOT / entry["path"]).read_bytes()).hexdigest(), entry["sha256"], entry["path"])
        self.assertFalse(record["unchanged_independent_odds"]["modified_by_this_revision"])
        previous = json.loads((ROOT / "docs/evidence/advanced-models-rule-inheritance-r12-20261013.json").read_text())
        for path, entry in record["unchanged_protocols"].items():
            self.assertEqual(entry["sha256"], previous["unchanged_protocols"][path]["sha256"], path)

    def test_full_goals_and_unfinished_personnel_scope_are_not_downgraded(self):
        matrix = json.loads((ROOT / "docs/evidence/s3-r13-goal-status-20261016.json").read_text())
        goals = {g["goal_id"]: g for g in matrix["goals"]}
        self.assertTrue({f"ORIGINAL-{n}" for n in range(1, 7)}.issubset(goals))
        self.assertIn("PERSONNEL-FULL", goals)
        self.assertTrue(goals["PERSONNEL-FULL"]["remaining_class"])
        self.assertEqual(goals["PANNA"]["code_status"], "deferred_by_user")
        protocol = json.loads((ROOT / "advanced-models-v1.json").read_text())
        design = json.loads((ROOT / "advanced-confirmation-v1.json").read_text())
        per_league = len(protocol["candidates"]) + len(protocol["goalmodel_candidates"]) * len(protocol["goalmodel_horizons"])
        self.assertEqual(matrix["counts"]["family_size"], per_league * len(design["registered_leagues"]))
        self.assertEqual(matrix["counts"]["family_size"], 380)
        self.assertFalse(matrix["new_cloud_execution"]["numerical_effect_claim"])
        self.assertFalse(matrix["new_cloud_execution"]["roi_claim"])


if __name__ == "__main__":
    unittest.main()
