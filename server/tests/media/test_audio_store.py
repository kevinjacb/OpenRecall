"""Phase 3 — the slot-indexed frame log (spec §3.1, D5).

The invariant under test throughout: **slot `i` covers `[i*20, (i+1)*20)` ms
of session time, always.** Loss and silence are recorded, never compacted
away. If this ever stops holding, the Recording Detail playhead drifts away
from the transcript and the page's core interaction breaks quietly.
"""
from __future__ import annotations

import pytest

from opensapien_server.media.audio import FRAME_MS, AudioStore


@pytest.fixture
def store(tmp_path):
    return AudioStore(tmp_path / "audio")


def _slots(store, session_id="s1", start_ms=0, end_ms=10_000):
    return list(store.read_range(session_id, start_ms, end_ms))


# ---- basic write/read -------------------------------------------------------


def test_a_fresh_session_has_no_audio(store):
    assert store.has("s1") is False
    assert store.stat("s1").slot_count == 0
    assert _slots(store) == []


def test_frames_round_trip_in_order(store):
    store.write_at("s1", 0, [b"aa", b"bb", b"cc"])

    assert _slots(store) == [b"aa", b"bb", b"cc"]
    assert store.has("s1") is True
    assert store.stat("s1").slot_count == 3


def test_appends_continue_from_the_cursor(store):
    store.write_at("s1", 0, [b"aa"])
    store.write_at("s1", 1, [b"bb"])

    assert _slots(store) == [b"aa", b"bb"]


def test_sessions_are_isolated(store):
    store.write_at("s1", 0, [b"aa"])
    store.write_at("s2", 0, [b"zz"])

    assert _slots(store, "s1") == [b"aa"]
    assert _slots(store, "s2") == [b"zz"]


# ---- the D5 invariant -------------------------------------------------------


def test_a_write_ahead_of_the_cursor_records_the_silence(store):
    """VAD suppresses silence, so a jump forward in slot is real elapsed time
    that must survive into the log."""
    store.write_at("s1", 0, [b"aa"])
    store.write_at("s1", 100, [b"bb"])

    slots = _slots(store)
    assert len(slots) == 101
    assert slots[0] == b"aa"
    assert slots[1:100] == [b""] * 99
    assert slots[100] == b"bb"


def test_slot_index_equals_elapsed_time_over_a_lossy_stream(store):
    """The invariant stated directly: after writing packets at scattered
    session times, slot count is elapsed-ms / 20 — no more, no less."""
    for slot in (0, 5, 6, 40, 41, 42, 300):
        store.write_at("s1", slot, [b"x"])

    stat = store.stat("s1")
    assert stat.slot_count == 301
    assert stat.duration_ms == 301 * FRAME_MS


def test_a_late_packet_is_dropped_rather_than_shifting_the_timeline(store):
    """An append-only log cannot rewrite history. Appending the late frame
    anyway would shift every later frame's timestamp — exactly the drift this
    design exists to prevent."""
    store.write_at("s1", 0, [b"aa"])
    store.write_at("s1", 10, [b"bb"])

    assert store.write_at("s1", 5, [b"late"]) == 0

    slots = _slots(store)
    assert len(slots) == 11
    assert slots[10] == b"bb"


def test_a_gap_marker_packet_advances_time_without_audio(store):
    """Firmware emits gap markers with no frames while VAD suppresses; they
    carry time, and time is what the log is indexed by."""
    store.write_at("s1", 0, [b"aa"])
    store.write_at("s1", 50, [])  # a gap-marker packet

    assert store.stat("s1").slot_count == 50
    assert _slots(store) == [b"aa"] + [b""] * 49


def test_mark_gap_extends_the_timeline(store):
    store.write_at("s1", 0, [b"aa"])
    store.mark_gap("s1", 9)

    assert store.stat("s1").slot_count == 10
    assert _slots(store)[1:] == [b""] * 9


def test_mark_gap_of_zero_is_a_noop(store):
    store.write_at("s1", 0, [b"aa"])
    store.mark_gap("s1", 0)

    assert store.stat("s1").slot_count == 1


def test_a_long_gap_costs_a_constant_number_of_bytes(store):
    """Run-length encoding is what makes an always-on wearable affordable:
    an hour of silence must not cost an hour of records."""
    store.write_at("s1", 0, [b"aa"])
    store.write_at("s1", 180_000, [b"bb"])  # an hour later

    # 2 frames (2+2 bytes each) + one 6-byte gap record.
    assert store.stat("s1").byte_count < 100
    assert store.stat("s1").slot_count == 180_001


# ---- reading a window -------------------------------------------------------


def test_read_range_slices_by_session_time(store):
    store.write_at("s1", 0, [b"a", b"b", b"c", b"d", b"e"])

    assert list(store.read_range("s1", 40, 80)) == [b"c", b"d"]


def test_read_range_is_empty_past_the_end(store):
    store.write_at("s1", 0, [b"a"])

    assert list(store.read_range("s1", 1000, 2000)) == []


def test_read_range_of_an_unknown_session_is_empty(store):
    assert list(store.read_range("nope", 0, 1000)) == []


# ---- malformed frames -------------------------------------------------------


def test_an_oversized_frame_becomes_a_gap_not_a_corrupt_record(store):
    """A frame length of 0xFFFF collides with the gap marker, so it must
    never reach the file."""
    store.write_at("s1", 0, [b"x" * 70_000])

    assert store.stat("s1").slot_count == 1
    assert _slots(store) == [b""]


def test_an_empty_frame_becomes_a_gap(store):
    store.write_at("s1", 0, [b"", b"ok"])

    assert _slots(store) == [b"", b"ok"]


def test_a_truncated_log_stops_cleanly(store):
    """A crash mid-append can only truncate the last record; losing 20 ms
    must not cost the whole recording."""
    store.write_at("s1", 0, [b"aa", b"bb"])
    path = store.log_path("s1")
    path.write_bytes(path.read_bytes()[:-1])

    assert list(AudioStore(path.parent).read_range("s1", 0, 10_000)) == [b"aa"]


# ---- peaks ------------------------------------------------------------------


def test_peaks_are_stored_one_byte_per_slot(store):
    store.write_at("s1", 0, [b"a", b"b"], peaks=[10, 200])

    assert store.peaks("s1", 0, 40) == [10, 200]


def test_peaks_line_up_with_slots_across_a_gap(store):
    """Fixed stride is the point: byte N must be slot N, or the waveform
    drifts from the audio it is drawn against."""
    store.write_at("s1", 0, [b"a"], peaks=[99])
    store.write_at("s1", 5, [b"b"], peaks=[42])

    assert store.peaks("s1", 0, 6 * FRAME_MS) == [99, 0, 0, 0, 0, 42]


def test_a_short_peak_list_cannot_desynchronise_the_stride(store):
    store.write_at("s1", 0, [b"a", b"b", b"c"], peaks=[10])

    assert store.peaks("s1", 0, 60) == [10, 0, 0]


def test_peaks_are_clamped_to_a_byte(store):
    store.write_at("s1", 0, [b"a", b"b"], peaks=[999, -5])

    assert store.peaks("s1", 0, 40) == [255, 0]


def test_peaks_of_an_unknown_session_are_empty(store):
    assert store.peaks("nope", 0, 1000) == []


# ---- erase and delete -------------------------------------------------------


def test_erase_range_replaces_audio_with_silence_and_keeps_the_length(store):
    """Deleting one recording must not shift the timestamps of every
    recording after it in the same session."""
    store.write_at("s1", 0, [b"a", b"b", b"c", b"d"])

    assert store.erase_range("s1", 20, 60) == 2

    assert _slots(store) == [b"a", b"", b"", b"d"]
    assert store.stat("s1").slot_count == 4


def test_erase_range_zeroes_the_peaks_too(store):
    store.write_at("s1", 0, [b"a", b"b", b"c"], peaks=[10, 20, 30])

    store.erase_range("s1", 20, 40)

    assert store.peaks("s1", 0, 60) == [10, 0, 30]


def test_erase_range_is_idempotent(store):
    store.write_at("s1", 0, [b"a", b"b"])

    assert store.erase_range("s1", 0, 20) == 1
    assert store.erase_range("s1", 0, 20) == 0


def test_erase_range_on_an_unknown_session_is_a_noop(store):
    assert store.erase_range("nope", 0, 1000) == 0


def test_delete_removes_both_files_and_is_idempotent(store):
    store.write_at("s1", 0, [b"a"], peaks=[5])

    assert store.delete("s1") is True
    assert store.delete("s1") is False
    assert store.has("s1") is False
    assert store.peaks("s1", 0, 100) == []


# ---- safety -----------------------------------------------------------------


def test_a_traversing_session_id_cannot_escape_the_root(store, tmp_path):
    """Session ids come from the relay and are used as filenames."""
    store.write_at("../../escaped", 0, [b"a"])

    assert not (tmp_path / "escaped.opusraw").exists()
    assert store.log_path("../../escaped").parent == store.log_path("s1").parent


def test_slot_count_survives_a_reopen(store):
    """The file is the source of truth; the in-memory cursor is only a cache."""
    store.write_at("s1", 0, [b"a"])
    store.write_at("s1", 10, [b"b"])

    reopened = AudioStore(store.log_path("s1").parent)

    assert reopened.slot_count("s1") == 11
    reopened.write_at("s1", 11, [b"c"])
    assert list(reopened.read_range("s1", 0, 1000))[11] == b"c"
