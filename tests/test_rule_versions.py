"""Keep current rule entry points aligned without rewriting revision history."""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RuleVersionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.framework = (ROOT / "docs/football-framework-v1.7.md").read_text(encoding="utf-8")
        cls.images = (ROOT / "docs/odds-screenshot-reading-v1.2.md").read_text(encoding="utf-8")
        cls.framework_revision = re.search(r"版本：v1\.7；修订批次：(\d+)", cls.framework).group(1)
        cls.image_revision = re.search(r"^# .*v1\.2（修订批次(\d+)）", cls.images).group(1)

    def test_current_entry_points_match_rule_headers(self):
        framework_rev = self.framework_revision
        image_rev = self.image_revision
        self.assertIn(f"当前正文为v1.2修订批次{image_rev}", self.framework.split("## 零、")[0])
        self.assertIn(f"当前配套：《足球盘口分析框架》v1.7 修订批次{framework_rev}", self.images.split("## 0.")[0])

        agents_intro = (ROOT / "AGENTS.md").read_text(encoding="utf-8").split("## 每场必须实际执行")[0]
        access_intro = (ROOT / "docs/model-access.md").read_text(encoding="utf-8").split("## 为什么")[0]
        for intro in (agents_intro, access_intro):
            self.assertIn(f"v1.7 修订批次{framework_rev}", intro)
            self.assertIn(f"v1.2 修订批次{image_rev}", intro)

    def test_publish_gate_and_example_use_current_versions(self):
        publish_check = self.framework.split("### 21.4 ", 1)[1].split("### 21.5 ", 1)[0]
        self.assertIn(f"v1.7批次{self.framework_revision}", publish_check)
        self.assertIn(f"v1.2批次{self.image_revision}", publish_check)
        invocation = self.framework.split("### 21.3 ", 1)[1].split("### 21.4 ", 1)[0]
        self.assertIn(f"v1.7修订批次{self.framework_revision}执行", invocation)
        self.assertIn(f"v1.2修订批次{self.image_revision}处理", invocation)
        sample = self.framework.split("### 25.2 ", 1)[1].split("### 25.3 ", 1)[0]
        self.assertIn(f'"framework": "v1.7-r{self.framework_revision}"', sample)
        self.assertIn(f'"image_rules": "v1.2-r{self.image_revision}', sample)


if __name__ == "__main__":
    unittest.main()
