#!/usr/bin/env python3
"""Discover MikroTik routers through RouterOS API and build devices.json."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import ipaddress
import json
import ssl
import sys
from pathlib import Path


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
        return ((first & 0x1f) << 16) | int.from_bytes(await reader.readexactly(2), "big")
    if first < 0xf0:
        return ((first & 0x0f) << 24) | int.from_bytes(await reader.readexactly(3), "big")
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
        encoded = word.encode("utf-8")
        writer.write(encode_length(len(encoded)) + encoded)
    writer.write(b"\x00")
    await writer.drain()


async def read_command_reply(reader: asyncio.StreamReader) -> list[tuple[str, dict[str, str]]]:
    result: list[tuple[str, dict[str, str]]] = []
    while True:
        sentence = await read_sentence(reader)
        kind = sentence[0] if sentence else "!done"
        fields: dict[str, str] = {}
        for word in sentence[1:]:
            if word.startswith("=") and "=" in word[1:]:
                key, value = word[1:].split("=", 1)
                fields[key] = value
        result.append((kind, fields))
        if kind in ("!done", "!fatal"):
            return result


async def discover_one(address: str, args: argparse.Namespace, password: str,
                       semaphore: asyncio.Semaphore, tls_context: ssl.SSLContext | None
                       ) -> tuple[str, str] | None:
    async with semaphore:
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(address, args.port, ssl=tls_context), timeout=args.timeout
            )
            await write_sentence(writer, ["/login", f"=name={args.username}", f"=password={password}"])
            login = await asyncio.wait_for(read_command_reply(reader), timeout=args.timeout)
            if any(kind in ("!trap", "!fatal") for kind, _ in login):
                return None

            await write_sentence(writer, ["/system/identity/print"])
            reply = await asyncio.wait_for(read_command_reply(reader), timeout=args.timeout)
            if any(kind in ("!trap", "!fatal") for kind, _ in reply):
                return None
            identity = next((fields.get("name", "").strip()
                             for kind, fields in reply if kind == "!re" and fields.get("name", "").strip()), "")
            return (address, identity) if identity else None
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, ssl.SSLError):
            return None
        finally:
            if writer:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass


def unique_device_names(found: list[tuple[str, str]]) -> list[dict[str, str]]:
    counts: dict[str, int] = {}
    for _, identity in found:
        counts[identity] = counts.get(identity, 0) + 1
    used: set[str] = set()
    devices: list[dict[str, str]] = []
    for address, identity in found:
        name = identity
        if counts[identity] > 1:
            name = f"{identity}-{address.rsplit('.', 1)[-1]}"
        if name in used:
            name = f"{name}-{address}"
        used.add(name)
        devices.append({"name": name, "host": address})
    return devices


async def run(args: argparse.Namespace) -> int:
    try:
        network = ipaddress.ip_network(args.network, strict=False)
    except ValueError as exc:
        raise ValueError(f"Niepoprawna podsieć: {exc}") from exc
    if network.version != 4:
        raise ValueError("Skaner obsługuje obecnie podsieci IPv4.")
    host_count = network.num_addresses - (2 if network.prefixlen < 31 else 0)
    if host_count > args.max_hosts:
        raise ValueError(f"Podsieć obejmuje {host_count} adresów; limit to {args.max_hosts}. Podaj węższą podsieć lub zwiększ --max-hosts.")

    if args.output.exists() and not args.force:
        if not sys.stdin.isatty():
            raise ValueError(f"Plik {args.output} już istnieje. Użyj --force, aby go zastąpić.")
        answer = input(f"Plik {args.output} istnieje. Zastąpić go? [t/N] ").strip().lower()
        if answer not in ("t", "tak", "y", "yes"):
            print("Anulowano; plik nie został zmieniony.")
            return 0

    password = getpass.getpass(f"Hasło API dla {args.username}: ")
    if args.ssl:
        tls_context = ssl._create_unverified_context() if args.insecure else ssl.create_default_context()
    else:
        if args.insecure:
            raise ValueError("--insecure ma zastosowanie tylko razem z --ssl.")
        tls_context = None

    print(f"Skanuję {network} na porcie API {args.port}...")
    semaphore = asyncio.Semaphore(args.workers)
    hosts = [str(host) for host in network.hosts()]
    results = await asyncio.gather(*(discover_one(host, args, password, semaphore, tls_context) for host in hosts))
    found = sorted((result for result in results if result), key=lambda item: ipaddress.ip_address(item[0]))
    if not found:
        print("Nie znaleziono urządzeń z dostępnym API i podanymi danymi logowania.", file=sys.stderr)
        return 1

    devices = unique_device_names(found)
    config = {
        "defaults": {
            "username": args.username,
            "port": args.port,
            "ssl": args.ssl,
            "verify_ssl": not args.insecure,
            "password_mode": "shared",
        },
        "devices": devices,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(args.output)

    print(f"Zapisano {len(devices)} urządzeń do {args.output}:")
    for device in devices:
        print(f"  {device['name']}: {device['host']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Wykrywa MikroTiki przez API i tworzy devices.json")
    parser.add_argument("--network", default="10.0.138.0/24", help="skanowana podsieć IPv4 (domyślnie 10.0.138.0/24)")
    parser.add_argument("--username", default="admin", help="wspólna nazwa użytkownika API (domyślnie admin)")
    parser.add_argument("--port", type=int, default=8728, help="port API (domyślnie 8728)")
    parser.add_argument("--ssl", action="store_true", help="użyj API-SSL zamiast nieszyfrowanego API")
    parser.add_argument("--insecure", action="store_true", help="nie weryfikuj certyfikatu; używaj tylko z --ssl")
    parser.add_argument("--timeout", type=float, default=1.5, help="limit czasu próby dla jednego adresu w sekundach")
    parser.add_argument("--workers", type=int, default=48, help="maksymalna liczba równoległych prób")
    parser.add_argument("--max-hosts", type=int, default=4096, help="maksymalny rozmiar skanowanej podsieci")
    parser.add_argument("--output", type=Path, default=Path("devices.json"), help="plik wynikowy")
    parser.add_argument("--force", action="store_true", help="zastąp istniejący plik bez pytania")
    args = parser.parse_args()
    if args.workers < 1 or args.timeout <= 0 or args.max_hosts < 1:
        parser.error("--workers, --timeout i --max-hosts muszą być dodatnie")
    try:
        return asyncio.run(run(args))
    except (ValueError, KeyboardInterrupt) as exc:
        if str(exc):
            print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
