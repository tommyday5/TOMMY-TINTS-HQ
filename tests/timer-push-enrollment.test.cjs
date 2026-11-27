const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

function source(name) {
  return fs.readFileSync(path.join(__dirname, "..", "static", name), "utf8");
}

function fixture({ supported = true, saved = true } = {}) {
  const events = [];
  const scope = "/hq/timer/alerts/push/";
  const origin = "https://hq.example.invalid";
  const elements = {};
  for (const id of ["push-enrollment", "push-prepare", "push-enable", "push-status"]) {
    elements[id] = { disabled: id === "push-enable", textContent: "",
      addEventListener(type, fn) { this[type] = fn; } };
  }
  elements["push-enrollment"].dataset = { key: "BA", csrf: "synthetic-csrf", operationId: "synthetic-operation" };
  const registration = {
    scope: origin + scope,
    pushManager: { subscribe(options) {
      events.push(["subscribe", options]);
      return Promise.resolve({ toJSON() {
        return { endpoint: "https://web.push.apple.com/synthetic", keys: { p256dh: "public", auth: "secret" } };
      } });
    } }
  };
  const context = {
    document: { getElementById: id => elements[id] },
    window: supported ? { isSecureContext: true, PushManager: {}, Notification: {} } : { isSecureContext: false },
    navigator: { serviceWorker: {
      register(url, options) { events.push(["register",url,options]); return Promise.resolve(registration); },
      ready: Promise.resolve(registration)
    } },
    location: { origin, reload() { events.push(["reload"]); } },
    fetch(url, options) { events.push(["post",url,options]); return Promise.resolve({
      ok: saved, json: async () => ({enrolled: saved})
    }); },
    atob, URL, URLSearchParams, Uint8Array
  };
  vm.runInNewContext(source("timer-push-enrollment.js"), context);
  return { events, elements };
}

test("device setup does not subscribe until explicit owner gesture", async () => {
  const { events, elements } = fixture();
  assert.equal(events.length, 0);
  assert.equal(elements["push-enable"].disabled, true);
  await elements["push-prepare"].click();
  assert.equal(events.length, 1);
  assert.equal(events[0][0], "register");
  assert.equal(events[0][1], "/hq/timer/alerts/push/sw.js");
  assert.equal(events[0][2].scope, "/hq/timer/alerts/push/");
  assert.equal(elements["push-enable"].disabled, false);
  await elements["push-enable"].click();
  assert.equal(events[1][0], "subscribe");
  assert.equal(events[1][1].userVisibleOnly, true);
  assert.equal(events[2][0], "post");
  const options = events[2][2];
  assert.equal(options.redirect, "error");
  assert.equal(options.credentials, "same-origin");
  assert.equal(options.body.get("consent"), "yes");
  assert.equal(options.body.get("operation_id"), "synthetic-operation");
  assert.equal(options.body.get("csrf"), "synthetic-csrf");
  assert.equal(events[3][0], "reload");
});

test("unsupported browser and failed private save never claim enrollment", async () => {
  const unsupported = fixture({supported:false});
  await unsupported.elements["push-prepare"].click();
  assert.equal(unsupported.events.length,0);
  assert.match(unsupported.elements["push-status"].textContent,/unavailable/);
  const failed = fixture({saved:false});
  await failed.elements["push-prepare"].click();
  await failed.elements["push-enable"].click();
  assert.equal(failed.events.some(e => e[0] === "reload"),false);
  assert.match(failed.elements["push-status"].textContent,/not confirmed/);
  assert.equal(failed.elements["push-enable"].disabled,false);
});

test("isolated service worker ignores supplied private payload and external links", async () => {
  const listeners = {};
  const notifications = [];
  const navigation = [];
  const self = {
    addEventListener: (type, fn) => { listeners[type] = fn; },
    registration: { showNotification: async (title, options) => notifications.push({title,options}) },
    clients: { openWindow: async url => navigation.push(url) }
  };
  vm.runInNewContext(source("timer-push-sw.js"), {self});
  assert.equal(listeners.fetch, undefined);
  let pending;
  listeners.push({data: {json() { throw new Error("Must not read private payload"); }}, waitUntil(p) { pending = p; }});
  await pending;
  assert.equal(notifications.length,1);
  assert.equal(notifications[0].title,"Tommy Tints HQ");
  assert.equal(notifications[0].options.body,"Open HQ to review a private workshop alert.");
  let closed = false;
  listeners.notificationclick({notification:{data:{url:"https://evil.invalid/"},close(){closed=true;}},waitUntil(p){pending=p;}});
  await pending;
  assert.equal(closed,true);
  assert.deepEqual(navigation,["/hq/timer/alerts"]);
});
