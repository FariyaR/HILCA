/* HILCA Mission Control — SPA orchestrator.
   intake -> CCU casts (approval gate) -> chat-bubble dialectic with live
   telemetry (tokens, convergence gauge, verdicts, activity feed) -> synthesis,
   deliverable, executive log. Driven entirely by the SSE stream; an
   interrupted run replays its transcript from REST and resumes the stream
   from the last checkpoint. */
(function () {
  'use strict';

  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  var HUES = ['#38bdf8', '#fb7185', '#fbbf24', '#34d399', '#a78bfa'];
  var COLLAPSE_AT = 700;          // chars before a bubble renders collapsed
  var GAUGE_CIRC = 251.2;         // 2πr for r=40

  /* ------------------------------------------------------------------ state */
  var S = {
    runId: null,
    budget: 2000000,
    tokens: 0, tokensShown: 0,
    roster: [],                    // [{name, role, directive, persona, rubric}]
    byName: {},                    // name -> {index, hue}
    round: 0, phase: 'main',
    msgs: 0,
    verdicts: { agree: 0, disagree: 0 },
    threshold: 0.85,
    stream: null,
    done: false,
    lastDivider: null,             // "round|phase" of the last divider drawn
    pauseBusy: false,
  };

  /* ------------------------------------------------------------ tiny utils */
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
    });
  }
  function el(html) {
    var t = document.createElement('template');
    t.innerHTML = html.trim();
    return t.content.firstElementChild;
  }
  function monogram(name) {
    var parts = String(name).replace(/^The\s+/i, '').trim().split(/\s+/);
    return (parts.length > 1 ? parts[0][0] + parts[1][0] : parts[0].slice(0, 2)).toUpperCase();
  }
  function hueFor(name) {
    if (name === 'CCU') return 'var(--gold)';
    var a = S.byName[name];
    return a ? a.hue : '#94a3b8';
  }
  function toast(text, cls) {
    var t = el('<div class="toast ' + (cls || '') + '">' + esc(text) + '</div>');
    $('#toasts').appendChild(t);
    setTimeout(function () { t.remove(); }, 4000);
  }
  function api(method, path, body) {
    return fetch(path, {
      method: method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error((j && (j.detail && JSON.stringify(j.detail) || j.message)) || ('HTTP ' + r.status));
        return j;
      });
    });
  }

  /* ================================================================ INTAKE */
  function initChips(root) {
    var input = el('<input type="text" placeholder="' + esc(root.dataset.placeholder || '') + '">');
    root.appendChild(input);
    root.addEventListener('click', function () { input.focus(); });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ',') {
        e.preventDefault();
        var v = input.value.trim().replace(/,+$/, '');
        if (!v) return;
        var chip = el('<span class="chip">' + esc(v) + '<span class="x">×</span></span>');
        chip.querySelector('.x').addEventListener('click', function () { chip.remove(); });
        root.insertBefore(chip, input);
        input.value = '';
      } else if (e.key === 'Backspace' && !input.value) {
        var chips = $$('.chip', root);
        if (chips.length) chips[chips.length - 1].remove();
      }
    });
    return { values: function () { return $$('.chip', root).map(function (c) { return c.childNodes[0].textContent.trim(); }); } };
  }

  function initStepper(id, min, max) {
    var root = $('#' + id), val = $('.val', root);
    function set(v) {
      v = Math.max(min, Math.min(max, v));
      val.textContent = v;
      $$('.presets[data-for="' + id + '"] .p').forEach(function (p) {
        p.classList.toggle('on', +p.dataset.v === v);
      });
    }
    $$('button', root).forEach(function (b) {
      b.addEventListener('click', function () { set(+val.textContent + (+b.dataset.d)); });
    });
    $$('.presets[data-for="' + id + '"] .p').forEach(function (p) {
      p.addEventListener('click', function () { set(+p.dataset.v); });
    });
    set(+val.textContent);
    return { get: function () { return +val.textContent; } };
  }

  var tagsChips = initChips($('#chips-tags'));
  var hintChips = initChips($('#chips-hints'));
  var stepMain = initStepper('step-main', 1, 100);
  var stepFinal = initStepper('step-final', 0, 10);

  $('#lnk-resume').addEventListener('click', function () {
    $('#resume-row').classList.toggle('show');
  });

  $('#intake-form').addEventListener('submit', function (e) {
    e.preventDefault();
    var topic = $('#f-topic').value.trim();
    if (!topic) return;
    var urls = [$('#f-url1').value, $('#f-url2').value, $('#f-url3').value]
      .map(function (u) { return u.trim(); }).filter(Boolean);
    var body = {
      topic: topic,
      tags: tagsChips.values().slice(0, 5),
      agent_hints: hintChips.values().slice(0, 3),
      evidence_urls: urls,
      email: $('#f-email').value.trim() || null,
      main_rounds: stepMain.get(),
      final_rounds: stepFinal.get(),
    };
    var btn = $('#btn-launch');
    btn.disabled = true; btn.textContent = 'Initializing…';
    api('POST', '/api/runs', body).then(function (j) {
      S.runId = j.run_id;
      S.budget = j.token_budget || S.budget;
      enterRun(topic, body.tags);
      S.stream = openDialecticStream(S.runId, handlers);
      setTyping('CCU', 'reading the mission intake…');
    }).catch(function (err) {
      btn.disabled = false; btn.textContent = 'Initiate Dialectic';
      toast('Launch failed: ' + err.message, 'err');
    });
  });

  $('#btn-resume').addEventListener('click', function () {
    var id = $('#f-resume-id').value.trim();
    if (id) joinRun(id);
  });

  /* Open an existing run: attach to the live stream when the engine is
     running (the stream replays the full event history), REST-replay when it
     is not, and offer a checkpoint resume for interrupted runs. */
  function joinRun(id) {
    api('GET', '/api/runs/' + id).then(function (run) {
      S.runId = id;
      enterRun(run.topic, run.tags || []);
      if (run.roster && run.roster.length) setRoster(run.roster);
      if (run.live) {
        S.stream = openDialecticStream(id, handlers);
        toast('Attached to the live run — replaying…', 'ok');
      } else if (run.status === 'complete') {
        replayHistory(run);
        toast('Run already complete — transcript restored', 'ok');
      } else {
        replayHistory(run);
        S.stream = openDialecticStream(id, handlers, { resume: true });
        toast('Resuming from the last checkpoint…', 'ok');
      }
    }).catch(function (err) { toast('Cannot open run: ' + err.message, 'err'); });
  }

  /* =============================================================== RUN VIEW */
  function enterRun(topic, tags) {
    // The URL carries the run: a fully reloaded page (mobile browsers kill
    // background tabs) reattaches to its run instead of landing on intake.
    try { history.replaceState(null, '', '?attach=' + S.runId); } catch (e) {}
    $('#screen-intake').classList.remove('active');
    $('#screen-run').classList.add('active');
    $('#mi-topic').textContent = topic;
    $('#mi-tags').innerHTML = tags.map(function (t) { return '<span class="mtag">' + esc(t) + '</span>'; }).join('');
    var rid = $('#mi-runid');
    rid.querySelector('span').textContent = S.runId.slice(0, 8) + '…';
    rid.onclick = function () {
      navigator.clipboard && navigator.clipboard.writeText(S.runId);
      toast('Run ID copied', 'ok');
    };
  }

  /* -------------------------------------------------------------- roster */
  function setRoster(roster) {
    S.roster = roster;
    S.byName = {};
    roster.forEach(function (a, i) {
      S.byName[a.name] = { index: i + 1, hue: HUES[i % HUES.length] };
    });
    $('#st-agents').textContent = roster.length;

    var list = $('#cast-list');
    list.innerHTML = '';
    roster.forEach(function (a, i) {
      var hue = HUES[i % HUES.length];
      var item = el(
        '<div class="cast-item" style="animation-delay:' + (i * 0.07) + 's" data-agent="' + esc(a.name) + '">' +
          '<div class="avatar" style="background:linear-gradient(135deg,' + hue + ',color-mix(in srgb,' + hue + ' 55%, #101828))">' + esc(monogram(a.name)) + '</div>' +
          '<div class="who">' +
            '<div class="nm">' + esc(a.name) + '</div>' +
            '<div class="rl">' + esc(a.role || '') + '</div>' +
            '<div class="st"><span>standby</span></div>' +
          '</div>' +
        '</div>');
      list.appendChild(item);
    });

    var sel = $('#fb-target');
    sel.innerHTML = '<option value="CCU">→ CCU</option>' + roster.map(function (a) {
      return '<option value="' + esc(a.name) + '">→ ' + esc(a.name) + '</option>';
    }).join('');
  }

  function castStatus(name, text, thinking) {
    var item = $('.cast-item[data-agent="' + (window.CSS && CSS.escape ? CSS.escape(name) : name) + '"]');
    if (!item) return;
    var st = $('.st', item);
    st.className = 'st' + (thinking ? ' thinking' : '');
    st.innerHTML = (thinking ? '<span class="pulse"></span>' : '') + '<span>' + esc(text) + '</span>';
  }

  function castVerdict(name, verdict) {
    var item = $('.cast-item[data-agent="' + (window.CSS && CSS.escape ? CSS.escape(name) : name) + '"]');
    if (!item) return;
    var nm = $('.nm', item);
    if (!$('.v-badge', nm)) nm.appendChild(el('<span class="v-badge ' + verdict + '">' + verdict.toUpperCase() + '</span>'));
  }

  /* ---------------------------------------------------------------- chat */
  var chatScroll = $('#chat-scroll'), chatInner = $('#chat-inner');

  function pinned() {
    return chatScroll.scrollHeight - chatScroll.scrollTop - chatScroll.clientHeight < 160;
  }
  function scrollToEnd(force) {
    if (force || pinned()) chatScroll.scrollTop = chatScroll.scrollHeight;
  }
  chatScroll.addEventListener('scroll', function () {
    $('#jump-latest').classList.toggle('show', !pinned() && !S.done);
  });
  $('#jump-latest').addEventListener('click', function () { scrollToEnd(true); });

  function append(node) {
    var typing = $('#typing-row');
    if (typing) chatInner.insertBefore(node, typing); else chatInner.appendChild(node);
    scrollToEnd();
  }

  function divider(round, phase) {
    var key = round + '|' + phase;
    if (S.lastDivider === key) return;
    S.lastDivider = key;
    var lab = phase === 'final' ? 'Final Round ' + round : 'Round ' + round;
    append(el('<div class="round-div ' + (phase === 'final' ? 'final' : '') + '">' +
      '<div class="line"></div><div class="lab">' + esc(lab) + '</div><div class="line"></div></div>'));
  }

  function sysPill(text) {
    append(el('<div class="sys-pill">' + esc(text) + '</div>'));
  }

  /* strip the machine footer (STANCE / CONVERGENCE) into a subtle chip row */
  function splitFooter(text) {
    var stance = null, conv = null;
    var body = text.replace(/^\s*STANCE\s*:\s*(.+?)\s*$/gim, function (_, v) { stance = v; return ''; })
                   .replace(/^\s*CONVERGENCE\s*:\s*([\d.]+)\s*$/gim, function (_, v) { conv = v; return ''; })
                   .trim();
    return { body: body, stance: stance, conv: conv };
  }

  function bubble(opts) {
    // opts: {who, kind, round, text, rowCls, verdict, expand}
    var isCCU = opts.who === 'CCU';
    var hue = hueFor(opts.who);
    var f = splitFooter(opts.text);
    var long = f.body.length > COLLAPSE_AT && !opts.expand;

    var avatar = isCCU
      ? '<div class="avatar ccu">◆</div>'
      : opts.who === 'You'
        ? '<div class="avatar you">YOU</div>'
        : '<div class="avatar" style="background:linear-gradient(135deg,' + hue + ',color-mix(in srgb,' + hue + ' 55%, #101828))">' + esc(monogram(opts.who)) + '</div>';

    var footer = '';
    if (f.stance || f.conv) {
      footer = '<div style="margin-top:10px;padding-top:8px;border-top:1px dashed var(--stroke);font:500 11px/1.5 var(--font);color:var(--text-3)">' +
        (f.stance ? '<span>Stance — ' + esc(f.stance) + '</span>' : '') +
        (f.conv ? '<span style="margin-left:10px;color:var(--text-2)">· convergence ' + esc(f.conv) + '</span>' : '') +
        '</div>';
    }
    var verdictHtml = opts.verdict
      ? '<div class="verdict ' + opts.verdict + '">' + (opts.verdict === 'agree' ? '✓ AGREE' : opts.verdict === 'disagree' ? '✕ DISAGREE' : '? UNCLEAR') + '</div>'
      : '';

    var row = el(
      '<div class="msg ' + (opts.rowCls || '') + '">' + avatar +
        '<div class="bubble" style="--accent:' + hue + '">' +
          '<div class="meta"><span class="nm">' + esc(opts.who) + '</span>' +
            (opts.kind ? '<span class="kind">' + esc(opts.kind) + '</span>' : '') +
            (opts.round ? '<span class="rnd">R' + opts.round + '</span>' : '') +
          '</div>' +
          '<div class="body' + (long ? ' collapsed' : '') + '">' + verdictHtml +
            '<div class="md-wrap">' + renderMarkdown(f.body) + '</div>' + footer +
            (long ? '<span class="expander"><span class="lbl">Expand</span> <span class="car">▼</span></span>' : '') +
          '</div>' +
        '</div>' +
      '</div>');

    var exp = $('.expander', row);
    if (exp) exp.addEventListener('click', function () {
      var b = $('.body', row);
      b.classList.toggle('collapsed');
      $('.lbl', exp).textContent = b.classList.contains('collapsed') ? 'Expand' : 'Collapse';
    });
    append(row);
    bumpMsgs();
  }

  function engineCard(opts) {
    // opts: {icon, title, status: 'ok'|'warn'|null, text, open}
    var st = opts.status === 'ok' ? '<span class="st-ok">PASS</span>'
           : opts.status === 'warn' ? '<span class="st-warn">FLAGGED</span>' : '';
    var card = el(
      '<div class="engine-card' + (opts.open ? ' open' : '') + '">' +
        '<div class="ec-head"><span class="ic">' + opts.icon + '</span><span>' + esc(opts.title) + '</span>' + st + '<span class="car">▼</span></div>' +
        '<div class="ec-body">' + renderMarkdown(opts.text) + '</div>' +
      '</div>');
    $('.ec-head', card).addEventListener('click', function () { card.classList.toggle('open'); });
    append(card);
    bumpMsgs();
  }

  /* typing indicator */
  function setTyping(who, label) {
    clearTyping();
    if (S.done || !who) return;
    var hue = hueFor(who);
    var avatar = who === 'CCU'
      ? '<div class="avatar ccu">◆</div>'
      : '<div class="avatar" style="background:linear-gradient(135deg,' + hue + ',color-mix(in srgb,' + hue + ' 55%, #101828))">' + esc(monogram(who)) + '</div>';
    var row = el(
      '<div id="typing-row">' + avatar +
        '<div class="t-bubble" style="--accent:' + hue + '">' +
          '<span class="t-label">' + esc(who) + (label ? ' — ' + esc(label) : '') + '</span>' +
          '<span class="dots"><i></i><i></i><i></i></span>' +
        '</div>' +
      '</div>');
    chatInner.appendChild(row);
    if (who !== 'CCU') castStatus(who, 'thinking', true);
    scrollToEnd();
  }
  function clearTyping() {
    var t = $('#typing-row');
    if (t) t.remove();
  }

  /* ------------------------------------------------------------ dashboard */
  function bumpMsgs() { S.msgs++; $('#st-msgs').textContent = S.msgs; }

  function setCalls(d) {
    if (typeof d.calls !== 'number') return;
    $('#st-calls').textContent = d.ceiling ? d.calls + '/' + d.ceiling : String(d.calls);
  }

  /* context ring (Claude-style /compact) */
  var CTX_CIRC = 75.4; // 2π × r12, matches the SVG in index.html
  function setCtx(d) {
    var ring = $('#ctx-ring');
    if (!ring || typeof d.pct !== 'number') return;
    var p = Math.max(0, Math.min(1, d.pct));
    $('#ctx-fg').style.strokeDashoffset = (CTX_CIRC * (1 - p)).toFixed(1);
    $('#ctx-val').textContent = Math.round(p * 100);
    ring.classList.toggle('crit', p >= 0.7);
    ring.classList.toggle('warn', p >= 0.55 && p < 0.7);
    ring.title = 'Context window ' + Math.round(p * 100) + '% used (~' +
      Number(d.est_tokens).toLocaleString() + ' of ' + Number(d.window).toLocaleString() +
      ' tokens) — click to compact';
  }

  function setTokens(v) {
    if (typeof v !== 'number' || v <= S.tokens) { if (typeof v === 'number') S.tokens = Math.max(S.tokens, v); return tweenTokens(); }
    S.tokens = v; tweenTokens();
  }
  var tokTween = null;
  function tweenTokens() {
    if (tokTween) cancelAnimationFrame(tokTween);
    var from = S.tokensShown, to = S.tokens, t0 = performance.now();
    (function step(t) {
      var k = Math.min(1, (t - t0) / 600);
      k = 1 - Math.pow(1 - k, 3);
      S.tokensShown = Math.round(from + (to - from) * k);
      $('#tok-label').textContent = S.tokensShown.toLocaleString() + ' / ' + (S.budget / 1000000) + 'M';
      var pct = Math.min(100, S.tokensShown / S.budget * 100);
      var fill = $('#tok-fill');
      fill.style.width = pct + '%';
      fill.classList.toggle('warn', pct > 80);
      if (k < 1) tokTween = requestAnimationFrame(step);
    })(t0);
  }

  function setRound(round, phase) {
    S.round = round; S.phase = phase;
    $('#st-round').textContent = round;
    var chip = $('#phase-chip');
    chip.className = phase === 'final' ? 'final' : '';
    chip.id = 'phase-chip';
    chip.textContent = phase === 'final' ? 'Final Conclusion · R' + round : 'Main Loop · Round ' + round;
    var track = $('#round-track');
    var dot = track.querySelector('[data-r="' + round + '"]');
    if (!dot) {
      dot = el('<span class="rdot' + (phase === 'final' ? ' final-r' : '') + '" data-r="' + round + '"></span>');
      track.appendChild(dot);
    }
    $$('.rdot', track).forEach(function (d) {
      d.classList.remove('now');
      if (+d.dataset.r < round) d.classList.add('done');
    });
    dot.classList.add('now');
  }

  function setGauge(avg) {
    var fg = $('#gauge-fg');
    fg.style.strokeDashoffset = GAUGE_CIRC * (1 - Math.max(0, Math.min(1, avg)));
    $('#gauge-val').textContent = (avg * 100).toFixed(0) + '%';
    $('#gauge-note').textContent = 'Average vote ' + avg.toFixed(2) + ' · exit threshold ' + S.threshold.toFixed(2);
  }
  function setGaugeTick(threshold) {
    S.threshold = threshold;
    // place the tick on the circle at threshold
    var ang = threshold * 2 * Math.PI - Math.PI / 2;
    var cx = 46, cy = 46;
    var t = $('#gauge-tick');
    t.setAttribute('x1', cx + Math.cos(ang) * 35);
    t.setAttribute('y1', cy + Math.sin(ang) * 35);
    t.setAttribute('x2', cx + Math.cos(ang) * 45);
    t.setAttribute('y2', cy + Math.sin(ang) * 45);
  }
  setGaugeTick(0.85);

  var feedN = 0;
  function feed(text, hot) {
    feedN++;
    var f = $('#feed');
    var t = new Date();
    var hh = String(t.getHours()).padStart(2, '0') + ':' + String(t.getMinutes()).padStart(2, '0');
    f.appendChild(el('<div class="feed-item' + (hot ? ' hot' : '') + '"><span class="fi-t">' + hh + '</span><span>' + esc(text) + '</span></div>'));
    while (f.children.length > 80) f.removeChild(f.firstChild);
    f.scrollTop = f.scrollHeight;
  }

  function setLive(state) { // 'live' | 'paused' | 'off'
    var pill = $('#live-pill');
    pill.className = state === 'live' ? '' : state;
    pill.id = 'live-pill';
    $('#live-txt').textContent = state === 'live' ? 'LIVE' : state === 'paused' ? 'PAUSED' : 'ENDED';
  }

  /* ------------------------------------------------------------- approval */
  function showApproval(roster) {
    var grid = $('#cards-grid');
    grid.innerHTML = '';
    roster.forEach(function (a, i) {
      var hue = HUES[i % HUES.length];
      grid.appendChild(el(
        '<div class="role-card" style="--accent:' + hue + ';animation-delay:' + (i * 0.08) + 's" data-i="' + i + '">' +
          '<div class="rc-top">' +
            '<div class="avatar" style="background:linear-gradient(135deg,' + hue + ',color-mix(in srgb,' + hue + ' 55%, #101828))">' + esc(monogram(a.name)) + '</div>' +
            '<input class="rc-name" type="text" value="' + esc(a.name) + '" style="margin:0;font-weight:700">' +
          '</div>' +
          '<div class="rc-label">Epistemic role</div>' +
          '<input class="rc-role" type="text" value="' + esc(a.role || '') + '">' +
          '<div class="rc-label">Persona</div>' +
          '<input class="rc-persona" type="text" value="' + esc(a.persona || '') + '">' +
          '<div class="rc-label">Round-1 directive</div>' +
          '<textarea class="rc-directive">' + esc(a.directive || '') + '</textarea>' +
        '</div>'));
    });
    $('#approval').classList.remove('hidden');
  }

  $('#btn-approve').addEventListener('click', function () {
    var cards = $$('.role-card');
    var roster = cards.map(function (c, i) {
      var src = S.roster[i] || {};
      return {
        name: $('.rc-name', c).value.trim() || ('Subagent ' + (i + 1)),
        role: $('.rc-role', c).value.trim() || 'Dialectic agent',
        persona: $('.rc-persona', c).value.trim(),
        directive: $('.rc-directive', c).value.trim() || 'Contribute your specialist thesis.',
        rubric: src.rubric || '',
      };
    });
    var btn = $('#btn-approve');
    btn.disabled = true; btn.textContent = 'Releasing…';
    api('POST', '/api/runs/' + S.runId + '/approve', { roster: roster })
      .then(function () { /* roster_approved arrives on the stream */ })
      .catch(function (err) {
        btn.disabled = false; btn.textContent = 'Approve Cast & Begin';
        toast('Approval failed: ' + err.message, 'err');
      });
  });

  /* ------------------------------------------------------------- composer */
  $('#fb-send').addEventListener('click', sendFeedback);
  $('#fb-text').addEventListener('keydown', function (e) { if (e.key === 'Enter') sendFeedback(); });
  function sendFeedback() {
    var text = $('#fb-text').value.trim();
    if (!text || !S.runId) return;
    var target = $('#fb-target').value;
    api('POST', '/api/runs/' + S.runId + '/feedback', { target: target, message: text })
      .then(function () {
        $('#fb-text').value = '';
        toast('Queued — delivered at ' + target + "'s next turn", 'ok');
      })
      .catch(function (err) { toast('Feedback failed: ' + err.message, 'err'); });
  }

  /* -------------------------------------------------------------- controls */
  $('#btn-pause').addEventListener('click', function () {
    if (!S.runId || S.pauseBusy) return;
    S.pauseBusy = true;
    var btn = $('#btn-pause');
    var pausing = !btn.classList.contains('paused');
    api('POST', '/api/runs/' + S.runId + (pausing ? '/pause' : '/resume'))
      .then(function () {
        btn.classList.toggle('paused', pausing);
        btn.innerHTML = pausing ? '▶ &nbsp;Resume' : '⏸ &nbsp;Pause';
        if (pausing) { setLive('paused'); toast('Pausing before the next model call…'); }
        else setLive('live');
      })
      .catch(function (err) { toast(err.message, 'err'); })
      .finally(function () { S.pauseBusy = false; });
  });

  $('#btn-new').addEventListener('click', function () { location.href = location.pathname; });

  $('#ctx-ring').addEventListener('click', function () {
    var ring = $('#ctx-ring');
    if (!S.runId || S.done || ring.classList.contains('busy')) return;
    ring.classList.add('busy');
    api('POST', '/api/runs/' + S.runId + '/compact')
      .then(function () { toast('Compaction requested — runs before the next model call'); })
      .catch(function (err) {
        ring.classList.remove('busy');
        toast('Compaction request failed: ' + err.message, 'err');
      });
  });

  $('#btn-deliv').addEventListener('click', function () {
    window.open('/api/runs/' + S.runId + '/deliverable', '_blank');
  });

  $('#btn-transcript').addEventListener('click', function () {
    window.open('/api/runs/' + S.runId + '/transcript', '_blank');
  });

  /* ============================================================== HANDLERS */
  var handlers = {
    log: function (d) {
      feed(d.message, /Round|round|verdict|Convergence|plateaued|Devil|Gap|deliverable|resumed/.test(d.message));
    },

    context_ready: function (d) {
      sysPill('Grounding material assembled — ' + d.chars.toLocaleString() + ' characters');
      setTyping('CCU', 'architecting the dialectic cast…');
    },

    round_started: function (d) {
      setRound(d.round, d.phase);
      divider(d.round, d.phase);
      if (d.round > 1) {
        setTyping('CCU', d.phase === 'final' ? 'drafting the final-round directives…' : 'dispatching round ' + d.round + ' directives…');
      }
    },

    agent_spawned: function (d) { /* roster is delivered whole via cast text; collect */
      // build incrementally so the cast panel fills as agents spawn
      var existing = S.roster.slice();
      existing[d.index - 1] = { name: d.name, role: d.role, directive: d.directive, persona: '', rubric: '' };
      setRoster(existing.filter(Boolean));
    },

    awaiting_approval: function (d) {
      clearTyping();
      setRoster(d.roster);
      showApproval(d.roster);
      setLive('paused');
      feed('Awaiting operator approval of the cast', true);
    },

    roster_approved: function (d) {
      $('#approval').classList.add('hidden');
      setRoster(d.roster);
      setLive('live');
      sysPill('Cast approved by the operator — dialectic released');
      if (S.roster.length) setTyping(S.roster[0].name, 'composing the opening thesis…');
    },

    ccu_message: function (d) {
      clearTyping();
      setTokens(d.tokens);
      setCalls(d);
      routeCcu(d.type, d.text, d.round);
    },

    agent_message: function (d) {
      clearTyping();
      setTokens(d.tokens);
      setCalls(d);
      if (d.agent === 'OPERATOR') { sysPill('Operator update recorded'); return; }
      routeAgent(d);
    },

    agent_verdict: function (d) {
      castVerdict(d.agent, d.verdict);
      S.verdicts[d.verdict] = (S.verdicts[d.verdict] || 0) + 1;
      $('#panel-verdicts').classList.remove('hidden');
      $('#tv-agree').textContent = S.verdicts.agree || 0;
      $('#tv-disagree').textContent = S.verdicts.disagree || 0;
    },

    convergence: function (d) {
      setGaugeTick(d.threshold);
      setGauge(d.average);
    },

    synthesis: function (d) { setTokens(d.tokens); /* rendered via ccu_message final_synthesis */ },

    deliverable: function (d) {
      $('#panel-deliv').classList.remove('hidden');
      toast(d.emailed ? 'Deliverable ready — emailed and downloadable' : 'Deliverable ready for download', 'ok');
    },

    log_summary: function (d) {
      $('#panel-exec').classList.remove('hidden');
      $('#exec-summary').textContent = d.text;
    },

    human_feedback: function (d) {
      bubble({ who: 'You', kind: 'to ' + d.target, round: d.round, text: d.text, rowCls: 'you-row', expand: true });
    },

    reconnecting: function () {
      setLive('paused');
      toast('Connection hiccup — reconnecting to the run…', 'err');
    },
    reconnected: function () {
      setLive('live');
      toast('Reconnected — caught up on missed events', 'ok');
    },

    paused: function () { setLive('paused'); },
    resumed: function () {
      setLive('live');
      var btn = $('#btn-pause');
      btn.classList.remove('paused');
      btn.innerHTML = '⏸ &nbsp;Pause';
    },

    resumed_from_checkpoint: function (d) {
      sysPill('Resumed from checkpoint — phase ' + d.phase + ', round ' + d.round);
      toast('Run resumed from its last checkpoint', 'ok');
    },

    context_usage: function (d) { setCtx(d); },

    compacted: function (d) {
      $('#ctx-ring').classList.remove('busy');
      var how = d.reason === 'auto' ? 'automatic — window crossed the threshold' : 'manual';
      feed('Context compacted (' + how + '): ~' + Number(d.before_tokens).toLocaleString() +
           ' → ~' + Number(d.after_tokens).toLocaleString() + ' est. tokens', true);
      toast('Context compacted — ~' + Number(d.before_tokens).toLocaleString() +
            ' → ~' + Number(d.after_tokens).toLocaleString() + ' tokens', 'ok');
    },

    done: function (d) {
      S.done = true;
      clearTyping();
      setLive('off');
      $('#st-calls').textContent = d.calls + '/' + d.ceiling;
      setTokens(d.tokens);
      var chip = $('#phase-chip');
      chip.className = 'complete'; chip.id = 'phase-chip';
      chip.textContent = 'Mission Complete';
      $$('#round-track .rdot').forEach(function (x) { x.classList.add('done'); x.classList.remove('now'); });
      S.roster.forEach(function (a) { castStatus(a.name, 'concluded'); });
      append(el('<div class="banner done-banner">' +
        '<div class="b-t">Dialectic complete</div>' +
        '<div class="b-s">' + d.rounds + ' rounds · ' + d.calls + ' model calls · ' +
        Number(d.tokens).toLocaleString() + ' tokens · ' + d.agents + ' agents</div>' +
        '<button class="btn btn-primary" onclick="document.getElementById(\'btn-deliv\').click()">Download the deliverable</button> ' +
        '<button class="btn" onclick="document.getElementById(\'btn-transcript\').click()">Full transcript</button>' +
        '</div>'));
      $('#fb-text').disabled = true; $('#fb-send').disabled = true;
      $('#fb-text').placeholder = 'The mission has concluded.';
      scrollToEnd(true);
    },

    error: function (d) { runError(d.message, /checkpoint|interrupted/i.test(d.message)); },
    connection_lost: function (d) { runError(d.message, true); },
  };

  function runError(message, offerResume) {
    S.done = true;
    clearTyping();
    setLive('off');
    var b = el('<div class="banner err-banner">' +
      '<div class="b-t">Run interrupted</div>' +
      '<div class="b-s">' + esc(message) + '</div>' +
      (offerResume ? '<button class="btn" id="btn-ck-resume">⟳ &nbsp;Reattach to the run</button>' : '') +
      '</div>');
    append(b);
    var r = $('#btn-ck-resume', b);
    if (r) r.addEventListener('click', function () {
      // Full clean rebuild via the ?attach= boot path: attaches to the live
      // hub when the engine survived, or resumes from the checkpoint if not.
      location.href = location.pathname + '?attach=' + S.runId;
    });
    scrollToEnd(true);
  }

  /* ------------------------------------------------------- message routing */
  function routeCcu(type, text, round) {
    switch (type) {
      case 'ccu_cast':
        bubble({ who: 'CCU', kind: 'Foundation Briefing', round: round, text: text, rowCls: 'ccu-row' });
        // approval gate (if on) arrives next and clears this; otherwise agent 1 speaks
        if (S.roster.length) setTyping(S.roster[0].name, 'composing the opening thesis…');
        break;
      case 'ccu_directives':
        engineCard({ icon: '🧭', title: 'Round ' + round + ' directives dispatched', text: text });
        if (S.roster.length) setTyping(S.roster[0].name, 'responding to the directives…');
        break;
      case 'ccu_wrapup':
        bubble({ who: 'CCU', kind: 'Audit & Next-Round Agenda', round: round, text: text, rowCls: 'ccu-row' });
        setTyping('CCU', 'recording the round summary…');
        break;
      case 'round_summary':
        engineCard({ icon: '🗜', title: 'Round ' + round + ' — sliding summary', text: text });
        break;
      case 'ccu_final_agenda':
        bubble({ who: 'CCU', kind: 'Final Conclusion Directives', round: round, text: text, rowCls: 'ccu-row' });
        if (S.roster.length) setTyping(S.roster[0].name, 'preparing the closing statement…');
        break;
      case 'da_validation':
        engineCard({
          icon: '😈', title: "Devil's Advocate validation",
          status: /^\s*PASS/i.test(text) ? 'ok' : 'warn', text: text,
        });
        setTyping('CCU', 'running the gap analysis…');
        break;
      case 'gap_analysis':
        engineCard({
          icon: '🔍', title: 'Gap analysis',
          status: /NO CRITICAL GAPS/i.test(text) ? 'ok' : 'warn', text: text,
        });
        setTyping('CCU', 'composing the Final Synthesis & Executive Audit…');
        break;
      case 'final_synthesis':
        bubble({ who: 'CCU', kind: 'Final Synthesis & Executive Audit', round: round, text: text, rowCls: 'synth-row' });
        if (S.roster.length) setTyping(S.roster[0].name, 'delivering the final verdict…');
        break;
      case 'deliverable':
        engineCard({ icon: '📄', title: 'Single-document deliverable composed', text: text });
        setTyping('CCU', 'recording the executive log entry…');
        break;
      case 'log_summary':
        engineCard({ icon: '🧾', title: 'Executive log entry (≤800 chars)', text: text, open: true });
        break;
      case 'context_compacted':
        engineCard({ icon: '🗜', title: 'Context compacted — conversation digests recorded', text: text });
        break;
      default:
        bubble({ who: 'CCU', kind: type.replace(/_/g, ' '), round: round, text: text, rowCls: 'ccu-row' });
    }
  }

  function routeAgent(d) {
    var kind = d.type === 'thesis' ? 'Opening Thesis'
             : d.type === 'final_verdict' ? 'Final Verdict'
             : S.phase === 'final' ? 'Closing Statement' : 'Thesis';
    var verdict = null;
    var text = d.text;
    if (d.type === 'final_verdict') {
      var m = text.match(/^\s*VERDICT\s*:\s*(AGREE|DISAGREE)\s*/i);
      verdict = m ? m[1].toLowerCase() : 'unclear';
      if (m) text = text.slice(m[0].length);
    }
    bubble({ who: d.agent, kind: kind, round: d.round, text: text, verdict: verdict });
    castStatus(d.agent, 'spoke · R' + d.round);

    // hand-off heuristics: next agent types, else the CCU takes over
    var idx = d.index || (S.byName[d.agent] && S.byName[d.agent].index);
    if (idx && idx < S.roster.length) {
      var next = S.roster[idx];
      if (next) setTyping(next.name,
        d.type === 'final_verdict' ? 'delivering the final verdict…'
        : d.round === 1 && d.type === 'thesis' ? 'composing the opening thesis…'
        : 'responding in round ' + d.round + '…');
    } else if (d.type === 'final_verdict') {
      setTyping('CCU', 'composing the mission deliverable…');
    } else {
      setTyping('CCU', 'auditing the round…');
    }
  }

  /* --------------------------------------------------- resume: replay REST */
  function replayHistory(run) {
    var lastRound = 0;
    (run.messages || []).forEach(function (m) {
      if (m.round_num !== lastRound) {
        lastRound = m.round_num;
        setRound(m.round_num, 'main');
        divider(m.round_num, 'main');
      }
      if (m.agent_name === 'CCU') routeCcu(m.message_type, m.message, m.round_num);
      else if (m.agent_name === 'OPERATOR') sysPill('Cast approved by the operator');
      else if (m.message_type === 'human_feedback') {
        bubble({ who: 'You', kind: 'to ' + m.agent_name, round: m.round_num, text: m.message, rowCls: 'you-row', expand: true });
      } else routeAgent({ agent: m.agent_name, type: m.message_type, text: m.message, round: m.round_num, index: null });
    });
    clearTyping();
    (run.logs || []).slice(-14).forEach(function (l) { feed(l.message); });
    sysPill('— transcript restored —');
  }

  /* ------------------------------------------------------------------ boot */
  // ?attach=<run_id>: rejoin an existing run (set on every run start, so a
  // reloaded mobile tab or a shared link lands back inside the mission).
  var attachId = new URLSearchParams(location.search).get('attach');
  if (attachId) joinRun(attachId);
})();
