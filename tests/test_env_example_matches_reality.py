"""`.env.example` is the first file a beta user copies. It drifted.

It pointed at api.hermix.network (a domain we do not run) and described a
"device login" flow that no longer exists, so anyone following it landed on a
broken install and concluded the product was broken. Nothing caught it, because
documentation has no tests — so these are its tests.

Every assertion here is about drift between the example and the code that
actually reads it. None of them constrain what the defaults SHOULD be.
"""
import pathlib
import re

import pytest

from hermix import _config

ENV_EXAMPLE = pathlib.Path(__file__).resolve().parent.parent / ".env.example"
REPO = ENV_EXAMPLE.parent


def _pairs():
    """Every KEY=VALUE in the example, comments stripped."""
    out = {}
    for raw in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def test_the_example_exists_and_parses():
    assert ENV_EXAMPLE.is_file()
    assert _pairs(), "no KEY=VALUE lines survived parsing"


def test_every_documented_key_is_one_the_code_actually_reads():
    """A key here that nothing reads is a lie the user cannot detect."""
    source = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in REPO.glob("*.py"))
    unknown = [k for k in _pairs() if k not in source]
    assert not unknown, f"documented but never read: {unknown}"


def test_the_hub_url_matches_the_one_the_code_defaults_to():
    """The exact drift that shipped: two different domains, one of them ours."""
    assert _pairs()["HERMIX_API_URL"] == _config.DEFAULT_API_URL


def test_the_example_does_not_name_a_domain_we_do_not_run():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    domains = set(re.findall(r"https?://([a-z0-9.\-]+)", text))
    assert domains <= {"api.hermix.dev"}, f"unexpected domain(s): {domains}"


@pytest.mark.parametrize("key,getter", [
    ("HERMIX_LLM", _config.llm_mode),
    ("HERMIX_MATCH_EVERY_HOURS", _config.match_every_hours),
    ("HERMIX_MAX_NEW_DIGS_PER_CYCLE", _config.max_new_digs_per_cycle),
    ("HERMIX_MAX_NOTIFY_PER_DAY", _config.max_notify_per_day),
])
def test_documented_values_are_the_real_defaults(monkeypatch, key, getter):
    """The example claims to restate the defaults. If it quietly stops matching
    them, a user reading it is being misinformed about their own agent."""
    monkeypatch.delenv(key, raising=False)
    documented = _pairs()[key]
    assert str(getter()) == documented, (
        f"{key}: example says {documented!r}, code defaults to {getter()!r}")


def test_the_billing_promise_is_stated_where_the_user_configures_it():
    """HERMIX_LLM=auto spends the user's own money. Someone skimming for a knob
    to flip must be able to see that without reading the source."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8").lower()
    assert "hermix_llm" in text
    assert "never spent" in text or "never be spent" in text
    assert "your own" in text


def test_no_stale_onboarding_story():
    """"Bearer token from device login" described a flow that does not exist;
    registration is automatic. Fail if that language comes back."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8").lower()
    for stale in ("device login", "phase 1", "hermix.network"):
        assert stale not in text, f"stale onboarding language returned: {stale!r}"
