from __future__ import annotations

import struct

import pytest

from llm_cli.protocol.envelopes import EnvelopeError, Request, Response
from llm_cli.protocol.framing import FrameError, decode_frame, encode_frame


def test_frame_round_trip_is_deterministic() -> None:
    payload = {"z": 2, "message": "héllo", "a": [1, True]}
    encoded = encode_frame(payload)
    assert struct.unpack(">I", encoded[:4])[0] == len(encoded) - 4
    assert decode_frame(encoded) == payload
    assert encoded == encode_frame(payload)


def test_frame_rejects_invalid_lengths_and_non_object_json() -> None:
    with pytest.raises(FrameError):
        decode_frame(struct.pack(">I", 99) + b"{}")
    with pytest.raises(FrameError):
        decode_frame(struct.pack(">I", 2) + b"[]")
    with pytest.raises(FrameError):
        decode_frame(struct.pack(">I", 0))


def test_request_envelope_validates_required_types() -> None:
    request = Request.create(
        request_id="req_1", method="system.ping", params={}, profile_id="default"
    )
    assert Request.from_dict(request.to_dict()) == request
    invalid = request.to_dict()
    invalid["params"] = []
    with pytest.raises(EnvelopeError):
        Request.from_dict(invalid)


def test_response_envelope_round_trip() -> None:
    response = Response(request_id="req_1", ok=True, result={"ready": True})
    assert Response.from_dict(response.to_dict()) == response
