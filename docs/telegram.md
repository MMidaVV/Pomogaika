# Telegram для показа

Premium нужен, чтобы бот отвечал **из вашего чата**. Юзерботов нет — только Bot API.

Ошибка **This bot doesn't support secretary mode yet** значит: боту ещё не разрешили быть «секретарём». Это включается в BotFather, не в настройках Premium.

## 1. Бот

1. В Telegram откройте [@BotFather](https://t.me/BotFather)
2. `/newbot` → имя, например `KaVa Pomogaika`, username на латинице
3. Скопируйте токен в `.env`:

```
TELEGRAM_BOT_TOKEN=123:ABC
```

4. Обязательно для ответа из вашего чата:

```
/mybots → ваш бот → Bot Settings → Business Mode
(или Secretary Mode / Режим секретаря)
→ Turn on / Включить
```

Пока это выключено, Telegram пишет `doesn't support secretary mode yet`. Проверка: у бота в API должно стать `can_connect_to_business: true`.

Пилот: **@kava_pomogaika_bot** — Secretary Mode уже включён. В `.env` должен быть токен именно этого бота.

5. `/setprivacy` → **Disable** (для лички не обязательно)
6. `/setjoingroups` можно Disable

Свой числовой id (не обязательно): [@userinfobot](https://t.me/userinfobot) и в `.env`:

```
TELEGRAM_OWNER_USER_ID=123456789
TOKENROUTER_API_KEY=
TOKENROUTER_MODEL=z-ai/glm-5.3-flash
```

Если ключ TokenRouter есть — бот понимает свободные формулировки («куда машину деть»). Без ключа остаётся FAQ. После правки `.env` перезапустите `.\run.ps1`.

## 2. Запуск

```powershell
cd $HOME\OneDrive\Desktop\Pomogaika
.\run.ps1
```

Напишите боту **/start** с аккаунта Галины — это пульт собственника.

С другого аккаунта напишите боту «Где парковка?» — вам придёт черновик с кнопками. Так можно показывать даже без Business.

## 3. Premium / Business — ответ от вашего имени

Сначала шаг 4 из раздела 1 (Business Mode в BotFather). Потом:

1. Телефон → Настройки → Telegram Business (или «Бизнес»)
2. Чат-боты → вставить username бота
3. Включить чтение сообщений и ответы

После этого гость пишет **вам в личку**, бот готовит черновик, «Отправить» уходит от вашего имени.

## 4. Мини-приложение собственника

На компьютере календарь: [http://127.0.0.1:8000/app](http://127.0.0.1:8000/app)

В боте: **/app**. Сетка месяца, занятость по площадкам, очередь черновиков. Закрытие дат только в нашем календаре, на Авито/Суточно не уходит.

Чтобы кнопка открывалась **внутри Telegram на телефоне**, нужна публичная **https**-ссылка на тот же `/app` (туннель или хостинг). Тогда в `.env`:

```
TELEGRAM_WEBAPP_URL=https://пример.ru/app
```

`http://127.0.0.1` Telegram как Mini App не принимает. После правки `.env` перезапустите `.\run.ps1`.
