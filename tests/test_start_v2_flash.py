"""The launch profile must not mutate .env, environment variables or launch services in tests."""
from scripts import start_v2_flash


def test_profile_overrides_only_the_three_choices():
    original = {
        "TICKETMIND_KNOWLEDGE_DATASET": "production-v1",
        "TICKETMIND_DECISION_MODEL": "qwen3.8-max-0902",
        "TICKETMIND_RETRIEVAL_MODE": "hybrid",
        "TICKETMIND_JUDGE_MODEL": "deepseek-v4.1-flash",
        "DASHSCOPE_API_KEY": "local-secret-for-test",
        "TICKETMIND_DATABASE_URL": "postgresql+psycopg://local-only",
    }
    actual = start_v2_flash.profile_environment(original)
    assert {key: actual[key] for key in start_v2_flash.PROFILE} == start_v2_flash.PROFILE
    assert actual["TICKETMIND_JUDGE_MODEL"] == original["TICKETMIND_JUDGE_MODEL"]
    assert actual["DASHSCOPE_API_KEY"] == original["DASHSCOPE_API_KEY"]
    assert actual["TICKETMIND_DATABASE_URL"] == original["TICKETMIND_DATABASE_URL"]
    assert original["TICKETMIND_KNOWLEDGE_DATASET"] == "production-v1"


def test_launcher_forwards_existing_startup_flags_without_starting_services(monkeypatch):
    recorded = {}
    monkeypatch.setattr(
        start_v2_flash.subprocess,
        "call",
        lambda command, *, cwd, env: recorded.update(command=command, cwd=cwd, env=env) or 0,
    )
    assert start_v2_flash.main(["--skip-infra", "--check"]) == 0
    assert recorded["command"][-2:] == ["--skip-infra", "--check"]
    assert recorded["command"][1] == str(start_v2_flash.ROOT / "scripts" / "start_local.py")
    assert recorded["cwd"] == start_v2_flash.ROOT
    assert all(recorded["env"][key] == value for key, value in start_v2_flash.PROFILE.items())
