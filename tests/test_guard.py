"""Tests for the git/glab policy and the localhost web checks.

Style follows tests/test_models.py: table-driven cases under subTest, so one bad
case is pinpointed without hiding the rest.
"""

import unittest

from gateway import config, toolguard, webguard


class GitPushPolicyTest(unittest.TestCase):
    """A force push must be refused however it is spelled — flag or refspec."""

    def setUp(self):
        self.policy = config.DEFAULTS["tools"]["git"]["policy"]

    def _decide(self, argv):
        return toolguard.check_git_policy(argv, self.policy)[0]

    def test_force_and_delete_are_denied(self):
        cases = [
            # flag forms
            ["push", "--force", "origin", "main"],
            ["push", "-f", "origin", "main"],
            ["push", "--force", "origin"],
            ["push", "--mirror", "origin"],
            ["push", "--delete", "origin", "old-branch"],
            # refspec forms: a leading '+' forces with no flag present
            ["push", "origin", "+main:main"],
            ["push", "origin", "+refs/heads/main:refs/heads/main"],
            ["push", "origin", "+main"],
            # an empty source deletes the remote branch
            ["push", "origin", ":old-branch"],
            ["push", "origin", ":refs/heads/old-branch"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                self.assertEqual(self._decide(argv), "deny")

    def test_ordinary_pushes_still_allowed(self):
        cases = [
            ["push", "origin", "my-branch"],
            ["push", "origin", "main:main"],
            ["push", "-u", "origin", "my-branch"],
            ["push", "origin", "HEAD"],
            # --force-with-lease is a safe force and stays permitted
            ["push", "--force-with-lease", "origin", "my-branch"],
            # non-push subcommands are unaffected by the push rules
            ["status"],
            ["commit", "-m", "+not a refspec"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                self.assertEqual(self._decide(argv), "allow")


class HostIsLoopbackTest(unittest.TestCase):
    def test_only_loopback_names_pass(self):
        cases = [
            ("127.0.0.1:8600", True),
            ("127.0.0.1", True),
            ("localhost:8600", True),
            ("LocalHost:8600", True),
            ("[::1]:8600", True),
            ("[::1]", True),
            # a DNS-rebinding page reaches the loopback socket under its own name
            ("attacker.example:8600", False),
            ("example.com", False),
            ("127.0.0.1.nip.io:8600", False),
            ("192.168.1.10:8600", False),
            ("", False),
            (None, False),
        ]
        for host, expected in cases:
            with self.subTest(host=host):
                self.assertIs(webguard.host_is_loopback(host), expected)


class OriginAllowedTest(unittest.TestCase):
    def test_absent_origin_ok_listed_origin_ok_others_refused(self):
        allowed = ("http://127.0.0.1:8600",)
        cases = [
            # non-browser clients (curl, the guard shims) send no Origin
            (None, True),
            ("", True),
            ("http://127.0.0.1:8600", True),
            ("https://example.com", False),
            ("http://localhost:8600", False),  # not in this allow list
            ("null", False),
        ]
        for origin, expected in cases:
            with self.subTest(origin=origin):
                self.assertIs(webguard.origin_is_allowed(origin, allowed), expected)

    def test_empty_allow_list_refuses_every_browser_origin(self):
        self.assertFalse(webguard.origin_is_allowed("http://127.0.0.1:8765", ()))
        self.assertTrue(webguard.origin_is_allowed(None, ()))


class TokenMatchesTest(unittest.TestCase):
    def test_comparison(self):
        cases = [
            ("s3cret", "s3cret", True),
            ("s3cret", "other", False),
            (None, "s3cret", False),
            ("", "s3cret", False),
            # no expected token configured -> the check is not applied
            (None, None, True),
            ("anything", "", True),
        ]
        for supplied, expected, ok in cases:
            with self.subTest(supplied=supplied, expected=expected):
                self.assertIs(webguard.token_matches(supplied, expected), ok)


class CheckTest(unittest.TestCase):
    """webguard.check composes the three rules; verify each can refuse alone."""

    HEADER = "X-TierRoute-Token"

    def _check(self, headers):
        return webguard.check(
            headers,
            allowed_origins=("http://127.0.0.1:8600",),
            client_header=self.HEADER,
            client_token="good-token",
        )

    def test_fully_valid_request_passes(self):
        self.assertIsNone(
            self._check(
                {
                    "Host": "127.0.0.1:8600",
                    "Origin": "http://127.0.0.1:8600",
                    self.HEADER: "good-token",
                }
            )
        )

    def test_each_rule_refuses_independently(self):
        good = {
            "Host": "127.0.0.1:8600",
            "Origin": "http://127.0.0.1:8600",
            self.HEADER: "good-token",
        }
        cases = [
            ("bad host", {**good, "Host": "attacker.example:8600"}),
            ("bad origin", {**good, "Origin": "https://example.com"}),
            ("bad token", {**good, self.HEADER: "guessed"}),
            ("no token", {k: v for k, v in good.items() if k != self.HEADER}),
        ]
        for label, headers in cases:
            with self.subTest(case=label):
                self.assertIsNotNone(self._check(headers))


if __name__ == "__main__":
    unittest.main()
