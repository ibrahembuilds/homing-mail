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

  // Agent chat.
  var composer = document.getElementById('composer');
  if (composer) {
    var log = document.getElementById('log');
    var prompt = document.getElementById('prompt');
    var button = composer.querySelector('button.primary');
    var attach = document.getElementById('attach');
    var add = function (kind, text, error) {
      var node = document.createElement('div');
      node.className = 'msg ' + kind + (error ? ' error' : '');
      node.textContent = text;
      log.appendChild(node);
      log.scrollTop = log.scrollHeight;
      return node;
    };
    var send = function (text) {
      var suggest = document.getElementById('suggest');
      if (suggest) suggest.remove();
      add('user', text);
      prompt.value = '';
      prompt.disabled = button.disabled = true;
      var waiting = add('thinking', 'Working…');
      fetch(composer.dataset.url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf, 'X-Requested-With': 'fetch' },
        body: JSON.stringify({ text: text })
      }).then(function (reply) {
        return reply.json().catch(function () { return { error: 'The server did not answer (HTTP ' + reply.status + ').' }; });
      }).then(function (data) {
        waiting.remove();
        (data.messages || []).forEach(function (m) {
          add(m.kind, m.kind === 'tool' ? m.summary : m.text, m.error);
        });
        if (data.error) add('note', data.error);
        // New approvals appear in the box at the top of the page.
        if (data.pending) {
          fetch(window.location.href, { headers: { 'X-Requested-With': 'fetch' } })
            .then(function (reply) { return reply.text(); })
            .then(function (page) {
              var fresh = new DOMParser().parseFromString(page, 'text/html').getElementById('approvals-slot');
              if (fresh) document.getElementById('approvals-slot').innerHTML = fresh.innerHTML;
            });
        }
      }).catch(function () {
        waiting.remove();
        add('note', 'Connection lost. Reload the page to see whether the agent finished.');
      }).then(function () {
        prompt.disabled = button.disabled = false;
        prompt.focus();
      });
    };
    composer.addEventListener('submit', function (event) {
      event.preventDefault();
      var text = prompt.value.trim();
      if (text) send(text);
    });
    prompt.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); composer.requestSubmit(); }
    });
    document.querySelectorAll('#suggest button').forEach(function (chip) {
      chip.addEventListener('click', function () { if (!prompt.disabled) send(chip.textContent); });
    });
    var grow = function () {
      prompt.style.height = 'auto';
      prompt.style.height = Math.min(prompt.scrollHeight, 220) + 'px';
    };
    prompt.addEventListener('input', grow);
    // Leads attached in the chat become a list; the agent is then told about it.
    if (attach) attach.addEventListener('change', function () {
      var file = attach.files[0];
      attach.value = '';
      if (!file) return;
      if (!window.confirm('Do you have a lawful reason to email the people in "' + file.name + '" (for example a ' +
                          'business interest, or they agreed to hear from you)?')) return;
      var body = new FormData();
      body.append('file', file);
      body.append('consent', 'yes');
      var waiting = add('thinking', 'Uploading ' + file.name + '…');
      prompt.disabled = button.disabled = true;
      fetch(composer.dataset.upload, {
        method: 'POST', body: body, headers: { 'X-CSRF-Token': csrf, 'X-Requested-With': 'fetch' }
      }).then(function (reply) {
        return reply.json().catch(function () { return { error: 'Upload failed (HTTP ' + reply.status + ').' }; });
      }).then(function (data) {
        waiting.remove();
        prompt.disabled = button.disabled = false;
        if (data.error) { add('note', data.error); return; }
        var ask = prompt.value.trim() || 'Read these leads and suggest a personal first email for them, using news about their companies.';
        send(ask + '\n\n[Uploaded ' + file.name + ': list "' + data.name + '" (list_id ' + data.list_id + '), ' +
             data.added + ' leads' + (data.invalid ? ', ' + data.invalid + ' invalid rows skipped' : '') +
             (data.columns && data.columns.length ? '; columns: ' + data.columns.join(', ') : '') + ']');
      }).catch(function () {
        waiting.remove();
        prompt.disabled = button.disabled = false;
        add('note', 'Upload failed. Check your connection and try again.');
      });
    });
    log.scrollTop = log.scrollHeight;
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
