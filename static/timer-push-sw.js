/* Isolated private-device service worker. No caching, fetch interception,
 * customer data, or outbound requests. No server-side sender exists yet. */
"use strict";
self.addEventListener("push", event => {
  event.waitUntil(self.registration.showNotification("Tommy Tints HQ", {
    body: "Open HQ to review a private workshop alert.", tag: "hq-private-workshop-alert",
    icon: "/hq-icon-180.png"
  }));
});
self.addEventListener("notificationclick", event => {
  event.notification.close();
  event.waitUntil(self.clients.openWindow("/hq/timer/alerts"));
});