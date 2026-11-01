"""AMS migration: secrets come from the environment (AMS secret), logs use the level-prefix format."""

from __future__ import annotations

import logging

from autocut.worker import config, server
from autocut.worker.config import WorkerConfig, load_secrets


def test_load_secrets_reads_env_without_file(monkeypatch):
    monkeypatch.setenv("AUTOCUT_FEIYU_KEY", "fk-env")
    monkeypatch.setenv("AUTOCUT_DSH_EVENT_TOKEN", "tok-env")
    monkeypatch.setenv("UNRELATED_SECRET", "nope")
    got = load_secrets(None)
    assert got == {"AUTOCUT_FEIYU_KEY": "fk-env", "AUTOCUT_DSH_EVENT_TOKEN": "tok-env"}


def test_load_secrets_env_wins_and_file_fills_the_rest(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOCUT_DSH_EVENT_TOKEN", "tok-env")
    monkeypatch.delenv("AUTOCUT_FEIYU_KEY", raising=False)
    sec = tmp_path / "w.env"
    sec.write_text("AUTOCUT_DSH_EVENT_TOKEN=tok-file\nAUTOCUT_FEIYU_KEY=fk-file\n")
    got = load_secrets(str(sec))
    assert got["AUTOCUT_DSH_EVENT_TOKEN"] == "tok-env"
    assert got["AUTOCUT_FEIYU_KEY"] == "fk-file"


def test_load_secrets_empty_env_value_is_ignored(monkeypatch):
    monkeypatch.setenv("AUTOCUT_FEIYU_KEY", "")
    monkeypatch.delenv("AUTOCUT_DSH_EVENT_TOKEN", raising=False)
    assert load_secrets(None) == {}


def test_emitter_enabled_from_env_secret(monkeypatch, tmp_path):
    from autocut.worker import events

    monkeypatch.setenv("AUTOCUT_DSH_EVENT_TOKEN", "tok-env")
    assert events.Emitter(WorkerConfig(root=tmp_path)).enabled
    monkeypatch.delenv("AUTOCUT_DSH_EVENT_TOKEN")
    assert not events.Emitter(WorkerConfig(root=tmp_path)).enabled


def test_secret_names_are_the_declared_ones():
    assert set(config.SECRET_NAMES) >= {"AUTOCUT_FEIYU_KEY", "AUTOCUT_DSH_EVENT_TOKEN"}


def test_log_format_level_prefix_under_ams():
    fmt = server.log_format({"AMS_SERVICE_ID": "autocut-worker"})
    rec = logging.LogRecord("autocut.worker.server", logging.WARNING, __file__, 1, "boom %s", ("x",), None)
    line = logging.Formatter(fmt).format(rec)
    assert line == "WARNING autocut.worker.server: boom x"


def test_log_format_keeps_timestamp_outside_ams():
    assert server.log_format({}).startswith("%(asctime)s ")


def test_ingest_env_never_passes_the_keychain_cookie_source(tmp_path):
    from autocut.worker import ingest

    env_file = tmp_path / "ingest.env"
    env_file.write_text("KAOROU_YT_COOKIES_PATH=/x/yt-cookies.txt\nKAOROU_YT_COOKIES_BROWSER=chrome\nOPENROUTER_API_KEY=nope\n")
    env = ingest.ingest_env(WorkerConfig(root=tmp_path, ingest_env_file=str(env_file)))
    assert env["KAOROU_YT_COOKIES_PATH"] == "/x/yt-cookies.txt"
    assert "KAOROU_YT_COOKIES_BROWSER" not in env
    assert "OPENROUTER_API_KEY" not in env
