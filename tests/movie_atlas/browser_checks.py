from __future__ import annotations

import re
import threading

import pytest
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from conftest import CLEAR, PAUSED


@pytest.fixture
def site(atlas_fixture, monkeypatch):
    app = atlas_fixture.app
    playback_requests = []

    def forbidden():
        playback_requests.append(True)
        return {"error": "Playback is isolated in this test"}, 409

    for rule in app.url_map.iter_rules():
        if "POST" in rule.methods and rule.rule.startswith(("/api/play", "/api/mode", "/api/media/", "/api/share")):
            monkeypatch.setitem(app.view_functions, rule.endpoint, forbidden)

    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", playback_requests
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def pair_controller(phone):
    phone.locator("#create").click()
    expect(phone.locator("#pairCode")).to_have_text(re.compile(r"^[A-Z2-9]{6}$"))
    return phone.locator("#pairCode").inner_text()


def pair_display(tv, origin, code):
    tv.goto(origin + "/movie-atlas/display")
    tv.locator("#code").fill(code)
    tv.get_by_role("button", name="Під’єднати екран").click()
    expect(tv.locator("#atlas")).to_be_visible()


def test_phone_and_tv_sync_and_selector_handoff(atlas_fixture, site):
    f = atlas_fixture
    origin, playback = site
    prior = f.seed("previous", "a", "Synthetic Zulu", 2014, ["Drama"])
    f.seed("chosen", "b", "Synthetic Alpha", 2024, ["Drama"])
    f.seed("third", "c", "Synthetic Comedy", 2024, ["Comedy"])
    f.seed("hidden", "d", "Hidden Fixture", 2025, ["Drama"], hidden=True)
    f.seed("deleted", "e", "Deleted Fixture", 2025, ["Drama"], deleted=True)
    f.write_history()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        phone_context = browser.new_context(viewport={"width": 430, "height": 930}, has_touch=True)
        tv_context = browser.new_context(viewport={"width": 1440, "height": 900})
        phone, tv = phone_context.new_page(), tv_context.new_page()
        page_errors = []
        for page in (phone, tv):
            page.on("pageerror", lambda error: page_errors.append(str(error)))

        phone.goto(origin + "/video?job_id=" + prior)
        expect(phone.locator("#catalogTitle")).to_have_text("Synthetic Zulu")
        phone.get_by_role("link", name="Кіноатлас").click()
        f.ownership = PAUSED
        phone.locator("#create").click()
        expect(phone.get_by_role("alert")).to_contain_text("Екран зайнятий")
        f.ownership = {"state": "UNKNOWN"}
        with phone.expect_response(lambda response: response.url.endswith("/api/movie-atlas/sessions")) as denied:
            phone.locator("#create").click()
        assert denied.value.status == 409
        f.ownership = CLEAR
        code = pair_controller(phone)
        pair_display(tv, origin, code)
        expect(tv.locator(".poster")).to_have_count(3)
        expect(tv.locator("button.poster")).to_have_count(0)
        expect(tv.locator("#tools")).to_be_hidden()

        phone.locator("#sort").select_option("title")
        expect(tv.locator(".poster").first).to_contain_text("Synthetic Alpha")
        phone.locator("#genre").select_option("Drama")
        expect(tv.locator(".poster")).to_have_count(2)
        phone.locator("#yearMin").select_option("2024")
        phone.locator("#yearMax").select_option("2024")
        expect(tv.locator(".poster")).to_have_count(1)
        phone.get_by_role("button", name="Synthetic Alpha", exact=False).click()
        expect(tv.locator("#selectedTitle")).to_have_text("Synthetic Alpha")
        expect(tv.locator("#selectedMeta")).to_have_text("2024 · Drama")
        phone.locator("#clearSelection").click()
        expect(tv.locator("#selectedTitle")).to_have_text("Фільм не вибрано")

        viewport = phone.locator("#viewport")
        viewport.scroll_into_view_if_needed()
        box = viewport.bounding_box()
        assert box is not None
        x, y = box["x"] + 70, box["y"] + 20
        phone.mouse.move(x, y)
        phone.mouse.down()
        phone.mouse.move(x + 65, y + 17, steps=6)
        phone.mouse.up()
        phone.locator("#zoomIn").click()
        expect(tv.locator("#posters")).to_have_attribute("style", re.compile(r"--yaw: (?!0deg).*--pitch: (?!0deg).*--zoom: 1\.15"))
        assert tv.locator("#posters").evaluate("(el) => getComputedStyle(el).transform").startswith("matrix3d(")
        tv.emulate_media(reduced_motion="reduce")
        expect(tv.locator("#posters")).to_have_css("transform", "none")
        tv.emulate_media(reduced_motion="no-preference")

        # Force a real two-controller revision conflict using the same role-local
        # capability from this tab. The UI must surface it and retry final input.
        path = origin + "/api/movie-atlas/sessions/" + code
        revision_race_started = False

        def advance_revision(route):
            nonlocal revision_race_started
            should_continue = False
            try:
                request = route.request
                if (
                    revision_race_started
                    or request.method != "PUT"
                    or request.post_data_json["state"].get("query") != "Com"
                ):
                    should_continue = True
                    return

                revision_race_started = True
                headers = {
                    "Origin": origin,
                    "Sec-Fetch-Site": "same-origin",
                    "X-Atlas-Token": request.headers["x-atlas-token"],
                }
                for _ in range(3):
                    snapshot = phone_context.request.get(path, params={"role": "controller"}, headers=headers)
                    assert snapshot.status == 200, snapshot.text()
                    response = phone_context.request.put(
                        path,
                        headers=headers,
                        data={"revision": snapshot.json()["revision"], "state": {"sort": "year"}},
                    )
                    if response.status != 409 or response.json().get("code") != "revision_conflict":
                        break
                assert response.status == 200, response.text()
                assert response.json()["revision"] > request.post_data_json["revision"]
                should_continue = True
            finally:
                if should_continue:
                    route.continue_()
                else:
                    route.abort()

        phone.route(path, advance_revision)
        with phone.expect_response(
            lambda response: response.url == path
            and response.request.method == "PUT"
            and response.request.post_data_json["state"].get("query") == "Com"
            and response.status == 409
        ) as conflict:
            phone.locator("#query").fill("Com")
        assert conflict.value.json()["code"] == "revision_conflict"
        expect(phone.get_by_role("alert")).to_contain_text("Стан змінився")
        phone.unroute(path, advance_revision)
        expect(tv.locator("#empty")).to_be_visible()
        phone.locator("#query").fill("Alpha")
        expect(tv.locator(".poster")).to_have_count(1)

        tv_context.set_offline(True)
        expect(tv.get_by_role("alert")).to_be_visible(timeout=12000)
        expect(tv.locator("#viewport")).to_be_hidden()
        phone.get_by_role("button", name="Synthetic Alpha", exact=False).click()
        phone.reload()
        expect(phone.locator("#selectedTitle")).to_have_text("Synthetic Alpha")
        tv_context.set_offline(False)
        tv.reload()
        expect(tv.locator("#selectedTitle")).to_have_text("Synthetic Alpha")
        expect(phone.locator("#query")).to_have_value("Alpha")

        f.ownership = PAUSED
        expect(tv.get_by_role("alert")).to_contain_text("Екран зайнятий")
        expect(tv.locator("#viewport")).to_be_hidden()
        f.ownership = CLEAR
        expect(tv.locator("#selectedTitle")).to_be_visible()
        phone.locator("#back").click()
        expect(phone).to_have_url(origin + "/video")
        expect(phone.locator("#catalogTitle")).to_have_text("Synthetic Zulu")
        expect(tv.get_by_role("alert")).to_contain_text("Сесію завершено")

        phone.get_by_role("link", name="Кіноатлас").click()
        code = pair_controller(phone)
        pair_display(tv, origin, code)
        phone.locator(".poster").filter(has_text="Synthetic Alpha").click()
        expect(tv.locator("#selectedTitle")).to_have_text("Synthetic Alpha")
        phone.locator("#choose").click()
        expect(phone).to_have_url(re.compile(r"/video\?atlas_history=[0-9a-f]{64}$"))
        expect(phone.locator("#catalogTitle")).to_have_text("Synthetic Alpha")
        expect(phone.locator("#openButton")).to_be_enabled()
        expect(tv.get_by_role("alert")).to_contain_text("Сесію завершено")
        assert not page_errors
        assert not playback
        browser.close()


def test_empty_catalog_flat_view_and_explicit_exit(atlas_fixture, site):
    origin, playback = site
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        phone = browser.new_page(reduced_motion="reduce")
        tv = browser.new_page()
        phone.goto(origin + "/movie-atlas")
        code = pair_controller(phone)
        pair_display(tv, origin, code)
        expect(phone.locator("#empty")).to_have_text("Каталог порожній.")
        expect(tv.locator("#empty")).to_have_text("Каталог порожній.")
        expect(phone.locator("#cameraTools")).to_be_hidden()
        expect(phone.locator("#choose")).to_be_disabled()
        tv.locator("#flat").click()
        expect(tv.locator("#posters")).to_have_css("transform", "none")
        assert not tv.locator("canvas").count()
        tv.locator("#exit").click()
        expect(tv.locator("#join")).to_be_visible()
        pair_display(tv, origin, code)
        phone.locator("#exit").click()
        expect(phone).to_have_url(origin + "/video")
        expect(tv.get_by_role("alert")).to_contain_text("Сесію завершено")
        assert not playback
        browser.close()
