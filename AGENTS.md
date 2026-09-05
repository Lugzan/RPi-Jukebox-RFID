# Development notes

## Tests on macOS

This project has Linux- and Raspberry Pi-specific dependencies, including
`evdev`, which cannot be built on macOS because it requires Linux kernel
headers.

On macOS, do not run the complete test suite (`./run_pytest.sh`). Run focused,
platform-independent test suites instead, for example:

```bash
source .venv/bin/activate
pytest test/rfid/test_pn532_uart.py
```

Run the full suite only in a Linux/Raspberry Pi OS or project CI/Docker
environment.
