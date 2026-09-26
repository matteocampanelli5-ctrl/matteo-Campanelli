/* Service worker minimal de MatNot pour les notifications Web Push. */
self.addEventListener("push", (event) => {
  let payload = {
    title: "MatNot",
    body: "Vous avez un rappel.",
  };

  if (event.data) {
    try {
      payload = { ...payload, ...event.data.json() };
    } catch {
      payload.body = event.data.text() || payload.body;
    }
  }

  event.waitUntil(
    self.registration.showNotification(payload.title || "MatNot", {
      body: payload.body || "Vous avez un rappel.",
      tag: payload.item_id ? `matnot-${payload.item_id}` : "matnot-reminder",
      renotify: true,
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((clientList) => {
        for (const client of clientList) {
          if ("focus" in client) return client.focus();
        }
        if (self.clients.openWindow) return self.clients.openWindow("/");
        return undefined;
      }),
  );
});