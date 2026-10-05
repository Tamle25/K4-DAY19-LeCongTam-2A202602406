"""Knowledge Graph (Neo4j) + GraphRAG over two drug-topic knowledge bases.

Contract (fixed — bench_kg.py and the tests rely on it):
    link_entity(name, known)                       -> one of `known` or None          (TODO KG-1)
    build_graph(graph, law_docs, news_docs, llm_fn)   load both KBs into Neo4j      (TODO KG-2)
        every node created from ONE document carries the property `doc_id`
    Neo4jGraph.context(question, doc_ids)         -> list[str] facts               (TODO KG-3)
    GraphRAGAgent.answer(question, top_k)         -> str                           (TODO KG-4)

Everything else in this file is a HINT: one possible ontology (below). Use it as is, change it,
or design your own — your own ontology + report/ONTOLOGY.md earns the bonus (see SUBMISSION.md).

Suggested ontology (Crime is the bridge between the law KB and the news KB):

    (:Article {id, title, law, doc_id})-[:DEFINES]->(:Crime {name})
    (:Article)-[:HAS_CLAUSE]->(:Clause {id, number, penalty, text})-[:MENTIONS]->(:Substance {name})
    (:Case {name, summary, date, doc_id})-[:CHARGED_WITH]->(:Crime)
    (:Case)-[:INVOLVES {amount}]->(:Substance)
    (:Case)-[:LOCATED_IN]->(:Location {name})
    (:Person {name, aliases})-[:INVOLVED_IN {role, sentence, charge}]->(:Case)
"""

from __future__ import annotations

import difflib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from .models import Document
from .store import EmbeddingStore

# Canonical substance names: the ones BLHS Chương XX lists, plus common ones in Vietnamese news.
SUBSTANCES = ["Heroine", "Cocaine", "Methamphetamine", "Amphetamine", "MDMA", "XLR-11", "Ketamine",
              "cần sa", "thuốc phiện", "côca"]
CLAUSE_START = re.compile(r"^(\d+)\.\s", re.MULTILINE)
FOOTNOTE = re.compile(r"\[\d+\]")

def load_markdown_docs(folder: str | Path) -> list[Document]:
    """Read crawler output (.md with a flat `key: "value"` front matter) into Documents."""
    docs = []
    for path in sorted(Path(folder).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        _, front, body = raw.split("---", 2)
        metadata = {k: json.loads(v) for k, v in re.findall(r'^(\w+): (".*")$', front, re.MULTILINE)}
        docs.append(Document(id=metadata.get("doc_id", path.stem), content=body.strip(), metadata=metadata))
    return docs

def normalize_crime(name: str) -> str:
    """'Tội Mua bán trái phép chất ma túy' -> 'mua bán trái phép chất ma túy'."""
    name = re.sub(r"\s+", " ", name.strip().strip("\"'“”").lower())
    return name.removeprefix("tội ").strip()

def link_entity(name: str, known: list[str], normalize: Callable[[str], str] = normalize_crime) -> str | None:
    """Map a free-text mention (e.g. a charge written by a journalist) onto one canonical name in `known`."""
    if not name or not name.strip() or not known:
        return None
    norm_name = normalize(name)
    if not norm_name:
        return None

    # Map normalized forms back to the original spelling in known
    norm_to_orig: dict[str, str] = {}
    for orig in known:
        norm = normalize(orig)
        if norm and norm not in norm_to_orig:
            norm_to_orig[norm] = orig

    # 1. Exact match after normalize
    if norm_name in norm_to_orig:
        return norm_to_orig[norm_name]

    # 2. Fuzzy match with difflib
    matches = difflib.get_close_matches(norm_name, list(norm_to_orig.keys()), n=1, cutoff=0.8)
    if matches:
        return norm_to_orig[matches[0]]

    return None

SYNONYMS_MAP = {
    "kẹo": "MDMA",
    "thuốc lắc": "MDMA",
    "ecstasy": "MDMA",
    "ma túy kẹo": "MDMA",
    "đá": "Methamphetamine",
    "ma túy đá": "Methamphetamine",
    "chấm đá": "Methamphetamine",
    "hàng đá": "Methamphetamine",
    "meth": "Methamphetamine",
    "ke": "Ketamine",
    "khay": "Ketamine",
    "hàng trắng": "Heroine",
    "bạch phiến": "Heroine",
    "heroin": "Heroine",
    "bồ đà": "cần sa",
    "cỏ": "cần sa",
    "cỏ mỹ": "cần sa",
    "côca": "Cocaine",
    "coca": "Cocaine",
}

def canonicalize_substance(name: str) -> str:
    """Map slang or street names to canonical BLHS substance names."""
    cleaned = name.strip()
    lowered = cleaned.lower()
    for sub in SUBSTANCES:
        if sub.lower() == lowered:
            return sub
    for slang, canon in SYNONYMS_MAP.items():
        if slang in lowered:
            return canon
    for sub in SUBSTANCES:
        if sub.lower() in lowered:
            return sub
    return cleaned

def find_substances(text: str) -> list[str]:
    lowered = text.lower()
    return [name for name in SUBSTANCES if name.lower() in lowered]

# ----------------------------------------------------------------------------------------------
# HINT — suggested ontology: extraction helpers
# ----------------------------------------------------------------------------------------------

def parse_law_article(doc: Document) -> dict[str, Any]:
    """Deterministic (regex) extraction for one 'Điều' — law text is regular enough to skip the LLM."""
    article_id = doc.metadata["article"]                       # "Điều 251 BLHS"
    title = doc.metadata["title"].split(". ", 1)[-1]           # "Tội mua bán trái phép chất ma túy"
    body = FOOTNOTE.sub("", doc.content)
    starts = list(CLAUSE_START.finditer(body))
    clauses = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        text = body[start.start():end].strip()
        first_line = text.splitlines()[0]
        penalty = re.search(r"\bbị ((?:phạt|tù|cảnh cáo).+?)(?::|$)", first_line)
        clauses.append({
            "id": f"{article_id} khoản {start.group(1)}",
            "number": int(start.group(1)),
            "penalty": penalty.group(1).rstrip(".") if penalty else "",
            "text": text,
            "substances": find_substances(text),
        })
    return {
        "id": article_id,
        "law": doc.metadata.get("law", ""),
        "title": title,
        "doc_id": doc.id,
        "crime": normalize_crime(title) if title.startswith("Tội ") else None,
        "clauses": clauses,
    }

NEWS_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài. Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "charges": ["tội danh, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất, dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp", "amount": "khối lượng nếu có"}}],
  "people": [{{"name": "họ tên", "aliases": ["biệt danh"], "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "charge": "tội danh của người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "sentence": "mức án nếu có, ví dụ: tử hình, 8 năm tù"}}]
}}]}}
Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def extract_news_cases(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM extraction for one news article; charges are re-linked to law-KB crimes in code."""
    prompt = NEWS_EXTRACTION_PROMPT.format(
        crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        cases = json.loads(llm_fn(prompt)).get("cases", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    for case in cases:
        case["charges"] = sorted({c for c in (link_entity(x, known_crimes) for x in case.get("charges", [])) if c})
        for s in case.get("substances", []):
            if s.get("name"):
                s["raw_name"] = s["name"]
                s["name"] = canonicalize_substance(s["name"])
        for person in case.get("people", []):
            person["charge"] = link_entity(person.get("charge") or "", known_crimes) or ""
    return cases

# ----------------------------------------------------------------------------------------------
# Neo4j
# ----------------------------------------------------------------------------------------------

class Neo4jGraph:
    """Thin wrapper over the official neo4j driver."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        from neo4j import GraphDatabase

        self.driver = GraphDatabase.driver(uri, auth=(user, password), notifications_min_severity="OFF")
        self.driver.verify_connectivity()

    def close(self) -> None:
        self.driver.close()

    def run(self, cypher: str, **params: Any) -> list[dict]:
        records, _, _ = self.driver.execute_query(cypher, params)
        return [record.data() for record in records]

    def reset(self) -> None:
        """Delete every node, relationship and constraint (bench_kg.py calls this before build_graph)."""
        self.run("MATCH (n) DETACH DELETE n")
        for row in self.run("SHOW CONSTRAINTS YIELD name RETURN name"):
            self.run(f"DROP CONSTRAINT `{row['name']}` IF EXISTS")

    def stats(self) -> dict[str, int]:
        nodes = self.run("MATCH (n) RETURN count(n) AS n")[0]["n"]
        rels = self.run("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
        return {"nodes": nodes, "relationships": rels}

    def seed_facts(self, question: str, doc_ids: list[str], skip_labels: tuple[str, ...] = (),
                   limit: int = 60) -> tuple[list[str], list[str]]:
        """Ontology-independent first step: seed nodes + their 1-hop edges as text facts.

        Seeds = nodes whose `doc_id` is in doc_ids, or whose `name`/`aliases` appear in the question.
        Returns (seed elementIds, facts). Nodes with a label in skip_labels are left out of the facts.
        """
        seeds = self.run(
            """
            MATCH (n)
            WHERE n.doc_id IN $doc_ids
               OR (n.name IS :: STRING AND size(n.name) >= 3 AND toLower($q) CONTAINS toLower(n.name))
               OR any(a IN coalesce(n.aliases, []) WHERE size(a) >= 3 AND toLower($q) CONTAINS toLower(a))
            RETURN elementId(n) AS id
            """,
            q=question, doc_ids=doc_ids,
        )
        seed_ids = [row["id"] for row in seeds]
        edges = self.run(
            """
            MATCH (s)-[r]-(m)
            WHERE elementId(s) IN $ids
              AND none(l IN labels(s) + labels(m) WHERE l IN $skip)
            WITH DISTINCT r LIMIT $limit
            WITH startNode(r) AS a, r, endNode(r) AS b
            RETURN labels(a)[0] AS a_label, coalesce(a.name, a.id) AS a_name, type(r) AS rel,
                   properties(r) AS props, labels(b)[0] AS b_label, coalesce(b.name, b.id) AS b_name
            """,
            ids=seed_ids, skip=list(skip_labels), limit=limit,
        )
        facts = []
        for e in edges:
            props = ", ".join(f"{k}: {v}" for k, v in e["props"].items() if v)
            facts.append(f"({e['a_label']}: {e['a_name']}) -[{e['rel']}{' {' + props + '}' if props else ''}]-> "
                         f"({e['b_label']}: {e['b_name']})")
        return seed_ids, facts

    # ---------------------------------------------------------------- HINT — suggested ontology: writes

    def suggested_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Crime", "name"), ("Case", "name"),
                           ("Substance", "name"), ("Person", "name"), ("Location", "name")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_law_article(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id}) SET a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.text = clause.text, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (s IN clause.substances | MERGE (sub:Substance {name: s}) MERGE (cl)-[:MENTIONS]->(sub))
            """,
            **article,
        )
        # Custom Ontology: Link HAS_MAX_PENALTY to clause with highest penalty
        clauses = article.get("clauses", [])
        if clauses:
            max_cl = None
            for cl in reversed(clauses):
                pen = cl.get("penalty", "").lower()
                if "tử hình" in pen or "chung thân" in pen:
                    max_cl = cl
                    break
            if not max_cl:
                for cl in reversed(clauses):
                    if "tù" in cl.get("penalty", "").lower():
                        max_cl = cl
                        break
            if not max_cl:
                max_cl = clauses[-1]
            self.run(
                """
                MATCH (a:Article {id: $aid}), (cl:Clause {id: $cid})
                MERGE (a)-[r:HAS_MAX_PENALTY]->(cl)
                SET r.penalty = $penalty
                """,
                aid=article["id"], cid=max_cl["id"], penalty=max_cl.get("penalty", "")
            )

    def add_news_case(self, case: dict, doc: Document) -> None:
        self.run(
            """
            MERGE (k:Case {name: $name})
              SET k.summary = $summary, k.date = $date, k.doc_id = $doc_id, k.source_title = $title
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances | MERGE (sub:Substance {name: s.name}) MERGE (k)-[r:INVOLVES]->(sub)
                SET r.amount = s.amount, r.raw_name = coalesce(s.raw_name, s.name))
            FOREACH (p IN $people | MERGE (person:Person {name: p.name})
                SET person.aliases = coalesce(p.aliases, []), person.doc_id = $doc_id
                MERGE (person)-[r:INVOLVED_IN]->(k) SET r.role = p.role, r.charge = p.charge, r.sentence = p.sentence)
            """,
            name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""), date=case.get("date", ""), location=case.get("location", ""),
            charges=case.get("charges", []), people=[p for p in case.get("people", []) if p.get("name")],
            substances=[s for s in case.get("substances", []) if s.get("name")],
            doc_id=doc.id, title=doc.metadata.get("title", ""),
        )

    # ---------------------------------------------------------------- KG-3

    def context(self, question: str, doc_ids: list[str], max_facts: int = 60) -> list[str]:
        """Graph facts for a question: seeds + 1 hop, then the legal basis of every case reached."""
        seed_ids, facts = self.seed_facts(question, doc_ids)

        # a. Cases that are a seed or next to one
        case_rows = self.run(
            """
            MATCH (k:Case)
            WHERE elementId(k) IN $ids OR EXISTS { MATCH (s)--(k) WHERE elementId(s) IN $ids }
            RETURN DISTINCT elementId(k) AS id, k.name AS name, k.summary AS summary
            """,
            ids=seed_ids,
        )
        case_ids = [r["id"] for r in case_rows]
        for r in case_rows:
            if r["summary"]:
                facts.append(f"Vụ việc '{r['name']}': {r['summary']}")

        # b. For those cases follow:
        #    (Case)-[:CHARGED_WITH]->(Crime)<-[:DEFINES]-(Article)-[:HAS_CLAUSE]->(Clause)
        q_lower = question.lower()
        asks_max_penalty = any(w in q_lower for w in ["tối đa", "cao nhất", "nặng nhất"])

        if case_ids:
            clause_rows = self.run(
                """
                MATCH (k:Case)-[:CHARGED_WITH]->(c:Crime)<-[:DEFINES]-(a:Article)-[:HAS_CLAUSE]->(cl:Clause)
                WHERE elementId(k) IN $case_ids
                  AND (
                    cl.number = 1
                    OR ($asks_max AND EXISTS { (a)-[:HAS_MAX_PENALTY]->(cl) })
                    OR EXISTS { MATCH (k)-[:INVOLVES]->(s:Substance)<-[:MENTIONS]-(cl) }
                  )
                RETURN DISTINCT a.id AS article_id, a.title AS title, cl.number AS number, cl.text AS text
                ORDER BY a.id, cl.number
                """,
                case_ids=case_ids, asks_max=asks_max_penalty,
            )
            for r in clause_rows:
                facts.append(f"[{r['article_id']} - {r['title']}] khoản {r['number']}: {r['text']}")

        # c. Articles named in the question ("Điều 251" -> re.findall(r"[Đđ]iều\s+(\d+)", question)):
        article_nums = re.findall(r"[Đđ]iều\s+(\d+)", question)
        if article_nums:
            article_patterns = [f"Điều {num} " for num in article_nums]
            subs_in_q = find_substances(question)
            art_rows = self.run(
                """
                MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)
                WHERE any(pat IN $patterns WHERE a.id STARTS WITH pat)
                  AND (
                    cl.number = 1
                    OR ($asks_max AND EXISTS { (a)-[:HAS_MAX_PENALTY]->(cl) })
                    OR (size($subs) > 0 AND EXISTS { MATCH (s:Substance)<-[:MENTIONS]-(cl) WHERE s.name IN $subs })
                  )
                RETURN DISTINCT a.id AS article_id, a.title AS title, cl.number AS number, cl.text AS text
                ORDER BY a.id, cl.number
                """,
                patterns=article_patterns, asks_max=asks_max_penalty, subs=subs_in_q,
            )
            for r in art_rows:
                fact = f"[{r['article_id']} - {r['title']}] khoản {r['number']}: {r['text']}"
                if fact not in facts:
                    facts.append(fact)

        # De-duplicate while preserving order
        seen = set()
        deduped = []
        for f in facts:
            if f not in seen:
                seen.add(f)
                deduped.append(f)

        return deduped[:max_facts]

# ---------------------------------------------------------------------------------------------- KG-2

def build_graph(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                llm_fn: Callable[..., str]) -> None:
    """Load both KBs into an empty graph. llm_fn(prompt, json_mode=False) -> str (metered OpenAI chat)."""
    graph.suggested_constraints()
    articles = [parse_law_article(d) for d in law_docs]
    for a in articles:
        graph.add_law_article(a)
    crimes = [a["crime"] for a in articles if a["crime"]]
    for d in news_docs:
        cases = extract_news_cases(d, lambda p: llm_fn(p, json_mode=True), crimes)
        for case in cases:
            graph.add_news_case(case, d)
        time.sleep(1.5)

# ---------------------------------------------------------------------------------------------- KG-4

GRAPH_PROMPT = """Trả lời câu hỏi chỉ dựa trên ngữ cảnh (đoạn văn bản và dữ kiện từ knowledge graph).
Nêu rõ số Điều luật khi có. Nếu ngữ cảnh không đủ, nói không đủ thông tin.

Dữ kiện knowledge graph:
{facts}

Đoạn văn bản:
{chunks}

Câu hỏi: {question}
Trả lời:"""

class GraphRAGAgent:
    """Hybrid GraphRAG: the same vector top-k as flat RAG, plus facts expanded from the graph."""

    def __init__(self, store: EmbeddingStore, graph: Neo4jGraph, llm_fn: Callable[[str], str]) -> None:
        self.store = store
        self.graph = graph
        self.llm_fn = llm_fn

    def answer(self, question: str, top_k: int = 3) -> str:
        chunks = self.store.search(question, top_k=top_k)
        doc_ids = []
        for c in chunks:
            did = c.get("metadata", {}).get("doc_id")
            if did and did not in doc_ids:
                doc_ids.append(did)
        facts = self.graph.context(question, doc_ids)
        facts_text = "\n".join(f"- {f}" for f in facts) if facts else "Không có dữ kiện đồ thị."
        chunks_text = "\n\n".join(f"[{i+1}] {c['content']}" for i, c in enumerate(chunks))
        prompt = GRAPH_PROMPT.format(facts=facts_text, chunks=chunks_text, question=question)
        return self.llm_fn(prompt)
