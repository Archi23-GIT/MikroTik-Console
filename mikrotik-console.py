#!/usr/bin/env python3
"""Interactive multi-router console using the native RouterOS API."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import re
import readline
import shlex
import shutil
import ssl
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Device:
    name: str
    host: str
    username: str
    port: int = 8729
    password_mode: str = "shared"
    password: str | None = None
    use_ssl: bool = True
    verify_ssl: bool = True
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def read_config(path: Path) -> tuple[list[Device], str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Nie można odczytać konfiguracji {path}: {exc}") from exc
    entries = data.get("devices") if isinstance(data, dict) else None
    defaults = data.get("defaults", {}) if isinstance(data, dict) else {}
    if not isinstance(defaults, dict):
        raise ValueError('Sekcja "defaults" musi być obiektem JSON.')
    # Keep loading older configuration files while preferring shared defaults.
    default_password_mode = defaults.get("password_mode", data.get("password_mode", "shared")) if isinstance(data, dict) else "shared"
    if default_password_mode not in ("shared", "individual"):
        raise ValueError('"password_mode" musi mieć wartość "shared" albo "individual".')
    if not isinstance(entries, list) or not entries:
        raise ValueError('Plik musi zawierać niepustą tablicę "devices".')
    devices: list[Device] = []
    names: set[str] = set()
    for item in entries:
        if not isinstance(item, dict) or not all(item.get(k) for k in ("name", "host")):
            raise ValueError("Każde urządzenie musi mieć pola name i host.")
        name = str(item["name"])
        if name in names:
            raise ValueError(f"Nazwa urządzenia powtarza się: {name}")
        names.add(name)
        username = item.get("username", defaults.get("username", "admin"))
        use_ssl = bool(item.get("ssl", defaults.get("ssl", False)))
        default_port = 8729 if use_ssl else 8728
        password_mode = item.get("password_mode", default_password_mode)
        if password_mode not in ("shared", "individual"):
            raise ValueError(f'Nieprawidłowy password_mode dla urządzenia {name}: użyj "shared" albo "individual".')
        devices.append(Device(
            name=name, host=str(item["host"]), username=str(username),
            port=int(item.get("port", defaults.get("port", default_port))), password_mode=password_mode,
            use_ssl=use_ssl, verify_ssl=bool(item.get("verify_ssl", defaults.get("verify_ssl", True))),
        ))
    return devices, default_password_mode


def encode_length(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    if length < 0x4000:
        length |= 0x8000
        return bytes([(length >> 8) & 0xff, length & 0xff])
    if length < 0x200000:
        length |= 0xc00000
        return bytes([(length >> 16) & 0xff, (length >> 8) & 0xff, length & 0xff])
    if length < 0x10000000:
        length |= 0xe0000000
        return bytes([(length >> 24) & 0xff, (length >> 16) & 0xff,
                      (length >> 8) & 0xff, length & 0xff])
    return b"\xf0" + length.to_bytes(4, "big")


async def read_length(reader: asyncio.StreamReader) -> int:
    first = (await reader.readexactly(1))[0]
    if first < 0x80:
        return first
    if first < 0xc0:
        return ((first & 0x3f) << 8) | (await reader.readexactly(1))[0]
    if first < 0xe0:
        tail = await reader.readexactly(2)
        return ((first & 0x1f) << 16) | int.from_bytes(tail, "big")
    if first < 0xf0:
        tail = await reader.readexactly(3)
        return ((first & 0x0f) << 24) | int.from_bytes(tail, "big")
    return int.from_bytes(await reader.readexactly(4), "big")


async def read_sentence(reader: asyncio.StreamReader) -> list[str]:
    words: list[str] = []
    while True:
        length = await read_length(reader)
        if length == 0:
            return words
        words.append((await reader.readexactly(length)).decode("utf-8", errors="replace"))


async def write_sentence(writer: asyncio.StreamWriter, words: list[str]) -> None:
    for word in words:
        payload = word.encode("utf-8")
        writer.write(encode_length(len(payload)) + payload)
    writer.write(b"\x00")
    await writer.drain()


def parse_command(line: str) -> list[str]:
    """Translate RouterOS menu paths and API-style arguments into a sentence."""
    try:
        tokens = shlex.split(line, posix=True)
    except ValueError as exc:
        raise ValueError(f"Niepoprawne cudzysłowy: {exc}") from exc
    if len(tokens) == 1 and tokens[0].strip("/") == "cancel":
        return ["/cancel"]
    # Accept both RouterOS menu paths (`/system package update ...`) and
    # API-style paths (`/system/package/update/...`). Do not split arguments.
    expanded: list[str] = []
    for index, token in enumerate(tokens):
        if "=" not in token and (token.startswith("/") or (index == 0 and "/" in token)):
            expanded.extend(part for part in token.strip("/").split("/") if part)
        else:
            expanded.append(token)
    tokens = expanded
    if not tokens:
        raise ValueError("Puste polecenie.")

    actions = {"print", "add", "set", "remove", "enable", "disable", "comment",
               "move", "run", "reset", "monitor", "export", "import", "edit",
               "check-for-updates", "install"}
    path_parts: list[str] = []
    action_index = None
    for i, token in enumerate(tokens):
        if token.lower() in actions:
            action_index = i
            break
        path_parts.append(token.strip("/"))
    if action_index is None:
        # RouterOS has commands whose final component is itself the action
        # (for example check-for-updates, reboot, or package-specific verbs).
        # If arguments follow, the preceding component is the action.
        first_argument = next((i for i, token in enumerate(tokens)
                               if token.startswith(("=", "?")) or "=" in token), len(tokens))
        action_index = first_argument - 1 if first_argument < len(tokens) else len(tokens) - 1
        path_parts = [part.strip("/") for part in tokens[:action_index]]
        if action_index < 0 or not tokens[action_index].strip("/"):
            raise ValueError("Nie znaleziono polecenia RouterOS.")
    path = "/".join(part for part in path_parts if part)
    action = tokens[action_index].lower()
    command = f"/{path}/{action}" if path else f"/{action}"
    words = [command]
    where_mode = False
    for token in tokens[action_index + 1:]:
        if token.lower() == "where":
            where_mode = True
        elif where_mode:
            if token.startswith("?"):
                words.append(token)
            elif token.startswith("!"):
                words.append("?-" + token[1:])
            elif "=" in token:
                key, value = token.split("=", 1)
                words.append(f"?{key}={value}")
            else:
                words.append(f"?{token}")
        elif token.startswith("?"):
            words.append(token)
        elif token.startswith("="):
            # Accept both RouterOS API's =key=value form and CLI key=value form.
            words.append(token)
        elif "=" in token:
            key, value = token.split("=", 1)
            words.append(f"={key}={value}")
        else:
            # API command flags (for example `print detail`) are empty-value words.
            words.append(f"={token}=")
    return words


def parse_reply(words: list[str]) -> tuple[str, dict[str, str]]:
    kind = words[0] if words else "!done"
    fields: dict[str, str] = {}
    for word in words[1:]:
        if word.startswith("=") and "=" in word[1:]:
            key, value = word[1:].split("=", 1)
            fields[key] = value
    return kind, fields


async def receive_reply(device: Device) -> list[tuple[str, dict[str, str]]]:
    assert device.reader
    replies: list[tuple[str, dict[str, str]]] = []
    while True:
        sentence = await read_sentence(device.reader)
        kind, fields = parse_reply(sentence)
        replies.append((kind, fields))
        if kind in ("!done", "!fatal"):
            return replies
        if kind == "!trap":
            # RouterOS normally follows !trap with !done; read it to keep the stream in sync.
            continue


async def connect_device(device: Device) -> None:
    context: ssl.SSLContext | bool
    if device.use_ssl:
        context = ssl.create_default_context() if device.verify_ssl else ssl._create_unverified_context()
    else:
        context = False
    device.reader, device.writer = await asyncio.wait_for(
        asyncio.open_connection(device.host, device.port, ssl=context), timeout=12
    )
    await write_sentence(device.writer, ["/login", f"=name={device.username}", f"=password={device.password or ''}"])
    replies = await asyncio.wait_for(receive_reply(device), timeout=12)
    if any(kind in ("!trap", "!fatal") for kind, _ in replies):
        message = next((fields.get("message", "odrzucono logowanie") for kind, fields in replies if kind in ("!trap", "!fatal")), "odrzucono logowanie")
        raise ConnectionError(message)


async def execute(device: Device, command: list[str], output: asyncio.Queue) -> None:
    if not device.writer or not device.reader or device.writer.is_closing():
        raise ConnectionError("połączenie jest zamknięte")
    async with device.lock:
        await write_sentence(device.writer, command)
        traps: list[str] = []
        while True:
            assert device.reader
            sentence = await asyncio.wait_for(read_sentence(device.reader), timeout=120)
            kind, fields = parse_reply(sentence)
            if kind == "!trap":
                message = fields.get("message", str(fields))
                if message not in traps:
                    traps.append(message)
            else:
                await output.put((device.name, kind, fields))
            if kind in ("!done", "!fatal"):
                if traps:
                    await output.put((device.name, "!trap", {"message": "; ".join(traps)}))
                return


async def output_printer(queue: asyncio.Queue) -> None:
    record_numbers: dict[str, int] = {}
    progress: dict[str, int | None] = {}
    progress_seen = False
    progress_active = False
    last_progress_bucket = -1
    tty = sys.stdout.isatty()
    check_updates_command = False

    def show_progress(name: str, status: str, percent: int | None) -> None:
        nonlocal progress_seen, progress_active, last_progress_bucket
        progress_seen = True
        progress[name] = percent if percent is not None else progress.get(name, 0)
        values = [value or 0 for value in progress.values()]
        average = sum(values) // max(1, len(values))
        ready = sum(value == 100 for value in values)
        line = f"Pobieranie pakietów: {ready}/{len(values)} gotowe, średnio {average}% | [{name}] {status}"
        width = max(48, shutil.get_terminal_size((120, 24)).columns)
        line = textwrap.shorten(line, width=width, placeholder="…")
        if tty:
            print("\r\x1b[2K" + line, end="", flush=True)
            progress_active = True
        else:
            bucket = average // 10
            if bucket > last_progress_bucket or ready == len(values):
                print(line, flush=True)
                last_progress_bucket = bucket

    while True:
        name, kind, fields = await queue.get()
        if kind == "!reset":
            record_numbers.pop(name, None)
            progress.setdefault(name, None)
            check_updates_command = fields.get("check_updates", False)
        elif kind == "!command-end":
            if progress_seen:
                if progress_active:
                    print(flush=True)
                ready = sum(value == 100 for value in progress.values())
                print(f"Pobieranie zakończone: {ready}/{len(progress)} routerów zgłosiło komplet pakietów. RouterOS prosi o restart.", flush=True)
            progress.clear()
            progress_seen = False
            progress_active = False
            last_progress_bucket = -1
            check_updates_command = False
        elif kind == "!re":
            status = fields.get("status", "")
            status_lower = status.lower()
            if "download" in status_lower or "calculating download size" in status_lower:
                match = re.search(r"(\d+)%", status)
                percent = int(match.group(1)) if match else (100 if "downloaded" in status_lower else None)
                show_progress(name, status, percent)
                continue
            record_numbers[name] = record_numbers.get(name, 0) + 1
            prefix = f"[{name}] #{record_numbers[name]} "
            content = "  ".join(f"{key}={value.replace(chr(10), ' / ')}" for key, value in fields.items())
            content = content or "(pusty rekord)"
            width = max(48, shutil.get_terminal_size((120, 24)).columns)
            lines = textwrap.wrap(content, width=max(20, width - len(prefix)),
                                  subsequent_indent=" " * len(prefix), break_long_words=False,
                                  break_on_hyphens=False)
            is_intermediate_check = status_lower.startswith("finding out latest version")
            is_update_status = check_updates_command and bool(status) and not is_intermediate_check
            color = "\033[32m" if status_lower == "system is already up to date" else "\033[31m"
            use_color = is_update_status and sys.stdout.isatty() and "NO_COLOR" not in os.environ
            for index, line in enumerate(lines or [content]):
                rendered = (prefix if index == 0 else " " * len(prefix)) + line
                print(f"{color}{rendered}\033[0m" if use_color else rendered, flush=True)
        elif kind == "!trap":
            if progress_active:
                print(flush=True)
                progress_active = False
            print(f"[{name}] BŁĄD: {fields.get('message', fields)}", file=sys.stderr, flush=True)
        elif kind == "!fatal":
            if progress_active:
                print(flush=True)
                progress_active = False
            print(f"[{name}] BŁĄD KRYTYCZNY: {fields}", file=sys.stderr, flush=True)


COMPLETIONS = [
    # SYSTEM
    "/system identity print", "/system resource print", "/system clock print",
    "/system health print", "/system routerboard print", "/system license print",
    "/system routerboard upgrade", "/system reboot",
    "/system/reset-configuration",
    "/system logging print", "/system ntp client print",
    "/system device-mode print",
    "/system scheduler print", "/system script print",
    "/log print", "/user print", "/user active print", "/certificate print",
    "/system package print",
    "/system package update print",
    "/system package update check-for-updates",
    "/system package update check-for-updates once",
    "/system package update download",
    "/system package update install",
    "/system package update set channel=stable",
    "/system package update set channel=long-term",
    "/system package update set channel=testing",
    "/system package update set channel=development",
    # IP
    "/interface print", "/interface ethernet print", "/interface bridge print",
    "/ip address print", "/ip arp print", "/ip neighbor print", "/ip route print",
    "/ip service print", "/ip dns print", "/ip cloud print", "/ip pool print",
    "/ip dhcp-client print", "/ip dhcp-server print", "/ip dhcp-server lease print",
    "/ip dhcp-server network print",
    "/ip firewall connection print", "/ip firewall address-list print",
    "/ip firewall filter print", "/ip firewall nat print",
    "/ip firewall mangle print", "/ip firewall raw print",
    "/ip ipsec peer print", "/ip ipsec policy print", "/ip ipsec active-peers print",
    "/queue simple print",
    # TOOLS
    "/tool ping address=", "/tool traceroute address=", "/tool torch interface=",
    "/tool netwatch print", "/tool mac-server print", "/tool e-mail print",
    "/tool sniffer print", "/tool profile", "/tool romon print",
    "/tool romon set enabled=yes", "tool/romon/set enabled=yes",
    ":help", ":list", ":all", ":only ", ":reconnect", ":quit",
]


def setup_completion(device_names: list[str]) -> None:
    def complete(text: str, state: int) -> str | None:
        buffer = readline.get_line_buffer()
        # libedit on macOS may treat `/` as a word break even after setting
        # empty completion delimiters. In that case readline replaces only
        # the final path component, so offer the leaf instead of the full path.
        if buffer.startswith("/system/") and "/" not in text:
            leaves = [
                value.rsplit("/", 1)[-1]
                for value in COMPLETIONS
                if value.startswith("/system/")
                and value.rsplit("/", 1)[-1].startswith(text)
            ]
            return leaves[state] if state < len(leaves) else None
        if buffer.startswith("//"):
            candidates = ["//" + value.lstrip("/") for value in COMPLETIONS]
        else:
            candidates = list(COMPLETIONS)
        for name in device_names:
            candidates.append(f":only {name}")
        candidates = [value for value in candidates if value.startswith(buffer)]
        return candidates[state] if state < len(candidates) else None

    readline.set_completer(complete)
    # Completion candidates are whole command lines (many contain spaces).
    readline.set_completer_delims("")
    # macOS Python commonly uses libedit; other builds use GNU readline.
    for binding in ("bind ^I rl_complete", "tab: complete"):
        try:
            readline.parse_and_bind(binding)
        except Exception:
            pass


async def close_device(device: Device) -> None:
    if device.writer:
        device.writer.close()
        try:
            await device.writer.wait_closed()
        except Exception:
            pass
    device.reader = None
    device.writer = None


async def reconnect_devices(devices: list[Device]) -> dict[str, Device]:
    print("Zamykam stare sesje i łączę ponownie ze wszystkimi urządzeniami...")
    await asyncio.gather(*(close_device(device) for device in devices), return_exceptions=True)
    results = await asyncio.gather(*(connect_device(device) for device in devices), return_exceptions=True)
    connected: dict[str, Device] = {}
    for device, result in zip(devices, results):
        if isinstance(result, BaseException):
            print(f"[{device.name}] Nadal niedostępny: {result}", file=sys.stderr)
            await close_device(device)
        else:
            connected[device.name] = device
            print(f"[{device.name}] Połączono ponownie: {device.username}@{device.host}:{device.port}")
    print(f"Połączono ponownie z {len(connected)} z {len(devices)} urządzeń.")
    return connected


def print_help() -> None:
    print("""Polecenia konsoli:
  /system identity print     wyślij polecenie API do wybranych urządzeń
  /ip address add address=192.0.2.1/24 interface=ether1
  /system package update check-for-updates once
  /system package update install  (instaluje aktualizację i restartuje router)
  /system routerboard upgrade     (aktualizuje RouterBOOT)
  /system reboot                  (restartuje router)
  /system/reset-configuration    (kasuje konfigurację i może rozłączyć router)
  :list                      pokaż urządzenia i bieżący wybór
  :only nazwa1,nazwa2        ustaw urządzenia docelowe
  :all                       wybierz wszystkie połączone urządzenia
  :reconnect                 połącz ponownie ze wszystkimi urządzeniami z konfiguracji
  Tab                        podpowiada główne polecenia IP, SYSTEM, TOOLS i nazwy urządzeń
  :help                      pokaż tę pomoc
  :quit                      zakończ i zamknij połączenia API

Używaj składni polecenia RouterOS: ścieżka, akcja, a następnie argumenty.
API nie udostępnia interaktywnego terminala; każde polecenie ma postać osobnej
komendy API, a wynik przychodzi jako rekordy. Odpowiedzi są wypisywane na bieżąco.
Przed instalacją aktualizacji lub resetem wybierz router poleceniem :only nazwa.
""")


async def run(args: argparse.Namespace) -> int:
    devices, default_password_mode = read_config(args.config)
    shared_password: str | None = None
    if any(d.password_mode == "shared" for d in devices):
        shared_password = getpass.getpass("Wspólne hasło API dla urządzeń: ")
    for device in devices:
        if device.password_mode == "individual":
            device.password = getpass.getpass(f"Osobne hasło API dla {device.name} ({device.username}@{device.host}): ")
        else:
            device.password = shared_password
    results = await asyncio.gather(*(connect_device(d) for d in devices), return_exceptions=True)
    connected: dict[str, Device] = {}
    for device, result in zip(devices, results):
        if isinstance(result, BaseException):
            print(f"[{device.name}] Błąd połączenia: {result}", file=sys.stderr)
            await close_device(device)
        else:
            connected[device.name] = device
            mode = "API-SSL" if device.use_ssl else "API (bez TLS)"
            print(f"[{device.name}] Połączono: {device.username}@{device.host}:{device.port} ({mode})")
    if not connected:
        return 1

    setup_completion(list(connected))
    output_queue: asyncio.Queue = asyncio.Queue()
    printer = asyncio.create_task(output_printer(output_queue))
    selected = set(connected)
    print("Konsola MikroTik API gotowa. Wpisz :help, aby zobaczyć polecenia.")
    try:
        while True:
            targets = ",".join(sorted(selected))
            try:
                line = await asyncio.to_thread(input, f"MikroTik[{targets}]> ")
            except EOFError:
                break
            stripped = line.strip()
            if not stripped:
                continue
            if stripped in (":quit", ":q", ":exit"):
                break
            if stripped == ":help":
                print_help()
            elif stripped == ":list":
                for name, device in connected.items():
                    state = "połączono" if device.writer and not device.writer.is_closing() else "rozłączono"
                    chosen = ", wybrane" if name in selected else ""
                    print(f"  {name}: {device.username}@{device.host}:{device.port} ({state}{chosen})")
            elif stripped == ":all":
                selected = set(connected)
                print("Wybrano wszystkie połączone urządzenia.")
            elif stripped == ":reconnect":
                connected = await reconnect_devices(devices)
                selected = set(connected)
                if not connected:
                    print("Nie udało się połączyć z żadnym urządzeniem. Możesz spróbować ponownie poleceniem :reconnect.", file=sys.stderr)
            elif stripped.startswith(":only "):
                requested = {part.strip() for part in stripped[6:].split(",") if part.strip()}
                unknown = requested - connected.keys()
                if unknown:
                    print("Nieznane lub niepołączone urządzenia: " + ", ".join(sorted(unknown)))
                elif not requested:
                    print("Podaj co najmniej jedną nazwę urządzenia.")
                else:
                    selected = requested
                    print("Wybrano: " + ", ".join(sorted(selected)))
            elif stripped.startswith(":"):
                print("Nieznane polecenie konsoli. Wpisz :help.")
            else:
                try:
                    command = parse_command(stripped)
                except ValueError as exc:
                    print(f"Błędne polecenie: {exc}", file=sys.stderr)
                    continue
                # Start row numbering afresh for each command, independently per router.
                for name in selected:
                    output_queue.put_nowait((name, "!reset", {
                        "check_updates": command[0] == "/system/package/update/check-for-updates"
                    }))
                target_names = sorted(selected)
                results = await asyncio.gather(*(execute(connected[name], command, output_queue) for name in target_names), return_exceptions=True)
                output_queue.put_nowait(("", "!command-end", {}))
                for name, result in zip(target_names, results):
                    if isinstance(result, BaseException):
                        print(f"[{name}] Błąd wykonania: {result}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nPrzerywam...")
    finally:
        printer.cancel()
        await asyncio.gather(printer, return_exceptions=True)
        await asyncio.gather(*(close_device(d) for d in connected.values()), return_exceptions=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Wspólna konsola API do wielu urządzeń MikroTik")
    parser.add_argument("-c", "--config", type=Path, default=Path("devices.json"), help="plik JSON z listą urządzeń (domyślnie devices.json)")
    parser.add_argument("--password-prompt", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except (ValueError, KeyboardInterrupt) as exc:
        if str(exc):
            print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
