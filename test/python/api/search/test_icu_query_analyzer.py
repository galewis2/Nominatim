# SPDX-License-Identifier: GPL-3.0-or-later
#
# This file is part of Nominatim. (https://nominatim.org)
#
# Copyright (C) 2025 by the Nominatim developer community.
# For a full list of authors see the git log.
"""
Tests for query analyzer for ICU tokenizer.
"""
import pytest
import pytest_asyncio

from nominatim_api import NominatimAPIAsync
from nominatim_api.search.query import Phrase
import nominatim_api.search.query as qmod
import nominatim_api.search.icu_tokenizer as tok
from nominatim_api.logging import set_log_output, get_and_disable


async def add_word(conn, word_id, word_token, wtype, word, info=None):
    t = conn.t.meta.tables['word']
    await conn.execute(t.insert(), {'word_id': word_id,
                                    'word_token': word_token,
                                    'type': wtype,
                                    'word': word,
                                    'info': info})


def make_phrase(query):
    return [Phrase(qmod.PHRASE_ANY, s) for s in query.split(',')]


@pytest_asyncio.fixture
async def conn(table_factory):
    """ Create an asynchronous SQLAlchemy engine for the test DB.
    """
    table_factory('nominatim_properties',
                  definition='property TEXT, value TEXT',
                  content=(('tokenizer_import_normalisation', ':: lower();'),
                           ('tokenizer_import_transliteration', "'1' > '/1/'; 'ä' > 'ä '")))
    table_factory('word',
                  definition='word_id INT, word_token TEXT, type TEXT, word TEXT, info JSONB')

    async with NominatimAPIAsync() as api:
        async with api.begin() as conn:
            yield conn


@pytest.mark.asyncio
async def test_empty_phrase(conn):
    ana = await tok.create_query_analyzer(conn)

    query = await ana.analyze_query([])

    assert len(query.source) == 0
    assert query.num_token_slots() == 0


@pytest.mark.asyncio
async def test_single_phrase_with_unknown_terms(conn):
    ana = await tok.create_query_analyzer(conn)

    await add_word(conn, 1, 'foo', 'w', 'FOO')

    query = await ana.analyze_query(make_phrase('foo BAR'))

    assert len(query.source) == 1
    assert query.source[0].ptype == qmod.PHRASE_ANY
    assert query.source[0].text == 'foo bar'

    assert query.num_token_slots() == 2
    assert query.nodes[0].partial.token == 1
    assert query.nodes[1].partial is None


@pytest.mark.asyncio
async def test_multiple_phrases(conn):
    ana = await tok.create_query_analyzer(conn)

    await add_word(conn, 1, 'one', 'w', 'one')
    await add_word(conn, 2, 'two', 'w', 'two')
    await add_word(conn, 100, 'one two', 'W', 'one two')
    await add_word(conn, 3, 'three', 'w', 'three')

    query = await ana.analyze_query(make_phrase('one two,three'))

    assert len(query.source) == 2


@pytest.mark.asyncio
async def test_splitting_in_transliteration(conn):
    ana = await tok.create_query_analyzer(conn)

    await add_word(conn, 1, 'mä', 'W', 'ma')
    await add_word(conn, 2, 'fo', 'W', 'fo')

    query = await ana.analyze_query(make_phrase('mäfo'))

    assert query.num_token_slots() == 2
    assert query.nodes[0].starting
    assert query.nodes[1].starting
    assert query.nodes[1].btype == qmod.BREAK_TOKEN


@pytest.mark.asyncio
@pytest.mark.parametrize('term,order', [('23456', ['P', 'H', 'W']),
                                        ('3', ['H', 'W'])])
async def test_penalty_postcodes_and_housenumbers(conn, term, order):
    ana = await tok.create_query_analyzer(conn)

    await add_word(conn, 2, term, 'H', term)
    await add_word(conn, 3, term, 'w', term)
    await add_word(conn, 4, term, 'W', term)

    query = await ana.analyze_query(make_phrase(term))

    assert query.num_token_slots() == 1

    torder = [(tl.tokens[0].penalty, tl.ttype) for tl in query.nodes[0].starting]
    torder.sort()

    assert [t[1] for t in torder] == order


@pytest.mark.asyncio
async def test_category_words_only_at_beginning(conn):
    ana = await tok.create_query_analyzer(conn)

    await add_word(conn, 1, 'foo', 'S', 'FOO', {'op': 'in'})
    await add_word(conn, 2, 'bar', 'w', 'BAR')

    query = await ana.analyze_query(make_phrase('foo BAR foo'))

    assert query.num_token_slots() == 3
    assert len(query.nodes[0].starting) == 1
    assert query.nodes[0].starting[0].ttype == qmod.TOKEN_NEAR_ITEM
    assert not query.nodes[2].starting


@pytest.mark.asyncio
async def test_freestanding_qualifier_words_become_category(conn):
    ana = await tok.create_query_analyzer(conn)

    await add_word(conn, 1, 'foo', 'S', 'FOO', {'op': '-'})

    query = await ana.analyze_query(make_phrase('foo'))

    assert query.num_token_slots() == 1
    assert len(query.nodes[0].starting) == 1
    assert query.nodes[0].starting[0].ttype == qmod.TOKEN_NEAR_ITEM


@pytest.mark.asyncio
async def test_qualifier_words(conn):
    ana = await tok.create_query_analyzer(conn)

    await add_word(conn, 1, 'foo', 'S', None, {'op': '-'})
    await add_word(conn, 2, 'bar', 'w', None)

    query = await ana.analyze_query(make_phrase('foo BAR foo BAR foo'))

    assert query.num_token_slots() == 5
    assert set(t.ttype for t in query.nodes[0].starting) == {qmod.TOKEN_QUALIFIER}
    assert set(t.ttype for t in query.nodes[2].starting) == {qmod.TOKEN_QUALIFIER}
    assert set(t.ttype for t in query.nodes[4].starting) == {qmod.TOKEN_QUALIFIER}


@pytest.mark.asyncio
async def test_add_unknown_housenumbers(conn):
    ana = await tok.create_query_analyzer(conn)

    await add_word(conn, 1, '23', 'H', '23')

    query = await ana.analyze_query(make_phrase('466 23 99834 34a'))

    assert query.num_token_slots() == 4
    assert query.nodes[0].starting[0].ttype == qmod.TOKEN_HOUSENUMBER
    assert len(query.nodes[0].starting[0].tokens) == 1
    assert query.nodes[0].starting[0].tokens[0].token == 0
    assert query.nodes[1].starting[0].ttype == qmod.TOKEN_HOUSENUMBER
    assert len(query.nodes[1].starting[0].tokens) == 1
    assert query.nodes[1].starting[0].tokens[0].token == 1
    assert query.nodes[2].has_tokens(3, qmod.TOKEN_POSTCODE)
    assert not query.nodes[2].has_tokens(3, qmod.TOKEN_HOUSENUMBER)
    assert not query.nodes[2].has_tokens(4, qmod.TOKEN_HOUSENUMBER)
    assert not query.nodes[3].has_tokens(4, qmod.TOKEN_HOUSENUMBER)


@pytest.mark.asyncio
@pytest.mark.parametrize('logtype', ['text', 'html'])
async def test_log_output(conn, logtype):

    ana = await tok.create_query_analyzer(conn)

    await add_word(conn, 1, 'foo', 'w', 'FOO')

    set_log_output(logtype)
    await ana.analyze_query(make_phrase('foo'))

    assert get_and_disable()
