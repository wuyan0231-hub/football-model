"""Keep the complete current framework and its entry points aligned."""
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
