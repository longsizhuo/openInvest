"""
可复用的 LangChain Agent 构建模块。

功能特性：
- 使用 OpenAI LLM（温度可配置）
- 可选的 Bing 网页搜索工具
- 基于 Chroma 的本地知识库检索工具

环境变量（常见）：
- OPENAI_API_KEY
- BING_SUBSCRIPTION_KEY 与 BING_SEARCH_URL（或与 Bing 包装器兼容的变量）

用法示例：
  from agent import create_agent, SimpleAgent
  agent = create_agent(persist_directory='db', enable_search=True, temperature=0)
  print(agent.run("纳斯达克是否适合长期定投？"))
"""

from typing import Optional, List

from langchain_classic.agents import initialize_agent, AgentType
from langchain_openai import ChatOpenAI
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings
from langchain_community.utilities import BingSearchAPIWrapper
from langchain_classic.chains import RetrievalQA
from langchain_classic.tools import Tool



def build_llm(temperature: float = 0.0, model: str = "gpt-3.5-turbo", **kwargs) -> ChatOpenAI:
    """构建基础对话模型（ChatOpenAI）。"""
    return ChatOpenAI(temperature=temperature, model=model, **kwargs)


def build_vectorstore(
    persist_directory: str = "db",
    collection_name: Optional[str] = None,
    **kwargs
):
    """构建持久化的 Chroma 向量库。"""
    # 提取可能传入的 openai_api_key 和 openai_api_base，用于 Embeddings
    embedding_kwargs = {}
    if "openai_api_key" in kwargs:
        embedding_kwargs["openai_api_key"] = kwargs["openai_api_key"]
    if "openai_api_base" in kwargs:
        embedding_kwargs["openai_api_base"] = kwargs["openai_api_base"]
        
    embeddings = OpenAIEmbeddings(**embedding_kwargs)

    # 修复 collection_name 为 None 导致的 TypeError
    if collection_name is None:
        collection_name = "langchain"

    return Chroma(
        persist_directory=persist_directory,
        collection_name=collection_name,
        embedding_function=embeddings,
    )


def build_tools(
    llm: ChatOpenAI,
    vectordb: Chroma,
    enable_search: bool = True,
    **kwargs,
) -> List[Tool]:
    """为 Agent 创建工具列表。

    - web_search：使用 Bing 的网页搜索（可选）
    - kb_qa：基于本地 Chroma 知识库的问答（RetrievalQA）
    """
    tools: List[Tool] = []

    # 本地知识库问答工具：基于 RetrievalQA，输入/输出均为字符串
    qa = RetrievalQA.from_chain_type(
        llm=llm,
        chain_type="stuff",
        retriever=vectordb.as_retriever(),
        return_source_documents=False,
    )
    tools.append(
        Tool(
            name="kb_qa",
            func=qa.run,
            description="利用本地 Chroma 知识库回答问题。",
        )
    )

    if enable_search:
        bing_params = {}
        if "bing_subscription_key" in kwargs:
            bing_params["bing_subscription_key"] = kwargs["bing_subscription_key"]
        if "bing_search_url" in kwargs:
            bing_params["bing_search_url"] = kwargs["bing_search_url"]
        search = BingSearchAPIWrapper(**bing_params)
        tools.insert(
            0,
            Tool(
                name="web_search",
                func=search.run,
                description="进行网页搜索，获取最新信息与新闻。",
            ),
        )

    return tools


def create_agent(
    *,
    temperature: float = 0.0,
    persist_directory: str = "db",
    enable_search: bool = True,
    verbose: bool = False,
    collection_name: Optional[str] = None,
    model: str = "gpt-3.5-turbo",
    **llm_kwargs
):
    """工厂函数：根据配置创建带工具的 AgentExecutor。

    返回的 AgentExecutor 支持 .run(str) 或 .invoke({"input": str})。
    """
    llm = build_llm(temperature=temperature, model=model, **llm_kwargs)
    
    # 将 llm_kwargs 中的 api key 信息也传递给 vectorstore，因为 embeddings 也需要 key
    vectordb = build_vectorstore(
        persist_directory=persist_directory, 
        collection_name=collection_name,
        **llm_kwargs
    )
    tools = build_tools(llm=llm, vectordb=vectordb, enable_search=enable_search, **llm_kwargs)

    agent = initialize_agent(
        tools=tools,
        llm=llm,
        agent=AgentType.ZERO_SHOT_REACT_DESCRIPTION,
        verbose=verbose,
        handle_parsing_errors=True,
    )
    return agent


class SimpleAgent:
    """一个轻量的 Agent 封装，便于复用。

    示例：
        sa = SimpleAgent(persist_directory='db', enable_search=True)
        answer = sa.run("纳斯达克是否适合长期定投？")
    """

    def __init__(
        self,
        *,
        temperature: float = 0.0,
        persist_directory: str = "db",
        enable_search: bool = True,
        verbose: bool = False,
        collection_name: Optional[str] = None,
        model: str = "gpt-3.5-turbo",
        **llm_kwargs
    ) -> None:
        self._agent = create_agent(
            temperature=temperature,
            persist_directory=persist_directory,
            enable_search=enable_search,
            verbose=verbose,
            collection_name=collection_name,
            model=model,
            **llm_kwargs
        )

    def run(self, question: str) -> str:
        return self._agent.invoke({"input": question})["output"]



__all__ = ["create_agent", "SimpleAgent", "build_llm", "build_vectorstore", "build_tools"]