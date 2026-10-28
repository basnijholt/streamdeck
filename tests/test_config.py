import json
from pathlib import Path

import pytest
import yaml

from deck.cli import main
from deck.config import Action, Config, ConfigError, load_config

FIXTURES = Path(__file__).parent / "fixtures"

MAC = {"host": "macbook-m5", "key": "~/.config/deck/mac_ed25519"}
HA = {"url": "http://ha.local:8123", "token_env": "HA_TOKEN"}


def write(tmp_path, data) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def home(*keys, dials=(), **sections):
    return {"pages": {"home": {"keys": list(keys), "dials": list(dials)}}, **sections}


def errors_of(tmp_path, data) -> list[str]:
    with pytest.raises(ConfigError) as e:
        load_config(write(tmp_path, data))
    return e.value.errors


def test_valid_fixture_loads():
    cfg = load_config(FIXTURES / "valid.yaml")
    assert cfg.pages["home"].keys[0].press == Action(type="mac.app", args="Ghostty")


def test_t3_slot_key_gets_focus_press():
    cfg = load_config(FIXTURES / "valid.yaml")
    key = cfg.pages["home"].keys[3]
    assert key.t3_slot == 0
    assert key.press == Action(type="t3.focus", args=0)


def test_unknown_action_type_reports_path(tmp_path):
    p = write(tmp_path, {"pages": {"home": {"keys": [None, None, None, {"press": {"mac.nope": 1}}]}}})
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert any(err.startswith("pages.home.keys.3.press") for err in e.value.errors)


def test_bad_action_args_report_nested_path(tmp_path):
    key = {"press": {"ddc": {"code": 16, "value": "loud"}}}
    errs = errors_of(tmp_path, home(key, ddc={"model": "X"}))
    assert any(err.startswith("pages.home.keys.0.press.value") for err in errs)


def test_unknown_key_rejected(tmp_path):
    errs = errors_of(tmp_path, home({"lable": "typo"}))
    assert any(err.startswith("pages.home.keys.0.lable") for err in errs)


BAD_URLS = [
    "file:///etc/passwd",
    "javascript:alert(1)",
    "ssh://x",
    "http://a b",
    "ht\ntp://x",
    " http://a",
    "http://a\n-x",
    "https://a\x00b",
    "http://a\x7f",
]


@pytest.mark.parametrize("url", BAD_URLS)
def test_mac_url_rejects_non_http(tmp_path, url):
    errs = errors_of(tmp_path, home({"press": {"mac.url": url}}, mac=MAC))
    assert any(err.startswith("pages.home.keys.0.press") for err in errs)


@pytest.mark.parametrize("url", BAD_URLS)
def test_mac_tab_url_rejects_non_http(tmp_path, url):
    key = {"press": {"mac.tab": {"match": "x", "url": url}}}
    errs = errors_of(tmp_path, home(key, mac=MAC))
    assert any(err.startswith("pages.home.keys.0.press") for err in errs)


def test_mac_url_accepts_http_and_https(tmp_path):
    keys = [{"press": {"mac.url": u}} for u in ("http://a.example", "https://a.example/x")]
    cfg = load_config(write(tmp_path, home(*keys, mac=MAC)))
    assert cfg.pages["home"].keys[1].press == Action(type="mac.url", args="https://a.example/x")


T3_SERVER = {
    "name": "pc",
    "url": "http://127.0.0.1:3773",
    "web_url": "https://t3.example",
    "environment_id": "env",
    "token_env": "T3_TOKEN",
}


def t3_section(*servers):
    return {"servers": list(servers), "models": {}}


@pytest.mark.parametrize("url", BAD_URLS)
def test_home_assistant_url_rejects_non_http(tmp_path, url):
    errs = errors_of(tmp_path, home(home_assistant={**HA, "url": url}))
    assert any(err.startswith("home_assistant.url") for err in errs)


@pytest.mark.parametrize("field", ["url", "web_url"])
@pytest.mark.parametrize("url", BAD_URLS)
def test_t3_server_urls_reject_non_http(tmp_path, field, url):
    errs = errors_of(tmp_path, home(t3=t3_section({**T3_SERVER, field: url})))
    assert any(err.startswith(f"t3.servers.0.{field}") for err in errs)


def test_home_assistant_and_t3_urls_accept_http_and_https(tmp_path):
    server = {**T3_SERVER, "url": "HTTP://127.0.0.1:3773", "web_url": "https://t3.example/x"}
    ha = {**HA, "url": "https://ha.example"}
    cfg = load_config(write(tmp_path, home(home_assistant=ha, t3=t3_section(server))))
    assert cfg.home_assistant.url == "https://ha.example"
    assert cfg.t3.servers[0].web_url == "https://t3.example/x"


def test_t3_effort_options_default_to_codex_reasoning_effort(tmp_path):
    cfg = load_config(write(tmp_path, home(t3=t3_section(T3_SERVER))))
    assert cfg.t3.effort_options == {"codex": "reasoningEffort"}


def test_t3_effort_options_can_be_replaced(tmp_path):
    section = {**t3_section(T3_SERVER), "effort_options": {"codex": "effort", "other": "depth"}}
    cfg = load_config(write(tmp_path, home(t3=section)))
    assert cfg.t3.effort_options == {"codex": "effort", "other": "depth"}


def test_duplicate_t3_server_names_rejected(tmp_path):
    other = {**T3_SERVER, "name": "laptop"}
    again = {**T3_SERVER, "url": "http://127.0.0.1:3774"}
    errs = errors_of(tmp_path, home(t3=t3_section(T3_SERVER, other, again)))
    assert len(errs) == 1
    assert errs[0].startswith("t3.servers.2.name") and "'pc'" in errs[0]


def test_distinct_t3_server_names_accepted(tmp_path):
    other = {**T3_SERVER, "name": "laptop"}
    cfg = load_config(write(tmp_path, home(t3=t3_section(T3_SERVER, other))))
    assert [s.name for s in cfg.t3.servers] == ["pc", "laptop"]


def test_too_many_keys_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, home(*[None] * 37)))


def test_36_keys_and_6_dials_accepted(tmp_path):
    dials = [{"kind": "volume"}] * 6
    cfg = load_config(write(tmp_path, home(*[None] * 36, dials=dials, mac=MAC)))
    assert len(cfg.pages["home"].keys) == 36


def test_too_many_dials_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, home(dials=[{"kind": "volume"}] * 7)))


def test_mac_action_without_mac_section(tmp_path):
    errs = errors_of(tmp_path, home({"press": {"mac.app": "Ghostty"}}))
    assert any("mac" in err and err.startswith("pages.home.keys.0.press") for err in errs)


def test_long_press_is_checked_too(tmp_path):
    errs = errors_of(tmp_path, home({"long_press": {"mac.lock": None}}))
    assert any(err.startswith("pages.home.keys.0.long_press") for err in errs)


def test_ha_action_and_state_need_home_assistant(tmp_path):
    key = {
        "state": {"ha": "light.x"},
        "press": {"ha": {"service": "light.toggle", "entity_id": "light.x"}},
    }
    errs = errors_of(tmp_path, home(key))
    assert any(err.startswith("pages.home.keys.0.state") for err in errs)
    assert any(err.startswith("pages.home.keys.0.press") for err in errs)


def test_ddc_action_needs_ddc_section(tmp_path):
    key = {"press": {"ddc": {"code": 16, "value": 35}}}
    errs = errors_of(tmp_path, home(key))
    assert any(err.startswith("pages.home.keys.0.press") and "ddc" in err for err in errs)
    assert load_config(write(tmp_path, home(key, ddc={"model": "X"}))).ddc is not None


def test_t3_action_needs_t3_section(tmp_path):
    errs = errors_of(tmp_path, home({"t3.slot": 2}))
    assert any("t3" in err for err in errs)


def test_dial_requirements(tmp_path):
    dials = [{"kind": "brightness"}, {"kind": "t3.model"}, {"kind": "ha.brightness"}]
    errs = errors_of(tmp_path, home(dials=dials))
    assert any(err.startswith("pages.home.dials.0") and "ddc" in err for err in errs)
    assert any(err.startswith("pages.home.dials.1") and "t3" in err for err in errs)
    assert any(err.startswith("pages.home.dials.2") and "home_assistant" in err for err in errs)


def test_volume_and_input_dials_need_mac_and_audio(tmp_path):
    errs = errors_of(tmp_path, home(dials=[{"kind": "volume"}, {"kind": "input"}]))
    assert any(err.startswith("pages.home.dials.0") and "mac" in err for err in errs)
    assert any(err.startswith("pages.home.dials.1") and "mac" in err for err in errs)
    assert any(err.startswith("pages.home.dials.1") and "audio" in err for err in errs)
    assert not any(err.startswith("pages.home.dials.0") and "audio" in err for err in errs)

    errs = errors_of(tmp_path, home(dials=[{"kind": "input"}], mac=MAC))
    assert [e.split(":")[0] for e in errs] == ["pages.home.dials.0.kind"]
    assert "audio" in errs[0]

    audio = {"outputs": [], "inputs": ["q9"]}
    ok = home(dials=[{"kind": "volume"}, {"kind": "input"}], mac=MAC, audio=audio)
    assert len(load_config(write(tmp_path, ok)).pages["home"].dials) == 2


def test_utf8_config_loads_when_default_encoding_is_not_utf8(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(home({"icon": "\U0001f47b"}), allow_unicode=True), encoding="utf-8")
    # Simulate a service environment whose default text encoding is ASCII.
    monkeypatch.setattr("io.text_encoding", lambda encoding, stacklevel=2: encoding or "ascii")
    assert load_config(p).pages["home"].keys[0].icon == "\U0001f47b"


def test_ha_brightness_dial_needs_entity_id(tmp_path):
    errs = errors_of(tmp_path, home(dials=[{"kind": "ha.brightness"}], home_assistant=HA))
    assert errs == ["pages.home.dials.0.entity_id: ha.brightness dial requires entity_id"]


def test_page_target_must_exist(tmp_path):
    errs = errors_of(tmp_path, home({"press": {"page": "nowhere"}}))
    assert any(err.startswith("pages.home.keys.0.press") and "nowhere" in err for err in errs)


def test_page_target_resolves(tmp_path):
    data = home({"press": {"page": "two"}})
    data["pages"]["two"] = {"keys": []}
    assert load_config(write(tmp_path, data)).pages["home"].keys[0].press.args == "two"


def test_start_page_must_exist(tmp_path):
    errs = errors_of(tmp_path, {"start_page": "missing", "pages": {"home": {}}})
    assert any(err.startswith("start_page") for err in errs)


def test_output_name_must_exist_in_audio(tmp_path):
    audio = {"outputs": [{"name": "iLoud", "match": "iloud"}], "inputs": []}
    ok = home({"press": {"mac.audio.output": {"name": "iLoud"}}}, mac=MAC, audio=audio)
    load_config(write(tmp_path, ok))
    bad = home({"press": {"mac.audio.output": {"name": "Nope"}}}, mac=MAC, audio=audio)
    errs = errors_of(tmp_path, bad)
    assert any(err.startswith("pages.home.keys.0.press") and "Nope" in err for err in errs)


def test_output_name_without_audio_section(tmp_path):
    bad = home({"press": {"mac.audio.output": {"name": "iLoud"}}}, mac=MAC)
    assert errors_of(tmp_path, bad)


@pytest.mark.parametrize("name", ["../x", "a/b", "x y", ""])
def test_script_name_rejects_path(tmp_path, name):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, home({"press": {"mac.script": name}}, mac=MAC)))


def test_script_name_accepts_plain_name(tmp_path):
    cfg = load_config(write(tmp_path, home({"press": {"mac.script": "do-it_1.sh"}}, mac=MAC)))
    assert cfg.pages["home"].keys[0].press.args == "do-it_1.sh"


def test_all_structural_errors_reported_at_once(tmp_path):
    keys = [{"press": {"mac.nope": 1}}, {"press": {"mac.app": 5}}]
    errs = errors_of(tmp_path, home(*keys, mac=MAC))
    assert [e.split(":")[0] for e in errs] == ["pages.home.keys.0.press", "pages.home.keys.1.press"]


def test_all_reference_errors_reported_at_once(tmp_path):
    keys = [{"press": {"page": "x"}}, {"press": {"mac.lock": None}}]
    errs = errors_of(tmp_path, {**home(*keys), "start_page": "y"})
    assert sorted(e.split(":")[0] for e in errs) == [
        "pages.home.keys.0.press",
        "pages.home.keys.1.press",
        "start_page",
    ]


def test_t3_focus_slot_range(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, home({"t3.slot": 6})))


def test_invalid_yaml_is_config_error(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("pages: [unclosed")
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert e.value.errors


def test_missing_file_is_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def test_empty_file_is_config_error(tmp_path):
    p = tmp_path / "empty.yaml"
    p.write_text("")
    with pytest.raises(ConfigError):
        load_config(p)


def test_json_schema_available():
    schema = Config.model_json_schema()
    assert json.dumps(schema)
    assert "t3.slot" in json.dumps(schema)


def test_cli_check_ok_and_error(tmp_path, capsys):
    assert main(["check", str(FIXTURES / "valid.yaml")]) == 0
    assert capsys.readouterr().out.strip() == "ok"
    bad = write(tmp_path, home({"press": {"mac.app": "Ghostty"}}))
    assert main(["check", str(bad)]) == 1
    out = capsys.readouterr()
    assert "pages.home.keys.0.press" in out.out + out.err


def test_cli_schema_prints_json(capsys):
    assert main(["schema"]) == 0
    assert json.loads(capsys.readouterr().out)["title"] == "Config"
