/* 人とロボットの境界線 / THE LINE BETWEEN HUMAN AND MACHINE

   1. 境界線スライダ（このページの主役）
   2. 右レールの機械側テレメトリ
   3. 時計
   演出はすべて任意。失敗しても本文は読める作りにする。 */

(() => {
  'use strict';

  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const pad = (n, w = 2) => String(n).padStart(w, '0');

  /* ------------------------------------------------------------ 境界線 */

  const split = document.getElementById('split');
  if (split) {
    const chars = Array.from(split.dataset.text || '');
    const human = document.getElementById('splitHuman');
    const machine = document.getElementById('splitMachine');
    const handle = document.getElementById('splitHandle');

    const roPos = document.getElementById('roPos');
    const roHuman = document.getElementById('roHuman');
    const roMachine = document.getElementById('roMachine');
    const roState = document.getElementById('roState');


    const toSpan = (text) => {
      const el = document.createElement('span');
      el.textContent = text;
      return el;
    };
    const humanSpans = chars.map(toSpan);
    const machineSpans = chars.map(ch =>
      toSpan('U+' + ch.codePointAt(0).toString(16).toUpperCase().padStart(4, '0')));
    human.replaceChildren(...humanSpans);
    machineSpans.forEach((el, i) => {
      if (i) machine.append('  ');
      machine.append(el);
    });


    const countSide = (spans, x, left) => spans.filter(el => {
      const r = el.getBoundingClientRect();
      const c = (r.left + r.right) / 2;
      return left ? c < x : c > x;
    }).length;

    let pos = 50;
    let drifting = !reduced;   // 触られるまでは境界がゆっくり呼吸する

    const setPos = (next) => {
      pos = Math.min(100, Math.max(0, next));
      split.style.setProperty('--split', pos + '%');
      handle.setAttribute('aria-valuenow', Math.round(pos));

      if (!roPos) return;
      const rect = split.getBoundingClientRect();
      const x = rect.left + rect.width * pos / 100;
      const h = countSide(humanSpans, x, true);
      const m = countSide(machineSpans, x, false);
      roPos.textContent = pos.toFixed(1) + '%';
      roHuman.textContent = h + ' 字';
      roMachine.textContent = m + ' 字';
      roState.textContent =
        h === 0 ? '機械のみ / MACHINE'
        : m === 0 ? '人間のみ / HUMAN'
        : '境界上 / ON THE LINE';
    };

    const fromEvent = (ev) => {
      const rect = split.getBoundingClientRect();
      setPos(((ev.clientX - rect.left) / rect.width) * 100);
    };

    split.addEventListener('pointerdown', (ev) => {
      drifting = false;
      split.setPointerCapture(ev.pointerId);
      fromEvent(ev);
    });

    split.addEventListener('pointermove', (ev) => {
      if (!split.hasPointerCapture(ev.pointerId)) return;
      ev.preventDefault();
      fromEvent(ev);
    });

    handle.addEventListener('keydown', (ev) => {
      const step = ev.shiftKey ? 10 : 3;
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(ev.key)) return;
      drifting = false;
      setPos(ev.key === 'Home' ? 0
        : ev.key === 'End' ? 100
        : pos + (ev.key === 'ArrowLeft' ? -step : step));
      ev.preventDefault();
    });

    let t = 0;
    const drift = () => {
      if (drifting) setPos(50 + Math.sin(t += 0.006) * 15);
      requestAnimationFrame(drift);
    };
    setPos(pos);

    if (document.fonts) document.fonts.ready.then(() => setPos(pos));
    window.addEventListener('resize', () => setPos(pos));
    if (!reduced) requestAnimationFrame(drift);
  }

  /* ------------------------------------------------------------- 時計 */

  const clock = document.getElementById('clock');
  if (clock) {
    const tick = () => {
      const d = new Date();
      clock.textContent =
        `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    };
    tick();
    setInterval(tick, 1000);
  }

  /* --------------------------------------------------- 右レールの機械語
     機械側が絶えず何か言い続けている、という層。内容は装飾で、
     読ませるためではなく「密度」として置いている。 */

  const stream = document.getElementById('stream');
  if (stream && !reduced) {
    const HEX = '0123456789ABCDEF';
    const WORDS = [
      'SCAN', 'DETECT', 'CONF', 'HUMAN?', 'MACHINE?', 'BOUNDARY',
      '照合', '判定不能', '境界', 'RESP', 'ENC', 'U+5883', '人間可読',
      'ЧЕЛОВЕК', '分界', '경계', 'LATENCY', 'THRESH',
    ];
    const rnd = (a) => a[Math.floor(Math.random() * a.length)];
    const hex = (n) => Array.from({ length: n }, () => rnd(HEX)).join('');

    const line = () =>
      `${hex(4)} ${rnd(WORDS)} ${(Math.random()).toFixed(3)}`;

    let buf = Array.from({ length: 14 }, line);
    stream.textContent = buf.join('  ／  ');

    setInterval(() => {
      buf.push(line());
      buf.shift();
      stream.textContent = buf.join('  ／  ');
    }, 900);
  }
})();
