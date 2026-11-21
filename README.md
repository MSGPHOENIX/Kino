# My Cinema: бэкенд (этап 1)

1. **Neon**: создай проект, скопируй строку подключения (Connection string) → это `DATABASE_URL`.
2. **GitHub**: в репозитории `my-cinema` нажми Add file → Upload files и загрузи всё содержимое этой папки (папка `app` тоже, с файлами внутри).
3. **Render**: New → Blueprint → выбери репозиторий. Когда спросит, заполни `DATABASE_URL`, `INVITE_CODE` (придумай код) и `FRONTEND_ORIGIN` (пока можно `*`). `JWT_SECRET` создастся сам.
4. Открой `https://<имя-сервиса>.onrender.com/docs`: там можно зарегистрироваться (`/auth/register`) и проверить API.
5. **cron-job.org**: добавь задачу, которая каждые 10 минут открывает `https://<имя-сервиса>.onrender.com/health`.

## Сайт (папка web)
Файл `web/index.html` это готовый сайт. Cloudflare Pages → Upload assets → загрузи его. Адрес `*.pages.dev` вставь в `FRONTEND_ORIGIN` на Render.

## Видео
`MEDIA_BASE_URL` в Render: адрес хранилища, откуда отдаются потоки (`hls_path` серии добавляется к нему).
