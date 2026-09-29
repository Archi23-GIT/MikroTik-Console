# Wspólna konsola API MikroTik

Program łączy się równolegle z wieloma routerami przez natywne RouterOS API. Utrzymuje osobne połączenie API do każdego routera, wykonuje polecenia równolegle i pokazuje odpowiedzi jako rekordy oznaczone nazwą urządzenia.

## Konfiguracja routerów

Domyślna konfiguracja programu korzysta z nieszyfrowanego API na porcie TCP 8728. Na każdym routerze włącz usługę `api` i ogranicz dostęp do adresu komputera z aplikacją, na przykład:

```routeros
/ip service set api disabled=no address=192.168.88.10/32
```

Ogranicz dostęp także regułami firewalla. Użyj konta RouterOS z uprawnieniem `api` i tylko tymi pozostałymi uprawnieniami, których wymagają wykonywane polecenia. **Nieszyfrowane API przesyła hasło i polecenia jawnym tekstem**, więc używaj go wyłącznie w zaufanej sieci lokalnej lub przez VPN. RouterOS domyślnie używa portów 8728 dla API i 8729 dla API-SSL ([dokumentacja MikroTik](https://help.mikrotik.com/docs/spaces/ROS/pages/47579160/API)).

## Hasła

W `devices.json` wspólne ustawienia znajdują się w sekcji `defaults`; wpisy w `devices` zawierają zwykle tylko nazwę i adres routera. Ustawienia konkretnego urządzenia można nadpisać w jego wpisie. Tryb hasła ustaw w `defaults`:

- `"password_mode": "shared"` pyta raz o wspólne hasło dla routerów bez własnego ustawienia.
- `"password_mode": "individual"` pyta osobno dla każdego takiego routera.
- Wpis `"password_mode": "individual"` wewnątrz konkretnego urządzenia wymusza osobne hasło dla niego, nawet gdy trybem domyślnym jest `shared`.

Hasła są pobierane przy uruchomieniu i nie są zapisywane do pliku. Zobacz [devices.example.json](devices.example.json).

## Automatyczne wykrywanie urządzeń

`mikrotik-config-builder.py` skanuje podsieć przez port API, loguje się do odpowiadających routerów i pobiera ich nazwy z `/system identity`. Do wykrycia urządzenia potrzebne jest dostępne API oraz poprawny wspólny login i hasło. Hasło jest pytane w terminalu i nie trafia do pliku.

```sh
python3 mikrotik-config-builder.py --network 10.0.138.0/24
```

Program domyślnie używa nieszyfrowanego API na porcie 8728 i zapisuje wynik do `devices.json`. Jeśli plik już istnieje, zapyta przed zastąpieniem. Można wskazać inną podsieć, port, użytkownika i plik wynikowy, np. `--network 10.0.137.0/24 --output nowa-siec.json`. Dla API-SSL użyj `--ssl --port 8729`; certyfikat jest weryfikowany domyślnie. `--insecure` wyłącza tę weryfikację i należy go używać tylko świadomie.

Nazwa urządzenia pochodzi z RouterOS Identity. Gdy kilka urządzeń ma taką samą nazwę, generator dodaje do nazwy końcówkę z ostatnim oktetem adresu IP.

## Uruchomienie

```sh
cp devices.example.json devices.json
```

Uzupełnij adresy i użytkowników w `devices.json`, po czym uruchom:

```sh
python3 mikrotik-console.py
```

## Obsługa konsoli

- Wpisz polecenie RouterOS, aby wysłać je równolegle do wybranych urządzeń, np. `/system identity print`.
- Ścieżki można wpisać ze spacjami albo ukośnikami, np. `/system package update check-for-updates` lub `/system/package/update/check-for-updates`.
- Tab podpowiada też aktualizację RouterOS: `/system package print`, sprawdzenie aktualizacji, pobranie, wybór kanału oraz instalację. `download` pokazuje zbiorczy postęp zamiast wypisywać każdy fragment pobierania. `install` pobiera i instaluje aktualizację, po czym router uruchamia się ponownie. Przed instalacją wybierz router przez `:only nazwa`, aby nie zrestartować wszystkich wybranych urządzeń naraz.
- `:list` pokazuje urządzenia i ich stan.
- `:only router-dom,router-biuro` wybiera urządzenia docelowe.
- `:all` wybiera wszystkie połączone urządzenia.
- `:reconnect` zamyka stare sesje i próbuje połączyć ponownie ze wszystkimi routerami z konfiguracji; niedostępne urządzenia można ponowić kolejnym `:reconnect`.
- `/system reboot`, `/system routerboard upgrade` oraz `/system/reset-configuration` są podpowiadane przez Tab. Reset konfiguracji kasuje ustawienia routera i może zerwać połączenie; przed poleceniem wybierz pojedyncze urządzenie przez `:only nazwa`.
- `Tab` podpowiada typowe ścieżki z głównych gałęzi IP, SYSTEM i TOOLS oraz komendy konsoli.
- Tab podpowiada również `/system routerboard upgrade` do aktualizacji RouterBOOT, `/system reboot` do restartu oraz `/system/reset-configuration` do resetu konfiguracji. Po restarcie użyj `:reconnect`, aby ponownie połączyć się bez zamykania aplikacji.
- Podpowiedzi `/tool romon print` i `/tool romon set enabled=yes` pokazują stan i włączają RoMON na wybranym urządzeniu.
- `:help` pokazuje pomoc, a `:quit` zamyka połączenia.

Konsola przekazuje komendy do API bez zamkniętej listy RouterOS akcji, więc może obsługiwać komendy dostępne w danej wersji RouterOS i jej pakietach, o ile są dostępne przez API. Polecenia można podać jako ścieżkę CLI ze spacjami albo API ze znakami `/`; po `print` można użyć API query, np. `/ip/address/print ?address=192.168.99.1/24`. Typowe warunki CLI po `where` są tłumaczone na zapytania API. Każdy rekord mieści atrybuty w jednym wierszu, zawijanym do szerokości terminala. API nie jest terminalem tekstowym: nie przekazuje ANSI-owego paska postępu ani interaktywnych pytań. Podpowiadanie Tab korzysta z listy przykładowych poleceń w programie; nie pobiera dynamicznych podpowiedzi z routera.

Tylko końcowe statusy polecenia `/system package update check-for-updates` są kolorowane w terminalu: „System is already up to date” na zielono, inne końcowe statusy na czerwono. Status pośredni „finding out latest version...” oraz odpowiedzi pozostałych komend pozostają w domyślnym kolorze terminala. Kolory są wyłączone przy przekierowaniu wyjścia oraz po ustawieniu `NO_COLOR`.
