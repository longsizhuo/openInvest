"""Observed text identity is separate from acquisition/time evidence.

This is the identity of the available headline/snippet, not proof that the
publisher's full article was unchanged or that a historical timestamp is valid.
Existing snapshot/evidence hashes deliberately remain untouched.
"""
from __future__ import annotations

from openinvest.core.evidence_eligibility import digest


def reported_text(source):
    return {key:source.get(key) or '' for key in ('url','title','snippet','raw_text')}


def item_text(item):
    return reported_text({'url':str(item.url).strip(),'title':item.title,
        'snippet':item.snippet,'raw_text':getattr(item,'text','')})


def content_id(source):
    return digest({'contract':'observed_news_text_v1',**reported_text(source)})


def analysis_identity(event, sources):
    if sources:
        identity=digest(sorted({content_id(source) for source in sources}))
        return 'reported-text:'+identity, identity
    return event['event_id'], event.get('_version_id',0)
