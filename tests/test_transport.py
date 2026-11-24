import pytest

from equipment_mcp.transport import http_settings, server_url, stdio_params


def test_http_settings_from_url():
    assert http_settings("http://127.0.0.1:8000/mcp") == {
        "host": "127.0.0.1",
        "port": 8000,
        "streamable_http_path": "/mcp",
    }


def test_http_settings_defaults():
    assert http_settings("http://localhost") == {
        "host": "localhost",
        "port": 80,
        "streamable_http_path": "/mcp",
    }


@pytest.mark.parametrize("url", ["127.0.0.1:8000/mcp", "ftp://host/mcp", "http:///mcp"])
def test_http_settings_rejects_bad_url(url):
    with pytest.raises(ValueError):
        http_settings(url)


def test_server_url_empty_means_stdio(monkeypatch):
    monkeypatch.setenv("MCP_SERVER_URL", "")
    assert server_url() is None


def test_stdio_child_is_forced_to_stdio():
    assert stdio_params().args[-1] == "--stdio"
