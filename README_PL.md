# Wspólna konsola API MikroTik

[English](README.md) | **Polski**

**Aktualna wersja: 1.5.1** (wartość z pliku `VERSION`).

Program łączy się równolegle z wieloma routerami przez natywne RouterOS API. Utrzymuje osobne połączenie API do każdego routera, wykonuje polecenia równolegle i pokazuje odpowiedzi jako rekordy oznaczone nazwą urządzenia.

Każdy MikroTik musi mieć skonfigurowany i osiągalny adres IP — IPv4 albo IPv6. RouterOS API działa przez TCP/IP, dlatego urządzeń bez adresu IP nie można obsłużyć tą aplikacją.

## Przykładowy ekran

![Przykład działania konsoli MikroTik API](docs/screenshots/mikrotik-console-example.png)

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

## Język komunikatów

Oba skrypty automatycznie wybierają język komunikatów, pytań, błędów i pomocy. Polski jest używany, gdy wykryty język systemu to polski; w pozostałych przypadkach, również przy nieudanym wykrywaniu, używany jest angielski. macOS: pierwszy język z `AppleLanguages`; Windows: język interfejsu użytkownika; Linux: ustawienia `LC_ALL`, `LANGUAGE`, `LC_MESSAGES` i `LANG`, w tej kolejności. Wspólny moduł `mikrotik_language.py` musi znajdować się obok skryptów. Odpowiedzi RouterOS i komunikaty błędów pochodzące z systemu są wyświetlane w oryginale.

Możesz wymusić język w obu programach przez `--lang pl` albo `--lang en`. Opcja ma pierwszeństwo przed ustawieniami systemu i zmienia również język pomocy `--help`. Bez `--lang` język jest wykrywany automatycznie.

```sh
python3 mikrotik-config-builder.py --ipv6 --lang en
python3 mikrotik-console.py --lang pl
```

## Automatyczne wykrywanie urządzeń

`mikrotik-config-builder.py` skanuje podsieć przez port API, loguje się do odpowiadających routerów i pobiera ich nazwy z `/system identity`. Do wykrycia urządzenia potrzebne jest dostępne API oraz poprawny wspólny login i hasło. Hasło jest pytane w terminalu i nie trafia do pliku.

```sh
python3 mikrotik-config-builder.py --network 10.0.138.0/24
```

Program domyślnie używa nieszyfrowanego API na porcie 8728 i zapisuje wynik do `devices.json`. Jeśli plik już istnieje, zapyta przed zastąpieniem. Można wskazać inną podsieć, port, użytkownika i plik wynikowy, np. `--network 10.0.137.0/24 --output nowa-siec.json`. Dla API-SSL użyj `--ssl --port 8729`; certyfikat jest weryfikowany domyślnie. `--insecure` wyłącza tę weryfikację i należy go używać tylko świadomie.

W trybie `--ipv6` program wysyła zapytania MNDP UDP/5678 przez IPv4 broadcast (ogólny i skierowany na każdy lokalny interfejs) oraz IPv6 multicast. Czeka 35 sekund, uwzględniając domyślny, 30-sekundowy cykl ogłoszeń RouterOS. Z ogłoszeń pobiera adresy IPv6, a adresy IPv4 wykorzystuje dodatkowo jako tymczasowy punkt połączenia do odczytania `/ip/neighbor/print`. Następnie próbuje po IPv6 dotrzeć do sąsiadów widocznych routerom, także gdy komputer nie odebrał ich ogłoszeń bezpośrednio. Tymczasowe adresy IPv4 nie trafiają do `devices.json`; wynik zawiera adresy IPv6.

Wykrywanie korzysta z MNDP i tablic sąsiadów RouterOS, a nie ogólnej tablicy sąsiadów macOS, która może zawierać inne urządzenia i interfejsy systemowe. Nie wymaga podawania podsieci. Po wykryciu program próbuje zalogować się do API; hasło musi być prawidłowe, a usługa API dostępna. Jeśli część adresów nie odpowie, builder pokaże osobny błąd dla każdego hosta.

Adresy link-local `fe80::` działają tylko w segmencie, w którym zostały wykryte. Program zapisuje w `host` również identyfikator interfejsu komputera, np. `fe80::2ec8:1bff:fe05:2a4e%en0`; konsola używa go przy połączeniu przez IPv6. Wykrywanie obejmuje lokalne łącza komputera i nie przechodzi przez routery do innych VLAN-ów ani podsieci. Adresy z tabel sąsiadów są używane tylko pochodząc z odpowiedzi routerów MikroTik.

```sh
python3 mikrotik-config-builder.py --ipv6
```

Można też podać ograniczony, osiągalny prefiks IPv6 przez `--network`, aby sprawdzić go po kolei:

```sh
python3 mikrotik-config-builder.py --ipv6 --network fd12:3456:789a::/120 --max-hosts 256
```

Skaner respektuje limit `--max-hosts` (domyślnie 4096). Nie skanuj całego typowego prefiksu `/64`; ma zbyt wiele adresów. Router musi mieć włączone API, a reguły `/ip service` i IPv6 firewall muszą dopuszczać połączenie z komputera uruchamiającego skaner.

Program od razu wyświetla komunikat przygotowania, a przed wykrywaniem MNDP informuje o oczekiwaniu na ogłoszenia.

Nazwa urządzenia pochodzi z RouterOS Identity. Dla każdej nazwy generator zapisuje tylko jeden działający adres: pierwszy w kolejności adresów IP. Pozostałe adresy tej samej nazwy są pomijane, bez dodawania fragmentu IP do nazwy, dzięki czemu konsola łączy się z urządzeniem tylko raz. Routery o tej samej Identity są traktowane jako jedno urządzenie; osobne routery powinny mieć różne nazwy.

## Uruchomienie

```sh
cp devices.example.json devices.json
```

`devices.example.json` zawiera przykładowe 28 urządzeń `S01R1`–`S14R2` z adresami `10.0.138.1`–`10.0.138.28` i wspólnymi ustawieniami w `defaults`. Przed użyciem dostosuj nazwy i adresy do swojej sieci, po czym uruchom:

```sh
python3 mikrotik-console.py
```

## Parametry programów

### `mikrotik-console.py`

```sh
python3 mikrotik-console.py [--config PLIK_JSON] [--lang pl|en]
```

| Parametr | Domyślna wartość | Opis |
|---|---|---|
| `-h`, `--help` | — | Wyświetla pomoc i kończy działanie. |
| `--lang pl`, `--lang en` | automatyczne wykrywanie | Wymusza polskie lub angielskie komunikaty i pomoc. |
| `-c PLIK_JSON`, `--config PLIK_JSON` | `devices.json` | Wskazuje plik konfiguracji urządzeń. |
| `--password-prompt` | wyłączony | Ukryty, zgodnościowy przełącznik; hasła i tak są pobierane interaktywnie według `password_mode` w konfiguracji. |

#### Pola pliku `devices.json`

Konfiguracja zawiera sekcję `defaults` ze wspólnymi ustawieniami oraz tablicę `devices` z urządzeniami. Pola urządzenia mogą nadpisać odpowiadające im wartości domyślne.

| Pole | Domyślna wartość | Opis |
|---|---|---|
| `defaults` | `{}` | Wspólne ustawienia urządzeń; każde pole można nadpisać w konkretnym wpisie. |
| `devices` | wymagane | Niepusta tablica urządzeń. |
| `name` | wymagane | Nazwa urządzenia używana w konsoli; musi być unikalna. |
| `host` | wymagane | Osiągalny adres IPv4 lub IPv6 routera. |
| `username` | `admin` | Nazwa użytkownika API. |
| `port` | `8728` bez TLS, `8729` z TLS | Port TCP API; jawnie podana wartość zastępuje port wynikający z `ssl`. |
| `ssl` | `false` | `true` włącza API-SSL, `false` używa nieszyfrowanego API. |
| `verify_ssl` | `true` | Weryfikuje certyfikat API-SSL. Ma znaczenie, gdy `ssl` jest włączone. |
| `password_mode` | `shared` | `shared` pyta raz o wspólne hasło, a `individual` pyta osobno dla każdego urządzenia. Wpis urządzenia może nadpisać tryb z `defaults`. |

Starsze pliki mogą ustawiać `password_mode` na najwyższym poziomie JSON; nowe konfiguracje powinny umieszczać go w `defaults`.

### `mikrotik-config-builder.py`

```sh
python3 mikrotik-config-builder.py [PARAMETRY]
```

| Parametr | Domyślna wartość | Opis |
|---|---|---|
| `-h`, `--help` | — | Wyświetla pomoc i kończy działanie. |
| `--lang pl`, `--lang en` | automatyczne wykrywanie | Wymusza polskie lub angielskie komunikaty i pomoc. |
| `--network CIDR` | `10.0.138.0/24` bez `--ipv6`; brak przy automatycznym wykrywaniu IPv6 | Prefiks skanowany po kolei. Z `--ipv6` pominięcie tej opcji aktywnie wykrywa urządzenia przez MNDP. |
| `--ipv6` | wyłączony | Używa IPv6. Bez `--network` wysyła zapytania MNDP przez IPv4 broadcast i IPv6 multicast, a następnie uzupełnia wykrywanie z tabel `/ip/neighbor` dostępnych routerów. Z `--network` skanuje podany prefiks. |
| `--username NAZWA` | `admin` | Wspólna nazwa użytkownika API używana do logowania i zapisana w sekcji `defaults`. Hasło jest pytane w terminalu. |
| `--port PORT` | `8728` | Port TCP API skanowanych routerów. Przy `--ssl` podaj port API-SSL, zwykle `8729`. |
| `--ssl` | wyłączony | Używa API-SSL zamiast nieszyfrowanego API. |
| `--insecure` | wyłączony | Wyłącza weryfikację certyfikatu; działa tylko z `--ssl`. |
| `--timeout SEKUNDY` | `1.5` | Maksymalny czas próby połączenia z jednym adresem. |
| `--workers LICZBA` | `48` | Maksymalna liczba równoległych prób połączenia. |
| `--max-hosts LICZBA` | `4096` | Limit liczby adresów do sprawdzenia w podanej podsieci. |
| `--output PLIK` | `devices.json` | Ścieżka pliku JSON, do którego zostanie zapisana konfiguracja. |
| `--force` | wyłączony | Zastępuje istniejący plik wynikowy bez pytania. Bez tego przełącznika program pyta o zgodę w terminalu. |

Przykład skanowania innej podsieci IPv4 z API-SSL i zapisem do osobnego pliku:

```sh
python3 mikrotik-config-builder.py --network 192.168.88.0/24 --ssl --port 8729 --output routers.json
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
