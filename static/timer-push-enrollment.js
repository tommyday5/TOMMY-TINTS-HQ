(() => {
  "use strict";
  const panel = document.getElementById("push-enrollment");
  if (!panel) return;
  const prepare = document.getElementById("push-prepare");
  const enable = document.getElementById("push-enable");
  const status = document.getElementById("push-status");
  let registration = null;
  const key = panel.dataset.key;
  const keyBytes = Uint8Array.from(atob(key.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - key.length % 4) % 4)), c => c.charCodeAt(0));
  prepare.addEventListener("click", async () => {
    if (!window.isSecureContext || !("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
      status.textContent = "Web Push unavailable here. On iPhone use the installed Home Screen app.";
      return;
    }
    prepare.disabled = true;
    try {
      registration = await navigator.serviceWorker.register("/hq/timer/alerts/push/sw.js", {scope: "/hq/timer/alerts/push/"});
      registration = await navigator.serviceWorker.ready;
      if (registration.scope !== new URL("/hq/timer/alerts/push/", location.origin).href) throw new Error("Incorrect scope");
      enable.disabled = false;
      status.textContent = "Prepared. Press Enable device registration to consent.";
    } catch (_) {
      status.textContent = "Device preparation failed. No HQ registration was saved.";
      prepare.disabled = false;
    }
  });
  enable.addEventListener("click", async () => {
    if (!registration) return;
    enable.disabled = true;
    try {
      // Called directly from the user's gesture, before any asynchronous work.
      const subscription = await registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: keyBytes});
      const data = subscription.toJSON();
      const body = new URLSearchParams({csrf: panel.dataset.csrf, action: "enroll", consent: "yes",
        application_key: key, endpoint: data.endpoint, p256dh: data.keys.p256dh, auth_key: data.keys.auth});
      const response = await fetch("/hq/timer/alerts/push/", {method: "POST", credentials: "same-origin",
        cache: "no-store", body, redirect: "error"});
      if (!response.ok || !(await response.json()).enrolled) throw new Error("Registration not saved");
      status.textContent = "HQ registration saved. Phone delivery is not enabled.";
      location.reload();
    } catch (_) {
      status.textContent = "HQ registration was not confirmed. Sign in again or review browser permission and retry. No notification was sent.";
      enable.disabled = false;
    }
  });
})();