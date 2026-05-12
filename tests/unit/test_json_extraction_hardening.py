import json

import pytest

from orchestrator.brain import extract_json_block


def test_extract_json_block_simple():
    text = '{"foo": 1}'
    assert extract_json_block(text) == '{"foo": 1}'


def test_extract_json_block_fenced():
    text = 'Here is the JSON:\n```json\n{"foo": 1}\n```'
    assert extract_json_block(text) == '{"foo": 1}'


def test_extract_json_block_prose_leading():
    text = 'The result is {"foo": 1}'
    assert extract_json_block(text) == '{"foo": 1}'


def test_extract_json_block_prose_trailing():
    text = '{"foo": 1} is the result'
    assert extract_json_block(text) == '{"foo": 1}'


def test_extract_json_block_prose_both():
    text = 'Result: {"foo": 1} - verified.'
    assert extract_json_block(text) == '{"foo": 1}'


def test_extract_json_block_handles_repro_case_bracket():
    # Reported error: Expecting value: line 1 column 2 (char 1)
    # This happened with "[/address-review]"
    text = '[/address-review] {"status": "passed"}'
    # New logic should skip the [/ part and find the { block
    assert extract_json_block(text) == '{"status": "passed"}'


def test_extract_json_block_handles_malformed_bracket_no_json():
    # If it's just garbage starting with [, it still returns the whole thing
    # (to let json.loads fail as before) BUT now it will strip it.
    text = "[/address-review]"
    assert extract_json_block(text) == "[/address-review]"
    with pytest.raises(json.JSONDecodeError):
        json.loads(extract_json_block(text))


def test_extract_json_block_nested():
    text = 'Outer { "inner": { "val": 1 } } tail'
    assert extract_json_block(text) == '{ "inner": { "val": 1 } }'


def test_extract_json_block_multiple_fences_prefer_last():
    text = 'Example:\n```json\n{"foo": 0}\n```\nFinal:\n```json\n{"foo": 1}\n```'
    assert extract_json_block(text) == '{"foo": 1}'


def test_extract_json_block_multiple_objects():
    # Model returns multiple objects, we take the first one
    text = 'Here is the first: {"foo": 1} and the second: {"bar": 2}'
    assert extract_json_block(text) == '{"foo": 1}'


def test_extract_json_block_trailing_garbage():
    text = '{"foo": 1} some extra prose'
    assert extract_json_block(text) == '{"foo": 1}'


def test_extract_json_block_fallbacks_to_greedy_brace_match_on_decode_error():
    text = 'prefix {"foo": 1,} suffix }'
    assert extract_json_block(text) == '{"foo": 1,} suffix }'


def test_extract_json_block_unwraps_known_wrapper_string():
    text = '{"response": "{\\"status\\": \\"passed\\"}"}'
    assert extract_json_block(text) == '{"status": "passed"}'


def test_extract_json_block_unwraps_known_wrapper_dict():
    text = '{"content": {"status": "passed", "summary": "ok"}}'
    assert extract_json_block(text) == '{"status": "passed", "summary": "ok"}'
