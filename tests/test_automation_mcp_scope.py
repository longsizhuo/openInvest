"""Unattended jobs have analysis capabilities, never financial mutation rights."""
import asyncio,json
from types import SimpleNamespace

import pytest

from openinvest.connectors import mcp_server as server
from tests.test_news_source_snapshots import source_context


def isolate_registry(monkeypatch):
    manager=server.mcp._tool_manager
    monkeypatch.setattr(manager,'_tools',dict(manager._tools))


def test_automation_scope_removes_money_preferences_and_execution_feedback(monkeypatch):
    isolate_registry(monkeypatch)
    monkeypatch.setenv('INVEST_REASONING_BY','agent')
    server._apply_scope('automation')
    tools={tool.name for tool in asyncio.run(server.mcp.list_tools())}
    assert tools==server.AUTOMATION_ALLOWED_TOOLS
    assert not tools & {'buy','sell','deposit','withdraw','record_execution','set_allocations','track_asset','untrack_asset','add_news_source','remove_news_source','run_committee'}
    assert {'collect_news','event_history','prepare_committee','submit_committee','committee_review','quality_status'}<=tools
    with pytest.raises(Exception,match='Unknown tool'):
        asyncio.run(server.mcp.call_tool('deposit',{'currency':'CNY','amount':100}))


@pytest.mark.parametrize('http',[False,True])
def test_actual_main_applies_scope_for_both_transports_and_never_spawns_scheduler(monkeypatch,http):
    isolate_registry(monkeypatch)
    monkeypatch.setenv('INVEST_REASONING_BY','agent')
    monkeypatch.setenv('INVEST_MCP_SCOPE','automation')
    monkeypatch.setattr('sys.argv',['openinvest-mcp']+(['--http'] if http else []))
    names=[]
    def started():names.extend(tool.name for tool in asyncio.run(server.mcp.list_tools()))
    monkeypatch.setattr(server,'_serve_http',started)
    monkeypatch.setattr(server.mcp,'run',started)
    monkeypatch.setattr(server,'_start_embedded_scheduler',lambda:pytest.fail('automation must not spawn other capabilities'))
    server.main()
    assert set(names)==server.AUTOMATION_ALLOWED_TOOLS


def test_misspelled_scope_cannot_widen_an_unattended_server(monkeypatch):
    isolate_registry(monkeypatch)
    before={tool.name for tool in asyncio.run(server.mcp.list_tools())}
    with pytest.raises(ValueError,match='refusing an unrestricted'):server._apply_scope('automtion')
    assert {tool.name for tool in asyncio.run(server.mcp.list_tools())}==before


def test_automation_refuses_backend_mode_even_if_a_model_key_is_present(monkeypatch):
    isolate_registry(monkeypatch)
    monkeypatch.setenv('INVEST_REASONING_BY','backend')
    monkeypatch.setenv('LLM_API_KEY','SYNTHETIC_ONLY')
    with pytest.raises(ValueError,match='implicit backend inference is unavailable'):server._apply_scope('automation')


def test_mcp_collector_delegates_capture_without_constructing_a_model(monkeypatch):
    calls=[]
    monkeypatch.setattr('openinvest.services.news_collection.collect',lambda **kwargs:calls.append(kwargs) or {'status':'coverage_unknown','model_calls':0})
    monkeypatch.setattr('openinvest.core.committee.agent_io._create_agent',lambda *args,**kwargs:pytest.fail('capture must not call a model'))
    result=server.collect_news(since='2026-11-01T00:00:00+00:00')
    assert result['model_calls']==0 and calls==[{'since':'2026-11-01T00:00:00+00:00'}]


def test_mcp_and_cli_history_share_qualified_readonly_view_with_visible_unknowns(source_context,monkeypatch):
    from openinvest.db.event_store import EventStore
    from openinvest.skill_cmds.analysis_cmds import cmd_event_history
    database=EventStore(str(source_context/'db/events.db'))
    database.upsert_event({'one_line_claim':'Synthetic unknown source','severity':'high','stance':'risk','affected_symbols':['SYNTHETIC']})
    database.conn.close()
    before=(source_context/'db/events.db').read_bytes()
    mcp=server.event_history()
    printed=[]
    monkeypatch.setattr('openinvest.skill_cmds.analysis_cmds._print_json',printed.append)
    cmd_event_history(SimpleNamespace(hours=48,limit=100))
    cli=printed[0]
    assert mcp==cli and mcp['events']==[] and mcp['source_quality']['current_unqualified_events']==1
    assert mcp['coverage_watermark'] is None
    assert (source_context/'db/events.db').read_bytes()==before
