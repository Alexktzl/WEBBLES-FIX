
// ── Navigation ──
function openMain(mode) {
  document.getElementById('screen-start').classList.remove('active');
  document.getElementById('screen-main').classList.add('active');
  setMode(mode);
}
function goStart() {
  document.getElementById('screen-main').classList.remove('active');
  document.getElementById('screen-start').classList.add('active');
}

// ── Mode ──
function setMode(mode) {
  const badge = document.getElementById('mode-badge-left');
  if (mode === 'fix') {
    badge.className = 'mode-badge mode-fix';
    badge.textContent = '🔧 FIX';
  } else if (mode === 'create') {
    badge.className = 'mode-badge mode-create';
    badge.textContent = '✨ CREATE';
  } else {
    // chat — нейтральный
    badge.className = 'mode-badge mode-fix';
    badge.textContent = '💬 CHAT';
  }
  // Прокинуть режим в бэкенд чата, чтобы Create-режим получил write_file/make_dir.
  if (window.eel && typeof eel.chat_set_mode === 'function') {
    eel.chat_set_mode(mode || 'chat');
  }
}

// ── Chat overlay ──
let overlayOpen = false;
function toggleChatOverlay() {
  overlayOpen ? closeChatOverlay() : openChatOverlay();
}
function openChatOverlay() {
  overlayOpen = true;
  document.getElementById('chat-overlay').classList.add('open');
  document.getElementById('overlay-backdrop').style.display = 'block';
}
function closeChatOverlay() {
  overlayOpen = false;
  document.getElementById('chat-overlay').classList.remove('open');
  document.getElementById('overlay-backdrop').style.display = 'none';
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') { closeChatOverlay(); closeAllModals(); } });

// ── Filter chats ──
function filterChats(q) {
  document.querySelectorAll('#chat-list .chat-item-row').forEach(row => {
    const name = row.querySelector('.ci-name').textContent.toLowerCase();
    row.style.display = name.includes(q.toLowerCase()) ? '' : 'none';
  });
}

// ── Live: множественные чаты ────────────────────────────────────────
// chat_list / chat_new / chat_switch / chat_delete / chat_history — Eel API.
function escapeHtmlLite(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function refreshChatList() {
  if (!(window.eel && typeof eel.chat_list === 'function')) return;
  eel.chat_list()(function (chats) {
    var list = document.getElementById('chat-list');
    if (!list) return;
    if (!chats || !chats.length) {
      list.innerHTML = '<div style="padding:10px 14px;color:var(--txt3);font-size:11px">' +
        'Нет чатов — нажми «+ Новый», чтобы создать первый.</div>';
      return;
    }
    var html = '';
    for (var i = 0; i < chats.length; i++) {
      var c = chats[i];
      var icon = c.mode === 'create' ? '✨' : (c.mode === 'fix' ? '🔧' : '💬');
      var when = (c.ts || '').replace('T', ' ').slice(0, 16);
      var activeCls = c.active ? ' active' : '';
      var msgsTag = (typeof c.messages === 'number' && c.messages > 0)
        ? (' · ' + c.messages + ' сообщ.') : '';
      html +=
        '<div class="chat-item-row' + activeCls + '" data-id="' + escapeHtmlLite(c.id) + '">' +
          '<span class="ci-icon">' + icon + '</span>' +
          '<div class="ci-info">' +
            '<div class="ci-name">' + escapeHtmlLite(c.name) + '</div>' +
            '<div class="ci-date">' + escapeHtmlLite(when) + escapeHtmlLite(msgsTag) + '</div>' +
          '</div>' +
          '<button class="ch-btn" data-del title="Удалить чат" ' +
            'style="margin-left:auto;background:transparent;border:none;color:var(--txt3);font-size:14px;padding:2px 6px">✕</button>' +
        '</div>';
    }
    list.innerHTML = html;
    // Клик по строке — переключить, клик по крестику — удалить.
    list.querySelectorAll('.chat-item-row').forEach(function (row) {
      row.addEventListener('click', function (ev) {
        var id = row.getAttribute('data-id');
        if (!id) return;
        if (ev.target && ev.target.matches('[data-del]')) {
          if (!confirm('Удалить чат и его историю?')) return;
          ev.stopPropagation();
          eel.chat_delete(id)(function () {
            refreshChatList();
            loadActiveChatHistory();
          });
          return;
        }
        eel.chat_switch(id)(function (ok) {
          if (!ok) return;
          refreshChatList();
          loadActiveChatHistory();
        });
      });
    });
  });
}
function loadActiveChatHistory() {
  if (!(window.eel && typeof eel.chat_history === 'function')) return;
  eel.chat_history('')(function (items) {
    var msgs = document.getElementById('chat-messages');
    if (!msgs) return;
    // Снести всё кроме typing-indicator и msg-fixed-каркаса.
    Array.prototype.slice.call(msgs.querySelectorAll('.msg-row')).forEach(function (r) {
      if (r.id !== 'typing-indicator' && r.id !== 'msg-fixed') r.remove();
    });
    if (!items || !items.length) return;
    items.forEach(function (h) {
      var role = h.role;
      var content = h.content || '';
      var div = document.createElement('div');
      div.className = 'msg-row ' + (role === 'user' ? 'user' : 'agent');
      var safe = escapeHtmlLite(content).replace(/\n/g, '<br>');
      if (role === 'user') {
        div.innerHTML =
          '<div class="msg-avatar avatar-user">А</div>' +
          '<div class="msg-bubble"><div class="msg-meta">Вы</div>' +
          '<div class="msg-text">' + safe + '</div></div>';
      } else {
        div.innerHTML =
          '<div class="msg-avatar avatar-agent">W</div>' +
          '<div class="msg-bubble"><div class="msg-meta">Webbles Agent</div>' +
          '<div class="msg-text">' + safe + '</div></div>';
      }
      var typing = document.getElementById('typing-indicator');
      if (typing) msgs.insertBefore(div, typing); else msgs.appendChild(div);
    });
  });
}
window.refreshChatList = refreshChatList;
window.loadActiveChatHistory = loadActiveChatHistory;

// ── File block toggle ──
function toggleFileBlock(header) {
  const block = header.parentElement;
  const code = block.querySelector('.fb-code');
  const footer = block.querySelector('.fb-footer');
  const chevron = header.querySelector('.fb-chevron');
  const open = code.classList.toggle('open');
  footer.classList.toggle('open', open);
  chevron.classList.toggle('open', open);
}

// ── Collapse blocks ──
function toggleCollapse(btn, id) {
  const el = document.getElementById(id);
  const open = el.classList.toggle('open');
  btn.textContent = (open ? '▾ ' : '▸ ') + btn.textContent.slice(2);
}

// ── Apply fix ──
function applyFix() {
  const fixed = document.getElementById('msg-fixed');
  fixed.style.display = 'flex';
  fixed.scrollIntoView({ behavior: 'smooth', block: 'end' });
  // Update task 4→done, 5→running
  const statuses = document.querySelectorAll('.task-status');
  if (statuses[3]) { statuses[3].className='task-status ts-done'; statuses[3].textContent='✓ Готово'; }
  if (statuses[4]) { statuses[4].className='task-status ts-running'; statuses[4].textContent='⟳ Идёт'; }
}

// ── Send message ──
function sendMessage() {
  const ta = document.querySelector('.chat-textarea');
  const text = ta.value.trim();
  if (!text) return;
  // Обрабатываем /команды
  if (text.startsWith('/') && typeof handleChatCommand === 'function' && handleChatCommand(text)) {
    ta.value = ''; ta.style.height = ''; return;
  }
  addUserMessage(text);
  ta.value = '';
  ta.style.height = '';
  showTyping();
  // Если открыто через studio.py (Eel) — реально шлём в LLM-чат.
  if (window.eel && typeof eel.chat_send === 'function') {
    eel.chat_send(text)(function (reply) {
      hideTyping();
      addAgentMessage(reply || '(пустой ответ)');
      // В Create-режиме: если LLM создал файлы — показываем кнопку запуска Fix
      if (_CHAT_MODE === 'create' && window._createProjectPath) {
        showCreateRunFixBtn();
      }
    });
  }
}
function addAgentMessage(text) {
  var msgs = document.getElementById('chat-messages');
  if (!msgs) return;
  var div = document.createElement('div');
  div.className = 'msg-row agent';
  var ts = (new Date()).toTimeString().slice(0,5);
  var safe = String(text || '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\n/g, '<br>');
  div.innerHTML = '<div class="msg-avatar avatar-agent">W</div>' +
    '<div class="msg-bubble"><div class="msg-meta">Webbles Agent &nbsp;·&nbsp; ' + ts + '</div>' +
    '<div class="msg-text">' + safe + '</div></div>';
  var typing = document.getElementById('typing-indicator');
  if (typing) msgs.insertBefore(div, typing); else msgs.appendChild(div);
  div.scrollIntoView({ behavior: 'smooth', block: 'end' });
}
function addUserMessage(text) {
  const msgs = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.className = 'msg-row user';
  var ts = (new Date()).toTimeString().slice(0,5);
  div.innerHTML =
    '<div class="msg-avatar avatar-user">А</div>' +
    '<div class="msg-bubble"><div class="msg-meta">Вы &nbsp;·&nbsp; ' + ts + '</div>' +
    '<div class="msg-text">' + escapeHtmlLite(text) + '</div></div>';
  msgs.insertBefore(div, document.getElementById('typing-indicator'));
  div.scrollIntoView({ behavior: 'smooth', block: 'end' });
}
function showTyping() {
  const t = document.getElementById('typing-indicator');
  if (!t) return;
  t.style.display = 'flex';
  t.scrollIntoView({ behavior: 'smooth', block: 'end' });
  // Live-режим скрывает спиннер по факту ответа (см. sendMessage), таймер —
  // только демо-режим без агента.
  if (!(window.eel && typeof eel.chat_send === 'function')) {
    setTimeout(hideTyping, 2000);
  }
}
function hideTyping() {
  document.getElementById('typing-indicator').style.display = 'none';
}
function handleInput(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
}
function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 100) + 'px';
}

// ── New chat ──
// Новый чат: показываем мини-диалог выбора режима (fix / create)
function newChatModal() {
  document.getElementById('modal-new-chat').classList.add('open');
}

function createChatMode(mode) {
  closeModal('modal-new-chat');
  var modeLabel = {fix:'Fix', create:'Create', chat:'Chat'}[mode] || 'Чат';
  var name = modeLabel + ' ' + nowTime();

  if (!(window.eel && typeof eel.chat_new === 'function')) {
    clearChatMessages();
    var tEl = document.getElementById('chat-title');
    if (tEl) tEl.textContent = name;
    if (mode === 'create' && window.agentChat) agentChat(createWelcomeMsg());
    hideSuggestNext(mode);
    return;
  }

  eel.chat_new(name, mode)(function(id) {
    if (!id) return;
    _CHAT_MODE = mode;
    var tEl = document.getElementById('chat-title');
    if (tEl) tEl.textContent = name;
    window.refreshChatList && window.refreshChatList();
    clearChatMessages();
    hideSuggestNext(mode);

    if (mode === 'create') {
      window._createProjectPath = null;
      setTimeout(function() { initCreateProject(); }, 200);
    } else {
      window.loadActiveChatHistory && window.loadActiveChatHistory();
    }
  });
}

function createWelcomeMsg() {
  return '✨ <b>Режим Create</b> — опишите, что хотите создать: язык, архитектуру, цель проекта.';
}

// Инициализация Create-проекта: запрашиваем имя → создаём папку → показываем welcome
function initCreateProject() {
  var pname = prompt('Название нового проекта (латиница, без пробелов):', 'my_project');
  if (!pname) { pname = 'my_project'; }
  pname = pname.replace(/\s+/g, '_').replace(/[^\w-]/g, '').slice(0, 40) || 'my_project';

  if (window.eel && typeof eel.create_init_project === 'function') {
    eel.create_init_project(pname)(function(res) {
      if (res && res.ok) {
        window._createProjectPath = res.path;
        var shortPath = res.path.replace(/\\/g, '/').split('/').slice(-2).join('/');
        agentChat(
          '✨ <b>Режим Create — ' + escapeHtml(pname) + '</b><br>' +
          'Проект: <code>' + escapeHtml(shortPath) + '</code><br><br>' +
          'Опишите что хотите создать: язык, структуру, зависимости. ' +
          'Я создам все файлы через инструменты, потом можно запустить Fix.'
        );
        // Обновляем project-name в шапке
        var pn = document.getElementById('project-name');
        if (pn) { pn.textContent = pname; pn.style.color = ''; }
        if (window.setMode) window.setMode('create');
        var mb = document.getElementById('mode-badge-left');
        if (mb) mb.style.opacity = '';
      } else {
        agentChat('⚠️ Не удалось создать папку проекта: ' + escapeHtml((res && res.reason) || 'unknown'));
      }
    });
  } else {
    agentChat(createWelcomeMsg());
  }
}

// Запуск Fix на созданном проекте
function createRunFix(lang) {
  lang = lang || 'python';
  if (!(window.eel && typeof eel.create_run_fix === 'function')) {
    agentChat('⚠️ Studio.py недоступен.');
    return;
  }
  agentChat('🚀 Запускаю Fix на созданном проекте…');
  eel.create_run_fix(lang)(function(ok) {
    if (!ok) agentChat('⚠️ Не удалось запустить Fix — проверь что проект инициализирован.');
  });
}

function clearChatMessages() {
  var msgs = document.getElementById('chat-messages');
  if (!msgs) return;
  Array.prototype.slice.call(msgs.querySelectorAll('.msg-row')).forEach(function(r) {
    if (r.id !== 'typing-indicator' && r.id !== 'msg-fixed') r.remove();
  });
}

function nowTime() {
  return (new Date()).toTimeString().slice(0,5);
}

function hideSuggestNext(mode) {
  var el = document.getElementById('suggest-next');
  if (!el) return;
  if (mode === 'create') {
    el.style.display = 'none';
  } else {
    el.style.display = '';
  }
}

// Устаревшие алиасы (на случай вызова из других мест)
function newChat() { newChatModal(); }
function newChatForCurrentProject() { newChatModal(); }

// Текущий режим чата (для показа кнопки Fix в Create)
var _CHAT_MODE = 'chat';

function showCreateRunFixBtn() {
  // Удаляем предыдущую кнопку если была
  var old = document.getElementById('create-run-fix-bar');
  if (old) old.remove();
  var msgs = document.getElementById('chat-messages');
  if (!msgs) return;
  var bar = document.createElement('div');
  bar.id = 'create-run-fix-bar';
  bar.className = 'msg-row agent';
  bar.style.cssText = 'justify-content:center;padding:4px 0';
  bar.innerHTML =
    '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;justify-content:center">' +
      '<button onclick="createRunFix(\'python\')" ' +
        'style="background:var(--green);color:#fff;border:none;border-radius:8px;padding:8px 20px;cursor:pointer;font-size:13px;font-weight:700">▶ Запустить Fix (Python)</button>' +
      '<button onclick="createRunFix(\'javascript\')" ' +
        'style="background:var(--s3);color:var(--txt);border:1px solid var(--border);border-radius:8px;padding:8px 16px;cursor:pointer;font-size:13px">▶ Fix (JS)</button>' +
      '<button onclick="createRunFix(\'rust\')" ' +
        'style="background:var(--s3);color:var(--txt);border:1px solid var(--border);border-radius:8px;padding:8px 16px;cursor:pointer;font-size:13px">▶ Fix (Rust)</button>' +
    '</div>';
  var typing = document.getElementById('typing-indicator');
  if (typing) msgs.insertBefore(bar, typing); else msgs.appendChild(bar);
  bar.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

// ── Кнопки ввода ──
function attachProjectFile() {
  var files = document.querySelectorAll('.tree-file[data-rel]');
  if (!files.length) {
    if (window.agentChat) window.agentChat('📁 Файлы проекта недоступны — сначала откройте проект.');
    return;
  }
  // Показываем список файлов проекта для выбора
  var names = Array.prototype.slice.call(files).slice(0, 40).map(function(f) {
    return f.getAttribute('data-rel');
  });
  var list = names.join('\n');
  var rel = prompt('Выберите файл для вставки в контекст:\n\n' + list);
  if (!rel) return;
  openFileInChat(rel);
}

function mentionFile() {
  var ta = document.querySelector('.chat-textarea');
  if (!ta) return;
  var cur = ta.value;
  ta.value = cur + (cur && !cur.endsWith(' ') ? ' ' : '') + '@';
  ta.focus();
  // При вводе после @ можно ловить подсказки, но пока просто ставим курсор
}

function showCommands() {
  if (window.agentChat) {
    window.agentChat(
      '<b>Доступные команды:</b><br>' +
      '<code>/fix</code> — запустить починку<br>' +
      '<code>/review</code> — показать очередь ревью<br>' +
      '<code>/clear</code> — очистить чат<br>' +
      '<code>/settings</code> — открыть настройки<br>' +
      '<code>/files</code> — список файлов проекта'
    );
  }
}

function openFileInChat(rel) {
  if (!rel) return;
  if (window.eel && typeof eel.chat_file_diff === 'function') {
    eel.chat_file_diff(rel)(function(snap) {
      if (!snap) return;
      // Показываем полное содержимое файла в чате (before = текущее состояние)
      var content = (snap.before != null) ? snap.before : '';
      if (!content && snap.missing) {
        if (window.agentChat) window.agentChat('📄 <code>' + escapeHtml(rel) + '</code> — файл ещё не был в прогоне, снимка нет.');
        return;
      }
      // Используем renderFileLines если доступна, иначе простой pre
      var msgs = document.getElementById('chat-messages');
      if (!msgs) return;
      var row = document.createElement('div');
      row.className = 'msg-row agent';
      var linesHtml = typeof renderFileLines === 'function'
        ? renderFileLines(content)
        : '<pre style="font-size:11px;white-space:pre;overflow-x:auto">' + escapeHtml(content) + '</pre>';
      row.innerHTML =
        '<div class="msg-avatar avatar-agent">W</div>' +
        '<div class="msg-bubble" style="max-width:100%;width:100%;min-width:0;flex:1 1 auto">' +
          '<div class="msg-meta">Файл · ' + (new Date()).toTimeString().slice(0,5) + '</div>' +
          '<div style="margin-top:6px">' +
            '<div style="padding:4px 10px;background:var(--s3);border-radius:6px 6px 0 0;font-size:11px;font-weight:600;color:var(--txt2)">' +
              '📄 ' + escapeHtml(rel) + '</div>' +
            '<div class="diff-col" style="max-height:480px;border:1px solid var(--border);border-top:none;border-radius:0 0 6px 6px">' +
              linesHtml +
            '</div>' +
          '</div>' +
        '</div>';
      var typing = document.getElementById('typing-indicator');
      if (typing) msgs.insertBefore(row, typing); else msgs.appendChild(row);
      row.scrollIntoView({ behavior: 'smooth', block: 'end' });
    });
  } else {
    if (window.agentChat) window.agentChat('📄 Открытие файлов доступно только через studio.py.');
  }
}

// Перехватываем команды в чате
function handleChatCommand(text) {
  var t = text.trim().toLowerCase();
  if (t === '/fix') { startAnalysis(); return true; }
  if (t === '/review') { viewAllErrors(); return true; }
  if (t === '/clear') { clearChatMessages(); return true; }
  if (t === '/settings') { openSettings(); return true; }
  if (t === '/files') {
    var files = document.querySelectorAll('.tree-file[data-rel]');
    var list = Array.prototype.slice.call(files).map(function(f){ return f.getAttribute('data-rel'); }).join('\n');
    if (window.agentChat) window.agentChat('📁 <b>Файлы проекта:</b><br>' + escapeHtml(list || 'нет файлов'));
    return true;
  }
  return false;
}

// ── File select ──
function selectFile(el) {
  document.querySelectorAll('.tree-file').forEach(f => f.classList.remove('active'));
  el.classList.add('active');
  // Клик по файлу → показываем полное содержимое в чате.
  var rel = el.getAttribute('data-rel') || (el.textContent || '').trim();
  if (rel && typeof openFileInChat === 'function') {
    openFileInChat(rel);
  } else if (rel && window.requestFileDiff) {
    window.requestFileDiff(rel);
  }
}

// ── Запрос diff по файлу + рендер блока «Было / Стало» в чат ──
(function () {
  function escapeHtmlLocal(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function basenameLocal(p) { return String(p || '').split(/[\\\/]/).pop(); }

  function renderColumn(content, errorSet, opts) {
    // Возвращает HTML-строки нумерованного кода. Строки, чьи номера есть в
    // errorSet (Set<int>), помечаются классом «removed» — становятся красными.
    // `opts.placeholder` — текст-заглушка, если content пустой (используется
    // для колонки «ПОСЛЕ» когда патч ещё не готов — #3).
    opts = opts || {};
    var lines = (content == null ? '' : String(content)).split(/\r?\n/);
    // Уберём финальный пустой '' от завершающего '\n'.
    if (lines.length && lines[lines.length - 1] === '') lines.pop();
    var html = '';
    for (var i = 0; i < lines.length; i++) {
      var n = i + 1;
      var cls = (errorSet && errorSet.has && errorSet.has(n)) ? ' removed' : '';
      html += '<div class="dc-line' + cls + '">' +
                '<span class="dc-num">' + n + '</span>' +
                '<span class="dc-text">' + escapeHtmlLocal(lines[i] || ' ') + '</span>' +
              '</div>';
    }
    if (!html) {
      var msg = opts.placeholder || '(пусто)';
      html = '<div class="dc-line" style="padding:24px 12px;text-align:center">' +
             '<span class="dc-num">—</span>' +
             '<span class="dc-text" style="color:var(--txt3);font-style:italic;' +
             'white-space:normal">' + escapeHtmlLocal(msg) + '</span></div>';
    }
    return html;
  }

  function renderFileDiff(payload) {
    var msgs = document.getElementById('chat-messages');
    if (!msgs) return;
    payload = payload || {};
    var path = payload.path || '?';
    if (payload.missing) {
      var div = document.createElement('div');
      div.className = 'msg-row agent';
      div.innerHTML =
        '<div class="msg-avatar avatar-agent">W</div>' +
        '<div class="msg-bubble"><div class="msg-meta">Webbles Agent · сейчас</div>' +
        '<div class="msg-text">По файлу <code>' + escapeHtmlLocal(path) +
        '</code> ещё нет снимка «Было/Стало». Запусти Fix-режим — снимок появится '+
        'после первого касания файла движком.</div></div>';
      var typing = document.getElementById('typing-indicator');
      if (typing) msgs.insertBefore(div, typing); else msgs.appendChild(div);
      div.scrollIntoView({ behavior: 'smooth', block: 'end' });
      return;
    }
    var errorLines = payload.error_lines || [];
    var errSet = new Set();
    for (var i = 0; i < errorLines.length; i++) {
      var ln = parseInt(errorLines[i] && errorLines[i].line, 10);
      if (!isNaN(ln) && ln > 0) errSet.add(ln);
    }
    var changes = payload.changes || [];
    // Контекст для кнопок — путь файла; кладём в data-атрибут, чтобы JS-handler
    // взял свежее значение, не замыкая closure-переменную в HTML-строке.
    var row = document.createElement('div');
    row.className = 'msg-row agent';
    // Важно для flex: позволяем строке усохнуть до ширины контейнера. Без
    // min-width:0 длинные `white-space:pre`-строки кода распирают bubble за
    // правую границу чата.
    row.style.width = '100%';
    row.style.minWidth = '0';
    row.style.maxWidth = '100%';
    row.style.alignSelf = 'stretch';
    row.style.boxSizing = 'border-box';
    row.setAttribute('data-diff-path', path);

    var headerErrCount = errSet.size;
    var headerChCount = changes.length;
    var headerInfo = '';
    if (headerErrCount) headerInfo += ' · ошибок: ' + headerErrCount;
    if (headerChCount) headerInfo += ' · вердиктов: ' + headerChCount;

    row.innerHTML =
      '<div class="msg-avatar avatar-agent">W</div>' +
      // bubble: занять всё свободное пространство, но НЕ распирать родителя
      // содержимым (min-width:0 + overflow:hidden + max-width:100%).
      '<div class="msg-bubble" style="max-width:100%;width:100%;min-width:0;flex:1 1 auto;overflow:hidden">' +
        '<div class="msg-meta">Webbles Agent · сейчас</div>' +
        '<div class="msg-text" style="margin-bottom:6px">' +
          'Файл <code>' + escapeHtmlLocal(path) + '</code>' + escapeHtmlLocal(headerInfo) +
        '</div>' +
        '<div class="diff-block">' +
          '<div class="diff-header">' +
            '<span>' + escapeHtmlLocal(basenameLocal(path)) + '</span>' +
            '<div style="display:flex;gap:6px">' +
              '<span class="diff-label diff-label-before">ДО</span>' +
              '<span class="diff-label diff-label-after">ПОСЛЕ</span>' +
            '</div>' +
          '</div>' +
          // diff-cols — flex; обе колонки должны иметь min-width:0, иначе
          // overflow-x:auto на .diff-col не сработает и они вытолкнут bubble.
          '<div class="diff-cols" style="min-width:0">' +
            '<div class="diff-col diff-col-before" style="min-width:0;flex:1 1 0">' +
              '<div class="diff-col-label" style="justify-content:space-between">' +
                '<span>ДО</span>' +
                '<button class="exp-btn" data-explain-before="1" title="Что было не так?">' +
                  '❓ Объясни ошибку</button>' +
              '</div>' +
              renderColumn(payload.before, errSet) +
            '</div>' +
            '<div class="diff-col diff-col-after" style="min-width:0;flex:1 1 0">' +
              '<div class="diff-col-label" style="justify-content:space-between">' +
                '<span>ПОСЛЕ</span>' +
                '<button class="exp-btn" data-explain-after="1" title="Как и почему исправлено?">' +
                  'ℹ️ Объясни фикс</button>' +
              '</div>' +
              // #3: если патч ещё не готов (payload.after пустой/отсутствует) —
              // показываем понятную заглушку вместо просто «(пусто)».
              renderColumn(payload.after, null, {
                placeholder: '⏳ Патч ещё не готов — движок обрабатывает файл. ' +
                             'Подождите завершения текущей итерации, ' +
                             'снимок «После» появится здесь автоматически.'
              }) +
            '</div>' +
          '</div>' +
        '</div>' +
      '</div>';

    // Навешиваем обработчики на кнопки — без inline-onclick, чтобы корректно
    // подцепить замыкание на конкретный путь.
    var bBefore = row.querySelector('button[data-explain-before]');
    var bAfter  = row.querySelector('button[data-explain-after]');
    if (bBefore) bBefore.addEventListener('click', function () { explainBefore(path); });
    if (bAfter)  bAfter.addEventListener('click',  function () { explainAfter(path); });

    var typing = document.getElementById('typing-indicator');
    if (typing) msgs.insertBefore(row, typing); else msgs.appendChild(row);
    row.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }

  function explainBefore(path) {
    if (!(window.eel && typeof eel.chat_explain_before === 'function')) {
      addLocalAgentMsg('Чат недоступен (открыто без studio.py).');
      return;
    }
    addLocalAgentMsg('🤔 Объясняю, что было не так в <code>' +
                     escapeHtmlLocal(basenameLocal(path)) + '</code> …');
    eel.chat_explain_before(path)(function (reply) {
      addLocalAgentMsg(reply || '(пустой ответ)');
    });
  }
  function explainAfter(path) {
    if (!(window.eel && typeof eel.chat_explain_after === 'function')) {
      addLocalAgentMsg('Чат недоступен (открыто без studio.py).');
      return;
    }
    addLocalAgentMsg('🤔 Объясняю фикс <code>' +
                     escapeHtmlLocal(basenameLocal(path)) + '</code> …');
    eel.chat_explain_after(path)(function (reply) {
      addLocalAgentMsg(reply || '(пустой ответ)');
    });
  }
  function addLocalAgentMsg(html) {
    var msgs = document.getElementById('chat-messages');
    if (!msgs) return;
    var div = document.createElement('div');
    div.className = 'msg-row agent';
    div.innerHTML =
      '<div class="msg-avatar avatar-agent">W</div>' +
      '<div class="msg-bubble"><div class="msg-meta">Webbles Agent · сейчас</div>' +
      '<div class="msg-text">' + html + '</div></div>';
    var typing = document.getElementById('typing-indicator');
    if (typing) msgs.insertBefore(div, typing); else msgs.appendChild(div);
    div.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }

  function requestFileDiff(rel) {
    if (!(window.eel && typeof eel.chat_file_diff === 'function')) {
      addLocalAgentMsg('Открой Studio через <code>python studio.py</code> ' +
                       '— тогда клик по файлу покажет «Было/Стало».');
      return;
    }
    eel.chat_file_diff(rel)(function (payload) {
      renderFileDiff(payload);
    });
  }
  window.requestFileDiff = requestFileDiff;
  window.renderFileDiff = renderFileDiff;
})();

// ── Modals ──
function openSettings() {
  document.getElementById('modal-settings').classList.add('open');
  loadSettings();
}

// Вспомогательная: установить toggle-кнопку в состояние on/off
function setToggle(id, value) {
  var btn = document.getElementById(id);
  if (!btn) return;
  btn.classList.toggle('on', !!value);
}

// Вспомогательная: читать состояние toggle-кнопки
function getToggle(id) {
  var btn = document.getElementById(id);
  return btn ? btn.classList.contains('on') : false;
}

// Вспомогательная: безопасно установить значение input/select
function setVal(id, val) {
  var el = document.getElementById(id);
  if (!el || val == null) return;
  el.value = String(val);
}

function getVal(id) {
  var el = document.getElementById(id);
  return el ? el.value : '';
}

function toggleChatLLMFields() {
  var t = document.getElementById('cfg-chat-type');
  var fields = document.getElementById('cfg-chat-cloud-fields');
  if (!t || !fields) return;
  fields.style.display = t.value === 'cloud' ? '' : 'none';
}

function loadSettings() {
  if (!(window.eel && typeof eel.get_config === 'function')) return;
  eel.get_config()(function(cfg) {
    if (!cfg) return;
    var llm = cfg.llm || {};
    var pl = cfg.pipeline || {};
    var tools = cfg.tools || {};
    var chatCfg = cfg.chat_llm || {};

    // LLM tab — основной провайдер (профиль "patch" или первый в списке)
    var prov = {};
    var provs = llm.providers || [];
    for (var i = 0; i < provs.length; i++) {
      if (provs[i].profile === 'patch' || i === 0) { prov = provs[i]; break; }
    }
    setVal('cfg-llm-base-url', prov.base_url || '');
    setVal('cfg-llm-model', prov.model || '');
    setVal('cfg-llm-api-key', prov.api_key || '');
    setVal('cfg-llm-max-tokens', prov.max_tokens != null ? prov.max_tokens : (llm.max_tokens || ''));
    setVal('cfg-llm-timeout', llm.timeout || '');

    // Pipeline tab
    setVal('cfg-max-iterations', pl.max_iterations);
    setVal('cfg-max-global-cycles', pl.max_global_cycles);
    setVal('cfg-project-timeout', pl.project_timeout);
    setVal('cfg-parallel-workers', pl.parallel_workers);
    setToggle('cfg-run-tests', pl.run_tests);
    setToggle('cfg-semantic-audit', pl.semantic_audit);
    setToggle('cfg-use-mypy', pl.use_mypy);
    setToggle('cfg-use-ruff', pl.use_ruff !== false);
    setToggle('cfg-use-bandit', pl.use_bandit);
    setToggle('cfg-security-scan', pl.security_scan);
    setToggle('cfg-pip-audit', pl.pip_audit);
    setToggle('cfg-dry-run', cfg.dry_run);
    setToggle('cfg-verbose', cfg.verbose);

    // Tools tab
    setToggle('cfg-tool-correctr', (tools.correctr || {}).enabled);
    setToggle('cfg-tool-repomix', (tools.repomix || {}).enabled);
    setToggle('cfg-tool-aider', (tools.aider_repomap || {}).enabled);
    setToggle('cfg-tool-semgrep', (tools.semgrep || {}).enabled);
    setToggle('cfg-tool-hypothesis', (tools.hypothesis || {}).enabled);

    // Chat LLM tab
    var chatType = (chatCfg.type === 'cloud') ? 'cloud' : 'local';
    setVal('cfg-chat-type', chatType);
    setVal('cfg-chat-base-url', chatCfg.base_url || '');
    setVal('cfg-chat-api-key', chatCfg.api_key || '');
    setVal('cfg-chat-model', chatCfg.model || '');
    toggleChatLLMFields();
  });
}

function saveSettings() {
  if (!(window.eel && typeof eel.get_config === 'function')) {
    closeModal('modal-settings');
    return;
  }
  eel.get_config()(function(cfg) {
    cfg = cfg || {};

    // LLM — обновляем основной провайдер (patch), остальные не трогаем
    cfg.llm = cfg.llm || {};
    cfg.llm.providers = cfg.llm.providers || [];
    var patchIdx = -1;
    for (var i = 0; i < cfg.llm.providers.length; i++) {
      if (cfg.llm.providers[i].profile === 'patch' || patchIdx === -1) patchIdx = i;
    }
    if (patchIdx === -1) {
      cfg.llm.providers.push({profile: 'patch'}); patchIdx = 0;
    }
    var prov = cfg.llm.providers[patchIdx];
    prov.base_url = getVal('cfg-llm-base-url');
    prov.model = getVal('cfg-llm-model');
    var ak = getVal('cfg-llm-api-key');
    if (ak) prov.api_key = ak;
    var mt = parseInt(getVal('cfg-llm-max-tokens'), 10);
    if (!isNaN(mt)) prov.max_tokens = mt;
    var tout = parseInt(getVal('cfg-llm-timeout'), 10);
    if (!isNaN(tout)) cfg.llm.timeout = tout;

    // Pipeline
    cfg.pipeline = cfg.pipeline || {};
    var mi = parseInt(getVal('cfg-max-iterations'), 10);
    if (!isNaN(mi)) cfg.pipeline.max_iterations = mi;
    var mgc = parseInt(getVal('cfg-max-global-cycles'), 10);
    if (!isNaN(mgc)) cfg.pipeline.max_global_cycles = mgc;
    var pt = parseInt(getVal('cfg-project-timeout'), 10);
    if (!isNaN(pt)) cfg.pipeline.project_timeout = pt;
    var pw = parseInt(getVal('cfg-parallel-workers'), 10);
    if (!isNaN(pw)) cfg.pipeline.parallel_workers = pw;
    cfg.pipeline.run_tests = getToggle('cfg-run-tests');
    cfg.pipeline.semantic_audit = getToggle('cfg-semantic-audit');
    cfg.pipeline.use_mypy = getToggle('cfg-use-mypy');
    cfg.pipeline.use_ruff = getToggle('cfg-use-ruff');
    cfg.pipeline.use_bandit = getToggle('cfg-use-bandit');
    cfg.pipeline.security_scan = getToggle('cfg-security-scan');
    cfg.pipeline.pip_audit = getToggle('cfg-pip-audit');
    cfg.dry_run = getToggle('cfg-dry-run');
    cfg.verbose = getToggle('cfg-verbose');

    // Tools
    cfg.tools = cfg.tools || {};
    cfg.tools.correctr = cfg.tools.correctr || {};
    cfg.tools.correctr.enabled = getToggle('cfg-tool-correctr');
    cfg.tools.repomix = cfg.tools.repomix || {};
    cfg.tools.repomix.enabled = getToggle('cfg-tool-repomix');
    cfg.tools.aider_repomap = cfg.tools.aider_repomap || {};
    cfg.tools.aider_repomap.enabled = getToggle('cfg-tool-aider');
    cfg.tools.semgrep = cfg.tools.semgrep || {};
    cfg.tools.semgrep.enabled = getToggle('cfg-tool-semgrep');
    cfg.tools.hypothesis = cfg.tools.hypothesis || {};
    cfg.tools.hypothesis.enabled = getToggle('cfg-tool-hypothesis');

    // Chat LLM
    var chatType = getVal('cfg-chat-type');
    cfg.chat_llm = cfg.chat_llm || {};
    cfg.chat_llm.type = chatType;
    if (chatType === 'cloud') {
      cfg.chat_llm.base_url = getVal('cfg-chat-base-url');
      var ck = getVal('cfg-chat-api-key');
      if (ck) cfg.chat_llm.api_key = ck;
      cfg.chat_llm.model = getVal('cfg-chat-model');
    }

    eel.save_config(cfg)(function(r) {
      closeModal('modal-settings');
      if (r && r.ok) {
        agentChat('✅ Настройки сохранены в <code>webles_config.json</code>');
      } else {
        agentChat('⚠️ Ошибка сохранения настроек: ' + escapeHtml((r && r.error) || 'unknown'));
      }
    });
  });
}
function openLoadModal() { document.getElementById('modal-load').classList.add('open'); }
function closeModal(id) { document.getElementById(id).classList.remove('open'); }
function closeAllModals() { document.querySelectorAll('.modal-overlay').forEach(m => m.classList.remove('open')); }
function closeModalOnBg(e, id) { if (e.target.classList.contains('modal-overlay')) closeModal(id); }

// ── Settings tabs ──
function switchTab(btn, panelId) {
  document.querySelectorAll('.stab').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.stab-panel').forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById(panelId).classList.add('active');
}

// ── Lang select ──
function selectLang(el) {
  document.querySelectorAll('.lang-opt').forEach(o => o.classList.remove('active'));
  el.classList.add('active');
}

// ── Start analysis ──
function startAnalysis() {
  closeModal('modal-load');
  openMain('fix');
  // Если открыто через studio.py (Eel) — реально запускаем агента.
  if (window.eel && typeof eel.start_fix === 'function') {
    var langEl = document.querySelector('.lang-opt.active');
    var lang = (langEl ? langEl.textContent : 'python').trim().toLowerCase();
    var path = window.__projectPath || '';
    if (!path) { alert('Сначала выберите папку проекта.'); return; }
    if (window.clearDemo) window.clearDemo();
    eel.start_fix(path, lang);
  }
}

// ── View errors ──
// #6 + #8 + #9: «Все ошибки →» (теперь «На ревью») открывает модалку с
// реальными патчами из <project>/.webbles_fix/needs_review/. Каждая
// карточка раскрывается → видно diff/intent/confidence + три кнопки:
//   * Применить — eel.needs_review_apply(sig) → защита PatchEngine.
//   * Отклонить — eel.needs_review_reject(sig) → запись удаляется.
//   * Спросить чат — eel.needs_review_chat_opinion(sig) → LLM-вердикт
//     с обязательным предупреждением «LLM может ошибиться».
// Открывает очередь ревью прямо в чате (не модалка)
function viewAllErrors() {
  if (!(window.eel && typeof eel.needs_review_list === 'function')) {
    agentChat('👁 <b>Очередь ревью</b> — недоступна вне studio.py.');
    return;
  }
  eel.needs_review_list()(function(items) {
    items = items || [];
    if (!items.length) {
      agentChat('👁 <b>Очередь ревью пуста</b> — ничего не ждёт вашего решения.');
      return;
    }
    agentChat('👁 <b>Очередь ревью: ' + items.length + ' патч(ей)</b>');
    for (var i = 0; i < items.length; i++) {
      renderReviewItemInChat(items[i]);
    }
  });
}

// Вспомогательная функция: нумерованные строки файла (глобальная версия renderColumn)
function renderFileLines(content) {
  var lines = (content == null ? '' : String(content)).split(/\r?\n/);
  if (lines.length && lines[lines.length - 1] === '') lines.pop();
  var html = '';
  for (var i = 0; i < lines.length; i++) {
    html += '<div class="dc-line">' +
              '<span class="dc-num">' + (i + 1) + '</span>' +
              '<span class="dc-text">' + escapeHtml(lines[i] || ' ') + '</span>' +
            '</div>';
  }
  if (!html) html = '<div class="dc-line" style="padding:20px 12px"><span class="dc-num">—</span>' +
                    '<span class="dc-text" style="color:var(--txt3);font-style:italic">пусто</span></div>';
  return html;
}

// Рендерим один review-item в чате: шапка + diff-окна (до/после) + кнопки
function renderReviewItemInChat(it) {
  if (!it) return;
  var msgs = document.getElementById('chat-messages');
  if (!msgs) return;
  var sig = it.sig || it.safe_sig || '';
  var safeSig = it.safe_sig || sig;
  var conf = typeof it.confidence === 'number' ? (it.confidence * 100).toFixed(0) + '%' : '?';
  var confColor = (it.confidence || 0) >= 0.7 ? 'var(--green)' : (it.confidence || 0) >= 0.5 ? 'var(--yellow)' : 'var(--red)';
  var safeAttr = escapeHtmlAttr(safeSig);

  var wrap = document.createElement('div');
  wrap.className = 'msg-row msg-agent';
  wrap.style.cssText = 'align-items:flex-start;width:100%;min-width:0;max-width:100%;box-sizing:border-box';

  // Шапка карточки
  var headerHtml =
    '<div style="background:var(--s3);padding:8px 12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;border-bottom:1px solid var(--border)">' +
      '<span style="font-weight:600;font-size:13px">' + escapeHtml(it.file || '?') + ':' + (it.line || 0) + '</span>' +
      '<code style="background:var(--s4);padding:1px 5px;border-radius:3px;font-size:11px">' + escapeHtml(it.code || '?') + '</code>' +
      '<span style="color:' + confColor + ';font-size:11px">conf ' + conf + '</span>' +
      (it.patch_source ? '<span style="color:var(--txt3);font-size:11px">' + escapeHtml(it.patch_source) + '</span>' : '') +
    '</div>' +
    (it.message ? '<div style="padding:6px 12px;font-size:12px;color:var(--txt2)">' + escapeHtml(it.message) + '</div>' : '') +
    (it.intent ? '<div style="padding:4px 12px 6px;font-size:11px;color:var(--txt3)"><i>' + escapeHtml(it.intent) + '</i></div>' : '');

  // Diff-окна: заглушка пока грузится
  var diffPlaceholder =
    '<div class="diff-cols" style="min-width:0;height:300px">' +
      '<div class="diff-col diff-col-before" style="min-width:0;flex:1 1 0">' +
        '<div class="diff-col-label"><span>ДО</span></div>' +
        '<div style="padding:20px;color:var(--txt3);font-size:12px">Загрузка…</div>' +
      '</div>' +
      '<div class="diff-col diff-col-after" style="min-width:0;flex:1 1 0">' +
        '<div class="diff-col-label"><span>ПОСЛЕ</span></div>' +
        '<div style="padding:20px;color:var(--txt3);font-size:12px">Загрузка…</div>' +
      '</div>' +
    '</div>';

  // Кнопки действий
  var actionsHtml =
    '<div style="display:flex;gap:8px;padding:8px 12px;border-top:1px solid var(--border);background:var(--s2);flex-wrap:wrap;align-items:center">' +
      '<button data-action="apply" onclick="reviewApply(\'' + safeAttr + '\',this)" ' +
        'style="background:var(--green);color:#fff;border:none;border-radius:6px;padding:6px 16px;cursor:pointer;font-size:12px;font-weight:600">✓ Принять</button>' +
      '<button data-action="reject" onclick="reviewReject(\'' + safeAttr + '\',this)" ' +
        'style="background:var(--s3);color:var(--txt);border:1px solid var(--border);border-radius:6px;padding:6px 16px;cursor:pointer;font-size:12px">✕ Отклонить</button>' +
      '<button data-action="retry" onclick="reviewRetry(\'' + safeAttr + '\',this)" ' +
        'style="background:var(--s3);color:var(--accent);border:1px solid var(--accent);border-radius:6px;padding:6px 16px;cursor:pointer;font-size:12px">↺ Повторить генерацию</button>' +
    '</div>';

  wrap.innerHTML =
    '<div style="flex:1;min-width:0;background:var(--s2);border:1px solid var(--border);border-radius:8px;overflow:hidden">' +
      headerHtml +
      '<div class="diff-block" style="border-radius:0;border:none;margin:0" data-diff-container="' + safeAttr + '">' +
        diffPlaceholder +
      '</div>' +
      actionsHtml +
    '</div>';

  msgs.appendChild(wrap);
  msgs.scrollTop = msgs.scrollHeight;

  // Подгружаем полный diff асинхронно
  if (window.eel && typeof eel.needs_review_get_diff === 'function') {
    eel.needs_review_get_diff(safeSig)(function(d) {
      var container = msgs.querySelector('[data-diff-container="' + safeAttr + '"]');
      if (!container) return;
      if (!d || !d.ok) {
        container.innerHTML = '<div style="padding:12px;color:var(--txt3);font-size:12px">Не удалось загрузить содержимое файла: ' + escapeHtml((d && d.reason) || 'unknown') + '</div>';
        return;
      }
      container.innerHTML =
        '<div class="diff-cols" style="min-width:0">' +
          '<div class="diff-col diff-col-before" style="min-width:0;flex:1 1 0;max-height:480px">' +
            '<div class="diff-col-label"><span>ДО</span><span style="color:var(--txt3);font-size:10px;margin-left:4px">' + escapeHtml(d.file || '') + '</span></div>' +
            renderFileLines(d.before) +
          '</div>' +
          '<div class="diff-col diff-col-after" style="min-width:0;flex:1 1 0;max-height:480px">' +
            '<div class="diff-col-label"><span>ПОСЛЕ</span><span style="color:var(--txt3);font-size:10px;margin-left:4px">' + escapeHtml(d.file || '') + '</span></div>' +
            renderFileLines(d.after) +
          '</div>' +
        '</div>';
    });
  }
}

function escapeHtmlAttr(s) {
  return String(s == null ? '' : s).replace(/'/g, '&#39;').replace(/"/g, '&quot;');
}

function reviewApply(sig, btn) {
  if (!sig || !btn) return;
  btn.disabled = true; btn.textContent = '…';
  eel.needs_review_apply(sig)(function(r) {
    var wrap = btn.closest('.msg-row');
    if (r && r.ok) {
      btn.textContent = '✓ Применено';
      btn.style.opacity = '0.6';
      var reject = wrap && wrap.querySelector('button:last-child');
      if (reject) reject.disabled = true;
      refreshNeedsReviewBadge();
    } else {
      btn.textContent = '✗ Ошибка';
      btn.disabled = false;
    }
  });
}

function reviewReject(sig, btn) {
  if (!sig || !btn) return;
  btn.disabled = true; btn.textContent = '…';
  eel.needs_review_reject(sig)(function(r) {
    if (r && r.ok !== false) {
      var wrap = btn.closest('.msg-row');
      if (wrap) { wrap.style.opacity = '0.4'; wrap.style.pointerEvents = 'none'; }
      refreshNeedsReviewBadge();
    } else {
      btn.disabled = false; btn.textContent = '✕ Отклонить';
    }
  });
}

function reviewRetry(sig, btn) {
  if (!sig || !btn) return;
  btn.disabled = true; btn.textContent = '…';
  if (!(window.eel && typeof eel.needs_review_retry === 'function')) {
    btn.disabled = false; btn.textContent = '↺ Повторить генерацию';
    agentChat('⚠️ needs_review_retry недоступен (запустите через studio.py).');
    return;
  }
  eel.needs_review_retry(sig)(function(r) {
    if (r && r.ok !== false) {
      var wrap = btn.closest('.msg-row');
      if (wrap) {
        wrap.style.opacity = '0.5'; wrap.style.pointerEvents = 'none';
      }
      agentChat('↺ Патч удалён из очереди ревью. Ошибка будет перегенерирована при следующем запуске пайплайна.');
      refreshNeedsReviewBadge();
    } else {
      btn.disabled = false; btn.textContent = '↺ Повторить генерацию';
      agentChat('⚠️ Не удалось удалить патч: ' + escapeHtml((r && r.reason) || 'unknown'));
    }
  });
}

// Обновляем бейдж "На ревью" без необходимости run
function refreshNeedsReviewBadge() {
  if (!(window.eel && typeof eel.needs_review_list === 'function')) return;
  eel.needs_review_list()(function(items) {
    items = items || [];
    if (window.setRecentErrors) {
      window.setRecentErrors(items.map(function(it) {
        return {file: it.file || '?', line: it.line || 0, code: it.code || '?', verdict: 'NEEDS_REVIEW'};
      }));
    }
    if (window.renderSuggestErrors) window.renderSuggestErrors();
    if (window.renderSuggestNext) window.renderSuggestNext();
  });
}

function refreshReviewModal() {
  var list = document.getElementById('all-errors-list');
  if (!list) return;
  list.innerHTML = '<div style="color:var(--txt3);text-align:center;padding:20px">Загружаю…</div>';
  if (!(window.eel && typeof eel.needs_review_list === 'function')) {
    list.innerHTML = '<div style="color:var(--txt3);text-align:center;padding:20px">' +
                     'Eel недоступен — модалка работает только из studio.py.</div>';
    return;
  }
  eel.needs_review_list()(function (items) {
    list.innerHTML = '';
    items = items || [];
    if (!items.length) {
      list.innerHTML = '<div style="color:var(--txt3);text-align:center;padding:24px">' +
                       'Очередь ревью пуста — ничего не ждёт вашего решения.</div>';
      return;
    }
    for (var i = 0; i < items.length; i++) {
      list.appendChild(buildReviewCard(items[i]));
    }
  });
}

function buildReviewCard(it) {
  var card = document.createElement('div');
  card.className = 'review-card';
  card.style.cssText = 'border:1px solid var(--border);border-radius:8px;' +
                      'padding:10px;margin-bottom:10px;background:var(--s2)';
  var confTxt = (typeof it.confidence === 'number') ? it.confidence.toFixed(2) : '?';
  card.innerHTML =
    '<div class="rc-head" style="display:flex;justify-content:space-between;' +
          'align-items:center;cursor:pointer">' +
      '<div>' +
        '<b>' + escapeHtml(it.file || '?') + ':' + escapeHtml(String(it.line || 0)) + '</b>' +
        ' <span style="color:var(--txt3)">[' + escapeHtml(it.code || '?') + ']</span>' +
        '<div style="color:var(--txt3);font-size:12px;margin-top:2px">' +
          escapeHtml(it.message || '') +
        '</div>' +
      '</div>' +
      '<div style="display:flex;gap:6px;align-items:center;font-size:12px;color:var(--txt3)">' +
        '<span title="Источник">' + escapeHtml(it.patch_source || '?') + '</span>' +
        '<span title="Confidence" style="color:var(--yellow)">conf ' + confTxt + '</span>' +
        '<span class="rc-toggle">▾</span>' +
      '</div>' +
    '</div>' +
    '<div class="rc-body" style="display:none;margin-top:10px"></div>';

  var head = card.querySelector('.rc-head');
  var body = card.querySelector('.rc-body');
  head.addEventListener('click', function () {
    if (body.style.display === 'none') {
      expandReviewCard(it, body);
      body.style.display = 'block';
      card.querySelector('.rc-toggle').textContent = '▴';
    } else {
      body.style.display = 'none';
      card.querySelector('.rc-toggle').textContent = '▾';
    }
  });
  return card;
}

function expandReviewCard(it, body) {
  // Кнопки + место под diff/чат-вердикт.
  body.innerHTML =
    '<div style="color:var(--txt3);font-size:12px;margin-bottom:6px">' +
      'intent: <i>' + escapeHtml(it.intent || '(не указан)') + '</i> · ' +
      'строк в патче: ' + escapeHtml(String(it.patch_lines || 0)) +
    '</div>' +
    '<div class="rc-actions" style="display:flex;gap:8px;flex-wrap:wrap">' +
      '<button class="rc-apply" style="background:var(--green);color:#000;' +
            'border:none;padding:6px 14px;border-radius:5px;font-weight:600;cursor:pointer">' +
        '✓ Применить</button>' +
      '<button class="rc-reject" style="background:var(--red-dim);color:var(--red);' +
            'border:1px solid #ef444433;padding:6px 14px;border-radius:5px;cursor:pointer">' +
        '✗ Отклонить</button>' +
      '<button class="rc-ask" style="background:var(--s3);color:var(--txt2);' +
            'border:1px solid var(--border);padding:6px 14px;border-radius:5px;cursor:pointer">' +
        '💬 Спросить чат</button>' +
    '</div>' +
    '<div class="rc-result" style="margin-top:10px;font-size:12px"></div>';

  var resultEl = body.querySelector('.rc-result');
  var sig = it.sig;

  body.querySelector('.rc-apply').addEventListener('click', function () {
    resultEl.innerHTML = '<span style="color:var(--txt3)">Применяю…</span>';
    eel.needs_review_apply(sig)(function (r) {
      if (r && r.ok) {
        resultEl.innerHTML = '<span style="color:var(--green)">✓ Применено к ' +
                             escapeHtml(r.file || '') + '</span>';
        setTimeout(refreshReviewModal, 500);
      } else {
        resultEl.innerHTML = '<span style="color:var(--red)">✗ Отказ: ' +
                             escapeHtml((r && r.reason) || 'неизвестно') + '</span>';
      }
    });
  });

  body.querySelector('.rc-reject').addEventListener('click', function () {
    if (!confirm('Удалить запись из очереди ревью? Это действие нельзя отменить.')) return;
    eel.needs_review_reject(sig)(function (r) {
      if (r && r.ok) setTimeout(refreshReviewModal, 200);
      else resultEl.innerHTML = '<span style="color:var(--red)">✗ Не удалось удалить</span>';
    });
  });

  body.querySelector('.rc-ask').addEventListener('click', function () {
    resultEl.innerHTML = '<span style="color:var(--txt3)">Спрашиваю чат-LLM (это может занять несколько секунд)…</span>';
    eel.needs_review_chat_opinion(sig)(function (r) {
      if (!r || !r.ok) {
        resultEl.innerHTML = '<span style="color:var(--red)">✗ LLM не ответила: ' +
                             escapeHtml((r && r.reason) || 'неизвестно') + '</span>';
        return;
      }
      var color = (r.verdict === 'approve') ? 'var(--green)' :
                  (r.verdict === 'reject')  ? 'var(--red)'   :
                                              'var(--yellow)';
      var verdictLabel = (r.verdict === 'approve') ? 'Можно применить' :
                        (r.verdict === 'reject')  ? 'Лучше отклонить' :
                                                    'Не уверена';
      var concernsHtml = '';
      if (r.concerns && r.concerns.length) {
        concernsHtml = '<ul style="margin:6px 0 0 18px;color:var(--txt3)">';
        for (var i = 0; i < r.concerns.length; i++) {
          concernsHtml += '<li>' + escapeHtml(r.concerns[i]) + '</li>';
        }
        concernsHtml += '</ul>';
      }
      resultEl.innerHTML =
        '<div style="padding:10px;background:var(--s3);border-radius:6px;border:1px solid var(--border)">' +
          '<div style="color:' + color + ';font-weight:600;margin-bottom:4px">' +
            '🤖 Чат-LLM: ' + escapeHtml(verdictLabel) +
          '</div>' +
          '<div>' + escapeHtml(r.summary || '') + '</div>' +
          concernsHtml +
          '<div style="margin-top:8px;padding:6px 8px;background:var(--yellow-dim,#f59e0b22);' +
                'color:var(--yellow);border-radius:4px;font-size:11px">' +
            escapeHtml(r.disclaimer || '⚠️ LLM может ошибиться — проверьте патч сами.') +
          '</div>' +
        '</div>';
    });
  });
}

// ── Theme toggle ──
let isDark = false;
function toggleTheme() {
  isDark = !isDark;
  const html = document.documentElement;
  const btn = document.getElementById('theme-toggle-btn');
  if (isDark) {
    html.classList.add('dark');
    html.classList.remove('light');
    if (btn) btn.textContent = '🌙 Тёмная';
  } else {
    html.classList.remove('dark');
    html.classList.add('light');
    if (btn) btn.textContent = '☀️ Светлая';
  }
}
