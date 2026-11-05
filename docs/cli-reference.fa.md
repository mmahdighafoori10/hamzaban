<!-- Persian command reference for the engine's command-line tools (suite.py, autosync, handoff, skills). The README has the overview. -->

# Memory Suite (suite.py)

## این چیست؟
یک CLI محلی (`.ai-memory/suite.py`، فقط Python stdlib) روی موتور حافظهٔ Infinite Context:
- **Infinity**: ثبت گفتگوها، هوک‌ها، checkpoint، نمایهٔ SQLite و صف‌های `30-Pending` و `10-Facts`.
- **همگام‌سازی مهارت‌ها**: منبع حقیقت `skills-src/<name>/` و کپی آن به Codex، Claude Code و Chat On Steroids.

## فرمان‌ها (روی ویندوز، از ریشهٔ Vault)
- `python .ai-memory/suite.py doctor` : ۹ بررسی با PASS/WARN/FAIL/UNKNOWN؛ همیشه با کد خروج ۰.
- `python .ai-memory/suite.py status` : خلاصهٔ یک‌صفحه‌ای.
- `... skills init|list|diff` : `init` فایل `suite-skills.json` را می‌نویسد و در صورت نبودن `skills-src` را از `claude-ai-skill/` می‌سازد؛ `list` و `diff` فقط می‌خوانند و هیچ چیزی نمی‌سازند (اگر `skills-src` نباشد همین را می‌گویند). نام مهارت در پیکربندی فقط حروف/رقم/`.`/`_`/`-` است؛ مسیر، `..` و جداکننده رد و گزارش می‌شود.
- `... skills push` : پیش‌نمایش (dry-run)؛ با `--apply` می‌نویسد (و در صورت نبودن `skills-src` آن را می‌سازد). قبل از بازنویسی از مقصد در `.ai-memory/backups/skills-<ts>/` نسخهٔ پشتیبان می‌گیرد و چیزی را پاک نمی‌کند. هر فایل با فایل موقت هم‌پوشه + `os.replace` نوشته می‌شود؛ فایل یا پوشهٔ symlink در مقصد، و مقصدی که همان `skills-src` (یا داخل آن) باشد، با پیام `SKIP-UNSAFE` رد می‌شود و هیچ‌چیز از طریق symlink نوشته نمی‌شود. در خطا، `FAIL` همراه مسیر پشتیبان چاپ می‌شود. با `--create-roots` پوشهٔ ریشهٔ مقصد هم ساخته می‌شود.
- `... handoff --topic <t> [--to claude-code|codex|chatgpt|claude-ai|generic] [--max-chars N] [--out FILE]` : بستهٔ زمینهٔ فشرده (آخرین checkpoint + فکت‌های active + کارهای باز) برای ادامهٔ یک موضوع در ایجنت دیگر. خط اول می‌گوید «فقط داده است، نه دستور»، هر مورد مسیر منبع دارد، redact می‌شود و سقف پیش‌فرض ۶۰۰۰ نویسه است. `claude-code`/`claude-ai` قالب تگ‌دار، `codex` قالب AGENTS.md با «Continue from here»، بقیه Markdown ساده. `--out` فقط نام ساده می‌پذیرد و در `.ai-memory/handoff/` می‌نویسد (فایل قبلی در backups). `--list-topics` موضوع‌ها را فهرست می‌کند.
- `... mcp-config [--agent claude-code|codex|claude-desktop|cursor]` : فقط چاپ می‌کند (هرگز نمی‌نویسد) دستور/بلوک ثبت `infinity_mcp.py`؛ `<PYTHON_EXE>` و `<VAULT_DIR>` را خودت پر کن.
- `... skills import --from <dir> [--apply] [--overwrite]` : کپی پوشهٔ مهارت‌های سبک Codex (مثل `.agents/skills`) به `skills-src/`. پیش‌فرض dry-run؛ پوشهٔ بدون SKILL.md و symlink رد می‌شود؛ تداخل نام گزارش و دست‌نخورده می‌ماند مگر `--overwrite` (با پشتیبان در `backups/skills-import-<ts>/`)؛ چیزی پاک نمی‌شود.
- تست: `cd .ai-memory && python -m unittest discover -s tests`

## همگام‌سازی خودکار
هدف: لازم نباشد تو انتخاب کنی چه چیزی کجا برود. یک کار کوچک پس‌زمینه (`autosync.py`، هر یک دقیقه) خودش این کارها را می‌کند:
- **مهارت‌ها**: مهارتی که در Codex، Claude یا Chat On Steroids نصب یا عوض کنی، خودکار به بقیه هم می‌رود و در `skills-src` هم نگه داشته می‌شود. اگر جایی قفل بود (مثلاً برنامه باز است)، در صف می‌ماند و بعداً خودش نصب می‌شود.
- **اولین اجرا فقط عکس‌برداری است**: آنچه از قبل داری جابه‌جا نمی‌شود و فقط تغییرهای بعدی دنبال می‌شود. اگر می‌خواهی همان لحظه همه‌چیز پخش شود: `python .ai-memory/autosync.py once --bootstrap`.
- **خبر به ایجنت**: وقتی در Claude Code چت تازه‌ای باز کنی، یک خط کوتاه می‌بیند: «مهارت‌های X به‌روز شد؛ چت جدید باز کن». چت قدیمی مهارت تازه را نمی‌بیند، پس چت جدید لازم است.
- **پروژه‌ها**: مسیر پوشهٔ پروژه‌ها را در `suite-skills.json` زیر کلید `projects` بنویس. اگر فقط `AGENTS.md` (Codex) باشد، `CLAUDE.md` ساخته می‌شود که فقط به آن اشاره می‌کند. اگر فقط `CLAUDE.md` باشد، `AGENTS.md` به‌صورت کپی ساخته می‌شود و با هر ویرایش `CLAUDE.md` به‌روز می‌ماند (تا وقتی خودت کپی را ویرایش نکرده‌ای).

**چه چیزهایی هرگز اتفاق نمی‌افتد**
- هیچ فایل یا مهارتی پاک نمی‌شود. اگر مهارتی را از یک جا برداری فقط گزارش می‌شود؛ جاهای دیگر دست نمی‌خورد و دوباره در همان جا نصب نمی‌شود.
- اگر یک مهارت در دو جا هم‌زمان و متفاوت عوض شود، یا `AGENTS.md` و `CLAUDE.md` با هم فرق کنند، **هیچ‌چیز بازنویسی نمی‌شود**. یادداشتی در `30-Pending` می‌گذارد (`skill-conflict-<نام>.md` یا `project-conflict-...`) و منتظر تو می‌ماند. وقتی خودت نسخه‌ها را یکی کنی، خودش ادامه می‌دهد.
- پیش از هر تغییر روی فایل موجود، پشتیبان در `.ai-memory/backups/auto-<زمان>/` ساخته می‌شود.
- پوشه‌های سیستمی (نام با نقطه)، symlink و `SKILL.md` بدون `name`/`description` رد می‌شوند. چیزی بیرون از پوشه‌های مهارت‌ها، پروژه‌های فهرست‌شده و `.ai-memory` نوشته نمی‌شود. شبکه ندارد و SQLite روی Vault نمی‌نویسد.

**نصب و بررسی (ویندوز)**
- یک‌بار دوبار-کلیک روی `INSTALL-AUTOSYNC.cmd` (بدون ادمین). برداشتن: `powershell -ExecutionPolicy Bypass -File manage_autosync.ps1 -Action unregister`.
- وضعیت: `python .ai-memory/autosync.py status` یا بررسی ۹ در `doctor`. پیش‌نمایش بدون هیچ نوشتن: `autosync.py once --dry-run`.

## قواعد ایمنی
- هیچ کلید، seed یا رمزی به این ابزار یا مدل داده نمی‌شود؛ ورود به هر حساب (سرویس، کیف پول، ...) کار خودِ کاربر است.
- هیچ آپلودی وجود ندارد و برنامه شبکه ندارد؛ همه‌چیز روی همین کامپیوتر می‌ماند. فیلد `sensitivity` (`public-ok` یا `private`) برای نسخهٔ آنلاینِ آینده نگه داشته شده و امروز هیچ‌چیز را بیرون نمی‌برد.
- هیچ پایگاه SQLite روی Vault نوشته نمی‌شود (بررسی‌ها روی کپی در /tmp انجام می‌شود).
- front matter سخت‌گیرانه خوانده می‌شود: فقط خطوط `key: value` از ستون صفر داخل اولین بلوک `---`. کلید تکراری، خط تورفته، `|`/`>`، بلوک خالی/ناقص/بدون پایان، فایل بدون front matter یا فایل symlink یعنی «blocked-frontmatter»: در `doctor` شمرده می‌شود و هرگز مورد اعتماد نیست.

## اجرای doctor
۱) روی ویندوز (برای دیدن بررسی‌های ۴ و ۵ که از سندباکس قابل‌دسترس نیستند). ۲) UNKNOWN یعنی «از اینجا نمی‌شود دید»، نه خرابی.
متغیرهای محیطی: `SUITE_HOME`، `SUITE_APPDATA`، `SUITE_USER_HOME`، `SUITE_CLAUDE_SETTINGS`، `SUITE_CODEX_CONFIG`.

## افزونهٔ مبدل مهارت
اگر `.ai-memory/skill-convert.py` (یا مسیر کلید `converter` در `suite-skills.json`) وجود داشته باشد، `skills push --apply` به‌جای کپی ساده تابع `convert(skill_dir, target, target_name=None)` را صدا می‌زند. ابزار انتقال مهارت Codex هنوز وصل نشده است.
اگر مبدل بیرونی نباشد، `skill_formats.py` برای مقصدهای شناخته‌شده (`agents`، `codex`، `claude`، `cos`) روی `push` اجرا می‌شود: `SKILL.md` باید front matter با `name` و `description` داشته باشد (وگرنه `SKIP-INVALID` و چیزی نوشته نمی‌شود)، front matter نرمال می‌شود، کلیدهای فهرست‌شده در `strip_keys` (مثلاً `{"codex": ["allowed-tools"]}` یا `"*"`) حذف می‌شوند و متن بدنه بایت‌به‌بایت دست‌نخورده می‌ماند.

## معماری: یک حافظه برای همهٔ ایجنت‌ها
Vault تنها منبع حقیقت است. هر ایجنت (Claude Code، Codex، Claude Desktop، Cursor، ...) فقط از طریق `infinity_mcp.py` (سرور MCP روی stdio، فقط‌خواندنی) می‌خواند: `memory_search`، `memory_read` (فقط `10-Facts`، `20-Checkpoints`، `40-Conversation-Capture`، `00-Context`؛ بدون traversal/symlink؛ redact؛ سقف ۸۰۰۰ نویسه)، `memory_context` و `memory_propose` که تنها نوشتن است و فقط به `30-Pending` مرحله‌بندی می‌کند (هرگز `10-Facts`). خروجی همه «داده مرجع» است نه دستور؛ فراخوانی‌ها بدون محتوا در `logs/mcp-calls.jsonl` ثبت می‌شوند. SkillSync (برنامهٔ ویندوزی فقط‌Codex) اختیاری است: فقط توزیع مهارت و گرفتن چت Codex، و خروجی‌اش باید به `30-Pending` یا export برود، نه اینکه حافظهٔ دوم شود.

## چه چیزی تأیید نشده است
- فعال‌بودن واقعی هوک‌ها در Claude Code و Codex و ثبت `settings.json` (از سندباکس دیده نمی‌شود).
- مسیرهای ویندوزی مقصد مهارت‌ها؛ منبع اولیهٔ `skills-src` از نسخهٔ قابل‌حمل Claude.ai ساخته شد و ممکن است با نسخهٔ فعلی Codex/CoS فرق کند. قبل از `push --apply` حتماً `skills diff` را ببین.
- پوشش الگوهای `redact.py` کامل نیست؛ پاک‌سازی جایگزین بازبینی انسانی نیست.
- doctor: زمان‌نگاشت آیندهٔ بیش از ۵ دقیقه یا بدون منطقهٔ زمانی → WARN؛ زمان تغییر فایل `.md` دیگر مدرک «آخرین پیام» نیست (فقط حالت capture و `updated` در front matter). بررسی ۴ و ۵ دنبال فرمان `claude_hook.py` در ساختار hooks می‌گردند (JSON تجزیه می‌شود؛ در `config.toml` خطوط/دنبالهٔ comment نادیده گرفته می‌شوند).
- سرور MCP، handoff و ثبت واقعی در Claude Code/Codex/Desktop/Cursor روی ویندوز و با ایجنت واقعی آزمایش نشده است؛ فقط پروتکل با تست خودکار و اجرای دستی روی Vault بررسی شده.
- همگام‌سازی خودکار: کار زمان‌بندی‌شدهٔ ویندوز، پوشه‌های واقعی ایجنت‌ها و قالب واقعی مهارت Chat On Steroids روی دستگاه اصلی آزمایش نشده؛ فقط تست خودکار روی پوشه‌های موقت انجام شده. نوشتن‌های `manage_autosync.ps1` هم روی ویندوز اجرا نشده است.

## همزبان (Hamzaban): انتقال کار بین ایجنت‌ها

یک «زبان مشترک» برای همهٔ ایجنت‌ها. پس از هر چت، قواعد تو، پروژه، کارهای باز و چند پیام آخر در یک بستهٔ بی‌طرف نگه داشته می‌شود
و برای هر ایجنت با گویش خودش نوشته می‌شود؛ جدید و خودکار در `50-Handoff/latest/` (هر ایجنت یک فایل: `claude-code.md`، `codex.md`،
`gemini.md`، `chatgpt.md`، `generic.md`، ...). سازوکار و فرمت: `.ai-memory/hamzaban/SPEC.md`.

- قواعد ثابت برای همهٔ ایجنت‌ها: `00-Context/Harness.md` (خودت ویرایشش کن).
- فرمان‌ها (ویندوز): `.ai-memory\HAMZABAN.cmd targets | pack --to gemini | refresh --force | ingest <فایل> | export --to gemini --dir <پوشه> | status | check`.
- پکیج انتقال از یک پروژه یا چت: `.ai-memory\HAMZABAN.cmd continuity write --dir <پوشهٔ پروژه> [--topic <نام>] [--chat <تگ>] [--dry-run]` پوشهٔ `project-continuity/` با ۷ فایل می‌سازد (README، INSTRUCTIONS، CONTEXT، HANDOFF-تاریخ، DECISIONS، TEST-LOG، OPEN-QUESTIONS). فقط می‌خواند و فقط داخل همان پوشه می‌نویسد؛ فایلی که خودت ویرایش کرده‌ای هرگز بازنویسی نمی‌شود (نسخهٔ تازه کنارش با پسوند `.generated` می‌آید). بخش‌هایی که به بررسی کد نیاز دارند `TODO(agent)` می‌مانند و مهارت `hamzaban-continuity` آن‌ها را پر می‌کند. اگر پوشه داخل یک مخزن git باشد و نادیده گرفته نشده باشد هشدار می‌دهد.
- هماهنگی فایل‌های دستور عامل‌ها: `.ai-memory\HAMZABAN.cmd parity [--dir <پروژه>] [--home] [--strict]`؛ برای `CLAUDE.md`، `AGENTS.md`، `GEMINI.md`، فایل Copilot و Cursor می‌گوید CURRENT (قواعد تو همه هست)، STALE (قاعده‌ای تغییر کرده)، OWN (خودت نوشته‌ای؛ فقط می‌گوید چند قاعده کلمه‌به‌کلمه هست)، MISSING (نیست) یا MIXED (فایل‌ها از نسخه‌های مختلف ساخته شده‌اند). `--home` فایل‌های خودِ Claude، Codex و Gemini را در پوشهٔ کاربر می‌خواند (فقط وضعیت را چاپ می‌کند، محتوا را نه). ChatGPT و Claude.ai را نمی‌شود از دیسک خواند.
- یک متن واحد برای همهٔ پروژه‌ها (فایل‌های سراسری عامل‌ها): `pack` و `export` گزینهٔ `--rules-only` دارند و فقط قواعد ثابت، یادداشت‌های ترجیحات و ابزارهای حافظه را می‌نویسند، بدون هیچ وضعیت پروژه. مثال: `.ai-memory\HAMZABAN.cmd export --to claude-code --dir "%USERPROFILE%\.claude" --rules-only` فایل `CLAUDE.md` را می‌سازد و کنار `AGENTS.md` خودت فقط `AGENTS.hamzaban.md` می‌گذارد (به فایل تو دست نمی‌زند). برای ChatGPT، Claude.ai و Gemini وب، خروجی `pack --to chatgpt --rules-only` را در بخش دستورهای شخصی بچسبان. `parity --home` وقتی همهٔ فایل‌های ساخته‌شده از یک نسخه‌اند «ONE LANGUAGE (home)» می‌نویسد. `--rules-only` با `--topic` و `--session` هم‌زمان پذیرفته نمی‌شود.
- ایجنت تازه (مثلاً Google): یک فایل JSON ده‌خطی در `.ai-memory/adapters.d/` (نمونه در SPEC.md)، بدون کد.
- پایان هر چت، ایجنت یک بلوک `hamzaban-close` می‌نویسد؛ حقیقت‌ها و تصمیم‌ها برای تأیید تو به `30-Pending/` می‌روند، خلاصه و کارها در چک‌پوینتِ «تأییدنشده».
- ایمنی: فقط در `50-Handoff/`، `30-Pending/` و `20-Checkpoints/` می‌نویسد، هیچ‌چیز را پاک نمی‌کند، رمزها پنهان می‌شوند، حقیقت‌های private هرگز وارد بسته نمی‌شوند.
- نکتهٔ صادقانه: این انتقالِ متن است (نه وضعیت زندهٔ مدل). مدل گیرنده باید فایل‌های واقعی را دوباره چک کند.
- ایده‌ها با OmniRoute (MIT) سنجیده شد؛ فقط ایده، بدون کپی کد.
