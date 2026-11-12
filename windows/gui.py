"""Desktop entrypoint of the HH auto-apply GUI. Packagers must route worker modes before Tk."""

import sys

from gui_protocol import LETTER_REJECTIONS, LETTER_REVISION_FAILURES, RETRY_NOTICES


APP_TITLE = "Автоотклики HH"
ACTION_TITLES = {
    "hh_login": "вход в HH", "chatgpt_login": "вход в ChatGPT", "resumes": "загрузка резюме",
    "models": "загрузка списка моделей", "preview": "предпросмотр писем", "apply": "отклики с проверкой",
    "auto": "автоотклики", "chats": "ответы в чатах",
}
SUCCESS_TITLES = {
    "hh_login": "Вход в HH выполнен.", "chatgpt_login": "Вход в ChatGPT выполнен.",
    "resumes": "Резюме загружены.", "models": "Список моделей загружен.",
    "preview": "Предпросмотр готов. Письма — в списке «Готовые письма».",
    "apply": "Отклики завершены.", "auto": "Автоотклики завершены.", "chats": "Ответы в чатах завершены.",
}
# The journal (#11): this many lines stay in memory; the file rotates past LOG_FILE_LIMIT bytes.
LOG_MEMORY_LINES = 5000
LOG_FILE_LIMIT = 1024 * 1024


def log_file_path():
    from gui_profile import state_dir
    return state_dir() / "log" / "gui.log"


def write_log_file(path, line):
    """Append one journal line; past the limit the file becomes gui.log.1. Never raises."""
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_FILE_LIMIT:
            path.replace(path.with_name(path.name + ".1"))
        with path.open("a", encoding="utf-8") as file:
            file.write(line + "\n")
    except OSError:
        pass


# Fixed texts of the automatic model chain (#10): the worker sends only these codes.
MODEL_SWITCHES = {
    "daily_limit": "закончился дневной лимит бесплатных запросов",
    "rate_limit": "провайдер ограничил частоту запросов",
    "region": "модель недоступна в твоём регионе",
    "reasoning": "модель не отвечает без рассуждения",
    "key": "провайдер отклонил ключ или доступ к модели",
    "unavailable": "модель или провайдер недоступны",
    "empty": "модель вернула пустой ответ",
}
FALLBACK_STATES = {
    "ok": "доступна", "missing": "нет в списке моделей провайдера",
    "no_key": "нет сохранённого ключа, модель пропущена", "network": "провайдер не ответил",
}


CHAT_WAIT_ENDS = {
    "question": "Робот прислал следующий вопрос. Готовлю ответ на проверку.",
    "timeout": "Робот не прислал следующий вопрос. Перехожу к следующему чату.",
    "left": "Робот вышел из чата. Перехожу к следующему чату.",
    "skipped": "Чат изменился во время ожидания. Чат пропущен, новый ответ не отправлен.",
    "stopped": "Ожидание следующего вопроса прервано.",
}
COUNT_TITLES = {
    "attempts": "попыток отправки", "http_success": "принято HH", "captcha_rejections": "отклонено капчей",
    "chats": "чатов", "inspected": "просмотрено", "drafts": "черновиков", "skipped": "пропущено",
    "failures": "ошибок",
}
# Windows 11 dark palette: dark page, raised cards, one accent colour.
PAGE, CARD, BORDER, CONTROL_BORDER = "#202020", "#2b2b2b", "#3a3a3a", "#4a4a4a"
CONTROL, CONTROL_HOVER, CONTROL_PRESSED, FIELD = "#373737", "#3f3f3f", "#323232", "#1f1f1f"
TEXT, MUTED, DISABLED = "#ffffff", "#c5c5c5", "#787878"
ACCENT, ACCENT_HOVER, ACCENT_PRESSED, ON_ACCENT = "#60cdff", "#5ab9e6", "#4fa3cc", "#000000"
SUCCESS, WARNING, CRITICAL = "#6ccb5f", "#fce100", "#ff99a4"
# InfoBar severities: (background, icon colour, Segoe Fluent Icons glyph, fallback glyph).
SEVERITY = {
    "info": ("#2b3640", ACCENT, "\ue946", "i"),
    "success": ("#393d1b", SUCCESS, "\ue930", "✓"),
    "warning": ("#433519", WARNING, "\ue7ba", "!"),
    "error": ("#442726", CRITICAL, "\uea39", "×"),
}
REVIEW_ACTIONS = frozenset({"preview", "apply", "auto", "chats"})
UNKNOWN_RESULT = " Результат последней отправки неизвестен. Проверь отклики на hh.ru, прежде чем запускать снова."


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--worker"]:
        from gui_runtime import attach_standard_streams
        attach_standard_streams()
        from gui_worker import worker_main
        return worker_main()
    if argv == ["--gateway-worker"]:
        from gui_runtime import attach_standard_streams
        attach_standard_streams()
        from gui_runtime import gateway_main
        return gateway_main()
    if argv:
        return 2
    import tkinter as tk
    from tkinter import ttk, messagebox
    enable_dpi_awareness()
    root = tk.Tk()
    from profile_migration import MigrationError, migrate_all
    try:
        migrate_all()
    except MigrationError as error:
        messagebox.showerror(APP_TITLE, str(error))
        root.destroy()
        return 1
    try:
        Application(root, tk, ttk, messagebox)
        try:
            import updater
            updater.mark_started()  # The window opened: this code version is good.
        except (ImportError, OSError):
            pass
    except (OSError, ValueError):
        messagebox.showerror(APP_TITLE, "Не удалось открыть папку настроек приложения.")
        root.destroy()
        return 1
    root.mainloop()
    return 0


def enable_dpi_awareness():
    """Render crisp text on scaled Windows displays instead of a blurry bitmap."""
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, ImportError, OSError):
        pass


def dark_title_bar(window):
    """Ask Windows 10 20H1+/11 to draw a dark title bar; older systems keep the default."""
    try:
        import ctypes
        window.update_idletasks()
        handle = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE, then the pre-20H1 id.
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(handle, attribute, ctypes.byref(value),
                                                         ctypes.sizeof(value)) == 0:
                break
    except (AttributeError, ImportError, OSError):
        pass


def needs_attention(severity, action):
    """Long runs always call the user back; short ones only when something went wrong.
    A stop the user pressed ("info") needs no alert."""
    return severity in {"error", "warning"} or (severity == "success" and action in REVIEW_ACTIONS)


def attention(window, level):
    """Soft system sound and a taskbar flash until the user returns to the window."""
    try:
        import winsound
        sound = {"error": winsound.MB_ICONHAND, "warning": winsound.MB_ICONEXCLAMATION}
        winsound.MessageBeep(sound.get(level, winsound.MB_ICONASTERISK))
    except (ImportError, RuntimeError):
        pass
    try:
        import ctypes
        from ctypes import wintypes

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND),
                        ("dwFlags", wintypes.DWORD), ("uCount", wintypes.UINT),
                        ("dwTimeout", wintypes.DWORD)]

        handle = ctypes.windll.user32.GetParent(window.winfo_id())
        if handle == ctypes.windll.user32.GetForegroundWindow():
            return  # The user already looks at the window; the sound is enough.
        # FLASHW_ALL | FLASHW_TIMERNOFG: caption and taskbar button until foreground.
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), handle, 0x3 | 0xC, 0, 0)
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:  # An alert must never break the finish of a run.
        pass


def describe_vacancy(item):
    """Human-readable vacancy card; raw API JSON is unreadable for a person."""
    vacancy = item.get("vacancy") if isinstance(item, dict) else None
    vacancy = vacancy if isinstance(vacancy, dict) else {}

    def name_of(key):
        value = vacancy.get(key)
        return value.get("name") if isinstance(value, dict) else None

    def amount(value):
        return f"{value:,}".replace(",", " ") if isinstance(value, int) else str(value)

    lines = [str(vacancy.get("name") or "Вакансия")]
    if employer := name_of("employer"):
        lines.append("Компания: " + str(employer))
    salary = vacancy.get("salary")
    if isinstance(salary, dict) and (salary.get("from") or salary.get("to")):
        parts = [prefix + " " + amount(salary[key]) for prefix, key in (("от", "from"), ("до", "to"))
                 if salary.get(key)]
        if salary.get("currency"):
            parts.append(str(salary["currency"]))
        lines.append("Зарплата: " + " ".join(parts))
    if area := name_of("area"):
        lines.append("Город: " + str(area))
    if url := vacancy.get("alternate_url"):
        lines.append("Ссылка: " + str(url))
    if isinstance(item, dict) and item.get("reason"):
        lines.append("Причина пропуска: " + str(item["reason"]))
    return "\n".join(lines)


# First-run setup (#5): one step per screen; the same pages serve the Settings menu.
WIZARD_PAGES = (
    ("hh", "Вход в HH", "Войди в аккаунт hh.ru. Приложение ищет вакансии и отправляет отклики от твоего имени."),
    ("ai", "AI-провайдер", "Кто пишет сопроводительные письма и ответы в чатах. С ключами OpenRouter, Gemini "
                           "или Groq модели выбираются и меняются сами, когда у одной кончается лимит."),
    ("resume", "Резюме и поиск", "Выбери резюме и какие вакансии искать. Резюме загружаются сами после входа в HH."),
    ("done", "Всё готово", "Настройка завершена. Начни с предпросмотра: он показывает письма и ничего не отправляет. "
                           "Вход, AI-провайдер и стили письма потом — в меню «Настройки» вверху справа."),
)


WELCOME = (
    ("TLabel", "Приложение ищет вакансии на hh.ru, пишет сопроводительные письма через ChatGPT "
               "или другого AI-провайдера "
               "и отправляет отклики. Письмо уходит только после твоего решения."),
    ("Subheading.TLabel", "Что нужно"),
    ("TLabel", "• аккаунт на hh.ru и хотя бы одно опубликованное резюме;\n"
               "• аккаунт ChatGPT, ключ OpenAI-совместимого API (есть бесплатные: OpenRouter,\n"
               "  Gemini, Groq) или локальная модель."),
    ("Subheading.TLabel", "Порядок работы"),
    ("TLabel", "1. Пройди настройку: вход в HH, AI-провайдер, резюме и поиск.\n"
               "2. Запусти предпросмотр: он показывает письма и ничего не отправляет.\n"
               "3. Нажми «Откликнуться с проверкой писем» и подтверди каждое письмо.\n"
               "Вход, AI-провайдер и стили письма — в меню «Настройки» вверху справа."),
    ("Warn.TLabel", "HH может заблокировать аккаунт за автоматизацию. Капчу вводи только сам."),
)


class Application:
    def __init__(self, root, tk, ttk, dialogs):
        import tomllib
        from gui_profile import initialize, read_searches
        from gui_steps import chatgpt_signed_in, hh_signed_in
        self.root, self.tk, self.ttk, self.dialogs = root, tk, ttk, dialogs
        self.session, self.resumes, self.previews = None, [], []
        self.recovery_acknowledgement = None
        self.searches = read_searches()
        config = tomllib.loads(initialize().read_text(encoding="utf-8"))
        model = config["openai"]["default"]["model"]
        self.signed_hh, self.signed_chatgpt = hh_signed_in(), chatgpt_signed_in()
        self.load_provider()
        self.model = tk.StringVar(value="" if model == "CHOOSE_AFTER_LOGIN" else model)
        self.search, self.resume = tk.StringVar(), tk.StringVar()
        self.search_mode = tk.StringVar(value="query")  # "query" or "all" (HH suggestions for the resume).
        self.all_resumes = tk.BooleanVar(value=False)
        from gui_profile import read_option
        self.no_letter = tk.BooleanVar(value=read_option("no_letter"))
        self.no_letter.trace_add("write", lambda *_a: self.save_no_letter())
        self.limit, self.minimum, self.maximum = tk.StringVar(value="1"), tk.StringVar(value="60"), tk.StringVar(value="180")
        self.status = tk.StringVar()
        self.review_kind, self.sending, self.current_resume = None, False, None
        self.buttons, self.action_buttons = [], {}
        self.log_lines, self.log_path, self.log_view = [], log_file_path(), None
        self.device_window = None
        root.title(APP_TITLE)
        root.geometry("1180x820")
        root.minsize(920, 620)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.setup_style()
        dark_title_bar(root)

        outer = ttk.Frame(root, padding=(20, 14, 20, 16), style="Page.TFrame")
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer, style="Page.TFrame")
        header.pack(fill="x")
        ttk.Label(header, text=APP_TITLE, style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="Отклики на hh.ru с письмами от AI", style="PageHint.TLabel").pack(
            side="left", padx=12, pady=(8, 0))
        import os
        version = os.environ.get("HH_APP_VERSION")
        self.update_release = None
        self.update_button = ttk.Button(header, text="Обновить", style="Accent.TButton", command=self.apply_update)
        if version:
            ttk.Label(header, text="версия " + version, style="PageHint.TLabel").pack(side="right", pady=(8, 0))
        self.build_settings_menu(header)
        self.build_infobar(outer)

        body = self.body = ttk.Frame(outer, style="Page.TFrame")
        body.pack(fill="both", expand=True)
        self.wizard = ttk.Frame(outer, style="Page.TFrame")  # Shown instead of body while setting up (#5).
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        sidebar_box, sidebar = self.scrollable(body)
        sidebar_box.grid(row=0, column=0, sticky="nsw")
        main = ttk.Frame(body, style="Card.TFrame", padding=(22, 16, 22, 18))
        main.grid(row=0, column=1, sticky="nsew", padx=(16, 0))
        self.build_sidebar(sidebar)
        self.build_wizard(self.wizard)
        self.build_main(main)
        self.load_styles()
        self.update_search_mode()
        self.review_buttons = (self.yes, self.no, self.submit, self.edit, self.manual_button)
        self.clear_review()
        for variable in (self.search, self.search_mode, self.limit, self.minimum, self.maximum):
            variable.trace_add("write", lambda *_a: self.refresh_steps())
        self.refresh_steps()
        self.say(self.next_hint())
        self.decide_start()
        root.after(100, self.poll)
        self.root.after_idle(self.check_recovery)
        self.root.after_idle(self.auto_load_resumes)
        self.root.after(3000, self.check_updates)

    # ---- layout -----------------------------------------------------------------

    def setup_style(self):
        """Fluent-like light theme on the built-in clam engine: no extra dependency."""
        from tkinter import font
        style = self.ttk.Style(self.root)
        style.theme_use("clam")
        families = set(font.families(self.root))
        variable = "Segoe UI Variable Text" in families
        body = "Segoe UI Variable Text" if variable else "Segoe UI"
        strong = ("Segoe UI Variable Display", "bold") if variable else ("Segoe UI Semibold",)
        self.icon_font = ("Segoe Fluent Icons" if "Segoe Fluent Icons" in families else
                          "Segoe MDL2 Assets" if "Segoe MDL2 Assets" in families else None)

        def heavy(size):
            return (strong[0], size) + strong[1:]
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            font.nametofont(name).configure(family=body, size=10)
        self.text_font = (body, 11)
        self.root.configure(background=PAGE)
        style.configure(".", background=CARD, foreground=TEXT, font=(body, 10), bordercolor=BORDER,
                        lightcolor=CARD, darkcolor=CARD, troughcolor=BORDER, focuscolor=ACCENT,
                        selectbackground=ACCENT, selectforeground=ON_ACCENT, fieldbackground=FIELD,
                        insertcolor=TEXT)
        # Classic Tk parts (combobox drop-down list) read the option database.
        for option, value in (("*TCombobox*Listbox.background", CONTROL), ("*TCombobox*Listbox.foreground", TEXT),
                              ("*TCombobox*Listbox.selectBackground", ACCENT),
                              ("*TCombobox*Listbox.selectForeground", ON_ACCENT)):
            self.root.option_add(option, value)
        style.map(".", foreground=[("disabled", DISABLED)])
        style.configure("Page.TFrame", background=PAGE)
        style.configure("Card.TFrame", background=CARD, relief="solid", borderwidth=1, bordercolor=BORDER)
        style.configure("CurrentCard.TFrame", background=CARD, relief="solid", borderwidth=1, bordercolor=ACCENT)
        style.configure("Title.TLabel", background=PAGE, font=heavy(17))
        style.configure("PageHint.TLabel", background=PAGE, foreground=MUTED, font=(body, 9))
        style.configure("Heading.TLabel", font=heavy(14))
        style.configure("Subheading.TLabel", font=heavy(10))
        for name, colour in (("StepDone", SUCCESS), ("StepCurrent", ACCENT), ("StepLocked", MUTED)):
            style.configure(name + ".TLabel", foreground=colour, font=heavy(10))
        style.configure("Hint.TLabel", foreground=MUTED, font=(body, 9))
        style.configure("Done.TLabel", foreground=SUCCESS, font=(body, 9))
        style.configure("Locked.TLabel", foreground=DISABLED, font=(body, 9))
        style.configure("Error.TLabel", foreground=CRITICAL, font=(body, 9))
        style.configure("Warn.TLabel", foreground=WARNING, font=(body, 9))
        style.configure("TButton", background=CONTROL, foreground=TEXT, bordercolor=CONTROL_BORDER,
                        lightcolor=CONTROL, darkcolor=CONTROL, padding=(12, 5), relief="solid", borderwidth=1)
        button_states = [("disabled", CARD), ("pressed", CONTROL_PRESSED), ("active", CONTROL_HOVER)]
        style.map("TButton", background=button_states, lightcolor=button_states, darkcolor=button_states,
                  bordercolor=[("disabled", BORDER), ("focus", ACCENT)])
        style.configure("Accent.TButton", background=ACCENT, foreground=ON_ACCENT, bordercolor=ACCENT,
                        lightcolor=ACCENT, darkcolor=ACCENT, font=heavy(10), padding=(14, 6))
        accent_states = [("disabled", "#434343"), ("pressed", ACCENT_PRESSED), ("active", ACCENT_HOVER)]
        style.map("Accent.TButton", background=accent_states, lightcolor=accent_states, darkcolor=accent_states,
                  bordercolor=accent_states, foreground=[("disabled", DISABLED)])
        style.configure("Expander.TButton", anchor="w", padding=(14, 9))
        for widget in ("TEntry", "TCombobox", "TSpinbox"):
            style.configure(widget, fieldbackground=FIELD, bordercolor=CONTROL_BORDER, lightcolor=FIELD,
                            darkcolor=FIELD, padding=5, arrowcolor=MUTED, background=CONTROL, foreground=TEXT)
            style.map(widget, bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)],
                      fieldbackground=[("disabled", CARD), ("readonly", FIELD)],
                      background=[("active", CONTROL_HOVER)],
                      selectbackground=[("readonly !focus", FIELD)], selectforeground=[("readonly !focus", TEXT)])
        style.configure("TCheckbutton", indicatorbackground=FIELD, indicatorforeground=ON_ACCENT)
        style.configure("TRadiobutton", indicatorbackground=FIELD, indicatorforeground=ON_ACCENT)
        style.map("TRadiobutton", indicatorbackground=[("selected", ACCENT)], background=[("active", CARD)])
        style.map("TCheckbutton", indicatorbackground=[("selected", ACCENT)], background=[("active", CARD)])
        style.configure("Accent.Horizontal.TProgressbar", background=ACCENT, troughcolor=BORDER,
                        bordercolor=BORDER, lightcolor=ACCENT, darkcolor=ACCENT, thickness=4)
        # Thin neutral scrollbars, as in Windows 11 lists.
        style.configure("Vertical.TScrollbar", background="#5a5a5a", troughcolor=CARD, bordercolor=CARD,
                        arrowcolor=MUTED, lightcolor="#5a5a5a", darkcolor="#5a5a5a", arrowsize=11, gripcount=0)
        style.map("Vertical.TScrollbar", background=[("active", "#7a7a7a")])
        style.configure("Page.Vertical.TScrollbar", troughcolor=PAGE, bordercolor=PAGE)
        for level, (background, icon, _glyph, _fallback) in SEVERITY.items():
            style.configure(f"{level}.Info.TFrame", background=background, relief="solid", borderwidth=1,
                            bordercolor=background)
            style.configure(f"{level}.Info.TLabel", background=background, foreground=TEXT, font=(body, 10))
            style.configure(f"{level}.InfoIcon.TLabel", background=background, foreground=icon,
                            font=(self.icon_font, 12) if self.icon_font else (body, 11, "bold"))

    def build_infobar(self, parent):
        """Fluent InfoBar: one status line whose colour and icon state its severity."""
        ttk = self.ttk
        self.infobar = ttk.Frame(parent, style="info.Info.TFrame", padding=(14, 9))
        self.infobar.pack(fill="x", pady=(10, 14))
        self.infobar_icon = ttk.Label(self.infobar, style="info.InfoIcon.TLabel")
        self.infobar_icon.pack(side="left", padx=(0, 10))
        message = ttk.Label(self.infobar, textvariable=self.status, style="info.Info.TLabel",
                            anchor="w", justify="left")
        message.pack(side="left", fill="x", expand=True)
        message.bind("<Configure>", lambda event: message.configure(wraplength=max(event.width - 8, 200)))
        self.infobar_message = message
        # Errors and warnings link to the journal instead of opening it by themselves (#11).
        self.infobar_more = ttk.Button(self.infobar, text="Подробнее", command=self.open_log)
        # One action for a warning, e.g. «Войти» after a lost login (#5).
        self.infobar_action = ttk.Button(self.infobar)
        self.set_severity("info")

    def set_severity(self, level):
        infobar = getattr(self, "infobar", None)
        if infobar is None:
            return
        _background, _icon, glyph, fallback = SEVERITY[level]
        infobar.configure(style=f"{level}.Info.TFrame")
        more = getattr(self, "infobar_more", None)
        if more is not None:
            if level in ("error", "warning"):
                more.pack(side="right", padx=(10, 0))
            else:
                more.pack_forget()
        self.infobar_icon.configure(style=f"{level}.InfoIcon.TLabel", text=glyph if self.icon_font else fallback)
        self.infobar_message.configure(style=f"{level}.Info.TLabel")

    def scrollable(self, parent):
        """Vertical scroll for the sidebar so every step stays reachable on small screens."""
        tk, ttk = self.tk, self.ttk
        box = ttk.Frame(parent, style="Page.TFrame")
        canvas = tk.Canvas(box, highlightthickness=0, borderwidth=0, background=PAGE)
        bar = ttk.Scrollbar(box, orient="vertical", command=canvas.yview, style="Page.Vertical.TScrollbar")
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        inner = ttk.Frame(canvas, padding=(0, 0, 8, 0), style="Page.TFrame")
        window = canvas.create_window(0, 0, anchor="nw", window=inner)
        inner.bind("<Configure>", lambda _e: canvas.configure(
            scrollregion=canvas.bbox("all"), width=inner.winfo_reqwidth()))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))

        def wheel(event):
            canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
        return box, inner

    def action_button(self, parent, text, action):
        button = self.ttk.Button(parent, text=text, command=lambda: self.launch(action))
        self.buttons.append(button)
        self.action_buttons[action] = button
        return button

    def card(self, parent, style="Card.TFrame"):
        """White card on the page background (Windows 11 settings-card pattern)."""
        card = self.ttk.Frame(parent, style=style, padding=(16, 12, 16, 14))
        card.columnconfigure(0, weight=1)
        return card

    def section(self, parent, title):
        """A titled card of the main screen; the setup steps live in the wizard (#5)."""
        card = self.card(parent)
        card.pack(fill="x", pady=(0, 10))
        self.ttk.Label(card, text=title, style="StepCurrent.TLabel").grid(row=0, column=0, sticky="w")
        frame = self.ttk.Frame(card)
        frame.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        frame.columnconfigure(0, weight=1)
        return frame

    def hint(self, parent, text, row, style="Hint.TLabel", pady=(4, 0)):
        label = self.ttk.Label(parent, text=text, style=style, wraplength=300, justify="left")
        label.grid(row=row, column=0, columnspan=2, sticky="w", pady=pady)
        return label

    def search_widgets(self, parent, row):
        """Search mode controls; the wizard and the main screen each get one set on the same variables."""
        ttk = self.ttk
        ttk.Label(parent, text="Какие вакансии искать").grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 0))
        modes = ttk.Frame(parent)
        modes.grid(row=row + 1, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(modes, text="По запросу", value="query", variable=self.search_mode,
                        command=self.select_search_mode).pack(side="left")
        ttk.Radiobutton(modes, text="Все подходящие для резюме", value="all", variable=self.search_mode,
                        command=self.select_search_mode).pack(side="left", padx=(12, 0))
        entry = ttk.Entry(parent, textvariable=self.search)
        entry.grid(row=row + 2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        entry.bind("<FocusOut>", lambda _e: self.remember_search())
        self.search_entries.append(entry)
        self.search_hints.append(self.hint(parent, "", row + 3, pady=(2, 0)))
        return entry

    def build_sidebar(self, side):
        """The main screen holds only work (#5): resume, search and style change often; setup is in the wizard."""
        ttk = self.ttk
        self.search_entries, self.search_hints = [], []
        resume = self.section(side, "Резюме и поиск")
        self.resume_box_main = ttk.Combobox(resume, textvariable=self.resume, state="readonly", width=30)
        self.resume_box_main.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.resume_box_main.bind("<<ComboboxSelected>>", self.select_resume)
        self.search_entry = self.search_widgets(resume, 1)
        ttk.Checkbutton(resume, text="Откликаться со всех резюме по очереди", variable=self.all_resumes).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))
        refresh = ttk.Button(resume, text="Обновить список резюме", command=lambda: self.launch("resumes"))
        refresh.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.buttons.append(refresh)
        self.extra_action_buttons = [("resumes", refresh)]
        ttk.Label(resume, text="Стиль письма").grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.style_name = self.tk.StringVar()
        self.style_box = ttk.Combobox(resume, textvariable=self.style_name, state="readonly", width=30)
        self.style_box.grid(row=8, column=0, columnspan=2, sticky="ew")
        self.style_box.bind("<<ComboboxSelected>>", self.select_style)

        run = self.section(side, "Отклики")
        self.action_button(run, "Предпросмотр писем", "preview").grid(row=1, column=0, sticky="ew")
        self.hint(run, "Показывает письма и ничего не отправляет. Начни с него.", 2, pady=(2, 8))
        self.action_button(run, "Откликнуться с проверкой писем", "apply").grid(row=3, column=0, sticky="ew")
        self.hint(run, "Каждое письмо ты читаешь и подтверждаешь сам.", 4, pady=(2, 4))
        ttk.Checkbutton(run, text="Без сопроводительного письма", variable=self.no_letter).grid(
            row=5, column=0, sticky="w")
        self.hint(run, "Отклик уходит только с резюме, AI не нужен. Вакансии, где HH требует письмо "
                       "или есть тест, пропускаются. Действует для откликов с проверкой и автооткликов.",
                  6, pady=(2, 8))
        self.action_button(run, "Ответить в чатах", "chats").grid(row=7, column=0, sticky="ew")
        self.hint(run, "Ответы работодателям в чатах HH, тоже с проверкой.", 8, pady=(2, 0))

        # Risky mode stays folded away from the main flow.
        self.auto_toggle = ttk.Button(side, text="▸ Дополнительно: автоотклики без проверки",
                                      style="Expander.TButton", command=self.toggle_auto)
        self.auto_toggle.pack(fill="x", pady=(0, 10))
        self.auto_frame = auto = self.card(side)
        self.hint(auto, "Письма уходят без твоей проверки. Сначала посмотри предпросмотр.", 0,
                  "Warn.TLabel", pady=(0, 8))
        fields = ttk.Frame(auto)
        fields.grid(row=1, column=0, sticky="ew")
        ttk.Label(fields, text="Сколько успешных откликов отправить").grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Spinbox(fields, textvariable=self.limit, from_=1, to=1000, width=6).grid(
            row=1, column=0, sticky="w")
        ttk.Label(fields, text="Считаются только отклики, которые принял HH.", style="Hint.TLabel",
                  wraplength=300, justify="left").grid(row=2, column=0, columnspan=4, sticky="w", pady=(2, 0))
        ttk.Label(fields, text="Пауза между откликами, секунд: от").grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Spinbox(fields, textvariable=self.minimum, from_=1, to=86400, increment=10, width=6).grid(
            row=4, column=0, sticky="w")
        ttk.Label(fields, text="до").grid(row=4, column=1, padx=6)
        ttk.Spinbox(fields, textvariable=self.maximum, from_=1, to=86400, increment=10, width=6).grid(
            row=4, column=2, sticky="w")
        self.auto_error = ttk.Label(auto, style="Error.TLabel", wraplength=300, justify="left")
        self.auto_error.grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.action_button(auto, "Запустить автоотклики…", "auto").grid(row=3, column=0, sticky="ew", pady=(6, 0))

    def build_wizard(self, parent):
        """Setup pages (#5): the first run walks them in order; the Settings menu opens one of them."""
        ttk = self.ttk
        parent.columnconfigure(0, weight=1)
        top = ttk.Frame(parent, style="Page.TFrame")
        top.grid(row=0, column=0, sticky="ew")
        self.wizard_step = ttk.Label(top, style="PageHint.TLabel")
        self.wizard_step.pack(anchor="w")
        self.wizard_bar = ttk.Progressbar(top, style="Accent.Horizontal.TProgressbar", maximum=4, length=420)
        self.wizard_bar.pack(anchor="w", pady=(4, 10))
        # The card hugs its page and carries the buttons, so «Далее» never falls below a small screen.
        holder = self.card(parent)
        holder.grid(row=1, column=0, sticky="new")
        holder.configure(padding=(28, 22, 28, 24))
        self.wizard_title = ttk.Label(holder, style="Heading.TLabel")
        self.wizard_title.grid(row=0, column=0, sticky="w")
        self.wizard_note = ttk.Label(holder, style="Hint.TLabel", wraplength=640, justify="left")
        self.wizard_note.grid(row=1, column=0, sticky="w", pady=(2, 14))
        pages = ttk.Frame(holder)
        pages.grid(row=2, column=0, sticky="nsew")
        pages.columnconfigure(0, weight=1)
        self.wizard_pages = {}
        for name, _title, _note in WIZARD_PAGES:
            frame = ttk.Frame(pages)
            frame.grid(row=0, column=0, sticky="nw")
            frame.grid_remove()
            self.wizard_pages[name] = frame

        login = self.wizard_pages["hh"]
        self.action_button(login, "Войти в HH", "hh_login").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.hh_state = ttk.Label(login, style="Hint.TLabel")
        self.hh_state.grid(row=0, column=1, sticky="w")
        # Login input lives next to the login button, not in the review area.
        self.credential_frame = ttk.Frame(login)
        self.credential_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        self.credential_frame.columnconfigure(0, weight=1)
        self.credential_label = ttk.Label(self.credential_frame, text="Email или телефон от аккаунта HH",
                                          style="Subheading.TLabel")
        self.credential_label.grid(row=0, column=0, columnspan=2, sticky="w")
        self.credential = ttk.Entry(self.credential_frame, width=40)
        self.credential.grid(row=1, column=0, sticky="ew")
        self.credential.bind("<Return>", lambda _e: self.submit_credential())
        self.submit = ttk.Button(self.credential_frame, text="Продолжить", style="Accent.TButton",
                                 command=self.submit_credential)
        self.submit.grid(row=1, column=1, padx=(6, 0))

        ai = self.wizard_pages["ai"]
        self.provider_button = ttk.Button(ai, text="AI-провайдер…", command=self.edit_provider)
        self.provider_button.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.buttons.append(self.provider_button)
        self.provider_state = ttk.Label(ai, style="Hint.TLabel", wraplength=420, justify="left")
        self.provider_state.grid(row=0, column=1, sticky="w")
        self.chatgpt_button = self.action_button(ai, "Войти в ChatGPT", "chatgpt_login")
        self.chatgpt_button.grid(row=1, column=0, sticky="ew", padx=(0, 10), pady=(6, 0))
        self.chatgpt_state = ttk.Label(ai, style="Hint.TLabel")
        self.chatgpt_state.grid(row=1, column=1, sticky="w", pady=(6, 0))
        self.action_button(ai, "Проверить подключение", "models").grid(
            row=2, column=0, sticky="ew", padx=(0, 10), pady=(6, 0))
        self.model_state = ttk.Label(ai, style="Hint.TLabel", wraplength=420, justify="left")
        self.model_state.grid(row=2, column=1, sticky="w", pady=(6, 0))
        self.free_hint = self.hint(ai, "", 3, pady=(10, 0))
        self.free_hint.configure(wraplength=640)
        ttk.Checkbutton(ai, text="Откликаться без сопроводительного письма: AI не нужен",
                        variable=self.no_letter).grid(row=4, column=0, columnspan=2, sticky="w", pady=(16, 0))
        self.hint(ai, "Предпросмотр и ответы в чатах всё равно используют AI: его можно подключить позже "
                      "в меню «Настройки».", 5, pady=(2, 0))

        resume = self.wizard_pages["resume"]
        self.action_button(resume, "Обновить список резюме", "resumes").grid(row=0, column=0, sticky="w")
        ttk.Label(resume, text="Резюме").grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.resume_box = ttk.Combobox(resume, textvariable=self.resume, state="readonly", width=44)
        self.resume_box.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.resume_box.bind("<<ComboboxSelected>>", self.select_resume)
        self.wizard_search_entry = self.search_widgets(resume, 3)
        self.hint(resume, "Для каждого резюме выбери свой режим поиска.", 7, pady=(4, 0))

        done = self.wizard_pages["done"]
        ttk.Button(done, text="Перейти к откликам", style="Accent.TButton", command=self.finish_wizard).grid(
            row=0, column=0, sticky="w", ipady=2)

        nav = ttk.Frame(holder)
        nav.grid(row=3, column=0, sticky="ew", pady=(22, 0))
        self.wizard_back = ttk.Button(nav, text="Назад", command=lambda: self.wizard_move(-1))
        self.wizard_back.pack(side="left")
        self.wizard_next = ttk.Button(nav, text="Далее", style="Accent.TButton", command=lambda: self.wizard_move(1))
        self.wizard_next.pack(side="left", padx=(6, 0))
        self.wizard_close = ttk.Button(nav, text="Готово", command=self.close_wizard)
        self.wizard_page, self.wizard_settings = None, False

    def build_settings_menu(self, header):
        """«Настройки» (#5): setup pages, letter styles, fallback models and the journal after setup."""
        ttk = self.ttk
        menu = self.settings_menu = self.tk.Menu(self.root, tearoff=0, background=CARD, foreground=TEXT,
                                                 activebackground=ACCENT, activeforeground=ON_ACCENT,
                                                 disabledforeground=DISABLED, borderwidth=0)
        self.settings_items = (
            ("Аккаунт HH…", lambda: self.show_wizard("hh", settings=True), True),
            ("AI-провайдер…", lambda: self.show_wizard("ai", settings=True), True),
            ("Стили письма…", self.edit_letter_style, True),
            ("Журнал…", self.open_log, False),
            ("Пройти настройку заново", lambda: self.show_wizard("hh"), True),
        )
        for label, command, _locked in self.settings_items:
            if label == "Пройти настройку заново":
                menu.add_separator()
            menu.add_command(label=label, command=command)
        self.settings_button = ttk.Button(header, text="Настройки ▾", command=self.post_settings_menu)
        self.settings_button.pack(side="right", padx=(12, 0))
        self.setup_status = ttk.Label(header, style="PageHint.TLabel", cursor="hand2")
        self.setup_status.pack(side="right", pady=(8, 0))
        self.setup_status.bind("<Button-1>", lambda _e: self.post_settings_menu())

    def post_settings_menu(self):
        button = self.settings_button
        self.settings_menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())

    def lock_settings(self, running):
        """Setup must not change under a running worker; the journal stays open."""
        menu = getattr(self, "settings_menu", None)
        if menu is None:
            return
        for label, _command, locked in self.settings_items:
            if locked:
                menu.entryconfigure(label, state="disabled" if running else "normal")

    # ---- wizard -----------------------------------------------------------------

    def wizard_sequence(self):
        return [name for name, _title, _note in WIZARD_PAGES]

    def page_done(self, name, progress=None):
        progress = progress or self.progress_state()
        return {"hh": progress.hh, "ai": progress.no_letter or (progress.chatgpt and progress.model),
                "resume": progress.resume and progress.search, "done": True}[name]

    def setup_complete(self):
        """Setup kept between starts: login, AI or the no-letter mode, a model and a saved search."""
        progress = self.progress_state()
        return (self.signed_hh and (progress.no_letter or (self.ai_ready and progress.model))
                and bool(self.searches))

    def decide_start(self):
        """The wizard opens until setup is done once; later a lost login only warns (#5)."""
        from gui_profile import read_option, save_option
        if not read_option("setup_done"):
            if not self.setup_complete():
                self.show_wizard()
                return
            try:
                save_option("setup_done", True)  # A profile set up before the wizard existed.
            except (OSError, ValueError):
                pass
        self.root.after_idle(self.check_lost_login)

    def show_wizard(self, page=None, settings=False):
        self.wizard_settings = settings
        if page is None:  # The first unfinished step.
            page = next((name for name in self.wizard_sequence() if not self.page_done(name)), "done")
        self.body.pack_forget()
        self.wizard.pack(fill="both", expand=True)
        self.show_page(page)

    def show_page(self, name):
        if name not in self.wizard_sequence():
            name = "hh"
        self.wizard_page = name
        for key, frame in self.wizard_pages.items():
            if key == name:
                frame.grid()
            else:
                frame.grid_remove()
        title, note = next((title, note) for key, title, note in WIZARD_PAGES if key == name)
        self.wizard_title.configure(text=title)
        self.wizard_note.configure(text=note)
        self.refresh_wizard()
        self.say(self.page_hint(name))

    def page_hint(self, name):
        """The InfoBar line for a setup page: what to do on it, or that «Далее» is open."""
        if name == "done":
            return "Нажми «Перейти к откликам»."
        if self.page_done(name):
            return "Шаг выполнен. Нажми «Далее»." if not self.wizard_settings else "Готово. Можно закрыть настройки."
        return {"hh": "Нажми «Войти в HH» и введи email или телефон, затем код.",
                "ai": ("Нажми «Войти в ChatGPT»." if self.provider.kind == "chatmock" and not self.ai_ready
                       else "Нажми «Проверить подключение»: модель выберется сама." if self.ai_ready
                       else "Нажми «AI-провайдер…» и сохрани ключ API."),
                "resume": "Выбери резюме и введи запрос или выбери «Все подходящие для резюме»."}[name]

    def refresh_wizard(self):
        page = getattr(self, "wizard_page", None)
        if page is None or not self.wizard.winfo_manager():
            return
        sequence = self.wizard_sequence()
        if page not in sequence:
            self.show_page(page)
            return
        # The step count follows the no-letter switch, which drops the model step.
        steps = [key for key in sequence if key != "done"]
        number = steps.index(page) + 1 if page in steps else len(steps)
        self.wizard_step.configure(text="Настройка завершена" if page == "done"
                                   else f"Настройка · Шаг {number} из {len(steps)}")
        self.wizard_bar.configure(maximum=len(steps), value=number if page == "done" else number - 1)
        index = sequence.index(page)
        idle = self.session is None
        self.wizard_back.configure(state="normal" if index > 0 and idle else "disabled")
        if page == "done":
            self.wizard_next.pack_forget()
        else:
            self.wizard_next.pack(side="left", padx=(6, 0), after=self.wizard_back)
            self.wizard_next.configure(state="normal" if idle and self.page_done(page) else "disabled")
        if self.wizard_settings:
            self.wizard_close.pack(side="right")
            self.wizard_close.configure(state="normal" if idle else "disabled")
        else:
            self.wizard_close.pack_forget()

    def wizard_move(self, step):
        sequence = self.wizard_sequence()
        index = sequence.index(self.wizard_page)
        if step > 0 and not self.page_done(self.wizard_page):
            return  # «Далее» waits for this step.
        if 0 <= index + step < len(sequence):
            self.show_page(sequence[index + step])

    def close_wizard(self):
        self.wizard.pack_forget()
        self.body.pack(fill="both", expand=True)
        self.wizard_page = None
        self.refresh_steps()
        self.say(self.next_hint())

    def finish_wizard(self):
        from gui_profile import save_option
        try:
            save_option("setup_done", True)
        except (OSError, ValueError):
            self.append_log("Не удалось сохранить отметку о завершённой настройке.")
        self.close_wizard()
        self.say("Настройка завершена. Начни с «Предпросмотр писем»: он ничего не отправляет.", "success")

    def check_lost_login(self):
        """Setup was done, but a login or key is gone: one warning with one button, not the wizard."""
        if self.wizard.winfo_manager():
            return
        if not self.signed_hh:
            self.say("Вход в HH не выполнен или истёк. Нажми «Войти».", "warning",
                     action=("Войти", lambda: self.show_wizard("hh", settings=True)))
        elif not self.no_letter.get() and not self.ai_ready:
            if self.provider.kind == "chatmock":
                self.say("Вход в ChatGPT не выполнен или истёк. Нажми «Войти».", "warning",
                         action=("Войти", lambda: self.show_wizard("ai", settings=True)))
            else:
                self.say("Ключ AI-провайдера не найден. Нажми «Изменить ключ».", "warning",
                         action=("Изменить ключ", self.edit_provider))

    def toggle_auto(self):
        if self.auto_frame.winfo_ismapped():
            self.auto_frame.pack_forget()
            self.auto_toggle.configure(text="▸ Дополнительно: автоотклики без проверки")
        else:
            self.auto_frame.pack(fill="x", pady=(0, 10), after=self.auto_toggle)
            self.auto_toggle.configure(text="▾ Дополнительно: автоотклики без проверки")

    def text_box(self, parent, height, readonly=True):
        frame = self.ttk.Frame(parent)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        text = self.tk.Text(frame, height=height, wrap="word", font=self.text_font, relief="flat",
                            borderwidth=0, padx=12, pady=10, background=FIELD if readonly else CONTROL,
                            foreground=TEXT, selectbackground=ACCENT, selectforeground=ON_ACCENT, highlightthickness=1, highlightbackground=BORDER,
                            highlightcolor=ACCENT, insertbackground=TEXT,
                            state="disabled" if readonly else "normal", undo=not readonly)
        if readonly:
            self.make_copyable(text)
        else:
            self.make_editable(text)
        bar = self.ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=bar.set)
        text.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        return frame, text

    def make_copyable(self, text):
        """Let a disabled Text be copied: Tk gives it no focus on Windows and
        maps Ctrl+C to Cyrillic keysyms in the Russian layout."""
        def copy(_event=None):
            try:
                selected = text.get("sel.first", "sel.last")
            except self.tk.TclError:
                selected = text.get("1.0", "end-1c")
            if selected:
                self.root.clipboard_clear()
                self.root.clipboard_append(selected)
            return "break"

        def select_all(_event=None):
            text.tag_add("sel", "1.0", "end-1c")
            return "break"

        def control_key(event):
            # Windows virtual-key codes do not depend on the keyboard layout.
            actions = {67: copy, 65: select_all, 45: copy}  # C, A, Insert
            action = actions.get(event.keycode)
            return action(event) if action else None

        menu = self.tk.Menu(text, tearoff=0)
        menu.add_command(label="Копировать", command=copy)
        menu.add_command(label="Выделить всё", command=select_all)
        text.bind("<Button-1>", lambda _e: text.focus_set(), add="+")
        text.bind("<Control-KeyPress>", control_key)
        text.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

    def make_editable(self, text):
        """Give an editable Text clipboard keys that work in any keyboard layout
        and a context menu. Each change fires <<Edited>> so callers can re-validate."""
        def run(virtual):
            def action(_event=None):
                try:
                    text.event_generate(virtual)
                except self.tk.TclError:  # Nothing to undo or an empty clipboard.
                    pass
                text.event_generate("<<Edited>>")
                return "break"
            return action

        def select_all(_event=None):
            text.tag_add("sel", "1.0", "end-1c")
            text.mark_set("insert", "end-1c")
            return "break"

        cut, copy, paste = run("<<Cut>>"), run("<<Copy>>"), run("<<Paste>>")
        undo, redo = run("<<Undo>>"), run("<<Redo>>")

        def control_key(event):
            # Windows virtual-key codes do not depend on the keyboard layout.
            actions = {67: copy, 86: paste, 88: cut, 65: select_all, 90: undo, 89: redo, 45: copy}
            action = actions.get(event.keycode)
            return action(event) if action else None

        menu = self.tk.Menu(text, tearoff=0)
        menu.add_command(label="Вырезать", command=cut)
        menu.add_command(label="Копировать", command=copy)
        menu.add_command(label="Вставить", command=paste)
        menu.add_separator()
        menu.add_command(label="Выделить всё", command=select_all)
        text.clipboard_keys = control_key  # Tests call it: a hidden window gets no key events.
        text.bind("<Control-KeyPress>", control_key)
        text.bind("<Button-3>", lambda e: (text.focus_set(), menu.tk_popup(e.x_root, e.y_root)))

    def build_main(self, main):
        ttk = self.ttk
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)
        top = ttk.Frame(main)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        top.columnconfigure(1, weight=1)
        self.heading = ttk.Label(top, text="С чего начать", style="Heading.TLabel")
        self.heading.grid(row=0, column=0, sticky="w")
        self.counter = ttk.Label(top, style="Hint.TLabel")
        self.counter.grid(row=1, column=0, columnspan=2, sticky="w")
        self.progress = ttk.Progressbar(top, mode="indeterminate", length=120,
                                        style="Accent.Horizontal.TProgressbar")
        self.progress.grid(row=0, column=2, padx=(0, 8))
        self.progress.grid_remove()
        self.stop_button = ttk.Button(top, text="Остановить", command=self.stop, state="disabled")
        self.stop_button.grid(row=0, column=3)
        self.stop_button.grid_remove()
        self.force_button = ttk.Button(top, text="Прервать сразу…", command=self.force, state="disabled")
        self.force_button.grid(row=0, column=4, padx=(4, 0))
        self.force_button.grid_remove()
        self.view_button = ttk.Button(top, text="Как начать", command=self.toggle_view)
        self.view_button.grid(row=0, column=5, padx=(8, 0))
        self.log_button = ttk.Button(top, text="Журнал…", command=self.open_log)
        self.log_button.grid(row=0, column=6, padx=(6, 0))

        self.welcome = welcome = ttk.Frame(main, padding=(4, 4, 4, 4))
        welcome.grid(row=1, column=0, sticky="nsew")
        welcome.columnconfigure(0, weight=1)
        labels = []
        for row, (style, text) in enumerate(WELCOME):
            label = ttk.Label(welcome, text=text, style=style, justify="left")
            label.grid(row=row, column=0, sticky="w", pady=(12 if style == "Subheading.TLabel" else 4, 0))
            labels.append(label)
        # Fluent typography: keep lines near 60 characters for easy reading.
        welcome.bind("<Configure>", lambda e: [item.configure(wraplength=min(max(e.width - 16, 300), 560))
                                               for item in labels])
        # The one primary call to action on the page.
        self.next_button = ttk.Button(welcome, command=self.do_next, style="Accent.TButton", cursor="hand2")
        self.next_button.grid(row=len(WELCOME), column=0, sticky="w", pady=(20, 0), ipady=2)

        self.work = work = ttk.Frame(main)
        work.grid(row=1, column=0, sticky="nsew")
        work.columnconfigure(0, weight=1)
        history = ttk.Frame(work)
        history.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        history.columnconfigure(1, weight=1)
        ttk.Label(history, text="Готовые письма:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.preview_box = ttk.Combobox(history, state="disabled")
        self.preview_box.grid(row=0, column=1, sticky="ew")
        self.preview_box.bind("<<ComboboxSelected>>", self.select_preview)

        panel = ttk.Frame(work)
        panel.grid(row=1, column=0, sticky="nsew", pady=(0, 8))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(1, weight=1)
        # The journal lives in its own window (#11): the work view shows only the vacancy.
        ttk.Label(panel, text="Вакансия или вопрос").grid(row=0, column=0, sticky="w", pady=(0, 2))
        frame, self.context = self.text_box(panel, 5)
        frame.grid(row=1, column=0, sticky="nsew")
        self.letter_label = ttk.Label(work, text="Сопроводительное письмо")
        self.letter_label.grid(row=2, column=0, sticky="w")
        frame, self.letter = self.text_box(work, 7)
        frame.grid(row=3, column=0, sticky="nsew", pady=(2, 6))

        self.decision = decision = ttk.Frame(work)
        decision.grid(row=4, column=0, sticky="ew")
        self.yes = ttk.Button(decision, text="Отправить письмо", style="Accent.TButton",
                              command=lambda: self.respond("yes"))
        self.no = ttk.Button(decision, text="Пропустить", command=lambda: self.respond("no"))
        self.yes.pack(side="left")
        self.no.pack(side="left", padx=6)
        ttk.Label(decision, text="Текст уйдёт без изменений.",
                  style="Hint.TLabel").pack(side="left", padx=6)

        self.chat_frame = self.card(work)
        self.chat_frame.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        self.refine_label = ttk.Label(self.chat_frame, style="Subheading.TLabel")
        self.refine_label.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        self.instruction = ttk.Entry(self.chat_frame)
        self.instruction.grid(row=1, column=0, sticky="ew")
        self.instruction.bind("<Return>", lambda _e: self.refine())
        self.edit = ttk.Button(self.chat_frame, text="Переписать", command=self.refine)
        self.edit.grid(row=1, column=1, sticky="ew", padx=(6, 0))
        self.manual_label = ttk.Label(self.chat_frame)
        self.manual_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.manual = self.tk.Text(self.chat_frame, height=2, wrap="word", font=self.text_font,
                                   relief="flat", borderwidth=0, padx=10, pady=8, background=CONTROL,
                                   foreground=TEXT, selectbackground=ACCENT, selectforeground=ON_ACCENT, highlightthickness=1, highlightbackground=CONTROL_BORDER,
                                   highlightcolor=ACCENT, insertbackground=TEXT)
        self.manual.grid(row=3, column=0, sticky="ew")
        self.manual_button = ttk.Button(self.chat_frame, text="Взять мой текст", command=self.manual_edit)
        self.manual_button.grid(row=3, column=1, sticky="new", padx=(6, 0))
        self.option = ttk.Combobox(self.chat_frame, state="readonly")
        self.option.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        self.edit_hint = ttk.Label(self.chat_frame, style="Hint.TLabel")
        self.edit_hint.grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))

        work.rowconfigure(1, weight=1, minsize=110)
        work.rowconfigure(3, weight=2, minsize=130)
        self.show_welcome()

    # ---- views ------------------------------------------------------------------

    def show_welcome(self):
        self.work.grid_remove()
        self.welcome.grid()
        self.heading.configure(text="С чего начать")
        self.view_button.configure(text="К работе")

    def show_work(self):
        welcome = getattr(self, "welcome", None)
        if welcome is None:
            return
        welcome.grid_remove()
        self.work.grid()
        self.view_button.configure(text="Как начать")

    def toggle_view(self):
        if self.welcome.winfo_ismapped():
            self.show_work()
            self.heading.configure(text="Вакансия или вопрос")
        else:
            self.show_welcome()

    # ---- step guidance ----------------------------------------------------------

    def progress_state(self):
        from gui_steps import Progress
        index = self.resume_box.current()
        return Progress(hh=self.signed_hh, chatgpt=self.ai_ready, ai_kind=self.provider.kind,
                        resume=0 <= index < len(self.resumes),
                        search=self.search_mode.get() == "all" or bool(self.search.get().strip()),
                        model=self.free_mode() or self.model.get() not in {"", "CHOOSE_AFTER_LOGIN"},
                        no_letter=self.no_letter.get())

    def next_hint(self):
        from gui_steps import next_hint
        return next_hint(self.progress_state())

    def next_action(self, progress):
        if not progress.hh:
            return "hh_login", "Начать: войти в HH"
        if not progress.chatgpt:
            return (("chatgpt_login", "Войти в ChatGPT") if progress.ai_kind == "chatmock"
                    else ("provider", "Сохранить ключ AI-провайдера"))
        if not progress.resume:
            return "resumes", "Обновить список резюме"
        if not progress.search:
            return "search", "Ввести поисковый запрос"
        if not progress.model:
            return "models", "Проверить подключение"
        return "preview", "Запустить предпросмотр писем"

    def auto_load_resumes(self):
        """Load resumes without a click once HH sign-in exists; reading only, never sending."""
        if not self.signed_hh or self.resumes:
            return
        if self.session is not None:
            self.root.after(1000, self.auto_load_resumes)  # Wait for the current run to end.
            return
        self.launch("resumes")

    def do_next(self):
        action, _text = self.next_action(self.progress_state())
        pages = {"hh_login": "hh", "chatgpt_login": "ai", "provider": "ai", "models": "ai"}
        if action in pages:  # Setup actions live on the setup pages (#5).
            self.show_wizard(pages[action], settings=True)
        elif action == "search":
            self.search_target().focus_set()
        else:
            self.launch(action)

    def refresh_steps(self):
        """Lock unready actions, highlight the one next button and keep the setup status current."""
        if getattr(self, "wizard_pages", None) is None:
            return
        from gui_steps import blocker, parse_auto_settings
        progress = self.progress_state()
        self.hh_state.configure(text="✓ вход выполнен" if progress.hh else "вход не выполнен",
                                style="Done.TLabel" if progress.hh else "Hint.TLabel")
        chatmock = self.provider.kind == "chatmock"
        if chatmock:
            self.chatgpt_button.grid()
            self.chatgpt_state.grid()
            self.chatgpt_state.configure(text="✓ вход выполнен" if progress.chatgpt else "вход не выполнен",
                                         style="Done.TLabel" if progress.chatgpt else "Hint.TLabel")
        else:
            self.chatgpt_button.grid_remove()
            self.chatgpt_state.grid_remove()
        status = {"chatmock": "", "openai": " · ✓ ключ сохранён" if progress.chatgpt else " · нужен ключ",
                  "local": ""}[self.provider.kind]
        self.provider_state.configure(text=self.provider.title + status,
                                      style="Done.TLabel" if progress.chatgpt and not chatmock else "Hint.TLabel")
        from urllib.parse import urlsplit
        provider = ("ChatGPT" if chatmock else "локальная модель" if self.provider.kind == "local"
                    else urlsplit(self.provider.base_url).hostname or "API")
        free = self.free_mode()
        parts = ["HH " + ("✓" if progress.hh else "✗"),
                 ("бесплатные модели" if free else provider) + " " + ("✓" if progress.chatgpt else "✗")]
        if not free:
            parts.append(self.model.get() or "модель не выбрана")
        keys = getattr(self, "free_keys", [])
        self.model_state.configure(
            text=("Модели выбирает приложение: ключи — " + (", ".join(keys) if keys else "нет") + "." if free
                  else f"Модель: {self.model.get()} (выбрана автоматически)." if self.model.get()
                  else "Модель выберется сама после проверки подключения."),
            style="Done.TLabel" if progress.model and progress.chatgpt else "Hint.TLabel")
        self.free_hint.configure(text=(
            "Письмо пишет первая доступная модель из списка проверенных бесплатных моделей Groq, Gemini и "
            "OpenRouter. Когда у модели кончается лимит или она недоступна, приложение берёт следующую. "
            "Ключ ещё одного бесплатного провайдера удлиняет список." if free else
            "Ключ OpenRouter, Gemini или Groq включает бесплатные модели, которые меняются сами."))
        if progress.no_letter:
            parts.append("отклики без письма")
        self.setup_status.configure(text=" · ".join(parts))
        _values, auto_problem = parse_auto_settings(self.limit.get(), self.minimum.get(), self.maximum.get())
        self.auto_error.configure(text=auto_problem or "")
        action, text = self.next_action(progress)
        self.next_button.configure(text=text)
        idle = self.session is None
        for name, button in self.action_buttons.items():
            ready = blocker(name, progress) is None and (name != "auto" or auto_problem is None)
            button.configure(state="normal" if idle and ready else "disabled",
                             style="Accent.TButton" if name == action else "TButton")
        for name, button in getattr(self, "extra_action_buttons", ()):
            button.configure(state="normal" if idle and blocker(name, progress) is None else "disabled")
        self.next_button.configure(state="normal" if idle else "disabled")
        self.refresh_wizard()

    # ---- state helpers ----------------------------------------------------------

    def replace_text(self, widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def append_log(self, text):
        """One journal line: kept in memory, shown in the «Журнал» window, appended to the log file."""
        import time
        line = time.strftime("%H:%M:%S  ") + text
        lines = getattr(self, "log_lines", None)
        if lines is None:
            return
        lines.append(line)
        del lines[:-LOG_MEMORY_LINES]
        write_log_file(getattr(self, "log_path", None), time.strftime("%Y-%m-%d ") + line)
        view = getattr(self, "log_view", None)
        if view is not None:
            try:
                view.configure(state="normal")
                view.insert("end", line + "\n")
                view.see("end")
                view.configure(state="disabled")
            except self.tk.TclError:
                self.log_view = None

    def open_log(self):
        """The technical journal in its own window, away from the work view (#11)."""
        tk, ttk = self.tk, self.ttk
        window = getattr(self, "log_window", None)
        if window is not None and window.winfo_exists():
            window.deiconify()
            window.lift()
            return
        window = self.log_window = tk.Toplevel(self.root, background=CARD)
        window.title("Журнал")
        window.geometry("860x480")
        window.transient(self.root)
        dark_title_bar(window)
        frame = ttk.Frame(window, padding=14)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        ttk.Label(frame, text="Технические подробности запусков. Они нужны, чтобы разобрать сбой.",
                  style="Hint.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 6))
        box, view = self.text_box(frame, 18)
        box.grid(row=1, column=0, sticky="nsew")
        view.configure(font=("Consolas", 9))
        self.replace_text(view, "".join(line + "\n" for line in self.log_lines))
        view.see("end")
        self.log_view = view
        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, sticky="w", pady=(10, 0))

        def copy():
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(self.log_lines))
            self.say("Журнал скопирован в буфер обмена.")

        def folder():
            import os
            path = getattr(self, "log_path", None)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                os.startfile(path.parent)
            except (AttributeError, OSError):
                self.say("Не удалось открыть папку с журналом.", "warning")

        def close():
            self.log_view = self.log_window = None
            window.destroy()
        ttk.Button(buttons, text="Скопировать всё", command=copy).pack(side="left")
        ttk.Button(buttons, text="Открыть папку с журналом", command=folder).pack(side="left", padx=6)
        ttk.Button(buttons, text="Закрыть", command=close).pack(side="left")
        window.protocol("WM_DELETE_WINDOW", close)

    def set_busy(self, running):
        for button in self.buttons:
            button.configure(state="disabled" if running else "normal")
        self.lock_settings(running)
        style_box = getattr(self, "style_box", None)
        if style_box is not None:
            style_box.configure(state="disabled" if running else "readonly")
        self.stop_button.configure(state="normal" if running else "disabled", text="Остановить")
        if running:
            self.stop_button.grid()
        else:
            self.stop_button.grid_remove()
        self.force_button.configure(state="disabled")
        self.force_button.grid_remove()
        progress = getattr(self, "progress", None)
        if progress is not None:
            if running:
                progress.grid()
                progress.start(12)
            else:
                progress.stop()
                progress.grid_remove()
        if not running:
            self.refresh_steps()
        elif getattr(self, "next_button", None) is not None:
            self.next_button.configure(state="disabled")

    def clear_review(self):
        self.review_kind = None
        for button in self.review_buttons:
            button.configure(state="disabled")
        self.credential.delete(0, "end")
        self.instruction.delete(0, "end")
        self.manual.delete("1.0", "end")
        self.option.configure(values=())
        self.option.set("")
        # Show only the controls the current decision needs.
        for frame in (getattr(self, "credential_frame", None), getattr(self, "chat_frame", None),
                      getattr(self, "decision", None)):
            if frame is not None:
                frame.grid_remove()

    def selected_id(self):
        index = self.resume_box.current()
        if not 0 <= index < len(self.resumes):
            raise ValueError("Сначала загрузи и выбери опубликованное резюме в блоке «Резюме и поиск».")
        return self.resumes[index]["id"]

    def search_value(self):
        """Text sent to HH; "" asks HH for all vacancies it suggests for the resume."""
        mode = getattr(self, "search_mode", None)
        return "" if mode is not None and mode.get() == "all" else self.search.get()

    def search_target(self):
        """The search entry the person sees now: the wizard page or the main screen."""
        wizard = getattr(self, "wizard", None)
        return (self.wizard_search_entry if wizard is not None and wizard.winfo_manager()
                else self.search_entry)

    def update_search_mode(self):
        if not hasattr(self, "search_entries"):
            return
        all_mode = self.search_mode.get() == "all"
        for entry in self.search_entries:
            entry.configure(state="disabled" if all_mode else "normal")
        for hint in self.search_hints:
            hint.configure(text="HH подберёт вакансии под это резюме, как в разделе «Подходящие вакансии». "
                                "Выбор сохраняется сам." if all_mode else
                           "Например: Python разработчик. Запрос сохраняется сам.")

    def select_search_mode(self):
        self.update_search_mode()
        if self.search_mode.get() == "query":
            self.search_target().focus_set()
        self.remember_search()
        self.refresh_steps()

    def remember_search(self):
        """Save the search choice for the selected resume without asking."""
        from gui_profile import read_searches, save_search
        resume_id, search = self.current_resume, self.search_value()
        if (not resume_id or (search and not search.strip()) or (not search and self.search_mode.get() != "all")
                or len(search) > 4000 or self.searches.get(resume_id) == search):
            return
        try:
            save_search(resume_id, search)
            self.searches = read_searches()
        except (OSError, ValueError):
            self.append_log("Не удалось сохранить поисковый запрос.")

    def select_resume(self, _event=None):
        self.remember_search()
        try:
            self.current_resume = self.selected_id()
            saved = self.searches.get(self.current_resume)
            self.search_mode.set("all" if saved == "" else "query")
            self.search.set(saved or "")
        except ValueError:
            self.current_resume = None
        self.update_search_mode()
        self.refresh_steps()

    def select_model(self, _event=None):
        from gui_profile import set_model
        try:
            set_model(self.model.get())
        except (OSError, ValueError):
            self.dialogs.showerror("Модель", "Не удалось сохранить модель. Выбери её снова.")
        self.refresh_steps()

    def show_first_letter(self):
        """After a preview the view shows a ready letter, not the last skipped vacancy."""
        letters = [index for index, item in enumerate(getattr(self, "previews", [])) if "reason" not in item]
        if letters and getattr(self, "preview_box", None) is not None:
            self.preview_box.current(letters[0])
            self.select_preview()

    def select_preview(self, _event=None):
        if self.session is not None and self.session.state.pending is not None:
            return  # Never replace the displayed text underneath an approval.
        index = self.preview_box.current()
        if 0 <= index < len(self.previews):
            value = self.previews[index]
            self.replace_text(self.context, describe_vacancy(value))
            self.replace_text(self.letter, value["text"])

    def save(self):
        from gui_profile import save_search, set_model, read_searches
        try:
            resume_id = self.selected_id()
            search = self.search_value()
            all_mode = getattr(self, "search_mode", None) is not None and self.search_mode.get() == "all"
            if not all_mode and not search.strip():
                if getattr(self, "search_entry", None) is not None:
                    self.search_target().focus_set()
                raise ValueError("Введи поисковый запрос на HH в блоке «Резюме и поиск» или выбери «Все подходящие для резюме».")
            if len(search) > 4000:
                raise ValueError("Поисковый запрос слишком длинный: максимум 4000 символов.")
            save_search(resume_id, search)
            if self.model.get() not in {"", "CHOOSE_AFTER_LOGIN"}:
                set_model(self.model.get())
            self.searches = read_searches()
            return True
        except (OSError, ValueError) as error:
            self.dialogs.showerror("Настройки", str(error))
            return False

    def check_recovery(self):
        from gui_recovery import RecoveryMarker, WARNING
        try:
            pending = RecoveryMarker().pending()
            if pending is None:
                self.recovery_acknowledgement = None
                return True
            if pending == getattr(self, "recovery_acknowledgement", None):
                return True
            self.say("Прошлая отправка завершилась без подтверждения. Проверь отклики на hh.ru перед новой отправкой.", "warning")
            if self.dialogs.askyesno("Проверь прошлую отправку", WARNING):
                self.recovery_acknowledgement = pending
                return True
            return False
        except OSError:
            self.dialogs.showerror("Проверка прошлой отправки",
                                   "Не удалось проверить, чем закончилась прошлая отправка. Новая отправка запрещена.")
            return False

    # ---- actions ----------------------------------------------------------------

    def launch(self, action):
        from gui_controller import Session, action_settings
        from gui_profile import set_model
        from gui_recovery import MUTATING_ACTIONS
        if self.session is not None:
            return
        try:
            if action in MUTATING_ACTIONS and not self.check_recovery():
                return
            resume, search = "", ""
            # The free mode picks models itself; applications without a letter need none (#9).
            needs_model = not self.free_mode() and not (action in {"apply", "auto"} and self.no_letter.get())
            if action in REVIEW_ACTIONS and needs_model:
                if self.model.get() == "CHOOSE_AFTER_LOGIN" or not self.model.get():
                    raise ValueError("Подключи AI: «Настройки» → «AI-провайдер…» → «Проверить подключение».")
                set_model(self.model.get())
            if action in {"preview", "apply", "auto"}:
                resume, search = self.selected_id(), self.search_value()
                if not self.save():
                    return
            if action in {"apply", "auto"} and self.all_resumes.get():
                missing = [item.get("title") or item["id"] for item in self.resumes
                           if item["id"] not in self.searches]
                if missing:
                    raise ValueError("Включён режим «со всех резюме по очереди», но нет запроса для резюме:\n• "
                                     + "\n• ".join(missing) + "\n\nВыбери каждое резюме в блоке «Резюме и поиск» и задай для него "
                                     "запрос или режим «Все подходящие». Или сними этот флажок.")
            limit, minimum, maximum, consent = 1, 60, 180, False
            if action == "auto":
                from auto_send import validate_settings
                from gui_steps import parse_auto_settings
                values, problem = parse_auto_settings(self.limit.get(), self.minimum.get(), self.maximum.get())
                if problem:
                    raise ValueError(problem)
                limit, minimum, maximum = values
                validate_settings(limit, minimum, maximum)
                consent = self.dialogs.askyesno("Автоотклики без проверки",
                    f"Отправить до {limit} успешных откликов без проверки каждого письма?\n\n"
                    "Лимит считает только отклики, которые принял HH. Пропущенные вакансии, "
                    "неудачные отправки и повторы после капчи в лимит не входят.\n\n"
                    f"Пауза между откликами: от {minimum:g} до {maximum:g} секунд.\n"
                    "Пауза не защищает от блокировки аккаунта на HH.\n\n"
                    "Если результат отправки неизвестен, проверь отклики на hh.ru. Не запускай повторно вслепую.")
                if not consent:
                    return
            settings = action_settings(action, resume=resume, search=search, all_resumes=self.all_resumes.get(),
                limit=limit, min_pause=minimum, max_pause=maximum, consent=consent,
                no_letter=self.no_letter.get())
            self.session = Session(action, settings,
                recovery_acknowledgement=getattr(self, "recovery_acknowledgement", None))
            self.clear_review()
            self.previews = []
            self.preview_box.configure(values=(), state="disabled")
            self.preview_box.set("")
            self.replace_text(self.context, "")
            self.replace_text(self.letter, "")
            self.prepare_run_view(action, limit)
            title = ACTION_TITLES.get(action, action)
            self.say("Выполняется: " + title + ". Дождись результата или нажми «Остановить».")
            self.append_log("Запуск: " + title)
            self.set_busy(True)
            self.session.start()
        except (OSError, ValueError) as error:
            self.dialogs.showerror("Запуск", str(error))

    def prepare_run_view(self, action, limit):
        self.run_counts = {"vacancies": 0, "sent": 0, "captcha": 0, "unconfirmed": 0,
                           "limit": limit if action == "auto" else None}
        self.review_note = None
        self.stop_reason = None
        self.letter_model, self.model_switched = None, False
        if getattr(self, "heading", None) is None:
            return
        if action in REVIEW_ACTIONS:
            self.show_work()
            self.heading.configure(text=ACTION_TITLES[action].capitalize())
            self.letter_label.configure(text="Ответ в чат" if action == "chats" else
                                        "Сопроводительное письмо (предпросмотр, не отправляется)"
                                        if action == "preview" else "Сопроводительное письмо")
        self.update_counter()

    def save_no_letter(self):
        from gui_profile import save_option
        try:
            save_option("no_letter", bool(self.no_letter.get()))
        except (OSError, ValueError):
            self.append_log("Не удалось сохранить режим без сопроводительного письма.")
        self.refresh_steps()

    def update_counter(self):
        counter = getattr(self, "counter", None)
        counts = getattr(self, "run_counts", None)
        if counter is None or counts is None:
            return
        parts = []
        if counts["vacancies"]:
            parts.append(f"вакансий: {counts['vacancies']}")
        if counts["sent"] or counts["limit"]:
            parts.append((f"успешных откликов: {counts['sent']} из {counts['limit']}" if counts["limit"]
                          else f"отправлено: {counts['sent']}"))
        if counts.get("captcha"):
            parts.append(f"отклонено капчей: {counts['captcha']}")
        if counts.get("unconfirmed"):
            parts.append(f"без подтверждения HH: {counts['unconfirmed']}")
        text = " · ".join(parts)
        counter.configure(text=text[:1].upper() + text[1:])

    def respond(self, action, text=""):
        from gui_protocol import ProtocolError
        if self.session is None:
            return
        try:
            self.session.respond(action, text)
            self.clear_review()  # Edits consume this review; next one has a fresh ID.
            self.say("Правка принята. Новый вариант придёт на проверку и сам не отправится."
                            if action in {"edit", "manual"} else "Решение принято. Жду следующий шаг.")
        except ProtocolError:
            self.stop()

    def submit_credential(self):
        if str(self.submit["state"]) == "normal" and self.credential.get().strip():
            self.respond("submit", self.credential.get())

    def refine(self):
        if str(self.edit["state"]) == "normal" and self.instruction.get().strip():
            self.respond("edit", self.instruction.get())

    def manual_edit(self):
        text = self.option.get() if self.option["values"] else self.manual.get("1.0", "end-1c")
        if text.strip():
            self.respond("manual", text)

    def stop(self):
        if self.session is not None:
            self.session.cancel()
            self.clear_review()
            self.say("Останавливаю после текущего шага. Если текст уже отправлялся, проверь hh.ru перед повтором.", "warning")
            stop_button = getattr(self, "stop_button", None)
            if stop_button is not None:
                stop_button.configure(state="disabled", text="Останавливаю…")
                self.force_button.grid()
                self.force_button.configure(state="normal")

    def force(self):
        if self.session is not None and self.dialogs.askyesno("Прервать сразу",
                "Процесс завершится немедленно. Неизвестно, ушла ли последняя отправка. "
                "Вход может потребоваться заново.\n\nПосле этого проверь отклики на hh.ru. Прервать?"):
            self.session.force()
            self.clear_review()

    def close(self):
        if self.session is not None:
            if self.dialogs.askyesno("Операция ещё идёт",
                    "Остановить операцию? Окно не закроется сразу. "
                    "Дождись остановки и закрой окно снова."):
                self.stop()
            return
        self.root.destroy()

    # ---- self-update ------------------------------------------------------------

    UPDATE_INTERVAL_MS = 6 * 3600 * 1000

    def run_in_background(self, work, done):
        """Run work() in a thread; call done(result, error) on the Tk thread."""
        import threading
        box = {}

        def target():
            try:
                box["result"] = work()
            except Exception as error:  # updater.UpdateError carries a plain sentence.
                box["error"] = error
        thread = threading.Thread(target=target, daemon=True)
        thread.start()

        def wait():
            if thread.is_alive():
                self.root.after(200, wait)
            else:
                done(box.get("result"), box.get("error"))
        self.root.after(200, wait)

    def check_updates(self):
        import updater
        self.root.after(self.UPDATE_INTERVAL_MS, self.check_updates)
        if not updater.enabled() or self.update_release is not None:
            return

        def done(release, error):
            if error is None and release is not None:
                self.update_release = release
                self.update_button.configure(text="Обновить до " + release["version"])
                self.update_button.pack(side="right", padx=(0, 12))
                self.append_log("Доступна версия " + release["version"] + ". Кнопка «Обновить» — справа вверху.")
        self.run_in_background(updater.check, done)

    def apply_update(self):
        import updater
        release = self.update_release
        if release is None:
            return
        if self.session is not None:
            self.dialogs.showinfo("Обновление", "Сейчас идёт запуск. Дождись его завершения, затем обнови программу.")
            return
        if not self.dialogs.askyesno("Обновление", f"Установить версию {release['version']}?\n\n"
                                     "Программа закроется и через несколько секунд откроется снова. "
                                     "Вход в HH, ключи и настройки сохранятся."):
            return
        self.update_button.configure(state="disabled", text="Скачиваю обновление…")
        self.say("Скачиваю обновление " + release["version"] + ". Не закрывай окно.")

        def done(command, error):
            if error is not None or self.session is not None:
                self.update_button.configure(state="normal", text="Обновить до " + release["version"])
                message = str(error) if isinstance(error, updater.UpdateError) else "Обновление не удалось."
                self.say(message + " Программа работает на текущей версии.", "error")
                self.append_log("Обновление не установлено: " + message)
                return
            updater.launch(command)
            self.root.destroy()
        self.run_in_background(lambda: updater.prepare(release), done)

    # ---- AI provider ------------------------------------------------------------

    def free_mode(self):
        import ai_provider
        provider = getattr(self, "provider", None)
        return provider is not None and ai_provider.free_mode(provider)

    def load_provider(self):
        """Current provider and whether it can generate letters (login, stored key or local)."""
        import ai_provider
        from gui_profile import read_provider
        try:
            self.provider = read_provider()
        except (OSError, ValueError, KeyError):
            self.provider = ai_provider.Provider(ai_provider.CHATMOCK, ai_provider.CHATMOCK_URL)
        self.ai_ready = ai_provider.ready(self.provider, chatgpt_signed_in=self.signed_chatgpt)
        # Which free APIs have a key: their built-in models make up the automatic chain.
        self.free_keys = [api.title for api in ai_provider.FREE_APIS
                          if ai_provider.read_key(ai_provider.parse(api.base_url)) is not None]

    def edit_provider(self):
        """Choose ChatMock, an OpenAI-compatible API (key in Windows Credential Manager) or a local model."""
        import webbrowser
        import ai_provider
        from gui_profile import set_provider
        tk, ttk = self.tk, self.ttk
        if getattr(self, "provider_window", None) is not None and self.provider_window.winfo_exists():
            self.provider_window.lift()
            return
        window = self.provider_window = tk.Toplevel(self.root, background=CARD)
        window.title("AI-провайдер")
        window.transient(self.root)
        window.resizable(False, False)
        dark_title_bar(window)
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="AI-провайдер", style="Heading.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(frame, text="Кто пишет письма и ответы в чатах.", style="Hint.TLabel").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(2, 10))
        kind = tk.StringVar(value=self.provider.kind)
        address = tk.StringVar(value=self.provider.base_url if self.provider.kind != ai_provider.CHATMOCK
                               else ai_provider.PRESETS[ai_provider.OPENAI])
        key = tk.StringVar()
        for row, value in enumerate((ai_provider.CHATMOCK, ai_provider.OPENAI, ai_provider.LOCAL), start=2):
            ttk.Radiobutton(frame, text=ai_provider.TITLES[value], value=value, variable=kind,
                            command=lambda: update()).grid(row=row, column=0, columnspan=3, sticky="w")
        ttk.Label(frame, text="Адрес API").grid(row=5, column=0, sticky="w", pady=(12, 0), padx=(0, 8))
        address_entry = ttk.Entry(frame, textvariable=address, width=44)
        address_entry.grid(row=5, column=1, columnspan=2, sticky="ew", pady=(12, 0))
        presets = ttk.Frame(frame)
        presets.grid(row=6, column=1, columnspan=2, sticky="w", pady=(4, 0))
        for title, url in (("Ollama", "http://127.0.0.1:11434/v1"), ("LM Studio", "http://127.0.0.1:1234/v1")):
            ttk.Button(presets, text=title, command=lambda value=url: address.set(value)).pack(side="left", padx=(0, 6))
        free_presets = ttk.Frame(frame)
        free_presets.grid(row=6, column=1, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(free_presets, text="Бесплатно:", style="Hint.TLabel").pack(side="left", padx=(0, 6))
        for api in ai_provider.FREE_APIS:
            ttk.Button(free_presets, text=api.title,
                       command=lambda value=api.base_url: address.set(value)).pack(side="left", padx=(0, 6))
        key_page = ttk.Button(free_presets, text="Получить ключ",
                              command=lambda: webbrowser.open(ai_provider.free_api(address.get()).key_page))
        key_page.pack(side="left", padx=(6, 0))
        key_label = ttk.Label(frame, text="Ключ API")
        key_label.grid(row=7, column=0, sticky="w", pady=(10, 0), padx=(0, 8))
        key_entry = ttk.Entry(frame, textvariable=key, show="•", width=44)
        key_entry.grid(row=7, column=1, sticky="ew", pady=(10, 0))
        forget = ttk.Button(frame, text="Удалить ключ", command=lambda: delete())
        forget.grid(row=7, column=2, sticky="w", padx=(6, 0), pady=(10, 0))
        key_hint = ttk.Label(frame, style="Hint.TLabel", wraplength=520, justify="left")
        key_hint.grid(row=8, column=1, columnspan=2, sticky="w", pady=(2, 0))
        warning = ttk.Label(frame, style="Warn.TLabel", wraplength=560, justify="left")
        warning.grid(row=9, column=0, columnspan=3, sticky="w", pady=(12, 0))
        error = ttk.Label(frame, style="Error.TLabel", wraplength=560, justify="left")
        error.grid(row=10, column=0, columnspan=3, sticky="w", pady=(6, 0))
        buttons = ttk.Frame(frame)
        buttons.grid(row=11, column=0, columnspan=3, sticky="w", pady=(12, 0))

        def chosen():
            if kind.get() == ai_provider.CHATMOCK:
                return ai_provider.Provider(ai_provider.CHATMOCK, ai_provider.CHATMOCK_URL)
            provider = ai_provider.parse(address.get())
            if provider.kind != kind.get():
                raise ValueError("Для локальной модели укажи адрес 127.0.0.1 или localhost с портом."
                                 if kind.get() == ai_provider.LOCAL else
                                 "Для внешнего провайдера укажи адрес https://, не локальный.")
            return provider

        def stored_key():
            try:
                provider = chosen()
            except ValueError:
                return None
            return ai_provider.read_key(provider) if provider.needs_key else None

        def describe_key(*_args):
            """Key hint and buttons follow the address: a free API gets its terms and key page."""
            if kind.get() != ai_provider.OPENAI:
                return
            api = ai_provider.free_api(address.get())
            saved = stored_key() is not None
            key_hint.configure(text=("Ключ сохранён. Оставь поле пустым, чтобы не менять его. " if saved else "")
                               + (f"{api.title}: {api.note} " if api else "")
                               + "Ключ хранится в диспетчере учётных данных Windows, не в файлах.")
            forget.configure(state="normal" if saved else "disabled")
            key_page.configure(state="normal" if api else "disabled")

        address.trace_add("write", describe_key)

        def update(*_args):
            current = kind.get()
            address_entry.configure(state="disabled" if current == ai_provider.CHATMOCK else "normal")
            if current == ai_provider.LOCAL and ai_provider.kind_of(address.get()) != ai_provider.LOCAL:
                address.set(ai_provider.PRESETS[ai_provider.LOCAL])
            if current == ai_provider.OPENAI and ai_provider.kind_of(address.get()) != ai_provider.OPENAI:
                address.set(ai_provider.PRESETS[ai_provider.OPENAI])
            presets.grid() if current == ai_provider.LOCAL else presets.grid_remove()
            uses_key = current == ai_provider.OPENAI
            for widget in (key_label, key_entry, forget, key_hint, free_presets):
                widget.grid() if uses_key else widget.grid_remove()
            describe_key()
            warning.configure(text="" if current == ai_provider.CHATMOCK else
                              "Тексты резюме и вакансий будут уходить выбранному провайдеру. "
                              "Его условия обработки данных — на твоей ответственности.")
            error.configure(text="")

        def delete():
            try:
                ai_provider.delete_key(chosen())
            except (OSError, ValueError) as problem:
                error.configure(text=str(problem))
                return
            update()

        def save():
            try:
                provider = chosen()
                if provider.needs_key:
                    if key.get().strip():
                        ai_provider.save_key(provider, key.get())
                    elif ai_provider.read_key(provider) is None:
                        raise ValueError("Введи ключ API: без него провайдер не ответит.")
                if provider != self.provider:
                    set_provider(provider.base_url)
                    self.model.set("")
            except (OSError, ValueError) as problem:
                error.configure(text=str(problem))
                return
            key.set("")
            close()
            self.load_provider()
            self.refresh_steps()
            self.say(f"AI-провайдер: {self.provider.title}. Теперь загрузи список моделей и выбери модель."
                     if self.ai_ready else self.next_hint(), "success" if self.ai_ready else "info")
            self.append_log("AI-провайдер изменён: " + self.provider.title)

        def close():
            key.set("")
            self.provider_window = None
            window.destroy()
        ttk.Button(buttons, text="Сохранить", style="Accent.TButton", command=save).pack(side="left")
        ttk.Button(buttons, text="Отмена", command=close).pack(side="left", padx=6)
        window.protocol("WM_DELETE_WINDOW", close)
        update()
        self.provider_controls = {"kind": kind, "address": address, "key": key, "key_entry": key_entry,
                                  "error": error, "save": save, "update": update}

    # ---- letter style -----------------------------------------------------------

    def load_styles(self, select=None):
        """Fill the step 3 selector; a damaged styles file falls back to "Исходный" with a warning."""
        from gui_profile import read_styles
        try:
            styles, active, warning = read_styles()
        except (OSError, ValueError):
            styles, active, warning = {}, "", "Не удалось прочитать стили письма."
        self.style_box.configure(values=list(styles))
        self.style_name.set(select or active)
        if warning:
            self.style_warning = warning
            self.root.after_idle(lambda: self.say(warning, "warning"))
        return styles, active

    def select_style(self, _event=None):
        from gui_profile import activate_style
        name = self.style_name.get()
        try:
            activate_style(name)
        except (OSError, ValueError, KeyError) as problem:
            self.dialogs.showerror("Стиль письма", str(problem) or "Не удалось выбрать стиль.")
            self.load_styles()
            return
        self.say(f"Стиль письма «{name}» выбран. Он действует на следующем предпросмотре.", "success")
        self.append_log("Стиль письма: " + name)

    def ask_style_name(self, title, initial=""):
        """Modal name prompt built from ttk only: no new frozen modules for code-only updates."""
        tk, ttk = self.tk, self.ttk
        parent = getattr(self, "style_window", None) or self.root
        window = tk.Toplevel(parent, background=CARD)
        window.title(title)
        window.transient(parent)
        window.resizable(False, False)
        dark_title_bar(window)
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Название стиля (до 60 символов):").pack(anchor="w")
        value = tk.StringVar(value=initial)
        entry = ttk.Entry(frame, textvariable=value, width=44)
        entry.pack(fill="x", pady=(6, 10))
        result = {"name": None}

        def finish(accepted):
            result["name"] = value.get() if accepted else None
            window.destroy()
        buttons = ttk.Frame(frame)
        buttons.pack(anchor="w")
        ttk.Button(buttons, text="OK", style="Accent.TButton", command=lambda: finish(True)).pack(side="left")
        ttk.Button(buttons, text="Отмена", command=lambda: finish(False)).pack(side="left", padx=6)
        entry.bind("<Return>", lambda _event: finish(True))
        window.bind("<Escape>", lambda _event: finish(False))
        window.protocol("WM_DELETE_WINDOW", lambda: finish(False))
        entry.select_range(0, "end")
        entry.focus_set()
        window.grab_set()
        window.wait_window()
        return result["name"]

    def edit_letter_style(self):
        """Letter styles: list, edit, create, duplicate, rename, delete. The active one is used."""
        import gui_profile
        from gui_profile import BUILTIN_STYLE, MIGRATED_STYLE, validate_prompt
        tk, ttk = self.tk, self.ttk
        if getattr(self, "style_window", None) is not None and self.style_window.winfo_exists():
            self.style_window.lift()
            return
        try:
            styles, active = self.load_styles()
        except (OSError, ValueError):
            self.dialogs.showerror("Стиль письма", "Не удалось прочитать стили письма из профиля.")
            return
        window = self.style_window = tk.Toplevel(self.root, background=CARD)
        window.title("Стиль письма")
        window.transient(self.root)
        window.geometry("780x640")
        window.minsize(580, 460)
        dark_title_bar(window)
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)
        ttk.Label(frame, text="Стиль письма", style="Heading.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(frame, wraplength=720, justify="left", text=(
            "ChatGPT получает этот текст как инструкцию для каждого письма. Опиши тон, длину, обращение "
            "и чего избегать. Данные резюме и вакансии приложение добавляет само.")).grid(
            row=1, column=0, sticky="w", pady=(6, 10))
        bar = ttk.Frame(frame)
        bar.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        bar.columnconfigure(0, weight=1)
        chosen = tk.StringVar(value=active)
        box = ttk.Combobox(bar, textvariable=chosen, state="readonly", values=list(styles))
        box.grid(row=0, column=0, sticky="ew")
        editor_frame, editor = self.text_box(frame, 14, readonly=False)
        editor_frame.grid(row=3, column=0, sticky="nsew")
        ttk.Label(frame, style="Hint.TLabel", wraplength=720, justify="left", text=(
            "Подстановки: %(vacancy_name)s — вакансия, %(employer_name)s — компания, "
            "%(resume_title)s — резюме, %(first_name)s — имя. Знак % пиши как %%. "
            "%(email)s и %(phone)s добавят твои контакты в каждое письмо.")).grid(
            row=4, column=0, sticky="w", pady=(8, 0))
        note = ttk.Label(frame, style="Hint.TLabel", wraplength=720, justify="left")
        note.grid(row=5, column=0, sticky="w", pady=(4, 0))
        error = ttk.Label(frame, style="Error.TLabel", wraplength=720, justify="left")
        error.grid(row=6, column=0, sticky="w", pady=(4, 0))
        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, sticky="ew", pady=(10, 0))
        state = {"styles": styles}

        def text():
            return editor.get("1.0", "end-1c").strip()

        def show(name):
            chosen.set(name)
            box.configure(values=list(state["styles"]))
            editor.delete("1.0", "end")
            editor.insert("1.0", state["styles"][name])
            editor.edit_reset()  # Ctrl+Z must not undo the loaded style into an empty field.
            builtin = name == BUILTIN_STYLE
            for button in (rename_button, delete_button):
                button.configure(state="disabled" if builtin else "normal")
            note.configure(text="Стиль «Исходный» не меняется. «Сохранить» создаст из правок новый стиль."
                           if builtin else "")
            check()

        def reload(name=None):
            styles, active = self.load_styles(select=name)
            state["styles"] = styles
            show(name if name in styles else active)

        def run(action, success):
            try:
                result = action()
            except (OSError, ValueError, KeyError) as problem:
                error.configure(text=str(problem) or "Операция со стилем не удалась.")
                return None
            reload(result if isinstance(result, str) else None)
            self.say(success, "success")
            return result

        def check(_event=None):
            try:
                validate_prompt(text())
                error.configure(text="")
                save_button.configure(state="normal")
            except ValueError as problem:
                error.configure(text=str(problem))
                save_button.configure(state="disabled")

        def save():
            name = chosen.get()
            if name == BUILTIN_STYLE:
                if text() == state["styles"][BUILTIN_STYLE]:
                    run(lambda: gui_profile.activate_style(BUILTIN_STYLE) or BUILTIN_STYLE,
                        "Выбран стиль «Исходный».")
                    close()
                    return
                name = gui_profile.free_style_name(MIGRATED_STYLE, state["styles"])
            saved = run(lambda: gui_profile.save_style(name, text()),
                        f"Стиль письма «{name}» сохранён и выбран. Проверь его на предпросмотре писем.")
            if saved:
                self.append_log("Стиль письма сохранён: " + saved)
                close()

        def create():
            name = self.ask_style_name("Новый стиль")
            if name:
                run(lambda: gui_profile.duplicate_style(BUILTIN_STYLE, name), f"Создан стиль «{name.strip()}».")

        def duplicate():
            source = chosen.get()
            name = self.ask_style_name("Дублировать стиль",
                                       gui_profile.free_style_name(source + " (копия)", state["styles"]))
            if name:
                run(lambda: gui_profile.duplicate_style(source, name), f"Создана копия «{name.strip()}».")

        def rename():
            source = chosen.get()
            name = self.ask_style_name("Переименовать стиль", source)
            if name and name.strip() != source:
                run(lambda: gui_profile.rename_style(source, name), f"Стиль переименован: «{name.strip()}».")

        def delete():
            source = chosen.get()
            if self.dialogs.askyesno("Удалить стиль", f"Удалить стиль «{source}»? Текст стиля пропадёт."):
                run(lambda: gui_profile.delete_style(source), f"Стиль «{source}» удалён.")

        def close():
            self.style_window = None
            window.destroy()
        for title, command in (("Новый", create), ("Дублировать", duplicate)):
            ttk.Button(bar, text=title, command=command).grid(row=0, column=bar.grid_size()[0], padx=(6, 0))
        rename_button = ttk.Button(bar, text="Переименовать", command=rename)
        rename_button.grid(row=0, column=bar.grid_size()[0], padx=(6, 0))
        delete_button = ttk.Button(bar, text="Удалить", command=delete)
        delete_button.grid(row=0, column=bar.grid_size()[0], padx=(6, 0))
        save_button = ttk.Button(buttons, text="Сохранить и выбрать", style="Accent.TButton", command=save)
        save_button.pack(side="left")
        ttk.Button(buttons, text="Закрыть", command=close).pack(side="left", padx=6)
        box.bind("<<ComboboxSelected>>", lambda _event: show(chosen.get()))
        window.protocol("WM_DELETE_WINDOW", close)
        editor.bind("<KeyRelease>", check)
        editor.bind("<<Edited>>", check)  # Menu paste and cut send no KeyRelease.
        show(chosen.get() if chosen.get() in styles else BUILTIN_STYLE)
        editor.focus_set()
        self.style_controls = (editor, save_button, error, check)
        self.style_dialog = {"box": box, "chosen": chosen, "show": show, "create": create,
                             "duplicate": duplicate, "rename": rename, "delete": delete,
                             "rename_button": rename_button, "delete_button": delete_button, "note": note}

    # ---- ChatGPT device login ---------------------------------------------------

    def show_device_code(self, url, code):
        import webbrowser
        tk, ttk = self.tk, self.ttk
        self.close_device_window()
        window = self.device_window = tk.Toplevel(self.root, background=CARD)
        window.title("Вход в ChatGPT")
        dark_title_bar(window)
        window.transient(self.root)
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", self.stop)
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Вход в ChatGPT", style="Heading.TLabel").pack(anchor="w")
        steps = ("1. Нажми «Открыть страницу входа».\n"
                 "2. Войди в свой аккаунт ChatGPT.\n"
                 "3. Введи на странице этот код:")
        ttk.Label(frame, text=steps, justify="left").pack(anchor="w", pady=(8, 6))
        ttk.Label(frame, text=code, font=("Consolas", 20, "bold")).pack(anchor="w", pady=(0, 10))
        buttons = ttk.Frame(frame)
        buttons.pack(anchor="w")

        def copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(code)
            copied.configure(text="Код скопирован.")
        ttk.Button(buttons, text="Открыть страницу входа", style="Accent.TButton",
                   command=lambda: webbrowser.open(url)).pack(side="left")
        ttk.Button(buttons, text="Скопировать код", command=copy).pack(side="left", padx=6)
        ttk.Button(buttons, text="Отмена", command=self.stop).pack(side="left")
        copied = ttk.Label(frame, style="Done.TLabel")
        copied.pack(anchor="w", pady=(6, 0))
        ttk.Label(frame, text="Адрес страницы: " + url, style="Hint.TLabel").pack(anchor="w")
        ttk.Label(frame, text="После входа это окно закроется само.", style="Hint.TLabel").pack(anchor="w")

    def close_device_window(self):
        window = getattr(self, "device_window", None)
        if window is not None:
            window.destroy()
            self.device_window = None

    # ---- worker events ----------------------------------------------------------

    def handle_frame(self, data):
        from gui_protocol import ProtocolError
        session = self.session
        if session is None or data["run"] != session.run or session.cancelled.is_set():
            return
        if data["type"] == "review":
            self.stop_chat_wait()
            session.state.accept(data)
            payload = data["payload"]
            if (set(payload) != {"kind", "context", "text", "options"}
                    or payload["kind"] not in {"application", "chat", "credential"}
                    or not all(isinstance(payload[k], str) for k in ("context", "text"))
                    or not isinstance(payload["options"], list)
                    or not all(isinstance(item, str) for item in payload["options"])):
                raise ProtocolError("Invalid review payload")
            self.clear_review()
            self.review_kind = payload["kind"]
            if self.review_kind == "credential":
                self.credential_label.configure(text=payload["context"] or "Введи данные для входа")
                self.credential_frame.grid()
                self.submit.configure(state="normal")
                self.credential.focus_set()
                self.say("Введи «" + (payload["context"] or "данные для входа")
                                + "» под кнопкой «Войти в HH» и нажми «Продолжить».")
                return
            self.show_work()
            self.replace_text(self.context, payload["context"])
            self.replace_text(self.letter, payload["text"])
            chat = self.review_kind == "chat"
            bare = not chat and payload["text"] == ""  # #9: an application without a letter.
            written = getattr(self, "letter_model", None)
            self.letter_label.configure(text=("Ответ в чат. Его получит работодатель." if chat else
                                              "Отклик без сопроводительного письма: работодатель увидит только резюме."
                                              if bare else "Сопроводительное письмо. Его получит работодатель.")
                                        + (f" Написала модель: {written}." if written and not bare else ""))
            self.yes.configure(text="Отправить ответ" if chat else "Отправить отклик" if bare else "Отправить письмо")
            self.decision.grid()
            self.yes.configure(state="normal")
            self.no.configure(state="normal")
            # Both an answer in a chat and a letter can be rewritten by AI or replaced by own text.
            what = "ответ" if chat else "письмо"
            self.refine_label.configure(text=f"Попроси AI переписать {what} (работодатель не увидит просьбу):")
            self.manual_label.configure(text="Или напиши ответ сам:" if chat else
                                        "Или исправь письмо сам и нажми «Взять мой текст»:")
            self.edit_hint.configure(text=f"После правки {what} снова придёт на проверку и сам не отправится.")
            if not bare:  # Nothing to rewrite when no letter is sent.
                self.chat_frame.grid()
                self.edit.configure(state="normal")
                self.manual_button.configure(state="normal")
            self.manual.configure(height=2 if chat else 5)
            if not chat:
                self.manual.insert("1.0", payload["text"])  # Fix a word without retyping the letter.
            self.option.configure(values=payload["options"])
            if payload["options"]:
                self.option.grid()
                self.option.current(0)
            else:
                self.option.grid_remove()
            note, self.review_note = getattr(self, "review_note", None), None
            self.say((note + " " if note else "") + "Прочитай текст. Нажми «" + str(self.yes["text"])
                     + "», «Пропустить» или исправь его ниже.", "warning" if note else "info")
            return
        payload = data["payload"]
        name, value = payload.get("name"), payload.get("data")
        if name == "resumes":
            self.resumes = value
            for box in (self.resume_box, self.resume_box_main):
                box.configure(values=[item["title"] for item in value])
            if value:
                self.resume_box.current(0)
                self.select_resume()
            self.append_log(f"Загружено резюме: {len(value)}")
        elif name == "models":
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ProtocolError("Invalid model list")
            import ai_provider
            if self.free_mode():
                self.append_log(f"Подключение проверено. Доступно бесплатных моделей: {len(value)}. "
                                "Модель выбирается сама для каждого письма.")
            else:
                # No choice to make: a tested default, else the first listed model.
                if value and self.model.get() not in value:
                    self.model.set(ai_provider.preferred_model(self.provider, value))
                    self.select_model()
                self.append_log("Подключение проверено. Модель выбрана автоматически: "
                                + (self.model.get() or "нет подходящей модели") + ".")
            self.refresh_steps()
        elif name == "device":
            self.show_device_code(value["url"], value["code"])
            self.say("Окно «Вход в ChatGPT»: открой страницу входа и введи код.")
        elif name == "captcha":
            self.append_log("HH запросил капчу. Введи текст с картинки в окне «Капча».")
            self.say("HH запросил капчу. Введи текст с картинки в окне «Капча».", "warning")
            attention(self.root, "warning")
        elif name == "sending":
            self.sending = value is True
            self.say("Идёт отправка в HH…" if self.sending else "Отправка завершена. Жду следующий шаг.")
        elif name == "counts":
            if (not isinstance(value, dict) or set(value) != {"attempts", "http_success", "captcha_rejections"}
                    or any(type(count) is not int or count < 0 for count in value.values())):
                raise ProtocolError("Invalid counts")
            if getattr(self, "run_counts", None) is not None:
                # Only what HH accepted counts toward the limit (#8); the rest is shown apart.
                self.run_counts.update(sent=value["http_success"], captcha=value["captcha_rejections"],
                                       unconfirmed=max(0, value["attempts"] - value["http_success"]
                                                       - value["captcha_rejections"]))
                self.update_counter()
        elif name in {"letter", "application", "preview_skipped"}:
            if session.state.pending is not None:
                raise ProtocolError("Output cannot replace a pending reviewed message")
            if name == "preview_skipped":
                value = {**value, "text": "Пропущена: " + value["reason"]}
            self.replace_text(self.context, describe_vacancy(value))
            self.replace_text(self.letter, value["text"])
            if getattr(self, "run_counts", None) is not None:
                self.run_counts["vacancies"] += 1
                self.update_counter()
            if name in {"letter", "preview_skipped"}:
                self.previews.append(value)
                self.preview_box.configure(state="readonly", values=[
                    f"{number}. {item['vacancy'].get('name') or 'Вакансия'}" + (" — пропущена" if "reason" in item else "")
                    for number, item in enumerate(self.previews, 1)])
                self.preview_box.current(len(self.previews) - 1)
        elif name == "diagnostic":
            self.append_log("Сведения о сбое сети сохранены: " + value)
        elif name == "chat_wait" and type(value) is int and 0 < value <= 3600:
            self.append_log(f"Жду следующий вопрос робота в этом чате (до {value} с).")
            self.start_chat_wait(value)
        elif name == "chat_wait_end" and value in CHAT_WAIT_ENDS:
            self.stop_chat_wait()
            self.append_log(CHAT_WAIT_ENDS[value])
            if value != "question":
                self.say(CHAT_WAIT_ENDS[value])
        elif name == "retry" and isinstance(value, str) and value in RETRY_NOTICES:
            self.append_log(RETRY_NOTICES[value])
        elif name == "fallback" and value == "chatik.hh.ru":
            self.append_log("Узел chatik.hh.ru не ответил. Подключаюсь через резервный узел HH.")
        elif (name == "letter_rejected" and isinstance(value, dict)
              and value.get("reason") in LETTER_REJECTIONS and isinstance(value.get("text"), str)
              and isinstance(value.get("vacancy"), dict)):
            title = str(value["vacancy"].get("name") or "Вакансия")
            employer = (value["vacancy"].get("employer") or {}).get("name")
            self.append_log(f"Письмо отклонено проверкой: {value['reason']}. "
                            "Вакансия пропущена, отклик не отправлен.")
            self.append_log("Вакансия: " + title + (f" — {employer}" if employer else ""))
            self.append_log("Отклонённое письмо: " + value["text"][:2000])
        elif (name == "model_switch" and isinstance(value, dict) and value.get("reason") in MODEL_SWITCHES
              and isinstance(value.get("model"), str) and len(value["model"]) <= 300):
            self.append_log(f"Модель {value['model']} пропущена: {MODEL_SWITCHES[value['reason']]}. "
                            "Пробую следующую.")
            self.model_switched = True
        elif name == "model_used" and isinstance(value, str) and len(value) <= 300:
            self.letter_model = value
            if getattr(self, "model_switched", False):
                self.append_log("Текст написала модель " + value + ".")
            self.model_switched = False
        elif name == "fallback_check" and isinstance(value, list):
            for item in value[:20]:
                if (isinstance(item, dict) and isinstance(item.get("model"), str) and len(item["model"]) <= 300
                        and isinstance(item.get("state"), str)):
                    state = item["state"]
                    text = FALLBACK_STATES.get(state) or (
                        f"провайдер ответил HTTP {state[5:]}" if state.startswith("http_") and state[5:].isdecimal()
                        else "состояние неизвестно")
                    self.append_log(f"Бесплатная модель {item['model']}: {text}.")
        elif name == "letter_revision" and value in LETTER_REVISION_FAILURES:
            self.review_note = f"Письмо не переписано: {value}. Осталось прежнее письмо."
            self.append_log(self.review_note)
        elif name == "stopped" and isinstance(value, str):
            self.stop_reason = value
            self.append_log("Причина остановки: " + value)

    def start_chat_wait(self, seconds):
        """Count down in the InfoBar while the worker polls the same chat."""
        import time
        self.stop_chat_wait()
        self.chat_wait_until = time.monotonic() + seconds
        self.tick_chat_wait()

    def tick_chat_wait(self):
        import math
        import time
        self.chat_wait_job = None
        until = getattr(self, "chat_wait_until", None)
        if until is None or self.session is None:
            return
        # Round first: (t + 60) - t can be 60.0000000001, which ceil turns into 61.
        left = max(0, math.ceil(round(until - time.monotonic(), 3)))
        self.say(f"Жду следующий вопрос робота в этом чате: осталось {left} с. "
                 "Новый вопрос придёт на проверку сам.")
        if left > 0:
            self.chat_wait_job = self.root.after(1000, self.tick_chat_wait)

    def stop_chat_wait(self):
        self.chat_wait_until = None
        job, self.chat_wait_job = getattr(self, "chat_wait_job", None), None
        if job is not None:
            self.root.after_cancel(job)

    def finish_action(self):
        """One InfoBar button after a failed run: a provider that rejected the key leads to the key (#5)."""
        import re
        if re.search(r": HTTP 40[13]; check the API key", getattr(self, "stop_reason", None) or ""):
            return ("Изменить ключ", self.edit_provider)
        return None

    def finish_message(self, data, action=None):
        from gui_steps import FAILURE_HINTS, readable_reason
        done = data["done"]
        if data.get("busy"):
            return ("Не запущено: уже идёт другой запуск HH (окно GUI или автоотклики в PowerShell). "
                    "Дождись его завершения или останови его.")
        if data["success"]:
            message = SUCCESS_TITLES.get(action, "Готово.")
            previews = getattr(self, "previews", [])
            if action == "preview" and previews and all("reason" in item for item in previews):
                message = ("Предпросмотр готов, но писем нет: все найденные вакансии пропущены. "
                           "Причины — в списке «Готовые письма». Попробуй другой запрос.")
        elif data["cancelled"] or (done is not None and done.get("cancelled")):
            message = "Остановлено. Уже отправленные отклики не отменяются."
        elif done is not None and done.get("network"):
            message = (f"Нет связи с сервером {done['network']}. Проверь интернет или VPN и повтори."
                       " Подробности — в журнале.")
        elif getattr(self, "stop_reason", None):
            # Our own guard named the cause: a login/provider hint would point the wrong way.
            # The person reads a plain sentence; the internal reason stays in the journal (#11).
            message = "Запуск остановлен. " + readable_reason(self.stop_reason)
        else:
            message = FAILURE_HINTS.get(action, "Не удалось завершить.") + " Подробности — в журнале."
        if done is not None and isinstance(done.get("counts"), dict) and any(done["counts"].values()):
            message += " " + ", ".join(f"{COUNT_TITLES.get(key, key)}: {count}"
                                       for key, count in done["counts"].items()).capitalize() + "."
        if data["forced"] or done is None or done.get("unknown") or self.sending:
            if action in {"apply", "auto", "chats"}:
                message += UNKNOWN_RESULT
        return message

    def say(self, text, level="info", action=None):
        """Show one status message in the InfoBar with its severity and, optionally, one button."""
        self.status.set(text)
        self.set_severity(level)
        button = getattr(self, "infobar_action", None)
        if button is not None:
            if action:
                button.configure(text=action[0], command=action[1])
                button.pack(side="right", padx=(10, 0))
            else:
                button.pack_forget()

    def finish_severity(self, data, action):
        done = data["done"]
        if data.get("busy"):
            return "warning"
        if action in {"apply", "auto", "chats"} and (data["forced"] or done is None or done.get("unknown")):
            return "warning"
        if data["success"]:
            return "success"
        if data["cancelled"] or (done is not None and done.get("cancelled")):
            return "info"
        return "error"

    def finish_run(self, session, data):
        from gui_steps import chatgpt_signed_in, hh_signed_in
        self.stop_chat_wait()
        self.close_device_window()
        if session.action == "hh_login":
            self.signed_hh = hh_signed_in()
        elif session.action == "chatgpt_login":
            self.signed_chatgpt = chatgpt_signed_in()
            self.load_provider()
        message = self.finish_message(data, session.action)
        self.append_log(message)
        self.sending = False
        self.session = None
        self.set_busy(False)
        if data["success"] and session.action not in REVIEW_ACTIONS:
            message += " Дальше: " + self.next_hint()
        severity = self.finish_severity(data, session.action)
        self.say(message, severity, action=None if data["success"] else self.finish_action())
        if needs_attention(severity, session.action):
            attention(self.root, severity)
        if session.action == "hh_login" and data["success"]:
            self.root.after_idle(self.auto_load_resumes)
        if session.action == "preview":
            self.show_first_letter()
        # A failure no longer swaps the work view for the journal (#11): «Подробнее» opens it.

    def poll(self):
        import queue
        from gui_protocol import ProtocolError
        if self.session is not None:
            session = self.session
            try:
                for _ in range(100):
                    name, data = session.events.get_nowait()
                    if name == "frame":
                        try:
                            self.handle_frame(data)
                        except (ProtocolError, ValueError, TypeError, KeyError):
                            self.stop()
                    elif name == "status":
                        self.say(data)
                        self.append_log(data)
                    elif name == "finished":
                        self.clear_review()
                        self.finish_run(session, data)
                        break
            except queue.Empty:
                pass
        self.root.after(100, self.poll)


if __name__ == "__main__":
    sys.exit(main())
