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
from pathlib import Path
from datetime import datetime
from threading import Lock

# Global debug log
DEBUG_LOG_FILE = None
DEBUG_LOG_LOCK = Lock()

def debug_log(message):
    """Write debug message to log file"""
    if DEBUG_LOG_FILE:
        with DEBUG_LOG_LOCK:
            with open(DEBUG_LOG_FILE, 'a') as f:
                timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                f.write(f"[{timestamp}] {message}\n")
                f.flush()

# Media manager process names to auto-detect
ARR_MANAGERS = [
    'Sonarr', 'Radarr', 'Lidarr', 'Readarr', 
    'Prowlarr', 'Bazarr', 'Whisparr'
]

# File extensions to ignore (databases, logs, etc.)
IGNORE_EXTENSIONS = {
    '.db', '.db-wal', '.db-shm', '.db-journal',
    '.log', '.txt', '.xml', '.json', '.conf'
}

class FileTransferInfo:
    """Tracks information about a file being written"""
    def __init__(self, fd, filepath, position, size, target_size=None):
        self.fd = fd
        self.filepath = filepath
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
        if actual_position > self.target_size * 1.1:
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
    """Extract season/episode information from filename"""
    patterns = [
        r'[Ss](\d+)[Ee](\d+)',
        r'(\d+)[xX](\d+)',
        r'[Ss]eason\s*(\d+).*[Ee]pisode\s*(\d+)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, filename)
        if match:
            season = int(match.group(1))
            episode = int(match.group(2))
            return (season, episode)
    
    return None

def find_matching_source(dest_filename, read_files):
    """Find the best matching source file for a destination"""
    dest_lower = dest_filename.lower()
    for src_name, src_size in read_files.items():
        if src_name.lower() == dest_lower:
            return src_size
    
    dest_ep = extract_episode_info(dest_filename)
    if dest_ep:
        for src_name, src_size in read_files.items():
            src_ep = extract_episode_info(src_name)
            if src_ep == dest_ep:
                return src_size
    
    return None

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

def get_open_files(pid, verbose_log=False):
    """Get files currently being written by the process"""
    open_files = {}
    read_files = {}
    
    if verbose_log:
        debug_log(f"=== VERBOSE SCAN of PID {pid} ===")
    
    try:
        fd_dir = Path(f"/proc/{pid}/fd")
        fdinfo_dir = Path(f"/proc/{pid}/fdinfo")
        
        if not fd_dir.exists() or not fdinfo_dir.exists():
            if verbose_log:
                debug_log(f"  /proc/{pid}/fd or fdinfo does not exist")
            return {}
        
        all_fds = {}
        for fd_link in fd_dir.iterdir():
            fd = fd_link.name
            try:
                filepath = fd_link.resolve()
                
                if verbose_log:
                    debug_log(f"  FD {fd}: {filepath}")
                
                if not filepath.is_file():
                    if verbose_log:
                        debug_log(f"    Skipped: not a regular file")
                    continue
                
                try:
                    file_size = filepath.stat().st_size
                except (OSError, FileNotFoundError) as e:
                    if verbose_log:
                        debug_log(f"    Skipped: could not stat - {e}")
                    continue
                
                fdinfo_path = fdinfo_dir / fd
                if not fdinfo_path.exists():
                    if verbose_log:
                        debug_log(f"    Skipped: no fdinfo")
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
                    if verbose_log:
                        debug_log(f"    Skipped: could not read fdinfo - {e}")
                    continue
                
                access_mode = flags & 0o3
                is_ignored = should_ignore_file(str(filepath))
                
                all_fds[fd] = {
                    'filepath': filepath,
                    'file_size': file_size,
                    'position': position,
                    'access_mode': access_mode,
                    'flags': flags,
                    'ignored': is_ignored
                }
                
                if verbose_log:
                    debug_log(f"    size={file_size} pos={position} flags={oct(flags)} mode={access_mode} ignored={is_ignored}")
            
            except (PermissionError, OSError) as e:
                if verbose_log:
                    debug_log(f"  FD {fd_link.name}: Error - {e}")
                continue
        
        for fd, info in all_fds.items():
            access_mode = info['access_mode']
            
            if access_mode == 0:
                filename = info['filepath'].name
                read_files[filename] = info['file_size']
                if verbose_log:
                    debug_log(f"  Read file: {filename} ({info['file_size']} bytes)")
            
            elif access_mode in (1, 2) and not info['ignored']:
                filepath = info['filepath']
                file_size = info['file_size']
                position = info['position']
                
                current_pos = file_size
                
                filename = filepath.name
                target_size = find_matching_source(filename, read_files)
                
                match_method = "none"
                matched_source = "none"
                if target_size is not None:
                    match_method = "pattern/exact"
                    for src_name, src_size in read_files.items():
                        if src_size == target_size:
                            matched_source = src_name
                            break
                elif read_files:
                    target_size = max(read_files.values())
                    match_method = "largest"
                    for src_name, src_size in read_files.items():
                        if src_size == target_size:
                            matched_source = src_name
                            break
                else:
                    target_size = max(file_size, 1)
                    match_method = "fallback"
                
                if verbose_log:
                    debug_log(f"  Write file: {filename}")
                    debug_log(f"    current={file_size} target={target_size} match={match_method} source={matched_source}")
                    debug_log(f"    Available read files: {list(read_files.keys())}")
                
                key = f"{fd}_{filepath}"
                open_files[key] = FileTransferInfo(fd, str(filepath), current_pos, file_size, target_size)
        
        if verbose_log:
            debug_log(f"  Result: {len(open_files)} writable files, {len(read_files)} read files")
        return open_files
    
    except (PermissionError, OSError) as e:
        if verbose_log:
            debug_log(f"  Error scanning PID {pid}: {e}")
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

def draw_ui(stdscr, pid_list, all_files, last_update):
    """Draw the curses UI"""
    height, width = stdscr.getmaxyx()
    
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
            prefix = f"[{proc.name()}] "
        except:
            prefix = f"[PID {pid}] "
        
        filename = prefix + file_info.filename
        if len(filename) > width - 1:
            filename = filename[:width-4] + "..."
        stdscr.addstr(row, 0, filename, curses.A_BOLD | curses.color_pair(4))
        row += 1
        
        filepath = f"  {file_info.filepath}"
        if len(filepath) > width - 1:
            filepath = filepath[:width-4] + "..."
        stdscr.addstr(row, 0, filepath, curses.color_pair(5))
        row += 1
        
        bar_width = min(40, width - 20)
        filled = int((file_info.percent / 100) * bar_width)
        bar = "█" * filled + "░" * (bar_width - filled)
        progress_str = f"  [{bar}] {file_info.percent:.1f}%"
        stdscr.addstr(row, 0, progress_str, curses.color_pair(6))
        row += 1
        
        size_str = f"  {format_size(file_info.position)} / {format_size(file_info.target_size)}"
        stdscr.addstr(row, 0, size_str, curses.color_pair(2))
        
        if file_info.speed > 0:
            speed_str = f"  Speed: {format_speed(file_info.speed)}"
            eta_str = f"  ETA: {format_time(file_info.eta_seconds)}"
            info = speed_str + eta_str
            if len(size_str) + len(info) < width - 1:
                stdscr.addstr(row, len(size_str), info, curses.color_pair(7))
        
        row += 2
        
        if row >= height - 2:
            break
    
    stdscr.addstr(height - 1, 0, "Press 'q' to quit", curses.color_pair(2))
    
    stdscr.noutrefresh()
    curses.doupdate()

def run_monitor(stdscr, pid_list):
    """Main monitoring loop with curses UI"""
    debug_log(f"run_monitor started with PIDs: {pid_list}")
    
    curses.start_color()
    curses.init_pair(1, curses.COLOR_CYAN, curses.COLOR_BLACK)
    curses.init_pair(2, curses.COLOR_WHITE, curses.COLOR_BLACK)
    curses.init_pair(3, curses.COLOR_YELLOW, curses.COLOR_BLACK)
    curses.init_pair(4, curses.COLOR_GREEN, curses.COLOR_BLACK)
    curses.init_pair(5, curses.COLOR_BLUE, curses.COLOR_BLACK)
    curses.init_pair(6, curses.COLOR_MAGENTA, curses.COLOR_BLACK)
    curses.init_pair(7, curses.COLOR_CYAN, curses.COLOR_BLACK)
    
    stdscr.nodelay(True)
    curses.curs_set(0)
    
    tracked_files = {}
    last_update = time.time()
    iteration = 0
    
    while True:
        try:
            iteration += 1
            
            key = stdscr.getch()
            if key == ord('q') or key == ord('Q'):
                debug_log("User quit")
                break
            
            active_pids = [p for p in pid_list if psutil.pid_exists(p)]
            if not active_pids:
                debug_log("All processes exited")
                stdscr.clear()
                stdscr.addstr(0, 0, "All monitored processes have exited.", curses.A_BOLD)
                stdscr.addstr(1, 0, "Press any key to exit...")
                stdscr.nodelay(False)
                stdscr.getch()
                break
            
            if iteration % 10 == 1:
                debug_log(f"=== Scan iteration {iteration} ===")
            
            current_files = {}
            has_writable = False
            for pid in active_pids:
                pid_files = get_open_files(pid, verbose_log=False)
                if pid_files:
                    has_writable = True
                for key, file_info in pid_files.items():
                    current_files[(pid, key)] = file_info
            
            if has_writable and iteration % 10 == 1:
                debug_log("=== Writable files detected, running verbose scan ===")
                for pid in active_pids:
                    get_open_files(pid, verbose_log=True)
            
            if iteration % 10 == 1:
                debug_log(f"Total files found across all PIDs: {len(current_files)}")
            
            for composite_key, file_info in current_files.items():
                if composite_key in tracked_files:
                    old_pos = tracked_files[composite_key].position
                    tracked_files[composite_key].update(file_info.position, file_info.size)
                    new_pos = tracked_files[composite_key].position
                    if iteration % 10 == 1 and old_pos != new_pos:
                        debug_log(f"  Updated: {file_info.filename} {old_pos} -> {new_pos}")
                else:
                    tracked_files[composite_key] = file_info
                    debug_log(f"  New file tracked: {file_info.filename} at {file_info.position}/{file_info.target_size}")
            
            keys_to_remove = [k for k in tracked_files if k not in current_files]
            for key in keys_to_remove:
                debug_log(f"  File closed: {tracked_files[key].filename}")
                del tracked_files[key]
            
            draw_ui(stdscr, active_pids, tracked_files, last_update)
            last_update = time.time()
            
            time.sleep(0.5)
        
        except KeyboardInterrupt:
            debug_log("KeyboardInterrupt")
            break
        except curses.error as e:
            if iteration % 10 == 1:
                debug_log(f"Curses error: {e}")
            time.sleep(0.1)
            continue
        except Exception as e:
            debug_log(f"Unexpected error: {e}")
            raise

def main():
    global DEBUG_LOG_FILE
    
    parser = argparse.ArgumentParser(
        description='Monitor file write operations for *arr media managers'
    )
    parser.add_argument('pids', type=int, nargs='*', 
                       help='Process ID(s) to monitor')
    parser.add_argument('-d', '--debug', action='store_true',
                       help='Show debug information')
    parser.add_argument('--log', type=str, metavar='FILE',
                       help='Enable debug logging to specified file')
    
    args = parser.parse_args()
    
    # Only enable logging if --log is specified
    if args.log:
        DEBUG_LOG_FILE = args.log
        try:
            with open(DEBUG_LOG_FILE, 'w') as f:
                f.write(f"=== *arr Monitor Debug Log - {datetime.now()} ===\n\n")
            debug_log(f"Starting arr-monitor with args: {sys.argv}")
        except Exception as e:
            print(f"Warning: Could not create debug log at {DEBUG_LOG_FILE}: {e}")
            DEBUG_LOG_FILE = None
    
    if args.pids:
        pids = args.pids
        for pid in pids:
            if not psutil.pid_exists(pid):
                print(f"Error: Process {pid} does not exist")
                debug_log(f"Error: Process {pid} does not exist")
                return 1
        
        if len(pids) == 1:
            try:
                proc = psutil.Process(pids[0])
                print(f"Monitoring: {proc.name()} (PID: {pids[0]})")
                debug_log(f"Monitoring: {proc.name()} (PID: {pids[0]})")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                print(f"Monitoring PID: {pids[0]}")
                debug_log(f"Monitoring PID: {pids[0]}")
        else:
            print(f"Monitoring {len(pids)} processes: {', '.join(map(str, pids))}")
            debug_log(f"Monitoring {len(pids)} processes: {', '.join(map(str, pids))}")
    else:
        pids = select_process_interactive()
        if pids is None:
            return 1
        debug_log(f"Selected PIDs: {pids}")
    
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
    
    try:
        debug_log("Starting curses interface")
        curses.wrapper(run_monitor, pids)
        debug_log("Curses interface exited normally")
    except KeyboardInterrupt:
        debug_log("Interrupted by user")
        pass
    except Exception as e:
        debug_log(f"Error in curses interface: {e}")
        raise
    
    debug_log("Exiting")
    return 0

if __name__ == '__main__':
    sys.exit(main())