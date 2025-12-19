from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Callable

from langchain_openai import ChatOpenAI
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings

from langchain.tools import tool
from langchain_core.messages import HumanMessage, ToolMessage
from langchain.agents import create_agent
from langchain.agents.middleware import wrap_tool_call

# Optional
try:
    from langchain_community.utilities import BingSearchAPIWrapper
except Exception:  # pragma: no cover
    BingSearchAPIWrapper = None  # type: ignore

from news import get_real_finance_news


# -----------------------------
# Tool: Finance News Deep Search
# -----------------------------
def _format_news_items(items: List[Dict[str, Any]], max_items: int = 4) -> str:
    lines: List[str] = []
    for i, item in enumerate(items[:max_items], 1):
        title = item.get("title", "No Title")
        domain = item.get("domain", "Unknown")
        date = item.get("date", "")
        url = item.get("url", "")
        summary = item.get("summary", "") or ""

        lines.append(
            f"{i}. {title}\n"
            f"   source: {domain} {date}\n"
            f"   summary: {summary}\n"
            f"   url: {url}"
        )
    return "\n\n".join(lines).strip()


def search_finance_news_impl(query: str) -> str:
    """
    深度金融新闻搜索：DDGS 找 URL -> 你自己的抓取器抽正文 -> 质量评分分桶
    """
    try:
        results = get_real_finance_news(
            query,
            max_results=5,
            extract_fulltext=True,
        )

        trusted = results.get("trusted", []) or []
        review = results.get("review", []) or []

        # 优先 trusted，不足则补 review
        merged = trusted[:]
        if len(merged) < 3:
            merged.extend(review)

        # 去重
        uniq: List[Dict[str, Any]] = []
        seen = set()
        for it in merged:
            u = it.get("url", "")
            if not u or u in seen:
                continue
            seen.add(u)
            uniq.append(it)

        if not uniq:
            return f"No detailed articles found for query: {query}"

        return _format_news_items(uniq, max_items=4)

    except Exception as e:
        return f"finance_news tool error: {e}"


# -----------------------------
# Build LLM / Vectorstore
# -----------------------------
def build_llm(
    *,
    temperature: float = 0.0,
    model: str = "gpt-4o-mini",
    timeout: int = 30,
    **kwargs,
) -> ChatOpenAI:
    return ChatOpenAI(
        model=model,
        temperature=temperature,
        timeout=timeout,
        **kwargs,
    )


def build_vectorstore(
    *,
    persist_directory: str = "db",
    collection_name: str = "langchain",
    **kwargs,
) -> Chroma:
    # embeddings 的 key/base_url 走 kwargs 透传（兼容你现有参数习惯）
    embedding_kwargs: Dict[str, Any] = {}
    if "openai_api_key" in kwargs:
        embedding_kwargs["openai_api_key"] = kwargs["openai_api_key"]
    if "openai_api_base" in kwargs:
        embedding_kwargs["openai_api_base"] = kwargs["openai_api_base"]

    embeddings = OpenAIEmbeddings(**embedding_kwargs)

    return Chroma(
        persist_directory=persist_directory,
        collection_name=collection_name,
        embedding_function=embeddings,
    )


# -----------------------------
# Middleware: Tool error handling
# -----------------------------
@wrap_tool_call
def tool_error_guard(request, handler):
    try:
        return handler(request)
    except Exception as e:
        # 给模型一个可读的 ToolMessage，而不是直接把异常炸穿整个执行
        return ToolMessage(
            content=f"Tool execution failed: {type(e).__name__}: {e}",
            tool_call_id=request.tool_call["id"],
        )


# -----------------------------
# Build Tools (LangChain v1 style)
# -----------------------------
def build_tools(
    *,
    vectordb: Chroma,
    enable_search: bool = True,
    bing_subscription_key: Optional[str] = None,
    bing_search_url: Optional[str] = None,
) -> List[Callable]:
    retriever = vectordb.as_retriever(search_kwargs={"k": 6})

    @tool("kb_search")
    def kb_search(query: str) -> str:
        """Search local Chroma knowledge base and return the most relevant passages with metadata."""
        docs = retriever.get_relevant_documents(query)
        if not docs:
            return "No relevant documents found in local KB."

        parts: List[str] = []
        for i, d in enumerate(docs[:6], 1):
            meta = d.metadata or {}
            src = meta.get("source") or meta.get("url") or meta.get("file_path") or "unknown"
            parts.append(f"[{i}] source={src}\n{d.page_content}")

        return "\n\n".join(parts)

    tools: List[Callable] = [kb_search]

    if enable_search:

        @tool("finance_news")
        def finance_news(query: str) -> str:
            """Deep finance news search (DDG news + fetch + article extraction). Use specific keywords."""
            return search_finance_news_impl(query)

        tools.append(finance_news)

        if bing_subscription_key and BingSearchAPIWrapper is not None:
            wrapper = BingSearchAPIWrapper(
                bing_subscription_key=bing_subscription_key,
                bing_search_url=bing_search_url,
            )

            @tool("web_search")
            def web_search(query: str) -> str:
                """General web search for broader context."""
                return wrapper.run(query)

            tools.append(web_search)

    return tools


# -----------------------------
# Factory: create graph agent
# -----------------------------
DEFAULT_SYSTEM_PROMPT = """You are a finance-focused research assistant.
When the user asks about markets, macro, companies, or "latest" information:
- Prefer using finance_news to fetch recent articles.
- If KB may contain relevant internal notes, use kb_search.
- Use web_search only when finance_news is insufficient.
- Do not fabricate sources. If evidence is weak, explicitly say so.
Output should be concise, actionable, and cite which tool results you relied on (by describing source domains/titles)."""


def create_agent_graph(
    *,
    temperature: float = 0.0,
    persist_directory: str = "db",
    collection_name: str = "langchain",
    enable_search: bool = True,
    debug: bool = False,
    model: str = "gpt-4o-mini",
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    bing_subscription_key: Optional[str] = None,
    bing_search_url: Optional[str] = None,
    **llm_kwargs,
):
    llm = build_llm(temperature=temperature, model=model, **llm_kwargs)
    vectordb = build_vectorstore(
        persist_directory=persist_directory,
        collection_name=collection_name,
        **llm_kwargs,
    )
    tools = build_tools(
        vectordb=vectordb,
        enable_search=enable_search,
        bing_subscription_key=bing_subscription_key,
        bing_search_url=bing_search_url,
    )

    agent = create_agent(
        model=llm,
        tools=tools,
        system_prompt=system_prompt,
        middleware=[tool_error_guard],
        debug=debug,
    )
    return agent


class SimpleAgent:
    """
    轻量封装：保持你原来的 .run("...") 体验，但底层已是 LangChain v1 graph agent。
    """

    def __init__(
        self,
        *,
        temperature: float = 0.0,
        persist_directory: str = "db",
        collection_name: str = "langchain",
        enable_search: bool = True,
        debug: bool = False,
        model: str = "gpt-4o-mini",
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        bing_subscription_key: Optional[str] = None,
        bing_search_url: Optional[str] = None,
        **llm_kwargs,
    ) -> None:
        self._agent = create_agent_graph(
            temperature=temperature,
            persist_directory=persist_directory,
            collection_name=collection_name,
            enable_search=enable_search,
            debug=debug,
            model=model,
            system_prompt=system_prompt,
            bing_subscription_key=bing_subscription_key,
            bing_search_url=bing_search_url,
            **llm_kwargs,
        )

    def run(self, question: str) -> str:
        state = self._agent.invoke(
            {"messages": [HumanMessage(content=question)]}
        )
        # create_agent 的 graph 输出是一个 state dict，messages 在里面
        msgs = state.get("messages", [])
        if not msgs:
            return ""
        last = msgs[-1]
        return getattr(last, "content", "") or ""


__all__ = ["create_agent_graph", "SimpleAgent", "build_llm", "build_vectorstore", "build_tools"]
