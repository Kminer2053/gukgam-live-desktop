"""악성 문자열로부터 메뉴 링크와 모듈 경로를 보호하는 순수 렌더 시험."""
from __future__ import annotations

import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, quote, urlsplit

import pytest
from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOTS = (
    ROOT / "gukgam" / "web" / "templates",
    ROOT / "cloud" / "gukgam" / "web" / "templates",
)
DESTINATIONS = ("/", "/sessions/new", "/items", "/search", "/admin")
MODULE_PATHS = (
    "/static/js/common.js",
    "/static/js/closed-block-view.js",
    "/static/js/report-view.js",
    "/static/js/audio-capture.js",
)
NODE = shutil.which("node")


class ParsedHTML(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.scripts: list[tuple[dict[str, str | None], list[str]]] = []
        self._script_data: list[str] | None = None
        self.feed(html)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.tags.append((tag, attributes))
        if tag == "script":
            self._script_data = []
            self.scripts.append((attributes, self._script_data))

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._script_data = None

    def handle_data(self, data: str) -> None:
        if self._script_data is not None:
            self._script_data.append(data)


@pytest.fixture(params=TEMPLATE_ROOTS, ids=("local", "cloud-copy"))
def env(request: pytest.FixtureRequest) -> Environment:
    return Environment(loader=FileSystemLoader(request.param), autoescape=True)


def _render_base(env: Environment, version: str, *, cloud: bool = False, page_module: bool = False) -> str:
    template = env.from_string(
        '{% extends "base.html" %}{% block scripts %}'
        '<script type="module" src="/static/js/dashboard.js"></script>{% endblock %}'
    ) if page_module else env.get_template("base.html")
    return template.render(
        request=SimpleNamespace(url=SimpleNamespace(path="/")),
        me={"role": "operator", "name": ""},
        auth_enabled=True,
        desktop_local=True,
        version=version,
        cloud=cloud,
    )


@pytest.mark.parametrize("href", DESTINATIONS)
def test_allowed_navigation_preserves_destination_and_current_page(env: Environment, href: str) -> None:
    nav_link = env.get_template("_macros.html").module.nav_link
    for path in (href, href.rstrip("/") + "/detail", "/unrelated"):
        parsed = ParsedHTML(str(nav_link(href, "메뉴", "home", path)))
        anchors = [attrs for tag, attrs in parsed.tags if tag == "a"]
        assert len(anchors) == 1
        assert anchors[0]["href"] == href
        active = path == href or (href != "/" and path.startswith(href))
        assert anchors[0].get("aria-current") == ("page" if active else None)
        icons = [attrs for tag, attrs in parsed.tags if tag == "svg"]
        assert len(icons) == 1
        assert icons[0]["aria-hidden"] == "true"
        assert icons[0]["focusable"] == "false"
        assert "<span>메뉴</span>" in str(nav_link(href, "메뉴", "home", path))


@pytest.mark.parametrize("href", (
    "javascript:alert(1)",
    "//outside.invalid/path",
    "/\\outside.invalid/path",
    '/admin" onclick="alert(1)',
), ids=("javascript", "protocol-relative", "backslash", "attribute-injection"))
def test_unlisted_navigation_falls_back_to_single_origin_root(env: Environment, href: str) -> None:
    nav_link = env.get_template("_macros.html").module.nav_link
    parsed = ParsedHTML(str(nav_link(href, "메뉴", "home", "/")))
    anchors = [attrs for tag, attrs in parsed.tags if tag == "a"]
    assert anchors == [{"href": "/", "aria-current": "page"}]
    assert not any(name.lower().startswith("on") for _, attrs in parsed.tags for name in attrs)


@pytest.mark.parametrize("version", (
    "0.1.0",
    "",
    '</script><script id="injected">alert(1)</script><!--',
    '한글&tag="quoted"\\path\nnext',
), ids=("release", "empty", "script-closing-injection", "json-escaping"))
def test_import_map_round_trips_versions_without_script_injection(env: Environment, version: str) -> None:
    html = _render_base(env, version)
    parsed = ParsedHTML(html)
    assert len(parsed.scripts) == 1
    attributes, chunks = parsed.scripts[0]
    assert set(attributes) == {"src", "data-version"}
    assert attributes["data-version"] == version
    bootstrap_url = urlsplit(attributes["src"])
    assert not bootstrap_url.scheme and not bootstrap_url.netloc and not bootstrap_url.fragment
    assert bootstrap_url.path == "/static/js/module-importmap.js"
    assert parse_qsl(bootstrap_url.query, keep_blank_values=True) == [("v", version)]
    assert chunks == []
    assert html.lower().count("</script>") == 1
    assert not any(attrs.get("id") == "injected" for _, attrs in parsed.tags)

    assert NODE is not None, "The existing Node runtime is required for the bootstrap execution test."
    template_root = Path(env.loader.searchpath[0])
    static_root = (ROOT / "gukgam" / "web" / "static" if template_root == TEMPLATE_ROOTS[0]
                   else ROOT / "cloud" / "public" / "static")
    source_path = static_root / "js" / "module-importmap.js"
    result = subprocess.run(
        [NODE, "-e", r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const events = [];
const inserted = [];
const bootstrap = {
  dataset: {version: input.version},
  after(element) { inserted.push(element); events.push('import-map-inserted'); },
};
const document = {
  currentScript: bootstrap,
  createElement(tag) {
    assert.equal(tag, 'script');
    return {type: '', textContent: ''};
  },
};
vm.runInNewContext(input.source, {document}, {timeout: 1000});
events.push('next-module-parsed');
assert.equal(inserted.length, 1);
assert.deepEqual(events, ['import-map-inserted', 'next-module-parsed']);
assert.deepEqual(Object.keys(inserted[0]).sort(), ['textContent', 'type']);
assert.equal(inserted[0].type, 'importmap');
process.stdout.write(JSON.stringify(inserted[0]));
"""],
        input=json.dumps({"source": source_path.read_text(encoding="utf-8"), "version": attributes["data-version"]}),
        capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    assert result.returncode == 0, result.stderr
    inserted = json.loads(result.stdout)
    script = inserted["textContent"]
    encoded = quote(version, safe="-_.!~*'()")
    imports = json.loads(script)["imports"]
    assert imports == {path: path + "?v=" + encoded for path in MODULE_PATHS}
    for path, destination in imports.items():
        url = urlsplit(destination)
        assert not url.scheme and not url.netloc and not url.fragment
        assert url.path == path
        assert parse_qsl(url.query, keep_blank_values=True) == [("v", version)]
    assert "</script" not in script.lower()


def test_blocking_bootstrap_precedes_page_modules(env: Environment) -> None:
    parsed = ParsedHTML(_render_base(env, "0.1.0", page_module=True))
    assert len(parsed.scripts) == 2
    bootstrap, module = (attributes for attributes, _ in parsed.scripts)
    assert bootstrap["src"] == "/static/js/module-importmap.js?v=0.1.0"
    assert not {"async", "defer", "type"} & bootstrap.keys()
    assert module == {"type": "module", "src": "/static/js/dashboard.js"}


def test_cloud_pages_keep_import_maps_absent(env: Environment) -> None:
    html = _render_base(env, '</script><script id="injected">alert(1)</script>', cloud=True)
    parsed = ParsedHTML(html)
    assert parsed.scripts == []
    assert not any(attrs.get("id") == "injected" for _, attrs in parsed.tags)
