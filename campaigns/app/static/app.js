(function () {
  'use strict';
  var csrf = (document.querySelector('meta[name=csrf]') || {}).content || '';

  // Confirmation for destructive actions: data-confirm on a form or a button.
  document.addEventListener('submit', function (event) {
    var form = event.target;
    var message = (event.submitter && event.submitter.dataset.confirm) || form.dataset.confirm;
    if (message && !window.confirm(message)) event.preventDefault();
  });

  // Copy buttons: data-copy="#element".
  document.addEventListener('click', function (event) {
    var button = event.target.closest('[data-copy]');
    if (!button) return;
    var source = document.querySelector(button.dataset.copy);
    if (!source) return;
    var text = source.value || source.textContent;
    var done = function () { var old = button.textContent; button.textContent = 'Copied'; setTimeout(function () { button.textContent = old; }, 1500); };
    if (navigator.clipboard) { navigator.clipboard.writeText(text).then(done, function () {}); }
    else { var range = document.createRange(); range.selectNodeContents(source); var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range); }
  });

  // Email finder: "Use" puts the address into the add form.
  document.addEventListener('click', function (event) {
    var button = event.target.closest('[data-fill]');
    var field = document.getElementById('add-email');
    if (!button || !field) return;
    field.value = button.dataset.fill;
    field.focus();
  });

  // Checkboxes that show or hide a field: data-toggle="#field" (shown when unchecked).
  document.querySelectorAll('[data-toggle]').forEach(function (box) {
    var target = document.querySelector(box.dataset.toggle);
    if (!target) return;
    box.addEventListener('change', function () { target.classList.toggle('hide', box.checked); });
  });

  // Campaign editor: one step at a time. Without JavaScript every step shows as one long page.
  var stepNav = document.querySelector('.steps[data-tabs]');
  if (stepNav) {
    var stepLinks = Array.prototype.slice.call(stepNav.querySelectorAll('a'));
    var order = stepLinks.map(function (a) { return a.hash.slice(1); });
    var panes = document.querySelectorAll('[data-pane]');
    var nextButton = document.querySelector('[data-next]');
    var memory = 'step:' + location.pathname;
    var current = order[0];
    var showStep = function (id) {
      if (order.indexOf(id) < 0) {
        var target = id && document.getElementById(id);
        var pane = target && target.closest('[data-pane]');
        id = pane ? pane.dataset.pane : order[0];
      }
      current = id;
      panes.forEach(function (p) { p.classList.toggle('hide', p.dataset.pane !== id); });
      stepLinks.forEach(function (a) { a.classList.toggle('on', a.hash === '#' + id); });
      if (nextButton) nextButton.classList.toggle('hide', id === order[order.length - 1]);
      try { sessionStorage.setItem(memory, id); } catch (e) { /* private mode */ }
    };
    stepLinks.forEach(function (a) {
      a.addEventListener('click', function (event) {
        event.preventDefault();
        history.replaceState(null, '', a.hash);
        showStep(a.hash.slice(1));
        window.scrollTo({ top: 0 });
      });
    });
    if (nextButton) nextButton.addEventListener('click', function () {
      var id = order[order.indexOf(current) + 1];
      history.replaceState(null, '', '#' + id);
      showStep(id);
      window.scrollTo({ top: 0 });
    });
    document.addEventListener('invalid', function (event) {
      var pane = event.target.closest && event.target.closest('[data-pane]');
      if (pane && pane.dataset.pane !== current) showStep(pane.dataset.pane);
    }, true);
    var start = location.hash.slice(1);
    if (!start) { try { start = sessionStorage.getItem(memory); } catch (e) { start = ''; } }
    showStep(start);
    if (location.hash) {
      var anchor = document.getElementById(location.hash.slice(1));
      if (anchor && order.indexOf(location.hash.slice(1)) < 0) anchor.scrollIntoView();
    }
  }

  // Campaign editor: live preview and merge-tag insertion.
  var html = document.getElementById('html');
  var preview = document.getElementById('preview');
  if (html && preview) {
    var timer = null;
    var render = function () {
      var body = html.value;
      if (!/<html[\s>]/i.test(body)) {
        body = '<!doctype html><html><body style="margin:0;padding:16px;font-family:Arial,Helvetica,sans-serif;' +
          'font-size:15px;line-height:1.5;color:#222">' + body + '</body></html>';
      }
      preview.srcdoc = body;
    };
    html.addEventListener('input', function () { clearTimeout(timer); timer = setTimeout(render, 250); });
    render();
    document.querySelectorAll('.tags code').forEach(function (tag) {
      tag.addEventListener('click', function () {
        var start = html.selectionStart, end = html.selectionEnd;
        html.value = html.value.slice(0, start) + tag.textContent + html.value.slice(end);
        html.focus();
        html.selectionStart = html.selectionEnd = start + tag.textContent.length;
        render();
      });
    });
  }

  // Settings: show the fields of the selected AI provider, load model lists.
  var aiForm = document.getElementById('ai-form');
  if (aiForm) {
    var provider = document.getElementById('ai_provider');
    var status = document.getElementById('models-status');
    var show = function () {
      aiForm.querySelectorAll('[data-provider]').forEach(function (row) {
        row.style.display = row.dataset.provider === provider.value ? '' : 'none';
      });
    };
    provider.addEventListener('change', show);
    show();
    document.getElementById('load-models').addEventListener('click', function () {
      var name = provider.value;
      status.textContent = 'Loading…';
      fetch(aiForm.dataset.modelsUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf, 'X-Requested-With': 'fetch' },
        body: JSON.stringify({ provider: name, key: document.getElementById('ai_key_' + name).value })
      }).then(function (reply) { return reply.json(); }).then(function (data) {
        if (data.error) { status.textContent = data.error; return; }
        var catalog = data.catalog || data.models.map(function (id) { return { id: id, name: id }; });
        var price = function (m) {
          if (m.free) return 'free';
          if (m.input == null || m.output == null) return '';
          return '$' + m.input + ' / $' + m.output;
        };
        [document.getElementById('models-' + name), document.getElementById('models-writer')].forEach(function (list) {
          if (!list) return;
          list.innerHTML = '';
          catalog.forEach(function (m) {
            var option = document.createElement('option');
            option.value = m.id;
            option.label = (price(m) ? price(m) + ' · ' : '') + m.name;
            list.appendChild(option);
          });
        });
        var input = document.getElementById('ai_model_' + name);
        var recommended = data.recommended || {};
        var cheapAgent = (recommended.agent || [])[0];
        if (!input.value) input.value = cheapAgent ? cheapAgent.id : (data.suggested || '');
        var picks = document.getElementById('model-picks');
        var any = false;
        picks.querySelectorAll('[data-picks]').forEach(function (row) {
          row.innerHTML = '';
          (recommended[row.dataset.picks] || []).forEach(function (m) {
            any = true;
            var target = row.dataset.target === 'agent' ? input : document.getElementById(row.dataset.target);
            var chip = document.createElement('button');
            chip.type = 'button';
            chip.className = 'model-pick' + (target.value === m.id ? ' on' : '');
            var perThousand = m.input != null ? (1.5 * m.input + 0.15 * m.output) : null;
            chip.innerHTML = '<strong></strong><span></span>';
            chip.querySelector('strong').textContent = m.name.replace(/^[^:]+:\s*/, '');
            chip.querySelector('span').textContent = price(m) + (perThousand != null ? ' · ≈ $' + perThousand.toFixed(2) + ' per 1,000 emails' : '');
            chip.addEventListener('click', function () {
              target.value = m.id;
              row.querySelectorAll('.model-pick').forEach(function (c) { c.classList.remove('on'); });
              chip.classList.add('on');
            });
            row.appendChild(chip);
          });
        });
        picks.hidden = !any;
        status.textContent = data.models.length + ' models available.' + (any ? ' Good-value picks below, or type to search all.' : ' Start typing in the Model box to pick one.');
      }).catch(function () { status.textContent = 'Could not load the model list.'; });
    });
  }

  // Long values show three lines; a click shows all of it.
  document.querySelectorAll('.clamp').forEach(function (el) {
    el.addEventListener('click', function () { el.classList.toggle('open'); });
  });

  // A page with work running in the background reloads itself until it is done.
  var refresh = document.querySelector('[data-refresh]');
  if (refresh) setTimeout(function () { window.location.reload(); }, (parseInt(refresh.dataset.refresh, 10) || 5) * 1000);

  // The sidebar folds down to icons; the choice is kept in a cookie so the next page starts that way.
  var navToggle = document.getElementById('nav-toggle');
  var navOpen = document.getElementById('nav-open');
  if (navToggle && navOpen) {
    var setNav = function (hidden) {
      document.documentElement.classList.toggle('nav-collapsed', hidden);
      document.cookie = 'nav=' + (hidden ? 'collapsed' : 'open') + '; path=/; max-age=31536000; SameSite=Lax';
      navOpen.setAttribute('aria-expanded', hidden ? 'false' : 'true');
      (hidden ? navOpen : navToggle).focus();
    };
    navToggle.addEventListener('click', function () { setNav(true); });
    navOpen.addEventListener('click', function () { setNav(false); });
  }

  // Settings: one section at a time; links like settings#leadtools open the right one.
  var settingsNav = document.querySelector('[data-settings-tabs]');
  if (settingsNav) {
    var panes = document.querySelectorAll('[data-settings-pane]');
    var tabs = settingsNav.querySelectorAll('a');
    var showPane = function (hash) {
      var target = hash && document.getElementById(hash.slice(1));
      var pane = target && (target.hasAttribute('data-settings-pane') ? target : target.closest('[data-settings-pane]'));
      pane = pane || panes[0];
      panes.forEach(function (p) { p.hidden = p !== pane; });
      tabs.forEach(function (a) { a.classList.toggle('on', a.hash === '#' + pane.id); });
      if (target && target.tagName === 'DETAILS') { target.open = true; target.scrollIntoView({ block: 'start' }); }
    };
    settingsNav.addEventListener('click', function (event) {
      var link = event.target.closest('a');
      if (!link) return;
      event.preventDefault();
      history.replaceState(null, '', link.hash);
      showPane(link.hash);
    });
    window.addEventListener('hashchange', function () { showPane(location.hash); });
    showPane(location.hash);
  }

  // Campaign step 3: the controls for the chosen kind of personal email.
  var modeChoices = document.querySelector('[data-mode-choices]');
  if (modeChoices) {
    var showMode = function () {
      var picked = modeChoices.querySelector('input[name=ai_mode]:checked');
      var value = picked ? picked.value : '';
      document.querySelectorAll('.mode-box[data-when-mode]').forEach(function (box) {
        box.hidden = box.dataset.whenMode.split(' ').indexOf(value) === -1;
      });
    };
    modeChoices.addEventListener('change', showMode);
  }

  // Write an email: the preview shows it the way the recipient's mail program will.
  var composeText = document.getElementById('compose-text');
  if (composeText) {
    var frame = document.getElementById('compose-preview');
    var pvTimer = null;
    var renderPreview = function () {
      fetch(composeText.dataset.previewUrl, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf, 'X-Requested-With': 'fetch' },
        body: JSON.stringify({ text: composeText.value, sender: (document.getElementById('c-from') || {}).value })
      }).then(function (reply) { return reply.json(); }).then(function (data) { frame.srcdoc = data.html; });
    };
    composeText.addEventListener('input', function () { clearTimeout(pvTimer); pvTimer = setTimeout(renderPreview, 300); });
    var fromSelect = document.getElementById('c-from');
    if (fromSelect) fromSelect.addEventListener('change', renderPreview);
    var mirror = function (inputId, targetId, empty) {
      var input = document.getElementById(inputId), target = document.getElementById(targetId);
      if (input && target) input.addEventListener('input', function () { target.textContent = input.value || empty; });
    };
    mirror('c-subject', 'pv-subject', 'Subject');
    mirror('c-to', 'pv-to', '…');
    renderPreview();
  }

  // The agent's list of chats opens beside the conversation; the choice is remembered.
  var historyToggle = document.getElementById('history-toggle');
  var historyPanel = document.getElementById('history');
  if (historyToggle && historyPanel) {
    var setHistory = function (open) {
      historyPanel.hidden = !open;
      document.getElementById('agent-chat').classList.toggle('history-open', open);
      historyToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      try { localStorage.setItem('agentHistory', open ? '1' : '0'); } catch (e) { /* private mode */ }
    };
    historyToggle.addEventListener('click', function () { setHistory(historyPanel.hidden); });
    document.getElementById('history-close').addEventListener('click', function () { setHistory(false); });
    var remembered = null;
    try { remembered = localStorage.getItem('agentHistory'); } catch (e) { /* private mode */ }
    if (remembered === '1') setHistory(true);
  }

  // Agent chat. A message starts a turn on the server; the page then asks every
  // second what the agent has done so far, until it has answered.
  var composer = document.getElementById('composer');
  var chat = document.getElementById('chat');
  if (composer && chat) {
    var log = document.getElementById('log');
    var turn = document.getElementById('turn');
    var prompt = document.getElementById('prompt');
    var button = composer.querySelector('button.primary');
    var attach = document.getElementById('attach');
    var live = document.getElementById('live');
    var activity = document.getElementById('activity');
    var errorBox = document.getElementById('chat-error');
    var attached = document.getElementById('attached');
    var attachment = null;
    var after = parseInt(chat.dataset.after || '0', 10);
    var timer = null;
    var headers = { 'X-CSRF-Token': csrf, 'X-Requested-With': 'fetch' };
    var nearBottom = function () { return log.scrollHeight - log.scrollTop - log.clientHeight < 120; };
    var toBottom = function () { log.scrollTop = log.scrollHeight; };
    var busy = function (on) {
      prompt.disabled = button.disabled = on;
      live.hidden = !on;
      if (on) errorBox.hidden = true;
    };
    var showError = function (text) {
      document.getElementById('error-text').textContent = text;
      errorBox.hidden = false;
    };
    var openSteps = function () {
      var open = {};
      turn.querySelectorAll('details.agent-steps[open]').forEach(function (d) { open[d.dataset.id] = true; });
      return open;
    };
    var refreshApprovals = function () {
      var slot = document.getElementById('approvals-slot');
      if (!slot) return;
      fetch(window.location.href, { headers: { 'X-Requested-With': 'fetch' } })
        .then(function (reply) { return reply.text(); })
        .then(function (page) {
          var fresh = new DOMParser().parseFromString(page, 'text/html').getElementById('approvals-slot');
          if (fresh) slot.innerHTML = fresh.innerHTML;
        });
    };
    var poll = function () {
      fetch(chat.dataset.poll + '?after=' + after, { headers: headers })
        .then(function (reply) { return reply.json(); })
        .then(function (data) {
          var stick = nearBottom(), open = openSteps();
          if (data.html.trim() !== turn.dataset.html) {
            turn.dataset.html = data.html.trim();
            turn.innerHTML = data.html;
            turn.querySelectorAll('details.agent-steps').forEach(function (d) { if (open[d.dataset.id]) d.open = true; });
          }
          if (data.activity) activity.textContent = data.activity;
          if (stick) toBottom();
          if (data.status === 'running') { timer = setTimeout(poll, 1000); return; }
          busy(false);
          if (data.status === 'error') showError(data.error || 'Something went wrong.');
          if (data.pending) refreshApprovals();
          var offerCard = document.getElementById('offer-live');   // the offer assistant's live card
          if (offerCard) fetch(offerCard.dataset.url, { headers: headers })
            .then(function (reply) { return reply.text(); }).then(function (html) { offerCard.innerHTML = html; });
          prompt.focus();
        })
        .catch(function () { timer = setTimeout(poll, 3000); });   // keep trying through a network hiccup
    };
    var settle = function () {
      // Earlier turns move out of the live area so the next poll only redraws the new one.
      while (turn.firstChild) log.insertBefore(turn.firstChild, turn);
      turn.dataset.html = '';
    };
    var send = function (text) {
      var suggest = document.getElementById('suggest');
      if (!text && !attachment) return;
      busy(true);
      fetch(composer.dataset.url, {
        method: 'POST', headers: Object.assign({ 'Content-Type': 'application/json' }, headers),
        body: JSON.stringify({ text: text, attachment: attachment })
      }).then(function (reply) {
        return reply.json().catch(function () { return { error: 'The server did not answer (HTTP ' + reply.status + ').' }; });
      }).then(function (data) {
        if (data.error && !data.after) { busy(false); showError(data.error); return; }
        if (suggest) suggest.remove();
        settle();
        turn.insertAdjacentHTML('beforebegin', data.html);
        after = data.after;
        prompt.value = '';
        prompt.style.height = 'auto';
        attachment = null;
        attached.hidden = true;
        toBottom();
        poll();
      }).catch(function () {
        busy(false);
        showError('Could not reach the server. Check your connection and try again.');
      });
    };
    composer.addEventListener('submit', function (event) {
      event.preventDefault();
      send(prompt.value.trim());
    });
    prompt.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); composer.requestSubmit(); }
    });
    prompt.addEventListener('input', function () {
      prompt.style.height = 'auto';
      prompt.style.height = Math.min(prompt.scrollHeight, 220) + 'px';
    });
    document.querySelectorAll('#suggest button').forEach(function (chip) {
      chip.addEventListener('click', function () {
        if (prompt.disabled) return;
        var text = chip.textContent;
        if (/\s$/.test(text)) { prompt.value = text; prompt.focus(); return; }   // needs the rest from you
        send(text.trim());
      });
    });
    document.getElementById('stop').addEventListener('click', function () {
      activity.textContent = 'Stopping';
      fetch(chat.dataset.stop, { method: 'POST', headers: headers });
    });
    document.getElementById('retry').addEventListener('click', function () {
      busy(true);
      fetch(chat.dataset.retry, { method: 'POST', headers: headers })
        .then(function (reply) { return reply.json(); })
        .then(function (data) {
          if (data.error) { busy(false); showError(data.error); return; }
          poll();
        });
    });
    // Leads attached in the chat become a list; the next message tells the agent about it.
    if (attach) attach.addEventListener('change', function () {
      var file = attach.files[0];
      attach.value = '';
      if (!file) return;
      if (!window.confirm('Do you have a lawful reason to email the people in "' + file.name + '" (for example a ' +
                          'business interest, or they agreed to hear from you)?')) return;
      var body = new FormData();
      body.append('file', file);
      body.append('consent', 'yes');
      document.getElementById('attached-name').textContent = 'Uploading ' + file.name + '…';
      attached.hidden = false;
      fetch(composer.dataset.upload, { method: 'POST', body: body, headers: headers })
        .then(function (reply) {
          return reply.json().catch(function () { return { error: 'Upload failed (HTTP ' + reply.status + ').' }; });
        }).then(function (data) {
          if (data.error) { attached.hidden = true; showError(data.error); return; }
          attachment = { list_id: data.list_id, file: data.file };
          document.getElementById('attached-name').textContent = data.file + ' · ' + data.added + ' with email' +
            (data.needs_email ? ', ' + data.needs_email + ' without' : '');
          prompt.focus();
        }).catch(function () {
          attached.hidden = true;
          showError('Upload failed. Check your connection and try again.');
        });
    });
    document.getElementById('attached-remove').addEventListener('click', function () {
      attachment = null;
      attached.hidden = true;
    });
    toBottom();
    turn.dataset.html = turn.innerHTML.trim();
    if (chat.dataset.status === 'running') {   // a turn still going from before the page loaded
      busy(true);
      poll();
    }
  }

  // Adding an email account: a provider fills in its servers.
  var accountForm = document.querySelector('[data-provider-form]');
  if (accountForm) {
    var field = function (name) { return accountForm.elements[name]; };
    var applyProvider = function (radio) {
      var d = radio.dataset;
      field('smtp_host').value = d.smtpHost;
      field('smtp_port').value = d.smtpPort;
      field('smtp_tls').value = d.smtpTls;
      field('imap_host').value = d.imapHost;
      field('imap_port').value = d.imapPort;
      field('daily_limit').value = d.limit;
      accountForm.querySelector('[data-provider-help]').textContent = d.help;
      var custom = radio.value === 'other' || radio.value === 'server';
      accountForm.querySelector('[data-server-fields]').open = custom;
      // A mailbox on this server needs no password to send; other providers do.
      field('smtp_password').required = radio.value !== 'server';
      accountForm.querySelector('[data-needs-password] label').textContent =
        radio.value === 'google' || radio.value === 'yahoo' ? 'App password' : 'Password';
      field('smtp_password').placeholder = radio.value === 'google' ? '16-letter app password' : '';
    };
    accountForm.querySelectorAll('input[name="provider"]').forEach(function (radio) {
      radio.addEventListener('change', function () { applyProvider(radio); });
      if (radio.checked) applyProvider(radio);
    });
  }

  // Sequence: day labels follow the waits as they are typed.
  var sequence = document.querySelector('[data-sequence]');
  if (sequence) {
    var round = function (n) { return Math.round(n * 10) / 10; };
    var redraw = function () {
      var day = 0;
      var delays = sequence.querySelectorAll('[data-seq-delay]');
      var labels = sequence.querySelectorAll('[data-seq-day]');
      delays.forEach(function (input, i) {
        day += parseFloat(input.value) || 0;
        if (labels[i]) labels[i].textContent = 'day ' + round(day);
      });
      var total = document.querySelector('[data-seq-total]');
      if (total && delays.length) total.textContent = (delays.length + 1) + ' emails over ' + round(day) + ' days';
    };
    sequence.addEventListener('input', redraw);
  }
})();
