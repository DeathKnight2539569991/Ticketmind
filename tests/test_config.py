from ticketmind.core.config import get_settings


def test_get_settings(monkeypatch) -> None:
    monkeypatch.setenv("TICKETMIND_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv(
        "TICKETMIND_DATABASE_URL",
        "postgresql+psycopg://ticketmind_app:localpassword@localhost:5432/ticketmind_test",
    )

    get_settings.cache_clear()

    try:
        settings = get_settings()

        assert settings.log_level == "DEBUG"
        assert (
            settings.database_url.unicode_string()
            == "postgresql+psycopg://ticketmind_app:localpassword@localhost:5432/ticketmind_test"
        )
    finally:
        get_settings.cache_clear()