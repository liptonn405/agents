import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyst_agent.retrieval import CodeIndex
from test_agent import make_repo


class RetrievalTests(unittest.TestCase):
    def test_russian_query_finds_profile_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            index = CodeIndex(repo)
            snippets = index.retrieve(
                "Показать в профиле пользователя статистику сетей", top_k=5
            )
            paths = {snippet.file_path for snippet in snippets}
            self.assertIn("front/src/templates/auth/profile.html", paths)
            self.assertIn("front/src/miminet_auth.py", paths)

    def test_reported_line_numbers_match_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            index = CodeIndex(repo)
            snippets = index.retrieve("статистика сетей в профиле", top_k=5)
            self.assertTrue(snippets)
            for snippet in snippets:
                source = (repo / snippet.file_path).read_text(encoding="utf-8")
                offset = source.find(snippet.content)
                self.assertGreaterEqual(offset, 0)
                actual = source.count("\n", 0, offset) + 1
                match = re.search(r"строки (\d+)-", snippet.reason or "")
                self.assertIsNotNone(match)
                self.assertEqual(int(match.group(1)), actual)


if __name__ == "__main__":
    unittest.main()
