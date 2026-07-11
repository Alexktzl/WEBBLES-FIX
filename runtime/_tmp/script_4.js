
(function () {
  if (!window.eel) return;  // открыто без studio.py → остаётся демо-режим
  window.__projectPath = "";
  window.__live = true;

  // КРИТИЧНО: регистрируем JS-функцию, чтобы Python (studio.py) мог её звать
  // через eel.applyAgentEvent(evt)(). Без этого события из агента не дойдут.
  try { eel.expose(window.applyAgentEvent, "applyAgentEvent"); } catch (e) {}

  function liveMsg(text) {
    var msgs = document.getElementById("chat-messages");
    if (!msgs) return;
    var row = document.createElement("div");
    row.className = "msg-row agent";
    row.innerHTML = '<div class="msg-avatar avatar-agent">W</div>' +
      '<div class="msg-bubble"><div class="msg-meta">Webbles Agent</div>' +
      '<div class="msg-text">' + text + '</div></div>';
    var typing = document.getElementById("typing-indicator");
    if (typing) msgs.insertBefore(row, typing); else msgs.appendChild(row);
  }

  // Чистим зашитый демо-диалог прототипа (оставляем typing/итоговый каркас).
  function clearDemo() {
    var msgs = document.getElementById("chat-messages");
    if (!msgs) return;
    Array.prototype.slice.call(msgs.querySelectorAll(".msg-row")).forEach(function (r) {
      if (r.id !== "typing-indicator" && r.id !== "msg-fixed") r.remove();
    });
  }
  window.clearDemo = clearDemo;

  function chooseFolder(cb) {
    eel.pick_folder()(function (p) {
      if (!p) return;
      window.__projectPath = p;
      var box = document.querySelector("#modal-load [style*='dashed']");
      if (box) { var info = box.querySelector("div"); if (info) info.textContent = "Выбрано: " + p; }
      // Авто-определение языка
      if (window.eel && typeof eel.detect_language === 'function') {
        eel.detect_language(p)(function(res) {
          if (!res) return;
          var lang = (res.language || '').toLowerCase();
          // Подсвечиваем нужный язык в модалке
          var opts = document.querySelectorAll('.lang-opt');
          var found = false;
          opts.forEach(function(opt) {
            var optLang = (opt.textContent || opt.getAttribute('data-lang') || '').trim().toLowerCase();
            if (optLang === lang) { opt.click(); found = true; }
          });
          // Предупреждение если язык не поддерживается
          if (!res.supported && res.language) {
            var warn = document.querySelector('#modal-load .lang-warn');
            if (!warn) {
              warn = document.createElement('div');
              warn.className = 'lang-warn';
              warn.style.cssText = 'margin:6px 0;padding:6px 10px;background:#fff3cd;border-radius:6px;font-size:11px;color:#856404';
              var langArea = document.querySelector('#modal-load .lang-row') || document.querySelector('#modal-load .form-group');
              if (langArea) langArea.parentNode.insertBefore(warn, langArea.nextSibling);
            }
            warn.textContent = '⚠️ Определён язык "' + (res.language || '?') + '" — пайплайн ещё не поддерживает его. ' + (res.message || '');
          } else {
            var old = document.querySelector('#modal-load .lang-warn');
            if (old) old.remove();
          }
          if (!found && lang) {
            var box2 = document.querySelector("#modal-load [style*='dashed'] div");
            if (box2) box2.textContent = 'Выбрано: ' + p + ' [' + (res.language || '?') + ']';
          }
        });
      }
      if (cb) cb(p);
    });
  }
  window.chooseFolder = chooseFolder;

  function markLive() {
    var badge = document.querySelector(".status-badge");
    if (badge) { badge.textContent = "🟢 LIVE"; }
    clearDemo();
    // Нейтральный стартовый вид: название проекта, badge, дерево файлов.
    var pn = document.getElementById("project-name");
    if (pn) { pn.textContent = window.t ? window.t('project.noProject') : 'Проект не выбран'; pn.style.color = "var(--txt3)"; }
    var mb = document.getElementById("mode-badge-left");
    if (mb) mb.style.opacity = "0";
    var tree = document.getElementById("file-tree");
    if (tree) {
      var treePh = window.t ? window.t('tree.placeholder') : 'Файлы появятся после анализа…';
      tree.innerHTML = '<div class="tree-folder" data-i18n="tree.placeholder" style="color:var(--txt3)">' + treePh + '</div>';
    }
    var clist = document.getElementById("chat-list");
    if (clist) clist.innerHTML =
      '<div style="padding:10px 14px;color:var(--txt3);font-size:11px">Загрузка чатов…</div>';
    // Загружаем реальный список чатов. Историю НЕ грузим автоматически —
    // при старте показываем чистый чат; история доступна через клик в сайдбаре.
    if (window.refreshChatList) window.refreshChatList();
    liveMsg("💬 Выберите чат в боковой панели или нажмите <b>«Новый чат»</b> для начала работы.");
    // Загружаем счётчик очереди ревью с диска.
    if (window.refreshNeedsReviewBadge) window.refreshNeedsReviewBadge();
    // Лента «Активность» содержит зашитые демо-записи (12:02/12:03/12:04 + «—»).
    // Чистим до заголовка, реальные события пойдут уже сверху.
    if (window.clearActivity) window.clearActivity();
    // Mini-pipeline зашит в done/done/running/pending/pending (демо). До
    // первого run_started корректно показывать все шаги в pending.
    if (window.resetPipeline) window.resetPipeline();
    // Suggestion-карточки — в нейтральный placeholder.
    if (window.clearSuggestions) window.clearSuggestions();
    liveMsg("🟢 <b>Live-режим</b> — подключено к агенту. Нажмите «🔧 Исправить проект», выберите папку и язык, затем «Начать анализ».");
  }

  function wire() {
    markLive();
    // Большая кнопка «Исправить проект» в live-режиме открывает выбор проекта,
    // а не демо-экран. (Присвоение onclick перекрывает инлайновый обработчик.)
    var btnFix = document.querySelector(".btn-fix");
    if (btnFix) btnFix.onclick = function () { openLoadModal(); };
    // Клик по drop-зоне в модалке/на старте → выбор папки.
    document.addEventListener("click", function (ev) {
      var t = ev.target; if (!t) return;
      var txt = (t.textContent || "");
      if (txt.indexOf("выберите файл") !== -1 || txt.indexOf("Перетащите папку") !== -1) {
        chooseFolder();
      }
    });
  }

  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", wire);
  else wire();
})();
