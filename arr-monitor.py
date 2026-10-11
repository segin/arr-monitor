#!/usr/bin/env python3

"""
*arr Media Manager File Transfer Monitor
Monitors file write operations for Sonarr, Radarr, Lidarr, etc.
"""

import sys
import platform

# Check for Linux before importing anything that may not exist elsewhere
if platform.system() != 'Linux':
    print("Error: This tool requires Linux (uses /proc filesystem).", file=sys.stderr)
    print(f"Detected OS: {platform.system()}", file=sys.stderr)
    sys.exit(1)

import os
import time
import curses
from curses import window as CursesWindow
import argparse
import re
import shlex
import signal
import stat
import unicodedata
from datetime import datetime
from threading import Lock
from typing import Optional, Dict, Tuple, List, NamedTuple, TextIO

try:
    import psutil
except ImportError:
    print("Error: The 'psutil' library is required. Install it with: pip install psutil", file=sys.stderr)
    sys.exit(1)

# Try to import wcwidth for proper double-width character handling
try:
    from wcwidth import wcwidth as _wcwidth
    HAS_WCWIDTH = True
except ImportError:
    HAS_WCWIDTH = False

# Media manager process names to auto-detect
ARR_MANAGERS = [
    'Sonarr', 'Radarr', 'Lidarr', 'Readarr',
    'Prowlarr', 'Bazarr', 'Whisparr'
]

# Runtimes that can host a manager, which is then named on their command line
# (e.g. "mono Sonarr.exe", "python3 bazarr.py")
HOST_RUNTIMES = ('mono', 'mono-sgen', 'dotnet', 'python', 'python3')
HOSTED_EXTENSIONS = ('.exe', '.dll', '.py')

# qBittorrent process names, only auto-detected with --qbittorrent
QBITTORRENT_NAMES = ['qbittorrent', 'qbittorrent-nox']

# File extensions to ignore (databases, logs, etc.)
IGNORE_EXTENSIONS = {
    '.db', '.db-wal', '.db-shm', '.db-journal',
    '.log', '.txt', '.xml', '.json', '.conf',
    '.zip', '.dll', '.nfo'
}

# File access modes from open() flags (O_RDONLY, O_WRONLY, O_RDWR)
ACCESS_MODE_READ = 0
ACCESS_MODE_WRITE = 1
ACCESS_MODE_READWRITE = 2
ACCESS_MODE_MASK = 0o3  # Mask to extract access mode from flags

# Suffix the kernel appends to the link text of an unlinked file
DELETED_SUFFIX = ' (deleted)'

# Colour pair numbers and their foreground colours
COLOR_TITLE = 1
COLOR_IDLE = 3
COLOR_NAME = 4
COLOR_DEST = 5
COLOR_BAR = 6
COLOR_SPEED = 7
COLOR_SOURCE = 8
COLOR_PAIRS = {
    COLOR_TITLE: curses.COLOR_CYAN,
    COLOR_IDLE: curses.COLOR_YELLOW,
    COLOR_NAME: curses.COLOR_GREEN,
    COLOR_DEST: curses.COLOR_BLUE,
    COLOR_BAR: curses.COLOR_MAGENTA,
    COLOR_SPEED: curses.COLOR_CYAN,
    COLOR_SOURCE: curses.COLOR_RED,
}

class ReadFileInfo(NamedTuple):
    """Information about a file being read by the process"""
    size: int
    path: str

# Configuration constants grouped by category
class Config:
    """Configuration constants for the monitor"""
    # Polling and logging
    POLL_INTERVAL_SECONDS = 0.5
    VERBOSE_LOG_INTERVAL = 100  # Log verbosely every N iterations
    NEW_PROCESS_SCAN_INTERVAL = 20  # Check for new *arr processes every N iterations
    LOG_MAX_BYTES = 50 * 1024 * 1024  # Stop logging once the debug log reaches this size

    # File transfer tracking
    MIN_SPEED_SAMPLE_SECONDS = 0.1  # Ignore speed samples taken closer together than this
    SPEED_SMOOTHING = 0.5  # Weight of the newest sample in the speed average
    STALL_SECONDS = 3.0  # Report zero speed after this long without progress

    # UI dimensions
    MAX_PROGRESS_BAR_WIDTH = 40
    MIN_TERMINAL_HEIGHT = 5
    MIN_TERMINAL_WIDTH = 20
    PATH_INDENT = 2  # Indentation for source/destination paths
    FIRST_ENTRY_ROW = 4  # First row below the header

    # Cache limits
    EPISODE_CACHE_MAX_SIZE = 1000  # Maximum entries in episode cache
    PATH_CACHE_MAX_SIZE = 500  # Maximum entries in path abbreviation cache

def sanitize_text(text: str) -> str:
    """Make untrusted text (file and process names) safe to print

    Names come from other processes and may contain control characters,
    terminal escape sequences or bytes that are not valid UTF-8.
    """
    text = text.encode('utf-8', 'backslashreplace').decode('utf-8')
    return ''.join(ch if ch.isprintable() else '?' for ch in text)

def _char_width(ch: str) -> int:
    """Number of terminal columns a character occupies"""
    if HAS_WCWIDTH:
        width = _wcwidth(ch)
        return width if width >= 0 else 1
    if unicodedata.combining(ch) or unicodedata.category(ch) in ('Mn', 'Me'):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1

def display_width(text: str) -> int:
    """Number of terminal columns a string occupies"""
    if text.isascii():
        return len(text)
    return sum(_char_width(ch) for ch in text)

def clip_to_width(text: str, max_width: int, keep_end: bool = False) -> str:
    """Cut a string down to at most max_width terminal columns

    Args:
        text: String to cut
        max_width: Maximum display width in columns
        keep_end: Keep the end of the string instead of the start
    """
    if max_width <= 0:
        return ""
    if display_width(text) <= max_width:
        return text

    kept = []
    used = 0
    for ch in (reversed(text) if keep_end else text):
        width = _char_width(ch)
        if used + width > max_width:
            break
        kept.append(ch)
        used += width
    if keep_end:
        kept.reverse()
    return ''.join(kept)

class DebugLogger:
    """Thread-safe debug logger with context manager support"""
    def __init__(self, filepath: Optional[str] = None):
        self.filepath = filepath
        self.file_handle: Optional[TextIO] = None
        self.lock = Lock()
        self.bytes_written = 0
        self.error: Optional[str] = None  # Why logging stopped early, if it did

    def __enter__(self):
        """Open log file when entering context"""
        if self.filepath:
            try:
                # This usually runs as root: never write through a symlink, and
                # keep the log (which lists other users' files) private
                fd = os.open(self.filepath,
                             os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                self.file_handle = os.fdopen(fd, 'w', encoding='utf-8', errors='backslashreplace')
                self.file_handle.write(f"=== *arr Monitor Debug Log - {datetime.now()} ===\n\n")
                self.file_handle.flush()
            except OSError as e:
                print(f"Warning: Could not create debug log at {self.filepath}: {e}", file=sys.stderr)
                self.file_handle = None
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Close log file when exiting context"""
        self._close()
        return False

    def _close(self) -> None:
        if self.file_handle:
            try:
                self.file_handle.close()
            except OSError:
                pass
            self.file_handle = None

    def log(self, message: str) -> None:
        """Write debug message to log file"""
        with self.lock:
            if not self.file_handle:
                return
            try:
                timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                line = f"[{timestamp}] {sanitize_text(message)}\n"
                self.file_handle.write(line)
                self.bytes_written += len(line)
                if self.bytes_written >= Config.LOG_MAX_BYTES:
                    self.file_handle.write("Log size limit reached, logging stopped\n")
                    self.error = "size limit reached"
                    self._close()
                    return
                self.file_handle.flush()
            except (OSError, ValueError) as e:
                # The screen belongs to curses here, so report it after exit
                self.error = str(e)
                self._close()

    @property
    def is_enabled(self) -> bool:
        """Check if logging is enabled"""
        return self.file_handle is not None

class FileTransferInfo:
    """Tracks information about a file being written"""
    def __init__(self, fd: str, filepath: str, size: int,
                 target_size: Optional[int] = None, source_filepath: Optional[str] = None):
        self.fd = fd
        self.filepath = filepath
        self.source_filepath = source_filepath  # Store the source file path
        self.position = size
        self.target_size = target_size  # None when the final size is unknown
        self.speed: float = 0
        self.has_speed_sample = False
        self._sample_position = size
        self._sample_time = time.monotonic()
        self._last_progress_time = self._sample_time
        self._raise_target_to_position()

    def update(self, size: int, target_size: Optional[int] = None,
               source_filepath: Optional[str] = None) -> None:
        """Update position and calculate speed

        Args:
            size: Current file size in bytes
            target_size: Target size found by the latest scan, if a source was matched
            source_filepath: Source file found by the latest scan, if any
        """
        # Validate inputs
        if size < 0:
            return  # Ignore invalid size

        # The source may only become visible after the destination was first seen
        if source_filepath is not None and target_size is not None:
            self.source_filepath = source_filepath
            self.target_size = target_size

        self.position = size
        self._raise_target_to_position()

        current_time = time.monotonic()
        time_delta = current_time - self._sample_time
        if time_delta < Config.MIN_SPEED_SAMPLE_SECONDS:
            return

        sample_speed = max(size - self._sample_position, 0) / time_delta
        if sample_speed > 0:
            if self.speed > 0:
                self.speed += Config.SPEED_SMOOTHING * (sample_speed - self.speed)
            else:
                self.speed = sample_speed
            self._last_progress_time = current_time
        elif current_time - self._last_progress_time >= Config.STALL_SECONDS:
            self.speed = 0

        self.has_speed_sample = True
        self._sample_position = size
        self._sample_time = current_time

    def _raise_target_to_position(self) -> None:
        """Keep the target from falling behind what has already been written"""
        if self.target_size is not None and self.position > self.target_size:
            self.target_size = self.position

    @property
    def percent(self) -> Optional[float]:
        """Calculate percentage complete, or None if the target size is unknown"""
        if self.target_size is None:
            return None
        if self.target_size > 0:
            return min((self.position / self.target_size) * 100, 100.0)
        return 100.0

    @property
    def eta_seconds(self) -> Optional[float]:
        """Calculate ETA in seconds"""
        if self.speed > 0 and self.target_size is not None and self.target_size > self.position:
            remaining = self.target_size - self.position
            return remaining / self.speed
        return None

    @property
    def filename(self) -> str:
        """Get just the filename"""
        return os.path.basename(self.filepath)

def format_size(bytes_val: float) -> str:
    """Format bytes as human-readable size

    Args:
        bytes_val: Number of bytes to format

    Returns:
        Human-readable size string (e.g., "1.5 GiB")
    """
    for unit in ['B', 'KiB', 'MiB', 'GiB', 'TiB']:
        if bytes_val < 1024.0:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.1f} PiB"

def format_speed(bytes_per_sec: float) -> str:
    """Format bytes/sec as human-readable speed

    Args:
        bytes_per_sec: Transfer speed in bytes per second

    Returns:
        Human-readable speed string (e.g., "10.5 MiB/s")
    """
    return format_size(bytes_per_sec) + "/s"

def format_time(seconds: Optional[float]) -> str:
    """Format seconds as HH:MM:SS or MM:SS

    Args:
        seconds: Time duration in seconds, or None for unknown

    Returns:
        Formatted time string (e.g., "1:23:45" or "5:30" or "--:--")
    """
    if seconds is None:
        return "--:--"

    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"

# Compiled regex patterns for episode info extraction (compiled once at module level)
EPISODE_PATTERNS = [
    re.compile(r'[Ss](\d+)[Ee](\d+)'),  # S01E05
    re.compile(r'(?<!\d)(\d{1,2})[xX](\d{2,3})(?!\d)'),  # 1x05, but not 1920x1080
    re.compile(r'[Ss]eason\s*(\d+).*[Ee]pisode\s*(\d+)'),  # Season 1 Episode 5
]

def extract_episode_info(filename: str) -> Optional[Tuple[int, int]]:
    """Extract season/episode information from filename

    Supports common TV episode naming patterns:
    - S01E05 or s01e05 (standard format)
    - 1x05 (alternate format)
    - Season 1 Episode 5 (verbose format)

    Returns:
        Tuple of (season, episode) as integers, or None if no match
    """
    for pattern in EPISODE_PATTERNS:
        match = pattern.search(filename)
        if match:
            season = int(match.group(1))
            episode = int(match.group(2))
            return (season, episode)

    return None

def _cached_episode_info(filename: str,
                         episode_cache: Dict[str, Optional[Tuple[int, int]]]) -> Optional[Tuple[int, int]]:
    """Get episode info for a filename through the size-limited cache"""
    if filename not in episode_cache:
        if len(episode_cache) >= Config.EPISODE_CACHE_MAX_SIZE:
            # Remove oldest entry (first key)
            episode_cache.pop(next(iter(episode_cache)))
        episode_cache[filename] = extract_episode_info(filename)
    return episode_cache[filename]

def _match_by_exact_name(dest_path: str, dest_size: int,
                         read_files: List[ReadFileInfo]) -> Optional[ReadFileInfo]:
    """Try to match destination file by exact name (case-insensitive)

    Args:
        dest_path: Full path of the file being written
        dest_size: Current size of the file being written
        read_files: Files open for reading

    Returns:
        The matching source file, or None if no match
    """
    dest_lower = os.path.basename(dest_path).lower()
    candidates = [info for info in read_files
                  if os.path.basename(info.path).lower() == dest_lower
                  and info.path != dest_path and info.size >= dest_size]
    if not candidates:
        return None
    # Several same-named sources: the smallest that still fits is the closest match
    return min(candidates, key=lambda info: info.size)

def _match_by_episode_pattern(dest_path: str, read_files: List[ReadFileInfo],
                              episode_cache: Dict[str, Optional[Tuple[int, int]]]) -> Optional[ReadFileInfo]:
    """Try to match destination file by episode pattern (S01E05, etc.)

    Args:
        dest_path: Full path of the file being written
        read_files: Files open for reading
        episode_cache: Dict to cache episode info extraction results

    Returns:
        The matching source file, or None if no match
    """
    dest_ep = _cached_episode_info(os.path.basename(dest_path), episode_cache)
    if not dest_ep:
        return None

    candidates = [info for info in read_files
                  if info.path != dest_path
                  and _cached_episode_info(os.path.basename(info.path), episode_cache) == dest_ep]
    if not candidates:
        return None

    # An episode's video and subtitles share the episode number: prefer the same file type
    dest_ext = os.path.splitext(dest_path)[1].lower()
    for info in candidates:
        if os.path.splitext(info.path)[1].lower() == dest_ext:
            return info
    return candidates[0]

def _match_sole_candidate(dest_path: str, dest_size: int,
                          read_files: List[ReadFileInfo]) -> Optional[ReadFileInfo]:
    """Match a renamed destination when only one read file could be its source

    Args:
        dest_path: Full path of the file being written
        dest_size: Current size of the file being written
        read_files: Files open for reading that no other write was matched to

    Returns:
        The source file if exactly one fits, otherwise None
    """
    dest_ext = os.path.splitext(dest_path)[1].lower()
    candidates = [info for info in read_files
                  if info.path != dest_path and info.size >= dest_size
                  and os.path.splitext(info.path)[1].lower() == dest_ext]
    return candidates[0] if len(candidates) == 1 else None

def abbreviate_path(path_str: str, max_width: int, cache: Optional[Dict[Tuple[str, int], str]] = None) -> str:
    """Abbreviate a path to fit within max_width terminal columns

    Uses the wcwidth library if available for double-width character support
    (CJK, emoji, etc.), and Python's own Unicode tables otherwise. The result
    is safe to print.

    Args:
        path_str: Path string to abbreviate
        max_width: Maximum display width in columns
        cache: Optional cache dict for storing results to improve performance

    Returns:
        Abbreviated path string that fits within max_width
    """
    if max_width <= 0:
        return ""

    # Check cache first if provided
    cache_key = (path_str, max_width)
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    text = sanitize_text(path_str)
    if display_width(text) <= max_width:
        result = text
    elif max_width <= 3:
        result = "..."[:max_width]
    else:
        # Show the end of the path (filename is most important)
        result = "..." + clip_to_width(text, max_width - 3, keep_end=True)

    # Store in cache with size limit if cache provided
    if cache is not None:
        if len(cache) >= Config.PATH_CACHE_MAX_SIZE:
            # Remove oldest entry (first key)
            cache.pop(next(iter(cache)))
        cache[cache_key] = result

    return result

def should_ignore_file(filepath: str) -> bool:
    """Check if file should be ignored"""
    return os.path.splitext(filepath)[1].lower() in IGNORE_EXTENSIONS

def _identify_manager(name: str, cmdline: List[str], include_qbittorrent: bool = False) -> Optional[str]:
    """Work out which manager a process is, if any

    Args:
        name: Process name
        cmdline: Process command line
        include_qbittorrent: Also recognise qBittorrent

    Returns:
        Manager name, or None if the process is not one
    """
    if name in ARR_MANAGERS:
        return name
    if include_qbittorrent and name in QBITTORRENT_NAMES:
        return name
    if name in HOST_RUNTIMES or name.startswith('python3.'):
        managers = {manager.lower(): manager for manager in ARR_MANAGERS}
        for arg in cmdline[1:]:
            stem, ext = os.path.splitext(os.path.basename(arg))
            if ext.lower() in HOSTED_EXTENSIONS and stem.lower() in managers:
                return managers[stem.lower()]
    return None

def get_process_name(pid: int, name_cache: Optional[Dict[int, str]] = None) -> str:
    """Get the display name of a process, using the process name cache if given"""
    if name_cache is not None and pid in name_cache:
        return name_cache[pid]
    try:
        proc = psutil.Process(pid)
        name = proc.name()
        try:
            cmdline = proc.cmdline()
        except psutil.Error:
            cmdline = []
        name = _identify_manager(name, cmdline, include_qbittorrent=True) or name
    except psutil.Error:
        name = f"PID {pid}"
    if name_cache is not None:
        name_cache[pid] = name
    return name

def is_qbittorrent_process(pid: int, name_cache: Optional[Dict[int, str]] = None) -> bool:
    """Check if a PID belongs to qBittorrent, using the process name cache if given"""
    return get_process_name(pid, name_cache) in QBITTORRENT_NAMES

def get_process_start_time(pid: int) -> Optional[float]:
    """Get the start time of a running process

    The start time tells a monitored process apart from a later one that was
    given the same PID.

    Returns:
        Start time, or None if the process is gone (or is a zombie)
    """
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return None
        return proc.create_time()
    except psutil.Error:
        return None

def find_arr_processes(include_qbittorrent: bool = False) -> List[Tuple[int, str]]:
    """Find all running *arr manager processes

    Args:
        include_qbittorrent: Also detect qBittorrent processes
    """
    found = []
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            name = _identify_manager(proc.info['name'] or '', proc.info['cmdline'] or [],
                                     include_qbittorrent)
            if name:
                found.append((proc.info['pid'], name))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return found

def _parse_fdinfo(fdinfo_path: str) -> Optional[Tuple[int, int]]:
    """Parse file descriptor flags and position from fdinfo file

    Args:
        fdinfo_path: Path to the fdinfo file

    Returns:
        Tuple of (flags, position), or None if parsing failed
    """
    flags = None
    position = 0
    try:
        with open(fdinfo_path, 'r') as f:
            for line in f:
                if line.startswith('flags:'):
                    flags = int(line.split()[1], 8)
                elif line.startswith('pos:'):
                    position = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    if flags is None:
        return None
    return (flags, position)

def get_open_files(pid: int, logger: Optional[DebugLogger] = None,
                  verbose_log: bool = False,
                  episode_cache: Optional[Dict[str, Optional[Tuple[int, int]]]] = None,
                  qbittorrent: bool = False) -> Dict[str, FileTransferInfo]:
    """Get files currently being written by the process

    Args:
        pid: Process ID to scan
        logger: Optional DebugLogger instance
        verbose_log: Enable verbose debug logging
        episode_cache: Optional dict to cache episode info extraction
        qbittorrent: Process is qBittorrent - only report writes that are a
            move of a file it also has open for reading under the same name
            (downloads in progress are not transfers), and do not skip any
            file extensions since torrents can contain anything
    """
    if episode_cache is None:
        episode_cache = {}

    log = logger.log if (verbose_log and logger) else None

    open_files: Dict[str, FileTransferInfo] = {}
    read_files: List[ReadFileInfo] = []
    write_files: List[Tuple[str, str, int]] = []  # (fd, filepath, size), matched after the scan

    if log:
        log(f"=== VERBOSE SCAN of PID {pid} ===")

    fd_dir = f"/proc/{pid}/fd"
    try:
        fds = os.listdir(fd_dir)
    except OSError as e:
        if log:
            log(f"  Error scanning PID {pid}: {e}")
        return {}

    # Collect all FD info and categorize; writes are matched to sources afterwards
    for fd in fds:
        fd_link = f"{fd_dir}/{fd}"
        try:
            # Stat through the link itself rather than the path it names: the
            # path belongs to the process's own mount namespace (containers)
            # and stops existing once the file is unlinked or renamed over
            stat_info = os.stat(fd_link)
            if not stat.S_ISREG(stat_info.st_mode):
                continue
            filepath = os.readlink(fd_link)
        except OSError as e:
            if log:
                log(f"  FD {fd}: Skipped - {e}")
            continue

        if filepath.endswith(DELETED_SUFFIX):
            filepath = filepath[:-len(DELETED_SUFFIX)]
        file_size = stat_info.st_size

        fdinfo = _parse_fdinfo(f"/proc/{pid}/fdinfo/{fd}")
        if fdinfo is None:
            if log:
                log(f"  FD {fd}: Skipped - could not read fdinfo flags")
            continue
        flags, position = fdinfo

        # Extract access mode from flags (O_RDONLY=0, O_WRONLY=1, O_RDWR=2)
        access_mode = flags & ACCESS_MODE_MASK

        if log:
            log(f"  FD {fd}: {filepath}")
            log(f"    size={file_size} pos={position} flags={oct(flags)} mode={access_mode}")

        if not qbittorrent and should_ignore_file(filepath):
            if log:
                log("    Skipped: ignored extension")
            continue

        if access_mode == ACCESS_MODE_READ:
            read_files.append(ReadFileInfo(size=file_size, path=filepath))
        elif access_mode in (ACCESS_MODE_WRITE, ACCESS_MODE_READWRITE):
            write_files.append((fd, filepath, file_size))

    # Match by name first, so that a guess for a renamed file cannot take a
    # source that plainly belongs to another write
    matches: Dict[int, Tuple[ReadFileInfo, str]] = {}
    for index, (fd, filepath, file_size) in enumerate(write_files):
        source = _match_by_exact_name(filepath, file_size, read_files)
        method = "exact"
        if source is None and not qbittorrent:
            source = _match_by_episode_pattern(filepath, read_files, episode_cache)
            method = "episode"
        if source is not None:
            matches[index] = (source, method)

    claimed = {source.path for source, _ in matches.values()}

    for index, (fd, filepath, file_size) in enumerate(write_files):
        filename = os.path.basename(filepath)
        source, method = matches.get(index, (None, "unknown"))

        if source is None and qbittorrent:
            if log:
                log(f"  Write file: {filename} - skipped, no source open (not a move)")
            continue

        if source is None:
            source = _match_sole_candidate(
                filepath, file_size, [info for info in read_files if info.path not in claimed])
            if source is not None:
                method = "sole candidate"
                claimed.add(source.path)

        # The target is left unknown rather than guessed when nothing matches
        target_size = max(source.size, 1) if source else None
        source_path = source.path if source else None

        if log:
            matched_source = os.path.basename(source_path) if source_path else "none"
            log(f"  Write file: {filename}")
            log(f"    current={file_size} target={target_size} match={method} source={matched_source}")

        key = f"{fd}_{filepath}"
        open_files[key] = FileTransferInfo(fd, filepath, file_size, target_size, source_path)

    if log:
        log(f"  Result: {len(open_files)} writable files, {len(read_files)} read files")
    return open_files

def check_process_access(pid: int) -> Optional[str]:
    """Check that the open files of a process can be listed

    Returns:
        None if they can, otherwise a short description of the problem
    """
    # A zombie's fd directory also reports "permission denied"
    if get_process_start_time(pid) is None:
        return "process no longer exists"
    try:
        os.listdir(f"/proc/{pid}/fd")
    except PermissionError:
        return "permission denied"
    except OSError:
        return "process no longer exists"
    return None

def select_process_interactive(include_qbittorrent: bool = False) -> Optional[List[int]]:
    """Interactive process selection"""
    processes = find_arr_processes(include_qbittorrent)

    if not processes:
        print("No *arr processes found running.")
        print(f"\nAvailable managers: {', '.join(ARR_MANAGERS)}")
        print("\nYou can also monitor a specific PID:")
        print("  arr-monitor.py <PID>")
        return None

    if len(processes) == 1:
        pid, name = processes[0]
        print(f"Found: {name} (PID: {pid})")
        return [pid]

    print(f"Found {len(processes)} *arr process(es):\n")
    for i, (pid, name) in enumerate(processes, 1):
        print(f"  {i}. {name} (PID: {pid})")
    print("  A. Monitor all")

    while True:
        try:
            choice = input(f"\nSelect process to monitor [1-{len(processes)}/A]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if choice.upper() == 'A':
            return [pid for pid, name in processes]
        try:
            idx = int(choice) - 1
        except ValueError:
            idx = -1
        if 0 <= idx < len(processes):
            return [processes[idx][0]]
        print("Invalid selection")

_colors_enabled = False

def init_colors() -> None:
    """Set up colours if the terminal has them and NO_COLOR is not set"""
    global _colors_enabled
    _colors_enabled = False
    if os.environ.get('NO_COLOR'):
        return
    try:
        if not curses.has_colors():
            return
        curses.start_color()
        try:
            # Keep the terminal's own background instead of forcing black
            curses.use_default_colors()
            background = -1
        except curses.error:
            background = curses.COLOR_BLACK
        for pair, foreground in COLOR_PAIRS.items():
            curses.init_pair(pair, foreground, background)
        _colors_enabled = True
    except curses.error:
        pass

def color(pair: int) -> int:
    """Attribute for a colour pair, or no attribute when colours are off"""
    return curses.color_pair(pair) if _colors_enabled else 0

def _put(stdscr: CursesWindow, row: int, col: int, text: str, attr: int = 0) -> None:
    """Write text at a position, cut to the screen width and made safe to print

    A failed write only loses this piece of text, never the rest of the frame.
    """
    try:
        height, width = stdscr.getmaxyx()
        if not (0 <= row < height) or not (0 <= col < width - 1):
            return
        stdscr.addstr(row, col, clip_to_width(sanitize_text(text), width - 1 - col), attr)
    except curses.error:
        pass

def _refresh(stdscr: CursesWindow) -> None:
    try:
        stdscr.noutrefresh()
        curses.doupdate()
    except curses.error:
        pass

def draw_message(stdscr: CursesWindow, lines: List[Tuple[str, int]]) -> None:
    """Replace the screen contents with a few lines of text

    Args:
        stdscr: Curses screen object for rendering
        lines: (text, attribute) for each line, from the top
    """
    stdscr.erase()
    for row, (text, attr) in enumerate(lines):
        _put(stdscr, row, 0, text, attr)
    _refresh(stdscr)

def draw_ui(stdscr: CursesWindow, pid_list: List[int], tracked_files: Dict[Tuple[int, str], FileTransferInfo],
           path_cache: Optional[Dict[Tuple[str, int], str]] = None,
           proc_name_cache: Optional[Dict[int, str]] = None) -> None:
    """Draw the curses UI with file transfer progress

    Renders a terminal UI showing active file transfers with progress bars,
    transfer speeds, ETAs, and source/destination paths. Handles terminal
    resize gracefully and uses caching for performance.

    Args:
        stdscr: Curses screen object for rendering
        pid_list: List of process IDs being monitored
        tracked_files: Dictionary mapping (pid, file_key) to FileTransferInfo
        path_cache: Optional cache for abbreviated paths to improve render performance
        proc_name_cache: Optional cache for process names to avoid repeated psutil calls
    """
    if path_cache is None:
        path_cache = {}
    if proc_name_cache is None:
        proc_name_cache = {}

    try:
        height, width = stdscr.getmaxyx()
    except curses.error:
        # Terminal might be in an invalid state during resize
        return

    # Ensure minimum dimensions
    if height < Config.MIN_TERMINAL_HEIGHT or width < Config.MIN_TERMINAL_WIDTH:
        draw_message(stdscr, [("Terminal too small", 0)])
        return

    stdscr.erase()

    if len(pid_list) == 1:
        pid = pid_list[0]
        proc_name = f"{get_process_name(pid, proc_name_cache)} (PID: {pid})"
    else:
        proc_name = f"Monitoring {len(pid_list)} processes"

    _put(stdscr, 0, 0, f"*arr File Transfer Monitor - {proc_name}", curses.A_BOLD | color(COLOR_TITLE))
    _put(stdscr, 1, 0, f"Time: {datetime.now().strftime('%H:%M:%S')}")
    _put(stdscr, 2, 0, "─" * min(width - 1, 80))

    footer = "Press 'q' to quit"
    footer_row = height - 1

    if not tracked_files:
        _put(stdscr, min(Config.FIRST_ENTRY_ROW, footer_row - 1), 0,
             "No active file writes detected...", color(COLOR_IDLE))
        _put(stdscr, footer_row, 0, footer)
        _refresh(stdscr)
        return

    indent = " " * Config.PATH_INDENT
    path_width = width - 1 - Config.PATH_INDENT
    # Room for "  [" + bar + "] 100.0%"
    bar_width = max(0, min(Config.MAX_PROGRESS_BAR_WIDTH, width - 1 - len("  [] 100.0%")))

    row = Config.FIRST_ENTRY_ROW
    drawn = 0
    for (pid, file_key), file_info in tracked_files.items():
        # Header, optional source, destination, progress bar, size
        rows_needed = 5 if file_info.source_filepath else 4
        if row + rows_needed > footer_row:
            break

        # Green line: [ProcessName] filename
        header = f"[{get_process_name(pid, proc_name_cache)}] {file_info.filename}"
        _put(stdscr, row, 0, header, curses.A_BOLD | color(COLOR_NAME))
        row += 1

        # Red line: source path (indented)
        if file_info.source_filepath:
            source_display = indent + abbreviate_path(file_info.source_filepath, path_width, path_cache)
            _put(stdscr, row, 0, source_display, color(COLOR_SOURCE))
            row += 1

        # Blue line: destination path (indented)
        dest_display = indent + abbreviate_path(file_info.filepath, path_width, path_cache)
        _put(stdscr, row, 0, dest_display, color(COLOR_DEST))
        row += 1

        percent = file_info.percent
        if percent is None:
            _put(stdscr, row, 0, "  (total size unknown)", color(COLOR_BAR))
            size_str = f"  {format_size(file_info.position)} written"
        else:
            if file_info.position < file_info.target_size:
                # Never round an unfinished transfer up to 100%
                percent = min(percent, 99.9)
            filled = int((percent / 100) * bar_width)
            bar = "█" * filled + "░" * (bar_width - filled)
            _put(stdscr, row, 0, f"  [{bar}] {percent:.1f}%", color(COLOR_BAR))
            size_str = f"  {format_size(file_info.position)} / {format_size(file_info.target_size)}"
        row += 1

        _put(stdscr, row, 0, size_str)

        if file_info.has_speed_sample:
            speed = format_speed(file_info.speed)
            eta = format_time(file_info.eta_seconds)
            if percent is None:
                # No ETA without a target
                forms = (f"  Speed: {speed}", f"  {speed}")
            else:
                forms = (f"  Speed: {speed}  ETA: {eta}", f"  {speed}  {eta}")
            # Fall back to a shorter form on narrow terminals
            for info in forms:
                if len(size_str) + len(info) <= width - 1:
                    _put(stdscr, row, len(size_str), info, color(COLOR_SPEED))
                    break

        # Blank line between entries
        row += 2
        drawn += 1

    hidden = len(tracked_files) - drawn
    if hidden > 0:
        footer += f"  (+{hidden} more not shown)"
    _put(stdscr, footer_row, 0, footer)

    _refresh(stdscr)

def _quit_requested(stdscr: CursesWindow) -> bool:
    """Wait up to one poll interval for a keypress and check for 'q'

    Returns early on any key or terminal resize, so both are handled at once
    instead of on the next poll.
    """
    stdscr.timeout(int(Config.POLL_INTERVAL_SECONDS * 1000))
    key = stdscr.getch()
    try:
        # Take everything that is already waiting, not one key per poll
        stdscr.nodelay(True)
        while key != -1:
            if key in (ord('q'), ord('Q')):
                return True
            key = stdscr.getch()
    finally:
        stdscr.timeout(int(Config.POLL_INTERVAL_SECONDS * 1000))
    return False

def run_monitor(
    stdscr: CursesWindow,
    pid_list: List[int],
    logger: Optional[DebugLogger] = None,
    add_new_processes: bool = False,
    include_qbittorrent: bool = False,
) -> None:
    """Main monitoring loop with curses UI

    Continuously polls the specified processes for open file descriptors,
    tracks file write progress, and displays real-time updates in a curses UI.
    Handles terminal resizing, process exits, and user input (q to quit).

    Args:
        stdscr: Curses screen object for rendering the UI
        pid_list: List of process IDs to monitor for file operations
        logger: Optional DebugLogger instance for debug output
        add_new_processes: When True, periodically scan for new *arr processes
        include_qbittorrent: When True, new process scans also pick up qBittorrent
    """
    if logger:
        logger.log(f"run_monitor started with PIDs: {pid_list}")

    init_colors()
    try:
        curses.curs_set(0)
    except curses.error:
        # Terminal cannot hide the cursor
        pass

    pid_list = list(pid_list)
    tracked_files: Dict[Tuple[int, str], FileTransferInfo] = {}
    iteration = 0
    episode_cache: Dict[str, Optional[Tuple[int, int]]] = {}  # Cache for episode info extraction
    path_abbreviation_cache: Dict[Tuple[str, int], str] = {}  # (path, width) -> abbreviated_path
    process_name_cache: Dict[int, str] = {}  # Cache for process names: pid -> name
    process_start_times: Dict[int, float] = {}  # pid -> start time, to notice reused PIDs
    last_terminal_width = 0  # Track terminal width to detect resizes
    verbose_this_iteration = False

    while True:
        try:
            iteration += 1

            # Drop processes that exited, including ones whose PID now belongs
            # to a different process
            for pid in list(pid_list):
                start_time = get_process_start_time(pid)
                if start_time is not None and process_start_times.setdefault(pid, start_time) == start_time:
                    continue
                pid_list.remove(pid)
                process_start_times.pop(pid, None)
                process_name_cache.pop(pid, None)
                if logger:
                    logger.log(f"Process {pid} exited")

            if add_new_processes:
                if iteration % Config.NEW_PROCESS_SCAN_INTERVAL == 0 or not pid_list:
                    processes = find_arr_processes(include_qbittorrent)
                    new_pids = [pid for pid, _ in processes if pid not in pid_list]
                    if new_pids:
                        pid_list.extend(new_pids)
                        if logger:
                            logger.log(f"Added {len(new_pids)} new process(es): {new_pids}")
                if not pid_list:
                    tracked_files.clear()
                    draw_message(stdscr, [
                        ("Waiting for *arr processes to start...", curses.A_BOLD),
                        ("Press 'q' to quit", curses.A_DIM),
                    ])
                    if _quit_requested(stdscr):
                        break
                    continue
            elif not pid_list:
                if logger:
                    logger.log("All processes exited")
                stdscr.timeout(-1)
                while True:
                    draw_message(stdscr, [
                        ("All monitored processes have exited.", curses.A_BOLD),
                        ("Press any key to exit...", 0),
                    ])
                    # A terminal resize is not a keypress
                    if stdscr.getch() != curses.KEY_RESIZE:
                        break
                break

            # Only do verbose logging on first iteration or periodically
            # to avoid duplicate scanning overhead
            verbose_this_iteration = bool(
                (iteration == 1 or iteration % Config.VERBOSE_LOG_INTERVAL == 0)
                and logger and logger.is_enabled)

            if verbose_this_iteration:
                logger.log(f"=== Scan iteration {iteration} ===")

            current_files = {}
            for pid in pid_list:
                pid_files = get_open_files(pid, logger=logger, verbose_log=verbose_this_iteration,
                                           episode_cache=episode_cache,
                                           qbittorrent=is_qbittorrent_process(pid, process_name_cache))
                for file_key, file_info in pid_files.items():
                    current_files[(pid, file_key)] = file_info

            if verbose_this_iteration:
                logger.log(f"Total files found across all PIDs: {len(current_files)}")

            for file_key, file_info in current_files.items():
                tracked = tracked_files.get(file_key)
                if tracked is None:
                    tracked_files[file_key] = file_info
                    if logger:
                        logger.log(f"  New file tracked: {file_info.filename} at {file_info.position}/{file_info.target_size}")
                else:
                    old_pos = tracked.position
                    tracked.update(file_info.position, file_info.target_size, file_info.source_filepath)
                    if verbose_this_iteration and old_pos != tracked.position:
                        logger.log(f"  Updated: {file_info.filename} {old_pos} -> {tracked.position}")

            keys_to_remove = [k for k in tracked_files if k not in current_files]
            for file_key in keys_to_remove:
                if logger:
                    logger.log(f"  File closed: {tracked_files[file_key].filename}")
                del tracked_files[file_key]

            # Handle terminal resize by clearing path cache
            # Path abbreviations are width-dependent, so we need to recalculate them
            try:
                current_width = stdscr.getmaxyx()[1]
                if current_width != last_terminal_width:
                    path_abbreviation_cache.clear()
                    last_terminal_width = current_width
                    if logger:
                        logger.log(f"Terminal resized to width {current_width}, cleared path cache")
            except curses.error:
                # Terminal might be in invalid state during resize
                pass

            draw_ui(stdscr, pid_list, tracked_files, path_abbreviation_cache, process_name_cache)

            if _quit_requested(stdscr):
                if logger:
                    logger.log("User quit")
                break

        except KeyboardInterrupt:
            if logger:
                logger.log("KeyboardInterrupt")
            break
        except curses.error as e:
            # Handle curses-specific errors (terminal resize, etc.)
            if logger and verbose_this_iteration:
                logger.log(f"Curses error: {e}")
            time.sleep(Config.POLL_INTERVAL_SECONDS)
            continue
        except psutil.NoSuchProcess as e:
            # Process disappeared during monitoring
            if logger:
                logger.log(f"Process no longer exists: {e}")
            # Will be dropped by the liveness check on next iteration
            time.sleep(Config.POLL_INTERVAL_SECONDS)
            continue
        except OSError as e:
            # File system errors (permission denied, file not found, etc.)
            if logger:
                logger.log(f"File system error: {e}")
            time.sleep(Config.POLL_INTERVAL_SECONDS)
            continue
        except Exception as e:
            # Unexpected errors - log and re-raise for debugging
            if logger:
                logger.log(f"Unexpected error: {type(e).__name__}: {e}")
            raise

def _raise_keyboard_interrupt(signum, frame):
    """Turn a termination signal into the normal Ctrl-C exit path"""
    raise KeyboardInterrupt

def main() -> int:
    parser = argparse.ArgumentParser(
        description='Monitor file write operations for *arr media managers',
        epilog='Examples:\n'
               '  %(prog)s              # Interactive process selection\n'
               '  %(prog)s -a           # Monitor all detected *arr processes\n'
               '  %(prog)s -a -A        # Monitor all and keep adding new processes\n'
               '  %(prog)s -a -q        # Monitor all, including qBittorrent moves\n'
               '  %(prog)s 1234         # Monitor specific PID\n'
               '  %(prog)s 1234 5678    # Monitor multiple PIDs\n'
               '  %(prog)s --debug 1234 # Show debug info for PID',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('pids', type=int, nargs='*',
                       help='Process ID(s) to monitor')
    parser.add_argument('-d', '--debug', action='store_true',
                       help='Show debug information')
    parser.add_argument('--log', type=str, metavar='FILE',
                       help='Enable debug logging to specified file')
    parser.add_argument('-a', '--all', action='store_true',
                       help='Automatically monitor all detected *arr processes '
                            '(in addition to any PIDs given)')
    parser.add_argument('-A', '--add-new-processes', action='store_true',
                       help='With --all, keep monitoring for newly started *arr processes')
    parser.add_argument('-q', '--qbittorrent', action='store_true',
                       help='Also detect qBittorrent and track its moves of completed '
                            'downloads to their final folder')

    args = parser.parse_args()

    if args.add_new_processes and not args.all:
        parser.error("--add-new-processes requires --all")

    # Create logger context manager
    logger = DebugLogger(args.log) if args.log else DebugLogger()

    pids = list(dict.fromkeys(args.pids))
    for pid in pids:
        if not psutil.pid_exists(pid):
            print(f"Error: Process {pid} does not exist", file=sys.stderr)
            return 1

    if args.all:
        processes = [(pid, name) for pid, name in find_arr_processes(args.qbittorrent)
                     if pid not in pids]
        if processes:
            print(f"Auto-detected {len(processes)} process(es):")
            for pid, name in processes:
                print(f"  - {name} (PID: {pid})")
            pids.extend(pid for pid, name in processes)
        elif not pids:
            if not args.add_new_processes:
                print("No *arr processes found running.", file=sys.stderr)
                print(f"\nAvailable managers: {', '.join(ARR_MANAGERS)}", file=sys.stderr)
                return 1
            print("No *arr processes found running. Waiting for new processes...")
    elif not pids:
        pids = select_process_interactive(args.qbittorrent)
        if pids is None:
            return 1

    if args.pids:
        if len(args.pids) == 1:
            name = sanitize_text(get_process_name(args.pids[0]))
            print(f"Monitoring: {name} (PID: {args.pids[0]})")
        else:
            print(f"Monitoring {len(args.pids)} processes: {', '.join(map(str, args.pids))}")

    # Leave out processes whose open files cannot be read instead of
    # silently showing nothing for them
    accessible = []
    permission_denied = False
    for pid in pids:
        problem = check_process_access(pid)
        if problem is None:
            accessible.append(pid)
            continue
        print(f"Warning: Skipping PID {pid}: {problem}", file=sys.stderr)
        permission_denied = permission_denied or problem == "permission denied"
    if permission_denied:
        print("\nTry running with sudo:", file=sys.stderr)
        print(f"  sudo {shlex.join(sys.argv)}", file=sys.stderr)
    if pids and not accessible and not args.add_new_processes:
        return 1
    pids = accessible

    if args.debug:
        # Use logger context manager even in debug mode
        with logger:
            for pid in pids:
                print(f"\nDebug: Scanning /proc/{pid}/fd/...")
                files = get_open_files(pid, logger=logger, verbose_log=True,
                                       qbittorrent=is_qbittorrent_process(pid))
                if not files:
                    print("No files found matching criteria")
                else:
                    print(f"\nFound {len(files)} file(s) being written:")
                    for info in files.values():
                        target = info.target_size if info.target_size is not None else "unknown"
                        percent = f"{info.percent:.1f}%" if info.percent is not None else "unknown"
                        print(f"\n  FD {info.fd}: {sanitize_text(info.filepath)}")
                        if info.source_filepath:
                            print(f"    Source: {sanitize_text(info.source_filepath)}")
                        print(f"    Position: {info.position}, Target: {target}")
                        print(f"    Percent: {percent}")
        return 0

    # Leave through the normal exit path on termination, so that curses
    # restores the terminal
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    signal.signal(signal.SIGHUP, _raise_keyboard_interrupt)

    # Use logger as context manager
    with logger:
        logger.log(f"Starting arr-monitor with args: {sys.argv}")

        try:
            logger.log("Starting curses interface")
            curses.wrapper(run_monitor, pids, logger, args.add_new_processes, args.qbittorrent)
            logger.log("Curses interface exited normally")
        except KeyboardInterrupt:
            logger.log("Interrupted by user")
        except Exception as e:
            logger.log(f"Error in curses interface: {e}")
            raise

        logger.log("Exiting")

    if logger.error:
        print(f"Warning: Debug logging to {args.log} stopped early: {logger.error}", file=sys.stderr)

    return 0

if __name__ == '__main__':
    sys.exit(main())
