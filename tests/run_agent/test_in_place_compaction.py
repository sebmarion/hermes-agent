"""Tests for in-place context compaction (config: compression.in_place, #38763).

When ``compression.in_place`` is True, ``compress_context()`` rewrites the
message list and rebuilds the system prompt but keeps the SAME ``session_id``:
no ``end_session``, no ``parent_session_id`` child row, no ``name #N`` title
renumber, no flush-cursor reset. This eliminates the session-rotation bug
cluster (#33618 /goal loss, #14238 lost response, #33907 orphans, #45117 search
gaps, #42228 null cwd). When the flag is explicitly False, rotation behaves
exactly as before as the opt-out fallback.
"""

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _make_agent(session_db, session_id, *, in_place):
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=session_db,
            session_id=session_id,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.compression_in_place = in_place
    # Mock the compressor to return a deterministic shrunk transcript so the
    # test exercises the DB-mutation path, not summarization quality.
    def _fake_compress(messages, current_tokens=None, focus_topic=None, force=False):
        return [
            {"role": "user", "content": "[CONTEXT COMPACTION] summary of prior turns"},
            {"role": "assistant", "content": "recent reply"},
        ]

    agent.context_compressor.compress = _fake_compress
    agent.context_compressor._last_compress_aborted = False
    agent.context_compressor._last_summary_error = None
    agent.context_compressor.compression_count = 1
    return agent


def _seed(db, sid, title, n=8):
    db.create_session(sid, "cli", model="test/model")
    db.set_session_title(sid, title)
    for i in range(n):
        db.append_message(
            session_id=sid,
            role="user" if i % 2 == 0 else "assistant",
            content=f"msg {i}",
        )


class TestInPlaceCompaction:
    def test_in_place_keeps_same_session_id(self):
        """In-place mode: id unchanged, no child row, no rename, history kept."""
        from hermes_state import SessionDB
        from agent.conversation_compression import compress_context

        with tempfile.TemporaryDirectory() as tmp:
            db = SessionDB(db_path=Path(tmp) / "t.db")
            sid = "20260619_120000_aaaaaa"
            _seed(db, sid, "my-research")
            agent = _make_agent(db, sid, in_place=True)
            agent._last_flushed_db_idx = 5

            messages = [{"role": "user", "content": f"m{i}"} for i in range(8)]
            compressed, _sp = compress_context(
                agent, messages, approx_tokens=100_000, system_message="sys"
            )

            # Identity never moved.
            assert agent.session_id == sid
            # No continuation row forked.
            child = db._conn.execute(
                "SELECT id FROM sessions WHERE parent_session_id = ?", (sid,)
            ).fetchall()
            assert child == []
            # Session not ended; title untouched (no "#2").
            row = db.get_session(sid)
            assert row["end_reason"] is None
            assert row["title"] == "my-research"
            # DURABLE, NON-DESTRUCTIVE compaction (the core invariant, per
            # Teknium's review): the LIVE context is the compacted set, but the
            # pre-compaction turns are PRESERVED on disk (active=0), not deleted
            # — searchable + recoverable under the SAME id. A resume reloads the
            # compacted set so compaction actually shrinks the live session and
            # doesn't immediately re-compact (#38763).
            reloaded = db.get_messages_as_conversation(sid)
            assert len(reloaded) == 2
            assert [m.get("content") for m in reloaded] == [
                "[CONTEXT COMPACTION] summary of prior turns",
                "recent reply",
            ]
            assert row["message_count"] == 2  # live (active) count
            # NON-DESTRUCTIVE: the 8 seeded originals survive at active=0
            # alongside the 2 compacted rows — nothing was DELETEd.
            all_rows = db.get_messages(sid, include_inactive=True)
            assert len(all_rows) == 10
            archived = [m for m in all_rows if not m.get("active", 1)]
            assert len(archived) == 8
            # The originals remain FTS-searchable (active=0 is a content-
            # preserving UPDATE; the fts triggers don't key on active).
            hit = db._conn.execute(
                "SELECT 1 FROM messages_fts f JOIN messages m ON m.id = f.rowid "
                "WHERE m.session_id = ? AND messages_fts MATCH 'msg' AND m.active = 0 "
                "LIMIT 1",
                (sid,),
            ).fetchone()
            assert hit is not None
            # Flush identity/cursor reset so next-turn appends diff against the
            # compacted transcript (rebuilds the identity set on next flush).
            assert agent._last_flushed_db_idx == 0
            assert agent._flushed_db_message_ids == set()
            # Rotation-independent in-place signal set for the gateway.
            assert agent._last_compaction_in_place is True
            # Live transcript actually shrank.
            assert len(compressed) == 2

    def test_in_place_alternation_preserved(self):
        """The compacted list must not introduce consecutive same-role messages."""
        from hermes_state import SessionDB
        from agent.conversation_compression import compress_context

        with tempfile.TemporaryDirectory() as tmp:
            db = SessionDB(db_path=Path(tmp) / "t.db")
            sid = "20260619_120500_cccccc"
            _seed(db, sid, "alt")
            agent = _make_agent(db, sid, in_place=True)
            messages = [{"role": "user", "content": f"m{i}"} for i in range(8)]
            compressed, _ = compress_context(
                agent, messages, approx_tokens=100_000, system_message="sys"
            )
            roles = [m["role"] for m in compressed if m.get("role") != "system"]
            assert all(roles[i] != roles[i + 1] for i in range(len(roles) - 1))

    def test_in_place_skips_redundant_preflush(self):
        """In-place must NOT pre-flush current-turn messages: replace_messages
        rewrites the whole row, so a flush would INSERT rows it immediately
        deletes (wasted writes). The current-turn tail survives via the
        compressor's `compressed` output, not the flush."""
        from hermes_state import SessionDB
        from agent.conversation_compression import compress_context

        with tempfile.TemporaryDirectory() as tmp:
            db = SessionDB(db_path=Path(tmp) / "t.db")
            _seed(db, "ip_flush", "f")
            agent = _make_agent(db, "ip_flush", in_place=True)
            calls = {"n": 0}
            agent._flush_messages_to_session_db = lambda *a, **k: calls.__setitem__(
                "n", calls["n"] + 1
            )
            compress_context(
                agent, [{"role": "user", "content": "x"}] * 8,
                approx_tokens=100_000, system_message="sys",
            )
            assert calls["n"] == 0

    def test_rotation_still_preflushes(self):
        """Rotation MUST pre-flush so current-turn messages survive in the
        preserved old (parent) session before it is ended (#47202)."""
        from hermes_state import SessionDB
        from agent.conversation_compression import compress_context

        with tempfile.TemporaryDirectory() as tmp:
            db = SessionDB(db_path=Path(tmp) / "t.db")
            _seed(db, "rot_flush", "f")
            agent = _make_agent(db, "rot_flush", in_place=False)
            calls = {"n": 0}
            agent._flush_messages_to_session_db = lambda *a, **k: calls.__setitem__(
                "n", calls["n"] + 1
            )
            compress_context(
                agent, [{"role": "user", "content": "x"}] * 8,
                approx_tokens=100_000, system_message="sys",
            )
            assert calls["n"] == 1


class TestRotationFallbackWhenFlagOff:
    def test_rotation_when_flag_off(self):
        """Rotation is now the OPT-OUT fallback (default flipped to in-place in
        #38763). With in_place=False explicitly set, legacy rotation is
        unchanged — forks a renamed continuation session."""
        from hermes_state import SessionDB
        from agent.conversation_compression import compress_context

        with tempfile.TemporaryDirectory() as tmp:
            db = SessionDB(db_path=Path(tmp) / "t.db")
            sid = "20260619_130000_bbbbbb"
            _seed(db, sid, "my-research")
            agent = _make_agent(db, sid, in_place=False)
            agent._last_flushed_db_idx = 5

            messages = [{"role": "user", "content": f"m{i}"} for i in range(8)]
            compress_context(
                agent, messages, approx_tokens=100_000, system_message="sys"
            )

            # Identity rotated to a fresh id.
            assert agent.session_id != sid
            # Old session ended via compression; continuation forked + renamed.
            assert db.get_session(sid)["end_reason"] == "compression"
            child = db._conn.execute(
                "SELECT id, title FROM sessions WHERE parent_session_id = ?", (sid,)
            ).fetchall()
            assert len(child) == 1
            assert child[0]["title"] == "my-research #2"
            # Flush cursor reset for the new row.
            assert agent._last_flushed_db_idx == 0
            # Rotation mode does NOT set the in-place signal.
            assert getattr(agent, "_last_compaction_in_place", False) is False


class TestInPlaceSignalForGateway:
    """compress_context must expose a rotation-independent flag the gateway can
    read (instead of an id-change diff) to re-baseline transcript handling."""

    def test_signal_set_on_in_place_unset_on_rotation(self):
        from hermes_state import SessionDB
        from agent.conversation_compression import compress_context

        with tempfile.TemporaryDirectory() as tmp:
            db = SessionDB(db_path=Path(tmp) / "t.db")
            # in-place → flag True
            _seed(db, "s_ip", "ip")
            a_ip = _make_agent(db, "s_ip", in_place=True)
            compress_context(
                a_ip, [{"role": "user", "content": "x"}] * 8,
                approx_tokens=100_000, system_message="sys",
            )
            assert a_ip._last_compaction_in_place is True

            # rotation → flag False
            _seed(db, "s_rot", "rot")
            a_rot = _make_agent(db, "s_rot", in_place=False)
            compress_context(
                a_rot, [{"role": "user", "content": "x"}] * 8,
                approx_tokens=100_000, system_message="sys",
            )
            assert a_rot._last_compaction_in_place is False


class TestInPlaceConfigDefault:
    def test_flag_defaults_on(self):
        """In-place is the default as of #38763 (rotation is now opt-out via
        compression.in_place: false)."""
        from hermes_cli.config import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["compression"].get("in_place") is True

    @pytest.mark.parametrize(
        ("compression_config", "expected"),
        [({}, True), ({"in_place": False}, False)],
    )
    def test_agent_missing_config_defaults_on_but_explicit_false_opts_out(
        self, compression_config, expected
    ):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}), patch(
            "hermes_cli.config.load_config",
            return_value={"compression": compression_config},
        ):
            from run_agent import AIAgent

            agent = AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                model="test/model",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )

        assert agent.compression_in_place is expected


class TestDispatchProjectionPersistence:
    def test_changed_projection_is_atomically_archived_and_rebaselined(self):
        from agent.conversation_compression import (
            conversation_history_after_compression,
            persist_in_place_projection,
        )
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "t.db"
            db = SessionDB(db_path=db_path)
            sid = "dispatch_projection_success"
            _seed(db, sid, "projection", n=4)
            agent = _make_agent(db, sid, in_place=True)
            agent._last_compaction_in_place = False
            agent._last_flushed_db_idx = 4
            agent._flushed_db_message_ids = {1234}
            previous = [
                {"role": "user", "content": "msg 0"},
                {"role": "assistant", "content": "msg 1"},
                {"role": "user", "content": "msg 2"},
                {"role": "assistant", "content": "msg 3"},
            ]
            compacted = [
                {"role": "user", "content": "bounded summary"},
                {"role": "assistant", "content": "recent reply"},
            ]
            expected_active_ids = tuple(row["id"] for row in db.get_messages(sid))

            with patch.object(
                db, "archive_and_compact", wraps=db.archive_and_compact
            ) as archive:
                adopted, persisted = persist_in_place_projection(
                    agent, previous, compacted
                )

            archive.assert_called_once_with(
                sid,
                compacted,
                expected_active_message_ids=expected_active_ids,
            )
            assert persisted is True
            assert adopted is compacted
            rows = db.get_messages(sid, include_inactive=True)
            assert all(row["active"] == 0 and row["compacted"] == 1 for row in rows[:4])
            assert all(row["active"] == 1 for row in rows[4:])
            assert agent._last_compaction_in_place is True
            assert agent._last_flushed_db_idx == 0
            assert agent._flushed_db_message_ids == set()

            # The re-baseline treats the inserted projection as history, so a
            # normal append-only flush writes only the new tail message.
            history = conversation_history_after_compression(agent, adopted)
            assert history is not adopted
            appended = adopted + [{"role": "user", "content": "new turn"}]
            agent._flush_messages_to_session_db(appended, history)
            assert [row["content"] for row in db.get_messages(sid)] == [
                "bounded summary",
                "recent reply",
                "new turn",
            ]

            db.close()
            restarted = SessionDB(db_path=db_path)
            try:
                assert [
                    message["content"]
                    for message in restarted.get_messages_as_conversation(sid)
                ] == ["bounded summary", "recent reply", "new turn"]
            finally:
                restarted.close()

    def test_restored_whitespace_normalization_still_matches_durable_prefix(self):
        from agent.conversation_compression import persist_in_place_projection
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmp:
            db = SessionDB(db_path=Path(tmp) / "t.db")
            sid = "dispatch_projection_restored_whitespace"
            db.create_session(sid, "cli", model="test/model")
            agent = _make_agent(db, sid, in_place=True)
            live_messages = [
                {"role": "user", "content": "  ordinary resumed prompt  \n"},
                {"role": "assistant", "content": "\nordinary response\n"},
            ]
            agent._flush_messages_to_session_db(live_messages)
            assert [row["content"] for row in db.get_messages(sid)] == [
                "  ordinary resumed prompt  \n",
                "\nordinary response\n",
            ]
            previous = db.get_messages_as_conversation(sid)
            assert [message["content"] for message in previous] == [
                "ordinary resumed prompt",
                "ordinary response",
            ]
            compacted = [{"role": "user", "content": "bounded summary"}]

            adopted, persisted = persist_in_place_projection(
                agent,
                previous,
                compacted,
            )

            assert adopted is compacted
            assert persisted is True
            assert [
                message["content"]
                for message in db.get_messages_as_conversation(sid)
            ] == ["bounded summary"]

    def test_live_multimodal_normalization_still_matches_durable_prefix(self):
        from agent.conversation_compression import persist_in_place_projection
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmp:
            db = SessionDB(db_path=Path(tmp) / "t.db")
            sid = "dispatch_projection_live_multimodal"
            db.create_session(sid, "cli", model="test/model")
            agent = _make_agent(db, sid, in_place=True)
            previous = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "inspect this"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,abc"},
                        },
                    ],
                },
                {"role": "assistant", "content": "ordinary response"},
            ]
            agent._flush_messages_to_session_db(previous)
            assert [row["content"] for row in db.get_messages(sid)] == [
                "inspect this\n[screenshot]",
                "ordinary response",
            ]
            compacted = [{"role": "user", "content": "bounded visual summary"}]

            adopted, persisted = persist_in_place_projection(
                agent,
                previous,
                compacted,
            )

            assert adopted is compacted
            assert persisted is True
            assert [
                message["content"]
                for message in db.get_messages_as_conversation(sid)
            ] == ["bounded visual summary"]

    def test_db_failure_returns_exact_original_and_rolls_back_all_state(self):
        from agent.conversation_compression import persist_in_place_projection
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmp:
            db = SessionDB(db_path=Path(tmp) / "t.db")
            sid = "dispatch_projection_failure"
            _seed(db, sid, "projection", n=4)
            original = [
                {"role": "user", "content": "msg 0"},
                {"role": "assistant", "content": "msg 1"},
                {"role": "user", "content": "msg 2"},
                {"role": "assistant", "content": "msg 3"},
            ]
            compacted = [{"role": "user", "content": "must not be adopted"}]
            flushed_ids = {17, 23}
            agent = SimpleNamespace(
                _session_db=db,
                session_id=sid,
                _last_compaction_in_place=False,
                _last_flushed_db_idx=4,
                _flushed_db_message_ids=flushed_ids,
            )

            # Fail after archive_and_compact has issued its active=0 UPDATE.
            # SessionDB's transaction must roll that UPDATE back as well as
            # preventing any partial projection INSERT.
            with patch.object(
                db, "_insert_message_rows", side_effect=RuntimeError("insert failed")
            ):
                adopted, persisted = persist_in_place_projection(
                    agent, original, compacted
                )

            assert persisted is False
            assert adopted is original
            assert agent._last_compaction_in_place is False
            assert agent._last_flushed_db_idx == 4
            assert agent._flushed_db_message_ids is flushed_ids
            rows = db.get_messages(sid, include_inactive=True)
            assert [row["content"] for row in rows] == [f"msg {i}" for i in range(4)]
            assert all(row["active"] == 1 and row["compacted"] == 0 for row in rows)
            assert db.get_session(sid)["message_count"] == 4

    def test_concurrent_append_after_snapshot_fails_cas_without_archiving(self):
        from agent.conversation_compression import persist_in_place_projection
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "t.db"
            db = SessionDB(db_path=db_path)
            writer = SessionDB(db_path=db_path)
            sid = "dispatch_projection_stale"
            _seed(db, sid, "projection", n=4)
            previous = [
                {"role": "user", "content": "msg 0"},
                {"role": "assistant", "content": "msg 1"},
                {"role": "user", "content": "msg 2"},
                {"role": "assistant", "content": "msg 3"},
            ]
            compacted = [{"role": "user", "content": "must not be adopted"}]
            flushed_ids = {17, 23}
            agent = SimpleNamespace(
                _session_db=db,
                session_id=sid,
                _last_compaction_in_place=False,
                _last_flushed_db_idx=4,
                _flushed_db_message_ids=flushed_ids,
            )
            original_archive = db.archive_and_compact

            def archive_after_concurrent_append(*args, **kwargs):
                writer.append_message(
                    session_id=sid,
                    role="user",
                    content="concurrent append",
                )
                return original_archive(*args, **kwargs)

            try:
                with (
                    patch.object(
                        db,
                        "try_acquire_compression_lock",
                        wraps=db.try_acquire_compression_lock,
                    ) as acquire,
                    patch.object(
                        db,
                        "release_compression_lock",
                        wraps=db.release_compression_lock,
                    ) as release,
                    patch.object(
                        db,
                        "archive_and_compact",
                        side_effect=archive_after_concurrent_append,
                    ),
                ):
                    adopted, persisted = persist_in_place_projection(
                        agent, previous, compacted
                    )

                assert adopted is previous
                assert persisted is False
                assert agent._last_compaction_in_place is False
                assert agent._last_flushed_db_idx == 4
                assert agent._flushed_db_message_ids is flushed_ids
                assert [row["content"] for row in db.get_messages(sid)] == [
                    "msg 0",
                    "msg 1",
                    "msg 2",
                    "msg 3",
                    "concurrent append",
                ]
                rows = db.get_messages(sid, include_inactive=True)
                assert all(
                    row["active"] == 1 and row["compacted"] == 0
                    for row in rows
                )
                assert db.get_session(sid)["message_count"] == 5
                acquire.assert_called_once()
                release.assert_called_once()
            finally:
                writer.close()
                db.close()

    def test_without_durable_session_adopts_memory_projection_but_reports_not_persisted(self):
        from agent.conversation_compression import persist_in_place_projection

        agent = SimpleNamespace(_session_db=None, session_id=None)
        original = [{"role": "user", "content": "old"}]
        compacted = [{"role": "user", "content": "bounded"}]

        adopted, persisted = persist_in_place_projection(agent, original, compacted)

        assert adopted is compacted
        assert persisted is False
        assert not hasattr(agent, "_last_compaction_in_place")


class TestCompactedTurnsStaySearchable:
    """Teknium's review hinges on the pre-compaction transcript staying
    DISCOVERABLE after in-place compaction. Compaction-archived rows
    (active=0, compacted=1) must surface in session_search by default, while
    rewind/undo rows (active=0, compacted=0) must stay hidden. The two share
    the active flag but are distinguished by the compacted flag."""

    def test_compacted_turns_found_by_default_search(self):
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmp:
            db = SessionDB(db_path=Path(tmp) / "t.db")
            sid = "20260619_search"
            db.create_session(sid, "cli", model="test/model")
            for r, c in [
                ("user", "configure the HMAC secret"),
                ("assistant", "set it in config.yaml"),
                ("user", "deploy returns 403"),
                ("assistant", "rotate the HMAC"),
                ("user", "works now"),
                ("assistant", "great"),
            ]:
                db.append_message(session_id=sid, role=r, content=c)

            before = db.search_messages("HMAC", role_filter=["user", "assistant"])
            assert len(before) == 2

            db.archive_and_compact(
                sid,
                [
                    {"role": "user", "content": "[SUMMARY] earlier setup"},
                    {"role": "assistant", "content": "ok"},
                ],
            )

            # The archived originals (active=0, compacted=1) are still found by
            # the DEFAULT search — this is the durability requirement.
            after = db.search_messages("HMAC", role_filter=["user", "assistant"])
            assert {m["id"] for m in after} == {1, 4}
            # Live context still excludes them.
            assert len(db.get_messages_as_conversation(sid)) == 2

    def test_rewound_turns_stay_hidden(self):
        """Rewind/undo (active=0, compacted=0) must NOT leak into default
        search — the distinction the compacted flag preserves."""
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmp:
            db = SessionDB(db_path=Path(tmp) / "t.db")
            sid = "20260619_undo"
            db.create_session(sid, "cli", model="test/model")
            db.append_message(session_id=sid, role="user", content="ZEBRAWORD remember this")
            db.append_message(session_id=sid, role="assistant", content="noted")
            db.rewind_to_message(sid, db.get_messages(sid)[0]["id"])

            assert db.search_messages("ZEBRAWORD", role_filter=["user", "assistant"]) == []
            recovered = db.search_messages(
                "ZEBRAWORD", role_filter=["user", "assistant"], include_inactive=True
            )
            assert len(recovered) == 1
