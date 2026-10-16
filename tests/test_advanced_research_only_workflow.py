"""Exercise dispatch conditions against normal, legacy and research-only runs.

These tests evaluate the workflow's actual condition text. They do not launch
Actions or replace the required cloud regression, native restore or save.
"""
import ast
from pathlib import Path
import re
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
from unittest import mock
import os
import json


ROOT = Path(__file__).parents[1]
WORKFLOW = (ROOT / ".github/workflows/advanced-models.yml").read_text()
PREPARATION = (
    "club_schedule_collection", "pilot_collection", "collection",
    "player_load_collection", "facts", "confirmation", "capture", "scoring",
)
LEGACY = (
    "legacy_capture_v1", "legacy_capture_v2", "legacy_scoring_v1", "legacy_scoring_v2",
)


def job(name):
    return re.split(r"(?m)^  [a-z_]+:\n", WORKFLOW.split("  " + name + ":\n", 1)[1], 1)[0]


def step(body, identity=None, name=None):
    for block in re.split(r"(?m)^      - ", body)[1:]:
        if ((identity and "\n        id: " + identity + "\n" in block) or
                (name and block.startswith("name: " + name + "\n"))):
            return block
    raise AssertionError((identity, name))


def condition(block, indent="        "):
    match = re.search(r"(?m)^" + indent + r"if: (.+)\n", block)
    if not match:
        return "true"
    value = match.group(1)
    if value == ">-":
        lines = []
        for line in block[match.end():].splitlines():
            if not line.startswith(indent + "  "):
                break
            lines.append(line.strip())
        value = " ".join(lines)
    return value


def namespace(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{key: namespace(item) for key, item in value.items()})
    return value


def evaluate(expression, context):
    expression = expression.removeprefix("${{").removesuffix("}}").strip()
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", " not ", expression)
    expression = re.sub(r"\btrue\b", "True", expression)
    expression = re.sub(r"\bfalse\b", "False", expression)
    # Preserve the string literals used for Actions environment variables.
    expression = expression.replace("'True'", "'true'").replace("'False'", "'false'")
    parsed = ast.parse(expression.strip(), mode="eval")
    return bool(eval(compile(parsed, "<workflow condition>", "eval"),
                     {"__builtins__": {}}, {**{k: namespace(v) for k, v in context.items()},
                                           "cancelled": lambda: False,
                                           "hashFiles": lambda _: ""}))


def context(event="workflow_dispatch", *, research_only=False, main=True,
            schedule="", private=True, repository="wuyan0231-hub/football-model"):
    value = {
        "github": {"event_name": event, "repository": repository,
                   "ref": "refs/heads/" + ("main" if main else "review"),
                   "event": {"repository": {"private": private}, "schedule": schedule}},
        "inputs": {"research_only": research_only}, "vars": {"ENABLE_SCHEDULE": "true"},
        "needs": {"verify": {"result": "success"},
                  "pre_study_capture": {"result": "success", "outputs": {
                      "restore_outcome": "success", "persist_outcome": "success"}},
                  "advanced": {"outputs": {"restore_outcome": "success", "persist_outcome": "success"}},
                  "study_leagues": {"result": "success"}},
        "steps": {identity: {"outcome": "success"} for identity in (
            "restore", "durable", "check", "persist", "research", "finalize", *PREPARATION, *LEGACY)},
    }
    env = job("advanced").split("    env:\n", 1)[1].split("    steps:\n", 1)[0]
    value["env"] = {}
    for key in ("IS_RELEASE", "STUDY_RUN", "LEGACY_SHORT_RUN", "RESEARCH_ONLY"):
        expression = re.search(r"(?m)^      " + key + r": (.+)$", env).group(1)
        value["env"][key] = str(evaluate(expression, value)).lower()
    return value


class ResearchOnlyWorkflowTests(unittest.TestCase):
    def test_input_is_optional_boolean_default_false(self):
        dispatch = WORKFLOW.split("  workflow_dispatch:\n", 1)[1].split("  schedule:\n", 1)[0]
        self.assertRegex(dispatch, r"research_only:\n[\s\S]*required: false\n[\s\S]*type: boolean\n[\s\S]*default: false\n")

    def test_only_explicit_manual_dispatch_skips_preparation(self):
        for event, flag, main, schedule, expected_pre, expected_collect, expected_facts, expected_legacy in (
            ("workflow_dispatch", False, True, "", True, True, True, False),
            ("workflow_dispatch", True, True, "", False, False, False, False),
            ("push", True, True, "", True, True, True, False),
            ("schedule", True, True, "7 2 * * *", True, True, True, False),
            ("schedule", True, True, "10,40 * * * *", False, False, True, True),
            ("workflow_dispatch", False, False, "", False, False, False, False),
        ):
            with self.subTest(event=event, research_only=flag, main=main, schedule=schedule):
                value = context(event, research_only=flag, main=main, schedule=schedule)
                self.assertEqual(evaluate(condition(job("pre_study_capture"), "    "), value), expected_pre)
                for identity in PREPARATION[:4]:
                    self.assertEqual(evaluate(condition(step(job("advanced"), identity)), value), expected_collect)
                for identity in PREPARATION[4:]:
                    self.assertEqual(evaluate(condition(step(job("advanced"), identity)), value), expected_facts)
                for identity in LEGACY:
                    self.assertEqual(evaluate(condition(step(job("advanced"), identity)), value), expected_legacy)

    def test_research_only_retains_complete_verify_and_real_state_work(self):
        value = context(research_only=True)
        value["needs"]["pre_study_capture"]["result"] = "skipped"
        self.assertTrue(evaluate(condition(job("verify"), "    "), value))
        self.assertIn("python -m unittest discover -s tests -v", job("verify"))
        self.assertIn("r-base-core r-cran-jsonlite", job("verify"))
        self.assertTrue(evaluate(condition(job("advanced"), "    "), value))
        for identity, command in (
            ("restore", "python scripts/cloud_state.py load --runtime runtime"),
            ("durable", "python -m football_model.advanced_durability"),
            ("check", "python scripts/cloud_state.py check --runtime runtime"),
            ("persist", "python scripts/cloud_state_save_once.py --runtime runtime"),
        ):
            block = step(job("advanced"), identity)
            self.assertTrue(evaluate(condition(block), value), identity)
            self.assertIn(command, block)
        for failed in ("failure", "cancelled", "skipped"):
            value["needs"]["verify"]["result"] = failed
            self.assertFalse(evaluate(condition(job("advanced"), "    "), value))
        value["steps"]["check"]["outcome"] = "failure"
        self.assertFalse(evaluate(condition(step(job("advanced"), "persist")), value))

    def test_failure_gate_ignores_only_intentionally_omitted_steps(self):
        gate = condition(step(job("advanced"), name="工程失败保持失败"))
        for only in (False, True):
            with self.subTest(research_only=only):
                value = context(research_only=only)
                if only:
                    value["needs"]["pre_study_capture"]["result"] = "skipped"
                    for identity in PREPARATION:
                        value["steps"][identity]["outcome"] = "skipped"
                self.assertFalse(evaluate(gate, value))
                for identity in ("restore", "durable", "check", "persist"):
                    for outcome in ("failure", "skipped"):
                        with self.subTest(identity=identity, outcome=outcome):
                            value["steps"][identity]["outcome"] = outcome
                            self.assertTrue(evaluate(gate, value))
                            value["steps"][identity]["outcome"] = "success"
                if not only:
                    for identity in PREPARATION:
                        value["steps"][identity]["outcome"] = "skipped"
                        self.assertTrue(evaluate(gate, value), identity)
                        value["steps"][identity]["outcome"] = "success"
                value["needs"]["pre_study_capture"]["result"] = "failure"
                self.assertTrue(evaluate(gate, value))

    def test_legacy_and_engineering_branch_failures_still_fail(self):
        gate = condition(step(job("advanced"), name="工程失败保持失败"))
        legacy = context("schedule", research_only=True, schedule="10,40 * * * *")
        self.assertFalse(evaluate(gate, legacy))
        for identity in LEGACY:
            legacy["steps"][identity]["outcome"] = "failure"
            self.assertTrue(evaluate(gate, legacy))
            legacy["steps"][identity]["outcome"] = "success"
        branch = context(main=False)
        branch["steps"]["research"]["outcome"] = "failure"
        self.assertTrue(evaluate(gate, branch))

    def test_league_and_final_jobs_require_actual_saved_parent(self):
        for only in (False, True):
            for name in ("study_leagues", "study_finalize"):
                value = context(research_only=only)
                self.assertTrue(evaluate(condition(job(name), "    "), value))
                for identity in ("restore_outcome", "persist_outcome"):
                    value["needs"]["advanced"]["outputs"][identity] = "failure"
                    self.assertFalse(evaluate(condition(job(name), "    "), value))
                    value["needs"]["advanced"]["outputs"][identity] = "success"
        self.assertIn("--checkpoint-state --repo .", job("study_leagues"))
        self.assertIn("max-parallel: 1", job("study_leagues"))
        self.assertIn("--resume-only", job("study_finalize"))
        final_gate = condition(step(job("study_finalize"), name="未完成与执行失败保持可见"))
        value = context(research_only=True)
        self.assertFalse(evaluate(final_gate, value))
        for identity in ("finalize", "confirmation", "persist"):
            value["steps"][identity]["outcome"] = "failure"
            self.assertTrue(evaluate(final_gate, value))
            value["steps"][identity]["outcome"] = "success"
        value["needs"]["study_leagues"]["result"] = "failure"
        self.assertTrue(evaluate(final_gate, value))

    def test_receipt_records_skip_without_promoting_old_collection(self):
        block = step(job("advanced"), name="保存逐步骤事实回执")
        script = textwrap.dedent(block.split("          python - <<'PY'\n", 1)[1].split("          PY", 1)[0])
        value = context(research_only=True)
        env = {**value["env"], **{key.upper(): entry["outcome"] for key, entry in value["steps"].items()},
               "PRE_STUDY_CAPTURE": "skipped", "RESEARCH": "skipped"}
        env.update({identity.upper(): "skipped" for identity in PREPARATION})
        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            try:
                os.chdir(directory)
                stale = Path("runtime/advanced_features/collection/by_selection/all/latest.json")
                stale.parent.mkdir(parents=True)
                stale.write_text(json.dumps({"full_collection_completed": True}))
                env["GITHUB_STEP_SUMMARY"] = str(Path(directory) / "summary")
                with mock.patch.dict(os.environ, env):
                    exec(compile(script, "<actual workflow receipt>", "exec"), {})
                receipt = json.loads(Path("advanced-run-receipt.json").read_text())
                self.assertEqual(receipt["RESEARCH_ONLY"], "true")
                self.assertEqual(receipt["COLLECTION"], "skipped")
                self.assertEqual(receipt["PRE_STUDY_CAPTURE"], "skipped")
                self.assertFalse(receipt["full_collection_completed"])
                self.assertFalse(receipt["research_full_completed"])
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
