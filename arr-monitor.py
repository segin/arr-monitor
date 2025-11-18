#!/usr/bin/env python3

"""
*arr Media Manager File Transfer Monitor
Monitors file write operations for Sonarr, Radarr, Lidarr, etc.
"""

import os
import sys
import time
import curses
import psutil
import argparse
import re
import platform
from pathlib import Path
from datetime import datetime
from threading import Lock

# Check for Linux early
if platform.system() != 'Linux':
    print(f"Error: This tool requires Linux (uses /proc filesystem).", file=sys.stderr)
    print(f"Detected OS: {platform.system()}", file=sys.stderr)
    sys.exit(1)

# Try to import wcwidth for proper double-width character handling
try:
    from wcwidth import wcswidth
    HAS_WCWIDTH = True
except ImportError:
    HAS_WCWIDTH = False

class DebugLogger:
    """Thread-safe debug logger with context manager support"""
    def __init__(self, filepath=None):
        self.filepath = filepath
        self.file_handle = None
        self.lock = Lock()
    
    def __enter__(self):
        """Open log file when entering context"""
        if self.filepath:
            try:
                self.file_handle = open(self.filepath, 'w')
                self.file_handle.write(f"=== *arr Monitor Debug Log - {datetime.now()} ===\n\n")
                self.file_handle.flush()
            except OSError as e:
                print(f"Warning: Could not create debug log at {self.filepath}: {e}")
                self.file_handle = None
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Close log file when exiting context"""
        if self.file_handle:
            try:
                self.file_handle.close()
            except OSError:
                pass
        return False
    
    def log(self, message):
        """Write debug message to log file"""
        if self.file_handle:
            with self.lock:
                try:
                    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                    self.file_handle.write(f"[{timestamp}] {message}\n")
                    self.file_handle.flush()
                except OSError:
                    # Silently ignore logging errors
                    pass
    
    @property
    def is_enabled(self):
        """Check if logging is enabled"""
        return self.file_handle is not None

# Media manager process names to auto-detect
ARR_MANAGERS = [
    'Sonarr', 'Radarr', 'Lidarr', 'Readarr', 
    'Prowlarr', 'Bazarr', 'Whisparr'
]

# File extensions to ignore (databases, logs, etc.)
IGNORE_EXTENSIONS = {
    '.db', '.db-wal', '.db-shm', '.db-journal',
    '.log', '.txt', '.xml', '.json', '.conf',
    '.zip', '.dll'
}

# Constants for configuration
POLL_INTERVAL_SECONDS = 0.5
VERBOSE_LOG_INTERVAL = 100  # Log verbosely every N iterations
TARGET_SIZE_EXPANSION_THRESHOLD = 1.1  # Expand target size if file exceeds by this factor
MIN_PROGRESS_BAR_WIDTH = 40
PROGRESS_BAR_PADDING = 20
MIN_TERMINAL_HEIGHT = 5
MIN_TERMINAL_WIDTH = 20
EPISODE_CACHE_MAX_SIZE = 1000  # Maximum entries in episode info cache
EPISODE_CACHE_MAX_SIZE = 1000  # Maximum entries in episode cache before clearing

# File access modes from open() flags
ACCESS_MODE_READ = 0
ACCESS_MODE_WRITE = 1
ACCESS_MODE_READWRITE = 2

class FileTransferInfo:
    """Tracks information about a file being written"""
    def __init__(self, fd, filepath, position, size, target_size=None, source_filepath=None):
        self.fd = fd
        self.filepath = filepath
        self.source_filepath = source_filepath  # Store the source file path
        self.position = position
        self.size = size
        self.target_size = target_size if target_size is not None else size
        self.initial_target = self.target_size
        self.last_position = position
        self.last_time = time.time()
        self.speed = 0
        self.first_seen = time.time()
        
    def update(self, position, size):
        """Update position, size, and calculate speed"""
        current_time = time.time()
        time_delta = current_time - self.last_time
        
        actual_position = size
        
        # Only expand target if position significantly exceeds it
        if actual_position > self.target_size * TARGET_SIZE_EXPANSION_THRESHOLD:
            self.target_size = actual_position
        
        if time_delta > 0:
            bytes_written = actual_position - self.last_position
            if bytes_written > 0:
                self.speed = bytes_written / time_delta
        
        self.last_position = actual_position
        self.position = actual_position
        self.size = size
        self.last_time = current_time
    
    @property
    def percent(self):
        """Calculate percentage complete"""
        if self.target_size > 0:
            pct = (self.position / self.target_size) * 100
            return min(pct, 100.0)
        return 0
    
    @property
    def eta_seconds(self):
        """Calculate ETA in seconds"""
        if self.speed > 0 and self.target_size > self.position:
            remaining = self.target_size - self.position
            return remaining / self.speed
        return None
    
    @property
    def filename(self):
        """Get just the filename"""
        return os.path.basename(self.filepath)

def format_size(bytes_val):
    """Format bytes as human-readable size"""
    for unit in ['B', 'KiB', 'MiB', 'GiB', 'TiB']:
        if bytes_val < 1024.0:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.1f} PiB"

def format_speed(bytes_per_sec):
    """Format bytes/sec as human-readable speed"""
    return format_size(bytes_per_sec) + "/s"

def format_time(seconds):
    """Format seconds as HH:MM:SS or MM:SS"""
    if seconds is None:
        return "--:--"
    
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"

def extract_episode_info(filename):
    """Extract season/episode information from filename
    
    Supports common TV episode naming patterns:
    - S01E05 or s01e05 (standard format)
    - 1x05 (alternate format)
    - Season 1 Episode 5 (verbose format)
    
    Returns:
        Tuple of (season, episode) as integers, or None if no match
    """
    patterns = [
        r'[Ss](\d+)[Ee](\d+)',  # S01E05
        r'(\d+)[xX](\d+)',  # 1x05
        r'[Ss]eason\s*(\d+).*[Ee]pisode\s*(\d+)',  # Season 1 Episode 5
    ]
    
    for pattern in patterns:
        match = re.search(pattern, filename)
        if match:
            season = int(match.group(1))
            episode = int(match.group(2))
            return (season, episode)
    
    return None

def find_matching_source(dest_filename, read_files, episode_cache):
    """Find the best matching source file for a destination
    
    Args:
        dest_filename: Destination filename to match
        read_files: Dict of {filename: size}
        episode_cache: Dict to cache episode info extraction results
    """
    dest_lower = dest_filename.lower()
    
    # Try exact match first (case-insensitive)
    for src_name, src_size in read_files.items():
        if src_name.lower() == dest_lower:
            return src_size
    
    # Try episode pattern matching with caching
    if dest_filename not in episode_cache:
        # Enforce cache size limit using LRU-style eviction
        if len(episode_cache) >= EPISODE_CACHE_MAX_SIZE:
            # Remove oldest entry (first key in dict - Python 3.7+ maintains insertion order)
            episode_cache.pop(next(iter(episode_cache)))
        episode_cache[dest_filename] = extract_episode_info(dest_filename)
    
    dest_ep = episode_cache[dest_filename]
    if dest_ep:
        for src_name, src_size in read_files.items():
            if src_name not in episode_cache:
                # Enforce cache size limit
                if len(episode_cache) >= EPISODE_CACHE_MAX_SIZE:
                    episode_cache.pop(next(iter(episode_cache)))
                episode_cache[src_name] = extract_episode_info(src_name)
            
            if episode_cache[src_name] == dest_ep:
                return src_size
    
    return None

# Cache for abbreviated paths to avoid recalculating on every render
_path_abbreviation_cache = {}
PATH_CACHE_MAX_SIZE = 500  # Limit cache size

def abbreviate_path(path_str, max_width):
    """Abbreviate a path to fit within max_width characters
    
    Automatically uses wcwidth library if available for proper double-width
    character support (CJK, emoji, etc.). Falls back to simple character
    counting for ASCII/Latin text.
    
    Results are cached to improve performance during repeated renders.
    """
    if max_width <= 0:
        return ""
    
    # Check cache first
    cache_key = (path_str, max_width)
    if cache_key in _path_abbreviation_cache:
        return _path_abbreviation_cache[cache_key]
    
    # Calculate abbreviated path
    if HAS_WCWIDTH:
        # Use proper display width calculation
        try:
            actual_width = wcswidth(path_str)
            if actual_width < 0:  # Contains non-printable characters
                actual_width = len(path_str)
        except (TypeError, ValueError):
            # Fallback if wcswidth fails on unexpected input
            actual_width = len(path_str)
        
        if actual_width <= max_width:
            result = path_str
        elif max_width <= 3:
            result = "..."[:max_width]
        else:
            # Try to show the end of the path (filename is most important)
            result = "..."
            for i in range(len(path_str)):
                truncated = "..." + path_str[i:]
                try:
                    trunc_width = wcswidth(truncated)
                    if trunc_width < 0:
                        trunc_width = len(truncated)
                except (TypeError, ValueError):
                    trunc_width = len(truncated)
                if trunc_width <= max_width:
                    result = truncated
                    break
    else:
        # Fallback: simple character counting (works for ASCII/Latin)
        if len(path_str) <= max_width:
            result = path_str
        elif max_width > 3:
            result = "..." + path_str[-(max_width-3):]
        else:
            result = path_str[:max_width]
    
    # Store in cache with size limit
    if len(_path_abbreviation_cache) >= PATH_CACHE_MAX_SIZE:
        # Remove oldest entry (first key)
        _path_abbreviation_cache.pop(next(iter(_path_abbreviation_cache)))
    _path_abbreviation_cache[cache_key] = result
    
    return result

def should_ignore_file(filepath):
    """Check if file should be ignored"""
    path = Path(filepath)
    return path.suffix.lower() in IGNORE_EXTENSIONS

def find_arr_processes():
    """Find all running *arr manager processes"""
    found = []
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            name = proc.info['name']
            if name in ARR_MANAGERS:
                found.append((proc.info['pid'], name))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return found

def get_open_files(pid, logger=None, verbose_log=False, episode_cache=None):
    """Get files currently being written by the process
    
    Args:
        pid: Process ID to scan
        logger: Optional DebugLogger instance
        verbose_log: Enable verbose debug logging
        episode_cache: Optional dict to cache episode info extraction
    """
    if episode_cache is None:
        episode_cache = {}
    
    open_files = {}
    read_files = {}
    
    if verbose_log and logger:
        logger.log(f"=== VERBOSE SCAN of PID {pid} ===")
    
    try:
        fd_dir = Path(f"/proc/{pid}/fd")
        fdinfo_dir = Path(f"/proc/{pid}/fdinfo")
        
        if not fd_dir.exists() or not fdinfo_dir.exists():
            if verbose_log and logger:
                logger.log(f"  /proc/{pid}/fd or fdinfo does not exist")
            return {}
        
        # Single pass: collect all FD info and categorize immediately
        for fd_link in fd_dir.iterdir():
            fd = fd_link.name
            try:
                # Check fdinfo exists early before expensive operations
                fdinfo_path = fdinfo_dir / fd
                if not fdinfo_path.exists():
                    if verbose_log and logger:
                        logger.log(f"  FD {fd}: Skipped - no fdinfo")
                    continue
                
                # Resolve symlink with early error handling
                try:
                    filepath = fd_link.resolve()
                except (OSError, RuntimeError) as e:
                    if verbose_log and logger:
                        logger.log(f"  FD {fd}: Skipped - could not resolve: {e}")
                    continue
                
                if verbose_log and logger:
                    logger.log(f"  FD {fd}: {filepath}")
                
                # Check if regular file and get size - combined to reduce syscalls
                try:
                    stat_info = filepath.stat()
                    if not filepath.is_file():
                        if verbose_log and logger:
                            logger.log(f"    Skipped: not a regular file")
                        continue
                    file_size = stat_info.st_size
                except (OSError, FileNotFoundError) as e:
                    if verbose_log and logger:
                        logger.log(f"    Skipped: could not stat - {e}")
                    continue
                
                position = 0
                flags = 0
                
                try:
                    with open(fdinfo_path, 'r') as f:
                        for line in f:
                            if line.startswith('pos:'):
                                position = int(line.split()[1])
                            elif line.startswith('flags:'):
                                flags = int(line.split()[1], 8)
                except (OSError, ValueError) as e:
                    if verbose_log and logger:
                        logger.log(f"    Skipped: could not read fdinfo - {e}")
                    continue
                
                # Extract access mode from flags (O_RDONLY=0, O_WRONLY=1, O_RDWR=2)
                access_mode = flags & 0o3
                
                if verbose_log and logger:
                    logger.log(f"    size={file_size} pos={position} flags={oct(flags)} mode={access_mode}")
                
                # Categorize immediately
                if access_mode == ACCESS_MODE_READ:
                    # Read file
                    filename = filepath.name
                    read_files[filename] = (file_size, str(filepath))
                    if verbose_log and logger:
                        logger.log(f"  Read file: {filename} ({file_size} bytes)")
                
                elif access_mode in (ACCESS_MODE_WRITE, ACCESS_MODE_READWRITE):
                    # Write file - check if should be ignored
                    if should_ignore_file(str(filepath)):
                        if verbose_log and logger:
                            logger.log(f"    Skipped: ignored extension")
                        continue
                    
                    filename = filepath.name
                    current_pos = file_size
                    
                    # Find matching source with caching
                    match_result = find_matching_source(filename, {k: v[0] for k, v in read_files.items()}, episode_cache)
                    
                    target_size = match_result
                    source_path = None
                    match_method = "none"
                    matched_source = "none"
                    
                    if target_size is not None:
                        match_method = "pattern/exact"
                        for src_name, (src_size, src_path) in read_files.items():
                            if src_size == target_size:
                                matched_source = src_name
                                source_path = src_path
                                break
                    elif read_files:
                        target_size = max(v[0] for v in read_files.values())
                        match_method = "largest"
                        for src_name, (src_size, src_path) in read_files.items():
                            if src_size == target_size:
                                matched_source = src_name
                                source_path = src_path
                                break
                    else:
                        target_size = max(file_size, 1)
                        match_method = "fallback"
                    
                    if verbose_log and logger:
                        logger.log(f"  Write file: {filename}")
                        logger.log(f"    current={file_size} target={target_size} match={match_method} source={matched_source}")
                    
                    key = f"{fd}_{filepath}"
                    open_files[key] = FileTransferInfo(fd, str(filepath), current_pos, file_size, target_size, source_path)
            
            except (PermissionError, OSError) as e:
                if verbose_log and logger:
                    logger.log(f"  FD {fd_link.name}: Error - {e}")
                continue
        
        if verbose_log and logger:
            logger.log(f"  Result: {len(open_files)} writable files, {len(read_files)} read files")
        return open_files
    
    except (PermissionError, OSError) as e:
        if verbose_log and logger:
            logger.log(f"  Error scanning PID {pid}: {e}")
        return {}

def select_process_interactive():
    """Interactive process selection"""
    processes = find_arr_processes()
    
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
    print(f"  A. Monitor all")
    
    while True:
        try:
            choice = input(f"\nSelect process to monitor [1-{len(processes)}/A]: ").strip()
            if choice.upper() == 'A':
                return [pid for pid, name in processes]
            idx = int(choice) - 1
            if 0 <= idx < len(processes):
                return [processes[idx][0]]
            print("Invalid selection")
        except (ValueError, KeyboardInterrupt):
            return None

def draw_ui(stdscr, pid_list, all_files, last_update, path_cache=None):
    """Draw the curses UI
    
    Args:
        stdscr: Curses screen object
        pid_list: List of process IDs being monitored
        all_files: Dictionary of tracked file transfers
        last_update: Timestamp of last update
        path_cache: Optional dict to cache abbreviated paths
    """
    if path_cache is None:
        path_cache = {}
    try:
        height, width = stdscr.getmaxyx()
    except curses.error:
        # Terminal might be in an invalid state during resize
        return
    
    # Ensure minimum dimensions
    if height < MIN_TERMINAL_HEIGHT or width < MIN_TERMINAL_WIDTH:
        return
    
    try:
        stdscr.erase()
        
        if len(pid_list) == 1:
            try:
                proc = psutil.Process(pid_list[0])
                proc_name = f"{proc.name()} (PID: {pid_list[0]})"
            except:
                proc_name = f"PID: {pid_list[0]}"
        else:
            proc_name = f"Monitoring {len(pid_list)} processes"
        
        header = f"*arr File Transfer Monitor - {proc_name}"
        stdscr.addstr(0, 0, header[:width-1], curses.A_BOLD | curses.color_pair(1))
        stdscr.addstr(1, 0, f"Time: {datetime.now().strftime('%H:%M:%S')}", curses.color_pair(2))
        stdscr.addstr(2, 0, "─" * min(width - 1, 80))
        
        if not all_files:
            stdscr.addstr(4, 0, "No active file writes detected...", curses.color_pair(3))
            stdscr.addstr(height - 1, 0, "Press 'q' to quit", curses.color_pair(2))
            stdscr.noutrefresh()
            curses.doupdate()
            return
        
        row = 4
        for (pid, key), file_info in all_files.items():
            if row >= height - 3:
                break
            
            try:
                proc = psutil.Process(pid)
                proc_name = proc.name()
            except:
                proc_name = f"PID {pid}"
            
            # Green line: [ProcessName] filename
            filename = os.path.basename(file_info.filepath)
            header = f"[{proc_name}] {filename}"
            stdscr.addstr(row, 0, header[:width-1], curses.A_BOLD | curses.color_pair(4))  # Green
            row += 1
            
            # Red line: source path (indented by 2)
            if file_info.source_filepath:
                cache_key = (file_info.source_filepath, width - 3)
                if cache_key not in path_cache:
                    path_cache[cache_key] = abbreviate_path(file_info.source_filepath, width - 3)
                source_display = "  " + path_cache[cache_key]
                stdscr.addstr(row, 0, source_display[:width-1], curses.color_pair(8))  # Red
                row += 1
            
            # Blue line: destination path (indented by 2)
            cache_key = (file_info.filepath, width - 3)
            if cache_key not in path_cache:
                path_cache[cache_key] = abbreviate_path(file_info.filepath, width - 3)
            dest_display = "  " + path_cache[cache_key]
            stdscr.addstr(row, 0, dest_display[:width-1], curses.color_pair(5))  # Blue
            row += 1
            
            bar_width = min(MIN_PROGRESS_BAR_WIDTH, width - PROGRESS_BAR_PADDING)
            if bar_width > 0:
                filled = int((file_info.percent / 100) * bar_width)
                bar = "█" * filled + "░" * (bar_width - filled)
                progress_str = f"  [{bar}] {file_info.percent:.1f}%"
                stdscr.addstr(row, 0, progress_str[:width-1], curses.color_pair(6))
            row += 1
            
            size_str = f"  {format_size(file_info.position)} / {format_size(file_info.target_size)}"
            stdscr.addstr(row, 0, size_str[:width-1], curses.color_pair(2))
            
            if file_info.speed > 0:
                speed_str = f"  Speed: {format_speed(file_info.speed)}"
                eta_str = f"  ETA: {format_time(file_info.eta_seconds)}"
                info = speed_str + eta_str
                if len(size_str) + len(info) < width - 1:
                    stdscr.addstr(row, len(size_str), info[:width-1-len(size_str)], curses.color_pair(7))
            
            row += 2
            
            if row >= height - 2:
                break
        
        stdscr.addstr(height - 1, 0, "Press 'q' to quit"[:width-1], curses.color_pair(2))
        
        stdscr.noutrefresh()
        curses.doupdate()
    except curses.error:
        # Silently handle curses errors during rendering (e.g., terminal resize)
        pass

def run_monitor(stdscr, pid_list, logger=None):
    """Main monitoring loop with curses UI
    
    Args:
        stdscr: Curses screen object
        pid_list: List of process IDs to monitor
        logger: Optional DebugLogger instance
    """
    if logger:
        logger.log(f"run_monitor started with PIDs: {pid_list}")
    
    curses.start_color()
    curses.init_pair(1, curses.COLOR_CYAN, curses.COLOR_BLACK)
    curses.init_pair(2, curses.COLOR_WHITE, curses.COLOR_BLACK)
    curses.init_pair(3, curses.COLOR_YELLOW, curses.COLOR_BLACK)
    curses.init_pair(4, curses.COLOR_GREEN, curses.COLOR_BLACK)
    curses.init_pair(5, curses.COLOR_BLUE, curses.COLOR_BLACK)
    curses.init_pair(6, curses.COLOR_MAGENTA, curses.COLOR_BLACK)
    curses.init_pair(7, curses.COLOR_CYAN, curses.COLOR_BLACK)
    curses.init_pair(8, curses.COLOR_RED, curses.COLOR_BLACK)
    
    stdscr.nodelay(True)
    curses.curs_set(0)
    
    tracked_files = {}
    last_update = time.time()
    iteration = 0
    episode_cache = {}  # Cache for episode info extraction
    path_abbreviation_cache = {}  # Cache for abbreviated paths: (path, width) -> abbreviated_path
    last_terminal_width = 0  # Track terminal width to detect resizes
    
    while True:
        try:
            iteration += 1
            
            key = stdscr.getch()
            if key == ord('q') or key == ord('Q'):
                if logger:
                    logger.log("User quit")
                break
            
            active_pids = [p for p in pid_list if psutil.pid_exists(p)]
            if not active_pids:
                if logger:
                    logger.log("All processes exited")
                stdscr.clear()
                stdscr.addstr(0, 0, "All monitored processes have exited.", curses.A_BOLD)
                stdscr.addstr(1, 0, "Press any key to exit...")
                stdscr.nodelay(False)
                stdscr.getch()
                break
            
            # Only do verbose logging on first iteration or periodically
            # to avoid duplicate scanning overhead
            verbose_this_iteration = (iteration == 1 or iteration % VERBOSE_LOG_INTERVAL == 0) and logger and logger.is_enabled
            
            if verbose_this_iteration:
                logger.log(f"=== Scan iteration {iteration} ===")
            
            # Clear episode cache if it grows too large to prevent unbounded memory growth
            if len(episode_cache) > EPISODE_CACHE_MAX_SIZE:
                if logger:
                    logger.log(f"Episode cache size {len(episode_cache)} exceeded limit, clearing")
                episode_cache.clear()
            
            current_files = {}
            for pid in active_pids:
                pid_files = get_open_files(pid, logger=logger, verbose_log=verbose_this_iteration, episode_cache=episode_cache)
                for key, file_info in pid_files.items():
                    current_files[(pid, key)] = file_info
            
            if verbose_this_iteration:
                logger.log(f"Total files found across all PIDs: {len(current_files)}")
            
            for composite_key, file_info in current_files.items():
                if composite_key in tracked_files:
                    old_pos = tracked_files[composite_key].position
                    tracked_files[composite_key].update(file_info.position, file_info.size)
                    new_pos = tracked_files[composite_key].position
                    if verbose_this_iteration and old_pos != new_pos:
                        logger.log(f"  Updated: {file_info.filename} {old_pos} -> {new_pos}")
                else:
                    tracked_files[composite_key] = file_info
                    if logger:
                        logger.log(f"  New file tracked: {file_info.filename} at {file_info.position}/{file_info.target_size}")
            
            keys_to_remove = [k for k in tracked_files if k not in current_files]
            for key in keys_to_remove:
                if logger:
                    logger.log(f"  File closed: {tracked_files[key].filename}")
                del tracked_files[key]
            
            # Clear path cache on terminal resize to avoid stale cached widths
            try:
                current_width = stdscr.getmaxyx()[1]
                if current_width != last_terminal_width:
                    path_abbreviation_cache.clear()
                    last_terminal_width = current_width
                    if logger and verbose_this_iteration:
                        logger.log(f"Terminal width changed to {current_width}, cleared path cache")
            except curses.error:
                pass
            
            draw_ui(stdscr, active_pids, tracked_files, last_update, path_abbreviation_cache)
            last_update = time.time()
            
            time.sleep(POLL_INTERVAL_SECONDS)
        
        except KeyboardInterrupt:
            if logger:
                logger.log("KeyboardInterrupt")
            break
        except curses.error as e:
            if verbose_this_iteration:
                logger.log(f"Curses error: {e}")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        except Exception as e:
            if logger:
                logger.log(f"Unexpected error: {e}")
            raise

def main():
    parser = argparse.ArgumentParser(
        description='Monitor file write operations for *arr media managers',
        epilog='Examples:\n'
               '  %(prog)s              # Interactive process selection\n'
               '  %(prog)s --all        # Monitor all detected *arr processes\n'
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
    parser.add_argument('--all', action='store_true',
                       help='Automatically monitor all detected *arr processes')
    
    args = parser.parse_args()
    
    # Create logger context manager
    logger = DebugLogger(args.log) if args.log else DebugLogger()
    
    if args.pids:
        pids = args.pids
        for pid in pids:
            if not psutil.pid_exists(pid):
                print(f"Error: Process {pid} does not exist")
                return 1
        
        if len(pids) == 1:
            try:
                proc = psutil.Process(pids[0])
                print(f"Monitoring: {proc.name()} (PID: {pids[0]})")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                print(f"Monitoring PID: {pids[0]}")
        else:
            print(f"Monitoring {len(pids)} processes: {', '.join(map(str, pids))}")
    elif args.all:
        processes = find_arr_processes()
        if not processes:
            print("No *arr processes found running.")
            print(f"\nAvailable managers: {', '.join(ARR_MANAGERS)}")
            return 1
        pids = [pid for pid, name in processes]
        print(f"Auto-detected {len(pids)} process(es):")
        for pid, name in processes:
            print(f"  - {name} (PID: {pid})")
    else:
        pids = select_process_interactive()
        if pids is None:
            return 1
    
    if args.debug:
        for pid in pids:
            print(f"\nDebug: Scanning /proc/{pid}/fd/...")
            files = get_open_files(pid, verbose_log=True)
            if not files:
                print("No files found matching criteria")
            else:
                print(f"\nFound {len(files)} file(s) being written:")
                for key, info in files.items():
                    print(f"\n  FD {info.fd}: {info.filepath}")
                    print(f"    Position: {info.position}, Target: {info.target_size}")
                    print(f"    Percent: {info.percent:.1f}%")
        return 0
    
    for pid in pids:
        try:
            test_dir = Path(f"/proc/{pid}/fd")
            list(test_dir.iterdir())
        except PermissionError:
            print(f"\nError: Permission denied for PID {pid}. Try running with sudo:")
            print(f"  sudo {sys.argv[0]} {' '.join(map(str, pids))}")
            return 1
        except FileNotFoundError:
            print(f"\nError: Process {pid} no longer exists")
            return 1
    
    # Use logger as context manager
    with logger:
        if logger.is_enabled:
            logger.log(f"Starting arr-monitor with args: {sys.argv}")
        
        try:
            if logger.is_enabled:
                logger.log("Starting curses interface")
            curses.wrapper(run_monitor, pids, logger)
            if logger.is_enabled:
                logger.log("Curses interface exited normally")
        except KeyboardInterrupt:
            if logger.is_enabled:
                logger.log("Interrupted by user")
            pass
        except Exception as e:
            if logger.is_enabled:
                logger.log(f"Error in curses interface: {e}")
            raise
        
        if logger.is_enabled:
            logger.log("Exiting")
    
    return 0

if __name__ == '__main__':
    sys.exit(main())