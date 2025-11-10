# arr-monitor

`arr-monitor` is a Python script for montioring the real-time progress of individual file operations in the *arr media managers, like Sonarr, et. al.

![Recording 2025-11-08 221514](https://github.com/user-attachments/assets/fa11b794-4076-4d11-9f30-463104d1f14a)

## Getting Started

`arr-monitor` comes as a single Python script, `arr-monitor.py`. Place it in your `$PATH` and run it.

You will need to either run `arr-monitor` as root, or a user with the `CAP_SYS_PTRACE` and `CAP_DAC_READ_SEARCH` capabilities granted.

## System Requirements

 * Linux
 * `procps` virtual filesystem mounted at `/proc`
 * Python 3.x
 * `psutil` library
