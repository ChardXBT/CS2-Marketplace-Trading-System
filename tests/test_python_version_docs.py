"""Consistency checks for the public Python version documentation."""

from pathlib import Path
import re
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


def extract_workflow_python_versions(text):
    """Return python-version scalar values from workflow YAML text.

    This intentionally parses only the simple scalar form used by this
    repository's workflows; it does not attempt to be a general YAML parser.
    """
    versions = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("python-version:"):
            continue
        value = stripped.split(":", 1)[1].strip()
        value = value.split("#", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if value:
            versions.append(value)
    return versions


def extract_readme_python_requirement(text):
    """Return the major.minor requirement in the README Technology section."""
    match = re.search(r"- \*\*Python (\d+)\.(\d+)\+\*\*", text)
    if match is None:
        return None
    return f"{match.group(1)}.{match.group(2)}"


def repository_workflow_python_versions():
    versions = set()
    workflow_dir = REPO_ROOT / ".github" / "workflows"
    for path in sorted(workflow_dir.glob("*.yml")) + sorted(workflow_dir.glob("*.yaml")):
        versions.update(extract_workflow_python_versions(path.read_text(encoding="utf-8")))
    return versions


class PythonVersionDocumentationTests(unittest.TestCase):
    def test_public_readme_matches_workflow_python_version(self):
        workflow_versions = repository_workflow_python_versions()
        self.assertEqual(
            len(workflow_versions),
            1,
            f"workflows must agree on one Python version, found {sorted(workflow_versions)}",
        )
        readme_version = extract_readme_python_requirement(
            (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        )
        self.assertEqual(
            {readme_version},
            workflow_versions,
            "README Technology section must document the workflow Python version",
        )

    def test_workflow_parser_handles_quotes_whitespace_comments_and_duplicates(self):
        text = """
          python-version: "3.12"  # production runtime
          python-version: '3.12'
          python-version: 3.12
        """
        self.assertEqual(
            extract_workflow_python_versions(text),
            ["3.12", "3.12", "3.12"],
        )

    def test_workflow_parser_ignores_empty_missing_and_malformed_values(self):
        self.assertEqual(extract_workflow_python_versions(""), [])
        self.assertEqual(extract_workflow_python_versions("python-version:"), [])
        self.assertEqual(extract_workflow_python_versions("python-version:   # missing"), [])
        self.assertEqual(extract_workflow_python_versions("python_version: 3.12"), [])

    def test_readme_parser_handles_missing_or_nonsemantic_text(self):
        self.assertIsNone(extract_readme_python_requirement(""))
        self.assertIsNone(extract_readme_python_requirement("Python 3.12+"))
        self.assertEqual(
            extract_readme_python_requirement("- **Python 3.12+** for testing"),
            "3.12",
        )


if __name__ == "__main__":
    unittest.main()
