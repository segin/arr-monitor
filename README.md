# arr-monitor

`arr-monitor` is a Python script for monitoring the real-time progress of individual file operations in the *arr media managers, like Sonarr, et al.

<img width="662" height="445" alt="image" src="https://github.com/user-attachments/assets/7e457f39-d97e-480c-8477-7f26c4d76028" />

## Getting Started

`arr-monitor` comes as a single Python script, `arr-monitor.py`. Install the `psutil` library (`pip install psutil`, or your distribution's `python-psutil` package), place the script in your `$PATH` and run it.

You will need to either run `arr-monitor` as root or as a user with the `CAP_SYS_PTRACE` and `CAP_DAC_READ_SEARCH` capabilities granted.

Double-width characters in filenames (CJK, emoji) are measured with Python's built-in Unicode tables. Installing the optional `wcwidth` library makes this more accurate for newer characters.

## Usage

```
usage: arr-monitor.py [-h] [-d] [--log FILE] [-a] [-A] [-q] [pids ...]

Monitor file write operations for *arr media managers

positional arguments:
  pids                  Process ID(s) to monitor

options:
  -h, --help            show this help message and exit
  -d, --debug           Show debug information
  --log FILE            Enable debug logging to specified file
  -a, --all             Automatically monitor all detected *arr processes (in addition to any PIDs
                        given)
  -A, --add-new-processes
                        With --all, keep monitoring for newly started *arr processes
  -q, --qbittorrent     Also detect qBittorrent and track its moves of completed downloads to
                        their final folder

Examples:
  arr-monitor.py              # Interactive process selection
  arr-monitor.py -a           # Monitor all detected *arr processes
  arr-monitor.py -a -A        # Monitor all and keep adding new processes
  arr-monitor.py -a -q        # Monitor all, including qBittorrent moves
  arr-monitor.py 1234         # Monitor specific PID
  arr-monitor.py 1234 5678    # Monitor multiple PIDs
  arr-monitor.py --debug 1234 # Show debug info for PID
```

If no PID(s) or `--all` argument is provided, *arr media manager processes will be detected automatically. If only one is running it is monitored straight away; otherwise they are displayed in an interactive prompt for your selection, e.g.

```
Found 3 *arr process(es):

  1. Lidarr (PID: 1492)
  2. Radarr (PID: 1506)
  3. Sonarr (PID: 1507)
  A. Monitor all

Select process to monitor [1-3/A]:
```

### qBittorrent

With `-q`/`--qbittorrent`, `qbittorrent` and `qbittorrent-nox` processes are detected alongside the *arr managers, and the move of a completed download from the incomplete/temporary folder to its final folder is shown like any other transfer.

Only a move that actually copies data is shown, i.e. one between different filesystems. A move within one filesystem is a rename and finishes instantly. Files that are still downloading are not shown.

### When running

The name of the *arr working on the file and the destination filename will be shown in green.

The source file path will be shown in red below that, and the destination file path below that in blue.

Long paths will have the beginning of the path truncated off, see the screenshot at the beginning of this README.

If the source of a file cannot be identified, no source line or percentage is shown, only the amount written so far.

Press `q` to quit. Colours are turned off if the `NO_COLOR` environment variable is set.

### Tests

```
python3 -m unittest discover tests
```

## System Requirements

 * Linux
 * `procfs` virtual filesystem mounted at `/proc`
 * Python 3.8 or newer
 * `psutil` library

### Optional:
 * `wcwidth`

## License

`arr-monitor` is provided under [Creative Commons CC0 1.0 Universal](https://creativecommons.org/publicdomain/zero/1.0/).
