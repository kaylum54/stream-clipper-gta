"""Browser journeys against the real app (loops off, live checks faked, Post for Me + Discord mocked)."""
import json
import re
import sqlite3
import sys
import time

import pytest
from playwright.sync_api import expect

from clipper import db
from conftest import start_server  # e2e/conftest.py, already loaded by pytest
from e2e.seed import curves, make_clip

# any visible box past the screen edge, outside strips that scroll sideways on purpose
OUTSIDE_JS = r"""() => {
  const W = innerWidth, out = [];
  const scrolls = (el) => { for (let p = el.parentElement; p; p = p.parentElement) { const s = getComputedStyle(p); if (/(auto|scroll)/.test(s.overflowX) && p.scrollWidth > p.clientWidth + 1) return true; } return false; };
  for (const el of document.querySelectorAll('body *')) {
    if (el.closest('[hidden], dialog:not([open]), .skip, .sr-only, .playhead')) continue;
    const r = el.getBoundingClientRect();
    if (r.width && r.height && (r.right > W + 0.5 || r.left < -0.5) && !scrolls(el)) out.push(`${el.tagName}.${el.className} ${Math.round(r.left)}..${Math.round(r.right)}`);
  }
  return out;
}"""


def no_hscroll(page):
    return page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def post(page, path):
    """A change through the page's own origin, with the header the server insists on."""
    return page.evaluate("p => fetch(p, { method: 'POST', headers: { 'X-Clipper': '1' } }).then(r => r.status)", path)


@pytest.fixture(scope="module")
def bare(tmp_path_factory, mock):
    """A new user's machine: nothing set up, and ffmpeg and streamlink missing."""
    d = tmp_path_factory.mktemp("bare")
    (d / "config.json").write_text(json.dumps({"ffmpeg": str(d / "missing" / "ffmpeg.exe"), "streamlink": str(d / "missing" / "streamlink.exe")}))
    proc, base = start_server(d, mock.base_url)
    yield base
    proc.terminate()
    proc.wait(10)


HOSTILE = '<img src=x onerror="window.__xss=1">'


@pytest.fixture(scope="module")
def big(tmp_path_factory, mock):
    """600 clips waiting and 121 streamers (3 recording), with stream text that tries to be HTML."""
    d = tmp_path_factory.mktemp("big")
    (d / "clips").mkdir()
    clip = d / "clips" / "big.mp4"
    make_clip(clip, 65, 440)
    conn = db.connect(d / "clipper.db")
    now = time.time()
    ids = [conn.execute("INSERT INTO streamers(platform, channel, url, checked_at, created_at) VALUES ('twitch',?,?,?,?)",
                        (f"streamer{i:03}", f"https://www.twitch.tv/streamer{i:03}", now - 30, now)).lastrowid for i in range(120)]
    bad = conn.execute("INSERT INTO streamers(platform, channel, url, live, title, checked_at, created_at) VALUES ('twitch',?,?,1,?,?,?)",
                       (HOSTILE, "javascript:window.__xss=2", "<b onclick=x>title</b>", now - 30, now)).lastrowid
    audio, chat = curves(65, 1, True)
    sig = json.dumps({"audio": 3.0, "chat": 1.0, "hype": 0.5, "laugh": 0.0, "peak_offset": 40, "curve": {"audio_db": audio, "chat": chat, "hype": chat}})
    rows = [(ids[i % 120], f"streamer{i % 120:03}", round(2 + (i * 7919 % 600) / 100, 2), now - 60 * i, None) for i in range(599)]
    rows.append((bad, HOSTILE, 99.0, now, "<script>window.__xss=3</script> said <b>this</b>"))  # the best, so it opens first
    conn.executemany("INSERT INTO clips(streamer_id, streamer_name, platform, peak_ts, start_ts, end_ts, score, signals, path, thumb, transcript, caption, status, created_at) "
                     "VALUES (?,?,'twitch',0,0,65,?,?,?,?,?,'cap','pending',?)",
                     [(sid, name, score, sig, str(clip), str(clip.with_suffix(".jpg")), said, at) for sid, name, score, at, said in rows])
    done = conn.execute("INSERT INTO clips(streamer_id, streamer_name, platform, peak_ts, start_ts, end_ts, score, signals, path, thumb, caption, status, created_at, decided_at) "
                        "VALUES (?,?,'twitch',0,0,65,5,?,?,?,'cap','posted',?,?)", (bad, HOSTILE, sig, str(clip), str(clip.with_suffix(".jpg")), now, now)).lastrowid
    conn.execute("INSERT INTO posts(clip_id, accounts, status, results, created_at, updated_at) VALUES (?,?,'done',?,?,?)",
                 (done, '["spc_tt1"]', json.dumps([{"account_id": "spc_tt1", "platform": "tiktok", "username": HOSTILE, "success": True, "error": None,
                                                     "url": "javascript:window.__xss=4"}]), now, now))
    conn.close()
    (d / "config.json").write_text(json.dumps({"postforme_api_key": "test-key"}))
    proc, base = start_server(d, mock.base_url)
    # the app ends recordings left over from before it started, so these go in after it is up
    with sqlite3.connect(d / "clipper.db", timeout=30) as c:
        for sid in ids[:3]:
            c.execute("INSERT INTO sessions(streamer_id, started_at, dir, status) VALUES (?,?,?, 'recording')", (sid, now - 600, str(d / "rec" / str(sid))))
    yield base
    proc.terminate()
    proc.wait(10)


def test_first_run_explains_and_adds_a_streamer(page, fresh):
    # bug caught: an empty screen on first run, or the add form swallowing bad links silently
    page.goto(fresh)
    expect(page.get_by_role("heading", name="Set up Stream Clipper")).to_be_visible()  # a new user lands on the checklist
    page.get_by_role("link", name="Add a streamer").click()
    url = page.get_by_placeholder("Paste a Twitch, Kick or YouTube channel link")
    expect(url).to_be_focused()
    url.fill("https://example.com/nope")
    page.get_by_role("button", name="Add", exact=True).click()
    expect(page.locator("#add-msg")).to_have_class(re.compile("err"))
    expect(url).to_have_attribute("aria-invalid", "true")
    url.fill("twitch.tv/SomeStreamer")  # the help text says a link without https works
    page.get_by_role("button", name="Add", exact=True).click()
    expect(page.locator("#add-msg")).to_contain_text("Added somestreamer")
    expect(page.locator("#streamer-list .srow")).to_have_count(1)
    expect(page.locator("#streamer-list .srow")).to_contain_text("Offline")
    page.goto(fresh + "/#review")  # nothing to review yet: it says why, and what happens next
    expect(page.locator("#stage")).to_contain_text("isn't live right now")
    expect(page.locator("#stage")).to_contain_text("checks again every 2 min")
    assert not page.errors, page.errors


def test_setup_flow_on_a_fresh_data_dir(page, bare):
    # bug caught: a new user never told that ffmpeg is missing, or a setup step that links nowhere
    page.goto(bare)
    expect(page.get_by_role("heading", name="Set up Stream Clipper")).to_be_visible()
    steps = page.locator("#steps")
    expect(steps).to_contain_text("ffmpeg not found")
    expect(steps).to_contain_text("streamlink not found")
    if sys.platform == "win32":
        expect(steps).to_contain_text("winget install Gyan.FFmpeg")
    expect(steps).to_contain_text("GB free")
    expect(page.locator("#step-system .step-state")).to_have_text("Missing")
    expect(page.locator("#setup-progress")).to_contain_text("0 of 5")
    # each step links to where it's done, and lands in the field
    page.get_by_role("link", name="Connect Discord").click()
    expect(page.locator("#f-discord_webhook_url")).to_be_focused()
    page.go_back()
    page.get_by_role("link", name="Connect Post for Me").click()
    expect(page.locator("#f-postforme_api_key")).to_be_focused()
    page.go_back()
    page.get_by_role("link", name="Add a streamer").click()
    expect(page.locator("#add-url")).to_be_focused()
    page.locator("#add-url").fill("https://kick.com/someone")
    page.get_by_role("button", name="Add", exact=True).click()
    expect(page.locator("#add-msg")).to_contain_text("Added someone")
    # the checklist ticks the step off and stays one click away
    page.get_by_role("link", name="Setup 1 of 5").click()
    expect(page.locator("#step-streamer .step-state")).to_have_text("Done")
    expect(page.locator("#setup-progress")).to_contain_text("1 of 5")
    page.goto(bare)  # tools still missing: a plain visit opens the checklist, not an empty review
    expect(page.get_by_role("heading", name="Set up Stream Clipper")).to_be_visible()
    assert not page.errors, page.errors


def test_review_queue_plays_and_approves(page, seeded, mock):
    # bug caught: the main job (watch, caption, approve) breaking: no video, wrong order, or nothing sent
    page.goto(seeded)
    items = page.locator("#queue li.q")
    expect(items).to_have_count(3)
    expect(items.first).to_contain_text("7.4")  # highest score first
    video = page.locator("#stage video")
    expect(video).to_have_attribute("src", re.compile(r"/media/clips/demo-0\.mp4"))
    page.wait_for_function("document.querySelector('#stage video').readyState >= 1")
    expect(page.locator(".strip svg polyline")).to_have_count(1)
    expect(page.locator(".strip .s-peak")).to_have_count(1)
    box = page.locator(".strip .graph").bounding_box()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)  # the strip seeks
    t = page.evaluate("document.querySelector('#stage video').currentTime")
    assert 25 < t < 40, t
    expect(page.locator("#panel")).to_contain_text("Ele caiu do prédio")
    expect(page.locator("#panel .why")).to_contain_text("Loudness")
    expect(page.locator("#panel .acct")).to_have_count(2)  # accounts from (mock) Post for Me
    page.keyboard.press("j")
    expect(page.locator("#queue button[aria-current='true']")).to_contain_text("5.1")
    page.keyboard.press("k")
    page.locator("#panel .acct input").nth(1).uncheck()
    page.reload()  # your ticks for this clip survive a refresh
    expect(page.locator("#panel .acct")).to_have_count(2)
    expect(page.locator("#panel .acct input").nth(1)).not_to_be_checked()
    page.locator("#caption").fill("he fell off the building #gta6")
    page.get_by_role("button", name="Approve and post").click()
    expect(items).to_have_count(2)
    expect(page.locator("#panel .note")).to_contain_text("Sent")
    posted = []
    for _ in range(50):  # the upload runs in the background after approve returns (lookup, upload, then create)
        posted = [r for r in mock.requests if r[0] == "POST" and r[1] == "/v1/social-posts"]
        if posted:
            break
        page.wait_for_timeout(100)
    assert posted and posted[-1][3]["caption"] == "he fell off the building #gta6"
    assert posted[-1][3]["social_accounts"] == ["spc_tt1"]  # only the account left ticked
    page.goto(seeded + "/#posted")
    expect(page.locator("#posted-list")).to_contain_text("Daily upload limit reached")  # seeded failed result shown with its reason
    expect(page.locator("#posted-list")).to_contain_text("he fell off the building")
    assert not page.errors, page.errors


def test_reject_with_keyboard_undo_and_restore(page, seeded):
    # bug caught: R doing nothing, a held key rejecting a run of clips, or a rejected clip lost for good
    page.goto(seeded)
    expect(page.locator("#queue li.q").first).to_be_visible()
    before = page.locator("#queue li.q").count()
    name = page.locator("#queue button[aria-current='true'] .q-name").inner_text()
    page.locator("#stage").click(position={"x": 5, "y": 5})
    page.keyboard.press("r")
    expect(page.locator("#queue li.q")).to_have_count(before - 1)
    page.get_by_role("button", name="Undo").click()
    expect(page.locator("#queue li.q")).to_have_count(before)
    expect(page.locator("#queue button[aria-current='true'] .q-name")).to_have_text(name)
    page.locator("#stage").click(position={"x": 5, "y": 5})
    page.keyboard.press("r")
    expect(page.locator("#queue li.q")).to_have_count(before - 1)
    # holding R sends repeats once the first reject is done: they must not reject the clip that slid into place
    page.evaluate("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'r', repeat: true, bubbles: true }))")
    page.wait_for_timeout(600)
    expect(page.locator("#queue li.q")).to_have_count(before - 1)
    page.goto(seeded + "/#posted")
    page.get_by_role("tab", name="Rejected").click()
    expect(page.locator("#posted-list")).to_contain_text(name)
    page.get_by_role("button", name="Back to queue").first.click()
    page.goto(seeded + "/#review")
    expect(page.locator("#queue li.q")).to_have_count(before)
    assert not page.errors, page.errors


def test_settings_save_masks_secret_and_tests_discord(page, seeded, mock):
    # bug caught: a saved key echoed back into the page, or the Discord test going nowhere
    hook = f"{mock.base_url}/api/webhooks/1/abc"
    page.goto(seeded + "/#settings")
    page.locator("#f-discord_webhook_url").fill(hook)
    page.get_by_role("button", name="Save settings").click()
    expect(page.locator("#save-status")).to_contain_text("Saved")
    expect(page.locator("#f-discord_webhook_url")).to_have_attribute("placeholder", re.compile("Saved"))
    assert hook not in page.content()
    page.get_by_role("button", name="Send a test message").click()
    expect(page.locator("#set-discord")).to_contain_text("Check your Discord channel for the message")
    assert any(r[1].startswith("/api/webhooks/1/abc") for r in mock.requests)
    page.get_by_role("button", name="Test connection").click()
    expect(page.locator("#set-postforme")).to_contain_text("2 connected account(s)")
    # a key pasted and tested without pressing Save is saved first, then tested, and still never shown back
    hook2 = f"{mock.base_url}/api/webhooks/2/def"
    page.locator("#f-discord_webhook_url").fill(hook2)
    page.get_by_role("button", name="Send a test message").click()
    expect(page.locator("#set-discord")).to_contain_text("Check your Discord channel for the message")
    assert any(r[1].startswith("/api/webhooks/2/def") for r in mock.requests)
    expect(page.locator("#f-discord_webhook_url")).to_have_value("")
    assert hook2 not in page.content() and hook2 not in page.evaluate("fetch('/api/settings').then(r => r.text())")
    assert not page.errors, page.errors


def test_settings_check_ranges_before_saving(page, seeded):
    # bug caught: an out-of-range number sent to the server, or an error nobody can find
    page.goto(seeded + "/#settings")
    field = page.locator("#f-poll_seconds")
    field.fill("5")
    page.get_by_role("button", name="Save settings").click()
    expect(page.locator("#f-poll_seconds-err")).to_contain_text("30 to 3600")
    expect(field).to_have_attribute("aria-invalid", "true")
    expect(field).to_be_focused()
    expect(page.locator("#save-status")).to_contain_text("needs fixing")
    assert page.evaluate("fetch('/api/settings').then(r => r.json()).then(s => s.poll_seconds)") == 120
    page.locator("#f-preroll").fill("30")  # the drawing of the clip follows the numbers
    expect(page.locator("#set-clipping")).to_contain_text("Each clip is 55 seconds long")
    field.fill("90")
    page.get_by_role("button", name="Save settings").click()
    expect(page.locator("#save-status")).to_contain_text("Saved")
    saved = page.evaluate("fetch('/api/settings').then(r => r.json())")
    assert saved["poll_seconds"] == 90 and saved["preroll"] == 30
    page.evaluate("fetch('/api/settings', { method: 'PUT', headers: { 'X-Clipper': '1', 'Content-Type': 'application/json' }, body: JSON.stringify({ poll_seconds: 120, preroll: 40 }) })")
    assert not page.errors, page.errors


def test_theme_toggle_persists(page, seeded, browser):
    # bug caught: the theme choice forgotten on reload, or a blocked localStorage breaking the page
    page.goto(seeded + "/#streamers")
    html = page.locator("html")
    bg = "getComputedStyle(document.body).backgroundColor"
    light = page.evaluate(bg)
    page.get_by_role("button", name="Switch to dark theme").click()
    expect(html).to_have_attribute("data-theme", "dark")
    dark = page.evaluate(bg)
    assert dark != light
    page.reload()
    expect(html).to_have_attribute("data-theme", "dark")
    assert page.evaluate(bg) == dark
    page.get_by_role("button", name="Switch to light theme").click()
    expect(html).not_to_have_attribute("data-theme", "dark")
    page.reload()
    assert page.evaluate(bg) == light
    assert not page.errors, page.errors
    ctx = browser.new_context(color_scheme="dark")
    ctx.add_init_script("Object.defineProperty(window, 'localStorage', { get() { throw new DOMException('blocked', 'SecurityError'); } })")
    p2 = ctx.new_page()
    errors = []
    p2.on("pageerror", lambda e: errors.append(str(e)))
    p2.goto(seeded + "/#review")
    expect(p2.locator("#queue li.q").first).to_be_visible()
    p2.get_by_role("button", name="Switch to light theme").click()
    expect(p2.locator("html")).to_have_attribute("data-theme", "light")
    assert not errors, errors
    ctx.close()


def test_600_clip_queue_stays_responsive(page, big):
    # bug caught: 500 rows of video thumbnails built at once, or a poll rebuilding the list under your pointer
    page.goto(big)
    items = page.locator("#queue li.q")
    expect(items.first).to_be_visible()
    expect(page.locator("#queue-count")).to_contain_text("600")
    assert items.count() == 50  # the visible part, not all 500 the API sends
    renders = page.evaluate("performance.getEntriesByName('queue-render').map(e => [e.startTime, e.duration])")
    print("queue renders (start, ms):", renders)
    assert max(d for _, d in renders) < 250, renders
    assert renders[-1][0] + renders[-1][1] < 5000, renders  # from navigation to a full queue
    expect(page.locator("#queue-foot")).to_contain_text("500 of 600")
    ms = page.evaluate("""async () => {
      const t0 = performance.now();
      for (let i = 0; i < 20; i++) document.dispatchEvent(new KeyboardEvent('keydown', { key: 'j', bubbles: true }));
      await new Promise(requestAnimationFrame);
      return performance.now() - t0; }""")
    print("20 x J:", ms, "ms")
    assert ms < 2000, ms
    page.evaluate("document.querySelector('#queue li.q').dataset.mark = 'kept'")
    victim = page.evaluate("document.querySelectorAll('#queue li.q')[30].dataset.key")
    assert post(page, f"/api/clips/{victim}/reject") == 200
    page.wait_for_function("document.querySelector('#queue-count').textContent.includes('599')", timeout=8000)
    assert page.evaluate("document.querySelector('#queue li.q').dataset.mark") == "kept"  # the same element, not a rebuilt one
    page.get_by_role("button", name="Show 50 more").click()
    expect(items).to_have_count(100)
    # stream text stays text
    items.first.click()  # the best clip carries the hostile name and transcript
    expect(page.locator("#panel")).to_contain_text("<script>")
    expect(items.first).to_contain_text(HOSTILE)
    assert page.evaluate("window.__xss") is None
    page.goto(big + "/#posted")
    expect(page.locator("#posted-list")).to_contain_text(HOSTILE)
    assert page.locator("#posted-list a[href^='javascript']").count() == 0
    assert not page.errors, page.errors


def test_121_streamers_render_in_pages_and_filter(page, big):
    # bug caught: 100+ rows rebuilt on every poll, or a recording timer that never moves
    page.goto(big + "/#streamers")
    rows = page.locator("#streamer-list .srow")
    expect(rows).to_have_count(100)
    ms = page.evaluate("Math.max(...performance.getEntriesByName('streamers-render').map(e => e.duration))")
    print("streamer render:", ms, "ms")
    assert ms < 250, ms
    page.get_by_role("button", name=re.compile("Show 21 more")).click()
    expect(rows).to_have_count(121)
    expect(page.locator("#streamer-list")).to_contain_text(HOSTILE)
    assert page.locator("#streamer-list a[href^='javascript']").count() == 0
    timer = page.locator("#streamer-list .srow .state .num").first
    first = timer.inner_text()
    page.wait_for_timeout(1300)
    assert timer.inner_text() != first  # recording time moves on its own between polls
    expect(page.get_by_role("button", name="Clip last minute")).to_have_count(3)
    page.get_by_placeholder("Find a streamer").fill("streamer01")
    expect(rows).to_have_count(10)
    assert page.evaluate("window.__xss") is None
    assert not page.errors, page.errors


@pytest.mark.parametrize("tab", ["setup", "review", "streamers", "posted", "activity", "settings"])
def test_phone_width_has_no_sideways_scroll(browser, seeded, tab):
    # bug caught: the layout breaking on a phone (reviewing clips away from the laptop)
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    page.goto(f"{seeded}/#{tab}")
    page.wait_for_timeout(700)
    assert no_hscroll(page), tab
    assert not page.evaluate(OUTSIDE_JS), (tab, page.evaluate(OUTSIDE_JS))
    if tab == "review":
        expect(page.get_by_role("button", name="Approve and post")).to_be_in_viewport()  # the decision stays in reach
    ctx.close()
