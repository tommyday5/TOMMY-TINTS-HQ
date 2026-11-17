const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const source = readFileSync("static/hq-updates.js", "utf8");

async function setup({ automatic = false, next = "new", stored } = {}) {
    const doc = {};
    const win = {};
    const button = {};
    const banner = { hidden: true, dataset: { build: "old", automatic: String(automatic) } };
    const latest = {};
    const state = { reloads: 0, prompts: 0, allow: true, next, fetches: 0 };
    const document = {
        visibilityState: "visible",
        getElementById: id => ({
            "hq-update-banner": banner,
            "hq-update-refresh": { addEventListener: (event, handler) => { button[event] = handler; } },
            "hq-latest-version": latest,
        })[id],
        addEventListener: (event, handler) => { doc[event] = handler; },
    };
    vm.runInNewContext(source, {
        document,
        fetch: async (path, options) => {
            assert.equal(path, "/api/hq/version");
            assert.equal(options.cache, "no-store");
            assert.equal(options.credentials, "omit");
            state.fetches++;
            if (state.next === null) throw new Error("offline");
            return { ok: true, json: async () => ({ version: state.next }) };
        },
        sessionStorage: { getItem: () => stored, setItem: (_, value) => { stored = value; } },
        window: {
            location: { reload: () => { state.reloads++; } },
            confirm: () => { state.prompts++; return state.allow; },
            addEventListener: (event, handler) => { win[event] = handler; },
            setInterval: (handler, interval) => { assert.equal(interval, 60000); win.tick = handler; },
        },
    });
    await new Promise(resolve => setImmediate(resolve));
    return { state, banner, latest, windowEvents: win, docEvents: doc, button, document };
}

test("signed-in update is manual and edited work requires confirmation", async () => {
    const { state, banner, latest, docEvents, button } = await setup();
    assert.equal(latest.textContent, "new");
    assert.equal(banner.hidden, false);
    assert.equal(state.reloads, 0);
    docEvents.input({ target: {} });
    state.allow = false;
    button.click();
    assert.equal(state.prompts, 1);
    assert.equal(state.reloads, 0);
    state.allow = true;
    button.click();
    assert.equal(state.reloads, 1);
});

test("foreground, page restoration and active polling detect changes", async () => {
    const { state, banner, windowEvents, docEvents, document } = await setup({ next: "old" });
    assert.equal(banner.hidden, true);
    document.visibilityState = "hidden";
    await windowEvents.tick();
    assert.equal(state.fetches, 1);
    document.visibilityState = "visible";
    state.next = "new";
    await docEvents.visibilitychange();
    assert.equal(banner.hidden, false);
    state.next = null;
    await windowEvents.pageshow();
    assert.equal(banner.hidden, false);
    state.next = "old";
    await windowEvents.tick();
    assert.equal(banner.hidden, true);
});

test("idle public screen refreshes once and falls back to the button", async () => {
    assert.equal((await setup({ automatic: true })).state.reloads, 1);
    const { state, banner } = await setup({ automatic: true, stored: "new" });
    assert.equal(state.reloads, 0);
    assert.equal(banner.hidden, false);
    const delayed = await setup({ automatic: true, next: "old" });
    delayed.docEvents.click({ target: {} });
    delayed.state.next = "new";
    await delayed.windowEvents.focus();
    assert.equal(delayed.state.reloads, 0);
    assert.equal(delayed.banner.hidden, false);
});
