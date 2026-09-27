# Кнопка «Инструкция» в админке Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement natively task-by-task with test-driven-development (this plan is small and interdependent enough that subagent-driven-development would add overhead without benefit).

**Goal:** заказчик (владелец или роль `admin`) нажимает «📖 Инструкция» в главном меню админки и получает
актуальный PDF с инструкцией по пользованию и историей изменений.

**Architecture:** статический PDF, закоммиченный в `bot/assets/` (Dockerfile копирует только `bot/`, так
что файл вне неё на сервер не попадёт), собранный из markdown-источника в `docs/client-guide/`. Кнопка и
обработчик в `bot/handlers/admin/__init__.py` копируют уже существующий в проекте паттерн — «Скачать
таблицу» (`a:stat:file` в `bot/handlers/admin/stats.py`): `FSInputFile` + `call.message.answer_document`.

**Tech Stack:** Python 3.11, aiogram 3.15, pytest (`./.venv-agent/bin/python -m pytest -q`); генерация
PDF — reportlab (Platypus) с шрифтом DejaVu Sans (кириллица), выполняется один раз при подготовке
контента, на сервер не переносится (только результат — PDF-файл).

**Spec:** `docs/superpowers/specs/2026-09-28-client-guide-pdf-design.md`

## Global Constraints

- Кнопка видна и владельцу, и роли `admin` (весь роутер `bot/handlers/admin/__init__.py` уже под
  `AdminFilter`) — отдельного `require_owner` не нужно, это не владельческое действие (спека §3).
- PDF-файл лежит строго внутри `bot/` (`bot/assets/client-guide.pdf`) — `docs/` в Docker-образ не
  попадает (спека §2, обнаружено в `Dockerfile`: `COPY bot ./bot`).
- Markdown-источник (`docs/client-guide/client-guide.md`) в рантайме бота не участвует — только для
  моего редактирования, PDF генерируется из него отдельно и коммитится вместе с исходником (спека §5).
- Содержание PDF: шапка с датой обновления, короткое описание бота, разделы по Воронке/Рассылкам/
  Статистике/Настройкам, история изменений по датам. Без раздела о разграничении прав admin/владелец
  (спека §2).
- Отсутствие файла на диске не должно ронять хендлер — вежливый `show_alert=True` (спека §4).

## Review Focus

- Роль `stats` не должна получить документ даже прямым вызовом `a:guide` — весь роутер уже под
  `AdminFilter`, но стоит явно проверить, а не полагаться на то, что «наверное, сработает».
- Отсутствующий на диске файл (например, забыли закоммитить после переноса) — хендлер не должен упасть
  необработанным исключением, а вежливо ответить алертом.
- Кнопка не должна быть спрятана от роли `admin` по ошибке — это лёгкая ошибка copy-paste, раз рядом в
  том же меню есть паттерн владельческих проверок из темы A.
- PDF должен реально открываться и содержать кириллический текст без «квадратиков» — типичная проблема
  reportlab без зарегистрированного шрифта с поддержкой кириллицы.
- Файл должен физически лежать там, откуда его подхватит Docker-образ (`bot/assets/`, не `docs/`) — если
  по ошибке положить в `docs/`, фича тихо не будет работать в проде, хотя все тесты (которые гоняются на
  файлах из репозитория напрямую, не через собранный образ) всё равно пройдут.

---

## Task 1: Контент — markdown-источник и сгенерированный PDF

**Files:**
- Create: `docs/client-guide/client-guide.md`
- Create: `bot/assets/client-guide.pdf` (бинарный, сгенерированный)
- Create: `docs/client-guide/generate_pdf.py` — скрипт генерации (запускается вручную при каждом
  обновлении контента, в рантайме бота не используется)

**Interfaces:**
- Produces: файл `bot/assets/client-guide.pdf`, который потребляет Task 2 (путь к нему захардкожен в
  обработчике как константа — см. Task 2).
- Consumes: ничего из предыдущих задач.

- [ ] **Step 1: Написать `docs/client-guide/client-guide.md`.**

  Простая, предсказуемая для парсинга структура (её и разбирает `generate_pdf.py` — заголовки на
  строках, начинающихся с `#`/`##`, обычный текст абзацами, списки строками с `- `):
  - `# <название бота>` и сразу под ним `Обновлено: 28.09.2026`.
  - `## О боте` — 2-3 предложения, что делает бот (переиспользовать формулировки из уже опубликованной
    карточки функционала бота).
  - `## Как пользоваться админ-панелью` с подразделами `### 🧭 Воронка`, `### 📤 Рассылки`,
    `### 📊 Статистика`, `### ⚙️ Настройки` — по 2-4 предложения на раздел, что в нём настраивается,
    на основе того, что реально уже есть в `bot/handlers/admin/flow.py`, `broadcast.py`, `stats.py`,
    `settings.py` (главное меню, `flow_status`, разделы `MENU` в `bot/handlers/admin/__init__.py`).
  - `## История изменений` — список `- ДД.ММ.ГГГГ: <что изменилось>`, первая запись —
    `- 28.09.2026: добавлена кнопка «📖 Инструкция» с этим документом`.

- [ ] **Step 2: Написать `docs/client-guide/generate_pdf.py`.**

  Минимальный парсер (не полноценный markdown — ровно то, что покрывает Step 1's структура) +
  генерация через reportlab Platypus:

  ```python
  """Перегенерировать bot/assets/client-guide.pdf из client-guide.md.
  Запуск: python docs/client-guide/generate_pdf.py
  """
  from pathlib import Path
  from reportlab.lib.pagesizes import A4
  from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
  from reportlab.pdfbase import pdfmetrics
  from reportlab.pdfbase.ttfonts import TTFont
  from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem

  FONT_PATH = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
  FONT_BOLD_PATH = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
  MD_PATH = Path(__file__).with_name("client-guide.md")
  PDF_PATH = Path(__file__).resolve().parents[2] / "bot" / "assets" / "client-guide.pdf"
  ```

  Регистрирует `DejaVuSans`/`DejaVuSans-Bold` через `pdfmetrics.registerFont(TTFont(...))`, строит
  стили (`Title`, `Heading1`, `Heading2`, `Normal`) на базе этого шрифта (иначе кириллица в стандартных
  reportlab-шрифтах не отрисуется). Построчно разбирает `client-guide.md`: `# `→ заголовок документа,
  `## `→ `Heading1`, `### `→ `Heading2`, `- `→ пункт списка (`ListFlowable`), пустая строка → разделитель
  абзацев, всё остальное → `Paragraph` обычным стилем. Собирает `story` и вызывает
  `SimpleDocTemplate(str(PDF_PATH), pagesize=A4).build(story)`.

- [ ] **Step 3: Сгенерировать PDF и проверить его глазами / программно.**

  Run: `python3 docs/client-guide/generate_pdf.py`
  Expected: файл `bot/assets/client-guide.pdf` создан.

  Затем прочитать текст обратно и убедиться, что кириллица не превратилась в мусор и разделы на месте:

  ```python
  from pypdf import PdfReader
  text = "\n".join(p.extract_text() for p in PdfReader("bot/assets/client-guide.pdf").pages)
  assert "Воронка" in text and "История изменений" in text
  ```

  Это разовая ручная проверка (или маленький скрипт), не часть `pytest`-сьюта — содержание документа не
  покрывается автотестами (спека §6).

- [ ] **Step 4: Commit**

  ```bash
  git add docs/client-guide/client-guide.md docs/client-guide/generate_pdf.py bot/assets/client-guide.pdf
  git commit -m "docs: контент и генератор PDF-инструкции для админки"
  ```

## Task 2: Кнопка и обработчик в главном меню админки

**Files:**
- Modify: `bot/handlers/admin/__init__.py`
- Test: `tests/test_e2e.py`

**Interfaces:**
- Consumes: `bot/assets/client-guide.pdf` (Task 1) — путь к нему как модульная константа.
- Produces: ничего, что использовали бы другие задачи — это последняя задача плана.

- [ ] **Step 1: Написать тесты** в `tests/test_e2e.py` (по образцу
  `test_stats_file_button_sends_document`):

  ```python
  async def test_guide_button_sends_document_to_owner(stack):
      dp, bot, session, deps = stack
      await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
      session.requests.clear()
      await feed(dp, bot, callback=make_callback("a:guide", user_id=ADMIN_ID))
      assert "SendDocument" in session.names()


  async def test_guide_button_sends_document_to_admin_role_client(stack):
      dp, bot, session, deps = stack
      await deps.access.add(VIEWER_ID, "Клиент", None, role="admin")
      await feed(dp, bot, message=make_message("/admin", user_id=VIEWER_ID))
      session.requests.clear()
      await feed(dp, bot, callback=make_callback("a:guide", user_id=VIEWER_ID))
      assert "SendDocument" in session.names()


  async def test_guide_button_missing_file_shows_alert_not_crash(stack, monkeypatch):
      dp, bot, session, deps = stack
      import bot.handlers.admin as admin_pkg
      monkeypatch.setattr(admin_pkg, "GUIDE_PDF_PATH", admin_pkg.GUIDE_PDF_PATH.with_name("missing.pdf"))
      await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
      session.requests.clear()
      await feed(dp, bot, callback=make_callback("a:guide", user_id=ADMIN_ID))
      assert "SendDocument" not in session.names()
      alerts = [r for r in session.calls("AnswerCallbackQuery") if r.show_alert]
      assert alerts


  async def test_guide_button_rejected_for_stats_role_client(stack):
      """Review Focus: роль stats не проходит AdminFilter — прямой вызов a:guide не должен
      сработать, а не просто «наверное, сработает, раз кнопка спрятана»."""
      dp, bot, session, deps = stack
      await deps.access.add(VIEWER_ID, "Клиент", None, role="stats")
      session.requests.clear()
      await feed(dp, bot, callback=make_callback("a:guide", user_id=VIEWER_ID))
      assert "SendDocument" not in session.names()
  ```

  (`VIEWER_ID`, `ADMIN_ID`, `stack`, `feed`, `make_message`, `make_callback` — уже существующие фикстуры
  и хелперы файла, используются ровно как в соседних тестах доступа из темы A.)

- [ ] **Step 2: Запустить тесты, убедиться что все четыре падают** (кнопки и хендлера ещё нет — тест на
  роль `stats` тоже упадёт: сейчас на `a:guide` нет вообще никакого обработчика, значит `SendDocument`
  и так не будет, но `AdminFilter` в текущем виде это не пока проверяет напрямую для несуществующего
  колбэка — фактическая причина падения не важна, важно что после Step 3 все четыре проходят по
  правильной причине).

  Run: `./.venv-agent/bin/python -m pytest -q tests/test_e2e.py -k guide_button -v`
  Expected: FAIL для первых трёх (нет хендлера); четвёртый (`stats_role`) может неожиданно PASS уже
  сейчас — если так, это нормально (обработчика ещё нет ни для кого), просто убедиться, что после Step 3
  он продолжает проходить и по правильной причине (роутер отфильтровал, а не «хендлера нет вообще»).

- [ ] **Step 3: Добавить кнопку, константу пути и обработчик в `bot/handlers/admin/__init__.py`.**

  - В `MENU` добавить пятой строкой `[("📖 Инструкция", "a:guide")]`.
  - Добавить `from pathlib import Path` и `from aiogram.types import FSInputFile` к импортам.
  - Добавить модульную константу:
    `GUIDE_PDF_PATH = Path(__file__).resolve().parents[2] / "assets" / "client-guide.pdf"`
  - Добавить обработчик:

    ```python
    @router.callback_query(F.data == "a:guide")
    async def cb_guide(call: CallbackQuery) -> None:
        if not GUIDE_PDF_PATH.exists():
            await call.answer("Файл инструкции не найден, сообщите разработчику", show_alert=True)
            return
        await call.message.answer_document(FSInputFile(GUIDE_PDF_PATH, filename="Инструкция.pdf"))
        await call.answer()
    ```

- [ ] **Step 4: Тесты проходят.**

  Run: `./.venv-agent/bin/python -m pytest -q tests/test_e2e.py -k guide_button -v`
  Expected: PASS (4 passed)

- [ ] **Step 5: Полный прогон.**

  Run: `./.venv-agent/bin/python -m pytest -q`
  Expected: PASS (весь набор, без регрессий)

- [ ] **Step 6: Обновить `CLAUDE.md`** — короткая запись о кнопке «Инструкция», где лежит PDF/markdown-
  источник, и напоминание себе на будущее: обновлять `docs/client-guide/client-guide.md` +
  перегенерировать PDF при каждом функциональном изменении бота (ссылка на спеку).

- [ ] **Step 7: Commit**

  ```bash
  git add bot/handlers/admin/__init__.py tests/test_e2e.py CLAUDE.md
  git commit -m "feat: кнопка «Инструкция» в админке — отправляет PDF с инструкцией и историей изменений"
  ```
