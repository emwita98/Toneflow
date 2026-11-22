/* Scan & Refill page: camera QR scanning, phone lookup and walk-in sales. */
(() => {
  'use strict';

  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const REWARD_AT = Number(document.body.dataset.rewardAt || 10);
  const panel = document.getElementById('result');
  const startBtn = document.getElementById('startBtn');
  const camState = document.getElementById('camState');
  const phoneForm = document.getElementById('phoneForm');
  const phoneInput = document.getElementById('phoneInput');
  const walkinBtn = document.getElementById('walkinBtn');

  let scanner = null;
  let running = false;
  let busy = false;
  let lastCode = '';
  let lastAt = 0;
  let timer = null;

  // ---- helpers ---------------------------------------------------------
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;   // textContent: never inject HTML
    return node;
  };

  const fmt = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;

  function dots(points) {
    const wrap = el('div', 'dots');
    for (let i = 1; i <= REWARD_AT; i++) {
      wrap.append(el('span', 'dot' + (i <= points ? ' on' : '') + (i === REWARD_AT ? ' gift' : '')));
    }
    return wrap;
  }

  async function post(url, body) {
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
        body: JSON.stringify(body),
      });
      return await res.json();
    } catch (err) {
      return { status: 'error', title: 'Connection problem',
               message: 'Could not reach the server. Check your connection and try again.' };
    }
  }

  function show(d) {
    clearInterval(timer);
    panel.className = 'result ' + (d.status || 'error');
    panel.replaceChildren(el('div', 'result-title', d.title || 'Done'));
    if (d.client) {
      panel.append(el('div', 'result-client', d.client.name + ' · ' + d.client.phone));
    }
    if (typeof d.points === 'number') panel.append(dots(d.points));
    const msg = el('div', 'result-msg', d.message || '');
    panel.append(msg);

    if (d.status === 'cooldown' && d.wait_seconds > 0) {
      let left = d.wait_seconds;
      const tick = () => {
        if (left <= 0) {
          clearInterval(timer);
          msg.textContent = 'Cooldown finished - scan again to add the point.';
          return;
        }
        msg.textContent = 'Next point can be added in ' + fmt(left) + '.';
        left -= 1;
      };
      tick();
      timer = setInterval(tick, 1000);
    }
    if (navigator.vibrate) navigator.vibrate(d.status === 'ok' || d.status === 'free' ? 80 : [60, 60, 60]);
  }

  // ---- camera ----------------------------------------------------------
  function onScan(text) {
    const now = Date.now();
    if (busy || (text === lastCode && now - lastAt < 6000)) return;
    busy = true;
    lastCode = text;
    lastAt = now;
    post('/api/refill', { code: text }).then((d) => {
      show(d);
      lastAt = Date.now();
      setTimeout(() => { busy = false; }, 1500);
    });
  }

  async function startCamera() {
    if (typeof Html5Qrcode === 'undefined') {
      camState.textContent = 'Scanner library could not load. Use the phone number option.';
      return;
    }
    try {
      scanner = scanner || new Html5Qrcode('reader');
      await scanner.start(
        { facingMode: 'environment' },
        { fps: 10, qrbox: (w, h) => { const s = Math.floor(Math.min(w, h) * 0.7); return { width: s, height: s }; } },
        onScan,
        () => {}
      );
      running = true;
      startBtn.textContent = 'Stop camera';
      camState.textContent = 'Point the camera at the client’s QR code.';
    } catch (err) {
      camState.textContent = 'Camera unavailable. Allow camera access (HTTPS is required off localhost) or use a phone number.';
    }
  }

  async function stopCamera() {
    try { await scanner.stop(); scanner.clear(); } catch (_) { /* already stopped */ }
    running = false;
    startBtn.textContent = 'Start camera';
    camState.textContent = 'Camera is off.';
  }

  startBtn.addEventListener('click', () => (running ? stopCamera() : startCamera()));

  // ---- phone lookup ----------------------------------------------------
  phoneForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const phone = phoneInput.value.trim();
    if (!phone) return;
    const btn = phoneForm.querySelector('button');
    btn.disabled = true;
    show(await post('/api/refill', { phone }));
    btn.disabled = false;
    phoneInput.value = '';
    phoneInput.focus();
  });

  // ---- walk-in ---------------------------------------------------------
  walkinBtn.addEventListener('click', async () => {
    walkinBtn.disabled = true;
    show(await post('/api/walkin', {}));
    setTimeout(() => { walkinBtn.disabled = false; }, 1200);
  });
})();
