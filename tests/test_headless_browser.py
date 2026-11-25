from __future__ import annotations

from pathlib import Path
import unittest

from familiekilder.browsers import BrowserBootstrapError
from familiekilder_testing import headless_browser


class FakeStartBrowser(headless_browser.HeadlessBrowser):
    """Never starts a process; `failures` is what each start attempt raises."""

    def __init__(self, failures: list[BaseException]) -> None:
        super().__init__(Path("fictional-browser"))
        self.failures = list(failures)
        self.profiles: list[Path] = []

    def _launch(self, profile: Path) -> None:
        self.profiles.append(profile)

    def new_page(self, url: str) -> headless_browser.BrowserPage:
        if self.failures:
            raise self.failures.pop(0)
        return headless_browser.BrowserPage(self, "target", "session")


class StartTests(unittest.TestCase):
    def test_an_unanswered_start_is_tried_again_with_a_new_profile(self) -> None:
        browser = FakeStartBrowser([BrowserBootstrapError("no answer")])
        with browser:
            self.assertEqual(len(browser.profiles), 2)
            self.assertNotEqual(*browser.profiles)
            self.assertFalse(browser.profiles[0].exists())
        self.assertFalse(browser.profiles[1].exists())

    def test_start_gives_up_after_the_last_attempt(self) -> None:
        errors = [BrowserBootstrapError("no answer")] * headless_browser.START_ATTEMPTS
        browser = FakeStartBrowser(errors)
        with self.assertRaises(BrowserBootstrapError):
            browser.__enter__()
        self.assertEqual(len(browser.profiles), headless_browser.START_ATTEMPTS)
        self.assertFalse(any(profile.exists() for profile in browser.profiles))

    def test_other_errors_are_not_retried(self) -> None:
        browser = FakeStartBrowser([ValueError("a bug")])
        with self.assertRaises(ValueError):
            browser.__enter__()
        self.assertEqual(len(browser.profiles), 1)

    def test_a_failing_test_body_is_never_repeated(self) -> None:
        browser = FakeStartBrowser([])
        runs = 0
        with self.assertRaises(BrowserBootstrapError), browser:
            runs += 1
            raise BrowserBootstrapError("inside the test")
        self.assertEqual((runs, len(browser.profiles)), (1, 1))


if __name__ == "__main__":
    unittest.main()
