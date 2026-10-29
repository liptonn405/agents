"""Read-only Code RAG for the AnalystAgent.

The index is in-memory and disposable.  It never writes to the repository.
Embeddings are deterministic hashing features with IDF weights, so the agent
works without downloading an embedding model.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import chromadb
from chromadb.config import Settings

SOURCE_EXTENSIONS = {".py", ".html", ".js", ".sql"}
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    "vendor",
    "assets",
    "migrations",
    "tests",
    "auth_tests",
}
MAX_FILE_SIZE = 256 * 1024
CHUNK_SIZE = 1800
CHUNK_OVERLAP = 200

RU_EN_HINTS: dict[str, str] = {
    "профил": "profile user",
    "пользовател": "user",
    "сет": "network net",
    "тополог": "topology",
    "созда": "create created",
    "дат": "date",
    "количеств": "count",
    "модел": "model",
    "класс": "class",
    "поле": "field column",
    "таблиц": "table",
    "последн": "last latest",
    "шаблон": "template",
    "страниц": "page",
    "обработчик": "view handler",
    "запрос": "request query",
    "баз": "database db",
    "данн": "data",
    "добав": "add",
    "отображ": "display show",
    "показ": "show display",
    "измен": "change update",
    "удал": "delete remove",
    "редакт": "edit",
    "список": "list",
    "проверк": "check validation",
    "ошибк": "error",
    "тест": "test",
    "эмуляц": "emulation simulation",
    "симуляц": "simulation",
    "запуск": "run start",
    "останов": "stop",
    "авторизац": "auth login",
    "регистрац": "register signup",
    "парол": "password",
    "админ": "admin",
}

LEXICAL_STOPWORDS = {
    "и", "в", "во", "на", "по", "не", "с", "со", "для", "что", "как",
    "это", "из", "к", "у", "о", "об", "от", "до", "или", "а", "но",
    "the", "and", "to", "of", "in", "is", "a", "an", "for", "on", "with",
}

SYMBOL_STOPWORDS = {
    "Miminet", "GitHub", "Flask", "Python", "SQLAlchemy", "Jinja", "Jinja2",
    "Issue", "README", "API", "JSON", "HTTP", "HTML", "CSS", "JavaScript",
}


@dataclass(frozen=True)
class ContextSnippet:
    file_path: str
    content: str
    relevance: float
    reason: str | None = None


class HashEmbeddingFunction:
    """Deterministic hashing embeddings with corpus IDF weights."""

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim
        self.idf: dict[str, float] = {}
        self._default_idf = 1.0

    @staticmethod
    def _tokens(text: str) -> Iterable[str]:
        for raw in re.findall(r"[A-Za-zА-Яа-яЁё0-9_]+", text):
            lowered = raw.lower()
            yield lowered
            for part in re.split(r"_+", raw):
                if part and part.lower() != lowered:
                    yield part.lower()
            for part in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", raw):
                if part.lower() != lowered:
                    yield part.lower()

    def _features(self, text: str) -> Iterable[str]:
        for token in self._tokens(text):
            yield token
            if len(token) >= 5:
                for size in (3, 4):
                    for i in range(len(token) - size + 1):
                        yield f"{size}:{token[i : i + size]}"

    def fit(self, texts: Sequence[str]) -> "HashEmbeddingFunction":
        document_count = 0
        document_frequency: Counter[str] = Counter()
        for text in texts:
            document_count += 1
            document_frequency.update(set(self._features(text)))
        self.idf = {
            feature: math.log((document_count + 1) / (frequency + 1)) + 1.0
            for feature, frequency in document_frequency.items()
        }
        self._default_idf = math.log(document_count + 1) + 1.0
        return self

    @staticmethod
    def _expand_query(text: str) -> str:
        hints: list[str] = []
        for token in HashEmbeddingFunction._tokens(text.lower()):
            for prefix, expansion in RU_EN_HINTS.items():
                if token.startswith(prefix):
                    hints.append(expansion)
                    break
        return " ".join(hints)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for feature, count in Counter(self._features(text)).items():
            digest = hashlib.md5(feature.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "little") % self.dim
            vector[index] += (1.0 + math.log(count)) * self.idf.get(
                feature, self._default_idf
            )
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            vector[0] = 1.0
            return vector
        return [value / norm for value in vector]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(f"{text}\n{self._expand_query(text)}")


def resolve_repo(repo_path: str | Path | None = None) -> Path:
    """Find the Miminet checkout without writing anything into it."""
    def valid(path: Path) -> bool:
        return (path / "front" / "src").is_dir() and (path / "back" / "src").is_dir()

    if repo_path:
        resolved = Path(repo_path).expanduser().resolve()
        if not valid(resolved):
            raise RuntimeError(
                f"Не похоже на репозиторий Miminet: {repo_path}. "
                "Ожидались каталоги front/src и back/src."
            )
        return resolved

    candidates: list[Path] = []
    for env_name in ("ANALYST_REPO", "MIMINET_REPO"):
        value = os.getenv(env_name)
        if value:
            candidates.append(Path(value))

    here = Path(__file__).resolve()
    candidates.extend(
        [
            here.parents[3] / "miminet",
            Path.cwd() / "miminet",
            Path.cwd().parent / "miminet",
            Path.cwd(),
        ]
    )

    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if valid(resolved):
            return resolved

    raise RuntimeError(
        "Репозиторий Miminet не найден. Передайте --repo, ANALYST_REPO "
        "или MIMINET_REPO."
    )


def iter_source_files(repo_root: Path) -> Iterable[tuple[Path, str]]:
    for root_name in ("front/src", "back/src"):
        root = repo_root / root_name
        if not root.is_dir():
            continue
        for file_path in sorted(root.rglob("*")):
            if not file_path.is_file():
                continue
            relative = file_path.relative_to(repo_root).as_posix()
            parts = set(relative.split("/"))
            if parts & EXCLUDED_DIRS:
                continue
            if file_path.suffix.lower() not in SOURCE_EXTENSIONS:
                continue
            if file_path.name.endswith(".min.js"):
                continue
            if relative.startswith("front/src/static/"):
                if file_path.suffix.lower() != ".js":
                    continue
                if file_path.parent != repo_root / "front" / "src" / "static":
                    continue
            try:
                if file_path.stat().st_size > MAX_FILE_SIZE:
                    continue
            except OSError:
                continue
            yield file_path, relative


def iter_chunks(
    text: str,
    size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> Iterable[tuple[str, int]]:
    text = text.rstrip()
    if not text:
        return
    start = 0
    length = len(text)
    while start < length:
        end = min(start + size, length)
        if end < length:
            newline = text.rfind("\n", start + size // 2, end)
            if newline > start:
                end = newline
        raw_chunk = text[start:end]
        chunk = raw_chunk.strip()
        if chunk:
            leading = len(raw_chunk) - len(raw_chunk.lstrip())
            chunk_start = start + leading
            start_line = text.count("\n", 0, chunk_start) + 1
            yield chunk, start_line
        if end >= length:
            break
        start = max(end - overlap, start + 1)


class CodeIndex:
    """Disposable in-memory index over front/src and back/src."""

    def __init__(self, repo_path: str | Path | None = None) -> None:
        self.repo_path = resolve_repo(repo_path)
        self.embedder = HashEmbeddingFunction()
        self._client: chromadb.ClientAPI
        try:
            self._client = chromadb.EphemeralClient(
                settings=Settings(anonymized_telemetry=False)
            )
        except Exception:
            self._client = chromadb.EphemeralClient()

        digest = hashlib.sha1(str(self.repo_path).encode("utf-8")).hexdigest()[:8]
        try:
            self.collection = self._client.get_or_create_collection(
                name=f"analyst_repo_{digest}",
                metadata={"hnsw:space": "cosine"},
            )
        except Exception:
            self.collection = self._client.get_or_create_collection(
                name=f"analyst_repo_{digest}"
            )
        self._build()

    def _build(self) -> None:
        documents: list[str] = []
        embedding_texts: list[str] = []
        ids: list[str] = []
        metadatas: list[dict[str, str | int]] = []
        file_count = 0

        for file_path, relative in iter_source_files(self.repo_path):
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not content.strip():
                continue
            file_count += 1
            for chunk_index, (chunk, start_line) in enumerate(iter_chunks(content)):
                documents.append(chunk)
                embedding_texts.append(f"{relative}\n{relative}\n{chunk}")
                ids.append(f"{relative}::{start_line}::{chunk_index}")
                metadatas.append(
                    {
                        "path": relative,
                        "start_line": start_line,
                        "end_line": start_line + chunk.count("\n"),
                    }
                )

        if not documents:
            raise RuntimeError("Не удалось найти исходники для индексации.")

        self.embedder.fit(embedding_texts)
        for offset in range(0, len(documents), 200):
            self.collection.add(
                ids=ids[offset : offset + 200],
                documents=documents[offset : offset + 200],
                metadatas=metadatas[offset : offset + 200],
                embeddings=self.embedder.embed_documents(
                    embedding_texts[offset : offset + 200]
                ),
            )

    def _lexical_features(self, text: str) -> set[str]:
        return {
            token
            for token in self.embedder._tokens(text)
            if len(token) > 1 and token not in LEXICAL_STOPWORDS
        }

    @staticmethod
    def _extract_symbols(text: str) -> list[str]:
        symbols: list[str] = []
        for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b", text):
            if token in SYMBOL_STOPWORDS:
                continue
            if "_" not in token and not any(ch.isupper() for ch in token):
                continue
            if token not in symbols:
                symbols.append(token)
        return symbols[:6]

    def _definition_candidates(
        self,
        issue_text: str,
        query_vector: list[float],
        query_features: set[str],
    ) -> list[tuple[float, float, str, str, dict]]:
        candidates: list[tuple[float, float, str, str, dict]] = []
        idf = self.embedder.idf

        for symbol in self._extract_symbols(issue_text):
            symbol_candidates: list[tuple[float, float, str, str, dict]] = []
            for needle in (f"class {symbol}", f"def {symbol}"):
                try:
                    found = self.collection.get(
                        where_document={"$contains": needle},
                        include=["documents", "metadatas", "embeddings"],
                        limit=3,
                    )
                except Exception:
                    continue

                if needle.startswith("class "):
                    pattern = re.compile(rf"\bclass\s+{re.escape(symbol)}\b")
                else:
                    pattern = re.compile(rf"\bdef\s+{re.escape(symbol)}\s*\(")

                for i, (document, metadata) in enumerate(
                    zip(found.get("documents") or [], found.get("metadatas") or [])
                ):
                    if not pattern.search(document):
                        continue
                    metadata = metadata or {}
                    path = metadata.get("path", "")
                    vector_score = 0.0
                    embeddings = found.get("embeddings")
                    if embeddings is not None and i < len(embeddings):
                        try:
                            vector_score = max(
                                0.0,
                                min(
                                    1.0,
                                    sum(
                                        float(a) * float(b)
                                        for a, b in zip(query_vector, embeddings[i])
                                    ),
                                ),
                            )
                        except (TypeError, ValueError):
                            pass
                    path_words = (
                        path.replace("/", " ").replace(".", " ").replace("_", " ")
                    )
                    lexical_score = sum(
                        idf.get(feature, 1.0)
                        for feature in query_features
                        & self._lexical_features(document)
                    )
                    path_score = sum(
                        idf.get(feature, 1.0)
                        for feature in query_features
                        & self._lexical_features(path_words)
                    )
                    symbol_candidates.append(
                        (
                            lexical_score + 3.0 * path_score + 8.0 * vector_score + 100.0,
                            vector_score,
                            path,
                            document,
                            metadata,
                        )
                    )
            if symbol_candidates:
                candidates.append(max(symbol_candidates, key=lambda item: item[0]))
        return candidates

    def retrieve(self, query: str, top_k: int = 5) -> list[ContextSnippet]:
        count = self.collection.count()
        limit = min(max(1, top_k), count)
        if limit <= 0:
            return []

        fetch_limit = min(max(limit * 20, 100), count)
        query_vector = self.embedder.embed_query(query)
        result = self.collection.query(
            query_embeddings=[query_vector],
            n_results=fetch_limit,
        )
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]

        expanded_query = f"{query}\n{self.embedder._expand_query(query)}"
        query_features = self._lexical_features(expanded_query)
        idf = self.embedder.idf
        candidates: list[tuple[float, float, str, str, dict]] = []

        for i, document in enumerate(documents):
            metadata = metadatas[i] if i < len(metadatas) else {}
            path = (metadata or {}).get("path", f"unknown/file_{i}")
            distance = float(distances[i]) if i < len(distances) else 1.0
            vector_score = max(0.0, min(1.0, 1.0 - distance))
            path_words = path.replace("/", " ").replace(".", " ").replace("_", " ")
            lexical_score = sum(
                idf.get(feature, 1.0)
                for feature in query_features & self._lexical_features(document)
            )
            path_score = sum(
                idf.get(feature, 1.0)
                for feature in query_features & self._lexical_features(path_words)
            )
            candidates.append(
                (
                    lexical_score + 3.0 * path_score + 8.0 * vector_score,
                    vector_score,
                    path,
                    document,
                    metadata or {},
                )
            )

        candidates.extend(
            self._definition_candidates(query, query_vector, query_features)
        )

        unique: dict[tuple[str, object], tuple] = {}
        for item in candidates:
            key = (item[2], item[4].get("start_line"))
            current = unique.get(key)
            if current is None or item[0] > current[0]:
                unique[key] = item

        snippets: list[ContextSnippet] = []
        per_file: dict[str, int] = {}
        for _, vector_score, path, document, metadata in sorted(
            unique.values(), key=lambda item: item[0], reverse=True
        ):
            if per_file.get(path, 0) >= 2:
                continue
            start_line = metadata.get("start_line")
            end_line = metadata.get("end_line")
            reason = (
                f"строки {start_line}-{end_line}"
                if isinstance(start_line, int) and isinstance(end_line, int)
                else None
            )
            snippets.append(
                ContextSnippet(
                    file_path=path,
                    content=document,
                    relevance=round(vector_score, 4),
                    reason=reason,
                )
            )
            per_file[path] = per_file.get(path, 0) + 1
            if len(snippets) >= limit:
                break
        return snippets
