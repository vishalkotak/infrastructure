import subprocess
import unittest
from unittest.mock import patch

import install


class InstallerCredentials(unittest.TestCase):
    def test_live_dispatch_enables_publication(self):
        new_run = {"created_at": "9999-01-01T00:00:00Z", "html_url": "https://github.com/test/run"}
        with patch("install.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run, \
                patch("install.gh", return_value={"workflow_runs": [new_run]}):
            install.dispatch_run("main", apply=True)
        self.assertTrue(json_payload_apply(run.call_args.kwargs["input"]))

    def test_installer_never_reads_or_enables_alerts_on_forks(self):
        repos = [{"full_name": f"vishalkotak/{name}", "owner": {"login": "vishalkotak"}, **metadata}
                 for name, metadata in [("original", {"fork": False}), ("forked", {"fork": True}),
                                        ("unknown", {})]]
        replies = [{"login": "vishalkotak"}, [repos], []]
        with patch("install.credential_api", side_effect=replies) as api, patch("install.verify_controller_client"):
            install.verify_alert_access("private")
        self.assertEqual(api.call_count, 3)
        self.assertIn("repos/vishalkotak/original/dependabot/alerts", api.call_args.args[1])

    def test_waits_for_enabled_alerts_to_become_readable(self):
        delayed = install.AlertAccessError("HTTP 403: Dependabot alerts are disabled for this repository.")
        with patch("install.credential_api", side_effect=[delayed, delayed, []]) as api, \
                patch("install.time.sleep") as sleep:
            self.assertEqual(install.wait_for_alerts("private", "repos/x/y/dependabot/alerts"), [])
        self.assertEqual(api.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_wait_does_not_retry_permission_denial(self):
        with patch("install.credential_api", side_effect=install.AlertAccessError("HTTP 403: Resource not accessible")) as api, \
                patch("install.time.sleep") as sleep:
            with self.assertRaises(install.AlertAccessError):
                install.wait_for_alerts("private", "repos/x/y/dependabot/alerts")
        self.assertEqual(api.call_count, 1)
        sleep.assert_not_called()

    def test_wait_stops_after_bound_without_claiming_success(self):
        with patch("install.credential_api", side_effect=install.AlertAccessError("HTTP 403: alerts disabled")) as api, \
                patch("install.time.sleep"):
            with self.assertRaises(install.AlertAccessError):
                install.wait_for_alerts("private", "repos/x/y/dependabot/alerts", attempts=3)
        self.assertEqual(api.call_count, 3)

    def test_transient_dispatch_error_retries_without_reinstalling(self):
        replies = [subprocess.CompletedProcess([], 1, "", "HTTP 500"),
                   subprocess.CompletedProcess([], 0, "", "")]
        new_run = {"created_at": "9999-01-01T00:00:00Z", "html_url": "https://github.com/test/run"}
        with patch("install.subprocess.run", side_effect=replies) as run, \
                patch("install.gh", side_effect=[{"workflow_runs": []}, {"workflow_runs": [new_run]}]), \
                patch("install.time.sleep"):
            result = install.dispatch_run("main")
        self.assertEqual(result, new_run)
        self.assertEqual(run.call_count, 2)
        self.assertFalse(json_payload_apply(run.call_args.kwargs["input"]))

    def test_dispatch_error_with_created_run_does_not_duplicate(self):
        new_run = {"created_at": "9999-01-01T00:00:00Z", "html_url": "https://github.com/test/run"}
        with patch("install.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "HTTP 500")) as run, \
                patch("install.gh", return_value={"workflow_runs": [new_run]}), \
                patch("install.time.sleep"):
            result = install.dispatch_run("main")
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

    def test_normalized_repo_scope_does_not_request_redundant_authorization(self):
        reply = subprocess.CompletedProcess([], 0, "HTTP/2 200\nx-oauth-scopes: repo, workflow\n\n{}", "")
        with patch("install.subprocess.run", return_value=reply) as run:
            install.ensure_workflow_scope()
        self.assertEqual(run.call_count, 1)

    def test_alert_access_failure_never_overwrites_secret(self):
        with patch("install.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "private\n", "")) as run, \
                patch("install.verify_alert_access", side_effect=install.AlertAccessError("HTTP 403")):
            with self.assertRaises(SystemExit):
                install.save_scheduled_token()
        self.assertEqual(run.call_count, 1)

    def test_explicitly_disabled_alerts_are_enabled_and_verified(self):
        replies = [{"login": "vishalkotak"},
                   [[{"full_name": "vishalkotak/a", "owner": {"login": "vishalkotak"}, "fork": False}]],
                   install.AlertAccessError("HTTP 403: Dependabot alerts are disabled"), None, []]
        with patch("install.credential_api", side_effect=replies) as api, patch("install.verify_controller_client"):
            install.verify_alert_access("private")
        self.assertEqual(api.call_args_list[-2].kwargs["method"], "PUT")

    def test_permission_denial_does_not_try_to_enable_alerts(self):
        replies = [{"login": "vishalkotak"},
                   [[{"full_name": "vishalkotak/a", "owner": {"login": "vishalkotak"}, "fork": False}]],
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
