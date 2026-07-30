import importlib


def test_default_values(monkeypatch):
    monkeypatch.delenv("TENANT_NAMESPACE", raising=False)
    monkeypatch.delenv("APPLICATION", raising=False)
    monkeypatch.delenv("MAX_RETRIES", raising=False)
    monkeypatch.delenv("AI_ANALYSIS_ENABLED", raising=False)
    monkeypatch.delenv("RETRY_ENABLED", raising=False)
    monkeypatch.delenv("AI_MODEL", raising=False)

    import calunga_release_watcher.config as config
    importlib.reload(config)

    assert config.TENANT_NAMESPACE == "calunga-tenant"
    assert config.APPLICATION == "calunga-v2-index-main"
    assert config.APPLICATIONS == {"calunga-v2-index-main"}
    assert config.MAX_RETRIES == 3
    assert config.AI_ANALYSIS_ENABLED is False
    assert config.RETRY_ENABLED is False
    assert config.AI_MODEL == "claude-haiku-4-5"


def test_env_override_strings(monkeypatch):
    monkeypatch.setenv("TENANT_NAMESPACE", "custom-ns")
    monkeypatch.setenv("APPLICATION", "my-app")

    import calunga_release_watcher.config as config
    importlib.reload(config)

    assert config.TENANT_NAMESPACE == "custom-ns"
    assert config.APPLICATION == "my-app"
    assert config.APPLICATIONS == {"my-app"}


def test_comma_separated_applications(monkeypatch):
    monkeypatch.setenv("APPLICATION", "pnc-import, pnc-import-remediated , other-app")

    import calunga_release_watcher.config as config
    importlib.reload(config)

    assert config.APPLICATIONS == {"pnc-import", "pnc-import-remediated", "other-app"}


def test_env_override_int(monkeypatch):
    monkeypatch.setenv("MAX_RETRIES", "5")
    monkeypatch.setenv("AI_MAX_LOG_LINES", "500")

    import calunga_release_watcher.config as config
    importlib.reload(config)

    assert config.MAX_RETRIES == 5
    assert config.AI_MAX_LOG_LINES == 500


def test_env_override_bool(monkeypatch):
    monkeypatch.setenv("RETRY_ENABLED", "true")
    monkeypatch.setenv("AI_ANALYSIS_ENABLED", "True")

    import calunga_release_watcher.config as config
    importlib.reload(config)

    assert config.RETRY_ENABLED is True
    assert config.AI_ANALYSIS_ENABLED is True


def test_bool_false_values(monkeypatch):
    monkeypatch.setenv("RETRY_ENABLED", "false")
    monkeypatch.setenv("AI_ANALYSIS_ENABLED", "no")

    import calunga_release_watcher.config as config
    importlib.reload(config)

    assert config.RETRY_ENABLED is False
    assert config.AI_ANALYSIS_ENABLED is False
