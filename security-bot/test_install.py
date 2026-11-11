import subprocess
import unittest
from unittest.mock import patch

import install


class InstallerCredentials(unittest.TestCase):
    def test_transient_dispatch_error_retries_without_reinstalling(self):
        replies = [subprocess.CompletedProcess([], 1, "", "HTTP 500"),
                   subprocess.CompletedProcess([], 0, "", "")]
        new_run = {"created_at": "9999-01-01T00:00:00Z", "html_url": "https://github.com/test/run"}
        with patch("install.subprocess.run", side_effect=replies) as run, \
                patch("install.gh", side_effect=[{"workflow_runs": []}, {"workflow_runs": [new_run]}]), \
                patch("install.time.sleep"):
            result = install.dispatch_dry_run("main")
        self.assertEqual(result, new_run)
        self.assertEqual(run.call_count, 2)
        self.assertFalse(json_payload_apply(run.call_args.kwargs["input"]))

    def test_dispatch_error_with_created_run_does_not_duplicate(self):
        new_run = {"created_at": "9999-01-01T00:00:00Z", "html_url": "https://github.com/test/run"}
        with patch("install.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "HTTP 500")) as run, \
                patch("install.gh", return_value={"workflow_runs": [new_run]}), \
                patch("install.time.sleep"):
            result = install.dispatch_dry_run("main")
        self.assertEqual(result, new_run)
        self.assertEqual(run.call_count, 1)

    def test_existing_token_is_piped_to_secret_storage_not_argv(self):
        replies = [subprocess.CompletedProcess([], 0, "private-test-credential\n", ""),
                   subprocess.CompletedProcess([], 0, "", "")]
        with patch("install.subprocess.run", side_effect=replies) as run, \
                patch("install.verify_alert_access") as verify:
            install.save_scheduled_token()
        verify.assert_called_once_with("private-test-credential")
        self.assertEqual(run.call_args.kwargs["input"], "private-test-credential")
        for call in run.call_args_list:
            self.assertNotIn("private-test-credential", str(call.args))

    def test_failed_credential_read_never_attempts_secret_write(self):
        with patch("install.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "error")) as run:
            with self.assertRaises(SystemExit):
                install.save_scheduled_token()
        self.assertEqual(run.call_count, 1)

    def test_existing_workflow_scope_does_not_repeat_login(self):
        response = subprocess.CompletedProcess([], 0, "HTTP/2 200\nx-oauth-scopes: repo, workflow, security_events, gist\n\n{}", "")
        with patch("install.subprocess.run", return_value=response) as run:
            install.ensure_workflow_scope()
        self.assertEqual(run.call_count, 1)

    def test_missing_workflow_scope_refreshes_before_upload(self):
        replies = [subprocess.CompletedProcess([], 0, "HTTP/2 200\nx-oauth-scopes: repo, gist\n\n{}", ""),
                   subprocess.CompletedProcess([], 0)]
        with patch("install.subprocess.run", side_effect=replies) as run:
            install.ensure_workflow_scope()
        self.assertIn("refresh", run.call_args.args[0])
        self.assertIn("security_events", run.call_args.args[0][-1])

    def test_missing_security_scope_refreshes_even_with_workflow_scope(self):
        replies = [subprocess.CompletedProcess([], 0, "HTTP/2 200\nx-oauth-scopes: repo, workflow\n\n{}", ""),
                   subprocess.CompletedProcess([], 0)]
        with patch("install.subprocess.run", side_effect=replies) as run:
            install.ensure_workflow_scope()
        self.assertIn("security_events", run.call_args.args[0][-1])

    def test_alert_access_failure_never_overwrites_secret(self):
        with patch("install.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "private\n", "")) as run, \
                patch("install.verify_alert_access", side_effect=install.AlertAccessError("HTTP 403")):
            with self.assertRaises(SystemExit):
                install.save_scheduled_token()
        self.assertEqual(run.call_count, 1)

    def test_explicitly_disabled_alerts_are_enabled_and_verified(self):
        replies = [{"login": "vishalkotak"},
                   [[{"full_name": "vishalkotak/a", "owner": {"login": "vishalkotak"}}]],
                   install.AlertAccessError("HTTP 403: Dependabot alerts are disabled"), None, []]
        with patch("install.credential_api", side_effect=replies) as api:
            install.verify_alert_access("private")
        self.assertEqual(api.call_args_list[-2].kwargs["method"], "PUT")

    def test_permission_denial_does_not_try_to_enable_alerts(self):
        replies = [{"login": "vishalkotak"},
                   [[{"full_name": "vishalkotak/a", "owner": {"login": "vishalkotak"}}]],
                   install.AlertAccessError("HTTP 403: Resource not accessible")]
        with patch("install.credential_api", side_effect=replies) as api:
            with self.assertRaises(install.AlertAccessError):
                install.verify_alert_access("private")
        self.assertEqual(api.call_count, 3)


def json_payload_apply(payload):
    import json
    return json.loads(payload)["inputs"]["apply"]


if __name__ == "__main__":
    unittest.main()
