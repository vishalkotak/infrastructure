import unittest

import poetry_security as security


class PoetryRemediation(unittest.TestCase):
    def test_frozen_requirements_have_only_validated_package_pins(self):
        lock = {"package": [{"name": "Some_Package", "version": "1.2.3"}]}
        self.assertEqual(security.requirements(lock), "some-package==1.2.3\n")
        for value in ["1.2\n--extra-index-url=https://evil.example", "file:///etc/passwd"]:
            with self.assertRaises(ValueError):
                security.requirements({"package": [{"name": "safe", "version": value}]})

    def test_every_advisory_must_be_satisfied_by_the_chosen_minimum(self):
        audit = {"dependencies": [{"name": "filelock", "vulns": [
            {"id": "one", "fix_versions": ["4.0.9", "5.0.0rc1"]},
            {"id": "two", "fix_versions": ["4.2.1"]}]}]}
        self.assertEqual(security.patched_versions(audit), {"filelock": "4.2.1"})

    def test_unpatched_findings_block_remediation(self):
        audit = {"dependencies": [{"name": "unsafe", "vulns": [{"id": "one", "fix_versions": []}]}]}
        with self.assertRaises(ValueError):
            security.patched_versions(audit)

    def test_only_ancestors_of_vulnerable_dependencies_are_selected(self):
        manifest = {"tool": {"poetry": {"dependencies": {"python": "^3.12", "fastapi": "^0.1", "requests": "*"}}}}
        lock = {"package": [{"name": "fastapi", "dependencies": {"starlette": "<1"}},
                            {"name": "requests", "dependencies": {"urllib3": "*"}}]}
        self.assertEqual(security.affected_parents(lock, manifest, {"starlette": "1.3.1"}), {"fastapi": None})

    def test_dependency_changes_allowed_but_scripts_and_dependency_removal_rejected(self):
        before = {"tool": {"poetry": {"name": "app", "dependencies": {"fastapi": "^0.1"}}}}
        after = {"tool": {"poetry": {"name": "app", "dependencies": {"fastapi": ">=1"},
                   "group": {"security": {"dependencies": {"starlette": ">=1.3.1"}}}}}}
        self.assertTrue(security.allowed_manifest_change(before, after))
        after["tool"]["poetry"]["scripts"] = {"surprise": "evil:main"}
        self.assertFalse(security.allowed_manifest_change(before, after))
        self.assertFalse(security.allowed_manifest_change(before, {"tool": {"poetry": {"name": "app"}}}))


if __name__ == "__main__":
    unittest.main()
