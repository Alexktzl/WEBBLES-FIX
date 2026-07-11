
// ── i18n ──
var _lang = 'ru';
var _T = {
  ru: {
    'lp.chats': 'История чатов',
    'lp.newProject': 'Новый проект',
    'project.noProject': 'Проект не выбран',
    'tree.placeholder': 'Файлы появятся после анализа…',
    'step.scan': 'Анализ проекта',
    'step.index': 'Индекс символов',
    'step.generate': 'Генерация фикса',
    'step.validate': 'Валидация',
    'step.apply': 'Применение',
    'chat.panelTitle': '💬 Чаты',
    'chat.newBtn': 'Новый чат',
    'chat.searchPh': '🔍  Поиск чатов...',
    'typing.label': 'Webbles думает...',
    'input.placeholder': 'Спросить что-то о проекте...',
    'input.attach': 'Прикрепить файл проекта',
    'input.mention': 'Упомянуть файл (@файл)',
    'input.commands': 'Команды',
    'rp.header': '⚡ Задачи агента',
    'act.title': 'Активность',
    'rp.steps': 'Шаги',
    'err.noErrors': '⚠ Ошибок ещё не было',
    'err.placeholder': 'События появятся в ходе прогона…',
    'review.openBtn': 'Открыть очередь ревью →',
    'next.title': '💡 Что дальше?',
    'next.placeholder': 'Запустите починку — здесь появятся рекомендации.',
    'modal.newChat': 'Новый чат',
    'modal.chooseMode': 'Выберите режим:',
    'modal.fixDesc': '<b>Fix</b> — найти и исправить ошибки в проекте',
    'modal.createDesc': '<b>Create</b> — создать новый проект с нуля',
    'modal.chatDesc': '<b>Chat</b> — свободный диалог с ассистентом',
    'settings.title': '⚙️ Настройки',
    'settings.tabTools': '🔧 Инструменты',
    'settings.tabTheme': '🎨 Тема',
    'settings.llmDesc': 'LLM для генерации патчей (основной движок)',
  },
  en: {
    'lp.chats': 'Chat History',
    'lp.newProject': 'New project',
    'project.noProject': 'No project selected',
    'tree.placeholder': 'Files will appear after analysis…',
    'step.scan': 'Project scan',
    'step.index': 'Symbol index',
    'step.generate': 'Fix generation',
    'step.validate': 'Validation',
    'step.apply': 'Apply patches',
    'chat.panelTitle': '💬 Chats',
    'chat.newBtn': 'New Chat',
    'chat.searchPh': '🔍  Search chats...',
    'typing.label': 'Webbles is thinking...',
    'input.placeholder': 'Ask something about the project...',
    'input.attach': 'Attach project file',
    'input.mention': 'Mention file (@file)',
    'input.commands': 'Commands',
    'rp.header': '⚡ Agent tasks',
    'act.title': 'Activity',
    'rp.steps': 'Steps',
    'err.noErrors': '⚠ No errors yet',
    'err.placeholder': 'Events will appear during the run…',
    'review.openBtn': 'Open review queue →',
    'next.title': '💡 What\'s next?',
    'next.placeholder': 'Run the fixer — recommendations will appear here.',
    'modal.newChat': 'New Chat',
    'modal.chooseMode': 'Choose mode:',
    'modal.fixDesc': '<b>Fix</b> — find and fix errors in the project',
    'modal.createDesc': '<b>Create</b> — build a new project from scratch',
    'modal.chatDesc': '<b>Chat</b> — free conversation with the assistant',
    'settings.title': '⚙️ Settings',
    'settings.tabTools': '🔧 Tools',
    'settings.tabTheme': '🎨 Theme',
    'settings.llmDesc': 'LLM for patch generation (main engine)',
  }
};

function t(key) {
  return (_T[_lang] && _T[_lang][key]) || (_T['ru'][key]) || key;
}

function applyLang(lang) {
  _lang = lang;
  // text content
  document.querySelectorAll('[data-i18n]').forEach(function(el) {
    var key = el.getAttribute('data-i18n');
    var val = t(key);
    // Use innerHTML for keys that may contain <b> tags
    if (val.indexOf('<') !== -1) el.innerHTML = val;
    else el.textContent = val;
  });
  // placeholder attributes
  document.querySelectorAll('[data-i18n-placeholder]').forEach(function(el) {
    el.placeholder = t(el.getAttribute('data-i18n-placeholder'));
  });
  // title attributes
  document.querySelectorAll('[data-i18n-title]').forEach(function(el) {
    el.title = t(el.getAttribute('data-i18n-title'));
  });
  // Pipeline steps: re-apply with icon prefix preserved
  document.querySelectorAll('.mp-step[data-i18n]').forEach(function(el) {
    var icon = el.classList.contains('done') ? '✓ ' :
               el.classList.contains('running') ? '⟳ ' : '○ ';
    el.textContent = icon + t(el.getAttribute('data-i18n'));
  });
  // Lang button label
  var btn = document.getElementById('lang-btn');
  if (btn) btn.textContent = lang === 'ru' ? 'EN' : 'RU';
  // Store preference
  try { localStorage.setItem('webbles_lang', lang); } catch(e) {}
}

function toggleLang() {
  applyLang(_lang === 'ru' ? 'en' : 'ru');
}

// Init: restore saved lang preference
(function() {
  var saved = '';
  try { saved = localStorage.getItem('webbles_lang') || ''; } catch(e) {}
  if (saved === 'en') applyLang('en');
  // else default ru is already in HTML
})();

window.t = t;
window.applyLang = applyLang;
