#!/usr/bin/env python3
"""Discover MikroTik routers through RouterOS API and build devices.json."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import ipaddress
import json
import socket
import ssl
import struct
import subprocess
import sys
import time
from pathlib import Path

from mikrotik_language import add_language_argument, configure_language, tr


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


def neighbor_ipv6_candidates(reply: list[tuple[str, dict[str, str]]], source: str,
                             local_interface: str = "") -> dict[str, str]:
    """Extract IPv6 addresses of MikroTik neighbors from RouterOS /ip/neighbor."""
    zone = source.partition("%")[2] or local_interface
    candidates: dict[str, str] = {}
    for kind, fields in reply:
        if kind != "!re":
            continue
        platform = fields.get("platform", "").casefold()
        if platform and "mikrotik" not in platform:
            continue
        address = fields.get("address6", "").strip()
        if not address:
            alternate = fields.get("address", "").strip()
            if ":" in alternate:
                address = alternate
        address = address.split("/", 1)[0]
        try:
            parsed = ipaddress.IPv6Address(address.split("%", 1)[0])
        except ValueError:
            continue
        if parsed.is_link_local:
            if not zone:
                continue
            candidates[f"{parsed}%{zone}"] = fields.get("identity", "").strip()
        elif not (parsed.is_multicast or parsed.is_unspecified or parsed.is_loopback):
            candidates[str(parsed)] = fields.get("identity", "").strip()
    return candidates


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


def _parse_mndp_ipv6(packet: bytes) -> list[str]:
    """Read IPv6 address TLVs from a valid MNDP announcement (UDP/5678)."""
    addresses: list[str] = []
    if len(packet) < 8:
        return addresses
    # MNDP starts with a four-byte header, followed by big-endian type/length TLVs.
    offset = 4
    has_mac = False
    ipv6_values: list[ipaddress.IPv6Address] = []
    while offset + 4 <= len(packet):
        kind, length = struct.unpack_from(">HH", packet, offset)
        offset += 4
        if length > len(packet) - offset:
            return []
        value = packet[offset:offset + length]
        offset += length
        if kind == 1 and length == 6:
            has_mac = True
        if kind == 15 and length == 16:  # IPv6 address
            address = ipaddress.IPv6Address(value)
            if address.is_link_local and not address.is_multicast:
                ipv6_values.append(address)
    if not has_mac:
        return []
    return [str(address) for address in ipv6_values]


def _parse_mndp_ipv4(packet: bytes) -> list[str]:
    """Read IPv4 address TLVs from a valid MNDP announcement."""
    addresses: list[str] = []
    if len(packet) < 8:
        return addresses
    offset = 4
    has_mac = False
    while offset + 4 <= len(packet):
        kind, length = struct.unpack_from(">HH", packet, offset)
        offset += 4
        if length > len(packet) - offset:
            return []
        value = packet[offset:offset + length]
        offset += length
        if kind == 1 and length == 6:
            has_mac = True
        elif kind == 5 and length == 4:
            address = ipaddress.IPv4Address(value)
            if not (address.is_unspecified or address.is_multicast or address.is_loopback):
                addresses.append(str(address))
    return addresses if has_mac else []


def _address_sort_key(host: str) -> tuple[int, int, str]:
    address, _, zone = host.partition("%")
    parsed = ipaddress.ip_address(address)
    return parsed.version, int(parsed), zone


def discover_mndp_ipv6(wait_seconds: float = 35.0) -> list[str]:
    """Actively discover RouterOS MNDP announcements on local IPv6 links."""
    if not hasattr(socket, "IPV6_JOIN_GROUP"):
        return []
    try:
        interfaces = socket.if_nameindex()
    except OSError:
        return []
    if not interfaces:
        return []

    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    found: set[str] = set()
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        try:
            sock.bind(("::", 5678))
        except OSError:
            # A running WinBox instance may already be listening on UDP/5678.
            sock.bind(("::", 0))
        group = socket.inet_pton(socket.AF_INET6, "ff02::1")
        joined: list[int] = []
        for interface_index, interface_name in interfaces:
            try:
                membership = group + struct.pack("@I", interface_index)
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_JOIN_GROUP, membership)
                joined.append(interface_index)
            except OSError:
                continue

        # Repeat the MNDP refresh because some links/devices drop the first multicast.
        def refresh() -> None:
            for interface_index in joined:
                try:
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, interface_index)
                    sock.sendto(b"\x00\x00\x00\x00", ("ff02::1", 5678, 0, interface_index))
                except OSError:
                    continue

        refresh()
        deadline = time.monotonic() + wait_seconds
        next_refresh = time.monotonic() + 2.0
        refreshes_left = 2
        while time.monotonic() < deadline:
            now = time.monotonic()
            timeout = min(deadline - now, max(0.05, next_refresh - now))
            sock.settimeout(timeout)
            try:
                packet, sender = sock.recvfrom(65535)
            except socket.timeout:
                if refreshes_left and time.monotonic() < deadline:
                    refresh()
                    refreshes_left -= 1
                    next_refresh = time.monotonic() + 2.0
                    continue
                if time.monotonic() >= deadline:
                    break
                continue
            except OSError:
                break
            scope_id = sender[3] if len(sender) > 3 else 0
            if not scope_id:
                continue
            try:
                # Windows interface names are not portable IPv6 zone identifiers.
                interface_name = str(scope_id) if sys.platform == "win32" else socket.if_indextoname(scope_id)
            except (AttributeError, OSError):
                continue
            for address in _parse_mndp_ipv6(packet):
                found.add(f"{address}%{interface_name}")
    finally:
        sock.close()

    return sorted(found, key=_address_sort_key)


def _windows_ipv4_broadcast_interfaces() -> list[tuple[str, str, str]]:
    """Read local IPv4 networks and matching IPv6 scope indices using PowerShell."""
    command = (
        "$ErrorActionPreference='Stop'; "
        "@(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.AddressState -eq 'Preferred' } | "
        "ForEach-Object { "
        "$v6 = Get-NetIPInterface -AddressFamily IPv6 -InterfaceAlias $_.InterfaceAlias "
        "-ErrorAction SilentlyContinue | Select-Object -First 1; "
        "[pscustomobject]@{ Address=$_.IPAddress; Prefix=$_.PrefixLength; Scope=$v6.InterfaceIndex } "
        "}) | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if result.returncode:
            raise ValueError(result.stderr.strip() or "PowerShell failed")
        records = json.loads(result.stdout.strip() or "[]")
        if isinstance(records, dict):
            records = [records]
        interfaces = []
        for record in records:
            address = ipaddress.IPv4Address(record["Address"])
            network = ipaddress.IPv4Network((str(address), int(record["Prefix"])), strict=False)
            scope = str(record.get("Scope") or "")
            if address.is_loopback or address.is_link_local or address.is_unspecified or network.prefixlen >= 31:
                continue
            interfaces.append((scope, str(address), str(network.broadcast_address)))
        return interfaces
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError) as exc:
        print(tr(f"Nie można odczytać interfejsów Windows dla MNDP IPv4: {exc}",
                 f"Cannot read Windows interfaces for IPv4 MNDP: {exc}"), file=sys.stderr)
        return []


def _local_ipv4_broadcast_interfaces() -> list[tuple[str, str, str]]:
    """Return (interface, local IPv4, directed broadcast) from ifconfig."""
    if sys.platform == "win32":
        return _windows_ipv4_broadcast_interfaces()
    try:
        result = subprocess.run(["/sbin/ifconfig", "-a"], capture_output=True,
                                text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []

    interfaces: list[tuple[str, str, str]] = []
    current_interface = ""
    for line in result.stdout.splitlines():
        if line and not line[0].isspace() and ":" in line:
            current_interface = line.split(":", 1)[0]
            continue
        fields = line.split()
        if not current_interface or len(fields) < 2 or fields[0] != "inet":
            continue
        try:
            address = ipaddress.IPv4Address(fields[1])
            mask_index = fields.index("netmask")
            raw_mask = fields[mask_index + 1]
            mask_value = int(raw_mask, 16) if raw_mask.startswith("0x") else int(ipaddress.IPv4Address(raw_mask))
            network = ipaddress.IPv4Network((str(address), str(ipaddress.IPv4Address(mask_value))), strict=False)
        except (ValueError, IndexError):
            continue
        if address.is_unspecified or address.is_loopback or address.is_link_local or network.prefixlen >= 31:
            continue
        interfaces.append((current_interface, str(address), str(network.broadcast_address)))
    return interfaces


def discover_mndp_ipv4(wait_seconds_per_interface: float = 1.5) -> dict[str, str]:
    """Ask for MNDP advertisements over each local IPv4 broadcast interface."""
    found: dict[str, str] = {}
    for interface, local_address, broadcast in _local_ipv4_broadcast_interfaces():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                sock.bind((local_address, 5678))
            except OSError:
                # If WinBox already owns UDP/5678, unicast replies can still return
                # to the request's ephemeral source port.
                sock.bind((local_address, 0))
            # WinBox sends a limited broadcast and each interface's directed broadcast.
            request = b"\x00\x00\x00\x00"
            sock.sendto(request, ("255.255.255.255", 5678))
            if broadcast != "255.255.255.255":
                sock.sendto(request, (broadcast, 5678))
            deadline = time.monotonic() + wait_seconds_per_interface
            next_refresh = time.monotonic() + wait_seconds_per_interface / 2
            refreshed = False
            while time.monotonic() < deadline:
                timeout = min(deadline - time.monotonic(), max(0.05, next_refresh - time.monotonic()))
                sock.settimeout(timeout)
                try:
                    packet, _sender = sock.recvfrom(65535)
                except socket.timeout:
                    if not refreshed and time.monotonic() < deadline:
                        sock.sendto(request, ("255.255.255.255", 5678))
                        if broadcast != "255.255.255.255":
                            sock.sendto(request, (broadcast, 5678))
                        refreshed = True
                        continue
                    break
                for address in _parse_mndp_ipv6(packet):
                    if interface:
                        found[f"{address}%{interface}"] = interface
                # IPv4 MNDP packets can still bootstrap a RouterOS API query;
                # its /ip/neighbor table then supplies link-local IPv6 peers.
                for address in _parse_mndp_ipv4(packet):
                    found[address] = interface
        except OSError:
            continue
        finally:
            sock.close()
    return found


def discover_ipv6_neighbors() -> dict[str, str]:
    """Discover IPv6 endpoints and IPv4 bootstrap endpoints from MNDP."""
    found = {host: host.partition("%")[2] for host in discover_mndp_ipv6()}
    found.update(discover_mndp_ipv4())
    return dict(sorted(found.items(), key=lambda item: _address_sort_key(item[0])))


async def discover_one(address: str, args: argparse.Namespace, password: str,
                       semaphore: asyncio.Semaphore, tls_context: ssl.SSLContext | None,
                       read_neighbors: bool = False, known_identity: str = "",
                       local_interface: str = ""
                       ) -> tuple[str, str, str, dict[str, str]]:
    async with semaphore:
        writer: asyncio.StreamWriter | None = None
        phase = tr("nawiązywania TCP", "establishing TCP connection")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(address, args.port, ssl=tls_context), timeout=args.timeout
            )
            phase = tr("logowania do API", "logging in to the API")
            await write_sentence(writer, ["/login", f"=name={args.username}", f"=password={password}"])
            login = await asyncio.wait_for(read_command_reply(reader), timeout=args.timeout)
            login_error = next((fields.get("message", tr("logowanie odrzucone", "login rejected"))
                                for kind, fields in login if kind in ("!trap", "!fatal")), None)
            if login_error:
                return address, "", tr(f"{known_identity + ': ' if known_identity else ''}logowanie: {login_error}", f"{known_identity + ': ' if known_identity else ''}login: {login_error}"), {}

            phase = tr("pobierania identity", "reading identity")
            await write_sentence(writer, ["/system/identity/print"])
            reply = await asyncio.wait_for(read_command_reply(reader), timeout=args.timeout)
            command_error = next((fields.get("message", tr("komenda odrzucona", "command rejected"))
                                  for kind, fields in reply if kind in ("!trap", "!fatal")), None)
            if command_error:
                return address, "", f"{known_identity + ': ' if known_identity else ''}identity: {command_error}", {}
            identity = next((fields.get("name", "").strip()
                             for kind, fields in reply if kind == "!re" and fields.get("name", "").strip()), "")
            neighbor_hosts: dict[str, str] = {}
            if identity and read_neighbors:
                try:
                    await write_sentence(writer, ["/ip/neighbor/print"])
                    neighbor_reply = await asyncio.wait_for(read_command_reply(reader), timeout=args.timeout)
                    if not any(kind in ("!trap", "!fatal") for kind, _ in neighbor_reply):
                        neighbor_hosts = neighbor_ipv6_candidates(neighbor_reply, address, local_interface)
                except (asyncio.TimeoutError, OSError, asyncio.IncompleteReadError):
                    pass
            error = "" if identity else tr(f"{known_identity + ': ' if known_identity else ''}API nie zwróciło nazwy identity", f"{known_identity + ': ' if known_identity else ''}the API did not return an identity name")
            return address, identity, error, neighbor_hosts
        except asyncio.TimeoutError:
            return address, "", tr(f"{known_identity + ': ' if known_identity else ''}timeout podczas {phase}", f"{known_identity + ': ' if known_identity else ''}timeout while {phase}"), {}
        except ssl.SSLError as exc:
            return address, "", tr(f"{known_identity + ': ' if known_identity else ''}TLS podczas {phase}: {exc}", f"{known_identity + ': ' if known_identity else ''}TLS error while {phase}: {exc}"), {}
        except asyncio.IncompleteReadError:
            return address, "", tr(f"{known_identity + ': ' if known_identity else ''}serwer zamknął połączenie podczas {phase}", f"{known_identity + ': ' if known_identity else ''}the server closed the connection while {phase}"), {}
        except OSError as exc:
            return address, "", f"{known_identity + ': ' if known_identity else ''}{phase}: {exc.strerror or exc}", {}
        finally:
            if writer:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass


def unique_device_names(found: list[tuple[str, str]]) -> list[dict[str, str]]:
    """Keep the first reachable address for each RouterOS identity."""
    used: set[str] = set()
    devices: list[dict[str, str]] = []
    for address, identity in found:
        if identity in used:
            continue
        used.add(identity)
        devices.append({"name": identity, "host": address})
    return devices


async def run(args: argparse.Namespace) -> int:
    print(tr("Przygotowanie skanowania urządzeń...", "Preparing device scan..."), flush=True)
    automatic_ipv6 = args.ipv6 and args.network is None
    if automatic_ipv6:
        print(tr("Wykrywanie MNDP: oczekiwanie na ogłoszenia IPv6 (35 sekund), następnie sprawdzanie interfejsów IPv4...", "MNDP discovery: waiting for IPv6 announcements (35 seconds), then checking IPv4 interfaces..."), flush=True)
        hosts = discover_ipv6_neighbors()
        if not hosts:
            print(tr("Nie wykryto adresów IPv6 w ogłoszeniach MNDP. Sprawdź, czy MNDP jest włączone na routerach i czy komputer jest w tym samym segmencie sieci.", "No IPv6 addresses found in MNDP announcements. Check that MNDP is enabled on the routers and that the computer is on the same network segment."), file=sys.stderr)
            return 1
        if len(hosts) > args.max_hosts:
            raise ValueError(tr(f"Wykryto {len(hosts)} adresów IPv6; limit to {args.max_hosts}. Zwiększ --max-hosts albo podaj prefiks przez --network.", f"Discovered {len(hosts)} IPv6 addresses; the limit is {args.max_hosts}. Increase --max-hosts or specify a prefix using --network."))
        ipv6_count = sum(":" in host for host in hosts)
        ipv4_bootstrap_count = len(hosts) - ipv6_count
        scan_description = tr(f"{ipv6_count} adresów IPv6 oraz {ipv4_bootstrap_count} adresów IPv4 do odczytu tabel sąsiadów MNDP", f"{ipv6_count} IPv6 addresses and {ipv4_bootstrap_count} IPv4 addresses for reading MNDP neighbor tables")
    else:
        network_text = args.network or "10.0.138.0/24"
        try:
            network = ipaddress.ip_network(network_text, strict=False)
        except ValueError as exc:
            raise ValueError(tr(f"Niepoprawna podsieć: {exc}", f"Invalid subnet: {exc}")) from exc
        expected_version = 6 if args.ipv6 else 4
        if network.version != expected_version:
            if args.ipv6:
                raise ValueError(tr("Opcja --ipv6 wymaga podania podsieci IPv6 w --network.", "The --ipv6 option requires an IPv6 subnet in --network."))
            raise ValueError(tr("Podano podsieć IPv6. Użyj --ipv6, aby skanować IPv6.", "An IPv6 subnet was specified. Use --ipv6 to scan IPv6."))
        if network.version == 4:
            host_count = network.num_addresses - (2 if network.prefixlen < 31 else 0)
        else:
            # IPv6 hosts() excludes subnet-router anycast except for /127 and /128.
            host_count = network.num_addresses if network.prefixlen >= 127 else network.num_addresses - 1
        if host_count > args.max_hosts:
            raise ValueError(tr(f"Podsieć obejmuje {host_count} adresów; limit to {args.max_hosts}. Podaj węższą podsieć lub zwiększ --max-hosts.", f"The subnet contains {host_count} addresses; the limit is {args.max_hosts}. Specify a smaller subnet or increase --max-hosts."))
        hosts = [str(host) for host in network.hosts()]
        scan_description = str(network)

    if args.output.exists() and not args.force:
        if not sys.stdin.isatty():
            raise ValueError(tr(f"Plik {args.output} już istnieje. Użyj --force, aby go zastąpić.", f"File {args.output} already exists. Use --force to overwrite it."))
        answer = input(tr(f"Plik {args.output} istnieje. Zastąpić go? [t/N] ", f"File {args.output} exists. Overwrite it? [y/N] ")).strip().lower()
        if answer not in ("t", "tak", "y", "yes"):
            print(tr("Anulowano; plik nie został zmieniony.", "Cancelled; the file was not changed."))
            return 0

    password = getpass.getpass(tr(f"Hasło API dla {args.username}: ", f"API password for {args.username}: "))
    if args.ssl:
        tls_context = ssl._create_unverified_context() if args.insecure else ssl.create_default_context()
    else:
        if args.insecure:
            raise ValueError(tr("--insecure ma zastosowanie tylko razem z --ssl.", "--insecure can only be used with --ssl."))
        tls_context = None

    print(tr(f"Skanuję {scan_description} na porcie API {args.port}...", f"Scanning {scan_description} on API port {args.port}..."))
    semaphore = asyncio.Semaphore(args.workers)
    if automatic_ipv6:
        pending = {host: ("", scope) for host, scope in hosts.items()}
    else:
        pending = {host: ("", "") for host in dict.fromkeys(hosts)}
    attempted: set[str] = set()
    results: list[tuple[str, str, str, dict[str, str]]] = []
    discovered_from_routers = 0
    while pending:
        batch = [host for host in pending if host not in attempted]
        if not batch:
            break
        if len(attempted) + len(batch) > args.max_hosts:
            batch = batch[:max(0, args.max_hosts - len(attempted))]
        if not batch:
            break
        batch_labels = pending
        pending = {}
        attempted.update(batch)
        batch_results = await asyncio.gather(*(
            discover_one(host, args, password, semaphore, tls_context,
                         read_neighbors=automatic_ipv6,
                         known_identity=batch_labels.get(host, ("", ""))[0],
                         local_interface=batch_labels.get(host, ("", ""))[1])
            for host in batch
        ))
        results.extend(batch_results)
        if automatic_ipv6:
            for _address, identity, _error, neighbors in batch_results:
                if identity:
                    for neighbor, neighbor_identity in neighbors.items():
                        if neighbor not in attempted:
                            pending.setdefault(neighbor, (neighbor_identity, _address.partition("%")[2] or batch_labels.get(_address, ("", ""))[1]))
            if len(attempted) + len(pending) > args.max_hosts:
                pending = dict(sorted(pending.items(), key=lambda item: _address_sort_key(item[0]))[:max(0, args.max_hosts - len(attempted))])
            discovered_from_routers += len(pending)

    if discovered_from_routers:
        print(tr(f"Dodatkowo znaleziono {discovered_from_routers} adresów IPv6 w tabelach sąsiadów RouterOS.", f"Found {discovered_from_routers} additional IPv6 addresses in RouterOS neighbor tables."))
    found = sorted(
        ((address, identity) for address, identity, _error, _neighbors in results
         if identity and (not automatic_ipv6 or ":" in address)),
        key=lambda item: (
            ipaddress.ip_address(item[0].split("%", 1)[0]).version,
            int(ipaddress.ip_address(item[0].split("%", 1)[0])),
            item[0].partition("%")[2],
        ),
    )
    failures = [(address, error) for address, identity, error, _neighbors in results if not identity]
    if not found:
        print(tr("Nie znaleziono urządzeń z dostępnym API i podanymi danymi logowania.", "No devices found with an accessible API and the supplied credentials."), file=sys.stderr)
        for address, error in failures:
            if ":" in address:
                print(f"  [{address}] {error}", file=sys.stderr)
        return 1
    if failures:
        print(tr(f"Uwaga: API nie odpowiedziało poprawnie dla {len(failures)} z {len(attempted)} sprawdzonych adresów.", f"Warning: the API did not respond successfully for {len(failures)} of {len(attempted)} addresses checked."))
        for address, error in failures:
            if ":" in address:
                print(f"  [{address}] {error}")

    devices = unique_device_names(found)
    duplicates = len(found) - len(devices)
    if duplicates:
        print(tr(f"Pominięto {duplicates} dodatkowych adresów urządzeń o tej samej nazwie RouterOS Identity.", f"Skipped {duplicates} additional addresses of devices with the same RouterOS Identity."))
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

    print(tr(f"Zapisano {len(devices)} urządzeń do {args.output}:", f"Saved {len(devices)} devices to {args.output}:"))
    for device in devices:
        print(f"  {device['name']}: {device['host']}")
    return 0


def main() -> int:
    configure_language()
    parser = argparse.ArgumentParser(description=tr("Wykrywa MikroTiki przez API i tworzy devices.json", "Discover MikroTik devices via API and create devices.json"))
    add_language_argument(parser)
    parser.add_argument("--network", help=tr("prefiks do skanowania; domyślnie IPv4 10.0.138.0/24, gdy nie użyto --ipv6", "prefix to scan; defaults to IPv4 10.0.138.0/24 unless --ipv6 is used"))
    parser.add_argument("--ipv6", action="store_true", help=tr("użyj IPv6; bez --network wykrywa routery przez MNDP na lokalnych łączach, z --network skanuje ten prefiks", "use IPv6; without --network discover routers via MNDP on local links, with --network scan that prefix"))
    parser.add_argument("--username", default="admin", help=tr("wspólna nazwa użytkownika API (domyślnie admin)", "shared API username (default: admin)"))
    parser.add_argument("--port", type=int, default=8728, help=tr("port API (domyślnie 8728)", "API port (default: 8728)"))
    parser.add_argument("--ssl", action="store_true", help=tr("użyj API-SSL zamiast nieszyfrowanego API", "use API-SSL instead of unencrypted API"))
    parser.add_argument("--insecure", action="store_true", help=tr("nie weryfikuj certyfikatu; używaj tylko z --ssl", "skip certificate verification; use only with --ssl"))
    parser.add_argument("--timeout", type=float, default=1.5, help=tr("limit czasu próby dla jednego adresu w sekundach", "timeout per address in seconds"))
    parser.add_argument("--workers", type=int, default=48, help=tr("maksymalna liczba równoległych prób", "maximum number of concurrent attempts"))
    parser.add_argument("--max-hosts", type=int, default=4096, help=tr("maksymalny rozmiar skanowanej podsieci", "maximum number of addresses to scan"))
    parser.add_argument("--output", type=Path, default=Path("devices.json"), help=tr("plik wynikowy", "output file"))
    parser.add_argument("--force", action="store_true", help=tr("zastąp istniejący plik bez pytania", "overwrite the existing file without prompting"))
    args = parser.parse_args()
    if args.workers < 1 or args.timeout <= 0 or args.max_hosts < 1:
        parser.error(tr("--workers, --timeout i --max-hosts muszą być dodatnie", "--workers, --timeout and --max-hosts must be positive"))
    try:
        return asyncio.run(run(args))
    except (ValueError, KeyboardInterrupt) as exc:
        if str(exc):
            print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
