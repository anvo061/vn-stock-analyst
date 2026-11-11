import json
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from stock_analyst import core, serve
from stock_analyst.api import InviteStore

TOKEN = "test-token-not-real-0123456789abcdefABCDEFG"


def parse_fragment(link):
    """Read the link the way web/src/lib.js parseInviteHash does."""
    return {k: v[0] for k, v in parse_qs(urlsplit(link).fragment).items()}


# --- build_invite_link ------------------------------------------------------------------

def test_link_carries_api_and_token_after_the_hash():
    link = serve.build_invite_link("https://ban.github.io/bang-dien/", TOKEN, "https://abc-def.trycloudflare.com")
    assert link == f"https://ban.github.io/bang-dien/#api=https%3A%2F%2Fabc-def.trycloudflare.com&token={TOKEN}"
    assert parse_fragment(link) == {"api": "https://abc-def.trycloudflare.com", "token": TOKEN}
    assert TOKEN not in urlsplit(link).path + urlsplit(link).query  # nothing secret reaches the web host's server


def test_link_without_api_uses_the_same_origin():
    link = serve.build_invite_link("http://localhost:5173", TOKEN)
    assert link == f"http://localhost:5173/#token={TOKEN}"


def test_api_is_reduced_to_its_origin_rules():
    assert unquote(parse_fragment(serve.build_invite_link("https://x.test/", TOKEN, "https://abc.trycloudflare.com/"))["api"]) == "https://abc.trycloudflare.com"
    for bad in ("https://abc.trycloudflare.com/api", "https://abc.trycloudflare.com/?a=1", "http://abc.trycloudflare.com",
                "ftp://x.test", "javascript:alert(1)", "https://user:pw@x.test"):
        with pytest.raises(ValueError):
            serve.build_invite_link("https://x.test/", TOKEN, bad)


@pytest.mark.parametrize("web", ["http://ban.github.io/", "https://x.test/#old", "x.test", "", "https://u:p@x.test/"])
def test_bad_web_addresses_are_refused(web):
    with pytest.raises(ValueError):
        serve.build_invite_link(web, TOKEN)


@pytest.mark.parametrize("token", ["", "short", "has space in it here!!", "a&token=evil-but-long-enough", "x" * 200])
def test_malformed_tokens_are_refused(token):
    with pytest.raises(ValueError):
        serve.build_invite_link("https://x.test/", token)


# --- CLI --------------------------------------------------------------------------------

@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    config = tmp_path / "live.json"
    config.write_text(json.dumps({"sources": ["vietcap"], "allowed_origins": [], "port": 8787}), encoding="utf-8")
    monkeypatch.setattr(serve, "CONFIG_PATH", config)
    return tmp_path, config


def run(capsys, *argv):
    code = serve.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_add_with_web_prints_a_working_link(cli_env, capsys):
    tmp, _ = cli_env
    code, out = run(capsys, "invite", "add", "An", "--web", "https://ban.github.io/bang-dien/", "--api", "https://abc.trycloudflare.com")
    assert code == 0 and parse_fragment(out["link"])["token"] == out["token"]
    assert InviteStore(tmp / "live" / "invites.json").who(out["token"]) == "An"


def test_add_without_web_prints_only_the_token(cli_env, capsys):
    code, out = run(capsys, "invite", "add", "An")
    assert code == 0 and "link" not in out and out["token"]


def test_web_url_can_come_from_config(cli_env, capsys):
    _, config = cli_env
    config.write_text(json.dumps({"sources": [], "allowed_origins": [], "port": 1, "web_url": "https://ban.github.io/bang-dien/"}), encoding="utf-8")
    code, out = run(capsys, "invite", "add", "An")
    assert code == 0 and out["link"].startswith("https://ban.github.io/bang-dien/#token=")


def test_bad_address_creates_no_invite(cli_env, capsys):
    tmp, _ = cli_env
    code, out = run(capsys, "invite", "add", "An", "--web", "http://ban.github.io/")
    assert code == 1 and "https" in out["error"]
    assert not (tmp / "live" / "invites.json").exists()


def test_link_for_existing_token_and_refusal_after_revoke(cli_env, capsys):
    _, out = run(capsys, "invite", "add", "An")
    token = out["token"]
    code, linked = run(capsys, "invite", "link", "--token", token, "--web", "https://x.test/", "--api", "https://abc.trycloudflare.com")
    assert code == 0 and parse_fragment(linked["link"])["token"] == token
    run(capsys, "invite", "revoke", "An")
    code, refused = run(capsys, "invite", "link", "--token", token, "--web", "https://x.test/")
    assert code == 1 and "thu hồi" in refused["error"]


def test_link_needs_a_web_address(cli_env, capsys):
    _, out = run(capsys, "invite", "add", "An")
    code, refused = run(capsys, "invite", "link", "--token", out["token"])
    assert code == 1 and "--web" in refused["error"]
