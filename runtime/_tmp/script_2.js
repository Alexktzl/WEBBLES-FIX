
(function () {
  function basename(p) { return String(p || "").split(/[\\/]/).pop(); }

  function agentChat(html) {
    var msgs = document.getElementById("chat-messages");
    if (!msgs) return;
    var row = document.createElement("div");
    row.className = "msg-row agent";
    row.innerHTML =
      '<div class="msg-avatar avatar-agent">W</div>' +
      '<div class="msg-bubble"><div class="msg-meta">Webbles Agent &nbsp;·&nbsp; сейчас</div>' +
      '<div class="msg-text">' + html + '</div></div>';
    var typing = document.getElementById("typing-indicator");
    if (typing) msgs.insertBefore(row, typing); else msgs.appendChild(row);
    row.scrollIntoView({ behavior: "smooth", block: "end" });
  }
  window.agentChat = agentChat;

  // Максимум записей в ленте — чтоб не разбухала на долгих прогонах.
  var ACTIVITY_MAX = 200;
  function activity(text) {
    var log = document.querySelector(".activity-log");
    if (!log) return;
    var e = document.createElement("div");
    e.className = "act-entry recent";
    var t = new Date().toTimeString().slice(0, 5);
    e.innerHTML = '<span class="act-time">' + t + '</span>' + text;
    log.appendChild(e);
    // FIFO-обрезание старых записей.
    var entries = log.querySelectorAll(".act-entry");
    var excess = entries.length - ACTIVITY_MAX;
    for (var i = 0; i < excess; i++) entries[i].remove();
    // Автопрокрутка вниз — последняя запись в зоне видимости.
    log.scrollTop = log.scrollHeight;
  }
  // Чистит блок «Активность» от демо-данных, оставляя только заголовок.
  // Зовётся при входе в live-режим.
  function clearActivity() {
    var log = document.querySelector(".activity-log");
    if (!log) return;
    var entries = log.querySelectorAll(".act-entry");
    for (var i = 0; i < entries.length; i++) entries[i].remove();
  }
  window.clearActivity = clearActivity;

  // ── Mini Pipeline (левая панель) ──
  // 5 шагов с маркерами data-step: scan/index/generate/validate/apply.
  // Эти шаги — крупные стадии живого прогона, а не реальные стейджи движка
  // (Analyze/Classify/Generate/Validate/Decide). Достаточно для визуала.
  function setPipelineStep(name, status) {
    var steps = document.querySelectorAll('.mp-step[data-step="' + name + '"]');
    steps.forEach(function(step) {
      step.className = "mp-step " + status;
      var i18nKey = step.getAttribute('data-i18n');
      var label = i18nKey ? (window.t ? window.t(i18nKey) : i18nKey) : (step.textContent || "").replace(/^[✓⟳○]\s*/, "").trim();
      var icon = status === "done" ? "✓ " :
                 (status === "running" ? '<span class="spin">⟳</span> ' : "○ ");
      step.innerHTML = icon + escapeHtml(label);
    });
  }
  function resetPipeline() {
    ["scan", "index", "generate", "validate", "apply"].forEach(function (n) {
      setPipelineStep(n, "pending");
    });
  }
  function pipelineAllDone() {
    ["scan", "index", "generate", "validate", "apply"].forEach(function (n) {
      setPipelineStep(n, "done");
    });
  }
  window.resetPipeline = resetPipeline;

  // ── История чатов (левый overlay) ──
  // Без L.6 (персистентность между сессиями) — пока добавляем только текущие
  // прогоны этой сессии. Каждый run_started кладёт новую запись наверх,
  // помечает active, остальные становятся неактивными.
  var _chatRunCount = 0;
  function addChatRun(projectName) {
    var list = document.getElementById("chat-list");
    if (!list) return;
    _chatRunCount += 1;
    var name = projectName || ("Прогон " + _chatRunCount);
    var time = new Date().toTimeString().slice(0, 5);
    var row = document.createElement("div");
    row.className = "chat-item-row active";
    row.innerHTML =
      '<span class="ci-icon">🔧</span>' +
      '<div class="ci-info">' +
        '<div class="ci-name">' + escapeHtml(name) + '</div>' +
        '<div class="ci-date">Сегодня, ' + escapeHtml(time) + '</div>' +
      '</div>';
    // Деактивируем прежнюю активную, ставим placeholder-рядки в неактивные.
    Array.prototype.forEach.call(
      list.querySelectorAll(".chat-item-row"),
      function (r) { r.classList.remove("active"); }
    );
    // Удаляем placeholder-«Нет сохранённых чатов» (markLive ставит div, не row).
    Array.prototype.forEach.call(list.children, function (c) {
      if (!c.classList || !c.classList.contains("chat-item-row")) c.remove();
    });
    if (list.firstChild) list.insertBefore(row, list.firstChild);
    else list.appendChild(row);
  }
  window.addChatRun = addChatRun;

  // ── Suggestion-карточки ──
  // Карточка ошибок (sc-warn): топ-N последних обработанных ошибок.
  // Карточка «Что дальше?» (sc-info): подсказки из live-счётчиков.
  var recentErrors = []; // [{file, line, code, verdict}]
  var RECENT_ERR_MAX = 6;
  function recordRecentError(d, verdict) {
    var entry = {
      file: d.file || "?",
      line: d.line || 0,
      code: d.code || "?",
      verdict: verdict || "",
    };
    recentErrors.unshift(entry);
    if (recentErrors.length > RECENT_ERR_MAX) recentErrors.length = RECENT_ERR_MAX;
    renderSuggestErrors();
    renderSuggestNext();
  }
  function renderSuggestErrors() {
    var title = document.getElementById("suggest-errors-title");
    var list = document.getElementById("suggest-errors-list");
    if (!title || !list) return;
    if (!recentErrors.length) {
      title.textContent = "👁 На ревью пока пусто";
      list.innerHTML = '<li style="color:var(--txt3)">Сюда попадают патчи, которые движок отправил на ручной просмотр.</li>';
      return;
    }
    title.textContent = "👁 На ревью (" + recentErrors.length + ")";
    var html = "";
    for (var i = 0; i < recentErrors.length; i++) {
      var e = recentErrors[i];
      var tag = e.verdict ? (" · " + e.verdict.toLowerCase()) : "";
      html += "<li>" + escapeHtml(basename(e.file)) + ":" + escapeHtml(e.line) +
              " [" + escapeHtml(e.code) + "]" + escapeHtml(tag) + "</li>";
    }
    list.innerHTML = html;
  }
  function renderSuggestNext() {
    var list = document.getElementById("suggest-next-list");
    if (!list) return;
    var tips = [];
    if (counters.review > 0) {
      tips.push("👁 Просмотреть " + counters.review +
                " патч(ей) в <code>.webbles_fix/needs_review/</code>");
    }
    if (counters.failed > 0) {
      tips.push("✗ " + counters.failed + " ошибок не получилось починить — нужен ручной разбор");
    }
    if (counters.accepted > 0) {
      tips.push("✓ Применено " + counters.accepted +
                " патчей — запустить тесты на изменённом коде");
    }
    if (!tips.length) {
      tips.push("Запустите починку — здесь появятся рекомендации.");
    }
    list.innerHTML = tips.map(function (t) { return "<li>" + t + "</li>"; }).join("");
  }
  function clearSuggestions() {
    recentErrors = [];
    renderSuggestErrors();
    renderSuggestNext();
  }
  window.clearSuggestions = clearSuggestions;
  window.renderSuggestErrors = renderSuggestErrors;
  window.renderSuggestNext = renderSuggestNext;
  window.setRecentErrors = function(items) { recentErrors = items; };

  function treeFile(file) {
    // Сначала пробуем точное совпадение по data-rel (project_tree кладёт
    // относительный путь); если не нашли — fallback на basename для совместимости
    // с зашитыми демо-нодами.
    var rel = String(file || "");
    var byRel = document.querySelector('.tree-file[data-rel="' + rel.replace(/"/g, "") + '"]');
    if (byRel) return byRel;
    var name = basename(file), nodes = document.querySelectorAll(".tree-file"), i;
    for (i = 0; i < nodes.length; i++)
      if (nodes[i].textContent.indexOf(name) !== -1) return nodes[i];
    return null;
  }

  // ── Рендер дерева проекта из события project_tree ──
  function renderProjectTree(payload) {
    var tree = document.getElementById("file-tree");
    if (!tree) return;
    var items = (payload && payload.items) || [];
    var truncated = !!(payload && payload.truncated);
    var root = (payload && payload.root) || "";
    // Полная замена содержимого. Шапка с именем корня — для контекста.
    tree.innerHTML = "";
    if (root) {
      var hdr = document.createElement("div");
      hdr.className = "tree-folder";
      hdr.style.color = "var(--txt3)";
      hdr.style.fontSize = "10px";
      hdr.style.textTransform = "uppercase";
      hdr.style.letterSpacing = ".6px";
      hdr.style.paddingBottom = "4px";
      hdr.textContent = basename(root) || root;
      hdr.title = root;
      tree.appendChild(hdr);
    }
    if (!items.length) {
      var empty = document.createElement("div");
      empty.className = "tree-folder";
      empty.style.color = "var(--txt3)";
      empty.textContent = "Пусто";
      tree.appendChild(empty);
      return;
    }
    var BASE = 12, STEP = 14;
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      var depth = (typeof it.depth === "number") ? it.depth : 0;
      var pad = BASE + depth * STEP;
      var rel = String(it.path || "");
      var name = basename(rel);
      if (it.kind === "dir") {
        var d = document.createElement("div");
        d.className = "tree-folder";
        d.style.paddingLeft = pad + "px";
        d.innerHTML = "▾&nbsp;&nbsp;" + escapeHtml(name);
        d.title = rel;
        tree.appendChild(d);
      } else {
        var f = document.createElement("div");
        f.className = "tree-file";
        f.style.paddingLeft = (pad + 16) + "px";
        f.setAttribute("data-rel", rel);
        f.setAttribute("onclick", "selectFile(this)");
        f.title = rel;
        f.innerHTML = '<span class="file-dot"></span>' + escapeHtml(name);
        tree.appendChild(f);
      }
    }
    if (truncated) {
      var tail = document.createElement("div");
      tail.className = "tree-folder";
      tail.style.color = "var(--txt3)";
      tail.style.fontStyle = "italic";
      tail.textContent = "… усечено (лимит файлов)";
      tree.appendChild(tail);
    }
  }
  window.renderProjectTree = renderProjectTree;

  function setDot(file, cls) {
    var node = treeFile(file); if (!node) return;
    var dot = node.querySelector(".file-dot");
    if (dot) dot.className = "file-dot " + cls;
    var badge = node.querySelector(".file-err-badge, .file-warn-badge");
    if (cls === "dot-green" && badge) badge.remove();
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  window.escapeHtml = escapeHtml;

  // ── Live-счётчики в шапке чата ──
  var counters = { accepted: 0, review: 0, failed: 0, total: 0 };
  function showCounters() {
    var box = document.getElementById("live-counters");
    if (box) box.style.display = "";
  }
  function updateCounters() {
    var set = function (id, v) {
      var el = document.getElementById(id);
      if (el) el.textContent = String(v);
    };
    set("lc-accepted", counters.accepted);
    set("lc-review", counters.review);
    set("lc-failed", counters.failed);
    set("lc-total", counters.total);
  }

  // ── Динамический task-list по реальным ошибкам ──
  // Ключ: file:line:code → DOM-нода задачи. Создаём при error_dequeued,
  // обновляем статус при verdict / applied / needs_review / failed.
  var taskNodes = {};
  function errorKey(d) {
    return String(d.file || "?") + ":" + String(d.line || 0) + ":" + String(d.code || "?");
  }
  function ensureLiveTasksContainer() {
    var c = document.getElementById("live-tasks");
    if (c) return c;
    var panel = document.querySelector(".right-panel .rp-section");
    if (!panel) return null;
    panel.innerHTML = '<div class="rp-sec-title">Ошибки в работе</div>' +
      '<div id="live-tasks"></div>';
    return document.getElementById("live-tasks");
  }
  function addLiveTask(d) {
    var key = errorKey(d);
    if (taskNodes[key]) return taskNodes[key];
    var c = ensureLiveTasksContainer();
    if (!c) return null;
    counters.total += 1;
    var num = counters.total;
    var row = document.createElement("div");
    row.className = "task-item";
    row.innerHTML =
      '<div class="task-num">' + num + '</div>' +
      '<div class="task-info">' +
        '<div class="task-name">' + escapeHtml(basename(d.file)) + ':' + escapeHtml(d.line) + '</div>' +
        '<div class="task-sub">' + escapeHtml(d.code) + '</div>' +
      '</div>' +
      '<span class="task-status ts-running">⟳ Идёт</span>';
    c.appendChild(row);
    taskNodes[key] = row;
    showCounters();
    updateCounters();
    return row;
  }
  function setLiveTaskStatus(d, cls, label, subText) {
    var row = taskNodes[errorKey(d)] || addLiveTask(d);
    if (!row) return;
    var st = row.querySelector(".task-status");
    if (st) { st.className = "task-status " + cls; st.textContent = label; }
    if (subText) {
      var sub = row.querySelector(".task-sub");
      if (sub) sub.textContent = subText;
    }
    row.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function applyAgentEvent(evt) {
    if (!evt || !evt.type) return;
    var d = evt.data || {};
    switch (evt.type) {
      case "run_started":
        counters = { accepted: 0, review: 0, failed: 0, total: 0 };
        taskNodes = {};
        ensureLiveTasksContainer();
        showCounters();
        updateCounters();
        // Чистим ленту «Активность» от прошлого прогона/демо-данных.
        if (window.clearActivity) window.clearActivity();
        if (window.clearSuggestions) window.clearSuggestions();
        // Скрываем «Что дальше?» пока идёт прогон
        (function(){ var el = document.getElementById('suggest-next'); if (el) el.style.display = 'none'; })();
        // Pipeline: всё в pending, потом первый шаг — scan = running.
        if (window.resetPipeline) window.resetPipeline();
        setPipelineStep("scan", "running");
        // Mode-badge в шапке.
        if (window.setMode && d.mode) window.setMode(d.mode);
        var mbadge = document.getElementById('mode-badge-left');
        if (mbadge) mbadge.style.opacity = '';
        // Запись в историю чатов — имя по basename(project).
        addChatRun(d.project ? basename(d.project) : null);
        var proj = d.project ? (" для <code>" + escapeHtml(d.project) + "</code>") : "";
        var langTag = d.language ? (" · язык: <b>" + escapeHtml(d.language) + "</b>") : "";
        agentChat("Запускаю починку в режиме <b>" + escapeHtml(d.mode) + "</b>" + proj + langTag + ".");
        activity("Старт починки");
        break;
      case "run_error":
        agentChat("⚠️ Ошибка: " + escapeHtml(d.message || "неизвестно"));
        activity("Ошибка прогона");
        break;
      case "project_tree":
        renderProjectTree(d);
        activity("Дерево проекта: " + escapeHtml((d.items || []).length) + " элементов" +
                 (d.truncated ? " (усечено)" : ""));
        // Имя проекта в шапке слева — последний сегмент пути.
        var pn = document.getElementById("project-name");
        if (pn && d.root) {
          pn.textContent = basename(d.root) || d.root;
          pn.style.color = '';
        }
        // Pipeline: scan = done, index пошёл.
        setPipelineStep("scan", "done");
        setPipelineStep("index", "running");
        break;
      case "error_dequeued":
        addLiveTask(d);
        var msgTail = d.message ? (" — " + escapeHtml(String(d.message).slice(0, 100))) : "";
        agentChat("Анализирую <code>" + escapeHtml(basename(d.file)) +
                  "</code> — " + escapeHtml(d.code) + " (стр. " + escapeHtml(d.line) + ")" + msgTail + ".");
        activity("Взял " + escapeHtml(d.code) + " @ " + escapeHtml(basename(d.file)));
        // Pipeline: индекс закончен, начинается генерация патча.
        setPipelineStep("index", "done");
        setPipelineStep("generate", "running");
        setPipelineStep("validate", "pending");
        setPipelineStep("apply", "pending");
        break;
      case "patch_proposed":
        var intent = d.intent ? String(d.intent) : "";
        var src = d.patch_source ? (" · " + d.patch_source) : "";
        if (intent) {
          agentChat("Предлагаю исправление: " + escapeHtml(intent) + escapeHtml(src) + ".");
        }
        setLiveTaskStatus(d, "ts-running", "⟳ Патч", intent ? intent.slice(0, 40) : (d.code || ""));
        // Pipeline: патч есть → валидация в работе.
        setPipelineStep("generate", "done");
        setPipelineStep("validate", "running");
        break;
      case "verdict":
        // Промежуточное событие — само по себе UI не двигает, но фиксируем
        // confidence/verdict как подпись и записываем в Recent.
        var v = String(d.verdict || "").toUpperCase();
        if (v && taskNodes[errorKey(d)]) {
          var sub = taskNodes[errorKey(d)].querySelector(".task-sub");
          if (sub) {
            var conf = (typeof d.confidence === "number") ? (" · conf " + d.confidence.toFixed(2)) : "";
            sub.textContent = (sub.textContent || "") + " · " + v + conf;
          }
        }
        recordRecentError(d, v);
        // Pipeline: валидация done → применение в работе.
        setPipelineStep("validate", "done");
        setPipelineStep("apply", "running");
        break;
      case "applied":
        counters.accepted += 1; updateCounters();
        setDot(d.file, "dot-green");
        setLiveTaskStatus(d, "ts-done", "✓ Принят", null);
        activity("Применено: " + escapeHtml(basename(d.file)) +
                 (d.code ? (" [" + escapeHtml(d.code) + "]") : ""));
        setPipelineStep("apply", "done");
        renderSuggestNext();
        break;
      case "needs_review":
        counters.review += 1; updateCounters();
        setDot(d.file, "dot-yellow");
        setLiveTaskStatus(d, "ts-pending", "👁 Ревью", null);
        activity("На ручной просмотр: " + escapeHtml(basename(d.file)) +
                 (d.code ? (" [" + escapeHtml(d.code) + "]") : ""));
        setPipelineStep("apply", "done");
        renderSuggestNext();
        break;
      case "failed":
        counters.failed += 1; updateCounters();
        setLiveTaskStatus(d, "ts-pending", "✗ Не смог", null);
        activity("Не смог: " + escapeHtml(basename(d.file)) +
                 (d.code ? (" [" + escapeHtml(d.code) + "]") : ""));
        setPipelineStep("apply", "done");
        renderSuggestNext();
        break;
      case "explanation":
        agentChat(
          '<b>Объяснение</b><br>' +
          '<b>Что:</b> ' + escapeHtml(d.what) + '<br>' +
          '<b>Почему:</b> ' + escapeHtml(d.why) + '<br>' +
          '<b>Как:</b> ' + escapeHtml(d.how));
        break;
      case "run_finished":
        agentChat("Готово. Принято: <b>" + escapeHtml(d.accepted) +
                  "</b>, на ревью: <b>" + escapeHtml(d.needs_review) +
                  "</b>, не смог: <b>" + escapeHtml(d.failed) + "</b>.");
        activity("Прогон завершён");
        // Pipeline: все шаги завершены.
        pipelineAllDone();
        // Показываем «Что дальше?» только после завершения
        (function(){ var el = document.getElementById('suggest-next'); if (el) el.style.display = ''; })();
        renderSuggestNext();
        break;
    }
  }

  function connectAgentStream(url) {
    try {
      var ws = new WebSocket(url);
      ws.onmessage = function (m) {
        try { applyAgentEvent(JSON.parse(m.data)); } catch (e) {}
      };
      ws.onerror = function () { /* graceful: оффлайн — просто нет потока */ };
      return ws;
    } catch (e) { return null; }
  }

  // Экспорт для бэкенда/отладки.
  window.applyAgentEvent = applyAgentEvent;
  window.connectAgentStream = connectAgentStream;
  // Доп. тип события от studio.py — ошибка прогона.
  var _orig = window.applyAgentEvent;
  window.applyAgentEvent = function (evt) {
    if (evt && evt.type === "run_error") {
      agentChat("⚠️ Ошибка: " + escapeHtml((evt.data || {}).message || "неизвестно"));
      activity("Ошибка прогона");
      return;
    }
    _orig(evt);
  };
})();
