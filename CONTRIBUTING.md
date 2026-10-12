# Contributing

Report bugs and propose improvements through GitHub Issues. Include the application version from `VERSION`, Python version, operating system, RouterOS version, command used, expected behavior, and sanitized output. Remove passwords, tokens, and private configuration before posting. Do not attach your active `devices.json` or backups.

Keep changes focused and update both `README.md` and `README_PL.md` when behavior changes. Add regression tests for meaningful bug fixes. Test doubles should avoid real router connections, credential prompts, and host network discovery.

Run tests from the repository root:

```sh
python3 -m unittest discover -v
```

On Windows, use `py -m unittest discover -v`. Install optional Windows completion support with `py -m pip install -r requirements.txt`.

Before changing application behavior, follow `BACKUP_POLICY.md`. Releases remain in the `1.6.x` series until the maintainer changes that policy. Submit pull requests with a description of the resulting behavior, validation, and any platform limitations. Contributions are provided under the repository's GNU GPL v3 license; do not include code with incompatible licensing.
