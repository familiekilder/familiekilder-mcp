"""README, package keywords and the prompt page name every service in REGISTRY.

The service lists in these texts are written by hand, so these tests stop a new
service from being added to REGISTRY without them.
"""

import asyncio
import html
import re
import tempfile
import tomllib
import unittest
from pathlib import Path

from familiekilder import setup_page
from familiekilder.live_config import Config
from familiekilder.source_server import build_server
from familiekilder.sources import REGISTRY

PROJECT = Path(__file__).resolve().parents[1]
README = PROJECT / "README.md"
PROMPTS = PROJECT / "docs/oppskrifter.md"
GUIDE = PROJECT / "docs/kom-i-gang.md"
AGENT_SETUP = PROJECT / "docs/agentoppsett.md"
WIN_LINUX_TRIAL = PROJECT / "docs/prove-pa-windows-og-linux.md"
ARCHITECTURE = PROJECT / "docs/arkitektur.md"
SETUP_PAGE_HTML = PROJECT / "pages/oppsett/index.html"
SERVICE_LINE_MARKER = "<!-- Tjenestelisten er hentet fra sources.REGISTRY. -->"
# A tool name in backticks, such as `spond_list_chats`.
TOOL_NAME = re.compile(r"`([a-z]+(?:_[a-z]+)+)`")
LOCALHOST_PORT = re.compile(r"http://127\.0\.0\.1:(\d+)")


def flat(text: str) -> str:
    """Text with every run of white space as one space."""
    return " ".join(text.split())


def unofficial_sources():
    """Services read without an official API, that is without a personal access token."""
    return [source for source in REGISTRY.values() if source.login != "token"]


def quoted_prompts(text: str) -> list[str]:
    """Each block quote in a Markdown text, as one flat string."""
    prompts, current = [], []
    for line in text.splitlines() + [""]:
        if line.startswith(">"):
            current.append(line[1:].strip())
        elif current:
            prompts.append(flat(" ".join(current)))
            current = []
    return prompts


def read_only_tool_names() -> set[str]:
    with tempfile.TemporaryDirectory() as directory:
        config = Config(Path(directory), {name: frozenset() for name in REGISTRY})
        server, runtime = build_server(config, read_only=True)
        try:
            tools = asyncio.run(server.list_tools())
        finally:
            runtime.close()
    return {tool.name for tool in tools}


class DiscoveryTextTests(unittest.TestCase):
    def test_readme_service_line_names_every_service(self):
        lines = README.read_text(encoding="utf-8").splitlines()
        service_line = lines[lines.index(SERVICE_LINE_MARKER) + 1]
        for source in REGISTRY.values():
            with self.subTest(source=source.name):
                self.assertIn(source.label, service_line)

    def test_readme_search_words_name_every_unofficial_service(self):
        readme = flat(README.read_text(encoding="utf-8"))
        self.assertIn("uoffisiell", readme)
        for platform in ("macOS", "Windows", "Linux"):
            with self.subTest(platform=platform):
                self.assertIn(platform, readme)
        for browser in ("Brave", "Chrome", "Edge"):
            with self.subTest(browser=browser):
                self.assertIn(browser, readme)
        for source in unofficial_sources():
            with self.subTest(source=source.name):
                self.assertIn(f"{source.label} API", readme)

    def test_package_keywords_name_mcp_and_every_service(self):
        project = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        keywords = project["keywords"]
        self.assertIn("mcp", keywords)
        for source in REGISTRY.values():
            with self.subTest(source=source.name):
                self.assertIn(source.label.casefold(), keywords)

    def test_prompt_page_keeps_every_readme_prompt(self):
        readme = README.read_text(encoding="utf-8")
        self.assertIn("](docs/oppskrifter.md)", readme)
        page = quoted_prompts(PROMPTS.read_text(encoding="utf-8"))
        prompts = quoted_prompts(readme)
        self.assertGreaterEqual(len(prompts), 4)
        for prompt in prompts:
            with self.subTest(prompt=prompt[:40]):
                self.assertIn(prompt, page)

    def test_prompt_page_names_only_tools_the_server_offers(self):
        named = set(TOOL_NAME.findall(PROMPTS.read_text(encoding="utf-8")))
        self.assertIn("family_connections", named)
        self.assertEqual(named - read_only_tool_names(), set())

    def test_setup_docs_contain_pages_url(self):
        for path in (README, GUIDE, AGENT_SETUP):
            with self.subTest(path=path.relative_to(PROJECT).as_posix()):
                self.assertIn(setup_page.PAGES_URL, path.read_text(encoding="utf-8"))

    def test_reopen_commands_match_setup_page_agent_message(self):
        page_html = SETUP_PAGE_HTML.read_text(encoding="utf-8")
        match = re.search(r'<pre id="agent-message"[^>]*>(.*?)</pre>', page_html, re.DOTALL)
        self.assertIsNotNone(match, "agent-message block missing from pages/oppsett/index.html")
        agent_message = html.unescape(match.group(1)).strip()
        lines = agent_message.splitlines()
        unix_cmd = next(
            line.removeprefix("macOS/Linux:").strip()
            for line in lines
            if line.startswith("macOS/Linux:")
        )
        win_cmd = next(
            line.removeprefix("Windows (PowerShell):").strip()
            for line in lines
            if line.startswith("Windows (PowerShell):")
        )
        self.assertIn("setup --no-open", unix_cmd)
        self.assertIn("setup --no-open", win_cmd)

        for path in (README, PROMPTS, AGENT_SETUP, GUIDE):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(PROJECT).as_posix()):
                self.assertIn(unix_cmd, text)
                self.assertIn(win_cmd, text)

        flat_message = flat(agent_message)
        for path in (README, PROMPTS, AGENT_SETUP):
            with self.subTest(quoted=path.relative_to(PROJECT).as_posix()):
                text = path.read_text(encoding="utf-8")
                # Without backticks Markdown drops the backslash in `\.private`.
                self.assertIn(f"`{unix_cmd}`", text)
                self.assertIn(f"`{win_cmd}`", text)
                prompts = [prompt.replace("`", "") for prompt in quoted_prompts(text)]
                self.assertIn(flat_message, prompts)

    def test_documented_setup_port_matches_setup_page_constant(self):
        expected_origin = f"http://127.0.0.1:{setup_page.SETUP_PORT}"
        for path in (
            README,
            GUIDE,
            AGENT_SETUP,
            WIN_LINUX_TRIAL,
            ARCHITECTURE,
            SETUP_PAGE_HTML,
        ):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(PROJECT).as_posix()):
                self.assertIn(expected_origin, text)
                ports = {int(port) for port in LOCALHOST_PORT.findall(text)}
                self.assertEqual(ports, {setup_page.SETUP_PORT})


if __name__ == "__main__":
    unittest.main()
