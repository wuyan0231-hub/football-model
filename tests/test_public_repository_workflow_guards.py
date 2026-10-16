"""Visibility-independent execution stays bound to the source repository."""
from pathlib import Path
import re
import unittest

from tests.test_advanced_research_only_workflow import condition, context, evaluate


ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github/workflows"
OWNER_REPOSITORY = "wuyan0231-hub/football-model"
GUARDED = {
    "advanced-models.yml": ("verify", "pre_study_capture", "advanced"),
    "evidence-artifact-export.yml": ("export",),
    "feature-models.yml": ("features",),
    "fixture-recovery.yml": ("recover",),
    "football-state-verify.yml": ("verify",),
    "football-total-shrink.yml": ("total-shrink-shadow",),
    "football.yml": ("football",),
    "fundamentals.yml": ("fundamentals",),
    "unl-probability-repair-verification.yml": ("verify",),
}


def workflow(name):
    return (WORKFLOWS / name).read_text()


def job(name, identity):
    return re.split(r"(?m)^  [a-z_-]+:\n", workflow(name).split("  " + identity + ":\n", 1)[1], 1)[0]


def job_condition(name, identity):
    return condition(job(name, identity), "    ")


def allowed_context(name, private=True, repository=OWNER_REPOSITORY):
    value = context(private=private, repository=repository)
    if name == "unl-probability-repair-verification.yml":
        value["github"]["ref"] = "refs/heads/codex/recovery-20261014"
    return value


class PublicRepositoryWorkflowGuardTests(unittest.TestCase):
    def test_source_repository_is_allowed_in_both_visibility_states(self):
        for name, identities in GUARDED.items():
            for identity in identities:
                for private in (True, False):
                    with self.subTest(workflow=name, job=identity, private=private):
                        self.assertTrue(evaluate(job_condition(name, identity), allowed_context(name, private)))

    def test_forks_and_other_repositories_cannot_run_source_jobs(self):
        for name, identities in GUARDED.items():
            for identity in identities:
                expression = job_condition(name, identity)
                self.assertIn("github.repository == '" + OWNER_REPOSITORY + "'", expression)
                for repository in ("another-owner/football-model", "wuyan0231-hub/another-repo"):
                    for private in (True, False):
                        with self.subTest(workflow=name, job=identity, repository=repository, private=private):
                            self.assertFalse(evaluate(expression, allowed_context(name, private, repository)))
        for path in WORKFLOWS.glob("*.yml"):
            self.assertNotIn("repository.private", path.read_text(), path.name)

    def test_existing_branch_and_manual_guards_remain_effective(self):
        for name, identity, required_ref in (
            ("fixture-recovery.yml", "recover", "refs/heads/main"),
            ("unl-probability-repair-verification.yml", "verify", "refs/heads/codex/recovery-20261014"),
            ("advanced-models.yml", "pre_study_capture", "refs/heads/main"),
        ):
            value = allowed_context(name, private=False)
            self.assertIn("github.ref == '" + required_ref + "'", job_condition(name, identity))
            value["github"]["ref"] = "refs/heads/unapproved-branch"
            self.assertFalse(evaluate(job_condition(name, identity), value), name)
        value = allowed_context("fundamentals.yml", private=False)
        value["github"]["event_name"] = "push"
        self.assertFalse(evaluate(job_condition("fundamentals.yml", "fundamentals"), value))

    def test_schedules_still_require_enable_variable_and_keep_original_times(self):
        for name, identity in (
            ("advanced-models.yml", "verify"), ("advanced-models.yml", "pre_study_capture"),
            ("advanced-models.yml", "advanced"), ("feature-models.yml", "features"),
            ("football.yml", "football"),
        ):
            value = context("schedule", private=False, schedule="7 2 * * *")
            self.assertTrue(evaluate(job_condition(name, identity), value), (name, identity))
            value["vars"]["ENABLE_SCHEDULE"] = "false"
            self.assertFalse(evaluate(job_condition(name, identity), value), (name, identity))
        self.assertIn("- cron: '17 1 * * *'", workflow("football.yml"))
        self.assertIn("- cron: '47 1 * * *'", workflow("feature-models.yml"))
        self.assertIn("- cron: '10,40 * * * *'", workflow("advanced-models.yml"))
        self.assertIn("- cron: '7 2 * * *'", workflow("advanced-models.yml"))

    def test_secret_workflows_use_only_trusted_events_and_pr_tests_stay_read_only(self):
        for name in GUARDED:
            text = workflow(name)
            self.assertNotIn("pull_request_target", text, name)
            trigger = text.split("\non:\n", 1)[1].split("\npermissions:\n", 1)[0]
            events = set(re.findall(r"(?m)^  ([a-z_]+):", trigger))
            self.assertTrue(events <= {"push", "schedule", "workflow_dispatch"}, (name, events))
            self.assertNotIn("pull_request:", trigger, name)
            # No permission was added for making the source repository public.
            self.assertNotIn("write-all", text, name)
            self.assertNotIn("id-token:", text, name)
        tests = workflow("tests.yml")
        self.assertIn("  pull_request:\n", tests)
        self.assertIn("permissions:\n  contents: read\n", tests)
        self.assertNotIn("secrets.", tests)
        self.assertNotIn("pull_request_target", tests)
        self.assertNotRegex(tests, r"(?m)^  [a-z_-]+: write$")


if __name__ == "__main__":
    unittest.main()
