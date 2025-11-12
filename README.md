# arr-monitor

`arr-monitor` is a Python script for montioring the real-time progress of individual file operations in the *arr media managers, like Sonarr, et. al.

<img width="662" height="445" alt="image" src="https://github.com/user-attachments/assets/7e457f39-d97e-480c-8477-7f26c4d76028" />

## Getting Started

`arr-monitor` comes as a single Python script, `arr-monitor.py`. Place it in your `$PATH` and run it.

You will need to either run `arr-monitor` as root or as a user with the `CAP_SYS_PTRACE` and `CAP_DAC_READ_SEARCH` capabilities granted.

Unicode filenames with double-width characters may cause display issues if you do not have the `wcwidth` library installed. 

## Usage

```
usage: arr-monitor.py [-h] [-d] [--log FILE] [--all] [pids ...]

Monitor file write operations for *arr media managers

positional arguments:
  pids         Process ID(s) to monitor

options:
  -h, --help   show this help message and exit
  -d, --debug  Show debug information
  --log FILE   Enable debug logging to specified file
  --all        Automatically monitor all detected *arr processes

Examples:
  arr-monitor.py              # Interactive process selection
  arr-monitor.py --all        # Monitor all detected *arr processes
  arr-monitor.py 1234         # Monitor specific PID
  arr-monitor.py 1234 5678    # Monitor multiple PIDs
  arr-monitor.py --debug 1234 # Show debug info for PI
```

If no PID(s) or `--all` argument is provided, *arr media manager processes will be detected automatically and displayed in an interactive prompt for your selection, e.g.

```
Found 3 *arr process(es):

  1. Lidarr (PID: 1492)
  2. Radarr (PID: 1506)
  3. Sonarr (PID: 1507)
  A. Monitor all

Select process to monitor [1-3/A]:
```

## System Requirements

 * Linux
 * `procps` virtual filesystem mounted at `/proc`
 * Python 3.x
 * `psutil` library
### Optional:
 * `wcwidth`
