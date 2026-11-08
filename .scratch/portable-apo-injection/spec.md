Status: implemented

# Portable APO Driver & Cross-PC Microphone Injection Reliability

## Problem Statement

Коли користувач переносить `MusicPad.exe` (разом із конфігурацією або без неї) на інший ПК, режим маршрутизації «Ін'єкція в мікрофон» не працює, хоча на основному ПК розробника все працює справно. Причини охоплюють увесь ланцюжок:
1. Збережений у `musicpad_config.json` GUID цільового мікрофона зі старого ПК (`target_mic_guid`) приймається без перевірки існування пристрою на новому ПК, а інсталятор драйвера через `RegCreateKeyExW` створює фантомну гілку реєстру для неіснуючого мікрофона і рапортує «Драйвер активний».
2. Зіставлення назви вибраного мікрофона (`find_endpoint_for_sounddevice`) не відтинає суфікс індексу `" (<індекс>)"` і не враховує повне системне ім'я пристрою (`PKEY_Device_FriendlyName`), через що при 31-символьному обрізанні MME або наявності кількох входів на одному адаптері (`Realtek(R) Audio`) обирається хибний або перший випадковий мікрофон (наприклад, `Steam Streaming Microphone`).
3. На іншому ПК з моно-мікрофоном (1 канал) або форматом `PCM int16` метод `MusicPadAPO::CheckFormat` повертає `APOERR_FORMAT_NOT_SUPPORTED` замість `S_FALSE` із рекомендованим `IEEE_FLOAT` форматом, через що `audiodg.exe` відхиляє завантаження APO.
4. На материнських платах і ноутбуках із вендорськими MFX/SFX (Realtek, Waves, Intel SST, Nahimic) сторонні шумодави стоять у графі захоплення *після* EFX і вирізають ін'єктований трек, а в `PKEY_MODE_EFX` відсутні режими `COMMUNICATIONS`, `SPEECH` та `RAW`.
5. Під час збірки через CMake (зокрема у релізному пайплайні) відсутнє статичне лінкування рантайму (`/MT`), у `requirements.txt` відсутній `deepfilternet-slim`, а `MusicPad.spec` та `--self-test` мовчки допускають створення `.exe` без `musicpad_apo.dll` та `musicpad_dsp.dll`.
6. До першого запуску потоку в `audiodg.exe` метод `get_apo_status()` підміняє нульові значення частоти й каналів на `48000` і `2`, блокуючи використання реальної частоти/каналів моно-мікрофона, а інтерфейс не попереджає користувача й не пропонує встановити драйвер при ввімкненні режиму «Ін'єкція в мікрофон».

## Solution

Забезпечити гарантовану, самодіагностовану роботу режиму «Ін'єкція в мікрофон» та Драйвера MusicPad на будь-якому ПК з Windows 10/11:
- Суворо валідувати існування й активність (`DeviceState == 1`) цільового мікрофона на поточному ПК, точно зіставляти мікрофони за повним `FriendlyName`, `endpoint_name` та `adapter_name` без впливу суфікса індексу `" (N)"` чи обрізання MME, і автоматично переприв'язувати конфіг при запуску на іншому ПК.
- Реалізувати повне узгодження форматів `S_FALSE` у `MusicPadAPO`, прописувати всі режими захоплення (`DEFAULT`, `COMMUNICATIONS`, `SPEECH`, `RAW`) у `PKEY_MODE_EFX`, тимчасово вимикати конфліктні вендорські `SFX`/`MFX`/`LFX` із повним бекапом та відновленням, і зберегти цілісність реєстрації `Steam Streaming Microphone`.
- Зробити збірку нативних DLL повністю статичною (`/MT` у CMake, `-static` у MinGW), обов'язковою у `MusicPad.spec` та `--self-test`, додати `deepfilternet-slim` у залежності й пакувати `vcruntime140.dll` для `deepfilter.dll`.
- Додати інтерактивну пропозицію встановлення/перенесення драйвера (UAC) при перемиканні в режим «Ін'єкція в мікрофон» або зміні цільового мікрофона, відкат до «Віртуальний кабель» при скасуванні першого встановлення, та чіткі сповіщення, якщо під час відтворення треку драйвер не активний або не обробляє потік у `audiodg.exe`.

## User Stories

1. As a користувач на іншому ПК, I want MusicPad автоматично ігнорувати чужий `target_mic_guid` із перенесеного `musicpad_config.json` і прив'язуватися до реального активного цільового мікрофона мого ПК, so that програма не намагається ін'єктувати звук у неіснуючий пристрій.
2. As a користувач із довгою або локалізованою назвою мікрофона та USB-префіксом (наприклад, `"Мікрофон гарнітури (2- HyperX Cloud II)"`), I want MusicPad безпомилково знаходити відповідний системний endpoint навіть при 31-символьному обрізанні MME та суфіксі `" (1)"`, so that драйвер встановлюється саме на вибраний мною мікрофон.
3. As a користувач ПК із звуковою картою Realtek (де одночасно активні `Stereo Mix`, `Line In` та `Microphone`), I want зіставлення мікрофона враховувати і назву входу (`endpoint_name`), і назву адаптера (`adapter_name`), so that драйвер не ставиться помилково на `Stereo Mix` чи `Line In`.
4. As a користувач, який встановлює Драйвер MusicPad через UAC, I want інсталятор перевіряти, що цільовий GUID реально існує і активний (`DeviceState == 1`), so that у реєстрі ніколи не створюються фантомні ключі `Capture\<GUID>\FxProperties`.
5. As a користувач, який перевіряє Стан драйвера в налаштуваннях, I want бачити статус «активний» лише тоді, коли цільовий мікрофон реально підключений і активний у системі (`DeviceState == 1`), so that програма не вводить мене в оману фантомним статусом.
6. As a користувач 1-канального (моно) мікрофона або мікрофона з нестандартною частотою дискретизації (44100 Гц) чи форматом PCM, I want Драйвер MusicPad коректно узгоджувати формат із `audiodg.exe` через повернення `S_FALSE` та підстановку `IEEE_FLOAT`, so that Windows вставляє системний конвертер форматів і завантажує драйвер без помилок.
7. As a користувач ноутбука чи материнської плати з вендорськими ефектами мікрофона (Realtek Audio, Waves MaxxAudio, Intel SST, Nahimic), I want інсталятор драйвера тимчасово вимикати вендорські `SFX`/`MFX`/`LFX` обробники на цільовому мікрофоні (зберігаючи їх у резервній копії), so that сторонні шумодави після нашої EFX не вирізають музику з мікрофона.
8. As a користувач, який видаляє Драйвер MusicPad або переносить його на інший мікрофон, I want усі раніше збережені вендорські `SFX`/`MFX`/`LFX` налаштування попереднього мікрофона повністю відновлюватися з `backup.json`, so that заводська обробка звуку повертається до початкового стану.
9. As a користувач Discord, ігор та голосових чатів, які відкривають мікрофон у режимах `COMMUNICATIONS`, `SPEECH` або `RAW`, I want Драйвер MusicPad реєструвати всі ці режими в `PKEY_MODE_EFX`, so that ін'єкція треку працює в будь-якому голосовому застосунку на Windows 10 і Windows 11.
10. As a користувач Steam, I want встановлення та видалення Драйвера MusicPad не чіпати системний CLSID `{3B97825E-5D34-4C2E-8A53-7BC22F9E014A}` (`Steam Streaming Microphone`), so that віртуальний мікрофон Steam не ламається.
11. As a користувач моно-мікрофона, який запускає трек до того, як `audiodg.exe` вперше оновив поля частоти й каналів у буфері ін'єкції, I want аудіорушій використовувати реальну частоту та кількість каналів вибраного мікрофона замість жорстких `48000 Гц / 2 канали`, so that перші секунди треку не спотворюються за темпом і каналами.
12. As a користувач на новому ПК, який вмикає перемикач «Ін'єкція в мікрофон» у налаштуваннях, коли драйвер ще не встановлений або потребує ремонту/перенесення, I want одразу отримати діалог встановлення/ремонту драйвера (UAC), so that я можу налаштувати ін'єкцію в один клік.
13. As a користувач, який скасував діалог UAC при спробі вперше увімкнути «Ін'єкція в мікрофон» (`state == "not_installed"`), I want перемикач режиму маршрутизації автоматично повернутися на «Віртуальний кабель», so that програма не залишається в неробочому стані.
14. As a користувач у режимі «Ін'єкція в мікрофон», який змінює Цільовий мікрофон у списку на інший фізичний мікрофон, I want одразу отримати пропозицію перенести Драйвер MusicPad (UAC) на новий мікрофон, so that ін'єкція одразу працює на новому мікрофоні.
15. As a користувач, який натискає Play у режимі «Ін'єкція в мікрофон», коли драйвер не активний або `audiodg.exe` не читає буфер ін'єкції (`apo_status.is_alive == False`), I want побачити чітке попередження, що звук грає лише в моніторингу (навушниках), so that я розумію, чому співрозмовники не чують трек і що потрібно зробити.
16. As a користувач, який запускає портативний `MusicPad.exe` на чистому ПК без встановленого Visual C++ Redistributable, I want всі нативні бібліотеки (`musicpad_apo.dll`, `musicpad_dsp.dll`, `deepfilter.dll`) завантажуватися без помилок відсутності `MSVCP140.dll` чи `VCRUNTIME140.dll`, so that ін'єкція та обробка голосу працюють «з коробки».
17. As a розробник або релізний пайплайн, I want `MusicPad.spec` та `--self-test` завершуватися з помилкою, якщо `musicpad_apo.dll` або `musicpad_dsp.dll` відсутні, so that жоден реліз не може вийти без вбудованого драйвера.

## Implementation Decisions

- **Модуль інсталяції та діагностики драйвера (`apo_installer`)**:
  - Розширити модель `CaptureEndpoint` полем `friendly_name` (зчитується з `PKEY_Device_FriendlyName` `{a45c254e-df1c-4efd-8020-67d146a850e0},14`).
  - Оновити `find_endpoint_for_sounddevice`: перед пошуком видаляти суфікс індексу `" (\d+)"` у кінці назви пристрою SoundDevice/WinMM; порівнювати з `friendly_name`, `display_name`, а при 31-символьному обрізанні MME перевіряти одночасне входження префіксів `endpoint_name` та `adapter_name` (без обмеження `len(adapter_lower) >= 8` та зрізу `[:12]`, який ламається на довгих локалізованих назвах).
  - Прибрати `{3B97825E-5D34-4C2E-8A53-7BC22F9E014A}` із `LEGACY_SPIKE_CLSIDS`, залишивши там лише власний тестовий CLSID `{8E65B49A-5B3E-4B3D-9E5D-2C1B8A4F7E21}`.
  - Додати перевірку існування та активності endpoint'а (`DeviceState == 1`) у `get_driver_status` та `install_driver`. Якщо `norm_guid` не є активним endpoint'ом у `HKLM\...\Capture\<GUID>` із `DeviceState == 1`, `get_driver_status` повертає `state="inactive"` («Цільовий мікрофон не знайдено в системі»), а `install_driver` не створює фантомних підключів через `RegCreateKeyExW`, а кидає зрозумілу помилку або бере активний фізичний мікрофон, якщо GUID не передано.
  - У `install_driver` записувати у `PKEY_MODE_EFX` повний перелік режимів захоплення: `DEFAULT` (`{C18E2F7E-933D-4965-B7D1-1EEF228D2AF3}`), `COMMUNICATIONS` (`{98951333-B9CD-48B1-A0A3-FF40682D73F7}`), `SPEECH` (`{FC1CF0A4-7506-4CD1-AF77-CC1670754D9E}`) та `RAW` (`{FC1CFC9B-B9D6-4CFA-B5E0-4BB2166878B2}`).
  - Тимчасово видаляти з `FxProperties` цільового мікрофона ключі `PKEY_FX_LFX_LEGACY`, `PKEY_FX_SFX_SINGLE`, `PKEY_FX_MFX_SINGLE`, `PKEY_FX_SFX_MULTI`, `PKEY_FX_MFX_MULTI`, `PKEY_MODE_SFX`, `PKEY_MODE_MFX` під час встановлення нашої EFX (оскільки всі вихідні значення `FxProperties` вже зберігаються у `backup.json` функцією `_snapshot_endpoint_if_needed` і повністю відновлюються у `_restore_single_endpoint_from_backup`).
  - Під час `install_driver` також копіювати `vcruntime140.dll` / `vcruntime140_1.dll` (якщо знайдені у бандлі або системі) у `C:\Program Files\MusicPad` та `C:\Windows\System32` (якщо там відсутні) для безвідмовного завантаження `deepfilter.dll` у `audiodg.exe`.

- **Нативний модуль APO (`musicpad_apo`) та CMake**:
  - У `MusicPadAPO::CheckFormat` реалізувати стандартну поведінку `IAudioProcessingObject`:
    - Якщо `pOppositeFormat` передано і він є валідним `IEEE_FLOAT` (32-біт float, 1..8 каналів, 8000..192000 Гц), а `pRequestedFormat` відрізняється від нього (за типом, розрядністю, частотою або кількістю каналів), встановлювати `*ppSupportedFormat = pOppositeFormat`, викликати `pOppositeFormat->AddRef()` і повертати `S_FALSE`.
    - Якщо `pOppositeFormat == NULL`, а `pRequestedFormat` є валідним `IEEE_FLOAT` — повертати `S_OK` із `*ppSupportedFormat = pRequestedFormat` (`AddRef()`).
  - У `CMakeLists.txt` встановити `CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"` для MSVC та прапорці `-static -static-libgcc -static-libstdc++` для GNU/Clang, щоб CMake-збірка не мала динамічних залежностей від MSVC CRT.

- **Модуль спільної пам'яті (`injection_shm`) та Аудіорушій (`audio_engine`)**:
  - У `InjectionShm.get_apo_status()` повертати фактичні значення `sample_rate` (`OFF_APO_SAMPLE_RATE`) та `channels` (`OFF_APO_CHANNELS`) без примусової підміни `0` на `48000` і `2`, коли `apo_hb <= 0` (тобто поки `audiodg.exe` ще жодного разу не викликав `LockForProcess` / `APOProcess`).
  - У `AudioEngine._run` у режимі `ROUTING_MODE_INJECTION`:
    - Використовувати `apo_status.sample_rate` / `apo_status.channels` лише якщо вони `> 0`, інакше брати реальні `in_rate` та `mic_channels` вибраного цільового мікрофона (а вже якщо і вони невідомі — `48000` та `2`).
    - Якщо під час активного відтворення треку з увімкненою передачею в мікрофон (`track_active_for_apo`) протягом > 1.0 с від старту треку `apo_status.is_alive` залишається `False`, один раз на трек викликати попередження (через callback/лог статусу), що Драйвер MusicPad не активний у `audiodg.exe` (потрібно встановити/відремонтувати драйвер або відкрити мікрофон у Discord/грі).

- **Головний модуль інтерфейсу (`main`) та пакування (`MusicPad.spec`, `requirements.txt`)**:
  - У `_load_saved_state` та `_resolve_selected_mic_guid` перевіряти, чи входить збережений `target_mic_guid` до переліку активних endpoint'ів `list_capture_endpoints()` поточного ПК; якщо ні — скидати його і визначати GUID за поточним вибраним мікрофоном `self.mix_mic_var`.
  - У `MusicPadApp.__init__` не затирати повідомлення про проблему драйвера викликом `self.status_var.set("")`, якщо під час старту `_refresh_driver_status` виявив проблему в режимі ін'єкції.
  - У `_on_routing_mode_changed`: якщо користувач вибрав «Ін'єкція в мікрофон», а `status.state != "active"`, одразу пропонувати встановити (при `not_installed` / `wrong_endpoint`) або відремонтувати (при `inactive` / `outdated`) драйвер через UAC. Якщо при `not_installed` користувач відмовився або UAC не вдався — автоматично повертати `routing_mode_var` на `ROUTING_MODE_VIRTUAL_CABLE`.
  - У `_mix_mic_changed` та `_input_changed`: якщо активний режим «Ін'єкція в мікрофон» і після зміни мікрофона `status.state == "wrong_endpoint"`, пропонувати користувачу перенести драйвер (UAC) на щойно вибраний мікрофон.
  - У `_play_local_track` / запуску треку: якщо активний режим «Ін'єкція в мікрофон», але `get_driver_status().state != "active"`, показувати помітне попередження (`notify_error`), що трек відтворюється лише в навушниках, бо Драйвер MusicPad не активний на вибраному мікрофоні.
  - У `MusicPad.spec` та `_run_self_test`: перевіряти наявність `musicpad_apo.dll` та `musicpad_dsp.dll` (кидати `FileNotFoundError` у `MusicPad.spec`, якщо бінарників немає, та додавати перевірку `_find_built_dll("musicpad_apo.dll")` і `_find_built_dll("musicpad_dsp.dll")` у `_run_self_test`).
  - Додати `deepfilternet-slim>=0.1.0` у `requirements.txt` та пакувати `vcruntime140.dll` / `vcruntime140_1.dll` у `MusicPad.spec`.

## Testing Decisions

- **Що робить тест хорошим**: тестуємо виключно зовнішню поведінку модулів на їхніх публічних межах (seams) — вхідні стани реєстру/пристроїв/спільної пам'яті та результуючі побічні ефекти чи повернуті структури (`DriverStatus`, `CaptureEndpoint`, `ApoRuntimeStatus`, виклики UAC, коди повернення COM-методів `IsInputFormatSupported` / `IsOutputFormatSupported`), без прив'язки до приватних допоміжних змінних.
- **Модулі та межі (Seams), які тестуються**:
  1. **Публічний інтерфейс `musicpad.apo_installer` (`list_capture_endpoints`, `find_endpoint_for_sounddevice`, `get_driver_status`, `install_driver`, `uninstall_driver`)**:
     - Зіставлення назв мікрофонів із суфіксом `" (1)"`, префіксом USB-порту `"5- "`, 31-символьним обрізанням MME на довгих українських/англійських назвах та кількома входами на одному адаптері (`Realtek(R) Audio`).
     - Відхилення неіснуючих / неактивних (`DeviceState != 1`) GUID у `get_driver_status` та `install_driver`.
     - Запис усіх 4 режимів у `PKEY_MODE_EFX`, очищення вендорських `SFX`/`MFX` при встановленні та їх повне відновлення з `backup.json` при видаленні; збереження CLSID `Steam Streaming Microphone`.
  2. **Межа `InjectionShm` + `AudioEngine` у режимі `ROUTING_MODE_INJECTION`**:
     - Повернення `sample_rate=0, channels=0` у `get_apo_status()` до першого heartbeat від APO та використання реальних `in_rate` і `mic_channels` (зокрема 1-канального моно при 44100 Гц) під час запису в `InjectionShm`.
     - Попередження користувача при спробі відтворення в режимі ін'єкції без активного APO.
  3. **Межа UI `MusicPadApp` (перемикання режиму маршрутизації, зміна мікрофона та міграція конфігу між ПК)**:
     - Автоматична заміна чужого `target_mic_guid` із `musicpad_config.json` на реальний GUID поточного ПК.
     - Пропозиція встановлення драйвера при ввімкненні «Ін'єкція в мікрофон», відкат на «Віртуальний кабель» при відмові, та пропозиція перенесення драйвера при зміні мікрофона.
  4. **Нативний COM-інтерфейс `musicpad_apo.dll` у `native/tests/test_native.cpp`**:
     - Перевірка `IsInputFormatSupported` / `IsOutputFormatSupported` на повернення `S_FALSE` та підстановку `pOppositeFormat` при незбігу каналів (1ch vs 2ch), частоти (44100 vs 48000) або формату.
- **Prior art у кодовій базі**:
  - `TestNativeDspAndShm.test_apo_installer_helpers_and_cli` та `TestInjectionRoutingAndUi.test_audio_engine_injection_mode_streams_and_shm` у `tests/test_musicpad.py`.
  - `TestProductionApoDll` у `native/tests/test_native.cpp`.

## Out of Scope

- Отримання платного EV-сертифіката та підписання драйвера через Microsoft Partner Center / WHQL (залишається модель із `DisableProtectedAudioDG=1` згідно з ADR-0002).
- Створення окремого MSI/InnoSetup інсталятора для самої програми `MusicPad.exe`.
- Підтримка ASIO або прямого Kernel Streaming в обхід системного графа Windows Audio (`audiodg.exe`).

## Further Notes

- Пов'язані архітектурні рішення: ADR-0001, ADR-0002, ADR-0003, ADR-0004, ADR-0009 та новий ADR-0018.
