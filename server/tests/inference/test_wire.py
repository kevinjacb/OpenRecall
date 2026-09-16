from openrecall_server.inference import wire
from openrecall_server.ingest.streaming_transcriber import Token


def test_pcm_round_trips_exactly():
    pcm = bytes(range(256)) * 4
    assert wire.decode_pcm(wire.encode_pcm(pcm)) == pcm


def test_token_round_trips_all_fields():
    t = Token(text="hello", start_ms=120, end_ms=480, sentence_id=3)
    assert wire.token_from_json(wire.token_to_json(t)) == t


def test_token_json_is_plain_types():
    d = wire.token_to_json(Token(text="x", start_ms=0, end_ms=1))
    assert set(d) == {"text", "start_ms", "end_ms", "sentence_id"}
    assert all(isinstance(v, (str, int)) for v in d.values())
