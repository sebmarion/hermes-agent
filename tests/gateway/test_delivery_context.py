from gateway.session_context import (
    async_delivery_supported,
    bind_delivery_context,
    get_session_env,
    reset_delivery_context,
    set_session_vars,
    clear_session_vars,
)


def test_delivery_context_is_nest_safe_and_preserves_unrelated_context():
    outer = set_session_vars(
        platform="telegram",
        chat_id="chat-1",
        session_key="outer-key",
        session_id="outer-session",
        ui_session_id="outer-ui",
        async_delivery=False,
    )
    try:
        first = bind_delivery_context(
            session_key="web-key",
            session_id="web-session",
            ui_session_id="web-ui",
            async_delivery=True,
        )
        try:
            assert get_session_env("HERMES_SESSION_PLATFORM") == "telegram"
            assert get_session_env("HERMES_SESSION_CHAT_ID") == "chat-1"
            assert get_session_env("HERMES_SESSION_KEY") == "web-key"
            assert get_session_env("HERMES_SESSION_ID") == "web-session"
            assert get_session_env("HERMES_UI_SESSION_ID") == "web-ui"
            assert async_delivery_supported() is True

            nested = bind_delivery_context(
                session_key="nested-key",
                session_id="nested-session",
                ui_session_id="nested-ui",
                async_delivery=False,
            )
            try:
                assert get_session_env("HERMES_SESSION_KEY") == "nested-key"
                assert async_delivery_supported() is False
            finally:
                reset_delivery_context(nested)

            assert get_session_env("HERMES_SESSION_KEY") == "web-key"
            assert get_session_env("HERMES_SESSION_ID") == "web-session"
            assert get_session_env("HERMES_UI_SESSION_ID") == "web-ui"
            assert async_delivery_supported() is True
        finally:
            reset_delivery_context(first)

        assert get_session_env("HERMES_SESSION_PLATFORM") == "telegram"
        assert get_session_env("HERMES_SESSION_CHAT_ID") == "chat-1"
        assert get_session_env("HERMES_SESSION_KEY") == "outer-key"
        assert get_session_env("HERMES_SESSION_ID") == "outer-session"
        assert get_session_env("HERMES_UI_SESSION_ID") == "outer-ui"
        assert async_delivery_supported() is False
    finally:
        clear_session_vars(outer)
