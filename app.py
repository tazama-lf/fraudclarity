import json
import os
import re
import uuid
from argparse import ArgumentParser
from pathlib import Path
from time import perf_counter
from typing import Annotated, Any, TypedDict
from urllib.error import URLError
from urllib.request import urlopen

import gradio as gr
from dotenv import load_dotenv
from langchain_chroma import Chroma
from langchain_core.messages import HumanMessage, RemoveMessage, SystemMessage
from langchain_ollama import ChatOllama, OllamaEmbeddings
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
CHROMA_PATH = BASE_DIR / "chroma_db"
PROMPT_PATH = BASE_DIR / "prompts" / "system_prompt.txt"
CLAUDE_PROMPT_PATH = BASE_DIR / "prompts" / "system_prompt_claude.txt"
RULE_DOC_PATH = BASE_DIR / "docs" / "rule_trigger_discovery.md"

DEFAULT_ANSWER_PROVIDER = "ollama"
ANSWER_PROVIDER_CHOICES = ["ollama", "claude"]
PREFERRED_ANSWER_MODEL = "mistral:7b-instruct"
FALLBACK_ANSWER_MODEL = "llama3.1:8b"
CLAUDE_ANSWER_MODEL = "claude-sonnet-4-6"
JUDGE_MODEL = "heuristic-fast-judge"
PREFERRED_SUPER_JUDGE_MODEL = "mistral:7b-instruct"
EMBED_MODEL = "nomic-embed-text"
RETRIEVER_K = 3
DEFAULT_JUDGE_TOP_K = 4
MAX_CONTEXT_MESSAGES = 8
SUMMARY_TRIGGER_MESSAGES = 12
MAX_CONTEXT_CHARS_PER_DOC = 700
MAX_JUDGE_EVIDENCE_CHARS_PER_DOC = 360
MAX_JUDGE_ANSWER_CHARS = 900
MAX_JUDGE_REFERENCE_CHARS = 700
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7860
DEFAULT_MAX_PORT = 7900
OLLAMA_BASE_URL = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

if not CHROMA_PATH.exists():
    raise FileNotFoundError(f"Vector store not found at {CHROMA_PATH}. Run ingest.ipynb first.")
if not PROMPT_PATH.exists():
    raise FileNotFoundError(f"System prompt not found at {PROMPT_PATH}.")


def get_installed_ollama_models() -> list[str]:
    try:
        with urlopen(f"{OLLAMA_BASE_URL}/api/tags", timeout=2) as response:
            payload = json.load(response)
    except (URLError, TimeoutError, OSError, json.JSONDecodeError):
        return []

    return [model.get("name", "") for model in payload.get("models", []) if model.get("name")]


def resolve_model(preferred_model: str, fallback_model: str, installed_models: list[str]) -> tuple[str, bool]:
    if preferred_model in installed_models:
        return preferred_model, False
    if fallback_model in installed_models:
        return fallback_model, True
    return fallback_model, True


def normalize_answer_provider(provider_name: str | None) -> str:
    provider_name = (provider_name or DEFAULT_ANSWER_PROVIDER).strip().lower()
    return provider_name if provider_name in ANSWER_PROVIDER_CHOICES else DEFAULT_ANSWER_PROVIDER


def prompt_path_for_provider(provider_name: str) -> Path:
    if provider_name == "claude" and CLAUDE_PROMPT_PATH.exists():
        return CLAUDE_PROMPT_PATH
    return PROMPT_PATH


def build_answer_runtime(provider_name: str) -> dict[str, Any]:
    provider_name = normalize_answer_provider(provider_name)
    if provider_name in ANSWER_RUNTIME_CACHE:
        return ANSWER_RUNTIME_CACHE[provider_name]

    prompt_template = prompt_path_for_provider(provider_name).read_text(encoding="utf-8")
    if provider_name == "claude":
        if not ANTHROPIC_API_KEY:
            raise EnvironmentError("ANTHROPIC_API_KEY is not set. Add it before selecting Claude.")
        from langchain_anthropic import ChatAnthropic

        os.environ["ANTHROPIC_API_KEY"] = ANTHROPIC_API_KEY
        runtime = {
            "provider": "claude",
            "model_name": CLAUDE_ANSWER_MODEL,
            "prompt_name": prompt_path_for_provider("claude").name,
            "llm": ChatAnthropic(model=CLAUDE_ANSWER_MODEL, temperature=0, max_tokens=2048),
            "prompt_template": prompt_template,
        }
    else:
        runtime = {
            "provider": "ollama",
            "model_name": ANSWER_MODEL,
            "prompt_name": PROMPT_PATH.name,
            "llm": ChatOllama(model=ANSWER_MODEL, temperature=0, base_url=OLLAMA_BASE_URL),
            "prompt_template": prompt_template,
        }

    ANSWER_RUNTIME_CACHE[provider_name] = runtime
    return runtime


def set_answer_provider(provider_name: str) -> dict[str, Any]:
    global ANSWER_PROVIDER, ACTIVE_ANSWER_PROVIDER, ACTIVE_ANSWER_MODEL, answer_llm, system_prompt_template

    runtime = build_answer_runtime(provider_name)
    ANSWER_PROVIDER = runtime["provider"]
    ACTIVE_ANSWER_PROVIDER = runtime["provider"]
    ACTIVE_ANSWER_MODEL = runtime["model_name"]
    answer_llm = runtime["llm"]
    system_prompt_template = runtime["prompt_template"]
    return runtime


installed_models = get_installed_ollama_models()
ANSWER_MODEL, used_fallback_answer_model = resolve_model(PREFERRED_ANSWER_MODEL, FALLBACK_ANSWER_MODEL, installed_models)
SUPER_JUDGE_MODEL, used_fallback_super_judge = resolve_model(PREFERRED_SUPER_JUDGE_MODEL, ANSWER_MODEL, installed_models)
embeddings = OllamaEmbeddings(model=EMBED_MODEL, base_url=OLLAMA_BASE_URL)
vectorstore = Chroma(persist_directory=str(CHROMA_PATH), embedding_function=embeddings)
retriever = vectorstore.as_retriever(search_kwargs={"k": RETRIEVER_K})
ANSWER_RUNTIME_CACHE: dict[str, dict[str, Any]] = {}
ANSWER_PROVIDER = DEFAULT_ANSWER_PROVIDER
ACTIVE_ANSWER_PROVIDER = DEFAULT_ANSWER_PROVIDER
ACTIVE_ANSWER_MODEL = ANSWER_MODEL
system_prompt_template = prompt_path_for_provider(DEFAULT_ANSWER_PROVIDER).read_text(encoding="utf-8")
answer_llm: Any = None
super_judge_llm = ChatOllama(model=SUPER_JUDGE_MODEL, temperature=0, base_url=OLLAMA_BASE_URL)
set_answer_provider(DEFAULT_ANSWER_PROVIDER)


class State(TypedDict):
    messages: Annotated[list, add_messages]
    summary: str
    last_turn_metrics: dict[str, Any]


def trim_context_text(text: str, limit: int = MAX_CONTEXT_CHARS_PER_DOC) -> str:
    text = text.strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def format_docs(docs: list[Any]) -> str:
    return "\n\n".join(trim_context_text(doc.page_content) for doc in docs)


def recent_messages_for_answer(messages: list[Any]) -> list[Any]:
    return messages[-MAX_CONTEXT_MESSAGES:]


def call_model(state: State) -> dict[str, Any]:
    messages = state["messages"]
    summary = state.get("summary", "")
    recent_messages = recent_messages_for_answer(messages)
    latest_user_message = recent_messages[-1].content

    retrieval_start = perf_counter()
    docs = retriever.invoke(latest_user_message)
    retrieval_seconds = perf_counter() - retrieval_start

    context = format_docs(docs)
    system_message = SystemMessage(content=system_prompt_template.format(context=context, summary=summary))

    answer_start = perf_counter()
    response = answer_llm.invoke([system_message] + recent_messages)
    answer_seconds = perf_counter() - answer_start

    return {
        "messages": [response],
        "last_turn_metrics": {
            "retrieval_seconds": round(retrieval_seconds, 3),
            "answer_seconds": round(answer_seconds, 3),
            "doc_count": len(docs),
            "history_messages_used": len(recent_messages),
        },
    }


def summarize_conversation(state: State) -> dict[str, Any]:
    summary = state.get("summary", "")
    messages = state["messages"]
    messages_to_summarize = messages[:-MAX_CONTEXT_MESSAGES]
    summary_prompt = (
        "Create a concise summary of the following conversation. "
        f"Extend the existing summary: {summary}\n\nNew messages to summarize:\n"
    )
    for message in messages_to_summarize:
        summary_prompt += f"{message.type}: {message.content}\n"
    response = answer_llm.invoke([HumanMessage(content=summary_prompt)])
    delete_messages = [RemoveMessage(id=message.id) for message in messages_to_summarize if message.id]
    return {"summary": response.content, "messages": delete_messages}


def should_summarize(state: State) -> str:
    if len(state["messages"]) > SUMMARY_TRIGGER_MESSAGES:
        return "summarize_conversation"
    return END


workflow = StateGraph(State)
workflow.add_node("agent", call_model)
workflow.add_node("summarize_conversation", summarize_conversation)
workflow.add_edge(START, "agent")
workflow.add_conditional_edges("agent", should_summarize)
workflow.add_edge("summarize_conversation", END)
memory = MemorySaver()
chat_app = workflow.compile(checkpointer=memory)


def new_thread_id() -> str:
    return str(uuid.uuid4())


def trim_judge_evidence_text(text: str, limit: int = MAX_JUDGE_EVIDENCE_CHARS_PER_DOC) -> str:
    text = text.strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def trim_judge_text(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def tokenize_for_judge(text: str | None) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", (text or "").lower()))


def extract_rule_ids(text: str | None) -> set[str]:
    matches = re.findall(r"rule[\s-]?(\d{1,3})", text or "", flags=re.IGNORECASE)
    return {f"RULE-{int(match):03d}" for match in matches}


def split_judge_sentences(text: str | None) -> list[str]:
    raw_sentences = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return [sentence.strip() for sentence in raw_sentences if sentence.strip()][:6]


def load_rule_reference_evidence(rule_ids: set[str]) -> list[dict[str, str]]:
    if not rule_ids or not RULE_DOC_PATH.exists():
        return []

    rule_doc_text = RULE_DOC_PATH.read_text(encoding="utf-8")
    evidence_items = []
    for rule_id in sorted(rule_ids):
        rule_label = rule_id.title()
        pattern = rf"(?ims)^\d+\.\s+#\s+{re.escape(rule_label)}:.*?(?=^\d+\.\s+#\s+Rule-\d+:|\Z)"
        match = re.search(pattern, rule_doc_text)
        if match:
            evidence_items.append(
                {
                    "source": str(RULE_DOC_PATH.relative_to(BASE_DIR)).replace("/", "\\"),
                    "content": match.group(0).strip(),
                }
            )
    return evidence_items


def score_evidence_doc(doc: Any, question_tokens: set[str], requested_rules: set[str]) -> int:
    source = (doc.metadata.get("source", "unknown") or "").lower()
    content = doc.page_content or ""
    content_tokens = tokenize_for_judge(content)
    content_rules = extract_rule_ids(content) | extract_rule_ids(source)
    score = len(question_tokens & content_tokens)
    if "rule_trigger_discovery" in source:
        score += 8
    if source.endswith(".md"):
        score += 3
    if "transcript" in source:
        score -= 6
    if "dev-set-up" in source or "setup" in source:
        score -= 4
    if requested_rules:
        shared_rules = requested_rules & content_rules
        if shared_rules:
            score += 25 * len(shared_rules)
        else:
            score -= 10
    return score


def retrieve_evidence_items(question: str, answer: str, top_k: int = DEFAULT_JUDGE_TOP_K) -> list[dict[str, Any]]:
    requested_rules = extract_rule_ids(question)
    direct_rule_evidence = load_rule_reference_evidence(requested_rules)
    question_tokens = tokenize_for_judge(question)
    query_text = question if requested_rules else f"{question}\n\n{answer}"
    fetch_k = max(top_k * 4, 12) if requested_rules else max(top_k * 2, top_k)
    docs = vectorstore.as_retriever(search_kwargs={"k": fetch_k}).invoke(query_text)
    ranked_docs = sorted(
        docs,
        key=lambda doc: score_evidence_doc(doc, question_tokens, requested_rules),
        reverse=True,
    )

    selected_items = list(direct_rule_evidence)
    seen_pairs = {(item["source"], item["content"]) for item in selected_items}
    for doc in ranked_docs:
        score = score_evidence_doc(doc, question_tokens, requested_rules)
        if requested_rules and score <= 0:
            continue
        candidate = {
            "source": doc.metadata.get("source", "unknown"),
            "content": trim_judge_evidence_text(doc.page_content),
        }
        pair = (candidate["source"], candidate["content"])
        if pair in seen_pairs:
            continue
        selected_items.append(candidate)
        seen_pairs.add(pair)
        if len(selected_items) >= top_k:
            break

    return [{**item, "evidence_id": index} for index, item in enumerate(selected_items[:top_k], start=1)]


def render_evidence(evidence_items: list[dict[str, Any]]) -> str:
    if not evidence_items:
        return "No evidence retrieved from Chroma."
    return "\n\n".join(
        f"[Evidence {item['evidence_id']}] {item['source']}: {item['content']}" for item in evidence_items
    )


def parse_model_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("Model did not return JSON")
    return json.loads(text[start : end + 1])


def choose_reference_points(reference_answer: str | None, evidence_items: list[dict[str, Any]]) -> list[str]:
    if reference_answer and reference_answer.strip():
        return split_judge_sentences(trim_judge_text(reference_answer, MAX_JUDGE_REFERENCE_CHARS))

    points = []
    for item in evidence_items:
        for sentence in split_judge_sentences(item["content"]):
            cleaned = sentence.strip()
            if not cleaned or cleaned.startswith("#") or cleaned.startswith("|") or cleaned.startswith("**Rule name:**"):
                continue
            if cleaned.startswith("**Summary:**"):
                cleaned = cleaned.replace("**Summary:**", "", 1).strip()
            points.append(cleaned)
            if len(points) >= 4:
                return points[:4]
    return points[:4]


def score_answer_sentences(answer_sentences: list[str], evidence_text: str, evidence_rules: set[str]) -> tuple[list[str], list[str]]:
    evidence_tokens = tokenize_for_judge(evidence_text)
    supported = []
    unsupported = []

    for sentence in answer_sentences:
        sentence_tokens = tokenize_for_judge(sentence)
        if not sentence_tokens:
            continue
        overlap_ratio = len(sentence_tokens & evidence_tokens) / max(1, len(sentence_tokens))
        sentence_rules = extract_rule_ids(sentence)
        has_rule_support = not sentence_rules or bool(sentence_rules & evidence_rules)
        if overlap_ratio >= 0.35 and has_rule_support:
            supported.append(sentence)
        else:
            unsupported.append(sentence)

    return supported[:2], unsupported[:2]


def judge_answer_basic(
    question: str,
    answer: str,
    reference_answer: str | None = None,
    top_k: int = DEFAULT_JUDGE_TOP_K,
    evidence_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    evidence_items = evidence_items or retrieve_evidence_items(question, answer, top_k=top_k)
    answer_sentences = split_judge_sentences(trim_judge_text(answer, MAX_JUDGE_ANSWER_CHARS))
    evidence_text = render_evidence(evidence_items)
    evidence_rules = set().union(*(extract_rule_ids(item["content"]) for item in evidence_items)) if evidence_items else set()
    question_rules = extract_rule_ids(question)
    answer_rules = extract_rule_ids(answer)
    supported_points, unsupported_points = score_answer_sentences(answer_sentences, evidence_text, evidence_rules)

    reference_points = choose_reference_points(reference_answer, evidence_items)
    missing_points = []
    answer_tokens = tokenize_for_judge(answer)
    for point in reference_points:
        point_tokens = tokenize_for_judge(point)
        if point_tokens and len(point_tokens & answer_tokens) / max(1, len(point_tokens)) < 0.35:
            missing_points.append(point)
        if len(missing_points) >= 2:
            break

    if question_rules and not answer_rules:
        missing_points.insert(0, f"Missing explicit rule reference: {sorted(question_rules)[0]}")
        missing_points = missing_points[:2]
    elif evidence_rules and not answer_rules:
        missing_points.insert(0, f"Missing explicit rule reference: {sorted(evidence_rules)[0]}")
        missing_points = missing_points[:2]

    total_claims = max(1, len(supported_points) + len(unsupported_points))
    accuracy_score = round(100 * len(supported_points) / total_claims)
    groundedness_score = accuracy_score
    completeness_base = max(1, len(reference_points[:2]))
    completeness_score = round(100 * (completeness_base - len(missing_points[:2])) / completeness_base)
    overall_score = round(0.45 * accuracy_score + 0.30 * groundedness_score + 0.25 * completeness_score)
    hallucination_ratio = len(unsupported_points) / total_claims
    hallucination_risk = "high" if hallucination_ratio >= 0.5 else "medium" if hallucination_ratio >= 0.2 else "low"
    verdict = "Fast heuristic judge based on evidence overlap."
    if unsupported_points and not supported_points:
        verdict = "Answer has weak evidence overlap and likely needs review."
    elif missing_points:
        verdict = "Answer is partly grounded but misses some important evidence points."
    elif supported_points:
        verdict = "Answer appears grounded in the retrieved evidence."

    return {
        "overall_score": overall_score,
        "accuracy_score": accuracy_score,
        "groundedness_score": groundedness_score,
        "completeness_score": completeness_score,
        "hallucination_risk": hallucination_risk,
        "verdict": verdict,
        "supported_points": supported_points,
        "unsupported_or_incorrect_points": unsupported_points,
        "missing_points": missing_points[:2],
        "judge_model": JUDGE_MODEL,
        "evidence": evidence_items,
    }


def safe_ratio(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        return 0
    return round(100 * numerator / denominator)


def build_super_judge_prompt(question: str, answer: str, evidence_text: str, reference_answer: str | None = None) -> str:
    answer_block = trim_judge_text(answer, MAX_JUDGE_ANSWER_CHARS)
    reference_block = trim_judge_text(reference_answer, MAX_JUDGE_REFERENCE_CHARS) if reference_answer else "No reference answer provided."
    return f"""
Evaluate the answer using only the evidence. Return compact JSON only.

JSON schema:
{{
  \"atomic_claims\": [{{\"claim\": \"short claim\", \"status\": \"supported|contradicted|unverifiable\", \"evidence_ids\": [1], \"reason\": \"short reason\"}}],
  \"required_points\": [{{\"point\": \"short point\", \"covered\": true, \"evidence_ids\": [1], \"reason\": \"short reason\"}}],
  \"rule_checks\": [{{\"rule\": \"Rule-001\", \"required\": true, \"satisfied\": true, \"reason\": \"short reason\"}}],
  \"summary\": \"one short sentence\"
}}

Limits:
- At most 4 atomic_claims.
- At most 4 required_points.
- At most 3 rule_checks.
- Keep every reason short.

Question:
{question}

Answer:
{answer_block}

Reference:
{reference_block}

Evidence:
{evidence_text}
""".strip()


def compute_super_judge_scores(classification: dict[str, Any]) -> dict[str, Any]:
    atomic_claims = classification.get("atomic_claims", [])
    required_points = classification.get("required_points", [])
    rule_checks = [item for item in classification.get("rule_checks", []) if item.get("required", True)]
    total_claims = len(atomic_claims)
    supported_claims = sum(1 for item in atomic_claims if item.get("status") == "supported")
    contradicted_claims = sum(1 for item in atomic_claims if item.get("status") == "contradicted")
    evidence_backed_claims = sum(1 for item in atomic_claims if item.get("evidence_ids"))
    total_required_points = len(required_points)
    covered_required_points = sum(1 for item in required_points if item.get("covered"))
    total_rule_checks = len(rule_checks)
    satisfied_rule_checks = sum(1 for item in rule_checks if item.get("satisfied"))
    accuracy_score = 0 if total_claims == 0 else max(0, round(100 * (supported_claims - contradicted_claims) / total_claims))
    groundedness_score = 0 if total_claims == 0 else safe_ratio(evidence_backed_claims, total_claims)
    completeness_score = safe_ratio(covered_required_points, total_required_points)
    rule_specificity_score = safe_ratio(satisfied_rule_checks, total_rule_checks)
    overall_score = round(0.45 * accuracy_score + 0.20 * groundedness_score + 0.25 * completeness_score + 0.10 * rule_specificity_score)
    supported_points = [item["claim"] for item in atomic_claims if item.get("status") == "supported"]
    unsupported_or_incorrect_points = [item["claim"] for item in atomic_claims if item.get("status") in {"contradicted", "unverifiable"}]
    missing_points = [item["point"] for item in required_points if not item.get("covered")]
    critical_failures = [item["claim"] for item in atomic_claims if item.get("status") == "contradicted"]
    unsupported_ratio = 0 if total_claims == 0 else len(unsupported_or_incorrect_points) / total_claims
    hallucination_risk = "high" if contradicted_claims > 0 or unsupported_ratio >= 0.50 else "medium" if unsupported_ratio >= 0.20 else "low"
    return {
        "overall_score": overall_score,
        "accuracy_score": accuracy_score,
        "groundedness_score": groundedness_score,
        "completeness_score": completeness_score,
        "rule_specificity_score": rule_specificity_score,
        "hallucination_risk": hallucination_risk,
        "supported_points": supported_points,
        "unsupported_or_incorrect_points": unsupported_or_incorrect_points,
        "missing_points": missing_points,
        "critical_failures": critical_failures,
    }


def judge_answer_super(
    question: str,
    answer: str,
    reference_answer: str | None = None,
    top_k: int = DEFAULT_JUDGE_TOP_K,
    evidence_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    evidence_items = evidence_items or retrieve_evidence_items(question, answer, top_k=top_k)
    prompt = build_super_judge_prompt(question, answer, render_evidence(evidence_items), reference_answer=reference_answer)
    classification = parse_model_json(super_judge_llm.invoke(prompt).content)
    scores = compute_super_judge_scores(classification)
    return {
        **scores,
        "judge_model": SUPER_JUDGE_MODEL,
        "classification": classification,
        "evidence": evidence_items,
        "verdict": classification.get("summary", ""),
    }


def load_rule_reference_sections(rule_ids: list[str]) -> list[dict[str, str]]:
    if not rule_ids or not RULE_DOC_PATH.exists():
        return []

    rule_doc_text = RULE_DOC_PATH.read_text(encoding="utf-8")
    sections = []
    for rule_id in sorted(rule_ids):
        rule_label = rule_id.title()
        pattern = rf"(?ims)^\d+\.\s+#\s+{re.escape(rule_label)}:.*?(?=^\d+\.\s+#\s+Rule-\d+:|\Z)"
        match = re.search(pattern, rule_doc_text)
        if match:
            sections.append(
                {
                    "rule_id": rule_id,
                    "source": str(RULE_DOC_PATH.relative_to(BASE_DIR)).replace("/", "\\"),
                    "content": match.group(0).strip(),
                }
            )
    return sections


def parse_rule_section(section_text: str, rule_id: str) -> dict[str, Any]:
    title_match = re.search(r"^\d+\.\s+#\s+(Rule-\d+:\s+.+)$", section_text, flags=re.IGNORECASE | re.MULTILINE)
    rule_name_match = re.search(r"\*\*Rule name:\*\*\s*(.+)", section_text)
    summary_match = re.search(
        r"\*\*Summary:\*\*\s*(.+?)(?=\n\*\*|\n\d+\.\s+#\s+Rule-\d+:|\Z)",
        section_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    params_match = re.search(
        r"\*\*Query parameters:\*\*\s*(.+?)(?=\n\*\*Note:|\n\*\*Returned column:|\n\*\*Post-query processing:|\n\d+\.\s+#\s+Rule-\d+:|\Z)",
        section_text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    title = title_match.group(1).strip() if title_match else rule_id.title()
    title = re.sub(r"\s*\{#.+\}$", "", title).strip()
    rule_name = rule_name_match.group(1).strip() if rule_name_match else ""
    summary = re.sub(r"\s+", " ", summary_match.group(1)).strip() if summary_match else ""

    parameter_names = []
    if params_match:
        for line in params_match.group(1).splitlines():
            line = line.strip()
            if not line.startswith("|"):
                continue
            cells = [cell.strip() for cell in line.split("|")[1:-1]]
            if not cells or cells[0].lower() == "parameter" or set(cells[0]) == {":", "-"}:
                continue
            parameter_names.append(cells[0])

    return {
        "title": title,
        "rule_name": rule_name,
        "summary_sentences": split_judge_sentences(summary),
        "parameter_names": parameter_names,
    }


def build_grounded_rule_answer(question: str) -> dict[str, Any] | None:
    requested_rules = sorted(extract_rule_ids(question))
    sections = load_rule_reference_sections(requested_rules)
    if not sections:
        return None

    answer_blocks = []
    for section in sections[:3]:
        parsed = parse_rule_section(section["content"], section["rule_id"])
        lines = [parsed["title"]]
        if parsed["rule_name"]:
            lines.append(f"Rule name: {parsed['rule_name']}")

        summary_sentences = parsed["summary_sentences"][:3]
        if summary_sentences:
            lines.append(" ".join(summary_sentences))

        if len(parsed["parameter_names"]) == 1:
            lines.append(f"The parameter {parsed['parameter_names'][0]} is sourced from the rule configuration.")
        elif len(parsed["parameter_names"]) > 1:
            if len(parsed["parameter_names"]) > 2:
                params_text = ", ".join(parsed["parameter_names"][:-1]) + f" and {parsed['parameter_names'][-1]}"
            else:
                params_text = " and ".join(parsed["parameter_names"])
            lines.append(f"Both {params_text} are sourced from the rule configuration.")

        answer_blocks.append("\n".join(lines))

    return {
        "answer": "\n\n".join(answer_blocks),
        "doc_count": len(sections),
        "sources": [section["source"] for section in sections],
    }


def ask_core(question: str, thread_id: str) -> dict[str, Any]:
    grounded_start = perf_counter()
    grounded_rule_answer = build_grounded_rule_answer(question)
    if grounded_rule_answer:
        retrieval_seconds = perf_counter() - grounded_start
        return {
            "answer": grounded_rule_answer["answer"],
            "metrics": {
                "retrieval_seconds": round(retrieval_seconds, 3),
                "answer_seconds": 0.0,
                "doc_count": grounded_rule_answer["doc_count"],
                "history_messages_used": 1,
                "grounded_rule_answer": True,
            },
        }

    config = {"configurable": {"thread_id": thread_id}}
    result = chat_app.invoke({"messages": [HumanMessage(content=question)]}, config)
    return {
        "answer": result["messages"][-1].content,
        "metrics": result.get("last_turn_metrics", {}),
    }


def build_judge_markdown(title: str, result: dict[str, Any] | None) -> str:
    if not result:
        return ""
    lines = [
        f"### {title}",
        f"Overall: {result.get('overall_score', 0)}%",
        f"Accuracy: {result.get('accuracy_score', 0)}%",
        f"Groundedness: {result.get('groundedness_score', 0)}%",
        f"Completeness: {result.get('completeness_score', 0)}%",
    ]
    if "rule_specificity_score" in result:
        lines.append(f"Rule specificity: {result.get('rule_specificity_score', 0)}%")
    lines.append(f"Hallucination risk: {result.get('hallucination_risk', 'unknown')}")
    if result.get("verdict"):
        lines.extend(["", result["verdict"]])
    for label, key in [
        ("Supported points", "supported_points"),
        ("Unsupported or incorrect points", "unsupported_or_incorrect_points"),
        ("Missing points", "missing_points"),
    ]:
        items = result.get(key) or []
        if items:
            lines.extend(["", f"{label}:"])
            lines.extend([f"- {item}" for item in items[:4]])
    return "\n".join(lines)


def build_evidence_markdown(evidence_items: list[dict[str, Any]]) -> str:
    if not evidence_items:
        return "No evidence retrieved."
    blocks = []
    for item in evidence_items:
        snippet = item["content"].strip().replace("\n", " ")
        if len(snippet) > 320:
            snippet = snippet[:317] + "..."
        blocks.append(f"- Evidence {item['evidence_id']} from {item['source']}: {snippet}")
    return "\n".join(blocks)


def build_answer_runtime_markdown(provider_name: str) -> str:
    provider_name = normalize_answer_provider(provider_name)
    if provider_name == "claude":
        status_line = "Ready" if ANTHROPIC_API_KEY else "Set ANTHROPIC_API_KEY to enable Claude."
        return (
            "### Runtime\n"
            f"- Provider: Claude\n"
            f"- Model: {CLAUDE_ANSWER_MODEL}\n"
            f"- Status: {status_line}\n"
            f"- Cached: {'Yes' if 'claude' in ANSWER_RUNTIME_CACHE else 'On first use'}"
        )

    return (
        "### Runtime\n"
        f"- Provider: Ollama\n"
        f"- Model: {ANSWER_MODEL}\n"
        f"- Status: {'Fallback model in use' if used_fallback_answer_model else 'Ready'}\n"
        f"- Cached: {'Yes' if 'ollama' in ANSWER_RUNTIME_CACHE else 'On first use'}"
    )


def build_timing_markdown(timing_metrics: dict[str, Any] | None) -> str:
    if not timing_metrics:
        return "### Timings\n- No timing data yet."

    lines = ["### Timings"]
    ordered_fields = [
        ("provider_switch_seconds", "Provider switch"),
        ("retrieval_seconds", "Retrieval"),
        ("answer_seconds", "Answer generation"),
        ("judge_retrieval_seconds", "Judge retrieval"),
        ("judge_basic_seconds", "Judge"),
        ("judge_super_seconds", "Super judge"),
        ("total_seconds", "Total turn"),
    ]
    for key, label in ordered_fields:
        value = timing_metrics.get(key)
        if value is not None:
            lines.append(f"- {label}: {value:.2f}s")

    doc_count = timing_metrics.get("doc_count")
    if doc_count is not None:
        lines.append(f"- Retrieved chunks: {doc_count}")
    if timing_metrics.get("grounded_rule_answer"):
        lines.append("- Grounded rule answer path: Yes")

    return "\n".join(lines)


def build_side_panel(
    answer_text: str,
    answer_provider: str,
    answer_model: str,
    judge_mode: str,
    timing_metrics: dict[str, Any] | None = None,
    basic_result: dict[str, Any] | None = None,
    super_result: dict[str, Any] | None = None,
    show_evidence: bool = False,
) -> str:
    provider_label = "Claude" if answer_provider == "claude" else "Ollama"
    sections = [
        "## FraudClarity answer",
        answer_text,
        "### Session",
        f"- Provider: {provider_label}",
        f"- Model: {answer_model}",
        f"- Judge mode: {judge_mode}",
        build_timing_markdown(timing_metrics),
    ]
    if judge_mode in {"judge", "both"} and basic_result:
        sections.extend(["", build_judge_markdown("AI Judge", basic_result)])
    if judge_mode in {"super_judge", "both"} and super_result:
        sections.extend(["", build_judge_markdown("AI Super Judge", super_result)])
    evidence_items = basic_result.get("evidence") if basic_result and basic_result.get("evidence") else super_result.get("evidence") if super_result else []
    if show_evidence and evidence_items:
        sections.extend(["", "### Evidence", build_evidence_markdown(evidence_items)])
    return "\n\n".join(sections)


def run_chat_turn(
    message: str,
    history: list[dict[str, str]] | None,
    thread_id: str | None,
    answer_provider: str,
    judge_mode: str,
    reference_answer: str,
    top_k: int,
    show_evidence: bool,
) -> tuple[list[dict[str, str]], str, str, str, str]:
    history = history or []
    thread_id = thread_id or new_thread_id()
    message = (message or "").strip()

    if not message:
        return history, thread_id, "", "Enter a fraud question.", "{}"

    request_start = perf_counter()
    try:
        provider_start = perf_counter()
        runtime = set_answer_provider(answer_provider)
        provider_switch_seconds = perf_counter() - provider_start
    except Exception as exc:
        panel_markdown = "## Answer runtime unavailable\n\n" + str(exc)
        detail_payload = {
            "question": message,
            "answer_provider": normalize_answer_provider(answer_provider),
            "error": str(exc),
        }
        return history, thread_id, message, panel_markdown, json.dumps(detail_payload, indent=2)

    answer_result = ask_core(message, thread_id)
    answer_text = answer_result["answer"]
    timing_metrics = {
        "provider_switch_seconds": round(provider_switch_seconds, 3),
        **answer_result.get("metrics", {}),
    }

    basic_result = None
    super_result = None
    evidence_items = None
    if judge_mode != "none":
        judge_retrieval_start = perf_counter()
        evidence_items = retrieve_evidence_items(message, answer_text, top_k=int(top_k))
        timing_metrics["judge_retrieval_seconds"] = round(perf_counter() - judge_retrieval_start, 3)
        timing_metrics["judge_doc_count"] = len(evidence_items)

    if judge_mode in {"judge", "both"}:
        basic_start = perf_counter()
        basic_result = judge_answer_basic(
            message,
            answer_text,
            reference_answer=reference_answer or None,
            top_k=int(top_k),
            evidence_items=evidence_items,
        )
        timing_metrics["judge_basic_seconds"] = round(perf_counter() - basic_start, 3)

    if judge_mode in {"super_judge", "both"}:
        super_start = perf_counter()
        super_result = judge_answer_super(
            message,
            answer_text,
            reference_answer=reference_answer or None,
            top_k=int(top_k),
            evidence_items=evidence_items,
        )
        timing_metrics["judge_super_seconds"] = round(perf_counter() - super_start, 3)

    timing_metrics["total_seconds"] = round(perf_counter() - request_start, 3)

    badges = [runtime["model_name"], f"{timing_metrics['total_seconds']:.1f}s"]
    if basic_result:
        badges.append(f"judge {basic_result.get('overall_score', 0)}%")
    if super_result:
        badges.append(f"super judge {super_result.get('overall_score', 0)}%")

    updated_history = history + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": answer_text + "\n\n" + " | ".join(badges)},
    ]
    detail_payload = {
        "question": message,
        "answer": answer_text,
        "answer_provider": runtime["provider"],
        "answer_model": runtime["model_name"],
        "judge_mode": judge_mode,
        "timings": timing_metrics,
        "basic_judge": basic_result,
        "super_judge": super_result,
    }
    panel_markdown = build_side_panel(
        answer_text,
        runtime["provider"],
        runtime["model_name"],
        judge_mode,
        timing_metrics=timing_metrics,
        basic_result=basic_result,
        super_result=super_result,
        show_evidence=show_evidence,
    )
    return updated_history, thread_id, "", panel_markdown, json.dumps(detail_payload, indent=2)


def clear_chat() -> tuple[list[dict[str, str]], str, str, str, str]:
    return [], new_thread_id(), "", "Conversation cleared. New session started.", "{}"


APP_THEME = gr.themes.Soft(primary_hue="emerald", neutral_hue="slate")
APP_CSS = """
:root {
    color-scheme: light;
    --fc-bg-top: #eef8f4;
    --fc-bg-bottom: #e3f3ee;
    --fc-shell: #f9fffc;
    --fc-panel: #ffffff;
    --fc-panel-soft: #f6fcfa;
    --fc-border: #bfdcd3;
    --fc-border-strong: #7ea69d;
    --fc-ink: #083b34;
    --fc-ink-soft: #0f544a;
    --fc-muted: #44665f;
    --fc-accent: #58cfb0;
    --fc-accent-strong: #045046;
    --fc-user-bg: #def7ef;
    --fc-bot-bg: #ffffff;
    --fc-shadow: 0 18px 56px rgba(4, 80, 70, 0.12);
    --fc-shadow-soft: 0 8px 24px rgba(4, 80, 70, 0.08);
}
html, body {
    height: 100%;
    overflow: hidden;
    background: var(--fc-bg-bottom) !important;
}
body, .gradio-container, .gradio-container .dark, .gradio-container .light {
    color-scheme: light !important;
    min-height: 100vh !important;
    height: 100vh !important;
    overflow: hidden !important;
    background:
        radial-gradient(circle at top left, rgba(88, 207, 176, 0.16) 0%, rgba(88, 207, 176, 0) 30%),
        linear-gradient(180deg, var(--fc-bg-top) 0%, var(--fc-bg-bottom) 100%) !important;
    color: var(--fc-ink) !important;
    font-family: Segoe UI, Arial, sans-serif !important;
}
.gradio-container, .gradio-container .dark, .gradio-container .light {
    --body-background-fill: transparent !important;
    --body-text-color: var(--fc-ink) !important;
    --color-accent: var(--fc-accent-strong) !important;
    --button-primary-background-fill: var(--fc-accent-strong) !important;
    --button-primary-text-color: #ffffff !important;
    --button-secondary-text-color: var(--fc-ink) !important;
    --button-secondary-background-fill: #ffffff !important;
    --block-background-fill: transparent !important;
    --block-border-color: transparent !important;
    --input-background-fill: #ffffff !important;
    --input-border-color: var(--fc-border) !important;
    --input-text-color: var(--fc-ink) !important;
    --input-placeholder-color: var(--fc-muted) !important;
    --checkbox-label-text-color: var(--fc-ink) !important;
    --code-background-fill: #f6fcfa !important;
    --code-text-color: var(--fc-ink) !important;
    padding: 6px !important;
    box-sizing: border-box;
}
.gradio-container *,
.gradio-container *::placeholder {
    color: var(--fc-ink) !important;
    -webkit-text-fill-color: currentColor !important;
}
.gradio-container svg {
    fill: var(--fc-ink) !important;
    stroke: var(--fc-ink) !important;
}
.gradio-container main,
.gradio-container .app,
.gradio-container .fillable {
    padding: 0 !important;
    background: transparent !important;
}
#fc-app {
    width: 100%;
    height: calc(100vh - 12px);
    display: grid;
    grid-template-rows: auto auto minmax(0, 1fr) auto auto;
    gap: 8px;
    background: var(--fc-shell);
    border: 1px solid #d7ebe4;
    border-radius: 22px;
    box-shadow: var(--fc-shadow);
    backdrop-filter: blur(18px);
    overflow: hidden;
    padding: 10px;
}
#fc-header {
    display: flex;
    align-items: center;
    gap: 12px;
    min-height: 0;
    padding: 4px 4px 0 4px;
}
#fc-logo {
    width: 156px;
    max-width: 34vw;
    height: auto;
    display: block;
}
#fc-header-copy h1 {
    margin: 0;
    font-size: 22px;
    line-height: 1.05;
    letter-spacing: -0.02em;
    color: var(--fc-ink) !important;
}
#fc-header-copy p {
    margin: 4px 0 0 0;
    font-size: 13px;
    color: var(--fc-muted) !important;
    line-height: 1.35;
}
#fc-options,
#fc-detailsbox {
    border: 1px solid var(--fc-border) !important;
    border-radius: 16px !important;
    background: var(--fc-panel) !important;
    box-shadow: var(--fc-shadow-soft);
    overflow: hidden;
}
#fc-options button,
#fc-detailsbox button,
#fc-options summary,
#fc-detailsbox summary,
#fc-detailtabs button {
    background: var(--fc-panel) !important;
    color: var(--fc-ink) !important;
}
#fc-options .label-wrap span,
#fc-detailsbox .label-wrap span,
#fc-options .label-wrap,
#fc-detailsbox .label-wrap {
    font-size: 13px !important;
    font-weight: 600 !important;
    color: var(--fc-ink) !important;
}
.fc-compact-markdown,
.fc-compact-markdown *,
.gradio-container [data-testid='markdown'],
.gradio-container [data-testid='markdown'] * {
    color: var(--fc-ink) !important;
}
.fc-compact-markdown p,
.fc-compact-markdown li,
.fc-compact-markdown h1,
.fc-compact-markdown h2,
.fc-compact-markdown h3,
.fc-compact-markdown span,
.fc-compact-markdown div {
    font-size: 12px !important;
    line-height: 1.4 !important;
}
.fc-option-grid {
    gap: 8px !important;
}
.gradio-container label,
.gradio-container legend,
.gradio-container p,
.gradio-container span,
.gradio-container li,
.gradio-container h1,
.gradio-container h2,
.gradio-container h3,
.gradio-container h4,
.gradio-container .prose,
.gradio-container .prose * {
    color: var(--fc-ink) !important;
}
.gradio-container label,
.gradio-container legend {
    font-size: 12px !important;
}
.gradio-container [data-testid='dropdown'] label,
.gradio-container [data-testid='textbox'] label,
.gradio-container [data-testid='checkbox'] + label,
.gradio-container [data-testid='slider'] label,
.gradio-container [data-testid='accordion'] label {
    font-size: 12px !important;
    color: var(--fc-ink) !important;
}
.gradio-container [data-testid='dropdown'] button,
.gradio-container [data-testid='dropdown'] input,
.gradio-container [data-testid='dropdown'] span,
.gradio-container [data-testid='dropdown'] svg,
.gradio-container [data-testid='dropdown'] * {
    color: var(--fc-ink) !important;
    fill: var(--fc-ink) !important;
}
.gradio-container [data-testid='dropdown'] > div,
.gradio-container [data-testid='dropdown'] button[role='combobox'],
.gradio-container [data-testid='dropdown'] .wrap,
.gradio-container [data-testid='dropdown'] .secondary-wrap,
.gradio-container textarea,
.gradio-container input:not([type='checkbox']),
.gradio-container select,
.gradio-container button[role='combobox'],
.gradio-container [data-testid='textbox'] textarea,
.gradio-container [data-testid='textbox'] input,
.gradio-container [data-testid='slider'] input,
.gradio-container [role='tab'],
.gradio-container [data-testid='accordion'] {
    border-radius: 14px !important;
    border: 1px solid var(--fc-border) !important;
    background: #ffffff !important;
    color: var(--fc-ink) !important;
    box-shadow: none !important;
    font-size: 13px !important;
}
.gradio-container [data-testid='dropdown'] > div,
.gradio-container [data-testid='dropdown'] button[role='combobox'],
.gradio-container [data-testid='dropdown'] .wrap,
.gradio-container [data-testid='dropdown'] .secondary-wrap,
.gradio-container [role='listbox'],
.gradio-container [role='option'],
.gradio-container .choices,
.gradio-container .choices__list,
.gradio-container .choices__inner,
.gradio-container .choices__item {
    border-radius: 0 !important;
}
.gradio-container [data-testid='accordion'] {
    background: var(--fc-panel) !important;
}
.gradio-container [data-testid='accordion'] * {
    color: var(--fc-ink) !important;
}
.gradio-container [data-testid='checkbox'] {
    appearance: auto !important;
    -webkit-appearance: checkbox !important;
    width: 16px !important;
    height: 16px !important;
    min-width: 16px !important;
    min-height: 16px !important;
    margin: 0 !important;
    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
    accent-color: var(--fc-accent-strong) !important;
    cursor: pointer !important;
    vertical-align: middle;
}
.gradio-container [data-testid='checkbox'] + label,
.gradio-container [data-testid='checkbox'] ~ label,
.gradio-container [data-testid='checkbox'] + span,
.gradio-container [data-testid='checkbox'] ~ span {
    color: var(--fc-ink) !important;
    cursor: pointer !important;
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
}
.gradio-container [role='listbox'],
.gradio-container [role='option'],
.gradio-container .choices,
.gradio-container .choices__list,
.gradio-container .choices__inner,
.gradio-container .choices__item,
.gradio-container .choices__placeholder {
    background: #ffffff !important;
    color: var(--fc-ink) !important;
    border-color: var(--fc-border) !important;
}
.gradio-container [role='option'][aria-selected='true'],
.gradio-container [role='option']:hover {
    background: #e9f7f3 !important;
    color: var(--fc-ink) !important;
}
.gradio-container [data-testid='dropdown'],
.gradio-container [data-testid='dropdown'] > div,
.gradio-container input[role='listbox'],
.gradio-container [data-testid='dropdown'] button[role='combobox'],
.gradio-container [data-testid='dropdown'] .wrap,
.gradio-container [data-testid='dropdown'] .secondary-wrap,
.gradio-container [role='listbox'],
.gradio-container [role='option'],
.gradio-container .choices,
.gradio-container .choices__list,
.gradio-container .choices__inner,
.gradio-container .choices__item {
    border-radius: 0 !important;
}
.gradio-container pre,
.gradio-container code,
.gradio-container [data-testid='code'],
.gradio-container [data-testid='code'] * {
    background: #f6fcfa !important;
    color: var(--fc-ink) !important;
}
#fc-chatbot {
    min-height: 0 !important;
    height: 100% !important;
    display: flex !important;
    flex-direction: column !important;
    border: 1px solid var(--fc-border) !important;
    border-radius: 18px !important;
    background: var(--fc-panel-soft) !important;
    padding: 6px 8px !important;
    overflow: hidden !important;
    box-shadow: var(--fc-shadow-soft);
}
#fc-chatbot > .wrapper,
#fc-chatbot .bubble-wrap {
    flex: 1 1 auto !important;
    min-height: 0 !important;
    height: 100% !important;
    display: flex !important;
    flex-direction: column !important;
}
#fc-chatbot .bubble-wrap {
    overflow-y: auto !important;
}
#fc-chatbot label {
    display: none !important;
}
#fc-chatbot > .wrap,
#fc-chatbot .wrapper,
#fc-chatbot .bubble-wrap,
#fc-chatbot .message-wrap {
    width: 100%;
    max-width: 980px;
    margin-left: auto;
    margin-right: auto;
}
#fc-chatbot .message.user,
#fc-chatbot [data-testid='chatbot-user'] {
    background: var(--fc-user-bg) !important;
    border: 1px solid #9ad7c5 !important;
}
#fc-chatbot .message.bot,
#fc-chatbot [data-testid='chatbot-bot'] {
    background: var(--fc-bot-bg) !important;
    border: 1px solid var(--fc-border) !important;
}
#fc-chatbot .message,
#fc-chatbot .message *,
#fc-chatbot .bubble,
#fc-chatbot .bubble *,
#fc-chatbot markdown,
#fc-chatbot markdown * {
    color: var(--fc-ink) !important;
    line-height: 1.5;
    font-size: 13px !important;
}
#fc-composerwrap {
    padding: 0;
}
#fc-composer {
    width: 100%;
    margin: 0 auto;
    padding: 8px;
    border-radius: 18px;
    background: #ffffff !important;
    border: 1px solid var(--fc-border) !important;
    box-shadow: var(--fc-shadow-soft);
}
#fc-input textarea,
#fc-input input {
    min-height: 48px !important;
    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
    padding: 4px 6px 0 6px !important;
    font-size: 13px !important;
    color: var(--fc-ink) !important;
}
#fc-input textarea::placeholder,
#fc-input input::placeholder {
    color: var(--fc-muted) !important;
}
#fc-controls {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 96px;
    gap: 8px;
    align-items: end;
}
#fc-send button,
.gradio-container .primary {
    min-height: 48px;
    border-radius: 14px !important;
    border: 1px solid transparent !important;
    background: linear-gradient(135deg, var(--fc-accent-strong) 0%, #05685a 100%) !important;
    color: #ffffff !important;
    -webkit-text-fill-color: #ffffff !important;
    font-weight: 600 !important;
    box-shadow: 0 10px 20px rgba(4, 80, 70, 0.18);
}
#fc-clear button,
.gradio-container .secondary {
    border-radius: 14px !important;
    background: #ffffff !important;
    color: var(--fc-ink) !important;
    border: 1px solid var(--fc-border-strong) !important;
}
.gradio-container input[type='range'] {
    accent-color: var(--fc-accent-strong) !important;
}
#fc-detailtabs [role='tablist'] {
    gap: 6px;
}
#fc-detailtabs [role='tab'] {
    border-radius: 999px !important;
    font-size: 12px !important;
    background: #ffffff !important;
    color: var(--fc-ink) !important;
}
#fc-detailtabs button[aria-selected='true'] {
    background: #e9f7f3 !important;
    color: var(--fc-ink) !important;
}
a[href*='gradio.app'],
.gradio-container footer {
    display: none !important;
}
@media (max-width: 900px) {
    .gradio-container,
    .gradio-container .dark,
    .gradio-container .light {
        padding: 0 !important;
    }
    #fc-app {
        height: 100vh;
        border-radius: 0;
        border: none;
        padding: 8px;
        gap: 6px;
    }
    #fc-header {
        gap: 10px;
        align-items: flex-start;
    }
    #fc-logo {
        width: 118px;
        max-width: 30vw;
    }
    #fc-header-copy h1 {
        font-size: 18px;
    }
    #fc-header-copy p {
        font-size: 12px;
    }
    #fc-controls {
        grid-template-columns: minmax(0, 1fr) 88px;
    }
    #fc-send button,
    .gradio-container .primary {
        min-height: 44px;
    }
    #fc-input textarea,
    #fc-input input {
        min-height: 44px !important;
    }
}
.fc-compact-markdown > *:first-child {
    margin-top: 0 !important;
}
.fc-compact-markdown > *:last-child {
    margin-bottom: 0 !important;
}
"""


def create_demo() -> gr.Blocks:
        with gr.Blocks(theme=APP_THEME, css=APP_CSS, title="FraudClarity Chatbot") as demo:
                thread_state = gr.State(new_thread_id())
                history_state = gr.State([])

                with gr.Column(elem_id="fc-app"):
                        gr.Markdown(
                                """
<div id='fc-header'>
    <img id='fc-logo' src='https://www.tazama.org/wp-content/uploads/sites/22/2025/02/Tazama_Primary.svg' alt='Tazama logo' />
    <div id='fc-header-copy'>
        <h1>FraudClarity Chatbot</h1>
        <p>Ask about fraud rules, evidence, triggers, and transaction-monitoring logic.</p>
    </div>
</div>
"""
                        )

                        with gr.Accordion("Chat options", open=False, elem_id="fc-options"):
                                with gr.Row(elem_classes=["fc-option-grid"]):
                                        answer_provider = gr.Dropdown(
                                                choices=ANSWER_PROVIDER_CHOICES,
                                                value=DEFAULT_ANSWER_PROVIDER,
                                                label="Answer provider",
                                        )
                                        judge_mode = gr.Dropdown(
                                                choices=["none", "judge", "super_judge", "both"],
                                                value="none",
                                                label="Judge mode",
                                        )
                                with gr.Row(elem_classes=["fc-option-grid"]):
                                        top_k = gr.Slider(1, 12, value=DEFAULT_JUDGE_TOP_K, step=1, label="Evidence depth")
                                        show_evidence = gr.Checkbox(value=False, label="Show evidence")
                                with gr.Accordion("Reference answer", open=False):
                                        reference_answer = gr.Textbox(
                                                label="Reference answer",
                                                lines=3,
                                                placeholder="Optional gold answer for stricter judging",
                                        )
                                model_status = gr.Markdown(
                                        build_answer_runtime_markdown(DEFAULT_ANSWER_PROVIDER),
                                        elem_classes=["fc-compact-markdown"],
                                )
                                clear_button = gr.Button("Clear conversation", elem_id="fc-clear")

                        chatbot = gr.Chatbot(label="FraudClarity", type="messages", elem_id="fc-chatbot", allow_tags=False)

                        with gr.Column(elem_id="fc-composerwrap"):
                                with gr.Row(elem_id="fc-composer"):
                                        with gr.Row(elem_id="fc-controls"):
                                                prompt_box = gr.Textbox(
                                                        label="Message",
                                                        lines=2,
                                                        placeholder="Ask why a rule fired or how a fraud pattern is detected",
                                                        elem_id="fc-input",
                                                        scale=9,
                                                )
                                                send_button = gr.Button("Send", variant="primary", elem_id="fc-send", scale=2)

                        with gr.Accordion("Answer details", open=False, elem_id="fc-detailsbox"):
                                with gr.Tabs(elem_id="fc-detailtabs"):
                                        with gr.Tab("Summary"):
                                                score_panel = gr.Markdown(
                                                        "## FraudClarity answer\n\nRun a question to view answer details, timings, and judge output only when needed.",
                                                        elem_classes=["fc-compact-markdown"],
                                                )
                                        with gr.Tab("Structured details"):
                                                detail_json = gr.Code(label="Structured details", language="json", value="{}")

                answer_provider.change(
                        fn=build_answer_runtime_markdown,
                        inputs=[answer_provider],
                        outputs=[model_status],
                        queue=False,
                )
                send_button.click(
                        fn=run_chat_turn,
                        inputs=[prompt_box, history_state, thread_state, answer_provider, judge_mode, reference_answer, top_k, show_evidence],
                        outputs=[chatbot, thread_state, prompt_box, score_panel, detail_json],
                        queue=False,
                ).then(lambda history: history, inputs=[chatbot], outputs=[history_state])
                prompt_box.submit(
                        fn=run_chat_turn,
                        inputs=[prompt_box, history_state, thread_state, answer_provider, judge_mode, reference_answer, top_k, show_evidence],
                        outputs=[chatbot, thread_state, prompt_box, score_panel, detail_json],
                        queue=False,
                ).then(lambda history: history, inputs=[chatbot], outputs=[history_state])
                clear_button.click(
                        fn=clear_chat,
                        inputs=None,
                        outputs=[chatbot, thread_state, prompt_box, score_panel, detail_json],
                        queue=False,
                ).then(lambda history: history, inputs=[chatbot], outputs=[history_state])

        return demo


def parse_args() -> ArgumentParser:
    parser = ArgumentParser(description="Run the FraudClarity Gradio app.")
    parser.add_argument("--host", default=os.getenv("FRAUDCLARITY_HOST", DEFAULT_HOST), help="Host interface to bind")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("FRAUDCLARITY_PORT", str(DEFAULT_PORT))),
        help="Preferred starting port",
    )
    parser.add_argument(
        "--max-port",
        type=int,
        default=int(os.getenv("FRAUDCLARITY_MAX_PORT", str(DEFAULT_MAX_PORT))),
        help="Highest port to try if the preferred port is busy",
    )
    return parser.parse_args()


def launch_demo(demo: gr.Blocks, host: str, preferred_port: int, max_port: int) -> int:
    for candidate_port in range(preferred_port, max_port + 1):
        try:
            demo.launch(
                server_name=host,
                server_port=candidate_port,
                share=False,
                prevent_thread_lock=False,
                quiet=True,
                inbrowser=False,
            )
            print(f"FraudClarity Chatbot running at http://{host}:{candidate_port}")
            return candidate_port
        except OSError:
            continue
    raise OSError(f"Could not find an open port in the range {preferred_port}-{max_port}.")


def main() -> int:
    args = parse_args()
    demo = create_demo()
    launch_demo(demo, args.host, args.port, args.max_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())