# Контракт файлов релиза

Постоянное имя для первой установки: `hh-applicant-tool-gui-windows-x64-setup.exe`.
После первого релиза с этой доработкой ссылка для кнопки загрузки:
https://github.com/nikchester/hh-applicant-tool-gui/releases/latest/download/hh-applicant-tool-gui-windows-x64-setup.exe

Копия побайтово совпадает с `hh-applicant-tool-gui-<версия>-windows-x64-setup.exe`.
Оба имени остаются в assets и имеют одинаковый SHA-256. `SHA256SUMS.txt` содержит
строки для обоих EXE, версионного `code.zip`, `update.json` и `update.json.sig`.
Это контроль целостности; сам EXE по-прежнему не имеет подписи Authenticode.

Существующие assets не удаляются и не переименовываются. v0.6.0, проверенный
17 ноября 2026 года, пока содержит только версионный EXE: постоянная ссылка
не заработает до отдельно разрешённого нового релиза. До этого используйте
страницу https://github.com/nikchester/hh-applicant-tool-gui/releases/latest.

Проверка скачанной копии в PowerShell:

```powershell
Get-FileHash .\hh-applicant-tool-gui-windows-x64-setup.exe -Algorithm SHA256
```

Сравните результат со строкой этого имени в `SHA256SUMS.txt` того же релиза.

## Автообновление и каналы

`update_bundle.py` по-прежнему подписывает точное версионное имя установщика,
его размер и SHA-256, а также версионный архив кода. Затем `release_assets.py`
создаёт копию и итоговые checksums, не изменяя подписанные файлы. `updater.py`
берёт имя из подписанного JSON; формат, ключ и выбор code/runtime не меняются.
При совпадении runtime используется архив кода, иначе версионный установщик.

Для `-beta.N` workflow сохраняет `--prerelease --latest=false`. Копия может
присутствовать в prerelease, но `releases/latest` и автообновление выбирают
стабильный релиз. Черновики также не являются последним стабильным релизом.

## Лицензии и notices

Для HH 2.3.7 проверено разрешение автора `s3rgeym` от 2026-11-14 на включение
пакета в установщик: [исходный вопрос и ответ](https://github.com/s3rgeym/hh-applicant-tool/issues/97#issuecomment-6883547540).
В wheel HH декларации и текста лицензии нет; разрешение не обозначается как
MIT и не распространяется автоматически на другие компоненты.

В wheel ChatMock 1.40 проверен `chatmock-1.40.dist-info/licenses/LICENSE`:
MIT, copyright 2025 Game_Time. В Windows wheel PyInstaller 6.16.0 проверен
`pyinstaller-6.16.0.dist-info/licenses/COPYING.txt`: GPL v2 or later и исключение
для bootloader. Сборщик сохраняет эти тексты вместе с metadata и CPython LICENSE.txt.

Транзитивные зависимости разрешаются при сборке и не полностью закреплены.
Поэтому окончательный список версий, деклараций и скопированных текстов берётся
из `dependency-inventory.json` и `THIRD-PARTY-NOTICES.txt` конкретной сборки.
Нельзя утверждать проверку всех транзитивных лицензий по двум прямым пинам:
перед распространением нужно сверить этот inventory и обязательства всех
фактически включённых компонентов, в том числе certifi (MPL-2.0) и Pillow
(MIT-CMU), если они вошли в сборку.

## Проверки и ограничения

Локально: `python windows/check_release_assets.py` и
`python windows/check_packaging.py`. Новые проверки используют синтетические
файлы, сверяют каждый checksum и неизменность подписанных входов.

Windows 10/11 x64, установка для текущего пользователя, AppId, каталог установки,
ярлыки и сохранение профиля определяются прежним `installer.iss` и не меняются.
Ручная установка, GUI, удаление и обновление на Windows пока не проверены.
У `dev` и `main` есть отдельные расхождения версий/launcher; этот PR их не сливает.
