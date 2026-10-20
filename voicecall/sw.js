// Service Worker：Web Push 来电通知（见 .devflow/INTERFACE-incoming-call.md C 段）
// 经典 Web Push：push → showNotification；notificationclick → 唤起/聚焦 PWA 到来电页。
// 文案由后端固定生成（中性"来电"），这里只兜底缺省，绝不自造人设名。

self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = {}; }
  const title = data.title || '来电';
  const opts = {
    body: data.body || '点击接听',
    tag: 'incoming-call',        // 同 tag 覆盖，连发只留最新一条
    renotify: true,
    requireInteraction: true,    // 安卓常驻；iOS 忽略但无害
    data: { url: data.url || '/?call=1' },
    actions: [                   // 安卓平铺；iOS 需展开，UI 不依赖其平铺可见
      { action: 'answer', title: '接听' },
      { action: 'reject', title: '拒绝' },
    ],
  };
  // userVisibleOnly：每条 push 必弹通知，不得静默（iOS 强制）
  event.waitUntil(self.registration.showNotification(title, opts));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  if (event.action === 'reject') return;  // 拒绝：不开页，来电标志交前台/TTL 处置
  const url = (event.notification.data && event.notification.data.url) || '/?call=1';
  event.waitUntil((async () => {
    const wins = await clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const w of wins) {
      try { await w.focus(); if (w.navigate) { try { await w.navigate(url); } catch (e) {} } return; }
      catch (e) {}
    }
    if (clients.openWindow) { try { await clients.openWindow(url); } catch (e) { await clients.openWindow('/?call=1'); } }
  })());
});
