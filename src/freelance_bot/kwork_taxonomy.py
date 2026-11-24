"""Таксономия категорий Kwork, сгенерированная с живых публичных страниц.

Не редактируйте вручную: запустите ``python tools/build_taxonomy.py`` после
повторного сохранения ``fixtures/probes/kwork_categories.json`` и
``fixtures/probes/kwork_category_map.json``.

Структура повторяет меню Kwork: 7 верхних категорий, в каждой — группы
подкатегорий. Ключи — стабильные слаги (``design``, ``programming``,
...) — используются в ``config.yaml`` (``filters.categories``) и в CLI
(``--category design``).
"""

from __future__ import annotations

# key -> {name, kwork_id, url, groups: {group name: [(subcategory, url), ...]}}
TAXONOMY: dict[str, dict[str, object]] = {
    "design": {
        "name": "Дизайн",
        "kwork_slug": "design",
        "kwork_id": 15,
        "url": "https://kwork.ru/categories/design",
        "groups": {
            "Логотип и брендинг": [
                ("Логотипы", "https://kwork.ru/categories/logo/logotipy"),
                ("Фирменный стиль", "https://kwork.ru/categories/logo/firmenniy-stil"),
                ("Визитки", "https://kwork.ru/categories/logo/vizitki"),
                (
                    "Брендирование и сувенирка",
                    "https://kwork.ru/categories/logo/brendirovanie-i-suvenirka",
                ),
            ],
            "Презентации и инфографика": [
                (
                    "Презентации",
                    "https://kwork.ru/categories/presentations-infographics/prezentatsii",
                ),
                (
                    "Инфографика",
                    "https://kwork.ru/categories/presentations-infographics/infografika",
                ),
                (
                    "Карта и схема",
                    "https://kwork.ru/categories/presentations-infographics/karta-skhema",
                ),
            ],
            "Арт и иллюстрации": [
                (
                    "Портрет, шарж, карикатура",
                    "https://kwork.ru/categories/illustrations/portret-sharzh-karikatura",
                ),
                (
                    "Иллюстрации и рисунки",
                    "https://kwork.ru/categories/illustrations/illyustratsii-i-risunki",
                ),
                ("Дизайн игр", "https://kwork.ru/categories/illustrations/dizayn-igr"),
                ("Тату, принты", "https://kwork.ru/categories/illustrations/tatu-dizayn"),
                ("Стикеры", "https://kwork.ru/categories/illustrations/stikery"),
                ("NFT арт", "https://kwork.ru/categories/illustrations/nft-art"),
                (
                    "Готовые шаблоны и рисунки",
                    "https://kwork.ru/categories/illustrations/gotovie-shablony-i-risunki",
                ),
            ],
            "Веб и мобильный дизайн": [
                ("Веб-дизайн", "https://kwork.ru/categories/web-plus-mobile-design/veb-dizayn"),
                (
                    "Мобильный дизайн",
                    "https://kwork.ru/categories/web-plus-mobile-design/mobilniy-dizayn",
                ),
                ("Email-дизайн", "https://kwork.ru/categories/web-plus-mobile-design/email-dizayn"),
                (
                    "Баннеры и иконки",
                    "https://kwork.ru/categories/web-plus-mobile-design/bannery-i-ikonki",
                ),
                (
                    "Юзабилити-аудит",
                    "https://kwork.ru/categories/usability-testing/yuzabiliti-audit",
                ),
            ],
            "Маркетплейсы и соцсети": [
                (
                    "Дизайн в соцсетях",
                    "https://kwork.ru/categories/e-commerce-social-network/dizayn-v-sotssetyakh",
                ),
                (
                    "Дизайн для маркетплейсов",
                    "https://kwork.ru/categories/e-commerce-social-network/dizayn-dlya-marketpleysov",
                ),
            ],
            "Интерьер и экстерьер": [
                ("Интерьер", "https://kwork.ru/categories/interior-exterior-design/interer"),
                (
                    "Дизайн домов и сооружений",
                    "https://kwork.ru/categories/interior-exterior-design/dizayn-zdaniy-i-sooruzheniy",
                ),
                (
                    "Ландшафтный дизайн",
                    "https://kwork.ru/categories/interior-exterior-design/landshaftniy-dizayn",
                ),
                (
                    "Дизайн мебели",
                    "https://kwork.ru/categories/interior-exterior-design/dizayn-mebeli",
                ),
            ],
            "Обработка и редактирование": [
                (
                    "Отрисовка в векторе",
                    "https://kwork.ru/categories/vector-tracing/otrisovka-v-vektore",
                ),
                (
                    "Фотомонтаж и обработка",
                    "https://kwork.ru/categories/vector-tracing/fotomontazh-i-obrabotka",
                ),
                ("3D-графика", "https://kwork.ru/categories/vector-tracing/3d-grafika"),
            ],
            "Полиграфия": [
                ("Брошюра и буклет", "https://kwork.ru/categories/graphic-design/broshyura-buklet"),
                ("Листовки и флаер", "https://kwork.ru/categories/graphic-design/listovki-flaer"),
                (
                    "Плакат и афиша",
                    "https://kwork.ru/categories/graphic-design/plakat-afisha-poster",
                ),
                (
                    "Календарь и открытка",
                    "https://kwork.ru/categories/graphic-design/kalendar-otkrytka",
                ),
                (
                    "Каталог, меню, книга",
                    "https://kwork.ru/categories/graphic-design/katalog-menyu-zhurnal",
                ),
                (
                    "Грамота и сертификат",
                    "https://kwork.ru/categories/graphic-design/gramota-sertifikat",
                ),
                ("Гайд и чек-лист", "https://kwork.ru/categories/graphic-design/gayd-i-chek-list"),
            ],
            "Промышленный дизайн": [
                (
                    "Электроника и устройства",
                    "https://kwork.ru/categories/packaging/elektronika-i-ustroystva",
                ),
                (
                    "Предметы и аксессуары",
                    "https://kwork.ru/categories/packaging/predmety-i-aksessuary",
                ),
                (
                    "Упаковка и этикетка",
                    "https://kwork.ru/categories/packaging/upakovka-i-etiketka",
                ),
            ],
            "Наружная реклама": [
                (
                    "Билборды и стенды",
                    "https://kwork.ru/categories/outdoor-advertising/bilbordy-i-stendy",
                ),
                (
                    "Витрины и вывески",
                    "https://kwork.ru/categories/outdoor-advertising/vitriny-i-vyveski",
                ),
            ],
            "ИИ-генерация изображений": [
                ("Нейрофотосессия", "https://kwork.ru/categories/imagegeneration/neyrofotosessiya"),
                (
                    "ИИ-аватары и портреты",
                    "https://kwork.ru/categories/imagegeneration/ii-avatary-i-portrety",
                ),
                (
                    "ИИ-иллюстрации и концепт-арт",
                    "https://kwork.ru/categories/imagegeneration/ii-illyustratsii-i-kontsept-art",
                ),
                (
                    "ИИ-логотипы и инфографика",
                    "https://kwork.ru/categories/imagegeneration/ii-logotipy-i-infografika",
                ),
            ],
        },
    },
    "programming": {
        "name": "Разработка и IT",
        "kwork_slug": "programming",
        "kwork_id": 11,
        "url": "https://kwork.ru/categories/programming",
        "groups": {
            "Доработка и настройка сайта": [
                ("Доработка сайта", "https://kwork.ru/categories/website-repair/dorabotka-sayta"),
                ("Настройка сайта", "https://kwork.ru/categories/website-repair/nastroyka-sayta"),
                (
                    "Защита и лечение сайта",
                    "https://kwork.ru/categories/website-repair/zashchita-i-lechenie-sayta",
                ),
                ("Ускорение сайта", "https://kwork.ru/categories/website-repair/uskorenie-sayta"),
                (
                    "Плагины и темы",
                    "https://kwork.ru/categories/website-repair/plaginy-moduli-i-temy",
                ),
                (
                    "Исправление ошибок",
                    "https://kwork.ru/categories/website-repair/ispravlenie-oshibok",
                ),
            ],
            "Создание сайтов": [
                ("Новый сайт", "https://kwork.ru/categories/website-development/noviy-sayt"),
                (
                    "Копия сайта",
                    "https://kwork.ru/categories/website-development/kopiya-sushchestvuyushchego",
                ),
            ],
            "Верстка": [
                (
                    "Верстка по макету",
                    "https://kwork.ru/categories/frontend/verstka-po-dizayn-maketu",
                ),
                (
                    "Доработка и адаптация верстки",
                    "https://kwork.ru/categories/frontend/dorabotka-verstki",
                ),
            ],
            "Десктоп программирование": [
                ("Макросы для Office", "https://kwork.ru/categories/software/office"),
                ("1С", "https://kwork.ru/categories/software/1s"),
                ("Готовые программы", "https://kwork.ru/categories/software/drugie-gotovie"),
                ("Программы на заказ", "https://kwork.ru/categories/software/drugie-na-zakaz"),
            ],
            "Скрипты, боты и mini apps": [
                ("Скрипты", "https://kwork.ru/categories/script-programming/skripty"),
                ("Парсеры", "https://kwork.ru/categories/script-programming/parsery"),
                ("Чат-боты", "https://kwork.ru/categories/script-programming/chat-boty"),
                (
                    "Telegram Mini Apps",
                    "https://kwork.ru/categories/script-programming/telegram-mini-apps",
                ),
                ("ИИ-агенты", "https://kwork.ru/categories/script-programming/ii-agenty"),
                ("ИИ-боты", "https://kwork.ru/categories/script-programming/ii-boty"),
                (
                    "Машинное обучение",
                    "https://kwork.ru/categories/script-programming/mashinnoe-obuchenie",
                ),
                (
                    "Интернет вещей (IoT)",
                    "https://kwork.ru/categories/script-programming/internet-veshchey-iot",
                ),
            ],
            "Мобильные приложения": [
                ("iOS", "https://kwork.ru/categories/mobile-apps/ios"),
                ("Android", "https://kwork.ru/categories/mobile-apps/android"),
            ],
            "Игры": [
                ("Разработка игр", "https://kwork.ru/categories/game-dev/razrabotka-igr"),
                ("Игровой сервер", "https://kwork.ru/categories/game-dev/igrovoy-server"),
                ("Готовые игры", "https://kwork.ru/categories/game-dev/gotovie-igry"),
            ],
            "Сервера и хостинг": [
                (
                    "Администрирование сервера",
                    "https://kwork.ru/categories/server-administration/administrirovanie-servera",
                ),
                ("Домены", "https://kwork.ru/categories/server-administration/domeny"),
                ("Хостинг", "https://kwork.ru/categories/server-administration/khosting"),
            ],
            "Юзабилити, тесты и помощь": [
                (
                    "Юзабилити-аудит",
                    "https://kwork.ru/categories/usability-testing/yuzabiliti-audit",
                ),
                (
                    "Тестирование на ошибки",
                    "https://kwork.ru/categories/usability-testing/testirovanie-na-oshibki",
                ),
                (
                    "Компьютерная и IT помощь",
                    "https://kwork.ru/categories/usability-testing/kompyuternaya-i-it-pomoshch",
                ),
            ],
        },
    },
    "writing_translations": {
        "name": "Тексты и переводы",
        "kwork_slug": "writing-translations",
        "kwork_id": 5,
        "url": "https://kwork.ru/categories/writing-translations",
        "groups": {
            "Тексты и наполнение сайта": [
                ("Статьи", "https://kwork.ru/categories/creative-writing/stati"),
                ("SEO-тексты", "https://kwork.ru/categories/creative-writing/seo-teksty"),
                (
                    "Карточки товаров",
                    "https://kwork.ru/categories/creative-writing/kartochki-tovarov",
                ),
                ("Комментарии", "https://kwork.ru/categories/creative-writing/kommentarii"),
                (
                    "Художественные тексты",
                    "https://kwork.ru/categories/creative-writing/khudozhestvennie-teksty",
                ),
                ("Сценарии", "https://kwork.ru/categories/creative-writing/stsenarii"),
                ("Корректура", "https://kwork.ru/categories/creative-writing/korrektura"),
            ],
            "Переводы": [
                ("Переводы, 74 языка", "https://kwork.ru/categories/translations"),
            ],
            "Набор текста": [
                ("С аудио/видео", "https://kwork.ru/categories/typing/s-audio-video"),
                ("С изображений", "https://kwork.ru/categories/typing/s-izobrazheniy"),
            ],
            "Продающие и бизнес тексты": [
                (
                    "Продающие тексты",
                    "https://kwork.ru/categories/business-copywriting/prodayushchie-teksty",
                ),
                (
                    "Реклама и email",
                    "https://kwork.ru/categories/business-copywriting/reklama-i-email",
                ),
                (
                    "Скрипты продаж и выступлений",
                    "https://kwork.ru/categories/business-copywriting/skripty-prodazh-i-vystupleniya",
                ),
                (
                    "Коммерческие предложения",
                    "https://kwork.ru/categories/business-copywriting/kommercheskie-predlozheniya",
                ),
                (
                    "Посты для соцсетей",
                    "https://kwork.ru/categories/business-copywriting/posty-dlya-sotssetey",
                ),
                (
                    "Нейминг и слоганы",
                    "https://kwork.ru/categories/personal-assistant/menedzhment-proekta-neyming-i-slogany",
                ),
            ],
            "Резюме и вакансии": [
                ("Составление резюме", "https://kwork.ru/categories/resumes-and-letters/rezyume"),
                (
                    "Сопроводительные письма",
                    "https://kwork.ru/categories/resumes-and-letters/soprovoditelnie-pisma",
                ),
                ("Текст вакансии", "https://kwork.ru/categories/resumes-and-letters/vakansii"),
            ],
            "ИИ-тексты": [
                (
                    "ИИ-генерация статей",
                    "https://kwork.ru/categories/textgeneration/ii-generatsiya-statey",
                ),
                (
                    "ИИ-обработка текстов",
                    "https://kwork.ru/categories/textgeneration/ii-obrabotka-tekstov",
                ),
            ],
        },
    },
    "seo": {
        "name": "SEO и трафик",
        "kwork_slug": "seo",
        "kwork_id": 17,
        "url": "https://kwork.ru/categories/seo",
        "groups": {
            "SEO": [
                ("Ссылки", "https://kwork.ru/categories/links"),
                ("Продвижение сайта в топ", "https://kwork.ru/categories/integrated-promotion"),
                ("Внутренняя оптимизация", "https://kwork.ru/categories/optimization"),
                ("Семантическое ядро", "https://kwork.ru/categories/keywords"),
                ("SEO аудиты, консультации", "https://kwork.ru/categories/audit"),
            ],
            "Трафик": [
                ("Посетители на сайт", "https://kwork.ru/categories/traffic/posetiteli-na-sayt"),
                (
                    "Поведенческие факторы",
                    "https://kwork.ru/categories/traffic/povedencheskie-faktory",
                ),
            ],
            "Статистика и аналитика": [
                (
                    "Метрики и счетчики",
                    "https://kwork.ru/categories/analytics/metriki-i-schetchiki",
                ),
                (
                    "Анализ сайтов, рынка",
                    "https://kwork.ru/categories/analytics/marketingoviy-analiz",
                ),
            ],
        },
    },
    "promotion": {
        "name": "Соцсети и маркетинг",
        "kwork_slug": "promotion",
        "kwork_id": 45,
        "url": "https://kwork.ru/categories/promotion",
        "groups": {
            "Соцсети и SMM": [
                ("Youtube", "https://kwork.ru/categories/smm/youtube"),
                ("ВКонтакте", "https://kwork.ru/categories/smm/vkontakte"),
                ("Telegram", "https://kwork.ru/categories/smm/telegram"),
                ("MAX", "https://kwork.ru/categories/smm/max"),
                ("Одноклассники", "https://kwork.ru/categories/smm/odnoklassniki"),
                ("Дзен", "https://kwork.ru/categories/smm/yandeks-dzen"),
                ("TikTok", "https://kwork.ru/categories/smm/tiktok"),
                ("VC.ru", "https://kwork.ru/categories/smm/vc-ru"),
                ("Reddit", "https://kwork.ru/categories/smm/reddit"),
                ("Другие", "https://kwork.ru/categories/smm/drugie"),
            ],
            "Контекстная реклама": [
                ("Google Ads", "https://kwork.ru/categories/context/google"),
                ("Яндекс Директ", "https://kwork.ru/categories/context/yandeks"),
            ],
            "Базы данных и клиентов": [
                ("Сбор данных", "https://kwork.ru/categories/information-bases/sbor-dannykh"),
                ("Готовые базы", "https://kwork.ru/categories/information-bases/gotovie-bazy"),
                (
                    "Проверка, чистка базы",
                    "https://kwork.ru/categories/information-bases/proverka-chistka-bazy",
                ),
            ],
            "E-mail рассылки": [
                ("E-mail маркетинг и рассылки", "https://kwork.ru/categories/email-marketing"),
            ],
            "Маркетплейсы и доски объявлений": [
                (
                    "Маркетплейсы",
                    "https://kwork.ru/categories/bulletin-boards/torgovie-ploshchadki",
                ),
                (
                    "Доски объявлений",
                    "https://kwork.ru/categories/bulletin-boards/doski-obyavlenii",
                ),
                (
                    "Справочники и каталоги",
                    "https://kwork.ru/categories/bulletin-boards/spravochniki-katalogi",
                ),
            ],
            "Маркетинг и PR": [
                ("Контент-маркетинг", "https://kwork.ru/categories/marketing/pr"),
                ("Продвижение музыки", "https://kwork.ru/categories/marketing/prodvizhenie-muzyki"),
            ],
        },
    },
    "audio_video": {
        "name": "Аудио, видео, съемка",
        "kwork_slug": "audio-video",
        "kwork_id": 7,
        "url": "https://kwork.ru/categories/audio-video",
        "groups": {
            "Аудиозапись и озвучка": [
                ("Озвучка и дикторы", "https://kwork.ru/categories/audio/ozvuchka"),
                ("Аудиоролик", "https://kwork.ru/categories/audio/audiorolik"),
            ],
            "Музыка и песни": [
                (
                    "Песня (музыка + текст + вокал)",
                    "https://kwork.ru/categories/music/pesnya-muzyka-tekst-vokal",
                ),
                ("Написание музыки", "https://kwork.ru/categories/music/napisanie-muzyki"),
                ("Тексты песен", "https://kwork.ru/categories/music/napisanie-teksta-pesen"),
                ("Запись вокала", "https://kwork.ru/categories/music/zapis-vokala"),
                ("Аранжировка", "https://kwork.ru/categories/music/aranzhirovka"),
            ],
            "Редактирование аудио": [
                ("Обработка звука", "https://kwork.ru/categories/editing-audio/redaktirovanie"),
                (
                    "Выделение звука из видео",
                    "https://kwork.ru/categories/editing-audio/konvertatsiya",
                ),
            ],
            "Интро и анимация логотипа": [
                ("Анимация логотипа", "https://kwork.ru/categories/intro/animatsiya-logotipa"),
                ("Интро и заставки", "https://kwork.ru/categories/intro/intro-i-zastavki"),
                ("GIF-анимация", "https://kwork.ru/categories/intro/gif-animatsiya"),
            ],
            "Видеоролики": [
                ("Дудл-видео", "https://kwork.ru/categories/animation/dudl-video"),
                ("Анимационный ролик", "https://kwork.ru/categories/animation/animatsionniy-rolik"),
                ("Проморолик", "https://kwork.ru/categories/animation/promo-rolik"),
                (
                    "Ролики для соцсетей",
                    "https://kwork.ru/categories/animation/roliki-dlya-sotssetey",
                ),
                ("Слайд-шоу", "https://kwork.ru/categories/animation/slayd-shou"),
                (
                    "Скринкасты и видеообзоры",
                    "https://kwork.ru/categories/animation/skrinkasty-i-videoobzory",
                ),
                (
                    "Кинетическая типографика",
                    "https://kwork.ru/categories/animation/kineticheskaya-tipografika",
                ),
                ("Видео с ведущим", "https://kwork.ru/categories/animation/video-s-vedushchim"),
                ("Видеопрезентация", "https://kwork.ru/categories/animation/videoprezentatsiya"),
                ("Видеосъемка и монтаж", "https://kwork.ru/categories/editing-media"),
                ("Сценарии роликов", "https://kwork.ru/categories/creative-writing/dlya-rolika"),
            ],
            "ИИ-генерация видео": [
                ("ИИ-генерация видео", "https://kwork.ru/categories/videogeneration"),
            ],
        },
    },
    "business": {
        "name": "Бизнес и жизнь",
        "kwork_slug": "business",
        "kwork_id": 83,
        "url": "https://kwork.ru/categories/business",
        "groups": {
            "Персональный помощник": [
                (
                    "Работа в MS Office",
                    "https://kwork.ru/categories/personal-assistant/rabota-v-ms-office",
                ),
                (
                    "Поиск информации",
                    "https://kwork.ru/categories/personal-assistant/poisk-informatsii",
                ),
                (
                    "Анализ информации",
                    "https://kwork.ru/categories/personal-assistant/analiz-informatsii",
                ),
                (
                    "Любая интеллектуальная работа",
                    "https://kwork.ru/categories/personal-assistant/lyubaya-intellektualnaya-rabota",
                ),
                (
                    "Любая рутинная работа",
                    "https://kwork.ru/categories/personal-assistant/lyubaya-rutinnaya-rabota",
                ),
                (
                    "Менеджмент проектов",
                    "https://kwork.ru/categories/personal-assistant/menedzhment-proekta",
                ),
            ],
            "Бухгалтерия и налоги": [
                ("Для физлиц", "https://kwork.ru/categories/financial-consulting/dlya-fiz-lits"),
                (
                    "Для юрлиц и ИП",
                    "https://kwork.ru/categories/financial-consulting/dlya-yur-lits-i-ip",
                ),
            ],
            "Обзвоны и продажи": [
                (
                    "Продажи по телефону",
                    "https://kwork.ru/categories/calls-sales/prodazhi-po-telefonu",
                ),
                ("Телефонный опрос", "https://kwork.ru/categories/calls-sales/telefonniy-opros"),
            ],
            "Юридическая помощь": [
                (
                    "Договор и доверенность",
                    "https://kwork.ru/categories/lawyer-consulting/dogovor-doverennost",
                ),
                (
                    "Судебный документ",
                    "https://kwork.ru/categories/lawyer-consulting/sudebniy-dokument",
                ),
                (
                    "Ведение ООО и ИП",
                    "https://kwork.ru/categories/lawyer-consulting/vedenie-ooo-i-ip",
                ),
                (
                    "Юридическая консультация",
                    "https://kwork.ru/categories/lawyer-consulting/yuridicheskaya-konsultatsiya",
                ),
                ("Интернет-право", "https://kwork.ru/categories/lawyer-consulting/internet-pravo"),
            ],
            "Продажа сайтов и групп": [
                ("Сайт с доменом", "https://kwork.ru/categories/sites-for-sale/sayt-s-domenom"),
                ("Сайт без домена", "https://kwork.ru/categories/sites-for-sale/sayt-bez-domena"),
                (
                    "Приложения",
                    "https://kwork.ru/categories/sites-for-sale/domen-prilozhenie-i-inoe",
                ),
                (
                    "Группы и каналы соцсетей",
                    "https://kwork.ru/categories/sites-for-sale/gruppy-i-kanaly-sotssetey",
                ),
            ],
            "Подбор персонала": [
                ("Подбор резюме", "https://kwork.ru/categories/recruitment/podbor-rezyume"),
                ("Найм специалиста", "https://kwork.ru/categories/recruitment/naym-spetsialista"),
            ],
            "Презентация": [
                (
                    "Оформление",
                    "https://kwork.ru/categories/presentations-infographics/prezentatsii",
                ),
                (
                    "Текст и сценарий",
                    "https://kwork.ru/categories/business-copywriting/skripty-prodazh-i-vystupleniya-tekst-prezentatsii-vystupleniya",
                ),
            ],
            "Обучение и консалтинг": [
                ("Обучение и консалтинг", "https://kwork.ru/categories/training-consulting"),
            ],
            "Стройка и ремонт": [
                (
                    "Проектирование объекта",
                    "https://kwork.ru/categories/engineering/proektirovanie-i-obustroystvo",
                ),
                ("Строительство", "https://kwork.ru/categories/engineering/stroitelstvo"),
                (
                    "Дизайн домов и сооружений",
                    "https://kwork.ru/categories/interior-exterior-design/dizayn-zdaniy-i-sooruzheniy",
                ),
                ("Интерьер", "https://kwork.ru/categories/interior-exterior-design/interer"),
                (
                    "Ландшафтный дизайн",
                    "https://kwork.ru/categories/interior-exterior-design/landshaftniy-dizayn",
                ),
            ],
        },
    },
}
