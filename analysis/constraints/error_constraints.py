"""
Constraints table: error_code -> DO/DON'T hints for case-file builder.
"""

from typing import Dict, List, Tuple


ERROR_CONSTRAINTS: Dict[str, Dict[str, List[str]]] = {
    "E0432": {
        "do": ["Add the missing crate to Cargo.toml under [dependencies].",
               "Check the exact module path."],
        "dont": ["Don't add mod X; for a crate.",
                 "Don't comment out the use line."],
    },
    "E0433": {
        "do": ["Verify the path: case-sensitive crate name.",
               "Add extern crate / use for the right module."],
        "dont": ["Don't invent a new module to satisfy the path."],
    },
    "E0425": {
        "do": ["Import the symbol with use path::to::it.",
               "Check for typos against actual definitions."],
        "dont": ["Don't define a new symbol with that name unless needed.",
                 "Don't stub or replace the call with unimplemented!()."],
    },
    "E0599": {
        "do": ["Use one of the methods listed in the error note.",
               "Add use Trait; if the method comes from a trait."],
        "dont": ["Don't add the method to a foreign type's impl block.",
                 "Don't introduce unsafe."],
    },
    "E0609": {
        "do": ["Use one of the existing fields listed in the error note.",
               "Update the call site to the new field name if renamed."],
        "dont": ["Don't add a new field to the struct just to satisfy the access.",
                 "Don't rename the struct."],
    },
    "E0560": {
        "do": ["Use the actual field names from the struct definition.",
               "Remove the unknown field from the struct literal."],
        "dont": ["Don't add the unknown field to the struct."],
    },
    "E0382": {
        "do": ["Pass a reference (&value) instead of moving.",
               "Call .clone() if the type implements Clone."],
        "dont": ["Don't use unsafe to bypass the borrow checker.",
                 "Don't wrap in Rc/Arc as a first reflex."],
    },
    "E0502": {
        "do": ["Scope the immutable borrow so it ends before the mutable one.",
               "Take the data out (mem::take / clone)."],
        "dont": ["Don't introduce unsafe.",
                 "Don't restructure into Rc<RefCell<_>> as a first reflex."],
    },
    "E0507": {
        "do": ["Borrow the field instead of moving (&self.field).",
               "Use .clone() if cheap.",
               "Replace via mem::take / mem::replace if you need ownership."],
        "dont": ["Don't change the signature to take self by value just for this."],
    },
    "E0308": {
        "do": ["Convert with .into() / as / to_string() / &... / *... as appropriate.",
               "Adjust the call site to produce the expected type."],
        "dont": ["Don't change the signature of a public function to match a wrong caller.",
                 "Don't add lossy as casts between unrelated types."],
    },
    "E0277": {
        "do": ["Implement the missing trait for the type if you own it.",
               "Use a different type that already implements the trait.",
               "Add the missing use for the trait."],
        "dont": ["Don't #[allow(...)] to silence the error.",
                 "Don't call .unwrap() / .expect() on uncompiled code."],
    },
    "E0765": {
        "do": ["Close the string literal with a matching double-quote.",
               "Escape embedded double-quotes inside the string."],
        "dont": ["Don't delete the offending line - restore the closing quote."],
    },
    "E0061": {
        "do": ["Match the call site to the function's actual arity.",
               "Provide the missing argument with the correct type."],
        "dont": ["Don't change the function signature if it is widely used."],
    },
    "unused_variable": {
        "do": ["Prefix the binding with _ if intentionally unused.",
               "Remove the binding entirely if no side effect is needed."],
        "dont": ["Don't #[allow(unused_variables)] at module level."],
    },
    "unused_import": {
        "do": ["Remove the unused use line."],
        "dont": ["Don't keep the import just because it might be needed later."],
    },
    "dead_code": {
        "do": ["Remove the dead item if it is private and truly unreachable.",
               "Mark pub or #[allow(dead_code)] only if it's a public API."],
        "dont": ["Don't blanket-silence dead_code for the whole module."],
    },
    "TEST_FAILURE": {
        "do": ["Read the failing assertion carefully and fix the production code.",
               "Trace the value the test expected back to the function under test.",
               "If the test encodes the intended contract, fix the implementation."],
        "dont": ["Don't edit, weaken, or delete the test to make it pass.",
                 "Don't add broad try/except to silence the failure.",
                 "Don't hardcode the expected output in the production code."],
    },
    "SEMANTIC_MISMATCH": {
        "do": ["Decide which side is authoritative - usually fix the code.",
               "Make the smallest change that re-aligns code and documentation.",
               "If the doc is outdated and the code is correct, update the doc."],
        "dont": ["Don't delete or gut the docstring to silence the mismatch.",
                 "Don't assume the code is correct by default.",
                 "Don't hide the discrepancy."],
    },
    "SECURITY": {
        "do": ["Treat all external input as untrusted and validate at the boundary.",
               "Prefer the safe API of the same library.",
               "Move secrets out of source into environment variables.",
               "Keep the fix minimal and behaviour-preserving."],
        "dont": ["Don't comment out or # nosec the finding.",
                 "Don't log, print, or echo the secret while fixing.",
                 "Don't broaden into an unrelated refactor."],
    },
    "hardcoded_secret": {
        "do": ["Read the secret from an environment variable (os.environ / process.env) or a secret store.",
               "Fail loudly if the variable is missing.",
               "Rotate the leaked credential - once committed, consider it compromised."],
        "dont": ["Don't keep the literal as a default value next to the env lookup.",
                 "Don't move the secret to another tracked file."],
    },
    "dangerous_eval": {
        "do": ["Use a safe parser — json.loads or ast.literal_eval — for structured data.",
               "Dispatch via an explicit allow-list mapping, not eval."],
        "dont": ["Don't rely on blacklisting or sanitizing the string and still pass it to eval.",
                 "Don't replace eval with exec."],
    },
    "unsafe_deserialization": {
        "do": ["Use yaml.safe_load instead of yaml.load.",
               "Restrict accepted types when deserializing external data."],
        "dont": ["Don't deserialize untrusted bytes with pickle/marshal.",
                 "Don't pass Loader=yaml.Loader/FullLoader."],
    },
    "sql_injection": {
        "do": ["Use parameterized queries / prepared statements.",
               "Validate dynamic identifiers against an allow-list."],
        "dont": ["Don't escape quotes manually.",
                 "Don't concatenate or f-string user input into SQL."],
    },
    "command_injection": {
        "do": ["Pass arguments as a list without a shell (shell=False).",
               "Validate against an allow-list when the command depends on input."],
        "dont": ["Don't build the command with string concatenation.",
                 "Don't rely on quoting/escaping as the fix."],
    },
    "xss": {
        "do": ["Assign untrusted text via textContent / innerText.",
               "If HTML is required, sanitize with a vetted library like DOMPurify."],
        "dont": ["Don't write user-controlled strings into innerHTML.",
                 "Don't hand-roll a regex sanitizer."],
    },

    # ---- P.0 mypy codes ----
    "name-defined": {
        "do": ["Define the name in the same module before use.",
               "Add the correct import / from ... import ... if it lives elsewhere.",
               "Check for typos against the suggestions mypy lists."],
        "dont": ["Don't # type: ignore[name-defined] to silence.",
                 "Don't shadow a built-in with a stub of the same name."],
    },
    "used-before-def": {
        "do": ["Move the definition above the first use, or reorder imports.",
               "For forward refs in annotations, use from __future__ import annotations or quote the name."],
        "dont": ["Don't # type: ignore - the runtime behaviour is broken too."],
    },
    "attr-defined": {
        "do": ["Use one of the attributes the type actually exposes.",
               "Narrow with isinstance(...) if the attr belongs to a subclass.",
               "Rename to the existing attribute if it is a typo."],
        "dont": ["Don't add the attribute just to silence mypy.",
                 "Don't # type: ignore[attr-defined] to bypass a real bug."],
    },
    "arg-type": {
        "do": ["Convert the value to the expected type (int(x), str(x), Path(x)).",
               "Fix the call-site to pass a value of the expected type.",
               "If the signature is too narrow, widen it to Union[...] / Protocol."],
        "dont": ["Don't cast() blindly to satisfy the type-checker.",
                 "Don't change the function signature to Any."],
    },
    "return-value": {
        "do": ["Return a value that matches the annotated return type.",
               "If the function returns multiple types, widen to Union[...]."],
        "dont": ["Don't change the annotation to Any.",
                 "Don't drop the annotation."],
    },
    "assignment": {
        "do": ["Convert the RHS to the LHS type, or fix the LHS annotation.",
               "If intentionally re-typing, give it a different name."],
        "dont": ["Don't broaden the LHS to Any."],
    },
    "union-attr": {
        "do": ["Narrow with if x is None: ... / if isinstance(x, T): ...",
               "Use assert x is not None when the invariant is enforced earlier.",
               "Provide a default with x or default when sensible."],
        "dont": ["Don't # type: ignore[union-attr] - runtime will throw."],
    },
    "call-arg": {
        "do": ["Pass exactly the arguments the signature requires.",
               "Update all callers if the signature changed."],
        "dont": ["Don't add **kwargs to swallow unknown arguments."],
    },
    "call-overload": {
        "do": ["Adjust argument types to match one of the existing overloads.",
               "Add a new overload if genuinely needed."],
        "dont": ["Don't # type: ignore - it is a real bug."],
    },
    "index": {
        "do": ["Use a key of the correct type.",
               "Narrow Optional[...] containers first."],
        "dont": ["Don't cast the key blindly."],
    },
    "operator": {
        "do": ["Convert one of the operands to a compatible type.",
               "Define __add__ / __lt__ / ... on the class if intentional."],
        "dont": ["Don't # type: ignore[operator] - Python will raise TypeError."],
    },
    "import": {
        "do": ["Add the missing package to requirements.txt / pyproject.toml.",
               "Correct the import path if wrong.",
               "Install types-<package> if the lib has no stubs."],
        "dont": ["Don't # type: ignore[import] without installing stubs."],
    },
    "import-not-found": {
        "do": ["Add the missing package to requirements.txt.",
               "Check the module path for typos."],
        "dont": ["Don't silence with # type: ignore - the module is genuinely missing."],
    },
    "import-untyped": {
        "do": ["Install types-<package> from typeshed if available.",
               "Add ignore_missing_imports = True for the pkg in mypy.ini if no stubs."],
        "dont": ["Don't replace the package with a half-baked stub of your own."],
    },
    "unreachable": {
        "do": ["Remove the dead code below the terminating statement.",
               "Move debugging code above the return/raise if it was intentional."],
        "dont": ["Don't leave commented-out blocks pretending to be guarded code."],
    },
    "no-redef": {
        "do": ["Rename one of the definitions to avoid clashing.",
               "If shadowing is intentional, # type: ignore[no-redef] explicitly."],
        "dont": ["Don't delete a definition silently - it may break callers."],
    },
    "var-annotated": {
        "do": ["Add an explicit annotation, e.g. xs: list[int] = []."],
        "dont": ["Don't annotate as Any - mypy loses tracking elsewhere."],
    },
    "no-untyped-def": {
        "do": ["Add annotations for parameters and return type."],
        "dont": ["Don't annotate everything as Any."],
    },
}


# Курируемые before→after примеры для контекст-зависимых security-кодов
# (идея Алекса 2026-07-09): эти коды осознанно оставлены LLM (§3 CLAUDE.md),
# т.к. детерминированный фиксер их безопасно не берёт. Конкретный пример
# «плохо → правильно» в промпте резко поднимает качество LLM-фикса и работает
# кросс-языково. Безопасность держит отдельный страж-подлинности (не даёт
# «починить» косметическим suppression-комментарием) — см.
# generate_patch_stage._security_fix_is_cosmetic.
SECURITY_EXAMPLES: dict = {
    "sql_injection": {
        "python": (
            "BAD  (injectable): cur.execute(f\"SELECT * FROM users WHERE id = {uid}\")\n"
            "GOOD (parameterized): cur.execute(\"SELECT * FROM users WHERE id = ?\", (uid,))\n"
            "Rules: use the driver paramstyle (sqlite3 -> ?, psycopg2/mysql -> %s); pass "
            "values as the SECOND argument to execute(); NEVER concatenate/format/f-string "
            "user values into the SQL text. If a dynamic table/column name is required, keep "
            "it flagged (cannot be parameterized) rather than faking a fix. Do NOT add a "
            "suppression comment (# nosec / # noqa / nosemgrep)."
        ),
        "rust": (
            "BAD  (injectable): sqlx::query(&format!(\"SELECT * FROM users WHERE id = {}\", id))\n"
            "GOOD (bound): sqlx::query(\"SELECT * FROM users WHERE id = $1\").bind(id)\n"
            "Never format!/concatenate user values into SQL; use bind params. Do NOT add "
            "#[allow(...)] to hide the lint."
        ),
        "csharp": (
            "BAD  (injectable): new SqlCommand($\"SELECT * FROM users WHERE id = {id}\", conn)\n"
            "GOOD (parameterized): var cmd = new SqlCommand(\"SELECT * FROM users WHERE id = @id\", conn);\n"
            "                      cmd.Parameters.AddWithValue(\"@id\", id);\n"
            "Use @-parameters + Parameters.Add; never interpolate/concat user values into SQL. "
            "С EF используй LINQ/FromSqlInterpolated, не FromSqlRaw со строкой. Не глуши #pragma warning."
        ),
        "cpp": (
            "BAD  (injectable): \"SELECT * FROM users WHERE id = \" + id\n"
            "GOOD (prepared): sqlite3_prepare_v2(db, \"SELECT * FROM users WHERE id = ?\", ...);\n"
            "                 sqlite3_bind_text(stmt, 1, id, -1, SQLITE_TRANSIENT);\n"
            "Используй prepared statements + bind; никогда не склеивай пользовательский ввод в SQL."
        ),
    },
    "command_injection": {
        "python": (
            "BAD  (shell injection): subprocess.run(f\"ls {path}\", shell=True)\n"
            "GOOD (no shell): subprocess.run([\"ls\", path])\n"
            "Pass arguments as a list with shell=False; never build the command from user "
            "input via string concat/format. Do NOT add # nosec/# noqa."
        ),
        "rust": (
            "BAD:  Command::new(\"sh\").arg(\"-c\").arg(format!(\"ls {}\", p)).output()\n"
            "GOOD: Command::new(\"ls\").arg(p).output()\n"
            "Pass args separately; never interpolate user input into a shell string."
        ),
        "csharp": (
            "BAD:  Process.Start(\"cmd.exe\", \"/c dir \" + path)\n"
            "GOOD: var psi = new ProcessStartInfo(\"dir\"); psi.ArgumentList.Add(path);\n"
            "      psi.UseShellExecute = false; Process.Start(psi);\n"
            "Аргументы через ArgumentList, UseShellExecute=false; не строй команду конкатенацией."
        ),
        "cpp": (
            "BAD:  system((\"ls \" + path).c_str());   // shell-инъекция\n"
            "GOOD: execvp с массивом аргументов (posix_spawn/exec*), без оболочки.\n"
            "Не используй system()/popen() с пользовательским вводом; передавай args массивом."
        ),
    },
    "dangerous_eval": {
        "python": (
            "BAD  (arbitrary code): result = eval(user_data)\n"
            "GOOD (structured data): import ast; result = ast.literal_eval(user_data)\n"
            "GOOD (dispatch): handler = {\"add\": do_add, \"sub\": do_sub}[cmd]  # allow-list\n"
            "Use ast.literal_eval/json.loads for data, or an explicit allow-list mapping "
            "for dispatch. NEVER replace eval with exec. Do NOT # nosec."
        ),
    },
    "hardcoded_secret": {
        "python": (
            "BAD  (leaked): API_KEY = \"sk-live-abc123\"\n"
            "GOOD (from env): import os; API_KEY = os.environ[\"API_KEY\"]  # fails loudly if missing\n"
            "Read from env/secret store; do NOT keep the literal as a default "
            "(os.environ.get(\"API_KEY\", \"sk-live-abc123\") still leaks it). Rotate the "
            "committed credential — treat it as compromised. Do NOT move it to another file."
        ),
        "rust": (
            "BAD:  const API_KEY: &str = \"sk-live-abc123\";\n"
            "GOOD: let api_key = std::env::var(\"API_KEY\").expect(\"API_KEY not set\");\n"
            "Read from env; never keep the literal as a fallback."
        ),
        "csharp": (
            "BAD:  const string ApiKey = \"sk-live-abc123\";\n"
            "GOOD: var apiKey = Environment.GetEnvironmentVariable(\"API_KEY\")\n"
            "                   ?? throw new InvalidOperationException(\"API_KEY not set\");\n"
            "Читай из env/IConfiguration/secret-store; не оставляй литерал дефолтом. Ротируй утёкший ключ."
        ),
        "cpp": (
            "BAD:  const char* API_KEY = \"sk-live-abc123\";\n"
            "GOOD: const char* API_KEY = std::getenv(\"API_KEY\");  // проверь на null\n"
            "Читай из окружения; не держи секрет литералом."
        ),
    },
    "unsafe_deserialization": {
        "python": (
            "BAD  (yaml.load): data = yaml.load(text)\n"
            "GOOD: data = yaml.safe_load(text)\n"
            "BAD  (pickle untrusted): obj = pickle.loads(untrusted_bytes)\n"
            "GOOD: use json.loads for untrusted data (pickle executes arbitrary code).\n"
            "Never pass Loader=yaml.Loader/FullLoader. Do NOT # nosec."
        ),
        "csharp": (
            "BAD  (RCE): new BinaryFormatter().Deserialize(stream)\n"
            "GOOD: System.Text.Json.JsonSerializer.Deserialize<T>(stream)\n"
            "BinaryFormatter/NetDataContractSerializer/LosFormatter выполняют код — замени на "
            "System.Text.Json (или Newtonsoft с TypeNameHandling.None). Не JavaScriptSerializer с типами."
        ),
    },
    "xss": {
        "javascript": (
            "BAD  (XSS): el.innerHTML = userInput\n"
            "GOOD (text): el.textContent = userInput\n"
            "GOOD (if HTML needed): el.innerHTML = DOMPurify.sanitize(userInput)\n"
            "Assign untrusted text via textContent/innerText; if HTML is required sanitize "
            "with a vetted library. Do NOT hand-roll a regex sanitizer or add // nosemgrep."
        ),
        "python": (
            "BAD  (Django): return HttpResponse(mark_safe(user_input))\n"
            "GOOD: rely on template auto-escaping; never mark_safe untrusted input.\n"
            "Do NOT wrap user input in mark_safe / |safe."
        ),
        "csharp": (
            "BAD  (Razor): @Html.Raw(userInput)\n"
            "GOOD: @userInput   // Razor авто-экранирует\n"
            "Полагайся на авто-экранирование Razor; не оборачивай пользовательский ввод в Html.Raw."
        ),
    },
    # ---- новые категории (все языки) ----
    "path_traversal": {
        "python": (
            "BAD  (../ escape): open(os.path.join(base, user_name))\n"
            "GOOD: p = os.path.realpath(os.path.join(base, user_name))\n"
            "      if not p.startswith(os.path.realpath(base)+os.sep): raise ValueError\n"
            "Резолви путь и проверь, что он ВНУТРИ базового каталога; отсекай '..'."
        ),
        "csharp": (
            "BAD:  File.ReadAllText(Path.Combine(baseDir, userName))\n"
            "GOOD: var full = Path.GetFullPath(Path.Combine(baseDir, userName));\n"
            "      if (!full.StartsWith(Path.GetFullPath(baseDir))) throw ...;\n"
            "Проверь, что итоговый путь внутри базового каталога."
        ),
        "cpp": (
            "Канонизируй путь (realpath/std::filesystem::weakly_canonical) и проверь, что он "
            "начинается с разрешённого базового каталога; отвергай '..'-выход."
        ),
    },
    "xxe": {
        "python": (
            "BAD  (XXE): ET.parse(untrusted_xml)\n"
            "GOOD: import defusedxml.ElementTree as ET; ET.parse(untrusted_xml)\n"
            "Используй defusedxml для недоверенного XML (отключает внешние сущности)."
        ),
        "csharp": (
            "BAD:  new XmlDocument().Load(stream)   // DTD/внешние сущности\n"
            "GOOD: var s = new XmlReaderSettings { DtdProcessing = DtdProcessing.Prohibit,\n"
            "        XmlResolver = null }; XmlReader.Create(stream, s);\n"
            "Запрети DTD и обнули XmlResolver для недоверенного XML."
        ),
    },
    "weak_crypto": {
        "python": (
            "BAD:  hashlib.md5(data)  / hashlib.sha1(data)   // слабые\n"
            "GOOD: hashlib.sha256(data)   // для паролей — bcrypt/argon2\n"
            "MD5/SHA1 сломаны для безопасности; sha256+ для целостности, bcrypt/argon2 для паролей."
        ),
        "csharp": (
            "BAD:  MD5.Create() / SHA1.Create() / DES\n"
            "GOOD: SHA256.Create(); для шифрования — AES; для паролей — PBKDF2/bcrypt/argon2.\n"
            "Замени MD5/SHA1/DES/RC4 на SHA256+/AES."
        ),
        "cpp": (
            "Замени MD5/SHA1 на SHA-256+ (целостность) или библиотечный bcrypt/argon2 (пароли); "
            "не самопиши крипту."
        ),
    },
    "buffer_overflow": {
        "cpp": (
            "BAD:  strcpy(dst, src); sprintf(buf, \"%s\", s); gets(buf);\n"
            "GOOD: используй границы: snprintf(buf, sizeof buf, ...), strncpy/strlcpy,\n"
            "      std::string/std::vector вместо сырых буферов; никогда gets().\n"
            "Всегда ограничивай размер записи размером буфера."
        ),
    },
    "format_string": {
        "cpp": (
            "BAD:  printf(user_input);              // формат из ввода\n"
            "GOOD: printf(\"%s\", user_input);\n"
            "Никогда не передавай пользовательский ввод как строку формата — только через %s."
        ),
    },
}


# ---------------------------------------------------------------------------
# FIX_RECIPES — карточки-рецепты по коду ошибки (2026-07-10, идея Алекса).
# «Разжевать мастеру»: для кодов с КАНОНИЧЕСКИМ фиксом суём в промт готовый
# образец было→стало + правило. Слабая/локальная модель копирует паттерн, а не
# думает с нуля → выше yield БЕЗ смены модели. Обобщение SECURITY_EXAMPLES на
# обычные баг-коды. Код обычно однозначно задаёт язык (CA*/CS* = C#,
# cppcheck::*/bugprone-* = C++), поэтому ключ — сам код. Только КАНОНИЧЕСКИЕ
# фиксы (контекст-зависимые — dead-code-логика — не сюда, там модель думает).
FIX_RECIPES: dict = {
    # ---- C# (Roslyn/NetAnalyzers/компилятор) ----
    "CS8602": (
        "BAD:  return s.ToUpper();            // s может быть null (CS8602)\n"
        "GOOD: return s?.ToUpper() ?? string.Empty;\n"
        "Добавь null-проверку перед разыменованием: `x?.M()`, или "
        "`if (x is null) return/throw;`. НЕ глуши через `!` (null-forgiving), "
        "если не можешь доказать non-null."
    ),
    "CS8604": (
        "BAD:  Use(s);                        // s может быть null-аргументом\n"
        "GOOD: if (s is null) throw new ArgumentNullException(nameof(s)); Use(s);\n"
        "Проверь аргумент на null перед передачей туда, где null недопустим."
    ),
    "CS8618": (
        "BAD:  public string Name { get; set; }   // non-nullable не инициализирован\n"
        "GOOD: public string Name { get; set; } = string.Empty;   // или сделать required/nullable\n"
        "Инициализируй non-nullable поле/свойство (в конструкторе, дефолтом или "
        "`required`), либо пометь тип как nullable (`string?`) если null валиден."
    ),
    "CA2000": (
        "BAD:  var r = new StreamReader(path); return r.ReadLine();   // утечка (CA2000)\n"
        "GOOD: using var r = new StreamReader(path); return r.ReadLine();\n"
        "Оборачивай IDisposable в `using var x = ...;` или `using (var x = ...) { }` "
        "чтобы гарантировать Dispose. НЕ добавляй комментарий-подавление."
    ),
    "CA1806": (
        "BAD:  input.Trim();                  // результат выброшен (CA1806)\n"
        "GOOD: input = input.Trim();          // или return input.Trim();\n"
        "Используй возвращаемое значение: методы вроде Trim/Replace/Select "
        "возвращают НОВЫЙ объект, исходный не меняется. Присвой или верни результат."
    ),
    "CA2201": (
        "BAD:  throw new Exception(\"bad arg\");\n"
        "GOOD: throw new ArgumentException(\"bad arg\", nameof(arg));\n"
        "Бросай КОНКРЕТНЫЙ тип (ArgumentException/InvalidOperationException/...), "
        "не базовый Exception/SystemException/ApplicationException."
    ),
    "CA1508": (
        "Условие всегда true/false (мёртвый код). Если это ошибка логики — почини "
        "условие (напр. проверял не ту переменную). Если условие действительно "
        "лишнее — удали его и мёртвую ветку. НЕ подавляй предупреждение."
    ),
    # ---- C++ (cppcheck / clang-tidy) ----
    "cppcheck::nullPointer": (
        "BAD:  return p->value;               // p может быть null\n"
        "GOOD: if (p == nullptr) return {}; return p->value;\n"
        "Проверь указатель на nullptr перед разыменованием."
    ),
    "cppcheck::nullPointerRedundantCheck": (
        "Указатель разыменован, а ПОТОМ проверен на null — переставь проверку "
        "ПЕРЕД разыменованием (или убери лишнее разыменование)."
    ),
    "cppcheck::uninitMemberVar": (
        "BAD:  class C { int x; C() {} };     // x не инициализирован\n"
        "GOOD: class C { int x = 0; C() {} }; // или C() : x(0) {}\n"
        "Инициализируй член в списке инициализации конструктора или при объявлении."
    ),
    "cppcheck::noExplicitConstructor": (
        "BAD:  class C { C(int n); };\n"
        "GOOD: class C { explicit C(int n); };\n"
        "Добавь `explicit` к конструктору с одним аргументом — блокирует неявные "
        "конверсии-ловушки."
    ),
    "cppcheck::invalidPrintfArgType_sint": (
        "printf-спецификатор не совпал с типом аргумента. Приведи в соответствие: "
        "%d — int, %u — unsigned, %ld — long, %zu — size_t, %s — char*."
    ),
    "bugprone-switch-missing-default-case": (
        "BAD:  switch (x) { case 0: ...; case 1: ...; }\n"
        "GOOD: switch (x) { case 0: ...; case 1: ...; default: break; }\n"
        "Добавь `default:` в switch по не-enum значению."
    ),
    "bugprone-unchecked-string-to-number-conversion": (
        "BAD:  int n = atoi(s);               // ошибка не проверяется\n"
        "GOOD: используй strtol с проверкой errno/endptr (C) или std::from_chars "
        "(C++), проверь успех конверсии."
    ),
    # ---- C++ канонические (из NR C++-серий 2026-07-10) ----
    "cppcheck::bitwiseOnBoolean": (
        "BAD:  if (a & b)                     // побитовое И над bool\n"
        "GOOD: if (a && b)                    // логическое И\n"
        "Для bool используй логические && / ||, не побитовые & / | "
        "(если это не намеренная битовая маска)."
    ),
    "cppcheck::returnTempReference": (
        "BAD:  const std::string& f() { return std::string(\"x\"); }  // висячая ссылка\n"
        "GOOD: std::string f() { return std::string(\"x\"); }         // по значению\n"
        "Не возвращай ссылку на временный/локальный объект — верни ПО ЗНАЧЕНИЮ."
    ),
    "cppcheck::uselessOverride": (
        "override-метод дословно повторяет базовый (только вызывает Base::m()) — "
        "удали его, наследование само подхватит базовую реализацию."
    ),
    "cppcheck::nullPointerOutOfMemory": (
        "BAD:  T* p = new(std::nothrow) T; p->x = 1;   // p может быть null\n"
        "GOOD: T* p = new(std::nothrow) T; if (p) p->x = 1;\n"
        "Проверь результат аллокации (nothrow-new/malloc) перед использованием."
    ),
    "cppcheck::nullPointerOutOfResources": (
        "Результат аллокации/открытия ресурса может быть null при нехватке "
        "ресурсов — проверь перед разыменованием."
    ),
    "bugprone-macro-parentheses": (
        "BAD:  #define SQ(x) x*x                // SQ(a+b) => a+b*a+b\n"
        "GOOD: #define SQ(x) ((x)*(x))\n"
        "Оборачивай каждый параметр макроса И всё тело в скобки."
    ),
    "bugprone-implicit-widening-of-multiplication-result": (
        "BAD:  long bytes = width * height;    // int*int переполнится ДО расширения\n"
        "GOOD: long bytes = static_cast<long>(width) * height;\n"
        "Приведи операнд к широкому типу ДО умножения, а не результат после."
    ),
    "bugprone-signed-char-misuse": (
        "BAD:  int v = c;                       // c это char, может быть отрицательным\n"
        "GOOD: int v = static_cast<unsigned char>(c);\n"
        "Приводи char к unsigned char перед использованием как индекс/код."
    ),
    "bugprone-empty-catch": (
        "BAD:  try { risky(); } catch (...) {}  // ошибка проглочена\n"
        "GOOD: логируй, обработай или проброс: catch (const std::exception& e) "
        "{ log(e.what()); throw; }\n"
        "Не глотай исключение молча."
    ),
    # ---- C# дополнительные канонические ----
    "CA2213": (
        "BAD:  class C : IDisposable { Stream _s = ...; void Dispose() {} }\n"
        "GOOD: void Dispose() { _s?.Dispose(); }\n"
        "Освобождай в Dispose() поля-члены, которые сам создал и которые "
        "IDisposable."
    ),
    "CA2016": (
        "BAD:  await InnerAsync();              // не передан CancellationToken\n"
        "GOOD: await InnerAsync(cancellationToken);\n"
        "Пробрасывай CancellationToken в вызываемые async-методы, которые его "
        "принимают."
    ),
    "CA1827": (
        "BAD:  if (items.Count() > 0)          // LINQ Count() перечисляет всё\n"
        "GOOD: if (items.Any())\n"
        "Для проверки непустоты используй Any(), не Count()>0."
    ),
    "CA1829": (
        "BAD:  int n = items.Count();          // LINQ по коллекции со свойством\n"
        "GOOD: int n = items.Count;            // или .Length для массива\n"
        "Используй свойство Count/Length, не LINQ-метод Count()."
    ),
    "CA2245": (
        "BAD:  this.X = this.X;                 // само-присваивание\n"
        "Убери само-присваивание или исправь опечатку (вероятно имелось в виду "
        "другое поле/параметр)."
    ),
    # ---- Rust (clippy) ----
    "clippy::needless_return": (
        "BAD:  fn f() -> i32 { return 1; }\n"
        "GOOD: fn f() -> i32 { 1 }\n"
        "Убери `return` у последнего выражения — в Rust оно и так возвращается."
    ),
    "clippy::redundant_clone": (
        "BAD:  let b = a.clone(); use_owned(b);   // a дальше не нужен\n"
        "GOOD: let b = a; use_owned(b);           // move вместо clone\n"
        "Убери лишний .clone(), если оригинал больше не используется — передай "
        "владение (move)."
    ),
    "clippy::needless_borrow": (
        "BAD:  foo(&&x)  / foo(&x) где foo берёт по значению/уже ссылку\n"
        "GOOD: foo(x) / foo(&x)\n"
        "Убери лишний & — компилятор не делает авто-разыменование здесь."
    ),
    "clippy::clone_on_copy": (
        "BAD:  let y = x.clone();               // x: Copy-тип (i32/…)\n"
        "GOOD: let y = x;                        // Copy копируется сам\n"
        "Для Copy-типов .clone() лишний."
    ),
    "clippy::map_clone": (
        "BAD:  it.map(|x| x.clone())\n"
        "GOOD: it.cloned()                       // или .copied() для Copy\n"
        "Используй .cloned()/.copied() вместо map с clone."
    ),
    "clippy::or_fun_call": (
        "BAD:  opt.unwrap_or(expensive())        // expensive() зовётся всегда\n"
        "GOOD: opt.unwrap_or_else(|| expensive())\n"
        "Ленивый вариант (*_or_else) не вычисляет аргумент, если не нужен."
    ),
    "clippy::redundant_field_names": (
        "BAD:  Foo { x: x, y: y }\n"
        "GOOD: Foo { x, y }\n"
        "Используй сокращённую инициализацию поля, когда имя совпадает."
    ),
    "clippy::unnecessary_cast": (
        "BAD:  let n = 5i32 as i32;             // приведение к тому же типу\n"
        "GOOD: let n = 5i32;\n"
        "Убери приведение as, если тип уже такой."
    ),
    "clippy::useless_conversion": (
        "BAD:  foo(x.into())                    // x уже нужного типа\n"
        "GOOD: foo(x)\n"
        "Убери лишний .into()/.try_into(), если тип уже совпадает."
    ),
    "clippy::len_zero": (
        "BAD:  if v.len() == 0\n"
        "GOOD: if v.is_empty()\n"
        "Используй .is_empty() вместо сравнения длины с 0."
    ),
    "clippy::redundant_pattern_matching": (
        "BAD:  if let Ok(_) = res { }\n"
        "GOOD: if res.is_ok() { }\n"
        "Используй .is_ok()/.is_some() вместо `if let Ok(_)/Some(_)`."
    ),
    "clippy::single_char_pattern": (
        "BAD:  s.split(\"x\")                    // строка из 1 символа\n"
        "GOOD: s.split('x')                      // char быстрее\n"
        "Для паттерна из одного символа используй char-литерал, не строку."
    ),
    # ---- Python (канонические, LLM-путь) ----
    "E711": (
        "BAD:  if x == None:\n"
        "GOOD: if x is None:\n"
        "Сравнивай с None через is / is not, не == / !=."
    ),
    "E712": (
        "BAD:  if flag == True:  /  if flag == False:\n"
        "GOOD: if flag:          /  if not flag:\n"
        "Не сравнивай с True/False явно."
    ),
    "B006": (
        "BAD:  def f(items=[]):                 // общий изменяемый дефолт!\n"
        "GOOD: def f(items=None):\n"
        "          if items is None: items = []\n"
        "Не используй изменяемый объект ([]/{}) как дефолт аргумента — он один "
        "на все вызовы. Дефолт None + инициализация внутри."
    ),
    "W605": (
        "BAD:  re.compile('\\d+')               // невалидный escape в обычной строке\n"
        "GOOD: re.compile(r'\\d+')              // raw-строка\n"
        "Для регэкспов/escape используй raw-строку r'...'."
    ),
}


_AUTO_RECIPES_CACHE: dict = {}
_AUTO_RECIPES_LOADED = [False]


def _load_auto_recipes() -> dict:
    """Авто-рецепты из runtime/auto_recipes.json (собраны recipe_harvester из
    ПОДТВЕРЖДЁННЫХ побед). Кэш на процесс. Отсутствие файла — не ошибка."""
    if _AUTO_RECIPES_LOADED[0]:
        return _AUTO_RECIPES_CACHE
    _AUTO_RECIPES_LOADED[0] = True
    try:
        import json as _json
        from pathlib import Path as _P
        p = _P(__file__).resolve().parents[2] / "runtime" / "auto_recipes.json"
        if p.is_file():
            data = _json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _AUTO_RECIPES_CACHE.update(data)
    except Exception:
        pass
    return _AUTO_RECIPES_CACHE


def get_fix_recipe(error_code: str, language: str = "") -> str:
    """Карточка-рецепт (было→стало + правило) по коду ошибки, или ''.
    Ручные FIX_RECIPES — ПЕРВЫМИ (высший траст, курируемые); авто-рецепты из
    подтверждённых побед — фолбэком (2026-07-10, идея Алекса)."""
    code = (error_code or "").strip()
    manual = FIX_RECIPES.get(code, "")
    if manual:
        return manual
    auto = _load_auto_recipes().get(code)
    if isinstance(auto, dict) and auto.get("recipe"):
        return auto["recipe"]
    return ""


def get_security_example(error_code: str, language: str) -> str:
    """before→after пример для security-кода под конкретный язык (или '')."""
    entry = SECURITY_EXAMPLES.get(error_code or "")
    if not entry:
        return ""
    lang = (language or "").lower()
    if lang in ("python", "py"):
        lang = "python"
    elif lang in ("rust", "rs"):
        lang = "rust"
    elif lang in ("javascript", "js", "typescript", "ts"):
        lang = "javascript"
    elif lang in ("csharp", "cs", "c#"):
        lang = "csharp"
    elif lang in ("cpp", "c++", "cxx", "cc", "c"):
        lang = "cpp"
    return entry.get(lang, "")


def get_constraints(error_code: str) -> Tuple[List[str], List[str]]:
    entry = ERROR_CONSTRAINTS.get(error_code or "")
    if not entry:
        return [], []
    return list(entry.get("do", [])), list(entry.get("dont", []))


def is_known(error_code: str) -> bool:
    return bool(error_code) and error_code in ERROR_CONSTRAINTS
