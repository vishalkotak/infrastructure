import subprocess
import unittest
from unittest.mock import patch

import install


class InstallerCredentials(unittest.TestCase):
    def test_existing_token_is_piped_to_secret_storage_not_argv(self):
        replies = [subprocess.CompletedProcess([], 0, "private-test-credential\n", ""),
                   subprocess.CompletedProcess([], 0, "", "")]
        with patch("install.subprocess.run", side_effect=replies) as run:
            install.save_scheduled_token()
        self.assertEqual(run.call_args.kwargs["input"], "private-test-credential")
        for call in run.call_args_list:
            self.assertNotIn("private-test-credential", str(call.args))

    def test_failed_credential_read_never_attempts_secret_write(self):
        with patch("install.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "error")) as run:
            with self.assertRaises(SystemExit):
                install.save_scheduled_token()
        self.assertEqual(run.call_count, 1)

    def test_existing_workflow_scope_does_not_repeat_login(self):
        response = subprocess.CompletedProcess([], 0, "HTTP/2 200\nx-oauth-scopes: repo, workflow, gist\n\n{}", "")
        with patch("install.subprocess.run", return_value=response) as run:
            install.ensure_workflow_scope()
        self.assertEqual(run.call_count, 1)

    def test_missing_workflow_scope_refreshes_before_upload(self):
        replies = [subprocess.CompletedProcess([], 0, "HTTP/2 200\nx-oauth-scopes: repo, gist\n\n{}", ""),
                   subprocess.CompletedProcess([], 0)]
        with patch("install.subprocess.run", side_effect=replies) as run:
            install.ensure_workflow_scope()
        self.assertIn("refresh", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
