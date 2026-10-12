# Security

## Supported version

Security fixes target the latest `1.6.x` version. Older patch versions should be upgraded. There is no guaranteed response time or long-term support commitment.

## Reporting vulnerabilities

Do not publish credentials, exploitable configuration, or sensitive network details in a public issue. Use GitHub's private vulnerability reporting if it is enabled on this repository. If that option is unavailable, open an issue requesting a private reporting channel without disclosing exploit details.

The tool executes commands with the permissions of the configured RouterOS account. Use the minimum required permissions. API without TLS exposes credentials and commands to network observers; use API-SSL with certificate verification or a trusted network/VPN. Avoid disabling verification unless you understand the consequences.

Passwords remain in process memory. Local device files and backups may contain network topology and usernames and must remain private. This tool does not provide cross-router transactions or rollback.
