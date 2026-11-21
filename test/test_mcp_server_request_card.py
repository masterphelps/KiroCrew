"""An agent changes MCP servers only by asking: ``request_server`` -> ``mcp.server`` card.

The agent's file tool is refused on both MCP registries, and the refusal names
``request_server``. The sandbox seals ``<data home>/mcp.json`` read-only. The tool
raises a card that shows the server's name and the change to its entry; Apply sends
it through the MCP page's own routes, so the store holds exactly the entry the card
showed; **Use in open chats now** adds the session restart; Cancel writes nothing.

Through the real card engine and the real ``/api/mcp/custom`` routes on a temporary
store. The remove and restart steps are recorded, not run: their own routes are
covered by the MCP page's tests.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from change_cards_helpers import SK, FakeState, _card_headers, _fake_auth
from mcp_launch_approval_helpers import (  # noqa: F401 - fixture registered by name
    _stub_request,
    toggle_home_fixture,
)

from kiro_crew import change_card_catalog as catalog
from kiro_crew import sandbox_leaves, sandbox_precreate
from kiro_crew.config import write_provenance
from kiro_crew.dashboard.change_cards import CardStore
from kiro_crew.dashboard.handlers import change_cards as routes
from kiro_crew.dashboard.handlers import mcp as mcp_mod
from kiro_crew.dashboard.handlers import mcp_custom, request_server_page
from kiro_crew.dashboard.handlers import template_server_request as tsr
from kiro_crew.security.paths import sensitive_write_path_refusal
from kiro_crew.testing.synthetic_credentials import github_classic

AGENT = {"X-Test-Auth": "internal", "X-Session-Key": SK}
ENTRY = {"command": "npx", "args": ["-y", "@acme/weather-mcp"], "env": {"CITY": "Paris"}}

# Exercise the real resolver and approval store rather than the suite's launch stub.
ENFORCE_LAUNCH_APPROVAL = True


# ── the registries are not the agent's to write ──


@pytest.mark.parametrize("leaf", [".kiro/crew/mcp.json", ".kiro/settings/mcp.json"])
def test_an_agent_write_to_an_mcp_registry_is_refused_and_names_request_server(leaf):
    reason = sensitive_write_path_refusal(str(Path.home() / leaf))
    assert reason is not None and reason.startswith("Blocked: write to protected path")
    assert "request_server" in reason


def test_another_protected_path_keeps_its_plain_refusal():
    reason = sensitive_write_path_refusal(str(Path.home() / ".kiro/crew/config.json"))
    assert reason is not None and "request_server" not in reason


def test_the_sandbox_seals_the_store_and_vouches_for_the_gateways_own_saves():
    # A sandboxed shell cannot write it, nor create it while absent.
    assert "mcp.json" in sandbox_leaves._CREW_READONLY_LEAVES
    assert "mcp.json" in sandbox_precreate._CREW_PRECREATE_READONLY_FILE_LEAVES
    # The gateway's save is a new inode; the launcher's pin must not refuse it.
    assert "mcp.json" in write_provenance.PUBLISHED_LEAVES


# ── request_server -> card -> Apply / Cancel ──


class _Null:
    def log_api_access(self, **_kw: Any) -> None:
        return None


@pytest.fixture
def env(tmp_path, monkeypatch):
    from kiro_crew.agent_materialization import mcp_sources
    from kiro_crew.config import paths

    monkeypatch.setattr(paths, "_agents_dir_override", lambda: tmp_path / "agents")
    monkeypatch.setattr(paths, "_mate_agents_dir_override", lambda: tmp_path / "mates")
    monkeypatch.setattr(mcp_sources, "_app_owned_mcp_keys", lambda: {})
    monkeypatch.setattr(mcp_mod, "_extra_mcp_scopes", lambda: [])
    store_path = tmp_path / "mcp.json"
    monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", store_path)
    monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", tmp_path / "kiro.mcp.json")
    monkeypatch.setattr(mcp_mod, "_MCP_LOCK_PATH", tmp_path / "kiro.mcp.lock")
    monkeypatch.setattr(routes, "sel", lambda: _Null())
    monkeypatch.setattr(mcp_custom, "sel", lambda: _Null())
    monkeypatch.setattr(tsr, "_audit", lambda *a, **k: None)

    async def rebuilt() -> bool:
        return True

    async def recorded(_entries: dict) -> None:
        return None

    monkeypatch.setattr(mcp_custom, "_rebuild_agent_config", rebuilt)
    monkeypatch.setattr(mcp_custom, "_record_owner_servers", recorded)
    monkeypatch.setattr(mcp_custom, "_tell_running_sessions", lambda request, names: None)
    seen: dict[str, Any] = {
        "removed": [],
        "restarts": 0,
        "path": store_path,
        "stubs": [],
        "enabled": [],
        "sharing": {"sharing_on": False, "shared": False, "cwd_use": None},
    }
    monkeypatch.setattr(request_server_page, "_sharing_state", lambda name: dict(seen["sharing"]))
    return seen


def _app(store: CardStore, seen: dict[str, Any]) -> web.Application:
    async def remove(request: web.Request) -> web.Response:
        name = (await request.json())["name"]
        seen["removed"].append(name)
        mcp_mod._remove_kirocrew_entry(name)
        return web.json_response({"ok": True})

    async def restart(request: web.Request) -> web.Response:
        seen["restarts"] += 1
        return web.json_response({"ok": True, "sessions_reset": 1})

    app = web.Application(middlewares=[_fake_auth, routes.change_card_middleware])
    app["state"] = FakeState(store)
    routes.register_change_card_routes(app)
    app.router.add_post("/api/mcp/custom", mcp_custom.api_mcp_custom_add)
    app.router.add_put("/api/mcp/custom/{name}", mcp_custom.api_mcp_custom_update)
    app.router.add_post("/api/mcp/remove", remove)
    app.router.add_post("/api/sessions/restart", restart)

    async def launch(request: web.Request) -> web.Response:
        if "launches" in seen:
            return web.json_response(
                mcp_mod.display_launches(request.query["name"], seen["launches"])
            )
        return web.json_response({"name": request.query["name"], "expected_launch": "launch-tok"})

    async def stub(request: web.Request) -> web.Response:
        if seen.get("real_stub"):
            response = await mcp_mod.api_mcp_gateway_set_stub(request)
            seen["stub_response"] = json.loads(response.body)
            return response
        seen["stubs"].append(await request.json())
        return web.json_response({"ok": True, "applied": True, "restart_required": False})

    async def enable(request: web.Request) -> web.Response:
        seen["enabled"].append((await request.json())["enabled"])
        return web.json_response({"ok": True, "enabled": True, "running": True})

    app.router.add_post("/api/mcp-gateway/enable", enable)
    app.router.add_get("/api/mcp-gateway/servers/launch", launch)
    app.router.add_post("/api/mcp-gateway/servers/stub", stub)
    return app


def _run(store: CardStore, seen: dict[str, Any], fn):
    async def main():
        client = TestClient(TestServer(_app(store, seen)))
        await client.start_server()
        try:
            out = await fn(client)
            if routes._BACKGROUND:
                await asyncio.gather(*list(routes._BACKGROUND))
            return out
        finally:
            await client.close()

    return asyncio.run(main())


async def _ask(c, body: dict[str, Any], *, claim: bool = True) -> tuple[int, dict[str, Any]]:
    """Ask as an agent would; with an entry, the agent says it does not use the folder."""
    if claim and "entry" in body and "uses_chat_folder" not in body:
        body = {**body, "uses_chat_folder": False}
    r = await c.post("/api/cards/agent/request-server", json=body, headers=AGENT)
    return r.status, await r.json()


async def _apply(c, card: dict[str, Any]) -> list[int]:
    """Run the plan as the browser does, filling a step field from an earlier answer."""
    statuses, answers = [], []
    for i, step in enumerate(card["plan"]["apply"]):
        body = step["body"]
        if step.get("fill"):
            body = dict(body)
            for f in step["fill"]:
                body[f["field"]] = answers[f["step"]][f["key"]]
        kwargs = {} if step["method"] == "GET" else {"json": body}
        r = await c.request(
            step["method"], step["path"], headers=_card_headers(card, step=i), **kwargs
        )
        statuses.append(r.status)
        answers.append(await r.json())
        if r.status >= 400:
            break
    return statuses


def _stored(seen: dict[str, Any]) -> dict[str, Any]:
    return json.loads(seen["path"].read_text(encoding="utf-8"))["mcpServers"]


def _write_store(seen: dict[str, Any], servers: dict[str, Any]) -> None:
    seen["path"].write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def test_add_raises_a_card_and_apply_writes_the_entry_it_showed(env, tmp_path):
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        status, card = await _ask(
            c,
            {
                "action": "add",
                "source": "custom",
                "server": "weather",
                "entry": ENTRY,
                "uses_chat_folder": True,
            },
        )
        assert status == 200, card
        return card, await _apply(c, card)

    card, statuses = _run(store, env, go)
    assert card["kind"] == catalog.KIND_MCP_SERVER
    assert card["title"] == "Add server: weather"
    rows = {r["label"]: r for r in card["changes"]}
    assert rows["Command"]["after"] == "npx -y @acme/weather-mcp"
    assert rows["Environment"]["add"] == ["CITY=Paris"]
    assert card["params"]["use_now"] is True
    assert statuses == [200, 200]
    assert _stored(env)["weather"] == ENTRY
    assert env["restarts"] == 1


def test_change_shows_the_real_diff_and_redacts_a_stored_secret(env, tmp_path):
    secret = github_classic()
    _write_store(env, {"weather": {"command": "npx", "args": ["old"], "env": {"TOKEN": secret}}})
    store = CardStore(tmp_path / "c.json")
    new = {"command": "npx", "args": ["new"], "env": {"TOKEN": "fresh"}}

    async def go(c):
        status, card = await _ask(c, {"action": "change", "server": "weather", "entry": new})
        assert status == 200, card
        return card, await _apply(c, card)

    card, statuses = _run(store, env, go)
    assert card["title"] == "Change server: weather"
    rows = {r["label"]: r for r in card["changes"]}
    assert rows["Command"] == {"label": "Command", "before": "npx old", "after": "npx new"}
    assert rows["Environment"]["add"] == ["TOKEN=fresh"]
    # The stored secret never reaches the card the agent raised.
    assert rows["Environment"]["remove"] == ["TOKEN=[REDACTED: credential]"]
    assert secret not in json.dumps(card)
    assert statuses == [200, 200]
    assert _stored(env)["weather"] == new


@pytest.mark.parametrize(
    "field, label, sep, base",
    [
        ("env", "Environment", "=", {"command": "npx"}),
        ("headers", "Headers", ": ", {"url": "https://x.test"}),
    ],
)
@pytest.mark.parametrize(
    "old, new, side",
    [(None, "Paris", "add"), ("Rome", "Paris", "add"), ("Paris", None, "remove")],
    ids=["add", "change", "remove"],
)
def test_env_and_header_values_go_through_the_redactor_the_same_on_every_action(
    field, label, sep, base, old, new, side
):
    from kiro_crew.change_card_mcp_server import entry_rows

    secret = github_classic()

    def values(city: str | None) -> dict[str, str] | None:
        # The side the test reads holds CITY=Paris and the planted secret.
        if city is None:
            return None
        return {"CITY": city, "TOKEN": secret if city == "Paris" else "old"}

    old_v, new_v = values(old), values(new)
    before = None if old_v is None else {**base, field: old_v}
    after = None if new_v is None else {**base, field: new_v}
    rows = {r["label"]: r for r in entry_rows(before, after)}
    assert rows[label][side] == [f"CITY{sep}Paris", f"TOKEN{sep}[REDACTED: credential]"]
    assert secret not in json.dumps(rows)


def test_remove_sends_the_pages_remove(env, tmp_path):
    _write_store(env, {"weather": dict(ENTRY)})
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        status, card = await _ask(c, {"action": "remove", "server": "weather"})
        assert status == 200, card
        return card, await _apply(c, card)

    card, statuses = _run(store, env, go)
    assert card["title"] == "Remove server: weather"
    assert [s["path"] for s in card["plan"]["apply"]] == [
        "/api/mcp/remove",
        "/api/sessions/restart",
    ]
    assert statuses == [200, 200]
    assert env["removed"] == ["weather"]


def test_use_in_open_chats_now_unchecked_drops_the_restart(env, tmp_path):
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        status, card = await _ask(
            c,
            {
                "action": "add",
                "source": "custom",
                "server": "weather",
                "entry": ENTRY,
                "uses_chat_folder": True,
            },
        )
        assert status == 200, card
        r = await c.post(
            f"/api/cards/{card['id']}/preview",
            json={"params": {**card["params"], "use_now": False}, "revision": card["revision"]},
            headers={"X-Test-Auth": "owner"},
        )
        assert r.status == 200, await r.text()
        unchecked = await r.json()
        return unchecked, await _apply(c, unchecked)

    card, statuses = _run(store, env, go)
    assert [s["path"] for s in card["plan"]["apply"]] == ["/api/mcp/custom"]
    assert statuses == [200]
    assert _stored(env)["weather"] == ENTRY
    assert env["restarts"] == 0


def test_cancel_writes_nothing(env, tmp_path):
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        status, card = await _ask(
            c, {"action": "add", "source": "custom", "server": "weather", "entry": ENTRY}
        )
        assert status == 200, card
        r = await c.post(
            f"/api/cards/{card['id']}/cancel",
            json={"revision": card["revision"]},
            headers={"X-Test-Auth": "owner"},
        )
        return r.status, store.get(card["id"])

    status, final = _run(store, env, go)
    assert status == 200
    assert final["status"] == "cancelled"
    assert not env["path"].exists()
    assert env["restarts"] == 0


@pytest.mark.parametrize(
    "body, code",
    [
        ({"action": "change", "server": "nope", "entry": ENTRY}, "not_found"),
        ({"action": "remove", "server": "nope"}, "not_found"),
        ({"action": "add", "source": "custom", "server": "weather"}, "invalid_param"),
        ({"action": "move", "server": "weather"}, "invalid_action"),
    ],
)
def test_a_request_that_cannot_apply_is_refused_with_its_reason(env, tmp_path, body, code):
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        return await _ask(c, body)

    status, out = _run(store, env, go)
    assert status in (400, 404, 409), out
    assert out["code"] == code


@pytest.mark.parametrize(
    "entry, reason",
    [
        ({**ENTRY, "timeout": 30}, "unknown spec key 'timeout'"),
        ({**ENTRY, "type": "stdio"}, "unknown spec key 'type'"),
        ({**ENTRY, "cwd": "/srv"}, "unknown spec key 'cwd'"),
        (
            {"command": "npx", "url": "https://mcp.example.com"},
            "spec cannot have both 'command' and 'url'",
        ),
        ({"url": "https://mcp.example.com", "env": {"A": "b"}}, "'env' is not valid on a remote"),
    ],
)
def test_an_add_the_page_would_refuse_raises_no_card(env, tmp_path, entry, reason):
    # The card's Apply is POST /api/mcp/custom: an entry that route refuses must
    # be refused when the agent asks, with the route's reason, not after Apply.
    assert reason in (mcp_custom._validate_spec(entry) or "")
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        return await _ask(c, {"action": "add", "source": "custom", "server": "w", "entry": entry})

    status, out = _run(store, env, go)
    assert status == 400, out
    assert out["code"] == "invalid_param"
    assert reason in out["error"]
    assert store.pending(None) == []


def test_a_change_to_a_field_other_flows_manage_raises_no_card(env, tmp_path):
    # The PUT keeps ``disabledTools`` as stored and refuses a save that edits it.
    _write_store(env, {"weather": {**ENTRY, "disabledTools": ["delete"]}})
    store = CardStore(tmp_path / "c.json")
    edited = {**ENTRY, "args": ["new"], "disabledTools": []}

    async def go(c):
        return await _ask(c, {"action": "change", "server": "weather", "entry": edited})

    status, out = _run(store, env, go)
    assert status == 400, out
    assert "'disabledTools' is managed by other flows" in out["error"]
    assert store.pending(None) == []


def test_a_change_that_keeps_a_managed_field_applies(env, tmp_path):
    _write_store(env, {"weather": {**ENTRY, "disabledTools": ["delete"]}})
    store = CardStore(tmp_path / "c.json")
    edited = {**ENTRY, "args": ["new"], "disabledTools": ["delete"]}

    async def go(c):
        status, card = await _ask(c, {"action": "change", "server": "weather", "entry": edited})
        assert status == 200, card
        return await _apply(c, card)

    assert _run(store, env, go) == [200, 200]
    assert _stored(env)["weather"] == edited


@pytest.mark.parametrize(
    "kind, params",
    [
        ("mcp.server", {"action": "add", "server": "weather", "entry": ENTRY}),
        # The one other card an agent could add a launch line with: one way to ask.
        ("mcp.add_custom", {"servers": {"weather": ENTRY}}),
    ],
)
def test_propose_change_cannot_raise_an_mcp_registry_write(env, tmp_path, kind, params):
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        r = await c.post(
            "/api/cards/agent/propose", json={"kind": kind, "params": params}, headers=AGENT
        )
        return r.status, (await r.json())["code"]

    assert _run(store, env, go) == (403, "use_request_server")


# ── Turn on MCP server sharing ──


def _offer(card: dict[str, Any]) -> dict[str, Any] | None:
    return next((r for r in card["changes"] if r.get("field") == "share_offer"), None)


async def _repreview(c, card: dict[str, Any], **params: Any) -> dict[str, Any]:
    r = await c.post(
        f"/api/cards/{card['id']}/preview",
        json={"params": {**card["params"], **params}, "revision": card["revision"]},
        headers={"X-Test-Auth": "owner"},
    )
    assert r.status == 200, await r.text()
    return await r.json()


ADD = {"action": "add", "source": "custom", "server": "weather", "entry": ENTRY}


def test_an_entry_without_the_folder_claim_raises_no_card(env, tmp_path):
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        return await _ask(c, ADD, claim=False)

    status, out = _run(store, env, go)
    assert (status, out["code"]) == (400, "invalid_param")
    assert "uses_chat_folder" in out["error"]


def test_a_safe_server_is_offered_sharing_checked_and_apply_is_ready(env, tmp_path):
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        return (await _ask(c, ADD))[1]

    card = _run(store, env, go)
    assert _offer(card) == {"field": "share_offer", "label": "Sharing", "after": True}
    # Sharing off for the install: the same Apply turns it on, then shares the
    # server with the launch the write produced.
    assert [s["path"].split("?")[0] for s in card["plan"]["apply"]] == [
        "/api/mcp/custom",
        "/api/mcp-gateway/enable",
        "/api/mcp-gateway/servers/launch",
        "/api/mcp-gateway/servers/stub",
        "/api/sessions/restart",
    ]


def test_sharing_applies_with_the_launch_the_write_produced(env, tmp_path):
    env["sharing"]["sharing_on"] = True
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        card = (await _ask(c, ADD))[1]
        return card, await _apply(c, card)

    card, statuses = _run(store, env, go)
    assert [(s["method"], s["path"].split("?")[0]) for s in card["plan"]["apply"]] == [
        ("POST", "/api/mcp/custom"),
        ("GET", "/api/mcp-gateway/servers/launch"),
        ("POST", "/api/mcp-gateway/servers/stub"),
        ("POST", "/api/sessions/restart"),
    ]
    assert statuses == [200, 200, 200, 200]
    assert _stored(env)["weather"] == ENTRY
    assert env["stubs"] == [
        {
            "name": "weather",
            "stub": True,
            "cwd_use": "not_used",
            "expected_launch": "launch-tok",
            "expected_entry": ENTRY,
        }
    ]
    assert env["enabled"] == []


@pytest.mark.parametrize(
    "body",
    [
        # The asking agent says it uses the folder.
        {**ADD, "uses_chat_folder": True},
        # Kiro Crew's own reading finds the folder in an arg or env value ...
        {**ADD, "entry": {"command": "npx", "args": ["-y", "some-tool", "."]}},
        {**ADD, "entry": {"command": "node", "args": ["server.js", "./data"]}},
        {
            **ADD,
            "entry": {"command": "uvx", "args": ["tool"], "env": {"ROOT": "${workspaceFolder}"}},
        },
        {**ADD, "entry": {"command": "uvx", "args": ["tool", "--root", "$PWD"]}},
        # ... or in a known file, git or filesystem server.
        {
            **ADD,
            "entry": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"]},
        },
        {**ADD, "server": "git", "entry": {"command": "uvx", "args": ["mcp-server-git"]}},
        # A remote server runs no process here, so there is nothing to share.
        {**ADD, "entry": {"url": "https://mcp.example.com/sse"}},
    ],
)
def test_a_server_that_is_not_safe_to_share_has_no_box(env, tmp_path, body):
    env["sharing"]["sharing_on"] = True
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        return await _ask(c, body)

    status, card = _run(store, env, go)
    assert status == 200, card
    assert _offer(card) is None
    assert not [s for s in card["plan"]["apply"] if "mcp-gateway" in s["path"]]


@pytest.mark.parametrize("field", ["args", "env"])
@pytest.mark.parametrize(
    "value,uses_folder",
    [
        ("scripts/x.js", True),
        ("./x.py", True),
        ("server.py", True),
        ("index.mjs", True),
        ("src/main.ts", True),
        ("index.cjs", True),
        ("@acme/weather-mcp", False),
        ("weather-mcp@1.0", False),
        ("-y", False),
        ("--script=index.js", False),
        ("https://x.test/a.js", False),
        ("/opt/x/server.py", False),
        ("~/server.py", False),
        ("${HOME}/server.py", False),
    ],
)
def test_relative_scripts_use_the_chat_folder(field, value, uses_folder):
    from kiro_crew.change_card_mcp_server import detects_chat_folder, safe_to_share

    entry = {"command": "node", field: [value] if field == "args" else {"SCRIPT": value}}
    assert detects_chat_folder("weather", entry) is uses_folder
    assert (
        safe_to_share(
            {"server": "weather", "uses_chat_folder": False},
            entry,
            {"share_exclusive": True},
        )
        is not uses_folder
    )


def test_a_server_recorded_as_using_the_folder_has_no_box(env, tmp_path):
    _write_store(env, {"weather": dict(ENTRY)})
    env["sharing"] = {"sharing_on": True, "shared": False, "cwd_use": "uses_cwd"}
    store = CardStore(tmp_path / "c.json")
    new = {**ENTRY, "args": ["-y", "@acme/weather-mcp@2"]}

    async def go(c):
        return (await _ask(c, {"action": "change", "server": "weather", "entry": new}))[1]

    assert _offer(_run(store, env, go)) is None


def test_a_shared_server_that_now_uses_the_folder_stops_sharing(env, tmp_path):
    _write_store(env, {"weather": dict(ENTRY)})
    env["sharing"] = {"sharing_on": True, "shared": True, "cwd_use": "not_used"}
    store = CardStore(tmp_path / "c.json")
    new = {**ENTRY, "args": ["-y", "@acme/weather-mcp", "./cache"]}

    async def go(c):
        return (await _ask(c, {"action": "change", "server": "weather", "entry": new}))[1]

    card = _run(store, env, go)
    assert _offer(card) is None
    stub_steps = [s for s in card["plan"]["apply"] if s["path"].endswith("/stub")]
    assert [s["body"] for s in stub_steps] == [
        {"name": "weather", "stub": False, "cwd_use": "uses_cwd"}
    ]


def test_a_remove_offers_no_sharing(env, tmp_path):
    _write_store(env, {"weather": dict(ENTRY)})
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        return (await _ask(c, {"action": "remove", "server": "weather"}))[1]

    assert _offer(_run(store, env, go)) is None


def test_unchecking_sharing_on_a_shared_server_stops_sharing_it(env, tmp_path):
    _write_store(env, {"weather": dict(ENTRY)})
    env["sharing"] = {"sharing_on": True, "shared": True, "cwd_use": "not_used"}
    store = CardStore(tmp_path / "c.json")
    new = {**ENTRY, "args": ["-y", "@acme/weather-mcp@2"]}

    async def go(c):
        card = (await _ask(c, {"action": "change", "server": "weather", "entry": new}))[1]
        unchecked = await _repreview(c, card, share=False)
        return card, unchecked

    card, unchecked = _run(store, env, go)
    # A shared server stays shared by default, its new launch approved by the Apply.
    assert _offer(card)["after"] is True
    assert "/api/mcp-gateway/servers/stub" in [s["path"] for s in card["plan"]["apply"]]
    stub_steps = [s for s in unchecked["plan"]["apply"] if s["path"].endswith("/stub")]
    assert [s["body"] for s in stub_steps] == [{"name": "weather", "stub": False}]


def test_the_code_warning_shows_when_the_command_or_env_changes(env, tmp_path):
    remote = {"url": "https://x.test"}
    _write_store(env, {"weather": dict(ENTRY), "remote": dict(remote)})
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        env_only = (
            await _ask(
                c,
                {
                    "action": "change",
                    "server": "weather",
                    "entry": {**ENTRY, "env": {"CITY": "Rome"}},
                },
            )
        )[1]
        command = (
            await _ask(
                c, {"action": "change", "server": "weather", "entry": {**ENTRY, "args": ["other"]}}
            )
        )[1]
        added = (await _ask(c, {**ADD, "server": "news"}))[1]
        others = [
            (await _ask(c, {"action": "change", "server": "remote", "entry": {**remote, **extra}}))[
                1
            ]
            for extra in ({"scopes": ["read"]}, {"headers": {"X-City": "Paris"}})
        ]
        return env_only, command, added, others

    env_only, command, added, others = _run(store, env, go)
    # An env change alone (e.g. NODE_OPTIONS) changes the code that runs.
    assert env_only["risk"] == "code_exec"
    assert command["risk"] == "code_exec"
    assert added["risk"] == "code_exec"
    # A field that is neither the launch line nor env, headers included, does not.
    assert [o.get("risk") for o in others] == ["normal", "normal"], others


@pytest.mark.parametrize("source", ["cli", "template", "markdown", "mate", "app", "invalid"])
def test_a_second_declarer_has_no_share_offer(env, tmp_path, monkeypatch, source):
    from kiro_crew.agent_materialization import mcp_sources

    _write_store(env, {"weather": dict(ENTRY)})
    if source == "app":
        monkeypatch.setattr(mcp_sources, "_app_owned_mcp_keys", lambda: {"weather": True})
    else:
        path = {
            "cli": tmp_path / "kiro.mcp.json",
            "template": tmp_path / "agents" / "other.json",
            "markdown": tmp_path / "agents" / "other.md",
            "mate": tmp_path / "mates" / "other.json",
            "invalid": tmp_path / "kiro.mcp.json",
        }[source]
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"name": "other", "mcpServers": {"weather": ENTRY}})
        if source == "markdown":
            text = "---\n" + text + "\n---\n"
        elif source == "invalid":
            text = "{"
        path.write_text(text, encoding="utf-8")
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        status, card = await _ask(
            c,
            {"action": "change", "server": "weather", "entry": {**ENTRY, "args": ["new"]}},
        )
        assert status == 200, card
        assert _offer(card) is None
        return await _apply(c, card)

    assert _run(store, env, go) == [200, 200]
    assert env["stubs"] == []
    assert env["enabled"] == []


@pytest.mark.parametrize(
    "rel",
    [
        "agents/kirocrew.json",
        "agents/kirocrew-worker.json",
        "agents/kirocrew-heartbeat.json",
        "mates/kirocrew-mate-worker.json",
    ],
)
def test_a_spec_crew_derives_from_the_store_is_not_another_declarer(env, tmp_path, rel):
    # The default spec copies every store server and the worker spec copies the
    # default's servers, so a copy there is the store entry itself, not a second one.
    _write_store(env, {"weather": dict(ENTRY)})
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": path.stem, "mcpServers": {"weather": ENTRY}}), "utf-8")
    store = CardStore(tmp_path / "c.json")
    new = {**ENTRY, "args": ["new"]}

    async def go(c):
        return (await _ask(c, {"action": "change", "server": "weather", "entry": new}))[1]

    assert _offer(_run(store, env, go)) == {
        "field": "share_offer",
        "label": "Sharing",
        "after": False,
    }


@pytest.mark.parametrize("reason", ["declarer", "remote"])
def test_a_shared_server_refused_for_another_reason_records_no_folder_use(env, tmp_path, reason):
    # Only a chat-folder signal may record ``uses_cwd``; another reason to stop
    # sharing says nothing about the folder.
    _write_store(env, {"weather": dict(ENTRY)})
    env["sharing"] = {"sharing_on": True, "shared": True, "cwd_use": "not_used"}
    new: dict[str, Any] = {**ENTRY, "args": ["new"]}
    if reason == "declarer":
        other = tmp_path / "agents" / "other.json"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text(json.dumps({"name": "other", "mcpServers": {"weather": ENTRY}}), "utf-8")
    else:
        new = {"url": "https://mcp.example.com/sse"}
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        return (await _ask(c, {"action": "change", "server": "weather", "entry": new}))[1]

    card = _run(store, env, go)
    assert _offer(card) is None
    stub_steps = [s for s in card["plan"]["apply"] if s["path"].endswith("/stub")]
    assert [s["body"] for s in stub_steps] == [{"name": "weather", "stub": False}]


def test_a_url_move_over_stored_headers_raises_no_card(env, tmp_path):
    # The PUT refuses it (``url_change_with_stored_headers``), so the card must not
    # be raised for the person to approve and then fail on Apply.
    stored = {"url": "https://a.example.com/mcp", "headers": {"Authorization": "Bearer s"}}
    _write_store(env, {"remote": stored})
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        return await _ask(
            c,
            {"action": "change", "server": "remote", "entry": {"url": "https://b.example.com/mcp"}},
        )

    status, out = _run(store, env, go)
    assert status == 400, out
    assert out["code"] == "invalid_param"
    assert "cannot change 'url'" in out["error"]
    assert store.pending(None) == []


def _resolved_entry(entry):
    from kiro_crew.mcp_gateway.launch_approval import (
        ResolvedLaunch,
        env_fingerprint,
        launch_fingerprint,
    )

    return ResolvedLaunch(
        launch_fingerprint(entry["command"], entry.get("args", []), entry.get("env", {})),
        entry["command"],
        tuple(entry.get("args", [])),
        frozenset({env_fingerprint(entry.get("env", {}))}),
        tuple(sorted(entry.get("env", {}).items())),
    )


@pytest.mark.parametrize("difference", ["extra_launch", "args", "env", "command", "none"])
def test_apply_approves_only_the_entry_on_the_card(env, tmp_path, monkeypatch, difference):
    import sys

    from kiro_crew.mcp_gateway import launch_resolve

    entry = {**ENTRY, "command": sys.executable}
    other = {**entry, "args": ["different"]}
    if difference == "env":
        other = {**entry, "env": {"CITY": "Rome"}}
    elif difference == "command":
        other = {**entry, "command": "another-program"}
    elif difference == "none":
        other = entry
    env["sharing"]["sharing_on"] = True
    env["real_stub"] = True
    # The preview token really describes the changed launch set, so the old
    # freshness-only check would approve it. The card's entry is the boundary.
    env["launches"] = [_resolved_entry(other)]
    if difference == "extra_launch":
        env["launches"].append(_resolved_entry(entry))
    monkeypatch.setattr(
        launch_resolve, "resolve_launches", lambda names: {"weather": env["launches"]}
    )
    monkeypatch.setattr(mcp_mod, "refused_servers", lambda: {})
    approved = []
    monkeypatch.setattr(mcp_mod, "approve", lambda launches: approved.append(launches))
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        status, card = await _ask(c, {**ADD, "entry": entry})
        assert status == 200, card
        assert _offer(card)["after"] is True
        statuses = await _apply(c, card)
        return statuses

    statuses = _run(store, env, go)
    assert _stored(env)["weather"] == entry
    if difference == "none":
        assert statuses == [200, 200, 200, 200]
        assert approved == [{"weather": env["launches"]}]
        assert env["restarts"] == 1
    else:
        assert statuses == [200, 200, 409]
        assert env["stub_response"]["code"] == "launch_differs_from_card"
        assert approved == []
        assert env["restarts"] == 0


def test_the_entry_guard_accepts_only_the_matching_complete_launch(monkeypatch):
    import sys

    from kiro_crew.mcp_gateway import launch_resolve, rewriter_launch

    monkeypatch.setattr(rewriter_launch, "_resolve_target_command", lambda *args: sys.executable)
    entry = {**ENTRY, "command": sys.executable}
    launch = _resolved_entry(entry)
    assert launch_resolve.matches_entry(entry, [launch])
    assert not launch_resolve.matches_entry(entry, [])
    assert not launch_resolve.matches_entry(entry, [launch, launch])
    assert not launch_resolve.matches_entry({**entry, "env": []}, [launch])


@pytest.mark.asyncio
async def test_cli_inherited_launch_cannot_be_approved_with_the_card_entry(toggle_home):
    import sys

    from kiro_crew.mcp_gateway import launch_resolve
    from kiro_crew.mcp_gateway.launch_approval import load_approvals

    entry = {"command": sys.executable, "args": ["shown"]}
    (toggle_home / "a.json").write_text(
        json.dumps(
            {"name": "a", "tools": ["@srv"], "mcpServers": {"srv": entry}, "includeMcpJson": False}
        ),
        encoding="utf-8",
    )
    (toggle_home / "b.json").write_text(
        json.dumps({"name": "b", "mcpServers": {}, "tools": ["@srv"]}), encoding="utf-8"
    )
    settings = toggle_home.parent / "settings" / "mcp.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps({"mcpServers": {"srv": {**entry, "args": ["not-shown"]}}}), encoding="utf-8"
    )
    launches = await asyncio.to_thread(launch_resolve.resolve_launches, ["srv"])
    assert {launch.args for launch in launches["srv"]} == {("shown",), ("not-shown",)}
    token = mcp_mod.display_launches("srv", launches["srv"])["expected_launch"]
    before = load_approvals().approved_pairs
    response = await mcp_mod.api_mcp_gateway_set_stub(
        _stub_request(
            {
                "name": "srv",
                "stub": True,
                "cwd_use": "not_used",
                "expected_launch": token,
                "expected_entry": entry,
            }
        )
    )
    assert response.status == 409
    assert json.loads(response.body)["code"] == "launch_differs_from_card"
    assert load_approvals().approved_pairs == before


@pytest.mark.parametrize("tamper", ["remove", "replace"])
def test_card_step_cannot_drop_or_replace_the_expected_entry(env, tmp_path, tamper):
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        status, card = await _ask(c, ADD)
        assert status == 200, card
        step = next(s for s in card["plan"]["apply"] if s["body"] and s["body"].get("stub"))
        fill = step["fill"][0]
        evidence = [{} for _ in range(fill["step"] + 1)]
        evidence[fill["step"]] = {"expected_launch": "fresh-token"}
        body = {**step["body"], "expected_launch": "fresh-token"}

        def match():
            return routes.match_step(
                step, method="POST", path=step["path"], query={}, body=body, evidence=evidence
            )

        assert match() is None
        if tamper == "remove":
            body.pop("expected_entry")
        else:
            body["expected_entry"] = {**ENTRY, "args": ["not-shown"]}
        assert match() == "body"

    _run(store, env, go)


def test_entry_guard_rejects_a_different_expansion_or_unresolved_command(monkeypatch):
    import sys
    from dataclasses import replace

    from kiro_crew.mcp_gateway import launch_resolve, rewriter_launch

    entry = {**ENTRY, "command": sys.executable}
    launch = _resolved_entry(entry)
    changed = replace(launch, derived_env=frozenset({"different-effective-env"}))
    assert not launch_resolve.matches_entry(entry, [changed])
    monkeypatch.setattr(rewriter_launch, "_resolve_target_command", lambda *args: "")
    assert not launch_resolve.matches_entry(entry, [launch])


@pytest.mark.parametrize("folder_use", [True, False])
def test_unsharing_restarts_open_chats_even_when_use_now_is_false(env, tmp_path, folder_use):
    _write_store(env, {"weather": dict(ENTRY)})
    env["sharing"] = {"sharing_on": True, "shared": True, "cwd_use": "not_used"}
    store = CardStore(tmp_path / "c.json")
    new = {**ENTRY, "args": ["./cache" if folder_use else "new"]}

    async def go(c):
        status, card = await _ask(c, {"action": "change", "server": "weather", "entry": new})
        assert status == 200, card
        card = await _repreview(c, card, share=False, use_now=False)
        return await _apply(c, card)

    assert _run(store, env, go) == [200, 200, 200]
    assert env["stubs"][0]["stub"] is False
    assert env["restarts"] == 1


@pytest.mark.parametrize("sharing_on", [True, False])
@pytest.mark.parametrize("action", ["add", "change"])
def test_sharing_with_use_now_false_builds_and_applies_without_a_restart(
    env, tmp_path, action, sharing_on
):
    # The share steps include a GET with no body; the restart check must read it.
    if action == "change":
        _write_store(env, {"weather": {**ENTRY, "args": ["old"]}})
    env["sharing"] = {
        "sharing_on": sharing_on,
        "shared": action == "change",
        "cwd_use": "not_used",
    }
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        status, card = await _ask(c, {**ADD, "action": action})
        assert status == 200, card
        card = await _repreview(c, card, share=True, use_now=False)
        return card, await _apply(c, card)

    card, statuses = _run(store, env, go)
    paths = [s["path"].split("?")[0] for s in card["plan"]["apply"]]
    assert "/api/mcp-gateway/servers/launch" in paths
    assert "/api/sessions/restart" not in paths
    assert all(st == 200 for st in statuses)
    assert env["stubs"][0]["stub"] is True
    assert env["restarts"] == 0


@pytest.mark.parametrize("action", ["add", "change"])
def test_whitespace_command_is_normalized_before_the_card_binds_its_launch(
    env, tmp_path, monkeypatch, action
):
    import sys

    from kiro_crew.mcp_gateway import launch_resolve, rewriter_launch

    entry = {**ENTRY, "command": " npx "}
    normalized = mcp_custom._clean_spec(entry)
    if action == "change":
        _write_store(env, {"weather": {**ENTRY, "args": ["old"]}})
    env["sharing"] = {"sharing_on": True, "shared": action == "change", "cwd_use": "not_used"}
    env["real_stub"] = True
    env["launches"] = [_resolved_entry({**normalized, "command": sys.executable})]
    monkeypatch.setattr(
        rewriter_launch,
        "_resolve_target_command",
        lambda command, *args: sys.executable if command == "npx" else None,
    )
    monkeypatch.setattr(
        launch_resolve, "resolve_launches", lambda names: {"weather": env["launches"]}
    )
    monkeypatch.setattr(mcp_mod, "refused_servers", lambda: {})
    approved = []
    monkeypatch.setattr(mcp_mod, "approve", lambda launches: approved.append(launches))
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        status, card = await _ask(c, {**ADD, "action": action, "entry": entry})
        assert status == 200, card
        statuses = await _apply(c, card)
        return card, statuses

    card, statuses = _run(store, env, go)
    assert statuses == [200, 200, 200, 200], env.get("stub_response")
    stub = next(s for s in card["plan"]["apply"] if s["path"].endswith("/stub"))
    assert stub["body"]["expected_entry"] == normalized
    assert _stored(env)["weather"] == normalized
    assert approved == [{"weather": env["launches"]}]
    assert env["restarts"] == 1


@pytest.mark.parametrize("use_now", [True, False])
def test_remove_unshares_a_gone_server_and_restarts_chats(env, tmp_path, monkeypatch, use_now):
    from kiro_crew.config import loader

    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"mcp_gateway": {"enabled": True, "stub_servers": ["weather"]}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "config_path", lambda: config)
    monkeypatch.setattr(mcp_mod, "_local_overlay_section", lambda: {})
    _write_store(env, {"weather": dict(ENTRY)})
    env["sharing"] = {"sharing_on": True, "shared": True, "cwd_use": "not_used"}
    env["real_stub"] = True
    unshared = []

    async def apply_stub(*, launch_approved):
        # The real unshare route runs after removal, without a resolvable entry.
        assert _stored(env) == {}
        unshared.append(loader.KiroCrewConfig.load().mcp_gateway.stub_servers)
        return {"applied": True, "restart_required": False}

    store = CardStore(tmp_path / "c.json")

    async def go(c):
        c.app["state"]._mcp_gateway_apply_stub = apply_stub
        status, card = await _ask(c, {"action": "remove", "server": "weather"})
        assert status == 200, card
        card = await _repreview(c, card, use_now=use_now)
        assert _offer(card) is None
        return await _apply(c, card)

    assert _run(store, env, go) == [200, 200, 200]
    assert len(unshared) == 1 and "weather" not in unshared[0]
    assert "weather" not in loader.KiroCrewConfig.load().mcp_gateway.stub_servers
    assert _stored(env) == {}
    assert env["restarts"] == 1


@pytest.mark.parametrize(
    "action, before, submitted",
    [
        ("add", None, {"command": " npx ", "args": [], "env": {}}),
        ("add", None, {"url": "https://x.test", "scopes": [], "headers": {}}),
        (
            "change",
            {
                "url": "https://x.test",
                "oauthScopes": ["read"],
                "oauth": {"clientId": "old", "oauthScopes": ["read"], "issuer": "issuer"},
                "disabledTools": ["delete"],
            },
            {"url": "https://x.test", "scopes": ["write"], "clientId": "new"},
        ),
        (
            "change",
            {"url": "https://x.test", "oauthScopes": ["read"], "oauth": {"clientId": "old"}},
            {"url": "https://x.test"},
        ),
        (
            "change",
            {"url": "https://x.test", "oauthScopes": ["read"], "oauth": {"clientId": "old"}},
            {"url": "https://x.test", "oauthScopes": ["write"], "oauth": {"clientId": "new"}},
        ),
    ],
    ids=[
        "add-stdio",
        "add-remote",
        "change-internal-hints",
        "change-clear-hints",
        "change-wire-hints",
    ],
)
def test_card_rows_match_exactly_the_entry_apply_stores(env, tmp_path, action, before, submitted):
    from kiro_crew.change_card_mcp_server import entry_rows

    if before is not None:
        _write_store(env, {"weather": before})
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        status, card = await _ask(
            c,
            {
                "action": action,
                "source": "custom",
                "server": "weather",
                "entry": submitted,
                "uses_chat_folder": True,
            },
        )
        assert status == 200, card
        assert await _apply(c, card) == [200, 200]
        return card

    card = _run(store, env, go)
    assert card["changes"] == entry_rows(before, _stored(env)["weather"])


@pytest.mark.parametrize("disabled", [True, False])
def test_add_refuses_disabled_instead_of_previewing_an_ignored_flag(env, tmp_path, disabled):
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        return await _ask(c, {**ADD, "entry": {**ENTRY, "disabled": disabled}})

    status, out = _run(store, env, go)
    assert status == 400, out
    assert out["code"] == "invalid_param"
    assert "disabled" in out["error"]
    assert store.pending(None) == []
    assert not env["path"].exists()
